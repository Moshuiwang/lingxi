"""租约过期及有限退避，无法证明安全重试的外发明确保留未知。"""

from __future__ import annotations

from lingxi.core.admin.followup import EXTERNAL_STAGES, RecoveryCounts


def retry_followup(store, *, id, owner, attempt, now, result_code, stopped=False):
    """内部失败有限重试，停止交接不计入失败次数。"""
    with store.transaction() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT stage,effect_started_at,failure_count,created_at FROM admin_action_followup "
            "WHERE id=%s AND status='running' AND lease_owner=%s AND attempt=%s FOR UPDATE",
            (id, owner, attempt),
        )
        row = cursor.fetchone()
        if row is None:
            return False
        stage, effect, failures, created = row
        failures += int(not stopped)
        status = _recovery_status(stage, effect, failures, created, now)
        delay = 0 if stopped else (5, 15, 30, 60, 120, 300)[min(max(failures - 1, 0), 5)]
        cursor.execute(
            "UPDATE admin_action_followup SET status=%s,result_code=%s,failure_count=%s,"
            "next_attempt_at=%s + %s * interval '1 second',lease_owner=NULL,lease_until=NULL,"
            "finished_at=CASE WHEN %s='retry_wait' THEN NULL ELSE %s END,updated_at=%s WHERE id=%s",
            (status, result_code, failures, now, delay, status, now, now, id),
        )
        return True


def _recovery_status(stage, effect, failures, created, now):
    """未知优先于耗尽，不能把潜在送达误报为失败。"""
    if stage in EXTERNAL_STAGES and effect is not None:
        return "unknown"
    if failures >= 8 or (now - created).total_seconds() >= 86400:
        return "failed"
    return "retry_wait"


def recover_expired(store, *, now, limit=32):
    """两进程安全分担过期记录，未识别版本保持原样等待兼容消费者。"""
    counts = {"recoverable": 0, "unknown": 0, "failed": 0}
    with store.transaction() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT id,stage,effect_started_at,failure_count,created_at "
            "FROM admin_action_followup WHERE contract_version=1 AND status='running' "
            "AND lease_until<=%s ORDER BY lease_until,id FOR UPDATE SKIP LOCKED LIMIT %s",
            (now, min(32, max(0, limit))),
        )
        for id, stage, effect, failures, created in cursor.fetchall():
            status = _recovery_status(stage, effect, failures, created, now)
            counts["recoverable" if status == "retry_wait" else status] += 1
            cursor.execute(
                "UPDATE admin_action_followup SET status=%s,result_code=%s,lease_owner=NULL,"
                "lease_until=NULL,next_attempt_at=%s,updated_at=%s,"
                "finished_at=CASE WHEN %s='retry_wait' THEN NULL ELSE %s END WHERE id=%s",
                (
                    status,
                    "effect_result_unknown" if status == "unknown" else "lease_expired",
                    now,
                    now,
                    status,
                    now,
                    id,
                ),
            )
        _settle_management_cards(cursor, limit)
        _close_dependencies(cursor, now, limit)
        _expire_old(cursor, now, limit)
    return RecoveryCounts(**counts)


def _close_dependencies(cursor, now, limit):
    """依赖失败或未知的后继明确关闭，不伪造成功。"""
    cursor.execute(
        "WITH blocked AS (SELECT f.id FROM admin_action_followup f "
        "JOIN admin_action_followup d ON d.id=f.depends_on_id "
        "WHERE f.contract_version=1 AND f.status IN ('pending','retry_wait') "
        "AND d.status IN ('failed','unknown','skipped') "
        "ORDER BY f.id FOR UPDATE OF f SKIP LOCKED LIMIT %s) "
        "UPDATE admin_action_followup f SET status='skipped',result_code='dependency_not_succeeded',"
        "finished_at=%s,updated_at=%s FROM blocked b WHERE f.id=b.id",
        (min(32, max(0, limit)), now, now),
    )


def _expire_old(cursor, now, limit):
    """到期先结束依赖工作并删除可识别阶段，不无限保留历史。"""
    cursor.execute(
        "SELECT f.id FROM pending_action p JOIN admin_action_followup f ON f.pending_action_id=p.id "
        "WHERE p.retention_expires_at<=%s "
        "ORDER BY p.retention_expires_at,f.id FOR UPDATE OF f SKIP LOCKED LIMIT %s",
        (now, min(32, max(0, limit))),
    )
    ids = [row[0] for row in cursor.fetchall()]
    if not ids:
        return
    cursor.execute(
        "UPDATE admin_action_followup SET status=CASE WHEN effect_started_at IS NOT NULL "
        "AND stage=ANY(%s) THEN 'unknown' ELSE 'failed' END, result_code='retention_expired',"
        "updated_at=%s,finished_at=%s WHERE id=ANY(%s) AND status IN ('pending','running','retry_wait')",
        (list(EXTERNAL_STAGES), now, now, ids),
    )
    cursor.execute(
        "UPDATE admin_action_followup SET status='skipped',result_code='dependency_expired',"
        "finished_at=%s,updated_at=%s WHERE depends_on_id=ANY(%s) "
        "AND status IN ('pending','retry_wait')",
        (now, now, ids),
    )
    cursor.execute("DELETE FROM admin_action_followup WHERE id=ANY(%s)", (ids,))


def _settle_management_cards(cursor, limit):
    """只收口当前操作的下发中卡；终止事实持久化后重启仍能补刷。"""
    cursor.execute(
        "WITH unfinished AS (SELECT c.message_id FROM management_card_context c "
        "JOIN LATERAL (SELECT id FROM pending_action WHERE origin_card_message_id=c.message_id "
        "ORDER BY created_at DESC,id DESC LIMIT 1) p ON true "
        "WHERE c.state='dispatching' AND EXISTS (SELECT 1 FROM admin_action_followup f "
        "WHERE f.pending_action_id=p.id AND f.stage IN ('permission_recompute','publish_observe') "
        "AND f.status IN ('failed','skipped')) "
        "ORDER BY c.message_id FOR UPDATE OF c SKIP LOCKED LIMIT %s) "
        "UPDATE management_card_context c SET state='incomplete',dispatch_status='incomplete',"
        "state_version=state_version+1,card_sequence=card_sequence+1,needs_refresh=true,"
        "updated_at=now() FROM unfinished u WHERE c.message_id=u.message_id",
        (min(32, max(0, limit)),),
    )
