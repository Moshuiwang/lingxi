"""`scripts/ci/check_matrix_row_size_ratchet.py` 的解析与判定用例（Issue #335）。

跟 ``test_size_ratchet_check.py``/``test_acceptance_matrix_check.py`` 同一惯例：
每个用例先构造一份会违规的输入，断言它被具体地拒绝，而不是只跑一遍真实矩阵看它
绿——一份只会通过的检查等于没有检查。
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "ci" / "check_matrix_row_size_ratchet.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("matrix_row_size_ratchet_check_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CHECK = _load_script()


def matrix_document(rows: str, *, extra_table: str = "") -> str:
    """构造一份最小合法矩阵文档：一张 MATRIX_HEADER 表 + 可选的额外表格。"""

    return (
        "# 产品验收矩阵\n\n"
        "> 状态：测试夹具。\n\n"
        "## 一、关键机制验收矩阵\n\n"
        "| # | 可验证断言 | 层级 | 状态 |\n"
        "|---|---|---|---|\n"
        f"{rows}"
        f"{extra_table}"
    )


class BaselineParsingTest(unittest.TestCase):
    def test_good_baseline_parses(self) -> None:
        text = "# 注释行\n\n11485\tV-权限-15\n2840\tV-开通-19\n"
        self.assertEqual(
            CHECK.parse_baseline(text),
            {"V-权限-15": 11485, "V-开通-19": 2840},
        )

    def test_malformed_row_fails_closed(self) -> None:
        with self.assertRaises(CHECK.BaselineError):
            CHECK.parse_baseline("not-a-number\tV-开通-19\n")

    def test_missing_tab_fails_closed(self) -> None:
        with self.assertRaises(CHECK.BaselineError):
            CHECK.parse_baseline("2840 V-开通-19\n")

    def test_duplicate_identifier_fails_closed(self) -> None:
        with self.assertRaises(CHECK.BaselineError):
            CHECK.parse_baseline("100\tV-开通-19\n200\tV-开通-19\n")

    def test_invalid_identifier_fails_closed(self) -> None:
        with self.assertRaises(CHECK.BaselineError):
            CHECK.parse_baseline("100\t不是断言编号\n")


class RenderRoundTripTest(unittest.TestCase):
    def test_render_then_parse_round_trips(self) -> None:
        entries = {"V-开通-19": 2840, "V-权限-15": 11485}
        rendered = CHECK.render_baseline(entries)
        self.assertEqual(CHECK.parse_baseline(rendered), entries)


class AssertionRowParsingTest(unittest.TestCase):
    """断言行解析：只认 MATRIX_HEADER 表下、编号格是合法 V-* 编号的行。"""

    def test_row_under_matrix_header_is_measured(self) -> None:
        text = matrix_document("| V-开通-01 | 一句判定 | L2（真库） | 已认领 |\n")
        current = CHECK.measure_rows(text)
        self.assertIn("V-开通-01", current)
        expected_bytes = len(
            "| V-开通-01 | 一句判定 | L2（真库） | 已认领 |".encode("utf-8")
        )
        self.assertEqual(current["V-开通-01"], expected_bytes)

    def test_row_under_a_different_table_header_is_ignored(self) -> None:
        """表头不是 MATRIX_HEADER 的表格（例如合同覆盖清单）不参与丈量，
        即使某一格文本里出现 V-* 字样也不会被误当成断言行。"""

        extra = (
            "\n### 覆盖清单\n\n"
            "| 合同章节 | 对应断言 | 说明 |\n"
            "|---|---|---|\n"
            "| 首次对话 | V-开通-01…03 | — |\n"
        )
        text = matrix_document("| V-开通-01 | 一句判定 | L2（真库） | 已认领 |\n", extra_table=extra)
        current = CHECK.measure_rows(text)
        self.assertEqual(set(current), {"V-开通-01"})

    def test_row_inside_fenced_code_block_is_ignored(self) -> None:
        text = matrix_document(
            "```\n"
            "| V-开通-99 | 示例，不是真实登记 | L2 | 已认领 |\n"
            "```\n"
        )
        current = CHECK.measure_rows(text)
        self.assertEqual(current, {})

    def test_row_missing_from_matrix_header_table_is_ignored_even_if_it_looks_like_one(self) -> None:
        """编号格开头是 V- 但所在表没有 MATRIX_HEADER 表头：本检查不认，
        这类问题由 check_acceptance_matrix.py 负责判红，本检查不重复也不漏。"""

        text = (
            "# 产品验收矩阵\n\n"
            "| 编号 | 说明 |\n"
            "|---|---|\n"
            "| V-开通-01 | 表头不对，不是合法矩阵行 |\n"
        )
        current = CHECK.measure_rows(text)
        self.assertEqual(current, {})

    def test_duplicate_identifier_across_two_rows_takes_the_larger_byte_count(self) -> None:
        """按行独立计的防御性兜底：同一编号出现两行时，取较大字节数代表该编号，
        确保任意一行超标都会被抓到，不被同编号下的短行掩盖（见模块文档字符串）。"""

        short_row = "| V-开通-01 | 短 | L2 | 已认领 |\n"
        long_row = "| V-开通-01 | " + ("很长的裁定沿革" * 40) + " | L2 | 已认领 |\n"
        current = CHECK.measure_rows(matrix_document(short_row + long_row))
        self.assertEqual(
            current["V-开通-01"],
            max(len(short_row.rstrip("\n").encode("utf-8")), len(long_row.rstrip("\n").encode("utf-8"))),
        )


class EvaluateTest(unittest.TestCase):
    """核心判定逻辑：不依赖磁盘，直接喂 (baseline, current) 字典。"""

    def test_growth_beyond_recorded_ceiling_is_rejected(self) -> None:
        baseline = {"V-权限-15": 11485}
        current = {"V-权限-15": 11486}
        failures = CHECK.evaluate(baseline, current)
        self.assertTrue(any("超过棘轮基线记录的上限" in f for f in failures), failures)

    def test_shrinking_below_recorded_ceiling_is_rejected_until_refreshed(self) -> None:
        baseline = {"V-权限-15": 11485}
        current = {"V-权限-15": 9000}
        failures = CHECK.evaluate(baseline, current)
        self.assertTrue(any("与实测" in f and "不一致" in f for f in failures), failures)

    def test_exact_match_passes(self) -> None:
        baseline = {"V-权限-15": 11485}
        current = {"V-权限-15": 11485}
        self.assertEqual(CHECK.evaluate(baseline, current), [])

    def test_new_row_crossing_threshold_unregistered_is_rejected(self) -> None:
        baseline: dict[str, int] = {}
        current = {"V-开通-01": CHECK.THRESHOLD_BYTES + 1}
        failures = CHECK.evaluate(baseline, current)
        self.assertTrue(any("新超过单行体量棘轮阈值" in f for f in failures), failures)

    def test_row_exactly_at_threshold_and_unregistered_passes(self) -> None:
        baseline: dict[str, int] = {}
        current = {"V-开通-01": CHECK.THRESHOLD_BYTES}
        self.assertEqual(CHECK.evaluate(baseline, current), [])

    def test_row_under_threshold_and_unregistered_passes(self) -> None:
        baseline: dict[str, int] = {}
        current = {"V-开通-01": 42}
        self.assertEqual(CHECK.evaluate(baseline, current), [])

    def test_deleted_identifier_leaving_baseline_is_not_a_failure(self) -> None:
        """断言编号已经不在矩阵里（删除/改名）：棘轮的目的已经达成，不强制立即刷新。"""

        baseline = {"V-权限-15": 11485}
        current: dict[str, int] = {}
        self.assertEqual(CHECK.evaluate(baseline, current), [])

    def test_manually_inflating_the_baseline_without_touching_the_row_is_rejected(self) -> None:
        baseline = {"V-权限-15": 99999}
        current = {"V-权限-15": 11485}
        failures = CHECK.evaluate(baseline, current)
        self.assertTrue(any("与实测" in f and "不一致" in f for f in failures), failures)


class TotalSizeNoticeTest(unittest.TestCase):
    """总量触发线：只提示、不卡红——本类断言提示内容本身，
    RunCheckDoesNotBlockOnTotalTriggerTest 断言 run_check() 的退出码不受影响。"""

    def test_below_both_trigger_lines_reports_plain_status(self) -> None:
        text = "短文档\n" * 3
        notice = CHECK.render_total_size_notice(text)
        self.assertIn("未触及分册触发线", notice)
        self.assertNotIn("下一次改动", notice)  # 该句只出现在触线横幅里
        self.assertNotIn("\n", notice)  # 未触线是单行状态，不是多行横幅

    def test_over_byte_trigger_reports_banner(self) -> None:
        text = "字" * (CHECK.TOTAL_BYTES_TRIGGER // 3 + 10)  # 每个汉字 3 字节，确保过线
        notice = CHECK.render_total_size_notice(text)
        self.assertIn("分册", notice)
        self.assertIn("提示", notice)

    def test_over_line_trigger_reports_banner(self) -> None:
        text = "\n".join(f"行{i}" for i in range(CHECK.TOTAL_LINES_TRIGGER + 1))
        notice = CHECK.render_total_size_notice(text)
        self.assertIn("分册", notice)
        self.assertIn("提示", notice)


class MeasureDocumentsTest(unittest.TestCase):
    """分册合并规则（Issue #479）：以断言编号为 key 取整个集合里的最大字节数。"""

    def test_rows_from_every_volume_are_measured(self) -> None:
        documents = {
            "验收矩阵-一册.md": matrix_document("| V-开通-01 | 一句判定 | L2 | 已认领 |\n"),
            "验收矩阵-二册.md": matrix_document("| V-权限-01 | 另一句判定 | L2 | 已验证 |\n"),
        }
        self.assertEqual(set(CHECK.measure_documents(documents)), {"V-开通-01", "V-权限-01"})

    def test_the_same_identifier_in_two_volumes_takes_the_larger_count(self) -> None:
        """搬去哪一册都不改变判定：任意一册里的超标行都不会被另一册的短行掩盖。"""
        short_row = "| V-开通-01 | 短 | L2 | 已认领 |\n"
        long_row = "| V-开通-01 | " + ("很长的裁定沿革" * 40) + " | L2 | 已认领 |\n"
        documents = {
            "验收矩阵-一册.md": matrix_document(short_row),
            "验收矩阵-二册.md": matrix_document(long_row),
        }
        self.assertEqual(
            CHECK.measure_documents(documents)["V-开通-01"],
            len(long_row.rstrip("\n").encode("utf-8")),
        )


