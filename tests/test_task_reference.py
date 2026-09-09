"""#657 九正三负、隔离回合、安全号码和旧目录兼容的可执行证据。"""

from __future__ import annotations

import asyncio
import io
import json
import unittest
from dataclasses import replace
from unittest.mock import Mock

from test_worker_queue_consumer import FakeWorkerQueue, worker_config

from lingxi.apps import trace
from lingxi.apps.worker.cli import _log
from lingxi.apps.worker.service import WorkerService
from lingxi.apps.worker.service_ports import WorkerObservers
from lingxi.apps.worker.task_processing import failure_content
from lingxi.apps.worker.turn import WorkerTurnExecutor
from lingxi.config.content import ContentCatalog, ContentRenderError, default_content_catalog
from lingxi.core.task_reference import (
    FAILURE_REFERENCE_KEYS,
    TaskReference,
    append_failure_reference,
    parse_reference,
    reference_fields,
    render_reference,
    task_reference,
)

A = "01J00000000000000000000001"
B = "01J00000000000000000000002"
TASK = "tsk_" + B
BAD = (
    "T-",
    "T-T-" + A,
    "tsk_" + A,
    "T-" + A + "x",
    "张三",
    "a@example.com",
    "'; SELECT 1--",
    "$(whoami)",
    "trace_id=" + A,
    "8" * 26,
)


class ReferenceTests(unittest.TestCase):
    def test_closed_shapes_and_stable_fallback(self):
        self.assertEqual(parse_reference(A), TaskReference(A, "event"))
        self.assertEqual(task_reference(TASK, A), TaskReference(A, "event"))
        for value in (None, *BAD):
            self.assertEqual(task_reference(TASK, value), TaskReference("T-" + B, "task"))
        self.assertNotEqual(task_reference(TASK), task_reference("tsk_" + A))
        for value in BAD:
            self.assertIsNone(parse_reference(value))

    def test_bad_task_id_is_integrity_failure_without_echo_or_random_number(self):
        fields = reference_fields("姓名邮箱@example.com", "非法来源")
        self.assertTrue(fields["reference_integrity_error"])
        self.assertIsNone(fields["task_id"])
        self.assertIsNone(fields["trace_id"])
        self.assertIsNone(fields["reference"])
        catalog = default_content_catalog()
        old = catalog.text("worker.failed")
        self.assertEqual(append_failure_reference(catalog, old, task_id="坏值"), old)

    def test_nine_positive_three_negative_and_no_context(self):
        catalog = default_content_catalog()
        self.assertEqual(len(FAILURE_REFERENCE_KEYS), 9)
        for key in FAILURE_REFERENCE_KEYS:
            for source, expected in ((A, A), (None, "T-" + B), ("bad", "T-" + B)):
                result = append_failure_reference(
                    catalog, catalog.text(key), task_id=TASK, trace_id=source
                )
                self.assertIn(expected, result.text)
                self.assertEqual(result.key, key)
                self.assertEqual(result.version, catalog.version)
        for key in ("worker.stopped", "worker.stopped_result", "worker.context_too_long"):
            args = {"result": "已完成部分"} if key.endswith("stopped_result") else {}
            old = catalog.text(key, **args)
            self.assertEqual(append_failure_reference(catalog, old, task_id=TASK, trace_id=A), old)
        self.assertEqual(
            failure_content(catalog, "turn_timeout")[1], catalog.text("worker.running_timeout")
        )
        self.assertIn(A, failure_content(catalog, "turn_timeout", task_id=TASK, trace_id=A)[1].text)

    def test_render_rejects_all_wrong_types_before_formatting(self):
        catalog = default_content_catalog()
        for kind in ("event", "task", "unknown"):
            for value in BAD:
                with self.assertRaises(ContentRenderError):
                    render_reference(catalog, TaskReference(value, kind))
        for kind, value in (("task", A), ("event", "T-" + A), ("unknown", A)):
            with self.assertRaises(ContentRenderError):
                render_reference(catalog, TaskReference(value, kind))
        for key in ("worker.failure_reference", "worker.failure_task_reference"):
            with self.assertRaises(ContentRenderError):
                catalog.text(key, reference="a@example.com")

    def test_invalid_cli_does_not_connect_and_does_not_echo(self):
        connect = Mock(side_effect=AssertionError("非法号码不得建连"))
        for value in BAD:
            err = io.StringIO()
            self.assertEqual(
                trace.run(
                    [value], env={trace.DSN_ENV_VAR: "synthetic"}, connect=connect, stderr=err
                ),
                1,
            )
            self.assertNotIn(value, err.getvalue())
        connect.assert_not_called()

    def test_old_override_and_invalid_new_override(self):
        catalog = default_content_catalog()
        overridden = catalog.with_text_overrides({"worker.failed": "系统暂时未能完成。"})
        rendered = append_failure_reference(
            overridden, overridden.text("worker.failed"), task_id=TASK
        )
        self.assertEqual(rendered.text, "系统暂时未能完成。\n任务参考号：T-" + B + "。")
        for value in ("缺少号码", "追溯号：{trace_id}。", "mcp__{reference}"):
            with self.assertRaises(ValueError):
                catalog.with_text_overrides({"worker.failure_reference": value})
        import tomllib

        from lingxi.config.content import CONTENT_PATH

        doc = tomllib.loads(CONTENT_PATH.read_text())
        doc["texts"]["worker.failure_reference"] = "追溯号：{trace_id}。"
        with self.assertRaises(ValueError):
            ContentCatalog.from_mapping(doc)

    def test_worker_log_process_number_is_separate(self):
        stream = io.StringIO()
        _log(stream, B, "info", "terminal", **reference_fields(TASK, A))
        row = json.loads(stream.getvalue())
        self.assertEqual(row["worker_run_id"], B)
        self.assertEqual(row["trace_id"], A)


