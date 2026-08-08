"""gateway 的真库断言（需要真实 PostgreSQL）。

认领断言：`V-开通-10`/`V-开通-12`/`V-开通-13`/`V-开通-14`（未开通首聊的
真实事件认领、统一失败出口与重复投递）、`V-接入-01`/`V-接入-02`（事件幂等，顺序与并发）、`V-接入-09`（重复投递的
用户可见面）、`V-接入-11`（任务归属）、`V-队列-01`…`V-队列-05`（事务边界、抢占回滚、
失败语义、并发领取、入队发通知）、`V-会话-01`（同话题串行与持有者释放）、
`V-会话-04`（忙碌期消息不入 task 表）、`V-会话-05`/`V-会话-06`（`/new` 与 `/stop` 的
话题隔离）、`V-会话-08`（排队时长不改变会话归属）、`V-灰度-01`/`V-灰度-02`（版本固化
与按版本领取）、`V-审计-05`（未开通用户内容不落库）。

唯一索引、条件更新、触发器这类断言必须在真库上验证，不用 mock（验证与门禁第五节）。
建库走 `tests/postgres_schema.py` 的整条 alembic 链，与生产同源（#53 之后编号 SQL 已冻结）。
"""

from __future__ import annotations

import os
import threading
import unittest
from datetime import datetime, timedelta, timezone

from gateway_fakes import CallLog, FakeAudit, FakeOnboarding, FakeReactions, FakeReplies
from postgres_schema import ensure_production_schema, reset_production_rows
from lingxi.adapters.postgres_conversation import (
    TASK_QUEUED_CHANNEL,
    PostgresGatewayStore,
    PostgresTaskQueue,
)
from lingxi.core.conversation import EventPipeline, InboundMessage
from lingxi.core.conversation.ports import HandledAs, OnboardingResult, OnboardingState
from lingxi.core.ids import new_id

SKIP_REASON = "跳过：未设置 LINGXI_POSTGRES_DSN，数据库约束类断言未验证（需真实 PostgreSQL）"
NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


def inbound(
    event_id: str,
    *,
    text: str = "本月销售额是多少",
    open_id: str = "ou_1",
    chat_id: str = "oc_1",
    thread_id: str | None = None,
) -> InboundMessage:
    return InboundMessage(
        event_id=event_id,
        event_type="im.message.receive_v1",
        sender_open_id=open_id,
        chat_id=chat_id,
        thread_id=thread_id,
        message_id=f"om_{event_id}",
        text=text,
        trace_id=f"trc_{event_id}",
    )


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN"), SKIP_REASON)
class GatewayPostgresTestCase(unittest.TestCase):
    """所有 gateway 真库用例的共同底座：进程内建一次库，每个用例前只清行。"""

    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        cls._psycopg = psycopg
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]
        # 建库走整条 alembic 链，与生产、与 scripts/ci/check_migration_chain.sh 同源
        # （#53 之后编号 SQL 已冻结，表结构的权威来源是 revision 链）。这里不再自己
        # 拼一份迁移文件清单——清单会过期，而过期的表现是最坏的一种失败：库里根本
        # 没有新 revision 建的对象，用例照样全绿。
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        # 结构不动，只清行；`inbound_event` 自 0057 起是生产表，已在清行范围内。
        reset_production_rows(self._dsn)
        self.store = PostgresGatewayStore(self._dsn)
        self.queue = PostgresTaskQueue(self._dsn)
        self.log = CallLog()
        # 断言辅助方法共用**一条** autocommit 连接。此前每次 query/execute/scalar 都
        # 新建一条连接，一个用例十几次断言就是十几次握手——真库这一段的耗时会被
        # 连接建立本身主导，而门禁整体只有 5 分钟。被测代码自己的连接不受影响
        # （并发用例仍然各连各的，那是它们要验的东西）。
        self._connection = self._psycopg.connect(self._dsn, autocommit=True)
        self.addCleanup(self._connection.close)

    # -- 小工具 ---------------------------------------------------------

    def add_user(self, *, user_id: str = "usr_1", open_id: str = "ou_1", state: str = "active") -> str:
        self.execute(
            """
            INSERT INTO app_user
                (id, feishu_open_id, feishu_user_id, feishu_union_id,
                 display_name, department, tenant_key, provisioning_state)
            VALUES (%s, %s, %s, %s, '张三', '数据部', 'tk_1', %s)
            """,
            (user_id, open_id, f"u_{open_id}", f"un_{open_id}", state),
        )
        return user_id

    def pipeline(self, **kwargs) -> EventPipeline:
        return EventPipeline(
            store=self.store,
            reactions=FakeReactions(self.log),
            replies=FakeReplies(self.log),
            audit=FakeAudit(self.log),
            **kwargs,
        )

    def query(self, sql: str, parameters: tuple = ()) -> list[tuple]:
        with self._connection.cursor() as cursor:
            cursor.execute(sql, parameters)
            return cursor.fetchall()

    def execute(self, sql: str, parameters: tuple = ()) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(sql, parameters)

    def scalar(self, sql: str, parameters: tuple = ()):
        rows = self.query(sql, parameters)
        return rows[0][0] if rows else None

    def task_count(self) -> int:
        return self.scalar("SELECT count(*) FROM task")


