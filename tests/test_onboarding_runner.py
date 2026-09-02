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
from lingxi.core.permission.local_override import LocalPermissionOverrideEntry, OverrideDirection
from lingxi.core.permission.mcp_readiness import ReadinessOutcome
from lingxi.core.permission.merge_sources import (
    REASON_GRANT_REDUNDANT_WILDCARD,
    REASON_LOCAL_OVERRIDE_READ_FAILED,
    REASON_SUPPRESS_INAPPLICABLE_WILDCARD,
)
from lingxi.core.permission.metric_translation import translate_company_functions
from lingxi.core.permission.publish import PermissionGrantBlockedByAccountState
from lingxi.core.permission.publish_row import ADMIN_FULL_ACCESS_FUNCTION

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
#: 「公司+职能→指标名」翻译映射（Issue #227 / #346）默认夹具：**恒等**翻译（职能
#: 标签"销售分析"逐字符翻译成同名指标名），让本文件绝大多数既有断言（此前编码的是
#: 修复前"未翻译"行为，值列表恰好都是"销售分析"）在 `_publish` 真正接入
#: `translate_company_functions` 之后原样成立，同时仍然真实走一遍翻译层——正向/
#: 否定/变异专用断言见 ``PublishTranslationTests``，它用一个翻译结果与职能标签
#: **不同**的映射，证明翻译真的发生了（恒等映射本身无法区分"翻译过"与"直接透传"）。
METRIC_TRANSLATION_MAP: Mapping[str, Mapping[str, Sequence[str]]] = {
    "88": {"销售分析": ("销售分析",)},
    "99": {"销售分析": ("销售分析",)},
}


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


class FakeLegacyImportReport:
    def __init__(self, imported: int, already_present: int = 0, group_id: str | None = None, group_created: bool = False) -> None:
        self.imported = imported
        self.already_present = already_present
        self.group_id = group_id
        self.group_created = group_created


class FakeLegacyImporter:
    """存量差集导入口的假实现（rc25 S-1，Issue #540）：记录每次收到的计划；可注入异常
    代表落库失败。``imported`` 默认等于计划行数（全部新写入）。"""

    def __init__(self, error: Exception | None = None, *, already_present: int = 0) -> None:
        self._error = error
        self._already_present = already_present
        self.calls: list[dict[str, Any]] = []

    def import_plan(self, *, user_id: str, target_open_id: str, plan: Any, now: Any) -> Any:
        self.calls.append({"user_id": user_id, "target_open_id": target_open_id, "plan": plan, "now": now})
        if self._error is not None:
            raise self._error
        total = len(plan.pairs) + len(plan.all_scope_metrics)
        imported = max(total - self._already_present, 0)
        group_id = "lpg_fake" if plan.all_scope_metrics else None
        return FakeLegacyImportReport(
            imported, self._already_present, group_id, group_created=bool(plan.all_scope_metrics) and imported > 0
        )


class FakeDecision:
    def __init__(self, *, enqueued: bool, permission_version: int, outbox_id: str) -> None:
        self.enqueued = enqueued
        self.permission_version = permission_version
        self.outbox_id = outbox_id


class FakeIntent:
    def __init__(self, status: str) -> None:
        self.status = status


class FakeDecisions:
    def __init__(
        self,
        *,
        enqueued: bool = True,
        statuses: Sequence[str] = ("published",),
        blocked_account_state: str | None = None,
    ) -> None:
        self._enqueued = enqueued
        self._statuses = list(statuses)
        self.rows: list[Any] = []
        self.reasons: list[str] = []
        self.require_enabled_account: list[bool] = []
        self.loads = 0
        # Issue #483：非空时照真实实现抛 ``PermissionGrantBlockedByAccountState``，
        # 用来钉死"开通链被账号状态挡住时收敛到既有停用终态"这条收口。
        self._blocked_account_state = blocked_account_state

    def record_decision(
        self,
        *,
        user_id: str,
        row: Any,
        reason: str,
        require_enabled_account: bool,
        decided_at: datetime,
    ) -> Any:
        # 开通链落的恒是需要账号有效的授权（Issue #483）：这条断言让调用点一旦
        # 传成 False（或忘了传）就当场红，而不是在真库用例里才暴露。
        assert require_enabled_account is True, "首次开通必须声明需要账号有效"
        self.require_enabled_account.append(require_enabled_account)
        if self._blocked_account_state is not None:
            raise PermissionGrantBlockedByAccountState(self._blocked_account_state)
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


class RecordingFailureReasons:
    """``FailureReasonRecorder`` 的内存假实现（Issue #337）：记录每一次调用的
    完整关键字参数，供断言核对。"""

    def __init__(self, *, raise_error: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self.raise_error = raise_error

    def record_failure(self, *, trace_id: str, failure_reason: str, event_type: str) -> None:
        if self.raise_error:
            raise RuntimeError("模拟失败原因落库故障")
        self.calls.append(
            {"trace_id": trace_id, "failure_reason": failure_reason, "event_type": event_type}
        )


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
        # 默认 None；注入了存量令牌源而没显式给导入口时，自动配一个假导入口（构造期
        # 不变式：源装了导入口必装，见 ConstructionTests）。
        "legacy_importer": None,
        "decisions": FakeDecisions(),
        "readiness": FakeReadiness(),
        "notifier": FakeNotifier(),
        "ledger": FakeLedger(),
        "audit": RecordingAudit(),
        "onboarding_failed": None,
        # 默认 None：哨兵——不注入失败原因落库口时，行为必须与接线之前逐字节
        # 一致（Issue #337，见 FailureReasonRecordingTests.test_no_recorder_
        # injected_keeps_prior_behavior）。
        "failure_reasons": None,
        # 默认 None：哨兵——不注入本地权限覆盖 store 时，行为必须与接线之前逐字节
        # 一致（S-P-3，见 LocalOverrideMergeTests.test_store_absent_matches_todays_behavior）。
        "local_overrides": None,
    }
    parts.update({key: value for key, value in overrides.items() if key in parts})
    if parts["stock_tokens"] is not None and parts["legacy_importer"] is None and "legacy_importer" not in overrides:
        parts["legacy_importer"] = FakeLegacyImporter()
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
        metric_translation_map=overrides.get("metric_translation_map", METRIC_TRANSLATION_MAP),
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
        failure_reasons=parts["failure_reasons"],
        local_overrides=parts["local_overrides"],
        legacy_importer=parts["legacy_importer"],
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


