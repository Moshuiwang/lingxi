#!/usr/bin/env python3
"""使用标准库检查仓库内项目级 Skills 的基础结构。"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = REPOSITORY_ROOT / ".agents" / "skills"
NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FRONTMATTER_PATTERN = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
KEY_VALUE_PATTERN = re.compile(r"^([a-z][a-z0-9-]*):\s*(.+)$")
INTERFACE_KEY_PATTERN = re.compile(r'^  ([a-z_]+):\s*("(?:[^"\\]|\\.)*")$')


def quoted_value(raw_value: str) -> str:
    raw_value = raw_value.strip()
    if raw_value.startswith('"') and raw_value.endswith('"'):
        return json.loads(raw_value)
    return raw_value


def parse_frontmatter(skill_file: Path) -> tuple[dict[str, str], list[str]]:
    content = skill_file.read_text(encoding="utf-8")
    match = FRONTMATTER_PATTERN.match(content)
    if not match:
        return {}, ["SKILL.md 缺少有效的 YAML frontmatter"]

    values = {}
    errors = []
    for line in match.group(1).splitlines():
        key_value = KEY_VALUE_PATTERN.match(line)
        if not key_value:
            errors.append(f"无法解析 frontmatter：{line}")
            continue
        key, raw_value = key_value.groups()
        if key in values:
            errors.append(f"frontmatter 字段重复：{key}")
            continue
        try:
            values[key] = quoted_value(raw_value)
        except json.JSONDecodeError:
            errors.append(f"frontmatter 字符串格式无效：{key}")

    unexpected = sorted(set(values) - {"name", "description"})
    if unexpected:
        errors.append(f"frontmatter 包含非预期字段：{', '.join(unexpected)}")
    return values, errors


def parse_interface(metadata_file: Path) -> tuple[dict[str, str], list[str]]:
    if not metadata_file.is_file():
        return {}, ["缺少 agents/openai.yaml"]

    lines = metadata_file.read_text(encoding="utf-8").splitlines()
    if "interface:" not in lines:
        return {}, ["agents/openai.yaml 缺少 interface"]

    values = {}
    errors = []
    in_interface = False
    for line in lines:
        if line == "interface:":
            in_interface = True
            continue
        if not in_interface:
            continue
        if line and not line.startswith(" "):
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = INTERFACE_KEY_PATTERN.match(line)
        if not match:
            errors.append(f"无法解析 agents/openai.yaml：{line}")
            continue
        key, raw_value = match.groups()
        if key in values:
            errors.append(f"agents/openai.yaml 字段重复：{key}")
            continue
        try:
            values[key] = json.loads(raw_value)
        except json.JSONDecodeError:
            errors.append(f"agents/openai.yaml 字符串格式无效：{key}")
    return values, errors


def validate_skill(skill_dir: Path) -> list[str]:
    errors = []
    skill_name = skill_dir.name
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.is_file():
        return ["缺少 SKILL.md"]

    frontmatter, frontmatter_errors = parse_frontmatter(skill_file)
    errors.extend(frontmatter_errors)
    declared_name = frontmatter.get("name", "")
    description = frontmatter.get("description", "")

    if not NAME_PATTERN.fullmatch(skill_name) or len(skill_name) > 64:
        errors.append("目录名必须是 64 字符以内的 hyphen-case")
    if declared_name != skill_name:
        errors.append(f"frontmatter name 必须与目录名一致：{skill_name}")
    if not description or len(description) > 1024:
        errors.append("description 必须为 1 至 1024 个字符")
    if "<" in description or ">" in description:
        errors.append("description 不能包含尖括号")

    interface, interface_errors = parse_interface(skill_dir / "agents" / "openai.yaml")
    errors.extend(interface_errors)
    required_interface = {"display_name", "short_description", "default_prompt"}
    missing_interface = sorted(required_interface - set(interface))
    if missing_interface:
        errors.append(f"agents/openai.yaml 缺少字段：{', '.join(missing_interface)}")

    short_description = interface.get("short_description", "")
    if short_description and not 25 <= len(short_description) <= 64:
        errors.append("short_description 必须为 25 至 64 个字符")
    default_prompt = interface.get("default_prompt", "")
    if default_prompt and f"${skill_name}" not in default_prompt:
        errors.append(f"default_prompt 必须明确提及 ${skill_name}")

    if "TODO" in skill_file.read_text(encoding="utf-8"):
        errors.append("SKILL.md 仍包含 TODO")
    return errors


def main() -> int:
    if not SKILLS_ROOT.is_dir():
        print("项目 Skill 检查失败：缺少 .agents/skills", file=sys.stderr)
        return 1

    skill_dirs = sorted(path for path in SKILLS_ROOT.iterdir() if path.is_dir())
    if not skill_dirs:
        print("项目 Skills：目录为空", file=sys.stderr)
        return 1

    failures = []
    for skill_dir in skill_dirs:
        for error in validate_skill(skill_dir):
            failures.append(f"{skill_dir.relative_to(REPOSITORY_ROOT)}：{error}")

    if failures:
        print("项目 Skill 检查失败：", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print(f"项目 Skills：通过（{len(skill_dirs)} 个）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
