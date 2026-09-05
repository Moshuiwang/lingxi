"""发送记录的真库断言（Issue #586 完成标准 3/4/6）。

只有真库能证伪它们：**同名单重跑零新增**是 ``dedupe_key`` 唯一约束加
``ON CONFLICT ... DO UPDATE ... WHERE status <> 'delivered'`` 这条守卫的属性；
**已送达不可回退**、**去重键与用途不可改写**是 ``BEFORE UPDATE`` 触发器的属性；
**账号删除带走记录**是外键 ``ON DELETE CASCADE`` 的属性。在假 store 上跑，这几条
无论实现怎么写都是绿的。

表结构由 ``migrations/alembic/versions/0088_outreach_message.py`` 建立，测试库走
``ensure_production_schema`` 的整条 alembic 链，与生产同源。
"""

from __future__ import annotations

import json
import os
import unittest

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_outreach import PostgresOutreachStore, PostgresOutreachSubjects
from lingxi.core.outreach.dispatch import STATUS_DELIVERED, STATUS_FAILED

SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，主动发送记录的真库断言未验证（需真实 PostgreSQL 16）"
    if not os.environ.get("LINGXI_POSTGRES_DSN")
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，主动发送记录的真库断言未验证"
)

USER_ID = "usr_outreach_real"
EMAIL = "outreach.subject@example.invalid"
OPEN_ID = f"ou_{USER_ID}"
DEDUPE = "outreach.welcome:apply:usr_outreach_real"
CONTENT_KEY = "outreach.welcome"
CONTENT_VERSION = "2026-09-05"
STYLE = "header_markdown"
PERMISSIONS_TEXT = '{"1011": ["充值金额"]}'