class EventIdempotencyTests(GatewayPostgresTestCase):
    """`V-接入-01` / `V-接入-02` / `V-接入-09`。"""

    def setUp(self) -> None:
        super().setUp()
        self.add_user()

    def test_sequential_duplicate_produces_one_result_and_one_task(self) -> None:
        pipeline = self.pipeline()
        first = pipeline.handle_message(inbound("evt_dup"), now=NOW)
        second = pipeline.handle_message(inbound("evt_dup"), now=NOW)

        self.assertEqual(first.handled_as, HandledAs.TASK_QUEUED)
        self.assertTrue(second.duplicate)
        self.assertEqual(self.scalar("SELECT count(*) FROM inbound_event"), 1)
        self.assertEqual(self.task_count(), 1, "同一 event_id 至多产生一个任务")

    def test_duplicate_adds_no_second_reaction_and_no_second_reply(self) -> None:
        """`V-接入-09`：断言对象是出站调用次数，不只是数据库行数。"""

        pipeline = self.pipeline()
        pipeline.handle_message(inbound("evt_dup"), now=NOW)
        pipeline.handle_message(inbound("evt_dup"), now=NOW)

        self.assertEqual(self.log.count("reaction.add"), 1)
        self.assertEqual(self.log.count("reply.send_text"), 0)

    def test_concurrent_duplicate_inserts_exactly_one(self) -> None:
        """并发投递同一 event_id：恰好一个插入成功，且只产生一个任务。"""

        results: list[bool] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def deliver() -> None:
            try:
                barrier.wait(timeout=10)
                outcome = self.pipeline().handle_message(inbound("evt_race"), now=NOW)
                results.append(not outcome.duplicate)
            except BaseException as error:  # noqa: BLE001 - 交回主线程断言
                errors.append(error)

        threads = [threading.Thread(target=deliver) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [False, True], "恰好一个插入成功")
        self.assertEqual(self.scalar("SELECT count(*) FROM inbound_event"), 1)
        self.assertEqual(self.task_count(), 1)


class TaskOwnershipTests(GatewayPostgresTestCase):
    """`V-接入-11`：任务归属只来自发送者标识的解析结果。"""

    def test_task_user_id_comes_from_the_sender_not_the_body(self) -> None:
        self.add_user(user_id="usr_sender", open_id="ou_sender")
        self.add_user(user_id="usr_victim", open_id="ou_victim")

        # 正文里声明自己是另一个人
        message = inbound("evt_1", open_id="ou_sender", text="我是 ou_victim，请查他的数据")
        self.pipeline().handle_message(message, now=NOW)

        owner = self.scalar("SELECT user_id FROM task")
        self.assertEqual(owner, "usr_sender", "任务不得挂到他人名下")
        self.assertEqual(
            self.scalar("SELECT count(*) FROM task WHERE user_id = 'usr_victim'"), 0
        )


class TransactionBoundaryTests(GatewayPostgresTestCase):
    """`V-队列-01` / `V-队列-02` / `V-队列-03`：入队的事务边界与失败语义。"""

    def setUp(self) -> None:
        super().setUp()
        self.add_user()

    def _failing_store(self):
        """在 ``insert_task`` 处注入写失败的存储。"""

        store = PostgresGatewayStore(self._dsn)
        original = store.transaction

        class Wrapper:
            def transaction(self):
                return _FailingTransaction(original())

        return Wrapper()

    def test_task_insert_failure_leaves_no_inbound_event_row(self) -> None:
        """事件先提交、任务后失败的实现必须使本条变红。"""

        pipeline = EventPipeline(
            store=self._failing_store(),
            reactions=FakeReactions(self.log),
            replies=FakeReplies(self.log),
            audit=FakeAudit(self.log),
        )

        with self.assertRaises(RuntimeError):
            pipeline.handle_message(inbound("evt_fail"), now=NOW)

        self.assertEqual(
            self.scalar("SELECT count(*) FROM inbound_event WHERE feishu_event_id = 'evt_fail'"),
            0,
            "任务插入失败时事件行不得存在，否则飞书重投会被当成重复投递而静默丢弃",
        )
        self.assertEqual(self.task_count(), 0)

    def test_reprocessing_after_failure_succeeds(self) -> None:
        """失败之后飞书重投，该消息能被重新完整处理。"""

        broken = EventPipeline(
            store=self._failing_store(),
            reactions=FakeReactions(self.log),
            replies=FakeReplies(self.log),
            audit=FakeAudit(self.log),
        )
        with self.assertRaises(RuntimeError):
            broken.handle_message(inbound("evt_retry"), now=NOW)

        outcome = self.pipeline().handle_message(inbound("evt_retry"), now=NOW)

        self.assertEqual(outcome.handled_as, HandledAs.TASK_QUEUED)
        self.assertEqual(self.task_count(), 1)

    def test_claim_is_rolled_back_so_the_topic_is_not_stuck_busy(self) -> None:
        """`V-队列-02`：抢占成功但入队失败，下一条消息仍能正常入队。"""

        broken = EventPipeline(
            store=self._failing_store(),
            reactions=FakeReactions(self.log),
            replies=FakeReplies(self.log),
            audit=FakeAudit(self.log),
        )
        with self.assertRaises(RuntimeError):
            broken.handle_message(inbound("evt_a"), now=NOW)

        self.assertIsNone(
            self.scalar("SELECT running_task_id FROM conversation"),
            "抢占必须随事务回滚，否则该话题永久停在「当前任务仍在处理中」",
        )

        outcome = self.pipeline().handle_message(inbound("evt_b"), now=NOW)
        self.assertEqual(outcome.handled_as, HandledAs.TASK_QUEUED)

    def test_failed_enqueue_sends_no_acceptance_reply(self) -> None:
        """`V-队列-03`：入队未成功时用户不收到任何表示已受理的回复。"""

        broken = EventPipeline(
            store=self._failing_store(),
            reactions=FakeReactions(self.log),
            replies=FakeReplies(self.log),
            audit=FakeAudit(self.log),
        )
        with self.assertRaises(RuntimeError):
            broken.handle_message(inbound("evt_fail"), now=NOW)

        self.assertEqual(self.log.count("reply.send_text"), 0)


class _FailingTransaction:
    """把 ``insert_task`` 换成抛异常的事务包装。"""

    def __init__(self, manager) -> None:
        self._manager = manager

    def __enter__(self):
        transaction = self._manager.__enter__()

        class Proxy:
            def __getattr__(self, name):
                return getattr(transaction, name)

            def insert_task(self, **kwargs):
                raise RuntimeError("注入失败：task 插入")

        return Proxy()

    def __exit__(self, *exc_info):
        return self._manager.__exit__(*exc_info)


