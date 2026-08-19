"""组织快照读取编排的形状测试（Issue #250）。

**假的是 FeishuDirectoryClient，不是飞书。** 这里只证明
``read_org_snapshot`` 如实组装两条身份路径各自看到的内容，包括看漏、看少的情况——
批次是否可以整体提交由 ``core/identity/org_snapshot.verify_batch`` 判定，本文件不
重复那一层的用例（见 ``tests/test_identity_org_snapshot.py``）。真实调用属 L4a。
"""

from __future__ import annotations

import unittest

from lingxi.adapters.feishu_org_snapshot_reader import (
    MAX_DEPARTMENTS_PER_TENANT,
    OrgSnapshotReadError,
    read_org_snapshot,
)
from lingxi.core.identity.org_snapshot import require_complete_batch, verify_batch, IntegrityProblem


class FakeDirectoryClient:
    """按 (租户, 部门 ID) 建索引的假客户端，不发起任何网络请求。"""

    def __init__(
        self,
        *,
        app_tenants: list[dict[str, object]],
        user_tenants: list[dict[str, object]],
        # tenant_key -> department_id -> (departments, members)
        app_scope: dict[str, dict[str, tuple[list[dict], list[dict]]]] | None = None,
        user_scope: dict[str, dict[object, tuple[list[dict], list[dict]]]] | None = None,
    ) -> None:
        self._app_tenants = app_tenants
        self._user_tenants = user_tenants
        self._app_scope = app_scope or {}
        self._user_scope = user_scope or {}
        self.app_calls: list[tuple[str, str]] = []
        self.user_calls: list[tuple[str, object]] = []

    def list_collaboration_tenants_as_app(self, *, token: str) -> list[dict[str, object]]:
        assert token == "app-token"
        return self._app_tenants

    def list_collaboration_tenants(self, *, token: str) -> list[dict[str, object]]:
        assert token == "user-token"
        return self._user_tenants

    def list_share_entities(self, *, token: str, tenant_key: str, department_id: str):
        assert token == "app-token"
        self.app_calls.append((tenant_key, department_id))
        return self._app_scope[tenant_key].get(department_id, ([], []))

    def list_visible_organization(
        self, *, token: str, tenant_key: str, department_id: str | None = None, department_id_type: str | None = None
    ):
        assert token == "user-token"
        key = None if department_id is None else (department_id, department_id_type)
        self.user_calls.append((tenant_key, key))
        return self._user_scope[tenant_key].get(key, ([], []))


def _member(open_id: str, name: str = "某人") -> dict[str, object]:
    return {"open_id": open_id, "user_id": f"u_{open_id}", "union_id": f"on_{open_id}", "name": name}


