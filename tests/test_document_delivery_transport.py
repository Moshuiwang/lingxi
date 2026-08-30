"""文档投递链路——gateway 独立消费循环 + 检查点恢复 + 读回判据（Issue #341 S-ES-3）。

覆盖任务卡「【测试】」列出的六项：

① ``task_document_delivery_request.task_id`` 的 UNIQUE 约束真实生效（幂等键，
   真库断言，不用 mock）；
② 检查点恢复：注入"建档成功后崩溃"，续做时不二次 ``create_document``（spy 断言
   恰一次调用），并从下一步续走到底，恢复后正文不重复（同一段落不出现两次）；
②b（Issue #353）幂等判据封死"外部写成功但检查点未推进"的崩溃窗口：注入"正文
   已经写入飞书、但进程在写检查点之前崩溃"，续做时读回正文根 block 判定非空，
   跳过重驱 ``write_paragraphs``，正文全程单份；
③ ``read_members`` 不含目标 open_id 或档位不是 ``full_access`` → ``uncertain``，
   不得判 ``succeeded``；
④ definite 错误（飞书明确拒绝）→ ``failed`` + ``last_error``；
⑤ 未配置 ``LINGXI_GATEWAY_TENANT_DOMAIN``（即 ``GatewayConfig.tenant_domain is
   None``）→ 循环不注册，零行为差异（哨兵，不需要真库）；
⑥ worker 侧：终态成功且报告契约 ``document_request`` 非空 → 恰一行 ``pending``；
   终态失败，或字段为空 → 零行。
⑦（opus 审查 P1-2）：一次被 ``reclaim_stale_processing`` 回收过的"慢消费者"最终
   跑完建档，写检查点时发现持有权已经丢失（这一行已经被另一次认领接手并跑出了
   不同的结论）→ 当场中止，全程 no-op（不写正文、不授权、不读回、不发通知），
   已经落下的真实终态原样保留、不被覆盖。

①-④、⑥-⑦ 需要真库（唯一约束、CHECK、以及 ``write_terminal_event`` 与终态事务的
真实交互不能靠假连接验证）；⑤ 是纯装配层判断，不接触数据库或网络。

变异锚点（任务卡登记，2026-08-27 实测还原）：
- 删掉迁移 0074 的 ``task_id UNIQUE`` 约束 → ①红；
- 把 ``DocumentDeliveryConsumer._process_claim`` 的 ``if document_id is None``
  判断去掉（每次都调用 ``create_document``）→ ②红；
- 把 ``_process_claim`` 里 ``recovering_from_checkpoint`` 的幂等判据整段删掉
  （恢复路径无条件重驱 ``write_paragraphs``）→ ②b 红（正文写两遍）；
- 把 ``recovering_from_checkpoint and bool(...)`` 的判空条件写反（改成
  ``not bool(...)``，即"非空才重写、空反而跳过"）→ ②b 红（方向倒了，正文写
  两遍且首次路径反而会漏写）；
- 让 ``recovering_from_checkpoint`` 恒为 ``False``（幂等判据分支永远不生效，
  等价于把整条恢复分支废掉）→ ②b 红（正文写两遍）；
- 把成功判据从"``read_members`` 确认 full_access"改成"四步没有抛异常就
  succeeded"（去掉 ``_has_confirmed_full_access`` 校验）→ ③红；
- 把 ``adapters/postgres_document_delivery.py`` 四个 ``mark_*`` 的 ``rowcount``
  检查删掉（静默无视 0 行）→ ⑦红（``docx.write_calls``/``grant_calls``/
  ``read_calls`` 会变成非空，``notifier.sent`` 也会非空，且 ``document_id`` 会被
  慢消费者的建档结果覆盖）。
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

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.adapters.feishu_docx_delivery import FeishuDocxDeliveryError, LarkDocxDelivery
from lingxi.adapters.feishu_user_message import FeishuUserMessages
from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_conversation import PostgresTaskQueue
from lingxi.adapters.postgres_document_delivery import (
    DocumentDeliveryClaim,
    PostgresDocumentDeliveryStore,
)
from lingxi.apps.gateway.config import GatewayConfig, _Secret
from lingxi.apps.gateway.document_delivery import (
    DocumentDeliveryConsumer,
    _has_confirmed_full_access,
    assemble_document_delivery_consumer,
)
from lingxi.apps.worker.config import WorkerConfig
from lingxi.apps.worker.service import WorkerService

DSN = os.environ.get("LINGXI_POSTGRES_DSN")
SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，文档投递链路的真库断言未验证"
    if not DSN
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，文档投递链路的真库断言未验证"
)
POSTGRES_READY = bool(DSN) and psycopg_available()

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
        self.read_body_children_calls: list[str] = []
        self._create_result = create_result
        self._members = members

    def create_document(self, title: str) -> str:
        self.create_calls.append(title)
        if callable(self._create_result):
            return self._create_result()
        return self._create_result

    def write_paragraphs(self, document_id: str, paragraphs: list[str]) -> None:
        self.write_calls.append((document_id, list(paragraphs)))

    def read_body_children(self, document_id: str) -> list[dict[str, Any]]:
        """Issue #353 幂等判据的假实现：一份文档"是否已经写过正文"完全由
        ``write_calls`` 里是否已经有过针对这个 ``document_id`` 的写入决定——不
        单独维护一份影子状态，就是"外部系统真实发生过什么"这件事本身，与生产
        实现（读飞书真实 block 列表）对应的语义完全一致：写没写过，读回就照实
        反映什么。
        """

        self.read_body_children_calls.append(document_id)
        return [
            {"block_type": 2, "text": text}
            for doc_id, paragraphs in self.write_calls
            if doc_id == document_id
            for text in paragraphs
        ]

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


class _RecordingDeliveryStore:
    """``PostgresDocumentDeliveryStore`` 的假实现，只记录调用、不接触数据库
    ——供不需要真库的 gateway 分支单测使用（Issue #408 正式方案接线）。"""

    def __init__(self) -> None:
        self.document_created: list[tuple[str, str, str | None]] = []
        self.succeeded: list[str] = []
        self.uncertain: list[tuple[str, str]] = []
        self.failed: list[tuple[str, str]] = []
        self.notified: list[str] = []

    def mark_document_created(
        self, *, request_id: str, document_id: str, resource_url: str | None = None
    ) -> None:
        self.document_created.append((request_id, document_id, resource_url))

    def mark_succeeded(self, *, request_id: str) -> None:
        self.succeeded.append(request_id)

    def mark_notified(self, *, request_id: str) -> None:
        self.notified.append(request_id)

    def mark_uncertain(self, *, request_id: str, last_error: str) -> None:
        self.uncertain.append((request_id, last_error))

    def mark_failed(self, *, request_id: str, last_error: str) -> None:
        self.failed.append((request_id, last_error))


