"""存量令牌只读源 bitable 适配器的协议面断言（Issue #281 载体，Trace #304 批次 3）。

全部跑在注入的假传输层与假（但形状真实）主密钥上：**本文件不做任何真实飞书调用**
（真实读取 + 真实解密留受控窗口的 L4a，见 PR 未验证项）。测试主密钥是 biai-agent 加密
规格公开的自验向量，非生产密钥（同 ``tests/test_mcp_token_postgres.py``）。

覆盖三件事：

1. :class:`BitableStockTokenSource`（纯 I/O 层）——按邮箱整表分页查找，三种原始事实
   （查无此行 / 有行无密文 / 有行有密文）+ 多行命中失败关闭 + 大小写不敏感匹配 + 令牌
   只走请求头；
2. :class:`DecryptingStockTokenSource`（组合层）——三态原始事实翻成四态端口结果，
   解密失败翻成 ``DECRYPT_FAILED`` 状态而不是异常；
3. 凭据与外部标识不外泄：令牌只走请求头，Base / 表标识不进错误消息。
"""

from __future__ import annotations

import unittest

from lingxi.adapters.feishu_directory import FeishuDirectoryError
from lingxi.adapters.mcp_token_cipher import McpTokenCipher
from lingxi.adapters.stock_token_bitable import (
    BitableStockTokenSource,
    DecryptingStockTokenSource,
    RawStockTokenRow,
    StockTokenSourceError,
)
from lingxi.core.identity.stock_token_source import (
    ADOPTABLE,
    DECRYPT_FAILED,
    NO_CIPHER,
    NO_ROW,
)

BASE_URL = "https://open.feishu.invalid/open-apis"
APP_TOKEN = "app-token-fake"
TABLE_ID = "table-id-fake"
TOKEN = "t-fake-token"
FAKE_EMAIL = "jiaming.jia@example.invalid"

# 规格公开的测试向量主密钥，非生产密钥（同 test_mcp_token_postgres.py）。
SPEC_MASTER_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
OTHER_MASTER_KEY = "enp6enp6enp6enp6enp6enp6enp6enp6enp6enp6eno="


class RecordingTransport:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, method, url, *, body=None, token=None):
        self.calls.append({"method": method, "url": url, "body": body, "token": token})
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _reader(responses: list[object], **kwargs) -> tuple[BitableStockTokenSource, RecordingTransport]:
    transport = RecordingTransport(responses)
    reader = BitableStockTokenSource(
        base_url=BASE_URL,
        app_token=APP_TOKEN,
        table_id=TABLE_ID,
        access_token=lambda: TOKEN,
        transport=transport,
        **kwargs,
    )
    return reader, transport


def _page(items, *, has_more=False, page_token=None):
    data = {"items": items}
    if has_more:
        data["has_more"] = True
        data["page_token"] = page_token
    return {"code": 0, "data": data}


def _cipher_of(plaintext: str, key: str = SPEC_MASTER_KEY) -> str:
    return McpTokenCipher(key).encrypt(plaintext)


class ConstructionTest(unittest.TestCase):
    def test_http_base_url_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            BitableStockTokenSource(
                base_url="http://open.feishu.invalid/open-apis",
                app_token=APP_TOKEN,
                table_id=TABLE_ID,
                access_token=lambda: TOKEN,
            )

    def test_blank_identifiers_are_rejected_without_echoing(self) -> None:
        with self.assertRaises(ValueError) as caught:
            BitableStockTokenSource(
                base_url=BASE_URL, app_token="  ", table_id=TABLE_ID, access_token=lambda: TOKEN
            )
        self.assertNotIn("  ", str(caught.exception).replace("（不回显收到的值）", ""))

    def test_construction_makes_no_request(self) -> None:
        _, transport = _reader([])
        self.assertEqual(transport.calls, [])

    def test_token_provider_must_be_callable(self) -> None:
        with self.assertRaises(ValueError):
            BitableStockTokenSource(
                base_url=BASE_URL, app_token=APP_TOKEN, table_id=TABLE_ID, access_token=TOKEN
            )


