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
    python3 check_installed_package.py --source-only        # 只核对源码清单（本地仓库门禁）

``--process`` 是 Issue #56 按进程拆 extras 之后加的。**`src/lingxi/` 里没有任何模块级
第三方 import，全部是函数内延迟导入**，所以「进程入口 import 成功」并不能证明它的运行
依赖装上了——只装一个空环境也照样能 import 成功。要让「某个 extra 漏声明依赖」变红，
必须显式导入第三方模块本身，这正是 ``PROCESS_RUNTIME_IMPORTS`` 的第二个元组在做的事。
"""

from __future__ import annotations

import argparse
import ast
import importlib
import importlib.util
import pathlib
import re
import sys
from collections.abc import Iterable, Mapping
from importlib.metadata import PackageNotFoundError, metadata

# 逐个检查，缺哪个报哪个，不要笼统失败。
REQUIRED_MODULES = (
    # 包初始化文件也属于制品：它们决定 Python 的包边界，不能因为多数为空就从
    # 制品清单里隐身。`scheduler.__main__` 是可执行入口，不能直接 import，下面的
    # `_installed_module_location` 会只取它的已安装文件位置，不启动续期进程。
    "lingxi",
    "lingxi.adapters",
    "lingxi.apps",
    "lingxi.apps.worker",
    "lingxi.config",
    "lingxi.config.content",
    "lingxi.core",
    "lingxi.core.alerting",
    "lingxi.core.conversation",
    "lingxi.core.execution",
    "lingxi.core.identity",
    "lingxi.core.permission",
    "lingxi.core.ids",
    "lingxi.core.identity.onboarding",
    "lingxi.core.identity.identifiers",
    "lingxi.core.identity.credentials",
    "lingxi.core.identity.org_snapshot",
    "lingxi.core.identity.first_contact",
    "lingxi.core.execution.tool_policy",
    "lingxi.core.execution.audit",
    "lingxi.core.execution.hooks",
    "lingxi.core.execution.input_safety",
    "lingxi.core.execution.message_stream",
    "lingxi.core.execution.card_stream",
    "lingxi.adapters.claude_agent_hooks",
    "lingxi.core.permission.galaxy_export",
    "lingxi.core.permission.galaxy_scope",
    "lingxi.core.permission.account_match",
    "lingxi.core.permission.role_function",
    "lingxi.adapters.galaxy_csv_export",
    "lingxi.adapters.galaxy_import",
    "lingxi.adapters.retention",
    "lingxi.adapters.feishu_roster_bitable",
    "lingxi.adapters.feishu_reauthorization",
    # 花名册审计日报（Issue #52）：比对与渲染在 core，基线读取与群发在 adapters。
    # 四个都要在制品里能 import——它们由 lingxi-scheduler 在运行时按需加载，
    # "本地测试全绿但 wheel 里没有这个模块"正是 V-部署-10 要挡的形状。
    "lingxi.core.identity.roster_audit",
    "lingxi.core.identity.roster_report",
    "lingxi.adapters.postgres_roster_audit",
    "lingxi.adapters.feishu_group_message",
    "lingxi.adapters.role_function_map_file",
    "lingxi.adapters.feishu_directory",
    "lingxi.adapters.delegated_credentials",
    "lingxi.adapters.oauth_bridge_client",
    "lingxi.adapters.postgres",
    "lingxi.adapters.postgres_identity",
    "lingxi.adapters.claude_agent_session",
    # apps/ 是新增的顶层子目录：进程入口漏进制品只在部署时暴露（V-部署-10），
    # "测试全绿但 python -m 起不来"正是它的形状（Issue #37 / #16）。
    "lingxi.apps.scheduler",
    "lingxi.apps.scheduler.__main__",
    # 正式重授权是 scheduler 镜像里的**一次性**运维 job；scripts/ 被 .dockerignore
    # 排除，若这里漏掉 apps/reauthorize，源码测试仍会绿而部署 job 会在镜像内消失。
    "lingxi.apps.reauthorize",
    "lingxi.apps.reauthorize.__main__",
    "lingxi.apps.worker.cli",
    "lingxi.apps.worker.config",
    "lingxi.apps.worker.report",
    "lingxi.apps.worker.turn",
    "lingxi.apps.worker.delivery",
    "lingxi.apps.worker.service",
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

# 源码树里仍保留的 Bot-Test / 历史受控验证资产。它们不是正式用户路径的漏项：
# `bot-test` extra 会单独校验它们，但正式制品关键模块清单必须明确写出不纳入的理由。
# 这张表是固定政策，不是任意模块的逃生口；`check_module_manifests` 会对它逐项核对。
MODULE_MANIFEST_EXEMPTIONS: dict[str, str] = {
    "lingxi.adapters.feishu_bitable_association": "Bot-Test 历史测试资产，不纳入正式用户路径清单",
    "lingxi.adapters.feishu_onboarding": "Bot-Test 受控验证资产，仅由 bot-test 进程加载",
    "lingxi.adapters.oauth_bridge": "Bot-Test 受控验证资产，仅由 bot-test 进程加载",
    "lingxi.adapters.postgres_onboarding": "Bot-Test 受控验证资产，仅由 bot-test 进程加载",
    "lingxi.adapters.refresh_tokens": "Bot-Test 受控验证资产，仅由 bot-test 进程加载",
}

# 这是与上面实际登记表**独立维护**的批准快照。键集和理由全文都故意重复写在这里，
# 不能从 `MODULE_MANIFEST_EXEMPTIONS` 派生；否则新增/改写源码登记会把“漂移检查”变成
# 同一字面量的自引用，永远不会变红。正式变更豁免时必须同时审查并更新这两份冻结数据。
_FROZEN_MODULE_MANIFEST_EXEMPTION_KEYS = frozenset(
    {
        "lingxi.adapters.feishu_bitable_association",
        "lingxi.adapters.feishu_onboarding",
        "lingxi.adapters.oauth_bridge",
        "lingxi.adapters.postgres_onboarding",
        "lingxi.adapters.refresh_tokens",
    }
)
_FROZEN_MODULE_MANIFEST_EXEMPTION_REASONS: dict[str, str] = {
    "lingxi.adapters.feishu_bitable_association": "Bot-Test 历史测试资产，不纳入正式用户路径清单",
    "lingxi.adapters.feishu_onboarding": "Bot-Test 受控验证资产，仅由 bot-test 进程加载",
    "lingxi.adapters.oauth_bridge": "Bot-Test 受控验证资产，仅由 bot-test 进程加载",
    "lingxi.adapters.postgres_onboarding": "Bot-Test 受控验证资产，仅由 bot-test 进程加载",
    "lingxi.adapters.refresh_tokens": "Bot-Test 受控验证资产，仅由 bot-test 进程加载",
}

# 这个文件没有 `if __name__ == "__main__"` 保护，直接 import 会启动常驻 scheduler。
# 它仍必须出现在制品清单和 scheduler 的静态依赖闭包里，只是运行时检查改用 find_spec。
_NON_IMPORTABLE_MODULES = frozenset({"lingxi.apps.scheduler.__main__"})

# 随包发布的数据文件：模块导入成功不代表数据文件进了 wheel（后者要靠
# pyproject.toml 的 package-data 声明）。缺失时角色职能会整列变成「未映射」，或让
# 正式用户路径在部署后失去版本化内容目录。
REQUIRED_PACKAGE_DATA = (
    ("lingxi.config", "galaxy_role_function_map.toml"),
    ("lingxi.config", "content.toml"),
)

_INSTALL_MARKERS = ("site-packages", "dist-packages")

# 按进程分组的运行时依赖（Issue #56）。键与 pyproject.toml 的
# ``[project.optional-dependencies]`` 组名一一对应；值是
# （该进程要导入的 lingxi 模块, 该进程运行时真正需要的第三方模块）。
#
# 第三方那一列是从进程入口逐个追 import 链得到的，不是照抄 pyproject——照抄的话
# 这个检查就永远不会红。CI 在**每个 extra 各自的干净环境**里跑对应的一项，
# 见 .github/workflows/ci.yml 的 `Epic Full / extras` 矩阵。
PROCESS_RUNTIME_IMPORTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "scheduler": (
        # `scheduler.__main__` 在模块级 `raise SystemExit(main())`（没有 __name__ 卫语句），
        # 运行时检查只用 find_spec 定位它；静态闭包仍必须把这个真正的启动入口登记。
        # 花名册审计日报（#52）的两个 adapter 由 `build_loop` **在函数内** import。
        # 函数内 import 意味着"进程能起来"证明不了"这两个模块装得上"——正是 #29 之后
        # 建立的防漂移机制在这里的缺口：不列进来，extras 那条干净环境的腿永远不会红。
        (
            "lingxi.apps.scheduler",
            "lingxi.apps.scheduler.__main__",
            "lingxi.config.content",
            "lingxi.adapters.delegated_credentials",
            "lingxi.adapters.feishu_directory",
            "lingxi.adapters.retention",
            "lingxi.adapters.feishu_group_message",
            "lingxi.adapters.feishu_roster_bitable",
            "lingxi.adapters.postgres_roster_audit",
            "lingxi.adapters.postgres",
            "lingxi.core.identity.credentials",
            "lingxi.core.identity.identifiers",
            "lingxi.core.identity.roster_audit",
            "lingxi.core.identity.roster_report",
            "lingxi.core.alerting",
            "lingxi.core.ids",
        ),
        # reauthorize 复用 scheduler 镜像；Bridge 的 WebSocket 依赖也必须在该制品中
        # 显式可导入，虽然常驻 scheduler 入口本身不建立 Bridge 连接。
        ("cryptography.fernet", "psycopg", "websockets.sync.client"),
    ),
    "reauthorize": (
        (
            "lingxi.apps.reauthorize",
            "lingxi.apps.reauthorize.__main__",
            "lingxi.adapters.delegated_credentials",
            "lingxi.adapters.feishu_directory",
            "lingxi.adapters.feishu_reauthorization",
            "lingxi.adapters.oauth_bridge_client",
            "lingxi.adapters.postgres",
            "lingxi.core.identity.credentials",
            "lingxi.core.identity.identifiers",
            "lingxi.core.ids",
        ),
        ("cryptography.fernet", "psycopg", "websockets.sync.client"),
    ),
    "worker": (
        (
            "lingxi.apps.worker.__main__",
            "lingxi.apps.worker.cli",
            "lingxi.apps.worker.config",
            "lingxi.apps.worker.report",
            "lingxi.apps.worker.turn",
            "lingxi.apps.worker.delivery",
            "lingxi.apps.worker.service",
            "lingxi.adapters.claude_agent_hooks",
            "lingxi.adapters.claude_agent_session",
            "lingxi.adapters.postgres",
            "lingxi.adapters.postgres_conversation",
            "lingxi.config.content",
            "lingxi.core.conversation.ports",
            "lingxi.core.execution.audit",
            "lingxi.core.execution.card_stream",
            "lingxi.core.execution.hooks",
            "lingxi.core.execution.input_safety",
            "lingxi.core.execution.message_stream",
            "lingxi.core.execution.tool_policy",
            "lingxi.core.ids",
        ),
        ("claude_agent_sdk", "psycopg"),
    ),
    "gateway": (
        (
            # 注意导入的是承载 ``main`` 的包与 ``__main__``：后者带 ``if __name__``
            # 卫语句（与 worker 同惯例），import 它不会真的把长连接跑起来。
            "lingxi.apps.gateway",
            "lingxi.apps.gateway.config",
            "lingxi.apps.gateway.__main__",
            "lingxi.config.content",
            "lingxi.adapters.feishu_events",
            "lingxi.adapters.feishu_longconn",
            "lingxi.adapters.feishu_outbound",
            "lingxi.adapters.postgres_conversation",
            "lingxi.adapters.postgres",
            "lingxi.core.conversation.commands",
            "lingxi.core.conversation.pipeline",
            "lingxi.core.conversation.ports",
            "lingxi.core.conversation.session_window",
            "lingxi.core.ids",
        ),
        # websockets 显式列出，尽管 lark-oapi 传递携带它——理由见 pyproject.toml
        # 的 [gateway] 组注释。这里取 ``websockets.exceptions``（lark 实际 import
        # 的那个子模块）而不是顶层包：websockets 15 的顶层做了惰性导入，
        # ``import websockets`` 成功证明不了子模块装全了。
        ("lark_oapi", "psycopg", "websockets.exceptions"),
    ),
    # Bot-Test 受控验证资产（代码框架第五节），不是生产进程；四个 adapter 走显式
    # 制品豁免，但它们依赖的正式 core.identity.onboarding 仍属于正式制品清单。
    "bot-test": (
        (
            "lingxi.adapters.feishu_onboarding",
            "lingxi.adapters.oauth_bridge",
            "lingxi.adapters.oauth_bridge_client",
            "lingxi.adapters.refresh_tokens",
            "lingxi.adapters.postgres_onboarding",
            "lingxi.adapters.postgres",
            "lingxi.core.identity.onboarding",
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

# 每个 extra 的源码入口。下面的静态闭包会遍历函数体内的延迟 import，反向证明
# PROCESS_RUNTIME_IMPORTS 没有漏掉某个实际会被该进程加载的 lingxi 模块。
PROCESS_SOURCE_ENTRY_POINTS: dict[str, tuple[str, ...]] = {
    "scheduler": ("lingxi.apps.scheduler", "lingxi.apps.scheduler.__main__"),
    "reauthorize": ("lingxi.apps.reauthorize.__main__",),
    "worker": ("lingxi.apps.worker.__main__",),
    "gateway": ("lingxi.apps.gateway", "lingxi.apps.gateway.__main__"),
    "bot-test": (
        "lingxi.adapters.feishu_onboarding",
        "lingxi.adapters.oauth_bridge",
        "lingxi.adapters.refresh_tokens",
        "lingxi.adapters.postgres_onboarding",
    ),
    # 迁移作业运行 alembic，不加载任何 lingxi 模块；这是显式边界，不是漏登记。
    "migrate": (),
}

PROCESS_ENTRY_EXEMPTIONS: dict[str, str] = {
    "migrate": "迁移作业只运行 alembic upgrade，不绑定 lingxi 运行时模块",
}
# 与 `PROCESS_ENTRY_EXEMPTIONS` 分开冻结，防止迁移边界理由被改写后仍与自身副本相等。
_FROZEN_PROCESS_ENTRY_EXEMPTION_KEYS = frozenset({"migrate"})
_FROZEN_PROCESS_ENTRY_EXEMPTION_REASONS: dict[str, str] = {
    "migrate": "迁移作业只运行 alembic upgrade，不绑定 lingxi 运行时模块",
}


# CI 的 extras 矩阵所在文件。用 ``__file__`` 定位而不是 cwd：本检查刻意在仓库目录
# 之外运行，但它自己始终躺在仓库里，CI 也是按绝对路径调用它的。
CI_WORKFLOW = pathlib.Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
SOURCE_ROOT = pathlib.Path(__file__).resolve().parents[2] / "src" / "lingxi"

# 刻意不进 CI 矩阵的组。**目前为空**；往里加必须在注释里写清为什么该组不需要
# 「干净环境里装一次」的证明，否则这就成了漏加矩阵行的后门。
MATRIX_EXEMPT_EXTRAS: frozenset[str] = frozenset()

# 只认单行写法 `extra: [a, b, c]`。改成多行 YAML 列表时这里会找不到而**失败**，
# 不是静默通过——找不到就当作对不上账。
_MATRIX_LINE = re.compile(r"^[ \t]*extra:[ \t]*\[([^\]]*)\]", re.MULTILINE)


def source_module_files(source_root: pathlib.Path | None = None) -> dict[str, pathlib.Path]:
    """返回 `src/lingxi/` 中每个 Python 模块的源码文件。

    包初始化文件用包名表示（例如 `__init__.py` → `lingxi.apps`），因为它们同样
    决定制品的可导入边界。清单完整性只从这个反向枚举得到基准，不从手工清单反推
    源码，所以清单少一项时一定能被发现。
    """

    root = SOURCE_ROOT if source_root is None else source_root
    if not root.is_dir():
        return {}

    found: dict[str, pathlib.Path] = {}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        parts = list(relative.parts)
        if parts[-1] == "__init__.py":
            parts = parts[:-1]
        else:
            parts[-1] = parts[-1][: -len(".py")]
        module = ".".join(["lingxi", *parts])
        found[module] = path
    return found


def source_module_names(source_root: pathlib.Path | None = None) -> set[str]:
    """返回源码树实际存在的模块名集合，供门禁和白盒测试共同使用。"""

    return set(source_module_files(source_root))


# 兼容门禁脚本中“枚举模块”的自然读法；实现只保留一份。
iter_source_modules = source_module_names


def _resolve_source_module(target: str, source_files: Mapping[str, pathlib.Path]) -> str | None:
    """把 `from lingxi.x import Symbol` 的目标归一成实际源码模块名。"""

    if not target.startswith("lingxi"):
        return None
    parts = target.split(".")
    for end in range(len(parts), 0, -1):
        candidate = ".".join(parts[:end])
        if candidate in source_files:
            return candidate
    return None


def _source_imports(module: str, source_files: Mapping[str, pathlib.Path]) -> set[str]:
    """读取一个源码模块的 lingxi import，包含函数体和相对 import。"""

    path = source_files[module]
    package = module if path.name == "__init__.py" else module.rsplit(".", 1)[0]
    found: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def add_target(target: str) -> None:
        resolved = _resolve_source_module(target, source_files)
        if resolved is not None:
            found.add(resolved)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add_target(alias.name)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue

        if node.level:
            base = package.split(".")
            anchor = base[: len(base) - node.level + 1]
            prefix = ".".join(anchor)
            base_target = ".".join(part for part in (prefix, node.module or "") if part)
            if base_target:
                add_target(base_target)
            for alias in node.names:
                if base_target:
                    add_target(f"{base_target}.{alias.name}")
                elif prefix:
                    add_target(f"{prefix}.{alias.name}")
            continue

        if node.module:
            add_target(node.module)
            for alias in node.names:
                add_target(f"{node.module}.{alias.name}")

    return found


def process_source_closure(
    extra: str, source_files: Mapping[str, pathlib.Path] | None = None
) -> set[str]:
    """计算一个进程入口实际会加载的 lingxi 模块闭包。"""

    files = source_module_files() if source_files is None else source_files
    roots = PROCESS_SOURCE_ENTRY_POINTS[extra]
    seen: set[str] = set()
    pending = list(roots)
    while pending:
        module = pending.pop()
        if module in seen or module not in files:
            continue
        seen.add(module)
        pending.extend(_source_imports(module, files) - seen)
    return seen


def check_module_manifests(
    *,
    source_modules: set[str] | None = None,
    required_modules: Iterable[str] | None = None,
    process_runtime_imports: Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]] | None = None,
    exemptions: Mapping[str, str] | None = None,
) -> list[str]:
    """反向核对制品清单、进程模块清单与源码实际模块。

    `REQUIRED_MODULES` 负责正式制品；`PROCESS_RUNTIME_IMPORTS` 第一列负责每个进程的
    lingxi import 闭包；源码中仅保留的 Bot-Test 资产走显式豁免。三者都从源码反向
    检查，清单漏项、陈旧项、错误豁免和进程闭包漏项都会返回失败，而不是静默通过。
    """

    files = source_module_files()
    actual_source = set(files) if source_modules is None else set(source_modules)
    required = tuple(REQUIRED_MODULES if required_modules is None else required_modules)
    process = (
        PROCESS_RUNTIME_IMPORTS if process_runtime_imports is None else process_runtime_imports
    )
    actual_exemptions = dict(
        MODULE_MANIFEST_EXEMPTIONS if exemptions is None else exemptions
    )
    failures: list[str] = []

    if not files:
        failures.append(f"{SOURCE_ROOT}：找不到 `src/lingxi/` 或其中没有 Python 模块")
        return failures

    if len(required) != len(set(required)):
        failures.append("REQUIRED_MODULES：存在重复登记，清单必须逐项且唯一")
    frozen_exemption_names = set(_FROZEN_MODULE_MANIFEST_EXEMPTION_KEYS)
    frozen_exemption_reasons = _FROZEN_MODULE_MANIFEST_EXEMPTION_REASONS
    actual_exemption_names = set(actual_exemptions)
    if set(frozen_exemption_reasons) != frozen_exemption_names:
        failures.append("模块豁免冻结键集与冻结理由全文不一致，冻结清单本身需要修复。")
    for name in sorted(actual_exemption_names - frozen_exemption_names):
        failures.append(
            f"豁免 `{name}`：不是已批准的模块豁免；不能用错误豁免掩盖制品清单漏项。"
        )
    for name in sorted(frozen_exemption_names - actual_exemption_names):
        failures.append(f"豁免 `{name}`：已批准但未登记，必须保留可审查理由。")
    for name, reason in sorted(actual_exemptions.items()):
        if not isinstance(reason, str) or not reason.strip():
            failures.append(f"豁免 `{name}`：缺少理由，不能静默忽略源码模块。")
        if name not in actual_source:
            failures.append(f"豁免 `{name}`：源码中不存在，豁免登记已陈旧。")
        elif name in frozen_exemption_names and reason != frozen_exemption_reasons.get(name):
            failures.append(f"豁免 `{name}`：理由与已批准政策不一致，不能借改名扩大豁免范围。")

    required_set = set(required)
    for name in sorted(actual_source - actual_exemption_names - required_set):
        failures.append(
            f"模块 `{name}`：存在于 src/lingxi/，但未登记进 REQUIRED_MODULES，"
            "也没有有效的显式豁免。"
        )
    for name in sorted(required_set - actual_source):
        failures.append(f"REQUIRED_MODULES：登记了不存在的模块 `{name}`。")
    for name in sorted(required_set & actual_exemption_names):
        failures.append(
            f"模块 `{name}`：同时出现在 REQUIRED_MODULES 和豁免表，归类矛盾；"
            "请保留正式制品登记或明确的资产豁免。"
        )

    expected_processes = set(PROCESS_SOURCE_ENTRY_POINTS)
    actual_processes = set(process)
    for extra in sorted(expected_processes - actual_processes):
        failures.append(f"PROCESS_RUNTIME_IMPORTS：缺少进程 `{extra}` 的模块清单。")
    for extra in sorted(actual_processes - expected_processes):
        failures.append(f"PROCESS_RUNTIME_IMPORTS：登记了未知进程 `{extra}`。")

    actual_entry_exemptions = PROCESS_ENTRY_EXEMPTIONS
    frozen_entry_names = set(_FROZEN_PROCESS_ENTRY_EXEMPTION_KEYS)
    if (
        set(_FROZEN_PROCESS_ENTRY_EXEMPTION_REASONS) != frozen_entry_names
        or set(actual_entry_exemptions) != frozen_entry_names
        or any(
            actual_entry_exemptions.get(name) != _FROZEN_PROCESS_ENTRY_EXEMPTION_REASONS.get(name)
            for name in frozen_entry_names
        )
    ):
        failures.append("PROCESS_ENTRY_EXEMPTIONS：进程入口豁免发生漂移，必须保留迁移边界理由。")

    for extra in sorted(expected_processes & actual_processes):
        lingxi_modules, _third_party_modules = process[extra]
        listed = tuple(lingxi_modules)
        listed_set = set(listed)
        if len(listed) != len(listed_set):
            failures.append(f"进程 `{extra}`：PROCESS_RUNTIME_IMPORTS 第一列存在重复模块。")

        for name in sorted(listed_set - actual_source):
            failures.append(f"进程 `{extra}`：登记了不存在的模块 `{name}`。")

        try:
            expected = process_source_closure(extra, files)
        except (OSError, SyntaxError) as error:
            failures.append(f"进程 `{extra}`：无法解析源码 import 闭包（{type(error).__name__}: {error}）。")
            expected = set()

        for name in sorted(expected - listed_set):
            failures.append(
                f"进程 `{extra}`：源码 import 闭包使用 `{name}`，但未登记进"
                " PROCESS_RUNTIME_IMPORTS 第一列。"
            )
        for name in sorted(listed_set - expected):
            failures.append(
                f"进程 `{extra}`：登记了 `{name}`，但它不在该进程的源码 import 闭包中；"
                "请移除陈旧项或补充真实入口。"
            )

        if expected and extra in PROCESS_ENTRY_EXEMPTIONS:
            failures.append(
                f"进程 `{extra}`：存在实际 lingxi import 闭包，却登记为入口豁免。"
            )
        if not expected and extra not in PROCESS_ENTRY_EXEMPTIONS:
            failures.append(
                f"进程 `{extra}`：没有模块但缺少显式 PROCESS_ENTRY_EXEMPTIONS 理由。"
            )

        for name in sorted(listed_set):
            if name in actual_exemption_names and extra != "bot-test":
                failures.append(
                    f"进程 `{extra}`：模块 `{name}` 是正式制品豁免，不能被正式进程依赖。"
                )
            elif name not in required_set and name not in actual_exemption_names:
                failures.append(
                    f"进程 `{extra}`：模块 `{name}` 不在 REQUIRED_MODULES，也不在有效豁免中。"
                )

    return failures


def _print_module_manifest_summary() -> None:
    """打印可回读的模块总数、两份清单和全部豁免。"""

    source = source_module_names()
    print(f"模块清单完整性：{len(source)} 个 src/lingxi 模块")
    print(f"  - 正式制品清单（{len(REQUIRED_MODULES)}）：{', '.join(REQUIRED_MODULES)}")
    for extra in sorted(PROCESS_RUNTIME_IMPORTS):
        lingxi_modules, _third_party_modules = PROCESS_RUNTIME_IMPORTS[extra]
        print(f"  - 进程 `{extra}` 清单（{len(lingxi_modules)}）：{', '.join(lingxi_modules) or '（无）'}")
    print(
        f"  - 制品显式豁免（{len(MODULE_MANIFEST_EXEMPTIONS)}）："
        + ", ".join(
            f"{name}（{reason}）" for name, reason in sorted(MODULE_MANIFEST_EXEMPTIONS.items())
        )
    )
    print(
        "  - 进程入口显式豁免："
        + ", ".join(
            f"{name}（{reason}）"
            for name, reason in sorted(PROCESS_ENTRY_EXEMPTIONS.items())
        )
    )


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


def _installed_module_location(module_name: str) -> pathlib.Path:
    """返回已安装模块文件位置；对可执行入口避免执行模块代码。"""

    if module_name in _NON_IMPORTABLE_MODULES:
        spec = importlib.util.find_spec(module_name)
        if spec is None or not spec.origin or spec.origin == "built-in":
            raise ImportError(f"找不到可执行入口的安装文件（{module_name}）")
        return pathlib.Path(spec.origin)

    module = importlib.import_module(module_name)
    return pathlib.Path(module.__file__ or "")


def _check_process(name: str) -> list[str]:
    """校验某个进程 extra 的运行依赖在当前环境里真的可用。"""

    failures: list[str] = []
    lingxi_modules, third_party_modules = PROCESS_RUNTIME_IMPORTS[name]

    for module_name in lingxi_modules:
        try:
            location = _installed_module_location(module_name)
        except Exception as error:  # noqa: BLE001 - 任何导入失败都是制品问题
            failures.append(f"{name} 进程入口 {module_name}：导入失败（{type(error).__name__}: {error}）")
            continue
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
    parser.add_argument(
        "--source-only",
        action="store_true",
        help="只运行源码模块清单反向对账，供 verify_repository.sh 在未安装制品的工作树中使用。",
    )
    args = parser.parse_args()

    if args.source_only and args.process:
        parser.error("--source-only 不能与 --process 同时使用")

    failures: list[str] = check_module_manifests()
    if args.source_only:
        if failures:
            print("模块清单完整性：不通过", file=sys.stderr)
            for line in failures:
                print(f"  - {line}", file=sys.stderr)
            return 1
        _print_module_manifest_summary()
        return 0

    for name in REQUIRED_MODULES:
        try:
            location = _installed_module_location(name)
        except Exception as error:  # noqa: BLE001 - 任何导入失败都是制品问题
            failures.append(f"{name}：导入失败（{type(error).__name__}: {error}）")
            continue

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

    _print_module_manifest_summary()
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
