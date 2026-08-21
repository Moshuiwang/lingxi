"""正式首次开通编排的单元与契约用例（Epic D / S-D-02，Issue #65 / #160）。

认领断言：`V-开通-04`（失败不回退 `provisioning_state`）、`V-开通-11`（全部完成才宣告
成功并报出实际范围）、`V-开通-12`（确定性失败统一无权限且不建环境 / 不发布）、
`V-开通-13`（终态互斥、超时与内部故障不混淆、后置异常不改写）、`V-开通-14`（重试 / 并发 /
重入不重复创建或发布）。

这些用例**不连数据库、不发请求、不等十五分钟**：编排层的每一个协作者都是注入的假实现，
时钟与等待也是注入的。真实存取由各自适配器的真库用例负责，真实首次开通属 L4a。
"""

from __future__ import annotations

import threading
import unittest
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from lingxi.core.conversation.ports import RETRYABLE_REASONS, OnboardingState
from lingxi.core.identity.first_contact import (
    EmploymentStatus,
    IdentityRecordDraft,
    decide_first_contact,
    locate_by_open_id,
)
from lingxi.core.identity.onboarding_runner import (
    KEY_COMPLETED,
    KEY_DELEGATED_SUBJECT,
    KEY_INTERNAL_ERROR,
    KEY_NOT_AUTHORIZED,
    KEY_SUSPENDED,
    KEY_SYNCING,
    KEY_SYNC_TIMEOUT,
    STATE_ACTIVE,
    STATE_MCP_SYNCING,
    STATE_PROVISIONING,
    AutoOnboardingRunner,
    EnvironmentResult,
    draft_from_member,
    roster_row_for,
)
from lingxi.core.identity.org_snapshot import DirectoryAvailability, SnapshotMember
from lingxi.core.identity.provisioning import (
    ProvisioningRejection,
    ProvisioningResult,
    UserProvisioningStatus,
)
from lingxi.core.permission.mcp_readiness import ReadinessOutcome

UTC = timezone.utc
OPEN_ID = "ou_employee_1"
USER_ID = "usr_01HTEST"

MEMBER = SnapshotMember(
    tenant_key="tenant_a",
    member_key="mk_1",
    open_id=OPEN_ID,
    user_id="fu_1",
    union_id="on_1",
    display_name="王小明",
    display_name_locale="zh_cn",
    department_names=("销售部",),
)

EMPLOYED = EmploymentStatus(
    is_activated=True, is_exited=False, is_frozen=False, is_resigned=False, is_unjoin=False
)
FROZEN = EmploymentStatus(
    is_activated=True, is_exited=False, is_frozen=True, is_resigned=False, is_unjoin=False
)

ROSTER_ROWS: tuple[Mapping[str, Any], ...] = (
    {"personnel_id": "fu_1", "employee_no": "0012", "email": "Xiaoming@Example.com", "name": "王小明"},
)
GALAXY_USER_ROWS = ({"user_id": "g_1", "user_name": "0012", "email": "xiaoming@example.com"},)
ROLE_ROWS = ({"user_id": "g_1", "role_id": "r_1", "role_name": "销售分析师"},)
DATACOUNTRY_ROWS = ({"user_id": "g_1", "datacountry_id": "c_1"},)
COUNTRY_ROWS = ({"country_key": "c_1", "name": "Kenya", "name_cn": "肯尼亚", "boss_company_id": "88"},)
ROLE_FUNCTION_MAP = {"销售分析师": "销售分析"}


# ----------------------------------------------------------------------
# 假实现
# ----------------------------------------------------------------------


class FakeLookup:
    def __init__(self, availability: DirectoryAvailability, members: tuple[SnapshotMember, ...]) -> None:
        self.availability = availability
        self.members = members


class FakeDirectory:
    def __init__(
        self,
        *,
        availability: DirectoryAvailability = DirectoryAvailability.AVAILABLE,
        members: tuple[SnapshotMember, ...] = (MEMBER,),
        error: Exception | None = None,
    ) -> None:
        self._lookup = FakeLookup(availability, members)
        self._error = error

    def lookup(self, open_id: str) -> Any:
        if self._error is not None:
            raise self._error
        return self._lookup


class FakeEmployment:
    def __init__(self, status: EmploymentStatus | None = EMPLOYED, error: Exception | None = None) -> None:
        self._status = status
        self._error = error
        self.calls: list[tuple[str, str]] = []

    def status(self, *, tenant_key: str, open_id: str) -> EmploymentStatus | None:
        self.calls.append((tenant_key, open_id))
        if self._error is not None:
            raise self._error
        return self._status


class FakeRoster:
    def __init__(self, rows: Sequence[Mapping[str, Any]] | None = ROSTER_ROWS) -> None:
        self._rows = rows

    def rows(self) -> Sequence[Mapping[str, Any]] | None:
        return self._rows


class FakeGalaxySnapshot:
    user_rows = GALAXY_USER_ROWS
    country_rows = COUNTRY_ROWS

    def role_rows(self, galaxy_user_id: str) -> tuple[Mapping[str, Any], ...]:
        return tuple(row for row in ROLE_ROWS if row["user_id"] == galaxy_user_id)

    def datacountry_rows(self, galaxy_user_id: str) -> tuple[Mapping[str, Any], ...]:
        return tuple(row for row in DATACOUNTRY_ROWS if row["user_id"] == galaxy_user_id)


_UNSET = object()


class FakeGalaxy:
    """``snapshot=None`` 必须能表达"库里没有有效批次"，因此缺省用哨兵而不是 ``None``。"""

    def __init__(self, snapshot: Any = _UNSET) -> None:
        self._snapshot = FakeGalaxySnapshot() if snapshot is _UNSET else snapshot

    def load_current(self) -> Any:
        return self._snapshot


class FakeProvisioning:
    def __init__(self, result: ProvisioningResult | None = None) -> None:
        self.result = result or ProvisioningResult.created(USER_ID)
        self.requests: list[Any] = []

    def provision(self, request: Any) -> ProvisioningResult:
        self.requests.append(request)
        return self.result


class FakeUsers:
    """``status=None`` 必须能表达"刚建完档却读不回来"，因此缺省同样用哨兵。"""

    def __init__(self, status: Any = _UNSET, *, abort_result: bool = True) -> None:
        self.status = UserProvisioningStatus("enabled", "matching", 0) if status is _UNSET else status
        self.advanced: list[str] = []
        #: `_abort_if_stalled` 每一次调用的完整参数（Issue #282 §7.4「当场收口」）。
        self.aborted: list[tuple[str, tuple[str, ...], str]] = []
        self._abort_result = abort_result

    def read_status(self, user_id: str) -> UserProvisioningStatus | None:
        return self.status

    def advance_provisioning_state(self, user_id: str, *, to: str) -> bool:
        self.advanced.append(to)
        return True

    def abort_stalled_provisioning(
        self, *, user_id: str, expected_states: Sequence[str], reason: str
    ) -> bool:
        self.aborted.append((user_id, tuple(expected_states), reason))
        return self._abort_result


