"""迁移 ``0096_plpgsql_search_path`` 的真库断言：全部固定、往返与权限收回。

三组事实各自只能在真库上证明：

1. **全部固定**：``public`` 下每一个 PL/pgSQL 函数的 ``proconfig`` 都含
   ``search_path=pg_catalog, pg_temp``，21 个名字逐个断言、再加一条「一个都不缺」的
   全称断言——只数个数的话，新加一个没固定的函数照样绿。五个重定义函数的触发器绑定、
   属主、SECURITY 属性与 0095 时一致，未限定的 7 处表引用全部带上 ``public.``。
2. **往返**：``upgrade head → downgrade -1 → upgrade head`` 在有数据的库上跑，每张表
   行数不变，触发器正负例在三个时点表现一致；直接重放两段 SQL 各两遍不报错。
3. **权限收回**：模拟托管平台的角色与默认权限形态，清理函数对 ``anon`` /
   ``authenticated`` 的直接 EXECUTE 与创建者默认权限里的两项被撤，``service_role`` /
   ``lingxi_scheduler`` / 属主保留；执行者是非超级用户、对属主只有 ADMIN 没有 SET 时，
   临时授予在同一段里精确收回，成员关系前后逐条相同。

隔离正负例（搜索路径被改成恶意 schema 时函数仍访问 ``public`` 对象）不在本文件，
由另一组用例覆盖。全部用例只动自己新建的探针库与探针角色，不碰共享测试库；
托管平台角色名（``anon`` 等）是集群级对象，谁建谁清。数据全部为虚构化名。
"""

from __future__ import annotations

import ast
import logging
import os
import unittest
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from postgres_schema import (
    ALEMBIC_INI,
    VERSIONS_DIRECTORY,
    ensure_production_schema,
    psycopg_available,
)

SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，函数搜索路径断言未验证（需真实 PostgreSQL 16）"
    if not os.environ.get("LINGXI_POSTGRES_DSN")
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，函数搜索路径断言未验证"
)

REVISION_FILE = VERSIONS_DIRECTORY / "0096_plpgsql_search_path.py"
HEAD_REVISION = "0096_plpgsql_search_path"
PREVIOUS_REVISION = "0095_contact_reachability"
PINNED_SEARCH_PATH = "search_path=pg_catalog, pg_temp"
CLEANUP_FUNCTION = "public.lingxi_retention_cleanup(timestamptz, integer)"
RETENTION_WINDOW = timedelta(hours=2160)

# 21 个函数逐个点名：19 个由 0096 固定，2 个（清理函数与删除防线）由 0054 建时即固定。
ALTER_ONLY_FUNCTIONS = (
    "feishu_org_sync_run_fix_expiry",
    "galaxy_import_batch_fix_expiry",
    "inbound_event_fix_expiry",
    "task_freeze_invariants",
    "queue_failure_notice_fix_expiry",
    "task_delivery_event_fix_expiry",
    "publish_outbox_fix_expiry",
    "mcp_access_token_immutable",
    "mcp_sync_check_fix_expiry",
    "onboarding_completion_notice_fix_expiry",
    "innertest_content_capture_fix_expiry",
    "task_document_delivery_request_fix_expiry",
    "outreach_message_freeze_anchors",
    "pending_action_fix_retention_expiry",
)
REDEFINED_FUNCTIONS = (
    "app_user_reject_delegated_subject",
    "credential_reject_app_user_subject",
    "feishu_org_sync_run_verify_children",
    "app_user_record_real_inbound",
    "app_user_adopt_prior_inbound",
)
ALREADY_PINNED_FUNCTIONS = ("lingxi_reject_premature_delete", "lingxi_retention_cleanup")
ALL_PLPGSQL_FUNCTIONS = ALTER_ONLY_FUNCTIONS + REDEFINED_FUNCTIONS + ALREADY_PINNED_FUNCTIONS

# (函数名, 必须出现的限定引用, 不得再出现的未限定引用)。7 处逐条列出。
QUALIFIED_REFERENCES = (
    (
        "app_user_reject_delegated_subject",
        "FROM public.feishu_delegated_subject",
        "FROM feishu_delegated_subject",
    ),
    ("credential_reject_app_user_subject", "FROM public.app_user WHERE", "FROM app_user WHERE"),
    (
        "feishu_org_sync_run_verify_children",
        "FROM public.feishu_org_tenant_snapshot",
        "FROM feishu_org_tenant_snapshot",
    ),
    (
        "feishu_org_sync_run_verify_children",
        "FROM public.feishu_org_member_snapshot",
        "FROM feishu_org_member_snapshot",
    ),
    ("app_user_record_real_inbound", "UPDATE public.app_user SET", "UPDATE app_user SET"),
    ("app_user_adopt_prior_inbound", "FROM public.inbound_event WHERE", "FROM inbound_event WHERE"),
    ("app_user_adopt_prior_inbound", "UPDATE public.app_user SET", "UPDATE app_user SET"),
)

