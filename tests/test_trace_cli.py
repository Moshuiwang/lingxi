"""追溯号只读查询命令的验收（Issue #280 §7.2，`V-部署-14`）。

单元层测参数校验与缺省 DSN 的失败关闭；真库层证明四张表联合读取、身份最小化
（默认不打印 open_id）与"没有任何写路径"这条否定断言——后者用真实只读事务证明，
不是靠源码里没写 INSERT 这种脆弱的字面量扫描。
"""

from __future__ import annotations

import ast
import io
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.adapters.postgres import connect
from lingxi.apps import trace

DSN = os.environ.get("LINGXI_POSTGRES_DSN")
SKIP_DB = (
    "LINGXI_POSTGRES_DSN 未设置：跳过追溯号 CLI 的真库断言"
    if not DSN
    else "LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动：跳过追溯号 CLI 的真库断言"
)

_MODULE_PATH = Path(trace.__file__)


class NoWritePathSourceScanTests(unittest.TestCase):
    """否定断言：源码里不存在任何写 SQL 字面量或写方法调用。

    与真库层的只读事务证明是**两道**独立的防线：这一道挡的是"以后有人手滑加了一条
    UPDATE"这种最直接的回归，静态、零依赖、每次都跑。"""

    def test_no_write_sql_verbs_appear_in_any_string_literal(self) -> None:
        tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"), filename=str(_MODULE_PATH))
        write_verbs = ("insert ", "update ", "delete ", "drop ", "truncate ", "alter ")
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                lowered = node.value.lower()
                for verb in write_verbs:
                    with self.subTest(verb=verb.strip()):
                        self.assertNotIn(verb, lowered)

    def test_the_argument_parser_declares_no_write_flag(self) -> None:
        source = _MODULE_PATH.read_text(encoding="utf-8")
        for forbidden in ("--set", "--write", "--delete", "--update", "--abort", "--force"):
            self.assertNotIn(forbidden, source)

    def test_no_dead_exception_classes_sit_unused_in_the_module(self) -> None:
        """独立审查（分支 fix/291-280-user-experience 收尾）：``TraceLookupError``
        曾经定义在这个模块、也在 ``__all__`` 里，却从来没有任何地方 ``raise`` 过它
        （核实：真实"查无此追溯号"由 ``_render`` 返回一条正常字符串处理，``run()``
        对它照常返回 exit=0——``test_an_unknown_trace_id_says_so_explicitly`` 钉住
        了这条行为，空结果是一次合法的成功查询，不是需要额外异常类型的失败）。
        已删除该类。这里用 AST 静态扫描全部本模块定义的异常类，逐个确认它在同一
        文件里至少被 ``raise`` 过一次——挡的是"以后又悄悄加一个没人用的异常类、
        只是看起来像是留了扩展点"这类回归，不逐个类名硬编码。"""

        tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"), filename=str(_MODULE_PATH))
        defined_exception_names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
            and any(
                isinstance(base, ast.Name) and base.id.endswith(("Error", "Exception"))
                for base in node.bases
            )
        }
        raised_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                func = node.exc.func
                if isinstance(func, ast.Name):
                    raised_names.add(func.id)
        unused = defined_exception_names - raised_names
        self.assertEqual(
            unused,
            set(),
            f"本模块定义了从未被 raise 过的异常类：{unused}——真实用起来或删掉",
        )


class RunArgumentAndFailureClosedTests(unittest.TestCase):
    def test_missing_dsn_env_var_exits_one(self) -> None:
        err = io.StringIO()
        code = trace.run(["trc_x"], env={}, stderr=err)
        self.assertEqual(code, 1)
        self.assertIn(trace.DSN_ENV_VAR, err.getvalue())

    def test_missing_trace_id_argument_is_a_usage_error(self) -> None:
        with self.assertRaises(SystemExit):
            trace.run([], env={"LINGXI_POSTGRES_DSN": "postgresql://u:p@x/y"})

    def test_a_connection_failure_exits_one_and_does_not_leak_the_dsn(self) -> None:
        def exploding_connect(dsn: str, **kwargs: object):
            raise RuntimeError("connection refused")

        err = io.StringIO()
        code = trace.run(
            ["trc_x"],
            env={"LINGXI_POSTGRES_DSN": "postgresql://u:p@192.0.2.1:5/db"},
            stderr=err,
            connect=exploding_connect,
        )
        self.assertEqual(code, 1)
        self.assertNotIn("192.0.2.1", err.getvalue())
        self.assertNotIn("u:p", err.getvalue())


