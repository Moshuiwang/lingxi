"""管理群审计日报正文（Issue #52；D2 裁定 + 裁定 C1）。

认领断言：V-花名册-21、V-花名册-22、V-花名册-23、V-花名册-24、V-花名册-47、
V-花名册-48。

**口径已按 D2 改写**（S-B-04）：受控管理群的日报**允许**展示用于定位的存档身份
（姓名、工号），此前"正文无任何真实值"的整组断言随 2026-08-08 的产品负责人裁定作废。
留下来的否定面同样重要，而且更难写对：**花名册当前值不进正文**、没有新旧值对形态、
没有可执行入口、没有飞书外部标识原值。

这一层大量使用模式扫描：断言"正文里**没有**某类东西"。写成"断言字符串不出现"而不是
"断言行数为 0"是刻意的——一条把花名册当前值拼进标题的实现，行数照样是对的。
"""

from __future__ import annotations

import unittest
from datetime import UTC, date, datetime

from lingxi.core.identity.identifiers import redact_identifier
from lingxi.core.identity.roster_audit import ArchivedIdentity, RosterAuditReport, compare_roster
from lingxi.core.identity.roster_report import HANDOVER_MARK, render_daily_report
from lingxi.core.identity.roster_snapshot import (
    DEFAULT_SNAPSHOT_STALE_AFTER,
    RosterSnapshotStatus,
)
from lingxi.core.ids import is_ulid

REPORT_DATE = date(2026, 8, 6)

# 两个合法 ULID（`core/ids.py` 的形状），互不相同。
FIRST_USER = "usr_01JQZX3M5N7P9R1T3V5W7Y9A0B"
SECOND_USER = "usr_01K2AB4D6F8H0J2M4P6R8T0V2W"
THIRD_USER = "usr_01M3CD5E7G9J1K3N5Q7S9V1X3Y"

# 飞书外部标识：前 6 位相同、完整值不同（当前能力实测 710 人中 57 组前缀碰撞）。
FIRST_PERSON = "ou_pfxAAAA1111"
SECOND_PERSON = "ou_pfxBBBB2222"
THIRD_PERSON = "ou_pfxCCCC3333"

# 存档身份（姓名、工号）。D2 之下这些**必须**出现在正文里：不给它们，管理员拿到的
# 只是一串 ULID，没法在花名册或后台定位到人。
ARCHIVED_IDENTITY_VALUES = ("张三", "E1001", "李四", "E1002", "王五", "E1003")

# 存档邮箱。D2 允许发原值，但本实现**刻意不印邮箱**：定位靠姓名 + 工号已经够，
# 而"邮箱变了"这件事由字段名说明。多印一列只是每天扩大进入群聊的资料面。
ARCHIVED_EMAILS = ("zhangsan@example.com", "lisi@example.com", "wangwu@example.com")

# 花名册**当前值**。它们一个都不该出现：日报给的是比对结论与 Lingxi 侧的存档身份，
# 不是花名册整表内容的按条目搬运（`V-花名册-22`）。
ROSTER_VALUES = ("张三改名", "E9999", "zhangsan.new@example.com", "新同学", "newcomer@example.com")

# 「新旧值对」的各种写法。正文不并列旧值与新值，因此这些形态一个都不该出现。
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


def identities() -> dict[str, ArchivedIdentity]:
    """D2 的存档身份索引：与 :func:`scenario` 用的是同一份基线。"""

    return {person.app_user_id: person for person in baseline()}


def baseline() -> list[ArchivedIdentity]:
    return [
        ArchivedIdentity(FIRST_USER, FIRST_PERSON, "张三", "E1001", "zhangsan@example.com"),
        ArchivedIdentity(SECOND_USER, SECOND_PERSON, "李四", "E1002", "lisi@example.com"),
        ArchivedIdentity(THIRD_USER, THIRD_PERSON, "王五", "E1003", "wangwu@example.com"),
    ]


