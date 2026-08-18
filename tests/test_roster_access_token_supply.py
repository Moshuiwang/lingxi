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
from datetime import datetime, timedelta, timezone
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
# 二、频率上界：每 UTC 日至多一次，且按"尝试"计
# --------------------------------------------------------------------------


class DailyRefreshBudgetTest(unittest.TestCase):
    def test_a_second_attempt_on_the_same_utc_day_is_refused(self) -> None:
        budget = DailyRefreshBudget()

        self.assertTrue(budget.try_consume(DAY))
        self.assertFalse(budget.try_consume(DAY + timedelta(hours=10)))
        self.assertEqual(budget.spent_on, DAY.date())

    def test_the_next_utc_day_gets_its_own_attempt(self) -> None:
        budget = DailyRefreshBudget()
        budget.try_consume(DAY)

        self.assertTrue(budget.try_consume(DAY + timedelta(days=1)))

    def test_the_day_boundary_is_utc_not_local(self) -> None:
        """日界按 UTC，与日报的日界一致。用一个非 UTC 时区表达同一时刻，
        判定必须不变——否则部署机器的时区会悄悄改变消费频率。"""

        budget = DailyRefreshBudget()
        budget.try_consume(datetime(2026, 8, 18, 23, 30, tzinfo=timezone.utc))

        same_moment_elsewhere = datetime(
            2026, 8, 19, 7, 30, tzinfo=timezone(timedelta(hours=8))
        )
        self.assertFalse(budget.try_consume(same_moment_elsewhere))

    def test_a_naive_moment_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            DailyRefreshBudget().try_consume(datetime(2026, 8, 18, 9, 0))


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
        provider, _holder, clock, _audit, refresh = build_provider()
        provider()

        # 走到安全余量之内：还没真过期，但已经不该交出去。
        clock.advance(timedelta(seconds=7200) - DEFAULT_ACCESS_TOKEN_SAFETY_MARGIN)
        with self.assertRaises(AccessTokenUnavailable) as raised:
            provider()

        # 同一 UTC 日，上界拦住了第二次消费——这正是"每日一次"该有的样子。
        self.assertEqual(raised.exception.reason, "daily_refresh_budget_exhausted")
        self.assertEqual(refresh.calls, 1)

    def test_the_next_utc_day_refreshes_again(self) -> None:
        provider, _holder, clock, _audit, refresh = build_provider()
        provider()

        clock.advance(timedelta(days=1))
        self.assertEqual(provider(), FAKE_ACCESS_TOKEN)
        self.assertEqual(refresh.calls, 2)

    def test_the_daily_ceiling_counts_attempts_not_successes(self) -> None:
        """按"尝试"计而不是按"成功"计：一次失败的续期在飞书那边同样可能已经消费掉
        这条一次性令牌，按"成功才记账"会让一个每轮都失败的缺陷每 60 秒烧一次。"""

        provider, _holder, _clock, audit, refresh = build_provider(
            outcomes=[AccessTokenUnavailable("refresh_indeterminate")]
        )

        with self.assertRaises(AccessTokenUnavailable) as first:
            provider()
        with self.assertRaises(AccessTokenUnavailable) as second:
            provider()

        self.assertEqual(first.exception.reason, "refresh_indeterminate")
        self.assertEqual(second.exception.reason, "daily_refresh_budget_exhausted")
        self.assertEqual(refresh.calls, 1, "失败之后当天不得再去消费凭据")
        self.assertEqual(
            audit.actions(),
            ["roster_access_token.unavailable", "roster_access_token.unavailable"],
        )

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
            outcomes=[
                AccessTokenUnavailable("no_credential_available"),
                AccessTokenUnavailable("no_credential_available"),
            ]
        )

        for _ in range(5):
            with self.assertRaises(AccessTokenUnavailable):
                provider()
            clock.advance(timedelta(minutes=1))

        self.assertEqual(
            audit.actions(),
            ["roster_access_token.unavailable", "roster_access_token.unavailable"],
            "同一天里：一条续期失败 + 一条上界拒绝，不因重试而重复",
        )
        reasons = [fields["reason"] for _, fields in audit.records]
        self.assertEqual(reasons, ["no_credential_available", "daily_refresh_budget_exhausted"])

        clock.advance(timedelta(days=1))
        with self.assertRaises(AccessTokenUnavailable):
            provider()
        self.assertEqual(len(audit.records), 3, "新的一天重新记一条")

    def test_neither_the_audit_nor_the_exception_carries_the_token(self) -> None:
        """`V-花名册-33` / `V-身份-03`：审计只有分类与日期。"""

        provider, _holder, _clock, audit, _refresh = build_provider()
        provider()

        self.assertNotIn(FAKE_ACCESS_TOKEN, audit.rendered())
        for _action, fields in audit.records:
            self.assertNotIn("token", fields)
            for value in fields.values():
                self.assertNotIn(FAKE_ACCESS_TOKEN, str(value))


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
        events: list[str] | None = None,
        consumed_days=(),
    ) -> None:
        self._claims = list(claims)
        self._save_failures = save_failures
        self.events = events if events is not None else []
        self.consumed_days = list(consumed_days)
        self.saved: list[dict[str, object]] = []
        self.revoked: list[str] = []
        self.claim_calls: list[dict[str, object]] = []

    def claim_due(self, *, require_due: bool = True, refuse_if_consumed_on=None):
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
        self.events.append("save")
        self.saved.append(fields)
        consumed_at = fields.get("refresh_consumed_at")
        if consumed_at is not None:
            self.consumed_days.append(consumed_at.date())
        return True

    def revoke(self, *, reason: str, generation=None) -> bool:
        self.revoked.append(reason)
        return True

    def revoke_stale_consumed(self, **_kwargs) -> bool:
        return False


