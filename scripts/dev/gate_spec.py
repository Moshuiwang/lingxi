#!/usr/bin/env python3
"""从 CI 工作流 YAML 现读门禁的环境配方，供 `scripts/dev/check.sh` 一键复现（Issue #236）。

背景：PR #233 的 `Epic Full / gate` 报过一次 `ModuleNotFoundError: No module named
'lark_oapi'`——根因是实施者本机虚拟环境装了全套 extras，而门禁只装 `scheduler` 组，
同一棵树「本机全绿、CI 直接 ERROR」。修复很简单，但发现它的唯一途径是把改动推上去
让 CI 跑一次。

本文件解决的不是那次具体缺陷，而是让类似环境漂移能在本机复现：extras 组合、
shellcheck 版本、Python 版本、真库参数**只在 `.github/workflows/{ci,story}.yml`
里写一份**，本文件只读不抄——`scripts/dev/check.sh` 不允许另起一份硬编码清单
（Issue #236 范围明确禁止「两处各写一份清单」）。

不使用 YAML 库：`scripts/ci/check_installed_package.py` 的 extras 矩阵对账
（`_MATRIX_LINE`）已经是「按锚点做正则解析、写法变了就直接失败」的先例，这里延续
同一约定，不为一个开发工具引入新依赖。解析失败一律抛 `GateSpecError` 并说明原因，
不安静地退回旧值——旧值退回等于本工具自己制造出它要消灭的那类漂移。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
STORY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "story.yml"

_TOP_LEVEL_JOB_KEY = re.compile(r"^  [A-Za-z0-9_-]+:[ \t]*$")
_STEP_NAME = re.compile(r"^(\s*)- name:\s*(.+?)\s*$")
_RUN_LINE = re.compile(r"^(\s*)run:\s*(.*)$")
_PIP_INSTALL_EXTRAS = re.compile(r"install\s+'\.\[([^\]]+)\]'")
_SHELLCHECK_PIN = re.compile(r"'shellcheck-py==([0-9][0-9A-Za-z.\-]*)'")
_RUFF_PIN = re.compile(r"'ruff==([0-9][0-9A-Za-z.\-]*)'")
_PYTHON_VERSION = re.compile(r"python-version:\s*'([0-9]+\.[0-9]+)'")
_POSTGRES_IMAGE = re.compile(r"image:\s*(postgres:\S+)")
_POSTGRES_AUTH = re.compile(r"POSTGRES_HOST_AUTH_METHOD:\s*(\S+)")
_POSTGRES_DB = re.compile(r"POSTGRES_DB:\s*(\S+)")


class GateSpecError(RuntimeError):
    """工作流结构变了、本文件的锚点没跟着更新——必须响亮失败，不能退回旧值。"""


def _job_block(text: str, job_name: str) -> str:
    """截出某个顶层 job 的文本块（从 `  <job>:` 到下一个同缩进 job key 之前）。"""

    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if re.match(rf"^  {re.escape(job_name)}:[ \t]*$", line):
            start = index
            break
    if start is None:
        raise GateSpecError(f"找不到顶层 job `{job_name}`：工作流结构可能已经变化")

    end = len(lines)
    for index in range(start + 1, len(lines)):
        if _TOP_LEVEL_JOB_KEY.match(lines[index]):
            end = index
            break
    return "\n".join(lines[start:end])


def _step_block(job_text: str, step_name: str) -> str:
    """截出 job 内某个 step 的文本块（按 `- name:` 的缩进定位同级边界）。"""

    lines = job_text.splitlines()
    start = None
    indent = None
    for index, line in enumerate(lines):
        match = _STEP_NAME.match(line)
        if match and match.group(2).strip("'\"") == step_name:
            start = index
            indent = match.group(1)
            break
    if start is None:
        raise GateSpecError(f"找不到 step `{step_name}`：工作流结构可能已经变化")

    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith(f"{indent}- name:"):
            end = index
            break
    return "\n".join(lines[start:end])


def _run_command_lines(step_text: str) -> list[str]:
    """提取 step 的 `run:` 命令，兼容单行与 `run: |` 块两种写法。"""

    lines = step_text.splitlines()
    for index, line in enumerate(lines):
        match = _RUN_LINE.match(line)
        if not match:
            continue
        indent, rest = match.group(1), match.group(2)
        if rest and rest not in ("|", ">"):
            return [rest.strip()]
        base_indent = len(indent)
        block: list[str] = []
        for later in lines[index + 1 :]:
            if later.strip() == "":
                continue
            later_indent = len(later) - len(later.lstrip(" "))
            if later_indent <= base_indent:
                break
            stripped = later.strip()
            # 跳过 shell 注释行：`run: |` 块里一行注释掉的旧安装命令（例如
            # `# python3 -m pip install '.[old]' 'shellcheck-py==0.9.0'`）
            # 不应该被当成"这一步真正要执行的命令"去解析版本号——独立审查
            # 实测坐实过这个误判（取到了注释里的旧版本，不是真正生效的那行）。
            if stripped.startswith("#"):
                continue
            block.append(stripped)
        if not block:
            raise GateSpecError("`run:` 块写法下没有找到任何命令行")
        return block
    raise GateSpecError("step 里找不到 `run:`")


def _pip_install_extras(command: str) -> list[str]:
    match = _PIP_INSTALL_EXTRAS.search(command)
    if not match:
        raise GateSpecError(f"这行命令不是预期的 `pip install '.[...]'` 形态：{command!r}")
    return [item.strip() for item in match.group(1).split(",") if item.strip()]


def _shellcheck_pin(command: str) -> str | None:
    match = _SHELLCHECK_PIN.search(command)
    return match.group(1) if match else None


def _ruff_pin(command: str) -> str | None:
    match = _RUFF_PIN.search(command)
    return match.group(1) if match else None


def _python_version(job_text: str) -> str:
    match = _PYTHON_VERSION.search(job_text)
    if not match:
        raise GateSpecError("job 里找不到 `python-version: 'X.Y'`")
    return match.group(1)


def _postgres_service(job_text: str) -> dict[str, str]:
    image = _POSTGRES_IMAGE.search(job_text)
    auth = _POSTGRES_AUTH.search(job_text)
    db = _POSTGRES_DB.search(job_text)
    if not (image and auth and db):
        raise GateSpecError(
            "job 里找不到完整的 postgres services 声明（image / "
            "POSTGRES_HOST_AUTH_METHOD / POSTGRES_DB 三者缺一）"
        )
    return {"image": image.group(1), "auth_method": auth.group(1), "db": db.group(1)}


class GateSpec:
    """`Epic Full / gate`（ci.yml）的环境配方。"""

    def __init__(
        self,
        *,
        extras: list[str],
        shellcheck_version: str,
        ruff_version: str,
        python_version: str,
        postgres: dict[str, str],
    ) -> None:
        self.extras = extras
        self.shellcheck_version = shellcheck_version
        self.ruff_version = ruff_version
        self.python_version = python_version
        self.postgres = postgres


class FastSpec:
    """`Story Fast / fast`（story.yml）的环境配方——无真库、无镜像。"""

    def __init__(
        self,
        *,
        extras: list[str],
        shellcheck_version: str,
        ruff_version: str,
        python_version: str,
    ) -> None:
        self.extras = extras
        self.shellcheck_version = shellcheck_version
        self.ruff_version = ruff_version
        self.python_version = python_version


def parse_gate_spec(ci_yml_text: str) -> GateSpec:
    """解析 ci.yml 的 `gate` job：两步 pip install 在同一个环境里累加安装，
    与 gate 真正跑 `verify_repository.sh` 那一刻的已安装 extras 完全一致
    （见该步骤在 job 里的先后顺序：scheduler+migrate 装完先做制品完整性检查，
    再装 worker 做 Agent SDK 冒烟，随后才跑仓库门禁——门禁看到的是三组的并集）。
    """

    job = _job_block(ci_yml_text, "gate")

    install_step = _step_block(job, "安装测试依赖与锁定版本的 shellcheck")
    install_cmd = _run_command_lines(install_step)[0]
    extras = _pip_install_extras(install_cmd)
    shellcheck_version = _shellcheck_pin(install_cmd)
    if shellcheck_version is None:
        raise GateSpecError(f"安装依赖这一步没有锁定 shellcheck-py 版本：{install_cmd!r}")
    ruff_version = _ruff_pin(install_cmd)
    if ruff_version is None:
        raise GateSpecError(f"安装依赖这一步没有锁定 ruff 版本：{install_cmd!r}")

    smoke_step = _step_block(job, "真实 Agent SDK 冒烟（不调模型、不用凭据）")
    smoke_lines = _run_command_lines(smoke_step)
    smoke_install = next((line for line in smoke_lines if "pip install" in line), None)
    if smoke_install is None:
        raise GateSpecError("Agent SDK 冒烟步骤里找不到 pip install 行")
    extras = extras + _pip_install_extras(smoke_install)

    return GateSpec(
        extras=extras,
        shellcheck_version=shellcheck_version,
        ruff_version=ruff_version,
        python_version=_python_version(job),
        postgres=_postgres_service(job),
    )


def parse_fast_spec(story_yml_text: str) -> FastSpec:
    job = _job_block(story_yml_text, "fast")
    install_step = _step_block(job, "安装快速门禁依赖")
    install_cmd = _run_command_lines(install_step)[0]
    extras = _pip_install_extras(install_cmd)
    shellcheck_version = _shellcheck_pin(install_cmd)
    if shellcheck_version is None:
        raise GateSpecError(f"安装依赖这一步没有锁定 shellcheck-py 版本：{install_cmd!r}")
    ruff_version = _ruff_pin(install_cmd)
    if ruff_version is None:
        raise GateSpecError(f"安装依赖这一步没有锁定 ruff 版本：{install_cmd!r}")
    return FastSpec(
        extras=extras,
        shellcheck_version=shellcheck_version,
        ruff_version=ruff_version,
        python_version=_python_version(job),
    )


def load_gate_spec() -> GateSpec:
    return parse_gate_spec(CI_WORKFLOW.read_text(encoding="utf-8"))


def load_fast_spec() -> FastSpec:
    return parse_fast_spec(STORY_WORKFLOW.read_text(encoding="utf-8"))


def _main() -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", choices=["gate", "fast"], help="要现读哪个 job 的环境配方")
    args = parser.parse_args()

    try:
        if args.job == "gate":
            spec = load_gate_spec()
            print(f"EXTRAS={','.join(spec.extras)}")
            print(f"SHELLCHECK_VERSION={spec.shellcheck_version}")
            print(f"RUFF_VERSION={spec.ruff_version}")
            print(f"PYTHON_VERSION={spec.python_version}")
            print(f"POSTGRES_IMAGE={spec.postgres['image']}")
            print(f"POSTGRES_AUTH_METHOD={spec.postgres['auth_method']}")
            print(f"POSTGRES_DB={spec.postgres['db']}")
        else:
            spec = load_fast_spec()
            print(f"EXTRAS={','.join(spec.extras)}")
            print(f"SHELLCHECK_VERSION={spec.shellcheck_version}")
            print(f"RUFF_VERSION={spec.ruff_version}")
            print(f"PYTHON_VERSION={spec.python_version}")
    except GateSpecError as error:
        print(f"gate_spec：解析 {args.job} 的环境配方失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
