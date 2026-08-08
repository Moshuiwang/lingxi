"""Issue #90 的队列消费、卡片与失败终态断言。

真库组覆盖 V-队列-06/08/09、V-会话-07/11/13 的状态、隔离、轮询与出站收口；纯逻辑组
覆盖 V-会话-06、V-卡片-01/02/03 和 worker 收口，避免把外部飞书/CardKit L4a 误写成
已验证。
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
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
    def __init__(self, *, fail: bool = False) -> None:
        self.texts: list[str] = []
        self.calls: list[dict[str, object]] = []
        self.fail = fail

    def send_text(
        self,
        *,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
        text: str,
    ) -> None:
        self.calls.append(
            {
                "chat_id": chat_id,
                "thread_id": thread_id,
                "reply_to_message_id": reply_to_message_id,
                "text": text,
            }
        )
        self.texts.append(text)
        if self.fail:
            raise RuntimeError("text fallback")


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
        self.assertEqual(
            text.calls[0]["chat_id"],
            "chat-a",
            "V-卡片-03：文本回退必须保留原 chat_id",
        )
        self.assertEqual(
            text.calls[0]["thread_id"],
            "topic-a",
            "V-卡片-03：文本回退必须保留原 thread_id",
        )


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
        self.failed_contents = []

    def start(self) -> None:
        self.events.append(("start", None))

    def progress(self, *, elapsed_seconds: int) -> None:
        self.events.append(("progress", elapsed_seconds))

    def complete(self, *, result: str, elapsed_seconds: int = 0) -> None:
        self.events.append(("complete", result))

    def fail(self, *, content) -> None:
        self.failed_contents.append(content)
        self.events.append(("fail", content.key))


class DroppingNotifyListener:
    """真库消费者测试用的丢通知监听器：等待轮询上限后仍返回未唤醒。"""

    def __init__(self) -> None:
        self.wait_started = threading.Event()
        self.wait_calls: list[float] = []

    def __enter__(self) -> "DroppingNotifyListener":
        return self

    def wait(self, *, timeout_seconds: float) -> bool:
        self.wait_calls.append(timeout_seconds)
        self.wait_started.set()
        time.sleep(timeout_seconds)
        return False

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        return None


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
            config=worker_config(
                external_texts=(("metric.description", "指标目录中的已知描述"),),
            ),
            queue=queue,
            executor_factory=lambda config, marker: executor,
            delivery_factory=lambda context, marker: delivery,
        )
        asyncio.run(service.process_once())

        self.assertEqual(delivery.events[-1], ("complete", "结果"))
        self.assertEqual(queue.finished[0]["status"], "succeeded")
        self.assertEqual(queue.finished[0]["agent_session_id"], "new-session")
        self.assertIsNotNone(executor.kwargs["resume_session_id"])
        self.assertEqual(
            executor.kwargs["external_texts"],
            (("metric.description", "指标目录中的已知描述"),),
        )

    def test_success_result_survives_card_update_failure_in_the_same_topic(self) -> None:
        """V-卡片-03：成功回合的卡片更新失败时补发完整结果且仍为 succeeded。"""

        queue = FakeWorkerQueue()
        cards = RecordingCards(fail="update")
        text = RecordingText()
        delivery = CardTaskDelivery(
            chat_id=queue.context.chat_id,
            thread_id=queue.context.thread_id,
            reply_to_message_id=queue.context.reply_to_message_id or "",
            cards=cards,
            replies=text,
        )

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                kwargs["on_stream_event"]({"kind": "assistant_message"})  # type: ignore[index]
                return {
                    "turn": {
                        "closed": True,
                        "final_text": "答案串：123 万元",
                        "session_id": "new-session",
                    },
                    "failure": None,
                }

        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
            delivery_factory=lambda context, marker: delivery,
        )
        asyncio.run(service.process_once())

        self.assertEqual(queue.finished[0]["status"], "succeeded")
        self.assertEqual(len(text.calls), 1, "成功结果只能补发一次")
        self.assertIn("答案串：123 万元", text.calls[0]["text"])
        self.assertNotIn("本次任务未取得可用结果", text.calls[0]["text"])
        self.assertEqual(text.calls[0]["chat_id"], "chat-1")
        self.assertEqual(text.calls[0]["thread_id"], "topic-1")

    def test_success_result_is_failed_only_when_text_fallback_also_fails(self) -> None:
        """V-卡片-03：文本回退本身失败才把成功回合降为 failed。"""

        queue = FakeWorkerQueue()
        delivery = CardTaskDelivery(
            chat_id=queue.context.chat_id,
            thread_id=queue.context.thread_id,
            reply_to_message_id=queue.context.reply_to_message_id or "",
            cards=RecordingCards(fail="update"),
            replies=RecordingText(fail=True),
        )

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                kwargs["on_stream_event"]({"kind": "assistant_message"})  # type: ignore[index]
                return {
                    "turn": {"closed": True, "final_text": "答案串", "session_id": None},
                    "failure": None,
                }

        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
            delivery_factory=lambda context, marker: delivery,
        )
        asyncio.run(service.process_once())

        self.assertEqual(queue.finished[0]["status"], "failed")
        self.assertEqual(queue.finished[0]["error_kind"], "delivery_failed")

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

    def _scalar(self, sql: str, parameters: tuple[object, ...] = ()) -> object:
        assert DSN is not None
        with connect(DSN) as connection:
            return connection.execute(sql, parameters).fetchone()[0]

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> None:
        assert DSN is not None
        with connect(DSN) as connection:
            with connection.transaction():
                connection.execute(sql, parameters)

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

    def test_dropped_notify_is_recovered_by_polling_within_configured_bound(self) -> None:
        """V-队列-06：丢弃 NOTIFY 后仍在 poll_interval 内领取任务。"""

        assert DSN is not None
        listener = DroppingNotifyListener()
        executor_claimed = threading.Event()
        claimed_at: list[float] = []

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                claimed_at.append(time.monotonic())
                executor_claimed.set()
                return {
                    "turn": {"closed": True, "final_text": "轮询结果", "session_id": None},
                    "failure": None,
                }

        poll_interval = 0.05
        service = WorkerService(
            config=worker_config(
                worker_id="worker-poll",
                poll_interval_seconds=poll_interval,
                max_concurrency=1,
            ),
            queue=self.queue,
            executor_factory=lambda config, marker: Executor(),
            delivery_factory=lambda context, marker: RecordingDelivery(),
            listener_factory=lambda: listener,
        )

        async def scenario() -> float:
            stop_event = asyncio.Event()
            consumer = asyncio.create_task(service.run(stop_event=stop_event))
            started = await asyncio.to_thread(listener.wait_started.wait, 2.0)
            self.assertTrue(started, "消费者应先进入等待，才能模拟通知丢失")
            queued_at = time.monotonic()
            pipeline = EventPipeline(
                store=PostgresGatewayStore(DSN),
                reactions=FakeReactions(CallLog()),
                replies=FakeReplies(CallLog()),
                audit=FakeAudit(CallLog()),
            )
            pipeline.handle_message(
                InboundMessage(
                    "evt-dropped-notify",
                    "im.message.receive_v1",
                    "ou-90",
                    "chat-poll",
                    "topic-poll",
                    "msg-poll",
                    "问题",
                    "trace-poll",
                )
            )
            claimed = await asyncio.to_thread(executor_claimed.wait, 2.0)
            self.assertTrue(claimed, "丢通知后消费者仍应通过轮询领取")
            stop_event.set()
            await asyncio.wait_for(consumer, timeout=2.0)
            return queued_at

        queued_at = asyncio.run(scenario())
        self.assertTrue(listener.wait_calls)
        self.assertTrue(claimed_at)
        self.assertLessEqual(
            claimed_at[0] - queued_at,
            poll_interval + 0.2,
            "丢弃 NOTIFY 后领取不得超过配置轮询上限（含测试调度余量）",
        )
        self.assertEqual(
            self.queue.claim(worker_id="probe", target_worker_version="stable"),
            [],
            "任务已由消费者领取并完成，不应仍留在 queued",
        )

    def test_two_users_keep_session_and_delivery_scope_separate(self) -> None:
        """V-会话-07：不同用户的任务不会串用会话或投递定位。"""

        assert DSN is not None
        with connect(DSN) as connection:
            with connection.transaction():
                connection.execute(
                    """INSERT INTO app_user
                       (id, feishu_open_id, feishu_user_id, feishu_union_id,
                        display_name, department, tenant_key, provisioning_state)
                       VALUES ('usr-91','ou-91','u-91','un-91','李四','销售部','tk-91','active')"""
                )
        log = CallLog()
        pipeline = EventPipeline(
            store=PostgresGatewayStore(DSN),
            reactions=FakeReactions(log),
            replies=FakeReplies(log),
            audit=FakeAudit(log),
        )
        pipeline.handle_message(
            InboundMessage(
                "evt-user-a",
                "im.message.receive_v1",
                "ou-90",
                "chat-user-a",
                "topic-user-a",
                "msg-user-a",
                "答案 A",
                "trace-user-a",
            )
        )
        pipeline.handle_message(
            InboundMessage(
                "evt-user-b",
                "im.message.receive_v1",
                "ou-91",
                "chat-user-b",
                "topic-user-b",
                "msg-user-b",
                "答案 B",
                "trace-user-b",
            )
        )

        deliveries: list[tuple[str, str, str | None, RecordingDelivery]] = []

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {"closed": True, "final_text": prompt, "session_id": None},
                    "failure": None,
                }

        def delivery_factory(context, marker):
            delivery = RecordingDelivery()
            deliveries.append((context.user_id, context.chat_id, context.thread_id, delivery))
            return delivery

        service = WorkerService(
            config=worker_config(max_concurrency=2),
            queue=self.queue,
            executor_factory=lambda config, marker: Executor(),
            delivery_factory=delivery_factory,
        )
        asyncio.run(service.process_once())

        self.assertEqual(
            {
                (user_id, chat_id, thread_id)
                for user_id, chat_id, thread_id, _delivery in deliveries
            },
            {
                ("usr-90", "chat-user-a", "topic-user-a"),
                ("usr-91", "chat-user-b", "topic-user-b"),
            },
        )
        self.assertEqual(
            {
                user_id: delivery.events[-1]
                for user_id, _chat_id, _thread_id, delivery in deliveries
            },
            {"usr-90": ("complete", "答案 A"), "usr-91": ("complete", "答案 B")},
        )

    def test_context_too_long_suggests_new_without_replacing_agent_session(self) -> None:
        """V-会话-11：上下文超限提示 /new，原 agent_session_id 保持不变。"""

        assert DSN is not None
        pipeline = EventPipeline(
            store=PostgresGatewayStore(DSN),
            reactions=FakeReactions(CallLog()),
            replies=FakeReplies(CallLog()),
            audit=FakeAudit(CallLog()),
        )
        outcome = pipeline.handle_message(
            InboundMessage(
                "evt-context-too-long",
                "im.message.receive_v1",
                "ou-90",
                "chat-context",
                "topic-context",
                "msg-context",
                "继续追问",
                "trace-context",
            )
        )
        assert outcome.task_id is not None
        conversation_id = self._scalar(
            "SELECT conversation_id FROM task WHERE id=%s", (outcome.task_id,)
        )
        self.execute(
            "UPDATE conversation SET agent_session_id='session-original' WHERE id=%s",
            (conversation_id,),
        )
        delivery = RecordingDelivery()

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {"closed": False, "final_text": "", "session_id": None},
                    "failure": {"code": "context_too_long"},
                }

        service = WorkerService(
            config=worker_config(worker_id="worker-context"),
            queue=self.queue,
            executor_factory=lambda config, marker: Executor(),
            delivery_factory=lambda context, marker: delivery,
        )
        asyncio.run(service.process_once())

        self.assertEqual(self._scalar("SELECT status FROM task"), "failed")
        self.assertEqual(delivery.events[-1], ("fail", "worker.context_too_long"))
        self.assertIn("/new", delivery.failed_contents[-1].text)
        self.assertEqual(
            self._scalar(
                "SELECT agent_session_id FROM conversation WHERE id=%s", (conversation_id,)
            ),
            "session-original",
        )
        self.assertIsNone(self._scalar("SELECT running_task_id FROM conversation"))

    def test_uncertain_side_effect_recovery_has_one_outbound_failure(self) -> None:
        """V-会话-13：恢复收口按出站调用次数防止重复交付，不只看库行数。"""

        self._insert_old_task(
            task_id="tsk-side-once",
            conversation_id="cnv-side-once",
            status="running",
            side_effect_state="possible",
            attempts=1,
        )
        deliveries: list[RecordingDelivery] = []

        def delivery_factory(context, marker):
            delivery = RecordingDelivery()
            deliveries.append(delivery)
            return delivery

        service = WorkerService(
            config=worker_config(worker_id="worker-recovery"),
            queue=self.queue,
            delivery_factory=delivery_factory,
        )
        asyncio.run(service.process_once())
        asyncio.run(service.process_once())

        self.assertEqual(self._scalar("SELECT status FROM task WHERE id='tsk-side-once'"), "failed")
        self.assertEqual(len(deliveries), 1, "恢复后只能创建一次用户可见投递")
        self.assertEqual(
            [event for event in deliveries[0].events if event[0] == "fail"],
            [("fail", "worker.side_effect_uncertain")],
            "V-会话-13 应断言真实出站调用，不以 task 行数代替",
        )

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
