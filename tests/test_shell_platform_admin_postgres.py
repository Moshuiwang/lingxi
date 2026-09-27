"""迁移 ``0099_drop_shell_platform_admin`` 的真库断言（Issue #887）。

四组事实只能在真库上证明：

1. **无角色即空操作**：库里没有目标角色时 upgrade / downgrade 都不动任何东西；真实
   alembic 链（真实角色名）上 ``head → -1 → head`` 同样不凭空造出角色。
2. **空壳往返**：按预发实读的形态建占位角色与三行默认权限 → upgrade 后它名下默认权限
   0 行、角色消失，其余对象的属主与 ACL 快照不变 → downgrade 后三行与原文逐字相等、
   角色回来且仍不能登录。
3. **非空壳跳过**：能登录 / 有成员关系 / 在本库拥有对象 / 别的库还引用它，任一条成立
   都只发提示、角色与默认权限原样保留。
4. **无权响亮失败**：非超级用户执行时报错，事务回滚后一切原样。

角色是集群级对象：本文件的目标角色与被授权角色一律用带随机后缀的探针名（迁移 SQL 以
角色名为参数渲染），不与其他用例的 ``anon`` 等同名角色冲突；谁建谁清，失败路径上
先删探针库再删角色。只有第 1 组的真实链用例使用真实角色名，且只读断言它不存在。
"""

from __future__ import annotations

import ast
import logging
import os
import unittest
import uuid
from typing import Any

from postgres_schema import ALEMBIC_INI, VERSIONS_DIRECTORY, psycopg_available

SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，空壳平台角色清理断言未验证（需真实 PostgreSQL）"
    if not os.environ.get("LINGXI_POSTGRES_DSN")
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，空壳平台角色清理断言未验证"
)

REVISION_FILE = VERSIONS_DIRECTORY / "0099_drop_shell_platform_admin.py"
REVISION = "0099_drop_shell_platform_admin"
PARENT = "0098_qa_corpus"
REAL_ROLE = "supabase_admin"
REAL_PLATFORM_ROLES = ("anon", "authenticated", "service_role")

# 预发本地库实读原文（Issue #887，迁库后、清理前），名字按探针替换后逐字比对。
STAGE_DEFAULT_ACL = {
    "f": "{postgres=X/supabase_admin,anon=X/supabase_admin,authenticated=X/supabase_admin,"
    "service_role=X/supabase_admin}",
    "r": "{postgres=arwdDxtm/supabase_admin,anon=arwdDxtm/supabase_admin,"
    "authenticated=arwdDxtm/supabase_admin,service_role=arwdDxtm/supabase_admin}",
    "S": "{postgres=rwU/supabase_admin,anon=rwU/supabase_admin,authenticated=rwU/supabase_admin,"
    "service_role=rwU/supabase_admin}",
}

# 其余对象的属主与 ACL：public 下的表 / 序列 / 函数 / schema 本身 / 别的角色的默认权限。
OTHERS_SNAPSHOT_SQL = """
SELECT 'class', c.relname, pg_get_userbyid(c.relowner), coalesce(c.relacl::text, '')
  FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace
UNION ALL
SELECT 'proc', p.proname, pg_get_userbyid(p.proowner), coalesce(p.proacl::text, '')
  FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace
UNION ALL
SELECT 'schema', n.nspname, pg_get_userbyid(n.nspowner), coalesce(n.nspacl::text, '')
  FROM pg_namespace n WHERE n.nspname = 'public'
UNION ALL
SELECT 'default_acl', pg_get_userbyid(d.defaclrole) || ':' || d.defaclobjtype::text,
       d.defaclnamespace::regnamespace::text, d.defaclacl::text
  FROM pg_default_acl d WHERE pg_get_userbyid(d.defaclrole) <> ALL(%s::text[])
ORDER BY 1, 2
"""


