"""运营审计账 ``operation_audit`` 的真库断言：追加回读、只追加、到期固定、清理接入、无秘密。

有 ``LINGXI_POSTGRES_DSN`` 时在真实 PostgreSQL 上跑（建库走 ``postgres_schema``
整链前滚，与生产同源），否则明确跳过并写明原因。数据库约束类断言只在真库上验证，
不用 mock：

1. 两个写口（同事务 / 自开连接）都能追加，回读与写入逐列相等；调用方事务回滚则一行不留；
2. ``UPDATE`` 一律被触发器拒绝——改结果码、改到期时间、改创建时间都不行；
3. ``expires_at`` 固定为 ``created_at + 2160 小时``，调用方传别的值也被覆盖；
4. 注入时钟后清理只删到期行，边界含等号、批量上限留给下一轮、朴素时刻拒绝；
5. 载体清理的待确认操作那一面同一事务里带走到期的审计行；
6. 整行 ``::text`` 扫描找不到样本秘密——秘密在模型层就被拒绝，库里从未出现过。

7. 迁移 ``0097`` 在探针库上：表非空时拒绝降级且一行不丢，空表时降级把表与两只函数
   一并带走、再前滚回来。

另有一条不需要数据库的源码守卫：到期清理函数必须有生产调用方。
数据全部为虚构化名，不含任何真实人员数据。
"""

from __future__ import annotations

import ast
import inspect
import logging
import os
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from postgres_schema import ALEMBIC_INI, psycopg_available

from lingxi.core.admin.operation_audit import (
    EntryPoint,
    OperationAuditEntry,
    OperationPhase,
)
from lingxi.core.admin.registry import AdminRole

SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，运营审计账的真库断言未验证（需真实 PostgreSQL 16）"
    if not os.environ.get("LINGXI_POSTGRES_DSN")
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，运营审计账的真库断言未验证"
)

RETENTION_WINDOW = timedelta(hours=2160)
ULID = "01HXYZABCDEFGHJKMNPQRSTVWX"
DIGEST = "sha256:" + "cd" * 32

#: 全部列名——钉住「没有自由文本列」这件事：以后谁加一列，这里先红，再去回答那一列
#: 放的是什么形状的值。
COLUMNS = (
    "id",
    "operation_id",
    "operation",
    "phase",
    "initiated_by",
    "actor_roles",
    "decided_by",
    "executor",
    "entry_point",
    "purpose",
    "target_kind",
    "target_count",
    "target_digest",
    "target_user_id",
    "result_code",
    "result_counts",
    "evidence_ref",
    "pending_action_id",
    "trace_id",
    "created_at",
    "expires_at",
)


def sample_entry(operation_id: str = f"opr_{ULID}", **overrides: object) -> OperationAuditEntry:
    fields: dict[str, object] = {
        "operation_id": operation_id,
        "operation": "innertest.additions",
        "phase": OperationPhase.EXECUTED,
        "initiated_by": "ou_admin_fake",
        "actor_roles": frozenset({AdminRole.OPS_ADMIN, AdminRole.SUPER_ADMIN}),
        "entry_point": EntryPoint.SCHEDULER_FOLLOWUP,
        "decided_by": "ou_decider_fake",
        "executor": f"scheduler@2.5.0:run_{ULID}",
        "purpose": "innertest_additions",
        "target_kind": "batch",
        "target_count": 2,
        "target_digest": DIGEST,
        "target_user_id": f"usr_{ULID}",
        "result_code": "completed",
        "result_counts": {"provisioned": 1, "skipped": 1},
        "evidence_ref": f"innertest_batch:ibt_{ULID}",
        "pending_action_id": f"pac_{ULID}",
        "trace_id": ULID,
    }
    fields.update(overrides)
    return OperationAuditEntry(**fields)


def _calls_function(path: Path, name: str) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            callee = node.func
            if isinstance(callee, ast.Name) and callee.id == name:
                return True
            if isinstance(callee, ast.Attribute) and callee.attr == name:
                return True
    return False