class LookupRawTest(unittest.TestCase):
    def test_no_match_returns_none(self) -> None:
        reader, _ = _reader([_page([{"record_id": "rec_1", "fields": {"email": "other@x.invalid"}}])])
        self.assertIsNone(reader.lookup_raw(FAKE_EMAIL))

    def test_match_with_cipher(self) -> None:
        cipher = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8gISIjJCUmJygpKissLS4v"
        reader, _ = _reader(
            [_page([{"record_id": "rec_1", "fields": {
                "email": FAKE_EMAIL, "token_cipher": cipher, "status": "approved",
            }}])]
        )
        row = reader.lookup_raw(FAKE_EMAIL)
        self.assertEqual(row, RawStockTokenRow(token_cipher=cipher, status="approved"))

    def test_match_without_cipher(self) -> None:
        reader, _ = _reader(
            [_page([{"record_id": "rec_1", "fields": {"email": FAKE_EMAIL, "token_cipher": ""}}])]
        )
        row = reader.lookup_raw(FAKE_EMAIL)
        self.assertEqual(row, RawStockTokenRow(token_cipher="", status=""))

    def test_match_is_case_insensitive(self) -> None:
        reader, _ = _reader(
            [_page([{"record_id": "rec_1", "fields": {"email": FAKE_EMAIL.upper()}}])]
        )
        self.assertIsNotNone(reader.lookup_raw(FAKE_EMAIL))

    def test_multiple_matches_fail_closed_not_a_guess(self) -> None:
        """email 多行命中=按错误处理（不猜哪一行）。"""

        reader, _ = _reader(
            [_page([
                {"record_id": "rec_1", "fields": {"email": FAKE_EMAIL, "token_cipher": "a"}},
                {"record_id": "rec_2", "fields": {"email": FAKE_EMAIL, "token_cipher": "b"}},
            ])]
        )
        with self.assertRaises(StockTokenSourceError) as caught:
            reader.lookup_raw(FAKE_EMAIL)
        self.assertEqual(caught.exception.code, "multiple_rows_matched")

    def test_blank_email_is_rejected(self) -> None:
        reader, transport = _reader([])
        with self.assertRaises(ValueError):
            reader.lookup_raw("   ")
        self.assertEqual(transport.calls, [])

    def test_token_goes_to_header_never_to_url(self) -> None:
        reader, transport = _reader([_page([])])
        reader.lookup_raw(FAKE_EMAIL)
        call = transport.calls[0]
        self.assertEqual(call["token"], TOKEN)
        self.assertNotIn(TOKEN, call["url"])
        self.assertEqual(call["method"], "GET")
        self.assertIn(f"/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/records?", call["url"])

    def test_pagination_follows_the_cursor(self) -> None:
        reader, transport = _reader(
            [
                _page([{"record_id": "rec_1", "fields": {"email": "other@x.invalid"}}],
                      has_more=True, page_token="p2"),
                _page([{"record_id": "rec_2", "fields": {"email": FAKE_EMAIL}}]),
            ]
        )
        row = reader.lookup_raw(FAKE_EMAIL)
        self.assertIsNotNone(row)
        self.assertIn("page_token=p2", transport.calls[1]["url"])

    def test_stalled_cursor_is_indeterminate(self) -> None:
        reader, _ = _reader([_page([], has_more=True, page_token=None)])
        with self.assertRaises(StockTokenSourceError) as caught:
            reader.lookup_raw(FAKE_EMAIL)
        self.assertEqual(caught.exception.code, "pagination_stalled")
        self.assertFalse(caught.exception.definite)

    def test_pagination_limit_is_an_error(self) -> None:
        pages = [_page([], has_more=True, page_token=f"p{index}") for index in range(3)]
        reader, _ = _reader(pages, max_pages=2)
        with self.assertRaises(StockTokenSourceError) as caught:
            reader.lookup_raw(FAKE_EMAIL)
        self.assertEqual(caught.exception.code, "pagination_limit")

    def test_non_object_page_item_is_rejected(self) -> None:
        reader, _ = _reader([{"code": 0, "data": {"items": ["not-an-object"]}}])
        with self.assertRaises(StockTokenSourceError) as caught:
            reader.lookup_raw(FAKE_EMAIL)
        self.assertEqual(caught.exception.code, "invalid_page_item")

    def test_empty_table_without_items_key_is_not_a_failure(self) -> None:
        reader, _ = _reader([{"code": 0, "data": {}}])
        self.assertIsNone(reader.lookup_raw(FAKE_EMAIL))


