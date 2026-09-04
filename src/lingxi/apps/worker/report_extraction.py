"""从一次回合报告里取出各类供落库/供低敏审计日志用的字段。

纯函数，不碰网络、不碰数据库、不持有任何状态。从 :mod:`lingxi.apps.worker.service` 搬出——``WorkerService`` 本体（消费循环、
终态收口、进度节流）不拆，只把这批不依赖 ``self`` 的模块级纯函数连同各自
专属常量一起挪到独立文件。
``_load_task_system_prompt`` 被 ``apps/worker/cli.py`` 外部 import，
``apps/worker/service.py`` 顶部对本文件做 re-export 维持该调用点不变。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

_MAX_LOG_TOKEN_CHARS = 64
# 拒绝文案对用户承诺"问题已经被记录"：一次回合里模型可能反复撞同一个越界
# 工具，工具名列表上界防止一次异常回合把收口日志撑大。
_MAX_LOG_DENIED_TOOL_NAMES = 20


def _cap_log_token(value: str) -> tuple[str, bool]:
    """把失败码/安全原因码这类短标识截到审计安全的长度上界。

    这些值目前全部来自本仓库固定的枚举式常量（失败码、安全原因码），不是
    模型输出，但收口日志是低敏审计的唯一出口——不给未来新增码值设长度上界，
    就是给"某次改动不小心把一段自由文本塞进这个字段"留了一条不设防的泄漏面。
    """
    if len(value) <= _MAX_LOG_TOKEN_CHARS:
        return value, False
    return value[:_MAX_LOG_TOKEN_CHARS], True


def _denied_tool_summary(report: Mapping[str, Any]) -> tuple[int, tuple[str, ...]]:
    """从一次回合报告里取出被拒工具调用的计数与工具名。

    这里只取计数与工具名——不取 ``tool_input``，工具参数正文与用户资料值
    不属于这条低敏审计事件。早退分支（未真正跑过 ``PreToolUse`` 判定）取不
    到就如实记 0/空，不假装有据可查。**已知边界（如实登记、不修）**：executor
    在拒绝**之后**异常退出时，已发生的拒绝计数会跟着不完整的 ``report``
    一起丢失，同样如实返回 0/空——回合本身已落到响亮的失败终态，唯一代价
    是看不到补充事实。
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
    """从一次回合报告里取出**供落库**的守卫拒绝计数（迁移 ``0070``）。

    与 :func:`_denied_tool_summary` 故意**不共享**同一个返回值：这里要写进
    ``task.guard_denied_count`` 供统计聚合，聚合层必须能区分"真的查过、结果
    是零次拒绝"与"没有可用的审计数据"，把后者算成 0 会悄悄低估真实拒绝次数
    且没有任何信号能让读者发现。``report["audit"]`` 不存在，或 ``denied_
    count`` 不是 ``int``/是负数（结构性地不可信）都返回 ``None``；其余情况
    原样返回真实整数，包括合法的 0。
    """
    audit = report.get("audit") if isinstance(report, Mapping) else None
    if not isinstance(audit, Mapping):
        return None
    count = audit.get("denied_count")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        return count
    return None


