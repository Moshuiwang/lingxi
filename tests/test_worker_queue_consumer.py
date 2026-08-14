"""Issue #90 的队列消费与失败终态断言；Issue #151 起收口方式改为写投递事件 outbox。

真库组覆盖 V-队列-06/08/09、V-会话-07 的状态、隔离与轮询；纯逻辑组覆盖 V-会话-06
与 worker 收口写入的投递事件形状，避免把外部飞书/CardKit L4a 误写成已验证。

**Issue #151 起，``WorkerService`` 不再持有任何出站 transport**：收口只写
``task_delivery_event``（``started``/``progress``/``terminal``）并把任务转入
``awaiting_delivery``，不直接调用飞书或释放话题——话题占用与最终业务状态改由
:mod:`lingxi.adapters.postgres_conversation` 的 ``confirm_delivery`` /
``expire_undelivered_terminals`` 收口，见 ``tests/test_delivery_outbox.py`` 的
真库断言（V-投递-01…06/10）。``CardStream``/``CardTaskDelivery`` 曾经把 Worker
接到飞书 CardKit，与「Worker 只写数据库，不直接调用飞书」的架构边界冲突，已随
本次改动从 ``apps/worker`` 移除；``CardStream`` 本身留在
``core/execution/card_stream.py`` 作为协议无关的可复用组件，供 #152 的 Gateway
消费者注入真实 transport 时使用，下面的 ``CardStreamTests`` 继续直接覆盖它。
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
from lingxi.apps.worker.service import WorkerService
from lingxi.config.content import default_content_catalog
from lingxi.core.conversation import EventPipeline, InboundMessage
from lingxi.core.execution.card_stream import (
    CardCreated,
    CardRateLimiter,
    CardStream,
    DeliveryRejected,
)
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
    """``error`` 默认 ``DeliveryRejected``（明确失败，独立审核 R-1 白名单）；传入
    ``TimeoutError`` 等其它任何异常类型模拟"结果不明"，见
    ``core.execution.card_stream`` 模块说明。
    """

    def __init__(
        self, *, fail: str | None = None, error: type[BaseException] = DeliveryRejected
    ) -> None:
        self.fail = fail
        self._error = error
        self.calls: list[tuple[str, int | None]] = []
        self.bodies: list[str] = []

    def create(self, **kwargs: object) -> CardCreated:
        self.calls.append(("create", None))
        self.bodies.append(kwargs["card"].body)  # type: ignore[union-attr]
        if self.fail == "create":
            raise self._error("card create")
        return CardCreated(card_id="card-1", message_id="msg-card-1")

    def update(self, *, sequence: int, **kwargs: object) -> None:
        self.calls.append(("update", sequence))
        self.bodies.append(kwargs["card"].body)  # type: ignore[union-attr]
        if self.fail == "update":
            raise self._error("card update")

    def close(self, *, sequence: int, **kwargs: object) -> None:
        self.calls.append(("close", sequence))
        self.bodies.append(kwargs["card"].body)  # type: ignore[union-attr]
        if self.fail == "close":
            raise self._error("card close")


class RecordingText:
    def __init__(self, *, fail: bool = False, error: type[BaseException] = DeliveryRejected) -> None:
        self.texts: list[str] = []
        self.calls: list[dict[str, object]] = []
        self.fail = fail
        self._error = error

    def send_text(
        self,
        *,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
        text: str,
    ) -> str:
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
            raise self._error("text fallback")
        return "msg-fallback-1"


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

    def test_finish_counts_toward_the_shared_global_rate_budget(self) -> None:
        """独立审核 P2-2：``finish()`` 刻意不经过单话题 500ms 节流（终态帧是结果
        本身，被吞掉比不节流更糟），但全进程 50 次/秒的预算必须同样计入终态更新
        +关闭这两次调用，否则并发多话题同时终态时全局计数会失真。
        """

        limiter = CardRateLimiter()
        cards = RecordingCards()
        text = RecordingText()
        stream = CardStream(
            chat_id="chat-a",
            thread_id="topic-a",
            reply_to_message_id="msg-a",
            transport=cards,
            fallback=text,
            monotonic=lambda: 0.0,
            rate_limiter=limiter,
        )
        stream.start()  # 消费全局预算第 1 个名额
        for index in range(48):
            self.assertTrue(limiter.allow(topic=f"filler-{index}", now=0.0))
        # 至此全局预算已经用掉 49/50，只剩 1 个名额。

        stream.finish(result="结果", elapsed_seconds=1)  # 终态更新 + 关闭，各占一个名额

        self.assertFalse(
            limiter.allow(topic="topic-brand-new", now=0.0),
            "finish() 的两次调用必须计入全局 50 次/秒预算，否则这里会被误放行",
        )

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

    def test_close_failure_alone_does_not_fall_back_to_a_duplicate_text(self) -> None:
        """独立审核 P1-2：终态**更新**已经成功（用户已经能在卡片里看到完整答案），
        只有随后的**关闭**失败——不得整体降级为文本兜底，否则用户会在同一话题里
        看到同一条答案两遍。G-CARD 实测：未手动关闭的流式卡片距上次开启 10 分钟
        后由平台自动关闭，关闭失败本身不构成结果丢失。
        """

        cards = RecordingCards(fail="close")
        text = RecordingText()
        stream = CardStream(
            chat_id="chat-a",
            thread_id="topic-a",
            reply_to_message_id="msg-a",
            transport=cards,
            fallback=text,
        )
        stream.start()
        stream.finish(result="已产生的答案", elapsed_seconds=1)

        self.assertFalse(stream.fallback_needed, "只有关闭失败，不能整体降级为文本通道")
        self.assertEqual(
            [kind for kind, _ in cards.calls],
            ["create", "update", "close"],
            "终态更新必须真实发出；关闭也确实被尝试过（只是失败了）",
        )

        message_id = stream.send_fallback(default_content_catalog().text("worker.failed"))
        self.assertIsNone(message_id, "fallback_needed 为假时 send_fallback 不产生任何外部调用")
        self.assertEqual(text.calls, [], "答案已经在卡片里对用户可见，绝不能再发一条重复文本终态")

    def test_create_timeout_is_not_swallowed_into_a_fallback_downgrade(self) -> None:
        """独立审核 B-1/R-1：``TimeoutError``（真实 adapter 走 ``requests``，其网络
        异常全部是内置 ``OSError`` 的子类）不是"明确失败"——``start()`` 必须原样把
        它抛出去，不能像 ``DeliveryRejected`` 那样吞掉并置位 ``fallback_needed``
        （那会让调用方误以为已经拿到"应该改走文本通道"这个明确结论）。
        """

        cards = RecordingCards(fail="create", error=TimeoutError)
        text = RecordingText()
        stream = CardStream(
            chat_id="chat-a", thread_id="topic-a", reply_to_message_id="msg-a",
            transport=cards, fallback=text,
        )

        with self.assertRaises(TimeoutError):
            stream.start()

        self.assertIsNone(stream.card_id, "没有拿到 card_id，不能假设建卡成功")
        self.assertFalse(stream.fallback_needed, "结果不明绝不能置位 fallback_needed")

    def test_terminal_update_timeout_is_not_swallowed_into_a_fallback_downgrade(self) -> None:
        """独立审核 B-1 场景 1：终态更新超时不得被 ``finish()`` 吞掉后降级为文本
        兜底——必须原样抛出，且不再继续调用 ``close()``。
        """

        cards = RecordingCards(fail="update", error=TimeoutError)
        text = RecordingText()
        stream = CardStream(
            chat_id="chat-a", thread_id="topic-a", reply_to_message_id="msg-a",
            transport=cards, fallback=text,
        )
        stream.start()

        with self.assertRaises(TimeoutError):
            stream.finish(result="已产生的答案", elapsed_seconds=1)

        self.assertFalse(stream.fallback_needed, "结果不明绝不能置位 fallback_needed")
        self.assertEqual(
            [kind for kind, _ in cards.calls], ["create", "update"], "结果不明时不得继续调用 close"
        )

    def test_close_timeout_still_does_not_fall_back_like_any_other_close_failure(self) -> None:
        """``close()`` 步骤的异常分类不延伸到这里（见 ``card_stream.py`` 注释）：
        无论关闭失败是明确拒绝还是网络类异常，都不改变"更新已经成功、答案已对
        用户可见"这个结论，``TimeoutError`` 与 ``DeliveryRejected`` 在这一步行为
        一致。
        """

        cards = RecordingCards(fail="close", error=TimeoutError)
        text = RecordingText()
        stream = CardStream(
            chat_id="chat-a", thread_id="topic-a", reply_to_message_id="msg-a",
            transport=cards, fallback=text,
        )
        stream.start()
        stream.finish(result="已产生的答案", elapsed_seconds=1)  # 不应该抛出

        self.assertFalse(stream.fallback_needed, "关闭失败（无论何种异常）都不整体降级")
        self.assertEqual([kind for kind, _ in cards.calls], ["create", "update", "close"])

    def test_text_fallback_timeout_is_not_swallowed(self) -> None:
        """独立审核 B-1 场景 2：文本兜底发送超时必须原样抛出，调用方据此不清预留位、
        不进入重试退避。
        """

        cards = RecordingCards(fail="create")  # 明确失败，走文本通道
        text = RecordingText(fail=True, error=TimeoutError)
        stream = CardStream(
            chat_id="chat-a", thread_id="topic-a", reply_to_message_id="msg-a",
            transport=cards, fallback=text,
        )
        stream.start()
        self.assertTrue(stream.fallback_needed)

        with self.assertRaises(TimeoutError):
            stream.send_fallback(default_content_catalog().text("worker.failed"))

        self.assertIsNone(stream.message_id, "没有拿到 message_id，不能假设文本已经送达")
        self.assertEqual(len(text.calls), 1, "发送确实被尝试过一次")

    def test_resuming_with_an_existing_card_id_does_not_create_a_second_card(self) -> None:
        """Issue #152：Gateway 消费循环重启后用 ``initial_*`` 恢复，``start()``
        必须是安全的空操作，不产生第二次 ``create()`` 调用（状态合同第 7 条）。"""

        cards = RecordingCards()
        text = RecordingText()
        stream = CardStream(
            chat_id="chat-a",
            thread_id="topic-a",
            reply_to_message_id="msg-a",
            transport=cards,
            fallback=text,
            initial_card_id="card-resumed",
            initial_sequence=2,
            initial_message_id="msg-resumed",
        )

        stream.start()
        self.assertEqual(cards.calls, [], "resume 场景下 start() 必须是空操作")
        self.assertEqual(stream.card_id, "card-resumed")
        self.assertEqual(stream.message_id, "msg-resumed")

        stream.finish(result="结果", elapsed_seconds=1)
        self.assertEqual(
            [sequence for kind, sequence in cards.calls if kind in {"update", "close"}],
            [3, 4],
            "序号从持久化的 initial_sequence 之后继续，不从零重新计数",
        )

    def test_resuming_with_fallback_already_needed_skips_the_card_path_entirely(self) -> None:
        """已经降级为文本通道的任务重启后不得再尝试建卡或更新卡片。"""

        cards = RecordingCards()
        text = RecordingText()
        stream = CardStream(
            chat_id="chat-a",
            thread_id="topic-a",
            reply_to_message_id="msg-a",
            transport=cards,
            fallback=text,
            initial_fallback_needed=True,
        )

        stream.start()
        stream.update(elapsed_seconds=1)
        stream.finish(result="结果")
        self.assertEqual(cards.calls, [], "已降级的任务重启后不得再触碰卡片通道")

        message_id = stream.send_fallback(default_content_catalog().text("worker.failed"))
        self.assertEqual(message_id, "msg-fallback-1")
        self.assertEqual(stream.message_id, "msg-fallback-1")


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
        self.events: list[dict[str, object]] = []
        self.terminals: list[dict[str, object]] = []
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

    def append_delivery_event(self, **kwargs: object) -> None:
        self.events.append(kwargs)
        return None

    def write_terminal_event(self, **kwargs: object) -> None:
        self.terminals.append(kwargs)
        return None


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
    """Issue #151：``_process_task`` 只写 ``task_delivery_event``，不再调用任何出站
    transport；断言因此改看 ``queue.events``/``queue.terminals`` 记录了什么，而不是
    ``delivery`` 对象收到了什么调用。"""

    def test_success_writes_started_and_terminal_events(self) -> None:
        queue = FakeWorkerQueue()

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                self.kwargs = kwargs
                kwargs["on_stream_event"]({"kind": "assistant_message"})  # type: ignore[index]
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
        )
        asyncio.run(service.process_once())

        self.assertEqual(queue.events[0]["event_type"], "started")
        self.assertEqual(queue.events[1]["event_type"], "progress")
        terminal = queue.terminals[0]
        self.assertEqual(terminal["terminal_kind"], "success")
        self.assertIsNone(terminal["error_kind"])
        self.assertEqual(terminal["content"], "结果")
        self.assertEqual(terminal["agent_session_id"], "new-session")
        self.assertIsNotNone(executor.kwargs["resume_session_id"])
        self.assertEqual(
            executor.kwargs["external_texts"],
            (("metric.description", "指标目录中的已知描述"),),
        )

    def test_stop_and_timeout_write_distinct_terminal_kinds(self) -> None:
        for stopped, failure_code, expected_terminal_kind, expected_error in (
            (True, None, "stopped", "stopped"),
            (False, "turn_timeout", "timeout", "running_timeout"),
        ):
            with self.subTest(expected_terminal_kind=expected_terminal_kind):
                queue = FakeWorkerQueue(stopped=stopped)

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
                )
                asyncio.run(service.process_once())
                terminal = queue.terminals[0]
                self.assertEqual(terminal["terminal_kind"], expected_terminal_kind)
                self.assertEqual(terminal["error_kind"], expected_error)
                expected_key = "worker.stopped" if stopped else "worker.running_timeout"
                self.assertEqual(terminal["content"], default_content_catalog().text(expected_key).text)

    def test_withheld_output_writes_redacted_withheld_terminal_not_success(self) -> None:
        """#141/#149：整段正文因安全策略被拒发时，即使 closed=True 也不得写成
        ``terminal_kind='success'``——用户没有拿到结果，必须走独立、可查询的
        ``redacted_withheld`` 终态。改坏这条路由（例如去掉 withheld 判断）必须让
        本用例变红。"""

        queue = FakeWorkerQueue()

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {
                        "closed": True,
                        "final_text": "本次结果涉及需要保护的内容，已被安全策略拦截，未能提供结果。",
                        "session_id": "new-session",
                        "output_safety": {"blocked": True, "withheld": True, "reasons": ("forbidden_value",)},
                        "user_result": "redacted_withheld",
                    },
                    "failure": None,
                }

        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
        )
        asyncio.run(service.process_once())

        terminal = queue.terminals[0]
        self.assertEqual(terminal["terminal_kind"], "redacted_withheld")
        self.assertEqual(terminal["error_kind"], "redacted_withheld")
        self.assertNotEqual(terminal["terminal_kind"], "success")
        # withheld 的原始 final_text 不得进入投递事件：正文只能是目录里的固定安全
        # 文案，不是模型给出的（已经被判定不可展示的）片段。
        self.assertEqual(
            terminal["content"], default_content_catalog().text("worker.redacted_withheld").text
        )


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
            # 0.2 → 0.3：Issue #151 给 _housekeep() 每轮多加了一次真实数据库往返
            # （expire_undelivered_terminals），在这条真库用例的调度余量上留出对应
            # 空间，仍然远小于用户可感知的延迟（V-队列-06 只关心"不会无限期悬挂"）。
            poll_interval + 0.3,
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

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {"closed": True, "final_text": prompt, "session_id": None},
                    "failure": None,
                }

        service = WorkerService(
            config=worker_config(max_concurrency=2),
            queue=self.queue,
            executor_factory=lambda config, marker: Executor(),
        )
        asyncio.run(service.process_once())

        # Issue #151：Worker 不再持有出站 transport，投递意图只落在
        # ``task_delivery_event``。按用户回读各自的终态事件与话题定位，验证
        # 两个用户互不串用会话或投递定位（V-会话-07）。
        with connect(DSN) as connection:
            rows = connection.execute(
                """
                SELECT t.user_id, c.feishu_chat_id, c.feishu_thread_id, e.content, t.status
                  FROM task_delivery_event AS e
                  JOIN task AS t ON t.id = e.task_id
                  JOIN conversation AS c ON c.id = t.conversation_id
                 WHERE e.event_type = 'terminal'
                 ORDER BY t.user_id
                """
            ).fetchall()
        by_user = {row[0]: row for row in rows}
        self.assertEqual(set(by_user), {"usr-90", "usr-91"})
        self.assertEqual(
            (by_user["usr-90"][1], by_user["usr-90"][2]), ("chat-user-a", "topic-user-a")
        )
        self.assertEqual(
            (by_user["usr-91"][1], by_user["usr-91"][2]), ("chat-user-b", "topic-user-b")
        )
        self.assertEqual(by_user["usr-90"][3], "答案 A")
        self.assertEqual(by_user["usr-91"][3], "答案 B")
        self.assertEqual(by_user["usr-90"][4], "awaiting_delivery")
        self.assertEqual(by_user["usr-91"][4], "awaiting_delivery")

    def test_context_too_long_suggests_new_without_replacing_agent_session(self) -> None:
        """上下文超限提示 /new，原 agent_session_id 保持不变；Issue #151 起失败
        终态也进入 ``awaiting_delivery`` 并继续占用话题，不再立即释放——释放要等
        投递解析（confirm_delivery / expire_undelivered_terminals，见
        ``tests/test_delivery_outbox.py``）。"""

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
        )
        asyncio.run(service.process_once())

        self.assertEqual(self._scalar("SELECT status FROM task"), "awaiting_delivery")
        terminal = self._scalar(
            "SELECT content FROM task_delivery_event WHERE event_type='terminal'"
        )
        self.assertIn("/new", terminal)
        self.assertEqual(
            self._scalar("SELECT error_kind FROM task"), "context_too_long"
        )
        self.assertEqual(
            self._scalar(
                "SELECT agent_session_id FROM conversation WHERE id=%s", (conversation_id,)
            ),
            "session-original",
        )
        # 失败终态同样进入 awaiting_delivery，话题继续占用直到投递解析——本
        # Story 的状态合同第 2 条明确覆盖失败/停止/超时/withheld，不只是成功路径。
        self.assertIsNotNone(self._scalar("SELECT running_task_id FROM conversation"))

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