class HappyPathTest(unittest.TestCase):
    def test_a_single_tenant_with_matching_paths_produces_a_committable_batch(self) -> None:
        client = FakeDirectoryClient(
            app_tenants=[{"tenant_key": "tenant_a"}],
            user_tenants=[{"tenant_key": "tenant_a"}],
            app_scope={
                "tenant_a": {
                    "0": (
                        [{"open_department_id": "0"}, {"open_department_id": "od_1"}],
                        [{"open_user_id": "ou_1"}],
                    ),
                    "od_1": ([], []),
                }
            },
            user_scope={
                "tenant_a": {
                    None: (
                        [{"open_department_id": "od_1", "name": "研发部"}],
                        [],
                    ),
                    ("od_1", "open_department_id"): ([], [_member("ou_1", "张一")]),
                }
            },
        )

        batch = read_org_snapshot(client=client, app_token="app-token", user_token="user-token")

        report = require_complete_batch(batch)
        self.assertEqual(report.tenant_count, 1)
        self.assertEqual(report.member_count, 1)
        self.assertEqual(batch.members[0].open_id, "ou_1")
        self.assertEqual(batch.members[0].department_names, ("研发部",))

    def test_the_pseudo_root_record_is_excluded_from_the_app_walk(self) -> None:
        """根部门的伪根记录必须被排除，否则会被误当成子部门重新入队（死循环风险）。"""

        client = FakeDirectoryClient(
            app_tenants=[{"tenant_key": "tenant_a"}],
            user_tenants=[{"tenant_key": "tenant_a"}],
            app_scope={"tenant_a": {"0": ([{"open_department_id": "0"}], [{"open_user_id": "ou_1"}])}},
            user_scope={"tenant_a": {None: ([], [_member("ou_1")])}},
        )

        read_org_snapshot(client=client, app_token="app-token", user_token="user-token")

        # 只有根部门被访问过一次，伪根没有触发第二次对 "0" 的重复请求或死循环。
        self.assertEqual(client.app_calls, [("tenant_a", "0")])

    def test_a_department_shared_by_two_parents_yields_exactly_one_row(self) -> None:
        """一个部门可能同时是两个父部门的子部门（共享组织节点），因此会在两次不同
        层级的响应里各出现一次。写库那一侧对 (sync_run_id, tenant_key,
        department_key) 有唯一约束，本层必须去重成一行，否则整轮提交会在写库阶段
        失败——这是本模块"如实组装"的例外：**行identity 去重**不是替调用方猜内容，
        只是不把同一件事实说两遍。"""

        client = FakeDirectoryClient(
            app_tenants=[{"tenant_key": "tenant_a"}],
            user_tenants=[{"tenant_key": "tenant_a"}],
            app_scope={"tenant_a": {"0": ([], [{"open_user_id": "ou_1"}])}},
            user_scope={
                "tenant_a": {
                    None: (
                        [
                            {"open_department_id": "od_parent_1", "name": "父部门一"},
                            {"open_department_id": "od_parent_2", "name": "父部门二"},
                        ],
                        [],
                    ),
                    ("od_parent_1", "open_department_id"): (
                        [{"open_department_id": "od_shared", "name": "共享部门"}],
                        [],
                    ),
                    ("od_parent_2", "open_department_id"): (
                        [{"open_department_id": "od_shared", "name": "共享部门"}],
                        [_member("ou_1")],
                    ),
                    ("od_shared", "open_department_id"): ([], []),
                }
            },
        )

        batch = read_org_snapshot(client=client, app_token="app-token", user_token="user-token")

        shared_rows = [d for d in batch.departments if d.department_key == "od_shared"]
        self.assertEqual(len(shared_rows), 1, "同一个 department_key 只能落一行，否则违反数据库唯一约束")

    def test_tenant_discovery_covers_a_tenant_only_visible_to_the_app_identity(self) -> None:
        """租户发现遍历两条身份路径的并集（Issue #250 编排者复查 F1 修复后的行为）：
        一个只有应用身份能看到的租户仍会出现在结果里，标记为用户身份不可见。"""

        client = FakeDirectoryClient(
            app_tenants=[{"tenant_key": "tenant_a"}, {"tenant_key": "tenant_b"}],
            user_tenants=[{"tenant_key": "tenant_a"}],
            app_scope={
                "tenant_a": {"0": ([], [{"open_user_id": "ou_1"}])},
                "tenant_b": {"0": ([], [{"open_user_id": "ou_2"}])},
            },
            user_scope={"tenant_a": {None: ([], [_member("ou_1")])}},
        )

        batch = read_org_snapshot(client=client, app_token="app-token", user_token="user-token")

        self.assertEqual({scope.tenant_key for scope in batch.tenants}, {"tenant_a", "tenant_b"})
        tenant_b = next(scope for scope in batch.tenants if scope.tenant_key == "tenant_b")
        self.assertFalse(tenant_b.visible_to_user_identity)


