"""Gateway 文档投递独立消费循环。

认领 ``task_document_delivery_request`` 行（worker 侧终态事务已插入），驱动
"建档 → 写正文 → 授予可管理 → 读回确认"四步，成功后把链接作为追加消息发给
用户。**独立于** ``DeliveryConsumer``：四步至少一步是真实飞书 HTTP 调用，混进
同一条循环会阻塞其他用户的终态送达，因此各自轮询、各自失败隔离。

**失败分类只有两种**：确定性入参校验失败与服务端明确业务拒绝 → ``failed``；
其余一切 → ``uncertain``（白名单反转，不自动重试，转人工核对）——有副作用的
调用异常时不能假设没有生效。检查点独立提交，恢复路径靠读回判断正文是否已
写过，绝不重复建档。**明示降级**必须落库、换用如实文案、留结构化日志三者
缺一不可，但不触发故障告警——降级是交付成功，不是故障。已知边界（无租约
认领、恢复路径可能覆盖用户编辑窗口、``read_members`` 失败但已实际授权）本批
只登记不改动，详见 ``docs/决策记录/`` 对应记录。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from lingxi.adapters.postgres_document_delivery import (
    DELIVERY_TYPE_DOCX,
    DELIVERY_TYPE_SHEET,
    DocumentDeliveryClaim,
    PostgresDocumentDeliveryStore,
)
from lingxi.config.content import ContentCatalog, default_content_catalog

logger = logging.getLogger(__name__)

AlertCallback = Callable[[str, str], None]

#: 循环级异常告警的占位 trace_id（与 ``apps/gateway/delivery.py`` 的
#: ``LOOP_ALERT_TRACE_ID`` 同一手法，各自独立命名，避免管理群通知分不清告警
#: 来自哪一条循环）。
LOOP_ALERT_TRACE_ID = "gateway-document-delivery-loop"

#: 一轮最多认领并处理的行数——与 ``DeliveryConsumer`` 的默认批量同量级，不是
#: 精确调校值：文档交付预期是低频动作，不需要为它单独暴露一个环境变量。
DEFAULT_BATCH_LIMIT = 5


def _default_alert(kind: str, task_id: str) -> None:
    """默认告警出口：结构化日志。真实告警路由由调用方注入。"""
    logger.error("文档投递告警 kind=%s task_id=%s", kind, task_id)


def _has_confirmed_full_access(
    members: list[dict[str, Any]], open_id: str, *, delivery_type: str = DELIVERY_TYPE_DOCX
) -> bool:
    """判定 read_members 读回结果是否确认目标 open_id 具备 full_access。

    只关心"有没有恰好一条命中目标 open_id 且档位是 full_access 的记录"这一个
    布尔结论。``delivery_type``：``FULL_ACCESS_PERM``/``OPENID_MEMBER_TYPE``
    两个常量在 docx 与 sheets 两个并列适配器里各自独立定义，本方法同时服务
    两条 ``_finalize_claim`` 路径，因此按 ``delivery_type`` 选取对应模块的
    常量，不恒从 docx 模块导入。
    """
    if delivery_type == DELIVERY_TYPE_SHEET:
        from lingxi.adapters.feishu_sheets_delivery import FULL_ACCESS_PERM, OPENID_MEMBER_TYPE
    else:
        from lingxi.adapters.feishu_docx_delivery import FULL_ACCESS_PERM, OPENID_MEMBER_TYPE

    return any(
        member.get("member_type") == OPENID_MEMBER_TYPE
        and member.get("member_id") == open_id
        and member.get("perm") == FULL_ACCESS_PERM
        for member in members
    )


class DocumentDeliveryConsumer:
    """一轮：回收卡住的行、清理耗尽重试预算的行、认领新行、逐行驱动到底。

    单行失败按行隔离（同 ``apps/gateway/delivery.py::DeliveryConsumer.run_once``
    的姿态）：一行的异常不得带走同一轮里的其他行；循环级查询（认领、回收）
    失败则整轮降级为"这一轮无事可做"，下一轮重来。
    """

    def __init__(
        self,
        *,
        store: PostgresDocumentDeliveryStore,
        docx: Any,
        notifier: Any,
        sheets: Any = None,
        catalog: ContentCatalog | None = None,
        on_alert: AlertCallback | None = None,
        limit: int = DEFAULT_BATCH_LIMIT,
    ) -> None:
        """装配文档/表格投递消费者所需的存储、适配器与告警回调。"""
        self._store = store
        self._docx = docx
        # 表格能力可选——单测/未装配表格能力时可以不传，只要队列里不出现
        # delivery_type='sheet' 的行就不会被触碰（见 _process_claim 的分派）。
        self._sheets = sheets
        self._notifier = notifier
        self._catalog = catalog or default_content_catalog()
        self._alert = on_alert or _default_alert
        self._limit = limit

    def _housekeep(self) -> None:
        """清理耗尽重试预算与卡住的处理中行；两段各自降级，互不影响。"""
        try:
            exhausted = self._store.fail_exhausted_pending()
        except Exception as error:  # 见类文档：只降级这一段
            logger.error("文档投递：清理耗尽重试预算的待认领行失败 error=%s", type(error).__name__)
        else:
            if exhausted > 0:
                # 这类行此前被直接丢弃——转 failed 却从不上报。不带具体
                # task_id（这是一次批量转态，不是单行结果）。
                logger.error("gateway.document_delivery.attempts_exhausted count=%s", exhausted)
                self._alert("document_delivery_attempts_exhausted", "")

        try:
            requeued, reclaim_failed = self._store.reclaim_stale_processing()
        except Exception as error:  # 见类文档：只降级这一段
            logger.error("文档投递：回收卡住的处理中行失败 error=%s", type(error).__name__)
        else:
            del requeued  # 退回 pending 等下一轮重来，不是失败结果，不必上报。
            if reclaim_failed > 0:
                logger.error("gateway.document_delivery.reclaim_failed count=%s", reclaim_failed)
                self._alert("document_delivery_reclaim_failed", "")

    def _resend_pending_notices(self) -> None:
        """补发未确认送达的成功通知，排在认领新行之前。

        已经 succeeded 却没能通知到用户是最靠近"用户体感落空"的一类残留，
        不能因为本轮新到的建档请求把批量配额占满就一直排不上号。
        """
        try:
            pending_notices = self._store.claim_unnotified_succeeded(limit=self._limit)
        except Exception as error:  # 见类文档：只降级这一段
            logger.error("文档投递：查询待补发通知的行失败 error=%s", type(error).__name__)
            return
        for item in pending_notices:
            self._send_ready_notice(
                request_id=item.id,
                task_id=item.task_id,
                requester_open_id=item.requester_open_id,
                document_id=item.document_id,
                delivery_type=item.delivery_type,
                resource_url=item.resource_url,
                body_degraded_reason=item.body_degraded_reason,
            )

    def run_once(self) -> int:
        """跑一轮，返回本轮实际认领并处理的行数。"""
        self._housekeep()
        self._resend_pending_notices()

        try:
            claims = self._store.claim_pending(limit=self._limit)
        except Exception as error:  # 本轮无事可做，下一轮重来
            logger.error("文档投递：认领待处理行失败 error=%s", type(error).__name__)
            return 0

        for claim in claims:
            logger.info(
                "gateway.document_delivery.claimed task_id=%s attempts=%s",
                claim.task_id,
                claim.attempts,
            )
            try:
                self._process_claim(claim)
            except Exception as error:  # 一行失败不得带走同一轮的其他行
                logger.error(
                    "文档投递单行处理异常，本轮其余行不受影响 task_id=%s error=%s",
                    claim.task_id,
                    type(error).__name__,
                )
        return len(claims)

    def _process_claim(self, claim: DocumentDeliveryClaim) -> None:
        """按 ``claim.delivery_type`` 分派到 docx 或 sheet 的处理方法。"""
        if claim.delivery_type == DELIVERY_TYPE_SHEET:
            self._process_sheet_claim(claim)
        else:
            self._process_docx_claim(claim)

    def _drive_docx_channel(
        self, claim: DocumentDeliveryClaim
    ) -> tuple[str, list[dict[str, Any]], str | None]:
        """执行建档/续做、写正文、授权、读回确认四步。

        返回 ``(document_id, members, degraded_reason)``；异常不在这里分类，
        原样向上抛给调用方按类型归 ``failed``/``uncertain``。
        """
        document_id = claim.document_id
        body_degraded_reason = claim.body_degraded_reason
        if document_id is not None:
            # 检查点恢复路径：文档已经建好，只需要判断正文写没写过——这条
            # 判据换成服务端一次建档之后仍有判别力，不是恒真（见模块文档）。
            if not self._docx.read_body_children(document_id):
                self._docx.write_paragraphs(document_id, list(claim.paragraphs))
        else:
            document_id, body_degraded_reason = self._create_docx_body(claim)
        self._docx.grant_full_access(document_id, claim.requester_open_id)
        members = self._docx.read_members(document_id)
        return document_id, members, body_degraded_reason

    def _process_docx_claim(self, claim: DocumentDeliveryClaim) -> None:
        from lingxi.adapters.feishu_docx_delivery import FeishuDocxDeliveryError
        from lingxi.adapters.postgres_document_delivery import DocumentDeliveryOwnershipLost

        try:
            document_id, members, body_degraded_reason = self._drive_docx_channel(claim)
        except DocumentDeliveryOwnershipLost:
            # 建档检查点提交时发现持有权已经不在本次调用手里（典型：这次调用
            # 是一个被 reclaim_stale_processing 判定为"卡住"并回收过的慢
            # 消费者）。当场中止，交给真正持有它的那次调用。
            logger.warning(
                "文档投递：建档成功但持有权已丢失，放弃本行续做 task_id=%s", claim.task_id
            )
            return
        except FeishuDocxDeliveryError as error:
            if error.definite:
                self._fail(claim, last_error=error.code)
            else:
                # 结果不明（含一次建档超时/HTTP 5xx：服务端可能已经把整篇
                # 文档建出来，只是拿不到 id）→ uncertain，不自动重试，这行
                # 不会被再次认领，不会产生第二篇文档。
                self._uncertain(claim, last_error=error.code)
            return
        except ValueError as error:
            # 各动作的入参校验在**发出任何 HTTP 请求之前**就失败，抛纯
            # ValueError——没有请求真的发出去，重放必然得到同一个结论，归
            # failed 而不是 uncertain（uncertain 会误导排查方向）。
            self._fail(claim, last_error=type(error).__name__)
            return
        except Exception as error:  # 白名单反转：不能假设有副作用的调用
            # 异常时一定没有生效，只有上面显式捕获的两类才归 failed。
            self._uncertain(claim, last_error=type(error).__name__)
            return

        self._finalize_claim(
            claim,
            document_id=document_id,
            members=members,
            resource_url=None,
            body_degraded_reason=body_degraded_reason,
        )

    def _create_docx_body_via_markdown(
        self, claim: DocumentDeliveryClaim
    ) -> tuple[str | None, str | None]:
        """尝试一次建档路径，返回 ``(document_id, degraded_reason)``。

        ``document_id`` 为 ``None`` 表示这条路径不适用（未配置 markdown 或
        止损闸关闭）或前置守卫命中，调用方应改走段落路径；此时
        ``degraded_reason`` 是前置守卫产生的原因码（不适用时为 ``None``）。
        捕获范围窄到 ``PRE_FLIGHT_DEGRADE_REASONS``——它们都是发出请求
        **之前**判定的，改路不会产生第二篇文档；其余异常原样抛出。
        """
        from lingxi.adapters.feishu_docx_delivery import (
            PRE_FLIGHT_DEGRADE_REASONS,
            FeishuDocxDeliveryError,
        )

        if claim.markdown is None or not self._docx.markdown_convert_enabled:
            return None, None
        try:
            created = self._docx.create_document_with_markdown(claim.title, claim.markdown)
        except FeishuDocxDeliveryError as error:
            if error.code not in PRE_FLIGHT_DEGRADE_REASONS:
                raise
            logger.warning(
                "gateway.document_delivery.pre_flight_degrade task_id=%s reason=%s",
                claim.task_id,
                error.code,
            )
            return None, error.code
        document_id = created.document_id
        # 检查点：独立提交，不与后续步骤共享事务。这条路径上正文已经随建档
        # 写完，因此检查点一旦落下，"正文写没写过"就不再是问题。
        self._store.mark_document_created(request_id=claim.id, document_id=document_id)
        if created.degraded_reason is not None:
            self._store.mark_body_degraded(request_id=claim.id, reason=created.degraded_reason)
            logger.warning(
                "gateway.document_delivery.body_degraded task_id=%s reason=%s",
                claim.task_id,
                created.degraded_reason,
            )
        return document_id, created.degraded_reason

    def _create_docx_body(self, claim: DocumentDeliveryClaim) -> tuple[str, str | None]:
        """首次路径的「建档 + 写正文」两步，返回 ``(document_id, 降级原因)``。

        两条路径见模块文档「写正文步的幂等判据」：一次建档检查点在调用成功
        之后才提交，因此"检查点已落"蕴含"正文已写"；两步段落路径保留了
        "建了档、正文还没写"的中间态，恢复路径的读回判据因此仍有判别力。
        **降级检查点先于写正文提交**：先写后落时一次崩溃会把降级信号带走，
        恢复路径会发出不带降级说明的通知。
        """
        document_id, reason = self._create_docx_body_via_markdown(claim)
        if document_id is not None:
            return document_id, reason

        document_id = self._docx.create_document(claim.title)
        self._store.mark_document_created(request_id=claim.id, document_id=document_id)
        if reason is not None:
            self._store.mark_body_degraded(request_id=claim.id, reason=reason)
            logger.warning(
                "gateway.document_delivery.body_degraded task_id=%s reason=%s",
                claim.task_id,
                reason,
            )
        self._docx.write_paragraphs(document_id, list(claim.paragraphs))
        return document_id, reason

    def _process_sheet_claim(self, claim: DocumentDeliveryClaim) -> None:
        """表格分支：与 ``_process_docx_claim`` 逐项对应。

        差异见 ``adapters/feishu_sheets_delivery.py`` 模块文档「与文档交付的
        差异点」——写值天然幂等（无条件重放），查默认 sheet_id 是纯只读调用
        （同样无条件重放，不需要检查点），建表响应自带链接。
        """
        from lingxi.adapters.feishu_sheets_delivery import FeishuSheetsDeliveryError
        from lingxi.adapters.postgres_document_delivery import DocumentDeliveryOwnershipLost

        spreadsheet_token = claim.document_id
        resource_url = claim.resource_url
        try:
            if spreadsheet_token is None:
                spreadsheet_token, resource_url = self._sheets.create_spreadsheet(claim.title)
                # 检查点：独立提交，额外一并落 resource_url（链接随建表响应
                # 一起拿到，不需要第二次调用）。
                self._store.mark_document_created(
                    request_id=claim.id, document_id=spreadsheet_token, resource_url=resource_url
                )
            sheet_id = self._sheets.get_default_sheet_id(spreadsheet_token)
            self._sheets.write_values(
                spreadsheet_token, sheet_id, [list(row) for row in claim.paragraphs]
            )
            self._sheets.grant_full_access(spreadsheet_token, claim.requester_open_id)
            members = self._sheets.read_members(spreadsheet_token)
        except DocumentDeliveryOwnershipLost:
            logger.warning(
                "文档投递：建表成功但持有权已丢失，放弃本行续做 task_id=%s", claim.task_id
            )
            return
        except FeishuSheetsDeliveryError as error:
            if error.definite:
                self._fail(claim, last_error=error.code)
            else:
                self._uncertain(claim, last_error=error.code)
            return
        except ValueError as error:
            # 同 docx 分支：入参校验在发出请求之前就失败，归 failed。
            self._fail(claim, last_error=type(error).__name__)
            return
        except Exception as error:  # 白名单反转，同 docx 分支
            self._uncertain(claim, last_error=type(error).__name__)
            return

        self._finalize_claim(
            claim, document_id=spreadsheet_token, members=members, resource_url=resource_url
        )

    def _finalize_claim(
        self,
        claim: DocumentDeliveryClaim,
        *,
        document_id: str,
        members: list[dict[str, Any]],
        resource_url: str | None,
        body_degraded_reason: str | None = None,
    ) -> None:
        """docx/sheet 两条分支共用的收口：验证权限读回、落终态、发送通知。

        ``body_degraded_reason`` 只由 docx 分支传，非 ``None`` 时成功通知
        改用明示降级的文案；sheet 分支结构上恒为 ``None``。
        """
        from lingxi.adapters.postgres_document_delivery import DocumentDeliveryOwnershipLost

        if not _has_confirmed_full_access(
            members, claim.requester_open_id, delivery_type=claim.delivery_type
        ):
            # 读回结构正常，但没有找到目标 open_id 的 full_access 记录：结果
            # 不明（可能权限还没生效、也可能授权那一步实际没有成功），不得
            # 判 succeeded。
            self._uncertain(claim, last_error="permission_not_confirmed")
            return

        try:
            self._store.mark_succeeded(request_id=claim.id)
        except DocumentDeliveryOwnershipLost:
            # 流程已经全部跑完，但落终态这一步才发现持有权已经丢失——不发
            # 通知：这一行现在究竟是什么状态由真正持有它的那次调用决定。
            logger.warning(
                "文档投递：流程已跑完但持有权已丢失，放弃写入终态与通知 task_id=%s",
                claim.task_id,
            )
            return
        except Exception as error:  # 落库失败仍按结果不明处理，不假装成功
            self._uncertain(claim, last_error=type(error).__name__)
            return

        logger.info(
            "gateway.document_delivery.succeeded task_id=%s attempts=%s delivery_type=%s",
            claim.task_id,
            claim.attempts,
            claim.delivery_type,
        )
        self._send_ready_notice(
            request_id=claim.id,
            task_id=claim.task_id,
            requester_open_id=claim.requester_open_id,
            document_id=document_id,
            delivery_type=claim.delivery_type,
            resource_url=resource_url,
            body_degraded_reason=body_degraded_reason,
        )

    def _fail(self, claim: DocumentDeliveryClaim, *, last_error: str) -> None:
        from lingxi.adapters.postgres_document_delivery import DocumentDeliveryOwnershipLost

        try:
            self._store.mark_failed(request_id=claim.id, last_error=last_error)
        except DocumentDeliveryOwnershipLost:
            # 这一行已经不是我们的了——另一次调用可能已经落下了不同的结论。
            # 不覆盖、不告警：告警必须描述真实发生的终态。
            logger.warning(
                "文档投递：判定为 failed 但持有权已丢失，放弃写入终态与告警 task_id=%s",
                claim.task_id,
            )
            return
        except Exception as error:  # 记录失败不能让异常逃出本方法
            logger.error(
                "文档投递终态写入失败（failed）task_id=%s error=%s",
                claim.task_id,
                type(error).__name__,
            )
        logger.error(
            "gateway.document_delivery.failed task_id=%s attempts=%s last_error=%s",
            claim.task_id,
            claim.attempts,
            last_error,
        )
        # definite 失败必须可见：与 _uncertain 已有的告警对称补上；
        # V-交付-03「未确认成功不自动重发」覆盖的是重试语义，不代表 failed
        # 终态本身不需要人工核对。
        self._alert("document_delivery_failed", claim.task_id)
        key, dedupe_prefix, variables = (
            ("delivery.sheet_failed", "sheet-failed", {"reference": claim.task_id})
            if claim.delivery_type == DELIVERY_TYPE_SHEET
            else ("delivery.document_failed", "document-failed", {})
        )
        self._send_terminal_notice(
            claim, key=key, dedupe_prefix=dedupe_prefix, template_variables=variables
        )

    def _uncertain(self, claim: DocumentDeliveryClaim, *, last_error: str) -> None:
        from lingxi.adapters.postgres_document_delivery import DocumentDeliveryOwnershipLost

        try:
            self._store.mark_uncertain(request_id=claim.id, last_error=last_error)
        except DocumentDeliveryOwnershipLost:
            # 同 _fail：持有权已经不在我们手里，不覆盖、不告警。
            logger.warning(
                "文档投递：判定为 uncertain 但持有权已丢失，放弃写入终态与告警 task_id=%s",
                claim.task_id,
            )
            return
        except Exception as error:  # 记录失败不能让异常逃出本方法
            logger.error(
                "文档投递终态写入失败（uncertain）task_id=%s error=%s",
                claim.task_id,
                type(error).__name__,
            )
        logger.warning(
            "gateway.document_delivery.uncertain task_id=%s attempts=%s last_error=%s",
            claim.task_id,
            claim.attempts,
            last_error,
        )
        # V-交付-03：未确认成功不自动重发，转人工核对——记告警级审计。不建议
        # 用户自行重试：成因可能是有副作用的调用网络异常，无法证明没有生效。
        self._alert("document_delivery_uncertain", claim.task_id)
        key, dedupe_prefix, variables = (
            ("delivery.sheet_uncertain", "sheet-uncertain", {"reference": claim.task_id})
            if claim.delivery_type == DELIVERY_TYPE_SHEET
            else ("delivery.document_uncertain", "document-uncertain", {})
        )
        self._send_terminal_notice(
            claim, key=key, dedupe_prefix=dedupe_prefix, template_variables=variables
        )

    def _resolve_ready_notice(
        self,
        *,
        delivery_type: str,
        document_id: str,
        resource_url: str | None,
        body_degraded_reason: str | None,
    ) -> tuple[str, str, str]:
        """算出就绪通知的 ``(url, content_key, dedupe_prefix)``。

        每条文案的归因必须对它的触发源逐字为真：``server_simplified_body``
        是服务端自己简化的，文档其实仍带格式；两道前置守卫（长度、标题
        形态）各有专条；其余（历史行）留在通用的降级文案。去重前缀在 docx
        侧三条降级文案间共用——原发送与补发是同一条通知的两次尝试，不是
        两条独立通知。
        """
        from lingxi.adapters.feishu_docx_delivery import (
            BODY_TOO_LONG,
            SERVER_SIMPLIFIED_BODY,
            TITLE_NOT_EMBEDDABLE,
        )

        if delivery_type == DELIVERY_TYPE_SHEET:
            if not isinstance(resource_url, str) or not resource_url:
                # 结构性不应发生：resource_url 与 document_id 在同一次
                # mark_document_created 调用里一起落盘。响亮失败而不是猜测
                # 一个链接。
                raise LookupError("sheet 分支缺少可用的 resource_url")
            return resource_url, "delivery.sheet_ready", "sheet-ready"

        url = self._docx.document_url(document_id)
        if body_degraded_reason is None:
            content_key = "delivery.document_ready"
        elif body_degraded_reason == SERVER_SIMPLIFIED_BODY:
            content_key = "delivery.document_ready_simplified"
        elif body_degraded_reason == BODY_TOO_LONG:
            content_key = "delivery.document_ready_degraded_too_long"
        elif body_degraded_reason == TITLE_NOT_EMBEDDABLE:
            content_key = "delivery.document_ready_degraded_title"
        else:
            # 默认落在段落路径那条：未来新增的原因码在补上分派之前先说"已按
            # 段落交付"，是可被用户当场证伪的过度告知，不是一句假担保。
            content_key = "delivery.document_ready_degraded"
        return url, content_key, "document-ready"

    def _send_ready_notice(
        self,
        *,
        request_id: str,
        task_id: str,
        requester_open_id: str,
        document_id: str,
        delivery_type: str = "docx",
        resource_url: str | None = None,
        body_degraded_reason: str | None = None,
    ) -> None:
        """成功后把文档/表格链接作为追加消息发给提问用户。

        失败只记日志/告警，不改写已经落库的 ``succeeded`` 终态——资源已经
        建好且用户已经拿到权限，通知只是锦上添花的提醒。显式字段而不是接受
        整个 ``DocumentDeliveryClaim``：本方法还服务补发未确认送达通知的
        路径，那条路径没有完整 claim。**成功才置位** ``notified_at``：与
        ``mark_notified`` 的幂等闸配合，补发路径下一轮会自然跳过已确认的行。
        """
        try:
            url, content_key, dedupe_prefix = self._resolve_ready_notice(
                delivery_type=delivery_type,
                document_id=document_id,
                resource_url=resource_url,
                body_degraded_reason=body_degraded_reason,
            )
            content = self._catalog.text(content_key, url=url)
            self._notifier.send_text(
                open_id=requester_open_id,
                text=content.text,
                dedupe_key=f"{dedupe_prefix}:{request_id}",
            )
            self._store.mark_notified(request_id=request_id)
        except Exception as error:  # 通知失败不得回滚已经确认的交付结果
            logger.error(
                "文档投递完成通知发送失败，交付结果不受影响 task_id=%s error=%s",
                task_id,
                type(error).__name__,
            )
            self._alert("document_delivery_notice_failed", task_id)

    def _send_terminal_notice(
        self,
        claim: DocumentDeliveryClaim,
        *,
        key: str,
        dedupe_prefix: str,
        template_variables: dict[str, Any] | None = None,
    ) -> None:
        """failed/uncertain 终态后把固定文案作为追加消息发给提问用户。

        失败只记日志/告警，不改写已经落库的终态——同 ``_send_ready_notice``
        的姿态。``dedupe_prefix`` 与成功那一路的既有前缀各自独立，三种终态
        各自的通知不会互相去重掉。``template_variables``：``ContentCatalog.
        text`` 要求调用方变量集合与模板变量集合逐一相等，sheet 分支的 key
        带 ``{reference}``，由调用方显式传入。
        """
        try:
            content = self._catalog.text(key, **(template_variables or {}))
            self._notifier.send_text(
                open_id=claim.requester_open_id,
                text=content.text,
                dedupe_key=f"{dedupe_prefix}:{claim.id}",
            )
        except Exception as error:  # 通知失败不得回滚已经落库的终态
            logger.error(
                "文档投递终态通知发送失败 task_id=%s key=%s error=%s",
                claim.task_id,
                key,
                type(error).__name__,
            )
            self._alert("document_delivery_notice_failed", claim.task_id)

    def run_forever(
        self,
        *,
        stop: threading.Event,
        poll_interval_seconds: float,
        heartbeat: Callable[[], None] | None = None,
        on_tick: Callable[[], None] | None = None,
    ) -> None:
        """长期循环：每轮之间固定等待一个轮询间隔。

        同 ``apps/gateway/delivery.py::DeliveryConsumer.run_forever`` 的姿态。
        """
        while not stop.is_set():
            if heartbeat is not None:
                try:
                    heartbeat()
                except Exception as error:  # 心跳失败不能带走投递职责
                    logger.error("文档投递心跳记录失败 error=%s", type(error).__name__)
            if on_tick is not None:
                try:
                    on_tick()
                except Exception as error:  # 告警自身失败不能带走投递职责
                    logger.error("文档投递告警状态机推进失败 error=%s", type(error).__name__)
            try:
                self.run_once()
            except Exception as error:  # 一轮异常不得带走整条循环
                logger.error("文档投递循环级异常，本轮降级后继续 error=%s", type(error).__name__)
            stop.wait(poll_interval_seconds)
        logger.info("文档投递消费循环已停止")


def _build_document_delivery_transports(config: Any) -> tuple[Any, Any, Any]:
    """按配置装配 docx/sheets 适配器与用户消息通知器。"""
    from lingxi.adapters.feishu_docx_delivery import LarkDocxDelivery
    from lingxi.adapters.feishu_sheets_delivery import LarkSheetsDelivery
    from lingxi.adapters.feishu_tenant_token import FeishuTenantTokenClient
    from lingxi.adapters.feishu_user_message import FeishuUserMessages
    from lingxi.core.permission.tenant_token_supply import TenantAccessTokenSupply

    #: 与管理群/权限变化通知/花名册日报各自独立前缀同一纪律——同一个
    #: ``im/v1/messages`` 接口下，每条独立投递语义必须有自己的去重命名空间。
    #: 17 + 32 = 49 字符，在飞书 50 字符上限内。
    notice_uuid_prefix = "lingxi-doc-ready-"

    tenant_access_token = TenantAccessTokenSupply(
        fetch=FeishuTenantTokenClient(
            base_url=config.feishu_base_url,
            app_id=config.app_id,
            app_secret=str(config.app_secret),
        ).fetch,
    )
    docx = LarkDocxDelivery(
        base_url=config.feishu_base_url,
        tenant_access_token=tenant_access_token,
        tenant_domain=config.tenant_domain,
        # 装配层把已经读好的布尔值传进去（adapters/ 不直接读 os.environ，
        # 见代码框架「三、横切约定」）。
        markdown_convert_enabled=config.markdown_convert_enabled,
    )
    sheets = LarkSheetsDelivery(
        base_url=config.feishu_base_url,
        tenant_access_token=tenant_access_token,
    )
    notifier = FeishuUserMessages(
        base_url=config.feishu_base_url,
        app_id=config.app_id,
        app_secret=str(config.app_secret),
        uuid_prefix=notice_uuid_prefix,
    )
    return docx, sheets, notifier


def assemble_document_delivery_consumer(
    config: Any, *, store: Any = None, alerting_duty: Any = None
) -> DocumentDeliveryConsumer | None:
    """装配文档/表格投递独立消费循环。

    ``config.tenant_domain`` 未配置时返回 ``None``——循环整体不注册（docx 与
    sheet 共用同一条循环、同一个失败关闭开关），调用方据此决定要不要起第二条
    后台线程，不会用一个猜测的域名硬跑。``LarkSheetsDelivery`` 本身不需要
    ``tenant_domain``，这里仍复用同一个开关只是为了不新增第二个装配条件。
    """
    if config.tenant_domain is None:
        logger.info("gateway.document_delivery.duty_not_registered reason=missing_tenant_domain")
        return None

    docx, sheets, notifier = _build_document_delivery_transports(config)
    return DocumentDeliveryConsumer(
        store=store
        or PostgresDocumentDeliveryStore(
            str(config.postgres_dsn), timeouts=config.postgres_timeouts
        ),
        docx=docx,
        sheets=sheets,
        notifier=notifier,
        on_alert=alerting_duty.delivery_alert_callback() if alerting_duty is not None else None,
    )


__all__ = [
    "DEFAULT_BATCH_LIMIT",
    "LOOP_ALERT_TRACE_ID",
    "DocumentDeliveryConsumer",
    "assemble_document_delivery_consumer",
]
