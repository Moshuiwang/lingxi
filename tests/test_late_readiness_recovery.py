"""迟到就绪恢复职责的纯逻辑验收（V-开通-18，外部独立审查 F1-F4，第三轮 G1-G2 修复）。

十五分钟同步超时之后仍然确认成功的用户，最终会被写成 ``active`` 并得到「可以开始使用」
的主动通知；在此之前他不会收到任何暗示已经可用、实际却发不了问数的消息——这是
`V-开通-18` 的完整断言，本文件承担它的编排半边（判定层的断言在
``tests/test_mcp_readiness_machine.py``，持久化半边的真库断言在
``tests/test_postgres_late_readiness_recovery.py``）。

外部独立审查坐实的必修，本文件各有对应的**会变红**的用例：

- **F1「active 但永远不告知」**：状态推进与排通知是原子操作（由 fake ``_Activator``
  模拟），通知发送失败**不会**丢失——它留在待发 outbox 里，下一轮仍会被重新认领并重试，
  直到真正送达为止。``ActiveButNeverNotifiedIsImpossibleTest`` 是这条修复的核心用例。
- **F2**：候选一律重新探一次，**不存在**"跳过探针直接判就绪"的分支——`FakeCandidate`
  上没有 ``already_ready`` 字段，任何试图依赖它的实现都会在类型上直接失败。
- **F3**：CAS 失败（版本不对/账号停用/已经被推进）**绝不发送任何通知**。
- **F4**：探针**之后**（激活/解析）的未预期异常会调用 ``record_processing_failure``
  占住调度窗口（防止"毒候选"饿死后面排队的候选）；零候选、零待发通知时**不记完成
  审计**。
- **G1（第三轮）**：收件人暂不可用（``notice_recipient_open_id`` 返回 ``None``）**不是**
  永久放弃——它只留在 ``pending`` 按既有退避重试，绝不落到任何终态（真正的"不用再等
  了"只有 ``ON DELETE CASCADE`` 一种事实来源）。``RecipientUnavailableTest`` 钉住这条。
- **G2（第三轮）**：探针调用（``probe_after_timeout``）本身抛出的未预期异常也要占住
  调度窗口——不只是探针**之后**的步骤。``ProcessingFailureTest.
  test_a_probe_call_failure_also_records_a_processing_failure_before_reraising``
  钉住这条：探针那次失败用**当前** ``attempt_no``，探针之后失败继续用**下一个**。

否定断言（合同的"不得 / 不允许"必须有对应否定测试，验证与门禁第八节）：

1. **未就绪（等待中 / 技术失败 / 探针未接线）绝不推进 ``active``、绝不排任何通知**；
2. **CAS 失败绝不发通知**（F3）；
3. **通知行不会重复产生**：同一个 ``dedupe_key`` 不会在 outbox 里出现第二行（数据库
   ``UNIQUE`` 约束，见 ``FakeLateReadinessStore`` 的 ``_by_dedupe`` 模拟）。**这不等于
   "用户不会收到重复消息"**——发送成功到标记送达之间崩溃是已知的「至少一次投递」
   窗口（登记在 ``lingxi.apps.scheduler.late_readiness_recovery`` 模块自己的文档
   字符串），重试会带着同一个 ``dedupe_key`` 再发一次；那一层的去重是飞书平台侧未
   验证的行为（L1/L4a，与 ``adapters/feishu_user_message.py`` 已经登记的边界同型），
   本测试文件不覆盖、也不为它新增补偿逻辑；
4. **单个候选 / 单条通知的失败不带走整轮**；
5. 报告与审计**只有计数**，不含权限值、open_id 或渲染后的正文。
"""

from __future__ import annotations

import threading
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace

from lingxi.apps.scheduler.late_readiness_recovery import (
    DEFAULT_NOTICE_DRAIN_LIMIT,
    DEFAULT_RECOVERY_INTERVAL_SECONDS,
    DEFAULT_RECOVERY_LIMIT,
    LateReadinessRecoveryDuty,
)
from lingxi.core.identity.onboarding_runner import FIRST_ONBOARDING_REASON, KEY_COMPLETED
from lingxi.core.permission.mcp_readiness_base import (
    ReadinessAttempt,
    ReadinessBinding,
    ReadinessOutcome,
)

USER_A = "usr_01JQZX3M5N7P9R1T3V5W7Y9A0B"
USER_B = "usr_01JQZX3M5N7P9R1T3V5W7Y9A0C"
OPEN_ID = "ou_fake_open_id_for_tests"
PERMISSIONS = '{"1011":["日活"]}'
VERSION = 3
MOMENT = datetime(2026, 8, 20, 3, 0, tzinfo=UTC)