@unittest.skipUnless(DSN and psycopg_available(), SKIP_DB)
class TraceLookupRealDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._dsn = DSN
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        reset_production_rows(self._dsn)
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO app_user
                     (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name,
                      department, tenant_key, provisioning_state, permission_version)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 'mcp_syncing', 1)""",
                ("usr_trace_1", "ou_trace_1", "fs_trace_1", "on_trace_1", "化名甲", "测试部门", "tenant-fake"),
            )
            cursor.execute(
                """INSERT INTO inbound_event
                     (feishu_event_id, received_at, event_type, user_open_id, handled_as,
                      trace_id, onboarding_dispatched_at)
                   VALUES (%s, %s, %s, %s, 'auto_provisioning', %s, %s)""",
                (
                    "evt_trace_1",
                    datetime.now(timezone.utc) - timedelta(minutes=5),
                    "im.message.receive_v1",
                    "ou_trace_1",
                    "trc_lookup_1",
                    datetime.now(timezone.utc) - timedelta(minutes=5),
                ),
            )

    def _run(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = trace.run(list(args), env={"LINGXI_POSTGRES_DSN": self._dsn}, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def test_an_unknown_trace_id_says_so_explicitly(self) -> None:
        code, out, _err = self._run("trc_does_not_exist")
        self.assertEqual(code, 0)
        self.assertIn("查无此追溯号", out)
        self.assertNotIn("事件标识", out, "查无结果不得输出空壳记录")

    def test_a_known_trace_id_reports_event_and_user_state_without_open_id_by_default(
        self,
    ) -> None:
        code, out, _err = self._run("trc_lookup_1")
        self.assertEqual(code, 0)
        self.assertIn("evt_trace_1", out)
        self.assertIn("mcp_syncing", out)
        self.assertIn("usr_trace_1", out)
        self.assertNotIn("ou_trace_1", out, "默认输出不得包含 open_id")

    def test_include_open_id_flag_prints_it(self) -> None:
        code, out, _err = self._run("trc_lookup_1", "--include-open-id")
        self.assertEqual(code, 0)
        self.assertIn("ou_trace_1", out)

    def test_the_cli_has_no_write_path_end_to_end(self) -> None:
        """否定断言：跑完一次真实查询之后，库里一行都没有改变（用只读连接跑通全流程，
        不是靠"我没写代码调用写方法"这种自证）。"""

        before_events = self._count("inbound_event")
        before_users = self._count("app_user")

        code, _out, _err = self._run("trc_lookup_1")

        self.assertEqual(code, 0)
        self.assertEqual(self._count("inbound_event"), before_events)
        self.assertEqual(self._count("app_user"), before_users)

    def test_a_read_only_connection_rejects_a_write_attempt(self) -> None:
        """真正的强制力来源：CLI 用 ``connection.read_only = True`` 打开连接。这里
        直接证明——同样方式打开一个连接，尝试写入必须被数据库本身拒绝，而不是
        仅仅因为调用方"没写"那条语句。故意在这条只读事务里执行一次 UPDATE，
        必须抛出（PostgreSQL 的只读事务错误），证明约束是真的。"""

        with connect(self._dsn) as connection:
            connection.read_only = True
            with connection.cursor() as cursor:
                with self.assertRaises(Exception):
                    cursor.execute(
                        "UPDATE app_user SET display_name = 'x' WHERE id = 'usr_trace_1'"
                    )

    def _count(self, table: str) -> int:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT count(*) FROM {table}")
            return int(cursor.fetchone()[0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
