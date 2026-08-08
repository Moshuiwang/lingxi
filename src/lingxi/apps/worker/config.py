"""worker 入口的类型化配置：只从 ``LINGXI_`` 前缀环境变量读一次。

[代码框架「三、横切约定」](../../../../docs/技术设计/代码框架.md)要求配置在 ``apps``
入口一次性读取并构造成类型化对象往下传，``core`` 与 ``adapters`` 不碰 ``os.environ``；
主机、端口、路径、密钥不得硬编码（`V-部署-01`）。

校验刻意放在**构造期**：白名单形态、工具数量这类约束在运行期才发现，意味着一次
本不该发起的会话已经建起来了。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from lingxi.core.execution.tool_policy import is_well_formed_tool_name
from lingxi.core.ids import new_ulid

ENV_PREFIX = "LINGXI_WORKER_"

# 这是部署配置的默认口径；实际任务值仍从环境变量读取。硬上限是产品为单任务
# 设下的安全边界，越过它必须在启动期拒绝，不能让一次部署带着不确定的成本口径运行。
DEFAULT_MAX_TURNS = 20
MAX_TURNS_HARD_LIMIT = 30
DEFAULT_TURN_TIMEOUT_SECONDS = 600.0
TURN_TIMEOUT_HARD_LIMIT_SECONDS = 900.0

# 本切片只允许一个明确确认过的**只读 MCP 工具**（Issue #37 实施范围 2）。要求
# ``mcp__`` 前缀是这条范围的机器可核对形式：Skill、Agent、Task 与任何内置工具都
# 因此落在配置期拒绝分支里，不需要维护一份"禁止配置"的名单。
MCP_TOOL_PREFIX = "mcp__"


class WorkerConfigError(ValueError):
    """配置不合法。启动即失败，不留到会话建立之后。"""


@dataclass(frozen=True)
class WorkerConfig:
    """一次受控回合需要的全部输入。"""

    question: str
    read_only_tool: str
    trace_id: str
    # 单回合墙钟上限：SDK 传输挂住不发终止消息时，没有它整个回合会永久等待，
    # 连失败报告都出不来（Codex 复查发现）。
    turn_timeout_seconds: float
    # 直接构造配置的测试与嵌入调用方沿用旧接口时仍使用同一安全默认值；正式入口
    # 通过 load_config 显式校验并传入部署值。
    max_turns: int = DEFAULT_MAX_TURNS
    audit_input_fields: tuple[str, ...] = ()
    failure_text_markers: tuple[str, ...] = ()
    mcp_servers: Mapping[str, Any] = field(default_factory=dict)
    workspace: str | None = None
    model: str | None = None
    system_prompt: str | None = None
    # #93 walking skeleton：CLI 可接收一个已知的指标描述外部文本。真实花名册 / MCP
    # 来源仍由后续主链路注入；这里不把该配置当作权限或身份事实。
    external_texts: tuple[tuple[str, str], ...] = ()
    worker_id: str = ""
    target_worker_version: str = "stable"
    queue_max_wait_seconds: float = 180.0
    worker_version_unavailable_seconds: float = 180.0
    running_heartbeat_timeout_seconds: float = 90.0
    heartbeat_interval_seconds: float = 30.0
    stop_poll_interval_seconds: float = 1.0
    poll_interval_seconds: float = 2.0
    max_auto_retries: int = 1
    max_concurrency: int = 16


def load_config(env: Mapping[str, str], *, require_question: bool = True) -> WorkerConfig:
    """从环境变量构造配置。缺失或不合法时抛 :class:`WorkerConfigError`。"""

    required_names = ("QUESTION", "READONLY_TOOL") if require_question else ("READONLY_TOOL",)
    missing = [name for name in required_names if not _text(env, name)]
    if missing:
        raise WorkerConfigError(
            "缺少必填环境变量：" + "、".join(f"{ENV_PREFIX}{name}" for name in missing)
        )

    return WorkerConfig(
        question=_text(env, "QUESTION") or "",
        read_only_tool=_read_only_tool(env),
        trace_id=_validated_trace_id(_text(env, "TRACE_ID")),
        max_turns=_max_turns(_text(env, "MAX_TURNS")),
        turn_timeout_seconds=_turn_timeout(_text(env, "TURN_TIMEOUT_SECONDS")),
        audit_input_fields=_names(env, "AUDIT_INPUT_FIELDS"),
        failure_text_markers=_failure_markers(env),
        mcp_servers=_mcp_servers(env),
        workspace=_text(env, "WORKSPACE"),
        model=_text(env, "MODEL"),
        system_prompt=_text(env, "SYSTEM_PROMPT"),
        external_texts=_external_texts(env),
        worker_id=_text(env, "ID") or new_ulid(),
        target_worker_version=_text(env, "TARGET_VERSION") or "stable",
        queue_max_wait_seconds=_duration(env, "QUEUE_MAX_WAIT_SECONDS", 180.0),
        worker_version_unavailable_seconds=_duration(
            env, "WORKER_VERSION_UNAVAILABLE_SECONDS", 180.0
        ),
        running_heartbeat_timeout_seconds=_duration(
            env, "RUNNING_HEARTBEAT_TIMEOUT_SECONDS", 90.0
        ),
        heartbeat_interval_seconds=_duration(env, "HEARTBEAT_INTERVAL_SECONDS", 30.0),
        stop_poll_interval_seconds=_duration(env, "STOP_POLL_INTERVAL_SECONDS", 1.0),
        poll_interval_seconds=_duration(env, "POLL_INTERVAL_SECONDS", 2.0),
        max_auto_retries=_positive_int(env, "MAX_AUTO_RETRIES", 1, allow_zero=True),
        max_concurrency=_positive_int(env, "MAX_CONCURRENCY", 16),
    )


def _text(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(f"{ENV_PREFIX}{name}")
    if value is None:
        return None
    value = value.strip()
    return value or None


def _read_only_tool(env: Mapping[str, str]) -> str:
    value = _text(env, "READONLY_TOOL") or ""
    if not is_well_formed_tool_name(value):
        # 逗号、空格、通配符都会落到这里：白名单只接受**一个精确名称**，
        # 想放行第二个工具必须是一次有人复核的范围变更，不是改一个环境变量。
        raise WorkerConfigError(
            f"{ENV_PREFIX}READONLY_TOOL 必须是单个合法工具名（只允许字母、数字、"
            f"下划线、点和连字符），收到：{value!r}"
        )
    if not value.startswith(MCP_TOOL_PREFIX):
        raise WorkerConfigError(
            f"{ENV_PREFIX}READONLY_TOOL 只能是以 {MCP_TOOL_PREFIX} 开头的只读 MCP 工具；"
            f"Skill、Agent、Task 和内置工具都不在本切片范围内，收到：{value!r}"
        )
    return value


def _names(env: Mapping[str, str], name: str) -> tuple[str, ...]:
    raw = _text(env, name)
    if not raw:
        return ()
    names = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not names:
        raise WorkerConfigError(f"{ENV_PREFIX}{name} 只有分隔符，没有任何名称")
    return names


def _failure_markers(env: Mapping[str, str]) -> tuple[str, ...]:
    """已登记的业务失败措辞。默认为空——外部 MCP 用什么措辞表达业务失败必须先从
    真实回执确认再登记，猜错的后果是把失败写成成功（`V-执行-06`）。"""

    raw = _text(env, "FAILURE_MARKERS")
    if not raw:
        return ()
    parsed = _json(raw, "FAILURE_MARKERS")
    if not isinstance(parsed, list) or not all(isinstance(item, str) and item for item in parsed):
        raise WorkerConfigError(f"{ENV_PREFIX}FAILURE_MARKERS 必须是非空字符串组成的 JSON 数组")
    return tuple(parsed)


def _mcp_servers(env: Mapping[str, str]) -> Mapping[str, Any]:
    raw = _text(env, "MCP_SERVERS")
    if not raw:
        return {}
    parsed = _json(raw, "MCP_SERVERS")
    if not isinstance(parsed, dict):
        raise WorkerConfigError(f"{ENV_PREFIX}MCP_SERVERS 必须是 JSON 对象（服务名 → 配置）")
    return parsed


def _external_texts(env: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """读取 CLI 已知的指标描述，不让环境变量变成任意来源元数据。"""

    description = _text(env, "METRIC_DESCRIPTION")
    return (("metric.description", description),) if description else ()


def _json(raw: str, name: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError as error:
        # 原文可能含连接串或令牌，只回报错误位置，不回显内容。
        raise WorkerConfigError(f"{ENV_PREFIX}{name} 不是合法 JSON：{error.__class__.__name__}") from None


def _validated_trace_id(value: str) -> str:
    from lingxi.core.ids import is_ulid, new_ulid

    if not value:
        return new_ulid()
    if not is_ulid(value):
        # 不回显收到的值：误接进来的可能是令牌。
        raise WorkerConfigError("LINGXI_WORKER_TRACE_ID 必须是 26 位 Crockford ULID（收到的值不回显）")
    return value


def _turn_timeout(value: str) -> float:
    if not value:
        return DEFAULT_TURN_TIMEOUT_SECONDS
    try:
        seconds = float(value)
    except ValueError as error:
        raise WorkerConfigError("LINGXI_WORKER_TURN_TIMEOUT_SECONDS 必须是正数（秒）") from error
    import math

    if seconds <= 0 or not math.isfinite(seconds) or seconds > TURN_TIMEOUT_HARD_LIMIT_SECONDS:
        # inf / 1e999 会让 asyncio.timeout 永不触发，把本选项要防的永久挂起
        # 原样带回来（终轮 Codex 复查发现）；越过产品硬上限同样在启动期拒绝。
        raise WorkerConfigError(
            "LINGXI_WORKER_TURN_TIMEOUT_SECONDS 必须是正的有限秒数，且不得超过"
            f" {TURN_TIMEOUT_HARD_LIMIT_SECONDS:g} 秒"
        )
    return seconds


def _max_turns(value: str) -> int:
    if not value:
        return DEFAULT_MAX_TURNS
    try:
        turns = int(value)
    except ValueError as error:
        raise WorkerConfigError("LINGXI_WORKER_MAX_TURNS 必须是正整数") from error
    if turns <= 0 or turns > MAX_TURNS_HARD_LIMIT:
        raise WorkerConfigError(
            f"LINGXI_WORKER_MAX_TURNS 必须在 1 到 {MAX_TURNS_HARD_LIMIT} 之间"
        )
    return turns


def _duration(env: Mapping[str, str], name: str, default: float) -> float:
    value = _text(env, name)
    if not value:
        return default
    try:
        seconds = float(value)
    except ValueError as error:
        raise WorkerConfigError(f"{ENV_PREFIX}{name} 必须是正的有限秒数") from error
    import math

    if seconds <= 0 or not math.isfinite(seconds):
        raise WorkerConfigError(f"{ENV_PREFIX}{name} 必须是正的有限秒数")
    return seconds


def _positive_int(
    env: Mapping[str, str], name: str, default: int, *, allow_zero: bool = False
) -> int:
    value = _text(env, name)
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise WorkerConfigError(f"{ENV_PREFIX}{name} 必须是整数") from error
    invalid = parsed < 0 if allow_zero else parsed <= 0
    if invalid:
        raise WorkerConfigError(f"{ENV_PREFIX}{name} 必须是合法的正整数")
    return parsed
