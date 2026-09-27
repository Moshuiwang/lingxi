"""清掉迁库后留下的空壳平台管理角色 ``supabase_admin``（Issue #887）。

Revision ID: 0099_drop_shell_platform_admin
Revises: 0098_qa_corpus

## 为什么

2.6.0 把数据库从托管平台整体迁到本机 PostgreSQL 时，为了让来源库的默认权限原样
恢复，目标库上按同名建了一个占位角色 ``supabase_admin``：不能登录、没有任何特殊
属性、不属于任何角色、在库里不拥有任何对象，唯一的用处是承载托管平台当年给它设的
三行默认权限（``public`` 下 FUNCTIONS / TABLES / SEQUENCES，授予 ``postgres`` /
``anon`` / ``authenticated`` / ``service_role``）。本机库上从来没有人以它的身份建
对象，这三行默认权限也就永远不会生效；留着它只会让每次盘点角色时多一个说不清来历
的名字。

## 改了什么

一段 ``DO`` 块，按顺序判断：

1. 角色不存在（CI、本机容器、已经清过的库）→ 什么都不做。
2. **空壳守卫**，任一不满足 → ``RAISE NOTICE`` 说明原因，什么都不做：
   六个属性（超级用户 / 登录 / 建角色 / 建库 / 复制 / 绕过行级安全）全为假；
   成员关系为 0（既不是谁的成员，也没有成员）；本库里属主为它的表、函数、schema、
   类型为 0；集群共享依赖 ``pg_shdepend`` 里引用它的每一行都在**本库**、且都是
   **它自己名下的** schema 级默认权限行（别的库、共享对象、别人的授权一概不许有）；
   它名下的默认权限行都是 schema 级、对象类型在表 / 序列 / 函数 / 类型之内。
3. 执行身份没有删它的权限 → 响亮失败（迁移应以超级用户身份执行）。
4. 逐行撤掉它名下的默认权限，回读为 0 行，否则响亮失败；再 ``DROP ROLE``。

## 没改什么

不碰 ``anon`` / ``authenticated`` / ``service_role``（它们在本库有实际授权），不碰
任何其他角色的默认权限、对象属主与对象权限，不改成员关系。

## 回滚

``downgrade()`` 只在 ``supabase_admin`` 不存在、且四个被授权角色都存在时才动：
``CREATE ROLE supabase_admin NOLOGIN``，再按预发实读的原文补回三行默认权限
（表的 ``MAINTAIN`` 只在 PostgreSQL 17 及以上存在，低版本补回其余七项）。其余情况
（角色还在、或平台角色缺席的库）什么都不做——凭空造一个角色没有意义。不删数据。

## 为什么这里由迁移删角色

``migrations/README.md`` 在 ``0054`` 一节立过「角色清退由 Ops 显式执行」：角色是集群级
对象，库级迁移不该去删可能被别处引用的东西。本条是有意的例外，理由与边界写在
README 的本 revision 小节：删之前先证明它是空壳、且集群里除本库自己名下的默认权限外
没有任何引用；证明不了就跳过，不删。
"""

from __future__ import annotations

import re

from alembic import op

revision: str = "0099_drop_shell_platform_admin"
down_revision: str | None = "0098_qa_corpus"
branch_labels: str | None = None
depends_on: str | None = None

# 要清掉的占位角色，与预发实读的四个被授权角色（顺序即原文 ACL 的顺序）。
TARGET_ROLE = "supabase_admin"
GRANTEES: tuple[str, ...] = ("postgres", "anon", "authenticated", "service_role")

# 角色名直接嵌进 SQL 字面量，只接受不需要引号的小写标识符。
_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

