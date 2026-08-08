"""Issue #90 的队列消费、卡片与失败终态断言。

真库组覆盖 V-队列-08/09/13 的状态与释放；纯逻辑组覆盖 V-会话-06、V-卡片-01/02/03
和 worker 收口，避免把外部飞书/CardKit L4a 误写成已验证。
"""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone

from gateway_fakes import FakeAudit, FakeReactions, FakeReplies, CallLog
from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_conversation import (
    ClaimedTask,
    PostgresGatewayStore,
    PostgresTaskQueue,
    PostgresTaskQueueListener,
    TerminalTask,
    _Transaction,
)
from lingxi.apps.worker.config import WorkerConfig
from lingxi.apps.worker.delivery import CardTaskDelivery
from lingxi.apps.worker.service import WorkerService
from lingxi.config.content import default_content_catalog
from lingxi.core.conversation import EventPipeline, InboundMessage
from lingxi.core.execution.card_stream import CardRateLimiter, CardStream
from postgres_schema import ensure_production_schema, reset_production_rows


DSN = os.environ.get("LINGXI_POSTGRES_DSN")
SKIP_DB = "需要 LINGXI_POSTGRES_DSN 才能运行真库队列断言"


def worker_config(**overrides: object) -> WorkerConfig:
    values: dict[str, object] = {
        "question": "",
        "read_only_tool": "mcp__q__read",
        "trace_id": "01J00000000000000000000000",
        "turn_timeout_seconds": 1.0,
        "worker_id": "worker-test",
        "target_worker_version": "stable",
        "heartbeat_interval_seconds": 0.01,
        "poll_interval_seconds": 0.01,
    }
    values.update(overrides)
    return WorkerConfig(**values)  # type: ignore[arg-type]


class RecordingCards:
    def __init__(self, *, fail: str | None = None) -> None:
        self.fail = fail
        self.calls: list[tuple[str, int | None]] = []
        self.bodies: list[str] = []

    def create(self, **kwargs: object) -> str:
        self.calls.append(("create", None))
        self.bodies.append(kwargs["card"].body)  # type: ignore[union-attr]
        if self.fail == "create":
            raise RuntimeError("card create")
        return "card-1"

    def update(self, *, sequence: int, **kwargs: object) -> None:
        self.calls.append(("update", sequence))
        self.bodies.append(kwargs["card"].body)  # type: ignore[union-attr]
        if self.fail == "update":
            raise RuntimeError("card update")

    def close(self, *, sequence: int, **kwargs: object) -> None:
        self.calls.append(("close", sequence))
        self.bodies.append(kwargs["card"].body)  # type: ignore[union-attr]
        if self.fail == "close":
            raise RuntimeError("card close")


class RecordingText:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def send_text(self, *, text: str, **kwargs: object) -> None:
        self.texts.append(text)


class CardStreamTests(unittest.TestCase):
    def test_sequence_is_monotonic_and_topic_updates_are_throttled(self) -> None:
        now = [0.0]
        cards = RecordingCards()
        text = RecordingText()
        stream = CardStream(
            chat_id="chat-a",
            thread_id="topic-a",
            reply_to_message_id="msg-a",
            transport=cards,
            fallback=text,
            monotonic=lambda: now[0],
            rate_limiter=CardRateLimiter(),
        )

        stream.start()
        stream.update(elapsed_seconds=0)
        now[0] = 0.5
        stream.update(elapsed_seconds=1)
        stream.finish(result="结果", elapsed_seconds=1)

        self.assertEqual(
            [sequence for kind, sequence in cards.calls if kind in {"update", "close"}],
            [1, 2, 3],
            "卡片更新与关闭必须使用严格递增序号；同话题 500ms 内的中间帧要被抑制",
        )
        self.assertIn("已完成 · 1 秒", cards.bodies[-2])
        self.assertEqual(text.texts, [])

    def test_card_failure_falls_back_to_same_topic_text(self) -> None:
        cards = RecordingCards(fail="update")
        text = RecordingText()
        stream = CardStream(
            chat_id="chat-a",
            thread_id="topic-a",
            reply_to_message_id="msg-a",
            transport=cards,
            fallback=text,
        )
        stream.start()
        stream.finish(failure=default_content_catalog().text("worker.failed"))
        stream.send_fallback(default_content_catalog().text("worker.failed"))

        self.assertTrue(stream.fallback_needed)
        self.assertEqual(text.texts, ["本次任务未取得可用结果，请稍后重试。"])


