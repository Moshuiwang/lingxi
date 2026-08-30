"""``adapters.postgres_permission_recompute_trigger.BackgroundPermissionRecomputeTrigger``
的纯逻辑断言（Trace #445 opus 审查坐实并修复）。

只测这一层异步执行器自己的编排：``trigger()`` 是否真的立即返回、后台线程是否
真的串行执行被包装的 delegate、失败与队列已满两种情形是否各自记对了审计——不
重新测 ``PermissionRecomputeAdapter``/``TargetedPermissionRecompute`` 的业务
判定本身（那些分别在 ``tests/test_permission_recompute_trigger_postgres.py``、
``tests/test_targeted_permission_recompute.py``）。``core/admin/card_callback.py``
的 EXECUTED-only/幂等去重判据完全不受本卡影响，钉在
``tests/test_admin_card_callback.py::RecomputeTriggerWiringTests``；本文件唯一
与 ``AdminCardCallbackHandler`` 相关的一条用例只证明"注入这一层执行器之后，
``handle()`` 的响应时间不再受 delegate 快慢影响"，不重复覆盖那个类自己的分支。
"""

from __future__ import annotations

import threading
import time
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from lingxi.adapters.postgres_permission_recompute_trigger import (
    BackgroundPermissionRecomputeTrigger,
)
from lingxi.core.admin.card_callback import AdminCardCallbackHandler
from lingxi.core.admin.notification import DECISION_CONFIRM
from lingxi.core.admin.pending_action import PendingAction, PendingActionStatus, PendingActionType

NOW = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)


def _pending(
    *,
    pending_id: str = "pac_bg_test0000000000000000",
    status: PendingActionStatus = PendingActionStatus.PENDING,
    card_id: str | None = None,
) -> PendingAction:
    return PendingAction(
        id=pending_id,
        action_type=PendingActionType.SUSPEND_USER,
        target_open_id="ou_target",
        target_state_snapshot="enabled",
        initiated_by_open_id="ou_admin",
        status=status,
        card_delivered=True,
        card_id=card_id,
        reason=None,
        created_at=NOW,
        confirm_deadline_at=NOW + timedelta(minutes=10),
        decided_at=NOW,
        decided_by_open_id="ou_admin",
    )


