"""从一次回合报告（``apps/worker/report.py::build_report`` 的产物）里取出各类
供落库/供低敏审计日志用的字段：纯函数，不碰网络、不碰数据库、不持有任何状态。

从 :mod:`lingxi.apps.worker.service`（Trace #358 S-H-2，Issue #350 Gate G-3
裁定 Option A）搬出——``WorkerService`` 本体（消费循环、终态收口、进度节流）不
拆，只把这批不依赖 ``self`` 的模块级纯函数连同各自专属常量一起挪到独立文件。
``_load_task_system_prompt`` 被 ``apps/worker/cli.py`` 外部 import，
``apps/worker/service.py`` 顶部对本文件做 re-export 维持该调用点不变。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

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
    """从一次回合报告里取出**供落库**的文档投递请求（Issue #341 S-ES-3；Issue
    #408 正式方案接线新增 ``markdown``）。

    ``report["document_request"]`` 由 ``apps/worker/report.py::build_report``
    投影（见该函数文档）：``None`` 或已过硬上限与出口安全检查的
    ``{"title": str, "paragraphs": list[str], "markdown": str}``。这里只做结构
    校验，形状不对一律返回 ``None``——与 :func:`_report_guard_denied_count` 同一
    纪律：结构性地不可信就不传，不猜测、不编造。

    ``markdown`` 单独降级：它是「段落之外的附加值」，不是幂等判据也不是兜底
    路径依赖的字段（那两者都只看 ``paragraphs``）——形状不对时这里只丢弃
    ``markdown`` 本身（落库为 ``NULL``，gateway 侧据此回退段落路径），不因为
    这一个字段拒绝整条本来合法的登记请求。
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
    markdown = request.get("markdown")
    return {
        "title": title,
        "paragraphs": paragraphs,
        "markdown": markdown if isinstance(markdown, str) and markdown else None,
    }


