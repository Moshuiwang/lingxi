"""权限发布消费与就绪确认职责的纯逻辑验收（Issue #156 / S-C-03b）。

**本文件承担 `V-权限-07` 的调度与消费面**：每日刷新排出来的发布意图真的会被消费、
发布读回一致之后真的会产生 MCP 同步确认链、以及权限变化真的会告知用户。`V-权限-08`
刷新侧的另一半（撤权行怎么结算）在 ``tests/test_permission_refresh_duty.py``。

否定断言（合同的"不得 / 不允许"必须有对应否定测试，验证与门禁第八节）：

1. **就绪没成功绝不发"范围更新"通知**（等待中、技术失败、超时都不发）；
2. **撤权通知只在发布读回一致之后发**——候选集只来自已发布的意图；
3. 通知失败**不阻塞权限生效、不改变发布或就绪状态、不抛异常**；收件人查询失败则
   **本轮不推进、终态不落**（否则一次瞬时异常就把这条通知永久吞掉）；
4. **一轮 tick 不被就绪等待阻塞**：整轮零 ``sleep``，职责也没有等待端口；单轮另有
   **停止钩子与时间预算**，发布面也逐条检查（`V-部署-03`「停止领取新工作」）；
5. **三个面各按自身依赖装配，缺谁只停谁**：缺 MCP 端点只停探针（撤权通知照常，且
   候选查询随之只取撤权那一类，**不让积压的授权候选饿死撤权通知**）；缺主密钥停整个
   通知面；缺权限表坐标/令牌只停发布面。每一面**恰一条**审计；
6. 收件人不可用（未开通 / 停用 / 删除中）**不发通知**；
7. 停止信号之后**零发布、零探针、零通知**；
8. 单个用户失败不带走整轮；
9. 报告与审计**只有计数**，不含邮箱、姓名、权限值与 open_id；整面未装配时
   ``probe_wired`` **也是 False**，不谎报探针没问题。
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import pathlib
import threading
import unittest
from datetime import UTC, datetime, timedelta

from lingxi.apps.scheduler import (
    PermissionPublishDuty,
    PermissionPublishReport,
    ReadinessFollowUp,
    SchedulerConfig,
    SchedulerLoop,
    build_loop,
)
from lingxi.apps.scheduler.permission_publish import (
    DEFAULT_READINESS_LIMIT,
    FOLLOW_UP_REASONS,
    REVOKE_ONLY_REASONS,
)
from lingxi.apps.scheduler.permission_refresh import (
    PERMISSION_REFRESH_REASON,
    PERMISSION_REVOKE_REASON,
)
from lingxi.core.permission.mcp_readiness import (
    CONTRACT_SCHEDULE,
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


NOW = datetime(2026, 8, 18, 3, 0, tzinfo=UTC)
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
    """发布执行器返回的一次尝试的最小形状。

    ``outbox_id`` 是 F2 之后新增的最小字段：职责要靠它累积"本轮已经认领过谁"。
    """

    def __init__(self, *, published: bool, outbox_id: str = "pob_1") -> None:
        self.published = published
        self.outbox_id = outbox_id


class FakeExecutor:
    """逐条认领的假执行器：每次 ``run_once`` 交出脚本里的下一条，空了就返回空。"""

    def __init__(self, *attempts: FakeAttempt, on_call=None) -> None:
        self._queue = list(attempts)
        self.calls: list[int] = []
        #: 每次调用收到的本轮排除清单，供 F2 的断言比对。
        self.excludes: list[tuple[str, ...]] = []
        self._on_call = on_call

    def run_once(self, *, limit: int = 50, exclude=()):
        self.calls.append(limit)
        self.excludes.append(tuple(exclude))
        if self._on_call is not None:
            self._on_call()
        if not self._queue:
            return ()
        return (self._queue.pop(0),)


class FakePending:
    def __init__(
        self,
        user_id: str,
        permission_version: int,
        permissions: str,
        *,
        reason: str = PERMISSION_REFRESH_REASON,
    ) -> None:
        self.user_id = user_id
        self.permission_version = permission_version
        self.permissions = permissions
        # 真实查询按 reason 过滤并 LIMIT；假实现照做，否则"窗口被占死"这类缺陷
        # 在用例里根本发生不了。
        self.reason = reason


class FakeIntents:
    def __init__(
        self,
        *pending: FakePending,
        recipients: dict[str, str] | None = None,
        reclaimed: int = 0,
        pending_error: Exception | None = None,
        recipient_error: Exception | None = None,
    ) -> None:
        self._pending = pending
        self._pending_error = pending_error
        self._recipient_error = recipient_error
        self._recipients = (
            {USER_ONE: OPEN_ID, USER_TWO: OPEN_ID} if recipients is None else recipients
        )
        self._reclaimed = reclaimed
        self.reclaim_calls = 0
        self.pending_calls: list[int] = []
        self.schedule_calls: list[tuple[int, int]] = []
        self.reason_calls: list[tuple[str, ...]] = []
        self.recipient_calls: list[str] = []

    def reclaim_stale(self, *, older_than: timedelta = timedelta(minutes=15)) -> int:
        self.reclaim_calls += 1
        return self._reclaimed

    def published_awaiting_readiness(
        self, *, reasons, interval_seconds: int, budget_seconds: int, limit: int = 50
    ):
        self.pending_calls.append(limit)
        self.reason_calls.append(tuple(reasons))
        self.schedule_calls.append((interval_seconds, budget_seconds))
        if self._pending_error is not None:
            raise self._pending_error
        wanted = tuple(reasons)
        matched = [item for item in self._pending if item.reason in wanted]
        return tuple(matched[:limit])

    def notice_recipient_open_id(self, user_id: str) -> str | None:
        self.recipient_calls.append(user_id)
        if self._recipient_error is not None:
            raise self._recipient_error
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

    def __init__(
        self, script: dict[str, object] | None = None, *, probe_wired: bool = True
    ) -> None:
        self._script = script or {}
        self.calls: list[tuple[str, int, str]] = []
        self.schedule = CONTRACT_SCHEDULE
        self.probe_wired = probe_wired

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


_UNSET = object()


def build_duty(
    *,
    executor=_UNSET,
    intents: FakeIntents | None = None,
    ticker: FakeTicker | None = None,
    checks: FakeChecks | None = None,
    notices: FakeNotices | None = None,
    audit: RecordingAudit | None = None,
    stop: threading.Event | None = None,
    wire_readiness: bool = True,
    on_alert=None,
    round_budget_seconds: float = 60.0,
    readiness_limit: int = DEFAULT_READINESS_LIMIT,
    clock=None,
):
    executor = FakeExecutor() if executor is _UNSET else executor
    intents = intents or FakeIntents()
    ticker = ticker or FakeTicker()
    checks = checks or FakeChecks()
    notices = notices or FakeNotices()
    audit = audit or RecordingAudit()
    readiness = (
        ReadinessFollowUp(ticker=ticker, checks=checks, notices=notices) if wire_readiness else None
    )
    duty = PermissionPublishDuty(
        executor=executor,
        intents=intents,
        audit=audit,
        readiness=readiness,
        on_alert=on_alert,
        readiness_limit=readiness_limit,
        round_budget_seconds=round_budget_seconds,
        clock=clock,
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
        # **逐条认领**（N4）：每次只领一条，好在每条之前重新看一眼停止标志与时间预算。
        self.assertEqual(parts["executor"].calls, [1, 1, 1])
        self.assertEqual(report.attempts, 2)
        self.assertEqual(report.published, 1)
        self.assertEqual(report.reclaimed, 2)

    def test_the_round_carries_the_claimed_ledger_across_the_one_by_one_calls(self) -> None:
        """**一轮的边界在职责这一层**（Epic C 冻结缺陷 F2）。

        发布面是 ``run_once(limit=1)`` × N 的形状，每次调用都是全新一次——执行器自己
        那个本轮集合每次只装得下一个元素，等于没有。因此累积的已认领清单必须由职责
        传下去，"一条意图一轮最多认领一次"才在**生产形态**下成立。

        这条断言会在有人把 ``exclude=tuple(claimed)`` 改回 ``run_once(limit=1)`` 时变红。
        """

        duty, parts = build_duty(
            executor=FakeExecutor(
                FakeAttempt(published=False, outbox_id="pob_1"),
                FakeAttempt(published=False, outbox_id="pob_2"),
            ),
        )

        duty.run_once()

        self.assertEqual(
            parts["executor"].excludes,
            [(), ("pob_1",), ("pob_1", "pob_2")],
            "本轮已认领的清单必须逐条累积着传下去",
        )

    def test_the_round_does_not_swallow_an_executor_failure(self) -> None:
        """**发布面不得把执行器异常吞掉**（Epic C 冻结重验的存活变异 S）。

        F2 的「一轮最多认领一次」靠的是本轮已认领清单一路传下去。今天它成立还额外
        依赖一个事实：执行器抛异常时整轮当场结束，没有"跳过这条、继续认领下一条"
        的分支——所以清单在异常路径上有没有记全并不影响结果。

        风险在于**紧挨着的就绪面正是 ``try/except`` 逐条隔离的形状**，而本模块自己的
        文档也写着"一个人的失败不得带走整轮"。哪天有人照着把发布面也改成吞掉异常
        继续下一条，那条刚失败、清单里又没记上的意图就会在同一轮里被立刻重认领——
        F2 静默复活，而在此之前没有任何用例会变红。

        这条断言把"异常照常冒出去"钉成契约：要改成逐条隔离，就必须同时想清楚清单
        在异常路径上怎么记，而不是悄悄退回原缺陷。
        """

        boom = RuntimeError("外部发布失败")

        def explode() -> None:
            raise boom

        duty, parts = build_duty(
            executor=FakeExecutor(
                FakeAttempt(published=False, outbox_id="pob_1"),
                on_call=explode,
            ),
        )

        with self.assertRaises(RuntimeError) as caught:
            duty.run_once()

        self.assertIs(caught.exception, boom, "执行器的异常必须原样冒出，不被折算或吞掉")
        self.assertEqual(
            parts["executor"].calls,
            [1],
            "整轮当场结束：不得在异常之后继续认领下一条",
        )

    def test_the_claimed_ledger_does_not_leak_into_the_next_round(self) -> None:
        """否定断言：清单**每轮新建**。跨轮持有会让一条意图在这个进程里再也轮不到，
        那比原缺陷更糟——重试将永远不会发生。"""

        duty, parts = build_duty(
            executor=FakeExecutor(
                FakeAttempt(published=False, outbox_id="pob_1"),
                FakeAttempt(published=False, outbox_id="pob_1"),
            ),
        )

        duty.run_once()
        parts["executor"].excludes.clear()
        duty.run_once()

        self.assertEqual(parts["executor"].excludes[0], (), "新一轮从空清单开始")

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
                        executor=FakeExecutor(),
                        intents=FakeIntents(),
                        audit=RecordingAudit(),
                        **kwargs,
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
        self.assertEqual(report.advanced, 0)
        self.assertEqual(report.pending_readiness, 1)
        # 收件人查询在推进**之前**（N2），因此它照常发生；真正的否定面是"零通知"。
        self.assertEqual(parts["intents"].recipient_calls, [USER_ONE])

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


class NoticeReliabilityTest(unittest.TestCase):
    """N2：收件人先查再推进；发送前的每条异常路径都留 ``permission_notice.*`` 痕迹。"""

    def test_a_recipient_lookup_failure_does_not_burn_the_confirmation(self) -> None:
        """否定断言：收件人查询失败 → **本轮不推进、终态不落**，下一轮原样重来。

        反过来（先落终态再查收件人）会让一次瞬时数据库异常把这条确认永久收口：候选集
        从此排除它，用户永远收不到通知，留下的只有一条泛化的 user_failed。
        """

        duty, parts = build_duty(
            intents=FakeIntents(
                FakePending(USER_ONE, 2, GRANTED), recipient_error=RuntimeError("注入的库抖动")
            ),
            ticker=FakeTicker({USER_ONE: ReadinessOutcome.READY}),
        )

        report = duty.run_once()

        self.assertEqual(parts["ticker"].calls, [], "推进没有发生")
        self.assertEqual(parts["checks"].calls, [], "连进度都不用读")
        self.assertEqual(report.advanced, 0)
        self.assertEqual(report.failed, 1)

    def test_the_recipient_is_looked_up_before_the_probe(self) -> None:
        order: list[str] = []

        class OrderedIntents(FakeIntents):
            def notice_recipient_open_id(self, user_id: str) -> str | None:
                order.append("recipient")
                return super().notice_recipient_open_id(user_id)

        class OrderedTicker(FakeTicker):
            def advance(self, binding, *, permissions, progress):
                order.append("advance")
                return super().advance(binding, permissions=permissions, progress=progress)

        duty, _ = build_duty(
            intents=OrderedIntents(FakePending(USER_ONE, 2, GRANTED)),
            ticker=OrderedTicker({USER_ONE: ReadinessOutcome.READY}),
        )

        duty.run_once()

        self.assertEqual(order, ["recipient", "advance"])

    def test_a_notice_port_crash_leaves_a_notice_level_trace(self) -> None:
        """否定断言：发送前崩溃**不得只留一条泛化的 user_failed**。"""

        duty, parts = build_duty(
            intents=FakeIntents(FakePending(USER_ONE, 2, GRANTED)),
            ticker=FakeTicker({USER_ONE: ReadinessOutcome.READY}),
            notices=FakeNotices(error=RuntimeError("注入的渲染崩溃")),
        )

        report = duty.run_once()

        failed = parts["audit"].fields_for("permission_notice.failed")
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["error_code"], "RuntimeError")
        self.assertEqual(failed[0]["stage"], "render_or_dispatch")
        self.assertEqual(report.notices_failed, 1)
        self.assertEqual(report.failed, 1, "同时仍是一次响亮的用户级失败")


class DueOrderingTest(unittest.TestCase):
    """N3：候选窗口只装"这一轮真的该动"的，且新发布优先。"""

    def test_the_sweep_hands_the_schedule_to_the_query(self) -> None:
        duty, parts = build_duty()

        duty.run_once()

        self.assertEqual(
            parts["intents"].schedule_calls,
            [(CONTRACT_SCHEDULE.interval_seconds, CONTRACT_SCHEDULE.budget_seconds)],
            "到期判据必须与就绪节奏同源，否则窗口会被没到期的候选占满",
        )

    def test_a_backlog_does_not_starve_the_round(self) -> None:
        """积压场景：窗口里全是到期项时照样逐条推进，不因为条数多就跳过。"""

        pending = [FakePending(f"usr_{index:026d}", 2, GRANTED) for index in range(50)]
        duty, parts = build_duty(
            intents=FakeIntents(*pending, recipients={}),
            ticker=FakeTicker({item.user_id: ReadinessOutcome.WAITING for item in pending}),
        )

        report = duty.run_once()

        self.assertEqual(report.advanced, 50)
        self.assertEqual(len(parts["ticker"].calls), 50)


class ReadinessAlertTest(unittest.TestCase):
    """N5：刷新链的超时至少产生一条可告警事实。"""

    def test_a_timeout_reports_an_alertable_fact(self) -> None:
        alerts: list[tuple[str, str]] = []
        duty, _ = build_duty(
            intents=FakeIntents(FakePending(USER_ONE, 2, GRANTED)),
            ticker=FakeTicker({USER_ONE: ReadinessOutcome.TIMED_OUT}),
            on_alert=lambda kind, user: alerts.append((kind, user)),
        )

        duty.run_once()

        self.assertEqual(alerts, [("permission_readiness_timed_out", USER_ONE)])

    def test_a_ready_confirmation_reports_nothing(self) -> None:
        alerts: list[tuple[str, str]] = []
        duty, _ = build_duty(
            intents=FakeIntents(FakePending(USER_ONE, 2, GRANTED)),
            ticker=FakeTicker({USER_ONE: ReadinessOutcome.READY}),
            on_alert=lambda kind, user: alerts.append((kind, user)),
        )

        duty.run_once()

        self.assertEqual(alerts, [])

    def test_an_exploding_alert_callback_does_not_change_the_result(self) -> None:
        def explode(kind: str, user: str) -> None:
            raise RuntimeError("注入的告警回调崩溃")

        duty, _ = build_duty(
            intents=FakeIntents(FakePending(USER_ONE, 2, GRANTED)),
            ticker=FakeTicker({USER_ONE: ReadinessOutcome.TIMED_OUT}),
            on_alert=explode,
        )

        report = duty.run_once()

        self.assertEqual(report.timed_out, 1)
        self.assertEqual(report.failed, 0, "观察者不是这条链的一部分")


class UnwiredProbeTest(unittest.TestCase):
    """N6：探针未接线时，撤权通知照常、报告里看得出来。"""

    def test_the_report_says_the_probe_is_not_wired(self) -> None:
        duty, _ = build_duty(ticker=FakeTicker(probe_wired=False))

        report = duty.run_once()

        self.assertFalse(report.probe_wired)
        self.assertTrue(report.readiness_wired)

    def test_a_backlog_of_grants_does_not_starve_the_revocation_notice(self) -> None:
        """否定断言（定向终核 Q1）：**探针未接线时，积压的授权候选不得饿死撤权通知**。

        授权候选在 ``probe=None`` 下每轮都只得到 ``None``——不落记录，于是永远保持
        "还没探过"这个最高优先级。若它们仍参与查询，只要积压条数 ≥ 单轮就绪预算，
        后发布的撤权行就再也进不了窗口，而每轮都在重复取回同一批毫无进展的候选。
        """

        backlog = [FakePending(f"usr_{index:026d}", 2, GRANTED) for index in range(5)]
        revoked = FakePending(USER_TWO, 3, REVOKED, reason=PERMISSION_REVOKE_REASON)
        duty, parts = build_duty(
            intents=FakeIntents(*backlog, revoked),
            ticker=FakeTicker({USER_TWO: ReadinessOutcome.NO_PERMISSION}, probe_wired=False),
            readiness_limit=2,
        )

        report = duty.run_once()

        self.assertEqual(
            parts["intents"].reason_calls, [REVOKE_ONLY_REASONS], "只认领不依赖探针的那一类"
        )
        self.assertEqual([call["user_id"] for call in parts["notices"].calls], [USER_TWO])
        self.assertEqual(report.revoked, 1)
        self.assertEqual(report.notices_sent, 1)

    def test_with_a_probe_both_reasons_are_claimed(self) -> None:
        """反面：探针接线之后，授权候选照常回到窗口里。"""

        duty, parts = build_duty(
            intents=FakeIntents(FakePending(USER_ONE, 2, GRANTED)),
            ticker=FakeTicker({USER_ONE: ReadinessOutcome.READY}),
        )

        report = duty.run_once()

        self.assertEqual(parts["intents"].reason_calls, [FOLLOW_UP_REASONS])
        self.assertEqual(report.ready, 1)

    def test_an_unwired_follow_up_face_does_not_claim_the_probe_is_fine(self) -> None:
        """否定断言（定向终核 Q2）：整面没装配时 ``probe_wired`` **也是 False**。

        给出 ``readiness_wired=False, probe_wired=True`` 会让读报告的人以为探针是好的、
        只是别处出了问题。
        """

        duty, _ = build_duty(wire_readiness=False)

        report = duty.run_once()

        self.assertFalse(report.readiness_wired)
        self.assertFalse(report.probe_wired)

    def test_the_publish_face_can_be_absent(self) -> None:
        duty, parts = build_duty(
            executor=None,
            intents=FakeIntents(FakePending(USER_ONE, 2, REVOKED)),
            ticker=FakeTicker({USER_ONE: ReadinessOutcome.NO_PERMISSION}),
        )

        report = duty.run_once()

        self.assertFalse(duty.publish_wired)
        self.assertFalse(report.publish_wired)
        self.assertEqual(report.attempts, 0)
        self.assertEqual(report.revoked, 1, "已发布的撤权照常确认与通知")
        self.assertEqual(len(parts["notices"].calls), 1)

    def test_a_duty_with_neither_face_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            PermissionPublishDuty(intents=FakeIntents(), audit=RecordingAudit())


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

    def test_a_stop_signal_inside_the_publish_loop_claims_no_more(self) -> None:
        """否定断言（N4）：**停止信号落在发布循环中间就不再认领新意图**。

        `V-部署-03`「停止领取新工作」——重算面与就绪面都是逐条检查的，发布面漏掉这一条
        会让 SIGTERM 之后整批新意图继续被认领走。
        """

        stop = threading.Event()
        executor = FakeExecutor(*(FakeAttempt(published=True) for _ in range(5)), on_call=stop.set)
        duty, parts = build_duty(executor=executor, stop=stop)

        report = duty.run_once()

        self.assertEqual(len(executor.calls), 1, "第一条之后就不再领了")
        self.assertEqual(report.attempts, 1)
        self.assertTrue(report.interrupted)
        self.assertEqual(parts["intents"].pending_calls, [], "中断后不再开就绪面")

    def test_a_round_that_runs_out_of_time_stops_and_resumes_next_round(self) -> None:
        """否定断言（N4）：**单轮有时间上界**，外部劣化时本轮止步、下轮继续。

        条数预算挡不住时间：一条发布要写外部表 + 逐字段读回，把 50 条刷满可以到几十
        分钟，而活性心跳每轮才跳一次（默认阈值 180 秒）。
        """

        moment = [NOW]

        def clock() -> datetime:
            return moment[0]

        def spend_a_minute() -> None:
            moment[0] = moment[0] + timedelta(seconds=31)

        executor = FakeExecutor(
            *(FakeAttempt(published=True) for _ in range(5)), on_call=spend_a_minute
        )
        duty, parts = build_duty(
            executor=executor,
            intents=FakeIntents(FakePending(USER_ONE, 2, GRANTED)),
            ticker=FakeTicker({USER_ONE: ReadinessOutcome.READY}),
            clock=clock,
            round_budget_seconds=60.0,
        )

        report = duty.run_once()

        self.assertEqual(report.attempts, 2, "两条就用掉 62 秒，本轮止步")
        self.assertTrue(report.interrupted)
        self.assertEqual(parts["ticker"].calls, [], "本轮不再开就绪面")

    def test_an_illegal_round_budget_is_rejected(self) -> None:
        for value in (0, -1, True):
            with self.subTest(value):
                with self.assertRaises(ValueError):
                    PermissionPublishDuty(
                        executor=FakeExecutor(),
                        intents=FakeIntents(),
                        audit=RecordingAudit(),
                        round_budget_seconds=value,  # type: ignore[arg-type]
                    )

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

    def test_without_the_base_token_the_publish_face_names_the_variable(self) -> None:
        audit = RecordingAudit()

        build_loop(self._config(), audit=audit)

        records = self._own_records(audit, "permission_publish.")
        self.assertEqual(len(records), 1, "前置缺项时本职责的审计恰 1 条")
        _action, fields = records[0]
        self.assertEqual(fields["variable"], "LINGXI_PERMISSION_BITABLE_APP_TOKEN")
        self.assertNotIn("value", fields)

    def test_without_an_explicit_supply_build_loop_wires_the_publish_face_by_default(
        self,
    ) -> None:
        """产品负责人 2026-08-18 就 #226 裁定方向 3（应用身份）之后，``build_loop``
        不再需要调用方交出令牌供给——``permission_table_access_token`` 未显式传入时，
        它自己建一条应用身份供给（``app_id``/``app_secret`` 是 scheduler 本来就必需的
        配置），发布面因此**默认真实注册**。与花名册那条（#215）同一条口径。

        **装配阶段不得发起任何真实请求**：只验证 ``duty.publish_wired``，不调用
        供给本身——真实令牌获取属 L4a，见 ``tests/test_feishu_tenant_token.py``。
        """

        audit = RecordingAudit()

        loop = build_loop(
            self._wired_config(
                LINGXI_MCP_TOKEN_ENCRYPT_KEY=SPEC_MASTER_KEY,
                LINGXI_QUERY_MCP_ENDPOINT="https://mcp.example.invalid/rpc",
            ),
            audit=audit,
        )

        duties = {duty.name: duty for duty in loop.duties}
        self.assertIn("权限发布与就绪确认", duties)
        self.assertTrue(duties["权限发布与就绪确认"].publish_wired)
        self.assertEqual(
            self._own_records(audit, "permission_publish."),
            [],
            "配置齐全时默认供给应当让发布面真实注册，不留『未装配』审计",
        )

    def test_a_missing_supply_is_still_distinguishable_from_a_failing_one_at_the_builder_level(
        self,
    ) -> None:
        """``_build_permission_publish_duty`` 直接调用（不经 ``build_loop`` 的默认构造）
        时，「没有供给」与「有供给但拿不到令牌」这两种状态必须仍然可分辨——形状照
        ``tests/test_roster_audit_duty.py`` 的
        ``test_a_missing_token_supply_is_distinguishable_from_a_failing_one``。
        ``build_loop`` 现在总会交出一条默认供给，但这条区分本身是
        ``_build_permission_publish_duty`` 自己的合同，不因调用方总是传非 ``None``
        而失去意义——直接构造它的调用方（例如未来的另一个入口）仍然可能传 ``None``。
        """

        from lingxi.apps.scheduler import _build_permission_publish_duty
        from lingxi.core.permission.table_access_token_supply import (
            PermissionTableAccessTokenProvider,
            PermissionTableAccessTokenUnavailable,
        )

        config = self._wired_config()

        missing_audit = RecordingAudit()
        missing = _build_permission_publish_duty(
            config, stop=threading.Event(), audit=missing_audit, permission_table_access_token=None
        )
        self.assertEqual(
            self._own_records(missing_audit, "permission_publish.")[0][1]["reason"],
            "permission_table_access_token_unwired",
        )
        # 就绪面没配 MCP 端点，因此两面都装不起来时不注册；这里只关心发布面那条原因码
        # 是否仍然可达，不关心整体是否注册。
        del missing

        def always_failing() -> str:
            raise PermissionTableAccessTokenUnavailable("fetch_unavailable")

        failing_audit = RecordingAudit()
        registered = _build_permission_publish_duty(
            config,
            stop=threading.Event(),
            audit=failing_audit,
            permission_table_access_token=PermissionTableAccessTokenProvider(fetch=always_failing),
        )

        self.assertIsNotNone(registered, "供给存在但会失败时，发布面照常注册")
        self.assertTrue(registered.publish_wired)
        self.assertEqual(
            self._own_records(failing_audit, "permission_publish."),
            [],
            "运行期失败不产生装配阶段的『未装配』审计——那条只在真的调用供给时才出现",
        )

    def test_without_the_mcp_endpoint_only_the_probe_is_skipped(self) -> None:
        """否定断言（N6）：**缺 MCP 端点只关掉探针**——撤权通知与发布都照常。

        撤权通知不依赖探针；把整面关掉会让一个权限刚被收回的人永远收不到告知。
        """

        audit = RecordingAudit()

        loop = build_loop(
            self._wired_config(LINGXI_MCP_TOKEN_ENCRYPT_KEY=SPEC_MASTER_KEY),
            permission_table_access_token=lambda: "u-fake-token",
            audit=audit,
        )

        duties = {duty.name: duty for duty in loop.duties}
        duty = duties["权限发布与就绪确认"]
        self.assertTrue(duty.publish_wired)
        self.assertTrue(duty.readiness_wired, "通知面照常装配")
        records = self._own_records(audit, "permission_readiness.")
        self.assertEqual(len(records), 1, "探针未装配的审计恰 1 条")
        self.assertEqual(records[0][0], "permission_readiness.probe_not_wired")
        self.assertEqual(records[0][1]["variable"], "LINGXI_QUERY_MCP_ENDPOINT")
        self.assertEqual(
            self._own_records(audit, "permission_publish."), [], "发布面照常，不留未注册审计"
        )

    def test_without_the_bitable_token_the_readiness_face_still_runs(self) -> None:
        """否定断言（N6）：**缺发布面前置时，已发布权限的确认与通知照常**。

        没有理由因为"暂时写不了新的一行"就把已经发布出去、正等着确认的那些一起停掉。
        """

        audit = RecordingAudit()

        loop = build_loop(
            self._config(
                LINGXI_MCP_TOKEN_ENCRYPT_KEY=SPEC_MASTER_KEY,
                LINGXI_QUERY_MCP_ENDPOINT="https://mcp.example.invalid/rpc",
            ),
            audit=audit,
        )

        duties = {duty.name: duty for duty in loop.duties}
        self.assertIn("权限发布与就绪确认", duties)
        duty = duties["权限发布与就绪确认"]
        self.assertFalse(duty.publish_wired)
        self.assertTrue(duty.readiness_wired)
        records = self._own_records(audit, "permission_publish.")
        self.assertEqual(len(records), 1, "发布面未装配的审计恰 1 条")
        self.assertEqual(records[0][0], "permission_publish.publish_not_wired")

    def test_with_neither_face_the_duty_is_not_registered(self) -> None:
        audit = RecordingAudit()

        loop = build_loop(self._config(), audit=audit)

        self.assertNotIn("权限发布与就绪确认", [duty.name for duty in loop.duties])
        records = self._own_records(audit, "permission_publish.")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0][0], "permission_publish.duty_not_registered")

    def test_without_the_master_key_the_whole_follow_up_face_is_skipped(self) -> None:
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

    def test_the_probe_is_wired_with_the_verified_content_text_reader(self) -> None:
        """Issue #253：装配层必须显式注入已验证的
        ``content_text_metrics_reader``，而不是让 ``QueryMcpProbe`` 落回默认值——
        默认的 ``default_metrics_reader`` 在真实问数 MCP 上永远技术失败（见
        ``docs/参考证据/问数MCP-list_metrics真实响应形状.md``）。
        """

        from lingxi.adapters.query_mcp_probe import (
            content_text_metrics_reader,
            default_metrics_reader,
        )

        loop = build_loop(
            self._wired_config(
                LINGXI_MCP_TOKEN_ENCRYPT_KEY=SPEC_MASTER_KEY,
                LINGXI_QUERY_MCP_ENDPOINT="https://mcp.example.invalid/rpc",
            ),
            permission_table_access_token=lambda: "u-fake-token",
            audit=RecordingAudit(),
        )

        duty = {duty.name: duty for duty in loop.duties}["权限发布与就绪确认"]
        probe = duty._readiness.ticker._probe
        self.assertIs(probe.metrics_reader, content_text_metrics_reader)
        self.assertIsNot(probe.metrics_reader, default_metrics_reader)

    def test_a_direction_agnostic_provider_fits_the_same_injection_point(self) -> None:
        """Issue #226 前置：无论最终方向是哪一个，令牌供给的形状都是
        ``Callable[[], str]``——``PermissionTableAccessTokenProvider``
        （:mod:`lingxi.core.permission.table_access_token_supply`）作为这个形状的
        方向无关外壳，必须能原样替换掉裸 lambda，装配结果不变。"""

        from lingxi.core.permission.table_access_token_supply import (
            PermissionTableAccessTokenProvider,
        )

        audit = RecordingAudit()
        supply = PermissionTableAccessTokenProvider(fetch=lambda: "u-fake-token")

        loop = build_loop(
            self._wired_config(
                LINGXI_MCP_TOKEN_ENCRYPT_KEY=SPEC_MASTER_KEY,
                LINGXI_QUERY_MCP_ENDPOINT="https://mcp.example.invalid/rpc",
            ),
            permission_table_access_token=supply,
            audit=audit,
        )

        duties = {duty.name: duty for duty in loop.duties}
        self.assertIn("权限发布与就绪确认", duties)
        self.assertTrue(duties["权限发布与就绪确认"].publish_wired)
        self.assertTrue(duties["权限发布与就绪确认"].readiness_wired)
        self.assertEqual(
            self._own_records(audit, "permission_publish."),
            [],
            "配了供给之后发布面不该再留任何未装配审计",
        )

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
