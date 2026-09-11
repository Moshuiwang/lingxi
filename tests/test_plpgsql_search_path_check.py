"""`scripts/ci/check_plpgsql_search_path.py` 的判定用例（Issue #661）。

这份检查的价值全在它**会变红**：每一条都构造一份坏输入，断言它被具体地拒绝；
最后一组反过来跑真实仓库，钉住「基线与现状一致」，并钉住基线只能变短。
"""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[1]
SCRIPT = REPOSITORY_ROOT / "scripts" / "ci" / "check_plpgsql_search_path.py"
VERIFY_REPOSITORY = REPOSITORY_ROOT / "scripts" / "ci" / "verify_repository.sh"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CHECK = _load_module(SCRIPT, "plpgsql_search_path_check_under_test")

#: 基线的逐字快照：0096 之前已经存在、定义里不带 SET search_path 的 19 个函数
#: （#661 列出的 17 个触发器函数 + 0095 新增的 2 个）。清单只能变短。
PINNED_BASELINE = frozenset(
    {
        ("0054_retention_cleanup.py", "galaxy_import_batch_fix_expiry"),
        ("0057_gateway_tables.py", "inbound_event_fix_expiry"),
        ("0057_gateway_tables.py", "task_freeze_invariants"),
        ("0058_worker_execution.py", "queue_failure_notice_fix_expiry"),
        ("0059_delivery_outbox.py", "task_delivery_event_fix_expiry"),
        ("0064_permission_publish_outbox.py", "publish_outbox_fix_expiry"),
        ("0065_mcp_token_and_sync_check.py", "mcp_access_token_immutable"),
        ("0065_mcp_token_and_sync_check.py", "mcp_sync_check_fix_expiry"),
        ("0066_onboarding_notice_outbox.py", "onboarding_completion_notice_fix_expiry"),
        ("0069_innertest_content_capture.py", "innertest_content_capture_fix_expiry"),
        ("0074_task_document_delivery.py", "task_document_delivery_request_fix_expiry"),
        ("0088_outreach_message.py", "outreach_message_freeze_anchors"),
        ("0089_carrier_retention.py", "pending_action_fix_retention_expiry"),
        ("0095_contact_reachability.py", "app_user_adopt_prior_inbound"),
        ("0095_contact_reachability.py", "app_user_record_real_inbound"),
        ("20260806_baseline_006_012.py", "app_user_reject_delegated_subject"),
        ("20260806_baseline_006_012.py", "credential_reject_app_user_subject"),
        ("20260806_baseline_006_012.py", "feishu_org_sync_run_fix_expiry"),
        ("20260806_baseline_006_012.py", "feishu_org_sync_run_verify_children"),
    }
)

GOOD = """
CREATE OR REPLACE FUNCTION demo_fix_expiry() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    NEW.expires_at := NEW.started_at + INTERVAL '2160 hours';
    RETURN NEW;
END;
$$;
"""

BAD = """
CREATE OR REPLACE FUNCTION demo_fix_expiry() RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN NEW;
END;
$$;
"""


def _definitions(sql: str, base_line: int = 1) -> list:
    return CHECK.definitions_in_sql(sql, Path("fake_revision.py"), base_line)


def _verdicts(sql: str) -> list[str | None]:
    return [definition.verdict for definition in _definitions(sql)]


def _revision_source(upgrade_sql: str, downgrade_sql: str = "SELECT 1;") -> str:
    """与真实 revision 同型的假文件：常量 + upgrade()/downgrade() 各引用自己的那份。"""

    return (
        '"""假 revision：只为门禁用例服务。"""\n\n'
        f"_UPGRADE_SQL = r'''{upgrade_sql}'''\n\n"
        f"_DOWNGRADE_SQL = r'''{downgrade_sql}'''\n\n\n"
        "def _execute(sql: str) -> None:\n    pass\n\n\n"
        "def upgrade() -> None:\n    _execute(_UPGRADE_SQL)\n\n\n"
        "def downgrade() -> None:\n    _execute(_DOWNGRADE_SQL)\n"
    )


