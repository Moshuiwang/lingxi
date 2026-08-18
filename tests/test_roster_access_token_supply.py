"""花名册日报的短期令牌供给（Issue #215 主接线，方案 C）。

产品负责人 2026-08-18 裁定接受**按日节奏**（留痕见 [#203 决策评论]
(https://github.com/Moshuiwang/lingxi/issues/203#issuecomment-5321623142) 第 7 项）：
一次性 ``refresh_token`` 的消费从约 5.6 天一次改为按需（事实上按日），**凭据轮换
职责仍是唯一消费者**。本文件钉住这条接线的全部边界。

认领断言：

- ``V-身份-03``（凭据只以密文落盘、不进日志与交付物）在派生短期令牌上的落地：
  短期令牌**连密文都不落**，只在进程内；
- ``V-身份-04``（续期失败或结果不明确时撤销、不重放一次性凭据）在按需入口上的落地；
- ``V-花名册-29``（四个前置任缺其一不注册、前置齐备时由 ``build_loop`` 真实注册，
  且**注册完全由配置驱动**）——第四个前置"读取令牌供给"由本次接线兑现；
- ``V-花名册-33``（审计与日志不含资料值）在令牌供给这一侧的落地。

**没有覆盖什么**：真实飞书续期返回的 ``expires_in`` 取值、真实的按日消费节奏与真实
日报送达仍属 L4a（证据等级 1）。这里的全部断言跑在注入的假凭据库、假授权客户端与
假时钟上，与 ``adapters/feishu_directory.py`` 同一姿态。
"""

from __future__ import annotations

import ast
import pathlib
import unittest
from datetime import date, datetime, timedelta, timezone
from typing import NamedTuple
from unittest import mock

from lingxi.apps.scheduler import CredentialRotationLoop
from lingxi.core.identity.access_token_supply import (
    DEFAULT_ACCESS_TOKEN_SAFETY_MARGIN,
    SUPPLY_FAILURE_REASONS,
    AccessTokenUnavailable,
    DailyRefreshBudget,
    DerivedAccessTokenHolder,
    RosterAccessTokenProvider,
)
from lingxi.core.identity.credentials import (
    AuthorizationGrant,
    DerivedAccessToken,
    RefreshAlreadyConsumedToday,
    SecretToken,
)

REPOSITORY_ROOT = pathlib.Path(__file__).parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src" / "lingxi"

FAKE_REFRESH_TOKEN = "fake-refresh-token-for-tests-only"
FAKE_ACCESS_TOKEN = "fake-access-token-for-tests-only"
FAKE_NEXT_REFRESH_TOKEN = "fake-next-refresh-token-for-tests-only"
DAY = datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)


def derived(*, lifetime: int | None = 7200, value: str = FAKE_ACCESS_TOKEN) -> DerivedAccessToken:
    return DerivedAccessToken(SecretToken(value), lifetime)


def no_retry_backoff():
    """把落盘重试的退避间隔压成 0。

    **只压等待，不减次数**：重试次数才是被测行为，等待只是墙钟成本。不这么做的话，
    两条"永久写盘失败"的用例要各自空等 4.2 秒，而它们跑在每次门禁上。
    """

    return mock.patch("lingxi.apps.scheduler.SAVE_RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0))


class MovableClock:
    """可手动推进的时钟：跨日与临期的用例要能自己决定"现在几点"。"""

    def __init__(self, now: datetime = DAY) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now = self.now + delta


class RecordingAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, object]]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, dict(fields)))

    def actions(self) -> list[str]:
        return [action for action, _ in self.records]

    def rendered(self) -> str:
        return " ".join(
            f"{action} {sorted(fields.items())}" for action, fields in self.records
        )


# --------------------------------------------------------------------------
# 一、进程内持有者：不落盘、按寿命判新鲜
# --------------------------------------------------------------------------


class HolderTest(unittest.TestCase):
    def test_a_cold_holder_has_nothing_to_hand_out(self) -> None:
        """重启后为空。短期令牌没有任何持久载体，这是刻意的（凭据不进数据库/用户环境）。"""

        holder = DerivedAccessTokenHolder()

        self.assertFalse(holder.has_token)
        self.assertIsNone(holder.fresh(now=DAY))

    def test_a_stored_token_is_handed_out_until_the_safety_margin(self) -> None:
        holder = DerivedAccessTokenHolder()

        self.assertTrue(holder.store(derived(lifetime=7200), now=DAY))

        token = holder.fresh(now=DAY + timedelta(hours=1))
        self.assertIsNotNone(token)
        self.assertEqual(token.reveal(), FAKE_ACCESS_TOKEN)

    def test_a_token_inside_the_safety_margin_counts_as_expired(self) -> None:
        """临期按安全余量提前判死：一轮花名册要读很多页，卡着过期点交出去等于把
        失败推给读到一半的那一页。"""

        holder = DerivedAccessTokenHolder()
        holder.store(derived(lifetime=7200), now=DAY)
        expires_at = DAY + timedelta(seconds=7200)

        self.assertIsNotNone(holder.fresh(now=expires_at - DEFAULT_ACCESS_TOKEN_SAFETY_MARGIN - timedelta(seconds=1)))
        self.assertIsNone(holder.fresh(now=expires_at - DEFAULT_ACCESS_TOKEN_SAFETY_MARGIN))
        self.assertIsNone(holder.fresh(now=expires_at + timedelta(seconds=1)))

    def test_the_default_safety_margin_is_a_real_margin(self) -> None:
        """余量必须真的存在，而且要用**绝对值**断言。

        上一条用例拿被测常量自己算边界，因此把余量调成 0 它照样绿——变异验红时实测到
        这一点（M9 首轮未变红）。一个"用被测值自证"的断言，测的是恒等式而不是行为。
        """

        self.assertGreaterEqual(DEFAULT_ACCESS_TOKEN_SAFETY_MARGIN, timedelta(minutes=1))

        holder = DerivedAccessTokenHolder()
        holder.store(derived(lifetime=7200), now=DAY)

        # 距过期只剩 30 秒：任何有意义的余量都该判它不新鲜，否则一轮跨很多页的
        # 花名册读取会在读到一半时撞上过期。
        self.assertIsNone(holder.fresh(now=DAY + timedelta(seconds=7200 - 30)))

    def test_a_token_without_a_lifetime_is_refused_instead_of_cached(self) -> None:
        """寿命未知就不缓存：否则它会在某个说不清的时刻开始让花名册读取失败，
        而报出来的原因会指向花名册而不是令牌供给。"""

        holder = DerivedAccessTokenHolder()

        self.assertFalse(holder.store(derived(lifetime=None), now=DAY))
        self.assertFalse(holder.has_token)
        self.assertIsNone(holder.fresh(now=DAY))

    def test_a_lifetime_less_response_does_not_drop_the_token_already_held(self) -> None:
        holder = DerivedAccessTokenHolder()
        holder.store(derived(lifetime=7200), now=DAY)

        holder.store(derived(lifetime=None, value="another-token"), now=DAY)

        token = holder.fresh(now=DAY + timedelta(minutes=1))
        self.assertIsNotNone(token)
        self.assertEqual(token.reveal(), FAKE_ACCESS_TOKEN)

    def test_the_holder_never_prints_the_token(self) -> None:
        """`V-身份-03` / `V-花名册-33` 的形状断言：持有者只回答有无，不回答值。"""

        holder = DerivedAccessTokenHolder()
        holder.store(derived(), now=DAY)

        self.assertNotIn(FAKE_ACCESS_TOKEN, repr(holder))
        self.assertNotIn(FAKE_ACCESS_TOKEN, str(holder))
        self.assertTrue(holder.has_token)
        self.assertNotIn(FAKE_ACCESS_TOKEN, str(holder.__dict__ if hasattr(holder, "__dict__") else {}))

    def test_a_bare_string_token_is_refused(self) -> None:
        """明文令牌不得以裸字符串流转：普通字符串会被 repr、日志与 dataclass 原样吐出。"""

        holder = DerivedAccessTokenHolder()

        with self.assertRaises(TypeError):
            holder.store(FAKE_ACCESS_TOKEN, now=DAY)  # type: ignore[arg-type]

    def test_a_naive_moment_is_refused(self) -> None:
        holder = DerivedAccessTokenHolder()

        with self.assertRaises(ValueError):
            holder.store(derived(), now=datetime(2026, 8, 18, 9, 0))
        with self.assertRaises(ValueError):
            holder.fresh(now=datetime(2026, 8, 18, 9, 0))

    def test_clearing_the_holder_leaves_nothing_behind(self) -> None:
        holder = DerivedAccessTokenHolder()
        holder.store(derived(), now=DAY)

        holder.clear()

        self.assertFalse(holder.has_token)
        self.assertIsNone(holder.fresh(now=DAY))