#: ``core/execution/message_stream.py::_usage_summary`` 产出的四个已知 token
#: 计数字段名，与该模块同源——这里独立列一份常量而不是 import 那个私有名字，
#: 避免为四个字面量常量新增一条跨层 import 边界。字段名一旦那边改动，这里
#: 也要跟着改（无自动化保证，人工同步）。
_TOKEN_USAGE_FIELD_NAMES = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _report_token_usage(report: Mapping[str, Any]) -> dict[str, int] | None:
    """从一次回合报告里取出**供落库**的 token 用量（迁移 ``0070``）。

    ``report["resources"]["usage"]`` 恒为 ``{"status": "known"|"unknown",
    ["fields": {...}]}``。只有 ``status == "known"`` 时才有真正可信的计数
    ——``"unknown"`` 覆盖三种取不到的原因，共同点是没有可入库的数字，返回
    ``None`` 如实反映，不编造 0。早退分支同样返回 ``None``；返回值只包含
    ``fields`` 里实际出现的键，不为缺失的字段补零。
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
    """从一次回合报告里取出**供落库**的文档投递请求。

    ``report["document_request"]`` 由 ``build_report`` 投影：``None`` 或已过
    检查的 ``{"title", "paragraphs", "markdown"}``。这里只做结构校验，形状
    不对一律返回 ``None``——结构性地不可信就不传，不猜测、不编造。
    ``markdown`` 单独降级：它是段落之外的附加值，不是幂等判据也不是兜底
    路径依赖的字段，形状不对时只丢弃这一个字段（落库为 ``NULL``），不因此
    拒绝整条本来合法的登记请求。
    """
    request = report.get("document_request") if isinstance(report, Mapping) else None
    if not isinstance(request, Mapping):
        return None
    title = request.get("title")
    paragraphs = request.get("paragraphs")
    if not isinstance(title, str) or not title:
        return None
    if (
        not isinstance(paragraphs, list)
        or not paragraphs
        or not all(isinstance(paragraph, str) for paragraph in paragraphs)
    ):
        return None
    markdown = request.get("markdown")
    return {
        "title": title,
        "paragraphs": paragraphs,
        "markdown": markdown if isinstance(markdown, str) and markdown else None,
    }


def _report_sheet_request(report: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """从一次回合报告里取出**供落库**的表格投递请求。

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
    if (
        not isinstance(rows, list)
        or not rows
        or not all(
            isinstance(row, list) and row and all(isinstance(cell, str) for cell in row)
            for row in rows
        )
    ):
        return None
    return {"title": title, "rows": rows}


def _tool_result_count(report: Mapping[str, Any]) -> int:
    """从一次回合报告里取出这一轮**真实**工具调用次数，补一处可观测性缺口。

    运维此前没有任何字段能直接回答"这一轮到底有没有真的调用过工具"；这里
    把 ``report["audit"]["tool_result_count"]`` 取出并写进
    ``worker.task.terminal``。``== 0`` **单独不构成**异常信号——闲聊类问题
    本来就不需要调用工具；真正的判定见 ``_protocol_breakdown_reasons``，与
    这里的调用次数无关。早退分支同样如实返回 0。
    """
    audit = report.get("audit") if isinstance(report, Mapping) else None
    if not isinstance(audit, Mapping):
        return 0
    count = audit.get("tool_result_count")
    return count if isinstance(count, int) else 0


# 默认提示词文件的单次读取上界：提示词是几百到几千字的行为指令，64KiB 已
# 远超合理体量；不设上界，一次误操作（比如把数据文件拷成提示词文件名）就会
# 把巨块文本塞进每一轮模型上下文，成本失控且难以察觉。超限按"不可用"降级，
# 与文件缺失同一路径。
_MAX_SYSTEM_PROMPT_BYTES = 64 * 1024


def _read_bounded_prompt_file(path: str) -> tuple[bytes | None, str | None]:
    """有界、防符号链接/FIFO 地读取提示词文件，返回 ``(内容, 失败原因码)``。

    ``O_NOFOLLOW`` 拒绝符号链接——一个指向 ``.mcp.json`` 的链接会把凭据喂进
    模型上下文，而出口安全发生在模型执行之后、撤不回已发送的系统提示；
    ``O_NONBLOCK`` + 普通文件校验拒绝 FIFO/设备文件；有界读取保证误放一个
    数 GiB 文件时不整读进内存。残余边界（如实登记，不在本层修）：本层不对
    路径做目录白名单，写死允许目录同样违反「不硬编码路径」。
    """
    import os as _os
    import stat as _stat

    try:
        fd = _os.open(path, _os.O_RDONLY | _os.O_NOFOLLOW | _os.O_NONBLOCK)
    except OSError:
        return None, "unreadable"
    try:
        if not _stat.S_ISREG(_os.fstat(fd).st_mode):
            return None, "not_regular_file"
        chunks: list[bytes] = []
        remaining = _MAX_SYSTEM_PROMPT_BYTES + 1
        while remaining > 0:
            try:
                chunk = _os.read(fd, remaining)
            except OSError:
                return None, "unreadable"
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        _os.close(fd)
    if len(raw) > _MAX_SYSTEM_PROMPT_BYTES:
        return None, "oversized"
    return raw, None