class IntegrityHandoffTest(unittest.TestCase):
    """本模块不做完整性判断，只如实组装——这里验证"如实"这件事：读漏了就该被下一层挡住。"""

    def test_an_empty_app_tenant_list_still_surfaces_a_user_only_tenant_and_gets_rejected(self) -> None:
        """F1 变异锚点：应用身份路径完全看不到任何租户（例如批次半页 / 权限暂时
        收窄），但用户身份路径看到了 tenant_a。此前的实现只遍历
        ``app_tenant_keys``，tenant_a 会被整个排除在 ``SnapshotBatch`` 之外——
        路径不一致判据根本没有机会检查它，本用例只是恰好因为"应用路径连一个租户
        都没有"而顺带触发 NO_TENANT；真正危险的不对称场景（应用路径还有*别的*
        租户、只漏看这一个）见
        ``TenantDiscoveryMissAppSideTest.test_a_tenant_only_visible_to_the_user_identity_is_not_silently_dropped``，
        那一条才是能证明"批次原本会被判完整"的红线。"""

        client = FakeDirectoryClient(
            app_tenants=[],
            user_tenants=[{"tenant_key": "tenant_a"}],
            user_scope={"tenant_a": {None: ([], [_member("ou_1")])}},
        )

        batch = read_org_snapshot(client=client, app_token="app-token", user_token="user-token")

        self.assertEqual({scope.tenant_key for scope in batch.tenants}, {"tenant_a"})
        tenant_a = batch.tenants[0]
        self.assertEqual(tenant_a.app_member_keys, frozenset())
        self.assertEqual(tenant_a.user_member_keys, frozenset({"ou_1"}))
        report = verify_batch(batch)
        self.assertIn(IntegrityProblem.PATH_SETS_DIFFER, report.problems)
        with self.assertRaises(Exception):
            require_complete_batch(batch)

    def test_paths_seeing_different_member_sets_are_recorded_as_is_not_reconciled(self) -> None:
        """两条路径看到的成员集合不一致时，本模块**不**替调用方猜一个"应该对"的值——
        原样交给下一层的 PATH_SETS_DIFFER 挡住（2026-07-28 实况问题的正式校验）。"""

        client = FakeDirectoryClient(
            app_tenants=[{"tenant_key": "tenant_a"}],
            user_tenants=[{"tenant_key": "tenant_a"}],
            app_scope={"tenant_a": {"0": ([], [{"open_user_id": "ou_1"}, {"open_user_id": "ou_2"}])}},
            user_scope={"tenant_a": {None: ([], [_member("ou_1")])}},
        )

        batch = read_org_snapshot(client=client, app_token="app-token", user_token="user-token")

        report = verify_batch(batch)
        self.assertIn(IntegrityProblem.PATH_SETS_DIFFER, report.problems)

    def test_a_tenant_invisible_to_user_identity_is_recorded_as_such(self) -> None:
        client = FakeDirectoryClient(
            app_tenants=[{"tenant_key": "tenant_a"}],
            user_tenants=[],
            app_scope={"tenant_a": {"0": ([], [{"open_user_id": "ou_1"}])}},
        )

        batch = read_org_snapshot(client=client, app_token="app-token", user_token="user-token")

        self.assertFalse(batch.tenants[0].visible_to_user_identity)
        report = verify_batch(batch)
        self.assertIn(IntegrityProblem.TENANT_NOT_USER_VISIBLE, report.problems)


