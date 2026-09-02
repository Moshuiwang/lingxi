"""花名册审计的真库断言（Issue #52 / W4-B，定案 A+甲）。

认领断言：V-花名册-10、V-花名册-11、V-花名册-12、V-花名册-14、V-花名册-15、
V-花名册-35、V-花名册-36。

比对集的两条过滤写在 SQL 里，因此只有真库能证伪它们：在 Python 里过滤的实现，用假的
基线读取器跑照样是绿的。「存档不写回」同理——只有跑完一整轮再回读数据库，才能说
`employee_no` 真的没被改过。
"""

from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timezone

from postgres_schema import ensure_production_schema, psycopg_available

from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_roster_audit import PostgresRosterBaselineReader
from lingxi.apps.scheduler import RosterAuditDuty
from lingxi.core.identity.roster_audit import DiffKind
from lingxi.core.identity.roster_snapshot import (
    DEFAULT_SNAPSHOT_STALE_AFTER,
    RosterRound,
    RosterSnapshotStatus,
)

SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，花名册审计的真库断言未验证（需真实 PostgreSQL 16）"
    if not os.environ.get("LINGXI_POSTGRES_DSN")
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，花名册审计的真库断言未验证"
)

# 存档三字段之外的一切都不进比对，但它们必须真的落在库里才能证明"没被读进来"。
ARCHIVED_FIELDS_SQL = ("display_name", "employee_no", "email")

# 只在花名册行里出现、库里从来没有的哨兵值。整轮跑完后全库扫描它们（`V-花名册-15`）。
SENTINEL_ACCOUNT_STATE = "LX52SENTINELFROZEN"
SENTINEL_DEPARTMENT = "LX52SENTINELDEPT"

# 未开通人员的资料值。它们一个都不许出现在输出、日报、审计或日志里（`V-花名册-12`）。
UNPROVISIONED_NAME = "未开通的人"
UNPROVISIONED_EMPLOYEE_NO = "E7777"
UNPROVISIONED_EMAIL = "not.provisioned@example.com"

OTHER_PROVISIONING_STATES = ("guest", "matching", "manual_review", "provisioning", "mcp_syncing", "aborted")
EXCLUDED_ACCOUNT_STATES = ("deleting", "deleted")


class FakeSender:
    def __init__(self) -> None:
        self.payloads: list[dict[str, str]] = []

    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None:
        self.payloads.append({"chat_id": chat_id, "text": text, "dedupe_key": dedupe_key})


class RecordingAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, object]]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, dict(fields)))


