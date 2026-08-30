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
    MAX_CONVERTED_BLOCKS,
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


class ConvertMarkdownToBlocksTest(unittest.TestCase):
    """Issue #408 正式方案：官方 markdown→blocks 转换（默认关闭，见
    ``LarkDocxDelivery.write_body`` 与模块文档「markdown 官方转换开关」）。

    Issue #442（2026-08-30 受控探针实证）：真实响应 ``data.blocks`` 不是文档
    顺序，真实顺序由 ``data.first_level_block_ids`` 给出，另有一个与 blocks
    无关的 ``data.block_id_to_image_urls`` 键。本类的夹具直接采用探针实测形状
    （标题→两列表项→正文，``block_types`` 乱序 ``[12, 2, 3, 12]``，
    ``first_level_block_ids`` 正序）。
    """

    def test_blocks_are_reordered_by_first_level_block_ids_and_readonly_fields_are_stripped(
        self,
    ) -> None:
        """探针实测形状（Issue #442 正文登记的原始实测）：``blocks`` 数组
        物理顺序是乱的（``block_types`` 顺序 ``[12, 2, 3, 12]``），真实文档
        顺序（标题→列表项一→列表项二→正文）由 ``first_level_block_ids`` 给出。
        本夹具的物理顺序刻意与真实顺序不同（列表项一排在物理首位、标题排在
        物理第三位），确保"按 blocks 数组物理顺序返回"与"按
        first_level_block_ids 重排"在这个夹具上给出不同结果。同时验证：
        - 负号/连字符逐字保真（「-12.85%」「3-5%」不被改动）；
        - 只读字段（``block_id``/``parent_id``/``children``）在返回前被剔除；
        - 响应里与 blocks 无关的 ``block_id_to_image_urls`` 键被忽略、不误当块。

        变异锚点：把按 ``first_level_block_ids`` 重排这一步删掉、直接返回
        ``mapping_blocks`` 物理顺序，本用例会从"返回顺序=标题/列表1/列表2/
        正文"变红成"返回顺序=列表1/正文/标题/列表2"（与 ``blocks`` 数组物理
        顺序一致，而不是真实文档顺序）。
        """

        # 物理响应顺序（block_types = [12, 2, 3, 12]，探针实测形状）。
        item1_block = {
            "block_id": "blk-item1",
            "block_type": 12,
            "text": {"elements": [{"text_run": {"content": "列表项一 3-5%"}}]},
        }
        body_block = {
            "block_id": "blk-body",
            "parent_id": DOCUMENT_ID,
            "children": [],
            "block_type": 2,
            "text": {"elements": [{"text_run": {"content": "周环比 -12.85%"}}]},
        }
        heading_block = {
            "block_id": "blk-heading",
            "parent_id": DOCUMENT_ID,
            "block_type": 3,
            "text": {"elements": [{"text_run": {"content": "标题"}}]},
        }
        item2_block = {
            "block_id": "blk-item2",
            "block_type": 12,
            "text": {"elements": [{"text_run": {"content": "列表项二"}}]},
        }
        response_blocks = [item1_block, body_block, heading_block, item2_block]
        transport = RecordingTransport(
            [
                {
                    "code": 0,
                    "data": {
                        "blocks": response_blocks,
                        # 真实文档顺序：标题→列表项一→列表项二→正文。
                        "first_level_block_ids": [
                            "blk-heading",
                            "blk-item1",
                            "blk-item2",
                            "blk-body",
                        ],
                        "block_id_to_image_urls": {"blk-body": "https://example.invalid/img.png"},
                    },
                }
            ]
        )
        client = _client(transport)

        blocks = client.convert_markdown_to_blocks("# 标题\n\n- 列表项一 3-5%\n- 列表项二\n\n周环比 -12.85%")

        self.assertEqual(len(transport.calls), 1)
        method, url, body, token = transport.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, f"{BASE_URL}/docx/v1/documents/blocks/convert")
        self.assertEqual(token, FAKE_TOKEN)
        # 返回顺序必须等于 first_level_block_ids 声明的真实文档顺序。
        self.assertEqual(
            [block["text"]["elements"][0]["text_run"]["content"] for block in blocks],
            ["标题", "列表项一 3-5%", "列表项二", "周环比 -12.85%"],
        )
        # 只读字段（block_id/parent_id/children）必须被剔除。
        for block in blocks:
            self.assertNotIn("block_id", block)
            self.assertNotIn("parent_id", block)
            self.assertNotIn("children", block)
        self.assertEqual(blocks[0]["block_type"], 3)  # 标题
        self.assertEqual(blocks[1]["block_type"], 12)  # 列表项一
        self.assertEqual(blocks[2]["block_type"], 12)  # 列表项二
        self.assertEqual(blocks[3]["block_type"], 2)  # 正文

    def test_missing_first_level_block_ids_is_rejected_and_definite(self) -> None:
        """变异锚点：把"first_level_block_ids 缺失/为空 → definite 拒绝"这条
        判据删掉，本用例会从抛出 ``FeishuDocxDeliveryError`` 变红成静默按
        ``blocks`` 数组物理顺序返回（乱序交付）。
        """

        transport = RecordingTransport(
            [{"code": 0, "data": {"blocks": [{"block_id": "blk-1", "block_type": 2}]}}]
        )
        client = _client(transport)

        with self.assertRaises(FeishuDocxDeliveryError) as raised:
            client.convert_markdown_to_blocks("# 标题")

        self.assertTrue(raised.exception.definite)
        self.assertEqual(raised.exception.code, "markdown_convert_missing_first_level_block_ids")

    def test_empty_first_level_block_ids_is_rejected_and_definite(self) -> None:
        transport = RecordingTransport(
            [
                {
                    "code": 0,
                    "data": {
                        "blocks": [{"block_id": "blk-1", "block_type": 2}],
                        "first_level_block_ids": [],
                    },
                }
            ]
        )
        client = _client(transport)

        with self.assertRaises(FeishuDocxDeliveryError) as raised:
            client.convert_markdown_to_blocks("# 标题")

        self.assertTrue(raised.exception.definite)
        self.assertEqual(raised.exception.code, "markdown_convert_missing_first_level_block_ids")

    def test_a_block_outside_first_level_block_ids_is_rejected_as_unsupported_nested_blocks(
        self,
    ) -> None:
        """典型场景：含表格的 markdown——表格自身是一级块，出现在
        ``first_level_block_ids`` 里，但它的单元格作为独立元素出现在
        ``blocks`` 数组里、却不在 ``first_level_block_ids`` 内。本仓库当前
        不支持任何带嵌套结构的 markdown，必须 definite 拒绝、不做静默丢块。

        变异锚点：把"块必须在 first_level_block_ids 内"这条判据删掉，本用例
        会从抛出 ``FeishuDocxDeliveryError`` 变红成静默丢弃单元格块、只返回
        表格这一个块。
        """

        table_block = {"block_id": "blk-table", "block_type": 31}
        table_cell_block = {"block_id": "blk-cell", "block_type": 32}
        transport = RecordingTransport(
            [
                {
                    "code": 0,
                    "data": {
                        "blocks": [table_block, table_cell_block],
                        "first_level_block_ids": ["blk-table"],
                    },
                }
            ]
        )
        client = _client(transport)

        with self.assertRaises(FeishuDocxDeliveryError) as raised:
            client.convert_markdown_to_blocks("| a | b |\n| - | - |\n| 1 | 2 |")

        self.assertTrue(raised.exception.definite)
        self.assertEqual(raised.exception.code, "unsupported_nested_blocks")

    def test_a_block_missing_block_id_is_rejected_as_unsupported_nested_blocks(self) -> None:
        """块缺 ``block_id`` 时无法确认它是否属于一级块，同样不能静默丢弃或
        猜测归类，一律走与"嵌套结构"相同的 definite 拒绝分支。"""

        transport = RecordingTransport(
            [
                {
                    "code": 0,
                    "data": {
                        "blocks": [{"block_type": 2}],
                        "first_level_block_ids": ["blk-1"],
                    },
                }
            ]
        )
        client = _client(transport)

        with self.assertRaises(FeishuDocxDeliveryError) as raised:
            client.convert_markdown_to_blocks("正文")

        self.assertEqual(raised.exception.code, "unsupported_nested_blocks")

    def test_first_level_block_ids_referencing_an_unknown_block_is_a_lookup_error(self) -> None:
        """``first_level_block_ids`` 引用了一个在 ``blocks`` 数组里找不到的
        ``block_id``：响应内部不自洽，归类为结果不明，不是 definite 拒绝
        （同 :meth:`LarkDocxDelivery.read_body_children` 既有分类口径）。"""

        transport = RecordingTransport(
            [
                {
                    "code": 0,
                    "data": {
                        "blocks": [{"block_id": "blk-1", "block_type": 2}],
                        "first_level_block_ids": ["blk-1", "blk-missing"],
                    },
                }
            ]
        )
        client = _client(transport)

        with self.assertRaises(LookupError):
            client.convert_markdown_to_blocks("正文")

    def test_a_duplicate_block_id_in_mapping_blocks_is_rejected_as_a_count_mismatch(
        self,
    ) -> None:
        """rc21 修复包 B（P3 docx 对账，opus 审查发现）：``blocks`` 数组里出现
        两个块共用同一个 ``block_id``（``blk-1``）。建映射的字典推导式
        （``by_block_id = {block["block_id"]: block for block in
        mapping_blocks}``）会用后一个静默覆盖前一个——复现"重复 block_id
        静默丢块"：前一个块（这里是"第一段"）的内容会从返回结果里凭空消失，
        且不留任何痕迹。重排后的块数（2，等于 ``first_level_block_ids`` 的
        长度）与原始 ``mapping_blocks`` 块数（3）对不上，必须 definite 拒绝。

        变异存活证据：把 ``len(ordered_blocks) != len(mapping_blocks)`` 这条
        对账删掉，本用例会从抛出 `FeishuDocxDeliveryError` 变红成静默返回
        只含"第二段"（丢弃"第一段"）与"blk-2"两个块的结果。
        """

        transport = RecordingTransport(
            [
                {
                    "code": 0,
                    "data": {
                        "blocks": [
                            {"block_id": "blk-1", "block_type": 2, "text": "第一段"},
                            {"block_id": "blk-1", "block_type": 2, "text": "第二段"},
                            {"block_id": "blk-2", "block_type": 2, "text": "第三段"},
                        ],
                        "first_level_block_ids": ["blk-1", "blk-2"],
                    },
                }
            ]
        )
        client = _client(transport)

        with self.assertRaises(FeishuDocxDeliveryError) as raised:
            client.convert_markdown_to_blocks("正文")

        self.assertTrue(raised.exception.definite)
        self.assertEqual(raised.exception.code, "markdown_convert_block_count_mismatch")

    def test_a_duplicate_first_level_block_id_is_rejected_even_when_counts_coincidentally_match(
        self,
    ) -> None:
        """rc21 修复包 B（P3 docx 对账，opus 审查发现）：``first_level_block_
        ids`` 自身出现重复（``["blk-1", "blk-1"]``），且 ``blocks`` 数组里
        也恰好有两个块共用同一个 ``block_id``——两种成因在计数上互相抵消
        （重排后块数 2 == 原始块数 2），单靠"重排后块数与原始块数对不上"这
        一条对账**无法发现问题**，必须额外靠"``first_level_block_ids`` 自身
        有没有重复"这条独立对账挡住。复现"first_level 重复静默重复交付"：
        同一个块（字典推导式覆盖后剩下的那个）会在返回结果里出现两次。

        变异存活证据：把 ``len(first_level_block_ids) != len(set(...))`` 这条
        对账删掉，本用例会从抛出 `FeishuDocxDeliveryError` 变红成静默返回
        同一个块重复两次的结果（`len(ordered_blocks) == len(mapping_blocks)`
        这条对账不会报错，因为两个计数恰好都是 2）。
        """

        transport = RecordingTransport(
            [
                {
                    "code": 0,
                    "data": {
                        "blocks": [
                            {"block_id": "blk-1", "block_type": 2, "text": "第一段"},
                            {"block_id": "blk-1", "block_type": 2, "text": "第二段"},
                        ],
                        "first_level_block_ids": ["blk-1", "blk-1"],
                    },
                }
            ]
        )
        client = _client(transport)

        with self.assertRaises(FeishuDocxDeliveryError) as raised:
            client.convert_markdown_to_blocks("正文")

        self.assertTrue(raised.exception.definite)
        self.assertEqual(raised.exception.code, "markdown_convert_duplicate_first_level_block_ids")

    def test_an_empty_markdown_is_rejected_before_any_call(self) -> None:
        transport = RecordingTransport([])
        client = _client(transport)

        with self.assertRaises(ValueError):
            client.convert_markdown_to_blocks("   ")

        self.assertEqual(transport.calls, [])

    def test_a_feishu_business_error_code_is_rejected_and_definite(self) -> None:
        transport = RecordingTransport([{"code": 99991400, "msg": "rate limited"}])
        client = _client(transport)

        with self.assertRaises(FeishuDocxDeliveryError) as raised:
            client.convert_markdown_to_blocks("# 标题")

        self.assertEqual(raised.exception.code, "feishu_code_99991400")
        self.assertTrue(raised.exception.definite)

    def test_a_success_response_missing_blocks_is_a_lookup_error_not_a_silent_empty_result(
        self,
    ) -> None:
        """结果不明：飞书说成功（``code=0``）但拿不到 ``blocks`` 字段——不能
        当成"转换出一份空文档"，必须是 ``LookupError``。

        变异锚点：把"检查 blocks 是否是非空 list"这条判据删掉、直接
        ``return data.get("blocks") or []``，本用例会从抛出 ``LookupError``
        变红成返回 ``[]``。
        """

        transport = RecordingTransport([{"code": 0, "data": {}}])
        client = _client(transport)

        with self.assertRaises(LookupError):
            client.convert_markdown_to_blocks("# 标题")

    def test_a_response_whose_blocks_are_all_non_mapping_is_a_definite_docx_error(
        self,
    ) -> None:
        """P2 顺手（独立审查）：``blocks`` 字段本身是非空 list（不落进上面那条
        ``LookupError`` 分支），但每一项都不是期望的 block 形状——过滤掉非
        Mapping 项后剩下空列表。此前这里直接返回空列表，让它流进
        :meth:`LarkDocxDelivery.write_blocks`，在那里撞上一条与"入参校验、
        还没发出任何请求"同型的裸 ``ValueError("blocks 不能为空")``——把"转换
        接口已经真实调用、拿到的响应形状不对"误归成了
        `apps/gateway/document_delivery.py` 白名单里"发起请求前的确定性入参
        校验"那一类。应改为在这里直接判定失败，抛
        :class:`FeishuDocxDeliveryError`（``definite=True``——转换端点不写入
        任何文档、无外部副作用，同一份 markdown 重放会得到同样的转换结果，
        是确定性失败，不是"结果不明"）。

        变异锚点：删掉"过滤后是否为空"这条判据，本用例会从抛
        ``FeishuDocxDeliveryError`` 变红成静默返回 ``[]``。
        """

        transport = RecordingTransport(
            [{"code": 0, "data": {"blocks": ["not-a-block", 42, None]}}]
        )
        client = _client(transport)

        with self.assertRaises(FeishuDocxDeliveryError) as raised:
            client.convert_markdown_to_blocks("# 标题")

        self.assertTrue(raised.exception.definite)
        self.assertEqual(raised.exception.code, "markdown_convert_blocks_not_mapping")


