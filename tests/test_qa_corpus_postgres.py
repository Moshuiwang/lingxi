"""`adapters/postgres_qa_corpus.py` 与迁移 `0098_qa_corpus` 的真库断言（Issue #664）。

只验证真库才能证伪的部分：写入与逐列回读、``task_id`` 冲突不双写、``UPDATE`` 一律被
触发器拒绝、用户删除随删（裁定前默认）、表上**没有** ``expires_at`` 且既有全部保留
职责跑过之后二百天前的语料仍在、关键词索引真的建在两列上、迁移的降级边界。纯逻辑
断言见 ``tests/test_qa_corpus_record.py``。
"""

from __future__ import annotations

import logging
import os
import re
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

from postgres_schema import (
    ALEMBIC_INI,
    REPOSITORY_ROOT,
    ensure_production_schema,
    psycopg_available,
    reset_production_rows,
)

from lingxi.core.innertest_content_capture import CapturedToolCall
from lingxi.core.qa_corpus import QaCorpusRecord

SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，问答留存语料的真库断言未验证"
    if not os.environ.get("LINGXI_POSTGRES_DSN")
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，问答留存语料的真库断言未验证"
)
OWNER_ID = "usr_qa_corpus_owner"
_FAKE_TOKEN = "sk-fake-token-1234567890abcdef"


def sample_record(*, task_id: str, user_id: str = OWNER_ID, **overrides) -> QaCorpusRecord:
    values = {
        "task_id": task_id,
        "conversation_id": f"cnv_{task_id}",
        "user_id": user_id,
        "trace_id": "01HXYZTRACE00000000000000A",
        "task_created_at": datetime(2026, 9, 14, 8, 0, tzinfo=UTC),
        "question_content": "上周新增用户数是多少",
        "question_redaction_count": 0,
        "answer_delivered": "上周新增用户数是 1234。",
        "answer_delivered_redaction_count": 0,
        "answer_model_raw": "上周新增用户数是 1234。（模型原文）",
        "answer_model_raw_redaction_count": 0,
        "tool_calls": (
            CapturedToolCall(
                tool_use_id="t1",
                tool_name="mcp__query__list_metrics",
                tool_input={"metric": "new_users"},
                result_summary={"result_kind": "ok", "content": "ok", "truncated": False},
                redaction_count=1,
            ),
        ),
        "terminal_kind": "success",
        "user_result": "obtained",
        "failure_code": None,
        "output_safety_withheld": False,
        "worker_id": "worker-1",
        "worker_version": "2.5.0",
        "target_worker_version": "stable",
        "system_prompt_digest": "sha256:abc",
        "model": "model-x",
    }
    values.update(overrides)
    return QaCorpusRecord(**values)


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN") and psycopg_available(), SKIP_REASON)
class QaCorpusPostgresTestCase(unittest.TestCase):
    """本文件真库用例的共同底座：整链建库、清行、种一个用户。"""

    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        cls._psycopg = psycopg
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        from lingxi.adapters.postgres_qa_corpus import PostgresQaCorpus

        reset_production_rows(self._dsn)
        self.store = PostgresQaCorpus(self._dsn)
        self._connection = self._psycopg.connect(self._dsn, autocommit=True)
        self.addCleanup(self._connection.close)
        self.seed_user(OWNER_ID)

    def execute(self, sql: str, parameters: tuple = ()) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(sql, parameters)

    def fetch(self, sql: str, parameters: tuple = ()) -> list[tuple]:
        with self._connection.cursor() as cursor:
            cursor.execute(sql, parameters)
            return list(cursor.fetchall())

    def seed_user(self, user_id: str) -> None:
        self.execute(
            """INSERT INTO app_user
               (id, feishu_open_id, feishu_user_id, feishu_union_id,
                display_name, department, tenant_key, provisioning_state)
               VALUES (%s, %s, %s, %s, '化名甲', '数据部', 'tk_qa', 'active')
               ON CONFLICT (id) DO NOTHING""",
            (user_id, f"ou_{user_id}", f"u_{user_id}", f"un_{user_id}"),
        )

    def task_ids(self) -> list[str]:
        return [row[0] for row in self.fetch("SELECT task_id FROM qa_corpus ORDER BY task_id")]


