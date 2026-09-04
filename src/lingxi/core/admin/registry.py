"""管理员角色登记表的判定形状：三类角色、登记条目、默认拒绝谓词。

只有类型与纯函数，没有 I/O——真实读表住在 ``adapters/admin_registry.py``，按
``AdminRegistryEntry`` 的字段形状把一行 ``admin_registry`` 转成这里的值对象。

## 已知边界（不是缺陷，是当前迁移阶段的现状）

- **运行时 DSN 具备登记表写能力**：运行时进程尚未以限权角色连库，结构上具备
  写入 `admin_registry` 的数据库权限，只是代码从不发起写查询；表级只读角色
  隔离是未来加固项。
- **普通已开通用户发 `/admin` 走业务路径，不做内容嗅探**：分流点只在专用主体
  命中或未开通状态时才会触达，判定只发生在分流点本身，不对已经确定走业务
  路径的消息做二次内容嗅探。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class AdminRole(str, Enum):
    """产品合同「管理员处理入口与安全确认」定义的三类角色。

    与迁移 ``0067`` 的三个授予布尔列一一对应。取值即数据库列名前缀，避免两处
    各自维护一份映射。
    """

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
        """条目状态是否为 ``active``。"""
        return self.entry_status == ENTRY_STATUS_ACTIVE

    def has_role(self, role: AdminRole) -> bool:
        """条目是否被授予了这一类角色。"""
        return role in self.roles


class AdminRegistrySeedConflictError(RuntimeError):
    """种子写入检测到已存在一条 ``active`` 登记，但与本次意图播种的内容不一致。

    定义在这个纯类型模块而不是 ``adapters/admin_registry.py``，是为了让
    ``apps/admin_bootstrap`` 能在**不引入 psycopg 依赖链**的情况下拿到这个
    类型去写 ``except``——它对协作对象一律走延迟 import（供测试注入假实现）。

    ``mismatched_fields`` 只列出不一致的字段名，不带任何取到的值——尤其不带
    ``feishu_open_id`` 或其他可能泄露身份的内容；调用方据此响亮报告"哪类字段
    不一致"，而不是把一次真正的不一致误报成一次安静的幂等成功。
    """

    def __init__(self, *, mismatched_fields: tuple[str, ...]) -> None:
        """记录种子写入与已有登记不一致的字段名，供调用方响亮报告。"""
        self.mismatched_fields = mismatched_fields
        super().__init__(
            "已存在一条 active 登记，但与本次意图播种的内容不一致，"
            "不一致的字段：" + "、".join(mismatched_fields)
        )


def is_authorized_admin(entry: AdminRegistryEntry | None) -> bool:
    """默认拒绝谓词：条目不存在、非 active，或三类角色没有全部授予，一律不是管理员。

    授权要求**三类角色全真**（三类角色合并授予）：``entry.roles`` 必须逐一
    等于 :data:`ALL_ADMIN_ROLES` 这个全集，"只授予了部分角色"（结构上不该
    出现——数据库的 ``CHECK`` 已经挡住这类写入；这里是应用层的第二道防线）
    与"一个角色都没有"得到同一个判定结果，都不是有效管理员，不能因为行存在、
    或恰好授予了某一两个角色就被误当成"有一点权限"——MVP 没有这个概念。
    """
    return entry is not None and entry.active and entry.roles == ALL_ADMIN_ROLES
