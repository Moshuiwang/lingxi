"""worker 入口的类型化配置：只从 ``LINGXI_`` 前缀环境变量读一次。

[代码框架「三、横切约定」](../../../../docs/技术设计/代码框架.md)要求配置在 ``apps``
入口一次性读取并构造成类型化对象往下传，``core`` 与 ``adapters`` 不碰 ``os.environ``；
主机、端口、路径、密钥不得硬编码（`V-部署-01`）。

校验刻意放在**构造期**：白名单形态、工具数量这类约束在运行期才发现，意味着一次
本不该发起的会话已经建起来了。
"""

from __future__ import annotations

import json
import posixpath
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from lingxi.apps.worker.session_cleanup import (
    DEFAULT_SESSION_DISK_BUDGET_BYTES,
    DEFAULT_SESSION_DISK_LOW_WATER_RATIO,
    DEFAULT_SESSION_RECLAIM_MIN_AGE_SECONDS,
)
from lingxi.core.execution.input_safety import SAFE_OUTPUT_FALLBACK, WITHHELD_MESSAGE
from lingxi.core.execution.tool_policy import is_well_formed_tool_name
from lingxi.core.ids import new_ulid
from lingxi.core.mcp_naming import QUERY_MCP_SERVER_NAME

ENV_PREFIX = "LINGXI_WORKER_"

# 这是部署配置的默认口径；实际任务值仍从环境变量读取。硬上限是产品为单任务
# 设下的安全边界，越过它必须在启动期拒绝，不能让一次部署带着不确定的成本口径运行。
DEFAULT_MAX_TURNS = 20
MAX_TURNS_HARD_LIMIT = 30
# worker-queue 的并发上限：一次性建连探针的结果不能被当作执行并发上界；
# 生产与 stage 的执行并发固定为 4；这个值同时是直接构造配置与环境变量
# loader 的硬门，避免仅靠 compose/运维评论维持一个可漂移的数字。
DEFAULT_MAX_CONCURRENCY = 4
MAX_CONCURRENCY_HARD_LIMIT = 4
# 这是**业务执行预算**，不是端到端总耗时的承诺：SDK 会话收尾（终态接收、
# 用量回收）另有独立、有界的收尾宽限（见 ``DEFAULT_DRAIN_GRACE_SECONDS``），
# 不计入这个预算，也不因预算耗尽被截断。对外承诺"单任务最多 N 秒"，N 必须
# 是预算加收尾宽限，不是这一个值。
DEFAULT_TURN_TIMEOUT_SECONDS = 600.0
TURN_TIMEOUT_HARD_LIMIT_SECONDS = 900.0
# 收尾宽限：真实链路观测到的固定收尾开销留出充分余量，不让正常收尾撞到这个
# 上限；只有 SDK 断开挂起等病态情况才会触发，触发时报告独立的 ``drain_
# timeout`` 原因码，不与业务墙钟超时混淆。
DEFAULT_DRAIN_GRACE_SECONDS = 30.0
DRAIN_GRACE_HARD_LIMIT_SECONDS = 120.0

# 队列模式下收到 SIGTERM 后，进程级最多再等多久：这是"通知在途回合停止"之后、
# 放弃继续等待、接受该任务留在 running（由未来某次心跳超时回收）之前的总
# 预算。推导见 `scripts/ci/check_deploy_contract.py` 核对 compose 的
# `stop_grace_period` 那一段：检测延迟 + 收尾宽限 + 一次终态写库预算，取整
# 留一点余量而不是精确等式（真正的硬约束由 compose 侧再乘安全系数兜底）。
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 45.0
SHUTDOWN_TIMEOUT_HARD_LIMIT_SECONDS = 300.0

# 白名单只放行明确确认过的**只读 MCP 工具**。要求 ``mcp__`` 前缀是这条范围
# 的机器可核对形式：Skill、Agent、Task 与任何内置工具都因此落在配置期拒绝
# 分支里，不需要维护一份"禁止配置"的名单。**不是"只允许一个"**：真实问数
# MCP 注册了多个只读工具，放行一个、拒绝其余会让每一次真实提问都在
# ``PreToolUse`` 被拒——曾经"每条都有回复、一次真正的查询都没执行"的根因之一。
MCP_TOOL_PREFIX = "mcp__"

# 白名单每一项都必须以这个前缀开头——服务名段与写侧（写 ``.mcp.json`` 的那
# 一侧）共用同一个常量 ``QUERY_MCP_SERVER_NAME``（定义在零依赖的
# ``lingxi.core.mcp_naming``），不各自维护一份字符串。见 ``_read_only_tools``
# 的装配期断言：两侧不一致时启动失败关闭，不留到用户提问那一刻才无声降级。
QUERY_MCP_TOOL_PREFIX = f"{MCP_TOOL_PREFIX}{QUERY_MCP_SERVER_NAME}__"