@unittest.skipUnless(POSTGRES_READY, SKIP_REASON)
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

    def _seed_extra_task(self, task_id: str) -> None:
        """``task_document_delivery_request.task_id`` 是 UNIQUE + 外键——一个用例
        要种两行请求时，第二行必须挂在一个不同的 ``task`` 上，不能复用
        ``self.TASK_ID``。"""

        conversation_id = f"cnv-{task_id}"
        self.execute(
            """INSERT INTO conversation
               (id,user_id,feishu_chat_id,feishu_thread_id,running_task_id)
               VALUES (%s,'usr-doc',%s,%s,NULL)""",
            (conversation_id, f"chat-{task_id}", f"topic-{task_id}"),
        )
        self.execute(
            """INSERT INTO task
               (id,conversation_id,user_id,inbound_event_id,prompt,status,
                target_worker_version,attempts,content_expires_at)
               VALUES (%s,%s,'usr-doc',%s,'问题','succeeded','stable',1,now())""",
            (task_id, conversation_id, f"event-{task_id}"),
        )

    def _seed_pending_request(
        self,
        *,
        request_id: str = "tdd-1",
        document_id: str | None = None,
        task_id: str | None = None,
        markdown: str | None = None,
    ) -> None:
        self.execute(
            """INSERT INTO task_document_delivery_request
               (id, task_id, requester_open_id, title, paragraphs, document_id, markdown)
               VALUES (%s, %s, %s, '标题', %s, %s, %s)""",
            (
                request_id,
                task_id or self.TASK_ID,
                self.REQUESTER_OPEN_ID,
                json.dumps(["段落一", "段落二"], ensure_ascii=False),
                document_id,
                markdown,
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

    def test_a_null_open_id_requester_degrades_to_no_document_without_losing_the_answer(
        self,
    ) -> None:
        """P2-3（opus 审查）：提问用户的 ``feishu_open_id`` 为 ``NULL``
        （``app_user`` 全有全无 CHECK 下的合法 guest 态，如尚未匹配身份、或组织
        资料同步专用账号）时，迁移 0074 的 ``requester_open_id`` CHECK 会拒绝
        插入这一行——**这不能拖累已经真实产生的终态答案**：``write_terminal_
        event`` 必须照常成功、``task`` 照常转入 ``awaiting_delivery``，只有文档
        投递请求这一行不存在。

        变异锚点：把插入语句外面的 SAVEPOINT（嵌套 ``connection.transaction()``）
        去掉，本用例会变红——``CheckViolation`` 会一路冲垮外层事务，
        ``write_terminal_event`` 抛异常而不是返回一份成功的 ``AppendedEvent``，
        终态答案与 ``task`` 状态转移也会被一并回滚。
        """

        queue = PostgresTaskQueue(DSN)
        conversation_id = "cnv-null-open-id"
        task_id = "tsk-null-open-id"
        with connect(DSN) as connection:
            with connection.transaction():
                # 全有全无 CHECK：identity 五列必须同为 NULL 或同为非 NULL。
                connection.execute(
                    """INSERT INTO app_user
                       (id, provisioning_state, tenant_key)
                       VALUES ('usr-null-open-id', 'guest', NULL)"""
                )
                connection.execute(
                    """INSERT INTO conversation
                       (id,user_id,feishu_chat_id,feishu_thread_id,running_task_id)
                       VALUES (%s,'usr-null-open-id',%s,%s,%s)""",
                    (conversation_id, f"chat-{conversation_id}", f"topic-{conversation_id}", task_id),
                )
                connection.execute(
                    """INSERT INTO task
                       (id,conversation_id,user_id,inbound_event_id,prompt,status,
                        target_worker_version,worker_id,heartbeat_at,attempts,content_expires_at)
                       VALUES (%s,%s,'usr-null-open-id',%s,'问题','running','stable',
                               'worker-1',now(),1,now())""",
                    (task_id, conversation_id, f"event-{task_id}"),
                )

        appended = queue.write_terminal_event(
            task_id=task_id,
            worker_id="worker-1",
            terminal_kind="success",
            error_kind=None,
            content="答案",
            document_request={"title": "标题", "paragraphs": ["段落一"]},
        )

        self.assertIsNotNone(appended, "终态答案必须成功写入，不能被文档请求插入失败拖累")
        self.assertFalse(appended.duplicate)
        self.assertEqual(
            self.scalar("SELECT status FROM task WHERE id = %s", (task_id,)),
            "awaiting_delivery",
        )
        self.assertEqual(
            self.scalar(
                "SELECT content FROM task_delivery_event WHERE task_id = %s AND event_type = 'terminal'",
                (task_id,),
            ),
            "答案",
        )
        self.assertEqual(
            self.scalar(
                "SELECT count(*) FROM task_document_delivery_request WHERE task_id = %s", (task_id,)
            ),
            0,
        )

    def test_an_empty_dict_document_request_is_not_silently_skipped(self) -> None:
        """P2-3（opus 审查）：``document_request={}`` 是合法的非 ``None`` 值，
        但布尔求值为假——此前 ``document_request or sheet_request`` 会把它当成
        "没提供"，既不尝试插入，也不触发 ``worker.document_request_insert_
        failed`` 审计，调用方（未来任何按这条事件名聚合失败的运维消费方）永远
        看不到这类畸形输入。修复后必须走到插入这一步、因缺少 ``title``/
        ``paragraphs`` 被拒绝为 ``ValueError``，并落一条审计日志——终态答案本身
        仍然必须成功写入，不受这次注定失败的插入拖累（同 P2-3 既有的 null
        open_id 用例姿态）。

        变异锚点：把 ``delivery_request = document_request if document_request
        is not None else sheet_request`` 改回 ``document_request or sheet_
        request``，本用例会从"确实尝试插入并记审计"变红成"整段被静默跳过、
        assertLogs 收不到任何 ERROR"。
        """

        queue = PostgresTaskQueue(DSN)
        conversation_id = "cnv-empty-dict-request"
        task_id = "tsk-empty-dict-request"
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
                       VALUES (%s,%s,'usr-doc',%s,'问题','running','stable',
                               'worker-1',now(),1,now())""",
                    (task_id, conversation_id, f"event-{task_id}"),
                )

        with self.assertLogs(
            "lingxi.adapters.postgres_conversation._queue_outbox", level="ERROR"
        ) as logs:
            appended = queue.write_terminal_event(
                task_id=task_id,
                worker_id="worker-1",
                terminal_kind="success",
                error_kind=None,
                content="答案",
                document_request={},
            )

        self.assertIsNotNone(appended, "终态答案必须成功写入，不能被这次畸形插入拖累")
        self.assertFalse(appended.duplicate)
        self.assertTrue(
            any("document_request_insert_failed" in message for message in logs.output),
            f"必须记一条插入失败审计，实际日志：{logs.output}",
        )
        self.assertEqual(
            self.scalar(
                "SELECT count(*) FROM task_document_delivery_request WHERE task_id = %s", (task_id,)
            ),
            0,
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
        # Issue #353：这个用例里"崩溃"发生在检查点提交之后、write_paragraphs
        # 从未被调用过之前——恢复读回正文根 block 应为空，write_paragraphs 照常
        # 被调用恰一次，不多不少。
        self.assertEqual(docx.read_body_children_calls, ["doc-checkpoint-1"])
        self.assertEqual(len(docx.write_calls), 1)
        self.assertEqual(len(docx.grant_calls), 1)
        self.assertEqual(len(docx.read_calls), 1)
        # 恢复后正文不重复：全程写入的段落逐段恰好各出现一次，不是"段落一/段落二"
        # 各出现两次。
        all_written_paragraphs = [text for _, paragraphs in docx.write_calls for text in paragraphs]
        self.assertEqual(all_written_paragraphs, ["段落一", "段落二"])
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

    def test_checkpoint_recovery_after_body_already_written_skips_rewrite_and_stays_single_copy(
        self,
    ) -> None:
        """Issue #353 崩溃窗口用例：正文已经真实写入飞书，但进程在写下一个检查点
        （或任何后续动作）之前就崩溃了——不存在"写正文已完成"这个本地记录，唯一
        能封死这个窗口的判据是读外部系统的真实状态。

        阶段一手工模拟"外部写成功、进程随后崩溃"：认领、建档、写检查点、直接调用
        一次 ``docx.write_paragraphs``（代表这次外部调用确实成功了），到此为止不
        再往下走——不调用 grant_full_access/read_members/mark_succeeded，模拟
        进程就在这一刻死掉，这一行停在 ``processing``、``document_id`` 已经非空。

        阶段二回收 + 全新消费者续做：读回判定正文非空，跳过重驱
        ``write_paragraphs``，直接继续 grant_full_access/read_members，最终成功。

        变异锚点：把 ``_process_claim`` 幂等判据删掉/写反/废掉（见模块文档字符串
        变异锚点列表），本用例应变红——``write_calls`` 会变成 2（同一份正文写
        两遍）。
        """

        self._seed_pending_request(request_id="tdd-crash-window")
        docx = _SpyDocx(
            create_result="doc-crash-window-1",
            members=[{"member_type": "openid", "member_id": self.REQUESTER_OPEN_ID, "perm": "full_access"}],
        )
        notifier = _SpyNotifier()

        # 阶段一：认领 + 建档 + 检查点提交 + 正文外部写入成功，随后"崩溃"。
        claims = self.store.claim_pending(limit=1)
        self.assertEqual(len(claims), 1)
        claim = claims[0]
        document_id = docx.create_document(claim.title)
        self.store.mark_document_created(request_id=claim.id, document_id=document_id)
        docx.write_paragraphs(document_id, list(claim.paragraphs))
        self.assertEqual(docx.create_calls, ["标题"])
        self.assertEqual(len(docx.write_calls), 1, "阶段一：外部写入确实发生了恰一次")

        # 回拨 updated_at 模拟"卡住了一段时间"，让 reclaim_stale_processing 能捞到。
        self.execute(
            "UPDATE task_document_delivery_request SET updated_at = now() - interval '10 minutes' WHERE id = %s",
            (claim.id,),
        )

        # 阶段二：全新消费者续做——同一个 docx（代表同一个外部飞书系统），
        # document_id 已经检查点持久化，读回判定正文非空，跳过重驱写正文。
        consumer = DocumentDeliveryConsumer(store=self.store, docx=docx, notifier=notifier)
        processed = consumer.run_once()

        self.assertEqual(processed, 1)
        self.assertEqual(docx.create_calls, ["标题"], "create_document 全程只应被调用一次")
        self.assertEqual(docx.read_body_children_calls, ["doc-crash-window-1"])
        self.assertEqual(
            len(docx.write_calls), 1, "write_paragraphs 全程只应被调用一次——恢复路径必须跳过重驱"
        )
        all_written_paragraphs = [text for _, paragraphs in docx.write_calls for text in paragraphs]
        self.assertEqual(all_written_paragraphs, ["段落一", "段落二"], "正文不得重复出现")
        self.assertEqual(len(docx.grant_calls), 1)
        self.assertEqual(len(docx.read_calls), 1)
        self.assertEqual(
            self.scalar("SELECT status FROM task_document_delivery_request WHERE id = %s", (claim.id,)),
            "succeeded",
        )
        self.assertEqual(
            self.scalar("SELECT document_id FROM task_document_delivery_request WHERE id = %s", (claim.id,)),
            "doc-crash-window-1",
        )
        self.assertEqual(len(notifier.sent), 1)

    def test_first_time_path_never_calls_read_body_children_and_behaves_unchanged(
        self,
    ) -> None:
        """首次路径回归（Issue #353）：``document_id`` 本次调用才建出来时，行为
        必须与修复前逐字相同——不多调用一次 ``read_body_children``，``write_
        paragraphs`` 正常无条件被调用恰一次。"""

        self._seed_pending_request(request_id="tdd-first-time")
        docx = _SpyDocx(
            create_result="doc-first-time-1",
            members=[{"member_type": "openid", "member_id": self.REQUESTER_OPEN_ID, "perm": "full_access"}],
        )
        notifier = _SpyNotifier()
        consumer = DocumentDeliveryConsumer(store=self.store, docx=docx, notifier=notifier)

        processed = consumer.run_once()

        self.assertEqual(processed, 1)
        self.assertEqual(docx.create_calls, ["标题"])
        self.assertEqual(docx.read_body_children_calls, [], "首次路径不得多做这次读回")
        self.assertEqual(len(docx.write_calls), 1)
        self.assertEqual(
            self.scalar("SELECT status FROM task_document_delivery_request WHERE id = 'tdd-first-time'"),
            "succeeded",
        )

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
        ``last_error`` 记错误分类码，不含正文。opus 审查 R-1 第 3 条：用户本人也要
        收到一条对应的追加消息（此前只有 succeeded 会发）。
        """

        self._seed_pending_request(request_id="tdd-definite")

        class RejectingDocx(_SpyDocx):
            def create_document(self, title: str) -> str:
                self.create_calls.append(title)
                raise FeishuDocxDeliveryError("feishu_code_99999", definite=True)

        docx = RejectingDocx()
        notifier = _SpyNotifier()
        alerts: list[tuple[str, str]] = []
        consumer = DocumentDeliveryConsumer(
            store=self.store, docx=docx, notifier=notifier, on_alert=lambda kind, task_id: alerts.append((kind, task_id))
        )

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
        # R-1 独立审核（必修 1）：definite 失败必须记一条告警——此前只落日志。
        self.assertIn(("document_delivery_failed", "tsk-doc-1"), alerts)
        # R-1（必修 3）：用户本人收到一条对应文案的追加消息，与成功那一路
        # （``document-ready:``）用不同的 dedupe 前缀，不会互相去重掉。
        self.assertEqual(len(notifier.sent), 1)
        open_id, text, dedupe_key = notifier.sent[0]
        self.assertEqual(open_id, self.REQUESTER_OPEN_ID)
        self.assertEqual(text, "抱歉，你要的文档生成失败了。你可以重新发起问数再试一次；问题已记录。")
        self.assertEqual(dedupe_key, "document-failed:tdd-definite")

    def test_indefinite_error_is_uncertain_not_failed(self) -> None:
        """结果不明（非 definite 的异常，例如网络类）→ ``uncertain``，不是 ``failed``
        ——白名单反转：只有明确拒绝才归 ``failed``。opus 审查 R-1 第 3 条：用户本人
        也要收到一条措辞与 failed 区分的追加消息（不建议直接重试）。
        """

        self._seed_pending_request(request_id="tdd-indefinite")

        class FlakyDocx(_SpyDocx):
            def create_document(self, title: str) -> str:
                self.create_calls.append(title)
                raise FeishuDocxDeliveryError("transport_error", definite=False)

        docx = FlakyDocx()
        notifier = _SpyNotifier()
        consumer = DocumentDeliveryConsumer(store=self.store, docx=docx, notifier=notifier)

        consumer.run_once()

        self.assertEqual(
            self.scalar("SELECT status FROM task_document_delivery_request WHERE id = 'tdd-indefinite'"),
            "uncertain",
        )
        self.assertEqual(len(notifier.sent), 1)
        open_id, text, dedupe_key = notifier.sent[0]
        self.assertEqual(open_id, self.REQUESTER_OPEN_ID)
        self.assertEqual(text, "文档生成结果暂无法确认，已转人工核对。")
        self.assertEqual(dedupe_key, "document-uncertain:tdd-indefinite")

    def test_a_deterministic_precondition_valueerror_is_failed_not_uncertain(self) -> None:
        """P3 顺手（opus 审查）：``adapters.feishu_docx_delivery`` 的入参校验在
        发出任何 HTTP 请求之前失败（例如 ``requester_open_id`` 形状不对），抛的
        是 ``ValueError``——这类"没有任何请求真的发出去、重放必然同一个结论"的
        确定性配置错误必须归 ``failed``，不是 ``uncertain``（后者暗示"可能已经
        生效，需要人工核对飞书那一侧"，对这类从未发出请求的情形是误导）。
        """

        self._seed_pending_request(request_id="tdd-bad-openid")

        class BadOpenIdDocx(_SpyDocx):
            def grant_full_access(self, document_id: str, open_id: str) -> None:
                self.grant_calls.append((document_id, open_id))
                raise ValueError("open_id 必须是飞书用户 open_id，不回显收到的值")

        docx = BadOpenIdDocx(create_result="doc-bad-openid")
        notifier = _SpyNotifier()
        alerts: list[tuple[str, str]] = []
        consumer = DocumentDeliveryConsumer(
            store=self.store,
            docx=docx,
            notifier=notifier,
            on_alert=lambda kind, task_id: alerts.append((kind, task_id)),
        )

        consumer.run_once()

        self.assertEqual(
            self.scalar("SELECT status FROM task_document_delivery_request WHERE id = 'tdd-bad-openid'"),
            "failed",
        )
        self.assertEqual(
            self.scalar(
                "SELECT last_error FROM task_document_delivery_request WHERE id = 'tdd-bad-openid'"
            ),
            "ValueError",
        )
        self.assertIn(("document_delivery_failed", "tsk-doc-1"), alerts)
        self.assertEqual(len(notifier.sent), 1)
        self.assertEqual(notifier.sent[0][1], "抱歉，你要的文档生成失败了。你可以重新发起问数再试一次；问题已记录。")

    # -- ⑦ P1-2：回收后慢消费者全程 no-op --------------------------------------

    def test_slow_consumer_after_reclaim_is_a_total_no_op_and_sends_no_notice(self) -> None:
        """一个"慢消费者"认领了一行，还没来得及提交建档检查点就被
        ``reclaim_stale_processing`` 判定为卡住并回收；这一行随后被一个更快的
        消费者重新认领并跑到 ``succeeded``。慢消费者终于跑完自己的
        ``create_document`` 后尝试提交检查点——此时持有权早已不在它手里，必须
        当场中止、全程 no-op：不覆盖已经真实生效的终态，不写正文、不授权、
        不读回、不发通知。

        变异锚点：删掉 ``mark_document_created`` 的 rowcount 检查，本用例会变红
        （``write_calls``/``grant_calls``/``read_calls`` 变成非空，
        ``notifier.sent`` 也会非空，且 ``document_id`` 会被慢消费者的建档结果
        "doc-slow" 覆盖，快消费者留下的 "doc-fast" 与 ``succeeded`` 终态都会
        被悄悄破坏）。
        """

        self._seed_pending_request(request_id="tdd-slow")

        # 阶段一：慢消费者认领（attempts 1 -> processing，document_id 仍为 None）。
        slow_claims = self.store.claim_pending(limit=1)
        self.assertEqual(len(slow_claims), 1)
        slow_claim = slow_claims[0]
        self.assertIsNone(slow_claim.document_id)

        # 让它"卡住"：回拨 updated_at 到回收窗口之外，reclaim 把它退回 pending。
        self.execute(
            "UPDATE task_document_delivery_request SET updated_at = now() - interval '10 minutes' "
            "WHERE id = %s",
            (slow_claim.id,),
        )
        requeued, failed = self.store.reclaim_stale_processing()
        self.assertEqual((requeued, failed), (1, 0))

        # 阶段二：一个更快的消费者重新认领并跑完全部四步、落 succeeded。
        fast_claims = self.store.claim_pending(limit=1)
        self.assertEqual(len(fast_claims), 1)
        fast_claim = fast_claims[0]
        self.store.mark_document_created(request_id=fast_claim.id, document_id="doc-fast")
        self.store.mark_succeeded(request_id=fast_claim.id)
        self.assertEqual(
            self.scalar("SELECT status FROM task_document_delivery_request WHERE id = 'tdd-slow'"),
            "succeeded",
        )

        # 阶段三：慢消费者这才跑完自己的 create_document，尝试提交检查点——用它
        # 自己在阶段一拿到的、已经过期的 claim 对象（document_id 仍是 None）。
        docx = _SpyDocx(create_result="doc-slow")
        notifier = _SpyNotifier()
        consumer = DocumentDeliveryConsumer(store=self.store, docx=docx, notifier=notifier)

        consumer._process_claim(slow_claim)

        self.assertEqual(docx.create_calls, ["标题"], "慢消费者自己的建档调用确实发生了")
        self.assertEqual(docx.write_calls, [], "不得续写正文")
        self.assertEqual(docx.grant_calls, [], "不得续授权")
        self.assertEqual(docx.read_calls, [], "不得续读回")
        self.assertEqual(notifier.sent, [], "不得发出任何通知")
        # 快消费者落下的真实终态原样保留，没有被慢消费者的迟到写入覆盖。
        self.assertEqual(
            self.scalar("SELECT status FROM task_document_delivery_request WHERE id = 'tdd-slow'"),
            "succeeded",
        )
        self.assertEqual(
            self.scalar("SELECT document_id FROM task_document_delivery_request WHERE id = 'tdd-slow'"),
            "doc-fast",
        )

    # -- ⑧ R-2 死信 + `V-投递-06` 正文到期擦除（opus 审查 R-2/P1-3） -------------

    def test_fail_expired_pending_converts_only_the_stale_unconsumed_row(self) -> None:
        """R-2 死信面：``pending`` 超过窗口仍未被任何消费循环认领 → 转
        ``failed``（``last_error = 'pending_expired_unconsumed'``）；窗口内的行
        原样不动。"""

        self._seed_pending_request(request_id="tdd-dead-letter")
        self._seed_extra_task("tsk-doc-fresh")
        self._seed_pending_request(request_id="tdd-fresh", task_id="tsk-doc-fresh")
        self.execute(
            "UPDATE task_document_delivery_request SET created_at = now() - interval '31 minutes' "
            "WHERE id = 'tdd-dead-letter'"
        )

        from datetime import timedelta

        converted = self.store.fail_expired_pending(older_than=timedelta(minutes=30))

        self.assertEqual(converted, 1)
        self.assertEqual(
            self.scalar("SELECT status FROM task_document_delivery_request WHERE id = 'tdd-dead-letter'"),
            "failed",
        )
        self.assertEqual(
            self.scalar(
                "SELECT last_error FROM task_document_delivery_request WHERE id = 'tdd-dead-letter'"
            ),
            "pending_expired_unconsumed",
        )
        # 窗口内的行不受影响：仍然是 pending，认领谓词照常能捞到它。
        self.assertEqual(
            self.scalar("SELECT status FROM task_document_delivery_request WHERE id = 'tdd-fresh'"),
            "pending",
        )

    def test_redact_expired_content_empties_title_and_paragraphs_but_keeps_operational_facts(
        self,
    ) -> None:
        """``V-投递-06``：过了 24 小时上限的正文被擦空（``title``/``paragraphs``/
        迁移 0079 新增的 ``markdown``），``status``/``document_id``/``attempts``
        等运行事实原样保留；未到期的行一个字都不动。"""

        self._seed_pending_request(
            request_id="tdd-content-expired", document_id="doc-kept", markdown="# 标题\n\n正文"
        )
        self._seed_extra_task("tsk-doc-content-fresh")
        self._seed_pending_request(request_id="tdd-content-fresh", task_id="tsk-doc-content-fresh")
        self.execute(
            "UPDATE task_document_delivery_request SET status = 'failed', attempts = 3, "
            "last_error = 'feishu_code_1', content_expires_at = now() - interval '1 hour' "
            "WHERE id = 'tdd-content-expired'"
        )

        redacted = self.store.redact_expired_content()

        self.assertEqual(redacted, 1)
        row = self.execute_and_fetchone(
            "SELECT title, paragraphs, status, document_id, attempts, last_error, markdown "
            "FROM task_document_delivery_request WHERE id = 'tdd-content-expired'"
        )
        self.assertEqual(row[0], "")
        self.assertEqual(row[1], [])
        # 运行事实原样保留——只擦正文，不擦"发生过什么"。
        self.assertEqual(row[2], "failed")
        self.assertEqual(row[3], "doc-kept")
        self.assertEqual(row[4], 3)
        self.assertEqual(row[5], "feishu_code_1")
        # markdown 原文与 title/paragraphs 同一次擦除一起清，不留在库里过期不清。
        self.assertIsNone(row[6])
        # 未到期的行一个字都不动。
        self.assertEqual(
            self.scalar("SELECT title FROM task_document_delivery_request WHERE id = 'tdd-content-fresh'"),
            "标题",
        )
        # 已经擦过的行不会被同一次调用重复计数（谓词是 title <> ''）。
        self.assertEqual(self.store.redact_expired_content(), 0)

    def test_title_paragraphs_shape_check_rejects_half_redacted_rows(self) -> None:
        """P1-3 顺手：真实内容态（非空标题 + 非空段落数组）与擦除态（两者都为空）
        都合法，"半擦"（只擦了一边）被数据库层直接拒绝。"""

        import psycopg

        with self.assertRaises(psycopg.errors.CheckViolation):
            self.execute(
                """INSERT INTO task_document_delivery_request
                   (id, task_id, requester_open_id, title, paragraphs, document_id)
                   VALUES ('tdd-half-a', %s, %s, '', %s, NULL)""",
                (self.TASK_ID, self.REQUESTER_OPEN_ID, json.dumps(["段落一"])),
            )

    def test_title_paragraphs_shape_check_rejects_empty_paragraphs_array_with_a_title(
        self,
    ) -> None:
        import psycopg

        with self.assertRaises(psycopg.errors.CheckViolation):
            self.execute(
                """INSERT INTO task_document_delivery_request
                   (id, task_id, requester_open_id, title, paragraphs, document_id)
                   VALUES ('tdd-half-b', %s, %s, '标题', '[]'::jsonb, NULL)""",
                (self.TASK_ID, self.REQUESTER_OPEN_ID),
            )

    def test_markdown_column_check_rejects_sheet_rows_carrying_a_markdown_value(self) -> None:
        """迁移 0079 的 CHECK（``delivery_type = 'docx' OR markdown IS NULL``）：
        ``sheet`` 类型没有"markdown 排版"这个概念，数据库层直接拒绝把这一列的
        值写进 sheet 行——与迁移 0078 ``resource_url`` 的既有同型 CHECK 同一
        姿态。"""

        import psycopg

        with self.assertRaises(psycopg.errors.CheckViolation):
            self.execute(
                """INSERT INTO task_document_delivery_request
                   (id, task_id, requester_open_id, title, paragraphs, delivery_type, markdown)
                   VALUES ('tdd-sheet-markdown', %s, %s, '标题', %s, 'sheet', '# 不该出现在表格行')""",
                (
                    self.TASK_ID,
                    self.REQUESTER_OPEN_ID,
                    json.dumps([["月份", "销售额"]], ensure_ascii=False),
                ),
            )

    def test_markdown_column_accepts_a_docx_row_with_a_non_null_value_as_a_positive_control(
        self,
    ) -> None:
        """反向哨兵：不是这一列对任何输入都拒绝——docx 行携带 markdown 是合法
        的真实内容态。"""

        self._seed_pending_request(request_id="tdd-docx-markdown", markdown="# 标题\n\n正文")

        self.assertEqual(
            self.scalar("SELECT markdown FROM task_document_delivery_request WHERE id = 'tdd-docx-markdown'"),
            "# 标题\n\n正文",
        )

    # -- ⑨ P2-2：通知未确认送达补发 ---------------------------------------------

    def test_successful_notify_sets_notified_at(self) -> None:
        """成功路径下通知一旦确认送达，``notified_at`` 立刻置位——补发扫描
        （``notified_at IS NULL``）不会再捞到这一行。"""

        self._seed_pending_request(request_id="tdd-notify-ok")
        docx = _SpyDocx(
            create_result="doc-notify-ok",
            members=[{"member_type": "openid", "member_id": self.REQUESTER_OPEN_ID, "perm": "full_access"}],
        )
        notifier = _SpyNotifier()
        consumer = DocumentDeliveryConsumer(store=self.store, docx=docx, notifier=notifier)

        consumer.run_once()

        self.assertEqual(len(notifier.sent), 1)
        self.assertIsNotNone(
            self.scalar(
                "SELECT notified_at FROM task_document_delivery_request WHERE id = 'tdd-notify-ok'"
            )
        )

    def test_run_once_resends_a_stale_unnotified_success_before_claiming_new_rows(self) -> None:
        """P2-2：``succeeded`` 但通知从未确认送达、且已经过了退避窗口的行，
        ``run_once`` 优先补发；补发成功后置位 ``notified_at``，同一行不会再被
        下一轮捞到。
        """

        self._seed_pending_request(request_id="tdd-unnotified", document_id="doc-unnotified")
        self.execute(
            "UPDATE task_document_delivery_request "
            "SET status = 'succeeded', permission_confirmed_at = now(), "
            "updated_at = now() - interval '11 minutes' WHERE id = 'tdd-unnotified'"
        )
        notifier = _SpyNotifier()
        consumer = DocumentDeliveryConsumer(store=self.store, docx=_SpyDocx(), notifier=notifier)

        consumer.run_once()

        self.assertEqual(len(notifier.sent), 1)
        open_id, text, dedupe_key = notifier.sent[0]
        self.assertEqual(open_id, self.REQUESTER_OPEN_ID)
        self.assertIn("doc-unnotified", text)
        self.assertEqual(dedupe_key, "document-ready:tdd-unnotified")
        self.assertIsNotNone(
            self.scalar(
                "SELECT notified_at FROM task_document_delivery_request WHERE id = 'tdd-unnotified'"
            )
        )

        # 下一轮：notified_at 已经非空，不再被补发扫描捞到，不重复发送。
        notifier.sent.clear()
        consumer.run_once()
        self.assertEqual(notifier.sent, [])

    def test_a_fresh_unnotified_success_within_the_backoff_window_is_not_resent_yet(
        self,
    ) -> None:
        """还没到退避窗口（10 分钟）的未确认通知不会被本轮补发——避免对一次刚刚
        失败、原因可能还没消失的通知无节制重试。"""

        self._seed_pending_request(request_id="tdd-fresh-unnotified", document_id="doc-fresh")
        self.execute(
            "UPDATE task_document_delivery_request "
            "SET status = 'succeeded', permission_confirmed_at = now(), updated_at = now() "
            "WHERE id = 'tdd-fresh-unnotified'"
        )
        notifier = _SpyNotifier()
        consumer = DocumentDeliveryConsumer(store=self.store, docx=_SpyDocx(), notifier=notifier)

        consumer.run_once()

        self.assertEqual(notifier.sent, [])
        self.assertIsNone(
            self.scalar(
                "SELECT notified_at FROM task_document_delivery_request WHERE id = 'tdd-fresh-unnotified'"
            )
        )

    def test_mark_notified_is_idempotent_and_never_overwrites_the_first_confirmation(
        self,
    ) -> None:
        self._seed_pending_request(request_id="tdd-mark-notified", document_id="doc-x")
        self.execute(
            "UPDATE task_document_delivery_request "
            "SET status = 'succeeded', permission_confirmed_at = now() "
            "WHERE id = 'tdd-mark-notified'"
        )

        self.store.mark_notified(request_id="tdd-mark-notified")
        first = self.scalar(
            "SELECT notified_at FROM task_document_delivery_request WHERE id = 'tdd-mark-notified'"
        )
        self.assertIsNotNone(first)

        self.store.mark_notified(request_id="tdd-mark-notified")  # 第二次调用：no-op
        second = self.scalar(
            "SELECT notified_at FROM task_document_delivery_request WHERE id = 'tdd-mark-notified'"
        )
        self.assertEqual(first, second)

    def execute_and_fetchone(self, sql: str, parameters: tuple[object, ...] = ()) -> tuple:
        assert DSN is not None
        with connect(DSN) as connection:
            return connection.execute(sql, parameters).fetchone()


@unittest.skipUnless(POSTGRES_READY, SKIP_REASON)
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

    def test_successful_task_with_document_request_markdown_persists_the_raw_markdown_column(
        self,
    ) -> None:
        """迁移 0079（Issue #408 正式方案接线）：报告契约里的 ``markdown`` 字段
        原样落进新增的 ``markdown`` 列，段落列同时照常落库——两列并存，互不
        替代。"""

        task_id = "tsk-doc-markdown"
        self._insert_queued_task(task_id=task_id, conversation_id="cnv-doc-markdown")

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {"closed": True, "final_text": "问答结果", "session_id": None},
                    "failure": None,
                    "audit": {"denied_count": 0, "tool_result_count": 1},
                    "document_request": {
                        "title": "月度报告",
                        "paragraphs": ["第一段", "第二段"],
                        "markdown": "第一段\n\n第二段",
                    },
                }

        service = WorkerService(
            config=self._worker_config(),
            queue=self.queue,
            executor_factory=lambda config, marker: Executor(),
        )
        asyncio.run(service.process_once())

        with connect(DSN) as connection:
            row = connection.execute(
                "SELECT status, title, paragraphs, markdown "
                "FROM task_document_delivery_request WHERE task_id = %s",
                (task_id,),
            ).fetchone()
        self.assertEqual(row[0], "pending")
        self.assertEqual(row[1], "月度报告")
        self.assertEqual(row[2], ["第一段", "第二段"])
        self.assertEqual(row[3], "第一段\n\n第二段")

    def test_successful_task_with_document_request_missing_markdown_leaves_the_column_null(
        self,
    ) -> None:
        """``markdown`` 是段落之外的附加值：报告契约里缺失这个字段（旧形状、或
        取不到）不拒绝整条登记请求，只是这一列落 ``NULL``——段落照常落库，
        gateway 侧据此回退段落路径。"""

        task_id = "tsk-doc-no-markdown"
        self._insert_queued_task(task_id=task_id, conversation_id="cnv-doc-no-markdown")

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

        with connect(DSN) as connection:
            row = connection.execute(
                "SELECT status, markdown FROM task_document_delivery_request WHERE task_id = %s",
                (task_id,),
            ).fetchone()
        self.assertEqual(row[0], "pending")
        self.assertIsNone(row[1])

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


class _RecordingUserMessageTransport:
    """真实形状假传输层：按顺序返回预置响应，记录每次调用（同
    ``test_feishu_docx_delivery.py::RecordingTransport`` 的形状）。"""

    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, object] | None, str | None]] = []

    def __call__(self, method: str, url: str, *, body=None, token=None):
        self.calls.append((method, url, body, token))
        if not self._responses:
            raise AssertionError("假传输层收到了超出预置数量的调用")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _real_shape_tenant_token_response() -> dict[str, Any]:
    """``/auth/v3/tenant_access_token/internal`` 的真实响应形状。"""

    return {"code": 0, "msg": "ok", "tenant_access_token": "t-fake-tenant-access-token", "expire": 7200}


