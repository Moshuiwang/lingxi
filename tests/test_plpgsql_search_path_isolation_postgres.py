"""迁移 ``0096_plpgsql_search_path`` 的隔离正负例：搜索路径被改成恶意 schema 时函数仍按 ``public`` 行事。

「固定 ``search_path`` + 表名加 ``public.`` 前缀」到底挡住了什么，只有真的注入一次才能证明。
同一组注入在三种定义上各跑一遍：

1. **正例（加固定义）**：库在链头，攻击者把会话 ``search_path`` 改成 ``evil, pg_catalog, public``
   并在 ``evil`` 里放同名假表 / 同签名假运算符，再用真实 INSERT / UPDATE 触发触发器——函数
   必须仍按 ``public`` 真表与 ``pg_catalog`` 运算符行事（该拒的拒、该写的写到真表）。
2. **负例（原定义）**：探针库 ``downgrade -1`` 回到 0095 的原文定义后做同一注入，函数**必须**
   被劫持（读到假表 / 用上假运算符）——这是证明测试能分辨的负例；随后 ``upgrade head``
   复原，``proconfig`` 回到固定值，同一注入再次失效。
3. **权限**：托管平台形态（``anon`` / ``authenticated`` / ``service_role`` 存在且创建者默认权限
   给它们函数 EXECUTE）下整链重建，清理函数对前两者的有效 EXECUTE 为假、``service_role``
   保留，默认权限里不再有前两者，成员关系前后一致。

14 个只用 NEW / OLD 与内置对象的函数里没有一个调用 ``now()`` 之类的具名函数；它们能被
搜索路径劫持的内置对象是**运算符**（``+``、``<>``，以及 ``IS DISTINCT FROM`` 底层的 ``=``），
因此对它们的注入用同签名的假运算符。临时表那一组**不改** ``search_path``：默认路径下
``pg_temp`` 隐含排在最前，只要能建临时表就能遮住未限定的表名——这正是 ``public.`` 前缀
单独挡住的那一格。数据全部为固定化名的合成数据。
"""

from __future__ import annotations

import os
import unittest
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from postgres_schema import force_rebuild_schema, psycopg_available, reset_production_rows
from test_plpgsql_search_path_postgres import (
    CLEANUP_FUNCTION,
    HEAD_REVISION,
    PINNED_SEARCH_PATH,
    PLATFORM_ROLES,
    PREVIOUS_REVISION,
    REDEFINED_FUNCTIONS,
    SearchPathPostgresTestCase,
    _run_alembic,
)

SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，函数搜索路径隔离断言未验证（需真实 PostgreSQL 16）"
    if not os.environ.get("LINGXI_POSTGRES_DSN")
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，函数搜索路径隔离断言未验证"
)

# 攻击者的会话搜索路径：evil 排在 pg_catalog 之前，表名、函数名、运算符都先到 evil 里找。
INJECTED_SEARCH_PATH = "evil, pg_catalog, public"
RETENTION_WINDOW = timedelta(hours=2160)
# 三个固定时刻：真表里的「刚才」、更早的一条真实入站、假对象刻意返回的远古时间。
RECEIVED = datetime(2026, 9, 11, 10, 0, tzinfo=UTC)
EARLIER = datetime(2026, 9, 11, 9, 0, tzinfo=UTC)
ANCIENT = datetime(2001, 1, 1, tzinfo=UTC)
CIPHER_ISSUED = "A" * 86 + "=="
CIPHER_OVERWRITE = "B" * 86 + "=="

