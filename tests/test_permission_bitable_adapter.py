"""当前权限多维表格 adapter 的协议面断言（Issue #156 / S-C-01）。

全部跑在注入的假传输层上：**本文件不做任何真实飞书调用、不读任何凭据**（真实写读回
留受控窗口的 L4a，见模块文档与 PR 未验证项）。

覆盖三件事：

1. 请求形态（路径、分页参数、请求体、部分更新只带自己的字段）；
2. 错误分类——服务端明确拒绝 = 明确失败，其余一切 = 结果不明（白名单口径，同
   ``adapters/feishu_delivery.py``）；
3. 凭据与外部标识不外泄：令牌只走请求头，Base / 表标识不进错误消息。
"""

from __future__ import annotations

import unittest

from lingxi.adapters.feishu_directory import FeishuDirectoryError
from lingxi.adapters.feishu_permission_bitable import BitablePermissionTable
from lingxi.core.permission.publish import PermissionTableError

BASE_URL = "https://open.feishu.invalid/open-apis"
APP_TOKEN = "app-token-fake"
TABLE_ID = "table-id-fake"
TOKEN = "t-fake-token"
FAKE_EMAIL = "jiaming.jia@example.invalid"


class RecordingTransport:
    """假传输：按调用次序返回预置响应，并记录每一次请求。"""

    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, method, url, *, body=None, token=None):
        self.calls.append({"method": method, "url": url, "body": body, "token": token})
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _table(responses: list[object], **kwargs) -> tuple[BitablePermissionTable, RecordingTransport]:
    transport = RecordingTransport(responses)
    table = BitablePermissionTable(
        base_url=BASE_URL,
        app_token=APP_TOKEN,
        table_id=TABLE_ID,
        access_token=lambda: TOKEN,
        transport=transport,
        **kwargs,
    )
    return table, transport


def _page(items, *, has_more=False, page_token=None):
    data = {"items": items}
    if has_more:
        data["has_more"] = True
        data["page_token"] = page_token
    return {"code": 0, "data": data}


def _record(record_id="rec_1", fields=None):
    return {"code": 0, "data": {"record": {"record_id": record_id, "fields": fields or {}}}}


class ConstructionTest(unittest.TestCase):
    def test_http_base_url_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            BitablePermissionTable(
                base_url="http://open.feishu.invalid/open-apis",
                app_token=APP_TOKEN,
                table_id=TABLE_ID,
                access_token=lambda: TOKEN,
            )

    def test_blank_identifiers_are_rejected_without_echoing(self) -> None:
        with self.assertRaises(ValueError) as caught:
            BitablePermissionTable(
                base_url=BASE_URL,
                app_token="  ",
                table_id=TABLE_ID,
                access_token=lambda: TOKEN,
            )
        self.assertNotIn("  ", str(caught.exception).replace("（不回显收到的值）", ""))

    def test_construction_makes_no_request(self) -> None:
        _, transport = _table([])
        self.assertEqual(transport.calls, [])

    def test_token_provider_must_be_callable(self) -> None:
        with self.assertRaises(ValueError):
            BitablePermissionTable(
                base_url=BASE_URL, app_token=APP_TOKEN, table_id=TABLE_ID, access_token=TOKEN
            )