# 受控验收专用：输出安全 canary 的两个合法档位。合法值集合是验收合同的
# 一部分，不是随口列举——``masked`` 验证「局部遮蔽、业务结论幸存」，
# ``withheld`` 验证「整段拒发、独立 redacted_withheld 终态」。非法值必须
# 启动即失败（失败关闭），与 gateway 的卡片故障注入开关同一纪律。
OUTPUT_SAFETY_CANARY_MODES = ("masked", "withheld")

# masked 档位固定前置的幸存句（由 apps/worker/turn.py 注入）。放在 config 里是
# 因为配置校验必须能拿到它做子串守卫（见 ``_output_safety_canary``）；措辞刻意
# 自述身份，用户侧一旦看到就知道这一轮是受控验收注入，不是真实业务结论。改动
# 此句前先确认它仍不含任何已知敏感模式（系统提示标记、mcp__ 工具名形态等），
# 否则 masked 档位的"幸存句必然幸存"前提会被自己打破。
OUTPUT_SAFETY_CANARY_SURVIVOR_BODY = (
    "受控验收合成正文：本句由输出安全注入夹具生成，保留可展示的业务结论。"
)

# 内测轮内容级采集：两个裸变量名，**不带** ``LINGXI_WORKER_`` 前缀——与
# ``LINGXI_USER_ENV_ROOT``/``LINGXI_INNERTEST_ROSTER_OPEN_IDS`` 同一惯例，
# 因为"内测轮"是横切概念，不是 worker 私有配置。完整判定语义见
# `_innertest_content_capture`。
CONTENT_CAPTURE_FLAG_VAR = "LINGXI_INNERTEST_CONTENT_CAPTURE"
CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VAR = "LINGXI_INNERTEST_CONTENT_CAPTURE_ENVIRONMENT_CONFIRM"
# 第二确认变量要求的**精确**取值：单一开关容易被整份部署文件复制/续用带进
# 不该带进的环境，要求同时命中一个不像会被误抄的字面量，把"部署配置漂移"
# 与"确有其人显式选择开启"的门槛拉开一截；门禁断言这个值不出现在任何入库
# 的 compose 编排文件里。
CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VALUE = "stage-innertest-explicit-opt-in"

# 代码侧的环境判据：两个采集开关变量本身回答不了"现在跑在哪个环境"。这个
# 变量把判据搬回**入库的 compose 文件**：生产 compose 的 `environment:`
# 写死声明、且覆盖 `env_file`，"抄 env 文件"这条路径因此被堵死。语义只朝
# 一个方向收紧：声明为生产则内容采集一律不生效，未声明则维持原有双变量
# 判定，不拦住 worker 启动。
DEPLOY_ENVIRONMENT_VAR = "LINGXI_DEPLOY_ENVIRONMENT"
#: 被认作「这是生产」的取值（大小写与首尾空白不敏感）。写成**元组字面量**是为了
#: 让 `scripts/ci/check_deploy_contract.py` 能用 `ast.literal_eval` 直接读到它，
#: 不 import 业务代码就能核对 compose 里写的值确实在这张表里（拼错是静默的）。
PRODUCTION_ENVIRONMENT_VALUES = ("prod", "production", "生产")

# 文档交付触发机制：默认关闭——关闭时 apps/worker/turn.py 完全不挂 delivery
# MCP 服务，行为与本开关加入之前逐字节一致。校验姿态照抄
# ``_innertest_content_capture`` 的主开关：只接受精确值 ``"1"``，错配按启动
# 即失败处理，不悄悄当作未开启。
DOCUMENT_DELIVERY_ENABLED_VAR = "DOCUMENT_DELIVERY_ENABLED"


class WorkerConfigError(ValueError):
    """配置不合法。启动即失败，不留到会话建立之后。"""


