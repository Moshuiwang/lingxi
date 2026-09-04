"""预开通（[Issue #541](https://github.com/Moshuiwang/lingxi/issues/541)，rc25 S-8a）里
**不需要数据库也不需要开通链**的那一半：邮箱 → 飞书身份的提前定位，以及系统触发入口
用到的三个小协作者（合成事件标识、账本 no-op、静默投递）。

整条链在系统触发路径上的行为（X-1 同邮箱闸仍然生效、静默、当场收口、幂等）由
``tests/test_onboarding_runner.py::SystemTriggerTests`` 覆盖；候选查询那一半由
``tests/test_postgres_stalled_provisioning.py`` 在真库上覆盖。本文件只管定位判据。
"""

from __future__ import annotations

import unittest
from collections.abc import Mapping, Sequence
from typing import Any

from lingxi.core.conversation.ports import OnboardingState
from lingxi.core.identity.onboarding_terminal import KEY_COMPLETED, KEY_SYNCING
from lingxi.core.identity.org_snapshot import DirectoryAvailability, SnapshotMember
from lingxi.core.identity.preprovision import (
    NULL_DISPATCH_LEDGER,
    ORIGIN_FIRST_CHAT,
    ORIGIN_PREPROVISION,
    SKIP_DIRECTORY_MULTIPLE_MEMBERS,
    SKIP_DIRECTORY_UNAVAILABLE,
    SKIP_EMAIL_BLANK,
    SKIP_EMAIL_MULTIPLE_PERSONNEL,
    SKIP_EMAIL_NOT_IN_ROSTER,
    SKIP_PERSONNEL_NOT_IN_DIRECTORY,
    PreprovisionSkip,
    PreprovisionTarget,
    deliver_silently,
    is_system_trigger,
    locate_by_email,
    origin_of,
    plan_preprovision,
    system_event_id,
)


def member(*, user_id: str = "u_ming", open_id: str = "ou_ming") -> SnapshotMember:
    return SnapshotMember(
        tenant_key="tenant-fake",
        member_key=f"tenant-fake:{open_id}",
        open_id=open_id,
        user_id=user_id,
        union_id=f"on_{open_id}",
        display_name="化名甲",
        display_name_locale=None,
        department_names=("测试部门",),
    )


def roster_row(*, personnel_id: str, email: str) -> Mapping[str, Any]:
    return {
        "personnel_id": personnel_id,
        "email": email,
        "name": "化名甲",
        "employee_no": f"E{personnel_id}",
        "record_id": f"rec_{personnel_id}",
    }


class FakeDirectory:
    """``lookup_by_user_id`` 的内存假实现：``user_id`` → 该轮快照里的候选成员。"""

    def __init__(
        self,
        members: Mapping[str, Sequence[SnapshotMember]] | None = None,
        *,
        availability: DirectoryAvailability = DirectoryAvailability.AVAILABLE,
    ) -> None:
        self._members = dict(members or {})
        self._availability = availability
        self.calls: list[str] = []

    def lookup_by_user_id(self, user_id: str) -> Any:
        self.calls.append(user_id)

        class _Lookup:
            availability = self._availability
            members = tuple(self._members.get(user_id, ()))

        return _Lookup()


ROSTER = (
    roster_row(personnel_id="u_ming", email="Xiaoming@Example.com"),
    roster_row(personnel_id="u_hong", email="hong@example.com"),
    # 同一个邮箱下两个不同的人员 ID——花名册实测里有 86 组这种形态。
    roster_row(personnel_id="u_dup_a", email="shared@example.com"),
    roster_row(personnel_id="u_dup_b", email="shared@example.com"),
)

DIRECTORY = FakeDirectory(
    {"u_ming": (member(),), "u_hong": (member(user_id="u_hong", open_id="ou_hong"),)}
)


