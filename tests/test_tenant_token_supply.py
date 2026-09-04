"""应用身份令牌的进程内缓存与按需续期（Issue #226 裁定 3）。

认领：手上有新鲜令牌就不重新请求；没有或临期就换一次；换回来的令牌寿命未知/一到手
就临期时失败关闭（``PermissionTableAccessTokenUnavailable("fetch_unavailable")``，
与 :mod:`lingxi.core.permission.table_access_token_supply` 既有的失败词表同一套，
不新增分类）；不设"每日至多一次"的频率上界（与花名册的
``RosterAccessTokenProvider`` 唯一的实质差异，理由见模块文档）。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from lingxi.core.identity.access_token_supply import (
    DEFAULT_ACCESS_TOKEN_SAFETY_MARGIN,
    DerivedAccessTokenHolder,
)
from lingxi.core.identity.credentials import DerivedAccessToken, SecretToken
from lingxi.core.permission.table_access_token_supply import (
    PermissionTableAccessTokenUnavailable,
)
from lingxi.core.permission.tenant_token_supply import TenantAccessTokenSupply

FAKE_TOKEN = "fake-tenant-token-for-tests-only"
DAY = datetime(2026, 8, 18, 9, 0, tzinfo=UTC)


class MovableClock:
    def __init__(self, now: datetime = DAY) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now = self.now + delta


def derived(*, lifetime: int | None = 7200, value: str = FAKE_TOKEN) -> DerivedAccessToken:
    return DerivedAccessToken(SecretToken(value), lifetime)


class _CountingFetch:
    """按脚本依次返回值或抛异常的假 fetch。"""

    def __init__(self, outcomes=None) -> None:
        self._outcomes = list(outcomes or [])
        self.calls = 0

    def __call__(self) -> DerivedAccessToken:
        self.calls += 1
        if self._outcomes:
            outcome = self._outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return derived()


def build_supply(*, outcomes=None, now: datetime = DAY):
    clock = MovableClock(now)
    fetch = _CountingFetch(outcomes=outcomes)
    supply = TenantAccessTokenSupply(fetch=fetch, clock=clock)
    return supply, fetch, clock


class ConstructionTest(unittest.TestCase):
    def test_a_non_callable_fetch_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TenantAccessTokenSupply(fetch="not-callable")  # type: ignore[arg-type]


class FreshnessTest(unittest.TestCase):
    def test_a_cold_supply_fetches_on_the_first_call(self) -> None:
        supply, fetch, _clock = build_supply()

        self.assertEqual(supply(), FAKE_TOKEN)
        self.assertEqual(fetch.calls, 1)

    def test_a_fresh_cached_token_is_handed_out_without_a_new_fetch(self) -> None:
        supply, fetch, clock = build_supply()
        supply()

        clock.advance(timedelta(minutes=5))
        for _ in range(20):
            self.assertEqual(supply(), FAKE_TOKEN)

        self.assertEqual(fetch.calls, 1, "同一份令牌在有效期内不得重复换取")

    def test_an_expiring_token_triggers_the_next_fetch(self) -> None:
        supply, fetch, clock = build_supply()
        supply()

        clock.advance(timedelta(seconds=7200) - DEFAULT_ACCESS_TOKEN_SAFETY_MARGIN)
        self.assertEqual(supply(), FAKE_TOKEN)

        self.assertEqual(fetch.calls, 2)

    def test_there_is_no_daily_ceiling_unlike_the_roster_supply(self) -> None:
        """应用身份令牌换取用的是静态、可重复使用的 app_id/app_secret，不是一次性
        凭据，因此这里**不设**"每 UTC 日至多一次"的频率上界——多次过期都能各自
        触发一次续期。"""

        supply, fetch, clock = build_supply()
        supply()

        for _ in range(5):
            clock.advance(timedelta(hours=3))
            supply()

        self.assertEqual(fetch.calls, 6, "每次真正过期都应当能再次换取，没有账本拦着")


class FailClosedTest(unittest.TestCase):
    def test_a_fetch_error_propagates_unmodified(self) -> None:
        """``fetch`` 抛出的异常原样上抛，不在这里吞掉或改写——由外层
        ``PermissionTableAccessTokenProvider`` 归类。"""

        supply, _fetch, _clock = build_supply(outcomes=[RuntimeError("boom")])

        with self.assertRaises(RuntimeError):
            supply()

    def test_an_unusable_lifetime_fails_closed_with_the_known_classification(self) -> None:
        """换回来的令牌寿命未知（或一到手就临期）时，不得把它当"成功"交出去——
        这条不新增失败分类，直接复用外层 Provider 已经定义好的
        ``fetch_unavailable``，让整条链路的失败词表只有一份。"""

        supply, fetch, _clock = build_supply(outcomes=[derived(lifetime=None)])

        with self.assertRaises(PermissionTableAccessTokenUnavailable) as raised:
            supply()

        self.assertEqual(raised.exception.reason, "fetch_unavailable")
        self.assertEqual(fetch.calls, 1)

    def test_a_non_derived_access_token_return_value_is_rejected(self) -> None:
        """``fetch`` 必须返回 ``DerivedAccessToken``——裸字符串或其他类型直接拒绝，
        避免令牌明文以未包装的形态流转。"""

        supply, _fetch, _clock = build_supply(outcomes=[FAKE_TOKEN])  # type: ignore[list-item]

        with self.assertRaises(TypeError):
            supply()

    def test_never_returns_an_empty_or_placeholder_string(self) -> None:
        supply, _fetch, _clock = build_supply(outcomes=[PermissionTableAccessTokenUnavailable("fetch_unavailable")])

        with self.assertRaises(PermissionTableAccessTokenUnavailable):
            token = supply()
            self.fail(f"不该拿到任何返回值，却拿到了 {token!r}")


class CustomHolderTest(unittest.TestCase):
    def test_an_injected_holder_is_used_instead_of_a_fresh_one(self) -> None:
        holder = DerivedAccessTokenHolder()
        holder.store(derived(), now=DAY)
        fetch = _CountingFetch()
        supply = TenantAccessTokenSupply(fetch=fetch, holder=holder, clock=MovableClock(DAY))

        self.assertEqual(supply(), FAKE_TOKEN)
        self.assertEqual(fetch.calls, 0, "注入的持有者里已经有新鲜令牌，不该再去换")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
