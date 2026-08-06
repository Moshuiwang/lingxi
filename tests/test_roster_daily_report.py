"""管理群审计日报正文（Issue #52 / W4-B，定案 A+甲；裁定 C1）。

认领断言：V-花名册-21、22、23、24。

这一层全是模式扫描：断言"正文里**没有**某类东西"。写成"断言字符串不出现"而不是
"断言行数为 0"是刻意的——一条把真实邮箱拼进标题的实现，行数照样是对的。
"""

from __future__ import annotations

import unittest
from datetime import date

from lingxi.core.ids import is_ulid
from lingxi.core.identity.identifiers import redact_identifier
from lingxi.core.identity.roster_audit import ArchivedIdentity, compare_roster
from lingxi.core.identity.roster_report import HANDOVER_MARK, render_daily_report

REPORT_DATE = date(2026, 8, 6)

# 两个合法 ULID（`core/ids.py` 的形状），互不相同。
FIRST_USER = "usr_01JQZX3M5N7P9R1T3V5W7Y9A0B"
SECOND_USER = "usr_01K2AB4D6F8H0J2M4P6R8T0V2W"
THIRD_USER = "usr_01M3CD5E7G9J1K3N5Q7S9V1X3Y"

# 飞书外部标识：前 6 位相同、完整值不同（当前能力实测 710 人中 57 组前缀碰撞）。
FIRST_PERSON = "ou_pfxAAAA1111"
SECOND_PERSON = "ou_pfxBBBB2222"
THIRD_PERSON = "ou_pfxCCCC3333"

# 真实资料值。这些**一个都不许**出现在正文里。
ARCHIVED_VALUES = ("张三", "E1001", "zhangsan@example.com", "李四", "E1002", "lisi@example.com")
ROSTER_VALUES = ("张三改名", "E9999", "zhangsan.new@example.com", "新同学", "newcomer@example.com")

# 「新旧值对」的各种写法。甲的口径是根本不给值，因此这些形态一个都不该出现。
ARROW_FORMS = ("→", "->", "=>", "⇒", "改为", "变为", "原值", "新值", "旧值")

# 可执行入口的痕迹（`V-花名册-24`；合同 :75/:85/:237、V-管理-11）。
EXECUTABLE_MARKERS = (
    "http",
    "://",
    "www.",
    "](",
    "button",
    "btn",
    "onclick",
    "callback",
    "card_action",
    "按钮",
    "点击",
    "一键",
    "立即处理",
    "确认删除",
)


def scenario():
    """一份覆盖三类条目的报告：改名、姓名+邮箱同变（转交）、花名册查无。"""

    baseline = [
        ArchivedIdentity(FIRST_USER, FIRST_PERSON, "张三", "E1001", "zhangsan@example.com"),
        ArchivedIdentity(SECOND_USER, SECOND_PERSON, "李四", "E1002", "lisi@example.com"),
        ArchivedIdentity(THIRD_USER, THIRD_PERSON, "王五", "E1003", "wangwu@example.com"),
    ]
    rows = [
        {
            "personnel_id": FIRST_PERSON,
            "name": "张三改名",
            "employee_no": "E1001",
            "email": "zhangsan@example.com",
        },
        {
            "personnel_id": SECOND_PERSON,
            "name": "新同学",
            "employee_no": "E1002",
            "email": "newcomer@example.com",
        },
        # 第三个人在花名册里查无。
    ]
    return compare_roster(baseline, rows)


class ReportShapeTest(unittest.TestCase):
    """V-花名册-21：逐条＝标识 + 变化字段名 + 转交标注；转交条目可区分。"""

    def setUp(self) -> None:
        self.report = scenario()
        self.body = render_daily_report(self.report, report_date=REPORT_DATE)

    def test_every_entry_gets_its_own_line_with_identifier_and_field_names(self) -> None:
        lines = self.body.splitlines()

        first_line = next(line for line in lines if FIRST_USER in line)
        self.assertIn("变化字段", first_line)
        self.assertIn("姓名", first_line)
        # 只改了姓名，不该顺带列上工号或邮箱这两个字段名。
        self.assertNotIn("工号", first_line)
        self.assertNotIn("邮箱", first_line)

    def test_the_handover_entry_is_distinguishable_from_an_ordinary_change(self) -> None:
        handover_line = next(line for line in self.body.splitlines() if SECOND_USER in line)
        ordinary_line = next(line for line in self.body.splitlines() if FIRST_USER in line)

        self.assertIn(HANDOVER_MARK, handover_line)
        self.assertNotIn(HANDOVER_MARK, ordinary_line)
        # 全文只有那一条被标注。
        self.assertEqual(self.body.count(HANDOVER_MARK), 1)

    def test_the_removed_entry_says_no_automatic_action_was_taken(self) -> None:
        """移除只上报（`V-花名册-26`）。正文写明这一点，管理员才不会以为已经处理过。"""

        self.assertIn("花名册查无此人", self.body)
        self.assertIn("未做任何自动处置", self.body)
        self.assertIn(THIRD_USER, self.body)

    def test_the_report_states_the_date_and_the_examined_population(self) -> None:
        self.assertIn(REPORT_DATE.isoformat(), self.body)
        self.assertIn("已开通用户 3 人", self.body)

    def test_rendering_is_byte_identical_for_the_same_input(self) -> None:
        """`V-花名册-31` 第③面：重启后当日重发的载荷必须与首次逐字段一致。"""

        self.assertEqual(self.body, render_daily_report(scenario(), report_date=REPORT_DATE))