class ScriptedAuthorization:
    def __init__(self, outcome, *, lifetime: int | None = 7200) -> None:
        self._outcome = outcome
        self._lifetime = lifetime
        self.calls = 0

    def refresh(self, current: AuthorizationGrant):
        self.calls += 1
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome, derived(lifetime=self._lifetime)


class _Claim:
    """``StoredCredential`` 的最小替身：按需入口只用到这四个字段。"""

    def __init__(self, *, generation: str = "gen-1") -> None:
        self.subject_open_id = "ou_delegated_authorization_subject"
        self.grant = AuthorizationGrant(SecretToken(FAKE_REFRESH_TOKEN), 604800, "offline_access")
        self.generation = generation


def build_supply_loop(
    *,
    claims=None,
    outcome=None,
    save_failures: int = 0,
    lifetime: int | None = 7200,
    consumed_days=(),
    now: datetime = DAY,
):
    events: list[str] = []
    holder = RecordingHolder(events)
    vault = ScriptedVault(
        [_Claim()] if claims is None else claims,
        save_failures=save_failures,
        events=events,
        consumed_days=consumed_days,
    )
    replacement = AuthorizationGrant(SecretToken(FAKE_NEXT_REFRESH_TOKEN), 604800, "offline_access")
    authorization = ScriptedAuthorization(
        replacement if outcome is None else outcome, lifetime=lifetime
    )
    clock = MovableClock(now)
    loop = CredentialRotationLoop(
        vault=vault,
        authorization=authorization,
        interval_seconds=0.01,
        holder=holder,
        clock=clock,
    )
    return loop, vault, holder, events, clock, authorization


