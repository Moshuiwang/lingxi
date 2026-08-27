"""Agent worker 的队列消费、心跳、停止和终态收口。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from lingxi.adapters.postgres_conversation import ClaimedTask, PostgresTaskQueue, TerminalTask
from lingxi.adapters.user_mcp_config import UserMcpConfigError, load_user_mcp_servers
from lingxi.apps.worker.config import QUERY_MCP_TOOL_PREFIX, WorkerConfig
from lingxi.apps.worker.session_cleanup import delete_agent_session_files
from lingxi.apps.worker.turn import WorkerTurnExecutor
from lingxi.config.content import ContentCatalog, RenderedContent, default_content_catalog
from lingxi.core.delivery.ports import DeliveryEventType, TerminalKind, assert_content_allowed
from lingxi.core.execution.card_stream import (
    PROGRESS_ACTION_COMPOSING,
    PROGRESS_ACTION_PROCESSING,
    PROGRESS_ACTION_QUERYING,
    encode_progress_action,
)
from lingxi.core.innertest_content_capture import ContentCaptureRecord
from lingxi.core.year_grounding_guard import detect_year_grounding_suspect

logger = logging.getLogger(__name__)

# `claim()` 前重判停机信号需要跨过 `loop.add_signal_handler` 自管道机制的完整
# 投递链路（PR #173 独立复核第二轮 P2-1）：OS 送达信号 → 自管道被写入一个
# 字节 → 事件循环下一轮 `select()` 才发现可读，把**自管道 reader 回调**排进
# 就绪队列并在同一轮执行 → 该回调再把**真正的用户回调**（这里是
# `stop.set()`）通过 `_add_callback_signalsafe` 排进下一轮就绪队列 → 再下一轮
# 才真正执行。经真实 SIGTERM + 真实 `add_signal_handler` 实测（5/5、3/3 稳定
# 复现，见该轮评论）：`await asyncio.sleep(0)` 只能让当前协程前进一轮，必须
# 连续让出 **3 轮**才能观测到 `is_set()` 变为 True——上一版只让出一次，对
# 真实信号无效（对 `loop.call_soon` 模拟的假信号有效，因为它跳过了自管道的
# 两跳，这也是为什么当时新增用例是绿的、生产路径依旧漏的原因）。这里给到
# 5 轮，在实测必需的 3 轮之上留出实现细节漂移的余量；一旦提前观测到
# `is_set()` 就立即返回，不多空转。
_STOP_SIGNAL_DRAIN_YIELDS = 5

# 语义化等待进度（Issue #321 方向 C，产品负责人 2026-08-27 裁定，留痕 #321 评论
# 5434086490）：长问数期间卡片显示阶段性文案（「正在第 N 次查询指标数据」/「正在
# 整理与生成回答」），覆盖两次模型输出之间卡片完全静止的问题。两个阈值共享同一份
# 「上次真正写库的进度更新是什么时候」状态（见 `_process_task` 里的
# `_write_progress_if_due`），从两个不同的调用点检查：
# - 事件驱动（工具调用开始、模型文本输出）最短间隔 `_PROGRESS_MIN_UPDATE_
#   INTERVAL_SECONDS`——防止工具事件密集时把 outbox 写爆、把 CardKit 限流(500ms/
#   话题)进一步逼近；
# - 兜底计时驱动（`_monitor` 每轮调用一次）最长间隔 `_PROGRESS_FALLBACK_SECONDS`
#   ——距上次更新超过这个阈值时强制推一次纯用时更新，即使没有任何新信号。
# 两者是同一个节流函数在同一份状态上的两种阈值，不是两套独立机制。
_PROGRESS_MIN_UPDATE_INTERVAL_SECONDS = 5.0
_PROGRESS_FALLBACK_SECONDS = 30.0


class QueueListener(Protocol):
    def wait(self, *, timeout_seconds: float) -> bool: ...


ExecutorFactory = Callable[[WorkerConfig, Callable[[], None]], Any]
HeartbeatCallback = Callable[[], None]
TaskStuckCallback = Callable[[str, int], None]
# 终态收口低敏审计事件（Issue #90 评论 5306860255 的独立复核 P1）：字段名与取值
# 见 ``WorkerService._log_terminal_outcome``。``WorkerService`` 是纯组装对象，
# 不知道自己会被哪个进程入口装配，也不该假设 stdlib ``logging`` 有 handler——
# 真实队列 worker 的 `apps/worker/cli.py` 刻意从不调用 `logging.basicConfig()`
# （见该文件 `_LogOnlyAlertSender` 的说明：未配置 handler 时默认阈值
# `WARNING` 会把 `logging.info(...)` 悄悄吞掉），因此这条低敏审计事件必须像
# `heartbeat`/`on_task_stuck`/`on_alert_tick` 一样由装配层注入真正的输出出口，
# 不能自己直接调 `logging`。
TerminalOutcomeCallback = Callable[[Mapping[str, object]], None]
# 年份接地护栏第二层的结构化告警出口（Issue #326）：与 ``TerminalOutcomeCallback``
# 同一条纪律——``WorkerService`` 不直接调 stdlib ``logging``（理由同上），检测到
# 的信号必须交给装配层注入的回调，由 ``apps/worker/cli.py`` 接到既有的结构化
# stderr 出口（``worker.year_grounding_suspect``，带 trace_id）。``None`` 时
# ``_check_year_grounding_suspect`` 整体跳过，不做检测、不产生任何额外开销。
YearGroundingSuspectCallback = Callable[[Mapping[str, object]], None]
_MAX_LOG_TOKEN_CHARS = 64
# 独立审查（Issue #291 拒绝文案对用户承诺"问题已经被记录"）：一次回合里模型
# 可能反复撞同一个越界工具，工具名列表上界防止一次异常回合把收口日志撑大。
_MAX_LOG_DENIED_TOOL_NAMES = 20


def _cap_log_token(value: str) -> tuple[str, bool]:
    """把失败码/安全原因码这类短标识截到审计安全的长度上界（Issue #90 评论
    5306860255 的独立复核 P3-2）。这些值目前全部来自本仓库固定的枚举式常量
    （``turn.py``/``report.py`` 的失败码、``input_safety`` 的原因码），不是模型
    输出，但收口日志是低敏审计的唯一出口——不给未来新增码值设长度上界，就是
    给"某次改动不小心把一段自由文本塞进这个字段"留了一条不设防的泄漏面。
    """

    if len(value) <= _MAX_LOG_TOKEN_CHARS:
        return value, False
    return value[:_MAX_LOG_TOKEN_CHARS], True


def _denied_tool_summary(report: Mapping[str, Any]) -> tuple[int, tuple[str, ...]]:
    """从一次回合报告里取出被拒工具调用的计数与工具名（独立审查，Issue #291）。

    ``tool_policy.py`` 的拒绝文案对用户承诺"这是系统侧的临时限制、问题已经被
    记录"，但此前这句话只在 ``LINGXI_WORKER_MODE=turn``（受控验证用的单回合
    模式）里是真的——``report["audit"]["denied_count"]`` 早就算出来了
    （见 ``report.py``），真正处理用户任务的 queue 模式（``_process_task``）却
    从未把它读出来过，运维在生产 stderr 里看不到任何一次拒绝，白名单配错时
    只能像 #291 真实事故那样靠用户反馈才发现。这里只取计数与工具名——不取
    ``tool_input``，工具参数正文与用户资料值不属于这条低敏审计事件。

    ``report["audit"]`` 在早退分支（开工前已 ``stop_requested``、读用户 MCP
    配置失败、执行器抛出未预期异常）不存在——这些分支从未真正跑过一次
    ``PreToolUse`` 判定，取不到就如实记 0/空，不假装有据可查。

    **已知边界（独立审查 codex P2-A5，如实登记、不修）**：与上一段"从未跑过判定"
    不同的是另一种时序——这一回合**已经**发生过至少一次真实的 ``PreToolUse``
    拒绝，但 executor 在拒绝**之后**异常退出（未预期异常，落进本文件顶部
    ``except Exception`` 那一类早退分支，``report`` 不带 ``audit`` 字段）。这种
    情况下本轮已经发生过的拒绝计数会跟着这份不完整的 ``report`` 一起丢失——
    ``_denied_tool_summary`` 同样如实返回 0/空，不去猜、也无法从这份 ``report``
    里补回来。可以接受：这轮回合本身已经落到一个响亮的失败终态（未预期异常
    带着 ``type(error).__name__`` 收口，见 ``_process_task`` 的失败分支），运维
    看得到"这一轮坏了"；唯一的代价是看不到"坏之前它还拒绝过几次工具调用"这个
    补充事实，不是静默丢失整轮结果。
    """

    audit = report.get("audit") if isinstance(report, Mapping) else None
    if not isinstance(audit, Mapping):
        return 0, ()
    count = audit.get("denied_count")
    count = count if isinstance(count, int) else 0
    names: list[str] = []
    denied_entries = audit.get("denied")
    if isinstance(denied_entries, list):
        for entry in denied_entries:
            if isinstance(entry, Mapping):
                name = entry.get("tool_name")
                if isinstance(name, str):
                    names.append(name)
    return count, tuple(names[:_MAX_LOG_DENIED_TOOL_NAMES])


def _report_guard_denied_count(report: Mapping[str, Any]) -> int | None:
    """从一次回合报告里取出**供落库**的守卫拒绝计数（Issue #303/#304 批次 4，
    迁移 ``0070``）。

    与 :func:`_denied_tool_summary` 故意**不共享**同一个返回值：那个函数服务
    低敏结构化日志，"取不到就如实记 0"是它自己文档写明的姿态，对人工看
    stderr 排障是合理的简化。这里要写进 ``task.guard_denied_count``，供
    ``core/daily_report.py`` 做统计聚合——聚合层必须能区分"这一轮真的查过、
    结果是零次拒绝"与"这一轮没有可用的审计数据"，把后者也算成 0 会让通报正文
    悄悄低估真实拒绝次数，且**没有任何信号**能让读者发现这一段被低估过（"不可
    判定"纪律要挡的正是这种静默失真，见 ``core/daily_report.py`` 模块文档）。

    ``report["audit"]`` 不存在（早退分支：开工前已 ``stop_requested``、读用户
    MCP 配置失败、执行器抛出未预期异常，均从未真正跑过一次 ``PreToolUse``
    判定）时返回 ``None``；``denied_count`` 存在但不是 ``int``、或是负数
    （结构不符预期，结构性地不可信——拒绝计数不存在"负几次"，出现负数只可能是
    上游数据被破坏，与 :func:`_report_token_usage` 对同一类不可信数字的处理
    对称，批次 4 opus 审查 P3-2）同样返回 ``None``。其余情况原样返回真实整数，
    包括合法的 0。
    """

    audit = report.get("audit") if isinstance(report, Mapping) else None
    if not isinstance(audit, Mapping):
        return None
    count = audit.get("denied_count")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        return count
    return None


#: ``core/execution/message_stream.py::_usage_summary`` 产出的四个已知 token
#: 计数字段名，与该模块的 ``_USAGE_TOKEN_FIELDS`` 同源——这里独立列一份常量
#: 而不是 import 那个私有名字，避免给 ``core/execution`` 增加一个只为了这四个
#: 字符串常量存在的跨层依赖（``apps/`` 依赖 ``core/`` 是允许方向，但没必要为
#: 四个字面量常量新增一条 import 边界）。真正的口径来源仍是那个模块，字段名
#: 一旦那边改动，这里也要跟着改（无自动化保证，人工同步）。
_TOKEN_USAGE_FIELD_NAMES = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _report_token_usage(report: Mapping[str, Any]) -> dict[str, int] | None:
    """从一次回合报告里取出**供落库**的 token 用量（Issue #303/#304 批次 4，
    迁移 ``0070``）。

    ``report["resources"]["usage"]``（与 ``report["audit"]["usage"]`` 是同一个
    对象，见 ``apps/worker/report.py::build_report``）恒为
    ``{"status": "known"|"unknown", "source": ..., ["fields": {...}]}``
    （``core/execution/message_stream.py::_usage_summary``）。只有
    ``status == "known"`` 时才有真正可信的计数——``"unknown"`` 覆盖"这一轮从未
    收到 SDK 的 ``ResultMessage``""SDK 没给 usage 字段""给了字段但一个已知计数
    都不认识"三种取不到的原因，共同点是**没有可入库的数字**，返回 ``None``
    如实反映，不编造 0（与 :func:`_report_guard_denied_count` 同一纪律）。

    早退分支（``report`` 不带 ``resources``）同样返回 ``None``。返回值只包含
    ``fields`` 里实际出现的键——``_usage_summary`` 本来就"取到几个算几个"，这里
    原样透传，不为缺失的字段补零。
    """

    resources = report.get("resources") if isinstance(report, Mapping) else None
    usage = resources.get("usage") if isinstance(resources, Mapping) else None
    if not isinstance(usage, Mapping) or usage.get("status") != "known":
        return None
    fields = usage.get("fields")
    if not isinstance(fields, Mapping):
        return None
    result: dict[str, int] = {}
    for name in _TOKEN_USAGE_FIELD_NAMES:
        candidate = fields.get(name)
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
            result[name] = candidate
    return result or None


def _report_document_request(report: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """从一次回合报告里取出**供落库**的文档投递请求（Issue #341 S-ES-3）。

    ``report["document_request"]`` 由 ``apps/worker/report.py::build_report``
    投影（见该函数文档）：``None`` 或已过硬上限与出口安全检查的
    ``{"title": str, "paragraphs": list[str]}``。这里只做结构校验，形状不对一律
    返回 ``None``——与 :func:`_report_guard_denied_count` 同一纪律：结构性地
    不可信就不传，不猜测、不编造。
    """

    request = report.get("document_request") if isinstance(report, Mapping) else None
    if not isinstance(request, Mapping):
        return None
    title = request.get("title")
    paragraphs = request.get("paragraphs")
    if not isinstance(title, str) or not title:
        return None
    if not isinstance(paragraphs, list) or not paragraphs or not all(
        isinstance(paragraph, str) for paragraph in paragraphs
    ):
        return None
    return {"title": title, "paragraphs": paragraphs}


def _tool_result_count(report: Mapping[str, Any]) -> int:
    """从一次回合报告里取出这一轮**真实**工具调用次数（Issue #291 L6 取证结论补的
    可观测性缺口）。

    2026-08-22 那次取证：模型（qwen3.7-plus）把工具调用非确定性地写成了正文散文，
    回合仍然 ``closed=True`` 收口，一段被出口净化层遮蔽过的 tool_use JSON 被当成
    「查询完成」交付给用户。定位这件事花了 40 分钟，因为运维手里没有任何一个字段
    能直接回答"这一轮到底有没有真的调用过工具"——只能翻完整条 Claude Agent SDK
    事件流去数。``report["audit"]["tool_result_count"]``（``report.py`` 的
    ``stream.tool_result_count``）早就算出来了，只是从未随终态审计事件一起离开
    进程。这里把它取出来，写进 ``worker.task.terminal``；今天有这个字段，同一次
    取证是 1 分钟，不是 40 分钟。

    ``tool_result_count == 0`` **单独不构成**异常信号——闲聊类问题本来就不需要
    调用任何工具，这条字段只负责"如实记录事实"，不在这里做任何判定。真正的判定
    见 ``_protocol_breakdown_reasons``：那条只认 ``output_safety.reasons``，与
    这里的调用次数无关，不能把两者混为一谈（各自的判据必须能独立解释各自的
    产品事实）。

    ``report["audit"]`` 在早退分支（开工前已 ``stop_requested``、读用户 MCP 配置
    失败、执行器抛出未预期异常）不存在，如实返回 0，理由与 ``_denied_tool_
    summary`` 相同：不假装有据可查。
    """

    audit = report.get("audit") if isinstance(report, Mapping) else None
    if not isinstance(audit, Mapping):
        return 0
    count = audit.get("tool_result_count")
    return count if isinstance(count, int) else 0


# 默认提示词文件的单次读取上界（2026-08-23，产品负责人裁定提示词外置为挂载卷
# 文件、随时可改）：提示词是几百到几千字的行为指令，64KiB 已远超合理体量；不设
# 上界，一次误操作（比如把数据文件拷成提示词文件名）就会把巨块文本塞进每一轮
# 模型上下文，成本失控且难以察觉。超限按"不可用"降级，与文件缺失同一路径。
_MAX_SYSTEM_PROMPT_BYTES = 64 * 1024


def _load_task_system_prompt(path: str) -> tuple[str | None, str | None, str | None]:
    """每个任务开始时现读默认提示词文件，返回 ``(提示词, 内容摘要, 降级原因)``。

    现读（而不是启动时读一次）就是这个机制的全部意义：运维编辑挂载卷上的文件后
    **下一条消息即生效**，不需要重启容器或重建镜像。各类不可用（缺失/不可读、
    非普通文件、超限、空文件）一律降级为 ``(None, None, 原因码)``——提示词是行为
    调优不是安全屏障，把任务押在一个随手可改的文件上才是更大的风险；降级必须
    留痕，由调用方写结构化告警。

    读取姿态（外部独立审查 2026-08-23 P1-2/P1-3）：``O_NOFOLLOW`` 拒绝符号链接
    ——worker 同时挂着含用户 MCP 令牌的用户环境卷，一个指向 ``.mcp.json`` 的
    链接会把凭据喂进模型上下文，而出口安全发生在模型执行之后、撤不回已发送的
    系统提示；``O_NONBLOCK`` + 普通文件校验拒绝 FIFO/设备文件——对 FIFO 的
    普通 open 会无限阻塞事件循环，心跳与停止处理一起停摆；**有界读取**（至多
    上界 + 1 字节）保证误放一个数 GiB 文件时不整读进内存。残余边界（如实登记，
    不在本层修）：路径本身来自部署配置（0600 的 env 文件，运维控制），本层不
    再对路径做目录白名单——代码里写死允许目录违反「不硬编码路径」（V-部署-01），
    换一个环境变量做白名单则只是把同一信任问题往上挪一层。

    读到内容后还有一道**终态文案自检**：出口安全层会把提示词逐句派生成禁词遮蔽
    模型正文（``input_safety._derive_fragments``），若提示词与固定终态文案
    （空产出兜底/整段拒发）互相命中，空产出回合会在 ``constrain_output`` 的终态
    自检里抛 ``InputSafetyError``，把"总是返回一份报告"的契约炸掉。这里用同一个
    公开函数预演一遍：命中即降级（原因码 ``terminal_text_collision``），坏提示词
    只废掉自己，不废掉回合。

    摘要（sha256 前 12 位）随终态审计事件落日志——「用户这一轮**选定**的是哪版
    提示词」与 content.toml 的版本纪律同一动机；记录口径是"本轮解析出并交给
    执行器装配的版本"，不声称模型一定收到了它（装配或建连失败的回合会带着摘要
    落失败终态，此时摘要回答的是"失败那一轮试图使用哪版"）。只记摘要不记正文。
    """

    import os as _os
    import stat as _stat

    from lingxi.core.execution.input_safety import (
        SAFE_OUTPUT_FALLBACK,
        WITHHELD_MESSAGE,
        InputSafetyError,
        constrain_output,
    )

    try:
        fd = _os.open(path, _os.O_RDONLY | _os.O_NOFOLLOW | _os.O_NONBLOCK)
    except OSError:
        return None, None, "unreadable"
    try:
        if not _stat.S_ISREG(_os.fstat(fd).st_mode):
            return None, None, "not_regular_file"
        chunks: list[bytes] = []
        remaining = _MAX_SYSTEM_PROMPT_BYTES + 1
        while remaining > 0:
            try:
                chunk = _os.read(fd, remaining)
            except OSError:
                return None, None, "unreadable"
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        _os.close(fd)
    if len(raw) > _MAX_SYSTEM_PROMPT_BYTES:
        return None, None, "oversized"
    try:
        prompt = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None, None, "not_utf8"
    if not prompt:
        return None, None, "empty"
    try:
        for fixed_text in (SAFE_OUTPUT_FALLBACK, WITHHELD_MESSAGE):
            if constrain_output(fixed_text, system_prompt=prompt).blocked:
                return None, None, "terminal_text_collision"
    except InputSafetyError:
        return None, None, "terminal_text_collision"
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
    return prompt, digest, None


# P0 护栏（Issue #291 L6 取证结论）：模型正文里出现内部工具名或过程标记
# （``mcp__``、``tool_use_id``、``trace_id`` 这类协议细节），**永远**是模型把
# 工具调用协议写成了正文散文，不是"内容需要脱敏但业务结论还在"。
# ``core/execution/input_safety.py`` 的净化层职责到"遮蔽敏感片段"为止——它不
# 判断"这段正文根本不该被当成答案交付"，那个判断只能发生在这里（收到 `output_
# safety.reasons` 的调用方）。刻意排除其余原因码（``forbidden_value``/
# ``forbidden_fragment``/``system_prompt_marker``）：那三类是"内容含有已知敏感
# 值/系统提示"，`withheld` 分支已经按"是否还有幸存业务内容"正确处理，不属于本
# 护栏收紧的范围，收紧过窄的边界会误伤正常业务回答。
_PROTOCOL_BREAKDOWN_REASON_CODES = frozenset({"internal_tool_name", "process_marker"})
_MODEL_PROTOCOL_BREAKDOWN_FAILURE_CODE = "model_protocol_breakdown"


def _protocol_breakdown_reasons(output_safety: Mapping[str, Any] | None) -> tuple[str, ...]:
    """从 ``turn.output_safety.reasons`` 里取出命中 P0 护栏的原因码（如果有）。

    只做字面判定，不猜测：``reasons`` 形状不对就如实返回空元组，交给上层按
    "没有命中"处理——护栏要收紧的是"命中了却被当成成功"，不是"形状可疑就一律
    判失败"（那会把真实的执行器异常伪装成协议异常，污染审计）。
    """

    if not isinstance(output_safety, Mapping):
        return ()
    raw_reasons = output_safety.get("reasons")
    if not isinstance(raw_reasons, (list, tuple)):
        return ()
    return tuple(
        str(reason) for reason in raw_reasons if str(reason) in _PROTOCOL_BREAKDOWN_REASON_CODES
    )


class WorkerService:
    """一个进程持续消费一个固定 target version 的任务。

    ``process_once`` 是白盒测试和受控演练的入口；``run`` 才是长期 worker 循环。队列
    的 LISTEN 只用于降低延迟，任何一轮都会走轮询与回收检查，所以丢 NOTIFY 不会让
    queued 永久悬挂。

    **投递意图只落数据库，不直接调用飞书**（Issue #151 状态合同）：任务收口时写入
    ``task_delivery_event`` 的 ``started``/``progress``/``terminal`` 事件并把任务
    转为 ``awaiting_delivery``；把事件消费为真实飞书卡片/文本、记录
    ``platform_received`` 并最终收敛业务状态是 Gateway（#152）的职责，本类不再
    持有任何出站 transport。
    """

    def __init__(
        self,
        *,
        config: WorkerConfig,
        queue: Any,
        executor_factory: ExecutorFactory | None = None,
        listener_factory: Callable[[], QueueListener] | None = None,
        catalog: ContentCatalog | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        heartbeat: HeartbeatCallback | None = None,
        on_task_stuck: TaskStuckCallback | None = None,
        session_root: Path | None = None,
        session_cleanup_batch_limit: int = 20,
        on_alert_tick: Callable[[], None] | None = None,
        on_terminal_outcome: TerminalOutcomeCallback | None = None,
        content_capture_writer: Callable[[ContentCaptureRecord], None] | None = None,
        on_year_grounding_suspect: YearGroundingSuspectCallback | None = None,
    ) -> None:
        self._config = config
        self._queue = queue
        self._executor_factory = executor_factory or (
            lambda worker_config, marker: WorkerTurnExecutor(
                worker_config,
                mark_external_side_effect=marker,
                # 常驻 queue worker 必须让 SIGTERM 停机预算耗尽时的取消原样传播
                # 出来（PR #173 独立复核 P1-3）：默认行为（一次性 turn 模式 CLI
                # 那份）会把取消吞成一份"已取消"的失败报告并正常返回，
                # `_process_task` 就会把它当成真实完成的回合同步写终态——预算
                # 耗尽后任务本该保持 `running`、交给心跳超时回收
                # （`reclaim_stale_with_outcomes`，已在 #90/#151 验证），不该被
                # 写成一次可能失真的 FAILED 终态。见 `WorkerTurnExecutor.run_turn`
                # 里 `propagate_cancellation` 的完整说明。
                propagate_cancellation=True,
                # 内测轮内容级采集（Issue #251/#304 批次 3）：`worker_config` 是
                # `_process_task` 按任务覆盖出的 `task_config`（见该方法），
                # `innertest_content_capture_enabled` 未被那次覆盖触碰，取值恒等
                # 于装配时的 `config.innertest_content_capture_enabled`。默认
                # False 时这里传 False，`WorkerTurnExecutor` 不构造任何收集器
                # ——与"默认关闭不产生额外行为"同一条纪律在这一层的体现。
                capture_raw_content=worker_config.innertest_content_capture_enabled,
            )
        )
        self._listener_factory = listener_factory
        self._catalog = catalog or default_content_catalog()
        self._sleep = sleep
        self._monotonic = monotonic
        self._heartbeat = heartbeat
        self._on_task_stuck = on_task_stuck
        # Agent 会话 JSONL 物理清理（Issue #153）：``None`` 表示当前环境没有可用的
        # 会话根目录（例如缺 ``HOME``），此时 ``_cleanup_agent_sessions`` 整体跳过，
        # 不触碰清理队列——留着排队等下一个配置正确的进程来处理，而不是假装已清理。
        self._session_root = session_root
        self._session_cleanup_batch_limit = session_cleanup_batch_limit
        # 告警状态机的恢复计时与重试投递都需要被定期"戳一下"（Issue #153）；worker
        # 没有 scheduler 那种专门的定时职责循环，借用每轮收口顺便调用。
        self._on_alert_tick = on_alert_tick
        # 终态收口低敏审计事件的真正输出出口（Issue #90 评论 5306860255 独立复核
        # P1）：``None`` 时 `_log_terminal_outcome` 整体跳过——没有装配方就没有
        # 输出，不假装写了一条实际被吞掉的日志。
        self._on_terminal_outcome = on_terminal_outcome
        # 内测轮内容级采集的落库出口（Issue #251/#304 批次 3）：``None`` 时
        # `_capture_content_if_enabled` 整体跳过，不构造记录、不尝试写库——与
        # `_on_terminal_outcome` 同一姿态，没有装配方就没有输出。真正的装配
        # 判断（是否按开关构造一个真实写入方）在 `apps/worker/cli.py`。
        self._content_capture_writer = content_capture_writer
        # 年份接地护栏第二层（Issue #326）：``None`` 时 `_check_year_grounding_
        # suspect` 整体跳过——没有装配方就不做检测，与 `_on_terminal_outcome`/
        # `_content_capture_writer` 同一姿态。真正的装配（是否接一个真实 sink）
        # 在 `apps/worker/cli.py`。
        self._on_year_grounding_suspect = on_year_grounding_suspect
        # SIGTERM 收到后由 run() 设置：在途任务的 `_monitor` 据此把"进程正在停机"
        # 与"用户发了 /stop"同等看待，主动请求 Agent SDK 中断当前回合（Issue #153
        # 完成标准第 3 条）。默认 None，保持 process_once()/白盒测试的既有行为不变。
        self._global_stop: asyncio.Event | None = None

    async def process_once(self) -> bool:
        """做一轮回收、领取和执行；返回这一轮是否观察到任务。"""

        self._emit_heartbeat()
        self._tick_alerts()
        terminal_tasks = self._housekeep()

        # 再判一次停机信号，紧贴在 claim() 之前（PR #173 独立复核 P2-1，第二轮
        # 复核证明单次 `sleep(0)` 对真实信号无效，见模块顶部
        # `_STOP_SIGNAL_DRAIN_YIELDS` 的机制说明）：`run()` 的
        # `while not stop.is_set()` 判定与这里的 `claim()` 之间没有任何 await
        # （心跳、告警 tick、`_housekeep()` 全是同步代码），信号如果恰好在这段
        # 同步窗口内被操作系统送达，`self._global_stop.is_set()` 在没有真正让
        # 事件循环跑完自管道投递链路之前读到的都是旧值——会把一条还在排队、
        # 从未执行过的任务领走，直接收口成 `stopped` 且不会被重排。因此连续
        # 让出 `_STOP_SIGNAL_DRAIN_YIELDS` 轮（一旦提前观测到 `is_set()` 立即
        # 返回，不多空转），让"停止新领取"（#153 完成标准第 3 条）在这个窗口
        # 里也真正成立。`self._global_stop` 为 ``None`` 时（`process_once()`
        # 的白盒调用方与既有测试）跳过整段，行为不变、不多一次调度。
        if self._global_stop is not None:
            for _ in range(_STOP_SIGNAL_DRAIN_YIELDS):
                await asyncio.sleep(0)
                if self._global_stop.is_set():
                    return bool(terminal_tasks)

        tasks = self._queue.claim(
            worker_id=self._config.worker_id,
            target_worker_version=self._config.target_worker_version,
            limit=self._config.max_concurrency,
        )
        if not tasks:
            return bool(terminal_tasks)
        await asyncio.gather(*(self._process_task(task) for task in tasks))
        return True

    def _housekeep(self) -> list[TerminalTask]:
        terminals: list[TerminalTask] = []
        fail_versions = getattr(self._queue, "fail_unavailable_versions", None)
        if fail_versions is not None:
            unavailable = fail_versions(
                available_versions=(self._config.target_worker_version,),
                unavailable_for=timedelta(
                    seconds=self._config.worker_version_unavailable_seconds
                )
            )
            terminals.extend(unavailable)
            # 独立于"排队太久"（queued_stuck）：这一类是"目标 worker 版本压根没有
            # 可用实例"，运维需要看到的诊断动作不同（部署缺一个版本 vs 单纯积压），
            # 因此用独立的告警类型（Issue #153 最小可观测性第四类）。
            self._report_task_stuck("worker_version_unavailable", len(unavailable))
        reclaim_queued = getattr(self._queue, "reclaim_queued", None)
        if reclaim_queued is not None:
            queued = reclaim_queued(
                max_wait=timedelta(seconds=self._config.queue_max_wait_seconds)
            )
            terminals.extend(queued)
            self._report_task_stuck("queued_stuck", len(queued))
        reclaim_stale = getattr(self._queue, "reclaim_stale_with_outcomes", None)
        if reclaim_stale is not None:
            requeued, stale_terminals = reclaim_stale(
                older_than=timedelta(seconds=self._config.running_heartbeat_timeout_seconds),
                max_auto_retries=self._config.max_auto_retries,
            )
            terminals.extend(stale_terminals)
            self._report_task_stuck(
                "running_heartbeat_timeout", len(requeued) + len(stale_terminals)
            )
            self._report_task_stuck(
                "retry_exhausted",
                sum(item.error_kind == "retry_exhausted" for item in stale_terminals),
            )
        # 二十四小时到期仍未确认送达的投递终态：状态合同第 8 条、V-投递-06。
        # 这一步只强制收敛任务状态、释放话题并清空事件正文；把清理结果对外展现
        # 为"投递已过期，请重新提问"仍是 Gateway（下一次用户主动消息触发）的职责。
        # 二十四小时上限不接受这里传参：它由迁移 0059 的触发器锁定在
        # task_delivery_event.expires_at 列上，调用方不再持有另一份可以让它
        # 漂移的窗口配置（内审 P2-1）。
        expire_undelivered = getattr(self._queue, "expire_undelivered_terminals", None)
        if expire_undelivered is not None:
            expired = expire_undelivered()
            terminals.extend(expired)
            # 与 core.alerting.AlertKind.AWAITING_DELIVERY_STUCK 对齐（Issue #153
            # 最小可观测性第二类：queued/running/awaiting-delivery 滞留三选一）。
            self._report_task_stuck("awaiting_delivery_stuck", len(expired))
        self._cleanup_agent_sessions()
        return terminals

    def _cleanup_agent_sessions(self) -> None:
        """认领并（先归档、再）物理清理一批到期的 Agent 会话 JSONL（Issue #153；
        归档见 Issue #291 L6 取证结论、``session_cleanup.py`` 模块文档「删除前先
        归档」——``/new`` 等触发点排的清理不再直接销毁原始转录）。

        没有配置可用的会话根目录，或队列适配器不支持这组方法（旧测试用的假队列）
        时整体跳过——不半途认领又做不了事，让请求继续排队给下一个真正能处理它的
        进程。单条处理失败不清 ``done_at``：下一轮的十分钟软领取窗口会重试，见迁移
        0061 头部注释。
        """

        if self._session_root is None:
            return
        claim = getattr(self._queue, "claim_session_cleanups", None)
        mark_done = getattr(self._queue, "mark_session_cleanups_done", None)
        if claim is None or mark_done is None:
            return
        try:
            pending = claim(limit=self._session_cleanup_batch_limit)
        except Exception as error:  # noqa: BLE001 - 清理认领失败不能带走任务职责
            logger.error("Agent 会话清理队列认领失败 error=%s", type(error).__name__)
            return
        if not pending:
            return
        # 根目录本身不存在与"根目录存在、这个会话确实没有文件"是两件不同的事
        # （PR #173 独立复核 P2-6）：`delete_agent_session_files` 对两者都返回
        # `0`（幂等设计，理由见该函数文档），但把这两种情况合并成同一个"标记
        # 完成"分支是错的——`.env.example` 里一个写错的 `LINGXI_WORKER_
        # SESSION_ROOT`（例如示例值 `/var/lib/lingxi/users/.claude/projects`，
        # 而镜像固定 `HOME=/tmp`）会让这一批本该被清理的会话被静默标记完成；
        # `agent_session_cleanup.agent_session_id` 是唯一索引 +
        # `ON CONFLICT DO NOTHING`，标记完成的行不会被重新排队，事后改对配置
        # 也补不回来。这里已经认领（`claimed_at`/`worker_id` 已写），因此不能
        # 简单地什么都不做就返回——十分钟软领取窗口本来就是为这类"认领了但
        # 这次处理不了"设计的重试兜底，只需要不调用 `mark_done` 即可让它到点
        # 被下一个进程重新认领。
        if not self._session_root.is_dir():
            logger.error(
                "Agent 会话清理根目录不存在，本轮跳过、不标记完成 pending=%d",
                len(pending),
            )
            return
        done_ids: list[str] = []
        for item in pending:
            try:
                # 归档先于删除（Issue #291 L6 取证结论，见 session_cleanup.py
                # 模块文档「删除前先归档」）：`/new` 等触发点排的清理如果直接
                # 物理删除，会把验收/取证现场需要的原始 JSONL 一并销毁。
                delete_agent_session_files(
                    self._session_root,
                    item.agent_session_id,
                    user_env_root=self._config.user_env_root,
                    user_id=item.user_id,
                )
            except Exception as error:  # noqa: BLE001 - 单条失败不影响本轮其余条目
                logger.error(
                    "Agent 会话 JSONL 归档/物理删除失败 reason=%s error=%s",
                    item.reason,
                    type(error).__name__,
                )
                continue
            done_ids.append(item.id)
        if done_ids:
            try:
                mark_done(ids=done_ids)
            except Exception as error:  # noqa: BLE001 - 标记失败只影响是否重试，不影响正确性
                logger.error("Agent 会话清理标记完成失败 error=%s", type(error).__name__)

    def _emit_heartbeat(self) -> None:
        if self._heartbeat is None:
            return
        try:
            self._heartbeat()
        except Exception as error:  # noqa: BLE001 - 心跳失败不能带走任务职责
            # 只记异常类型；心跳是告警输入，不能因为告警输入失败而让 worker 停止消费。
            import logging

            logging.getLogger(__name__).error(
                "worker 心跳记录失败，任务职责继续运行 error=%s", type(error).__name__
            )

    def _tick_alerts(self) -> None:
        """定期戳一下告警状态机的恢复计时与投递重试（Issue #153）。

        worker 没有 scheduler 那种独立的定时职责循环，借用队列消费循环本身每轮
        调用一次；戳的频率因此等于 ``poll_interval_seconds``（默认 2 秒），比
        scheduler 的告警职责频率高，但告警状态机本身对调用频率不敏感（去重、
        阈值判定都基于时间戳而非调用次数）。
        """

        if self._on_alert_tick is None:
            return
        try:
            self._on_alert_tick()
        except Exception as error:  # noqa: BLE001 - 告警自身失败不能带走任务职责
            import logging

            logging.getLogger(__name__).error(
                "worker 告警状态机推进失败，任务职责继续运行 error=%s", type(error).__name__
            )

    def _report_task_stuck(self, kind: str, count: int) -> None:
        if self._on_task_stuck is None or count <= 0:
            return
        try:
            self._on_task_stuck(kind, count)
        except Exception as error:  # noqa: BLE001 - 告警失败不应改变任务状态
            import logging

            logging.getLogger(__name__).error(
                "任务滞留告警记录失败，任务状态保持由队列收口 error=%s", type(error).__name__
            )

    async def _process_task(self, claimed: ClaimedTask) -> None:
        context = self._queue.task_context(
            task_id=claimed.task_id, worker_id=self._config.worker_id
        )
        if context is None:
            return

        marker = lambda: self._queue.mark_side_effect(
            task_id=claimed.task_id, worker_id=self._config.worker_id
        )
        self._append_event(claimed, event_type="started", idempotency_key_suffix="started")

        stop_event = asyncio.Event()
        if context.stop_requested or claimed.stop_requested:
            stop_event.set()
        if stop_event.is_set():
            self._finish_terminal(
                claimed,
                terminal_kind=TerminalKind.STOPPED.value,
                error_kind="stopped",
                content=self._catalog.text("worker.stopped"),
            )
            return
        started_at = self._monotonic()
        progress_count = 0
        # 提示词摘要在 try 之外初始化。字段口径（外部独立审查 2026-08-23 P2-4
        # 定稿）：「本轮**选定**并交给执行器装配的提示词版本」——文件读失败时为
        # None；读到之后装配/建连失败的回合会带着摘要落失败终态，此时它回答的是
        # "失败那一轮试图使用哪版"，不声称模型已经收到。
        system_prompt_digest: str | None = None
        # 内测轮内容级采集（Issue #251/#304 批次 3）：只在 try 主体真的构造出
        # executor 之后才非 None（见下）；`UserMcpConfigError` 等在构造 executor
        # 之前就失败的路径没有任何回合内容可采集，`_capture_content_if_enabled`
        # 据此判断是否跳过。
        executor: WorkerTurnExecutor | None = None

        # 语义化等待进度（Issue #321 方向 C）：三个信号源——模型文本输出
        # （`on_stream_event` 的 `assistant_message`）、工具调用开始
        # （`on_tool_call`，数据来自 `ToolGateway.set_tool_call_listener`，见
        # `apps/worker/turn.py::run_turn`）、30 秒兜底计时（`_monitor` 的
        # `on_stall_tick`）——共享这一份状态，只区分两类文案：问数查询工具
        # （`QUERY_MCP_TOOL_PREFIX` 前缀）与其它/生成阶段。`last_progress_write_at`
        # 以任务开始时刻为锚点：`_process_task` 开头的 "started" 事件已经让
        # Gateway 建卡并展示过一次默认文案（`start()`），下面的节流窗口紧接着它
        # 算起，不是从零开始。
        last_progress_write_at = started_at
        progress_action = PROGRESS_ACTION_PROCESSING
        query_count = 0
        # P2-4（Issue #328 opus 审查）：进度写库丢进线程池、不在调用方所在的同步
        # 回调里等待（与 `system_prompt_file` 读取同一手法，见上方 `asyncio.
        # to_thread` 用法）。`_last_progress_write_task` 把连续几次写入串成一条
        # 链——每个写入任务先等前一个跑完才真正发起自己的 `to_thread` 调用，
        # 保证同一任务的 progress 事件仍然严格按调用顺序落库（默认线程池允许
        # 多个 worker 线程并发跑，若各写各的、不定序完成，`sequence` 与
        # `query_count` 递增的先后关系就可能和真实调用顺序对不上）；
        # `background_progress_writes` 额外持有全部在途任务的引用，防止在完成
        # 前被垃圾回收——`asyncio` 只对运行中的任务保留弱引用。收口点见下方
        # `finally` 块。
        background_progress_writes: set[asyncio.Task[None]] = set()
        last_progress_write_task: asyncio.Task[None] | None = None

        def _write_progress_if_due(min_gap_seconds: float) -> None:
            """任意两次真正写库的 progress 更新间隔 ≥`min_gap_seconds` 才放行
            （节流保护，防频控；工具事件密集时合并成一次、只保留最新状态）。

            写库本身丢进线程池执行、不在这里等待结果（与 `service.py` 别处的
            `asyncio.to_thread` 同一手法）：这个函数被 SDK 流式回调
            （`on_stream_event`/`on_tool_call`，同步、由 `turn.py` 的迭代循环
            直接调用）与 30 秒兜底 tick（`on_stall_tick`，同步、由 `_monitor`
            的轮询循环直接调用）共用，三处调用方都不是 `async def`、改不成
            `await`。真实 psycopg 同步写最坏可能卡住数秒（锁等待/网络抖动），
            不丢进线程池会直接拖住事件循环、连带心跳与停止处理跟着变慢。
            fire-and-forget 是安全的：`_append_event` 内部已经把一切异常都吞成
            一条结构化日志（失败不中断任务，见其文档），调用方不需要等待结果；
            即使这次写入排在终态写入之后才真正落库，`append_delivery_event` 的
            所有权校验（任务此时已不在 `running`）也会让它安全地什么都不做，
            不产生游离事件。真正的收口点在 `_process_task` 的 `finally` 块：
            那里会等齐本回合排出的全部后台写入，保证终态判定之前它们已落库、
            不留下未完成的写入线程。

            **节流状态本身（`last_progress_write_at`/`progress_count`/
            `last_progress_write_task` 这三个 `nonlocal`）的读取与更新全程不
            `await`**：这个函数从进入到把新写入任务排出去为止是一段连续的
            同步代码，中途没有任何让出点。三个调用方（`on_stream_event`/
            `on_tool_call`/`on_stall_tick`）虽然可能来自不同的异步任务
            （`turn.py` 的流式循环 vs `_monitor`），但事件循环单线程运行、
            协作式调度只在 `await` 处才可能切换——没有 `await` 就没有交叉，
            因此这里不需要锁也不会有竞态。这条前提只对**这个函数自身**成立；
            函数末尾创建的写入任务本身是异步的，不在这条前提覆盖范围内。
            """

            nonlocal last_progress_write_at, progress_count, last_progress_write_task
            now = self._monotonic()
            if now - last_progress_write_at < min_gap_seconds:
                return
            last_progress_write_at = now
            progress_count += 1
            content: str | None = None
            if progress_action == PROGRESS_ACTION_QUERYING:
                content = encode_progress_action(PROGRESS_ACTION_QUERYING, query_count=query_count)
            elif progress_action == PROGRESS_ACTION_COMPOSING:
                content = encode_progress_action(PROGRESS_ACTION_COMPOSING)
            idempotency_key_suffix = f"progress:{progress_count}"
            elapsed_seconds = int(max(0.0, now - started_at))
            previous_write_task = last_progress_write_task

            async def _write_after_previous() -> None:
                if previous_write_task is not None:
                    # 只是排队等前一次写完，不关心它是否成功——失败已经在
                    # `_append_event` 内部记过日志，这里再等一次异常没有意义。
                    await asyncio.gather(previous_write_task, return_exceptions=True)
                await asyncio.to_thread(
                    self._append_event,
                    claimed,
                    event_type="progress",
                    idempotency_key_suffix=idempotency_key_suffix,
                    elapsed_seconds=elapsed_seconds,
                    content=content,
                )

            # 不在这里用 `add_done_callback` 提前从 `background_progress_writes`
            # 摘除已完成的任务：那样会让它的异常（即使实际上从不会发生，见上）
            # 在被 `finally` 块的 `gather` 取走之前就被摘掉，触发 asyncio 的
            # "exception was never retrieved" 噪音日志。这个集合按回合生命周期
            # 存在，一次回合内的写入次数有限，留着已完成的任务不构成内存问题。
            write_task = asyncio.create_task(_write_after_previous())
            last_progress_write_task = write_task
            background_progress_writes.add(write_task)

        def on_stream_event(event: Mapping[str, Any]) -> None:
            nonlocal progress_action
            if event.get("kind") == "assistant_message":
                # 模型在两次工具调用之间/收尾前输出的正文——归入"生成阶段"文案。
                progress_action = PROGRESS_ACTION_COMPOSING
                _write_progress_if_due(_PROGRESS_MIN_UPDATE_INTERVAL_SECONDS)

        def on_tool_call(tool_name: str) -> None:
            nonlocal progress_action, query_count
            # 只区分两类文案（产品负责人裁定）：问数查询工具单独计数、给出"第 N
            # 次"文案；其它任何工具（含被拒绝的越界调用）一律并入"生成阶段"——
            # 不回显工具名、参数或任何查询内容。
            if isinstance(tool_name, str) and tool_name.startswith(QUERY_MCP_TOOL_PREFIX):
                query_count += 1
                progress_action = PROGRESS_ACTION_QUERYING
            else:
                progress_action = PROGRESS_ACTION_COMPOSING
            _write_progress_if_due(_PROGRESS_MIN_UPDATE_INTERVAL_SECONDS)

        def on_stall_tick() -> None:
            # `_monitor` 每个 `stop_poll_interval_seconds` 调用一次：距上次真正
            # 写库的更新 ≥30 秒时强制推一次纯用时更新（沿用当前的 progress_
            # action/query_count，即使没有任何新信号）。
            _write_progress_if_due(_PROGRESS_FALLBACK_SECONDS)

        monitor = asyncio.create_task(
            self._monitor(claimed.task_id, stop_event, on_stall_tick=on_stall_tick)
        )

        try:
            # Epic D 闸⑥红线：每个用户的问数必须用他自己的那份 MCP 配置，绝不
            # 回退到全进程共用的 self._config.mcp_servers——回退意味着用户 A
            # 的问数用了一份不属于他的令牌去查数，是越权返回数据。这里**结构性
            # 地没有回退分支**：读取失败（UserMcpConfigError）在下面单独一支
            # except 里收口成失败报告，从不构造 executor、从不调用 run_turn，
            # self._config（携带全进程共用配置）不会被传给 _executor_factory。
            # 唯一能让任务真正执行的路径，是读到了这个用户自己的配置并用它
            # 覆盖出一份 task_config。
            user_mcp_servers = load_user_mcp_servers(
                root=self._config.user_env_root or "", user_id=claimed.user_id
            )
            # 默认提示词**每任务现读**（2026-08-23，产品负责人裁定提示词外置）：
            # 编辑挂载卷上的文件后下一条消息即生效。读不到就本任务降级为无提示词
            # 执行并留结构化告警——config 层已保证 file 与进程级 system_prompt
            # 互斥，这里的覆盖不会吃掉任何别处配置的值。
            task_system_prompt = self._config.system_prompt
            if self._config.system_prompt_file:
                # to_thread：读取本身已有界（≤64KiB+1），但慢挂载/存储抖动下的
                # 一次 open/read 仍可能停顿；放线程池里跑，不占事件循环（心跳与
                # 停止处理都在循环上，codex 二轮复验指出）。
                task_system_prompt, system_prompt_digest, prompt_degraded = (
                    await asyncio.to_thread(
                        _load_task_system_prompt, self._config.system_prompt_file
                    )
                )
                if prompt_degraded is not None:
                    logger.warning(
                        "worker.system_prompt.degraded reason=%s task_id=%s（本任务以无提示词执行）",
                        prompt_degraded,
                        claimed.task_id,
                    )
            # ``replace`` 会重跑 ``__post_init__``：task_config 携带的是**已解析**
            # 的提示词，必须同时清掉文件指针，否则「file 与 prompt 互斥」的不变量
            # 会把每一个成功读到提示词的任务当场炸成 session_failed（首版实现
            # 实测踩中，见 tests 的双任务用例）。
            task_config = replace(
                self._config,
                mcp_servers=user_mcp_servers,
                system_prompt=task_system_prompt,
                system_prompt_file=None,
            )
            executor = self._executor_factory(task_config, marker)
            report = await executor.run_turn(
                context.prompt,
                resume_session_id=(
                    context.agent_session_id if context.resumed_session else None
                ),
                stop_event=stop_event,
                on_stream_event=on_stream_event,
                on_tool_call=on_tool_call,
                external_texts=self._config.external_texts,
            )
        except UserMcpConfigError as error:
            # error.code 是本模块自定的安全码（不含路径 / 内容 / 令牌），供运维
            # 诊断具体失败原因（例如用户还没走完首次开通、配置文件形状不对）。
            report = {
                "turn": {"closed": False, "final_text": "", "session_id": None},
                "failure": {
                    "code": "user_mcp_config_unavailable",
                    "message": f"user_mcp_config:{error.code}",
                },
            }
        except Exception as error:  # noqa: BLE001 - worker 绝不留下 running
            report = {
                "turn": {"closed": False, "final_text": "", "session_id": None},
                "failure": {"code": "session_failed", "message": type(error).__name__},
            }
        finally:
            monitor.cancel()
            try:
                await monitor
            except asyncio.CancelledError:
                pass
            # 等齐本回合排出的全部后台 progress 写入（P2-4）：`run_turn()` 与
            # `_monitor` 都已经收尾，期间同步回调可能排出的写入任务此刻已经
            # 全部创建完毕，只是不保证已经跑完。终态判定与 `_finish_terminal`
            # 依赖"这一轮的进度写入已经落库或明确失败"这个前提（尤其是真库
            # 测试按事件计数断言），因此在这里一次性等完，不在每次写入时单独
            # 等——`return_exceptions=True` 只是双保险：`_append_event` 内部已
            # 经吞掉了一切异常，这里不应该、也不允许再被它带走终态收口。
            if background_progress_writes:
                await asyncio.gather(*background_progress_writes, return_exceptions=True)

        # 这里刻意**不再**回读队列侧的 stop 标志（此前是
        # `stop_requested = stop_event.is_set() or self._queue.stop_requested(...)`）：
        # 终态只由这一轮实际发生的事实决定，见下方 `stop_is_the_outcome` 的说明。
        # 留着它就是留一个没人用的每回合额外查询和一个会诱使后来者再次「用 stop
        # 覆盖终态」的现成变量。停止在途回合仍由 `_monitor` 置位 `stop_event`
        # 驱动执行层中断，那条路径没有改动。
        turn = report.get("turn") or {}
        failure = report.get("failure") or {}
        failure_code = failure.get("code") if isinstance(failure, Mapping) else None
        final_text = turn.get("final_text") if isinstance(turn, Mapping) else ""
        final_text = final_text if isinstance(final_text, str) else ""
        elapsed_seconds = int(max(0.0, self._monotonic() - started_at))

        output_safety = turn.get("output_safety") if isinstance(turn, Mapping) else None
        withheld = bool(isinstance(output_safety, Mapping) and output_safety.get("withheld"))
        # withheld 只对"本来会成功交付内容"的回合有意义：一个超时/失败/未收口的
        # 回合无论正文如何都不会交付 final_text，其终态必须保留真实失败原因。
        # 此前 withheld 分支排在失败判定之前（独立审核 F1）：失败回合的残余正文
        # 一旦触发出口安全（真实泄露片段或受控 canary 注入都可能），真超时就会被
        # 改写成 redacted_withheld——运维丢失真实失败终态，验收拿到假阳性证据。
        deliverable = bool(turn.get("closed")) and not failure
        denied_count, denied_tool_names = _denied_tool_summary(report)
        tool_result_count = _tool_result_count(report)
        # 通报补数落库值（Issue #303/#304 批次 4，迁移 0070）：与上面
        # denied_count（低敏结构化日志用，取不到时如实记 0）故意分开计算——
        # 这两个是"取不到就留 NULL、不编造"，服务的是 core/daily_report.py 的
        # 统计聚合，两套值的"取不到"语义不同，不能共用同一次求值结果。
        guard_denied_count_for_report = _report_guard_denied_count(report)
        token_usage_for_report = _report_token_usage(report)
        # P0 护栏（Issue #291 L6 取证结论，见 `_protocol_breakdown_reasons`）：
        # `closed=True` 且没有 `failure` 只说明"SDK 认为这一轮正常收口"，不说明
        # "这段正文是一个可以交付给用户的答案"。模型把工具调用协议细节写成正文
        # 散文时，`output_safety` 的净化层会正确遮蔽敏感片段但仍然把（遮蔽后的）
        # 残余正文当成有效业务内容放行——`withheld` 因此不会置位，旧实现就此把
        # 一段协议残骸当成「查询完成」交付。这里单独判定，不依赖 `withheld`。
        protocol_breakdown_reasons = _protocol_breakdown_reasons(output_safety)

        # 终态优先级（Issue #195）：真中断 → 其他失败 → withheld → 成功。
        # 此前第一分支是 `stop_requested or failure_code == "interrupted"`，
        # 让一次**并发到达**的 stop 压过所有已经发生的事实：
        #   1. 回合已因 `turn_timeout`/`drain_timeout`/`session_failed` 失败，
        #      终态被改写成 `stopped`，残余正文还会经 `worker.stopped_result`
        #      交付——真实失败原因丢失，与本文件「失败终态保留真实失败原因」
        #      的既有约定（#186 F1）直接冲突；
        #   2. 回合已经 `closed=True` 出结果，晚到的 stop 把成功降级成
        #      `stopped`，只有成功分支才写的 `session_id` 一并丢失，用户拿不到
        #      已经产出的结果、也无法在会话内追问——踩「重启与重试不得造成
        #      用户结果丢失」这条红线。
        # 因此 `stopped` 只认执行层给出的**真中断**：`interrupted` 是
        # `adapters/claude_agent_session.py` 观测到本地 `stop_event` 已置位、
        # 调用过 `client.interrupt()` 之后才抛出的 `AgentSessionInterrupted`
        # （`turn.py` 映射），它是这条链路上唯一带因果的「这一轮真的被 stop
        # 打断了」信号。队列侧的 `stop_requested` 只说明「某一刻有人请求过停止」，
        # 不说明这一轮的结果由它决定，因此**不参与**终态选择。
        #
        # 尤其不能用「没有失败码」反推成 stop（codex 一级独立审查 P1-1）：
        # `closed=False` 本身就是失败事实，`failure_code is None` 只是没人给它
        # 起名字（屏障失效 `gate_bypassed`、failure 映射缺 `code` 都是这种形状）。
        # 也不能把 `cancelled` 当成 stop 的别名（同审查 P1-2）：它来自
        # `turn.py/_sdk_termination_failure` 对 SDK 自报的
        # `aborted_streaming`/`aborted_tools`，与本地 `stop_event` 之间没有任何
        # 因果绑定，SDK 完全可能在没人 stop 的时候自行 abort——认它就等于让一次
        # 晚到的 stop 掩盖真实的 SDK 终止失败。
        #
        # 注意开工前就带着 stop_requested 的任务在 `_process_task` 开头就已收口
        # 成 `stopped`，根本不会走到这里；这里处理的只有「执行途中 stop 与回合
        # 终点赛跑」这一种情况。
        stop_is_the_outcome = failure_code == "interrupted"

        if stop_is_the_outcome:
            # `result=final_text` 是模型生成的终态正文（Issue #322），出口校验
            # 只保留协议泄漏检查——理由见 `content._validate_user_visible_text`。
            content = (
                self._catalog.text(
                    "worker.stopped_result", result=final_text, contains_model_text=True
                )
                if final_text
                else self._catalog.text("worker.stopped")
            )
            self._finish_terminal(
                claimed,
                terminal_kind=TerminalKind.STOPPED.value,
                error_kind="stopped",
                content=content,
                elapsed_seconds=elapsed_seconds,
                failure_code=failure_code,
                output_safety=output_safety,
                denied_count=denied_count,
                denied_tool_names=denied_tool_names,
                tool_result_count=tool_result_count,
                system_prompt_digest=system_prompt_digest,
                guard_denied_count=guard_denied_count_for_report,
                token_usage=token_usage_for_report,
            )
        elif not deliverable:
            error_kind, content = self._failure_content(failure_code)
            terminal_kind = (
                TerminalKind.TIMEOUT.value if failure_code == "turn_timeout" else TerminalKind.FAILED.value
            )
            self._finish_terminal(
                claimed,
                terminal_kind=terminal_kind,
                error_kind=error_kind,
                content=content,
                elapsed_seconds=elapsed_seconds,
                failure_code=failure_code,
                output_safety=output_safety,
                denied_count=denied_count,
                denied_tool_names=denied_tool_names,
                tool_result_count=tool_result_count,
                system_prompt_digest=system_prompt_digest,
                guard_denied_count=guard_denied_count_for_report,
                token_usage=token_usage_for_report,
            )
        elif protocol_breakdown_reasons:
            # P0 护栏（Issue #291 L6 取证结论）：模型正文里出现内部工具名或过程
            # 标记，永远是模型把工具调用协议写成了正文散文，不是一个可以交付给
            # 用户的答案——不得判 success。复用既有失败终态形状（`TerminalKind.
            # FAILED`）与既有用户文案路径（`_failure_content` → `worker.failed`
            # 通用失败文案）：#280 的追溯号机制只覆盖 `inbound_event`（首次开通
            # 事件），`ClaimedTask`/`SessionCleanupTask` 都不携带能回查那张表的
            # `trace_id`，在 worker 这条队列消费链路上不可达，因此不额外编造一个
            # 走不通的追溯号占位符——用户看到的仍是通用失败文案，真实原因只进
            # `failure_code`/审计日志，供运维用 `worker.task.terminal` 查询。
            error_kind, content = self._failure_content(_MODEL_PROTOCOL_BREAKDOWN_FAILURE_CODE)
            self._finish_terminal(
                claimed,
                terminal_kind=TerminalKind.FAILED.value,
                error_kind=error_kind,
                content=content,
                elapsed_seconds=elapsed_seconds,
                failure_code=_MODEL_PROTOCOL_BREAKDOWN_FAILURE_CODE,
                output_safety=output_safety,
                denied_count=denied_count,
                denied_tool_names=denied_tool_names,
                tool_result_count=tool_result_count,
                system_prompt_digest=system_prompt_digest,
                guard_denied_count=guard_denied_count_for_report,
                token_usage=token_usage_for_report,
            )
        elif withheld:
            # #141/#149：整段正文因安全策略被拒发，即使 closed=True 也不得记
            # succeeded——用户没有拿到结果，必须走独立、可查询的 redacted_withheld
            # 终态（status 沿用既有取值域，用 error_kind 承载可查询原因）。
            self._finish_terminal(
                claimed,
                terminal_kind=TerminalKind.REDACTED_WITHHELD.value,
                error_kind="redacted_withheld",
                content=self._catalog.text("worker.redacted_withheld"),
                elapsed_seconds=elapsed_seconds,
                failure_code=failure_code,
                output_safety=output_safety,
                denied_count=denied_count,
                denied_tool_names=denied_tool_names,
                tool_result_count=tool_result_count,
                system_prompt_digest=system_prompt_digest,
                guard_denied_count=guard_denied_count_for_report,
                token_usage=token_usage_for_report,
            )
        else:
            # 走到这里必然 `deliverable and not withheld and not protocol_
            # breakdown_reasons`：回合已收口、有结果、没有失败、正文没有触发
            # P0 护栏。即使 stop 晚到，这份已经产出的结果照常交付，`session_id`
            # 照常持久化（Issue #195）。
            self._finish_terminal(
                claimed,
                terminal_kind=TerminalKind.SUCCESS.value,
                error_kind=None,
                content=RenderedContent(key="worker.result", version=self._catalog.version, text=final_text),
                elapsed_seconds=elapsed_seconds,
                session_id=turn.get("session_id") if isinstance(turn, Mapping) else None,
                failure_code=failure_code,
                output_safety=output_safety,
                denied_count=denied_count,
                denied_tool_names=denied_tool_names,
                tool_result_count=tool_result_count,
                system_prompt_digest=system_prompt_digest,
                guard_denied_count=guard_denied_count_for_report,
                token_usage=token_usage_for_report,
                # 文档投递请求（Issue #341 S-ES-3 报告契约）：只在这一轮真正判定
                # 为业务成功的分支才转发——其余分支（stop/failure/protocol
                # breakdown/withheld）即使 report["document_request"] 恰好非空
                # （理论上不会：turn.py 只在 failure is None 时才填充这个字段，
                # 但这几个分支各自有自己判成非成功的独立理由，例如 withheld 是
                # 安全策略事后拒发正文），也绝不建文档投递请求——用户既然没有
                # 拿到问答本身的结果，就不该收到一份可能同样有问题的文档。
                document_request=_report_document_request(report),
            )

        # 内测轮内容级采集（Issue #251/#304 批次 3）：无论上面走了哪条终态分支
        # 都尝试采集——失败/超时回合的问题原文与已尝试的工具调用同样是"以日志
        # 分析缺陷"要看的信号，不只是成功回合才有采集价值。必须排在全部终态
        # 分支之后：终态收口（用户结果）优先于采集（旁路观测），即使这里失败
        # 也不能影响上面已经写好的终态。
        self._capture_content_if_enabled(claimed, executor=executor, question=context.prompt)

    def _capture_content_if_enabled(
        self, claimed: ClaimedTask, *, executor: WorkerTurnExecutor | None, question: str
    ) -> None:
        """内测轮内容级采集的写入点（Issue #251/#304 批次 3）。

        失败必须整体降级为一条结构化审计日志、不得向上抛——采集是旁路观测，
        不是任务能否完成的一部分（结构约束「采集失败不影响任务主流程」，见
        docs/技术设计/数据库设计.md 与 apps/worker/config.py 的模块文档）。

        ``executor`` 为 ``None``（进入 try 主体前就失败——例如
        ``UserMcpConfigError`` 从未走到构造 executor 那一步，或任务在开头就
        因带着 ``stop_requested`` 提前收口）时没有任何可采集的回合内容，直接
        跳过；``self._content_capture_writer`` 为 ``None``（未装配写入方，见
        ``apps/worker/cli.py`` 只在开关开启时才构造）同样跳过——两个判断分别
        兜住"这次没有回合内容"与"这次没有落库出口"，都不是错误，不记日志。

        成功构造出记录后还会调用 :meth:`_check_year_grounding_suspect`（Issue
        #326 批次 5 卡 E，年份接地护栏第二层检测），复用同一个 ``record`` 里
        已经解析好的问句与工具调用，不重新解析一遍。
        """

        if executor is None or self._content_capture_writer is None:
            return
        record: ContentCaptureRecord | None = None
        try:
            record = executor.build_content_capture_record(
                task_id=claimed.task_id,
                worker_id=self._config.worker_id,
                question=question,
            )
            if record is not None:
                self._content_capture_writer(record)
        except Exception as error:  # noqa: BLE001 - 采集失败降级为日志，不丢用户结果
            logger.error(
                "内测轮内容级采集写入失败，任务结果不受影响 task_id=%s error=%s",
                claimed.task_id,
                type(error).__name__,
            )
        # 年份接地护栏第二层（Issue #326）：独立于上面采集写入的 try/except——
        # 检测本身的缺陷不能连带影响"记录有没有落库"的判断，也不能与写库失败
        # 共用同一条日志、分不清是采集坏了还是检测坏了。写库失败但记录已经在
        # 内存里构造出来时（`record is not None`）仍然照常检测：本护栏只依赖
        # 内存中的问句与工具调用，不依赖这次落库是否成功。
        if record is not None:
            self._check_year_grounding_suspect(record)

    def _check_year_grounding_suspect(self, record: ContentCaptureRecord) -> None:
        """年份接地护栏第二层：结构性检测 + 告警（Issue #326，批次 5 卡 E）。

        只做检测与告警，**不拦截、不改答案投递路径**——调用方
        ``_capture_content_if_enabled`` 已经在全部终态分支收口之后才调用本方法
        （见该方法末尾的调用点），本方法自身再包一层独立 try/except，双重保证
        检测代码的任何异常都不可能影响任务终态或已经完成的内容采集写入。

        判定逻辑（相对时间词表、年份提取、三条件与）全部在 ``core/
        year_grounding_guard.py``——本方法只负责"取当前年份、调用纯逻辑判定、
        把结果交给装配层注入的告警出口"这三步组装，不重复任何判定规则。
        """

        if self._on_year_grounding_suspect is None:
            return
        try:
            suspect = detect_year_grounding_suspect(
                task_id=record.task_id,
                question=record.question_content,
                tool_calls=record.tool_calls,
                current_year=datetime.now().year,
            )
            if suspect is not None:
                self._on_year_grounding_suspect(suspect.to_alert_fields())
        except Exception as error:  # noqa: BLE001 - 检测是旁路，异常不得影响任务终态
            logger.error(
                "年份接地护栏检测异常，任务结果不受影响 task_id=%s error=%s",
                record.task_id,
                type(error).__name__,
            )

    def _append_event(
        self,
        claimed: ClaimedTask,
        *,
        event_type: str,
        idempotency_key_suffix: str,
        elapsed_seconds: int | None = None,
        content: str | None = None,
    ) -> None:
        """写入非终态事件；失败不中断任务执行——它是可恢复的运行信号，不是结果。"""

        try:
            # 写入前自查（Issue #328 opus 审查 R1）：在这里、在这次调用真正携带
            # 的 `content` 上再用一次 `CONTENT_BEARING_EVENT_TYPES`/
            # `PROGRESS_CONTENT_MAX_LENGTH` 自查——不依赖 `self._queue` 具体实现
            # 是否也做了同一层校验（真实 `PostgresTaskQueue` 做了，测试用的
            # fake 队列未必做）。命中问题时抛出的 `ValueError` 与真实数据库写入
            # 失败走同一条"失败不中断任务"的收口路径，不区分对待。
            assert_content_allowed(DeliveryEventType(event_type), content)
            self._queue.append_delivery_event(
                task_id=claimed.task_id,
                worker_id=self._config.worker_id,
                event_type=event_type,
                idempotency_key=f"{claimed.task_id}:a{claimed.attempts}:{idempotency_key_suffix}",
                elapsed_seconds=elapsed_seconds,
                content=content,
            )
        except Exception as error:  # noqa: BLE001 - 事件是可恢复的运行信号，不能带走任务
            import logging

            logging.getLogger(__name__).error(
                "worker.delivery_event_write_failed event_type=%s error=%s",
                event_type,
                type(error).__name__,
            )

    def _finish_terminal(
        self,
        claimed: ClaimedTask,
        *,
        terminal_kind: str,
        error_kind: str | None,
        content: RenderedContent,
        elapsed_seconds: int = 0,
        session_id: str | None = None,
        failure_code: object = None,
        output_safety: Mapping[str, Any] | None = None,
        denied_count: int = 0,
        denied_tool_names: tuple[str, ...] = (),
        tool_result_count: int = 0,
        system_prompt_digest: str | None = None,
        guard_denied_count: int | None = None,
        token_usage: Mapping[str, int] | None = None,
        document_request: Mapping[str, Any] | None = None,
    ) -> None:
        """写终态事件、把任务转入 ``awaiting_delivery``（Issue #151 状态合同第 2
        条）。话题继续占用直到投递解析，因此新建立的 ``session_id``（只在业务
        成功时非空）随终态事件一起持久化，留到确认送达时才写回
        ``conversation.agent_session_id``——同一话题在此期间不会有第二个任务插进
        来读它，延后写入是安全的（见 ``core.delivery.ports`` 与
        ``PostgresTaskQueue.confirm_delivery`` 的取舍说明）。

        本方法是 ``_process_task`` 里所有终态写入的唯一收口点，因此低敏审计日志
        （Issue #90 评论 5306860255；``denied_count``/``denied_tool_names`` 见
        Issue #291 独立审查；``tool_result_count`` 见 Issue #291 L6 取证结论）
        放在这里记一次，覆盖 stop/withheld/success/failure 全部分支，不必在每个
        分支各写一遍。``guard_denied_count``/``token_usage``（Issue #303/#304
        批次 4，迁移 ``0070``）同样在这里落库一次——两者是"取不到就留 NULL、
        不编造"的通报统计用值，与上面供日志用的 ``denied_count``（取不到时如实
        记 0）故意不是同一份计算结果，见调用方 ``_report_guard_denied_count``/
        ``_report_token_usage`` 的文档。

        ``document_request``（Issue #341 S-ES-3，迁移 ``0074``）：仅当调用方
        （``_process_task`` 真正成功分支）判定"业务成功且报告契约携带非空
        document_request"时才非 ``None``——原样透传 ``write_terminal_event``，
        由它在写终态的同一事务里插入文档投递请求行；其余分支恒 ``None``。
        """

        self._log_terminal_outcome(
            task_id=claimed.task_id,
            failure_code=failure_code,
            error_kind=error_kind,
            terminal_kind=terminal_kind,
            output_safety=output_safety,
            denied_count=denied_count,
            denied_tool_names=denied_tool_names,
            tool_result_count=tool_result_count,
            system_prompt_digest=system_prompt_digest,
        )
        self._queue.write_terminal_event(
            task_id=claimed.task_id,
            worker_id=self._config.worker_id,
            terminal_kind=terminal_kind,
            error_kind=error_kind,
            content=content.text,
            elapsed_seconds=elapsed_seconds,
            agent_session_id=session_id,
            token_usage=token_usage,
            guard_denied_count=guard_denied_count,
            document_request=document_request,
        )

    def _log_terminal_outcome(
        self,
        *,
        task_id: str,
        failure_code: object,
        error_kind: str | None,
        terminal_kind: str,
        output_safety: Mapping[str, Any] | None,
        denied_count: int = 0,
        denied_tool_names: tuple[str, ...] = (),
        tool_result_count: int = 0,
        system_prompt_digest: str | None = None,
    ) -> None:
        """queue 收口低敏结构化审计事件（Issue #90 评论 5306860255）：queue 链路
        此前失败码与安全命中规则完全不可回读，r13 只能靠猜直接原因。这里只记
        分类性的失败码、落库 ``error_kind``、``terminal_kind``、安全判定的
        布尔/原因码、本回合被 ``ToolPolicy`` 拒绝的调用计数与工具名，以及这一轮
        真实的工具调用次数——**严禁**记录正文内容、用户 open_id、prompt、模型
        输出片段或工具入参正文。

        ``denied_count``/``denied_tool_names`` 是 Issue #291 独立审查补的一项：
        ``tool_policy.py`` 的拒绝文案对用户承诺"这是系统侧的临时限制、问题
        已经被记录"，但此前 queue 链路从未把 ``report["audit"]["denied_count"]``
        （早就算出来了，见 ``report.py``）写进任何运维可见的地方——白名单配错
        导致的拒绝只能像 #291 真实事故那样，靠用户反馈才会被发现。

        ``tool_result_count`` 是 Issue #291 L6 取证结论补的一项（见
        ``_tool_result_count`` 的完整说明）：2026-08-22 那次取证——模型把工具
        调用协议写成正文散文、被净化层遮蔽后仍当成成功交付——运维定位"这一轮
        到底有没有真的调用过工具"花了 40 分钟，因为这个字段此前同样从未离开
        进程。

        独立复核 P1：这条事件不能直接调用 stdlib ``logging``——``WorkerService``
        不知道自己会被哪个进程入口装配，而真实队列 worker 的 ``apps/worker/
        cli.py`` 刻意从不调用 ``logging.basicConfig()``（未配置 handler 时默认
        阈值 ``WARNING`` 会把 ``logging.info(...)`` 悄悄吞掉，运维在真实容器
        stderr 里永远看不到）。因此改为调用装配层注入的 ``on_terminal_outcome``
        回调，由 ``cli.py`` 接到本文件既有的结构化 stderr 出口（``worker.task.
        terminal``，带 ``trace_id``；``denied_count > 0`` 时该出口把这条事件
        提到 ``warning`` 级别，见 ``cli.py`` 的 ``_terminal_outcome_sink``）。
        没有装配方（``None``，例如白盒测试与旧调用方）时整体跳过，不假装写了
        一条实际不存在的日志。
        """

        if self._on_terminal_outcome is None:
            return

        blocked = bool(isinstance(output_safety, Mapping) and output_safety.get("blocked"))
        withheld = bool(isinstance(output_safety, Mapping) and output_safety.get("withheld"))
        reasons: tuple[str, ...] = ()
        if isinstance(output_safety, Mapping):
            raw_reasons = output_safety.get("reasons")
            if isinstance(raw_reasons, (list, tuple)):
                reasons = tuple(str(reason) for reason in raw_reasons)

        # P3-2：失败码与每个原因码入日志前截到长度上界，避免未来某次改动不小心
        # 把自由文本塞进这两个字段时，审计日志变成新的正文泄漏面。被拒工具名
        # 同一惯例：内置工具名/已知 MCP 工具名很短，真正会撑长的是模型臆造的
        # 畸形名字或凭据形态的字符串，同样不能不设上界。
        truncated = False
        capped_failure_code: str | None = None
        if failure_code is not None:
            capped_failure_code, code_truncated = _cap_log_token(str(failure_code))
            truncated = truncated or code_truncated
        capped_reasons: list[str] = []
        for reason in reasons:
            capped_reason, reason_truncated = _cap_log_token(reason)
            capped_reasons.append(capped_reason)
            truncated = truncated or reason_truncated
        capped_denied_tool_names: list[str] = []
        for name in denied_tool_names:
            capped_name, name_truncated = _cap_log_token(name)
            capped_denied_tool_names.append(capped_name)
            truncated = truncated or name_truncated

        fields = {
            "task_id": task_id,
            "failure_code": capped_failure_code,
            "error_kind": error_kind,
            "terminal_kind": terminal_kind,
            "output_safety_blocked": blocked,
            "output_safety_withheld": withheld,
            "output_safety_reasons": tuple(capped_reasons),
            "denied_count": denied_count,
            "denied_tool_names": tuple(capped_denied_tool_names),
            "tool_result_count": tool_result_count,
            # 「这一轮**选定**的默认提示词版本」的唯一追溯依据（sha256 前 12 位；
            # 未配置提示词文件或本轮降级时为 None；口径见 _process_task 的初始化
            # 注释——记录"选定并交给执行器装配的版本"，不声称模型已收到）。摘要
            # 是固定形态短标识，不过 _cap_log_token——它不可能携带自由文本。
            "system_prompt_digest": system_prompt_digest,
            "truncated": truncated,
        }
        try:
            self._on_terminal_outcome(fields)
        except Exception as error:  # noqa: BLE001 - 观测失败不能带走任务职责，参照 _append_event
            logger.error(
                "终态收口审计事件回调失败，任务收口继续 error=%s", type(error).__name__
            )

    async def _monitor(
        self,
        task_id: str,
        stop_event: asyncio.Event,
        *,
        on_stall_tick: Callable[[], None] | None = None,
    ) -> None:
        last_heartbeat = self._monotonic()
        while True:
            # 活性文件必须在这条循环里戳，不能只靠 `process_once()` 开头那一次
            # （PR #173 独立复核 P1-5）：`process_once()` 会 `await
            # asyncio.gather(...)` 等完整批任务才返回，一个正常但较长的回合
            # （`turn_timeout_seconds` 默认 600s）足以让活性文件年龄超过 worker
            # 角色的健康检查阈值（默认 60s），把"完全正常、只是在忙"的容器打成
            # unhealthy。`_monitor` 本来就按 `stop_poll_interval_seconds`
            # （默认 1s）在跳、贯穿整个在途任务的生命周期，是"进程仍在做正确的
            # 事"这个信号真正应该来源的地方；每跳一次戳一次是本地文件写入，
            # 成本可忽略。真实证据：`tests/test_worker_queue_consumer.py` 的
            # ``test_liveness_stays_fresh_through_a_long_in_flight_turn`` ——
            # 删掉这一行后该用例会变红。
            self._emit_heartbeat()
            # SIGTERM 与用户 `/stop` 对在途回合而言是同一件事——都要求 Agent SDK
            # 尽快中断当前回合（Issue #153 完成标准第 3 条："通知在途回合停止"）。
            # `self._global_stop` 只在 `run()` 收到停止信号时被置位，process_once()
            # 的白盒调用方与既有测试不传它，行为不变。
            if self._queue.stop_requested(
                task_id=task_id, worker_id=self._config.worker_id
            ) or (self._global_stop is not None and self._global_stop.is_set()):
                stop_event.set()
            now = self._monotonic()
            if now - last_heartbeat >= self._config.heartbeat_interval_seconds:
                if not self._queue.heartbeat(
                    task_id=task_id, worker_id=self._config.worker_id
                ):
                    return
                last_heartbeat = now
            if on_stall_tick is not None:
                try:
                    on_stall_tick()
                except Exception as error:  # noqa: BLE001 - 兜底刷新失败不能带走任务职责
                    logger.error(
                        "语义化进度兜底刷新失败，任务职责继续运行 error=%s",
                        type(error).__name__,
                    )
            await self._sleep(self._config.stop_poll_interval_seconds)

    def _failure_content(self, code: object) -> tuple[str, RenderedContent]:
        if code == "context_too_long":
            return "context_too_long", self._catalog.text("worker.context_too_long")
        if code == "turn_timeout":
            return "running_timeout", self._catalog.text("worker.running_timeout")
        if code == "side_effect_uncertain":
            return "side_effect_uncertain", self._catalog.text("worker.side_effect_uncertain")
        if code == "max_turns_exceeded":
            # Issue #90 评论 5306860255：turn 模式（apps/worker/turn.py 的
            # `_sdk_termination_failure`）早已把撞满 Agent 轮数上限分类为
            # `max_turns_exceeded`，但 queue 收口此前落进这里的默认分支，
            # 被压平成通用 `session_failed` 文案——用户看到的是「请稍后重试」，
            # 而重试对"问题本身步骤太多"这种失败原因没有意义。这里给它一个
            # 独立、可查询的 error_kind 和产品负责人定稿的专属文案。
            return "max_turns_exceeded", self._catalog.text("worker.max_turns")
        if code == _MODEL_PROTOCOL_BREAKDOWN_FAILURE_CODE:
            # Issue #291 L6 取证结论：不新增专属用户文案——「模型把工具调用协议
            # 写成了正文」是运维需要知道的事实，不是用户需要（或应该）知道的
            # 过程细节；把它说给用户听本身就是又一次过程泄漏。复用通用失败文案，
            # 专属性只保留在 `failure_code`（审计/日志可查）。
            return _MODEL_PROTOCOL_BREAKDOWN_FAILURE_CODE, self._catalog.text("worker.failed")
        if code == "result_too_large":
            # 2026-08-23 真实故障：未加窄过滤的指标查询回执超过 SDK 读流缓冲上限
            # （分类在 apps/worker/turn.py）。与 max_turns_exceeded 同一姿态——
            # 「请稍后重试」对确定性失败是误导，专属文案给出可行动的建议。
            return "result_too_large", self._catalog.text("worker.result_too_large")
        return "session_failed", self._catalog.text("worker.failed")

    async def run(self, *, stop_event: asyncio.Event | None = None) -> None:
        stop = stop_event or asyncio.Event()
        # 见 `_monitor`：在途任务据此把"进程正在停机"与"用户发了 /stop"同等看待。
        self._global_stop = stop
        listener_context = self._listener_factory() if self._listener_factory else None
        if listener_context is None:
            while not stop.is_set():
                did_work = await self.process_once()
                if not did_work:
                    await self._sleep(self._config.poll_interval_seconds)
            return

        with listener_context as listener:
            while not stop.is_set():
                did_work = await self.process_once()
                if did_work:
                    continue
                await asyncio.to_thread(
                    listener.wait,
                    timeout_seconds=self._config.poll_interval_seconds,
                )


WorkerQueueConsumer = WorkerService
