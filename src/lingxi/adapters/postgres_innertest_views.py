"""批次只读投影：资格、阶段、开通和检查分别来自原权威。"""

from lingxi.core.admin.innertest import InnertestError, envelope


def batch_view(connection, scope, principal, batch):
    """先限制主体再取逐人明细，不泄露其他批次。"""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT status,trace_id,roster_version,pending_action_id FROM innertest_batch "
            "WHERE id=%s AND scope=%s AND initiated_by=%s",
            (batch, scope, principal),
        )
        row = cursor.fetchone()
        if row is None:
            raise InnertestError("batch_not_found")
        cursor.execute(
            "SELECT i.id,i.email,i.result_code,u.provisioning_state,u.account_state,"
            "u.permission_version,COALESCE((SELECT CASE WHEN o.status='pending' AND "
            "o.effect_started_at IS NOT NULL THEN 'unknown' ELSE o.status END FROM outreach_message o "
            "WHERE o.user_id=u.id AND o.purpose='apply' ORDER BY o.created_at DESC LIMIT 1),"
            "'notification_not_requested') FROM innertest_batch_item i LEFT JOIN app_user u "
            "ON u.feishu_open_id=i.open_id WHERE i.batch_id=%s ORDER BY i.email",
            (batch,),
        )
        people = _people(cursor.fetchall())
        cursor.execute(
            "SELECT batch_item_id,stage,status,result_code FROM admin_action_followup "
            "WHERE batch_id=%s ORDER BY created_at,id",
            (batch,),
        )
        stages = [dict(item_id=r[0], stage=r[1], state=r[2], code=r[3]) for r in cursor.fetchall()]
        cursor.execute(
            "SELECT DISTINCT ON(batch_item_id) batch_item_id,result_code,metric_count,"
            "permission_version,finished_at FROM innertest_check WHERE batch_item_id IN "
            "(SELECT id FROM innertest_batch_item WHERE batch_id=%s) "
            "ORDER BY batch_item_id,finished_at DESC",
            (batch,),
        )
        checks = [
            dict(
                item_id=r[0],
                code=r[1],
                metric_count=r[2],
                permission_version=r[3],
                finished_at=r[4].isoformat(),
            )
            for r in cursor.fetchall()
        ]
    return envelope(
        trace_id=row[1],
        state=row[0],
        batch_id=batch,
        roster_version=row[2],
        pending_action_id=row[3],
        items=people,
        stages=stages,
        checks=checks,
    )


def _people(rows):
    """用户事实的最小可见字段，不附带凭据或上游正文。"""
    people = []
    for item in rows:
        people.append(
            dict(
                item_id=item[0],
                email=item[1],
                membership=item[2],
                provisioning_state=item[3],
                account_state=item[4],
                permission_version=item[5],
                notification_state=item[6],
            )
        )
    return people
