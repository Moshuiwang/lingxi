"""投递调度公平性：待重试任务不占满候选机会（`V-队列-09`，真库多用户构造）。

与 ``test_gateway_delivery.py`` 的分工：那边一次只有一个任务，钉的是**单条任务**
的顺序、幂等与崩溃恢复；本文件一次构造**超过一批**的不同用户任务，钉的是**任务
之间**的名额分配——一条持续失败的老任务不得每轮都被选中却零进展，把批量上限之外
的健康用户永远挡在候选之外。

失败必须能按话题挑：真实故障从来不是"整个进程的卡片通道一起坏"，而是某几个人的
这次外发失败。因此本文件的两个传输层假实现按 ``chat_id`` 决定成败，而不是像
``RecordingCards`` 那样一个全局开关。
"""

from __future__ import annotations

from typing import Any

from test_gateway_delivery import DeliveryConsumerTestCase

from lingxi.apps.gateway.delivery import DeliveryConsumer
from lingxi.core.execution.card_stream import CardCreated, DeliveryRejectedError


class SelectiveCards:
    """按话题决定这一次建卡是成功还是被服务端**明确拒绝**。"""

    def __init__(self, *, rejecting_chats: set[str]) -> None:
        """记住哪些话题的建卡要被拒；这个集合允许调用方事后改，用来表示"故障恢复了"。"""
        self.rejecting_chats = rejecting_chats
        self.created_chats: list[str] = []

    def create(self, *, chat_id: str, thread_id: Any, reply_to_message_id: str, card: Any) -> Any:
        """建卡：命中集合就明确拒绝（消费循环据此永久降级到文本通道）。"""
        del thread_id, reply_to_message_id, card
        if chat_id in self.rejecting_chats:
            raise DeliveryRejectedError("按话题注入的建卡拒绝")
        self.created_chats.append(chat_id)
        return CardCreated(card_id=f"card-{chat_id}", message_id=f"msg-card-{chat_id}")

    def update(self, *, card_id: str, sequence: int, card: Any) -> None:
        """卡片流式更新：本文件不注入这一步的失败。"""
        del card_id, sequence, card

    def close(self, *, card_id: str, sequence: int, card: Any) -> None:
        """关闭卡片：本文件不注入这一步的失败。"""
        del card_id, sequence, card


class SelectiveText:
    """按话题决定这一次文本发送是成功还是被服务端明确拒绝。"""

    def __init__(self, *, rejecting_chats: set[str]) -> None:
        """记住哪些话题的文本发送要被拒；``attempts`` 记下每个话题被尝试了几次。"""
        self.rejecting_chats = rejecting_chats
        self.attempts: list[str] = []
        self.delivered: list[str] = []

    def send_text(
        self, *, chat_id: str, thread_id: Any, reply_to_message_id: str, text: str
    ) -> str:
        """发文本：先记尝试再判成败，好让"退避窗口内一次都没试过"可被观察。"""
        del thread_id, reply_to_message_id, text
        self.attempts.append(chat_id)
        if chat_id in self.rejecting_chats:
            raise DeliveryRejectedError("按话题注入的文本发送拒绝")
        self.delivered.append(chat_id)
        return f"msg-text-{chat_id}"


class _NoRetryBookkeepingQueue:
    """代理真实队列，只把重试退避的写回吞掉——**对照组**，不是修复。

    用来证伪"把批量上限调大就好了"：退避不落库时，候选查询看不见"这条任务还不
    该再试"，于是持续失败的任务每轮原样占满前 N 名。把上限从 20 调到 50 只是把
    "前 N 名"换成"前 50 名"，在失败任务数超过新上限的那一刻同样失效。
    """

    def __init__(self, queue: Any) -> None:
        """包住真实队列；除退避写回之外的一切都直通。"""
        self._queue = queue

    def __getattr__(self, name: str) -> Any:
        """未覆写的方法一律直通真实队列。"""
        return getattr(self._queue, name)

    def record_delivery_retry(self, **kwargs: object) -> None:
        """对照组的关键：退避一个字都不写进库。"""
        del kwargs


