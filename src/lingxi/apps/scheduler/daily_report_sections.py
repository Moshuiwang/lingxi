"""内测每日通报的段落组装与渲染：纯函数，只依赖显式传参，不碰任何实例状态。

从 `daily_report.py` 搬出——这部分逻辑不需要 :class:`~lingxi.apps.scheduler.
daily_report.DailyReportDuty` 的任何实例字段，独立成同包模块把原模块压回
体量棘轮阈值以内。依赖实例状态（`self._fetch`/`self._source`/`self._audit`
等）的取数与收尾逻辑仍留在 `daily_report.py`。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from lingxi.core.daily_report import (
    DENIED_COUNT_ALL_NULL_REASON,
    RESOURCE_USAGE_ALL_NULL_REASON,
    ActiveUserStats,
    DailyReportInputs,
    DeliveryOutcomeRow,
    LocalOverrideActivity,
    MetricCoverageGap,
    PartialCount,
    Section,
    StatusDistribution,
    TaskOutcomeRow,
    TokenUsageStats,
    apply_repeat_throttle,
    build_active_user_stats,
    build_delivery_outcome,
    build_denied_count_stats,
    build_failure_top,
    build_guard_triggered_count,
    build_latency_stats,
    build_local_override_activity,
    build_metric_coverage_gap,
    build_status_distribution,
    build_token_usage_stats,
    render_daily_report,
)


@dataclass(frozen=True)
class _DailyReportRawData:
    """`_fetch_daily_report_raw` 的返回值。

    六段（含两段可选）各自的原始结果与读取失败原因，供
    `_build_daily_report_sections` 转成 Section。
    """

    active_counts: Sequence[int] | None
    active_reason: str | None
    outcome_rows: Sequence[TaskOutcomeRow] | None
    outcome_reason: str | None
    durations: Sequence[float] | None
    latency_reason: str | None
    delivery_rows: Sequence[DeliveryOutcomeRow] | None
    delivery_reason: str | None
    guard_denied_raw: tuple[int, int, int] | None
    guard_denied_fetch_reason: str | None
    token_usage_raw: tuple[int, int, int, int, int, int] | None
    token_usage_fetch_reason: str | None
    metric_coverage_raw: tuple[Sequence[str], Sequence[str]] | None
    metric_coverage_fetch_reason: str | None
    local_override_raw: tuple[int, int, int, int, int, int] | None
    local_override_fetch_reason: str | None


@dataclass(frozen=True)
class _DailyReportSections:
    """`_build_daily_report_sections` 的返回值。

    六段（含两段可选）的 Section 与节流计算结果，供渲染与收尾阶段消费。
    """

    active_users: Section[ActiveUserStats]
    status_distribution: Section[StatusDistribution]
    failure_top: Section
    guard_triggered: Section
    denied_count: Section[PartialCount]
    latency: Section
    delivery_outcome: Section
    resource_usage: Section[TokenUsageStats]
    metric_coverage_gap: Section[MetricCoverageGap | None] | None
    local_override_activity: Section[LocalOverrideActivity | None] | None
    throttled_lines: tuple
    updated_streaks: dict[str, int]


def _build_daily_report_sections(
    raw: _DailyReportRawData,
    today: date,
    *,
    reason_streaks: dict[str, int],
    metric_coverage_wired: bool,
    local_override_activity_wired: bool,
) -> _DailyReportSections:
    """把取数阶段的原始结果转成 Section。

    含两段可选段的 Section，以及节流计算结果。`reason_streaks`/
    `metric_coverage_wired`/`local_override_activity_wired` 是调用方
    （`DailyReportDuty`）实例状态的显式快照，本函数自身不持有任何状态。
    """
    active_users = _section_active_users(raw)
    status_distribution, failure_top, guard_triggered, throttled_lines, updated_streaks = (
        _sections_status_and_throttle(raw, reason_streaks)
    )
    latency, delivery_outcome = _sections_latency_and_delivery(raw)
    denied_count, resource_usage = _sections_denied_and_usage(raw)
    metric_coverage_gap, local_override_activity = _sections_coverage_and_override(
        raw,
        metric_coverage_wired=metric_coverage_wired,
        local_override_activity_wired=local_override_activity_wired,
    )
    return _DailyReportSections(
        active_users=active_users,
        status_distribution=status_distribution,
        failure_top=failure_top,
        guard_triggered=guard_triggered,
        denied_count=denied_count,
        latency=latency,
        delivery_outcome=delivery_outcome,
        resource_usage=resource_usage,
        metric_coverage_gap=metric_coverage_gap,
        local_override_activity=local_override_activity,
        throttled_lines=throttled_lines,
        updated_streaks=updated_streaks,
    )


def _section_active_users(raw: _DailyReportRawData) -> Section[ActiveUserStats]:
    """把 active_users 原始取数结果转成 Section。"""
    if raw.active_reason is not None:
        return Section.undetermined(raw.active_reason)
    return Section.of(build_active_user_stats(raw.active_counts or ()))


def _sections_status_and_throttle(
    raw: _DailyReportRawData, reason_streaks: dict[str, int]
) -> tuple[Section[StatusDistribution], Section, Section, tuple, dict]:
    """状态分布 / 失败 Top / 拦截触发三段，外加节流计算。

    三段共用同一次取数结果；节流状态依赖本轮是否取到失败分类，因此与
    这三段放在一起处理。`reason_streaks` 是调用方当前的节流状态快照。
    """
    if raw.outcome_reason is not None:
        status_distribution = Section.undetermined(raw.outcome_reason)
        failure_top = Section.undetermined(raw.outcome_reason)
        guard_triggered = Section.undetermined(raw.outcome_reason)
        today_top: tuple = ()
        failure_top_determined = False
    else:
        rows = raw.outcome_rows or ()
        status_distribution = Section.of(build_status_distribution(rows))
        today_top = build_failure_top(rows)
        failure_top = Section.of(today_top)
        guard_triggered = Section.of(build_guard_triggered_count(rows))
        failure_top_determined = True

    if failure_top_determined:
        throttled_lines, updated_streaks = apply_repeat_throttle(reason_streaks, today_top)
    else:
        # 本轮取不到失败分类：节流状态原样冻结，不因一次瞬时故障被清零或
        # 提前推进（见 `core.daily_report.apply_repeat_throttle` 的文档）。
        throttled_lines, updated_streaks = (), dict(reason_streaks)

    return status_distribution, failure_top, guard_triggered, throttled_lines, updated_streaks


def _sections_latency_and_delivery(raw: _DailyReportRawData) -> tuple[Section, Section]:
    """延迟统计段 + 投递结果段。

    两段各自独立取数、独立降级，放在一起只是因为都只需要一次 if/else
    判断。
    """
    if raw.latency_reason is not None:
        latency = Section.undetermined(raw.latency_reason)
    else:
        latency = Section.of(build_latency_stats(raw.durations or ()))

    if raw.delivery_reason is not None:
        delivery_outcome = Section.undetermined(raw.delivery_reason)
    else:
        delivery_outcome = Section.of(build_delivery_outcome(raw.delivery_rows or ()))

    return latency, delivery_outcome


def _sections_denied_and_usage(
    raw: _DailyReportRawData,
) -> tuple[Section[PartialCount], Section[TokenUsageStats]]:
    """拦截计数段 + 资源用量段。

    查询本身失败 → 走 `_fetch` 的既有不可判定路径；查询成功但窗口内的
    任务在这个字段上全部是 NULL → 纯函数返回 `None`，这里才降级为不可
    判定（与查询失败是两种不同原因，用不同的 reason 文案区分）。
    """
    if raw.guard_denied_fetch_reason is not None:
        denied_count = Section.undetermined(raw.guard_denied_fetch_reason)
    else:
        covered, uncovered, total = raw.guard_denied_raw
        denied_stats = build_denied_count_stats(
            covered_tasks=covered, uncovered_tasks=uncovered, total=total
        )
        denied_count = (
            Section.of(denied_stats)
            if denied_stats is not None
            else Section.undetermined(DENIED_COUNT_ALL_NULL_REASON)
        )

    if raw.token_usage_fetch_reason is not None:
        resource_usage = Section.undetermined(raw.token_usage_fetch_reason)
    else:
        covered, uncovered, input_tokens, output_tokens, cache_creation, cache_read = (
            raw.token_usage_raw
        )
        usage_stats = build_token_usage_stats(
            covered_tasks=covered,
            uncovered_tasks=uncovered,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation,
            cache_read_input_tokens=cache_read,
        )
        resource_usage = (
            Section.of(usage_stats)
            if usage_stats is not None
            else Section.undetermined(RESOURCE_USAGE_ALL_NULL_REASON)
        )

    return denied_count, resource_usage


def _sections_coverage_and_override(
    raw: _DailyReportRawData,
    *,
    metric_coverage_wired: bool,
    local_override_activity_wired: bool,
) -> tuple[Section[MetricCoverageGap | None] | None, Section[LocalOverrideActivity | None] | None]:
    """「未覆盖新指标」日检 + 「本地权限覆盖活动」段。

    两段都是三态，见 `core/daily_report.py` 的 `DailyReportInputs` 对应
    字段文档：未接线保持 `None`；接线但本轮取数失败 → 不可判定；取数
    成功 → 纯函数判定（差集/活动量为空时 `Section.of(None)`，正文因此
    完全不出现这一段——无差异不报）。`metric_coverage_wired`/
    `local_override_activity_wired` 是调用方对应可选回调"是否接线"的
    显式快照。
    """
    metric_coverage_gap: Section[MetricCoverageGap | None] | None
    if not metric_coverage_wired:
        metric_coverage_gap = None
    elif raw.metric_coverage_fetch_reason is not None:
        metric_coverage_gap = Section.undetermined(raw.metric_coverage_fetch_reason)
    else:
        assert raw.metric_coverage_raw is not None
        mcp_metric_ids, mapped_metric_ids = raw.metric_coverage_raw
        metric_coverage_gap = Section.of(
            build_metric_coverage_gap(mcp_metric_ids, mapped_metric_ids)
        )

    local_override_activity: Section[LocalOverrideActivity | None] | None
    if not local_override_activity_wired:
        local_override_activity = None
    elif raw.local_override_fetch_reason is not None:
        local_override_activity = Section.undetermined(raw.local_override_fetch_reason)
    else:
        assert raw.local_override_raw is not None
        (
            granted_today,
            suppressed_today,
            revoked_today,
            active_grant_total,
            active_suppress_total,
            affected_user_count,
        ) = raw.local_override_raw
        local_override_activity = Section.of(
            build_local_override_activity(
                granted_today=granted_today,
                suppressed_today=suppressed_today,
                revoked_today=revoked_today,
                active_grant_total=active_grant_total,
                active_suppress_total=active_suppress_total,
                affected_user_count=affected_user_count,
            )
        )

    return metric_coverage_gap, local_override_activity


def _render_daily_report_text(
    sections: _DailyReportSections,
    *,
    window_start: datetime,
    window_end: datetime,
    delivery_window_start: datetime,
    delivery_window_end: datetime,
) -> str:
    """把六段 Section 装进 `DailyReportInputs` 并渲染成正文。"""
    inputs = DailyReportInputs(
        window_start=window_start,
        window_end=window_end,
        active_users=sections.active_users,
        status_distribution=sections.status_distribution,
        failure_top=sections.failure_top,
        guard_triggered=sections.guard_triggered,
        denied_count=sections.denied_count,
        latency=sections.latency,
        resource_usage=sections.resource_usage,
        delivery_outcome=sections.delivery_outcome,
        metric_coverage_gap=sections.metric_coverage_gap,
        local_override_activity=sections.local_override_activity,
        delivery_window_start=delivery_window_start,
        delivery_window_end=delivery_window_end,
    )
    return render_daily_report(inputs, throttled_failure_lines=sections.throttled_lines)
