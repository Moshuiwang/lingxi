"""``lingxi-scheduler``：定时职责进程。

跑十二个职责，由 :class:`SchedulerLoop` 按同一个周期依次驱动；装配顺序与
每个职责的前置条件见 `apps/scheduler/assembly.py::build_loop`，各职责自己
的产品语义、已知残留与设计取舍见其所在模块的文档字符串。定时职责单独分进
本进程：与请求路径混在一起会让重启语义不清。**职责之间互不牵连**（断言
V-保留-15）：`SchedulerLoop.run_once` 逐个职责捕获异常，一个职责连续失败
不影响其余职责这一轮照常跑。

退出语义（断言 V-部署-03、V-保留-17）：收到 SIGTERM/SIGINT 后停止领取新
工作，做完已经领取的那一次再退出。首次开通编排的执行器跑在独立线程池上，
不受 `SchedulerLoop` 的调用线程约束，因此 `main()` 用 try/finally 包住
`run_forever()`，无条件调用一次 `join_onboarding_executors` 把同一条退出
语义接到这条独立线程池上；收尾失败只记日志，不覆盖原始异常。
"""

from __future__ import annotations

import logging
import sys
import traceback

from lingxi.apps.scheduler.alerting_assembly import _combined_heartbeat, build_alerting_duty
from lingxi.apps.scheduler.assembly import (
    _build_late_readiness_recovery_duty,
    _build_onboarding_duty,
    _build_org_snapshot_sync_duty,
    _build_permission_publish_duty,
    _build_permission_refresh_duty,
    _build_permission_retention_duty,
    _build_readiness_follow_up,
    _build_roster_audit_duty,
    _build_roster_snapshot_sync_duty,
    _build_stalled_provisioning_duty,
    build_loop,
)
from lingxi.apps.scheduler.audit import AuditSink, StructuredLogAuditSink
from lingxi.apps.scheduler.config import (
    DEFAULT_FEISHU_BASE_URL,
    DEFAULT_INTERVAL_SECONDS,
    SchedulerConfig,
    _Secret,
)
from lingxi.apps.scheduler.credential_rotation import (
    SAVE_RETRY_BACKOFF_SECONDS,
    CredentialRotationLoop,
    RotationReport,
    _is_definite_failure,
)
from lingxi.apps.scheduler.daily_report import DailyReportDuty, _build_daily_report_duty
from lingxi.apps.scheduler.late_readiness_recovery import (
    DEFAULT_NOTICE_DRAIN_LIMIT,
    DEFAULT_RECOVERY_INTERVAL_SECONDS,
    DEFAULT_RECOVERY_LIMIT,
    LateReadinessRecoveryDuty,
    LateReadinessRecoveryReport,
)
from lingxi.apps.scheduler.loop import SchedulerLoop, install_signal_handlers
from lingxi.apps.scheduler.onboarding import join_onboarding_executors
from lingxi.apps.scheduler.org_snapshot_sync import OrgSnapshotSyncDuty
from lingxi.apps.scheduler.permission_publish import (
    PermissionPublishDuty,
    PermissionPublishReport,
    ReadinessFollowUp,
)
from lingxi.apps.scheduler.permission_refresh import (
    PERMISSION_REFRESH_REASON,
    PERMISSION_REVOKE_REASON,
    PermissionRefreshDuty,
    PermissionRefreshReport,
)
from lingxi.apps.scheduler.retention import (
    IDLE_CONVERSATION_SWEEP_AFTER,
    ContentCaptureRetentionDuty,
    IdleConversationSweepDuty,
    PermissionRetentionReport,
    PermissionRetentionSweepDuty,
    RetentionCleanupDuty,
)
from lingxi.apps.scheduler.roster_audit import RosterAuditDuty, RosterSnapshotSyncDuty
from lingxi.apps.scheduler.stalled_provisioning import (
    DEFAULT_STALLED_LEASE_SECONDS,
    DEFAULT_STALLED_LIMIT,
    StalledProvisioningDuty,
    StalledProvisioningReport,
)
from lingxi.core.alerting import (
    AlertDispatcher,
    AlertingDuty,
    AlertManager,
    AlertPolicy,
)
from lingxi.core.alerting import (
    AlertSender as _AlertSender,
)

