"""飞书应用身份令牌（``tenant_access_token``）获取（Issue #226 裁定 3）。

传输层全部注入，本文件**不发起任何真实网络请求**——真实调用属 L4a，留给
`biai-stage` + `Bot-Test`。请求路径与响应字段吸收自已受控验证过的
``scripts/sync_feishu_org_snapshot.py`` 的 ``app_access_token()``，本文件额外钉住
它没做的两件事：``expire`` 字段解析、传输错误分类。
"""

from __future__ import annotations

import unittest
from urllib.error import URLError

from lingxi.adapters.feishu_tenant_token import (
    FeishuTenantTokenClient,
    FeishuTenantTokenError,
    urllib_transport,
)
from lingxi.core.identity.credentials import DerivedAccessToken

BASE_URL = "https://feishu.invalid/open-apis"
FAKE_APP_SECRET = "fake-app-secret-for-tests-only"


class RecordingTransport:
    """按顺序返回预置响应，并记录每次调用。"""

    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []

    def __call__(self, method: str, url: str, *, body=None):
        self.calls.append((method, url, body))
        if not self._responses:
            raise AssertionError("假传输层收到了超出预置数量的调用")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class ClientConstructionTest(unittest.TestCase):
    def test_a_non_https_base_url_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            FeishuTenantTokenClient(
                base_url="http://feishu.invalid/open-apis",
                app_id="cli_fake",
                app_secret=FAKE_APP_SECRET,
            )

    def test_missing_credentials_are_rejected_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            FeishuTenantTokenClient(base_url=BASE_URL, app_id="", app_secret=FAKE_APP_SECRET)
        with self.assertRaises(ValueError):
            FeishuTenantTokenClient(base_url=BASE_URL, app_id="cli_fake", app_secret="")


class FetchTest(unittest.TestCase):
    def test_a_successful_fetch_returns_a_derived_token_with_its_lifetime(self) -> None:
        transport = RecordingTransport(
            [{"code": 0, "tenant_access_token": "t-fake-tenant-token", "expire": 7200}]
        )
        client = FeishuTenantTokenClient(
            base_url=BASE_URL, app_id="cli_fake", app_secret=FAKE_APP_SECRET, transport=transport
        )

        derived = client.fetch()

        self.assertIsInstance(derived, DerivedAccessToken)
        self.assertEqual(derived.token.reveal(), "t-fake-tenant-token")
        self.assertEqual(derived.expires_in, 7200)
        self.assertEqual(len(transport.calls), 1)
        method, url, body = transport.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/auth/v3/tenant_access_token/internal"))
        assert body is not None
        self.assertEqual(body["app_id"], "cli_fake")
        self.assertEqual(body["app_secret"], FAKE_APP_SECRET)

    def test_the_app_secret_never_appears_in_the_url(self) -> None:
        transport = RecordingTransport(
            [{"code": 0, "tenant_access_token": "t-fake-tenant-token", "expire": 7200}]
        )
        client = FeishuTenantTokenClient(
            base_url=BASE_URL, app_id="cli_fake", app_secret=FAKE_APP_SECRET, transport=transport
        )

        client.fetch()

        _, url, _ = transport.calls[0]
        self.assertNotIn(FAKE_APP_SECRET, url)

    def test_a_feishu_business_error_code_is_rejected(self) -> None:
        transport = RecordingTransport([{"code": 10003, "msg": "invalid app_secret"}])
        client = FeishuTenantTokenClient(
            base_url=BASE_URL, app_id="cli_fake", app_secret=FAKE_APP_SECRET, transport=transport
        )

        with self.assertRaises(FeishuTenantTokenError) as raised:
            client.fetch()

        self.assertEqual(raised.exception.code, "feishu_code_10003")
        self.assertNotIn(FAKE_APP_SECRET, str(raised.exception))

    def test_a_missing_token_is_rejected(self) -> None:
        transport = RecordingTransport([{"code": 0, "expire": 7200}])
        client = FeishuTenantTokenClient(
            base_url=BASE_URL, app_id="cli_fake", app_secret=FAKE_APP_SECRET, transport=transport
        )

        with self.assertRaises(FeishuTenantTokenError) as raised:
            client.fetch()

        self.assertEqual(raised.exception.code, "tenant_access_token_missing")

    def test_a_missing_or_invalid_lifetime_is_rejected(self) -> None:
        """与专用授权续期不同：这里没有别的东西需要抢救（没有 refresh_token 要落盘），
        寿命缺失就直接拒绝，不把判断推给缓存层。"""

        for bad_lifetime in (
            {},
            {"expire": 0},
            {"expire": "7200"},
            {"expire": True},
            {"expire": -1},
        ):
            with self.subTest(bad_lifetime=bad_lifetime):
                transport = RecordingTransport(
                    [{"code": 0, "tenant_access_token": "t-fake-tenant-token", **bad_lifetime}]
                )
                client = FeishuTenantTokenClient(
                    base_url=BASE_URL,
                    app_id="cli_fake",
                    app_secret=FAKE_APP_SECRET,
                    transport=transport,
                )

                with self.assertRaises(FeishuTenantTokenError) as raised:
                    client.fetch()
                self.assertEqual(raised.exception.code, "tenant_access_token_lifetime_missing")

    def test_every_call_is_a_fresh_request_no_caching_here(self) -> None:
        """本适配器**不缓存**——缓存在 core.permission.tenant_token_supply。"""

        transport = RecordingTransport(
            [
                {"code": 0, "tenant_access_token": "t-one", "expire": 7200},
                {"code": 0, "tenant_access_token": "t-two", "expire": 7200},
            ]
        )
        client = FeishuTenantTokenClient(
            base_url=BASE_URL, app_id="cli_fake", app_secret=FAKE_APP_SECRET, transport=transport
        )

        first = client.fetch()
        second = client.fetch()

        self.assertEqual(first.token.reveal(), "t-one")
        self.assertEqual(second.token.reveal(), "t-two")
        self.assertEqual(len(transport.calls), 2)


class DefaultTransportTest(unittest.TestCase):
    """默认传输层的错误分类（不发真实请求，注入的是 ``urlopen`` 会抛出的异常类型）。"""

    def test_a_transport_error_is_classified(self) -> None:
        import lingxi.adapters.feishu_tenant_token as module

        original = module.urlopen

        def boom(*_args, **_kwargs):
            raise URLError("boom")

        module.urlopen = boom
        try:
            with self.assertRaises(FeishuTenantTokenError) as raised:
                urllib_transport(
                    "POST",
                    f"{BASE_URL}/auth/v3/tenant_access_token/internal",
                    body={"app_id": "a", "app_secret": "s"},
                )
            self.assertEqual(raised.exception.code, "transport_error")
        finally:
            module.urlopen = original


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
