"""Issue #76：源码模块与制品/进程依赖清单的反向对账。

这些用例不只证明当前清单是绿的，还主动删除登记、加入未登记模块和制造错误豁免，
确认门禁确实会变红。否则一份“当前状态通过”的测试无法证明下一次新增模块不会静默
漏检。
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
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


def _run_source_only_with_edit(edit):
    """在临时仓库中改写检查器源码，并走真实 CLI 路径取得退出码。"""

    with tempfile.TemporaryDirectory(prefix="issue76-manifest-") as directory:
        temporary_repository = Path(directory) / "repository"
        temporary_script = temporary_repository / "scripts" / "ci" / SCRIPT.name
        temporary_script.parent.mkdir(parents=True)
        shutil.copytree(
            REPOSITORY_ROOT / "src" / "lingxi",
            temporary_repository / "src" / "lingxi",
        )
        original = SCRIPT.read_text(encoding="utf-8")
        temporary_script.write_text(edit(original), encoding="utf-8")
        return subprocess.run(
            [str(Path(sys.executable).resolve()), str(temporary_script), "--source-only"],
            cwd=temporary_repository,
            capture_output=True,
            text=True,
            check=False,
        )


class ModuleManifestReconciliationTest(unittest.TestCase):
    def test_real_repository_manifest_is_complete(self) -> None:
        failures = CHECKER.check_module_manifests()
        source = CHECKER.source_module_names()
        artifact_modules = set(CHECKER.REQUIRED_MODULES)
        exempted_modules = set(CHECKER.MODULE_MANIFEST_EXEMPTIONS)

        self.assertEqual([], failures, "\n".join(failures))
        self.assertSetEqual(source, artifact_modules | exempted_modules)
        self.assertSetEqual(set(), artifact_modules & exempted_modules)

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

    def test_source_edit_formal_module_to_unapproved_exemption_fails(self) -> None:
        def edit(source: str) -> str:
            self.assertIn('    "lingxi.adapters.galaxy_import",\n', source)
            self.assertIn("MODULE_MANIFEST_EXEMPTIONS: dict[str, str] = {\n", source)
            source = source.replace('    "lingxi.adapters.galaxy_import",\n', "", 1)
            return source.replace(
                "MODULE_MANIFEST_EXEMPTIONS: dict[str, str] = {\n",
                'MODULE_MANIFEST_EXEMPTIONS: dict[str, str] = {\n'
                '    "lingxi.adapters.galaxy_import": "临时随意豁免理由",\n',
                1,
            )

        result = _run_source_only_with_edit(edit)
        output = result.stdout + result.stderr

        self.assertNotEqual(0, result.returncode, output)
        self.assertIn("lingxi.adapters.galaxy_import", output)
        self.assertIn("不是已批准的模块豁免", output)

    def test_source_edit_module_exemption_reason_fails(self) -> None:
        # 2026-08-23 #146 清退后示例改用 feishu_bitable_association——它未随本轮
        # 清退变动，仍是 MODULE_MANIFEST_EXEMPTIONS 里理由文本保持不变的一项。
        def edit(source: str) -> str:
            old = '"lingxi.adapters.feishu_bitable_association": "Bot-Test 历史测试资产，不纳入正式用户路径清单"'
            self.assertIn(old, source)
            return source.replace(old, '"lingxi.adapters.feishu_bitable_association": "被改写的豁免理由"', 1)

        result = _run_source_only_with_edit(edit)
        output = result.stdout + result.stderr

        self.assertNotEqual(0, result.returncode, output)
        self.assertIn("lingxi.adapters.feishu_bitable_association", output)
        self.assertIn("理由与已批准政策不一致", output)

    def test_source_edit_migrate_entry_reason_fails(self) -> None:
        def edit(source: str) -> str:
            old = '"migrate": "迁移作业只运行 alembic upgrade，不绑定 lingxi 运行时模块"'
            self.assertIn(old, source)
            return source.replace(old, '"migrate": "被改写的迁移入口理由"', 1)

        result = _run_source_only_with_edit(edit)
        output = result.stdout + result.stderr

        self.assertNotEqual(0, result.returncode, output)
        self.assertIn("PROCESS_ENTRY_EXEMPTIONS", output)
        self.assertIn("进程入口豁免发生漂移", output)

    def test_a_process_cannot_depend_on_a_formal_artifact_exemption(self) -> None:
        # 2026-08-24 #203 清退后示例改用 feishu_bitable_association——原示例
        # oauth_bridge 已随本轮清退删除，不再是 MODULE_MANIFEST_EXEMPTIONS 里的
        # 豁免模块；feishu_bitable_association 是清退后仍保留的唯一豁免项。
        process = dict(CHECKER.PROCESS_RUNTIME_IMPORTS)
        lingxi_modules, third_party_modules = process["scheduler"]
        process["scheduler"] = (
            (*lingxi_modules, "lingxi.adapters.feishu_bitable_association"),
            third_party_modules,
        )

        failures = CHECKER.check_module_manifests(process_runtime_imports=process)

        self.assertTrue(failures)
        self.assertTrue(any("正式制品豁免" in line for line in failures))

    def test_migrate_empty_module_list_requires_an_explicit_boundary(self) -> None:
        self.assertEqual(CHECKER.process_source_closure("migrate"), set())
        self.assertIn("migrate", CHECKER.PROCESS_ENTRY_EXEMPTIONS)

    def test_a_parent_package_init_gaining_a_dependency_is_reported(self) -> None:
        """Issue #116：父包 `__init__` 新增未登记依赖必须让进程闭包门禁变红。

        Python 导入 `lingxi.core.execution.audit`（worker 已登记的入口之一）前会先
        执行 `lingxi/core/__init__.py`；加固前的闭包计算只追最深子模块，看不见父包
        `__init__` 里新增的 import，问题会留到干净镜像启动才暴露。这里直接在父包
        `__init__` 里加一个新依赖，断言闭包能追到它、且未登记时门禁报红。
        """

        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory) / "lingxi"
            shutil.copytree(CHECKER.SOURCE_ROOT, temporary_root)

            new_module = temporary_root / "core" / "synthetic_parent_dependency.py"
            new_module.write_text("MARKER = 'synthetic'\n", encoding="utf-8")
            core_init = temporary_root / "core" / "__init__.py"
            core_init.write_text(
                core_init.read_text(encoding="utf-8")
                + "\nfrom lingxi.core.synthetic_parent_dependency import MARKER\n",
                encoding="utf-8",
            )

            original_root = CHECKER.SOURCE_ROOT
            CHECKER.SOURCE_ROOT = temporary_root
            try:
                files = CHECKER.source_module_files()
                worker_closure = CHECKER.process_source_closure("worker", files)
                failures = CHECKER.check_module_manifests(
                    source_modules=set(files),
                    required_modules=(
                        *CHECKER.REQUIRED_MODULES,
                        "lingxi.core.synthetic_parent_dependency",
                    ),
                )
            finally:
                CHECKER.SOURCE_ROOT = original_root

        self.assertIn(
            "lingxi.core.synthetic_parent_dependency",
            worker_closure,
            "父包 __init__ 新增的依赖必须出现在依赖它的进程闭包里",
        )
        self.assertTrue(failures)
        self.assertTrue(
            any("lingxi.core.synthetic_parent_dependency" in line for line in failures)
        )
        self.assertTrue(any("import 闭包" in line for line in failures))


if __name__ == "__main__":
    unittest.main()
