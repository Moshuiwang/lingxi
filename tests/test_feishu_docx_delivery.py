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

正文写入那一步的形状取自 Trace #544 S-7c 的 stage 受控探针（2026-09-03，
Bot-Test 真实调用，14 篇受控文档用后全删并逐篇回读确认）：

- 一次建档写全文：``POST /docs_ai/v1/documents``
  ``{"format": "markdown", "content": "<title>标题</title>\n\n正文"}``
  → ``data.document.{document_id, revision_id, url}``，可选 ``data.result``
  与 ``data.warnings``
"""

from __future__ import annotations

import io
import unittest
from urllib.error import HTTPError, URLError

from docx_body_sample import COMPREHENSIVE_MARKDOWN, COMPREHENSIVE_MARKDOWN_SHAPES

from lingxi.adapters.feishu_docx_delivery import (
    BODY_TOO_LONG,
    DOCS_AI_RESULT_FAILED,
    MAX_MARKDOWN_CHARS,
    PRE_FLIGHT_DEGRADE_REASONS,
    SERVER_SIMPLIFIED_BODY,
    TITLE_NOT_EMBEDDABLE,
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


def _client(transport, *, tenant_access_token=None, markdown_convert_enabled: bool = False) -> LarkDocxDelivery:
    return LarkDocxDelivery(
        base_url=BASE_URL,
        tenant_access_token=tenant_access_token or _token_supply(),
        tenant_domain=TENANT_DOMAIN,
        transport=transport,
        markdown_convert_enabled=markdown_convert_enabled,
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
    def test_a_successful_read_matches_the_real_shape_and_reports_full_access(self) -> None:
        """真实形状（编排者 2026-08-27 stage 真实调用实测，见 ``read_members``
        文档字符串）：协作者数组在 ``data.items``，不是 ``data.members``；每一项
        额外带 ``perm_type`` 字段（本方法只保留 ``member_type``/``member_id``/
        ``perm`` 三个字段，``perm_type`` 与其它任何多余字段一样被丢弃）。

        变异锚点：把 ``read_members`` 里 ``data.get("items")`` 这一行删掉（只留
        ``data.get("members")``），本用例从返回目标协作者变红成
        ``LookupError``——这正是 2026-08-27 stage 自测坐实的真实故障：四步全成功
        后读回必然判定结果不明。
        """

        transport = RecordingTransport(
            [
                {
                    "code": 0,
                    "data": {
                        "items": [
                            {
                                "member_type": "openid",
                                "member_id": OPEN_ID,
                                "perm": "full_access",
                                "perm_type": "container",
                            },
                            {"member_type": "app", "member_id": "cli_fake_app", "perm": "full_access", "perm_type": "container"},
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
        self.assertNotIn("perm_type", target)

    def test_a_legacy_members_shaped_response_is_still_accepted_as_a_degraded_fallback(self) -> None:
        """降级读：``items`` 缺失但 ``members`` 存在时仍要能读出协作者——不是当前
        真实形状（见上一条用例），只是不排除历史响应或未来回归会回到这个形状，
        兼容读不应该让调用方多做一次判断。"""

        transport = RecordingTransport(
            [
                {
                    "code": 0,
                    "data": {
                        "members": [
                            {"member_type": "openid", "member_id": OPEN_ID, "perm": "full_access"},
                        ]
                    },
                }
            ]
        )
        client = _client(transport)

        members = client.read_members(DOCUMENT_ID)

        target = next(m for m in members if m["member_id"] == OPEN_ID)
        self.assertEqual(target["perm"], "full_access")

    def test_a_feishu_business_error_code_is_rejected(self) -> None:
        transport = RecordingTransport([{"code": 99991400, "msg": "rate limited"}])
        client = _client(transport)

        with self.assertRaises(FeishuDocxDeliveryError) as raised:
            client.read_members(DOCUMENT_ID)

        self.assertEqual(raised.exception.code, "feishu_code_99991400")

    def test_a_success_response_missing_items_and_members_is_a_lookup_error(self) -> None:
        transport = RecordingTransport([{"code": 0, "data": {}}])
        client = _client(transport)

        with self.assertRaises(LookupError):
            client.read_members(DOCUMENT_ID)


class ReadBodyChildrenTest(unittest.TestCase):
    """Issue #353 幂等判据新增方法：读回正文根 block 的现有子块。"""

    def test_a_successful_read_returns_the_existing_children(self) -> None:
        transport = RecordingTransport(
            [
                {
                    "code": 0,
                    "data": {
                        "items": [
                            {"block_id": "blk-1", "block_type": 2, "text": {"elements": []}},
                        ]
                    },
                }
            ]
        )
        client = _client(transport)

        children = client.read_body_children(DOCUMENT_ID)

        self.assertEqual(len(transport.calls), 1)
        method, url, body, token = transport.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, f"{BASE_URL}/docx/v1/documents/{DOCUMENT_ID}/blocks/{DOCUMENT_ID}/children")
        self.assertIsNone(body)
        self.assertEqual(token, FAKE_TOKEN)
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]["block_id"], "blk-1")

    def test_a_freshly_created_document_has_no_children(self) -> None:
        """判据的正常起点：从未写过正文的文档，根 block 的子块应当是空列表。"""

        transport = RecordingTransport([{"code": 0, "data": {"items": []}}])
        client = _client(transport)

        self.assertEqual(client.read_body_children(DOCUMENT_ID), [])

    def test_a_blank_document_id_is_rejected_before_any_call(self) -> None:
        transport = RecordingTransport([])
        client = _client(transport)

        with self.assertRaises(ValueError):
            client.read_body_children("   ")

        self.assertEqual(transport.calls, [])

    def test_a_feishu_business_error_code_is_rejected(self) -> None:
        transport = RecordingTransport([{"code": 99991400, "msg": "rate limited"}])
        client = _client(transport)

        with self.assertRaises(FeishuDocxDeliveryError) as raised:
            client.read_body_children(DOCUMENT_ID)

        self.assertEqual(raised.exception.code, "feishu_code_99991400")

    def test_a_success_response_missing_items_is_a_lookup_error(self) -> None:
        """结果不明：响应成功但没有可回读的 ``items`` 字段——不能当成"确定为空"，
        否则会把"读不出真实状态"悄悄误判成"正文没写过"，让恢复路径继续重驱写
        正文，正是这条判据要防的洞。

        变异锚点：把这里"检查 items 是否是 list"这条判据删掉、直接
        ``return data.get("items") or []``，本用例会从抛出 ``LookupError``
        变红成返回 ``[]``。
        """

        transport = RecordingTransport([{"code": 0, "data": {}}])
        client = _client(transport)

        with self.assertRaises(LookupError):
            client.read_body_children(DOCUMENT_ID)


