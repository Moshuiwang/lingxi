"""`scripts/ci/check_core_layering.py` 的分层 import 判定用例（Issue #238）。

每个用例先在一个临时目录里搭一棵最小的 ``lingxi.core`` 包，写一个会违规的模块，
断言它被具体地拒绝；最后一条反过来跑真实仓库的 ``src/lingxi/core``，
证明检查不是靠临时夹具"空转"出来的绿灯。
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "ci" / "check_core_layering.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("core_layering_check_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CHECK = _load_script()


class _TempSourceTree:
    """搭一棵 ``<root>/lingxi/core/...`` 目录，把 ``find_violations`` 指向它。"""

    def __enter__(self) -> "_TempSourceTree":
        self._tmp = tempfile.TemporaryDirectory()
        self.source_root = Path(self._tmp.name) / "lingxi"
        self.core_root = self.source_root / "core"
        self.core_root.mkdir(parents=True)
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._tmp.cleanup()

    def write(self, relative: str, content: str) -> Path:
        path = self.core_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path


class ModuleLevelImportTest(unittest.TestCase):
    def test_module_level_external_sdk_import_is_rejected(self) -> None:
        with _TempSourceTree() as tree:
            path = tree.write("ids.py", "import httpx\n")
            violations = CHECK.find_violations(path, source_root=tree.source_root)
        self.assertTrue(any("httpx" in v for v in violations), violations)

    def test_module_level_adapters_import_is_rejected(self) -> None:
        with _TempSourceTree() as tree:
            path = tree.write("ids.py", "from lingxi.adapters.postgres import connect\n")
            violations = CHECK.find_violations(path, source_root=tree.source_root)
        self.assertTrue(any("lingxi.adapters.postgres" in v for v in violations), violations)

    def test_module_level_apps_import_is_rejected(self) -> None:
        with _TempSourceTree() as tree:
            path = tree.write("ids.py", "import lingxi.apps.gateway\n")
            violations = CHECK.find_violations(path, source_root=tree.source_root)
        self.assertTrue(any("lingxi.apps.gateway" in v for v in violations), violations)

    def test_stdlib_and_sibling_core_imports_are_allowed(self) -> None:
        with _TempSourceTree() as tree:
            tree.write("helpers.py", "VALUE = 1\n")
            path = tree.write(
                "ids.py",
                "from __future__ import annotations\nimport hashlib\nimport uuid\nfrom lingxi.core.helpers import VALUE\nfrom lingxi.config import content\n",
            )
            violations = CHECK.find_violations(path, source_root=tree.source_root)
        self.assertEqual(violations, [])


class DeferredImportTest(unittest.TestCase):
    """本仓库的第三方 import 全在函数体里；只扫模块级等于没扫。"""

    def test_function_body_external_sdk_import_is_caught(self) -> None:
        with _TempSourceTree() as tree:
            path = tree.write(
                "permission/publish.py",
                "def do_it():\n    import psycopg\n    return psycopg\n",
            )
            violations = CHECK.find_violations(path, source_root=tree.source_root)
        self.assertTrue(any("psycopg" in v for v in violations), violations)

    def test_nested_try_block_deferred_import_is_caught(self) -> None:
        with _TempSourceTree() as tree:
            path = tree.write(
                "permission/publish.py",
                "def do_it():\n    try:\n        import claude_agent_sdk\n    except ImportError:\n        return None\n    return claude_agent_sdk\n",
            )
            violations = CHECK.find_violations(path, source_root=tree.source_root)
        self.assertTrue(any("claude_agent_sdk" in v for v in violations), violations)

    def test_class_method_body_relative_import_of_adapters_is_caught(self) -> None:
        with _TempSourceTree() as tree:
            path = tree.write(
                "permission/publish.py",
                "class Runner:\n    def run(self):\n        from ...adapters.postgres import connect\n        return connect\n",
            )
            violations = CHECK.find_violations(path, source_root=tree.source_root)
        self.assertTrue(any("adapters.postgres" in v for v in violations), violations)

    def test_clean_module_with_deferred_stdlib_import_passes(self) -> None:
        with _TempSourceTree() as tree:
            path = tree.write(
                "permission/publish.py",
                "def do_it():\n    import json\n    return json\n",
            )
            violations = CHECK.find_violations(path, source_root=tree.source_root)
        self.assertEqual(violations, [])


class FailClosedTest(unittest.TestCase):
    def test_missing_core_directory_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist"
            with self.assertRaises(CHECK.LayeringCheckError):
                CHECK.iter_core_files(core_root=missing)

    def test_syntax_error_fails_closed(self) -> None:
        with _TempSourceTree() as tree:
            path = tree.write("broken.py", "def broken(:\n")
            with self.assertRaises(CHECK.LayeringCheckError):
                CHECK.find_violations(path, source_root=tree.source_root)


class RealRepositoryTest(unittest.TestCase):
    """反向验证：真实的 ``src/lingxi/core`` 当前必须零违规。"""

    def test_real_core_tree_has_zero_violations(self) -> None:
        files = CHECK.iter_core_files()
        violations: list[str] = []
        for path in files:
            violations.extend(CHECK.find_violations(path))
        self.assertEqual(violations, [], violations)
        self.assertGreater(len(files), 10)


if __name__ == "__main__":
    unittest.main()