# --------------------------------------------------------------------------
# 二、频率上界：每 UTC 日至多一次，在"领取成功"那一刻记账
# --------------------------------------------------------------------------


class DailyRefreshBudgetTest(unittest.TestCase):
    """账本本身只是一个日期水位；**什么时候记账**由消费点决定（见 OnDemandRefreshTest）。"""

    def test_a_charged_day_is_spent_and_the_next_one_is_not(self) -> None:
        budget = DailyRefreshBudget()

        self.assertFalse(budget.is_spent(DAY.date()))
        budget.charge(DAY.date())

        self.assertTrue(budget.is_spent(DAY.date()))
        self.assertFalse(budget.is_spent((DAY + timedelta(days=1)).date()))
        self.assertEqual(budget.spent_on, DAY.date())

    def test_charging_the_same_day_twice_changes_nothing(self) -> None:
        budget = DailyRefreshBudget()
        budget.charge(DAY.date())
        budget.charge(DAY.date())

        self.assertTrue(budget.is_spent(DAY.date()))

    def test_only_a_normalized_date_is_accepted(self) -> None:
        """只收 ``date``。``datetime`` 是 ``date`` 的子类，误传会让"今天"变成一个带时刻的
        对象、比较时永不相等——上界会静默失效。归一到 UTC 是调用方的责任，因为它必须与
        凭据文件那一侧用同一把尺子。"""

        budget = DailyRefreshBudget()

        for wrong in (DAY, datetime(2026, 8, 18, 9, 0), "2026-08-18", None):
            with self.subTest(value=type(wrong).__name__):
                with self.assertRaises(ValueError):
                    budget.is_spent(wrong)  # type: ignore[arg-type]
                with self.assertRaises(ValueError):
                    budget.charge(wrong)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 三、失败分类：固定词表，正文里不可能出现令牌值
# --------------------------------------------------------------------------


class SupplyFailureReasonTest(unittest.TestCase):
    def test_a_reason_outside_the_vocabulary_is_refused(self) -> None:
        """否定断言：分类会进审计与异常正文，自由文本足以把令牌值带出去。"""

        for smuggled in (
            f"refresh_failed token={FAKE_ACCESS_TOKEN}",
            FAKE_ACCESS_TOKEN,
            "",
            "refresh_failed ",
        ):
            with self.subTest(reason=smuggled[:16]):
                with self.assertRaises(ValueError) as raised:
                    AccessTokenUnavailable(smuggled)
                self.assertNotIn(FAKE_ACCESS_TOKEN, str(raised.exception))

    def test_the_exception_text_is_exactly_the_classification(self) -> None:
        error = AccessTokenUnavailable("refresh_failed")

        self.assertEqual(str(error), "refresh_failed")
        self.assertEqual(error.reason, "refresh_failed")
        self.assertIn("refresh_failed", SUPPLY_FAILURE_REASONS)


# --------------------------------------------------------------------------
# 四、供给：新鲜就给、否则按需换一次
# --------------------------------------------------------------------------


class _CountingRefresh:
    """记账用的假"受控续期"：按脚本要么写入持有者、要么抛错。"""

    def __init__(self, holder: DerivedAccessTokenHolder, clock: MovableClock, *, outcomes=None) -> None:
        self._holder = holder
        self._clock = clock
        self._outcomes = list(outcomes or [])
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1
        outcome = self._outcomes.pop(0) if self._outcomes else derived()
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is not None:
            self._holder.store(outcome, now=self._clock())


def build_provider(*, outcomes=None, now: datetime = DAY):
    holder = DerivedAccessTokenHolder()
    clock = MovableClock(now)
    audit = RecordingAudit()
    refresh = _CountingRefresh(holder, clock, outcomes=outcomes)
    provider = RosterAccessTokenProvider(
        holder=holder, refresh=refresh, audit=audit, clock=clock
    )
    return provider, holder, clock, audit, refresh


