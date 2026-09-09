"""两进程每次准入同读数据库版本，不把读取故障误判为名单外。"""

from __future__ import annotations

from lingxi.adapters.postgres import connect
from lingxi.core.admin.innertest import InnertestError
from lingxi.core.identity.innertest_roster_gate import is_open_id_innertest_allowed


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
