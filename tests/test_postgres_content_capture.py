"""`adapters/postgres_content_capture.py` 与迁移 `0069_innertest_content_capture`
的真库断言（Issue #251/#304 批次 3，V-采集-04/08/09）。

只验证真库才能证伪的部分：JSONB 落库与回读、`expires_at` 触发器（90 天结构性
上限，调用方传什么都会被覆盖，`UPDATE` 不能改写 `created_at`/`task_id`）、
`task_id` 外键级联删除、以及迁移文件头登记的「轮次结束即清」两条受控 SQL 真的
可执行。凭据形状过滤与 JSON 结构的纯逻辑断言见
``tests/test_innertest_content_capture.py``，不在此重复。
"""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.adapters.postgres_content_capture import PostgresContentCaptureWriter
from lingxi.core.innertest_content_capture import CapturedToolCall, ContentCaptureRecord

SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，内测轮内容级采集的真库断言未验证"
    if not os.environ.get("LINGXI_POSTGRES_DSN")
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，内测轮内容级采集的真库断言未验证"
)


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN") and psycopg_available(), SKIP_REASON)
class ContentCapturePostgresTestCase(unittest.TestCase):
    """本文件全部真库用例的共同底座。"""

    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        cls._psycopg = psycopg
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        reset_production_rows(self._dsn)
        self.writer = PostgresContentCaptureWriter(self._dsn)
        self._connection = self._psycopg.connect(self._dsn, autocommit=True)
        self.addCleanup(self._connection.close)
        self.execute(
            """INSERT INTO app_user
               (id, feishu_open_id, feishu_user_id, feishu_union_id,
                display_name, department, tenant_key, provisioning_state)
               VALUES ('usr-1','ou-1','u-1','un-1','张三','数据部','tk-1','active')"""
        )

    def execute(self, sql: str, parameters: tuple = ()) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(sql, parameters)

    def fetchone(self, sql: str, parameters: tuple = ()) -> tuple:
        with self._connection.cursor() as cursor:
            cursor.execute(sql, parameters)
            return cursor.fetchone()

    def seed_task(self, *, task_id: str, conversation_id: str | None = None) -> None:
        conversation_id = conversation_id or f"conv-{task_id}"
        self.execute(
            """INSERT INTO conversation (id, user_id, feishu_chat_id, feishu_thread_id)
               VALUES (%s, 'usr-1', %s, %s) ON CONFLICT (id) DO NOTHING""",
            (conversation_id, f"chat-{conversation_id}", f"topic-{conversation_id}"),
        )
        self.execute(
            """INSERT INTO task
               (id, conversation_id, user_id, inbound_event_id, prompt, status,
                target_worker_version, attempts, content_expires_at)
               VALUES (%s, %s, 'usr-1', %s, '问题', 'succeeded', 'stable', 1, now())""",
            (task_id, conversation_id, f"event-{task_id}"),
        )

    def sample_record(self, *, task_id: str, worker_id: str = "worker-1") -> ContentCaptureRecord:
        return ContentCaptureRecord(
            task_id=task_id,
            worker_id=worker_id,
            question_content="上周新增用户数是多少",
            question_redaction_count=0,
            answer_content="上周新增用户数是 1234",
            answer_redaction_count=0,
            tool_calls=(
                CapturedToolCall(
                    tool_use_id="t1",
                    tool_name="mcp__query__list_metrics",
                    tool_input={"metric": "new_users"},
                    result_summary={"result_kind": "ok", "content": "ok", "truncated": False},
                    redaction_count=0,
                ),
            ),
        )


class WriteAndReadBackTests(ContentCapturePostgresTestCase):
    def test_write_then_read_back_matches_the_record(self) -> None:
        self.seed_task(task_id="t1")
        record = self.sample_record(task_id="t1")

        row_id = self.writer.write(record)

        rows = self.writer.read_recent_for_task("t1")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], row_id)
        self.assertTrue(row_id.startswith("icc_"))
        self.assertEqual(row["task_id"], "t1")
        self.assertEqual(row["worker_id"], "worker-1")
        self.assertEqual(row["question_content"], "上周新增用户数是多少")
        self.assertEqual(row["answer_content"], "上周新增用户数是 1234")
        self.assertEqual(row["question_redaction_count"], 0)
        self.assertEqual(row["tool_calls"], record.tool_calls_payload())
        self.assertEqual(row["tool_calls_redaction_count"], 0)

    def test_multiple_rows_for_the_same_task_are_ordered_newest_first_and_respect_limit(self) -> None:
        self.seed_task(task_id="t1")
        self.writer.write(self.sample_record(task_id="t1", worker_id="worker-a"))
        self.writer.write(self.sample_record(task_id="t1", worker_id="worker-b"))
        self.writer.write(self.sample_record(task_id="t1", worker_id="worker-c"))

        rows = self.writer.read_recent_for_task("t1", limit=2)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["worker_id"], "worker-c")
        self.assertEqual(rows[1]["worker_id"], "worker-b")

    def test_a_task_with_no_capture_returns_an_empty_list(self) -> None:
        self.seed_task(task_id="t1")

        self.assertEqual(self.writer.read_recent_for_task("t1"), [])