class ProviderTest(unittest.TestCase):
    def test_a_cold_process_refreshes_on_the_first_daily_round(self) -> None:
        """冷启动为空：首次授权由另一个进程（reauthorize job）建立，它也丢弃了
        派生令牌，因此 scheduler 里的持有者一开始一定是空的。"""

        provider, holder, _clock, audit, refresh = build_provider()

        self.assertFalse(holder.has_token)
        self.assertEqual(provider(), FAKE_ACCESS_TOKEN)
        self.assertEqual(refresh.calls, 1)
        self.assertEqual(audit.actions(), ["roster_access_token.refreshed"])

    def test_a_fresh_token_is_handed_out_without_touching_the_credential(self) -> None:
        """一轮花名册要读很多页，每页现取一次；每页都去消费一次一次性凭据是灾难。"""

        provider, _holder, clock, _audit, refresh = build_provider()
        provider()

        clock.advance(timedelta(minutes=5))
        for _ in range(20):
            self.assertEqual(provider(), FAKE_ACCESS_TOKEN)

        self.assertEqual(refresh.calls, 1, "同一轮内只换一次")

    def test_an_expiring_token_triggers_the_next_refresh(self) -> None:
        """临期就去换：**频率上界不在这一层**。

        供给方只回答"手上这份还能不能用"，"今天还能不能再换一次"由真正消费凭据的
        那一侧回答（见 :class:`OnDemandRefreshTest`）。把上界放在这里，等于"打算试一次
        就扣一次"，可证明零消费的失败也会占掉名额（两路审查 O3）。
        """

        provider, _holder, clock, _audit, refresh = build_provider()
        provider()

        # 走到安全余量之内：还没真过期，但已经不该交出去。
        clock.advance(timedelta(seconds=7200) - DEFAULT_ACCESS_TOKEN_SAFETY_MARGIN)
        self.assertEqual(provider(), FAKE_ACCESS_TOKEN)

        self.assertEqual(refresh.calls, 2)

    def test_the_next_utc_day_refreshes_again(self) -> None:
        provider, _holder, clock, _audit, refresh = build_provider()
        provider()

        clock.advance(timedelta(days=1))
        self.assertEqual(provider(), FAKE_ACCESS_TOKEN)
        self.assertEqual(refresh.calls, 2)

    def test_a_refused_round_never_returns_a_placeholder(self) -> None:
        """失败关闭：绝不返回空串或占位符。空值会被花名册读取层折成
        `access_token_missing` 的**读取失败**，把排障指向源头而不是凭据。"""

        provider, _holder, _clock, _audit, _refresh = build_provider(
            outcomes=[AccessTokenUnavailable("no_credential_available")]
        )

        with self.assertRaises(AccessTokenUnavailable):
            provider()

    def test_an_unknown_exception_is_folded_into_a_single_classification(self) -> None:
        """注入的续期实现抛了本模块不认识的异常：只记分类，不记异常正文
        （正文可能带响应内容）。"""

        provider, _holder, _clock, audit, _refresh = build_provider(
            outcomes=[RuntimeError(f"boom {FAKE_ACCESS_TOKEN}")]
        )

        with self.assertRaises(AccessTokenUnavailable) as raised:
            provider()

        self.assertEqual(raised.exception.reason, "refresh_error")
        self.assertNotIn(FAKE_ACCESS_TOKEN, audit.rendered())
        self.assertNotIn(FAKE_ACCESS_TOKEN, str(raised.exception))

    def test_a_successful_refresh_with_an_unusable_token_is_reported_as_such(self) -> None:
        """续期与落盘都成功、只是派生令牌用不了（寿命未知）：凭据没有丢，
        因此**不撤销**；今天的预算已经用掉，明天再试。"""

        provider, holder, _clock, audit, _refresh = build_provider(
            outcomes=[derived(lifetime=None)]
        )

        with self.assertRaises(AccessTokenUnavailable) as raised:
            provider()

        self.assertEqual(raised.exception.reason, "derived_token_unusable")
        self.assertFalse(holder.has_token)
        self.assertEqual(audit.actions(), ["roster_access_token.unavailable"])

    def test_the_same_failure_is_audited_once_per_utc_day(self) -> None:
        """拒绝会在每一轮定时循环里重复发生（默认 60 秒一轮），逐次记会把审计淹掉。"""

        provider, _holder, clock, audit, _refresh = build_provider(
            outcomes=[AccessTokenUnavailable("no_credential_available")] * 6
        )

        for _ in range(5):
            with self.assertRaises(AccessTokenUnavailable):
                provider()
            clock.advance(timedelta(minutes=1))

        self.assertEqual(
            audit.actions(),
            ["roster_access_token.unavailable"],
            "同一天同一分类只记一条，不因每分钟一轮的重试而重复",
        )

        clock.advance(timedelta(days=1))
        with self.assertRaises(AccessTokenUnavailable):
            provider()
        self.assertEqual(len(audit.records), 2, "新的一天重新记一条")

    def test_two_different_failures_on_the_same_day_are_both_audited(self) -> None:
        """去重是按（日期，分类）而不是按日期：把两种不同的失败合并成一条，
        当天后发生的那个真实原因就再也不会出现在审计里。"""

        provider, _holder, _clock, audit, _refresh = build_provider(
            outcomes=[
                AccessTokenUnavailable("no_credential_available"),
                AccessTokenUnavailable("refresh_indeterminate"),
            ]
        )

        for _ in range(2):
            with self.assertRaises(AccessTokenUnavailable):
                provider()

        self.assertEqual(
            [fields["reason"] for _, fields in audit.records],
            ["no_credential_available", "refresh_indeterminate"],
        )

    def test_neither_the_audit_nor_the_exception_carries_the_token(self) -> None:
        """`V-花名册-33` / `V-身份-03`：审计只有分类与日期。"""

        provider, _holder, _clock, audit, _refresh = build_provider()
        provider()

        self.assertNotIn(FAKE_ACCESS_TOKEN, audit.rendered())
        for _action, fields in audit.records:
            self.assertNotIn("token", fields)
            for value in fields.values():
                self.assertNotIn(FAKE_ACCESS_TOKEN, str(value))

    def test_the_exception_carries_no_cause_chain(self) -> None:
        """O2：``__cause__`` 里挂着原始 transport 异常，任何一次 traceback 打印都会把
        响应正文（可能含令牌）带进日志。排障信息只走净化过的类名字段。"""

        class ResponseCarryingError(RuntimeError):
            pass

        provider, _holder, _clock, audit, _refresh = build_provider(
            outcomes=[ResponseCarryingError(f"body={{'access_token': '{FAKE_ACCESS_TOKEN}'}}")]
        )

        with self.assertRaises(AccessTokenUnavailable) as raised:
            provider()

        self.assertIsNone(raised.exception.__cause__, "不得保留原因链")
        self.assertTrue(raised.exception.__suppress_context__, "traceback 不得打印上下文")
        self.assertNotIn(FAKE_ACCESS_TOKEN, audit.rendered())
        self.assertEqual(audit.records[0][1]["error_type"], "ResponseCarryingError")


# --------------------------------------------------------------------------
# 五、按需入口：先成功落盘，再交出派生令牌
# --------------------------------------------------------------------------


class RecordingHolder(DerivedAccessTokenHolder):
    """把"交出令牌"这一步记进共享事件序列，用来钉住它与落盘的先后。"""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    def store(self, derived_token, *, now):  # type: ignore[override]
        self.events.append("store")
        return super().store(derived_token, now=now)


class ScriptedVault:
    """凭据库替身：记录领取参数与落盘次序，可注入落盘失败。"""

    def __init__(
        self,
        claims,
        *,
        save_failures: int = 0,
        superseded: bool = False,
        events: list[str] | None = None,
        consumed_days=(),
    ) -> None:
        self._claims = list(claims)
        self._save_failures = save_failures
        self._superseded = superseded
        self.events = events if events is not None else []
        self.consumed_days = list(consumed_days)
        self.saved: list[dict[str, object]] = []
        self.revoked: list[str] = []
        self.claim_calls: list[dict[str, object]] = []

    def claim_due(self, *, require_due: bool = True, refuse_if_consumed_on=None):
        # 替身也照做真实凭据库的成对守卫（O8）：签名与前置条件对不上要在这里就响。
        if not require_due and refuse_if_consumed_on is None:
            raise ValueError("放开到期判定必须同时给出频率上界（两个参数成对使用）")
        self.claim_calls.append(
            {"require_due": require_due, "refuse_if_consumed_on": refuse_if_consumed_on}
        )
        if refuse_if_consumed_on is not None and refuse_if_consumed_on in self.consumed_days:
            raise RefreshAlreadyConsumedToday("今天已经消费过一次续期凭据，本次领取被拒")
        return self._claims.pop(0) if self._claims else None

    def save(self, **fields):
        if self._save_failures > 0:
            self._save_failures -= 1
            raise RuntimeError("模拟写盘失败")
        if self._superseded:
            # 世代不符 / 主体登记 CAS 未通过：真实凭据库返回 False 且**什么都没写**。
            self.events.append("save_refused")
            return False
        self.events.append("save")
        self.saved.append(fields)
        consumed_at = fields.get("refresh_consumed_at")
        if consumed_at is not None:
            self.consumed_days.append(consumed_at.astimezone(timezone.utc).date())
        return True

    def revoke(self, *, reason: str, generation=None) -> bool:
        self.revoked.append(reason)
        return True

    def revoke_stale_consumed(self, **_kwargs) -> bool:
        return False


class ScriptedAuthorization:
    def __init__(self, outcome, *, lifetime: int | None = 7200, clock=None, takes=None) -> None:
        self._outcome = outcome
        self._lifetime = lifetime
        # 一次真实续期要走一个 HTTP 往返：注入耗时，好让"用哪一刻算"这件事可被观察。
        # 假时钟不前进的话，"落盘用领取前那一刻、缓存用续期后那一刻"这两个语义
        # 会退化成同一个值，相应的变异就永远杀不死（两路审查 O6）。
        self._clock = clock
        self._takes = takes
        self.calls = 0

    def refresh(self, current: AuthorizationGrant):
        self.calls += 1
        if self._clock is not None and self._takes is not None:
            self._clock.advance(self._takes)
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome, derived(lifetime=self._lifetime)


