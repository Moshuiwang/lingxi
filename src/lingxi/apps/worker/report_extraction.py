"""失败签名构造、提示词文件读取与审计令牌截断：worker 收口用到的几件应用层小工具。

从 :mod:`lingxi.apps.worker.service` 搬出的模块级函数里，读回合报告字段的那一批已
归入 :mod:`lingxi.core.delivery.turn_report`；留在这里的三组要么做有界文件 I/O
（提示词文件读取），要么只服务收口日志与异常收口，不进 ``core/``。
``_load_task_system_prompt`` 被 ``apps/worker/cli.py`` 外部 import，
``apps/worker/service.py`` 顶部对本文件做 re-export 维持该调用点不变。
"""

from __future__ import annotations

import hashlib

from lingxi.core.failure_signature import (
    _FAILURE_SIGNATURE_DIGEST_HEX_CHARS,
    _FAILURE_SIGNATURE_PREFIX,
    _MAX_FAILURE_SIGNATURE_CHARS,
    UNKNOWN_FAILURE_SIGNATURE,
)

# 显式再导出：终态收口与审计出口仍从本模块取失败签名的清洗函数，调用点不随本次平移改动。
from lingxi.core.failure_signature import sanitize_failure_signature as sanitize_failure_signature

_MAX_LOG_TOKEN_CHARS = 64


def _cap_log_token(value: str) -> tuple[str, bool]:
    """把失败码/安全原因码这类短标识截到审计安全的长度上界。

    这些值目前全部来自本仓库固定的枚举式常量（失败码、安全原因码），不是
    模型输出，但收口日志是低敏审计的唯一出口——不给未来新增码值设长度上界，
    就是给"某次改动不小心把一段自由文本塞进这个字段"留了一条不设防的泄漏面。
    """
    if len(value) <= _MAX_LOG_TOKEN_CHARS:
        return value, False
    return value[:_MAX_LOG_TOKEN_CHARS], True


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


# 失败签名与「没人起名字」的失败码：多数任务失败此前**无法归因**——结构化
# 日志只留下失败码为空的终态，底层异常的类型、文本、堆栈一条都没有离开
# 进程。这里补的是那条线索，**不是**把异常正文放出来：`V-花名册-33` 禁止把
# 外部标识原值写进日志，本模块采用固定形状的加密摘要，不把动态类名或异常
# 正文原样带进低敏出口。

#: 签名长度上界。摘要是固定 ASCII 形状；64 字符仍与 ``_cap_log_token`` 同量级，
#: 兼容迁移 0080 已有的 TEXT 列和旧日志预算。


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
