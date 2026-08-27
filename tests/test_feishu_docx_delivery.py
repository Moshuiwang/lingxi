"""``adapters.feishu_docx_delivery`` 的契约测试（Issue #341 S-ES-1）。

传输层全部注入，本文件**不发起任何真实网络请求**——真实链路已由 #341 S0 探针
（2026-08-27，四步全通，证据等级 6）验证过，本文件只钉住"调用形态与飞书契约
逐字一致"这件事，理由同 ``tests/test_feishu_tenant_token.py`` 的模块文档。

请求路径与字段形状取自派工卡登记的 S0 探针实测结果：

- 建文档：``POST /docx/v1/documents`` ``{"title": ...}`` → ``data.document.document_id``
- 写正文：``POST /docx/v1/documents/{document_id}/blocks/{document_id}/children``
  ``{"children": [...], "index": 0}``，每段 ``{"block_type": 2, "text":
  {"elements": [{"text_run": {"content": ...}}]}}``
- 授权：``POST /drive/v1/permissions/{document_id}/members?type=docx``
  ``{"member_type": "openid", "member_id": ..., "perm": "full_access"}``
- 读回：``GET /drive/v1/permissions/{document_id}/members?type=docx``
"""

from __future__ import annotations

import unittest
from urllib.error import URLError

from lingxi.adapters.feishu_docx_delivery import (
    FeishuDocxDeliveryError,
    LarkDocxDelivery,
    urllib_transport,
)

BASE_URL = "https://feishu.invalid/open-apis"
TENANT_DOMAIN = "gv3qfk4q2rp.feishu.cn"
FAKE_TOKEN = "t-fake-tenant-access-token"
DOCUMENT_ID = "NPqsd9sQKot4fExGvK6cLyzBnfc"
OPEN_ID = "ou_target_user"


class RecordingTransport:
    """按顺序返回预置响应，并记录每次调用（同 ``test_feishu_tenant_token.py``
    的假传输层形状）。"""

    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, object] | None, str | None]] = []

    def __call__(self, method: str, url: str, *, body=None, token=None):
        self.calls.append((method, url, body, token))
        if not self._responses:
            raise AssertionError("假传输层收到了超出预置数量的调用")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _token_supply(token: str = FAKE_TOKEN):
    return lambda: token


def _client(transport, *, tenant_access_token=None) -> LarkDocxDelivery:
    return LarkDocxDelivery(
        base_url=BASE_URL,
        tenant_access_token=tenant_access_token or _token_supply(),
        tenant_domain=TENANT_DOMAIN,
        transport=transport,
    )


class ClientConstructionTest(unittest.TestCase):
    def test_a_non_https_base_url_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            LarkDocxDelivery(
                base_url="http://feishu.invalid/open-apis",
                tenant_access_token=_token_supply(),
                tenant_domain=TENANT_DOMAIN,
            )

    def test_a_non_callable_token_supply_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            LarkDocxDelivery(base_url=BASE_URL, tenant_access_token=FAKE_TOKEN, tenant_domain=TENANT_DOMAIN)  # type: ignore[arg-type]

    def test_tenant_domain_must_be_a_bare_domain(self) -> None:
        for bad_domain in ("", "   ", "https://gv3qfk4q2rp.feishu.cn", "gv3qfk4q2rp.feishu.cn/docx", "has space.feishu.cn"):
            with self.subTest(bad_domain=bad_domain):
                with self.assertRaises(ValueError):
                    LarkDocxDelivery(base_url=BASE_URL, tenant_access_token=_token_supply(), tenant_domain=bad_domain)