class WorkerReferenceTests(unittest.TestCase):
    def test_real_terminal_branches_and_context_is_authoritative(self):
        for code in (
            "session_failed",
            "turn_timeout",
            "side_effect_uncertain",
            "max_turns_exceeded",
            "result_too_large",
            "mcp_bad_gateway",
            "redacted_withheld",
            "interrupted",
            "context_too_long",
        ):
            for source in (A, None):
                queue = FakeWorkerQueue()
                queue.claimed = replace(queue.claimed, task_id=TASK, trace_id=B)
                queue.context = replace(queue.context, task_id=TASK, trace_id=source)
                rows = []

                class Executor:
                    async def run_turn(self, prompt, **kwargs):
                        if code == "redacted_withheld":
                            return {
                                "turn": {
                                    "closed": True,
                                    "final_text": "",
                                    "output_safety": {"withheld": True},
                                }
                            }
                        return {"failure": {"code": code}}

                service = WorkerService(
                    config=worker_config(),
                    queue=queue,
                    executor_factory=lambda config, marker: Executor(),
                    observers=WorkerObservers(on_terminal_outcome=rows.append),
                )
                asyncio.run(service.process_once())
                text = queue.terminals[0]["content"]
                if code in {"interrupted", "context_too_long"}:
                    self.assertNotIn("号：", text)
                else:
                    self.assertIn(A if source else "T-" + B, text)
                self.assertEqual(rows[0]["trace_id"], source)
                self.assertEqual(rows[0]["reference"], source or "T-" + B)

    def test_four_interleaved_executors_and_late_callbacks_remain_bound(self):
        async def exercise():
            stream = io.StringIO()
            callbacks = []
            executors = []
            rows = []
            queues = []
            for index in range(4):
                task_id = "tsk_" + f"01J{index:023d}"
                source = f"01K{index:023d}" if index < 3 else None
                queue = FakeWorkerQueue(stopped=False)
                queue.claimed = replace(queue.claimed, task_id=task_id, trace_id=B)
                queue.context = replace(queue.context, task_id=task_id, trace_id=source)
                queues.append(queue)

            def factory(config, marker):
                executor = WorkerTurnExecutor(config, stderr_stream=stream)
                callbacks.append(executor._sdk_stderr_sink)
                executors.append(executor)

                class Run:
                    async def run_turn(self, prompt, **kwargs):
                        executor._sdk_stderr_sink("合成开始")
                        await asyncio.sleep(0)
                        executor._sdk_stderr_sink("合成结束")
                        number = int(config.task_id[-1])
                        if number == 0:
                            return {"turn": {"closed": True, "final_text": "成功"}}
                        return {
                            "failure": {
                                "code": ("turn_timeout", "interrupted", "session_failed")[
                                    number - 1
                                ]
                            }
                        }

                return Run()

            class ManyQueue(FakeWorkerQueue):
                def __init__(self):
                    super().__init__()
                    self.remaining = [q.claimed for q in queues]

                def claim(self, **kwargs):
                    result, self.remaining = self.remaining, []
                    return result

                def task_context(self, *, task_id, **kwargs):
                    return next(q.context for q in queues if q.context.task_id == task_id)

            queue = ManyQueue()
            service = WorkerService(
                config=worker_config(max_concurrency=4),
                queue=queue,
                executor_factory=factory,
                observers=WorkerObservers(on_terminal_outcome=rows.append),
            )
            await service.process_once()
            for cb in reversed(callbacks):
                cb("合成迟到回调")
            expected = {q.context.task_id: q.context.trace_id for q in queues}
            for row in [*map(json.loads, stream.getvalue().splitlines()), *rows]:
                self.assertEqual(row["trace_id"], expected[row["task_id"]])
                self.assertEqual(
                    row["reference"], expected[row["task_id"]] or "T-" + row["task_id"][4:]
                )
            self.assertEqual(len(stream.getvalue().splitlines()), 12)
            self.assertEqual(len(rows), 4)
            self.assertCountEqual([row["task_id"] for row in rows], expected)

        asyncio.run(exercise())