class WriteAndReadBackTests(QaCorpusPostgresTestCase):
    def test_record_then_read_back_every_column(self) -> None:
        record = sample_record(task_id="tsk_qa_1")

        self.assertTrue(self.store.record(record))

        row = self.store.for_task("tsk_qa_1")
        self.assertIsNotNone(row)
        self.assertTrue(row.id.startswith("qac_"))
        self.assertEqual(row.record, record)
        self.assertEqual(row.record.tool_calls_redaction_count, 1)
        self.assertIsNotNone(row.created_at.tzinfo)
        self.assertIsNone(self.store.for_task("tsk_missing"))

    def test_a_reclaimed_task_does_not_write_twice(self) -> None:
        """``ON CONFLICT (task_id) DO NOTHING``：重领任务的第二次写入不覆盖、不重复。"""

        first = sample_record(task_id="tsk_qa_1", answer_delivered="第一次的实收正文")
        second = sample_record(task_id="tsk_qa_1", answer_delivered="重领之后的正文")

        self.assertTrue(self.store.record(first))
        self.assertFalse(self.store.record(second))

        self.assertEqual(self.task_ids(), ["tsk_qa_1"])
        self.assertEqual(
            self.store.for_task("tsk_qa_1").record.answer_delivered, "第一次的实收正文"
        )

    def test_only_records_are_accepted_by_both_writers(self) -> None:
        from lingxi.adapters.postgres_qa_corpus import record_qa_corpus

        with self.assertRaises(TypeError):
            self.store.record({"task_id": "tsk_qa_1"})  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            record_qa_corpus(self._connection, object())  # type: ignore[arg-type]
        self.assertEqual(self.task_ids(), [])

    def test_the_connection_level_writer_lives_and_dies_with_the_caller_transaction(self) -> None:
        from lingxi.adapters.postgres import connect
        from lingxi.adapters.postgres_qa_corpus import record_qa_corpus

        with connect(self._dsn) as connection:
            with connection.transaction():
                self.assertTrue(record_qa_corpus(connection, sample_record(task_id="tsk_qa_kept")))
            try:
                with connection.transaction():
                    self.assertTrue(
                        record_qa_corpus(connection, sample_record(task_id="tsk_qa_rolled"))
                    )
                    raise RuntimeError("回滚")
            except RuntimeError:
                pass

        self.assertEqual(self.task_ids(), ["tsk_qa_kept"])


