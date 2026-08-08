"""首聊定位与建档判定（纯逻辑，无数据库、无网络）。

认领断言：
- V-开通-01：建档草稿的 ``permission_record_id`` 恒为 ``None``，不先占位再回填；
- V-开通-06：定位不到或资料不完整时不产出任何草稿，统一按无可用权限结束；
- V-开通-07：飞书成员详情显示非在职时不建档，且 ``status`` 不进入草稿；
- V-开通-08：姓名不含汉字时按既有规则正常判定，不走语言分支、不静默丢弃；
- V-身份-02：专用授权用户自身不会被建成 ``app_user``；
- V-身份-04：专用授权失效（组织资料不可用）时给出可理解终态且不写半条资料。

#16 正文五条硬约束在本文件各有一条对应用例，见各测试的中文注释。
"""

from __future__ import annotations

import unittest

from lingxi.core.identity.first_contact import (
    EmploymentStatus,
    FailureReason,
    FirstContactOutcome,
    LocationOutcome,
    decide_first_contact,
    locate_by_open_id,
)
from lingxi.core.identity.identifiers import redact_identifier
from lingxi.core.identity.org_snapshot import DirectoryAvailability, SnapshotMember


DELEGATED_SUBJECT = "ou_delegated_authorization_subject"


def member(
    *,
    tenant_key: str = "tenant_a",
    member_key: str = "ou_zhang",
    open_id: str = "ou_zhang",
    user_id: str = "user_zhang",
    union_id: str = "union_zhang",
    display_name: str = "张一",
    display_name_locale: str | None = "zh-CN",
    department_names: tuple[str, ...] = ("测试部门",),
) -> SnapshotMember:
    return SnapshotMember(
        tenant_key=tenant_key,
        member_key=member_key,
        open_id=open_id,
        user_id=user_id,
        union_id=union_id,
        display_name=display_name,
        display_name_locale=display_name_locale,
        department_names=department_names,
    )


def employed() -> EmploymentStatus:
    return EmploymentStatus(is_activated=True, is_exited=False, is_frozen=False, is_resigned=False, is_unjoin=False)


def decide(**overrides: object):
    arguments: dict[str, object] = {
        "open_id": "ou_zhang",
        "location": locate_by_open_id("ou_zhang", (member(),)),
        "employment": employed(),
        "directory": DirectoryAvailability.AVAILABLE,
        "delegated_subject_open_id": DELEGATED_SUBJECT,
    }
    arguments.update(overrides)
    return decide_first_contact(**arguments)  # type: ignore[arg-type]


class LocateTest(unittest.TestCase):
    def test_an_exact_open_id_match_locates_the_member(self) -> None:
        result = locate_by_open_id("ou_zhang", (member(), member(member_key="ou_li", open_id="ou_li", user_id="user_li", union_id="union_li", display_name="李四")))

        self.assertIs(result.outcome, LocationOutcome.LOCATED)
        self.assertIsNotNone(result.member)
        assert result.member is not None
        self.assertEqual(result.member.user_id, "user_zhang")
        self.assertEqual(result.candidate_count, 1)

    def test_an_unknown_open_id_is_not_found_and_never_falls_back_to_a_name(self) -> None:
        result = locate_by_open_id("ou_absent", (member(display_name="张一"),))

        self.assertIs(result.outcome, LocationOutcome.NOT_FOUND)
        self.assertIsNone(result.member)
        self.assertEqual(result.candidate_count, 0)

    def test_two_members_sharing_one_open_id_are_ambiguous_rather_than_arbitrarily_picked(self) -> None:
        duplicate = member(member_key="ou_zhang_second", user_id="user_other", union_id="union_other")

        result = locate_by_open_id("ou_zhang", (member(), duplicate))

        self.assertIs(result.outcome, LocationOutcome.AMBIGUOUS)
        self.assertIsNone(result.member)
        self.assertEqual(result.candidate_count, 2)

    def test_identifier_prefixes_are_never_used_for_matching(self) -> None:
        """硬约束 5：``open_user_id`` 的 6 位前缀在 710 人中有 57 组碰撞，只可用于日志脱敏。"""
        stored = member(member_key="ou_abc_first_person", open_id="ou_abc_first_person")
        incoming = "ou_abc_third_person"

        result = locate_by_open_id(incoming, (stored,))

        self.assertEqual(redact_identifier(stored.open_id), redact_identifier(incoming))
        self.assertIs(result.outcome, LocationOutcome.NOT_FOUND)

    def test_matching_ignores_surrounding_whitespace_but_not_case_or_substrings(self) -> None:
        result_padded = locate_by_open_id("  ou_zhang  ", (member(),))
        result_prefix = locate_by_open_id("ou_zha", (member(),))
        result_case = locate_by_open_id("OU_ZHANG", (member(),))

        self.assertIs(result_padded.outcome, LocationOutcome.LOCATED)
        self.assertIs(result_prefix.outcome, LocationOutcome.NOT_FOUND)
        self.assertIs(result_case.outcome, LocationOutcome.NOT_FOUND)

    def test_an_empty_open_id_is_not_found_and_never_matches_a_blank_snapshot_row(self) -> None:
        self.assertIs(locate_by_open_id("", (member(),)).outcome, LocationOutcome.NOT_FOUND)
        self.assertIs(locate_by_open_id("   ", (member(),)).outcome, LocationOutcome.NOT_FOUND)