STALE_AFTER_SECONDS = DEFAULT_SNAPSHOT_STALE_AFTER.total_seconds()
SNAPSHOT_MOMENT = datetime(2026, 8, 6, 1, 30, tzinfo=UTC)


def fresh_snapshot() -> RosterSnapshotStatus:
    return RosterSnapshotStatus(
        action="replace",
        read_status="complete",
        stale_after_seconds=STALE_AFTER_SECONDS,
        captured_at=SNAPSHOT_MOMENT,
        row_count=1206,
        age_seconds=0.0,
    )


def kept_snapshot(*, age_seconds: float, alert: str = "empty_source") -> RosterSnapshotStatus:
    return RosterSnapshotStatus(
        action="keep_previous",
        read_status="empty_source",
        stale_after_seconds=STALE_AFTER_SECONDS,
        alert=alert,
        captured_at=SNAPSHOT_MOMENT,
        row_count=1206,
        age_seconds=age_seconds,
    )


def absent_snapshot() -> RosterSnapshotStatus:
    return RosterSnapshotStatus(
        action="no_snapshot_yet",
        read_status="failed",
        stale_after_seconds=STALE_AFTER_SECONDS,
        alert="failed_definite",
        failure_code="feishu_code_91403",
        failure_kind="definite",
    )


def render(report=None, **overrides):
    """按生产调用形态渲染：默认带上存档身份与一份新鲜快照。"""

    return render_daily_report(
        scenario() if report is None else report,
        report_date=REPORT_DATE,
        **{"identities": identities(), "snapshot": fresh_snapshot(), **overrides},
    )


class ReportShapeTest(unittest.TestCase):
    """V-花名册-21：逐条＝标识 + 变化字段名 + 转交标注；转交条目可区分。"""

    def setUp(self) -> None:
        self.report = scenario()
        self.body = render(self.report)

    def test_every_entry_gets_its_own_line_with_identifier_identity_and_field_names(self) -> None:
        lines = self.body.splitlines()

        first_line = next(line for line in lines if FIRST_USER in line)
        # D2：存档身份与变化字段名在同一行，管理员一眼看到"谁、哪一列变了"。
        self.assertIn("姓名 张三", first_line)
        self.assertIn("工号 E1001", first_line)
        self.assertIn("变化字段", first_line)

        # 只改了姓名，"变化字段"那一段就只能有姓名。断在段落上而不是整行上：
        # 存档身份那一段本来就带"工号"两个字，断整行会永远绿。
        changed_fields = first_line.split("变化字段：", 1)[1]
        self.assertIn("姓名", changed_fields)
        self.assertNotIn("工号", changed_fields)
        self.assertNotIn("邮箱", changed_fields)

    def test_an_entry_without_changed_fields_still_carries_the_archived_identity(self) -> None:
        """花名册查无此人这一类没有"变化字段"，但身份必须给全——否则管理员拿到的
        是一串没法处置的 ULID。"""

        removed_line = next(line for line in self.body.splitlines() if THIRD_USER in line)

        self.assertIn("姓名 王五", removed_line)
        self.assertIn("工号 E1003", removed_line)
        self.assertNotIn("变化字段", removed_line)

    def test_an_archived_field_that_was_never_filled_in_renders_as_an_explicit_placeholder(self) -> None:
        """建档时就没留工号的用户确实存在。空白渲染成占位符而不是空——一行
        `姓名 张三｜工号 ` 会被读成渲染坏了，而"存档为空"本身是要看到的事实。"""

        index = identities()
        index[FIRST_USER] = ArchivedIdentity(FIRST_USER, FIRST_PERSON, "张三", "", "zhangsan@example.com")

        body = render(identities=index)

        first_line = next(line for line in body.splitlines() if FIRST_USER in line)
        self.assertIn("工号 （存档为空）", first_line)

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

        self.assertEqual(self.body, render())