@dataclass(frozen=True)
class WorkerConfig:
    """一次受控回合需要的全部输入。"""

    question: str
    read_only_tools: tuple[str, ...]
    trace_id: str
    # 单回合墙钟上限（业务执行预算）：SDK 传输挂住不发终止消息时，没有它整个
    # 回合会永久等待，连失败报告都出不来。不含收尾宽限。
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
    # 默认提示词文件：指向挂载卷上的一个 UTF-8 文本文件，queue 模式**每个
    # 任务开始时现读**，编辑后下一条消息即生效，不需要重启或重建镜像。与
    # ``system_prompt``、``output_safety_canary`` 都互斥（理由见
    # ``_validate_output_safety_canary``）。文件缺失/不可读/超限时该任务
    # 降级为无提示词执行并留结构化告警——提示词是行为调优，不是安全屏障。
    system_prompt_file: str | None = None
    # 受控验收专用开关：在出口安全约束之前，把已配置的**合成** system
    # prompt 确定性地注入最终正文，使真实 Queue 链路不依赖模型"恰好复述"
    # 提示词就能触发局部遮蔽（masked）或整段拒发（withheld）。默认 ``None``
    # （不注入）；开启时必须同时配置 ``LINGXI_WORKER_SYSTEM_PROMPT``，且只
    # 允许配合合成 canary 提示使用——真实系统提示不进受控验收。
    output_safety_canary: str | None = None
    # CLI 可接收一个已知的指标描述外部文本。真实花名册 / MCP 来源仍由后续
    # 主链路注入；这里不把该配置当作权限或身份事实。
    external_texts: tuple[tuple[str, str], ...] = ()
    worker_id: str = ""
    target_worker_version: str = "stable"
    queue_max_wait_seconds: float = 180.0
    worker_version_unavailable_seconds: float = 180.0
    running_heartbeat_timeout_seconds: float = 90.0
    # 待投递、失败或送达状态不明的投递终态最长保留时间：自 terminal 事件写入
    # 起最长 24 小时未确认即到期，强制收敛为 delivery_expired 并释放话题。
    # **这里不再有对应的配置字段**：这个上限由迁移 0059 的触发器锁定在
    # task_delivery_event.expires_at 上，应用层曾有一个同名字段从未被任何
    # 查询真正读取过，已删除。
    heartbeat_interval_seconds: float = 30.0
    stop_poll_interval_seconds: float = 1.0
    poll_interval_seconds: float = 2.0
    max_auto_retries: int = 1
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    # 队列模式 SIGTERM 优雅停机预算；见上方 DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
    # 的推导注释。一次性 turn 模式不使用这个值。
    shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
    # Agent 会话 JSONL 物理清理：留空时由队列模式入口按
    # ``$HOME/.claude/projects`` 推导默认根目录，见 apps/worker/session_cleanup.py。
    session_root: str | None = None
    session_cleanup_batch_limit: int = 20
    # 会话转录容量回收：定点清理只在 ``/new``、权限刷新等触发点排队时才发生，
    # 正常问数流程一次都不排——没有这一条，转录就在容器的内存盘上单调增长
    # 直到写满。完整取舍见 ``apps/worker/session_cleanup.py`` 模块文档「容量
    # 回收」。预算 ``0`` 表示显式关闭回收，是留给运维保全取证现场的逃生口。
    session_disk_budget_bytes: int = DEFAULT_SESSION_DISK_BUDGET_BYTES
    session_disk_low_water_ratio: float = DEFAULT_SESSION_DISK_LOW_WATER_RATIO
    session_reclaim_min_age_seconds: float = DEFAULT_SESSION_RECLAIM_MIN_AGE_SECONDS
    # 两次容量回收之间的最小间隔：``process_once`` 每 2 秒跑一轮，没必要每轮都去
    # 扫一遍目录（扫描本身是 stat 每个文件，在小机器上不是零成本）。
    session_reclaim_interval_seconds: float = 60.0
    # 用户环境根目录：queue 模式处理每个任务时，按 ``user_id`` 读
    # ``<user_env_root>/<user_id>/.mcp.json`` 作为这次会话专属的
    # ``mcp_servers``。**不带 ``LINGXI_WORKER_`` 前缀**：与 scheduler 侧读
    # 同一个裸变量名。可选字段：`queue` 模式下由 ``apps/worker/cli.py`` 在其
    # 分支单独要求，不在这里强制必填，否则会连累 `turn` 模式与测试路径。
    user_env_root: str | None = None
    # 内测轮内容级采集：见 `_innertest_content_capture` 的完整判定说明。默认
    # False——关闭状态必须可被断言证明，直接构造 WorkerConfig 的测试与嵌入
    # 调用方同样默认关闭，与 loader 路径行为一致。
    innertest_content_capture_enabled: bool = False
    # 主开关已配置但第二确认变量缺失/不匹配：采集**仍然关闭**，这个字段只用于
    # 让 apps/worker/cli.py 在启动期打一条显眼告警，帮助运维发现"以为开了但
    # 其实结构性没生效"，不参与任何功能判断。
    innertest_content_capture_misconfigured: bool = False
    # 文档交付触发机制：默认 False，与 loader 路径行为一致。为真时
    # apps/worker/turn.py 才会挂 delivery MCP 服务——这里只是一个开关位，不
    # 持有工具名字面量（唯一事实来源是 document_delivery.DELIVER_DOCUMENT_
    # TOOL_NAME）。
    document_delivery_enabled: bool = False

    def __post_init__(self) -> None:
        """校验直接构造路径与 loader 路径共用的启动期不变量。"""
        if (
            isinstance(self.max_concurrency, bool)
            or not isinstance(self.max_concurrency, int)
            or not 1 <= self.max_concurrency <= MAX_CONCURRENCY_HARD_LIMIT
        ):
            raise WorkerConfigError(
                f"max_concurrency 必须在 1 到 {MAX_CONCURRENCY_HARD_LIMIT} 之间（产品并发上限）"
            )
        # canary 的**全部**不变量放在类型自身而不是只放在 load_config：直接
        # 构造 WorkerConfig 是文档支持的测试/嵌入路径，只靠 loader 校验时，
        # 绕过 loader 的构造能带着拼错的档位或危险提示一路跑起来。两条路径
        # 必须同等失败关闭。提示词文件的两条互斥不变量同理放在类型自身，且
        # **先于** canary 校验，避免真正的问题被 canary 校验的报错说成别的。
        if self.system_prompt_file and self.system_prompt:
            raise WorkerConfigError(
                "system_prompt_file 与 system_prompt 不得同时配置：两个来源并存时"
                "无法回答「这一轮到底用的哪份提示词」"
            )
        if self.system_prompt_file and self.output_safety_canary is not None:
            raise WorkerConfigError(
                "system_prompt_file 与 output_safety_canary 不得同时配置：canary 的"
                "子串不变量在启动期针对固定提示词校验，逐任务变化的文件内容无法背书"
            )
        _validate_output_safety_canary(
            self.output_safety_canary,
            self.system_prompt,
            mode_label="output_safety_canary",
            prompt_label="system_prompt",
        )


