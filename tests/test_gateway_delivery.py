"""Issue #152：Gateway 投递消费循环的真库断言。

覆盖：`V-卡片-01`（sequence 严格递增）、`V-卡片-02`（限流）、`V-卡片-03`（首次失败后
永久走文本通道、同话题只发一次文本终态）、`V-投递-03`（platform_received 的真实获得
路径与 uncertain 不自动重发）、状态合同第 7 条（重启从最后确认 sequence 恢复，不产生
第二张有效卡片/第二条文本终态）。

真实飞书 CardKit/发送接口不在本文件断言范围（L4a，留 Bot-Test/Stage）；本文件用
``core.execution.card_stream`` 的 ``CardTransport``/``TextTransport`` 假实现验证
消费循环本身的顺序、幂等与崩溃恢复语义，用真实 PostgreSQL 验证持久化的部分。
"""

from __future__ import annotations

import json
import os
import unittest
from typing import Any

from postgres_schema import ensure_production_schema, reset_production_rows

from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_conversation import PostgresTaskQueue
from lingxi.apps.gateway.delivery import DeliveryConsumer
from lingxi.core.execution.card_stream import CardCreated, DeliveryRejected

SKIP_REASON = "跳过：未设置 LINGXI_POSTGRES_DSN，Gateway 投递消费的数据库约束类断言未验证"


class RecordingCards:
    """记录调用；``fail_at`` 控制第几次调用（1-based）开始抛出同步异常，
    ``fail_error`` 控制抛出的异常类型（默认 ``DeliveryRejected``，模拟服务端明确
    拒绝；传入其它任何异常类型——``TimeoutError`` 等 ``OSError`` 子类、
    ``json.JSONDecodeError``、或任何未预期的异常——都模拟独立审核 R-1 的"结果
    不明"场景：白名单反转后，只有 ``DeliveryRejected`` 才是"明确失败"，除它以外
    的一切都不确定服务端是否已经处理）。
    """

    def __init__(
        self,
        *,
        fail_at: int | None = None,
        fail_error: type[BaseException] = DeliveryRejected,
    ) -> None:
        self._fail_at = fail_at
        self._fail_error = fail_error
        self._calls = 0
        self.create_calls: list[dict] = []
        self.update_calls: list[dict] = []
        self.close_calls: list[dict] = []

    def _maybe_fail(self) -> None:
        self._calls += 1
        if self._fail_at is not None and self._calls >= self._fail_at:
            if self._fail_error is json.JSONDecodeError:
                # JSONDecodeError 的构造签名是 (msg, doc, pos)，不是单个消息字符串——
                # 模拟 lark_oapi 内部解析响应体失败时真实抛出的形状。
                raise json.JSONDecodeError("模拟响应体解析失败", "", 0)
            raise self._fail_error("card call failed")

    def create(self, **kwargs: object) -> CardCreated:
        self._maybe_fail()
        self.create_calls.append(kwargs)
        return CardCreated(card_id="card-1", message_id="msg-card-1")

    def update(self, **kwargs: object) -> None:
        self._maybe_fail()
        self.update_calls.append(kwargs)

    def close(self, **kwargs: object) -> None:
        self._maybe_fail()
        self.close_calls.append(kwargs)


