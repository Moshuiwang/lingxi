"""Issue #75：数据库连接、语句和锁超时的仓库级约定与会变红门禁。"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import textwrap
import threading
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
from lingxi.adapters.postgres_conversation import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS as GATEWAY_DEFAULT_CONNECT_TIMEOUT_SECONDS,
)
from lingxi.adapters.postgres_conversation import (
    PostgresGatewayStore,
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
            (
                config.connect_timeout_seconds,
                config.statement_timeout_seconds,
                config.lock_timeout_seconds,
            ),
            (MAX_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS),
        )

    def test_missing_environment_values_keep_finite_defaults(self) -> None:
        config = PostgresTimeouts.from_env({})
        self.assertEqual(config, DEFAULT_POSTGRES_TIMEOUTS)

    def test_invalid_environment_values_are_rejected_without_unbounded_fallback(self) -> None:
        for raw in ("0", "-1", str(MAX_TIMEOUT_SECONDS + 1), "not-a-number"):
            with self.subTest(raw=raw):
                with self.assertRaises(PostgresTimeoutConfigError):
                    PostgresTimeouts.from_env({"LINGXI_POSTGRES_STATEMENT_TIMEOUT_SECONDS": raw})

    def test_retention_adapter_statement_timeout_exceeds_function_lock_wait_budget(self) -> None:
        """清理函数的两次 2s 锁等待必须先于适配器级 statement_timeout 返回。"""

        lock_wait_budget = (
            RETENTION_FUNCTION_LOCK_WAIT_COUNT * RETENTION_FUNCTION_LOCK_TIMEOUT_SECONDS
        )
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

    def test_gateway_store_forwards_its_default_connect_timeout(self) -> None:
        """Issue #248 缺口二：``postgres_conversation`` 包自己转发的默认建连超时
        必须真的进了 ``PostgresGatewayStore`` 默认构造出的 ``PostgresTimeouts``，
        不能只是模块顶层一个没人核对的「兼容导出」常量。

        上面 ``test_factory_always_passes_all_three_boundaries_to_psycopg`` 只证明
        连接工厂（``adapters.postgres.connect``）拿到什么 ``timeouts`` 就转发给
        psycopg 什么——它压根不经过 ``PostgresGatewayStore.__init__`` 的默认构造
        路径，因此就算这个包把默认值改错（例如把
        ``_gateway_store.DEFAULT_CONNECT_TIMEOUT_SECONDS`` 悄悄改大），那条测试
        仍然全绿。代码框架「PostgreSQL 连接与超时」明写：5 秒上界是由 scheduler
        150 秒停机宽限反推出来的，不是随手定的数字，悄悄放大它必须有断言变红。
        """

        self.assertEqual(GATEWAY_DEFAULT_CONNECT_TIMEOUT_SECONDS, 5)

        store = PostgresGatewayStore("postgresql://test/db")

        self.assertEqual(store._timeouts.connect_timeout_seconds, 5)

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

    def test_repository_connection_borrow_scope_passes_and_is_not_vacuous(self) -> None:
        """全仓默认复用调用点一处不改即全绿；计数为正，证明扫描确实识别到了工厂调用。"""

        runtime = CHECK.scan_connection_borrow_scope()
        scripts = CHECK.scan_connection_borrow_scope(CHECK.CONTROLLED_SCRIPTS_ROOT)
        self.assertEqual(runtime.failures, [])
        self.assertEqual(scripts.failures, [])
        self.assertGreater(runtime.reused_calls, 0)
        self.assertGreater(runtime.dedicated_calls + scripts.dedicated_calls, 0)
        self.assertEqual(runtime.exempted_calls + scripts.exempted_calls, 0)

    def test_a_bare_psycopg_connection_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "lingxi"
            (source / "adapters").mkdir(parents=True)
            (source / "adapters" / "postgres.py").write_text("# 工厂占位\n", encoding="utf-8")
            bad = source / "adapters" / "bad_store.py"
            bad.write_text(
                "import psycopg\n\ndef open_store(dsn):\n    return psycopg.connect(dsn)\n",
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
        self.assertTrue(
            any(
                "bad_store.py:1" in failure and "直接从 psycopg 导入" in failure
                for failure in failures
            )
        )
        self.assertTrue(
            any(
                "bad_store.py:4" in failure and "裸 PostgreSQL 连接" in failure
                for failure in failures
            )
        )

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
        self.assertTrue(
            any(
                "bad_store.py:1" in failure and "直接导入 psycopg" in failure
                for failure in failures
            )
        )
        self.assertTrue(
            any(
                "bad_store.py:4" in failure and "裸 PostgreSQL 连接" in failure
                for failure in failures
            )
        )

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
                "from psycopg.types.json import Json\n\ndef wrap(value):\n    return Json(value)\n",
                encoding="utf-8",
            )

            failures = CHECK.check_runtime_connections(source)

        self.assertEqual(failures, [])


FACTORY_IMPORT = "from lingxi.adapters.postgres import connect\n"


def _borrow_scope(code: str, **kwargs) -> tuple[list[str], int, int]:
    """把一份样本写成临时包里的适配器文件，跑借用围栏，返回 (判红清单, 默认复用数, 独占数)。"""

    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "lingxi"
        (source / "adapters").mkdir(parents=True)
        (source / "adapters" / "postgres.py").write_text("# 工厂占位\n", encoding="utf-8")
        (source / "adapters" / "sample.py").write_text(textwrap.dedent(code), encoding="utf-8")
        scan = CHECK.scan_connection_borrow_scope(source, **kwargs)
    return scan.failures, scan.reused_calls, scan.dedicated_calls


def _borrow_scope_within(seconds: float, code: str) -> tuple[list[str], int, int]:
    """在独立线程里跑围栏：到时未结束即判失败（门禁挂死也要有确定的红），异常原样抛出。"""

    outcome: list[object] = []

    def run() -> None:
        try:
            outcome.append(_borrow_scope(code))
        except Exception as error:
            outcome.append(error)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise AssertionError(f"围栏扫描 {seconds} 秒内没有结束（别名收集挂死）")
    result = outcome[0]
    if isinstance(result, Exception):
        raise result
    assert isinstance(result, tuple)
    return result


class ConnectionBorrowScopeGateTest(unittest.TestCase):
    """连接借用围栏的正反例：默认复用路径的 ``connect()`` 与其游标不得逃出 ``with`` 块。

    变异实测：把 ``_scan_borrow_scope_file`` 里 ``if id(node) in context_ids`` 改成恒真，
    下面四条违规样本里「裸 connect() 赋值后使用」必红；把 ``_escapes_inside`` /
    ``_uses_after`` 改成直接 ``return []``，其余三条必红。把 ``_bind`` 改回无条件覆盖，
    别名振荡用例抛错（再去掉轮数上界则超时）、跨作用域覆盖用例变绿；把
    ``_non_name_with_targets`` 改成 ``return []``、``_carries_handle`` 改成只认名字与游标
    表达式，``as`` 目标用例与容器字面量用例各自变绿。
    """

    # ---- 四种违规样本，各判红一次

    def test_a_bare_connect_assigned_and_used_is_red(self) -> None:
        failures, reused, _ = _borrow_scope(
            FACTORY_IMPORT
            + "def f(dsn):\n    c = connect(dsn)\n    c.execute('SELECT 1')\n    c.close()\n"
        )
        self.assertEqual(reused, 1)
        self.assertEqual(len(failures), 1)
        self.assertIn("sample.py:3 默认复用路径的 connect() 没有写在 with 语句", failures[0])

    def test_connect_again_after_close_inside_the_with_block_is_red(self) -> None:
        failures, _, _ = _borrow_scope(
            FACTORY_IMPORT
            + "def f(dsn):\n"
            + "    with connect(dsn) as c:\n"
            + "        c.execute('SELECT 1')\n"
            + "        c.close()\n"
            + "        with connect(dsn) as d:\n"
            + "            d.execute('SELECT 2')\n"
        )
        self.assertEqual(len(failures), 1)
        self.assertIn(
            "sample.py:6 with 块内已 close() 归还的连接，同一块内又取 connect()", failures[0]
        )

    def test_a_cursor_stored_on_self_is_red(self) -> None:
        for expression in ("c.cursor()", "c.execute('SELECT 1')", "c"):
            with self.subTest(expression=expression):
                failures, _, _ = _borrow_scope(
                    FACTORY_IMPORT
                    + "class Store:\n"
                    + "    def f(self, dsn):\n"
                    + "        with connect(dsn) as c:\n"
                    + f"            self.kept = {expression}\n"
                )
                self.assertEqual(len(failures), 1)
                self.assertIn("sample.py:5 连接或游标存进了属性 / 容器", failures[0])

    def test_a_cursor_escaping_the_with_block_and_executing_is_red(self) -> None:
        failures, _, _ = _borrow_scope(
            FACTORY_IMPORT
            + "def f(dsn):\n"
            + "    with connect(dsn) as c:\n"
            + "        cur = c.cursor()\n"
            + "    cur.execute('SELECT 1')\n"
            + "    return cur.fetchone()\n"
        )
        self.assertEqual(
            [failure.split(" ", 1)[0] for failure in failures],
            ["lingxi/adapters/sample.py:5", "lingxi/adapters/sample.py:6"],
        )
        self.assertTrue(all("名字 cur 指向的连接或游标仍在使用" in failure for failure in failures))

    # ---- 同类逃逸：归还后再用连接、随 return 逃出、global / nonlocal

    def test_using_the_connection_after_the_with_block_is_red(self) -> None:
        failures, _, _ = _borrow_scope(
            FACTORY_IMPORT
            + "def f(dsn):\n"
            + "    with connect(dsn) as c:\n"
            + "        c.execute('SELECT 1')\n"
            + "    c.commit()\n"
        )
        self.assertEqual(len(failures), 1)
        self.assertIn("sample.py:5 with 块已退出，名字 c 指向的连接或游标仍在使用", failures[0])

    def test_returning_a_cursor_from_inside_the_with_block_is_red(self) -> None:
        failures, _, _ = _borrow_scope(
            FACTORY_IMPORT
            + "def f(dsn):\n    with connect(dsn) as c:\n        return c.execute('SELECT 1')\n"
        )
        self.assertEqual(len(failures), 1)
        self.assertIn("sample.py:4 连接或游标随 return 逃出 with 块", failures[0])

    def test_a_cursor_assigned_to_a_global_or_nonlocal_name_is_red(self) -> None:
        failures, _, _ = _borrow_scope(
            FACTORY_IMPORT
            + "KEPT = None\n"
            + "def f(dsn):\n"
            + "    global KEPT\n"
            + "    with connect(dsn) as c:\n"
            + "        KEPT = c.cursor()\n"
        )
        self.assertEqual(len(failures), 1)
        self.assertIn("sample.py:6 游标或连接赋给了 with 块外的名字", failures[0])

    # ---- 正例，各判绿

    def test_the_standard_with_block_is_green(self) -> None:
        failures, reused, dedicated = _borrow_scope(
            FACTORY_IMPORT
            + "def f(dsn):\n"
            + "    with connect(dsn) as connection:\n"
            + "        with connection.cursor() as cursor:\n"
            + "            cursor.execute('SELECT 1')\n"
            + "            rows = cursor.fetchall()\n"
            + "        first = connection.execute('SELECT 2').fetchone()\n"
            + "    with connect(dsn) as connection:\n"
            + "        connection.execute('SELECT 3')\n"
            + "    return rows, first\n"
        )
        self.assertEqual(failures, [])
        self.assertEqual((reused, dedicated), (2, 0))

    def test_dedicated_connections_are_outside_the_fence(self) -> None:
        for arguments in ("dedicated=True", "autocommit=True, dedicated=True"):
            with self.subTest(arguments=arguments):
                failures, reused, dedicated = _borrow_scope(
                    FACTORY_IMPORT
                    + "class Holder:\n"
                    + "    def open(self, dsn):\n"
                    + f"        self._connection = connect(dsn, {arguments})\n"
                )
                self.assertEqual(failures, [])
                self.assertEqual((reused, dedicated), (0, 1))

    def test_closing_inside_the_with_block_without_another_connect_is_green(self) -> None:
        failures, _, _ = _borrow_scope(
            FACTORY_IMPORT
            + "def f(dsn):\n"
            + "    with connect(dsn) as c:\n"
            + "        c.execute('SELECT 1')\n"
            + "        c.close()\n"
        )
        self.assertEqual(failures, [])

    def test_all_import_aliases_are_recognised_green_in_with_and_red_outside(self) -> None:
        """别名 import 是威胁模型第一条：三种形态下 with 内判绿、赋值即判红，一条都不能漏。"""

        forms = (
            ("from lingxi.adapters.postgres import connect as open_db\n", "open_db"),
            ("import lingxi.adapters.postgres as pg\n", "pg.connect"),
            ("from lingxi.adapters import postgres\n", "postgres.connect"),
            ("import lingxi.adapters.postgres\n", "lingxi.adapters.postgres.connect"),
            ("from .postgres import connect\n", "connect"),
            ("from lingxi.adapters import postgres\n", "getattr(postgres, 'connect')"),
        )
        for header, call in forms:
            with self.subTest(call=call):
                green, reused, _ = _borrow_scope(
                    header
                    + f"def f(dsn):\n    with {call}(dsn) as c:\n        c.execute('SELECT 1')\n"
                )
                self.assertEqual(green, [])
                self.assertEqual(reused, 1)
                red, _, _ = _borrow_scope(header + f"def f(dsn):\n    c = {call}(dsn)\n")
                self.assertEqual(len(red), 1)
                self.assertIn("sample.py:3 默认复用路径的 connect()", red[0])

    def test_kwargs_unpacking_or_dynamic_dedicated_cannot_bypass_the_fence(self) -> None:
        """``**kwargs`` 与 ``dedicated=<变量>`` 静态判不出走哪条路，按默认复用路径要求。"""

        for call in ("connect(dsn, **options)", "connect(dsn, dedicated=flag)"):
            with self.subTest(call=call):
                failures, reused, dedicated = _borrow_scope(
                    FACTORY_IMPORT + f"def f(dsn, options, flag):\n    c = {call}\n"
                )
                self.assertEqual((reused, dedicated), (1, 0))
                self.assertEqual(len(failures), 1)

    def test_a_wrapper_around_the_factory_is_not_mistaken_for_it(self) -> None:
        failures, reused, _ = _borrow_scope(
            "from lingxi.adapters.innertest_request import connect\n"
            "def f(dsn):\n    c = connect(dsn)\n"
        )
        self.assertEqual((failures, reused), ([], 0))

    # ---- 别名收集只升不降：振荡片段 2 秒内结束、跨作用域覆盖不漏判、不收敛就响亮失败

    def test_a_name_rebound_between_two_factory_members_settles_fast_and_is_red(self) -> None:
        """同一个名字先后赋给工厂的两个成员曾让别名收集每轮互相覆盖、门禁挂死；现在绑定
        只升不降，顺序、反序、if / else 三种写法都必须 2 秒内结束，且曾绑到 connect 的名字
        按工厂审查（宁可多判）。"""

        header = "from lingxi.adapters import postgres as pg\n"
        samples = {
            "顺序": "alias = pg.connect\nalias = pg.PostgresTimeouts\n",
            "反序": "alias = pg.PostgresTimeouts\nalias = pg.connect\n",
            "if / else": "if pg:\n    alias = pg.connect\nelse:\n    alias = pg.PostgresTimeouts\n",
        }
        for label, body in samples.items():
            with self.subTest(sample=label):
                code = header + body + "def f(dsn):\n    c = alias(dsn)\n"
                call_line = code.count("\n")
                failures, reused, _ = _borrow_scope_within(2.0, code)
                self.assertEqual(reused, 1)
                self.assertEqual(len(failures), 1)
                self.assertIn(f"sample.py:{call_line} 默认复用路径的 connect()", failures[0])

    def test_a_non_factory_import_in_another_scope_does_not_unbind_the_factory_alias(
        self,
    ) -> None:
        """``open_db`` 在一个作用域绑到工厂后，另一个作用域 ``from elsewhere import connect
        as open_db`` 不得把它覆盖成非工厂——不论先后，两处 ``open_db(...)`` 都按默认复用
        路径审查（宁可多判）。"""

        factory_first = (
            "from lingxi.adapters.postgres import connect as open_db\n"
            "def a(dsn):\n    c = open_db(dsn)\n"
            "def b():\n    from elsewhere import connect as open_db\n    return open_db('x')\n"
        )
        factory_last = (
            "from elsewhere import connect as open_db\n"
            "def a(dsn):\n    c = open_db(dsn)\n"
            "def b():\n    from lingxi.adapters.postgres import connect as open_db\n"
            "    return open_db('x')\n"
        )
        for label, code in (("工厂在前", factory_first), ("工厂在后", factory_last)):
            with self.subTest(order=label):
                failures, reused, _ = _borrow_scope(code)
                self.assertEqual(reused, 2)
                self.assertEqual(
                    [failure.split(" ", 1)[0] for failure in failures],
                    ["lingxi/adapters/sample.py:3", "lingxi/adapters/sample.py:6"],
                )
                self.assertTrue(all("默认复用路径的 connect()" in f for f in failures))

    def test_alias_collection_that_never_settles_fails_loudly(self) -> None:
        """轮数上界是兜底：把登记函数换回「无条件覆盖、每次都算改动」，扫描必须抛错，
        而不是静默返回或挂死。"""

        def overwrite(bindings: dict[str, str], name: str, canonical: str) -> bool:
            bindings[name] = canonical
            return True

        with mock.patch.object(CHECK, "_bind", overwrite):
            with self.assertRaisesRegex(RuntimeError, "别名收集没有收敛"):
                _borrow_scope_within(2.0, FACTORY_IMPORT + "alias = connect\n")

    # ---- ``as`` 目标与容器字面量

    def test_binding_the_connection_to_anything_but_a_name_is_red(self) -> None:
        """``as self.connection`` / ``as store["c"]`` 让句柄一落地就在块外可达，判红；
        ``as c`` 与不写 ``as`` 照旧判绿。"""

        for target, expected in (("self.connection", 1), ("store['c']", 1), ("c", 0), ("", 0)):
            with self.subTest(target=target or "无 as"):
                clause = f" as {target}" if target else ""
                failures, reused, _ = _borrow_scope(
                    FACTORY_IMPORT
                    + "class Store:\n"
                    + "    def f(self, dsn, store):\n"
                    + f"        with connect(dsn){clause}:\n"
                    + "            pass\n"
                )
                self.assertEqual(reused, 1)
                self.assertEqual(len(failures), expected)
                if expected:
                    self.assertIn("sample.py:4 连接绑定到属性 / 下标 / 解包目标", failures[0])

    def test_a_container_literal_carrying_the_connection_or_cursor_escapes_too(self) -> None:
        """装进 Dict / List / Tuple / Set 字面量（含嵌套）再 return 或存进属性，与直接逃出
        同判；不含句柄的容器照旧判绿。"""

        for expression in (
            "{'connection': c, 'cursor': c.cursor()}",
            "[c.execute('SELECT 1')]",
            "(rows, c)",
            "{c}",
            "{'pair': (rows, [c.cursor()])}",
        ):
            with self.subTest(expression=expression):
                failures, _, _ = _borrow_scope(
                    FACTORY_IMPORT
                    + "def f(dsn):\n"
                    + "    with connect(dsn) as c:\n"
                    + "        rows = c.execute('SELECT 1').fetchall()\n"
                    + f"        return {expression}\n"
                )
                self.assertEqual(len(failures), 1)
                self.assertIn("sample.py:5 连接或游标随 return 逃出 with 块", failures[0])
        failures, _, _ = _borrow_scope(
            FACTORY_IMPORT
            + "class Store:\n"
            + "    def f(self, dsn):\n"
            + "        with connect(dsn) as c:\n"
            + "            self.kept = [c]\n"
        )
        self.assertEqual(len(failures), 1)
        self.assertIn("sample.py:5 连接或游标存进了属性 / 容器", failures[0])
        failures, _, _ = _borrow_scope(
            FACTORY_IMPORT
            + "def f(dsn):\n"
            + "    with connect(dsn) as c:\n"
            + "        rows = c.execute('SELECT 1').fetchall()\n"
            + "    return {'rows': rows}\n"
        )
        self.assertEqual(failures, [])

    # ---- 豁免清单：只读常量、精确到行、运行期塞不进去

    def test_the_exemption_list_is_empty_read_only_and_line_exact(self) -> None:
        self.assertEqual(dict(CHECK.BORROW_SCOPE_EXEMPTIONS), {})
        with self.assertRaises(TypeError):
            CHECK.BORROW_SCOPE_EXEMPTIONS[("lingxi/adapters/sample.py", 3)] = "运行期塞进来的豁免"

        code = FACTORY_IMPORT + "def f(dsn):\n    c = connect(dsn)\n    d = connect(dsn)\n"
        explicit = types.MappingProxyType({("lingxi/adapters/sample.py", 3): "试验用"})
        failures, _, _ = _borrow_scope(code, exemptions=explicit)
        self.assertEqual(len(failures), 1, "豁免只放行登记的那一行，相邻一行照红")
        self.assertIn("sample.py:4", failures[0])

    def test_rebinding_the_module_level_list_at_runtime_does_not_reach_the_scan(self) -> None:
        """`__main__` 里重绑常量也没用：扫描函数用的是定义时绑定的默认值。"""

        code = FACTORY_IMPORT + "def f(dsn):\n    c = connect(dsn)\n"
        with mock.patch.object(
            CHECK, "BORROW_SCOPE_EXEMPTIONS", {("lingxi/adapters/sample.py", 3): "运行期重绑"}
        ):
            failures, _, _ = _borrow_scope(code)
        self.assertEqual(len(failures), 1)


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
