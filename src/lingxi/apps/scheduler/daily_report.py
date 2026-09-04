"""内测每日通报职责：:class:`DailyReportDuty`。

一轮做三件事：分六段独立读取统计（四段读昨日、``denied_count``/``resource_usage``
同窗口，投递结果段独立读前天，见 `core/daily_report.py`）→ 纯函数聚合与节流 →
该发就发一条到管理群。形状照花名册日报（同一天至多一次的判重水位、发送失败不
置位、重试共用去重键），但多一条规则：六段数据源各自独立失败，只让对应段落
显式「不可判定」，不拖累其余段落与整轮发送。**判重水位持久化，节流状态仍在
进程内存**：发送成功后写一行水位到数据库，跨重启也能判重；``_reason_streaks``
（失败分类连续在榜天数）仍只在内存里，跨重启会短暂失效再恢复，是已知接受的
残留。**聚合挪出主线程**：六段聚合＋渲染＋发送整体派进后台线程，避免慢聚合把
心跳评估时钟顶到阈值外、触发假心跳告警；同一时刻至多一个后台聚合在飞。发送
失败记审计并升级为 WARNING，经与花名册日报共用的告警接线触发运行告警，不静默。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from lingxi.apps.scheduler.audit import AuditSink
from lingxi.apps.scheduler.config import SchedulerConfig
from lingxi.apps.scheduler.daily_report_sections import (
    _build_daily_report_sections,
    _DailyReportRawData,
    _DailyReportSections,
    _render_daily_report_text,
)
from lingxi.core.daily_report import DeliveryOutcomeRow, Section, TaskOutcomeRow

logger = logging.getLogger(__name__)


#: 安全的发送失败原因分类。只记异常**类名**（`type(error).__name__`）不够——
#: 运维要靠类名自己猜"这是我们自己的 uuid 预算算错了，还是飞书/网络那一侧的
#: 问题"——两类问题的处置完全不同（前者是代码 bug，后者多半会自愈重试）。分类
#: 只看异常**类型**，不看异常消息文本（消息文本可能带正文片段），因此这个
#: 分类本身不含任何敏感值。
def _classify_send_failure(error: Exception) -> str:
    from lingxi.adapters.feishu_group_message import FeishuGroupMessageError

    if isinstance(error, ValueError):
        # delivery_uuid() 唯一会抛的异常类型：前缀非法，或折算出的投递去重 ID
        # 超过飞书 50 字符上限，见 adapters/feishu_group_message.py 的
        # DAILY_REPORT_UUID_PREFIX 登记。
        return "uuid_budget"
    if isinstance(error, FeishuGroupMessageError):
        # 令牌获取失败、飞书业务错误码、响应形状不对——都是"消息确实发出去过
        # 请求，但没能成功"的传输/平台层问题。
        return "transport"
    # 没有归类成 "render"：本职责的 `render_daily_report(...)` 调用发生在这个
    # try 块**之外**（渲染失败会直接让 run_once 整体抛出，不会走到这条
    # send_failed 审计），因此这里不虚构一个结构上到不了的分类，宁可归入更诚实
    # 的 other，也不假装能区分一个实际不可达的类别。
    return "other"


#: 后台聚合线程的主线程等待上限（见类文档「聚合挪出主线程」）。**不是聚合
#: 本身的超时**，只控制调用方（scheduler 主循环所在的线程，心跳与
#: `AlertingDuty` 的心跳评估都在上面）愿意为这一轮等多久才拿回控制权。取值
#: 远小于 `AlertPolicy.heartbeat_timeout_seconds` 默认值（120 秒），也远大于
#: 测试用假数据源的实际耗时，因此既有测试都能在这个窗口内拿到同步返回值。
DEFAULT_AGGREGATION_JOIN_TIMEOUT_SECONDS = 2.0


class _DailyStatsSource(Protocol):
    """四段独立统计的读取口。

    实现是 :class:`~lingxi.adapters.postgres_daily_report.PostgresDailyReportSource`。
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

    def guard_denied_count_stats(
        self, *, window_start: datetime, window_end: datetime
    ) -> tuple[int, int, int]: ...

    def token_usage_stats(
        self, *, window_start: datetime, window_end: datetime
    ) -> tuple[int, int, int, int, int, int]: ...


