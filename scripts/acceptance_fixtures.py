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
``LINGXI_GATEWAY_CARD_FAILURE_INJECT``（``apps/gateway/config.py``）与
``LINGXI_WORKER_OUTPUT_SAFETY_CANARY``（``apps/worker/config.py``）。它们各自的
业务分支已经有相当完整的契约测试（默认关闭、四/两档合法值、非法值拒绝、只影响
被选中的一步、不泄露敏感信息），分别在 ``tests/test_gateway_config.py`` /
``tests/test_gateway_delivery.py`` 与 ``tests/test_worker_entry.py``——本模块**不**
重新验证它们的业务效果。

本模块只承担一件此前没有任何地方承担的事：给全部已登记夹具一份**可机器核对**的
登记表，用于：

- 配套的 ``tests/test_acceptance_fixtures_contract.py`` 核对这份登记表与源码
  没有漂移（新增一个跟随既有命名纪律的夹具却忘记登记时，测试变红）；
- Epic E 的 Stage 演练脚本（``scripts/stage_rehearsal_epic_e.sh``）在验收窗口
  **开窗前**核对当前环境没有任何残留夹具变量、**收窗后**核对全部夹具变量确已
  撤除——即第 4 条硬要求「用后撤除并回读」的可执行落点。

本模块不导入任何业务模块、不连数据库、不发起任何网络调用，可以在没有装好任何
可选依赖的干净环境里直接运行。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

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
    """返回给定环境（默认 ``os.environ``）中当前被设置的已登记夹具及其取值。

    空字符串或纯空白值按未设置处理，与各夹具自己的 ``_text()`` 解析口径一致
    （见 ``apps/gateway/config.py`` / ``apps/worker/config.py`` 的 ``_text``）。
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
    """硬要求第 4 条的可执行落点：确认没有任何已登记夹具残留。

    验收窗口开始前用于确认"上一轮没有人忘记清理"，结束后用于确认"这一轮清理
    干净了"。两处都应该调用同一份实现，不要各自重新判断一遍。
    """

    leaked = active_fixtures(env)
    if leaked:
        names = "、".join(sorted(leaked))
        raise RuntimeError(
            "以下受控触发夹具变量仍设置在环境中，验收窗口开始前或结束后必须清除"
            f"（不回显具体取值）：{names}"
        )


# ---------------------------------------------------------------------------
# 登记表与源码的漂移检测
# ---------------------------------------------------------------------------

# 只识别本仓库已经确立的夹具命名纪律：以 ``_INJECT`` 或 ``_CANARY`` 结尾（或
# 内含该词根）的变量名后缀，通过各文件自带的 ``ENV_PREFIX`` 拼成完整变量名。
# 这不是通用的秘密/配置扫描器，只用于防止 FIXTURES 登记表与源码静默漂移——
# 新增一个跟随该纪律命名的夹具却忘记登记时，配套测试会变红并提示补登记。
_ENV_PREFIX_PATTERN = re.compile(r'^ENV_PREFIX\s*=\s*"([A-Z0-9_]+)"', re.MULTILINE)
_INJECTION_SUFFIX_PATTERN = re.compile(
    r'_text\(\s*env,\s*"([A-Z0-9_]*(?:INJECT|CANARY)[A-Z0-9_]*)"\s*\)'
)


def discover_fixture_env_vars_in_source(src_root: Path | None = None) -> set[str]:
    """静态扫描 ``src/lingxi`` 下形如 ``_text(env, "...INJECT...")`` /
    ``"...CANARY..."`` 的调用，按同文件的 ``ENV_PREFIX`` 拼出完整变量名。

    返回值应当与 :data:`FIXTURE_ENV_VARS` 集合相等；配套测试断言这一点。
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
            print(f"残留：{name}（未回显取值）", file=sys.stderr)
        return 1
    print("受控触发夹具环境变量：全部未设置。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="受控触发夹具登记表：列出已登记夹具，或检查当前进程环境是否干净。"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--list", action="store_true", help="列出全部已登记夹具及一句话说明"
    )
    group.add_argument(
        "--check-clear",
        action="store_true",
        help="确认当前进程环境中没有任何已登记夹具变量残留；有残留时退出码非零",
    )
    args = parser.parse_args(argv)
    if args.list:
        return _cli_list()
    return _cli_check_clear()


if __name__ == "__main__":
    raise SystemExit(main())