class _Claim:
    """``StoredCredential`` 的最小替身：按需入口只用到这四个字段。"""

    def __init__(self, *, generation: str = "gen-1") -> None:
        self.subject_open_id = "ou_delegated_authorization_subject"
        self.grant = AuthorizationGrant(SecretToken(FAKE_REFRESH_TOKEN), 604800, "offline_access")
        self.generation = generation


class SupplyLoopFixture(NamedTuple):
    loop: CredentialRotationLoop
    vault: ScriptedVault
    holder: RecordingHolder
    events: list[str]
    clock: MovableClock
    authorization: ScriptedAuthorization
    budget: DailyRefreshBudget


def build_supply_loop(
    *,
    claims=None,
    outcome=None,
    save_failures: int = 0,
    superseded: bool = False,
    lifetime: int | None = 7200,
    consumed_days=(),
    now: datetime = DAY,
    refresh_takes: timedelta | None = None,
) -> SupplyLoopFixture:
    events: list[str] = []
    holder = RecordingHolder(events)
    vault = ScriptedVault(
        [_Claim()] if claims is None else claims,
        save_failures=save_failures,
        superseded=superseded,
        events=events,
        consumed_days=consumed_days,
    )
    replacement = AuthorizationGrant(SecretToken(FAKE_NEXT_REFRESH_TOKEN), 604800, "offline_access")
    clock = MovableClock(now)
    authorization = ScriptedAuthorization(
        replacement if outcome is None else outcome,
        lifetime=lifetime,
        clock=clock,
        takes=refresh_takes,
    )
    budget = DailyRefreshBudget()
    loop = CredentialRotationLoop(
        vault=vault,
        authorization=authorization,
        interval_seconds=0.01,
        holder=holder,
        budget=budget,
        clock=clock,
    )
    return SupplyLoopFixture(loop, vault, holder, events, clock, authorization, budget)


