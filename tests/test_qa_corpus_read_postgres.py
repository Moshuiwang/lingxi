"""问答留存语料读取侧的真库断言（Issue #664 完成标准 2 / 4 / 6）。

只放真库才能证伪的部分：未授权读取在 SQL 层面零查询语料表；读取 / 导出各在运营审计账
落一行且任何列值不含正文；导出摘要等于文件 sha256、条数等于行数；按人 / 时间窗（含起
不含止）/ 关键词（中文子串、`%` `_` 转义）三种检索；`EXPLAIN` 证实三字符以上的关键词走
trgm 索引；授予幂等、撤销后读取被拒、非管理员不能授予、审计失败时读取扣住输出、导出
删文件、授予整笔回滚。样本正文全部合成。
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.core.qa_corpus import CorpusFilter, rows_digest

REPOSITORY_ROOT = Path(__file__).parents[1]
SCRIPT = REPOSITORY_ROOT / "scripts" / "ops" / "qa_corpus.py"
SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，问答留存语料读取侧的真库断言未验证"
    if not os.environ.get("LINGXI_POSTGRES_DSN")
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，问答留存语料读取侧的真库断言未验证"
)

READER = "ou_reader_fake"
ADMIN = "ou_admin_fake"
STRANGER = "ou_stranger_fake"
USER_A = "usr_corpus_a"
USER_B = "usr_corpus_b"
SAMPLE_QUESTION = "合成样本：上季度华东区新增用户数"
SAMPLE_ANSWER = "合成样本：上季度华东区新增用户 4321 人。"
SAMPLE_RAW = "合成样本：模型原文 4321 人（未投影）"
_CORPUS_TABLE = re.compile(r"\bqa_corpus\b(?!_reader)")
T0 = datetime(2026, 9, 10, 0, 0, tzinfo=UTC)


def _load_script() -> Any:
    name = "qa_corpus_ops_postgres_under_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


TOOL = _load_script()


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN") and psycopg_available(), SKIP_REASON)
class CorpusReadPostgresTestCase(unittest.TestCase):
    """整链建库、清行、种两位用户与一位管理员；语料行直接 INSERT 以控制 ``created_at``。"""

    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        cls._psycopg = psycopg
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        from lingxi.adapters.admin_registry import seed_admin_registry_entry
        from lingxi.adapters.postgres_qa_corpus import PostgresQaCorpus

        reset_production_rows(self._dsn)
        self.store = PostgresQaCorpus(self._dsn)
        self._connection = self._psycopg.connect(self._dsn, autocommit=True)
        self.addCleanup(self._connection.close)
        for user_id in (USER_A, USER_B):
            self.execute(
                """INSERT INTO app_user
                   (id, feishu_open_id, feishu_user_id, feishu_union_id,
                    display_name, department, tenant_key, provisioning_state)
                   VALUES (%s, %s, %s, %s, '化名', '数据部', 'tk_qa', 'active')""",
                (user_id, f"ou_{user_id}", f"u_{user_id}", f"un_{user_id}"),
            )
        seed_admin_registry_entry(self._dsn, feishu_open_id=ADMIN, label="ops-oncall")

    def execute(self, sql: str, parameters: tuple = ()) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(sql, parameters)

    def fetch(self, sql: str, parameters: tuple = ()) -> list[tuple]:
        with self._connection.cursor() as cursor:
            cursor.execute(sql, parameters)
            return list(cursor.fetchall())

    def insert_row(
        self,
        row_id: str,
        *,
        user_id: str = USER_A,
        created_at: datetime = T0,
        question: str = SAMPLE_QUESTION,
        delivered: str = SAMPLE_ANSWER,
    ) -> None:
        self.execute(
            """INSERT INTO qa_corpus
               (id, task_id, conversation_id, user_id, question_content, answer_delivered,
                answer_model_raw, terminal_kind, user_result, worker_id, worker_version,
                target_worker_version, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'success', 'obtained', 'worker-1', '2.5.0',
                       'stable', %s)""",
            (
                row_id,
                f"tsk_{row_id}",
                f"cnv_{row_id}",
                user_id,
                question,
                delivered,
                SAMPLE_RAW,
                created_at,
            ),
        )

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = TOOL.main(["--dsn", self._dsn, *argv])
        return code, out.getvalue(), err.getvalue()

    def grant(self, open_id: str = READER) -> None:
        code, _out, err = self.run_main(
            "--initiated-by", ADMIN, "grant", "--open-id", open_id, "--label", "corpus-reader"
        )
        self.assertEqual(code, 0, err)

    def audit_rows(self) -> list[dict[str, Any]]:
        rows = self.fetch(
            "SELECT operation, phase, initiated_by, actor_roles, executor, entry_point, target_kind,"
            " target_count, target_digest, target_user_id, result_code, result_counts::text,"
            " evidence_ref, operation_audit::text FROM operation_audit"
            " WHERE operation LIKE 'corpus.%%' ORDER BY created_at, id"
        )
        keys = (
            "operation",
            "phase",
            "initiated_by",
            "actor_roles",
            "executor",
            "entry_point",
            "target_kind",
            "target_count",
            "target_digest",
            "target_user_id",
            "result_code",
            "result_counts",
            "evidence_ref",
            "whole_row",
        )
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def assert_no_sample(self, *texts: str) -> None:
        for text in texts:
            for sample in (SAMPLE_QUESTION, SAMPLE_ANSWER, SAMPLE_RAW, "华东区"):
                self.assertNotIn(sample, text)


class UnauthorizedReadTest(CorpusReadPostgresTestCase):
    """完成标准 2：受控假 open_id 读取 → 退出码 2、语料表零查询、输出不含样本。"""

    def test_a_stranger_gets_exit_2_and_no_statement_ever_touches_the_corpus_table(self) -> None:
        import lingxi.adapters.postgres_qa_corpus as corpus_adapter

        self.insert_row("qac_1")
        statements: list[str] = []
        real_connect = corpus_adapter.connect
        psycopg = self._psycopg

        class _Recording(psycopg.Cursor):
            def execute(self, query, params=None, *args, **kwargs):
                statements.append(query if isinstance(query, str) else str(query))
                return super().execute(query, params, *args, **kwargs)

        def recording_connect(dsn: str, **kwargs: Any) -> Any:
            connection = real_connect(dsn, dedicated=True)
            connection.cursor_factory = _Recording
            return connection

        corpus_adapter.connect = recording_connect
        self.addCleanup(setattr, corpus_adapter, "connect", real_connect)

        code, out, err = self.run_main("--initiated-by", STRANGER, "read", "--keyword", "华东区")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("不是生效的语料读取者", err)
        self.assert_no_sample(out, err)
        self.assertTrue(statements, "闸门本身应当查过读取角色表")
        self.assertEqual([sql for sql in statements if _CORPUS_TABLE.search(sql)], [])
        self.assertEqual(self.audit_rows(), [])

    def test_a_revoked_reader_is_a_stranger_again(self) -> None:
        self.insert_row("qac_1")
        self.grant()
        code, _out, err = self.run_main("--initiated-by", ADMIN, "revoke", "--open-id", READER)
        self.assertEqual(code, 0, err)

        code, out, err = self.run_main("--initiated-by", READER, "read", "--keyword", "华东区")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(
            [row["operation"] for row in self.audit_rows()],
            ["corpus.reader_grant", "corpus.reader_revoke"],
        )


class AuditRowTest(CorpusReadPostgresTestCase):
    """完成标准 4：读取 / 导出各一行审计，字段齐全、不含正文；导出摘要与条数对得上文件。"""

    def test_read_leaves_one_executed_row_with_digest_and_counts_but_no_content(self) -> None:
        self.insert_row("qac_1")
        self.insert_row("qac_2", user_id=USER_B, created_at=T0 + timedelta(minutes=1))
        self.grant()

        code, out, err = self.run_main(
            "--initiated-by", READER, "read", "--keyword", "华东区", "--since", "2026-09-01"
        )

        self.assertEqual(code, 0, err)
        self.assertIn(SAMPLE_QUESTION, out)
        self.assertIn(SAMPLE_RAW, out)
        rows = [row for row in self.audit_rows() if row["operation"] == "corpus.read"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["phase"], "executed")
        self.assertEqual(row["entry_point"], "ops_script")
        self.assertEqual(row["initiated_by"], READER)
        self.assertEqual(row["actor_roles"], "")
        self.assertTrue(row["executor"].startswith("scheduler-script:qa_corpus@"))
        self.assertEqual(row["target_kind"], "qa_corpus")
        self.assertIsNone(row["target_user_id"])
        self.assertEqual(row["target_count"], 2)
        self.assertEqual(row["target_digest"], rows_digest(["qac_2", "qac_1"]))
        self.assertEqual(json.loads(row["result_counts"]), {"rows": 2, "users": 2})
        self.assertEqual(row["evidence_ref"], "corpus_window:20260901T000000Z_open")
        self.assert_no_sample(row["whole_row"])

    def test_reading_by_person_records_the_target_user(self) -> None:
        self.insert_row("qac_1")
        self.grant()
        code, _out, err = self.run_main(
            "--initiated-by", READER, "read", "--open-id", f"ou_{USER_A}"
        )
        self.assertEqual(code, 0, err)
        row = [row for row in self.audit_rows() if row["operation"] == "corpus.read"][0]
        self.assertEqual(row["target_user_id"], USER_A)

    def test_export_audit_digest_is_the_file_sha256_and_count_is_the_line_count(self) -> None:
        for index in range(3):
            self.insert_row(f"qac_{index}", created_at=T0 + timedelta(minutes=index))
        self.grant()
        root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "exports"
        self.enterContext(_Environment(TOOL.EXPORT_ROOT_VAR, str(root)))

        code, out, err = self.run_main(
            "--initiated-by", READER, "export", "--open-id", f"ou_{USER_A}", "--out", "a.jsonl"
        )

        self.assertEqual(code, 0, err)
        path = root / "a.jsonl"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        content = path.read_bytes()
        lines = content.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual([json.loads(line)["id"] for line in lines], ["qac_2", "qac_1", "qac_0"])
        rows = [row for row in self.audit_rows() if row["operation"] == "corpus.export"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["target_digest"], "sha256:" + hashlib.sha256(content).hexdigest())
        self.assertEqual(row["target_count"], 3)
        self.assertEqual(row["target_user_id"], USER_A)
        self.assertEqual(json.loads(row["result_counts"]), {"rows": 3, "users": 1})
        self.assertEqual(row["evidence_ref"], "corpus_export:a.jsonl")
        self.assert_no_sample(row["whole_row"], out, err)
        self.assertIn(row["target_digest"], out)

    def test_an_audit_failure_withholds_the_read_and_deletes_the_export(self) -> None:
        self.insert_row("qac_1")
        self.grant()
        root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "exports"
        self.enterContext(_Environment(TOOL.EXPORT_ROOT_VAR, str(root)))

        class _Broken:
            def record(self, entry: Any) -> str:
                raise RuntimeError("审计账写不进去")

        original = TOOL.resolve_operation_audit
        TOOL.resolve_operation_audit = lambda dsn: _Broken()
        self.addCleanup(setattr, TOOL, "resolve_operation_audit", original)

        code, out, err = self.run_main("--initiated-by", READER, "read", "--keyword", "华东区")
        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        self.assert_no_sample(out, err)

        code, out, err = self.run_main(
            "--initiated-by", READER, "export", "--keyword", "华东区", "--out", "a.jsonl"
        )
        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        self.assertFalse((root / "a.jsonl").exists())
        self.assertEqual([row["operation"] for row in self.audit_rows()], ["corpus.reader_grant"])


class SearchTest(CorpusReadPostgresTestCase):
    """完成标准 6：按人、时间窗（含起不含止）、关键词（中文子串、转义）各一条真库断言 + EXPLAIN。"""

    def test_by_person_returns_only_that_persons_rows(self) -> None:
        self.insert_row("qac_a1")
        self.insert_row("qac_b1", user_id=USER_B)
        self.assertEqual(
            [row.id for row in self.store.search(CorpusFilter(user_id=USER_A), limit=20)],
            ["qac_a1"],
        )
        self.assertEqual(
            [row.id for row in self.store.search(CorpusFilter(user_id=USER_B), limit=20)],
            ["qac_b1"],
        )

    def test_the_window_includes_its_start_and_excludes_its_end(self) -> None:
        self.insert_row("qac_before", created_at=T0 - timedelta(seconds=1))
        self.insert_row("qac_start", created_at=T0)
        self.insert_row("qac_inside", created_at=T0 + timedelta(hours=1))
        self.insert_row("qac_end", created_at=T0 + timedelta(hours=2))
        chosen = CorpusFilter(since=T0, until=T0 + timedelta(hours=2))

        found = [row.id for row in self.store.search(chosen, limit=20)]

        self.assertEqual(found, ["qac_inside", "qac_start"])
        self.assertEqual([row.id for row in self.store.iter_export(chosen)], found)

    def test_keyword_matches_chinese_substrings_in_both_columns_with_escaping(self) -> None:
        self.insert_row("qac_q", question="今年增长 100% 的公司", delivered="没有命中词")
        self.insert_row("qac_d", question="没有命中词", delivered="实收里写着 a_b 与 100 元")
        self.insert_row("qac_raw", question="无关", delivered="无关")
        # 三行的模型原文列都含「模型原文」：那一列不作检索目标，命中零行才对。
        cases = {
            "100%": ["qac_q"],
            "100": ["qac_d", "qac_q"],
            "a_b": ["qac_d"],
            "aXb": [],
            "命中词": ["qac_d", "qac_q"],
            "模型原文": [],
        }
        for keyword, expected in cases.items():
            with self.subTest(keyword=keyword):
                found = [
                    row.id for row in self.store.search(CorpusFilter(keyword=keyword), limit=20)
                ]
                self.assertEqual(sorted(found), expected)

    def test_explain_uses_the_trgm_indexes_for_three_or_more_characters(self) -> None:
        from lingxi.adapters.postgres_qa_corpus import _KEYWORD_SQL, _SELECT_SQL
        from lingxi.core.qa_corpus import like_pattern

        self.insert_row("qac_1")
        sql = _SELECT_SQL + " WHERE " + _KEYWORD_SQL + " ORDER BY created_at DESC, id DESC LIMIT 20"
        self.execute("SET enable_seqscan = off")
        self.addCleanup(self.execute, "SET enable_seqscan = on")
        for keyword, expected in (("华东区", True), ("新增用户", True), ("华东", False)):
            with self.subTest(keyword=keyword):
                pattern = like_pattern(keyword)
                plan = "\n".join(row[0] for row in self.fetch("EXPLAIN " + sql, (pattern, pattern)))
                self.assertEqual("qa_corpus_question_trgm_idx" in plan, expected, plan)
                self.assertEqual("qa_corpus_answer_trgm_idx" in plan, expected, plan)

    def test_the_read_limit_is_honoured_and_capped(self) -> None:
        for index in range(5):
            self.insert_row(f"qac_{index}", created_at=T0 + timedelta(minutes=index))
        self.assertEqual(len(self.store.search(CorpusFilter(user_id=USER_A), limit=2)), 2)
        with self.assertRaises(ValueError):
            self.store.search(CorpusFilter(user_id=USER_A), limit=201)


class ReaderRoleTest(CorpusReadPostgresTestCase):
    def test_grant_is_idempotent_with_one_active_row_and_two_audit_rows(self) -> None:
        self.grant()
        self.grant()

        self.assertEqual(
            self.fetch("SELECT count(*) FROM qa_corpus_reader WHERE entry_status = 'active'"),
            [(1,)],
        )
        rows = self.audit_rows()
        self.assertEqual([row["result_code"] for row in rows], ["granted", "already_active"])
        self.assertEqual(rows[0]["target_kind"], "qa_corpus_reader")
        self.assertEqual(rows[0]["target_user_id"], READER)
        self.assertEqual(rows[0]["actor_roles"], "permission_admin,ops_admin,super_admin")
        self.assertRegex(rows[0]["evidence_ref"], r"^qa_corpus_reader:qcr_[0-9A-Z]{26}$")

    def test_a_reader_who_is_not_an_admin_cannot_grant(self) -> None:
        self.grant()
        code, _out, err = self.run_main(
            "--initiated-by", READER, "grant", "--open-id", STRANGER, "--label", "x"
        )
        self.assertEqual(code, 2)
        self.assertIn("不是一位生效的已登记管理员", err)
        self.assertEqual(
            self.fetch("SELECT feishu_open_id FROM qa_corpus_reader ORDER BY feishu_open_id"),
            [(READER,)],
        )

    def test_a_failed_audit_rolls_the_grant_back(self) -> None:
        original = TOOL.record_audit_in_transaction

        def broken(connection: Any, entry: Any) -> str:
            raise RuntimeError("审计账写不进去")

        TOOL.record_audit_in_transaction = broken
        self.addCleanup(setattr, TOOL, "record_audit_in_transaction", original)

        code, _out, err = self.run_main(
            "--initiated-by", ADMIN, "grant", "--open-id", READER, "--label", "corpus-reader"
        )

        self.assertEqual(code, 2)
        self.assertIn("授予未生效、已回滚", err)
        self.assertEqual(self.fetch("SELECT count(*) FROM qa_corpus_reader"), [(0,)])
        self.assertEqual(self.audit_rows(), [])


class _Environment:
    """临时设置一个环境变量，退出时恢复。"""

    def __init__(self, name: str, value: str) -> None:
        self._name, self._value = name, value

    def __enter__(self) -> None:
        self._previous = os.environ.get(self._name)
        os.environ[self._name] = self._value

    def __exit__(self, *exc: object) -> None:
        if self._previous is None:
            os.environ.pop(self._name, None)
        else:
            os.environ[self._name] = self._previous


if __name__ == "__main__":
    unittest.main()
