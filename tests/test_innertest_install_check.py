"""安装自检的 socket 目录权限与清单形状断言。"""

import hashlib
import importlib.util
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts/admin/innertest_install_check.py"
SPEC = importlib.util.spec_from_file_location("innertest_install_check_under_test", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"无法加载模块：{MODULE_PATH}")
CHECK_MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CHECK_MODULE
SPEC.loader.exec_module(CHECK_MODULE)
print(f"安装自检测试加载路径：{MODULE_PATH}")


class InnertestInstallCheckTests(unittest.TestCase):
    RELAY_UID = 41001
    RELAY_GID = 41001

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.socket_directory = self.root / "socket"
        self.socket_directory.mkdir()
        self.manifest = self._manifest()

    def _manifest(self) -> dict:
        python = self.root / "python"
        relay = self.root / "relay.py"
        relay_config = self.root / "relay.json"
        binding = self.root / "binding.json"
        authorized_keys = self.root / "authorized_keys"
        python.write_bytes(b"python")
        relay.write_bytes(b"relay")
        relay_config.write_text("{}", encoding="utf-8")
        binding.write_text("{}", encoding="utf-8")
        command = f'command="{python} -I -S {relay}"'
        authorized_keys.write_text(
            f"restrict,no-user-rc,{command} ssh-ed25519 synthetic-key\n", encoding="utf-8"
        )
        return {
            "schema_revision": 1,
            "python": str(python),
            "relay": str(relay),
            "relay_sha256": hashlib.sha256(relay.read_bytes()).hexdigest(),
            "relay_config": str(relay_config),
            "binding": str(binding),
            "socket_directory": str(self.socket_directory),
            "relay_uid": self.RELAY_UID,
            "relay_gid": self.RELAY_GID,
            "socket_owner_uid": 10001,
            "socket_gid": 42001,
            "authorized_keys": str(authorized_keys),
        }

    def _check(self, **overrides):
        manifest = dict(self.manifest)
        socket_uid = overrides.get("socket_uid", manifest["socket_owner_uid"])
        socket_gid = overrides.get("socket_gid_value", manifest["socket_gid"])
        socket_mode = overrides.get("socket_mode", 0o750)
        parent_uid = overrides.get("parent_uid", 0)
        parent_mode = overrides.get("parent_mode", 0o750)

        def fake_lstat(path):
            path = Path(path)
            if path == self.socket_directory:
                return SimpleNamespace(
                    st_uid=socket_uid,
                    st_gid=socket_gid,
                    st_mode=stat.S_IFDIR | socket_mode,
                )
            if path == self.socket_directory.parent:
                return SimpleNamespace(
                    st_uid=parent_uid,
                    st_gid=0,
                    st_mode=stat.S_IFDIR | parent_mode,
                )
            return SimpleNamespace(st_uid=0, st_gid=0, st_mode=stat.S_IFREG | 0o644)

        with (
            patch.object(Path, "lstat", new=fake_lstat),
            patch(
                "pwd.getpwuid",
                return_value=SimpleNamespace(
                    pw_name="relay", pw_gid=self.RELAY_GID, pw_uid=self.RELAY_UID
                ),
            ),
            patch("os.getgrouplist", return_value=[self.RELAY_GID]),
            patch("grp.getgrnam", side_effect=KeyError),
        ):
            return CHECK_MODULE.check(manifest)

    def test_socket_directory_owned_by_socket_owner_passes(self) -> None:
        result = self._check()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["errors"], [])

    def test_socket_directory_owned_by_root_is_rejected(self) -> None:
        result = self._check(socket_uid=0)
        self.assertIn("socket_directory_owner_invalid", result["errors"])

    def test_socket_directory_group_writable_is_rejected(self) -> None:
        result = self._check(socket_mode=0o770)
        self.assertIn("socket_directory_mode_invalid", result["errors"])

    def test_socket_directory_parent_must_be_root_protected(self) -> None:
        result = self._check(parent_uid=self.RELAY_UID)
        self.assertIn("socket_directory_parent_not_protected", result["errors"])

    def test_manifest_requires_socket_owner_and_gid(self) -> None:
        for field in ("socket_owner_uid", "socket_gid"):
            with self.subTest(field=field):
                missing = dict(self.manifest)
                del missing[field]
                self.assertEqual(CHECK_MODULE.check(missing)["errors"], ["schema_invalid"])

                wrong_type = dict(self.manifest, **{field: "42001"})
                self.assertEqual(CHECK_MODULE.check(wrong_type)["errors"], ["schema_invalid"])

    def test_forced_command_shape_still_enforced(self) -> None:
        authorized_keys = Path(self.manifest["authorized_keys"])
        authorized_keys.write_text(
            "restrict,no-user-rc ssh-ed25519 synthetic-key\n", encoding="utf-8"
        )
        result = self._check()
        self.assertIn("forced_command_invalid", result["errors"])


if __name__ == "__main__":
    unittest.main()
