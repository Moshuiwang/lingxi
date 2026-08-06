"""`scripts/ci/check_acceptance_matrix.py` 的解析与判定用例。

这份检查的价值全在它**会变红**：一条只会通过的检查等于没有检查。因此下面每一个
用例都构造一份坏文档，断言它被具体地拒绝——而不是只跑一遍真文档看它绿。
最后一条用例反过来跑真实仓库文档，防止检查因为文档结构变化而变成空转。
"""

from __future__ import annotations

import importlib.util
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "ci" / "check_acceptance_matrix.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("acceptance_matrix_check_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CHECK = _load_script()


def gate_document(matrix_rows: str, coverage_rows: str) -> str:
    return (
        "# 验证与门禁\n\n"
        "## 七、关键机制验收矩阵\n\n"
        "| # | 可验证断言 | 层级 | 状态 |\n"
        "|---|---|---|---|\n"
        f"{matrix_rows}\n"
        "### 10.3 合同条款覆盖清单\n\n"
        "| 合同章节 | 对应断言 | 说明 |\n"
        "|---|---|---|\n"
        f"{coverage_rows}\n"
    )


GOOD_MATRIX = (
    "| V-开通-01 | 不先占位再回填 | L2（真库） | 已认领 |\n"
    "| V-开通-02 | 命中多条不自动选择 | L2（真库） | 未认领 |\n"
    "| V-开通-03 | 均查不到时给出终态 | L2（真库） | 已验证 |\n"
)
GOOD_COVERAGE = (
    "| 首次对话与自动准入 | V-开通-01…03 | — |\n"
    "| 动态工作的去向 | 无对应断言 | 只规定工作项去向，不产生用户可见规则 |\n"
)
CONTRACT = (
    "# 产品合同与外部边界\n\n"
    "## 首次对话与自动准入\n\n正文。\n\n"
    "## 动态工作的去向\n\n正文。\n"
)


