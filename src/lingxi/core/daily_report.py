"""内测每日通报：从统计层数据渲染出管理群可读的正文——纯渲染层。

**这是全仓库唯一渲染「内测每日通报」正文的地方。** 本模块无网络、无数据库
依赖：统计数据经 :mod:`lingxi.core.daily_report_stats` 构造好的
:class:`~lingxi.core.daily_report_stats.DailyReportInputs` 传入；该模块的
``build_*``/``Section``/统计 dataclass 从本模块 re-export，外部既有导入路径
不变。

投递结果段使用一个独立、更早的统计窗口（比其余六段整整早一个自然日）：
``task_delivery_event.expires_at`` 的 24 小时确认期在其余段落的窗口里通常
还没关闭，「过期」桶在结构上恒为零，改用更早窗口后才可能统计到真实非零值，
见 :func:`_render_delivery_outcome`。统计窗口按 UTC 自然日切分，正文标题
同时标注北京时间区间。正文没有任何可执行入口：管理群是通知面，不是操作面；
用户标识不出现在正文里，逐用户任务量用匿名分桶呈现。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from lingxi.core.daily_report_stats import (
    DENIED_COUNT_ALL_NULL_REASON as DENIED_COUNT_ALL_NULL_REASON,
)
from lingxi.core.daily_report_stats import (
    RESOURCE_USAGE_ALL_NULL_REASON as RESOURCE_USAGE_ALL_NULL_REASON,
)
from lingxi.core.daily_report_stats import (
    ActiveUserStats,
    DailyReportInputs,
    DeliveryOutcomeStats,
    FailureReasonCount,
    LatencyStats,
    LocalOverrideActivity,
    MetricCoverageGap,
    PartialCount,
    Section,
    StatusDistribution,
    ThrottledFailureLine,
    TokenUsageStats,
)
from lingxi.core.daily_report_stats import DeliveryOutcomeRow as DeliveryOutcomeRow
from lingxi.core.daily_report_stats import TaskOutcomeRow as TaskOutcomeRow
from lingxi.core.daily_report_stats import apply_repeat_throttle as apply_repeat_throttle
from lingxi.core.daily_report_stats import build_active_user_stats as build_active_user_stats
from lingxi.core.daily_report_stats import build_delivery_outcome as build_delivery_outcome
from lingxi.core.daily_report_stats import build_denied_count_stats as build_denied_count_stats
from lingxi.core.daily_report_stats import build_failure_top as build_failure_top
from lingxi.core.daily_report_stats import (
    build_guard_triggered_count as build_guard_triggered_count,
)
from lingxi.core.daily_report_stats import build_latency_stats as build_latency_stats
from lingxi.core.daily_report_stats import (
    build_local_override_activity as build_local_override_activity,
)
from lingxi.core.daily_report_stats import (
    build_metric_coverage_gap as build_metric_coverage_gap,
)
from lingxi.core.daily_report_stats import (
    build_status_distribution as build_status_distribution,
)
from lingxi.core.daily_report_stats import build_token_usage_stats as build_token_usage_stats

#: 失败分类 reason_code 渲染前的形状白名单；已知边界见 :func:`_safe_reason_code`
#: 文档。
_REASON_CODE_PATTERN = re.compile(r"[a-z0-9_]{1,64}")


def _safe_reason_code(reason_code: str) -> str:
    """套一层形状白名单，不匹配的一律归入 ``"other"``，不让形状之外的取值直接进入群发的日报正文。

    **已知边界（不是回归）**：这是形状校验，不是语义校验——挡得住带形状之外
    字符的泄露（大写、CJK、``@``/``.``、空格……），挡不住通篇小写字母/数字/
    下划线的标识符；真实飞书 open_id 标准形态（``ou_`` + 32 位小写十六进制）
    恰好整体落在这个盲区里，对它覆盖率为零。当前全仓 ``error_kind`` 写入点
    均为固定蛇形小写字面量；若未来引入动态取值，必须先把白名单改为枚举已知
    集合，不能继续依赖字符集校验（见 ``tests/test_daily_report_render.py``
    对这条边界的记录）。
    """
    if isinstance(reason_code, str) and _REASON_CODE_PATTERN.fullmatch(reason_code):
        return reason_code
    return "other"


#: 失败分类原因码 → 中文：管理员看不出英文机器码对应哪一类真实故障，沿用
#: ``core/admin/router.py`` 同一套显示名映射姿势（白名单式展示层翻译，未登记
#: 的取值回退成「原值（未登记显示名）」，不崩、不假装认识）。``"other"`` 是
#: :func:`_safe_reason_code` 自己的安全哨兵值，映射到它自身、不参与"未登记"
#: 回退——它已经是一个管理员看得懂的英文词。
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
    """把已经过 :func:`_safe_reason_code` 白名单校验的原因码翻译成中文显示名。

    未登记的取值（词表遗漏，或未来新增但没有同步登记）回退成「原值（未登记
    显示名）」，与 ``core/admin/router.py`` ``_display_or_unregistered`` 同一
    样式。
    """
    label = _ERROR_KIND_LABEL.get(safe_reason_code)
    if label is None:
        return f"{safe_reason_code}（未登记显示名）"
    return label


#: 调用次数基线对照恒为不可判定：MCP 调用次数只见于 worker 结构化日志的
#: ``audit.call_count`` 字段，未落库，无法对照。
CALL_COUNT_BASELINE_UNAVAILABLE_REASON = (
    "MCP 调用次数（对照 #296 的 16 次/任务基线）同样仅见于 worker 结构化日志的 "
    "audit.call_count 字段，未落库，无法对照"
)

_BEIJING_OFFSET = timedelta(hours=8)


def _format_window_header(window_start: datetime, window_end: datetime) -> str:
    """窗口起止**各自带日期**，不只写时分——避免读者误读成"零时长窗口"。

    两端都写全日期才不会歧义，且两个时区都完整给出，不需要读者自己心算
    偏移量。
    """
    utc_start = window_start.astimezone(UTC)
    utc_end = window_end.astimezone(UTC)
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
        # 同一个键），渲染进正文的展示值另外过一遍形状白名单——两者故意分开，
        # 只有拼进正文这一步换成人话，不影响任何既有的键值逻辑。
        throttled = by_code.get(entry.reason_code)
        safe_code = _safe_reason_code(entry.reason_code)
        display_code = _humanize_error_kind(safe_code)
        if throttled is not None and throttled.throttled:
            # 节流行必须真的更短，不是比未节流行长：未节流的行本来就从不展开
            # 任何明细，只保留读者真正需要的两个新增信息——连续在榜天数、已节流。
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
    """``uncovered_tasks > 0`` 时附加的覆盖度说明。

    告诉读者这个数字不是窗口内全部任务的准确总和，还有多少个任务因为字段
    缺失没有参与求和。``uncovered_tasks == 0`` 时不附加任何说明，避免给
    完全覆盖的正常情形也画蛇添足。
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
    note = _render_coverage_note(
        covered_tasks=stats.covered_tasks, uncovered_tasks=stats.uncovered_tasks
    )
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
    # 调用次数基线对照恒为不可判定，与「本段是否查到时延样本」无关——即使窗口内
    # 没有任何已完成任务，这条结构性缺口依然存在，两种情形都要出现这一行。
    call_count_note = f"调用次数对照（#296 基线 16 次/任务）：{_render_undetermined(CALL_COUNT_BASELINE_UNAVAILABLE_REASON)}"
    return f"{header}{body}\n{call_count_note}"


