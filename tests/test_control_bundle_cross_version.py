"""控制包跨版本核对：文件集合以包自身索引为准，逐成员安全检查一条不放松。

背景（Trace #770 批次 2 事故）：旧 `verify()` 要求 tar 成员集合恰等于当前版本的 `FILES`，
v2.4.3 的 15 成员控制包被 2.5.0 工具拒绝，预发升级停在 `current_release`。这里的用例分别钉住
「子集包 / 超集包能过」和「越界路径、缺必备文件、tar 与索引不一致、安装目录多放少放都拒」。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from test_deploy_automation import ROOT, archive, bundle, package

# rc.98 真实控制包的形状：必备文件加三份只供人读的说明 / 示例。
LEGACY_FILES = (
    *bundle.REQUIRED_FILES,
    "deploy/control/sshd_config.example",
    "deploy/control/authorized_keys.example",
    "deploy/control/README.md",
)
FUTURE_FILE = "deploy/control/examples/future.json"


def craft(root, tar_files, index_files):
    """tar 与索引可以不一致地造包，用来钉住「多一个 / 少一个都拒」。"""
    contents = {}
    for name in {*tar_files, *index_files}:
        source = ROOT / name
        contents[name] = (
            source.read_bytes()
            if source.is_file()
            else ("temporary control fixture: " + name + "\n").encode()
        )
    entries = [
        {
            "path": name,
            "sha256": bundle.digest(contents[name]),
            "size": len(contents[name]),
            "mode": 0o444,
            "source_commit": "a" * 40,
        }
        for name in index_files
    ]
    index = {
        "schema_revision": 1,
        "source_commit": "a" * 40,
        "runtime": bundle.RUNTIME,
        "files": entries,
    }
    index_bytes = bundle.canonical(index)
    archived = {name: contents[name] for name in tar_files}
    archived[bundle.INDEX] = index_bytes
    path = root / bundle.ASSET
    archive(path, archived)
    metadata = {
        "asset": bundle.ASSET,
        "sha256": bundle.digest(path.read_bytes()),
        "index_sha256": bundle.digest(index_bytes),
        "schema_revision": 1,
        "source_commit": "a" * 40,
        "runtime": bundle.RUNTIME,
    }
    return path, metadata


def install_root(root):
    """安装根由 root 预先建立且不可由受限账号写入；显式定权限，不依赖当前 umask。"""
    installed = root / "installed"
    installed.mkdir()
    installed.chmod(0o755)
    return installed


def release(target):
    """只读安装目录在临时目录清理前要放开写权限。"""
    for path in [target, *target.rglob("*")]:
        if path.is_dir():
            path.chmod(0o700)


class CrossVersionAcceptTests(unittest.TestCase):
    def test_required_files_are_subset_of_build_files(self):
        self.assertTrue(set(bundle.REQUIRED_FILES) <= set(bundle.FILES))
        self.assertTrue(all(bundle.allowed_path(name) for name in bundle.FILES))

    def test_legacy_subset_package_verifies_installs_and_activates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg, metadata = package(root, files=LEGACY_FILES)
            index, contents = bundle.verify(pkg, metadata)
            self.assertEqual(set(contents), {*LEGACY_FILES, bundle.INDEX})
            installed = install_root(root)
            target = bundle.install(pkg, metadata, installed)
            self.assertEqual(bundle.verify_install(target, metadata), target)
            bundle.activate(installed, metadata)
            self.assertEqual(os.readlink(installed / "current"), target.name)
            release(target)

    def test_future_superset_package_verifies_and_installs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg, metadata = package(root, files=(*bundle.FILES, FUTURE_FILE))
            index, contents = bundle.verify(pkg, metadata)
            self.assertIn(FUTURE_FILE, contents)
            target = bundle.install(pkg, metadata, install_root(root))
            self.assertTrue((target / FUTURE_FILE).is_file())
            release(target)


class CrossVersionRejectTests(unittest.TestCase):
    def test_path_policy(self):
        for name in ("deploy/x.py", "scripts/ci/x.py", "scripts/admin/x.py", "deploy/a/b/c"):
            with self.subTest(name=name):
                self.assertTrue(bundle.allowed_path(name))
        for name in (
            "scripts/evil.py",
            "scripts/dev/x.py",
            "etc/passwd",
            "/deploy/x",
            "deploy//x",
            "deploy/./x",
            "deploy/../x",
            "deploy/x/",
            "deploy/",
            "deploy2/x",
            "src/lingxi/x.py",
            bundle.INDEX,
        ):
            with self.subTest(name=name):
                self.assertFalse(bundle.allowed_path(name))

    def test_out_of_bounds_paths_in_index_and_tar_rejected(self):
        for evil in ("scripts/evil.py", "etc/passwd", "deploy//x", "deploy/./x"):
            with self.subTest(path=evil), tempfile.TemporaryDirectory() as tmp:
                files = (*bundle.REQUIRED_FILES, evil)
                pkg, metadata = craft(Path(tmp), files, files)
                with self.assertRaises(bundle.BundleError):
                    bundle.verify(pkg, metadata)

    def test_missing_required_file_rejected_even_when_index_is_consistent(self):
        for missing in bundle.REQUIRED_FILES:
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as tmp:
                files = tuple(name for name in LEGACY_FILES if name != missing)
                pkg, metadata = package(Path(tmp), files=files)
                with self.assertRaises(bundle.BundleError):
                    bundle.verify(pkg, metadata)

    def test_tar_and_index_must_list_the_same_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg, metadata = craft(
                Path(tmp), (*bundle.REQUIRED_FILES, "deploy/extra.txt"), bundle.REQUIRED_FILES
            )
            with self.assertRaises(bundle.BundleError):
                bundle.verify(pkg, metadata)
        with tempfile.TemporaryDirectory() as tmp:
            pkg, metadata = craft(
                Path(tmp), bundle.REQUIRED_FILES, (*bundle.REQUIRED_FILES, "deploy/ghost.txt")
            )
            with self.assertRaises(bundle.BundleError):
                bundle.verify(pkg, metadata)

    def test_duplicate_index_entry_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg, metadata = craft(
                Path(tmp), bundle.REQUIRED_FILES, (*bundle.REQUIRED_FILES, bundle.REQUIRED_FILES[0])
            )
            with self.assertRaises(bundle.BundleError):
                bundle.verify(pkg, metadata)

    def test_installed_directory_extra_or_missing_file_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg, metadata = package(root, files=LEGACY_FILES)
            target = bundle.install(pkg, metadata, install_root(root))
            folder = target / "deploy/control"
            folder.chmod(0o755)
            extra = folder / "extra.txt"
            extra.write_bytes(b"not in index\n")
            extra.chmod(0o444)
            folder.chmod(0o555)
            with self.assertRaises(bundle.BundleError):
                bundle.verify_install(target, metadata)
            folder.chmod(0o755)
            extra.unlink()
            folder.chmod(0o555)
            bundle.verify_install(target, metadata)
            folder.chmod(0o755)
            (folder / "README.md").unlink()
            folder.chmod(0o555)
            with self.assertRaises(bundle.BundleError):
                bundle.verify_install(target, metadata)
            release(target)

    def test_installed_index_must_match_expected_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg, metadata = package(root, files=LEGACY_FILES)
            target = bundle.install(pkg, metadata, install_root(root))
            index = json.loads((target / bundle.INDEX).read_bytes())
            self.assertEqual({x["path"] for x in index["files"]}, set(LEGACY_FILES))
            with self.assertRaises(bundle.BundleError):
                bundle.verify_install(target, dict(metadata, index_sha256="0" * 64))
            release(target)


if __name__ == "__main__":
    unittest.main()