def _reserve_kwargs(**overrides):
    base = {
        "recipient_open_id": OPEN_ID,
        "user_id": USER_ID,
        "purpose": "apply",
        "dedupe_key": DEDUPE,
        "content_key": CONTENT_KEY,
        "content_version": CONTENT_VERSION,
        "card_style": STYLE,
    }
    base.update(overrides)
    return base


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN") and psycopg_available(), SKIP_REASON)
class OutreachRecordPostgresTest(unittest.TestCase):
    """真库上的发送记录：幂等、终态不可回退、回查形状。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dsn = os.environ["LINGXI_POSTGRES_DSN"]
        ensure_production_schema(cls.dsn)

    def setUp(self) -> None:
        reset_production_rows(self.dsn)
        self.store = PostgresOutreachStore(self.dsn)
        self._insert_user()

    def _insert_user(self, *, permissions: str | None = PERMISSIONS_TEXT) -> None:
        with connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO app_user
                     (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name,
                      department, tenant_key, email, provisioning_state, permission_version)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'active', 3)""",
                (USER_ID, OPEN_ID, "fs_x", "on_x", "化名甲", "测试部门", "tenant-fake", EMAIL),
            )
            if permissions is not None:
                cursor.execute(
                    """INSERT INTO publish_outbox
                         (id, user_id, permission_version, reason, status, payload,
                          published_at)
                       VALUES (%s, %s, 3, 'first_onboarding', 'published', %s, now())""",
                    ("pob_outreach", USER_ID, json.dumps({"permissions": permissions})),
                )

    def _row(self):
        with connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status, attempts, message_id, last_error, purpose"
                "  FROM outreach_message WHERE dedupe_key = %s",
                (DEDUPE,),
            )
            return cursor.fetchone()

    def _count(self) -> int:
        with connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM outreach_message")
            return int(cursor.fetchone()[0])

    def test_a_first_reserve_creates_exactly_one_pending_row(self) -> None:
        record = self.store.reserve(**_reserve_kwargs())
        self.assertEqual(record.status, "pending")
        self.assertEqual(record.attempts, 1)
        self.assertEqual(self._count(), 1)

    def test_rerunning_the_same_roster_adds_no_row_and_reports_delivered(self) -> None:
        """完成标准 3：同名单重跑零新增，且调用方看到"已送达"因而跳过。"""
        first = self.store.reserve(**_reserve_kwargs())
        self.store.mark_delivered(first.record_id, message_id="om_real")
        second = self.store.reserve(**_reserve_kwargs())
        self.assertEqual(second.status, STATUS_DELIVERED)
        self.assertEqual(second.attempts, 1)
        self.assertEqual(self._count(), 1)

    def test_a_retry_after_a_failure_increments_attempts_on_the_same_row(self) -> None:
        first = self.store.reserve(**_reserve_kwargs())
        self.store.mark_failed(first.record_id, error="feishu_code_230013")
        self.assertEqual(self._row()[0], STATUS_FAILED)
        second = self.store.reserve(**_reserve_kwargs())
        self.assertEqual(second.record_id, first.record_id)
        self.assertEqual(second.attempts, 2)
        self.assertEqual(second.status, "pending")
        self.assertEqual(self._count(), 1)

    def test_a_delivered_row_cannot_be_pushed_back_to_failed(self) -> None:
        """否定断言：已送达不可回退，否则回查会读成"这个人没收到"。"""
        record = self.store.reserve(**_reserve_kwargs())
        self.store.mark_delivered(record.record_id, message_id="om_real")
        self.store.mark_failed(record.record_id, error="late_error")
        self.assertEqual(self._row()[0], STATUS_DELIVERED)

    def test_the_database_refuses_to_rewrite_the_dedupe_key(self) -> None:
        """否定断言：幂等锚点不可改写，改了等于伪造历史。"""
        self.store.reserve(**_reserve_kwargs())
        with self.assertRaises(Exception):
            with connect(self.dsn) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE outreach_message SET dedupe_key = 'other' WHERE dedupe_key = %s",
                    (DEDUPE,),
                )

    def test_the_database_refuses_to_turn_a_precheck_into_a_real_delivery(self) -> None:
        """否定断言：预检不算正式送达，事后也改不成。"""
        self.store.reserve(**_reserve_kwargs(purpose="precheck", user_id=None))
        with self.assertRaises(Exception):
            with connect(self.dsn) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE outreach_message SET purpose = 'apply' WHERE dedupe_key = %s",
                    (DEDUPE,),
                )

    def test_a_delivered_row_must_carry_its_delivery_time(self) -> None:
        """否定断言：状态与送达时间不允许互相矛盾。"""
        self.store.reserve(**_reserve_kwargs())
        with self.assertRaises(Exception):
            with connect(self.dsn) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE outreach_message SET status = 'delivered' WHERE dedupe_key = %s",
                    (DEDUPE,),
                )

    def test_deleting_the_account_takes_its_records_with_it(self) -> None:
        self.store.reserve(**_reserve_kwargs())
        with connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM app_user WHERE id = %s", (USER_ID,))
        self.assertEqual(self._count(), 0)

    def test_delivered_keys_answer_the_dry_run_question(self) -> None:
        record = self.store.reserve(**_reserve_kwargs())
        self.assertEqual(self.store.delivered_dedupe_keys([DEDUPE]), frozenset())
        self.store.mark_delivered(record.record_id, message_id="om_real")
        self.assertEqual(self.store.delivered_dedupe_keys([DEDUPE]), frozenset({DEDUPE}))

    def test_the_lookback_returns_facts_without_any_body_text(self) -> None:
        """完成标准 6：回查能回答发给谁 / 内容键＋版本 / 何时 / 结果，正文不在其中。"""
        record = self.store.reserve(**_reserve_kwargs())
        self.store.mark_delivered(record.record_id, message_id="om_real")
        views = self.store.recent_records(limit=10)
        self.assertEqual(len(views), 1)
        view = views[0]
        self.assertEqual(view.recipient_open_id, OPEN_ID)
        self.assertEqual(view.content_key, CONTENT_KEY)
        self.assertEqual(view.content_version, CONTENT_VERSION)
        self.assertEqual(view.status, STATUS_DELIVERED)
        self.assertEqual(view.message_id, "om_real")
        self.assertIsNotNone(view.delivered_at)
        self.assertNotIn("你好", str(view))


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN") and psycopg_available(), SKIP_REASON)
class OutreachSubjectPostgresTest(unittest.TestCase):
    """定位链一次读齐：邮箱 → 花名册姓名 + app_user + 已发布权限。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dsn = os.environ["LINGXI_POSTGRES_DSN"]
        ensure_production_schema(cls.dsn)

    def setUp(self) -> None:
        reset_production_rows(self.dsn)
        self.subjects = PostgresOutreachSubjects(self.dsn)

    def _seed(self, *, roster_names: tuple[str, ...] = ("王晋 (Joshua Wang)",)) -> None:
        with connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO app_user
                     (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name,
                      department, tenant_key, email, provisioning_state, permission_version)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'active', 3)""",
                (USER_ID, OPEN_ID, "fs_x", "on_x", "化名甲", "测试部门", "tenant-fake", EMAIL),
            )
            cursor.execute(
                """INSERT INTO publish_outbox
                     (id, user_id, permission_version, reason, status, payload,
                      published_at)
                   VALUES (%s, %s, 3, 'first_onboarding', 'published', %s, now())""",
                ("pob_outreach", USER_ID, json.dumps({"permissions": PERMISSIONS_TEXT})),
            )
            cursor.execute(
                """INSERT INTO roster_snapshot
                     (id, captured_at, row_count, pages_read, reported_total,
                      total_matches_rows, rows_without_personnel_id)
                   VALUES ('rss_x', now(), %s, 1, %s, TRUE, 0)""",
                (len(roster_names), len(roster_names)),
            )
            for index, name in enumerate(roster_names):
                cursor.execute(
                    """INSERT INTO roster_snapshot_row
                         (snapshot_id, row_index, personnel_id, email, name, employee_no, record_id)
                       VALUES ('rss_x', %s, %s, %s, %s, %s, %s)""",
                    (index, f"per_{index}", EMAIL.upper(), name, f"no_{index}", f"rec_{index}"),
                )

    def test_the_lookup_joins_the_roster_name_with_the_published_scope(self) -> None:
        self._seed()
        facts = self.subjects.facts_for(f"  {EMAIL.upper()} ")
        self.assertEqual(facts.user_id, USER_ID)
        self.assertEqual(facts.open_id, OPEN_ID)
        self.assertEqual(facts.provisioning_state, "active")
        self.assertEqual(facts.account_state, "enabled")
        self.assertEqual(facts.roster_names, ("王晋 (Joshua Wang)",))
        self.assertIn("充值金额", facts.permissions or "")

    def test_two_roster_rows_for_one_email_both_come_back(self) -> None:
        """不在 SQL 里替产品做判断：同邮箱的全部姓名都返回，由装配层决定发不发。"""
        self._seed(roster_names=("王晋", "李四"))
        self.assertEqual(set(self.subjects.facts_for(EMAIL).roster_names), {"王晋", "李四"})

    def test_an_unknown_email_is_an_answer_not_a_failure(self) -> None:
        facts = self.subjects.facts_for("nobody@example.invalid")
        self.assertIsNone(facts.user_id)
        self.assertEqual(facts.roster_names, ())


if __name__ == "__main__":
    unittest.main()
