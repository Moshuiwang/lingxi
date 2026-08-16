#!/usr/bin/env python3
"""导出 Epic Full 已构建的四个候选镜像并生成机器可读 manifest（Issue #150）。

`Epic Full / image` job 已经在本机（runner）构建好四个镜像（`lingxi-<service>:build-a`），
但那些镜像只存在于当次 runner 的 docker daemon 里，job 结束就随 runner 一起消失。
`biai-stage` 想要拿**这个 PR 的这次构建**去验收（而不是合并后 `Main Publish` 重新构建的
另一份对象），就需要这份构建产物能被下载、且下载下来的东西能被证明"就是这次构建的"。

本脚本负责后半句：把每个镜像 `docker save` 成 tar，各自算一份 sha256（下载完整性）与
`docker inspect` 的 `.Id`（镜像内容 digest，`docker load` 之后可独立回读核对），
连同 PR head SHA、Git tree、构建输入（tested sha / 批次 / run id）一起写进 manifest.json。

**digest 不是跨构建稳定值**：`.Id` 是镜像 config 的 sha256，config 里带 `created`
时间戳，因此同一源码两次构建的 `.Id`必然不同（`image_manifest.py` 的模块说明有更完整的
论证）。这里不需要跨构建稳定——manifest 只承诺"这份 tar 展开后就是这个 digest 的镜像"，
Stage 下载后 `docker load` 回读到相同的 `.Id`，就证明传输过程没有被替换或损坏。

用法：

    python3 scripts/ci/write_epic_candidate_images.py \\
        --repository moshuiwang/lingxi --pr-number 150 \\
        --head-sha <40 位> --tested-sha <40 位> --tree-sha <40 位> --run-id 123 \\
        --batch 20260813 --output-dir /tmp/epic-candidate-images \\
        --image scheduler=lingxi-scheduler:build-a \\
        --image migrate=lingxi-migrate:build-a \\
        --image gateway=lingxi-gateway:build-a \\
        --image worker=lingxi-worker:build-a
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass

# 与 deploy/README.md「本版包含哪些进程」逐字一致：admin 进程尚未建立，不为它构建镜像。
REQUIRED_SERVICES = ("gateway", "migrate", "scheduler", "worker")

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
BATCH_RE = re.compile(r"^\d{8}$")
IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
TAR_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def run_command(argv: list[str]) -> CommandResult:
    completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def parse_image_argument(raw: str) -> tuple[str, str]:
    """把 `--image scheduler=lingxi-scheduler:build-a` 拆成 `(service, reference)`。"""

    if "=" not in raw:
        raise ValueError(f"--image 参数 `{raw}` 不是 `<service>=<reference>` 形状")
    service, _, reference = raw.partition("=")
    service, reference = service.strip(), reference.strip()
    if not service or not reference:
        raise ValueError(f"--image 参数 `{raw}` 的 service 或 reference 为空")
    return service, reference


def save_image(reference: str, destination: pathlib.Path, runner=run_command) -> None:
    """`docker save` 到指定路径。失败时抛出携带 stderr 的 RuntimeError，不吞错。"""

    result = runner(["docker", "save", "-o", str(destination), reference])
    if result.returncode != 0:
        raise RuntimeError(
            f"docker save {reference} 失败：{(result.stderr or result.stdout).strip()}"
        )


def read_image_digest(reference: str, runner=run_command) -> str:
    """读一个**本地**镜像的 `.Id`（配置 digest）。形状不对时拒绝，不猜测。"""

    result = runner(["docker", "inspect", "--format", "{{.Id}}", reference])
    if result.returncode != 0:
        raise RuntimeError(f"docker inspect {reference} 失败：{result.stderr.strip()}")
    digest = result.stdout.strip()
    if not IMAGE_DIGEST_RE.fullmatch(digest):
        raise RuntimeError(f"镜像 {reference} 的 .Id 不是合法 digest 形状：{digest!r}")
    return digest


def sha256_file(path: pathlib.Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def manifest_document(
    *,
    repository: str,
    pr_number: int,
    head_sha: str,
    tested_sha: str,
    tree_sha: str,
    run_id: int,
    batch: str,
    generated_at: str,
    images: list[dict[str, object]],
) -> dict[str, object]:
    """校验并组装 manifest 正文。纯函数，不碰文件系统或 docker——供单测直接调用。"""

    if repository.count("/") != 1:
        raise ValueError("repository 必须是 owner/name")
    if pr_number <= 0 or run_id <= 0:
        raise ValueError("pr_number 与 run_id 必须为正整数")
    for label, value in (("head_sha", head_sha), ("tested_sha", tested_sha), ("tree_sha", tree_sha)):
        if not SHA_RE.fullmatch(value):
            raise ValueError(f"{label} 不是 40 位小写 Git SHA")
    if not BATCH_RE.fullmatch(batch):
        raise ValueError(f"batch `{batch}` 不是 8 位日期")

    services = tuple(sorted(str(image.get("service", "")) for image in images))
    if services != REQUIRED_SERVICES:
        raise ValueError(
            f"镜像集合为 {services}，必须恰好是 {REQUIRED_SERVICES}"
            "（四个正式交付进程各一次，缺一不可、多一不可）"
        )

    for image in images:
        for field in ("service", "reference", "tar", "tar_sha256", "image_digest"):
            if not image.get(field):
                raise ValueError(f"镜像条目缺少 {field}：{image}")
        if not IMAGE_DIGEST_RE.fullmatch(str(image["image_digest"])):
            raise ValueError(f"镜像 {image['service']} 的 image_digest 形状非法：{image['image_digest']}")
        if not TAR_SHA_RE.fullmatch(str(image["tar_sha256"])):
            raise ValueError(f"镜像 {image['service']} 的 tar_sha256 形状非法：{image['tar_sha256']}")
        size = image.get("tar_size_bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ValueError(f"镜像 {image['service']} 的 tar_size_bytes 非法：{size!r}")

    return {
        "schema": 1,
        "repository": repository,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "tested_sha": tested_sha,
        "tree_sha": tree_sha,
        "run_id": run_id,
        "batch": batch,
        "generated_at": generated_at,
        "images": sorted(images, key=lambda item: str(item["service"])),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--tested-sha", required=True)
    parser.add_argument("--tree-sha", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--batch", required=True, help="8 位发布批次日期，与 build_image.sh 的 LINGXI_IMAGE_BATCH 一致")
    parser.add_argument("--output-dir", type=pathlib.Path, required=True, help="tar 与 manifest.json 的落地目录")
    parser.add_argument(
        "--image",
        action="append",
        default=[],
        metavar="SERVICE=REFERENCE",
        help="可重复；四个服务各一次，例如 scheduler=lingxi-scheduler:build-a",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    short_commit = args.tested_sha[:12]

    images: list[dict[str, object]] = []
    for raw in args.image:
        service, reference = parse_image_argument(raw)
        tar_name = f"lingxi-{service}-{args.batch}-{short_commit}.tar"
        tar_path = args.output_dir / tar_name
        print(f"导出 {service}（{reference}） → {tar_path}")
        save_image(reference, tar_path)
        tar_sha256 = sha256_file(tar_path)
        tar_size_bytes = tar_path.stat().st_size
        digest = read_image_digest(reference)
        print(f"  tar sha256={tar_sha256[:16]}… ({tar_size_bytes} bytes)  image digest={digest[:19]}…")
        images.append(
            {
                "service": service,
                "reference": reference,
                "tar": tar_name,
                "tar_sha256": tar_sha256,
                "tar_size_bytes": tar_size_bytes,
                "image_digest": digest,
            }
        )

    generated_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    document = manifest_document(
        repository=args.repository,
        pr_number=args.pr_number,
        head_sha=args.head_sha,
        tested_sha=args.tested_sha,
        tree_sha=args.tree_sha,
        run_id=args.run_id,
        batch=args.batch,
        generated_at=generated_at,
        images=images,
    )
    manifest_path = args.output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    print(f"候选镜像 manifest：{manifest_path}（{len(images)} 个镜像，PR #{args.pr_number}，tree={args.tree_sha[:12]}）")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError) as error:
        print(f"导出候选镜像失败：{error}", file=sys.stderr)
        raise SystemExit(1) from error
