#!/usr/bin/env python3
"""按固定镜像生成预发布记录，正式提升只改变发布资格、不重新构建。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from urllib.parse import quote

SERVICES = ("scheduler", "migrate", "gateway", "worker")
VERSION = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
TAG = re.compile(rf"^v{VERSION}(?:-rc\.([1-9][0-9]*))?$")
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class ReleaseError(RuntimeError):
    pass


def command(*args: str, input_text: str | None = None) -> str:
    if args[0] == "gh":
        entry = os.environ.get("LINGXI_GH_COMMAND")
        if entry:
            if not Path(entry).is_absolute():
                raise ReleaseError("机器身份入口必须使用绝对路径")
            args = (entry, *args[1:])
        elif os.environ.get("GITHUB_ACTIONS") != "true":
            raise ReleaseError(
                "本机必须设置 LINGXI_GH_COMMAND 为已批准的机器身份入口，不使用个人登录"
            )
    result = subprocess.run(args, input=input_text, capture_output=True, text=True, timeout=120)
    if result.returncode:
        # CLI 错误可能包含认证头或临时下载地址，日志只给命令名和退出码。
        raise ReleaseError(f"{args[0]} 执行失败，退出码 {result.returncode}")
    return result.stdout.strip()


def api(path: str, *, method: str = "GET", data: dict | None = None):
    argv = ["gh", "api", "-X", method, path]
    if data is not None:
        argv.extend(["--input", "-"])
    result = command(*argv, input_text=json.dumps(data) if data is not None else None)
    return json.loads(result) if result else None


def paginated(path: str) -> list:
    result = []
    for page in range(1, 101):
        batch = api(f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
        result.extend(batch)
        if len(batch) < 100:
            return result
    raise ReleaseError("分页超过上限，无法确认记录完整")


def canonical(document: dict) -> bytes:
    return (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def fingerprint(document: dict) -> str:
    return hashlib.sha256(canonical(document)).hexdigest()


def release_version(tag: str) -> tuple[str, bool]:
    match = TAG.fullmatch(tag)
    if not match:
        raise ReleaseError("版本必须为 vX.Y.Z 或 vX.Y.Z-rc.N")
    return ".".join(match.group(1, 2, 3)), bool(match.group(4))


def validate_manifest(doc: dict, repository: str, *, prerelease: bool) -> None:
    version, is_candidate = release_version(doc.get("tag", ""))
    expected_branch = "release/" + ".".join(version.split(".")[:2])
    if (
        doc.get("schema") != 1
        or doc.get("repository") != repository
        or doc.get("version") != version
        or doc.get("branch") != expected_branch
        or doc.get("prerelease") is not prerelease
        or is_candidate != prerelease
    ):
        raise ReleaseError("发布类型、版本、分支或仓库不一致")
    if not SHA.fullmatch(doc.get("commit", "")) or not SHA.fullmatch(doc.get("tree", "")):
        raise ReleaseError("发布记录缺少固定提交与源码树")
    if not isinstance(doc.get("run_id"), int) or doc["run_id"] <= 0:
        raise ReleaseError("发布记录缺少有效构建编号")
    images = doc.get("images", {})
    if set(images) != set(SERVICES):
        raise ReleaseError("发布必须包含四个完整镜像")
    for service, reference in images.items():
        prefix = f"ghcr.io/{repository.lower()}-{service}@"
        if not isinstance(reference, str) or not reference.startswith(prefix):
            raise ReleaseError("镜像仓库或服务不匹配")
        if not DIGEST.fullmatch(reference[len(prefix) :]):
            raise ReleaseError("镜像必须按完整 digest 固定")
    if not isinstance(doc.get("migration_heads"), list) or not doc["migration_heads"]:
        raise ReleaseError("发布记录缺少迁移头")


def validate_acceptance(receipt: dict, candidate: dict) -> None:
    repository = candidate["repository"]
    if (
        receipt.get("schema") != 1
        or receipt.get("candidate_tag") != candidate["tag"]
        or receipt.get("manifest_sha256") != fingerprint(candidate)
        or receipt.get("result") != "passed"
    ):
        raise ReleaseError("没有与本候选完全匹配的通过记录")
    url_pattern = rf"https://github\.com/{re.escape(repository)}/(?:issues|pull)/[1-9][0-9]*(?:#issuecomment-[0-9]+)?"
    if not re.fullmatch(url_pattern, receipt.get("evidence_url", "")):
        raise ReleaseError("验收证据必须指向本仓库明确的工作记录")
    recovery = receipt.get("recovery", {})
    if not isinstance(recovery, dict) or not recovery.get("instructions"):
        raise ReleaseError("缺少实际可执行的恢复说明")
    if not re.fullmatch(url_pattern, recovery.get("evidence_url", "")):
        raise ReleaseError("缺少恢复验证证据")
    old_version, old_candidate = release_version(recovery.get("previous_tag", ""))
    if old_candidate or tuple(map(int, old_version.split("."))) >= tuple(
        map(int, candidate["version"].split("."))
    ):
        raise ReleaseError("恢复版本必须是更早的正式版本")


def validate_release_record(release: dict, doc: dict) -> None:
    if release.get("draft") or release.get("tag_name") != doc["tag"]:
        raise ReleaseError("GitHub Release 尚未发布或版本不匹配")
    if release.get("prerelease") is not doc["prerelease"]:
        raise ReleaseError("GitHub Release 标记与清单不一致")
    assets = [a for a in release.get("assets", []) if a.get("name") == "release-manifest.json"]
    if len(assets) != 1:
        raise ReleaseError("发布记录必须恰好有一份镜像清单")


def find_release(repository: str, tag: str) -> dict | None:
    return next(
        (r for r in paginated(f"repos/{repository}/releases") if r["tag_name"] == tag), None
    )


def load_release(repository: str, tag: str, *, require_completed: bool = True) -> tuple[dict, dict]:
    release_version(tag)
    release = find_release(repository, tag)
    if release is None:
        raise ReleaseError("指定版本不存在")
    with tempfile.TemporaryDirectory(prefix="lingxi-release-") as directory:
        command(
            "gh",
            "release",
            "download",
            tag,
            "--repo",
            repository,
            "--pattern",
            "release-manifest.json",
            "--dir",
            directory,
        )
        doc = json.loads((Path(directory) / "release-manifest.json").read_text())
    validate_manifest(doc, repository, prerelease=release_version(tag)[1])
    validate_release_record(release, doc)
    actual = api(f"repos/{repository}/commits/{quote(tag, safe='')}")
    if actual["sha"] != doc["commit"] or actual["commit"]["tree"]["sha"] != doc["tree"]:
        raise ReleaseError("版本标签与清单源码不一致")
    run = api(f"repos/{repository}/actions/runs/{doc['run_id']}")
    if (
        (require_completed and run.get("conclusion") != "success")
        or run.get("head_sha") != doc["commit"]
        or run.get("head_branch") != doc["branch"]
        or run.get("event") != "push"
        or run.get("path") != ".github/workflows/publish.yml"
    ):
        raise ReleaseError("找不到对应维护分支的成功构建记录")
    return release, doc


def write_release(doc: dict, output: Path) -> None:
    output.write_bytes(canonical(doc))
    repo, tag = doc["repository"], doc["tag"]
    existing = find_release(repo, tag)
    if existing and not existing.get("draft"):
        _, old = load_release(repo, tag, require_completed=False)
        if old != doc:
            raise ReleaseError("已存在同名版本且内容不一致，禁止覆盖")
        print(f"版本已存在且内容一致：{tag}")
        return
    tags = paginated(f"repos/{repo}/git/matching-refs/tags/{quote(tag, safe='')}")
    exact = next((t for t in tags if t["ref"] == f"refs/tags/{tag}"), None)
    if exact:
        actual = api(f"repos/{repo}/commits/{quote(tag, safe='')}")
        if actual["sha"] != doc["commit"]:
            raise ReleaseError("版本标签已指向其他提交，禁止移动")
    else:
        api(
            f"repos/{repo}/git/refs",
            method="POST",
            data={"ref": f"refs/tags/{tag}", "sha": doc["commit"]},
        )
    notes = f"{'预发布候选；尚不可用于生产' if doc['prerelease'] else '正式发布包已就绪；不代表生产已部署'}。\n\n源码：{doc['commit']}\n维护分支：{doc['branch']}\n镜像清单摘要：{fingerprint(doc)}\n"
    if existing is None:
        existing = api(
            f"repos/{repo}/releases",
            method="POST",
            data={
                "tag_name": tag,
                "target_commitish": doc["commit"],
                "name": tag,
                "body": notes,
                "draft": True,
                "prerelease": doc["prerelease"],
                "make_latest": "false",
            },
        )
    else:
        assets = existing.get("assets", [])
        if assets:
            # 中断后只接受此前上传的同一份内容，不删除或覆盖任何附件。
            with tempfile.TemporaryDirectory() as directory:
                command(
                    "gh",
                    "release",
                    "download",
                    tag,
                    "--repo",
                    repo,
                    "--pattern",
                    "release-manifest.json",
                    "--dir",
                    directory,
                )
                if (Path(directory) / "release-manifest.json").read_bytes() != canonical(doc):
                    raise ReleaseError("草稿已有不同清单，拒绝覆盖")
    if not existing.get("assets"):
        command(
            "gh", "release", "upload", tag, str(output) + "#release-manifest.json", "--repo", repo
        )
    api(
        f"repos/{repo}/releases/{existing['id']}",
        method="PATCH",
        data={
            "draft": False,
            "prerelease": doc["prerelease"],
            "body": notes,
            "make_latest": "false",
        },
    )
    print(f"GitHub Release 已写入：{tag}；镜像没有重新构建")


def prepare(output: Path) -> None:
    version = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
    release_version("v" + version)
    branch = os.environ.get("GITHUB_REF_NAME", "")
    expected = "release/" + ".".join(version.split(".")[:2])
    if branch != expected or os.environ.get("GITHUB_REF_PROTECTED") != "true":
        raise ReleaseError("仅允许匹配版本号的受保护维护分支打包")
    commit = os.environ["GITHUB_SHA"]
    if command("git", "rev-parse", "HEAD") != commit:
        raise ReleaseError("当前检出不是工作流固定提交")
    doc = {
        "schema": 1,
        "repository": os.environ["GITHUB_REPOSITORY"],
        "version": version,
        "tag": f"v{version}-rc.{os.environ['GITHUB_RUN_NUMBER']}",
        "prerelease": True,
        "branch": branch,
        "commit": commit,
        "tree": command("git", "rev-parse", "HEAD^{tree}"),
        "run_id": int(os.environ["GITHUB_RUN_ID"]),
    }
    output.write_bytes(canonical(doc))


def migration_heads() -> list[str]:
    import ast

    revisions, parents = set(), set()
    for path in Path("migrations/alembic/versions").glob("*.py"):
        for node in ast.parse(path.read_text()).body:
            target = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
            elif isinstance(node, ast.AnnAssign):
                target = node.target
            if not isinstance(target, ast.Name) or target.id not in ("revision", "down_revision"):
                continue
            value = ast.literal_eval(node.value)
            if target.id == "revision":
                revisions.add(value)
            elif value is not None:
                parents.update(value if isinstance(value, tuple) else (value,))
    return sorted(revisions - parents)


def candidate(plan: Path, image_tag: str, output: Path) -> None:
    from push_image import read_back_digest

    doc = json.loads(plan.read_text())
    doc["images"] = {}
    for service in SERVICES:
        reference = f"ghcr.io/{doc['repository'].lower()}-{service}:{image_tag}"
        digest = read_back_digest(reference)
        if digest is None:
            raise ReleaseError("四镜像未全部推送并回读，不创建预发布")
        doc["images"][service] = reference.split(":", 1)[0] + "@" + digest
    doc["migration_heads"] = migration_heads()
    validate_manifest(doc, doc["repository"], prerelease=True)
    write_release(doc, output)


def receipt_for(candidate_doc: dict) -> dict:
    path = Path("deploy/releases/acceptance") / (candidate_doc["tag"] + ".json")
    if not path.is_file():
        raise ReleaseError("候选没有经过审查并合入 main 的验收记录，禁止正式提升")
    receipt = json.loads(path.read_text())
    validate_acceptance(receipt, candidate_doc)
    return receipt


def promote(repository: str, tag: str, output: Path, apply: bool) -> None:
    if (
        os.environ.get("GITHUB_REF") != "refs/heads/main"
        or os.environ.get("GITHUB_REF_PROTECTED") != "true"
    ):
        raise ReleaseError("正式提升只能由受保护 main 的发布工作流执行")
    _, candidate_doc = load_release(repository, tag)
    if not candidate_doc["prerelease"]:
        raise ReleaseError("必须选择预发布候选")
    receipt = receipt_for(candidate_doc)
    result = dict(
        candidate_doc,
        tag="v" + candidate_doc["version"],
        prerelease=False,
        candidate_tag=tag,
        candidate_manifest_sha256=fingerprint(candidate_doc),
        acceptance=receipt,
    )
    validate_manifest(result, repository, prerelease=False)
    output.write_bytes(canonical(result))
    if apply:
        write_release(result, output)
    else:
        print("正式提升预检通过；未写入版本")


def resolve(repository: str, tag: str, environment: str, output: Path) -> None:
    _, doc = load_release(repository, tag)
    if environment == "production":
        if doc["prerelease"]:
            raise ReleaseError("生产拒绝预发布版本")
        _, candidate_doc = load_release(repository, doc.get("candidate_tag", ""))
        receipt_path = "deploy/releases/acceptance/" + candidate_doc["tag"] + ".json"
        authoritative = api(f"repos/{repository}/contents/{receipt_path}?ref=main")
        receipt = json.loads(base64.b64decode(authoritative["content"]))
        validate_acceptance(receipt, candidate_doc)
        if doc.get("acceptance") != receipt:
            raise ReleaseError("正式版没有引用 main 中经过审查的同一验收记录")
        if any(
            doc.get(key) != candidate_doc.get(key)
            for key in ("commit", "tree", "images", "migration_heads", "run_id")
        ):
            raise ReleaseError("正式版不是已验收的同一份制品")
        if doc.get("candidate_manifest_sha256") != fingerprint(candidate_doc):
            raise ReleaseError("正式版与候选摘要不匹配")
    lines = [
        f"LINGXI_IMAGE_REGISTRY=ghcr.io/{repository.lower().rsplit('/', 1)[0]}",
        "LINGXI_IMAGE_TAG=" + tag,
    ]
    lines.extend(
        f"LINGXI_{name.upper()}_IMAGE_DIGEST=@{doc['images'][name].split('@')[1]}"
        for name in SERVICES
    )
    output.write_text("\n".join(lines) + "\n")
    print("部署选择已核对，只输出不含凭据的镜像配置；未部署")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--output", type=Path, required=True)
    p = sub.add_parser("candidate")
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--image-tag", required=True)
    p.add_argument("--output", type=Path, required=True)
    for name in ("promote", "resolve"):
        p = sub.add_parser(name)
        p.add_argument("--repository", required=True)
        p.add_argument("--tag", required=True)
        p.add_argument("--output", type=Path, required=True)
        if name == "promote":
            p.add_argument("--apply", action="store_true")
        else:
            p.add_argument("--environment", choices=("stage", "production"), required=True)
    args = parser.parse_args()
    if hasattr(args, "repository") and not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository
    ):
        raise ReleaseError("仓库必须是 owner/name")
    kwargs = vars(args).copy()
    operation = kwargs.pop("operation")
    globals()[operation](**kwargs)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ReleaseError, ValueError, KeyError, OSError, subprocess.TimeoutExpired) as error:
        print(f"发布拒绝：{error}", file=sys.stderr)
        raise SystemExit(1) from error