class TenantDiscoveryMissAppSideTest(unittest.TestCase):
    """F1 变异锚点：应用身份路径半页 / 权限暂时收窄，漏看了其中一个租户——但应用
    路径还看得见*别的*租户，所以批次不是"零租户"这种一眼假的形状。这是真正危险的
    场景：批次表面完整（另一个租户自身两条路径完全对齐），一个真实存在的租户会
    从新基线里整个消失，而完整性校验永远没机会检查它，因为它压根不出现在
    ``SnapshotBatch.tenants`` 里。

    把这里的修复临时改回 ``for tenant_key in sorted(app_tenant_keys):``（只遍历
    应用身份路径看到的租户）就会让本用例变红：``batch.tenants`` 会只剩
    ``tenant_visible_to_both``，``tenant_only_visible_to_user`` 连同它的成员一起
    从批次里消失，而 ``require_complete_batch`` 不会抛任何异常——一个真实存在、
    仍有成员的租户会被静默提交为"不存在"。"""

    def test_a_tenant_only_visible_to_the_user_identity_is_not_silently_dropped(self) -> None:
        client = FakeDirectoryClient(
            app_tenants=[{"tenant_key": "tenant_visible_to_both"}],
            user_tenants=[{"tenant_key": "tenant_visible_to_both"}, {"tenant_key": "tenant_only_visible_to_user"}],
            app_scope={"tenant_visible_to_both": {"0": ([], [{"open_user_id": "ou_both"}])}},
            user_scope={
                "tenant_visible_to_both": {None: ([], [_member("ou_both")])},
                "tenant_only_visible_to_user": {None: ([], [_member("ou_user_only")])},
            },
        )

        batch = read_org_snapshot(client=client, app_token="app-token", user_token="user-token")

        self.assertEqual(
            {scope.tenant_key for scope in batch.tenants},
            {"tenant_visible_to_both", "tenant_only_visible_to_user"},
            "只在应用身份路径能看到的租户里找 tenant_only_visible_to_user 是找不到的——"
            "并集遍历必须让它出现在批次里，哪怕应用侧看不到它。",
        )
        self.assertIn(
            "ou_user_only",
            {item.open_id for item in batch.members},
            "这名成员必须真的进了 batch.members，而不只是在 TenantScope 里被记一笔",
        )

        missing_side = next(
            scope for scope in batch.tenants if scope.tenant_key == "tenant_only_visible_to_user"
        )
        self.assertEqual(missing_side.app_member_keys, frozenset(), "应用侧看不到，必须显式置空，不能借用用户侧的值")
        self.assertEqual(missing_side.user_member_keys, frozenset({"ou_user_only"}))

        report = verify_batch(batch)
        self.assertFalse(report.complete, "批次必须被挡住，不能因为另一个租户自身对齐就被判完整")
        self.assertIn(IntegrityProblem.PATH_SETS_DIFFER, report.problems)
        with self.assertRaises(Exception):
            require_complete_batch(batch)


class TenantKeyShapeTest(unittest.TestCase):
    """F2 变异锚点：租户键取不到就该抛错，不能被 ``if key`` 静默过滤掉。"""

    def test_a_malformed_app_tenant_record_raises_instead_of_being_dropped(self) -> None:
        client = FakeDirectoryClient(
            app_tenants=[{"tenant_key": "tenant_a"}, {"no_tenant_key_field": "whatever"}],
            user_tenants=[{"tenant_key": "tenant_a"}],
            app_scope={"tenant_a": {"0": ([], [{"open_user_id": "ou_1"}])}},
        )

        with self.assertRaises(OrgSnapshotReadError) as raised:
            read_org_snapshot(client=client, app_token="app-token", user_token="user-token")
        self.assertEqual(raised.exception.args[0], "app_scope_tenant_key_missing")

    def test_a_malformed_user_tenant_record_raises_instead_of_being_dropped(self) -> None:
        client = FakeDirectoryClient(
            app_tenants=[{"tenant_key": "tenant_a"}],
            user_tenants=[{"tenant_key": "tenant_a"}, {"tenant_key": ""}],
            app_scope={"tenant_a": {"0": ([], [{"open_user_id": "ou_1"}])}},
        )

        with self.assertRaises(OrgSnapshotReadError) as raised:
            read_org_snapshot(client=client, app_token="app-token", user_token="user-token")
        self.assertEqual(raised.exception.args[0], "user_scope_tenant_key_missing")


