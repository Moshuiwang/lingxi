#!/usr/bin/env python3
"""校验**已安装的** lingxi 包完整，而不是 ``src/`` 目录完整。

测试跑的是 ``PYTHONPATH=src``，部署跑的是 ``pip install`` 出来的制品。两者会因为
新增子目录、打包配置变化或 ``__init__.py`` 遗漏而分叉，而分叉在部署时才暴露。

本检查刻意**不做条件跳过**。曾经的写法是「``import lingxi`` 成功才检查」，那样
环境坏掉会表现为静默跳过——一个看起来像通过的跳过，比不检查更危险。这里要么
通过，要么失败。

用法：先安装本包，再在**仓库目录之外**运行本脚本。

    python3 check_installed_package.py                      # 制品完整性（全部关键模块）
    python3 check_installed_package.py --process scheduler  # 追加：该进程的运行依赖真的装上了

``--process`` 是 Issue #56 按进程拆 extras 之后加的。**`src/lingxi/` 里没有任何模块级
第三方 import，全部是函数内延迟导入**，所以「进程入口 import 成功」并不能证明它的运行
依赖装上了——只装一个空环境也照样能 import 成功。要让「某个 extra 漏声明依赖」变红，
必须显式导入第三方模块本身，这正是 ``PROCESS_RUNTIME_IMPORTS`` 的第二个元组在做的事。
"""

from __future__ import annotations

import argparse
import importlib
import pathlib
import re
import sys
from importlib.metadata import PackageNotFoundError, metadata

# 逐个 import，缺哪个报哪个，不要笼统失败。
REQUIRED_MODULES = (
    "lingxi.core.ids",
    "lingxi.core.identity.onboarding",
    "lingxi.core.identity.identifiers",
    "lingxi.core.identity.credentials",
    "lingxi.core.identity.org_snapshot",
    "lingxi.core.identity.first_contact",
    "lingxi.core.execution.tool_policy",
    "lingxi.core.execution.audit",
    "lingxi.core.execution.hooks",
    "lingxi.core.execution.message_stream",
    "lingxi.adapters.claude_agent_hooks",
    "lingxi.core.permission.galaxy_export",
    "lingxi.core.permission.galaxy_scope",
    "lingxi.core.permission.account_match",
    "lingxi.core.permission.role_function",
    "lingxi.adapters.galaxy_csv_export",
    "lingxi.adapters.galaxy_import",
    "lingxi.adapters.retention",
    "lingxi.adapters.feishu_roster_bitable",
    "lingxi.adapters.role_function_map_file",
    "lingxi.adapters.feishu_directory",
    "lingxi.adapters.delegated_credentials",
    "lingxi.adapters.postgres_identity",
    "lingxi.adapters.claude_agent_session",
    # apps/ 是新增的顶层子目录：进程入口漏进制品只在部署时暴露（V-部署-10），
    # "测试全绿但 python -m 起不来"正是它的形状（Issue #37 / #16）。
    "lingxi.apps.scheduler",
    "lingxi.apps.worker.cli",
    "lingxi.apps.worker.config",
    "lingxi.apps.worker.turn",
    "lingxi.apps.worker.__main__",
    # S4 前半（#57）新增的 gateway 进程与它的会话领域包。core/conversation/ 是
    # 新的顶层子目录，与 apps/ 当初同一个形状：漏进制品只在部署时暴露。
    "lingxi.core.conversation.commands",
    "lingxi.core.conversation.session_window",
    "lingxi.core.conversation.ports",
    "lingxi.core.conversation.pipeline",
    "lingxi.adapters.feishu_events",
    "lingxi.adapters.feishu_longconn",
    "lingxi.adapters.feishu_outbound",
    "lingxi.adapters.postgres_conversation",
    "lingxi.apps.gateway",
    "lingxi.apps.gateway.config",
    "lingxi.apps.gateway.__main__",
)

# 随包发布的数据文件：模块导入成功不代表数据文件进了 wheel（后者要靠
# pyproject.toml 的 package-data 声明）。缺失时角色职能会整列变成「未映射」。
REQUIRED_PACKAGE_DATA = (("lingxi.config", "galaxy_role_function_map.toml"),)

