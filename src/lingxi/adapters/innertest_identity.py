"""把执行阶段选定的身份、内测资格和名单版本原子保存，重启不重新换人。"""

from datetime import UTC, datetime, timedelta

from lingxi.adapters.postgres_innertest import InnertestPrincipal
from lingxi.core.admin.innertest import InnertestError
from lingxi.core.identity.preprovision import PreprovisionTarget


def persist_resolved_identity(*, store, service, item, target):
    """实时读取已在事务外结束；本事务仅核验来源、授权与领取代数后写资格。"""
    if service is None or not isinstance(target, PreprovisionTarget) or target.identity is None:
        raise InnertestError("identity_unavailable")
    with store.transaction() as connection, connection.cursor() as cursor:
        service._version(cursor, lock=True)
        _check_lease(cursor, item)
        cursor.execute(
            "SELECT i.email,i.open_id,i.personnel_id,i.result_code,b.initiated_by,"
            "b.binding_id,b.binding_version FROM innertest_batch_item i "
            "JOIN innertest_batch b ON b.id=i.batch_id JOIN pending_action p "
            "ON p.id=b.pending_action_id WHERE i.id=%s AND b.id=%s AND b.scope=%s "
            "AND b.status='executed' AND p.status='executed' FOR UPDATE OF i",
            (item.batch_item_id, item.batch_id, service.scope),
        )
        row = cursor.fetchone()
        if row is None or row[0] != target.email:
            raise InnertestError("identity_binding_mismatch")
        service._principal(connection, InnertestPrincipal(row[5], row[4], row[6]))
        if row[3] != "identity_resolution_pending":
            if (row[1], row[2]) != (target.open_id, target.personnel_id):
                raise InnertestError("identity_binding_mismatch")
            return
        _check_snapshot(cursor, target.identity)
        cursor.execute(
            "INSERT INTO innertest_membership(scope,open_id,email,batch_id) "
            "VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING open_id",
            (service.scope, target.open_id, row[0], item.batch_id),
        )
        added = cursor.fetchone() is not None
        if added:
            cursor.execute(
                "UPDATE innertest_roster_version SET version=version+1,updated_at=now() WHERE scope=%s",
                (service.scope,),
            )
        cursor.execute(
            "UPDATE innertest_batch_item SET open_id=%s,personnel_id=%s,result_code=%s WHERE id=%s",
            (
                target.open_id,
                target.personnel_id,
                "added" if added else "already_member",
                item.batch_item_id,
            ),
        )
        service.audit.record(
            "innertest.identity_resolved",
            batch_id=item.batch_id,
            batch_item_id=item.batch_item_id,
            trace_id=item.trace_id,
            **target.identity.audit_facts(),
        )


def _check_lease(cursor, item):
    """失去领取权的旧执行者不能再写入资格。"""
    cursor.execute(
        "SELECT id FROM admin_action_followup WHERE id=%s AND lease_owner=%s "
        "AND attempt=%s AND status='running' AND lease_until>now() FOR UPDATE",
        (item.id, item.lease_owner, item.attempt),
    )
    if cursor.fetchone() is None:
        raise InnertestError("lease_lost")


def persist_identity_refusal(*, store, item, reason):
    """拒绝也保留终态；中断后再领取沿用结论，不让失败显示成一直等待解析。"""
    with store.transaction() as connection, connection.cursor() as cursor:
        _check_lease(cursor, item)
        cursor.execute(
            "UPDATE innertest_batch_item SET result_code=%s WHERE id=%s AND batch_id=%s "
            "AND open_id IS NULL AND result_code='identity_resolution_pending'",
            (reason, item.batch_item_id, item.batch_id),
        )


def _check_snapshot(cursor, identity):
    """实时选择期间快照替换或过期时交回重做，不把旧候选写成当前身份。"""
    cursor.execute("SELECT id,captured_at FROM roster_snapshot FOR SHARE")
    row = cursor.fetchone()
    if (
        row is None
        or row != (identity.snapshot_version, identity.snapshot_captured_at)
        or row[1] <= datetime.now(UTC) - timedelta(days=90)
    ):
        raise InnertestError("identity_snapshot_changed")
