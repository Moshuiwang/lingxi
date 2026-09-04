"""权限发布表短期令牌供给的方向无关外壳（Issue #226 前置）。

三个候选写入身份方向（复用 #215 已交付的专用主体 / 新建专用主体 / 应用身份）由产品
负责人裁定，本文件**不涉及、也不预判**任何一个方向的具体实现——钉的是三个方向共同
需要的外壳：拿新鲜令牌、拿不到时怎么失败关闭、失败怎么审计、令牌值绝不泄漏。

认领的是 #226 完成标准里方向无关的那部分：

- 令牌获取失败的失败关闭语义，且"未接线"（``build_loop`` 收到 ``None``，已在
  ``_build_permission_publish_duty`` 处理，见 ``tests/test_permission_publish_duty.py``
  的 ``DutyRegistrationTest``）与"配了但拿不到"（本文件）两种状态**审计动作名不同**，
  因此天然可分辨；
- 令牌值不进日志、审计与异常；
- 装配行为的形状（``Callable[[], str]``）与 #215 的 ``RosterAccessTokenProvider``
  一致，供任何一个方向直接复用这层外壳。

真实的凭据来源（``fetch`` 参数的具体实现）不属于本 Story，见
:mod:`lingxi.core.permission.table_access_token_supply` 的模块文档。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from lingxi.core.permission.table_access_token_supply import (
    TABLE_TOKEN_SUPPLY_FAILURE_REASONS,
    PermissionTableAccessTokenProvider,
    PermissionTableAccessTokenUnavailableError,
)

FAKE_TOKEN = "fake-permission-table-token-for-tests-only"
DAY = datetime(2026, 8, 18, 9, 0, tzinfo=UTC)


class MovableClock:
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
        return " ".join(f"{action} {sorted(fields.items())}" for action, fields in self.records)


class _ScriptedFetch:
    """按脚本依次返回值或抛异常的假 fetch。脚本用完之后固定返回 ``FAKE_TOKEN``。"""

    def __init__(self, outcomes=None) -> None:
        self._outcomes = list(outcomes or [])
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self._outcomes:
            outcome = self._outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return FAKE_TOKEN


def build_provider(*, outcomes=None, now: datetime = DAY):
    clock = MovableClock(now)
    audit = RecordingAudit()
    fetch = _ScriptedFetch(outcomes=outcomes)
    provider = PermissionTableAccessTokenProvider(fetch=fetch, audit=audit, clock=clock)
    return provider, fetch, clock, audit


class FailureReasonVocabularyTest(unittest.TestCase):
    def test_a_reason_outside_the_vocabulary_is_refused(self) -> None:
        """否定断言：分类会进审计与异常正文，自由文本足以把令牌值带出去。"""

        for smuggled in (f"fetch_error token={FAKE_TOKEN}", FAKE_TOKEN, "", "fetch_error "):
            with self.subTest(reason=smuggled[:16]):
                with self.assertRaises(ValueError) as raised:
                    PermissionTableAccessTokenUnavailableError(smuggled)
                self.assertNotIn(FAKE_TOKEN, str(raised.exception))

    def test_the_exception_text_is_exactly_the_classification(self) -> None:
        error = PermissionTableAccessTokenUnavailableError("fetch_unavailable")

        self.assertEqual(str(error), "fetch_unavailable")
        self.assertEqual(error.reason, "fetch_unavailable")
        self.assertIn("fetch_unavailable", TABLE_TOKEN_SUPPLY_FAILURE_REASONS)


class ProviderShapeTest(unittest.TestCase):
    """``PermissionTableAccessTokenProvider`` 是 ``Callable[[], str]``：三个候选方向
    都能把自己的 ``fetch`` 塞进来，外壳行为不因方向而异。"""

    def test_the_provider_is_callable_and_hands_out_the_fetched_token(self) -> None:
        provider, fetch, _clock, _audit = build_provider()

        self.assertEqual(provider(), FAKE_TOKEN)
        self.assertEqual(fetch.calls, 1)

    def test_every_call_re_invokes_fetch_no_hidden_cache(self) -> None:
        """本外壳没有新鲜度缓存——是否需要缓存是方向裁定的一部分，见模块文档；
        这里钉住"没有缓存"这条当前事实，一旦有人偷偷加了缓存，这条用例先变红。"""

        provider, fetch, _clock, _audit = build_provider()

        provider()
        provider()
        provider()

        self.assertEqual(fetch.calls, 3)

    def test_a_non_callable_fetch_is_rejected_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            PermissionTableAccessTokenProvider(fetch="not-callable")  # type: ignore[arg-type]


class FailClosedTest(unittest.TestCase):
    """拿不到令牌：失败关闭，绝不返回空串或占位符。"""

    def test_a_declared_unavailable_failure_propagates_and_is_audited(self) -> None:
        provider, _fetch, _clock, audit = build_provider(
            outcomes=[PermissionTableAccessTokenUnavailableError("fetch_unavailable")]
        )

        with self.assertRaises(PermissionTableAccessTokenUnavailableError) as raised:
            provider()

        self.assertEqual(raised.exception.reason, "fetch_unavailable")
        self.assertEqual(audit.actions(), ["permission_table_access_token.unavailable"])
        self.assertEqual(audit.records[0][1]["reason"], "fetch_unavailable")

    def test_an_unknown_exception_is_folded_into_a_single_classification(self) -> None:
        """注入的 fetch 抛了本模块不认识的异常：只记分类与异常类名，不记正文——
        正文可能带响应内容或凭据材料。"""

        provider, _fetch, _clock, audit = build_provider(
            outcomes=[RuntimeError(f"boom token={FAKE_TOKEN}")]
        )

        with self.assertRaises(PermissionTableAccessTokenUnavailableError) as raised:
            provider()

        self.assertEqual(raised.exception.reason, "fetch_error")
        self.assertNotIn(FAKE_TOKEN, audit.rendered())
        self.assertEqual(audit.records[0][1]["error_type"], "RuntimeError")

    def test_an_empty_token_is_refused_not_handed_out(self) -> None:
        """空串会让"没有凭据"伪装成"发布表读写失败"，把排障指向错误的地方。"""

        provider, _fetch, _clock, audit = build_provider(outcomes=[""])

        with self.assertRaises(PermissionTableAccessTokenUnavailableError) as raised:
            provider()

        self.assertEqual(raised.exception.reason, "token_empty")
        self.assertEqual(audit.actions(), ["permission_table_access_token.unavailable"])

    def test_the_failure_is_distinguishable_from_the_unwired_assembly_reason(self) -> None:
        """未接线（装配层的 ``permission_table_access_token_unwired``）与配了但拿不到
        （本模块的 ``permission_table_access_token.unavailable``）**审计动作名不同**，
        因此不依赖任何额外判断就能分辨——这正是 #226 要求的可分辨性。"""

        provider, _fetch, _clock, audit = build_provider(
            outcomes=[PermissionTableAccessTokenUnavailableError("fetch_unavailable")]
        )

        with self.assertRaises(PermissionTableAccessTokenUnavailableError):
            provider()

        self.assertNotIn("permission_table_access_token_unwired", audit.actions())
        self.assertIn("permission_table_access_token.unavailable", audit.actions())