# 假运算符三套：与真运算符签名逐位相同，才会按「路径里最靠前的那个」遮住 pg_catalog 的同名者。
# 文本 = 恒假：让「主体是否已存在」的 EXISTS 永远找不到。
EVIL_TEXT_NEVER_EQUAL_SQL = """
CREATE FUNCTION evil.text_never_equal(text, text) RETURNS boolean
    LANGUAGE sql IMMUTABLE AS 'SELECT false';
CREATE OPERATOR evil.= (LEFTARG = text, RIGHTARG = text, FUNCTION = evil.text_never_equal);
"""
# 时间 <> 恒假、时间 = 恒真（IS DISTINCT FROM 底层用 =）、时间 + 间隔返回远古：让冻结与到期推导失守。
EVIL_FREEZE_BYPASS_SQL = """
CREATE FUNCTION evil.ts_never_differs(timestamptz, timestamptz) RETURNS boolean
    LANGUAGE sql IMMUTABLE AS 'SELECT false';
CREATE OPERATOR evil.<> (LEFTARG = timestamptz, RIGHTARG = timestamptz,
                         FUNCTION = evil.ts_never_differs);
CREATE FUNCTION evil.ts_always_equal(timestamptz, timestamptz) RETURNS boolean
    LANGUAGE sql IMMUTABLE AS 'SELECT true';
CREATE OPERATOR evil.= (LEFTARG = timestamptz, RIGHTARG = timestamptz,
                        FUNCTION = evil.ts_always_equal);
CREATE FUNCTION evil.ts_plus_ancient(timestamptz, interval) RETURNS timestamptz
    LANGUAGE sql IMMUTABLE AS $$SELECT '2001-01-01 00:00:00+00'::timestamptz$$;
CREATE OPERATOR evil.+ (LEFTARG = timestamptz, RIGHTARG = interval,
                        FUNCTION = evil.ts_plus_ancient);
"""
# 文本 = 恒真：让「密文是否被改写」的 IS DISTINCT FROM 永远说没改。
EVIL_TEXT_ALWAYS_EQUAL_SQL = """
CREATE FUNCTION evil.text_always_equal(text, text) RETURNS boolean
    LANGUAGE sql IMMUTABLE AS 'SELECT true';
CREATE OPERATOR evil.= (LEFTARG = text, RIGHTARG = text, FUNCTION = evil.text_always_equal);
"""