class AllCountriesGalaxySnapshot(FakeGalaxySnapshot):
    """银河「全非」通配授权（``scope.all_countries=True``，`galaxy_scope.py` 哨兵
    展开）——与 ``ADMIN_FULL_ACCESS_FUNCTION`` 职能是两个互相独立的
    ``all_companies=True`` 成因（`Issue #440` 通配角 v2）。快照仍需要至少一个非哨兵
    国家行，否则 ``aggregate_permission`` 会因 ``company_ids`` 为空提前拒绝
    （该判据不看 ``all_countries``）。"""

    country_rows = (
        {"country_key": "0", "name": "ALL", "name_cn": "全非", "boss_company_id": "0"},
        {"country_key": "c_1", "name": "Kenya", "name_cn": "肯尼亚", "boss_company_id": "88"},
    )

    def datacountry_rows(self, galaxy_user_id: str) -> tuple[Mapping[str, Any], ...]:
        return ({"user_id": galaxy_user_id, "datacountry_id": "0"},)


class AdminRoleGalaxySnapshot(FakeGalaxySnapshot):
    """持有 :data:`ADMIN_FULL_ACCESS_FUNCTION` 职能——`aggregate_permission` 的
    「角色即全公司」特例，强制 ``all_companies=True``，与上面的「全非」通配是两个
    独立成因中的另一个（真全指标通配，`merge_sources` 模块文档「通配角 v2」）。"""

    def role_rows(self, galaxy_user_id: str) -> tuple[Mapping[str, Any], ...]:
        return ({"user_id": galaxy_user_id, "role_id": "r_admin", "role_name": ADMIN_FULL_ACCESS_FUNCTION},)


