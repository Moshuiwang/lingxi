"""V-银河-09 / V-银河-10：工号（主）/ 邮箱（辅）匹配与 nick_name 不参与判定。

对应《2026-08-05 花名册身份链与工号邮箱匹配》决策，是门禁 V-开通-02/03/06/09
的纯函数层用例；全部数据为虚构合成值。
"""

from __future__ import annotations

import unittest

from lingxi.core.permission.account_match import (
    MATCHED,
    NOT_FOUND,
    match_galaxy_account,
)


def roster_row(
    personnel_id: str = "ou_p1",
    employee_no: str = "80001",
    email: str = "he.xugong@example-corp.invalid",
    name: str = "何虚工",
) -> dict[str, object]:
    return {"personnel_id": personnel_id, "employee_no": employee_no, "email": email, "name": name}


def galaxy_row(
    user_id: str = "U-1",
    user_name: str = "80001",
    email: str = "he.xugong@example-corp.invalid",
    nick_name: str = "何虚工",
) -> dict[str, object]:
    return {"user_id": user_id, "user_name": user_name, "email": email, "nick_name": nick_name}


class GalaxyAccountMatchedTest(unittest.TestCase):
    """工号唯一命中即采用；工号缺失或未命中时邮箱回退。"""

    def test_unique_employee_no_hit_is_matched(self) -> None:
        rows = [galaxy_row(), galaxy_row(user_id="U-2", user_name="80002", email="other@example-corp.invalid")]
        result = match_galaxy_account("ou_p1", [roster_row()], rows)
        self.assertEqual(result.state, MATCHED)
        self.assertEqual(result.reason, "unique_employee_no_match")
        self.assertEqual(result.matched_key, "employee_no")
        self.assertEqual(result.galaxy_user_id, "U-1")

    def test_both_keys_agreeing_on_one_account_is_matched(self) -> None:
        result = match_galaxy_account("ou_p1", [roster_row()], [galaxy_row()])
        self.assertEqual((result.state, result.matched_key), (MATCHED, "employee_no"))

    def test_missing_employee_no_falls_back_to_unique_email(self) -> None:
        # V-开通-09：工号缺失但邮箱可用时按合同走邮箱回退。
        result = match_galaxy_account("ou_p1", [roster_row(employee_no="")], [galaxy_row(user_name="99999")])
        self.assertEqual((result.state, result.reason), (MATCHED, "unique_email_match"))
        self.assertEqual(result.matched_key, "email")

    def test_employee_no_miss_falls_back_to_unique_email(self) -> None:
        # 决策记录：「工号缺失**或未命中**时以邮箱匹配」。
        result = match_galaxy_account("ou_p1", [roster_row(employee_no="70000")], [galaxy_row(user_name="80001")])
        self.assertEqual((result.state, result.reason), (MATCHED, "unique_email_match"))

    def test_email_is_normalized_but_employee_no_is_exact(self) -> None:
        result = match_galaxy_account(
            "ou_p1",
            [roster_row(employee_no="", email="  HE.XUGONG@Example-Corp.invalid ")],
            [galaxy_row(user_name="x")],
        )
        self.assertEqual(result.state, MATCHED)
        # 工号是字符串精确比较：0012 与 12 是两个账号，不做数值化。
        result = match_galaxy_account(
            "ou_p1", [roster_row(employee_no="0012", email="")], [galaxy_row(user_name="12", email="")]
        )
        self.assertEqual(result.state, NOT_FOUND)