INSERT_USER_SQL = (
    "INSERT INTO public.app_user (id, feishu_open_id, feishu_user_id, feishu_union_id,"
    " display_name, department, tenant_key, provisioning_state)"
    " VALUES (%s, %s, %s, %s, %s, '合成部门', 'tenant_probe', 'active')"
)
INSERT_INBOUND_SQL = (
    "INSERT INTO public.inbound_event (feishu_event_id, received_at, event_type, user_open_id,"
    " trace_id) VALUES (%s, %s, 'im.message.receive_v1', %s, %s)"
)
# 用真实语句触发的每个场景的两种预期：加固定义下「守住」、原定义下「被劫持」。
# 键是场景名（与本文件的 scenario_* 方法一一对应），值是场景返回的观察值字典。
DEFENDED: dict[str, dict[str, Any]] = {
    "reject_delegated_subject_fake_table": {
        "outcome": "rejected:专用授权账号不能被建成用户记录",
        "user_row_created": False,
    },
    "reject_delegated_subject_temp_table": {
        "outcome": "rejected:专用授权账号不能被建成用户记录",
        "user_row_created": False,
        "session_search_path_names_evil": False,
    },
    "reject_delegated_subject_operator": {
        "outcome": "rejected:专用授权账号不能被建成用户记录",
        "user_row_created": False,
    },
    "credential_reject_app_user_subject_fake_table": {
        "outcome": "rejected:该 open_id 已是员工用户记录，不能成为专用授权主体",
        "subject_now_is_user": False,
    },
    "verify_children_fake_tables": {
        "outcome": "rejected:完成批次 run_probe 的声明计数与实际子行不一致（租户 1/0、成员 1/0）",
        "complete_run_persisted": False,
    },
    "record_real_inbound_fake_table": {
        "outcome": "accepted",
        "public_last_inbound_at": RECEIVED,
        "evil_last_inbound_at": None,
    },
    "adopt_prior_inbound_fake_table": {
        "outcome": "accepted",
        "public_first_inbound_at": EARLIER,
    },
    "inbound_event_fix_expiry_operators": {
        "insert_expires_at": RECEIVED + RETENTION_WINDOW,
        "update_outcome": "rejected:不允许修改入站事件的接收时间",
        "received_at_after_update": RECEIVED,
    },
    "task_freeze_invariants_operators": {
        "insert_content_expires_at": RECEIVED + RETENTION_WINDOW,
        "update_outcome": "rejected:不允许修改任务的创建时间",
        "created_at_after_update": RECEIVED,
    },
    "pending_action_fix_retention_expiry_operators": {
        "insert_retention_expires_at": RECEIVED + RETENTION_WINDOW,
        "update_outcome": "rejected:不允许修改待确认操作的创建时间",
        "created_at_after_update": RECEIVED,
    },
    "mcp_access_token_immutable_operator": {
        "update_outcome": "rejected:不允许覆盖已签发的访问令牌密文",
        "cipher_after_update": CIPHER_ISSUED,
    },
}
HIJACKED: dict[str, dict[str, Any]] = {
    "reject_delegated_subject_fake_table": {"outcome": "accepted", "user_row_created": True},
    "reject_delegated_subject_temp_table": {
        "outcome": "accepted",
        "user_row_created": True,
        "session_search_path_names_evil": False,
    },
    "reject_delegated_subject_operator": {"outcome": "accepted", "user_row_created": True},
    "credential_reject_app_user_subject_fake_table": {
        "outcome": "accepted",
        "subject_now_is_user": True,
    },
    "verify_children_fake_tables": {"outcome": "accepted", "complete_run_persisted": True},
    "record_real_inbound_fake_table": {
        "outcome": "accepted",
        "public_last_inbound_at": None,
        "evil_last_inbound_at": RECEIVED,
    },
    "adopt_prior_inbound_fake_table": {"outcome": "accepted", "public_first_inbound_at": ANCIENT},
    "inbound_event_fix_expiry_operators": {
        "insert_expires_at": ANCIENT,
        "update_outcome": "accepted",
        "received_at_after_update": ANCIENT,
    },
    "task_freeze_invariants_operators": {
        "insert_content_expires_at": ANCIENT,
        "update_outcome": "accepted",
        "created_at_after_update": ANCIENT,
    },
    "pending_action_fix_retention_expiry_operators": {
        "insert_retention_expires_at": ANCIENT,
        "update_outcome": "accepted",
        "created_at_after_update": ANCIENT,
    },
    "mcp_access_token_immutable_operator": {
        "update_outcome": "accepted",
        "cipher_after_update": CIPHER_OVERWRITE,
    },
}


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN") and psycopg_available(), SKIP_REASON)
class InjectionScenarioTestCase(SearchPathPostgresTestCase):
    """注入场景的共同底座：一条被改了搜索路径的会话执行触发语句，观察值经干净连接读回。

    每个 ``scenario_*`` 自己造数、自己清场（``finally`` 里清掉触及的行并删掉 ``evil``），
    因此同一个库上可以按任意顺序连跑，正例类与负例类共用同一份场景代码。
    """

    def scenarios(self) -> list[tuple[str, Callable[[str], dict[str, Any]]]]:
        return [(name, getattr(self, f"scenario_{name}")) for name in DEFENDED]

    @contextmanager
    def injected_session(self, dsn: str, search_path: str | None = INJECTED_SEARCH_PATH):
        with (
            self._psycopg.connect(dsn, autocommit=True) as connection,
            connection.cursor() as cursor,
        ):
            if search_path is not None:
                cursor.execute(f"SET search_path = {search_path}")
            yield cursor

    def attempt(self, cursor: Any, sql: str, parameters: tuple = ()) -> str:
        """执行触发语句：触发器拒绝返回 ``rejected:<文案>``，落库返回 ``accepted``。

        只捕获 PL/pgSQL 的 RAISE；别的错误（例如表名解析失败）原样抛出，让用例以
        错误而不是以「被拒绝」收场——那正是限定被拿掉后该出现的响亮失败。
        """

        from psycopg import errors

        try:
            cursor.execute(sql, parameters or None)
        except errors.RaiseException as raised:
            return "rejected:" + str(raised).splitlines()[0]
        return "accepted"

    def clear(self, dsn: str) -> None:
        """清掉场景触及的行与 ``evil``：按外键子→父顺序 DELETE（毫秒级）。

        只有装了 DELETE 防线的组织快照批次表才 TRUNCATE，且只在原定义把假批次放进来时。
        TRUNCATE 要换 relfilenode，一次 CASCADE 实测近四秒，三十几个场景跑下来会把
        本模块抬到门禁超时的量级。
        """

        self.admin(
            "SET lock_timeout = '5s';"
            " DELETE FROM public.mcp_access_token; DELETE FROM public.task;"
            " DELETE FROM public.conversation; DELETE FROM public.pending_action;"
            " DELETE FROM public.inbound_event; DELETE FROM public.app_user;"
            " DELETE FROM public.feishu_delegated_subject;"
            " DROP SCHEMA IF EXISTS evil CASCADE",
            dsn=dsn,
        )
        if self.scalar("SELECT count(*) FROM public.feishu_org_sync_run", dsn=dsn):
            self.admin(
                "SET lock_timeout = '5s'; TRUNCATE public.feishu_org_sync_run CASCADE", dsn=dsn
            )

    def build_evil(self, dsn: str, *statements: str) -> None:
        self.admin("DROP SCHEMA IF EXISTS evil CASCADE; CREATE SCHEMA evil", dsn=dsn)
        for statement in statements:
            self.admin(statement, dsn=dsn)

    def shadow_table(self, dsn: str, table: str) -> None:
        """在 evil 里建一张与真表同列、无约束、无数据的假表。"""

        self.admin(
            f"CREATE TABLE evil.{table} AS SELECT * FROM public.{table} WITH NO DATA", dsn=dsn
        )

    def seed_delegated_subject(self, dsn: str) -> None:
        self.admin(
            "INSERT INTO public.feishu_delegated_subject (purpose, subject_open_id)"
            " VALUES ('org_directory_sync', 'ou_delegated_probe')",
            dsn=dsn,
        )

    def seed_user(self, dsn: str) -> None:
        self.admin(
            INSERT_USER_SQL, ("usr_probe", "ou_user_probe", "u_probe", "on_probe", "化名·探针"), dsn
        )

    def user_row_exists(self, dsn: str, user_id: str) -> bool:
        return bool(
            self.scalar("SELECT count(*) FROM public.app_user WHERE id = %s", (user_id,), dsn)
        )

    # ---- 五个重定义函数：假表 --------------------------------------------------

    def scenario_reject_delegated_subject_fake_table(self, dsn: str) -> dict[str, Any]:
        """``app_user_reject_delegated_subject``：evil 里的假主体表是空的，真表里主体存在。"""

        try:
            self.seed_delegated_subject(dsn)
            self.build_evil(dsn)
            self.shadow_table(dsn, "feishu_delegated_subject")
            with self.injected_session(dsn) as cursor:
                outcome = self.attempt(
                    cursor,
                    INSERT_USER_SQL,
                    ("usr_inject", "ou_delegated_probe", "u_inject", "on_inject", "化名·注入"),
                )
            return {"outcome": outcome, "user_row_created": self.user_row_exists(dsn, "usr_inject")}
        finally:
            self.clear(dsn)

    def scenario_reject_delegated_subject_temp_table(self, dsn: str) -> dict[str, Any]:
        """同一函数，但攻击者**不改**搜索路径，只建一张同名临时表。"""

        try:
            self.seed_delegated_subject(dsn)
            with self.injected_session(dsn, search_path=None) as cursor:
                cursor.execute(
                    "CREATE TEMP TABLE feishu_delegated_subject"
                    " AS SELECT * FROM public.feishu_delegated_subject WITH NO DATA"
                )
                cursor.execute("SHOW search_path")
                session_search_path = cursor.fetchone()[0]
                outcome = self.attempt(
                    cursor,
                    INSERT_USER_SQL,
                    ("usr_inject", "ou_delegated_probe", "u_inject", "on_inject", "化名·注入"),
                )
            return {
                "outcome": outcome,
                "user_row_created": self.user_row_exists(dsn, "usr_inject"),
                "session_search_path_names_evil": "evil" in session_search_path
                or "pg_temp" in session_search_path,
            }
        finally:
            self.clear(dsn)

    def scenario_reject_delegated_subject_operator(self, dsn: str) -> dict[str, Any]:
        """同一函数，表名不动，只把文本 ``=`` 换成恒假的假运算符。

        这一组与假表那一组分工不同：``public.`` 前缀挡假表，``SET search_path`` 挡假运算符。
        只删掉 SET 子句而保留前缀时，假表那一组照样绿，本组必红。
        """

        try:
            self.seed_delegated_subject(dsn)
            self.build_evil(dsn, EVIL_TEXT_NEVER_EQUAL_SQL)
            with self.injected_session(dsn) as cursor:
                outcome = self.attempt(
                    cursor,
                    INSERT_USER_SQL,
                    ("usr_inject", "ou_delegated_probe", "u_inject", "on_inject", "化名·注入"),
                )
            return {"outcome": outcome, "user_row_created": self.user_row_exists(dsn, "usr_inject")}
        finally:
            self.clear(dsn)

    def scenario_credential_reject_app_user_subject_fake_table(self, dsn: str) -> dict[str, Any]:
        """``credential_reject_app_user_subject``：evil 里的假用户表是空的，真表里员工存在。"""

        try:
            self.seed_user(dsn)
            self.build_evil(dsn)
            self.shadow_table(dsn, "app_user")
            with self.injected_session(dsn) as cursor:
                outcome = self.attempt(
                    cursor,
                    "INSERT INTO public.feishu_delegated_subject (purpose, subject_open_id)"
                    " VALUES ('org_directory_sync', 'ou_user_probe')",
                )
            subject = self.scalar(
                "SELECT subject_open_id FROM public.feishu_delegated_subject"
                " WHERE purpose = 'org_directory_sync'",
                dsn=dsn,
            )
            return {"outcome": outcome, "subject_now_is_user": subject == "ou_user_probe"}
        finally:
            self.clear(dsn)

    def scenario_verify_children_fake_tables(self, dsn: str) -> dict[str, Any]:
        """``feishu_org_sync_run_verify_children``：假子表各一行，真子表为空，声明计数 1/1。"""

        try:
            self.build_evil(dsn)
            self.shadow_table(dsn, "feishu_org_tenant_snapshot")
            self.shadow_table(dsn, "feishu_org_member_snapshot")
            self.admin(
                "INSERT INTO evil.feishu_org_tenant_snapshot"
                " (id, sync_run_id, tenant_key, visible_to_user_identity)"
                " VALUES ('tenant_fake', 'run_probe', 'tenant_probe', true);"
                " INSERT INTO evil.feishu_org_member_snapshot (id, sync_run_id, tenant_key,"
                " member_key, open_id, user_id, union_id, display_name)"
                " VALUES ('member_fake', 'run_probe', 'tenant_probe', 'member_probe', 'ou_fake',"
                " 'u_fake', 'on_fake', '化名·假成员')",
                dsn=dsn,
            )
            with self.injected_session(dsn) as cursor:
                # 一致性触发器是 DEFERRABLE INITIALLY DEFERRED：自动提交下在这条语句提交时触发。
                outcome = self.attempt(
                    cursor,
                    "INSERT INTO public.feishu_org_sync_run (id, source_app_id, status,"
                    " completed_at, tenant_count, member_count)"
                    " VALUES ('run_probe', 'cli_probe', 'complete', %s, 1, 1)",
                    (RECEIVED,),
                )
            persisted = bool(
                self.scalar(
                    "SELECT count(*) FROM public.feishu_org_sync_run WHERE id = 'run_probe'",
                    dsn=dsn,
                )
            )
            return {"outcome": outcome, "complete_run_persisted": persisted}
        finally:
            self.clear(dsn)

    def scenario_record_real_inbound_fake_table(self, dsn: str) -> dict[str, Any]:
        """``app_user_record_real_inbound``：evil 里的假用户表有同一个人，看更新落到哪张表。"""

        try:
            self.seed_user(dsn)
            self.build_evil(dsn)
            self.shadow_table(dsn, "app_user")
            self.admin(
                "INSERT INTO evil.app_user (id, feishu_open_id) VALUES ('usr_probe', 'ou_user_probe')",
                dsn=dsn,
            )
            with self.injected_session(dsn) as cursor:
                outcome = self.attempt(
                    cursor, INSERT_INBOUND_SQL, ("evt_inject", RECEIVED, "ou_user_probe", "trace_i")
                )
            return {
                "outcome": outcome,
                "public_last_inbound_at": self.scalar(
                    "SELECT last_inbound_at FROM public.app_user WHERE id = 'usr_probe'", dsn=dsn
                ),
                "evil_last_inbound_at": self.scalar(
                    "SELECT last_inbound_at FROM evil.app_user WHERE id = 'usr_probe'", dsn=dsn
                ),
            }
        finally:
            self.clear(dsn)

    def scenario_adopt_prior_inbound_fake_table(self, dsn: str) -> dict[str, Any]:
        """``app_user_adopt_prior_inbound``：假入站表里是远古事件，真入站表里是刚才那条。

        原定义会把假表里的远古时间当成「首次入站」写进真表——假数据落进真表，不只是读错。
        """

        try:
            self.admin(INSERT_INBOUND_SQL, ("evt_real", EARLIER, "ou_new_probe", "trace_r"), dsn)
            self.build_evil(dsn)
            self.shadow_table(dsn, "inbound_event")
            self.admin(
                "INSERT INTO evil.inbound_event (feishu_event_id, received_at, event_type,"
                " user_open_id, trace_id, expires_at) VALUES ('evt_fake', %s, 'x', 'ou_new_probe',"
                " 'trace_f', %s)",
                (ANCIENT, ANCIENT),
                dsn,
            )
            with self.injected_session(dsn) as cursor:
                outcome = self.attempt(
                    cursor,
                    INSERT_USER_SQL,
                    ("usr_new", "ou_new_probe", "u_new", "on_new", "化名·新人"),
                )
            return {
                "outcome": outcome,
                "public_first_inbound_at": self.scalar(
                    "SELECT first_inbound_at FROM public.app_user WHERE id = 'usr_new'", dsn=dsn
                ),
            }
        finally:
            self.clear(dsn)

    # ---- 只用 NEW / OLD 与内置对象的函数：假运算符 ---------------------------------

    def scenario_inbound_event_fix_expiry_operators(self, dsn: str) -> dict[str, Any]:
        """``inbound_event_fix_expiry``（``*_fix_expiry`` 代表）：假 ``+`` 让到期推导成远古，假 ``<>`` 让改时间不再被拒。"""

        try:
            self.build_evil(dsn, EVIL_FREEZE_BYPASS_SQL)
            with self.injected_session(dsn) as cursor:
                cursor.execute(INSERT_INBOUND_SQL, ("evt_probe", RECEIVED, None, "trace_p"))
                inserted_expiry = self.scalar(
                    "SELECT expires_at FROM public.inbound_event WHERE feishu_event_id = 'evt_probe'",
                    dsn=dsn,
                )
                update_outcome = self.attempt(
                    cursor,
                    "UPDATE public.inbound_event SET received_at = %s"
                    " WHERE feishu_event_id = 'evt_probe'",
                    (ANCIENT,),
                )
            return {
                "insert_expires_at": inserted_expiry,
                "update_outcome": update_outcome,
                "received_at_after_update": self.scalar(
                    "SELECT received_at FROM public.inbound_event WHERE feishu_event_id = 'evt_probe'",
                    dsn=dsn,
                ),
            }
        finally:
            self.clear(dsn)

    def scenario_task_freeze_invariants_operators(self, dsn: str) -> dict[str, Any]:
        """``task_freeze_invariants``（``*_freeze_*`` 代表）：假 ``=`` 让 IS DISTINCT FROM 永远说没改。"""

        try:
            self.seed_user(dsn)
            self.admin(INSERT_INBOUND_SQL, ("evt_task", EARLIER, "ou_user_probe", "trace_t"), dsn)
            self.admin(
                "INSERT INTO public.conversation (id, user_id, feishu_chat_id)"
                " VALUES ('conv_probe', 'usr_probe', 'oc_probe')",
                dsn=dsn,
            )
            self.build_evil(dsn, EVIL_FREEZE_BYPASS_SQL)
            with self.injected_session(dsn) as cursor:
                cursor.execute(
                    "INSERT INTO public.task (id, conversation_id, user_id, inbound_event_id, prompt,"
                    " target_worker_version, created_at)"
                    " VALUES ('task_probe', 'conv_probe', 'usr_probe', 'evt_task', '化名问题', 'w1', %s)",
                    (RECEIVED,),
                )
                inserted_expiry = self.scalar(
                    "SELECT content_expires_at FROM public.task WHERE id = 'task_probe'", dsn=dsn
                )
                update_outcome = self.attempt(
                    cursor,
                    "UPDATE public.task SET created_at = %s WHERE id = 'task_probe'",
                    (ANCIENT,),
                )
            return {
                "insert_content_expires_at": inserted_expiry,
                "update_outcome": update_outcome,
                "created_at_after_update": self.scalar(
                    "SELECT created_at FROM public.task WHERE id = 'task_probe'", dsn=dsn
                ),
            }
        finally:
            self.clear(dsn)

    def scenario_pending_action_fix_retention_expiry_operators(self, dsn: str) -> dict[str, Any]:
        """``pending_action_fix_retention_expiry``（最晚加入的 ALTER 组函数）：同上两把假运算符。"""

        try:
            self.build_evil(dsn, EVIL_FREEZE_BYPASS_SQL)
            with self.injected_session(dsn) as cursor:
                cursor.execute(
                    "INSERT INTO public.pending_action (id, action_type, target_open_id,"
                    " target_state_snapshot, initiated_by_open_id, confirm_deadline_at, created_at)"
                    " VALUES ('pa_probe', 'suspend_user', 'ou_target', '{}', 'ou_admin', %s, %s)",
                    (RECEIVED + timedelta(hours=1), RECEIVED),
                )
                inserted_expiry = self.scalar(
                    "SELECT retention_expires_at FROM public.pending_action WHERE id = 'pa_probe'",
                    dsn=dsn,
                )
                update_outcome = self.attempt(
                    cursor,
                    "UPDATE public.pending_action SET created_at = %s WHERE id = 'pa_probe'",
                    (ANCIENT,),
                )
            return {
                "insert_retention_expires_at": inserted_expiry,
                "update_outcome": update_outcome,
                "created_at_after_update": self.scalar(
                    "SELECT created_at FROM public.pending_action WHERE id = 'pa_probe'", dsn=dsn
                ),
            }
        finally:
            self.clear(dsn)

    def scenario_mcp_access_token_immutable_operator(self, dsn: str) -> dict[str, Any]:
        """``mcp_access_token_immutable``：假的文本 ``=`` 恒真，已签发密文能否被覆盖。"""

        try:
            self.seed_user(dsn)
            self.admin(
                "INSERT INTO public.mcp_access_token (user_id, token_cipher, issued_at)"
                " VALUES ('usr_probe', %s, %s)",
                (CIPHER_ISSUED, RECEIVED),
                dsn,
            )
            self.build_evil(dsn, EVIL_TEXT_ALWAYS_EQUAL_SQL)
            with self.injected_session(dsn) as cursor:
                # 表里只有这一行，故意不带 WHERE：会话里文本 = 已经是假的，别让触发语句自己也用它。
                update_outcome = self.attempt(
                    cursor,
                    "UPDATE public.mcp_access_token SET token_cipher = %s",
                    (CIPHER_OVERWRITE,),
                )
            return {
                "update_outcome": update_outcome,
                "cipher_after_update": self.scalar(
                    "SELECT token_cipher FROM public.mcp_access_token WHERE user_id = 'usr_probe'",
                    dsn=dsn,
                ),
            }
        finally:
            self.clear(dsn)


