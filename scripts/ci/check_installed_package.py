#!/usr/bin/env python3
"""校验**已安装的** lingxi 包完整，而不是 ``src/`` 目录完整。

测试跑的是 ``PYTHONPATH=src``，部署跑的是 ``pip install`` 出来的制品。两者会因为
新增子目录、打包配置变化或 ``__init__.py`` 遗漏而分叉，而分叉在部署时才暴露。

本检查刻意**不做条件跳过**。曾经的写法是「``import lingxi`` 成功才检查」，那样
环境坏掉会表现为静默跳过——一个看起来像通过的跳过，比不检查更危险。这里要么
通过，要么失败。

用法：先安装本包，再在**仓库目录之外**运行本脚本。
"""

from __future__ import annotations

import importlib
import pathlib
import sys

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
)

_INSTALL_MARKERS = ("site-packages", "dist-packages")


def main() -> int:
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

    if failures:
        print("已安装包完整性：不通过", file=sys.stderr)
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        return 1

    print(f"已安装包完整性：{len(REQUIRED_MODULES)} 个模块全部来自已安装的包")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