class OnDemandRefreshTest(unittest.TestCase):
    def test_the_credential_is_persisted_before_the_derived_token_is_handed_over(self) -> None:
        """**次序是这条接线的核心安全属性**。

        ``refresh_token`` 一次性有效："换到了但没落盘"等于凭据丢失、需人工重新授权。
        先把派生令牌交出去，会让这条丢失路径在日报照常工作的表象下发生，直到下一次
        轮换才被发现。变异验红锚点：把 ``_remember_derived`` 挪到 ``_save_with_retry``
        之前，本用例必须变红。
        """

        loop, _vault, holder, events, _clock, _authorization = build_supply_loop()

        loop.refresh_for_supply()

        self.assertEqual(events, ["save", "store"])
        self.assertTrue(holder.has_token)

    def test_the_on_demand_claim_asks_for_a_credential_that_is_not_due_yet(self) -> None:
        """日报每天要令牌，而轮换点五天多才到一次；放开到期判定必须配一道频率上界，
        因此两个参数**成对**出现。"""

        loop, vault, _holder, _events, _clock, _authorization = build_supply_loop()

        loop.refresh_for_supply()

        self.assertEqual(len(vault.claim_calls), 1)
        self.assertIs(vault.claim_calls[0]["require_due"], False)
        self.assertEqual(vault.claim_calls[0]["refuse_if_consumed_on"], DAY.date())

    def test_the_persisted_credential_records_the_day_it_was_consumed(self) -> None:
        """重启也抹不掉的那道上界：判据随凭据落盘。"""

        loop, vault, _holder, _events, _clock, _authorization = build_supply_loop()

        loop.refresh_for_supply()

        self.assertEqual(vault.saved[0]["refresh_consumed_at"], DAY)
        self.assertEqual(vault.consumed_days, [DAY.date()])

    def test_a_second_refresh_on_the_same_day_is_refused_by_the_persisted_ceiling(self) -> None:
        """进程内预算清零（重启）也拦不住的那一次，由凭据文件里的判据挡下。"""

        loop, vault, _holder, _events, _clock, authorization = build_supply_loop(
            claims=[_Claim(), _Claim()]
        )
        loop.refresh_for_supply()

        with self.assertRaises(AccessTokenUnavailable) as raised:
            loop.refresh_for_supply()

        self.assertEqual(raised.exception.reason, "refresh_already_consumed_today")
        self.assertEqual(authorization.calls, 1, "被上界拒绝时不得再向飞书发起续期")
        self.assertEqual(vault.revoked, [], "被上界拒绝不是凭据问题，不得撤销")

    def test_a_persist_failure_hands_out_nothing_and_reports_the_credential_as_lost(self) -> None:
        """写盘失败＝凭据丢失风险：**不交出令牌**、撤销、按不可恢复响亮留痕。

        变异验红锚点：把"落盘失败仍然交出令牌"改回去，本用例必须变红。
        """

        loop, vault, holder, _events, _clock, _authorization = build_supply_loop(save_failures=99)

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

        loop, vault, holder, events, _clock, _authorization = build_supply_loop(save_failures=1)

        loop.refresh_for_supply()

        self.assertEqual(events, ["save", "store"])
        self.assertTrue(holder.has_token)
        self.assertEqual(vault.revoked, [])

    def test_a_definite_refresh_failure_revokes_and_reports_the_classification(self) -> None:
        """`V-身份-04`：续期失败一律撤销，不重放一次性凭据。"""

        from lingxi.adapters.feishu_directory import FeishuDirectoryError

        loop, vault, holder, _events, _clock, _authorization = build_supply_loop(
            outcome=FeishuDirectoryError("feishu_code_20037")
        )

        with self.assertRaises(AccessTokenUnavailable) as raised:
            loop.refresh_for_supply()

        self.assertEqual(raised.exception.reason, "refresh_failed")
        self.assertEqual(vault.revoked, ["refresh_failed"])
        self.assertFalse(holder.has_token)

    def test_an_indeterminate_refresh_is_classified_apart_from_a_definite_failure(self) -> None:
        loop, vault, _holder, _events, _clock, _authorization = build_supply_loop(
            outcome=TimeoutError("模拟回程超时")
        )

        with self.assertRaises(AccessTokenUnavailable) as raised:
            loop.refresh_for_supply()

        self.assertEqual(raised.exception.reason, "refresh_indeterminate")
        self.assertEqual(vault.revoked, ["refresh_indeterminate"])

    def test_no_claimable_credential_is_not_disguised_as_anything_else(self) -> None:
        loop, vault, _holder, _events, _clock, authorization = build_supply_loop(claims=[None])

        with self.assertRaises(AccessTokenUnavailable) as raised:
            loop.refresh_for_supply()

        self.assertEqual(raised.exception.reason, "no_credential_available")
        self.assertEqual(authorization.calls, 0)
        self.assertEqual(vault.revoked, [])

    def test_a_stopping_process_never_starts_a_refresh(self) -> None:
        """停止之后不再开启任何一次续期：半途中断的续期等于凭据丢失
        （模块头注释、`V-部署-03`）。"""

        loop, vault, _holder, _events, _clock, authorization = build_supply_loop()
        loop.request_stop()

        with self.assertRaises(AccessTokenUnavailable) as raised:
            loop.refresh_for_supply()

        self.assertEqual(raised.exception.reason, "scheduler_stopping")
        self.assertEqual(vault.claim_calls, [], "停止中一条都不领")
        self.assertEqual(authorization.calls, 0)

    def test_a_refresh_without_a_token_lifetime_keeps_the_credential(self) -> None:
        """飞书没给短期令牌寿命：新凭据已经落盘，**不撤销**；只是今天没有令牌可用。"""

        loop, vault, holder, _events, _clock, _authorization = build_supply_loop(lifetime=None)

        with self.assertRaises(AccessTokenUnavailable) as raised:
            loop.refresh_for_supply()

        self.assertEqual(raised.exception.reason, "derived_token_unusable")
        self.assertEqual(len(vault.saved), 1, "凭据照常落盘")
        self.assertEqual(vault.revoked, [], "凭据没有丢，不得撤销")
        self.assertFalse(holder.has_token)


class ScheduledRotationFeedsTheHolderTest(unittest.TestCase):
    def test_a_due_rotation_also_hands_the_derived_token_to_the_holder(self) -> None:
        """到期轮换那一条路径同样不再丢弃派生令牌（此前 ``:328`` 当场丢弃）。"""

        loop, vault, holder, events, _clock, _authorization = build_supply_loop()

        report = loop.run_once()

        self.assertEqual((report.claimed, report.rotated, report.revoked), (1, 1, 0))
        self.assertEqual(events, ["save", "store"])
        self.assertTrue(holder.has_token)
        self.assertEqual(vault.saved[0]["refresh_consumed_at"], DAY)

    def test_a_due_rotation_that_cannot_persist_hands_out_nothing(self) -> None:
        loop, vault, holder, _events, _clock, _authorization = build_supply_loop(save_failures=99)

        with no_retry_backoff(), self.assertLogs("lingxi.apps.scheduler", level="ERROR"):
            report = loop.run_once()

        self.assertEqual((report.claimed, report.rotated, report.revoked), (1, 0, 1))
        self.assertFalse(holder.has_token)
        self.assertEqual(vault.revoked, ["rotation_persist_failed"])


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
# 七、装配：日报职责真的拿到这条供给
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


if __name__ == "__main__":
    unittest.main()