class ExpiryTriggerTests(ContentCapturePostgresTestCase):
    """V-采集-08：``expires_at`` 结构性固定为 ``created_at + 2160`` 小时。"""

    def test_expires_at_is_fixed_to_ninety_days_after_created_at_on_insert(self) -> None:
        self.seed_task(task_id="t1")
        row_id = self.writer.write(self.sample_record(task_id="t1"))

        created_at, expires_at = self.fetchone(
            "SELECT created_at, expires_at FROM innertest_content_capture WHERE id = %s", (row_id,)
        )

        self.assertEqual(expires_at - created_at, timedelta(hours=2160))

    def test_update_cannot_move_expires_at_regardless_of_what_is_written(self) -> None:
        self.seed_task(task_id="t1")
        row_id = self.writer.write(self.sample_record(task_id="t1"))
        far_future = datetime.now(UTC) + timedelta(days=3650)

        self.execute(
            "UPDATE innertest_content_capture SET expires_at = %s WHERE id = %s",
            (far_future, row_id),
        )

        created_at, expires_at = self.fetchone(
            "SELECT created_at, expires_at FROM innertest_content_capture WHERE id = %s", (row_id,)
        )
        self.assertEqual(expires_at - created_at, timedelta(hours=2160))
        self.assertNotEqual(expires_at, far_future)

    def test_update_cannot_change_created_at(self) -> None:
        self.seed_task(task_id="t1")
        row_id = self.writer.write(self.sample_record(task_id="t1"))

        with self.assertRaises(self._psycopg.errors.RaiseException):
            self.execute(
                "UPDATE innertest_content_capture SET created_at = now() - INTERVAL '1 day' WHERE id = %s",
                (row_id,),
            )

    def test_update_cannot_change_task_id(self) -> None:
        self.seed_task(task_id="t1")
        self.seed_task(task_id="t2")
        row_id = self.writer.write(self.sample_record(task_id="t1"))

        with self.assertRaises(self._psycopg.errors.RaiseException):
            self.execute(
                "UPDATE innertest_content_capture SET task_id = 't2' WHERE id = %s", (row_id,)
            )


class TaskCascadeDeleteTests(ContentCapturePostgresTestCase):
    def test_deleting_the_task_cascades_to_its_capture_rows(self) -> None:
        self.seed_task(task_id="t1")
        self.writer.write(self.sample_record(task_id="t1"))

        self.execute("DELETE FROM task WHERE id = 't1'")

        self.assertEqual(self.writer.read_recent_for_task("t1"), [])


class RoundEndCleanupTests(ContentCapturePostgresTestCase):
    """迁移 0069 文件头登记的「轮次结束即清」两条受控 SQL：真实可执行、清理后
    其它表不受影响（V-采集-09）。"""

    def test_windowed_delete_clears_only_the_targeted_time_range(self) -> None:
        self.seed_task(task_id="t-old")
        self.seed_task(task_id="t-new")
        # t-old 的一行直接按早于本轮的历史时间戳插入——触发器只锁 UPDATE 改写
        # created_at（V-采集-08），INSERT 时显式提供的 created_at 本身可控，
        # 这正是"轮次结束即清"要用来筛选历史行的那个字段。适配器的 write()
        # 不接受自定义 created_at（真实写入路径永远是"此刻"），因此这里绕过
        # 适配器直接建一行，只为构造测试夹具。
        old_row = "icc_old0000000000000000000001"
        self.execute(
            """
            INSERT INTO innertest_content_capture
                (id, task_id, worker_id, question_content, answer_content,
                 tool_calls, created_at)
            VALUES (%s, 't-old', 'worker-1', '历史问题', '历史回答', '[]'::jsonb,
                    now() - INTERVAL '10 days')
            """,
            (old_row,),
        )
        self.writer.write(self.sample_record(task_id="t-new"))
        round_start = datetime.now(UTC) - timedelta(days=1)
        round_end = datetime.now(UTC) + timedelta(days=1)

        self.execute(
            "DELETE FROM innertest_content_capture WHERE created_at >= %s AND created_at < %s",
            (round_start, round_end),
        )

        self.assertEqual(self.writer.read_recent_for_task("t-new"), [])
        remaining = self.fetchone(
            "SELECT count(*) FROM innertest_content_capture WHERE id = %s", (old_row,)
        )
        self.assertEqual(remaining[0], 1, "窗口外的历史行不应被本轮清理动到")

    def test_truncate_clears_the_table_without_touching_task(self) -> None:
        self.seed_task(task_id="t1")
        self.writer.write(self.sample_record(task_id="t1"))

        self.execute("TRUNCATE innertest_content_capture")

        self.assertEqual(self.writer.read_recent_for_task("t1"), [])
        task_row = self.fetchone("SELECT count(*) FROM task WHERE id = 't1'")
        self.assertEqual(task_row[0], 1, "清空采集表不得影响 task 本身")


if __name__ == "__main__":
    unittest.main()
