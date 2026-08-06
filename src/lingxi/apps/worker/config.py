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
    audit_input_fields: tuple[str, ...] = ()
    failure_text_markers: tuple[str, ...] = ()
    mcp_servers: Mapping[str, Any] = field(default_factory=dict)
    workspace: str | None = None
    model: str | None = None
    system_prompt: str | None = None


def load_config(env: Mapping[str, str]) -> WorkerConfig:
    """从环境变量构造配置。缺失或不合法时抛 :class:`WorkerConfigError`。"""

    missing = [name for name in ("QUESTION", "READONLY_TOOL") if not _text(env, name)]
    if missing:
        raise WorkerConfigError(
            "缺少必填环境变量：" + "、".join(f"{ENV_PREFIX}{name}" for name in missing)
        )

    return WorkerConfig(
        question=_text(env, "QUESTION") or "",
        read_only_tool=_read_only_tool(env),
        trace_id=_validated_trace_id(_text(env, "TRACE_ID")),
        turn_timeout_seconds=_turn_timeout(_text(env, "TURN_TIMEOUT_SECONDS")),
        audit_input_fields=_names(env, "AUDIT_INPUT_FIELDS"),
        failure_text_markers=_failure_markers(env),
        mcp_servers=_mcp_servers(env),
        workspace=_text(env, "WORKSPACE"),
        model=_text(env, "MODEL"),
        system_prompt=_text(env, "SYSTEM_PROMPT"),
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
        return 600.0
    try:
        seconds = float(value)
    except ValueError as error:
        raise WorkerConfigError("LINGXI_WORKER_TURN_TIMEOUT_SECONDS 必须是正数（秒）") from error
    import math

    if seconds <= 0 or not math.isfinite(seconds):
        # inf / 1e999 会让 asyncio.timeout 永不触发，把本选项要防的永久挂起
        # 原样带回来（终轮 Codex 复查发现）。
        raise WorkerConfigError("LINGXI_WORKER_TURN_TIMEOUT_SECONDS 必须是正的有限秒数")
    return seconds