class OnDemandRefreshTest(unittest.TestCase):
    def test_the_credential_is_persisted_before_the_derived_token_is_handed_over(self) -> None:
        """**次序是这条接线的核心安全属性**。

        ``refresh_token`` 一次性有效："换到了但没落盘"等于凭据丢失、需人工重新授权。
        先把派生令牌交出去，会让这条丢失路径在日报照常工作的表象下发生，直到下一次
        轮换才被发现。变异验红锚点：把 ``_remember_derived`` 挪到 ``_save_with_retry``
        之前，本用例必须变红。
        """

        loop, _vault, holder, events, _clock, _authorization, _budget = build_supply_loop()

        loop.refresh_for_supply()

        self.assertEqual(events, ["save", "store"])
        self.assertTrue(holder.has_token)

    def test_the_on_demand_claim_asks_for_a_credential_that_is_not_due_yet(self) -> None:
        """日报每天要令牌，而轮换点五天多才到一次；放开到期判定必须配一道频率上界，
        因此两个参数**成对**出现。"""

        loop, vault, _holder, _events, _clock, _authorization, _budget = build_supply_loop()

        loop.refresh_for_supply()

        self.assertEqual(len(vault.claim_calls), 1)
        self.assertIs(vault.claim_calls[0]["require_due"], False)
        self.assertEqual(vault.claim_calls[0]["refuse_if_consumed_on"], DAY.date())

    def test_the_persisted_credential_records_the_day_it_was_consumed(self) -> None:
        """重启也抹不掉的那道上界：判据随凭据落盘。"""

        loop, vault, _holder, _events, _clock, _authorization, _budget = build_supply_loop()

        loop.refresh_for_supply()

        self.assertEqual(vault.saved[0]["refresh_consumed_at"], DAY)
        self.assertEqual(vault.consumed_days, [DAY.date()])

    def test_a_second_refresh_on_the_same_day_is_refused_in_process(self) -> None:
        """进程内那道上界：连凭据文件都不必去开锁读，就该拒绝。"""

        loop, vault, _holder, _events, _clock, authorization, _budget = build_supply_loop(
            claims=[_Claim(), _Claim()]
        )
        loop.refresh_for_supply()
        claims_before = len(vault.claim_calls)

        with self.assertRaises(AccessTokenUnavailable) as raised:
            loop.refresh_for_supply()

        self.assertEqual(raised.exception.reason, "daily_refresh_budget_exhausted")
        self.assertEqual(authorization.calls, 1, "被上界拒绝时不得再向飞书发起续期")
        self.assertEqual(len(vault.claim_calls), claims_before, "被上界拒绝时不得再去领取")
        self.assertEqual(vault.revoked, [], "被上界拒绝不是凭据问题，不得撤销")

    def test_a_restart_on_the_same_day_is_refused_by_the_persisted_ceiling(self) -> None:
        """进程内预算随重启清零，拦不住的那一次由凭据文件里的判据挡下。

        这是两道上界里唯一能防住崩溃重启循环的一道——一次性令牌被高频消费正是
        2026-08-08 那次事故的形状。
        """

        loop, vault, _holder, _events, clock, authorization, _budget = build_supply_loop(
            claims=[_Claim(), _Claim()]
        )
        loop.refresh_for_supply()

        # 重启：新的持有者、新的预算，但**同一个凭据文件**。
        restarted = CredentialRotationLoop(
            vault=vault,
            authorization=authorization,
            interval_seconds=0.01,
            holder=DerivedAccessTokenHolder(),
            budget=DailyRefreshBudget(),
            clock=clock,
        )

        with self.assertRaises(AccessTokenUnavailable) as raised:
            restarted.refresh_for_supply()

        self.assertEqual(raised.exception.reason, "refresh_already_consumed_today")
        self.assertEqual(authorization.calls, 1, "被上界拒绝时不得再向飞书发起续期")
        self.assertEqual(vault.revoked, [], "被上界拒绝不是凭据问题，不得撤销")

    def test_a_persist_failure_hands_out_nothing_and_reports_the_credential_as_lost(self) -> None:
        """写盘失败＝凭据丢失风险：**不交出令牌**、撤销、按不可恢复响亮留痕。

        变异验红锚点：把"落盘失败仍然交出令牌"改回去，本用例必须变红。
        """

        loop, vault, holder, _events, _clock, _authorization, _budget = build_supply_loop(save_failures=99)

        with no_retry_backoff():
            with self.assertLogs("lingxi.apps.scheduler", level="ERROR") as captured:
                with self.assertRaises(AccessTokenUnavailable) as raised:
                    loop.refresh_for_supply()

        self.assertEqual(raised.exception.reason, "credential_persist_failed")
        self.assertFalse(holder.has_token, "落盘失败时一个令牌都不得交出")
        self.assertEqual(vault.revoked, ["rotation_persist_failed"])
        self.assertTrue(any("不可恢复" in line for line in captured.output))
        self.assertTrue(all(FAKE_ACCESS_TOKEN not in line for line in captured.output))
        self.assertTrue(all(FAKE_NEXT_REFRESH_TOKEN not in line for line in captured.output))

    def test_a_transient_persist_failure_still_ends_up_handing_out_the_token(self) -> None:
        """一次瞬时抖动不该报废一条一次性凭据：重试成功后次序依然是先落盘后交出。"""

        loop, vault, holder, events, _clock, _authorization, _budget = build_supply_loop(save_failures=1)

        loop.refresh_for_supply()

        self.assertEqual(events, ["save", "store"])
        self.assertTrue(holder.has_token)
        self.assertEqual(vault.revoked, [])

    def test_a_definite_refresh_failure_revokes_and_reports_the_classification(self) -> None:
        """`V-身份-04`：续期失败一律撤销，不重放一次性凭据。"""

        from lingxi.adapters.feishu_directory import FeishuDirectoryError

        loop, vault, holder, _events, _clock, _authorization, _budget = build_supply_loop(
            outcome=FeishuDirectoryError("feishu_code_20037")
        )

        with self.assertRaises(AccessTokenUnavailable) as raised:
            loop.refresh_for_supply()

        self.assertEqual(raised.exception.reason, "refresh_failed")
        self.assertEqual(vault.revoked, ["refresh_failed"])
        self.assertFalse(holder.has_token)

    def test_an_indeterminate_refresh_is_classified_apart_from_a_definite_failure(self) -> None:
        loop, vault, _holder, _events, _clock, _authorization, _budget = build_supply_loop(
            outcome=TimeoutError("模拟回程超时")
        )

        with self.assertRaises(AccessTokenUnavailable) as raised:
            loop.refresh_for_supply()

        self.assertEqual(raised.exception.reason, "refresh_indeterminate")
        self.assertEqual(vault.revoked, ["refresh_indeterminate"])

    def test_no_claimable_credential_is_not_disguised_as_anything_else(self) -> None:
        loop, vault, _holder, _events, _clock, authorization, _budget = build_supply_loop(claims=[None])

        with self.assertRaises(AccessTokenUnavailable) as raised:
            loop.refresh_for_supply()

        self.assertEqual(raised.exception.reason, "no_credential_available")
        self.assertEqual(authorization.calls, 0)
        self.assertEqual(vault.revoked, [])

    def test_a_stopping_process_never_starts_a_refresh(self) -> None:
        """停止之后不再开启任何一次续期：半途中断的续期等于凭据丢失
        （模块头注释、`V-部署-03`）。"""

        loop, vault, _holder, _events, _clock, authorization, _budget = build_supply_loop()
        loop.request_stop()

        with self.assertRaises(AccessTokenUnavailable) as raised:
            loop.refresh_for_supply()

        self.assertEqual(raised.exception.reason, "scheduler_stopping")
        self.assertEqual(vault.claim_calls, [], "停止中一条都不领")
        self.assertEqual(authorization.calls, 0)

    def test_a_refresh_without_a_token_lifetime_keeps_the_credential(self) -> None:
        """飞书没给短期令牌寿命：新凭据已经落盘，**不撤销**；只是今天没有令牌可用。"""

        loop, vault, holder, _events, _clock, _authorization, _budget = build_supply_loop(lifetime=None)

        with self.assertRaises(AccessTokenUnavailable) as raised:
            loop.refresh_for_supply()

        self.assertEqual(raised.exception.reason, "derived_token_unusable")
        self.assertEqual(len(vault.saved), 1, "凭据照常落盘")
        self.assertEqual(vault.revoked, [], "凭据没有丢，不得撤销")
        self.assertFalse(holder.has_token)

    # ---- O1：save 返回 False（世代不符 / 主体 CAS 未过）------------------

    def test_a_superseded_save_hands_out_nothing(self) -> None:
        """**两路审查交叉确认的 P1**：``save`` 返回 ``False`` 是"没落盘"，不是"落盘了"。

        期间发生新授权时，本链换到的新 ``refresh_token`` 被丢弃，而旧的那条已经在飞书
        那边作废——这条链已经死了。此时把派生令牌交出去，日报会照常工作到令牌过期
        （约两小时），把凭据丢失整整盖住那么久。

        变异验红锚点：把 ``SUPERSEDED`` 重新折成 ``SAVED``，本用例必须变红。
        """

        loop, vault, holder, events, _clock, _authorization, _budget = build_supply_loop(
            superseded=True
        )

        with self.assertLogs("lingxi.apps.scheduler", level="WARNING") as captured:
            with self.assertRaises(AccessTokenUnavailable) as raised:
                loop.refresh_for_supply()

        self.assertEqual(raised.exception.reason, "no_credential_available")
        self.assertFalse(holder.has_token, "没落盘就一个令牌都不交出")
        self.assertNotIn("store", events)
        self.assertEqual(vault.saved, [], "被拒的保存不产生落盘记录")
        self.assertEqual(vault.revoked, [], "期间的新授权不得被旧链连带撤销")
        self.assertTrue(any("新授权取代" in line for line in captured.output))

    # ---- O3：记账点在"领取成功"之后 -------------------------------------

    def test_a_failure_before_the_claim_does_not_spend_the_daily_budget(self) -> None:
        """可证明**零消费**的失败不得占掉当日名额。

        这不是省一次请求的问题：产品负责人早上补完授权，当天全天都会被"今天已经换过
        了"拒绝，而凭据层恰恰刻意让新授权不带消费标记（两路审查 O3）。
        """

        loop, vault, _holder, _events, _clock, authorization, budget = build_supply_loop(
            claims=[None, _Claim()]
        )

        with self.assertRaises(AccessTokenUnavailable) as raised:
            loop.refresh_for_supply()

        self.assertEqual(raised.exception.reason, "no_credential_available")
        self.assertIsNone(budget.spent_on, "没领到凭据就没消费，不得记账")

        # 同一天补上授权：同一个进程立刻恢复供给，不必等到明天。
        loop.refresh_for_supply()

        self.assertEqual(authorization.calls, 1)
        self.assertEqual(budget.spent_on, DAY.date())

    def test_a_stop_does_not_spend_the_daily_budget(self) -> None:
        loop, _vault, _holder, _events, _clock, _authorization, budget = build_supply_loop()
        loop.request_stop()

        with self.assertRaises(AccessTokenUnavailable):
            loop.refresh_for_supply()

        self.assertIsNone(budget.spent_on)

    def test_any_failure_after_the_claim_spends_the_daily_budget(self) -> None:
        """领取成功＝消费标记已经原子置位，这条一次性令牌从此回不来了。

        之后无论飞书拒绝、结果不明确还是写盘失败，当天都不得再换一次——否则一个
        每轮都失败的缺陷会每 60 秒烧一次。
        """

        cases = (
            ({"outcome": FeishuDirectoryErrorForTests("feishu_code_20037")}, "refresh_failed"),
            ({"outcome": TimeoutError("模拟回程超时")}, "refresh_indeterminate"),
            ({"save_failures": 99}, "credential_persist_failed"),
            ({"superseded": True}, "no_credential_available"),
            ({"lifetime": None}, "derived_token_unusable"),
        )
        for kwargs, expected in cases:
            with self.subTest(reason=expected):
                loop, _vault, _holder, _events, _clock, _authorization, budget = build_supply_loop(
                    **kwargs
                )

                with no_retry_backoff():
                    with self.assertRaises(AccessTokenUnavailable) as raised:
                        loop.refresh_for_supply()

                self.assertEqual(raised.exception.reason, expected)
                self.assertEqual(budget.spent_on, DAY.date(), "触达过飞书就必须记账")

    # ---- O2：异常不带原因链 ---------------------------------------------

    def test_the_refresh_failure_carries_no_cause_chain(self) -> None:
        """原始 transport 异常留在 ``__cause__`` 里，任何 traceback 打印都会把响应正文
        （可能含令牌）带进日志。"""

        loop, _vault, _holder, _events, _clock, _authorization, _budget = build_supply_loop(
            outcome=FeishuDirectoryErrorForTests(f"body-with-{FAKE_ACCESS_TOKEN}")
        )

        with self.assertRaises(AccessTokenUnavailable) as raised:
            loop.refresh_for_supply()

        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)
        self.assertNotIn(FAKE_ACCESS_TOKEN, str(raised.exception))

    # ---- O5 / O6：两把尺子必须一致，两个时刻必须分清 ----------------------

    def test_the_persisted_ceiling_is_asked_in_utc_days(self) -> None:
        """上界按 **UTC 日**问，与凭据文件那一侧用同一把尺子。

        用一个非 UTC 时钟表达同一时刻：判定必须不变，否则部署机器的时区会让午夜前后
        的那一段悄悄多消费一次一次性凭据。
        """

        eastern = timezone(timedelta(hours=8))
        # UTC 是 8-18 23:30，本地时钟显示 8-19 07:30。
        local_midnight_window = datetime(2026, 8, 19, 7, 30, tzinfo=eastern)
        loop, vault, _holder, _events, _clock, _authorization, budget = build_supply_loop(
            now=local_midnight_window
        )

        loop.refresh_for_supply()

        self.assertEqual(vault.claim_calls[0]["refuse_if_consumed_on"], date(2026, 8, 18))
        self.assertEqual(budget.spent_on, date(2026, 8, 18))

    def test_the_two_moments_of_one_refresh_are_kept_apart(self) -> None:
        """一次续期跨越一个 HTTP 往返，因此有两个时刻，各有各的用途：

        - **领取前**那一刻落盘成"今天消费过一次"——它要和领取那一刻对齐，晚了会在
          午夜前后落到第二天，让上界白白多给一次名额；
        - **续期后**那一刻用来算派生令牌还能活多久——用早的那一刻会把往返耗时算进
          寿命里，令牌因此被当成比实际更新鲜。
        """

        loop, vault, holder, _events, clock, _authorization, _budget = build_supply_loop(
            refresh_takes=timedelta(minutes=30)
        )

        loop.refresh_for_supply()

        self.assertEqual(vault.saved[0]["refresh_consumed_at"], DAY, "落盘用领取前那一刻")
        # 令牌寿命 7200 秒，从续期结束（DAY+30min）起算：DAY+2h 时仍在余量之外。
        self.assertIsNotNone(holder.fresh(now=DAY + timedelta(hours=2)))
        self.assertEqual(clock.now, DAY + timedelta(minutes=30))

    # ---- O7：真实原因不被例行拒绝盖住 -------------------------------------

    def test_an_unusable_token_from_the_due_rotation_is_still_the_reported_reason(self) -> None:
        """到期轮换换到一份不可用的派生令牌（飞书没给寿命）之后，当天后续每一轮
        供给都必须继续报**真实原因**，而不是"今天已经换过了"。

        否则一个真实故障会被一个例行拒绝盖住：运维看到的是"额度用完了"，而实际是
        飞书的响应少了一个字段（两路审查 O7）。
        """

        loop, vault, holder, _events, _clock, _authorization, _budget = build_supply_loop(
            claims=[_Claim(), _Claim()], lifetime=None
        )

        loop.run_once()

        self.assertFalse(holder.has_token)
        self.assertEqual(len(vault.saved), 1, "凭据照常落盘，消费标记也已经写下")

        with self.assertRaises(AccessTokenUnavailable) as raised:
            loop.refresh_for_supply()

        self.assertEqual(raised.exception.reason, "derived_token_unusable")

    def test_a_usable_token_clears_the_unusable_marker(self) -> None:
        """拿到可用令牌之后，那个记号必须**当天就**清掉。

        记号只在"今天"这一天有意义。不清掉的话，同一天里后来那次成功会被前面那次
        失败继续污染：运维看到的是"派生令牌不可用"，而实际上令牌好好地在持有者里。
        故意让两次都落在同一个 UTC 日——跨日的写法测不出这条，因为跨日之后记号本来
        就不匹配了（变异验红实测：N12 首轮存活）。
        """

        fixture = build_supply_loop(claims=[_Claim(), _Claim()], lifetime=None)
        loop, vault = fixture.loop, fixture.vault

        loop.run_once()  # 第一次：飞书没给寿命，记号置上
        self.assertEqual(loop._derived_unusable_on, DAY.date())  # noqa: SLF001

        fixture.authorization._lifetime = 7200  # noqa: SLF001 - 把假响应换成带寿命的
        loop.run_once()  # 同一天第二次：换到可用令牌，记号必须清掉

        self.assertTrue(fixture.holder.has_token)
        # 当天再问供给：文件侧的上界会拒绝，原因应当就是上界本身。
        with self.assertRaises(AccessTokenUnavailable) as raised:
            loop.refresh_for_supply()

        self.assertEqual(raised.exception.reason, "refresh_already_consumed_today")
        self.assertEqual(len(vault.saved), 2)


