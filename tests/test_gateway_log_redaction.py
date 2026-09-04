"""Issue #176（安全）：Gateway 进程内对第三方飞书 SDK 日志的凭据脱敏。

伪造值统一使用明显假的占位（``fake-access-key-value``/``fake-ticket-value``），
不使用任何形似真实凭据的值（协作约定：测试与 CI 不接触真实凭据、不复现真实
日志原文）。
"""

from __future__ import annotations

import io
import logging
import unittest

from lingxi.apps.gateway.log_redaction import (
    LARK_SDK_LOGGER_NAME,
    CredentialQueryRedactingFilter,
    install_credential_redaction,
)
from lingxi.core.execution.audit import redact_query_parameter_values


class RedactQueryParameterValuesTests(unittest.TestCase):
    """纯函数层：核心脱敏出口 core.execution.audit.redact_query_parameter_values。"""

    def test_query_parameter_values_are_masked_but_keys_and_shape_survive(self) -> None:
        text = (
            "connected to wss://example.invalid/ws?device_id=dev-1"
            "&access_key=fake-access-key-value&ticket=fake-ticket-value"
        )

        redacted = redact_query_parameter_values(text)

        self.assertNotIn("fake-access-key-value", redacted)
        self.assertNotIn("fake-ticket-value", redacted)
        self.assertIn("device_id=***", redacted)
        self.assertIn("access_key=***", redacted)
        self.assertIn("ticket=***", redacted)
        # 参数名与查询串的形状必须保留，脱敏不是整段抹掉——运维仍要能诊断
        # "查询串长什么样"，只是看不到具体取值。
        self.assertTrue(redacted.startswith("connected to wss://example.invalid/ws?"))

    def test_text_without_query_parameters_is_unchanged(self) -> None:
        text = "connected to wss://example.invalid/ws"

        self.assertEqual(redact_query_parameter_values(text), text)

    def test_an_empty_query_parameter_value_is_still_masked(self) -> None:
        redacted = redact_query_parameter_values("?ticket=")

        self.assertEqual(redacted, "?ticket=***")


class CredentialQueryRedactingFilterTests(unittest.TestCase):
    """构造含伪造认证查询参数的日志记录穿过过滤器→输出无参数值残留。"""

    def _record(self, msg: str, args: tuple = ()) -> logging.LogRecord:
        return logging.LogRecord(
            name=LARK_SDK_LOGGER_NAME,
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg=msg,
            args=args,
            exc_info=None,
        )

    def test_a_record_carrying_a_forged_credential_query_param_is_scrubbed(self) -> None:
        record = self._record(
            "connected to wss://example.invalid/ws"
            "?ticket=fake-ticket-value&access_key=fake-access-key-value"
        )

        kept = CredentialQueryRedactingFilter().filter(record)

        self.assertTrue(kept, "过滤器只改写内容，不应该丢弃这条日志本身")
        rendered = record.getMessage()
        self.assertNotIn("fake-ticket-value", rendered)
        self.assertNotIn("fake-access-key-value", rendered)
        self.assertIn("ticket=***", rendered)

    def test_percent_style_args_are_redacted_after_message_formatting(self) -> None:
        """第三方代码也可能用 ``logger.info(fmt, *args)`` 这种老式写法——参数值
        此时藏在 ``record.args`` 里，不在 ``record.msg`` 的字面量文本中，过滤器
        必须先完成一次格式化才能看到、进而盖住它。
        """

        record = self._record(
            "connected to %s", ("wss://example.invalid/ws?ticket=fake-ticket-value",)
        )

        CredentialQueryRedactingFilter().filter(record)

        self.assertNotIn("fake-ticket-value", record.getMessage())

    def test_a_message_without_any_query_parameters_passes_through_unchanged(self) -> None:
        record = self._record("ping success")

        CredentialQueryRedactingFilter().filter(record)

        self.assertEqual(record.getMessage(), "ping success")


