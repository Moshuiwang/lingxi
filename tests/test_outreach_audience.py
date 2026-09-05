"""收件人装配：状态驱动、失败关闭（Issue #586 完成标准 1/7 的数据侧）。

只对 ``provisioning_state=active`` 且 ``account_state=enabled`` 的人产出可发送的
取值——判据是状态，不是时间，因此经迟到就绪恢复才激活的人在下一次 ``--apply``
里自然被捞到，不需要"猜他是什么时候好的"。

否定面：不在库里、还没 active、账号停用、没有 open_id、权限读不懂、零指标、
花名册同邮箱指向两个不同姓名——一条都不产出取值，各自给出可打印的跳过原因。
"""

from __future__ import annotations

import unittest

from lingxi.config.content import default_content_catalog
from lingxi.core.outreach.audience import (
    SKIP_AMBIGUOUS_NAME,
    SKIP_COMPANY_NAME_MISSING,
    SKIP_NO_METRICS,
    SKIP_NO_OPEN_ID,
    SKIP_NO_PERMISSIONS,
    SKIP_NOT_ACTIVE,
    SKIP_NOT_FOUND,
    SKIP_UNREADABLE_PERMISSIONS,
    SubjectFacts,
    plan_outreach,
)

CATALOG = default_content_catalog()
EMAIL = "joshua.wang@example.invalid"
NAMES = ("王晋 (Joshua Wang)",)
PERMISSIONS = '{"1011": ["充值金额", "日活用户数"]}'


def _facts(**overrides) -> SubjectFacts:
    base = {
        "email": EMAIL,
        "user_id": "usr_fake",
        "open_id": "ou_fake_open_id_for_tests",
        "provisioning_state": "active",
        "account_state": "enabled",
        "permissions": PERMISSIONS,
        "roster_names": NAMES,
    }
    base.update(overrides)
    return SubjectFacts(**base)


def _plan(facts: SubjectFacts, *, company_names: dict[str, str] | None = None):
    return plan_outreach(
        facts,
        company_names={"1011": "尼日利亚"} if company_names is None else company_names,
        total_company_count=43,
        catalog=CATALOG,
    )


class SendableTest(unittest.TestCase):
    def test_an_active_user_with_a_published_scope_is_sendable(self) -> None:
        plan = _plan(_facts())
        self.assertTrue(plan.sendable)
        self.assertIsNone(plan.skip_reason)
        self.assertTrue(plan.active)
        self.assertEqual(plan.company_scope, "尼日利亚")
        self.assertEqual(plan.metric_count, 2)

    def test_the_wildcard_scope_is_folded_for_the_dry_run_listing(self) -> None:
        plan = _plan(_facts(permissions='{"*": ["充值金额"]}'))
        self.assertTrue(plan.sendable)
        self.assertEqual(plan.company_scope, "全部公司（43 家）")

    def test_the_name_comes_from_the_roster_snapshot_verbatim(self) -> None:
        plan = _plan(_facts())
        assert plan.audience is not None
        self.assertEqual(plan.audience.display_name, NAMES[0])

    def test_the_same_roster_name_twice_is_one_person_not_an_ambiguity(self) -> None:
        """重复邮箱是同一个人离职再入职的常态；姓名一致就不是歧义。"""
        plan = _plan(_facts(roster_names=(NAMES[0], f" {NAMES[0]} ")))
        self.assertTrue(plan.sendable)


