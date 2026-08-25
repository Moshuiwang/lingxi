"""Issue #151：投递事件 outbox 的真库断言。

覆盖 V-投递-01/02/04/05/06/10：严格递增序号与幂等重试、终态写入与话题占用同事务、
业务终态与投递终态分离（`resolve_delivered_outcome` 不因投递结果改写业务结论）、
会话保留阶段的正文清理（`/new`、空闲到点、按用户清理）、二十四小时到期强制收敛。

V-投递-03（真实飞书权威接口确认）、V-投递-07（备份恢复）、V-投递-08（结果依赖型
追问的读侧集成）、V-投递-09（L4a 真实回读）不在本文件断言范围——它们分别需要
Gateway 消费循环（#152）、受控灾备演练或真实外部系统，均不是本 Story 的完成标准。

唯一索引、CHECK 约束、触发器必须在真库上验证，不用 mock（验证与门禁第五节）；
每条 "不得 / 不允许" 规则都配一条主动尝试违规的否定用例（验证与门禁第八节）。
"""

from __future__ import annotations

import asyncio
import os
import threading
import unittest
from datetime import datetime, timedelta, timezone

from postgres_schema import ensure_production_schema, reset_production_rows

from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_conversation import (
    AppendedEvent,
    PostgresGatewayStore,
    PostgresTaskQueue,
)
from lingxi.apps.worker.config import WorkerConfig
from lingxi.apps.worker.service import WorkerService

SKIP_REASON = "跳过：未设置 LINGXI_POSTGRES_DSN，投递 outbox 的数据库约束类断言未验证"


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN"), SKIP_REASON)
class DeliveryOutboxTestCase(unittest.TestCase):
    """本文件全部真库用例的共同底座：进程内建一次库，每个用例前只清行。"""

    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        cls._psycopg = psycopg
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        reset_production_rows(self._dsn)
        self.queue = PostgresTaskQueue(self._dsn)
        self.store = PostgresGatewayStore(self._dsn)
        self._connection = self._psycopg.connect(self._dsn, autocommit=True)
        self.addCleanup(self._connection.close)
        self.execute(
            """INSERT INTO app_user
               (id, feishu_open_id, feishu_user_id, feishu_union_id,
                display_name, department, tenant_key, provisioning_state)
               VALUES ('usr-1','ou-1','u-1','un-1','张三','数据部','tk-1','active')"""
        )
        self.execute(
            """INSERT INTO app_user
               (id, feishu_open_id, feishu_user_id, feishu_union_id,
                display_name, department, tenant_key, provisioning_state)
               VALUES ('usr-2','ou-2','u-2','un-2','李四','销售部','tk-2','active')"""
        )

    # -- 小工具 -----------------------------------------------------------

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

    def seed_running_task(
        self,
        *,
        task_id: str,
        conversation_id: str,
        user_id: str = "usr-1",
        worker_id: str = "worker-1",
        status: str = "running",
    ) -> None:
        """直接建一条处于指定状态的任务，跳过完整入队/领取流程（与
        ``test_worker_queue_consumer.py`` 的 ``_insert_old_task`` 同一手法）。

        ``conversation`` 用 ``ON CONFLICT DO UPDATE`` 是为了让同一个会话下追加
        第二个任务（多轮场景的测试夹具）时可以复用同一行，而不是要求调用方先手工
        判断这个会话是否已经建过。
        """

        self.execute(
            """INSERT INTO conversation (id,user_id,feishu_chat_id,feishu_thread_id,running_task_id)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (id) DO UPDATE SET running_task_id = EXCLUDED.running_task_id""",
            (
                conversation_id,
                user_id,
                f"chat-{conversation_id}",
                f"topic-{conversation_id}",
                task_id if status in ("running", "awaiting_delivery") else None,
            ),
        )
        self.execute(
            """INSERT INTO task
               (id,conversation_id,user_id,inbound_event_id,prompt,status,
                target_worker_version,worker_id,heartbeat_at,attempts,content_expires_at)
               VALUES (%s,%s,%s,%s,'问题',%s,'stable',%s,now(),1,now())""",
            (task_id, conversation_id, user_id, f"event-{task_id}", status, worker_id),
        )

    def seed_terminal_event(
        self,
        *,
        task_id: str,
        sequence: int = 1,
        terminal_kind: str = "success",
        error_kind: str | None = None,
        content: str | None = "已送达的答案",
        worker_id: str = "worker-1",
        created_at_sql: str = "now()",
        platform_received: bool = False,
    ) -> None:
        """绕过 ``write_terminal_event`` 直接插入一条 terminal 行，供到期/清理类
        用例控制 ``created_at``（``write_terminal_event`` 的 created_at 由触发器
        固定为 now()，测试到期行为必须能背景回填）。``created_at_sql`` 与
        ``platform_received`` 都是受控的内部字面量，不是绑定参数——它们要写的是
        ``now()``/区间表达式，直接拼进 SQL 比强行绑定一个字符串时间戳更贴合触发器
        的真实调用方式。"""

        platform_received_sql = "now()" if platform_received else "NULL"
        self.execute(
            f"""
            INSERT INTO task_delivery_event
                (id, task_id, sequence, event_type, terminal_kind, error_kind,
                 content, worker_id, idempotency_key, created_at, platform_received_at)
            VALUES (%s, %s, %s, 'terminal', %s, %s, %s, %s, %s, {created_at_sql}, {platform_received_sql})
            """,
            (
                f"tde-{task_id}-{sequence}",
                task_id,
                sequence,
                terminal_kind,
                error_kind,
                content,
                worker_id,
                f"{task_id}:terminal",
            ),
        )


