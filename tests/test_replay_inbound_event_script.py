"""S-A-07 受控验收夹具（Issue #57 验收缺口）：``scripts/replay_inbound_event.py``
的纯逻辑单测。

只测脚本里不需要真实数据库/飞书/长连接的两块：envelope 校验、``ReplayTransport``
的产出与结果摘要拼装、``_AuditCapture`` 的过滤规则。真正重放一个真实事件属
L4a，留给 biai-stage/Bot-Test 受控执行，不在这里断言。

加载方式照抄既有先例 ``tests/test_galaxy_import_script.py``：``scripts/`` 不是一个
包，用 ``importlib.util.spec_from_file_location`` 按路径直接装载模块。
"""

from __future__ import annotations

import importlib.util
import json
import logging
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "replay_inbound_event.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("replay_inbound_event_script_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _envelope(
    *,
    event_id: str = "evt_1",
    event_type: str = "im.message.receive_v1",
    open_id: str = "ou_1",
    message_id: str = "om_1",
    chat_id: str = "oc_1",
    chat_type: str = "p2p",
    message_type: str = "text",
    content: str = '{"text": "hi"}',
) -> dict:
    return {
        "header": {"event_id": event_id, "event_type": event_type},
        "event": {
            "sender": {"sender_id": {"open_id": open_id}},
            "message": {
                "message_id": message_id,
                "chat_id": chat_id,
                "chat_type": chat_type,
                "message_type": message_type,
                "content": content,
            },
        },
    }


class ValidateEnvelopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_script()

    def test_a_well_formed_envelope_passes(self) -> None:
        self.module.validate_envelope(_envelope())  # 不抛异常即通过

    def test_not_an_object_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.module.validate_envelope(["not", "an", "object"])

    def test_missing_fields_are_all_reported_at_once(self) -> None:
        """一次性报告全部问题，而不是命中第一个就退出——验收现场改一次文件
        比来回跑脚本各改一个字段省事。
        """

        with self.assertRaises(ValueError) as raised:
            self.module.validate_envelope({"header": {}, "event": {}})
        message = str(raised.exception)
        self.assertIn("header.event_id", message)
        self.assertIn("header.event_type", message)
        self.assertIn("event.sender.sender_id.open_id", message)
        self.assertIn("event.message.message_id", message)
        self.assertIn("event.message.chat_id", message)
        self.assertIn("event.message.chat_type", message)
        self.assertIn("event.message.message_type", message)
        self.assertIn("event.message.content", message)

    def test_wrong_event_type_is_rejected(self) -> None:
        payload = _envelope(event_type="card.action.trigger")
        with self.assertRaises(ValueError) as raised:
            self.module.validate_envelope(payload)
        self.assertIn("header.event_type", str(raised.exception))

    def test_group_chat_is_rejected(self) -> None:
        """问数与多轮对话只服务飞书私聊：群聊事件在生产入口本身就会被拒绝，
        重放它测不出幂等，必须在启动前就挡住。
        """

        payload = _envelope(chat_type="group")
        with self.assertRaises(ValueError) as raised:
            self.module.validate_envelope(payload)
        self.assertIn("event.message.chat_type", str(raised.exception))

    def test_blank_open_id_counts_as_missing(self) -> None:
        payload = _envelope(open_id="   ")
        with self.assertRaises(ValueError) as raised:
            self.module.validate_envelope(payload)
        self.assertIn("event.sender.sender_id.open_id", str(raised.exception))

    def test_missing_message_type_is_rejected(self) -> None:
        """S-A-07 r19 实测缺口（Issue #57 评论 5307741204）：缺 message_type 的
        envelope 会被真实解析判为不支持类型、不建任务，验收者会把"脚本输入不
        完整"误读成产品回归——必须在启动前挡住。
        """

        payload = _envelope()
        del payload["event"]["message"]["message_type"]
        with self.assertRaises(ValueError) as raised:
            self.module.validate_envelope(payload)
        self.assertIn("event.message.message_type", str(raised.exception))

    def test_non_text_message_type_is_rejected(self) -> None:
        payload = _envelope(message_type="image")
        with self.assertRaises(ValueError) as raised:
            self.module.validate_envelope(payload)
        message = str(raised.exception)
        self.assertIn("event.message.message_type", message)
        self.assertIn("'text'", message)

    def test_padded_text_message_type_is_rejected(self) -> None:
        """独立审核 F6：生产解析 ``message_text`` 用原值比较，``"text "`` 会被判成
        非文本、正文提取为空——校验必须与生产同口径做精确比较，strip 后放行会让
        重放悄悄变成一个空问题任务。"""

        payload = _envelope(message_type="text ")
        with self.assertRaises(ValueError) as raised:
            self.module.validate_envelope(payload)
        self.assertIn("event.message.message_type", str(raised.exception))


class AuditCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_script()
        self.capture = self.module._AuditCapture()

    def _emit(self, logger_name: str, msg: str, args: tuple) -> None:
        record = logging.LogRecord(
            name=logger_name,
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg=msg,
            args=args,
            exc_info=None,
        )
        self.capture.emit(record)

    def test_captures_gateway_audit_actions(self) -> None:
        self._emit("lingxi.apps.gateway", "audit %s %s", ("inbound_event.duplicate", {}))
        self.assertEqual(self.capture.actions, ["inbound_event.duplicate"])

    def test_ignores_records_from_other_loggers(self) -> None:
        self._emit("lingxi.apps.gateway.delivery", "audit %s %s", ("task.enqueued", {}))
        self.assertEqual(self.capture.actions, [])

    def test_ignores_non_audit_log_lines(self) -> None:
        self._emit("lingxi.apps.gateway", "收到信号 %s，开始停机", (15,))
        self.assertEqual(self.capture.actions, [])


class ReplayTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_script()

    def test_stream_yields_the_same_envelope_the_requested_number_of_times(self) -> None:
        envelope = _envelope(event_id="evt_replay")
        audit = self.module._AuditCapture()
        transport = self.module.ReplayTransport(envelope, times=3, audit=audit)

        produced = list(transport.stream())

        self.assertEqual(produced, [envelope, envelope, envelope])
        self.assertTrue(transport.exhausted, "产完全部剧本后必须置 exhausted，供 should_stop 收工")
        self.assertEqual(transport.connects, 1, "全部重放放在同一次连接会话里")

    def test_exhausted_is_false_until_the_generator_is_fully_drained(self) -> None:
        envelope = _envelope()
        audit = self.module._AuditCapture()
        transport = self.module.ReplayTransport(envelope, times=2, audit=audit)

        iterator = transport.stream()
        next(iterator)
        self.assertFalse(
            transport.exhausted, "还没产完剧本时 exhausted 必须是 False，否则会提前收工漏放"
        )
        next(iterator)
        with self.assertRaises(StopIteration):
            next(iterator)
        self.assertTrue(transport.exhausted)

    def test_report_summarises_the_first_round_as_not_duplicate(self) -> None:
        envelope = _envelope(event_id="evt_first")
        audit = self.module._AuditCapture()
        transport = self.module.ReplayTransport(envelope, times=2, audit=audit)

        audit.actions.append("inbound_event.auto_provisioning")
        transport.report(envelope, None)

        self.assertEqual(len(transport.rounds), 1)
        summary = transport.rounds[0]
        self.assertEqual(summary["round"], 1)
        self.assertEqual(summary["event_id"], "evt_first")
        self.assertFalse(summary["duplicate"])
        self.assertEqual(summary["audit_actions"], ["inbound_event.auto_provisioning"])
        self.assertIsNone(summary["handler_error"])
        self.assertEqual(audit.actions, [], "读取之后必须清空，不能被下一轮继续累积")

    def test_report_summarises_a_replay_as_duplicate(self) -> None:
        envelope = _envelope(event_id="evt_dup")
        audit = self.module._AuditCapture()
        transport = self.module.ReplayTransport(envelope, times=2, audit=audit)

        audit.actions.append("inbound_event.duplicate")
        transport.report(envelope, None)

        self.assertTrue(transport.rounds[0]["duplicate"])

    def test_report_records_a_handler_error_when_present(self) -> None:
        envelope = _envelope()
        audit = self.module._AuditCapture()
        transport = self.module.ReplayTransport(envelope, times=1, audit=audit)

        transport.report(envelope, RuntimeError("boom"))

        self.assertEqual(transport.rounds[0]["handler_error"], "RuntimeError")

    def test_report_output_never_contains_message_content(self) -> None:
        """不打印消息正文全文：摘要只包含固定 action 名称与 event_id，不回显
        ``event.message.content``。
        """

        secret_text = "这是不应该出现在摘要里的消息正文"
        envelope = _envelope(content=json.dumps({"text": secret_text}))
        audit = self.module._AuditCapture()
        transport = self.module.ReplayTransport(envelope, times=1, audit=audit)

        transport.report(envelope, None)

        rendered = json.dumps(transport.rounds[0], ensure_ascii=False)
        self.assertNotIn(secret_text, rendered)