def _candidate(
    *,
    user_id: str = USER_A,
    version: int = VERSION,
    next_attempt_no: int = 8,
    system_triggered: bool = False,
) -> SimpleNamespace:
    """真实 ``LateOnboardingCandidate`` 的最小形状。**刻意不含 ``already_ready``**
    （F2）：任何试图读它的实现会在这里直接 ``AttributeError``，而不是安静地拿到
    一个默认值。``system_triggered``（rc25 修复包 F3）：预开通 origin 的链，恢复
    完成必须静默。"""

    return SimpleNamespace(
        user_id=user_id,
        permission_version=version,
        permissions=PERMISSIONS,
        next_attempt_no=next_attempt_no,
        system_triggered=system_triggered,
    )


def _attempt(outcome: ReadinessOutcome) -> ReadinessAttempt:
    kwargs: dict = {}
    if outcome is ReadinessOutcome.READY:
        kwargs = {"metric_count": 4}
    elif outcome is ReadinessOutcome.WAITING:
        kwargs = {"error_code": "empty_metrics", "metric_count": 0}
    else:
        kwargs = {"error_code": "probe_overran_timeout"}
    return ReadinessAttempt(
        binding=ReadinessBinding(USER_A, VERSION),
        attempt_no=8,
        outcome=outcome,
        started_at=MOMENT,
        finished_at=MOMENT,
        **kwargs,
    )


class FakeCandidates:
    """返回一份固定候选列表，**只在第一次调用时**返回（此后为空）——与真实候选查询
    "一旦推进就退出候选集"的效果对齐，让跨轮的幂等/重试类用例不需要额外的状态机。"""

    def __init__(self, *candidates: SimpleNamespace) -> None:
        self._candidates = list(candidates)
        self._consumed = False
        self.calls: list[dict] = []

    def late_onboarding_recovery_candidates(
        self, *, reason: str, recovery_interval_seconds: int, limit: int = 50
    ):
        self.calls.append({"reason": reason, "interval": recovery_interval_seconds, "limit": limit})
        if self._consumed:
            return ()
        self._consumed = True
        return tuple(self._candidates[:limit])


class FakeTicker:
    """按用户脚本返回一次判定；``UNWIRED`` 表示"探针未接线"。"""

    UNWIRED = object()

    def __init__(self, script: dict[str, object] | None = None) -> None:
        self._script = script or {}
        self.calls: list[tuple[str, int, int]] = []
        self.processing_failures: list[tuple[str, int, str]] = []

    def probe_after_timeout(self, binding: ReadinessBinding, *, attempt_no: int):
        self.calls.append((binding.user_id, binding.permission_version, attempt_no))
        step = self._script.get(binding.user_id, ReadinessOutcome.READY)
        if step is self.UNWIRED:
            return None
        if isinstance(step, BaseException):
            raise step
        return _attempt(step)

    def record_processing_failure(
        self, binding: ReadinessBinding, *, attempt_no: int, code: str
    ) -> ReadinessAttempt:
        self.processing_failures.append((binding.user_id, attempt_no, code))
        return ReadinessAttempt(
            binding=binding,
            attempt_no=attempt_no,
            outcome=ReadinessOutcome.TECHNICAL_FAILURE,
            started_at=MOMENT,
            finished_at=MOMENT,
            error_code=code,
        )


