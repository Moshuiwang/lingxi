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

    **失败类动作与失败终态升级到 ``WARNING``**（Issue #282 §0.3 核实结论修复）：
    ``core/identity/onboarding_runner.py`` 与 ``core/conversation/
    onboarding_recovery.py`` 的文档字符串在多处写下「动作名带 ``failed`` 后缀，由审计
    实现升到 ``WARNING``」，但本类此前**无条件** ``logger.info``——那条承诺从未真正
    兑现过：scheduler 里全部"响亮审计"其实一条都不响亮，全部淹在 INFO 流水里。形状照
    ``apps/gateway/__init__.py`` 同名实现的既有规则（该实现已经把这条规则落地过一次，
    这里只是把同一条规则搬进 scheduler，不新造判据）：动作名以
    ``failed``/``error``/``unparsable`` 结尾即升级。额外加两条 scheduler 专属的判据：

    - ``onboarding.result`` 这一行本身**没有**失败后缀（它是每次开通收口时都会记的
      通用终态记账动作，成功与失败共用同一个动作名），因此单独判它的
      ``failure_reason`` 字段：非空说明这次开通没有成功，同样应该响亮，不能靠后缀
      规则漏检。
    - ``_EXTRA_WARNING_ACTIONS`` 显式名单（同 ``apps/gateway/__init__.py`` 的
      ``_EXTRA_WARNING_ACTIONS`` 同一个扩展位，外部独立审查 P3-4：此前搬这条规则
      过来的时候把这个位置漏掉了）：动作名本身不带失败后缀、但描述的是一种需要
      运维注意的降级状态。当前只有一条登记——
      ``stalled_provisioning.notifier_not_wired``（Issue #282，`V-开通-19`）：
      通知出口没有装配、本轮所有停摆候选一条都不处理，这是"本该响亮"却没有
      ``failed``/``error``/``unparsable`` 后缀的典型例子，需要显式列进来才会被
      升级。名单只收**运维需要立刻知道**的降级状态，不是所有"未接线"类动作的
      通用兜底——其余同类动作（如 ``onboarding.duty_not_registered``）是否需要
      同样处理留给各自的 Story 按需登记，本次不批量搬。
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