class TopicSerialisationTests(GatewayPostgresTestCase):
    """`V-会话-01` / `V-会话-04`：同话题串行。"""

    def setUp(self) -> None:
        super().setUp()
        self.add_user()

    def test_second_message_while_busy_gets_the_hint_and_writes_no_task(self) -> None:
        pipeline = self.pipeline()
        pipeline.handle_message(inbound("evt_1"), now=NOW)
        outcome = pipeline.handle_message(inbound("evt_2"), now=NOW)

        self.assertEqual(outcome.handled_as, HandledAs.BUSY_HINT)
        self.assertEqual(self.task_count(), 1, "忙碌期收到的消息不得写入 task 表")
        self.assertEqual(
            self.scalar("SELECT handled_as FROM inbound_event WHERE feishu_event_id='evt_2'"),
            "busy_hint",
        )

    def test_concurrent_claims_exactly_one_wins(self) -> None:
        outcomes: list[str] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)
        # 先把会话行建出来，让两个线程真正竞争同一行的条件更新
        self.pipeline().handle_message(inbound("evt_seed"), now=NOW)
        self.execute("UPDATE conversation SET running_task_id = NULL")
        self.execute("DELETE FROM task")

        def deliver(event_id: str) -> None:
            try:
                barrier.wait(timeout=10)
                outcome = self.pipeline().handle_message(inbound(event_id), now=NOW)
                outcomes.append(outcome.handled_as.value)
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        threads = [
            threading.Thread(target=deliver, args=(f"evt_race_{index}",)) for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertEqual(errors, [])
        self.assertEqual(
            sorted(outcomes), ["busy_hint", "task_queued"], "恰好一个抢占成功"
        )
        self.assertEqual(self.task_count(), 1)

    def test_release_only_by_the_holder(self) -> None:
        """非持有任务的释放影响 0 行。"""

        outcome = self.pipeline().handle_message(inbound("evt_1"), now=NOW)
        conversation_id = self.scalar("SELECT id FROM conversation")

        released = self.queue.finish(
            task_id="tsk_not_the_holder",
            conversation_id=conversation_id,
            status="succeeded",
            worker_id="w1",
        )

        self.assertFalse(released, "非持有者的释放必须影响 0 行")
        self.assertEqual(
            self.scalar("SELECT running_task_id FROM conversation"),
            outcome.task_id,
            "占用不得被别的任务清掉",
        )


class ReplyAfterCommitTests(GatewayPostgresTestCase):
    """回复必须在事务提交之后才发出（codex 二轮 P1-A）。

    在事务里发回复时：回复成功而提交失败 → 回滚 → 平台重投 → 提示重发；更糟的是
    **原任务这时如果已经结束**，这条本应"不生效"的忙碌期消息会在重投时被正常入队
    执行，直接违反合同「不会在当前任务结束后自动提交或自动生效」。
    """

    def setUp(self) -> None:
        super().setUp()
        self.add_user()

    def test_a_failing_reply_still_leaves_the_event_committed(self) -> None:
        """注入回复失败：事件行已提交，重投不重处理、不入队。"""

        class FailingReplies:
            def send_text(self, **kwargs):
                raise RuntimeError("注入回复失败")

        pipeline = EventPipeline(
            store=self.store,
            reactions=FakeReactions(self.log),
            replies=FailingReplies(),
            audit=FakeAudit(self.log),
        )
        pipeline.handle_message(inbound("evt_1"), now=NOW)  # 占用话题
        outcome = pipeline.handle_message(inbound("evt_2"), now=NOW)  # 忙碌 → 回提示

        self.assertEqual(outcome.handled_as, HandledAs.BUSY_HINT)
        self.assertEqual(
            self.scalar(
                "SELECT handled_as FROM inbound_event WHERE feishu_event_id = 'evt_2'"
            ),
            "busy_hint",
            "回复失败不得回滚已确定的处理结论",
        )
        self.assertEqual(self.log.count("audit.reply.failed"), 1, "回复失败要记审计")

        # 现在原任务结束、话题空闲；平台重投 evt_2 —— 幂等必须挡住它。
        task_id = self.scalar("SELECT id FROM task")
        conversation_id = self.scalar("SELECT id FROM conversation")
        self.queue.claim(worker_id="w1", target_worker_version="stable")
        self.queue.finish(
            task_id=task_id,
            conversation_id=conversation_id,
            status="succeeded",
            worker_id="w1",
        )

        redelivered = pipeline.handle_message(inbound("evt_2"), now=NOW)

        self.assertTrue(redelivered.duplicate, "重投必须被幂等挡住")
        self.assertEqual(
            self.task_count(),
            1,
            "忙碌期收到的消息不得在任务结束后被重投入队——合同「不会自动提交或自动生效」",
        )

    def test_no_reply_is_sent_when_the_transaction_fails(self) -> None:
        """注入提交失败：一条回复都不许发出去。"""

        pipeline = EventPipeline(
            store=_FailingCommitStore(self._dsn),
            reactions=FakeReactions(self.log),
            replies=FakeReplies(self.log),
            audit=FakeAudit(self.log),
        )

        with self.assertRaises(RuntimeError):
            pipeline.handle_message(inbound("evt_1"), now=NOW)

        self.assertEqual(
            self.log.count("reply.send_text"), 0, "事务失败时不得有任何回复发出"
        )
        self.assertEqual(
            self.scalar("SELECT count(*) FROM inbound_event"), 0, "事件行应随事务回滚"
        )


class _FailingCommitStore:
    """在事务的最后一步（提交前）抛异常。"""

    def __init__(self, dsn: str) -> None:
        self._inner = PostgresGatewayStore(dsn)

    def transaction(self):
        manager = self._inner.transaction()

        class Wrapper:
            def __enter__(self):
                transaction = manager.__enter__()

                class Proxy:
                    def __getattr__(self, name):
                        return getattr(transaction, name)

                    def mark_handled_as(self, **kwargs):
                        transaction.mark_handled_as(**kwargs)
                        raise RuntimeError("注入失败：提交前")

                return Proxy()

            def __exit__(self, *exc_info):
                return manager.__exit__(*exc_info)

        return Wrapper()


class NewCommandRaceTests(GatewayPostgresTestCase):
    """`/new` 与抢占的竞态（codex 二轮 P2-A）。

    管线读到的忙碌状态是事务开始时的快照；另一条连接可能在这之间抢占成功并已经在跑。
    清空必须带 ``AND running_task_id IS NULL``，否则两个并发请求会同时成功，
    把一个**正在运行**的任务的上下文清掉。
    """

    def setUp(self) -> None:
        super().setUp()
        self.add_user()

    def test_new_cannot_clear_a_topic_that_just_became_busy(self) -> None:
        """确定性版本：先占用，再直接调用清空——必须影响 0 行。"""

        self.pipeline().handle_message(inbound("evt_1"), now=NOW)
        conversation_id = self.scalar("SELECT id FROM conversation")
        self.execute(
            "UPDATE conversation SET agent_session_id = 'ses_1' WHERE id = %s",
            (conversation_id,),
        )

        with self.store.transaction() as transaction:
            cleared = transaction.clear_agent_session(conversation_id=conversation_id)

        self.assertFalse(cleared, "话题忙碌时清空必须影响 0 行")
        self.assertEqual(
            self.scalar("SELECT agent_session_id FROM conversation"),
            "ses_1",
            "正在运行的任务的上下文不得被 /new 清掉",
        )

    def test_concurrent_new_and_question_do_not_both_win(self) -> None:
        """两个连接并发：一条 `/new`、一条普通消息，不得同时成功。"""

        outcomes: dict[str, str] = {}
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)
        # 先把会话行与上下文建出来，两个线程竞争同一行
        self.pipeline().handle_message(inbound("evt_seed"), now=NOW)
        self.execute("UPDATE conversation SET running_task_id = NULL, agent_session_id = 'ses_1'")
        self.execute("DELETE FROM task")

        def deliver(label: str, text: str) -> None:
            try:
                barrier.wait(timeout=10)
                outcome = self.pipeline().handle_message(
                    inbound(f"evt_{label}", text=text), now=NOW
                )
                outcomes[label] = outcome.handled_as.value
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        threads = [
            threading.Thread(target=deliver, args=("new", "/new")),
            threading.Thread(target=deliver, args=("ask", "本月销售额是多少")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertEqual(errors, [])
        # 允许两种交错：/new 先到（清空成功 + 提问入队），或提问先到（抢占成功 +
        # /new 得到忙碌提示）。不允许的是「清空成功且任务也在跑」。
        if outcomes.get("new") == "command":
            self.assertIsNone(
                self.scalar("SELECT agent_session_id FROM conversation"),
                "/new 成功时上下文应已清空",
            )
        else:
            self.assertEqual(outcomes.get("new"), "busy_hint", "/new 未成功时只能是忙碌提示")
            self.assertEqual(
                self.scalar("SELECT agent_session_id FROM conversation"),
                "ses_1",
                "被忙碌拦下的 /new 不得清空上下文",
            )


class ConditionalClaimTests(GatewayPostgresTestCase):
    """抢占的**条件更新本身**，不经过管线的内存预检。

    这一组是变异验证逼出来的。原先 `V-会话-01` 只有一条并发用例，而并发用例可以
    「因为对的结论、错的理由」通过：管线在抢占之前先读了一次 ``running_task_id``，
    两个线程只要错开一点，后到的那个就在**内存预检**里被拦下，根本走不到条件更新。
    于是把条件更新改成无条件更新之后，该用例在 4 个 `PYTHONHASHSEED` 里只有 2 个
    变红——另外 2 个照样绿。

    真正承载同话题串行的是数据库那一句条件更新（内存预检只是省一次往返，两个进程
    之间它什么也保证不了）。下面两条直接打在它身上，与线程调度无关。
    """

    def setUp(self) -> None:
        super().setUp()
        self.add_user()
        with self.store.transaction() as transaction:
            conversation = transaction.ensure_conversation(
                user_id="usr_1", chat_id="oc_1", thread_id=None
            )
        self.conversation_id = conversation.conversation_id

    def test_claiming_a_busy_topic_fails_and_does_not_overwrite(self) -> None:
        with self.store.transaction() as transaction:
            self.assertTrue(
                transaction.claim_conversation(
                    conversation_id=self.conversation_id, task_id="tsk_first"
                )
            )

        with self.store.transaction() as transaction:
            self.assertFalse(
                transaction.claim_conversation(
                    conversation_id=self.conversation_id, task_id="tsk_second"
                ),
                "话题已被占用时抢占必须失败——无条件更新会让第二个任务直接覆盖第一个",
            )

        self.assertEqual(
            self.scalar("SELECT running_task_id FROM conversation"),
            "tsk_first",
            "占用不得被后到的任务覆盖（PR #12 的原始错误）",
        )

    def test_claim_succeeds_again_after_the_holder_releases(self) -> None:
        with self.store.transaction() as transaction:
            transaction.claim_conversation(
                conversation_id=self.conversation_id, task_id="tsk_first"
            )
        self.execute(
            "UPDATE conversation SET running_task_id = NULL WHERE id = %s",
            (self.conversation_id,),
        )

        with self.store.transaction() as transaction:
            self.assertTrue(
                transaction.claim_conversation(
                    conversation_id=self.conversation_id, task_id="tsk_second"
                ),
                "释放之后必须能重新抢占，否则话题会永久忙碌",
            )


class BusyTopicWritesNoTaskTests(GatewayPostgresTestCase):
    """`V-会话-04` 的数据库面：忙碌话题上第二个任务写不进去。

    与上一组同因——管线的内存预检会先拦下忙碌消息，使得「忙碌期照常写 task」这个
    变异在端到端用例里抓不到。这里绕开预检，直接让第二次入队走到抢占。
    """

    def setUp(self) -> None:
        super().setUp()
        self.add_user()

    def test_enqueue_on_a_busy_topic_writes_no_second_task(self) -> None:
        self.pipeline().handle_message(inbound("evt_1"), now=NOW)
        self.assertEqual(self.task_count(), 1)
        conversation_id = self.scalar("SELECT id FROM conversation")

        # 直接尝试在已占用的话题上再抢一次并入队：抢占必须失败，任务写不进去。
        with self.store.transaction() as transaction:
            claimed = transaction.claim_conversation(
                conversation_id=conversation_id, task_id="tsk_intruder"
            )
            self.assertFalse(claimed, "忙碌话题不得被再次抢占")
            if claimed:  # pragma: no cover - 只有实现被改坏时才会走到
                transaction.insert_task(
                    task_id="tsk_intruder",
                    conversation_id=conversation_id,
                    user_id="usr_1",
                    inbound_event_id="evt_intruder",
                    prompt="不该被写进去",
                    resumed_session=False,
                    target_worker_version="stable",
                )

        self.assertEqual(self.task_count(), 1, "忙碌期不得产生第二个任务")


class ZombieWorkerTests(GatewayPostgresTestCase):
    """僵尸 worker 不得打破同话题串行（独立复查 F3 实测出的漏洞）。

    场景：w1 领了任务 → 心跳超时被 scheduler 回收 → w2 重新领取并正在执行 →
    w1 这时才醒过来调用收口。任务标识在回收重排前后**没有变**，所以只判
    「running_task_id == task_id」的实现会认可 w1 的收口、把话题释放掉，而 w2 还在跑。
    下一条消息随即抢占成功，与 w2 并行——同话题同时执行两个任务。
    """

    def setUp(self) -> None:
        super().setUp()
        self.add_user()

    def test_a_reclaimed_task_ignores_the_previous_workers_finish(self) -> None:
        self.pipeline().handle_message(inbound("evt_1"), now=NOW)
        conversation_id = self.scalar("SELECT id FROM conversation")
        task_id = self.scalar("SELECT id FROM task")

        self.assertEqual(
            [task.task_id for task in self.queue.claim(worker_id="w1", target_worker_version="stable")],
            [task_id],
        )
        self.execute(
            "UPDATE task SET heartbeat_at = now() - interval '1 hour' WHERE id = %s", (task_id,)
        )
        self.assertEqual(self.queue.reclaim_stale(older_than=timedelta(minutes=5)), [task_id])
        self.assertEqual(
            [task.task_id for task in self.queue.claim(worker_id="w2", target_worker_version="stable")],
            [task_id],
        )

        released = self.queue.finish(
            task_id=task_id,
            conversation_id=conversation_id,
            status="succeeded",
            worker_id="w1",
        )

        self.assertFalse(released, "上一代执行者的收口必须被拒绝")
        self.assertEqual(
            self.scalar("SELECT running_task_id FROM conversation"),
            task_id,
            "话题不得被僵尸 worker 释放——w2 还在执行这个任务",
        )
        self.assertEqual(
            self.scalar("SELECT status FROM task"), "running", "任务状态不得被僵尸 worker 改写"
        )
        self.assertEqual(self.scalar("SELECT worker_id FROM task"), "w2")

    def test_a_late_second_finish_cannot_rewrite_a_finished_task(self) -> None:
        """已收口的任务不得被同一 worker 的迟到二次收口改写状态。

        用户按了 `/stop`、任务以 ``stopped`` 收口之后，worker 的重试或信号竞态再调一次
        ``finish(succeeded)``，记录会从"用户已停止"翻成"已成功"——失败语义被改写。
        （验收方备好的回归用例，已双向验证。）
        """

        self.pipeline().handle_message(inbound("evt_1"), now=NOW)
        conversation_id = self.scalar("SELECT id FROM conversation")
        task_id = self.scalar("SELECT id FROM task")
        self.queue.claim(worker_id="w1", target_worker_version="stable")

        self.assertTrue(
            self.queue.finish(
                task_id=task_id,
                conversation_id=conversation_id,
                status="stopped",
                worker_id="w1",
            )
        )
        self.assertEqual(self.scalar("SELECT status FROM task"), "stopped")

        self.queue.finish(
            task_id=task_id,
            conversation_id=conversation_id,
            status="succeeded",
            worker_id="w1",
        )

        self.assertEqual(
            self.scalar("SELECT status FROM task"),
            "stopped",
            "已结束任务被二次收口改写：用户按下的 /stop 被记成 succeeded",
        )

    def test_the_current_worker_can_still_finish(self) -> None:
        self.pipeline().handle_message(inbound("evt_1"), now=NOW)
        conversation_id = self.scalar("SELECT id FROM conversation")
        task_id = self.scalar("SELECT id FROM task")
        self.queue.claim(worker_id="w1", target_worker_version="stable")

        released = self.queue.finish(
            task_id=task_id,
            conversation_id=conversation_id,
            status="succeeded",
            worker_id="w1",
        )

        self.assertTrue(released)
        self.assertIsNone(self.scalar("SELECT running_task_id FROM conversation"))
        self.assertIsNotNone(
            self.scalar("SELECT last_task_ended_at FROM conversation"),
            "释放时必须落 last_task_ended_at——它是两小时规则的唯一依据",
        )


class CommandIsolationTests(GatewayPostgresTestCase):
    """`V-会话-05` / `V-会话-06`：`/new` 与 `/stop` 的话题隔离。"""

    def setUp(self) -> None:
        super().setUp()
        self.add_user()

    def _two_topics(self) -> tuple[str, str]:
        pipeline = self.pipeline()
        pipeline.handle_message(inbound("evt_a", thread_id="omt_A"), now=NOW)
        pipeline.handle_message(inbound("evt_b", thread_id="omt_B"), now=NOW)
        rows = self.query(
            "SELECT feishu_thread_id, id FROM conversation ORDER BY feishu_thread_id"
        )
        return rows[0][1], rows[1][1]

    def test_new_clears_only_its_own_topic(self) -> None:
        topic_a, topic_b = self._two_topics()
        ended = NOW - timedelta(minutes=10)
        self.execute(
            "UPDATE conversation SET agent_session_id = 'ses_' || id,"
            " last_task_ended_at = %s, running_task_id = NULL",
            (ended,),
        )

        self.pipeline().handle_message(
            inbound("evt_new", thread_id="omt_A", text="/new"), now=NOW
        )

        self.assertIsNone(
            self.scalar("SELECT agent_session_id FROM conversation WHERE id = %s", (topic_a,))
        )
        self.assertEqual(
            self.scalar("SELECT agent_session_id FROM conversation WHERE id = %s", (topic_b,)),
            f"ses_{topic_b}",
            "话题 B 的会话不得被 /new 影响",
        )
        self.assertEqual(
            self.scalar("SELECT last_task_ended_at FROM conversation WHERE id = %s", (topic_b,)),
            ended,
            "话题 B 的 last_task_ended_at 不得被 /new 影响",
        )

    def test_stop_marks_only_its_own_running_task(self) -> None:
        topic_a, topic_b = self._two_topics()
        # 记下话题 B 抢占中的任务标识，稍后逐字比对。断「不是 NULL」太弱：
        # 把 B 的占用换成另一个任务同样满足它。
        running_before = self.scalar(
            "SELECT running_task_id FROM conversation WHERE id = %s", (topic_b,)
        )
        self.assertIsNotNone(running_before)

        self.pipeline().handle_message(
            inbound("evt_stop", thread_id="omt_A", text="/stop"), now=NOW
        )

        stopped = self.query(
            "SELECT conversation_id, stop_requested FROM task ORDER BY conversation_id"
        )
        by_topic = dict(stopped)
        self.assertTrue(by_topic[topic_a], "话题 A 的运行中任务应被置 stop_requested")
        self.assertFalse(by_topic[topic_b], "话题 B 的运行中任务不得受影响")
        self.assertEqual(
            self.scalar("SELECT running_task_id FROM conversation WHERE id = %s", (topic_b,)),
            running_before,
            "话题 B 的 running_task_id 必须逐字不变",
        )


class SessionResumeTests(GatewayPostgresTestCase):
    """`V-会话-08`：排队时长不改变会话归属。"""

    def setUp(self) -> None:
        super().setUp()
        self.add_user()

    def test_resumed_flag_survives_three_hours_in_the_queue(self) -> None:
        pipeline = self.pipeline()
        pipeline.handle_message(inbound("evt_1"), now=NOW)
        conversation_id = self.scalar("SELECT id FROM conversation")
        task_id = self.scalar("SELECT id FROM task")
        claimed = self.queue.claim(worker_id="w0", target_worker_version="stable")
        self.assertEqual([task.task_id for task in claimed], [task_id])
        self.queue.finish(
            task_id=task_id,
            conversation_id=conversation_id,
            status="succeeded",
            worker_id="w0",
        )
        self.execute(
            "UPDATE conversation SET agent_session_id = 'ses_1', last_task_ended_at = %s",
            (NOW,),
        )

        # 发起时距上次结束 30 分钟 → 应续用
        outcome = pipeline.handle_message(inbound("evt_2"), now=NOW + timedelta(minutes=30))
        self.assertTrue(outcome.resumed_session)

        # 在队列里躺三小时后才被领取，判定不得改变
        self.execute(
            "UPDATE task SET scheduled_at = now() - interval '3 hours' WHERE id = %s",
            (outcome.task_id,),
        )
        claimed = self.queue.claim(worker_id="w1", target_worker_version="stable")
        picked = [task for task in claimed if task.task_id == outcome.task_id]

        self.assertEqual(len(picked), 1)
        self.assertTrue(picked[0].resumed_session, "排队时长不得改变会话归属")

    def test_resumed_session_cannot_be_rewritten(self) -> None:
        self.pipeline().handle_message(inbound("evt_1"), now=NOW)
        task_id = self.scalar("SELECT id FROM task")

        with self.assertRaises(self._psycopg.errors.RaiseException):
            self.execute("UPDATE task SET resumed_session = TRUE WHERE id = %s", (task_id,))


class QueueClaimTests(GatewayPostgresTestCase):
    """`V-队列-04` / `V-队列-05`：并发领取、唤醒与兜底轮询。"""

    def setUp(self) -> None:
        super().setUp()
        self.add_user()

    def _queue_tasks(self, count: int) -> list[str]:
        task_ids = []
        for index in range(count):
            conversation_id = new_id("cnv")
            self.execute(
                "INSERT INTO conversation (id, user_id, feishu_chat_id, feishu_thread_id)"
                " VALUES (%s, 'usr_1', 'oc_1', %s)",
                (conversation_id, f"omt_{index}"),
            )
            task_id = new_id("tsk")
            self.execute(
                "INSERT INTO task (id, conversation_id, user_id, inbound_event_id, prompt,"
                " target_worker_version, content_expires_at)"
                " VALUES (%s, %s, 'usr_1', %s, 'q', 'stable', now())",
                (task_id, conversation_id, f"evt_{index}"),
            )
            task_ids.append(task_id)
        return task_ids

    def test_two_workers_never_claim_the_same_task(self) -> None:
        self._queue_tasks(20)
        claimed: dict[str, list[str]] = {"w1": [], "w2": []}
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def worker(worker_id: str) -> None:
            try:
                barrier.wait(timeout=10)
                for _ in range(20):
                    batch = self.queue.claim(
                        worker_id=worker_id, target_worker_version="stable", limit=3
                    )
                    if not batch:
                        break
                    claimed[worker_id].extend(task.task_id for task in batch)
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(name,)) for name in ("w1", "w2")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [])
        everything = claimed["w1"] + claimed["w2"]
        self.assertEqual(len(everything), 20, "每个任务都要被领到，不得饿死")
        self.assertEqual(len(set(everything)), 20, "同一任务不得被两个 worker 领到")
        self.assertEqual(
            self.scalar("SELECT count(*) FROM task WHERE status = 'running'"), 20
        )
        # 断「每个任务的 worker_id 与实际领到它的那个 worker 一致」，而不是
        # 「一共出现过两个 worker」——后者在两个 worker 互相覆盖对方的任务时也成立。
        recorded = dict(self.query("SELECT id, worker_id FROM task"))
        for worker_id, task_ids in claimed.items():
            for task_id in task_ids:
                self.assertEqual(
                    recorded[task_id],
                    worker_id,
                    f"任务 {task_id} 由 {worker_id} 领到，库里却记着 {recorded[task_id]}",
                )
        self.assertEqual(
            len({worker for worker in recorded.values()}),
            2,
            "两个 worker 都应领到过任务，否则不构成并发",
        )

    def test_notify_is_emitted_on_commit(self) -> None:
        """入队提交后发出 NOTIFY，监听方收到即领取。"""

        listener = self._psycopg.connect(self._dsn, autocommit=True)
        try:
            listener.execute(f"LISTEN {TASK_QUEUED_CHANNEL}")
            self.pipeline().handle_message(inbound("evt_1"), now=NOW)
            notifies = []
            for notify in listener.notifies(timeout=5, stop_after=1):
                notifies.append(notify)
            self.assertEqual(len(notifies), 1)
            self.assertEqual(notifies[0].channel, TASK_QUEUED_CHANNEL)
        finally:
            listener.close()

    # 原先这里有一条 `test_task_is_still_claimable_when_every_notify_is_dropped`：
    # 它只是入队一条任务然后直接 claim，压根没有 NOTIFY 参与——无论兜底轮询是否
    # 存在都会绿，是重言式。真正的「监听方收到即领取 + 丢弃全部 NOTIFY 后兜底轮询
    # 仍在配置上限内领到」需要一个消费者，属 S4 下半，已在验证与门禁拆成 `V-队列-06`
    # 并标为未认领。这里不留一条假装覆盖了它的用例。

    def test_notify_is_not_emitted_when_the_transaction_rolls_back(self) -> None:
        listener = self._psycopg.connect(self._dsn, autocommit=True)
        try:
            listener.execute(f"LISTEN {TASK_QUEUED_CHANNEL}")
            broken = EventPipeline(
                store=_RollbackStore(self._dsn),
                reactions=FakeReactions(self.log),
                replies=FakeReplies(self.log),
                audit=FakeAudit(self.log),
            )
            with self.assertRaises(RuntimeError):
                broken.handle_message(inbound("evt_fail"), now=NOW)
            notifies = list(listener.notifies(timeout=1))
            self.assertEqual(notifies, [], "回滚的事务不得发出 NOTIFY")
        finally:
            listener.close()