_INSTALL_MARKERS = ("site-packages", "dist-packages")

# 按进程分组的运行时依赖（Issue #56）。键与 pyproject.toml 的
# ``[project.optional-dependencies]`` 组名一一对应；值是
# （该进程要导入的 lingxi 模块, 该进程运行时真正需要的第三方模块）。
#
# 第三方那一列是从进程入口逐个追 import 链得到的，不是照抄 pyproject——照抄的话
# 这个检查就永远不会红。CI 在**每个 extra 各自的干净环境**里跑对应的一项，
# 见 .github/workflows/ci.yml 的 `CI / extras` 矩阵。
PROCESS_RUNTIME_IMPORTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "scheduler": (
        # 注意导入的是承载 ``main`` 的包，不是 ``lingxi.apps.scheduler.__main__``：
        # 后者在模块级 ``raise SystemExit(main())``（没有 __name__ 卫语句），
        # import 它会真的把续期扫描进程跑起来。
        ("lingxi.apps.scheduler", "lingxi.adapters.delegated_credentials", "lingxi.adapters.retention"),
        ("cryptography.fernet", "psycopg"),
    ),
    "worker": (
        ("lingxi.apps.worker.__main__", "lingxi.apps.worker.cli", "lingxi.adapters.claude_agent_session"),
        ("claude_agent_sdk",),
    ),
    "gateway": (
        (
            # 注意导入的是承载 ``main`` 的包与 ``__main__``：后者带 ``if __name__``
            # 卫语句（与 worker 同惯例），import 它不会真的把长连接跑起来。
            "lingxi.apps.gateway",
            "lingxi.apps.gateway.config",
            "lingxi.apps.gateway.__main__",
            "lingxi.adapters.feishu_events",
            "lingxi.adapters.feishu_longconn",
            "lingxi.adapters.feishu_outbound",
            "lingxi.adapters.postgres_conversation",
        ),
        # websockets 显式列出，尽管 lark-oapi 传递携带它——理由见 pyproject.toml
        # 的 [gateway] 组注释。这里取 ``websockets.exceptions``（lark 实际 import
        # 的那个子模块）而不是顶层包：websockets 15 的顶层做了惰性导入，
        # ``import websockets`` 成功证明不了子模块装全了。
        ("lark_oapi", "psycopg", "websockets.exceptions"),
    ),
    # Bot-Test 受控验证资产（代码框架第五节），不是生产进程；这些模块刻意不在
    # REQUIRED_MODULES 里——那份清单只管正式制品——但它们的依赖同样要能装上，
    # 否则受控验证会在 biai-stage 上才失败。
    "bot-test": (
        (
            "lingxi.adapters.feishu_onboarding",
            "lingxi.adapters.oauth_bridge",
            "lingxi.adapters.refresh_tokens",
            "lingxi.adapters.postgres_onboarding",
        ),
        ("cryptography.fernet", "lark_oapi", "psycopg", "websockets.sync.client"),
    ),
    # 迁移作业（Issue #53）：部署时跑一次 `python -m alembic upgrade head`，不是常驻
    # 进程。**lingxi 模块那一列刻意为空**——迁移工具链不得渗入运行时代码
    # （断言 V-迁移-04：`grep -rn "sqlalchemy\|alembic" src/` 必须为空），
    # 所以这一组没有任何 lingxi 入口，只有第三方那一列要证明装得上。
    #
    # psycopg 与 alembic 并列，不是冗余：alembic 自己不依赖任何驱动，驱动由 URL 的
    # scheme 决定。少了它，`upgrade head` 在干净环境里报 ModuleNotFoundError，而
    # 这条矩阵腿是唯一会在干净环境里跑的检查（外审实测出的缺口）。
    "migrate": ((), ("alembic", "psycopg")),
}


# CI 的 extras 矩阵所在文件。用 ``__file__`` 定位而不是 cwd：本检查刻意在仓库目录
# 之外运行，但它自己始终躺在仓库里，CI 也是按绝对路径调用它的。
CI_WORKFLOW = pathlib.Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"