def _prompt_collides_with_terminal_text(prompt: str) -> bool:
    """预演一遍终态自检：提示词是否会与固定终态文案互相命中。

    出口安全层会把提示词逐句派生成禁词遮蔽模型正文，若提示词与固定终态文案
    （空产出兜底/整段拒发）互相命中，空产出回合会在终态自检里抛异常，把
    "总是返回一份报告"的契约炸掉。这里用同一个公开函数预演一遍，命中就让
    调用方提前降级——坏提示词只废掉自己，不废掉回合。
    """
    from lingxi.core.execution.input_safety import (
        SAFE_OUTPUT_FALLBACK,
        WITHHELD_MESSAGE,
        InputSafetyError,
        constrain_output,
    )

    try:
        return any(
            constrain_output(fixed_text, system_prompt=prompt).blocked
            for fixed_text in (SAFE_OUTPUT_FALLBACK, WITHHELD_MESSAGE)
        )
    except InputSafetyError:
        return True


def _load_task_system_prompt(path: str) -> tuple[str | None, str | None, str | None]:
    """每个任务开始时现读默认提示词文件，返回 ``(提示词, 内容摘要, 降级原因)``。

    现读（而不是启动时读一次）就是这个机制的全部意义：运维编辑挂载卷上的
    文件后**下一条消息即生效**，不需要重启容器或重建镜像。各类不可用一律
    降级为 ``(None, None, 原因码)``——提示词是行为调优不是安全屏障，把任务
    押在一个随手可改的文件上才是更大的风险；降级必须留痕，由调用方写结构化
    告警。摘要（sha256 前 12 位）随终态审计事件落日志，只记摘要不记正文；
    记录口径是"本轮解析出并交给执行器装配的版本"，不声称模型一定收到了它。
    """
    raw, reason = _read_bounded_prompt_file(path)
    if reason is not None:
        return None, None, reason
    try:
        prompt = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None, None, "not_utf8"
    if not prompt:
        return None, None, "empty"
    if _prompt_collides_with_terminal_text(prompt):
        return None, None, "terminal_text_collision"
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
    return prompt, digest, None


# P0 护栏：模型正文里出现内部工具名或过程标记（协议细节），**永远**是模型把
# 工具调用协议写成了正文散文，不是"内容需要脱敏但业务结论还在"。净化层职责
# 到"遮蔽敏感片段"为止，"这段正文根本不该被当成答案交付"的判断只能发生在这里。
# 刻意排除其余原因码：那三类是"内容含有已知敏感值/系统提示"，withheld 分支
# 已按"是否还有幸存业务内容"正确处理，收紧过窄的边界会误伤正常业务回答。
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


# 失败签名与「没人起名字」的失败码：多数任务失败此前**无法归因**——结构化
# 日志只留下失败码为空的终态，底层异常的类型、文本、堆栈一条都没有离开
# 进程。这里补的是那条线索，**不是**把异常正文放出来：`V-花名册-33` 禁止把
# 外部标识原值写进日志，本模块采用固定形状的加密摘要，不把动态类名或异常
# 正文原样带进低敏出口。

#: 签名长度上界。摘要是固定 ASCII 形状；64 字符仍与 ``_cap_log_token`` 同量级，
#: 兼容迁移 0080 已有的 TEXT 列和旧日志预算。
_MAX_FAILURE_SIGNATURE_CHARS = 64

