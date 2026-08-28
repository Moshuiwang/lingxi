"""``adapters.feishu_sheets_delivery`` 的契约测试（Issue #354 S-H3-2）。

传输层全部注入，本文件**不发起任何真实网络请求**——真实链路已由 #354 S-W0-3
探针（2026-08-28，四步全通，证据等级 6）验证过，本文件只钉住"调用形态与飞书
契约逐字一致"这件事，理由同 ``tests/test_feishu_docx_delivery.py`` 的模块文档。

请求路径与字段形状取自 Issue #354 最新评论登记的 S-W0-3 探针实测结果：

- 建表：``POST /sheets/v3/spreadsheets`` ``{"title": ...}`` →
  ``data.spreadsheet.{spreadsheet_token,url}``
- 查默认 sheet_id：``GET /sheets/v3/spreadsheets/{token}/sheets/query`` →
  ``data.sheets[0].sheet_id``
- 写值：``PUT /sheets/v2/spreadsheets/{token}/values``
  ``{"valueRange": {"range": ..., "values": [[...]]}}``
- 授权：``POST /drive/v1/permissions/{token}/members?type=sheet``
  ``{"member_type": "openid", "member_id": ..., "perm": "full_access"}``
- 读回：``GET /drive/v1/permissions/{token}/members?type=sheet``
"""

from __future__ import annotations

import io
import unittest
from urllib.error import HTTPError, URLError

from lingxi.adapters.feishu_sheets_delivery import (
    FeishuSheetsDeliveryError,
    LarkSheetsDelivery,
    _column_letter,
    urllib_transport,
)

BASE_URL = "https://feishu.invalid/open-apis"
FAKE_TOKEN = "t-fake-tenant-access-token"
SPREADSHEET_TOKEN = "shtcnFakeSpreadsheetToken"
SHEET_ID = "sheetIdFake01"
SPREADSHEET_URL = "https://example.feishu.cn/sheets/shtcnFakeSpreadsheetToken"
OPEN_ID = "ou_target_user"


