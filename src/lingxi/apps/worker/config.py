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

from lingxi.core.execution.input_safety import SAFE_OUTPUT_FALLBACK, WITHHELD_MESSAGE
from lingxi.core.execution.tool_policy import is_well_formed_tool_name
from lingxi.core.ids import new_ulid

ENV_PREFIX = "LINGXI_WORKER_"

# 这是部署配置的默认口径；实际任务值仍从环境变量读取。硬上限是产品为单任务
# 设下的安全边界，越过它必须在启动期拒绝，不能让一次部署带着不确定的成本口径运行。
DEFAULT_MAX_TURNS = 20
MAX_TURNS_HARD_LIMIT = 30
# 这是**业务执行预算**，不是端到端总耗时的承诺（#143，产品负责人 2026-08-13
# 拍板）：SDK 会话收尾（终态接收、用量回收）另有独立、有界的收尾宽限
# （见 ``DEFAULT_DRAIN_GRACE_SECONDS``），不计入这个预算，也不因预算耗尽被
# 截断。对外如果要承诺"单任务最多 N 秒"，N 必须是预算加收尾宽限，不是这一个值。
DEFAULT_TURN_TIMEOUT_SECONDS = 600.0
TURN_TIMEOUT_HARD_LIMIT_SECONDS = 900.0
# 收尾宽限：真实链路观测到的固定收尾开销约 6 秒（#143），默认值留出充分余量，
# 不让正常收尾撞到这个上限；只有 SDK 断开挂起等病态情况才会触发，触发时报告
# 独立的 ``drain_timeout`` 原因码，不与业务墙钟超时混淆。
DEFAULT_DRAIN_GRACE_SECONDS = 30.0
DRAIN_GRACE_HARD_LIMIT_SECONDS = 120.0

# 队列模式下收到 SIGTERM 后，进程级最多再等多久（Issue #153）：这是"通知在途回合
# 停止"之后、放弃继续等待、接受该任务留在 running（由未来某次心跳超时回收）之前
# 的总预算。derivation（`scripts/ci/check_deploy_contract.py` 按同一模型核对
# compose 的 `stop_grace_period`）：`stop_poll_interval_seconds` 默认 1.0s（`/stop`
# 检测延迟）+ `DEFAULT_DRAIN_GRACE_SECONDS` 30.0s（SDK 收尾宽限，独立于业务墙钟）
# + 一次终态写库的预算（按连接工厂合法覆盖上界 `MAX_TIMEOUT_SECONDS=5` 建模：
# 5 + 2×5 = 15s）= 46s，取整为 45s 留一点余量而不是精确等式（真正的硬约束由
# compose 侧再乘 1.5 安全系数兜底）。
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 45.0
SHUTDOWN_TIMEOUT_HARD_LIMIT_SECONDS = 300.0

# 本切片只允许一个明确确认过的**只读 MCP 工具**（Issue #37 实施范围 2）。要求
# ``mcp__`` 前缀是这条范围的机器可核对形式：Skill、Agent、Task 与任何内置工具都
# 因此落在配置期拒绝分支里，不需要维护一份"禁止配置"的名单。
MCP_TOOL_PREFIX = "mcp__"

# S-A-07 受控验收专用（Issue #142 验收缺口）：输出安全 canary 的两个合法档位。
# 合法值集合是验收合同的一部分，不是随口列举——``masked`` 验证「局部遮蔽、业务
# 结论幸存」，``withheld`` 验证「整段拒发、独立 redacted_withheld 终态」。非法值
# 必须启动即失败（失败关闭），不允许一个拼错的值悄悄放行（与 gateway 的
# ``LINGXI_GATEWAY_CARD_FAILURE_INJECT`` 同一纪律，PR #183 先例）。
OUTPUT_SAFETY_CANARY_MODES = ("masked", "withheld")

