"""`scripts/ops/outreach.py` 的纯逻辑用例与「预检=正式」的静态断言（Issue #586）。

加载方式同 `tests/test_preprovision_ops.py`：`scripts/` 下的文件用
`importlib.util.spec_from_file_location` 按路径加载。

覆盖 Issue #586 的完成标准 5（预检与正式同源）与本脚本自己的四条：默认档零发送、
名单形态写错当场拒、逐人失败关闭、互斥开关不许同时给。**静态断言用 AST 数调用点**
——`render_welcome_card` 与 `plan_outreach` 在整条链上各自只许出现一次调用；有人
为预检另写一份渲染或另装配一份数据，这里就会变红，而不是等真机预检验不出问题。
"""

from __future__ import annotations

import ast
import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from lingxi.core.outreach.audience import SubjectFacts
from lingxi.core.outreach.dispatch import (
    OutreachPurpose,
    OutreachRecordingError,
    OutreachTarget,
)
from lingxi.core.outreach.welcome_card import WELCOME_CONTENT_KEY

REPOSITORY_ROOT = Path(__file__).parents[1]
SCRIPT = REPOSITORY_ROOT / "scripts" / "ops" / "outreach.py"
CORE_PACKAGE = REPOSITORY_ROOT / "src" / "lingxi" / "core" / "outreach"


def _load_script() -> Any:
    module_name = "outreach_ops_under_test"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # 先登记再执行：脚本用了 `from __future__ import annotations`，dataclass 解析
    # 延迟求值的字段注解时要在 sys.modules 里查到本模块。
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


TOOL = _load_script()

EMAIL_A = "joshua.wang@example.invalid"
EMAIL_B = "yiming.yi@example.invalid"
COMPANY_NAMES = {"1011": "尼日利亚"}
PERMISSIONS = '{"1011": ["充值金额", "日活用户数"]}'
ADMIN_OPEN_ID = "ou_admin_fake_for_tests"


def _facts(email: str = EMAIL_A, **overrides: Any) -> SubjectFacts:
    base: dict[str, Any] = {
        "email": email,
        "user_id": f"usr_{email.split('@')[0]}",
        "open_id": f"ou_{email.split('@')[0]}",
        "provisioning_state": "active",
        "account_state": "enabled",
        "permissions": PERMISSIONS,
        "roster_names": ("王晋 (Joshua Wang)",),
    }
    base.update(overrides)
    return SubjectFacts(**base)


def _recipients(*facts: SubjectFacts) -> tuple[Any, ...]:
    by_email = {item.email: item for item in facts}
    return TOOL.build_recipients(
        tuple(by_email),
        facts_for=lambda email: by_email[email],
        company_names=COMPANY_NAMES,
        total_company_count=43,
    )


def _write_roster(text: str) -> Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
    handle.write(text)
    handle.close()
    return Path(handle.name)


class FakeDispatcher:
    """记录每一次 ``deliver``；``errors`` 按邮箱注入异常。"""

    def __init__(self, *, errors: dict[str, BaseException] | None = None) -> None:
        self.calls: list[tuple[OutreachTarget, OutreachPurpose]] = []
        self._errors = errors or {}

    def deliver(self, target: OutreachTarget, *, purpose: OutreachPurpose) -> Any:
        self.calls.append((target, purpose))
        error = self._errors.get(target.audience.display_name)
        if error is not None:
            raise error
        return _Outcome()


class _Outcome:
    skipped = False
    status = "delivered"
    message_id = "om_fake"
    error_code = None


class _AlreadyDeliveredOutcome:
    skipped = True
    status = "delivered"
    message_id = None
    error_code = None


class DedupingDispatcher:
    """按幂等键主体折叠的假编排：同一个 ``subject`` 第二次就报"此前已送达"。

    它模拟的是真库那条 ``ON CONFLICT ... status = 'delivered'`` 守卫，用来证明一次
    预检里两个人**不会**落在同一个键上。
    """

    def __init__(self) -> None:
        self.calls: list[OutreachTarget] = []
        self._seen: set[str] = set()

    def deliver(self, target: OutreachTarget, *, purpose: OutreachPurpose) -> Any:
        self.calls.append(target)
        if target.subject in self._seen:
            return _AlreadyDeliveredOutcome()
        self._seen.add(target.subject)
        return _Outcome()