class FeishuDirectoryErrorForTests(Exception):
    """带 ``definite`` 属性的假明确失败：分类走 adapters 层给出的这个属性。"""

    definite = True


class ScheduledRotationFeedsTheHolderTest(unittest.TestCase):
    def test_a_due_rotation_also_hands_the_derived_token_to_the_holder(self) -> None:
        """到期轮换那一条路径同样不再丢弃派生令牌（此前 ``:328`` 当场丢弃）。"""

        loop, vault, holder, events, _clock, _authorization, _budget = build_supply_loop()

        report = loop.run_once()

        self.assertEqual((report.claimed, report.rotated, report.revoked), (1, 1, 0))
        self.assertEqual(events, ["save", "store"])
        self.assertTrue(holder.has_token)
        self.assertEqual(vault.saved[0]["refresh_consumed_at"], DAY)

    def test_a_due_rotation_that_cannot_persist_hands_out_nothing(self) -> None:
        loop, vault, holder, _events, _clock, _authorization, _budget = build_supply_loop(save_failures=99)

        with no_retry_backoff(), self.assertLogs("lingxi.apps.scheduler", level="ERROR"):
            report = loop.run_once()

        self.assertEqual((report.claimed, report.rotated, report.revoked), (1, 0, 1))
        self.assertFalse(holder.has_token)
        self.assertEqual(vault.revoked, ["rotation_persist_failed"])

    def test_a_due_rotation_whose_result_is_superseded_hands_out_nothing(self) -> None:
        """到期轮换这一侧同样要区分"落盘了"与"被新授权取代"（两路审查 P1 的另一半）。

        变异验红实测：只钉住按需入口那一侧时，把到期轮换的 ``SUPERSEDED`` 也当成
        ``SAVED`` 的变异会存活（N3 首轮）。
        """

        loop, vault, holder, events, _clock, _authorization, _budget = build_supply_loop(
            superseded=True
        )

        with self.assertLogs("lingxi.apps.scheduler", level="WARNING") as captured:
            report = loop.run_once()

        # 收尾判定不变：不撤销（新凭据不能被旧链连带删掉）。
        self.assertEqual((report.claimed, report.rotated, report.revoked), (1, 1, 0))
        self.assertEqual(vault.revoked, [])
        # 但派生令牌一个都不交出：这条链已经没有活着的凭据了。
        self.assertFalse(holder.has_token)
        self.assertNotIn("store", events)
        self.assertTrue(any("新授权取代" in line for line in captured.output))


# --------------------------------------------------------------------------
# 六、否定断言：一次性 refresh_token 的消费点在正式代码里仍然唯一
# --------------------------------------------------------------------------


class ConsumptionSiteScanner(ast.NodeVisitor):
    """扫出一份源码里所有"消费一次性 ``refresh_token``"的痕迹。

    三种痕迹分别对应三个必须唯一的位置：

    - ``grant_type: refresh_token`` 的请求体——真正向飞书兑换的地方；
    - ``.refresh(...)`` 调用——授权客户端那一层的调用点；
    - ``.claim_due(...)`` 调用——原子置位消费标记、领取凭据去续期的地方。
    """

    def __init__(self) -> None:
        self.grant_type_sites: list[int] = []
        self.refresh_call_sites: list[int] = []
        self.claim_sites: list[int] = []

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and key.value == "grant_type"
                and isinstance(value, ast.Constant)
                and value.value == "refresh_token"
            ):
                self.grant_type_sites.append(node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute):
            if node.func.attr == "refresh":
                self.refresh_call_sites.append(node.lineno)
            elif node.func.attr == "claim_due":
                self.claim_sites.append(node.lineno)
        self.generic_visit(node)