class FakeWorkerQueue:
    def __init__(self, *, stopped: bool = False) -> None:
        self.claimed = ClaimedTask(
            task_id="tsk-1",
            conversation_id="cnv-1",
            user_id="usr-1",
            prompt="问题",
            resumed_session=True,
            target_worker_version="stable",
            attempts=1,
            reply_to_message_id="msg-1",
            stop_requested=stopped,
        )
        from lingxi.adapters.postgres_conversation import TaskContext

        self.context = TaskContext(
            task_id="tsk-1",
            conversation_id="cnv-1",
            user_id="usr-1",
            prompt="问题",
            resumed_session=True,
            target_worker_version="stable",
            attempts=1,
            reply_to_message_id="msg-1",
            chat_id="chat-1",
            thread_id="topic-1",
            agent_session_id="old-session",
            stop_requested=stopped,
            side_effect_state="none",
        )
        self.finished: list[dict[str, object]] = []
        self.marked = 0

    def claim(self, **kwargs: object) -> list[ClaimedTask]:
        if self.claimed is None:  # type: ignore[comparison-overlap]
            return []
        claimed, self.claimed = self.claimed, None  # type: ignore[assignment]
        return [claimed]

    def task_context(self, **kwargs: object):
        return self.context

    def mark_side_effect(self, **kwargs: object) -> bool:
        self.marked += 1
        return True

    def heartbeat(self, **kwargs: object) -> bool:
        return True

    def stop_requested(self, **kwargs: object) -> bool:
        return self.context.stop_requested

    def finish(self, **kwargs: object) -> bool:
        self.finished.append(kwargs)
        return True


class RecordingDelivery:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    def start(self) -> None:
        self.events.append(("start", None))

    def progress(self, *, elapsed_seconds: int) -> None:
        self.events.append(("progress", elapsed_seconds))

    def complete(self, *, result: str, elapsed_seconds: int = 0) -> None:
        self.events.append(("complete", result))

    def fail(self, *, content) -> None:
        self.events.append(("fail", content.key))


class WorkerServiceTests(unittest.TestCase):
    def test_success_resumes_session_and_releases_with_new_session_id(self) -> None:
        queue = FakeWorkerQueue()
        delivery = RecordingDelivery()

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                self.kwargs = kwargs
                return {
                    "turn": {"closed": True, "final_text": "结果", "session_id": "new-session"},
                    "failure": None,
                }

        executor = Executor()
        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: executor,
            delivery_factory=lambda context, marker: delivery,
        )
        asyncio.run(service.process_once())

        self.assertEqual(delivery.events[-1], ("complete", "结果"))
        self.assertEqual(queue.finished[0]["status"], "succeeded")
        self.assertEqual(queue.finished[0]["agent_session_id"], "new-session")
        self.assertIsNotNone(executor.kwargs["resume_session_id"])

    def test_stop_and_timeout_are_terminal_and_release_the_topic(self) -> None:
        for stopped, failure_code, expected_status in (
            (True, None, "stopped"),
            (False, "turn_timeout", "failed"),
        ):
            with self.subTest(expected_status=expected_status):
                queue = FakeWorkerQueue(stopped=stopped)
                delivery = RecordingDelivery()

                class Executor:
                    async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                        return {
                            "turn": {"closed": False, "final_text": ""},
                            "failure": {"code": failure_code} if failure_code else None,
                        }

                service = WorkerService(
                    config=worker_config(),
                    queue=queue,
                    executor_factory=lambda config, marker: Executor(),
                    delivery_factory=lambda context, marker: delivery,
                )
                asyncio.run(service.process_once())
                self.assertEqual(queue.finished[0]["status"], expected_status)
                self.assertEqual(delivery.events[-1][0], "fail")
                expected_key = "worker.stopped" if stopped else "worker.running_timeout"
                self.assertEqual(delivery.events[-1][1], expected_key)
                expected_error = "stopped" if stopped else "running_timeout"
                self.assertEqual(queue.finished[0]["error_kind"], expected_error)