class _RecordingAudit:
    """append 在 CPython 里对 GIL 是原子的，供后台线程与主线程共用不加锁——与
    仓库既有的其余 ``_RecordingAudit`` 假实现同一姿态，只是这里额外说明一句：
    这份假实现被真实的后台线程调用，不是纯粹的单线程测试替身。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, dict(fields)))

    def fields_for(self, action: str) -> list[dict]:
        return [fields for name, fields in self.records if name == action]


class _SlowDelegate:
    """``trigger()`` 阻塞直到测试主动 ``release``——用于证明执行器真的做到了
    "提交即返回"，真正的执行发生在后台线程里，与调用方的时间线脱钩。"""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls: list[PendingAction] = []

    def trigger(self, pending: PendingAction) -> None:
        self.calls.append(pending)
        self.started.set()
        self.release.wait(timeout=5)


class _FailingDelegate:
    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls: list[PendingAction] = []

    def trigger(self, pending: PendingAction) -> None:
        self.calls.append(pending)
        raise self._error


def _wait_until(predicate, *, timeout: float = 2.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class TriggerReturnsImmediatelyTests(unittest.TestCase):
    def test_trigger_does_not_wait_for_the_delegate_to_finish(self) -> None:
        delegate = _SlowDelegate()
        executor = BackgroundPermissionRecomputeTrigger(delegate, audit=_RecordingAudit())
        try:
            started_at = time.monotonic()
            executor.trigger(_pending())
            elapsed = time.monotonic() - started_at

            self.assertLess(elapsed, 0.5, "trigger() 必须立即返回，不等待 delegate 执行完")
            self.assertTrue(
                delegate.started.wait(timeout=2), "后台线程应该已经取走这一条并开始执行"
            )
        finally:
            delegate.release.set()


class BackgroundFailureAuditTests(unittest.TestCase):
    """失败仍走原 ``admin.card_callback.recompute_trigger_failed`` 审计姿态
    （同步分支见 ``tests/test_admin_card_callback.py::RecomputeTriggerWiringTests.
    test_recompute_failure_does_not_propagate_and_is_audited`` 的同一动作名/
    字段形状——运维不需要区分"这次失败是同步分支还是后台执行器"）。"""

    def test_a_background_failure_is_audited_with_the_same_action_and_fields(self) -> None:
        audit = _RecordingAudit()
        delegate = _FailingDelegate(RuntimeError("模拟后台重算失败"))
        executor = BackgroundPermissionRecomputeTrigger(delegate, audit=audit)
        pending = _pending()

        executor.trigger(pending)

        found = _wait_until(
            lambda: audit.fields_for("admin.card_callback.recompute_trigger_failed")
        )
        self.assertTrue(found, "后台执行失败必须被响亮审计，不能静默吞掉")
        [fields] = audit.fields_for("admin.card_callback.recompute_trigger_failed")
        self.assertEqual(fields["pending_action_id"], pending.id)
        self.assertEqual(fields["error"], "RuntimeError")

    def test_one_failure_does_not_stop_the_worker_from_processing_the_next_item(self) -> None:
        """单条失败只记审计、不影响下一条——工作线程不能被一次异常带崩。"""

        audit = _RecordingAudit()
        delegate = _FailingDelegate(RuntimeError("boom"))
        executor = BackgroundPermissionRecomputeTrigger(delegate, audit=audit)

        executor.trigger(_pending(pending_id="pac_bg_fail_0000000000001"))
        executor.trigger(_pending(pending_id="pac_bg_fail_0000000000002"))

        self.assertTrue(_wait_until(lambda: len(delegate.calls) == 2))
        self.assertTrue(
            _wait_until(
                lambda: len(
                    audit.fields_for("admin.card_callback.recompute_trigger_failed")
                )
                == 2
            )
        )


class QueueFullDropTests(unittest.TestCase):
    """有界队列（容量 1~4）满时丢弃并记一条与"失败"不同的独立审计——这条从未
    真正被执行过，谈不上"失败"，动作名因此可分辨（模块文档「有界队列 + 丢弃」
    一节）。"""

    def test_queue_full_drops_the_newest_item_and_audits_distinctly(self) -> None:
        delegate = _SlowDelegate()
        audit = _RecordingAudit()
        executor = BackgroundPermissionRecomputeTrigger(
            delegate, audit=audit, queue_maxsize=1
        )
        first = _pending(pending_id="pac_bg_full_0000000000001")
        second = _pending(pending_id="pac_bg_full_0000000000002")
        third = _pending(pending_id="pac_bg_full_0000000000003")

        try:
            executor.trigger(first)
            self.assertTrue(
                delegate.started.wait(timeout=2), "第一条应该已经被工作线程取走并开始执行"
            )
            executor.trigger(second)  # 队列容量 1，此刻队列为空，排队成功。
            executor.trigger(third)  # 队列已满（装着 second），丢弃。

            self.assertEqual(delegate.calls, [first], "队列已满这一条从未被执行过")
            [fields] = audit.fields_for("admin.card_callback.recompute_trigger_dropped")
            self.assertEqual(fields["pending_action_id"], third.id)
            self.assertEqual(
                audit.fields_for("admin.card_callback.recompute_trigger_failed"),
                [],
                "丢弃不是失败，不能借用失败那条审计动作名",
            )
        finally:
            delegate.release.set()

    def test_queue_maxsize_out_of_bounds_is_rejected_at_construction(self) -> None:
        for bad in (0, 5, -1):
            with self.subTest(queue_maxsize=bad):
                with self.assertRaises(ValueError):
                    BackgroundPermissionRecomputeTrigger(
                        _SlowDelegate(), audit=_RecordingAudit(), queue_maxsize=bad
                    )


@dataclass(frozen=True)
class _FakeDecision:
    ok: bool
    message: str
    terminal_status: PendingActionStatus | None


@dataclass(frozen=True)
class _FakeOutcome:
    decision: _FakeDecision
    pending: PendingAction | None


class _FakeConfirm:
    """只支持 ``confirm()``——本组用例只驱动确认分支，未用到的方法保持
    ``NotImplementedError``，误用会立刻报错而不是安静地返回错误结果。"""

    def __init__(self, outcome: _FakeOutcome) -> None:
        self._outcome = outcome

    def confirm(self, *, pending_action_id: str, clicker_open_id: str) -> _FakeOutcome:
        return self._outcome

    def cancel(self, *, pending_action_id: str, clicker_open_id: str) -> _FakeOutcome:
        raise NotImplementedError

    def next_card_sequence(self, *, pending_action_id: str) -> int:
        raise NotImplementedError


class _FakeDisplayNames:
    """本文件唯一用例的 ``card_id=None`` 让 ``_update_card_to_terminal`` 提前
    返回，永远不会真的调用这三个方法——只为满足构造参数的必填性，不需要
    ``tests/test_admin_card_callback.py::FakeDisplayNames`` 那样可配置的行为
    （代码框架"各自独立声明 Protocol"惯例，测试替身同理不共享）。"""

    def user_label(self, *, open_id: str) -> str:
        raise NotImplementedError

    def company_label(self, *, company_id: str) -> str:
        raise NotImplementedError

    def metric_label(self, *, metric_id: str) -> str:
        raise NotImplementedError


class HandleDoesNotBlockOnSlowRecomputeTests(unittest.TestCase):
    """任务要求的核心断言：注入 ``BackgroundPermissionRecomputeTrigger`` 之后，
    ``AdminCardCallbackHandler.handle()`` 的响应时间不再受 delegate 快慢影响
    ——修复前 ``_trigger_recompute`` 在 ``handle()`` 的主路径里同步调用，一个
    卡在数据库调用里的重算会直接拖慢飞书卡片回调的应答（见适配器模块文档
    「``BackgroundPermissionRecomputeTrigger``」一节）。``card_id=None`` 让
    ``_update_card_to_terminal`` 提前返回，不需要另外装配 ``confirm_cards``。
    """

    def test_a_slow_background_trigger_does_not_delay_handles_response(self) -> None:
        delegate = _SlowDelegate()
        audit = _RecordingAudit()
        executor = BackgroundPermissionRecomputeTrigger(delegate, audit=audit)
        pending = _pending(status=PendingActionStatus.EXECUTED, card_id=None)
        outcome = _FakeOutcome(
            decision=_FakeDecision(
                ok=True, message="", terminal_status=PendingActionStatus.EXECUTED
            ),
            pending=pending,
        )
        handler = AdminCardCallbackHandler(
            pending_actions=_FakeConfirm(outcome),
            confirm_cards=object(),  # card_id=None，本用例从不会调用它
            group_notifier=None,
            group_chat_id=None,
            audit=audit,
            recompute_trigger=executor,
            display_names=_FakeDisplayNames(),
        )

        try:
            started_at = time.monotonic()
            response = handler.handle(
                operator_open_id="ou_admin",
                pending_action_id=pending.id,
                decision=DECISION_CONFIRM,
                trace_id="trc_bg_no_block",
            )
            elapsed = time.monotonic() - started_at

            self.assertLess(elapsed, 1.0, "handle() 不该被一个慢 trigger 阻塞住")
            self.assertEqual(response["toast"]["type"], "success")
            self.assertTrue(
                delegate.started.wait(timeout=2), "后台线程应该已经取走这一条并开始执行"
            )
        finally:
            delegate.release.set()


if __name__ == "__main__":
    unittest.main()