# 刻意不进 CI 矩阵的组。**目前为空**；往里加必须在注释里写清为什么该组不需要
# 「干净环境里装一次」的证明，否则这就成了漏加矩阵行的后门。
MATRIX_EXEMPT_EXTRAS: frozenset[str] = frozenset()

# 只认单行写法 `extra: [a, b, c]`。改成多行 YAML 列表时这里会找不到而**失败**，
# 不是静默通过——找不到就当作对不上账。
_MATRIX_LINE = re.compile(r"^[ \t]*extra:[ \t]*\[([^\]]*)\]", re.MULTILINE)


def installed_extras() -> set[str] | None:
    """已安装制品声明的 extras；读不到返回 ``None``。"""

    try:
        return set(metadata("lingxi").get_all("Provides-Extra") or [])
    except PackageNotFoundError:
        return None


def ci_matrix_extras(workflow_text: str) -> set[str] | None:
    """从 ci.yml 文本里读出 extras 矩阵；没有那一行返回 ``None``。"""

    match = _MATRIX_LINE.search(workflow_text)
    if match is None:
        return None
    return {item.strip().strip("\"'") for item in match.group(1).split(",") if item.strip()}


def check_ci_matrix(declared: set[str], workflow_text: str | None) -> list[str]:
    """每个 extra 都必须出现在 ci.yml 的 extras 矩阵里。

    只对账「pyproject ↔ 本脚本」这半边是不够的：把新组加进 pyproject 和
    ``PROCESS_RUNTIME_IMPORTS``、唯独漏掉矩阵那一行，CI 就从没在干净环境里装过
    它，而 gate 全绿——独立复查实测出过这个漏洞。这里补上另半边，让代码框架里
    「这条不靠自觉」的说法真正成立。
    """

    if workflow_text is None:
        return [f"{CI_WORKFLOW}：读不到 CI 配置，无法核对 extras 矩阵"]
    matrix = ci_matrix_extras(workflow_text)
    if matrix is None:
        return ["ci.yml：找不到 `extra: [...]` 矩阵行，extras 矩阵无法核对（改了写法就同步本脚本的正则）"]

    failures: list[str] = []
    for name in sorted(declared - matrix - MATRIX_EXEMPT_EXTRAS):
        failures.append(
            f"extra `{name}`：不在 .github/workflows/ci.yml 的 extras 矩阵里，"
            "CI 从没在干净环境里装过它。请加进 `extra: [...]`。"
        )
    for name in sorted(matrix - declared):
        failures.append(
            f"extra `{name}`：ci.yml 矩阵里有，但已安装制品没有声明它，那条矩阵腿必然失败。"
        )
    return failures


def _read_ci_workflow() -> str | None:
    try:
        return CI_WORKFLOW.read_text(encoding="utf-8")
    except OSError:
        return None


def check_declared_extras(declared: set[str]) -> list[str]:
    """已安装制品声明的 extras 必须与 ``PROCESS_RUNTIME_IMPORTS`` 一一对上。

    CI 矩阵和本脚本都是**按名字列举**的：新增一个 extra 却忘了同步，它就悄悄没有
    任何检查覆盖，而且不会有任何东西变红——正是「绿色的测试不等于会变红的测试」。
    这里读的是**已安装制品的元数据**（``Provides-Extra``）而不是 pyproject.toml：
    本检查刻意在仓库目录之外运行，回头读源码树就把这个前提丢了。
    """

    known = set(PROCESS_RUNTIME_IMPORTS)
    failures: list[str] = []
    for name in sorted(declared - known):
        failures.append(
            f"extra `{name}`：pyproject.toml 声明了它，但 PROCESS_RUNTIME_IMPORTS 没有，"
            "于是它的依赖没有任何检查覆盖。请补上本脚本的条目，"
            "并同步加进 .github/workflows/ci.yml 的 extras 矩阵。"
        )
    for name in sorted(known - declared):
        failures.append(
            f"extra `{name}`：PROCESS_RUNTIME_IMPORTS 有它，但已安装制品没有声明。"
            "pyproject.toml 可能把这一组改名或删掉了。"
        )
    return failures


