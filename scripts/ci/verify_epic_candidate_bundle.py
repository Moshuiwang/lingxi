#!/usr/bin/env python3
"""校验一份已下载解压的 PR 候选镜像包（Issue #150）。

用途有两处：

1. **`Epic Full / image` job 自校验**：manifest 与四个 tar 刚写完，立即用本脚本核对
   一遍再上传——"产物写对了"和"产物真的写对了"是两回事，前者只是假设。
2. **`biai-stage` 下载/导入校验**：从
   `epic-candidate-images-pr-<PR 编号>-<PR head sha>` artifact 下载并解压后，
   `docker compose ... up` 之前必须先跑通本脚本；`--import` 会额外执行 `docker load`
   并核对回读到的镜像 digest，证明"导入到本机 docker 的东西"与 manifest 记录的一致。

判定分两层，任一层不一致都会让脚本以非零退出：

  - **完整性**（不需要 docker）：manifest 结构合法、四个服务齐全
    （scheduler / migrate / worker / gateway，缺一不可、多一不可）、每个 tar 文件都在、
    大小与 sha256 都与 manifest 记录的一致。
  - **来源绑定**（不需要 docker）：调用方可传入 `--expect-*`，与 manifest 记录的
    repository / PR 编号 / head sha / tree sha / run id 核对——防止"文件是对的，
    但对应的是另一个 PR 或另一次构建"。
  - **导入一致**（`--import`，需要 docker）：`docker load` 之后回读每个镜像的 `.Id`，
    核对与 manifest 记录的 image_digest 一致。

全部失败一次性收集后统一报告，不在中途停下——这样 Stage 操作者一次就能看到全部问题，
不必来回重跑。

用法：

    python3 scripts/ci/verify_epic_candidate_bundle.py /path/to/bundle-dir \\
        --expect-repository moshuiwang/lingxi --expect-pr-number 150 \\
        --expect-head-sha <40 位> --expect-run-id 123 --import
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass

REQUIRED_SERVICES = ("gateway", "migrate", "scheduler", "worker")
IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
TAR_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def run_command(argv: list[str]) -> CommandResult:
    completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


class BundleError(RuntimeError):
    """manifest.json 本身读不出来或结构坏到无法继续核对时用这个，其余情形累积成 list[str]。"""


def load_manifest(bundle_dir: pathlib.Path) -> dict[str, object]:
    manifest_path = bundle_dir / "manifest.json"
    if not manifest_path.is_file():
        raise BundleError(f"{bundle_dir} 下没有 manifest.json（artifact 缺失或下载不完整）")
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise BundleError(f"manifest.json 不是合法 JSON：{error}") from error
    if not isinstance(document, dict):
        raise BundleError("manifest.json 顶层必须是一个对象")
    return document


def check_manifest_shape(document: dict[str, object]) -> list[str]:
    """结构与四镜像齐全性核对，不涉及文件系统或 docker。"""

    failures: list[str] = []
    if document.get("schema") != 1:
        failures.append(f"manifest.schema 是 {document.get('schema')!r}，期望 1")

    for label in ("head_sha", "tested_sha", "tree_sha"):
        value = document.get(label)
        if not isinstance(value, str) or not SHA_RE.fullmatch(value):
            failures.append(f"manifest.{label} 不是 40 位小写 Git SHA：{value!r}")

    for label in ("pr_number", "run_id"):
        value = document.get(label)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            failures.append(f"manifest.{label} 不是正整数：{value!r}")

    if (
        not isinstance(document.get("repository"), str)
        or document.get("repository", "").count("/") != 1
    ):
        failures.append(f"manifest.repository 不是 owner/name 形状：{document.get('repository')!r}")

    images = document.get("images")
    if not isinstance(images, list):
        failures.append(f"manifest.images 不是列表：{images!r}")
        return failures

    if len(images) != len(REQUIRED_SERVICES):
        failures.append(
            f"manifest.images 有 {len(images)} 个条目，要求恰好 {len(REQUIRED_SERVICES)} 个"
        )

    services = tuple(
        sorted(str(item.get("service", "")) for item in images if isinstance(item, dict))
    )
    if services != REQUIRED_SERVICES:
        failures.append(f"manifest.images 的服务集合是 {services}，必须恰好是 {REQUIRED_SERVICES}")

    for item in images:
        if not isinstance(item, dict):
            failures.append(f"manifest.images 里有非对象条目：{item!r}")
            continue
        for field in (
            "service",
            "reference",
            "tar",
            "tar_sha256",
            "tar_size_bytes",
            "image_digest",
        ):
            if field not in item:
                failures.append(f"镜像条目缺少字段 {field}：{item}")
        digest = item.get("image_digest")
        if isinstance(digest, str) and not IMAGE_DIGEST_RE.fullmatch(digest):
            failures.append(f"镜像 {item.get('service')} 的 image_digest 形状非法：{digest!r}")
        tar_sha = item.get("tar_sha256")
        if isinstance(tar_sha, str) and not TAR_SHA_RE.fullmatch(tar_sha):
            failures.append(f"镜像 {item.get('service')} 的 tar_sha256 形状非法：{tar_sha!r}")

    return failures


def check_expectations(
    document: dict[str, object],
    *,
    expect_repository: str | None,
    expect_pr_number: int | None,
    expect_head_sha: str | None,
    expect_tree_sha: str | None,
    expect_run_id: int | None,
) -> list[str]:
    """把下载方期望绑定的 PR/构建身份与 manifest 记录的核对。全部可选，缺省不核对该项。"""

    failures: list[str] = []
    checks = (
        ("repository", expect_repository),
        ("pr_number", expect_pr_number),
        ("head_sha", expect_head_sha),
        ("tree_sha", expect_tree_sha),
        ("run_id", expect_run_id),
    )
    for field, expected in checks:
        if expected is None:
            continue
        actual = document.get(field)
        if actual != expected:
            failures.append(f"manifest.{field} 是 {actual!r}，期望 {expected!r}（候选身份不匹配）")
    return failures


def sha256_file(path: pathlib.Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def check_bundle_files(document: dict[str, object], bundle_dir: pathlib.Path) -> list[str]:
    """每个 tar 是否存在、大小和 sha256 是否与 manifest 记录一致（下载完整性）。

    先比大小、再算 sha256：大小不对时没必要白读一遍大文件；但两项都要检查——
    大小相同、内容被替换的情形（同尺寸的另一份 tar）只有 sha256 能挡住。
    """

    failures: list[str] = []
    images = document.get("images")
    if not isinstance(images, list):
        return failures  # check_manifest_shape 已经报过这个问题

    for item in images:
        if not isinstance(item, dict):
            continue
        service = item.get("service", "?")
        tar_name = item.get("tar")
        if not isinstance(tar_name, str):
            failures.append(f"镜像 {service} 的 manifest 条目没有合法 tar 文件名")
            continue
        tar_path = bundle_dir / tar_name
        if not tar_path.is_file():
            failures.append(f"镜像 {service} 的 tar 文件缺失：{tar_path}（下载不完整）")
            continue

        expected_size = item.get("tar_size_bytes")
        actual_size = tar_path.stat().st_size
        if isinstance(expected_size, int) and actual_size != expected_size:
            failures.append(
                f"镜像 {service} 的 tar 大小是 {actual_size} 字节，manifest 记录 {expected_size} 字节"
                "（下载不完整或被替换）"
            )

        expected_sha = item.get("tar_sha256")
        actual_sha = sha256_file(tar_path)
        if expected_sha != actual_sha:
            failures.append(
                f"镜像 {service} 的 tar sha256 不符：实际 {actual_sha}，manifest 记录 {expected_sha}"
            )

    return failures


def import_and_check_digest(
    document: dict[str, object], bundle_dir: pathlib.Path, runner=run_command
) -> list[str]:
    """`docker load` 每个 tar，回读 `.Id` 与 manifest 记录的 image_digest 核对。

    `docker load` 会按 tar 内嵌的 RepoTags 自动打回原来的引用（即 manifest 里的
    `reference`），因此 load 完直接对该引用 `docker inspect` 即可，不需要额外重新打 tag。
    """

    failures: list[str] = []
    images = document.get("images")
    if not isinstance(images, list):
        return failures

    for item in images:
        if not isinstance(item, dict):
            continue
        service = item.get("service", "?")
        tar_name = item.get("tar")
        reference = item.get("reference")
        expected_digest = item.get("image_digest")
        if not isinstance(tar_name, str) or not isinstance(reference, str):
            failures.append(f"镜像 {service} 缺少 tar 或 reference 字段，无法导入")
            continue
        tar_path = bundle_dir / tar_name
        if not tar_path.is_file():
            failures.append(f"镜像 {service} 的 tar 文件缺失，无法导入：{tar_path}")
            continue

        load_result = runner(["docker", "load", "-i", str(tar_path)])
        if load_result.returncode != 0:
            failures.append(
                f"镜像 {service} 导入失败：{(load_result.stderr or load_result.stdout).strip()}"
            )
            continue

        inspect_result = runner(["docker", "inspect", "--format", "{{.Id}}", reference])
        if inspect_result.returncode != 0:
            failures.append(
                f"镜像 {service} 导入后读不到引用 {reference}：{inspect_result.stderr.strip()}"
            )
            continue
        actual_digest = inspect_result.stdout.strip()
        if actual_digest != expected_digest:
            failures.append(
                f"镜像 {service} 导入后的 digest 是 {actual_digest}，manifest 记录 {expected_digest}"
                "（导入的对象与候选身份不一致）"
            )

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "bundle_dir", type=pathlib.Path, help="包含 manifest.json 与四个 tar 的目录"
    )
    parser.add_argument("--expect-repository")
    parser.add_argument("--expect-pr-number", type=int)
    parser.add_argument("--expect-head-sha")
    parser.add_argument("--expect-tree-sha")
    parser.add_argument("--expect-run-id", type=int)
    parser.add_argument(
        "--import",
        dest="do_import",
        action="store_true",
        help="额外执行 docker load 并核对导入后的镜像 digest（需要本机 docker）",
    )
    args = parser.parse_args()

    if not args.bundle_dir.is_dir():
        print(f"候选镜像包目录不存在：{args.bundle_dir}", file=sys.stderr)
        return 1

    try:
        document = load_manifest(args.bundle_dir)
    except BundleError as error:
        print(f"候选镜像包校验：不通过\n  - {error}", file=sys.stderr)
        return 1

    failures: list[str] = []
    failures.extend(check_manifest_shape(document))
    failures.extend(
        check_expectations(
            document,
            expect_repository=args.expect_repository,
            expect_pr_number=args.expect_pr_number,
            expect_head_sha=args.expect_head_sha,
            expect_tree_sha=args.expect_tree_sha,
            expect_run_id=args.expect_run_id,
        )
    )
    failures.extend(check_bundle_files(document, args.bundle_dir))
    if args.do_import:
        failures.extend(import_and_check_digest(document, args.bundle_dir))

    if failures:
        print("候选镜像包校验：不通过", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    images = document.get("images", [])
    print(
        f"候选镜像包校验：通过（PR #{document.get('pr_number')}，"
        f"head={str(document.get('head_sha'))[:12]}，tree={str(document.get('tree_sha'))[:12]}，"
        f"{len(images)} 个镜像{'，已导入并核对 digest' if args.do_import else ''}）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