@unittest.skipUnless(DSN, SKIP_DB)
class RealQueueTerminalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert DSN is not None
        ensure_production_schema(DSN)

    def setUp(self) -> None:
        assert DSN is not None
        reset_production_rows(DSN)
        self.queue = PostgresTaskQueue(DSN)
        with connect(DSN) as connection:
            with connection.transaction():
                connection.execute(
                    """INSERT INTO app_user
                       (id, feishu_open_id, feishu_user_id, feishu_union_id,
                        display_name, department, tenant_key, provisioning_state)
                       VALUES ('usr-90','ou-90','u-90','un-90','张三','数据部','tk-90','active')"""
                )

    def _insert_old_task(
        self,
        *,
        task_id: str,
        conversation_id: str,
        version: str = "stable",
        status: str = "queued",
        side_effect_state: str = "none",
        attempts: int = 0,
    ) -> None:
        assert DSN is not None
        with connect(DSN) as connection:
            with connection.transaction():
                connection.execute(
                    """INSERT INTO conversation
                       (id,user_id,feishu_chat_id,feishu_thread_id,running_task_id)
                       VALUES (%s,'usr-90',%s,%s,%s)""",
                    (
                        conversation_id,
                        f"chat-{conversation_id}",
                        f"topic-{conversation_id}",
                        task_id,
                    ),
                )
                connection.execute(
                    """INSERT INTO task
                       (id,conversation_id,user_id,inbound_event_id,prompt,status,
                        target_worker_version,worker_id,heartbeat_at,attempts,
                        created_at,scheduled_at,side_effect_state,content_expires_at)
                       VALUES (%s,%s,'usr-90',%s,'问题',%s,%s,%s,
                               now()-interval '5 minutes',%s,
                               now()-interval '5 minutes',now()-interval '5 minutes',%s,now())""",
                    (
                        task_id,
                        conversation_id,
                        f"event-{task_id}",
                        status,
                        version,
                        "worker-old" if status == "running" else None,
                        attempts,
                        side_effect_state,
                    ),
                )

    def test_queued_without_worker_is_failed_and_releases_topic(self) -> None:
        self._insert_old_task(task_id="tsk-q", conversation_id="cnv-q")
        terminals = self.queue.reclaim_queued(max_wait=timedelta(minutes=3))
        self.assertEqual([item.error_kind for item in terminals], ["queued_timeout"])
        with connect(DSN) as connection:
            self.assertEqual(
                connection.execute("SELECT status FROM task WHERE id='tsk-q'").fetchone()[0],
                "failed",
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT running_task_id FROM conversation WHERE id='cnv-q'"
                ).fetchone()[0]
            )

    def test_unavailable_version_is_not_changed_to_stable(self) -> None:
        self._insert_old_task(task_id="tsk-c", conversation_id="cnv-c", version="canary")
        self._insert_old_task(task_id="tsk-s", conversation_id="cnv-s", version="stable")
        terminals = self.queue.fail_unavailable_versions(
            available_versions=("stable",), unavailable_for=timedelta(minutes=3)
        )
        self.assertEqual([item.task_id for item in terminals], ["tsk-c"])
        with connect(DSN) as connection:
            row = connection.execute(
                "SELECT status,target_worker_version FROM task WHERE id='tsk-c'"
            ).fetchone()
            self.assertEqual(row, ("failed", "canary"))
            self.assertEqual(
                connection.execute("SELECT status FROM task WHERE id='tsk-s'").fetchone()[0],
                "queued",
            )

    def test_stale_safe_retry_then_exhaustion_and_side_effect_no_replay(self) -> None:
        self._insert_old_task(
            task_id="tsk-r", conversation_id="cnv-r", status="running", attempts=1
        )
        self.assertEqual(
            self.queue.reclaim_stale(older_than=timedelta(seconds=90)), ["tsk-r"]
        )
        claimed = self.queue.claim(worker_id="worker-2", target_worker_version="stable")
        self.assertEqual(claimed[0].attempts, 2)
        with connect(DSN) as connection:
            with connection.transaction():
                connection.execute(
                    "UPDATE task SET heartbeat_at=now()-interval '2 minutes' WHERE id='tsk-r'"
                )
        self.assertEqual(self.queue.reclaim_stale(older_than=timedelta(seconds=90)), [])
        with connect(DSN) as connection:
            self.assertEqual(
                connection.execute("SELECT status FROM task WHERE id='tsk-r'").fetchone()[0],
                "failed",
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT running_task_id FROM conversation WHERE id='cnv-r'"
                ).fetchone()[0]
            )

        self._insert_old_task(
            task_id="tsk-x",
            conversation_id="cnv-x",
            status="running",
            side_effect_state="possible",
            attempts=1,
        )
        self.assertEqual(self.queue.reclaim_stale(older_than=timedelta(seconds=90)), [])
        with connect(DSN) as connection:
            self.assertEqual(
                connection.execute("SELECT status FROM task WHERE id='tsk-x'").fetchone()[0],
                "failed",
            )

    def test_listener_receives_committed_notify(self) -> None:
        assert DSN is not None
        with PostgresTaskQueueListener(DSN) as listener:
            with connect(DSN) as connection:
                with connection.transaction():
                    connection.execute("NOTIFY task_queued")
            self.assertTrue(listener.wait(timeout_seconds=2.0))

    def test_claim_context_keeps_reply_scope_and_finish_persists_session(self) -> None:
        assert DSN is not None
        pipeline = EventPipeline(
            store=PostgresGatewayStore(DSN),
            reactions=FakeReactions(CallLog()),
            replies=FakeReplies(CallLog()),
            audit=FakeAudit(CallLog()),
        )
        outcome = pipeline.handle_message(
            InboundMessage(
                "evt-session",
                "im.message.receive_v1",
                "ou-90",
                "chat-session",
                "topic-session",
                "msg-session",
                "问题",
                "trace-session",
            )
        )
        claimed = self.queue.claim(worker_id="worker-session", target_worker_version="stable")
        self.assertEqual(claimed[0].task_id, outcome.task_id)
        context = self.queue.task_context(
            task_id=claimed[0].task_id, worker_id="worker-session"
        )
        assert context is not None
        self.assertEqual(context.reply_to_message_id, "msg-session")
        self.assertTrue(
            self.queue.finish(
                task_id=claimed[0].task_id,
                conversation_id=claimed[0].conversation_id,
                status="succeeded",
                worker_id="worker-session",
                agent_session_id="session-saved",
            )
        )
        with connect(DSN) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT agent_session_id,running_task_id FROM conversation WHERE id=%s",
                    (claimed[0].conversation_id,),
                ).fetchone(),
                ("session-saved", None),
            )

    def test_enqueue_failure_has_one_catalog_notice_and_reprocesses_after_recovery(self) -> None:
        assert DSN is not None
        log = CallLog()
        replies = FakeReplies(log)
        pipeline = EventPipeline(
            store=PostgresGatewayStore(DSN),
            reactions=FakeReactions(log),
            replies=replies,
            audit=FakeAudit(log),
        )
        message = InboundMessage(
            "evt-queue-failure",
            "im.message.receive_v1",
            "ou-90",
            "chat-90",
            "topic-90",
            "msg-queue-failure",
            "问题",
            "trace-queue-failure",
        )
        original = _Transaction.insert_task

        def fail_insert(self, **kwargs: object) -> None:
            raise RuntimeError("fault injection")

        _Transaction.insert_task = fail_insert  # type: ignore[assignment]
        try:
            first = pipeline.handle_message(message)
            second = pipeline.handle_message(message)
        finally:
            _Transaction.insert_task = original

        self.assertIsNone(first.handled_as)
        self.assertIsNone(second.handled_as)
        self.assertEqual(log.count("reply.send_text"), 1)
        self.assertIn("LX-QUEUE-001", log.fields("reply.send_text")[0]["text"])
        self.assertEqual(
            pipeline.handle_message(message).handled_as.value,
            "task_queued",
            "失败事务没有落 inbound_event，故故障恢复后重投必须能够完整入队",
        )