class RosterLoadingTest(unittest.TestCase):
    """名单形态写错当场拒、零发送。"""

    def test_an_email_only_roster_loads(self) -> None:
        path = _write_roster(f"email\n{EMAIL_A}\n{EMAIL_B}\n")
        self.assertEqual(TOOL.load_recipients(path), (EMAIL_A, EMAIL_B))

    def test_the_preprovision_roster_shape_still_loads(self) -> None:
        """同一份名单先预开通再主动告知，不必另存一份必然会漂移的副本。"""
        path = _write_roster(f"email,position,company_scope\n{EMAIL_A},A国家总经理,1011\n")
        self.assertEqual(TOOL.load_recipients(path), (EMAIL_A,))

    def test_the_email_is_normalised(self) -> None:
        path = _write_roster(f"email\n  {EMAIL_A.upper()} \n")
        self.assertEqual(TOOL.load_recipients(path), (EMAIL_A,))

    def test_a_roster_without_an_email_column_is_refused(self) -> None:
        path = _write_roster("mail\nsomeone@example.invalid\n")
        with self.assertRaises(TOOL.RosterError):
            TOOL.load_recipients(path)

    def test_a_blank_email_is_refused(self) -> None:
        path = _write_roster(f"email,position\n,{'A国家总经理'}\n")
        with self.assertRaises(TOOL.RosterError):
            TOOL.load_recipients(path)

    def test_a_duplicate_email_is_refused_after_normalisation(self) -> None:
        """否定断言：同一个人两行不猜哪一行为准，否则他会被发两次。"""
        path = _write_roster(f"email\n{EMAIL_A}\n{EMAIL_A.upper()}\n")
        with self.assertRaises(TOOL.RosterError):
            TOOL.load_recipients(path)

    def test_an_empty_roster_is_refused(self) -> None:
        path = _write_roster("email\n")
        with self.assertRaises(TOOL.RosterError):
            TOOL.load_recipients(path)

    def test_a_duplicate_email_header_is_refused_instead_of_taking_the_last_column(self) -> None:
        """否定断言：`email,email` 里"哪一列为准"没有答案。

        按后一列取值会把收件人静默换成另一个人——名单上写着甲，卡片发给乙，而清单
        看起来完全正常。
        """
        path = _write_roster(f"email,email\n{EMAIL_A},{EMAIL_B}\n")
        with self.assertRaises(TOOL.RosterError):
            TOOL.load_recipients(path)

    def test_a_case_folded_duplicate_header_is_refused_too(self) -> None:
        """归一后同名即重复：`Email` 与 ` email ` 是同一列。"""
        path = _write_roster(f"Email, email \n{EMAIL_A},{EMAIL_B}\n")
        with self.assertRaises(TOOL.RosterError):
            TOOL.load_recipients(path)

    def test_a_row_with_more_fields_than_the_header_is_refused(self) -> None:
        """否定断言：字段数对不上时"哪一格是邮箱"不可知，整份拒绝。"""
        path = _write_roster(f"email,position\n{EMAIL_A},A国家总经理,多出来的一格\n")
        with self.assertRaises(TOOL.RosterError):
            TOOL.load_recipients(path)

    def test_a_row_with_fewer_fields_than_the_header_is_refused(self) -> None:
        path = _write_roster(f"email,position\n{EMAIL_A}\n")
        with self.assertRaises(TOOL.RosterError):
            TOOL.load_recipients(path)

    def test_a_blank_email_with_extra_fields_is_a_roster_error_not_a_crash(self) -> None:
        """名单错误必须以名单错误的形态出现：traceback 会被读成"脚本坏了"。"""
        path = _write_roster("email,position\n,A国家总经理,多出来的一格\n")
        with self.assertRaises(TOOL.RosterError):
            TOOL.load_recipients(path)


