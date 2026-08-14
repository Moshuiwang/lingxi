"""Issue #75：数据库连接、语句和锁超时的仓库级约定与会变红门禁。"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from lingxi.adapters.postgres import (
    DEFAULT_POSTGRES_TIMEOUTS,
    MAX_TIMEOUT_SECONDS,
    PostgresTimeoutConfigError,
    PostgresTimeouts,
    connect,
)
from lingxi.adapters.retention import (
    RETENTION_CLEANUP_STATEMENT_TIMEOUT_SECONDS,
    RETENTION_CLEANUP_TIMEOUTS,
    RETENTION_DELETE_BATCH_MARGIN_SECONDS,
    RETENTION_FUNCTION_LOCK_TIMEOUT_SECONDS,
    RETENTION_FUNCTION_LOCK_WAIT_COUNT,
    PostgresRetentionCleaner,
)


REPOSITORY_ROOT = Path(__file__).parents[1]
CHECK_PATH = REPOSITORY_ROOT / "scripts" / "ci" / "check_db_timeouts.py"
MIGRATION_DSN_PATH = REPOSITORY_ROOT / "migrations" / "alembic" / "migration_dsn.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHECK = _load_module(CHECK_PATH, "db_timeout_check_under_test")
MIGRATION_DSN = _load_module(MIGRATION_DSN_PATH, "migration_dsn_timeout_under_test")


class PostgresTimeoutConfigTest(unittest.TestCase):
    def test_business_defaults_have_one_bounded_source(self) -> None:
        self.assertEqual(
            (
                DEFAULT_POSTGRES_TIMEOUTS.connect_timeout_seconds,
                DEFAULT_POSTGRES_TIMEOUTS.statement_timeout_seconds,
                DEFAULT_POSTGRES_TIMEOUTS.lock_timeout_seconds,
            ),
            (5, 3, 2),
        )
        self.assertEqual(
            DEFAULT_POSTGRES_TIMEOUTS.libpq_options,
            "-c statement_timeout=3s -c lock_timeout=2s",
        )

    def test_legal_environment_overrides_are_applied(self) -> None:
        config = PostgresTimeouts.from_env(
            {
                "LINGXI_POSTGRES_CONNECT_TIMEOUT_SECONDS": str(MAX_TIMEOUT_SECONDS),
                "LINGXI_POSTGRES_STATEMENT_TIMEOUT_SECONDS": str(MAX_TIMEOUT_SECONDS),
                "LINGXI_POSTGRES_LOCK_TIMEOUT_SECONDS": str(MAX_TIMEOUT_SECONDS),
            }
        )
        self.assertEqual(
            (config.connect_timeout_seconds, config.statement_timeout_seconds, config.lock_timeout_seconds),
            (MAX_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS),
        )

    def test_missing_environment_values_keep_finite_defaults(self) -> None:
        config = PostgresTimeouts.from_env({})
        self.assertEqual(config, DEFAULT_POSTGRES_TIMEOUTS)

    def test_invalid_environment_values_are_rejected_without_unbounded_fallback(self) -> None:
        for raw in ("0", "-1", str(MAX_TIMEOUT_SECONDS + 1), "not-a-number"):
            with self.subTest(raw=raw):
                with self.assertRaises(PostgresTimeoutConfigError):
                    PostgresTimeouts.from_env(
                        {"LINGXI_POSTGRES_STATEMENT_TIMEOUT_SECONDS": raw}
                    )

    def test_retention_adapter_statement_timeout_exceeds_function_lock_wait_budget(self) -> None:
        """清理函数的两次 2s 锁等待必须先于适配器级 statement_timeout 返回。"""

        lock_wait_budget = RETENTION_FUNCTION_LOCK_WAIT_COUNT * RETENTION_FUNCTION_LOCK_TIMEOUT_SECONDS
        self.assertEqual(
            RETENTION_CLEANUP_STATEMENT_TIMEOUT_SECONDS,
            lock_wait_budget + RETENTION_DELETE_BATCH_MARGIN_SECONDS,
        )
        self.assertGreater(RETENTION_CLEANUP_TIMEOUTS.statement_timeout_seconds, lock_wait_budget)
        self.assertEqual(
            PostgresRetentionCleaner("postgresql://test/db")._timeouts,
            RETENTION_CLEANUP_TIMEOUTS,
        )

    def test_factory_always_passes_all_three_boundaries_to_psycopg(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        class FakePsycopg(types.ModuleType):
            def connect(self, dsn: str, **kwargs: object) -> object:
                calls.append((dsn, kwargs))
                return object()

        with mock.patch.dict(sys.modules, {"psycopg": FakePsycopg("psycopg")}):
            connection = connect("postgresql://test/db", autocommit=True)

        self.assertIsNotNone(connection)
        self.assertEqual(len(calls), 1)
        dsn, kwargs = calls[0]
        self.assertEqual(dsn, "postgresql://test/db")
        self.assertEqual(kwargs["connect_timeout"], 5)
        self.assertEqual(kwargs["options"], "-c statement_timeout=3s -c lock_timeout=2s")
        self.assertTrue(kwargs["autocommit"])

    def test_callers_cannot_replace_factory_timeout_options(self) -> None:
        with self.assertRaises(TypeError):
            connect("postgresql://test/db", connect_timeout=0)
        with self.assertRaises(TypeError):
            connect("postgresql://test/db", options="")

    def test_factory_rejects_unvalidated_timeout_objects(self) -> None:
        with self.assertRaises(TypeError):
            connect("postgresql://test/db", timeouts=object())


class DbTimeoutGateTest(unittest.TestCase):
    def test_repository_runtime_connections_pass(self) -> None:
        self.assertEqual(CHECK.check_runtime_connections(), [])
        self.assertEqual(CHECK.check_runtime_connections(CHECK.CONTROLLED_SCRIPTS_ROOT), [])
        self.assertEqual(CHECK.check_migration_connection(), [])

    def test_a_bare_psycopg_connection_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "lingxi"
            (source / "adapters").mkdir(parents=True)
            (source / "adapters" / "postgres.py").write_text("# 工厂占位\n", encoding="utf-8")
            bad = source / "adapters" / "bad_store.py"
            bad.write_text(
                "import psycopg\n\n"
                "def open_store(dsn):\n"
                "    return psycopg.connect(dsn)\n",
                encoding="utf-8",
            )

            failures = CHECK.check_runtime_connections(source)

        self.assertTrue(failures)
        self.assertTrue(any("bad_store.py:1" in failure for failure in failures))
        self.assertTrue(any("裸 PostgreSQL 连接" in failure for failure in failures))

    def test_self_psycopg_connection_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "lingxi"
            (source / "adapters").mkdir(parents=True)
            (source / "adapters" / "postgres.py").write_text("# 工厂占位\n", encoding="utf-8")
            bad = source / "adapters" / "bad_store.py"
            bad.write_text(
                "class Bad:\n"
                "    def open(self):\n"
                "        return self._psycopg.connect(self._dsn)\n",
                encoding="utf-8",
            )

            failures = CHECK.check_runtime_connections(source)

        self.assertTrue(any("bad_store.py:3" in failure for failure in failures))

    def test_from_psycopg_connection_import_and_class_connect_is_rejected(self) -> None:
        """Issue #116：`from psycopg.connection import Connection` 的 module 是
        `psycopg.connection`，随后 `Connection.connect(dsn)` 也是裸建连——两处都必须
        变红，不能只靠 import 那一行报错就当作覆盖到位。"""

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "lingxi"
            (source / "adapters").mkdir(parents=True)
            (source / "adapters" / "postgres.py").write_text("# 工厂占位\n", encoding="utf-8")
            bad = source / "adapters" / "bad_store.py"
            bad.write_text(
                "from psycopg.connection import Connection\n\n"
                "def open_store(dsn):\n"
                "    return Connection.connect(dsn)\n",
                encoding="utf-8",
            )

            failures = CHECK.check_runtime_connections(source)

        self.assertTrue(failures)
        self.assertTrue(any("bad_store.py:1" in failure and "直接从 psycopg 导入" in failure for failure in failures))
        self.assertTrue(any("bad_store.py:4" in failure and "裸 PostgreSQL 连接" in failure for failure in failures))

    def test_import_psycopg_connection_submodule_and_connect_is_rejected(self) -> None:
        """`import psycopg.connection` 的 `name` 是 `psycopg.connection`，非字面量
        `"psycopg"`；绑定的本地名字仍是顶层 `psycopg`，随后 `psycopg.connect(dsn)`
        同样是裸建连，两处都必须变红（Issue #116）。"""

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "lingxi"
            (source / "adapters").mkdir(parents=True)
            (source / "adapters" / "postgres.py").write_text("# 工厂占位\n", encoding="utf-8")
            bad = source / "adapters" / "bad_store.py"
            bad.write_text(
                "import psycopg.connection\n\n"
                "def open_store(dsn):\n"
                "    return psycopg.connect(dsn)\n",
                encoding="utf-8",
            )

            failures = CHECK.check_runtime_connections(source)

        self.assertTrue(failures)
        self.assertTrue(any("bad_store.py:1" in failure and "直接导入 psycopg" in failure for failure in failures))
        self.assertTrue(any("bad_store.py:4" in failure and "裸 PostgreSQL 连接" in failure for failure in failures))

    def test_unrelated_psycopg_submodule_import_is_not_flagged(self) -> None:
        """`psycopg.types.json` 等与建连无关的子模块不应被这条门禁误杀——
        `adapters/galaxy_import.py`、`adapters/postgres_identity.py` 已经在用
        `from psycopg.types.json import Json/Jsonb`（Issue #116 加固前的真实用法）。"""

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "lingxi"
            (source / "adapters").mkdir(parents=True)
            (source / "adapters" / "postgres.py").write_text("# 工厂占位\n", encoding="utf-8")
            fine = source / "adapters" / "fine_store.py"
            fine.write_text(
                "from psycopg.types.json import Json\n\n"
                "def wrap(value):\n"
                "    return Json(value)\n",
                encoding="utf-8",
            )

            failures = CHECK.check_runtime_connections(source)

        self.assertEqual(failures, [])


class MigrationTimeoutConfigTest(unittest.TestCase):
    def test_migration_has_an_explicit_finite_exception_configuration(self) -> None:
        self.assertEqual(MIGRATION_DSN.MIGRATION_CONNECT_TIMEOUT_SECONDS, 5)
        self.assertEqual(MIGRATION_DSN.MIGRATION_STATEMENT_TIMEOUT_SECONDS, 60)
        self.assertEqual(MIGRATION_DSN.MIGRATION_LOCK_TIMEOUT_SECONDS, 10)
        self.assertEqual(
            MIGRATION_DSN.migration_connect_args(),
            {
                "connect_timeout": 5,
                "options": "-c statement_timeout=60s -c lock_timeout=10s",
            },
        )


if __name__ == "__main__":
    unittest.main()