class LocateByEmailTests(unittest.TestCase):
    """邮箱 → 花名册 ``personnel_id`` → 组织快照成员。**任一环节非唯一即跳过。**"""

    def test_a_unique_email_locates_the_member(self) -> None:
        located = locate_by_email("Xiaoming@Example.com", roster_rows=ROSTER, directory=DIRECTORY)
        assert isinstance(located, PreprovisionTarget)
        self.assertEqual(located.personnel_id, "u_ming")
        self.assertEqual(located.open_id, "ou_ming")

    def test_the_email_is_normalized_the_same_way_as_the_publish_record_key(self) -> None:
        """名单里大写 / 带空白与花名册里小写是纯格式差异，不能被当成查无此人；
        口径必须与 ``account_match.normalize_email``（也就是正式表 ``record_key``）
        同源。"""

        located = locate_by_email(
            "  XIAOMING@EXAMPLE.COM ", roster_rows=ROSTER, directory=DIRECTORY
        )
        self.assertIsInstance(located, PreprovisionTarget)

    def test_an_email_matching_several_personnel_ids_is_always_skipped(self) -> None:
        """**产品负责人 2026-09-02 裁定 6（硬规则）。** 认错人在迁移 ``0085`` 的部分
        唯一索引之后**不可自愈**：错的那一行会永久占住这个邮箱，真正的那个人首聊时
        被「同邮箱已绑定他人」拒绝，而仓库里没有改绑动作。因此哪怕只有一个人员 ID
        在组织快照里，也一律跳过、不猜。"""

        directory = FakeDirectory({"u_dup_a": (member(user_id="u_dup_a", open_id="ou_a"),)})
        located = locate_by_email("shared@example.com", roster_rows=ROSTER, directory=directory)

        self.assertEqual(
            located,
            PreprovisionSkip(email="shared@example.com", reason=SKIP_EMAIL_MULTIPLE_PERSONNEL),
        )
        self.assertEqual(directory.calls, [], "判断多命中不该再去读组织快照")

    def test_the_same_personnel_id_on_several_roster_rows_is_not_a_conflict(self) -> None:
        """同一个人在花名册里有两行（离职再入职是既有事实）不是"两个人"：判据是
        去重后的**人员 ID 集合**，不是行数。"""

        rows = ROSTER + (roster_row(personnel_id="u_ming", email="xiaoming@example.com"),)
        located = locate_by_email("xiaoming@example.com", roster_rows=rows, directory=DIRECTORY)
        self.assertIsInstance(located, PreprovisionTarget)

    def test_an_email_absent_from_the_roster_is_skipped(self) -> None:
        located = locate_by_email("nobody@example.com", roster_rows=ROSTER, directory=DIRECTORY)
        self.assertEqual(
            located, PreprovisionSkip(email="nobody@example.com", reason=SKIP_EMAIL_NOT_IN_ROSTER)
        )

    def test_a_blank_email_is_skipped_with_its_own_reason(self) -> None:
        located = locate_by_email("   ", roster_rows=ROSTER, directory=DIRECTORY)
        self.assertEqual(located, PreprovisionSkip(email="   ", reason=SKIP_EMAIL_BLANK))

    def test_a_personnel_id_outside_the_directory_snapshot_is_skipped(self) -> None:
        """组织快照只覆盖花名册人员的一部分（实测 59.9%）。"不在快照里"既可能是
        离职、也可能只是那个租户没共享给本应用——两种都不是"可以开通"。"""

        located = locate_by_email("hong@example.com", roster_rows=ROSTER, directory=FakeDirectory())
        self.assertEqual(
            located,
            PreprovisionSkip(email="hong@example.com", reason=SKIP_PERSONNEL_NOT_IN_DIRECTORY),
        )

    def test_several_members_sharing_one_user_id_are_skipped(self) -> None:
        """``feishu_org_member_snapshot`` 对 ``user_id`` 没有唯一约束（账号复用换人
        按 #34 方案 C 留给管理员侧审计）。多条候选时不许自己挑一条。"""

        directory = FakeDirectory({"u_ming": (member(), member(open_id="ou_ming_2"))})
        located = locate_by_email("xiaoming@example.com", roster_rows=ROSTER, directory=directory)
        self.assertEqual(
            located,
            PreprovisionSkip(email="xiaoming@example.com", reason=SKIP_DIRECTORY_MULTIPLE_MEMBERS),
        )

    def test_an_unavailable_directory_is_not_the_same_as_a_missing_person(self) -> None:
        """资料不可用 / 过九十天上限时"查不到"不是事实，只是我们暂时看不见——与
        ``AutoOnboardingRunner._locate`` 同一条纪律，原因码必须分得开。"""

        directory = FakeDirectory({"u_ming": (member(),)}, availability=DirectoryAvailability.STALE)
        located = locate_by_email("xiaoming@example.com", roster_rows=ROSTER, directory=directory)
        self.assertEqual(
            located,
            PreprovisionSkip(email="xiaoming@example.com", reason=SKIP_DIRECTORY_UNAVAILABLE),
        )


