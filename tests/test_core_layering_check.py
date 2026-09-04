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

    def __enter__(self) -> _TempSourceTree:
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


class TransitiveClosureTest(unittest.TestCase):
    """M2（2026-08-19 三方复查最后一轮）：`core/` 即使不直接 import adapters/apps，
    也可能通过一个内部模块间接触达——`find_violations` 只看每个文件的直接
    import 目标，看不到这种传递关系。用一条真实存在于当前仓库的链路
    （`core/conversation/pipeline.py` -> `lingxi.config.content`）的**结构**
    在临时目录里复现：`core/` 模块 import 一个 `config/` 模块，那个 `config/`
    模块再 import adapters——这正是"core/ 已经在 import lingxi.config.*，
    未来任何人往被 import 的模块里加一句 adapters 导入"的无意可达场景。
    """

    def test_core_module_importing_a_config_module_that_imports_adapters_is_caught(self) -> None:
        with _TempSourceTree() as tree:
            # `check_installed_package.py` 的 `_source_imports` 只把 import 目标
            # 算成"lingxi 内部边"，前提是那个目标在源码树里真的能解析到一个
            # 存在的模块——这正是它比裸字符串匹配更严谨的地方（不会把一个从未
            # 存在过的 import 目标误判成"内部依赖"）。因此这条夹具必须真的建出
            # `lingxi/adapters/postgres.py`，不能只在 import 语句里写这个名字。
            adapters_dir = tree.source_root / "adapters"
            adapters_dir.mkdir(parents=True)
            (adapters_dir / "__init__.py").write_text("", encoding="utf-8")
            (adapters_dir / "postgres.py").write_text("def connect():\n    ...\n", encoding="utf-8")

            config_dir = tree.source_root / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "__init__.py").write_text("", encoding="utf-8")
            (config_dir / "content.py").write_text(
                "from lingxi.adapters.postgres import connect\n"
                "\n"
                "def helper():\n"
                "    return connect\n",
                encoding="utf-8",
            )
            tree.write(
                "conversation/pipeline.py",
                "from lingxi.config.content import helper\n\ndef use():\n    return helper()\n",
            )
            (tree.core_root / "conversation" / "__init__.py").write_text("", encoding="utf-8")
            (tree.core_root / "__init__.py").write_text("", encoding="utf-8")

            violations = CHECK.find_transitive_violations(tree.source_root)

        self.assertTrue(violations, "间接触达 adapters 的链路应当被抓到")
        self.assertTrue(any("pipeline.py" in v and "adapters" in v for v in violations), violations)
        # 链条本身要可读，能看出是经由 config.content 触达的，不能只说"违反了"。
        self.assertTrue(any("lingxi.config.content" in v for v in violations), violations)

    def test_core_module_importing_a_clean_config_module_is_not_flagged(self) -> None:
        with _TempSourceTree() as tree:
            config_dir = tree.source_root / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "__init__.py").write_text("", encoding="utf-8")
            (config_dir / "content.py").write_text("VALUE = 1\n", encoding="utf-8")
            tree.write(
                "conversation/pipeline.py",
                "from lingxi.config.content import VALUE\n",
            )
            (tree.core_root / "conversation" / "__init__.py").write_text("", encoding="utf-8")
            (tree.core_root / "__init__.py").write_text("", encoding="utf-8")

            violations = CHECK.find_transitive_violations(tree.source_root)

        self.assertEqual(violations, [])


class RealRepositoryTransitiveClosureTest(unittest.TestCase):
    """反向验证：真实仓库当前的 `core/` 传递闭包必须零违规（不只是直接 import
    零违规）。"""

    def test_real_core_tree_has_zero_transitive_violations(self) -> None:
        self.assertEqual(CHECK.find_transitive_violations(), [])


if __name__ == "__main__":
    unittest.main()
