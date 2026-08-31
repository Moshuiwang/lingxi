"""内测每日通报：从任务队列与投递终态聚合出的统计级报告——纯计算与渲染层（Issue #303 S-O-01）。

**这是全仓库唯一渲染「内测每日通报」正文的地方。** 本模块无网络、无数据库依赖：
外部数据一律由调用方（`adapters/postgres_daily_report.py` 读、`apps/scheduler/
daily_report.py` 编排）作为参数传入，符合[代码框架](../../../docs/技术设计/代码框架.md)
「二、三层之间的 import 规则」对 `core/` 的约束。

## 数据从哪来、为什么有的段落曾经恒为「不可判定」

#303 的通报字段清单要求七类统计：活跃用户与逐用户任务量、成功/失败/超时/停止分布、
失败分类 Top、时延分布（对照 #296 的 262 秒基线）、token 用量、守卫触发与拒绝计数、
投递结果分布。核对现有数据库设计（`docs/技术设计/数据库设计.md` 第二节）后发现：
**`task` 表与 `task_delivery_event` 表能撑起前面五类里的大部分**（状态、
`error_kind` 失败分类、`started_at`/`ended_at` 时延、`platform_message_kind`/
`platform_received_at`/`expires_at` 投递结果）。

**token 用量与 PreToolUse 拒绝计数曾经从未落库**（#303 首版）——它们只存在于
worker 进程自己的结构化日志行 `worker.task.terminal`（`resources.usage`、
`audit.call_count`/`audit.denied_count`，见 `src/lingxi/apps/worker/service.py`），
scheduler 与 worker 是两个独立部署的进程、不共享文件系统或任何日志聚合系统
（`代码框架.md` 三：「结构化输出到 stdout/stderr，不写日志文件、不自行轮转」），
scheduler 当时没有任何代码路径能读到 worker 的这两个字段，因此这两段在当时的
架构下**恒为**「不可判定」。**Issue #303/#304 批次 4 起补上这个缺口**：迁移
``0070`` 给 ``task`` 表新增 ``token_usage``/``guard_denied_count`` 两列，
``apps/worker/service.py`` 的终态收口点（唯一收口点 ``_finish_terminal``）与
写终态事件同一事务落库；这两段因此改为**真实聚合**，不再结构性地不可判定。

**"不可判定"没有整段消失，含义变窄了**：改造后仍然可能出现"这一窗口内的段落
不可判定"——(1) 适配器查询本身失败（数据库暂时不可达等，走 :func:`_fetch`
既有的单段失败路径，与其余五段同一条纪律）；(2) 窗口内**存在**任务，但这两个
新列**全部**是 ``NULL``（例如窗口内的任务全部产生于迁移 ``0070`` 落地之前，
或该任务从未真正跑过一次回合、结构性地没有可落库的数字——见
:func:`_report_guard_denied_count`/:func:`_report_token_usage` 在
``apps/worker/service.py`` 里的文档）。**"NULL 行归入不可判定、不静默计为零"
是逐行纪律，不是逐段纪律**：一个窗口里部分任务有值、部分任务是 ``NULL`` 时，
本模块只对有值的任务求和，并把"多少个任务缺这个字段"作为一个独立的、显式的
数字一并呈现（:class:`PartialCount`/:class:`TokenUsageStats` 的
``uncovered_tasks``），不悄悄把 ``NULL`` 当 0 揉进总数、也不因为有缺失就让
整段变成「不可判定」——那会把"部分已知"错误地降级成"完全未知"，丢失已经拿到
的真实信息。只有窗口内**一个任务都没有可用值**时（``covered_tasks == 0`` 且
``uncovered_tasks > 0``），才整段判「不可判定」，理由见
:data:`RESOURCE_USAGE_ALL_NULL_REASON`/:data:`DENIED_COUNT_ALL_NULL_REASON`——
两列全部 ``NULL`` 有三类真实成因，不止"迁移 0070 之前"与"从未真正进入过执行
回合"两种：**窗口内的任务此刻可能全部仍在排队或运行中**，还没有走到终态
写入那一步（这两列只在终态收口点落库，见 ``apps/worker/service.py`` 的
``_report_guard_denied_count``/``_report_token_usage`` 文档），这是最常见的
一种、且会随任务陆续收口而自然消失，与另外两种"结构性、不会自愈"的成因
性质不同（批次 4 opus 审查 P3-3 补齐，此前两个原因字符串都漏了这一种，读者
可能误以为窗口内的任务都已经跑完却查不到数据）。窗口内**没有任何任务**
（两个计数都是 0）是一个合法的确定结论（"这一天没有任务"），不是不可判定，
与本模块其余段落对"空窗口"的既有处理方式一致。

守卫触发（`turn_timeout`/`max_turns_exceeded`/`drain_timeout`，与 `V-护栏-01/02/08`
的原因码同源）恰好会写进 `task.error_kind`，因此单独可判定，与「拒绝计数」拆成两个
独立段落——同一条 #303 条目里能拿到的部分不因拿不到的另一半被一起隐藏。

## 用户标识为什么不出现在正文里

`core/identity/identifiers.py` 的 :func:`~lingxi.core.identity.identifiers.
redact_identifier` 文档字符串明确写着「**全仓库唯一允许缩短飞书标识的地方，而且只
允许用于日志**」——管理群通知不是日志，且它缩短的是飞书外部标识，不是 Lingxi 内部
`app_user.id`。因此本模块对「逐用户任务量」的呈现选择**匿名分桶**（`1 条=N 人，
2-5 条=M 人……`），不携带任何形式的用户标识（缩短或未缩短），比复用一个明确标注
「仅限日志」的函数更安全：调用方（`adapters/postgres_daily_report.py`）在 SQL 层面
就只需要 ``COUNT(*)`` 分组后的计数，从未把 ``user_id`` 取回 Python。

## 重复内容节流（继承自旧系统的教训）

#303 记录旧系统的实践结论：「高权限名单加 7 天节流」。本报告把同一条纪律用在**失败
分类 Top** 上——:func:`apply_repeat_throttle` 记录每个原因码连续出现在 Top 榜的
天数，连续第 8 天起收起明细、只报计数与「连续第 N 天，已节流」，避免同一批陈旧
问题天天占据管理群的注意力，节流行本身也必须真的比未节流的一行更简短、不是
反而更长的纯装饰（opus 批量审查 P3-4 修复，见 `_render_failure_top`）；一旦某天
该原因码跌出 Top 榜，连续计数归零，重新出现按新一轮计。这套状态是调用方
（`DailyReportDuty`）持有的进程内字典，与
`RosterAuditDuty._completed_on` 同一条已知残留：**跨重启会清零重新计数**，不持久化
——本 Story 授权范围不含新迁移，且连续 7 天的节流阈值本身足够宽，一次重启不会让
用户看到明显的行为回退。

## 投递结果段为什么用一个独立、更早的窗口（opus 批量审查 P2 修复）

其余六段统计窗口固定是「昨天」（`[D-1, D)`，`D` 为通报当天 UTC 零点，见
`DailyReportDuty.run_once`）。投递结果段如果沿用同一个窗口就会有一个结构性缺陷：
`task_delivery_event.expires_at` 是「投递创建时刻 + 24 小时」，而**「昨天」这个
窗口里的行，24 小时到期时刻恰好落在今天**（早的落在今天凌晨，晚的落在今天深夜）；
通报在今天刚过零点后不久就跑，此刻绝大多数昨天创建的投递行，它们的 24 小时确认
期**根本还没关闭**——「过期」这一桶因此在结构上**恒为零**，不是"窗口内确实没有
过期投递"这个真结论，是"这个窗口问的问题现在还答不出来"。

修复：投递结果段改用**再早一天**的独立窗口 `[D-2, D-1)`（比其余六段整整早一个
自然日）。这个窗口里的投递行创建于两天前，到今天通报运行时 24 小时确认期已经
**完全关闭**，「过期」桶因此才有可能统计到真实的非零值。正文里这一段必须单独
标注它的窗口起止（与页首统计窗口不同），不能让读者误以为整份报告说的是同一天，
见 `_render_delivery_outcome`。

## 时区：UTC 计窗、正文双标注

统计窗口按 UTC 自然日切分（与 `roster_snapshot`/`task.content_expires_at` 等既有
UTC 约定一致），但正文标题**同时**标注对应的北京时间区间——`docs/技术设计/验收
矩阵-审计与保留.md` 的「花名册审计日报」一节已经记录过日报「UTC 日界与北京时间自
然日相差 8 小时」在读者侧造成的歧义（且当时明确留待产品负责人裁定、未处理）。本报
告不重复这个歧义：同时给出两个时区的起止时刻，读者不需要自己心算偏移量。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Generic, TypeVar

T = TypeVar("T")

#: 失败分类 reason_code 渲染前的形状白名单（opus 批量审查 P1 修复）。`reason_code`
#: 结构上来自 `task.error_kind`（`GUARD_ERROR_KINDS`/`TIMEOUT_ERROR_KINDS` 这类
#: 蛇形小写枚举值），但本模块拿到的只是调用方传入的字符串，不持有任何保证它恒为
#: 这个形状的类型系统约束——分类逻辑未来的一次改动完全可能意外把姓名、邮箱、
#: open_id 或原始异常文本当成了 reason_code。渲染前用这个白名单兜底，不匹配的值
#: 一律归入 `"other"`，不让任何不符合预期形状的取值直接进入群发的日报正文。这不是
#: "这类值现在会出现"的证据，是"即使出现也不会泄露"的结构性保证。
#:
#: **已知边界（不是回归）**：这是**形状**校验，不是语义校验——挡得住带有形状之外
#: 字符的泄露（大写字母、CJK、``@``/``.``、空格……），挡不住通篇小写字母/数字/
#: 下划线的标识符。要点是：**真实飞书用户 open_id 的标准形态（``ou_`` + 32 位
#: 小写十六进制）恰好整体落在这个盲区里**——这道纵深对「它最想挡的那一类标识」
#: 覆盖率为零，可诚实复述的结论只有「挡住带大写/CJK/标点等形状外字符的泄露；
#: 对全小写标识符（含真实形态 open_id）无效」。当前全仓 ``task.error_kind`` 的
#: 写入点均为固定蛇形小写字面量、无动态来源；若未来引入动态 error_kind，必须
#: 先把本白名单改为**枚举已知 reason_code 集合**，不能继续依赖字符集校验。见
#: `tests/test_daily_report_render.py` 的
#: `test_a_bare_lowercase_open_id_shaped_value_is_not_caught_by_the_shape_whitelist`
#: 对这条边界的诚实记录。
_REASON_CODE_PATTERN = re.compile(r"[a-z0-9_]{1,64}")


def _safe_reason_code(reason_code: str) -> str:
    if isinstance(reason_code, str) and _REASON_CODE_PATTERN.fullmatch(reason_code):
        return reason_code
    return "other"


#: 失败分类原因码 → 中文（Trace #469 修复包 B，B-8 遗留第 2 项）：「失败分类
#: Top」此前直出 ``task.error_kind`` 的英文机器码（``turn_timeout``/
#: ``max_turns_exceeded`` 这类），管理员看不出对应哪一类真实故障——沿用
#: ``core/admin/router.py`` B-6 建立的同一套显示名映射姿势（白名单式展示层
#: 翻译，未登记的取值回退成「原值（未登记显示名）」，不崩、不假装认识）。
#: 覆盖 :data:`GUARD_ERROR_KINDS`/:data:`TIMEOUT_ERROR_KINDS` 与
#: ``apps/worker/service.py``/``core/delivery/ports.py``/
#: ``adapters/postgres_conversation/`` 现有登记的全部 ``error_kind`` 取值。
#: ``"other"`` 是 :func:`_safe_reason_code` 自己的安全哨兵值（形状白名单
#: 兜底，不是真实 ``error_kind``），映射到它自身、不参与"未登记"回退——它
#: 已经是一个管理员看得懂的英文词，不需要再包一层「未登记显示名」提示。
_ERROR_KIND_LABEL: dict[str, str] = {
    "other": "other",
    "turn_timeout": "单轮对话超时",
    "max_turns_exceeded": "对话轮数超限",
    "drain_timeout": "收尾超时",
    "running_timeout": "执行超时",
    "context_too_long": "上下文过长",
    "side_effect_uncertain": "执行结果不确定（需人工核实是否已生效）",
    "model_protocol_breakdown": "模型输出协议异常",
    "result_too_large": "查询结果过大",
    "mcp_bad_gateway": "指标 MCP 网关返回 502",
    "session_failed": "会话执行失败",
    "stopped": "用户主动停止",
    "redacted_withheld": "内容因安全策略被拦截",
    "delivery_expired": "投递已过期",
    "retry_exhausted": "重试次数耗尽",
    "queued_timeout": "排队超时未领取",
    "worker_version_unavailable": "目标执行版本不可用",
}


def _humanize_error_kind(safe_reason_code: str) -> str:
    """把已经过 :func:`_safe_reason_code` 白名单校验的原因码翻译成中文显示名
    （Trace #469 修复包 B，B-8）。未登记的取值（词表遗漏，或未来新增但没有
    同步登记）回退成「原值（未登记显示名）」，与 ``core/admin/router.py``
    ``_display_or_unregistered`` 同一样式。"""

    label = _ERROR_KIND_LABEL.get(safe_reason_code)
    if label is None:
        return f"{safe_reason_code}（未登记显示名）"
    return label

#: 与旧系统「高权限名单加 7 天节流」同一量级的节流阈值（#303 继承的设计教训）。
#: 连续出现天数超过这个数才折叠，即第 1–7 天正常展开、第 8 天起折叠。
REPEAT_THROTTLE_DAYS = 7

#: 失败分类 Top 榜单默认展示条数上限。
DEFAULT_FAILURE_TOP_LIMIT = 10

#: 已知的护栏触发原因码（`task.error_kind`）。与 `core/delivery/ports.py` 的
#: `TerminalKind.TIMEOUT` 默认值、`apps/worker/service.py::_failure_content` 落库
#: 的具体分类码对齐（`V-护栏-01/02/08` 断言的原因码同源：`max_turns_exceeded`=超轮数、
#: `turn_timeout`=墙钟超时、`drain_timeout`=收尾超时）。
GUARD_ERROR_KINDS = frozenset({"turn_timeout", "max_turns_exceeded", "drain_timeout"})

#: 计入「超时」桶的原因码：都是墙钟维度的超时。`max_turns_exceeded` 是轮数超限、
#: 不是时间超限，因此不进这个集合——它仍计入「失败」桶，也仍计入 `GUARD_ERROR_KINDS`。
TIMEOUT_ERROR_KINDS = frozenset({"running_timeout", "turn_timeout", "drain_timeout"})

#: 窗口内**存在**任务、但 ``task.token_usage`` **全部**为 ``NULL`` 时使用（模块
#: 文档「NULL 行归入不可判定、不静默计为零」）——不是查询本身失败，是这批任务
#: 结构性地没有可用的 token 用量数字，三类真实成因见下面文案本身（批次 4 opus
#: 审查 P3-1/P3-3：此前只列了"迁移 0070 之前"与"从未真正跑过一次回合"两种，
#: 漏了最常见的"窗口内任务此刻仍在排队/运行中、还没收口"；且旧文案在
#: ``DENIED_COUNT_ALL_NULL_REASON`` 里直接嵌了一句「见
#: RESOURCE_USAGE_ALL_NULL_REASON」——这段文案会随通报正文直接展示给管理群，
#: 是 Python 源码里的变量名，读者看不到、也不该需要认识它才能看懂通报在说
#: 什么，已改为两段各自白话复述，不互相引用标识符）。
RESOURCE_USAGE_ALL_NULL_REASON = (
    "本窗口内的任务在 token 用量这一项上全部没有可用数字：可能是这些任务此刻"
    "仍在排队或运行中、还没有跑完收口，也可能是任务本身产生于这项统计上线"
    "之前，或任务虽然已经结束但从未真正进入过一次执行回合（例如开工前就被"
    "中止、读取用户配置失败、执行器出现未预期异常），因此没有任何数字可以聚合"
)
#: 与 :data:`RESOURCE_USAGE_ALL_NULL_REASON` 成因相同（``task.guard_denied_count``
#: 与 ``task.token_usage`` 在同一次终态写入里同时落库）；独立成一份文案而不是
#: 互相引用对方变量名，理由同上——读者不该需要跳到另一段、认出一个 Python
#: 标识符才能看懂这一段在说什么。
DENIED_COUNT_ALL_NULL_REASON = (
    "本窗口内的任务在守卫拒绝计数这一项上全部没有可用数字：可能是这些任务此刻"
    "仍在排队或运行中、还没有跑完收口，也可能是任务本身产生于这项统计上线"
    "之前，或任务虽然已经结束但从未真正进入过一次执行回合（例如开工前就被"
    "中止、读取用户配置失败、执行器出现未预期异常），因此没有任何数字可以聚合"
)
CALL_COUNT_BASELINE_UNAVAILABLE_REASON = (
    "MCP 调用次数（对照 #296 的 16 次/任务基线）同样仅见于 worker 结构化日志的 "
    "audit.call_count 字段，未落库，无法对照"
)

_BEIJING_OFFSET = timedelta(hours=8)

_TASK_COUNT_BUCKET_EDGES: tuple[tuple[int, int | None], ...] = ((1, 1), (2, 5), (6, 10), (11, None))


@dataclass(frozen=True)
class Section(Generic[T]):
    """一段可能取不到的统计数据。

    `undetermined_reason` 非空即表示这一段「不可判定」——**绝不能**把「取不到」悄悄
    渲染成 0 或干脆省略这一段（#303 继承自旧系统的教训：静默失败曾被误读成
    「一切正常」）。判定值与不可判定原因互斥：判定值存在时原因必为 `None`，反之亦然，
    由 :meth:`of`/:meth:`undetermined` 两个构造入口保证，不留第三种可以绕过的姿势。
    """

    value: T | None
    undetermined_reason: str | None = None

    @classmethod
    def of(cls, value: T) -> "Section[T]":
        return cls(value=value, undetermined_reason=None)

    @classmethod
    def undetermined(cls, reason: str) -> "Section[T]":
        if not reason or not reason.strip():
            raise ValueError("不可判定必须给出非空原因，不能只留一个空段落")
        return cls(value=None, undetermined_reason=reason)

    @property
    def is_determined(self) -> bool:
        return self.undetermined_reason is None


@dataclass(frozen=True)
class ActiveUserStats:
    active_user_count: int
    #: `(分桶标签, 落在该桶的用户数)`，次序固定：1 条 / 2-5 条 / 6-10 条 / 11+ 条。
    #: 不含任何用户标识（`user_id` 只用于 SQL 分组计数，从不取回调用方，见模块文档）。
    task_count_buckets: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class StatusDistribution:
    success: int
    failed: int
    timeout: int
    stopped: int
    #: 排队 / 执行中 / 待投递等非终态，供参照，不属于 #303 要求的四分类本身。
    in_progress: int


@dataclass(frozen=True)
class FailureReasonCount:
    reason_code: str
    count: int


@dataclass(frozen=True)
class ThrottledFailureLine:
    reason_code: str
    count: int
    streak_days: int
    throttled: bool


@dataclass(frozen=True)
class LatencyStats:
    sample_count: int
    average_seconds: float
    median_seconds: float
    p90_seconds: float
    max_seconds: float


@dataclass(frozen=True)
class DeliveryOutcomeStats:
    delivered_card: int
    delivered_fallback_text: int
    expired: int
    #: 仍在 24 小时投递确认窗口内、尚无结论的终态行。
    pending: int


@dataclass(frozen=True)
class PartialCount:
    """一段可能只覆盖窗口内**部分**任务的计数（Issue #303/#304 批次 4：
    ``task.guard_denied_count`` 逐行可能是 ``NULL``）。

    ``total`` 只是 ``covered_tasks`` 那些任务的求和——``uncovered_tasks`` 那些
    任务的字段是 ``NULL``，不参与求和，也不被静默当成 0（模块文档「NULL 行归入
    不可判定、不静默计为零」）。``uncovered_tasks > 0`` 时渲染层必须把这个数字
    一并显示，不能只展示 ``total`` 让读者误以为它是窗口内全部任务的准确总和。
    """

    total: int
    covered_tasks: int
    uncovered_tasks: int


@dataclass(frozen=True)
class TokenUsageStats:
    """窗口内 token 用量的部分聚合，与 :class:`PartialCount` 同一条「NULL 行不
    静默计零」纪律，只是四个字段（对应 ``core/execution/message_stream.py::
    _usage_summary`` 的四个已知计数字段）各自求和，覆盖度只按整行
    ``task.token_usage`` 是否为 ``NULL`` 判断（行内单个字段缺失是否存在，不在
    本模块的判断范围——见 ``apps/worker/service.py::_report_token_usage`` 文档
    「取到几个算几个」）。
    """

    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    covered_tasks: int
    uncovered_tasks: int


@dataclass(frozen=True)
class MetricCoverageGap:
    """「MCP 指标目录 vs 映射表覆盖面」差集的非空结果（Issue #320 并入项，来自
    #303 准入巡检对照，旧系统「指标准入」的等价物）。

    只在**存在**未覆盖指标时才会被构造——「无差异」由 :func:`build_metric_coverage_gap`
    返回 ``None`` 表达，不是一个 ``uncovered_metric_ids=()`` 的空实例，理由见该函数
    文档「无差异不报」一节。
    """

    #: 已排序去重的指标 ID：出现在 MCP 目录里、但当前映射表（``company_function_
    #: metric_map.toml``）任何一个「公司+职能」条目都没有覆盖到的那些。
    uncovered_metric_ids: tuple[str, ...]


@dataclass(frozen=True)
class LocalOverrideActivity:
    """本地权限覆盖当日活动与当前生效总量的非空结果（Issue #319 S-P-1c，管理员
    经确认卡对个别用户完成的授权/抑制/收回，见迁移 ``0072``/``0073``）。

    只在**存在**当日活动或当前有生效条目时才会被构造——「无差异」（当日零活动
    且当前生效总数为零）由 :func:`build_local_override_activity` 返回 ``None``
    表达，与 :class:`MetricCoverageGap` 同一条「无差异不报」纪律（该类文档
    「只在存在未覆盖指标时才会被构造」一节）。
    """

    #: 当日（对齐日报既有 UTC 日界统计窗口）新增的授权/抑制笔数，按 `direction`
    #: 分列。
    granted_today: int
    suppressed_today: int
    #: 当日被收回的笔数，**不分原方向**——收回是同一行状态翻转（迁移 `0072`
    #: 文件头部「为什么用『同一行状态翻转』」），一笔收回既可能翻转自一条历史
    #: grant 行也可能翻转自 suppress 行；读者需要的是「今天发生了几次收回操作」
    #: 这一个数字，拆成两个方向反而制造虚假精度（哪个方向被收回不改变这个事实）。
    revoked_today: int
    #: 当前生效（`entry_status = 'active'`）条目总数，按方向分列——与「当日新增
    #: 笔数」不是同一件事：历史上分多天新增、至今未被收回的条目都计入这里。
    active_grant_total: int
    active_suppress_total: int
    #: 当前生效条目覆盖的去重用户数（两个方向取并集）——同一用户可能同时命中
    #: 多条公司×指标覆盖，本字段回答「影响了多少个人」而不是「有多少行」。
    affected_user_count: int


@dataclass(frozen=True)
class DailyReportInputs:
    """一轮通报要渲染的全部数据，每段各自可能「不可判定」。"""

    window_start: datetime
    window_end: datetime
    active_users: Section[ActiveUserStats]
    status_distribution: Section[StatusDistribution]
    failure_top: Section[tuple[FailureReasonCount, ...]]
    guard_triggered: Section[int]
    denied_count: Section[PartialCount]
    latency: Section[LatencyStats]
    resource_usage: Section[TokenUsageStats]
    delivery_outcome: Section[DeliveryOutcomeStats]
    #: 投递结果段**独立**的统计窗口（opus 批量审查 P2 修复），与上面
    #: `window_start`/`window_end` 不是同一对值——见模块文档「投递结果段为什么用
    #: 一个独立、更早的窗口」。即使 `delivery_outcome` 本轮不可判定，这两个字段
    #: 依然必须给出"本来打算问哪个窗口"，供正文标注与测试钉住边界。
    delivery_window_start: datetime
    delivery_window_end: datetime
    #: 「未覆盖新指标」日检（Issue #320 并入项）。**默认 ``None``，且与其余七段的
    #: 「不可判定」不是同一件事**：
    #:
    #: - ``None``（未接线，本字段的默认值）：本职责这一轮**根本没有尝试**这项检查
    #:   （前置配置不全，见 ``apps/scheduler/daily_report.py`` 的装配文档）——正文
    #:   里完全不出现这一段，不是"检查了，不确定"；
    #: - ``Section.undetermined(reason)``：接线了，但这一轮取数失败（MCP 查不到，
    #:   或映射表读取失败）——正文出现一行不可判定说明，与其余段落同一条纪律
    #:   （"绝不能把「取不到」悄悄渲染成 0 或干脆省略"）；
    #: - ``Section.of(None)``：接线了，真查了，两边一致，没有差异——「无差异不报」，
    #:   正文同样不出现这一段；
    #: - ``Section.of(gap)``（``gap`` 非空）：存在未覆盖的新指标，正文出现「待分配」段。
    #:
    #: 放在字段列表末尾并给默认值，是为了不打破既有全部调用点（生产代码与测试）
    #: 现有的关键字参数构造方式——本字段是纯新增，不重排、不改动任何既有字段。
    metric_coverage_gap: "Section[MetricCoverageGap | None] | None" = None
    #: 「本地权限覆盖活动」段（Issue #319 S-P-1c）。三态语义与
    #: :attr:`metric_coverage_gap` 完全一致（只是数据源换成 `local_permission_
    #: override` 表，见 ``apps/scheduler/daily_report.py`` 的装配文档）：
    #:
    #: - ``None``（未接线，默认值）：本轮**没有尝试**读取——正文完全不出现这一段；
    #: - ``Section.undetermined(reason)``：接线了，但本轮取数失败——正文出现一行
    #:   不可判定说明；
    #: - ``Section.of(None)``：接线了，真查了，当日零活动且当前生效总数为零——
    #:   「无差异不报」，正文同样不出现这一段；
    #: - ``Section.of(activity)``（非空）：存在当日活动或当前有生效条目，正文
    #:   出现「本地权限覆盖活动」段，只含计数，不含 open_id/公司/指标名/理由。
    #:
    #: 同样放在字段列表末尾并给默认值，理由与 `metric_coverage_gap` 相同。
    local_override_activity: "Section[LocalOverrideActivity | None] | None" = None


# --------------------------------------------------------------------------
# 纯计算：从「哑」的分组行构造各段统计
# --------------------------------------------------------------------------


def build_active_user_stats(per_user_task_counts: Sequence[int]) -> ActiveUserStats:
    """从「每个活跃用户当窗口内的任务数」构造匿名分桶统计。

    入参**只是一串整数**，调用方（适配器）不得把 `user_id` 一并传入——这是本模块
    在类型层面就守住「用户标识不进正文」的方式之一。
    """

    buckets: list[tuple[str, int]] = []
    for low, high in _TASK_COUNT_BUCKET_EDGES:
        if high is None:
            label = f"{low}+ 条"
            matched = sum(1 for count in per_user_task_counts if count >= low)
        elif low == high:
            label = f"{low} 条"
            matched = sum(1 for count in per_user_task_counts if count == low)
        else:
            label = f"{low}-{high} 条"
            matched = sum(1 for count in per_user_task_counts if low <= count <= high)
        buckets.append((label, matched))
    return ActiveUserStats(
        active_user_count=len(per_user_task_counts),
        task_count_buckets=tuple(buckets),
    )


#: 供适配器与核心层共用的「哑」分组行类型：`(task.status, task.error_kind, 该组件数)`。
#: SQL 只按这两列分组计数，不做任何分类判断——分类规则（谁算超时、谁算护栏触发）
#: 全部在本模块的纯函数里，保持可以脱离数据库单测。
TaskOutcomeRow = tuple[str, "str | None", int]


def build_status_distribution(rows: Sequence[TaskOutcomeRow]) -> StatusDistribution:
    success = failed = timeout = stopped = in_progress = 0
    for status, error_kind, count in rows:
        if status == "succeeded":
            success += count
        elif status == "stopped":
            stopped += count
        elif status == "failed":
            if error_kind in TIMEOUT_ERROR_KINDS:
                timeout += count
            else:
                failed += count
        else:
            # queued / running / awaiting_delivery：窗口内还没走到终态。
            in_progress += count
    return StatusDistribution(
        success=success, failed=failed, timeout=timeout, stopped=stopped, in_progress=in_progress
    )


def build_failure_top(
    rows: Sequence[TaskOutcomeRow], *, limit: int = DEFAULT_FAILURE_TOP_LIMIT
) -> tuple[FailureReasonCount, ...]:
    if limit < 1:
        raise ValueError("失败分类 Top 的展示条数上限必须是正整数")
    totals: dict[str, int] = {}
    for status, error_kind, count in rows:
        if status not in ("failed", "stopped") or not error_kind:
            continue
        totals[error_kind] = totals.get(error_kind, 0) + count
    # 按次数降序；次数相同时按原因码字典序，保证渲染结果确定、不随字典遍历顺序漂移
    # （与 `roster_report.py` 「渲染结果对同样的输入逐字节一致」同一条纪律）。
    ordered = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    return tuple(FailureReasonCount(reason_code=code, count=n) for code, n in ordered[:limit])


def build_guard_triggered_count(rows: Sequence[TaskOutcomeRow]) -> int:
    return sum(count for _status, error_kind, count in rows if error_kind in GUARD_ERROR_KINDS)


def build_denied_count_stats(
    *, covered_tasks: int, uncovered_tasks: int, total: int
) -> PartialCount | None:
    """从适配器的哑聚合（``COUNT(*) FILTER``/``SUM``，见
    ``adapters/postgres_daily_report.py::guard_denied_count_stats``）构造
    :class:`PartialCount`；窗口内**有**任务但**全部**是 ``NULL``（一个都没覆盖到）
    时返回 ``None``，调用方据此构造 ``Section.undetermined(DENIED_COUNT_ALL_
    NULL_REASON)``（模块文档「NULL 行归入不可判定、不静默计为零」）。窗口内
    没有任何任务（``covered_tasks == uncovered_tasks == 0``）是合法的确定零，
    不走这条 ``None`` 分支。
    """

    if covered_tasks == 0 and uncovered_tasks > 0:
        return None
    return PartialCount(total=total, covered_tasks=covered_tasks, uncovered_tasks=uncovered_tasks)


def build_token_usage_stats(
    *,
    covered_tasks: int,
    uncovered_tasks: int,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
) -> TokenUsageStats | None:
    """与 :func:`build_denied_count_stats` 同一条判定，对应
    ``adapters/postgres_daily_report.py::token_usage_stats`` 的哑聚合结果。"""

    if covered_tasks == 0 and uncovered_tasks > 0:
        return None
    return TokenUsageStats(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        covered_tasks=covered_tasks,
        uncovered_tasks=uncovered_tasks,
    )


def apply_repeat_throttle(
    previous_streaks: Mapping[str, int],
    today_top: Sequence[FailureReasonCount],
    *,
    threshold: int = REPEAT_THROTTLE_DAYS,
) -> tuple[tuple[ThrottledFailureLine, ...], dict[str, int]]:
    """把「今天的失败分类 Top」与「此前的连续在榜天数」合并，产出节流后的展示行
    与更新后的连续天数字典。

    调用方只在**成功送达**之后才把返回的字典提交为新状态（与 `RosterAuditDuty` 的
    日期水位「只在发送成功后才置位」同一条纪律）——送达失败时保留旧状态，下一轮
    重试不会因为一次瞬时故障而错误地把连续计数清零或提前推进。

    今天没有出现在 Top 榜的原因码**从返回字典里消失**，即连续计数归零；哪天它再次
    出现，从 1 重新计。
    """

    updated: dict[str, int] = {}
    lines: list[ThrottledFailureLine] = []
    for entry in today_top:
        streak = previous_streaks.get(entry.reason_code, 0) + 1
        updated[entry.reason_code] = streak
        lines.append(
            ThrottledFailureLine(
                reason_code=entry.reason_code,
                count=entry.count,
                streak_days=streak,
                throttled=streak > threshold,
            )
        )
    return tuple(lines), updated


def build_latency_stats(durations_seconds: Sequence[float]) -> LatencyStats | None:
    """从原始耗时样本（秒）构造统计摘要；无样本返回 ``None``（调用方据此判定
    「窗口内没有已完成的任务」，与「取不到数据」是两回事，不应渲染成不可判定）。
    """

    if not durations_seconds:
        return None
    ordered = sorted(durations_seconds)
    count = len(ordered)
    return LatencyStats(
        sample_count=count,
        average_seconds=sum(ordered) / count,
        median_seconds=_percentile(ordered, 0.5),
        p90_seconds=_percentile(ordered, 0.9),
        max_seconds=ordered[-1],
    )


def _percentile(ordered: Sequence[float], fraction: float) -> float:
    """线性插值分位数。`ordered` 必须已经升序排列且非空（内部调用方保证）。"""

    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


#: 供适配器与核心层共用的「哑」投递分组行：
#: `(platform_message_kind, 是否已确认送达, 是否已过 24 小时到期, 该组件数)`。
DeliveryOutcomeRow = tuple["str | None", bool, bool, int]


def build_delivery_outcome(rows: Sequence[DeliveryOutcomeRow]) -> DeliveryOutcomeStats:
    card = fallback = expired = pending = 0
    for kind, received, is_expired, count in rows:
        if received and kind == "card":
            card += count
        elif received and kind == "text":
            fallback += count
        elif not received and is_expired:
            expired += count
        else:
            pending += count
    return DeliveryOutcomeStats(
        delivered_card=card, delivered_fallback_text=fallback, expired=expired, pending=pending
    )


def build_metric_coverage_gap(
    mcp_metric_ids: Sequence[str], mapped_metric_ids: Sequence[str]
) -> MetricCoverageGap | None:
    """「MCP 指标目录 vs 映射表覆盖面」差集（Issue #320 并入项）：纯集合运算，
    不做任何 I/O——调用方（``apps/scheduler/daily_report.py``）负责分别取到这两组
    指标 ID 再传进来。

    **无差异不报**：`mcp_metric_ids` 里的每一个 ID 都能在 `mapped_metric_ids` 里
    找到时返回 ``None``，调用方据此不往正文里插入这一段——这不是「不可判定」，是
    「判定过，没有问题」，与 :class:`Section` 的既有二分法（判定值 / 不可判定原因）
    不完全对齐，因此 :attr:`DailyReportInputs.metric_coverage_gap` 用
    ``Section[MetricCoverageGap | None]`` 而不是 ``Section[MetricCoverageGap]``——
    「有值」这件事本身分「有差异」与「查过，无差异」两种，只有前者需要占用正文篇幅。

    结果按字符串排序、去重（`dict.fromkeys` 保序后再 `sorted`，与本仓库其余聚合层
    同一条纪律），保证同一组输入产出同一份输出——正文因此不会因为集合迭代顺序不同
    而每天字节不同。**不做任何大小写或全半角归一**：指标 ID 逐字符透传，与
    ``core/permission/publish_row.py`` 的「零归一」纪律一致，这里比对的是"MCP 报告
    的 ID 字符串"与"映射表里写的 ID 字符串"是否**逐字**相等，不是"看起来像不像
    同一个指标"。
    """

    mapped = set(mapped_metric_ids)
    gap = sorted(dict.fromkeys(metric_id for metric_id in mcp_metric_ids if metric_id not in mapped))
    if not gap:
        return None
    return MetricCoverageGap(uncovered_metric_ids=tuple(gap))


def build_local_override_activity(
    *,
    granted_today: int,
    suppressed_today: int,
    revoked_today: int,
    active_grant_total: int,
    active_suppress_total: int,
    affected_user_count: int,
) -> LocalOverrideActivity | None:
    """从适配器的哑聚合（``COUNT(*) FILTER``/``COUNT(DISTINCT ...)``，见
    ``adapters/postgres_local_permission.py::PostgresLocalPermissionOverrideStore.
    daily_activity_stats``）构造 :class:`LocalOverrideActivity`（Issue #319
    S-P-1c）：纯集合运算，不做任何 I/O。

    **无差异不报**：当日零新增（`granted_today == suppressed_today ==
    revoked_today == 0`）且当前生效总数为零（`active_grant_total ==
    active_suppress_total == 0`）时返回 ``None``，调用方据此不往正文里插入这
    一段——与 :func:`build_metric_coverage_gap` 同一条「判定过，没有问题」纪律，
    不是「取不到」。`affected_user_count` 不单独参与这条判断：两个方向的生效
    总数都是零时，覆盖它们的去重用户数结构上也必然是零，不需要重复核对。
    """

    if (
        granted_today == 0
        and suppressed_today == 0
        and revoked_today == 0
        and active_grant_total == 0
        and active_suppress_total == 0
    ):
        return None
    return LocalOverrideActivity(
        granted_today=granted_today,
        suppressed_today=suppressed_today,
        revoked_today=revoked_today,
        active_grant_total=active_grant_total,
        active_suppress_total=active_suppress_total,
        affected_user_count=affected_user_count,
    )


# --------------------------------------------------------------------------
# 渲染：纯函数，输入相同则输出逐字节相同
# --------------------------------------------------------------------------


def _format_window_header(window_start: datetime, window_end: datetime) -> str:
    """窗口起止**各自带日期**，不只写时分——`00:00–00:00` 这种同名时刻的写法会让人
    误读成"零时长窗口"，两端都写全日期才不会歧义（对照花名册日报在
    `docs/技术设计/验收矩阵-审计与保留.md` 的「花名册审计日报」一节留下的「UTC 日界
    与北京自然日相差 8 小时」教训，这里两个时区都完整给出，不需要读者自己心算偏移
    量）。
    """

    utc_start = window_start.astimezone(timezone.utc)
    utc_end = window_end.astimezone(timezone.utc)
    beijing_start = utc_start + _BEIJING_OFFSET
    beijing_end = utc_end + _BEIJING_OFFSET
    return (
        f"统计窗口：{utc_start:%Y-%m-%d %H:%M}–{utc_end:%Y-%m-%d %H:%M}（UTC）"
        f" ／ {beijing_start:%Y-%m-%d %H:%M}–{beijing_end:%Y-%m-%d %H:%M}（北京时间）"
    )


def _render_undetermined(reason: str) -> str:
    return f"不可判定（原因：{reason}）"


def _render_active_users(section: Section[ActiveUserStats]) -> str:
    if not section.is_determined:
        assert section.undetermined_reason is not None
        return f"活跃用户与任务量分布：{_render_undetermined(section.undetermined_reason)}"
    stats = section.value
    assert stats is not None
    buckets = "，".join(f"{label}={count} 人" for label, count in stats.task_count_buckets if count)
    if not buckets:
        buckets = "（窗口内无任务）"
    return f"活跃用户：{stats.active_user_count} 人；任务量分布：{buckets}"


def _render_status_distribution(section: Section[StatusDistribution]) -> str:
    if not section.is_determined:
        assert section.undetermined_reason is not None
        return f"任务结果分布：{_render_undetermined(section.undetermined_reason)}"
    stats = section.value
    assert stats is not None
    return (
        f"任务结果：成功 {stats.success}，失败 {stats.failed}，超时 {stats.timeout}，"
        f"停止 {stats.stopped}（进行中/其他 {stats.in_progress}）"
    )


def _render_failure_top(
    section: Section[tuple[FailureReasonCount, ...]],
    throttled_lines: Sequence[ThrottledFailureLine] | None,
) -> list[str]:
    if not section.is_determined:
        assert section.undetermined_reason is not None
        return [f"失败分类 Top：{_render_undetermined(section.undetermined_reason)}"]
    entries = section.value
    assert entries is not None
    if not entries:
        return ["失败分类 Top：窗口内无失败或停止任务"]
    lines = ["失败分类 Top："]
    by_code = {line.reason_code: line for line in (throttled_lines or ())}
    for entry in entries:
        # 匹配节流记录用原始 reason_code（与 apply_repeat_throttle 记账时用的是
        # 同一个键），渲染进正文的展示值另外过一遍形状白名单——两者故意分开。
        throttled = by_code.get(entry.reason_code)
        safe_code = _safe_reason_code(entry.reason_code)
        # 展示用中文显示名（Trace #469 修复包 B，B-8）：节流匹配、streak 记账
        # 仍然全部用上面的原始英文 safe_code 做字典键，只有拼进正文这一步换
        # 成人话，不影响任何既有的键值逻辑。
        display_code = _humanize_error_kind(safe_code)
        if throttled is not None and throttled.throttled:
            # 节流行必须真的更短，不是比未节流行长（opus P3-4 修复）：此前这里
            # 还带一句"仅计数不再展开明细"——本函数里未节流的行本来就从不展开
            # 任何明细，这句说明纯属装饰，只会让"节流"看起来比它实际做的事更
            # 复杂。现在只保留读者真正需要的两个新增信息：连续在榜天数、已节流。
            lines.append(
                f"- {display_code}：{entry.count} 次（连续第 {throttled.streak_days} 天，已节流）"
            )
        else:
            lines.append(f"- {display_code}：{entry.count} 次")
    return lines


def _render_guard_triggered(section: Section[int]) -> str:
    if not section.is_determined:
        assert section.undetermined_reason is not None
        return f"守卫触发计数（超轮数/超时/收尾超时）：{_render_undetermined(section.undetermined_reason)}"
    return f"守卫触发计数（超轮数/超时/收尾超时）：{section.value} 次"


def _render_coverage_note(*, covered_tasks: int, uncovered_tasks: int) -> str:
    """``uncovered_tasks > 0`` 时附加的覆盖度说明——告诉读者这个数字不是窗口内
    全部任务的准确总和，还有多少个任务因为字段缺失没有参与求和（模块文档
    「NULL 行归入不可判定、不静默计为零」）。``uncovered_tasks == 0`` 时不附加
    任何说明，避免给完全覆盖的正常情形也画蛇添足。
    """

    if uncovered_tasks <= 0:
        return ""
    total_tasks = covered_tasks + uncovered_tasks
    return f"（覆盖 {covered_tasks}/{total_tasks} 个任务；另有 {uncovered_tasks} 个任务因字段缺失未计入，不计为零）"


def _render_denied_count(section: Section[PartialCount]) -> str:
    label = "工具调用拒绝计数（PreToolUse 拒绝）"
    if not section.is_determined:
        assert section.undetermined_reason is not None
        return f"{label}：{_render_undetermined(section.undetermined_reason)}"
    stats = section.value
    assert stats is not None
    note = _render_coverage_note(covered_tasks=stats.covered_tasks, uncovered_tasks=stats.uncovered_tasks)
    return f"{label}：{stats.total} 次{note}"


def _render_latency(section: Section[LatencyStats]) -> str:
    header = "时延分布（Agent 执行耗时 started_at→ended_at；对照 #296 基线 262 秒/任务）："
    if not section.is_determined:
        assert section.undetermined_reason is not None
        return header + _render_undetermined(section.undetermined_reason)
    stats = section.value
    if stats is None:
        body = "窗口内没有已完成的任务，无样本"
    else:
        body = (
            f"均值 {stats.average_seconds:.1f}s，中位 {stats.median_seconds:.1f}s，"
            f"P90 {stats.p90_seconds:.1f}s，最大 {stats.max_seconds:.1f}s（{stats.sample_count} 个样本）"
        )
    # 调用次数基线对照恒为不可判定（原因见模块文档），与「本段是否查到时延样本」
    # 无关——即使窗口内没有任何已完成任务，这条结构性缺口依然存在，因此不放在
    # 「有样本」分支内部，两种情形都要出现。
    call_count_note = f"调用次数对照（#296 基线 16 次/任务）：{_render_undetermined(CALL_COUNT_BASELINE_UNAVAILABLE_REASON)}"
    return f"{header}{body}\n{call_count_note}"


def _render_resource_usage(section: Section[TokenUsageStats]) -> str:
    # 标题不写"与成本估算"：本段现在是 task.token_usage 的真实聚合（Issue
    # #303/#304 批次 4），但没有接入任何模型定价表——展示的是原始 token 计数，
    # 不是货币成本，标题不能承诺一个本模块没有计算的数字。
    label = "token 用量"
    if not section.is_determined:
        assert section.undetermined_reason is not None
        return f"{label}：{_render_undetermined(section.undetermined_reason)}"
    stats = section.value
    assert stats is not None
    note = _render_coverage_note(covered_tasks=stats.covered_tasks, uncovered_tasks=stats.uncovered_tasks)
    body = (
        f"input={stats.input_tokens}，output={stats.output_tokens}，"
        f"cache_creation={stats.cache_creation_input_tokens}，"
        f"cache_read={stats.cache_read_input_tokens}"
    )
    return f"{label}：{body}{note}"


def _format_delivery_window_note(window_start: datetime, window_end: datetime) -> str:
    """投递结果段的窗口标注，刻意不复用 `_format_window_header` 的原样输出——
    这一段的窗口本来就与页首、以及其余全部段落不同（见模块文档「投递结果段为
    什么用一个独立、更早的窗口」），必须让读者一眼看出"这段说的是哪一天"，不能
    默认它跟页首那一行是同一个窗口。
    """

    utc_start = window_start.astimezone(timezone.utc)
    utc_end = window_end.astimezone(timezone.utc)
    beijing_start = utc_start + _BEIJING_OFFSET
    beijing_end = utc_end + _BEIJING_OFFSET
    return (
        f"（本段窗口：{utc_start:%Y-%m-%d %H:%M}–{utc_end:%Y-%m-%d %H:%M}（UTC）"
        f" ／ {beijing_start:%Y-%m-%d %H:%M}–{beijing_end:%Y-%m-%d %H:%M}（北京时间），"
        "比上方统计窗口早一天：24 小时投递确认期必须已经完全关闭，"
        "「过期」这一桶才有可能统计到非零值）"
    )


def _render_delivery_outcome(
    section: Section[DeliveryOutcomeStats], *, window_start: datetime, window_end: datetime
) -> str:
    window_note = _format_delivery_window_note(window_start, window_end)
    if not section.is_determined:
        assert section.undetermined_reason is not None
        return f"投递结果分布：{_render_undetermined(section.undetermined_reason)}\n{window_note}"
    stats = section.value
    assert stats is not None
    return (
        f"投递结果：成功(卡片) {stats.delivered_card}，兜底(文本) {stats.delivered_fallback_text}，"
        f"过期 {stats.expired}，待定(24h 窗口内) {stats.pending}\n{window_note}"
    )


def _render_metric_coverage_gap(section: "Section[MetricCoverageGap | None] | None") -> str:
    """「待分配」段（Issue #320 并入项）。返回空字符串表示**这一段完全不出现**——
    与其余段落不同，本段允许彻底不出现，见 :attr:`DailyReportInputs.metric_coverage_gap`
    的三态说明。未接线（``section is None``）与「接线了、查过、没有差异」
    （``section.value is None``）都返回空字符串，调用方据此不往正文里插入这一段。
    """

    if section is None:
        return ""
    if not section.is_determined:
        assert section.undetermined_reason is not None
        return f"待分配（新指标未覆盖核对）：{_render_undetermined(section.undetermined_reason)}"
    gap = section.value
    if gap is None or not gap.uncovered_metric_ids:
        return ""
    ids = "、".join(gap.uncovered_metric_ids)
    return (
        f"待分配（新指标未覆盖核对）：MCP 指标目录中发现映射表尚未覆盖的指标 {ids}，"
        "需要产品负责人补充 company_function_metric_map.toml"
    )


def _render_local_override_activity(
    section: "Section[LocalOverrideActivity | None] | None",
) -> str:
    """「本地权限覆盖活动」段（Issue #319 S-P-1c）。返回空字符串表示这一段完全
    不出现——与 :func:`_render_metric_coverage_gap` 同一姿态：未接线
    （``section is None``）与「接线了、查过、当日无变化且当前无生效条目」
    （``section.value is None``）都返回空字符串，见
    :attr:`DailyReportInputs.local_override_activity` 的三态说明。

    正文只含计数：不含 open_id、公司 ID、指标名或理由文本——管理群通知需要
    知道「有没有人被本地授权/抑制」这一事实级别的信号，不需要知道「是谁、
    为了哪个公司哪个指标、理由是什么」，与 `V-花名册-34` 的隐私纪律同向。

    **措辞如实化（Trace #328 opus 审查；2026-08-29 再次如实化，PM 裁定，Issue
    #419；2026-08-29 P1-2 独立审查再次收窄）**：正文用「本窗口」不用「今日」——
    本段与本报告其余六段统计的是同一个 UTC 窗口，而这个窗口实际是「昨天」
    （`[D-1, D)`，模块文档「投递结果段为什么用一个独立、更早的窗口」一节旁的
    既有约定），把它说成「今日」并不准确。「生效」/「登记」的取舍：四源合并此前
    挂在 `aggregate.granted` 判据**之后**，一个当前没有任何银河权限的用户走的是
    撤权分支，从不到达本地覆盖合并那一步（`V-权限-15` 曾登记的已知限制）；PM
    2026-08-29 裁定消除了这条限制的**判据**（零银河用户的本地授权现在无条件
    参与合并），但**是否已经落到外部表**取决于哪条链真的跑到了这个人：新用户
    走开通链会立刻生效；**已开通的存量用户只能靠每日重算把它发布出去，而每日
    重算在现网当前未运行**（花名册短期令牌供给未配置，`permission_refresh.py`
    模块文档「为什么顺序判据…」一节）。笼统写「生效」会让读者误以为这两条链都
    已经真的把内容写进外部表。因此正文拆成两句：授权/抑制/收回笔数仍是对
    `local_permission_override` 这张表本身的如实计数（`entry_status='active'`，
    与是否已发布无关，故用「登记」）；额外单独注明生效口径——开通链已生效、
    重算侧待每日重算恢复运行——不再笼统断言「生效」，也不再走回 2026-08-18 那种
    对所有场景一刀切成「登记」的过度保守。

    **术语与管理卡/确认卡同步（Trace #469 收尾批修复包 E，#439 PM 裁定）**：三个
    计数的名字用「补充授权 / 屏蔽指标 / 撤销」，逐字取自 ``core/admin/
    notification._ACTION_LABEL``（与 ``core/admin/management_card.
    _DIRECTION_LABEL``、``core/admin/router._OVERRIDE_DIRECTION_LABEL`` 同一份
    口径）。本段是全链路术语统一的第五个出口——管理群同一批读者，白天在管理卡
    上点的是「撤销」，晚上在日报里读到的却是「收回」，同一件事两套说法
    （TOP-7）。这次改的只是这三个名字，「本窗口」/「登记」/「生效口径」三处
    PM 已裁定的措辞逐字不动。
    """

    if section is None:
        return ""
    if not section.is_determined:
        assert section.undetermined_reason is not None
        return f"本地权限覆盖活动：{_render_undetermined(section.undetermined_reason)}"
    activity = section.value
    if activity is None:
        return ""
    return (
        f"本地权限覆盖活动：本窗口新增 补充授权 {activity.granted_today} 笔、"
        f"屏蔽指标 {activity.suppressed_today} 笔、撤销 {activity.revoked_today} 笔；"
        f"当前登记 补充授权 {activity.active_grant_total} 条、"
        f"屏蔽指标 {activity.active_suppress_total} 条，"
        f"涉及 {activity.affected_user_count} 位用户"
        "（生效口径：开通链已生效，重算侧待每日重算恢复运行）"
    )


def render_daily_report(
    inputs: DailyReportInputs,
    *,
    throttled_failure_lines: Sequence[ThrottledFailureLine] | None = None,
) -> str:
    """渲染通报正文（纯函数，输入相同则逐字节输出相同）。

    正文**没有任何可执行入口**（没有按钮、链接、回调），与 `roster_report.py` 的
    `V-花名册-24` 同一条纪律：管理群是通知面，不是操作面。
    """

    lines = [
        "【内测每日通报】",
        _format_window_header(inputs.window_start, inputs.window_end),
        "本报告为统计级数据，不含用户对话原文、姓名、工号或邮箱。",
        "",
        _render_active_users(inputs.active_users),
        _render_status_distribution(inputs.status_distribution),
        "",
        *_render_failure_top(inputs.failure_top, throttled_failure_lines),
        "",
        _render_guard_triggered(inputs.guard_triggered),
        _render_denied_count(inputs.denied_count),
        "",
        _render_latency(inputs.latency),
        "",
        _render_resource_usage(inputs.resource_usage),
        "",
        _render_delivery_outcome(
            inputs.delivery_outcome,
            window_start=inputs.delivery_window_start,
            window_end=inputs.delivery_window_end,
        ),
    ]
    coverage_line = _render_metric_coverage_gap(inputs.metric_coverage_gap)
    if coverage_line:
        lines.extend(["", coverage_line])
    local_override_line = _render_local_override_activity(inputs.local_override_activity)
    if local_override_line:
        lines.extend(["", local_override_line])
    return "\n".join(lines)