class TaskAlertTests(unittest.TestCase):
    def test_alerts_separate_tasks_and_preserve_non_task_event(self):
        from test_alerting_integration import FakeAlertSender, ManualClock, RecordingAudit

        from lingxi.core.alerting import AlertDispatcher, AlertingDuty, AlertManager, AlertPolicy

        sender, clock, audit = FakeAlertSender(), ManualClock(), RecordingAudit()
        duty = AlertingDuty(
            manager=AlertManager(policy=AlertPolicy(send_failure_threshold=1)),
            dispatcher=AlertDispatcher(sender=sender, chat_id="synthetic", clock=clock),
            clock=clock,
            audit=audit,
        )
        report = duty.delivery_alert_callback()
        report("dispatch_uncertain:card_finish", TASK, A)
        report("dispatch_uncertain:card_finish", TASK, A)
        report("dispatch_uncertain:card_finish", "tsk_" + A, None)
        report("delivery_loop_failed", "gateway-delivery-loop")
        duty.dispatcher.run_once(at=clock.value)
        self.assertEqual(len(sender.calls), 3)
        self.assertEqual(len({r["dedupe_key"] for r in sender.calls}), 3)
        text = "\n".join(r["text"] for r in sender.calls)
        self.assertIn("追溯号：" + A, text)
        self.assertIn("任务参考号：T-" + A, text)
        self.assertIn("gateway-delivery-loop", text)
        task_rows = [fields for _, fields in audit.records if fields["task_id"]]
        self.assertEqual({r["task_id"] for r in task_rows}, {TASK, "tsk_" + A})
        self.assertEqual(task_rows[0]["trace_id"], A)

    def test_admin_task_shape_keeps_authorization_and_invalid_zero_calls(self):
        from test_admin_router import (
            ADMIN_OPEN_ID,
            FakeQueries,
            FakeRegistry,
            _full_admin_entry,
            _router,
        )

        for identity in ("unknown", "ou_employee"):
            registry = FakeRegistry(
                {"ou_employee": replace(_full_admin_entry(), roles=frozenset())}
            )
            router, _, queries, _ = _router(queries=FakeQueries(), registry=registry)
            router.route(open_id=identity, text="/admin trace T-" + A, trace_id=B)
            self.assertEqual(queries.trace_calls, [])
        router, _, queries, _ = _router(queries=FakeQueries())
        router.route(open_id=ADMIN_OPEN_ID, text="/admin trace T-" + A, trace_id=B)
        self.assertEqual(queries.trace_calls, ["T-" + A])
        for bad in BAD:
            router, _, queries, _ = _router(queries=FakeQueries())
            router.route(open_id=ADMIN_OPEN_ID, text="/admin trace " + bad, trace_id=B)
            self.assertEqual(queries.trace_calls, [])