class LocalOverrideMergeTests(unittest.TestCase):
    """开通侧的四源合并接线（S-P-3，Issue #319）：与
    ``tests/test_permission_refresh_duty.py::LocalOverrideMergeTest`` 同一组断言，
    证明两个调用点消费的是同一个 ``merge_permission_sources``。开通侧的 ``galaxy``
    输入自 #346 修复起是 ``translate_company_functions`` **翻译后**的指标名映射
    （与每日重算同一条路径，见 ``merge_sources`` 模块文档「挂点」一节
    「2026-08-28 更正」；本文件默认夹具 ``METRIC_TRANSLATION_MAP`` 对"销售分析"是
    恒等翻译，因此本类既有断言的值列表原样成立），本地覆盖的指标名字符串仍然精确
    并入/减去，不受翻译结果影响。"""

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

    def test_a_fully_suppressed_grant_is_not_authorized_not_a_crash(self) -> None:
        """红线-2（Trace #328 opus 审查）「onboarding 侧同分支核对」：银河这一侧
        原本是有效授权（默认夹具只有一个公司/一个职能），但本地抑制把它压光到
        空字典。``_publish`` 走的是**新建**行路径，一个内容为空的新建行对问数
        MCP 毫无意义（既没有指标可读，也不该为一个净权限为零的人耗费一份令牌），
        因此归类为确定性业务失败（``not_authorized``），不落到
        ``build_translated_publish_row`` 对空输入的 ``ValueError`` 冒泡成一条
        不可分辨的 ``INTERNAL_ERROR``。"""

        overrides = FakeLocalOverrides(
            {USER_ID: (_override_entry(direction=OverrideDirection.SUPPRESS, metric_name="销售分析"),)}
        )
        parts, result = run_once(local_overrides=overrides)

        self.assertIs(result.state, OnboardingState.STARTED)
        self.assertEqual(parts["audit"].facts("onboarding.result")["state"], "not_authorized")
        self.assertEqual(
            parts["audit"].facts("onboarding.result")["failure_reason"],
            "fully_suppressed_by_local_override",
        )
        self.assertEqual(parts["decisions"].rows, [], "全抑制时不得排出任何发布意图")

    def test_a_partially_suppressed_grant_still_publishes_normally(self) -> None:
        """否定断言：只抑掉**一部分**指标（不是全部）时走的仍是正常发布，不是
        ``not_authorized``——证明判据是"合并结果整体为空"，不是"发生过任何抑制"。"""

        overrides = FakeLocalOverrides(
            {
                USER_ID: (
                    _override_entry(direction=OverrideDirection.GRANT, metric_name="额外授权"),
                    _override_entry(direction=OverrideDirection.SUPPRESS, metric_name="销售分析"),
                )
            }
        )
        parts, result = run_once(local_overrides=overrides)

        self.assertIs(result.state, OnboardingState.STARTED)
        self.assertEqual(parts["audit"].facts("onboarding.result")["state"], "completed")
        self.assertEqual(parts["decisions"].rows[0].permissions, '{"88":["额外授权"]}')

    def test_limited_metric_wildcard_grant_is_unioned_into_the_published_metrics(self) -> None:
        """通配角 v2（`Issue #440`）：``all_companies=True`` 但成因是
        ``scope.all_countries``（银河「全非」通配）、职能不含
        :data:`ADMIN_FULL_ACCESS_FUNCTION`——「有限指标 ``*``」形态。判据
        ``ADMIN_FULL_ACCESS_FUNCTION in aggregate.functions`` 为假，接线传
        ``full_access_wildcard=False``：本地授权改为在 ``"*"`` 清单上参与并集，
        不再被误判成冗余而整体跳过（onboarding 侧与每日重算同一接线，见
        ``tests/test_permission_refresh_duty.py`` 同名用例）。"""

        overrides = FakeLocalOverrides(
            {USER_ID: (_override_entry(direction=OverrideDirection.GRANT, metric_name="额外授权"),)}
        )
        parts, result = run_once(
            galaxy=FakeGalaxy(AllCountriesGalaxySnapshot()),
            metric_translation_map={"*": {"销售分析": ("销售分析",)}},
            local_overrides=overrides,
        )

        self.assertIs(result.state, OnboardingState.STARTED)
        self.assertEqual(
            parts["decisions"].rows[0].permissions,
            '{"*":["销售分析","额外授权"]}',
            "有限指标通配下本地授权应参与并集，不整体跳过",
        )
        self.assertNotIn(
            "onboarding.local_override_skipped",
            parts["audit"].actions(),
            "有限指标通配这一支恒不登记跳过原因（模块文档「通配角 v2」）",
        )

    def test_full_access_wildcard_admin_still_skips_both_grant_and_suppress(self) -> None:
        """回归防护：真全指标通配（``ADMIN_FULL_ACCESS_FUNCTION`` 职能）下判据仍为
        真，接线保持 ``full_access_wildcard=True``——通配角 v1 的既有行为（本地授权
        / 抑制整体不参与合并）逐字节不变，不因 v2 判据接线而被误伤（onboarding 侧，
        与每日重算 ``LocalOverrideMergeTest.
        test_wildcard_admin_skips_both_grant_and_suppress_with_audit`` 同一断言）。"""

        overrides = FakeLocalOverrides(
            {
                USER_ID: (
                    _override_entry(direction=OverrideDirection.GRANT, metric_name="额外授权"),
                    _override_entry(direction=OverrideDirection.SUPPRESS, metric_name="销售分析"),
                )
            }
        )
        parts, result = run_once(
            galaxy=FakeGalaxy(AdminRoleGalaxySnapshot()),
            role_function_map={ADMIN_FULL_ACCESS_FUNCTION: ADMIN_FULL_ACCESS_FUNCTION},
            metric_translation_map={"*": {ADMIN_FULL_ACCESS_FUNCTION: ("销售分析",)}},
            local_overrides=overrides,
        )

        self.assertIs(result.state, OnboardingState.STARTED)
        self.assertEqual(
            parts["decisions"].rows[0].permissions,
            '{"*":["销售分析"]}',
            "真全指标通配下本地源整体不生效",
        )
        skipped = [
            fields for name, fields in parts["audit"].records if name == "onboarding.local_override_skipped"
        ]
        reasons = {fields["reason"] for fields in skipped}
        self.assertEqual(
            reasons, {REASON_GRANT_REDUNDANT_WILDCARD, REASON_SUPPRESS_INAPPLICABLE_WILDCARD}
        )


