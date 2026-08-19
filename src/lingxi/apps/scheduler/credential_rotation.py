"""专用授权凭据轮换职责：:class:`CredentialRotationLoop`。

从 :mod:`lingxi.apps.scheduler`（#237 拆分）搬出。它是「四达文档会议助手」
``refresh_token`` 的唯一消费者（到期轮换 + Issue #215 的按需供给两个入口），退出语义
与失败处置规则见包的 ``__init__.py`` 模块文档。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from lingxi.core.identity.access_token_supply import (
    AccessTokenUnavailable,
    DerivedAccessTokenHolder,
)
from lingxi.core.identity.credentials import (
    AuthorizationGrant,
    CredentialAction,
    CredentialSaveOutcome,
    DerivedAccessToken,
    RefreshAlreadyConsumedToday,
    RefreshOutcome,
    decide_after_refresh,
)
from lingxi.core.identity.identifiers import redact_identifier

from lingxi.apps.scheduler.config import DEFAULT_INTERVAL_SECONDS

logger = logging.getLogger(__name__)

# 新凭据写库的退避重试间隔（秒）。飞书侧旧凭据在续期成功那一刻已作废，
# 这里的每一次重试都是在挽救一条一次性凭据；用 _stop.wait 而不是 sleep，
# 让 SIGTERM 仍能立即打断等待。
SAVE_RETRY_BACKOFF_SECONDS = (0.2, 1.0, 3.0)


@dataclass(frozen=True)
class RotationReport:
    claimed: int = 0
    rotated: int = 0
    revoked: int = 0
    # 领取到了、也换到了新凭据，但**新凭据没有落盘**——期间发生了新授权，本链结果作废。
    # 与写盘失败同一条口径：当前生效的凭据不是本次产生的，因此不计 ``rotated``；
    # 但也不撤销（新凭据不能被旧链连带删掉），因此不计 ``revoked``。
    superseded: int = 0


class _Vault(Protocol):
    def claim_due(self, *, for_supply: bool = ...) -> Any: ...
    def save(
        self,
        *,
        subject_open_id: str,
        grant: AuthorizationGrant,
        issued_at: Any = ...,
        replacing_generation: Any = ...,
        expected_registered_subject_open_id: Any = ...,
        refresh_consumed_at: Any = ...,
    ) -> Any: ...
    def revoke(self, *, reason: str, generation: Any = ...) -> bool: ...


class _Authorization(Protocol):
    def refresh(self, current: AuthorizationGrant) -> tuple[AuthorizationGrant, Any]: ...


class CredentialRotationLoop:
    """按飞书返回有效期的 80% 触发轮换的扫描循环，**兼一次性 ``refresh_token`` 的唯一消费者**。

    循环本身不判断"该不该轮换"——到期判定写在 SQL 的领取条件里（轮换点已由
    :func:`lingxi.core.identity.credentials.rotation_deadline` 算好并落库），
    失败后的处置写在 :func:`decide_after_refresh`。这里只负责编排与退出。

    Issue #215 给它加了第二个入口 :meth:`refresh_for_supply`：花名册日报每天要一次派生
    短期令牌，而换取它必须消费同一条一次性 ``refresh_token``。**唯一消费者这条边界因此
    没有变**——日报不自己去换，而是让本职责按需换一次，再从进程内持有者取。产品负责人
    2026-08-18 裁定接受由此带来的按日消费节奏（原为约 5.6 天一次）。

    两个入口共用同一套纪律：领取（原子置位消费标记）→ 换新 → **先成功落盘、再交出**。
    次序不能反：``refresh_token`` 一次性有效，"换到了但没落盘"等于凭据丢失，而先把派生
    令牌交出去会让这条丢失路径在日报正常工作的表象下发生。
    """

    name = "凭据轮换"

    def __init__(
        self,
        *,
        vault: _Vault,
        authorization: _Authorization,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        stop: threading.Event | None = None,
        holder: DerivedAccessTokenHolder | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._vault = vault
        self._authorization = authorization
        self._interval_seconds = interval_seconds
        # 与同一进程内的其他职责共享停止标志：SIGTERM 必须一次让所有职责都停止
        # 领取新工作，而不是只停下恰好持有信号处理函数的那一个。
        self._stop = threading.Event() if stop is None else stop
        # 派生短期令牌的进程内持有者。没有它时轮换照常工作，只是派生令牌被丢弃
        # （接线之前的行为）；有它时到期轮换与按需续期都把令牌喂进去。
        self._holder = holder
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # 「上一次消费换回来的那份派生令牌不可用」的记号，值是**那一次消费的权威时刻**。
        # 没有它的话，当天后续每一轮都只会看到「今天已经换过了」，而真正的原因（飞书没给
        # 寿命、或令牌一到手就临期）再也不会出现在审计里——一个真实故障被一个例行拒绝
        # 盖住（两路审查 O7）。
        #
        # 用消费时刻而不是凭据世代号来绑定：这个时刻由凭据库在锁内生成、随新凭据一起
        # 落盘，因此**每一次消费都唯一**——换了凭据代际（人工重授权、或另一个进程完成
        # 的轮换）必然带来不同的消费时刻或干脆没有消费标记，记号自动失效。世代号做不到
        # 这一点：``save`` 生成的新世代号根本不会回到这里（收口轮 P2-c②）。
        self._derived_unusable_at: datetime | None = None

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    @property
    def derived_token_holder(self) -> DerivedAccessTokenHolder | None:
        """本职责写入派生短期令牌的那个持有者。

        只读暴露，供装配处断言"日报侧读的正是轮换职责写的那一份"——这条链断了不会有
        任何用例变红，除非它有一个可观察的接缝（两路审查 O4）。
        """

        return self._holder

    def request_stop(self) -> None:
        self._stop.set()

    def run_once(self) -> RotationReport:
        """领取至多一条到期凭据并处理它。已经在停止中则一条都不领。"""

        if self._stop.is_set():
            return RotationReport()
        # 先收殓崩溃窗口留下的「已消费未落库」行：它们的旧令牌已被飞书作废，
        # 不收殓就会在租期结束后被当成正常凭据再领取一次（Codex 复查发现）。
        stale_collector = getattr(self._vault, "revoke_stale_consumed", None)
        if callable(stale_collector):
            stale_collector()
        if self._stop.is_set():
            # SIGTERM 可能在收殓等待文件锁期间到达：领取前必须再看一次，
            # 否则会在关闭宽限期里再启动一条最长 20 秒的续期请求（终轮 Codex）。
            return RotationReport()
        claim = self._vault.claim_due()
        if claim is None:
            return RotationReport()

        # 权威消费时刻由凭据库在文件锁内生成并随领取返回；这里绝不自己再算一个。
        consumed_at = getattr(claim, "consumed_at", None) or self._clock()
        derived: Any = None
        try:
            replacement, derived = self._authorization.refresh(claim.grant)
            outcome = RefreshOutcome.ROTATED
        except Exception as error:  # noqa: BLE001 - 任何异常都不足以证明"旧凭据还能用"
            replacement = None
            outcome = RefreshOutcome.FAILED if _is_definite_failure(error) else RefreshOutcome.INDETERMINATE
            logger.warning("专用授权续期未成功 outcome=%s error=%s", outcome.value, type(error).__name__)

        claim_generation = getattr(claim, "generation", None) or None
        if decide_after_refresh(outcome) is CredentialAction.ROTATE and replacement is not None:
            saved = self._save_with_retry(
                subject_open_id=claim.subject_open_id,
                replacement=replacement,
                replacing_generation=claim_generation,
                refresh_consumed_at=consumed_at,
            )
            if saved is CredentialSaveOutcome.SAVED:
                logger.info("专用授权凭据已轮换 subject=%s", redact_identifier(claim.subject_open_id))
                # **落盘成功之后**才把派生令牌交给进程内持有者：反过来会让"续期成功但
                # 写盘失败＝凭据丢失"这条路径在日报照常工作的表象下发生。
                self._remember_derived(derived, consumed_at=consumed_at)
                return RotationReport(claimed=1, rotated=1)
            if saved is CredentialSaveOutcome.SUPERSEDED:
                # 期间发生了新授权：本链的新凭据被丢弃，而旧的那条已经在飞书那边消费掉。
                # 不撤销（不能连带删掉新凭据），但**一个令牌都不交出**——交出去日报会照常
                # 工作到令牌过期为止，把"这条链已经死了"整整盖住那么久（两路审查 P1）。
                logger.warning(
                    "轮换结果已被期间的新授权取代，本链结果作废，派生短期令牌不予交出 subject=%s",
                    redact_identifier(claim.subject_open_id),
                )
                # 不计 rotated：当前生效的凭据不是本次产生的（与写盘失败同一条口径）。
                return RotationReport(claimed=1, superseded=1)
            # 新凭据没能落库：旧的此刻已被飞书作废，继续留着只会让下一轮拿死
            # 凭据再撞一次墙。撤销并用可与普通失败区分的日志请求人工重新授权
            # （独立复查发现：此前这里的异常会带着仅存于内存的新凭据一起消失）。
            logger.error(
                "不可恢复：续期成功但新凭据写库失败，旧凭据已被飞书作废，需人工重新授权 subject=%s",
                redact_identifier(claim.subject_open_id),
            )
            self._vault.revoke(reason="rotation_persist_failed", generation=claim_generation)
            return RotationReport(claimed=1, revoked=1)

        # 只撤销领取到的那一代：期间的新授权不得被旧链失败连带删除（终轮 Codex）。
        self._vault.revoke(reason=f"refresh_{outcome.value}", generation=claim_generation)
        return RotationReport(claimed=1, revoked=1)

    def refresh_for_supply(self) -> None:
        """**按需**消费一次续期，把派生短期令牌交给进程内持有者（Issue #215）。

        由花名册日报的令牌供给（:class:`~lingxi.core.identity.access_token_supply.
        RosterAccessTokenProvider`）调用，本身不返回令牌——令牌只经持有者流转，而持有者
        只在**新凭据成功落盘之后**才被写入。这条次序是本方法存在的全部理由：日报不自己
        去消费一次性 ``refresh_token``，唯一消费者仍是本职责。

        与到期轮换的唯一不同写在一个参数里：``for_supply=True``。它把"放开到期判定"
        与"每 UTC 日至多一次"捆成一件事，由凭据库在**自己的文件锁内、用锁内的当前时刻**
        判定。上界因此只有一份判据、只有一个时钟：

        - 进程重启、崩溃重启循环、同一宿主机上的第二个实例都绕不过它；
        - 人工重授权换来的新凭据没有消费标记，**当天立刻可以再换一次**——这是已接受的
          语义，而任何一份进程内账本副本都会认不出新凭据、继续拒到第二天（收口轮 P1）。

        每一次调用都要去开一次文件锁（拒绝时也是）。这是刻意的取舍：正常情况下日报一天
        只问一次（持有者里有新鲜令牌时根本不会走到这里），异常情况下每轮一次文件锁的
        代价，换的是"上界只有一个权威"。

        失败一律抛 :class:`AccessTokenUnavailable`，**只带分类、不带值、traceback 里
        不展示原始异常**；凭据的处置与到期轮换完全一致（续期失败撤销、写盘失败撤销并按
        不可恢复留痕），因为"这条一次性凭据还能不能用"与是谁触发的这次续期无关。
        """

        if self._stop.is_set():
            # 停止中不再开启任何一次续期：半途中断的续期等于凭据丢失（模块头注释）。
            raise AccessTokenUnavailable("scheduler_stopping")
        try:
            claim = self._vault.claim_due(for_supply=True)
        except RefreshAlreadyConsumedToday as error:
            raise AccessTokenUnavailable(self._refusal_reason(error)) from None
        if claim is None:
            # 没有可领取的凭据（未授权、已撤销、或正被另一条链消费中）。
            raise AccessTokenUnavailable("no_credential_available")
        consumed_at = getattr(claim, "consumed_at", None) or self._clock()

        claim_generation = getattr(claim, "generation", None) or None
        try:
            replacement, derived = self._authorization.refresh(claim.grant)
        except Exception as error:  # noqa: BLE001 - 任何异常都不足以证明"旧凭据还能用"
            outcome = RefreshOutcome.FAILED if _is_definite_failure(error) else RefreshOutcome.INDETERMINATE
            logger.warning("按需续期未成功 outcome=%s error=%s", outcome.value, type(error).__name__)
            self._vault.revoke(reason=f"refresh_{outcome.value}", generation=claim_generation)
            # from None：原始异常留在 __cause__ 里，任何一次 traceback 打印都会把响应
            # 正文（可能含令牌）带进日志。排障信息以净化过的类名走上面那行日志。
            raise AccessTokenUnavailable(f"refresh_{outcome.value}") from None

        saved = self._save_with_retry(
            subject_open_id=claim.subject_open_id,
            replacement=replacement,
            replacing_generation=claim_generation,
            refresh_consumed_at=consumed_at,
        )
        if saved is CredentialSaveOutcome.FAILED:
            logger.error(
                "不可恢复：按需续期成功但新凭据写库失败，旧凭据已被飞书作废，需人工重新授权 subject=%s",
                redact_identifier(claim.subject_open_id),
            )
            self._vault.revoke(reason="rotation_persist_failed", generation=claim_generation)
            # 落盘失败就**不交出令牌**：让日报这一轮失败，好过在凭据已经丢失的情况下
            # 照常发出日报、把丢失掩盖到下一次轮换才被发现。
            raise AccessTokenUnavailable("credential_persist_failed")
        if saved is CredentialSaveOutcome.SUPERSEDED:
            # 期间有新授权：本链的新凭据被丢弃，旧的已在飞书那边作废。不撤销（新凭据
            # 不能被旧链连带删掉），但同样**什么都不交出**——这条链已经没有活着的凭据了。
            logger.warning(
                "按需续期的结果已被期间的新授权取代，本链结果作废，派生短期令牌不予交出 subject=%s",
                redact_identifier(claim.subject_open_id),
            )
            raise AccessTokenUnavailable("no_credential_available")

        logger.info("专用授权凭据已按需轮换 subject=%s", redact_identifier(claim.subject_open_id))
        if not self._remember_derived(derived, consumed_at=consumed_at):
            # 凭据没有丢（已落盘），只是这份派生令牌不可用。不撤销、不重试。
            raise AccessTokenUnavailable("derived_token_unusable")

    def _refusal_reason(self, error: RefreshAlreadyConsumedToday) -> str:
        """今天已经换过一次时，对外报哪个分类。

        默认报上界本身；但如果**正是那一次**换到的派生令牌不可用，真实原因是它，不是
        "今天已经换过了"。原因漂移会让一个真实故障看起来像例行拒绝（O7）。

        比对的是消费时刻：凭据上记着的那一刻必须与本进程记下"不可用"的那一刻是同一个。
        换了凭据代际（人工重授权、或另一个进程完成的轮换）时两者必然不同，记号自动失效
        ——那种情况下本进程对新凭据一无所知，只能如实报上界。
        """

        if self._derived_unusable_at is not None and self._derived_unusable_at == error.consumed_at:
            return "derived_token_unusable"
        return "refresh_already_consumed_today"

    def _remember_derived(self, derived: Any, *, consumed_at: datetime) -> bool:
        """把派生令牌交给进程内持有者。没有持有者或令牌不可用时返回 ``False``。

        "可用"的判据由持有者给：它只在这份令牌**当场就能通过新鲜度判定**时才算成功，
        因此一份一到手就临期的令牌不会被误当成拿到了（收口轮 P2-c①）。

        令牌值不进日志：这里只记"有没有拿到可用的一份"，以及"哪一次消费换回来的那份
        不可用"。
        """

        if self._holder is None:
            # 没有装配持有者（接线之前的形态）。不是"这一份不可用"，不记那个状态。
            return False
        if not isinstance(derived, DerivedAccessToken):
            logger.warning("续期未交出派生短期令牌，花名册读取本轮无令牌可用")
            self._derived_unusable_at = consumed_at
            return False
        if not self._holder.store(derived, now=self._clock()):
            logger.warning("派生短期令牌不可用（寿命未知或一到手就临期），不予缓存")
            self._derived_unusable_at = consumed_at
            return False
        self._derived_unusable_at = None
        return True

    def _save_with_retry(
        self,
        *,
        subject_open_id: str,
        replacement: Any,
        replacing_generation: str | None = None,
        refresh_consumed_at: datetime | None = None,
    ) -> CredentialSaveOutcome:
        """新凭据落盘带短退避重试：一次瞬时抖动不该报废一条一次性凭据。

        返回**三态**而不是布尔（:class:`CredentialSaveOutcome`）：轮换收尾只关心"要不要
        撤销"，因此"落盘了"与"被新授权取代"曾被压成同一个真值；但派生短期令牌关心的是
        "这条链还有没有活着的凭据"，两者在那个问题上是相反的答案。
        """

        for delay_seconds in (0.0, *SAVE_RETRY_BACKOFF_SECONDS):
            if delay_seconds:
                self._stop.wait(delay_seconds)
            try:
                saved = self._vault.save(
                    subject_open_id=subject_open_id,
                    grant=replacement,
                    replacing_generation=replacing_generation,
                    expected_registered_subject_open_id=subject_open_id,
                    refresh_consumed_at=refresh_consumed_at,
                )
                if saved is False:
                    # 世代不符或主体登记 CAS 未通过＝期间有新授权：**新凭据没有落盘**。
                    # 对撤销判定来说这算已妥善收尾，对派生令牌来说这条链已经死了。
                    return CredentialSaveOutcome.SUPERSEDED
                return CredentialSaveOutcome.SAVED
            except Exception as error:  # noqa: BLE001 - 记录后重试，最终失败由调用方处置
                logger.warning("新凭据写库失败，将重试 error=%s", type(error).__name__)
        return CredentialSaveOutcome.FAILED

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as error:  # noqa: BLE001 - 定时职责不因一轮异常而终止
                logger.error("本轮续期扫描异常，下一轮继续 error=%s", type(error).__name__)
            if self._stop.is_set():
                break
            self._stop.wait(self._interval_seconds)
        logger.info("续期扫描已停止领取并退出")


def _is_definite_failure(error: BaseException) -> bool:
    """区分"飞书明确拒绝"与"结果不明确"。

    两者的处置**相同**（都撤销），区分只为了让日志与后续审计能分辨这两件事。
    分类是协议细节，由 adapters 层以 ``definite`` 属性给出（代码框架第二节：
    协议细节不进 apps 层）；没有该属性的异常一律视为"结果不明确"。
    """

    definite = getattr(error, "definite", None)
    return definite is True
