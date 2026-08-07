#!/usr/bin/env python3
"""把 Story PR 路由到文档快检、普通快检或完整门禁（Issue #82）。

未知路径一律升级到完整门禁。分类器的目标不是猜得尽可能细，而是让新增目录不会因为
没人更新路径表而静默绕过检查。
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


DOCUMENT_PREFIXES = ("docs/",)
DOCUMENT_FILES = {"AGENTS.md", "README.md"}
FULL_PREFIXES = (".github/workflows/", "deploy/", "migrations/", "scripts/ci/")
FULL_FILES = {".dockerignore", "Dockerfile", "alembic.ini", "pyproject.toml"}
FAST_PREFIXES = ("experiments/", "scripts/dev/", "src/", "tests/", "workers/")


def is_document(path: str) -> bool:
    return path.endswith(".md") or path in DOCUMENT_FILES or path.startswith(DOCUMENT_PREFIXES)


def is_full(path: str) -> bool:
    return path in FULL_FILES or path.startswith(FULL_PREFIXES)


def is_fast(path: str) -> bool:
    return path.startswith(FAST_PREFIXES)


def classify(paths: list[str]) -> str:
    normalized = []
    for raw in paths:
        path = raw.strip()
        if not path:
            continue
        normalized.append(path[2:] if path.startswith("./") else path)
    if not normalized:
        return "full"
    if all(is_document(path) for path in normalized):
        return "docs"
    if any(is_full(path) for path in normalized):
        return "full"
    if all(is_document(path) or is_fast(path) for path in normalized):
        return "fast"
    return "full"


def changed_paths(base: str, head: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", base, head],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()


def write_output(destination: Path, mode: str, paths: list[str]) -> None:
    worker_changed = any(path.startswith("workers/") for path in paths)
    with destination.open("a", encoding="utf-8") as output:
        output.write(f"mode={mode}\n")
        output.write(f"worker_changed={'true' if worker_changed else 'false'}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()

    paths = changed_paths(args.base, args.head)
    mode = classify(paths)
    write_output(args.github_output, mode, paths)
    print(f"Story 路由：{mode}（{len(paths)} 个变更路径）")
    for path in paths:
        print(f"  - {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