class GalaxyAccountNotAuthorizedTest(unittest.TestCase):
    """任何非唯一成功都统一走无可用银河权限出口，同时保留内部原因。"""

    def test_person_absent_from_roster_is_not_authorized(self) -> None:
        # V-开通-06：花名册查无对应记录 → 不建档、不建待办。
        result = match_galaxy_account("ou_absent", [roster_row()], [galaxy_row()])
        self.assertEqual((result.state, result.reason), (NOT_FOUND, "roster_not_found"))

    def test_duplicate_roster_rows_with_identical_keys_are_not_authorized(self) -> None:
        result = match_galaxy_account("ou_p1", [roster_row(), roster_row()], [galaxy_row()])
        self.assertEqual((result.state, result.reason), (NOT_FOUND, "roster_multiple_rows"))

    def test_duplicate_roster_rows_with_conflicting_keys_are_not_authorized(self) -> None:
        rows = [roster_row(), roster_row(employee_no="80002")]
        result = match_galaxy_account("ou_p1", rows, [galaxy_row()])
        self.assertEqual((result.state, result.reason), (NOT_FOUND, "roster_multiple_rows"))

    def test_missing_both_keys_is_not_authorized(self) -> None:
        # V-开通-06：工号与邮箱等必要资料均缺失。
        result = match_galaxy_account("ou_p1", [roster_row(employee_no="", email="")], [galaxy_row()])
        self.assertEqual((result.state, result.reason), (NOT_FOUND, "required_fields_missing"))

    def test_employee_no_hitting_two_accounts_is_not_authorized(self) -> None:
        # V-开通-02：匹配键命中多条记录时不自动选择任何一条。
        rows = [galaxy_row(), galaxy_row(user_id="U-2", email="other@example-corp.invalid")]
        result = match_galaxy_account("ou_p1", [roster_row()], rows)
        self.assertEqual((result.state, result.reason), (NOT_FOUND, "employee_no_multiple_hits"))

    def test_conflicting_keys_are_not_authorized(self) -> None:
        # V-开通-02：工号指向 U-1、邮箱指向 U-2 → 两键结果冲突。
        rows = [
            galaxy_row(user_id="U-1", email="someone.else@example-corp.invalid"),
            galaxy_row(user_id="U-2", user_name="70000"),
        ]
        result = match_galaxy_account("ou_p1", [roster_row()], rows)
        self.assertEqual((result.state, result.reason), (NOT_FOUND, "key_conflict"))

    def test_email_multi_hit_is_not_authorized_even_when_employee_no_hits_uniquely(self) -> None:
        # V-开通-02 / V-银河-09：任一键命中多条都不是唯一成功。
        rows = [
            galaxy_row(user_id="U-1"),
            galaxy_row(user_id="U-2", user_name="80002", nick_name="别人"),
        ]
        result = match_galaxy_account("ou_p1", [roster_row()], rows)
        self.assertEqual((result.state, result.reason), (NOT_FOUND, "email_multiple_hits"))

    def test_email_fallback_hitting_two_accounts_is_not_authorized(self) -> None:
        rows = [
            galaxy_row(user_id="U-1", user_name="90001"),
            galaxy_row(user_id="U-2", user_name="90002"),
        ]
        result = match_galaxy_account("ou_p1", [roster_row(employee_no="")], rows)
        self.assertEqual((result.state, result.reason), (NOT_FOUND, "email_multiple_hits"))

    def test_employee_no_hit_with_blank_galaxy_user_id_is_not_authorized(self) -> None:
        result = match_galaxy_account("ou_p1", [roster_row()], [galaxy_row(user_id="")])

        self.assertEqual((result.state, result.reason), (NOT_FOUND, "galaxy_record_incomplete"))
        self.assertIsNone(result.galaxy_user_id)

    def test_email_hit_with_blank_galaxy_user_id_is_not_authorized(self) -> None:
        result = match_galaxy_account(
            "ou_p1",
            [roster_row(employee_no="")],
            [galaxy_row(user_id="", user_name="99999")],
        )

        self.assertEqual((result.state, result.reason), (NOT_FOUND, "galaxy_record_incomplete"))
        self.assertIsNone(result.galaxy_user_id)