class FakeLateReadinessStore:
    """组合 ``_Activator`` + ``_NoticeOutbox``：内存版，行为对齐真实
    ``PostgresLateReadinessStore`` 的两条关键保证——

    1. CAS 失败（``allow`` 里显式标了 ``False``）**不产生任何通知**；
    2. 同一个 ``dedupe_key`` 不会产生第二条通知，即使 ``activate_after_late_readiness``
       被调用多次（真实实现靠数据库 ``UNIQUE`` 约束，这里靠一个 dict）。

    ``claim_one_due_notice`` 不模拟真实的到期节奏（那条断言在真库测试里），只保证
    "还是 pending 就能被认领到"——这正是编排层需要验证的那一半：**失败之后还留在
    outbox 里，下一次调用仍然能捞到它**。
    """

    def __init__(
        self,
        *,
        allow: dict[str, bool] | None = None,
        current_versions: dict[str, int] | None = None,
    ) -> None:
        self._allow = allow or {}
        #: 每个用户"数据库里真实的当前版本"。默认不设，意味着任何版本都通过——
        #: 只有显式配置了才会像真实 CAS 一样核对 ``expected_permission_version``
        #: （F3 的 duty 级证据：探针绑定的版本必须原样传给 CAS，不能被换成别的值）。
        self._current_versions = current_versions or {}
        self.activate_calls: list[tuple[str, int]] = []
        #: rc25 修复包 F3：静默完成（挂起首聊补一句）的用户，供否定用例断言。
        self.armed_silently: list[str] = []
        self._notices: dict[str, dict] = {}
        self._by_dedupe: dict[str, str] = {}
        self._seq = 0

    def activate_after_late_readiness(
        self,
        *,
        user_id: str,
        expected_permission_version: int,
        company_name: str,
        function_name: str,
        dedupe_key: str,
        silent_system_trigger: bool = False,
    ) -> bool:
        self.activate_calls.append((user_id, expected_permission_version))
        if not self._allow.get(user_id, True):
            return False
        current = self._current_versions.get(user_id)
        if current is not None and current != expected_permission_version:
            # 模拟真实 CAS 的 ``AND permission_version = %(expected)s``：版本对不上
            # 就拒绝，不写任何东西。
            return False
        if silent_system_trigger:
            # 与真实实现同语义（rc25 修复包 F3）：系统触发不排任何通知，改挂首聊
            # 补一句；这里只记录"挂起发生过"，供否定用例断言零出站。
            self.armed_silently.append(user_id)
            return True
        if dedupe_key not in self._by_dedupe:
            self._seq += 1
            notice_id = f"obn_{self._seq}"
            self._notices[notice_id] = {
                "notice_id": notice_id,
                "user_id": user_id,
                "permission_version": expected_permission_version,
                "company_name": company_name,
                "function_name": function_name,
                "dedupe_key": dedupe_key,
                "status": "pending",
                # 认领即退避：与真实实现同一条纪律（``claim_one_due_notice`` 在认领的
                # 同一步把下一次到期时间前移）。这里没有真实时钟，用一个布尔位模拟——
                # 一旦被认领就不可用，直到测试显式调用 ``simulate_backoff_elapsed``
                # 模拟"下一次到期时间已经过去"（对应真实系统里时间的流逝）。少了这一位，
                # 单轮内的 ``_drain_notices`` 循环会把同一条失败的通知反复认领到
                # ``notice_limit`` 次，与真实系统的退避语义不符。
                "available": True,
            }
            self._by_dedupe[dedupe_key] = notice_id
        return True

    def claim_one_due_notice(self):
        for notice in self._notices.values():
            if notice["status"] == "pending" and notice["available"]:
                notice["available"] = False
                return SimpleNamespace(
                    **{k: v for k, v in notice.items() if k not in ("status", "available")}
                )
        return None

    def mark_notice_delivered(self, notice_id: str) -> None:
        self._notices[notice_id]["status"] = "delivered"

    def mark_notice_failed(self, notice_id: str, *, error: str) -> None:
        self._notices[notice_id]["last_error"] = error
        # 状态保持 pending：与真实实现一致，退避已经在"认领"这一步算过。

    def simulate_backoff_elapsed(self, user_id: str | None = None) -> None:
        """测试专用：模拟"退避窗口已经过去"（对应真实实现里 ``next_attempt_at``
        到期），让已认领但还没送达的通知重新可被认领。生产代码不会调用这个方法。"""

        for notice in self._notices.values():
            if user_id is None or notice["user_id"] == user_id:
                notice["available"] = True

    def notice_status(self, user_id: str) -> str | None:
        for notice in self._notices.values():
            if notice["user_id"] == user_id:
                return notice["status"]
        return None

    def notice_count(self, user_id: str | None = None) -> int:
        if user_id is None:
            return len(self._notices)
        return sum(1 for n in self._notices.values() if n["user_id"] == user_id)


class FakeRecipients:
    def __init__(self, *, open_ids: dict[str, str | None] | None = None) -> None:
        self._open_ids = {USER_A: OPEN_ID, USER_B: OPEN_ID} if open_ids is None else open_ids
        self.calls: list[str] = []

    def notice_recipient_open_id(self, user_id: str) -> str | None:
        self.calls.append(user_id)
        return self._open_ids.get(user_id)


class FakeNotifier:
    """按用户脚本决定第几次调用才成功；默认恒成功。"""

    def __init__(self, *, fail_first: dict[str, int] | None = None) -> None:
        self._fail_first = fail_first or {}
        self._sent_count: dict[str, int] = {}
        self.calls: list[dict] = []

    def send(self, *, open_id: str, key: str, values, dedupe_key: str) -> None:
        self.calls.append(
            {"open_id": open_id, "key": key, "values": dict(values), "dedupe_key": dedupe_key}
        )
        # 用 dedupe_key 里编码的 user_id 反查该发几次失败（去重键形如
        # "onboarding:recovery:<user>:<version>"）。
        user_id = dedupe_key.split(":")[2] if dedupe_key.count(":") >= 2 else dedupe_key
        count = self._sent_count.get(user_id, 0) + 1
        self._sent_count[user_id] = count
        threshold = self._fail_first.get(user_id, 0)
        if count <= threshold:
            raise RuntimeError("send_failed")


class RecordingAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, dict(fields)))

    def fields_for(self, action: str) -> list[dict]:
        return [fields for name, fields in self.records if name == action]

    def actions(self) -> list[str]:
        return [name for name, _ in self.records]


_UNSET = object()


def build_duty(
    *,
    candidates=_UNSET,
    ticker=_UNSET,
    store: FakeLateReadinessStore | None = None,
    recipients: FakeRecipients | None = None,
    notifier: FakeNotifier | None = None,
    audit: RecordingAudit | None = None,
    reason: str = FIRST_ONBOARDING_REASON,
    stop: threading.Event | None = None,
    **kwargs,
):
    candidates = FakeCandidates(_candidate()) if candidates is _UNSET else candidates
    ticker = FakeTicker() if ticker is _UNSET else ticker
    store = store or FakeLateReadinessStore()
    recipients = recipients or FakeRecipients()
    notifier = notifier or FakeNotifier()
    audit = audit or RecordingAudit()
    duty = LateReadinessRecoveryDuty(
        candidates=candidates,
        ticker=ticker,
        activator=store,
        notices=store,
        recipients=recipients,
        notifier=notifier,
        audit=audit,
        reason=reason,
        stop=stop,
        **kwargs,
    )
    return duty, {
        "candidates": candidates,
        "ticker": ticker,
        "store": store,
        "recipients": recipients,
        "notifier": notifier,
        "audit": audit,
    }


class ConstructionTest(unittest.TestCase):
    def test_reason_is_required(self) -> None:
        for reason in ("", "   "):
            with self.subTest(reason=reason):
                with self.assertRaises(ValueError):
                    build_duty(reason=reason)

    def test_recovery_interval_must_be_a_positive_integer(self) -> None:
        for bad in (0, -1, True, 1.5):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    build_duty(recovery_interval_seconds=bad)

    def test_limit_must_be_a_positive_integer(self) -> None:
        for bad in (0, -1, True):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    build_duty(limit=bad)

    def test_notice_limit_must_be_a_positive_integer(self) -> None:
        for bad in (0, -1, True):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    build_duty(notice_limit=bad)

    def test_defaults_match_the_documented_choices(self) -> None:
        self.assertEqual(DEFAULT_RECOVERY_INTERVAL_SECONDS, 900)
        self.assertEqual(DEFAULT_RECOVERY_LIMIT, 50)
        self.assertEqual(DEFAULT_NOTICE_DRAIN_LIMIT, 50)


class ReadyCandidateTest(unittest.TestCase):
    """核心正向：超时后就绪 → 原子推进 ``active`` + 排通知 → 同一轮把通知发出去。"""

    def test_a_late_success_activates_and_delivers_in_the_same_tick(self) -> None:
        duty, seams = build_duty(
            candidates=FakeCandidates(_candidate()),
            ticker=FakeTicker({USER_A: ReadinessOutcome.READY}),
        )

        report = duty.run_once()

        self.assertEqual(report.examined, 1)
        self.assertEqual(report.ready, 1)
        self.assertEqual(report.activated, 1)
        self.assertEqual(report.notices_claimed, 1)
        self.assertEqual(report.notified, 1)
        self.assertEqual(report.failed, 0)
        self.assertEqual(seams["store"].activate_calls, [(USER_A, VERSION)])
        self.assertEqual(len(seams["notifier"].calls), 1)
        sent = seams["notifier"].calls[0]
        self.assertEqual(sent["key"], KEY_COMPLETED)
        self.assertEqual(sent["open_id"], OPEN_ID)
        self.assertEqual(sent["dedupe_key"], f"onboarding:recovery:{USER_A}:{VERSION}")
        self.assertEqual(seams["store"].notice_status(USER_A), "delivered")

    def test_the_completion_text_reports_the_users_actual_scope(self) -> None:
        candidate = _candidate()
        candidate.permissions = '{"2022":["收入","留存"]}'
        duty, seams = build_duty(
            candidates=FakeCandidates(candidate),
            ticker=FakeTicker({USER_A: ReadinessOutcome.READY}),
        )

        duty.run_once()

        values = seams["notifier"].calls[0]["values"]
        self.assertEqual(values["company_name"], "2022")
        self.assertEqual(sorted(values["function_name"].split("、")), ["收入", "留存"])