class _ReadEventsFailsForOneTask:
    """代理真实队列；指定任务的 ``read_delivery_events`` 每轮都抛普通瞬时错误。

    覆盖"退避以外的零进展"这一类：一个任务每轮抛穿 ``_process_task``，同样是占掉
    一个名额却什么都没做，同样不许一直挡在别人前面。
    """

    def __init__(self, queue: Any, *, failing_task_id: str) -> None:
        """包住真实队列，记下哪个任务的事件读取要一直失败。"""
        self._queue = queue
        self._failing_task_id = failing_task_id

    def __getattr__(self, name: str) -> Any:
        """未覆写的方法一律直通真实队列。"""
        return getattr(self._queue, name)

    def read_delivery_events(self, *, task_id: str, after_sequence: int) -> Any:
        """指定任务永远读不出事件，其余任务直通。"""
        if task_id == self._failing_task_id:
            raise RuntimeError("模拟这个任务的事件读取持续失败")
        return self._queue.read_delivery_events(task_id=task_id, after_sequence=after_sequence)


#: 本文件构造的用例统一用一个**很长**的退避基数，而不是生产默认的 2 秒。理由是防
#: 假红：这些用例要断言"退避窗口内不进候选"，而一轮要处理二十几个任务，机器一忙就
#: 可能在断言之前把 2 秒窗口跑过去，于是变成一条会时序偶发变红的用例。生产默认值
#: 本身另有一条独立断言钉住，不靠这里。
LONG_BACKOFF_BASE_SECONDS = 30.0