class FakeEnvironment:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.tokens: list[str] = []
        self._error = error

    def ensure(self, *, user_id: str, mcp_token: str) -> EnvironmentResult:
        if self._error is not None:
            raise self._error
        self.calls.append(user_id)
        self.tokens.append(mcp_token)
        return EnvironmentResult(created=True)


class FakeIssuedToken:
    # 形状合法的密文：base64(16B IV ‖ 32B 密文)。用真形状而不是随手一串字母，
    # 是因为 `PublishRow` 会在构造时判形状——判错了这条链会在发布那一步炸掉，
    # 而那正是"明文不进外部表格"那道防线。
    token_cipher = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8gISIjJCUmJygpKissLS4v"

    def reveal(self) -> str:
        return "plaintext-token-never-logged"


class FakeTokens:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.calls: list[str] = []

    def issue_token(self, user_id: str) -> Any:
        self.calls.append(user_id)
        if self._error is not None:
            raise self._error
        return FakeIssuedToken()


class FakeDecision:
    def __init__(self, *, enqueued: bool, permission_version: int, outbox_id: str) -> None:
        self.enqueued = enqueued
        self.permission_version = permission_version
        self.outbox_id = outbox_id


class FakeIntent:
    def __init__(self, status: str) -> None:
        self.status = status


class FakeDecisions:
    def __init__(self, *, enqueued: bool = True, statuses: Sequence[str] = ("published",)) -> None:
        self._enqueued = enqueued
        self._statuses = list(statuses)
        self.rows: list[Any] = []
        self.reasons: list[str] = []
        self.loads = 0

    def record_decision(self, *, user_id: str, row: Any, reason: str, decided_at: datetime) -> Any:
        self.rows.append(row)
        self.reasons.append(reason)
        return FakeDecision(enqueued=self._enqueued, permission_version=7, outbox_id="pub_1")

    def load(self, outbox_id: str) -> Any:
        self.loads += 1
        index = min(self.loads - 1, len(self._statuses) - 1)
        return FakeIntent(self._statuses[index])


class FakeSession:
    def __init__(self, outcome: ReadinessOutcome) -> None:
        self.outcome = outcome


class FakeReadiness:
    def __init__(self, outcome: ReadinessOutcome = ReadinessOutcome.READY) -> None:
        self._outcome = outcome
        self.bindings: list[Any] = []
        self.permissions: list[str] = []

    def confirm(self, binding: Any, *, permissions: str) -> Any:
        self.bindings.append(binding)
        self.permissions.append(permissions)
        return FakeSession(self._outcome)


class FakeNotifier:
    def __init__(self, error: Exception | None = None, fail_times: int | None = None) -> None:
        self.sent: list[tuple[str, str, Mapping[str, object], str]] = []
        self.attempts = 0
        self._error = error
        self._fail_times = fail_times

    def send(self, *, open_id: str, key: str, values: Mapping[str, object], dedupe_key: str) -> None:
        self.attempts += 1
        if self._error is not None and (
            self._fail_times is None or self.attempts <= self._fail_times
        ):
            raise self._error
        self.sent.append((open_id, key, dict(values), dedupe_key))

    def keys(self) -> list[str]:
        return [key for _, key, _, _ in self.sent]

    def terminal(self) -> tuple[str, str, Mapping[str, object], str]:
        return self.sent[-1]


class FakeLedger:
    def __init__(self, error: Exception | None = None) -> None:
        self.marked: list[str] = []
        self.released: list[str] = []
        self._error = error

    def mark_onboarding_dispatched(self, *, event_id: str) -> None:
        if self._error is not None:
            raise self._error
        self.marked.append(event_id)

    def release_onboarding_claim(self, *, event_id: str, claim_token=None) -> None:
        # 记下代次：**没有代次就不该放**（那会撤销别人的认领），这条由用例断言。
        self.released.append((event_id, claim_token))


class RecordingAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, object]]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, dict(fields)))

    def actions(self) -> list[str]:
        return [action for action, _ in self.records]

    def facts(self, action: str) -> dict[str, object]:
        for name, fields in self.records:
            if name == action:
                return fields
        raise AssertionError(f"没有记到 {action}：{self.actions()}")


class InlineExecutor:
    """同步跑掉提交的任务，让链路断言不依赖线程调度。"""

    def __init__(self, accept: bool = True) -> None:
        self.accept = accept
        self.submitted = 0

    def submit(self, task: Any) -> bool:
        self.submitted += 1
        if not self.accept:
            return False
        task()
        return True


class QueueingExecutor:
    """只登记不执行：用来证明 ``start`` 没有在调用线程上跑链。"""

    def __init__(self) -> None:
        self.tasks: list[Any] = []

    def submit(self, task: Any) -> bool:
        self.tasks.append(task)
        return True

    def run_all(self) -> None:
        while self.tasks:
            self.tasks.pop(0)()


def build_runner(**overrides: Any) -> tuple[AutoOnboardingRunner, dict[str, Any]]:
    parts: dict[str, Any] = {
        "directory": FakeDirectory(),
        "employment": FakeEmployment(),
        "roster": FakeRoster(),
        "galaxy": FakeGalaxy(),
        "provisioning": FakeProvisioning(),
        "users": FakeUsers(),
        "environment": FakeEnvironment(),
        "tokens": FakeTokens(),
        "decisions": FakeDecisions(),
        "readiness": FakeReadiness(),
        "notifier": FakeNotifier(),
        "ledger": FakeLedger(),
        "audit": RecordingAudit(),
    }
    parts.update({key: value for key, value in overrides.items() if key in parts})
    executor = overrides.get("executor") or InlineExecutor()
    parts["executor"] = executor
    runner = AutoOnboardingRunner(
        directory=parts["directory"],
        employment=parts["employment"],
        roster=parts["roster"],
        galaxy=parts["galaxy"],
        provisioning=parts["provisioning"],
        users=parts["users"],
        environment=parts["environment"],
        tokens=parts["tokens"],
        decisions=parts["decisions"],
        readiness=parts["readiness"],
        notifier=parts["notifier"],
        ledger=parts["ledger"],
        audit=parts["audit"],
        role_function_map=overrides.get("role_function_map", ROLE_FUNCTION_MAP),
        delegated_subject=overrides.get(
            "delegated_subject", lambda: overrides.get("delegated_subject_open_id", "ou_delegated")
        ),
        notify_attempts=overrides.get("notify_attempts", 3),
        publish_allowed=overrides.get("publish_allowed", lambda: True),
        submit=executor.submit,
        sleep=overrides.get("sleep", lambda seconds: None),
        clock=overrides.get("clock", lambda: datetime(2026, 8, 18, tzinfo=UTC)),
        should_stop=overrides.get("should_stop", lambda: False),
        publish_wait_seconds=overrides.get("publish_wait_seconds", 3.0),
    )
    return runner, parts