class PreprovisionSilentRecoveryTest(unittest.TestCase):
    """rc25 修复包 F3：系统触发（预开通）的迟到就绪恢复**静默完成**。

    产品负责人裁定 4：预开通全程静默、首聊时才补一句。恢复完成的「开通完成」私聊
    对预开通用户违反这条承诺，因此：状态照常推进 active，但**不排任何通知**，改在
    同一个原子事务里挂起首聊补一句（适配器行为见
    ``tests/test_postgres_late_readiness_recovery.py``）。用户自己发起的链一字不变
    （本文件其余用例全部跑在 ``system_triggered=False`` 上，就是那半边的钉子）。
    """

    def test_a_preprovisioned_recovery_activates_without_any_outbound_message(self) -> None:
        duty, seams = build_duty(
            candidates=FakeCandidates(_candidate(system_triggered=True)),
            ticker=FakeTicker({USER_A: ReadinessOutcome.READY}),
        )

        report = duty.run_once()

        self.assertEqual(report.activated, 1, "静默不等于不恢复：照常推进 active")
        self.assertEqual(report.activated_silently, 1)
        self.assertEqual(seams["notifier"].calls, [], "预开通 origin 不产生任何出站消息")
        self.assertEqual(seams["store"].armed_silently, [USER_A], "改挂首聊补一句")
        self.assertEqual(report.notices_claimed, 0, "没有任何通知进 outbox")

        # 第二轮（通知面独立运行）也不得凭空长出消息。
        second = duty.run_once()
        self.assertEqual(seams["notifier"].calls, [])
        self.assertEqual(second.notified, 0)

    def test_the_silent_completion_leaves_an_audit_trail(self) -> None:
        """静默路径的审计不能少：这条审计是这次恢复在观测面上唯一的完成记录。"""

        duty, seams = build_duty(
            candidates=FakeCandidates(_candidate(system_triggered=True)),
            ticker=FakeTicker({USER_A: ReadinessOutcome.READY}),
        )
        duty.run_once()
        self.assertIn(
            "late_readiness_recovery.activated_silently",
            [action for action, _ in seams["audit"].records],
        )


class NotReadyCandidateTest(unittest.TestCase):
    """核心负向：未就绪 → 不写 ``active``、不排任何通知、不发任何暗示可用的消息。"""

    def test_waiting_does_not_activate_or_enqueue_a_notice(self) -> None:
        duty, seams = build_duty(ticker=FakeTicker({USER_A: ReadinessOutcome.WAITING}))

        report = duty.run_once()

        self.assertEqual(report.waiting, 1)
        self.assertEqual(report.activated, 0)
        self.assertEqual(report.notified, 0)
        self.assertEqual(seams["store"].activate_calls, [], "未就绪绝不能调用推进")
        self.assertEqual(seams["notifier"].calls, [], "未就绪绝不能发送任何消息")

    def test_technical_failure_does_not_activate_or_enqueue_a_notice(self) -> None:
        duty, seams = build_duty(ticker=FakeTicker({USER_A: ReadinessOutcome.TECHNICAL_FAILURE}))

        report = duty.run_once()

        self.assertEqual(report.technical_failures, 1)
        self.assertEqual(report.activated, 0)
        self.assertEqual(seams["store"].activate_calls, [])
        self.assertEqual(seams["notifier"].calls, [])

    def test_probe_unwired_leaves_the_candidate_untouched(self) -> None:
        duty, seams = build_duty(ticker=FakeTicker({USER_A: FakeTicker.UNWIRED}))

        report = duty.run_once()

        self.assertEqual(report.probe_unwired, 1)
        self.assertEqual(report.activated, 0)
        self.assertEqual(seams["store"].activate_calls, [])
        self.assertEqual(seams["notifier"].calls, [])

    def test_a_missing_ticker_never_activates(self) -> None:
        duty, seams = build_duty(candidates=FakeCandidates(_candidate()), ticker=None)

        report = duty.run_once()

        self.assertEqual(report.probe_unwired, 1)
        self.assertEqual(report.activated, 0)
        self.assertEqual(seams["store"].activate_calls, [])

    def test_the_duty_reports_whether_the_probe_face_is_wired(self) -> None:
        duty, _ = build_duty(ticker=None)
        self.assertFalse(duty.probe_wired)

        duty2, _ = build_duty()
        self.assertTrue(duty2.probe_wired)


