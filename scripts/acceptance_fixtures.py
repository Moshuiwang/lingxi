#!/usr/bin/env python3
"""受控触发夹具登记表（Trace v12 机制③：受控触发夹具确定性合同）。

背景：GitHub Issue #147（长期执行计划 v12）在 Epic A / S-A-07 执行复盘后新增
「受控触发夹具确定性合同」——受控失败、安全、降级类旅程必须用**确定性触发机制**
（注入开关、受控配置）而不是模型行为或偶发外部故障来构造，并且要满足五条硬要求：

1. 确定性触发（注入开关 / 受控配置，不依赖模型行为或偶发外部故障）；
2. 默认关闭（不设置该变量时行为与它加入之前逐字节一致）；
3. 失败关闭（非法取值必须在启动期拒绝，不能悄悄放行）；
4. 用后撤除并回读（验收窗口结束后必须清除，且要能验证确实清除了）；
5. 夹具输入契约与生产解析同口径，且夹具自身要有测试。

仓库里已经存在两个满足上述五条、以环境变量驱动的正式夹具：
``LINGXI_GATEWAY_CARD_FAILURE_INJECT``（取值解析在 ``apps/gateway/config.py``，
生效装配在 ``apps/gateway/__init__.py`` 的 ``assemble_delivery_consumer``，注入点
说明在 ``apps/gateway/delivery.py``）与 ``LINGXI_WORKER_OUTPUT_SAFETY_CANARY``
（取值解析在 ``apps/worker/config.py``，启动期显眼告知在 ``apps/worker/cli.py``）。
Epic D 的 #237/#238/#239 模块拆分后取值解析仍留在两个 ``config.py``，但实际生效
与可观测的落点已分散到上述文件，核对开关是否仍在生效时应从这些落点回读。它们各自的
业务分支已经有相当完整的契约测试（默认关闭、四/两档合法值、非法值拒绝、只影响
被选中的一步、不泄露敏感信息），分别在 ``tests/test_gateway_config.py`` /
``tests/test_gateway_delivery.py`` 与 ``tests/test_worker_entry.py``——本模块**不**
重新验证它们的业务效果。

本模块只承担一件此前没有任何地方承担的事：给全部已登记夹具一份**可机器核对**的
登记表，用于：

- 配套的 ``tests/test_acceptance_fixtures_contract.py`` 核对这份登记表与源码
  没有漂移（新增一个跟随既有命名纪律的夹具却忘记登记时，测试变红）；
- Epic E 的 Stage 演练脚本（``scripts/stage_rehearsal_mvp_acceptance.sh``）在
  验收窗口**开窗前**核对没有任何残留夹具变量、**收窗后**核对全部夹具变量确已
  撤除——即第 4 条硬要求「用后撤除并回读」的可执行落点。

**「用后撤除并回读」要查三面，不是一面**（2026-08-18 编排者修复包 F2）：夹具在
真实 Stage 环境是通过 ``deploy/.env.stage.gateway`` / ``.worker-queue`` 这类 env
文件注入**容器**的，不是注入执行本脚本的宿主 shell 进程。只查 ``os.environ``
（``--check-clear``）查的是错的环境——宿主 shell 干净，不代表某份 env 文件里忘记
删掉的一行、或某个仍在运行的容器进程环境里没被拿掉。因此本模块提供三个独立的
检查入口，Stage 演练脚本三个都要跑：

- ``--check-clear``：宿主 shell 当前进程环境（``os.environ``）；
- ``--check-files <path>...``：一个或多个 env 文件（`.env.stage.*` 这类），或
  ``-`` 表示从标准输入读一段 ``KEY=VALUE`` 文本（用于接 ``docker compose exec
  <service> env`` 的输出，检查**容器内**实际生效的环境）。

本模块不导入任何业务模块、不连数据库、不发起任何网络调用，可以在没有装好任何
可选依赖的干净环境里直接运行。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPOSITORY_ROOT / "src" / "lingxi"


@dataclass(frozen=True)
class Fixture:
    """一个受控触发夹具的登记条目。"""

    env_var: str
    """完整环境变量名（含前缀），验收窗口结束后必须从进程环境中撤除。"""

    owner_module: str
    """实现该夹具的模块（供人核对来源，本模块运行期不 import 它）。"""

    description: str
    """一句话说明它触发什么、默认状态是什么。"""

    contract_tests: str
    """已经覆盖它业务分支的既有契约测试文件（逗号分隔），本模块不重复这些测试。"""


FIXTURES: tuple[Fixture, ...] = (
    Fixture(
        env_var="LINGXI_GATEWAY_CARD_FAILURE_INJECT",
        owner_module="lingxi.apps.gateway.config",
        description=(
            "飞书卡片建卡/流式更新/关闭三步降级注入（合法值 create/update/close/all）；"
            "默认未设置=不注入，装配路径与夹具加入之前逐字节一致。"
        ),
        contract_tests="tests/test_gateway_config.py, tests/test_gateway_delivery.py",
    ),
    Fixture(
        env_var="LINGXI_WORKER_OUTPUT_SAFETY_CANARY",
        owner_module="lingxi.apps.worker.config",
        description=(
            "输出安全 masked/withheld 两档确定性注入（须同时配置合成 system prompt）；"
            "默认未设置=不注入，正文按真实模型输出正常走出口检查。"
        ),
        contract_tests="tests/test_worker_entry.py",
    ),
)

FIXTURE_ENV_VARS: tuple[str, ...] = tuple(fixture.env_var for fixture in FIXTURES)


def active_fixtures(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """返回给定环境映射（默认 ``os.environ``，即**当前宿主 shell 进程**）中当前
    被设置的已登记夹具及其取值。

    空字符串或纯空白值按未设置处理，与各夹具自己的 ``_text()`` 解析口径一致
    （见 ``apps/gateway/config.py`` / ``apps/worker/config.py`` 的 ``_text``）。

    **这个函数只看得到调用它的进程自己的环境**，看不到某份 env 文件的内容，也
    看不到某个正在运行的容器内部的环境——那两处分别用
    :func:`active_fixtures_in_file` 与 :func:`active_fixtures_in_text` 检查。
    """

    source: Mapping[str, str] = os.environ if env is None else env
    result: dict[str, str] = {}
    for var in FIXTURE_ENV_VARS:
        raw = source.get(var)
        if raw is None:
            continue
        stripped = raw.strip()
        if stripped:
            result[var] = stripped
    return result


def assert_no_active_fixtures(env: Mapping[str, str] | None = None) -> None:
    """硬要求第 4 条在宿主 shell 这一面的可执行落点：确认没有任何已登记夹具残留。

    只覆盖宿主 shell 进程环境；env 文件与容器内环境请分别用
    :func:`active_fixtures_in_file` / :func:`active_fixtures_in_text` 检查——三面
    都要查，见模块文档「用后撤除并回读要查三面」一节。
    """

    leaked = active_fixtures(env)
    if leaked:
        names = "、".join(sorted(leaked))
        raise RuntimeError(
            "以下受控触发夹具变量仍设置在宿主 shell 环境中，验收窗口开始前或结束后"
            f"必须清除（不回显具体取值；env 文件与容器内环境须另行核对）：{names}"
        )


# ---------------------------------------------------------------------------
# env 文件 / 容器环境文本解析（F2：夹具经 env 文件注入容器，不是注入宿主 shell）
# ---------------------------------------------------------------------------

_ENV_LINE_PATTERN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _parse_env_text(text: str) -> dict[str, str]:
    """按 ``KEY=VALUE`` 逐行解析环境变量文本。

    兼容两种来源：docker compose 的 ``.env`` 文件（允许 ``export `` 前缀、``#``
    开头整行注释、空行）与 ``docker compose exec <service> env`` / ``printenv``
    的输出（无注释，每行就是 ``NAME=VALUE``）。**不做 shell 语法级别的完整
    解析**（不处理跨行值、变量展开、反斜杠转义）——这两种来源都用不到这些能力，
    多做只会把"看不懂的行"悄悄吞掉、制造假阴性。取值两端若恰好是一对匹配的
    引号（单或双），去掉这对引号；不匹配的引号原样保留（宁可少解析，不猜）。
    """

    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_LINE_PATTERN.match(line)
        if not match:
            continue
        name, value = match.group(1), match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[name] = value
    return result


def active_fixtures_in_text(text: str) -> dict[str, str]:
    """在一段 ``KEY=VALUE`` 文本（env 文件内容，或 ``env``/``printenv`` 输出）中
    查找已登记夹具。空值/纯空白值按未设置处理，口径与 :func:`active_fixtures`
    一致。
    """

    parsed = _parse_env_text(text)
    result: dict[str, str] = {}
    for var in FIXTURE_ENV_VARS:
        raw = parsed.get(var)
        if raw is None:
            continue
        stripped = raw.strip()
        if stripped:
            result[var] = stripped
    return result


def active_fixtures_in_file(path: Path) -> dict[str, str]:
    """:func:`active_fixtures_in_text` 的文件版本。

    文件不存在时返回空字典（按"这个来源没有查到残留"处理，不是"确认干净"——
    文件本该存在却不存在，是 Stage 演练脚本 preflight 步骤单独核对的事，不是本
    函数的职责）。
    """

    if not path.is_file():
        return {}
    return active_fixtures_in_text(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 登记表与源码的漂移检测
# ---------------------------------------------------------------------------

# 只识别本仓库已经确立的**一种**夹具命名形态：调用方写成
# ``_text(env, "...INJECT...")`` / ``"...CANARY..."``，且同一文件里存在顶层
# ``ENV_PREFIX = "..."`` 常量——当前 gateway/worker 两处配置模块都是这个形状。
#
# **已知盲区（2026-08-18 编排者修复包 P2-12 明确记录，不是新发现才补的免责声明）**：
# 任何用别的写法读取环境变量的夹具都会被漏掉，例如：
# - 把完整变量名当成一个字符串字面量直接读，不经过 ``_text()`` + ``ENV_PREFIX``
#   拼接（形如 ``os.environ.get("LINGXI_SCHEDULER_SOMETHING_INJECT")``）；
# - 变量名不含 ``INJECT`` / ``CANARY`` 词根；
# - 所在文件没有顶层 ``ENV_PREFIX`` 常量（不同的配置装配风格）。
#
# 这**不是**通用的秘密/配置扫描器，只给当前两个夹具的登记表提供"没有漂移"这一条
# 具体保证。新增夹具如果沿用现有两处的写法会被自动发现并要求登记；沿用其他写法
# 则不会被发现，需要作者自己手工登记，并在 PR 里说明为什么扫描器找不到它——不能
# 把"扫描器没报警"当成"没有新夹具"的证据。
_ENV_PREFIX_PATTERN = re.compile(r'^ENV_PREFIX\s*=\s*"([A-Z0-9_]+)"', re.MULTILINE)
_INJECTION_SUFFIX_PATTERN = re.compile(
    r'_text\(\s*env,\s*"([A-Z0-9_]*(?:INJECT|CANARY)[A-Z0-9_]*)"\s*\)'
)


def discover_fixture_env_vars_in_source(src_root: Path | None = None) -> set[str]:
    """静态扫描 ``src/lingxi`` 下形如 ``_text(env, "...INJECT...")`` /
    ``"...CANARY..."`` 的调用，按同文件的 ``ENV_PREFIX`` 拼出完整变量名。

    返回值应当与 :data:`FIXTURE_ENV_VARS` 集合相等；配套测试断言这一点。**只覆盖
    上面登记的这一种命名形态**，见本节文档「已知盲区」。
    """

    root = SRC_ROOT if src_root is None else src_root
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        prefix_match = _ENV_PREFIX_PATTERN.search(text)
        if prefix_match is None:
            continue
        prefix = prefix_match.group(1)
        for suffix_match in _INJECTION_SUFFIX_PATTERN.finditer(text):
            found.add(prefix + suffix_match.group(1))
    return found


# ---------------------------------------------------------------------------
# CLI：供人和 Stage 演练脚本调用
# ---------------------------------------------------------------------------


def _cli_list() -> int:
    for fixture in FIXTURES:
        print(f"{fixture.env_var}\t{fixture.description}")
    return 0


def _cli_check_clear() -> int:
    leaked = active_fixtures()
    if leaked:
        for name in sorted(leaked):
            print(f"残留：{name}（未回显取值；来源：宿主 shell 进程环境）", file=sys.stderr)
        return 1
    print("受控触发夹具环境变量：宿主 shell 进程环境全部未设置（未检查 env 文件与容器）。")
    return 0


def _cli_check_files(raw_paths: list[str]) -> int:
    any_leak = False
    for raw_path in raw_paths:
        if raw_path == "-":
            source_label = "<stdin>"
            leaked = active_fixtures_in_text(sys.stdin.read())
        else:
            path = Path(raw_path)
            source_label = str(path)
            if not path.is_file():
                print(f"跳过（文件不存在，未纳入本次检查）：{source_label}", file=sys.stderr)
                continue
            leaked = active_fixtures_in_file(path)
        if leaked:
            any_leak = True
            for name in sorted(leaked):
                print(f"残留：{name}（未回显取值；来源：{source_label}）", file=sys.stderr)
    if any_leak:
        return 1
    print("受控触发夹具环境变量：指定来源中均未查到残留。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "受控触发夹具登记表：列出已登记夹具，或检查某一面环境是否干净"
            "（宿主 shell / env 文件 / 容器 env 输出，三面分别检查）。"
        )
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--list", action="store_true", help="列出全部已登记夹具及一句话说明"
    )
    group.add_argument(
        "--check-clear",
        action="store_true",
        help="确认宿主 shell 当前进程环境中没有任何已登记夹具变量残留（不检查 env 文件或容器）",
    )
    group.add_argument(
        "--check-files",
        nargs="+",
        metavar="PATH",
        help=(
            "确认一个或多个 env 文件、或 docker compose exec <service> env 的输出"
            "（传 - 从标准输入读取）中没有残留；不存在的路径按未纳入检查处理，不判失败"
        ),
    )
    args = parser.parse_args(argv)
    if args.list:
        return _cli_list()
    if args.check_files:
        return _cli_check_files(args.check_files)
    return _cli_check_clear()


if __name__ == "__main__":
    raise SystemExit(main())