class FreshSnapshotSource:
    """一轮花名册取用的替身：交出注入的行 + 一份刚刚整体替换过的快照状态。

    快照链本身（替换 / 保旧 / 超龄）在 `tests/test_roster_snapshot*.py` 与
    `tests/test_roster_daily_source.py` 里验；本文件验的是**真库那一侧**——比对集过滤、
    存档不写回、端到端，因此这里只需要一个"快照可用且新鲜"的稳定前提。
    """

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def current(self, *, now: datetime) -> RosterRound:
        return RosterRound(
            tuple(self._rows),
            RosterSnapshotStatus(
                action="replace",
                read_status="complete",
                stale_after_seconds=DEFAULT_SNAPSHOT_STALE_AFTER.total_seconds(),
                captured_at=now,
                row_count=max(len(self._rows), 1),
                age_seconds=0.0,
            ),
        )


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN") and psycopg_available(), SKIP_REASON)
class RosterAuditPostgresTestCase(unittest.TestCase):
    """真库断言的共同底座。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '5s'")
            cursor.execute("TRUNCATE app_user CASCADE")

    # -- 造数与回读 ---------------------------------------------------------

    def insert_user(
        self,
        *,
        app_user_id: str,
        personnel_id: str,
        display_name: str = "存档姓名",
        employee_no: str | None = "E0000",
        email: str | None = "archived@example.com",
        provisioning_state: str = "active",
        account_state: str = "enabled",
        tombstone: bool = False,
    ) -> str:
        """插一名用户。

        身份六元组要么全空要么全非空（app_user 的 CHECK：不写半条记录）。``tombstone``
        对应删除编排完成后的形态——数据库设计 :144 要求进入 ``deleted`` 时清空飞书标识、
        姓名、部门与租户，因此那一行只能整体为空，不能只把姓名清成空串。
        """

        with connect(self._dsn) as connection, connection.cursor() as cursor:
            if tombstone:
                cursor.execute(
                    """INSERT INTO app_user
                         (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name,
                          display_name_locale, department, tenant_key, employee_no, email,
                          provisioning_state, account_state)
                       VALUES (%s, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, %s, %s)""",
                    (app_user_id, provisioning_state, account_state),
                )
                return app_user_id
            cursor.execute(
                """INSERT INTO app_user
                     (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name,
                      display_name_locale, department, tenant_key, employee_no, email,
                      provisioning_state, account_state)
                   VALUES (%s, %s, %s, %s, %s, 'zh_cn', '某部门', 'tenant_a', %s, %s, %s, %s)""",
                (
                    app_user_id,
                    f"ou_open_{personnel_id}",
                    personnel_id,
                    f"on_union_{personnel_id}",
                    display_name,
                    employee_no,
                    email,
                    provisioning_state,
                    account_state,
                ),
            )
        return app_user_id

    def archived_snapshot(self) -> dict[str, tuple[object, ...]]:
        """全部 app_user 行的存档三字段快照，用于逐字段对比。"""

        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT id, {', '.join(ARCHIVED_FIELDS_SQL)}, updated_at FROM app_user ORDER BY id")
            return {str(row[0]): tuple(row[1:]) for row in cursor.fetchall()}

    def every_text_value_in_the_database(self) -> str:
        """把全库所有文本列的内容拼成一个大字符串，用于哨兵扫描。"""

        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT table_name, column_name FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND data_type IN ('text', 'character varying', 'jsonb', 'json')
                    ORDER BY table_name, column_name"""
            )
            columns = cursor.fetchall()
            collected: list[str] = []
            for table, column in columns:
                cursor.execute(f'SELECT string_agg("{column}"::text, e\'\\n\') FROM "{table}"')
                row = cursor.fetchone()
                if row and row[0]:
                    collected.append(str(row[0]))
        return "\n".join(collected)

    def run_full_round(
        self,
        rows: list[dict[str, object]],
        *,
        report_date: date = date(2026, 8, 6),
    ) -> tuple[object, FakeSender, RecordingAudit]:
        """跑一整轮真实职责：真库读基线 → 比对 → 渲染 → 发送。"""

        sender = FakeSender()
        audit = RecordingAudit()
        duty = RosterAuditDuty(
            baseline_reader=PostgresRosterBaselineReader(self._dsn),
            roster_source=FreshSnapshotSource(rows),
            sender=sender,
            audit=audit,
            chat_id="oc_fake_admin_group_for_tests",
            clock=lambda: datetime(
                report_date.year, report_date.month, report_date.day, 9, 0, tzinfo=timezone.utc
            ),
        )
        return duty.run_once(), sender, audit