# 托管平台的三个角色名是迁移里写死的对象，测试只能用同名角色，建与清都在本文件。
PLATFORM_ROLES = ("anon", "authenticated", "service_role")

FUNCTION_STATE_SQL = """
SELECT p.proname,
       pg_get_userbyid(p.proowner),
       p.prosecdef,
       p.provolatile,
       p.proleakproof,
       p.prorettype::regtype::text,
       coalesce(p.proacl::text, ''),
       coalesce((SELECT string_agg(
                     format('%%s|%%s|%%s|%%s|%%s|%%s', t.tgname, t.tgrelid::regclass, t.tgtype,
                            t.tgenabled, t.tgdeferrable, t.tginitdeferred)
                     , ';' ORDER BY t.tgname)
                   FROM pg_trigger t WHERE t.tgfoid = p.oid AND NOT t.tgisinternal), '')
  FROM pg_proc p
 WHERE p.pronamespace = 'public'::regnamespace AND p.proname = ANY(%s)
 ORDER BY p.proname
"""

MEMBERSHIP_SQL = """
SELECT am.roleid::regrole::text, am.member::regrole::text, am.grantor::regrole::text,
       am.admin_option, am.inherit_option, am.set_option
  FROM pg_auth_members am
 WHERE am.roleid = %s::regrole OR am.member = %s::regrole
 ORDER BY 1, 2, 3
"""


def revision_sql() -> tuple[str, str]:
    """静态取 0096 里两段 SQL 的最终文本（与 ``postgres_schema.revision_sql`` 同型）。

    0096 的两段 SQL 由模块顶层的函数拼出来，不是字面常量；这里在受限命名空间里
    执行模块源码（``alembic.op`` 用占位对象替代），拿到 ``_UPGRADE_SQL`` /
    ``_DOWNGRADE_SQL``，既不需要 alembic 也不会跑到 ``upgrade()`` 本身。
    """

    source = REVISION_FILE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(REVISION_FILE))
    body = [
        node
        for node in tree.body
        if not (isinstance(node, ast.ImportFrom) and node.module == "alembic")
    ]
    namespace: dict[str, Any] = {"__name__": "revision_0096_static", "op": None}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(REVISION_FILE), "exec"), namespace)
    return namespace["_UPGRADE_SQL"], namespace["_DOWNGRADE_SQL"]