class StructuralGuaranteeTests(QaCorpusPostgresTestCase):
    """迁移 ``0098`` 给出的结构性保证。"""

    def test_every_update_is_rejected_by_the_trigger(self) -> None:
        self.store.record(sample_record(task_id="tsk_qa_1"))

        for statement in (
            "UPDATE qa_corpus SET question_content = '改写' WHERE task_id = 'tsk_qa_1'",
            "UPDATE qa_corpus SET created_at = now() - interval '1 day'",
            "UPDATE qa_corpus SET user_id = user_id",
        ):
            with self.subTest(statement=statement):
                with self.assertRaises(self._psycopg.errors.RaiseException):
                    self.execute(statement)
        self.assertEqual(
            self.store.for_task("tsk_qa_1").record.question_content, "上周新增用户数是多少"
        )

    def test_deleting_the_user_removes_that_users_corpus_only(self) -> None:
        """裁定前默认：用户从当前环境删除时语料随删（外键级联），其他人的一行不动。"""

        self.seed_user("usr_qa_corpus_other")
        self.store.record(sample_record(task_id="tsk_qa_1"))
        self.store.record(sample_record(task_id="tsk_qa_2", user_id="usr_qa_corpus_other"))

        self.execute("DELETE FROM app_user WHERE id = %s", (OWNER_ID,))

        self.assertEqual(self.task_ids(), ["tsk_qa_2"])

    def test_the_table_has_no_expiry_column_and_no_retention_adapter_mentions_it(self) -> None:
        """「不到期」是结构性的：无键可扫，也没有任何清理适配器引用这张表。"""

        columns = {
            row[0]
            for row in self.fetch(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = 'public' AND table_name = 'qa_corpus'"
            )
        }
        self.assertNotIn("expires_at", columns)
        self.assertFalse({name for name in columns if "expir" in name or "retention" in name})

        adapters = REPOSITORY_ROOT / "src" / "lingxi" / "adapters"
        retention_modules = sorted(adapters.glob("*retention*.py"))
        self.assertTrue(retention_modules)
        for module in retention_modules:
            with self.subTest(module=module.name):
                self.assertNotIn("qa_corpus", module.read_text(encoding="utf-8"))
        migration = REPOSITORY_ROOT / "migrations" / "alembic" / "versions" / "0098_qa_corpus.py"
        ddl = re.search(r"CREATE TABLE qa_corpus \((.*?)\);", migration.read_text("utf-8"), re.S)
        self.assertIsNotNone(ddl)
        self.assertNotIn("expires_at", ddl.group(1))

    def test_every_existing_retention_duty_leaves_a_two_hundred_day_old_row_alone(self) -> None:
        """既有保留职责逐个跑过：内测采集表的到期行被删，语料一列都不变。"""

        from lingxi.adapters.postgres import connect
        from lingxi.adapters.postgres_carrier_retention import PostgresCarrierRetention
        from lingxi.adapters.postgres_content_capture_retention import (
            PostgresContentCaptureRetention,
        )
        from lingxi.adapters.postgres_innertest_retention import purge_innertest_history
        from lingxi.adapters.postgres_late_readiness_recovery import PostgresLateReadinessStore
        from lingxi.adapters.postgres_operation_audit import purge_expired_operation_audit
        from lingxi.adapters.postgres_permission_publish import PostgresPermissionPublishStore
        from lingxi.adapters.retention import PostgresRetentionCleaner

        now = datetime.now(UTC)
        self.store.record(sample_record(task_id="tsk_qa_old"))
        # 造「二百天前」的行只能绕过只追加触发器：这里是测试夹具，不是产品路径。
        self.execute("ALTER TABLE qa_corpus DISABLE TRIGGER qa_corpus_no_update")
        self.execute("UPDATE qa_corpus SET created_at = %s", (now - timedelta(days=200),))
        self.execute("ALTER TABLE qa_corpus ENABLE TRIGGER qa_corpus_no_update")
        self._seed_expired_innertest_capture(now)
        before = self.fetch("SELECT qa_corpus::text FROM qa_corpus")

        self.assertEqual(PostgresContentCaptureRetention(self._dsn).purge_expired(now=now), 1)
        carriers = PostgresCarrierRetention(self._dsn)
        carriers.redact_expired_task_prompts(now=now)
        carriers.purge_expired_inbound_events(now=now)
        carriers.redact_expired_pending_actions(now=now)
        carriers.purge_expired_queue_failure_notices(now=now)
        PostgresRetentionCleaner(self._dsn).run_once()
        PostgresPermissionPublishStore(self._dsn).redact_expired_payloads()
        PostgresLateReadinessStore(self._dsn).purge_expired_notices()
        with connect(self._dsn) as connection:
            with connection.transaction():
                purge_expired_operation_audit(connection, now=now)
                purge_innertest_history(connection, now=now, limit=100)

        self.assertEqual(self.fetch("SELECT qa_corpus::text FROM qa_corpus"), before)
        self.assertEqual(self.fetch("SELECT count(*) FROM innertest_content_capture"), [(0,)])

    def _seed_expired_innertest_capture(self, now: datetime) -> None:
        self.execute(
            """INSERT INTO conversation (id, user_id, feishu_chat_id, feishu_thread_id)
               VALUES ('cnv_qa_old', %s, 'chat_qa_old', 'topic_qa_old')""",
            (OWNER_ID,),
        )
        self.execute(
            """INSERT INTO task
               (id, conversation_id, user_id, inbound_event_id, prompt, status,
                target_worker_version, attempts, content_expires_at)
               VALUES ('tsk_qa_old', 'cnv_qa_old', %s, 'event_qa_old', '问题', 'succeeded',
                       'stable', 1, now())""",
            (OWNER_ID,),
        )
        expired = now - timedelta(days=91)
        self.execute(
            """INSERT INTO innertest_content_capture
               (id, task_id, worker_id, question_content, answer_content, created_at, expires_at)
               VALUES ('icc_expired', 'tsk_qa_old', 'wkr', '问题原文', '回答原文', %s, %s)""",
            (expired, expired),
        )

    def test_keyword_indexes_exist_on_the_two_searchable_columns_only(self) -> None:
        definitions = {
            row[0]: row[1]
            for row in self.fetch(
                "SELECT indexname, indexdef FROM pg_indexes"
                " WHERE schemaname = 'public' AND tablename = 'qa_corpus'"
            )
        }
        self.assertIn("gin_trgm_ops", definitions["qa_corpus_question_trgm_idx"])
        self.assertIn("(question_content", definitions["qa_corpus_question_trgm_idx"])
        self.assertIn("(answer_delivered", definitions["qa_corpus_answer_trgm_idx"])
        self.assertFalse(
            [name for name, definition in definitions.items() if "answer_model_raw" in definition]
        )

    def test_no_sample_secret_ever_reaches_the_table(self) -> None:
        """写入侧只收已过滤的记录：真库里回读不到假凭据。"""

        from lingxi.core.execution.audit import redact_free_text_with_count

        question, count = redact_free_text_with_count(f"用 token={_FAKE_TOKEN} 查")
        self.store.record(
            sample_record(
                task_id="tsk_qa_1", question_content=question, question_redaction_count=count
            )
        )

        hits = self.fetch(
            "SELECT count(*) FROM qa_corpus WHERE qa_corpus::text ILIKE %s", (f"%{_FAKE_TOKEN}%",)
        )
        self.assertEqual(hits, [(0,)])
        self.assertGreater(self.store.for_task("tsk_qa_1").record.question_redaction_count, 0)