def _require_worker_env_vars(env: Mapping[str, str], *, require_question: bool) -> None:
    required_names = ("QUESTION", "READONLY_TOOLS") if require_question else ("READONLY_TOOLS",)
    missing = [name for name in required_names if not _text(env, name)]
    if missing:
        raise WorkerConfigError(
            "缺少必填环境变量：" + "、".join(f"{ENV_PREFIX}{name}" for name in missing)
        )


def _validate_prompt_source_exclusivity(
    env: Mapping[str, str], *, system_prompt_file: str | None, system_prompt: str | None
) -> None:
    """环境变量口径的互斥前置报错。

    ``WorkerConfig.__post_init__`` 另有字段名口径的同等检查兜底直接构造路径；
    不前置的话，canary 的 loader 校验会抢先用"需要同时配置 SYSTEM_PROMPT"
    误导运维往错误方向修配置。
    """
    if system_prompt_file and system_prompt:
        raise WorkerConfigError(
            f"{ENV_PREFIX}SYSTEM_PROMPT_FILE 与 {ENV_PREFIX}SYSTEM_PROMPT 不得同时配置"
            "：两个来源并存时无法回答「这一轮到底用的哪份提示词」"
        )
    if system_prompt_file and _text(env, "OUTPUT_SAFETY_CANARY"):
        raise WorkerConfigError(
            f"{ENV_PREFIX}SYSTEM_PROMPT_FILE 与 {ENV_PREFIX}OUTPUT_SAFETY_CANARY 不得"
            "同时配置：canary 的子串不变量在启动期针对固定提示词校验，逐任务变化的"
            "文件内容无法背书"
        )