#: 认领代次的替身：真库里是那一次认领写进 ``onboarding_dispatched_at`` 的时刻。
CLAIM_TOKEN = "claim-1"


def run_once(**overrides: Any) -> tuple[dict[str, Any], Any]:
    runner, parts = build_runner(**overrides)
    result = runner.start(
        event_id="evt_1", open_id=OPEN_ID, trace_id="trace_1", claim_token=CLAIM_TOKEN
    )
    return parts, result


class HappyPathTests(unittest.TestCase):
    """`V-开通-11`：环境、发布与当前用户 MCP 确认全部完成之后才宣告成功并报出范围。"""

    def test_full_chain_reaches_active_and_reports_real_scope(self) -> None:
        parts, result = run_once()

        self.assertIs(result.state, OnboardingState.STARTED, "start 必须立刻返回 started")
        audit = parts["audit"]
        self.assertEqual(audit.facts("onboarding.result")["state"], "completed")
        # 合同的两条固定提示都要出现：进入同步等待时的「权限正在同步，预计最多需要
        # 十五分钟」，以及全部完成后带实际公司与职能的成功提示。
        self.assertEqual(parts["notifier"].keys(), [KEY_SYNCING, KEY_COMPLETED])
        _, key, values, dedupe = parts["notifier"].terminal()
        self.assertEqual(key, KEY_COMPLETED)
        self.assertEqual(values, {"company_name": "88", "function_name": "销售分析"})
        self.assertEqual(dedupe, "onboarding:evt_1")
        # 状态推进次序固定，`active` 只在就绪之后。
        self.assertEqual(parts["users"].advanced, [STATE_PROVISIONING, STATE_MCP_SYNCING, STATE_ACTIVE])
        self.assertEqual(parts["environment"].calls, [USER_ID])
        self.assertEqual(parts["decisions"].reasons, ["first_onboarding"])
        self.assertEqual(parts["ledger"].marked, ["evt_1"])

    def test_environment_is_created_before_the_permission_row_is_decided(self) -> None:
        """次序断言：先有环境再排发布意图。

        反过来会让一个"权限已经写出去、环境还没建"的用户被 MCP 认成可用，而 worker
        那边根本没有他的 `.mcp.json`。
        """

        order: list[str] = []

        class OrderedEnvironment(FakeEnvironment):
            def ensure(self, *, user_id: str, mcp_token: str) -> EnvironmentResult:
                order.append("environment")
                return super().ensure(user_id=user_id, mcp_token=mcp_token)

        class OrderedDecisions(FakeDecisions):
            def record_decision(self, **kwargs: Any) -> Any:
                order.append("publish")
                return super().record_decision(**kwargs)

        run_once(environment=OrderedEnvironment(), decisions=OrderedDecisions())
        self.assertEqual(order, ["environment", "publish"])

    def test_readiness_is_bound_to_the_user_and_the_published_version(self) -> None:
        parts, _ = run_once()
        binding = parts["readiness"].bindings[0]
        self.assertEqual(binding.user_id, USER_ID)
        self.assertEqual(binding.permission_version, 7)
        self.assertIn("销售分析", parts["readiness"].permissions[0])

    def test_the_syncing_notice_arrives_before_the_blocking_readiness_wait(self) -> None:
        """「权限正在同步，最多十五分钟」必须在**等待之前**发；等完再说等于没说。"""

        order: list[str] = []

        class OrderedNotifier(FakeNotifier):
            def send(self, **kwargs: Any) -> None:
                order.append(kwargs["key"])
                super().send(**kwargs)

        class OrderedReadiness(FakeReadiness):
            def confirm(self, binding: Any, *, permissions: str) -> Any:
                order.append("confirm")
                return super().confirm(binding, permissions=permissions)

        run_once(notifier=OrderedNotifier(), readiness=OrderedReadiness())
        self.assertEqual(order, [KEY_SYNCING, "confirm", KEY_COMPLETED])

    def test_the_progress_and_terminal_notices_do_not_dedupe_each_other(self) -> None:
        parts, _ = run_once()
        keys = {dedupe for _, _, _, dedupe in parts["notifier"].sent}
        self.assertEqual(len(keys), 2, "进度提示与终态是两个用途，各自一个去重键")

    def test_the_plaintext_token_never_reaches_the_audit_trail(self) -> None:
        parts, _ = run_once()
        secret = FakeIssuedToken().reveal()
        rendered = repr(parts["audit"].records)
        self.assertNotIn(secret, rendered, "令牌明文不得出现在审计里")
        self.assertIn(secret, parts["environment"].tokens, "明文只该到达用户环境写入口")