class TargetTest(unittest.TestCase):
    def test_the_apply_target_goes_to_the_person_and_keys_on_the_user_id(self) -> None:
        recipient = _recipients(_facts())[0]
        target = TOOL.build_target(
            recipient, purpose=OutreachPurpose.APPLY, admin_open_id=None, run_id="run1"
        )
        self.assertEqual(target.recipient_open_id, recipient.facts.open_id)
        self.assertEqual(target.subject, recipient.facts.user_id)
        self.assertEqual(target.user_id, recipient.facts.user_id)

    def test_the_precheck_target_goes_to_the_admin_and_carries_the_run_id(self) -> None:
        """预检要能按样式反复做，因此幂等键带本次运行号，不是"一生一次"。"""
        recipient = _recipients(_facts())[0]
        target = TOOL.build_target(
            recipient, purpose=OutreachPurpose.PRECHECK, admin_open_id=ADMIN_OPEN_ID, run_id="run1"
        )
        self.assertEqual(target.recipient_open_id, ADMIN_OPEN_ID)
        self.assertEqual(target.subject, f"{ADMIN_OPEN_ID}:run1:{recipient.facts.user_id}")
        self.assertIsNone(target.user_id)

    def test_two_people_in_one_precheck_do_not_share_a_key(self) -> None:
        """否定断言：一次预检两行名单要发两张卡，不是一张。"""
        recipients = _recipients(_facts(), _facts(EMAIL_B, roster_names=("李四",)))
        subjects = {
            TOOL.build_target(
                item,
                purpose=OutreachPurpose.PRECHECK,
                admin_open_id=ADMIN_OPEN_ID,
                run_id="run1",
            ).subject
            for item in recipients
        }
        self.assertEqual(len(subjects), 2)

    def test_precheck_without_a_recipient_is_refused(self) -> None:
        recipient = _recipients(_facts())[0]
        with self.assertRaises(ValueError):
            TOOL.build_target(
                recipient, purpose=OutreachPurpose.PRECHECK, admin_open_id=None, run_id="run1"
            )

    def test_both_purposes_carry_the_very_same_audience_object(self) -> None:
        """完成标准 5 的行为半边：两档只差收件人，取值一模一样。"""
        recipient = _recipients(_facts())[0]
        apply_target = TOOL.build_target(
            recipient, purpose=OutreachPurpose.APPLY, admin_open_id=None, run_id="run1"
        )
        precheck_target = TOOL.build_target(
            recipient, purpose=OutreachPurpose.PRECHECK, admin_open_id=ADMIN_OPEN_ID, run_id="run1"
        )
        self.assertIs(apply_target.audience, precheck_target.audience)


class RunOutreachTest(unittest.TestCase):
    def test_a_person_who_is_not_sendable_is_skipped_without_any_send(self) -> None:
        recipients = _recipients(_facts(), _facts(EMAIL_B, provisioning_state="mcp_syncing"))
        dispatcher = FakeDispatcher()
        results = TOOL.run_outreach(
            recipients,
            dispatcher=dispatcher,
            purpose=OutreachPurpose.APPLY,
            admin_open_id=None,
            run_id="run1",
        )
        self.assertEqual(len(dispatcher.calls), 1)
        statuses = {item.email: item.status for item in results}
        self.assertEqual(statuses[EMAIL_A], "delivered")
        self.assertEqual(statuses[EMAIL_B], "skipped")

    def test_one_persons_failure_does_not_block_the_others(self) -> None:
        """逐人失败关闭：一个人的异常只让他自己计入失败。"""
        recipients = _recipients(_facts(), _facts(EMAIL_B, roster_names=("李四",)))
        dispatcher = FakeDispatcher(errors={"李四": RuntimeError("装配炸了")})
        results = TOOL.run_outreach(
            recipients,
            dispatcher=dispatcher,
            purpose=OutreachPurpose.APPLY,
            admin_open_id=None,
            run_id="run1",
        )
        statuses = {item.email: item.status for item in results}
        self.assertEqual(statuses[EMAIL_A], "delivered")
        self.assertEqual(statuses[EMAIL_B], "failed_RuntimeError")

    def test_a_failure_detail_never_carries_the_exception_text(self) -> None:
        recipients = _recipients(_facts(EMAIL_B, roster_names=("李四",)))
        dispatcher = FakeDispatcher(errors={"李四": RuntimeError(f"炸在 {EMAIL_B}")})
        results = TOOL.run_outreach(
            recipients,
            dispatcher=dispatcher,
            purpose=OutreachPurpose.APPLY,
            admin_open_id=None,
            run_id="run1",
        )
        self.assertNotIn(EMAIL_B, str(results[0].status))
        self.assertIsNone(results[0].detail)