class ZeroGalaxyLocalGrantTests(unittest.TestCase):
    """`V-权限-15` 已消除的已知限制在开通侧的对应断言（PM 2026-08-29 裁定，
    Issue #419）：``aggregate.granted`` 为假不再让 `_match` 直接拒绝，先看
    **本地授权**能否兜底出可发布内容——与
    ``tests/test_permission_refresh_duty.py::ZeroGalaxyLocalGrantTest``
    同一组断言，证明两个调用点消费的是同一条产品语义。"""

    def test_zero_galaxy_user_with_a_local_grant_completes_with_exactly_the_local_set(
        self,
    ) -> None:
        """正向：零银河权限 + 本地授权（未被同键抑制）→ 正常完成开通，发布内容
        精确等于本地授权集合——不出现任何翻译产物（"销售分析"）。"""

        overrides = FakeLocalOverrides({USER_ID: (_override_entry(),)})
        parts, result = run_once(role_function_map={}, local_overrides=overrides)

        self.assertIs(result.state, OnboardingState.STARTED)
        self.assertEqual(parts["audit"].facts("onboarding.result")["state"], "completed")
        self.assertEqual(parts["decisions"].rows[0].permissions, '{"88":["本地指标"]}')
        # 走的是发布分支，因此令牌、环境、状态推进都正常发生——与"确定性业务失败
        # 不建环境、不发布"的既有边界（`DeterministicRejectionTests`）互不矛盾：
        # 那条边界只适用于最终真的没有任何可发布内容的人。
        self.assertEqual(parts["environment"].calls, [USER_ID])
        self.assertIn(STATE_ACTIVE, parts["users"].advanced)

    def test_a_same_key_suppressed_grant_still_stays_unauthorized_with_zero_footprint(
        self,
    ) -> None:
        """否定：本地授权被**同一个键**（同公司同指标）的本地抑制清空后，合并
        结果仍是空字典——维持"无可用银河权限"终态不变，且**不为这个人签发令牌、
        创建用户环境、推进开通状态**（`_reject_zero_galaxy_without_local_grant`
        排在 `_issue_token`/`_create_environment` 之前，不是像红线-2 那样先建
        环境再在 `_publish` 里拒绝）。"""

        overrides = FakeLocalOverrides(
            {
                USER_ID: (
                    _override_entry(direction=OverrideDirection.GRANT, metric_name="本地指标"),
                    _override_entry(direction=OverrideDirection.SUPPRESS, metric_name="本地指标"),
                )
            }
        )
        parts, result = run_once(role_function_map={}, local_overrides=overrides)

        self.assertIs(result.state, OnboardingState.STARTED)
        facts = parts["audit"].facts("onboarding.result")
        self.assertEqual(facts["state"], "not_authorized")
        self.assertEqual(
            facts["failure_reason"],
            "no_supported_function",
            "原因是银河本来就没给（aggregate.reason），不是红线-2 的本地行政性收回",
        )
        self.assertEqual(parts["notifier"].terminal()[1], KEY_NOT_AUTHORIZED)
        self.assertEqual(parts["environment"].calls, [], "无权限终态不得创建用户环境")
        self.assertEqual(parts["decisions"].rows, [], "无权限终态不得排发布意图")
        self.assertEqual(parts["users"].advanced, [], "无权限终态不得推进开通状态")

    def test_a_never_granted_user_with_no_local_override_stays_unauthorized(self) -> None:
        """否定：既无银河也无本地授权（默认 ``local_overrides=None``，装配层未
        接线）→ 与改动前逐字节一致——`DeterministicRejectionTests.
        test_no_supported_function_is_an_unauthorized_terminal` 的同一断言，
        这里额外证明它在新分支下依然成立。"""

        parts, _ = run_once(role_function_map={})

        self.assertEqual(parts["audit"].facts("onboarding.result")["failure_reason"], "no_supported_function")
        self.assertEqual(parts["environment"].calls, [])
        self.assertEqual(parts["decisions"].rows, [])
        self.assertEqual(parts["users"].advanced, [])

    def test_galaxy_recovering_restores_the_union_for_the_same_local_grant(self) -> None:
        """正向：同一份本地授权配置，银河一侧从零恢复为有效授权后，发布内容从
        「精确本地集合」变回「银河翻译结果 ∪ 本地授权」——两条分支共用同一个
        ``merge_permission_sources``，不是两套互相独立的合并逻辑。"""

        overrides = FakeLocalOverrides({USER_ID: (_override_entry(),)})

        zero_parts, zero_result = run_once(role_function_map={}, local_overrides=overrides)
        self.assertIs(zero_result.state, OnboardingState.STARTED)
        self.assertEqual(zero_parts["decisions"].rows[0].permissions, '{"88":["本地指标"]}')

        recovered_parts, recovered_result = run_once(local_overrides=overrides)
        self.assertIs(recovered_result.state, OnboardingState.STARTED)
        self.assertEqual(
            recovered_parts["decisions"].rows[0].permissions,
            '{"88":["本地指标","销售分析"]}',
            "银河恢复后并集恢复",
        )

    def test_the_translation_gate_does_not_block_a_local_only_publish(self) -> None:
        """`publish_allowed` 闸门的适用范围没有变（Issue #419「既有出口闸门全部
        保持」）：它只保护"银河内容需要翻译才能安全发布"这件事。零银河用户没有
        银河内容，与改动前"零银河用户结构上从不到达这道检查"逐字节一致——闸门
        关闭时，一个零银河 + 本地授权的用户仍然能正常完成开通。"""

        overrides = FakeLocalOverrides({USER_ID: (_override_entry(),)})
        parts, result = run_once(
            role_function_map={}, local_overrides=overrides, publish_allowed=lambda: False
        )

        self.assertIs(result.state, OnboardingState.STARTED)
        self.assertEqual(parts["audit"].facts("onboarding.result")["state"], "completed")
        self.assertEqual(parts["decisions"].rows[0].permissions, '{"88":["本地指标"]}')
        self.assertNotIn("onboarding.publish_gate_closed", parts["audit"].actions())