class AuditDeduplicationTest(unittest.TestCase):
    """同一天同一分类只记一条：定时循环默认每分钟一轮，逐次记录会把真正的信号淹掉。"""

    def test_repeated_failures_the_same_day_are_recorded_once(self) -> None:
        provider, _fetch, _clock, audit = build_provider(
            outcomes=[
                PermissionTableAccessTokenUnavailableError("fetch_unavailable"),
                PermissionTableAccessTokenUnavailableError("fetch_unavailable"),
                PermissionTableAccessTokenUnavailableError("fetch_unavailable"),
            ]
        )

        for _ in range(3):
            with self.assertRaises(PermissionTableAccessTokenUnavailableError):
                provider()

        self.assertEqual(audit.actions(), ["permission_table_access_token.unavailable"])

    def test_a_different_reason_the_same_day_is_recorded_separately(self) -> None:
        provider, _fetch, _clock, audit = build_provider(
            outcomes=[
                PermissionTableAccessTokenUnavailableError("fetch_unavailable"),
                "",
            ]
        )

        with self.assertRaises(PermissionTableAccessTokenUnavailableError):
            provider()
        with self.assertRaises(PermissionTableAccessTokenUnavailableError):
            provider()

        self.assertEqual(
            [entry[1]["reason"] for entry in audit.records],
            ["fetch_unavailable", "token_empty"],
        )

    def test_the_next_day_records_again(self) -> None:
        provider, fetch, clock, audit = build_provider(
            outcomes=[
                PermissionTableAccessTokenUnavailableError("fetch_unavailable"),
                PermissionTableAccessTokenUnavailableError("fetch_unavailable"),
            ]
        )

        with self.assertRaises(PermissionTableAccessTokenUnavailableError):
            provider()
        clock.advance(timedelta(days=1))
        with self.assertRaises(PermissionTableAccessTokenUnavailableError):
            provider()

        self.assertEqual(
            [entry[1]["report_date"] for entry in audit.records],
            [DAY.date().isoformat(), (DAY.date() + timedelta(days=1)).isoformat()],
        )

    def test_a_success_between_two_failures_does_not_reset_the_dedup_window(self) -> None:
        """去重按"当天记过没有"，不因为中间来了一次成功就重置。"""

        provider, _fetch, _clock, audit = build_provider(
            outcomes=[
                PermissionTableAccessTokenUnavailableError("fetch_unavailable"),
                FAKE_TOKEN,
                PermissionTableAccessTokenUnavailableError("fetch_unavailable"),
            ]
        )

        with self.assertRaises(PermissionTableAccessTokenUnavailableError):
            provider()
        self.assertEqual(provider(), FAKE_TOKEN)
        with self.assertRaises(PermissionTableAccessTokenUnavailableError):
            provider()

        self.assertEqual(audit.actions(), ["permission_table_access_token.unavailable"])


class NoAuditSinkTest(unittest.TestCase):
    def test_without_an_audit_sink_the_failure_still_propagates(self) -> None:
        fetch = _ScriptedFetch(
            outcomes=[PermissionTableAccessTokenUnavailableError("fetch_unavailable")]
        )
        provider = PermissionTableAccessTokenProvider(fetch=fetch)

        with self.assertRaises(PermissionTableAccessTokenUnavailableError):
            provider()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
