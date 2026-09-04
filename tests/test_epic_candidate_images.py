"""`write_epic_candidate_images.py` 与 `verify_epic_candidate_bundle.py` 的用例（Issue #150）。

这两个脚本合起来是"PR 候选四镜像制品链"里唯一需要新证明正确性的部分：构建、契约核对、
双构建等价都是既有门禁（`verify_image_contract.sh` / `image_manifest.py`）已经覆盖的。
本文件不重复覆盖 docker 本身的行为——`save_image` / `read_image_digest` /
`import_and_check_digest` 都通过注入的 fake runner 测试，不依赖本机是否装了 docker。

`check_bundle_files` 一组是任务卡要求的"变异会失败、恢复正确对象后通过"的可复现固化：
构造一份自洽的候选包，改坏一个 tar 的字节，断言校验红；换回原字节，断言校验绿。
"""

from __future__ import annotations

import importlib.util
import json
import sys
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


WRITER = load(ROOT / "scripts/ci/write_epic_candidate_images.py", "epic_candidate_images_writer_under_test")
VERIFIER = load(ROOT / "scripts/ci/verify_epic_candidate_bundle.py", "epic_candidate_bundle_verifier_under_test")

HEAD = "a" * 40
TESTED = "b" * 40
TREE = "c" * 40
DIGEST = "sha256:" + "1" * 64
OTHER_DIGEST = "sha256:" + "2" * 64
TAR_SHA = "3" * 64


def _image(service: str, *, digest: str = DIGEST, tar_sha256: str = TAR_SHA, size: int = 42) -> dict:
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
        self.assertEqual([item["service"] for item in document["images"]], ["gateway", "migrate", "scheduler", "worker"])

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
            WRITER.save_image("x:y", Path("/tmp/does-not-matter.tar"), runner=self._runner(1, stderr="no such image"))

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
        failures = VERIFIER.check_manifest_shape(self._document(images=_images() + [_image("worker")]))
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
        document = {"repository": "a/b", "pr_number": 1, "head_sha": HEAD, "tree_sha": TREE, "run_id": 2}
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
    """`--import` 路径：docker load / inspect 都走 fake runner，不依赖本机 docker。"""

    def _document(self) -> dict:
        return {"images": [_image("worker", digest=DIGEST)]}

    def _fake_runner(self, *, load_returncode=0, inspect_stdout=DIGEST, inspect_returncode=0):
        def runner(argv):
            if argv[:2] == ["docker", "load"]:
                return VERIFIER.CommandResult(load_returncode, "", "" if load_returncode == 0 else "load failed")
            if argv[:2] == ["docker", "inspect"]:
                return VERIFIER.CommandResult(inspect_returncode, inspect_stdout, "" if inspect_returncode == 0 else "inspect failed")
            raise AssertionError(f"unexpected command: {argv}")

        return runner

    def test_matching_digest_passes(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            (directory / "lingxi-worker.tar").write_bytes(b"anything")
            failures = VERIFIER.import_and_check_digest(
                self._document(), directory, runner=self._fake_runner()
            )
            self.assertEqual(failures, [])

    def test_mismatched_digest_after_import_is_caught(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            (directory / "lingxi-worker.tar").write_bytes(b"anything")
            failures = VERIFIER.import_and_check_digest(
                self._document(), directory, runner=self._fake_runner(inspect_stdout=OTHER_DIGEST)
            )
            self.assertTrue(any("worker" in f and "不一致" in f for f in failures), failures)

    def test_docker_load_failure_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            (directory / "lingxi-worker.tar").write_bytes(b"anything")
            failures = VERIFIER.import_and_check_digest(
                self._document(), directory, runner=self._fake_runner(load_returncode=1)
            )
            self.assertTrue(any("导入失败" in f for f in failures), failures)

    def test_missing_tar_before_import_is_caught(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            failures = VERIFIER.import_and_check_digest(
                self._document(), directory, runner=self._fake_runner()
            )
            self.assertTrue(any("缺失" in f for f in failures), failures)


if __name__ == "__main__":
    unittest.main()
