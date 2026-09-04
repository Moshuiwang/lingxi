"""scheduler 共用的审计出口：:class:`AuditSink` 端口与结构化日志实现。

``AuditSink`` 与 :class:`lingxi.core.alerting.AuditSink` 是同一个结构化签名，这里
单独写一份是刻意的解耦：三个进程各自的 ``apps/<name>`` 不应该因为审计端口耦合
在一起，合并后可收敛成一份，不需要现在跨切片耦合。
"""

from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class AuditSink(Protocol):
    """审计出口。

    ``audit_event`` 表尚未建立；当前实现写结构化日志。职责只依赖这个签名，届时换
    实现不动职责代码。签名与网关侧的同名 Protocol 一致（结构化类型，两边互相
    满足），合并后可收敛成一份，不需要现在跨切片耦合。
    """

    def record(self, action: str, /, **fields: object) -> None: ...


class StructuredLogAuditSink:
    """把审计动作写成一行结构化日志。

    字段按键名排序输出，保证同一事件的日志可稳定比对与 grep；调用方负责不传入
    资料值本身，本类原样输出收到的字段。动作名以 ``failed``/``error``/
    ``unparsable`` 结尾，或 ``onboarding.result`` 带非空 ``failure_reason``
    （它是每次开通收口都会记的通用终态动作，成功/失败共用同一个动作名，
    后缀规则漏检不到它），或命中 :attr:`_EXTRA_WARNING_ACTIONS` 显式名单时，
    一律升级为 ``WARNING``。后者收纳"运维需要立刻知道，但动作名本身不带失败
    后缀"的降级状态，只按需登记，不是所有"未接线"类动作的通用兜底。
    """

    _EXTRA_WARNING_ACTIONS = frozenset({"stalled_provisioning.notifier_not_wired"})

    def record(self, action: str, /, **fields: object) -> None:
        promote = (
            action.endswith(("failed", "error", "unparsable"))
            or (action == "onboarding.result" and bool(fields.get("failure_reason")))
            or action in self._EXTRA_WARNING_ACTIONS
        )
        rendered = " ".join(f"{key}={value}" for key, value in sorted(fields.items()))
        log = logger.warning if promote else logger.info
        log("审计 action=%s %s", action, rendered)