class PrecheckFanOutTest(unittest.TestCase):
    """一次预检里每个人各一张卡，谁也不被当成"此前已送达"。"""

    def test_a_two_person_precheck_delivers_twice(self) -> None:
        recipients = _recipients(_facts(), _facts(EMAIL_B, roster_names=("李四",)))
        dispatcher = DedupingDispatcher()
        results = TOOL.run_outreach(
            recipients,
            dispatcher=dispatcher,
            purpose=OutreachPurpose.PRECHECK,
            admin_open_id=ADMIN_OPEN_ID,
            run_id="run1",
        )
        self.assertEqual(len(dispatcher.calls), 2)
        self.assertEqual({item.status for item in results}, {"delivered"})


class StateAtSendTest(unittest.TestCase):
    """真发之前重读一次状态：装配到发送之间被停用的人不发。"""

    def setUp(self) -> None:
        self.reads: list[str] = []

    def _reader(self, mapping: dict[str, tuple[str | None, str | None]]) -> Any:
        def reader(user_id: str) -> tuple[str | None, str | None]:
            self.reads.append(user_id)
            return mapping[user_id]

        return reader

    def _run(self, states, *, purpose=OutreachPurpose.APPLY):
        recipients = _recipients(_facts())
        dispatcher = FakeDispatcher()
        results = TOOL.run_outreach(
            recipients,
            dispatcher=dispatcher,
            purpose=purpose,
            admin_open_id=ADMIN_OPEN_ID,
            run_id="run1",
            state_at_send=self._reader({recipients[0].facts.user_id: states}),
        )
        return dispatcher, results

    def test_a_person_deactivated_after_assembly_is_skipped_without_any_send(self) -> None:
        """否定断言：装配那一刻是 active，按下发送时已经不是了——这一张不发。"""
        dispatcher, results = self._run(("mcp_syncing", "enabled"))
        self.assertEqual(dispatcher.calls, [])
        self.assertEqual(results[0].status, "skipped")
        self.assertEqual(results[0].detail, TOOL.SKIP_NOT_ACTIVE_AT_SEND)

    def test_an_account_disabled_after_assembly_is_skipped_too(self) -> None:
        dispatcher, results = self._run(("active", "disabled"))
        self.assertEqual(dispatcher.calls, [])
        self.assertEqual(results[0].detail, TOOL.SKIP_NOT_ACTIVE_AT_SEND)

    def test_a_person_who_vanished_from_app_user_is_skipped(self) -> None:
        dispatcher, results = self._run((None, None))
        self.assertEqual(dispatcher.calls, [])
        self.assertEqual(results[0].detail, TOOL.SKIP_NOT_ACTIVE_AT_SEND)

    def test_a_still_active_person_is_sent_to_after_one_reread(self) -> None:
        dispatcher, results = self._run(("active", "enabled"))
        self.assertEqual(len(dispatcher.calls), 1)
        self.assertEqual(results[0].status, "delivered")
        self.assertEqual(len(self.reads), 1)

    def test_precheck_never_consults_the_state_at_send(self) -> None:
        """预检的收件人是管理员本人，不是这个人；这道闸只属于正式发送。"""
        dispatcher, results = self._run(
            ("mcp_syncing", "enabled"), purpose=OutreachPurpose.PRECHECK
        )
        self.assertEqual(len(dispatcher.calls), 1)
        self.assertEqual(self.reads, [])
        self.assertEqual(results[0].status, "delivered")