class NotSendableTest(unittest.TestCase):
    def test_a_person_who_is_not_in_app_user_is_skipped(self) -> None:
        plan = _plan(_facts(user_id=None))
        self.assertFalse(plan.sendable)
        self.assertEqual(plan.skip_reason, SKIP_NOT_FOUND)

    def test_a_person_still_provisioning_is_skipped(self) -> None:
        """否定断言：只对 active 的人发。"""
        plan = _plan(_facts(provisioning_state="mcp_syncing"))
        self.assertEqual(plan.skip_reason, SKIP_NOT_ACTIVE)
        self.assertFalse(plan.active)

    def test_a_suspended_account_is_skipped_even_when_active(self) -> None:
        """停用的人收到「你现在可以开始提问」是一句当场被证伪的话。"""
        plan = _plan(_facts(account_state="suspended"))
        self.assertEqual(plan.skip_reason, SKIP_NOT_ACTIVE)

    def test_a_person_without_an_open_id_is_skipped(self) -> None:
        plan = _plan(_facts(open_id=None))
        self.assertEqual(plan.skip_reason, SKIP_NO_OPEN_ID)

    def test_a_person_without_a_published_scope_is_skipped(self) -> None:
        plan = _plan(_facts(permissions=None))
        self.assertEqual(plan.skip_reason, SKIP_NO_PERMISSIONS)

    def test_an_unreadable_scope_is_skipped_rather_than_guessed(self) -> None:
        plan = _plan(_facts(permissions="not json at all"))
        self.assertEqual(plan.skip_reason, SKIP_UNREADABLE_PERMISSIONS)

    def test_a_revoked_scope_yields_no_card(self) -> None:
        plan = _plan(_facts(permissions="{}"))
        self.assertEqual(plan.skip_reason, SKIP_NO_METRICS)

    def test_two_different_roster_names_on_one_email_are_not_guessed(self) -> None:
        """否定断言：同邮箱指向两个不同姓名时不猜发给谁。"""
        plan = _plan(_facts(roster_names=("王晋", "李四")))
        self.assertEqual(plan.skip_reason, SKIP_AMBIGUOUS_NAME)

    def test_a_person_missing_from_the_roster_is_not_guessed_either(self) -> None:
        plan = _plan(_facts(roster_names=()))
        self.assertEqual(plan.skip_reason, SKIP_AMBIGUOUS_NAME)

    def test_no_skip_reason_carries_personal_data(self) -> None:
        reasons = {
            _plan(_facts(user_id=None)).skip_reason,
            _plan(_facts(roster_names=("王晋", "李四"))).skip_reason,
        }
        for reason in reasons:
            self.assertNotIn("王晋", str(reason))


class CompanyNameMissingTest(unittest.TestCase):
    """公司中文名查不到的人整条跳过，不回落显示编号。"""

    def test_a_company_without_a_chinese_name_skips_the_person(self) -> None:
        """否定断言：编号是内部标识，不能印在一张给用户看的欢迎卡上。"""
        plan = _plan(_facts(), company_names={})
        self.assertFalse(plan.sendable)
        self.assertEqual(plan.skip_reason, SKIP_COMPANY_NAME_MISSING)
        self.assertTrue(plan.active)

    def test_one_missing_name_among_several_still_skips(self) -> None:
        facts = _facts(permissions='{"1011": ["充值金额"], "9999": ["充值金额"]}')
        plan = _plan(facts, company_names={"1011": "尼日利亚"})
        self.assertEqual(plan.skip_reason, SKIP_COMPANY_NAME_MISSING)

    def test_a_folded_long_list_is_judged_by_the_same_rule(self) -> None:
        """连范围都说不全的人不发，即使那张卡只会显示"N 家公司"。"""
        ids = [str(1010 + index) for index in range(6)]
        permissions = "{" + ", ".join(f'"{key}": ["充值金额"]' for key in ids) + "}"
        names = {key: f"公司{key}" for key in ids[:-1]}
        self.assertEqual(
            _plan(_facts(permissions=permissions), company_names=names).skip_reason,
            SKIP_COMPANY_NAME_MISSING,
        )

    def test_the_wildcard_scope_never_needs_a_company_name(self) -> None:
        plan = _plan(_facts(permissions='{"*": ["充值金额"]}'), company_names={})
        self.assertTrue(plan.sendable)

    def test_the_skip_reason_carries_no_company_number(self) -> None:
        plan = _plan(_facts(), company_names={})
        self.assertNotIn("1011", str(plan.skip_reason))


if __name__ == "__main__":
    unittest.main()
