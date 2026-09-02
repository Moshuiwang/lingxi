"""`scripts/ops/import_local_permission_override.py` 的真库断言（Issue #441）。

与 ``tests/test_permission_refresh_duty.py`` / ``tests/test_permission_refresh_postgres.py``
同一分工：单元测试（``tests/test_import_local_permission_override.py``）钉纯逻辑
（差集计算、用户级判定编排、CSV 解析），本文件钉**只有真库能证伪**的那半——
``local_permission_override``/``pending_action`` 两张表的外键与唯一索引是否真的
被满足、幂等重跑是否真的零新增行、默认（不传 ``--apply``）与 ``--dry-run`` 是否
真的都不写任何一行、只有 ``--apply`` 才真正落库。

数据全部为虚构化名，不含任何真实导出内容。
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.adapters.admin_registry import seed_admin_registry_entry
from lingxi.adapters.galaxy_import import PostgresGalaxyImportStore
from lingxi.adapters.postgres import connect

SCRIPT = Path(__file__).parents[1] / "scripts" / "ops" / "import_local_permission_override.py"


def _load_script():
    module_name = "import_local_permission_override_under_test_pg"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


TOOL = _load_script()

SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，旧表差集导入工具的真库断言未验证（需真实 PostgreSQL 16）"
    if not os.environ.get("LINGXI_POSTGRES_DSN")
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，旧表差集导入工具的真库断言未验证"
)

USER_ID = "usr_import_target"
EMPLOYEE_NO = "20001"
EMAIL = "import.target@example.invalid"
FEISHU_OPEN_ID = "ou_import_target"
INITIATED_BY = "ou_operator"

ROLE_FUNCTION_MAP = {"A运营": "运营"}
GALAXY_METRIC = "银河已给指标"
LEGACY_ONLY_METRIC = "仅旧表遗留指标"
METRIC_TRANSLATION_MAP = {"BC-甲": {"运营": (GALAXY_METRIC,)}}


def _galaxy_tables() -> dict[str, list[dict[str, str]]]:
    return {
        "user": [
            {
                "user_id": "G-20001",
                "dept_id": "D1",
                "user_name": EMPLOYEE_NO,
                "nick_name": "导入目标",
                "email": EMAIL,
                "create_time": "2019-01-02 03:04:05",
            }
        ],
        "user_role": [
            {"user_id": "G-20001", "role_id": "R-甲", "user_name": "导入目标", "role_name": "A运营"}
        ],
        "role_menu": [{"role_id": "R-甲", "menu_id": "M1", "role_name": "A运营", "menu_name": "报表"}],
        "sys_user_datacountry": [
            {"USER_ID": "G-20001", "DATACOUNTRY_ID": "101", "USER_NAME": "导入目标", "DATACOUNTRY_NAME": "甲国"}
        ],
        "sys_country": [
            {
                "id": "7", "country_key": "101", "name": "ALPHA", "code": "AL",
                "name_cn": "甲国", "region_key": "1", "region_name": "甲区", "boss_company_id": "BC-甲",
            }
        ],
    }


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN") and psycopg_available(), SKIP_REASON)
class ImportLocalPermissionOverridePostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        reset_production_rows(self._dsn)
        # rc25 S-2d（对抗审查 P-8）：``--initiated-by`` 必须是一位生效的已登记管理员，
        # 否则整次运行在读导出之前就被拒（退出码 2、零写入）。真库用例因此要先把这个
        # 责任人登记进 ``admin_registry``——用产品自己的种子函数，不手拼 INSERT。
        seed_admin_registry_entry(self._dsn, feishu_open_id=INITIATED_BY, label="导入责任人（测试）")
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO app_user
                     (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name,
                      department, tenant_key, employee_no, email,
                      provisioning_state, account_state)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', 'enabled')""",
                (
                    USER_ID, FEISHU_OPEN_ID, "on_import_target", "un_import_target", "导入目标",
                    "测试部门", "tenant-fake", EMPLOYEE_NO, EMAIL,
                ),
            )
        PostgresGalaxyImportStore(self._dsn).import_export(
            source_label="合成导出（测试）", source_digest="digest-import-tool", tables=_galaxy_tables()
        )
        self._legacy_csv = self._write_legacy_csv(
            f'{{"BC-甲": ["{GALAXY_METRIC}", "{LEGACY_ONLY_METRIC}"]}}'
        )
        # 不用随包发布的生产翻译配置——那份文件不认识本文件虚构的 "A运营"/"BC-甲"，
        # 会让每一次匹配都在翻译层 fail-closed 掉（`metric_translation_uncovered`），
        # 与"银河没给"混在一起分不清。用 `--role-function-map`/`--metric-translation-map`
        # 显式指向这两份测试夹具专用的最小配置文件。
        self._role_function_map_path = self._write_toml(
            'role_function_map', '[roles]\n"A运营" = "运营"\n'
        )
        self._metric_translation_map_path = self._write_toml(
            'metric_translation_map', f'[companies."BC-甲"]\n"运营" = ["{GALAXY_METRIC}"]\n'
        )

    def _write_legacy_csv(self, permissions_json: str) -> Path:
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8", newline=""
        )
        handle.write("email,permissions\n")
        handle.write(f'{EMAIL},"{permissions_json.replace(chr(34), chr(34) * 2)}"\n')
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        return Path(handle.name)

    def _write_toml(self, label: str, text: str) -> Path:
        handle = tempfile.NamedTemporaryFile(
            mode="w", prefix=f"{label}-", suffix=".toml", delete=False, encoding="utf-8"
        )
        handle.write(text)
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        return Path(handle.name)

    def _count_active_overrides(self) -> int:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM local_permission_override WHERE entry_status = 'active'"
            )
            return int(cursor.fetchone()[0])

    def _run(self, *extra_args: str) -> int:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = TOOL.main(
                [
                    str(self._legacy_csv), "--initiated-by", INITIATED_BY, "--dsn", self._dsn,
                    "--role-function-map", str(self._role_function_map_path),
                    "--metric-translation-map", str(self._metric_translation_map_path),
                    *extra_args,
                ]
            )
        self._last_output = buffer.getvalue()
        return code

    # ---- 差集只导入旧表多出来的那一份 ------------------------------------

    def test_the_diff_is_imported_not_the_full_legacy_content(self) -> None:
        code = self._run("--apply")

        self.assertEqual(code, 0)
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT company_id, metric_name, reason, direction, entry_status"
                " FROM local_permission_override WHERE user_id = %s",
                (USER_ID,),
            )
            rows = cursor.fetchall()
        self.assertEqual(len(rows), 1, "银河已给的那个指标不该被重复导入")
        company_id, metric_name, reason, direction, entry_status = rows[0]
        self.assertEqual(company_id, "BC-甲")
        self.assertEqual(metric_name, LEGACY_ONLY_METRIC)
        self.assertEqual(reason, TOOL.IMPORT_REASON)
        self.assertEqual(direction, "grant")
        self.assertEqual(entry_status, "active")

    def test_the_backing_pending_action_is_a_terminal_executed_grant(self) -> None:
        self._run("--apply")

        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pa.action_type, pa.status, pa.card_delivered, pa.initiated_by_open_id,"
                "       pa.decided_by_open_id, pa.target_open_id, pa.payload"
                "  FROM local_permission_override lpo"
                "  JOIN pending_action pa ON pa.id = lpo.pending_action_id"
                " WHERE lpo.user_id = %s",
                (USER_ID,),
            )
            row = cursor.fetchone()
        self.assertIsNotNone(row, "local_permission_override 的 pending_action_id 必须指向一条真实存在的行")
        action_type, status, card_delivered, initiated_by, decided_by, target_open_id, payload = row
        self.assertEqual(action_type, TOOL.ACTION_TYPE_GRANT)
        self.assertEqual(status, "executed")
        self.assertFalse(card_delivered, "本工具从未真的发过确认卡")
        self.assertEqual(initiated_by, INITIATED_BY)
        self.assertEqual(decided_by, INITIATED_BY)
        self.assertEqual(target_open_id, FEISHU_OPEN_ID)
        self.assertIn(LEGACY_ONLY_METRIC, payload)

    # ---- 写入极性：默认与 --dry-run 都不写任何一行，只有 --apply 才写 -------

    def test_default_without_apply_writes_nothing(self) -> None:
        """rc21 修复包 B（P1+P2+P3 之 c，写入极性反转）：不传任何写入相关的
        参数时，默认行为等价于旧版的 ``--dry-run``——只出计划，不落库。

        变异存活证据：把 `main()` 里 `if not arguments.apply: ...; return 0`
        这条早退删掉，本用例会从"零行"变红成"一行"（默认又变回真正写入）。
        """

        code = self._run()

        self.assertEqual(code, 0)
        self.assertEqual(self._count_active_overrides(), 0)
        self.assertIn(EMAIL, self._last_output)
        self.assertIn(LEGACY_ONLY_METRIC, self._last_output)

    def test_dry_run_writes_nothing(self) -> None:
        """``--dry-run`` 是默认行为（见上一条用例）的兼容别名——旧的调用脚本
        显式传这个参数，行为必须与不传任何参数完全一致。"""

        code = self._run("--dry-run")

        self.assertEqual(code, 0)
        self.assertEqual(self._count_active_overrides(), 0)
        self.assertIn(EMAIL, self._last_output)
        self.assertIn(LEGACY_ONLY_METRIC, self._last_output)

    def test_apply_and_dry_run_together_is_treated_as_dry_run(self) -> None:
        """同时给出 `--apply` 与 `--dry-run`：按更保守的一侧处理，不写入。"""

        code = self._run("--apply", "--dry-run")

        self.assertEqual(code, 0)
        self.assertEqual(self._count_active_overrides(), 0)

    # ---- 幂等：同一份导出反复执行，新增行数恒为零 --------------------------

    def test_rerunning_the_same_export_creates_no_duplicate_rows(self) -> None:
        first_code = self._run("--apply")
        first_count = self._count_active_overrides()

        second_code = self._run("--apply")
        second_count = self._count_active_overrides()

        third_code = self._run("--apply")
        third_count = self._count_active_overrides()

        self.assertEqual((first_code, second_code, third_code), (0, 0, 0))
        self.assertEqual(first_count, 1)
        self.assertEqual(second_count, first_count, "重跑不得产生重复行")
        self.assertEqual(third_count, first_count, "重跑不得产生重复行")

        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM pending_action WHERE action_type = %s", (TOOL.ACTION_TYPE_GRANT,))
            pending_action_count = int(cursor.fetchone()[0])
        self.assertEqual(
            pending_action_count, 1, "已存在的授权不得为每次重跑各自留下一条孤儿 pending_action"
        )

    # ---- 银河已经完全覆盖旧表时零导入 --------------------------------------

    def test_a_user_fully_covered_by_galaxy_gets_nothing_imported(self) -> None:
        self._legacy_csv = self._write_legacy_csv(f'{{"BC-甲": ["{GALAXY_METRIC}"]}}')

        code = self._run("--apply")

        self.assertEqual(code, 0)
        self.assertEqual(self._count_active_overrides(), 0)


if __name__ == "__main__":
    unittest.main()
