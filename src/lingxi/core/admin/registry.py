"""管理员角色登记表的判定形状：三类角色、登记条目、默认拒绝谓词。

只有类型与纯函数，没有 I/O——真实读表住在 ``adapters/admin_registry.py``，按
``AdminRegistryEntry`` 的字段形状把一行 ``admin_registry`` 转成这里的值对象。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class AdminRole(str, Enum):
    """产品合同「管理员处理入口与安全确认」定义的三类角色，与迁移 ``0067`` 的三个
    授予布尔列一一对应。取值即数据库列名前缀，避免两处各自维护一份映射。"""

    PERMISSION_ADMIN = "permission_admin"
    OPS_ADMIN = "ops_admin"
    SUPER_ADMIN = "super_admin"


#: 全部角色，供"合并授予"这类需要枚举三者的调用方使用（例如种子命令），不散落
#: 三次字面量。
ALL_ADMIN_ROLES: frozenset[AdminRole] = frozenset(AdminRole)

#: 条目状态的取值域，与迁移 ``0067`` 的 CHECK 逐字一致。集中成常量而不是各处
#: 写字符串字面量，改状态名时只有一处需要同步。
ENTRY_STATUS_ACTIVE = "active"
ENTRY_STATUS_REVOKED = "revoked"


@dataclass(frozen=True)
class AdminRegistryEntry:
    """一条已经从数据库读出的登记表条目。

    ``entry_status`` 与 ``roles`` 都是**判定时刻的快照**——调用方每次都要用一条新读
    构造它，不得跨请求缓存这个对象本身（否则"角色收回后新请求立即拒绝"这条不变量
    在应用层就被打破，即使数据库那一侧已经正确写入了撤销）。
    """

    feishu_open_id: str
    label: str
    roles: frozenset[AdminRole]
    entry_status: str

    @property
    def active(self) -> bool:
        return self.entry_status == ENTRY_STATUS_ACTIVE

    def has_role(self, role: AdminRole) -> bool:
        return role in self.roles


def is_authorized_admin(entry: AdminRegistryEntry | None) -> bool:
    """默认拒绝谓词：条目不存在、非 active，或没有任何已授予角色，一律不是管理员。

    "active 但零角色"是一个刻意允许存在、但**没有任何能力**的状态（例如未来某次
    撤销把三个角色都收回、但登记条目本身还没走到撤销那一步）——它必须和"完全没有
    这条登记"得到同一个判定结果，不能因为行存在就被误当成"有一点权限"。
    """

    return entry is not None and entry.active and bool(entry.roles)
