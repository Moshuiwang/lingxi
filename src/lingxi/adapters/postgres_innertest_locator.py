"""同一短事务内复用邮箱唯一定位，listener 不另占第二条连接。"""

from datetime import UTC, datetime, timedelta

from lingxi.adapters.postgres_identity import DirectoryLookup
from lingxi.core.admin.innertest import InnertestError
from lingxi.core.identity.org_snapshot import DirectoryAvailability, SnapshotMember
from lingxi.core.identity.preprovision import locate_by_email


class TransactionDirectory:
    """从完整组织快照读取候选，保留非唯一结果。"""

    def __init__(self, connection):
        """连接归调用方事务持有。"""
        self.connection = connection

    def lookup_by_user_id(self, user_id):
        """不从不完整或到期资料推断目标身份。"""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM feishu_org_sync_run WHERE status='complete' "
                "AND expires_at>now() ORDER BY started_at DESC LIMIT 1"
            )
            run = cursor.fetchone()
            if run is None:
                raise InnertestError("roster_unavailable")
            cursor.execute(
                "SELECT tenant_key,member_key,open_id,user_id,union_id,display_name,"
                "display_name_locale,department_names FROM feishu_org_member_snapshot "
                "WHERE sync_run_id=%s AND user_id=%s",
                (run[0], user_id),
            )
            rows = cursor.fetchall()
        members = tuple(
            SnapshotMember(
                tenant_key=r[0],
                member_key=r[1],
                open_id=r[2],
                user_id=r[3],
                union_id=r[4],
                display_name=r[5],
                display_name_locale=r[6],
                department_names=tuple(r[7] or ()),
            )
            for r in rows
        )
        return DirectoryLookup(DirectoryAvailability.AVAILABLE, members)


def locate_transaction_email(email, *, connection):
    """花名册和组织资料只提供定位，绝不替代资格或银河权限。"""
    with connection.cursor() as cursor:
        cursor.execute("SELECT id,captured_at FROM roster_snapshot")
        row = cursor.fetchone()
        if row is None or row[1] <= datetime.now(UTC) - timedelta(days=90):
            raise InnertestError("roster_unavailable")
        cursor.execute(
            "SELECT personnel_id,email FROM roster_snapshot_row WHERE snapshot_id=%s", (row[0],)
        )
        rows = [dict(personnel_id=r[0], email=r[1]) for r in cursor.fetchall()]
    return locate_by_email(email, roster_rows=rows, directory=TransactionDirectory(connection))