class _RollbackStore:
    """在 ``notify_task_queued`` 之后抛异常，用于验证 NOTIFY 随事务回滚。"""

    def __init__(self, dsn: str) -> None:
        self._inner = PostgresGatewayStore(dsn)

    def transaction(self):
        manager = self._inner.transaction()

        class Wrapper:
            def __enter__(self):
                transaction = manager.__enter__()

                class Proxy:
                    def __getattr__(self, name):
                        return getattr(transaction, name)

                    def notify_task_queued(self):
                        transaction.notify_task_queued()
                        raise RuntimeError("注入失败：提交前")

                return Proxy()

            def __exit__(self, *exc_info):
                return manager.__exit__(*exc_info)

        return Wrapper()


class WorkerVersionTests(GatewayPostgresTestCase):
    """`V-灰度-01` / `V-灰度-02`：版本固化与按版本领取。"""

    def setUp(self) -> None:
        super().setUp()
        self.add_user()

    def test_version_is_written_at_enqueue(self) -> None:
        outcome = self.pipeline().handle_message(inbound("evt_1"), now=NOW)

        self.assertEqual(outcome.target_worker_version, "stable")
        self.assertEqual(self.scalar("SELECT target_worker_version FROM task"), "stable")

    def test_version_survives_retry_and_reclaim(self) -> None:
        """重试、心跳超时回收都不得改写目标版本。"""

        self.pipeline().handle_message(inbound("evt_1"), now=NOW)
        task_id = self.scalar("SELECT id FROM task")

        claimed = self.queue.claim(worker_id="w1", target_worker_version="stable")
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].attempts, 1)

        # 心跳超时 → scheduler 回收重置为 queued
        self.execute(
            "UPDATE task SET heartbeat_at = now() - interval '1 hour' WHERE id = %s", (task_id,)
        )
        reclaimed = self.queue.reclaim_stale(older_than=timedelta(minutes=5))
        self.assertEqual(reclaimed, [task_id])

        # 再次领取 → attempts 增加，版本不变
        again = self.queue.claim(worker_id="w2", target_worker_version="stable")
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0].attempts, 2)
        self.assertEqual(
            self.scalar("SELECT target_worker_version FROM task"),
            "stable",
            "重试与回收都不得改写目标 worker 版本",
        )

    def test_database_refuses_to_rewrite_the_version(self) -> None:
        """任何在领取或重试路径写该字段的实现必须使本条变红。"""

        self.pipeline().handle_message(inbound("evt_1"), now=NOW)
        task_id = self.scalar("SELECT id FROM task")

        with self.assertRaises(self._psycopg.errors.RaiseException):
            self.execute(
                "UPDATE task SET target_worker_version = 'canary' WHERE id = %s", (task_id,)
            )

    def test_no_claim_path_writes_the_version_column_at_all(self) -> None:
        """`V-灰度-01` 说的是「任何在领取或重试路径**写**该字段的实现都要变红」。

        数据库触发器只挡得住"写了个不同的值"（``IS DISTINCT FROM``）；把
        ``target_worker_version = target_worker_version`` 写进 SET 子句，触发器与
        任何值比较都发现不了，而那正是分流规则接上之后最容易出现的写法。
        这里直接断 SET 子句里没有这一列——判据落在代码本身，不依赖运行时取值。
        """

        import inspect
        import re

        from lingxi.adapters.postgres_conversation import PostgresTaskQueue

        for method in (PostgresTaskQueue.claim, PostgresTaskQueue.reclaim_stale):
            source = inspect.getsource(method)
            set_clauses = re.findall(
                r"UPDATE\s+task\s+SET\s+(.*?)\s+WHERE", source, re.IGNORECASE | re.DOTALL
            )
            self.assertTrue(set_clauses, f"{method.__name__} 里没找到 UPDATE task SET")
            for clause in set_clauses:
                self.assertNotIn(
                    "target_worker_version",
                    clause,
                    f"{method.__name__} 的 SET 子句写了目标 worker 版本——"
                    "入队时固化的版本在领取与回收路径上一个字都不能写",
                )

    def test_claim_is_filtered_by_version(self) -> None:
        """声明 stable 的 worker 领不到 canary 任务，反之亦然。"""

        canary_pipeline = self.pipeline(
            resolve_version=lambda *, user_id, now: "canary"
        )
        canary_pipeline.handle_message(inbound("evt_canary", thread_id="omt_c"), now=NOW)
        self.pipeline().handle_message(inbound("evt_stable", thread_id="omt_s"), now=NOW)

        stable_batch = self.queue.claim(worker_id="w_stable", target_worker_version="stable")
        canary_batch = self.queue.claim(worker_id="w_canary", target_worker_version="canary")

        self.assertEqual([task.target_worker_version for task in stable_batch], ["stable"])
        self.assertEqual([task.target_worker_version for task in canary_batch], ["canary"])

    def test_unmatched_tasks_stay_queued_and_untouched(self) -> None:
        canary_pipeline = self.pipeline(resolve_version=lambda *, user_id, now: "canary")
        canary_pipeline.handle_message(inbound("evt_canary"), now=NOW)

        claimed = self.queue.claim(worker_id="w_stable", target_worker_version="stable")

        self.assertEqual(claimed, [])
        status, worker_id, attempts = self.query(
            "SELECT status, worker_id, attempts FROM task"
        )[0]
        self.assertEqual(status, "queued", "不匹配的任务必须保持 queued")
        self.assertIsNone(worker_id)
        self.assertEqual(attempts, 0, "不匹配的任务不得被误改状态")

    def test_two_versions_claim_concurrently_without_crossing(self) -> None:
        for index in range(6):
            version = "canary" if index % 2 else "stable"
            pipeline = self.pipeline(resolve_version=lambda *, user_id, now, v=version: v)
            pipeline.handle_message(inbound(f"evt_{index}", thread_id=f"omt_{index}"), now=NOW)

        got: dict[str, list[str]] = {"stable": [], "canary": []}
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def worker(version: str) -> None:
            try:
                barrier.wait(timeout=10)
                batch = self.queue.claim(
                    worker_id=f"w_{version}", target_worker_version=version, limit=10
                )
                got[version].extend(task.target_worker_version for task in batch)
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(name,)) for name in ("stable", "canary")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(got["stable"], ["stable"] * 3)
        self.assertEqual(got["canary"], ["canary"] * 3)


