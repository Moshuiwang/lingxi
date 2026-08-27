"""内测每日通报职责：:class:`DailyReportDuty`（Issue #303 S-O-01；`denied_count`/
`resource_usage` 两段自 Issue #304 批次 4 起改为真实数据源，见下）。

一轮做三件事：分六段独立读取统计（四段读昨日、`denied_count`/`resource_usage`
也读昨日与前四段同一个窗口，投递结果段独立读前天——理由见 `core/daily_report.py`
模块文档「投递结果段为什么用一个独立、更早的窗口」，opus 批量审查 P2 修复）→
纯函数聚合与节流 → 该发就发一条到管理群。形状照 `apps/scheduler/roster_audit.py`
的 :class:`~lingxi.apps.scheduler.roster_audit.RosterAuditDuty`（同一天至多一次
的判重水位、发送失败不置位、同一天的重试共用一个去重键），但**多了一条
RosterAuditDuty 没有的规则**：六段数据源各自独立失败，只让对应的段落显式「不可
判定」，不拖累其余段落，也不拖累整轮发送——这是 #303 明确要求、而花名册日报当前
不需要的行为（它的判重逻辑是「整轮读不出来就整轮重试」，不是「分段降级」）。

## 判重水位已持久化，节流状态仍是进程内存（Issue #325）

**`_completed_on`（今天发过了没有）现在有两层**：进程内存里的这个日期水位仍然是
每一轮判重的**第一道**、最快的一关（避免同一进程存活期间的每一轮都去查库）；
真正跨重启存活的是数据库里的 `daily_report_watermark` 表（迁移 `0071`，
`adapters/postgres_daily_report_watermark.py`）——发送**成功**之后立刻写一行，
下一轮（含重启后的新进程）判重时，内存水位落空就去查这张表，查到即说明"这个
窗口、这个目的地已经发过"，直接补齐内存水位、不重新聚合、不重新发送。

**这修的是什么**：管理群实测坐实的残留——scheduler 每次重启（部署升级、进程
恢复）都把 `_completed_on` 清零，同一个统计窗口被重新判定成"还没发"，重新跑
一遍聚合与发送。2026-08-25 单日同窗口收到四条通报，对应当天多次部署重启。
`RosterAuditDuty` 的同形状残留（本文件模块文档开头提到的"同一条已知残留"）
**不在本次修复范围**——#325 只认领内测每日通报，花名册日报的判重水位是否同样
持久化留给它自己的 Story。

**`_reason_streaks`（失败分类 Top 每个原因码连续在榜几天）仍然只在进程内存
里**——这是 #325 明确没有认领的部分（Issue 正文「代码变动面」只提判重水位与
心跳假告警两项）。跨重启节流效果仍会短暂消失几天再重新生效，与 2026-08-06
产品负责人裁定（C2 / R2）的知情接受一致：连续 7 天的节流阈值本身足够宽，一次
重启不会让用户看到明显的行为回退。要给它也上持久列，属另一次新迁移，留给需要
时的后续 Story。

**「发送成功与标记写入」不是字面同一个数据库事务**：发送是一次 HTTP 调用，不是
数据库操作，两者结构上不可能被塞进同一个 `BEGIN`/`COMMIT`。真正的幂等保证来自
两层纵深——数据库水位挡住"隔了几小时甚至几天的重启"（本次实测的形状：部署
升级、进程恢复）；`send_text` 的 `dedupe_key` 与飞书服务端投递去重（见
`adapters/feishu_group_message.py::delivery_uuid` 文档）挡住"发送成功与水位
写入之间那道极窄缝隙里恰好发生崩溃重启"这类更罕见的情形。`mark_sent` 本身也是
幂等的（`ON CONFLICT DO NOTHING`），即使被调用两次也只留一行——这道防线因此
在两个不同的时间尺度上分别成立，合起来才是"进程崩在中间不产生第二条"的完整
论证，不是靠一句"同事务"就能诚实概括的单一机制。

## 重聚合为什么要挪出主线程（Issue #325）

管理群实测还坐实了第二个残留：2026-08-25/08-26 连续两天 00:06 出现
`scheduler.process_inactive` 告警、00:12 自愈。根因不是心跳真的停了，是**评估
心跳是否新鲜这件事，被本职责的聚合耗时挤到了心跳被记录之后很久**：

- `apps/scheduler/assembly.py::build_loop` 把 `AlertingDuty` 注册在 `duties`
  列表**最后**（"它汇总本轮观察到的信号，排在被观察者后面才看得到这一轮的
  事实"，见该文件 `build_loop` 文档字符串）——这个顺序本身是对的，不能靠调整
  顺序来"修"这个问题。
- `apps/scheduler/loop.py::SchedulerLoop.run_once()` 在**这一轮全部职责开始
  之前**记一次心跳，然后依次同步调用每个职责的 `run_once()`，`AlertingDuty`
  排在最后一个才轮到。
- 本职责在被调用时如果同步跑完六段聚合 + 渲染 + 发送（正常情况下很快，但没有
  任何机制保证它总是很快——大表扫描、飞书接口慢响应都可能把它拖到几分钟），
  `AlertingDuty.run_once()` 就要等聚合真正跑完才轮到自己执行；它内部
  `AlertManager.check_heartbeats(at=now)` 用的 `now` 是**它自己被调用那一刻**
  的时钟读数，而心跳是**这一整轮开始之前**记的——两者之间隔着本职责刚刚花掉
  的全部聚合耗时。一旦这段耗时超过 `AlertPolicy.heartbeat_timeout_seconds`
  （默认 120 秒），`check_heartbeats` 就会诚实地判定"心跳不新鲜"并发出
  `PROCESS_INACTIVE` 告警——进程其实一直活着，只是这一轮的评估时机被本职责的
  聚合耗时顶到了阈值之外。下一轮心跳刷新，`AlertingDuty` 观察到连续新鲜，
  `recovery_stable_seconds`（默认 300 秒）满足后自动发出恢复通知——这与实测的
  "00:06 告警、00:12 自愈"（相隔约 6 分钟，略高于 5 分钟的稳定恢复窗口）完全
  吻合。

  **这不是已知未修复缺口（PR #173 P2-2，见验收矩阵「运行告警」一节）的同一个
  问题**：那条登记的是"循环本身彻底停摆时没有任何东西检查停摆"（一个更深的
  结构性限制，仍然接受不修）；这里是"循环仍在正常推进，只是某一轮里排在前面
  的一个职责耗时较长，挤晚了排在后面的心跳评估"——两者的成因、影响面和修法
  都不同，本次只修后者，且只动本职责自己，不改 `SchedulerLoop`/`AlertingDuty`
  的既有顺序或语义（那两者的既有形状经过审查、有意为之，见上面引用的文档
  字符串）。

**修法**：六段聚合 + 渲染 + 发送 + 收尾（水位、审计、`_completed_on`/
`_reason_streaks`）整体挪进一个后台线程（:meth:`DailyReportDuty._aggregate_and_send`），
调用方所在的线程只等一个很短的上限
（`DEFAULT_AGGREGATION_JOIN_TIMEOUT_SECONDS`，默认 2 秒）就拿回控制权——聚合
本身没有超时、不会被打断，只是不再占着 `SchedulerLoop` 的主线程。正常情况下
（聚合速度远小于这个上限）行为与改动前完全一致：`run_once()` 同步等到结果、
原样返回正文。真正变慢的那一轮，`run_once()` 提前返回 `None`（"这一轮只是确认
聚合还在跑"），后台线程自己收尾，下一轮 `SchedulerLoop` 能立刻推进到
`AlertingDuty`，心跳评估拿到的是新鲜的时钟读数，不再被本职责的聚合耗时拖累。
与 worker 侧 `asyncio.to_thread` 挪文件读取出事件循环（`apps/worker/service.py`
的既有注释：「不占事件循环（心跳与停止处理都在循环上）」）是同一条纪律在
同步/线程模型下的对应写法——scheduler 是 `threading`（同步）模型、不是
`asyncio`，因此用后台线程而不是协程调度让出。

**同一时刻至多一个后台聚合在飞**：`_pending_thread` 记着上一次派发的线程，
还活着就不再派发第二个——两轮聚合同时读库、同时可能尝试发送，是比"聚合慢"
本身更糟的形状。**如实描述停止信号窗口**：后台线程创建时带 `daemon=True`，
`run_once` 派发之后只等 `_aggregation_join_timeout_seconds`（默认 2 秒）就拿回
控制权；`SchedulerLoop.run_forever()` 观察到停止标志后即返回，`apps/scheduler/
__init__.py::main` 随即退出，两处都不会额外 `join` 这条后台线程。代码里确实有
"读取阶段/渲染完成后再看一眼 `_stop`"两处检查点，但它们只在线程本身还活着、
还有机会被调度到那一行时才生效——慢聚合期间收到 `SIGTERM`，若进程在线程到达
下一个检查点之前就已退出，线程会被就地截断，不是稳定的"干净中断"保证。
**后果与恢复路径**：截断多数落在水位写入之前——下一轮（含重启后的新进程）
判重水位查不到，会重新聚合并尝试发送；真正挡住用户收到重复通报的是发送时
携带的确定性 `dedupe_key` 与飞书服务端投递去重（见上文"发送成功与标记写入"
一段），与送达失败下一轮照常重试是同一条纪律（`V-通报-07` 语义），不是本次
新增的例外。

## 送达失败不静默

发送异常时记一条 `daily_report.send_failed` 审计（只记异常类型与一个安全的粗粒度
原因分类——`uuid_budget`/`transport`/`other`，见 `_classify_send_failure`，opus
批量审查 P3 修复——不记异常消息文本、不记正文；正文虽已经过统计级脱敏，但它是给
管理群的，不是给运维日志的，同花名册日报 `V-花名册-33` 同一条纪律）并升级为
`logger.error`；水位与节流状态都**不**提交，下一轮照常重试。
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

from lingxi.apps.scheduler.audit import AuditSink
from lingxi.apps.scheduler.config import SchedulerConfig

logger = logging.getLogger(__name__)

#: 安全的发送失败原因分类（opus 批量审查 P3 修复）。此前 `daily_report.send_failed`
#: 只记异常**类名**（`type(error).__name__`），运维要靠类名自己猜"这是我们自己的
#: uuid 预算算错了，还是飞书/网络那一侧的问题"——两类问题的处置完全不同（前者是
#: 代码 bug，后者多半会自愈重试）。分类只看异常**类型**，不看异常消息文本（消息
#: 文本可能带正文片段），因此这个分类本身不含任何敏感值。
def _classify_send_failure(error: Exception) -> str:
    from lingxi.adapters.feishu_group_message import FeishuGroupMessageError

    if isinstance(error, ValueError):
        # delivery_uuid() 唯一会抛的异常类型：前缀非法，或折算出的投递去重 ID
        # 超过飞书 50 字符上限（opus 批量审查 P1 修复的那一类 bug，见
        # adapters/feishu_group_message.py 的 DAILY_REPORT_UUID_PREFIX 登记）。
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


#: 后台聚合线程的主线程等待上限（Issue #325，见类文档「重聚合为什么要挪出主
#: 线程」）。**不是聚合本身的超时**——聚合没有边界，慢就慢，后台线程会一直跑
#: 到收工或停止信号生效；这个数字只控制"调用方（scheduler 主循环所在的那条
#: 线程，心跳与 `AlertingDuty` 的心跳评估都在上面）最多为这一轮愿意等多久才
#: 把控制权拿回去"。取值远小于 `core/alerting.py` `AlertPolicy.
#: heartbeat_timeout_seconds` 的默认值（120 秒）——本职责单独占用的等待时间
#: 必须给其余职责与下一轮心跳留出充裕余量，不能自己就把预算花完；也远大于
#: 测试用假数据源的实际耗时（微秒级），因此全部既有测试在这个等待窗口内都能
#: 正常拿到同步返回值，行为不变。
DEFAULT_AGGREGATION_JOIN_TIMEOUT_SECONDS = 2.0


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

    def guard_denied_count_stats(
        self, *, window_start: datetime, window_end: datetime
    ) -> tuple[int, int, int]: ...

    def token_usage_stats(
        self, *, window_start: datetime, window_end: datetime
    ) -> tuple[int, int, int, int, int, int]: ...


class _LocalOverrideActivitySource(Protocol):
    """「本地权限覆盖活动」段的可选取数口（Issue #319 S-P-1c）；实现是
    :meth:`~lingxi.adapters.postgres_local_permission.PostgresLocalPermissionOverrideStore.
    daily_activity_stats`。与 `metric_coverage`（下方 `DailyReportDuty.__init__`
    的同名参数）同一条「可选取数回调」纪律，但本段需要统计窗口——`metric_coverage`
    问的是"当前"的全局覆盖面差集，不随统计窗口变化；本段问的是"这一天"发生了
    什么，因此签名比 `metric_coverage` 多了 `window_start`/`window_end` 两个
    参数，与 `_DailyStatsSource` 其余六个方法同一个窗口口径。
    """

    def __call__(
        self, *, window_start: datetime, window_end: datetime
    ) -> tuple[int, int, int, int, int, int]: ...


class _GroupSender(Protocol):
    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None: ...


class _SentWatermark(Protocol):
    """通报送达水位的持久化端口（Issue #325）；实现是
    :class:`~lingxi.adapters.postgres_daily_report_watermark.PostgresDailyReportWatermark`。
    职责只依赖这个签名，因此跨重启判重断言也能在没有数据库、没有网络的机器上跑完。
    """

    def already_sent(self, *, report_date: date, chat_id: str) -> bool: ...

    def mark_sent(self, *, report_date: date, chat_id: str) -> None: ...


class DailyReportDuty:
    """内测每日统计通报（Issue #303 S-O-01）。"""

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
        self._source = source
        self._watermark = watermark
        self._sender = sender
        self._audit = audit
        self._chat_id = chat_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._stop = threading.Event() if stop is None else stop
        self._aggregation_join_timeout_seconds = aggregation_join_timeout_seconds
        self._completed_on: date | None = None
        self._reason_streaks: dict[str, int] = {}
        # 上一次派发的后台聚合线程（Issue #325，见类文档「重聚合为什么要挪出主
        # 线程」）。``None`` 或已经跑完都表示"可以派发下一轮"；还活着就说明上一轮
        # 还没收工，本轮不重复派发。
        self._pending_thread: threading.Thread | None = None
        # 「未覆盖新指标」日检（Issue #320 并入项）。**可选，默认 None**——独立于
        # 其余六段数据源，不接线时这一段完全不出现在正文里（见 core/daily_report.py
        # 的 DailyReportInputs.metric_coverage_gap 三态说明）。返回值是
        # ``(mcp_metric_ids, mapped_metric_ids)``：两组指标 ID，纯集合运算
        # （`build_metric_coverage_gap`）留在 core 层，这里只负责取数与调度。**取数
        # 含一次真实 MCP 网络调用**（`fetch_metric_catalog`）——本回调只会在
        # `_aggregate_and_send`（Issue #325 起跑在后台聚合线程，见类文档「重聚合
        # 为什么要挪出主线程」）里被 `_fetch("metric_coverage", ...)` 调用，绝不会
        # 被 `run_once` 本身（调用方所在的线程，`SchedulerLoop` 主循环与
        # `AlertingDuty` 心跳评估共用这条线程）直接调用，因此不会重新引入 #325
        # 刚修好的"耗时职责挤占心跳评估"问题。
        self._metric_coverage = metric_coverage
        # 「本地权限覆盖活动」段（Issue #319 S-P-1c）。**可选，默认 None**——
        # 与 `metric_coverage` 同一条纪律：不接线时这一段完全不出现在正文里
        # （见 core/daily_report.py 的 DailyReportInputs.local_override_activity
        # 三态说明）。取数只查 Lingxi 自己的 `local_permission_override` 表，不
        # 含任何网络调用，因此不需要单独考虑「重新引入 #325 心跳阻塞问题」——
        # 与 `metric_coverage`（含一次真实 MCP 网络调用）不同，但仍然只会在
        # `_aggregate_and_send`（后台聚合线程）里被 `_fetch` 调用，不会被
        # `run_once` 本身直接调用，理由同上（保持两段接线方式一致，便于阅读）。
        self._local_override_activity = local_override_activity

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    @property
    def completed_on(self) -> date | None:
        """已完成通报的那一天（本进程内观察到的事实）。可能是本进程自己发的，
        也可能是读到持久水位后才确认"已经发过"（见类文档「判重水位已持久化」）。
        ``None`` 表示本进程实例还没确认今天已经处理完。"""

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
        """跑一轮。返回 ``None`` 表示本轮没有发送（停止中、今天已经做完，或本轮
        只是确认了一次"后台聚合还在跑，调用方不等"）；否则返回已发送的正文
        （供测试断言）——语义与改动前完全一致，差异只在于：聚合速度远慢于
        `_aggregation_join_timeout_seconds` 时，`run_once` 会先返回 `None`，
        真正的发送与收尾在后台线程里完成（见类文档「重聚合为什么要挪出主
        线程」，Issue #325）。"""

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

        if self._watermark.already_sent(chat_id=self._chat_id, report_date=today):
            # 持久水位显示本窗口已经发过——可能是本进程更早一轮成功后设的，也
            # 可能是重启前的旧进程设的（这正是 #325 要修的残留：内存水位重启即
            # 清零，数据库水位不会）。直接补齐内存水位，不重新聚合、不重新发送。
            self._completed_on = today
            return None

        # 统计窗口固定为「昨天」的完整 UTC 自然日：与花名册审计同样的 UTC 日界
        # 约定（`roster_report.py`「时间一律 UTC 标注」），取昨天而不是「今天已过去
        # 的部分」，是为了让每一份通报覆盖的都是一个**完整**的自然日，不会因为
        # scheduler 首轮 tick 落在当天早晚不同时刻而让统计口径每天不一样。
        window_end = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
        window_start = window_end - timedelta(days=1)
        # 投递结果段独立用**再早一天**的窗口（opus 批量审查 P2 修复，见
        # `core/daily_report.py` 模块文档「投递结果段为什么用一个独立、更早的
        # 窗口」）：`[D-1, D)` 里的投递行，24 小时确认期到今天通报运行时还没
        # 关闭，「过期」这一桶在那个窗口下结构上恒为零。
        delivery_window_end = window_start
        delivery_window_start = delivery_window_end - timedelta(days=1)

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
            except Exception as error:  # noqa: BLE001 - 后台线程异常不能无声消失
                logger.error(
                    "内测每日通报：后台聚合线程出现未预期异常，本轮未完成 error=%s",
                    type(error).__name__,
                )

        thread = threading.Thread(
            target=worker, name="lingxi-daily-report-aggregate", daemon=True
        )
        self._pending_thread = thread
        thread.start()
        thread.join(timeout=self._aggregation_join_timeout_seconds)
        if thread.is_alive():
            # 聚合仍在跑：不再等——这正是本次修复的核心，聚合耗时不得占用调用方
            # 所在的线程（scheduler 主循环：心跳与其余职责，含 AlertingDuty 的
            # 心跳评估，都在这条线程上）。后台线程会自己收尾（水位、审计、
            # `_completed_on`），下一轮由持久水位或内存 `_completed_on` 观察到。
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
        """六段聚合 + 渲染 + 发送 + 收尾——``run_once`` 派发进后台线程的那部分
        （Issue #325，见类文档「重聚合为什么要挪出主线程」）。跑在调用方之外的
        另一条线程上；本职责原本就只编排注入的协作者（数据源、发送器、水位、
        审计），不碰任何线程局部或全局可变状态之外的东西，因此这里不需要额外的
        线程安全考量——`self._completed_on`/`self._reason_streaks` 只在成功路径
        末尾写一次，且同一时刻至多一个后台聚合在飞（`run_once` 的
        `_pending_thread` 存活检查保证），不存在两条线程同时写的可能。"""

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
        # 通报补数（Issue #303/#304 批次 4）：与其余四段同一个窗口（task.created_at）、
        # 同一条 _fetch 单段失败纪律——查询本身失败时这一段照样只标不可判定，不
        # 拖累其余段落。是否"全部 NULL"因此需要整段判不可判定，是另一层判断，
        # 在下面拿到查询结果之后交给 core/daily_report.py 的纯函数。
        guard_denied_raw, guard_denied_fetch_reason = self._fetch(
            "denied_count",
            lambda: self._source.guard_denied_count_stats(
                window_start=window_start, window_end=window_end
            ),
        )
        token_usage_raw, token_usage_fetch_reason = self._fetch(
            "resource_usage",
            lambda: self._source.token_usage_stats(window_start=window_start, window_end=window_end),
        )
        # 「未覆盖新指标」日检（Issue #320 并入项）。与其余六段不同，本段**允许
        # 完全不接线**（`self._metric_coverage is None`）——那种情况下连尝试都不
        # 尝试，不产生 `_fetch` 的失败留痕，因为"没有接线"不是"这一轮取数失败"。
        metric_coverage_raw: tuple[Sequence[str], Sequence[str]] | None
        metric_coverage_fetch_reason: str | None
        if self._metric_coverage is not None:
            metric_coverage_raw, metric_coverage_fetch_reason = self._fetch(
                "metric_coverage", self._metric_coverage
            )
        else:
            metric_coverage_raw, metric_coverage_fetch_reason = None, None

        # 「本地权限覆盖活动」段（Issue #319 S-P-1c）。与 `metric_coverage` 同一
        # 条「未接线不产生 _fetch 失败留痕」纪律，但用主统计窗口（`window_start`/
        # `window_end`，与前五段同一个窗口）而不是 `metric_coverage` 那种无窗口
        # 的全局查询——见 `_LocalOverrideActivitySource` 的文档。
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

        # Issue #303/#304 批次 4：两段改为真实聚合（迁移 0070，见 core/daily_report.py
        # 模块文档「数据从哪来」）。查询本身失败 → 走 _fetch 的既有不可判定路径；
        # 查询成功但窗口内的任务在这个字段上全部是 NULL → 纯函数返回 None，这里
        # 才降级为不可判定（与查询失败是两种不同原因，用不同的 reason 文案区分）。
        denied_count: Section[PartialCount]
        if guard_denied_fetch_reason is not None:
            denied_count = Section.undetermined(guard_denied_fetch_reason)
        else:
            covered, uncovered, total = guard_denied_raw
            denied_stats = build_denied_count_stats(
                covered_tasks=covered, uncovered_tasks=uncovered, total=total
            )
            denied_count = (
                Section.of(denied_stats)
                if denied_stats is not None
                else Section.undetermined(DENIED_COUNT_ALL_NULL_REASON)
            )

        resource_usage: Section[TokenUsageStats]
        if token_usage_fetch_reason is not None:
            resource_usage = Section.undetermined(token_usage_fetch_reason)
        else:
            covered, uncovered, input_tokens, output_tokens, cache_creation, cache_read = (
                token_usage_raw
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

        # 三态见 core/daily_report.py 的 DailyReportInputs.metric_coverage_gap 文档：
        # 未接线保持 None；接线但本轮取数失败 → 不可判定；取数成功 → 纯函数算差集
        # （差集为空时 `Section.of(None)`，正文因此完全不出现这一段——无差异不报）。
        metric_coverage_gap: Section[MetricCoverageGap | None] | None
        if self._metric_coverage is None:
            metric_coverage_gap = None
        elif metric_coverage_fetch_reason is not None:
            metric_coverage_gap = Section.undetermined(metric_coverage_fetch_reason)
        else:
            assert metric_coverage_raw is not None
            mcp_metric_ids, mapped_metric_ids = metric_coverage_raw
            metric_coverage_gap = Section.of(
                build_metric_coverage_gap(mcp_metric_ids, mapped_metric_ids)
            )

        # 三态见 core/daily_report.py 的 DailyReportInputs.local_override_activity
        # 文档：未接线保持 None；接线但本轮取数失败 → 不可判定；取数成功 → 纯函数
        # 判定（当日零活动且当前生效总数为零时 `Section.of(None)`，正文因此完全
        # 不出现这一段——无差异不报，Issue #319 S-P-1c）。
        local_override_activity: Section[LocalOverrideActivity | None] | None
        if self._local_override_activity is None:
            local_override_activity = None
        elif local_override_fetch_reason is not None:
            local_override_activity = Section.undetermined(local_override_fetch_reason)
        else:
            assert local_override_raw is not None
            (
                granted_today,
                suppressed_today,
                revoked_today,
                active_grant_total,
                active_suppress_total,
                affected_user_count,
            ) = local_override_raw
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
            metric_coverage_gap=metric_coverage_gap,
            local_override_activity=local_override_activity,
            delivery_window_start=delivery_window_start,
            delivery_window_end=delivery_window_end,
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
            return None

        # 发送成功后立刻持久化水位（Issue #325）：即使这一步与上面的发送之间
        # 进程崩溃，也只是回到"水位没置位、下一轮按送达失败的既有语义重试"这个
        # 早已验证过的路径（`V-通报-07`）——真正防止"那次重试变成第二条可见
        # 消息"的是 `send_text` 的 `dedupe_key` 与飞书服务端去重，两者一起构成
        # 纵深（见类文档「判重水位已持久化」的「不是字面同一个数据库事务」一节）。
        # 写入本身幂等（`ON CONFLICT DO NOTHING`），重复调用不产生第二行。
        self._watermark.mark_sent(chat_id=self._chat_id, report_date=today)

        # 「未覆盖新指标」日检未接线（`metric_coverage_gap is None`）时不出现在这个
        # 列表的候选里——它不是"这一轮不可判定"，是"这一轮根本没有这一段"，混进
        # 同一份 `undetermined_sections` 会让"没接线"与"接线了但取数失败"变成同一
        # 种审计表现，无从分辨该找运维配置还是找 MCP 连通性。
        determinable_sections: list[tuple[str, Section]] = [
            ("active_users", active_users),
            ("status_distribution", status_distribution),
            ("failure_top", failure_top),
            ("latency", latency),
            ("delivery_outcome", delivery_outcome),
            ("denied_count", denied_count),
            ("resource_usage", resource_usage),
        ]
        if metric_coverage_gap is not None:
            determinable_sections.append(("metric_coverage", metric_coverage_gap))
        # 「本地权限覆盖活动」未接线（`local_override_activity is None`）同理不
        # 出现在这个列表里——理由与上面 `metric_coverage_gap` 的同一段注释一致。
        if local_override_activity is not None:
            determinable_sections.append(("local_override_activity", local_override_activity))
        self._audit.record(
            "daily_report.sent",
            report_date=today.isoformat(),
            active_users=active_users.value.active_user_count if active_users.is_determined else None,
            undetermined_sections=[
                name for name, section in determinable_sections if not section.is_determined
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
    那三个前置里有两个是外部 Base 坐标（要连去哪张表），本职责读 Lingxi 自己的
    ``task``/``task_delivery_event`` 两张表、写自己的 ``daily_report_watermark`` 一张表
    （Issue #325，见 :class:`~lingxi.adapters.postgres_daily_report_watermark.
    PostgresDailyReportWatermark`），三者都已经在 ``config.postgres_dsn`` 这个进程级
    必需配置里，不需要任何额外的外部标识。它唯一服务的目的地就是管理群，没有目的地
    也就没有必要跑六段查询。

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
    """装配「本地权限覆盖活动」段的取数回调（Issue #319 S-P-1c）。

    **与 :func:`_build_metric_coverage_check` 同一形状，但没有对应的「缺前置」
    分支**：后者需要两个额外的可选环境变量（`LINGXI_MCP_TOKEN_ENCRYPT_KEY`/
    `LINGXI_QUERY_MCP_ENDPOINT`）才能真的调一次 MCP；本段唯一依赖的是
    `config.postgres_dsn`/`config.postgres_timeouts`——两者是 `SchedulerConfig.
    from_env` 自身的必需项（`LINGXI_POSTGRES_DSN` 缺失时，进程在装配到这里之前
    就已经拒绝启动，见 `SchedulerConfig.from_env` 的 `required(...)`），因此
    **不存在**"这个前置缺了、只关掉这一段"的真实场景，本函数恒返回一个真实
    回调。保留 `... | None` 的返回类型只是为了与 `_build_metric_coverage_check`
    同一签名形状，供 `_build_daily_report_duty` 用统一的调用方式装配两段可选
    子功能，不是暗示这里真的会返回 `None`。

    表本身可能还没有被迁移过的环境（`local_permission_override` 是新增于
    本批次的表，见迁移 `0072`/`0073`）不在本函数的判断范围——那属于「装配时
    无法预知、只能在真的查询那一刻才会暴露」的运行期故障，与"表已存在但这次
    查询超时"没有本质区别，已经由 `DailyReportDuty._fetch` 的既有单段失败纪律
    覆盖（本段查询失败时只让这一段显式「不可判定」，不影响通报其余段落），
    不需要在装配阶段另建一条 `..._not_wired` 审计去重复这件事。
    """

    from lingxi.adapters.postgres_local_permission import PostgresLocalPermissionOverrideStore

    store = PostgresLocalPermissionOverrideStore(
        config.postgres_dsn, timeouts=config.postgres_timeouts
    )

    def _check(*, window_start: datetime, window_end: datetime) -> tuple[int, int, int, int, int, int]:
        return store.daily_activity_stats(window_start=window_start, window_end=window_end)

    return _check


def _build_metric_coverage_check(
    config: SchedulerConfig, *, audit: AuditSink
) -> Callable[[], tuple[Sequence[str], Sequence[str]]] | None:
    """装配「未覆盖新指标」日检的取数回调（Issue #320 并入项）。前置不齐**只关掉
    这一段**，不影响内测每日通报的其余六段——因此这里**不**走
    `permission_refresh.duty_not_registered` 那一套「恰一条审计 + 整个职责不注册」
    的纪律：本段是内测每日通报众多段落里的一段，不是一个独立职责，缺前置时只是
    正文少一段，其余段落与本职责本身照常。

    **两个前置，同一把复用的钥匙**（都是每日权限重算/就绪确认已经在用的既有配置，
    不新增任何环境变量）：

    - ``LINGXI_MCP_TOKEN_ENCRYPT_KEY``——本段需要读一份已签发的 MCP 令牌明文
      （:meth:`~lingxi.adapters.postgres_mcp_token.PostgresMcpTokenStore.read_token`），
      而唯一的读取口在构造时就要求已校验主密钥的加解密对象，与
      ``_build_permission_refresh_duty``/``_build_readiness_follow_up`` 同一条理由。
    - ``LINGXI_QUERY_MCP_ENDPOINT``——本段要真的调一次 ``list_metrics``。

    **返回的回调每次调用都重新读取映射表**（不在装配时读一次缓存）：外置路径
    （Issue #320，见 ``SchedulerConfig.metric_map_path``）承诺"编辑即生效"，日检
    如果只在启动时读一次，产品负责人当天改的内容要等下次重启才会被日检看见，
    与"编辑即生效"这句承诺矛盾。映射表读取失败（文件损坏/外置路径配错）会让
    这一次日检本轮标记为不可判定（`DailyReportDuty._fetch` 既有的单段失败纪律），
    不影响每日权限重算职责自己那一次独立的读取与判定。
    """

    if not config.mcp_token_encrypt_key or not config.query_mcp_endpoint:
        # 恰一条审计，形状照 `permission_readiness.probe_not_wired`（同一姿态：只
        # 关掉一个可选面，不影响承载它的职责本身）。只报变量名，不回显任何值。
        missing = [
            name
            for name, value in (
                ("LINGXI_MCP_TOKEN_ENCRYPT_KEY", config.mcp_token_encrypt_key),
                ("LINGXI_QUERY_MCP_ENDPOINT", config.query_mcp_endpoint),
            )
            if not value
        ]
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
            {metric_id for functions in mapping.values() for metrics in functions.values() for metric_id in metrics}
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
