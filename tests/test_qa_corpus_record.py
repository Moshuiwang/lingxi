"""问答留存语料记录的纯逻辑断言（Issue #664，合同「数据保留与删除」第三条例外）。

不依赖数据库或 Claude Agent SDK：记录形状校验、从回合素材构造记录、四段正文的凭据
形状过滤与计数、窄读取角色的默认拒绝谓词，以及脱敏纯函数搬到共用模块之后原模块
再导出的事实。伪造凭据统一用明显假的占位（形状能命中脱敏规则，不是任何真实系统会
签发的值）；真库落库与接线断言见 ``tests/test_qa_corpus_postgres.py`` 与
``tests/test_qa_corpus_wiring.py``。
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime

from lingxi.config.content import RenderedContent
from lingxi.core import content_redaction, innertest_content_capture
from lingxi.core.delivery.turn_outcome import TerminalDecision
from lingxi.core.execution.audit import TurnAudit
from lingxi.core.execution.tool_policy import ToolPolicy
from lingxi.core.innertest_content_capture import CapturedToolCall, RawTurnCapture
from lingxi.core.qa_corpus import (
    QaCorpusReaderEntry,
    QaCorpusRecord,
    build_qa_corpus_record,
    is_authorized_corpus_reader,
)

_FAKE_TOKEN = "sk-fake-token-1234567890abcdef"
_TOOL = "mcp__q__list_metrics"


class _Task:
    """结构上满足 ``CorpusTaskFacts`` 的最小任务事实。"""

    task_id = "tsk_01HXYZTESTTASK0000000000001"
    conversation_id = "cnv_01HXYZTESTCONV0000000000001"
    user_id = "usr_01HXYZTESTUSER0000000000001"
    trace_id = None
    task_created_at = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)
    target_worker_version = "stable"


def _decision(text: str, *, terminal_kind: str = "success", failure_code=None) -> TerminalDecision:
    return TerminalDecision(
        terminal_kind=terminal_kind,
        error_kind=None,
        content=RenderedContent(key="worker.result", version="v", text=text),
        failure_code=failure_code,
    )


def _capture(*, question: str, answer: str, tool_input: dict, result: str):
    """跑一遍真实的收集器与审计记账，得到已过滤并计数的采集记录。"""
    audit = TurnAudit()
    capture = RawTurnCapture()
    capture.on_pre_tool_use("t1", tool_input)
    verdict = ToolPolicy(allowed_tools=(_TOOL,)).decide(_TOOL, tool_input)
    audit.record_decision(
        tool_name=verdict.tool_name, tool_input=tool_input, tool_use_id="t1", verdict=verdict
    )
    audit.record_executed(tool_name=_TOOL, tool_use_id="t1")
    audit.record_tool_result(tool_use_id="t1", content=result)
    capture.on_stream_event(
        {"kind": "tool_result", "tool_use_id": "t1", "content": result, "is_error": False}
    )
    capture.on_stream_event({"kind": "assistant_message", "text": answer})
    return capture.build_record(
        task_id=_Task.task_id, worker_id="worker-test", question=question, summary=audit.summary()
    )


def _report(*, user_result: str | None = "obtained", withheld: bool = False) -> dict:
    turn: dict = {"closed": True, "final_text": "", "output_safety": {"withheld": withheld}}
    if user_result is not None:
        turn["user_result"] = user_result
    return {"turn": turn, "failure": None}


class BuildRecordTests(unittest.TestCase):
    """从收口后的回合素材构造语料记录。"""

    def test_plain_samples_are_kept_verbatim_with_zero_redactions(self) -> None:
        capture = _capture(
            question="上周新增用户数是多少",
            answer="上周新增用户数是 1234。",
            tool_input={"metric": "new_users"},
            result='{"metrics": [{"metric": "new_users", "value": 1234}]}',
        )

        record = build_qa_corpus_record(
            capture,
            task=_Task,
            decision=_decision("上周新增用户数是 1234。"),
            report=_report(),
            system_prompt_digest="sha256:abc",
            model="model-x",
        )

        self.assertEqual(record.question_content, "上周新增用户数是多少")
        self.assertEqual(record.answer_delivered, "上周新增用户数是 1234。")
        self.assertEqual(record.answer_model_raw, "上周新增用户数是 1234。")
        self.assertEqual(record.tool_calls[0].tool_input, {"metric": "new_users"})
        self.assertEqual(record.question_redaction_count, 0)
        self.assertEqual(record.answer_delivered_redaction_count, 0)
        self.assertEqual(record.answer_model_raw_redaction_count, 0)
        self.assertEqual(record.tool_calls_redaction_count, 0)
        self.assertEqual(record.terminal_kind, "success")
        self.assertEqual(record.user_result, "obtained")
        self.assertFalse(record.output_safety_withheld)
        self.assertEqual(record.user_id, _Task.user_id)
        self.assertEqual(record.conversation_id, _Task.conversation_id)
        self.assertEqual(record.task_created_at, _Task.task_created_at)
        self.assertEqual(record.target_worker_version, "stable")
        self.assertEqual(record.system_prompt_digest, "sha256:abc")
        self.assertEqual(record.model, "model-x")
        self.assertTrue(record.worker_version)

    def test_credential_shapes_in_all_four_columns_are_replaced_and_counted(self) -> None:
        """合同：凭据形状的内容照常过滤、不留存——四列各自命中、各自计数。"""

        capture = _capture(
            question=f"帮我用 token={_FAKE_TOKEN} 查一下",
            answer=f"已用 Authorization: Bearer {_FAKE_TOKEN} 查到 1234。",
            tool_input={"metric": "new_users", "api_key": _FAKE_TOKEN},
            result=f"ok token={_FAKE_TOKEN}",
        )

        record = build_qa_corpus_record(
            capture,
            task=_Task,
            decision=_decision(f"结果 1234，凭据 Bearer {_FAKE_TOKEN}"),
            report=_report(),
            system_prompt_digest=None,
            model=None,
        )

        for name in (
            "question_content",
            "answer_delivered",
            "answer_model_raw",
        ):
            with self.subTest(column=name):
                self.assertNotIn(_FAKE_TOKEN, getattr(record, name))
        self.assertNotIn(_FAKE_TOKEN, str(record.tool_calls_payload()))
        self.assertGreater(record.question_redaction_count, 0)
        self.assertGreater(record.answer_delivered_redaction_count, 0)
        self.assertGreater(record.answer_model_raw_redaction_count, 0)
        self.assertGreater(record.tool_calls_redaction_count, 0)

    def test_delivered_text_comes_from_the_decision_not_the_model_text(self) -> None:
        """实收正文与模型原文是两列：拒发回合两者必然不同，都要如实保留。"""

        capture = _capture(
            question="问题", answer="模型原文", tool_input={"metric": "m"}, result="ok"
        )

        record = build_qa_corpus_record(
            capture,
            task=_Task,
            decision=_decision(
                "结果因安全策略未能展示",
                terminal_kind="redacted_withheld",
                failure_code="redacted_withheld",
            ),
            report=_report(user_result="redacted_withheld", withheld=True),
            system_prompt_digest=None,
            model=None,
        )

        self.assertEqual(record.answer_delivered, "结果因安全策略未能展示")
        self.assertEqual(record.answer_model_raw, "模型原文")
        self.assertEqual(record.terminal_kind, "redacted_withheld")
        self.assertEqual(record.user_result, "redacted_withheld")
        self.assertEqual(record.failure_code, "redacted_withheld")
        self.assertTrue(record.output_safety_withheld)

    def test_a_report_without_user_result_is_recorded_as_unknown(self) -> None:
        capture = _capture(question="问题", answer="回答", tool_input={}, result="")

        record = build_qa_corpus_record(
            capture,
            task=_Task,
            decision=_decision("回答", terminal_kind="failed", failure_code="turn_timeout"),
            report={"turn": {"closed": False}, "failure": {"code": "turn_timeout"}},
            system_prompt_digest=None,
            model=None,
        )

        self.assertEqual(record.user_result, "unknown")
        self.assertEqual(record.failure_code, "turn_timeout")
        self.assertFalse(record.output_safety_withheld)


class RecordShapeTests(unittest.TestCase):
    """构造即校验：固定码列到不了数据库的 CHECK 才被拒绝。"""

    def _record(self, **overrides) -> QaCorpusRecord:
        base = QaCorpusRecord(
            task_id="tsk-1",
            conversation_id="cnv-1",
            user_id="usr-1",
            trace_id=None,
            task_created_at=None,
            question_content="问",
            question_redaction_count=0,
            answer_delivered="答",
            answer_delivered_redaction_count=0,
            answer_model_raw="答",
            answer_model_raw_redaction_count=0,
            tool_calls=(),
            terminal_kind="success",
            user_result="obtained",
            failure_code=None,
            output_safety_withheld=False,
            worker_id="worker-1",
            worker_version="unknown",
            target_worker_version="stable",
            system_prompt_digest=None,
            model=None,
        )
        return replace(base, **overrides)

    def test_a_valid_record_is_accepted(self) -> None:
        self.assertEqual(self._record().tool_calls_payload(), [])

    def test_bad_shapes_are_refused(self) -> None:
        cases = {
            "terminal_kind": "done",
            "user_result": "Obtained result",
            "question_redaction_count": -1,
            "answer_delivered_redaction_count": True,
            "task_id": "",
            "user_id": None,
            "failure_code": "x" * 129,
            "output_safety_withheld": "yes",
            "tool_calls": [],
            "answer_model_raw": None,
        }
        for name, value in cases.items():
            with self.subTest(field=name):
                with self.assertRaises(ValueError):
                    self._record(**{name: value})

    def test_tool_calls_payload_and_count_follow_the_captured_calls(self) -> None:
        call = CapturedToolCall(
            tool_use_id="t1",
            tool_name=_TOOL,
            tool_input={"metric": "m"},
            result_summary={"content": "ok", "truncated": False},
            redaction_count=2,
        )
        record = self._record(tool_calls=(call, call))

        self.assertEqual(record.tool_calls_redaction_count, 4)
        self.assertEqual(record.tool_calls_payload()[0]["tool_name"], _TOOL)


class ReaderPredicateTests(unittest.TestCase):
    """默认拒绝：只有 active 且没有撤销时间的登记才是读取者。"""

    def _entry(self, **overrides) -> QaCorpusReaderEntry:
        base = QaCorpusReaderEntry(
            id="qcr_1",
            feishu_open_id="ou_reader_fake",
            label="corpus-reader",
            entry_status="active",
            granted_by="ou_admin_fake",
            granted_at=datetime(2026, 9, 14, tzinfo=UTC),
        )
        return replace(base, **overrides)

    def test_missing_revoked_and_inconsistent_entries_are_refused(self) -> None:
        self.assertFalse(is_authorized_corpus_reader(None))
        self.assertFalse(is_authorized_corpus_reader(self._entry(entry_status="revoked")))
        self.assertFalse(
            is_authorized_corpus_reader(
                self._entry(entry_status="revoked", revoked_at=datetime(2026, 9, 15, tzinfo=UTC))
            )
        )
        self.assertFalse(
            is_authorized_corpus_reader(self._entry(revoked_at=datetime(2026, 9, 15, tzinfo=UTC)))
        )

    def test_an_active_entry_is_authorized(self) -> None:
        self.assertTrue(is_authorized_corpus_reader(self._entry()))


class SharedRedactionModuleTests(unittest.TestCase):
    """脱敏纯函数只有一份实现：内测采集模块再导出的是共用模块的同一个对象。"""

    def test_the_innertest_module_re_exports_the_shared_functions(self) -> None:
        self.assertIs(innertest_content_capture._redact_json, content_redaction.redact_json)
        self.assertIs(innertest_content_capture._stringify, content_redaction.stringify_tool_result)
        self.assertIs(innertest_content_capture._truncate, content_redaction.truncate_summary)
        self.assertEqual(
            innertest_content_capture.MAX_TOOL_RESULT_SUMMARY_BYTES,
            content_redaction.MAX_TOOL_RESULT_SUMMARY_BYTES,
        )

    def test_redact_json_counts_hits_in_keys_and_nested_leaves(self) -> None:
        redacted, count = content_redaction.redact_json(
            {"a": [f"token={_FAKE_TOKEN}", {"b": f"Bearer {_FAKE_TOKEN}"}], "n": 1}
        )

        self.assertNotIn(_FAKE_TOKEN, str(redacted))
        self.assertEqual(redacted["n"], 1)
        self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()