class EmploymentStatusTest(unittest.TestCase):
    """硬约束 2：可见范围不做在职过滤，定位到不等于在职。"""

    def test_only_an_activated_member_without_any_negative_flag_counts_as_employed(self) -> None:
        self.assertTrue(employed().employed)

    def test_each_negative_flag_alone_makes_the_member_not_employed(self) -> None:
        for flag in ("is_exited", "is_frozen", "is_resigned", "is_unjoin"):
            with self.subTest(flag=flag):
                status = EmploymentStatus(
                    is_activated=True,
                    is_exited=flag == "is_exited",
                    is_frozen=flag == "is_frozen",
                    is_resigned=flag == "is_resigned",
                    is_unjoin=flag == "is_unjoin",
                )
                self.assertFalse(status.employed)

    def test_a_member_that_is_not_activated_is_not_employed(self) -> None:
        status = EmploymentStatus(is_activated=False, is_exited=False, is_frozen=False, is_resigned=False, is_unjoin=False)

        self.assertFalse(status.employed)

    def test_a_payload_missing_any_flag_is_undecidable_rather_than_assumed_employed(self) -> None:
        full = {"is_activated": True, "is_exited": False, "is_frozen": False, "is_resigned": False, "is_unjoin": False}

        self.assertIsNotNone(EmploymentStatus.from_feishu(full))
        for missing in full:
            with self.subTest(missing=missing):
                payload = {key: value for key, value in full.items() if key != missing}
                self.assertIsNone(EmploymentStatus.from_feishu(payload))

    def test_non_boolean_flags_are_undecidable(self) -> None:
        payload = {"is_activated": "true", "is_exited": False, "is_frozen": False, "is_resigned": False, "is_unjoin": False}

        self.assertIsNone(EmploymentStatus.from_feishu(payload))

    def test_from_feishu_rejects_a_non_mapping(self) -> None:
        self.assertIsNone(EmploymentStatus.from_feishu(None))
        self.assertIsNone(EmploymentStatus.from_feishu([]))