_UPGRADE_TEMPLATE = r"""
DO $drop_shell_role$
DECLARE
    c_role      CONSTANT text := '%(role)s';
    v_oid       oid;
    v_db        oid;
    v_count     bigint;
    v_executor  record;
    v_row       record;
    v_grantee   text;
    v_objects   text;
BEGIN
    SELECT r.oid INTO v_oid FROM pg_catalog.pg_roles r WHERE r.rolname = c_role;
    IF v_oid IS NULL THEN
        RETURN;
    END IF;
    SELECT d.oid INTO v_db FROM pg_catalog.pg_database d WHERE d.datname = pg_catalog.current_database();

    -- 空壳守卫：任一条不满足就只说明、不动手。
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles r
                WHERE r.oid = v_oid
                  AND (r.rolsuper OR r.rolcanlogin OR r.rolcreaterole OR r.rolcreatedb
                       OR r.rolreplication OR r.rolbypassrls)) THEN
        RAISE NOTICE '角色 %% 带有超级用户 / 登录 / 建角色 / 建库 / 复制 / 绕过行级安全属性之一，不是空壳，本次不清理', c_role;
        RETURN;
    END IF;
    SELECT pg_catalog.count(*) INTO v_count FROM pg_catalog.pg_auth_members m
     WHERE m.roleid = v_oid OR m.member = v_oid;
    IF v_count > 0 THEN
        RAISE NOTICE '角色 %% 仍有 %% 条成员关系，不是空壳，本次不清理', c_role, v_count;
        RETURN;
    END IF;
    SELECT (SELECT pg_catalog.count(*) FROM pg_catalog.pg_class c WHERE c.relowner = v_oid)
         + (SELECT pg_catalog.count(*) FROM pg_catalog.pg_proc p WHERE p.proowner = v_oid)
         + (SELECT pg_catalog.count(*) FROM pg_catalog.pg_namespace n WHERE n.nspowner = v_oid)
         + (SELECT pg_catalog.count(*) FROM pg_catalog.pg_type t WHERE t.typowner = v_oid)
      INTO v_count;
    IF v_count > 0 THEN
        RAISE NOTICE '角色 %% 在本库拥有 %% 个对象，不是空壳，本次不清理', c_role, v_count;
        RETURN;
    END IF;
    SELECT pg_catalog.count(*) INTO v_count FROM pg_catalog.pg_shdepend s
     WHERE s.refclassid = 'pg_catalog.pg_authid'::pg_catalog.regclass
       AND s.refobjid = v_oid
       AND NOT (s.dbid = v_db
                AND s.classid = 'pg_catalog.pg_default_acl'::pg_catalog.regclass
                AND s.objid IN (SELECT d.oid FROM pg_catalog.pg_default_acl d
                                 WHERE d.defaclrole = v_oid));
    IF v_count > 0 THEN
        RAISE NOTICE '角色 %% 在集群里还有 %% 处引用不是本库它自己名下的默认权限（别的库、共享对象或授权），本次不清理', c_role, v_count;
        RETURN;
    END IF;
    SELECT pg_catalog.count(*) INTO v_count FROM pg_catalog.pg_default_acl d
     WHERE d.defaclrole = v_oid
       AND (d.defaclnamespace = 0 OR d.defaclobjtype NOT IN ('r', 'S', 'f', 'T'));
    IF v_count > 0 THEN
        RAISE NOTICE '角色 %% 名下有 %% 行全库级或非常见类型的默认权限，本次不清理', c_role, v_count;
        RETURN;
    END IF;

    -- 删角色要超级用户，或「能建角色 + 对它有管理权 + 能行使它的权限」（改它的默认权限）。
    SELECT r.rolsuper, r.rolcreaterole INTO v_executor
      FROM pg_catalog.pg_roles r WHERE r.rolname = current_user;
    IF NOT (v_executor.rolsuper
            OR (v_executor.rolcreaterole
                AND pg_catalog.pg_has_role(current_user, v_oid, 'MEMBER WITH ADMIN OPTION')
                AND pg_catalog.pg_has_role(current_user, v_oid, 'USAGE'))) THEN
        RAISE EXCEPTION '当前身份 %% 无权清理空壳角色 %%', current_user, c_role
            USING HINT = '迁移应以 LINGXI_MIGRATION_DSN 里的超级用户身份执行';
    END IF;

    FOR v_row IN
        SELECT d.defaclobjtype AS objtype, n.nspname, d.defaclacl
          FROM pg_catalog.pg_default_acl d
          JOIN pg_catalog.pg_namespace n ON n.oid = d.defaclnamespace
         WHERE d.defaclrole = v_oid
         ORDER BY n.nspname, d.defaclobjtype
    LOOP
        v_objects := CASE v_row.objtype
                         WHEN 'r' THEN 'TABLES' WHEN 'S' THEN 'SEQUENCES'
                         WHEN 'f' THEN 'FUNCTIONS' WHEN 'T' THEN 'TYPES' END;
        FOR v_grantee IN
            SELECT DISTINCT CASE WHEN a.grantee = 0 THEN 'PUBLIC'
                                 ELSE pg_catalog.quote_ident(g.rolname) END
              FROM pg_catalog.aclexplode(v_row.defaclacl) a
              LEFT JOIN pg_catalog.pg_roles g ON g.oid = a.grantee
        LOOP
            EXECUTE pg_catalog.format(
                'ALTER DEFAULT PRIVILEGES FOR ROLE %%I IN SCHEMA %%I REVOKE ALL ON %%s FROM %%s',
                c_role, v_row.nspname, v_objects, v_grantee);
        END LOOP;
    END LOOP;

    SELECT pg_catalog.count(*) INTO v_count FROM pg_catalog.pg_default_acl d WHERE d.defaclrole = v_oid;
    IF v_count <> 0 THEN
        RAISE EXCEPTION '撤完后角色 %% 名下仍有 %% 行默认权限，不删角色', c_role, v_count;
    END IF;
    EXECUTE pg_catalog.format('DROP ROLE %%I', c_role);
END
$drop_shell_role$;
"""