def _build_worker_config(
    env: Mapping[str, str],
    *,
    queue_mode: bool,
    system_prompt: str | None,
    system_prompt_file: str | None,
    content_capture_enabled: bool,
    content_capture_misconfigured: bool,
) -> WorkerConfig:
    return WorkerConfig(
        question=_text(env, "QUESTION") or "",
        read_only_tools=_read_only_tools(env),
        trace_id=_validated_trace_id(_text(env, "TRACE_ID")),
        max_turns=_max_turns(_text(env, "MAX_TURNS")),
        turn_timeout_seconds=_turn_timeout(_text(env, "TURN_TIMEOUT_SECONDS")),
        drain_grace_seconds=_drain_grace(_text(env, "DRAIN_GRACE_SECONDS")),
        audit_input_fields=_names(env, "AUDIT_INPUT_FIELDS"),
        failure_text_markers=_failure_markers(env),
        mcp_servers=({} if queue_mode else _mcp_servers(env)),
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
        running_heartbeat_timeout_seconds=_duration(env, "RUNNING_HEARTBEAT_TIMEOUT_SECONDS", 90.0),
        heartbeat_interval_seconds=_duration(env, "HEARTBEAT_INTERVAL_SECONDS", 30.0),
        stop_poll_interval_seconds=_duration(env, "STOP_POLL_INTERVAL_SECONDS", 1.0),
        poll_interval_seconds=_duration(env, "POLL_INTERVAL_SECONDS", 2.0),
        max_auto_retries=_positive_int(env, "MAX_AUTO_RETRIES", 1, allow_zero=True),
        max_concurrency=_max_concurrency(env),
        shutdown_timeout_seconds=_shutdown_timeout(_text(env, "SHUTDOWN_TIMEOUT_SECONDS")),
        session_root=_text(env, "SESSION_ROOT"),
        session_cleanup_batch_limit=_positive_int(env, "SESSION_CLEANUP_BATCH_LIMIT", 20),
        session_disk_budget_bytes=_positive_int(
            env,
            "SESSION_DISK_BUDGET_BYTES",
            DEFAULT_SESSION_DISK_BUDGET_BYTES,
            allow_zero=True,
        ),
        session_disk_low_water_ratio=_low_water_ratio(env),
        session_reclaim_min_age_seconds=_duration(
            env, "SESSION_RECLAIM_MIN_AGE_SECONDS", DEFAULT_SESSION_RECLAIM_MIN_AGE_SECONDS
        ),
        session_reclaim_interval_seconds=_duration(env, "SESSION_RECLAIM_INTERVAL_SECONDS", 60.0),
        user_env_root=_user_env_root(env),
        system_prompt_file=system_prompt_file,
        innertest_content_capture_enabled=content_capture_enabled,
        innertest_content_capture_misconfigured=content_capture_misconfigured,
        document_delivery_enabled=_document_delivery_enabled(env),
    )


def load_config(
    env: Mapping[str, str], *, require_question: bool = True, queue_mode: bool = False
) -> WorkerConfig:
    """从环境变量构造配置。缺失或不合法时抛 :class:`WorkerConfigError`。

    ``queue_mode=True`` 时**根本不调用** ``_mcp_servers(env)``——queue 模式下
    ``mcp_servers`` 已改为按任务 user_id 逐用户读取，全进程共用的
    ``LINGXI_WORKER_MCP_SERVERS`` 只服务 ``turn`` 模式。不只是"解析出来但不
    用"：非法 JSON 不会拒绝启动，合法值也不会有一份共享令牌驻留进程内存。
    """
    _require_worker_env_vars(env, require_question=require_question)
    system_prompt = _text(env, "SYSTEM_PROMPT")
    system_prompt_file = _system_prompt_file(env)
    _validate_prompt_source_exclusivity(
        env, system_prompt_file=system_prompt_file, system_prompt=system_prompt
    )
    content_capture_enabled, content_capture_misconfigured = _innertest_content_capture(env)
    return _build_worker_config(
        env,
        queue_mode=queue_mode,
        system_prompt=system_prompt,
        system_prompt_file=system_prompt_file,
        content_capture_enabled=content_capture_enabled,
        content_capture_misconfigured=content_capture_misconfigured,
    )