class DecideFirstContactTest(unittest.TestCase):
    def test_a_located_employed_member_produces_a_draft_without_a_permission_record(self) -> None:
        decision = decide()

        self.assertIs(decision.outcome, FirstContactOutcome.RECORD_READY)
        self.assertTrue(decision.creates_record)
        assert decision.draft is not None
        self.assertIsNone(decision.draft.permission_record_id)  # V-开通-01
        self.assertEqual(decision.draft.feishu_open_id, "ou_zhang")
        self.assertEqual(decision.draft.feishu_user_id, "user_zhang")
        self.assertEqual(decision.draft.feishu_union_id, "union_zhang")
        self.assertEqual(decision.draft.display_name, "张一")
        self.assertEqual(decision.draft.department, "测试部门")
        self.assertEqual(decision.draft.tenant_key, "tenant_a")
        self.assertEqual(decision.draft.provisioning_state, "matching")
        self.assertEqual(decision.content_key, "onboarding.matched")
        self.assertTrue(decision.content_version)

    def test_the_draft_carries_no_employment_status_field_at_all(self) -> None:
        """硬约束 2：``status`` 只用于当次拦截，存下来立刻产生陈旧窗口。"""
        decision = decide()
        assert decision.draft is not None

        names = tuple(vars(decision.draft))
        for forbidden in ("status", "is_activated", "is_exited", "is_frozen", "is_resigned", "is_unjoin", "employment"):
            with self.subTest(field=forbidden):
                self.assertNotIn(forbidden, names)

    def test_a_member_that_is_not_employed_is_refused_without_any_draft(self) -> None:
        not_employed = EmploymentStatus(is_activated=True, is_exited=False, is_frozen=True, is_resigned=False, is_unjoin=False)

        decision = decide(employment=not_employed)

        self.assertIs(decision.outcome, FirstContactOutcome.NOT_AUTHORIZED)
        self.assertIs(decision.failure_reason, FailureReason.NOT_EMPLOYED)
        self.assertIsNone(decision.draft)
        self.assertFalse(decision.creates_record)

    def test_an_undecidable_employment_status_is_not_authorized_without_a_draft(self) -> None:
        decision = decide(employment=None)

        self.assertIs(decision.outcome, FirstContactOutcome.NOT_AUTHORIZED)
        self.assertIs(decision.failure_reason, FailureReason.EMPLOYMENT_UNKNOWN)
        self.assertIsNone(decision.draft)

    def test_a_member_that_cannot_be_located_is_not_authorized_without_a_draft(self) -> None:
        decision = decide(open_id="ou_absent", location=locate_by_open_id("ou_absent", (member(),)))

        self.assertIs(decision.outcome, FirstContactOutcome.NOT_AUTHORIZED)
        self.assertIs(decision.failure_reason, FailureReason.NOT_LOCATED)
        self.assertEqual(
            decision.message,
            "当前没有可用的银河权限，请先在银河申请或补充权限。"
            "银河权限生效并完成同步后，请再回到 Lingxi 使用。"
            "Lingxi 不能代替你申请或扩大银河权限。"
            "如果你在银河已经有权限但仍看到此提示，请联系银河管理员。",
        )
        self.assertNotIn("人工核对", decision.message)
        self.assertIsNone(decision.draft)

    def test_an_ambiguous_match_is_not_authorized_without_picking_a_candidate(self) -> None:
        duplicate = member(member_key="ou_zhang_second", user_id="user_other", union_id="union_other")

        decision = decide(location=locate_by_open_id("ou_zhang", (member(), duplicate)))

        self.assertIs(decision.outcome, FirstContactOutcome.NOT_AUTHORIZED)
        self.assertIs(decision.failure_reason, FailureReason.AMBIGUOUS_IDENTITY)
        self.assertIsNone(decision.draft)

    def test_an_incomplete_profile_is_not_authorized_without_a_partial_draft(self) -> None:
        """V-开通-06：必要资料缺失时不写入不完整的用户记录。"""
        for field in ("user_id", "union_id", "display_name", "tenant_key"):
            with self.subTest(field=field):
                incomplete = member(**{field: "   "})  # type: ignore[arg-type]
                decision = decide(location=locate_by_open_id("ou_zhang", (incomplete,)))

                self.assertIs(decision.outcome, FirstContactOutcome.NOT_AUTHORIZED)
                self.assertIs(decision.failure_reason, FailureReason.INCOMPLETE_PROFILE)
                self.assertIsNone(decision.draft)

    def test_the_delegated_authorization_subject_is_never_recorded_as_a_user(self) -> None:
        """V-身份-02。"""
        subject = member(member_key=DELEGATED_SUBJECT, open_id=DELEGATED_SUBJECT, user_id="user_delegated", union_id="union_delegated", display_name="专用授权账号")

        decision = decide(open_id=DELEGATED_SUBJECT, location=locate_by_open_id(DELEGATED_SUBJECT, (subject,)))

        self.assertIs(decision.outcome, FirstContactOutcome.DELEGATED_SUBJECT_IGNORED)
        self.assertIsNone(decision.draft)
        self.assertFalse(decision.creates_record)

    def test_the_delegated_subject_is_refused_even_when_everything_else_would_pass(self) -> None:
        subject = member(member_key=DELEGATED_SUBJECT, open_id=DELEGATED_SUBJECT, user_id="user_delegated", union_id="union_delegated")

        decision = decide(
            open_id=f"  {DELEGATED_SUBJECT}  ",
            location=locate_by_open_id(DELEGATED_SUBJECT, (subject,)),
        )

        self.assertIs(decision.outcome, FirstContactOutcome.DELEGATED_SUBJECT_IGNORED)

    def test_a_lookalike_of_the_delegated_subject_is_still_an_ordinary_employee(self) -> None:
        # 前缀不用于比对：只有完全相同的 open_id 才是专用授权账号本身。
        lookalike = member(member_key="ou_delegated_authorization_subject_2", open_id="ou_delegated_authorization_subject_2")

        decision = decide(open_id=lookalike.open_id, location=locate_by_open_id(lookalike.open_id, (lookalike,)))

        self.assertIs(decision.outcome, FirstContactOutcome.RECORD_READY)

    def test_an_unavailable_or_stale_directory_gives_a_terminal_state_without_a_draft(self) -> None:
        """V-身份-04：专用授权失效后组织资料不可用，员工侧得到终态，不写半条资料。"""
        for availability in (DirectoryAvailability.UNAVAILABLE, DirectoryAvailability.STALE):
            with self.subTest(availability=availability):
                decision = decide(directory=availability)

                self.assertIs(decision.outcome, FirstContactOutcome.DIRECTORY_UNAVAILABLE)
                self.assertIsNone(decision.draft)
                self.assertFalse(decision.creates_record)
                self.assertEqual(
                    decision.message,
                    "当前暂时无法完成开通，已转交管理员处理，请不要重复发送。"
                    "处理完成后我们会通知你。错误码：LX-ONBOARD-001。",
                )

    def test_the_delegated_subject_is_refused_even_when_the_directory_is_unavailable(self) -> None:
        decision = decide(open_id=DELEGATED_SUBJECT, directory=DirectoryAvailability.UNAVAILABLE, location=locate_by_open_id(DELEGATED_SUBJECT, ()))

        self.assertIs(decision.outcome, FirstContactOutcome.DELEGATED_SUBJECT_IGNORED)


