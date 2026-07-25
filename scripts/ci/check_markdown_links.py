#!/usr/bin/env python3
"""检查正式 Markdown 文档中的本地链接，不访问外部网络。"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*]\(([^)\s]+)(?:\s+[^)]*)?\)")


def tracked_markdown_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "*.md"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    paths = []
    for raw_path in result.stdout.split("\0"):
        if not raw_path:
            continue
        path = Path(raw_path)
        if path.parts and path.parts[0] == ".tmp":
            continue
        paths.append(REPOSITORY_ROOT / path)
    return paths


def local_target(link: str) -> str | None:
    link = link.strip()
    if link.startswith("<") and link.endswith(">"):
        link = link[1:-1]
    if not link or link.startswith(("#", "//")):
        return None

    parsed = urlsplit(link)
    if parsed.scheme:
        return None
    return unquote(parsed.path)


def check_file(markdown_file: Path) -> list[str]:
    errors = []
    in_fenced_code = False

    for line_number, line in enumerate(
        markdown_file.read_text(encoding="utf-8").splitlines(), start=1
    ):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            in_fenced_code = not in_fenced_code
            continue
        if in_fenced_code:
            continue

        for match in MARKDOWN_LINK.finditer(line):
            target = local_target(match.group(1))
            if target is None:
                continue
            if not target:
                errors.append(f"{markdown_file.relative_to(REPOSITORY_ROOT)}:{line_number}: 本地链接缺少路径")
                continue

            candidate = (markdown_file.parent / target).resolve()
            try:
                candidate.relative_to(REPOSITORY_ROOT)
            except ValueError:
                errors.append(
                    f"{markdown_file.relative_to(REPOSITORY_ROOT)}:{line_number}: "
                    f"本地链接超出仓库范围：{target}"
                )
                continue

            if not candidate.exists():
                errors.append(
                    f"{markdown_file.relative_to(REPOSITORY_ROOT)}:{line_number}: "
                    f"本地链接目标不存在：{target}"
                )

    return errors


def main() -> int:
    errors = []
    for markdown_file in tracked_markdown_files():
        errors.extend(check_file(markdown_file))

    if errors:
        print("Markdown 本地链接检查失败：", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("Markdown 本地链接：通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
