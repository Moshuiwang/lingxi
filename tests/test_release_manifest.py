"""发布资格与镜像提升的正反用例；网络调用由真实 GitHub 演练另行覆盖。"""

import copy
import hashlib
import importlib
import importlib.util
import io
import os
import re
import shutil
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/ci"))
sys.path.insert(0, str(ROOT / "deploy"))
guard = importlib.import_module("check_release_flow")
release = importlib.import_module("release_manifest")
proof = importlib.import_module("verify_epic_candidate")
writer = importlib.import_module("write_epic_candidate")
bundle = importlib.import_module("control_bundle")


def candidate():
    return {
        "schema": 1,
        "repository": "Moshuiwang/lingxi",
        "tag": "v2.3.2-rc.42",
        "version": "2.3.2",
        "prerelease": True,
        "branch": "release/2.3",
        "commit": "a" * 40,
        "tree": "b" * 40,
        "run_id": 42,
        "migration_heads": ["0090_delivery_retry_backoff"],
        "images": {
            s: f"ghcr.io/moshuiwang/lingxi-{s}@sha256:" + "c" * 64 for s in release.SERVICES
        },
    }


def receipt(doc):
    return {
        "schema": 1,
        "candidate_tag": doc["tag"],
        "manifest_sha256": release.fingerprint(doc),
        "result": "passed",
        "evidence_url": "https://github.com/Moshuiwang/lingxi/issues/669#issuecomment-123",
        "recovery": {
            "previous_tag": "v2.3.1",
            "instructions": "复用已记录镜像，数据库结构未变化",
            "evidence_url": "https://github.com/Moshuiwang/lingxi/issues/669#issuecomment-124",
        },
    }


def control_package(root, *, index_tail=b""):
    """造一份能通过控制包核对的最小包；index_tail 只改写 tar 内嵌索引，模拟包内索引被改。"""
    contents = {name: f"fixture: {name}\n".encode() for name in bundle.REQUIRED_FILES}
    entries = [
        {
            "path": name,
            "sha256": bundle.digest(data),
            "mode": 0o444,
            "size": len(data),
            "source_commit": "a" * 40,
        }
        for name, data in contents.items()
    ]
    index = bundle.canonical(
        {
            "schema_revision": 1,
            "source_commit": "a" * 40,
            "runtime": bundle.RUNTIME,
            "files": entries,
        }
    )
    contents[bundle.INDEX] = index + index_tail
    path = root / bundle.ASSET
    with tarfile.open(path, "w", format=tarfile.USTAR_FORMAT) as archive:
        for name, data in contents.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o444, 0
            archive.addfile(info, io.BytesIO(data))
    metadata = {
        "asset": bundle.ASSET,
        "sha256": bundle.digest(path.read_bytes()),
        "index_sha256": bundle.digest(index),
        "schema_revision": 1,
        "source_commit": "a" * 40,
        "runtime": bundle.RUNTIME,
    }
    return path, metadata, index


def candidate_with_bundle(metadata):
    return dict(
        candidate(),
        schema=2,
        tag="v2.4.0-rc.7",
        version="2.4.0",
        branch="release/2.4",
        control_bundle=metadata,
    )


