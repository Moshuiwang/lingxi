"""问答留存语料读取侧的无库用例（Issue #664）：核心纯逻辑、运维脚本的顺序与失败关闭、部署门禁。

脚本 `scripts/ops/qa_corpus.py` 按路径加载（同 `tests/test_outreach_ops.py`），数据库全部经
脚本的 `resolve_*` 注入点换成假实现：这里证明的是**顺序**——鉴权在查询之前、审计在输出
之前、文件在审计失败时被删——以及导出路径的逃逸一律零写入。真库才能证伪的断言（三种检索、
索引、审计行落库）见 `tests/test_qa_corpus_read_postgres.py`。样本正文全部是合成的。
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import stat
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from lingxi.core.admin.operation_audit import OperationAuditEntry
from lingxi.core.admin.registry import ALL_ADMIN_ROLES, AdminRegistryEntry
from lingxi.core.innertest_content_capture import CapturedToolCall
from lingxi.core.qa_corpus import (
    DEFAULT_READ_LIMIT,
    MAX_READ_LIMIT,
    CapacityThresholds,
    CorpusFilter,
    QaCorpusReaderEntry,
    QaCorpusRecord,
    capacity_watermark,
    checked_export_name,
    checked_read_limit,
    digest_label,
    export_evidence_ref,
    export_line,
    like_pattern,
    read_result_counts,
    rows_digest,
    window_evidence_ref,
)

REPOSITORY_ROOT = Path(__file__).parents[1]
SCRIPT = REPOSITORY_ROOT / "scripts" / "ops" / "qa_corpus.py"


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


TOOL = _load(SCRIPT, "qa_corpus_ops_under_test")
CONTRACT = _load(
    REPOSITORY_ROOT / "scripts" / "ci" / "check_deploy_contract.py", "deploy_contract_for_corpus"
)

READER = "ou_reader_fake"
ADMIN = "ou_admin_fake"
STRANGER = "ou_stranger_fake"
SAMPLE_QUESTION = "合成样本：上季度华东区新增用户数"
SAMPLE_ANSWER = "合成样本：上季度华东区新增用户 4321 人。"
SAMPLE_RAW = "合成样本：模型原文 4321 人（未投影）"
SAMPLE_KEYWORD = "华东区"
NOW = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)


def _record(task_id: str, user_id: str = "usr_a") -> QaCorpusRecord:
    return QaCorpusRecord(
        task_id=task_id,
        conversation_id=f"cnv_{task_id}",
        user_id=user_id,
        trace_id=None,
        task_created_at=NOW,
        question_content=SAMPLE_QUESTION,
        question_redaction_count=0,
        answer_delivered=SAMPLE_ANSWER,
        answer_delivered_redaction_count=0,
        answer_model_raw=SAMPLE_RAW,
        answer_model_raw_redaction_count=0,
        tool_calls=(
            CapturedToolCall(
                tool_use_id="t1",
                tool_name="mcp__query__run",
                tool_input={"metric": "new_users"},
                result_summary={"result_kind": "ok", "content": "ok", "truncated": False},
                redaction_count=0,
            ),
        ),
        terminal_kind="success",
        user_result="obtained",
        failure_code=None,
        output_safety_withheld=False,
        worker_id="worker-1",
        worker_version="2.5.0",
        target_worker_version="stable",
        system_prompt_digest=None,
        model=None,
    )


@dataclass(frozen=True)
class _Row:
    id: str
    created_at: datetime
    record: QaCorpusRecord


def _rows(count: int) -> tuple[_Row, ...]:
    return tuple(
        _Row(
            id=f"qac_{index:03d}",
            created_at=NOW - timedelta(minutes=index),
            record=_record(f"t{index}"),
        )
        for index in range(count)
    )


# ---------------------------------------------------------------- 核心纯逻辑


class CorpusFilterTest(unittest.TestCase):
    def test_at_least_one_condition_is_required(self) -> None:
        with self.assertRaises(ValueError):
            CorpusFilter()

    def test_naive_moments_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            CorpusFilter(since=datetime(2026, 9, 1))
        with self.assertRaises(ValueError):
            CorpusFilter(until=datetime(2026, 9, 1))

    def test_the_window_must_be_ordered(self) -> None:
        with self.assertRaises(ValueError):
            CorpusFilter(since=NOW, until=NOW)
        with self.assertRaises(ValueError):
            CorpusFilter(since=NOW, until=NOW - timedelta(seconds=1))
        CorpusFilter(since=NOW, until=NOW + timedelta(seconds=1))

    def test_keyword_shape(self) -> None:
        for bad in ("", "   ", "a\x00b", "字" * 129):
            with self.subTest(keyword=bad), self.assertRaises(ValueError):
                CorpusFilter(keyword=bad)
        self.assertEqual(CorpusFilter(keyword="100%").keyword, "100%")

    def test_single_conditions_are_accepted(self) -> None:
        CorpusFilter(user_id="usr_a")
        CorpusFilter(since=NOW)
        CorpusFilter(until=NOW)
        with self.assertRaises(ValueError):
            CorpusFilter(user_id="")


class ReadLimitTest(unittest.TestCase):
    def test_bounds(self) -> None:
        self.assertEqual(DEFAULT_READ_LIMIT, 20)
        self.assertEqual(MAX_READ_LIMIT, 200)
        self.assertEqual(checked_read_limit(1), 1)
        self.assertEqual(checked_read_limit(200), 200)
        for bad in (0, -1, 201, True, "5", 2.0):
            with self.subTest(limit=bad), self.assertRaises(ValueError):
                checked_read_limit(bad)


class LikePatternTest(unittest.TestCase):
    def test_wildcards_and_backslash_are_escaped(self) -> None:
        self.assertEqual(like_pattern("100%"), "%100\\%%")
        self.assertEqual(like_pattern("a_b"), "%a\\_b%")
        self.assertEqual(like_pattern("a\\b"), "%a\\\\b%")
        self.assertEqual(like_pattern("华东区"), "%华东区%")


class ExportNameTest(unittest.TestCase):
    def test_only_plain_jsonl_names_are_accepted(self) -> None:
        self.assertEqual(checked_export_name("2026-09-14_usr.jsonl"), "2026-09-14_usr.jsonl")
        for bad in (
            "../x.jsonl",
            "/tmp/x.jsonl",
            "a/b.jsonl",
            ".hidden.jsonl",
            "x.txt",
            "x",
            "",
            "a" * 121 + ".jsonl",
            "含中文.jsonl",
            None,
        ):
            with self.subTest(name=bad), self.assertRaises(ValueError):
                checked_export_name(bad)


class ExportLineTest(unittest.TestCase):
    def test_line_carries_only_the_internal_id_plus_body_counts_and_terminal_facts(self) -> None:
        line = export_line(row_id="qac_1", created_at=NOW, record=_record("tsk1"))

        self.assertTrue(line.endswith("\n"))
        payload = json.loads(line)
        self.assertEqual(
            set(payload),
            {
                "id",
                "created_at",
                "task_created_at",
                "terminal_kind",
                "user_result",
                "failure_code",
                "output_safety_withheld",
                "question_content",
                "question_redaction_count",
                "answer_delivered",
                "answer_delivered_redaction_count",
                "answer_model_raw",
                "answer_model_raw_redaction_count",
                "tool_calls",
                "tool_calls_redaction_count",
            },
        )
        self.assertEqual(payload["question_content"], SAMPLE_QUESTION)
        self.assertIn(SAMPLE_QUESTION, line)
        without_id = json.dumps({key: value for key, value in payload.items() if key != "id"})
        for identifier in ("tsk1", "cnv_tsk1", "usr_a", "worker-1", "2.5.0", "stable"):
            self.assertNotIn(identifier, without_id)
        self.assertEqual(payload["tool_calls"][0]["tool_name"], "mcp__query__run")


class DigestTest(unittest.TestCase):
    def test_rows_digest_is_stable_and_order_sensitive(self) -> None:
        first = rows_digest(["qac_a", "qac_b"])
        self.assertEqual(first, rows_digest(["qac_a", "qac_b"]))
        self.assertNotEqual(first, rows_digest(["qac_b", "qac_a"]))
        self.assertEqual(rows_digest([]), digest_label(hashlib.sha256(b"").hexdigest()))
        self.assertRegex(first, r"^sha256:[0-9a-f]{64}$")

    def test_digest_label_only_accepts_hex(self) -> None:
        with self.assertRaises(ValueError):
            digest_label("not-hex")

    def test_result_counts(self) -> None:
        self.assertEqual(read_result_counts(["u1", "u2", "u1"]), {"rows": 3, "users": 2})
        self.assertEqual(read_result_counts([]), {"rows": 0, "users": 0})


class EvidenceRefTest(unittest.TestCase):
    def test_window_pointer_encodes_utc_and_open_ends(self) -> None:
        beijing = datetime(2026, 9, 1, 8, 0, tzinfo=timezone(timedelta(hours=8)))
        self.assertEqual(window_evidence_ref(beijing, None), "corpus_window:20260901T000000Z_open")
        self.assertEqual(window_evidence_ref(None, None), "corpus_window:open_open")

    def test_pointers_fit_the_audit_column(self) -> None:
        for ref in (
            window_evidence_ref(NOW, NOW + timedelta(days=1)),
            export_evidence_ref("2026-09-14_usr.jsonl"),
        ):
            with self.subTest(ref=ref):
                entry = OperationAuditEntry(
                    operation_id="qcr_1",
                    operation="corpus.read",
                    phase=TOOL.OperationPhase.EXECUTED,
                    initiated_by=READER,
                    actor_roles=frozenset(),
                    entry_point=TOOL.EntryPoint.OPS_SCRIPT,
                    executor="scheduler-script:qa_corpus@0:qcr_1",
                    result_code="ok",
                    evidence_ref=ref,
                )
                self.assertEqual(entry.evidence_ref, ref)


class CapacityWatermarkTest(unittest.TestCase):
    def test_crossings(self) -> None:
        thresholds = CapacityThresholds(max_rows=10, max_bytes=100)
        self.assertEqual(capacity_watermark(9, 99, thresholds), ())
        self.assertEqual(capacity_watermark(10, 99, thresholds), ("rows",))
        self.assertEqual(capacity_watermark(0, 100, thresholds), ("bytes",))
        self.assertEqual(capacity_watermark(10, 100, thresholds), ("rows", "bytes"))

    def test_shapes(self) -> None:
        with self.assertRaises(ValueError):
            CapacityThresholds(max_rows=0, max_bytes=1)
        with self.assertRaises(ValueError):
            capacity_watermark(-1, 0, CapacityThresholds(max_rows=1, max_bytes=1))


# ---------------------------------------------------------------- 脚本：假实现


class _Readers:
    def __init__(self, active: set[str] | None = None, *, fail: bool = False) -> None:
        self.active = set(active or ())
        self.fail = fail
        self.grants: list[tuple[str, str, str]] = []
        self.revokes: list[str] = []
        self.audit_entries: list[OperationAuditEntry] = []

    def entry(self, open_id: str) -> QaCorpusReaderEntry | None:
        if self.fail:
            raise RuntimeError("库读不到")
        if open_id not in self.active:
            return None
        return QaCorpusReaderEntry(
            id="qcr_x",
            feishu_open_id=open_id,
            label="corpus-reader",
            entry_status="active",
            granted_by=ADMIN,
            granted_at=NOW,
        )

    def grant(self, open_id: str, label: str, *, granted_by: str, audit: Any) -> tuple:
        created = open_id not in self.active
        self.active.add(open_id)
        self.grants.append((open_id, label, granted_by))
        entry = self.entry(open_id)
        return entry, created, audit(object(), entry, created)

    def revoke(self, open_id: str, *, audit: Any) -> tuple | None:
        if open_id not in self.active:
            return None
        self.active.discard(open_id)
        self.revokes.append(open_id)
        entry = QaCorpusReaderEntry(
            id="qcr_x",
            feishu_open_id=open_id,
            label="corpus-reader",
            entry_status="revoked",
            granted_by=ADMIN,
            granted_at=NOW,
            revoked_at=NOW,
        )
        return entry, audit(object(), entry)


class _AdminLookup:
    def __init__(self, admins: set[str] | None = None) -> None:
        self.admins = set(admins or ())

    def active_entry(self, *, open_id: str) -> AdminRegistryEntry | None:
        if open_id not in self.admins:
            return None
        return AdminRegistryEntry(
            feishu_open_id=open_id, label="ops", roles=ALL_ADMIN_ROLES, entry_status="active"
        )


class _Corpus:
    def __init__(self, rows: tuple = ()) -> None:
        self.rows = rows
        self.calls: list[str] = []

    def search(self, chosen: CorpusFilter, *, limit: int) -> tuple:
        self.calls.append("search")
        return self.rows[:limit]

    def iter_export(self, chosen: CorpusFilter):
        self.calls.append("iter_export")
        yield from self.rows

    def stats(self):
        self.calls.append("stats")
        return TOOL.__class__  # 不会被用到


class _Untouchable:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"未鉴权就碰了语料查询口：{name}")


class _Ledger:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.entries: list[OperationAuditEntry] = []

    def record(self, entry: OperationAuditEntry) -> str:
        if self.fail:
            raise RuntimeError("审计账写不进去")
        self.entries.append(entry)
        return f"opa_{len(self.entries)}"


class ScriptTestCase(unittest.TestCase):
    """把五个注入点换成假实现；每个用例自己决定谁是读取者、谁是管理员。"""

    def setUp(self) -> None:
        self.readers = _Readers({READER})
        self.admins = _AdminLookup({ADMIN})
        self.corpus: Any = _Corpus(_rows(3))
        self.ledger = _Ledger()
        self.recorded: list[OperationAuditEntry] = []
        self.user_ids = {"ou_user_fake": "usr_a"}
        self._patch("resolve_readers", lambda dsn: self.readers)
        self._patch("resolve_admin_registry_lookup", lambda dsn: self.admins)
        self._patch("resolve_corpus", lambda dsn: self.corpus)
        self._patch("resolve_operation_audit", lambda dsn: self.ledger)
        self._patch("resolve_user_id", lambda dsn, open_id: self.user_ids.get(open_id))
        self._patch("record_audit_in_transaction", self._record_in_transaction)

    def _record_in_transaction(self, connection: Any, entry: OperationAuditEntry) -> str:
        self.recorded.append(entry)
        return f"opa_tx_{len(self.recorded)}"

    def _patch(self, name: str, value: Any) -> None:
        original = getattr(TOOL, name)
        setattr(TOOL, name, value)
        self.addCleanup(setattr, TOOL, name, original)

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = TOOL.main(["--dsn", "postgresql://fake/db", *argv])
        return code, out.getvalue(), err.getvalue()

    def assert_no_sample(self, *texts: str) -> None:
        for text in texts:
            for sample in (SAMPLE_QUESTION, SAMPLE_ANSWER, SAMPLE_RAW, SAMPLE_KEYWORD):
                self.assertNotIn(sample, text)


class ParserTest(ScriptTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(redirect_stderr(io.StringIO()))

    def test_initiated_by_is_required_and_abbreviations_are_refused(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            TOOL.build_parser().parse_args(["read", "--keyword", "x"])
        self.assertEqual(caught.exception.code, 2)
        with self.assertRaises(SystemExit):
            TOOL.build_parser().parse_args(["--initiated", READER, "read", "--keyword", "x"])
        with self.assertRaises(SystemExit):
            TOOL.build_parser().parse_args(["--initiated-by", READER, "read", "--key", "x"])

    def test_limit_bounds_are_parse_errors(self) -> None:
        for value in ("0", "-1", "201", "abc"):
            with self.subTest(limit=value), self.assertRaises(SystemExit):
                TOOL.build_parser().parse_args(
                    ["--initiated-by", READER, "read", "--keyword", "x", "--limit", value]
                )
        arguments = TOOL.build_parser().parse_args(
            ["--initiated-by", READER, "read", "--keyword", "x"]
        )
        self.assertEqual(arguments.limit, DEFAULT_READ_LIMIT)

    def test_moments_default_to_utc(self) -> None:
        self.assertEqual(TOOL.parse_moment("2026-09-01"), datetime(2026, 9, 1, tzinfo=UTC))
        self.assertEqual(
            TOOL.parse_moment("2026-09-01T08:00:00+08:00").astimezone(UTC),
            datetime(2026, 9, 1, tzinfo=UTC),
        )
        with self.assertRaises(SystemExit):
            TOOL.build_parser().parse_args(["--initiated-by", READER, "read", "--since", "昨天"])

    def test_export_requires_out_and_initiator_shape_is_checked_before_anything(self) -> None:
        with self.assertRaises(SystemExit):
            TOOL.build_parser().parse_args(["--initiated-by", READER, "export", "--keyword", "x"])
        self.corpus = _Untouchable()
        self._patch("resolve_corpus", lambda dsn: self.corpus)
        code, out, err = self.run_main("--initiated-by", "ou bad", "read", "--keyword", "x")
        self.assertEqual(code, 2)
        self.assertIn("open_id 形状", err)

    def test_a_filter_is_required(self) -> None:
        code, out, err = self.run_main("--initiated-by", READER, "read")
        self.assertEqual(code, 2)
        self.assertEqual(self.corpus.calls, [])
        self.assertIn("至少要给一项过滤条件", err)


class UnauthorizedReadTest(ScriptTestCase):
    """完成标准 2 的无库形态：退出码 2、语料查询口一次都没被碰、输出没有样本。"""

    def test_a_stranger_is_refused_before_any_corpus_query(self) -> None:
        self.corpus = _Untouchable()
        self._patch("resolve_corpus", lambda dsn: self.corpus)

        for command in (
            ("read", "--keyword", SAMPLE_KEYWORD),
            ("export", "--keyword", "x", "--out", "a.jsonl"),
        ):
            with self.subTest(command=command[0]):
                code, out, err = self.run_main("--initiated-by", STRANGER, *command)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("不是生效的语料读取者", err)
                self.assert_no_sample(out, err)
        self.assertEqual(self.ledger.entries, [])

    def test_an_unreadable_reader_table_fails_closed(self) -> None:
        self.readers.fail = True
        self.corpus = _Untouchable()
        self._patch("resolve_corpus", lambda dsn: self.corpus)
        code, out, err = self.run_main("--initiated-by", READER, "read", "--keyword", "x")
        self.assertEqual(code, 2)
        self.assertIn("读取角色登记表不可读", err)

    def test_an_admin_without_the_reader_role_is_refused(self) -> None:
        """管理员不因其它角色获得读取权（默认拒绝谓词只认读取者登记）。"""
        self.corpus = _Untouchable()
        self._patch("resolve_corpus", lambda dsn: self.corpus)
        code, _out, err = self.run_main("--initiated-by", ADMIN, "read", "--keyword", "x")
        self.assertEqual(code, 2)
        self.assertIn("不是生效的语料读取者", err)


class ReadOrderingTest(ScriptTestCase):
    def test_rows_are_printed_only_after_the_audit_row_is_committed(self) -> None:
        code, out, err = self.run_main(
            "--initiated-by", READER, "read", "--keyword", SAMPLE_KEYWORD, "--limit", "2"
        )

        self.assertEqual(code, 0)
        self.assertIn(SAMPLE_QUESTION, out)
        self.assertIn(SAMPLE_RAW, out)
        self.assertEqual(out.count("=== qac_"), 2)
        entry = self.ledger.entries[0]
        self.assertEqual(entry.operation, "corpus.read")
        self.assertEqual(entry.phase.value, "executed")
        self.assertEqual(entry.entry_point.value, "ops_script")
        self.assertEqual(entry.initiated_by, READER)
        self.assertEqual(entry.actor_roles, frozenset())
        self.assertTrue(entry.executor.startswith("scheduler-script:qa_corpus@"))
        self.assertEqual(entry.target_kind, "qa_corpus")
        self.assertIsNone(entry.target_user_id)
        self.assertEqual(entry.target_count, 2)
        self.assertEqual(entry.target_digest, rows_digest(["qac_000", "qac_001"]))
        self.assertEqual(dict(entry.result_counts), {"rows": 2, "users": 1})
        self.assertEqual(entry.evidence_ref, "corpus_window:open_open")
        self.assert_no_sample(repr(entry))
        self.assertIn("审计 opa_1", err)

    def test_reading_by_person_resolves_the_open_id_and_records_the_target_user(self) -> None:
        code, out, _err = self.run_main(
            "--initiated-by", READER, "read", "--open-id", "ou_user_fake"
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.ledger.entries[0].target_user_id, "usr_a")
        code, out, err = self.run_main("--initiated-by", READER, "read", "--open-id", "ou_missing")
        self.assertEqual(code, 2)
        self.assertIn("不存在", err)

    def test_an_audit_failure_withholds_every_row(self) -> None:
        self.ledger.fail = True
        code, out, err = self.run_main(
            "--initiated-by", READER, "read", "--keyword", SAMPLE_KEYWORD
        )

        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        self.assertIn("审计行未能写入", err)
        self.assert_no_sample(out, err)
        self.assertEqual(self.corpus.calls, ["search"])

    def test_a_failed_query_is_nothing_done(self) -> None:
        class _Broken:
            def search(self, chosen: CorpusFilter, *, limit: int) -> tuple:
                raise RuntimeError("库断了")

        self._patch("resolve_corpus", lambda dsn: _Broken())
        code, out, err = self.run_main("--initiated-by", READER, "read", "--keyword", "x")
        self.assertEqual(code, 2)
        self.assertIn("语料查询失败：RuntimeError", err)
        self.assertEqual(self.ledger.entries, [])


class ExportTest(ScriptTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "exports"
        self.environment = {TOOL.EXPORT_ROOT_VAR: str(self.root)}
        original = TOOL.run_export
        self._patch(
            "run_export",
            lambda dsn, arguments, *, initiated_by: original(
                dsn, arguments, initiated_by=initiated_by, environment=self.environment
            ),
        )
        TOOL.RUNNERS["export"] = TOOL.run_export
        self.addCleanup(TOOL.RUNNERS.__setitem__, "export", original)

    def _export(self, *argv: str) -> tuple[int, str, str]:
        return self.run_main("--initiated-by", READER, "export", "--keyword", SAMPLE_KEYWORD, *argv)

    def test_success_writes_a_0600_file_and_audits_its_digest_and_count(self) -> None:
        code, out, err = self._export("--out", "batch.jsonl")

        self.assertEqual(code, 0, err)
        path = self.root / "batch.jsonl"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        lines = path.read_bytes().splitlines(keepends=True)
        self.assertEqual(len(lines), 3)
        entry = self.ledger.entries[0]
        self.assertEqual(entry.operation, "corpus.export")
        self.assertEqual(entry.target_count, 3)
        self.assertEqual(
            entry.target_digest, digest_label(hashlib.sha256(path.read_bytes()).hexdigest())
        )
        self.assertEqual(entry.evidence_ref, "corpus_export:batch.jsonl")
        self.assertEqual(dict(entry.result_counts), {"rows": 3, "users": 1})
        self.assert_no_sample(out, err, repr(entry))
        self.assertIn("batch.jsonl", out)
        self.assertEqual(json.loads(lines[0])["question_content"], SAMPLE_QUESTION)

    def test_path_escapes_are_refused_with_zero_writes(self) -> None:
        outside = self.root.parent / "outside.jsonl"
        for name in ("../outside.jsonl", str(outside), "sub/inner.jsonl", "..", ".jsonl"):
            with self.subTest(name=name):
                code, out, err = self._export("--out", name)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
        self.assertFalse(outside.exists())
        self.assertFalse(self.root.exists() and any(self.root.iterdir()))
        self.assertEqual(self.ledger.entries, [])
        self.assertEqual(self.corpus.calls, [])

    def test_a_symlink_at_the_target_name_is_refused_and_its_target_untouched(self) -> None:
        self.root.mkdir(mode=0o700)
        victim = self.root.parent / "victim.txt"
        victim.write_text("原有内容", encoding="utf-8")
        (self.root / "link.jsonl").symlink_to(victim)

        code, _out, err = self._export("--out", "link.jsonl")

        self.assertEqual(code, 2)
        self.assertIn("导出文件不可建", err)
        self.assertEqual(victim.read_text(encoding="utf-8"), "原有内容")
        self.assertEqual(self.ledger.entries, [])

    def test_a_symlinked_or_loose_root_is_refused(self) -> None:
        real = self.root.parent / "real"
        real.mkdir(mode=0o700)
        self.root.symlink_to(real)
        code, _out, err = self._export("--out", "a.jsonl")
        self.assertEqual(code, 2)
        self.assertIn("导出根不可打开", err)
        self.assertEqual(list(real.iterdir()), [])

        self.root.unlink()
        self.root.mkdir(mode=0o755)
        code, _out, err = self._export("--out", "a.jsonl")
        self.assertEqual(code, 2)
        self.assertIn("属主或权限不受控", err)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_an_existing_file_is_never_overwritten(self) -> None:
        self.root.mkdir(mode=0o700)
        (self.root / "a.jsonl").write_text("旧文件", encoding="utf-8")
        code, _out, _err = self._export("--out", "a.jsonl")
        self.assertEqual(code, 2)
        self.assertEqual((self.root / "a.jsonl").read_text(encoding="utf-8"), "旧文件")

    def test_missing_root_variable_means_no_export(self) -> None:
        self.environment.clear()
        code, _out, err = self._export("--out", "a.jsonl")
        self.assertEqual(code, 2)
        self.assertIn(TOOL.EXPORT_ROOT_VAR, err)
        self.assertEqual(self.corpus.calls, [])

    def test_an_audit_failure_deletes_the_file(self) -> None:
        self.ledger.fail = True
        code, out, err = self._export("--out", "a.jsonl")

        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        self.assertIn("导出文件已删除", err)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assert_no_sample(out, err)

    def test_a_query_failure_mid_export_deletes_the_file(self) -> None:
        def broken(chosen: CorpusFilter):
            yield _rows(1)[0]
            raise RuntimeError("库断了")

        self.corpus.iter_export = broken
        code, out, err = self._export("--out", "a.jsonl")
        self.assertEqual(code, 3)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(self.ledger.entries, [])


class ReaderRoleCommandsTest(ScriptTestCase):
    def test_only_a_valid_admin_can_grant_or_revoke(self) -> None:
        for initiator in (READER, STRANGER):
            for command in (
                ("grant", "--open-id", "ou_new", "--label", "corpus-reader"),
                ("revoke", "--open-id", READER),
            ):
                with self.subTest(initiator=initiator, command=command[0]):
                    code, out, err = self.run_main("--initiated-by", initiator, *command)
                    self.assertEqual(code, 2)
                    self.assertIn("不是一位生效的已登记管理员", err)
        self.assertEqual(self.readers.grants, [])
        self.assertEqual(self.readers.revokes, [])
        self.assertEqual(self.recorded, [])

    def test_grant_records_one_audit_row_in_the_same_transaction_and_is_idempotent(self) -> None:
        code, out, _err = self.run_main(
            "--initiated-by", ADMIN, "grant", "--open-id", "ou_new", "--label", "corpus-reader"
        )
        self.assertEqual(code, 0)
        self.assertIn("已授予读取角色", out)
        code, out, _err = self.run_main(
            "--initiated-by", ADMIN, "grant", "--open-id", "ou_new", "--label", "corpus-reader"
        )
        self.assertEqual(code, 0)
        self.assertIn("未重复登记", out)

        self.assertEqual(
            [entry.result_code for entry in self.recorded], ["granted", "already_active"]
        )
        first = self.recorded[0]
        self.assertEqual(first.operation, "corpus.reader_grant")
        self.assertEqual(first.actor_roles, ALL_ADMIN_ROLES)
        self.assertEqual(first.target_kind, "qa_corpus_reader")
        self.assertEqual(first.target_user_id, "ou_new")
        self.assertEqual(first.evidence_ref, "qa_corpus_reader:qcr_x")
        self.assertEqual(self.readers.grants[0], ("ou_new", "corpus-reader", ADMIN))

    def test_revoke_then_read_is_refused(self) -> None:
        code, out, _err = self.run_main("--initiated-by", ADMIN, "revoke", "--open-id", READER)
        self.assertEqual(code, 0)
        self.assertEqual(self.recorded[0].operation, "corpus.reader_revoke")
        self.assertEqual(self.recorded[0].result_code, "revoked")

        code, _out, err = self.run_main("--initiated-by", READER, "read", "--keyword", "x")
        self.assertEqual(code, 2)
        self.assertIn("不是生效的语料读取者", err)

        code, _out, err = self.run_main("--initiated-by", ADMIN, "revoke", "--open-id", READER)
        self.assertEqual(code, 2)
        self.assertIn("没有生效的读取角色登记", err)

    def test_a_failed_grant_transaction_is_reported_as_nothing_done(self) -> None:
        self._patch(
            "record_audit_in_transaction",
            lambda connection, entry: (_ for _ in ()).throw(RuntimeError("账坏了")),
        )
        code, _out, err = self.run_main(
            "--initiated-by", ADMIN, "grant", "--open-id", "ou_new", "--label", "x"
        )
        self.assertEqual(code, 2)
        self.assertIn("授予未生效、已回滚：RuntimeError", err)


class StatsTest(ScriptTestCase):
    def test_reader_or_admin_may_see_counts_and_nobody_else(self) -> None:
        @dataclass(frozen=True)
        class _Stats:
            rows: int
            total_bytes: int

        self.corpus.stats = lambda: _Stats(rows=12, total_bytes=4096)
        for initiator in (READER, ADMIN):
            with self.subTest(initiator=initiator):
                code, out, _err = self.run_main("--initiated-by", initiator, "stats")
                self.assertEqual(code, 0)
                self.assertIn("行数 12", out)
        code, out, err = self.run_main("--initiated-by", STRANGER, "stats")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(self.ledger.entries, [])


# ---------------------------------------------------------------- 部署门禁


class ExportRootDeployCheckTest(unittest.TestCase):
    """``check_qa_corpus_export_root``：导出根必须在 scheduler 专属持久卷内，且该卷不挂给模型侧。"""

    BASE = textwrap.dedent(
        """\
        scheduler:
          environment:
            LINGXI_QA_CORPUS_EXPORT_ROOT: /var/lib/lingxi/credentials/qa-corpus-exports
          volumes:
            - lingxi-credentials:/var/lib/lingxi/credentials
        worker-queue:
          volumes:
            - lingxi-users:/var/lib/lingxi/users
        """
    )

    def _run(
        self,
        *,
        base: str | None = None,
        stage: str = "",
        prod: str = "",
        innertest: str = "",
        env_example: str = "",
    ) -> list[str]:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        paths = {}
        for name, body in (
            ("compose.yaml", self.BASE if base is None else base),
            ("compose.stage.yaml", stage),
            ("compose.prod.yaml", prod),
            ("compose.innertest.yaml", innertest),
        ):
            path = directory / name
            path.write_text(f"services:\n{textwrap.indent(body, '  ')}", encoding="utf-8")
            paths[name] = path
        env_path = directory / ".env.example"
        env_path.write_text(env_example, encoding="utf-8")
        originals = (
            CONTRACT.COMPOSE_BASE,
            CONTRACT.COMPOSE_STAGE,
            CONTRACT.COMPOSE_PROD,
            CONTRACT.COMPOSE_INNERTEST,
            CONTRACT.ENV_EXAMPLE,
        )
        CONTRACT.COMPOSE_BASE = paths["compose.yaml"]
        CONTRACT.COMPOSE_STAGE = paths["compose.stage.yaml"]
        CONTRACT.COMPOSE_PROD = paths["compose.prod.yaml"]
        CONTRACT.COMPOSE_INNERTEST = paths["compose.innertest.yaml"]
        CONTRACT.ENV_EXAMPLE = env_path
        try:
            return CONTRACT.check_qa_corpus_export_root()
        finally:
            (
                CONTRACT.COMPOSE_BASE,
                CONTRACT.COMPOSE_STAGE,
                CONTRACT.COMPOSE_PROD,
                CONTRACT.COMPOSE_INNERTEST,
                CONTRACT.ENV_EXAMPLE,
            ) = originals

    def test_real_repository_state_passes(self) -> None:
        self.assertEqual(CONTRACT.check_qa_corpus_export_root(), [])

    def test_a_clean_declaration_passes(self) -> None:
        self.assertEqual(self._run(), [])

    def test_missing_or_interpolated_declaration_is_caught(self) -> None:
        self.assertTrue(
            any(
                "没有在 `environment:` 里声明" in f
                for f in self._run(
                    base="scheduler:\n  volumes:\n    - lingxi-credentials:/var/lib/lingxi/credentials\n"
                )
            )
        )
        failures = self._run(
            base=self.BASE.replace(
                "/var/lib/lingxi/credentials/qa-corpus-exports",
                "${LINGXI_EXPORT_ROOT:-/var/lib/lingxi/credentials/x}",
            )
        )
        self.assertTrue(any("字面量绝对路径" in f for f in failures), failures)

    def test_a_root_outside_every_named_volume_is_caught(self) -> None:
        failures = self._run(
            base=self.BASE.replace("/var/lib/lingxi/credentials/qa-corpus-exports", "/tmp/exports")
        )
        self.assertTrue(any("不在 scheduler 任何具名持久卷" in f for f in failures), failures)

    def test_the_volume_mounted_into_a_model_side_service_is_caught(self) -> None:
        failures = self._run(
            stage="worker-queue:\n  volumes:\n    - lingxi-credentials:/var/lib/lingxi/credentials:ro\n"
        )
        self.assertTrue(any("挂给了 `worker-queue`" in f for f in failures), failures)
        failures = self._run(
            prod="gateway:\n  volumes:\n    - lingxi-credentials:/var/lib/lingxi/credentials\n"
        )
        self.assertTrue(any("挂给了 `gateway`" in f for f in failures), failures)

    def test_the_variable_in_a_model_side_block_or_env_example_is_caught(self) -> None:
        failures = self._run(prod="worker:\n  environment:\n    LINGXI_QA_CORPUS_EXPORT_ROOT: /x\n")
        self.assertTrue(any("`worker` 块出现了" in f for f in failures), failures)
        failures = self._run(env_example="LINGXI_QA_CORPUS_EXPORT_ROOT=/var/lib/x\n")
        self.assertTrue(any("赋值行" in f for f in failures), failures)


if __name__ == "__main__":
    unittest.main()