class ExitCodeTest(unittest.TestCase):
    """退出码 3 与 2 分开：发出去了但收尾没做干净，不等于什么都没做。"""

    def test_a_clean_run_exits_zero(self) -> None:
        results = [TOOL.PersonResult(EMAIL_A, "delivered", "om_1")]
        with redirect_stderr(io.StringIO()):
            self.assertEqual(TOOL._exit_code(results, alert_error=None), 0)

    def test_an_alert_flush_failure_exits_three_and_says_sending_was_fine(self) -> None:
        results = [TOOL.PersonResult(EMAIL_A, "delivered", "om_1")]
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            code = TOOL._exit_code(results, alert_error="ConnectionError")
        self.assertEqual(code, 3)
        self.assertIn("已发送 1 条", buffer.getvalue())
        self.assertIn("仅告警投递失败", buffer.getvalue())

    def test_a_card_delivered_but_not_recorded_exits_three_with_its_message_id(self) -> None:
        """否定断言：不得让人以为零发送——message_id 与人工核对提示必须在输出里。"""
        results = [TOOL.PersonResult(EMAIL_A, TOOL.STATUS_DELIVERED_NOT_RECORDED, "om_real_1")]
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            code = TOOL._exit_code(results, alert_error=None)
        self.assertEqual(code, 3)
        self.assertIn("om_real_1", buffer.getvalue())
        self.assertIn("不要当成未发送", buffer.getvalue())

    def test_a_blowing_up_alert_flush_is_caught_and_named(self) -> None:
        class _Dispatcher:
            @staticmethod
            def run_once() -> None:
                raise ConnectionError("投不出去")

        class _Alerting:
            dispatcher = _Dispatcher()

        self.assertEqual(TOOL._flush_alerts(_Alerting()), "ConnectionError")

    def test_a_recording_error_is_not_reported_as_a_failed_send(self) -> None:
        """卡片已经在对方手里，把它记成 failed 会让人原样重跑。"""
        recipients = _recipients(_facts())
        dispatcher = FakeDispatcher(
            errors={
                "王晋 (Joshua Wang)": OutreachRecordingError(
                    record_id="omr_1", message_id="om_real_1"
                )
            }
        )
        results = TOOL.run_outreach(
            recipients,
            dispatcher=dispatcher,
            purpose=OutreachPurpose.APPLY,
            admin_open_id=None,
            run_id="run1",
        )
        self.assertEqual(results[0].status, TOOL.STATUS_DELIVERED_NOT_RECORDED)
        self.assertEqual(results[0].detail, "om_real_1")


class DryRunTest(unittest.TestCase):
    def test_the_listing_shows_what_the_person_would_see(self) -> None:
        recipients = _recipients(_facts())
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            TOOL.print_plan(recipients, delivered=frozenset())
        text = buffer.getvalue()
        self.assertIn(WELCOME_CONTENT_KEY, text)
        self.assertIn("王晋 (Joshua Wang)", text)
        self.assertIn("公司范围=尼日利亚", text)
        self.assertIn("指标数=2", text)
        self.assertIn("active=True", text)
        self.assertIn("未发过", text)

    def test_the_listing_marks_people_who_were_already_sent_to(self) -> None:
        recipients = _recipients(_facts())
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            TOOL.print_plan(recipients, delivered=frozenset({TOOL._apply_key(recipients[0])}))
        self.assertIn("已发过", buffer.getvalue())

    def test_a_skipped_person_shows_the_reason_instead_of_a_name(self) -> None:
        recipients = _recipients(_facts(provisioning_state="mcp_syncing"))
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            TOOL.print_plan(recipients, delivered=frozenset())
        self.assertIn("跳过=not_active", buffer.getvalue())


class FakeRegistryLookup:
    """假登记表：按 open_id 返回条目；``None`` 表示没有 active 条目。"""

    def __init__(self, entries: dict[str, Any]) -> None:
        self.calls: list[str] = []
        self._entries = entries

    def active_entry(self, *, open_id: str) -> Any:
        self.calls.append(open_id)
        return self._entries.get(open_id)


def _registry_entry(*, roles: frozenset[Any], status: str = "active") -> Any:
    from lingxi.core.admin.registry import AdminRegistryEntry

    return AdminRegistryEntry(
        feishu_open_id=ADMIN_OPEN_ID, label="化名管理员", roles=roles, entry_status=status
    )


def _all_roles() -> frozenset[Any]:
    from lingxi.core.admin.registry import AdminRole

    return frozenset(AdminRole)