class WriteBlocksTest(unittest.TestCase):
    def test_blocks_are_written_via_the_existing_children_endpoint(self) -> None:
        transport = RecordingTransport([{"code": 0, "data": {}}])
        client = _client(transport)
        blocks = [{"block_type": 1, "page": {}}, {"block_type": 2}]

        client.write_blocks(DOCUMENT_ID, blocks)

        self.assertEqual(len(transport.calls), 1)
        method, url, body, token = transport.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, f"{BASE_URL}/docx/v1/documents/{DOCUMENT_ID}/blocks/{DOCUMENT_ID}/children")
        self.assertEqual(body, {"children": blocks, "index": 0})
        self.assertEqual(token, FAKE_TOKEN)

    def test_an_empty_blocks_sequence_is_rejected_before_any_call(self) -> None:
        transport = RecordingTransport([])
        client = _client(transport)

        with self.assertRaises(ValueError):
            client.write_blocks(DOCUMENT_ID, [])

        self.assertEqual(transport.calls, [])

    def test_more_than_the_cap_is_rejected_with_a_definite_reason_code_before_any_call(self) -> None:
        """变异锚点：把 ``len(children) > MAX_CONVERTED_BLOCKS`` 这条判据删掉，
        本用例会从抛出 ``FeishuDocxDeliveryError`` 变红成真的发起一次插入请求。
        """

        transport = RecordingTransport([])
        client = _client(transport)
        blocks = [{"block_type": 2}] * (MAX_CONVERTED_BLOCKS + 1)

        with self.assertRaises(FeishuDocxDeliveryError) as raised:
            client.write_blocks(DOCUMENT_ID, blocks)

        self.assertEqual(raised.exception.code, "too_many_blocks")
        self.assertTrue(raised.exception.definite)
        self.assertEqual(transport.calls, [], "本地检查先于请求发出，不该真的发起 HTTP 调用")

    def test_exactly_the_cap_is_accepted(self) -> None:
        transport = RecordingTransport([{"code": 0, "data": {}}])
        client = _client(transport)
        blocks = [{"block_type": 2}] * MAX_CONVERTED_BLOCKS

        client.write_blocks(DOCUMENT_ID, blocks)

        self.assertEqual(len(transport.calls), 1)

    def test_a_blank_document_id_is_rejected_before_any_call(self) -> None:
        transport = RecordingTransport([])
        client = _client(transport)

        with self.assertRaises(ValueError):
            client.write_blocks("   ", [{"block_type": 2}])

        self.assertEqual(transport.calls, [])