class FailureClassificationTest(unittest.TestCase):
    def test_business_error_code_is_definite(self) -> None:
        reader, _ = _reader([{"code": 91403, "msg": "Forbidden"}])
        with self.assertRaises(StockTokenSourceError) as caught:
            reader.lookup_raw(FAKE_EMAIL)
        self.assertEqual(caught.exception.code, "feishu_code_91403")
        self.assertTrue(caught.exception.definite)

    def test_transport_error_stays_indeterminate(self) -> None:
        reader, _ = _reader([FeishuDirectoryError("transport_error")])
        with self.assertRaises(StockTokenSourceError) as caught:
            reader.lookup_raw(FAKE_EMAIL)
        self.assertEqual(caught.exception.code, "transport_error")
        self.assertFalse(caught.exception.definite)

    def test_missing_token_is_indeterminate_and_sends_nothing(self) -> None:
        transport = RecordingTransport([])
        reader = BitableStockTokenSource(
            base_url=BASE_URL,
            app_token=APP_TOKEN,
            table_id=TABLE_ID,
            access_token=lambda: "",
            transport=transport,
        )
        with self.assertRaises(StockTokenSourceError) as caught:
            reader.lookup_raw(FAKE_EMAIL)
        self.assertEqual(caught.exception.code, "access_token_missing")
        self.assertEqual(transport.calls, [])

    def test_error_message_does_not_echo_identifiers_or_token(self) -> None:
        reader, _ = _reader([{"code": 91403, "msg": "Forbidden"}])
        with self.assertRaises(StockTokenSourceError) as caught:
            reader.lookup_raw(FAKE_EMAIL)
        message = str(caught.exception)
        self.assertNotIn(APP_TOKEN, message)
        self.assertNotIn(TABLE_ID, message)
        self.assertNotIn(TOKEN, message)


class DecryptingSourceTest(unittest.TestCase):
    """组合层：三态原始事实翻成 core 端口的四态。"""

    def test_no_row_passes_through(self) -> None:
        reader, _ = _reader([_page([])])
        source = DecryptingStockTokenSource(reader, cipher=McpTokenCipher(SPEC_MASTER_KEY))
        self.assertEqual(source.lookup(FAKE_EMAIL).state, NO_ROW)

    def test_no_cipher_passes_through_with_status(self) -> None:
        reader, _ = _reader(
            [_page([{"record_id": "rec_1", "fields": {"email": FAKE_EMAIL, "status": "pending"}}])]
        )
        source = DecryptingStockTokenSource(reader, cipher=McpTokenCipher(SPEC_MASTER_KEY))
        result = source.lookup(FAKE_EMAIL)
        self.assertEqual(result.state, NO_CIPHER)
        self.assertEqual(result.status, "pending")
        self.assertEqual(result.secret, "")

    def test_matching_key_decrypts_to_adoptable(self) -> None:
        plaintext = "stock-secret-plaintext-value"
        cipher_text = _cipher_of(plaintext)
        reader, _ = _reader(
            [_page([{"record_id": "rec_1", "fields": {
                "email": FAKE_EMAIL, "token_cipher": cipher_text, "status": "approved",
            }}])]
        )
        source = DecryptingStockTokenSource(reader, cipher=McpTokenCipher(SPEC_MASTER_KEY))
        result = source.lookup(FAKE_EMAIL)
        self.assertEqual(result.state, ADOPTABLE)
        self.assertEqual(result.secret, plaintext)
        self.assertEqual(result.status, "approved")

    def test_wrong_master_key_becomes_decrypt_failed_not_an_exception(self) -> None:
        """解密失败是端口要表达的**合法状态**，不是异常——调用方靠状态分支响亮失败。"""

        cipher_text = _cipher_of("some-plaintext", key=SPEC_MASTER_KEY)
        reader, _ = _reader(
            [_page([{"record_id": "rec_1", "fields": {
                "email": FAKE_EMAIL, "token_cipher": cipher_text, "status": "approved",
            }}])]
        )
        source = DecryptingStockTokenSource(reader, cipher=McpTokenCipher(OTHER_MASTER_KEY))
        result = source.lookup(FAKE_EMAIL)
        self.assertEqual(result.state, DECRYPT_FAILED)
        self.assertEqual(result.secret, "")

    def test_corrupted_cipher_text_becomes_decrypt_failed(self) -> None:
        reader, _ = _reader(
            [_page([{"record_id": "rec_1", "fields": {
                "email": FAKE_EMAIL, "token_cipher": "not-a-valid-envelope",
            }}])]
        )
        source = DecryptingStockTokenSource(reader, cipher=McpTokenCipher(SPEC_MASTER_KEY))
        self.assertEqual(source.lookup(FAKE_EMAIL).state, DECRYPT_FAILED)

    def test_secret_never_appears_in_repr(self) -> None:
        plaintext = "never-logged-plaintext"
        reader, _ = _reader(
            [_page([{"record_id": "rec_1", "fields": {
                "email": FAKE_EMAIL, "token_cipher": _cipher_of(plaintext),
            }}])]
        )
        source = DecryptingStockTokenSource(reader, cipher=McpTokenCipher(SPEC_MASTER_KEY))
        result = source.lookup(FAKE_EMAIL)
        self.assertNotIn(plaintext, repr(result))

    def test_rejects_a_raw_key_instead_of_a_cipher_object(self) -> None:
        reader, _ = _reader([])
        with self.assertRaises(TypeError):
            DecryptingStockTokenSource(reader, cipher=SPEC_MASTER_KEY)  # type: ignore[arg-type]

    def test_ambiguous_source_propagates_as_an_exception_not_a_state(self) -> None:
        """源端失败（多行命中/网络异常）不是四态之一，原样上抛给调用方按本侧故障收口。"""

        reader, _ = _reader(
            [_page([
                {"record_id": "rec_1", "fields": {"email": FAKE_EMAIL, "token_cipher": "a"}},
                {"record_id": "rec_2", "fields": {"email": FAKE_EMAIL, "token_cipher": "b"}},
            ])]
        )
        source = DecryptingStockTokenSource(reader, cipher=McpTokenCipher(SPEC_MASTER_KEY))
        with self.assertRaises(StockTokenSourceError):
            source.lookup(FAKE_EMAIL)


