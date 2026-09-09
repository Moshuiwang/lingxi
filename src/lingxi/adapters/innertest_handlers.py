"""scheduler 两个固定业务阶段，复用系统开通及逐用户只读探针。"""

from datetime import UTC, datetime

from lingxi.core.admin.followup_consumer import FollowupResult
from lingxi.core.ids import new_id


class InnertestFollowupHandlers:
    """资格与就绪不是同一个事实，不自动发欢迎卡。"""

    def __init__(self, *, store, runner, probe):
        """Runner 是已装配的 start_system，禁止注入本地补授计划。"""
        self.store, self.runner, self.probe = store, runner, probe

    def handle(self, item):
        """读取短事务后释放连接，网络期间不持有数据库活动槽。"""
        target = self._target(item)
        if target is None:
            return FollowupResult("failed", "identity_missing")
        if item.stage == "innertest_preprovision":
            if target[3] != "active":
                result = self.runner.start_system(
                    email=target[0], trace_id=item.trace_id, initiated_by_open_id=target[2]
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
                    return FollowupResult("failed", "provisioning_failed")
            return FollowupResult()
        return self._check(item, target)

    def _target(self, item):
        """批次原身份和真实用户重新关联，不用客户端 open_id 填补用户。"""
        with self.store.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT i.email,i.open_id,b.initiated_by,u.provisioning_state,u.id,"
                "u.account_state,u.permission_version FROM innertest_batch_item i "
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
            return FollowupResult("failed", "check_failed")
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
        with self.store.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO innertest_check(id,batch_item_id,user_id,permission_version,"
                "publish_version,started_at,finished_at,result_code,metric_count,trace_id) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    new_id("ich"),
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
        return FollowupResult("succeeded" if code == "check_passed" else "failed", code)

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