class DepartmentIntegrityTest(unittest.TestCase):
    """F3 变异锚点：应用路径看到的部门必须纳入批次完整性校验，否则用户路径漏看
    的空部门（不含任何成员，成员集合两边照样能对上）会被判完整、静默从基线消失。

    把 ``_walk_app_scope`` 改回只返回成员集合（不返回部门集合）、或把
    ``read_org_snapshot`` 里不再把它写进 ``TenantScope.app_department_keys``，
    就会让本用例变红：``require_complete_batch`` 不会抛任何异常，
    ``od_ghost`` 这个部门会在这一轮完成批次里彻底消失，而下游看不出任何异常——
    这正是 F3 描述的"``_committable_batch()`` 把'有成员、零部门'当成可提交形状"。
    """

    def test_a_department_seen_only_by_the_app_path_fails_the_batch(self) -> None:
        client = FakeDirectoryClient(
            app_tenants=[{"tenant_key": "tenant_a"}],
            user_tenants=[{"tenant_key": "tenant_a"}],
            app_scope={
                "tenant_a": {
                    "0": ([{"open_department_id": "od_ghost"}], [{"open_user_id": "ou_1"}]),
                    # "od_ghost" 未在这里另外注册子层级：FakeDirectoryClient 对未知
                    # (tenant, department_id) 组合默认返回 ([], [])，遍历到它时不会
                    # 再产出更多部门或成员，模拟"这个部门本身没有任何成员"。
                }
            },
            user_scope={"tenant_a": {None: ([], [_member("ou_1")])}},
        )

        batch = read_org_snapshot(client=client, app_token="app-token", user_token="user-token")

        tenant_a = batch.tenants[0]
        self.assertEqual(tenant_a.app_department_keys, frozenset({"od_ghost"}))
        self.assertEqual(tenant_a.user_department_keys, frozenset())
        # 成员集合两边完全一致——如果完整性校验只看成员，这个批次会被判完整。
        self.assertEqual(tenant_a.app_member_keys, tenant_a.user_member_keys)

        report = verify_batch(batch)
        self.assertFalse(report.complete)
        self.assertIn(IntegrityProblem.DEPARTMENT_SETS_DIFFER, report.problems)
        with self.assertRaises(Exception):
            require_complete_batch(batch)


class MemberIdentityConflictTest(unittest.TestCase):
    """F5 变异锚点：同一个 open_id 在不同部门页给出不同 user_id / union_id 是身份
    矛盾，不能被后到的那一条静默覆盖。"""

    def test_conflicting_user_id_across_pages_raises(self) -> None:
        client = FakeDirectoryClient(
            app_tenants=[{"tenant_key": "tenant_a"}],
            user_tenants=[{"tenant_key": "tenant_a"}],
            app_scope={"tenant_a": {"0": ([], [{"open_user_id": "ou_1"}])}},
            user_scope={
                "tenant_a": {
                    None: ([{"open_department_id": "od_1"}, {"open_department_id": "od_2"}], []),
                    ("od_1", "open_department_id"): (
                        [],
                        [{"open_id": "ou_1", "user_id": "u_1", "union_id": "on_1", "name": "张一"}],
                    ),
                    ("od_2", "open_department_id"): (
                        [],
                        [{"open_id": "ou_1", "user_id": "u_1_conflict", "union_id": "on_1", "name": "张一"}],
                    ),
                }
            },
        )

        with self.assertRaises(OrgSnapshotReadError) as raised:
            read_org_snapshot(client=client, app_token="app-token", user_token="user-token")
        self.assertEqual(raised.exception.args[0], "user_scope_member_identity_conflict")

    def test_conflicting_union_id_across_pages_raises(self) -> None:
        client = FakeDirectoryClient(
            app_tenants=[{"tenant_key": "tenant_a"}],
            user_tenants=[{"tenant_key": "tenant_a"}],
            app_scope={"tenant_a": {"0": ([], [{"open_user_id": "ou_1"}])}},
            user_scope={
                "tenant_a": {
                    None: ([{"open_department_id": "od_1"}, {"open_department_id": "od_2"}], []),
                    ("od_1", "open_department_id"): (
                        [],
                        [{"open_id": "ou_1", "user_id": "u_1", "union_id": "on_1", "name": "张一"}],
                    ),
                    ("od_2", "open_department_id"): (
                        [],
                        [{"open_id": "ou_1", "user_id": "u_1", "union_id": "on_1_conflict", "name": "张一"}],
                    ),
                }
            },
        )

        with self.assertRaises(OrgSnapshotReadError) as raised:
            read_org_snapshot(client=client, app_token="app-token", user_token="user-token")
        self.assertEqual(raised.exception.args[0], "user_scope_member_identity_conflict")

    def test_the_same_identity_seen_twice_merges_department_names_without_raising(self) -> None:
        """正向对照：user_id / union_id 一致时不是冲突，只是同一个人跨部门可见，
        必须正常合并部门名，不能被 F5 的修复误伤。"""

        client = FakeDirectoryClient(
            app_tenants=[{"tenant_key": "tenant_a"}],
            user_tenants=[{"tenant_key": "tenant_a"}],
            app_scope={"tenant_a": {"0": ([], [{"open_user_id": "ou_1"}])}},
            user_scope={
                "tenant_a": {
                    None: (
                        [
                            {"open_department_id": "od_1", "name": "部门一"},
                            {"open_department_id": "od_2", "name": "部门二"},
                        ],
                        [],
                    ),
                    ("od_1", "open_department_id"): ([], [_member("ou_1")]),
                    ("od_2", "open_department_id"): ([], [_member("ou_1")]),
                }
            },
        )

        batch = read_org_snapshot(client=client, app_token="app-token", user_token="user-token")

        self.assertEqual(len(batch.members), 1)
        self.assertEqual(set(batch.members[0].department_names), {"部门一", "部门二"})