class HardenedFunctionsIgnoreInjectedSearchPathTest(InjectionScenarioTestCase):
    """验收 E-2 ④ 正例：链头定义下，每种注入都落空——按 public 真表与 pg_catalog 运算符行事。"""

    def setUp(self) -> None:
        reset_production_rows(self._dsn)

    def test_the_scenario_table_covers_every_redefined_function(self) -> None:
        """五个重定义函数每个至少有一个假表场景；场景名与预期表逐一对应。"""

        covered = {
            "app_user_reject_delegated_subject": "reject_delegated_subject_fake_table",
            "credential_reject_app_user_subject": "credential_reject_app_user_subject_fake_table",
            "feishu_org_sync_run_verify_children": "verify_children_fake_tables",
            "app_user_record_real_inbound": "record_real_inbound_fake_table",
            "app_user_adopt_prior_inbound": "adopt_prior_inbound_fake_table",
        }
        self.assertEqual(set(covered), set(REDEFINED_FUNCTIONS))
        self.assertEqual(set(DEFENDED), set(HIJACKED))
        for name, _ in self.scenarios():
            self.assertIn(name, HIJACKED)
            self.assertNotEqual(DEFENDED[name], HIJACKED[name], f"{name} 的正负预期必须不同")

    def test_reject_delegated_subject_ignores_a_fake_table_in_evil(self) -> None:
        self.assertEqual(
            self.scenario_reject_delegated_subject_fake_table(self._dsn),
            DEFENDED["reject_delegated_subject_fake_table"],
        )

    def test_reject_delegated_subject_ignores_a_same_named_temp_table(self) -> None:
        self.assertEqual(
            self.scenario_reject_delegated_subject_temp_table(self._dsn),
            DEFENDED["reject_delegated_subject_temp_table"],
        )

    def test_reject_delegated_subject_ignores_a_hijacked_equality_operator(self) -> None:
        self.assertEqual(
            self.scenario_reject_delegated_subject_operator(self._dsn),
            DEFENDED["reject_delegated_subject_operator"],
        )

    def test_credential_reject_app_user_subject_ignores_a_fake_table_in_evil(self) -> None:
        self.assertEqual(
            self.scenario_credential_reject_app_user_subject_fake_table(self._dsn),
            DEFENDED["credential_reject_app_user_subject_fake_table"],
        )

    def test_verify_children_counts_the_real_child_tables_only(self) -> None:
        self.assertEqual(
            self.scenario_verify_children_fake_tables(self._dsn),
            DEFENDED["verify_children_fake_tables"],
        )

    def test_record_real_inbound_updates_the_real_user_table_only(self) -> None:
        self.assertEqual(
            self.scenario_record_real_inbound_fake_table(self._dsn),
            DEFENDED["record_real_inbound_fake_table"],
        )

    def test_adopt_prior_inbound_reads_the_real_inbound_table_only(self) -> None:
        self.assertEqual(
            self.scenario_adopt_prior_inbound_fake_table(self._dsn),
            DEFENDED["adopt_prior_inbound_fake_table"],
        )

    def test_inbound_event_fix_expiry_uses_catalog_operators(self) -> None:
        self.assertEqual(
            self.scenario_inbound_event_fix_expiry_operators(self._dsn),
            DEFENDED["inbound_event_fix_expiry_operators"],
        )

    def test_task_freeze_invariants_uses_catalog_operators(self) -> None:
        self.assertEqual(
            self.scenario_task_freeze_invariants_operators(self._dsn),
            DEFENDED["task_freeze_invariants_operators"],
        )

    def test_pending_action_fix_retention_expiry_uses_catalog_operators(self) -> None:
        self.assertEqual(
            self.scenario_pending_action_fix_retention_expiry_operators(self._dsn),
            DEFENDED["pending_action_fix_retention_expiry_operators"],
        )

    def test_mcp_access_token_immutable_uses_catalog_operators(self) -> None:
        self.assertEqual(
            self.scenario_mcp_access_token_immutable_operator(self._dsn),
            DEFENDED["mcp_access_token_immutable_operator"],
        )


