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
from dataclasses import dataclass, field
from typing import Any, Mapping

from lingxi.core.execution.input_safety import SAFE_OUTPUT_FALLBACK, WITHHELD_MESSAGE
from lingxi.core.execution.tool_policy import is_well_formed_tool_name
from lingxi.core.ids import new_ulid
from lingxi.core.mcp_naming import QUERY_MCP_SERVER_NAME

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

# 白名单只放行明确确认过的**只读 MCP 工具**（Issue #37 实施范围 2）。要求
# ``mcp__`` 前缀是这条范围的机器可核对形式：Skill、Agent、Task 与任何内置工具都
# 因此落在配置期拒绝分支里，不需要维护一份"禁止配置"的名单。
#
# **不再是"只允许一个"**（Issue #291 P0）：真实问数 MCP 至少注册了 3 个只读工具
# （``list_metrics``/``describe_metric``/``search_dimension``），放行一个、拒绝
# 其余会让每一次真实提问都在 ``PreToolUse`` 被拒——这正是 2026-08-21 那次"每条
# 都有回复、一次真正的查询都没执行"的根因之一。
MCP_TOOL_PREFIX = "mcp__"

# 白名单每一项都必须以这个前缀开头——服务名段与写侧（``adapters/user_environment.py``
# 写 ``.mcp.json`` 的那一侧）共用同一个常量 ``QUERY_MCP_SERVER_NAME``（定义在零依赖的
# ``lingxi.core.mcp_naming``，独立审查见该模块文档），不各自维护一份字符串。见
# ``_read_only_tools`` 的装配期断言：两侧不一致时启动失败关闭，不留到用户提问那一刻
# 才无声降级。
QUERY_MCP_TOOL_PREFIX = f"{MCP_TOOL_PREFIX}{QUERY_MCP_SERVER_NAME}__"

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

# 内测轮内容级采集（Issue #251/#304 批次 3）：两个裸变量名，**不带**
# ``LINGXI_WORKER_`` 前缀——与 ``LINGXI_USER_ENV_ROOT``/
# ``LINGXI_INNERTEST_ROSTER_OPEN_IDS`` 同一惯例，因为"内测轮"是横切概念，不是
# worker 私有配置。完整判定语义见 `_innertest_content_capture` 与
# docs/技术设计/数据库设计.md「保留与删除」内测采集小节。
CONTENT_CAPTURE_FLAG_VAR = "LINGXI_INNERTEST_CONTENT_CAPTURE"
CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VAR = "LINGXI_INNERTEST_CONTENT_CAPTURE_ENVIRONMENT_CONFIRM"
# 第二确认变量要求的**精确**取值。选一句不像布尔值、不像会被误抄的短标签的
# 字面量，是"结构性保证不是文档约定"这条要求在只有环境变量可用时的具体落地：
# 单一开关容易被整份部署文件复制/续用带进不该带进的环境；要求同时命中这个
# 精确字面量，把"部署配置漂移"与"确有其人显式选择开启"的门槛拉开一截。
# `scripts/ci/check_deploy_contract.py` 的 `check_content_capture_prod_guard`
# 断言这个值永不出现在任何 compose 编排文件里（尤其是 deploy/compose.prod.yaml）。
CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VALUE = "stage-innertest-explicit-opt-in"


class WorkerConfigError(ValueError):
    """配置不合法。启动即失败，不留到会话建立之后。"""


