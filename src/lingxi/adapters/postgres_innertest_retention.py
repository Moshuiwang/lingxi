"""扩员批次和检查到期删除，持续资格不随审计到期移除。"""

from datetime import timedelta


def purge_innertest_history(connection, *, now, limit):
    """复用既有定时清理事务，每次固定小批量，不重置资格。"""
    cutoff = now - timedelta(hours=2160)
    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM innertest_check WHERE id IN "
            "(SELECT id FROM innertest_check WHERE started_at<=%s ORDER BY started_at LIMIT %s)",
            (cutoff, limit),
        )
        checks = cursor.rowcount
        cursor.execute(
            "DELETE FROM innertest_batch WHERE id IN "
            "(SELECT id FROM innertest_batch WHERE created_at<=%s ORDER BY created_at LIMIT %s)",
            (cutoff, limit),
        )
        return checks + cursor.rowcount