class WriteBodySwitchTest(unittest.TestCase):
    """Issue #408：``write_body`` 是「markdown 官方转换开关」唯一的可执行分支
    点——默认关闭时逐字调用既有 ``write_paragraphs``（零行为变化），打开时改走
    convert + write_blocks，失败一律直接向上抛出，不静默退回纯文本路径。"""

    def test_default_disabled_calls_write_paragraphs_with_zero_convert_calls(self) -> None:
        """变异锚点：把 ``write_body`` 里 ``if self._markdown_convert_enabled``
        分支反过来（默认走 convert），本用例会从"只发生一次 children 插入调用"
        变红成"先发生一次 convert 调用"。"""

        transport = RecordingTransport([{"code": 0, "data": {}}])
        client = _client(transport)  # markdown_convert_enabled 默认 False

        client.write_body(DOCUMENT_ID, paragraphs=["第一段正文"], markdown="# 标题\n\n第一段正文")

        self.assertEqual(len(transport.calls), 1)
        method, url, body, _ = transport.calls[0]
        self.assertEqual(url, f"{BASE_URL}/docx/v1/documents/{DOCUMENT_ID}/blocks/{DOCUMENT_ID}/children")
        self.assertEqual(
            body,
            {
                "children": [
                    {"block_type": 2, "text": {"elements": [{"text_run": {"content": "第一段正文"}}]}}
                ],
                "index": 0,
            },
        )

    def test_enabled_calls_convert_then_writes_the_reordered_stripped_blocks(self) -> None:
        """探针实测形状：``blocks`` 数组物理顺序与 ``first_level_block_ids``
        声明的真实顺序不同，且每个块携带只读 ``block_id``——写入端点收到的
        必须是按真实顺序重排、剔除只读字段后的结果，不是响应原始顺序/原始
        字段。"""

        heading_raw = {
            "block_id": "blk-heading",
            "block_type": 3,
            "text": {"elements": [{"text_run": {"content": "标题"}}]},
        }
        body_raw = {
            "block_id": "blk-body",
            "block_type": 2,
            "text": {"elements": [{"text_run": {"content": "第一段正文"}}]},
        }
        # 响应物理顺序刻意与真实文档顺序（标题在前）相反。
        transport = RecordingTransport(
            [
                {
                    "code": 0,
                    "data": {
                        "blocks": [body_raw, heading_raw],
                        "first_level_block_ids": ["blk-heading", "blk-body"],
                    },
                },
                {"code": 0, "data": {}},
            ]
        )
        client = _client(transport, markdown_convert_enabled=True)

        client.write_body(DOCUMENT_ID, paragraphs=["第一段正文"], markdown="# 标题")

        self.assertEqual(len(transport.calls), 2)
        convert_method, convert_url, convert_body, _ = transport.calls[0]
        self.assertEqual(convert_method, "POST")
        self.assertEqual(convert_url, f"{BASE_URL}/docx/v1/documents/blocks/convert")
        self.assertEqual(convert_body, {"content_type": "markdown", "content": "# 标题"})
        write_method, write_url, write_body, _ = transport.calls[1]
        self.assertEqual(write_url, f"{BASE_URL}/docx/v1/documents/{DOCUMENT_ID}/blocks/{DOCUMENT_ID}/children")
        self.assertEqual(
            write_body,
            {
                "children": [
                    {"block_type": 3, "text": {"elements": [{"text_run": {"content": "标题"}}]}},
                    {"block_type": 2, "text": {"elements": [{"text_run": {"content": "第一段正文"}}]}},
                ],
                "index": 0,
            },
        )

    def test_enabled_convert_failure_fails_closed_without_falling_back_to_paragraphs(self) -> None:
        """开关打开时 convert 调用失败必须失败关闭，绝不静默退回纯文本段落
        路径——只应该看到一次 convert 调用，看不到任何 children 插入调用。"""

        transport = RecordingTransport([{"code": 99991400, "msg": "rate limited"}])
        client = _client(transport, markdown_convert_enabled=True)

        with self.assertRaises(FeishuDocxDeliveryError) as raised:
            client.write_body(DOCUMENT_ID, paragraphs=["第一段正文"], markdown="# 标题")

        self.assertTrue(raised.exception.definite)
        self.assertEqual(len(transport.calls), 1)

    def test_enabled_over_cap_blocks_fail_closed_without_any_insert_call(self) -> None:
        oversized_blocks = [
            {"block_id": f"blk-{index}", "block_type": 2} for index in range(MAX_CONVERTED_BLOCKS + 1)
        ]
        oversized_first_level_ids = [block["block_id"] for block in oversized_blocks]
        transport = RecordingTransport(
            [
                {
                    "code": 0,
                    "data": {"blocks": oversized_blocks, "first_level_block_ids": oversized_first_level_ids},
                }
            ]
        )
        client = _client(transport, markdown_convert_enabled=True)

        with self.assertRaises(FeishuDocxDeliveryError) as raised:
            client.write_body(DOCUMENT_ID, paragraphs=["第一段正文"], markdown="超长 markdown")

        self.assertEqual(raised.exception.code, "too_many_blocks")
        self.assertEqual(len(transport.calls), 1, "只应该发生一次 convert 调用，不该真的尝试插入")


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
