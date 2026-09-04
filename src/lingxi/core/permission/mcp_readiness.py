"""发布之后的「当前用户 MCP 是否就绪」确认：五路互斥分流（纯编排）——公开入口。

发布怎么做、一行长什么样，见 :mod:`lingxi.core.permission.publish` 与
:mod:`lingxi.core.permission.publish_row`；本模块只回答一句话：**权限发布读回一致之后，
凭什么说这个人的问数 MCP 已经就绪，以及等多久算等不到了**。

判定依据是**探针**：用该用户自己的明文令牌真实执行一次 ``list_metrics``，可见指标条数
大于零才算就绪。五路结果与共享的判定引擎（``_ReadinessProbeRunner``）、阻塞式确认
（:class:`McpReadinessConfirmation`）住在 :mod:`lingxi.core.permission.mcp_readiness_base`；
tick 驱动的两种形态住在 :mod:`lingxi.core.permission.mcp_readiness_tick`。本模块把两边
的公开符号原样重新导出，是本子系统唯一的对外导入路径，因此拆分对调用方零影响。
"""

from __future__ import annotations

from lingxi.core.permission.mcp_readiness_base import (
    CONTRACT_SCHEDULE,
    DEFAULT_BUDGET_SECONDS,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_PROBE_TIMEOUT_SECONDS,
    MAX_ATTEMPTS,
    MAX_BUDGET_SECONDS,
    MAX_ERROR_CODE_LENGTH,
    MAX_INTERVAL_SECONDS,
    MIN_BUDGET_SECONDS,
    MIN_INTERVAL_SECONDS,
    MIN_PROBE_TIMEOUT_SECONDS,
    TERMINAL_OUTCOMES,
    McpProbe,
    McpProbeError,
    McpReadinessConfirmation,
    ReadinessAttempt,
    ReadinessBinding,
    ReadinessCheckStore,
    ReadinessOutcome,
    ReadinessSchedule,
    ReadinessSession,
    classify_probe,
    evaluate_permission_presence,
)
from lingxi.core.permission.mcp_readiness_tick import (
    ReadinessProgress,
    ReadinessRecoveryTicker,
    ReadinessTicker,
    next_probe_due,
)

__all__ = [
    "CONTRACT_SCHEDULE",
    "DEFAULT_BUDGET_SECONDS",
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_PROBE_TIMEOUT_SECONDS",
    "MAX_ATTEMPTS",
    "MIN_PROBE_TIMEOUT_SECONDS",
    "MAX_BUDGET_SECONDS",
    "MAX_ERROR_CODE_LENGTH",
    "MAX_INTERVAL_SECONDS",
    "MIN_BUDGET_SECONDS",
    "MIN_INTERVAL_SECONDS",
    "McpProbe",
    "McpProbeError",
    "McpReadinessConfirmation",
    "ReadinessAttempt",
    "ReadinessBinding",
    "ReadinessCheckStore",
    "ReadinessOutcome",
    "ReadinessProgress",
    "ReadinessRecoveryTicker",
    "ReadinessSchedule",
    "ReadinessSession",
    "ReadinessTicker",
    "TERMINAL_OUTCOMES",
    "classify_probe",
    "evaluate_permission_presence",
    "next_probe_due",
]