@dataclass(frozen=True)
class WorkerConfig:
    """一次受控回合需要的全部输入。"""

    question: str
    read_only_tools: tuple[str, ...]
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
    # 默认提示词文件（2026-08-23 终验事故后补，产品负责人裁定「提示词不进代码、
    # 不进镜像，随时可改、快速验证」）：指向挂载卷上的一个 UTF-8 文本文件，queue
    # 模式**每个任务开始时现读**——编辑该文件后下一条消息即生效，不需要重启容器
    # 或重建镜像。与 ``system_prompt``（进程级固定值）互斥：两个来源同时配置时无法
    # 回答"这一轮到底用的哪份"，启动即失败。与 ``output_safety_canary`` 也互斥：
    # canary 的全部子串不变量在启动期针对**固定**提示词校验（见
    # ``_validate_output_safety_canary``），逐任务变化的文件内容无法在启动期背书。
    # 文件缺失/不可读/超限时该任务**降级为无提示词执行**并留结构化告警——提示词
    # 是行为调优，不是安全屏障（屏障是 PreToolUse 白名单），失败关闭在这里意味着
    # 把整条问数服务押在一个运维随手可改的文件上，得不偿失。
    system_prompt_file: str | None = None
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
    # 用户环境根目录（Epic D 闸⑥）：queue 模式处理每个任务时，按任务的
    # ``user_id`` 读 ``<user_env_root>/<user_id>/.mcp.json``，把解析结果作为这一
    # 次会话专属的 ``mcp_servers``——见 ``apps/worker/service.py``。**不带
    # ``LINGXI_WORKER_`` 前缀**：与 scheduler 侧
    # （``apps/scheduler/config.py`` 的 ``user_env_root``）读同一个裸变量名
    # ``LINGXI_USER_ENV_ROOT``，两个进程指向同一个持久卷挂载点，避免出现"两个
    # 进程各自配了不同目录"的部署漂移。此处仍是**可选**字段（校验姿态照抄
    # scheduler 侧 ``optional_identifier``）：`turn`（一次性受控回合）模式不需要
    # 它；`queue` 模式下这是唯一真正会处理用户任务的路径，因此改由
    # ``apps/worker/cli.py`` 在 ``queue`` 分支单独要求，缺失即启动失败
    # （`EXIT_CONFIG_ERROR`），不是在这里强制必填——否则会连累不需要它的 `turn`
    # 模式与直接构造 ``WorkerConfig`` 的测试/嵌入路径。
    user_env_root: str | None = None
    # 内测轮内容级采集（Issue #251/#304 批次 3）：见 `_innertest_content_capture`
    # 的完整判定说明。默认 False——关闭状态必须可被断言证明，因此这里**不**
    # 提供任何会让默认值意外变真的构造路径（直接构造 WorkerConfig 的测试与
    # 嵌入调用方同样默认关闭，与 loader 路径行为一致）。
    innertest_content_capture_enabled: bool = False
    # 主开关已配置但第二确认变量缺失/不匹配：采集**仍然关闭**，这个字段只用于
    # 让 apps/worker/cli.py 在启动期打一条显眼告警，帮助运维发现"以为开了但
    # 其实结构性没生效"，不参与任何功能判断。
    innertest_content_capture_misconfigured: bool = False

    def __post_init__(self) -> None:
        # canary 的**全部**不变量放在类型自身而不是只放在 load_config（独立审核
        # F7，PR #186 补审 P2-2）：直接构造 WorkerConfig 是文档支持的测试/嵌入
        # 路径，只靠 loader 校验时，绕过 loader 的构造能带着拼错的档位或危险提示
        # 一路跑起来——注入退化成空字符串、canary 永远不触发，验收者会把"配置不
        # 完整"误读成"安全链路又没触发"。两条路径必须同等失败关闭。
        # 提示词文件的两条互斥不变量放在类型自身（理由同下，两条构造路径同等
        # 失败关闭），且**先于** canary 校验：canary 校验会因"canary 没配提示词"
        # 或"提示词与固定文案互为子串"先行报错，把真正的问题（不该同时配文件）
        # 说成别的。措辞用字段名口径，loader 侧另有环境变量口径的报错。
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