class D2BodyScopeTest(unittest.TestCase):
    """V-花名册-21 的肯定面与 V-花名册-22 的否定面（D2 口径）。"""

    def setUp(self) -> None:
        self.body = render()

    def test_the_archived_identity_of_every_reported_person_appears_in_the_body(self) -> None:
        """肯定面：D2 的理由是"不给原值管理员无法准确定位"，所以这一条必须真的在。"""

        for value in ARCHIVED_IDENTITY_VALUES:
            with self.subTest(value=value):
                self.assertIn(value, self.body, f"日报正文必须给出存档身份：{value}")

    def test_no_current_roster_value_appears_anywhere_in_the_body(self) -> None:
        """否定面①：正文是比对结论，不是花名册整表内容的按条目搬运。"""

        for value in ROSTER_VALUES:
            with self.subTest(value=value):
                self.assertNotIn(value, self.body, f"日报正文不得出现花名册当前值：{value}")

    def test_no_archived_email_value_appears_anywhere_in_the_body(self) -> None:
        """否定面②：定位靠姓名 + 工号；邮箱值多印一列只是扩大进群的资料面。"""

        for value in ARCHIVED_EMAILS:
            with self.subTest(value=value):
                self.assertNotIn(value, self.body, f"日报正文不印邮箱值：{value}")

    def test_no_old_to_new_value_pair_form_appears_anywhere_in_the_body(self) -> None:
        for form in ARROW_FORMS:
            with self.subTest(form=form):
                self.assertNotIn(form, self.body, f"日报正文不得出现新旧值形态：{form}")

    def test_field_names_are_present_which_is_what_makes_the_absence_meaningful(self) -> None:
        """反向自检：字段**名**必须在，否则上面几条"不出现"只是因为正文是空的。"""

        self.assertIn("邮箱", self.body)
        self.assertIn("变化字段", self.body)


class IdentifierIsActionableTest(unittest.TestCase):
    """V-花名册-23（裁定 C1，验收者定稿）：正文标识＝`app_user.id` 完整 ULID。"""

    def setUp(self) -> None:
        self.body = render()

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
        body = render()

        for marker in EXECUTABLE_MARKERS:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, body.lower() if marker.isascii() else body)

    def test_the_body_directs_the_administrator_to_the_management_path(self) -> None:
        """没有入口不等于没有出路：正文明确指向管理 MCP。"""

        body = render()

        self.assertIn("管理 MCP", body)