class PublishTranslationTests(unittest.TestCase):
    """`_publish` 接入 `translate_company_functions`（Issue #346，Trace #373
    S-H1-5）：开通链产出的发布行值列表必须是**翻译后的指标名**，与每日重算
    （`permission_refresh.py::_refresh_user`）走同一个函数、同一条 fail-closed
    语义——不是 `#346` 修复前那样直接把未翻译的职能标签写进正式权限表。

    默认夹具 `METRIC_TRANSLATION_MAP` 是恒等翻译（"销售分析" → "销售分析"），
    本类用一个翻译结果**明确不同于**职能标签的映射，才能证明翻译真的发生了
    （恒等映射下"翻译过"与"直接透传未翻译标签"产出逐字节相同，无法区分）。
    """

    #: 翻译结果故意与职能标签"销售分析"不同，证明发布行里的值列表来自翻译，
    #: 不是原样透传的职能标签。
    DISTINCT_TRANSLATION_MAP: Mapping[str, Mapping[str, Sequence[str]]] = {
        "88": {"销售分析": ("日活万人",)},
    }

    def test_the_published_row_carries_translated_metric_names_not_function_labels(
        self,
    ) -> None:
        """正向：产出值列表＝翻译后指标名，与 `translate_company_functions` 本身
        （每日重算共用的同一个函数）的产出逐字节一致——「同一函数、同一姿势」。"""

        parts, result = run_once(metric_translation_map=self.DISTINCT_TRANSLATION_MAP)

        self.assertIs(result.state, OnboardingState.STARTED)
        self.assertEqual(parts["audit"].facts("onboarding.result")["state"], "completed")
        expected = translate_company_functions(
            companies=("88",),
            functions=("销售分析",),
            all_companies=False,
            mapping=self.DISTINCT_TRANSLATION_MAP,
        )
        self.assertEqual(expected, {"88": ("日活万人",)})
        self.assertEqual(parts["decisions"].rows[0].permissions, '{"88":["日活万人"]}')
        self.assertNotIn("销售分析", parts["decisions"].rows[0].permissions, "不得残留未翻译的职能标签")

    def test_an_uncovered_combination_fails_closed_with_zero_external_writes(self) -> None:
        """否定用例（本卡核心）：存在未翻译（未覆盖）的「公司 + 职能」组合时，
        这条开通链**整条**拒绝发布——不产出部分结果，`record_decision`（唯一能把
        内容写进 `publish_outbox` 的入口）一次都不被调用，外部表零写入。"""

        empty_for_this_company = {"77": {"销售分析": ("日活万人",)}}  # 覆盖了别的公司，唯独没有"88"
        parts, result = run_once(metric_translation_map=empty_for_this_company)

        self.assertIs(result.state, OnboardingState.STARTED)
        # 拒绝如何向上表达：INTERNAL_ERROR 终态（本侧数据缺口，不是"没有银河权限"），
        # 用户看到冻结的 LX-ONBOARD-001，不是静默吞掉。
        facts = parts["audit"].facts("onboarding.result")
        self.assertEqual(facts["state"], "internal_error")
        self.assertEqual(facts["failure_reason"], "permission_translation_uncovered")
        self.assertEqual(parts["notifier"].terminal()[1], KEY_INTERNAL_ERROR)
        # 可观察性：专门的翻译门审计事件，原因码可分辨（与整轮判据的
        # `permission_translation_unavailable` 不是同一个值）。
        self.assertIn("onboarding.publish_gate_closed", parts["audit"].actions())
        gate_facts = parts["audit"].facts("onboarding.publish_gate_closed")
        self.assertEqual(gate_facts["reason"], "permission_translation_uncovered")
        # 核心否定断言：外部写入口一次都没被调用——零写入，不是"写了又回滚"。
        self.assertEqual(parts["decisions"].rows, [], "翻译未覆盖时一条发布意图都不得排")
        self.assertEqual(parts["decisions"].reasons, [])
        # 环境与令牌在翻译判据之前已经建好（次序如此，`_publish` 是链的第 7 步），
        # 但 MCP 就绪确认与 active 推进绝不能发生——半开的人不能被宣告成功。
        self.assertNotIn(STATE_ACTIVE, parts["users"].advanced)
        self.assertEqual(parts["readiness"].bindings, [], "翻译失败时不进入就绪确认")

    def test_a_completely_empty_mapping_is_treated_the_same_as_the_round_level_gate(
        self,
    ) -> None:
        """防御性分支：即便调用方没有先做整轮判据（`_match` 的 `publish_allowed`），
        `_publish` 自己遇到整体为空的映射时同样 fail-closed，原因码与整轮判据复用
        同一个值（`permission_translation_unavailable`）——不依赖"调用方一定会先
        做整轮判据"这条外部不变量，与 `permission_refresh._refresh_user` 同一处
        防御性注释同一条纪律。"""

        parts, result = run_once(metric_translation_map={}, publish_allowed=lambda: True)

        self.assertIs(result.state, OnboardingState.STARTED)
        facts = parts["audit"].facts("onboarding.result")
        self.assertEqual(facts["state"], "internal_error")
        self.assertEqual(facts["failure_reason"], "permission_translation_unavailable")
        self.assertEqual(parts["decisions"].rows, [], "映射整体为空时同样零写入")


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
    """`V-开通-24`：存量令牌 adopt-or-issue（Issue #281 改道，2026-08-25 裁定）。

    rc25 S-1 起可采纳的查找结果必须带 ``permissions`` 原文（存量差集导入的输入）；这里
    统一给空对象 ``{}``（旧行没有任何权限 → 没有可导入的内容），差集导入本身的断言见
    ``LegacyPermissionImportTests``。
    """

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
        stock = FakeStockTokens(StockTokenLookup(state=ADOPTABLE, secret=secret, status="approved", permissions="{}"))
        parts, _ = run_once(stock_tokens=stock)
        self.assertEqual(parts["tokens"].calls, [], "有可用存量密文时不该再签新")
        self.assertEqual(parts["tokens"].adopt_calls, [(USER_ID, secret)])
        self.assertEqual(parts["environment"].tokens, [secret], "落进用户环境的必须是采纳的那份明文")
        self.assertEqual(parts["audit"].facts("onboarding.stock_token_adopted")["status_approved"], True)

    def test_adopting_an_existing_row_is_audited_distinctly_from_a_fresh_adoption(self) -> None:
        """幂等：库里已经有这个用户的令牌行时，审计动作名必须与"首次采纳"可分辨。"""

        secret = "stock-plaintext-secret"
        stock = FakeStockTokens(StockTokenLookup(state=ADOPTABLE, secret=secret, permissions="{}"))
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
        stock = FakeStockTokens(StockTokenLookup(state=ADOPTABLE, secret=secret, status="pending", permissions="{}"))
        parts, _ = run_once(stock_tokens=stock)
        self.assertEqual(parts["tokens"].adopt_calls, [(USER_ID, secret)], "非 approved 不阻止采纳")
        self.assertEqual(parts["audit"].facts("onboarding.stock_token_adopted")["status_approved"], False)

    def test_blank_status_counts_as_approved(self) -> None:
        secret = "stock-plaintext-secret"
        stock = FakeStockTokens(StockTokenLookup(state=ADOPTABLE, secret=secret, status="", permissions="{}"))
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
        stock = FakeStockTokens(StockTokenLookup(state=ADOPTABLE, secret=secret, permissions="{}"))
        parts, _ = run_once(stock_tokens=stock)
        rendered = repr(parts["audit"].records)
        self.assertNotIn(secret, rendered)
        self.assertNotIn(self.EMAIL, rendered, "邮箱同样不得进审计（只留 user_id/trace）")