def load_config(
    env: Mapping[str, str], *, require_question: bool = True, queue_mode: bool = False
) -> WorkerConfig:
    """从环境变量构造配置。缺失或不合法时抛 :class:`WorkerConfigError`。

    ``queue_mode``（外部独立审查 F3）：queue 模式处理真实用户任务时，
    ``mcp_servers`` 已经改为按任务的 user_id 逐用户读取（Epic D 闸⑥，见
    ``apps/worker/service.py``），``LINGXI_WORKER_MCP_SERVERS``（全进程共用配置）
    只服务没有 user_id 概念的一次性受控回合（``turn`` 模式）。``queue_mode=True``
    时**根本不调用** ``_mcp_servers(env)``——不只是"解析出来但不用"：
    ①即使这个变量被配了非法 JSON，queue 进程也不会因为一个从不会被用到的
    变量而拒绝启动；②合法值也不会被 ``json.loads`` 成 Python 对象、不会有
    一份共享令牌以任何形式驻留在 queue 进程内存里。这比"解析出来、构造好
    WorkerConfig 之后再也不读那个字段"更强：后者仍然会让配置错误的部署在
    queue 模式下启动失败（明明这个模式根本不需要这个变量），也仍然会让令牌
    进程内存里多留一份从未被读取过的副本。
    """

    required_names = ("QUESTION", "READONLY_TOOLS") if require_question else ("READONLY_TOOLS",)
    missing = [name for name in required_names if not _text(env, name)]
    if missing:
        raise WorkerConfigError(
            "缺少必填环境变量：" + "、".join(f"{ENV_PREFIX}{name}" for name in missing)
        )

    system_prompt = _text(env, "SYSTEM_PROMPT")
    system_prompt_file = _system_prompt_file(env)
    # 环境变量口径的互斥前置报错（dataclass 的 __post_init__ 另有字段名口径的
    # 同等检查兜底直接构造路径）：不前置的话，canary 的 loader 校验会抢先用
    # "需要同时配置 SYSTEM_PROMPT"误导运维往错误方向修配置。
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
    content_capture_enabled, content_capture_misconfigured = _innertest_content_capture(env)
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
        user_env_root=_user_env_root(env),
        system_prompt_file=system_prompt_file,
        innertest_content_capture_enabled=content_capture_enabled,
        innertest_content_capture_misconfigured=content_capture_misconfigured,
    )


