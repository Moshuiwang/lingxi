"""问数 MCP 就绪探针适配器的断言（Issue #156 / S-C-02）。

认领断言：`V-权限-06` 的传输面（以**目标用户身份**发起真实调用；明确空结果不算就绪；
读不懂的响应一律落技术失败，绝不落就绪）。

**真实 MCP 协议面未实测（证据等级 L1）**：下面全部断言跑在注入的假传输层上，本 Story
一次真实调用都没有发生过。真实端点形态、``list_metrics`` 的返回形状与"权限还没同步"的
错误形态留 Epic C 冻结后的受控窗口做 L4a——模块文档「诚实边界」一节是这条登记的正文。

否定面：

- 令牌**不进 URL、不进日志、不进异常消息**，只进 ``Authorization`` 头；
- 端点不是 ``https://`` 一律拒绝（否则 Bearer 令牌明文上路）；
- 取不到令牌（没签发、解密失败、库不可达）落**技术失败**，不落就绪也不落无权限；
- 未知响应形状、未知结果形状、读不出指标列表，一律落技术失败；
- ``content`` 里只有一个文本块时**不数块数**——那会把"你没有任何指标"读成 1。
"""

from __future__ import annotations

import unittest

from lingxi.adapters.query_mcp_probe import (
    DEFAULT_TOOL_NAME,
    McpHttpResponse,
    QueryMcpProbe,
    default_metrics_reader,
)
from lingxi.core.permission.mcp_readiness import McpProbeError

ENDPOINT = "https://mcp.example.invalid/query"
USER = "usr_A"
TOKEN = "plaintext-token-for-tests-only"


class RecordingTransport:
    """记录每一次调用的假传输层。按脚本逐次返回响应或抛出异常。"""

    def __init__(self, *script: object) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    def __call__(self, method, url, *, body=None, token=None):
        self.calls.append({"method": method, "url": url, "body": body, "token": token})
        step = self.script.pop(0) if self.script else McpHttpResponse(200, {"result": {}})
        if isinstance(step, BaseException):
            raise step
        return step


def _ok(count: int) -> McpHttpResponse:
    return McpHttpResponse(
        200, {"jsonrpc": "2.0", "id": "x", "result": {"structuredContent": {"metrics": ["m"] * count}}}
    )


def _probe(*script: object, **kwargs) -> tuple[QueryMcpProbe, RecordingTransport]:
    transport = RecordingTransport(*script)
    options = {"token_provider": lambda user_id: TOKEN}
    options.update(kwargs)
    return QueryMcpProbe(endpoint=ENDPOINT, transport=transport, **options), transport


class ConstructionTest(unittest.TestCase):
    def test_requires_https_endpoint(self) -> None:
        for endpoint in ("http://mcp.example.invalid", "mcp.example.invalid", "", None, 42):
            with self.subTest(endpoint=repr(endpoint)):
                with self.assertRaises(ValueError) as caught:
                    QueryMcpProbe(endpoint=endpoint, token_provider=lambda _: TOKEN)
                if endpoint:
                    # 不回显收到的值（空串不构成回显，跳过）。
                    self.assertNotIn(str(endpoint), str(caught.exception))

    def test_requires_a_callable_token_provider(self) -> None:
        with self.assertRaises(ValueError):
            QueryMcpProbe(endpoint=ENDPOINT, token_provider=TOKEN)  # type: ignore[arg-type]

    def test_requires_a_tool_name(self) -> None:
        with self.assertRaises(ValueError):
            QueryMcpProbe(endpoint=ENDPOINT, token_provider=lambda _: TOKEN, tool_name="  ")


class RequestShapeTest(unittest.TestCase):
    def test_calls_list_metrics_as_the_target_user(self) -> None:
        probe, transport = _probe(_ok(3))
        self.assertEqual(probe.list_metrics(user_id=USER), 3)
        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], ENDPOINT)
        self.assertEqual(call["body"]["method"], "tools/call")
        self.assertEqual(call["body"]["params"]["name"], DEFAULT_TOOL_NAME)
        self.assertEqual(call["body"]["jsonrpc"], "2.0")

    def test_token_only_travels_in_the_transport_token_slot(self) -> None:
        """令牌不进 URL、不进请求体——只交给传输层放进 ``Authorization`` 头。"""

        probe, transport = _probe(_ok(1))
        probe.list_metrics(user_id=USER)
        call = transport.calls[0]
        self.assertEqual(call["token"], TOKEN)
        self.assertNotIn(TOKEN, call["url"])
        self.assertNotIn(TOKEN, str(call["body"]))

    def test_rejects_empty_user(self) -> None:
        probe, transport = _probe(_ok(1))
        for user_id in ("", "   ", None, 42):
            with self.subTest(user_id=repr(user_id)):
                with self.assertRaises(ValueError):
                    probe.list_metrics(user_id=user_id)
        self.assertEqual(transport.calls, [])


class TokenFailureTest(unittest.TestCase):
    """取不到令牌是**技术失败**，不是"没权限"，也绝不是就绪。"""

    def test_missing_token_is_technical(self) -> None:
        for value in (None, "", 42):
            with self.subTest(value=repr(value)):
                probe, transport = _probe(_ok(1), token_provider=lambda _u, v=value: v)
                with self.assertRaises(McpProbeError) as caught:
                    probe.list_metrics(user_id=USER)
                self.assertEqual(caught.exception.code, "token_missing")
                self.assertFalse(caught.exception.denied)
                # 拿不到令牌就不发请求。
                self.assertEqual(transport.calls, [])

    def test_provider_failure_is_translated_not_leaked(self) -> None:
        def failing(_user_id: str) -> str:
            raise RuntimeError("dbname=lingxi password=不该出现在任何地方")

        probe, transport = _probe(_ok(1), token_provider=failing)
        with self.assertRaises(McpProbeError) as caught:
            probe.list_metrics(user_id=USER)
        self.assertEqual(caught.exception.code, "token_unavailable")
        self.assertFalse(caught.exception.denied)
        message = f"{caught.exception!r} {caught.exception}"
        self.assertNotIn("password", message)
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(transport.calls, [])