class ComparisonSetIsActiveOnlyTest(RosterAuditPostgresTestCase):
    """V-花名册-10：比对集只来自 `provisioning_state='active'`。"""

    def test_none_of_the_six_other_provisioning_states_enters_the_comparison_set(self) -> None:
        active = self.insert_user(app_user_id="usr_active", personnel_id="ou_p_active")
        for index, state in enumerate(OTHER_PROVISIONING_STATES):
            self.insert_user(
                app_user_id=f"usr_other_{index}",
                personnel_id=f"ou_p_other_{index}",
                display_name=UNPROVISIONED_NAME,
                employee_no=UNPROVISIONED_EMPLOYEE_NO,
                # 邮箱逐行不同：迁移 0085 起 `app_user` 的规范化邮箱是唯一的，
                # 六行共用一个邮箱建不出来。本用例断言的是 provisioning_state，
                # 邮箱取值对结论没有作用面。
                email=f"other{index}.{UNPROVISIONED_EMAIL}",
                provisioning_state=state,
            )

        baseline = PostgresRosterBaselineReader(self._dsn).load_active_baseline()

        self.assertEqual([person.app_user_id for person in baseline], [active])
        self.assertEqual(len(baseline), 1, f"六种非 active 状态都不得进比对集：{OTHER_PROVISIONING_STATES}")

    def test_an_unprovisioned_person_never_reaches_the_report_audit_or_logs(self) -> None:
        """V-花名册-12：未开通人员的任何字段值都不出现在输出、日报、审计与日志里。

        断言的是**字符串不出现**，不是行数为 0：一条把未开通用户拼进标题的实现，
        行数照样是对的。
        """

        self.insert_user(app_user_id="usr_active", personnel_id="ou_p_active", display_name="存档姓名")
        self.insert_user(
            app_user_id="usr_guest",
            personnel_id="ou_p_guest",
            display_name=UNPROVISIONED_NAME,
            employee_no=UNPROVISIONED_EMPLOYEE_NO,
            email=UNPROVISIONED_EMAIL,
            provisioning_state="guest",
        )
        rows = [
            {"personnel_id": "ou_p_active", "name": "改过的姓名", "employee_no": "E0000", "email": "archived@example.com"},
            # 花名册里也有那位 guest，且资料与库里不同——比对集里没有他，所以不该被发现。
            {
                "personnel_id": "ou_p_guest",
                "name": "另一个未开通姓名",
                "employee_no": "E8888",
                "email": "still.not.provisioned@example.com",
            },
        ]

        with self.assertLogs("lingxi", level="INFO") as captured:
            report, sender, audit = self.run_full_round(rows)

        haystack = "\n".join(
            [repr(report), repr(audit.records), sender.payloads[0]["text"], *captured.output]
        )
        for value in (
            UNPROVISIONED_NAME,
            UNPROVISIONED_EMPLOYEE_NO,
            UNPROVISIONED_EMAIL,
            "另一个未开通姓名",
            "E8888",
            "still.not.provisioned@example.com",
            "usr_guest",
            "ou_p_guest",
        ):
            with self.subTest(value=value):
                self.assertNotIn(value, haystack, f"未开通人员的值不得出现：{value}")

        self.assertEqual([entry.app_user_id for entry in report.entries], ["usr_active"])


class DeletedAccountsAreExcludedTest(RosterAuditPostgresTestCase):
    """V-花名册-11：`account_state IN ('deleting','deleted')` 排除出比对集。

    删除编排会按数据库设计 :144 **清空**姓名等字段。不排除的话，每一个被删除的用户
    都会稳定地报一条「姓名变化」——这正是日报最不该产生的那种噪声。
    """

    def test_users_in_a_deletion_state_produce_zero_difference_entries(self) -> None:
        # `deleting`＝删除进行中，字段都还在。花名册那边的资料已经完全不同——不排除的话
        # 这一行会每天稳定地报一条「姓名变化」，而这个用户正在被删除，报了也没有意义。
        self.insert_user(
            app_user_id="usr_deleting",
            personnel_id="ou_p_deleting",
            display_name="删除中者存档姓名",
            employee_no="E5001",
            email="deleting@example.com",
            account_state="deleting",
        )
        # `deleted`＝删除编排完成后的 tombstone：飞书标识、姓名、部门、租户全部清空
        # （数据库设计 :144），只剩下一个映射不回人的内部 ID。
        self.insert_user(app_user_id="usr_deleted", personnel_id="", account_state="deleted", tombstone=True)

        baseline = PostgresRosterBaselineReader(self._dsn).load_active_baseline()
        report, sender, _audit = self.run_full_round(
            [
                {
                    "personnel_id": "ou_p_deleting",
                    "name": "花名册里换人了",
                    "employee_no": "E9999",
                    "email": "someone.else@example.com",
                }
            ]
        )

        self.assertEqual(baseline, (), f"{EXCLUDED_ACCOUNT_STATES} 都不得进比对集")
        self.assertEqual(report.entries, (), "删除态用户不得产生任何差异项")
        self.assertEqual(sender.payloads, [], "全是删除态时是空差异日，不发日报")

    def test_a_suspended_but_not_deleted_user_stays_in_the_comparison_set(self) -> None:
        """`suspended` 不在排除之列：停用不是删除，他的资料仍然该被盯着。"""

        self.insert_user(app_user_id="usr_suspended", personnel_id="ou_p_s", account_state="suspended")

        baseline = PostgresRosterBaselineReader(self._dsn).load_active_baseline()

        self.assertEqual([person.app_user_id for person in baseline], ["usr_suspended"])


