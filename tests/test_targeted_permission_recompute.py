"""``TargetedPermissionRecompute`` 的纯逻辑验收（Issue #438）。

只测本模块自己新增的编排：判定规则本身（匹配/聚合/翻译/合并/发布行结算）已经在
``tests/test_permission_refresh_duty.py`` 逐条钉过，这里复用同一批夹具与假实现
（``roster_row``/``galaxy_snapshot``/``identity``/``FakeGalaxy``/``FakePublish
History``/``FakeDecisions``/``FakeLocalOverrides``/``RecordingAudit``/``FixedClock``
——两处各写一份迟早漂移，与 ``test_permission_refresh_postgres.py`` 复用
``code_without_docstrings`` 同一条既有理由），只钉本模块特有的行为：

1. ``force_revoke`` 与 ``recompute_and_publish`` 是两条独立路径（模块文档「为什么是
   两个方法」）——``force_revoke`` 完全不碰花名册/银河/翻译，即使这些前置缺失也能
   正常清空一个已有发布足迹的用户。
2. ``recompute_and_publish`` 的跳过原因码齐全、可分辨（Issue #438「通配用户等跳过
   场景」的审计要求）。
3. 三处刻意与每日批不同（不做花名册今天更新过的顺序判据、``legacy`` 恒 ``None``、
   ``token_cipher`` 恒 ``None``）真的按文档实现。
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from lingxi.core.identity.roster_audit import ArchivedIdentity
from lingxi.core.permission.local_override import LocalPermissionOverrideEntry, OverrideDirection
from lingxi.core.permission.targeted_recompute import (
    RecomputeKind,
    SKIP_ARCHIVED_IDENTITY_INCOMPLETE,
    SKIP_MATCH_FAILED,
    SKIP_METRIC_TRANSLATION_UNAVAILABLE,
    SKIP_MISSING_PERSONNEL_ID,
    SKIP_MISSING_ROSTER_SNAPSHOT,
    SKIP_NO_GALAXY_BATCH,
    SKIP_NO_PUBLISHED_ROW,
    SKIP_USER_NOT_ACTIVE,
    TargetedPermissionRecompute,
)

from test_permission_refresh_duty import (
    COMPANY_ID,
    METRIC_NAME,
    METRIC_TRANSLATION_MAP,
    ROLE_FUNCTION_MAP,
    USER_ONE,
    FakeDecisions,
    FakeGalaxy,
    FakeLocalOverrides,
    FakePublishHistory,
    FixedClock,
    RecordingAudit,
    galaxy_snapshot,
    identity,
    roster_row,
)

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


class FakeIdentities:
    def __init__(self, *identities: ArchivedIdentity) -> None:
        self._by_id = {each.app_user_id: each for each in identities}

    def find_active(self, *, user_id: str):
        return self._by_id.get(user_id)


class FakeRosterRows:
    def __init__(self, rows=None) -> None:
        self._rows = rows

    def load_rows(self):
        return self._rows


def build_recompute(
    *,
    identities=(),
    roster_rows=(roster_row(),),
    galaxy=None,
    decisions=None,
    published_users=None,
    metric_translation_map=None,
    role_function_map=None,
    local_overrides=None,
    audit=None,
    clock=None,
):
    audit = audit or RecordingAudit()
    decisions = decisions or FakeDecisions()
    history = FakePublishHistory(published_users)
    recompute = TargetedPermissionRecompute(
        identities=FakeIdentities(*identities),
        roster_snapshot=FakeRosterRows(roster_rows),
        galaxy=FakeGalaxy(galaxy_snapshot() if galaxy is None else galaxy),
        decisions=decisions,
        publish_history=history,
        role_function_map=ROLE_FUNCTION_MAP if role_function_map is None else role_function_map,
        metric_translation_map=(
            METRIC_TRANSLATION_MAP if metric_translation_map is None else metric_translation_map
        ),
        audit=audit,
        local_overrides=local_overrides,
        clock=clock or FixedClock(NOW),
    )
    return recompute, {"audit": audit, "decisions": decisions, "history": history}


class ForceRevokeTests(unittest.TestCase):
    """``force_revoke``：只依赖身份 + 发布足迹，完全不碰花名册/银河/翻译。"""

    def test_revokes_a_user_with_an_existing_publish_footprint(self) -> None:
        recompute, parts = build_recompute(
            identities=(identity(),),
            roster_rows=None,  # 故意留空——force_revoke 不应该因此受影响
            galaxy=None,
            published_users={USER_ONE},
        )

        outcome = recompute.force_revoke(user_id=USER_ONE)

        self.assertEqual(outcome.kind, RecomputeKind.REVOKED)
        [call] = parts["decisions"].calls
        self.assertEqual(call["row"].permissions, "{}")
        self.assertIsNone(call["row"].token_cipher)
        fields = parts["audit"].fields_for("permission_targeted_recompute.completed")
        self.assertEqual(fields[-1]["mode"], "revoke")
        self.assertEqual(fields[-1]["kind"], "revoked")

    def test_skips_a_user_with_no_publish_footprint_and_explains_why(self) -> None:
        recompute, parts = build_recompute(identities=(identity(),), published_users=set())

        outcome = recompute.force_revoke(user_id=USER_ONE)

        self.assertEqual(outcome.kind, RecomputeKind.SKIPPED)
        self.assertEqual(outcome.reason, SKIP_NO_PUBLISHED_ROW)
        self.assertEqual(parts["decisions"].calls, [])

    def test_skips_an_unknown_or_deprovisioned_user(self) -> None:
        recompute, _ = build_recompute(identities=(), published_users={USER_ONE})

        outcome = recompute.force_revoke(user_id=USER_ONE)

        self.assertEqual(outcome.reason, SKIP_USER_NOT_ACTIVE)

    def test_skips_an_incomplete_archive(self) -> None:
        recompute, _ = build_recompute(
            identities=(identity(email=""),), published_users={USER_ONE}
        )

        outcome = recompute.force_revoke(user_id=USER_ONE)

        self.assertEqual(outcome.reason, SKIP_ARCHIVED_IDENTITY_INCOMPLETE)


class RecomputeAndPublishSkipTests(unittest.TestCase):
    """``recompute_and_publish``：前置缺失时清楚跳过，不产生任何发布/撤权写入。"""

    def test_skips_when_roster_snapshot_is_missing(self) -> None:
        recompute, parts = build_recompute(identities=(identity(),), roster_rows=None)

        outcome = recompute.recompute_and_publish(user_id=USER_ONE)

        self.assertEqual(outcome.reason, SKIP_MISSING_ROSTER_SNAPSHOT)
        self.assertEqual(parts["decisions"].calls, [])

    def test_skips_when_there_is_no_current_galaxy_batch(self) -> None:
        audit = RecordingAudit()
        decisions = FakeDecisions()
        recompute = TargetedPermissionRecompute(
            identities=FakeIdentities(identity()),
            roster_snapshot=FakeRosterRows((roster_row(),)),
            galaxy=FakeGalaxy(None),
            decisions=decisions,
            publish_history=FakePublishHistory(),
            role_function_map=ROLE_FUNCTION_MAP,
            metric_translation_map=METRIC_TRANSLATION_MAP,
            audit=audit,
            clock=FixedClock(NOW),
        )

        outcome = recompute.recompute_and_publish(user_id=USER_ONE)

        self.assertEqual(outcome.reason, SKIP_NO_GALAXY_BATCH)
        self.assertEqual(decisions.calls, [])

    def test_skips_when_metric_translation_is_entirely_unavailable(self) -> None:
        recompute, parts = build_recompute(
            identities=(identity(),), metric_translation_map={}
        )

        outcome = recompute.recompute_and_publish(user_id=USER_ONE)

        self.assertEqual(outcome.reason, SKIP_METRIC_TRANSLATION_UNAVAILABLE)
        self.assertEqual(parts["decisions"].calls, [])

    def test_skips_when_the_account_is_not_in_the_active_baseline(self) -> None:
        recompute, _ = build_recompute(identities=())

        outcome = recompute.recompute_and_publish(user_id=USER_ONE)

        self.assertEqual(outcome.reason, SKIP_USER_NOT_ACTIVE)

    def test_skips_when_the_archive_has_no_personnel_id(self) -> None:
        recompute, _ = build_recompute(identities=(identity(personnel_id=""),))

        outcome = recompute.recompute_and_publish(user_id=USER_ONE)

        self.assertEqual(outcome.reason, SKIP_MISSING_PERSONNEL_ID)

    def test_skips_when_matching_fails_and_does_not_touch_the_publish_row(self) -> None:
        recompute, parts = build_recompute(
            identities=(identity(personnel_id="ou_unknown_person"),)
        )

        outcome = recompute.recompute_and_publish(user_id=USER_ONE)

        self.assertEqual(outcome.reason, SKIP_MATCH_FAILED)
        self.assertEqual(parts["decisions"].calls, [])


class RecomputeAndPublishGrantTests(unittest.TestCase):
    """匹配成功、银河判定有效授权的正常路径：产出正确的发布行，且不签发令牌、不带
    存量沿用。"""

    def test_enqueues_a_freshly_translated_publish_row(self) -> None:
        recompute, parts = build_recompute(identities=(identity(),))

        outcome = recompute.recompute_and_publish(user_id=USER_ONE)

        self.assertEqual(outcome.kind, RecomputeKind.ENQUEUED)
        [call] = parts["decisions"].calls
        self.assertEqual(json.loads(call["row"].permissions), {COMPANY_ID: [METRIC_NAME]})
        # token_cipher 恒为 None（模块文档「三处刻意不同」第 3 条）——即便这是一条
        # 首次发布，本模块也绝不读取或签发令牌密文。
        self.assertIsNone(call["row"].token_cipher)
        self.assertTrue(call["clear_delivered_content"])

    def test_local_grant_on_top_of_zero_galaxy_permission_still_publishes(self) -> None:
        """零银河权限 + 本地授权兜底（PM 2026-08-29 裁定）在定向重算里同样生效。"""

        override = LocalPermissionOverrideEntry(
            user_id=USER_ONE,
            direction=OverrideDirection.GRANT,
            company_id=COMPANY_ID,
            metric_name="人工特批指标",
            reason="特批",
            initiated_by_open_id="ou_admin",
            pending_action_id="pac_test0000000000000000001",
            created_at=NOW,
        )
        recompute, parts = build_recompute(
            identities=(identity(),),
            galaxy=galaxy_snapshot(roles=()),  # 该账号没有任何角色 → aggregate.granted=False
            local_overrides=FakeLocalOverrides({USER_ONE: (override,)}),
        )

        outcome = recompute.recompute_and_publish(user_id=USER_ONE)

        self.assertEqual(outcome.kind, RecomputeKind.ENQUEUED)
        [call] = parts["decisions"].calls
        self.assertIn("人工特批指标", call["row"].permissions)

    def test_wildcard_user_skips_local_override_and_audits_why(self) -> None:
        """通配用户（银河「后台管理员」）：本地覆盖整体不参与合并，审计明确说明
        原因（Issue #438「通配用户等跳过场景」）。"""

        from lingxi.core.permission.publish_row import ADMIN_FULL_ACCESS_FUNCTION

        override = LocalPermissionOverrideEntry(
            user_id=USER_ONE,
            direction=OverrideDirection.GRANT,
            company_id=COMPANY_ID,
            metric_name="不会生效的指标",
            reason="特批",
            initiated_by_open_id="ou_admin",
            pending_action_id="pac_test0000000000000000002",
            created_at=NOW,
        )
        recompute, parts = build_recompute(
            identities=(identity(),),
            galaxy=galaxy_snapshot(roles=(("g-1001", ADMIN_FULL_ACCESS_FUNCTION),)),
            role_function_map={ADMIN_FULL_ACCESS_FUNCTION: ADMIN_FULL_ACCESS_FUNCTION},
            metric_translation_map={"*": {ADMIN_FULL_ACCESS_FUNCTION: ("全量指标",)}},
            local_overrides=FakeLocalOverrides({USER_ONE: (override,)}),
        )

        outcome = recompute.recompute_and_publish(user_id=USER_ONE)

        self.assertEqual(outcome.kind, RecomputeKind.ENQUEUED)
        [call] = parts["decisions"].calls
        self.assertNotIn("不会生效的指标", call["row"].permissions)
        skip_fields = parts["audit"].fields_for("permission_targeted_recompute.local_override_skipped")
        self.assertTrue(skip_fields)


class ForceRevokeVsRecomputeDoNotConflateTest(unittest.TestCase):
    """变异验红锚点：``force_revoke`` 绝不能落进合并管线重新授予权限——如果把
    ``force_revoke`` 误改成调用 ``recompute_and_publish``，一个银河仍然有效的
    用户会在"停用"时被重新授予权限，这条用例会因为断言 ``permissions == "{}"``
    直接变红。"""

    def test_force_revoke_ignores_galaxy_even_when_it_would_grant(self) -> None:
        recompute, parts = build_recompute(
            identities=(identity(),),
            published_users={USER_ONE},
        )

        outcome = recompute.force_revoke(user_id=USER_ONE)

        self.assertEqual(outcome.kind, RecomputeKind.REVOKED)
        [call] = parts["decisions"].calls
        self.assertEqual(call["row"].permissions, "{}")


if __name__ == "__main__":
    unittest.main()