class _LocalOverrideActivitySource(Protocol):
    """「本地权限覆盖活动」段的可选取数口。

    实现是 :meth:`~lingxi.adapters.postgres_local_permission.
    PostgresLocalPermissionOverrideStore.daily_activity_stats`。与
    `metric_coverage`（下方 `DailyReportDuty.__init__` 的同名参数）同一条
    「可选取数回调」纪律，但本段需要统计窗口——`metric_coverage` 问的是"当前"
    的全局覆盖面差集，不随统计窗口变化；本段问的是"这一天"发生了什么，因此
    签名比 `metric_coverage` 多了 `window_start`/`window_end` 两个参数，与
    `_DailyStatsSource` 其余六个方法同一个窗口口径。
    """

    def __call__(
        self, *, window_start: datetime, window_end: datetime
    ) -> tuple[int, int, int, int, int, int]: ...


class _GroupSender(Protocol):
    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None: ...


class _SentWatermark(Protocol):
    """通报送达水位的持久化端口。

    实现是 :class:`~lingxi.adapters.postgres_daily_report_watermark.
    PostgresDailyReportWatermark`。职责只依赖这个签名，因此跨重启判重断言
    也能在没有数据库、没有网络的机器上跑完。
    """

    def already_sent(self, *, report_date: date, chat_id: str) -> bool: ...

    def mark_sent(self, *, report_date: date, chat_id: str) -> None: ...