class InitiatorGateTest(unittest.TestCase):
    """责任人闸：两条真发路径必须带一位生效的已登记管理员。"""

    def test_a_registered_active_admin_passes(self) -> None:
        lookup = FakeRegistryLookup({ADMIN_OPEN_ID: _registry_entry(roles=_all_roles())})
        self.assertTrue(TOOL.initiated_by_is_registered_admin(lookup, ADMIN_OPEN_ID))
        self.assertEqual(lookup.calls, [ADMIN_OPEN_ID])

    def test_an_unknown_open_id_is_refused(self) -> None:
        """否定断言：默认拒绝——不在名单里的未知对象必须被挡住。"""
        lookup = FakeRegistryLookup({})
        self.assertFalse(TOOL.initiated_by_is_registered_admin(lookup, "ou_nobody"))

    def test_a_partially_granted_admin_is_refused(self) -> None:
        """否定断言：三类角色没有全部授予的人不是管理员（判据来自 core，不在这里另写）。"""
        from lingxi.core.admin.registry import AdminRole

        lookup = FakeRegistryLookup(
            {ADMIN_OPEN_ID: _registry_entry(roles=frozenset({AdminRole.OPS_ADMIN}))}
        )
        self.assertFalse(TOOL.initiated_by_is_registered_admin(lookup, ADMIN_OPEN_ID))

    def test_a_revoked_entry_is_refused(self) -> None:
        lookup = FakeRegistryLookup(
            {ADMIN_OPEN_ID: _registry_entry(roles=_all_roles(), status="revoked")}
        )
        self.assertFalse(TOOL.initiated_by_is_registered_admin(lookup, ADMIN_OPEN_ID))

    def test_a_blank_initiator_is_refused_before_any_lookup(self) -> None:
        """否定断言：空白发起人在**读库之前**被挡住。

        靠"连库连不上顺带失败"通过是拿运气当闸：库一旦可达，一个纯空白的取值就会
        被送进管理员判定。
        """
        calls: list[str] = []

        def explode(dsn: str) -> Any:
            calls.append(dsn)
            raise AssertionError("空白发起人不该走到读库这一步")

        original = TOOL.resolve_admin_registry_lookup
        TOOL.resolve_admin_registry_lookup = explode
        try:
            rejection = TOOL._reject_initiator("postgresql://unused", "   ")
        finally:
            TOOL.resolve_admin_registry_lookup = original
        self.assertEqual(calls, [])
        self.assertIn("不能为空白", rejection or "")

    def test_an_unreadable_registry_fails_closed(self) -> None:
        """否定断言：分辨不出"不是管理员"与"库读不到"时，不放行——发出去不可撤回。"""

        def explode(_dsn: str) -> Any:
            raise RuntimeError("库炸了")

        original = TOOL.resolve_admin_registry_lookup
        TOOL.resolve_admin_registry_lookup = explode
        try:
            rejection = TOOL._reject_initiator("postgresql://unused", ADMIN_OPEN_ID)
        finally:
            TOOL.resolve_admin_registry_lookup = original
        self.assertIsNotNone(rejection)
        self.assertIn("管理员登记表不可读", rejection or "")

    def test_the_audit_wrapper_adds_the_initiator_to_every_row(self) -> None:
        """发起人落审计；``outreach_message`` 不为此新增列。"""
        rows: list[tuple[str, dict]] = []

        class _Sink:
            def record(self, action: str, /, **fields: object) -> None:
                rows.append((action, dict(fields)))

        TOOL._AuditWithInitiator(_Sink(), initiated_by=ADMIN_OPEN_ID).record(
            "outreach.delivered", status="delivered"
        )
        self.assertEqual(rows[0][1]["initiated_by"], ADMIN_OPEN_ID)