class ShapeErrorTest(unittest.TestCase):
    def test_a_missing_member_identity_field_raises_instead_of_guessing(self) -> None:
        client = FakeDirectoryClient(
            app_tenants=[{"tenant_key": "tenant_a"}],
            user_tenants=[{"tenant_key": "tenant_a"}],
            app_scope={"tenant_a": {"0": ([], [{"open_user_id": "ou_1"}])}},
            user_scope={"tenant_a": {None: ([], [{"open_id": "ou_1", "user_id": "u_1", "name": "缺 union_id"}])}},
        )

        with self.assertRaises(OrgSnapshotReadError) as raised:
            read_org_snapshot(client=client, app_token="app-token", user_token="user-token")
        self.assertEqual(raised.exception.args[0], "user_scope_member_identity_incomplete")

    def test_a_missing_app_member_id_raises(self) -> None:
        client = FakeDirectoryClient(
            app_tenants=[{"tenant_key": "tenant_a"}],
            user_tenants=[{"tenant_key": "tenant_a"}],
            app_scope={"tenant_a": {"0": ([], [{"name": "无 open_user_id"}])}},
        )

        with self.assertRaises(OrgSnapshotReadError) as raised:
            read_org_snapshot(client=client, app_token="app-token", user_token="user-token")
        self.assertEqual(raised.exception.args[0], "app_scope_member_id_missing")

    def test_structured_member_names_from_the_app_path_are_accepted(self) -> None:
        """应用身份路径的成员姓名可能是 {default_value, i18n_value} 结构
        （Issue #250 编排者 2026-08-19 实测）；只在用户路径需要姓名时才解析，
        应用路径本身不需要姓名，这里只验证结构化值不会让解析炸掉。"""

        client = FakeDirectoryClient(
            app_tenants=[{"tenant_key": "tenant_a"}],
            user_tenants=[{"tenant_key": "tenant_a"}],
            app_scope={
                "tenant_a": {
                    "0": ([], [{"open_user_id": "ou_1", "name": {"default_value": "张一", "i18n_value": {}}}])
                }
            },
            user_scope={"tenant_a": {None: ([], [_member("ou_1", "张一")])}},
        )

        batch = read_org_snapshot(client=client, app_token="app-token", user_token="user-token")
        require_complete_batch(batch)  # 不抛异常即通过


class DepartmentLimitTest(unittest.TestCase):
    def test_the_department_traversal_upper_bound_is_generous_but_finite(self) -> None:
        self.assertGreater(MAX_DEPARTMENTS_PER_TENANT, 100)


if __name__ == "__main__":
    unittest.main()
