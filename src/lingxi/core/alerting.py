"""MVP 运行告警的纯逻辑状态机。

这里不发送网络请求，也不读环境变量或数据库。调用方把时钟、任务摘要和飞书出站
结果作为事件传进来；这样 L2 可以证明阈值、退避、去重和恢复，而不会把真实飞书
平台的行为误当成本地证据。告警状态只在当前进程内保存，跨重启的真幂等留给 S9
的持久审计事件。
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Protocol

logger = logging.getLogger(__name__)


_SAFE_CATEGORY = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SAFE_TRACE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_UTC = UTC


class AlertKind(str, Enum):
    """管理员需要区分的故障类型。"""

    PROCESS_INACTIVE = "process_inactive"
    QUEUED_STUCK = "queued_stuck"
    RUNNING_HEARTBEAT_TIMEOUT = "running_heartbeat_timeout"
    RETRY_EXHAUSTED = "retry_exhausted"
    # 待投递（awaiting_delivery）二十四小时强制到期收敛（Issue #153 最小可观测性
    # 第二类：queued/running/awaiting-delivery 滞留三选一）。与 QUEUED_STUCK 分开，
    # 因为运维要看到的诊断动作不同——这类是"确认送达一直没发生"，不是"还没被领取"。
    AWAITING_DELIVERY_STUCK = "awaiting_delivery_stuck"
    # 目标 worker 版本压根没有可用实例（灰度发布配置漏配一版）——与"单纯排队太久"
    # 是两类不同的运维动作，因此独立成一个告警类型（Issue #153 最小可观测性第四类）。
    WORKER_VERSION_UNAVAILABLE = "worker_version_unavailable"
    FEISHU_SEND_FAILED = "feishu_send_failed"
    # 首次开通失败需要管理员核查（Issue #280 §7.3）——此前 `LINGXI_ADMIN_GROUP_CHAT_ID`
    # 的全部消费方都与开通失败无关，「已转交管理员处理」这句用户文案背后没有任何送达
    # 动作。这一格补上开通失败（本侧故障终态 `LX-ONBOARD-001`）以及开通中途停摆收口的
    # 送达面。
    ONBOARDING_FAILED = "onboarding_failed"


class NoticeAction(str, Enum):
    ALERT = "alert"
    RECOVERY = "recovery"


@dataclass(frozen=True)
class AlertPolicy:
    """告警体验的部署口径。

    数值默认值对应 Issue #92 已确认的 MVP 口径；正式入口通过
    :meth:`from_mapping` 接收 `LINGXI_ALERT_` 前缀配置，避免为了调阈值改代码。
    """

    heartbeat_timeout_seconds: float = 120.0
    queued_timeout_seconds: float = 180.0
    running_heartbeat_timeout_seconds: float = 90.0
    send_failure_window_seconds: float = 300.0
    send_failure_threshold: int = 3
    dedupe_window_seconds: float = 1800.0
    recovery_stable_seconds: float = 300.0
    retry_base_seconds: float = 1.0
    retry_factor: float = 2.0
    retry_ceiling_seconds: float = 60.0

    def __post_init__(self) -> None:
        durations = (
            ("heartbeat_timeout_seconds", self.heartbeat_timeout_seconds),
            ("queued_timeout_seconds", self.queued_timeout_seconds),
            ("running_heartbeat_timeout_seconds", self.running_heartbeat_timeout_seconds),
            ("send_failure_window_seconds", self.send_failure_window_seconds),
            ("dedupe_window_seconds", self.dedupe_window_seconds),
            ("recovery_stable_seconds", self.recovery_stable_seconds),
            ("retry_base_seconds", self.retry_base_seconds),
            ("retry_factor", self.retry_factor),
            ("retry_ceiling_seconds", self.retry_ceiling_seconds),
        )
        for name, value in durations:
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} 必须是正的有限数字")
        if not isinstance(self.send_failure_threshold, int) or isinstance(
            self.send_failure_threshold, bool
        ) or self.send_failure_threshold <= 0:
            raise ValueError("send_failure_threshold 必须是正整数")
        if self.retry_factor <= 1:
            raise ValueError("retry_factor 必须大于 1，固定间隔不是退避")
        if self.retry_ceiling_seconds < self.retry_base_seconds:
            raise ValueError("retry_ceiling_seconds 不能小于 retry_base_seconds")

    @classmethod
    def from_mapping(
        cls, values: Mapping[str, str], *, prefix: str = "LINGXI_ALERT_"
    ) -> AlertPolicy:
        """从调用方已读取的配置构造策略；错误信息不回显任何取值。"""

        def number(name: str, default: float) -> float:
            raw = (values.get(f"{prefix}{name}") or "").strip()
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError:
                raise ValueError(f"{prefix}{name} 必须是正的有限数字") from None

        def integer(name: str, default: int) -> int:
            raw = (values.get(f"{prefix}{name}") or "").strip()
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError:
                raise ValueError(f"{prefix}{name} 必须是正整数") from None

        return cls(
            heartbeat_timeout_seconds=number("HEARTBEAT_TIMEOUT_SECONDS", 120.0),
            queued_timeout_seconds=number("QUEUED_TIMEOUT_SECONDS", 180.0),
            running_heartbeat_timeout_seconds=number(
                "RUNNING_HEARTBEAT_TIMEOUT_SECONDS", 90.0
            ),
            send_failure_window_seconds=number("SEND_FAILURE_WINDOW_SECONDS", 300.0),
            send_failure_threshold=integer("SEND_FAILURE_THRESHOLD", 3),
            dedupe_window_seconds=number("DEDUPE_WINDOW_SECONDS", 1800.0),
            recovery_stable_seconds=number("RECOVERY_STABLE_SECONDS", 300.0),
            retry_base_seconds=number("RETRY_BASE_SECONDS", 1.0),
            retry_factor=number("RETRY_FACTOR", 2.0),
            retry_ceiling_seconds=number("RETRY_CEILING_SECONDS", 60.0),
        )

    def retry_delay(self, attempt: int) -> float:
        """返回第 ``attempt`` 次重试前的等待秒数。"""

        if isinstance(attempt, bool) or attempt < 0:
            raise ValueError("重试次数不能为负")
        return min(self.retry_base_seconds * (self.retry_factor**attempt), self.retry_ceiling_seconds)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("告警时间必须带时区")
    return value.astimezone(_UTC)


def _category(value: str, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_CATEGORY.fullmatch(value):
        raise ValueError(f"{field} 必须是安全的类别标识")
    return value


def _safe_scope(raw: str, *, fallback_prefix: str) -> str:
    """把任意字符串归一成 ``AlertSignal.scope`` 允许的安全类别标识。

    与 ``AlertingDuty.delivery_alert_callback`` 里内联的同型归一化同一个理由：调用方
    （开通编排的 ``failure_reason``、异常类名拼出的 ``unexpected_ValueError`` 之类）
    完全不受这里的格式约束，归一失败绝不能让告警回调本身抛出异常打断调用方的主流程。
    """

    scope = re.sub(r"[^a-z0-9_.-]", "_", (raw or "unknown").lower())[:64]
    if not scope or not scope[0].isalpha():
        scope = f"{fallback_prefix}_{scope}"[:64]
    return scope


@dataclass(frozen=True)
class AlertSignal:
    """一次不含业务正文的故障观察。"""

    kind: AlertKind
    observed_at: datetime
    scope: str = "system"
    count: int = 1
    trace_id: str | None = None
    final: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", _as_utc(self.observed_at))
        object.__setattr__(self, "scope", _category(self.scope, "scope"))
        if isinstance(self.count, bool) or not isinstance(self.count, int) or self.count <= 0:
            raise ValueError("告警数量必须是正整数")
        if self.trace_id is not None:
            if not _SAFE_TRACE_ID.fullmatch(self.trace_id):
                raise ValueError("trace_id 必须是安全的标识")
        if self.kind is not AlertKind.FEISHU_SEND_FAILED and self.final:
            raise ValueError("只有飞书发送失败事件可以标记为 final")


#: 八类系统告警 → 中文标签（Trace #469 S-1 TOP-2）：此前群消息是一行英文
#: key=value（``action=alert event=worker.queued_stuck time=... count=...
#: trace_id=...``），运维之外的管理员读不懂哪个英文键对应什么故障；照抄
#: ``scripts/ops/host_health_alert.py::render_message`` 的分行中文标签范式
#: （见该脚本模块文档"防骚扰与恢复通知"一节的姊妹渲染函数），标题带
#: ``[BI Plus 运行告警]`` 前缀 + 告警/恢复动作，正文按"类型/范围/次数/时间/
#: 追溯号"五个中文标签分行——与宿主监控脚本的"容器/状态/主机/时间"四行同一
#: 视觉范式，不是碰巧长得像。
_ALERT_KIND_LABEL: dict[AlertKind, str] = {
    AlertKind.PROCESS_INACTIVE: "进程无心跳",
    AlertKind.QUEUED_STUCK: "任务排队超时未领取",
    AlertKind.RUNNING_HEARTBEAT_TIMEOUT: "任务执行中心跳超时",
    AlertKind.RETRY_EXHAUSTED: "任务重试次数耗尽",
    AlertKind.AWAITING_DELIVERY_STUCK: "投递确认超时未收到",
    AlertKind.WORKER_VERSION_UNAVAILABLE: "目标执行版本不可用",
    AlertKind.FEISHU_SEND_FAILED: "飞书发送失败",
    AlertKind.ONBOARDING_FAILED: "用户开通失败",
}

_NOTICE_ACTION_LABEL: dict[NoticeAction, str] = {
    NoticeAction.ALERT: "告警",
    NoticeAction.RECOVERY: "恢复",
}


@dataclass(frozen=True)
class AlertNotice:
    """可交给出站适配器的安全、纯文本告警。"""

    action: NoticeAction
    kind: AlertKind
    scope: str
    observed_at: datetime
    count: int
    trace_id: str | None
    dedupe_key: str

    @property
    def event_type(self) -> str:
        return f"{self.scope}.{self.kind.value}"

    @property
    def text(self) -> str:
        """只渲染类型、范围、时间、数量和 trace_id，不接收业务正文——分行中文
        标签范式（Trace #469 S-1 TOP-2），照抄
        ``scripts/ops/host_health_alert.py::render_message`` 的姊妹渲染函数。
        ``event_type`` 这个"scope.kind"组合键仍然通过 :attr:`event_type` 属性
        对外（供审计记录/告警去重使用），只是不再原样拼进人类可读正文——正文
        改成"类型"（中文标签）与"范围"两个独立标签，可读性更好，且信息量
        不减（组合键仍能从这两行反推）。
        """

        action_label = _NOTICE_ACTION_LABEL[self.action]
        kind_label = _ALERT_KIND_LABEL.get(self.kind, self.kind.value)
        trace = self.trace_id or "-"
        return (
            f"[BI Plus 运行告警] {action_label}\n"
            f"类型：{kind_label}\n"
            f"范围：{self.scope}\n"
            f"次数：{self.count}\n"
            f"时间：{self.observed_at.isoformat()}\n"
            f"追溯号：{trace}"
        )


@dataclass(frozen=True)
class HeartbeatStatus:
    component: str
    last_seen_at: datetime | None
    active: bool
    changed: bool


@dataclass
class _Heartbeat:
    timeout_seconds: float
    last_seen_at: datetime | None = None
    previous_active: bool | None = None


class HeartbeatRegistry:
    """记录进程最近一次心跳，并在窗口到期时翻转为不活跃。

    第一次检查一个尚未发过心跳的组件不会直接产生告警；启动期尚未完成装配不等于
    进程停止。调用方在常驻循环中周期 ``beat``，监控方用 ``statuses`` 判定翻转。
    """

    def __init__(self, *, default_timeout_seconds: float = 120.0) -> None:
        if not math.isfinite(default_timeout_seconds) or default_timeout_seconds <= 0:
            raise ValueError("default_timeout_seconds 必须是正的有限数字")
        self._default_timeout_seconds = default_timeout_seconds
        self._records: dict[str, _Heartbeat] = {}

    def register(self, component: str, *, timeout_seconds: float | None = None) -> None:
        component = _category(component, "component")
        timeout = self._default_timeout_seconds if timeout_seconds is None else timeout_seconds
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("heartbeat timeout 必须是正的有限数字")
        existing = self._records.get(component)
        if existing is None:
            self._records[component] = _Heartbeat(timeout_seconds=timeout)
            return
        if existing.timeout_seconds != timeout:
            raise ValueError("同一组件不能在运行中更换心跳阈值")

    def beat(self, component: str, *, at: datetime) -> None:
        component = _category(component, "component")
        self.register(component)
        record = self._records[component]
        moment = _as_utc(at)
        if record.last_seen_at is not None and moment < record.last_seen_at:
            raise ValueError("心跳时间不能倒退")
        record.last_seen_at = moment
        record.previous_active = True

    def status(self, component: str, *, at: datetime) -> HeartbeatStatus:
        """返回组件的活跃判定；``changed`` 是“自上次观察以来是否翻转”的边沿信号。

        这里**有意**推进 ``previous_active`` 基线：``changed`` 按设计是边沿触发的翻转
        信号（活跃↔不活跃只在发生的那一次为真），所以每次观察都要把基线推进到当前
        判定，否则同一次翻转会被反复报告。``HeartbeatTests`` 固化了这一语义（翻转当次
        为真、下一次同态为假），因此不能改成“读不改状态”。

        对告警决策没有副作用：唯一的生产消费者 ``AlertManager.check_heartbeats`` 只用
        ``active`` 决定观察 / 恢复，而 ``active`` 是 ``(at, last_seen_at, timeout)`` 的纯
        函数，与基线推进无关；``changed`` 只作日志与边沿判定参考。
        """

        component = _category(component, "component")
        record = self._records.get(component)
        if record is None:
            raise KeyError(component)
        moment = _as_utc(at)
        if record.last_seen_at is None:
            active = False
            changed = False
        else:
            active = (moment - record.last_seen_at).total_seconds() < record.timeout_seconds
            changed = record.previous_active is not None and active != record.previous_active
        # 推进边沿基线——理由见方法文档；只影响 changed，不影响 active 决策。
        record.previous_active = active if record.last_seen_at is not None else record.previous_active
        return HeartbeatStatus(component, record.last_seen_at, active, changed)

    def statuses(self, *, at: datetime) -> tuple[HeartbeatStatus, ...]:
        return tuple(self.status(component, at=at) for component in sorted(self._records))


@dataclass
class _FailureWindow:
    kind: AlertKind
    scope: str
    window_started_at: datetime
    last_failure_at: datetime
    consecutive_failures: int = 0
    total_count: int = 0
    last_alert_at: datetime | None = None
    recovery_since: datetime | None = None
    trace_id: str | None = None


class AlertManager:
    """阈值、故障窗口去重和稳定恢复的状态机。"""

    def __init__(
        self,
        *,
        policy: AlertPolicy | None = None,
        heartbeats: HeartbeatRegistry | None = None,
    ) -> None:
        self.policy = policy or AlertPolicy()
        self.heartbeats = heartbeats or HeartbeatRegistry(
            default_timeout_seconds=self.policy.heartbeat_timeout_seconds
        )
        self._windows: dict[tuple[AlertKind, str], _FailureWindow] = {}

    def register_process(self, component: str) -> None:
        self.heartbeats.register(
            component, timeout_seconds=self.policy.heartbeat_timeout_seconds
        )

    def heartbeat(self, component: str, *, at: datetime) -> None:
        self.heartbeats.beat(component, at=at)

    def check_heartbeats(
        self, *, at: datetime, trace_id: str | None = None
    ) -> tuple[AlertNotice, ...]:
        notices: list[AlertNotice] = []
        for status in self.heartbeats.statuses(at=at):
            if status.last_seen_at is None:
                continue
            signal = AlertSignal(
                kind=AlertKind.PROCESS_INACTIVE,
                observed_at=at,
                scope=status.component,
                trace_id=trace_id,
            )
            if status.active:
                notices.extend(self.resolve(signal))
            else:
                notices.extend(self.observe(signal))
        return tuple(notices)

    def task_stuck(
        self,
        kind: AlertKind,
        *,
        count: int,
        at: datetime,
        scope: str = "worker",
        trace_id: str | None = None,
    ) -> tuple[AlertNotice, ...]:
        if kind not in {
            AlertKind.QUEUED_STUCK,
            AlertKind.RUNNING_HEARTBEAT_TIMEOUT,
            AlertKind.RETRY_EXHAUSTED,
            AlertKind.AWAITING_DELIVERY_STUCK,
            AlertKind.WORKER_VERSION_UNAVAILABLE,
        }:
            raise ValueError("task_stuck 只接受五类任务滞留告警")
        return self.observe(
            AlertSignal(kind=kind, observed_at=at, scope=scope, count=count, trace_id=trace_id)
        )

    def send_failure(
        self,
        *,
        channel: str,
        final: bool,
        at: datetime,
        trace_id: str | None = None,
    ) -> tuple[AlertNotice, ...]:
        return self.observe(
            AlertSignal(
                kind=AlertKind.FEISHU_SEND_FAILED,
                observed_at=at,
                scope=channel,
                trace_id=trace_id,
                final=final,
            )
        )

    def send_succeeded(
        self,
        *,
        channel: str,
        at: datetime,
        trace_id: str | None = None,
    ) -> tuple[AlertNotice, ...]:
        return self.resolve(
            AlertSignal(
                kind=AlertKind.FEISHU_SEND_FAILED,
                observed_at=at,
                scope=channel,
                trace_id=trace_id,
            )
        )

    def observe(self, signal: AlertSignal) -> tuple[AlertNotice, ...]:
        key = (signal.kind, signal.scope)
        window = self._windows.get(key)
        if window is None or self._new_send_window(window, signal):
            window = _FailureWindow(
                kind=signal.kind,
                scope=signal.scope,
                window_started_at=signal.observed_at,
                last_failure_at=signal.observed_at,
            )
            self._windows[key] = window

        if signal.kind is AlertKind.FEISHU_SEND_FAILED and not signal.final:
            # 五分钟窗口过期的重置已由上面的 `_new_send_window` 统一兜底（过期即换成
            # 全新窗口，consecutive_failures 归零），这里只需累加当前失败次数。
            window.consecutive_failures += signal.count
        else:
            window.consecutive_failures = max(window.consecutive_failures, 1)

        window.last_failure_at = signal.observed_at
        window.recovery_since = None
        window.total_count += signal.count
        window.trace_id = signal.trace_id or window.trace_id

        threshold_reached = signal.final or (
            signal.kind is not AlertKind.FEISHU_SEND_FAILED
            or window.consecutive_failures >= self.policy.send_failure_threshold
        )
        if not threshold_reached:
            return ()

        if window.last_alert_at is not None and (
            signal.observed_at - window.last_alert_at
        ).total_seconds() < self.policy.dedupe_window_seconds:
            return ()

        window.last_alert_at = signal.observed_at
        return (self._notice(window, NoticeAction.ALERT, signal.observed_at),)

    def resolve(self, signal: AlertSignal) -> tuple[AlertNotice, ...]:
        key = (signal.kind, signal.scope)
        window = self._windows.get(key)
        if window is None:
            return ()
        if window.last_alert_at is None:
            # 尚未达到阈值的瞬时失败自行恢复，只留下日志，不产生恢复噪声。
            del self._windows[key]
            return ()
        if window.recovery_since is None:
            window.recovery_since = signal.observed_at
        window.trace_id = signal.trace_id or window.trace_id
        return self._recover_due(signal.observed_at)

    def tick(self, *, at: datetime) -> tuple[AlertNotice, ...]:
        """推进稳定恢复计时，也让待恢复事件不依赖下一次业务发送。"""

        return self._recover_due(_as_utc(at))

    def _recover_due(self, at: datetime) -> tuple[AlertNotice, ...]:
        notices: list[AlertNotice] = []
        for key in sorted(self._windows, key=lambda item: (item[0].value, item[1])):
            window = self._windows[key]
            if window.recovery_since is None:
                continue
            if (at - window.recovery_since).total_seconds() < self.policy.recovery_stable_seconds:
                continue
            notices.append(self._notice(window, NoticeAction.RECOVERY, at))
            del self._windows[key]
        return tuple(notices)

    def _new_send_window(self, window: _FailureWindow, signal: AlertSignal) -> bool:
        if signal.kind is not AlertKind.FEISHU_SEND_FAILED or signal.final:
            return False
        return (
            signal.observed_at - window.last_failure_at
        ).total_seconds() >= self.policy.send_failure_window_seconds

    @staticmethod
    def _notice(window: _FailureWindow, action: NoticeAction, at: datetime) -> AlertNotice:
        material = "\x00".join(
            (
                action.value,
                window.kind.value,
                window.scope,
                window.window_started_at.isoformat(),
            )
        ).encode("utf-8")
        dedupe_key = hashlib.sha256(material).hexdigest()
        return AlertNotice(
            action=action,
            kind=window.kind,
            scope=window.scope,
            observed_at=at,
            count=window.total_count,
            trace_id=window.trace_id,
            dedupe_key=dedupe_key,
        )


# ---------------------------------------------------------------------------
# 投递编排：把状态机接到管理群（Issue #92 建立，Issue #153 移至此处并接入生产
# main()）。
#
# 这一段最初写在 ``apps/scheduler/__init__.py``——#92 当时唯一的调用方是 scheduler
# 的审计日报职责。#153 需要 gateway（``DeliveryConsumer.on_alert`` 注入点）与 worker
# （队列消费循环）各自装配同一套状态机与投递重试/去重语义，三个进程各自是独立部署
# 单元，不能互相 import ``apps.<name>``（那会让镜像依赖面互相牵连）。这几个类只编排
# 注入的 ``sender``/``audit`` 接口、不直接做网络或文件 I/O（真正的 I/O 藏在调用方传入
# 的对象里），与 ``core/conversation/pipeline.py`` 里 ``EventPipeline`` 编排注入的
# ``reactions``/``replies`` 是同一种"core 里编排、adapters 里做事"的形状，因此挪到这里
# 而不是新增一个四进程都要 import 的 ``apps`` 工具模块。``apps/scheduler/__init__.py``
# 继续从这里 re-export，保持既有导入路径与测试不必改动。
# ---------------------------------------------------------------------------


class AuditSink(Protocol):
    def record(self, action: str, /, **fields: object) -> None: ...


class AlertSender(Protocol):
    """把一条已经渲染好的安全告警文本发到某个目标；具体传输由调用方注入。"""

    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None: ...


@dataclass
class _PendingAlert:
    notice: AlertNotice
    next_attempt_at: datetime
    attempt: int = 0


class AlertDispatcher:
    """把安全告警摘要投递给某个目标，并隔离投递失败。

    这里是唯一会调用发送适配器的告警编排层。发送失败只保留摘要和下一次重试时间，
    不把异常抛回调用方，也不保存告警正文之外的业务对象。真实发送行为由注入的
    sender 与各自进程的受控验证负责。
    """

    def __init__(
        self,
        *,
        sender: AlertSender,
        chat_id: str,
        policy: AlertPolicy | None = None,
        audit: AuditSink | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not chat_id:
            raise ValueError("告警投递目标不能为空")
        self._sender = sender
        self._chat_id = chat_id
        self._policy = policy or AlertPolicy()
        self._audit = audit
        self._clock = clock or (lambda: datetime.now(UTC))
        self._pending: dict[str, _PendingAlert] = {}
        self.observed_delays: list[float] = []

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def submit(self, notices: Sequence[AlertNotice]) -> None:
        """加入待投递队列；相同去重键只保留一条。"""

        now = _as_utc(self._clock())
        for notice in sorted(notices, key=lambda item: item.dedupe_key):
            self._pending.setdefault(
                notice.dedupe_key,
                _PendingAlert(notice=notice, next_attempt_at=now),
            )

    def run_once(self, *, at: datetime | None = None) -> int:
        """投递当前到期的告警，返回本轮成功数。"""

        now = _as_utc(self._clock() if at is None else at)
        sent = 0
        for dedupe_key in sorted(tuple(self._pending)):
            pending = self._pending.get(dedupe_key)
            if pending is None or pending.next_attempt_at > now:
                continue
            notice = pending.notice
            try:
                self._sender.send_text(
                    chat_id=self._chat_id,
                    text=notice.text,
                    dedupe_key=notice.dedupe_key,
                )
            except Exception as error:  # noqa: BLE001 - 告警失败只进入本地重试队列
                delay = self._policy.retry_delay(pending.attempt)
                pending.attempt += 1
                pending.next_attempt_at = now + timedelta(seconds=delay)
                self.observed_delays.append(delay)
                self._record(
                    "alert.send_failed",
                    event_type=notice.event_type,
                    action=notice.action.value,
                    attempt=pending.attempt,
                    error=type(error).__name__,
                )
                logger.error(
                    "运行告警发送失败，将重试 event=%s attempt=%s error=%s",
                    notice.event_type,
                    pending.attempt,
                    type(error).__name__,
                )
                continue
            del self._pending[dedupe_key]
            sent += 1
            self._record(
                "alert.sent",
                event_type=notice.event_type,
                action=notice.action.value,
                count=notice.count,
            )
        return sent

    def _record(self, action: str, /, **fields: object) -> None:
        if self._audit is None:
            return
        try:
            self._audit.record(action, **fields)
        except Exception as error:  # noqa: BLE001 - 审计观察失败不能改变告警重试
            logger.error("运行告警审计失败 action=%s error=%s", action, type(error).__name__)


class AlertingDuty:
    """把告警状态机、恢复计时和投递接在一个定时职责/后台线程上。

    三个进程各自装配一个实例：scheduler 挂进 ``SchedulerLoop`` 的定时职责列表；
    gateway 与 worker 没有等价的"定时职责列表"概念，直接在各自的周期性收口
    （``DeliveryConsumer.run_forever``、``WorkerService.process_once``）里调用
    ``run_once``/各回调即可，不需要专门的循环包装。
    """

    name = "运行告警"

    def __init__(
        self,
        *,
        manager: AlertManager,
        dispatcher: AlertDispatcher,
        audit: AuditSink | None = None,
        clock: Callable[[], datetime] | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        self._manager = manager
        self._dispatcher = dispatcher
        self._audit = audit
        self._clock = clock or (lambda: datetime.now(UTC))
        self._stop = threading.Event() if stop is None else stop

    @property
    def manager(self) -> AlertManager:
        return self._manager

    @property
    def dispatcher(self) -> AlertDispatcher:
        return self._dispatcher

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        self._stop.set()

    def heartbeat_callback(self, component: str) -> Callable[[], None]:
        """返回给常驻进程的无参数心跳回调。"""

        def beat() -> None:
            self._manager.heartbeat(component, at=_as_utc(self._clock()))

        return beat

    def task_stuck_callback(self) -> Callable[[str, int], None]:
        """返回给 Worker 的任务滞留回调，只接受类别与计数。"""

        def report(kind: str, count: int) -> None:
            notices = self._manager.task_stuck(
                AlertKind(kind), count=count, at=_as_utc(self._clock())
            )
            self._submit(notices)

        return report

    def send_outcome_callback(self) -> Callable[[str, bool], None]:
        """返回给卡片/群消息适配器的发送结果回调。"""

        def outcome(operation: str, succeeded: bool) -> None:
            at = _as_utc(self._clock())
            if succeeded:
                notices = self._manager.send_succeeded(channel=operation, at=at)
            else:
                notices = self._manager.send_failure(
                    channel=operation,
                    final=operation.endswith("_final"),
                    at=at,
                )
            self._submit(notices)

        return outcome

    def onboarding_failed_callback(self) -> Callable[[str, str], None]:
        """返回给首次开通编排的失败回调（Issue #280 §7.3 步 1）。

        群消息只含**故障类别**（内部原因码 ``reason``，经归一化，不是自由文本）、
        **计数**与**追溯号**（``trace_id``）；不接受、也不可能接受 open_id、姓名或
        任何资料值——回调签名里根本没有这些参数的位置。归一化失败（``reason`` 或
        ``trace_id`` 不满足 ``AlertSignal`` 的安全格式）不抛出，退化为不带
        ``trace_id`` 的信号，不让一次格式意外打断整条开通链的收尾。
        """

        def report(reason: str, trace_id: str) -> None:
            scope = _safe_scope(reason, fallback_prefix="onboarding")
            at = _as_utc(self._clock())
            try:
                signal = AlertSignal(
                    kind=AlertKind.ONBOARDING_FAILED, observed_at=at, scope=scope, trace_id=trace_id
                )
            except ValueError:
                signal = AlertSignal(kind=AlertKind.ONBOARDING_FAILED, observed_at=at, scope=scope)
            self._submit(self._manager.observe(signal))

        return report

    def onboarding_stalled_callback(self) -> Callable[[int], None]:
        """返回给「开通中途停摆收口」的计数回调（Issue #280 §7.3 步 2）。

        「有人卡住了」此前没有任何审计信号会送到管理群——这里只报一轮里真正被收口
        的候选**聚合计数**，不含 user_id、open_id 或任何单个候选的追溯号（聚合批次
        没有单一追溯号可代表这一整批）。
        """

        def report(count: int) -> None:
            if count <= 0:
                return
            at = _as_utc(self._clock())
            signal = AlertSignal(
                kind=AlertKind.ONBOARDING_FAILED,
                observed_at=at,
                scope="stalled_provisioning",
                count=count,
            )
            self._submit(self._manager.observe(signal))

        return report

    def delivery_alert_callback(self) -> Callable[[str, str], None]:
        """返回给 Gateway ``DeliveryConsumer.on_alert`` 的回调（Issue #153）。

        投递消费循环的告警形状是 ``(kind: str, task_id: str)``——``kind`` 是自由
        字符串（``"dispatch_uncertain:card_finish"``、``"progress_persist_failed:
        RuntimeError"`` 等，见 ``apps/gateway/delivery.py``），与 ``AlertKind`` 枚举
        不是同一套分类。这里统一归入 ``FEISHU_SEND_FAILED``（语义上都是"投递/飞书
        发送这条链路出了结果不明或失败"），把原始 kind 字符串标准化后放进
        ``scope``，让不同子类型各自独立限流与去重，不互相掩盖。``task_id`` 是
        ULID，通常满足 ``AlertSignal.trace_id`` 的安全格式；不满足时退化为不带
        trace_id，不让一次格式意外的输入打断整条告警链路。
        """

        def report(kind: str, task_id: str) -> None:
            scope = re.sub(r"[^a-z0-9_.-]", "_", kind.lower())[:64]
            if not scope or not scope[0].isalpha():
                scope = f"delivery_{scope}"[:64]
            at = _as_utc(self._clock())
            # PR #173 独立复核 P1-4：生产默认下 `AlertPolicy.send_failure_window_
            # seconds`（300s）与 `DeliveryConsumer.DEFAULT_ALERT_MIN_INTERVAL_
            # SECONDS`（300s，见 apps/gateway/delivery.py）两个 300 秒相等——
            # `_alert_deduped` 把同一 (kind, task_id) 的上报频率压到约每 300 秒
            # 一次，而 `AlertManager._new_send_window` 用 `>= 300s` 判定"窗口已
            # 过期、换新窗口"，于是每一次上报都恰好落在"窗口刚过期"那一侧，
            # `consecutive_failures` 每次被重置回 1，`threshold=3` 永远到不了：
            # 用生产默认复现过，两小时内单个 uncertain 任务被上报 24 次、实际
            # 发出的告警条数 = 0。
            #
            # 下面这两类不能等"攒够 N 次"：
            # - `dispatch_uncertain:*`——结果不明、按设计**绝不自动重发**，必须
            #   人工核对，是这条恢复路径的唯一入口；
            # - `fallback_send_failed:*`——文本兜底发送遇到明确拒绝错误，
            #   `V-告警-03`「终态失败立即告警」原文覆盖的正是这一类。
            # - `delivery_loop_*`（Issue #191）——投递消费循环自身的连续异常
            #   （`delivery_loop_failed:*`）与后台线程已经死亡
            #   （`delivery_loop_dead:*`）。这两类**上报之前就已经攒过次数了**：
            #   前者要求连续 `DEFAULT_LOOP_FAILURE_ALERT_THRESHOLD` 轮失败才上报，
            #   后者是一次不可逆事件，再让告警状态机攒第二遍等于重复计数，且会
            #   原样踩中上面那个 300 秒撞 300 秒的陷阱——那意味着"整条投递能力
            #   已经停摆"这件事永远发不出告警，正是 #191 要消灭的"无声"。
            # - `document_delivery_failed`/`document_delivery_uncertain`/
            #   `document_delivery_notice_failed`/`document_delivery_loop_dead:*`
            #   （Issue #341，opus 审查 R-1）——``apps/gateway/document_delivery.py``
            #   文档交付独立消费循环的同型四类：前三个各自对应一行请求的确定性终态
            #   （明确拒绝 / 结果不明 / 交付已确认但通知发送失败）或一次不可逆的
            #   循环退出事件，与上面 `dispatch_uncertain:*`/`delivery_loop_*` 同一条
            #   理由——要么"结果不明只能人工核对，不能等阈值"，要么"这条循环已经
            #   死了，再让状态机攒第二遍等于重复计数"；且文档交付本身是低频动作
            #   （见该模块 `DEFAULT_BATCH_LIMIT` 的文档），单个任务在 300 秒窗口内
            #   几乎不可能自然重复三次触发同一条 `(kind, task_id)`，原样套用
            #   "攒够阈值次数"会让这三类实质上永远发不出告警，同一个 300 秒撞
            #   300 秒的陷阱。
            # 三者都改成 `final=True`：命中即报，不等阈值；仍然受
            # `alert_min_interval_seconds`（上报节流）与 `dedupe_window_seconds`
            # （重复告警去重）双重约束，不会因为"立即"而刷屏。其余 kind
            # （目前只有 `progress_persist_failed:*`，一次可恢复的 DB 写入
            # 抖动；以及 `document_delivery_attempts_exhausted:*`/
            # `document_delivery_reclaim_failed:*`/`document_delivery_pending_
            # expired:*` 这类批量计数告警——持续复现时本来就会在窗口内自然攒够
            # 阈值，不属于"一次性事件"）继续走"攒够阈值次数"的既有降噪路径。
            final = (
                kind.startswith("dispatch_uncertain:")
                or kind.startswith("fallback_send_failed:")
                or kind.startswith("delivery_loop_")
                or kind == "document_delivery_failed"
                or kind == "document_delivery_uncertain"
                or kind == "document_delivery_notice_failed"
                or kind.startswith("document_delivery_loop_dead")
            )
            try:
                signal = AlertSignal(
                    kind=AlertKind.FEISHU_SEND_FAILED,
                    observed_at=at,
                    scope=scope,
                    trace_id=task_id,
                    final=final,
                )
            except ValueError:
                signal = AlertSignal(
                    kind=AlertKind.FEISHU_SEND_FAILED, observed_at=at, scope=scope, final=final
                )
            self._submit(self._manager.observe(signal))

        return report

    def observe(self, signal: AlertSignal) -> tuple[AlertNotice, ...]:
        notices = self._manager.observe(signal)
        self._submit(notices)
        return notices

    def run_once(self) -> tuple[AlertNotice, ...] | None:
        if self._stop.is_set():
            return None
        now = _as_utc(self._clock())
        notices = self._manager.check_heartbeats(at=now)
        notices += self._manager.tick(at=now)
        self._submit(notices)
        self._dispatcher.run_once(at=now)
        return notices

    def _submit(self, notices: Sequence[AlertNotice]) -> None:
        if not notices:
            return
        self._dispatcher.submit(notices)
        if self._audit is None:
            return
        for notice in sorted(notices, key=lambda item: item.dedupe_key):
            action = "alert.recovery_recorded" if notice.action.value == "recovery" else "alert.recorded"
            try:
                self._audit.record(
                    action,
                    event_type=notice.event_type,
                    count=notice.count,
                    trace_id=notice.trace_id or "-",
                )
            except Exception as error:  # noqa: BLE001 - 审计失败不能丢待投递告警
                logger.error("运行告警记录失败 action=%s error=%s", action, type(error).__name__)
