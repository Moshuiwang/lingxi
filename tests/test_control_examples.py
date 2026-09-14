"""控制包引导安装样例的结构、来源和最小披露门禁。"""

from __future__ import annotations

import importlib.util
import json
import re
import stat
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_ROOT = REPOSITORY_ROOT / "deploy"
EXAMPLES_ROOT = DEPLOY_ROOT / "control" / "examples"


def _load_by_path(path: Path, name: str):
    """按工作树绝对路径加载控制包模块，避免误用已安装副本。"""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模块：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# deploy_runtime 和 lingxi_deploy 使用无包名导入；按源码依赖顺序登记别名。
DEPLOY_STATE = _load_by_path(DEPLOY_ROOT / "deploy_state.py", "deploy_state")
CONTROL_BUNDLE = _load_by_path(DEPLOY_ROOT / "control_bundle.py", "control_bundle")
DEPLOY_RUNTIME = _load_by_path(DEPLOY_ROOT / "deploy_runtime.py", "deploy_runtime")
LINGXI_DEPLOY = _load_by_path(DEPLOY_ROOT / "lingxi_deploy.py", "lingxi_deploy")
print(
    "控制包样例测试加载路径："
    f"{DEPLOY_ROOT / 'lingxi_deploy.py'}；"
    f"{DEPLOY_ROOT / 'deploy_runtime.py'}；"
    f"{DEPLOY_ROOT / 'control_bundle.py'}"
)


def _read_json(name: str) -> dict:
    return json.loads((EXAMPLES_ROOT / name).read_text(encoding="utf-8"))


class ControlExamplesTests(unittest.TestCase):
    def test_host_contract_example_passes_validate_host(self) -> None:
        self.assertIsNone(LINGXI_DEPLOY.validate_host(_read_json("host-contract.json")))

    def test_public_config_example_passes_source_validator(self) -> None:
        # 当前源码的 public-config 校验函数实际位于 deploy/lingxi_deploy.py。
        self.assertIsInstance(LINGXI_DEPLOY.public_config(_read_json("public-config.json")), dict)

    def test_relay_example_reaches_install_relay_shape_branch_only(self) -> None:
        configuration = _read_json("innertest-relay.json")

        class ShapeBranchReachedError(Exception):
            pass

        with patch.object(
            CONTROL_BUNDLE,
            "verify_install",
            side_effect=ShapeBranchReachedError,
        ) as verify_install:
            with self.assertRaises(ShapeBranchReachedError):
                CONTROL_BUNDLE.install_relay(
                    Path("/placeholder/control-bundles"),
                    {"sha256": "b" * 64},
                    configuration,
                    Path("/placeholder/relay"),
                )
        verify_install.assert_called_once()

    def test_examples_are_json_and_contain_no_controlled_material(self) -> None:
        for name in (
            "host-contract.json",
            "public-config.json",
            "binding.json",
            "innertest-relay.json",
        ):
            with self.subTest(name=name):
                json.loads((EXAMPLES_ROOT / name).read_text(encoding="utf-8"))

        sample_bytes = b"\n".join(
            (EXAMPLES_ROOT / name).read_bytes()
            for name in (
                "host-contract.json",
                "public-config.json",
                "binding.json",
                "innertest-relay.json",
            )
        )
        for forbidden in (b"ssh-ed25519 AAAA", b"biai", b"biplus", b"/home/wangzp"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, sample_bytes)

    def test_stage_permission_backup_hook_is_safe_placeholder(self) -> None:
        hook = EXAMPLES_ROOT / "hooks" / "stage-permission-table-backup.sh"
        self.assertTrue(hook.is_file())
        self.assertEqual(stat.S_IMODE(hook.stat().st_mode), 0o755)
        text = hook.read_text(encoding="utf-8")
        self.assertIn("backup", text)
        self.assertIn("exec", text)
        for forbidden in ("/home/wangzp", "/tmp/", "localhost", "127.0.0.1"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)
        self.assertTrue(
            {
                "deploy/permission_table_guard.py",
                "deploy/权限发布表预发防护.md",
                "deploy/control/examples/hooks/stage-permission-table-backup.sh",
            }.issubset(CONTROL_BUNDLE.FILES)
        )

    def test_runbook_contains_required_markers(self) -> None:
        runbook = (DEPLOY_ROOT / "control" / "引导安装.md").read_text(encoding="utf-8")
        for marker in ("三路否定", "回退", "historical", "提升即放行"):
            with self.subTest(marker=marker):
                self.assertIn(marker, runbook)
        self.assertIn("/bin/sh", runbook)
        self.assertRegex(runbook, r"(?m)^.*forced-command.*nologin.*$")

    def test_runbook_code_blocks_have_no_bare_python3(self) -> None:
        runbook = (DEPLOY_ROOT / "control" / "引导安装.md").read_text(encoding="utf-8")
        code_blocks = re.findall(r"```[^\n]*\n(.*?)```", runbook, flags=re.DOTALL)
        self.assertFalse(re.search(r"(^|\s)python3\s", "\n".join(code_blocks), flags=re.MULTILINE))


if __name__ == "__main__":
    unittest.main()
