"""权限发布消费与就绪确认职责的纯逻辑验收（Issue #156 / S-C-03b）。

**本文件承担 `V-权限-07` 的调度与消费面**：每日刷新排出来的发布意图真的会被消费、
发布读回一致之后真的会产生 MCP 同步确认链、以及权限变化真的会告知用户。`V-权限-08`
刷新侧的另一半（撤权行怎么结算）在 ``tests/test_permission_refresh_duty.py``。

否定断言（合同的"不得 / 不允许"必须有对应否定测试，验证与门禁第八节）：

1. **就绪没成功绝不发"范围更新"通知**（等待中、技术失败、超时都不发）；
2. **撤权通知只在发布读回一致之后发**——候选集只来自已发布的意图；
3. 通知失败**不阻塞权限生效、不改变发布或就绪状态、不抛异常**；
4. **一轮 tick 不被就绪等待阻塞**：整轮零 ``sleep``，职责也没有等待端口；
5. 缺 MCP 端点或主密钥 → **就绪面不装配 + 恰一条审计，发布面照常**；
6. 收件人不可用（未开通 / 停用 / 删除中）**不发通知**；
7. 停止信号之后**零发布、零探针、零通知**；
8. 单个用户失败不带走整轮；
9. 报告与审计**只有计数**，不含邮箱、姓名、权限值与 open_id。
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import pathlib
import threading
import unittest
from datetime import datetime, timedelta, timezone

from lingxi.apps.scheduler import (
    PermissionPublishDuty,
    PermissionPublishReport,
    ReadinessFollowUp,
    SchedulerConfig,
    SchedulerLoop,
    build_loop,
)
from lingxi.apps.scheduler.permission_publish import (
    DEFAULT_PUBLISH_LIMIT,
    DEFAULT_READINESS_LIMIT,
    FOLLOW_UP_REASONS,
)
from lingxi.apps.scheduler.permission_refresh import (
    PERMISSION_REFRESH_REASON,
    PERMISSION_REVOKE_REASON,
)
from lingxi.core.permission.mcp_readiness import (
    ReadinessAttempt,
    ReadinessBinding,
    ReadinessOutcome,
    ReadinessProgress,
)
from lingxi.core.permission.notification import NoticeKind, NoticeResult

REPOSITORY_ROOT = pathlib.Path(__file__).parents[1]
DUTY_SOURCE = REPOSITORY_ROOT / "src" / "lingxi" / "apps" / "scheduler" / "permission_publish.py"


def duty_code() -> str:
    """职责源码**去掉全部文档字符串与注释**之后的正文。

    形状断言钉的是代码，不是文档：模块文档里恰恰必须写明"绝不 sleep""只取已发布的
    意图"这些词，拿原文扫描会把一份写得越清楚的文档判得越红（同
    ``tests/test_permission_refresh_duty.py`` 的同名助手）。
    """

    tree = ast.parse(DUTY_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            del body[0]
            if not body:
                body.append(ast.Pass())
    return ast.unparse(tree)


NOW = datetime(2026, 8, 18, 3, 0, tzinfo=timezone.utc)
USER_ONE = "usr_01JQZX3M5N7P9R1T3V5W7Y9A0B"
USER_TWO = "usr_01JQZX3M5N7P9R1T3V5W7Y9A0C"
OPEN_ID = "ou_fake_open_id_for_tests"
EMAIL = "jiaming.jia@example.invalid"
GRANTED = '{"1011":["日活"]}'
REVOKED = "{}"
SPEC_MASTER_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
COMPLETE_ENV = {
    "LINGXI_POSTGRES_DSN": "postgresql://user@localhost:5432/lingxi",
    "LINGXI_FEISHU_APP_ID": "cli_fake",
    "LINGXI_FEISHU_APP_SECRET": "secret_fake",
}


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


class FakeAttempt:
    """发布执行器返回的一次尝试的最小形状。"""

    def __init__(self, *, published: bool) -> None:
        self.published = published


class FakeExecutor:
    def __init__(self, *attempts: FakeAttempt) -> None:
        self._attempts = attempts
        self.calls: list[int] = []

    def run_once(self, *, limit: int = 50):
        self.calls.append(limit)
        return self._attempts


class FakePending:
    def __init__(self, user_id: str, permission_version: int, permissions: str) -> None:
        self.user_id = user_id
        self.permission_version = permission_version
        self.permissions = permissions


class FakeIntents:
    def __init__(
        self,
        *pending: FakePending,
        recipients: dict[str, str] | None = None,
        reclaimed: int = 0,
    ) -> None:
        self._pending = pending
        self._recipients = {USER_ONE: OPEN_ID, USER_TWO: OPEN_ID} if recipients is None else recipients
        self._reclaimed = reclaimed
        self.reclaim_calls = 0
        self.pending_calls: list[int] = []
        self.reason_calls: list[tuple[str, ...]] = []
        self.recipient_calls: list[str] = []

    def reclaim_stale(self, *, older_than: timedelta = timedelta(minutes=15)) -> int:
        self.reclaim_calls += 1
        return self._reclaimed

    def published_awaiting_readiness(self, *, reasons, limit: int = 50):
        self.pending_calls.append(limit)
        self.reason_calls.append(tuple(reasons))
        return self._pending

    def notice_recipient_open_id(self, user_id: str) -> str | None:
        self.recipient_calls.append(user_id)
        return self._recipients.get(user_id)


class FakeChecks:
    def __init__(self, progress: dict[str, tuple] | None = None) -> None:
        self._rows = progress or {}
        self.calls: list[tuple[str, int]] = []

    def load_checks(self, user_id: str, permission_version: int):
        self.calls.append((user_id, permission_version))
        return self._rows.get(user_id, ())


class FakeTicker:
    """按用户脚本返回一次判定；``None`` 表示"本轮没到期"。"""

    def __init__(self, script: dict[str, object] | None = None) -> None:
        self._script = script or {}
        self.calls: list[tuple[str, int, str]] = []

    def advance(self, binding: ReadinessBinding, *, permissions: str, progress: ReadinessProgress):
        self.calls.append((binding.user_id, binding.permission_version, permissions))
        step = self._script.get(binding.user_id)
        if isinstance(step, BaseException):
            raise step
        if step is None:
            return None
        return _attempt(binding, step)


def _attempt(binding: ReadinessBinding, outcome: ReadinessOutcome) -> ReadinessAttempt:
    kwargs: dict = {"error_code": None, "metric_count": None}
    if outcome is ReadinessOutcome.READY:
        kwargs = {"metric_count": 3}
    elif outcome is ReadinessOutcome.WAITING:
        kwargs = {"error_code": "empty_metrics", "metric_count": 0}
    else:
        kwargs = {"error_code": "no_publishable_permission"}
    return ReadinessAttempt(
        binding=binding,
        attempt_no=1,
        outcome=outcome,
        started_at=NOW,
        finished_at=NOW,
        **kwargs,
    )


class FakeNotices:
    def __init__(self, *, delivered: bool = True, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._delivered = delivered
        self._error = error

    def notify(self, *, user_id: str, open_id: str, permission_version: int, permissions: str):
        self.calls.append(
            {
                "user_id": user_id,
                "open_id": open_id,
                "permission_version": permission_version,
                "permissions": permissions,
            }
        )
        if self._error is not None:
            raise self._error
        kind = NoticeKind.RANGE_REVOKED if permissions == REVOKED else NoticeKind.RANGE_UPDATED
        return NoticeResult(
            delivered=self._delivered,
            kind=kind,
            content_key=f"permission.{kind.value}",
            content_version="2026-08-18",
            attempts=1 if self._delivered else 3,
            error_code=None if self._delivered else "RuntimeError",
        )


class RecordingAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, dict(fields)))

    def fields_for(self, action: str) -> list[dict]:
        return [fields for name, fields in self.records if name == action]


def build_duty(
    *,
    executor: FakeExecutor | None = None,
    intents: FakeIntents | None = None,
    ticker: FakeTicker | None = None,
    checks: FakeChecks | None = None,
    notices: FakeNotices | None = None,
    audit: RecordingAudit | None = None,
    stop: threading.Event | None = None,
    wire_readiness: bool = True,
):
    executor = executor or FakeExecutor()
    intents = intents or FakeIntents()
    ticker = ticker or FakeTicker()
    checks = checks or FakeChecks()
    notices = notices or FakeNotices()
    audit = audit or RecordingAudit()
    readiness = (
        ReadinessFollowUp(ticker=ticker, checks=checks, notices=notices)
        if wire_readiness
        else None
    )
    duty = PermissionPublishDuty(
        executor=executor,
        intents=intents,
        audit=audit,
        readiness=readiness,
        stop=stop,
    )
    return duty, {
        "executor": executor,
        "intents": intents,
        "ticker": ticker,
        "checks": checks,
        "notices": notices,
        "audit": audit,
    }


# --------------------------------------------------------------------------
# 一、发布面
# --------------------------------------------------------------------------


class PublishFaceTest(unittest.TestCase):
    def test_every_tick_reclaims_and_drives_the_executor(self) -> None:
        """S-C-01 的发布执行器在本 Story 之前**没有任何生产调用方**。"""

        duty, parts = build_duty(
            executor=FakeExecutor(FakeAttempt(published=True), FakeAttempt(published=False)),
            intents=FakeIntents(reclaimed=2),
        )

        report = duty.run_once()

        self.assertEqual(parts["intents"].reclaim_calls, 1, "崩溃留下的在途意图每轮收殓")
        self.assertEqual(parts["executor"].calls, [DEFAULT_PUBLISH_LIMIT])
        self.assertEqual(report.attempts, 2)
        self.assertEqual(report.published, 1)
        self.assertEqual(report.reclaimed, 2)

    def test_the_readiness_sweep_asks_for_its_own_budget(self) -> None:
        duty, parts = build_duty()

        duty.run_once()

        self.assertEqual(parts["intents"].pending_calls, [DEFAULT_READINESS_LIMIT])

    def test_the_sweep_only_claims_the_reasons_this_duty_owns(self) -> None:
        """否定断言：**首次开通那条意图不归本职责确认**。

        它由 Epic D 的开通编排自己确认并发"开通完成"；两边都捞的话，一个刚开通的用户
        会在"开通完成"之外再收到一条措辞完全不同的"可用范围已更新"，而且两个确认还会
        对同一个 (用户, 权限版本) 并发发探针。
        """

        duty, parts = build_duty()

        duty.run_once()

        self.assertEqual(parts["intents"].reason_calls, [FOLLOW_UP_REASONS])
        self.assertEqual(
            set(FOLLOW_UP_REASONS), {PERMISSION_REFRESH_REASON, PERMISSION_REVOKE_REASON}
        )
        self.assertNotIn("first_onboarding", FOLLOW_UP_REASONS)

    def test_illegal_budgets_are_rejected(self) -> None:
        for kwargs in ({"publish_limit": 0}, {"readiness_limit": -1}, {"publish_limit": True}):
            with self.subTest(kwargs):
                with self.assertRaises(ValueError):
                    PermissionPublishDuty(
                        executor=FakeExecutor(), intents=FakeIntents(), audit=RecordingAudit(), **kwargs
                    )

    def test_a_half_wired_readiness_face_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            PermissionPublishDuty(
                executor=FakeExecutor(),
                intents=FakeIntents(),
                audit=RecordingAudit(),
                readiness=object(),  # type: ignore[arg-type]
            )


# --------------------------------------------------------------------------
# 二、就绪 → 通知
# --------------------------------------------------------------------------


class ReadinessNoticeTest(unittest.TestCase):
    def _run(self, outcome, *, permissions=GRANTED, **kwargs):
        duty, parts = build_duty(
            intents=FakeIntents(FakePending(USER_ONE, 2, permissions), **kwargs.pop("intents", {})),
            ticker=FakeTicker({USER_ONE: outcome}),
            **kwargs,
        )
        return duty.run_once(), parts

    def test_a_ready_probe_sends_the_range_updated_notice(self) -> None:
        report, parts = self._run(ReadinessOutcome.READY)

        self.assertEqual(len(parts["notices"].calls), 1)
        call = parts["notices"].calls[0]
        self.assertEqual(call["user_id"], USER_ONE)
        self.assertEqual(call["open_id"], OPEN_ID)
        self.assertEqual(call["permission_version"], 2)
        self.assertEqual(call["permissions"], GRANTED)
        self.assertEqual(report.ready, 1)
        self.assertEqual(report.notices_sent, 1)

    def test_a_waiting_probe_sends_nothing(self) -> None:
        """否定断言：**就绪未成功绝不发"范围更新"通知。**"""

        report, parts = self._run(ReadinessOutcome.WAITING)

        self.assertEqual(parts["notices"].calls, [])
        self.assertEqual(report.notices_sent, 0)
        self.assertEqual(report.advanced, 1)

    def test_a_technical_failure_sends_nothing(self) -> None:
        report, parts = self._run(ReadinessOutcome.TECHNICAL_FAILURE)

        self.assertEqual(parts["notices"].calls, [])
        self.assertEqual(report.notices_sent, 0)

    def test_a_timeout_sends_nothing(self) -> None:
        """否定断言：超时**不通知**——我们没能确认这个人真的可以问数。"""

        report, parts = self._run(ReadinessOutcome.TIMED_OUT)

        self.assertEqual(parts["notices"].calls, [])
        self.assertEqual(report.timed_out, 1)
        self.assertEqual(report.notices_sent, 0)

    def test_a_revoked_row_notifies_right_after_the_readback(self) -> None:
        """撤权没有"就绪"可等：发布读回一致本身就是触发点（裁定 1）。"""

        report, parts = self._run(ReadinessOutcome.NO_PERMISSION, permissions=REVOKED)

        self.assertEqual(len(parts["notices"].calls), 1)
        self.assertEqual(parts["notices"].calls[0]["permissions"], REVOKED)
        self.assertEqual(report.revoked, 1)
        self.assertEqual(report.notices_sent, 1)

    def test_a_confirmation_that_is_not_due_makes_no_call_at_all(self) -> None:
        """否定断言：没到期就**不通知、不查收件人**。"""

        report, parts = self._run(None)

        self.assertEqual(parts["notices"].calls, [])
        self.assertEqual(parts["intents"].recipient_calls, [])
        self.assertEqual(report.advanced, 0)
        self.assertEqual(report.pending_readiness, 1)

    def test_the_progress_is_rebuilt_from_the_stored_checks(self) -> None:
        duty, parts = build_duty(
            intents=FakeIntents(FakePending(USER_ONE, 2, GRANTED)),
            ticker=FakeTicker({USER_ONE: ReadinessOutcome.WAITING}),
        )

        duty.run_once()

        self.assertEqual(parts["checks"].calls, [(USER_ONE, 2)])
        self.assertEqual(parts["ticker"].calls, [(USER_ONE, 2, GRANTED)])

    def test_the_candidate_set_only_comes_from_published_intents(self) -> None:
        """否定断言：**撤权通知只在读回一致之后发**。

        形状断言——职责能对意图存储做的事只有三件（协议里就这三个方法），取候选的唯一
        入口是 ``published_awaiting_readiness``；它没有任何"读一条 pending 意图"的路径，
        因此还没发布的撤权意图发不出通知。
        """

        from lingxi.apps.scheduler.permission_publish import _IntentStore

        methods = {
            name
            for name, value in vars(_IntentStore).items()
            if not name.startswith("_") and callable(value)
        }
        self.assertEqual(
            methods,
            {"reclaim_stale", "published_awaiting_readiness", "notice_recipient_open_id"},
        )
        source = duty_code()
        self.assertIn("published_awaiting_readiness", source)
        for forbidden in ("claim_next", "'pending'", "STATUS_PENDING"):
            self.assertNotIn(forbidden, source, f"候选集不得来自未发布的意图：{forbidden}")


class NoticeFailureTest(unittest.TestCase):
    def test_a_failed_notice_does_not_block_anything(self) -> None:
        """否定断言：通知失败**不抛异常、不改状态**，只计数。"""

        duty, parts = build_duty(
            intents=FakeIntents(FakePending(USER_ONE, 2, GRANTED)),
            ticker=FakeTicker({USER_ONE: ReadinessOutcome.READY}),
            notices=FakeNotices(delivered=False),
        )

        report = duty.run_once()

        self.assertEqual(report.ready, 1, "就绪结论不因为通知失败而改变")
        self.assertEqual(report.notices_failed, 1)
        self.assertEqual(report.notices_sent, 0)
        self.assertEqual(report.failed, 0, "通知失败不是一次用户级异常")

    def test_an_unavailable_recipient_is_counted_not_sent(self) -> None:
        """未开通 / 停用 / 删除中的账号不发通知（判据在适配器 SQL 里）。"""

        duty, parts = build_duty(
            intents=FakeIntents(FakePending(USER_ONE, 2, GRANTED), recipients={}),
            ticker=FakeTicker({USER_ONE: ReadinessOutcome.READY}),
        )

        report = duty.run_once()

        self.assertEqual(parts["notices"].calls, [])
        self.assertEqual(report.notices_skipped, 1)
        self.assertEqual(
            parts["audit"].fields_for("permission_notice.recipient_unavailable")[0]["user"],
            USER_ONE,
        )

    def test_the_readiness_record_is_written_before_the_notice(self) -> None:
        """次序断言：先落终态记录再通知。

        反过来会让一次记账失败变成"用户收到了通知、系统却认为还没处理完"，下一轮再发
        一条。这里用"通知抛异常时判定仍然发生过"来钉住次序。
        """

        duty, parts = build_duty(
            intents=FakeIntents(FakePending(USER_ONE, 2, GRANTED)),
            ticker=FakeTicker({USER_ONE: ReadinessOutcome.READY}),
            notices=FakeNotices(error=RuntimeError("注入的通知崩溃")),
        )

        report = duty.run_once()

        self.assertEqual(parts["ticker"].calls, [(USER_ONE, 2, GRANTED)], "判定已经发生")
        self.assertEqual(report.failed, 1, "通知端口自己崩溃算一次用户级失败，被隔离")
        self.assertEqual(
            parts["audit"].fields_for("permission_publish.user_failed")[0]["error"], "RuntimeError"
        )


# --------------------------------------------------------------------------
# 三、不阻塞、可隔离、可停止
# --------------------------------------------------------------------------


class NonBlockingTest(unittest.TestCase):
    def test_the_duty_has_no_waiting_port_at_all(self) -> None:
        """否定断言：**一轮 tick 不可能被就绪等待阻塞**。

        构造函数里没有 ``sleep``，源码里也不出现任何等待——S-C-02 的阻塞式确认会把整个
        SchedulerLoop 占住十五分钟，这正是 tick 形态存在的理由。
        """

        parameters = set(inspect.signature(PermissionPublishDuty.__init__).parameters)
        self.assertNotIn("sleep", parameters)
        source = duty_code()
        for forbidden in ("sleep", "import time", "wait(", "confirm(", "McpReadinessConfirmation"):
            self.assertNotIn(forbidden, source, f"发布职责不得等待：{forbidden}")

    def test_a_full_round_with_pending_confirmations_returns_immediately(self) -> None:
        """注入时钟证明：整轮里一次等待都没有发生。"""

        waits: list[float] = []
        pending = [FakePending(f"usr_{index:026d}", 2, GRANTED) for index in range(20)]
        duty, parts = build_duty(
            intents=FakeIntents(*pending, recipients={}),
            ticker=FakeTicker({item.user_id: ReadinessOutcome.WAITING for item in pending}),
        )

        original_sleep = __import__("time").sleep
        try:
            __import__("time").sleep = lambda seconds: waits.append(seconds)
            report = duty.run_once()
        finally:
            __import__("time").sleep = original_sleep

        self.assertEqual(waits, [], "整轮零 sleep")
        self.assertEqual(report.advanced, 20)

    def test_one_failing_confirmation_does_not_stop_the_round(self) -> None:
        duty, parts = build_duty(
            intents=FakeIntents(
                FakePending(USER_ONE, 2, GRANTED), FakePending(USER_TWO, 3, GRANTED)
            ),
            ticker=FakeTicker(
                {USER_ONE: RuntimeError("注入的探针崩溃"), USER_TWO: ReadinessOutcome.READY}
            ),
        )

        report = duty.run_once()

        self.assertEqual(report.failed, 1)
        self.assertEqual(report.ready, 1)
        self.assertEqual(len(parts["notices"].calls), 1)

    def test_the_duty_is_isolated_by_the_scheduler_loop(self) -> None:
        class Exploding:
            name = "会炸的职责"

            def run_once(self):
                raise RuntimeError("注入的职责异常")

        duty, parts = build_duty(
            intents=FakeIntents(FakePending(USER_ONE, 2, GRANTED)),
            ticker=FakeTicker({USER_ONE: ReadinessOutcome.READY}),
        )
        loop = SchedulerLoop(duties=(Exploding(), duty), interval_seconds=0)

        reports = loop.run_once()

        self.assertIsNone(reports[0])
        self.assertEqual(reports[1].ready, 1)

    def test_a_stopping_duty_does_nothing(self) -> None:
        """否定断言：停止之后**零发布、零探针、零通知**。"""

        stop = threading.Event()
        stop.set()
        duty, parts = build_duty(
            intents=FakeIntents(FakePending(USER_ONE, 2, GRANTED)),
            ticker=FakeTicker({USER_ONE: ReadinessOutcome.READY}),
            stop=stop,
        )

        self.assertIsNone(duty.run_once())
        self.assertEqual(parts["executor"].calls, [])
        self.assertEqual(parts["ticker"].calls, [])
        self.assertEqual(parts["notices"].calls, [])

    def test_a_stop_signal_during_the_sweep_interrupts_it(self) -> None:
        stop = threading.Event()

        class StoppingTicker(FakeTicker):
            def advance(self, binding, *, permissions, progress):
                stop.set()
                return super().advance(binding, permissions=permissions, progress=progress)

        duty, parts = build_duty(
            intents=FakeIntents(
                FakePending(USER_ONE, 2, GRANTED), FakePending(USER_TWO, 3, GRANTED)
            ),
            ticker=StoppingTicker({USER_ONE: ReadinessOutcome.WAITING}),
            stop=stop,
        )

        report = duty.run_once()

        self.assertTrue(report.interrupted)
        self.assertEqual(len(parts["ticker"].calls), 1, "停止之后不再推进后面的人")


# --------------------------------------------------------------------------
# 四、就绪面未装配
# --------------------------------------------------------------------------


class UnwiredReadinessTest(unittest.TestCase):
    def test_the_publish_face_still_runs(self) -> None:
        """否定断言：**缺就绪面时发布面照常**——发布不依赖探针。"""

        duty, parts = build_duty(
            executor=FakeExecutor(FakeAttempt(published=True)),
            intents=FakeIntents(FakePending(USER_ONE, 2, GRANTED)),
            wire_readiness=False,
        )

        report = duty.run_once()

        self.assertFalse(duty.readiness_wired)
        self.assertEqual(report.published, 1)
        self.assertFalse(report.readiness_wired)
        self.assertEqual(parts["intents"].pending_calls, [], "就绪面没装配就不取候选")
        self.assertEqual(parts["notices"].calls, [])


# --------------------------------------------------------------------------
# 五、报告与审计形状
# --------------------------------------------------------------------------


class ReportShapeTest(unittest.TestCase):
    def test_the_report_only_carries_counts(self) -> None:
        facts = PermissionPublishReport().audit_facts()

        self.assertTrue(all(isinstance(value, (int, bool)) for value in facts.values()))
        self.assertNotIn("interrupted", facts, "没中断就不写这一项")

    def test_no_audit_field_carries_a_sensitive_value(self) -> None:
        duty, parts = build_duty(
            intents=FakeIntents(FakePending(USER_ONE, 2, GRANTED)),
            ticker=FakeTicker({USER_ONE: ReadinessOutcome.READY}),
        )

        duty.run_once()

        rendered = str(parts["audit"].records)
        for secret in (EMAIL, OPEN_ID, "日活", "1011"):
            self.assertNotIn(secret, rendered)

    def test_the_completed_audit_is_written_every_round(self) -> None:
        duty, parts = build_duty()

        duty.run_once()

        self.assertEqual(len(parts["audit"].fields_for("permission_publish.completed")), 1)


# --------------------------------------------------------------------------
# 六、装配
# --------------------------------------------------------------------------


@unittest.skipUnless(
    importlib.util.find_spec("psycopg") and importlib.util.find_spec("cryptography"),
    "跳过：build_loop 会真的构造凭据保管与清理适配器，需要 psycopg 与 cryptography",
)
class DutyRegistrationTest(unittest.TestCase):
    """前置缺项 → 不注册 / 不装配、进程照常启动、审计恰 1 条、不回显值。"""

    def _config(self, **extra: str) -> SchedulerConfig:
        import tempfile

        from cryptography.fernet import Fernet

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return SchedulerConfig.from_env(
            {
                **COMPLETE_ENV,
                "LINGXI_DELEGATED_CREDENTIAL_KEY": Fernet.generate_key().decode(),
                "LINGXI_DELEGATED_CREDENTIAL_PATH": str(
                    pathlib.Path(directory.name) / "delegated.enc"
                ),
                **extra,
            }
        )

    def _wired_config(self, **extra: str) -> SchedulerConfig:
        return self._config(
            LINGXI_PERMISSION_BITABLE_APP_TOKEN="bascnFakePermissionBase",
            LINGXI_PERMISSION_BITABLE_TABLE_ID="tblFakePermissionTable",
            **extra,
        )

    def _own_records(self, audit: RecordingAudit, prefix: str):
        return [record for record in audit.records if record[0].startswith(prefix)]

    def test_without_the_base_token_the_duty_is_not_registered(self) -> None:
        audit = RecordingAudit()

        loop = build_loop(self._config(), audit=audit)

        self.assertNotIn("权限发布与就绪确认", [duty.name for duty in loop.duties])
        records = self._own_records(audit, "permission_publish.")
        self.assertEqual(len(records), 1, "前置缺项时本职责的审计恰 1 条")
        action, fields = records[0]
        self.assertEqual(action, "permission_publish.duty_not_registered")
        self.assertEqual(fields["variable"], "LINGXI_PERMISSION_BITABLE_APP_TOKEN")
        self.assertNotIn("value", fields)

    def test_without_the_access_token_supply_the_duty_is_not_registered(self) -> None:
        audit = RecordingAudit()

        loop = build_loop(self._wired_config(), audit=audit)

        self.assertNotIn("权限发布与就绪确认", [duty.name for duty in loop.duties])
        records = self._own_records(audit, "permission_publish.")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0][1]["reason"], "permission_table_access_token_unwired")

    def test_without_the_mcp_endpoint_only_the_readiness_face_is_skipped(self) -> None:
        """否定断言：**缺 MCP 端点 → 就绪面不装配 + 恰一条审计，发布面照常注册。**"""

        audit = RecordingAudit()

        loop = build_loop(
            self._wired_config(LINGXI_MCP_TOKEN_ENCRYPT_KEY=SPEC_MASTER_KEY),
            permission_table_access_token=lambda: "u-fake-token",
            audit=audit,
        )

        duties = {duty.name: duty for duty in loop.duties}
        self.assertIn("权限发布与就绪确认", duties)
        self.assertFalse(duties["权限发布与就绪确认"].readiness_wired)
        records = self._own_records(audit, "permission_readiness.")
        self.assertEqual(len(records), 1, "就绪面未装配的审计恰 1 条")
        self.assertEqual(records[0][1]["variable"], "LINGXI_QUERY_MCP_ENDPOINT")
        self.assertEqual(
            self._own_records(audit, "permission_publish."), [], "发布面照常，不留未注册审计"
        )

    def test_without_the_master_key_only_the_readiness_face_is_skipped(self) -> None:
        audit = RecordingAudit()

        loop = build_loop(
            self._wired_config(LINGXI_QUERY_MCP_ENDPOINT="https://mcp.example.invalid/rpc"),
            permission_table_access_token=lambda: "u-fake-token",
            audit=audit,
        )

        duties = {duty.name: duty for duty in loop.duties}
        self.assertIn("权限发布与就绪确认", duties)
        self.assertFalse(duties["权限发布与就绪确认"].readiness_wired)
        records = self._own_records(audit, "permission_readiness.")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0][1]["variable"], "LINGXI_MCP_TOKEN_ENCRYPT_KEY")
        self.assertNotIn("value", records[0][1], "审计里不得回显主密钥")

    def test_with_everything_wired_both_faces_are_assembled(self) -> None:
        audit = RecordingAudit()

        loop = build_loop(
            self._wired_config(
                LINGXI_MCP_TOKEN_ENCRYPT_KEY=SPEC_MASTER_KEY,
                LINGXI_QUERY_MCP_ENDPOINT="https://mcp.example.invalid/rpc",
            ),
            permission_table_access_token=lambda: "u-fake-token",
            audit=audit,
        )

        names = [duty.name for duty in loop.duties]
        duties = {duty.name: duty for duty in loop.duties}
        self.assertIn("权限发布与就绪确认", names)
        self.assertTrue(duties["权限发布与就绪确认"].readiness_wired)
        self.assertGreater(
            names.index("权限发布与就绪确认"),
            names.index("每日权限重算"),
            "发布消费排在每日重算之后：同一轮里当天的意图能立刻被推出去",
        )
        self.assertEqual(self._own_records(audit, "permission_readiness."), [])
        loop.request_stop()
        self.assertTrue(all(duty.stopping for duty in loop.duties))

    def test_the_new_variables_are_registered_in_the_environment_key_list(self) -> None:
        for variable in (
            "LINGXI_PERMISSION_BITABLE_APP_TOKEN",
            "LINGXI_PERMISSION_BITABLE_TABLE_ID",
            "LINGXI_QUERY_MCP_ENDPOINT",
            "LINGXI_QUERY_MCP_TIMEOUT_SECONDS",
        ):
            with self.subTest(variable):
                self.assertIn(variable, SchedulerConfig.ENVIRONMENT_KEYS)

    def test_a_plain_http_endpoint_fails_fast(self) -> None:
        """错配不是未配：静默降级会让用户令牌明文上路。"""

        with self.assertRaises(ValueError) as caught:
            self._config(LINGXI_QUERY_MCP_ENDPOINT="http://mcp.example.invalid/rpc")
        self.assertNotIn("mcp.example.invalid", str(caught.exception), "不回显取到的值")

    def test_a_probe_timeout_longer_than_the_interval_fails_fast(self) -> None:
        """探针超时必须 ≤ 轮询间隔，否则整轮确认的收口上界就是假的。"""

        with self.assertRaises(ValueError):
            self._config(LINGXI_QUERY_MCP_TIMEOUT_SECONDS="300")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