def _run_alembic(dsn: str, action: str, target: str) -> None:
    """进程内跑 ``alembic <upgrade|downgrade> <target>``，进出各存取一次 logger 的 disabled 位。

    与 ``postgres_schema.alembic_upgrade_head`` 同一个理由：``env.py`` 的 ``fileConfig``
    会禁用当时已存在的全部 logger，跨用例污染别的模块的 ``assertLogs``。
    """

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
class SearchPathPostgresTestCase(unittest.TestCase):
    """共同底座：连接、探针库与探针角色的建与清。"""

    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        cls._psycopg = psycopg
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]
        ensure_production_schema(cls._dsn)

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

    def database_dsn(self, database: str, user: str | None = None) -> str:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(self._dsn)
        netloc = parts.netloc
        if user is not None:
            host_part = netloc.rsplit("@", 1)[-1]
            netloc = f"{user}@{host_part}"
        return urlunsplit(parts._replace(netloc=netloc, path=f"/{database}"))

    def create_probe_database(self, owner: str | None = None) -> str:
        """新建一次性探针库并登记清理；返回库名。"""

        name = f"lingxi_sp_probe_{uuid.uuid4().hex[:8]}"
        owner_clause = f" OWNER {owner}" if owner else ""
        self.admin(f"CREATE DATABASE {name}{owner_clause}")
        self.addCleanup(self.admin, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        return name

    @contextmanager
    def platform_roles(self):
        """建出托管平台的三个角色，用完连同它们在探针库里的权限一起清掉。

        角色是集群级对象：本用例之外若已存在同名角色，说明上一次没清干净或环境
        本身就是托管形态，两种情况都不该由本测试静默接管，直接失败点名。
        """

        for role in PLATFORM_ROLES:
            self.assertIsNone(
                self.scalar("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)),
                f"集群里已经存在角色 {role}，本测试拒绝接管别人的角色",
            )
            self.admin(f"CREATE ROLE {role} NOLOGIN")
        try:
            yield
        finally:
            # 探针库由各自的 addCleanup 删除，但 addCleanup 在本上下文退出**之后**才跑，
            # 而 DROP ROLE 要求该角色在任何库里都没有权限依赖。因此这里先把角色在
            # 所有探针库里的权限清掉，再删角色。
            for (database,) in self.admin(
                "SELECT datname FROM pg_database WHERE datname LIKE 'lingxi_sp_probe_%%'"
            ):
                for role in PLATFORM_ROLES:
                    self.admin(f"DROP OWNED BY {role}", dsn=self.database_dsn(database))
            for role in PLATFORM_ROLES:
                self.admin(f"DROP ROLE IF EXISTS {role}")

    def function_states(self, dsn: str, names: tuple[str, ...]) -> list[tuple]:
        return self.admin(FUNCTION_STATE_SQL, (list(names),), dsn)

    def proconfig(self, dsn: str, name: str) -> list[str] | None:
        return self.scalar(
            "SELECT proconfig FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND proname = %s",
            (name,),
            dsn,
        )

    def unpinned_functions(self, dsn: str) -> list[str]:
        return [
            row[0]
            for row in self.admin(
                "SELECT p.proname FROM pg_proc p JOIN pg_language l ON l.oid = p.prolang"
                " WHERE p.pronamespace = 'public'::regnamespace AND l.lanname = 'plpgsql'"
                "   AND NOT coalesce(%s = ANY(p.proconfig), false) ORDER BY 1",
                (PINNED_SEARCH_PATH,),
                dsn,
            )
        ]

    def cleanup_acl_grantees(self, dsn: str) -> dict[str, str]:
        """清理函数直接 ACL 里 EXECUTE 的 {被授权者: 授予方}。"""

        return {
            grantee: grantor
            for grantee, grantor in self.admin(
                "SELECT a.grantee::regrole::text, a.grantor::regrole::text"
                "  FROM pg_proc p, aclexplode(p.proacl) a"
                " WHERE p.oid = %s::regprocedure AND a.privilege_type = 'EXECUTE'",
                (CLEANUP_FUNCTION,),
                dsn,
            )
        }

    def default_function_acl(self, dsn: str, creator: str) -> dict[str, str]:
        """创建者在 public 对 FUNCTIONS 的默认权限里 EXECUTE 的 {被授权者: 授予方}。"""

        return {
            grantee: grantor
            for grantee, grantor in self.admin(
                "SELECT a.grantee::regrole::text, a.grantor::regrole::text"
                "  FROM pg_default_acl d, aclexplode(d.defaclacl) a"
                " WHERE d.defaclrole = %s::regrole AND d.defaclnamespace = 'public'::regnamespace"
                "   AND d.defaclobjtype = 'f' AND a.privilege_type = 'EXECUTE'",
                (creator,),
                dsn,
            )
        }

    def effective_execute(self, dsn: str, role: str) -> bool:
        return bool(
            self.scalar(
                "SELECT has_function_privilege(%s, %s::regprocedure, 'EXECUTE')",
                (role, CLEANUP_FUNCTION),
                dsn,
            )
        )

    def memberships(self, owner: str, member: str) -> list[tuple]:
        return self.admin(MEMBERSHIP_SQL, (owner, member))


class EveryFunctionIsPinnedTest(SearchPathPostgresTestCase):
    """验收 E-2 ①②：全部固定、七处引用限定、重定义函数的绑定与属性不变。"""

    def test_each_of_the_twenty_one_functions_pins_the_search_path(self) -> None:
        for name in ALL_PLPGSQL_FUNCTIONS:
            with self.subTest(function=name):
                config = self.proconfig(self._dsn, name)
                self.assertIsNotNone(config, f"{name} 不存在或 proconfig 为空")
                self.assertIn(PINNED_SEARCH_PATH, config)

    def test_no_plpgsql_function_in_public_is_left_unpinned(self) -> None:
        """全称断言：以后新加一个没固定的函数，这条会变红（上一条只认名单）。"""

        self.assertEqual(self.unpinned_functions(self._dsn), [])

    def test_the_name_list_is_the_whole_population(self) -> None:
        """名单与库里的 PL/pgSQL 函数集合逐个相等：多一个或少一个都要更新名单。"""

        actual = {
            row[0]
            for row in self.admin(
                "SELECT p.proname FROM pg_proc p JOIN pg_language l ON l.oid = p.prolang"
                " WHERE p.pronamespace = 'public'::regnamespace AND l.lanname = 'plpgsql'"
            )
        }
        self.assertEqual(actual, set(ALL_PLPGSQL_FUNCTIONS))
        self.assertEqual(len(ALL_PLPGSQL_FUNCTIONS), 21)

    def test_the_seven_table_references_are_schema_qualified(self) -> None:
        for name, qualified, unqualified in QUALIFIED_REFERENCES:
            with self.subTest(function=name, reference=qualified):
                source = self.scalar(
                    "SELECT prosrc FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND proname = %s",
                    (name,),
                )
                self.assertIn(qualified, source)
                self.assertNotIn(unqualified, source)

    def test_redefined_functions_keep_binding_owner_and_security_attributes(self) -> None:
        """五个重定义函数的触发器绑定、属主、SECURITY DEFINER 等与 0095 时逐项相同。

        对照值不是手抄的：在探针库里 ``downgrade`` 到 0095 读一遍，再 ``upgrade`` 回来
        读一遍，两份逐项比对。``proacl`` 也在其中——CREATE OR REPLACE 必须保住权限。
        """

        probe = self.create_probe_database()
        probe_dsn = self.database_dsn(probe)
        _run_alembic(probe_dsn, "upgrade", PREVIOUS_REVISION)
        before = self.function_states(probe_dsn, REDEFINED_FUNCTIONS)
        self.assertEqual(len(before), len(REDEFINED_FUNCTIONS))
        for row in before:
            self.assertNotEqual(row[7], "", f"{row[0]} 在 0095 时就应当绑定着触发器")

        _run_alembic(probe_dsn, "upgrade", HEAD_REVISION)
        after = self.function_states(probe_dsn, REDEFINED_FUNCTIONS)
        self.assertEqual(after, before)
        self.assertEqual(self.unpinned_functions(probe_dsn), [])

    def test_the_two_already_pinned_functions_are_not_touched_by_this_revision(self) -> None:
        upgrade_sql, downgrade_sql = revision_sql()
        for name in ALREADY_PINNED_FUNCTIONS:
            with self.subTest(function=name):
                self.assertNotIn(f"FUNCTION public.{name}(", upgrade_sql)
                self.assertNotIn(f"FUNCTION public.{name}(", downgrade_sql)


class RoundTripTest(SearchPathPostgresTestCase):
    """验收 E-2 ⑤⑨：有数据的库上往返，行数与触发器行为在三个时点相同。"""

    def _seed(self, dsn: str) -> None:
        now = datetime.now(UTC)
        self.admin(
            "INSERT INTO feishu_delegated_subject (purpose, subject_open_id)"
            " VALUES ('org_directory_sync', 'ou_probe_delegated')",
            dsn=dsn,
        )
        # 先有入站、后建档：建档触发器认领此前的入站（0095 的第二个触发器）。
        self.admin(
            "INSERT INTO inbound_event (feishu_event_id, received_at, event_type, user_open_id, trace_id)"
            " VALUES ('evt_probe_early', %s, 'im.message.receive_v1', 'ou_probe_user', 'trace_probe_1')",
            (now - timedelta(minutes=5),),
            dsn,
        )
        self.admin(
            "INSERT INTO app_user (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name,"
            " department, tenant_key, provisioning_state)"
            " VALUES ('usr_probe', 'ou_probe_user', 'u_probe', 'on_probe', '化名·探针', '合成部门',"
            " 'tenant_probe', 'active')",
            dsn=dsn,
        )
        self.admin(
            "INSERT INTO conversation (id, user_id, feishu_chat_id) VALUES ('conv_probe', 'usr_probe', 'oc_probe')",
            dsn=dsn,
        )
        self.admin(
            "INSERT INTO task (id, conversation_id, user_id, inbound_event_id, prompt, target_worker_version)"
            " VALUES ('task_probe', 'conv_probe', 'usr_probe', 'evt_probe_early', '化名问题', 'w1')",
            dsn=dsn,
        )
        self.admin(
            "INSERT INTO pending_action (id, action_type, target_open_id, target_state_snapshot,"
            " initiated_by_open_id, confirm_deadline_at)"
            " VALUES ('pa_probe', 'suspend_user', 'ou_probe_target', '{}', 'ou_probe_admin', %s)",
            (now + timedelta(hours=1),),
            dsn,
        )
        self.admin(
            "INSERT INTO galaxy_import_batch (id, source_label, source_digest, status)"
            " VALUES ('gib_probe', '合成来源', 'digest_probe', 'complete')",
            dsn=dsn,
        )

    def _row_counts(self, dsn: str) -> dict[str, int]:
        tables = [
            row[0]
            for row in self.admin(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                "   AND tablename <> 'alembic_version' ORDER BY 1",
                dsn=dsn,
            )
        ]
        return {
            table: int(self.scalar(f'SELECT count(*) FROM public."{table}"', dsn=dsn))
            for table in tables
        }

    def _trigger_behaviour(self, dsn: str, marker: str) -> dict[str, Any]:
        """同一组正负例在每个时点各跑一遍，返回可比较的观察值。"""

        from psycopg import errors

        observed: dict[str, Any] = {}
        # task_freeze_invariants：状态推进放行；改创建时间拒绝。
        self.admin("UPDATE task SET status = 'running' WHERE id = 'task_probe'", dsn=dsn)
        self.admin("UPDATE task SET status = 'queued' WHERE id = 'task_probe'", dsn=dsn)
        observed["task_status_update"] = self.scalar(
            "SELECT status FROM task WHERE id = 'task_probe'", dsn=dsn
        )
        with self.assertRaises(errors.RaiseException) as raised:
            self.admin(
                "UPDATE task SET created_at = created_at - interval '1 day' WHERE id = 'task_probe'",
                dsn=dsn,
            )
        observed["task_created_at_rejected"] = "不允许修改任务的创建时间" in str(raised.exception)

        # pending_action_fix_retention_expiry：新增行的保留到期 = created_at + 2160h；改 created_at 拒绝。
        created_at = datetime.now(UTC) - timedelta(days=3)
        row_id = f"pa_{marker}"
        self.admin(
            "INSERT INTO pending_action (id, action_type, target_open_id, target_state_snapshot,"
            " initiated_by_open_id, confirm_deadline_at, created_at, retention_expires_at)"
            " VALUES (%s, 'resume_user', %s, '{}', 'ou_probe_admin', %s, %s, %s)",
            (
                row_id,
                f"ou_{marker}",
                created_at + timedelta(hours=1),
                created_at,
                created_at + timedelta(days=400),
            ),
            dsn,
        )
        expires = self.scalar(
            "SELECT retention_expires_at FROM pending_action WHERE id = %s", (row_id,), dsn
        )
        observed["pending_action_expiry_is_derived"] = expires == created_at + RETENTION_WINDOW
        with self.assertRaises(errors.RaiseException) as raised:
            self.admin(
                "UPDATE pending_action SET created_at = created_at - interval '1 day' WHERE id = %s",
                (row_id,),
                dsn,
            )
        observed["pending_action_created_at_rejected"] = "不允许修改待确认操作的创建时间" in str(
            raised.exception
        )
        self.admin("DELETE FROM pending_action WHERE id = %s", (row_id,), dsn)

        # 0095 的入站触发器（重定义过的那两个之一）：新入站推进 last_inbound_at。
        received = datetime.now(UTC)
        self.admin(
            "INSERT INTO inbound_event (feishu_event_id, received_at, event_type, user_open_id, trace_id)"
            " VALUES (%s, %s, 'im.message.receive_v1', 'ou_probe_user', %s)",
            (f"evt_{marker}", received, f"trace_{marker}"),
            dsn,
        )
        observed["last_inbound_advanced"] = (
            self.scalar("SELECT last_inbound_at FROM app_user WHERE id = 'usr_probe'", dsn=dsn)
            == received
        )
        self.admin("DELETE FROM inbound_event WHERE feishu_event_id = %s", (f"evt_{marker}",), dsn)

        # 身份双向防线（重定义过的另两个）：专用授权主体不能建成用户；用户不能成为主体。
        with self.assertRaises(errors.RaiseException) as raised:
            self.admin(
                "INSERT INTO app_user (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name,"
                " department, tenant_key) VALUES (%s, 'ou_probe_delegated', 'u_x', 'on_x', '化名', '部门', 't')",
                (f"usr_{marker}",),
                dsn,
            )
        observed["delegated_subject_rejected_as_user"] = "专用授权账号不能被建成用户记录" in str(
            raised.exception
        )
        with self.assertRaises(errors.RaiseException) as raised:
            self.admin(
                "UPDATE feishu_delegated_subject SET subject_open_id = 'ou_probe_user'"
                " WHERE purpose = 'org_directory_sync'",
                dsn=dsn,
            )
        observed["user_rejected_as_delegated_subject"] = "该 open_id 已是员工用户记录" in str(
            raised.exception
        )
        return observed

    def test_upgrade_downgrade_upgrade_keeps_rows_and_trigger_behaviour(self) -> None:
        probe = self.create_probe_database()
        probe_dsn = self.database_dsn(probe)
        _run_alembic(probe_dsn, "upgrade", HEAD_REVISION)
        self._seed(probe_dsn)
        # 建档触发器认领了更早的入站：first_inbound_at 来自那条事件，不是 NULL。
        self.assertIsNotNone(
            self.scalar(
                "SELECT first_inbound_at FROM app_user WHERE id = 'usr_probe'", dsn=probe_dsn
            )
        )

        counts_at_head = self._row_counts(probe_dsn)
        self.assertGreater(sum(counts_at_head.values()), 0)
        behaviour_at_head = self._trigger_behaviour(probe_dsn, "head1")
        self.assertTrue(all(behaviour_at_head.values()), behaviour_at_head)

        _run_alembic(probe_dsn, "downgrade", "-1")
        self.assertEqual(
            self.scalar("SELECT version_num FROM alembic_version", dsn=probe_dsn), PREVIOUS_REVISION
        )
        self.assertEqual(self._row_counts(probe_dsn), counts_at_head)
        self.assertEqual(self._trigger_behaviour(probe_dsn, "down"), behaviour_at_head)
        self.assertEqual(len(self.unpinned_functions(probe_dsn)), 19)

        _run_alembic(probe_dsn, "upgrade", HEAD_REVISION)
        self.assertEqual(
            self.scalar("SELECT version_num FROM alembic_version", dsn=probe_dsn), HEAD_REVISION
        )
        self.assertEqual(self._row_counts(probe_dsn), counts_at_head)
        self.assertEqual(self._trigger_behaviour(probe_dsn, "head2"), behaviour_at_head)
        self.assertEqual(self.unpinned_functions(probe_dsn), [])

    def test_replaying_either_half_twice_is_harmless(self) -> None:
        """两段 SQL 各自重放两遍都不报错，且落到与只跑一遍相同的状态。"""

        upgrade_sql, downgrade_sql = revision_sql()
        probe = self.create_probe_database()
        probe_dsn = self.database_dsn(probe)
        _run_alembic(probe_dsn, "upgrade", HEAD_REVISION)
        at_head = self.function_states(probe_dsn, ALL_PLPGSQL_FUNCTIONS)

        for _ in range(2):
            self.admin(upgrade_sql, dsn=probe_dsn)
        self.assertEqual(self.function_states(probe_dsn, ALL_PLPGSQL_FUNCTIONS), at_head)
        self.assertEqual(self.unpinned_functions(probe_dsn), [])

        for _ in range(2):
            self.admin(downgrade_sql, dsn=probe_dsn)
        self.assertEqual(len(self.unpinned_functions(probe_dsn)), 19)
        self.admin(upgrade_sql, dsn=probe_dsn)
        self.assertEqual(self.function_states(probe_dsn, ALL_PLPGSQL_FUNCTIONS), at_head)


class HostedPlatformPrivilegesTest(SearchPathPostgresTestCase):
    """验收 E-2 ③⑩：托管平台形态下的撤权、保留、恢复与临时授予的精确收回。"""

    def _hosted_probe(self, owner: str | None = None) -> str:
        """探针库先照托管平台设好创建者默认权限，再建到 0095——清理函数因此自带三条 EXECUTE。"""

        probe = self.create_probe_database(owner)
        creator = self.scalar("SELECT current_user")
        self.admin(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {creator} IN SCHEMA public"
            " GRANT EXECUTE ON FUNCTIONS TO anon, authenticated, service_role",
            dsn=self.database_dsn(probe),
        )
        return probe

    def test_anonymous_roles_lose_execute_while_service_and_scheduler_keep_it(self) -> None:
        creator = self.scalar("SELECT current_user")
        if creator != "postgres":
            self.skipTest("迁移只调整创建者 postgres 的默认权限；当前测试身份不是 postgres")
        with self.platform_roles():
            probe = self._hosted_probe()
            probe_dsn = self.database_dsn(probe)
            _run_alembic(probe_dsn, "upgrade", PREVIOUS_REVISION)

            seeded = self.cleanup_acl_grantees(probe_dsn)
            self.assertEqual(
                set(seeded),
                {
                    "anon",
                    "authenticated",
                    "service_role",
                    "lingxi_scheduler",
                    "lingxi_retention_owner",
                },
                "0095 时的形态必须与托管平台一致：两个平台角色各持一条直接 EXECUTE",
            )
            self.assertEqual(
                set(self.default_function_acl(probe_dsn, "postgres")), set(PLATFORM_ROLES)
            )
            trigger_acls_before = [
                row[6]
                for row in self.function_states(
                    probe_dsn, REDEFINED_FUNCTIONS + ALTER_ONLY_FUNCTIONS
                )
            ]

            _run_alembic(probe_dsn, "upgrade", HEAD_REVISION)
            after = self.cleanup_acl_grantees(probe_dsn)
            self.assertEqual(
                set(after), {"service_role", "lingxi_scheduler", "lingxi_retention_owner"}
            )
            self.assertEqual(set(after.values()), {"lingxi_retention_owner"}, "保留项的授予方不变")
            for role in ("anon", "authenticated"):
                self.assertFalse(
                    self.effective_execute(probe_dsn, role), f"{role} 的有效 EXECUTE 必须为假"
                )
            for role in ("service_role", "lingxi_scheduler", "lingxi_retention_owner"):
                self.assertTrue(
                    self.effective_execute(probe_dsn, role), f"{role} 的合法调用必须保留"
                )
            self.assertEqual(
                self.default_function_acl(probe_dsn, "postgres"), {"service_role": "postgres"}
            )
            # 19 个触发器函数自己的 ACL 一条不动（它们也因默认权限带着三个平台角色的 EXECUTE）。
            self.assertEqual(
                [
                    row[6]
                    for row in self.function_states(
                        probe_dsn, REDEFINED_FUNCTIONS + ALTER_ONLY_FUNCTIONS
                    )
                ],
                trigger_acls_before,
            )

            _run_alembic(probe_dsn, "downgrade", "-1")
            restored = self.cleanup_acl_grantees(probe_dsn)
            self.assertEqual(set(restored), set(seeded))
            self.assertEqual(restored["anon"], "lingxi_retention_owner", "按原授予方（属主）补回")
            self.assertEqual(restored["authenticated"], "lingxi_retention_owner")
            self.assertEqual(
                self.default_function_acl(probe_dsn, "postgres"),
                {"anon": "postgres", "authenticated": "postgres", "service_role": "postgres"},
            )

            _run_alembic(probe_dsn, "upgrade", HEAD_REVISION)
            self.assertEqual(set(self.cleanup_acl_grantees(probe_dsn)), set(after))

    def test_a_non_superuser_executor_returns_the_borrowed_set_role_exactly(self) -> None:
        """执行者对属主只有 ADMIN 没有 SET（生产形态）：借 SET、撤权、还回，成员关系逐条不变。

        必须换身份跑：超级用户对任何角色都能 SET ROLE，整段「借与还」在超级用户会话里
        根本不执行，断言恒绿。探针角色按 0054 的用例先例建：``NOSUPERUSER CREATEROLE``、
        拥有自己的库、整条链以它的身份前滚，于是 19 个函数都归它所有。
        """

        suffix = uuid.uuid4().hex[:8]
        probe_role = f"sp_probe_{suffix}"
        self.admin(f"CREATE ROLE {probe_role} NOSUPERUSER CREATEROLE LOGIN")

        def drop_probe_role() -> None:
            # 迁移 0054 会以探针身份把属主角色授给 lingxi_migrate，那条成员关系不随库消失。
            self.admin(
                f"REVOKE lingxi_retention_owner FROM lingxi_migrate GRANTED BY {probe_role} CASCADE"
            )
            self.admin(f"REVOKE lingxi_retention_owner FROM {probe_role} CASCADE")
            self.admin(f"DROP ROLE IF EXISTS {probe_role}")

        self.addCleanup(drop_probe_role)
        # ADMIN 有、SET 无：正是 #661 记录的生产迁移身份对属主的关系。
        self.admin(
            f"GRANT lingxi_retention_owner TO {probe_role} WITH ADMIN TRUE, INHERIT FALSE, SET FALSE"
        )

        with self.platform_roles():
            probe = self.create_probe_database(owner=probe_role)
            probe_dsn = self.database_dsn(probe, user=probe_role)
            _run_alembic(probe_dsn, "upgrade", PREVIOUS_REVISION)
            # 平台默认权限在这个库里不是 postgres 建的，两条直接 EXECUTE 用属主身份显式种上。
            self.admin(
                "SET ROLE lingxi_retention_owner;"
                f" GRANT EXECUTE ON FUNCTION {CLEANUP_FUNCTION} TO anon, authenticated;"
                " RESET ROLE",
                dsn=self.database_dsn(probe),
            )
            self.assertEqual(
                set(self.cleanup_acl_grantees(probe_dsn)),
                {"anon", "authenticated", "lingxi_scheduler", "lingxi_retention_owner"},
            )
            self.assertFalse(
                self.scalar(
                    "SELECT pg_has_role(%s, 'lingxi_retention_owner', 'SET')", (probe_role,)
                ),
                "前提：探针不能 SET ROLE 到属主，否则借还分支不会执行",
            )

            before = self.memberships("lingxi_retention_owner", probe_role)
            _run_alembic(probe_dsn, "upgrade", HEAD_REVISION)
            self.assertEqual(self.memberships("lingxi_retention_owner", probe_role), before)
            self.assertNotIn(
                ("lingxi_retention_owner", probe_role, probe_role),
                {row[:3] for row in self.memberships("lingxi_retention_owner", probe_role)},
                "借来的 SET 必须按授予方精确撤掉，不得留下自己授给自己的那一条",
            )
            self.assertEqual(
                set(self.cleanup_acl_grantees(probe_dsn)),
                {"lingxi_scheduler", "lingxi_retention_owner"},
            )
            for role in ("anon", "authenticated"):
                self.assertFalse(self.effective_execute(probe_dsn, role))
            self.assertEqual(self.unpinned_functions(probe_dsn), [])
            self.assertEqual(
                {
                    row[1]
                    for row in self.function_states(
                        probe_dsn, REDEFINED_FUNCTIONS + ALTER_ONLY_FUNCTIONS
                    )
                },
                {probe_role},
                "19 个函数仍归探针所有：重定义没有换属主",
            )

            _run_alembic(probe_dsn, "downgrade", "-1")
            self.assertEqual(self.memberships("lingxi_retention_owner", probe_role), before)
            restored = self.cleanup_acl_grantees(probe_dsn)
            self.assertEqual(
                set(restored),
                {"anon", "authenticated", "lingxi_scheduler", "lingxi_retention_owner"},
            )
            self.assertEqual(
                {restored["anon"], restored["authenticated"]}, {"lingxi_retention_owner"}
            )

            _run_alembic(probe_dsn, "upgrade", HEAD_REVISION)
            self.assertEqual(self.memberships("lingxi_retention_owner", probe_role), before)
            self.assertEqual(
                set(self.cleanup_acl_grantees(probe_dsn)),
                {"lingxi_scheduler", "lingxi_retention_owner"},
            )

    def test_a_revoke_that_does_not_take_effect_fails_loudly(self) -> None:
        """回读有效权限而不信退出码：撤不掉时迁移必须失败，不能静默留下匿名调用权。

        构造方式：让 ``anon`` 经成员关系从 ``service_role`` 继承 EXECUTE。直接 ACL 撤掉后
        有效权限仍为真，迁移应当以「仍是 true」失败并整体回滚——ACL 与版本号都不变。
        """

        from psycopg import errors

        creator = self.scalar("SELECT current_user")
        if creator != "postgres":
            self.skipTest("迁移只调整创建者 postgres 的默认权限；当前测试身份不是 postgres")
        with self.platform_roles():
            self.admin("GRANT service_role TO anon")  # 成员关系随 DROP ROLE 一起消失，不必单独撤
            probe = self._hosted_probe()
            probe_dsn = self.database_dsn(probe)
            _run_alembic(probe_dsn, "upgrade", PREVIOUS_REVISION)
            seeded = self.cleanup_acl_grantees(probe_dsn)

            with self.assertRaises(errors.RaiseException) as raised:
                _run_alembic(probe_dsn, "upgrade", HEAD_REVISION)
            self.assertIn("有效 EXECUTE 仍是", str(raised.exception))
            self.assertEqual(self.cleanup_acl_grantees(probe_dsn), seeded, "失败的迁移必须整体回滚")
            self.assertEqual(
                self.scalar("SELECT version_num FROM alembic_version", dsn=probe_dsn),
                PREVIOUS_REVISION,
            )
            self.assertEqual(len(self.unpinned_functions(probe_dsn)), 19)


class RevisionFileTest(unittest.TestCase):
    """不需要数据库的形状断言：revision 文件本身说了什么。"""

    def test_revision_ids_and_static_sql_shape(self) -> None:
        source = REVISION_FILE.read_text(encoding="utf-8")
        self.assertIn(f'revision: str = "{HEAD_REVISION}"', source)
        self.assertIn(f'down_revision: str | None = "{PREVIOUS_REVISION}"', source)
        upgrade_sql, downgrade_sql = revision_sql()
        for name in ALTER_ONLY_FUNCTIONS:
            self.assertIn(
                f"ALTER FUNCTION public.{name}() SET search_path = pg_catalog, pg_temp;",
                upgrade_sql,
            )
            self.assertIn(f"ALTER FUNCTION public.{name}() RESET search_path;", downgrade_sql)
        for name in REDEFINED_FUNCTIONS:
            self.assertIn(
                f"CREATE OR REPLACE FUNCTION public.{name}() RETURNS TRIGGER", upgrade_sql
            )
            self.assertIn(
                f"CREATE OR REPLACE FUNCTION public.{name}() RETURNS TRIGGER", downgrade_sql
            )
        self.assertEqual(upgrade_sql.count("SET search_path = pg_catalog, pg_temp"), 19)
        self.assertEqual(downgrade_sql.count("SET search_path = pg_catalog, pg_temp"), 0)
        self.assertIn("REVOKE EXECUTE ON FUNCTION", upgrade_sql)
        self.assertIn("GRANT EXECUTE ON FUNCTION", downgrade_sql)
        self.assertIn("GRANTED BY", upgrade_sql)
        self.assertIn("GRANTED BY", downgrade_sql)

    def test_the_file_exists_where_the_chain_expects_it(self) -> None:
        self.assertTrue(REVISION_FILE.is_file(), REVISION_FILE)
        self.assertIsInstance(Path(REVISION_FILE), Path)


if __name__ == "__main__":
    unittest.main()