def _run(revisions: dict[str, str], baseline: str = "# 空\n") -> tuple[int, str]:
    """把假 revision 与基线落到临时目录，真实调用 run()，返回退出码与全部输出。"""

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        versions = root / "versions"
        versions.mkdir()
        for name, source in revisions.items():
            (versions / name).write_text(source, encoding="utf-8")
        baseline_path = root / "baseline.txt"
        baseline_path.write_text(baseline, encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = CHECK.run(versions, baseline_path)
    return code, out.getvalue() + err.getvalue()


class SearchPathClauseTest(unittest.TestCase):
    """一条定义语句里 SET search_path 的各种写法：合规的过，其余逐一说清为什么红。"""

    def test_fixed_value_with_equals_passes(self) -> None:
        self.assertEqual(_verdicts(GOOD), [None])

    def test_to_form_uppercase_and_odd_whitespace_pass(self) -> None:
        sql = """
        create function demo() returns trigger language plpgsql
            SET   SEARCH_PATH
              TO PG_CATALOG ,pg_temp
        as $$ begin return new; end; $$;
        """
        self.assertEqual(_verdicts(sql), [None])

    def test_missing_clause_is_reported(self) -> None:
        self.assertEqual(_verdicts(BAD), ["缺 SET search_path = pg_catalog, pg_temp"])

    def test_pg_temp_first_is_reported(self) -> None:
        sql = GOOD.replace("pg_catalog, pg_temp", "pg_temp, pg_catalog")
        (verdict,) = _verdicts(sql)
        self.assertIn("pg_temp 不在 search_path 末尾", verdict)

    def test_pg_temp_absent_is_reported(self) -> None:
        sql = GOOD.replace("pg_catalog, pg_temp", "pg_catalog")
        (verdict,) = _verdicts(sql)
        self.assertIn("没有 pg_temp", verdict)

    def test_extra_schema_in_front_is_reported(self) -> None:
        """public 排在 pg_catalog 前面能顶掉内建函数：pg_temp 在末尾也不算合规。"""

        sql = GOOD.replace("pg_catalog, pg_temp", "public, pg_catalog, pg_temp")
        (verdict,) = _verdicts(sql)
        self.assertIn("应为 pg_catalog, pg_temp", verdict)
        self.assertIn("public, pg_catalog, pg_temp", verdict)

    def test_single_quoted_list_is_one_bogus_schema_name(self) -> None:
        """'pg_catalog, pg_temp' 整个是一个（不存在的）schema 名，PostgreSQL 不会拆开它。"""

        sql = GOOD.replace("pg_catalog, pg_temp", "'pg_catalog, pg_temp'")
        (verdict,) = _verdicts(sql)
        self.assertIsNotNone(verdict)

    def test_quoted_elements_are_accepted_when_they_spell_the_same_schemas(self) -> None:
        sql = GOOD.replace("pg_catalog, pg_temp", "'pg_catalog', \"pg_temp\"")
        self.assertEqual(_verdicts(sql), [None])

    def test_from_current_and_default_are_reported(self) -> None:
        for value in ("FROM CURRENT", "DEFAULT"):
            with self.subTest(value=value):
                sql = GOOD.replace("= pg_catalog, pg_temp", f"{value}")
                (verdict,) = _verdicts(sql)
                self.assertIn("不是固定清单", verdict)

    def test_language_clause_before_or_after_body_is_recognised(self) -> None:
        before = """
        CREATE FUNCTION a() RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp
        AS $$ BEGIN END; $$;
        """
        after = """
        CREATE FUNCTION b() RETURNS void
        AS $$ BEGIN END; $$
        SET search_path = pg_catalog, pg_temp LANGUAGE plpgsql;
        """
        for sql in (before, after):
            with self.subTest(sql=sql.strip().splitlines()[0]):
                (definition,) = _definitions(sql)
                self.assertEqual(definition.language, "plpgsql")
                self.assertIsNone(definition.verdict)
        (definition,) = _definitions(after.replace("SET search_path = pg_catalog, pg_temp ", ""))
        self.assertEqual(definition.language, "plpgsql")
        self.assertIsNotNone(definition.verdict)

    def test_custom_dollar_tag_comments_and_other_set_clauses(self) -> None:
        """0054 清理函数的形状：注释夹在子句之间、$cleanup$ 标签、还有别的 SET 与 E'…'。"""

        sql = """
        CREATE FUNCTION public.lingxi_retention_cleanup(
            p_now   timestamptz,
            p_limit integer DEFAULT 10,
            p_note  text DEFAULT E'it\\'s; not the end'
        )
        RETURNS TABLE (target_table text, deleted_rows bigint)
        LANGUAGE plpgsql
        SECURITY DEFINER
        -- 必须以 pg_temp 结尾；这行注释里写 CREATE FUNCTION 也不算一条定义
        /* 块注释 /* 可嵌套 */ 也不算 */
        SET search_path = pg_catalog, pg_temp
        SET lock_timeout = '2s'
        AS $cleanup$
        DECLARE v int;
        BEGIN
            -- 函数体里的分号与 $$ 都不该切断语句
            PERFORM 1; RETURN;
        END;
        $cleanup$;
        """
        (definition,) = _definitions(sql)
        self.assertEqual(definition.qualified_name, "public.lingxi_retention_cleanup")
        self.assertEqual(definition.name, "lingxi_retention_cleanup")
        self.assertIsNone(definition.verdict)

    def test_set_inside_the_body_does_not_count(self) -> None:
        """函数体里 SET search_path 不会写进 proconfig，Supabase 巡检照样报。"""

        sql = BAD.replace("BEGIN", "BEGIN\n    SET search_path = pg_catalog, pg_temp;")
        (verdict,) = _verdicts(sql)
        self.assertIn("缺 SET search_path", verdict)

    def test_alter_function_in_the_same_sql_does_not_count(self) -> None:
        sql = BAD + "\nALTER FUNCTION demo_fix_expiry() SET search_path = pg_catalog, pg_temp;\n"
        self.assertEqual(_verdicts(sql), ["缺 SET search_path = pg_catalog, pg_temp"])

    def test_every_set_clause_must_be_compliant(self) -> None:
        """写两次时最后一次生效：任何一次不合规就红，不猜哪一次算数。"""

        sql = GOOD.replace(
            "SET search_path = pg_catalog, pg_temp",
            "SET search_path = pg_catalog, pg_temp SET search_path = public",
        )
        (verdict,) = _verdicts(sql)
        self.assertIsNotNone(verdict)

    def test_sql_language_functions_and_procedures_are_judged_too(self) -> None:
        sql = """
        CREATE FUNCTION s() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;
        CREATE OR REPLACE PROCEDURE p() LANGUAGE plpgsql AS $$ BEGIN END; $$;
        """
        definitions = _definitions(sql)
        self.assertEqual([item.language for item in definitions], ["sql", "plpgsql"])
        self.assertTrue(all(item.verdict for item in definitions))

    def test_quoted_name_and_line_number_are_reported(self) -> None:
        sql = '\n\nCREATE FUNCTION public."Mixed"() RETURNS void LANGUAGE sql AS $$ SELECT 1 $$;'
        (definition,) = _definitions(sql, base_line=40)
        self.assertEqual(definition.name, "Mixed")
        self.assertEqual(definition.line, 42)

    def test_mentions_in_comments_are_not_definitions(self) -> None:
        sql = "-- CREATE FUNCTION nothing() here\nSELECT 1;\n/* CREATE OR REPLACE FUNCTION x() */"
        self.assertEqual(_definitions(sql), [])

    def test_unterminated_dollar_quote_is_an_error(self) -> None:
        with self.assertRaises(CHECK.CheckError) as caught:
            _definitions("CREATE FUNCTION f() RETURNS void LANGUAGE sql AS $$ SELECT 1;")
        self.assertIn("没有闭合", str(caught.exception))

    def test_definition_without_parameter_list_is_an_error(self) -> None:
        with self.assertRaises(CHECK.CheckError):
            _definitions("CREATE FUNCTION nothing RETURNS void;")


class RevisionFileTest(unittest.TestCase):
    """整个目录级别的判定：文件:行 函数名、基线、downgrade 豁免、响亮失败。"""

    def test_missing_clause_is_red_with_file_line_and_name(self) -> None:
        code, output = _run({"0100_demo.py": _revision_source(BAD)})
        self.assertEqual(code, 1)
        self.assertIn("0100_demo.py:4 demo_fix_expiry：缺 SET search_path", output)

    def test_compliant_revision_is_green_and_counts_are_printed(self) -> None:
        code, output = _run({"0100_demo.py": _revision_source(GOOD)})
        self.assertEqual(code, 0, output)
        self.assertIn("1 个函数定义：1 个带 SET search_path", output)

    def test_baseline_exempts_only_the_listed_file(self) -> None:
        revisions = {
            "0100_old.py": _revision_source(BAD),
            "0101_new.py": _revision_source(BAD),
        }
        code, output = _run(revisions, "0100_old.py\tdemo_fix_expiry\n")
        self.assertEqual(code, 1)
        self.assertNotIn("0100_old.py", output)
        self.assertIn("0101_new.py:4 demo_fix_expiry", output)
        code, output = _run(
            {"0100_old.py": _revision_source(BAD)}, "0100_old.py\tdemo_fix_expiry\n"
        )
        self.assertEqual(code, 0, output)
        self.assertIn("1 个在 1 条历史基线豁免内", output)

    def test_stale_baseline_entry_is_red(self) -> None:
        for baseline in ("0100_demo.py\tdemo_fix_expiry\n", "0999_gone.py\tdemo_fix_expiry\n"):
            with self.subTest(baseline=baseline.strip()):
                code, output = _run({"0100_demo.py": _revision_source(GOOD)}, baseline)
                self.assertEqual(code, 1)
                self.assertIn("基线只能变短", output)

    def test_baseline_format_errors_are_red(self) -> None:
        for baseline in ("0100_demo.py demo\n", "a.py\tf\ta.py\n", "a.py\tf\na.py\tf\n"):
            with self.subTest(baseline=baseline):
                code, output = _run({"0100_demo.py": _revision_source(GOOD)}, baseline)
                self.assertEqual(code, 1)
                self.assertIn("基线文件第", output)

    def test_zero_definitions_is_red(self) -> None:
        code, output = _run({"0100_demo.py": _revision_source("CREATE TABLE t (id int);")})
        self.assertEqual(code, 1)
        self.assertIn("没有解析到任何函数定义", output)

    def test_missing_or_empty_versions_directory_is_red(self) -> None:
        code, output = _run({})
        self.assertEqual(code, 1)
        self.assertIn("一个 revision 都没有", output)
        with tempfile.TemporaryDirectory() as raw:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = CHECK.run(Path(raw) / "missing", Path(raw) / "baseline.txt")
        self.assertEqual(code, 1)
        self.assertIn("目录不存在", err.getvalue())

    def test_downgrade_only_sql_is_not_judged_but_shared_sql_is(self) -> None:
        """降级恢复历史定义不判；同一份 SQL 一旦也被 upgrade 引用就照判。"""

        code, output = _run({"0100_demo.py": _revision_source(GOOD, downgrade_sql=BAD)})
        self.assertEqual(code, 0, output)
        shared = _revision_source(GOOD, downgrade_sql=BAD).replace(
            "def upgrade() -> None:\n    _execute(_UPGRADE_SQL)",
            "def upgrade() -> None:\n    _execute(_UPGRADE_SQL)\n    _execute(_DOWNGRADE_SQL)",
        )
        code, output = _run({"0100_demo.py": shared})
        self.assertEqual(code, 1)
        self.assertIn("demo_fix_expiry：缺 SET search_path", output)

    def test_literal_inside_downgrade_is_not_judged_but_inside_upgrade_is(self) -> None:
        inline = BAD.replace("\n", " ").strip()
        source = (
            "def upgrade() -> None:\n    pass\n\n\n"
            f"def downgrade() -> None:\n    execute({inline!r})\n"
        )
        code, output = _run({"0100_demo.py": source, "0101_ok.py": _revision_source(GOOD)})
        self.assertEqual(code, 0, output)
        source = f"def upgrade() -> None:\n    execute({inline!r})\n\n\ndef downgrade() -> None:\n    pass\n"
        code, output = _run({"0100_demo.py": source})
        self.assertEqual(code, 1)
        self.assertIn("0100_demo.py:2 demo_fix_expiry", output)

    def test_docstring_prose_is_ignored(self) -> None:
        source = _revision_source(GOOD).replace(
            '"""假 revision：只为门禁用例服务。"""',
            '"""说明里提到 CREATE OR REPLACE FUNCTION demo() 也不是一条定义。"""',
        )
        code, output = _run({"0100_demo.py": source})
        self.assertEqual(code, 0, output)

    def test_fstring_definition_is_red(self) -> None:
        source = (
            'NAME = "demo"\n'
            'SQL = f"CREATE FUNCTION {NAME}() RETURNS void LANGUAGE sql AS $$ SELECT 1 $$;"\n\n'
            "def upgrade() -> None:\n    execute(SQL)\n\n\ndef downgrade() -> None:\n    pass\n"
        )
        code, output = _run({"0100_demo.py": source})
        self.assertEqual(code, 1)
        self.assertIn("f-string", output)

    def test_cli_exit_codes_and_messages(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "versions").mkdir()
            (root / "versions" / "0100_demo.py").write_text(_revision_source(BAD), encoding="utf-8")
            (root / "baseline.txt").write_text("# 空\n", encoding="utf-8")
            command = [sys.executable, str(SCRIPT), "--versions-dir", str(root / "versions")]
            red = subprocess.run(
                [*command, "--baseline", str(root / "baseline.txt")],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(red.returncode, 1)
            self.assertIn("demo_fix_expiry：缺 SET search_path", red.stderr)
            (root / "baseline.txt").write_text("0100_demo.py\tdemo_fix_expiry\n", encoding="utf-8")
            green = subprocess.run(
                [*command, "--baseline", str(root / "baseline.txt")],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(green.returncode, 0, green.stderr)
            self.assertIn("通过", green.stdout)


class WiringTest(unittest.TestCase):
    """接线本身也要钉住：没接进门禁入口的检查等于没有检查。"""

    def test_verify_repository_invokes_the_check(self) -> None:
        script = VERIFY_REPOSITORY.read_text(encoding="utf-8")
        self.assertIn("\npython3 scripts/ci/check_plpgsql_search_path.py\n", script)


class RealRepositoryStateTest(unittest.TestCase):
    """仓库当前状态必须自洽：基线与现状一致，而且只能变短。"""

    def _real_run(self, baseline_text: str | None = None) -> tuple[int, str]:
        baseline_path = CHECK.BASELINE_PATH
        if baseline_text is not None:
            handle = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
            with handle:
                handle.write(baseline_text)
            baseline_path = Path(handle.name)
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = CHECK.run(CHECK.VERSIONS_DIR, baseline_path)
        finally:
            if baseline_text is not None:
                baseline_path.unlink()
        return code, out.getvalue() + err.getvalue()

    def test_real_revisions_pass_with_the_committed_baseline(self) -> None:
        code, output = self._real_run()
        self.assertEqual(code, 0, output)
        self.assertIn("数据库函数 search_path 门禁：通过", output)

    def test_baseline_is_a_subset_of_the_pinned_snapshot(self) -> None:
        baseline = CHECK.load_baseline(CHECK.BASELINE_PATH)
        added = sorted(baseline - PINNED_BASELINE)
        self.assertEqual(
            added,
            [],
            f"基线出现了钉住快照之外的新豁免：{added}。这份清单只能变短：删掉一行允许，"
            "新增一行即红——否则任何新函数都能靠登记基线绕过门禁。确实要放宽，"
            "必须同时改 PINNED_BASELINE 并在 PR 正文写明裁定依据。",
        )

    def test_every_baseline_entry_names_an_existing_revision_file(self) -> None:
        for file_name, function_name in sorted(CHECK.load_baseline(CHECK.BASELINE_PATH)):
            with self.subTest(entry=f"{file_name} / {function_name}"):
                self.assertTrue((CHECK.VERSIONS_DIR / file_name).is_file())

    def test_removing_one_baseline_line_turns_the_real_repository_red(self) -> None:
        """基线与现状精确对应：少登记一个历史函数，门禁必须立刻指名报红。"""

        lines = CHECK.BASELINE_PATH.read_text(encoding="utf-8").splitlines()
        entries = [line for line in lines if line and not line.startswith("#")]
        self.assertTrue(entries, "基线为空，本用例失去意义")
        dropped = entries[-1]
        remaining = "\n".join(line for line in lines if line != dropped) + "\n"
        code, output = self._real_run(remaining)
        self.assertEqual(code, 1)
        self.assertIn(dropped.split("\t")[1], output)


if __name__ == "__main__":
    unittest.main()
