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
from datetime import UTC, datetime

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_identity import PostgresAppUserStore
from lingxi.adapters.postgres_outreach import PostgresOutreachStore, PostgresOutreachSubjects
from lingxi.config.content import default_content_catalog
from lingxi.core.outreach.audience import SKIP_CONTACT_UNAVAILABLE, plan_outreach
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
#: 让 ``plan_outreach`` 能把上面这份权限装配成可发送的取值：公司编号与指标名都查得到中文名。
COMPANY_NAMES = {"1011": "尼日利亚"}
METRIC_LABELS = {"充值金额": "充值金额"}


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

    def test_a_retry_refreshes_the_content_version_and_style_on_the_same_row(self) -> None:
        """重试发的是**现在**这一版内容；记录留着上一次的版本，回查就成了假账。"""
        first = self.store.reserve(**_reserve_kwargs())
        self.store.mark_failed(first.record_id, error="feishu_code_230013")
        self.store.reserve(**_reserve_kwargs(content_version="2026-09-06", card_style="field_list"))
        with connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT content_version, card_style FROM outreach_message WHERE dedupe_key = %s",
                (DEDUPE,),
            )
            row = cursor.fetchone()
        self.assertEqual((row[0], row[1]), ("2026-09-06", "field_list"))

    def test_a_claim_for_another_recipient_changes_nothing_and_reports_the_old_one(self) -> None:
        """否定断言：同一个去重键换了收件人时一行不改，由调用方拒发。"""
        first = self.store.reserve(**_reserve_kwargs())
        self.store.mark_failed(first.record_id, error="feishu_code_230013")

        claimed = self.store.reserve(**_reserve_kwargs(recipient_open_id="ou_someone_else"))

        self.assertEqual(claimed.record_id, first.record_id)
        self.assertEqual(claimed.recipient_open_id, OPEN_ID)
        self.assertEqual(self._row()[:2], (STATUS_FAILED, 1))
        self.assertEqual(self._count(), 1)

    def test_marking_delivered_twice_reports_no_op_the_second_time(self) -> None:
        """已是终态的行不再改写；返回 ``False`` 让调用方留审计，而不是抛异常。"""
        record = self.store.reserve(**_reserve_kwargs())
        self.assertTrue(self.store.mark_delivered(record.record_id, message_id="om_real"))
        self.assertFalse(self.store.mark_delivered(record.record_id, message_id="om_other"))
        self.assertEqual(self._row()[2], "om_real")

    def test_marking_an_absent_record_delivered_is_a_no_op(self) -> None:
        self.assertFalse(self.store.mark_delivered("omr_not_there", message_id="om_real"))

    def test_the_database_freezes_the_delivery_time_of_a_delivered_row(self) -> None:
        """否定断言：送达时间是双通道核对的依据，事后改不得。"""
        record = self.store.reserve(**_reserve_kwargs())
        self.store.mark_delivered(record.record_id, message_id="om_real")
        with self.assertRaises(Exception):
            with connect(self.dsn) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE outreach_message SET delivered_at = now() + interval '1 day'"
                    " WHERE dedupe_key = %s",
                    (DEDUPE,),
                )

    def test_the_database_freezes_the_platform_id_of_a_delivered_row(self) -> None:
        record = self.store.reserve(**_reserve_kwargs())
        self.store.mark_delivered(record.record_id, message_id="om_real")
        with self.assertRaises(Exception):
            with connect(self.dsn) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE outreach_message SET message_id = 'om_forged' WHERE dedupe_key = %s",
                    (DEDUPE,),
                )

    def test_the_database_freezes_the_recipient_of_a_delivered_row(self) -> None:
        """否定断言：改掉收件人等于伪造"这张卡发给了谁"。"""
        record = self.store.reserve(**_reserve_kwargs())
        self.store.mark_delivered(record.record_id, message_id="om_real")
        with self.assertRaises(Exception):
            with connect(self.dsn) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE outreach_message SET recipient_open_id = 'ou_other'"
                    " WHERE dedupe_key = %s",
                    (DEDUPE,),
                )

    def test_a_pending_row_still_accepts_the_normal_bookkeeping(self) -> None:
        """冻结只在终态之后生效：未送达的行照常记账，否则重试无从进行。"""
        record = self.store.reserve(**_reserve_kwargs())
        self.store.mark_failed(record.record_id, error="feishu_code_230013")
        self.assertTrue(self.store.mark_delivered(record.record_id, message_id="om_real"))
        self.assertEqual(self._row()[0], STATUS_DELIVERED)

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
                    (
                        index,
                        "fs_x" if index == 0 else f"per_{index}",
                        EMAIL.upper(),
                        name,
                        f"no_{index}",
                        f"rec_{index}",
                    ),
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

    def test_multiple_roster_candidates_do_not_send_even_when_names_match(self):
        self._seed(roster_names=("同名员工", "同名员工"))
        facts = self.subjects.facts_for(EMAIL)
        self.assertEqual(facts.identity_failure_reason, "multiple_candidates")
        self.assertIsNone(facts.user_id)
        self.assertIsNone(facts.permissions)
        self.assertFalse(self._plan().sendable)

    def test_changed_personnel_binding_does_not_send_old_permissions_to_new_identity(self):
        self._seed()
        self.assertTrue(self._plan().sendable)
        with connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE roster_snapshot_row SET personnel_id='new-person'")
        facts = self.subjects.facts_for(EMAIL)
        self.assertEqual(facts.identity_failure_reason, "binding_mismatch")
        self.assertIsNone(facts.permissions)
        self.assertFalse(self._plan().sendable)

    def test_snapshot_count_mismatch_is_unavailable_not_departed(self):
        self._seed()
        with connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE roster_snapshot SET row_count=2")
        facts = self.subjects.facts_for(EMAIL)
        self.assertEqual(facts.identity_failure_reason, "snapshot_unavailable")
        self.assertFalse(self._plan().sendable)

    def test_an_unknown_email_is_an_answer_not_a_failure(self) -> None:
        facts = self.subjects.facts_for("nobody@example.invalid")
        self.assertIsNone(facts.user_id)
        self.assertEqual(facts.roster_names, ())

    def _plan(self):
        return plan_outreach(
            self.subjects.facts_for(EMAIL),
            company_names=COMPANY_NAMES,
            metric_labels=METRIC_LABELS,
            total_company_count=43,
            catalog=default_content_catalog(),
        )

    def test_a_contact_marked_unavailable_is_read_back_and_skipped_by_the_audience_plan(
        self,
    ) -> None:
        """「联系不上」从写侧到名单侧的整条接线：``record_contact_unavailable`` 落下
        ``outbound_unavailable_at`` / ``outbound_unavailable_code`` → ``facts_for``
        把两列原样带回 → ``plan_outreach`` 据此给出 ``contact_unavailable``。

        先证明同一个人在标记之前是可发送的：否则任何别的跳过原因都能让本用例
        假绿。变异锚点：把 ``_SUBJECT_SQL`` 里 ``u.outbound_unavailable_at`` 换成
        ``NULL``（或等价断线），本用例应变红（名单侧重新把这个人算成可发送）。
        """
        self._seed()
        before = self._plan()
        self.assertTrue(before.sendable, before.skip_reason)
        self.assertIsNone(self.subjects.facts_for(EMAIL).outbound_unavailable_at)

        marked_at = datetime.now(UTC)
        written = PostgresAppUserStore(self.dsn).record_contact_unavailable(
            open_id=OPEN_ID, when=marked_at, code="feishu_code_230013"
        )
        self.assertTrue(written)

        facts = self.subjects.facts_for(EMAIL)
        self.assertEqual(facts.outbound_unavailable_at, marked_at)
        self.assertEqual(facts.outbound_unavailable_code, "feishu_code_230013")

        after = self._plan()
        self.assertFalse(after.sendable)
        self.assertEqual(after.skip_reason, SKIP_CONTACT_UNAVAILABLE)
        self.assertTrue(after.active, "跳过原因是联系不上，不是没开通")


if __name__ == "__main__":
    unittest.main()