class InstallCredentialRedactionTests(unittest.TestCase):
    """安装函数本身：两层覆盖（命名 logger 源头 + root handler 兜底）都真的生效。"""

    def test_install_attaches_the_filter_to_the_named_source_logger(self) -> None:
        logger_name = "Lark-test-source-only"
        target = logging.getLogger(logger_name)
        before = list(target.filters)
        try:
            install_credential_redaction(source_logger_names=(logger_name,))

            self.assertTrue(
                any(isinstance(f, CredentialQueryRedactingFilter) for f in target.filters)
            )
        finally:
            target.filters = before

    def test_install_attaches_the_filter_to_every_existing_root_handler(self) -> None:
        root = logging.getLogger()
        probe_handler = logging.StreamHandler(io.StringIO())
        root.addHandler(probe_handler)
        try:
            install_credential_redaction(source_logger_names=())

            self.assertTrue(
                any(isinstance(f, CredentialQueryRedactingFilter) for f in probe_handler.filters)
            )
        finally:
            root.removeHandler(probe_handler)

    def test_a_record_from_an_unrelated_logger_is_still_scrubbed_by_the_root_handler(
        self,
    ) -> None:
        """覆盖处置要求"根 logger 传播路径"这半句：即使消息来自一个完全不叫
        Lark 的 logger，只要它传播到 root 已安装的 handler，也必须被同一层过滤器
        截住——这是不依赖"第三方 SDK 固定叫这个名字"这个可能过期的假设的兜底。
        """

        root = logging.getLogger()
        stream = io.StringIO()
        probe_handler = logging.StreamHandler(stream)
        probe_handler.setFormatter(logging.Formatter("%(message)s"))
        other_logger = logging.getLogger("not-lark-at-all")
        other_logger.setLevel(logging.INFO)
        root.addHandler(probe_handler)
        try:
            install_credential_redaction(source_logger_names=())

            other_logger.info("connected to wss://example.invalid/ws?ticket=fake-ticket-value")

            self.assertNotIn("fake-ticket-value", stream.getvalue())
            self.assertIn("ticket=***", stream.getvalue())
        finally:
            root.removeHandler(probe_handler)


class GatewayMainInstallsRedactionTests(unittest.TestCase):
    """再加一条测试证明 gateway 的日志装配确实安装了该过滤器——经真实 main()
    入口验证，不是只测一个没被接线调用的函数。

    ``main(env={})`` 会在装配之后很快因为缺少必填环境变量抛出
    ``GatewayConfigError`` 提前返回：这条路径不连数据库、不建长连接、不发起
    任何网络调用，但 ``logging.basicConfig`` 与 ``install_credential_redaction``
    都已经在配置校验之前执行过，足以证明真实入口确实完成了这层装配。
    """

    def test_calling_the_real_gateway_entry_point_installs_the_filter(self) -> None:
        from lingxi.apps.gateway import main as gateway_main

        lark_logger = logging.getLogger(LARK_SDK_LOGGER_NAME)
        before_lark = list(lark_logger.filters)
        root = logging.getLogger()
        before_root_handlers = list(root.handlers)
        stderr = io.StringIO()
        try:
            import contextlib

            with contextlib.redirect_stderr(stderr):
                code = gateway_main(env={})

            self.assertEqual(code, 2, "空配置必须以配置错误退出，不应该走到长连接装配")
            self.assertTrue(
                any(isinstance(f, CredentialQueryRedactingFilter) for f in lark_logger.filters),
                "main() 必须真的把过滤器挂到 Lark 源头 logger 上",
            )
            self.assertTrue(
                any(
                    isinstance(f, CredentialQueryRedactingFilter)
                    for handler in root.handlers
                    for f in handler.filters
                ),
                "main() 必须真的把过滤器挂到 root logger 当前的 handler 上",
            )
        finally:
            lark_logger.filters = before_lark
            for handler in root.handlers:
                if handler not in before_root_handlers:
                    root.removeHandler(handler)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
