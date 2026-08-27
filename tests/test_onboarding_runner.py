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
from lingxi.core.identity.innertest_roster_gate import is_open_id_innertest_allowed
from lingxi.core.identity.onboarding_runner import (
    KEY_COMPLETED,
    KEY_DELEGATED_SUBJECT,
    KEY_INNERTEST_NOT_OPEN,
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
from lingxi.core.identity.stock_token_source import (
    ADOPTABLE,
    DECRYPT_FAILED,
    NO_CIPHER,
    NO_ROW,
    StockTokenLookup,
)
from lingxi.core.permission.legacy_source import REASON_LEGACY_READ_FAILED
from lingxi.core.permission.local_override import LocalPermissionOverrideEntry, OverrideDirection
from lingxi.core.permission.mcp_readiness import ReadinessOutcome
from lingxi.core.permission.merge_sources import REASON_LOCAL_OVERRIDE_READ_FAILED
from lingxi.core.permission.publish import ExistingPermissionRow

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
        #: 内测名单闸（Issue #302 S-N-01）用它证明"挡在最前面"：闸拒绝时这里必须
        #: 保持空——组织快照读取压根不该发生。
        self.calls: list[str] = []

    def lookup(self, open_id: str) -> Any:
        self.calls.append(open_id)
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
        #: 一个可读的"这个人现在停在哪一格"投影，只随 `advance_provisioning_state`/
        #: `abort_stalled_provisioning` 更新（不影响 `read_status` 返回的固定夹具，
        #: 那个用来控制 `_recheck_still_provisionable` 的判定）。给 P2-1 那类
        #: "通知没送到绝不能改状态"的用例一个可以直接断言的落点。
        self.current_state: str = self.status.provisioning_state if self.status else "matching"

    def read_status(self, user_id: str) -> UserProvisioningStatus | None:
        return self.status

    def advance_provisioning_state(self, user_id: str, *, to: str) -> bool:
        self.advanced.append(to)
        self.current_state = to
        return True

    def abort_stalled_provisioning(
        self, *, user_id: str, expected_states: Sequence[str], reason: str
    ) -> bool:
        self.aborted.append((user_id, tuple(expected_states), reason))
        if self._abort_result:
            self.current_state = "aborted"
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
    created = True

    def reveal(self) -> str:
        return "plaintext-token-never-logged"


class FakeAdoptedToken(FakeIssuedToken):
    """``adopt_token`` 路径的假结果：``created`` 可控，``reveal()`` 原样返回调用方
    传入的候选明文——用来在测试里观察"库里已有则返回既有那份"这条语义。"""

    def __init__(self, secret: str, *, created: bool) -> None:
        self._secret = secret
        self.created = created

    def reveal(self) -> str:
        return self._secret


class FakeTokens:
    def __init__(self, error: Exception | None = None, *, adopt_created: bool = True) -> None:
        self._error = error
        self._adopt_created = adopt_created
        self.calls: list[str] = []
        self.adopt_calls: list[tuple[str, str]] = []

    def issue_token(self, user_id: str) -> Any:
        self.calls.append(user_id)
        if self._error is not None:
            raise self._error
        return FakeIssuedToken()

    def adopt_token(self, user_id: str, secret: str) -> Any:
        self.adopt_calls.append((user_id, secret))
        if self._error is not None:
            raise self._error
        return FakeAdoptedToken(secret, created=self._adopt_created)


class FakeStockTokens:
    """存量令牌只读源的假实现：注入一个固定结果，或注入一个异常代表源端查询失败。"""

    def __init__(self, result: StockTokenLookup | Exception | None = None) -> None:
        self._result = result if result is not None else StockTokenLookup(state=NO_ROW)
        self.calls: list[str] = []

    def lookup(self, email: str) -> StockTokenLookup:
        self.calls.append(email)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


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


class FakeLocalOverrides:
    """本地权限覆盖读取口的假实现（S-P-3，Issue #319）。同构于
    ``tests/test_permission_refresh_duty.py`` 的同名类（各自独立一份，理由见
    ``LocalOverrideSource`` 协议文档：``core/`` 不 import ``apps/``）。"""

    def __init__(
        self,
        entries: dict[str, tuple[LocalPermissionOverrideEntry, ...]] | None = None,
        *,
        fail_for: set[str] | None = None,
    ) -> None:
        self._entries = entries or {}
        self._fail_for = fail_for or set()
        self.calls: list[str] = []

    def effective_entries(self, *, user_id: str) -> tuple[LocalPermissionOverrideEntry, ...]:
        self.calls.append(user_id)
        if user_id in self._fail_for:
            raise RuntimeError("注入的本地覆盖读取失败")
        return self._entries.get(user_id, ())


class FakeLegacyTable:
    """存量权限只读源的假实现（S-P-2，Issue #319 / Trace #328）。同构于
    ``tests/test_permission_refresh_duty.py`` 的同名类——``LegacyPermissionTable``
    协议本身定义在 ``core/permission/legacy_source.py``，两个调用点的测试各自留一份
    假实现是测试夹具的既有惯例（同 ``FakeLocalOverrides``），不是层级约束要求的。"""

    def __init__(
        self,
        rows_by_email: dict[str, str] | None = None,
        *,
        find_error: Exception | None = None,
        duplicate_for: set[str] | None = None,
    ) -> None:
        self._rows = rows_by_email or {}
        self._find_error = find_error
        self._duplicate_for = duplicate_for or set()
        self.find_calls: list[tuple[str, str]] = []

    def find_rows(self, *, record_key: str, email: str) -> tuple[ExistingPermissionRow, ...]:
        self.find_calls.append((record_key, email))
        if self._find_error is not None:
            raise self._find_error
        if email not in self._rows:
            return ()
        row = ExistingPermissionRow(
            record_id=f"rec_{email}",
            fields={"record_key": email, "email": email, "permissions": self._rows[email]},
        )
        return (row, row) if email in self._duplicate_for else (row,)

    def read_row(self, record_id: str) -> dict[str, str]:
        email = record_id.removeprefix("rec_")
        return {"permissions": self._rows[email]}


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
        # 默认 None：哨兵——不注入存量令牌源时，行为必须与改动前逐字节一致
        # （本文件绝大多数用例走这条默认值，专门覆盖 adopt-or-issue 的用例见
        # StockTokenAdoptionTests）。
        "stock_tokens": None,
        "decisions": FakeDecisions(),
        "readiness": FakeReadiness(),
        "notifier": FakeNotifier(),
        "ledger": FakeLedger(),
        "audit": RecordingAudit(),
        "onboarding_failed": None,
        # 默认 None：哨兵——不注入本地权限覆盖 store 时，行为必须与接线之前逐字节
        # 一致（S-P-3，见 LocalOverrideMergeTests.test_store_absent_matches_todays_behavior）。
        "local_overrides": None,
        # 默认 None：哨兵——不注入存量权限只读源时，行为必须与接线之前逐字节一致
        # （S-P-2 #328，见 LegacySourceMergeTests.test_table_absent_matches_todays_behavior）。
        "legacy_source": None,
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
        stock_tokens=parts["stock_tokens"],
        decisions=parts["decisions"],
        readiness=parts["readiness"],
        notifier=parts["notifier"],
        ledger=parts["ledger"],
        audit=parts["audit"],
        role_function_map=overrides.get("role_function_map", ROLE_FUNCTION_MAP),
        # 默认放行：本文件绝大多数用例守的是名单闸**之后**的链路，不该被这道新增的
        # 前置闸挡住。内测名单闸自身的行为由 `InnerTestRosterGateTests` 专门覆盖。
        innertest_roster_gate=overrides.get("innertest_roster_gate", lambda open_id: True),
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
        onboarding_failed=parts["onboarding_failed"],
        local_overrides=parts["local_overrides"],
        legacy_source=parts["legacy_source"],
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


def _override_entry(
    *,
    user_id: str = USER_ID,
    direction: OverrideDirection = OverrideDirection.GRANT,
    company_id: str = "88",
    metric_name: str = "本地指标",
) -> LocalPermissionOverrideEntry:
    return LocalPermissionOverrideEntry(
        user_id=user_id,
        direction=direction,
        company_id=company_id,
        metric_name=metric_name,
        reason="U1 特批",
        initiated_by_open_id="ou_admin",
        pending_action_id="pac_fake",
        created_at=datetime(2026, 8, 18, tzinfo=UTC),
    )


class TwoCompanyGalaxySnapshot(FakeGalaxySnapshot):
    """两家公司的银河快照：供抑制用例证明"只有被抑制到空的公司键消失，另一家
    不受影响"（``merge_sources`` 模块文档「空结果」一节）。"""

    country_rows = (
        {"country_key": "c_1", "name": "Kenya", "name_cn": "肯尼亚", "boss_company_id": "88"},
        {"country_key": "c_2", "name": "Nigeria", "name_cn": "尼日利亚", "boss_company_id": "99"},
    )

    def datacountry_rows(self, galaxy_user_id: str) -> tuple[Mapping[str, Any], ...]:
        return (
            {"user_id": galaxy_user_id, "datacountry_id": "c_1"},
            {"user_id": galaxy_user_id, "datacountry_id": "c_2"},
        )


class LocalOverrideMergeTests(unittest.TestCase):
    """开通侧的四源合并接线（S-P-3，Issue #319）：与
    ``tests/test_permission_refresh_duty.py::LocalOverrideMergeTest`` 同一组断言，
    证明两个调用点消费的是同一个 ``merge_permission_sources``。开通侧的 ``galaxy``
    输入是**未翻译**的职能标签映射（onboarding 目前不调用翻译层，见
    ``merge_sources`` 模块文档「挂点」一节），本地覆盖的指标名字符串仍然精确并入/
    减去，不受周围标签影响。"""

    def test_store_absent_matches_todays_behavior(self) -> None:
        parts, result = run_once()

        self.assertIs(result.state, OnboardingState.STARTED)
        self.assertEqual(parts["decisions"].rows[0].permissions, '{"88":["销售分析"]}')

    def test_local_grant_is_unioned_into_the_published_metrics(self) -> None:
        """本地授权后聚合含并集（#319 验收断言）。"""

        overrides = FakeLocalOverrides({USER_ID: (_override_entry(),)})
        parts, _ = run_once(local_overrides=overrides)

        self.assertEqual(parts["decisions"].rows[0].permissions, '{"88":["本地指标","销售分析"]}')
        self.assertEqual(overrides.calls, [USER_ID])

    def test_local_suppression_removes_a_galaxy_granted_metric(self) -> None:
        """抑制后不含：本地抑制命中的是银河这一侧现算出的职能标签，不是本地授权
        自己加的那条；只有被抑制到空的公司键消失，另一家公司不受影响。"""

        overrides = FakeLocalOverrides(
            {USER_ID: (_override_entry(direction=OverrideDirection.SUPPRESS, metric_name="销售分析"),)}
        )
        parts, _ = run_once(galaxy=FakeGalaxy(TwoCompanyGalaxySnapshot()), local_overrides=overrides)

        self.assertEqual(parts["decisions"].rows[0].permissions, '{"99":["销售分析"]}')

    def test_read_failure_skips_local_source_and_audits(self) -> None:
        overrides = FakeLocalOverrides(fail_for={USER_ID})
        parts, result = run_once(local_overrides=overrides)

        self.assertIs(result.state, OnboardingState.STARTED)
        self.assertEqual(parts["decisions"].rows[0].permissions, '{"88":["销售分析"]}')
        self.assertIn("onboarding.local_override_skipped", parts["audit"].actions())
        facts = parts["audit"].facts("onboarding.local_override_skipped")
        self.assertEqual(facts["user"], USER_ID)
        self.assertEqual(facts["reason"], REASON_LOCAL_OVERRIDE_READ_FAILED)


class LegacySourceMergeTests(unittest.TestCase):
    """开通侧的存量权限沿用接线（S-P-2，Issue #319 / Trace #328）：与
    ``tests/test_permission_refresh_duty.py::LegacySourceMergeTest`` 同一组断言，
    证明两个调用点消费的是同一个 ``resolve_legacy_source``/``merge_permission_sources``。
    查找键取**规范化邮箱**（小写），与 ``ROSTER_ROWS[0]["email"]``（混合大小写）同源。"""

    NORMALIZED_EMAIL = "xiaoming@example.com"

    def test_table_absent_matches_todays_behavior(self) -> None:
        parts, result = run_once()

        self.assertIs(result.state, OnboardingState.STARTED)
        self.assertEqual(parts["decisions"].rows[0].permissions, '{"88":["销售分析"]}')

    def test_no_matching_legacy_row_is_also_identity(self) -> None:
        legacy = FakeLegacyTable({})
        parts, _ = run_once(legacy_source=legacy)

        self.assertEqual(parts["decisions"].rows[0].permissions, '{"88":["销售分析"]}')
        self.assertEqual(legacy.find_calls, [(self.NORMALIZED_EMAIL, self.NORMALIZED_EMAIL)])

    def test_legacy_permissions_are_unioned_into_the_published_metrics(self) -> None:
        """存量沿用后聚合含并集（本卡验收断言①）。"""

        legacy = FakeLegacyTable({self.NORMALIZED_EMAIL: '{"88":["存量指标"]}'})
        parts, _ = run_once(legacy_source=legacy)

        self.assertEqual(parts["decisions"].rows[0].permissions, '{"88":["存量指标","销售分析"]}')

    def test_read_failure_skips_legacy_source_and_audits(self) -> None:
        """本卡验收断言②：读取失败只跳过存量源，不整链失败。"""

        legacy = FakeLegacyTable(find_error=RuntimeError("注入的存量表读取失败"))
        parts, result = run_once(legacy_source=legacy)

        self.assertIs(result.state, OnboardingState.STARTED)
        self.assertEqual(parts["decisions"].rows[0].permissions, '{"88":["销售分析"]}')
        self.assertIn("onboarding.legacy_source_skipped", parts["audit"].actions())
        facts = parts["audit"].facts("onboarding.legacy_source_skipped")
        self.assertEqual(facts["user"], USER_ID)
        self.assertEqual(facts["reason"], REASON_LEGACY_READ_FAILED)
        self.assertEqual(facts["error"], "RuntimeError")

    def test_conflicting_legacy_rows_are_skipped_with_a_distinct_reason(self) -> None:
        """本卡验收断言⑤：命中多行失败关闭，原因码与读取失败可分辨。"""

        legacy = FakeLegacyTable(
            {self.NORMALIZED_EMAIL: '{"88":["存量指标"]}'}, duplicate_for={self.NORMALIZED_EMAIL}
        )
        parts, _ = run_once(legacy_source=legacy)

        self.assertEqual(parts["decisions"].rows[0].permissions, '{"88":["销售分析"]}')
        facts = parts["audit"].facts("onboarding.legacy_source_skipped")
        self.assertEqual(facts["reason"], "multiple_rows")
        self.assertNotIn("error", facts)


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
        # Issue #280 §7.1 渲染点 3：即使链从未真正开始跑，同步返回的终态也必须带
        # 上这一次的追溯号——否则"少发一条"的情形恰好是最需要它的情形。
        self.assertEqual(dict(result.messages[0].values), {"reference": "t1"})

    def test_stopping_declines_new_chains(self) -> None:
        runner, parts = build_runner(should_stop=lambda: True)
        result = runner.start(event_id="evt_1", open_id=OPEN_ID, trace_id="t1")
        self.assertIs(result.state, OnboardingState.INTERNAL_ERROR)
        self.assertIn("onboarding.start_declined_while_stopping", parts["audit"].actions())
        self.assertEqual(dict(result.messages[0].values), {"reference": "t1"})


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
        # Issue #280 §7.1：本侧故障终态必须带上本次事件的追溯号（`run_once` 固定用
        # `trace_id="trace_1"`），且不泄露内部原因码（只有 `reference` 一个变量）。
        self.assertEqual(parts["notifier"].terminal()[2], {"reference": "trace_1"})

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


class StockTokenAdoptionTests(unittest.TestCase):
    """`V-开通-24`：存量令牌 adopt-or-issue（Issue #281 改道，2026-08-25 裁定）。"""

    EMAIL = "Xiaoming@Example.com"  # 与 ROSTER_ROWS[0]["email"] 同源，见模块顶部 fixture。

    def test_sentinel_without_a_stock_token_source_matches_pre_change_behaviour(self) -> None:
        """哨兵：不注入存量令牌源（``build_runner`` 默认 ``stock_tokens=None``）时，
        行为必须与改动前逐字节一致——只签新，从不采纳。"""

        parts, _ = run_once()
        self.assertEqual(parts["tokens"].calls, [USER_ID])
        self.assertEqual(parts["tokens"].adopt_calls, [])

    def test_no_row_falls_back_to_issuing_a_new_token(self) -> None:
        stock = FakeStockTokens(StockTokenLookup(state=NO_ROW))
        parts, _ = run_once(stock_tokens=stock)
        self.assertEqual(stock.calls, [self.EMAIL])
        self.assertEqual(parts["tokens"].calls, [USER_ID])
        self.assertEqual(parts["tokens"].adopt_calls, [])
        self.assertEqual(parts["audit"].facts("onboarding.stock_token_absent")["state"], NO_ROW)

    def test_no_cipher_falls_back_to_issuing_a_new_token(self) -> None:
        stock = FakeStockTokens(StockTokenLookup(state=NO_CIPHER, status="pending"))
        parts, _ = run_once(stock_tokens=stock)
        self.assertEqual(parts["tokens"].calls, [USER_ID])
        self.assertEqual(parts["tokens"].adopt_calls, [])
        self.assertEqual(parts["audit"].facts("onboarding.stock_token_absent")["state"], NO_CIPHER)

    def test_adoptable_secret_is_adopted_instead_of_issuing_a_new_one(self) -> None:
        secret = "stock-plaintext-secret"
        stock = FakeStockTokens(StockTokenLookup(state=ADOPTABLE, secret=secret, status="approved"))
        parts, _ = run_once(stock_tokens=stock)
        self.assertEqual(parts["tokens"].calls, [], "有可用存量密文时不该再签新")
        self.assertEqual(parts["tokens"].adopt_calls, [(USER_ID, secret)])
        self.assertEqual(parts["environment"].tokens, [secret], "落进用户环境的必须是采纳的那份明文")
        self.assertEqual(parts["audit"].facts("onboarding.stock_token_adopted")["status_approved"], True)

    def test_adopting_an_existing_row_is_audited_distinctly_from_a_fresh_adoption(self) -> None:
        """幂等：库里已经有这个用户的令牌行时，审计动作名必须与"首次采纳"可分辨。"""

        secret = "stock-plaintext-secret"
        stock = FakeStockTokens(StockTokenLookup(state=ADOPTABLE, secret=secret))
        parts, _ = run_once(stock_tokens=stock, tokens=FakeTokens(adopt_created=False))
        self.assertEqual(parts["tokens"].adopt_calls, [(USER_ID, secret)])
        self.assertIn("onboarding.stock_token_existing_kept", parts["audit"].actions())
        self.assertNotIn("onboarding.stock_token_adopted", parts["audit"].actions())

    def test_decrypt_failure_is_a_loud_failure_never_a_fallback_to_issuing_new(self) -> None:
        """否定断言（#281 改道裁定）：解密失败必须响亮失败（`LX-ONBOARD-001`），
        **绝不**退回签新——签新会让用户环境令牌与正式表令牌错位，造成真实 MCP 认证
        静默失败。"""

        stock = FakeStockTokens(StockTokenLookup(state=DECRYPT_FAILED, status="approved"))
        parts, result = run_once(stock_tokens=stock)
        self.assertEqual(parts["audit"].facts("onboarding.result")["state"], "internal_error")
        self.assertEqual(
            parts["audit"].facts("onboarding.result")["failure_reason"],
            "stock_token_decrypt_failed",
        )
        self.assertEqual(parts["notifier"].terminal()[1], KEY_INTERNAL_ERROR)
        self.assertEqual(parts["tokens"].calls, [], "解密失败绝不能回退签新")
        self.assertEqual(parts["tokens"].adopt_calls, [])
        self.assertEqual(parts["environment"].calls, [], "没有可用令牌，环境不该被创建")
        self.assertEqual(parts["decisions"].rows, [], "没有可用令牌，权限不该被发布")
        self.assertIn("onboarding.stock_token_decrypt_failed", parts["audit"].actions())

    def test_non_approved_status_is_annotated_but_does_not_block_adoption(self) -> None:
        """权限面由银河同步权威决定，不由本步裁量——非 approved 只审计标注，仍然采纳。"""

        secret = "stock-plaintext-secret"
        stock = FakeStockTokens(StockTokenLookup(state=ADOPTABLE, secret=secret, status="pending"))
        parts, _ = run_once(stock_tokens=stock)
        self.assertEqual(parts["tokens"].adopt_calls, [(USER_ID, secret)], "非 approved 不阻止采纳")
        self.assertEqual(parts["audit"].facts("onboarding.stock_token_adopted")["status_approved"], False)

    def test_blank_status_counts_as_approved(self) -> None:
        secret = "stock-plaintext-secret"
        stock = FakeStockTokens(StockTokenLookup(state=ADOPTABLE, secret=secret, status=""))
        parts, _ = run_once(stock_tokens=stock)
        self.assertEqual(parts["audit"].facts("onboarding.stock_token_adopted")["status_approved"], True)

    def test_source_lookup_failure_is_an_internal_fault_not_a_business_rejection(self) -> None:
        stock = FakeStockTokens(RuntimeError("源端不可用"))
        parts, _ = run_once(stock_tokens=stock)
        self.assertEqual(parts["audit"].facts("onboarding.result")["state"], "internal_error")
        self.assertEqual(
            parts["audit"].facts("onboarding.result")["failure_reason"],
            "stock_token_lookup_failed_RuntimeError",
        )

    def test_the_secret_and_cipher_never_reach_the_audit_trail(self) -> None:
        secret = "stock-plaintext-secret-never-audited"
        stock = FakeStockTokens(StockTokenLookup(state=ADOPTABLE, secret=secret))
        parts, _ = run_once(stock_tokens=stock)
        rendered = repr(parts["audit"].records)
        self.assertNotIn(secret, rendered)
        self.assertNotIn(self.EMAIL, rendered, "邮箱同样不得进审计（只留 user_id/trace）")


class OnboardingFailedAlertCallbackTests(unittest.TestCase):
    """管理员送达（Issue #280 §7.3 步 1；`SYNC_TIMEOUT` 覆盖见独立审查 codex
    P1-3）：只在真正走到 `INTERNAL_ERROR` 或 `SYNC_TIMEOUT` 终态时调用一次注入的
    可选回调，签名 `(reason, trace_id)`，不含 open_id / 姓名 / 任何资料值——
    `SYNC_TIMEOUT` 的专用用例在 `SyncTimeoutTests` 里（离它自己的终态断言更近）。"""

    def test_an_internal_error_terminal_triggers_the_callback_exactly_once(self) -> None:
        calls: list[tuple[str, str]] = []
        parts, _ = run_once(
            directory=FakeDirectory(availability=DirectoryAvailability.STALE),
            onboarding_failed=lambda reason, trace_id: calls.append((reason, trace_id)),
        )
        self.assertEqual(calls, [("directory_unavailable", "trace_1")])

    def test_a_non_failure_terminal_never_triggers_the_callback(self) -> None:
        """否定断言：成功终态不得产生告警——回调签名里也没有"成功"这个概念，
        本测试证明它压根不会被调用。"""

        calls: list[tuple[str, str]] = []
        run_once(onboarding_failed=lambda reason, trace_id: calls.append((reason, trace_id)))
        self.assertEqual(calls, [], "开通完成不应该触发管理员告警")

    def test_a_deterministic_business_failure_never_triggers_the_callback(self) -> None:
        """否定断言（#251 开通告警：确定性业务失败不告警）：`NOT_AUTHORIZED`
        （无可用银河权限一类的确定性失败）不是内部故障，不得触发管理员送达
        回调——内测里这是预期结果，告警会变成噪音（Issue #251 正文原话）。

        与 `test_a_non_failure_terminal_never_triggers_the_callback`（成功终态）
        是两条独立的否定面：那一条挡的是"完成"，这一条挡的是"确定性地没有权限"，
        两者都不是"内部故障"，但走的是不同的终态分支，必须分别取证。

        触发方式与 `DeterministicRejectionTests.test_not_located` 同一个夹具
        （花名册/组织快照查无此人），只是这里额外注入回调并断言其从未被调用。
        """

        calls: list[tuple[str, str]] = []
        parts, _ = run_once(
            directory=FakeDirectory(members=()),
            onboarding_failed=lambda reason, trace_id: calls.append((reason, trace_id)),
        )
        self.assertEqual(
            parts["audit"].facts("onboarding.result")["state"], "not_authorized"
        )
        self.assertEqual(calls, [], "确定性业务失败（无可用权限）不应该触发管理员告警")

    def test_the_callback_never_receives_open_id_or_profile_values(self) -> None:
        """回调签名里根本没有传 open_id/姓名的位置——这里用真实签名反证：
        两个位置参数只可能是内部原因码与追溯号，两者都不是资料值。"""

        received: dict[str, Any] = {}

        def capture(reason: str, trace_id: str) -> None:
            received["reason"] = reason
            received["trace_id"] = trace_id

        run_once(
            directory=FakeDirectory(availability=DirectoryAvailability.STALE),
            onboarding_failed=capture,
        )
        self.assertNotIn(OPEN_ID, received.values())
        self.assertEqual(set(received), {"reason", "trace_id"})

    def test_a_raising_callback_does_not_break_notification_or_ledger(self) -> None:
        """否定断言：告警回调失败不得带走用户结论——**故意破坏**确认变红的对照组是
        「没有这条 try/except 时同一用例会抛穿 `_execute`」。"""

        def boom(reason: str, trace_id: str) -> None:
            raise RuntimeError("alert sink down")

        parts, _ = run_once(
            directory=FakeDirectory(availability=DirectoryAvailability.STALE),
            onboarding_failed=boom,
        )
        self.assertEqual(parts["notifier"].terminal()[1], KEY_INTERNAL_ERROR)
        self.assertEqual(parts["ledger"].marked, ["evt_1"])
        self.assertIn("onboarding.alert_callback_failed", parts["audit"].actions())

    def test_no_callback_injected_keeps_prior_behavior(self) -> None:
        """默认 `onboarding_failed=None`：不注入时用户仍然收到冻结文案，只是没有
        任何东西送到管理群（此前的行为，行为不变）。"""

        parts, _ = run_once(directory=FakeDirectory(availability=DirectoryAvailability.STALE))
        self.assertEqual(parts["notifier"].terminal()[1], KEY_INTERNAL_ERROR)

    def test_the_synchronous_stopping_terminal_also_triggers_the_callback(self) -> None:
        """独立审查（分支 fix/291-280-user-experience 收尾）：``start()`` 在
        ``should_stop()`` 为真时**同步**返回 ``INTERNAL_ERROR``，从不经过
        ``_execute``。此前只有 ``_execute`` 自己的 ``INTERNAL_ERROR`` 分支接了
        这个回调，这条同步分支被漏掉——用户看到「已转交管理员处理」，管理群
        实际上什么都没收到。"""

        calls: list[tuple[str, str]] = []
        runner, parts = build_runner(
            should_stop=lambda: True,
            onboarding_failed=lambda reason, trace_id: calls.append((reason, trace_id)),
        )
        result = runner.start(event_id="evt_1", open_id=OPEN_ID, trace_id="t1")

        self.assertIs(result.state, OnboardingState.INTERNAL_ERROR)
        self.assertEqual(calls, [("stopping", "t1")])

    def test_the_synchronous_executor_unavailable_terminal_also_triggers_the_callback(
        self,
    ) -> None:
        """同一条独立审查：提交执行器失败（队列满/执行器已停）同样是**同步**
        返回的 ``INTERNAL_ERROR``，同样此前从未触发管理员送达回调。"""

        calls: list[tuple[str, str]] = []
        runner, parts = build_runner(
            executor=InlineExecutor(accept=False),
            onboarding_failed=lambda reason, trace_id: calls.append((reason, trace_id)),
        )
        result = runner.start(event_id="evt_1", open_id=OPEN_ID, trace_id="t1")

        self.assertIs(result.state, OnboardingState.INTERNAL_ERROR)
        self.assertEqual(calls, [("executor_unavailable", "t1")])

    def test_a_raising_callback_on_the_synchronous_stopping_terminal_does_not_break_start(
        self,
    ) -> None:
        """否定断言，与 ``_execute`` 那条对照组同一形状：告警回调失败不得让
        ``start()`` 本身抛穿——用户仍然要拿到一个明确的终态返回值。"""

        def boom(reason: str, trace_id: str) -> None:
            raise RuntimeError("alert sink down")

        runner, parts = build_runner(should_stop=lambda: True, onboarding_failed=boom)
        result = runner.start(event_id="evt_1", open_id=OPEN_ID, trace_id="t1")

        self.assertIs(result.state, OnboardingState.INTERNAL_ERROR)
        self.assertIn("onboarding.alert_callback_failed", parts["audit"].actions())


class SyncTimeoutTests(unittest.TestCase):
    """`V-开通-13`：十五分钟同步超时是**专用**终态，不与内部故障码混淆。"""

    def test_timed_out_uses_the_dedicated_text_and_stays_in_mcp_syncing(self) -> None:
        parts, _ = run_once(readiness=FakeReadiness(ReadinessOutcome.TIMED_OUT))
        self.assertEqual(parts["audit"].facts("onboarding.result")["state"], "sync_timeout")
        self.assertEqual(parts["notifier"].terminal()[1], KEY_SYNC_TIMEOUT)
        # Issue #280 裁定 B2-4：`onboarding.sync_timeout` 也加追溯号。
        self.assertEqual(parts["notifier"].terminal()[2], {"reference": "trace_1"})
        self.assertEqual(parts["users"].advanced, [STATE_PROVISIONING, STATE_MCP_SYNCING])
        self.assertNotIn(STATE_ACTIVE, parts["users"].advanced)

    def test_a_sync_timeout_terminal_also_triggers_the_admin_callback(self) -> None:
        """独立审查 codex P1-3：产品合同对同步超时的措辞同样是"停止自动等待，
        转交管理员处理"（``docs/产品合同与外部边界.md``「权限同步期间」一节），
        与 ``INTERNAL_ERROR`` 分支承诺的"已转交管理员处理"是同一句产品承诺——
        此前只有后者真的送达管理群，``sync_timeout`` 这句承诺背后没有任何送达
        动作。``reason`` 用 ``mcp_sync_timeout``，与内部故障的原因码（如
        ``directory_unavailable``）可区分，管理员据此分得清"这是同步超时在等"
        还是"这是本侧真的坏了"。这条断言只覆盖告警侧，**不改变**
        ``late_readiness_recovery`` 的自动恢复语义：``provisioning_state`` 仍然
        停在 ``mcp_syncing``，不会因为这条告警被推进。"""

        calls: list[tuple[str, str]] = []
        parts, _ = run_once(
            readiness=FakeReadiness(ReadinessOutcome.TIMED_OUT),
            onboarding_failed=lambda reason, trace_id: calls.append((reason, trace_id)),
        )
        self.assertEqual(calls, [("mcp_sync_timeout", "trace_1")])
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

    def test_a_single_failed_notification_never_collapses_the_user(self) -> None:
        """外部独立审查 P2-1：通知没送到（即使已经放回过认领），也绝不能提前把
        `provisioning_state` 收口成 `aborted`——`aborted` 不在
        `StalledProvisioningDuty` 的候选判据（`provisioning`/`mcp_syncing`）里，
        提前收口等于把这个人从 45 分钟兜底的唯一通道里踢出去。"""

        users = FakeUsers()
        parts, _ = run_once(
            users=users,
            decisions=FakeDecisions(statuses=("failed",)),
            notifier=FakeNotifier(error=RuntimeError()),
        )

        self.assertEqual(users.aborted, [], "通知没送到，绝不能尝试收口")
        self.assertEqual(
            users.current_state,
            STATE_PROVISIONING,
            "状态必须原样留在中途格，等 StalledProvisioningDuty 45 分钟后重新尝试通知",
        )
        self.assertEqual(parts["ledger"].released, [("evt_1", CLAIM_TOKEN)])
        self.assertEqual(parts["ledger"].marked, [])

    def test_two_failed_notifications_never_abandon_the_user_to_a_dead_end(self) -> None:
        """同一条事件被连续执行两次、两次通知都失败：第二次仍然不得收口。

        真库半边（"停在 provisioning/mcp_syncing 且认领已超租约必然被
        `StalledProvisioningDuty` 的候选查询捞到"）由
        `tests/test_postgres_stalled_provisioning.py::CandidateQueryTest` 证明；
        本用例证明的是另一半——本模块自己绝不会抢先把这个人从候选判据里踢出去。
        两条证据合起来才是完整的"不会被永久遗弃"。
        """

        users = FakeUsers()
        runner, parts = build_runner(
            users=users,
            decisions=FakeDecisions(statuses=("failed",)),
            notifier=FakeNotifier(error=RuntimeError()),
        )
        runner.start(event_id="evt_1", open_id=OPEN_ID, trace_id="t1", claim_token=CLAIM_TOKEN)
        runner.start(event_id="evt_1", open_id=OPEN_ID, trace_id="t1", claim_token=CLAIM_TOKEN)

        self.assertEqual(users.aborted, [], "两轮通知全部失败，仍然绝不能收口")
        self.assertEqual(users.current_state, STATE_PROVISIONING)
        self.assertEqual(parts["ledger"].released, [("evt_1", CLAIM_TOKEN)])
        self.assertEqual(parts["ledger"].marked, ["evt_1"], "账仍然要记上收口，但不改状态")

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


class InnerTestRosterGateTests(unittest.TestCase):
    """内测名单闸（Issue #302 S-N-01）：挡在整条链最前面。

    认领 `V-开通-21`（名单外→内测未开放+零建档+脱敏审计）、`V-开通-22`（默认拒绝）、
    `V-开通-23`（名单内正常通过）。
    """

    def test_rejected_open_id_gets_the_frozen_innertest_not_open_terminal(self) -> None:
        parts, result = run_once(innertest_roster_gate=lambda open_id: False)

        self.assertEqual(result.state, OnboardingState.STARTED)
        open_id, key, _, _ = parts["notifier"].terminal()
        self.assertEqual(key, KEY_INNERTEST_NOT_OPEN)
        self.assertEqual(open_id, OPEN_ID)

    def test_rejected_open_id_leaves_zero_business_footprint(self) -> None:
        """否定断言：不建档、不发权限、不建环境、不推进状态——零业务状态残留。

        闸必须挡在身份定位**之前**：`directory.calls`/`employment.calls` 全程为空，
        证明组织快照读取与在职实时回读都没有发生，不只是"最终没建档"这一个结果。
        """

        parts, _ = run_once(innertest_roster_gate=lambda open_id: False)

        self.assertEqual(parts["directory"].calls, [])
        self.assertEqual(parts["employment"].calls, [])
        self.assertEqual(parts["provisioning"].requests, [])
        self.assertEqual(parts["environment"].calls, [])
        self.assertEqual(parts["tokens"].calls, [])
        self.assertEqual(parts["decisions"].rows, [])
        self.assertEqual(parts["users"].advanced, [])

    def test_rejection_audit_carries_no_identity_and_no_message_body(self) -> None:
        """审计只带 `event_id`/`trace_id`，同本文件其余每一条 `_audit.record` 一致。

        **不直接放 open_id（含 `redact_identifier()` 脱敏形式）**：脱敏值按其自身
        文档字符串只能进日志、不可反查也不可比较，放进结构化审计字段会被误当成
        可关联的身份键（`V-花名册-34`）。需要还原是谁时凭 `event_id` 回读
        `inbound_event.user_open_id` 即可，不在这里重复一份。
        """

        parts, _ = run_once(innertest_roster_gate=lambda open_id: False)

        audit = parts["audit"]
        self.assertIn("onboarding.innertest_roster_rejected", audit.actions())
        facts = audit.facts("onboarding.innertest_roster_rejected")
        self.assertEqual(facts["event_id"], "evt_1")
        self.assertEqual(facts["trace_id"], "trace_1")
        # 不记身份、不记消息正文：字段集合恰好是事件与追溯号，没有第三个键。
        self.assertEqual(set(facts), {"event_id", "trace_id"})
        self.assertNotIn(OPEN_ID, str(facts))

    def test_default_deny_rejects_an_open_id_that_is_on_no_list_at_all(self) -> None:
        """`V-开通-22` 默认拒绝：闸绑定一个真正的空集合（不是硬编码 `lambda: False`），

        用一个从未出现在任何测试夹具名单中的未知 open_id 证明——不是只证明某个
        已知危险对象被禁（验证与门禁 §八第 4 条）。
        """

        gate = AutoOnboardingRunner.build_innertest_roster_gate(frozenset())
        parts, _ = run_once(innertest_roster_gate=gate)

        _, key, _, _ = parts["notifier"].terminal()
        self.assertEqual(key, KEY_INNERTEST_NOT_OPEN)
        self.assertEqual(parts["provisioning"].requests, [], "空名单必须零建档")

    def test_open_id_on_the_roster_proceeds_through_the_normal_chain(self) -> None:
        """`V-开通-23`：名单内 open_id 不受闸影响，正常推进到既有成功终态。"""

        gate = AutoOnboardingRunner.build_innertest_roster_gate(frozenset({OPEN_ID}))
        parts, result = run_once(innertest_roster_gate=gate)

        self.assertEqual(result.state, OnboardingState.STARTED)
        _, key, values, _ = parts["notifier"].terminal()
        self.assertEqual(key, KEY_COMPLETED)
        self.assertEqual(len(parts["provisioning"].requests), 1, "名单内用户应正常建档")

    def test_build_innertest_roster_gate_wraps_the_pure_membership_function(self) -> None:
        """静态方法只是把纯判定函数绑上一份具体集合，不改变其语义。"""

        roster = frozenset({OPEN_ID})
        gate = AutoOnboardingRunner.build_innertest_roster_gate(roster)

        self.assertEqual(gate(OPEN_ID), is_open_id_innertest_allowed(OPEN_ID, roster))
        self.assertTrue(gate(OPEN_ID))
        self.assertFalse(gate("ou_never_listed_anywhere"))


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
            innertest_roster_gate=lambda open_id: True,
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

    def test_a_non_callable_onboarding_failed_is_refused_at_construction(self) -> None:
        with self.assertRaises(TypeError):
            AutoOnboardingRunner(
                submit=lambda task: True,
                sleep=lambda seconds: None,
                onboarding_failed="not-callable",  # type: ignore[arg-type]
                **self._parts(),
            )

    def test_a_missing_innertest_roster_gate_is_refused_at_construction(self) -> None:
        """没有默认放行（Issue #302 S-N-01）：缺省会让内测名单闸形同虚设。"""

        parts = self._parts()
        parts["innertest_roster_gate"] = None
        with self.assertRaises(TypeError):
            AutoOnboardingRunner(
                submit=lambda task: True, sleep=lambda seconds: None, **parts  # type: ignore[arg-type]
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