class NameAndOptionalFieldTest(unittest.TestCase):
    def test_latin_and_mixed_names_are_recorded_exactly_as_returned(self) -> None:
        """硬约束 3 / V-开通-08：姓名不假定中文。"""
        cases = (("Alice Smith", "en-US"), ("Anna 李", "zh-CN"), ("O'Brien-Núñez", None), ("张三", "zh-CN"))
        for name, locale in cases:
            with self.subTest(name=name):
                located = member(display_name=name, display_name_locale=locale)
                decision = decide(location=locate_by_open_id("ou_zhang", (located,)))

                self.assertIs(decision.outcome, FirstContactOutcome.RECORD_READY)
                assert decision.draft is not None
                self.assertEqual(decision.draft.display_name, name)
                self.assertEqual(decision.draft.display_name_locale, locale)

    def test_two_members_with_the_same_name_are_told_apart_by_open_id(self) -> None:
        """硬约束 3：姓名不是唯一键；710 人实测含 1 对重名。"""
        first = member(member_key="ou_first", open_id="ou_first", user_id="user_first", union_id="union_first", display_name="张三")
        second = member(member_key="ou_second", open_id="ou_second", user_id="user_second", union_id="union_second", display_name="张三")

        decision = decide(open_id="ou_second", location=locate_by_open_id("ou_second", (first, second)))

        assert decision.draft is not None
        self.assertEqual(decision.draft.feishu_user_id, "user_second")

    def test_a_member_without_locale_still_gets_recorded(self) -> None:
        """硬约束 4：locale 等可选增强字段不作为建档前提。"""
        thin = member(display_name_locale=None)

        decision = decide(location=locate_by_open_id("ou_zhang", (thin,)))

        self.assertIs(decision.outcome, FirstContactOutcome.RECORD_READY)
        assert decision.draft is not None
        self.assertIsNone(decision.draft.display_name_locale)

    def test_a_member_without_department_is_not_authorized(self) -> None:
        """产品合同把部门列进必要资料；缺失时不写半条记录，也不建人工待办。
        此前部门被当可选字段放行会建出半份档案（Codex 复查发现）。"""
        for department_names in ((), ("",), ("   ",)):
            with self.subTest(department_names=department_names):
                thin = member(department_names=department_names)
                decision = decide(location=locate_by_open_id("ou_zhang", (thin,)))
                self.assertIs(decision.outcome, FirstContactOutcome.NOT_AUTHORIZED)
                self.assertIs(decision.failure_reason, FailureReason.INCOMPLETE_PROFILE)
                self.assertIsNone(decision.draft)

    def test_the_draft_has_no_field_for_optional_enhancements(self) -> None:
        """`mobile` / `job_title` / `leader_id` 仍不入保存范围（硬约束 4）。

        工号与邮箱不再在禁止之列：《2026-08-05 花名册身份链与工号邮箱匹配》决策
        把它们纳入统一用户记录的保存范围（匹配银河的主/辅键），由花名册读取
        步骤填充；快照定位产出的 draft 中它们必须保持 None，不得从组织资料
        杜撰。"""
        decision = decide()
        assert decision.draft is not None

        for forbidden in ("mobile", "job_title", "leader_id"):
            with self.subTest(field=forbidden):
                self.assertNotIn(forbidden, vars(decision.draft))

        self.assertIsNone(decision.draft.employee_no)
        self.assertIsNone(decision.draft.email)


