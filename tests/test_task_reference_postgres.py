"""#657 独占合成真库：精确关联、并发收口、源与任务各自期限。"""

from __future__ import annotations

import io
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from functools import partial
from unittest.mock import Mock

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.adapters.admin_registry import PostgresAdminQueries
from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_conversation import PostgresTaskQueue
from lingxi.apps import trace
from lingxi.core.admin.router_render import render_trace
from lingxi.core.task_reference import reference_fields

DSN = os.environ.get("LINGXI_POSTGRES_DSN")
A = "01J00000000000000000000001"
B = "01J00000000000000000000002"
C = "01J00000000000000000000003"


@unittest.skipUnless(DSN and psycopg_available(), "需要独占合成 PostgreSQL")
class TaskReferencePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_production_schema(DSN)

    def setUp(self):
        reset_production_rows(DSN)
        self.queue = PostgresTaskQueue(DSN)
        self.queries = PostgresAdminQueries(DSN)
        for user in ("a", "b"):
            self.sql(
                """INSERT INTO app_user (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name, department, tenant_key)
                        VALUES (%s, %s, 'synthetic', 'synthetic', '合成人物', '合成部门', 'synthetic')""",
                ("usr_" + user, "ou_" + user),
            )

    def sql(self, sql, params=()):
        with connect(DSN) as conn, conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall() if cursor.description else []

    def seed(self, suffix, source=None, user="a", expired=False, version="stable", age="0 hours"):
        task = "tsk_" + suffix
        event = "event-" + suffix
        if source is not None:
            self.sql(
                """INSERT INTO inbound_event
                (feishu_event_id, event_type, user_open_id, trace_id, received_at)
                VALUES (%s, 'im.message.receive_v1', %s, %s,
                        now() + CASE WHEN %s THEN interval '-2161 hours' ELSE interval '0 hours' END)""",
                (event, "ou_" + user, source, expired),
            )
        self.sql(
            """INSERT INTO conversation (id, user_id, feishu_chat_id, running_task_id)
                    VALUES (%s, %s, %s, %s)""",
            ("cnv_" + suffix, "usr_" + user, "chat-" + suffix, task),
        )
        self.sql(
            """INSERT INTO task (id, conversation_id, user_id, inbound_event_id, prompt,
                    status, target_worker_version, created_at)
                    VALUES (%s, %s, %s, %s, '合成秘密问题', 'queued', %s, now()-%s::interval)""",
            (task, "cnv_" + suffix, "usr_" + user, event, version, age),
        )
        return task

    def cli(self, reference):
        out, err = io.StringIO(), io.StringIO()
        result = trace.run([reference], env={trace.DSN_ENV_VAR: DSN}, stdout=out, stderr=err)
        self.assertEqual(result, 0, err.getvalue())
        self.assertNotIn("合成秘密问题", out.getvalue())
        self.assertNotIn("ou_", out.getvalue())
        return out.getvalue()

    def test_concurrent_claim_exact_join_context_delivery_and_missing_source(self):
        expected = {
            self.seed(C, A): A,
            self.seed(B, B, user="b"): B,
            self.seed(A, C): C,
            self.seed("01J00000000000000000000004"): None,
        }
        barrier = threading.Barrier(2)

        def claim(worker):
            barrier.wait()
            return worker, PostgresTaskQueue(DSN).claim(
                worker_id=worker, target_worker_version="stable", limit=3
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            batches = list(pool.map(claim, ["w1", "w2"]))
        claimed = [task.task_id for _, batch in batches for task in batch]
        self.assertCountEqual(claimed, expected)
        self.assertEqual(len(set(claimed)), len(expected))
        for worker, batch in batches:
            for task in batch:
                self.assertEqual(task.trace_id, expected[task.task_id])
                self.assertIsNotNone(task.task_created_at)
                self.assertEqual(task.target_worker_version, "stable")
                context = self.queue.task_context(task_id=task.task_id, worker_id=worker)
                self.assertEqual(context.trace_id, task.trace_id)
                self.assertEqual(context.task_created_at, task.task_created_at)
                self.queue.append_delivery_event(
                    task_id=task.task_id,
                    worker_id=worker,
                    event_type="started",
                    idempotency_key=task.task_id + ":started",
                )
        pending = self.queue.list_pending_delivery_tasks()
        self.assertEqual({t.task_id: t.trace_id for t in pending}, expected)
        self.assertTrue(all(t.task_created_at is not None for t in pending))
        for task in pending:
            self.assertTrue(self.queue.reserve_dispatch(task_id=task.task_id, kind="card_create"))
        uncertain = self.queue.list_uncertain_delivery_tasks()
        self.assertEqual({t.task_id: t.trace_id for t in uncertain}, expected)

    def test_claim_does_not_lock_source_and_preserves_version_filter(self):
        self.seed(A, A)
        other = self.seed(B, B, version="canary")
        # 源事件已被另一事务锁住：领取仍可立即完成，证明没有源行锁。
        with connect(DSN) as conn, conn.cursor() as cursor:
            cursor.execute("SELECT * FROM inbound_event WHERE trace_id=%s FOR UPDATE", (A,))
            with ThreadPoolExecutor(max_workers=1) as pool:
                batch = pool.submit(
                    self.queue.claim, worker_id="w", target_worker_version="stable"
                ).result(3)
        self.assertEqual([t.task_id for t in batch], ["tsk_" + A])
        self.assertEqual(self.sql("SELECT status FROM task WHERE id=%s", (other,))[0][0], "queued")

    def test_expired_and_invalid_source_are_safe_fallback(self):
        for suffix, source, expired in (
            (A, A, True),
            (B, "邮箱@example.com", False),
            (C, None, False),
        ):
            self.seed(suffix, source, expired=expired)
        tasks = self.queue.claim(worker_id="w", target_worker_version="stable", limit=3)
        for task in tasks:
            self.assertIsNone(task.trace_id)
            context = self.queue.task_context(task_id=task.task_id, worker_id="w")
            self.assertIsNone(context.trace_id)
            fields = reference_fields(task.task_id, context.trace_id)
            self.assertEqual(fields["reference"], "T-" + task.task_id[4:])
            self.queue.append_delivery_event(
                task_id=task.task_id,
                worker_id="w",
                event_type="started",
                idempotency_key=task.task_id + ":started",
            )
        pending = self.queue.list_pending_delivery_tasks()
        self.assertEqual(len(pending), 3)
        self.assertTrue(all(t.trace_id is None for t in pending))
        for task in pending:
            self.assertTrue(self.queue.reserve_dispatch(task_id=task.task_id, kind="card_create"))
        self.assertTrue(all(t.trace_id is None for t in self.queue.list_uncertain_delivery_tasks()))

    def test_system_terminal_queued_timeout_version_and_reclaim_repeat(self):
        for source in (A, None):
            for kind in (
                "queued_timeout",
                "worker_version_unavailable",
                "retry_exhausted",
                "side_effect_uncertain",
            ):
                reset_production_rows(DSN)
                self.sql(
                    "INSERT INTO app_user (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name, department, tenant_key) VALUES ('usr_a','ou_a','synthetic','synthetic','合成人物','合成部门','synthetic')"
                )
                task = self.seed(B, source, age="1 hour")
                if kind in {"retry_exhausted", "side_effect_uncertain"}:
                    self.queue.claim(worker_id="w", target_worker_version="stable")
                    self.sql(
                        "UPDATE task SET heartbeat_at=now()-interval '1 hour', attempts=2, side_effect_state=%s WHERE id=%s",
                        ("possible" if kind == "side_effect_uncertain" else "none", task),
                    )
                    operation = partial(self.queue.reclaim_stale, older_than=timedelta(minutes=1))
                elif kind == "queued_timeout":
                    operation = partial(self.queue.reclaim_queued, max_wait=timedelta(minutes=1))
                else:
                    operation = partial(
                        self.queue.fail_unavailable_versions,
                        available_versions=[],
                        unavailable_for=timedelta(minutes=1),
                    )
                with ThreadPoolExecutor(max_workers=2) as pool:
                    list(pool.map(lambda _: operation(), range(2)))
                rows = self.sql(
                    "SELECT content FROM task_delivery_event WHERE task_id=%s AND event_type='terminal'",
                    (task,),
                )
                self.assertEqual(len(rows), 1)
                self.assertIn(source or "T-" + B, rows[0][0])
                operation()
                self.assertEqual(
                    self.sql(
                        "SELECT content FROM task_delivery_event WHERE task_id=%s AND event_type='terminal'",
                        (task,),
                    ),
                    rows,
                )

    def test_old_persisted_terminal_is_not_rendered_again(self):
        task = self.seed(B)
        self.queue.claim(worker_id="w", target_worker_version="stable")
        self.queue.write_terminal_event(
            task_id=task,
            worker_id="w",
            terminal_kind="failed",
            error_kind="session_failed",
            content="旧版诚实失败，没有号码",
            elapsed_seconds=1,
        )
        with connect(DSN) as conn, conn.cursor() as cursor:
            cursor.execute("SELECT id FROM task WHERE id=%s FOR UPDATE", (task,))
            with unittest.mock.patch.object(
                self.queue._content_catalog, "text", side_effect=AssertionError("不得重渲染")
            ):
                self.assertFalse(
                    self.queue._write_system_terminal(
                        cursor, task_id=task, error_kind="queued_timeout", from_status="queued"
                    )
                )
        self.assertEqual(
            len(
                self.sql(
                    "SELECT * FROM task_delivery_event WHERE task_id=%s AND event_type='terminal'",
                    (task,),
                )
            ),
            1,
        )
        self.assertEqual(
            self.queue.read_delivery_events(task_id=task, after_sequence=0)[0].content,
            "旧版诚实失败，没有号码",
        )

    def test_both_queries_exact_task_and_event_lifetimes(self):
        task = self.seed(A, B, age="2159 hours 59 minutes")
        self.seed(C, C, user="b")
        self.sql(
            "UPDATE task SET error_kind='session_failed', failure_code='turn_timeout', failure_signature='姓名@example.com' WHERE id=%s",
            (task,),
        )
        view = self.queries.trace_lookup(trace_id="T-" + A)
        self.assertEqual(view.reference_kind, "task")
        self.assertIsNone(view.event_count)
        self.assertEqual(view.task_failure_code, "turn_timeout")
        self.assertEqual(view.task_failure_signature, "unknown")
        self.assertNotIn("入站事件", render_trace("T-" + A, view))
        self.assertIn("turn_timeout", self.cli("T-" + A))
        self.assertIn("turn_timeout", self.cli(B))
        self.sql("DELETE FROM inbound_event WHERE trace_id=%s", (B,))
        self.sql(
            """INSERT INTO inbound_event (feishu_event_id, event_type, user_open_id, trace_id, received_at)
                    VALUES (%s, 'im.message.receive_v1', 'ou_a', %s, now()-interval '2161 hours')""",
            ("event-" + A, B),
        )
        self.assertIsNone(self.queries.trace_lookup(trace_id=B))
        self.assertIn("查无此追溯号", self.cli(B))
        self.assertIsNotNone(self.queries.trace_lookup(trace_id="T-" + A))
        self.assertIn("turn_timeout", self.cli("T-" + A))
        # 恢复源事件不改变 T- 查询的目标或原任务创建时间。
        self.sql("DELETE FROM inbound_event WHERE trace_id=%s", (B,))
        self.sql(
            """INSERT INTO inbound_event (feishu_event_id, event_type, user_open_id, trace_id)
                    VALUES (%s, 'im.message.receive_v1', 'ou_a', %s)""",
            ("event-" + A, B),
        )
        for index, age in enumerate(("2160 hours", "2161 hours")):
            suffix = f"01J{index + 8:023d}"
            source = f"01K{index + 8:023d}"
            expired_task = self.seed(suffix, source, age=age)
            self.sql("UPDATE task SET failure_code='turn_timeout' WHERE id=%s", (expired_task,))
            self.assertIsNone(self.queries.trace_lookup(trace_id="T-" + suffix))
            self.assertIn("查无此追溯号", self.cli("T-" + suffix))
            self.assertIsNone(self.queries.trace_lookup(trace_id=source).task_status)
            self.assertNotIn("turn_timeout", self.cli(source))
        self.assertIsNotNone(self.queries.trace_lookup(trace_id="T-" + C))

    def test_invalid_admin_adapter_does_not_query(self):
        with unittest.mock.patch(
            "lingxi.adapters.admin_registry.connect", Mock(side_effect=AssertionError("零查询"))
        ) as factory:
            for bad in ("T-", "T-T-" + A, "tsk_" + A, "姓名", "a@example.com", "'; SELECT 1--"):
                self.assertIsNone(self.queries.trace_lookup(trace_id=bad))
            factory.assert_not_called()

    def test_followup_unknown_is_visible_without_inbound_event(self):
        from datetime import UTC, datetime

        from lingxi.adapters.postgres_admin_followup import PostgresFollowupStore, enqueue_followups
        from lingxi.core.admin.followup import FollowupSpec

        self.sql("""INSERT INTO pending_action(id,action_type,target_open_id,initiated_by_open_id,
            target_state_snapshot,status,created_at,confirm_deadline_at) VALUES
            ('pac_ref','suspend_user','ou_synthetic','ou_admin','enabled','pending',now(),now()+interval '1 hour')""")
        with connect(DSN) as conn:
            enqueue_followups(
                conn,
                pending_action_id="pac_ref",
                trace_id=A,
                items=(FollowupSpec(subject_key="synthetic", stage="group_notify"),),
            )
        store = PostgresFollowupStore(DSN)
        item = store.claim_followup(
            consumer_kind="gateway", owner="synthetic", now=datetime.now(UTC) + timedelta(seconds=1)
        )
        self.assertTrue(
            store.complete_followup(
                id=item.id,
                owner="synthetic",
                attempt=item.attempt,
                status="unknown",
                result_code="unknown",
            )
        )
        view = self.queries.trace_lookup(trace_id=A)
        self.assertIsNone(view.event_count)
        self.assertEqual(len(view.followups), 1)
        text = render_trace(A, view)
        self.assertIn("结果待核实", text)
        self.assertIn(A, text)
        self.assertIn(item.id, text)
        self.assertNotIn("入站事件", text)
        self.assertIsNone(self.queries.trace_lookup(trace_id=B))
        self.sql("UPDATE admin_action_followup SET created_at=now()-interval '91 days'")
        still_visible = self.queries.trace_lookup(trace_id=A)
        self.assertIsNotNone(still_visible)
        self.assertEqual([row.followup_id for row in still_visible.followups], [item.id])
        self.sql("DELETE FROM pending_action WHERE id='pac_ref'")
        self.sql("""INSERT INTO pending_action(id,action_type,target_open_id,initiated_by_open_id,
            target_state_snapshot,status,created_at,confirm_deadline_at) VALUES
            ('pac_ref','suspend_user','ou_synthetic','ou_admin','enabled','pending',
             now()-interval '91 days',now()-interval '91 days'+interval '1 hour')""")
        with connect(DSN) as conn:
            enqueue_followups(
                conn,
                pending_action_id="pac_ref",
                trace_id=A,
                items=(FollowupSpec(subject_key="synthetic", stage="group_notify"),),
            )
        self.assertEqual(
            self.sql("""SELECT f.created_at > p.created_at + interval '90 days'
                FROM admin_action_followup f JOIN pending_action p ON p.id=f.pending_action_id
                WHERE p.id='pac_ref'"""),
            [(True,)],
        )
        self.assertIsNone(self.queries.trace_lookup(trace_id=A))