class CasFailureTest(unittest.TestCase):
    """F3：CAS 失败（版本不对 / 账号停用 / 已被别的路径推进）绝不发任何通知。"""

    def test_a_refused_activation_sends_no_notice(self) -> None:
        duty, seams = build_duty(
            ticker=FakeTicker({USER_A: ReadinessOutcome.READY}),
            store=FakeLateReadinessStore(allow={USER_A: False}),
        )

        report = duty.run_once()

        self.assertEqual(report.ready, 1)
        self.assertEqual(report.activated, 0)
        self.assertEqual(report.advance_refused, 1)
        self.assertEqual(report.notified, 0)
        self.assertEqual(report.notices_claimed, 0)
        self.assertEqual(seams["notifier"].calls, [])
        self.assertIn("late_readiness_recovery.advance_refused", seams["audit"].actions())

    def test_the_duty_threads_the_candidates_own_version_into_the_cas(self) -> None:
        """F3 的编排半边：探针绑定的是候选自带的 ``permission_version``，这个值必须
        原样传给 CAS，不能被换成一个常量或别的来源——否则版本守卫在类型上成立，但
        实际传入的值是假的，守卫形同虚设。用一个"数据库里真实版本"与候选版本不一致
        的场景验证：CAS 因此拒绝，不推进、不通知。真实 SQL 的版本比对本身由
        ``tests/test_postgres_late_readiness_recovery.py`` 的真库用例钉住。"""

        store = FakeLateReadinessStore(current_versions={USER_A: VERSION + 1})
        duty, seams = build_duty(
            candidates=FakeCandidates(_candidate(version=VERSION)),
            ticker=FakeTicker({USER_A: ReadinessOutcome.READY}),
            store=store,
        )

        report = duty.run_once()

        self.assertEqual(report.ready, 1)
        self.assertEqual(report.activated, 0, "版本已经过时，CAS 必须拒绝")
        self.assertEqual(report.advance_refused, 1)
        self.assertEqual(seams["notifier"].calls, [], "版本过时的人绝不能收到任何通知")
        self.assertEqual(store.notice_count(), 0)


class ActiveButNeverNotifiedIsImpossibleTest(unittest.TestCase):
    """F1 核心场景：状态推进成功之后，通知发送失败**不会**让用户永久收不到那句话——
    它留在待发 outbox 里，下一轮会被重新认领并重试，直到真正送达。
    """

    def test_a_notify_failure_is_retried_on_the_next_tick_until_delivered(self) -> None:
        store = FakeLateReadinessStore()
        notifier = FakeNotifier(fail_first={USER_A: 2})  # 前两次失败，第三次成功
        duty, seams = build_duty(
            candidates=FakeCandidates(_candidate()),
            ticker=FakeTicker({USER_A: ReadinessOutcome.READY}),
            store=store,
            notifier=notifier,
        )

        first = duty.run_once()
        self.assertEqual(first.activated, 1, "状态已经真实推进")
        self.assertEqual(first.notified, 0, "第一次发送失败")
        self.assertEqual(first.notice_failed, 1)
        self.assertEqual(store.notice_status(USER_A), "pending", "失败之后仍然留在 outbox 里")

        # 第二轮：没有新候选（已经 active），但待发通知仍然在，仍然会被重新认领
        # （``simulate_backoff_elapsed`` 模拟"到期时间已经过去"，对应真实系统里
        # 两轮之间真的流逝的时间）。
        store.simulate_backoff_elapsed(USER_A)
        second = duty.run_once()
        self.assertEqual(second.examined, 0, "已经推进过的人不再是候选")
        self.assertEqual(second.notices_claimed, 1, "失败的通知必须被重新认领")
        self.assertEqual(second.notified, 0, "第二次仍然失败")
        self.assertEqual(store.notice_status(USER_A), "pending")

        # 第三轮：发送成功，终于送达。
        store.simulate_backoff_elapsed(USER_A)
        third = duty.run_once()
        self.assertEqual(third.notified, 1)
        self.assertEqual(store.notice_status(USER_A), "delivered")

        # 全程只调用了一次 activate（不会因为通知重试而重复推进/重复排通知）。
        self.assertEqual(store.activate_calls, [(USER_A, VERSION)])
        self.assertEqual(store.notice_count(USER_A), 1, "自始至终只有一条通知")
        # 用户最终恰好收到一次成功送达。
        delivered_sends = sum(
            1
            for call in notifier.calls
            if call["dedupe_key"] == f"onboarding:recovery:{USER_A}:{VERSION}"
        )
        self.assertEqual(delivered_sends, 3, "两次失败尝试 + 一次成功，但只有一次真正送达")

    def test_a_permanently_failing_notifier_never_silently_gives_up(self) -> None:
        """否定断言：即使发送**一直**失败，通知也不会从 outbox 里消失——它不是"重试
        三次就放弃"，而是持久重试（配额与退避在真库层面，这里只验证编排层不会主动
        丢弃）。"""

        store = FakeLateReadinessStore()
        notifier = FakeNotifier(fail_first={USER_A: 999})
        duty, _ = build_duty(
            candidates=FakeCandidates(_candidate()),
            ticker=FakeTicker({USER_A: ReadinessOutcome.READY}),
            store=store,
            notifier=notifier,
        )

        for _ in range(5):
            store.simulate_backoff_elapsed(USER_A)
            duty.run_once()

        self.assertEqual(store.notice_status(USER_A), "pending", "从未被标记为放弃")
        self.assertEqual(store.notice_count(USER_A), 1, "重试不会产生第二条通知")


