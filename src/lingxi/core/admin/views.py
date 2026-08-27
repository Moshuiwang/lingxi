"""只读查询命令组的返回形状：与 ``AdminQueries`` 端口签名一起构成契约。

独立成模块（不放进 ``router.py``）是为了让**只需要这些数据形状、不需要命令解析
与路由编排**的调用方——``adapters/admin_registry.py``——不必在 import 时把
``core/admin/commands.py`` 一起拖进自己的闭包。这不是洁癖：`apps/admin_bootstrap`
（随 scheduler 镜像装的一次性种子命令）只需要写入登记表，从不解析或路由任何管理
命令，如果它经由 adapter 间接 import 到 ``commands.py``，scheduler 进程的运行依赖
闭包就会平白多出一段与它实际职责无关的代码路径（`scripts/ci/check_installed_
package.py` 的 `PROCESS_RUNTIME_IMPORTS` 静态闭包检查会如实反映这条多余的边）。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LocalPermissionOverrideView:
    """「查询用户状态」命令回显的一行当前生效本地权限覆盖（#319 S-P-1b 卡 B，
    ``/admin revoke_permission`` 的 UX 前置）：只列 ``entry_status='active'`` 行，
    数据经 ``adapters.postgres_local_permission.
    PostgresLocalPermissionOverrideStore.effective_entries``。

    ``reason`` 是完整原文——是否截断显示是 ``core/admin/router._render_user_status``
    的展示层决定（不回显 reason 全文，截断 20 字），本视图本身只是忠实的 DTO，
    不提前做任何截断，避免把展示细节耦合进数据形状。
    """

    override_id: str
    direction: str
    company_id: str
    metric_name: str
    reason: str
    created_at: str


@dataclass(frozen=True)
class AdminUserStatusView:
    """「查询用户状态」命令的最小必要信息——开通与账号状态，不含花名册资料。

    ``local_overrides``（#319 S-P-1b 卡 B 新增，默认空元组保持既有构造点不用改）
    是该用户当前生效的本地权限覆盖行列表，供 ``/admin revoke_permission`` 的
    UX 前置——管理员需要先看到 override_id 才能发起收回。"""

    identifier: str
    provisioning_state: str
    account_state: str
    permission_version: int
    updated_at: str
    local_overrides: tuple[LocalPermissionOverrideView, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class AdminEventView:
    """「追溯/审计查询」的一行最近事件，字段与 ``apps/trace`` 已经展示的口径一致。"""

    received_at: str
    event_type: str
    handled_as: str | None
    trace_id: str
