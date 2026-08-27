"""Gateway 文档投递独立消费循环（Issue #341 S-ES-3，依据 #341 评论 5434520679 审定设计）。

worker 侧（``apps/worker/service.py`` 经
``adapters/postgres_conversation/_queue_outbox.py::write_terminal_event``）已经在
任务终态事务里插入了一行 ``task_document_delivery_request``（状态 ``pending``，
迁移 0074）。本模块认领这些行，驱动"建档 → 写正文 → 授予可管理 → 读回确认"
四步（S-ES-1 ``adapters/feishu_docx_delivery.py::LarkDocxDelivery``），成功后把
文档链接作为一条追加消息发给提问用户。

**独立于既有 ``DeliveryConsumer``（``apps/gateway/delivery.py``），不共用同一条
批次循环**：那条循环的职责是把已经产生的问数终态尽快送到用户面前（卡片流式、
文本兜底），轮询节奏是"越快越好"；本循环的四步里至少一步是真实飞书 HTTP 调用
（``feishu_docx_delivery.REQUEST_TIMEOUT_SECONDS`` = 20 秒/步），如果混进同一条
循环，一次建档慢查询会连带阻塞其他用户的终态卡片/文本送达——这正是设计要避免的
"建档四步阻塞他人终态送达"。两条循环各自的后台线程、各自的轮询间隔、各自的失败
隔离，只共享同一个进程内的 ``stop_event`` 与 ``run_delivery_loop``/
``delivery_thread_watchdog`` 这两个与"消费循环长什么样"无关的通用线程管理工具
（见 ``apps/gateway/__init__.py``）。

**四步的失败分类只有两种，均在 :meth:`DocumentDeliveryConsumer._process_claim`
里体现**：

1. **definite（飞书明确拒绝）** → ``failed``：
   ``adapters.feishu_docx_delivery.FeishuDocxDeliveryError(definite=True)``，
   即收到非 0 的飞书业务错误码。这是系统已经确定、不会因为重试而改变结论的
   失败，不需要人工核对具体是哪一次调用；``last_error`` 只记错误分类码
   （``feishu_code_<n>`` 形态），不含正文、不含凭据。
2. **结果不明（网络类异常、响应缺失可回读标识、读回不含目标或权限档位不对）**
   → ``uncertain``：白名单反转——只有第 1 类明确失败才归 ``failed``，
   **其余一切**（``FeishuDocxDeliveryError(definite=False)``、
   ``LookupError``、任何未预期异常、``read_members`` 结构正常但没有目标
   ``open_id`` 或档位不是 ``full_access``）都归结果不明。原因与
   ``apps/gateway/delivery.py`` 模块说明的"白名单反转（独立审核 R-1）"一致：
   飞书的四个动作里有三个（建档、写正文、授权）是有副作用的写操作，一次网络
   异常或响应解析失败**不能证明动作没有生效**，把它当成 ``failed`` 允许
   下一轮重新调用，等于可能对同一次逻辑请求重复建档/重复授权。**``uncertain``
   不自动重试**（V-交付-03：未确认成功不自动重发，转人工核对）——见
   :meth:`PostgresDocumentDeliveryStore.mark_uncertain` 与本模块
   ``_process_claim`` 的调用点，命中后只记一条告警级审计，不会被
   :meth:`PostgresDocumentDeliveryStore.claim_pending` 再次认领
   （``status='uncertain'`` 不在该方法的查询谓词范围内）。

**检查点持久化在 :meth:`_process_claim` 内部体现**：只有 ``claim.document_id``
为 ``None`` 时才调用 ``create_document``，紧接着立刻调用
``PostgresDocumentDeliveryStore.mark_document_created`` 单独提交——这是一次独立
的数据库往返，不与后续三步共享事务。如果消费进程在这一步与下一步之间崩溃，
下一次认领到同一行时 ``claim.document_id`` 已经非空，四步流程从 ``write_
paragraphs`` 续做，绝不二次调用 ``create_document``（S0 探针实测：飞书建文档
接口没有幂等键，重放会真的多建一篇孤儿文档）。

**成功通知走"追加消息"，不进入任何已有话题的投递 outbox**：文档交付可能发生在
原问数任务已经确认送达很久之后（``uncertain``/``failed`` 转 ``pending`` 的
重试窗口、gateway 与 worker 各自的处理延迟），复用 ``core.execution.card_stream``
那一整套"同话题终态"语义没有意义——这里只是**另主动**发一条独立的文本消息给
这个人，复用 ``adapters.feishu_user_message.FeishuUserMessages``（与权限变化
通知同一条出站信道：``im/v1/messages`` + ``receive_id_type=open_id``）。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from lingxi.adapters.postgres_document_delivery import (
    DocumentDeliveryClaim,
    PostgresDocumentDeliveryStore,
)
from lingxi.config.content import ContentCatalog, default_content_catalog

logger = logging.getLogger(__name__)

AlertCallback = Callable[[str, str], None]

#: 循环级异常告警的占位 trace_id（与 ``apps/gateway/delivery.py`` 的
#: ``LOOP_ALERT_TRACE_ID`` 同一手法，各自独立命名——两条循环互不相关，共用同一个
#: 占位符会让管理群通知分不清告警来自哪一条循环）。
LOOP_ALERT_TRACE_ID = "gateway-document-delivery-loop"

#: 一轮最多认领并处理的行数——与 ``DeliveryConsumer`` 的默认批量同量级，不是
#: 精确调校值：文档交付预期是低频动作（用户显式要求生成文档才会命中），不需要
#: 为它单独暴露一个环境变量。
DEFAULT_BATCH_LIMIT = 5


def _default_alert(kind: str, task_id: str) -> None:
    """默认告警出口：结构化日志。真实告警路由由调用方注入（同
    ``apps/gateway/delivery.py::_default_alert`` 的姿态）。
    """

    logger.error("文档投递告警 kind=%s task_id=%s", kind, task_id)


def _has_confirmed_full_access(members: list[dict[str, Any]], open_id: str) -> bool:
    """判定 read_members 读回结果是否确认目标 open_id 具备 full_access。

    与 ``scripts/probe_drive_folder_permissions.py`` 的 ``_member_signature`` 取值
    口径一致（``member_type``/``member_id``/``perm`` 三元组），只是这里只关心
    "有没有恰好一条命中目标 open_id 且档位是 full_access 的记录"这一个布尔结论，
    不需要整份签名。
    """

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
    的姿态）：一行的异常不得带走同一轮里的其他行；循环级查询（认领、回收）失败
    则整轮降级为"这一轮无事可做"，下一轮重来，不让一次瞬时数据库错误杀死后台
    线程本身（真正兜底见 ``apps/gateway/__init__.py`` 的 ``run_delivery_loop``/
    ``delivery_thread_watchdog``）。
    """

    def __init__(
        self,
        *,
        store: PostgresDocumentDeliveryStore,
        docx: Any,
        notifier: Any,
        catalog: ContentCatalog | None = None,
        on_alert: AlertCallback | None = None,
        limit: int = DEFAULT_BATCH_LIMIT,
    ) -> None:
        self._store = store
        self._docx = docx
        self._notifier = notifier
        self._catalog = catalog or default_content_catalog()
        self._alert = on_alert or _default_alert
        self._limit = limit

    def run_once(self) -> int:
        """跑一轮，返回本轮实际认领并处理的行数。"""

        try:
            self._store.fail_exhausted_pending()
        except Exception as error:  # noqa: BLE001 - 见类文档：只降级这一段
            logger.error("文档投递：清理耗尽重试预算的待认领行失败 error=%s", type(error).__name__)

        try:
            self._store.reclaim_stale_processing()
        except Exception as error:  # noqa: BLE001 - 见类文档：只降级这一段
            logger.error("文档投递：回收卡住的处理中行失败 error=%s", type(error).__name__)

        try:
            claims = self._store.claim_pending(limit=self._limit)
        except Exception as error:  # noqa: BLE001 - 本轮无事可做，下一轮重来
            logger.error("文档投递：认领待处理行失败 error=%s", type(error).__name__)
            return 0

        for claim in claims:
            logger.info(
                "gateway.document_delivery.claimed task_id=%s attempts=%s", claim.task_id, claim.attempts
            )
            try:
                self._process_claim(claim)
            except Exception as error:  # noqa: BLE001 - 一行失败不得带走同一轮的其他行
                logger.error(
                    "文档投递单行处理异常，本轮其余行不受影响 task_id=%s error=%s",
                    claim.task_id,
                    type(error).__name__,
                )
        return len(claims)

    def _process_claim(self, claim: DocumentDeliveryClaim) -> None:
        from lingxi.adapters.feishu_docx_delivery import FeishuDocxDeliveryError

        document_id = claim.document_id
        try:
            if document_id is None:
                document_id = self._docx.create_document(claim.title)
                # 检查点：独立提交，不与下面三步共享事务（见模块说明）。
                self._store.mark_document_created(request_id=claim.id, document_id=document_id)
            self._docx.write_paragraphs(document_id, list(claim.paragraphs))
            self._docx.grant_full_access(document_id, claim.requester_open_id)
            members = self._docx.read_members(document_id)
        except FeishuDocxDeliveryError as error:
            if error.definite:
                self._fail(claim, last_error=error.code)
            else:
                self._uncertain(claim, last_error=error.code)
            return
        except Exception as error:  # noqa: BLE001 - 白名单反转（同 delivery.py R-1）：
            # 只有上面显式捕获的 definite FeishuDocxDeliveryError 才归 failed，
            # 其余一切（LookupError、未预期异常）都归结果不明——不能假设一次
            # 有副作用的调用在异常时一定没有生效。
            self._uncertain(claim, last_error=type(error).__name__)
            return

        if not _has_confirmed_full_access(members, claim.requester_open_id):
            # 读回结构正常，但没有找到目标 open_id 的 full_access 记录：结果不明
            # （可能是权限还没有生效、也可能是授权那一步实际没有成功），不得判
            # succeeded（测试③锚点）。
            self._uncertain(claim, last_error="permission_not_confirmed")
            return

        try:
            self._store.mark_succeeded(request_id=claim.id)
        except Exception as error:  # noqa: BLE001 - 落库失败仍按结果不明处理，不假装成功
            self._uncertain(claim, last_error=type(error).__name__)
            return

        logger.info(
            "gateway.document_delivery.succeeded task_id=%s attempts=%s", claim.task_id, claim.attempts
        )
        self._send_ready_notice(claim, document_id)

    def _fail(self, claim: DocumentDeliveryClaim, *, last_error: str) -> None:
        try:
            self._store.mark_failed(request_id=claim.id, last_error=last_error)
        except Exception as error:  # noqa: BLE001 - 记录失败不能让异常逃出本方法
            logger.error(
                "文档投递终态写入失败（failed）task_id=%s error=%s", claim.task_id, type(error).__name__
            )
        logger.error(
            "gateway.document_delivery.failed task_id=%s attempts=%s last_error=%s",
            claim.task_id,
            claim.attempts,
            last_error,
        )

    def _uncertain(self, claim: DocumentDeliveryClaim, *, last_error: str) -> None:
        try:
            self._store.mark_uncertain(request_id=claim.id, last_error=last_error)
        except Exception as error:  # noqa: BLE001 - 记录失败不能让异常逃出本方法
            logger.error(
                "文档投递终态写入失败（uncertain）task_id=%s error=%s", claim.task_id, type(error).__name__
            )
        logger.warning(
            "gateway.document_delivery.uncertain task_id=%s attempts=%s last_error=%s",
            claim.task_id,
            claim.attempts,
            last_error,
        )
        # V-交付-03：未确认成功不自动重发，转人工核对——记告警级审计。
        self._alert("document_delivery_uncertain", claim.task_id)

    def _send_ready_notice(self, claim: DocumentDeliveryClaim, document_id: str) -> None:
        """成功后把文档链接作为追加消息发给提问用户；失败只记日志/告警，不改写
        已经落库的 ``succeeded`` 终态——文档已经建好且用户已经拿到权限，通知只是
        一次锦上添花的提醒，不是这条请求"是否成功"的判据。
        """

        try:
            url = self._docx.document_url(document_id)
            content = self._catalog.text("delivery.document_ready", url=url)
            self._notifier.send_text(
                open_id=claim.requester_open_id,
                text=content.text,
                dedupe_key=f"document-ready:{claim.id}",
            )
        except Exception as error:  # noqa: BLE001 - 通知失败不得回滚已经确认的交付结果
            logger.error(
                "文档投递完成通知发送失败，交付结果不受影响 task_id=%s error=%s",
                claim.task_id,
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
        """长期循环：每轮之间固定等待一个轮询间隔（同
        ``apps/gateway/delivery.py::DeliveryConsumer.run_forever`` 的姿态）。
        """

        while not stop.is_set():
            if heartbeat is not None:
                try:
                    heartbeat()
                except Exception as error:  # noqa: BLE001 - 心跳失败不能带走投递职责
                    logger.error("文档投递心跳记录失败 error=%s", type(error).__name__)
            if on_tick is not None:
                try:
                    on_tick()
                except Exception as error:  # noqa: BLE001 - 告警自身失败不能带走投递职责
                    logger.error("文档投递告警状态机推进失败 error=%s", type(error).__name__)
            try:
                self.run_once()
            except Exception as error:  # noqa: BLE001 - 一轮异常不得带走整条循环
                logger.error("文档投递循环级异常，本轮降级后继续 error=%s", type(error).__name__)
            stop.wait(poll_interval_seconds)
        logger.info("文档投递消费循环已停止")


def assemble_document_delivery_consumer(
    config: Any, *, store: Any = None, alerting_duty: Any = None
) -> DocumentDeliveryConsumer | None:
    """装配文档投递独立消费循环（Issue #341 S-ES-3）。

    ``config.tenant_domain`` 未配置时返回 ``None``——循环整体不注册，与既有
    ``roster_audit.duty_not_registered`` 等姿态一致的失败关闭（调用方
    ``apps/gateway/__init__.py::main`` 据此决定要不要起第二条后台线程），不会
    用一个猜测的域名硬跑，也不会尝试装配任何飞书客户端或数据库连接。
    """

    if config.tenant_domain is None:
        logger.info("gateway.document_delivery.duty_not_registered reason=missing_tenant_domain")
        return None

    from lingxi.adapters.feishu_docx_delivery import LarkDocxDelivery
    from lingxi.adapters.feishu_tenant_token import FeishuTenantTokenClient
    from lingxi.adapters.feishu_user_message import FeishuUserMessages
    from lingxi.core.permission.tenant_token_supply import TenantAccessTokenSupply

    #: 与管理群/权限变化通知/花名册日报各自独立前缀同一纪律——同一个
    #: ``im/v1/messages`` 接口下，每条独立投递语义必须有自己的去重命名空间
    #: （见 ``adapters/feishu_group_message.py`` 的 ``uuid_prefix`` 文档）。
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
    )
    notifier = FeishuUserMessages(
        base_url=config.feishu_base_url,
        app_id=config.app_id,
        app_secret=str(config.app_secret),
        uuid_prefix=notice_uuid_prefix,
    )
    return DocumentDeliveryConsumer(
        store=store
        or PostgresDocumentDeliveryStore(str(config.postgres_dsn), timeouts=config.postgres_timeouts),
        docx=docx,
        notifier=notifier,
        on_alert=alerting_duty.delivery_alert_callback() if alerting_duty is not None else None,
    )


__all__ = [
    "DEFAULT_BATCH_LIMIT",
    "LOOP_ALERT_TRACE_ID",
    "DocumentDeliveryConsumer",
    "assemble_document_delivery_consumer",
]
