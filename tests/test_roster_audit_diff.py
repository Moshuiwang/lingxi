"""花名册比对纯函数（Issue #52 / W4-B，定案 A+甲）。

认领断言：V-花名册-01、V-花名册-02、V-花名册-03、V-花名册-04、V-花名册-05、
V-花名册-06、V-花名册-07、V-花名册-08、V-花名册-09。

这一层不碰数据库、不碰网络。比对集的过滤（`provisioning_state` / `account_state`）在
读取层的 SQL 里，断言在 `tests/test_roster_audit_postgres.py`。
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib
import unittest

from lingxi.core.identity.roster_audit import (
    ARCHIVED_FIELDS,
    ArchivedIdentity,
    DiffKind,
    PersonDiff,
    RosterAuditReport,
    compare_roster,
)

MODULE_PATH = pathlib.Path(__file__).parents[1] / "src" / "lingxi" / "core" / "identity" / "roster_audit.py"

# 比对用的固定资料。这些值在日报、审计与日志里都**不得出现**，
# 后续几个测试文件按同一份取值做模式扫描。
NAME = "张三"
EMPLOYEE_NO = "E1001"
EMAIL = "zhangsan@example.com"
PERSON = "ou_person_0001"


def archived(**overrides: object) -> ArchivedIdentity:
    values: dict[str, object] = {
        "app_user_id": "usr_01AAAAAAAAAAAAAAAAAAAAAAAA",
        "personnel_id": PERSON,
        "display_name": NAME,
        "employee_no": EMPLOYEE_NO,
        "email": EMAIL,
    }
    values.update(overrides)
    return ArchivedIdentity(**values)  # type: ignore[arg-type]


def roster_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "personnel_id": PERSON,
        "name": NAME,
        "employee_no": EMPLOYEE_NO,
        "email": EMAIL,
    }
    row.update(overrides)
    return row


class SingleFieldChangeTest(unittest.TestCase):
    """V-花名册-01：三字段逐一独立用例。"""

    CASES = (
        ("display_name", "name", "张三改名"),
        ("employee_no", "employee_no", "E9999"),
        ("email", "email", "zhangsan.new@example.com"),
    )

    def test_each_archived_field_alone_yields_exactly_one_named_difference(self) -> None:
        for field, roster_key, changed_value in self.CASES:
            with self.subTest(field=field):
                report = compare_roster([archived()], [roster_row(**{roster_key: changed_value})])

                self.assertEqual(len(report.entries), 1)
                entry = report.entries[0]
                self.assertEqual(entry.kind, DiffKind.CHANGED)
                # 恰一条差异，且**指名该字段**：只断言"有差异"的话，把三个字段揉成
                # 一个布尔值也能通过。
                self.assertEqual(entry.changed_fields, (field,))
                self.assertFalse(entry.handover)

    def test_the_three_archived_fields_are_exactly_the_designed_set(self) -> None:
        """比对面就是数据库设计 :152 说的那三个存档字段，不多不少。"""

        self.assertEqual(ARCHIVED_FIELDS, ("display_name", "employee_no", "email"))


class ThreeStateTest(unittest.TestCase):
    """V-花名册-02：全等 / 不等 / 花名册查无；A 方案下没有「新增人员」态。"""

    def test_identical_data_yields_an_empty_report_rather_than_none_or_an_error(self) -> None:
        report = compare_roster([archived()], [roster_row()])

        self.assertIsInstance(report, RosterAuditReport)
        self.assertEqual(report.entries, ())
        self.assertTrue(report.is_empty)
        self.assertEqual(report.examined, 1)

    def test_a_difference_is_reported_as_changed(self) -> None:
        report = compare_roster([archived()], [roster_row(name="另一个名字")])

        self.assertEqual([entry.kind for entry in report.entries], [DiffKind.CHANGED])
        self.assertFalse(report.is_empty)

    def test_a_person_missing_from_the_roster_is_reported_as_removed(self) -> None:
        report = compare_roster([archived()], [])

        self.assertEqual([entry.kind for entry in report.entries], [DiffKind.REMOVED])
        self.assertEqual(report.removed_count, 1)
        # 移除条目不带字段名：它不是"某个字段变了"。
        self.assertEqual(report.entries[0].changed_fields, ())

    def test_roster_rows_without_an_app_user_never_become_a_report_entry(self) -> None:
        """A 方案下**没有「新增人员」态**：比对集由 app_user 的已开通用户决定。

        花名册里多出来的人不是 Lingxi 的用户，本系统对他们没有任何判断——把他们
        报进管理群等于把非用户的资料也纳入了每日通知面。
        """

        outsider = roster_row(personnel_id="ou_person_9999", name="局外人", email="outsider@example.com")
        report = compare_roster([archived()], [roster_row(), outsider])

        self.assertTrue(report.is_empty)
        self.assertEqual(report.examined, 1)


class HandoverTest(unittest.TestCase):
    """V-花名册-03（肯定面）与 V-花名册-04（否定面）：转交迹象＝姓名与邮箱**同时**变化。"""

    def test_name_and_email_changing_together_is_marked_as_a_handover(self) -> None:
        report = compare_roster(
            [archived()],
            [roster_row(name="新同学", email="newcomer@example.com")],
        )

        self.assertEqual(len(report.entries), 1)
        entry = report.entries[0]
        self.assertTrue(entry.handover)
        self.assertEqual(entry.changed_fields, ("display_name", "email"))
        # 标记可独立取出：它是 PersonDiff 上的一个布尔字段，不用从字段名集合里再推一次。
        self.assertEqual(report.handover_count, 1)
        self.assertIn("handover", {f.name for f in dataclasses.fields(PersonDiff)})

    def test_none_of_the_three_near_misses_is_marked_as_a_handover(self) -> None:
        """三个否定面。只验肯定面的话，把条件写成「或」也能通过。"""

        cases = {
            "只改姓名": roster_row(name="新同学"),
            "只改邮箱": roster_row(email="newcomer@example.com"),
            # 工号不参与转交判定：工号变化更可能是花名册补录，不是换人。
            "姓名与工号同变": roster_row(name="新同学", employee_no="E9999"),
        }
        for label, row in cases.items():
            with self.subTest(case=label):
                report = compare_roster([archived()], [row])

                self.assertEqual(len(report.entries), 1)
                self.assertFalse(report.entries[0].handover, f"{label} 不应被标为转交")
                self.assertEqual(report.handover_count, 0)


class FullIdentifierJoinTest(unittest.TestCase):
    """V-花名册-05：人员 ID 按**完整值**比对，绝不截断。

    `open_user_id` 的 6 位前缀在 710 人实测中有 57 组碰撞（当前能力）。这里的两个
    人员 ID 前 6 位相同、完整值不同，截断比对会把它们判成同一个人。
    """

    FIRST = "ou_pfxAAAA1111"
    SECOND = "ou_pfxBBBB2222"

    def test_two_ids_sharing_a_six_character_prefix_are_never_treated_as_one_person(self) -> None:
        self.assertEqual(self.FIRST[:6], self.SECOND[:6], "用例前提：前 6 位必须相同")
        self.assertNotEqual(self.FIRST, self.SECOND)

        baseline = [
            archived(app_user_id="usr_01AAAAAAAAAAAAAAAAAAAAAAAA", personnel_id=self.FIRST),
            archived(app_user_id="usr_01BBBBBBBBBBBBBBBBBBBBBBBB", personnel_id=self.SECOND),
        ]
        # 花名册里**只有**第一个人，且他改了名。
        report = compare_roster(baseline, [roster_row(personnel_id=self.FIRST, name="张三改名")])

        by_user = {entry.app_user_id: entry for entry in report.entries}
        self.assertEqual(by_user["usr_01AAAAAAAAAAAAAAAAAAAAAAAA"].kind, DiffKind.CHANGED)
        # 截断到前 6 位时，第二个人会命中第一个人的行，被判成"资料变化"而不是"查无此人"。
        self.assertEqual(by_user["usr_01BBBBBBBBBBBBBBBBBBBBBBBB"].kind, DiffKind.REMOVED)


class DuplicateRosterRowTest(unittest.TestCase):
    """V-花名册-06：同一人员 ID 多行 → 行为唯一确定，不静默取任意行。

    花名册实测存在重复行，`feishu_roster_bitable` 因此刻意不去重。到了比对这一层，
    "取第一行"是在猜：猜中了没人知道，猜错了会把变化吞掉或把没变的报成变化。
    """

    def test_duplicate_rows_are_reported_as_their_own_category_in_either_order(self) -> None:
        matching = roster_row()
        differing = roster_row(name="另一个张三", email="other@example.com")

        for label, rows in (("匹配行在前", [matching, differing]), ("差异行在前", [differing, matching])):
            with self.subTest(case=label):
                report = compare_roster([archived()], rows)

                self.assertEqual(len(report.entries), 1, "多行只出一条，不是每行一条")
                self.assertEqual(report.entries[0].kind, DiffKind.AMBIGUOUS)
                self.assertEqual(report.ambiguous_count, 1)
                # 不假装知道哪些字段变了——根本没有选定任何一行来比。
                self.assertEqual(report.entries[0].changed_fields, ())


class AsymmetricBlankTest(unittest.TestCase):
    """V-花名册-07：空值口径**不对称**，双向用例。"""

    def test_a_blank_archived_field_never_produces_a_daily_false_positive(self) -> None:
        """存档为空的字段不参与比对：没有基线就没有"变化"可言。

        缺邮箱的用户每天被报一次，会让整份日报很快被当作噪声忽略。
        """

        for field, roster_key, roster_value in (
            ("display_name", "name", NAME),
            ("employee_no", "employee_no", EMPLOYEE_NO),
            ("email", "email", EMAIL),
        ):
            with self.subTest(blank_field=field):
                report = compare_roster(
                    [archived(**{field: ""})],
                    [roster_row(**{roster_key: roster_value})],
                )

                self.assertTrue(report.is_empty, f"存档 {field} 为空时不得报差异")

    def test_losing_a_value_that_was_archived_is_always_reported(self) -> None:
        """存档有值、花名册变空**必须报**：丢值往往是转交前兆。"""

        for field, roster_key in (("display_name", "name"), ("employee_no", "employee_no"), ("email", "email")):
            with self.subTest(cleared_field=field):
                report = compare_roster([archived()], [roster_row(**{roster_key: ""})])

                self.assertEqual(len(report.entries), 1)
                self.assertEqual(report.entries[0].changed_fields, (field,))

    def test_a_missing_key_counts_the_same_as_a_blank_value(self) -> None:
        """花名册行里整个键缺失，与值为空同义——都表示花名册那边现在没有这个值。"""

        row = roster_row()
        del row["email"]
        report = compare_roster([archived()], [row])

        self.assertEqual(report.entries[0].changed_fields, ("email",))


class OutputCarriesNoValuesTest(unittest.TestCase):
    """V-花名册-08：比对输出对象**本身不含字段值**；函数无 I/O。

    甲（日报只展示脱敏摘要）的不泄露性由数据结构保证，而不是靠渲染层记得别写。
    """

    ALLOWED_FIELDS = {"app_user_id", "kind", "changed_fields", "handover"}

    def test_person_diff_has_exactly_the_four_value_free_fields(self) -> None:
        self.assertEqual({f.name for f in dataclasses.fields(PersonDiff)}, self.ALLOWED_FIELDS)

    def test_no_archived_or_roster_value_survives_into_the_report_object(self) -> None:
        report = compare_roster(
            [archived()],
            [roster_row(name="新同学", employee_no="E9999", email="newcomer@example.com")],
        )

        rendered = repr(report)
        for value in (NAME, EMPLOYEE_NO, EMAIL, "新同学", "E9999", "newcomer@example.com"):
            self.assertNotIn(value, rendered, f"比对输出里不得出现资料值：{value}")
        # 人员 ID 也是外部标识，同样不进输出对象——输出只认内部 ULID。
        self.assertNotIn(PERSON, rendered)

    def test_the_comparison_module_imports_nothing_that_can_do_io(self) -> None:
        """`core/` 不 import `adapters/`，也不 import 任何能做 I/O 的标准库模块。

        用 AST 检查顶层 import，而不是"跑一次没报错"：没有网络的测试机上，
        一个只在特定分支才走的 `urlopen` 照样会一直是绿的。
        """

        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        self.assertEqual(imported, {"__future__", "collections.abc", "dataclasses", "enum"})
        forbidden = ("os", "socket", "logging", "psycopg", "urllib", "subprocess", "pathlib")
        for name in imported:
            self.assertFalse(
                name.split(".")[0] in forbidden or name.startswith("lingxi.adapters"),
                f"core 层不得 import {name}",
            )

    def test_the_same_input_always_produces_the_same_output(self) -> None:
        """确定性：`V-花名册-31` 第③面要求重启后的重发载荷逐字段一致，源头在这里。"""

        baseline = [
            archived(app_user_id=f"usr_01{letter * 24}", personnel_id=f"ou_person_{index}")
            for index, letter in enumerate("ABCDE")
        ]
        rows = [roster_row(personnel_id=f"ou_person_{index}", name=f"改名{index}") for index in range(5)]

        first = compare_roster(baseline, rows)
        second = compare_roster(list(reversed(baseline)), list(reversed(rows)))

        self.assertEqual(first, second, "输出不得依赖输入顺序或集合迭代序")


class ExcludedFieldsTest(unittest.TestCase):
    """V-花名册-09：部门与账号状态不进差异集，即便输入携带。

    定案 A 明确排除这两项：部门属组织快照持续同步的范畴；飞书在职状态与花名册
    「账号状态」按数据库设计 :150 按需回读、不落库。
    """

    def test_department_and_account_state_never_become_a_difference(self) -> None:
        row = roster_row(
            department="新部门",
            account_state="已冻结",
            # 连带把常见的近义键名也塞进来：实现如果按"行里有什么就比什么"写，
            # 这些键会一起漏进差异集。
            status="is_frozen",
            job_title="新职位",
        )

        report = compare_roster([archived()], [row])

        self.assertTrue(report.is_empty, "部门/账号状态/职位变化都不得进入差异集")

    def test_the_roster_key_map_covers_only_the_three_archived_fields(self) -> None:
        """读取字段集不因本切片扩大：花名册 adapter 仍然只读四列。"""

        from lingxi.adapters.feishu_roster_bitable import ROSTER_FIELD_NAMES, RosterRow
        from lingxi.core.identity.roster_audit import ROSTER_KEYS

        self.assertEqual(ROSTER_FIELD_NAMES, ("人员ID", "邮箱", "人员姓名", "工号"))
        self.assertEqual(RosterRow._fields, ("personnel_id", "email", "name", "employee_no", "record_id"))
        self.assertEqual(set(ROSTER_KEYS), {"display_name", "employee_no", "email"})
        self.assertEqual(set(ROSTER_KEYS.values()), {"name", "employee_no", "email"})

    def test_a_real_roster_row_object_can_be_compared_directly(self) -> None:
        """`RosterRow` 实现了 `get`，可以不经转换直接进比对函数。"""

        from lingxi.adapters.feishu_roster_bitable import RosterRow

        row = RosterRow(personnel_id=PERSON, email=EMAIL, name="改过的名字", employee_no=EMPLOYEE_NO, record_id="rec1")
        report = compare_roster([archived()], [row])

        self.assertEqual(report.entries[0].changed_fields, ("display_name",))


if __name__ == "__main__":
    unittest.main()
