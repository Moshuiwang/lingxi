"""内测每日通报职责：:class:`DailyReportDuty`（Issue #303 S-O-01）。

一轮做三件事：分四段独立读取昨日（UTC 自然日）统计 → 纯函数聚合与节流 → 该发就发
一条到管理群。形状照 `apps/scheduler/roster_audit.py` 的 :class:`~lingxi.apps.
scheduler.roster_audit.RosterAuditDuty`（同一天至多一次的判重水位、发送失败不置位、
同一天的重试共用一个去重键），但**多了一条 RosterAuditDuty 没有的规则**：四段数据源
各自独立失败，只让对应的段落显式「不可判定」，不拖累其余段落，也不拖累整轮发送
——这是 #303 明确要求、而花名册日报当前不需要的行为（它的判重逻辑是「整轮读不出来
就整轮重试」，不是「分段降级」）。

## 判重水位与节流状态都是进程内存

`_completed_on`（今天发过了没有）与 `_reason_streaks`（失败分类 Top 每个原因码
连续在榜几天）都只在这个职责实例的内存里，与 `RosterAuditDuty._completed_on` 同一条
已知残留：**重启后水位清零，当天可能重发一份内容相近的通报**；`_reason_streaks`
重启后从零重新计数，节流的效果会短暂消失几天再重新生效。两者都是 2026-08-06
产品负责人对花名册日报同一形状残留的知情接受（裁定 C2 / R2）的自然延伸——真幂等
需要给判重水位与节流状态各自的持久列，属新迁移，不在本 Story 授权范围内。

## 送达失败不静默

发送异常时记一条 `daily_report.send_failed` 审计（只记异常类型，不记正文——正文
虽已经过统计级脱敏，但它是给管理群的，不是给运维日志的，同花名册日报 `V-花名册-33`
同一条纪律）并升级为 `logger.error`；水位与节流状态都**不**提交，下一轮照常重试。
真正让「送达失败不静默」成立的是**调用方注入的 `on_send_outcome` 回调**（装配时
接到 `core/alerting.py` 的 `AlertingDuty.send_outcome_callback()`，与花名册日报
共用同一条已验证的告警接线，见 `apps/scheduler/assembly.py`）——发送失败因此既有
审计留痕，也会真的触发管理员可见的运行告警，不是仅仅记一行没人看的日志。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

from lingxi.core.daily_report import (
    DENIED_COUNT_UNAVAILABLE_REASON,
    RESOURCE_USAGE_UNAVAILABLE_REASON,
    ActiveUserStats,
    DailyReportInputs,
    DeliveryOutcomeRow,
    Section,
    StatusDistribution,
    TaskOutcomeRow,
    apply_repeat_throttle,
    build_active_user_stats,
    build_delivery_outcome,
    build_failure_top,
    build_guard_triggered_count,
    build_latency_stats,
    build_status_distribution,
    render_daily_report,
)

from lingxi.apps.scheduler.audit import AuditSink
from lingxi.apps.scheduler.config import SchedulerConfig

logger = logging.getLogger(__name__)


class _DailyStatsSource(Protocol):
    """四段独立统计的读取口；实现是
    :class:`~lingxi.adapters.postgres_daily_report.PostgresDailyReportSource`。
    职责只依赖这个签名，因此全部调度、节流与「不可判定」断言都能在没有数据库、
    没有网络的机器上跑完。
    """

    def active_user_task_counts(
        self, *, window_start: datetime, window_end: datetime
    ) -> Sequence[int]: ...

    def task_outcomes(
        self, *, window_start: datetime, window_end: datetime
    ) -> Sequence[TaskOutcomeRow]: ...

    def task_durations_seconds(
        self, *, window_start: datetime, window_end: datetime
    ) -> Sequence[float]: ...

    def delivery_outcomes(
        self, *, window_start: datetime, window_end: datetime
    ) -> Sequence[DeliveryOutcomeRow]: ...


class _GroupSender(Protocol):
    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None: ...


class DailyReportDuty:
    """内测每日统计通报（Issue #303 S-O-01）。"""

    name = "内测每日通报"

    def __init__(
        self,
        *,
        source: _DailyStatsSource,
        sender: _GroupSender,
        audit: AuditSink,
        chat_id: str,
        clock: Callable[[], datetime] | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        self._source = source
        self._sender = sender
        self._audit = audit
        self._chat_id = chat_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._stop = threading.Event() if stop is None else stop
        self._completed_on: date | None = None
        self._reason_streaks: dict[str, int] = {}

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    @property
    def completed_on(self) -> date | None:
        """已完成通报的那一天。``None`` 表示本进程实例今天还没发过。"""

        return self._completed_on

    def request_stop(self) -> None:
        self._stop.set()

    def _fetch(self, section: str, factory):
        """跑一段数据源；失败时留一条**恰一条**的审计，返回 ``(结果, 原因)``。
        `结果 is None and 原因 is not None` 表示这一段这一轮取不到（`core.daily_report.
        Section.undetermined` 的输入），其余段落不受影响——异常只吞在这一层，绝不
        向上冒泡带走整轮通报。
        """

        try:
            return factory(), None
        except Exception as error:  # noqa: BLE001 - 单段失败不得带走其余段落
            reason = f"{section} 本轮读取失败（{type(error).__name__}）"
            # 动作名以 `failed` 结尾，被 `StructuredLogAuditSink` 自动升级到 WARNING
            # （`apps/scheduler/audit.py` 的既有规则，与 `roster_audit.send_failed`
            # 同一条纪律）——数据源本轮读取失败值得被立刻看见，不该淹在 INFO 流水里。
            self._audit.record(
                "daily_report.section_read_failed", section=section, error=type(error).__name__
            )
            logger.warning(
                "内测每日通报：%s 段本轮读取失败，标记为不可判定，其余段落照常 error=%s",
                section,
                type(error).__name__,
            )
            return None, reason

    def run_once(self) -> str | None:
        """跑一轮。返回 ``None`` 表示本轮没有发送（停止中，或今天已经做完）；
        否则返回已发送的正文（供测试断言）。"""

        if self._stop.is_set():
            return None
        now = self._clock()
        today = now.date()
        if self._completed_on == today:
            return None

        # 统计窗口固定为「昨天」的完整 UTC 自然日：与花名册审计同样的 UTC 日界
        # 约定（`roster_report.py`「时间一律 UTC 标注」），取昨天而不是「今天已过去
        # 的部分」，是为了让每一份通报覆盖的都是一个**完整**的自然日，不会因为
        # scheduler 首轮 tick 落在当天早晚不同时刻而让统计口径每天不一样。
        window_end = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
        window_start = window_end - timedelta(days=1)

        active_counts, active_reason = self._fetch(
            "active_users",
            lambda: self._source.active_user_task_counts(
                window_start=window_start, window_end=window_end
            ),
        )
        outcome_rows, outcome_reason = self._fetch(
            "task_outcomes",
            lambda: self._source.task_outcomes(window_start=window_start, window_end=window_end),
        )
        durations, latency_reason = self._fetch(
            "latency",
            lambda: self._source.task_durations_seconds(
                window_start=window_start, window_end=window_end
            ),
        )
        delivery_rows, delivery_reason = self._fetch(
            "delivery_outcome",
            lambda: self._source.delivery_outcomes(window_start=window_start, window_end=window_end),
        )

        if self._stop.is_set():
            # 停止信号可能落在四段读取期间到达：干净中断，不发送、不置位、不提交
            # 节流状态，与 RosterAuditDuty 的同一条纪律（停止之后必须 0 次发送）。
            logger.info("停止信号在通报数据读取期间到达，本轮不发送")
            return None

        active_users: Section[ActiveUserStats]
        if active_reason is not None:
            active_users = Section.undetermined(active_reason)
        else:
            active_users = Section.of(build_active_user_stats(active_counts or ()))

        status_distribution: Section[StatusDistribution]
        if outcome_reason is not None:
            status_distribution = Section.undetermined(outcome_reason)
            failure_top = Section.undetermined(outcome_reason)
            guard_triggered = Section.undetermined(outcome_reason)
            today_top: tuple = ()
            failure_top_determined = False
        else:
            rows = outcome_rows or ()
            status_distribution = Section.of(build_status_distribution(rows))
            today_top = build_failure_top(rows)
            failure_top = Section.of(today_top)
            guard_triggered = Section.of(build_guard_triggered_count(rows))
            failure_top_determined = True

        if failure_top_determined:
            throttled_lines, updated_streaks = apply_repeat_throttle(self._reason_streaks, today_top)
        else:
            # 本轮取不到失败分类：节流状态原样冻结，不因一次瞬时故障被清零或
            # 提前推进（见 `core.daily_report.apply_repeat_throttle` 的文档）。
            throttled_lines, updated_streaks = (), dict(self._reason_streaks)

        if latency_reason is not None:
            latency = Section.undetermined(latency_reason)
        else:
            latency = Section.of(build_latency_stats(durations or ()))

        if delivery_reason is not None:
            delivery_outcome = Section.undetermined(delivery_reason)
        else:
            delivery_outcome = Section.of(build_delivery_outcome(delivery_rows or ()))

        # 这两段在当前架构下**恒为**不可判定：token 用量与 PreToolUse 拒绝计数只存在
        # 于 worker 进程自己的结构化日志，scheduler 没有任何代码路径能读到（见
        # `core/daily_report.py` 模块文档「数据从哪来」一节的完整理由）。不是本轮
        # 查询失败，是这条数据源在这个架构下压根不存在，因此不经过 `_fetch`。
        resource_usage = Section.undetermined(RESOURCE_USAGE_UNAVAILABLE_REASON)
        denied_count = Section.undetermined(DENIED_COUNT_UNAVAILABLE_REASON)

        inputs = DailyReportInputs(
            window_start=window_start,
            window_end=window_end,
            active_users=active_users,
            status_distribution=status_distribution,
            failure_top=failure_top,
            guard_triggered=guard_triggered,
            denied_count=denied_count,
            latency=latency,
            resource_usage=resource_usage,
            delivery_outcome=delivery_outcome,
        )
        text = render_daily_report(inputs, throttled_failure_lines=throttled_lines)

        if self._stop.is_set():
            logger.info("停止信号在通报渲染完成后到达，本轮不发送")
            return None

        try:
            # 同一天的通报（含失败重试）共用一个去重键：不确定态下的重试因此携带
            # 同一个投递 `uuid`，由飞书服务端去重，与花名册日报同一条纪律
            # （`RosterAuditDuty` 文档字符串「重试与"不确定态"」一节）。
            self._sender.send_text(
                chat_id=self._chat_id, text=text, dedupe_key=f"daily-report:{today.isoformat()}"
            )
        except Exception as error:  # noqa: BLE001 - 发送失败不得带走同一轮的其他职责
            self._audit.record(
                "daily_report.send_failed",
                report_date=today.isoformat(),
                error=type(error).__name__,
            )
            logger.error("内测每日通报发送失败，下一轮重试 error=%s", type(error).__name__)
            return None

        self._audit.record(
            "daily_report.sent",
            report_date=today.isoformat(),
            active_users=active_users.value.active_user_count if active_users.is_determined else None,
            undetermined_sections=[
                name
                for name, section in (
                    ("active_users", active_users),
                    ("status_distribution", status_distribution),
                    ("failure_top", failure_top),
                    ("latency", latency),
                    ("delivery_outcome", delivery_outcome),
                )
                if not section.is_determined
            ],
        )
        self._completed_on = today
        self._reason_streaks = updated_streaks
        logger.info(
            "内测每日通报已发送 统计窗口=%s~%s 字符数=%s",
            window_start.date().isoformat(),
            window_end.date().isoformat(),
            len(text),
        )
        return text


def _build_daily_report_duty(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
    on_send_outcome: Callable[[str, bool], None] | None = None,
) -> DailyReportDuty | None:
    """装配内测每日通报职责；前置不齐就**不注册**并留下**恰一条**审计，返回 ``None``。

    **住在本文件而不是 `apps/scheduler/assembly.py`**：assembly.py 已经触及体量棘轮
    上限（`scripts/ci/size_ratchet_baseline.txt` 登记的 1535 行，规则是「已超过阈值
    的文件只许变小不许变大」），把装配函数留在职责自己的模块里既不违反棘轮，也让
    「这个职责怎么被造出来」与「它自己是什么」物理相邻——`apps/scheduler/roster_audit.py`
    没有这样做只是历史先例，不是必须遵守的形状。`build_loop`（`assembly.py`）像装配
    其余职责一样直接导入并调用本函数，调用方感知不到这个区别。

    **唯一前置是管理群 chat_id**，形状照 `_build_roster_audit_duty`（`V-花名册-29` 的
    同一条纪律：缺项只报变量名、审计恰一条、其余职责照常运行）——但理由不同：花名册
    那三个前置里有两个是外部 Base 坐标（要连去哪张表），本职责只读 Lingxi 自己的
    ``task``/``task_delivery_event`` 两张表，两者都已经在 ``config.postgres_dsn`` 这个
    进程级必需配置里，不需要任何额外的外部标识。它唯一服务的目的地就是管理群，没有
    目的地也就没有必要跑四段查询。

    ``on_send_outcome`` 复用与花名册日报**同一条**已验证的告警接线
    （``alerting_duty.send_outcome_callback()``，见 ``build_loop`` 调用点）：两个职责
    都通过 :class:`~lingxi.adapters.feishu_group_message.FeishuGroupMessages` 发送，
    送达失败因此走同一套 `FEISHU_SEND_FAILED` 告警通道，不新建告警类型。
    """

    if not config.admin_group_chat_id:
        # 只报变量名，不回显任何值（`V-花名册-29` 的同一条纪律）。
        audit.record(
            "daily_report.duty_not_registered",
            reason="missing_environment_variable",
            variable="LINGXI_ADMIN_GROUP_CHAT_ID",
        )
        logger.warning(
            "未配置 LINGXI_ADMIN_GROUP_CHAT_ID，内测每日通报职责不注册；其余定时职责照常运行"
        )
        return None

    from lingxi.adapters.feishu_group_message import DAILY_REPORT_UUID_PREFIX, FeishuGroupMessages
    from lingxi.adapters.postgres_daily_report import PostgresDailyReportSource

    return DailyReportDuty(
        source=PostgresDailyReportSource(config.postgres_dsn, timeouts=config.postgres_timeouts),
        sender=FeishuGroupMessages(
            base_url=config.feishu_base_url,
            app_id=config.feishu_app_id,
            app_secret=config.feishu_app_secret,
            on_send_outcome=on_send_outcome,
            # 独立去重前缀（见 `FeishuGroupMessages.__init__` 的 `uuid_prefix` 文档）：
            # 与花名册日报共用同一个群、同一个飞书接口，但必须是两条互不干扰的投递
            # 语义，否则同一天两边都用日期当去重键会被飞书服务端误判成同一条。
            uuid_prefix=DAILY_REPORT_UUID_PREFIX,
        ),
        audit=audit,
        chat_id=config.admin_group_chat_id,
        stop=stop,
    )


def _wire_daily_report_duty(
    duties: list,
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
    alerting_duty,
) -> None:
    """装配并按需追加进 ``duties``——把 ``build_loop`` 里"造出来、非 None 才追加"这
    两行标准动作也收进本文件，让 ``assembly.py`` 的调用点只占**一行**（体量棘轮
    上限逼出来的形状，见 :func:`_build_daily_report_duty` 文档字符串「住在本文件」
    一节）；其余职责在 ``assembly.py`` 里仍然是显式的 ``if x is not None:
    duties.append(x)`` 两行，本函数不改变那个惯例，只是让**这一个**新增职责不必
    再占用 assembly.py 的行数预算。
    """

    duty = _build_daily_report_duty(
        config,
        stop=stop,
        audit=audit,
        on_send_outcome=(alerting_duty.send_outcome_callback() if alerting_duty else None),
    )
    if duty is not None:
        duties.append(duty)