class FakeGitHub:
    """内存里的 GitHub：只模拟发布脚本会碰到的 api 路径与 gh release 子命令，记录每次上传。"""

    def __init__(self):
        self.releases = []
        self.assets = {}
        self.tags = {}
        self.uploads = []
        self.downloads = []

    def seed(self, doc, assets, *, draft):
        """预置一份 Release（中断残留的草稿或已发布的候选）；assets 是名称到字节。"""
        record = {
            "id": len(self.releases) + 1,
            "tag_name": doc["tag"],
            "draft": draft,
            "prerelease": doc["prerelease"],
            "assets": [{"name": name} for name in assets],
        }
        self.releases.append(record)
        for name, data in assets.items():
            self.assets[(doc["tag"], name)] = data
        self.tags[doc["tag"]] = doc["commit"]
        return record

    def record(self, tag):
        return next(r for r in self.releases if r["tag_name"] == tag)

    def api(self, path, *, method="GET", data=None):
        route = path.split("?", 1)[0]
        page = int(re.search(r"[?&]page=(\d+)", path).group(1)) if "&page=" in path else 1
        if method == "GET" and route.endswith("/releases"):
            return copy.deepcopy(self.releases) if page == 1 else []
        if method == "GET" and "/git/matching-refs/tags/" in route:
            tag = unquote(route.rsplit("/", 1)[1])
            return [{"ref": f"refs/tags/{tag}"}] if tag in self.tags and page == 1 else []
        if method == "POST" and route.endswith("/git/refs"):
            self.tags[data["ref"].removeprefix("refs/tags/")] = data["sha"]
            return {"ref": data["ref"]}
        if method == "GET" and "/commits/" in route:
            tag = unquote(route.rsplit("/", 1)[1])
            return {"sha": self.tags[tag], "commit": {"tree": {"sha": "b" * 40}}}
        if method == "POST" and route.endswith("/releases"):
            record = {
                "id": len(self.releases) + 1,
                "tag_name": data["tag_name"],
                "draft": True,
                "prerelease": data["prerelease"],
                "assets": [],
            }
            self.releases.append(record)
            return copy.deepcopy(record)
        if method == "PATCH" and "/releases/" in route:
            record = next(r for r in self.releases if r["id"] == int(route.rsplit("/", 1)[1]))
            record.update(data)
            return copy.deepcopy(record)
        raise AssertionError(f"未预期的 api 调用：{method} {path}")

    def command(self, *args, input_text=None):
        if args[:3] == ("gh", "release", "download"):
            tag, name = args[3], args[args.index("--pattern") + 1]
            self.downloads.append((tag, name))
            if (tag, name) not in self.assets:
                raise release.ReleaseError("gh 执行失败，退出码 1")
            (Path(args[args.index("--dir") + 1]) / name).write_bytes(self.assets[(tag, name)])
            return ""
        if args[:3] == ("gh", "release", "upload"):
            tag, (source, label) = args[3], args[4].rsplit("#", 1)
            name = Path(source).name
            if name != label or (tag, name) in self.assets:
                raise AssertionError(f"上传附件名与标签不符或重复上传：{args[4]}")
            self.assets[(tag, name)] = Path(source).read_bytes()
            self.record(tag)["assets"].append({"name": name})
            self.uploads.append((tag, name))
            return ""
        raise AssertionError(f"未预期的命令：{args}")


