"""Issue #76：源码模块与制品/进程依赖清单的反向对账。

这些用例不只证明当前清单是绿的，还主动删除登记、加入未登记模块和制造错误豁免，
确认门禁确实会变红。否则一份“当前状态通过”的测试无法证明下一次新增模块不会静默
漏检。
"""

from __future__ import annotations

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).parents[1]
SCRIPT = REPOSITORY_ROOT / "scripts" / "ci" / "check_installed_package.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_installed_package_manifest_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CHECKER = _load_checker()


class ModuleManifestReconciliationTest(unittest.TestCase):
    def test_real_repository_manifest_is_complete(self) -> None:
        failures = CHECKER.check_module_manifests()

        self.assertEqual([], failures, "\n".join(failures))
        self.assertEqual(61, len(CHECKER.source_module_names()))
        self.assertEqual(56, len(CHECKER.REQUIRED_MODULES))
        self.assertEqual(5, len(CHECKER.MODULE_MANIFEST_EXEMPTIONS))

    def test_source_enumeration_includes_package_initializers(self) -> None:
        source = CHECKER.source_module_names()

        self.assertIn("lingxi", source)
        self.assertIn("lingxi.apps.worker", source)
        self.assertIn("lingxi.apps.scheduler.__main__", source)

    def test_an_unregistered_source_module_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory) / "lingxi"
            shutil.copytree(CHECKER.SOURCE_ROOT, temporary_root)
            new_module = temporary_root / "core" / "synthetic_new_module.py"
            new_module.write_text("MARKER = 'synthetic'\n", encoding="utf-8")
            ids_module = temporary_root / "core" / "ids.py"
            ids_module.write_text(
                ids_module.read_text(encoding="utf-8")
                + "\nfrom lingxi.core.synthetic_new_module import MARKER\n",
                encoding="utf-8",
            )

            original_root = CHECKER.SOURCE_ROOT
            CHECKER.SOURCE_ROOT = temporary_root
            try:
                source = CHECKER.source_module_names()
                failures = CHECKER.check_module_manifests(source_modules=source)
            finally:
                CHECKER.SOURCE_ROOT = original_root

        self.assertTrue(failures)
        self.assertTrue(any("lingxi.core.synthetic_new_module" in line for line in failures))
        self.assertTrue(any("REQUIRED_MODULES" in line for line in failures))

    def test_removing_an_artifact_registration_is_reported(self) -> None:
        required = tuple(
            name for name in CHECKER.REQUIRED_MODULES if name != "lingxi.apps.worker.report"
        )

        failures = CHECKER.check_module_manifests(required_modules=required)

        self.assertTrue(failures)
        self.assertTrue(any("lingxi.apps.worker.report" in line for line in failures))
        self.assertTrue(any("未登记进 REQUIRED_MODULES" in line for line in failures))

    def test_removing_a_process_registration_is_reported(self) -> None:
        process = dict(CHECKER.PROCESS_RUNTIME_IMPORTS)
        lingxi_modules, third_party_modules = process["worker"]
        process["worker"] = (
            tuple(name for name in lingxi_modules if name != "lingxi.apps.worker.report"),
            third_party_modules,
        )

        failures = CHECKER.check_module_manifests(process_runtime_imports=process)

        self.assertTrue(failures)
        self.assertTrue(any("lingxi.apps.worker.report" in line for line in failures))
        self.assertTrue(any("import 闭包" in line for line in failures))

    def test_an_error_exemption_is_reported(self) -> None:
        exemptions = dict(CHECKER.MODULE_MANIFEST_EXEMPTIONS)
        exemptions["lingxi.apps.worker.report"] = "错误地把正式 worker 模块当成测试资产"

        failures = CHECKER.check_module_manifests(exemptions=exemptions)

        self.assertTrue(failures)
        self.assertTrue(any("错误豁免" in line for line in failures))
        self.assertTrue(any("lingxi.apps.worker.report" in line for line in failures))

    def test_a_process_cannot_depend_on_a_formal_artifact_exemption(self) -> None:
        process = dict(CHECKER.PROCESS_RUNTIME_IMPORTS)
        lingxi_modules, third_party_modules = process["scheduler"]
        process["scheduler"] = (
            (*lingxi_modules, "lingxi.adapters.feishu_onboarding"),
            third_party_modules,
        )

        failures = CHECKER.check_module_manifests(process_runtime_imports=process)

        self.assertTrue(failures)
        self.assertTrue(any("正式制品豁免" in line for line in failures))

    def test_migrate_empty_module_list_requires_an_explicit_boundary(self) -> None:
        self.assertEqual(CHECKER.process_source_closure("migrate"), set())
        self.assertIn("migrate", CHECKER.PROCESS_ENTRY_EXEMPTIONS)


if __name__ == "__main__":
    unittest.main()