class LegacyPermissionImportTests(unittest.TestCase):
    """存量用户首聊差集导入（rc25 S-1，Issue #540，`V-权限-17`）：正式表已有其行且密文可采纳
    时，把「旧行权限 − 银河当前翻译」经导入口落成本地授权，挂在零银河判定**之前**；形状
    不受支持 / 原文解析失败 / 导入口失败一律 fail-closed（外部表零写入）。

    变异锚点：① 把 `_run` 里 `_import_legacy_permissions` 调用挪到
    `_reject_zero_galaxy_without_local_grant` 之后 →
    ``test_a_zero_galaxy_legacy_user_is_admitted_by_the_imported_rows`` 变红；② 让
    `_import_legacy_permissions` 吞掉导入口异常 →
    ``test_importer_failure_fails_closed_with_zero_external_writes`` 变红。"""

    EMAIL = "Xiaoming@Example.com"

    def _adoptable(self, permissions: str) -> FakeStockTokens:
        return FakeStockTokens(
            StockTokenLookup(state=ADOPTABLE, secret="stock-secret", status="approved", permissions=permissions)
        )

    def test_specific_row_imports_only_the_difference(self) -> None:
        """银河翻译给 88:销售分析；旧行多一个指标与一家映射外公司 → 只导这两对，无组。"""

        importer = FakeLegacyImporter()
        stock = self._adoptable('{"88":["销售分析","旧表指标"],"40":["旧表指标"]}')
        parts, result = run_once(stock_tokens=stock, legacy_importer=importer)

        self.assertIs(result.state, OnboardingState.STARTED)
        self.assertEqual(parts["audit"].facts("onboarding.result")["state"], "completed")
        self.assertEqual(len(importer.calls), 1)
        call = importer.calls[0]
        self.assertEqual(call["user_id"], USER_ID)
        self.assertEqual(call["target_open_id"], OPEN_ID)
        self.assertEqual(call["plan"].pairs, (("40", "旧表指标"), ("88", "旧表指标")))
        self.assertEqual(call["plan"].all_scope_metrics, ())
        facts = parts["audit"].facts("onboarding.legacy_permission_import")
        self.assertEqual(facts["shape"], "specific")
        self.assertEqual(facts["imported"], 2)
        self.assertEqual(facts["unmapped_companies_kept"], 1)
        self.assertEqual(facts["group_created"], False)

    def test_full_wildcard_row_becomes_one_all_scope_group(self) -> None:
        importer = FakeLegacyImporter()
        parts, result = run_once(stock_tokens=self._adoptable('{"*":["*"]}'), legacy_importer=importer)

        self.assertEqual(parts["audit"].facts("onboarding.result")["state"], "completed")
        plan = importer.calls[0]["plan"]
        self.assertEqual(plan.shape, "full_wildcard")
        self.assertEqual(plan.all_scope_metrics, ("销售分析",), "指标数 = 映射并集")
        self.assertEqual(plan.pairs, ())
        self.assertEqual(parts["audit"].facts("onboarding.legacy_permission_import")["group_created"], True)

    def test_an_identical_row_imports_nothing_and_is_audited_as_skipped(self) -> None:
        importer = FakeLegacyImporter()
        parts, _ = run_once(stock_tokens=self._adoptable('{"88":["销售分析"]}'), legacy_importer=importer)

        self.assertEqual(importer.calls, [], "差集为空时不调用导入口")
        facts = parts["audit"].facts("onboarding.legacy_permission_import_skipped")
        self.assertEqual(facts["reasons"], ["nothing_to_import"])
        self.assertEqual(parts["audit"].facts("onboarding.result")["state"], "completed")

    def test_a_zero_galaxy_legacy_user_is_admitted_by_the_imported_rows(self) -> None:
        """变异锚点①：导入必须先于零银河判定，导入行经本地覆盖读回后放行。"""

        importer = FakeLegacyImporter()
        overrides = FakeLocalOverrides()

        def import_plan(**kwargs: Any) -> Any:
            report = FakeLegacyImporter.import_plan(importer, **kwargs)
            # 模拟落库后本地覆盖表可读回这些行。
            overrides._entries[USER_ID] = tuple(
                _override_entry(company_id=company, metric_name=metric) for company, metric in kwargs["plan"].pairs
            )
            return report

        importer.import_plan = import_plan  # type: ignore[method-assign]
        parts, result = run_once(
            role_function_map={},
            stock_tokens=self._adoptable('{"88":["旧表指标"]}'),
            legacy_importer=importer,
            local_overrides=overrides,
        )

        self.assertIs(result.state, OnboardingState.STARTED)
        self.assertEqual(parts["audit"].facts("onboarding.result")["state"], "completed")
        self.assertEqual(parts["decisions"].rows[0].permissions, '{"88":["旧表指标"]}')
        self.assertEqual(parts["tokens"].adopt_calls, [(USER_ID, "stock-secret")])

    def test_unsupported_wildcard_shape_fails_closed(self) -> None:
        importer = FakeLegacyImporter()
        parts, _ = run_once(stock_tokens=self._adoptable('{"88":["*"]}'), legacy_importer=importer)

        facts = parts["audit"].facts("onboarding.result")
        self.assertEqual(facts["state"], "internal_error")
        self.assertEqual(facts["failure_reason"], "legacy_wildcard_shape_unsupported")
        self.assertEqual(parts["notifier"].terminal()[1], KEY_INTERNAL_ERROR)
        self.assertEqual(importer.calls, [])
        self.assertEqual(parts["decisions"].rows, [], "外部表零写入")
        self.assertEqual(parts["environment"].calls, [])
        self.assertEqual(parts["tokens"].adopt_calls, [], "fail-closed 早于令牌采纳")

    def test_a_blank_permissions_cell_imports_nothing(self) -> None:
        """独立审核 P2-2：空白单元格没有任何会被发布覆盖的内容，按 ``{}`` 处理而不是
        永久 fail-closed。"""

        importer = FakeLegacyImporter()
        parts, _ = run_once(stock_tokens=self._adoptable("   "), legacy_importer=importer)
        self.assertEqual(importer.calls, [])
        self.assertEqual(parts["audit"].facts("onboarding.legacy_permission_import_skipped")["reasons"], ["nothing_to_import"])
        self.assertEqual(parts["audit"].facts("onboarding.result")["state"], "completed")

    def test_unparseable_permissions_text_fails_closed(self) -> None:
        for text in ("not json", "[]", '{"88":[" "]}'):
            with self.subTest(text=text):
                importer = FakeLegacyImporter()
                parts, _ = run_once(stock_tokens=self._adoptable(text), legacy_importer=importer)
                facts = parts["audit"].facts("onboarding.result")
                self.assertEqual(facts["state"], "internal_error")
                self.assertEqual(facts["failure_reason"], "legacy_permissions_unparseable")
                self.assertEqual(importer.calls, [])
                self.assertEqual(parts["decisions"].rows, [])

    def test_importer_failure_fails_closed_with_zero_external_writes(self) -> None:
        """变异锚点②。"""

        importer = FakeLegacyImporter(RuntimeError("落库失败"))
        parts, _ = run_once(stock_tokens=self._adoptable('{"88":["旧表指标"]}'), legacy_importer=importer)

        facts = parts["audit"].facts("onboarding.result")
        self.assertEqual(facts["state"], "internal_error")
        self.assertEqual(facts["failure_reason"], "legacy_permission_import_failed_RuntimeError")
        self.assertEqual(parts["decisions"].rows, [])
        self.assertEqual(parts["environment"].calls, [])
        self.assertEqual(
            parts["audit"].facts("onboarding.legacy_permission_import_failed")["reason"],
            "legacy_permission_import_failed_RuntimeError",
        )

    def test_true_full_access_galaxy_skips_the_import(self) -> None:
        importer = FakeLegacyImporter()
        parts, _ = run_once(
            galaxy=FakeGalaxy(AdminRoleGalaxySnapshot()),
            metric_translation_map={"*": {"后台管理员": ("全部指标",)}},
            role_function_map={ADMIN_FULL_ACCESS_FUNCTION: ADMIN_FULL_ACCESS_FUNCTION},
            stock_tokens=self._adoptable('{"*":["*"]}'),
            legacy_importer=importer,
        )
        self.assertEqual(importer.calls, [])
        self.assertEqual(
            parts["audit"].facts("onboarding.legacy_permission_import_skipped")["reasons"],
            ["wildcard_galaxy_current"],
        )

    def test_issue_path_never_calls_the_importer(self) -> None:
        importer = FakeLegacyImporter()
        for lookup in (StockTokenLookup(state=NO_ROW), StockTokenLookup(state=NO_CIPHER, status="pending")):
            with self.subTest(state=lookup.state):
                parts, _ = run_once(stock_tokens=FakeStockTokens(lookup), legacy_importer=importer)
                self.assertEqual(importer.calls, [])
                self.assertNotIn("onboarding.legacy_permission_import", parts["audit"].actions())
                self.assertEqual(parts["tokens"].calls, [USER_ID])

    def test_the_raw_permissions_text_never_reaches_the_audit_trail(self) -> None:
        importer = FakeLegacyImporter()
        parts, _ = run_once(
            stock_tokens=self._adoptable('{"88":["销售分析","独一无二的旧表指标"]}'), legacy_importer=importer
        )
        rendered = repr(parts["audit"].records)
        self.assertNotIn("独一无二的旧表指标", rendered)
        self.assertNotIn(self.EMAIL, rendered)

    def test_translation_failure_happens_before_any_import_or_token(self) -> None:
        importer = FakeLegacyImporter()
        parts, _ = run_once(
            metric_translation_map={"88": {"别的职能": ("x",)}},
            stock_tokens=self._adoptable('{"88":["旧表指标"]}'),
            legacy_importer=importer,
        )
        self.assertEqual(parts["audit"].facts("onboarding.result")["failure_reason"], "permission_translation_uncovered")
        self.assertEqual(importer.calls, [])
        self.assertEqual(parts["tokens"].adopt_calls, [])
        self.assertEqual(parts["environment"].calls, [])


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