class ReleaseManifestTests(unittest.TestCase):
    def test_local_github_calls_require_explicit_machine_identity(self):
        with (
            patch.dict(os.environ, {"GITHUB_ACTIONS": "", "LINGXI_GH_COMMAND": ""}),
            patch.object(release.subprocess, "run") as run,
        ):
            with self.assertRaises(release.ReleaseError):
                release.command("gh", "api", "repos/example/repo")
            run.assert_not_called()
        with (
            patch.dict(
                os.environ, {"GITHUB_ACTIONS": "", "LINGXI_GH_COMMAND": "/approved/gh-machine"}
            ),
            patch.object(
                release.subprocess, "run", return_value=Mock(returncode=0, stdout="{}")
            ) as run,
        ):
            self.assertEqual(release.command("gh", "api", "repos/example/repo"), "{}")
            self.assertEqual(run.call_args.args[0][0], "/approved/gh-machine")

    def test_complete_candidate_is_valid(self):
        release.validate_manifest(candidate(), "Moshuiwang/lingxi", prerelease=True)

    def test_wrong_version_branch_repo_missing_image_and_mutable_image_rejected(self):
        for key, value in [
            ("version", "2.4.0"),
            ("branch", "main"),
            ("branch", "release/2.4"),
            ("prerelease", False),
            ("commit", "main"),
            ("run_id", 0),
            ("migration_heads", []),
        ]:
            with self.subTest(key=key, value=value):
                doc = dict(candidate(), **{key: value})
                with self.assertRaises(release.ReleaseError):
                    release.validate_manifest(doc, "Moshuiwang/lingxi", prerelease=True)
        for images in [
            {},
            {"scheduler": "latest"},
            dict(candidate()["images"], worker="ghcr.io/other/worker@sha256:" + "a" * 64),
        ]:
            with self.assertRaises(release.ReleaseError):
                release.validate_manifest(
                    dict(candidate(), images=images), "Moshuiwang/lingxi", prerelease=True
                )
        with self.assertRaises(release.ReleaseError):
            release.validate_manifest(candidate(), "other/repository", prerelease=True)

    def test_version_parser_rejects_shell_text_and_noncanonical_versions(self):
        for value in [
            "v2.03.2",
            "2.3.2",
            "v2.3.2-rc.0",
            "v2.3.2;false",
            "../../file",
            "v2.3.2\n",
            "v2.3.2-beta.1",
        ]:
            with self.subTest(value=value), self.assertRaises(release.ReleaseError):
                release.release_version(value)

    def test_receipt_binds_exact_images_and_real_evidence_location(self):
        doc = candidate()
        release.validate_acceptance(receipt(doc), doc)
        for key, value in [
            ("result", "unknown"),
            ("candidate_tag", "v2.3.2-rc.41"),
            ("manifest_sha256", "d" * 64),
            ("evidence_url", "https://example.com/pass"),
            ("recovery", {}),
        ]:
            with self.subTest(key=key), self.assertRaises(release.ReleaseError):
                release.validate_acceptance(dict(receipt(doc), **{key: value}), doc)
        old = receipt(doc)
        old["recovery"]["previous_tag"] = "v2.4.0"
        with self.assertRaises(release.ReleaseError):
            release.validate_acceptance(old, doc)
        changed = copy.deepcopy(doc)
        changed["images"]["worker"] = changed["images"]["worker"].replace("c" * 64, "d" * 64)
        with self.assertRaises(release.ReleaseError):
            release.validate_acceptance(receipt(doc), changed)

    def test_prerelease_cannot_be_selected_for_production(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(release, "load_release", return_value=({}, candidate())),
        ):
            output = Path(tmp) / "images.env"
            with self.assertRaises(release.ReleaseError):
                release.resolve("Moshuiwang/lingxi", candidate()["tag"], "production", output)
            self.assertFalse(output.exists())

    def test_stage_selection_uses_digests(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(release, "load_release", return_value=({}, candidate())),
        ):
            output = Path(tmp) / "images.env"
            release.resolve(
                "Moshuiwang/lingxi", candidate()["tag"], "stage", output, allow_legacy=True
            )
            text = output.read_text()
            self.assertEqual(text.count("@sha256:"), 4)
            self.assertNotIn("GITHUB_TOKEN", text)

    def test_promotion_preserves_every_image_and_does_not_build(self):
        doc = candidate()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                os.environ,
                {
                    "GITHUB_REF": "refs/heads/main",
                    "GITHUB_REF_PROTECTED": "true",
                    "GITHUB_SHA": "d" * 40,
                    "GITHUB_RUN_ID": "43",
                },
            ),
            patch.object(release, "load_release", return_value=({}, doc)),
            patch.object(release, "receipt_for", return_value=receipt(doc)),
            patch.object(release, "write_release") as write,
            patch.object(release, "command") as command,
        ):
            release.promote(
                doc["repository"], doc["tag"], Path(tmp) / "release-manifest.json", True
            )
            promoted = write.call_args.args[0]
            self.assertEqual(promoted["images"], doc["images"])
            self.assertEqual(promoted["commit"], doc["commit"])
            self.assertEqual(promoted["tag"], "v2.3.2")
            self.assertFalse(promoted["prerelease"])
            command.assert_not_called()

    def test_missing_receipt_rejects_promotion_without_writes(self):
        doc = candidate()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                os.environ,
                {
                    "GITHUB_REF": "refs/heads/main",
                    "GITHUB_REF_PROTECTED": "true",
                    "GITHUB_SHA": "d" * 40,
                    "GITHUB_RUN_ID": "43",
                },
            ),
            patch.object(release, "load_release", return_value=({}, doc)),
            patch.object(release, "write_release") as write,
        ):
            with self.assertRaises(release.ReleaseError):
                release.promote(
                    doc["repository"], doc["tag"], Path(tmp) / "release-manifest.json", True
                )
            write.assert_not_called()

    def test_retry_of_existing_release_checks_same_content_without_requiring_own_run_complete(self):
        doc = candidate()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(release, "find_release", return_value={"draft": False}),
            patch.object(release, "load_release", return_value=({}, doc)) as load,
            patch.object(release, "api") as api,
        ):
            release.write_release(doc, Path(tmp) / "release-manifest.json")
            load.assert_called_once_with(doc["repository"], doc["tag"], require_completed=False)
            api.assert_not_called()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(release, "find_release", return_value={"draft": False}),
            patch.object(release, "load_release", return_value=({}, dict(doc, commit="d" * 40))),
            self.assertRaises(release.ReleaseError),
        ):
            release.write_release(doc, Path(tmp) / "release-manifest.json")

    def test_unprotected_or_nonmain_cannot_promote(self):
        for ref, protected in [("refs/heads/main", "false"), ("refs/heads/release/2.3", "true")]:
            with (
                patch.dict(os.environ, {"GITHUB_REF": ref, "GITHUB_REF_PROTECTED": protected}),
                self.assertRaises(release.ReleaseError),
            ):
                release.promote("Moshuiwang/lingxi", "v2.3.2-rc.1", Path("/not-written"), True)

    def test_public_release_requires_exact_markers(self):
        doc = candidate()
        valid = {
            "tag_name": doc["tag"],
            "prerelease": True,
            "draft": False,
            "assets": [{"name": "release-manifest.json"}],
        }
        release.validate_release_record(valid, doc)
        for k, v in [
            ("draft", True),
            ("prerelease", False),
            ("tag_name", "v2.3.2"),
            ("assets", []),
        ]:
            with self.subTest(key=k), self.assertRaises(release.ReleaseError):
                release.validate_release_record(dict(valid, **{k: v}), doc)

    def test_receipt_cannot_validate_other_branch_proof(self):
        pr = {
            "number": 1,
            "head": {"sha": "a" * 40},
            "base": {"ref": "release/2.3"},
            "merged_at": "now",
            "merge_commit_sha": "b" * 40,
        }
        doc = writer.candidate_document(
            repository="Moshuiwang/lingxi",
            pr_number=1,
            head_sha="a" * 40,
            tested_sha="b" * 40,
            tree_sha="c" * 40,
            run_id=1,
            base_ref="release/2.3",
        )
        proof.select_merged_pr([pr], "b" * 40, "release/2.3")
        proof.validate_document(
            doc, repository="Moshuiwang/lingxi", pr=pr, tree_sha="c" * 40, run_id=1
        )
        with self.assertRaises(proof.CandidateError):
            proof.select_merged_pr([pr], "b" * 40, "release/2.4")
        with self.assertRaises(proof.CandidateError):
            proof.validate_document(
                dict(doc, base_ref="main"),
                repository="Moshuiwang/lingxi",
                pr=pr,
                tree_sha="c" * 40,
                run_id=1,
            )

    def test_migrations_are_discovered_from_current_chain(self):
        import re

        documented = (ROOT / "migrations/README.md").read_text()
        heads = release.migration_heads()
        self.assertEqual(len(heads), 1)
        self.assertRegex(heads[0], r"^00[0-9]{2}_[a-z_]+$")
        self.assertIn(heads[0], documented)
        self.assertTrue(
            any(
                re.search(r"revision.*" + re.escape(heads[0]), p.read_text())
                for p in (ROOT / "migrations/alembic/versions").glob("*.py")
            )
        )