class ClassificationTest(unittest.TestCase):
    def test_auth_status_is_denied_not_technical(self) -> None:
        for status in (401, 403):
            with self.subTest(status=status):
                probe, _ = _probe(McpHttpResponse(status, None))
                with self.assertRaises(McpProbeError) as caught:
                    probe.list_metrics(user_id=USER)
                self.assertTrue(caught.exception.denied)
                self.assertEqual(caught.exception.code, f"http_{status}")

    def test_other_http_status_is_technical(self) -> None:
        for status in (429, 500, 502, 404):
            with self.subTest(status=status):
                probe, _ = _probe(McpHttpResponse(status, None))
                with self.assertRaises(McpProbeError) as caught:
                    probe.list_metrics(user_id=USER)
                self.assertFalse(caught.exception.denied)

    def test_tool_level_error_is_denied(self) -> None:
        probe, _ = _probe(McpHttpResponse(200, {"result": {"isError": True, "content": []}}))
        with self.assertRaises(McpProbeError) as caught:
            probe.list_metrics(user_id=USER)
        self.assertEqual(caught.exception.code, "tool_error")
        self.assertTrue(caught.exception.denied)

    def test_jsonrpc_error_defaults_to_technical(self) -> None:
        """真实拒绝码未实测，默认保守：落技术失败，**绝不落就绪**。"""

        probe, _ = _probe(
            McpHttpResponse(200, {"error": {"code": -32003, "message": "回显了请求内容"}})
        )
        with self.assertRaises(McpProbeError) as caught:
            probe.list_metrics(user_id=USER)
        self.assertEqual(caught.exception.code, "jsonrpc_-32003")
        self.assertFalse(caught.exception.denied)
        # 服务端文本一个字都不进我们的错误码。
        self.assertNotIn("回显了请求内容", str(caught.exception))

    def test_denied_error_codes_are_injectable(self) -> None:
        probe, _ = _probe(
            McpHttpResponse(200, {"error": {"code": -32003}}), denied_error_codes=(-32003,)
        )
        with self.assertRaises(McpProbeError) as caught:
            probe.list_metrics(user_id=USER)
        self.assertTrue(caught.exception.denied)

    def test_broken_shapes_are_technical(self) -> None:
        cases = {
            "invalid_response_shape": McpHttpResponse(200, None),
            "invalid_result_shape": McpHttpResponse(200, {"result": "文本"}),
            "invalid_transport_result": "不是 McpHttpResponse",
        }
        for code, response in cases.items():
            with self.subTest(code=code):
                probe, _ = _probe(response)
                with self.assertRaises(McpProbeError) as caught:
                    probe.list_metrics(user_id=USER)
                self.assertEqual(caught.exception.code, code)
                self.assertFalse(caught.exception.denied)

    def test_empty_metric_list_is_returned_as_zero_not_ready(self) -> None:
        """适配器如实返回 0；"0 不算就绪"由状态机判定（职责分离）。"""

        probe, _ = _probe(_ok(0))
        self.assertEqual(probe.list_metrics(user_id=USER), 0)


class MetricsReaderTest(unittest.TestCase):
    def test_reads_structured_content_lists(self) -> None:
        for key in ("metrics", "items", "data", "result", "list"):
            with self.subTest(key=key):
                self.assertEqual(
                    default_metrics_reader({"structuredContent": {key: ["a", "b"]}}), 2
                )

    def test_reads_a_bare_structured_list(self) -> None:
        self.assertEqual(default_metrics_reader({"structuredContent": ["a"]}), 1)

    def test_reads_json_inside_a_text_block(self) -> None:
        result = {"content": [{"type": "text", "text": '{"metrics":["日活","收入"]}'}]}
        self.assertEqual(default_metrics_reader(result), 2)
        empty = {"content": [{"type": "text", "text": '{"metrics":[]}'}]}
        self.assertEqual(default_metrics_reader(empty), 0)

    def test_never_counts_content_blocks(self) -> None:
        """数块数会把"你没有任何指标"读成 1——恰好是要挡的那种假成功。"""

        result = {"content": [{"type": "text", "text": "你当前没有可用指标"}]}
        with self.assertRaises(McpProbeError) as caught:
            default_metrics_reader(result)
        self.assertEqual(caught.exception.code, "unrecognized_result_shape")
        self.assertFalse(caught.exception.denied)

    def test_unknown_shape_fails_instead_of_guessing(self) -> None:
        for result in ({}, {"structuredContent": 3}, {"content": "文本"}, {"content": [1, 2]}):
            with self.subTest(result=result):
                with self.assertRaises(McpProbeError):
                    default_metrics_reader(result)

    def test_injected_reader_result_is_validated(self) -> None:
        """注入的 reader 返回垃圾同样落技术失败，不放行。"""

        for value in (-1, "3", None, True):
            with self.subTest(value=repr(value)):
                probe, _ = _probe(
                    McpHttpResponse(200, {"result": {}}), metrics_reader=lambda _r, v=value: v
                )
                with self.assertRaises(McpProbeError) as caught:
                    probe.list_metrics(user_id=USER)
                self.assertEqual(caught.exception.code, "invalid_metric_count")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