def _real_shape_send_message_response() -> dict[str, Any]:
    """``/im/v1/messages`` 的真实响应形状（字段取自飞书开放平台文档；本模块的
    调用方只检查 ``code``，不解析 ``data``，这里带全字段只为形状真实）。"""

    return {
        "code": 0,
        "msg": "success",
        "data": {
            "message_id": "om_fake_message_id",
            "root_id": "om_fake_message_id",
            "parent_id": "",
            "msg_type": "text",
            "create_time": "1700000000000",
            "update_time": "1700000000000",
            "deleted": False,
            "updated": False,
            "chat_id": "oc_fake_chat_id",
            "sender": {"id": "cli_fake_app", "id_type": "app_id", "sender_type": "app", "tenant_key": "tk_fake"},
        },
    }


class _NoopNotifyStore:
    """只实现 ``_send_ready_notice``/``_uncertain``/``_fail`` 会用到的那几个
    方法——本测试类不接触数据库，专门钉住"通知发送路径本身是否真的能调通"这一
    件事，不是终态落库的正确性（那部分见 ``DocumentDeliveryTransportTestCase``
    的真库用例）。"""

    def __init__(self) -> None:
        self.notified: list[str] = []

    def mark_notified(self, *, request_id: str) -> None:
        self.notified.append(request_id)

    def mark_uncertain(self, *, request_id: str, last_error: str) -> None:
        del request_id, last_error

    def mark_failed(self, *, request_id: str, last_error: str) -> None:
        del request_id, last_error


