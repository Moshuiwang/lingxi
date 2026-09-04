#!/usr/bin/env python3
"""检查正式 Markdown 文档中的本地链接，不访问外部网络。

除本地路径是否存在外，同时校验站内锚点（同文件 `#锚点` 与跨文件
`file.md#锚点`）是否指向目标文件中真实存在的标题。锚点按 GitHub 的标题
转锚点规则由目标文件标题现算，不依赖手工维护的清单。
"""

from __future__ import annotations

import re
import subprocess
import sys
import unicodedata
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from urllib.parse import unquote, urlsplit

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*]\(([^)\s]+)(?:\s+[^)]*)?\)")
# GFM 允许 ATX 标题行首有 0-3 个空格缩进（4 个及以上视为缩进代码块）。
# 标题级别本身不参与后续逻辑，用非捕获组；捕获组 1 是标题文本。
ATX_HEADING = re.compile(r"^ {0,3}#{1,6}\s+(.+?)(?:\s+#+)?\s*$")
# 标题里的行内链接/图片 `[文字](URL)` / `![alt](URL)`：GitHub 渲染后按
# 纯文本生成锚点，URL 本身不进 slug，所以生成锚点前要先归约成显示文字。
INLINE_LINK_OR_IMAGE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")


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
        markdown_file = REPOSITORY_ROOT / path
        if markdown_file.is_file():
            paths.append(markdown_file)
    return paths


def iter_unfenced_lines(markdown_file: Path) -> Iterator[tuple[int, str]]:
    """逐行产出 (行号, 行内容)，跳过 ``` / ~~~ 围栏代码块内的行。

    `check_file` 与 `heading_anchors` 都要在扫描前排除围栏代码块，此前
    两处各写了一遍围栏开关判断；抽成共用迭代器避免逻辑漂移。
    """
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
        yield line_number, line


def slugify(heading_text: str) -> str:
    """按 GitHub 的标题转锚点规则生成 slug。

    规则（据本仓库现有互相引用的锚点逐条核对得出）：先把标题里的行内
    链接/图片归约为其显示文字（GitHub 是先渲染成纯文本再生成锚点，
    URL 本身不进 slug）；整体转小写；删除所有非字母、非数字、非空格、
    非连字符、非下划线的字符（标点、符号一律整体删除，不留分隔符）；
    再把空格替换成连字符，不合并连续的连字符。
    """
    heading_text = INLINE_LINK_OR_IMAGE.sub(r"\1", heading_text)
    lowered = heading_text.lower()
    kept_chars = []
    for char in lowered:
        if char in (" ", "-", "_"):
            kept_chars.append(char)
            continue
        category = unicodedata.category(char)
        if category[0] in ("L", "N") or category in ("Mn", "Mc"):
            kept_chars.append(char)
    return "".join(kept_chars).replace(" ", "-")


@cache
def heading_anchors(markdown_file: Path) -> frozenset[str]:
    """目标文件中全部标题按顺序生成的锚点集合（含重复标题的去重后缀）。

    去重规则与 GitHub（github-slugger）一致：原始 slug 与派生出的
    `-N` 后缀共用同一张“已产出锚点”登记表，逐个尝试 `-1`、`-2`……
    直到撞不上为止。因此 `A`、`A`、`A-1` 三个标题依次得到
    `a`、`a-1`、`a-1-1`；若只按基础 slug 计数会得到 `a`、`a-1`、`a-1`，
    第三个标题的锚点会与第二个标题冲突。
    """
    anchors = []
    produced: set[str] = set()

    for _, line in iter_unfenced_lines(markdown_file):
        match = ATX_HEADING.match(line)
        if not match:
            continue

        base_slug = slugify(match.group(1).strip())
        slug = base_slug
        suffix = 1
        while slug in produced:
            slug = f"{base_slug}-{suffix}"
            suffix += 1
        produced.add(slug)
        anchors.append(slug)

    return frozenset(anchors)


def parse_local_link(link: str) -> tuple[str, str, str] | None:
    """解析本地链接为 (清理后的原始链接, 路径, 锚点)；

    非本地链接（外部协议、协议相对地址）返回 None。"""
    link = link.strip()
    if link.startswith("<") and link.endswith(">"):
        link = link[1:-1]
    if not link or link.startswith("//"):
        return None

    parsed = urlsplit(link)
    if parsed.scheme:
        return None
    return link, unquote(parsed.path), unquote(parsed.fragment)


def check_file(markdown_file: Path) -> list[str]:
    errors = []

    for line_number, line in iter_unfenced_lines(markdown_file):
        for match in MARKDOWN_LINK.finditer(line):
            parsed = parse_local_link(match.group(1))
            if parsed is None:
                continue
            cleaned_link, target_path, fragment = parsed

            if not target_path and not fragment:
                if cleaned_link.startswith("#"):
                    # 形如 `(#)` 的占位链接，不指向任何标题，不视为坏锚点。
                    continue
                errors.append(f"{markdown_file.relative_to(REPOSITORY_ROOT)}:{line_number}: 本地链接缺少路径")
                continue

            if target_path:
                candidate = (markdown_file.parent / target_path).resolve()
                try:
                    candidate.relative_to(REPOSITORY_ROOT)
                except ValueError:
                    errors.append(
                        f"{markdown_file.relative_to(REPOSITORY_ROOT)}:{line_number}: "
                        f"本地链接超出仓库范围：{target_path}"
                    )
                    continue

                if not candidate.exists():
                    errors.append(
                        f"{markdown_file.relative_to(REPOSITORY_ROOT)}:{line_number}: "
                        f"本地链接目标不存在：{target_path}"
                    )
                    continue

                target_file = candidate
            else:
                target_file = markdown_file

            if fragment and target_file.is_file() and target_file.suffix.lower() == ".md":
                if fragment not in heading_anchors(target_file):
                    display_target = f"{target_path}#{fragment}" if target_path else f"#{fragment}"
                    errors.append(
                        f"{markdown_file.relative_to(REPOSITORY_ROOT)}:{line_number}: "
                        f"本地链接锚点不存在：{display_target}"
                        f"（{target_file.relative_to(REPOSITORY_ROOT)} 无该标题生成的锚点）"
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