class TotalSizeReportTest(unittest.TestCase):
    """触发线按**单个文件**判定（Issue #479 口径更新），合计只打印、不设阈值。"""

    def test_each_document_is_reported_on_its_own(self) -> None:
        documents = {
            "验收矩阵.md": "短文档\n",
            "验收矩阵-一册.md": "\n".join(f"行{i}" for i in range(CHECK.TOTAL_LINES_TRIGGER + 1)),
        }
        report = CHECK.render_total_size_report(documents)
        self.assertIn("验收矩阵.md：", report)
        self.assertIn("未触及分册触发线", report)
        self.assertIn("【提示】", report)  # 触线的那一册照样出横幅
        self.assertIn("验收矩阵全集合合计：2 个文件", report)

    def test_two_documents_each_under_the_trigger_do_not_trigger_on_their_sum(self) -> None:
        """口径更新的正面确认：单册各自不触线时，合计再大也只是打印，不出横幅。"""
        half = "字" * (CHECK.TOTAL_BYTES_TRIGGER // 3 - 10)
        report = CHECK.render_total_size_report({"a.md": half, "b.md": half})
        self.assertNotIn("【提示】", report)
        self.assertIn("验收矩阵全集合合计：2 个文件", report)


class RunCheckDoesNotBlockOnTotalTriggerTest(unittest.TestCase):
    """行为级验证：即便总量触线，只要单行棘轮本身没有违规，run_check() 仍必须
    退出 0——这是「非阻断」这条规则唯一真正生效的地方，字符串提示本身不够。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.matrix_path = root / "matrix.md"
        self.baseline_path = root / "baseline.txt"
        # 读取范围自检要求「总册 + 至少一个分册」（Issue #479 / PR #490 P2-1），
        # 所以夹具也按真实形状摆：一个总册 + 一个短行分册（短行不会进棘轮基线，
        # 不影响这些用例各自要测的东西）。
        self.volume_path = root / "验收矩阵-占位册.md"
        self.volume_path.write_text(
            matrix_document("| V-占位-01 | 占位分册的一行 | L2 | 已认领 |\n"), encoding="utf-8"
        )

        self._orig_matrix = CHECK.MATRIX_DOCUMENT
        self._orig_baseline = CHECK.BASELINE_PATH
        CHECK.MATRIX_DOCUMENT = self.matrix_path
        CHECK.BASELINE_PATH = self.baseline_path
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        CHECK.MATRIX_DOCUMENT = self._orig_matrix
        CHECK.BASELINE_PATH = self._orig_baseline

    def test_total_over_line_trigger_with_clean_rows_still_exits_zero(self) -> None:
        rows = "".join(
            f"| V-开通-{index:02d} | 一句判定 | L2（真库） | 已认领 |\n" for index in range(1, 3)
        )
        # 用大量非表格正文行把总行数推过 1500 行触发线，行内容本身与断言无关。
        padding = "\n".join(f"填充说明第 {i} 行。" for i in range(CHECK.TOTAL_LINES_TRIGGER + 5))
        text = matrix_document(rows) + "\n" + padding + "\n"
        self.matrix_path.write_text(text, encoding="utf-8")
        self.baseline_path.write_text(CHECK.render_baseline({}), encoding="utf-8")

        self.assertGreater(len(text.splitlines()), CHECK.TOTAL_LINES_TRIGGER)
        exit_code = CHECK.run_check()
        self.assertEqual(exit_code, 0)


class RunRefreshClassificationTest(unittest.TestCase):
    """``run_refresh`` 只挡「超过上限」与「新行未登记」两类失败，放行
    「基线记录 > 实测」（那正是它该修的陈旧记录）——与 check_size_ratchet.py 的
    run_refresh 同一纪律（见该脚本模块文档字符串 B1 回归教训）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.matrix_path = root / "matrix.md"
        self.baseline_path = root / "baseline.txt"
        # 同上：读取范围自检要求「总册 + 至少一个分册」。
        self.volume_path = root / "验收矩阵-占位册.md"
        self.volume_path.write_text(
            matrix_document("| V-占位-01 | 占位分册的一行 | L2 | 已认领 |\n"), encoding="utf-8"
        )

        self._orig_repository_root = CHECK.REPOSITORY_ROOT
        self._orig_matrix = CHECK.MATRIX_DOCUMENT
        self._orig_baseline = CHECK.BASELINE_PATH
        # evaluate() 在报「新超过阈值」失败时会算 BASELINE_PATH.relative_to
        # (REPOSITORY_ROOT) 来拼提示文案；一并打桩成同一棵临时目录，否则真实
        # 仓库根与临时 BASELINE_PATH 不在同一棵树下会直接抛 ValueError。
        CHECK.REPOSITORY_ROOT = root
        CHECK.MATRIX_DOCUMENT = self.matrix_path
        CHECK.BASELINE_PATH = self.baseline_path
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        CHECK.REPOSITORY_ROOT = self._orig_repository_root
        CHECK.MATRIX_DOCUMENT = self._orig_matrix
        CHECK.BASELINE_PATH = self._orig_baseline

    def _row(self, identifier: str, filler_char: str, filler_count: int) -> str:
        return f"| {identifier} | {filler_char * filler_count} | L2 | 已认领 |\n"

    def test_refresh_lowers_a_shrunk_entry(self) -> None:
        row = self._row("V-开通-01", "长", 300)  # 仍超 800B 阈值，但比下面登记的旧上限小
        text = matrix_document(row)
        actual_bytes = CHECK.measure_rows(text)["V-开通-01"]
        self.assertGreater(actual_bytes, CHECK.THRESHOLD_BYTES)  # 夹具自检：这条用例测「调低」不是「移除」
        self.matrix_path.write_text(text, encoding="utf-8")
        self.baseline_path.write_text(
            CHECK.render_baseline({"V-开通-01": actual_bytes + 500}), encoding="utf-8"
        )

        exit_code = CHECK.run_refresh()
        self.assertEqual(exit_code, 0)
        self.assertEqual(CHECK.load_baseline(self.baseline_path), {"V-开通-01": actual_bytes})

    def test_refresh_removes_an_entry_that_shrank_below_threshold(self) -> None:
        row = self._row("V-开通-01", "短", 3)
        self.matrix_path.write_text(matrix_document(row), encoding="utf-8")
        self.baseline_path.write_text(CHECK.render_baseline({"V-开通-01": 2000}), encoding="utf-8")

        exit_code = CHECK.run_refresh()
        self.assertEqual(exit_code, 0)
        self.assertEqual(CHECK.load_baseline(self.baseline_path), {})

    def test_refresh_refuses_when_an_unregistered_row_newly_crosses_threshold(self) -> None:
        registered_row = self._row("V-开通-01", "短", 3)
        new_giant_row = self._row("V-开通-02", "巨", 400)  # 远超 800B，未登记
        self.matrix_path.write_text(matrix_document(registered_row + new_giant_row), encoding="utf-8")
        self.baseline_path.write_text(CHECK.render_baseline({"V-开通-01": 2000}), encoding="utf-8")

        exit_code = CHECK.run_refresh()
        self.assertEqual(exit_code, 1)
        # 拒绝时不得动基线文件——既有的合法收紧也不该被这次失败连累着丢失。
        self.assertEqual(CHECK.load_baseline(self.baseline_path), {"V-开通-01": 2000})

    def test_refresh_refuses_when_a_registered_row_grew_past_its_ceiling(self) -> None:
        grown_row = self._row("V-开通-01", "长", 400)
        self.matrix_path.write_text(matrix_document(grown_row), encoding="utf-8")
        self.baseline_path.write_text(CHECK.render_baseline({"V-开通-01": 50}), encoding="utf-8")

        exit_code = CHECK.run_refresh()
        self.assertEqual(exit_code, 1)
        self.assertEqual(CHECK.load_baseline(self.baseline_path), {"V-开通-01": 50})


class ReadRangeFailsClosedTest(unittest.TestCase):
    """读取范围塌掉时必须判红（Issue #479 分册的直接后果，PR #490 独立审查 P2-1）。

    分册前，把矩阵文件挪走会让 ``read_matrix_text()`` 抛 ``BaselineError`` 自然判红。
    分册后同一场景变成「总册还在、8 个分册都不见了」：量到 0 条断言行，而
    ``evaluate`` 对「基线里有、当前没有」一律放行（那条 continue 是给真删除留的），
    于是脚本会安安静静 exit 0。这一组用例把那条路钉死。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.matrix_path = self.root / "matrix.md"
        self.baseline_path = self.root / "baseline.txt"

        self._orig_repository_root = CHECK.REPOSITORY_ROOT
        self._orig_matrix = CHECK.MATRIX_DOCUMENT
        self._orig_baseline = CHECK.BASELINE_PATH
        CHECK.REPOSITORY_ROOT = self.root
        CHECK.MATRIX_DOCUMENT = self.matrix_path
        CHECK.BASELINE_PATH = self.baseline_path
        self.addCleanup(self._restore)

        self.matrix_path.write_text(
            matrix_document("| V-开通-01 | 一句判定 | L2（真库） | 已认领 |\n"), encoding="utf-8"
        )
        self.baseline_path.write_text(CHECK.render_baseline({}), encoding="utf-8")

    def _restore(self) -> None:
        CHECK.REPOSITORY_ROOT = self._orig_repository_root
        CHECK.MATRIX_DOCUMENT = self._orig_matrix
        CHECK.BASELINE_PATH = self._orig_baseline

    def _add_volume(self, rows: str) -> Path:
        volume = self.root / "验收矩阵-一册.md"
        volume.write_text(matrix_document(rows), encoding="utf-8")
        return volume

    def test_every_volume_missing_fails_closed(self) -> None:
        """总册还在、分册一个都不在：必须判红，不许按「断言都下线了」放行。"""
        exit_code = CHECK.run_check()
        self.assertEqual(exit_code, 1)

    def test_the_failure_names_the_read_range(self) -> None:
        with self.assertRaises(CHECK.BaselineError) as caught:
            CHECK.verify_read_range({"matrix.md": ""}, {"V-开通-01": 100})
        self.assertIn("读取范围", str(caught.exception))
        self.assertIn(CHECK.MATRIX_VOLUME_GLOB, str(caught.exception))

    def test_enough_files_but_zero_assertion_rows_still_fails_closed(self) -> None:
        """文件数凑够也不够：一条断言行都没量到说明表格结构被改动了。"""
        (self.root / "验收矩阵-一册.md").write_text("# 分册\n\n没有任何表格。\n", encoding="utf-8")
        self.matrix_path.write_text("# 产品验收矩阵\n\n没有任何表格。\n", encoding="utf-8")
        self.assertEqual(CHECK.run_check(), 1)

    def test_a_hub_plus_one_volume_passes(self) -> None:
        """正面对照：补回一个分册就复绿——上面两条红不是被别的原因带出来的。"""
        self._add_volume("| V-权限-01 | 另一句判定 | L2 | 已验证 |\n")
        self.assertEqual(CHECK.run_check(), 0)

    def test_refresh_refuses_on_a_broken_read_range_and_keeps_the_baseline(self) -> None:
        """--refresh 会写基线：在坏掉的读取范围上刷新等于把全部登记一次抹掉。"""
        self.baseline_path.write_text(CHECK.render_baseline({"V-权限-15": 11485}), encoding="utf-8")
        self.assertEqual(CHECK.run_refresh(), 1)
        self.assertEqual(CHECK.load_baseline(self.baseline_path), {"V-权限-15": 11485})


class RealBaselineIsHonestTest(unittest.TestCase):
    """反向验证：仓库里已经提交的基线文件与真实矩阵文档必须诚实（记录值与实测
    精确相等），且脚本对当前真实矩阵实跑必须是绿——与 test_size_ratchet_check.py
    的 RealBaselineIsHonestTest 同一惯例。"""

    def test_committed_baseline_matches_actual_row_byte_counts(self) -> None:
        baseline = CHECK.load_baseline(CHECK.BASELINE_PATH)
        current = CHECK.measure_documents(CHECK.read_matrix_documents())
        self.assertEqual(CHECK.evaluate(baseline, current), [])

    def test_every_baseline_entry_is_still_measured_after_the_split(self) -> None:
        """分册（Issue #479）后每条基线登记都必须仍被丈量到。

        ``evaluate`` 对「基线里有、矩阵里没有」这种情况刻意不判红（断言下线是合法
        的）。所以如果丈量范围漏掉某个分册，上面那条用例会**静默变成空话**——它只
        会看到一个空的 current 然后通过。这条用例正面钉住：登记的 20 条全都要被
        当前的读取范围覆盖到。
        """
        baseline = CHECK.load_baseline(CHECK.BASELINE_PATH)
        current = CHECK.measure_documents(CHECK.read_matrix_documents())
        self.assertEqual(sorted(set(baseline) - set(current)), [])

    def test_the_real_matrix_is_read_as_a_hub_plus_volumes(self) -> None:
        documents = CHECK.read_matrix_documents()
        self.assertGreater(len(documents), 1, "只读到总册，分册没进丈量范围")
        self.assertIn(CHECK.MATRIX_DOCUMENT.name, documents)

    def test_committed_baseline_entries_are_all_valid_assertion_identifiers(self) -> None:
        baseline = CHECK.load_baseline(CHECK.BASELINE_PATH)
        for identifier in baseline:
            self.assertRegex(identifier, CHECK.ASSERTION_ID.pattern)

    def test_run_check_passes_against_the_real_repository_matrix(self) -> None:
        self.assertEqual(CHECK.run_check(), 0)

    def test_the_real_repository_read_range_self_check_passes(self) -> None:
        documents = CHECK.read_matrix_documents()
        CHECK.verify_read_range(documents, CHECK.measure_documents(documents))
        self.assertGreaterEqual(len(documents), CHECK.MINIMUM_MATRIX_DOCUMENTS)


if __name__ == "__main__":
    unittest.main()
