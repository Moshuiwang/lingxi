"""两进程每次准入同读数据库版本，不把读取故障误判为名单外。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_innertest_locator import locate_transaction_email
from lingxi.core.admin.innertest import InnertestError
from lingxi.core.identity.innertest_roster_gate import is_open_id_innertest_allowed
from lingxi.core.identity.preprovision import PreprovisionSkip
from lingxi.core.ids import new_id
from lingxi.core.permission.account_match import normalize_email


class PostgresInnertestRoster:
    """旧环境变量只在明确 legacy 模式使用；动态切换后不可静默回落。"""

    def __init__(self, dsn, *, scope, legacy=frozenset()):
        """Scope 由部署环境和机器人应用组成，不从协议输入读取。"""
        self.dsn, self.scope, self.legacy = dsn, scope, legacy

    def snapshot(self, open_id):
        """同语句返回版本及目标命中，故障传播为技术失败。"""
        with connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT v.version,v.mode,EXISTS(SELECT 1 FROM innertest_membership m "
                "WHERE m.scope=v.scope AND m.open_id=%s) FROM innertest_roster_version v "
                "WHERE v.scope=%s",
                (open_id, self.scope),
            )
            row = cursor.fetchone()
        if row is None:
            return 0, is_open_id_innertest_allowed(open_id, self.legacy)
        if row[1] == "legacy":
            return row[0], is_open_id_innertest_allowed(open_id, self.legacy)
        return row[0], row[2]

    def __call__(self, open_id):
        """正式准入调用端只取命中；保留版本供部署只读验收。"""
        return self.snapshot(open_id)[1]

    def progress(self, open_id):
        """尚未真正开始核验时只说尚未开始，不冒称身份已核验。"""
        with connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT f.status FROM innertest_membership m "
                "JOIN innertest_batch_item i ON i.batch_id=m.batch_id AND i.open_id=m.open_id "
                "JOIN admin_action_followup f ON f.id=i.followup_id "
                "WHERE m.scope=%s AND m.open_id=%s",
                (self.scope, open_id),
            )
            row = cursor.fetchone()
        if row and row[0] in {"pending", "retry_wait"}:
            return "onboarding.innertest_waiting"
        if row and row[0] == "running":
            return "onboarding.checking"
        return None

    def recovery_status(self):
        """兼容性声明不删除动态资格或阶段。"""
        with connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT version,mode FROM innertest_roster_version WHERE scope=%s", (self.scope,)
            )
            row = cursor.fetchone()
        return dict(
            schema_revision=1,
            compatible=True,
            version=row[0] if row else 0,
            mode=row[1] if row else "legacy",
            requires_dynamic_roster=bool(row and row[1] == "database"),
        )


def compare_legacy_sources(gateway, scheduler):
    """只输出固定导入集合与摘要；差异不能擅自取并集。"""
    import hashlib

    if gateway != scheduler:
        raise InnertestError("legacy_roster_mismatch")
    values = tuple(sorted(gateway))
    return values, hashlib.sha256("\n".join(values).encode()).hexdigest()


def binding_status(dsn, *, scope, binding_id):
    """部署只读核对当前绑定版本与角色，不输出主体资料。"""
    with connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT b.version,b.enabled,EXISTS(SELECT 1 FROM admin_registry a "
            "WHERE a.feishu_open_id=b.open_id AND a.entry_status='active' AND "
            "(a.permission_admin_granted OR a.super_admin_granted)) "
            "FROM innertest_admin_binding b WHERE b.id=%s AND b.scope=%s",
            (binding_id, scope),
        )
        row = cursor.fetchone()
    return dict(
        binding_id=binding_id,
        version=row[0] if row else None,
        enabled=bool(row and row[1]),
        authorized=bool(row and row[2]),
    )


class RosterImportRejectedError(InnertestError):
    """受控导入被拒绝；``detail`` 只装邮箱与拒绝原因码，不含任何飞书标识。"""

    def __init__(self, code, *, detail=()):
        """``detail`` 供操作者核对具体是哪些邮箱、为什么没能定位。"""
        super().__init__(code)
        self.detail = tuple(detail)


@dataclass(frozen=True)
class RosterImportPlan:
    """一次受控导入的预演或执行结果：待写入成员与固定顺序摘要。"""

    scope: str
    members: tuple[tuple[str, str], ...]
    digest: str


def _import_digest(emails):
    """按排序后的邮箱算摘要；重复邮箱排序后仍相邻可见，不会被悄悄去重。"""
    if not emails:
        raise InnertestError("empty_email_list")
    normalized = [normalize_email(email) for email in emails]
    if len(normalized) != len(set(normalized)):
        raise RosterImportRejectedError("duplicate_email_in_list")
    values = tuple(sorted(normalized))
    return values, hashlib.sha256("\n".join(values).encode()).hexdigest()


def _resolve_targets(connection, *, emails):
    """邮箱逐条定位到组织成员；任意一条解析失败即整份收集拒绝原因。"""
    resolved, skips = [], []
    for email in emails:
        located = locate_transaction_email(email, connection=connection)
        if isinstance(located, PreprovisionSkip):
            skips.append((email, located.reason))
        else:
            resolved.append((email, located.open_id))
    if skips:
        raise RosterImportRejectedError("email_resolution_failed", detail=tuple(skips))
    return tuple(resolved)


def _import_precondition(cursor, *, scope, lock):
    """目标 scope 已切数据库模式、或已经有成员，两种情形都不允许再次导入。"""
    cursor.execute(
        "SELECT mode FROM innertest_roster_version WHERE scope=%s"
        + (" FOR UPDATE" if lock else ""),
        (scope,),
    )
    row = cursor.fetchone()
    if row and row[0] == "database":
        return "scope_already_database"
    cursor.execute("SELECT EXISTS(SELECT 1 FROM innertest_membership WHERE scope=%s)", (scope,))
    if cursor.fetchone()[0]:
        return "scope_already_has_members"
    return None


def _plan_or_check(connection, *, scope, gateway_legacy, scheduler_legacy, emails, lock):
    """预演与执行共用的核对：前置条件、旧名单一致性、逐邮箱定位与摘要。

    ``emails`` 先落成元组：下面两步各自完整遍历一次，一次性迭代器传入会让
    第二遍悄悄看到空集合，从而漏写而不报错。
    """
    emails = tuple(emails)
    with connection.cursor() as cursor:
        blocked = _import_precondition(cursor, scope=scope, lock=lock)
    if blocked:
        raise RosterImportRejectedError(blocked)
    compare_legacy_sources(set(gateway_legacy), set(scheduler_legacy))
    _, digest = _import_digest(emails)
    resolved = _resolve_targets(connection, emails=emails)
    return resolved, digest


def plan_roster_import(dsn, *, scope, gateway_legacy, scheduler_legacy, emails):
    """只读预演：不写任何表，返回待写入集合与摘要，或抛出拒绝原因。"""
    with connect(dsn) as connection:
        resolved, digest = _plan_or_check(
            connection,
            scope=scope,
            gateway_legacy=gateway_legacy,
            scheduler_legacy=scheduler_legacy,
            emails=emails,
            lock=False,
        )
    return RosterImportPlan(scope=scope, members=resolved, digest=digest)


def apply_roster_import(
    dsn,
    *,
    scope,
    gateway_legacy,
    scheduler_legacy,
    emails,
    confirm_digest,
    admin_open_id,
    binding_id,
):
    """受控导入：单事务写版本行、绑定行与成员行；任何拒绝条件下整笔回滚。"""
    if not admin_open_id:
        raise InnertestError("delegated_subject_not_registered")
    with connect(dsn) as connection, connection.transaction():
        resolved, digest = _plan_or_check(
            connection,
            scope=scope,
            gateway_legacy=gateway_legacy,
            scheduler_legacy=scheduler_legacy,
            emails=emails,
            lock=True,
        )
        if digest != confirm_digest:
            raise RosterImportRejectedError("digest_mismatch")
        _write_import(
            connection,
            scope=scope,
            members=resolved,
            digest=digest,
            admin_open_id=admin_open_id,
            binding_id=binding_id,
        )
    return RosterImportPlan(scope=scope, members=resolved, digest=digest)


def _write_import(connection, *, scope, members, digest, admin_open_id, binding_id):
    """三张表加一条审计在同一事务写入；调用方已确认摘要与前置条件。"""
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO innertest_roster_version(scope,mode,import_digest) "
            "VALUES(%s,'database',%s) ON CONFLICT(scope) DO UPDATE SET "
            "mode='database',import_digest=EXCLUDED.import_digest,updated_at=now()",
            (scope, digest),
        )
        cursor.execute(
            "INSERT INTO innertest_admin_binding(id,scope,open_id,version,enabled) "
            "VALUES(%s,%s,%s,1,true)",
            (binding_id, scope, admin_open_id),
        )
        for email, open_id in members:
            cursor.execute(
                "INSERT INTO innertest_membership(scope,open_id,email) VALUES(%s,%s,%s)",
                (scope, open_id, email),
            )
        cursor.execute(
            "INSERT INTO innertest_audit(id,action,subject,trace_id) VALUES(%s,%s,%s,%s)",
            (new_id("iau"), "roster_imported", admin_open_id, new_id("trc")),
        )


def roster_import_status(dsn, *, scope):
    """受控导入回读：模式、版本、导入摘要与当前成员数，供只读校验使用。"""
    with connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT version,mode,import_digest FROM innertest_roster_version WHERE scope=%s",
            (scope,),
        )
        row = cursor.fetchone()
        cursor.execute("SELECT count(*) FROM innertest_membership WHERE scope=%s", (scope,))
        member_count = cursor.fetchone()[0]
    return dict(
        version=row[0] if row else 0,
        mode=row[1] if row else "legacy",
        import_digest=row[2] if row else None,
        member_count=member_count,
    )
