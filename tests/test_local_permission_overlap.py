"""职位授权范围重叠的纯逻辑与渲染断言（无数据库）。

裁定：本地补充库内部重叠按差集补齐（只登记缺的项，卡片展示「本次新增 N 项、已有
M 项沿用」，一项都不缺则拒绝并带项数）；撤销只撤本笔新增行；撤销回执加一句「该
用户经银河来源仍持有其中 K 项」，银河读不到时写「暂不可读」而不是 0 项。真库侧
（准备判定、执行插入、撤销范围）见 ``tests/test_local_permission_overlap_postgres.py``。
"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta

from lingxi.core.admin.card_callback import _outcome_text
from lingxi.core.admin.notification import (
    describe_galaxy_retention,
    render_confirm_card,
    render_group_notice,
    render_terminal_card,
)
from lingxi.core.admin.pending_action import (
    PendingAction,
    PendingActionStatus,
    PendingActionType,
    decide_prepare,
    local_permission_pairs,
    narrow_grant_payload,
)
from lingxi.core.permission.galaxy_retention import (
    GalaxyMetricMap,
    build_galaxy_metric_map,
    retained_by_galaxy,
)
from lingxi.core.permission.metric_translation import UncoveredPermissionCombinationError
from lingxi.core.permission.position_override import split_missing_pairs

NOW = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)

_POSITION_PAYLOAD = {
    "position_name": "A运营",
    "function": "运营",
    "company_scope": "1011",
    "companies": ["1011"],
    "pairs": [["1011", "m_a"], ["1011", "m_b"], ["1011", "m_c"]],
    "reason": "特批",
    "permission_group_id": "lpg_01M1C90YDGMTY567GDTZZJ4C5E",
}

_GROUP_REVOKE_PAYLOAD = {
    "permission_group_id": "lpg_01M1C90YDGMTY567GDTZZJ4C5E",
    "override_ids": ["lpo_1", "lpo_2"],
    "direction": "grant",
    "position_name": "A运营",
    "company_scope": "1011",
    "companies": ["1011"],
    "pairs": [["1011", "m_b"], ["1011", "m_c"]],
    "reason": "管理卡撤销职位范围授权",
}

_SINGLE_REVOKE_PAYLOAD = {
    "override_id": "lpo_01JGFJJZ008XSHEADGG8V74SPC",
    "direction": "grant",
    "company_id": "1011",
    "metric_name": "m_a",
    "reason": "离职交接",
}


def _pending(
    *,
    action_type: PendingActionType,
    payload: dict | None,
    status: PendingActionStatus = PendingActionStatus.EXECUTED,
) -> PendingAction:
    return PendingAction(
        id="pac_01JGFJJZ008XSHEADGG8V74SPC",
        action_type=action_type,
        target_open_id="ou_target",
        target_state_snapshot="active",
        initiated_by_open_id="ou_admin",
        status=status,
        card_delivered=True,
        card_id="cardkit_id_1",
        reason=None,
        created_at=NOW,
        confirm_deadline_at=NOW + timedelta(minutes=10),
        decided_at=NOW if status is not PendingActionStatus.PENDING else None,
        decided_by_open_id="ou_admin" if status is not PendingActionStatus.PENDING else None,
        payload=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
    )


class SplitMissingPairsTests(unittest.TestCase):
    REQUESTED = (("1011", "m_a"), ("1011", "m_b"), ("1011", "m_c"))

    def test_partial_overlap_keeps_only_the_missing_pairs_in_request_order(self) -> None:
        missing, reused = split_missing_pairs(self.REQUESTED, [("1011", "m_b")])
        self.assertEqual(missing, (("1011", "m_a"), ("1011", "m_c")))
        self.assertEqual(reused, (("1011", "m_b"),))

    def test_full_overlap_leaves_nothing_to_add(self) -> None:
        missing, reused = split_missing_pairs(self.REQUESTED, list(self.REQUESTED))
        self.assertEqual(missing, ())
        self.assertEqual(reused, self.REQUESTED)

    def test_no_overlap_adds_everything(self) -> None:
        missing, reused = split_missing_pairs(self.REQUESTED, [("2022", "m_a")])
        self.assertEqual(missing, self.REQUESTED)
        self.assertEqual(reused, ())

    def test_existing_rows_of_other_keys_or_duplicates_do_not_leak(self) -> None:
        missing, reused = split_missing_pairs(
            (("1011", "m_a"), ("1011", "m_a"), ("1011", "m_b")), [("1011", "m_a"), ("9", "m_b")]
        )
        self.assertEqual(missing, (("1011", "m_b"),))
        self.assertEqual(reused, (("1011", "m_a"),))


class DecidePrepareAllHeldTests(unittest.TestCase):
    def test_all_held_grant_is_rejected_with_the_pair_count(self) -> None:
        decision = decide_prepare(
            action_type=PendingActionType.LOCAL_PERMISSION_GRANT,
            current_account_state="present",
            held_pair_count=3,
        )
        self.assertFalse(decision.ok)
        self.assertEqual(decision.code, "target_state_changed")
        self.assertEqual(decision.message, "所选职位范围的 3 项均已登记，无需补授。")

    def test_without_a_count_the_existing_wording_is_untouched(self) -> None:
        decision = decide_prepare(
            action_type=PendingActionType.LOCAL_PERMISSION_GRANT, current_account_state="present"
        )
        self.assertIn("本地权限", decision.message)
        self.assertNotIn("均已登记", decision.message)

    def test_the_count_never_changes_suppress_wording(self) -> None:
        decision = decide_prepare(
            action_type=PendingActionType.LOCAL_PERMISSION_SUPPRESS,
            current_account_state="present",
            held_pair_count=1,
        )
        self.assertNotIn("均已登记", decision.message)


class PayloadHelpersTests(unittest.TestCase):
    def test_narrow_grant_payload_keeps_only_missing_pairs_and_records_reuse(self) -> None:
        narrowed = json.loads(
            narrow_grant_payload(
                json.dumps(_POSITION_PAYLOAD), missing=(("1011", "m_c"),), reused_count=2
            )
        )
        self.assertEqual(narrowed["pairs"], [["1011", "m_c"]])
        self.assertEqual(narrowed["reused_count"], 2)
        for key in ("position_name", "company_scope", "companies", "reason", "permission_group_id"):
            self.assertEqual(narrowed[key], _POSITION_PAYLOAD[key])

    def test_local_permission_pairs_reads_both_payload_shapes(self) -> None:
        self.assertEqual(
            local_permission_pairs(_GROUP_REVOKE_PAYLOAD), (("1011", "m_b"), ("1011", "m_c"))
        )
        self.assertEqual(local_permission_pairs(_SINGLE_REVOKE_PAYLOAD), (("1011", "m_a"),))
        self.assertEqual(local_permission_pairs({"reason": "x"}), ())


class RetainedByGalaxyTests(unittest.TestCase):
    PAIRS = (("1011", "m_a"), ("1011", "m_b"), ("2022", "m_a"))

    def test_concrete_company_keys_count_only_matching_metrics(self) -> None:
        galaxy = GalaxyMetricMap(
            permissions={"1011": ("m_a",), "2022": ("m_x",)}, full_access_wildcard=False
        )
        self.assertEqual(retained_by_galaxy(self.PAIRS, galaxy), 1)

    def test_no_galaxy_permission_at_all_retains_nothing(self) -> None:
        self.assertEqual(
            retained_by_galaxy(
                self.PAIRS, GalaxyMetricMap(permissions={}, full_access_wildcard=False)
            ),
            0,
        )

    def test_full_access_wildcard_retains_every_pair_including_all_scope(self) -> None:
        galaxy = GalaxyMetricMap(permissions={"*": ("m_a",)}, full_access_wildcard=True)
        self.assertEqual(retained_by_galaxy(self.PAIRS + (("*", "m_z"),), galaxy), 4)

    def test_limited_wildcard_judges_by_metric_regardless_of_company(self) -> None:
        galaxy = GalaxyMetricMap(permissions={"*": ("m_a",)}, full_access_wildcard=False)
        self.assertEqual(retained_by_galaxy(self.PAIRS + (("*", "m_a"),), galaxy), 3)

    def test_all_scope_local_pair_is_not_retained_without_a_galaxy_wildcard(self) -> None:
        galaxy = GalaxyMetricMap(
            permissions={"1011": ("m_a",), "2022": ("m_a",)}, full_access_wildcard=False
        )
        self.assertEqual(retained_by_galaxy((("*", "m_a"),), galaxy), 0)

    def test_build_marks_full_access_and_fails_loudly_on_uncovered_mapping(self) -> None:
        mapping = {"1011": {"运营": ("m_a", "m_b")}, "*": {"后台管理员": ("m_a",)}}
        limited = build_galaxy_metric_map(
            companies=("1011",), functions=("运营",), all_companies=False, mapping=mapping
        )
        self.assertEqual(limited.permissions, {"1011": ("m_a", "m_b")})
        self.assertFalse(limited.full_access_wildcard)
        full = build_galaxy_metric_map(
            companies=("1011",), functions=("后台管理员",), all_companies=True, mapping=mapping
        )
        self.assertTrue(full.full_access_wildcard)
        self.assertIn("*", full.permissions)
        with self.assertRaises(UncoveredPermissionCombinationError):
            build_galaxy_metric_map(
                companies=("3033",), functions=("运营",), all_companies=False, mapping=mapping
            )


class ReusedPairsRenderingTests(unittest.TestCase):
    def _grant(self, **overrides) -> PendingAction:
        return _pending(
            action_type=PendingActionType.LOCAL_PERMISSION_GRANT,
            payload={**_POSITION_PAYLOAD, **overrides},
            status=PendingActionStatus.PENDING,
        )

    def test_confirm_and_terminal_cards_show_added_and_reused_counts(self) -> None:
        pending = self._grant(pairs=[["1011", "m_b"], ["1011", "m_c"]], reused_count=1)
        confirm = render_confirm_card(pending, target_label="张三（zhang@example.com）")
        terminal = render_terminal_card(
            pending, target_label="张三（zhang@example.com）", outcome_text="操作已记录"
        )
        self.assertIn("本次新增 2 项、已有 1 项沿用", confirm.body)
        self.assertIn("本次新增 2 项、已有 1 项沿用", terminal.body)

    def test_no_reuse_keeps_the_card_free_of_the_sentence(self) -> None:
        for overrides in ({}, {"reused_count": 0}):
            with self.subTest(overrides=overrides):
                card = render_confirm_card(self._grant(**overrides), target_label="张三")
                self.assertNotIn("沿用", card.body)
                self.assertNotIn("本次新增", card.body)


class GalaxyRetentionRenderingTests(unittest.TestCase):
    def _revoke(self, payload: dict, **kwargs) -> PendingAction:
        return _pending(
            action_type=PendingActionType.LOCAL_PERMISSION_REVOKE, payload=payload, **kwargs
        )

    def test_three_states_render_three_distinct_sentences(self) -> None:
        cases = {
            2: "该用户经银河来源仍持有其中 2 项",
            0: "该用户经银河来源仍持有其中 0 项",
            None: "银河来源暂不可读",
        }
        for retained, expected in cases.items():
            with self.subTest(retained=retained):
                pending = self._revoke({**_GROUP_REVOKE_PAYLOAD, "galaxy_retained": retained})
                self.assertEqual(describe_galaxy_retention(pending), expected)

    def test_unreadable_is_never_rendered_as_zero(self) -> None:
        """变异锚点：把「读不到」渲染成 0 项，本用例由绿转红。"""

        pending = self._revoke({**_SINGLE_REVOKE_PAYLOAD, "galaxy_retained": None})
        sentence = describe_galaxy_retention(pending)
        self.assertEqual(sentence, "银河来源暂不可读")
        self.assertNotIn("0 项", _outcome_text(pending))
        self.assertNotIn("0 项", render_group_notice(pending, target_label="张三"))

    def test_rows_prepared_before_the_field_existed_and_other_actions_get_no_sentence(self) -> None:
        self.assertIsNone(describe_galaxy_retention(self._revoke(_GROUP_REVOKE_PAYLOAD)))
        grant = _pending(action_type=PendingActionType.LOCAL_PERMISSION_GRANT, payload=None)
        self.assertIsNone(describe_galaxy_retention(grant))

    def test_executed_revoke_terminal_card_and_group_notice_carry_the_same_sentence(
        self,
    ) -> None:
        for payload in (_GROUP_REVOKE_PAYLOAD, _SINGLE_REVOKE_PAYLOAD):
            with self.subTest(shape="group" if "pairs" in payload else "single"):
                pending = self._revoke({**payload, "galaxy_retained": 1})
                outcome = _outcome_text(pending)
                notice = render_group_notice(pending, target_label="张三")
                self.assertTrue(outcome.startswith("操作已记录，权限正在下发"), outcome)
                self.assertIn("该用户经银河来源仍持有其中 1 项", outcome)
                self.assertIn("该用户经银河来源仍持有其中 1 项", notice)
                self.assertIn("撤销", notice)

    def test_cancelled_or_failed_revoke_does_not_claim_retention(self) -> None:
        for status in (PendingActionStatus.CANCELLED, PendingActionStatus.EXPIRED):
            with self.subTest(status=status):
                pending = self._revoke(
                    {**_GROUP_REVOKE_PAYLOAD, "galaxy_retained": 1}, status=status
                )
                self.assertNotIn("仍持有", _outcome_text(pending))
                self.assertNotIn("仍持有", render_group_notice(pending, target_label="张三"))

    def test_non_revoke_outcome_text_is_byte_identical(self) -> None:
        grant = _pending(
            action_type=PendingActionType.LOCAL_PERMISSION_GRANT,
            payload={**_POSITION_PAYLOAD, "galaxy_retained": 1},
        )
        self.assertEqual(_outcome_text(grant), "操作已记录，权限正在下发")


if __name__ == "__main__":
    unittest.main()
