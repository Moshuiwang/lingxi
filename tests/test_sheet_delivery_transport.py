"""表格投递链路——gateway 独立消费循环表格分支 + worker 侧插入（Issue #354 S-H3-2）。

与 ``tests/test_document_delivery_transport.py`` 同一条真库底座（``ensure_
production_schema``/``reset_production_rows``），只新增表格分支特有的断言，不
重复文档分支已经覆盖过的公共机制（唯一约束、SAVEPOINT 降级、P1-2 持有权丢失、
死信/到期擦除、通知补发退避窗口等——那些是检查点表/状态机层面的公共行为，表格
分支复用同一张表、同一套 ``mark_*`` 方法，已经被文档分支的用例间接覆盖过）。

覆盖派工卡「必须有测试」的否定用例：

① 写值失败（``FeishuSheetsDeliveryError(definite=False)``）→ 不发送、状态转
   ``uncertain`` 可重试，且重试不重复建表（``create_spreadsheet`` 全程恰一次）；
② 授权降级（``read_members`` 返回的档位不是 ``full_access``）→ 判 ``uncertain``
   不发送、不 ``succeeded``；
③ 发送失败（通知适配器抛异常）→ 已经落库的 ``succeeded`` 终态不回滚、检查点
   （``document_id``/``resource_url``）保持不变、下一轮 ``run_once`` 通过
   ``claim_unnotified_succeeded`` 补发，不重复建表；
④ definite 错误（飞书明确拒绝）→ ``failed`` + 对称的表格失败文案；
⑤ 入参校验 ``ValueError``（发出请求之前失败）→ ``failed`` 不是 ``uncertain``；
⑥ worker 侧：终态成功且报告契约 ``sheet_request`` 非空 → 恰一行 ``pending``、
   ``delivery_type='sheet'``；终态失败或字段为空 → 零行；
⑦ ``write_terminal_event`` 的 ``document_request``/``sheet_request`` 互斥
   校验（结构性纵深防线，不需要真库——检查发生在建立数据库连接之前）。

变异锚点（任务卡登记）：
- 删掉 ``_process_sheet_claim`` 里 ``if spreadsheet_token is None`` 判断（每次
  都调用 ``create_spreadsheet``）→ ①红（``create_calls`` 变成 2）；
- 把 ``_finalize_claim`` 的成功判据从"``_has_confirmed_full_access`` 确认"改成
  "没抛异常就成功"→ ②红；
- 把 ``_send_ready_notice`` 里 ``self._store.mark_notified`` 提到通知发送**之前**
  → ③红（通知失败时 ``notified_at`` 仍会被错误置位，下一轮不再补发）；
- 把 ``_process_sheet_claim`` 的 ``FeishuSheetsDeliveryError`` 分支判定
  （``error.definite``）写反 → ④红（definite 错误落进 uncertain）；
- 把 ``_process_sheet_claim`` 的 ``except ValueError`` 分支删掉（并入通用
  ``except Exception``）→ ⑤红（ValueError 落进 uncertain 而不是 failed）；
- 把 ``adapters/postgres_conversation/_queue_outbox.py`` 里
  ``document_request is not None and sheet_request is not None`` 的互斥校验删掉
  → ⑦红（两者同传不再报错）。
"""

from __future__ import annotations

import json
import os
import unittest
from typing import Any

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.adapters.feishu_sheets_delivery import FeishuSheetsDeliveryError
from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_conversation import PostgresTaskQueue
from lingxi.adapters.postgres_document_delivery import (
    DELIVERY_TYPE_SHEET,
    PostgresDocumentDeliveryStore,
)
from lingxi.apps.gateway.document_delivery import DocumentDeliveryConsumer

DSN = os.environ.get("LINGXI_POSTGRES_DSN")
SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，表格投递链路的真库断言未验证"
    if not DSN
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，表格投递链路的真库断言未验证"
)
POSTGRES_READY = bool(DSN) and psycopg_available()