class DeliverySchedulingFairnessTests(DeliveryConsumerTestCase):
    """`V-队列-09`：一轮候选里的名额不许被零进展的任务长期占住。"""

    def build_consumer(self, *, queue: Any, cards: Any, texts: Any, **kwargs: Any) -> Any:
        """装一个退避基数很长的消费者；其余参数原样透传。"""
        return DeliveryConsumer(
            queue=queue,
            cards=cards,
            texts=texts,
            retry_backoff_base_seconds=LONG_BACKOFF_BASE_SECONDS,
            **kwargs,
        )

    def test_the_shipped_backoff_defaults_are_what_production_runs(self) -> None:
        """生产默认值单独钉一条：本文件其余用例为了不假红用的是加长基数。"""
        self.assertEqual(DeliveryConsumer.DEFAULT_RETRY_BACKOFF_BASE_SECONDS, 2.0)
        self.assertEqual(DeliveryConsumer.DEFAULT_RETRY_BACKOFF_CAP_SECONDS, 300.0)

    def seed_terminal_task(self, index: int, *, created_seconds_ago: float) -> str:
        """建一个"已经写完终态、等着被投递"的任务，用序号区分不同用户话题。

        ``created_at`` 显式指定：候选查询按它排序，公平性断言必须能精确控制谁在前。
        """
        conversation_id = f"cnv-{index}"
        task_id = f"tsk-{index}"
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
                reply_to_message_id,created_at,content_expires_at)
               VALUES (%s,%s,'usr-1',%s,'问题','running','stable','worker-1',now(),1,
                       %s, now() - %s * interval '1 second', now())""",
            (task_id, conversation_id, f"event-{task_id}", f"reply-{index}", created_seconds_ago),
        )
        self.start_task(task_id)
        self.finish_task(task_id, content=f"给 {conversation_id} 的答案")
        return f"chat-{conversation_id}"

    def delivered_task_ids(self) -> set[str]:
        """库里已经确认送达（终态事件带回执）的任务集合——不看进程内计数。"""
        rows = self.query(
            "SELECT task_id FROM task_delivery_event "
            "WHERE event_type='terminal' AND platform_received_at IS NOT NULL"
        )
        return {row[0] for row in rows}

    def retry_state(self, task_id: str) -> tuple[int, float | None]:
        """读回一个任务的退避档位与"还要等几秒"。``None`` ＝ 没有待还的退避。"""
        row = self.query(
            "SELECT delivery_retry_attempts, "
            "EXTRACT(EPOCH FROM (delivery_retry_after - now())) "
            "FROM task WHERE id = %s",
            (task_id,),
        )[0]
        return int(row[0]), None if row[1] is None else float(row[1])

    def test_backing_off_tasks_do_not_hold_the_whole_batch(self) -> None:
        """整整一批（20 个）持续失败的老任务之后，健康用户下一轮就能被处理。

        修复前：失败任务的 ``created_at`` 最早，每轮都占满 ``ORDER BY created_at
        LIMIT 20`` 的全部名额、每轮零进展，第 21 名健康用户永远进不了候选。
        """

        failing = [self.seed_terminal_task(i, created_seconds_ago=200 - i) for i in range(20)]
        healthy = [self.seed_terminal_task(i, created_seconds_ago=50 - i) for i in range(20, 24)]
        cards = SelectiveCards(rejecting_chats=set(failing))
        texts = SelectiveText(rejecting_chats=set(failing))
        consumer = self.build_consumer(queue=self.queue, cards=cards, texts=texts)

        self.assertEqual(consumer.run_once(), 20, "第一轮的 20 个名额全被最老的失败任务占住")
        self.assertEqual(texts.delivered, [], "这一轮没有任何一条终态真的送出去")
        self.assertEqual(set(texts.attempts), set(failing), "健康任务这一轮根本没被碰过")

        for task_index in range(20):
            attempts, remaining = self.retry_state(f"tsk-{task_index}")
            self.assertEqual(attempts, 1, "一次没成交的尝试记一档退避")
            self.assertIsNotNone(remaining)
            self.assertGreater(remaining, 0, "退避时刻必须落在将来，否则候选查询挡不住它")

        self.assertEqual(consumer.run_once(), 4, "退避中的 20 个让开，健康任务补上名额")
        self.assertEqual(sorted(texts.delivered), [], "健康任务走的是卡片通道，不该有文本兜底")
        self.assertEqual(sorted(cards.created_chats), sorted(healthy))
        self.assertEqual(
            self.delivered_task_ids(),
            {f"tsk-{index}" for index in range(20, 24)},
            "四个健康用户全部拿到答案，而失败任务一个都没有被记成已送达",
        )

    def test_healthy_tasks_keep_flowing_through_failure_recovery_and_restart(self) -> None:
        """混合失败、恢复与重启：健康任务一路通畅，恢复的任务恰好只送达一次。"""

        failing = [self.seed_terminal_task(i, created_seconds_ago=200 - i) for i in range(3)]
        healthy = [self.seed_terminal_task(i, created_seconds_ago=50 - i) for i in range(3, 6)]
        cards = SelectiveCards(rejecting_chats=set(failing))
        texts = SelectiveText(rejecting_chats=set(failing))
        consumer = self.build_consumer(queue=self.queue, cards=cards, texts=texts)

        self.assertEqual(consumer.run_once(), 6)
        self.assertEqual(sorted(cards.created_chats), sorted(healthy), "健康任务同轮照常建卡")
        self.assertEqual(
            self.delivered_task_ids(), {f"tsk-{index}" for index in range(3, 6)}, "健康的先到岸"
        )

        # "重启"：换一个全新的消费者实例。退避不再是进程内字典，因此不会随重启清零。
        restarted = self.build_consumer(queue=self.queue, cards=cards, texts=texts)
        self.assertEqual(restarted.run_once(), 0, "重启不得让退避中的任务立刻涌回候选")
        self.assertEqual(len(texts.attempts), 3, "重启后的这一轮一次外发都不该发生")

        # 故障恢复 + 退避窗口过去：三条失败任务各自补上一条文本终态，且只补一条。
        texts.rejecting_chats.clear()
        for index in range(3):
            self.expire_retry_backoff(f"tsk-{index}")
        self.assertEqual(restarted.run_once(), 3)
        self.assertEqual(sorted(texts.delivered), sorted(failing))

        # 再跑一轮：终态已经落地的任务必须离开候选，更不许再发一条同样的答案。
        self.assertEqual(restarted.run_once(), 0, "已经送达的任务不得回到候选")
        self.assertEqual(
            sorted(texts.attempts),
            sorted(failing * 2),
            "每条失败任务只有『失败一次 + 恢复后成功一次』两次外发，没有重复交付",
        )
        self.assertEqual(len(cards.created_chats), 3, "健康任务也不得被重复建卡")
        self.assertEqual(
            self.delivered_task_ids(),
            {f"tsk-{index}" for index in range(6)},
            "六个用户各自恰好一份答案",
        )

    def test_retry_backoff_grows_and_survives_a_restart(self) -> None:
        """退避档位随失败次数增长，并且跨进程重启保持有效——这是等待时延的来源。"""

        chat = self.seed_terminal_task(0, created_seconds_ago=100)
        cards = SelectiveCards(rejecting_chats={chat})
        texts = SelectiveText(rejecting_chats={chat})

        observed: list[tuple[int, float]] = []
        for _ in range(3):
            # 每一轮都换一个新实例：如果退避还留在进程内存里，换实例就等于清零，
            # 档位不会增长——这条断言因此同时钉住"落库"与"跨重启有效"。
            self.build_consumer(queue=self.queue, cards=cards, texts=texts).run_once()
            attempts, remaining = self.retry_state("tsk-0")
            self.assertIsNotNone(remaining)
            observed.append((attempts, remaining))
            self.expire_retry_backoff("tsk-0")

        self.assertEqual([attempts for attempts, _ in observed], [1, 2, 3], "档位逐次增长")
        ceilings = (
            LONG_BACKOFF_BASE_SECONDS,
            LONG_BACKOFF_BASE_SECONDS * 2,
            LONG_BACKOFF_BASE_SECONDS * 4,
        )
        for ceiling, (_, remaining) in zip(ceilings, observed, strict=True):
            self.assertGreater(remaining, 0.0, "退避时刻必须落在将来")
            self.assertLessEqual(remaining, ceiling, "不得超过这一档的指数退避上限")
        waits = [remaining for _, remaining in observed]
        self.assertLess(waits[0], waits[1], "第二档要比第一档等得久")
        self.assertLess(waits[1], waits[2], "第三档要比第二档等得久")
        self.assertEqual(self.delivered_task_ids(), set(), "一路失败，一次都不许记成已送达")

    def test_a_task_that_raises_every_round_stops_hogging_a_slot(self) -> None:
        """退避以外的零进展也让路：每轮抛穿的任务同样不许一直挡在最前面。"""

        self.seed_terminal_task(0, created_seconds_ago=200)
        healthy = self.seed_terminal_task(1, created_seconds_ago=50)
        cards = SelectiveCards(rejecting_chats=set())
        texts = SelectiveText(rejecting_chats=set())
        queue = _ReadEventsFailsForOneTask(self.queue, failing_task_id="tsk-0")
        consumer = self.build_consumer(queue=queue, cards=cards, texts=texts, limit=1)

        self.assertEqual(consumer.run_once(), 1, "第一轮的唯一名额被抛异常的老任务占住")
        self.assertEqual(cards.created_chats, [], "这一轮什么都没送出去")
        attempts, remaining = self.retry_state("tsk-0")
        self.assertEqual(attempts, 1)
        self.assertIsNotNone(remaining)

        self.assertEqual(consumer.run_once(), 1, "下一轮的名额让给了健康任务")
        self.assertEqual(cards.created_chats, [healthy])
        self.assertEqual(self.delivered_task_ids(), {"tsk-1"})

    def test_raising_the_batch_limit_is_a_control_not_a_fix(self) -> None:
        """**对照实验**：退避不参与候选时，把批量上限从 20 调到 50 并不是修复。

        上限只决定"前多少名"，不改变"失败任务永远排在最前面"这件事；失败任务一旦
        超过新上限，健康用户照样进不来。真正的修复是让退避进得了候选过滤条件。
        """

        failing = [self.seed_terminal_task(i, created_seconds_ago=200 - i) for i in range(25)]
        healthy = [self.seed_terminal_task(i, created_seconds_ago=50 - i) for i in range(25, 27)]
        cards = SelectiveCards(rejecting_chats=set(failing))
        texts = SelectiveText(rejecting_chats=set(failing))
        queue = _NoRetryBookkeepingQueue(self.queue)

        starved = DeliveryConsumer(queue=queue, cards=cards, texts=texts, limit=20)
        for _ in range(3):
            self.assertEqual(starved.run_once(), 20)
        self.assertEqual(cards.created_chats, [], "上限 20：三轮过去，健康用户一次都没轮到")
        self.assertEqual(self.delivered_task_ids(), set())

        # 上限调到 50 之后健康用户确实进来了——但那只是因为 50 > 25。这不是修复：
        # 失败任务再多两打，同一条曲线原样重演。
        widened = DeliveryConsumer(queue=queue, cards=cards, texts=texts, limit=50)
        self.assertEqual(widened.run_once(), 27)
        self.assertEqual(sorted(cards.created_chats), sorted(healthy))
        self.assertEqual(
            self.delivered_task_ids(),
            {f"tsk-{index}" for index in (25, 26)},
            "扩容只在失败任务数小于新上限时看起来有效，因此只作对照，不作修复",
        )
