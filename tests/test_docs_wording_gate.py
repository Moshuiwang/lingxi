"""``scripts/ci/check_docs_wording.py`` 的钉住用例（Issue #597）。

豁免清单只许变短，不许变长：判定方式是**子集**——删掉一条允许，新增一个文件即红。
这正是 ``tests/test_ruff_config.py`` 钉住 per-file-ignores 的同一条纪律：一份靠"往
里加一行"就能放行的清单不是门禁，是登记簿。清单要放宽，必须同时改这份快照并在 PR
正文说明理由，让"开后门"这件事在 diff 里无处躲。

本文件同样不写出被禁的那个字（写了会被门禁自己扫红），一律取
``GATE.BANNED_CHARACTER`` 在运行期构造。

变异实测：往 ``EXEMPT_FILES`` 里临时加一个文件（例如 ``docs/README.md``），跑本文件
应判红；还原后清 ``__pycache__`` 复跑应绿。
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

REPO_ROOT = Path(__file__).parents[1]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "check_docs_wording.py"


def _load_script():
    """按 tests/test_ci_extras_matrix.py 的既有写法加载门禁脚本。"""

    spec = importlib.util.spec_from_file_location("check_docs_wording_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


GATE = _load_script()

#: 豁免清单的逐字快照。前两条是产品负责人 2026-09-06 裁定不动的已收口 Trace 合同；
#: 中间三条是本批三件套（正文本身就在讨论这个被禁词）；最后一条是替换表所在处。
PINNED_EXEMPTIONS = frozenset(
    [
        "docs/traces/469-rc22打磨与体验批/合同.md",
        "docs/traces/521-rc24正式上线/合同.md",
        "docs/traces/630-清仓批一/合同.md",
        "docs/traces/630-清仓批一/任务表.md",
        "docs/traces/630-清仓批一/验收.md",
        "docs/技术设计/代码规范.md",
    ]
)


def _run(root: Path, tracked: list[str], exempt: dict[str, str]) -> tuple[int, str]:
    """真实调用 ``run()``，返回退出码与它打印的全部文字。"""

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = GATE.run(root, tracked, exempt)
    return code, out.getvalue() + err.getvalue()


class ExemptionListOnlyShrinksTest(unittest.TestCase):
    """豁免清单只能变短——这条断言是本门禁唯一的防开后门机制。"""

    def test_exemptions_are_a_subset_of_the_pinned_snapshot(self) -> None:
        added = sorted(set(GATE.EXEMPT_FILES) - PINNED_EXEMPTIONS)
        self.assertEqual(
            added,
            [],
            f"用词门禁的豁免清单出现了钉住快照之外的新条目：{added}。"
            "这份清单**只能变短、不能变长**：删掉一条允许，新增一个文件即红——"
            "否则任何人都能靠往清单里加文件绕过门禁。确实要放宽，"
            "必须同时改 PINNED_EXEMPTIONS 并在 PR 正文写明裁定依据。",
        )

    def test_every_exemption_carries_a_reason(self) -> None:
        for path, reason in GATE.EXEMPT_FILES.items():
            with self.subTest(path=path):
                self.assertIsInstance(reason, str)
                self.assertTrue(reason.strip(), f"豁免 {path} 没有写理由")

    def test_exemptions_are_exact_paths_not_directory_globs(self) -> None:
        for path in GATE.EXEMPT_FILES:
            with self.subTest(path=path):
                self.assertNotIn("*", path, "豁免不接受通配：一个通配等于放行整个目录")
                self.assertTrue((REPO_ROOT / path).is_file(), f"豁免 {path} 不是一个真实文件")


class GateBehaviourTest(unittest.TestCase):
    """用真实临时文件驱动 ``run()``：命中判红、干净判绿，不 mock 文件系统。"""

    def test_a_tracked_file_containing_the_banned_word_is_red(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "note.md").write_text(
                f"第一行\n第二{GATE.BANNED_CHARACTER}\n", encoding="utf-8"
            )
            code, output = _run(root, ["note.md"], {})

        self.assertEqual(code, 1)
        self.assertIn("note.md:2", output)
        self.assertIn("环节", output)

    def test_a_tracked_file_without_the_banned_word_is_green(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "note.md").write_text("入站环节与回调环节都通了\n", encoding="utf-8")
            code, _ = _run(root, ["note.md"], {})

        self.assertEqual(code, 0)

    def test_a_binary_file_is_not_a_blind_spot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payload = b"\x00\xff" + GATE.BANNED_CHARACTER.encode("utf-8") + b"\x00"
            (root / "blob.bin").write_bytes(payload)
            code, output = _run(root, ["blob.bin"], {})

        self.assertEqual(code, 1)
        self.assertIn("blob.bin:1", output)

    def test_a_stale_exemption_is_red(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "note.md").write_text("干净正文\n", encoding="utf-8")
            code, output = _run(root, ["note.md"], {"gone.md": "理由"})

        self.assertEqual(code, 1)
        self.assertIn("gone.md", output)

    def test_an_exemption_that_no_longer_matches_is_red(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "note.md").write_text("这一行已经没有被禁的词了\n", encoding="utf-8")
            code, output = _run(root, ["note.md"], {"note.md": "理由"})

        self.assertEqual(code, 1)
        self.assertIn("note.md", output)

    def test_an_exemption_without_a_reason_is_red(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "note.md").write_text(GATE.BANNED_CHARACTER + "\n", encoding="utf-8")
            code, _ = _run(root, ["note.md"], {"note.md": "   "})

        self.assertEqual(code, 1)

    def test_an_unreadable_tracked_file_is_red(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            code, output = _run(root, ["never-created.md"], {})

        self.assertEqual(code, 1)
        self.assertIn("never-created.md", output)


class FailClosedScanSurfaceTest(unittest.TestCase):
    """扫描面拿不到就必须判红：扫不动不等于没问题。"""

    def test_a_directory_without_git_yields_no_scan_surface(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            self.assertIsNone(GATE.tracked_files(Path(raw)))

    def test_an_empty_repository_yields_no_scan_surface(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            subprocess.run(["git", "init", "-q", raw], check=True, capture_output=True)
            self.assertIsNone(GATE.tracked_files(Path(raw)))


class WiringTest(unittest.TestCase):
    """两条门禁入口都必须调用本检查——只挂一条就有盲区。"""

    #: docs 作业只在 docs_changed=true 时触发，纯代码 PR（只改 .py/.yml）跑不到它；
    #: 被禁词的存量恰恰主要住在 ci.yml、tests/ 与 scripts/ 里。因此文档路径与代码
    #: 路径两个入口都要挂，少一个就等于给它留了最常走的那条入口。
    WIRED_ENTRY_POINTS = ("verify_docs.sh", "verify_repository.sh")

    def test_both_gate_entry_points_invoke_the_check(self) -> None:
        for name in self.WIRED_ENTRY_POINTS:
            with self.subTest(entry_point=name):
                text = (REPO_ROOT / "scripts" / "ci" / name).read_text(encoding="utf-8")
                self.assertIn(
                    "python3 scripts/ci/check_docs_wording.py",
                    text,
                    f"{name} 不再调用用词门禁：少挂一个入口，那条路径上的改动就再也扫不到",
                )


class RealRepositoryTest(unittest.TestCase):
    """脚本本身与整个仓库的现状：命令行入口真跑一次，退出码必须为 0。"""

    def test_the_gate_source_never_spells_the_banned_word(self) -> None:
        banned = GATE.BANNED_CHARACTER.encode("utf-8")
        for path in (SCRIPT, Path(__file__)):
            with self.subTest(path=path.name):
                self.assertNotIn(
                    banned,
                    path.read_bytes(),
                    "门禁脚本与本用例都不许写出被禁的字面量，否则门禁会判自己红，"
                    "或被迫把自己加进豁免清单——那是第一个后门",
                )

    def test_the_repository_passes_through_the_command_line_entry_point(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-B", str(SCRIPT)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
