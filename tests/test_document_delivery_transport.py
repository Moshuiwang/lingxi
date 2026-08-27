"""文档投递链路——gateway 独立消费循环 + 检查点恢复 + 读回判据（Issue #341 S-ES-3）。

覆盖任务卡「【测试】」列出的六项：

① ``task_document_delivery_request.task_id`` 的 UNIQUE 约束真实生效（幂等键，
   真库断言，不用 mock）；
② 检查点恢复：注入"建档成功后崩溃"，续做时不二次 ``create_document``（spy 断言
   恰一次调用），并从下一步续走到底；
③ ``read_members`` 不含目标 open_id 或档位不是 ``full_access`` → ``uncertain``，
   不得判 ``succeeded``；
④ definite 错误（飞书明确拒绝）→ ``failed`` + ``last_error``；
⑤ 未配置 ``LINGXI_GATEWAY_TENANT_DOMAIN``（即 ``GatewayConfig.tenant_domain is
   None``）→ 循环不注册，零行为差异（哨兵，不需要真库）；
⑥ worker 侧：终态成功且报告契约 ``document_request`` 非空 → 恰一行 ``pending``；
   终态失败，或字段为空 → 零行。

①-④、⑥ 需要真库（唯一约束、CHECK、以及 ``write_terminal_event`` 与终态事务的
真实交互不能靠假连接验证）；⑤ 是纯装配层判断，不接触数据库或网络。

变异锚点（任务卡登记，2026-08-27 实测还原）：
- 删掉迁移 0074 的 ``task_id UNIQUE`` 约束 → ①红；
- 把 ``DocumentDeliveryConsumer._process_claim`` 的 ``if document_id is None``
  判断去掉（每次都调用 ``create_document``）→ ②红；
- 把成功判据从"``read_members`` 确认 full_access"改成"四步没有抛异常就
  succeeded"（去掉 ``_has_confirmed_full_access`` 校验）→ ③红。
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from postgres_schema import ensure_production_schema, reset_production_rows

from lingxi.adapters.feishu_docx_delivery import FeishuDocxDeliveryError
from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_conversation import PostgresTaskQueue
from lingxi.adapters.postgres_document_delivery import PostgresDocumentDeliveryStore
from lingxi.apps.gateway.config import GatewayConfig, _Secret
from lingxi.apps.gateway.document_delivery import (
    DocumentDeliveryConsumer,
    assemble_document_delivery_consumer,
)
from lingxi.apps.worker.config import WorkerConfig
from lingxi.apps.worker.service import WorkerService

DSN = os.environ.get("LINGXI_POSTGRES_DSN")
SKIP_REASON = "跳过：未设置 LINGXI_POSTGRES_DSN，文档投递链路的真库断言未验证"

_USER_ENV_ROOT_DIR = tempfile.TemporaryDirectory(prefix="lingxi-doc-delivery-user-env-")
atexit.register(_USER_ENV_ROOT_DIR.cleanup)


def _seed_user_mcp_config(user_id: str) -> None:
    """给 ``user_id`` 放一份形状合法的 ``.mcp.json``（与
    ``tests/test_worker_queue_consumer.py`` 的同名夹具同一形状）——``_process_task``
    按用户读这份配置，读不到即失败关闭，不构造 executor。
    """

    home = Path(_USER_ENV_ROOT_DIR.name) / user_id
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
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class _SpyDocx:
    """docx 交付四步的假实现：可编排每一步的行为，记录调用次数供断言。

    ``create_result``/``members`` 既可以传常量，也可以传零参可调用（每次调用
    现求值一次），供需要"每次结果不同"或"抛异常"的用例复用同一个构造签名。
    """

    def __init__(self, *, create_result: Any = "doc-1", members: Any = ()) -> None:
        self.create_calls: list[str] = []
        self.write_calls: list[tuple[str, list[str]]] = []
        self.grant_calls: list[tuple[str, str]] = []
        self.read_calls: list[str] = []
        self._create_result = create_result
        self._members = members

    def create_document(self, title: str) -> str:
        self.create_calls.append(title)
        if callable(self._create_result):
            return self._create_result()
        return self._create_result

    def write_paragraphs(self, document_id: str, paragraphs: list[str]) -> None:
        self.write_calls.append((document_id, list(paragraphs)))

    def grant_full_access(self, document_id: str, open_id: str) -> None:
        self.grant_calls.append((document_id, open_id))

    def read_members(self, document_id: str) -> list[dict[str, Any]]:
        self.read_calls.append(document_id)
        if callable(self._members):
            return self._members()
        return list(self._members)

    def document_url(self, document_id: str) -> str:
        return f"https://example.feishu.cn/docx/{document_id}"


class _SpyNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send_text(self, *, open_id: str, text: str, dedupe_key: str) -> None:
        self.sent.append((open_id, text, dedupe_key))


@unittest.skipUnless(DSN, SKIP_REASON)
class DocumentDeliveryTransportTestCase(unittest.TestCase):
    """①-④ 的共同底座：真库、一个既有的 task 行（供插入文档投递请求关联）。"""

    TASK_ID = "tsk-doc-1"
    REQUESTER_OPEN_ID = "ou-doc-requester"

    @classmethod
    def setUpClass(cls) -> None:
        assert DSN is not None
        ensure_production_schema(DSN)

    def setUp(self) -> None:
        assert DSN is not None
        reset_production_rows(DSN)
        self.store = PostgresDocumentDeliveryStore(DSN)
        with connect(DSN) as connection:
            with connection.transaction():
                connection.execute(
                    """INSERT INTO app_user
                       (id, feishu_open_id, feishu_user_id, feishu_union_id,
                        display_name, department, tenant_key, provisioning_state)
                       VALUES ('usr-doc','ou-doc-requester','u-doc','un-doc',
                               '张三','数据部','tk-doc','active')"""
                )
                connection.execute(
                    """INSERT INTO conversation
                       (id,user_id,feishu_chat_id,feishu_thread_id,running_task_id)
                       VALUES ('cnv-doc','usr-doc','chat-doc','topic-doc',NULL)"""
                )
                connection.execute(
                    """INSERT INTO task
                       (id,conversation_id,user_id,inbound_event_id,prompt,status,
                        target_worker_version,attempts,content_expires_at)
                       VALUES (%s,'cnv-doc','usr-doc','event-doc','问题','succeeded',
                               'stable',1,now())""",
                    (self.TASK_ID,),
                )

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> None:
        assert DSN is not None
        with connect(DSN) as connection:
            with connection.transaction():
                connection.execute(sql, parameters)

    def scalar(self, sql: str, parameters: tuple[object, ...] = ()) -> object:
        assert DSN is not None
        with connect(DSN) as connection:
            row = connection.execute(sql, parameters).fetchone()
        return row[0] if row is not None else None

    def _seed_pending_request(self, *, request_id: str = "tdd-1", document_id: str | None = None) -> None:
        self.execute(
            """INSERT INTO task_document_delivery_request
               (id, task_id, requester_open_id, title, paragraphs, document_id)
               VALUES (%s, %s, %s, '标题', %s, %s)""",
            (
                request_id,
                self.TASK_ID,
                self.REQUESTER_OPEN_ID,
                json.dumps(["段落一", "段落二"], ensure_ascii=False),
                document_id,
            ),
        )

    # -- ① 唯一约束（幂等键） -------------------------------------------------

    def test_duplicate_task_id_insert_is_rejected_by_unique_constraint(self) -> None:
        """同一 ``task_id`` 二次插入被数据库唯一约束拒绝——一次问数至多一份文档。

        变异锚点：删掉迁移 0074 里 ``task_id`` 的 ``UNIQUE`` 约束，本用例应变红
        （第二次插入不再抛异常）。
        """

        import psycopg

        self._seed_pending_request(request_id="tdd-first")
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self._seed_pending_request(request_id="tdd-second")

    def test_write_terminal_event_document_request_insertion_is_idempotent(self) -> None:
        """``write_terminal_event`` 的幂等判定先于文档投递请求插入：同一次终态
        写入的网络重试（同 ``task_id``、相同 ``document_request``）不会产生第二
        行——第二次调用命中 ``idempotency_key`` 已存在，直接返回既有事件，压根
        不会走到插入这一步。
        """

        queue = PostgresTaskQueue(DSN)
        conversation_id = "cnv-idem"
        task_id = "tsk-idem"
        with connect(DSN) as connection:
            with connection.transaction():
                connection.execute(
                    """INSERT INTO conversation
                       (id,user_id,feishu_chat_id,feishu_thread_id,running_task_id)
                       VALUES (%s,'usr-doc',%s,%s,%s)""",
                    (conversation_id, f"chat-{conversation_id}", f"topic-{conversation_id}", task_id),
                )
                connection.execute(
                    """INSERT INTO task
                       (id,conversation_id,user_id,inbound_event_id,prompt,status,
                        target_worker_version,worker_id,heartbeat_at,attempts,content_expires_at)
                       VALUES (%s,%s,'usr-doc',%s,'问题','running','stable','worker-1',now(),1,now())""",
                    (task_id, conversation_id, f"event-{task_id}"),
                )

        request = {"title": "标题", "paragraphs": ["段落一"]}
        first = queue.write_terminal_event(
            task_id=task_id, worker_id="worker-1", terminal_kind="success",
            error_kind=None, content="答案", document_request=request,
        )
        second = queue.write_terminal_event(
            task_id=task_id, worker_id="worker-1", terminal_kind="success",
            error_kind=None, content="答案", document_request=request,
        )
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(
            self.scalar(
                "SELECT count(*) FROM task_document_delivery_request WHERE task_id = %s", (task_id,)
            ),
            1,
        )

    # -- ② 检查点恢复 ----------------------------------------------------------

    def test_checkpoint_recovery_never_creates_the_document_twice(self) -> None:
        """注入"建档成功后崩溃"：检查点已经单独提交了 ``document_id``，续做时从
        ``write_paragraphs`` 起步，绝不二次调用 ``create_document``（spy 断言
        恰一次），并且续做到底、最终成功。

        变异锚点：把 ``_process_claim`` 的 ``if document_id is None`` 判断去掉，
        本用例应变红（``create_calls`` 会变成 2）。
        """

        self._seed_pending_request(request_id="tdd-checkpoint")
        docx = _SpyDocx(
            create_result="doc-checkpoint-1",
            members=[{"member_type": "openid", "member_id": self.REQUESTER_OPEN_ID, "perm": "full_access"}],
        )
        notifier = _SpyNotifier()

        # 阶段一：手工认领 + 建档 + 检查点提交，模拟"崩溃"——不再往下走。
        claims = self.store.claim_pending(limit=1)
        self.assertEqual(len(claims), 1)
        claim = claims[0]
        document_id = docx.create_document(claim.title)
        self.store.mark_document_created(request_id=claim.id, document_id=document_id)
        self.assertEqual(docx.create_calls, ["标题"])

        # 崩溃后这一行停在 processing、document_id 已经非空。把 updated_at 回拨到
        # 回收窗口之外，模拟"卡住了一段时间"，让 reclaim_stale_processing 能捞到它
        # （真实场景里这一步由消费循环下一轮自己的时间推进触发，这里用回拨时间戳
        # 让测试不必真的等待 STALE_PROCESSING_AFTER_SECONDS）。
        self.execute(
            "UPDATE task_document_delivery_request SET updated_at = now() - interval '10 minutes' WHERE id = %s",
            (claim.id,),
        )

        # 阶段二：全新消费者续做——document_id 已经检查点持久化，四步从
        # write_paragraphs 起步。
        consumer = DocumentDeliveryConsumer(store=self.store, docx=docx, notifier=notifier)
        processed = consumer.run_once()

        self.assertEqual(processed, 1)
        self.assertEqual(docx.create_calls, ["标题"], "create_document 全程只应被调用一次")
        self.assertEqual(len(docx.write_calls), 1)
        self.assertEqual(len(docx.grant_calls), 1)
        self.assertEqual(len(docx.read_calls), 1)
        self.assertEqual(
            self.scalar("SELECT status FROM task_document_delivery_request WHERE id = %s", (claim.id,)),
            "succeeded",
        )
        self.assertEqual(
            self.scalar("SELECT document_id FROM task_document_delivery_request WHERE id = %s", (claim.id,)),
            "doc-checkpoint-1",
        )
        self.assertIsNotNone(
            self.scalar(
                "SELECT permission_confirmed_at FROM task_document_delivery_request WHERE id = %s", (claim.id,)
            )
        )
        self.assertEqual(len(notifier.sent), 1)
        self.assertEqual(notifier.sent[0][0], self.REQUESTER_OPEN_ID)

    # -- ③ read_members 判据 ----------------------------------------------------

    def test_missing_target_in_read_members_is_uncertain_not_succeeded(self) -> None:
        """``read_members`` 不含目标 open_id → ``uncertain``，不得判 ``succeeded``。

        变异锚点：把成功判据从"确认 full_access"改成"没抛异常就成功"，本用例
        应变红。
        """

        self._seed_pending_request(request_id="tdd-no-member")
        docx = _SpyDocx(create_result="doc-2", members=[])
        consumer = DocumentDeliveryConsumer(store=self.store, docx=docx, notifier=_SpyNotifier())

        processed = consumer.run_once()

        self.assertEqual(processed, 1)
        self.assertEqual(
            self.scalar("SELECT status FROM task_document_delivery_request WHERE id = 'tdd-no-member'"),
            "uncertain",
        )
        self.assertEqual(
            self.scalar("SELECT last_error FROM task_document_delivery_request WHERE id = 'tdd-no-member'"),
            "permission_not_confirmed",
        )

    def test_wrong_permission_tier_in_read_members_is_uncertain_not_succeeded(self) -> None:
        """``read_members`` 含目标 open_id 但档位不是 ``full_access`` → ``uncertain``。"""

        self._seed_pending_request(request_id="tdd-wrong-perm")
        docx = _SpyDocx(
            create_result="doc-3",
            members=[{"member_type": "openid", "member_id": self.REQUESTER_OPEN_ID, "perm": "view"}],
        )
        consumer = DocumentDeliveryConsumer(store=self.store, docx=docx, notifier=_SpyNotifier())

        consumer.run_once()

        self.assertEqual(
            self.scalar("SELECT status FROM task_document_delivery_request WHERE id = 'tdd-wrong-perm'"),
            "uncertain",
        )

    # -- ④ definite 失败 --------------------------------------------------------

    def test_definite_feishu_rejection_is_failed_with_last_error(self) -> None:
        """飞书明确拒绝（``FeishuDocxDeliveryError(definite=True)``）→ ``failed``，
        ``last_error`` 记错误分类码，不含正文。
        """

        self._seed_pending_request(request_id="tdd-definite")

        class RejectingDocx(_SpyDocx):
            def create_document(self, title: str) -> str:
                self.create_calls.append(title)
                raise FeishuDocxDeliveryError("feishu_code_99999", definite=True)

        docx = RejectingDocx()
        consumer = DocumentDeliveryConsumer(store=self.store, docx=docx, notifier=_SpyNotifier())

        consumer.run_once()

        self.assertEqual(
            self.scalar("SELECT status FROM task_document_delivery_request WHERE id = 'tdd-definite'"),
            "failed",
        )
        self.assertEqual(
            self.scalar("SELECT last_error FROM task_document_delivery_request WHERE id = 'tdd-definite'"),
            "feishu_code_99999",
        )
        # 明确失败不重试：attempts 已经因认领而 = 1，状态不会再被 claim_pending 选中。
        self.assertEqual(
            self.store.claim_pending(limit=10),
            [],
        )

    def test_indefinite_error_is_uncertain_not_failed(self) -> None:
        """结果不明（非 definite 的异常，例如网络类）→ ``uncertain``，不是 ``failed``
        ——白名单反转：只有明确拒绝才归 ``failed``。
        """

        self._seed_pending_request(request_id="tdd-indefinite")

        class FlakyDocx(_SpyDocx):
            def create_document(self, title: str) -> str:
                self.create_calls.append(title)
                raise FeishuDocxDeliveryError("transport_error", definite=False)

        docx = FlakyDocx()
        consumer = DocumentDeliveryConsumer(store=self.store, docx=docx, notifier=_SpyNotifier())

        consumer.run_once()

        self.assertEqual(
            self.scalar("SELECT status FROM task_document_delivery_request WHERE id = 'tdd-indefinite'"),
            "uncertain",
        )


@unittest.skipUnless(DSN, SKIP_REASON)
class WorkerDocumentRequestInsertionTestCase(unittest.TestCase):
    """⑥ worker 侧：终态成功且 document_request 非空 → 恰一行 pending；终态失败
    或字段为空 → 零行。真实 ``WorkerService.process_once()`` + 真实
    ``PostgresTaskQueue``，只有 Claude Agent SDK 执行器是假的（不需要模型额度）。
    """

    USER_ID = "usr-doc-worker"

    @classmethod
    def setUpClass(cls) -> None:
        assert DSN is not None
        ensure_production_schema(DSN)

    def setUp(self) -> None:
        assert DSN is not None
        reset_production_rows(DSN)
        _seed_user_mcp_config(self.USER_ID)
        self.queue = PostgresTaskQueue(DSN)
        with connect(DSN) as connection:
            with connection.transaction():
                connection.execute(
                    """INSERT INTO app_user
                       (id, feishu_open_id, feishu_user_id, feishu_union_id,
                        display_name, department, tenant_key, provisioning_state)
                       VALUES (%s,'ou-doc-worker','u-doc-worker','un-doc-worker',
                               '李四','数据部','tk-doc-worker','active')""",
                    (self.USER_ID,),
                )

    def _insert_queued_task(self, *, task_id: str, conversation_id: str) -> None:
        with connect(DSN) as connection:
            with connection.transaction():
                connection.execute(
                    """INSERT INTO conversation
                       (id,user_id,feishu_chat_id,feishu_thread_id,running_task_id)
                       VALUES (%s,%s,%s,%s,NULL)""",
                    (conversation_id, self.USER_ID, f"chat-{conversation_id}", f"topic-{conversation_id}"),
                )
                connection.execute(
                    """INSERT INTO task
                       (id,conversation_id,user_id,inbound_event_id,prompt,status,
                        target_worker_version,attempts,content_expires_at)
                       VALUES (%s,%s,%s,%s,'问题','queued','stable',0,now())""",
                    (task_id, conversation_id, self.USER_ID, f"event-{task_id}"),
                )

    def _worker_config(self) -> WorkerConfig:
        return WorkerConfig(
            question="",
            read_only_tools=("mcp__q__read",),
            trace_id="01J00000000000000000000000",
            turn_timeout_seconds=5.0,
            worker_id="worker-doc-test",
            target_worker_version="stable",
            heartbeat_interval_seconds=0.05,
            poll_interval_seconds=0.05,
            user_env_root=_USER_ENV_ROOT_DIR.name,
        )

    def _document_request_rows(self, task_id: str) -> list[tuple[str, str]]:
        with connect(DSN) as connection:
            rows = connection.execute(
                "SELECT status, title FROM task_document_delivery_request WHERE task_id = %s",
                (task_id,),
            ).fetchall()
        return [tuple(row) for row in rows]

    def test_successful_task_with_document_request_inserts_exactly_one_pending_row(self) -> None:
        task_id = "tsk-doc-success"
        self._insert_queued_task(task_id=task_id, conversation_id="cnv-doc-success")

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {"closed": True, "final_text": "问答结果", "session_id": None},
                    "failure": None,
                    "audit": {"denied_count": 0, "tool_result_count": 1},
                    "document_request": {"title": "月度报告", "paragraphs": ["第一段", "第二段"]},
                }

        service = WorkerService(
            config=self._worker_config(),
            queue=self.queue,
            executor_factory=lambda config, marker: Executor(),
        )
        asyncio.run(service.process_once())

        rows = self._document_request_rows(task_id)
        self.assertEqual(rows, [("pending", "月度报告")])
        with connect(DSN) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT requester_open_id FROM task_document_delivery_request WHERE task_id = %s",
                    (task_id,),
                ).fetchone()[0],
                "ou-doc-worker",
            )

    def test_failed_task_inserts_zero_document_request_rows(self) -> None:
        task_id = "tsk-doc-failed"
        self._insert_queued_task(task_id=task_id, conversation_id="cnv-doc-failed")

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {"closed": False, "final_text": "", "session_id": None},
                    "failure": {"code": "session_failed", "message": "boom"},
                }

        service = WorkerService(
            config=self._worker_config(),
            queue=self.queue,
            executor_factory=lambda config, marker: Executor(),
        )
        asyncio.run(service.process_once())

        self.assertEqual(self._document_request_rows(task_id), [])
        with connect(DSN) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM task WHERE id = %s", (task_id,)
                ).fetchone()[0],
                "awaiting_delivery",
            )

    def test_successful_task_without_document_request_inserts_zero_rows(self) -> None:
        task_id = "tsk-doc-empty"
        self._insert_queued_task(task_id=task_id, conversation_id="cnv-doc-empty")

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {"closed": True, "final_text": "普通问答结果", "session_id": None},
                    "failure": None,
                    "document_request": None,
                }

        service = WorkerService(
            config=self._worker_config(),
            queue=self.queue,
            executor_factory=lambda config, marker: Executor(),
        )
        asyncio.run(service.process_once())

        self.assertEqual(self._document_request_rows(task_id), [])


class TenantDomainNotConfiguredSentinelTest(unittest.TestCase):
    """⑤ 未配置 ``LINGXI_GATEWAY_TENANT_DOMAIN`` → 循环不注册，零行为差异。

    不接触数据库或网络：``assemble_document_delivery_consumer`` 在
    ``config.tenant_domain is None`` 时必须**在触碰任何飞书客户端或数据库连接
    构造之前**就返回 ``None``——用一个会在被调用时让测试失败的哨兵 DSN/凭据
    证明这一点。
    """

    def test_returns_none_without_touching_any_adapter(self) -> None:
        config = GatewayConfig(
            app_id="cli_sentinel",
            app_secret=_Secret("sentinel-secret"),
            postgres_dsn=_Secret("postgresql://sentinel-should-never-be-dialed/db"),
            tenant_domain=None,
        )

        result = assemble_document_delivery_consumer(config)

        self.assertIsNone(result)

    def test_configured_tenant_domain_assembles_a_real_consumer(self) -> None:
        """反向哨兵：配置了合法 ``tenant_domain`` 时确实装配出一个消费者（不是
        因为某种笔误让函数对任何输入都返回 ``None``）。不会真的发起网络请求
        （``LarkDocxDelivery``/``FeishuUserMessages`` 构造函数只存参数）。
        """

        config = GatewayConfig(
            app_id="cli_x",
            app_secret=_Secret("secret_x"),
            postgres_dsn=_Secret("postgresql://localhost/lingxi_test"),
            tenant_domain="example.feishu.cn",
        )

        result = assemble_document_delivery_consumer(config)

        self.assertIsInstance(result, DocumentDeliveryConsumer)


if __name__ == "__main__":
    unittest.main()
