"""扩员三工具数据库适配器，准备与确认使用同一资格和阶段权威。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from lingxi.adapters.innertest_request import connect
from lingxi.adapters.postgres_admin_followup import enqueue_followups
from lingxi.core.admin.followup import FollowupSpec
from lingxi.core.admin.innertest import InnertestError, envelope, normalized_intent, target_digest
from lingxi.core.ids import new_id


@dataclass(frozen=True)
class InnertestPrincipal:
    """服务端验证结果，不接受客户端自述。"""

    binding_id: str
    open_id: str
    version: int


class PostgresInnertestService:
    """每个请求与确认重读绑定、角色；数据库与审计失败整笔回滚。"""

    def __init__(self, dsn, *, scope, binding, locator, audit):
        """Locator 复用既有邮箱唯一定位，audit 不接收资料正文。"""
        self.dsn, self.scope, self.binding = dsn, scope, binding
        self.locator, self.audit = locator, audit

    def authenticate(self, uid):
        """只有已冻结 UID 能查绑定，角色登记是唯一授权依据。"""
        if uid != self.binding.peer_uid:
            raise InnertestError("not_authenticated")
        with connect(self.dsn) as connection:
            return self._principal(connection)

    def _principal(self, connection, expected=None):
        """锁住绑定与角色至当前事务结束，撤销后旧连接不可沿用身份。"""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT b.id,b.open_id,b.version,b.enabled,a.entry_status,"
                "a.permission_admin_granted,a.super_admin_granted "
                "FROM innertest_admin_binding b LEFT JOIN admin_registry a "
                "ON a.feishu_open_id=b.open_id AND a.entry_status='active' WHERE b.id=%s AND b.scope=%s FOR SHARE OF b",
                (self.binding.binding_id, self.scope),
            )
            row = cursor.fetchone()
            if row is None or not row[3]:
                raise InnertestError("binding_disabled")
            cursor.execute(
                "SELECT entry_status,permission_admin_granted,super_admin_granted "
                "FROM admin_registry WHERE feishu_open_id=%s AND entry_status='active' FOR SHARE",
                (row[1],),
            )
            roles = cursor.fetchone()
            if roles is None or roles[0] != "active" or not (roles[1] or roles[2]):
                raise InnertestError("not_authorized")
        value = InnertestPrincipal(row[0], row[1], row[2])
        if expected is not None and expected != value:
            raise InnertestError("binding_disabled")
        return value

    def call(self, principal, name, args):
        """工具名不是可执行字符串，只分发固定三个函数。"""
        methods = {
            "list_innertest_members": self.list_members,
            "prepare_innertest_additions": self.prepare,
            "get_innertest_batch": self.get_batch,
        }
        return methods[name](principal, **args)

    def list_members(self, principal, *, cursor=None, limit=20):
        """游标绑定名单版本，变化后要求重新查询第一页。"""
        with connect(self.dsn) as connection:
            self._principal(connection, principal)
            with connection.cursor() as cur:
                version = self._version(cur)
                offset = 0
                if cursor is not None:
                    try:
                        old, offset = map(int, cursor.split(":"))
                    except (ValueError, AttributeError) as error:
                        raise InnertestError("invalid_request") from error
                    if old != version or offset < 0:
                        raise InnertestError("stale_cursor")
                cur.execute(
                    "SELECT email,open_id FROM innertest_membership WHERE scope=%s "
                    "ORDER BY open_id LIMIT %s OFFSET %s",
                    (self.scope, limit + 1, offset),
                )
                rows = cur.fetchall()
        return envelope(
            version=version,
            members=[dict(email=r[0], open_id=r[1]) for r in rows[:limit]],
            cursor=f"{version}:{offset + limit}" if len(rows) > limit else None,
        )

    def _version(self, cursor, *, lock=False):
        """数据库模式只能由受控导入启用，MCP 准备不隐式切换。"""
        cursor.execute(
            "SELECT version,mode FROM innertest_roster_version WHERE scope=%s"
            + (" FOR UPDATE" if lock else ""),
            (self.scope,),
        )
        row = cursor.fetchone()
        if row is None or row[1] != "database":
            raise InnertestError("roster_unavailable")
        return row[0]

    def prepare(self, principal, *, request_key, emails):
        """先查业务键再定位；同键返回原批次，准备不新增资格。"""
        values, digest = normalized_intent(emails)
        with connect(self.dsn) as connection, connection.transaction():
            self._principal(connection, principal)
            with connection.cursor() as cursor:
                version = self._version(cursor, lock=True)
                cursor.execute(
                    "SELECT id,intent_digest FROM innertest_batch WHERE scope=%s "
                    "AND initiated_by=%s AND request_key=%s",
                    (self.scope, principal.open_id, request_key),
                )
                old = cursor.fetchone()
                if old:
                    if old[1] != digest:
                        raise InnertestError("idempotency_conflict")
                    return self._batch_view(connection, principal, old[0])
                targets = self._resolve(cursor, values)
                return self._insert_batch(
                    connection, cursor, principal, request_key, digest, version, targets
                )

    def _resolve(self, cursor, values):
        """保留每个规范化邮箱的结果，不能由管理员挑非唯一候选。"""
        from lingxi.core.identity.preprovision import PreprovisionSkip

        targets, seen = [], set()
        for email in values:
            target = self.locator(email, connection=cursor.connection)
            if isinstance(target, PreprovisionSkip):
                code = "identity_not_unique" if "multiple" in target.reason else "identity_missing"
                targets.append((email, None, None, code))
                continue
            open_id = target.open_id
            cursor.execute(
                "SELECT 1 FROM innertest_membership WHERE scope=%s AND open_id=%s",
                (self.scope, open_id),
            )
            existing = cursor.fetchone() is not None or open_id in seen
            targets.append(
                (email, open_id, target.personnel_id, "already_member" if existing else "new")
            )
            seen.add(open_id)
        if sum(len(str(v)) for row in targets for v in row) > 12000:
            raise InnertestError("card_too_large")
        return targets

    def _insert_batch(self, connection, cursor, principal, key, digest, version, targets):
        """批次、动作、发卡意图与审计共享事务。"""
        batch, action, trace = new_id("ibt"), new_id("pac"), new_id("trc")
        has_new = any(t[3] == "new" for t in targets)
        if has_new:
            self._pending_guard(cursor, principal, version)
            now = datetime.now(UTC)
            cursor.execute(
                "INSERT INTO pending_action(id,action_type,target_open_id,"
                "target_state_snapshot,initiated_by_open_id,confirm_deadline_at) "
                "VALUES(%s,'innertest_additions',%s,%s,%s,%s)",
                (
                    action,
                    principal.open_id,
                    str(version),
                    principal.open_id,
                    now + timedelta(seconds=600),
                ),
            )
        cursor.execute(
            "INSERT INTO innertest_batch(id,scope,initiated_by,binding_id,binding_version,"
            "request_key,intent_digest,roster_version,target_digest,pending_action_id,status,trace_id) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                batch,
                self.scope,
                principal.open_id,
                principal.binding_id,
                principal.version,
                key,
                digest,
                version,
                target_digest(targets),
                action if has_new else None,
                "pending" if has_new else "no_change",
                trace,
            ),
        )
        for email, open_id, personnel, code in targets:
            cursor.execute(
                "INSERT INTO innertest_batch_item(id,batch_id,email,open_id,personnel_id,"
                "result_code) VALUES(%s,%s,%s,%s,%s,%s)",
                (new_id("ibi"), batch, email, open_id, personnel, code),
            )
        if has_new:
            enqueue_followups(
                connection,
                pending_action_id=action,
                trace_id=trace,
                items=(
                    FollowupSpec(subject_key=batch, stage="confirmation_card_send", batch_id=batch),
                ),
            )
        self._audit(connection, batch, principal.open_id, trace, "prepared")
        return self._batch_view(connection, principal, batch)

    def _audit(self, connection, batch, subject, trace, action):
        """持久审计与状态一起提交；外部审计不可用同样回滚。"""
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO innertest_audit(id,batch_id,subject,trace_id,action) "
                "VALUES(%s,%s,%s,%s,%s)",
                (new_id("iau"), batch, subject, trace, action),
            )
        try:
            self.audit.record("innertest." + action, batch_id=batch, trace_id=trace)
        except Exception as error:
            raise InnertestError("audit_unavailable") from error

    def get_batch(self, principal, *, batch_id=None, request_key=None):
        """只投影本主体/本环境，查询不执行也不重发。"""
        with connect(self.dsn) as connection:
            self._principal(connection, principal)
            if request_key is not None:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT id FROM innertest_batch WHERE scope=%s AND initiated_by=%s "
                        "AND request_key=%s",
                        (self.scope, principal.open_id, request_key),
                    )
                    row = cursor.fetchone()
                    batch_id = row[0] if row else None
            return self._batch_view(connection, principal, batch_id)

    def _batch_view(self, connection, principal, batch):
        """后台和用户状态现读，不把阶段成功当作问数可用。"""
        from lingxi.adapters.postgres_innertest_views import batch_view

        return batch_view(connection, self.scope, principal.open_id, batch)

    def _pending_guard(self, cursor, principal, version):
        """旧确认可重新准备；不明卡片必须先核查，不能换键盲发。"""
        cursor.execute(
            "SELECT p.id,p.confirm_deadline_at,f.status,b.binding_version,b.roster_version,b.id,b.trace_id "
            "FROM pending_action p LEFT JOIN admin_action_followup f "
            "ON f.pending_action_id=p.id AND f.stage='confirmation_card_send' "
            "LEFT JOIN innertest_batch b ON b.pending_action_id=p.id "
            "WHERE p.target_open_id=%s AND p.status='pending'",
            (principal.open_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return
        if row[2] == "unknown":
            raise InnertestError("card_unknown")
        now = datetime.now(UTC)
        if row[5] and (row[1] <= now or row[3] != principal.version or row[4] != version):
            state = "expired" if row[1] <= now else "failed"
            cursor.execute(
                "UPDATE pending_action SET status=%s,decided_at=%s,reason='stale_confirmation' "
                "WHERE id=%s AND status='pending'",
                (state, now, row[0]),
            )
            cursor.execute("UPDATE innertest_batch SET status=%s WHERE id=%s", (state, row[5]))
            self._audit(cursor.connection, row[5], principal.open_id, row[6], state)
            return
        raise InnertestError("stale_confirmation")