class DailyReportDuty:
    """内测每日统计通报。"""

    name = "内测每日通报"

    def __init__(
        self,
        *,
        source: _DailyStatsSource,
        watermark: _SentWatermark,
        sender: _GroupSender,
        audit: AuditSink,
        chat_id: str,
        clock: Callable[[], datetime] | None = None,
        stop: threading.Event | None = None,
        aggregation_join_timeout_seconds: float = DEFAULT_AGGREGATION_JOIN_TIMEOUT_SECONDS,
        metric_coverage: Callable[[], tuple[Sequence[str], Sequence[str]]] | None = None,
        local_override_activity: _LocalOverrideActivitySource | None = None,
    ) -> None:
        """按注入的数据源/发送器/水位装配一个内测每日通报职责实例。"""
        self._source = source
        self._watermark = watermark
        self._sender = sender
        self._audit = audit
        self._chat_id = chat_id
        self._clock = clock or (lambda: datetime.now(UTC))
        self._stop = threading.Event() if stop is None else stop
        self._aggregation_join_timeout_seconds = aggregation_join_timeout_seconds
        self._completed_on: date | None = None
        self._reason_streaks: dict[str, int] = {}
        # 上一次派发的后台聚合线程（见类文档「聚合挪出主线程」）。``None`` 或
        # 已经跑完都表示"可以派发下一轮"；还活着就说明上一轮还没收工，本轮不
        # 重复派发。
        self._pending_thread: threading.Thread | None = None
        # 「未覆盖新指标」日检。**可选，默认 None**——不接线时这一段不出现在
        # 正文里。**取数含一次真实 MCP 网络调用**，只会在后台聚合线程里被
        # `_fetch` 调用，绝不会被 `run_once` 本身（scheduler 主循环所在的
        # 线程）直接调用，因此不会重新引入"耗时职责挤占心跳评估"问题。
        self._metric_coverage = metric_coverage
        # 「本地权限覆盖活动」段。**可选，默认 None**——与 `metric_coverage`
        # 同一条纪律：不接线时这一段完全不出现在正文里。取数只查 Lingxi 自己
        # 的表，不含网络调用，但仍然只会在后台聚合线程里被 `_fetch` 调用，
        # 不会被 `run_once` 本身直接调用，理由同上（保持两段接线方式一致，
        # 便于阅读）。
        self._local_override_activity = local_override_activity

    @property
    def stopping(self) -> bool:
        """是否已收到停止信号。"""
        return self._stop.is_set()

    @property
    def completed_on(self) -> date | None:
        """已完成通报的那一天（本进程内观察到的事实）。

        可能是本进程自己发的，也可能是读到持久水位后才确认"已经发过"（见类
        文档「判重水位持久化」）。``None`` 表示本进程实例还没确认今天已经
        处理完。
        """
        return self._completed_on

    def request_stop(self) -> None:
        """置位停止信号：本轮及之后不再发起新的聚合。"""
        self._stop.set()

    def _fetch(self, section: str, factory):
        """跑一段数据源；失败时留一条**恰一条**的审计，返回 ``(结果, 原因)``。

        `结果 is None and 原因 is not None` 表示这一段这一轮取不到
        （`core.daily_report.Section.undetermined` 的输入），其余段落不受
        影响——异常只吞在这一层，绝不向上冒泡带走整轮通报。
        """
        try:
            return factory(), None
        except Exception as error:  # 单段失败不得带走其余段落
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
        """跑一轮，返回已发送正文或 ``None``。

        ``None`` 表示本轮没有发送（停止中、今天已经做完，或本轮只是确认了
        一次"后台聚合还在跑，调用方不等"）；否则返回已发送的正文（供测试
        断言）。聚合速度远慢于 `_aggregation_join_timeout_seconds` 时，
        `run_once` 会先返回 `None`，真正的发送与收尾在后台线程里完成（见类
        文档「聚合挪出主线程」）。
        """
        if self._stop.is_set():
            return None
        now = self._clock()
        today = now.date()
        if self._completed_on == today:
            return None
        if self._pending_thread is not None and self._pending_thread.is_alive():
            # 上一次派发的后台聚合还没收工：不重复派发——两轮聚合同时读库、
            # 同时可能尝试发送，是比"聚合慢"本身更糟的形状。下一轮再看。
            return None

        if self._already_sent_today(today):
            return None

        window_start, window_end, delivery_window_start, delivery_window_end = (
            self._compute_report_windows(today)
        )
        return self._dispatch_aggregation(
            today,
            window_start=window_start,
            window_end=window_end,
            delivery_window_start=delivery_window_start,
            delivery_window_end=delivery_window_end,
        )

    def _already_sent_today(self, today: date) -> bool:
        """查持久水位；命中则补齐内存水位并返回 ``True``（本轮应跳过）。

        查询失败时保守跳过本轮、不冒泡——这是主线程里唯一没有被 `_fetch` 那套
        「单段失败」保护包住的库调用，不包会被循环层的通用 except 静默吞掉、
        留不下任何 `daily_report.*` 审计。查不到持久水位就无法确认"这个窗口
        是不是已经发过"，两种猜测都有代价：当作"没发过"继续跑，若水位其实
        已经写过就会重复发送；当作"已经发过"跳过，若水位其实没写就会晚发
        一轮。选保守跳过——宁可晚发、不可重发，与类文档「判重水位持久化」
        的幂等纪律方向一致，下一轮（含很快重试的下一次 tick）照常再查一次。
        """
        try:
            already_sent = self._watermark.already_sent(chat_id=self._chat_id, report_date=today)
        except Exception as error:  # 查不了水位时保守跳过本轮，不冒泡
            self._audit.record("daily_report.watermark_check_failed", error=type(error).__name__)
            logger.warning(
                "内测每日通报：持久水位查询失败，保守跳过本轮，下一轮重试 error=%s",
                type(error).__name__,
            )
            return True

        if already_sent:
            # 持久水位显示本窗口已经发过——可能是本进程更早一轮成功后设的，也
            # 可能是重启前的旧进程设的。直接补齐内存水位，不重新聚合、不重新
            # 发送。
            self._completed_on = today
            return True
        return False

    def _compute_report_windows(self, today: date) -> tuple[datetime, datetime, datetime, datetime]:
        """算出本轮统计窗口与投递结果段的独立窗口。

        统计窗口固定为「昨天」的完整 UTC 自然日：与花名册审计同样的 UTC 日界
        约定，取昨天而不是「今天已过去的部分」，是为了让每一份通报覆盖的都是
        一个**完整**的自然日，不会因为 scheduler 首轮 tick 落在当天早晚不同
        时刻而让统计口径每天不一样。投递结果段独立用**再早一天**的窗口：
        `[D-1, D)` 里的投递行，24 小时确认期到今天通报运行时还没关闭，「过期」
        这一桶在那个窗口下结构上恒为零。
        """
        window_end = datetime(today.year, today.month, today.day, tzinfo=UTC)
        window_start = window_end - timedelta(days=1)
        delivery_window_end = window_start
        delivery_window_start = delivery_window_end - timedelta(days=1)
        return window_start, window_end, delivery_window_start, delivery_window_end

    def _dispatch_aggregation(
        self,
        today: date,
        *,
        window_start: datetime,
        window_end: datetime,
        delivery_window_start: datetime,
        delivery_window_end: datetime,
    ) -> str | None:
        """派发后台聚合线程，只等一个很短的上限就拿回控制权。

        聚合仍在跑时不再等——聚合耗时不得占用调用方所在的线程（scheduler
        主循环：心跳与其余职责，含 AlertingDuty 的心跳评估，都在这条线程
        上）。后台线程会自己收尾（水位、审计、`_completed_on`），下一轮由
        持久水位或内存 `_completed_on` 观察到。
        """
        result: dict[str, str | None] = {}

        def worker() -> None:
            try:
                result["text"] = self._aggregate_and_send(
                    today,
                    window_start=window_start,
                    window_end=window_end,
                    delivery_window_start=delivery_window_start,
                    delivery_window_end=delivery_window_end,
                )
            except Exception as error:  # 后台线程异常不能无声消失
                logger.error(
                    "内测每日通报：后台聚合线程出现未预期异常，本轮未完成 error=%s",
                    type(error).__name__,
                )

        thread = threading.Thread(target=worker, name="lingxi-daily-report-aggregate", daemon=True)
        self._pending_thread = thread
        thread.start()
        thread.join(timeout=self._aggregation_join_timeout_seconds)
        if thread.is_alive():
            logger.info(
                "内测每日通报：聚合仍在后台线程运行，主循环不再等待 timeout=%ss",
                self._aggregation_join_timeout_seconds,
            )
            return None
        return result.get("text")

    def _aggregate_and_send(
        self,
        today: date,
        *,
        window_start: datetime,
        window_end: datetime,
        delivery_window_start: datetime,
        delivery_window_end: datetime,
    ) -> str | None:
        """六段聚合 + 渲染 + 发送 + 收尾——``run_once`` 派发进后台线程的那部分。

        跑在调用方之外的另一条线程上（见类文档「聚合挪出主线程」）；本职责
        原本就只编排注入的协作者（数据源、发送器、水位、审计），不碰任何线程
        局部或全局可变状态之外的东西，因此这里不需要额外的线程安全考量——
        `self._completed_on`/`self._reason_streaks` 只在成功路径末尾写一次，且
        同一时刻至多一个后台聚合在飞（`run_once` 的 `_pending_thread` 存活检查
        保证），不存在两条线程同时写的可能。
        """
        raw = self._fetch_daily_report_raw(
            window_start=window_start,
            window_end=window_end,
            delivery_window_start=delivery_window_start,
            delivery_window_end=delivery_window_end,
        )

        if self._stop.is_set():
            # 停止信号可能落在六段读取期间到达：干净中断，不发送、不置位、不提交
            # 节流状态，与 RosterAuditDuty 的同一条纪律（停止之后必须 0 次发送）。
            logger.info("停止信号在通报数据读取期间到达，本轮不发送")
            return None

        sections = self._build_sections_from_raw(raw, today)
        text = _render_daily_report_text(
            sections,
            window_start=window_start,
            window_end=window_end,
            delivery_window_start=delivery_window_start,
            delivery_window_end=delivery_window_end,
        )

        if self._stop.is_set():
            logger.info("停止信号在通报渲染完成后到达，本轮不发送")
            return None

        if not self._send_daily_report(today, text):
            return None

        watermark_persisted = self._persist_daily_report_watermark(today)
        self._finalize_daily_report(
            today,
            sections,
            text,
            window_start=window_start,
            window_end=window_end,
            watermark_persisted=watermark_persisted,
        )
        return text

    def _build_sections_from_raw(
        self, raw: _DailyReportRawData, today: date
    ) -> _DailyReportSections:
        """把实例状态显式快照后交给纯函数 `_build_daily_report_sections`。

        实现见 `daily_report_sections.py`。
        """
        return _build_daily_report_sections(
            raw,
            today,
            reason_streaks=self._reason_streaks,
            metric_coverage_wired=self._metric_coverage is not None,
            local_override_activity_wired=self._local_override_activity is not None,
        )

    def _fetch_daily_report_raw(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        delivery_window_start: datetime,
        delivery_window_end: datetime,
    ) -> _DailyReportRawData:
        """六段（含两段可选）独立取数。

        一段查询失败只让这段的 reason 非空，不拖累其余段落或抛出异常。
        """
        mandatory = self._fetch_mandatory_sections(
            window_start=window_start,
            window_end=window_end,
            delivery_window_start=delivery_window_start,
            delivery_window_end=delivery_window_end,
        )
        optional = self._fetch_optional_sections(window_start=window_start, window_end=window_end)
        return _DailyReportRawData(
            active_counts=mandatory[0],
            active_reason=mandatory[1],
            outcome_rows=mandatory[2],
            outcome_reason=mandatory[3],
            durations=mandatory[4],
            latency_reason=mandatory[5],
            delivery_rows=mandatory[6],
            delivery_reason=mandatory[7],
            guard_denied_raw=mandatory[8],
            guard_denied_fetch_reason=mandatory[9],
            token_usage_raw=mandatory[10],
            token_usage_fetch_reason=mandatory[11],
            metric_coverage_raw=optional[0],
            metric_coverage_fetch_reason=optional[1],
            local_override_raw=optional[2],
            local_override_fetch_reason=optional[3],
        )

    def _fetch_mandatory_sections(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        delivery_window_start: datetime,
        delivery_window_end: datetime,
    ) -> tuple:
        """六段中不可省略的四段，加上通报补数用的两段。

        六段共用同一窗口、同一条 `_fetch` 单段失败纪律。
        """
        core = self._fetch_core_sections(
            window_start=window_start,
            window_end=window_end,
            delivery_window_start=delivery_window_start,
            delivery_window_end=delivery_window_end,
        )
        supplement = self._fetch_supplement_sections(
            window_start=window_start, window_end=window_end
        )
        return core + supplement

    def _fetch_core_sections(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        delivery_window_start: datetime,
        delivery_window_end: datetime,
    ) -> tuple:
        """最初就有的四段：活跃用户、任务结果、延迟、投递结果。"""
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
            lambda: self._source.delivery_outcomes(
                window_start=delivery_window_start, window_end=delivery_window_end
            ),
        )
        return (
            active_counts,
            active_reason,
            outcome_rows,
            outcome_reason,
            durations,
            latency_reason,
            delivery_rows,
            delivery_reason,
        )

    def _fetch_supplement_sections(self, *, window_start: datetime, window_end: datetime) -> tuple:
        """通报补数用的两段。

        与其余四段同一个窗口（task.created_at）、同一条 `_fetch` 单段失败
        纪律——查询本身失败时这一段照样只标不可判定，不拖累其余段落。是否
        "全部 NULL"因此需要整段判不可判定，是另一层判断，交给
        `core/daily_report.py` 的纯函数在下一阶段处理。
        """
        guard_denied_raw, guard_denied_fetch_reason = self._fetch(
            "denied_count",
            lambda: self._source.guard_denied_count_stats(
                window_start=window_start, window_end=window_end
            ),
        )
        token_usage_raw, token_usage_fetch_reason = self._fetch(
            "resource_usage",
            lambda: self._source.token_usage_stats(
                window_start=window_start, window_end=window_end
            ),
        )
        return (
            guard_denied_raw,
            guard_denied_fetch_reason,
            token_usage_raw,
            token_usage_fetch_reason,
        )

    def _fetch_optional_sections(self, *, window_start: datetime, window_end: datetime) -> tuple:
        """两段允许完全不接线的可选段。

        不接线时连尝试都不尝试，不产生 `_fetch` 的失败留痕，因为"没有接线"
        不是"这一轮取数失败"。
        """
        # 「未覆盖新指标」日检。与其余六段不同，本段**允许完全不接线**
        # （`self._metric_coverage is None`）。
        metric_coverage_raw: tuple[Sequence[str], Sequence[str]] | None
        metric_coverage_fetch_reason: str | None
        if self._metric_coverage is not None:
            metric_coverage_raw, metric_coverage_fetch_reason = self._fetch(
                "metric_coverage", self._metric_coverage
            )
        else:
            metric_coverage_raw, metric_coverage_fetch_reason = None, None

        # 「本地权限覆盖活动」段。与 `metric_coverage` 同一条「未接线不产生
        # _fetch 失败留痕」纪律，但用主统计窗口（`window_start`/`window_end`，
        # 与前五段同一个窗口）而不是 `metric_coverage` 那种无窗口的全局查询——
        # 见 `_LocalOverrideActivitySource` 的文档。
        local_override_raw: tuple[int, int, int, int, int, int] | None
        local_override_fetch_reason: str | None
        if self._local_override_activity is not None:
            local_override_raw, local_override_fetch_reason = self._fetch(
                "local_override_activity",
                lambda: self._local_override_activity(
                    window_start=window_start, window_end=window_end
                ),
            )
        else:
            local_override_raw, local_override_fetch_reason = None, None

        return (
            metric_coverage_raw,
            metric_coverage_fetch_reason,
            local_override_raw,
            local_override_fetch_reason,
        )

    def _send_daily_report(self, today: date, text: str) -> bool:
        """发送正文，成功返回 ``True``。

        失败记审计 + 日志并返回 ``False``（调用方据此提前返回，不置位任何
        节流/水位状态）。
        """
        try:
            # 同一天的通报（含失败重试）共用一个去重键：不确定态下的重试因此携带
            # 同一个投递 `uuid`，由飞书服务端去重，与花名册日报同一条纪律
            # （`RosterAuditDuty` 文档字符串「重试与"不确定态"」一节）。
            self._sender.send_text(
                chat_id=self._chat_id, text=text, dedupe_key=f"daily-report:{today.isoformat()}"
            )
        except Exception as error:  # 发送失败不得带走同一轮的其他职责
            reason = _classify_send_failure(error)
            self._audit.record(
                "daily_report.send_failed",
                report_date=today.isoformat(),
                error=type(error).__name__,
                reason=reason,
            )
            logger.error(
                "内测每日通报发送失败，下一轮重试 error=%s reason=%s",
                type(error).__name__,
                reason,
            )
            return False
        return True

    def _persist_daily_report_watermark(self, today: date) -> bool:
        """发送成功后立刻持久化水位。

        失败时不掩盖"消息已经发出"这件事本身：`_finalize_daily_report` 里的
        `sent` 审计照常记（带 `watermark_persisted=False`），内存节流状态
        也照常置位；跨重启的重发风险如实登记在下面的
        `watermark_persist_failed` 审计里，交给运维按需处理，不假装持久化
        成功了。真正防止重复可见消息的是 `send_text` 的 `dedupe_key` 与飞书
        服务端去重（见类文档「判重水位持久化」）。
        """
        # 写入本身幂等（`ON CONFLICT DO NOTHING`），重复调用不产生第二行。这里
        # 额外包一层 try/except：`mark_sent` 抛异常与"进程崩溃"是两种不同的坏
        # 形状——崩溃时 `_finalize_daily_report` 的审计与置位根本来不及跑；但
        # 这里若不捕获，异常会顺着 `_aggregate_and_send` 冒泡到 worker() 的
        # 通用 except，只留一条含糊日志，用户已经看到通报却查不到"发过"的痕迹。
        try:
            self._watermark.mark_sent(chat_id=self._chat_id, report_date=today)
        except Exception as error:  # 水位写入失败不得掩盖"消息已发出"
            self._audit.record(
                "daily_report.watermark_persist_failed",
                report_date=today.isoformat(),
                error=type(error).__name__,
            )
            logger.error(
                "内测每日通报：消息已发送，但判重水位写入失败，跨重启存在重发"
                "风险，本进程存活期间不会重发 error=%s",
                type(error).__name__,
            )
            return False
        return True

    def _finalize_daily_report(
        self,
        today: date,
        sections: _DailyReportSections,
        text: str,
        *,
        window_start: datetime,
        window_end: datetime,
        watermark_persisted: bool,
    ) -> None:
        """记 `daily_report.sent` 审计、置位内存节流状态、记一条成功日志。"""
        # 「未覆盖新指标」日检未接线（`metric_coverage_gap is None`）时不出现在这个
        # 列表的候选里——它不是"这一轮不可判定"，是"这一轮根本没有这一段"，混进
        # 同一份 `undetermined_sections` 会让"没接线"与"接线了但取数失败"变成同一
        # 种审计表现，无从分辨该找运维配置还是找 MCP 连通性。
        determinable_sections: list[tuple[str, Section]] = [
            ("active_users", sections.active_users),
            ("status_distribution", sections.status_distribution),
            ("failure_top", sections.failure_top),
            ("latency", sections.latency),
            ("delivery_outcome", sections.delivery_outcome),
            ("denied_count", sections.denied_count),
            ("resource_usage", sections.resource_usage),
        ]
        if sections.metric_coverage_gap is not None:
            determinable_sections.append(("metric_coverage", sections.metric_coverage_gap))
        # 「本地权限覆盖活动」未接线（`local_override_activity is None`）同理不
        # 出现在这个列表里——理由与上面 `metric_coverage_gap` 的同一段注释一致。
        if sections.local_override_activity is not None:
            determinable_sections.append(
                ("local_override_activity", sections.local_override_activity)
            )
        self._audit.record(
            "daily_report.sent",
            report_date=today.isoformat(),
            active_users=sections.active_users.value.active_user_count
            if sections.active_users.is_determined
            else None,
            undetermined_sections=[
                name for name, section in determinable_sections if not section.is_determined
            ],
            # `False` 时如实标注这一轮的判重水位没能落盘（`watermark_persist_failed`
            # 审计记着具体异常类型），消息本身确实已经发出——见 `_persist_daily_
            # report_watermark` 的文档字符串。默认 `True`：绝大多数轮次里水位
            # 写入与发送一样成功。
            watermark_persisted=watermark_persisted,
        )
        self._completed_on = today
        self._reason_streaks = sections.updated_streaks
        logger.info(
            "内测每日通报已发送 统计窗口=%s~%s 字符数=%s",
            window_start.date().isoformat(),
            window_end.date().isoformat(),
            len(text),
        )