class OriginalDefinitionsAreHijackedTest(InjectionScenarioTestCase):
    """验收 E-2 ④ 负例：降级回 0095 原文定义后同一组注入全部得手；升到链头后全部落空。

    这条是「测试能分辨」的证明：同一份场景代码、同一份预期表，只有定义不同。
    ``downgrade`` 出来的定义与 0095 原文逐字节一致（0096 导入期自检 + 另一模块的属性对照），
    因此它同时也说明回滚到 0095 会把这些防线重新暴露给搜索路径注入。
    """

    def test_downgraded_definitions_are_hijacked_and_head_restores_the_defence(self) -> None:
        probe = self.create_probe_database()
        probe_dsn = self.database_dsn(probe)
        _run_alembic(probe_dsn, "upgrade", HEAD_REVISION)
        _run_alembic(probe_dsn, "downgrade", "-1")
        self.assertEqual(
            self.scalar("SELECT version_num FROM alembic_version", dsn=probe_dsn), PREVIOUS_REVISION
        )
        self.assertEqual(len(self.unpinned_functions(probe_dsn)), 19)

        for name, scenario in self.scenarios():
            with self.subTest(definition=PREVIOUS_REVISION, scenario=name):
                self.assertEqual(scenario(probe_dsn), HIJACKED[name])

        _run_alembic(probe_dsn, "upgrade", HEAD_REVISION)
        self.assertEqual(
            self.scalar("SELECT version_num FROM alembic_version", dsn=probe_dsn), HEAD_REVISION
        )
        self.assertEqual(self.unpinned_functions(probe_dsn), [])
        for name in REDEFINED_FUNCTIONS:
            self.assertIn(PINNED_SEARCH_PATH, self.proconfig(probe_dsn, name) or [])

        for name, scenario in self.scenarios():
            with self.subTest(definition=HEAD_REVISION, scenario=name):
                self.assertEqual(scenario(probe_dsn), DEFENDED[name])


