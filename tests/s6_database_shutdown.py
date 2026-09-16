"""停机用例只在目标进程的数据库后端结束后做一次恢复扫描。"""

import time
from datetime import UTC, datetime, timedelta

from lingxi.adapters.postgres import connect


def backend_is_present(connection, application_name):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
            "WHERE datname=current_database() AND application_name=%s)",
            (application_name,),
        )
        return cursor.fetchone()[0]


def wait_for_database_exit(dsn, application_name, *, timeout_seconds=5):
    deadline = time.monotonic() + timeout_seconds
    # 每次查询独立事务，避免重复读取同一个统计快照。
    with connect(dsn, dedicated=True, autocommit=True) as connection:
        while backend_is_present(connection, application_name):
            if time.monotonic() >= deadline:
                raise TimeoutError("停机子进程的数据库后端尚未结束")
            time.sleep(0.01)


def recover_after_database_exit(store, dsn, application_name, *, timeout_seconds=5):
    wait_for_database_exit(dsn, application_name, timeout_seconds=timeout_seconds)
    return store.recover_expired(now=datetime.now(UTC) + timedelta(seconds=121))
