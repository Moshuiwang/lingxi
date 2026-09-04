"""``core/permission/local_override.py`` 的纯逻辑测试（Issue #319 S-P-1a）。

不需要数据库；真库断言（迁移 ``0072`` 的约束、适配器读写）见
``tests/test_local_permission_postgres.py``。

两处变异锚点已实测验红、验证后原样还原（S-P-1a 实施卡要求，结果登记在此，
不重复登记在别处）：

1. **去掉 direction 校验**：临时把 ``LocalPermissionOverrideEntry.__post_init__``
   里 ``isinstance(self.direction, OverrideDirection)`` 那一条判断删掉后，
   ``DirectionValidationTests.test_raw_string_direction_is_rejected`` 由绿转红
   （不再抛 ``ValueError``）；改回后复绿。
2. **把 suppress 赢改成 grant 赢**：临时把 :func:`resolve_local_overrides` 里
   ``grants -= suppressions`` 改成反方向的 ``suppressions -= grants``
   （即改为 grant 覆盖 suppress）后，
   ``ConflictResolutionTests.test_suppress_wins_over_grant_for_the_same_key``
   由绿转红（``resolved.grants`` 不再为空、``resolved.suppressions`` 变空）；
   改回后复绿。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from lingxi.core.permission.local_override import (
    LocalPermissionOverrideEntry,
    OverrideDirection,
    ResolvedLocalOverrides,
    audit_fields,
    resolve_local_overrides,
    to_company_metric_map,
)

_NOW = datetime(2026, 8, 27, 3, 0, 0, tzinfo=UTC)


def _entry(
    *,
    user_id: str = "usr_1",
    direction: OverrideDirection = OverrideDirection.GRANT,
    company_id: str = "1011",
    metric_name: str = "日活",
    reason: str = "U1 后台管理员角色无业务职能映射，特批",
    initiated_by_open_id: str = "ou_admin",
    pending_action_id: str = "pac_1",
    created_at: datetime = _NOW,
) -> LocalPermissionOverrideEntry:
    return LocalPermissionOverrideEntry(
        user_id=user_id,
        direction=direction,
        company_id=company_id,
        metric_name=metric_name,
        reason=reason,
        initiated_by_open_id=initiated_by_open_id,
        pending_action_id=pending_action_id,
        created_at=created_at,
    )


class EntryConstructionTests(unittest.TestCase):
    def test_valid_entry_constructs_and_exposes_key(self) -> None:
        entry = _entry()

        self.assertEqual(entry.key, ("1011", "日活"))
        self.assertIs(entry.direction, OverrideDirection.GRANT)

    def test_blank_fields_are_rejected(self) -> None:
        blank_field_cases = {
            "user_id": "",
            "company_id": "   ",
            "metric_name": "",
            "reason": "  ",
            "initiated_by_open_id": "",
            "pending_action_id": "   ",
        }
        for field_name, blank_value in blank_field_cases.items():
            with self.subTest(field=field_name):
                kwargs = {field_name: blank_value}
                with self.assertRaises(ValueError):
                    _entry(**kwargs)

    def test_naive_created_at_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _entry(created_at=datetime(2026, 8, 27, 3, 0, 0))  # no tzinfo


class DirectionValidationTests(unittest.TestCase):
    """否定断言：``direction`` 必须是 :class:`OverrideDirection` 枚举实例，不接受
    一个恰好取值相等的裸字符串（变异锚点①，见模块文档）。"""

    def test_raw_string_direction_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            LocalPermissionOverrideEntry(
                user_id="usr_1",
                direction="grant",  # type: ignore[arg-type]
                company_id="1011",
                metric_name="日活",
                reason="x",
                initiated_by_open_id="ou_admin",
                pending_action_id="pac_1",
                created_at=_NOW,
            )

    def test_suppress_enum_member_is_accepted(self) -> None:
        entry = _entry(direction=OverrideDirection.SUPPRESS)

        self.assertIs(entry.direction, OverrideDirection.SUPPRESS)


class ResolveLocalOverridesTests(unittest.TestCase):
    def test_empty_entries_resolve_to_empty_sets(self) -> None:
        resolved = resolve_local_overrides(user_id="usr_1", entries=())

        self.assertEqual(resolved.grants, frozenset())
        self.assertEqual(resolved.suppressions, frozenset())

    def test_blank_user_id_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            resolve_local_overrides(user_id="   ", entries=())

    def test_mismatched_entry_user_id_is_rejected(self) -> None:
        """否定断言：混入别的用户的条目必须响亮失败，不能被默默接受——聚合必须
        按用户隔离，跨用户混入是权限串扰，比"忘了传"更危险（模块文档）。"""

        entries = [_entry(user_id="usr_2")]

        with self.assertRaises(ValueError):
            resolve_local_overrides(user_id="usr_1", entries=entries)

    def test_duplicate_identical_grants_collapse_to_one(self) -> None:
        entries = [_entry(), _entry()]

        resolved = resolve_local_overrides(user_id="usr_1", entries=entries)

        self.assertEqual(resolved.grants, frozenset({("1011", "日活")}))

    def test_grants_and_suppressions_on_different_keys_are_independent(self) -> None:
        entries = [
            _entry(company_id="1011", metric_name="日活", direction=OverrideDirection.GRANT),
            _entry(company_id="1012", metric_name="收入", direction=OverrideDirection.SUPPRESS),
        ]

        resolved = resolve_local_overrides(user_id="usr_1", entries=entries)

        self.assertEqual(resolved.grants, frozenset({("1011", "日活")}))
        self.assertEqual(resolved.suppressions, frozenset({("1012", "收入")}))


class ConflictResolutionTests(unittest.TestCase):
    """否定断言：同一 (公司, 指标) 键同时有生效 grant 与 suppress 时，suppress 赢
    （变异锚点②，见模块文档）。"""

    def test_suppress_wins_over_grant_for_the_same_key(self) -> None:
        entries = [
            _entry(company_id="1011", metric_name="日活", direction=OverrideDirection.GRANT),
            _entry(company_id="1011", metric_name="日活", direction=OverrideDirection.SUPPRESS),
        ]

        resolved = resolve_local_overrides(user_id="usr_1", entries=entries)

        self.assertEqual(resolved.grants, frozenset())
        self.assertEqual(resolved.suppressions, frozenset({("1011", "日活")}))

    def test_suppress_wins_regardless_of_input_order(self) -> None:
        entries = [
            _entry(company_id="1011", metric_name="日活", direction=OverrideDirection.SUPPRESS),
            _entry(company_id="1011", metric_name="日活", direction=OverrideDirection.GRANT),
        ]

        resolved = resolve_local_overrides(user_id="usr_1", entries=entries)

        self.assertEqual(resolved.grants, frozenset())
        self.assertEqual(resolved.suppressions, frozenset({("1011", "日活")}))


class ResolvedLocalOverridesInvariantTests(unittest.TestCase):
    """纵深防线：即使绕开 :func:`resolve_local_overrides` 直接构造，重叠的
    grants/suppressions 仍然被 ``__post_init__`` 拒绝。"""

    def test_overlapping_sets_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ResolvedLocalOverrides(
                grants=frozenset({("1011", "日活")}),
                suppressions=frozenset({("1011", "日活")}),
            )


class ToCompanyMetricMapTests(unittest.TestCase):
    def test_empty_pairs_produce_empty_map(self) -> None:
        self.assertEqual(to_company_metric_map(frozenset()), {})

    def test_pairs_are_grouped_and_sorted_per_company(self) -> None:
        pairs = frozenset(
            {("1011", "收入"), ("1011", "日活"), ("1012", "留存")}
        )

        result = to_company_metric_map(pairs)

        self.assertEqual(result, {"1011": ("收入", "日活"), "1012": ("留存",)})


class AuditFieldsTests(unittest.TestCase):
    def test_all_fields_are_present_and_unredacted(self) -> None:
        entry = _entry(direction=OverrideDirection.SUPPRESS)

        fields = audit_fields(entry)

        self.assertEqual(
            fields,
            {
                "user_id": "usr_1",
                "direction": "suppress",
                "company_id": "1011",
                "metric_name": "日活",
                "reason": "U1 后台管理员角色无业务职能映射，特批",
                "initiated_by_open_id": "ou_admin",
                "pending_action_id": "pac_1",
                "created_at": _NOW.isoformat(),
            },
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
