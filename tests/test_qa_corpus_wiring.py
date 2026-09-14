"""问答留存语料在 worker 收口处的接线断言（Issue #664，合同第三条例外）。

纯逻辑组用假队列与假执行器钉住写入点的三条纪律：默认关闭时不碰执行器、语料里的实收
正文与写进投递事件的终态正文逐字同源（写入点必须在终态收口**之后**）、写库失败不影响
终态。真库组用假 SDK 驱动真实 ``WorkerTurnExecutor`` 与真实 ``WorkerService`` 收口路径，
再从 ``qa_corpus`` 逐字回读问题原文、实收正文与模型原文。假 SDK 只证明本侧装配是通的，
证明不了真实 SDK 会触发这些事件（那是 L4a 的职责）。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows
from test_worker_entry import READ_ONLY_TOOL, FakeAgentSDK, ok_result, worker_env

from lingxi.adapters.postgres_conversation import ClaimedTask, TaskContext
from lingxi.apps.worker.config import WorkerConfig, load_config
from lingxi.apps.worker.service import WorkerService
from lingxi.apps.worker.service_ports import WorkerObservers
from lingxi.core.innertest_content_capture import ContentCaptureRecord
from lingxi.core.qa_corpus import QaCorpusRecord

DSN = os.environ.get("LINGXI_POSTGRES_DSN")
SKIP_DB = (
    "需要 LINGXI_POSTGRES_DSN 才能运行问答留存语料的真库接线断言"
    if not DSN
    else "LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，无法运行真库接线断言"
)
TASK_ID = "tsk_01HXYZ00000000000000000QAC"
USER_ID = "usr-1"
QUESTION = "近 7 天的活跃用户数是多少？"


def _seed_user_mcp_config(root: str, user_id: str) -> None:
    home = Path(root) / user_id
    home.mkdir(parents=True, exist_ok=True)
    (home / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "query": {
                        "type": "http",
                        "url": "https://example.invalid/mcp",
                        "headers": {"Authorization": "Bearer test-token"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )


class FakeQueue:
    """最小队列替身：一个任务、记下写进去的终态事件。"""

    def __init__(self, *, task_id: str = TASK_ID, prompt: str = QUESTION) -> None:
        self.claimed = ClaimedTask(
            task_id=task_id,
            conversation_id="cnv-1",
            user_id=USER_ID,
            prompt=prompt,
            resumed_session=False,
            target_worker_version="stable",
            attempts=1,
        )
        self.context = TaskContext(
            task_id=task_id,
            conversation_id="cnv-1",
            user_id=USER_ID,
            prompt=prompt,
            resumed_session=False,
            target_worker_version="stable",
            attempts=1,
            reply_to_message_id="msg-1",
            chat_id="chat-1",
            thread_id="topic-1",
            agent_session_id=None,
            stop_requested=False,
            side_effect_state="none",
        )
        self.terminals: list[dict[str, object]] = []

    def claim(self, **kwargs: object) -> list[ClaimedTask]:
        claimed, self.claimed = self.claimed, None  # type: ignore[assignment]
        return [claimed] if claimed is not None else []

    def task_context(self, **kwargs: object) -> TaskContext:
        return self.context

    def mark_side_effect(self, **kwargs: object) -> bool:
        return True

    def heartbeat(self, **kwargs: object) -> bool:
        return True

    def stop_requested(self, **kwargs: object) -> bool:
        return False

    def append_delivery_event(self, **kwargs: object) -> None:
        return None

    def write_terminal_event(self, **kwargs: object) -> None:
        self.terminals.append(kwargs)


class _FailingExecutor:
    """跑不出结果的执行器，但收集到了问题与模型原文；不定义任何语料以外的钩子。"""

    async def run_turn(self, prompt: str, **kwargs: object) -> dict:
        return {
            "turn": {
                "closed": False,
                "final_text": "",
                "session_id": None,
                "user_result": "unknown",
            },
            "failure": {"code": "session_failed", "message": "合成失败"},
        }

    def build_content_capture_record(self, **kwargs: object) -> ContentCaptureRecord:
        return ContentCaptureRecord(
            task_id=kwargs["task_id"],  # type: ignore[arg-type]
            worker_id=kwargs["worker_id"],  # type: ignore[arg-type]
            question_content=kwargs["question"],  # type: ignore[arg-type]
            question_redaction_count=0,
            answer_content="模型半途的原文",
            answer_redaction_count=0,
            tool_calls=(),
        )


class WiringTests(unittest.TestCase):
    """写入点的三条纪律：默认不碰执行器、实收正文同源、失败不影响终态。"""

    def setUp(self) -> None:
        self._root = tempfile.TemporaryDirectory(prefix="lingxi-qa-corpus-")
        self.addCleanup(self._root.cleanup)
        _seed_user_mcp_config(self._root.name, USER_ID)

    def _config(self, **overrides: object) -> WorkerConfig:
        values: dict[str, object] = {
            "question": "",
            "read_only_tools": ("mcp__q__read",),
            "trace_id": "01J00000000000000000000000",
            "turn_timeout_seconds": 1.0,
            "query_mcp_endpoint": "https://example.invalid/query",
            "worker_id": "worker-test",
            "heartbeat_interval_seconds": 0.01,
            "poll_interval_seconds": 0.01,
            "user_env_root": self._root.name,
        }
        values.update(overrides)
        return WorkerConfig(**values)  # type: ignore[arg-type]

    def _run(self, queue: FakeQueue, executor: object, **observers: object) -> None:
        service = WorkerService(
            config=self._config(),
            queue=queue,
            executor_factory=lambda config, marker: executor,
            observers=WorkerObservers(**observers),  # type: ignore[arg-type]
        )
        asyncio.run(asyncio.wait_for(service.process_once(), timeout=10))

    def test_default_disabled_never_touches_the_executor(self) -> None:
        """不配置 ``qa_corpus_writer`` 时执行器上任何采集钩子都不会被调用：用一个压根不
        定义这些方法的执行器证明，调用了就是 ``AttributeError``。"""

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {"closed": True, "final_text": "结果", "session_id": "s1"},
                    "failure": None,
                }

        queue = FakeQueue()
        self._run(queue, Executor())

        self.assertEqual(queue.terminals[0]["terminal_kind"], "success")

    def test_delivered_text_in_the_corpus_is_byte_for_byte_the_terminal_event_content(
        self,
    ) -> None:
        """实收正文同源：失败终态会在收口时追加追溯号，语料里的那一份必须是追加之后的。

        语料四列都过同一份凭据形状判据，追溯号本身（26 位含数字的裸串）会被判成裸令牌
        而遮蔽——这是已知的误伤，任务标识与追溯号列上仍能原样查到；因此同源断言比的是
        「终态正文过同一判据之后」的结果，成功回合的正文上游已过滤，逐字节相同。

        **变异验红**：把 ``service.py`` 里 ``self._qa_corpus.record(...)`` 挪到
        ``_finish_terminal`` 之前（用 ``decide_terminal`` 的原始结果），本用例必须变红——
        追溯号只在收口时才追加进正文。
        """
        from lingxi.core.execution.audit import redact_free_text

        received: list[QaCorpusRecord] = []
        queue = FakeQueue()

        self._run(queue, _FailingExecutor(), qa_corpus_writer=received.append)

        self.assertEqual(len(received), 1)
        record = received[0]
        terminal = queue.terminals[0]
        self.assertEqual(terminal["terminal_kind"], "failed")
        self.assertIn("T-01HXYZ00000000000000000QAC", str(terminal["content"]))
        self.assertEqual(record.answer_delivered, redact_free_text(str(terminal["content"])))
        self.assertTrue(record.answer_delivered.startswith("本次任务未取得可用结果"))
        self.assertIn("任务参考号", record.answer_delivered)
        self.assertEqual(record.question_content, QUESTION)
        self.assertEqual(record.answer_model_raw, "模型半途的原文")
        self.assertEqual(record.task_id, TASK_ID)
        self.assertEqual(record.user_id, USER_ID)
        self.assertEqual(record.terminal_kind, "failed")
        self.assertEqual(record.failure_code, "session_failed")
        self.assertEqual(record.worker_id, "worker-test")

    def test_a_writer_failure_does_not_change_the_terminal_outcome(self) -> None:
        def writer(record: QaCorpusRecord) -> bool:
            raise RuntimeError("库不可达")

        queue = FakeQueue()
        with self.assertLogs("lingxi.apps.worker.service", level="ERROR") as logs:
            self._run(queue, _FailingExecutor(), qa_corpus_writer=writer)

        self.assertEqual(queue.terminals[0]["terminal_kind"], "failed")
        self.assertEqual(len(logs.output), 1)
        self.assertIn("RuntimeError", logs.output[0])
        self.assertIn(TASK_ID, logs.output[0])
        self.assertNotIn("模型半途的原文", logs.output[0])

    def test_nothing_captured_means_nothing_written(self) -> None:
        class Executor(_FailingExecutor):
            def build_content_capture_record(self, **kwargs: object) -> None:
                return None

        received: list[QaCorpusRecord] = []
        queue = FakeQueue()
        self._run(queue, Executor(), qa_corpus_writer=received.append)

        self.assertEqual(received, [])
        self.assertEqual(queue.terminals[0]["terminal_kind"], "failed")


@unittest.skipUnless(DSN and psycopg_available(), SKIP_DB)
class RealTurnToDatabaseTest(unittest.TestCase):
    """假 SDK 驱动真实执行器与真实收口路径，语料真的落进 ``qa_corpus`` 并逐字回读。"""

    @classmethod
    def setUpClass(cls) -> None:
        ensure_production_schema(DSN)

    def setUp(self) -> None:
        from lingxi.adapters.postgres import connect

        reset_production_rows(DSN)
        with connect(DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO app_user
                     (id, feishu_open_id, feishu_user_id, feishu_union_id,
                      display_name, department, tenant_key, provisioning_state)
                   VALUES (%s, 'ou_qa_wiring', 'u_qa_wiring', 'un_qa_wiring',
                           '化名甲', '数据部', 'tk_qa_wiring', 'active')""",
                (USER_ID,),
            )
            connection.commit()
        self._root = tempfile.TemporaryDirectory(prefix="lingxi-qa-corpus-db-")
        self.addCleanup(self._root.cleanup)
        _seed_user_mcp_config(self._root.name, USER_ID)

    def test_question_delivered_text_and_model_text_round_trip_through_the_table(self) -> None:
        from lingxi.adapters.postgres_qa_corpus import PostgresQaCorpus

        FakeAgentSDK(
            [
                {
                    "kind": "tool",
                    "tool": READ_ONLY_TOOL,
                    "input": {"metric": "dau"},
                    "result": ok_result(),
                },
                {"kind": "text", "text": "近 7 天日活是 1024。"},
            ]
        ).install(self)
        config = load_config(
            worker_env(
                LINGXI_WORKER_QUESTION=None,
                LINGXI_QUERY_MCP_ENDPOINT="https://example.invalid/query",
                LINGXI_USER_ENV_ROOT=self._root.name,
                LINGXI_QA_CORPUS_RETENTION="1",
                LINGXI_WORKER_ID="worker-e2e",
            ),
            require_question=False,
            queue_mode=True,
        )
        self.assertTrue(config.qa_corpus_retention_enabled)
        store = PostgresQaCorpus(DSN)
        queue = FakeQueue()
        service = WorkerService(
            config=config, queue=queue, observers=WorkerObservers(qa_corpus_writer=store.record)
        )

        asyncio.run(asyncio.wait_for(service.process_once(), timeout=30))

        terminal = queue.terminals[0]
        self.assertEqual(terminal["terminal_kind"], "success")
        row = store.for_task(TASK_ID)
        self.assertIsNotNone(row)
        record = row.record
        self.assertEqual(record.question_content, QUESTION)
        self.assertEqual(record.answer_delivered, terminal["content"])
        self.assertEqual(record.answer_model_raw, "近 7 天日活是 1024。")
        self.assertEqual(record.user_id, USER_ID)
        self.assertEqual(record.worker_id, "worker-e2e")
        self.assertEqual(record.terminal_kind, "success")
        self.assertEqual(record.user_result, "obtained")
        self.assertEqual([call.tool_name for call in record.tool_calls], [READ_ONLY_TOOL])
        self.assertEqual(record.tool_calls[0].tool_input, {"metric": "dau"})
        self.assertEqual(record.question_redaction_count, 0)
        self.assertEqual(record.answer_delivered_redaction_count, 0)
        self.assertEqual(record.answer_model_raw_redaction_count, 0)
        self.assertEqual(record.tool_calls_redaction_count, 0)


if __name__ == "__main__":
    unittest.main()