class RecordingTransport:
    """按顺序返回预置响应，并记录每次调用（同 ``test_feishu_docx_delivery.py``
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


def _client(transport, *, tenant_access_token=None) -> LarkSheetsDelivery:
    return LarkSheetsDelivery(
        base_url=BASE_URL,
        tenant_access_token=tenant_access_token or _token_supply(),
        transport=transport,
    )


class ClientConstructionTest(unittest.TestCase):
    def test_a_non_https_base_url_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            LarkSheetsDelivery(
                base_url="http://feishu.invalid/open-apis",
                tenant_access_token=_token_supply(),
            )

    def test_a_non_callable_token_supply_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            LarkSheetsDelivery(base_url=BASE_URL, tenant_access_token=FAKE_TOKEN)  # type: ignore[arg-type]

    def test_does_not_require_tenant_domain(self) -> None:
        # 与 LarkDocxDelivery 的关键差异（模块文档「与文档交付的差异点」第 1
        # 条）：不接收 tenant_domain 参数，建表响应自带 url。
        client = LarkSheetsDelivery(base_url=BASE_URL, tenant_access_token=_token_supply())
        self.assertIsInstance(client, LarkSheetsDelivery)


class CreateSpreadsheetTest(unittest.TestCase):
    def test_a_successful_create_returns_token_and_url_matching_the_probed_shape(self) -> None:
        transport = RecordingTransport(
            [{"code": 0, "data": {"spreadsheet": {"spreadsheet_token": SPREADSHEET_TOKEN, "url": SPREADSHEET_URL, "title": "本月销售分析"}}}]
        )
        client = _client(transport)

        token, url = client.create_spreadsheet("本月销售分析")

        self.assertEqual(token, SPREADSHEET_TOKEN)
        self.assertEqual(url, SPREADSHEET_URL)
        self.assertEqual(len(transport.calls), 1)
        method, request_url, body, token_header = transport.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(request_url, f"{BASE_URL}/sheets/v3/spreadsheets")
        self.assertEqual(body, {"title": "本月销售分析"})
        self.assertEqual(token_header, FAKE_TOKEN)

    def test_an_empty_title_is_rejected_before_any_call(self) -> None:
        transport = RecordingTransport([])
        client = _client(transport)

        with self.assertRaises(ValueError):
            client.create_spreadsheet("   ")

        self.assertEqual(transport.calls, [])

    def test_a_feishu_business_error_code_is_rejected_and_definite(self) -> None:
        transport = RecordingTransport([{"code": 99991400, "msg": "invalid title"}])
        client = _client(transport)

        with self.assertRaises(FeishuSheetsDeliveryError) as raised:
            client.create_spreadsheet("本月销售分析")

        self.assertEqual(raised.exception.code, "feishu_code_99991400")
        self.assertTrue(raised.exception.definite)
        self.assertNotIn("本月销售分析", str(raised.exception))

    def test_a_success_response_missing_spreadsheet_token_is_a_lookup_error(self) -> None:
        """结果不明：飞书说成功但拿不到可回读标识——不是明确拒绝，必须是
        ``LookupError``，不能静默当成功处理。

        变异锚点：把 ``create_spreadsheet`` 里检查 ``spreadsheet_token`` 是否
        存在的判据删掉，本用例会从抛出 ``LookupError`` 变红。
        """

        transport = RecordingTransport([{"code": 0, "data": {"spreadsheet": {"url": SPREADSHEET_URL}}}])
        client = _client(transport)

        with self.assertRaises(LookupError):
            client.create_spreadsheet("本月销售分析")

    def test_a_success_response_missing_url_is_a_lookup_error(self) -> None:
        """探针实测建表响应自带 url——缺失时同样是结果不明，不能悄悄返回空链接。

        变异锚点：把 ``create_spreadsheet`` 里检查 ``url`` 是否存在的判据删掉，
        本用例会从抛出 ``LookupError`` 变红成返回一个 falsy 的 url。
        """

        transport = RecordingTransport(
            [{"code": 0, "data": {"spreadsheet": {"spreadsheet_token": SPREADSHEET_TOKEN}}}]
        )
        client = _client(transport)

        with self.assertRaises(LookupError):
            client.create_spreadsheet("本月销售分析")

    def test_a_missing_spreadsheet_object_is_a_lookup_error(self) -> None:
        transport = RecordingTransport([{"code": 0, "data": {}}])
        client = _client(transport)

        with self.assertRaises(LookupError):
            client.create_spreadsheet("本月销售分析")


class GetDefaultSheetIdTest(unittest.TestCase):
    def test_a_successful_query_returns_the_first_sheet_id(self) -> None:
        transport = RecordingTransport(
            [{"code": 0, "data": {"sheets": [{"sheet_id": SHEET_ID, "title": "Sheet1"}, {"sheet_id": "other"}]}}]
        )
        client = _client(transport)

        sheet_id = client.get_default_sheet_id(SPREADSHEET_TOKEN)

        self.assertEqual(sheet_id, SHEET_ID)
        method, url, body, token = transport.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, f"{BASE_URL}/sheets/v3/spreadsheets/{SPREADSHEET_TOKEN}/sheets/query")
        self.assertIsNone(body)
        self.assertEqual(token, FAKE_TOKEN)

    def test_an_empty_spreadsheet_token_is_rejected_before_any_call(self) -> None:
        transport = RecordingTransport([])
        client = _client(transport)

        with self.assertRaises(ValueError):
            client.get_default_sheet_id("  ")

        self.assertEqual(transport.calls, [])

    def test_an_empty_sheets_array_is_a_lookup_error(self) -> None:
        """新建表格默认至少有一个 sheet——空数组是结果不明，不是"这张表没有
        sheet"这个业务事实。

        变异锚点：把"``sheets`` 非空"判据改成只判"是 list"，本用例会从抛出
        ``LookupError`` 变红。
        """

        transport = RecordingTransport([{"code": 0, "data": {"sheets": []}}])
        client = _client(transport)

        with self.assertRaises(LookupError):
            client.get_default_sheet_id(SPREADSHEET_TOKEN)

    def test_a_missing_sheet_id_field_is_a_lookup_error(self) -> None:
        transport = RecordingTransport([{"code": 0, "data": {"sheets": [{"title": "Sheet1"}]}}])
        client = _client(transport)

        with self.assertRaises(LookupError):
            client.get_default_sheet_id(SPREADSHEET_TOKEN)


class WriteValuesTest(unittest.TestCase):
    def test_a_successful_write_uses_the_v2_endpoint_and_range_from_a1(self) -> None:
        transport = RecordingTransport([{"code": 0, "data": {"updatedRange": f"{SHEET_ID}!A1:B2", "updatedCells": 4}}])
        client = _client(transport)

        client.write_values(SPREADSHEET_TOKEN, SHEET_ID, [["a", "b"], ["c", "d"]])

        method, url, body, token = transport.calls[0]
        self.assertEqual(method, "PUT")
        # 注意版本号：写值是 v2，不是建表/查询用的 v3（模块文档「与文档交付的
        # 差异点」第 2 条）。
        self.assertEqual(url, f"{BASE_URL}/sheets/v2/spreadsheets/{SPREADSHEET_TOKEN}/values")
        self.assertEqual(
            body, {"valueRange": {"range": f"{SHEET_ID}!A1:B2", "values": [["a", "b"], ["c", "d"]]}}
        )
        self.assertEqual(token, FAKE_TOKEN)

    def test_range_end_column_matches_the_row_width_for_a_rectangular_matrix(self) -> None:
        """上游 ``build_sheet_request`` 已经把矩阵补齐成矩形（Trace #373 H3
        批量审查 P1），本模块只需要覆盖"多列"这一形状本身——不规则输入见
        ``RectangularMatrixDefenseTest``（本模块的防御断言，拒绝而不是猜测）。
        """

        transport = RecordingTransport([{"code": 0, "data": {}}])
        client = _client(transport)

        client.write_values(SPREADSHEET_TOKEN, SHEET_ID, [["a", "b", "c"], ["d", "e", "f"]])

        _, _, body, _ = transport.calls[0]
        self.assertEqual(body["valueRange"]["range"], f"{SHEET_ID}!A1:C2")

    def test_empty_rows_is_rejected_before_any_call(self) -> None:
        transport = RecordingTransport([])
        client = _client(transport)

        with self.assertRaises(ValueError):
            client.write_values(SPREADSHEET_TOKEN, SHEET_ID, [])

        self.assertEqual(transport.calls, [])

    def test_a_row_with_a_non_string_cell_is_rejected_before_any_call(self) -> None:
        transport = RecordingTransport([])
        client = _client(transport)

        with self.assertRaises(ValueError):
            client.write_values(SPREADSHEET_TOKEN, SHEET_ID, [["a", 1]])  # type: ignore[list-item]

        self.assertEqual(transport.calls, [])

    def test_an_empty_row_is_rejected_before_any_call(self) -> None:
        transport = RecordingTransport([])
        client = _client(transport)

        with self.assertRaises(ValueError):
            client.write_values(SPREADSHEET_TOKEN, SHEET_ID, [[]])

        self.assertEqual(transport.calls, [])

    def test_a_feishu_business_error_code_is_rejected_and_definite(self) -> None:
        transport = RecordingTransport([{"code": 90202, "msg": "range invalid"}])
        client = _client(transport)

        with self.assertRaises(FeishuSheetsDeliveryError) as raised:
            client.write_values(SPREADSHEET_TOKEN, SHEET_ID, [["a"]])

        self.assertTrue(raised.exception.definite)


class RectangularMatrixDefenseTest(unittest.TestCase):
    """P1（Trace #373 H3 批量审查）：适配器侧的第二道纵深防线——即使上游
    ``build_sheet_request`` 已经把矩阵补齐成矩形，本模块自己收到不规则矩阵时
    仍然拒绝，不猜测该怎么补（失败关闭）。

    变异锚点：把 ``write_values`` 里 ``row_lengths`` 的矩形校验删掉，本用例会
    从抛出 ``ValueError`` 变红（不规则矩阵会被放行、按最长行拼出 range 发出去）。
    """

    def test_an_irregular_matrix_is_rejected_before_any_call(self) -> None:
        transport = RecordingTransport([])
        client = _client(transport)

        with self.assertRaises(ValueError):
            client.write_values(SPREADSHEET_TOKEN, SHEET_ID, [["a"], ["b", "c", "d"]])

        self.assertEqual(transport.calls, [])

    def test_a_rectangular_matrix_is_accepted(self) -> None:
        """反向哨兵：矩形矩阵本身不受这道防御影响。"""

        transport = RecordingTransport([{"code": 0, "data": {}}])
        client = _client(transport)

        client.write_values(SPREADSHEET_TOKEN, SHEET_ID, [["a", "b"], ["c", "d"]])

        self.assertEqual(len(transport.calls), 1)


class RowsShapeValidationTest(unittest.TestCase):
    """P2-1（opus 审查）：``rows`` 或其中某一行不是列表/元组时，必须在
    ``list(row)`` 之前就失败关闭成本模块 ``ValueError``——不能让 Python 内建的
    ``TypeError`` 先冒出来，被调用方 ``_process_sheet_claim`` 误判为"结果不明"
    （``uncertain``）而不是"入参形状错误，一定还没发出请求"（``failed``）。

    变异锚点：把这里新增的形状校验（``rows``/``row`` 的 ``isinstance`` 检查）
    删掉，本用例会从抛出 ``ValueError`` 变红成抛出 ``TypeError``。
    """

    def test_a_none_row_is_a_value_error_not_a_type_error(self) -> None:
        transport = RecordingTransport([])
        client = _client(transport)

        with self.assertRaises(ValueError):
            client.write_values(SPREADSHEET_TOKEN, SHEET_ID, [None])  # type: ignore[list-item]

        self.assertEqual(transport.calls, [])

    def test_rows_itself_not_being_a_list_is_a_value_error(self) -> None:
        transport = RecordingTransport([])
        client = _client(transport)

        with self.assertRaises(ValueError):
            client.write_values(SPREADSHEET_TOKEN, SHEET_ID, None)  # type: ignore[arg-type]

        self.assertEqual(transport.calls, [])


class GrantFullAccessTest(unittest.TestCase):
    def test_a_successful_grant_uses_type_sheet_and_matches_the_probed_shape(self) -> None:
        transport = RecordingTransport([{"code": 0, "data": {"member": {"member_id": OPEN_ID, "perm": "full_access"}}}])
        client = _client(transport)

        client.grant_full_access(SPREADSHEET_TOKEN, OPEN_ID)

        method, url, body, token = transport.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, f"{BASE_URL}/drive/v1/permissions/{SPREADSHEET_TOKEN}/members?type=sheet")
        self.assertEqual(body, {"member_type": "openid", "member_id": OPEN_ID, "perm": "full_access"})
        self.assertEqual(token, FAKE_TOKEN)

    def test_an_open_id_without_the_user_prefix_is_rejected_before_any_call(self) -> None:
        transport = RecordingTransport([])
        client = _client(transport)

        for bad_open_id in ("", "  ", "oc_group_id", "ou_"):
            with self.subTest(bad_open_id=bad_open_id):
                with self.assertRaises(ValueError):
                    client.grant_full_access(SPREADSHEET_TOKEN, bad_open_id)

        self.assertEqual(transport.calls, [])

    def test_a_feishu_business_error_code_is_rejected_and_definite(self) -> None:
        transport = RecordingTransport([{"code": 1061002, "msg": "permission denied"}])
        client = _client(transport)

        with self.assertRaises(FeishuSheetsDeliveryError) as raised:
            client.grant_full_access(SPREADSHEET_TOKEN, OPEN_ID)

        self.assertTrue(raised.exception.definite)


class ReadMembersTest(unittest.TestCase):
    def test_a_successful_read_returns_items_matching_the_probed_shape(self) -> None:
        transport = RecordingTransport(
            [
                {
                    "code": 0,
                    "data": {
                        "items": [
                            {"member_type": "openid", "member_id": OPEN_ID, "perm": "full_access", "perm_type": "container"},
                            {"member_type": "openid", "member_id": "ou_app_self", "perm": "full_access"},
                        ]
                    },
                }
            ]
        )
        client = _client(transport)

        members = client.read_members(SPREADSHEET_TOKEN)

        self.assertEqual(
            members,
            [
                {"member_type": "openid", "member_id": OPEN_ID, "perm": "full_access"},
                {"member_type": "openid", "member_id": "ou_app_self", "perm": "full_access"},
            ],
        )
        method, url, body, token = transport.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, f"{BASE_URL}/drive/v1/permissions/{SPREADSHEET_TOKEN}/members?type=sheet")

    def test_a_missing_items_field_is_a_lookup_error(self) -> None:
        """真实响应形状是 ``data.items``（同 docx），不是 ``data.members``——
        缺失时结果不明，不能悄悄返回空列表当成"零协作者"。

        变异锚点：把"检查 items 是否存在"改成缺失时静默返回 ``[]``，本用例会
        从抛出 ``LookupError`` 变红。
        """

        transport = RecordingTransport([{"code": 0, "data": {"members": []}}])
        client = _client(transport)

        with self.assertRaises(LookupError):
            client.read_members(SPREADSHEET_TOKEN)


class ErrorClassificationTest(unittest.TestCase):
    def test_a_transport_exception_is_indefinite(self) -> None:
        transport = RecordingTransport([URLError("boom")])
        client = _client(transport, tenant_access_token=_token_supply())

        # urllib_transport 本身在真实调用里会把 URLError 分类成 indefinite；
        # 这里注入的假传输层直接抛出，等价于验证调用方（_data）不会把非
        # FeishuSheetsDeliveryError 异常悄悄吞掉或误判为 definite。
        with self.assertRaises(URLError):
            client.create_spreadsheet("t")

    def test_a_missing_token_from_the_supply_is_indefinite_not_a_feishu_rejection(self) -> None:
        transport = RecordingTransport([])
        client = _client(transport, tenant_access_token=lambda: "")

        with self.assertRaises(FeishuSheetsDeliveryError) as raised:
            client.create_spreadsheet("t")

        self.assertFalse(raised.exception.definite)
        self.assertEqual(transport.calls, [])

    def test_a_non_mapping_response_is_indefinite(self) -> None:
        transport = RecordingTransport([None])
        client = _client(transport)

        with self.assertRaises(FeishuSheetsDeliveryError) as raised:
            client.create_spreadsheet("t")

        self.assertFalse(raised.exception.definite)

    def test_error_code_rendering_only_accepts_a_real_int_not_injected_strings(self) -> None:
        """业务错误码只以货真价实的 int 形式拼进异常，防止响应体注入进异常消息
        （同 ``feishu_docx_delivery`` 的注入防护理由）。

        变异锚点：把 ``_safe_feishu_code`` 的 ``isinstance(value, int)`` 判断
        去掉、直接 ``f"feishu_code_{value}"``，本用例会从固定标签
        ``feishu_code_invalid`` 变红成把注入字符串原样拼进 code。
        """

        transport = RecordingTransport([{"code": "'; DROP TABLE users; --"}])
        client = _client(transport)

        with self.assertRaises(FeishuSheetsDeliveryError) as raised:
            client.create_spreadsheet("t")

        self.assertEqual(raised.exception.code, "feishu_code_invalid")

    def test_a_bool_code_is_not_treated_as_a_real_int(self) -> None:
        """``bool`` 是 ``int`` 的子类——``_safe_feishu_code`` 必须显式排除它，
        否则 ``code=True`` 会被当成 ``code=1`` 渲染成看似合法的错误码。
        """

        transport = RecordingTransport([{"code": True}])
        client = _client(transport)

        with self.assertRaises(FeishuSheetsDeliveryError) as raised:
            client.create_spreadsheet("t")

        self.assertEqual(raised.exception.code, "feishu_code_invalid")


class MissingCodeTest(unittest.TestCase):
    """P1（Trace #373 H3 批 codex 外审②修复①）：``code`` 字段缺失（``None``）
    不当作成功放行——组合场景是 ``urllib_transport`` 对 ``HTTPError`` 解析出
    JSON 就原样返回，若这份 JSON 是 ``{}``（HTTP 500 但响应体没有 ``code``），
    旧实现的 ``code not in (None, 0, "0")`` 判据会放行，写值被判"成功"，实际
    交付空表/未更新表。

    变异锚点：把 ``_data`` 里 ``code is None`` 这条分支删掉（退回旧的
    ``code not in (None, 0, "0")`` 判据），本组用例会从抛出
    ``FeishuSheetsDeliveryError`` 变红成静默放行。
    """

    def test_a_response_missing_code_is_rejected_and_indefinite(self) -> None:
        transport = RecordingTransport([{"data": {"spreadsheet": {"spreadsheet_token": SPREADSHEET_TOKEN, "url": SPREADSHEET_URL}}}])
        client = _client(transport)

        with self.assertRaises(FeishuSheetsDeliveryError) as raised:
            client.create_spreadsheet("本月销售分析")

        self.assertEqual(raised.exception.code, "missing_code")
        self.assertFalse(raised.exception.definite)

    def test_write_values_with_a_missing_code_response_is_rejected(self) -> None:
        """三个写操作（建表已单独覆盖）里挑 ``write_values`` 代表性覆盖一次：
        缺 ``code`` 时不能被判"已写成功"。"""

        transport = RecordingTransport([{}])
        client = _client(transport)

        with self.assertRaises(FeishuSheetsDeliveryError) as raised:
            client.write_values(SPREADSHEET_TOKEN, SHEET_ID, [["a"]])

        self.assertEqual(raised.exception.code, "missing_code")
        self.assertFalse(raised.exception.definite)

    def test_an_http_500_with_an_empty_json_body_is_rejected_end_to_end(self) -> None:
        """端到端组合场景：``urllib_transport`` 对 ``HTTPError`` 解析出 JSON
        就原样返回（不看有没有 ``code``），``{}`` 是这类响应体的真实最小形状
        （HTTP 500 但服务端没有按飞书契约填充业务错误码）——本用例钉住
        ``urllib_transport`` → ``_data`` 整条链路对这一形状 fail-closed，不是
        只在 ``_data`` 单元测试层面成立。
        """

        import lingxi.adapters.feishu_sheets_delivery as module

        def boom(*_args, **_kwargs):
            raise HTTPError(f"{BASE_URL}/sheets/v3/spreadsheets", 500, "Internal Server Error", {}, io.BytesIO(b"{}"))

        original = module.urlopen
        module.urlopen = boom
        try:
            client = LarkSheetsDelivery(
                base_url=BASE_URL, tenant_access_token=_token_supply(), transport=None
            )
            with self.assertRaises(FeishuSheetsDeliveryError) as raised:
                client.create_spreadsheet("本月销售分析")
            self.assertEqual(raised.exception.code, "missing_code")
            self.assertFalse(raised.exception.definite)
        finally:
            module.urlopen = original


class ColumnLetterTest(unittest.TestCase):
    def test_known_values_match_spreadsheet_column_convention(self) -> None:
        cases = {1: "A", 2: "B", 26: "Z", 27: "AA", 28: "AB", 52: "AZ", 53: "BA", 702: "ZZ", 703: "AAA"}
        for column_count, expected in cases.items():
            with self.subTest(column_count=column_count):
                self.assertEqual(_column_letter(column_count), expected)

    def test_non_positive_input_is_rejected(self) -> None:
        for bad_value in (0, -1):
            with self.subTest(bad_value=bad_value):
                with self.assertRaises(ValueError):
                    _column_letter(bad_value)


class UrllibTransportSmokeTest(unittest.TestCase):
    def test_a_connection_failure_is_classified_as_indefinite(self) -> None:
        # 与 test_feishu_docx_delivery.py 同一姿态：monkeypatch urlopen 而不是
        # 真的发起网络请求——确定性、不依赖 DNS/网络可用性。
        import lingxi.adapters.feishu_sheets_delivery as module

        original = module.urlopen

        def boom(*_args, **_kwargs):
            raise URLError("boom")

        module.urlopen = boom
        try:
            with self.assertRaises(FeishuSheetsDeliveryError) as raised:
                urllib_transport("POST", f"{BASE_URL}/sheets/v3/spreadsheets", body={"title": "x"}, token=FAKE_TOKEN)
            self.assertEqual(raised.exception.code, "transport_error")
            self.assertFalse(raised.exception.definite)
        finally:
            module.urlopen = original


if __name__ == "__main__":
    unittest.main()