def _notify_test_claim(
    *, requester_open_id: str, claim_id: str = "tdd-notify-1", task_id: str = "tsk-notify-1"
) -> DocumentDeliveryClaim:
    return DocumentDeliveryClaim(
        id=claim_id,
        task_id=task_id,
        requester_open_id=requester_open_id,
        title="标题",
        paragraphs=("正文",),
        document_id="doc-1",
        attempts=1,
    )


class RealNotifierWiringTest(unittest.TestCase):
    """真实装配 + 真实形状：证明 gateway 侧文档投递用户通知发送路径没有断线
    （2026-08-27 编排者 stage 自测排查项之一，不接触数据库或网络）。

    **背景与复核结论**：stage 真实日志出现过
    ``alert.recorded {'event_type': 'document_delivery_uncertain.feishu_send_failed', ...}``，
    起初怀疑是 ``_send_terminal_notice``/``_send_ready_notice`` 的用户通知发送
    路径本身断线（候选：``FeishuUserMessages`` 依赖注入缺失、``receive_id_type``
    口径、``send_text`` 方法签名不匹配）。逐项核对**未发现**这类断线：
    ``assemble_document_delivery_consumer`` 对 ``FeishuUserMessages`` 的构造参数
    与其 ``__init__`` 签名逐字匹配，``send_text`` 的调用点关键字参数
    （``open_id``/``text``/``dedupe_key``）与其方法签名逐字匹配，
    ``receive_id_type=open_id`` 写死在 URL 上未被参数化误传。

    真正原因是：``event_type`` 里的 ``feishu_send_failed`` 不是"这次消息发送调用
    失败了"的字面意思，而是 ``core/alerting.py::AlertingDuty.
    delivery_alert_callback`` 把**所有**投递告警统一归入的历史沿用伞形标签
    ``AlertKind.FEISHU_SEND_FAILED``（见该方法文档字符串："语义上都是投递/飞书
    发送这条链路出了结果不明或失败"）——``DocumentDeliveryConsumer._uncertain``
    无条件调用 ``self._alert("document_delivery_uncertain", claim.task_id)``，
    不管随后的通知是否真的发出去都会产生这一条审计行（本地可逐字复现：单独调用
    ``AlertingDuty.delivery_alert_callback()("document_delivery_uncertain",
    task_id)`` 即得到同一个 ``event_type``，不需要触碰 notifier）。触发这条
    ``uncertain`` 判定的真正故障是 defect 1（``read_members`` 读错字段名，见
    ``tests/test_feishu_docx_delivery.py::ReadMembersTest``）——四步全成功后
    读回必然 ``LookupError``，白名单反转把它判成 ``uncertain``。defect 1 修好
    后，真实成功的交付不会再落进这条分支。

    本测试类用与 ``assemble_document_delivery_consumer`` 完全相同的构造方式
    （只多注入一个假传输层，不发真实网络请求）装配一个真实的
    ``FeishuUserMessages``，驱动 ``_send_ready_notice``/``_uncertain``/
    ``_fail`` 三条通知路径各真实走一遍，用真实形状响应证明它们都不抛异常、也
    不会额外触发 ``document_delivery_notice_failed`` 告警。

    变异锚点：把 ``document_delivery.py`` 任一处 ``self._notifier.send_text``
    的关键字参数名改错（例如 ``dedupe_key`` 改成 ``dedup_key``），或把
    ``assemble_document_delivery_consumer`` 里 ``FeishuUserMessages(...)`` 的
    某个构造参数删掉/改错，本类用例会从"零 notice_failed 告警"变红成抛出
    ``TypeError``/多出一条 ``document_delivery_notice_failed`` 告警。
    """

    OPEN_ID = "ou_real_shaped_user"
    BASE_URL = "https://feishu.invalid/open-apis"

    def _real_notifier(self, transport: _RecordingUserMessageTransport) -> FeishuUserMessages:
        # 与 assemble_document_delivery_consumer 里的构造逐字同形（app_id/
        # app_secret/uuid_prefix 取值不同不影响验证目标：真实值来自配置注入，
        # 这里只需要合法形状）。
        return FeishuUserMessages(
            base_url=self.BASE_URL,
            app_id="cli_fake_app",
            app_secret="fake_app_secret",
            uuid_prefix="lingxi-doc-ready-",
            transport=transport,
        )

    def test_ready_notice_sends_through_the_real_adapter_without_error(self) -> None:
        transport = _RecordingUserMessageTransport(
            [_real_shape_tenant_token_response(), _real_shape_send_message_response()]
        )
        store = _NoopNotifyStore()
        alerts: list[tuple[str, str]] = []
        consumer = DocumentDeliveryConsumer(
            store=store,
            docx=_SpyDocx(),
            notifier=self._real_notifier(transport),
            on_alert=lambda kind, task_id: alerts.append((kind, task_id)),
        )

        consumer._send_ready_notice(
            request_id="tdd-ready-1",
            task_id="tsk-ready-1",
            requester_open_id=self.OPEN_ID,
            document_id="doc-ready-1",
        )

        self.assertEqual(alerts, [], "真实形状下不应该触发 notice_failed 告警")
        self.assertEqual(store.notified, ["tdd-ready-1"])
        self.assertEqual(len(transport.calls), 2)
        method, url, body, token = transport.calls[1]
        self.assertEqual(method, "POST")
        self.assertEqual(url, f"{self.BASE_URL}/im/v1/messages?receive_id_type=open_id")
        self.assertEqual(body["receive_id"], self.OPEN_ID)
        self.assertEqual(body["msg_type"], "text")
        self.assertIn("已生成", json.loads(body["content"])["text"])
        self.assertEqual(token, "t-fake-tenant-access-token")

    def test_uncertain_terminal_notice_sends_through_the_real_adapter_without_error(self) -> None:
        transport = _RecordingUserMessageTransport(
            [_real_shape_tenant_token_response(), _real_shape_send_message_response()]
        )
        alerts: list[tuple[str, str]] = []
        consumer = DocumentDeliveryConsumer(
            store=_NoopNotifyStore(),
            docx=_SpyDocx(),
            notifier=self._real_notifier(transport),
            on_alert=lambda kind, task_id: alerts.append((kind, task_id)),
        )
        claim = _notify_test_claim(requester_open_id=self.OPEN_ID)

        consumer._uncertain(claim, last_error="LookupError")

        # 唯一一条告警必须是 `_uncertain` 自身预期产生的那条（见上方类文档字符
        # 串重现说明），不能再叠加 notice_failed——如果通知发送本身也失败，会
        # 多出第二条 (`document_delivery_notice_failed`, ...)。
        self.assertEqual(alerts, [("document_delivery_uncertain", claim.task_id)])
        self.assertEqual(len(transport.calls), 2)
        method, url, body, token = transport.calls[1]
        self.assertEqual(url, f"{self.BASE_URL}/im/v1/messages?receive_id_type=open_id")
        self.assertEqual(body["receive_id"], self.OPEN_ID)
        self.assertIn("暂无法确认", json.loads(body["content"])["text"])

    def test_failed_terminal_notice_sends_through_the_real_adapter_without_error(self) -> None:
        transport = _RecordingUserMessageTransport(
            [_real_shape_tenant_token_response(), _real_shape_send_message_response()]
        )
        alerts: list[tuple[str, str]] = []
        consumer = DocumentDeliveryConsumer(
            store=_NoopNotifyStore(),
            docx=_SpyDocx(),
            notifier=self._real_notifier(transport),
            on_alert=lambda kind, task_id: alerts.append((kind, task_id)),
        )
        claim = _notify_test_claim(requester_open_id=self.OPEN_ID)

        consumer._fail(claim, last_error="feishu_code_99991400")

        self.assertEqual(alerts, [("document_delivery_failed", claim.task_id)])
        self.assertEqual(len(transport.calls), 2)
        method, url, body, token = transport.calls[1]
        self.assertEqual(url, f"{self.BASE_URL}/im/v1/messages?receive_id_type=open_id")
        self.assertEqual(body["receive_id"], self.OPEN_ID)
        self.assertIn("生成失败", json.loads(body["content"])["text"])


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