class RecipientUnavailableTest(unittest.TestCase):
    """外部独立审查第三轮 G1：``notice_recipient_open_id`` 只能回答"这一刻查不到"，
    答不出"这是不是永久性的"——账号被停用完全可能只是暂时的。提前判死会让一个
    已经 ``active`` 的人永远等不到「开通完成」，原路复活 F1 要堵的洞。真正的
    "不用再等了"只有 ``ON DELETE CASCADE``（真删除）一种事实来源，因此这里必须
    停在 ``pending``、按既有退避重试，绝不能落到任何终态。"""

    def test_missing_recipient_keeps_the_notice_pending_for_retry(self) -> None:
        store = FakeLateReadinessStore()
        duty, seams = build_duty(
            candidates=FakeCandidates(_candidate()),
            ticker=FakeTicker({USER_A: ReadinessOutcome.READY}),
            store=store,
            recipients=FakeRecipients(open_ids={USER_A: None}),
        )

        report = duty.run_once()

        self.assertEqual(report.activated, 1, "状态已经真实推进，不因为暂时联系不上而回滚")
        self.assertEqual(report.notice_recipient_unavailable, 1)
        self.assertEqual(report.notified, 0)
        self.assertEqual(seams["notifier"].calls, [])
        # 真实实现只有 pending / delivered 两种状态：收件人暂不可用必须停在
        # pending——这正是 G1 要堵的洞（旧实现会把它标成一个从此再也不会被
        # claim_one_due_notice 认领到的终态）。
        self.assertEqual(store.notice_status(USER_A), "pending")

    def test_a_recipient_that_becomes_available_later_still_gets_notified(self) -> None:
        """不只是"没被标终态"——还要证明它后续真的会被重试送达。"""

        store = FakeLateReadinessStore()
        recipients = FakeRecipients(open_ids={USER_A: None})
        duty, seams = build_duty(
            candidates=FakeCandidates(_candidate()),
            ticker=FakeTicker({USER_A: ReadinessOutcome.READY}),
            store=store,
            recipients=recipients,
        )
        duty.run_once()
        self.assertEqual(seams["notifier"].calls, [])

        # 账号"复活"：现在能查到 open_id 了（对应真实世界"停用"变回"启用"）。
        recipients._open_ids[USER_A] = OPEN_ID
        store.simulate_backoff_elapsed(USER_A)

        report = duty.run_once()

        self.assertEqual(report.notified, 1, "暂不可用之后一旦重新可达，仍然要收到「开通完成」")
        self.assertEqual(len(seams["notifier"].calls), 1)
        self.assertEqual(store.notice_status(USER_A), "delivered")


class ProcessingFailureTest(unittest.TestCase):
    """F4：探针之外的未预期异常也要占住调度窗口。"""

    def test_an_activation_failure_records_a_processing_failure_before_reraising(
        self,
    ) -> None:
        class ExplodingStore(FakeLateReadinessStore):
            def activate_after_late_readiness(self, **kwargs):
                raise RuntimeError("db_write_failed")

        store = ExplodingStore()
        ticker = FakeTicker({USER_A: ReadinessOutcome.READY})
        duty, seams = build_duty(
            candidates=FakeCandidates(_candidate()), ticker=ticker, store=store
        )

        report = duty.run_once()

        self.assertEqual(report.failed, 1)
        self.assertEqual(len(ticker.processing_failures), 1)
        failed_user, attempt_no, code = ticker.processing_failures[0]
        self.assertEqual(failed_user, USER_A)
        self.assertEqual(attempt_no, 9, "探针已经真的探成功过一次，占位用下一个 attempt_no")
        self.assertIn("RuntimeError", code)
        self.assertIn("late_readiness_recovery.user_failed", seams["audit"].actions())

    def test_a_probe_call_failure_also_records_a_processing_failure_before_reraising(
        self,
    ) -> None:
        """G2：外部独立审查第三轮坐实——上一轮的保护只包住了探针**之后**的异常，
        ``probe_after_timeout`` 调用本身（或它内部的落库）抛出未预期异常时完全没有
        保护，直接穿透到外层只记一条泛化的 ``user_failed`` 审计，``last_started_at``
        永远不前移，毒候选会在下一个 tick 立刻重新被选中、无限热循环，饿死队列
        后面的候选。这条用例钉住修复：探针调用要有它自己独立的异常保护。"""

        ticker = FakeTicker({USER_A: RuntimeError("probe_write_failed")})
        duty, seams = build_duty(candidates=FakeCandidates(_candidate()), ticker=ticker)

        report = duty.run_once()

        self.assertEqual(report.failed, 1)
        self.assertEqual(len(ticker.processing_failures), 1)
        failed_user, attempt_no, code = ticker.processing_failures[0]
        self.assertEqual(failed_user, USER_A)
        # 探针那一次没有真的探成功——占的是**当前**这次尝试的号，不是下一个
        # （探针之后的激活失败才用"下一个"，因为那时探针已经真的记过账）。
        self.assertEqual(attempt_no, 8, "探针没探成功，占用当前 attempt_no，不是下一个")
        self.assertIn("RuntimeError", code)
        self.assertIn("late_readiness_recovery.user_failed", seams["audit"].actions())


