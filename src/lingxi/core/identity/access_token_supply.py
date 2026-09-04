"""花名册日报所用短期令牌供给的规则。

只有规则，没有 I/O：真正的续期与落盘由 ``lingxi.apps.scheduler`` 的凭据轮换职责
执行，这里只回答三个问题——手上这份令牌还能不能用、该不该现在去换一次、换不到时
对外说什么。花名册日报每天要多次短期 ``access_token``，唯一来源是专用授权主体
那条一次性 ``refresh_token`` 的续期，凭据轮换职责是唯一消费者。

三条边界：（1）派生令牌只在进程内（:class:`DerivedAccessTokenHolder`），不落盘，
重启后为空；（2）两道频率上界（最小消费间隔、每 UTC 日消费次数上界），判据只有
一份——凭据文件里的消费标记，由凭据库在自己的文件锁内判定，不能有认不出凭据代际
的第二份进程内副本；（3）拿不到令牌就失败关闭
（:class:`AccessTokenUnavailableError`），对外只报分类，绝不回显任何值，也绝不
返回空串。已知并接受的残留：人工重授权会重置频率上界（人为门槛本身就是保护）；
"唯一消费者"的源码扫描是绊线，不承诺拦住有意绕过。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from lingxi.core.identity.credentials import DerivedAccessToken, SecretToken

#: 判定"还能不能用"时预留的安全余量。飞书返回的是发放那一刻的寿命，而令牌要经过
#: 一整轮分页读取；卡着过期点交出去，等于把失败推给读到一半的那一页。
DEFAULT_ACCESS_TOKEN_SAFETY_MARGIN = timedelta(minutes=5)

#: 供给失败的**全部**分类。刻意用固定词表而不是自由文本：分类会进审计与异常正文，
#: 一个 f-string 就足以把令牌值、响应体或群 ID 带出去（`V-花名册-33` 的同一条理由）。
#: 新增分类要同时想清楚"它会出现在审计里"这件事，因此必须显式加进本表。
SUPPLY_FAILURE_REASONS = frozenset(
    {
        # 进程正在停止：停止之后不再开启任何一次续期（半途中断的续期等于凭据丢失）。
        "scheduler_stopping",
        # 凭据库里没有可领取的凭据（未授权、已撤销、或正被另一条链消费中）。
        "no_credential_available",
        # 距上一次消费还未满最小间隔（默认 5 分钟）。判据在凭据文件里、由凭据库在
        # 锁内判定，因此进程重启、崩溃重启循环与第二个实例都绕不过它。与下面的
        # "当日上界"是两个不同的运维处置：这个通常是瞬时的，等一会儿再试就好。
        "refresh_min_interval_not_elapsed",
        # 当日消费次数已达上界（默认 100 次）。判据同样在凭据文件里、锁内判定。
        # 撞上它说明系统已经异常了数小时（最小间隔已把崩溃循环压到至多 12 次/小时），
        # 与"最小间隔未到"不是同一件事——这个要等到下一个 UTC 日才会恢复。
        "refresh_daily_limit_reached",
        # 飞书明确拒绝了这次续期。
        "refresh_failed",
        # 这次续期结果不明确（超时、连接中断、响应不完整）。
        "refresh_indeterminate",
        # 续期成功但新凭据没能落盘：**不交出令牌**，并按凭据丢失风险响亮留痕。
        "credential_persist_failed",
        # 续期成功、凭据已落盘，但派生令牌不可用（飞书未返回寿命，或刚拿到就已临期）。
        "derived_token_unusable",
        # 注入的续期实现抛了本模块不认识的异常：只记这一个分类，不记异常正文。
        "refresh_error",
    }
)


class AuditSink(Protocol):
    """审计出口。与 ``core/alerting.py``、``apps/scheduler`` 的同名 Protocol 结构一致。"""

    def record(self, action: str, /, **fields: object) -> None: ...


class AccessTokenUnavailableError(RuntimeError):
    """短期令牌供给失败。

    ``reason`` 必须取自 :data:`SUPPLY_FAILURE_REASONS`——构造期就校验，因此
    "异常正文里不会出现令牌值"是一条结构事实，不是靠调用点自觉。
    """

    def __init__(self, reason: str) -> None:
        if reason not in SUPPLY_FAILURE_REASONS:
            raise ValueError("短期令牌供给的失败分类必须取自固定词表（不回显收到的值）")
        super().__init__(reason)
        self.reason = reason


def _require_utc(moment: datetime, name: str) -> datetime:
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{name} 必须是带时区的 UTC 时间")
    return moment.astimezone(UTC)


class DerivedAccessTokenHolder:
    """派生短期令牌的进程内持有者。

    只有两个动作：轮换成功后存进来、按当前时刻取出来。不落盘，重启即空。

    多线程并发访问需要锁：``_token``/``_expires_at`` 是两个字段，不加锁时可能读到
    半更新组合，把新鲜令牌误判为过期；最坏后果有界，不触及凭据消费面。已知边界
    （接受）：并发 ``store()`` 不保证新旧顺序，较旧结果可能覆盖较新结果——过期
    检查兜底、消费侧失败会重试，可自愈；若飞书让同一 refresh_token 的旧
    access_token 随新签发立即失效，出现真实供给失败时需升级为按序号/时间戳排序。
    """

    __slots__ = ("_token", "_expires_at", "_safety_margin", "_lock")

    def __init__(self, *, safety_margin: timedelta = DEFAULT_ACCESS_TOKEN_SAFETY_MARGIN) -> None:
        if not isinstance(safety_margin, timedelta) or safety_margin < timedelta(0):
            raise ValueError("短期令牌的安全余量必须是非负的时间长度")
        self._token: SecretToken | None = None
        self._expires_at: datetime | None = None
        self._safety_margin = safety_margin
        self._lock = threading.Lock()

    @property
    def safety_margin(self) -> timedelta:
        return self._safety_margin

    @property
    def has_token(self) -> bool:
        """是否持有令牌。**只回答有无，不回答值**，也不判断新鲜与否。"""

        with self._lock:
            return self._token is not None

    def store(self, derived: DerivedAccessToken, *, now: datetime) -> bool:
        """存入一份刚派生出来的令牌；存不成一份马上就能用的就返回 ``False``。

        两种存不成的情况处理相同——寿命未知，或一到手就已临期（剩余寿命不足安全
        余量）：都不写入，避免"成功"让调用方以为拿到了可用令牌，把真实原因从
        审计里抹掉。返回值的含义因此是能不能用，不是有没有写进去；拒绝时不动
        已经持有的那一份，一次没带寿命的响应不该把手上还新鲜的令牌一起作废。

        两行赋值在锁内完成：``_token``/``_expires_at`` 必须同时对读者可见，不能
        出现半更新组合。
        """

        moment = _require_utc(now, "now")
        if not isinstance(derived, DerivedAccessToken):
            raise TypeError("只接受 DerivedAccessToken，避免明文令牌以裸字符串流转")
        if derived.expires_in is None:
            return False
        expires_at = moment + timedelta(seconds=derived.expires_in)
        if moment + self._safety_margin >= expires_at:
            return False
        with self._lock:
            self._token = derived.token
            self._expires_at = expires_at
        return True

    def fresh(self, *, now: datetime) -> SecretToken | None:
        """取出仍然新鲜的令牌；没有或已临期时返回 ``None``。

        临期按安全余量提前判死，不是等到过期那一刻。两个字段在同一把锁内成对
        读出（与 :meth:`store` 共用同一把锁），不会读到跨调用拼出来的半更新组合。
        """

        moment = _require_utc(now, "now")
        with self._lock:
            token = self._token
            expires_at = self._expires_at
        if token is None or expires_at is None:
            return None
        if moment + self._safety_margin >= expires_at:
            return None
        return token

    def clear(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = None

    def __repr__(self) -> str:  # pragma: no cover - 由 HolderTest 的形状断言覆盖
        with self._lock:
            has_token = self._token is not None
        return f"DerivedAccessTokenHolder(has_token={has_token})"


class RosterAccessTokenProvider:
    """花名册读取的短期令牌供给：``Callable[[], str]``，交给 ``BitableRosterPages``。

    判定次序刻意：有新鲜的直接给；没有或已临期才触发一次受控续期（频率上界不在
    这里，属于凭据文件那一侧）；续期与落盘全部成功之后才可能拿到令牌，顺序不能反。

    失败一律抛 :class:`AccessTokenUnavailableError`，绝不返回空串，不展示原始
    异常正文（响应体乃至令牌可能混在其中），只给净化后的异常类名。审计只记分类
    且同一 UTC 日同一分类只记一条——本供给被多线程并发调用，判重的读改写需要加锁。
    """

    def __init__(
        self,
        *,
        holder: DerivedAccessTokenHolder,
        refresh: Callable[[], None],
        audit: AuditSink | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(refresh):
            raise ValueError("refresh 必须是执行一次受控续期的可调用对象")
        self._holder = holder
        self._refresh = refresh
        self._audit = audit
        self._clock = clock or (lambda: datetime.now(UTC))
        self._audited_on: date | None = None
        self._audited_reasons: set[str] = set()
        # 审计去重专用锁：只包住 `_record` 内部对 `_audited_on`/`_audited_reasons`
        # 的读改写，不包住 `__call__` 里对 `_refresh()`/`_holder` 的调用——那两者
        # 各自已有自己的并发防线，不需要再借用这把锁去覆盖它们。
        self._audit_lock = threading.Lock()

    def __call__(self) -> str:
        now = _require_utc(self._clock(), "now")
        token = self._holder.fresh(now=now)
        if token is not None:
            return token.reveal()

        try:
            self._refresh()
        except AccessTokenUnavailableError as error:
            self._record(error.reason, now)
            raise
        except Exception as error:  # 只保留分类，异常正文可能带响应内容
            self._record("refresh_error", now, error_type=type(error).__name__)
            raise AccessTokenUnavailableError("refresh_error") from None

        moment = _require_utc(self._clock(), "now")
        token = self._holder.fresh(now=moment)
        if token is None:
            # 续期与落盘都成功了，只是这份派生令牌用不了（寿命未知，或刚拿到就临期）。
            # 凭据没有丢，因此**不撤销**；这次消费已经计入频率上界，下一次重试受最小
            # 间隔（默认 5 分钟）约束，不是要等到明天。
            raise self._unavailable("derived_token_unusable", moment)
        self._record_success(moment)
        return token.reveal()

    # ---- 内部 -------------------------------------------------------------

    def _unavailable(self, reason: str, now: datetime) -> AccessTokenUnavailableError:
        self._record(reason, now)
        return AccessTokenUnavailableError(reason)

    def _record(self, reason: str, now: datetime, **sanitized: str) -> None:
        """记一条失败审计；同一天同一分类只记一条。

        ``sanitized`` 只接受已经确认不含任何值的补充字段（目前只有异常类名）。
        判重的读改写在 ``_audit_lock`` 内完成，实际发出审计留在锁外——I/O 不需要
        占着这把只保护内存去重状态的锁。

        ``_audited_on`` 只单调前进：跨 UTC 午夜的迟到调用可能带着一个比当前
        ``_audited_on`` 更旧的日期到达，只在 ``today`` 严格晚于当前值时才前进/
        重置，避免把日期"倒退"回前一天、重置掉当天已积累的去重集合。
        """

        today = now.date()
        with self._audit_lock:
            if self._audited_on is None or today > self._audited_on:
                self._audited_on = today
                self._audited_reasons = set()
            if reason in self._audited_reasons:
                return
            self._audited_reasons.add(reason)
        if self._audit is not None:
            # 只有分类与日期。令牌、凭据、响应正文一个字节都不进审计。
            self._audit.record(
                "roster_access_token.unavailable",
                reason=reason,
                report_date=today.isoformat(),
                **sanitized,
            )

    def _record_success(self, now: datetime) -> None:
        if self._audit is not None:
            self._audit.record(
                "roster_access_token.refreshed",
                mode="on_demand",
                report_date=now.date().isoformat(),
            )


#: 向后兼容别名，供既有导入方使用。
AccessTokenUnavailable = AccessTokenUnavailableError