class NoValuesInBodyTest(unittest.TestCase):
    """V-花名册-22（甲的否定面）：正文无任何真实资料值、无新旧值对形态。"""

    def setUp(self) -> None:
        self.body = render_daily_report(scenario(), report_date=REPORT_DATE)

    def test_no_archived_or_current_roster_value_appears_anywhere_in_the_body(self) -> None:
        for value in ARCHIVED_VALUES + ROSTER_VALUES:
            with self.subTest(value=value):
                self.assertNotIn(value, self.body, f"日报正文不得出现资料值：{value}")

    def test_no_old_to_new_value_pair_form_appears_anywhere_in_the_body(self) -> None:
        for form in ARROW_FORMS:
            with self.subTest(form=form):
                self.assertNotIn(form, self.body, f"日报正文不得出现新旧值形态：{form}")

    def test_field_names_are_present_which_is_what_makes_the_absence_meaningful(self) -> None:
        """反向自检：字段**名**必须在，否则"没有值"只是因为正文是空的。"""

        self.assertIn("姓名", self.body)
        self.assertIn("邮箱", self.body)
        self.assertIn("变化字段", self.body)


class IdentifierIsActionableTest(unittest.TestCase):
    """V-花名册-23（裁定 C1，验收者定稿）：正文标识＝`app_user.id` 完整 ULID。"""

    def setUp(self) -> None:
        self.body = render_daily_report(scenario(), report_date=REPORT_DATE)

    def test_the_fixtures_really_are_ulids_with_colliding_external_prefixes(self) -> None:
        """用例前提自检。"""

        for identifier in (FIRST_USER, SECOND_USER, THIRD_USER):
            self.assertTrue(is_ulid(identifier.removeprefix("usr_")), f"{identifier} 必须是合法 ULID")
        self.assertEqual(FIRST_PERSON[:6], SECOND_PERSON[:6])
        self.assertNotEqual(FIRST_PERSON, SECOND_PERSON)

    def test_users_with_colliding_external_prefixes_still_get_distinct_identifiers(self) -> None:
        """肯定面：外部标识前 6 位撞了，日报里的标识依然互不相同、可唯一定位。"""

        self.assertIn(FIRST_USER, self.body)
        self.assertIn(SECOND_USER, self.body)
        self.assertNotEqual(FIRST_USER, SECOND_USER)

    def test_the_full_ulid_is_printed_without_truncation(self) -> None:
        """否定面③：不得截断 ULID。截断后的前缀会碰撞，唯一定位随即失效。"""

        for identifier in (FIRST_USER, SECOND_USER, THIRD_USER):
            with self.subTest(identifier=identifier):
                self.assertIn(identifier, self.body)
                # 出现次数与条目数一致：不能只印一次完整值、其余印短的。
                self.assertEqual(self.body.count(identifier), 1)

    def test_no_redacted_identifier_form_appears_in_the_body(self) -> None:
        """否定面①：`redact_identifier()` 的返回值不可反查也不可比较，进正文等于
        让管理员拿着一个定位不到人的字符串。"""

        redacted = redact_identifier(FIRST_PERSON)
        self.assertIn("…(", redacted, "用例前提：脱敏形态形如 ou_pfx…(14)")

        self.assertNotIn("…(", self.body)
        self.assertNotIn(redacted, self.body)

    def test_no_feishu_external_identifier_appears_in_the_body(self) -> None:
        """否定面②：花名册人员 ID / open_id / user_id / union_id 原值都不得进正文。"""

        for external in (FIRST_PERSON, SECOND_PERSON, THIRD_PERSON):
            with self.subTest(external=external):
                self.assertNotIn(external, self.body)
        # 连前缀形态也不该出现——正文里没有任何飞书标识。
        self.assertNotIn("ou_", self.body)


class NoExecutableEntryTest(unittest.TestCase):
    """V-花名册-24：正文里没有任何可执行入口。

    管理群是通知面。处置一律回到管理 MCP 走确认流程（合同 :75/:85/:237、V-管理-11）；
    群里放一个按钮，就等于给了一条绕过确认流程的路径。
    """

    def test_the_body_contains_no_link_button_or_callback_of_any_kind(self) -> None:
        body = render_daily_report(scenario(), report_date=REPORT_DATE)

        for marker in EXECUTABLE_MARKERS:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, body.lower() if marker.isascii() else body)

    def test_the_body_directs_the_administrator_to_the_management_path(self) -> None:
        """没有入口不等于没有出路：正文明确指向管理 MCP。"""

        body = render_daily_report(scenario(), report_date=REPORT_DATE)

        self.assertIn("管理 MCP", body)


class EmptyReportTest(unittest.TestCase):
    """空差异报告不会走到渲染（那天不发日报），但渲染它也不该炸。"""

    def test_rendering_an_empty_report_yields_a_header_only_body(self) -> None:
        from lingxi.core.identity.roster_audit import RosterAuditReport

        body = render_daily_report(RosterAuditReport((), examined=7), report_date=REPORT_DATE)

        self.assertIn("已开通用户 7 人", body)
        self.assertIn("0 条", body)
        self.assertNotIn("变化字段", body)


if __name__ == "__main__":
    unittest.main()