class PlanPreprovisionTests(unittest.TestCase):
    """一份名单 → 「可执行」与「跳过」两张清单。逐人失败关闭，不阻塞其他人。"""

    def test_one_bad_row_does_not_take_down_the_rest_of_the_list(self) -> None:
        targets, skips = plan_preprovision(
            ["xiaoming@example.com", "shared@example.com", "hong@example.com"],
            roster_rows=ROSTER,
            directory=DIRECTORY,
        )
        self.assertEqual([target.open_id for target in targets], ["ou_ming", "ou_hong"])
        self.assertEqual([skip.reason for skip in skips], [SKIP_EMAIL_MULTIPLE_PERSONNEL])

    def test_a_repeated_email_is_processed_once(self) -> None:
        """名单里同一个邮箱写了两遍是笔误，不是两个人；对同一个人跑两次开通链
        没有任何新结果。"""

        targets, skips = plan_preprovision(
            ["xiaoming@example.com", " Xiaoming@example.com "],
            roster_rows=ROSTER,
            directory=DIRECTORY,
        )
        self.assertEqual(len(targets), 1)
        self.assertEqual(skips, ())

    def test_the_list_order_is_preserved(self) -> None:
        targets, _ = plan_preprovision(
            ["hong@example.com", "xiaoming@example.com"], roster_rows=ROSTER, directory=DIRECTORY
        )
        self.assertEqual(
            [target.email for target in targets], ["hong@example.com", "xiaoming@example.com"]
        )


class SkipResultTests(unittest.TestCase):
    """跳过也要有一个可被批量脚本消费的结论。"""

    def test_a_skip_is_a_deterministic_business_failure_with_no_user_facing_message(self) -> None:
        result = PreprovisionSkip(
            email="x@example.com", reason=SKIP_EMAIL_MULTIPLE_PERSONNEL
        ).as_result()

        self.assertIs(result.state, OnboardingState.NOT_AUTHORIZED)
        self.assertEqual(result.failure_reason, SKIP_EMAIL_MULTIPLE_PERSONNEL)
        # 预开通失败**没有任何用户可见出口**：逐人原因只报告给产品负责人。
        self.assertEqual(result.messages, ())


class SystemTriggerHelpersTests(unittest.TestCase):
    """系统触发入口的三个小协作者。"""

    def test_the_synthetic_event_id_is_recognisable_and_round_trips(self) -> None:
        event_id = system_event_id("trace_x")
        self.assertTrue(is_system_trigger(event_id))
        self.assertEqual(origin_of(event_id), ORIGIN_PREPROVISION)

    def test_a_real_feishu_event_id_is_never_treated_as_a_system_trigger(self) -> None:
        self.assertFalse(is_system_trigger("evt_abcdef"))
        self.assertEqual(origin_of("evt_abcdef"), ORIGIN_FIRST_CHAT)
        # 空值同样不是系统触发：判据只认前缀，不做任何兜底放行。
        self.assertFalse(is_system_trigger(""))

    def test_the_null_ledger_does_nothing_and_raises_nothing(self) -> None:
        """系统触发没有 ``inbound_event`` 行，记账与放回都没有对象。"""

        self.assertIsNone(
            NULL_DISPATCH_LEDGER.mark_onboarding_dispatched(event_id="preprovision:x")
        )
        self.assertIsNone(
            NULL_DISPATCH_LEDGER.release_onboarding_claim(
                event_id="preprovision:x", claim_token=None
            )
        )

    def test_silent_delivery_reports_delivered_and_arms_only_the_completion_line(self) -> None:
        class Armer:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def mark_preprovision_notice_pending(self, *, open_id: str) -> bool:
                self.calls.append(open_id)
                return True

        armer = Armer()
        self.assertTrue(deliver_silently(key=KEY_SYNCING, open_id="ou_1", users=armer))
        self.assertEqual(armer.calls, [], "中途的「正在同步」不需要补，静默丢弃即可")

        self.assertTrue(deliver_silently(key=KEY_COMPLETED, open_id="ou_1", users=armer))
        self.assertEqual(armer.calls, ["ou_1"], "「开通完成」改成挂起到首聊时补一句")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
