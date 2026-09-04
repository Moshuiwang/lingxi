"""内测每日通报的纯计算层：从「哑」的数据库聚合行构造各段统计。

无网络、无数据库依赖：外部数据一律由调用方（``adapters/postgres_daily_report.py``
读、``apps/scheduler/daily_report.py`` 编排）作为参数传入。与
:mod:`lingxi.core.daily_report`（渲染层）成对——本模块只负责"从行数据算出统计
值"，:mod:`lingxi.core.daily_report` 从这里 re-export 全部公开符号。

**NULL 行归入不可判定、不静默计为零**是贯穿全部 ``build_*`` 函数的纪律：
token 用量、守卫拒绝计数这类字段可能整行是 ``NULL``（任务此刻仍在排队/执行
中，或产生于该统计上线之前，或从未真正跑过一次执行回合），本模块只对有值的
任务求和，并把"多少个任务缺这个字段"作为独立数字一并交给调用方，不悄悄把
``NULL`` 当 0 揉进总数；只有窗口内一个任务都没有可用值时才整体判「不可判定」。
用户标识不出现在任何输出里：:class:`ActiveUserStats` 只接收匿名分桶后的整数。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Generic, TypeVar

T = TypeVar("T")

#: 与旧系统「高权限名单加 7 天节流」同一量级的节流阈值。连续出现天数超过这个
#: 数才折叠，即第 1–7 天正常展开、第 8 天起折叠。
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

#: 窗口内**存在**任务、但 ``task.token_usage`` **全部**为 ``NULL`` 时使用——不是
#: 查询本身失败，是这批任务结构性地没有可用的 token 用量数字，三类真实成因见
#: 下面文案本身。两段文案各自白话复述，不互相引用对方的 Python 变量名——这段
#: 文案会随通报正文直接展示给管理群，读者不该需要认识源码标识符才能看懂。
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

_TASK_COUNT_BUCKET_EDGES: tuple[tuple[int, int | None], ...] = ((1, 1), (2, 5), (6, 10), (11, None))


@dataclass(frozen=True)
class Section(Generic[T]):
    """一段可能取不到的统计数据。

    `undetermined_reason` 非空即表示这一段「不可判定」——**绝不能**把「取不到」悄悄
    渲染成 0 或干脆省略这一段（继承自旧系统的教训：静默失败曾被误读成「一切正常」）。
    判定值与不可判定原因互斥：判定值存在时原因必为 `None`，反之亦然，由
    :meth:`of`/:meth:`undetermined` 两个构造入口保证，不留第三种可以绕过的姿势。
    """

    value: T | None
    undetermined_reason: str | None = None

    @classmethod
    def of(cls, value: T) -> Section[T]:
        """构造一段已判定的统计数据。"""
        return cls(value=value, undetermined_reason=None)

    @classmethod
    def undetermined(cls, reason: str) -> Section[T]:
        """构造一段不可判定的统计数据；`reason` 必须非空。"""
        if not reason or not reason.strip():
            raise ValueError("不可判定必须给出非空原因，不能只留一个空段落")
        return cls(value=None, undetermined_reason=reason)

    @property
    def is_determined(self) -> bool:
        """这一段是否已经判定出具体值（而不是不可判定）。"""
        return self.undetermined_reason is None


@dataclass(frozen=True)
class ActiveUserStats:
    """窗口内活跃用户数与匿名分桶后的逐用户任务量分布。"""

    active_user_count: int
    #: `(分桶标签, 落在该桶的用户数)`，次序固定：1 条 / 2-5 条 / 6-10 条 / 11+ 条。
    #: 不含任何用户标识（`user_id` 只用于 SQL 分组计数，从不取回调用方，见模块文档）。
    task_count_buckets: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class StatusDistribution:
    """窗口内任务按终态状态分类的计数。"""

    success: int
    failed: int
    timeout: int
    stopped: int
    #: 排队 / 执行中 / 待投递等非终态，供参照，不属于四分类本身。
    in_progress: int


@dataclass(frozen=True)
class FailureReasonCount:
    """一个失败/停止原因码在窗口内出现的次数。"""

    reason_code: str
    count: int


@dataclass(frozen=True)
class ThrottledFailureLine:
    """一行失败分类 Top，附带连续在榜天数与是否已被节流折叠。"""

    reason_code: str
    count: int
    streak_days: int
    throttled: bool


@dataclass(frozen=True)
class LatencyStats:
    """窗口内已完成任务的执行耗时分布摘要。"""

    sample_count: int
    average_seconds: float
    median_seconds: float
    p90_seconds: float
    max_seconds: float


@dataclass(frozen=True)
class DeliveryOutcomeStats:
    """投递结果独立窗口内的终态分布。"""

    delivered_card: int
    delivered_fallback_text: int
    expired: int
    #: 仍在 24 小时投递确认窗口内、尚无结论的终态行。
    pending: int


@dataclass(frozen=True)
class PartialCount:
    """一段可能只覆盖窗口内**部分**任务的计数。

    `task.guard_denied_count` 逐行可能是 ``NULL``：``total`` 只是
    ``covered_tasks`` 那些任务的求和，``uncovered_tasks`` 那些任务的字段是
    ``NULL``，不参与求和，也不被静默当成 0。``uncovered_tasks > 0`` 时渲染层
    必须把这个数字一并显示，不能只展示 ``total`` 让读者误以为它是全部任务的
    准确总和。
    """

    total: int
    covered_tasks: int
    uncovered_tasks: int


@dataclass(frozen=True)
class TokenUsageStats:
    """窗口内 token 用量的部分聚合。

    与 :class:`PartialCount` 同一条「NULL 行不静默计零」纪律，只是四个字段
    （对应 ``core/execution/message_stream.py::_usage_summary`` 的四个已知
    计数字段）各自求和，覆盖度只按整行 ``task.token_usage`` 是否为 ``NULL``
    判断（行内单个字段缺失是否存在不在本模块判断范围，见
    ``apps/worker/service.py::_report_token_usage`` 文档）。
    """

    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    covered_tasks: int
    uncovered_tasks: int


@dataclass(frozen=True)
class MetricCoverageGap:
    """「MCP 指标目录 vs 映射表覆盖面」差集的非空结果。

    旧系统「指标准入」的等价物。只在**存在**未覆盖指标时才会被构造——「无
    差异」由 :func:`build_metric_coverage_gap` 返回 ``None`` 表达，不是一个
    ``uncovered_metric_ids=()`` 的空实例，理由见该函数文档「无差异不报」一节。
    """

    #: 已排序去重的指标 ID：出现在 MCP 目录里、但当前映射表（``company_function_
    #: metric_map.toml``）任何一个「公司+职能」条目都没有覆盖到的那些。
    uncovered_metric_ids: tuple[str, ...]


@dataclass(frozen=True)
class LocalOverrideActivity:
    """本地权限覆盖当日活动与当前生效总量的非空结果。

    管理员经确认卡对个别用户完成的授权/抑制/收回，见迁移 ``0072``/``0073``。
    只在**存在**当日活动或当前有生效条目时才会被构造——「无差异」（当日零
    活动且当前生效总数为零）由 :func:`build_local_override_activity` 返回
    ``None`` 表达，与 :class:`MetricCoverageGap` 同一条「无差异不报」纪律。
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
    #: 投递结果段**独立**的统计窗口，与上面 `window_start`/`window_end` 不是
    #: 同一对值——见 :mod:`lingxi.core.daily_report` 模块文档「投递结果段为什么
    #: 用一个独立、更早的窗口」。即使 `delivery_outcome` 本轮不可判定，这两个
    #: 字段依然必须给出"本来打算问哪个窗口"，供正文标注与测试钉住边界。
    delivery_window_start: datetime
    delivery_window_end: datetime
    #: 「未覆盖新指标」日检。``None``（默认）＝本轮未接线，正文不出现这一段；
    #: ``Section.undetermined(reason)``＝接线了但取数失败，正文出现不可判定说明；
    #: ``Section.of(None)``＝查过、无差异，不出现；``Section.of(gap)``（非空）＝
    #: 存在未覆盖指标，正文出现「待分配」段。放在字段列表末尾给默认值，不打破
    #: 既有调用点的关键字参数构造方式。
    metric_coverage_gap: Section[MetricCoverageGap | None] | None = None
    #: 「本地权限覆盖活动」段，四态语义与上面完全一致（数据源换成本地权限覆盖
    #: 表）：``None``＝未接线不出现；``undetermined``＝取数失败；``of(None)``＝
    #: 查过无变化不出现；``of(activity)``＝存在活动或生效条目，出现该段，只含
    #: 计数，不含 open_id/公司/指标名/理由。
    local_override_activity: Section[LocalOverrideActivity | None] | None = None


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
    """把哑分组行归类为成功/失败/超时/停止/进行中五个桶。"""
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
    """按出现次数降序取失败/停止原因码 Top-N，次数相同按原因码字典序。"""
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
    """窗口内命中 :data:`GUARD_ERROR_KINDS` 的任务总数。"""
    return sum(count for _status, error_kind, count in rows if error_kind in GUARD_ERROR_KINDS)