def scan_consumption_sites(source: str) -> ConsumptionSiteScanner:
    scanner = ConsumptionSiteScanner()
    scanner.visit(ast.parse(source))
    return scanner


#: 正式路径上允许出现消费痕迹的文件，逐个写明理由。
FORMAL_GRANT_TYPE_FILES = {
    # 唯一一处真的向飞书兑换一次性 refresh_token 的请求体。
    "adapters/feishu_directory.py",
}
FORMAL_REFRESH_CALL_FILES = {
    # 唯一一处调用授权客户端换新凭据的地方：凭据轮换职责的两个入口
    # （到期轮换 run_once、按需续期 refresh_for_supply）都在这个文件里。
    "apps/scheduler/__init__.py",
}
FORMAL_CLAIM_FILES = {
    # 唯一一处领取凭据（原子置位消费标记）的地方。
    "apps/scheduler/__init__.py",
}
#: Bot-Test 受控验证资产：它们各有一套自己的续期实现，属于「正式开发的参考实现」，
#: 不属于正式用户入口（代码框架第五节）。豁免不是白名单一写了事——下面两条自证
#: 要求它们**仍然自己声明**是测试资产，且**没有被任何 apps/ 进程 import**。
BOT_TEST_ASSETS = {"adapters/refresh_tokens.py"}
BOT_TEST_DECLARATION = "不属于正式 Lingxi 用户入口"