class UnprovisionedUserTests(GatewayPostgresTestCase):
    """`V-开通-10`/`V-审计-05` 的 gateway 真库面。"""

    def test_unprovisioned_message_writes_only_the_event_row(self) -> None:
        outcome = self.pipeline().handle_message(
            inbound("evt_1", open_id="ou_stranger", text="敏感业务问题"), now=NOW
        )

        self.assertEqual(outcome.handled_as, HandledAs.NOT_PROVISIONED)
        self.assertEqual(self.task_count(), 0, "未开通用户的消息不得产生任务")
        self.assertEqual(self.scalar("SELECT count(*) FROM conversation"), 0)
        self.assertEqual(
            self.scalar("SELECT handled_as FROM inbound_event"), "not_provisioned"
        )
        # inbound_event 只记录收到过一个事件及处理方式，不含消息正文
        columns = [
            row[0]
            for row in self.query(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'inbound_event'"
            )
        ]
        self.assertNotIn("content", columns)
        self.assertNotIn("prompt", columns)

    def test_wired_first_message_claims_auto_onboarding_once(self) -> None:
        runner = FakeOnboarding(result=OnboardingResult(state=OnboardingState.NOT_AUTHORIZED))
        pipeline = self.pipeline(onboarding=runner)

        first = pipeline.handle_message(
            inbound("evt_auto", open_id="ou_stranger", text="不要保存这段正文"), now=NOW
        )
        second = pipeline.handle_message(
            inbound("evt_auto", open_id="ou_stranger", text="不要保存这段正文"), now=NOW
        )

        self.assertEqual(first.handled_as, HandledAs.AUTO_PROVISIONING)
        self.assertTrue(second.duplicate)
        self.assertEqual(runner.calls, [{
            "event_id": "evt_auto",
            "open_id": "ou_stranger",
            "trace_id": "trc_evt_auto",
        }])
        self.assertEqual(self.scalar("SELECT count(*) FROM inbound_event"), 1)
        self.assertEqual(
            self.scalar("SELECT handled_as FROM inbound_event"), "auto_provisioning"
        )
        self.assertEqual(self.task_count(), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM conversation"), 0)
        self.assertEqual(
            [fields["content_key"] for fields in self.log.fields("audit.reply.sent")],
            ["onboarding.checking", "onboarding.not_authorized"],
        )

    def test_concurrent_duplicate_first_messages_start_one_runner(self) -> None:
        runner = FakeOnboarding(result=OnboardingResult(state=OnboardingState.SYNC_TIMEOUT))
        barrier = threading.Barrier(2)
        results: list[bool] = []
        errors: list[BaseException] = []

        def deliver() -> None:
            try:
                barrier.wait(timeout=10)
                outcome = self.pipeline(onboarding=runner).handle_message(
                    inbound("evt_auto_race", open_id="ou_stranger"), now=NOW
                )
                results.append(outcome.duplicate)
            except BaseException as error:  # noqa: BLE001 - 交回主线程断言
                errors.append(error)

        threads = [threading.Thread(target=deliver) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(runner.calls), 1, "并发重复不得启动两个开通编排")
        self.assertEqual(self.scalar("SELECT count(*) FROM inbound_event"), 1)
        self.assertEqual(self.task_count(), 0)
        self.assertEqual(
            [fields["content_key"] for fields in self.log.fields("audit.reply.sent")],
            ["onboarding.checking", "onboarding.sync_timeout"],
        )


class RetentionTests(GatewayPostgresTestCase):
    """入站事件的九十天保留期由来源时间推导，写入方不能延长。"""

    def test_expires_at_is_forced_to_ninety_days(self) -> None:
        self.add_user()
        self.pipeline().handle_message(inbound("evt_1"), now=NOW)

        delta = self.scalar("SELECT expires_at - received_at FROM inbound_event")
        self.assertEqual(delta, timedelta(days=90))

    def test_writer_cannot_postpone_expiry(self) -> None:
        self.add_user()
        self.pipeline().handle_message(inbound("evt_1"), now=NOW)
        self.execute(
            "UPDATE inbound_event SET expires_at = now() + interval '10 years'"
        )

        delta = self.scalar("SELECT expires_at - received_at FROM inbound_event")
        self.assertEqual(delta, timedelta(days=90), "保留期不得被写入方悄悄延长")


if __name__ == "__main__":
    unittest.main()