class FindRowsTest(unittest.TestCase):
    def test_matches_either_record_key_or_email(self) -> None:
        table, transport = _table(
            [
                _page(
                    [
                        {
                            "record_id": "rec_1",
                            "fields": {"record_key": FAKE_EMAIL, "email": FAKE_EMAIL},
                        },
                        {
                            "record_id": "rec_2",
                            "fields": {"record_key": "EMP-1", "email": FAKE_EMAIL},
                        },
                        {
                            "record_id": "rec_3",
                            "fields": {"record_key": "other", "email": "other@x.invalid"},
                        },
                    ]
                )
            ]
        )
        rows = table.find_rows(record_key=FAKE_EMAIL, email=FAKE_EMAIL)
        self.assertEqual([row.record_id for row in rows], ["rec_1", "rec_2"])
        self.assertTrue(rows[0].matches_key(FAKE_EMAIL))
        self.assertFalse(rows[1].matches_key(FAKE_EMAIL))

    def test_segmented_cell_value_still_matches(self) -> None:
        """二级独立审查 P3-2：文本列的**分段形态**必须能命中。

        多维表格的文本列可能回来一个 `[{"text": …}, …]`；按 `str(value)` 比较会拿到
        Python 的 repr，永远匹配不上——而漏命中的后果不是查不到，是判成"这个人还没有
        行"于是**新建第二行权限**。归一必须与逐字段读回比对同一把尺子。
        """

        segmented = [
            {"type": "text", "text": "jiaming.jia@"},
            {"type": "text", "text": "example.invalid"},
        ]
        table, _ = _table(
            [_page([{"record_id": "rec_1", "fields": {"record_key": segmented, "email": ""}}])]
        )
        rows = table.find_rows(record_key=FAKE_EMAIL, email=FAKE_EMAIL)
        self.assertEqual([row.record_id for row in rows], ["rec_1"])
        # 命中之后仍按同一把尺子判定"是不是我们这个键"，因此走的是更新而不是冲突。
        self.assertTrue(rows[0].matches_key(FAKE_EMAIL))

    def test_match_is_case_insensitive(self) -> None:
        table, _ = _table(
            [
                _page(
                    [
                        {
                            "record_id": "rec_1",
                            "fields": {"record_key": FAKE_EMAIL.upper(), "email": ""},
                        }
                    ]
                )
            ]
        )
        self.assertEqual(len(table.find_rows(record_key=FAKE_EMAIL, email=FAKE_EMAIL)), 1)

    def test_token_goes_to_header_never_to_url(self) -> None:
        table, transport = _table([_page([])])
        table.find_rows(record_key=FAKE_EMAIL, email=FAKE_EMAIL)
        call = transport.calls[0]
        self.assertEqual(call["token"], TOKEN)
        self.assertNotIn(TOKEN, call["url"])
        self.assertEqual(call["method"], "GET")
        self.assertIn(f"/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/records?", call["url"])
        self.assertIn("page_size=500", call["url"])

    def test_pagination_follows_the_cursor(self) -> None:
        table, transport = _table(
            [
                _page(
                    [{"record_id": "rec_1", "fields": {"record_key": "x", "email": "x"}}],
                    has_more=True,
                    page_token="p2",
                ),
                _page(
                    [
                        {
                            "record_id": "rec_2",
                            "fields": {"record_key": FAKE_EMAIL, "email": FAKE_EMAIL},
                        }
                    ]
                ),
            ]
        )
        rows = table.find_rows(record_key=FAKE_EMAIL, email=FAKE_EMAIL)
        self.assertEqual([row.record_id for row in rows], ["rec_2"])
        self.assertIn("page_token=p2", transport.calls[1]["url"])

    def test_stalled_cursor_is_indeterminate_not_empty(self) -> None:
        """游标原地打转时必须失败：判成"没找到"会让同一个人被写出第二行。"""

        table, _ = _table([_page([], has_more=True, page_token=None)])
        with self.assertRaises(PermissionTableError) as caught:
            table.find_rows(record_key=FAKE_EMAIL, email=FAKE_EMAIL)
        self.assertEqual(caught.exception.code, "pagination_stalled")
        self.assertFalse(caught.exception.definite)

    def test_pagination_limit_is_an_error(self) -> None:
        pages = [_page([], has_more=True, page_token=f"p{index}") for index in range(3)]
        table, _ = _table(pages, max_pages=2)
        with self.assertRaises(PermissionTableError) as caught:
            table.find_rows(record_key=FAKE_EMAIL, email=FAKE_EMAIL)
        self.assertEqual(caught.exception.code, "pagination_limit")

    def test_non_object_page_item_is_rejected(self) -> None:
        table, _ = _table([{"code": 0, "data": {"items": ["not-an-object"]}}])
        with self.assertRaises(PermissionTableError) as caught:
            table.find_rows(record_key=FAKE_EMAIL, email=FAKE_EMAIL)
        self.assertEqual(caught.exception.code, "invalid_page_item")

    def test_empty_table_without_items_key_is_not_a_failure(self) -> None:
        table, _ = _table([{"code": 0, "data": {}}])
        self.assertEqual(table.find_rows(record_key=FAKE_EMAIL, email=FAKE_EMAIL), ())