# masked 档位固定前置的幸存句（由 apps/worker/turn.py 注入）。放在 config 里是
# 因为配置校验必须能拿到它做子串守卫（见 ``_output_safety_canary``）；措辞刻意
# 自述身份，用户侧一旦看到就知道这一轮是受控验收注入，不是真实业务结论。改动
# 此句前先确认它仍不含任何已知敏感模式（系统提示标记、mcp__ 工具名形态等），
# 否则 masked 档位的"幸存句必然幸存"前提会被自己打破。
OUTPUT_SAFETY_CANARY_SURVIVOR_BODY = "受控验收合成正文：本句由输出安全注入夹具生成，保留可展示的业务结论。"


class WorkerConfigError(ValueError):
    """配置不合法。启动即失败，不留到会话建立之后。"""


@dataclass(frozen=True)
class WorkerConfig:
    """一次受控回合需要的全部输入。"""

    question: str
    read_only_tool: str
    trace_id: str
    # 单回合墙钟上限（业务执行预算）：SDK 传输挂住不发终止消息时，没有它整个
    # 回合会永久等待，连失败报告都出不来（Codex 复查发现）。不含收尾宽限。
    turn_timeout_seconds: float
    # 直接构造配置的测试与嵌入调用方沿用旧接口时仍使用同一安全默认值；正式入口
    # 通过 load_config 显式校验并传入部署值。
    max_turns: int = DEFAULT_MAX_TURNS
    drain_grace_seconds: float = DEFAULT_DRAIN_GRACE_SECONDS
    audit_input_fields: tuple[str, ...] = ()
    failure_text_markers: tuple[str, ...] = ()
    mcp_servers: Mapping[str, Any] = field(default_factory=dict)
    workspace: str | None = None
    model: str | None = None
    system_prompt: str | None = None
    # S-A-07 受控验收专用开关（Issue #142 验收缺口，#154 r17 未通过后补）：在
    # 出口安全约束之前，把已配置的**合成** system prompt 确定性地注入最终正文，
    # 使真实 Queue 链路不依赖模型"恰好复述"提示词就能触发局部遮蔽（masked）或
    # 整段拒发（withheld）。默认 ``None``（不注入，报告与本开关加入之前逐字节
    # 一致）；开启时必须同时配置 ``LINGXI_WORKER_SYSTEM_PROMPT``（注入内容的
    # 唯一来源），且只允许配合合成 canary 提示使用——真实系统提示不进受控验收。
    output_safety_canary: str | None = None
    # #93 walking skeleton：CLI 可接收一个已知的指标描述外部文本。真实花名册 / MCP
    # 来源仍由后续主链路注入；这里不把该配置当作权限或身份事实。
    external_texts: tuple[tuple[str, str], ...] = ()
    worker_id: str = ""
    target_worker_version: str = "stable"
    queue_max_wait_seconds: float = 180.0
    worker_version_unavailable_seconds: float = 180.0
    running_heartbeat_timeout_seconds: float = 90.0
    # 待投递、失败或送达状态不明的投递终态最长保留时间（Issue #151 状态合同第 8
    # 条）：自 terminal 事件写入起最长 24 小时未确认 platform_received 即到期，
    # 强制收敛为 delivery_expired 并释放话题。**这里不再有对应的配置字段**：这个
    # 24 小时上限由迁移 0059 的触发器锁定在 task_delivery_event.expires_at 上，
    # PostgresTaskQueue.expire_undelivered_terminals 直接读那一列；应用层曾经有
    # 一个 delivery_expiry_seconds / DELIVERY_EXPIRY_SECONDS 可以让这个窗口漂移到
    # 数据库约束之外，且从未被任何查询真正读取过，已删除（内审 P2-1）。
    heartbeat_interval_seconds: float = 30.0
    stop_poll_interval_seconds: float = 1.0
    poll_interval_seconds: float = 2.0
    max_auto_retries: int = 1
    max_concurrency: int = 16
    # 队列模式 SIGTERM 优雅停机预算（Issue #153）；见上方 DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
    # 的推导注释。一次性 turn 模式不使用这个值。
    shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
    # Agent 会话 JSONL 物理清理（Issue #153）：留空时由队列模式入口按
    # ``$HOME/.claude/projects`` 推导默认根目录，见 apps/worker/session_cleanup.py。
    session_root: str | None = None
    session_cleanup_batch_limit: int = 20

    def __post_init__(self) -> None:
        # canary 的**全部**不变量放在类型自身而不是只放在 load_config（独立审核
        # F7，PR #186 补审 P2-2）：直接构造 WorkerConfig 是文档支持的测试/嵌入
        # 路径，只靠 loader 校验时，绕过 loader 的构造能带着拼错的档位或危险提示
        # 一路跑起来——注入退化成空字符串、canary 永远不触发，验收者会把"配置不
        # 完整"误读成"安全链路又没触发"。两条路径必须同等失败关闭。
        _validate_output_safety_canary(
            self.output_safety_canary,
            self.system_prompt,
            mode_label="output_safety_canary",
            prompt_label="system_prompt",
        )


