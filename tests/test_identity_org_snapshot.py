"""组织快照批次的完整性校验与 90 天过期规则（纯逻辑，无数据库、无网络）。

「完整性校验不过就不提交半轮快照」是硬规则：这里证明**判定**会不通过，
真库里「不通过就一行都不落」由 tests/test_identity_postgres_records.py 证明。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from lingxi.core.identity.org_snapshot import (
    SNAPSHOT_RETENTION_DAYS,
    DirectoryAvailability,
    IntegrityProblem,
    SnapshotBatch,
    SnapshotDepartment,
    SnapshotMember,
    TenantScope,
    directory_availability,
    snapshot_expires_at,
    verify_batch,
)


def member(
    *,
    tenant_key: str = "tenant_a",
    member_key: str = "ou_member_one",
    open_id: str = "ou_member_one",
    user_id: str = "user_one",
    union_id: str = "union_one",
    display_name: str = "张一",
    display_name_locale: str | None = "zh-CN",
    department_names: tuple[str, ...] = ("测试部门",),
) -> SnapshotMember:
    return SnapshotMember(
        tenant_key=tenant_key,
        member_key=member_key,
        open_id=open_id,
        user_id=user_id,
        union_id=union_id,
        display_name=display_name,
        display_name_locale=display_name_locale,
        department_names=department_names,
    )


def batch(members: tuple[SnapshotMember, ...], *, app_keys: frozenset[str] | None = None) -> SnapshotBatch:
    keys = frozenset(item.member_key for item in members)
    return SnapshotBatch(
        tenants=(
            TenantScope(
                tenant_key="tenant_a",
                visible_to_user_identity=True,
                app_member_keys=keys if app_keys is None else app_keys,
                user_member_keys=keys,
            ),
        ),
        departments=(SnapshotDepartment(tenant_key="tenant_a", department_key="dept_a", name="测试部门"),),
        members=members,
    )


class BatchIntegrityTest(unittest.TestCase):
    def test_a_member_row_outside_both_visible_paths_is_incomplete(self) -> None:
        """反方向的完整性：两条可见路径都没见过的成员行混进批次，提交后会为
        「不该被看到的人」建档（Codex 复查发现）。"""
        stranger = member(member_key="ou_stranger", open_id="ou_stranger", user_id="user_s", union_id="union_s", display_name="陌生人")
        bad = batch((member(), stranger), app_keys=frozenset({"ou_zhang"}))
        bad = SnapshotBatch(
            tenants=(TenantScope("tenant_a", True, frozenset({"ou_zhang"}), frozenset({"ou_zhang"})),),
            departments=bad.departments,
            members=(member(), stranger),
        )

        report = verify_batch(bad)

        self.assertFalse(report.complete)
        self.assertIn(IntegrityProblem.MEMBER_ROW_EXTRA, report.problems)

    def test_a_batch_with_matching_paths_and_complete_identity_fields_is_complete(self) -> None:
        report = verify_batch(batch((member(), member(member_key="ou_two", open_id="ou_two", user_id="user_two", union_id="union_two", display_name="Alice Smith", display_name_locale="en-US"))))

        self.assertTrue(report.complete)
        self.assertEqual(report.problems, ())
        self.assertEqual(report.member_count, 2)
        self.assertEqual(report.tenant_count, 1)

    def test_paths_that_see_different_member_sets_fail_the_batch(self) -> None:
        # 应用身份路径与用户身份路径必须看到同一批成员；不一致说明可见范围配置被改动。
        report = verify_batch(batch((member(),), app_keys=frozenset({"ou_member_one", "ou_only_visible_to_app"})))

        self.assertFalse(report.complete)
        self.assertIn(IntegrityProblem.PATH_SETS_DIFFER, report.problems)

    def test_a_member_missing_any_identity_field_fails_the_batch(self) -> None:
        for field in ("open_id", "user_id", "union_id", "display_name"):
            with self.subTest(field=field):
                report = verify_batch(batch((member(**{field: ""}),)))

                self.assertFalse(report.complete)
                self.assertIn(IntegrityProblem.IDENTITY_FIELD_MISSING, report.problems)

    def test_a_duplicated_open_id_fails_the_batch(self) -> None:
        # 实测 8 租户 710 人 open_id 去重后仍 710、重复 0；出现重复即数据面异常。
        report = verify_batch(
            batch((member(), member(member_key="ou_dup_key", user_id="user_two", union_id="union_two")))
        )

        self.assertFalse(report.complete)
        self.assertIn(IntegrityProblem.DUPLICATE_OPEN_ID, report.problems)

    def test_an_empty_tenant_or_an_empty_batch_fails(self) -> None:
        empty_tenant = SnapshotBatch(
            tenants=(TenantScope("tenant_a", True, frozenset(), frozenset()),),
            departments=(),
            members=(),
        )
        no_tenant = SnapshotBatch(tenants=(), departments=(), members=())

        self.assertIn(IntegrityProblem.EMPTY_TENANT, verify_batch(empty_tenant).problems)
        self.assertIn(IntegrityProblem.NO_TENANT, verify_batch(no_tenant).problems)

    def test_a_department_seen_only_by_the_app_path_fails_the_batch(self) -> None:
        """F3 变异锚点：一个空部门（不含任何成员）只被应用路径看到、用户路径没有
        产出对应的行——成员集合两边仍相等，必须靠部门集合本身的交叉校验才挡得住，
        否则原基线里那个部门会在这一轮悄悄消失。"""
        scope = TenantScope(
            "tenant_a",
            True,
            frozenset({"ou_member_one"}),
            frozenset({"ou_member_one"}),
            app_department_keys=frozenset({"dept_ghost"}),
            user_department_keys=frozenset(),
        )
        report = verify_batch(SnapshotBatch(tenants=(scope,), departments=(), members=(member(),)))

        self.assertFalse(report.complete)
        self.assertIn(IntegrityProblem.DEPARTMENT_SETS_DIFFER, report.problems)

    def test_matching_department_sets_on_both_paths_do_not_fail_the_batch(self) -> None:
        """两条路径都没看到额外部门（或都看到同一批）时不是问题——不该把"这个租户
        确实没有下级部门"这类合法形态误判成完整性缺陷。"""
        scope = TenantScope(
            "tenant_a",
            True,
            frozenset({"ou_member_one"}),
            frozenset({"ou_member_one"}),
            app_department_keys=frozenset({"dept_a"}),
            user_department_keys=frozenset({"dept_a"}),
        )
        report = verify_batch(
            SnapshotBatch(
                tenants=(scope,),
                departments=(SnapshotDepartment(tenant_key="tenant_a", department_key="dept_a", name="测试部门"),),
                members=(member(),),
            )
        )

        self.assertTrue(report.complete)

    def test_a_tenant_invisible_to_the_user_identity_fails_the_batch(self) -> None:
        # 2026-07-29 补齐可见范围后才成立的前提；配置被改回时必须整批失败而不是少同步一批人。
        scope = TenantScope("tenant_a", False, frozenset({"ou_member_one"}), frozenset({"ou_member_one"}))
        report = verify_batch(SnapshotBatch(tenants=(scope,), departments=(), members=(member(),)))

        self.assertFalse(report.complete)
        self.assertIn(IntegrityProblem.TENANT_NOT_USER_VISIBLE, report.problems)

    def test_a_member_that_belongs_to_no_declared_tenant_fails_the_batch(self) -> None:
        stray = member(tenant_key="tenant_unknown", member_key="ou_stray", open_id="ou_stray", user_id="user_stray", union_id="union_stray")
        original = batch((member(),))
        report = verify_batch(SnapshotBatch(tenants=original.tenants, departments=original.departments, members=(*original.members, stray)))

        self.assertFalse(report.complete)
        self.assertIn(IntegrityProblem.MEMBER_TENANT_UNKNOWN, report.problems)

    def test_a_member_key_seen_by_the_path_but_missing_a_row_fails_the_batch(self) -> None:
        # 只统计到人数、没落成行，正是"半轮快照"最典型的形态。
        scope = TenantScope("tenant_a", True, frozenset({"ou_member_one", "ou_counted_only"}), frozenset({"ou_member_one", "ou_counted_only"}))
        report = verify_batch(SnapshotBatch(tenants=(scope,), departments=(), members=(member(),)))

        self.assertFalse(report.complete)
        self.assertIn(IntegrityProblem.MEMBER_ROW_MISSING, report.problems)

    def test_a_repeated_member_key_within_one_tenant_fails_the_batch(self) -> None:
        twin = member(open_id="ou_other", user_id="user_other", union_id="union_other")
        report = verify_batch(batch((member(), twin)))

        self.assertFalse(report.complete)
        self.assertIn(IntegrityProblem.DUPLICATE_MEMBER_KEY, report.problems)

    def test_names_are_not_treated_as_a_key_so_duplicates_stay_complete(self) -> None:
        # 710 人实测含 1 对重名、11 人纯拉丁、9 人中拉混合；姓名既不是键也不假定中文。
        twins = (
            member(display_name="张三"),
            member(member_key="ou_two", open_id="ou_two", user_id="user_two", union_id="union_two", display_name="张三"),
            member(member_key="ou_three", open_id="ou_three", user_id="user_three", union_id="union_three", display_name="Alice Smith", display_name_locale="en-US"),
            member(member_key="ou_four", open_id="ou_four", user_id="user_four", union_id="union_four", display_name="Anna 李"),
        )

        report = verify_batch(batch(twins))

        self.assertTrue(report.complete)
        self.assertEqual(report.member_count, 4)

    def test_optional_fields_are_never_required(self) -> None:
        # mobile / job_title / leader_id 有值率 46.6% / 36.8% / 6.6%，只能作可选增强。
        thin = member(display_name_locale=None, department_names=())

        self.assertTrue(verify_batch(batch((thin,))).complete)


class SnapshotExpiryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.started_at = datetime(2026, 8, 5, 3, 0, tzinfo=UTC)

    def test_a_batch_expires_ninety_days_after_it_started(self) -> None:
        self.assertEqual(SNAPSHOT_RETENTION_DAYS, 90)
        self.assertEqual(snapshot_expires_at(self.started_at), self.started_at + timedelta(hours=2160))

    def test_naive_datetimes_are_rejected_because_storage_is_utc_only(self) -> None:
        with self.assertRaises(ValueError):
            snapshot_expires_at(datetime(2026, 8, 5, 3, 0))

    def test_availability_turns_stale_exactly_at_expiry_and_unavailable_without_a_batch(self) -> None:
        expires_at = snapshot_expires_at(self.started_at)

        self.assertIs(directory_availability(expires_at, expires_at - timedelta(seconds=1)), DirectoryAvailability.AVAILABLE)
        self.assertIs(directory_availability(expires_at, expires_at), DirectoryAvailability.STALE)
        self.assertIs(directory_availability(expires_at, expires_at + timedelta(days=1)), DirectoryAvailability.STALE)
        self.assertIs(directory_availability(None, self.started_at), DirectoryAvailability.UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