class CreateDocumentWithMarkdownTest(unittest.TestCase):
    """服务端一次建档写全文（Trace #544 S-7c）。

    形状取自 2026-09-03 stage 受控探针实测（Bot-Test 真实调用，受控文档用后
    删除并逐篇回读确认）：``POST /open-apis/docs_ai/v1/documents``，请求体
    ``{"format": "markdown", "content": "<title>…</title>\\n\\n正文"}``，响应
    ``data.document = {document_id, revision_id, url}``。

    变异锚点（改坏其一，对应用例必须变红）：

    - 把 :data:`MAX_MARKDOWN_CHARS` 的前置守卫整段删掉（超长正文直接送去撞
      504）→ ``test_a_body_over_the_length_guard_degrades_before_any_call`` 红；
    - 把 :func:`_degraded_reason` 的降级判据**只绑在 ``warnings`` 上**（删掉
      ``result`` 那两条分支）→
      ``test_partial_success_without_warnings_is_still_reported_as_degraded`` 与
      ``test_an_unregistered_result_value_is_reported_as_degraded`` 红；
    - 把默认传输层对 HTTP 5xx 的"不解析响应体、判结果不明"改回照常解析（于是
      响应体里的 ``code=2200`` 被当成飞书明确拒绝）→
      ``test_a_gateway_timeout_is_result_unknown_and_the_body_code_is_not_trusted``
      红。
    """

    def _response(self, **data: object) -> dict[str, object]:
        document = {"document_id": DOCUMENT_ID, "revision_id": 3, "url": f"https://{TENANT_DOMAIN}/docx/{DOCUMENT_ID}"}
        return {"code": 0, "msg": "", "data": {"document": document, **data}}

    def test_the_comprehensive_sample_is_delivered_in_exactly_one_call_with_the_probed_shape(self) -> None:
        """综合样本一次建档成功：**一次**调用、请求形态与探针实测逐字一致、
        正文一个字符没被改写。

        用 ``tests/docx_body_sample.py`` 的共用夹具而不是就地造一段 markdown：
        同一份样本同时服务 stage 真实回读与本地用例，换写入路径也不用搬家。
        """

        transport = RecordingTransport([self._response()])
        client = _client(transport, markdown_convert_enabled=True)

        created = client.create_document_with_markdown("2026 年 8 月经营简报", COMPREHENSIVE_MARKDOWN)

        self.assertEqual(created.document_id, DOCUMENT_ID)
        self.assertIsNone(created.degraded_reason)
        self.assertFalse(created.degraded)
        self.assertEqual(len(transport.calls), 1, "一次建档必须只发一次调用，不得再有 convert/写块调用")
        method, url, body, token = transport.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, f"{BASE_URL}/docs_ai/v1/documents")
        self.assertEqual(token, FAKE_TOKEN)
        assert body is not None
        self.assertEqual(set(body), {"format", "content"}, "探针实测不传 parent_token：落点与老路径本来就一致")
        self.assertEqual(body["format"], "markdown")
        content = body["content"]
        assert isinstance(content, str)
        self.assertTrue(content.startswith("<title>2026 年 8 月经营简报</title>\n\n"))
        self.assertTrue(content.endswith(COMPREHENSIVE_MARKDOWN))
        # 十八条形态一条都不能在拼接过程中丢失或被改写。
        for name, snippet in COMPREHENSIVE_MARKDOWN_SHAPES.items():
            self.assertIn(snippet, content, f"综合样本的「{name}」没有原样送出去")
        # 负号 / 区间 / 竖线：本仓库 2026-08-29 那次数据正确性缺陷的直接回归面。
        for verbatim in ("-12.85%", "3-5%", "+4.20%", "6-8%"):
            self.assertIn(verbatim, content)

    def test_a_clean_success_without_a_result_key_is_not_reported_as_degraded(self) -> None:
        """探针二实测：干净成功时 ``data`` 只有 ``document`` 一个键——"``result``
        键不存在"就是零告警，不能被当成"拿不准所以报降级"。"""

        transport = RecordingTransport([{"code": 0, "data": {"document": {"document_id": DOCUMENT_ID}}}])
        client = _client(transport, markdown_convert_enabled=True)

        created = client.create_document_with_markdown("标题", "正文")

        self.assertIsNone(created.degraded_reason)

    def test_partial_success_without_warnings_is_still_reported_as_degraded(self) -> None:
        """``result="partial_success"`` 且**没有** ``warnings``：必须报降级。

        这条就是"降级信号只绑在 ``warnings`` 上"这个变异的红灯——探针实测原始
        HTML 块被静默丢弃且不产生 warning，只看 warnings 会漏报。
        """

        transport = RecordingTransport([self._response(result="partial_success")])
        client = _client(transport, markdown_convert_enabled=True)

        created = client.create_document_with_markdown("标题", "正文")

        self.assertEqual(created.degraded_reason, SERVER_SIMPLIFIED_BODY)
        self.assertTrue(created.degraded)
        self.assertEqual(created.document_id, DOCUMENT_ID, "降级不是失败：文档已经建好，仍然要交付")

    def test_warnings_are_reported_as_degraded_even_when_result_says_success(self) -> None:
        """``warnings`` 非空时以警告为准，哪怕 ``result`` 自称 ``success``。"""

        transport = RecordingTransport(
            [self._response(result="success", warnings=["degrade_code=2108,msg=table simplified"])]
        )
        client = _client(transport, markdown_convert_enabled=True)

        created = client.create_document_with_markdown("标题", "正文")

        self.assertEqual(created.degraded_reason, SERVER_SIMPLIFIED_BODY)

    def test_an_empty_warnings_list_is_not_reported_as_degraded(self) -> None:
        """空数组不是降级——"这个键在但没内容"与"真的有警告"必须分开。"""

        transport = RecordingTransport([self._response(result="success", warnings=[])])
        client = _client(transport, markdown_convert_enabled=True)

        self.assertIsNone(client.create_document_with_markdown("标题", "正文").degraded_reason)

    def test_an_unregistered_result_value_is_reported_as_degraded(self) -> None:
        """未登记的 ``result`` 取值倒向"多说一句"，不当成干净成功。"""

        transport = RecordingTransport([self._response(result="partially_degraded_v2")])
        client = _client(transport, markdown_convert_enabled=True)

        self.assertEqual(
            client.create_document_with_markdown("标题", "正文").degraded_reason, SERVER_SIMPLIFIED_BODY
        )

    def test_result_failed_is_a_definite_failure_and_reports_no_document(self) -> None:
        """``result="failed"``：服务端说失败就是失败，不猜"也许文档其实建出来
        了"，也绝不把一个可能不完整的 ``document_id`` 交出去。"""

        transport = RecordingTransport([self._response(result="failed")])
        client = _client(transport, markdown_convert_enabled=True)

        with self.assertRaises(FeishuDocxDeliveryError) as raised:
            client.create_document_with_markdown("标题", "正文")

        self.assertEqual(raised.exception.code, DOCS_AI_RESULT_FAILED)
        self.assertTrue(raised.exception.definite)

    def test_a_body_over_the_length_guard_degrades_before_any_call(self) -> None:
        """**长度前置守卫**：超长正文在发出任何请求之前就判 ``body_too_long``。

        这道守卫的存在理由是探针实测的那次 504——200 000 字符的建档超时了，
        **而服务端其实已经把整篇文档建出来了**，调用方拿不到它的 id。与其撞上
        那种"结果不明且不可回读"，不如提前改走两步段落路径并明示降级。
        """

        transport = RecordingTransport([])
        client = _client(transport, markdown_convert_enabled=True)

        with self.assertRaises(FeishuDocxDeliveryError) as raised:
            client.create_document_with_markdown("标题", "正" * (MAX_MARKDOWN_CHARS + 1))

        self.assertEqual(raised.exception.code, BODY_TOO_LONG)
        self.assertTrue(raised.exception.definite)
        self.assertIn(BODY_TOO_LONG, PRE_FLIGHT_DEGRADE_REASONS)
        self.assertEqual(transport.calls, [], "守卫必须在发出请求之前判定，否则等于照样撞 504")

    def test_a_body_exactly_at_the_length_guard_is_still_sent(self) -> None:
        """边界：恰好等于阈值仍然走一次建档（守卫是"超过才拦"，不是"接近就拦"）。"""

        title = "标题"
        overhead = len(f"<title>{title}</title>\n\n")
        transport = RecordingTransport([self._response()])
        client = _client(transport, markdown_convert_enabled=True)

        client.create_document_with_markdown(title, "正" * (MAX_MARKDOWN_CHARS - overhead))

        self.assertEqual(len(transport.calls), 1)
        body = transport.calls[0][2]
        assert body is not None
        content = body["content"]
        assert isinstance(content, str)
        self.assertEqual(len(content), MAX_MARKDOWN_CHARS)

    def test_a_title_with_angle_brackets_degrades_before_any_call(self) -> None:
        """标题是拼在正文最前面的一个标签、不是独立字段：尖括号会破坏标签边界。
        **不静默转义、不剥字符**（那正是 2026-08-29 裁定停止的那类改写），改走
        两步路径——那条路上标题走 JSON 字段，用户拿到的标题逐字完整。"""

        transport = RecordingTransport([])
        client = _client(transport, markdown_convert_enabled=True)

        for title in ("营收 < 10 万的公司", "口径 </title> 说明"):
            with self.subTest(title=title):
                with self.assertRaises(FeishuDocxDeliveryError) as raised:
                    client.create_document_with_markdown(title, "正文")
                self.assertEqual(raised.exception.code, TITLE_NOT_EMBEDDABLE)
                self.assertIn(TITLE_NOT_EMBEDDABLE, PRE_FLIGHT_DEGRADE_REASONS)
        self.assertEqual(transport.calls, [])

    def test_an_empty_title_or_body_is_rejected_before_any_call(self) -> None:
        transport = RecordingTransport([])
        client = _client(transport, markdown_convert_enabled=True)

        with self.assertRaises(ValueError):
            client.create_document_with_markdown("   ", "正文")
        with self.assertRaises(ValueError):
            client.create_document_with_markdown("标题", "   ")
        self.assertEqual(transport.calls, [])

    def test_a_feishu_business_error_code_is_definite(self) -> None:
        transport = RecordingTransport([{"code": 1770001, "msg": "invalid param"}])
        client = _client(transport, markdown_convert_enabled=True)

        with self.assertRaises(FeishuDocxDeliveryError) as raised:
            client.create_document_with_markdown("标题", "正文")

        self.assertEqual(raised.exception.code, "feishu_code_1770001")
        self.assertTrue(raised.exception.definite)

    def test_a_success_response_missing_document_id_is_a_lookup_error(self) -> None:
        """成功响应缺可回读标识 = 结果不明，不能静默当成成功（文档可能真的建
        出来了，只是我们没拿到 id——与那次 504 同一类）。"""

        transport = RecordingTransport([{"code": 0, "data": {"document": {"revision_id": 3}}}])
        client = _client(transport, markdown_convert_enabled=True)

        with self.assertRaises(LookupError):
            client.create_document_with_markdown("标题", "正文")

    def test_the_kill_switch_is_exposed_as_a_read_only_property(self) -> None:
        """止损闸（``LINGXI_DOCX_MARKDOWN_CONVERT``）由调用方读，不由本类在
        建档方法里抛原因码表达——开关关闭时走段落路径**不是降级**。"""

        transport = RecordingTransport([])
        self.assertFalse(_client(transport).markdown_convert_enabled)
        self.assertTrue(_client(transport, markdown_convert_enabled=True).markdown_convert_enabled)
        self.assertEqual(transport.calls, [])