def load_config(env: Mapping[str, str], *, require_question: bool = True) -> WorkerConfig:
    """从环境变量构造配置。缺失或不合法时抛 :class:`WorkerConfigError`。"""

    required_names = ("QUESTION", "READONLY_TOOL") if require_question else ("READONLY_TOOL",)
    missing = [name for name in required_names if not _text(env, name)]
    if missing:
        raise WorkerConfigError(
            "缺少必填环境变量：" + "、".join(f"{ENV_PREFIX}{name}" for name in missing)
        )

    system_prompt = _text(env, "SYSTEM_PROMPT")
    return WorkerConfig(
        question=_text(env, "QUESTION") or "",
        read_only_tool=_read_only_tool(env),
        trace_id=_validated_trace_id(_text(env, "TRACE_ID")),
        max_turns=_max_turns(_text(env, "MAX_TURNS")),
        turn_timeout_seconds=_turn_timeout(_text(env, "TURN_TIMEOUT_SECONDS")),
        drain_grace_seconds=_drain_grace(_text(env, "DRAIN_GRACE_SECONDS")),
        audit_input_fields=_names(env, "AUDIT_INPUT_FIELDS"),
        failure_text_markers=_failure_markers(env),
        mcp_servers=_mcp_servers(env),
        workspace=_text(env, "WORKSPACE"),
        model=_text(env, "MODEL"),
        system_prompt=system_prompt,
        output_safety_canary=_output_safety_canary(env, system_prompt=system_prompt),
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
        shutdown_timeout_seconds=_shutdown_timeout(_text(env, "SHUTDOWN_TIMEOUT_SECONDS")),
        session_root=_text(env, "SESSION_ROOT"),
        session_cleanup_batch_limit=_positive_int(env, "SESSION_CLEANUP_BATCH_LIMIT", 20),
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


def _validate_output_safety_canary(
    canary: str | None,
    system_prompt: str | None,
    *,
    mode_label: str,
    prompt_label: str,
) -> None:
    """输出安全 canary 的全部启动期不变量（S-A-07 / Issue #142）。

    由 ``WorkerConfig.__post_init__`` 与 ``load_config`` 共用，两条路径同等失败
    关闭（PR #186 补审 P2-2）；``*_label`` 只决定错误文案里指向哪一侧的名字
    （环境变量名或字段名），不改变任何判定。

    失败关闭的三条：非法档位（拼写错误悄悄放行的经典形状）、"开着 canary 却没有
    system prompt"（注入退化成空字符串、canary 永远不触发，验收者会把"配置不完整"
    误读成"安全链路又没触发"，即 r17 的原样重演）、以及与受保护文本互为子串。
    """

    if canary is None:
        return
    if canary not in OUTPUT_SAFETY_CANARY_MODES:
        # 不回显收到的值（独立审核 F9，同 _validated_trace_id 与 gateway 卡片
        # 注入开关的既有纪律）：误接进来的可能是口令或提示词原文。
        raise WorkerConfigError(
            f"{mode_label} 只允许 "
            + " / ".join(OUTPUT_SAFETY_CANARY_MODES)
            + "（收到的值不回显）"
        )
    if not system_prompt:
        raise WorkerConfigError(
            f"{mode_label} 需要同时配置 {prompt_label}"
            "（合成 canary 提示是注入内容的唯一来源）"
        )
    # 子串守卫，**双向**（独立审核 F2 实证复现 + PR #186 补审 P2-3）：
    # - 合成提示是受保护文本的子串：出口约束的终态自检
    #   ``_ensure_terminal_text_is_safe`` 会在 withheld 分支抛 ``InputSafetyError``，
    #   把"总是返回一份报告"的契约炸掉；
    # - 受保护文本出现在合成提示之中：出口约束会按结构性边界把 system prompt 切成
    #   片段当禁词，于是幸存句 / 固定文案自身被派生成禁词——masked 档位的固定幸存句
    #   会被遮蔽掉，命中区间之外再无真实内容，masked 滑进 withheld；落到终态文案上
    #   同样让自检失败。构造出这个形状不需要恶意，一段多行合成提示中间夹一行幸存句
    #   就够了。
    # 两个方向都在启动期确定性拒绝，比运行期每回合炸更符合失败关闭。
    for protected_text, label in (
        (WITHHELD_MESSAGE, "withheld 固定文案"),
        (SAFE_OUTPUT_FALLBACK, "空产出兜底文案"),
        (OUTPUT_SAFETY_CANARY_SURVIVOR_BODY, "masked 幸存句"),
    ):
        if system_prompt in protected_text or protected_text in system_prompt:
            raise WorkerConfigError(
                f"{prompt_label} 不得与{label}互为子串（两个方向都不行）：canary 开启时"
                "它会让安全终态自检失败，或让固定幸存句被自己派生出的禁词遮蔽、masked "
                "滑进 withheld。请换一段互不重叠的多句合成提示（收到的值不回显）"
            )


def _output_safety_canary(env: Mapping[str, str], *, system_prompt: str | None) -> str | None:
    """读取输出安全 canary 档位，并以环境变量口径给出报错。"""

    value = _text(env, "OUTPUT_SAFETY_CANARY")
    _validate_output_safety_canary(
        value,
        system_prompt,
        mode_label=f"{ENV_PREFIX}OUTPUT_SAFETY_CANARY",
        prompt_label=f"{ENV_PREFIX}SYSTEM_PROMPT",
    )
    return value


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


def _drain_grace(value: str) -> float:
    if not value:
        return DEFAULT_DRAIN_GRACE_SECONDS
    try:
        seconds = float(value)
    except ValueError as error:
        raise WorkerConfigError("LINGXI_WORKER_DRAIN_GRACE_SECONDS 必须是正数（秒）") from error
    import math

    if seconds <= 0 or not math.isfinite(seconds) or seconds > DRAIN_GRACE_HARD_LIMIT_SECONDS:
        raise WorkerConfigError(
            "LINGXI_WORKER_DRAIN_GRACE_SECONDS 必须是正的有限秒数，且不得超过"
            f" {DRAIN_GRACE_HARD_LIMIT_SECONDS:g} 秒"
        )
    return seconds


def _shutdown_timeout(value: str | None) -> float:
    if not value:
        return DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
    try:
        seconds = float(value)
    except ValueError as error:
        raise WorkerConfigError(
            "LINGXI_WORKER_SHUTDOWN_TIMEOUT_SECONDS 必须是正数（秒）"
        ) from error
    import math

    if seconds <= 0 or not math.isfinite(seconds) or seconds > SHUTDOWN_TIMEOUT_HARD_LIMIT_SECONDS:
        raise WorkerConfigError(
            "LINGXI_WORKER_SHUTDOWN_TIMEOUT_SECONDS 必须是正的有限秒数，且不得超过"
            f" {SHUTDOWN_TIMEOUT_HARD_LIMIT_SECONDS:g} 秒"
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
