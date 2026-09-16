"""scheduler 两个固定业务阶段，复用系统开通及逐用户只读探针。

每个阶段的终态结果在运营审计账上逐人留一行「已执行」；等待重试不是终态、不记。
账写不进去只记一条结构化日志，阶段结果本身不因此改变：这里的执行已经发生，把它
报成失败会让消费者重跑一次已经做过的事。
"""

import logging
from datetime import UTC, datetime

from lingxi.adapters.postgres_innertest import (
    OPERATION_INNERTEST_ADDITIONS,
    PURPOSE_INNERTEST_ADDITIONS,
    admin_roles_snapshot,
)
from lingxi.adapters.postgres_operation_audit import record_operation_audit
from lingxi.core.admin.followup_consumer import FollowupResult
from lingxi.core.admin.innertest import InnertestError
from lingxi.core.admin.operation_audit import (
    EntryPoint,
    OperationAuditEntry,
    OperationPhase,
    executor_label,
)
from lingxi.core.identity.preprovision import PreprovisionSkip, PreprovisionTarget
from lingxi.core.ids import new_id

logger = logging.getLogger(__name__)


class InnertestFollowupHandlers:
    """资格与就绪不是同一个事实，不自动发欢迎卡。"""

    def __init__(self, *, store, runner, probe, identity_service=None):
        """Runner 是已装配的 start_system，禁止注入本地补授计划。"""
        self.store, self.runner, self.probe = store, runner, probe
        self.identity_service = identity_service

    def handle(self, item):
        """读取短事务后释放连接，网络期间不持有数据库活动槽。"""
        target = self._target(item)
        if target is None:
            return self._record(item, FollowupResult("failed", "identity_missing"))
        if item.stage == "innertest_preprovision":
            return self._record(item, self._preprovision(item, target))
        return self._check(item, target)

    def _preprovision(self, item, target):
        """未开通的人走系统触发开通；容量与在途两种等待交回消费者重试。"""
        if target[1] is None:
            if target[7] != "identity_resolution_pending":
                return FollowupResult("failed", target[7])
            pending = self._resolve_identity(item, target)
            if pending is not None:
                return pending
            target = self._target(item)
            if target is None or target[1] is None:
                return FollowupResult("failed", "identity_unavailable")
        if target[3] != "active":
            result = self.runner.start_system(
                email=target[0],
                trace_id=item.trace_id,
                initiated_by_open_id=target[2],
                expected_open_id=target[1],
            )
            if getattr(result, "failure_reason", None) in {"capacity_pending", "stopping"}:
                self.store.retry_followup(
                    id=item.id,
                    owner=item.lease_owner,
                    attempt=item.attempt,
                    now=datetime.now(UTC),
                    result_code="capacity_pending",
                    stopped=True,
                )
                return FollowupResult("retry_wait", "capacity_pending")
            if getattr(result, "failure_reason", None) == "already_running":
                return FollowupResult("retry_wait", "provisioning_pending")
            target = self._target(item)
            if target is None or target[3] != "active":
                return FollowupResult(
                    "failed", getattr(result, "failure_reason", None) or "provisioning_failed"
                )
        return FollowupResult()

    def _resolve_identity(self, item, target):
        """先释放数据库连接再读实时状态，选定结果只允许持久保存一次。"""
        from lingxi.adapters.innertest_identity import persist_resolved_identity

        located = self.runner.resolve_system_email(email=target[0], trace_id=item.trace_id)
        if isinstance(located, PreprovisionSkip):
            return self._refuse_identity(item, located.reason)
        if not isinstance(located, PreprovisionTarget):
            return FollowupResult(
                "retry_wait", getattr(located, "failure_reason", "identity_unavailable")
            )
        try:
            persist_resolved_identity(
                store=self.store, service=self.identity_service, item=item, target=located
            )
        except InnertestError as error:
            return self._refuse_identity(item, error.code)
        return None

    def _refuse_identity(self, item, reason):
        """仅当前领取者可保存拒绝；失去租约时交回消费者，不冒充完成。"""
        from lingxi.adapters.innertest_identity import persist_identity_refusal

        if reason == "lease_lost":
            return FollowupResult("retry_wait", reason)
        persist_identity_refusal(store=self.store, item=item, reason=reason)
        return FollowupResult("failed", reason)

    def _record(self, item, result, *, evidence_ref=None):
        """终态结果各留一行运营审计；写不进去只记日志，阶段结果原样返回。"""
        if result.status == "retry_wait":
            return result
        try:
            with self.store.transaction() as connection:
                record_operation_audit(
                    connection, self._audit_entry(connection, item, result, evidence_ref)
                )
        except Exception as error:
            logger.warning(
                "operation_audit.write_failed stage=%s followup_id=%s error=%s",
                item.stage,
                item.id,
                type(error).__name__,
            )
        return result

    @staticmethod
    def _audit_entry(connection, item, result, evidence_ref):
        """发起人与用户标识在写账的事务里现读，不用领取时的快照填补。"""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT b.initiated_by,u.id FROM innertest_batch_item i "
                "JOIN innertest_batch b ON b.id=i.batch_id "
                "LEFT JOIN app_user u ON u.feishu_open_id=i.open_id WHERE i.id=%s",
                (item.batch_item_id,),
            )
            initiated_by, user_id = cursor.fetchone()
        return OperationAuditEntry(
            operation_id=item.batch_id,
            operation=OPERATION_INNERTEST_ADDITIONS,
            phase=OperationPhase.EXECUTED,
            initiated_by=initiated_by,
            actor_roles=admin_roles_snapshot(connection, initiated_by),
            entry_point=EntryPoint.SCHEDULER_FOLLOWUP,
            executor=executor_label("scheduler", run_id=item.lease_owner),
            purpose=PURPOSE_INNERTEST_ADDITIONS,
            target_kind="user",
            target_user_id=user_id,
            result_code=f"{result.status}:{result.result_code}",
            evidence_ref=evidence_ref or "admin_action_followup:" + item.id,
            pending_action_id=item.pending_action_id,
            trace_id=item.trace_id,
        )

    def _target(self, item):
        """批次原身份和真实用户重新关联，不用客户端 open_id 填补用户。"""
        with self.store.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT i.email,i.open_id,b.initiated_by,u.provisioning_state,u.id,"
                "u.account_state,u.permission_version,i.result_code FROM innertest_batch_item i "
                "JOIN innertest_batch b ON b.id=i.batch_id LEFT JOIN app_user u "
                "ON u.feishu_open_id=i.open_id WHERE i.id=%s AND b.id=%s AND b.status='executed'",
                (item.batch_item_id, item.batch_id),
            )
            row = cursor.fetchone()
        if row is not None and row[4] is not None and item.target_user_id is None:
            if not self.store.resolve_target(
                id=item.id,
                owner=item.lease_owner,
                attempt=item.attempt,
                target_user_id=row[4],
                batch_identity_lookup=batch_identity_lookup,
            ):
                return None
        return row

    def _check(self, item, target):
        """检查前后同一权限版本且账号正常才记成功；不保存 MCP 正文。"""
        if target[3] != "active" or target[5] != "enabled":
            return self._record(item, FollowupResult("failed", "check_failed"))
        before = self._published(target[4], target[6])
        if before is None:
            return FollowupResult("retry_wait", "publish_pending")
        started, count, code = datetime.now(UTC), None, "check_passed"
        try:
            count = self.probe.list_metrics(user_id=target[4])
            if count < 1:
                code = "check_failed"
        except Exception as error:
            code = "check_failed" if getattr(error, "denied", False) else "check_unknown"
        after = self._target(item)
        if after != target or self._published(target[4], target[6]) != before:
            code = "check_version_changed"
        check_id = new_id("ich")
        with self.store.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO innertest_check(id,batch_item_id,user_id,permission_version,"
                "publish_version,started_at,finished_at,result_code,metric_count,trace_id) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    check_id,
                    item.batch_item_id,
                    target[4],
                    target[6],
                    target[6],
                    started,
                    datetime.now(UTC),
                    code,
                    count,
                    item.trace_id,
                ),
            )
        return self._record(
            item,
            FollowupResult("succeeded" if code == "check_passed" else "failed", code),
            evidence_ref="innertest_check:" + check_id,
        )

    def _published(self, user_id, version):
        """必须是当前权限版本的已发布引用，旧发布不能为新检查背书。"""
        with self.store.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id,permission_version FROM publish_outbox WHERE user_id=%s "
                "AND permission_version=%s AND status='published' ORDER BY created_at DESC LIMIT 1",
                (user_id, version),
            )
            return cursor.fetchone()


def batch_identity_lookup(connection, batch_item_id):
    """提供给唯一阶段表的身份核验端口，只查固定批次项。"""
    with connection.cursor() as cursor:
        cursor.execute("SELECT open_id FROM innertest_batch_item WHERE id=%s", (batch_item_id,))
        row = cursor.fetchone()
        return row[0] if row else None