class DocxMarkdownConvertGatewayWiringTest(unittest.TestCase):
    """Issue #408 正式方案接线：证明 gateway 配置 → ``LarkDocxDelivery`` 构造 →
    ``DocumentDeliveryConsumer._process_docx_claim`` 的分支决策 → 真实 HTTP 调用
    形状是逐段接起来的一整条链路，不只是"各自的单元测试都通过"。转换开关自身
    的分支语义（成功/超限/业务错误码分别产生什么调用序列）已由
    ``tests/test_feishu_docx_delivery.py::WriteBodySwitchTest`` 在适配器层验证
    过，本类不重复；只钉住 gateway 这一层特有的决策：**``claim.markdown`` 是否
    非 ``None`` 才决定要不要调用 ``write_body``**，与转换开关状态无关。不接触
    数据库或网络（传输层全部注入）。

    覆盖四条分支（任务卡登记的测试矩阵）：
    - 开关关 + markdown 非空 → 段落路径零变化；
    - 开关开 + markdown 非空 → 转换成功；
    - 开关开 + markdown 非空 + 飞书明确拒绝转换 → 失败关闭，沿用既有 definite
      分类，不静默退回段落路径；
    - markdown 为 ``None``（不论开关状态）→ 无条件回退段落路径。

    变异锚点：把 ``_process_docx_claim`` 里 ``if claim.markdown is not None``
    的判断删掉、改成恒调用 ``write_body``——最后一条用例（NULL markdown）会从
    "只发生 4 次调用、没有 convert"变红成"发生 convert 调用后因空 markdown
    被 ``convert_markdown_to_blocks`` 判定为空正文而失败"；把判断写反（改成
    ``is None`` 才调用 ``write_body``）——前两条用例会互换预期，"开关关零变化"
    那条会变成发生 convert 调用。
    """

    BASE_URL = "https://feishu.invalid/open-apis"
    TENANT_DOMAIN = "gv3qfk4q2rp.feishu.cn"
    OPEN_ID = "ou_wiring_target_user"
    DOCUMENT_ID = "doc-wire-1"

    def _docx(self, transport: Any, *, markdown_convert_enabled: bool) -> LarkDocxDelivery:
        return LarkDocxDelivery(
            base_url=self.BASE_URL,
            tenant_access_token=lambda: "t-fake-tenant-access-token",
            tenant_domain=self.TENANT_DOMAIN,
            transport=transport,
            markdown_convert_enabled=markdown_convert_enabled,
        )

    def _claim(self, *, markdown: str | None) -> DocumentDeliveryClaim:
        return DocumentDeliveryClaim(
            id="tdd-wire-1",
            task_id="tsk-wire-1",
            requester_open_id=self.OPEN_ID,
            title="标题",
            paragraphs=("正文段落",),
            document_id=None,
            attempts=1,
            markdown=markdown,
        )

    def _create_document_response(self) -> dict[str, Any]:
        return {
            "code": 0,
            "data": {"document": {"document_id": self.DOCUMENT_ID, "revision_id": 1, "title": "标题"}},
        }

    @staticmethod
    def _children_write_response() -> dict[str, Any]:
        return {"code": 0, "data": {}}

    @staticmethod
    def _convert_response() -> dict[str, Any]:
        # Issue #442：真实响应形状——每个块携带只读 block_id，真实顺序由
        # first_level_block_ids 给出（本夹具只有一个块，顺序无歧义，但形状要
        # 与探针实测对齐，否则会被 convert_markdown_to_blocks 的防御性拒绝
        # 判定为 first_level_block_ids 缺失）。
        return {
            "code": 0,
            "data": {
                "blocks": [
                    {
                        "block_id": "blk-converted-1",
                        "block_type": 2,
                        "text": {"elements": [{"text_run": {"content": "转换后正文"}}]},
                    }
                ],
                "first_level_block_ids": ["blk-converted-1"],
            },
        }

    @staticmethod
    def _grant_response() -> dict[str, Any]:
        return {"code": 0, "data": {}}

    def _read_members_response(self) -> dict[str, Any]:
        return {
            "code": 0,
            "data": {"items": [{"member_type": "openid", "member_id": self.OPEN_ID, "perm": "full_access"}]},
        }

    def test_switch_off_with_markdown_present_takes_the_paragraph_path_unchanged(self) -> None:
        """开关关：即使这一行带着 markdown 原文，也必须逐字沿用段落路径——只
        应该看到一次 children 插入调用，绝不会发生 blocks/convert 调用。"""

        transport = _RecordingUserMessageTransport(
            [
                self._create_document_response(),
                self._children_write_response(),
                self._grant_response(),
                self._read_members_response(),
            ]
        )
        store = _RecordingDeliveryStore()
        consumer = DocumentDeliveryConsumer(
            store=store, docx=self._docx(transport, markdown_convert_enabled=False), notifier=_SpyNotifier()
        )
        claim = self._claim(markdown="# 标题\n\n正文段落")

        consumer._process_docx_claim(claim)

        self.assertEqual(len(transport.calls), 4)
        _, write_url, write_body, _ = transport.calls[1]
        self.assertEqual(
            write_url, f"{self.BASE_URL}/docx/v1/documents/{self.DOCUMENT_ID}/blocks/{self.DOCUMENT_ID}/children"
        )
        self.assertEqual(
            write_body,
            {
                "children": [
                    {"block_type": 2, "text": {"elements": [{"text_run": {"content": "正文段落"}}]}}
                ],
                "index": 0,
            },
        )
        self.assertEqual(store.succeeded, ["tdd-wire-1"])
        self.assertEqual(store.failed, [])

    def test_switch_on_with_markdown_present_converts_then_succeeds(self) -> None:
        """开成功：转换开关打开、这一行带着 markdown 原文——依次发生 convert
        调用与用转换结果写入的 children 插入调用，最终判定成功。"""

        transport = _RecordingUserMessageTransport(
            [
                self._create_document_response(),
                self._convert_response(),
                self._children_write_response(),
                self._grant_response(),
                self._read_members_response(),
            ]
        )
        store = _RecordingDeliveryStore()
        consumer = DocumentDeliveryConsumer(
            store=store, docx=self._docx(transport, markdown_convert_enabled=True), notifier=_SpyNotifier()
        )
        claim = self._claim(markdown="# 标题\n\n正文段落")

        consumer._process_docx_claim(claim)

        self.assertEqual(len(transport.calls), 5)
        _, convert_url, convert_body, _ = transport.calls[1]
        self.assertEqual(convert_url, f"{self.BASE_URL}/docx/v1/documents/blocks/convert")
        self.assertEqual(convert_body, {"content_type": "markdown", "content": "# 标题\n\n正文段落"})
        _, write_url, write_body, _ = transport.calls[2]
        self.assertEqual(
            write_url, f"{self.BASE_URL}/docx/v1/documents/{self.DOCUMENT_ID}/blocks/{self.DOCUMENT_ID}/children"
        )
        # 写入端点收到的必须是剔除只读 block_id 后的块，不是转换响应原样。
        self.assertEqual(
            write_body["children"],
            [{"block_type": 2, "text": {"elements": [{"text_run": {"content": "转换后正文"}}]}}],
        )
        self.assertEqual(store.succeeded, ["tdd-wire-1"])
        self.assertEqual(store.failed, [])

    def test_switch_on_convert_failure_fails_closed_via_the_existing_definite_classification(
        self,
    ) -> None:
        """开失败关闭：转换调用收到飞书明确的业务错误码——沿用状态机既有的
        definite 分类判 ``failed``，绝不静默退回段落路径（因此不会再发生任何
        grant/read 调用）。"""

        transport = _RecordingUserMessageTransport(
            [self._create_document_response(), {"code": 99991400, "msg": "rate limited"}]
        )
        store = _RecordingDeliveryStore()
        consumer = DocumentDeliveryConsumer(
            store=store, docx=self._docx(transport, markdown_convert_enabled=True), notifier=_SpyNotifier()
        )
        claim = self._claim(markdown="# 标题\n\n正文段落")

        consumer._process_docx_claim(claim)

        self.assertEqual(len(transport.calls), 2, "convert 失败后不该再发生 grant/read 调用")
        self.assertEqual(store.succeeded, [])
        self.assertEqual(store.failed, [("tdd-wire-1", "feishu_code_99991400")])
        self.assertEqual(store.document_created, [("tdd-wire-1", self.DOCUMENT_ID, None)])

    def test_null_markdown_falls_back_to_paragraphs_regardless_of_the_switch_state(self) -> None:
        """NULL markdown 回退段落：即使转换开关打开，这一行的 ``markdown`` 列是
        ``NULL``（历史行、或登记侧未能落上原文）也必须无条件回退段落路径——
        判据是 ``claim.markdown is None``，与开关状态无关。"""

        transport = _RecordingUserMessageTransport(
            [
                self._create_document_response(),
                self._children_write_response(),
                self._grant_response(),
                self._read_members_response(),
            ]
        )
        store = _RecordingDeliveryStore()
        consumer = DocumentDeliveryConsumer(
            store=store, docx=self._docx(transport, markdown_convert_enabled=True), notifier=_SpyNotifier()
        )
        claim = self._claim(markdown=None)

        consumer._process_docx_claim(claim)

        self.assertEqual(len(transport.calls), 4, "不该发生任何 blocks/convert 调用")
        for _, url, _, _ in transport.calls:
            self.assertNotIn("blocks/convert", url)
        self.assertEqual(store.succeeded, ["tdd-wire-1"])
        self.assertEqual(store.failed, [])