class SequenceAndIdempotencyTests(DeliveryOutboxTestCase):
    """V-投递-01：严格递增、所有权校验、幂等重试不产生第二条事件。"""

    def setUp(self) -> None:
        super().setUp()
        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")

    def test_sequence_is_strictly_increasing_across_event_types(self) -> None:
        started = self.queue.append_delivery_event(
            task_id="tsk-1", worker_id="worker-1", event_type="started",
            idempotency_key="tsk-1:a1:started",
        )
        progress1 = self.queue.append_delivery_event(
            task_id="tsk-1", worker_id="worker-1", event_type="progress",
            idempotency_key="tsk-1:a1:progress:1", elapsed_seconds=3,
        )
        progress2 = self.queue.append_delivery_event(
            task_id="tsk-1", worker_id="worker-1", event_type="progress",
            idempotency_key="tsk-1:a1:progress:2", elapsed_seconds=6,
        )
        self.assertEqual((started.sequence, progress1.sequence, progress2.sequence), (1, 2, 3))
        self.assertFalse(started.duplicate or progress1.duplicate or progress2.duplicate)

    def test_retrying_the_same_idempotency_key_returns_the_same_row_not_a_new_one(self) -> None:
        first = self.queue.append_delivery_event(
            task_id="tsk-1", worker_id="worker-1", event_type="started",
            idempotency_key="tsk-1:a1:started",
        )
        retry = self.queue.append_delivery_event(
            task_id="tsk-1", worker_id="worker-1", event_type="started",
            idempotency_key="tsk-1:a1:started",
        )
        self.assertEqual(first.sequence, retry.sequence)
        self.assertFalse(first.duplicate)
        self.assertTrue(retry.duplicate)
        self.assertEqual(self.scalar("SELECT count(*) FROM task_delivery_event"), 1)

    def test_append_rejected_for_non_owning_worker(self) -> None:
        result = self.queue.append_delivery_event(
            task_id="tsk-1", worker_id="worker-zombie", event_type="started",
            idempotency_key="tsk-1:zombie:started",
        )
        self.assertIsNone(result)
        self.assertEqual(self.scalar("SELECT count(*) FROM task_delivery_event"), 0)

    def test_append_rejected_when_task_not_running(self) -> None:
        self.seed_running_task(task_id="tsk-queued", conversation_id="cnv-queued", status="queued")
        result = self.queue.append_delivery_event(
            task_id="tsk-queued", worker_id="worker-1", event_type="started",
            idempotency_key="tsk-queued:a1:started",
        )
        self.assertIsNone(result)

    def test_concurrent_appends_serialize_and_never_collide_on_sequence(self) -> None:
        """两个并发写者对同一任务追加事件：所有权锁定在 task 行上，天然串行化，
        不会产生重复或跳号的 sequence（V-投递-01 的并发覆盖）。"""

        errors: list[BaseException] = []
        results: list[AppendedEvent | None] = []
        lock = threading.Lock()

        def append(n: int) -> None:
            try:
                appended = self.queue.append_delivery_event(
                    task_id="tsk-1", worker_id="worker-1", event_type="progress",
                    idempotency_key=f"tsk-1:a1:progress:{n}", elapsed_seconds=n,
                )
            except BaseException as error:  # noqa: BLE001 - 收集到主线程再判定
                with lock:
                    errors.append(error)
                return
            with lock:
                results.append(appended)

        threads = [threading.Thread(target=append, args=(n,)) for n in range(1, 6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        sequences = sorted(item.sequence for item in results if item is not None)
        self.assertEqual(sequences, [1, 2, 3, 4, 5], "并发写入必须严格递增且互不冲突")


class TerminalEventTests(DeliveryOutboxTestCase):
    """状态合同第 2 条：终态写入与任务转 ``awaiting_delivery`` 同事务，话题继续占用。"""

    def setUp(self) -> None:
        super().setUp()
        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")

    def test_write_terminal_event_transitions_status_and_holds_the_topic(self) -> None:
        appended = self.queue.write_terminal_event(
            task_id="tsk-1", worker_id="worker-1", terminal_kind="success",
            error_kind=None, content="答案", agent_session_id="sess-new",
        )
        self.assertEqual(appended.sequence, 1)
        self.assertEqual(self.scalar("SELECT status FROM task WHERE id='tsk-1'"), "awaiting_delivery")
        # 话题**不释放**：running_task_id 仍指向这个任务，直到投递解析。
        self.assertEqual(
            self.scalar("SELECT running_task_id FROM conversation WHERE id='cnv-1'"), "tsk-1"
        )
        row = self.query(
            "SELECT terminal_kind, content, agent_session_id FROM task_delivery_event WHERE task_id='tsk-1'"
        )[0]
        self.assertEqual(row, ("success", "答案", "sess-new"))

    def test_write_terminal_event_persists_token_usage_and_guard_denied_count(self) -> None:
        """通报补数（迁移 0070，Issue #303/#304 批次 4）：终态写入同事务落
        ``task.token_usage``/``task.guard_denied_count``——真实 JSONB 往返，
        不是应用层假装写入。"""

        self.queue.write_terminal_event(
            task_id="tsk-1", worker_id="worker-1", terminal_kind="success",
            error_kind=None, content="答案",
            token_usage={"input_tokens": 100, "output_tokens": 20},
            guard_denied_count=3,
        )

        row = self.query("SELECT token_usage, guard_denied_count FROM task WHERE id='tsk-1'")[0]
        self.assertEqual(row[0], {"input_tokens": 100, "output_tokens": 20})
        self.assertEqual(row[1], 3)

    def test_write_terminal_event_without_usage_leaves_both_columns_null_not_zero(self) -> None:
        """取不到时必须是真正的 SQL NULL（``token_usage IS NULL``），不能是
        ``'null'::jsonb`` 或 ``0``——否则通报聚合会把"取不到"误判成"取到了、
        值是零/空对象"（见 ``adapters/postgres_conversation/_queue_outbox.py``
        的 ``_jsonb_or_none`` 文档）。"""

        self.queue.write_terminal_event(
            task_id="tsk-1", worker_id="worker-1", terminal_kind="failed",
            error_kind="session_failed", content=None,
        )

        row = self.query(
            "SELECT token_usage, guard_denied_count,"
            " (token_usage IS NULL) AS usage_is_sql_null"
            " FROM task WHERE id='tsk-1'"
        )[0]
        self.assertIsNone(row[0])
        self.assertIsNone(row[1])
        self.assertTrue(row[2])

    def test_second_terminal_write_is_rejected_not_a_second_valid_terminal(self) -> None:
        first = self.queue.write_terminal_event(
            task_id="tsk-1", worker_id="worker-1", terminal_kind="success",
            error_kind=None, content="答案",
        )
        second = self.queue.write_terminal_event(
            task_id="tsk-1", worker_id="worker-1", terminal_kind="failed",
            error_kind="session_failed", content="不应该生效的第二条终态",
        )
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(self.scalar("SELECT count(*) FROM task_delivery_event WHERE event_type='terminal'"), 1)
        self.assertEqual(self.scalar("SELECT terminal_kind FROM task_delivery_event WHERE task_id='tsk-1'"), "success")

    def test_write_terminal_event_rejected_for_non_owning_worker(self) -> None:
        result = self.queue.write_terminal_event(
            task_id="tsk-1", worker_id="worker-zombie", terminal_kind="success",
            error_kind=None, content="答案",
        )
        self.assertIsNone(result)
        self.assertEqual(self.scalar("SELECT status FROM task WHERE id='tsk-1'"), "running")


class DeliveryResolutionTests(DeliveryOutboxTestCase):
    """confirm_delivery / expire_undelivered_terminals：投递解析不得改写业务结论
    （V-投递-04），到期是唯一允许覆盖业务结论的路径。"""

    def test_confirm_delivery_resolves_success_carries_session_and_releases_topic(self) -> None:
        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.queue.write_terminal_event(
            task_id="tsk-1", worker_id="worker-1", terminal_kind="success",
            error_kind=None, content="答案", agent_session_id="sess-new",
        )
        confirmed = self.queue.confirm_delivery(
            task_id="tsk-1", platform_message_kind="card", platform_message_id="card-1",
        )
        self.assertTrue(confirmed)
        self.assertEqual(self.scalar("SELECT status FROM task WHERE id='tsk-1'"), "succeeded")
        self.assertIsNone(self.scalar("SELECT error_kind FROM task WHERE id='tsk-1'"))
        self.assertIsNone(self.scalar("SELECT running_task_id FROM conversation WHERE id='cnv-1'"))
        self.assertEqual(
            self.scalar("SELECT agent_session_id FROM conversation WHERE id='cnv-1'"), "sess-new"
        )
        self.assertIsNotNone(
            self.scalar("SELECT platform_received_at FROM task_delivery_event WHERE task_id='tsk-1'")
        )
        self.assertEqual(
            self.scalar("SELECT platform_message_id FROM task_delivery_event WHERE task_id='tsk-1'"),
            "card-1",
        )

    def test_confirm_delivery_queues_the_overwritten_agent_session_for_cleanup(self) -> None:
        """PR #173 独立复核 P2-4：``confirm_delivery()`` 与 ``finish()`` 用同一手法
        写回 ``agent_session_id``，同一个覆盖缺口在这条路径上也存在——新终态带来
        的新 session id 覆盖掉旧值时，旧值必须被排队做物理清理。"""

        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.execute("UPDATE conversation SET agent_session_id='sess-old' WHERE id='cnv-1'")
        self.queue.write_terminal_event(
            task_id="tsk-1", worker_id="worker-1", terminal_kind="success",
            error_kind=None, content="答案", agent_session_id="sess-new",
        )

        confirmed = self.queue.confirm_delivery(
            task_id="tsk-1", platform_message_kind="card", platform_message_id="card-1",
        )

        self.assertTrue(confirmed)
        self.assertEqual(
            self.scalar("SELECT agent_session_id FROM conversation WHERE id='cnv-1'"), "sess-new"
        )
        self.assertEqual(
            self.scalar(
                "SELECT reason FROM agent_session_cleanup WHERE agent_session_id='sess-old'"
            ),
            "session_overwritten",
            "被覆盖的旧 session id 必须被排队做物理清理，否则永久留在磁盘上",
        )
        self.assertIsNone(
            self.scalar(
                "SELECT 1 FROM agent_session_cleanup WHERE agent_session_id='sess-new'"
            ),
            "刚写入的新 session id 是活跃会话，不该被排队清理",
        )

    def test_confirm_delivery_keeps_business_failure_not_upgraded_by_successful_delivery(self) -> None:
        """V-投递-04：飞书接受了失败卡片，业务结果仍然是 failed，不能因为投递
        成功就变成 succeeded。"""

        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.queue.write_terminal_event(
            task_id="tsk-1", worker_id="worker-1", terminal_kind="failed",
            error_kind="session_failed", content="失败提示",
        )
        self.queue.confirm_delivery(
            task_id="tsk-1", platform_message_kind="text", platform_message_id="msg-1",
        )
        self.assertEqual(self.scalar("SELECT status FROM task WHERE id='tsk-1'"), "failed")
        self.assertEqual(self.scalar("SELECT error_kind FROM task WHERE id='tsk-1'"), "session_failed")

    def test_confirm_delivery_returns_false_when_task_not_awaiting_delivery(self) -> None:
        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        # 还在 running：从未写过 terminal 事件。
        self.assertFalse(
            self.queue.confirm_delivery(
                task_id="tsk-1", platform_message_kind="card", platform_message_id="c1"
            )
        )

    def test_confirm_delivery_is_not_repeatable(self) -> None:
        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.queue.write_terminal_event(
            task_id="tsk-1", worker_id="worker-1", terminal_kind="success",
            error_kind=None, content="答案",
        )
        self.assertTrue(
            self.queue.confirm_delivery(task_id="tsk-1", platform_message_kind="card", platform_message_id="c1")
        )
        # 第二次确认：任务已经不在 awaiting_delivery，也不应该覆盖已有送达标记。
        self.assertFalse(
            self.queue.confirm_delivery(task_id="tsk-1", platform_message_kind="card", platform_message_id="c2")
        )
        self.assertEqual(
            self.scalar("SELECT platform_message_id FROM task_delivery_event WHERE task_id='tsk-1'"), "c1"
        )

    def test_confirm_delivery_rolls_back_entirely_when_the_conversation_write_conflicts(self) -> None:
        """内审 P2-4：``conversation.running_task_id`` 与任务不一致（竞态或状态
        损坏）时，事件确认与任务收敛这两步已经在同一事务里执行、尚未提交——修复
        前它们会照常提交，只有 conversation 回写悄悄失败并返回一个和"没有可确认
        的东西"含义相同的 ``False``，``agent_session_id`` 被静默丢弃。修复后必须
        ``raise`` 并让整个事务回滚：三条写入要么全部生效，要么全部不生效。"""

        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.queue.write_terminal_event(
            task_id="tsk-1", worker_id="worker-1", terminal_kind="success",
            error_kind=None, content="答案正文", agent_session_id="sess-9",
        )
        # 模拟竞态：conversation 已经不再指向这个任务了（例如被别的路径改动）。
        self.execute("UPDATE conversation SET running_task_id='tsk-other' WHERE id='cnv-1'")

        with self.assertRaises(RuntimeError):
            self.queue.confirm_delivery(
                task_id="tsk-1", platform_message_kind="card", platform_message_id="om-1",
            )

        # 整个确认事务必须整体回滚：task 与 event 都不能推进到"已确认"，
        # 不能出现"业务已提交、会话回写却丢了"的半条状态。
        self.assertEqual(self.scalar("SELECT status FROM task WHERE id='tsk-1'"), "awaiting_delivery")
        self.assertIsNone(
            self.scalar("SELECT platform_received_at FROM task_delivery_event WHERE task_id='tsk-1'")
        )
        self.assertIsNone(self.scalar("SELECT agent_session_id FROM conversation WHERE id='cnv-1'"))
        # 重试（假设调用方在竞态解除后重试）仍然可以正常成功：这不是把任务卡死，
        # 只是不允许静默的半提交。
        self.execute("UPDATE conversation SET running_task_id='tsk-1' WHERE id='cnv-1'")
        self.assertTrue(
            self.queue.confirm_delivery(
                task_id="tsk-1", platform_message_kind="card", platform_message_id="om-2",
            )
        )
        self.assertEqual(
            self.scalar("SELECT agent_session_id FROM conversation WHERE id='cnv-1'"), "sess-9"
        )

    def test_expire_undelivered_terminals_forces_delivery_expired_even_for_business_success(self) -> None:
        """V-投递-04/06：二十四小时到期是唯一允许覆盖业务结论的路径——即使原始
        业务结论是 success，到期也必须收敛为 failed/delivery_expired，不得把
        任务写成用户已取得结果。"""

        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1", status="awaiting_delivery")
        self.seed_terminal_event(
            task_id="tsk-1", terminal_kind="success", content="答案",
            created_at_sql="now() - interval '25 hours'",
        )
        expired = self.queue.expire_undelivered_terminals()
        self.assertEqual([item.task_id for item in expired], ["tsk-1"])
        self.assertEqual(self.scalar("SELECT status FROM task WHERE id='tsk-1'"), "failed")
        self.assertEqual(self.scalar("SELECT error_kind FROM task WHERE id='tsk-1'"), "delivery_expired")
        self.assertIsNone(self.scalar("SELECT running_task_id FROM conversation WHERE id='cnv-1'"))
        # 正文清空，但 sequence/terminal_kind 等低敏事实原样保留最长九十天。
        row = self.query(
            "SELECT content, terminal_kind, sequence FROM task_delivery_event WHERE task_id='tsk-1'"
        )[0]
        self.assertEqual(row, (None, "success", 1))

    def test_expire_undelivered_terminals_ignores_not_yet_expired(self) -> None:
        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1", status="awaiting_delivery")
        self.seed_terminal_event(task_id="tsk-1", created_at_sql="now()")
        expired = self.queue.expire_undelivered_terminals()
        self.assertEqual(expired, [])
        self.assertEqual(self.scalar("SELECT status FROM task WHERE id='tsk-1'"), "awaiting_delivery")

    def test_expire_undelivered_terminals_ignores_already_delivered(self) -> None:
        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1", status="awaiting_delivery")
        self.seed_terminal_event(
            task_id="tsk-1",
            created_at_sql="now() - interval '25 hours'",
            platform_received=True,
        )
        self.execute("UPDATE task SET status='succeeded' WHERE id='tsk-1'")
        expired = self.queue.expire_undelivered_terminals()
        self.assertEqual(expired, [])
        self.assertEqual(self.scalar("SELECT status FROM task WHERE id='tsk-1'"), "succeeded")

    def test_expire_undelivered_terminals_is_governed_by_the_trigger_owned_expires_at_column(self) -> None:
        """内审 P2-1：清理判定必须直接读触发器锁定的 ``expires_at`` 列本身，不能
        在应用层按一个可配置窗口重新计算"是否过期"。这里构造一条 ``created_at``
        仍是刚才、但 ``expires_at`` 被直接改到已过期的行——修复前的查询判据是
        ``created_at < now() - 窗口``，这一行永远不会被判定过期（``created_at``
        是刚才）；修复后的查询判据是 ``expires_at <= now()``，必须正确收敛它。

        直接改 ``expires_at`` 前必须先关掉触发器：这本身就是证据——应用代码没有
        任何合法路径能做到下面这一步，这里只是为了不真的等 24 小时来模拟"到期
        时刻已到"。
        """

        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1", status="awaiting_delivery")
        self.seed_terminal_event(task_id="tsk-1", terminal_kind="success", content="答案")
        self.execute("ALTER TABLE task_delivery_event DISABLE TRIGGER task_delivery_event_expiry")
        self.execute(
            "UPDATE task_delivery_event SET expires_at = now() - interval '1 minute' WHERE task_id = %s",
            ("tsk-1",),
        )
        self.execute("ALTER TABLE task_delivery_event ENABLE TRIGGER task_delivery_event_expiry")

        expired = self.queue.expire_undelivered_terminals()

        self.assertEqual([item.task_id for item in expired], ["tsk-1"])
        self.assertEqual(self.scalar("SELECT status FROM task WHERE id='tsk-1'"), "failed")
        self.assertEqual(self.scalar("SELECT error_kind FROM task WHERE id='tsk-1'"), "delivery_expired")

    def test_append_delivery_event_accepts_safely_releasable_answer_with_content(self) -> None:
        """机制级验证（内审 P3-3）：``safely_releasable_answer`` 是 ``terminal``
        之外唯一允许携带正文的事件类型（``CONTENT_BEARING_EVENT_TYPES``），持久化
        机制本身完整可用。Worker 当前的执行路径还没有产生这类事件的调用点——见
        PR 正文「已知范围边界」，这条用例只证明底层机制不是"合同写了、实现里
        连能不能插进去都没验证过"。"""

        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        appended = self.queue.append_delivery_event(
            task_id="tsk-1", worker_id="worker-1", event_type="safely_releasable_answer",
            idempotency_key="tsk-1:a1:safely_releasable_answer:1", content="流式安全片段",
        )
        self.assertFalse(appended.duplicate)
        self.assertEqual(
            self.scalar(
                "SELECT content FROM task_delivery_event WHERE idempotency_key=%s",
                ("tsk-1:a1:safely_releasable_answer:1",),
            ),
            "流式安全片段",
        )


class SessionRetentionCleanupTests(DeliveryOutboxTestCase):
    """V-投递-05/10：会话边界触发（/new、空闲到点、按用户）统一清除已送达正文，
    送达前的正文不受影响（独立走二十四小时到期路径）。"""

    def _seed_delivered(self, *, task_id: str, conversation_id: str, user_id: str = "usr-1") -> None:
        self.seed_running_task(task_id=task_id, conversation_id=conversation_id, user_id=user_id)
        self.queue.write_terminal_event(
            task_id=task_id, worker_id="worker-1", terminal_kind="success",
            error_kind=None, content="已送达的答案",
        )
        self.queue.confirm_delivery(task_id=task_id, platform_message_kind="card", platform_message_id="c-" + task_id)

    def test_new_clears_delivered_content_in_the_same_transaction(self) -> None:
        self._seed_delivered(task_id="tsk-1", conversation_id="cnv-1")
        self.execute("UPDATE conversation SET agent_session_id='sess-old' WHERE id='cnv-1'")

        with self.store.transaction() as transaction:
            cleared = transaction.clear_agent_session(conversation_id="cnv-1")

        self.assertTrue(cleared)
        self.assertIsNone(self.scalar("SELECT agent_session_id FROM conversation WHERE id='cnv-1'"))
        self.assertIsNone(self.scalar("SELECT content FROM task_delivery_event WHERE task_id='tsk-1'"))

    def test_new_on_busy_topic_clears_nothing(self) -> None:
        self._seed_delivered(task_id="tsk-1", conversation_id="cnv-1")
        self.execute("UPDATE conversation SET agent_session_id='sess-old' WHERE id='cnv-1'")
        # 模拟话题忙碌：一个新任务正在占用。
        self.execute(
            """INSERT INTO task (id,conversation_id,user_id,inbound_event_id,prompt,status,
                                 target_worker_version,worker_id,heartbeat_at,attempts,content_expires_at)
               VALUES ('tsk-new','cnv-1','usr-1','event-new','追问','running','stable','worker-1',now(),1,now())"""
        )
        self.execute("UPDATE conversation SET running_task_id='tsk-new' WHERE id='cnv-1'")

        with self.store.transaction() as transaction:
            cleared = transaction.clear_agent_session(conversation_id="cnv-1")

        self.assertFalse(cleared)
        self.assertEqual(self.scalar("SELECT agent_session_id FROM conversation WHERE id='cnv-1'"), "sess-old")
        self.assertEqual(
            self.scalar("SELECT content FROM task_delivery_event WHERE task_id='tsk-1'"), "已送达的答案"
        )

    def test_pending_undelivered_content_is_not_touched_by_conversation_clear(self) -> None:
        """送达前的正文走独立的二十四小时到期路径，不受会话边界清理影响。"""

        self._seed_delivered(task_id="tsk-1", conversation_id="cnv-1")
        self.seed_running_task(task_id="tsk-2", conversation_id="cnv-1")
        self.queue.write_terminal_event(
            task_id="tsk-2", worker_id="worker-1", terminal_kind="success",
            error_kind=None, content="尚未送达的答案",
        )

        cleared_count = self.queue.clear_delivered_content_for_conversation(conversation_id="cnv-1")

        self.assertEqual(cleared_count, 1)
        self.assertIsNone(self.scalar("SELECT content FROM task_delivery_event WHERE task_id='tsk-1'"))
        self.assertEqual(
            self.scalar("SELECT content FROM task_delivery_event WHERE task_id='tsk-2'"), "尚未送达的答案"
        )

    def test_sweep_idle_conversations_clears_only_idle_and_delivered(self) -> None:
        self._seed_delivered(task_id="tsk-idle", conversation_id="cnv-idle")
        self.execute(
            "UPDATE conversation SET running_task_id=NULL, last_task_ended_at=now() - interval '3 hours' WHERE id='cnv-idle'"
        )
        self._seed_delivered(task_id="tsk-active", conversation_id="cnv-active")
        self.execute(
            "UPDATE conversation SET running_task_id=NULL, last_task_ended_at=now() - interval '5 minutes' WHERE id='cnv-active'"
        )

        cleared = self.queue.sweep_idle_conversations(idle_after=timedelta(hours=2))

        self.assertEqual(cleared, 1)
        self.assertIsNone(self.scalar("SELECT content FROM task_delivery_event WHERE task_id='tsk-idle'"))
        self.assertEqual(
            self.scalar("SELECT content FROM task_delivery_event WHERE task_id='tsk-active'"), "已送达的答案"
        )
        # 幂等：再跑一轮不再有可清的会话。
        self.assertEqual(self.queue.sweep_idle_conversations(idle_after=timedelta(hours=2)), 0)

    def test_sweep_idle_conversations_skips_busy_topics(self) -> None:
        self._seed_delivered(task_id="tsk-1", conversation_id="cnv-1")
        self.execute(
            "UPDATE conversation SET last_task_ended_at=now() - interval '3 hours' WHERE id='cnv-1'"
        )
        self.execute(
            """INSERT INTO task (id,conversation_id,user_id,inbound_event_id,prompt,status,
                                 target_worker_version,worker_id,heartbeat_at,attempts,content_expires_at)
               VALUES ('tsk-new','cnv-1','usr-1','event-new','追问','running','stable','worker-1',now(),1,now())"""
        )
        self.execute("UPDATE conversation SET running_task_id='tsk-new' WHERE id='cnv-1'")

        cleared = self.queue.sweep_idle_conversations(idle_after=timedelta(hours=2))

        self.assertEqual(cleared, 0)
        self.assertEqual(
            self.scalar("SELECT content FROM task_delivery_event WHERE task_id='tsk-1'"), "已送达的答案"
        )

    def test_clear_delivered_content_for_user_covers_all_their_conversations_only(self) -> None:
        self._seed_delivered(task_id="tsk-a1", conversation_id="cnv-a1", user_id="usr-1")
        self._seed_delivered(task_id="tsk-a2", conversation_id="cnv-a2", user_id="usr-1")
        self._seed_delivered(task_id="tsk-b1", conversation_id="cnv-b1", user_id="usr-2")

        cleared = self.queue.clear_delivered_content_for_user(user_id="usr-1")

        self.assertEqual(cleared, 2)
        self.assertIsNone(self.scalar("SELECT content FROM task_delivery_event WHERE task_id='tsk-a1'"))
        self.assertIsNone(self.scalar("SELECT content FROM task_delivery_event WHERE task_id='tsk-a2'"))
        self.assertEqual(
            self.scalar("SELECT content FROM task_delivery_event WHERE task_id='tsk-b1'"), "已送达的答案"
        )


class AgentSessionCleanupQueueTests(DeliveryOutboxTestCase):
    """Issue #153：三个会话边界触发点（``/new``、空闲到点、按用户清理）在各自的
    事务里往 ``agent_session_cleanup`` 排队；真正的文件删除留给 Worker 的周期性
    收口（``tests/test_worker_queue_consumer.py`` 覆盖那一半），本文件只覆盖
    "该不该排队、排的是不是正确的 session id、会不会排重复"。
    """

    def _pending_reasons(self, *, agent_session_id: str) -> list[str]:
        return [
            row[0]
            for row in self.query(
                "SELECT reason FROM agent_session_cleanup WHERE agent_session_id=%s",
                (agent_session_id,),
            )
        ]

    def test_new_command_queues_a_cleanup_for_the_session_it_just_retired(self) -> None:
        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.execute("UPDATE conversation SET running_task_id=NULL WHERE id='cnv-1'")
        self.execute("UPDATE conversation SET agent_session_id='sess-old' WHERE id='cnv-1'")

        with self.store.transaction() as transaction:
            cleared = transaction.clear_agent_session(conversation_id="cnv-1")

        self.assertTrue(cleared)
        self.assertEqual(self._pending_reasons(agent_session_id="sess-old"), ["new_command"])
        # 排队的是被清空之前的旧值，不是清空后的 NULL——否则整条记账毫无意义。
        self.assertEqual(
            self.scalar(
                "SELECT user_id FROM agent_session_cleanup WHERE agent_session_id='sess-old'"
            ),
            "usr-1",
        )

    def test_new_command_on_a_session_that_was_already_empty_queues_nothing(self) -> None:
        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.execute("UPDATE conversation SET running_task_id=NULL WHERE id='cnv-1'")
        # agent_session_id 从未被设置过（默认 NULL）。

        with self.store.transaction() as transaction:
            transaction.clear_agent_session(conversation_id="cnv-1")

        self.assertEqual(self.scalar("SELECT count(*) FROM agent_session_cleanup"), 0)

    def test_idle_sweep_queues_cleanup_even_without_pending_outbox_content(self) -> None:
        """空闲到点的物理清理候选集比"仍有未清正文"更宽（模块说明的核心取舍）：
        一个已经送达、outbox 正文早被清过、但仍持有 agent_session_id 的会话，
        到点后也必须被排队清理 JSONL。
        """

        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.execute(
            "UPDATE conversation SET running_task_id=NULL, agent_session_id='sess-idle', "
            "last_task_ended_at=now() - interval '3 hours' WHERE id='cnv-1'"
        )

        cleared = self.queue.sweep_idle_conversations(idle_after=timedelta(hours=2))

        self.assertEqual(cleared, 0, "没有待清正文，因此清理正文的返回计数应为 0")
        self.assertEqual(self._pending_reasons(agent_session_id="sess-idle"), ["idle_timeout"])
        # 到点扫描本身不改判定用的时间戳来源——agent_session_id 仍原样保留，
        # 是否 resume 仍由领取任务时的两小时规则时间戳比较决定（架构设计 5.2 节）。
        self.assertEqual(
            self.scalar("SELECT agent_session_id FROM conversation WHERE id='cnv-1'"), "sess-idle"
        )

    def test_idle_sweep_does_not_queue_a_conversation_that_is_still_active(self) -> None:
        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.execute(
            "UPDATE conversation SET running_task_id=NULL, agent_session_id='sess-fresh', "
            "last_task_ended_at=now() - interval '5 minutes' WHERE id='cnv-1'"
        )

        self.queue.sweep_idle_conversations(idle_after=timedelta(hours=2))

        self.assertEqual(self.scalar("SELECT count(*) FROM agent_session_cleanup"), 0)

    def test_idle_sweep_is_idempotent_and_does_not_duplicate_the_queue_entry(self) -> None:
        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.execute(
            "UPDATE conversation SET running_task_id=NULL, agent_session_id='sess-idle', "
            "last_task_ended_at=now() - interval '3 hours' WHERE id='cnv-1'"
        )

        self.queue.sweep_idle_conversations(idle_after=timedelta(hours=2))
        self.queue.sweep_idle_conversations(idle_after=timedelta(hours=2))

        self.assertEqual(
            self.scalar(
                "SELECT count(*) FROM agent_session_cleanup WHERE agent_session_id='sess-idle'"
            ),
            1,
            "同一个 session id 重复到点扫描不得排出第二条待办",
        )

    def test_idle_sweep_does_not_reselect_a_session_already_queued_by_another_trigger(
        self,
    ) -> None:
        """PR #173 独立复核 P2-5：``sweep_idle_conversations`` 的候选集查询必须
        用 ``NOT EXISTS`` 排除已经在队列里的 ``agent_session_id``——不只是靠
        写入时的 ``ON CONFLICT DO NOTHING`` 兜底去重，否则每 60 秒的扫描都会把
        每一个曾经用过会话、之后闲置的话题重新捞出来做一遍纯浪费的插入尝试
        （见 ``sweep_idle_conversations`` 的查询注释）。

        用一个已经被**另一个触发点**（``/new``）排过队、原因是 ``new_command``
        的会话来验证：即使它同时也满足空闲到点的候选条件，扫描也不应该"重新
        发现"它——候选集里根本不该再出现这一行，原始排队原因原样保留。
        """

        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.execute(
            "UPDATE conversation SET running_task_id=NULL, agent_session_id='sess-already-queued', "
            "last_task_ended_at=now() - interval '3 hours' WHERE id='cnv-1'"
        )
        # 模拟这个 session id 早先已经被 /new 触发点排过队（原因不同）。
        with self.store.transaction() as transaction:
            transaction._queue_session_cleanup(
                user_id="usr-1", agent_session_id="sess-already-queued", reason="new_command"
            )

        self.queue.sweep_idle_conversations(idle_after=timedelta(hours=2))

        self.assertEqual(
            self.scalar(
                "SELECT count(*) FROM agent_session_cleanup WHERE agent_session_id='sess-already-queued'"
            ),
            1,
            "已经在队列里的 session id 不应该被空闲扫描重新选中",
        )
        self.assertEqual(
            self._pending_reasons(agent_session_id="sess-already-queued"),
            ["new_command"],
            "原始排队原因必须原样保留，不能被扫描覆盖或追加",
        )

    def test_user_level_clear_retires_every_conversation_session_for_that_user_only(self) -> None:
        self.seed_running_task(task_id="tsk-a1", conversation_id="cnv-a1", user_id="usr-1")
        self.execute(
            "UPDATE conversation SET running_task_id=NULL, agent_session_id='sess-a1' "
            "WHERE id='cnv-a1'"
        )
        self.seed_running_task(task_id="tsk-a2", conversation_id="cnv-a2", user_id="usr-1")
        self.execute(
            "UPDATE conversation SET running_task_id=NULL, agent_session_id='sess-a2' "
            "WHERE id='cnv-a2'"
        )
        self.seed_running_task(task_id="tsk-b1", conversation_id="cnv-b1", user_id="usr-2")
        self.execute(
            "UPDATE conversation SET running_task_id=NULL, agent_session_id='sess-b1' "
            "WHERE id='cnv-b1'"
        )

        self.queue.clear_delivered_content_for_user(user_id="usr-1")

        # 停用/权限变化是硬失效：与 /new、空闲到点不同，这里必须立刻让
        # agent_session_id 本身失效，不依赖后续任务领取时的两小时比较。
        self.assertIsNone(
            self.scalar("SELECT agent_session_id FROM conversation WHERE id='cnv-a1'")
        )
        self.assertIsNone(
            self.scalar("SELECT agent_session_id FROM conversation WHERE id='cnv-a2'")
        )
        self.assertEqual(self._pending_reasons(agent_session_id="sess-a1"), ["user_cleared"])
        self.assertEqual(self._pending_reasons(agent_session_id="sess-a2"), ["user_cleared"])
        # 另一个用户的会话完全不受影响。
        self.assertEqual(
            self.scalar("SELECT agent_session_id FROM conversation WHERE id='cnv-b1'"), "sess-b1"
        )
        self.assertEqual(self._pending_reasons(agent_session_id="sess-b1"), [])

    def test_claim_marks_rows_and_a_second_claim_within_the_soft_window_returns_nothing(
        self,
    ) -> None:
        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.execute("UPDATE conversation SET running_task_id=NULL WHERE id='cnv-1'")
        self.execute("UPDATE conversation SET agent_session_id='sess-old' WHERE id='cnv-1'")
        with self.store.transaction() as transaction:
            transaction.clear_agent_session(conversation_id="cnv-1")

        first = self.queue.claim_session_cleanups(limit=10)
        second = self.queue.claim_session_cleanups(limit=10)

        self.assertEqual([item.agent_session_id for item in first], ["sess-old"])
        self.assertEqual(first[0].reason, "new_command")
        self.assertEqual(second, [], "十分钟软领取窗口内不得被第二次认领")

    def test_marking_done_removes_the_row_from_future_claims(self) -> None:
        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.execute("UPDATE conversation SET running_task_id=NULL WHERE id='cnv-1'")
        self.execute("UPDATE conversation SET agent_session_id='sess-old' WHERE id='cnv-1'")
        with self.store.transaction() as transaction:
            transaction.clear_agent_session(conversation_id="cnv-1")
        claimed = self.queue.claim_session_cleanups(limit=10)

        self.queue.mark_session_cleanups_done(ids=[item.id for item in claimed])

        self.assertEqual(
            self.scalar(
                "SELECT done_at IS NOT NULL FROM agent_session_cleanup WHERE agent_session_id='sess-old'"
            ),
            True,
        )
        # 十分钟窗口过期后也不会被重新捞出：done_at 非空的行被查询条件永久排除。
        self.execute(
            "UPDATE agent_session_cleanup SET claimed_at = now() - interval '1 hour' "
            "WHERE agent_session_id='sess-old'"
        )
        self.assertEqual(self.queue.claim_session_cleanups(limit=10), [])

    def test_stale_claim_past_the_soft_window_is_retried(self) -> None:
        """模拟上一次认领的进程异常退出、物理删除没跑完：十分钟后必须能被
        重新认领，而不是永久卡在"已认领但从未完成"状态。"""

        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")
        self.execute("UPDATE conversation SET running_task_id=NULL WHERE id='cnv-1'")
        self.execute("UPDATE conversation SET agent_session_id='sess-old' WHERE id='cnv-1'")
        with self.store.transaction() as transaction:
            transaction.clear_agent_session(conversation_id="cnv-1")
        self.queue.claim_session_cleanups(limit=10)
        self.execute(
            "UPDATE agent_session_cleanup SET claimed_at = now() - interval '11 minutes' "
            "WHERE agent_session_id='sess-old'"
        )

        retried = self.queue.claim_session_cleanups(limit=10)

        self.assertEqual([item.agent_session_id for item in retried], ["sess-old"])


class WorkerServiceHousekeepingIntegrationTests(DeliveryOutboxTestCase):
    """WorkerService._housekeep() 真的接线了到期清理，不只是 adapter 层有方法。"""

    def test_process_once_expires_stale_awaiting_delivery_tasks(self) -> None:
        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1", status="awaiting_delivery")
        self.seed_terminal_event(task_id="tsk-1", created_at_sql="now() - interval '25 hours'")

        config = WorkerConfig(
            question="", read_only_tools=("mcp__q__read",), trace_id="01J00000000000000000000000",
            turn_timeout_seconds=1.0, worker_id="worker-1", target_worker_version="stable",
        )
        service = WorkerService(config=config, queue=self.queue)
        asyncio.run(service.process_once())

        self.assertEqual(self.scalar("SELECT status FROM task WHERE id='tsk-1'"), "failed")
        self.assertEqual(self.scalar("SELECT error_kind FROM task WHERE id='tsk-1'"), "delivery_expired")


class NegativeConstraintTests(DeliveryOutboxTestCase):
    """每条"不得"规则的主动违规尝试（验证与门禁第八节）：这些约束必须由数据库
    本身拒绝，不能只靠应用层自觉。"""

    def setUp(self) -> None:
        super().setUp()
        self.seed_running_task(task_id="tsk-1", conversation_id="cnv-1")

    def _insert(self, **overrides: object) -> None:
        fields = {
            "id": "tde-raw",
            "task_id": "tsk-1",
            "sequence": 1,
            "event_type": "started",
            "terminal_kind": None,
            "content": None,
            "worker_id": "worker-1",
            "idempotency_key": "tsk-1:raw",
            "platform_received_at": None,
        }
        fields.update(overrides)
        self.execute(
            """
            INSERT INTO task_delivery_event
                (id, task_id, sequence, event_type, terminal_kind, content,
                 worker_id, idempotency_key, platform_received_at)
            VALUES (%(id)s, %(task_id)s, %(sequence)s, %(event_type)s, %(terminal_kind)s,
                    %(content)s, %(worker_id)s, %(idempotency_key)s, %(platform_received_at)s)
            """,
            fields,
        )

    def test_non_terminal_event_cannot_carry_terminal_kind(self) -> None:
        with self.assertRaises(self._psycopg.errors.CheckViolation):
            self._insert(event_type="progress", terminal_kind="success")

    def test_terminal_event_requires_terminal_kind(self) -> None:
        with self.assertRaises(self._psycopg.errors.CheckViolation):
            self._insert(event_type="terminal", terminal_kind=None)

    def test_content_rejected_on_non_content_bearing_event_types(self) -> None:
        with self.assertRaises(self._psycopg.errors.CheckViolation):
            self._insert(event_type="started", content="不该允许出现在这里")

    def test_platform_received_at_rejected_outside_terminal(self) -> None:
        with self.assertRaises(self._psycopg.errors.CheckViolation):
            self._insert(event_type="progress", platform_received_at=datetime.now(timezone.utc))

    def test_unknown_event_type_rejected(self) -> None:
        with self.assertRaises(self._psycopg.errors.CheckViolation):
            self._insert(event_type="not_a_real_event_type")

    def test_duplicate_sequence_for_same_task_rejected(self) -> None:
        self._insert(id="tde-1", idempotency_key="tsk-1:a")
        with self.assertRaises(self._psycopg.errors.UniqueViolation):
            self._insert(id="tde-2", idempotency_key="tsk-1:b")

    def test_duplicate_idempotency_key_rejected(self) -> None:
        self._insert(id="tde-1", sequence=1, idempotency_key="tsk-1:dup")
        with self.assertRaises(self._psycopg.errors.UniqueViolation):
            self._insert(id="tde-2", sequence=2, idempotency_key="tsk-1:dup")

    def test_created_at_is_immutable(self) -> None:
        self._insert(id="tde-1")
        with self.assertRaises(self._psycopg.errors.RaiseException):
            self.execute(
                "UPDATE task_delivery_event SET created_at = now() - interval '1 day' WHERE id='tde-1'"
            )

    def test_task_id_sequence_and_event_type_are_immutable(self) -> None:
        self._insert(id="tde-1")
        for column, value in (("task_id", "tsk-other"), ("sequence", 99), ("event_type", "terminal")):
            with self.subTest(column=column):
                with self.assertRaises(self._psycopg.errors.RaiseException):
                    self.execute(
                        f"UPDATE task_delivery_event SET {column} = %s WHERE id='tde-1'", (value,)
                    )

    def test_expires_at_is_forced_to_24_hours_from_created_at_regardless_of_caller_input(self) -> None:
        self.execute(
            """
            INSERT INTO task_delivery_event
                (id, task_id, sequence, event_type, worker_id, idempotency_key, expires_at)
            VALUES ('tde-forced', 'tsk-1', 1, 'started', 'worker-1', 'tsk-1:forced', now() + interval '365 days')
            """
        )
        expires_at, created_at = self.query(
            "SELECT expires_at, created_at FROM task_delivery_event WHERE id='tde-forced'"
        )[0]
        self.assertLess((expires_at - created_at) - timedelta(hours=24), timedelta(seconds=2))

    def test_task_status_check_accepts_awaiting_delivery_and_rejects_unknown_value(self) -> None:
        self.execute("UPDATE task SET status='awaiting_delivery' WHERE id='tsk-1'")
        self.assertEqual(self.scalar("SELECT status FROM task WHERE id='tsk-1'"), "awaiting_delivery")
        with self.assertRaises(self._psycopg.errors.CheckViolation):
            self.execute("UPDATE task SET status='not_a_real_status' WHERE id='tsk-1'")