#: 连类型名都取不到时的显式占位（例如某个对象的 ``__name__`` 被清成空串）。
#: **不是 ``None``**：这一列的存在意义就是"任何终态都留得下一个可查的记号"。
UNKNOWN_FAILURE_SIGNATURE = "unknown"

# 异常类型来自 Python 运行时，模块名和限定类名都可能被 SDK 动态改成用户输入。
# 不能用字符洗掉括号/空格来"净化"它：``ou_x`` 这类值只要本身由允许字符组成，
# 洗完仍会原样留在日志和 /admin trace。这里把完整类型身份只作为 SHA-256 输入，
# 出口只保留固定的类别词与 160-bit 摘要；摘要不是可逆编码，也不接收异常正文。
_FAILURE_SIGNATURE_DIGEST_HEX_CHARS = 40
_FAILURE_SIGNATURE_PREFIX = "exception"
_FAILURE_SIGNATURE_FAMILIES = frozenset(
    {"builtin", "database", "http", "sdk", "runtime", "external"}
)
_FAILURE_SIGNATURE_PATTERN = re.compile(
    rf"^{re.escape(_FAILURE_SIGNATURE_PREFIX)}\."
    rf"(?:{'|'.join(sorted(_FAILURE_SIGNATURE_FAMILIES))})\."
    rf"[0-9a-f]{{{_FAILURE_SIGNATURE_DIGEST_HEX_CHARS}}}$"
)

# 这类签名不是异常类型，而是结构化外因的固定分类；只允许已经批准的字面量
# 穿过跨进程报告。未来新增结构化外因必须在这里登记，不能让任意字符串借白名单
# 之名进入 task 或低敏日志。
_STABLE_FAILURE_SIGNATURES = frozenset({"mcp.query.http_502", UNKNOWN_FAILURE_SIGNATURE})

# 只输出这些固定类别名。匹配的是完整模块或其子模块，模块字符串本身永不回显，
# 因此即使动态模块名里带 open_id/邮箱，也只能影响类别（固定词）和摘要。
_EXCEPTION_MODULE_FAMILIES: tuple[tuple[str, str], ...] = (
    ("psycopg", "database"),
    ("httpx", "http"),
    ("httpcore", "http"),
    ("aiohttp", "http"),
    ("requests", "http"),
    ("claude_agent_sdk", "sdk"),
    ("asyncio", "runtime"),
    ("anyio", "runtime"),
    ("trio", "runtime"),
    ("lingxi", "runtime"),
    ("builtins", "builtin"),
)

#: 「这次失败没有人给它起名字」时按报告里已有事实推出的三个显式码：三个都
#: 落进默认分支，用户可见文案逐字不变，新增的区分度只留在审计/日志侧。
#: ``gate_bypassed`` 有工具调用绕过了 ``PreToolUse`` 判定；``unnamed_
#: failure`` 报告带了 ``failure`` 但没有 ``code``；``turn_not_closed`` 回合
#: 就是没收口。取值与 ``report.py`` 的 ``termination_reason`` 一致，不另造词。
GATE_BYPASSED_FAILURE_CODE = "gate_bypassed"
UNNAMED_FAILURE_CODE = "unnamed_failure"
TURN_NOT_CLOSED_FAILURE_CODE = "turn_not_closed"


