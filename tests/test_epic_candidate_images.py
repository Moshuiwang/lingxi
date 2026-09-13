"""`write_epic_candidate_images.py` 与 `verify_epic_candidate_bundle.py` 的用例（Issue #150）。

这两个脚本合起来是"PR 候选四镜像制品链"里唯一需要新证明正确性的部分：构建、契约核对、
双构建等价都是既有门禁（`verify_image_contract.sh` / `image_manifest.py`）已经覆盖的。
本文件不重复覆盖 docker 本身的行为——`save_image` / `read_image_digest` /
`import_and_check_digest` 都通过注入的 fake runner 测试，不依赖本机是否装了 docker。

`check_bundle_files` 一组是任务卡要求的"变异会失败、恢复正确对象后通过"的可复现固化：
构造一份自洽的候选包，改坏一个 tar 的字节，断言校验红；换回原字节，断言校验绿。

`import_and_check_digest` 一组（Issue #765）的夹具是用 `tarfile` 现场构造的**真实 tar**：
里面放 `docker save` 格式的 `manifest.json` 与它 `Config` 指向的 config blob，
摘要由 blob 内容算出。docker 侧仍走 fake runner——比对对象已经不是回读值，fake runner
只回答「load 成不成功、引用读不读得到」，存储驱动是什么对结论没有影响，用例专门钉住这点。
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


WRITER = load(
    ROOT / "scripts/ci/write_epic_candidate_images.py", "epic_candidate_images_writer_under_test"
)
VERIFIER = load(
    ROOT / "scripts/ci/verify_epic_candidate_bundle.py", "epic_candidate_bundle_verifier_under_test"
)

HEAD = "a" * 40
TESTED = "b" * 40
TREE = "c" * 40
DIGEST = "sha256:" + "1" * 64
OTHER_DIGEST = "sha256:" + "2" * 64
TAR_SHA = "3" * 64


def _image(
    service: str, *, digest: str = DIGEST, tar_sha256: str = TAR_SHA, size: int = 42
) -> dict:
    return {
        "service": service,
        "reference": f"lingxi-{service}:build-a",
        "tar": f"lingxi-{service}.tar",
        "tar_sha256": tar_sha256,
        "tar_size_bytes": size,
        "image_digest": digest,
    }


def _images(**overrides) -> list[dict]:
    images = [_image(service) for service in ("scheduler", "migrate", "gateway", "worker")]
    for service, patch in overrides.items():
        for image in images:
            if image["service"] == service:
                image.update(patch)
    return images


def _write_image_tar(
    path: Path,
    *,
    config: bytes = b'{"architecture":"amd64","os":"linux"}',
    layout: str = "oci",
    manifest: object = None,
    blob_name: str | None = None,
    blob_content: bytes | None = None,
) -> str:
    """现场构造一份 `docker save` 形状的 tar，返回其 config 摘要 ``sha256:<64 位>``。

    默认是自洽的：`manifest.json` 的 `Config` 指向 `blobs/sha256/<摘要>`（``layout="oci"``，
    Docker 25 起的形状）或 `<摘要>.json`（``layout="legacy"``），blob 内容就是 ``config``。
    要造坏样本时：``manifest`` 传入自定义对象（``None`` 之外的值原样写进 manifest.json，
    传 ``False`` 则不写 manifest.json）；``blob_name`` / ``blob_content`` 改写 blob 的
    名字或内容（``blob_name=""`` 表示不写 blob）。
    """

    digest_hex = hashlib.sha256(config).hexdigest()
    config_reference = f"blobs/sha256/{digest_hex}" if layout == "oci" else f"{digest_hex}.json"
    if manifest is None:
        manifest = [
            {
                "Config": config_reference,
                "RepoTags": ["lingxi-worker:build-a"],
                "Layers": [],
            }
        ]
    with tarfile.open(path, "w") as tar:

        def add(name: str, payload: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))

        if manifest is not False:
            add("manifest.json", json.dumps(manifest).encode("utf-8"))
        name = config_reference if blob_name is None else blob_name
        if name:
            add(name, config if blob_content is None else blob_content)
    return f"sha256:{digest_hex}"


class RequiredServicesConsistencyTest(unittest.TestCase):
    def test_writer_and_verifier_agree_on_the_four_services(self) -> None:
        self.assertEqual(WRITER.REQUIRED_SERVICES, VERIFIER.REQUIRED_SERVICES)
        self.assertEqual(WRITER.REQUIRED_SERVICES, ("gateway", "migrate", "scheduler", "worker"))


class ParseImageArgumentTest(unittest.TestCase):
    def test_valid_pair_is_split(self) -> None:
        self.assertEqual(
            WRITER.parse_image_argument("scheduler=lingxi-scheduler:build-a"),
            ("scheduler", "lingxi-scheduler:build-a"),
        )

    def test_missing_equals_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WRITER.parse_image_argument("scheduler")

    def test_empty_service_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WRITER.parse_image_argument("=lingxi-scheduler:build-a")

    def test_empty_reference_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WRITER.parse_image_argument("scheduler=")


class ManifestDocumentValidationTest(unittest.TestCase):
    def _document(self, **overrides):
        kwargs = dict(
            repository="Moshuiwang/lingxi",
            pr_number=150,
            head_sha=HEAD,
            tested_sha=TESTED,
            tree_sha=TREE,
            run_id=999,
            batch="20260813",
            generated_at="2026-08-13T00:00:00Z",
            images=_images(),
        )
        kwargs.update(overrides)
        return WRITER.manifest_document(**kwargs)

    def test_wellformed_document_passes(self) -> None:
        document = self._document()
        self.assertEqual(document["schema"], 1)
        self.assertEqual(len(document["images"]), 4)
        # 输出按 service 排序，不依赖调用方传入的顺序。
        self.assertEqual(
            [item["service"] for item in document["images"]],
            ["gateway", "migrate", "scheduler", "worker"],
        )

    def test_missing_service_is_rejected(self) -> None:
        images = [image for image in _images() if image["service"] != "worker"]
        with self.assertRaises(ValueError):
            self._document(images=images)

    def test_duplicate_service_is_rejected(self) -> None:
        images = _images() + [_image("worker")]
        with self.assertRaises(ValueError):
            self._document(images=images)

    def test_unknown_service_is_rejected(self) -> None:
        images = [image for image in _images() if image["service"] != "worker"] + [_image("admin")]
        with self.assertRaises(ValueError):
            self._document(images=images)

    def test_bad_head_sha_shape_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._document(head_sha="not-a-sha")

    def test_bad_batch_shape_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._document(batch="2026-08-13")

    def test_bad_repository_shape_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._document(repository="lingxi")

    def test_bad_image_digest_shape_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._document(images=_images(worker={"image_digest": "not-a-digest"}))

    def test_bad_tar_sha256_shape_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._document(images=_images(worker={"tar_sha256": "zz"}))

    def test_non_positive_tar_size_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._document(images=_images(worker={"tar_size_bytes": 0}))


class DockerCallSitesTest(unittest.TestCase):
    """`save_image` / `read_image_digest` 只测调用与解析逻辑，docker 本身用 fake runner 顶替。"""

    def _runner(self, returncode: int, stdout: str = "", stderr: str = ""):
        return lambda argv: WRITER.CommandResult(returncode, stdout, stderr)

    def test_save_image_failure_is_not_swallowed(self) -> None:
        with self.assertRaises(RuntimeError):
            WRITER.save_image(
                "x:y",
                Path("/tmp/does-not-matter.tar"),
                runner=self._runner(1, stderr="no such image"),
            )

    def test_read_image_digest_returns_id(self) -> None:
        digest = WRITER.read_image_digest("x:y", runner=self._runner(0, stdout=f"{DIGEST}\n"))
        self.assertEqual(digest, DIGEST)

    def test_read_image_digest_rejects_malformed_id(self) -> None:
        with self.assertRaises(RuntimeError):
            WRITER.read_image_digest("x:y", runner=self._runner(0, stdout="not-a-digest\n"))

    def test_read_image_digest_failure_is_not_swallowed(self) -> None:
        with self.assertRaises(RuntimeError):
            WRITER.read_image_digest("x:y", runner=self._runner(1, stderr="no such image"))


class Sha256FileTest(unittest.TestCase):
    def test_matches_hashlib_computed_independently(self) -> None:
        import hashlib

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.bin"
            payload = b"lingxi" * 100000  # 大于分块读取的 1MiB 缓冲，顺带覆盖多块路径
            path.write_bytes(payload)
            self.assertEqual(WRITER.sha256_file(path), hashlib.sha256(payload).hexdigest())
            self.assertEqual(VERIFIER.sha256_file(path), hashlib.sha256(payload).hexdigest())


class ManifestShapeCheckTest(unittest.TestCase):
    def _document(self, **overrides) -> dict:
        document = {
            "schema": 1,
            "repository": "Moshuiwang/lingxi",
            "pr_number": 150,
            "head_sha": HEAD,
            "tested_sha": TESTED,
            "tree_sha": TREE,
            "run_id": 999,
            "batch": "20260813",
            "generated_at": "2026-08-13T00:00:00Z",
            "images": _images(),
        }
        document.update(overrides)
        return document

    def test_wellformed_document_has_no_failures(self) -> None:
        self.assertEqual(VERIFIER.check_manifest_shape(self._document()), [])

    def test_wrong_schema_is_caught(self) -> None:
        failures = VERIFIER.check_manifest_shape(self._document(schema=2))
        self.assertTrue(any("schema" in f for f in failures), failures)

    def test_missing_image_is_caught(self) -> None:
        images = [image for image in _images() if image["service"] != "gateway"]
        failures = VERIFIER.check_manifest_shape(self._document(images=images))
        self.assertTrue(any("gateway" in f or "服务集合" in f for f in failures), failures)

    def test_extra_image_count_is_caught(self) -> None:
        failures = VERIFIER.check_manifest_shape(
            self._document(images=_images() + [_image("worker")])
        )
        self.assertTrue(any("4" in f for f in failures), failures)

    def test_bad_pr_number_type_is_caught(self) -> None:
        failures = VERIFIER.check_manifest_shape(self._document(pr_number="150"))
        self.assertTrue(any("pr_number" in f for f in failures), failures)

    def test_missing_field_in_image_entry_is_caught(self) -> None:
        images = _images()
        del images[0]["tar_sha256"]
        failures = VERIFIER.check_manifest_shape(self._document(images=images))
        self.assertTrue(any("tar_sha256" in f for f in failures), failures)


class ExpectationCheckTest(unittest.TestCase):
    def test_matching_expectations_pass(self) -> None:
        document = {
            "repository": "a/b",
            "pr_number": 1,
            "head_sha": HEAD,
            "tree_sha": TREE,
            "run_id": 2,
        }
        failures = VERIFIER.check_expectations(
            document,
            expect_repository="a/b",
            expect_pr_number=1,
            expect_head_sha=HEAD,
            expect_tree_sha=TREE,
            expect_run_id=2,
        )
        self.assertEqual(failures, [])

    def test_mismatched_head_sha_is_caught(self) -> None:
        document = {"head_sha": HEAD}
        failures = VERIFIER.check_expectations(
            document,
            expect_repository=None,
            expect_pr_number=None,
            expect_head_sha="f" * 40,
            expect_tree_sha=None,
            expect_run_id=None,
        )
        self.assertTrue(any("head_sha" in f for f in failures), failures)

    def test_unset_expectations_are_not_checked(self) -> None:
        document = {"head_sha": "anything"}
        failures = VERIFIER.check_expectations(
            document,
            expect_repository=None,
            expect_pr_number=None,
            expect_head_sha=None,
            expect_tree_sha=None,
            expect_run_id=None,
        )
        self.assertEqual(failures, [])


class BundleIntegrityDemoTest(unittest.TestCase):
    """任务卡要求的固化演示：篡改会红，恢复原对象会绿。"""

    def _write_bundle(self, directory: Path, *, worker_bytes: bytes) -> dict:
        payloads = {
            "scheduler": b"scheduler-payload",
            "migrate": b"migrate-payload",
            "gateway": b"gateway-payload",
            "worker": worker_bytes,
        }
        images = []
        for service, payload in payloads.items():
            tar_name = f"lingxi-{service}.tar"
            (directory / tar_name).write_bytes(payload)
            images.append(
                {
                    "service": service,
                    "reference": f"lingxi-{service}:build-a",
                    "tar": tar_name,
                    "tar_sha256": WRITER.sha256_file(directory / tar_name),
                    "tar_size_bytes": len(payload),
                    "image_digest": DIGEST,
                }
            )
        document = {
            "schema": 1,
            "repository": "Moshuiwang/lingxi",
            "pr_number": 150,
            "head_sha": HEAD,
            "tested_sha": TESTED,
            "tree_sha": TREE,
            "run_id": 999,
            "batch": "20260813",
            "generated_at": "2026-08-13T00:00:00Z",
            "images": images,
        }
        (directory / "manifest.json").write_text(json.dumps(document), encoding="utf-8")
        return document

    def test_tampering_fails_and_restoring_the_original_passes(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            original_bytes = b"worker-payload-original"
            document = self._write_bundle(directory, worker_bytes=original_bytes)

            # 1. 干净状态：完整性校验通过。
            self.assertEqual(VERIFIER.check_bundle_files(document, directory), [])

            # 2. 篡改 worker 的 tar（模拟下载损坏或被替换）：必须红，且点名 worker。
            (directory / "lingxi-worker.tar").write_bytes(original_bytes + b"-TAMPERED")
            failures = VERIFIER.check_bundle_files(document, directory)
            self.assertTrue(failures, "篡改后应当有失败项")
            self.assertTrue(any("worker" in f and "sha256" in f for f in failures), failures)

            # 3. 恢复原对象：必须重新变绿。
            (directory / "lingxi-worker.tar").write_bytes(original_bytes)
            self.assertEqual(VERIFIER.check_bundle_files(document, directory), [])

    def test_missing_tar_is_reported_as_incomplete_download(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            document = self._write_bundle(directory, worker_bytes=b"worker-payload")
            (directory / "lingxi-migrate.tar").unlink()
            failures = VERIFIER.check_bundle_files(document, directory)
            self.assertTrue(any("migrate" in f and "缺失" in f for f in failures), failures)


class LoadManifestTest(unittest.TestCase):
    def test_missing_manifest_raises_bundle_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(VERIFIER.BundleError):
                VERIFIER.load_manifest(Path(directory))

    def test_invalid_json_raises_bundle_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            (directory / "manifest.json").write_text("{not json", encoding="utf-8")
            with self.assertRaises(VERIFIER.BundleError):
                VERIFIER.load_manifest(directory)

    def test_non_object_json_raises_bundle_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            (directory / "manifest.json").write_text("[1, 2, 3]", encoding="utf-8")
            with self.assertRaises(VERIFIER.BundleError):
                VERIFIER.load_manifest(directory)


class ImportAndDigestCheckTest(unittest.TestCase):
    """`--import` 路径（Issue #765）：摘要从真实 tar 内读，docker 侧走 fake runner。

    结论不再依赖存储驱动：fake runner 仍会回答 `docker info`，但被测函数不该再问它；
    摘要对不上或读不到的 tar 不导入（不该有 `docker load` 调用）。
    """

    def _document(self, digest: str = DIGEST) -> dict:
        return {"images": [_image("worker", digest=digest)]}

    def _fake_runner(
        self,
        *,
        load_returncode=0,
        inspect_stdout=DIGEST,
        inspect_returncode=0,
        driver="overlay2",
        info_returncode=0,
        calls: list | None = None,
    ):
        def runner(argv):
            if calls is not None:
                calls.append(argv)
            if argv[:2] == ["docker", "info"]:
                return VERIFIER.CommandResult(
                    info_returncode,
                    driver if info_returncode == 0 else "",
                    "" if info_returncode == 0 else "info failed",
                )
            if argv[:2] == ["docker", "load"]:
                return VERIFIER.CommandResult(
                    load_returncode, "", "" if load_returncode == 0 else "load failed"
                )
            if argv[:2] == ["docker", "inspect"]:
                return VERIFIER.CommandResult(
                    inspect_returncode,
                    inspect_stdout,
                    "" if inspect_returncode == 0 else "inspect failed",
                )
            raise AssertionError(f"unexpected command: {argv}")

        return runner

    @staticmethod
    def _loads(calls: list) -> list[str]:
        """fake runner 收到的 `docker load` 调用里的 tar 路径。"""

        return [argv[3] for argv in calls if argv[:2] == ["docker", "load"]]

    # ---- ① 一致 ----------------------------------------------------------

    def test_matching_digest_passes(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            digest = _write_image_tar(directory / "lingxi-worker.tar")
            failures = VERIFIER.import_and_check_digest(
                self._document(digest), directory, runner=self._fake_runner()
            )
            self.assertEqual(failures, [])

    def test_legacy_layout_config_json_is_recognized(self) -> None:
        """Docker 25 之前 `Config` 是 `<摘要>.json`，同样认。"""

        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            digest = _write_image_tar(directory / "lingxi-worker.tar", layout="legacy")
            failures = VERIFIER.import_and_check_digest(
                self._document(digest), directory, runner=self._fake_runner()
            )
            self.assertEqual(failures, [])

    def test_matching_digest_passes_on_any_driver(self) -> None:
        """摘要对得上时，任何驱动下都是「一致」，不因驱动判红。"""

        with tempfile.TemporaryDirectory() as workdir:
            directory = Path(workdir)
            digest = _write_image_tar(directory / "lingxi-worker.tar")
            for driver in ("overlay2", "containerd-snapshotter", "btrfs"):
                failures = VERIFIER.import_and_check_digest(
                    self._document(digest), directory, runner=self._fake_runner(driver=driver)
                )
                self.assertEqual(failures, [], driver)

    def test_import_never_consults_the_storage_driver(self) -> None:
        """结论不依赖存储驱动的钉住：被测函数一旦问 `docker info` 就判红。"""

        def runner(argv):
            if argv[:2] == ["docker", "info"]:
                raise AssertionError("结论不该再依赖存储驱动，却调用了 docker info")
            return VERIFIER.CommandResult(0, DIGEST, "")

        with tempfile.TemporaryDirectory() as workdir:
            directory = Path(workdir)
            digest = _write_image_tar(directory / "lingxi-worker.tar")
            self.assertEqual(
                VERIFIER.import_and_check_digest(self._document(digest), directory, runner=runner),
                [],
            )
            _write_image_tar(directory / "lingxi-worker.tar", config=b'{"other":1}')
            failures = VERIFIER.import_and_check_digest(
                self._document(digest), directory, runner=runner
            )
            self.assertEqual(len(failures), 1, failures)
            self.assertIn("与清单记录不一致", failures[0])

    # ---- ② 摘要被改错 → 不一致（不是「无法核验」），两种驱动下都红 ----------

    def test_mismatched_digest_after_import_is_caught(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            _write_image_tar(directory / "lingxi-worker.tar")
            failures = VERIFIER.import_and_check_digest(
                self._document(OTHER_DIGEST), directory, runner=self._fake_runner()
            )
            self.assertTrue(any("worker" in f and "不一致" in f for f in failures), failures)

    def test_mismatch_on_overlay2_is_reported_as_inconsistent(self) -> None:
        """overlay2 下摘要对不上，就是「不一致」——这条结论逐字不变。"""

        with tempfile.TemporaryDirectory() as workdir:
            directory = Path(workdir)
            _write_image_tar(directory / "lingxi-worker.tar")
            failures = VERIFIER.import_and_check_digest(
                self._document("sha256:" + "b" * 64), directory, runner=self._fake_runner()
            )
        self.assertEqual(len(failures), 1, failures)
        self.assertIn("与候选身份不一致", failures[0])
        self.assertNotIn("本机无法核验", failures[0])

    def test_wrong_digest_is_inconsistent_under_both_drivers_and_not_loaded(self) -> None:
        """Issue #765 否定断言：config 摘要被改错的 tar，在 overlay2 与 containerd 两种
        驱动回读下**都**判「与清单记录不一致」，不再出现「本机无法核验」；对不上的
        tar 也不会被 `docker load` 进本机。"""

        for driver in ("overlay2", "containerd-snapshotter"):
            with tempfile.TemporaryDirectory() as workdir:
                directory = Path(workdir)
                calls: list = []
                _write_image_tar(directory / "lingxi-worker.tar", config=b'{"tampered":true}')
                failures = VERIFIER.import_and_check_digest(
                    self._document(DIGEST),
                    directory,
                    runner=self._fake_runner(driver=driver, calls=calls),
                )
            self.assertEqual(len(failures), 1, (driver, failures))
            self.assertIn("worker", failures[0])
            self.assertIn("与清单记录不一致", failures[0])
            self.assertNotIn("本机无法核验", failures[0])
            self.assertNotIn("读不到", failures[0])
            self.assertEqual(self._loads(calls), [], driver)

    def test_mismatch_on_containerd_is_inconsistent_not_unverifiable(self) -> None:
        """#707 的第三态由 #765 收回：containerd 快照器下同样给出明确结论。"""

        with tempfile.TemporaryDirectory() as workdir:
            directory = Path(workdir)
            _write_image_tar(directory / "lingxi-worker.tar")
            failures = VERIFIER.import_and_check_digest(
                self._document("sha256:" + "b" * 64),
                directory,
                runner=self._fake_runner(
                    inspect_stdout="sha256:" + "b" * 64, driver="containerd-snapshotter"
                ),
            )
        self.assertEqual(len(failures), 1, failures)
        self.assertIn("与清单记录不一致", failures[0])
        self.assertNotIn("本机无法核验", failures[0])

    def test_mismatch_on_btrfs_still_fails_closed(self) -> None:
        """摘要对不上在任何驱动下都判红，不放行。"""

        with tempfile.TemporaryDirectory() as workdir:
            directory = Path(workdir)
            _write_image_tar(directory / "lingxi-worker.tar")
            failures = VERIFIER.import_and_check_digest(
                self._document("sha256:" + "c" * 64),
                directory,
                runner=self._fake_runner(inspect_stdout="sha256:" + "c" * 64, driver="btrfs"),
            )
        self.assertTrue(failures, "摘要对不上时必须判红，不能放行")
        self.assertIn("与清单记录不一致", failures[0])

    def test_driver_unreadable_does_not_change_the_conclusion(self) -> None:
        """连驱动都读不到也无所谓：摘要来自 tar，结论照样明确。"""

        with tempfile.TemporaryDirectory() as workdir:
            directory = Path(workdir)
            digest = _write_image_tar(directory / "lingxi-worker.tar")
            runner = self._fake_runner(inspect_stdout="sha256:" + "d" * 64, info_returncode=1)
            self.assertEqual(
                VERIFIER.import_and_check_digest(self._document(digest), directory, runner=runner),
                [],
            )
            failures = VERIFIER.import_and_check_digest(
                self._document("sha256:" + "d" * 64), directory, runner=runner
            )
        self.assertEqual(len(failures), 1, failures)
        self.assertIn("与清单记录不一致", failures[0])
        self.assertNotIn("本机无法核验", failures[0])

    # ---- ③ tar 内读不到摘要 → 判红，信息是「读不到」而不是「不一致」 ------------

    def _assert_unreadable(self, failures: list[str], calls: list, *hints: str) -> None:
        self.assertEqual(len(failures), 1, failures)
        self.assertIn("worker", failures[0])
        self.assertIn("读不到 config 摘要", failures[0])
        self.assertNotIn("不一致", failures[0])
        self.assertNotIn("本机无法核验", failures[0])
        for hint in hints:
            self.assertIn(hint, failures[0])
        self.assertEqual(self._loads(calls), [], "读不到的 tar 不该导入")

    def _unreadable_case(self, **tar_kwargs) -> tuple[list[str], list]:
        with tempfile.TemporaryDirectory() as workdir:
            directory = Path(workdir)
            calls: list = []
            digest = _write_image_tar(directory / "lingxi-worker.tar", **tar_kwargs)
            failures = VERIFIER.import_and_check_digest(
                self._document(digest), directory, runner=self._fake_runner(calls=calls)
            )
        return failures, calls

    def test_tar_without_manifest_json_is_unreadable(self) -> None:
        failures, calls = self._unreadable_case(manifest=False)
        self._assert_unreadable(failures, calls, "没有 manifest.json")

    def test_tar_manifest_without_config_field_is_unreadable(self) -> None:
        failures, calls = self._unreadable_case(manifest=[{"RepoTags": ["x:y"], "Layers": []}])
        self._assert_unreadable(failures, calls, "Config 字段缺失")

    def test_tar_manifest_with_unknown_config_shape_is_unreadable(self) -> None:
        failures, calls = self._unreadable_case(
            manifest=[{"Config": "sha256:" + "1" * 64, "RepoTags": [], "Layers": []}]
        )
        self._assert_unreadable(failures, calls, "形状不认识")

    def test_tar_manifest_with_two_entries_is_unreadable(self) -> None:
        entry = {"Config": "blobs/sha256/" + "1" * 64, "RepoTags": [], "Layers": []}
        failures, calls = self._unreadable_case(manifest=[entry, entry])
        self._assert_unreadable(failures, calls, "恰好是一个镜像条目")

    def test_tar_manifest_not_a_list_is_unreadable(self) -> None:
        failures, calls = self._unreadable_case(manifest={"Config": "blobs/sha256/" + "1" * 64})
        self._assert_unreadable(failures, calls, "恰好是一个镜像条目")

    def test_tar_manifest_not_json_is_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as workdir:
            directory = Path(workdir)
            calls: list = []
            with tarfile.open(directory / "lingxi-worker.tar", "w") as tar:
                info = tarfile.TarInfo("manifest.json")
                info.size = len(b"{not json")
                tar.addfile(info, io.BytesIO(b"{not json"))
            failures = VERIFIER.import_and_check_digest(
                self._document(), directory, runner=self._fake_runner(calls=calls)
            )
        self._assert_unreadable(failures, calls, "不是合法 JSON")

    def test_tar_missing_config_blob_is_unreadable(self) -> None:
        failures, calls = self._unreadable_case(blob_name="")
        self._assert_unreadable(failures, calls, "没有 Config 指向的 blob")

    def test_tar_config_blob_content_mismatch_is_unreadable(self) -> None:
        """文件名说的摘要与 blob 内容算出的不同：tar 内部不自洽，不能拿文件名当摘要。"""

        failures, calls = self._unreadable_case(blob_content=b'{"forged":1}')
        self._assert_unreadable(failures, calls, "tar 内部不自洽")

    def test_not_a_tar_file_is_unreadable(self) -> None:
        """随便一串字节不是 tar：以前的用例夹具就是这样，现在必须判红而不是放过。"""

        with tempfile.TemporaryDirectory() as workdir:
            directory = Path(workdir)
            calls: list = []
            (directory / "lingxi-worker.tar").write_bytes(b"anything")
            failures = VERIFIER.import_and_check_digest(
                self._document(), directory, runner=self._fake_runner(calls=calls)
            )
        self._assert_unreadable(failures, calls, "不是可读的 tar")
        self.assertEqual(len(failures[0].splitlines()), 1, "报错应当只有一行")

    # ---- ④ 既有失败分支逐条仍在（否定断言） -----------------------------------

    def test_missing_tar_before_import_is_caught(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            failures = VERIFIER.import_and_check_digest(
                self._document(), directory, runner=self._fake_runner()
            )
            self.assertTrue(any("缺失" in f for f in failures), failures)

    def test_missing_image_digest_is_caught_before_import(self) -> None:
        """清单条目没有 image_digest：不能拿 None 去比，也不能导入。"""

        with tempfile.TemporaryDirectory() as workdir:
            directory = Path(workdir)
            calls: list = []
            _write_image_tar(directory / "lingxi-worker.tar")
            document = {"images": [_image("worker")]}
            del document["images"][0]["image_digest"]
            failures = VERIFIER.import_and_check_digest(
                document, directory, runner=self._fake_runner(calls=calls)
            )
        self.assertEqual(len(failures), 1, failures)
        self.assertIn("image_digest", failures[0])
        self.assertEqual(self._loads(calls), [])

    def test_malformed_image_digest_is_caught_before_import(self) -> None:
        with tempfile.TemporaryDirectory() as workdir:
            directory = Path(workdir)
            _write_image_tar(directory / "lingxi-worker.tar")
            failures = VERIFIER.import_and_check_digest(
                self._document("sha256:short"), directory, runner=self._fake_runner()
            )
        self.assertEqual(len(failures), 1, failures)
        self.assertIn("image_digest", failures[0])
        shape_failures = VERIFIER.check_manifest_shape(
            {"schema": 1, "images": _images(worker={"image_digest": "sha256:short"})}
        )
        self.assertTrue(
            any("image_digest 形状非法" in f for f in shape_failures),
            "check_manifest_shape 也必须拒绝形状非法的 image_digest",
        )

    def test_docker_load_failure_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            digest = _write_image_tar(directory / "lingxi-worker.tar")
            failures = VERIFIER.import_and_check_digest(
                self._document(digest), directory, runner=self._fake_runner(load_returncode=1)
            )
            self.assertTrue(any("导入失败" in f for f in failures), failures)

    def test_reference_unreadable_after_load_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            digest = _write_image_tar(directory / "lingxi-worker.tar")
            failures = VERIFIER.import_and_check_digest(
                self._document(digest), directory, runner=self._fake_runner(inspect_returncode=1)
            )
            self.assertTrue(any("读不到引用" in f for f in failures), failures)

    # ---- ⑤ 多镜像逐条核对，一条错整体红 ----------------------------------------

    def test_four_images_are_checked_one_by_one_and_one_bad_entry_fails_the_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as workdir:
            directory = Path(workdir)
            calls: list = []
            images = []
            for service in ("scheduler", "migrate", "gateway", "worker"):
                config = json.dumps({"service": service}).encode("utf-8")
                digest = _write_image_tar(directory / f"lingxi-{service}.tar", config=config)
                images.append(_image(service, digest=digest))
            document = {"images": images}
            self.assertEqual(
                VERIFIER.import_and_check_digest(
                    document, directory, runner=self._fake_runner(calls=calls)
                ),
                [],
            )
            self.assertEqual(len(self._loads(calls)), 4)

            # 只改 gateway 的记录：整体红，且只点名 gateway；另外三个照常导入。
            calls.clear()
            document["images"][2]["image_digest"] = OTHER_DIGEST
            failures = VERIFIER.import_and_check_digest(
                document, directory, runner=self._fake_runner(calls=calls)
            )
        self.assertEqual(len(failures), 1, failures)
        self.assertIn("gateway", failures[0])
        self.assertIn("与清单记录不一致", failures[0])
        loaded = self._loads(calls)
        self.assertEqual(len(loaded), 3)
        self.assertFalse(any("gateway" in path for path in loaded), loaded)


if __name__ == "__main__":
    unittest.main()
