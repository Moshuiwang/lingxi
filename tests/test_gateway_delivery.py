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
from datetime import timedelta
from typing import Any

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.adapters.postgres_conversation import PostgresTaskQueue
from lingxi.apps.gateway.delivery import DeliveryConsumer
from lingxi.config.content import default_content_catalog
from lingxi.core.delivery.ports import (
    DELIVERY_OPERATIONS,
    DeliveryOperation,
    DeliveryVerdict,
    classify_delivery_response,
)
from lingxi.core.execution.card_stream import (
    CardCreated,
    DeliveryRejectedError,
    DeliveryUncertainError,
)

#: 飞书「仍在发送」错误码：服务端已经收下这次外发并且还在处理。既不是完成也不是
#: 拒绝，判成任何一侧都会出事（判成功＝把没落地的消息记成已送达；判拒绝＝清预留位
#: 后改走另一条通道，原来那条随后真的送达，用户收到两份）。
IN_FLIGHT_CODE = 230049


def _in_flight_error(operation: DeliveryOperation, label: str) -> DeliveryUncertainError:
    """把一次「仍在发送」响应翻成消费侧看到的异常，**裁定全部交给真实规则表**。

    与 ``adapters/feishu_delivery._verdict`` 同一条判定路径：``retry_safe`` 取自
    :data:`DELIVERY_OPERATIONS` 里该操作登记的平台去重键，本文件自己不下任何裁定。
    这一点是被真实教训换来的——早先的假传输层直接硬写 ``retry_safe=True``，于是把
    规则表整个绕开：把 ``card_update`` 那一行的键改掉，端到端用例照样全绿，那条
    "安全重投"用例证明的只是它自己注入的信念。
    """

    outcome = classify_delivery_response(operation=operation, code=IN_FLIGHT_CODE)
    assert outcome.verdict is DeliveryVerdict.UNCERTAIN, "230049 必须裁定为结果不明"
    return DeliveryUncertainError(
        f"{label}结果不明（{outcome.reason}）",
        reason=outcome.reason,
        code=IN_FLIGHT_CODE,
        retry_safe=outcome.retry_safe,
    )


SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，Gateway 投递消费的数据库约束类断言未验证"
    if not os.environ.get("LINGXI_POSTGRES_DSN")
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，Gateway 投递消费的数据库约束类断言未验证"
)