class ArchiveIsNeverWrittenBackTest(RosterAuditPostgresTestCase):
    """V-花名册-14：存档不写回（核心）。

    `employee_no` 是匹配银河权限记录的**主键**。静默把花名册当前值写回存档，会在
    下一次匹配时改变匹配结果，而日报存在的意义恰恰是让人来判断这次变化该不该被接受。
    """

    def test_a_full_round_with_all_three_fields_changed_leaves_app_user_byte_identical(self) -> None:
        self.insert_user(
            app_user_id="usr_all_changed",
            personnel_id="ou_p_all",
            display_name="存档姓名",
            employee_no="E0000",
            email="archived@example.com",
        )
        before = self.archived_snapshot()

        rows = [
            {
                "personnel_id": "ou_p_all",
                "name": "花名册新姓名",
                "employee_no": "E9999",
                "email": "roster.new@example.com",
            }
        ]
        report, sender, _audit = self.run_full_round(rows)

        self.assertEqual(report.entries[0].changed_fields, ("display_name", "employee_no", "email"))
        self.assertEqual(len(sender.payloads), 1)

        after = self.archived_snapshot()
        self.assertEqual(set(before), set(after), "行数与主键都不得变化")
        for app_user_id, values in before.items():
            for index, field in enumerate(ARCHIVED_FIELDS_SQL):
                with self.subTest(field=field):
                    self.assertEqual(after[app_user_id][index], values[index], f"{field} 被写回了")
        # `updated_at` 也不该动：动了就说明发生过 UPDATE，哪怕字段值凑巧一样。
        self.assertEqual(after["usr_all_changed"][-1], before["usr_all_changed"][-1])

    def test_the_reader_module_contains_no_write_statement_at_all(self) -> None:
        """源码层的第二道：只读是设计，不是「这次恰好没写」。

        只扫**非文档字符串的字符串常量**（SQL 只可能住在那里），不扫注释与 docstring：
        本模块的注释里就写着「不出现 INSERT / UPDATE / DELETE」，按全文扫会被自己的
        说明文字误报，然后为了让它变绿去删掉那句说明——检查就此失去意义。
        """

        import ast
        import pathlib

        path = pathlib.Path(__file__).parents[1] / "src" / "lingxi" / "adapters" / "postgres_roster_audit.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))

        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        literals = [
            node.value.upper()
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
        ]

        # 反向自检：确实扫到了那条 SELECT，不是因为一个常量都没收集到才绿的。
        self.assertTrue(any("SELECT" in literal for literal in literals), "扫描必须看得见 SQL 常量")

        for statement in ("INSERT ", "UPDATE ", "DELETE ", "TRUNCATE ", "UPSERT", "ON CONFLICT"):
            for literal in literals:
                with self.subTest(statement=statement):
                    self.assertNotIn(statement, literal)