def _report_sheet_request(report: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """从一次回合报告里取出**供落库**的表格投递请求（Issue #354 S-H3-2）。

    与 :func:`_report_document_request` 逐项对称：``report["sheet_request"]``
    由 ``apps/worker/report.py::build_report`` 投影为 ``None`` 或
    ``{"title": str, "rows": list[list[str]]}``，这里只做结构校验，形状不对一律
    返回 ``None``——同一纪律：结构性地不可信就不传，不猜测、不编造。
    """

    request = report.get("sheet_request") if isinstance(report, Mapping) else None
    if not isinstance(request, Mapping):
        return None
    title = request.get("title")
    rows = request.get("rows")
    if not isinstance(title, str) or not title:
        return None
    if not isinstance(rows, list) or not rows or not all(
        isinstance(row, list) and row and all(isinstance(cell, str) for cell in row)
        for row in rows
    ):
        return None
    return {"title": title, "rows": rows}


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


# ---------------------------------------------------------------------------
# 失败签名与「没人起名字」的失败码（Issue #495）
# ---------------------------------------------------------------------------
#
# 2026-08-31 浸泡窗口取证：8 条任务失败里 6 条**无法归因**——结构化日志只留下
# ``worker.task.terminal error_kind=session_failed failure_code=null``，底层异常
# 的类型、文本、堆栈一条都没有离开进程。用户侧是对的（#465 响应覆盖成立），
# 运维侧是全黑的。能归因的另外 2 条恰恰是因为走了另一条**会落日志**的分支
# （``worker.mcp_server_unavailable`` 记下了 502），正反对照就在同一批样本里。
#
# 这里补的是那条线索，**不是**把异常正文放出来：``V-花名册-33`` 禁止把 ``ou_``
# 等外部标识原值写进日志，而 psycopg 等驱动的异常串常见形状正是
# ``DETAIL: Key (feishu_open_id)=(ou_...)``。rc22 opus 审查 P2-5 已经为
# ``event.pipeline_failed`` 做过同一次收敛（``core/conversation/pipeline.py``：
# 只记 ``type(error).__name__``），本模块照抄那条既有做法，不另造一套。

#: 失败签名允许出现的字符白名单之外的一切（ASCII 字母数字、下划线、点）。
#: 用**白名单**而不是黑名单：异常类型名与模块名是源码里写死的标识符，形状已知；
#: 任何超出这个集合的字符都说明这不是一个正常的类型标识（例如某个 SDK 用异常
#: 正文动态造类），一律剔除，宁可签名难看也不让未知内容搭车进日志。
_FAILURE_SIGNATURE_DISALLOWED = re.compile(r"[^A-Za-z0-9_.]")

#: 签名长度上界。与 ``_cap_log_token`` 的 64 同量级——真实类型限定名
#: （``psycopg.errors.UniqueViolation`` 30 字符）远在其内，超长只可能来自异常。
_MAX_FAILURE_SIGNATURE_CHARS = 64

#: 连类型名都取不到时的显式占位（例如某个对象的 ``__name__`` 被清成空串）。
#: **不是 ``None``**：这一列的存在意义就是"任何终态都留得下一个可查的记号"。
UNKNOWN_FAILURE_SIGNATURE = "unknown"

#: 「这次失败没有人给它起名字」时按报告里已有事实推出的三个显式码（Issue #495
#: 完成标准 3：``failure_code`` 在失败终态下不再为 ``null``）。三个都落进
#: ``apps/worker/service.py::_failure_content`` 的默认分支——用户可见文案因此
#: **逐字不变**（``worker.failed``），新增的区分度只留在审计/日志侧。
#:
#: - ``gate_bypassed``：有工具调用绕过了 ``PreToolUse`` 判定（屏障失效）；
#: - ``unnamed_failure``：报告带了 ``failure`` 但里面没有 ``code``；
#: - ``turn_not_closed``：回合就是没收口，没有更具体的事实可说。
#:   取值与 ``apps/worker/report.py`` 里 ``termination_reason`` 对同一情形使用
#:   的字符串一致，不另造第二套词。
GATE_BYPASSED_FAILURE_CODE = "gate_bypassed"
UNNAMED_FAILURE_CODE = "unnamed_failure"
TURN_NOT_CLOSED_FAILURE_CODE = "turn_not_closed"


def exception_failure_signature(error: BaseException) -> str:
    """把一个异常收敛成**可以进日志**的失败签名：只取类型的限定名。

    取 ``模块.限定名``（``builtins`` 前缀去掉）而不是裸 ``__name__``：真实故障里
    最需要区分的恰恰是同名类型——``psycopg.errors.OperationalError`` 与
    ``httpx.ConnectError`` 与 SDK 自己的 ``ProcessError`` 在裸类名下也许还能分开，
    但 ``TimeoutError``/``ConnectionError`` 这类名字被多个库各自定义，只有带上
    模块才回答得了"是数据库超时还是外部 HTTP 超时"。模块名与类名都是源码标识符，
    不是运行时数据，因此这一步不引入新的泄漏面。

    **异常正文（``str(error)``）在任何情况下都不参与**——那是 P2-5 收敛掉的那半，
    见本节顶部说明。
    """

    error_type = type(error)
    module = _FAILURE_SIGNATURE_DISALLOWED.sub("", getattr(error_type, "__module__", "") or "")
    raw_name = getattr(error_type, "__qualname__", "") or getattr(error_type, "__name__", "") or ""
    name = _FAILURE_SIGNATURE_DISALLOWED.sub("", raw_name)
    qualified = name if module in ("", "builtins") else f"{module}.{name}"
    if len(qualified) <= _MAX_FAILURE_SIGNATURE_CHARS:
        return qualified or UNKNOWN_FAILURE_SIGNATURE
    # 超过上界时**先丢模块路径、后动类名**：判别力全在类名那一半，从右边截断
    # 会先啃掉类名（``a.very.long.module.path.Timeout`` 截成 ``a.very.long.mod``
    # 这种毫无用处的形状）。类名自己就超限时保留右端——嵌套类的最内层名字最具体。
    if len(name) <= _MAX_FAILURE_SIGNATURE_CHARS:
        return name or UNKNOWN_FAILURE_SIGNATURE
    return name[-_MAX_FAILURE_SIGNATURE_CHARS:]


def failure_with_signature(code: str, message: str, error: BaseException) -> dict[str, str]:
    """构造一份带失败签名的 ``failure`` 映射（Issue #495）。

    三个调用点（``turn.py`` 的兜底 ``except``、``service.py`` 的
    ``UserMcpConfigError`` 与执行器兜底 ``except``）共用同一个构造口，是为了让
    "任何从异常来的失败都必须带签名"成为一件**改不漏**的事：新增一条异常收口
    分支时照抄这一行即可，不必记得再补一个字段。``message`` 由调用方按各自的
    脱敏纪律准备好后传入，本函数不再加工它。
    """

    return {"code": code, "message": message, "signature": exception_failure_signature(error)}


def sanitize_failure_signature(value: str) -> str:
    """按白名单洗掉签名里的一切非标识符字符并截到上界；洗空则回退占位符。

    :func:`exception_failure_signature` 自己做更聪明的截断（见其说明），这里是
    **给来路不明的值兜底**的那一版：报告是跨进程 JSON，``failure.signature``
    完全可能来自一个未来版本、一个测试替身、或一次形状不对的构造，读回时不能
    假设它已经符合约束。
    """

    cleaned = _FAILURE_SIGNATURE_DISALLOWED.sub("", value)[:_MAX_FAILURE_SIGNATURE_CHARS]
    return cleaned or UNKNOWN_FAILURE_SIGNATURE


def _report_failure_signature(report: Mapping[str, Any]) -> str | None:
    """从一次回合报告里取出失败签名；没有（例如失败根本不来自异常）返回 ``None``。

    ``None`` 是精确语义、不是"以后补"：``turn_timeout``/``drain_timeout``/
    ``cancelled`` 这些失败码本身已经把原因说全了，没有底层异常可签名，编一个
    占位符只会让"有签名"这件事失去信息量。
    """

    failure = report.get("failure") if isinstance(report, Mapping) else None
    if not isinstance(failure, Mapping):
        return None
    signature = failure.get("signature")
    if not isinstance(signature, str) or not signature:
        return None
    return sanitize_failure_signature(signature)


def _unnamed_failure_code(report: Mapping[str, Any]) -> str:
    """失败终态但 ``failure`` 没给出 ``code`` 时，按报告里已有的事实推一个显式码。

    **不推断成 ``stopped``、也不推断成任何用户可见语义**：这里只回答"这次失败
    在报告里长什么样"，三个取值各自对应一个可核对的事实（见上方常量说明）。
    """

    failure = report.get("failure") if isinstance(report, Mapping) else None
    if isinstance(failure, Mapping) and failure:
        return UNNAMED_FAILURE_CODE
    turn = report.get("turn") if isinstance(report, Mapping) else None
    if isinstance(turn, Mapping) and turn.get("gate_bypassed"):
        return GATE_BYPASSED_FAILURE_CODE
    return TURN_NOT_CLOSED_FAILURE_CODE