def _text(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(f"{ENV_PREFIX}{name}")
    if value is None:
        return None
    value = value.strip()
    return value or None


def _parse_readonly_tool_values(raw: str) -> tuple[str, ...]:
    """按逗号切分并校验空段、合法标识符形态，返回逐段保留的原始条目。

    逐段保留（不先用 ``if part.strip()`` 过滤）：逗号间的空段（``a,,b``）、
    多余的首尾逗号（``a,`` / ``,a``）都是配置形状错误，必须响亮拒绝，不能
    被静默丢弃成"看起来解析成功、其实少了一项"。
    """
    values = tuple(part.strip() for part in raw.split(","))
    empty_positions = [index + 1 for index, part in enumerate(values) if not part]
    if empty_positions:
        raise WorkerConfigError(
            f"{ENV_PREFIX}READONLY_TOOLS 存在空的逗号分段（第 {empty_positions} 段，从 1 计数，"
            f"含多余的首尾逗号）：配置形状错误按失败关闭处理，不静默丢弃，收到原始值：{raw!r}"
        )
    malformed = [value for value in values if not is_well_formed_tool_name(value)]
    if malformed:
        # 通配符、空格与其它非法字符都会落到这里：白名单只接受精确名称。
        raise WorkerConfigError(
            f"{ENV_PREFIX}READONLY_TOOLS 的每一项都必须是合法工具名（只允许字母、数字、"
            f"下划线、点和连字符），收到不合法项：{malformed!r}"
        )
    return values


def _validate_readonly_tool_prefixes(values: tuple[str, ...]) -> None:
    """确认每一项都落在 ``mcp__query__`` 前缀之内，且前缀后有具体工具名。

    前缀不一致时启动失败关闭，不留到用户提问那一刻才发现工具全被无声拒绝；
    裸前缀（前缀后为空）不指向任何真实工具，同样在装配期响亮报错。
    """
    not_mcp = [value for value in values if not value.startswith(MCP_TOOL_PREFIX)]
    if not_mcp:
        raise WorkerConfigError(
            f"{ENV_PREFIX}READONLY_TOOLS 只能是以 {MCP_TOOL_PREFIX} 开头的只读 MCP 工具；"
            f"Skill、Agent、Task 和内置工具都不在本切片范围内，收到：{not_mcp!r}"
        )
    mismatched = [value for value in values if not value.startswith(QUERY_MCP_TOOL_PREFIX)]
    if mismatched:
        raise WorkerConfigError(
            f"{ENV_PREFIX}READONLY_TOOLS 的工具前缀必须是 {QUERY_MCP_TOOL_PREFIX!r}"
            f"（与用户环境 .mcp.json 的 MCP 服务名 {QUERY_MCP_SERVER_NAME!r} 一致，"
            "见 lingxi.core.mcp_naming 的 QUERY_MCP_SERVER_NAME），"
            f"收到前缀不匹配的工具名：{mismatched!r}"
        )
    bare_prefix = [value for value in values if value == QUERY_MCP_TOOL_PREFIX]
    if bare_prefix:
        raise WorkerConfigError(
            f"{ENV_PREFIX}READONLY_TOOLS 不能是裸前缀 {QUERY_MCP_TOOL_PREFIX!r}（前缀后必须跟具体"
            f"的工具名，例如 {QUERY_MCP_TOOL_PREFIX}list_metrics），收到：{bare_prefix!r}"
        )


def _read_only_tools(env: Mapping[str, str]) -> tuple[str, ...]:
    """解析多值只读工具白名单。

    逗号分隔多个**精确名称**；不支持通配符，不支持空白分隔：白名单只接受
    机器可核对的精确名称，想放行一个新工具必须是一次有人复核的范围变更，
    不是改一个环境变量就能悄悄扩大。校验依次收窄，见
    :func:`_parse_readonly_tool_values` 与 :func:`_validate_readonly_tool_prefixes`。
    """
    raw = _text(env, "READONLY_TOOLS") or ""
    if not raw:
        raise WorkerConfigError(
            f"{ENV_PREFIX}READONLY_TOOLS 必须至少包含一个合法工具名（逗号分隔多个，"
            f"例如 {QUERY_MCP_TOOL_PREFIX}list_metrics,{QUERY_MCP_TOOL_PREFIX}describe_metric）"
        )
    values = _parse_readonly_tool_values(raw)
    _validate_readonly_tool_prefixes(values)
    # 去重且保序：同一个工具名在环境变量里写重不该产生"白名单有两条"的假象。
    return tuple(dict.fromkeys(values))


def _names(env: Mapping[str, str], name: str) -> tuple[str, ...]:
    raw = _text(env, name)
    if not raw:
        return ()
    names = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not names:
        raise WorkerConfigError(f"{ENV_PREFIX}{name} 只有分隔符，没有任何名称")
    return names


def _failure_markers(env: Mapping[str, str]) -> tuple[str, ...]:
    """已登记的业务失败措辞。

    默认为空——外部 MCP 用什么措辞表达业务失败必须先从真实回执确认再登记，
    猜错的后果是把失败写成成功（`V-执行-06`）。
    """
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


def _user_env_root(env: Mapping[str, str]) -> str | None:
    """读取裸变量 ``LINGXI_USER_ENV_ROOT``（不带 ``LINGXI_WORKER_`` 前缀）。

    校验姿态照抄 scheduler 侧的 ``optional_identifier``——可选、去首尾空白、
    内部不得含空白字符；两侧保持同一条基线规则，是"同一份部署值给两个进程
    用"的一部分。额外要求绝对且已规范化的路径：必须以 ``/`` 开头，且
    ``posixpath.normpath`` 归一化后与原值逐字节相同，筛掉相对路径、``..``
    穿越、连续斜杠与多余尾部斜杠——这个值会被直接拼进每个用户的家目录路径，
    真正的存在性与可读性核对留给 queue 模式启动预检。
    """
    value = (env.get("LINGXI_USER_ENV_ROOT") or "").strip()
    if not value:
        return None
    if any(character.isspace() for character in value):
        raise WorkerConfigError("环境变量 LINGXI_USER_ENV_ROOT 不得包含空白字符（不回显取到的值）")
    if not value.startswith("/") or posixpath.normpath(value) != value:
        # 不回显收到的值：形态错误的路径本身不敏感，但保持与本文件其它校验
        # 同一条"误接进来的可能是别的东西"纪律，不因为这一条是路径就破例。
        raise WorkerConfigError(
            "环境变量 LINGXI_USER_ENV_ROOT 必须是绝对且已规范化的路径"
            "（不含 `..`、`.`、连续斜杠或多余的尾部斜杠；不回显取到的值）"
        )
    return value


def _innertest_content_capture(env: Mapping[str, str]) -> tuple[bool, bool]:
    """内测轮内容级采集开关，返回 ``(enabled, misconfigured)``。

    主开关未配置——``(False, False)``；配置了但不是精确的 ``"1"``——启动即
    失败；环境自称生产（判定在第二确认**之前**）或第二确认变量不匹配——都是
    ``(False, True)``，只结构性挡住这项可选能力、不拦住 worker 启动；两者
    都对且非生产——``(True, False)``。**已知残余（未彻底堵住）**：把 stage
    的 env 文件整份复制进生产会带走两个变量，生产 compose 声明覆盖
    env_file 已堵死这条路径，最后防线仍是部署操作纪律。
    """
    flag = (env.get(CONTENT_CAPTURE_FLAG_VAR) or "").strip()
    if not flag:
        return False, False
    if flag != "1":
        raise WorkerConfigError(
            f'环境变量 {CONTENT_CAPTURE_FLAG_VAR} 只接受精确值 "1"（不回显收到的值）'
        )
    if declares_production(env):
        # 代码侧兜底：环境自称是生产，采集一律不生效，两个变量配得再对也
        # 不行。misconfigured=True 让调用方打一条显眼的启动告警，但不阻止
        # 进程启动（采集是旁路能力）。
        return False, True
    confirm = (env.get(CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VAR) or "").strip()
    if confirm != CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VALUE:
        return False, True
    return True, False


def declares_production(env: Mapping[str, str]) -> bool:
    """环境是否**自称**生产（``LINGXI_DEPLOY_ENVIRONMENT``）。

    只回答"有没有明确声明是生产"，不猜：没配、配空、配成别的值一律返回 ``False``。
    这不是"检测"生产（镜像与编排两侧完全相同，检测不出来），是让部署**声明**自己
    是谁，并把这份声明放在入库的 compose 文件里，使它不能被一份抄来的 env 文件覆盖。
    """
    return (
        env.get(DEPLOY_ENVIRONMENT_VAR) or ""
    ).strip().casefold() in PRODUCTION_ENVIRONMENT_VALUES


def _document_delivery_enabled(env: Mapping[str, str]) -> bool:
    """文档交付触发开关。

    未配置或为空：``False``——未配置就是未启用。配置了但不是精确的
    ``"1"``：启动即失败（与 ``_innertest_content_capture`` 的主开关同一
    姿态）——错配不是未配。
    """
    flag = (env.get(f"{ENV_PREFIX}{DOCUMENT_DELIVERY_ENABLED_VAR}") or "").strip()
    if not flag:
        return False
    if flag != "1":
        raise WorkerConfigError(
            f'{ENV_PREFIX}{DOCUMENT_DELIVERY_ENABLED_VAR} 只接受精确值 "1"（不回显收到的值）'
        )
    return True


def _system_prompt_file(env: Mapping[str, str]) -> str | None:
    """读取 ``LINGXI_WORKER_SYSTEM_PROMPT_FILE``（默认提示词文件路径）。

    形态校验照抄 ``_user_env_root``：可选、不得含空白、必须是绝对且已规范化的
    路径（挡掉相对路径、``..`` 穿越、连续斜杠）。只校验形态不碰文件系统——
    文件此刻不存在是合法状态（运维可以先起进程后放文件），存在性与可读性在
    每个任务开始时现读现判（见 ``apps/worker/service.py``），读不到就该任务
    降级为无提示词执行并留告警，不影响进程存活。
    """
    value = _text(env, "SYSTEM_PROMPT_FILE")
    if not value:
        return None
    if any(character.isspace() for character in value):
        raise WorkerConfigError(
            f"{ENV_PREFIX}SYSTEM_PROMPT_FILE 不得包含空白字符（不回显取到的值）"
        )
    if not value.startswith("/") or posixpath.normpath(value) != value:
        raise WorkerConfigError(
            f"{ENV_PREFIX}SYSTEM_PROMPT_FILE 必须是绝对且已规范化的路径"
            "（不含 `..`、`.`、连续斜杠或多余的尾部斜杠；不回显取到的值）"
        )
    return value


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
    """输出安全 canary 的全部启动期不变量。

    由 ``WorkerConfig.__post_init__`` 与 ``load_config`` 共用，两条路径同等
    失败关闭；``*_label`` 只决定错误文案里指向哪一侧的名字（环境变量名或
    字段名），不改变任何判定。失败关闭的三条：非法档位、"开着 canary 却
    没有 system prompt"（注入退化成空字符串，canary 永远不触发）、以及与
    受保护文本互为子串。
    """
    if canary is None:
        return
    if canary not in OUTPUT_SAFETY_CANARY_MODES:
        # 不回显收到的值：误接进来的可能是口令或提示词原文。
        raise WorkerConfigError(
            f"{mode_label} 只允许 " + " / ".join(OUTPUT_SAFETY_CANARY_MODES) + "（收到的值不回显）"
        )
    if not system_prompt:
        raise WorkerConfigError(
            f"{mode_label} 需要同时配置 {prompt_label}（合成 canary 提示是注入内容的唯一来源）"
        )
    # 子串守卫，**双向**：合成提示是受保护文本的子串会让出口约束的终态自检
    # 抛异常，把"总是返回一份报告"的契约炸掉；受保护文本出现在合成提示之中
    # 会让固定幸存句/文案自身被派生成禁词遮蔽，masked 滑进 withheld。两个
    # 方向都在启动期确定性拒绝，比运行期每回合炸更符合失败关闭。
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
        raise WorkerConfigError(
            f"{ENV_PREFIX}{name} 不是合法 JSON：{error.__class__.__name__}"
        ) from None


def _validated_trace_id(value: str) -> str:
    from lingxi.core.ids import is_ulid, new_ulid

    if not value:
        return new_ulid()
    if not is_ulid(value):
        # 不回显收到的值：误接进来的可能是令牌。
        raise WorkerConfigError(
            "LINGXI_WORKER_TRACE_ID 必须是 26 位 Crockford ULID（收到的值不回显）"
        )
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
        # 原样带回来；越过产品硬上限同样在启动期拒绝。
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
        raise WorkerConfigError(f"LINGXI_WORKER_MAX_TURNS 必须在 1 到 {MAX_TURNS_HARD_LIMIT} 之间")
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


def _max_concurrency(env: Mapping[str, str]) -> int:
    """读取 worker-queue 并发并在入口钉住产品硬上限。

    4 是已批准的部署合同，建连探针的结果不能被当作执行并发上界。
    ``WorkerConfig.__post_init__`` 还会校验直接构造路径，避免绕过 loader
    后带着更大的并发值进入消费循环。
    """
    concurrency = _positive_int(env, "MAX_CONCURRENCY", DEFAULT_MAX_CONCURRENCY)
    if concurrency > MAX_CONCURRENCY_HARD_LIMIT:
        raise WorkerConfigError(
            f"{ENV_PREFIX}MAX_CONCURRENCY 不得超过 {MAX_CONCURRENCY_HARD_LIMIT}"
        )
    return concurrency


def _low_water_ratio(env: Mapping[str, str]) -> float:
    """会话转录容量回收的低水位比例：``(0, 1]`` 之间的小数。

    ``1.0`` 合法（等价于"删到刚好等于预算"）；``0`` 与负数不合法——那会让一次
    回收把目录清空，把"容量回收"变成"全删"。上界同样封死：大于 1 的比例意味着
    低水位高于预算本身，回收永远达不到目标、每一轮都白扫一遍目录。
    """
    value = _text(env, "SESSION_DISK_LOW_WATER_RATIO")
    if not value:
        return DEFAULT_SESSION_DISK_LOW_WATER_RATIO
    try:
        ratio = float(value)
    except ValueError as error:
        raise WorkerConfigError(
            f"{ENV_PREFIX}SESSION_DISK_LOW_WATER_RATIO 必须是 (0, 1] 之间的小数"
        ) from error
    if not 0 < ratio <= 1:
        raise WorkerConfigError(
            f"{ENV_PREFIX}SESSION_DISK_LOW_WATER_RATIO 必须是 (0, 1] 之间的小数"
        )
    return ratio
