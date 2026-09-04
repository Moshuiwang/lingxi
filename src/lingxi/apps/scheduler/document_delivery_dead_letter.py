"""文档投递死信扫描 + 正文到期擦除：scheduler 侧的轻量周期职责。

补的是两个 gateway 独立消费循环自己够不到的洞：**死信面**——
``task_document_delivery_request`` 停在 ``pending`` 超过
:data:`~lingxi.adapters.postgres_document_delivery.PENDING_DEAD_LETTER_AFTER`
仍未被任何 gateway 实例认领，gateway 侧既有的两条止损都摸不到它；**正文到期
擦除**——待投递/失败/结果不明/尚未证明清除后可访问的正文最迟 24 小时清除。
两件事共用同一个职责、同一个扫描周期，都是对同一张表的一次
``UPDATE ... WHERE`` 扫描。**scheduler 侧无法直接给用户发消息**：死信转
``failed`` 之后不再有任何职责通知该用户，只能通过管理群告警触发人工核查，
已知边界不做补偿。**职责在表不存在时不注册不崩溃**：两次 ``UPDATE`` 各自
失败只记一条审计，不影响同轮其它职责，下一轮原样重试。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from lingxi.apps.scheduler.audit import AuditSink
from lingxi.apps.scheduler.config import SchedulerConfig

logger = logging.getLogger(__name__)


class _DeadLetterStore(Protocol):
    """最小读写口。

    实现是
    :class:`~lingxi.adapters.postgres_document_delivery.PostgresDocumentDeliveryStore`。
    """

    def fail_expired_pending(self) -> int: ...

    def redact_expired_content(self) -> int: ...


@dataclass(frozen=True)
class DocumentDeliveryMaintenanceReport:
    """一轮的结果。只有计数，没有任何行内容——同其它定时职责的既有纪律。"""

    #: 本轮判定为死信、转 ``failed`` 的行数。
    dead_lettered: int = 0
    #: 本轮到期擦除正文的行数。
    content_redacted: int = 0

    def audit_facts(self) -> dict[str, Any]:
        """把计数字段展开成一份可以直接喂给审计记录的字典。"""
        return {"dead_lettered": self.dead_lettered, "content_redacted": self.content_redacted}


class DocumentDeliveryMaintenanceDuty:
    """一轮：扫一次死信、擦一次到期正文，两者各自独立失败隔离。

    同 ``apps/gateway/document_delivery.py::DocumentDeliveryConsumer.run_once``
    对 ``fail_exhausted_pending``/``reclaim_stale_processing`` 的姿态：一段查询
    失败只降级这一段，不带走另一段，也不让异常杀死整条定时职责循环——那道防线
    已经在 :class:`~lingxi.apps.scheduler.loop.SchedulerLoop` 里，见模块文档。
    """

    name = "文档投递死信扫描与正文到期擦除"

    def __init__(
        self,
        *,
        store: _DeadLetterStore,
        audit: AuditSink,
        alert: Callable[[str, str], None] | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        """按注入的死信保管库/审计/告警回调装配一个维护职责实例。"""
        self._store = store
        self._audit = audit
        self._alert = alert
        self._stop = threading.Event() if stop is None else stop

    @property
    def stopping(self) -> bool:
        """是否已收到停止信号。"""
        return self._stop.is_set()

    def request_stop(self) -> None:
        """置位停止信号：本轮及之后不再处置任何行。"""
        self._stop.set()

    def run_once(self) -> DocumentDeliveryMaintenanceReport | None:
        """已经在停止中就一条都不处置。返回 ``None`` 表示本轮未执行。"""
        if self._stop.is_set():
            return None

        dead_lettered = self._sweep_dead_letters()
        content_redacted = self._sweep_expired_content()
        report = DocumentDeliveryMaintenanceReport(
            dead_lettered=dead_lettered, content_redacted=content_redacted
        )
        if dead_lettered or content_redacted:
            # 零候选时不记审计（同 ``StalledProvisioningDuty`` 的既有纪律）：本职责
            # 每轮都跑，健康系统里绝大多数 tick 什么都不该做。
            self._audit.record("document_delivery_maintenance.completed", **report.audit_facts())
            logger.info(
                "文档投递维护：死信转 failed %s 行，正文到期擦除 %s 行",
                dead_lettered,
                content_redacted,
            )
        return report

    def _sweep_dead_letters(self) -> int:
        try:
            count = self._store.fail_expired_pending()
        except Exception as error:  # 只降级这一段，见类文档
            self._audit.record(
                "document_delivery_maintenance.dead_letter_sweep_failed",
                error=type(error).__name__,
            )
            logger.error("文档投递死信扫描失败 error=%s", type(error).__name__)
            return 0
        if count > 0:
            # R-2：这批行此前从未有过 gateway 消费循环的"我要报告"路径（它们从未
            # 被 claim_pending 认领过），必须在这里补一条管理告警——否则"gateway
            # 一直没配置/没部署"这件事永远无声。
            logger.error("gateway.document_delivery.pending_expired count=%s", count)
            if self._alert is not None:
                try:
                    # 批量计数，没有单一 task_id 可代表——退化为不带 trace_id 的
                    # 信号（见 ``AlertingDuty.delivery_alert_callback`` 对空/非法
                    # task_id 的既有容错）。
                    self._alert("document_delivery_pending_expired", "")
                except Exception as error:  # 告警失败不得带走已完成的扫描
                    logger.error("文档投递死信告警发送失败 error=%s", type(error).__name__)
        return count

    def _sweep_expired_content(self) -> int:
        try:
            return self._store.redact_expired_content()
        except Exception as error:  # 只降级这一段，见类文档
            self._audit.record(
                "document_delivery_maintenance.content_redaction_failed",
                error=type(error).__name__,
            )
            logger.error("文档投递正文到期擦除失败 error=%s", type(error).__name__)
            return 0


def _wire_document_delivery_maintenance_duty(
    duties: list,
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
    alerting_duty: Any,
) -> None:
    """装配并按需追加进 ``duties``。

    住在本文件而不是 ``assembly.py``，同 ``_wire_daily_report_duty`` 的既有
    形状：调用点只占一行。**总能注册**（唯一依赖 ``LINGXI_POSTGRES_DSN``），
    因此不需要 ``if duty is not None`` 判断。复用与 gateway 侧同一个
    ``delivery_alert_callback``：告警文案归一化、kind 前缀与去重命名空间三个
    进程共用同一份纪律，不重复实现。
    """
    from lingxi.adapters.postgres_document_delivery import PostgresDocumentDeliveryStore

    duties.append(
        DocumentDeliveryMaintenanceDuty(
            store=PostgresDocumentDeliveryStore(
                config.postgres_dsn, timeouts=config.postgres_timeouts
            ),
            audit=audit,
            alert=(alerting_duty.delivery_alert_callback() if alerting_duty is not None else None),
            stop=stop,
        )
    )


__all__ = [
    "DocumentDeliveryMaintenanceDuty",
    "DocumentDeliveryMaintenanceReport",
]