class CreateDocumentTest(unittest.TestCase):
    def test_a_successful_create_returns_the_document_id_and_matches_the_probed_shape(self) -> None:
        transport = RecordingTransport([{"code": 0, "data": {"document": {"document_id": DOCUMENT_ID, "revision_id": 1}}}])
        client = _client(transport)

        document_id = client.create_document("本月销售分析")

        self.assertEqual(document_id, DOCUMENT_ID)
        self.assertEqual(len(transport.calls), 1)
        method, url, body, token = transport.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, f"{BASE_URL}/docx/v1/documents")
        self.assertEqual(body, {"title": "本月销售分析"})
        self.assertEqual(token, FAKE_TOKEN)

    def test_an_empty_title_is_rejected_before_any_call(self) -> None:
        transport = RecordingTransport([])
        client = _client(transport)

        with self.assertRaises(ValueError):
            client.create_document("   ")

        self.assertEqual(transport.calls, [])

    def test_a_feishu_business_error_code_is_rejected_and_definite(self) -> None:
        transport = RecordingTransport([{"code": 1770001, "msg": "invalid title"}])
        client = _client(transport)

        with self.assertRaises(FeishuDocxDeliveryError) as raised:
            client.create_document("本月销售分析")

        self.assertEqual(raised.exception.code, "feishu_code_1770001")
        self.assertTrue(raised.exception.definite)
        self.assertNotIn("本月销售分析", str(raised.exception))

    def test_a_success_response_missing_document_id_is_a_lookup_error_not_a_silent_success(self) -> None:
        """结果不明：飞书说成功（``code=0``）但拿不到可回读标识——这不是
        ``FeishuDocxDeliveryError``（那是"明确拒绝"），必须是 ``LookupError``。

        变异锚点：把 ``create_document`` 里"检查 ``document_id`` 是否存在"这条
        判据删掉、直接 ``return document.get("document_id")``，本用例会从抛出
        ``LookupError`` 变红成返回 ``None``。
        """

        transport = RecordingTransport([{"code": 0, "data": {"document": {"revision_id": 1}}}])
        client = _client(transport)

        with self.assertRaises(LookupError):
            client.create_document("本月销售分析")

    def test_a_success_response_missing_the_document_field_is_a_lookup_error(self) -> None:
        transport = RecordingTransport([{"code": 0, "data": {}}])
        client = _client(transport)

        with self.assertRaises(LookupError):
            client.create_document("本月销售分析")


class WriteParagraphsTest(unittest.TestCase):
    def test_multiple_paragraphs_become_multiple_children_blocks_in_one_call(self) -> None:
        transport = RecordingTransport([{"code": 0, "data": {}}])
        client = _client(transport)

        client.write_paragraphs(DOCUMENT_ID, ["第一段正文", "第二段正文"])

        self.assertEqual(len(transport.calls), 1)
        method, url, body, token = transport.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, f"{BASE_URL}/docx/v1/documents/{DOCUMENT_ID}/blocks/{DOCUMENT_ID}/children")
        self.assertEqual(
            body,
            {
                "children": [
                    {"block_type": 2, "text": {"elements": [{"text_run": {"content": "第一段正文"}}]}},
                    {"block_type": 2, "text": {"elements": [{"text_run": {"content": "第二段正文"}}]}},
                ],
                "index": 0,
            },
        )
        self.assertEqual(token, FAKE_TOKEN)

    def test_a_single_paragraph_is_a_single_child_block(self) -> None:
        transport = RecordingTransport([{"code": 0, "data": {}}])
        client = _client(transport)

        client.write_paragraphs(DOCUMENT_ID, ["只有一段"])

        _, _, body, _ = transport.calls[0]
        self.assertEqual(len(body["children"]), 1)
        self.assertEqual(body["index"], 0)

    def test_an_empty_paragraph_sequence_is_rejected_before_any_call(self) -> None:
        transport = RecordingTransport([])
        client = _client(transport)

        with self.assertRaises(ValueError):
            client.write_paragraphs(DOCUMENT_ID, [])

        self.assertEqual(transport.calls, [])

    def test_a_blank_paragraph_is_rejected_before_any_call(self) -> None:
        transport = RecordingTransport([])
        client = _client(transport)

        with self.assertRaises(ValueError):
            client.write_paragraphs(DOCUMENT_ID, ["正常的一段", "   "])

        self.assertEqual(transport.calls, [])

    def test_a_blank_document_id_is_rejected_before_any_call(self) -> None:
        transport = RecordingTransport([])
        client = _client(transport)

        with self.assertRaises(ValueError):
            client.write_paragraphs("   ", ["一段正文"])

        self.assertEqual(transport.calls, [])

    def test_a_feishu_business_error_code_is_rejected(self) -> None:
        """变异锚点（派工卡指定）：把 ``_data`` 里"非 0 code 抛错"这条判据改成
        静默（例如直接 ``return data`` 不检查 ``code``），本用例会从抛出
        ``FeishuDocxDeliveryError`` 变红成静默返回——**已实测确认变红**：本地
        临时把 ``_data`` 的 ``if code not in (None, 0, "0"): raise ...`` 分支
        注释掉，本用例（以及 ``CreateDocumentTest``/``GrantFullAccessTest`` 的
        同类用例）全部由绿变红；验证后已还原，不留在正式代码里。
        """

        transport = RecordingTransport([{"code": 99991400, "msg": "rate limited"}])
        client = _client(transport)

        with self.assertRaises(FeishuDocxDeliveryError) as raised:
            client.write_paragraphs(DOCUMENT_ID, ["一段正文"])

        self.assertEqual(raised.exception.code, "feishu_code_99991400")
        self.assertNotIn("一段正文", str(raised.exception))