class RedactedTargetSummaryTests(unittest.TestCase):
    """独立审核 P3-1：启动摘要必须能帮执行者肉眼确认环境，同时绝不带出口令
    或完整 DSN——摘要本身也可能被贴进工单或聊天记录。
    """

    def setUp(self) -> None:
        self.module = _load_script()

    def test_summary_contains_host_and_dbname_but_never_the_password(self) -> None:
        from lingxi.apps.gateway.config import ENV_PREFIX, load_config

        password = "super-secret-password-only-for-this-test"
        dsn = f"postgresql://lingxi:{password}@db.example.internal:5432/lingxi_prod?sslmode=require"
        env = {
            f"{ENV_PREFIX}APP_ID": "cli_1234567890abcdef",
            f"{ENV_PREFIX}APP_SECRET": "fake-secret-for-test-only",
            f"{ENV_PREFIX}POSTGRES_DSN": dsn,
        }
        config = load_config(env)

        summary = self.module.redacted_target_summary(config)

        self.assertIn("db.example.internal", summary)
        self.assertIn("5432", summary)
        self.assertIn("lingxi_prod", summary)
        self.assertIn("cli_123456", summary, "只回显 app_id 前 10 位，供肉眼核对是哪个飞书应用")
        self.assertNotIn(password, summary)
        self.assertNotIn(dsn, summary, "不得回显完整 DSN")
        self.assertNotIn("sslmode", summary, "查询参数不进摘要")
        self.assertNotIn("cli_1234567890abcdef", summary, "不完整回显 app_id")

    def test_summary_degrades_gracefully_when_the_dsn_has_no_host(self) -> None:
        """畸形 DSN 不应该让摘要本身报错——它只是一份肉眼核对用的提示。"""

        from lingxi.apps.gateway.config import ENV_PREFIX, load_config

        env = {
            f"{ENV_PREFIX}APP_ID": "cli_x",
            f"{ENV_PREFIX}APP_SECRET": "fake-secret-for-test-only",
            f"{ENV_PREFIX}POSTGRES_DSN": "not-a-valid-dsn",
        }
        config = load_config(env)

        summary = self.module.redacted_target_summary(config)

        self.assertIn("无法解析", summary)

    def test_summary_never_contains_the_password_for_a_keyword_value_dsn(self) -> None:
        """libpq 的关键字/值写法（``host=... password=...``）没有 ``scheme://``，
        ``urlsplit`` 解不出 ``netloc``、会把整段原样塞进 ``path``。这条断言要能
        抓住"忘了先判 netloc 是否非空、直接把 path 当 dbname 打出来"这类回归——
        独立审核实测：把实现里的 ``if parsed.netloc:`` 改成 ``if True:`` 后，
        本用例必须变红（已自查，见提交说明）。
        """

        from lingxi.apps.gateway.config import ENV_PREFIX, load_config

        password = "S3cr3tP@ss-only-for-this-test"
        dsn = f"host=db.example.internal port=5432 dbname=lingxi_prod user=lingxi password={password}"
        env = {
            f"{ENV_PREFIX}APP_ID": "cli_1234567890abcdef",
            f"{ENV_PREFIX}APP_SECRET": "fake-secret-for-test-only",
            f"{ENV_PREFIX}POSTGRES_DSN": dsn,
        }
        config = load_config(env)

        summary = self.module.redacted_target_summary(config)

        self.assertNotIn(password, summary)
        self.assertNotIn(dsn, summary, "不得原样回显关键字/值形式的整条 DSN")

    def test_summary_degrades_gracefully_when_the_port_is_not_numeric(self) -> None:
        """端口段不是合法数字时（如 ``...@host:notaport/db``），``urlsplit`` 切
        ``netloc`` 本身不报错——只有真的访问 ``.port`` 才会抛未捕获的
        ``ValueError``，之前会让脚本在打印这行肉眼核对提示的时候直接 traceback
        中止，而不是退化成占位符。
        """

        from lingxi.apps.gateway.config import ENV_PREFIX, load_config

        env = {
            f"{ENV_PREFIX}APP_ID": "cli_1234567890abcdef",
            f"{ENV_PREFIX}APP_SECRET": "fake-secret-for-test-only",
            f"{ENV_PREFIX}POSTGRES_DSN": "postgresql://lingxi:pass@db.example.internal:notaport/lingxi_prod",
        }
        config = load_config(env)

        summary = self.module.redacted_target_summary(config)  # 不得抛出 ValueError

        self.assertIn("db.example.internal", summary, "host 段本身合法，仍应正常回显")
        self.assertIn("无法解析", summary, "端口段解析失败时退化为占位符")
        self.assertNotIn("notaport", summary)
        self.assertNotIn("pass", summary, "netloc 里的口令片段不得出现在摘要里")


if __name__ == "__main__":
    unittest.main()