class ThreadingTests(unittest.TestCase):
    """#65 开工卡「共用线程复核」：链不得跑在调用线程上。"""

    def test_start_hands_off_and_does_not_run_the_chain_inline(self) -> None:
        executor = QueueingExecutor()
        runner, parts = build_runner(executor=executor)

        result = runner.start(event_id="evt_1", open_id=OPEN_ID, trace_id="trace_1")

        self.assertIs(result.state, OnboardingState.STARTED)
        self.assertEqual(parts["environment"].calls, [], "start 返回时链一步都还没跑")
        executor.run_all()
        self.assertEqual(parts["environment"].calls, [USER_ID])

    def test_a_second_start_for_the_same_person_does_not_open_a_second_chain(self) -> None:
        """`V-开通-14`：同一个人同一时刻只跑一条链。

        第二条**必须带可重试原因码**：它自己从来没被执行过，认领方要据此把认领放回去，
        否则那条事件永远没人再捞（认领即记账）。
        """

        executor = QueueingExecutor()
        runner, parts = build_runner(executor=executor)

        first = runner.start(event_id="evt_1", open_id=OPEN_ID, trace_id="t1")
        second = runner.start(event_id="evt_2", open_id=OPEN_ID, trace_id="t2")

        self.assertIs(first.state, OnboardingState.STARTED)
        self.assertIsNone(first.failure_reason)
        self.assertEqual(second.failure_reason, "already_running")
        self.assertIn(second.failure_reason, RETRYABLE_REASONS)
        self.assertEqual(len(executor.tasks), 1, "同一个人同一时刻只允许一条链")
        self.assertIn("onboarding.already_running", parts["audit"].actions())
        executor.run_all()
        self.assertEqual(parts["notifier"].keys(), [KEY_SYNCING, KEY_COMPLETED])

    def test_the_slot_is_released_after_the_chain_finishes(self) -> None:
        runner, parts = build_runner()
        runner.start(event_id="evt_1", open_id=OPEN_ID, trace_id="t1")
        runner.start(event_id="evt_2", open_id=OPEN_ID, trace_id="t2")
        self.assertEqual(
            parts["notifier"].keys(),
            [KEY_SYNCING, KEY_COMPLETED, KEY_SYNCING, KEY_COMPLETED],
            "上一条跑完之后同一个人可以再来",
        )

    def test_concurrent_starts_open_exactly_one_chain(self) -> None:
        executor = QueueingExecutor()
        runner, _ = build_runner(executor=executor)
        barrier = threading.Barrier(8)

        def go(index: int) -> None:
            barrier.wait()
            runner.start(event_id=f"evt_{index}", open_id=OPEN_ID, trace_id=f"t{index}")

        threads = [threading.Thread(target=go, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(executor.tasks), 1)

    def test_a_rejecting_executor_is_not_reported_as_accepted(self) -> None:
        runner, parts = build_runner(executor=InlineExecutor(accept=False))
        result = runner.start(event_id="evt_1", open_id=OPEN_ID, trace_id="t1")
        self.assertIs(result.state, OnboardingState.INTERNAL_ERROR)
        self.assertEqual(result.failure_reason, "executor_unavailable")
        self.assertIn("onboarding.rejected_by_executor", parts["audit"].actions())

    def test_stopping_declines_new_chains(self) -> None:
        runner, parts = build_runner(should_stop=lambda: True)
        result = runner.start(event_id="evt_1", open_id=OPEN_ID, trace_id="t1")
        self.assertIs(result.state, OnboardingState.INTERNAL_ERROR)
        self.assertIn("onboarding.start_declined_while_stopping", parts["audit"].actions())


class DeterministicRejectionTests(unittest.TestCase):
    """`V-开通-12`：确定性失败统一无权限终态，不建环境、不发布、不建待办。"""

    def _assert_unauthorized(self, parts: dict[str, Any], reason: str) -> None:
        self.assertEqual(parts["audit"].facts("onboarding.result")["state"], "not_authorized")
        self.assertEqual(parts["audit"].facts("onboarding.result")["failure_reason"], reason)
        self.assertEqual(parts["notifier"].keys(), [KEY_NOT_AUTHORIZED])
        self.assertEqual(parts["environment"].calls, [], "无权限终态不得创建用户环境")
        self.assertEqual(parts["decisions"].rows, [], "无权限终态不得排发布意图")
        self.assertEqual(parts["users"].advanced, [], "无权限终态不得推进开通状态")

    def test_not_located(self) -> None:
        parts, _ = run_once(directory=FakeDirectory(members=()))
        self._assert_unauthorized(parts, "not_located")

    def test_ambiguous_identity(self) -> None:
        parts, _ = run_once(directory=FakeDirectory(members=(MEMBER, MEMBER)))
        self._assert_unauthorized(parts, "ambiguous_identity")

    def test_not_employed(self) -> None:
        parts, _ = run_once(employment=FakeEmployment(FROZEN))
        self._assert_unauthorized(parts, "not_employed")

    def test_employment_undecidable(self) -> None:
        parts, _ = run_once(employment=FakeEmployment(None))
        self._assert_unauthorized(parts, "employment_unknown")

    def test_incomplete_profile(self) -> None:
        member = SnapshotMember(
            tenant_key="tenant_a",
            member_key="mk_1",
            open_id=OPEN_ID,
            user_id="fu_1",
            union_id="on_1",
            display_name="王小明",
            department_names=(),
        )
        parts, _ = run_once(directory=FakeDirectory(members=(member,)))
        self._assert_unauthorized(parts, "incomplete_profile")

    def test_roster_has_no_matching_row(self) -> None:
        parts, _ = run_once(roster=FakeRoster(rows=()))
        self.assertEqual(parts["audit"].facts("onboarding.result")["state"], "not_authorized")
        self.assertEqual(parts["environment"].calls, [])

    def test_duplicate_roster_rows_are_not_folded_into_a_success(self) -> None:
        """`V-开通-09`：同一人员 ID 多行即使字段相同也不是唯一原始记录。"""

        parts, _ = run_once(roster=FakeRoster(rows=ROSTER_ROWS + ROSTER_ROWS))
        self.assertEqual(parts["audit"].facts("onboarding.result")["state"], "not_authorized")
        self.assertEqual(parts["decisions"].rows, [])

    def test_no_supported_function_is_an_unauthorized_terminal(self) -> None:
        parts, _ = run_once(role_function_map={})
        self._assert_unauthorized(parts, "no_supported_function")

    def test_incomplete_identity_rejection_is_a_business_failure(self) -> None:
        rejected = ProvisioningResult.rejected(
            ProvisioningRejection.INCOMPLETE_IDENTITY, missing_fields=("department",)
        )
        parts, _ = run_once(provisioning=FakeProvisioning(rejected))
        self._assert_unauthorized(parts, "incomplete_identity")

    def test_delegated_subject_gets_its_own_frozen_text(self) -> None:
        parts, _ = run_once(delegated_subject_open_id=OPEN_ID)
        self.assertEqual(parts["notifier"].terminal()[1], KEY_DELEGATED_SUBJECT)
        self.assertEqual(parts["environment"].calls, [])

    def test_delegated_subject_rejected_by_the_write_side_takes_the_same_exit(self) -> None:
        rejected = ProvisioningResult.rejected(ProvisioningRejection.DELEGATED_SUBJECT)
        parts, _ = run_once(provisioning=FakeProvisioning(rejected))
        self.assertEqual(parts["notifier"].terminal()[1], KEY_DELEGATED_SUBJECT)


class InternalFaultTests(unittest.TestCase):
    """`V-开通-13`：本侧故障走 `LX-ONBOARD-001`，绝不冒充"没有银河权限"。"""

    def _assert_internal(self, parts: dict[str, Any], reason: str) -> None:
        self.assertEqual(parts["audit"].facts("onboarding.result")["state"], "internal_error")
        self.assertEqual(parts["audit"].facts("onboarding.result")["failure_reason"], reason)
        self.assertEqual(parts["notifier"].terminal()[1], KEY_INTERNAL_ERROR)

    def test_directory_unavailable(self) -> None:
        parts, _ = run_once(directory=FakeDirectory(availability=DirectoryAvailability.STALE))
        self._assert_internal(parts, "directory_unavailable")
        self.assertEqual(parts["employment"].calls, [], "资料不可用时不该再去读在职状态")

    def test_directory_read_failure_is_not_read_as_not_located(self) -> None:
        parts, _ = run_once(directory=FakeDirectory(error=RuntimeError("boom")))
        self._assert_internal(parts, "directory_read_failed_RuntimeError")

    def test_employment_read_failure_is_not_read_as_not_employed(self) -> None:
        parts, _ = run_once(employment=FakeEmployment(error=TimeoutError()))
        self._assert_internal(parts, "employment_read_failed_TimeoutError")

    def test_storage_integrity_is_not_reported_as_missing_galaxy_permission(self) -> None:
        """接口设计 §8.1：库把工号吞了，不能告诉用户去银河申请权限。"""

        rejected = ProvisioningResult.rejected(ProvisioningRejection.STORAGE_INTEGRITY)
        parts, _ = run_once(provisioning=FakeProvisioning(rejected))
        self._assert_internal(parts, "storage_integrity")
        self.assertNotEqual(parts["notifier"].terminal()[1], KEY_NOT_AUTHORIZED)

    def test_missing_roster_snapshot_is_our_gap_not_the_user_s(self) -> None:
        parts, _ = run_once(roster=FakeRoster(rows=None))
        self._assert_internal(parts, "roster_snapshot_missing")

    def test_missing_galaxy_batch_is_our_gap_not_the_user_s(self) -> None:
        parts, _ = run_once(galaxy=FakeGalaxy(snapshot=None))
        self._assert_internal(parts, "galaxy_batch_missing")

    def test_user_environment_failure_stops_before_publishing(self) -> None:
        parts, _ = run_once(environment=FakeEnvironment(error=PermissionError()))
        self._assert_internal(parts, "user_environment_failed_PermissionError")
        self.assertEqual(parts["decisions"].rows, [], "环境没建成不得发布权限")

    def test_token_issue_failure_stops_before_the_environment(self) -> None:
        parts, _ = run_once(tokens=FakeTokens(error=RuntimeError()))
        self._assert_internal(parts, "token_issue_failed_RuntimeError")
        self.assertEqual(parts["environment"].calls, [])

    def test_publish_that_never_completes_is_not_a_sync_timeout(self) -> None:
        parts, _ = run_once(decisions=FakeDecisions(statuses=("pending",)))
        self._assert_internal(parts, "publish_not_completed")
        self.assertNotEqual(parts["notifier"].terminal()[1], KEY_SYNC_TIMEOUT)

    def test_publish_failure_is_not_reported_as_success(self) -> None:
        parts, _ = run_once(decisions=FakeDecisions(statuses=("failed",)))
        self._assert_internal(parts, "publish_failed")
        self.assertEqual(parts["users"].advanced, [STATE_PROVISIONING])

    def test_superseded_publish_does_not_claim_this_onboarding_succeeded(self) -> None:
        parts, _ = run_once(decisions=FakeDecisions(statuses=("superseded",)))
        self._assert_internal(parts, "publish_superseded")

    def test_readiness_technical_failure_never_writes_active(self) -> None:
        parts, _ = run_once(readiness=FakeReadiness(ReadinessOutcome.TECHNICAL_FAILURE))
        self._assert_internal(parts, "readiness_technical_failure")
        self.assertNotIn(STATE_ACTIVE, parts["users"].advanced)

    def test_unexpected_exception_still_produces_a_user_conclusion(self) -> None:
        class Exploding(FakeUsers):
            def read_status(self, user_id: str) -> UserProvisioningStatus | None:
                raise ZeroDivisionError()

        parts, _ = run_once(users=Exploding())
        self._assert_internal(parts, "unexpected_ZeroDivisionError")


class SyncTimeoutTests(unittest.TestCase):
    """`V-开通-13`：十五分钟同步超时是**专用**终态，不与内部故障码混淆。"""

    def test_timed_out_uses_the_dedicated_text_and_stays_in_mcp_syncing(self) -> None:
        parts, _ = run_once(readiness=FakeReadiness(ReadinessOutcome.TIMED_OUT))
        self.assertEqual(parts["audit"].facts("onboarding.result")["state"], "sync_timeout")
        self.assertEqual(parts["notifier"].terminal()[1], KEY_SYNC_TIMEOUT)
        self.assertEqual(parts["users"].advanced, [STATE_PROVISIONING, STATE_MCP_SYNCING])
        self.assertNotIn(STATE_ACTIVE, parts["users"].advanced)

    def test_no_permission_after_a_granted_aggregate_is_an_internal_inconsistency(self) -> None:
        parts, _ = run_once(readiness=FakeReadiness(ReadinessOutcome.NO_PERMISSION))
        self.assertEqual(
            parts["audit"].facts("onboarding.result")["failure_reason"],
            "readiness_no_permission_after_grant",
        )
        self.assertEqual(parts["notifier"].terminal()[1], KEY_INTERNAL_ERROR)


class RecheckBeforeContinuingTests(unittest.TestCase):
    """接口设计 §8.1：`already_provisioned` 不等于"这个人现在还该被开通"。"""

    def test_a_suspended_account_stops_before_the_environment_and_the_publish(self) -> None:
        users = FakeUsers(UserProvisioningStatus("suspended", "matching", 0))
        parts, _ = run_once(
            users=users, provisioning=FakeProvisioning(ProvisioningResult.already_provisioned(USER_ID))
        )
        self.assertEqual(parts["notifier"].terminal()[1], KEY_SUSPENDED)
        self.assertEqual(parts["environment"].calls, [])
        self.assertEqual(parts["decisions"].rows, [])
        self.assertEqual(users.advanced, [])
        self.assertIn("onboarding.halted_account_state", parts["audit"].actions())

    def test_an_already_active_user_is_not_provisioned_a_second_time(self) -> None:
        """已 active：不重复建环境、不重复发布，但**照常通知**。

        这条路径正是"上一次结论没送到、被重新认领"的收敛出口——不通知就等于把它烧掉。
        重复推送由绑定事件的去重键挡住（同一条事件的两次执行用同一个键）。
        """

        users = FakeUsers(UserProvisioningStatus("enabled", "active", 3))
        parts, _ = run_once(
            users=users, provisioning=FakeProvisioning(ProvisioningResult.already_provisioned(USER_ID))
        )
        self.assertEqual(parts["environment"].calls, [], "已 active 的人不得重复创建环境")
        self.assertEqual(parts["decisions"].rows, [], "已 active 的人不得重复发布权限")
        self.assertEqual(parts["notifier"].keys(), [KEY_COMPLETED])
        self.assertEqual(
            parts["notifier"].terminal()[2],
            {"company_name": "88", "function_name": "销售分析"},
            "范围取本轮已经算出来的那一份，不凭空编",
        )
        self.assertEqual(parts["notifier"].terminal()[3], "onboarding:evt_1")
        self.assertIn("onboarding.already_active", parts["audit"].actions())
        self.assertEqual(parts["ledger"].marked, ["evt_1"], "账仍然要记上")

    def test_already_provisioned_and_still_enabled_continues_normally(self) -> None:
        parts, _ = run_once(
            provisioning=FakeProvisioning(ProvisioningResult.already_provisioned(USER_ID))
        )
        self.assertEqual(parts["environment"].calls, [USER_ID])
        self.assertEqual(parts["notifier"].terminal()[1], KEY_COMPLETED)

    def test_a_vanished_user_row_is_never_read_as_permission_to_continue(self) -> None:
        parts, _ = run_once(users=FakeUsers(status=None))
        self.assertEqual(
            parts["audit"].facts("onboarding.result")["failure_reason"], "user_row_disappeared"
        )
        self.assertEqual(parts["environment"].calls, [])


class NotificationAndLedgerTests(unittest.TestCase):
    """对账通知方案：编排自担通知 + 幂等键；账在通知之后才记。"""

    def test_the_dedupe_key_is_bound_to_the_event(self) -> None:
        parts, _ = run_once()
        self.assertEqual(parts["notifier"].terminal()[3], "onboarding:evt_1")

    def test_a_failed_notification_does_not_rewrite_the_terminal_state(self) -> None:
        parts, _ = run_once(notifier=FakeNotifier(error=RuntimeError()))
        self.assertEqual(parts["audit"].facts("onboarding.result")["state"], "completed")
        self.assertIn("onboarding.notify_failed", parts["audit"].actions())

    def test_a_transient_notification_failure_is_retried(self) -> None:
        """一次飞书抖动不该让用户永远停在「已收到」。"""

        notifier = FakeNotifier(error=RuntimeError(), fail_times=1)
        parts, _ = run_once(notifier=notifier)

        self.assertEqual(notifier.keys()[-1], KEY_COMPLETED)
        self.assertEqual(parts["ledger"].released, [], "重试成功就不该放回认领")
        self.assertEqual(parts["ledger"].marked, ["evt_1"])

    def test_an_undeliverable_conclusion_puts_the_claim_back(self) -> None:
        """通知反复送不到 → 放回认领，让下一轮把整条链重跑一遍。

        不放回就等于"系统认为处理完了、用户什么都没收到"，而认领即记账，那条事件此后
        再也没人捞得到。
        """

        parts, _ = run_once(notifier=FakeNotifier(error=RuntimeError()))

        self.assertEqual(
            parts["ledger"].released,
            [("evt_1", CLAIM_TOKEN)],
            "释放必须带上认领代次，否则会撤销别人的认领（ABA）",
        )
        self.assertEqual(parts["ledger"].marked, [], "放回之后不得同时记账")
        self.assertIn(
            "onboarding.claim_released_after_notify_failed", parts["audit"].actions()
        )

    def test_the_claim_is_put_back_at_most_once_per_event(self) -> None:
        """放回有上限：一次飞书长时间不可用不得把执行器永久占满。"""

        runner, parts = build_runner(notifier=FakeNotifier(error=RuntimeError()))
        runner.start(
            event_id="evt_1", open_id=OPEN_ID, trace_id="t1", claim_token=CLAIM_TOKEN
        )
        runner.start(
            event_id="evt_1", open_id=OPEN_ID, trace_id="t1", claim_token=CLAIM_TOKEN
        )

        self.assertEqual(parts["ledger"].released, [("evt_1", CLAIM_TOKEN)])
        self.assertEqual(parts["ledger"].marked, ["evt_1"], "第二次记账收口")
        self.assertIn("onboarding.notify_gave_up_failed", parts["audit"].actions())

    def test_a_failed_ledger_write_does_not_take_the_user_conclusion_with_it(self) -> None:
        parts, _ = run_once(ledger=FakeLedger(error=RuntimeError()))
        self.assertEqual(parts["notifier"].keys(), [KEY_SYNCING, KEY_COMPLETED])
        self.assertIn("onboarding.dispatch_record_failed", parts["audit"].actions())

    def test_the_ledger_is_marked_after_the_user_has_been_told(self) -> None:
        order: list[str] = []

        class OrderedNotifier(FakeNotifier):
            def send(self, **kwargs: Any) -> None:
                order.append("notify")
                super().send(**kwargs)

        class OrderedLedger(FakeLedger):
            def mark_onboarding_dispatched(self, *, event_id: str) -> None:
                order.append("ledger")
                super().mark_onboarding_dispatched(event_id=event_id)

        run_once(notifier=OrderedNotifier(), ledger=OrderedLedger())
        self.assertEqual(order, ["notify", "notify", "ledger"])


class PublishGateTests(unittest.TestCase):
    """翻译层不可用时**一条发布意图都不排**（与 Issue #227 的整轮判据同一条纪律）。

    本编排是 `record_decision` 的第三个调用点，不自己带闸就是那条判据的绕行入口——
    而绕过去的后果是往正式权限表写一行值列表还是职能标签、不是指标名的记录。
    """

    def test_a_closed_gate_stops_before_provisioning(self) -> None:
        parts, _ = run_once(publish_allowed=lambda: False)

        self.assertEqual(parts["provisioning"].requests, [], "闸门关着时连档都不建")
        self.assertEqual(parts["environment"].calls, [])
        self.assertEqual(parts["decisions"].rows, [], "一条发布意图都不排")
        self.assertIn("onboarding.publish_gate_closed", parts["audit"].actions())

    def test_a_closed_gate_is_an_internal_fault_not_a_missing_permission(self) -> None:
        """说成「没有银河权限」会把一个权限完全正常的人引去银河申请。"""

        parts, _ = run_once(publish_allowed=lambda: False)

        self.assertEqual(parts["notifier"].terminal()[1], KEY_INTERNAL_ERROR)
        self.assertEqual(
            parts["audit"].facts("onboarding.result")["failure_reason"],
            "permission_translation_unavailable",
        )

    def test_a_missing_intent_is_distinguishable_from_a_failed_publish(self) -> None:
        """「本轮没排出这一条」与「排了但发布失败」原因码必须分得开。"""

        class MissingIntent(FakeDecisions):
            def load(self, outbox_id: str) -> Any:
                self.loads += 1
                return None

        parts, _ = run_once(decisions=MissingIntent())

        self.assertEqual(
            parts["audit"].facts("onboarding.result")["failure_reason"], "publish_intent_missing"
        )

    def test_the_gate_cannot_be_left_out(self) -> None:
        """缺省放行等于把一次配置缺失变成一次真实的错误发布，而外部表不可回滚。"""

        with self.assertRaises(TypeError):
            build_runner(publish_allowed=None)


class ShutdownTests(unittest.TestCase):
    """停机落在链的中途：**不通知、不记账、把认领放回去**。

    不能当成一次失败终态告诉用户——那会在每次滚动部署时给正在开通的人推一条
    `LX-ONBOARD-001`，而他其实什么问题都没有，下一轮就会被重新捞起来跑完。
    """

    def test_a_stop_between_steps_aborts_and_releases(self) -> None:
        stops = {"value": False}

        class StoppingEnvironment(FakeEnvironment):
            def ensure(self, *, user_id: str, mcp_token: str) -> EnvironmentResult:
                stops["value"] = True
                return super().ensure(user_id=user_id, mcp_token=mcp_token)

        parts, _ = run_once(
            environment=StoppingEnvironment(), should_stop=lambda: stops["value"]
        )

        self.assertEqual(parts["notifier"].sent, [], "停机中止不得给用户任何结论")
        self.assertEqual(parts["ledger"].marked, [], "停机中止不得记账")
        self.assertEqual(
            parts["ledger"].released, [("evt_1", CLAIM_TOKEN)], "认领必须放回去"
        )
        self.assertIn("onboarding.aborted_while_stopping", parts["audit"].actions())
        self.assertEqual(parts["decisions"].rows, [], "停机之后不再排新的发布意图")

    def test_a_queued_chain_that_starts_after_the_stop_aborts_immediately(self) -> None:
        """已经排队、停机之后才被取到的那一条：第一步就中止并放回。"""

        parts, _ = run_once(should_stop=lambda: True, executor=InlineExecutor())

        # `start` 在停机中直接不受理，原因码可重试。
        self.assertIn("onboarding.start_declined_while_stopping", parts["audit"].actions())
        self.assertEqual(parts["environment"].calls, [])

    def test_a_stop_during_the_publish_wait_is_not_an_internal_fault(self) -> None:
        """停机不是"发布没完成"：那一版意图仍然有效，下一轮重跑会等到它。"""

        state = {"stopping": False}

        def sleep(seconds: float) -> None:
            state["stopping"] = True

        parts, _ = run_once(
            decisions=FakeDecisions(statuses=("pending",)),
            sleep=sleep,
            should_stop=lambda: state["stopping"],
        )

        self.assertEqual(parts["notifier"].sent, [])
        self.assertEqual(parts["ledger"].released, [("evt_1", CLAIM_TOKEN)])
        self.assertNotIn("onboarding.result", parts["audit"].actions())


class ActiveWriteTests(unittest.TestCase):
    """写 `active` 之前要再复核一次，而且推进结果不能忽略（`V-开通-04`）。"""

    def test_an_account_suspended_during_the_wait_is_not_written_active(self) -> None:
        """从建档后那次复核到就绪最长隔十七分钟，管理员在这段时间停用账号是真实形状。"""

        class ChangingUsers(FakeUsers):
            def __init__(self) -> None:
                super().__init__()
                self.reads = 0

            def read_status(self, user_id: str) -> UserProvisioningStatus | None:
                self.reads += 1
                if self.reads == 1:
                    return UserProvisioningStatus("enabled", "matching", 0)
                return UserProvisioningStatus("suspended", "mcp_syncing", 7)

        users = ChangingUsers()
        parts, _ = run_once(users=users)

        self.assertEqual(parts["notifier"].terminal()[1], KEY_SUSPENDED)
        self.assertNotIn(STATE_ACTIVE, users.advanced)
        self.assertEqual(
            parts["audit"].facts("onboarding.result")["failure_reason"], "account_not_enabled"
        )

    def test_a_refused_state_advance_is_never_reported_as_success(self) -> None:
        """条件更新影响 0 行 = 当前状态不允许被推到 active。忽略它就是对用户说假话。"""

        class RefusingUsers(FakeUsers):
            def advance_provisioning_state(self, user_id: str, *, to: str) -> bool:
                self.advanced.append(to)
                return to != STATE_ACTIVE

        parts, _ = run_once(users=RefusingUsers())

        self.assertEqual(parts["notifier"].terminal()[1], KEY_INTERNAL_ERROR)
        self.assertEqual(
            parts["audit"].facts("onboarding.result")["failure_reason"], "state_advance_refused"
        )
        self.assertIn("onboarding.state_advance_refused_failed", parts["audit"].actions())


class StalledAbortTests(unittest.TestCase):
    """编排层「当场收口」（Issue #282 §7.4，`V-开通-19` 的一半）：链把用户推进到
    `provisioning`（分水岭）之后遇到任何非 `SYNC_TIMEOUT`/`COMPLETED` 终态，都要当场
    把状态收口成 `aborted`，不必等停摆扫描的 45 分钟租约。"""

    def test_a_publish_failure_after_the_watershed_is_collapsed_at_once(self) -> None:
        parts, _ = run_once(decisions=FakeDecisions(statuses=("failed",)))
        self.assertEqual(
            parts["users"].aborted,
            [(USER_ID, (STATE_PROVISIONING, STATE_MCP_SYNCING), "publish_failed")],
        )

    def test_a_publish_not_completed_timeout_after_the_watershed_is_collapsed(self) -> None:
        parts, _ = run_once(decisions=FakeDecisions(statuses=("pending",)))
        self.assertEqual(len(parts["users"].aborted), 1)
        self.assertEqual(parts["users"].aborted[0][2], "publish_not_completed")

    def test_a_readiness_technical_failure_after_the_watershed_is_collapsed(self) -> None:
        parts, _ = run_once(readiness=FakeReadiness(ReadinessOutcome.TECHNICAL_FAILURE))
        self.assertEqual(len(parts["users"].aborted), 1)
        self.assertEqual(parts["users"].aborted[0][2], "readiness_technical_failure")

    def test_a_no_permission_after_grant_inconsistency_is_collapsed(self) -> None:
        """Issue #282 §0.2 明确点名的洞：`mcp_syncing` 上从未判过 `timed_out` 的失败，
        此前既不在迟到就绪恢复的候选集合里，也没有任何东西会回来看。"""

        parts, _ = run_once(readiness=FakeReadiness(ReadinessOutcome.NO_PERMISSION))
        self.assertEqual(len(parts["users"].aborted), 1)
        self.assertEqual(parts["users"].aborted[0][2], "readiness_no_permission_after_grant")

    def test_a_refused_state_advance_is_collapsed(self) -> None:
        """Issue #282 §0.2 同一张表里点名的另一个洞：`state_advance_refused`。"""

        class RefusingUsers(FakeUsers):
            def advance_provisioning_state(self, user_id: str, *, to: str) -> bool:
                self.advanced.append(to)
                return to != STATE_ACTIVE

        users = RefusingUsers()
        parts, _ = run_once(users=users)
        self.assertEqual(len(users.aborted), 1)
        self.assertEqual(users.aborted[0][2], "state_advance_refused")

    def test_failures_before_the_watershed_are_never_collapsed(self) -> None:
        """身份定位、匹配、令牌签发、用户环境四类失败发生在把用户推进到 `provisioning`
        之前——它们本来就会自然停在 `matching`/`guest`，不在 `_PROVISIONING_IN_FLIGHT`
        里，管线下一条消息自然重试（Issue #282 §0.1：卡住的判据不是失败，是失败发生在
        分水岭之后）。这里没有一个确定的、已经越过分水岭的 `user_id`，因此绝不能尝试
        收口。"""

        cases: list[dict[str, Any]] = [
            {"directory": FakeDirectory(availability=DirectoryAvailability.STALE)},
            {"roster": FakeRoster(rows=None)},
            {"tokens": FakeTokens(error=RuntimeError())},
            {"environment": FakeEnvironment(error=PermissionError())},
        ]
        for overrides in cases:
            with self.subTest(overrides=sorted(overrides)):
                parts, _ = run_once(**overrides)
                self.assertEqual(parts["users"].aborted, [])

    def test_sync_timeout_is_never_collapsed(self) -> None:
        """**否定断言**：`SYNC_TIMEOUT` 归 `V-开通-18`（迟到就绪恢复）继续等待，绝不能
        被本链的「当场收口」抢走——抢走会让 `provisioning_state` 提前变成 `aborted`，
        迟到就绪恢复的候选查询立刻少了一个人，而那个人本来可能几分钟后就真的就绪。"""

        parts, _ = run_once(readiness=FakeReadiness(ReadinessOutcome.TIMED_OUT))
        self.assertEqual(parts["users"].aborted, [])
        self.assertEqual(parts["users"].advanced, [STATE_PROVISIONING, STATE_MCP_SYNCING])

    def test_a_completed_chain_is_never_collapsed(self) -> None:
        parts, _ = run_once()
        self.assertEqual(parts["users"].aborted, [])
        self.assertEqual(parts["notifier"].terminal()[1], KEY_COMPLETED)

    def test_abort_failure_does_not_break_the_rest_of_execute(self) -> None:
        """收口写口本身抛异常：只记一条响亮审计，不阻止终态通知与记账收口——收口失败
        不改写已经决定的终态。"""

        class ExplodingAbort(FakeUsers):
            def abort_stalled_provisioning(
                self, *, user_id: str, expected_states: Sequence[str], reason: str
            ) -> bool:
                raise RuntimeError("boom")

        parts, _ = run_once(
            users=ExplodingAbort(), decisions=FakeDecisions(statuses=("failed",))
        )
        self.assertIn("onboarding.stalled_abort_failed", parts["audit"].actions())
        self.assertEqual(parts["ledger"].marked, ["evt_1"])
        self.assertEqual(parts["notifier"].terminal()[1], KEY_INTERNAL_ERROR)

    def test_a_cas_that_finds_zero_rows_still_lets_the_chain_finish_normally(self) -> None:
        """CAS 返回 `False`（状态在候选查到与收口之间被别的路径改写）不是错误，链照常
        收口——`abort_stalled_provisioning` 的 0 行结果不需要特殊处理。"""

        parts, _ = run_once(
            users=FakeUsers(abort_result=False), decisions=FakeDecisions(statuses=("failed",))
        )
        self.assertEqual(len(parts["users"].aborted), 1)
        self.assertEqual(parts["ledger"].marked, ["evt_1"])


class UnchangedPublishTests(unittest.TestCase):
    """`UNCHANGED` 也要等 `published`（修复包 P2-并发-4）。"""

    def test_an_unchanged_decision_still_waits_for_the_row_to_be_written(self) -> None:
        parts, _ = run_once(decisions=FakeDecisions(enqueued=False, statuses=("pending",)))

        self.assertGreater(parts["decisions"].loads, 0, "UNCHANGED 不得跳过发布等待")
        self.assertEqual(
            parts["audit"].facts("onboarding.result")["failure_reason"],
            "publish_not_completed",
            "发布面根本没跑不能表现成十五分钟的 MCP 同步超时",
        )

    def test_an_unchanged_but_already_published_decision_costs_no_extra_wait(self) -> None:
        parts, _ = run_once(decisions=FakeDecisions(enqueued=False, statuses=("published",)))

        self.assertEqual(parts["decisions"].loads, 1)
        self.assertEqual(parts["notifier"].terminal()[1], KEY_COMPLETED)


class PureFunctionTests(unittest.TestCase):
    def test_the_orchestration_draft_matches_the_decision_layer_field_for_field(self) -> None:
        """判定层说"资料齐了"和实际写进去的资料必须是同一份。"""

        decision = decide_first_contact(
            open_id=OPEN_ID,
            location=locate_by_open_id(OPEN_ID, (MEMBER,)),
            employment=EMPLOYED,
            directory=DirectoryAvailability.AVAILABLE,
            delegated_subject_open_id="ou_delegated",
        )
        self.assertIsInstance(decision.draft, IdentityRecordDraft)
        self.assertEqual(draft_from_member(MEMBER), decision.draft)

    def test_multiple_roster_rows_never_pick_one(self) -> None:
        self.assertIsNone(roster_row_for("fu_1", list(ROSTER_ROWS) * 2))
        self.assertEqual(roster_row_for("fu_1", ROSTER_ROWS), ROSTER_ROWS[0])
        self.assertIsNone(roster_row_for("fu_missing", ROSTER_ROWS))

    def test_the_archived_roster_values_are_written_verbatim(self) -> None:
        """存档写花名册**原值**（大小写不折叠），不是匹配用的归一值。"""

        parts, _ = run_once()
        request = parts["provisioning"].requests[0]
        self.assertEqual(request.employee_no, "0012")
        self.assertEqual(request.email, "Xiaoming@Example.com")

    def test_the_published_row_normalises_the_email_but_the_archive_does_not(self) -> None:
        parts, _ = run_once()
        row = parts["decisions"].rows[0]
        self.assertEqual(row.record_key, "xiaoming@example.com")
        self.assertEqual(row.email, "xiaoming@example.com")
        self.assertEqual(row.name, "王小明")


class ConstructionTests(unittest.TestCase):
    """两个注入口缺省就会静默改变行为，因此在类型层就不给这个选项。"""

    def _parts(self) -> dict:
        return dict(
            directory=FakeDirectory(),
            employment=FakeEmployment(),
            roster=FakeRoster(),
            galaxy=FakeGalaxy(),
            provisioning=FakeProvisioning(),
            users=FakeUsers(),
            environment=FakeEnvironment(),
            tokens=FakeTokens(),
            decisions=FakeDecisions(),
            readiness=FakeReadiness(),
            notifier=FakeNotifier(),
            ledger=FakeLedger(),
            audit=RecordingAudit(),
            role_function_map=ROLE_FUNCTION_MAP,
            delegated_subject=lambda: None,
            publish_allowed=lambda: True,
        )

    def test_a_missing_executor_is_refused_at_construction(self) -> None:
        """没有执行器就只能在调用线程上跑链，而那正是共用线程复核要消灭的形状。"""

        with self.assertRaises(TypeError):
            AutoOnboardingRunner(
                submit=None, sleep=lambda seconds: None, **self._parts()  # type: ignore[arg-type]
            )

    def test_a_missing_sleep_is_refused_at_construction(self) -> None:
        with self.assertRaises(TypeError):
            AutoOnboardingRunner(
                submit=lambda task: True, sleep=None, **self._parts()  # type: ignore[arg-type]
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