class FailureReasonRecordingTests(unittest.TestCase):
    """失败原因落库（Issue #337，可选，见 ``FailureReasonRecorder`` 协议
    文档）：紧邻既有 ``onboarding.result`` 审计，只在真的是一次失败终态时调用
    一次注入的可选口，不含 open_id / 姓名 / 任何资料值。"""

    def test_an_internal_error_terminal_records_the_failure_reason(self) -> None:
        recorder = RecordingFailureReasons()
        parts, _ = run_once(
            directory=FakeDirectory(availability=DirectoryAvailability.STALE),
            failure_reasons=recorder,
        )
        self.assertEqual(
            recorder.calls,
            [
                {
                    "trace_id": "trace_1",
                    "failure_reason": "directory_unavailable",
                    "event_type": "onboarding.result",
                }
            ],
        )

    def test_a_non_failure_terminal_never_records_anything(self) -> None:
        """否定断言：成功终态（``terminal.reason`` 恒为 ``None``）不得落任何
        失败原因行——本测试证明它压根不会被调用。"""

        recorder = RecordingFailureReasons()
        run_once(failure_reasons=recorder)
        self.assertEqual(recorder.calls, [], "开通完成不应该落任何失败原因行")

    def test_a_deterministic_business_failure_also_records_the_failure_reason(self) -> None:
        """与管理员告警回调（只覆盖 INTERNAL_ERROR/SYNC_TIMEOUT）不同：失败原因
        落库覆盖**任何**非空 ``terminal.reason``，包含确定性业务失败
        （``NOT_AUTHORIZED``）——`/admin trace` 需要能如实回答"这条追溯号当时
        判的是什么"，不只是"本侧故障"这一类。"""

        recorder = RecordingFailureReasons()
        parts, _ = run_once(directory=FakeDirectory(members=()), failure_reasons=recorder)
        self.assertEqual(
            parts["audit"].facts("onboarding.result")["state"], "not_authorized"
        )
        self.assertEqual(len(recorder.calls), 1)
        self.assertEqual(recorder.calls[0]["event_type"], "onboarding.result")

    def test_the_recorder_never_receives_open_id_or_profile_values(self) -> None:
        recorder = RecordingFailureReasons()
        run_once(
            directory=FakeDirectory(availability=DirectoryAvailability.STALE),
            failure_reasons=recorder,
        )
        self.assertEqual(len(recorder.calls), 1)
        self.assertNotIn(OPEN_ID, recorder.calls[0].values())
        self.assertEqual(set(recorder.calls[0]), {"trace_id", "failure_reason", "event_type"})

    def test_a_raising_recorder_does_not_break_notification_or_ledger(self) -> None:
        """否定断言：落库失败不得带走用户结论——**故意破坏**确认变红的对照组
        是「没有这条 try/except 时同一用例会抛穿 `_execute`」。"""

        recorder = RecordingFailureReasons(raise_error=True)
        parts, _ = run_once(
            directory=FakeDirectory(availability=DirectoryAvailability.STALE),
            failure_reasons=recorder,
        )
        self.assertEqual(parts["notifier"].terminal()[1], KEY_INTERNAL_ERROR)
        self.assertEqual(parts["ledger"].marked, ["evt_1"])
        self.assertIn("onboarding.failure_reason_record_failed", parts["audit"].actions())

    def test_no_recorder_injected_keeps_prior_behavior(self) -> None:
        """默认 ``failure_reasons=None``：不注入时用户仍然收到冻结文案，行为与
        接线之前逐字节一致。"""

        parts, _ = run_once(directory=FakeDirectory(availability=DirectoryAvailability.STALE))
        self.assertEqual(parts["notifier"].terminal()[1], KEY_INTERNAL_ERROR)


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

    def test_a_suspension_landing_inside_the_publish_transaction_reuses_the_same_terminal(
        self,
    ) -> None:
        """Issue #483：从建档后那次复核到落权限决定之间，管理员恰好完成停用。

        这个窗口是真实形状——中间隔着令牌签发与用户环境创建。落决定那把行锁里的复检
        会挡住这次发布，本用例钉的是**开通链的收口不得退化成通用内部故障**：用户看到
        的仍然是既有的「账号已停用」终态，审计动作与另外两处复核逐字相同。
        """

        parts, _ = run_once(decisions=FakeDecisions(blocked_account_state="suspended"))

        self.assertEqual(parts["notifier"].terminal()[1], KEY_SUSPENDED)
        self.assertIn("onboarding.halted_account_state", parts["audit"].actions())
        self.assertEqual(
            parts["audit"].facts("onboarding.halted_account_state")["account_state"], "suspended"
        )
        # 被挡之后不得继续推进状态机：不写 mcp_syncing、更不写 active。
        self.assertNotIn(STATE_MCP_SYNCING, parts["users"].advanced)
        self.assertNotIn(STATE_ACTIVE, parts["users"].advanced)

    def test_a_normal_onboarding_still_declares_that_it_needs_an_enabled_account(self) -> None:
        """正向断言：开通链恒声明 ``require_enabled_account=True``。

        假 store 里那条 ``assert require_enabled_account is True`` 只在被调用时才生效，
        这条用例保证它真的被调用过一次——否则"声明对不对"根本没有被测。
        """

        parts, _ = run_once()
        self.assertEqual(parts["decisions"].require_enabled_account, [True])

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


class LegacyImporterConstructionTests(unittest.TestCase):
    """结构性防漏接（rc25 S-1）：存量令牌源装了、差集导入口没装 → 构造期 ``TypeError``。"""

    def test_stock_tokens_without_an_importer_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            build_runner(stock_tokens=FakeStockTokens(), legacy_importer=None)

    def test_neither_wired_is_still_the_sentinel(self) -> None:
        runner, _ = build_runner()
        self.assertIsInstance(runner, AutoOnboardingRunner)


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
            metric_translation_map=METRIC_TRANSLATION_MAP,
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