class ProductionCallerSourceTest(unittest.TestCase):
    """清理函数在、没人调，是这类到期上限最常见的失效形状；按 AST 找调用点。"""

    def test_the_purge_has_a_production_caller_outside_its_own_module(self) -> None:
        import lingxi

        source_root = Path(inspect.getsourcefile(lingxi)).parent
        callers = sorted(
            path.relative_to(source_root).as_posix()
            for path in source_root.rglob("*.py")
            if path.name != "postgres_operation_audit.py"
            and _calls_function(path, "purge_expired_operation_audit")
        )

        self.assertEqual(callers, ["adapters/postgres_carrier_retention.py"])


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN") and psycopg_available(), SKIP_REASON)
class OperationAuditPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import psycopg
        from postgres_schema import ensure_production_schema

        cls._psycopg = psycopg
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        from postgres_schema import reset_production_rows

        from lingxi.adapters.postgres import connect
        from lingxi.adapters.postgres_operation_audit import PostgresOperationAudit

        reset_production_rows(self._dsn)
        self._connect = connect
        self.store = PostgresOperationAudit(self._dsn)

    # ---- 夹具 -------------------------------------------------------------

    def _fetch(self, sql: str, parameters: tuple = ()) -> list[tuple]:
        with self._connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(sql, parameters)
            return list(cursor.fetchall())

    def _execute(self, sql: str, parameters: tuple = ()) -> None:
        with self._connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(sql, parameters)
            connection.commit()

    def _seed_raw(self, row_id: str, *, created_at: datetime, expires_at: datetime) -> None:
        """绕过模型直接插一行，用来造已到期的行；到期列写什么都会被触发器覆盖。"""
        self._execute(
            "INSERT INTO operation_audit (id, operation_id, operation, phase, initiated_by,"
            " actor_roles, entry_point, created_at, expires_at)"
            " VALUES (%s, %s, 'preprovision.apply', 'prepared', 'ou_admin_fake', '',"
            " 'ops_script', %s, %s)",
            (row_id, f"opr_{row_id}", created_at, expires_at),
        )

    def _ids(self) -> list[str]:
        return [row[0] for row in self._fetch("SELECT id FROM operation_audit ORDER BY id")]

    def _purge(self, now: datetime, limit: int = 200) -> int:
        from lingxi.adapters.postgres_operation_audit import purge_expired_operation_audit

        with self._connect(self._dsn) as connection:
            with connection.transaction():
                return purge_expired_operation_audit(connection, now=now, limit=limit)

    # ---- 追加与回读 -------------------------------------------------------

    def test_record_then_read_back_by_operation_and_by_recency(self) -> None:
        entry = sample_entry()

        row_id = self.store.record(entry)

        self.assertTrue(row_id.startswith("opa_"))
        (record,) = self.store.for_operation(entry.operation_id)
        self.assertEqual(record.id, row_id)
        self.assertEqual(record.entry, entry)
        self.assertEqual(record.expires_at - record.created_at, RETENTION_WINDOW)
        self.assertEqual([item.id for item in self.store.recent(10)], [row_id])
        self.assertEqual(self.store.for_operation("opr_nobody"), ())

    def test_phases_of_one_operation_come_back_in_order(self) -> None:
        prepared = sample_entry(
            phase=OperationPhase.PREPARED, decided_by=None, executor=None, result_code=None
        )
        confirmed = sample_entry(phase=OperationPhase.CONFIRMED, executor=None, result_code=None)
        executed = sample_entry()
        ids = [self.store.record(item) for item in (prepared, confirmed, executed)]

        records = self.store.for_operation(executed.operation_id)

        self.assertEqual([item.id for item in records], ids)
        self.assertEqual(
            [item.entry.phase for item in records],
            [p.phase for p in (prepared, confirmed, executed)],
        )
        self.assertEqual([item.id for item in self.store.recent(2)], ids[:0:-1])

    def test_the_connection_level_writer_lives_and_dies_with_the_caller_transaction(self) -> None:
        from lingxi.adapters.postgres_operation_audit import record_operation_audit

        with self._connect(self._dsn) as connection:
            with self.assertRaises(RuntimeError), connection.transaction():
                record_operation_audit(connection, sample_entry(operation_id="opr_rolled_back"))
                raise RuntimeError("业务写入失败，审计一起回滚")
            with connection.transaction():
                kept = record_operation_audit(connection, sample_entry(operation_id="opr_kept"))

        self.assertEqual(self._ids(), [kept])
        self.assertEqual(self.store.for_operation("opr_rolled_back"), ())

    def test_only_entries_are_accepted_by_both_writers(self) -> None:
        from lingxi.adapters.postgres_operation_audit import record_operation_audit

        with self.assertRaises(TypeError):
            self.store.record({"operation": "preprovision.apply"})
        with self._connect(self._dsn) as connection, self.assertRaises(TypeError):
            record_operation_audit(connection, None)
        self.assertEqual(self._ids(), [])

    def test_the_table_has_exactly_the_designed_columns(self) -> None:
        columns = self._fetch(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'public' AND table_name = 'operation_audit'"
            " ORDER BY ordinal_position"
        )

        self.assertEqual(tuple(row[0] for row in columns), COLUMNS)
        foreign_keys = self._fetch(
            "SELECT count(*) FROM pg_constraint"
            " WHERE conrelid = 'public.operation_audit'::regclass AND contype = 'f'"
        )
        self.assertEqual(foreign_keys, [(0,)])

    # ---- 只追加 -----------------------------------------------------------

    def test_every_update_is_rejected_by_the_trigger(self) -> None:
        row_id = self.store.record(sample_entry())
        before = self._fetch("SELECT * FROM operation_audit WHERE id = %s", (row_id,))

        for statement in (
            "UPDATE operation_audit SET result_code = 'partial' WHERE id = %s",
            "UPDATE operation_audit SET expires_at = expires_at + interval '1 day' WHERE id = %s",
            "UPDATE operation_audit SET created_at = now() - interval '100 days' WHERE id = %s",
            "UPDATE operation_audit SET initiated_by = 'ou_someone_else' WHERE id = %s",
        ):
            with self.subTest(statement=statement):
                with self.assertRaises(self._psycopg.errors.RaiseException):
                    self._execute(statement, (row_id,))

        self.assertEqual(
            self._fetch("SELECT * FROM operation_audit WHERE id = %s", (row_id,)), before
        )

    # ---- 到期固定 ---------------------------------------------------------

    def test_expires_at_is_pinned_to_created_at_plus_2160_hours(self) -> None:
        created = datetime(2026, 1, 1, tzinfo=UTC)
        self._seed_raw("opa_forced", created_at=created, expires_at=created + timedelta(days=1))
        self._seed_raw("opa_far", created_at=created, expires_at=created + timedelta(days=400))

        rows = self._fetch(
            "SELECT id, expires_at FROM operation_audit WHERE id IN ('opa_forced','opa_far') ORDER BY id"
        )

        self.assertEqual(
            rows,
            [("opa_far", created + RETENTION_WINDOW), ("opa_forced", created + RETENTION_WINDOW)],
        )

    def test_the_check_constraints_mirror_the_model(self) -> None:
        for statement in (
            "INSERT INTO operation_audit (id, operation_id, operation, phase, initiated_by,"
            " actor_roles, entry_point) VALUES ('opa_bad_op', 'opr_x', 'Bad Op', 'prepared',"
            " 'ou_a', '', 'ops_script')",
            "INSERT INTO operation_audit (id, operation_id, operation, phase, initiated_by,"
            " actor_roles, entry_point) VALUES ('opa_bad_phase', 'opr_x', 'x.y', 'done',"
            " 'ou_a', '', 'ops_script')",
            "INSERT INTO operation_audit (id, operation_id, operation, phase, initiated_by,"
            " actor_roles, entry_point) VALUES ('opa_no_decider', 'opr_x', 'x.y', 'confirmed',"
            " 'ou_a', '', 'feishu_card')",
            "INSERT INTO operation_audit (id, operation_id, operation, phase, initiated_by,"
            " actor_roles, entry_point) VALUES ('opa_no_executor', 'opr_x', 'x.y', 'executed',"
            " 'ou_a', '', 'ops_script')",
            "INSERT INTO operation_audit (id, operation_id, operation, phase, initiated_by,"
            " actor_roles, entry_point, result_counts) VALUES ('opa_counts', 'opr_x', 'x.y',"
            " 'prepared', 'ou_a', '', 'ops_script', '[1]'::jsonb)",
        ):
            with self.subTest(statement=statement):
                with self.assertRaises(self._psycopg.errors.CheckViolation):
                    self._execute(statement)
        self.assertEqual(self._ids(), [])

    # ---- 到期清理 ---------------------------------------------------------

    def test_purge_deletes_only_expired_rows_and_the_boundary_is_inclusive(self) -> None:
        now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
        self._seed_raw(
            "opa_old", created_at=now - RETENTION_WINDOW - timedelta(days=1), expires_at=now
        )
        self._seed_raw("opa_edge", created_at=now - RETENTION_WINDOW, expires_at=now)
        self._seed_raw(
            "opa_fresh",
            created_at=now - RETENTION_WINDOW + timedelta(microseconds=1),
            expires_at=now,
        )
        self._seed_raw("opa_new", created_at=now - timedelta(days=1), expires_at=now)

        self.assertEqual(self._purge(now), 2)

        self.assertEqual(self._ids(), ["opa_fresh", "opa_new"])
        self.assertEqual(self._purge(now), 0, "再跑一次没有更多可删")

    def test_the_batch_limit_leaves_the_rest_for_the_next_round(self) -> None:
        now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
        for index in range(3):
            self._seed_raw(
                f"opa_old_{index}",
                created_at=now - RETENTION_WINDOW - timedelta(days=index + 1),
                expires_at=now,
            )

        self.assertEqual(self._purge(now, limit=2), 2)
        self.assertEqual(self._ids(), ["opa_old_0"], "最晚到期的那条留到下一轮")
        self.assertEqual(self._purge(now, limit=2), 1)
        self.assertEqual(self._ids(), [])

    def test_a_naive_moment_and_a_bad_limit_are_refused_before_any_delete(self) -> None:
        now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
        self._seed_raw(
            "opa_old", created_at=now - RETENTION_WINDOW - timedelta(days=1), expires_at=now
        )

        with self.assertRaises(ValueError):
            self._purge(now.replace(tzinfo=None))
        for bad_limit in (0, -1, True):
            with self.subTest(limit=bad_limit), self.assertRaises(ValueError):
                self._purge(now, limit=bad_limit)
        self.assertEqual(self._ids(), ["opa_old"])

    def test_the_carrier_retention_sweep_purges_expired_audit_rows_in_the_pending_action_face(
        self,
    ) -> None:
        """清理接在待确认操作那一面的同一事务里：到期的走、未到期的留、既有返回值不变。"""
        from lingxi.adapters.postgres_carrier_retention import PostgresCarrierRetention

        now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
        self._seed_raw(
            "opa_old", created_at=now - RETENTION_WINDOW - timedelta(days=1), expires_at=now
        )
        self._seed_raw("opa_new", created_at=now - timedelta(days=1), expires_at=now)

        redacted = PostgresCarrierRetention(self._dsn).redact_expired_pending_actions(now=now)

        self.assertEqual(redacted, 0, "返回值仍然只数 pending_action，没有混入审计行")
        self.assertEqual(self._ids(), ["opa_new"])

    # ---- 无秘密 -----------------------------------------------------------

    def test_no_sample_secret_ever_reaches_the_table(self) -> None:
        """秘密在模型层被拒绝；这里主动在整行文本里找，证明库里确实没有。"""
        self.store.record(sample_entry())
        secrets = {
            "assignment": "token=sk_live_FAKE",
            "bearer": "Bearer_sk_live_FAKE",
            "credential_url": "postgresql://app:hunter2@db.internal/lingxi",
            "whitespace": "sk live FAKE",
            "overlong": "x" * 200,
        }
        for shape, secret in secrets.items():
            with self.subTest(shape=shape):
                with self.assertRaises(ValueError):
                    self.store.record(sample_entry(result_code=secret))
                with self.assertRaises(ValueError):
                    self.store.record(sample_entry(result_counts={"provisioned": secret}))
        for needle in ("sk_live_FAKE", "sk live FAKE", "hunter2", "bearer", "x" * 200):
            with self.subTest(needle=needle):
                hits = self._fetch(
                    "SELECT count(*) FROM operation_audit WHERE operation_audit::text ILIKE %s",
                    (f"%{needle}%",),
                )
                self.assertEqual(hits, [(0,)])
        self.assertEqual(len(self._ids()), 1)


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
class RevisionDowngradeTest(unittest.TestCase):
    """迁移 ``0097`` 的降级边界：有记录就拒绝、一行不丢；空表才完整逆转。"""

    PARENT = "0096_plpgsql_search_path"
    OBJECTS_SQL = (
        "SELECT to_regclass('public.operation_audit') IS NOT NULL,"
        " to_regprocedure('public.operation_audit_fix_expiry()') IS NOT NULL,"
        " to_regprocedure('public.operation_audit_append_only()') IS NOT NULL"
    )

    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        cls._psycopg = psycopg
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]

    def _admin(self, sql: str, dsn: str | None = None) -> list[tuple]:
        with (
            self._psycopg.connect(dsn or self._dsn, autocommit=True) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(sql)
            return cursor.fetchall() if cursor.description else []

    def _probe_database(self) -> str:
        name = f"lingxi_opa_probe_{uuid.uuid4().hex[:8]}"
        self._admin(f"CREATE DATABASE {name}")
        self.addCleanup(self._admin, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        parts = urlsplit(self._dsn)
        return urlunsplit(parts._replace(path=f"/{name}"))

    def test_a_populated_table_refuses_downgrade_and_an_empty_one_reverts_completely(
        self,
    ) -> None:
        probe = self._probe_database()
        _run_alembic(probe, "upgrade", "0097_operation_audit")
        self._admin(
            "INSERT INTO operation_audit (id, operation_id, operation, phase, initiated_by,"
            " actor_roles, entry_point) VALUES ('opa_keep', 'opr_keep', 'preprovision.apply',"
            " 'prepared', 'ou_admin_fake', '', 'ops_script')",
            probe,
        )

        with self.assertRaises(Exception) as caught:
            _run_alembic(probe, "downgrade", self.PARENT)

        self.assertIn("compatible recovery", str(caught.exception))
        self.assertEqual(self._admin(self.OBJECTS_SQL, probe), [(True, True, True)])
        self.assertEqual(self._admin("SELECT id FROM operation_audit", probe), [("opa_keep",)])
        self.assertEqual(
            self._admin("SELECT version_num FROM alembic_version", probe),
            [("0097_operation_audit",)],
        )

        self._admin("DELETE FROM operation_audit", probe)
        _run_alembic(probe, "downgrade", self.PARENT)
        self.assertEqual(self._admin(self.OBJECTS_SQL, probe), [(False, False, False)])
        self.assertEqual(
            self._admin("SELECT version_num FROM alembic_version", probe), [(self.PARENT,)]
        )

        _run_alembic(probe, "upgrade", "0097_operation_audit")
        self.assertEqual(self._admin(self.OBJECTS_SQL, probe), [(True, True, True)])


if __name__ == "__main__":
    unittest.main()