class MatrixStatusColumnTest(unittest.TestCase):
    """①「断言无人认领」——状态缺失或非法必须变红。"""

    def test_good_matrix_parses_three_states(self) -> None:
        statuses, errors = CHECK.parse_matrix(gate_document(GOOD_MATRIX, GOOD_COVERAGE))
        self.assertEqual(errors, [])
        self.assertEqual(
            statuses,
            {"V-开通-01": "已认领", "V-开通-02": "未认领", "V-开通-03": "已验证"},
        )

    def test_missing_status_cell_fails(self) -> None:
        rows = GOOD_MATRIX.replace("| V-开通-02 | 命中多条不自动选择 | L2（真库） | 未认领 |", "| V-开通-02 | 命中多条不自动选择 | L2（真库） |")
        statuses, errors = CHECK.parse_matrix(gate_document(rows, GOOD_COVERAGE))
        self.assertNotIn("V-开通-02", statuses)
        self.assertTrue(any("应为 4 格" in error for error in errors), errors)

    def test_escaped_pipe_stays_inside_one_cell(self) -> None:
        """断言正文里出现字面竖线是合法的（正则、命令行管道），按 Markdown 用 `\\|` 转义。"""
        row = "| V-执行-07 | 白名单不接受 `a\\|b` 这类正则 | L2 | 已认领 |"
        self.assertEqual(
            CHECK.split_row(row),
            ["V-执行-07", "白名单不接受 `a|b` 这类正则", "L2", "已认领"],
        )
        statuses, errors = CHECK.parse_matrix(gate_document(GOOD_MATRIX + row + "\n", GOOD_COVERAGE))
        self.assertEqual(errors, [])
        self.assertEqual(statuses["V-执行-07"], "已认领")

    def test_cell_count_error_points_at_the_escape(self) -> None:
        """裸竖线切出 5 格时，报错要说清怎么写对，而不是含糊地怪状态列。"""
        row = "| V-执行-07 | 没转义的裸竖线 a|b | L2 | 已认领 |\n"
        _, errors = CHECK.parse_matrix(gate_document(GOOD_MATRIX + row, GOOD_COVERAGE))
        self.assertTrue(any("有 5 格" in error and "\\|" in error for error in errors), errors)

    def test_empty_status_cell_fails(self) -> None:
        rows = GOOD_MATRIX.replace("L2（真库） | 未认领 |", "L2（真库） |  |")
        _, errors = CHECK.parse_matrix(gate_document(rows, GOOD_COVERAGE))
        self.assertTrue(any("状态是 ''" in error for error in errors), errors)

    def test_status_outside_the_three_states_fails(self) -> None:
        """10.2 的 Issue 模板用「待实现」，抄进矩阵就是一套不受检查的语义。"""
        rows = GOOD_MATRIX.replace("| 未认领 |", "| 待实现 |")
        _, errors = CHECK.parse_matrix(gate_document(rows, GOOD_COVERAGE))
        self.assertTrue(any("待实现" in error for error in errors), errors)

    def test_assertion_table_without_status_header_fails(self) -> None:
        """整列被删掉时，不能因为「表头不匹配」就把整张表悄悄跳过。"""
        document = gate_document(GOOD_MATRIX, GOOD_COVERAGE).replace(
            "| # | 可验证断言 | 层级 | 状态 |\n|---|---|---|---|",
            "| # | 可验证断言 | 层级 |\n|---|---|---|",
        )
        statuses, errors = CHECK.parse_matrix(document)
        self.assertEqual(statuses, {})
        self.assertEqual(len(errors), 3, errors)
        self.assertTrue(all("没有「状态」列表头" in error for error in errors), errors)

    def test_duplicate_assertion_id_fails(self) -> None:
        rows = GOOD_MATRIX + "| V-开通-01 | 重复登记 | L2 | 已验证 |\n"
        _, errors = CHECK.parse_matrix(gate_document(rows, GOOD_COVERAGE))
        self.assertTrue(any("断言编号重复" in error for error in errors), errors)

    def test_malformed_identifier_cell_fails(self) -> None:
        """把补充条款写进编号格（真实发生过）会让编号列不可机读。"""
        rows = GOOD_MATRIX.replace("| V-开通-02 |", "| V-开通-02；还要求某某 |")
        _, errors = CHECK.parse_matrix(gate_document(rows, GOOD_COVERAGE))
        self.assertTrue(any("不是合法断言编号" in error for error in errors), errors)

    def test_example_table_inside_code_fence_is_ignored(self) -> None:
        document = gate_document(GOOD_MATRIX, GOOD_COVERAGE) + (
            "\n```markdown\n"
            "| # | 断言 | 层级 | 状态 |\n"
            "|---|---|---|---|\n"
            "| V-会话-01 | 示例 | L2 | 待实现 |\n"
            "```\n"
        )
        statuses, errors = CHECK.parse_matrix(document)
        self.assertEqual(errors, [])
        self.assertNotIn("V-会话-01", statuses)

    def test_empty_matrix_is_not_silently_green(self) -> None:
        _, errors = CHECK.parse_matrix("# 验证与门禁\n\n没有任何表格。\n")
        self.assertTrue(any("一条断言都没解析到" in error for error in errors), errors)


class CoverageListTest(unittest.TestCase):
    """②「合同条款无断言覆盖」——漏登记、留空、编号写错必须变红。"""

    def test_good_coverage_parses(self) -> None:
        coverage, errors = CHECK.parse_coverage(gate_document(GOOD_MATRIX, GOOD_COVERAGE))
        self.assertEqual(errors, [])
        self.assertEqual(coverage["首次对话与自动准入"][0], ["V-开通-01", "V-开通-02", "V-开通-03"])
        self.assertEqual(coverage["动态工作的去向"][0], [])

    def test_empty_assertion_cell_fails(self) -> None:
        rows = GOOD_COVERAGE.replace("| V-开通-01…03 |", "|  |")
        _, errors = CHECK.parse_coverage(gate_document(GOOD_MATRIX, rows))
        self.assertTrue(any("没有任何对应断言" in error for error in errors), errors)

    def test_no_assertion_without_reason_fails(self) -> None:
        rows = GOOD_COVERAGE.replace(
            "| 动态工作的去向 | 无对应断言 | 只规定工作项去向，不产生用户可见规则 |",
            "| 动态工作的去向 | 无对应断言 | — |",
        )
        _, errors = CHECK.parse_coverage(gate_document(GOOD_MATRIX, rows))
        self.assertTrue(any("没有说明理由" in error for error in errors), errors)

    def test_unparseable_reference_fails(self) -> None:
        rows = GOOD_COVERAGE.replace("V-开通-01…03", "见开通一节")
        _, errors = CHECK.parse_coverage(gate_document(GOOD_MATRIX, rows))
        self.assertTrue(any("不是合法的断言编号或区间" in error for error in errors), errors)

    def test_duplicate_section_fails(self) -> None:
        rows = GOOD_COVERAGE + "| 首次对话与自动准入 | V-开通-01 | — |\n"
        _, errors = CHECK.parse_coverage(gate_document(GOOD_MATRIX, rows))
        self.assertTrue(any("重复" in error for error in errors), errors)

    def test_missing_coverage_table_fails(self) -> None:
        _, errors = CHECK.parse_coverage("# 验证与门禁\n\n没有清单。\n")
        self.assertTrue(any("没找到合同条款覆盖清单" in error for error in errors), errors)

    def test_range_expands_and_reports_gap(self) -> None:
        expanded, error = CHECK.expand_reference("V-管理-01…03")
        self.assertIsNone(error)
        self.assertEqual(expanded, ["V-管理-01", "V-管理-02", "V-管理-03"])
        self.assertEqual(CHECK.expand_reference("V-管理-03…01")[1], "区间的起止顺序不对：'V-管理-03…01'")


