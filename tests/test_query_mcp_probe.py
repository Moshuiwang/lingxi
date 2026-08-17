"""问数 MCP 就绪探针适配器的断言（Issue #156 / S-C-02）。

认领断言：`V-权限-06` 的传输面（以**目标用户身份**发起真实调用；明确空结果不算就绪；
读不懂的响应一律落技术失败，绝不落就绪）。

**真实 MCP 协议面未实测（证据等级 L1）**：下面全部断言跑在注入的假传输层上，本 Story
一次真实调用都没有发生过。真实端点形态、``list_metrics`` 的返回形状与"权限还没同步"的
错误形态留 Epic C 冻结后的受控窗口做 L4a——模块文档「诚实边界」一节是这条登记的正文。

否定面：

- 令牌**不进 URL、不进日志、不进异常消息**，只进 ``Authorization`` 头；
- 端点不是 ``https://`` 一律拒绝（否则 Bearer 令牌明文上路）；
- **不跟随重定向**：https→http 降级与跨主机跳转都被拒，3xx 一律落技术失败
  （跟随会把 Bearer 令牌转发到新地址）；
- 响应必须**自证是这次请求的答复**：``jsonrpc`` 版本与 ``id`` 任一对不上即落技术失败；
- 取不到令牌（没签发、解密失败、库不可达）落**技术失败**，不落就绪也不落无权限；
- 未知响应形状、未知结果形状、指标列表不在约定位置，一律落技术失败；
- ``result.isError`` **默认落技术失败**，"明确拒绝"要显式注入白名单才成立；
- ``content`` 里只有一个文本块时**不数块数**——那会把"你没有任何指标"读成 1。
"""

from __future__ import annotations

import unittest

from lingxi.adapters.query_mcp_probe import (
    DEFAULT_TOOL_NAME,
    McpHttpResponse,
    QueryMcpProbe,
    _no_redirect_opener,
    default_metrics_reader,
)
from lingxi.core.permission.mcp_readiness import McpProbeError

ENDPOINT = "https://mcp.example.invalid/query"
USER = "usr_A"
TOKEN = "plaintext-token-for-tests-only"


class RecordingTransport:
    """记录每一次调用的假传输层。按脚本逐次返回响应或抛出异常。

    脚本元素可以是 :class:`McpHttpResponse`、异常实例，或一个 ``callable(request_id)``
    ——后者用来构造"带正确 id 的响应"，因为 id 是探针每次现生成的 ULID，测试事先不知道。
    """

    def __init__(self, *script: object) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    def __call__(self, method, url, *, body=None, token=None, timeout=None):
        self.calls.append(
            {"method": method, "url": url, "body": body, "token": token, "timeout": timeout}
        )
        step = self.script.pop(0) if self.script else _ok(1)
        if callable(step) and not isinstance(step, McpHttpResponse):
            step = step((body or {}).get("id"))
        if isinstance(step, BaseException):
            raise step
        return step


def _ok(count: int):
    """一份**形状完全正确**的成功响应：版本、id 与请求一致，指标挂在约定的位置。"""

    def build(request_id):
        return McpHttpResponse(
            200,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"structuredContent": {"metrics": ["m"] * count}},
            },
        )

    return build