class TenantIsAResultNotAConfigurationTest(unittest.TestCase):
    """硬约束 1：不设目标租户配置，租户是查出来的结果。"""

    def test_decide_first_contact_takes_no_tenant_configuration(self) -> None:
        import inspect

        parameters = set(inspect.signature(decide_first_contact).parameters)

        self.assertNotIn("tenant_key", parameters)
        self.assertNotIn("target_tenant_key", parameters)

    def test_the_tenant_comes_from_the_located_member(self) -> None:
        located = member(tenant_key="tenant_discovered")

        decision = decide(location=locate_by_open_id("ou_zhang", (located,)))

        assert decision.draft is not None
        self.assertEqual(decision.draft.tenant_key, "tenant_discovered")


class UserFacingMessageTest(unittest.TestCase):
    """错误信封约定：面向用户的文案不含内部标识、堆栈或表名。"""

    def _all_decisions(self):
        duplicate = member(member_key="ou_zhang_second", user_id="user_other", union_id="union_other")
        frozen = EmploymentStatus(is_activated=True, is_exited=False, is_frozen=True, is_resigned=False, is_unjoin=False)
        subject = member(member_key=DELEGATED_SUBJECT, open_id=DELEGATED_SUBJECT, user_id="user_delegated", union_id="union_delegated")
        return (
            decide(),
            decide(employment=frozen),
            decide(employment=None),
            decide(open_id="ou_absent", location=locate_by_open_id("ou_absent", (member(),))),
            decide(location=locate_by_open_id("ou_zhang", (member(), duplicate))),
            decide(location=locate_by_open_id("ou_zhang", (member(user_id="  "),))),
            decide(open_id=DELEGATED_SUBJECT, location=locate_by_open_id(DELEGATED_SUBJECT, (subject,))),
            decide(directory=DirectoryAvailability.UNAVAILABLE),
        )

    def test_every_outcome_has_a_non_empty_chinese_message(self) -> None:
        seen = set()
        for decision in self._all_decisions():
            with self.subTest(outcome=decision.outcome):
                self.assertTrue(decision.message.strip())
                self.assertTrue(any("一" <= character <= "鿿" for character in decision.message))
            seen.add(decision.outcome)

        self.assertEqual(seen, set(FirstContactOutcome))

    def test_no_message_leaks_an_internal_identifier_or_table_name(self) -> None:
        leaks = ("ou_zhang", "user_zhang", "union_zhang", "tenant_a", DELEGATED_SUBJECT, "app_user", "feishu_org_member_snapshot", "Traceback")
        for decision in self._all_decisions():
            for leak in leaks:
                with self.subTest(outcome=decision.outcome, leak=leak):
                    self.assertNotIn(leak, decision.message)


if __name__ == "__main__":
    unittest.main()