class _SpySheets:
    """表格交付流程的假实现：可编排每一步的行为，记录调用次数供断言——同
    ``test_document_delivery_transport.py::_SpyDocx`` 的形状。
    """

    def __init__(self, *, create_result: Any = ("sheet-token-1", "https://example.feishu.cn/sheets/sheet-token-1"), members: Any = ()) -> None:
        self.create_calls: list[str] = []
        self.get_sheet_id_calls: list[str] = []
        self.write_calls: list[tuple[str, str, list[list[str]]]] = []
        self.grant_calls: list[tuple[str, str]] = []
        self.read_calls: list[str] = []
        self._create_result = create_result
        self._members = members

    def create_spreadsheet(self, title: str) -> tuple[str, str]:
        self.create_calls.append(title)
        if callable(self._create_result):
            return self._create_result()
        return self._create_result

    def get_default_sheet_id(self, spreadsheet_token: str) -> str:
        self.get_sheet_id_calls.append(spreadsheet_token)
        return "sheet-id-1"

    def write_values(self, spreadsheet_token: str, sheet_id: str, rows: list[list[str]]) -> None:
        self.write_calls.append((spreadsheet_token, sheet_id, [list(row) for row in rows]))

    def grant_full_access(self, spreadsheet_token: str, open_id: str) -> None:
        self.grant_calls.append((spreadsheet_token, open_id))

    def read_members(self, spreadsheet_token: str) -> list[dict[str, Any]]:
        self.read_calls.append(spreadsheet_token)
        if callable(self._members):
            return self._members()
        return list(self._members)


class _SpyNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send_text(self, *, open_id: str, text: str, dedupe_key: str) -> None:
        self.sent.append((open_id, text, dedupe_key))


class _FailingNotifier:
    def send_text(self, *, open_id: str, text: str, dedupe_key: str) -> None:
        raise RuntimeError("simulated notify failure")


