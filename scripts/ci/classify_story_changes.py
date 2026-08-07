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
    return path in DOCUMENT_FILES or path.startswith(DOCUMENT_PREFIXES)


def is_full(path: str) -> bool:
    return path in FULL_FILES or path.startswith(FULL_PREFIXES)


def is_fast(path: str) -> bool:
    return path.startswith(FAST_PREFIXES)


def classify(paths: list[str]) -> str:
    normalized = []
    for raw in paths:
        # Git 文件名允许空格与换行；不得 strip，否则 ` docs/x.md` 会被伪装成
        # 安全的 docs/** 路径。NUL 分隔已经负责界定文件名边界。
        path = raw
        if not path:
            continue
        normalized.append(path[2:] if path.startswith("./") else path)
    if not normalized:
        return "full"
    # 目录风险优先于扩展名：deploy/README.md 与 scripts/ci/README.md 仍属于
    # 高风险目录，未来即使 Markdown 被用作运行期模板也不会误走文档快检。
    if any(is_full(path) for path in normalized):
        return "full"
    if all(is_document(path) for path in normalized):
        return "docs"
    if all(is_document(path) or is_fast(path) for path in normalized):
        return "fast"
    return "full"


def changed_paths(base: str, head: str, *, repository: Path | None = None) -> list[str]:
    result = subprocess.run(
        # 不过滤 D/T 等状态；并关闭 rename 折叠，让高风险旧路径和新路径都进入分类。
        # -z 让 Git 输出原始文件名并用 NUL 分隔；否则中文等非 ASCII 路径会被
        # core.quotePath 转义，docs/** 会被误判成未知高风险路径。
        ["git", "diff", "--name-only", "-z", "--no-renames", base, head],
        check=True,
        capture_output=True,
        text=True,
        cwd=repository,
    )
    return [path for path in result.stdout.split("\0") if path]


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