class GrantFullAccessTest(unittest.TestCase):
    def test_a_successful_grant_matches_the_probed_shape(self) -> None:
        transport = RecordingTransport([{"code": 0, "data": {}}])
        client = _client(transport)

        client.grant_full_access(DOCUMENT_ID, OPEN_ID)

        self.assertEqual(len(transport.calls), 1)
        method, url, body, token = transport.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, f"{BASE_URL}/drive/v1/permissions/{DOCUMENT_ID}/members?type=docx")
        self.assertEqual(body, {"member_type": "openid", "member_id": OPEN_ID, "perm": "full_access"})
        self.assertEqual(token, FAKE_TOKEN)

    def test_the_granted_perm_is_exactly_full_access_not_edit(self) -> None:
        """派工卡指定的变异锚点之一：把源码里的授权档位从 ``full_access`` 改成
        ``edit``。**已实测确认变红**：本地临时把
        ``LarkDocxDelivery.grant_full_access`` 里的 ``FULL_ACCESS_PERM`` 换成
        字面量 ``"edit"``，本用例从绿变红（``body["perm"]`` 断言失败）；验证后
        已还原，不留在正式代码里。「可管理」是决策记录 2026-08-23 裁定的唯一
        授予档位，降级成 ``edit`` 属于产品承诺被静默削弱，必须被测试挡住。
        """

        transport = RecordingTransport([{"code": 0, "data": {}}])
        client = _client(transport)

        client.grant_full_access(DOCUMENT_ID, OPEN_ID)

        _, _, body, _ = transport.calls[0]
        self.assertEqual(body["perm"], "full_access")

    def test_an_invalid_open_id_is_rejected_before_any_call_and_not_echoed(self) -> None:
        transport = RecordingTransport([])
        client = _client(transport)

        with self.assertRaises(ValueError) as raised:
            client.grant_full_access(DOCUMENT_ID, "oc_this_is_a_group_chat_id")

        self.assertEqual(transport.calls, [])
        self.assertNotIn("oc_this_is_a_group_chat_id", str(raised.exception))

    def test_a_feishu_business_error_code_is_rejected(self) -> None:
        transport = RecordingTransport([{"code": 99991672, "msg": "forbidden"}])
        client = _client(transport)

        with self.assertRaises(FeishuDocxDeliveryError) as raised:
            client.grant_full_access(DOCUMENT_ID, OPEN_ID)

        self.assertEqual(raised.exception.code, "feishu_code_99991672")
        self.assertTrue(raised.exception.definite)


