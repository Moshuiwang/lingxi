"""``core/admin/registry.py`` 的默认拒绝谓词（Issue #95 S-M-01）。

认领断言：V-管理-22（默认拒绝：不存在的条目、非 active 条目、零角色的 active 条目
三者一律判定为非管理员，且判定结果彼此不可区分——不给探测者可利用的信号）。
"""

from __future__ import annotations

import unittest

from lingxi.core.admin.registry import (
    ALL_ADMIN_ROLES,
    AdminRegistryEntry,
    AdminRole,
    ENTRY_STATUS_ACTIVE,
    ENTRY_STATUS_REVOKED,
    is_authorized_admin,
)


def _entry(
    *, status: str = ENTRY_STATUS_ACTIVE, roles: frozenset[AdminRole] = ALL_ADMIN_ROLES
) -> AdminRegistryEntry:
    return AdminRegistryEntry(
        feishu_open_id="ou_admin", label="delegated_subject", roles=roles, entry_status=status
    )


class DefaultDenyTests(unittest.TestCase):
    def test_none_entry_is_not_authorized(self) -> None:
        """`否定测试`：完全不在登记表中的对象——``None`` 是这条判定唯一能拿到的
        表示，与产品合同"查无此人"同一形态。"""

        self.assertFalse(is_authorized_admin(None))

    def test_revoked_entry_is_not_authorized_even_with_all_roles(self) -> None:
        """已撤销条目即使角色字段仍是 TRUE，也不构成授权——`entry_status` 是
        唯一的门，角色列只在门内才有意义。"""

        entry = _entry(status=ENTRY_STATUS_REVOKED, roles=ALL_ADMIN_ROLES)
        self.assertFalse(is_authorized_admin(entry))

    def test_active_entry_with_zero_roles_is_not_authorized(self) -> None:
        """active 但零角色：结构上允许存在、但没有任何能力，必须与"根本没有这条
        登记"得到同一个判定结果。"""

        entry = _entry(status=ENTRY_STATUS_ACTIVE, roles=frozenset())
        self.assertFalse(is_authorized_admin(entry))

    def test_active_entry_with_any_role_is_authorized(self) -> None:
        for role in AdminRole:
            with self.subTest(role=role):
                entry = _entry(status=ENTRY_STATUS_ACTIVE, roles=frozenset({role}))
                self.assertTrue(is_authorized_admin(entry))

    def test_active_entry_with_all_roles_is_authorized(self) -> None:
        entry = _entry(status=ENTRY_STATUS_ACTIVE, roles=ALL_ADMIN_ROLES)
        self.assertTrue(is_authorized_admin(entry))

    def test_has_role_reads_the_snapshot_only(self) -> None:
        entry = _entry(roles=frozenset({AdminRole.OPS_ADMIN}))
        self.assertTrue(entry.has_role(AdminRole.OPS_ADMIN))
        self.assertFalse(entry.has_role(AdminRole.SUPER_ADMIN))
        self.assertFalse(entry.has_role(AdminRole.PERMISSION_ADMIN))

    def test_all_admin_roles_covers_exactly_three_roles(self) -> None:
        """迁移 0067 恰好三个授予布尔列；这里钉住"三类角色"这个产品事实的数量，
        新增或减少角色必须显式改这条断言，不能悄悄漂移。"""

        self.assertEqual(len(ALL_ADMIN_ROLES), 3)
        self.assertSetEqual(
            {role.value for role in ALL_ADMIN_ROLES},
            {"permission_admin", "ops_admin", "super_admin"},
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