def _run_alembic(dsn: str, action: str, target: str) -> None:
    """进程内跑 ``alembic <upgrade|downgrade> <target>``，进出各存取一次 logger 的 disabled 位。"""

    from alembic import command
    from alembic.config import Config

    config = Config(str(ALEMBIC_INI))
    previous = os.environ.get("LINGXI_MIGRATION_DSN")
    os.environ["LINGXI_MIGRATION_DSN"] = dsn
    manager = logging.root.manager
    disabled_before = {
        name: logger.disabled
        for name, logger in manager.loggerDict.items()
        if isinstance(logger, logging.Logger)
    }
    try:
        getattr(command, action)(config, target)
    finally:
        if previous is None:
            os.environ.pop("LINGXI_MIGRATION_DSN", None)
        else:
            os.environ["LINGXI_MIGRATION_DSN"] = previous
        for name, was_disabled in disabled_before.items():
            logger = manager.loggerDict.get(name)
            if isinstance(logger, logging.Logger):
                logger.disabled = was_disabled


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN") and psycopg_available(), SKIP_REASON)
class RevisionDowngradeTest(unittest.TestCase):
    """迁移 ``0098`` 的降级边界：任一表有记录就拒绝、一行不丢；两表都空才完整逆转。"""

    PARENT = "0097_operation_audit"
    OBJECTS_SQL = (
        "SELECT to_regclass('public.qa_corpus') IS NOT NULL,"
        " to_regclass('public.qa_corpus_reader') IS NOT NULL,"
        " to_regprocedure('public.qa_corpus_append_only()') IS NOT NULL"
    )

    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        cls._psycopg = psycopg
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]

    def _admin(self, sql: str, dsn: str | None = None) -> list[tuple]:
        with (
            self._psycopg.connect(dsn or self._dsn, autocommit=True) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(sql)
            return cursor.fetchall() if cursor.description else []

    def _probe_database(self) -> str:
        name = f"lingxi_qac_probe_{uuid.uuid4().hex[:8]}"
        self._admin(f"CREATE DATABASE {name}")
        self.addCleanup(self._admin, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        parts = urlsplit(self._dsn)
        return urlunsplit(parts._replace(path=f"/{name}"))

    def test_populated_tables_refuse_downgrade_and_empty_ones_revert_completely(self) -> None:
        probe = self._probe_database()
        _run_alembic(probe, "upgrade", "0098_qa_corpus")
        self._admin(
            "INSERT INTO qa_corpus_reader (id, feishu_open_id, label, granted_by)"
            " VALUES ('qcr_keep', 'ou_reader_fake', 'corpus-reader', 'ou_admin_fake')",
            probe,
        )

        with self.assertRaises(Exception) as caught:
            _run_alembic(probe, "downgrade", self.PARENT)

        self.assertIn("compatible recovery", str(caught.exception))
        self.assertEqual(self._admin(self.OBJECTS_SQL, probe), [(True, True, True)])
        self.assertEqual(self._admin("SELECT id FROM qa_corpus_reader", probe), [("qcr_keep",)])
        self.assertEqual(
            self._admin("SELECT version_num FROM alembic_version", probe), [("0098_qa_corpus",)]
        )

        self._admin("DELETE FROM qa_corpus_reader", probe)
        _run_alembic(probe, "downgrade", self.PARENT)
        self.assertEqual(self._admin(self.OBJECTS_SQL, probe), [(False, False, False)])
        self.assertEqual(
            self._admin("SELECT version_num FROM alembic_version", probe), [(self.PARENT,)]
        )

        _run_alembic(probe, "upgrade", "0098_qa_corpus")
        self.assertEqual(self._admin(self.OBJECTS_SQL, probe), [(True, True, True)])

    def test_a_pre_installed_extension_is_reused_from_its_own_schema(self) -> None:
        """托管方可能已把 pg_trgm 装在别的 schema：迁移沿用它，不再装第二份。"""

        probe = self._probe_database()
        self._admin("CREATE EXTENSION pg_trgm WITH SCHEMA public", probe)

        _run_alembic(probe, "upgrade", "0098_qa_corpus")

        self.assertEqual(
            self._admin(
                "SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace"
                " WHERE e.extname = 'pg_trgm'",
                probe,
            ),
            [("public",)],
        )
        definitions = self._admin(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'qa_corpus_question_trgm_idx'",
            probe,
        )
        self.assertIn("gin_trgm_ops", definitions[0][0])
        self.assertNotIn("extensions.", definitions[0][0])
        self.assertEqual(
            self._admin("SELECT to_regnamespace('extensions') IS NOT NULL", probe), [(False,)]
        )


if __name__ == "__main__":
    unittest.main()