def build_denied_count_stats(
    *, covered_tasks: int, uncovered_tasks: int, total: int
) -> PartialCount | None:
    """从适配器的哑聚合构造 :class:`PartialCount`。

    见 ``adapters/postgres_daily_report.py::guard_denied_count_stats``；窗口
    内**有**任务但**全部**是 ``NULL``（一个都没覆盖到）时返回 ``None``，调用方
    据此构造 ``Section.undetermined(DENIED_COUNT_ALL_NULL_REASON)``。窗口内
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
    """从适配器的哑聚合构造 :class:`TokenUsageStats`。

    与 :func:`build_denied_count_stats` 同一条判定，对应
    ``adapters/postgres_daily_report.py::token_usage_stats`` 的哑聚合结果。
    """
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
    """把「今天的失败分类 Top」与「此前的连续在榜天数」合并。

    产出节流后的展示行与更新后的连续天数字典。调用方只在**成功送达**之后
    才把返回的字典提交为新状态（与 `RosterAuditDuty` 的
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
    """从原始耗时样本（秒）构造统计摘要。

    无样本返回 ``None``（调用方据此判定「窗口内没有已完成的任务」，与「取不
    到数据」是两回事，不应渲染成不可判定）。
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
    """把哑投递分组行归类为卡片送达/兜底文本送达/过期/待定四个桶。"""
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
    """「MCP 指标目录 vs 映射表覆盖面」差集。

    纯集合运算，不做任何 I/O——调用方负责分别取到这两组指标 ID 再传进来。
    **无差异不报**：每个 ID 都能在 ``mapped_metric_ids`` 里找到时返回
    ``None``，这不是「不可判定」，是「判定过，没有问题」，因此
    :attr:`DailyReportInputs.metric_coverage_gap` 用
    ``Section[MetricCoverageGap | None]``。结果按字符串排序、去重，**不做
    任何大小写或全半角归一**，指标 ID 逐字符透传比对。
    """
    mapped = set(mapped_metric_ids)
    gap = sorted(
        dict.fromkeys(metric_id for metric_id in mcp_metric_ids if metric_id not in mapped)
    )
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
    """从适配器的哑聚合构造 :class:`LocalOverrideActivity`。

    纯集合运算，不做任何 I/O。**无差异不报**：当日零新增且当前生效总数为零
    时返回 ``None``，调用方据此
    不往正文里插入这一段——与 :func:`build_metric_coverage_gap` 同一条「判定过，
    没有问题」纪律，不是「取不到」。``affected_user_count`` 不单独参与这条
    判断：两个方向的生效总数都是零时，覆盖它们的去重用户数结构上也必然是零。
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