class GatewayConfigMarkdownConvertFlagTest(unittest.TestCase):
    """``LINGXI_DOCX_MARKDOWN_CONVERT`` 的 ``apps/gateway/config.py`` 解析——
    Issue #467／rc22 S-4 翻转默认值后的三态语义：未配置＝开启（代码默认，
    docx 转换已通过 rc21 stage 探针验证）、精确值 ``"0"``＝显式关闭、历史值
    ``"1"``（翻转前唯一的开启值）仍然＝开启，其余值一律错配失败关闭（同
    ``apps/worker/config.py`` 既有 ``LINGXI_WORKER_DOCUMENT_DELIVERY_ENABLED``
    开关同一姿态），以及 ``assemble_document_delivery_consumer`` 把解析结果
    原样传进 ``LarkDocxDelivery`` 构造函数（不接触数据库或网络：
    ``tenant_domain`` 配置了合法值即可装配出一个真实消费者，两个适配器的构造
    函数都只存参数）。"""

    def _base_env(self, **overrides: str) -> dict[str, str]:
        env = {
            "LINGXI_GATEWAY_APP_ID": "cli_x",
            "LINGXI_GATEWAY_APP_SECRET": "secret_x",
            "LINGXI_GATEWAY_POSTGRES_DSN": "postgresql://localhost/lingxi_test",
        }
        env.update(overrides)
        return env

    def test_unset_defaults_to_enabled(self) -> None:
        """Issue #467：未设置＝开启，这是本次翻转的核心行为变化。"""

        from lingxi.apps.gateway.config import load_config

        config = load_config(self._base_env())

        self.assertTrue(config.markdown_convert_enabled)

    def test_exact_value_zero_disables_it(self) -> None:
        """新增的显式关闭途径：精确值 ``"0"``。"""

        from lingxi.apps.gateway.config import load_config

        config = load_config(self._base_env(LINGXI_DOCX_MARKDOWN_CONVERT="0"))

        self.assertFalse(config.markdown_convert_enabled)

    def test_legacy_value_one_still_enables_it(self) -> None:
        """兼容性回归：翻转前唯一的开启值 ``"1"`` 写进过既有 stage 配置，翻转
        默认值后仍必须解析成开启，不能因为默认值反转就产生语义漂移。"""

        from lingxi.apps.gateway.config import load_config

        config = load_config(self._base_env(LINGXI_DOCX_MARKDOWN_CONVERT="1"))

        self.assertTrue(config.markdown_convert_enabled)

    def test_any_other_value_fails_closed_at_startup(self) -> None:
        """错配不是未配：与 ``apps/worker/config.py::_document_delivery_enabled``
        同一纪律，一个拼错的值不该被静默当成任一状态长期放行。"""

        from lingxi.apps.gateway.config import GatewayConfigError, load_config

        for bad_value in ("true", "yes", "10", "2"):
            with self.subTest(value=bad_value):
                with self.assertRaises(GatewayConfigError):
                    load_config(self._base_env(LINGXI_DOCX_MARKDOWN_CONVERT=bad_value))

    def test_assembled_consumer_wires_the_flag_into_the_docx_adapter(self) -> None:
        """装配层把已经读好的布尔值传进 ``LarkDocxDelivery`` 构造函数（Issue #408
        正式方案接线）。只读私有属性断言，不驱动真实调用——``assemble_document_
        delivery_consumer`` 构造的令牌供给包着一个真实 ``FeishuTenantTokenClient``
        （走独立的 urllib 传输，不是这里注入的假传输层），真正调用 ``write_body``
        会触发它去发一次真实网络请求，与本类"不接触数据库或网络"的边界冲突；
        读私有属性是这里唯一不产生副作用的验证方式，且这个属性正是「布尔值有没有
        被传进构造函数」这件事本身的真值来源（``LarkDocxDelivery.__init__`` 里
        ``self._markdown_convert_enabled = bool(markdown_convert_enabled)``）。"""

        enabled_config = GatewayConfig(
            app_id="cli_x",
            app_secret=_Secret("secret_x"),
            postgres_dsn=_Secret("postgresql://localhost/lingxi_test"),
            tenant_domain="example.feishu.cn",
            markdown_convert_enabled=True,
        )
        disabled_config = GatewayConfig(
            app_id="cli_x",
            app_secret=_Secret("secret_x"),
            postgres_dsn=_Secret("postgresql://localhost/lingxi_test"),
            tenant_domain="example.feishu.cn",
            markdown_convert_enabled=False,
        )

        enabled_consumer = assemble_document_delivery_consumer(enabled_config)
        disabled_consumer = assemble_document_delivery_consumer(disabled_config)

        self.assertIsInstance(enabled_consumer, DocumentDeliveryConsumer)
        self.assertIsInstance(disabled_consumer, DocumentDeliveryConsumer)
        self.assertTrue(enabled_consumer._docx._markdown_convert_enabled)
        self.assertFalse(disabled_consumer._docx._markdown_convert_enabled)