class HostedPlatformRolesAfterFullRebuildTest(InjectionScenarioTestCase):
    """验收 E-2 ③⑩：平台角色存在时整链重建，匿名与登录态角色对清理函数无有效 EXECUTE。"""

    def test_anonymous_roles_cannot_execute_cleanup_after_the_whole_chain_rebuilds(self) -> None:
        creator = self.scalar("SELECT current_user")
        if creator != "postgres":
            self.skipTest("迁移只调整创建者 postgres 的默认权限；当前测试身份不是 postgres")
        with self.platform_roles():
            try:
                # 先照托管平台的形态设好创建者默认权限，再重建整链：0054 建出的清理函数
                # 因此自带三个平台角色的 EXECUTE，0096 再把其中两条撤掉。
                self.admin(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public"
                    " GRANT EXECUTE ON FUNCTIONS TO anon, authenticated, service_role"
                )
                members_before = self.memberships("lingxi_retention_owner", creator)
                force_rebuild_schema(self._dsn)

                for role in ("anon", "authenticated"):
                    self.assertFalse(
                        self.effective_execute(self._dsn, role), f"{role} 的有效 EXECUTE 必须为假"
                    )
                for role in ("service_role", "lingxi_scheduler", "lingxi_retention_owner"):
                    self.assertTrue(
                        self.effective_execute(self._dsn, role), f"{role} 的合法调用必须保留"
                    )
                self.assertEqual(
                    set(self.cleanup_acl_grantees(self._dsn)),
                    {"service_role", "lingxi_scheduler", "lingxi_retention_owner"},
                )
                self.assertEqual(
                    self.default_function_acl(self._dsn, "postgres"), {"service_role": "postgres"}
                )
                self.assertEqual(
                    self.memberships("lingxi_retention_owner", creator),
                    members_before,
                    "临时授予若有，必须在迁移内精确收回；成员关系不得多出或少掉任何一条",
                )
                self.assertEqual(self.unpinned_functions(self._dsn), [])
                self.assertIsNotNone(
                    self.scalar("SELECT to_regprocedure(%s)", (CLEANUP_FUNCTION,)),
                    "清理函数必须仍然存在",
                )
            finally:
                # 共享测试库不是探针库：角色是集群级对象，删角色前先把它们在本库的权限
                # （含默认权限项）清掉，再整链重建，把库还原成没有平台角色时的形态。
                for role in PLATFORM_ROLES:
                    self.admin(f"DROP OWNED BY {role}")
                force_rebuild_schema(self._dsn)


if __name__ == "__main__":
    unittest.main()