class RecordingText:
    """``fail_error`` 默认 ``DeliveryRejected``（明确失败）；传入其它异常类型模拟
    独立审核 R-1 的"结果不明"场景，见 ``RecordingCards`` 的类文档。
    """

    def __init__(
        self,
        *,
        fail: bool = False,
        message_id: str = "msg-text-1",
        fail_error: type[BaseException] = DeliveryRejected,
    ) -> None:
        self.fail = fail
        self.message_id = message_id
        self._fail_error = fail_error
        self.calls: list[dict] = []

    def send_text(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        if self.fail:
            raise self._fail_error("text send failed")
        return self.message_id


class RaisingCardsMidFinish:
    """``create`` 正常成功；终态 ``update``（``finish()`` 内部的第一次调用）崩溃。

    模拟"卡片其实已经建好、终态更新这一步的外部调用结果对进程来说永远不可知"——
    与 ``RaisingCardsMidCreate`` 同一手法，用 ``BaseException`` 表示真实进程崩溃，
    而不是 ``CardStream`` 自己会捕获的同步失败。
    """

    def __init__(self) -> None:
        self.create_calls: list[dict] = []

    def create(self, **kwargs: object) -> CardCreated:
        self.create_calls.append(kwargs)
        return CardCreated(card_id="card-1", message_id="msg-card-1")

    def update(self, **kwargs: object) -> None:
        raise _SimulatedCrash("终态更新的外部调用结果对进程来说永远不可知")

    def close(self, **kwargs: object) -> None:  # pragma: no cover - 不会走到
        raise AssertionError("崩溃恢复场景不应该继续调用 close")


class RaisingCardsMidCreate:
    """模拟"预留位已提交、外部调用本身在进行中崩溃"：``create`` 从不返回。"""

    def create(self, **kwargs: object) -> CardCreated:
        raise _SimulatedCrash("外部调用尚未确定结果时进程崩溃")

    def update(self, **kwargs: object) -> None:  # pragma: no cover - 不会走到
        raise AssertionError("崩溃恢复场景不应该继续调用 update")

    def close(self, **kwargs: object) -> None:  # pragma: no cover - 不会走到
        raise AssertionError("崩溃恢复场景不应该继续调用 close")


class _SimulatedCrash(BaseException):
    """刻意继承 ``BaseException`` 而不是 ``Exception``：``CardStream.start()`` 只
    捕获 ``Exception``，用它模拟"这次外部调用的结果对进程来说永远不可知"（真实的
    进程崩溃不会被自己的 ``except Exception`` 捕获），而不是"同步捕获到的明确失败"。
    测试断言的正是消费循环在这条边界上不把两者混为一谈。
    """


class _RecordDeliveryProgressFailsOnce:
    """代理真实 ``PostgresTaskQueue``；下一次 ``record_delivery_progress`` 调用
    抛出一个普通 ``RuntimeError``（模拟数据库连接瞬时重置），此后恢复正常。

    独立审核 P1-1 复现用的正是这种"不需要进程崩溃"的普通瞬时错误——真实进程
    崩溃（``_SimulatedCrash``）已经由既有用例覆盖，这里补的是"终态外部调用
    已经全部成功、只是随后的进度落库这一步失败"这半个此前完全没有测试覆盖的
    窗口。
    """

    def __init__(self, queue: Any) -> None:
        self._queue = queue
        self._should_fail = True

    def __getattr__(self, name: str) -> Any:
        return getattr(self._queue, name)

    def record_delivery_progress(self, **kwargs: object) -> None:
        if self._should_fail:
            self._should_fail = False
            raise RuntimeError("simulated transient database error")
        self._queue.record_delivery_progress(**kwargs)


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN"), SKIP_REASON)
class DeliveryConsumerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        cls._psycopg = psycopg
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        reset_production_rows(self._dsn)
        self.queue = PostgresTaskQueue(self._dsn)
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

    def query(self, sql: str, parameters: tuple = ()) -> list[tuple]:
        with self._connection.cursor() as cursor:
            cursor.execute(sql, parameters)
            return cursor.fetchall()

    def scalar(self, sql: str, parameters: tuple = ()):
        rows = self.query(sql, parameters)
        return rows[0][0] if rows else None

    def seed_running_task(
        self, *, task_id: str, conversation_id: str, reply_to_message_id: str = "reply-1"
    ) -> None:
        self.execute(
            """INSERT INTO conversation
               (id,user_id,feishu_chat_id,feishu_thread_id,running_task_id)
               VALUES (%s,'usr-1',%s,%s,%s)""",
            (conversation_id, f"chat-{conversation_id}", f"topic-{conversation_id}", task_id),
        )
        self.execute(
            """INSERT INTO task
               (id,conversation_id,user_id,inbound_event_id,prompt,status,
                target_worker_version,worker_id,heartbeat_at,attempts,
                reply_to_message_id,content_expires_at)
               VALUES (%s,%s,'usr-1',%s,'问题','running','stable','worker-1',now(),1,%s,now())""",
            (task_id, conversation_id, f"event-{task_id}", reply_to_message_id),
        )

    def start_task(self, task_id: str) -> None:
        self.queue.append_delivery_event(
            task_id=task_id, worker_id="worker-1", event_type="started",
            idempotency_key=f"{task_id}:a1:started",
        )

    def finish_task(self, task_id: str, *, content: str = "已送达的答案") -> None:
        self.queue.write_terminal_event(
            task_id=task_id, worker_id="worker-1", terminal_kind="success",
            error_kind=None, content=content,
        )


class HappyPathCardDeliveryTests(DeliveryConsumerTestCase):
    """一条开始事件建唯一卡片；流式更新与关闭严格递增；成功送达确认（`V-卡片-01`）。"""

    def test_started_progress_and_terminal_produce_one_card_and_confirm_delivery(self) -> None:
        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.start_task("tsk-1")

        # 受控时钟：建卡在 t=0 消费掉话题的首个限流名额；progress 必须等窗口过后
        # （t=0.6）才会被放行，否则会被 `V-卡片-02` 的 500ms 节流吞掉，这里要验证
        # 的正是"放行的更新与随后的终态更新共用整卡级严格递增序号"，节流吞掉的帧
        # 不构成矛盾但会让这条断言测不到想测的东西。
        clock = [0.0]
        cards = RecordingCards()
        texts = RecordingText()
        consumer = DeliveryConsumer(
            queue=self.queue, cards=cards, texts=texts, monotonic=lambda: clock[0]
        )
        consumer.run_once()  # 只处理 started：建卡。

        clock[0] = 0.6
        self.queue.append_delivery_event(
            task_id="tsk-1", worker_id="worker-1", event_type="progress",
            idempotency_key="tsk-1:a1:progress:1", elapsed_seconds=3,
        )
        self.finish_task("tsk-1")
        processed = consumer.run_once()

        self.assertEqual(processed, 1)
        self.assertEqual(len(cards.create_calls), 1, "只建一次卡片")
        self.assertEqual(
            [call["sequence"] for call in cards.update_calls], [1, 2],
            "进度更新 + 终态更新共用整卡级严格递增序号",
        )
        self.assertEqual([call["sequence"] for call in cards.close_calls], [3])
        self.assertEqual(texts.calls, [], "卡片路径全程未降级，不应该发文本兜底")

        row = self.query(
            "SELECT status, delivery_message_id, card_id, card_seq FROM task WHERE id='tsk-1'"
        )[0]
        self.assertEqual(row[0], "succeeded")
        self.assertEqual(row[1], "msg-card-1")
        self.assertEqual(row[2], "card-1")
        self.assertEqual(row[3], 3)
        received = self.scalar(
            "SELECT platform_received_at IS NOT NULL FROM task_delivery_event "
            "WHERE task_id='tsk-1' AND event_type='terminal'"
        )
        self.assertTrue(received)

    def test_progress_content_decodes_to_the_semantic_status_text(self) -> None:
        """P2-2（Issue #328 opus 审查）：`_handle_progress` 真的把
        `event.content` 交给 `decode_progress_action` 解码、再传给
        `CardStream.update`——不是只把游标推进了、内容字段被忽略（杀 M15：把
        `decode_progress_action(event.content)` 换成恒 `(processing, None)`
        或者直接不传 `content`，本用例会变红，因为渲染出的状态文案会退回默认
        「正在处理」而不是这里断言的"第 2 次查询"文案）。"""

        from lingxi.core.execution.card_stream import (
            PROGRESS_ACTION_QUERYING,
            encode_progress_action,
        )

        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.start_task("tsk-1")

        clock = [0.0]
        cards = RecordingCards()
        texts = RecordingText()
        consumer = DeliveryConsumer(
            queue=self.queue, cards=cards, texts=texts, monotonic=lambda: clock[0]
        )
        consumer.run_once()  # 只处理 started：建卡。

        clock[0] = 0.6  # 越过 `V-卡片-02` 的 500ms 单话题节流窗口
        self.queue.append_delivery_event(
            task_id="tsk-1", worker_id="worker-1", event_type="progress",
            idempotency_key="tsk-1:a1:progress:1", elapsed_seconds=5,
            content=encode_progress_action(PROGRESS_ACTION_QUERYING, query_count=2),
        )
        consumer.run_once()

        self.assertEqual(len(cards.update_calls), 1, "确实推进到了 progress 更新这一步")
        rendered_body = cards.update_calls[0]["card"].body
        self.assertIn("正在第 2 次查询指标数据", rendered_body)
        self.assertNotIn("正在处理", rendered_body, "不应该退回默认 processing 文案")

    def test_a_second_round_with_no_new_events_does_not_repeat_delivery(self) -> None:
        """重复轮询（没有新事件）不应该产生第二次外部调用（状态合同第 7 条）。"""

        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.start_task("tsk-1")
        self.finish_task("tsk-1")
        cards = RecordingCards()
        texts = RecordingText()
        consumer = DeliveryConsumer(queue=self.queue, cards=cards, texts=texts)
        consumer.run_once()
        first_round_calls = len(cards.create_calls) + len(cards.update_calls) + len(cards.close_calls)

        consumer.run_once()
        second_round_calls = (
            len(cards.create_calls) + len(cards.update_calls) + len(cards.close_calls)
        )
        self.assertEqual(second_round_calls, first_round_calls, "已确认送达的任务不再被消费")


class CardFailureFallsBackToTextTests(DeliveryConsumerTestCase):
    """`V-卡片-03`：卡片链路首次失败后停止后续卡片更新，只发一次文本终态。"""

    def test_update_failure_falls_back_and_confirms_as_text(self) -> None:
        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.start_task("tsk-1")
        self.finish_task("tsk-1", content="已产生的答案")

        # 第 1 次调用（create）成功，第 2 次（finish 的 update）失败。
        cards = RecordingCards(fail_at=2)
        texts = RecordingText()
        consumer = DeliveryConsumer(queue=self.queue, cards=cards, texts=texts)
        consumer.run_once()

        self.assertEqual(len(cards.create_calls), 1)
        self.assertEqual(len(cards.close_calls), 0, "更新失败后不再尝试关闭")
        self.assertEqual(len(texts.calls), 1, "同话题只发一次文本终态")
        self.assertIn("已产生的答案", texts.calls[0]["text"])

        row = self.query(
            "SELECT status, fallback_text, delivery_message_id FROM task WHERE id='tsk-1'"
        )[0]
        self.assertEqual(row[0], "succeeded")
        self.assertTrue(row[1])
        self.assertEqual(row[2], "msg-text-1")

    def test_text_fallback_failure_keeps_task_pending_for_retry(self) -> None:
        """文本兜底同步捕获到明确失败：清预留位、不确认送达，下一轮可以重试。

        独立审核 P2-4 修复后，明确失败带有退避——这里用受控时钟把时间拨过退避
        窗口，验证的仍然是"下一轮重试"这件事本身，不测退避的具体时长。
        """

        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.start_task("tsk-1")
        self.finish_task("tsk-1")

        clock = [0.0]
        cards = RecordingCards(fail_at=1)  # create 本身就失败，直接走文本通道
        texts = RecordingText(fail=True)
        consumer = DeliveryConsumer(
            queue=self.queue, cards=cards, texts=texts, monotonic=lambda: clock[0]
        )
        consumer.run_once()

        row = self.query(
            "SELECT status, dispatch_reserved_kind, delivery_message_id FROM task WHERE id='tsk-1'"
        )[0]
        self.assertEqual(row[0], "awaiting_delivery", "明确失败不确认送达，任务保持待投递")
        self.assertIsNone(row[1], "明确失败必须清空预留位，允许下一轮重试")
        self.assertIsNone(row[2])

        # 退避窗口内立即重试一次：不应该产生新的外发尝试（P2-4：无退避会打平台限流）。
        consumer.run_once()
        self.assertEqual(len(texts.calls), 1, "退避窗口内不应该再次尝试外发")

        # 时钟拨过退避窗口 + 文本发送恢复正常：下一轮应当成功重试并确认送达。
        clock[0] += DeliveryConsumer.DEFAULT_FALLBACK_BACKOFF_CAP_SECONDS
        texts.fail = False
        consumer.run_once()
        row = self.query("SELECT status FROM task WHERE id='tsk-1'")[0]
        self.assertEqual(row[0], "succeeded")
        self.assertEqual(len(texts.calls), 2, "第一次失败 + 退避过后的第二次重试各一次")


class CardFailureInjectionAcceptanceFixtureTests(DeliveryConsumerTestCase):
    """S-A-07 受控验收缺口专用注入开关（Issue #152 验收缺口、#154 评论
    5306860510、#162 E-022）：``apps.gateway._RejectingCards`` 命中被选中的那一步
    时确定性抛出 ``DeliveryRejected``，走的正是 ``CardFailureFallsBackToTextTests``
    已经验证过的既有降级路径——这里额外验证的是注入开关本身"命中步骤即拒绝、
    未命中步骤直通真实 transport"这条装配契约，而不是重新验证降级路径本身。
    """

    def test_create_injection_falls_back_to_a_single_text_terminal(self) -> None:
        from lingxi.apps.gateway import _RejectingCards

        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.start_task("tsk-1")
        self.finish_task("tsk-1", content="已产生的答案")

        real_cards = RecordingCards()
        cards = _RejectingCards(real_cards, inject="create")
        texts = RecordingText()
        consumer = DeliveryConsumer(queue=self.queue, cards=cards, texts=texts)
        consumer.run_once()

        self.assertEqual(
            real_cards.create_calls, [], "命中 create 时必须直接拒绝，不透传给真实 transport"
        )
        self.assertEqual(len(texts.calls), 1, "同话题只发一次文本终态")
        self.assertIn("已产生的答案", texts.calls[0]["text"])

        row = self.query(
            "SELECT status, fallback_text, delivery_message_id FROM task WHERE id='tsk-1'"
        )[0]
        self.assertEqual(row[0], "succeeded")
        self.assertTrue(row[1])
        self.assertEqual(row[2], "msg-text-1")

    def test_only_the_configured_step_is_rejected(self) -> None:
        """`update` 命中时 create 仍直通真实 transport——只有被选中的那一步拒绝，
        这是 ``_RejectingCards`` 文档写明的设计取舍，必须能被证伪。
        """

        from lingxi.apps.gateway import _RejectingCards

        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.start_task("tsk-1")
        self.finish_task("tsk-1", content="已产生的答案")

        real_cards = RecordingCards()
        cards = _RejectingCards(real_cards, inject="update")
        texts = RecordingText()
        consumer = DeliveryConsumer(queue=self.queue, cards=cards, texts=texts)
        consumer.run_once()

        self.assertEqual(len(real_cards.create_calls), 1, "create 未被选中，必须直通真实 transport")
        self.assertEqual(len(real_cards.close_calls), 0, "终态更新命中注入后不再尝试关闭")
        self.assertEqual(len(texts.calls), 1, "终态更新命中注入后降级为一次文本终态")


class CrashRecoveryDoesNotDuplicateDeliveryTests(DeliveryConsumerTestCase):
    """重复投递防线的核心验证：外发前预留位已提交、外部调用结果不明时崩溃重启，
    不得自动重发（Issue #151 审核 P3-6、issue 状态合同第 6 条）。
    """

    def test_a_crash_during_card_create_is_not_retried_automatically(self) -> None:
        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.start_task("tsk-1")

        crashing_cards = RaisingCardsMidCreate()
        texts = RecordingText()
        consumer = DeliveryConsumer(queue=self.queue, cards=crashing_cards, texts=texts)
        with self.assertRaises(_SimulatedCrash):
            consumer.run_once()

        # 预留位已经在外部调用之前独立提交，"进程崩溃"不会把它清空。
        row = self.query(
            "SELECT dispatch_reserved_kind, card_id, delivery_consumed_sequence "
            "FROM task WHERE id='tsk-1'"
        )[0]
        self.assertEqual(row[0], "card_create")
        self.assertIsNone(row[1])
        self.assertEqual(row[2], 0, "游标没有推进——这个 started 事件下一轮还会被看到")

        # "重启"后的下一轮消费者：预留位卡住的任务必须被排除在正常消费之外，
        # 不能因为看到同一个 started 事件又调用一次 create()。
        safe_cards = RecordingCards()
        recovered_consumer = DeliveryConsumer(queue=self.queue, cards=safe_cards, texts=texts)
        processed = recovered_consumer.run_once()
        self.assertEqual(processed, 0, "uncertain 任务不进入正常候选列表")
        self.assertEqual(len(safe_cards.create_calls), 0, "不得自动重发造成重复结果")

        uncertain = self.queue.list_uncertain_delivery_tasks()
        self.assertEqual(len(uncertain), 1)
        self.assertEqual(uncertain[0].task_id, "tsk-1")
        self.assertEqual(uncertain[0].reserved_kind, "card_create")

    def test_a_crash_during_terminal_finish_does_not_fall_back_to_a_duplicate_text(self) -> None:
        """本 Story 自查发现的边界：卡片已经建成、终态更新+关闭这一步崩溃后，下一轮
        绝不能把"重放会被 CardKit 拒绝"误判成"卡片链路失败"从而改发一条文本终态
        ——那样会在卡片其实已经送达之后又多发一次结果，是跨通道的重复投递。
        """

        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.start_task("tsk-1")
        self.finish_task("tsk-1")

        crashing_cards = RaisingCardsMidFinish()
        texts = RecordingText()
        consumer = DeliveryConsumer(queue=self.queue, cards=crashing_cards, texts=texts)
        with self.assertRaises(_SimulatedCrash):
            consumer.run_once()

        row = self.query(
            "SELECT dispatch_reserved_kind, card_id, fallback_text, status "
            "FROM task WHERE id='tsk-1'"
        )[0]
        self.assertEqual(row[0], "card_finish", "预留位必须在外部调用之前独立提交")
        self.assertEqual(row[1], "card-1", "建卡本身在崩溃之前已经成功并持久化")
        self.assertFalse(row[2], "还没有明确失败，不能标记为已降级")
        self.assertEqual(row[3], "awaiting_delivery")

        # "重启"后的下一轮：uncertain 任务必须被排除在正常消费之外，既不能重新调用
        # update/close（会撞上 CardKit 的 300317），也绝不能改发一条文本终态。
        safe_cards = RecordingCards()
        recovered = DeliveryConsumer(queue=self.queue, cards=safe_cards, texts=texts)
        processed = recovered.run_once()
        self.assertEqual(processed, 0)
        self.assertEqual(len(safe_cards.update_calls), 0)
        self.assertEqual(len(safe_cards.close_calls), 0)
        self.assertEqual(texts.calls, [], "不得因为终态更新崩溃就改发文本终态")

        uncertain = self.queue.list_uncertain_delivery_tasks()
        self.assertEqual([task.reserved_kind for task in uncertain], ["card_finish"])

    def test_a_transient_progress_persist_failure_after_terminal_finish_succeeds_does_not_duplicate_delivery(
        self,
    ) -> None:
        """独立审核 P1-1（红线）：终态卡片更新+关闭全部**成功**之后（用户已经在卡片
        里看到完整答案），紧接着的进度落库遇到一次普通瞬时错误（不需要进程崩溃、
        不需要 ``BaseException``）——绝不能让下一轮把这个任务当正常候选、用落后的
        ``card_seq`` 重放已经用掉的序号，被 CardKit 拒绝后误判成"卡片链路整体
        失败"、又发一条重复的文本终态。正确行为：预留位继续持有，任务落入
        ``uncertain``，不自动重发。
        """

        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.start_task("tsk-1")

        cards = RecordingCards()
        texts = RecordingText()
        consumer = DeliveryConsumer(queue=self.queue, cards=cards, texts=texts)
        consumer.run_once()  # 建卡（started 事件），正常成功。

        self.finish_task("tsk-1", content="已产生的答案")
        failing_queue = _RecordDeliveryProgressFailsOnce(self.queue)
        failing_consumer = DeliveryConsumer(queue=failing_queue, cards=cards, texts=texts)
        # 复现评论原文的关键点："这条路径不需要进程崩溃"——`run_once()` 按任务隔离
        # 异常（`except Exception`），这次瞬时错误在生产里就是被这样吞掉、正常
        # 跑完一整轮，而不是让进程崩溃退出。
        processed_first_round = failing_consumer.run_once()
        self.assertEqual(processed_first_round, 1)

        # 终态更新与关闭这两次外部调用已经真实发出并成功——这正是本次审核复现的
        # 场景：外部调用已经成功，只是随后的进度落库失败。
        self.assertEqual(len(cards.update_calls), 1, "终态更新已经真实发出")
        self.assertEqual(len(cards.close_calls), 1, "关闭也已经真实发出")
        self.assertEqual(texts.calls, [], "此时还不应该有任何文本兜底")

        row = self.query(
            "SELECT dispatch_reserved_kind, card_seq, fallback_text, status "
            "FROM task WHERE id='tsk-1'"
        )[0]
        self.assertEqual(
            row[0], "card_finish", "预留位必须继续持有，直到进度真正落库——这是本次修复的核心"
        )
        self.assertEqual(row[1], 0, "外部调用已经成功但进度落库失败，card_seq 不应该被写进去")
        self.assertFalse(row[2])
        self.assertEqual(row[3], "awaiting_delivery")

        uncertain = self.queue.list_uncertain_delivery_tasks()
        self.assertEqual(
            [task.reserved_kind for task in uncertain],
            ["card_finish"],
            "必须被路由为 uncertain，而不是可以被下一轮自动重放的正常候选",
        )

        # "重启"后的下一轮：uncertain 任务必须被排除在正常消费之外——既不能重放
        # 已经成功的终态更新/关闭，更不能因为重放被拒绝就改发一条文本终态。
        recovered = DeliveryConsumer(queue=self.queue, cards=cards, texts=texts)
        processed = recovered.run_once()
        self.assertEqual(processed, 0, "uncertain 任务不进入正常候选列表")
        self.assertEqual(len(cards.update_calls), 1, "不得重放已经成功的终态更新")
        self.assertEqual(len(cards.close_calls), 1, "不得重放已经成功的关闭")
        self.assertEqual(
            texts.calls,
            [],
            "卡片已经真实送达完整答案，绝不能因为一次瞬时错误又发一条重复的文本终态",
        )

    def test_clearing_the_reservation_by_hand_makes_the_task_processable_again(self) -> None:
        """人工核对**确认未送达**后清空预留位是两条恢复分支之一；清空之后消费恢复
        正常（独立审核 B-2：另一条"已送达"分支见
        ``delivery.py`` 模块文档，不重放外部调用，这里不重复覆盖）。
        """

        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.start_task("tsk-1")
        self.finish_task("tsk-1")
        self.queue.reserve_dispatch(task_id="tsk-1", kind="card_create")

        cards = RecordingCards()
        texts = RecordingText()
        consumer = DeliveryConsumer(queue=self.queue, cards=cards, texts=texts)
        consumer.run_once()
        self.assertEqual(len(cards.create_calls), 0, "预留位没清空前不得外发")

        self.queue.clear_dispatch_reservation(task_id="tsk-1")
        consumer.run_once()
        self.assertEqual(len(cards.create_calls), 1, "人工清空预留位后恢复正常消费")


class NetworkResultUnknownDoesNotDuplicateDeliveryTests(DeliveryConsumerTestCase):
    """独立审核 B-1（红线，P1）首次修复、独立审核 R-1（红线家族）反转为白名单：
    只有 ``DeliveryRejected``（服务端已经给出完整响应且业务错误码明确拒绝）才是
    "明确失败"；除它以外的一切——`requests` 的超时/连接类异常（真实 adapter 走
    lark-oapi，其 transport 是 ``requests.request(...)``，全部继承内置
    ``OSError``）、JSON 解析失败（``json.JSONDecodeError``）、响应结构缺失
    （``success()`` 为真但拿不到可回读标识）、任何其它未预期的异常——都不得被
    当成"明确失败"清预留位、降级或重试，必须转入 ``uncertain``、告警、预留位
    原样保留。用 ``TimeoutError``、``json.JSONDecodeError``、``LookupError``
    （模拟"success 真但缺可回读标识"）分别覆盖这几类成因，与 ``DeliveryRejected``
    模拟的"服务端明确拒绝"区分开（后者行为不变，见其余测试类）。
    """

    def test_a_timeout_during_card_create_does_not_retry_automatically(self) -> None:
        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.start_task("tsk-1")

        timeout_cards = RecordingCards(fail_at=1, fail_error=TimeoutError)
        texts = RecordingText()
        consumer = DeliveryConsumer(queue=self.queue, cards=timeout_cards, texts=texts)
        processed = consumer.run_once()
        self.assertEqual(processed, 1, "run_once 正常跑完这一轮，不因结果不明让整轮失败")

        row = self.query(
            "SELECT dispatch_reserved_kind, card_id, fallback_text, delivery_consumed_sequence "
            "FROM task WHERE id='tsk-1'"
        )[0]
        self.assertEqual(row[0], "card_create", "预留位必须原样保留，不得清空")
        self.assertIsNone(row[1], "没有拿到 card_id，不能假设建卡成功")
        self.assertFalse(row[2], "结果不明绝不能降级为文本兜底")
        self.assertEqual(row[3], 0, "游标不得推进，下一轮仍要重新评估这个 started 事件")
        self.assertEqual(texts.calls, [], "结果不明不得改走文本通道")

        uncertain = self.queue.list_uncertain_delivery_tasks()
        self.assertEqual([task.reserved_kind for task in uncertain], ["card_create"])

        # 下一轮：uncertain 任务被排除在正常消费之外，且沿用既有 uncertain 告警机制。
        alerts: list[tuple[str, str]] = []
        recovered = DeliveryConsumer(
            queue=self.queue,
            cards=RecordingCards(),
            texts=texts,
            on_alert=lambda kind, task_id: alerts.append((kind, task_id)),
        )
        processed_next = recovered.run_once()
        self.assertEqual(processed_next, 0, "uncertain 任务不进入正常候选")
        self.assertEqual(alerts, [("dispatch_uncertain:card_create", "tsk-1")])

    def test_a_timeout_during_terminal_update_does_not_fall_back_to_a_duplicate_text(self) -> None:
        """复现独立审核 B-1 场景 1：终态卡片更新读超时（服务端可能已经写入完成），
        绝不能整体降级为文本兜底——那样会在卡片其实已经送达之后又发一条重复文本。
        """

        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.start_task("tsk-1")
        self.finish_task("tsk-1", content="已产生的答案")

        # 第 1 次调用（create）成功，第 2 次（finish 的终态 update）超时。
        cards = RecordingCards(fail_at=2, fail_error=TimeoutError)
        texts = RecordingText()
        consumer = DeliveryConsumer(queue=self.queue, cards=cards, texts=texts)
        processed = consumer.run_once()
        self.assertEqual(processed, 1)

        self.assertEqual(len(cards.create_calls), 1)
        self.assertEqual(len(cards.close_calls), 0, "终态更新结果不明，不能继续调用关闭")
        self.assertEqual(
            texts.calls, [], "结果不明不得改走文本通道——这正是本次复现的跨通道重复投递"
        )

        row = self.query(
            "SELECT dispatch_reserved_kind, card_id, fallback_text, status "
            "FROM task WHERE id='tsk-1'"
        )[0]
        self.assertEqual(row[0], "card_finish", "预留位必须原样保留，不得清空")
        self.assertEqual(row[1], "card-1", "建卡本身已经成功并持久化")
        self.assertFalse(row[2], "结果不明不得标记为已降级")
        self.assertEqual(row[3], "awaiting_delivery")

        uncertain = self.queue.list_uncertain_delivery_tasks()
        self.assertEqual([task.reserved_kind for task in uncertain], ["card_finish"])

    def test_a_timeout_during_text_fallback_does_not_retry_automatically(self) -> None:
        """复现独立审核 B-1 场景 2：文本兜底发送读超时（服务端可能已经受理并投递），
        不得当成"明确失败"清预留位后按退避重发——必须转入 uncertain。
        """

        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.start_task("tsk-1")
        self.finish_task("tsk-1", content="已产生的答案")

        # create 本身明确失败（DeliveryRejected，行为不变），直接走文本通道；
        # 文本发送这一步改为超时。
        cards = RecordingCards(fail_at=1)
        texts = RecordingText(fail=True, fail_error=TimeoutError)
        consumer = DeliveryConsumer(queue=self.queue, cards=cards, texts=texts)
        processed = consumer.run_once()
        self.assertEqual(processed, 1)

        self.assertEqual(len(texts.calls), 1, "文本发送确实被尝试过一次")
        row = self.query("SELECT dispatch_reserved_kind, status FROM task WHERE id='tsk-1'")[0]
        self.assertEqual(row[0], "text_send", "结果不明必须保留预留位，不得清空重试")
        self.assertEqual(row[1], "awaiting_delivery")

        uncertain = self.queue.list_uncertain_delivery_tasks()
        self.assertEqual([task.reserved_kind for task in uncertain], ["text_send"])

        # 下一轮：uncertain 任务被排除在正常消费之外，不得自动重发。
        processed_next = consumer.run_once()
        self.assertEqual(processed_next, 0)
        self.assertEqual(len(texts.calls), 1, "结果不明不得自动重发")

    def test_an_explicit_rejection_is_unaffected_and_still_retries(self) -> None:
        """明确失败路径行为不变：`DeliveryRejected`（服务端明确拒绝，独立审核
        R-1 用它取代此前注入 ``RuntimeError`` 的既有测试写法）仍然立即清预留位、
        允许下一轮重试——与上面几条"结果不明"用例对照，证明白名单反转只改变了
        判别方向，没有改变既有的"明确失败"语义。
        """

        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.start_task("tsk-1")

        cards = RecordingCards(fail_at=1, fail_error=DeliveryRejected)
        texts = RecordingText()
        consumer = DeliveryConsumer(queue=self.queue, cards=cards, texts=texts)
        consumer.run_once()

        row = self.query("SELECT dispatch_reserved_kind, fallback_text FROM task WHERE id='tsk-1'")[
            0
        ]
        self.assertIsNone(row[0], "明确失败必须清空预留位，允许下一轮重试——与上面三条 TimeoutError 用例的行为相反")
        self.assertTrue(row[1], "明确失败整体降级为文本通道，这个既有语义没有被本次修复改变")

    def test_a_json_decode_error_during_card_create_does_not_retry_automatically(self) -> None:
        """独立审核 R-1 新增：`lark_oapi` 内部响应体解析失败时抛出的
        `json.JSONDecodeError` 不是黑名单能挡住的 `OSError`——旧的黑名单实现会把它
        当成"明确失败"清预留位、立即降级；白名单反转后，除 `DeliveryRejected`
        以外的一切异常默认归"结果不明"，这条用例正是证伪旧实现、证明新实现的
        对照组。
        """

        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.start_task("tsk-1")

        cards = RecordingCards(fail_at=1, fail_error=json.JSONDecodeError)
        texts = RecordingText()
        consumer = DeliveryConsumer(queue=self.queue, cards=cards, texts=texts)
        processed = consumer.run_once()
        self.assertEqual(processed, 1, "run_once 正常跑完这一轮，不因结果不明让整轮失败")

        row = self.query(
            "SELECT dispatch_reserved_kind, card_id, fallback_text, delivery_consumed_sequence "
            "FROM task WHERE id='tsk-1'"
        )[0]
        self.assertEqual(row[0], "card_create", "预留位必须原样保留，不得清空")
        self.assertIsNone(row[1], "没有拿到 card_id，不能假设建卡成功")
        self.assertFalse(row[2], "结果不明绝不能降级为文本兜底")
        self.assertEqual(row[3], 0, "游标不得推进，下一轮仍要重新评估这个 started 事件")
        self.assertEqual(texts.calls, [], "结果不明不得改走文本通道")

        uncertain = self.queue.list_uncertain_delivery_tasks()
        self.assertEqual([task.reserved_kind for task in uncertain], ["card_create"])

        # 下一轮：uncertain 任务被排除在正常消费之外，不得自动重发（外发计数不增）。
        recovered = DeliveryConsumer(queue=self.queue, cards=RecordingCards(), texts=texts)
        processed_next = recovered.run_once()
        self.assertEqual(processed_next, 0, "uncertain 任务不进入正常候选")

    def test_missing_readable_identifier_during_text_fallback_does_not_retry_automatically(
        self,
    ) -> None:
        """独立审核 R-1 新增：`response.success()` 为真但拿不到 `message_id`
        （真实 adapter 遇到这种响应形状时显式抛出 `LookupError`，见
        `adapters.feishu_delivery` 的模块说明）同样不是 `DeliveryRejected`——
        没有任何证据表明服务端拒绝了这次调用，反而它可能已经受理，只是响应缺失
        可回读标识，必须归"结果不明"，不得当成"明确失败"清预留位后按退避重发。
        """

        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.start_task("tsk-1")
        self.finish_task("tsk-1", content="已产生的答案")

        # create 本身明确失败，直接走文本通道；文本发送这一步响应缺可回读标识。
        cards = RecordingCards(fail_at=1)
        texts = RecordingText(fail=True, fail_error=LookupError)
        consumer = DeliveryConsumer(queue=self.queue, cards=cards, texts=texts)
        processed = consumer.run_once()
        self.assertEqual(processed, 1)

        self.assertEqual(len(texts.calls), 1, "文本发送确实被尝试过一次")
        row = self.query("SELECT dispatch_reserved_kind, status FROM task WHERE id='tsk-1'")[0]
        self.assertEqual(row[0], "text_send", "结果不明必须保留预留位，不得清空重试")
        self.assertEqual(row[1], "awaiting_delivery")

        uncertain = self.queue.list_uncertain_delivery_tasks()
        self.assertEqual([task.reserved_kind for task in uncertain], ["text_send"])

        # 下一轮：uncertain 任务被排除在正常消费之外，不得自动重发（外发计数不增）。
        processed_next = consumer.run_once()
        self.assertEqual(processed_next, 0)
        self.assertEqual(len(texts.calls), 1, "结果不明不得自动重发")


class UncertainTasksStopAlertingAfterExpiryTests(DeliveryConsumerTestCase):
    """独立审核 P2-4：`list_uncertain_delivery_tasks` 不能对一个已经被二十四小时
    到期路径收敛为 ``failed`` 的任务永远告警——那个任务已经不再需要任何人处理，
    `dispatch_reserved_kind` 字段之后也不会再被任何投递路径读取。
    """

    def test_expired_task_stops_being_reported_as_uncertain(self) -> None:
        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.start_task("tsk-1")
        # 直接插入一条已经过期的 terminal 行，跳过真实二十四小时等待（与
        # `DeliveryExpiredNoticeTests` 同一手法）。
        self.execute(
            """
            INSERT INTO task_delivery_event
                (id, task_id, sequence, event_type, terminal_kind, content,
                 worker_id, idempotency_key, created_at)
            VALUES ('tde-tsk-1-2','tsk-1',2,'terminal','success','已产生的答案',
                    'worker-1','tsk-1:terminal', now() - interval '25 hours')
            """
        )
        self.execute("UPDATE task SET status = 'awaiting_delivery' WHERE id = 'tsk-1'")
        # 模拟"崩溃在预留位提交与清空之间"：预留位卡住，任务落入 uncertain。
        self.assertTrue(self.queue.reserve_dispatch(task_id="tsk-1", kind="card_finish"))

        uncertain_before = self.queue.list_uncertain_delivery_tasks()
        self.assertEqual([task.task_id for task in uncertain_before], ["tsk-1"])

        expired = self.queue.expire_undelivered_terminals()
        self.assertEqual([task.task_id for task in expired], ["tsk-1"])

        uncertain_after = self.queue.list_uncertain_delivery_tasks()
        self.assertEqual(
            uncertain_after, [], "任务已经被到期路径收敛为 failed，不应该继续被当作 uncertain 告警"
        )

        row = self.query(
            "SELECT status, dispatch_reserved_kind FROM task WHERE id='tsk-1'"
        )[0]
        self.assertEqual(row[0], "failed", "到期路径的业务结论不受预留位状态影响")
        self.assertEqual(
            row[1],
            "card_finish",
            "预留位字段本身不需要被到期路径清空——任务收敛之后它不会再被任何查询读取",
        )


class RateLimitingTests(DeliveryConsumerTestCase):
    """`V-卡片-02`：单话题 500ms 内的中间更新帧被抑制，不产生外部调用。"""

    def test_topic_updates_are_throttled_within_the_same_round(self) -> None:
        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.start_task("tsk-1")
        for index in range(1, 4):
            self.queue.append_delivery_event(
                task_id="tsk-1", worker_id="worker-1", event_type="progress",
                idempotency_key=f"tsk-1:a1:progress:{index}", elapsed_seconds=index,
            )

        # 注入受控时钟：建卡消费掉话题的首个限流名额后，三次 progress 全部落在
        # 500ms 窗口内，只有窗口之外的最后一次应当被放行——用真实 wall clock 断言
        # 这一点会不稳定（取决于本轮实际跑了多久），因此不依赖真实时间流逝。
        clock = [0.0]
        cards = RecordingCards()
        texts = RecordingText()
        consumer = DeliveryConsumer(
            queue=self.queue, cards=cards, texts=texts, monotonic=lambda: clock[0]
        )
        consumer.run_once()

        self.assertEqual(len(cards.update_calls), 0, "建卡本身消费了首个限流名额，同一时刻的更新被抑制")
        cursor = self.scalar("SELECT delivery_consumed_sequence FROM task WHERE id='tsk-1'")
        self.assertEqual(cursor, 4, "游标必须推进到最后一个序号，即使更新被限流抑制")

        # 时钟前进超过 500ms 后的下一轮：限流解除，用最新状态发一次更新。
        clock[0] = 0.6
        self.queue.append_delivery_event(
            task_id="tsk-1", worker_id="worker-1", event_type="progress",
            idempotency_key="tsk-1:a1:progress:4", elapsed_seconds=9,
        )
        consumer.run_once()
        self.assertEqual(len(cards.update_calls), 1, "窗口之外的更新应当被放行")


class DeliveryExpiredNoticeTests(DeliveryConsumerTestCase):
    """`V-投递-06` 后半句：到期只在用户下一条主动消息上提示一次。"""

    def test_consume_notice_is_one_shot(self) -> None:
        from lingxi.adapters.postgres_conversation import PostgresGatewayStore

        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.start_task("tsk-1")
        # 直接插入一条已经过期的 terminal 行（跳过真实二十四小时等待）：触发器锁定
        # created_at 只在 INSERT 时可以自由指定，UPDATE 会被拒绝（0059 冻结的不变量），
        # 因此必须在插入时就回填，不能先正常写入再改时间。
        self.execute(
            """
            INSERT INTO task_delivery_event
                (id, task_id, sequence, event_type, terminal_kind, content,
                 worker_id, idempotency_key, created_at)
            VALUES ('tde-tsk-1-2','tsk-1',2,'terminal','success','已送达的答案',
                    'worker-1','tsk-1:terminal', now() - interval '25 hours')
            """
        )
        self.execute("UPDATE task SET status = 'awaiting_delivery' WHERE id = 'tsk-1'")
        expired = self.queue.expire_undelivered_terminals()
        self.assertEqual([task.task_id for task in expired], ["tsk-1"])

        store = PostgresGatewayStore(self._dsn)
        with store.transaction() as tx:
            first = tx.consume_delivery_expired_notice(conversation_id="cnv-1")
        with store.transaction() as tx:
            second = tx.consume_delivery_expired_notice(conversation_id="cnv-1")

        self.assertTrue(first, "有一条到期未提示的任务，第一次应当命中")
        self.assertFalse(second, "同一次到期只提示一次")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