class SnapshotStatusInBodyTest(unittest.TestCase):
    """D2 要求日报写明**快照时间与同步状态**；`V-花名册-47` 要求超龄按日报告警提醒。

    这一段是管理员判断"今天这份日报可不可信"的全部依据：比对用的花名册是几点读的、
    这一轮读取成没成功、基线是不是已经在变旧。缺了它，一份用三天前快照跑出来的
    "今天没有差异"和一份真正新鲜的完全无法区分。
    """

    def test_a_refreshed_snapshot_reports_its_capture_moment_in_utc(self) -> None:
        body = render(snapshot=fresh_snapshot())

        self.assertIn("2026-08-06 01:30:00 UTC", body)
        self.assertIn("1206 行", body)
        self.assertIn("本轮读取正常", body)
        self.assertNotIn("提醒：", body)
        self.assertNotIn("警告：", body)

    def test_a_kept_snapshot_names_the_reason_and_says_it_is_still_the_previous_one(self) -> None:
        body = render(snapshot=kept_snapshot(age_seconds=3 * 3600))

        self.assertIn("花名册读取结果为空", body)
        self.assertIn("继续使用上一份快照", body)
        self.assertIn("3.0 小时", body)
        # 还没到超龄阈值：不提醒。每一次读取抖动都提醒，提醒很快就会被忽略。
        self.assertNotIn("提醒：", body)

    def test_the_four_keep_previous_reasons_never_collapse_into_one_sentence(self) -> None:
        """四类保旧原因互不合并（`V-花名册-43` 的展示面）：合并任意两类，管理员就
        失去了"该去看哪里"这条信息——空源看花名册、不完整看列名、明确失败看权限、
        结果不明多半下一轮就好了。"""

        rendered = {
            alert: render(snapshot=kept_snapshot(age_seconds=3600, alert=alert))
            for alert in ("empty_source", "incomplete", "failed_definite", "failed_indeterminate")
        }
        reasons = {
            alert: next(line for line in body.splitlines() if "继续使用上一份快照" in line)
            for alert, body in rendered.items()
        }

        self.assertEqual(len(set(reasons.values())), 4, "四类保旧原因必须给出四句不同的说明")

    def test_an_unclassified_alert_is_reported_as_unclassified_rather_than_guessed(self) -> None:
        """读取层将来多一个告警分类时，日报如实说"没分过类"。归到四类中的任意一类
        会让管理员去查一个没坏的地方。"""

        body = render(snapshot=kept_snapshot(age_seconds=3600, alert="something_new"))

        self.assertIn("保旧原因未分类", body)

    def test_a_stale_snapshot_warns_and_states_that_nothing_was_deleted(self) -> None:
        """`V-花名册-47`：超龄**按日报告警提醒**，且明说快照不会被自动删除。

        产品负责人 2026-08-17 裁定的两句话必须都在正文里：管理员要知道"基线在变旧"，
        也要知道"我们没有替你删掉它"——否则下一步动作会走偏。
        """

        body = render(snapshot=kept_snapshot(age_seconds=STALE_AFTER_SECONDS + 7200))

        self.assertIn("提醒：", body)
        self.assertIn("已超过 48.0 小时未更新", body)
        self.assertIn("50.0 小时", body)
        self.assertIn("不会被自动删除", body)

    def test_a_snapshot_exactly_at_the_threshold_is_not_yet_stale(self) -> None:
        """边界属于"还没超龄"那一侧：阈值是"超过多久"，不是"到了多久"。"""

        body = render(snapshot=kept_snapshot(age_seconds=STALE_AFTER_SECONDS))

        self.assertNotIn("提醒：", body)

    def test_without_any_snapshot_the_body_says_no_comparison_happened(self) -> None:
        """`V-花名册-48`：一份快照都没有时**不比对**，正文必须说清这一点。

        最危险的输出是这一天照常渲染成"本次发现 0 条需要人工核实"——那句话会被读成
        "花名册没有变化"，而事实是"我们今天什么都没看"。
        """

        body = render(RosterAuditReport((), examined=9), snapshot=absent_snapshot())

        self.assertIn("警告：", body)
        self.assertIn("没有可用的花名册快照", body)
        self.assertIn("本次未进行比对", body)
        self.assertIn("花名册读取被明确拒绝", body)
        self.assertNotIn("本次发现", body, "没有比对就不能报『发现 N 条』")
        self.assertNotIn("已开通用户 9 人", body)

    def test_omitting_the_snapshot_renders_no_snapshot_section_at_all(self) -> None:
        """不传快照状态的调用方（只验条目形态的单测）不该凭空得到一行假的同步状态。"""

        body = render(snapshot=None)

        self.assertNotIn("花名册快照：", body)
        self.assertNotIn("提醒：", body)
        self.assertNotIn("警告：", body)

    def test_the_report_date_is_marked_as_utc(self) -> None:
        """产品负责人 2026-08-07 决定：日报统一使用 UTC+00:00。一份跨时区被转述的
        日报，日期含义必须只有一种。"""

        self.assertIn(f"{REPORT_DATE.isoformat()}（UTC）", render())


class EmptyReportTest(unittest.TestCase):
    """空差异报告不会走到渲染（那天不发日报），但渲染它也不该炸。"""

    def test_rendering_an_empty_report_yields_a_header_only_body(self) -> None:
        body = render(RosterAuditReport((), examined=7))

        self.assertIn("已开通用户 7 人", body)
        self.assertIn("0 条", body)
        self.assertNotIn("变化字段", body)


if __name__ == "__main__":
    unittest.main()