class RoundBehaviourTest(unittest.TestCase):
    def test_stop_signal_before_the_round_does_nothing(self) -> None:
        stop = threading.Event()
        stop.set()
        duty, seams = build_duty(stop=stop)

        report = duty.run_once()

        self.assertIsNone(report)
        self.assertEqual(seams["candidates"].calls, [])

    def test_stop_signal_mid_round_interrupts_and_leaves_the_rest_untouched(self) -> None:
        stop = threading.Event()

        class StoppingTicker(FakeTicker):
            def probe_after_timeout(self, binding, *, attempt_no):
                stop.set()
                return super().probe_after_timeout(binding, attempt_no=attempt_no)

        duty, seams = build_duty(
            candidates=FakeCandidates(_candidate(user_id=USER_A), _candidate(user_id=USER_B)),
            ticker=StoppingTicker({USER_A: ReadinessOutcome.READY, USER_B: ReadinessOutcome.READY}),
            stop=stop,
        )

        report = duty.run_once()

        self.assertTrue(report.interrupted)
        self.assertEqual(report.examined, 1, "停止信号之后不再处理下一个候选")
        self.assertEqual(seams["ticker"].calls, [(USER_A, VERSION, 8)])

    def test_a_single_candidate_failure_does_not_take_down_the_round(self) -> None:
        class ExplodingTicker(FakeTicker):
            def probe_after_timeout(self, binding, *, attempt_no):
                if binding.user_id == USER_A:
                    raise RuntimeError("boom")
                return super().probe_after_timeout(binding, attempt_no=attempt_no)

        duty, seams = build_duty(
            candidates=FakeCandidates(_candidate(user_id=USER_A), _candidate(user_id=USER_B)),
            ticker=ExplodingTicker({USER_B: ReadinessOutcome.READY}),
        )

        report = duty.run_once()

        self.assertEqual(report.failed, 1)
        self.assertEqual(report.activated, 1, "另一个用户照常处理完")
        self.assertIn("late_readiness_recovery.user_failed", seams["audit"].actions())

    def test_a_single_notice_failure_does_not_take_down_the_round(self) -> None:
        store = FakeLateReadinessStore()

        class ExplodingRecipients(FakeRecipients):
            def notice_recipient_open_id(self, user_id):
                raise RuntimeError("db_down")

        duty, seams = build_duty(
            candidates=FakeCandidates(_candidate()),
            ticker=FakeTicker({USER_A: ReadinessOutcome.READY}),
            store=store,
            recipients=ExplodingRecipients(),
        )

        report = duty.run_once()

        self.assertEqual(report.activated, 1)
        self.assertEqual(report.failed, 1)
        self.assertIn("late_readiness_recovery.notice_processing_failed", seams["audit"].actions())

    def test_the_round_is_scoped_to_the_declared_reason(self) -> None:
        duty, seams = build_duty()

        duty.run_once()

        self.assertEqual(seams["candidates"].calls[0]["reason"], FIRST_ONBOARDING_REASON)

    def test_zero_candidates_and_zero_notices_write_no_completed_audit(self) -> None:
        """F4：健康系统里绝大多数 tick 什么都不该做——不该无条件刷一条空审计。"""

        duty, seams = build_duty(candidates=FakeCandidates(), ticker=FakeTicker())

        report = duty.run_once()

        self.assertEqual(report.examined, 0)
        self.assertEqual(report.notices_claimed, 0)
        self.assertNotIn("late_readiness_recovery.completed", seams["audit"].actions())

    def test_some_activity_does_write_a_completed_audit(self) -> None:
        duty, seams = build_duty(ticker=FakeTicker({USER_A: ReadinessOutcome.WAITING}))

        duty.run_once()

        self.assertIn("late_readiness_recovery.completed", seams["audit"].actions())

    def test_the_report_and_audit_carry_no_field_values(self) -> None:
        """报告与审计只有计数与固定分类，不含权限值、open_id 或渲染后的正文。"""

        duty, seams = build_duty(ticker=FakeTicker({USER_A: ReadinessOutcome.READY}))

        report = duty.run_once()

        for value in report.audit_facts().values():
            self.assertNotIn(OPEN_ID, str(value))
            self.assertNotIn(PERMISSIONS, str(value))
        for _, fields in seams["audit"].records:
            for value in fields.values():
                self.assertNotIn(OPEN_ID, str(value))
                self.assertNotIn(PERMISSIONS, str(value))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