class AccountStateIsNeverPersistedTest(RosterAuditPostgresTestCase):
    """V-花名册-15：账号状态按需回读**不落库**（数据库设计 :150）。

    飞书在职状态与花名册「账号状态」都是"存下来立刻就会陈旧"的值，而它们唯一的用途
    正是拦截陈旧状态。本切片连读都不读它们；这条断言证明的是：即便它们被塞进输入行，
    整轮跑完之后库里也找不到。
    """

    def test_a_sentinel_account_state_carried_on_a_roster_row_never_reaches_the_database(self) -> None:
        self.insert_user(app_user_id="usr_sentinel", personnel_id="ou_p_sentinel")
        rows = [
            {
                "personnel_id": "ou_p_sentinel",
                "name": "改过的姓名",
                "employee_no": "E0000",
                "email": "archived@example.com",
                # 输入行携带账号状态与部门。定案排除这两项，它们既不进差异集，也不落库。
                "account_state": SENTINEL_ACCOUNT_STATE,
                "status": SENTINEL_ACCOUNT_STATE,
                "department": SENTINEL_DEPARTMENT,
            }
        ]

        report, sender, audit = self.run_full_round(rows)

        # 只有姓名进了差异集（`V-花名册-09` 在真库上的落点）。
        self.assertEqual(report.entries[0].changed_fields, ("display_name",))

        stored = self.every_text_value_in_the_database()
        self.assertNotIn(SENTINEL_ACCOUNT_STATE, stored, "账号状态不得落库")
        self.assertNotIn(SENTINEL_DEPARTMENT, stored, "部门不得由本切片写入")
        # 也不进日报与审计。
        haystack = sender.payloads[0]["text"] + repr(audit.records)
        self.assertNotIn(SENTINEL_ACCOUNT_STATE, haystack)
        self.assertNotIn(SENTINEL_DEPARTMENT, haystack)

    def test_the_sentinel_scan_can_actually_see_database_content(self) -> None:
        """反向自检：扫描函数真的能看见库里的文本，不是恒返回空串才绿的。"""

        self.insert_user(app_user_id="usr_visible", personnel_id="ou_p_visible", display_name="可见的存档姓名")

        self.assertIn("可见的存档姓名", self.every_text_value_in_the_database())


