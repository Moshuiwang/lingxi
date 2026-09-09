"""独立授权的欢迎卡发送前执行新检查，动态资格不隐含通知授权。"""

from datetime import UTC, datetime

from lingxi.adapters.postgres import connect
from lingxi.core.admin.innertest import InnertestError
from lingxi.core.ids import new_id


class CheckedInnertestSender:
    """只有受控 outreach 装配此口；批次消费者从不持有它。"""

    def __init__(self, *, dsn, sender, probe, initiated_by):
        """发送凭据沿用原 sender，逐用户探针不使用服务账号。"""
        self.dsn, self.sender, self.probe, self.initiated_by = dsn, sender, probe, initiated_by

    def send_card(self, *, open_id, card, dedupe_key):
        """检查与实际发送都复核当前权限，外发开始后未知永不盲发。"""
        self._authorized()
        if ":precheck:" not in dedupe_key:
            snapshot = self._snapshot(open_id)
            started, code, count = datetime.now(UTC), "check_passed", None
            try:
                count = self.probe.list_metrics(user_id=snapshot[0])
                if count < 1:
                    code = "check_failed"
            except Exception as error:
                code = "check_failed" if getattr(error, "denied", False) else "check_unknown"
            if not self._still_current(open_id, snapshot):
                code = "check_version_changed"
            self._record_check(snapshot, started, code, count)
            if code != "check_passed":
                raise InnertestError(code)
            self._authorized()
            if not self._still_current(open_id, snapshot):
                self._record_check(snapshot, started, "check_version_changed", count)
                raise InnertestError("check_version_changed")
        self._start_effect(dedupe_key)
        try:
            message_id = self.sender.send_card(open_id=open_id, card=card, dedupe_key=dedupe_key)
            if not message_id:
                raise InnertestError("notification_unknown")
            return message_id
        except Exception as error:
            definite = getattr(error, "definite", False)
            with connect(self.dsn) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE outreach_message SET status=%s,effect_started_at="
                    "CASE WHEN %s THEN NULL ELSE effect_started_at END,last_error=%s "
                    "WHERE dedupe_key=%s AND status<>'delivered'",
                    (
                        "failed" if definite else "unknown",
                        definite,
                        "notification_failed" if definite else "notification_unknown",
                        dedupe_key,
                    ),
                )
            raise InnertestError(
                "notification_failed" if definite else "notification_unknown"
            ) from error

    def _authorized(self):
        """CLI 的先行校验不能替代逐次发送时角色现读。"""
        with connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM admin_registry WHERE feishu_open_id=%s AND entry_status='active' "
                "AND (permission_admin_granted OR super_admin_granted)",
                (self.initiated_by,),
            )
            if cursor.fetchone() is None:
                raise InnertestError("not_authorized")

    def _snapshot(self, open_id):
        """账号和当前发布引用共同标识本次检查能说明的范围。"""
        with connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT u.id,u.permission_version,p.id FROM app_user u JOIN publish_outbox p "
                "ON p.user_id=u.id AND p.permission_version=u.permission_version "
                "WHERE u.feishu_open_id=%s AND u.account_state='enabled' "
                "AND u.provisioning_state='active' AND p.status='published' "
                "ORDER BY p.created_at DESC LIMIT 1",
                (open_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise InnertestError("check_failed")
        return row

    def _record_check(self, snapshot, started, code, count):
        """检查只记版本、计数与受控错误，不存指标正文或令牌。"""
        with connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO innertest_check(id,user_id,permission_version,publish_version,"
                "started_at,finished_at,result_code,metric_count,trace_id) "
                "VALUES(%s,%s,%s,%s,%s,now(),%s,%s,%s)",
                (
                    new_id("ich"),
                    snapshot[0],
                    snapshot[1],
                    snapshot[1],
                    started,
                    code,
                    count,
                    new_id("trc"),
                ),
            )

    def _start_effect(self, key):
        """上轮开始外发却未记账时，保持结果不明；绝不再次发送。"""
        with connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE outreach_message SET effect_started_at=now() "
                "WHERE dedupe_key=%s AND status='pending' AND effect_started_at IS NULL RETURNING id",
                (key,),
            )
            if cursor.fetchone() is None:
                cursor.execute(
                    "UPDATE outreach_message SET status='unknown',last_error='notification_unknown' "
                    "WHERE dedupe_key=%s AND status<>'delivered'",
                    (key,),
                )
                error = True
            else:
                error = False
        if error:
            raise InnertestError("notification_unknown")

    def _still_current(self, open_id, snapshot):
        """账号失效同样使旧检查过期，仍留下受控检查记录。"""
        try:
            return self._snapshot(open_id) == snapshot
        except InnertestError:
            return False