def _render_resource_usage(section: Section[TokenUsageStats]) -> str:
    # 标题不写"与成本估算"：本段是 task.token_usage 的真实聚合，但没有接入
    # 任何模型定价表——展示的是原始 token 计数，不是货币成本，标题不能承诺
    # 一个本模块没有计算的数字。
    label = "token 用量"
    if not section.is_determined:
        assert section.undetermined_reason is not None
        return f"{label}：{_render_undetermined(section.undetermined_reason)}"
    stats = section.value
    assert stats is not None
    note = _render_coverage_note(
        covered_tasks=stats.covered_tasks, uncovered_tasks=stats.uncovered_tasks
    )
    body = (
        f"input={stats.input_tokens}，output={stats.output_tokens}，"
        f"cache_creation={stats.cache_creation_input_tokens}，"
        f"cache_read={stats.cache_read_input_tokens}"
    )
    return f"{label}：{body}{note}"


def _format_delivery_window_note(window_start: datetime, window_end: datetime) -> str:
    """投递结果段的窗口标注，刻意不复用 `_format_window_header` 的原样输出。

    这一段的窗口本来就与页首、以及其余全部段落不同（见模块文档「投递结果段
    为什么用一个独立、更早的窗口」），必须让读者一眼看出"这段说的是哪一天"，
    不能默认它跟页首那一行是同一个窗口。
    """
    utc_start = window_start.astimezone(UTC)
    utc_end = window_end.astimezone(UTC)
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


def _render_metric_coverage_gap(section: Section[MetricCoverageGap | None] | None) -> str:
    """「待分配」段。返回空字符串表示**这一段完全不出现**。

    与其余段落不同，本段允许彻底不出现，见
    :attr:`DailyReportInputs.metric_coverage_gap` 的三态说明。未接线
    （``section is None``）与「接线了、查过、没有差异」
    （``section.value is None``）都返回空字符串。
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
    section: Section[LocalOverrideActivity | None] | None,
) -> str:
    """「本地权限覆盖活动」段。返回空字符串表示这一段完全不出现。

    未接线与查过但当日无变化且当前无生效条目都返回空字符串。正文只含计数：
    不含 open_id、公司 ID、指标名或理由文本，与 `V-花名册-34`
    的隐私纪律同向。用「本窗口」不用「今日」——本段统计的实际是与其余六段
    同一个 UTC 窗口。用「登记」不用「生效」：三个笔数只是对
    ``local_permission_override`` 表本身的如实计数，额外单独注明生效口径
    （开通链已生效，重算侧待每日重算恢复运行），不笼统断言「生效」。三个
    计数名字与管理卡/确认卡的 ``_ACTION_LABEL`` 同一份口径。
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