class SyntheticEndToEndTest(RosterAuditPostgresTestCase):
    """V-花名册-35：合成端到端，6 名已开通 + 1 名 guest。V-花名册-36：该用例在 CI 实跑。

    这是唯一一条把「真库过滤 → 比对 → 渲染 → 发送 → 审计 → 存档不变」串起来跑的用例。
    """

    ROSTER_VALUES = (
        "花名册改名",
        "roster.changed@example.com",
        "转交新人",
        "handover.new@example.com",
    )
    # D2：被报出来的那四个人，存档身份（姓名 + 工号）**必须**出现在正文里——
    # 不给原值管理员无法准确定位并处理人员变化。
    REPORTED_IDENTITY_VALUES = (
        "改名者存档姓名",
        "改邮箱者存档姓名",
        "转交者存档姓名",
        "消失者存档姓名",
        "E1001",
        "E1002",
        "E1003",
        "E1004",
    )
    # 没有被报出来的人，一个字段都不该露面。
    UNREPORTED_IDENTITY_VALUES = (
        "缺邮箱者存档姓名",
        "无变化者存档姓名",
        "E1005",
        "E1006",
    )
    # 存档邮箱：D2 允许发原值，但本实现刻意不印邮箱（定位靠姓名 + 工号）。
    ARCHIVED_EMAILS = (
        "changed.name@example.com",
        "changed.email@example.com",
        "handover.old@example.com",
        "vanished@example.com",
        "unchanged@example.com",
    )

    def _populate(self) -> None:
        self.insert_user(
            app_user_id="usr_e2e_1_name",
            personnel_id="ou_p_1",
            display_name="改名者存档姓名",
            employee_no="E1001",
            email="changed.name@example.com",
        )
        self.insert_user(
            app_user_id="usr_e2e_2_mail",
            personnel_id="ou_p_2",
            display_name="改邮箱者存档姓名",
            employee_no="E1002",
            email="changed.email@example.com",
        )
        self.insert_user(
            app_user_id="usr_e2e_3_handover",
            personnel_id="ou_p_3",
            display_name="转交者存档姓名",
            employee_no="E1003",
            email="handover.old@example.com",
        )
        self.insert_user(
            app_user_id="usr_e2e_4_vanished",
            personnel_id="ou_p_4",
            display_name="消失者存档姓名",
            employee_no="E1004",
            email="vanished@example.com",
        )
        # 存档缺邮箱：花名册那边有邮箱，但没有基线就不该报（`V-花名册-07`）。
        self.insert_user(
            app_user_id="usr_e2e_5_no_email",
            personnel_id="ou_p_5",
            display_name="缺邮箱者存档姓名",
            employee_no="E1005",
            email=None,
        )
        self.insert_user(
            app_user_id="usr_e2e_6_unchanged",
            personnel_id="ou_p_6",
            display_name="无变化者存档姓名",
            employee_no="E1006",
            email="unchanged@example.com",
        )
        self.insert_user(
            app_user_id="usr_e2e_7_guest",
            personnel_id="ou_p_7",
            display_name=UNPROVISIONED_NAME,
            employee_no=UNPROVISIONED_EMPLOYEE_NO,
            email=UNPROVISIONED_EMAIL,
            provisioning_state="guest",
        )

    def _rows(self) -> list[dict[str, object]]:
        return [
            {"personnel_id": "ou_p_1", "name": "花名册改名", "employee_no": "E1001", "email": "changed.name@example.com"},
            {"personnel_id": "ou_p_2", "name": "改邮箱者存档姓名", "employee_no": "E1002", "email": "roster.changed@example.com"},
            {"personnel_id": "ou_p_3", "name": "转交新人", "employee_no": "E1003", "email": "handover.new@example.com"},
            # ou_p_4 在花名册里查无此人。
            {"personnel_id": "ou_p_5", "name": "缺邮箱者存档姓名", "employee_no": "E1005", "email": "roster.has.email@example.com"},
            {"personnel_id": "ou_p_6", "name": "无变化者存档姓名", "employee_no": "E1006", "email": "unchanged@example.com"},
            {"personnel_id": "ou_p_7", "name": "花名册里的未开通者", "employee_no": "E9999", "email": "guest.roster@example.com"},
        ]

    def test_the_full_round_reports_exactly_the_four_expected_entries(self) -> None:
        self._populate()
        before = self.archived_snapshot()

        report, sender, audit = self.run_full_round(self._rows())

        # ---- 条目构成 ----
        self.assertEqual(len(report.entries), 4, "日报恰含 4 条")
        by_user = {entry.app_user_id: entry for entry in report.entries}
        self.assertEqual(set(by_user), {"usr_e2e_1_name", "usr_e2e_2_mail", "usr_e2e_3_handover", "usr_e2e_4_vanished"})
        self.assertEqual(by_user["usr_e2e_1_name"].changed_fields, ("display_name",))
        self.assertEqual(by_user["usr_e2e_2_mail"].changed_fields, ("email",))
        self.assertEqual(report.handover_count, 1, "转交恰 1")
        self.assertTrue(by_user["usr_e2e_3_handover"].handover)
        self.assertEqual(report.removed_count, 1, "移除恰 1")
        self.assertEqual(by_user["usr_e2e_4_vanished"].kind, DiffKind.REMOVED)

        # ---- 三类不该出现的人 ----
        body = sender.payloads[0]["text"]
        for absent in ("usr_e2e_5_no_email", "usr_e2e_6_unchanged", "usr_e2e_7_guest"):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, by_user)
                self.assertNotIn(absent, body)

        # ---- D2 肯定面：被报出来的人给得出存档身份 ----
        for value in self.REPORTED_IDENTITY_VALUES:
            with self.subTest(value=value):
                self.assertIn(value, body, f"日报正文必须给出存档身份：{value}")

        # ---- D2 否定面：花名册当前值、存档邮箱、未被报出的人一个都不出现 ----
        for value in self.ROSTER_VALUES + self.ARCHIVED_EMAILS + self.UNREPORTED_IDENTITY_VALUES:
            with self.subTest(value=value):
                self.assertNotIn(value, body, f"日报正文不得出现该值：{value}")
        self.assertNotIn("→", body)

        # ---- 审计与日志侧没有随日报口径放宽（`V-花名册-33`）----
        audited = repr(audit.records)
        for value in self.REPORTED_IDENTITY_VALUES + self.ROSTER_VALUES + self.ARCHIVED_EMAILS:
            with self.subTest(value=value):
                self.assertNotIn(value, audited, f"审计记录不得出现资料值：{value}")

        # ---- 发送与审计各一次 ----
        self.assertEqual(len(sender.payloads), 1)
        self.assertEqual([action for action, _ in audit.records], ["roster_audit.report_sent"])

        # ---- app_user 三字段零变化 ----
        after = self.archived_snapshot()
        self.assertEqual(before, after, "整轮跑完，app_user 的存档三字段与 updated_at 全部不变")

        # V-花名册-36：把执行计数打进 CI 日志，静默跳过时这一行不会出现。
        print(
            "V-花名册-36 端到端执行计数："
            f"比对集={report.examined} 条目={len(report.entries)} 转交={report.handover_count} "
            f"移除={report.removed_count} 发送={len(sender.payloads)} 审计={len(audit.records)}",
            flush=True,
        )

    def test_an_unchanged_population_is_an_empty_day_that_only_audits(self) -> None:
        """`V-花名册-25` 在真库上的另一面：没有差异就不发。"""

        self.insert_user(app_user_id="usr_same", personnel_id="ou_p_same")

        report, sender, audit = self.run_full_round(
            [{"personnel_id": "ou_p_same", "name": "存档姓名", "employee_no": "E0000", "email": "archived@example.com"}]
        )

        self.assertTrue(report.is_empty)
        self.assertEqual(sender.payloads, [])
        self.assertEqual([action for action, _ in audit.records], ["roster_audit.no_difference"])


