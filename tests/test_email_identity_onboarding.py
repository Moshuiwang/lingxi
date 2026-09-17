"""合成读取口经真实系统开通链验证身份选择和旧凭据防线。"""

import unittest
from dataclasses import replace
from types import SimpleNamespace

from test_onboarding_runner import (
    EMPLOYED,
    FROZEN,
    INITIATED_BY,
    MEMBER,
    OPEN_ID,
    ROSTER_ROWS,
    FakeDirectory,
    FakeRoster,
    FakeStockTokens,
    build_runner,
)

from lingxi.core.conversation.ports import OnboardingState
from lingxi.core.identity.onboarding_ports import EmailBinding
from lingxi.core.identity.org_snapshot import DirectoryAvailability
from lingxi.core.identity.preprovision import PreprovisionTarget
from lingxi.core.identity.stock_token_source import ADOPTABLE, StockTokenLookup

OLD = replace(MEMBER, member_key="old-member", user_id="old-person", open_id="old-open")
ROWS = ({**ROSTER_ROWS[0], "personnel_id": OLD.user_id, "employee_no": "old-job"}, *ROSTER_ROWS)


class Employment:
    def __init__(self, old=FROZEN):
        self.values = {OPEN_ID: EMPLOYED, OLD.open_id: old}
        self.calls = []

    def status(self, *, tenant_key, open_id):
        self.calls.append((tenant_key, open_id))
        value = self.values[open_id]
        if isinstance(value, Exception):
            raise value
        return value