class WriteTest(unittest.TestCase):
    def test_create_posts_fields_and_returns_record_id(self) -> None:
        table, transport = _table([_record("rec_7")])
        fields = {"record_key": FAKE_EMAIL, "email": FAKE_EMAIL}
        self.assertEqual(table.create_row(fields), "rec_7")
        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["body"], {"fields": fields})
        self.assertTrue(call["url"].endswith("/records"))

    def test_update_only_sends_given_fields(self) -> None:
        """部分更新语义：``token_cipher`` 不在字段集里，因此不会出现在请求体里。"""

        table, transport = _table([_record("rec_7")])
        table.update_row("rec_7", {"name": "化名甲"})
        call = transport.calls[0]
        self.assertEqual(call["method"], "PUT")
        self.assertEqual(call["body"], {"fields": {"name": "化名甲"}})
        self.assertNotIn("token_cipher", str(call["body"]))
        self.assertTrue(call["url"].endswith("/records/rec_7"))

    def test_read_returns_raw_fields(self) -> None:
        table, transport = _table([_record("rec_7", {"record_key": FAKE_EMAIL, "permissions": 12})])
        self.assertEqual(table.read_row("rec_7"), {"record_key": FAKE_EMAIL, "permissions": 12})
        self.assertEqual(transport.calls[0]["method"], "GET")

    def test_create_without_record_id_is_indeterminate(self) -> None:
        table, _ = _table([{"code": 0, "data": {"record": {"fields": {}}}}])
        with self.assertRaises(PermissionTableError) as caught:
            table.create_row({"record_key": FAKE_EMAIL})
        self.assertEqual(caught.exception.code, "record_id_missing")
        self.assertFalse(caught.exception.definite)

    def test_blank_record_id_is_rejected_before_any_call(self) -> None:
        table, transport = _table([])
        with self.assertRaises(ValueError):
            table.read_row("  ")
        self.assertEqual(transport.calls, [])


class FailureClassificationTest(unittest.TestCase):
    def test_business_error_code_is_definite(self) -> None:
        table, _ = _table([{"code": 91403, "msg": "Forbidden"}])
        with self.assertRaises(PermissionTableError) as caught:
            table.create_row({"record_key": FAKE_EMAIL})
        self.assertEqual(caught.exception.code, "feishu_code_91403")
        self.assertTrue(caught.exception.definite)

    def test_transport_error_stays_indeterminate(self) -> None:
        table, _ = _table([FeishuDirectoryError("transport_error")])
        with self.assertRaises(PermissionTableError) as caught:
            table.create_row({"record_key": FAKE_EMAIL})
        self.assertEqual(caught.exception.code, "transport_error")
        self.assertFalse(caught.exception.definite)

    def test_definite_flag_survives_translation(self) -> None:
        table, _ = _table([FeishuDirectoryError("feishu_code_1254043")])
        with self.assertRaises(PermissionTableError) as caught:
            table.read_row("rec_1")
        self.assertTrue(caught.exception.definite)

    def test_broken_response_shape_is_indeterminate(self) -> None:
        table, _ = _table(["not-a-mapping"])
        with self.assertRaises(PermissionTableError) as caught:
            table.read_row("rec_1")
        self.assertEqual(caught.exception.code, "invalid_response_shape")

    def test_missing_token_is_indeterminate_and_sends_nothing(self) -> None:
        transport = RecordingTransport([])
        table = BitablePermissionTable(
            base_url=BASE_URL,
            app_token=APP_TOKEN,
            table_id=TABLE_ID,
            access_token=lambda: "",
            transport=transport,
        )
        with self.assertRaises(PermissionTableError) as caught:
            table.read_row("rec_1")
        self.assertEqual(caught.exception.code, "access_token_missing")
        self.assertEqual(transport.calls, [])

    def test_credential_provider_errors_are_not_disguised(self) -> None:
        """拿不到凭据是本侧配置问题，不是"发布表异常"：原样上抛，不折成通道错误。"""

        def broken() -> str:
            raise RuntimeError("凭据未就绪")

        table = BitablePermissionTable(
            base_url=BASE_URL,
            app_token=APP_TOKEN,
            table_id=TABLE_ID,
            access_token=broken,
            transport=RecordingTransport([]),
        )
        with self.assertRaises(RuntimeError):
            table.read_row("rec_1")

    def test_error_message_does_not_echo_identifiers_or_token(self) -> None:
        table, _ = _table([{"code": 91403, "msg": "Forbidden"}])
        with self.assertRaises(PermissionTableError) as caught:
            table.create_row({"record_key": FAKE_EMAIL})
        message = str(caught.exception)
        self.assertNotIn(APP_TOKEN, message)
        self.assertNotIn(TABLE_ID, message)
        self.assertNotIn(TOKEN, message)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