class RealDatabaseCoverageGuardTest(unittest.TestCase):
    """V-花名册-36：端到端必须在 CI 实跑，不是静默跳过。

    本用例**不带 skip 装饰器**：它在任何环境都会执行，用来把"该跑真库却没跑"这件事
    变成一次失败，而不是一行不起眼的 `s`。
    """

    def test_the_end_to_end_case_is_not_skipped_when_a_database_is_configured(self) -> None:
        configured = bool(os.environ.get("LINGXI_POSTGRES_DSN"))
        container_only = bool(os.environ.get("LINGXI_POSTGRES_CONTAINER")) and not configured

        self.assertFalse(
            container_only,
            "设了 LINGXI_POSTGRES_CONTAINER 却没有 LINGXI_POSTGRES_DSN：真库断言会被静默跳过",
        )
        # Issue #370 修 2：门控升级为「DSN 存在 且 psycopg 可导入」双条件——DSN 配了但
        # 驱动没装是合法的清晰 skip（不是本守卫要抓的"静默跳过"），只有两个条件都满足
        # 却仍被跳过才是真正的覆盖率漏洞。
        if configured and psycopg_available():
            self.assertFalse(
                getattr(SyntheticEndToEndTest, "__unittest_skip__", False),
                "配置了真库且驱动可用时端到端用例不得被跳过",
            )


if __name__ == "__main__":
    unittest.main()