class PermissionsColumnTest(unittest.TestCase):
    """``permissions`` 列读取（rc25 S-1，Issue #540）：原文随可采纳结果透传，其余状态
    恒为 ``None``——它是存量差集导入的唯一输入，本模块只搬运不解释。"""

    def test_lookup_raw_carries_the_permissions_text(self) -> None:
        reader, _ = _reader(
            [_page([{"record_id": "rec_1", "fields": {
                "email": FAKE_EMAIL, "token_cipher": "c", "status": "approved",
                "permissions": ' {"*":["*"]} ',
            }}])]
        )
        raw = reader.lookup_raw(FAKE_EMAIL)
        self.assertEqual(raw, RawStockTokenRow(token_cipher="c", status="approved", permissions='{"*":["*"]}'))

    def test_missing_permissions_cell_reads_as_empty_text(self) -> None:
        reader, _ = _reader(
            [_page([{"record_id": "rec_1", "fields": {"email": FAKE_EMAIL, "token_cipher": "c"}}])]
        )
        self.assertEqual(reader.lookup_raw(FAKE_EMAIL).permissions, "")

    def test_adoptable_lookup_passes_permissions_through(self) -> None:
        reader, _ = _reader(
            [_page([{"record_id": "rec_1", "fields": {
                "email": FAKE_EMAIL, "token_cipher": _cipher_of("plain"), "status": "approved",
                "permissions": '{"88":["m1"]}',
            }}])]
        )
        source = DecryptingStockTokenSource(reader, cipher=McpTokenCipher(SPEC_MASTER_KEY))
        result = source.lookup(FAKE_EMAIL)
        self.assertEqual(result.state, ADOPTABLE)
        self.assertEqual(result.permissions, '{"88":["m1"]}')

    def test_only_adoptable_carries_permissions(self) -> None:
        no_cipher_reader, _ = _reader(
            [_page([{"record_id": "rec_1", "fields": {"email": FAKE_EMAIL, "permissions": '{"88":["m1"]}'}}])]
        )
        failed_reader, _ = _reader(
            [_page([{"record_id": "rec_1", "fields": {
                "email": FAKE_EMAIL, "token_cipher": "not-a-valid-envelope", "permissions": '{"88":["m1"]}',
            }}])]
        )
        for reader, expected_state in ((no_cipher_reader, NO_CIPHER), (failed_reader, DECRYPT_FAILED)):
            with self.subTest(state=expected_state):
                result = DecryptingStockTokenSource(reader, cipher=McpTokenCipher(SPEC_MASTER_KEY)).lookup(FAKE_EMAIL)
                self.assertEqual(result.state, expected_state)
                self.assertIsNone(result.permissions)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