class CrossCheckTest(unittest.TestCase):
    def _parts(self, matrix_rows: str = GOOD_MATRIX, coverage_rows: str = GOOD_COVERAGE, contract: str = CONTRACT):
        document = gate_document(matrix_rows, coverage_rows)
        statuses, _ = CHECK.parse_matrix(document)
        coverage, _ = CHECK.parse_coverage(document)
        return statuses, coverage, CHECK.contract_sections(contract)

    def test_consistent_documents_pass(self) -> None:
        self.assertEqual(CHECK.cross_check(*self._parts()), [])

    def test_new_contract_section_without_registration_fails(self) -> None:
        contract = CONTRACT + "\n## 新增的一节规则\n\n正文。\n"
        errors = CHECK.cross_check(*self._parts(contract=contract))
        self.assertTrue(any("新增的一节规则" in error for error in errors), errors)

    def test_reference_to_unknown_assertion_fails(self) -> None:
        rows = GOOD_COVERAGE.replace("V-开通-01…03", "V-开通-01、V-开通-77")
        errors = CHECK.cross_check(*self._parts(coverage_rows=rows))
        self.assertTrue(any("不存在的断言：V-开通-77" in error for error in errors), errors)

    def test_range_covering_a_missing_assertion_fails(self) -> None:
        """区间不能成为藏缺口的地方：`…05` 里少一号照样红。"""
        rows = GOOD_COVERAGE.replace("V-开通-01…03", "V-开通-01…05")
        errors = CHECK.cross_check(*self._parts(coverage_rows=rows))
        self.assertTrue(any("V-开通-04" in error for error in errors), errors)

    def test_stale_section_name_fails(self) -> None:
        rows = GOOD_COVERAGE.replace("| 动态工作的去向 |", "| 早已改名的一节 |")
        errors = CHECK.cross_check(*self._parts(coverage_rows=rows))
        self.assertTrue(any("不存在的章节" in error for error in errors), errors)

    def test_third_level_headings_are_registered_too(self) -> None:
        contract = CONTRACT + "\n### 开通流程\n\n正文。\n"
        errors = CHECK.cross_check(*self._parts(contract=contract))
        self.assertTrue(any("开通流程" in error for error in errors), errors)


class RealDocumentTest(unittest.TestCase):
    def test_repository_documents_pass(self) -> None:
        """真实文档必须自洽——否则上面的用例只是在测一份玩具输入。"""
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = CHECK.main()
        self.assertEqual(exit_code, 0, stderr.getvalue())
        self.assertIn("验收矩阵状态列：通过", stdout.getvalue())
        self.assertIn("合同条款覆盖清单：通过", stdout.getvalue())

    def test_every_assertion_in_the_gate_document_has_a_state(self) -> None:
        statuses, errors = CHECK.parse_matrix(CHECK.GATE_DOCUMENT.read_text(encoding="utf-8"))
        self.assertEqual(errors, [])
        self.assertGreater(len(statuses), 100)
        self.assertEqual(set(statuses.values()) - set(CHECK.ASSERTION_STATES), set())


if __name__ == "__main__":
    unittest.main()