class ReleaseAttachmentTests(unittest.TestCase):
    """候选与正式版 Release 都要带三个附件；索引附件与 tar 内嵌索引逐字节相同。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / "release-manifest.json"
        self.fake = FakeGitHub()
        for name in ("api", "command"):
            patcher = patch.object(release, name, side_effect=getattr(self.fake, name))
            patcher.start()
            self.addCleanup(patcher.stop)

    def embedded(self, package):
        with tarfile.open(package, mode="r:") as archive:
            return archive.extractfile(archive.getmember(bundle.INDEX)).read()

    def test_candidate_release_uploads_manifest_bundle_and_embedded_index(self):
        package, metadata, index = control_package(self.root)
        doc = candidate_with_bundle(metadata)
        release.write_release(doc, self.output, package)
        tag = doc["tag"]
        self.assertEqual(
            self.fake.uploads,
            [
                (tag, "release-manifest.json"),
                (tag, "lingxi-control.tar"),
                (tag, "control-index.json"),
            ],
        )
        self.assertEqual(self.fake.assets[(tag, "control-index.json")], self.embedded(package))
        self.assertEqual(self.fake.assets[(tag, "control-index.json")], index)
        self.assertEqual(
            hashlib.sha256(self.fake.assets[(tag, "control-index.json")]).hexdigest(),
            metadata["index_sha256"],
        )
        self.assertEqual(self.fake.assets[(tag, "lingxi-control.tar")], package.read_bytes())
        self.assertFalse(self.fake.record(tag)["draft"])

    def test_resumed_draft_with_same_three_assets_uploads_nothing(self):
        package, metadata, index = control_package(self.root)
        doc = candidate_with_bundle(metadata)
        self.fake.seed(
            doc,
            {
                "release-manifest.json": release.canonical(doc),
                "lingxi-control.tar": package.read_bytes(),
                "control-index.json": index,
            },
            draft=True,
        )
        release.write_release(doc, self.output, package)
        self.assertEqual(self.fake.uploads, [])
        self.assertIn((doc["tag"], "control-index.json"), self.fake.downloads)
        self.assertFalse(self.fake.record(doc["tag"])["draft"])

    def test_resumed_draft_with_different_index_is_refused_and_stays_draft(self):
        package, metadata, index = control_package(self.root)
        doc = candidate_with_bundle(metadata)
        self.fake.seed(
            doc,
            {
                "release-manifest.json": release.canonical(doc),
                "lingxi-control.tar": package.read_bytes(),
                "control-index.json": index + b"\n",
            },
            draft=True,
        )
        with self.assertRaisesRegex(release.ReleaseError, "已有索引附件不同"):
            release.write_release(doc, self.output, package)
        self.assertEqual(self.fake.uploads, [])
        self.assertEqual(self.fake.assets[(doc["tag"], "control-index.json")], index + b"\n")
        self.assertTrue(self.fake.record(doc["tag"])["draft"])

    def test_embedded_index_mismatching_manifest_is_refused_before_any_upload(self):
        package, metadata, _ = control_package(self.root, index_tail=b"\n")
        doc = candidate_with_bundle(metadata)
        with self.assertRaisesRegex(release.ReleaseError, "内嵌索引与清单不符"):
            release.write_release(doc, self.output, package)
        self.assertEqual(self.fake.uploads, [])
        self.assertEqual(self.fake.releases, [])
        self.assertFalse(self.output.exists())

    def test_promotion_uploads_embedded_index_without_downloading_candidate_index(self):
        package, metadata, index = control_package(self.root)
        doc = candidate_with_bundle(metadata)
        self.fake.seed(
            doc,
            {
                "release-manifest.json": release.canonical(doc),
                "lingxi-control.tar": package.read_bytes(),
            },
            draft=False,
        )
        with (
            patch.dict(
                os.environ,
                {
                    "GITHUB_REF": "refs/heads/main",
                    "GITHUB_REF_PROTECTED": "true",
                    "GITHUB_SHA": "d" * 40,
                    "GITHUB_RUN_ID": "43",
                },
            ),
            patch.object(release, "load_release", return_value=({}, doc)),
            patch.object(release, "receipt_for", return_value=receipt(doc)),
        ):
            release.promote(doc["repository"], doc["tag"], self.output, True)
        self.assertEqual(
            self.fake.uploads,
            [
                ("v2.4.0", "release-manifest.json"),
                ("v2.4.0", "lingxi-control.tar"),
                ("v2.4.0", "control-index.json"),
            ],
        )
        self.assertEqual(self.fake.assets[("v2.4.0", "control-index.json")], index)
        self.assertEqual(self.fake.assets[("v2.4.0", "lingxi-control.tar")], package.read_bytes())
        self.assertEqual(self.fake.downloads, [(doc["tag"], "lingxi-control.tar")])
        self.assertFalse(self.fake.record("v2.4.0")["draft"])
        self.assertFalse(self.fake.record("v2.4.0")["prerelease"])


class ControlBundleVerificationTests(unittest.TestCase):
    """resolve 在生产版本目录里以 root 核对控制包：源码旁不得留下 __pycache__。"""

    def test_verify_control_bundle_leaves_no_bytecode_cache_beside_sources(self):
        # 仓库 deploy/ 旁可能早有缓存，改把两份源码按同样相对布局复制到临时目录后按路径加载。
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "scripts/ci").mkdir(parents=True)
            (root / "deploy").mkdir()
            shutil.copyfile(
                ROOT / "scripts/ci/release_manifest.py", root / "scripts/ci/release_manifest.py"
            )
            shutil.copyfile(ROOT / "deploy/control_bundle.py", root / "deploy/control_bundle.py")
            package, metadata, _ = control_package(root)
            # 先把开关拨回假（模拟不带 -B 启动的解释器），核对函数自己必须把它关上。
            with (
                patch.object(sys, "dont_write_bytecode", False),
                patch.object(sys, "pycache_prefix", None),
            ):
                spec = importlib.util.spec_from_file_location(
                    "release_manifest_bytecode_probe", root / "scripts/ci/release_manifest.py"
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                # 探针有效性：开关为假时加载副本本身会留下缓存，说明不是解释器替我们挡住的。
                self.assertTrue((root / "scripts/ci/__pycache__").is_dir())
                module.verify_control_bundle(package, metadata)
                self.assertTrue(sys.dont_write_bytecode)
                with self.assertRaisesRegex(module.ReleaseError, "控制包内容与固定清单不符"):
                    module.verify_control_bundle(package, dict(metadata, sha256="0" * 64))
            self.assertFalse((root / "deploy/__pycache__").exists())
            self.assertEqual(list((root / "deploy").iterdir()), [root / "deploy/control_bundle.py"])


class ReleaseWorkflowTests(unittest.TestCase):
    def test_real_workflows(self):
        self.assertEqual(guard.check(), [])

    def test_each_missing_protection_turns_check_red(self):
        mutations = [
            (".github/workflows/publish.yml", "      - 'release/**'", "      - main"),
            (".github/workflows/ci.yml", '--base-ref "${{ github.base_ref }}"', ""),
            (".github/workflows/release.yml", "needs: [candidate]", "needs: []"),
            (
                ".github/workflows/publish.yml",
                "deploy/control_bundle.py --root",
                "missing_packager",
            ),
            (".github/workflows/publish.yml", "--control-bundle", "--missing-bundle"),
            (".github/workflows/release.yml", "github.ref_protected", "true"),
            (".github/CODEOWNERS", "/deploy/releases/acceptance/ @Moshuiwang", ""),
        ]
        paths = [
            ".github/workflows/publish.yml",
            ".github/workflows/ci.yml",
            ".github/workflows/release.yml",
            ".github/CODEOWNERS",
            "deploy/生产部署runbook.md",
        ]
        for name, old, new in mutations:
            with self.subTest(name=name, old=old), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for path in paths:
                    dest = root / path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text((ROOT / path).read_text())
                file = root / name
                self.assertIn(old, file.read_text())
                file.write_text(file.read_text().replace(old, new))
                self.assertTrue(guard.check(root))


if __name__ == "__main__":
    unittest.main()
