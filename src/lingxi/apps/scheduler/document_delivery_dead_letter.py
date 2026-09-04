"""文档投递死信扫描 + 正文到期擦除：scheduler 侧的轻量周期职责（Issue #341 R-2/P1-3）。

补的是两个 gateway 独立消费循环（``apps/gateway/document_delivery.py``）自己够不到
的洞：

1. **死信面（R-2 必修）**：``task_document_delivery_request`` 停在 ``pending``
   超过 :data:`~lingxi.adapters.postgres_document_delivery.PENDING_DEAD_LETTER_AFTER`
   （30 分钟）仍未被任何 gateway 实例认领——``attempts`` 永远是 0（``claim_pending``
   从未认领过），gateway 侧既有的两条止损（``fail_exhausted_pending``/
   ``reclaim_stale_processing``）都摸不到它。这类死信只有一种成因：gateway 从未
   配置 ``LINGXI_GATEWAY_TENANT_DOMAIN``、进程没有部署、或已经整条死掉。**scheduler
   与 gateway 是两个独立部署单元、独立崩溃域**——只要 scheduler 还活着，这条死信面
   就恒在，不依赖 gateway 是否配置或存活；行转 ``failed`` 之后，即使 gateway 后来
   配好也不会再消费它（``claim_pending`` 只认 ``status='pending'``），用户侧由
   部署契约兜底（见 ``deploy/.env.example`` 里 worker 文档交付开关与 gateway 域名
   的互相引用说明）。
2. **正文到期擦除（``V-投递-06``）**：待投递/失败/结果不明/尚未证明清除后可访问
   的正文最迟 24 小时清除，与 ``task_delivery_event``（迁移 0059）同一条产品口径。

两件事共用同一个职责、同一个扫描周期，而不是各开一个——都是对同一张表的一次
``UPDATE ... WHERE`` 扫描，没有理由为它们各自起一整套装配、审计前缀与告警接线。

**scheduler 侧无法直接给用户发消息**（scheduler 没有面向单个用户的出站信道，只有
面向管理群的告警）：死信转 ``failed`` 之后，"告诉这个用户失败了"这一步不在本职责
里发生——那是 gateway 消费循环 ``_fail``/``_uncertain`` 的职责（opus 审查 R-1 第
3 条），而这一行此刻已经不会再被 gateway 认领到。**已知边界，如实登记，不补偿**：
用户在这类"gateway 从未配置/未部署"的场景下不会收到任何主动消息，只能通过管理群
告警（本职责在死信数量 > 0 时上报）触发人工核查。这与「部署契约耦合成文」
（``deploy/.env.example``）要求 worker 与 gateway 两侧开关必须同开同关是同一个
产品事实的两面：只开 worker 侧会产生无人消费、最终被本职责判死的文档请求，且用户
不会收到任何解释。

**职责在表不存在时不注册不崩溃**：本职责与 :class:`~lingxi.apps.scheduler.
retention.PermissionRetentionSweepDuty` 等既有职责同一姿态——装配前置只是
``LINGXI_POSTGRES_DSN``（``SchedulerConfig`` 的必填项，进程起得来就一定有），因此
**总能注册**，不需要一个专门的"表存不存在"探测。真正的防线在
:class:`~lingxi.apps.scheduler.loop.SchedulerLoop` 的既有姿态（``run_once`` 逐职责
隔离异常，见该类文档）：如果这次提交部署时迁移 0074 因为顺序问题还没跑到（本表尚不
存在），本职责这一轮的两次 ``UPDATE`` 会各自抛出异常、被下面的 :meth:`_sweep`
捕获并记一条审计，**不影响同一轮里的其它定时职责**，下一轮原样重试——迁移一旦追上，
职责自愈，不需要重启进程或人工介入。
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
    """最小读写口，实现是
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
        return {"dead_lettered": self.dead_lettered, "content_redacted": self.content_redacted}


class DocumentDeliveryMaintenanceDuty:
    """一轮：扫一次死信、擦一次到期正文，两者各自独立失败隔离（同 ``apps/gateway/
    document_delivery.py::DocumentDeliveryConsumer.run_once`` 对
    ``fail_exhausted_pending``/``reclaim_stale_processing`` 的姿态：一段查询失败
    只降级这一段，不带走另一段，也不让异常杀死整条定时职责循环——那道防线已经在
    :class:`~lingxi.apps.scheduler.loop.SchedulerLoop` 里，见模块文档）。
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
        self._store = store
        self._audit = audit
        self._alert = alert
        self._stop = threading.Event() if stop is None else stop

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
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
    """装配并按需追加进 ``duties``——住在本文件而不是 ``assembly.py``，同
    ``apps/scheduler/daily_report.py::_wire_daily_report_duty`` 的既有形状（体量
    棘轮上限逼出来的做法，见该函数文档字符串）：``assembly.py`` 的调用点只占
    一行。**总能注册**（没有可选前置，唯一依赖 ``LINGXI_POSTGRES_DSN`` 是
    ``SchedulerConfig`` 的必填项），因此与 ``_wire_daily_report_duty``（那条职责
    有可选前置、``duty`` 可能是 ``None``）不同，本函数不需要 ``if duty is not
    None`` 判断，直接构造并追加。

    复用与 gateway 侧同一个 ``delivery_alert_callback``（Issue #153 起
    ``AlertingDuty`` 的通用方法，不是 gateway 专属）：告警文案的归一化、
    ``document_delivery_*`` kind 前缀与去重命名空间三个进程共用同一份纪律，不
    重复实现。
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
