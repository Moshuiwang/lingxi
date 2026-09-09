"""专用阶段的短事务登记、领取与完成；外部调用不持有这里的连接。"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import fields

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, connect
from lingxi.core.admin.followup import (
    GATEWAY_STAGES,
    SCHEDULER_STAGES,
    TERMINAL_STATUSES,
    ClaimedFollowup,
    FollowupCapacityError,
    FollowupRef,
    FollowupSpec,
)
from lingxi.core.ids import new_id

_CLAIM_COLUMNS = ", ".join("f." + field.name for field in fields(ClaimedFollowup))
_ACCEPT_LOCK = 6030091


def enqueue_followups(connection, *, pending_action_id, trace_id, items):
    """在调用方事务内登记全部阶段，失败原样冒泡使确认整体回滚。"""
    items = tuple(items)
    if not all(isinstance(item, FollowupSpec) for item in items):
        raise TypeError("只能登记固定阶段")
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", (_ACCEPT_LOCK,))
        cursor.execute(
            "SELECT count(*) FROM admin_action_followup WHERE status "
            "IN ('pending','running','retry_wait')"
        )
        if cursor.fetchone()[0] + len(items) > 10000:
            raise FollowupCapacityError("系统繁忙，请稍后重试")
        return tuple(_enqueue_one(cursor, pending_action_id, trace_id, item) for item in items)


def _enqueue_one(cursor, action_id, trace_id, item):
    """唯一键冲突只读已有引用，不能重置终态。"""
    cursor.execute(
        "INSERT INTO admin_action_followup "
        "(id,pending_action_id,trace_id,subject_key,stage,target_user_id,target_version,"
        "batch_id,batch_item_id,depends_on_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (pending_action_id,subject_key,stage) DO NOTHING",
        (
            new_id("afu"),
            action_id,
            trace_id,
            item.subject_key,
            item.stage,
            item.target_user_id,
            item.target_version,
            item.batch_id,
            item.batch_item_id,
            item.depends_on_id,
        ),
    )
    cursor.execute(
        "SELECT id,stage,status,result_code,updated_at FROM admin_action_followup "
        "WHERE pending_action_id=%s AND subject_key=%s AND stage=%s",
        (action_id, item.subject_key, item.stage),
    )
    row = cursor.fetchone()
    return FollowupRef(**dict(zip(("id", "stage", "status", "result_code", "updated_at"), row)))


class PostgresFollowupStore:
    """连接活动预算由同进程共享信号量限制，不常驻占用连接。"""

    def __init__(self, dsn, *, timeouts=DEFAULT_POSTGRES_TIMEOUTS, db_slots=None):
        """构造不联网；listener 与阶段可注入同一个两槽预算。"""
        self._dsn = dsn
        self._timeouts = timeouts
        self.db_slots = db_slots if db_slots is not None else threading.BoundedSemaphore(2)

    @contextmanager
    def transaction(self):
        """只为短 SQL 借连接，禁止调用方在此执行外部请求。"""
        with self.db_slots, connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                yield connection

    def claim_followup(self, *, consumer_kind, owner, now, lease_seconds=120):
        """只领取 v1、依赖成功、到期的一项；旧代不能写回新代。"""
        stages = {
            "gateway": GATEWAY_STAGES,
            "scheduler": SCHEDULER_STAGES,
            "recompute": {"permission_recompute"},
            "observe": {"publish_observe"},
            "postprocess": GATEWAY_STAGES - {"permission_recompute", "publish_observe"},
        }
        if consumer_kind not in stages or not owner or not 0 < lease_seconds <= 120:
            raise ValueError("消费者或租期无效")
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "WITH candidate AS (SELECT f.id FROM admin_action_followup f "
                "WHERE f.contract_version=1 AND f.status IN ('pending','retry_wait') "
                "AND f.next_attempt_at<=%s AND f.stage=ANY(%s) "
                "AND (f.depends_on_id IS NULL OR EXISTS (SELECT 1 FROM admin_action_followup d "
                "WHERE d.id=f.depends_on_id AND d.status='succeeded')) "
                "ORDER BY f.next_attempt_at,f.id FOR UPDATE OF f SKIP LOCKED LIMIT 1) "
                "UPDATE admin_action_followup f SET status='running',attempt=attempt+1,"
                "lease_owner=%s,lease_until=%s + %s * interval '1 second',updated_at=%s "
                "FROM candidate c WHERE f.id=c.id RETURNING " + _CLAIM_COLUMNS,
                (now, list(stages[consumer_kind]), owner, now, lease_seconds, now),
            )
            row = cursor.fetchone()
        return (
            None
            if row is None
            else ClaimedFollowup(
                **dict(zip((field.name for field in fields(ClaimedFollowup)), row))
            )
        )

    def mark_effect_started(self, *, id, owner, attempt, now):
        """持久标记成功才允许外发，过期租约也拒绝。"""
        return self._owned_update(
            "effect_started_at=COALESCE(effect_started_at,%s),updated_at=%s",
            (now, now),
            id,
            owner,
            attempt,
            extra=" AND lease_until>%s",
            extra_values=(now,),
        )

    def renew_lease(self, *, id, owner, attempt, now):
        """续租失败后执行方不得开始下一次外部操作。"""
        return self._owned_update(
            "lease_until=%s + interval '120 seconds',updated_at=%s",
            (now, now),
            id,
            owner,
            attempt,
            extra=" AND lease_until>%s",
            extra_values=(now,),
        )

    def _owned_update(self, assignment, values, id, owner, attempt, *, extra="", extra_values=()):
        """每次写回都保留 running、owner 和 attempt 三项条件。"""
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE admin_action_followup SET "
                + assignment
                + " WHERE id=%s AND status='running' AND lease_owner=%s AND attempt=%s"
                + extra,
                (*values, id, owner, attempt, *extra_values),
            )
            return cursor.rowcount == 1

    def complete_followup(
        self, *, id, owner, attempt, status, result_code, external_ref=None, next_items=()
    ):
        """本代完成和后继登记同事务；失去所有权绝不产生后继。"""
        if status not in TERMINAL_STATUSES:
            raise ValueError("完成状态无效")
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE admin_action_followup SET status=%s,result_code=%s,external_ref=%s,"
                "finished_at=now(),updated_at=now(),lease_owner=NULL,lease_until=NULL "
                "WHERE id=%s AND status='running' AND lease_owner=%s AND attempt=%s "
                "RETURNING pending_action_id,trace_id",
                (status, result_code, external_ref, id, owner, attempt),
            )
            row = cursor.fetchone()
            if row is None:
                return False
            if next_items:
                enqueue_followups(
                    connection, pending_action_id=row[0], trace_id=row[1], items=next_items
                )
            return True

    def retry_followup(self, *, id, owner, attempt, now, result_code, stopped=False):
        """停止释放不计业务失败；未知外发永远不会进入重试。"""
        from lingxi.adapters.postgres_admin_followup_recovery import retry_followup

        return retry_followup(
            self,
            id=id,
            owner=owner,
            attempt=attempt,
            now=now,
            result_code=result_code,
            stopped=stopped,
        )

    def recover_expired(self, *, now, limit=32):
        """每轮最多处理 32 项，保持有限内存。"""
        from lingxi.adapters.postgres_admin_followup_recovery import recover_expired

        return recover_expired(self, now=now, limit=limit)

    def list_for_action(self, *, pending_action_id):
        """管理员只读回查原确认对应的阶段，不提供重发入口。"""
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id,stage,status,result_code,updated_at FROM admin_action_followup "
                "WHERE pending_action_id=%s ORDER BY created_at,id LIMIT 100",
                (pending_action_id,),
            )
            return tuple(
                FollowupRef(
                    **dict(zip(("id", "stage", "status", "result_code", "updated_at"), row))
                )
                for row in cursor.fetchall()
            )

    def current_publish_reference(self, *, target_user_id):
        """仅关联当前版本的既有发布记录。"""
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT p.id,p.permission_version FROM publish_outbox p "
                "JOIN app_user u ON u.id=p.user_id AND "
                "u.permission_version=p.permission_version WHERE p.user_id=%s "
                "ORDER BY p.created_at DESC LIMIT 1",
                (target_user_id,),
            )
            return cursor.fetchone()

    def dependency_publish_state(self, item):
        """观察父阶段冻结的引用，目标版本变化时只返回已被取代。"""
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT p.status,u.permission_version FROM admin_action_followup f "
                "JOIN publish_outbox p ON p.id=f.external_ref "
                "JOIN app_user u ON u.id=p.user_id WHERE f.id=%s",
                (item.depends_on_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return row[0] if str(row[1]) == str(item.target_version) else "superseded"

    def recovery_status(self):
        """部署前只读兼容性清点，未知格式不能当作已恢复。"""
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT contract_version,status,count(*) FROM admin_action_followup "
                "GROUP BY contract_version,status"
            )
            rows = cursor.fetchall()
        return {
            "contract_versions": sorted({row[0] for row in rows}),
            "compatible": all(row[0] == 1 for row in rows),
            "inflight": sum(n for _, status, n in rows if status == "running"),
            "recoverable": sum(n for _, status, n in rows if status in {"pending", "retry_wait"}),
            "unknown": sum(n for _, status, n in rows if status == "unknown"),
        }

    def resolve_target(self, *, id, owner, attempt, target_user_id, batch_identity_lookup):
        """核验原批次项身份后补齐真实用户；批次查询由固定业务适配器注入。"""
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT batch_item_id,target_user_id FROM admin_action_followup "
                "WHERE id=%s AND status='running' AND lease_owner=%s AND attempt=%s "
                "FOR UPDATE",
                (id, owner, attempt),
            )
            row = cursor.fetchone()
            if row is None or row[0] is None:
                return False
            original_open_id = batch_identity_lookup(connection, row[0])
            if not original_open_id or (row[1] is not None and row[1] != target_user_id):
                return False
            cursor.execute(
                "SELECT feishu_open_id FROM app_user WHERE id=%s FOR SHARE", (target_user_id,)
            )
            user = cursor.fetchone()
            if user is None or user[0] != original_open_id:
                return False
            cursor.execute(
                "UPDATE admin_action_followup SET target_user_id=%s,"
                "result_code='resolved_target',updated_at=now() WHERE id=%s",
                (target_user_id, id),
            )
            return True