class GalaxyAccountNotFoundTest(unittest.TestCase):
    """可用键在银河均查无 → 申请指引终态（V-开通-03，不建待办）。"""

    def test_both_keys_missing_from_galaxy_is_not_found(self) -> None:
        rows = [galaxy_row(user_id="U-9", user_name="99999", email="another@example-corp.invalid")]
        result = match_galaxy_account("ou_p1", [roster_row()], rows)
        self.assertEqual((result.state, result.reason), (NOT_FOUND, "galaxy_account_not_found"))

    def test_employee_no_miss_without_email_is_not_found(self) -> None:
        # 产品负责人 2026-08-05 确认「缺邮箱按工号」：工号查无即申请指引。
        result = match_galaxy_account("ou_p1", [roster_row(email="")], [galaxy_row(user_name="99999")])
        self.assertEqual(result.state, NOT_FOUND)

    def test_email_miss_without_employee_no_is_not_found(self) -> None:
        result = match_galaxy_account(
            "ou_p1", [roster_row(employee_no="")], [galaxy_row(email="other@example-corp.invalid")]
        )
        self.assertEqual(result.state, NOT_FOUND)

    def test_galaxy_rows_missing_email_keep_the_not_authorized_result(self) -> None:
        # 早期纯邮箱口径的保守分支已被决策记录取代：银河存在缺 email 账号
        # 不再把「查无」整体升级为人工。
        rows = [
            galaxy_row(user_id="U-9", user_name="99999", email="another@example-corp.invalid"),
            galaxy_row(user_id="U-8", user_name="88888", email=""),
        ]
        result = match_galaxy_account("ou_p1", [roster_row()], rows)
        self.assertEqual(result.state, NOT_FOUND)


class GalaxyAccountNickNameIsAdvisoryOnlyTest(unittest.TestCase):
    """V-银河-10：nick_name 只作内部诊断，不参与判定。"""

    def test_nick_name_collision_does_not_change_a_matched_result(self) -> None:
        rows = [
            galaxy_row(),
            galaxy_row(user_id="U-2", user_name="80002", email="other@example-corp.invalid", nick_name="何虚工"),
        ]
        result = match_galaxy_account("ou_p1", [roster_row()], rows)
        self.assertEqual((result.state, result.galaxy_user_id), (MATCHED, "U-1"))

    def test_duplicate_employee_no_stays_not_authorized_even_when_one_nick_name_matches(self) -> None:
        # 变异守卫：两行同工号、姓名一同一异，若实现偷偷用姓名挑一条，本用例变红。
        rows = [
            galaxy_row(user_id="U-1", nick_name="何虚工", email="a@example-corp.invalid"),
            galaxy_row(user_id="U-2", nick_name="别人", email="b@example-corp.invalid"),
        ]
        result = match_galaxy_account("ou_p1", [roster_row()], rows)
        self.assertEqual((result.state, result.reason), (NOT_FOUND, "employee_no_multiple_hits"))

    def test_nick_name_cannot_rescue_a_missed_lookup(self) -> None:
        rows = [galaxy_row(user_name="99999", email="other@example-corp.invalid", nick_name="何虚工")]
        result = match_galaxy_account("ou_p1", [roster_row()], rows)
        self.assertEqual(result.state, NOT_FOUND)

    def test_outcome_is_identical_when_every_nick_name_changes(self) -> None:
        rows_a = [
            galaxy_row(nick_name="甲"),
            galaxy_row(user_id="U-2", user_name="70002", email="x@example-corp.invalid", nick_name="乙"),
        ]
        rows_b = [
            galaxy_row(nick_name="丙"),
            galaxy_row(user_id="U-2", user_name="70002", email="x@example-corp.invalid", nick_name="丁"),
        ]
        a = match_galaxy_account("ou_p1", [roster_row()], rows_a)
        b = match_galaxy_account("ou_p1", [roster_row()], rows_b)
        self.assertEqual((a.state, a.reason, a.galaxy_user_id), (b.state, b.reason, b.galaxy_user_id))

    def test_nick_name_is_reported_as_advisory_context(self) -> None:
        result = match_galaxy_account("ou_p1", [roster_row()], [galaxy_row()])
        self.assertEqual(result.advisory.roster_name, "何虚工")
        self.assertIn("何虚工", result.advisory.galaxy_nick_names)


class GalaxyAccountStateVocabularyTest(unittest.TestCase):
    def test_only_two_user_level_states_exist(self) -> None:
        self.assertEqual({MATCHED, NOT_FOUND}, {"matched", "not_found"})


if __name__ == "__main__":
    unittest.main()
