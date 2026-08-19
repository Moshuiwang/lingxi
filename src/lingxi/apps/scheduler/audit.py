"""scheduler 共用的审计出口：:class:`AuditSink` 端口与结构化日志实现。

从 :mod:`lingxi.apps.scheduler`（#237 拆分）搬出。``AuditSink`` 与
:class:`lingxi.core.alerting.AuditSink` 是同一个结构化签名，在这里单独写一份而不是
复用那边的类型，是刻意的解耦——三个进程各自的 ``apps/<name>`` 不应该因为审计端口
耦合在一起（合并后可收敛成一份，不需要现在跨切片耦合）。
"""

from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class AuditSink(Protocol):
    """审计出口。

    ``audit_event`` 表属 S9，尚未建立；当前实现写结构化日志。职责只依赖这个签名，
    届时换实现不动职责代码。签名与 #57 网关侧的同名 Protocol 一致（结构化类型，
    两边互相满足），合并后可收敛成一份，不需要现在跨切片耦合。
    """

    def record(self, action: str, /, **fields: object) -> None: ...


class StructuredLogAuditSink:
    """把审计动作写成一行结构化日志。

    字段按**键名排序**输出：审计行会被比对和 grep，顺序随 ``PYTHONHASHSEED`` 变化的
    日志没法稳定断言。本类原样输出收到的字段——「不带资料值」这条约束属于调用方，
    对应断言 ``V-花名册-33`` 因此断在调用方产生的那几行上。
    """

    def record(self, action: str, /, **fields: object) -> None:
        rendered = " ".join(f"{key}={value}" for key, value in sorted(fields.items()))
        logger.info("审计 action=%s %s", action, rendered)
