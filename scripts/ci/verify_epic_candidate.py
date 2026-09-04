#!/usr/bin/env python3
"""证明 main 当前树来自通过 Epic Full 的 PR 候选（Issue #82）。

证明存放在成功的 Epic Full run artifact 中。发布只比较 Git tree，而不是要求 PR
测试用的临时 merge SHA 与最终 merge SHA 相同；这样 GitHub 重新生成 merge commit 时
不会误拦，但 base 变化导致合并内容变化时一定会拦住。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any


class CandidateError(RuntimeError):
    pass


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """GitHub artifact 跳到对象存储时不把 GitHub Token 带到另一个主机。"""

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        redirected = super().redirect_request(
            request, file_pointer, code, message, headers, new_url
        )
        if redirected is None:
            return None
        old_host = urllib.parse.urlsplit(request.full_url).netloc
        new_host = urllib.parse.urlsplit(new_url).netloc
        if old_host != new_host:
            redirected.remove_header("Authorization")
        return redirected


def select_merged_pr(pulls: list[dict[str, Any]], commit_sha: str) -> dict[str, Any]:
    eligible = [
        pr
        for pr in pulls
        if pr.get("merged_at")
        and pr.get("base", {}).get("ref") == "main"
        and pr.get("merge_commit_sha") == commit_sha
    ]
    if len(eligible) != 1:
        raise CandidateError(f"当前提交对应的已合并 main PR 数量为 {len(eligible)}，要求恰好 1")
    return eligible[0]


def validate_document(
    document: dict[str, Any], *, repository: str, pr: dict[str, Any], tree_sha: str, run_id: int
) -> None:
    expected = {
        "schema": 1,
        "repository": repository,
        "pr_number": pr["number"],
        "head_sha": pr["head"]["sha"],
        "tree_sha": tree_sha,
        "run_id": run_id,
    }
    mismatches = [key for key, value in expected.items() if document.get(key) != value]
    if mismatches:
        raise CandidateError("候选证明与 main 不一致：" + "、".join(mismatches))
    tested_sha = document.get("tested_sha")
    if not isinstance(tested_sha, str) or len(tested_sha) != 40:
        raise CandidateError("候选证明缺少有效 tested_sha")


class GitHubReader:
    def __init__(self, token: str, api_url: str = "https://api.github.com") -> None:
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.opener = urllib.request.build_opener(SafeRedirectHandler())

    def request(self, url: str, *, accept: str = "application/vnd.github+json") -> bytes:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": accept,
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "lingxi-main-publish",
            },
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                return response.read()
        except (urllib.error.URLError, urllib.error.HTTPError) as error:
            raise CandidateError(f"GitHub API 回读失败：{error}") from error

    def json(self, path: str) -> Any:
        return json.loads(self.request(f"{self.api_url}{path}"))


def read_candidate_from_zip(payload: bytes) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            if names != ["candidate.json"]:
                raise CandidateError(f"候选 artifact 文件列表异常：{names}")
            return json.loads(archive.read("candidate.json"))
    except (zipfile.BadZipFile, json.JSONDecodeError) as error:
        raise CandidateError(f"候选 artifact 无法解析：{error}") from error


def verified_candidate(
    reader: GitHubReader, *, repository: str, commit_sha: str, tree_sha: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    encoded_repo = urllib.parse.quote(repository, safe="/")
    pulls = reader.json(f"/repos/{encoded_repo}/commits/{commit_sha}/pulls")
    pr = select_merged_pr(pulls, commit_sha)
    artifact_name = f"epic-candidate-pr-{pr['number']}-{pr['head']['sha']}"
    encoded_head = urllib.parse.quote(pr["head"]["sha"], safe="")

    for page in range(1, 4):
        runs = reader.json(
            f"/repos/{encoded_repo}/actions/workflows/ci.yml/runs"
            f"?event=pull_request&status=success&head_sha={encoded_head}&per_page=100&page={page}"
        ).get("workflow_runs", [])
        for run in runs:
            # 私有 GitHub App 读取的 workflow run 中 pull_requests 经常是空数组；
            # head_sha 则稳定等于 PR head。artifact 名与正文再同时绑定 PR 编号，
            # 不能只靠分支名（分支名可以复用）。
            if run.get("head_sha") != pr["head"]["sha"]:
                continue
            artifacts = reader.json(
                f"/repos/{encoded_repo}/actions/runs/{run['id']}/artifacts"
            ).get("artifacts", [])
            artifact = next(
                (
                    item
                    for item in artifacts
                    if item.get("name") == artifact_name and not item.get("expired")
                ),
                None,
            )
            if artifact is None:
                continue
            payload = reader.request(
                artifact["archive_download_url"], accept="application/vnd.github+json"
            )
            document = read_candidate_from_zip(payload)
            try:
                validate_document(
                    document,
                    repository=repository,
                    pr=pr,
                    tree_sha=tree_sha,
                    run_id=run["id"],
                )
            except CandidateError:
                continue
            return pr, document
        if len(runs) < 100:
            break
    raise CandidateError(
        f"找不到 PR #{pr['number']} 对应且内容一致的成功 Epic Full 候选证明。"
        "最常见成因（2026-08-28 Trace #373 H1 实例）：epic/* → main 的 PR 首跑只在 PR 头提交带"
        "独占一行的 git trailer `Image-Candidate: true` 时才构建镜像与候选证明（ci.yml image job"
        " 的 Issue #278 条件）；漏了标记则 Epic Full 绿但无证明，本检查失败关闭。补救：给 epic 分支"
        "补一个带该 trailer 的提交重新走 PR，或由具备 Actions 写权限者重跑该 Epic Full run"
        "（attempt≥2 无条件构建），或从非 epic/* 分支重走一次 PR。"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--tree-sha", required=True)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise CandidateError("缺少 GITHUB_TOKEN，不能回读候选证明")
    pr, document = verified_candidate(
        GitHubReader(token),
        repository=args.repository,
        commit_sha=args.commit_sha,
        tree_sha=args.tree_sha,
    )
    message = (
        f"main 候选身份：通过（PR #{pr['number']}，Epic Full run {document['run_id']}，"
        f"tree={args.tree_sha[:12]}）"
    )
    print(message)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as summary:
            summary.write(f"## 已验收候选\n\n- {message}\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CandidateError as error:
        print(f"main 候选身份：不通过：{error}", file=__import__("sys").stderr)
        raise SystemExit(1) from error