class ModeGuardTest(unittest.TestCase):
    """互斥开关与写入极性。"""

    def _reject(self, argv: list[str]) -> str | None:
        return TOOL._reject_conflicting_modes(TOOL._build_parser().parse_args(argv))

    def test_the_default_mode_is_dry_run(self) -> None:
        arguments = TOOL._build_parser().parse_args(["roster.csv"])
        self.assertFalse(arguments.apply)
        self.assertFalse(arguments.precheck)
        self.assertFalse(arguments.list)

    def test_apply_and_precheck_together_are_refused(self) -> None:
        self.assertIsNotNone(
            self._reject(
                [
                    "roster.csv",
                    "--apply",
                    "--precheck",
                    "--to",
                    "admin",
                    "--initiated-by",
                    ADMIN_OPEN_ID,
                ]
            )
        )

    def test_precheck_without_a_recipient_is_refused(self) -> None:
        self.assertIsNotNone(self._reject(["roster.csv", "--precheck"]))

    def test_apply_without_an_initiator_is_refused(self) -> None:
        """否定断言：没有责任人就不许真发。"""
        rejection = self._reject(["roster.csv", "--apply"])
        self.assertIsNotNone(rejection)
        self.assertIn("--initiated-by", rejection or "")

    def test_precheck_without_an_initiator_is_refused(self) -> None:
        rejection = self._reject(["roster.csv", "--precheck", "--to", "admin"])
        self.assertIsNotNone(rejection)
        self.assertIn("--initiated-by", rejection or "")

    def test_a_blank_initiator_does_not_count_as_given(self) -> None:
        rejection = self._reject(["roster.csv", "--apply", "--initiated-by", "   "])
        self.assertIsNotNone(rejection)

    def test_the_two_read_only_modes_do_not_require_an_initiator(self) -> None:
        """dry-run 与 --list 不发送任何东西，因此不要求责任人。"""
        self.assertIsNone(self._reject(["roster.csv"]))
        self.assertIsNone(self._reject(["--list"]))

    def test_a_roster_is_required_unless_listing(self) -> None:
        self.assertIsNotNone(self._reject([]))
        self.assertIsNone(self._reject(["--list"]))

    def test_a_non_positive_limit_is_a_parameter_error_not_an_empty_lookback(self) -> None:
        """否定断言：`--limit 0` 不是"回查零条"，是把参数写错了。

        让它退化成一次空清单，人会读成"库里真的没有记录"。
        """
        for value in ("0", "-1", "很多"):
            with self.subTest(value=value), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    TOOL._build_parser().parse_args(["--list", "--limit", value])
                self.assertEqual(raised.exception.code, 2)

    def test_a_positive_limit_still_parses(self) -> None:
        self.assertEqual(TOOL._build_parser().parse_args(["--list", "--limit", "5"]).limit, 5)

    def test_a_half_typed_apply_is_not_an_apply(self) -> None:
        """否定断言：`--a` 不得被当成 `--apply`——消息发出去不可撤回。"""
        with self.assertRaises(SystemExit):
            TOOL._build_parser().parse_args(["roster.csv", "--a"])


def _call_names(tree: ast.AST) -> list[str]:
    """一棵语法树里全部被调用的名字（含属性调用的属性名）。"""
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Name):
            names.append(function.id)
        elif isinstance(function, ast.Attribute):
            names.append(function.attr)
    return names


def _chain_call_names() -> list[str]:
    """整条主动发送链（core/outreach 全部模块 + 本脚本）里的全部调用名。"""
    sources = [*sorted(CORE_PACKAGE.glob("*.py")), SCRIPT]
    names: list[str] = []
    for path in sources:
        names.extend(_call_names(ast.parse(path.read_text(encoding="utf-8"))))
    return names


class SameSourceStaticTest(unittest.TestCase):
    """完成标准 5 的静态半边：预检与正式发送同一渲染函数、同一数据装配。

    这几条不是风格检查。真机预检的全部价值建立在"预检看到的就是将要发出去的那张
    卡"上；一旦有人为预检另写一份渲染或另装配一份数据，预检就退化成一次没有意义
    的往返，而这种退化在运行时完全看不出来——两条路径各自都能跑通。
    """

    def test_the_card_is_rendered_at_exactly_one_call_site(self) -> None:
        self.assertEqual(_chain_call_names().count("render_welcome_card"), 1)

    def test_the_audience_is_assembled_at_exactly_one_call_site(self) -> None:
        self.assertEqual(_chain_call_names().count("plan_outreach"), 1)

    def test_the_script_reaches_the_dispatcher_at_exactly_one_call_site(self) -> None:
        self.assertEqual(
            _call_names(ast.parse(SCRIPT.read_text(encoding="utf-8"))).count("deliver"), 1
        )

    def test_the_script_builds_targets_through_one_helper(self) -> None:
        self.assertEqual(
            _call_names(ast.parse(SCRIPT.read_text(encoding="utf-8"))).count("build_target"), 1
        )

    def test_the_script_never_imports_a_feishu_sdk_directly(self) -> None:
        """出站只走 adapters；脚本自己不碰协议细节。"""
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("lark_oapi", source)
        self.assertNotIn("import urllib", source)


if __name__ == "__main__":
    unittest.main()
