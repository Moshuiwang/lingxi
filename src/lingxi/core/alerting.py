"""MVP 运行告警的纯逻辑状态机。

这里不发送网络请求，也不读环境变量或数据库。调用方把时钟、任务摘要和飞书出站
结果作为事件传进来；这样 L2 可以证明阈值、退避、去重和恢复，而不会把真实飞书
平台的行为误当成本地证据。告警状态只在当前进程内保存，跨重启的真幂等留给 S9
的持久审计事件。
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Mapping


_SAFE_CATEGORY = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SAFE_TRACE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_UTC = timezone.utc


class AlertKind(str, Enum):
    """管理员需要区分的故障类型。"""

    PROCESS_INACTIVE = "process_inactive"
    QUEUED_STUCK = "queued_stuck"
    RUNNING_HEARTBEAT_TIMEOUT = "running_heartbeat_timeout"
    RETRY_EXHAUSTED = "retry_exhausted"
    FEISHU_SEND_FAILED = "feishu_send_failed"


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
    ) -> "AlertPolicy":
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
        """只渲染类型、时间、数量和 trace_id，不接收业务正文。"""

        trace = self.trace_id or "-"
        return (
            f"Lingxi 运行告警 action={self.action.value} event={self.event_type} "
            f"time={self.observed_at.isoformat()} count={self.count} trace_id={trace}"
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
        }:
            raise ValueError("task_stuck 只接受三类任务滞留告警")
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
            if (
                window.consecutive_failures
                and (signal.observed_at - window.last_failure_at).total_seconds()
                > self.policy.send_failure_window_seconds
            ):
                window.window_started_at = signal.observed_at
                window.consecutive_failures = 0
                window.total_count = 0
                window.last_alert_at = None
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