# 以下名字本文件不直接使用，只是 re-export：调用方历史上一直从
# ``lingxi.apps.scheduler`` 这个包顶层导入它们，而不是各自的实现模块。
__all__ = [
    "AlertDispatcher",
    "AlertManager",
    "AlertPolicy",
    "AlertingDuty",
    "AuditSink",
    "ContentCaptureRetentionDuty",
    "CredentialRotationLoop",
    "DEFAULT_FEISHU_BASE_URL",
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_NOTICE_DRAIN_LIMIT",
    "DEFAULT_RECOVERY_INTERVAL_SECONDS",
    "DEFAULT_RECOVERY_LIMIT",
    "DEFAULT_STALLED_LEASE_SECONDS",
    "DEFAULT_STALLED_LIMIT",
    "DailyReportDuty",
    "IDLE_CONVERSATION_SWEEP_AFTER",
    "IdleConversationSweepDuty",
    "LateReadinessRecoveryDuty",
    "LateReadinessRecoveryReport",
    "OrgSnapshotSyncDuty",
    "PERMISSION_REFRESH_REASON",
    "PERMISSION_REVOKE_REASON",
    "PermissionPublishDuty",
    "PermissionPublishReport",
    "PermissionRefreshDuty",
    "PermissionRefreshReport",
    "PermissionRetentionReport",
    "PermissionRetentionSweepDuty",
    "ReadinessFollowUp",
    "RetentionCleanupDuty",
    "RosterAuditDuty",
    "RosterSnapshotSyncDuty",
    "RotationReport",
    "SAVE_RETRY_BACKOFF_SECONDS",
    "SchedulerLoop",
    "StalledProvisioningDuty",
    "StalledProvisioningReport",
    "_AlertSender",
    "_Secret",
    "_build_daily_report_duty",
    "_build_late_readiness_recovery_duty",
    "_build_onboarding_duty",
    "_build_org_snapshot_sync_duty",
    "_build_permission_publish_duty",
    "_build_permission_refresh_duty",
    "_build_permission_retention_duty",
    "_build_readiness_follow_up",
    "_build_roster_audit_duty",
    "_build_roster_snapshot_sync_duty",
    "_build_stalled_provisioning_duty",
    "_is_definite_failure",
]

logger = logging.getLogger(__name__)


# _AlertSender/_PendingAlert/AlertDispatcher/AlertingDuty/_alert_utc 已迁移到
# lingxi.core.alerting（见本文件顶部的导入）：gateway 与 worker 也需要装配
# 同一套告警编排，三个 apps/<name> 互不 import，因此这段编排放进 core/（只
# 编排注入接口，不直接做 I/O）。本模块沿用既有的名字，既有测试不必改动。


def main(argv: list[str] | None = None) -> int:
    # 日志只到 stdout / stderr，不写文件、不自行轮转（断言 V-部署-04）。
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        config = SchedulerConfig.from_env()
    except ValueError as error:
        print(f"lingxi-scheduler 启动失败：{error}", file=sys.stderr)
        return 2
    try:
        alerting_duty = build_alerting_duty(config, audit=StructuredLogAuditSink())
        loop = build_loop(
            config,
            alerting_duty=alerting_duty,
            heartbeat=_combined_heartbeat(alerting_duty, "scheduler"),
        )
    except (RuntimeError, ValueError) as error:
        print(f"lingxi-scheduler 启动失败：{error}", file=sys.stderr)
        return 2

    install_signal_handlers(loop)
    logger.info("lingxi-scheduler 已启动 interval_seconds=%s", config.interval_seconds)
    try:
        loop.run_forever()
    finally:
        # `run_forever()` 只保证不再有新一轮 tick，开通执行器自己的独立线程池
        # 需要单独接线停止领取新工作、并在预算内等在途链收尾（见模块文档「退出
        # 语义」一节）——不放进 finally 会让主循环抛出未处理异常时这一行被绕过，
        # daemon 线程只能靠解释器退出时被任意截断。收尾自身的异常不得覆盖原始
        # 故障——记一条日志后放行，让 `run_forever()` 的原始异常继续原样传播。
        try:
            join_onboarding_executors(loop.duties)
        except Exception as error:  # 只记类型名与调用栈帧，异常正文不进日志
            logger.error(
                "lingxi-scheduler 收尾 join_onboarding_executors 失败 error=%s\n调用栈（不含异常正文）：\n%s",
                type(error).__name__,
                "".join(traceback.format_tb(error.__traceback__)),
            )
    return 0