class GatewayTimeoutTest(unittest.TestCase):
    """**超时/5xx 一律判结果不明，永不重试建档**（Trace #544 S-7c 实测）。

    探针实测：一次超长正文建档返回 HTTP 504 ＋ 响应体 ``{"code": 2200, "msg":
    "Gateway timeout. Please try again later."}``，而**服务端其实已经把整篇
    文档建出来了**、调用方拿不到 ``document_id``。如果照常解析响应体，那个非 0
    的业务码会把它伪装成"飞书明确拒绝"（definite），上层据此落 ``failed``——
    与真实世界相反，而且掩盖了"可能已经建好一篇完整文档"这件事。
    """

    @staticmethod
    def _raise_http_error(status: int, payload: bytes):
        def boom(*_args, **_kwargs):
            raise HTTPError(f"{BASE_URL}/docs_ai/v1/documents", status, "Gateway Timeout", {}, io.BytesIO(payload))

        return boom

    def _with_urlopen(self, replacement):
        import lingxi.adapters.feishu_docx_delivery as module

        original = module.urlopen
        module.urlopen = replacement
        self.addCleanup(lambda: setattr(module, "urlopen", original))

    def test_a_gateway_timeout_is_result_unknown_and_the_body_code_is_not_trusted(self) -> None:
        self._with_urlopen(
            self._raise_http_error(504, b'{"code": 2200, "msg": "Gateway timeout. Please try again later."}')
        )

        with self.assertRaises(FeishuDocxDeliveryError) as raised:
            urllib_transport(
                "POST", f"{BASE_URL}/docs_ai/v1/documents", body={"format": "markdown"}, token=FAKE_TOKEN
            )

        self.assertEqual(raised.exception.code, "http_504")
        self.assertFalse(
            raised.exception.definite,
            "504 不证明请求没有生效：判 definite 会让上层把一次可能已经建好文档的调用记成确定性失败",
        )

    def test_a_socket_timeout_is_result_unknown(self) -> None:
        def boom(*_args, **_kwargs):
            raise TimeoutError("timed out")

        self._with_urlopen(boom)

        with self.assertRaises(FeishuDocxDeliveryError) as raised:
            urllib_transport("POST", f"{BASE_URL}/docs_ai/v1/documents", body={}, token=FAKE_TOKEN)

        self.assertEqual(raised.exception.code, "transport_error")
        self.assertFalse(raised.exception.definite)

    def test_a_client_error_still_parses_the_business_code(self) -> None:
        """4xx 仍然照常解析：飞书的业务错误码走 2xx/4xx 返回，那是确定性拒绝。"""

        self._with_urlopen(self._raise_http_error(400, b'{"code": 1770001, "msg": "invalid param"}'))

        response = urllib_transport("POST", f"{BASE_URL}/docs_ai/v1/documents", body={}, token=FAKE_TOKEN)

        self.assertEqual(response, {"code": 1770001, "msg": "invalid param"})


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