class ReadMembersTest(unittest.TestCase):
    def test_a_successful_read_matches_the_probed_shape_and_reports_full_access(self) -> None:
        transport = RecordingTransport(
            [
                {
                    "code": 0,
                    "data": {
                        "members": [
                            {"member_type": "openid", "member_id": OPEN_ID, "perm": "full_access", "extra": "ignored"},
                            {"member_type": "app", "member_id": "cli_fake_app", "perm": "owner"},
                        ]
                    },
                }
            ]
        )
        client = _client(transport)

        members = client.read_members(DOCUMENT_ID)

        self.assertEqual(len(transport.calls), 1)
        method, url, body, token = transport.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, f"{BASE_URL}/drive/v1/permissions/{DOCUMENT_ID}/members?type=docx")
        self.assertIsNone(body)
        self.assertEqual(token, FAKE_TOKEN)

        target = next(m for m in members if m["member_id"] == OPEN_ID)
        self.assertEqual(target["member_type"], "openid")
        self.assertEqual(target["perm"], "full_access")
        self.assertNotIn("extra", target)

    def test_a_feishu_business_error_code_is_rejected(self) -> None:
        transport = RecordingTransport([{"code": 99991400, "msg": "rate limited"}])
        client = _client(transport)

        with self.assertRaises(FeishuDocxDeliveryError) as raised:
            client.read_members(DOCUMENT_ID)

        self.assertEqual(raised.exception.code, "feishu_code_99991400")

    def test_a_success_response_missing_members_is_a_lookup_error(self) -> None:
        transport = RecordingTransport([{"code": 0, "data": {}}])
        client = _client(transport)

        with self.assertRaises(LookupError):
            client.read_members(DOCUMENT_ID)


class DocumentUrlTest(unittest.TestCase):
    def test_the_url_is_built_from_the_configured_tenant_domain_with_no_network_call(self) -> None:
        transport = RecordingTransport([])
        client = _client(transport)

        url = client.document_url(DOCUMENT_ID)

        self.assertEqual(url, f"https://{TENANT_DOMAIN}/docx/{DOCUMENT_ID}")
        self.assertEqual(transport.calls, [])

    def test_a_blank_document_id_is_rejected(self) -> None:
        transport = RecordingTransport([])
        client = _client(transport)

        with self.assertRaises(ValueError):
            client.document_url("   ")


class TokenSupplyTest(unittest.TestCase):
    def test_the_token_supply_is_consulted_on_every_call_and_never_appears_in_the_url(self) -> None:
        calls = {"count": 0}

        def supply() -> str:
            calls["count"] += 1
            return f"{FAKE_TOKEN}-{calls['count']}"

        transport = RecordingTransport(
            [
                {"code": 0, "data": {"document": {"document_id": DOCUMENT_ID}}},
                {"code": 0, "data": {}},
            ]
        )
        client = _client(transport, tenant_access_token=supply)

        client.create_document("标题一")
        client.write_paragraphs(DOCUMENT_ID, ["正文"])

        self.assertEqual(calls["count"], 2)
        tokens = [call[3] for call in transport.calls]
        self.assertEqual(tokens, [f"{FAKE_TOKEN}-1", f"{FAKE_TOKEN}-2"])
        for _, url, _, _ in transport.calls:
            self.assertNotIn(FAKE_TOKEN, url)

    def test_a_missing_token_from_the_supply_is_rejected_without_calling_the_transport(self) -> None:
        transport = RecordingTransport([])
        client = _client(transport, tenant_access_token=lambda: "")

        with self.assertRaises(FeishuDocxDeliveryError) as raised:
            client.create_document("标题")

        self.assertFalse(raised.exception.definite)
        self.assertEqual(transport.calls, [])


class DefaultTransportTest(unittest.TestCase):
    """默认传输层的错误分类（不发真实请求，注入的是 ``urlopen`` 会抛出的异常
    类型），同 ``test_feishu_tenant_token.py`` 的 ``DefaultTransportTest``。"""

    def test_a_transport_error_is_classified_as_indefinite(self) -> None:
        import lingxi.adapters.feishu_docx_delivery as module

        original = module.urlopen

        def boom(*_args, **_kwargs):
            raise URLError("boom")

        module.urlopen = boom
        try:
            with self.assertRaises(FeishuDocxDeliveryError) as raised:
                urllib_transport("POST", f"{BASE_URL}/docx/v1/documents", body={"title": "x"}, token=FAKE_TOKEN)
            self.assertEqual(raised.exception.code, "transport_error")
            self.assertFalse(raised.exception.definite)
        finally:
            module.urlopen = original


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
