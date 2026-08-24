"""内测每日通报：从任务队列与投递终态聚合出的统计级报告——纯计算与渲染层（Issue #303 S-O-01）。

**这是全仓库唯一渲染「内测每日通报」正文的地方。** 本模块无网络、无数据库依赖：
外部数据一律由调用方（`adapters/postgres_daily_report.py` 读、`apps/scheduler/
daily_report.py` 编排）作为参数传入，符合[代码框架](../../../docs/技术设计/代码框架.md)
「二、三层之间的 import 规则」对 `core/` 的约束。

## 数据从哪来、为什么有的段落恒为「不可判定」

#303 的通报字段清单要求七类统计：活跃用户与逐用户任务量、成功/失败/超时/停止分布、
失败分类 Top、时延分布（对照 #296 的 262 秒基线）、token 用量与成本估算、守卫触发与
拒绝计数、投递结果分布。核对现有数据库设计（`docs/技术设计/数据库设计.md` 第二节）
后发现：**`task` 表与 `task_delivery_event` 表能撑起前面五类里的大部分**（状态、
`error_kind` 失败分类、`started_at`/`ended_at` 时延、`platform_message_kind`/
`platform_received_at`/`expires_at` 投递结果），但 **token 用量与 PreToolUse 拒绝
计数从未落库**——它们只存在于 worker 进程自己的结构化日志行 `worker.task.terminal`
（`resources.usage`、`audit.call_count`/`audit.denied_count`，见
`src/lingxi/apps/worker/service.py`）。scheduler 与 worker 是两个独立部署的进程，
不共享文件系统、不接入任何日志聚合系统（`代码框架.md` 三：「结构化输出到
stdout/stderr，不写日志文件、不自行轮转」）——scheduler **没有任何代码路径能读到**
worker 的这两个字段。

这正是 #303 要求的「不可判定」显式呈现原则要处理的真实场景，不是为了凑一个测试用例
而假造的缺口：`resource_usage`/`denied_count` 两段在当前架构下**恒为**「不可判定」，
原因见 :data:`RESOURCE_USAGE_UNAVAILABLE_REASON`/:data:`DENIED_COUNT_UNAVAILABLE_REASON`。
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
天数，连续第 8 天起收起明细、只报计数与「连续第 N 天在榜」，避免同一批陈旧问题
天天占据管理群的注意力；一旦某天该原因码跌出 Top 榜，连续计数归零，重新出现按
新一轮计。这套状态是调用方（`DailyReportDuty`）持有的进程内字典，与
`RosterAuditDuty._completed_on` 同一条已知残留：**跨重启会清零重新计数**，不持久化
——本 Story 授权范围不含新迁移，且连续 7 天的节流阈值本身足够宽，一次重启不会让
用户看到明显的行为回退。

## 时区：UTC 计窗、正文双标注

统计窗口按 UTC 自然日切分（与 `roster_snapshot`/`task.content_expires_at` 等既有
UTC 约定一致），但正文标题**同时**标注对应的北京时间区间——`docs/技术设计/验收
矩阵.md:608` 已经记录过花名册日报「UTC 日界与北京时间自然日相差 8 小时」在读者
侧造成的歧义（且当时明确留待产品负责人裁定、未处理）。本报告不重复这个歧义：同时
给出两个时区的起止时刻，读者不需要自己心算偏移量。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Generic, TypeVar

T = TypeVar("T")

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

RESOURCE_USAGE_UNAVAILABLE_REASON = (
    "token 用量与成本估算仅存在于 worker 进程的结构化日志（worker.task.terminal 的 "
    "resources.usage 字段），scheduler 与 worker 是独立部署进程、不共享文件系统或日志"
    "聚合通道，当前架构下无法读取；需 worker 侧新增落库字段才能取得，超出本 Story 范围"
)
DENIED_COUNT_UNAVAILABLE_REASON = (
    "工具调用拒绝计数（worker.task.terminal 的 audit.denied_count）同样仅存在于 worker "
    "结构化日志、未落库，原因与 token 用量一致（scheduler 与 worker 不共享日志通道）"
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
class DailyReportInputs:
    """一轮通报要渲染的全部数据，每段各自可能「不可判定」。"""

    window_start: datetime
    window_end: datetime
    active_users: Section[ActiveUserStats]
    status_distribution: Section[StatusDistribution]
    failure_top: Section[tuple[FailureReasonCount, ...]]
    guard_triggered: Section[int]
    denied_count: Section[int]
    latency: Section[LatencyStats]
    resource_usage: Section[str]
    delivery_outcome: Section[DeliveryOutcomeStats]


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


# --------------------------------------------------------------------------
# 渲染：纯函数，输入相同则输出逐字节相同
# --------------------------------------------------------------------------


def _format_window_header(window_start: datetime, window_end: datetime) -> str:
    """窗口起止**各自带日期**，不只写时分——`00:00–00:00` 这种同名时刻的写法会让人
    误读成"零时长窗口"，两端都写全日期才不会歧义（对照花名册日报在
    `docs/技术设计/验收矩阵.md:608` 留下的「UTC 日界与北京自然日相差 8 小时」教训，
    这里两个时区都完整给出，不需要读者自己心算偏移量）。
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
        throttled = by_code.get(entry.reason_code)
        if throttled is not None and throttled.throttled:
            lines.append(
                f"- {entry.reason_code}：{entry.count} 次（连续第 {throttled.streak_days} 天在榜，"
                "已节流，仅计数不再展开明细）"
            )
        else:
            lines.append(f"- {entry.reason_code}：{entry.count} 次")
    return lines


def _render_guard_triggered(section: Section[int]) -> str:
    if not section.is_determined:
        assert section.undetermined_reason is not None
        return f"守卫触发计数（超轮数/超时/收尾超时）：{_render_undetermined(section.undetermined_reason)}"
    return f"守卫触发计数（超轮数/超时/收尾超时）：{section.value} 次"


def _render_denied_count(section: Section[int]) -> str:
    if not section.is_determined:
        assert section.undetermined_reason is not None
        return f"工具调用拒绝计数（PreToolUse 拒绝）：{_render_undetermined(section.undetermined_reason)}"
    return f"工具调用拒绝计数（PreToolUse 拒绝）：{section.value} 次"


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


def _render_resource_usage(section: Section[str]) -> str:
    if not section.is_determined:
        assert section.undetermined_reason is not None
        return f"token 用量与成本估算：{_render_undetermined(section.undetermined_reason)}"
    return f"token 用量与成本估算：{section.value}"


def _render_delivery_outcome(section: Section[DeliveryOutcomeStats]) -> str:
    if not section.is_determined:
        assert section.undetermined_reason is not None
        return f"投递结果分布：{_render_undetermined(section.undetermined_reason)}"
    stats = section.value
    assert stats is not None
    return (
        f"投递结果：成功(卡片) {stats.delivered_card}，兜底(文本) {stats.delivered_fallback_text}，"
        f"过期 {stats.expired}，待定(24h 窗口内) {stats.pending}"
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
        _render_delivery_outcome(inputs.delivery_outcome),
    ]
    return "\n".join(lines)