def exception_failure_signature(error: BaseException) -> str:
    """把一个异常收敛成**可以进日志**的固定形状失败签名。

    直接落"模块.限定类名"会让 SDK 动态造出的类名把用户标识带进日志；对异常
    正文做字符白名单也挡不住形如 ``Key (feishu_open_id)=(ou_x)`` 的详情文本
    ，因为其中的标识本身全是白名单字符。这里只把模块/限定类名用于 SHA-256
    输入，返回 ``exception.<固定类别>.<160-bit摘要>``——类别只来自固定表，
    不会把动态类型或异常正文可逆地编码进持久状态，``str(error)`` 不参与。
    """
    error_type = type(error)
    try:
        module = getattr(error_type, "__module__", None)
        name = getattr(error_type, "__qualname__", None)
    except Exception:  # 恶意元类不得阻断失败收口
        return UNKNOWN_FAILURE_SIGNATURE
    if not isinstance(module, str) or not isinstance(name, str) or not module or not name:
        return UNKNOWN_FAILURE_SIGNATURE
    # 元数据也可能是带重载方法的 str 子类；归一为内建 str 后再做分类/编码，避免
    # 恶意 ``startswith`` 等实现把终态收口重新变成一个会抛异常的路径。
    module = str.__str__(module)
    name = str.__str__(name)

    family = _exception_signature_family(module)
    try:
        # str.encode 的显式调用避免可疑 str 子类重载 encode；surrogatepass 让异常
        # 元数据里的孤立代理字符也得到稳定摘要，而不是在兜底收口时再次抛错。
        identity = (
            str.encode(module, "utf-8", "surrogatepass")
            + b"\x00"
            + str.encode(name, "utf-8", "surrogatepass")
        )
        digest = hashlib.sha256(b"lingxi.failure-signature.v1\x00" + identity).hexdigest()
    except (TypeError, UnicodeError):
        return UNKNOWN_FAILURE_SIGNATURE
    signature = (
        f"{_FAILURE_SIGNATURE_PREFIX}.{family}.{digest[:_FAILURE_SIGNATURE_DIGEST_HEX_CHARS]}"
    )
    return (
        signature if len(signature) <= _MAX_FAILURE_SIGNATURE_CHARS else UNKNOWN_FAILURE_SIGNATURE
    )


def _exception_signature_family(module: str) -> str:
    """把异常模块归入固定低基数类别；不返回模块原文。"""
    for prefix, family in _EXCEPTION_MODULE_FAMILIES:
        if module == prefix or module.startswith(f"{prefix}."):
            return family
    return "external"


def failure_with_signature(code: str, message: str, error: BaseException) -> dict[str, str]:
    """构造一份带失败签名的 ``failure`` 映射。

    三个调用点（``turn.py`` 的兜底 ``except``、``service.py`` 的
    ``UserMcpConfigError`` 与执行器兜底 ``except``）共用同一个构造口，是为了让
    "任何从异常来的失败都必须带签名"成为一件**改不漏**的事：新增一条异常收口
    分支时照抄这一行即可，不必记得再补一个字段。``message`` 由调用方按各自的
    脱敏纪律准备好后传入，本函数不再加工它。
    """
    return {"code": code, "message": message, "signature": exception_failure_signature(error)}


def sanitize_failure_signature(value: str) -> str:
    """只接受固定分类或本模块生成的摘要，拒绝任意报告字符串。

    报告跨 worker/gateway 进程传递，``failure.signature`` 不能被当作可信的类型名。
    旧实现把 ``Key (feishu_open_id)=(ou_x)`` 洗成
    ``Keyfeishu_open_idou_x``，等于把敏感值换一种可逆形式继续持久化；现在任何不
    符合固定形状的值都降为 ``unknown``，不做字符替换，也不截取原文。
    """
    if not isinstance(value, str):
        return UNKNOWN_FAILURE_SIGNATURE
    if value in _STABLE_FAILURE_SIGNATURES:
        return value
    if _FAILURE_SIGNATURE_PATTERN.fullmatch(value):
        return value
    return UNKNOWN_FAILURE_SIGNATURE


def _report_failure_signature(report: Mapping[str, Any]) -> str | None:
    """从一次回合报告里取出失败签名；没有（例如失败根本不来自异常）返回 ``None``。

    通常签名是底层异常的固定类别摘要；少数结构化外因也可以携带
    稳定的分类签名，例如指标 MCP 的 ``mcp.query.http_502``。两种形状都必须经过
    同一条严格形状校验，不能让跨进程报告把自由文本带进审计出口。

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