def _text(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(f"{ENV_PREFIX}{name}")
    if value is None:
        return None
    value = value.strip()
    return value or None


def _read_only_tools(env: Mapping[str, str]) -> tuple[str, ...]:
    """解析多值只读工具白名单（Issue #291 P0）。

    逗号分隔多个**精确名称**；不支持通配符，不支持空白分隔——这两条此前唯一的
    单值实现已经在拒绝分支里验证过（``test_worker_entry.py``），这里延续同一
    条不变量：白名单只接受机器可核对的精确名称，想放行一个新工具必须是一次
    有人复核的范围变更，不是改一个环境变量就能悄悄扩大。

    多段校验依次收窄，报错各自可辨认：①逗号分段里有没有空段（挡住 ``a,,b``、
    多余首尾逗号——配置形状错误，独立审查 codex P1-1）；②每一项是不是合法标识符
    形态（挡住通配符、空格、逗号本身混进单项）；③是不是 ``mcp__`` 前缀（挡住
    Skill/Agent/Task/内置工具）；④是不是 ``QUERY_MCP_TOOL_PREFIX`` 前缀——**这一条
    是 Issue #291 根因 #1 的装配期断言**：与 ``adapters/user_environment.py`` 写进
    ``.mcp.json`` 的服务名不一致时启动失败关闭，不留到用户提问那一刻才发现工具全被
    无声拒绝；⑤前缀之后是不是空的（挡住裸前缀 ``mcp__query__`` 本身，独立审查
    codex P1-1）。
    """

    raw = _text(env, "READONLY_TOOLS") or ""
    if not raw:
        raise WorkerConfigError(
            f"{ENV_PREFIX}READONLY_TOOLS 必须至少包含一个合法工具名（逗号分隔多个，"
            f"例如 {QUERY_MCP_TOOL_PREFIX}list_metrics,{QUERY_MCP_TOOL_PREFIX}describe_metric）"
        )
    # 逐段保留（不先用 ``if part.strip()`` 过滤）：逗号间的空段（``a,,b``）、多余的
    # 首尾逗号（``a,`` / ``,a``）都是配置形状错误，必须响亮拒绝，不能被静默丢弃成
    # "看起来解析成功、其实少了一项"（独立审查 codex P1-1）。
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
    # 前缀之后必须还有具体的工具名：``mcp__query__`` 本身（前缀后为空）虽然通过了
    # 上面三段校验（合法标识符形态、mcp__ 前缀、query__ 前缀），但它不指向任何一个
    # 真实工具，会被 PreToolUse 逐字比对无声拒绝——必须在装配期响亮报错，不能留到
    # 用户提问那一刻才发现白名单里有一条"看似合法、实则永远拒绝"的空项
    # （独立审查 codex P1-1）。
    bare_prefix = [value for value in values if value == QUERY_MCP_TOOL_PREFIX]
    if bare_prefix:
        raise WorkerConfigError(
            f"{ENV_PREFIX}READONLY_TOOLS 不能是裸前缀 {QUERY_MCP_TOOL_PREFIX!r}（前缀后必须跟具体"
            f"的工具名，例如 {QUERY_MCP_TOOL_PREFIX}list_metrics），收到：{bare_prefix!r}"
        )
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


def _user_env_root(env: Mapping[str, str]) -> str | None:
    """读取裸变量 ``LINGXI_USER_ENV_ROOT``（不带 ``LINGXI_WORKER_`` 前缀）。

    校验姿态照抄 ``apps/scheduler/config.py`` 的 ``optional_identifier``——可选、
    去首尾空白、内部不得含空白字符（否则快速失败，且不回显取到的值）；两侧保持
    同一条基线规则，是"同一份部署值给两个进程用"这件事本身的一部分。

    在此之上（外部独立审查 F4）**额外要求绝对且已规范化的路径**：必须以 ``/``
    开头，且 ``posixpath.normpath`` 归一化后与原值逐字节相同——用这一条筛掉
    相对路径、``..`` 路径穿越分量、连续斜杠与多余的尾部斜杠。这个值随后会被
    直接拼进每个用户的家目录路径（``<root>/<user_id>/.mcp.json``，见
    ``adapters/user_mcp_config.py``），不接受任何"看起来像路径但形态不规范"的
    写法——本模块只服务判定期的构造安全，真正的存在性与可读性核对留给
    ``apps/worker/cli.py`` 的 queue 模式启动预检（读到这里就已经知道形态合法，
    但还没碰过文件系统）。
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
    """内测轮内容级采集开关（Issue #251/#304 批次 3），返回
    ``(enabled, misconfigured)``。

    - 主开关（``CONTENT_CAPTURE_FLAG_VAR``）未配置或为空：``(False, False)``
      ——未配置就是未启用，不是"某个开关关着"，是这项能力压根没被提起过。
    - 主开关配置了但不是精确的 ``"1"``：**启动即失败**（与
      ``apps/gateway/config.py`` 的 ``_card_failure_injection`` 同一姿态）——
      错配不是未配，宁可拒绝启动也不静默按未启用处理，否则真想开启却打错值的
      部署会悄悄以为自己开着。不回显收到的值。
    - 主开关为 ``"1"`` 但第二确认变量（``CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VAR``）
      不等于要求的精确字面量（``CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VALUE``）：
      ``(False, True)``——这是"正式环境即使配了也不得生效"在只有环境变量可用、
      且 stage/prod 两侧镜像与编排结构完全相同（无任何既有代码可读的环境判据）
      时的保守方案：单一开关容易随整份部署文件被误续用带进不该带进的环境，
      要求同时命中一个不像会被顺手抄对的精确字面量，把"部署配置漂移"与"确有
      其人显式选择在 stage 开启"的门槛拉开一截。**不让整个进程启动失败**：
      这只是一项可选能力被结构性挡住，worker 仍要正常服务用户问数；
      ``misconfigured=True`` 只用于让 ``apps/worker/cli.py`` 打一条显眼的启动
      期告警。
    - 两者都对：``(True, False)``。

    **已知残余风险（如实登记，不得声称已彻底堵住）**：stage 与生产当前共用
    完全相同的容器镜像与 compose 结构，唯一差异是各服务从哪个不入库的宿主机
    本地 env 文件读取变量（见 deploy/compose.stage.yaml 与 compose.prod.yaml
    头部说明）。如果有人把 stage 的 worker-queue env 文件**整份**复制进生产
    的对应文件，两个变量会一起被带过去，这道双变量确认无法在代码层面识别出
    "这其实是从 stage 抄过来的"。`scripts/ci/check_deploy_contract.py` 的
    `check_content_capture_prod_guard` 断言精确字面量与两个变量名都不出现在
    任何入库的 compose 编排文件里，`deploy/验收前部署配置清单.md` 与
    `deploy/.env.example` 同步登记"生产环境禁止配置"；这些是仓库能提供的最强
    机械保证，真正的最后一道防线仍是部署操作纪律。
    """

    flag = (env.get(CONTENT_CAPTURE_FLAG_VAR) or "").strip()
    if not flag:
        return False, False
    if flag != "1":
        raise WorkerConfigError(f'环境变量 {CONTENT_CAPTURE_FLAG_VAR} 只接受精确值 "1"（不回显收到的值）')
    confirm = (env.get(CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VAR) or "").strip()
    if confirm != CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VALUE:
        return False, True
    return True, False


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