def _check_process(name: str) -> list[str]:
    """校验某个进程 extra 的运行依赖在当前环境里真的可用。"""

    failures: list[str] = []
    lingxi_modules, third_party_modules = PROCESS_RUNTIME_IMPORTS[name]

    for module_name in lingxi_modules:
        try:
            module = importlib.import_module(module_name)
        except Exception as error:  # noqa: BLE001 - 任何导入失败都是制品问题
            failures.append(f"{name} 进程入口 {module_name}：导入失败（{type(error).__name__}: {error}）")
            continue
        location = pathlib.Path(module.__file__ or "")
        if not any(marker in location.parts for marker in _INSTALL_MARKERS):
            failures.append(
                f"{name} 进程入口 {module_name}：来自 {location}，不是已安装的包。"
                "请在仓库目录之外运行本检查。"
            )

    for module_name in third_party_modules:
        try:
            importlib.import_module(module_name)
        except Exception as error:  # noqa: BLE001 - 缺依赖与导入报错都是声明问题
            failures.append(
                f"{name} 运行依赖 {module_name}：导入失败（{type(error).__name__}: {error}）。"
                f"pyproject.toml 的 [{name}] 组可能漏了它。"
            )

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--process",
        choices=sorted(PROCESS_RUNTIME_IMPORTS),
        help="额外校验该进程 extra 的运行依赖已装上；省略时只做制品完整性检查。",
    )
    args = parser.parse_args()

    failures: list[str] = []
    for name in REQUIRED_MODULES:
        try:
            module = importlib.import_module(name)
        except Exception as error:  # noqa: BLE001 - 任何导入失败都是制品问题
            failures.append(f"{name}：导入失败（{type(error).__name__}: {error}）")
            continue

        location = pathlib.Path(module.__file__ or "")
        if not any(marker in location.parts for marker in _INSTALL_MARKERS):
            failures.append(
                f"{name}：来自 {location}，不是已安装的包。"
                "请在仓库目录之外运行本检查，否则它只是又测了一遍源码树。"
            )

    for package_name, file_name in REQUIRED_PACKAGE_DATA:
        try:
            package = importlib.import_module(package_name)
        except Exception as error:  # noqa: BLE001 - 任何导入失败都是制品问题
            failures.append(f"{package_name}：导入失败（{type(error).__name__}: {error}）")
            continue
        data_file = pathlib.Path(package.__file__ or "").parent / file_name
        if not data_file.is_file():
            failures.append(f"{package_name}/{file_name}：数据文件不在已安装的包里")
        elif not any(marker in data_file.parts for marker in _INSTALL_MARKERS):
            failures.append(f"{package_name}/{file_name}：来自 {data_file}，不是已安装的包。")

    # 不受 --process 影响：gate 那一步（不传 --process）也要能发现「新增了 extra
    # 却没人检查它」，否则这个漏洞要等到部署才暴露。三方对账——已安装制品的
    # Provides-Extra、本脚本的 PROCESS_RUNTIME_IMPORTS、ci.yml 的 extras 矩阵——
    # 任意两边对不上都在这里失败。
    declared = installed_extras()
    if declared is None:
        failures.append("lingxi：读不到已安装制品的元数据，无法核对 extras 声明")
    else:
        failures.extend(check_declared_extras(declared))
        failures.extend(check_ci_matrix(declared, _read_ci_workflow()))

    if args.process:
        failures.extend(_check_process(args.process))

    if failures:
        print("已安装包完整性：不通过", file=sys.stderr)
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        return 1

    print(
        f"已安装包完整性：{len(REQUIRED_MODULES)} 个模块与 "
        f"{len(REQUIRED_PACKAGE_DATA)} 个数据文件全部来自已安装的包"
    )
    print(
        f"extras 三方对账：{len(PROCESS_RUNTIME_IMPORTS)} 组"
        f"（{', '.join(sorted(PROCESS_RUNTIME_IMPORTS))}）在制品 Provides-Extra、"
        "本脚本与 ci.yml 矩阵三处一致"
    )
    if args.process:
        lingxi_modules, third_party_modules = PROCESS_RUNTIME_IMPORTS[args.process]
        print(
            f"{args.process} 进程运行依赖：{len(lingxi_modules)} 个进程入口模块与 "
            f"{len(third_party_modules)} 个第三方模块（{', '.join(third_party_modules)}）全部可导入"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