class RecordingCards:
    """记录调用；``fail_at`` 控制第几次调用（1-based）开始抛出同步异常，
    ``fail_error`` 控制抛出的异常类型（默认 ``DeliveryRejectedError``，模拟服务端明确
    拒绝；传入其它任何异常类型——``TimeoutError`` 等 ``OSError`` 子类、
    ``json.JSONDecodeError``、或任何未预期的异常——都模拟独立审核 R-1 的"结果
    不明"场景：白名单反转后，只有 ``DeliveryRejectedError`` 才是"明确失败"，除它以外
    的一切都不确定服务端是否已经处理）。
    """

    def __init__(
        self,
        *,
        fail_at: int | None = None,
        fail_error: type[BaseException] = DeliveryRejectedError,
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
    """``fail_error`` 默认 ``DeliveryRejectedError``（明确失败）；传入其它异常类型模拟
    独立审核 R-1 的"结果不明"场景，见 ``RecordingCards`` 的类文档。
    """

    def __init__(
        self,
        *,
        fail: bool = False,
        message_id: str = "msg-text-1",
        fail_error: type[BaseException] = DeliveryRejectedError,
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


class InFlightTerminalCards:
    """``create`` 正常成功；终态 ``update`` 头 ``in_flight_updates`` 次拿到平台
    「仍在发送」（``230049``）。

    **本类只模拟平台回了哪个码**，能不能安全重投一律由真实的
    ``classify_delivery_response`` 按 ``DELIVERY_OPERATIONS`` 判（见
    :func:`_in_flight_error`）。
    """

    def __init__(self, *, in_flight_updates: int = 1) -> None:
        self._remaining = in_flight_updates
        self.create_calls: list[dict] = []
        self.update_calls: list[dict] = []
        self.close_calls: list[dict] = []

    def create(self, **kwargs: object) -> CardCreated:
        self.create_calls.append(kwargs)
        return CardCreated(card_id="card-1", message_id="msg-card-1")

    def update(self, **kwargs: object) -> None:
        if self._remaining > 0:
            self._remaining -= 1
            raise _in_flight_error(DeliveryOperation.CARD_UPDATE, "卡片流式更新")
        self.update_calls.append(kwargs)

    def close(self, **kwargs: object) -> None:
        self.close_calls.append(kwargs)


class InFlightText:
    """文本兜底每次都拿到「仍在发送」。同样只模拟平台回了哪个码，裁定交给真实规则表。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def send_text(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        raise _in_flight_error(DeliveryOperation.TEXT_SEND, "发送投递文本")


class _ConfirmDeliveryFailsOnce:
    """代理真实 ``PostgresTaskQueue``；下一次 ``confirm_delivery`` 调用抛出一个
    普通 ``RuntimeError``（模拟数据库连接瞬时重置），此后恢复正常。

    用来验证"终态已经真的送达、只是确认这一步没成功"的任务在下一轮**仍然会被
    重新确认**——收紧确认前提不得把这条既有的恢复路径一起关掉。
    """

    def __init__(self, queue: Any) -> None:
        self._queue = queue
        self._should_fail = True

    def __getattr__(self, name: str) -> Any:
        return getattr(self._queue, name)

    def confirm_delivery(self, **kwargs: object) -> bool:
        if self._should_fail:
            self._should_fail = False
            raise RuntimeError("simulated transient database error")
        return self._queue.confirm_delivery(**kwargs)


class _ConfirmDeliverySpy:
    """代理真实 ``PostgresTaskQueue``，只记录 ``confirm_delivery`` 的调用参数。

    用来**独立**钉住消费循环这一侧的判据：``confirm_delivery`` 自身还有一道
    库级闸（消费游标 + 回执标识），只断言"库里没被写脏"会让两道闸互相遮蔽——
    单独把消费侧的判据改回旧行为时，库级闸仍会挡住，用例照样全绿。这里直接
    断言"这一轮**根本不该发起**这次确认调用"。
    """

    def __init__(self, queue: Any) -> None:
        self._queue = queue
        self.calls: list[dict] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._queue, name)

    def confirm_delivery(self, **kwargs: object) -> bool:
        self.calls.append(dict(kwargs))
        return self._queue.confirm_delivery(**kwargs)


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


class _PerTaskCursorFailure:
    """代理真实 ``PostgresTaskQueue``：**按 task_id** 决定这一条候选怎么坏。

    ``advance_then_raise`` 里的任务第一次推进游标成功、第二次抛；
    ``raise_immediately`` 里的任务第一次推进游标就抛（真正的零进展）。按 id 而不是
    按调用次序分派，是为了让用例不依赖候选发现查询实际返回的顺序。
    """

    def __init__(
        self, queue: Any, *, advance_then_raise: set[str], raise_immediately: set[str]
    ) -> None:
        self._queue = queue
        self._advance_then_raise = advance_then_raise
        self._raise_immediately = raise_immediately
        self.calls: dict[str, int] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._queue, name)

    def record_delivery_progress(self, *, task_id: str, **kwargs: object) -> None:
        seen = self.calls.get(task_id, 0) + 1
        self.calls[task_id] = seen
        if task_id in self._raise_immediately:
            raise RuntimeError("simulated transient database error")
        if task_id in self._advance_then_raise and seen >= 2:
            raise RuntimeError("simulated transient database error")
        self._queue.record_delivery_progress(task_id=task_id, **kwargs)


class _RaisesOnNthCursorAdvance:
    """代理真实 ``PostgresTaskQueue``：第 ``fail_on_call`` 次推进游标时抛。

    用来把"这一轮有没有进展"的两半分开造：``fail_on_call=1`` 是真正的零进展
    （一次游标都没推进就抛），``fail_on_call=2`` 是"已经推进过、之后才抛"。
    """

    def __init__(self, queue: Any, *, fail_on_call: int) -> None:
        self._queue = queue
        self._fail_on_call = fail_on_call
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._queue, name)

    def record_delivery_progress(self, **kwargs: object) -> None:
        self.calls += 1
        if self.calls == self._fail_on_call:
            raise RuntimeError("simulated transient database error")
        self._queue.record_delivery_progress(**kwargs)


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN") and psycopg_available(), SKIP_REASON)
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

    def expire_retry_backoff(self, task_id: str) -> None:
        """把这条任务的重试退避时刻拨到过去，表示"退避窗口已经过去了"。

        退避时刻是库里的 ``timestamptz``，不受注入时钟影响；真库用例表示时间流逝
        的既有手法就是直接改这类时间列（见 ``QueueDelayHintTests`` 的 ``created_at``）。
        """
        self.execute(
            "UPDATE task SET delivery_retry_after = now() - interval '1 second' WHERE id = %s",
            (task_id,),
        )

    def start_task(self, task_id: str) -> None:
        self.queue.append_delivery_event(
            task_id=task_id,
            worker_id="worker-1",
            event_type="started",
            idempotency_key=f"{task_id}:a1:started",
        )

    def finish_task(self, task_id: str, *, content: str = "已送达的答案") -> None:
        self.queue.write_terminal_event(
            task_id=task_id,
            worker_id="worker-1",
            terminal_kind="success",
            error_kind=None,
            content=content,
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
            task_id="tsk-1",
            worker_id="worker-1",
            event_type="progress",
            idempotency_key="tsk-1:a1:progress:1",
            elapsed_seconds=3,
        )
        self.finish_task("tsk-1")
        processed = consumer.run_once()

        self.assertEqual(processed, 1)
        self.assertEqual(len(cards.create_calls), 1, "只建一次卡片")
        self.assertEqual(
            [call["sequence"] for call in cards.update_calls],
            [1, 2],
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
            task_id="tsk-1",
            worker_id="worker-1",
            event_type="progress",
            idempotency_key="tsk-1:a1:progress:1",
            elapsed_seconds=5,
            content=encode_progress_action(PROGRESS_ACTION_QUERYING, query_count=2),
        )
        consumer.run_once()

        self.assertEqual(len(cards.update_calls), 1, "确实推进到了 progress 更新这一步")
        rendered_body = cards.update_calls[0]["card"].body
        self.assertIn("正在第 2 次查询指标数据", rendered_body)
        self.assertNotIn("正在处理", rendered_body, "不应该退回默认 processing 文案")

    def test_the_accumulated_step_list_survives_across_separate_poll_rounds(self) -> None:
        """Issue #407 方向 B：``_process_task`` 每一轮都会构造一个全新的
        ``CardStream``（本类文件头「过程流式的正文累积」已说明）——这条用例
        证明第二轮轮询构造的新实例仍然能从 outbox 找回第一轮已经追加过的行，
        卡片正文只会变长，不会在跨轮询边界"变短"。

        变异存活证据：把 ``DeliveryConsumer._prior_progress_history`` 改成恒
        返回 ``()``（不重建历史），本用例的第二次断言会变红——第二轮的正文
        会只剩第二行，缺掉第一轮已经展示过的第一行。

        Trace #469 S-1 TOP-9：第三轮追加第二行之后，第一行不再是"当前正在
        发生的步骤"，改用完成时措辞展示（"已完成第 1 次查询..."），只有
        当前追加的最后一行继续用"正在..."现在时措辞。
        """

        from lingxi.core.execution.card_stream import (
            PROGRESS_ACTION_COMPOSING,
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
        consumer.run_once()  # 第一轮：只处理 started，建卡。

        clock[0] = 0.6
        self.queue.append_delivery_event(
            task_id="tsk-1",
            worker_id="worker-1",
            event_type="progress",
            idempotency_key="tsk-1:a1:progress:1",
            elapsed_seconds=5,
            content=encode_progress_action(
                PROGRESS_ACTION_QUERYING, query_count=1, query_step="list_metrics"
            ),
        )
        consumer.run_once()  # 第二轮：新建一个 CardStream 处理第一条 progress。
        first_round_body = cards.update_calls[-1]["card"].body
        self.assertEqual(first_round_body, "正在第 1 次查询可用指标列表 · 5 秒")

        clock[0] = 1.2
        self.queue.append_delivery_event(
            task_id="tsk-1",
            worker_id="worker-1",
            event_type="progress",
            idempotency_key="tsk-1:a1:progress:2",
            elapsed_seconds=9,
            content=encode_progress_action(PROGRESS_ACTION_COMPOSING),
        )
        consumer.run_once()  # 第三轮：又一次全新的 CardStream，处理第二条 progress。
        second_round_body = cards.update_calls[-1]["card"].body

        self.assertEqual(
            second_round_body,
            "已完成第 1 次查询可用指标列表 · 5 秒\n正在整理与生成回答 · 9 秒",
            "第三轮（全新的 CardStream 实例）必须找回第二轮已经追加过的第一行"
            "（翻篇后改用完成时措辞）",
        )

    def test_a_second_round_with_no_new_events_does_not_repeat_delivery(self) -> None:
        """重复轮询（没有新事件）不应该产生第二次外部调用（状态合同第 7 条）。"""

        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.start_task("tsk-1")
        self.finish_task("tsk-1")
        cards = RecordingCards()
        texts = RecordingText()
        consumer = DeliveryConsumer(queue=self.queue, cards=cards, texts=texts)
        consumer.run_once()
        first_round_calls = (
            len(cards.create_calls) + len(cards.update_calls) + len(cards.close_calls)
        )

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
        """文本兜底同步捕获到明确失败：清预留位、不确认送达，退避过后可以重试。

        明确失败带有退避，退避时刻**落在库里**；这里把它拨到过去来表示"时间过
        去了"（同 ``QueueDelayHintTests`` 用 ``created_at`` 表示排队时长的手法），
        验证的仍然是"下一轮重试"这件事本身，不测退避的具体时长。
        """

        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.start_task("tsk-1")
        self.finish_task("tsk-1")

        cards = RecordingCards(fail_at=1)  # create 本身就失败，直接走文本通道
        texts = RecordingText(fail=True)
        consumer = DeliveryConsumer(queue=self.queue, cards=cards, texts=texts)
        consumer.run_once()

        row = self.query(
            "SELECT status, dispatch_reserved_kind, delivery_message_id FROM task WHERE id='tsk-1'"
        )[0]
        self.assertEqual(row[0], "awaiting_delivery", "明确失败不确认送达，任务保持待投递")
        self.assertIsNone(row[1], "明确失败必须清空预留位，允许下一轮重试")
        self.assertIsNone(row[2])

        # 退避窗口内立即重试一次：不应该产生新的外发尝试（无退避会打平台限流）。
        self.assertEqual(consumer.run_once(), 0, "退避窗口内连候选名额都不该占")
        self.assertEqual(len(texts.calls), 1, "退避窗口内不应该再次尝试外发")

        # 退避时刻拨到过去 + 文本发送恢复正常：下一轮应当成功重试并确认送达。
        self.expire_retry_backoff("tsk-1")
        texts.fail = False
        consumer.run_once()
        row = self.query("SELECT status FROM task WHERE id='tsk-1'")[0]
        self.assertEqual(row[0], "succeeded")
        self.assertEqual(len(texts.calls), 2, "第一次失败 + 退避过后的第二次重试各一次")


class CardFailureInjectionAcceptanceFixtureTests(DeliveryConsumerTestCase):
    """S-A-07 受控验收缺口专用注入开关（Issue #152 验收缺口、#154 评论
    5306860510、#162 E-022）：``apps.gateway.RejectingCards`` 命中被选中的那一步
    时确定性抛出 ``DeliveryRejectedError``，走的正是 ``CardFailureFallsBackToTextTests``
    已经验证过的既有降级路径——这里额外验证的是注入开关本身"命中步骤即拒绝、
    未命中步骤直通真实 transport"这条装配契约，而不是重新验证降级路径本身。
    """

    def test_create_injection_falls_back_to_a_single_text_terminal(self) -> None:
        from lingxi.apps.gateway.delivery_assembly import RejectingCards

        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.start_task("tsk-1")
        self.finish_task("tsk-1", content="已产生的答案")

        real_cards = RecordingCards()
        cards = RejectingCards(real_cards, inject="create")
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
        这是 ``RejectingCards`` 文档写明的设计取舍，必须能被证伪。
        """

        from lingxi.apps.gateway.delivery_assembly import RejectingCards

        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.start_task("tsk-1")
        self.finish_task("tsk-1", content="已产生的答案")

        real_cards = RecordingCards()
        cards = RejectingCards(real_cards, inject="update")
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
    只有 ``DeliveryRejectedError``（服务端已经给出完整响应且业务错误码明确拒绝）才是
    "明确失败"；除它以外的一切——`requests` 的超时/连接类异常（真实 adapter 走
    lark-oapi，其 transport 是 ``requests.request(...)``，全部继承内置
    ``OSError``）、JSON 解析失败（``json.JSONDecodeError``）、响应结构缺失
    （``success()`` 为真但拿不到可回读标识）、任何其它未预期的异常——都不得被
    当成"明确失败"清预留位、降级或重试，必须转入 ``uncertain``、告警、预留位
    原样保留。用 ``TimeoutError``、``json.JSONDecodeError``、``LookupError``
    （模拟"success 真但缺可回读标识"）分别覆盖这几类成因，与 ``DeliveryRejectedError``
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
            on_alert=lambda kind, task_id, trace_id=None: alerts.append((kind, task_id)),
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

        # create 本身明确失败（DeliveryRejectedError，行为不变），直接走文本通道；
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
        """明确失败路径行为不变：`DeliveryRejectedError`（服务端明确拒绝，独立审核
        R-1 用它取代此前注入 ``RuntimeError`` 的既有测试写法）仍然立即清预留位、
        允许下一轮重试——与上面几条"结果不明"用例对照，证明白名单反转只改变了
        判别方向，没有改变既有的"明确失败"语义。
        """

        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.start_task("tsk-1")

        cards = RecordingCards(fail_at=1, fail_error=DeliveryRejectedError)
        texts = RecordingText()
        consumer = DeliveryConsumer(queue=self.queue, cards=cards, texts=texts)
        consumer.run_once()

        row = self.query("SELECT dispatch_reserved_kind, fallback_text FROM task WHERE id='tsk-1'")[
            0
        ]
        self.assertIsNone(
            row[0], "明确失败必须清空预留位，允许下一轮重试——与上面三条 TimeoutError 用例的行为相反"
        )
        self.assertTrue(row[1], "明确失败整体降级为文本通道，这个既有语义没有被本次修复改变")

    def test_a_json_decode_error_during_card_create_does_not_retry_automatically(self) -> None:
        """独立审核 R-1 新增：`lark_oapi` 内部响应体解析失败时抛出的
        `json.JSONDecodeError` 不是黑名单能挡住的 `OSError`——旧的黑名单实现会把它
        当成"明确失败"清预留位、立即降级；白名单反转后，除 `DeliveryRejectedError`
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
        `adapters.feishu_delivery` 的模块说明）同样不是 `DeliveryRejectedError`——
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

        row = self.query("SELECT status, dispatch_reserved_kind FROM task WHERE id='tsk-1'")[0]
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
                task_id="tsk-1",
                worker_id="worker-1",
                event_type="progress",
                idempotency_key=f"tsk-1:a1:progress:{index}",
                elapsed_seconds=index,
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

        self.assertEqual(
            len(cards.update_calls), 0, "建卡本身消费了首个限流名额，同一时刻的更新被抑制"
        )
        cursor = self.scalar("SELECT delivery_consumed_sequence FROM task WHERE id='tsk-1'")
        self.assertEqual(cursor, 4, "游标必须推进到最后一个序号，即使更新被限流抑制")

        # 时钟前进超过 500ms 后的下一轮：限流解除，用最新状态发一次更新。
        clock[0] = 0.6
        self.queue.append_delivery_event(
            task_id="tsk-1",
            worker_id="worker-1",
            event_type="progress",
            idempotency_key="tsk-1:a1:progress:4",
            elapsed_seconds=9,
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


class PreprovisionNoticeConsumptionTests(DeliveryConsumerTestCase):
    """Issue #541 预开通：首聊补一句的消费端（``consume_preprovision_notice``）。

    只有真库能证伪"只提示一次"：查询与标记落在同一条 ``UPDATE ... RETURNING`` 里，
    在假 store 上无论实现怎么写都是绿的。挂起端（``mark_preprovision_notice_pending``
    的两道守卫）在 ``tests/test_identity_postgres_records.py``。
    """

    def _consume(self) -> bool:
        from lingxi.adapters.postgres_conversation import PostgresGatewayStore

        with PostgresGatewayStore(self._dsn).transaction() as tx:
            return tx.consume_preprovision_notice(user_id="usr-1")

    def test_an_armed_line_is_consumed_exactly_once(self) -> None:
        self.execute("UPDATE app_user SET preprovision_notice_armed_at = now() WHERE id = 'usr-1'")

        self.assertTrue(self._consume(), "挂起过就该在首聊时命中一次")
        self.assertFalse(self._consume(), "同一次挂起只提示一次")
        self.assertIsNotNone(
            self.scalar("SELECT preprovision_notice_sent_at FROM app_user WHERE id = 'usr-1'")
        )

    def test_a_user_who_was_never_armed_is_never_prompted(self) -> None:
        """**否定断言**：绝大多数用户从未被预开通，这条查询对他们恒为假。"""

        self.assertFalse(self._consume())
        self.assertIsNone(
            self.scalar("SELECT preprovision_notice_sent_at FROM app_user WHERE id = 'usr-1'")
        )

    # ------------------------------------------------------------------
    # peek（rc25 修复包 F1）：先渲染后消费的读取端。只有真库能证伪三件事：
    # 挂起判定与 consume 用同一对列、权限快照按 published + payload ? 'permissions'
    # + 版本对齐取、peek 本身零写入。
    # ------------------------------------------------------------------

    def _peek(self):
        from lingxi.adapters.postgres_conversation import PostgresGatewayStore

        with PostgresGatewayStore(self._dsn).transaction() as tx:
            return tx.peek_preprovision_notice(user_id="usr-1")

    def _seed_outbox(self, *, payload: str, version: int = 1, status: str = "published") -> None:
        self.execute(
            "UPDATE app_user SET permission_version = %s, "
            "preprovision_notice_armed_at = now() WHERE id = 'usr-1'",
            (version,),
        )
        self.execute(
            """INSERT INTO publish_outbox
                 (id, user_id, permission_version, reason, payload, status,
                  created_at, published_at, content_expires_at)
               VALUES ('pub-usr-1', 'usr-1', %s, 'first_onboarding', %s, %s,
                       now(), CASE WHEN %s = 'published' THEN now() END, now())""",
            (version, payload, status, status),
        )

    def test_peek_returns_the_published_scope_and_does_not_consume(self) -> None:
        permissions = '{"C001": ["\u9500\u552e\u57df"]}'
        self._seed_outbox(payload=json.dumps({"permissions": permissions}))

        first = self._peek()
        self.assertIsNotNone(first)
        self.assertEqual(first.permissions, permissions)
        second = self._peek()
        self.assertIsNotNone(second, "peek 只读，不得消费一次性标志")
        self.assertIsNone(
            self.scalar("SELECT preprovision_notice_sent_at FROM app_user WHERE id = 'usr-1'")
        )

        self.assertTrue(self._consume())
        self.assertIsNone(self._peek(), "消费之后不再有待说的那句话")

    def test_peek_reports_an_unavailable_snapshot_after_payload_redaction(self) -> None:
        """九十天保留期把 ``payload`` 擦成 ``'{}'`` 后：挂起仍然成立，但快照不可用
        （``permissions is None``）——调用方据此记审计、不消费，不发半句假话。"""

        self._seed_outbox(payload="{}")

        pending = self._peek()
        self.assertIsNotNone(pending, "挂起本身与快照可用性是两件事")
        self.assertIsNone(pending.permissions)

    def test_peek_ignores_an_unpublished_intent(self) -> None:
        """还没发布出去的意图不算数：快照必须是**已发布**那一版（与
        ``postgres_late_readiness_recovery`` 的候选判据同一套）。"""

        self._seed_outbox(payload=json.dumps({"permissions": "{}"}), status="pending")

        pending = self._peek()
        self.assertIsNotNone(pending)
        self.assertIsNone(pending.permissions)

    def test_peek_is_none_for_a_user_who_was_never_armed(self) -> None:
        self.assertIsNone(self._peek())


class QueueDelayHintTests(DeliveryConsumerTestCase):
    """Issue #465（rc22 S-3，排队可感知）：真库读取面 + `DeliveryConsumer` 消费面。

    与本文件其余用例的区别：这里的任务从未被任何 worker 领取（``status='queued'``，
    没有 ``worker_id``/``heartbeat_at``），因此从不出现在 ``list_pending_delivery_
    tasks``/``start_task`` 的既有路径里——``list_stale_queued_tasks`` 是唯一能看见
    它的查询。
    """

    def seed_queued_task(
        self,
        *,
        task_id: str,
        conversation_id: str,
        created_seconds_ago: float,
        reply_to_message_id: str = "reply-1",
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
                target_worker_version,attempts,reply_to_message_id,created_at,
                content_expires_at)
               VALUES (%s,%s,'usr-1',%s,'问题','queued','stable',0,%s,
                       now() - %s * interval '1 second', now())""",
            (
                task_id,
                conversation_id,
                f"event-{task_id}",
                reply_to_message_id,
                created_seconds_ago,
            ),
        )

    def test_only_tasks_past_the_threshold_are_returned(self) -> None:
        self.seed_queued_task(task_id="tsk-stale", conversation_id="cnv-1", created_seconds_ago=30)
        self.seed_queued_task(task_id="tsk-fresh", conversation_id="cnv-2", created_seconds_ago=1)

        stale = self.queue.list_stale_queued_tasks(older_than=timedelta(seconds=12), limit=50)

        self.assertEqual([row.task_id for row in stale], ["tsk-stale"])
        self.assertEqual(stale[0].chat_id, "chat-cnv-1")
        self.assertEqual(stale[0].thread_id, "topic-cnv-1")
        self.assertEqual(stale[0].reply_to_message_id, "reply-1")

    def test_delivery_consumer_sends_the_hint_exactly_once_per_stale_task(self) -> None:
        self.seed_queued_task(task_id="tsk-stale", conversation_id="cnv-1", created_seconds_ago=30)
        texts = RecordingText()
        consumer = DeliveryConsumer(
            queue=self.queue, cards=RecordingCards(), texts=texts, queue_delay_hint_seconds=12.0
        )

        consumer.run_once()
        consumer.run_once()

        self.assertEqual(len(texts.calls), 1, "同一个任务在持续排队期间只提示一次")
        self.assertEqual(
            texts.calls[0]["text"],
            default_content_catalog().text("gateway.busy_hint_queued").text,
        )
        self.assertEqual(texts.calls[0]["chat_id"], "chat-cnv-1")
        # 尽力而为的体验提示：不改变任务本身的状态，不产生任何 outbox 事件。
        self.assertEqual(self.scalar("SELECT status FROM task WHERE id='tsk-stale'"), "queued")
        self.assertEqual(
            self.scalar("SELECT count(*) FROM task_delivery_event WHERE task_id='tsk-stale'"), 0
        )

    def test_a_task_below_the_threshold_gets_no_hint(self) -> None:
        self.seed_queued_task(task_id="tsk-fresh", conversation_id="cnv-1", created_seconds_ago=1)
        texts = RecordingText()
        consumer = DeliveryConsumer(
            queue=self.queue, cards=RecordingCards(), texts=texts, queue_delay_hint_seconds=12.0
        )

        consumer.run_once()

        self.assertEqual(texts.calls, [], "刻意不做入站即回执：正常秒级领取时零噪音")

    def test_leaving_and_rejoining_the_stale_set_is_treated_as_a_new_wait(self) -> None:
        """被领取后不再是候选，进程内去重集合随之收缩；此后若又重新排队够久
        （现实中不该发生，这里只是构造出同一个 task_id 二次跨入候选集合），
        应当被当成一次新的排队重新提示，而不是被旧的去重记录永久挡住。"""

        self.seed_queued_task(task_id="tsk-stale", conversation_id="cnv-1", created_seconds_ago=30)
        texts = RecordingText()
        consumer = DeliveryConsumer(
            queue=self.queue, cards=RecordingCards(), texts=texts, queue_delay_hint_seconds=12.0
        )
        consumer.run_once()
        self.assertEqual(len(texts.calls), 1)

        self.execute(
            "UPDATE task SET status='running', worker_id='worker-1', heartbeat_at=now() "
            "WHERE id='tsk-stale'"
        )
        consumer.run_once()
        self.execute(
            "UPDATE task SET status='queued', worker_id=NULL, heartbeat_at=NULL "
            "WHERE id='tsk-stale'"
        )
        consumer.run_once()

        self.assertEqual(len(texts.calls), 2)


class TerminalReceiptRequiredBeforeConfirmTests(DeliveryConsumerTestCase):
    """IN-03（`V-投递-03`／`V-投递-04`）：**只有本次终态的有效回执**才允许确认送达。

    这一组是缺陷的真库复现与五分支回放。旧实现里 `_handle_terminal` 在两条通道
    都失败时仍然返回"可以确认"，`_maybe_confirm` 于是拿建进度卡那一轮写入的旧
    ``delivery_message_id`` 完成确认，同一事务里把任务记成 ``succeeded``、把
    ``conversation.running_task_id`` 清空——用户一个字都没收到，任务却已经收口、
    话题已经释放。
    """

    def _seed_task_with_terminal(self, task_id: str = "tsk-1") -> None:
        self.seed_running_task(task_id=task_id, conversation_id="cnv-1")
        self.start_task(task_id)
        self.finish_task(task_id, content="已产生的答案")

    def _assert_not_delivered(self, task_id: str = "tsk-1") -> None:
        """终态没有落地时必须成立的一组事实：任务不记成功、话题不释放、送达标记为空。"""
        row = self.query(
            "SELECT status, delivery_consumed_sequence, delivery_message_id FROM task WHERE id=%s",
            (task_id,),
        )[0]
        self.assertEqual(row[0], "awaiting_delivery", "两条通道都没成交，任务不得记成功")
        terminal_sequence = self.scalar(
            "SELECT sequence FROM task_delivery_event WHERE task_id=%s AND event_type='terminal'",
            (task_id,),
        )
        self.assertLess(row[1], terminal_sequence, "终态事件没被受理，消费游标不得越过它")
        self.assertEqual(
            self.scalar("SELECT running_task_id FROM conversation WHERE id='cnv-1'"),
            task_id,
            "话题必须继续占用——释放了就等于告诉下一轮提问「上一题已经结束」",
        )
        self.assertIsNone(
            self.scalar(
                "SELECT platform_received_at FROM task_delivery_event "
                "WHERE task_id=%s AND event_type='terminal'",
                (task_id,),
            ),
            "平台从未确认接收，platform_received_at 必须保持 NULL",
        )

    def test_card_terminal_and_text_fallback_both_rejected_is_never_confirmed(self) -> None:
        """**明确失败分支**（#615 R1 真库复现）：建卡成功并已持久化
        ``card_id``/``delivery_message_id``，终态卡片更新被明确拒绝、文本兜底
        也被明确拒绝——绝不能拿建卡那一轮的旧 ``message_id`` 确认送达。
        """

        self._seed_task_with_terminal()
        cards = RecordingCards(fail_at=2)  # 第 1 次 create 成功，第 2 次（终态 update）被拒
        texts = RecordingText(fail=True)
        consumer = DeliveryConsumer(queue=self.queue, cards=cards, texts=texts)
        consumer.run_once()

        self.assertEqual(len(cards.create_calls), 1)
        self.assertEqual(len(texts.calls), 1, "文本兜底尝试过一次，并被明确拒绝")
        self.assertEqual(
            self.scalar("SELECT delivery_message_id FROM task WHERE id='tsk-1'"),
            "msg-card-1",
            "建进度卡那一轮拿到的标识仍然留在库里——修复的是「不拿它确认」，不是「不落库」",
        )
        self.assertIsNone(
            self.scalar("SELECT dispatch_reserved_kind FROM task WHERE id='tsk-1'"),
            "明确失败清预留位，允许下一轮按退避重试",
        )
        self._assert_not_delivered()

    def test_the_consumer_does_not_even_attempt_to_confirm_without_a_receipt(self) -> None:
        """同一场景的**消费侧**独立断言：连 ``confirm_delivery`` 这次调用都不该发起。

        库级闸（消费游标 + 回执标识）是第二道防线；只断言"库里没被写脏"会让两道
        闸互相遮蔽。这条用例直接盯住消费循环自己的判据——把它改回"只要任务在
        ``awaiting_delivery`` 且流上有任意 ``message_id`` 就确认"，本用例立刻变红，
        而只看落库结果的那些用例仍会全绿。
        """

        self._seed_task_with_terminal()
        spy = _ConfirmDeliverySpy(self.queue)
        cards = RecordingCards(fail_at=2)
        texts = RecordingText(fail=True)
        DeliveryConsumer(queue=spy, cards=cards, texts=texts).run_once()

        self.assertEqual(
            spy.calls,
            [],
            "两条通道都没成交，消费侧不得拿建进度卡那一轮的旧标识去发起确认",
        )
        self._assert_not_delivered()

    def test_a_later_round_with_unconsumed_events_never_confirms_with_a_stale_id(self) -> None:
        """**应用层第一道闸**（``saw_unconsumed_events``）：本轮读到了未消费事件、却
        没能取得本次终态的回执 → 一律不确认。

        这半个窗口只有**第二轮及以后**才暴露：第一轮建进度卡拿到的 ``msg-card-1``
        已经落库，于是从第二轮起任务快照本身就带着一枚"看起来能用"的旧标识，
        ``task.message_id is None`` 那道兜底判据再也挡不住它。变异锚点：删掉
        ``_terminal_receipt`` 里的 ``saw_unconsumed_events`` 判据 → 第二轮会拿这枚
        旧标识去确认一条从未送到用户的答案（库级闸随后仍会挡下，但消费侧已经做出
        了错误裁定），本用例变红。
        """

        self._seed_task_with_terminal()
        cards = RecordingCards(fail_at=2)  # create 成功并落库 msg-card-1，终态 update 被拒
        texts = RecordingText(fail=True)
        DeliveryConsumer(queue=self.queue, cards=cards, texts=texts).run_once()

        self.assertEqual(
            self.scalar("SELECT delivery_message_id FROM task WHERE id='tsk-1'"),
            "msg-card-1",
            "第二轮的任务快照从这一刻起就带着一枚旧标识",
        )
        self._assert_not_delivered()

        # 上一轮的明确失败已经写下一段退避；这里把它拨到过去，好让本用例真正跑到
        # "第二轮拿着旧标识"的那半个窗口——本用例要钉的是回执判据，不是退避。
        self.expire_retry_backoff("tsk-1")
        spy = _ConfirmDeliverySpy(self.queue)
        processed = DeliveryConsumer(queue=spy, cards=cards, texts=texts).run_once()

        self.assertEqual(processed, 1, "这一轮确实处理了这个任务，不是空转")
        self.assertEqual(
            spy.calls,
            [],
            "本轮仍有未消费的终态事件、仍然没有回执，消费侧连这次确认调用都不该发起",
        )
        self._assert_not_delivered()

    def test_a_backoff_window_produces_no_attempt_and_no_confirmation(self) -> None:
        """**安全重试分支的否定面**：退避窗口内这一轮**一次外发都没发生**，
        同样不得确认——旧实现里这条路径返回 ``RETRY_LATER``，照样走去确认。
        退避落库之后这一轮更进一步：这个任务连候选名额都不占。
        """

        self._seed_task_with_terminal()
        cards = RecordingCards(fail_at=1)  # create 就被拒，直接走文本通道
        texts = RecordingText(fail=True)
        spy = _ConfirmDeliverySpy(self.queue)
        consumer = DeliveryConsumer(queue=spy, cards=cards, texts=texts)
        consumer.run_once()
        self.assertEqual(len(texts.calls), 1)

        self.assertEqual(consumer.run_once(), 0, "退避窗口内不得占用候选名额")
        self.assertEqual(len(texts.calls), 1, "退避窗口内不得产生新的外发尝试")
        self.assertEqual(spy.calls, [], "一次外发都没发生的轮次不得发起确认")
        self._assert_not_delivered()

    def test_an_in_flight_card_terminal_is_never_retried_automatically(self) -> None:
        """**在途不重投分支**：终态卡片更新拿到 ``230049``「仍在发送」。

        不得判成明确拒绝（那会清预留位并改发一条文本终态，而卡片随后很可能真的
        更新成功，用户收到两份）；也不得判成已送达；**更不得自动重投**——CardKit 的
        整卡级 ``sequence`` 是严格递增的操作序号，不是平台去重键：这条路径上
        ``card_seq`` 根本没有推进（HALT 不落进度），重投用的是同一个序号，会被平台
        判成落后序号直接拒绝、随即降级到文本通道，而卡片里已经有那份答案了，用户
        于是收到第二份完整答案。因此保留预留位、转人工核对，最终由到期收敛路径
        （``expire_undelivered_terminals``）在二十四小时后收成"投递已过期"。

        变异锚点：把 ``core.delivery.ports`` 里 ``card_update``/``card_close`` 的
        ``platform_idempotency_key`` 改回 ``"sequence"`` → 本用例变红（裁定不再由
        本文件硬写，见 :func:`_in_flight_error`）。
        """

        # 本用例的前提直接取自规则表，不靠本文件复述：卡片这两次外发**没有登记任何
        # 平台去重键**。消费侧现在无条件保留预留位（不再按 ``retry_safe`` 分支），
        # 因此这条前提只能在这里显式核对——否则改了规则表，下面那些行为断言会毫无
        # 反应地继续全绿，正是这次修复要消灭的那种假绿。
        for operation in (DeliveryOperation.CARD_UPDATE, DeliveryOperation.CARD_CLOSE):
            self.assertIsNone(
                DELIVERY_OPERATIONS[operation].platform_idempotency_key,
                f"{operation.value} 一旦被登记成带去重键，下面这些「绝不重投」断言就不再成立",
            )

        self._seed_task_with_terminal()
        clock = [0.0]
        alerts: list[tuple[str, str]] = []
        cards = InFlightTerminalCards(in_flight_updates=1)
        texts = RecordingText()
        consumer = DeliveryConsumer(
            queue=self.queue,
            cards=cards,
            texts=texts,
            monotonic=lambda: clock[0],
            on_alert=lambda kind, task_id, trace_id=None: alerts.append((kind, task_id)),
        )
        consumer.run_once()

        self.assertEqual(texts.calls, [], "「仍在发送」不得触发跨通道的文本兜底")
        self.assertFalse(
            self.scalar("SELECT fallback_text FROM task WHERE id='tsk-1'"),
            "「仍在发送」不是明确失败，不得把任务永久降级到文本通道",
        )
        self.assertEqual(
            self.scalar("SELECT dispatch_reserved_kind FROM task WHERE id='tsk-1'"),
            "card_finish",
            "没有平台去重键的通道遇到结果不明必须保留预留位，交人工核对",
        )
        self.assertEqual(
            alerts,
            [("card_finish_uncertain:in_flight", "tsk-1")],
            "不可重投是这条分支的常态出口，必须告警，不能只写一行日志",
        )
        self._assert_not_delivered()

        # 之后每一轮都抢不到预留位：既不重投，也不会悄悄改走文本通道。
        clock[0] += DeliveryConsumer.DEFAULT_RETRY_BACKOFF_CAP_SECONDS * 10
        self.assertEqual(consumer.run_once(), 0, "预留位卡住的任务不进入正常候选")
        self.assertEqual(len(cards.update_calls), 0, "绝不自动重投同一个 sequence")
        self.assertEqual(texts.calls, [])
        self.assertEqual(
            [task.reserved_kind for task in self.queue.list_uncertain_delivery_tasks()],
            ["card_finish"],
        )
        self._assert_not_delivered()

    def test_an_in_flight_text_fallback_is_never_retried_automatically(self) -> None:
        """**不明分支**：文本兜底拿到「仍在发送」。

        文本发送请求体没有任何平台幂等键，自动重投等于赌"上一次没送到"，赌错
        就是两条一模一样的答案。因此保留预留位、转人工核对，不重试也不确认。
        """

        self._seed_task_with_terminal()
        cards = RecordingCards(fail_at=1)  # create 被明确拒绝，直接走文本通道
        texts = InFlightText()
        consumer = DeliveryConsumer(queue=self.queue, cards=cards, texts=texts)
        consumer.run_once()

        self.assertEqual(len(texts.calls), 1)
        self.assertEqual(
            self.scalar("SELECT dispatch_reserved_kind FROM task WHERE id='tsk-1'"),
            "text_send",
            "没有幂等键的通道遇到结果不明必须保留预留位，交人工核对",
        )
        self._assert_not_delivered()

        consumer.run_once()
        self.assertEqual(len(texts.calls), 1, "预留位卡住的任务不进入正常候选，绝不自动重发")
        self.assertEqual(
            [task.reserved_kind for task in self.queue.list_uncertain_delivery_tasks()],
            ["text_send"],
        )

    def test_a_delivered_terminal_whose_confirm_failed_is_confirmed_on_a_later_round(self) -> None:
        """**成功 + 会话释放分支**：终态已经真的送到卡片上，只是 ``confirm_delivery``
        那一步遇到瞬时错误。下一轮没有任何新事件，仍然必须完成确认并释放话题
        ——收紧确认前提不得把这条既有的恢复路径一起关掉。
        """

        self._seed_task_with_terminal()
        cards = RecordingCards()
        texts = RecordingText()
        flaky_queue = _ConfirmDeliveryFailsOnce(self.queue)
        DeliveryConsumer(queue=flaky_queue, cards=cards, texts=texts).run_once()

        self.assertEqual(len(cards.update_calls), 1, "终态更新已经真实发出并成功")
        self.assertEqual(
            self.scalar("SELECT status FROM task WHERE id='tsk-1'"),
            "awaiting_delivery",
            "确认这一步失败了，任务还没收口",
        )

        DeliveryConsumer(queue=self.queue, cards=cards, texts=texts).run_once()

        self.assertEqual(len(cards.update_calls), 1, "不得重放已经成功的终态更新")
        self.assertEqual(texts.calls, [], "不得改发一条重复的文本终态")
        self.assertEqual(self.scalar("SELECT status FROM task WHERE id='tsk-1'"), "succeeded")
        self.assertIsNone(
            self.scalar("SELECT running_task_id FROM conversation WHERE id='cnv-1'"),
            "确认送达后话题才释放",
        )
        self.assertEqual(
            self.scalar(
                "SELECT platform_message_kind FROM task_delivery_event "
                "WHERE task_id='tsk-1' AND event_type='terminal'"
            ),
            "card",
        )


class ThrowAfterProgressDoesNotBackOffTests(DeliveryConsumerTestCase):
    """抛穿不等于零进展（IN-06）。

    「单任务异常不带走整轮」那条隔离 handler 里一律记一档退避，用的是**候选时刻**
    读到的 ``retry_attempts``。可推进游标的写回本身会把退避两列清零，因此一条
    "已经推进过、之后才抛"的任务会被拿旧档位重新记一次退避——最多让一次正在推进的
    投递白等一个退避窗口（上限 300 秒）。判据改成「本轮确实零进展才记退避」。

    变异锚点：把 ``run_once`` 里的 ``if not self._advanced_this_task:`` 去掉 →
    ``test_a_throw_after_the_cursor_advanced_records_no_backoff`` 变红。
    """

    def _seed_started_and_progress(self) -> None:
        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.start_task("tsk-1")
        self.queue.append_delivery_event(
            task_id="tsk-1",
            worker_id="worker-1",
            event_type="progress",
            idempotency_key="tsk-1:a1:progress:1",
            elapsed_seconds=3,
        )
        # 候选时刻已经累了几档退避：判据错的话会在这个旧值上继续往上加。
        self.execute("UPDATE task SET delivery_retry_attempts = 5 WHERE id = 'tsk-1'")

    def _run(self, *, fail_on_call: int) -> None:
        clock = [0.0]
        failing = _RaisesOnNthCursorAdvance(self.queue, fail_on_call=fail_on_call)
        consumer = DeliveryConsumer(
            queue=failing,
            cards=RecordingCards(),
            texts=RecordingText(),
            monotonic=lambda: clock[0],
        )
        consumer.run_once()

    def test_a_throw_after_the_cursor_advanced_records_no_backoff(self) -> None:
        """建卡这一步已经推进过游标（退避两列刚被清零），随后抛：不得再记退避。"""

        self._seed_started_and_progress()

        self._run(fail_on_call=2)

        self.assertEqual(
            self.scalar("SELECT delivery_retry_attempts FROM task WHERE id='tsk-1'"),
            0,
            "推进游标那一步已经把档位清零，抛穿不得拿候选时刻的旧值再记一档",
        )
        self.assertIsNone(
            self.scalar("SELECT delivery_retry_after FROM task WHERE id='tsk-1'"),
            "已经在推进的投递不该被罚等一个退避窗口",
        )

    def test_a_throw_before_any_progress_still_records_backoff(self) -> None:
        """对照：这一轮一次游标都没推进就抛 → 照旧按退避让路，名额不白占。"""

        self._seed_started_and_progress()

        self._run(fail_on_call=1)

        self.assertEqual(
            self.scalar("SELECT delivery_retry_attempts FROM task WHERE id='tsk-1'"),
            6,
            "真正的零进展仍然要在候选时刻的档位上继续往上记",
        )
        self.assertIsNotNone(self.scalar("SELECT delivery_retry_after FROM task WHERE id='tsk-1'"))

    def test_a_zero_progress_task_still_backs_off_after_another_task_advanced(self) -> None:
        """跨任务不变量：同一轮里前面那条推进过，后面这条**真零进展**仍必须记退避。

        「本轮有没有进展」是**每条候选各算各的**。标志位如果不在每条候选开头重置，
        排在推进过的那条之后的真零进展任务就漏记退避、继续每轮占名额，IN-06 的公平性
        修复破一半。单任务用例看不见这条泄漏——它要两条候选同轮才成立。

        **不假设候选顺序**：角色按 ``list_pending_delivery_tasks`` 实读回来的次序分派
        （最后一条当零进展），因此该查询将来改排序也不会让本用例失去判别力。

        变异锚点：删掉 ``_process_task`` 开头那句 ``self._advanced_this_task = False``
        → 本用例变红（最后那条的退避档位停在候选时刻的旧值、退避时刻仍为空）。
        """

        for index in range(1, 4):
            task_id = f"tsk-{index}"
            self.seed_running_task(task_id=task_id, conversation_id=f"cnv-{index}")
            self.start_task(task_id)
            self.queue.append_delivery_event(
                task_id=task_id,
                worker_id="worker-1",
                event_type="progress",
                idempotency_key=f"{task_id}:a1:progress:1",
                elapsed_seconds=3,
            )
            # 候选时刻已经累了几档退避：判据错的话会在这个旧值上继续往上加。
            # `created_at` 不碰——库里有触发器禁止改任务创建时间，次序只能实读。
            self.execute("UPDATE task SET delivery_retry_attempts = 5 WHERE id = %s", (task_id,))

        order = [task.task_id for task in self.queue.list_pending_delivery_tasks(limit=10)]
        self.assertEqual(len(order), 3, "前提：三条都进候选，跨任务泄漏才有地方发生")
        *earlier, last = order

        failing = _PerTaskCursorFailure(
            self.queue, advance_then_raise=set(earlier), raise_immediately={last}
        )
        consumer = DeliveryConsumer(
            queue=failing,
            cards=RecordingCards(),
            texts=RecordingText(),
            monotonic=lambda: 0.0,
        )
        consumer.run_once()

        processed = list(failing.calls)
        self.assertGreater(
            processed.index(last),
            0,
            "前提不成立：零进展那条被排在了最前面，这一轮根本没有推进过的任务可供泄漏，"
            "本用例失去判别力——次序若真的变了，要改的是角色分派，不是把断言放宽",
        )
        self.assertEqual(
            self.scalar("SELECT delivery_retry_attempts FROM task WHERE id=%s", (last,)),
            6,
            "排在推进过的任务之后的真零进展任务，仍然要按候选时刻的档位记退避",
        )
        self.assertIsNotNone(
            self.scalar("SELECT delivery_retry_after FROM task WHERE id=%s", (last,)),
            "真零进展没记退避＝它下一轮照旧占名额，公平性修复破一半",
        )
        for task_id in earlier:
            with self.subTest(task_id=task_id):
                self.assertEqual(
                    self.scalar("SELECT delivery_retry_attempts FROM task WHERE id=%s", (task_id,)),
                    0,
                    "推进过的那条：档位已被写回清零，不得再记一档",
                )
                self.assertIsNone(
                    self.scalar("SELECT delivery_retry_after FROM task WHERE id=%s", (task_id,))
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