_DOWNGRADE_TEMPLATE = r"""
DO $restore_shell_role$
DECLARE
    c_role      CONSTANT text := '%(role)s';
    c_grantees  CONSTANT text[] := ARRAY[%(grantees)s];
    v_to        text;
    v_tables    text := 'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER';
BEGIN
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname = c_role) THEN
        RETURN;
    END IF;
    IF (SELECT pg_catalog.count(*) FROM pg_catalog.pg_roles r WHERE r.rolname = ANY(c_grantees))
       <> pg_catalog.cardinality(c_grantees) THEN
        RETURN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n WHERE n.nspname = 'public') THEN
        RETURN;
    END IF;
    SELECT pg_catalog.string_agg(pg_catalog.quote_ident(g), ', ' ORDER BY o)
      INTO v_to FROM pg_catalog.unnest(c_grantees) WITH ORDINALITY AS u(g, o);
    -- MAINTAIN 自 PostgreSQL 17 起才有；预发 / 生产原文是 17 上的 arwdDxtm。
    IF pg_catalog.current_setting('server_version_num')::integer >= 170000 THEN
        v_tables := v_tables || ', MAINTAIN';
    END IF;
    EXECUTE pg_catalog.format('CREATE ROLE %%I NOLOGIN', c_role);
    EXECUTE pg_catalog.format(
        'ALTER DEFAULT PRIVILEGES FOR ROLE %%I IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO %%s',
        c_role, v_to);
    EXECUTE pg_catalog.format(
        'ALTER DEFAULT PRIVILEGES FOR ROLE %%I IN SCHEMA public GRANT %%s ON TABLES TO %%s',
        c_role, v_tables, v_to);
    EXECUTE pg_catalog.format(
        'ALTER DEFAULT PRIVILEGES FOR ROLE %%I IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO %%s',
        c_role, v_to);
END
$restore_shell_role$;
"""


def _checked(name: str) -> str:
    if not _ROLE_NAME.match(name):
        raise ValueError(f"角色名 {name!r} 不是可直接嵌入 SQL 的小写标识符")
    return name


def _upgrade_sql(role: str = TARGET_ROLE) -> str:
    return _UPGRADE_TEMPLATE % {"role": _checked(role)}


def _downgrade_sql(role: str = TARGET_ROLE, grantees: tuple[str, ...] = GRANTEES) -> str:
    quoted = ", ".join(f"'{_checked(name)}'" for name in grantees)
    return _DOWNGRADE_TEMPLATE % {"role": _checked(role), "grantees": quoted}


def _execute_verbatim(connection, sql: str) -> None:
    """与既有 revision 同型：不走 ``op.execute()``，避免空参数集触发插值模式。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _upgrade_sql())


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _downgrade_sql())