class EmailIdentityOnboardingTests(unittest.TestCase):
    def test_first_chat_does_not_refresh_a_known_old_job_binding(self):
        bindings = SimpleNamespace(
            bindings_for_email=lambda email: (
                EmailBinding("existing-user", OPEN_ID, MEMBER.user_id, "old-job"),
            )
        )
        runner, parts = build_runner(email_bindings=bindings)
        runner.start(event_id="trusted-event", open_id=OPEN_ID, trace_id="trusted-test")
        self.assertEqual(
            parts["audit"].facts("onboarding.result")["failure_reason"], "identity_binding_mismatch"
        )
        self.assertEqual(parts["provisioning"].requests, [])
        self.assert_no_grant(parts)

    def test_daily_recompute_keeps_archived_personnel_despite_another_roster_email_candidate(self):
        import test_permission_refresh_duty as refresh

        duty, parts = refresh.build_duty(
            identities=(refresh.identity(),),
            roster_rows=(
                refresh.roster_row(),
                refresh.roster_row("another-person", employee_no="another-job"),
            ),
        )
        report = duty.run_once()
        self.assertEqual((report.enqueued, report.revoked), (1, 0))
        self.assertEqual(parts["decisions"].calls[0]["user_id"], refresh.USER_ONE)
        self.assertEqual(parts["decisions"].calls[0]["row"].token_cipher, refresh.TOKEN_CIPHER)

    def build(
        self,
        *,
        old=FROZEN,
        rows=ROWS,
        members=(OLD, MEMBER),
        availability=DirectoryAvailability.AVAILABLE,
        **options,
    ):
        return build_runner(
            directory=FakeDirectory(availability=availability, members=members),
            roster=FakeRoster(rows),
            employment=Employment(old),
            **options,
        )

    def execute(self, *, expected=None, **options):
        runner, parts = self.build(**options)
        result = runner.start_system(
            email=ROSTER_ROWS[0]["email"],
            trace_id="email-test",
            initiated_by_open_id=INITIATED_BY,
            expected_open_id=expected,
        )
        return parts, result

    def assert_no_grant(self, parts):
        self.assertEqual(parts["tokens"].calls, [])
        self.assertEqual(parts["tokens"].adopt_calls, [])
        self.assertEqual(parts["decisions"].reasons, [])
        self.assertEqual(parts["environment"].calls, [])

    def test_active_and_departed_uses_live_status_and_opens_active_member(self):
        parts, result = self.execute()
        self.assertIs(result.state, OnboardingState.COMPLETED)
        self.assertEqual(parts["provisioning"].requests[0].identity.feishu_open_id, OPEN_ID)
        self.assertEqual({oid for _, oid in parts["employment"].calls}, {OPEN_ID, OLD.open_id})
        facts = parts["audit"].facts("identity.email_resolved")
        self.assertEqual((facts["candidate_count"], facts["active_candidate_count"]), (2, 1))
        self.assertEqual(facts["selected_personnel_id"], MEMBER.user_id)
        self.assertEqual(facts["snapshot_version"], "synthetic-roster")

    def test_old_personnel_absent_from_a_fresh_directory_opens_the_active_member(self):
        """旧人员 ID 已不在可用且新鲜的组织快照：明确非在职，不再让整条邮箱永远未决。"""

        parts, result = self.execute(members=(MEMBER,))
        self.assertIs(result.state, OnboardingState.COMPLETED)
        self.assertEqual(parts["provisioning"].requests[0].identity.feishu_open_id, OPEN_ID)
        self.assertEqual(set(parts["directory"].user_id_calls), {OLD.user_id, MEMBER.user_id})
        # 不在快照的候选没有可回读的主体：旧账号的实时状态一次都不会被读。
        self.assertEqual({oid for _, oid in parts["employment"].calls}, {OPEN_ID})
        facts = parts["audit"].facts("identity.email_resolved")
        self.assertEqual(
            (
                facts["candidate_count"],
                facts["active_candidate_count"],
                facts["absent_candidate_count"],
            ),
            (2, 1, 1),
        )
        self.assertEqual(facts["selected_personnel_id"], MEMBER.user_id)

    def test_execution_stage_resolution_selects_the_member_still_in_the_directory(self):
        """扩员执行阶段与预开通共用同一解析入口：(不在快照, 在职) 也走到选中主体。"""

        runner, parts = self.build(members=(MEMBER,))
        target = runner.resolve_system_email(email=ROSTER_ROWS[0]["email"], trace_id="expand-test")
        self.assertIsInstance(target, PreprovisionTarget)
        self.assertIs(target.member, MEMBER)
        self.assertEqual(target.identity.absent_candidate_count, 1)
        facts = parts["audit"].facts("identity.email_resolved")
        self.assertEqual(facts["identity_state"], "unique_active")

    def test_zero_members_from_a_stale_or_unavailable_directory_stays_undecided(self):
        """快照过期或不可用时"查不到"不是事实：两位候选都保持未知，不许折成离职。"""

        for availability in (DirectoryAvailability.STALE, DirectoryAvailability.UNAVAILABLE):
            with self.subTest(availability=availability.value):
                parts, result = self.execute(members=(MEMBER,), availability=availability)
                self.assertIs(result.state, OnboardingState.INTERNAL_ERROR)
                self.assertEqual(result.failure_reason, "email_identity_unavailable")
                facts = parts["audit"].facts("identity.email_resolved")
                self.assertEqual(facts["identity_reason"], "employment_unknown")
                self.assertIsNone(facts["absent_candidate_count"])
                self.assertEqual(parts["provisioning"].requests, [])
                self.assert_no_grant(parts)

    def test_a_lone_candidate_absent_from_a_fresh_directory_is_refused_not_errored(self):
        parts, result = self.execute(rows=ROSTER_ROWS, members=())
        self.assertIs(result.state, OnboardingState.NOT_AUTHORIZED)
        self.assertEqual(result.failure_reason, "email_identity_inactive")
        facts = parts["audit"].facts("identity.email_resolved")
        self.assertEqual(facts["absent_candidate_count"], 1)
        self.assertEqual(parts["provisioning"].requests, [])
        self.assert_no_grant(parts)

    def test_two_active_never_opens_either_member(self):
        parts, result = self.execute(old=EMPLOYED)
        self.assertEqual(result.failure_reason, "email_identity_active_conflict")
        self.assertEqual(parts["provisioning"].requests, [])
        self.assert_no_grant(parts)

    def test_unknown_and_failed_reads_cannot_remove_a_competing_candidate(self):
        for old in (None, RuntimeError("synthetic read failure")):
            with self.subTest(old=type(old).__name__):
                parts, result = self.execute(old=old)
                self.assertIs(result.state, OnboardingState.INTERNAL_ERROR)
                self.assertEqual(result.failure_reason, "email_identity_unavailable")
                self.assertEqual(parts["provisioning"].requests, [])
                self.assert_no_grant(parts)

    def test_previously_confirmed_open_id_cannot_drift_at_execution(self):
        parts, result = self.execute(expected=OLD.open_id)
        self.assertEqual(result.failure_reason, "email_identity_binding_mismatch")
        self.assertEqual(parts["provisioning"].requests, [])
        self.assert_no_grant(parts)

    def test_same_open_id_with_old_personnel_or_job_is_not_rebound(self):
        for personnel, employee in ((OLD.user_id, None), (MEMBER.user_id, "old-job")):
            bindings = SimpleNamespace(
                bindings_for_email=lambda email: (
                    EmailBinding("existing-user", OPEN_ID, personnel, employee),
                )
            )
            with self.subTest(personnel=personnel, employee=employee):
                parts, result = self.execute(email_bindings=bindings)
                self.assertEqual(result.failure_reason, "email_identity_unavailable")
                self.assertEqual(
                    parts["audit"].facts("identity.email_resolved")["identity_reason"],
                    "binding_mismatch",
                )
                self.assertEqual(parts["provisioning"].requests, [])
                self.assert_no_grant(parts)

    def test_email_reuse_does_not_adopt_old_token_or_import_old_local_grants(self):
        stock = FakeStockTokens(
            StockTokenLookup(
                ADOPTABLE, secret="synthetic-old-secret", permissions='{"88":["旧表指标"]}'
            )
        )
        parts, result = self.execute(stock_tokens=stock)
        self.assertEqual(result.failure_reason, "stock_token_identity_unresolved")
        self.assertEqual(parts["legacy_importer"].calls, [])
        self.assert_no_grant(parts)

    def test_a_current_unique_person_still_cannot_claim_an_unattributed_old_row(self):
        """旧人员已不在快照也不能让当前唯一候选继承同邮箱旧令牌或权限。"""

        stock = FakeStockTokens(
            StockTokenLookup(
                ADOPTABLE, secret="synthetic-old-secret", permissions='{"88":["旧表指标"]}'
            )
        )
        for entry in ("first_chat", "preprovision"):
            with self.subTest(entry=entry):
                runner, parts = build_runner(roster=FakeRoster(ROSTER_ROWS), stock_tokens=stock)
                if entry == "first_chat":
                    runner.start(event_id="trusted-event", open_id=OPEN_ID, trace_id="trusted-test")
                    reason = parts["audit"].facts("onboarding.result")["failure_reason"]
                else:
                    result = runner.start_system(
                        email=ROSTER_ROWS[0]["email"],
                        trace_id="preprovision-test",
                        initiated_by_open_id=INITIATED_BY,
                    )
                    reason = result.failure_reason
                self.assertEqual(reason, "stock_token_identity_unresolved")
                self.assertEqual(parts["legacy_importer"].calls, [])
                self.assert_no_grant(parts)

    def test_trusted_primary_key_path_does_not_select_another_email_identity(self):
        runner, parts = build_runner(roster=FakeRoster(ROWS))
        runner.start(event_id="trusted-event", open_id=OPEN_ID, trace_id="trusted-test")
        self.assertEqual(parts["directory"].user_id_calls, [])
        self.assertEqual(parts["provisioning"].requests[0].identity.feishu_user_id, MEMBER.user_id)

    def test_primary_key_entry_also_refuses_legacy_token_for_reused_email(self):
        stock = FakeStockTokens(StockTokenLookup(ADOPTABLE, secret="synthetic-old-secret"))
        runner, parts = build_runner(roster=FakeRoster(ROWS), stock_tokens=stock)
        runner.start(event_id="trusted-event", open_id=OPEN_ID, trace_id="trusted-test")
        self.assertEqual(
            parts["audit"].facts("onboarding.result")["failure_reason"],
            "stock_token_identity_unresolved",
        )
        self.assertEqual(parts["legacy_importer"].calls, [])
        self.assert_no_grant(parts)