@unittest.skipUnless(POSTGRES_READY, SKIP_REASON)
class SheetDeliveryTransportTestCase(unittest.TestCase):
    TASK_ID = "tsk-sheet-1"
    REQUESTER_OPEN_ID = "ou-sheet-requester"

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
                       VALUES ('usr-sheet','ou-sheet-requester','u-sheet','un-sheet',
                               '王五','数据部','tk-sheet','active')"""
                )
                connection.execute(
                    """INSERT INTO conversation
                       (id,user_id,feishu_chat_id,feishu_thread_id,running_task_id)
                       VALUES ('cnv-sheet','usr-sheet','chat-sheet','topic-sheet',NULL)"""
                )
                connection.execute(
                    """INSERT INTO task
                       (id,conversation_id,user_id,inbound_event_id,prompt,status,
                        target_worker_version,attempts,content_expires_at)
                       VALUES (%s,'cnv-sheet','usr-sheet','event-sheet','问题','succeeded',
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

    def _seed_pending_sheet_request(
        self, *, request_id: str = "tds-1", document_id: str | None = None, resource_url: str | None = None
    ) -> None:
        self.execute(
            """INSERT INTO task_document_delivery_request
               (id, task_id, requester_open_id, title, paragraphs, document_id,
                resource_url, delivery_type)
               VALUES (%s, %s, %s, '标题', %s, %s, %s, 'sheet')""",
            (
                request_id,
                self.TASK_ID,
                self.REQUESTER_OPEN_ID,
                json.dumps([["月份", "销售额"], ["1月", "100"]], ensure_ascii=False),
                document_id,
                resource_url,
            ),
        )

    # -- 装配层：claim 正确带出 delivery_type/resource_url --------------------

    def test_claim_pending_carries_delivery_type_and_resource_url(self) -> None:
        self._seed_pending_sheet_request(request_id="tds-claim", document_id="sheet-existing", resource_url="https://example.feishu.cn/sheets/sheet-existing")

        claims = self.store.claim_pending(limit=1)

        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].delivery_type, DELIVERY_TYPE_SHEET)
        self.assertEqual(claims[0].document_id, "sheet-existing")
        self.assertEqual(claims[0].resource_url, "https://example.feishu.cn/sheets/sheet-existing")
        self.assertEqual(claims[0].paragraphs, (["月份", "销售额"], ["1月", "100"]))

    # -- 成功路径 --------------------------------------------------------------

    def test_a_successful_sheet_flow_creates_writes_grants_and_succeeds(self) -> None:
        self._seed_pending_sheet_request(request_id="tds-success")
        sheets = _SpySheets(
            create_result=("sheet-token-x", "https://example.feishu.cn/sheets/sheet-token-x"),
            members=[{"member_type": "openid", "member_id": self.REQUESTER_OPEN_ID, "perm": "full_access"}],
        )
        notifier = _SpyNotifier()
        consumer = DocumentDeliveryConsumer(store=self.store, docx=object(), sheets=sheets, notifier=notifier)

        processed = consumer.run_once()

        self.assertEqual(processed, 1)
        self.assertEqual(sheets.create_calls, ["标题"])
        self.assertEqual(sheets.get_sheet_id_calls, ["sheet-token-x"])
        self.assertEqual(len(sheets.write_calls), 1)
        self.assertEqual(sheets.write_calls[0][2], [["月份", "销售额"], ["1月", "100"]])
        self.assertEqual(sheets.grant_calls, [("sheet-token-x", self.REQUESTER_OPEN_ID)])
        self.assertEqual(
            self.scalar("SELECT status FROM task_document_delivery_request WHERE id = 'tds-success'"),
            "succeeded",
        )
        self.assertEqual(
            self.scalar("SELECT document_id FROM task_document_delivery_request WHERE id = 'tds-success'"),
            "sheet-token-x",
        )
        self.assertEqual(
            self.scalar("SELECT resource_url FROM task_document_delivery_request WHERE id = 'tds-success'"),
            "https://example.feishu.cn/sheets/sheet-token-x",
        )
        self.assertEqual(
            self.scalar("SELECT delivery_type FROM task_document_delivery_request WHERE id = 'tds-success'"),
            "sheet",
        )
        self.assertEqual(len(notifier.sent), 1)
        open_id, text, dedupe_key = notifier.sent[0]
        self.assertEqual(open_id, self.REQUESTER_OPEN_ID)
        self.assertEqual(text, "你要的表格已生成：https://example.feishu.cn/sheets/sheet-token-x（你已获得可管理权限）")
        self.assertEqual(dedupe_key, "sheet-ready:tds-success")

    # -- ① 写值失败：不当作成功（不发送产物链接）、落 uncertain -----------------

    def test_write_failure_does_not_send_the_ready_link_and_is_uncertain_not_succeeded(self) -> None:
        """变异锚点①（上半）：把成功判据从"``_has_confirmed_full_access`` 确认"
        改成"没抛异常就成功"，本用例应变红（uncertain 状态会变成 succeeded）。

        V-交付-03：``uncertain`` 不会被 ``claim_pending`` 自动重试（该方法的查询
        谓词只认 ``status='pending'``）——"不发送"指不当作成功交付、不发送产物
        链接，不是完全静默：用户仍会收到一条独立措辞的"结果暂无法确认"通知
        （同 docx 分支既有姿态，见 ``content.toml`` 的 ``delivery.sheet_
        uncertain``）。"重试不重复建表"的真实可达路径是崩溃恢复（stale
        processing 被回收后重新认领），见下面
        ``test_checkpoint_recovery_after_a_crash_between_create_and_write_
        never_recreates_the_spreadsheet``。
        """

        self._seed_pending_sheet_request(request_id="tds-write-fail")

        class FlakyWriteSheets(_SpySheets):
            def write_values(self, spreadsheet_token, sheet_id, rows):
                raise FeishuSheetsDeliveryError("transport_error", definite=False)

        sheets = FlakyWriteSheets()
        notifier = _SpyNotifier()
        consumer = DocumentDeliveryConsumer(store=self.store, docx=object(), sheets=sheets, notifier=notifier)

        consumer.run_once()

        self.assertEqual(
            self.scalar("SELECT status FROM task_document_delivery_request WHERE id = 'tds-write-fail'"),
            "uncertain",
        )
        self.assertEqual(sheets.create_calls, ["标题"], "建表检查点已落盘，不因写值失败而重复建表")
        # uncertain 终态发的是独立措辞的"结果暂无法确认"通知，不是产物就绪链接
        # ——两者用不同的 content.toml 键与去重前缀，不会互相冒充。
        self.assertEqual(len(notifier.sent), 1)
        self.assertEqual(notifier.sent[0][1], "表格生成结果暂无法确认，已转人工核对。追溯号：tsk-sheet-1。")
        self.assertEqual(notifier.sent[0][2], "sheet-uncertain:tds-write-fail")
        # claim_pending 的查询谓词只认 pending：uncertain 行不会被再次认领。
        self.assertEqual(self.store.claim_pending(limit=10), [])

    def test_checkpoint_recovery_after_a_crash_between_create_and_write_never_recreates_the_spreadsheet(
        self,
    ) -> None:
        """崩溃恢复：建表检查点已经单独提交、但进程在写值之前崩溃——回收后的
        全新消费者续做时绝不二次调用 ``create_spreadsheet``（spy 断言恰一次），
        并续做到底、最终成功。与 ``test_document_delivery_transport.py::
        test_checkpoint_recovery_never_creates_the_document_twice`` 同一手法。

        变异锚点①（下半）：把 ``_process_sheet_claim`` 的
        ``if spreadsheet_token is None`` 判断去掉，本用例应变红
        （``create_calls`` 变成 2）。
        """

        self._seed_pending_sheet_request(request_id="tds-crash-recover")
        sheets = _SpySheets(
            create_result=("sheet-crash-1", "https://example.feishu.cn/sheets/sheet-crash-1"),
            members=[{"member_type": "openid", "member_id": self.REQUESTER_OPEN_ID, "perm": "full_access"}],
        )
        notifier = _SpyNotifier()

        # 阶段一：手工认领 + 建表 + 检查点提交，模拟"崩溃"——不再往下走。
        claims = self.store.claim_pending(limit=1)
        self.assertEqual(len(claims), 1)
        claim = claims[0]
        spreadsheet_token, url = sheets.create_spreadsheet(claim.title)
        self.store.mark_document_created(request_id=claim.id, document_id=spreadsheet_token, resource_url=url)
        self.assertEqual(sheets.create_calls, ["标题"])

        # 崩溃后这一行停在 processing、document_id/resource_url 已经非空。回拨
        # updated_at 到回收窗口之外，模拟"卡住了一段时间"。
        self.execute(
            "UPDATE task_document_delivery_request SET updated_at = now() - interval '10 minutes' WHERE id = %s",
            (claim.id,),
        )

        # 阶段二：全新消费者续做——spreadsheet_token/resource_url 已经检查点
        # 持久化，流程从 get_default_sheet_id/write_values 起步。
        consumer = DocumentDeliveryConsumer(store=self.store, docx=object(), sheets=sheets, notifier=notifier)
        processed = consumer.run_once()

        self.assertEqual(processed, 1)
        self.assertEqual(sheets.create_calls, ["标题"], "create_spreadsheet 全程只应被调用一次")
        self.assertEqual(len(sheets.write_calls), 1)
        self.assertEqual(len(sheets.grant_calls), 1)
        self.assertEqual(
            self.scalar("SELECT status FROM task_document_delivery_request WHERE id = %s", (claim.id,)),
            "succeeded",
        )
        self.assertEqual(
            self.scalar("SELECT document_id FROM task_document_delivery_request WHERE id = %s", (claim.id,)),
            "sheet-crash-1",
        )
        self.assertEqual(
            self.scalar("SELECT resource_url FROM task_document_delivery_request WHERE id = %s", (claim.id,)),
            "https://example.feishu.cn/sheets/sheet-crash-1",
        )
        self.assertEqual(len(notifier.sent), 1)
        self.assertEqual(notifier.sent[0][0], self.REQUESTER_OPEN_ID)

    # -- ② 授权降级 --------------------------------------------------------------

    def test_permission_downgrade_is_uncertain_not_succeeded(self) -> None:
        """变异锚点②：把成功判据从"确认 full_access"改成"没抛异常就成功"，本
        用例应变红。
        """

        self._seed_pending_sheet_request(request_id="tds-downgrade")
        sheets = _SpySheets(
            members=[{"member_type": "openid", "member_id": self.REQUESTER_OPEN_ID, "perm": "view"}]
        )
        notifier = _SpyNotifier()
        consumer = DocumentDeliveryConsumer(store=self.store, docx=object(), sheets=sheets, notifier=notifier)

        consumer.run_once()

        self.assertEqual(
            self.scalar("SELECT status FROM task_document_delivery_request WHERE id = 'tds-downgrade'"),
            "uncertain",
        )
        self.assertEqual(
            self.scalar("SELECT last_error FROM task_document_delivery_request WHERE id = 'tds-downgrade'"),
            "permission_not_confirmed",
        )
        # uncertain 终态仍然会给用户发一条对应措辞的追加消息（R-1 第 3 条，同
        # docx 分支既有姿态）——"不判 succeeded"不等于"不通知用户"。
        self.assertEqual(len(notifier.sent), 1)
        self.assertEqual(notifier.sent[0][1], "表格生成结果暂无法确认，已转人工核对。追溯号：tsk-sheet-1。")

    def test_missing_target_in_read_members_is_uncertain_not_succeeded(self) -> None:
        self._seed_pending_sheet_request(request_id="tds-no-member")
        sheets = _SpySheets(members=[])
        consumer = DocumentDeliveryConsumer(store=self.store, docx=object(), sheets=sheets, notifier=_SpyNotifier())

        consumer.run_once()

        self.assertEqual(
            self.scalar("SELECT status FROM task_document_delivery_request WHERE id = 'tds-no-member'"),
            "uncertain",
        )

    # -- ④ definite 失败 ----------------------------------------------------

    def test_definite_feishu_rejection_is_failed_with_sheet_specific_notice(self) -> None:
        """变异锚点④：把 ``error.definite`` 判定写反，本用例应变红（definite
        错误落进 uncertain）。
        """

        self._seed_pending_sheet_request(request_id="tds-definite")

        class RejectingSheets(_SpySheets):
            def create_spreadsheet(self, title: str) -> tuple[str, str]:
                self.create_calls.append(title)
                raise FeishuSheetsDeliveryError("feishu_code_99999", definite=True)

        sheets = RejectingSheets()
        notifier = _SpyNotifier()
        alerts: list[tuple[str, str]] = []
        consumer = DocumentDeliveryConsumer(
            store=self.store, docx=object(), sheets=sheets, notifier=notifier,
            on_alert=lambda kind, task_id: alerts.append((kind, task_id)),
        )

        consumer.run_once()

        self.assertEqual(
            self.scalar("SELECT status FROM task_document_delivery_request WHERE id = 'tds-definite'"),
            "failed",
        )
        self.assertEqual(
            self.scalar("SELECT last_error FROM task_document_delivery_request WHERE id = 'tds-definite'"),
            "feishu_code_99999",
        )
        self.assertIn(("document_delivery_failed", self.TASK_ID), alerts)
        self.assertEqual(len(notifier.sent), 1)
        open_id, text, dedupe_key = notifier.sent[0]
        self.assertEqual(open_id, self.REQUESTER_OPEN_ID)
        self.assertEqual(text, "抱歉，你要的表格生成失败了。你可以重新发起问数再试一次；问题已记录。追溯号：tsk-sheet-1。")
        self.assertEqual(dedupe_key, "sheet-failed:tds-definite")
        # 明确失败不重试。
        self.assertEqual(self.store.claim_pending(limit=10), [])

    def test_indefinite_error_is_uncertain_with_sheet_specific_notice(self) -> None:
        self._seed_pending_sheet_request(request_id="tds-indefinite")

        class FlakySheets(_SpySheets):
            def create_spreadsheet(self, title: str) -> tuple[str, str]:
                self.create_calls.append(title)
                raise FeishuSheetsDeliveryError("transport_error", definite=False)

        sheets = FlakySheets()
        notifier = _SpyNotifier()
        consumer = DocumentDeliveryConsumer(store=self.store, docx=object(), sheets=sheets, notifier=notifier)

        consumer.run_once()

        self.assertEqual(
            self.scalar("SELECT status FROM task_document_delivery_request WHERE id = 'tds-indefinite'"),
            "uncertain",
        )
        self.assertEqual(len(notifier.sent), 1)
        self.assertEqual(notifier.sent[0][1], "表格生成结果暂无法确认，已转人工核对。追溯号：tsk-sheet-1。")
        self.assertEqual(notifier.sent[0][2], "sheet-uncertain:tds-indefinite")

    # -- ⑤ 确定性入参校验错误：failed 不是 uncertain -----------------------------

    def test_a_deterministic_precondition_valueerror_is_failed_not_uncertain(self) -> None:
        """变异锚点⑤：把 ``except ValueError`` 分支删掉（并入通用
        ``except Exception``），本用例应变红（ValueError 落进 uncertain）。
        """

        self._seed_pending_sheet_request(request_id="tds-bad-input")

        class BadInputSheets(_SpySheets):
            def grant_full_access(self, spreadsheet_token: str, open_id: str) -> None:
                self.grant_calls.append((spreadsheet_token, open_id))
                raise ValueError("open_id 必须是飞书用户 open_id，不回显收到的值")

        sheets = BadInputSheets()
        notifier = _SpyNotifier()
        consumer = DocumentDeliveryConsumer(store=self.store, docx=object(), sheets=sheets, notifier=notifier)

        consumer.run_once()

        self.assertEqual(
            self.scalar("SELECT status FROM task_document_delivery_request WHERE id = 'tds-bad-input'"),
            "failed",
        )
        self.assertEqual(
            self.scalar("SELECT last_error FROM task_document_delivery_request WHERE id = 'tds-bad-input'"),
            "ValueError",
        )
        self.assertEqual(len(notifier.sent), 1)

    # -- ③ 发送失败：检查点保留、succeeded 不回滚、可补发 -------------------------

    def test_notify_failure_does_not_roll_back_succeeded_state_and_is_resent_next_round(self) -> None:
        """变异锚点③：把 ``mark_notified`` 提到通知发送之前，本用例应变红
        （通知失败但 ``notified_at`` 被错误置位，下一轮不再补发）。
        """

        self._seed_pending_sheet_request(request_id="tds-notify-fail")
        sheets = _SpySheets(
            create_result=("sheet-notify-1", "https://example.feishu.cn/sheets/sheet-notify-1"),
            members=[{"member_type": "openid", "member_id": self.REQUESTER_OPEN_ID, "perm": "full_access"}],
        )
        failing_notifier = _FailingNotifier()
        alerts: list[tuple[str, str]] = []
        consumer = DocumentDeliveryConsumer(
            store=self.store, docx=object(), sheets=sheets, notifier=failing_notifier,
            on_alert=lambda kind, task_id: alerts.append((kind, task_id)),
        )

        consumer.run_once()

        # 四步已经全部跑完并落终态 succeeded，只是通知没能真正送达。
        self.assertEqual(
            self.scalar("SELECT status FROM task_document_delivery_request WHERE id = 'tds-notify-fail'"),
            "succeeded",
        )
        self.assertIsNone(
            self.scalar("SELECT notified_at FROM task_document_delivery_request WHERE id = 'tds-notify-fail'")
        )
        self.assertIn(("document_delivery_notice_failed", self.TASK_ID), alerts)
        self.assertEqual(sheets.create_calls, ["标题"])

        # 补发窗口（NOTIFY_RETRY_AFTER）默认 10 分钟——回拨 updated_at 让下一轮
        # 能捞到它，模拟"过了退避窗口"。
        self.execute(
            "UPDATE task_document_delivery_request SET updated_at = now() - interval '20 minutes' WHERE id = 'tds-notify-fail'"
        )
        good_notifier = _SpyNotifier()
        resend_consumer = DocumentDeliveryConsumer(store=self.store, docx=object(), sheets=sheets, notifier=good_notifier)
        resend_consumer.run_once()

        self.assertEqual(sheets.create_calls, ["标题"], "补发通知不得重新建表")
        self.assertEqual(len(good_notifier.sent), 1)
        self.assertIsNotNone(
            self.scalar("SELECT notified_at FROM task_document_delivery_request WHERE id = 'tds-notify-fail'")
        )

    def test_missing_resource_url_at_notify_time_fails_the_notice_not_the_terminal_state(self) -> None:
        """防御性用例：``_send_ready_notice`` 收到 sheet 类型但 ``resource_url``
        为空（结构性不应发生，但不假设它一定不发生）时必须响亮记通知失败，不
        猜测/拼一个链接，也不影响已经落库的 succeeded 终态。
        """

        self._seed_pending_sheet_request(request_id="tds-no-url", document_id="sheet-existing", resource_url=None)
        # 直接调用底层方法验证防御分支，不依赖能产出这个反常状态的完整流程。
        from lingxi.apps.gateway.document_delivery import DocumentDeliveryConsumer as _Consumer

        notifier = _SpyNotifier()
        alerts: list[tuple[str, str]] = []
        consumer = _Consumer(
            store=self.store, docx=object(), sheets=_SpySheets(), notifier=notifier,
            on_alert=lambda kind, task_id: alerts.append((kind, task_id)),
        )

        consumer._send_ready_notice(
            request_id="tds-no-url",
            task_id=self.TASK_ID,
            requester_open_id=self.REQUESTER_OPEN_ID,
            document_id="sheet-existing",
            delivery_type=DELIVERY_TYPE_SHEET,
            resource_url=None,
        )

        self.assertEqual(notifier.sent, [])
        self.assertIn(("document_delivery_notice_failed", self.TASK_ID), alerts)