class RefreshTokenHasExactlyOneConsumerTest(unittest.TestCase):
    """一次性 ``refresh_token`` 在正式代码里只有一个消费者。

    这条断言是方案 C 的边界本身：日报接线之后，"谁可以消费 ``refresh_token``"是
    唯一没得商量的东西。两个消费者共用一条一次性令牌，正是 2026-08-08 授权码被烧
    那次事故的形状（AGENTS.md 工作底线）。

    源码扫描而不是行为用例，是因为这条要证明的是一条路径**不存在**——没有调用点
    可以断言，只能反向枚举。
    """

    def _sources(self):
        for path in sorted(SOURCE_ROOT.rglob("*.py")):
            yield path.relative_to(SOURCE_ROOT).as_posix(), path.read_text(encoding="utf-8")

    def test_the_scanner_actually_finds_the_known_sites(self) -> None:
        """防空扫：扫描器必须真的抓到已知的那几处，否则它随时可能变成一条永远绿的空断言。"""

        directory = (SOURCE_ROOT / "adapters" / "feishu_directory.py").read_text(encoding="utf-8")
        scheduler = (SOURCE_ROOT / "apps" / "scheduler" / "__init__.py").read_text(encoding="utf-8")

        self.assertTrue(scan_consumption_sites(directory).grant_type_sites)
        scheduler_sites = scan_consumption_sites(scheduler)
        self.assertGreaterEqual(len(scheduler_sites.refresh_call_sites), 2, "到期轮换与按需续期各一处")
        self.assertGreaterEqual(len(scheduler_sites.claim_sites), 2)

    def test_the_scanner_flags_a_synthetic_second_consumer(self) -> None:
        """分类器自证：三种痕迹各喂一段合成源码，必须都被抓到。"""

        cases = (
            ('body = {"grant_type": "refresh_token", "refresh_token": token}', "grant_type_sites"),
            ("grant, derived = self._authorization.refresh(current)", "refresh_call_sites"),
            ("claim = self._vault.claim_due(require_due=False)", "claim_sites"),
        )
        for source, attribute in cases:
            with self.subTest(attribute=attribute):
                self.assertTrue(getattr(scan_consumption_sites(source), attribute))

        # 反向：不含任何消费痕迹的源码不得被误报。
        clean = scan_consumption_sites('body = {"grant_type": "authorization_code"}\nx.refresh_at()')
        self.assertEqual(clean.grant_type_sites, [])
        self.assertEqual(clean.refresh_call_sites, [])
        self.assertEqual(clean.claim_sites, [])

    def test_no_module_outside_the_allowlist_consumes_a_refresh_token(self) -> None:
        grant_type_files: set[str] = set()
        refresh_call_files: set[str] = set()
        claim_files: set[str] = set()

        for relative, source in self._sources():
            scanner = scan_consumption_sites(source)
            if scanner.grant_type_sites:
                grant_type_files.add(relative)
            if scanner.refresh_call_sites:
                refresh_call_files.add(relative)
            if scanner.claim_sites:
                claim_files.add(relative)

        self.assertEqual(grant_type_files - BOT_TEST_ASSETS, FORMAL_GRANT_TYPE_FILES)
        self.assertEqual(refresh_call_files - BOT_TEST_ASSETS, FORMAL_REFRESH_CALL_FILES)
        self.assertEqual(claim_files - BOT_TEST_ASSETS, FORMAL_CLAIM_FILES)

    def test_every_exempted_asset_still_declares_itself_a_test_asset(self) -> None:
        """豁免的自证之一：资产头部声明还在。声明一旦被删掉（有人把它当正式代码用），
        这里立刻变红。"""

        for relative in BOT_TEST_ASSETS:
            with self.subTest(module=relative):
                source = (SOURCE_ROOT / relative).read_text(encoding="utf-8")
                docstring = ast.get_docstring(ast.parse(source)) or ""
                self.assertIn(BOT_TEST_DECLARATION, docstring)

    def test_no_process_entry_point_imports_an_exempted_asset(self) -> None:
        """豁免的自证之二：没有任何 ``apps/`` 进程 import 这些资产。

        只看模块级 import 是不够的——本仓库的依赖大多写在函数体里（代码框架第六节），
        因此这里扫的是整棵语法树上的全部 ``import`` 语句。
        """

        exempted_modules = {
            "lingxi." + relative.removesuffix(".py").replace("/", ".")
            for relative in BOT_TEST_ASSETS
        }
        offenders: list[str] = []
        for path in sorted((SOURCE_ROOT / "apps").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                imported: set[str] = set()
                if isinstance(node, ast.Import):
                    imported = {alias.name for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported = {node.module} | {
                        f"{node.module}.{alias.name}" for alias in node.names
                    }
                for module in imported & exempted_modules:
                    offenders.append(f"{path.relative_to(SOURCE_ROOT).as_posix()}:{node.lineno} {module}")

        self.assertEqual(offenders, [])

    def test_the_import_scan_would_catch_a_function_level_import(self) -> None:
        """防空扫：函数体内的 import 也必须被看见，否则上一条断言形同虚设。"""

        tree = ast.parse("def build():\n    from lingxi.adapters.refresh_tokens import Vault\n")
        found = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        ]

        self.assertEqual(found, ["lingxi.adapters.refresh_tokens"])


# --------------------------------------------------------------------------
# 七、失败语义：拿不到令牌不伪装成花名册读取失败，也不注销职责
# --------------------------------------------------------------------------


class SupplyFailureSemanticsTest(unittest.TestCase):
    """供给失败之后日报侧会发生什么。

    ``BitableRosterPages._token`` 刻意**不**把 provider 自己抛的异常折成
    :class:`RosterReadError`：拿不到凭据是本侧的授权问题，不是"花名册源头异常"，
    两者混在一起会让日报与告警指向错误的地方（去查花名册，而问题在凭据）。
    因此失败关闭发生在职责级：不注销、水位不置位、下一轮再试。
    """

    def _pages(self, provider):
        from lingxi.adapters.feishu_roster_bitable import BitableRosterPages

        def transport(*_args, **_kwargs):  # pragma: no cover - 令牌取不到就不该走到这里
            raise AssertionError("拿不到令牌时不得发起任何请求")

        return BitableRosterPages(
            base_url="https://open.feishu.cn/open-apis",
            app_token="bascnFakeAppToken",
            table_id="tblFakeTable",
            access_token=provider,
            transport=transport,
        )

    def test_a_supply_failure_is_not_folded_into_a_roster_read_failure(self) -> None:
        from lingxi.adapters.feishu_roster_bitable import read_roster_snapshot

        def failing() -> str:
            raise AccessTokenUnavailable("no_credential_available")

        with self.assertRaises(AccessTokenUnavailable) as raised:
            read_roster_snapshot(self._pages(failing))

        self.assertEqual(raised.exception.reason, "no_credential_available")

    def test_a_failing_duty_neither_stops_the_others_nor_stays_down(self) -> None:
        """`V-保留-15` 的同一条结构保证在这里的落地：职责级失败隔离 + 下一轮重试。"""

        from lingxi.apps.scheduler import SchedulerLoop

        class FailingRosterDuty:
            name = "花名册审计日报"

            def __init__(self) -> None:
                self.rounds = 0

            def run_once(self):
                self.rounds += 1
                raise AccessTokenUnavailable("credential_persist_failed")

        class OtherDuty:
            name = "保留清理"

            def __init__(self) -> None:
                self.rounds = 0

            def run_once(self):
                self.rounds += 1
                return "ok"

        roster, other = FailingRosterDuty(), OtherDuty()
        loop = SchedulerLoop(duties=(other, roster), interval_seconds=0.01)

        with self.assertLogs("lingxi.apps.scheduler", level="ERROR") as captured:
            loop.run_once()
            loop.run_once()

        self.assertEqual(roster.rounds, 2, "失败的职责下一轮照样再试，不被注销")
        self.assertEqual(other.rounds, 2, "一个职责失败不得带走另一个")
        self.assertTrue(
            all("AccessTokenUnavailable" in line for line in captured.output),
            captured.output,
        )
        self.assertTrue(
            all(FAKE_ACCESS_TOKEN not in line for line in captured.output),
            "职责级失败日志里不得出现令牌值",
        )


# --------------------------------------------------------------------------
# 八、装配：日报职责真的拿到这条供给
# --------------------------------------------------------------------------


class AssembledSupplyTest(unittest.TestCase):
    """`V-花名册-29` 的完成标准：配置齐全时职责真实注册，且供给就是这条按需链。"""

    def _config(self, **extra: str):
        import tempfile

        from cryptography.fernet import Fernet

        from lingxi.apps.scheduler import SchedulerConfig

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return SchedulerConfig.from_env(
            {
                "LINGXI_POSTGRES_DSN": "postgresql://user@localhost:5432/lingxi",
                "LINGXI_DELEGATED_CREDENTIAL_KEY": Fernet.generate_key().decode(),
                "LINGXI_DELEGATED_CREDENTIAL_PATH": str(
                    pathlib.Path(directory.name) / "delegated.enc"
                ),
                "LINGXI_FEISHU_APP_ID": "cli_fake",
                "LINGXI_FEISHU_APP_SECRET": "secret_fake",
                "LINGXI_ADMIN_GROUP_CHAT_ID": "oc_fake_admin_group_chat_id",
                "LINGXI_ROSTER_BITABLE_APP_TOKEN": "bascnFakeAppToken",
                "LINGXI_ROSTER_BITABLE_TABLE_ID": "tblFakeTable",
                **extra,
            }
        )

    def test_the_daily_report_duty_is_registered_and_shares_the_rotation_duty(self) -> None:
        from lingxi.apps.scheduler import build_loop

        audit = RecordingAudit()
        loop = build_loop(self._config(), audit=audit)

        names = [duty.name for duty in loop.duties]
        self.assertEqual(names, ["凭据轮换", "保留清理", "空闲会话清理", "花名册审计日报"])
        self.assertEqual(audit.records, [], "前置齐备时不该有『未注册』审计")

    def test_assembly_never_touches_the_credential(self) -> None:
        """装配阶段不发任何请求、不读凭据文件（`V-花名册-27` 的同一条口径）。

        凭据文件路径指向一个空目录：如果装配去读它、或者去换一次令牌，这里会失败。
        """

        from lingxi.apps.scheduler import build_loop

        config = self._config()
        build_loop(config, audit=RecordingAudit())

        self.assertFalse(pathlib.Path(config.credential_path).exists())

    # ---- O4：装配链的**行为**面 -------------------------------------------

    def _assemble(self, **extra: str):
        """装配一次，并把 `build_loop` 交给日报职责的那个令牌供给抓出来。

        只断言"职责注册了"是不够的：三条接线（建持有者 → 轮换写 → 日报读）全部断掉
        都不会让那种断言变红（两路审查 O4 实测三个变异全部存活）。这里把供给本身取出来
        当作被测对象。
        """

        from lingxi.apps.scheduler import _build_roster_audit_duty, build_loop

        captured: dict[str, object] = {}
        real = _build_roster_audit_duty

        def spy(config, **kwargs):
            captured["supply"] = kwargs["roster_access_token"]
            return real(config, **kwargs)

        with mock.patch("lingxi.apps.scheduler._build_roster_audit_duty", spy):
            loop = build_loop(self._config(**extra), audit=RecordingAudit())
        return loop, captured["supply"]

    def test_the_token_the_rotation_duty_writes_is_the_one_the_daily_report_reads(self) -> None:
        """接线的**行为**证据：轮换职责写进持有者的那一份，日报侧立刻就能取到。

        杀掉三个变异：`build_loop` 不建持有者、建了却没交给轮换职责、日报侧读的是
        另一个持有者。
        """

        loop, supply = self._assemble()
        rotation = loop.duties[0]

        holder = rotation.derived_token_holder
        self.assertIsNotNone(holder, "装配必须给轮换职责一个持有者")
        holder.store(derived(value="assembled-token"), now=datetime.now(timezone.utc))

        self.assertEqual(supply(), "assembled-token")

    def test_a_cold_supply_reaches_the_rotation_duty_and_fails_closed(self) -> None:
        """持有者为空时，供给必须真的落到**凭据轮换职责**上。

        这里没有凭据文件，因此那条链会一路走到"没有可领取的凭据"并失败关闭——
        分类正是轮换职责给出的那个。把 `refresh` 接成别的东西（或不接）都会让它变红。
        """

        loop, supply = self._assemble()
        rotation = loop.duties[0]
        self.assertFalse(rotation.derived_token_holder.has_token)

        with self.assertRaises(AccessTokenUnavailable) as raised:
            supply()

        self.assertEqual(raised.exception.reason, "no_credential_available")

    def test_the_assembled_supply_is_bounded_by_the_daily_ceiling(self) -> None:
        """装配出来的那条链带着频率上界：轮换职责拿到了账本，不是一个没有约束的循环。"""

        loop, supply = self._assemble()
        rotation = loop.duties[0]
        today = datetime.now(timezone.utc).date()

        # 直接把账本记成"今天已经用过"，等价于当天已经换过一次。
        rotation._budget.charge(today)  # noqa: SLF001 - 装配面没有别的观察点

        with self.assertRaises(AccessTokenUnavailable) as raised:
            supply()

        self.assertEqual(raised.exception.reason, "daily_refresh_budget_exhausted")


if __name__ == "__main__":
    unittest.main()