def _echo(payload: dict):
    """把请求 id 回填进给定载荷后返回 200。"""

    def build(request_id):
        body = dict(payload)
        body.setdefault("jsonrpc", "2.0")
        body["id"] = request_id
        return McpHttpResponse(200, body)

    return build


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

    def test_tool_level_error_defaults_to_technical_not_denied(self) -> None:
        """``isError`` 同时覆盖鉴权拒绝与工具自己崩溃，默认按**技术失败**。

        默认判成"同步中"会让一次工具崩溃安静地等满十五分钟再转运维，而运维拿到的分类是
        "权限还没同步"——指向完全错误的方向。真实语义 L4a 实测后再由装配层显式放宽。
        """

        probe, _ = _probe(_echo({"result": {"isError": True, "content": []}}))
        with self.assertRaises(McpProbeError) as caught:
            probe.list_metrics(user_id=USER)
        self.assertEqual(caught.exception.code, "tool_error")
        self.assertFalse(caught.exception.denied)

    def test_non_boolean_is_error_is_unreadable_not_ignored(self) -> None:
        """**G4**：``isError`` 类型不对就当读不懂，而不是"不是 True 那就当没错"。

        ``1`` / ``"true"`` 这类取值在 ``is True`` 下判否；若响应同时带着非空 metrics，
        一份自称出错的响应就会被一路读成就绪——正好违反"未知形状一律技术失败"。
        """

        for flag in (1, "true", "false", 0, [], {}, 1.0):
            with self.subTest(flag=repr(flag)):
                probe, _ = _probe(
                    _echo(
                        {
                            "result": {
                                "isError": flag,
                                "structuredContent": {"metrics": ["日活", "收入"]},
                            }
                        }
                    )
                )
                with self.assertRaises(McpProbeError) as caught:
                    probe.list_metrics(user_id=USER)
                self.assertEqual(caught.exception.code, "invalid_result_shape")
                self.assertFalse(caught.exception.denied)

    def test_an_explicit_null_is_error_is_unreadable(self) -> None:
        """**H1**：显式 ``"isError": null`` 与"没有这个键"必须区分开。

        按取值判（``result.get("isError")``）时两者都是 ``None``，于是一份自称出错、
        却又带着非空 metrics 的响应会被一路读成就绪。判据因此是**键在不在**。
        """

        probe, _ = _probe(
            _echo(
                {"result": {"isError": None, "structuredContent": {"metrics": ["日活", "收入"]}}}
            )
        )
        with self.assertRaises(McpProbeError) as caught:
            probe.list_metrics(user_id=USER)
        self.assertEqual(caught.exception.code, "invalid_result_shape")
        self.assertFalse(caught.exception.denied)

    def test_a_missing_is_error_key_is_a_normal_success(self) -> None:
        """对照：键**不在**才是"这次调用没出错"，不能被上一条误伤。"""

        probe, _ = _probe(_echo({"result": {"structuredContent": {"metrics": ["日活"]}}}))
        self.assertEqual(probe.list_metrics(user_id=USER), 1)

    def test_is_error_false_is_a_normal_success(self) -> None:
        """对照：显式 ``false`` 是合法的成功响应，不能被上一条误伤。"""

        probe, _ = _probe(
            _echo({"result": {"isError": False, "structuredContent": {"metrics": ["日活"]}}})
        )
        self.assertEqual(probe.list_metrics(user_id=USER), 1)

    def test_tool_level_error_can_be_whitelisted_explicitly(self) -> None:
        probe, _ = _probe(
            _echo({"result": {"isError": True, "content": []}}), tool_error_is_denied=True
        )
        with self.assertRaises(McpProbeError) as caught:
            probe.list_metrics(user_id=USER)
        self.assertTrue(caught.exception.denied)

    def test_jsonrpc_error_defaults_to_technical(self) -> None:
        """真实拒绝码未实测，默认保守：落技术失败，**绝不落就绪**。"""

        probe, _ = _probe(_echo({"error": {"code": -32003, "message": "回显了请求内容"}}))
        with self.assertRaises(McpProbeError) as caught:
            probe.list_metrics(user_id=USER)
        self.assertEqual(caught.exception.code, "jsonrpc_-32003")
        self.assertFalse(caught.exception.denied)
        # 服务端文本一个字都不进我们的错误码。
        self.assertNotIn("回显了请求内容", str(caught.exception))

    def test_a_non_integer_error_code_never_reaches_our_error_code(self) -> None:
        """**H2**：``error.code`` 必须是整数，否则统一落 ``invalid_result_shape``。

        拼进自己的错误码等于让一个**外部可控的字符串**一路进到
        ``mcp_sync_check.error_code``（落库、进审计）；超过 200 字符时还会撞上那一列的
        CHECK，把一次本该记下来的失败变成整轮确认中断。
        """

        payloads = (
            {"error": {"code": "not_authorized"}},
            {"error": {"code": None}},
            {"error": {"code": True}},
            {"error": {"code": 1.5}},
            {"error": {"code": ["x"]}},
            {"error": {"message": "没有 code"}},
            {"error": "整个 error 不是对象"},
            {"error": {"code": "长" * 500}},
        )
        for payload in payloads:
            with self.subTest(payload=str(payload)[:40]):
                probe, _ = _probe(_echo(payload))
                with self.assertRaises(McpProbeError) as caught:
                    probe.list_metrics(user_id=USER)
                self.assertEqual(caught.exception.code, "invalid_result_shape")
                self.assertFalse(caught.exception.denied)
                # 外部文本一个字都没进我们的错误码。
                self.assertNotIn("not_authorized", caught.exception.code)
                self.assertLessEqual(len(caught.exception.code), 200)

    def test_denied_error_codes_are_injectable(self) -> None:
        probe, _ = _probe(_echo({"error": {"code": -32003}}), denied_error_codes=(-32003,))
        with self.assertRaises(McpProbeError) as caught:
            probe.list_metrics(user_id=USER)
        self.assertTrue(caught.exception.denied)

    def test_broken_shapes_are_technical(self) -> None:
        cases = {
            "invalid_response_shape": McpHttpResponse(200, None),
            "invalid_result_shape": _echo({"result": "文本"}),
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


class ResponseIdentityTest(unittest.TestCase):
    """响应必须**自证是这次请求的答复**，否则任何一份长得像结果的 JSON 都能凑成就绪。"""

    def test_response_id_must_equal_the_request_id(self) -> None:
        probe, transport = _probe(
            McpHttpResponse(
                200,
                {"jsonrpc": "2.0", "id": "别人的请求", "result": {"structuredContent": {"metrics": ["m"]}}},
            )
        )
        with self.assertRaises(McpProbeError) as caught:
            probe.list_metrics(user_id=USER)
        self.assertEqual(caught.exception.code, "response_id_mismatch")
        self.assertFalse(caught.exception.denied)

    def test_missing_id_is_rejected(self) -> None:
        probe, _ = _probe(
            McpHttpResponse(200, {"jsonrpc": "2.0", "result": {"structuredContent": {"metrics": ["m"]}}})
        )
        with self.assertRaises(McpProbeError) as caught:
            probe.list_metrics(user_id=USER)
        self.assertEqual(caught.exception.code, "response_id_mismatch")

    def test_jsonrpc_version_must_be_present_and_exact(self) -> None:
        result = {"structuredContent": {"metrics": ["m"]}}
        for version in (None, "1.0", "2", 2.0):
            with self.subTest(version=repr(version)):

                def build(request_id, v=version):
                    payload = {"id": request_id, "result": result}
                    if v is not None:
                        payload["jsonrpc"] = v
                    return McpHttpResponse(200, payload)

                probe, _ = _probe(build)
                with self.assertRaises(McpProbeError) as caught:
                    probe.list_metrics(user_id=USER)
                self.assertEqual(caught.exception.code, "invalid_jsonrpc_version")
                self.assertFalse(caught.exception.denied)

    def test_each_request_uses_a_fresh_identifier(self) -> None:
        probe, transport = _probe(_ok(1), _ok(1))
        probe.list_metrics(user_id=USER)
        probe.list_metrics(user_id=USER)
        first, second = (call["body"]["id"] for call in transport.calls)
        self.assertNotEqual(first, second)
        # 请求标识是内部 ULID，不含任何用户资料。
        self.assertNotIn(USER, first)

    def test_transport_receives_the_configured_timeout(self) -> None:
        probe, transport = _probe(_ok(1), timeout_seconds=7)
        probe.list_metrics(user_id=USER)
        self.assertEqual(transport.calls[0]["timeout"], 7)
        self.assertEqual(probe.timeout_seconds, 7)

    def test_timeout_must_be_a_positive_integer(self) -> None:
        for value in (0, -1, True, 1.5, "20"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    QueryMcpProbe(
                        endpoint=ENDPOINT, token_provider=lambda _: TOKEN, timeout_seconds=value
                    )


class RedirectTest(unittest.TestCase):
    """**不跟随重定向**：跟随会把 Bearer 令牌转发到新主机，甚至降级到 http 明文。"""

    def test_redirect_handler_refuses_every_target(self) -> None:
        from urllib.request import Request

        handler = _no_redirect_opener().handlers
        redirect_handlers = [
            item for item in handler if hasattr(item, "redirect_request")
        ]
        self.assertTrue(redirect_handlers)
        request = Request(ENDPOINT, method="POST")
        for new_url in (
            "http://mcp.example.invalid/query",  # https → http 降级
            "https://attacker.example.invalid/query",  # 跨主机
            ENDPOINT + "/moved",  # 同源也不跟
        ):
            with self.subTest(new_url=new_url):
                for item in redirect_handlers:
                    self.assertIsNone(
                        item.redirect_request(request, None, 302, "Found", {}, new_url)
                    )

    def test_three_hundreds_are_technical_failures(self) -> None:
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                probe, _ = _probe(McpHttpResponse(status, None))
                with self.assertRaises(McpProbeError) as caught:
                    probe.list_metrics(user_id=USER)
                self.assertEqual(caught.exception.code, f"http_{status}")
                self.assertFalse(caught.exception.denied)


class MetricsReaderTest(unittest.TestCase):
    """默认 reader **只认一种形状**：``result.structuredContent.metrics`` 是列表。"""

    def test_reads_the_one_documented_shape(self) -> None:
        self.assertEqual(default_metrics_reader({"structuredContent": {"metrics": ["a", "b"]}}), 2)
        self.assertEqual(default_metrics_reader({"structuredContent": {"metrics": []}}), 0)

    def test_other_keys_are_not_metrics(self) -> None:
        """否定面：``{"structuredContent": {"data": ["warning"]}}`` 与指标毫无关系。

        上一版依次尝试 metrics/items/data/result/list 几个键下的任意列表，于是一份告警
        列表也能数出 1 条、判成就绪——假就绪是五路里唯一不可犯的方向。
        """

        for key in ("items", "data", "result", "list", "warnings"):
            with self.subTest(key=key):
                with self.assertRaises(McpProbeError) as caught:
                    default_metrics_reader({"structuredContent": {key: ["warning"]}})
                self.assertEqual(caught.exception.code, "unrecognized_result_shape")

    def test_a_bare_structured_list_is_not_recognised(self) -> None:
        with self.assertRaises(McpProbeError):
            default_metrics_reader({"structuredContent": ["a"]})

    def test_json_inside_a_text_block_is_no_longer_guessed(self) -> None:
        """文本块里的 JSON 也不再猜——真实形状 L4a 实测后由装配层注入已验证的 reader。"""

        result = {"content": [{"type": "text", "text": '{"metrics":["日活","收入"]}'}]}
        with self.assertRaises(McpProbeError):
            default_metrics_reader(result)

    def test_never_counts_content_blocks(self) -> None:
        """数块数会把"你没有任何指标"读成 1——恰好是要挡的那种假成功。"""

        result = {"content": [{"type": "text", "text": "你当前没有可用指标"}]}
        with self.assertRaises(McpProbeError) as caught:
            default_metrics_reader(result)
        self.assertEqual(caught.exception.code, "unrecognized_result_shape")
        self.assertFalse(caught.exception.denied)

    def test_unknown_shape_fails_instead_of_guessing(self) -> None:
        for result in (
            {},
            {"structuredContent": 3},
            {"structuredContent": {"metrics": "日活"}},
            {"content": "文本"},
            {"content": [1, 2]},
        ):
            with self.subTest(result=result):
                with self.assertRaises(McpProbeError):
                    default_metrics_reader(result)

    def test_an_injected_reader_can_widen_it_after_l4a(self) -> None:
        """收紧不是死路：实测出真实形状后，装配层注入一个**已验证的** reader 即可。"""

        probe, _ = _probe(
            _echo({"result": {"structuredContent": {"indicators": ["a", "b", "c"]}}}),
            metrics_reader=lambda result: len(result["structuredContent"]["indicators"]),
        )
        self.assertEqual(probe.list_metrics(user_id=USER), 3)

    def test_injected_reader_result_is_validated(self) -> None:
        """注入的 reader 返回垃圾同样落技术失败，不放行。"""

        for value in (-1, "3", None, True):
            with self.subTest(value=repr(value)):
                probe, _ = _probe(_echo({"result": {}}), metrics_reader=lambda _r, v=value: v)
                with self.assertRaises(McpProbeError) as caught:
                    probe.list_metrics(user_id=USER)
                self.assertEqual(caught.exception.code, "invalid_metric_count")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