@unittest.skipUnless(POSTGRES_READY, SKIP_REASON)
class WorkerSheetRequestInsertionTestCase(unittest.TestCase):
    """⑥ worker 侧：终态成功且 sheet_request 非空 → 恰一行 pending、
    delivery_type='sheet'；终态失败或字段为空 → 零行。与
    ``test_document_delivery_transport.py::WorkerDocumentRequestInsertionTestCase``
    同一底座，只测 worker→outbox 这一段（不驱动真实 gateway 消费循环）。
    """

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
                       VALUES ('usr-sheet-worker','ou-sheet-worker','u-sheet-worker',
                               'un-sheet-worker','赵六','数据部','tk-sheet-worker','active')"""
                )

    @classmethod
    def setUpClass(cls) -> None:
        assert DSN is not None
        ensure_production_schema(DSN)

    def _insert_queued_task(self, *, task_id: str, conversation_id: str) -> None:
        with connect(DSN) as connection:
            with connection.transaction():
                connection.execute(
                    """INSERT INTO conversation
                       (id,user_id,feishu_chat_id,feishu_thread_id,running_task_id)
                       VALUES (%s,'usr-sheet-worker',%s,%s,NULL)""",
                    (conversation_id, f"chat-{conversation_id}", f"topic-{conversation_id}"),
                )
                connection.execute(
                    """INSERT INTO task
                       (id,conversation_id,user_id,inbound_event_id,prompt,status,
                        target_worker_version,worker_id,heartbeat_at,attempts,content_expires_at)
                       VALUES (%s,%s,'usr-sheet-worker',%s,'问题','running','stable',
                               'worker-sheet-1',now(),0,now())""",
                    (task_id, conversation_id, f"event-{task_id}"),
                )

    def test_successful_terminal_with_sheet_request_inserts_one_pending_sheet_row(self) -> None:
        task_id = "tsk-sheet-worker-success"
        self._insert_queued_task(task_id=task_id, conversation_id="cnv-sheet-worker-success")

        appended = self.queue.write_terminal_event(
            task_id=task_id,
            worker_id="worker-sheet-1",
            terminal_kind="success",
            error_kind=None,
            content="问答结果",
            sheet_request={"title": "销售汇总", "rows": [["月份", "销售额"], ["1月", "100"]]},
        )

        self.assertFalse(appended.duplicate)
        with connect(DSN) as connection:
            row = connection.execute(
                "SELECT status, title, delivery_type, paragraphs, requester_open_id "
                "FROM task_document_delivery_request WHERE task_id = %s",
                (task_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        status, title, delivery_type, paragraphs, requester_open_id = row
        self.assertEqual(status, "pending")
        self.assertEqual(title, "销售汇总")
        self.assertEqual(delivery_type, "sheet")
        self.assertEqual(paragraphs, [["月份", "销售额"], ["1月", "100"]])
        self.assertEqual(requester_open_id, "ou-sheet-worker")

    def test_failed_terminal_kind_with_sheet_request_is_rejected(self) -> None:
        task_id = "tsk-sheet-worker-failed-kind"
        self._insert_queued_task(task_id=task_id, conversation_id="cnv-sheet-worker-failed-kind")

        with self.assertRaises(ValueError):
            self.queue.write_terminal_event(
                task_id=task_id,
                worker_id="worker-sheet-1",
                terminal_kind="failure",
                error_kind="session_failed",
                content=None,
                sheet_request={"title": "标题", "rows": [["a"]]},
            )

    def test_successful_terminal_without_sheet_request_inserts_zero_rows(self) -> None:
        task_id = "tsk-sheet-worker-empty"
        self._insert_queued_task(task_id=task_id, conversation_id="cnv-sheet-worker-empty")

        self.queue.write_terminal_event(
            task_id=task_id,
            worker_id="worker-sheet-1",
            terminal_kind="success",
            error_kind=None,
            content="问答结果",
        )

        with connect(DSN) as connection:
            count = connection.execute(
                "SELECT count(*) FROM task_document_delivery_request WHERE task_id = %s", (task_id,)
            ).fetchone()[0]
        self.assertEqual(count, 0)


# -- ⑦ document_request/sheet_request 互斥（结构性纵深防线，不需要真库） -------


class TerminalEventMutualExclusionTest(unittest.TestCase):
    """校验发生在建立数据库连接**之前**（纯 Python 参数检查），因此不需要真库
    —— 用一个不可能真正连通的 DSN 构造 queue 也能验证这条防线。

    变异锚点：把 ``adapters/postgres_conversation/_queue_outbox.py`` 里
    ``document_request is not None and sheet_request is not None`` 的互斥校验
    删掉，本用例应变红（不再抛 ValueError，转而尝试真的建立连接并因为 DSN 不可
    达而抛出另一种、语义完全不同的连接错误——判定条件本身也就随之失效）。
    """

    def test_both_non_none_is_rejected_before_any_connection_attempt(self) -> None:
        queue = PostgresTaskQueue("postgresql://unreachable.invalid/db")

        with self.assertRaises(ValueError) as raised:
            queue.write_terminal_event(
                task_id="irrelevant",
                worker_id="irrelevant",
                terminal_kind="success",
                error_kind=None,
                content="x",
                document_request={"title": "t", "paragraphs": ["p"]},
                sheet_request={"title": "t", "rows": [["r"]]},
            )

        self.assertIn("不能同时提供", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