# 装配函数留在本文件（而不是 assembly.py）：避免撑大已经逼近体量棘轮上限的
# 那个文件，也让「这个职责怎么被造出来」与「它自己是什么」物理相邻；
# build_loop 像装配其余职责一样直接导入并调用本函数，调用方感知不到这个
# 区别。
def _build_daily_report_duty(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
    on_send_outcome: Callable[[str, bool], None] | None = None,
) -> DailyReportDuty | None:
    """装配内测每日通报职责；前置不齐就**不注册**并留下**恰一条**审计，返回 ``None``。

    **唯一前置是管理群 chat_id**（同花名册日报「缺项只报变量名、审计恰一条、
    其余职责照常运行」的纪律）：本职责读写的三张表都在 `config.postgres_dsn`
    这个进程级必需配置里，不需要任何额外的外部标识；没有目的地就没有必要跑
    六段查询。``on_send_outcome`` 复用与花名册日报共用的告警接线。
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
    from lingxi.adapters.postgres_daily_report_watermark import PostgresDailyReportWatermark

    return DailyReportDuty(
        source=PostgresDailyReportSource(config.postgres_dsn, timeouts=config.postgres_timeouts),
        watermark=PostgresDailyReportWatermark(
            config.postgres_dsn, timeouts=config.postgres_timeouts
        ),
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
        metric_coverage=_build_metric_coverage_check(config, audit=audit),
        local_override_activity=_build_local_override_activity_check(config, audit=audit),
    )


def _build_local_override_activity_check(
    config: SchedulerConfig, *, audit: AuditSink
) -> _LocalOverrideActivitySource | None:
    """装配「本地权限覆盖活动」段的取数回调。

    **唯一依赖 `config.postgres_dsn`/`config.postgres_timeouts`**——两者是进程
    启动的必需配置，因此不存在"这个前置缺了、只关掉这一段"的真实场景，本函数
    恒返回一个真实回调。返回类型 ``... | None`` 只是为了与
    :func:`_build_metric_coverage_check` 同一签名形状，供调用方用统一方式装配
    两段可选子功能，不是暗示这里真的会返回 `None`。表还没迁移过、查询超时等
    运行期故障按 `DailyReportDuty._fetch` 的既有单段失败纪律降级（这一段显式
    「不可判定」，不影响通报其余段落），不在装配阶段另建审计重复这件事。
    """
    from lingxi.adapters.postgres_local_permission import PostgresLocalPermissionOverrideStore

    store = PostgresLocalPermissionOverrideStore(
        config.postgres_dsn, timeouts=config.postgres_timeouts
    )

    def _check(
        *, window_start: datetime, window_end: datetime
    ) -> tuple[int, int, int, int, int, int]:
        return store.daily_activity_stats(window_start=window_start, window_end=window_end)

    return _check


def _build_metric_coverage_check(
    config: SchedulerConfig, *, audit: AuditSink
) -> Callable[[], tuple[Sequence[str], Sequence[str]]] | None:
    """装配「未覆盖新指标」日检的取数回调。前置不齐**只关掉这一段**，不影响内测每日通报的其余段落。

    本段是众多段落里的一段，不是独立职责，缺前置时只是正文少一段，其余
    段落与本职责本身照常。两个前置复用既有配置、不新增任何环境变量：
    ``LINGXI_MCP_TOKEN_ENCRYPT_KEY``
    （读已签发 MCP 令牌明文需要）与 ``LINGXI_QUERY_MCP_ENDPOINT``（真的调一次
    ``list_metrics``）。返回的回调每次调用都重新读取映射表，不在装配时缓存——
    外置映射文件承诺"编辑即生效"，只读一次会打破这条承诺；读取失败会让日检
    本轮标记为不可判定（`DailyReportDuty._fetch` 既有的单段失败纪律）。
    """
    missing = _metric_coverage_missing_vars(config)
    if missing:
        # 恰一条审计，形状照 `permission_readiness.probe_not_wired`（同一姿态：只
        # 关掉一个可选面，不影响承载它的职责本身）。只报变量名，不回显任何值。
        audit.record(
            "daily_report.metric_coverage_not_wired",
            reason="missing_environment_variable",
            variables=missing,
        )
        logger.info(
            "未同时配置 LINGXI_MCP_TOKEN_ENCRYPT_KEY 与 LINGXI_QUERY_MCP_ENDPOINT，"
            "内测每日通报的「未覆盖新指标」日检不接线；通报其余段落照常"
        )
        return None
    return _build_metric_coverage_probe(config)


def _metric_coverage_missing_vars(config: SchedulerConfig) -> list[str]:
    """列出「未覆盖新指标」日检缺失的前置环境变量名（为空表示前置齐全）。"""
    return [
        name
        for name, value in (
            ("LINGXI_MCP_TOKEN_ENCRYPT_KEY", config.mcp_token_encrypt_key),
            ("LINGXI_QUERY_MCP_ENDPOINT", config.query_mcp_endpoint),
        )
        if not value
    ]


def _build_metric_coverage_probe(
    config: SchedulerConfig,
) -> Callable[[], tuple[Sequence[str], Sequence[str]]]:
    """前置已齐全时，构造真正调用 MCP 的取数回调。"""
    from lingxi.adapters.company_function_metric_map_file import load_company_function_metric_map
    from lingxi.adapters.mcp_token_cipher import McpTokenCipher
    from lingxi.adapters.postgres_mcp_token import PostgresMcpTokenStore
    from lingxi.adapters.query_mcp_probe import fetch_metric_catalog

    endpoint = config.query_mcp_endpoint
    tokens = PostgresMcpTokenStore(
        config.postgres_dsn,
        cipher=McpTokenCipher(config.mcp_token_encrypt_key),
        timeouts=config.postgres_timeouts,
    )

    def _check() -> tuple[Sequence[str], Sequence[str]]:
        mapping = load_company_function_metric_map(config.metric_map_path)
        mapped_metric_ids = sorted(
            {
                metric_id
                for functions in mapping.values()
                for metrics in functions.values()
                for metric_id in metrics
            }
        )
        user_id = tokens.any_token_holder()
        if user_id is None:
            # 没有任何人签发过令牌：还没有真实用户走完首次开通，日检本轮无法进行
            # （不是"没有差异"）——响亮抛出，交给 `_fetch` 标记为不可判定。
            raise RuntimeError("no_issued_mcp_token_available_for_catalog_probe")
        token = tokens.read_token(user_id)
        if not token:
            raise RuntimeError("issued_token_holder_has_no_readable_token")
        mcp_metric_ids = fetch_metric_catalog(endpoint=endpoint, token=token)
        return sorted(mcp_metric_ids), mapped_metric_ids

    return _check


def _wire_daily_report_duty(
    duties: list,
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
    alerting_duty,
) -> None:
    """装配内测每日通报职责，非 ``None`` 才追加进 ``duties``。

    把 ``build_loop`` 里"造出来、非 None 才追加"这两行标准动作也收进本
    文件，让 ``assembly.py`` 的调用点只占**一行**（体量棘轮上限逼出来的
    形状，见 :func:`_build_daily_report_duty` 顶部的装配位置说明）；其余
    职责在 ``assembly.py`` 里仍然是显式的 ``if x is not None:
    duties.append(x)`` 两行，本函数不改变那个惯例，只是让**这一个**新增
    职责不必再占用 assembly.py 的行数预算。
    """
    duty = _build_daily_report_duty(
        config,
        stop=stop,
        audit=audit,
        on_send_outcome=(alerting_duty.send_outcome_callback() if alerting_duty else None),
    )
    if duty is not None:
        duties.append(duty)