class HasConfirmedFullAccessNegativeTests(unittest.TestCase):
    """P2-9（opus 审查）：``_has_confirmed_full_access`` 是纯逻辑判定，此前只在
    真库层（``LINGXI_POSTGRES_DSN`` 门控）间接覆盖，未设置真库时这条红线——
    ``perm`` 不是 ``full_access``（例如飞书只给了 ``view``/``edit`` 只读/可编辑
    档位）绝不能被判定为"已确认"——在这台机器上零覆盖。这里补一个不依赖真库的
    直接单测。

    变异锚点：把 ``_has_confirmed_full_access`` 里 ``member.get("perm") ==
    FULL_ACCESS_PERM`` 这个条件删掉（或改成只看 ``member_type``/``member_id``
    是否匹配），本用例会从 ``False`` 变红成 ``True``。
    """

    TARGET_OPEN_ID = "ou_target_user"

    def test_view_permission_is_not_confirmed_as_full_access(self) -> None:
        members = [{"member_type": "openid", "member_id": self.TARGET_OPEN_ID, "perm": "view"}]

        self.assertFalse(_has_confirmed_full_access(members, self.TARGET_OPEN_ID))
        self.assertFalse(
            _has_confirmed_full_access(members, self.TARGET_OPEN_ID, delivery_type="sheet")
        )

    def test_edit_permission_is_not_confirmed_as_full_access(self) -> None:
        members = [{"member_type": "openid", "member_id": self.TARGET_OPEN_ID, "perm": "edit"}]

        self.assertFalse(_has_confirmed_full_access(members, self.TARGET_OPEN_ID))
        self.assertFalse(
            _has_confirmed_full_access(members, self.TARGET_OPEN_ID, delivery_type="sheet")
        )

    def test_full_access_permission_is_confirmed_as_a_positive_control(self) -> None:
        """反向哨兵：不是这个函数对任何输入都返回 False。"""

        members = [
            {"member_type": "openid", "member_id": self.TARGET_OPEN_ID, "perm": "full_access"}
        ]

        self.assertTrue(_has_confirmed_full_access(members, self.TARGET_OPEN_ID))


if __name__ == "__main__":
    unittest.main()