def revision_sql() -> dict[str, Any]:
    """静态取 0099 的两个 SQL 渲染函数（``alembic.op`` 用占位对象替代，不跑 upgrade 本身）。"""

    source = REVISION_FILE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(REVISION_FILE))
    body = [
        node
        for node in tree.body
        if not (isinstance(node, ast.ImportFrom) and node.module == "alembic")
    ]
    namespace: dict[str, Any] = {"__name__": "revision_0099_static", "op": None}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(REVISION_FILE), "exec"), namespace)
    return namespace


def _run_alembic(dsn: str, action: str, target: str) -> None:
    """进程内跑 alembic，进出各存取一次 logger 的 disabled 位（与 0096 用例同型）。"""

    from alembic import command
    from alembic.config import Config

    config = Config(str(ALEMBIC_INI))
    previous = os.environ.get("LINGXI_MIGRATION_DSN")
    os.environ["LINGXI_MIGRATION_DSN"] = dsn
    manager = logging.root.manager
    disabled_before = {
        name: logger.disabled
        for name, logger in manager.loggerDict.items()
        if isinstance(logger, logging.Logger)
    }
    try:
        getattr(command, action)(config, target)
    finally:
        if previous is None:
            os.environ.pop("LINGXI_MIGRATION_DSN", None)
        else:
            os.environ["LINGXI_MIGRATION_DSN"] = previous
        for name, was_disabled in disabled_before.items():
            logger = manager.loggerDict.get(name)
            if isinstance(logger, logging.Logger):
                logger.disabled = was_disabled


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN") and psycopg_available(), SKIP_REASON)
class ShellPlatformAdminTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        cls._psycopg = psycopg
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]
        cls._revision = revision_sql()

    def setUp(self) -> None:
        suffix = uuid.uuid4().hex[:8]
        self.role = f"p887_admin_{suffix}"
        # 第一位沿用真实的 postgres（任何集群都有），其余三位是探针，与原文位次一一对应。
        self.grantees = (
            "postgres",
            f"p887_anon_{suffix}",
            f"p887_auth_{suffix}",
            f"p887_svc_{suffix}",
        )
        self.executor = f"p887_exec_{suffix}"
        # 登记顺序与执行顺序相反：先删探针库（清掉库内依赖），再删角色。
        for name in (self.role, *self.grantees[1:], self.executor):
            self.addCleanup(self.admin, f"DROP ROLE IF EXISTS {name}")
        self.probe = self.create_probe_database()

    # ---------- 底座 ----------

    def database_dsn(self, database: str) -> str:
        from urllib.parse import urlsplit, urlunsplit

        return urlunsplit(urlsplit(self._dsn)._replace(path=f"/{database}"))

    def admin(self, sql: str, parameters: tuple = (), dsn: str | None = None) -> list[tuple]:
        with (
            self._psycopg.connect(dsn or self._dsn, autocommit=True) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(sql, parameters or None)
            return cursor.fetchall() if cursor.description else []

    def scalar(self, sql: str, parameters: tuple = (), dsn: str | None = None) -> Any:
        rows = self.admin(sql, parameters, dsn)
        return rows[0][0] if rows else None

    def create_probe_database(self) -> str:
        name = f"lingxi_p887_probe_{uuid.uuid4().hex[:8]}"
        self.admin(f"CREATE DATABASE {name}")
        self.addCleanup(self.admin, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        return self.database_dsn(name)

    def run_sql(self, sql: str, dsn: str, as_role: str | None = None) -> list[str]:
        """在一个事务里执行迁移 SQL，返回期间收到的 NOTICE；出错即回滚并原样抛出。"""

        notices: list[str] = []
        with self._psycopg.connect(dsn) as connection:
            connection.add_notice_handler(
                lambda diagnostic: notices.append(diagnostic.message_primary)
            )
            with connection.cursor() as cursor:
                if as_role:
                    cursor.execute(f"SET ROLE {as_role}")
                cursor.execute(sql)
        return notices

    def upgrade(self, dsn: str, as_role: str | None = None) -> list[str]:
        return self.run_sql(self._revision["_upgrade_sql"](self.role), dsn, as_role)

    def downgrade(self, dsn: str) -> list[str]:
        return self.run_sql(self._revision["_downgrade_sql"](self.role, self.grantees), dsn)

    def role_exists(self, name: str) -> bool:
        return self.scalar("SELECT count(*) FROM pg_roles WHERE rolname = %s", (name,)) == 1

    def default_acl(self, dsn: str) -> dict[str, str]:
        return {
            objtype: acl
            for objtype, acl in self.admin(
                "SELECT defaclobjtype::text, defaclacl::text FROM pg_default_acl"
                " WHERE defaclrole = (SELECT oid FROM pg_roles WHERE rolname = %s)"
                "   AND defaclnamespace = 'public'::regnamespace",
                (self.role,),
                dsn,
            )
        }

    def expected_acl(self) -> dict[str, str]:
        """预发原文按探针名替换；PostgreSQL 16 没有 MAINTAIN（m），原文的 m 相应去掉。"""

        version = int(self.scalar("SHOW server_version_num"))
        expected = {}
        for objtype, text in STAGE_DEFAULT_ACL.items():
            text = text.replace("supabase_admin", self.role)
            for real, probe in zip(("postgres", *REAL_PLATFORM_ROLES), self.grantees, strict=True):
                text = text.replace(f"{real}=", f"{probe}=")
            if version < 170000:
                text = text.replace("arwdDxtm", "arwdDxt")
            expected[objtype] = text
        return expected

    def others_snapshot(self, dsn: str) -> list[tuple]:
        return self.admin(OTHERS_SNAPSHOT_SQL, ([self.role],), dsn)

    def build_stage_shape(self, dsn: str) -> None:
        """按预发形态建占位角色、被授权角色、三行默认权限，另放几件「其余对象」。

        三行用 ``ALL`` 授予（与 downgrade 的逐项写法是两条独立路径），建完先与原文
        逐字比对，证明夹具本身就是预发的形态。
        """

        for name in self.grantees[1:]:
            self.admin(f"CREATE ROLE {name} NOLOGIN")
        self.admin(f"CREATE ROLE {self.role} NOLOGIN")
        to = ", ".join(self.grantees)
        for objects in ("FUNCTIONS", "TABLES", "SEQUENCES"):
            privilege = "EXECUTE" if objects == "FUNCTIONS" else "ALL"
            self.admin(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {self.role} IN SCHEMA public"
                f" GRANT {privilege} ON {objects} TO {to}",
                dsn=dsn,
            )
        anon = self.grantees[1]
        self.admin(
            "CREATE TABLE public.p887_other (id bigserial PRIMARY KEY);"
            "CREATE FUNCTION public.p887_fn() RETURNS int LANGUAGE sql"
            " SET search_path = pg_catalog, pg_temp AS 'SELECT 1';"
            f"GRANT SELECT ON public.p887_other TO {anon};"
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {anon};",
            dsn=dsn,
        )
        self.assertEqual(self.default_acl(dsn), self.expected_acl(), "夹具与预发实读原文不一致")

    def assert_untouched(self, dsn: str, notices: list[str], reason: str) -> None:
        self.assertTrue(self.role_exists(self.role), "非空壳角色被删了")
        self.assertEqual(self.default_acl(dsn), self.expected_acl(), "非空壳角色的默认权限被动了")
        self.assertTrue(
            any(reason in notice for notice in notices), f"提示里没有说明原因：{notices}"
        )

    # ---------- ① 无角色 ----------

    def test_no_role_upgrade_and_downgrade_are_noops(self) -> None:
        before = self.others_snapshot(self.probe)
        self.assertEqual(self.upgrade(self.probe), [])
        # 被授权角色缺席（探针只有 postgres）→ downgrade 也不造角色。
        self.assertEqual(self.downgrade(self.probe), [])
        self.assertFalse(self.role_exists(self.role))
        self.assertEqual(self.default_acl(self.probe), {})
        self.assertEqual(self.others_snapshot(self.probe), before)

    def test_real_chain_without_platform_roles_never_creates_role(self) -> None:
        self.assertFalse(self.role_exists(REAL_ROLE), f"集群里已有 {REAL_ROLE}，本测试拒绝接管")
        self.assertFalse(
            all(self.role_exists(name) for name in REAL_PLATFORM_ROLES),
            "集群里三个平台角色都在，downgrade 会真建角色；本测试拒绝在这种集群上跑",
        )
        _run_alembic(self.probe, "upgrade", REVISION)
        _run_alembic(self.probe, "downgrade", PARENT)
        self.assertFalse(self.role_exists(REAL_ROLE))
        _run_alembic(self.probe, "upgrade", REVISION)
        self.assertEqual(
            self.scalar("SELECT version_num FROM alembic_version", dsn=self.probe), REVISION
        )
        self.assertFalse(self.role_exists(REAL_ROLE))

    # ---------- ② 空壳往返 ----------

    def test_shell_role_is_dropped_and_restored_verbatim(self) -> None:
        self.build_stage_shape(self.probe)
        before = self.others_snapshot(self.probe)

        self.upgrade(self.probe)
        self.assertEqual(self.default_acl(self.probe), {}, "默认权限没有撤干净")
        self.assertEqual(
            self.scalar(
                "SELECT count(*) FROM pg_default_acl d JOIN pg_roles r ON r.oid = d.defaclrole"
                " WHERE r.rolname = %s",
                (self.role,),
                self.probe,
            )
            or 0,
            0,
        )
        self.assertFalse(self.role_exists(self.role), "空壳角色没有被删")
        self.assertEqual(self.others_snapshot(self.probe), before, "其余对象的属主或 ACL 被动了")

        self.downgrade(self.probe)
        self.assertTrue(self.role_exists(self.role))
        self.assertEqual(
            self.admin(
                "SELECT rolsuper, rolcanlogin, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls"
                " FROM pg_roles WHERE rolname = %s",
                (self.role,),
            ),
            [(False, False, False, False, False, False)],
        )
        self.assertEqual(
            self.default_acl(self.probe), self.expected_acl(), "downgrade 没有逐字复原三行"
        )
        self.assertEqual(self.others_snapshot(self.probe), before)

        # 复原后再 upgrade 仍能清掉：往返是闭合的。
        self.upgrade(self.probe)
        self.assertFalse(self.role_exists(self.role))

    # ---------- ③ 非空壳跳过 ----------

    def test_role_that_can_log_in_is_skipped(self) -> None:
        self.build_stage_shape(self.probe)
        self.admin(f"ALTER ROLE {self.role} LOGIN")
        self.assert_untouched(self.probe, self.upgrade(self.probe), "不是空壳")

    def test_role_with_membership_is_skipped(self) -> None:
        self.build_stage_shape(self.probe)
        self.admin(f"GRANT {self.grantees[1]} TO {self.role}")
        self.assert_untouched(self.probe, self.upgrade(self.probe), "成员关系")

    def test_role_owning_an_object_is_skipped(self) -> None:
        self.build_stage_shape(self.probe)
        self.admin(
            f"CREATE TABLE public.p887_owned (id int); ALTER TABLE public.p887_owned OWNER TO {self.role}",
            dsn=self.probe,
        )
        self.assert_untouched(self.probe, self.upgrade(self.probe), "拥有")

    def test_role_referenced_from_another_database_is_skipped(self) -> None:
        self.build_stage_shape(self.probe)
        other = self.create_probe_database()
        self.admin(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {self.role} IN SCHEMA public GRANT SELECT ON TABLES TO postgres",
            dsn=other,
        )
        self.assert_untouched(self.probe, self.upgrade(self.probe), "引用")

    # ---------- ④ 无权 ----------

    def test_executor_without_privilege_fails_loudly(self) -> None:
        self.build_stage_shape(self.probe)
        self.admin(f"CREATE ROLE {self.executor} NOLOGIN")
        with self.assertRaises(self._psycopg.errors.RaiseException) as caught:
            self.upgrade(self.probe, as_role=self.executor)
        self.assertIn("无权清理空壳角色", str(caught.exception))
        self.assertTrue(self.role_exists(self.role))
        self.assertEqual(
            self.default_acl(self.probe), self.expected_acl(), "失败后默认权限没有原样保留"
        )


if __name__ == "__main__":
    unittest.main()
