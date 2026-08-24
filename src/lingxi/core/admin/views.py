"""只读查询命令组的返回形状：与 ``AdminQueries`` 端口签名一起构成契约。

独立成模块（不放进 ``router.py``）是为了让**只需要这两个数据形状、不需要命令解析
与路由编排**的调用方——``adapters/admin_registry.py``——不必在 import 时把
``core/admin/commands.py`` 一起拖进自己的闭包。这不是洁癖：`apps/admin_bootstrap`
（随 scheduler 镜像装的一次性种子命令）只需要写入登记表，从不解析或路由任何管理
命令，如果它经由 adapter 间接 import 到 ``commands.py``，scheduler 进程的运行依赖
闭包就会平白多出一段与它实际职责无关的代码路径（`scripts/ci/check_installed_
package.py` 的 `PROCESS_RUNTIME_IMPORTS` 静态闭包检查会如实反映这条多余的边）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AdminUserStatusView:
    """「查询用户状态」命令的最小必要信息——开通与账号状态，不含权限记录细节、
    不含花名册资料。"""

    identifier: str
    provisioning_state: str
    account_state: str
    permission_version: int
    updated_at: str


@dataclass(frozen=True)
class AdminEventView:
    """「追溯/审计查询」的一行最近事件，字段与 ``apps/trace`` 已经展示的口径一致。"""

    received_at: str
    event_type: str
    handled_as: str | None
    trace_id: str
