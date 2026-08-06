"""飞书目录 API 客户端壳的形状测试：分页、错误映射、凭据不外泄。

**这些用例证明的是本侧的形状，不是飞书的行为。** 真实调用属 L4a，留给
`biai-stage` + `Bot-Test`；这里的传输层是注入的假实现，不发起任何网络请求。
"""

from __future__ import annotations

import logging
import unittest
from urllib.parse import parse_qs, urlparse

from lingxi.adapters.feishu_directory import (
    FeishuAuthorizationClient,
    FeishuDirectoryClient,
    FeishuDirectoryError,
    department_identifier,
)
from lingxi.core.identity.credentials import AuthorizationGrant, SecretToken
from lingxi.core.identity.first_contact import EmploymentStatus


BASE_URL = "https://feishu.invalid/open-apis"
FAKE_TOKEN = "fake-refresh-token-for-tests-only"


class RecordingTransport:
    """按 (方法, 路径关键字) 顺序返回预置响应，并记录每次调用。"""

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


def page(items: list[dict[str, object]], *, key: str, has_more: bool = False, page_token: str | None = None) -> dict[str, object]:
    data: dict[str, object] = {key: items, "has_more": has_more}
    if page_token:
        data["page_token"] = page_token
    return {"code": 0, "data": data}


class DirectoryPaginationTest(unittest.TestCase):
    def test_all_pages_are_followed_until_has_more_is_false(self) -> None:
        transport = RecordingTransport(
            [
                page([{"tenant_key": "tenant_a"}], key="target_tenant_list", has_more=True, page_token="p2"),
                page([{"tenant_key": "tenant_b"}], key="target_tenant_list"),
            ]
        )
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        tenants = client.list_collaboration_tenants(token="fake-user-token")

        self.assertEqual([item["tenant_key"] for item in tenants], ["tenant_a", "tenant_b"])
        self.assertEqual(len(transport.calls), 2)
        self.assertIn("page_token=p2", transport.calls[1][1])

    def test_a_repeated_page_token_raises_instead_of_returning_partial_data(self) -> None:
        """has_more=true 但游标停滞：服务端明确说还有数据，把半截结果当成功返回
        会让调用方拿它替换旧快照（Codex 复查发现，语义由「静默截断」改为报错）。"""
        transport = RecordingTransport(
            [
                page([{"tenant_key": "tenant_a"}], key="target_tenant_list", has_more=True, page_token="same"),
                page([{"tenant_key": "tenant_b"}], key="target_tenant_list", has_more=True, page_token="same"),
            ]
        )
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        with self.assertRaises(FeishuDirectoryError) as context:
            client.list_collaboration_tenants(token="fake-user-token")
        self.assertEqual(context.exception.code, "pagination_stalled")

    def test_the_real_probe_field_name_is_accepted(self) -> None:
        """真实探针（verify_feishu_association.sh）实测主字段是 target_tenant_list；
        此前的 collaboration_tenant_list 在真实响应里不存在（Codex 复查发现）。"""
        transport = RecordingTransport([page([{"tenant_key": "tenant_a"}], key="target_tenant_list")])
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        self.assertEqual(client.list_collaboration_tenants(token="fake-user-token")[0]["tenant_key"], "tenant_a")

    def test_a_non_object_page_item_is_a_shape_error(self) -> None:
        """终轮 Codex：静默丢弃畸形项会让被丢的租户躲过完整性校验。"""
        transport = RecordingTransport([page([{"tenant_key": "tenant_a"}, "碎片"], key="target_tenant_list")])
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        with self.assertRaises(FeishuDirectoryError) as context:
            client.list_collaboration_tenants(token="fake-user-token")
        self.assertEqual(context.exception.code, "invalid_page_item")

    def test_an_http_base_url_is_rejected_before_any_request(self) -> None:
        """飞书出站必须 HTTPS：误配 http:// 会把 Bearer token 与 App Secret
        明文上路（Codex 复查发现）。"""
        with self.assertRaises(ValueError):
            FeishuDirectoryClient(base_url="http://open.feishu.invalid", transport=RecordingTransport([]))

    def test_a_business_error_code_is_mapped_to_a_directory_error(self) -> None:
        transport = RecordingTransport([{"code": 99991663, "msg": "permission denied"}])
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_collaboration_tenants(token="fake-user-token")

        self.assertEqual(raised.exception.code, "feishu_code_99991663")

    def test_a_response_that_is_not_a_mapping_is_refused(self) -> None:
        transport = RecordingTransport([["not", "a", "mapping"]])
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        with self.assertRaises(FeishuDirectoryError):
            client.list_collaboration_tenants(token="fake-user-token")

    def test_visible_organization_separates_departments_from_members(self) -> None:
        entities = [
            {"open_department_id": "od_1", "name": "测试部门"},
            {"open_id": "ou_1", "user_id": "user_1", "union_id": "union_1", "name": "张一"},
        ]
        transport = RecordingTransport([page(entities, key="collaboration_entity_list")])
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        departments, members = client.list_visible_organization(token="fake-user-token", tenant_key="tenant_a")

        self.assertEqual([item["open_department_id"] for item in departments], ["od_1"])
        self.assertEqual([item["open_id"] for item in members], ["ou_1"])

    def test_open_department_id_value_and_type_survive_every_page(self) -> None:
        """#16 B3：下钻 open_department_id 时，值和类型必须在全部分页请求中同行。"""

        transport = RecordingTransport(
            [
                page([], key="collaboration_entity_list", has_more=True, page_token="p2"),
                page([{"open_id": "ou_1"}], key="collaboration_entity_list"),
            ]
        )
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        _, members = client.list_visible_organization(
            token="fake-user-token",
            tenant_key="tenant_a",
            department_id="od_child",
            department_id_type="open_department_id",
        )

        self.assertEqual([item["open_id"] for item in members], ["ou_1"])
        self.assertEqual(len(transport.calls), 2)
        for _, url, _, _ in transport.calls:
            query = parse_qs(urlparse(url).query)
            self.assertEqual(query["target_department_id"], ["od_child"])
            self.assertEqual(query["department_id_type"], ["open_department_id"])
        self.assertEqual(parse_qs(urlparse(transport.calls[1][1]).query)["page_token"], ["p2"])

    def test_department_id_value_and_type_are_sent_together(self) -> None:
        """飞书也可能只返回 department_id；不得把它误标成 open_department_id。"""

        transport = RecordingTransport([page([], key="collaboration_entity_list")])
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        client.list_visible_organization(
            token="fake-user-token",
            tenant_key="tenant_a",
            department_id="dept_child",
            department_id_type="department_id",
        )

        query = parse_qs(urlparse(transport.calls[0][1]).query)
        self.assertEqual(query["target_department_id"], ["dept_child"])
        self.assertEqual(query["department_id_type"], ["department_id"])

    def test_department_id_and_type_cannot_be_supplied_separately(self) -> None:
        """缺一项就在出站前失败，不能让飞书按默认 ID 类型猜测。"""

        for department_id, department_id_type in (
            ("od_child", None),
            (None, "open_department_id"),
            ("od_child", "tenant_department_id"),
        ):
            with self.subTest(department_id=department_id, department_id_type=department_id_type):
                transport = RecordingTransport([])
                client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)
                with self.assertRaises(ValueError):
                    client.list_visible_organization(
                        token="fake-user-token",
                        tenant_key="tenant_a",
                        department_id=department_id,
                        department_id_type=department_id_type,
                    )
                self.assertEqual(transport.calls, [], "非法 ID 对不得发出真实请求")

    def test_department_identifier_keeps_the_value_and_its_source_type(self) -> None:
        self.assertEqual(
            department_identifier({"open_department_id": "od_1", "department_id": "dept_1"}),
            ("od_1", "open_department_id"),
        )
        self.assertEqual(department_identifier({"department_id": "dept_1"}), ("dept_1", "department_id"))
        self.assertIsNone(department_identifier({"name": "测试部门"}))

    def test_visible_organization_business_error_stays_a_failure(self) -> None:
        """补齐参数只修正确请求；飞书明确失败仍按原语义抛错，不自动重试。"""

        transport = RecordingTransport([{"code": 40001, "msg": "bad department id type"}])
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_visible_organization(
                token="fake-user-token",
                tenant_key="tenant_a",
                department_id="od_child",
                department_id_type="open_department_id",
            )

        self.assertEqual(raised.exception.code, "feishu_code_40001")
        self.assertEqual(len(transport.calls), 1)

    def test_member_detail_and_employment_fields_are_not_changed(self) -> None:
        detail = {
            "open_id": "ou_1",
            "status": {
                "is_activated": True,
                "is_exited": False,
                "is_frozen": False,
                "is_resigned": False,
                "is_unjoin": False,
            },
        }
        transport = RecordingTransport([{"code": 0, "data": {"target_user": detail}}])
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        returned = client.get_member_detail(
            token="fake-user-token", tenant_key="tenant_a", member_id="ou_1", id_type="open_id"
        )

        self.assertEqual(returned, detail)
        employment = EmploymentStatus.from_feishu(returned["status"])
        self.assertIsNotNone(employment)
        assert employment is not None
        self.assertTrue(employment.employed)
        query = parse_qs(urlparse(transport.calls[0][1]).query)
        self.assertEqual(query["target_user_id_type"], ["open_id"])

    def test_the_tenant_key_is_a_call_argument_not_client_configuration(self) -> None:
        """硬约束 1：租户是查出来的结果，客户端不持有目标租户配置。"""
        import inspect

        self.assertNotIn("tenant_key", inspect.signature(FeishuDirectoryClient.__init__).parameters)
        self.assertIn("tenant_key", inspect.signature(FeishuDirectoryClient.list_visible_organization).parameters)


class AuthorizationClientTest(unittest.TestCase):
    def test_a_successful_refresh_returns_a_wrapped_secret(self) -> None:
        transport = RecordingTransport(
            [{"code": 0, "access_token": "fake-access", "refresh_token": "fake-next", "refresh_token_expires_in": 604800, "scope": "offline_access"}]
        )
        client = FeishuAuthorizationClient(base_url=BASE_URL, app_id="cli_fake", app_secret="secret_fake", transport=transport)

        grant, access_token = client.refresh(AuthorizationGrant(SecretToken(FAKE_TOKEN), 3600))

        self.assertEqual(grant.refresh_token.reveal(), "fake-next")
        self.assertEqual(grant.refresh_token_expires_in, 604800)
        self.assertEqual(grant.scope, "offline_access")
        self.assertEqual(access_token.reveal(), "fake-access")

    def test_an_incomplete_response_is_indeterminate_rather_than_silently_accepted(self) -> None:
        incomplete = (
            {"code": 0, "access_token": "fake-access", "refresh_token_expires_in": 604800},
            {"code": 0, "access_token": "fake-access", "refresh_token": "fake-next"},
            {"code": 0, "access_token": "fake-access", "refresh_token": "fake-next", "refresh_token_expires_in": 0},
            {"code": 0, "refresh_token": "fake-next", "refresh_token_expires_in": 604800},
        )
        for response in incomplete:
            with self.subTest(response=sorted(response)):
                client = FeishuAuthorizationClient(
                    base_url=BASE_URL, app_id="cli_fake", app_secret="secret_fake", transport=RecordingTransport([response])
                )
                with self.assertRaises(FeishuDirectoryError):
                    client.refresh(AuthorizationGrant(SecretToken(FAKE_TOKEN), 3600))

    def test_credentials_never_appear_in_the_url_or_in_logs(self) -> None:
        transport = RecordingTransport(
            [{"code": 0, "access_token": "fake-access", "refresh_token": "fake-next", "refresh_token_expires_in": 604800}]
        )
        client = FeishuAuthorizationClient(base_url=BASE_URL, app_id="cli_fake", app_secret="secret_fake", transport=transport)

        with self.assertLogs("lingxi.adapters.feishu_directory", level=logging.INFO) as captured:
            client.refresh(AuthorizationGrant(SecretToken(FAKE_TOKEN), 3600))

        method, url, body, token = transport.calls[0]
        self.assertEqual(method, "POST")
        self.assertNotIn(FAKE_TOKEN, url)
        self.assertNotIn("secret_fake", url)
        self.assertIsNone(token)
        assert body is not None
        self.assertEqual(body["refresh_token"], FAKE_TOKEN)  # 凭据只在请求体里
        for line in captured.output:
            for secret in (FAKE_TOKEN, "secret_fake", "fake-next", "fake-access"):
                self.assertNotIn(secret, line)

    def test_the_base_url_is_injected_so_nothing_is_hardcoded(self) -> None:
        """V-部署-01：主机名来自配置，不写死在代码里。"""
        import inspect

        parameters = inspect.signature(FeishuAuthorizationClient.__init__).parameters
        self.assertIn("base_url", parameters)
        self.assertIs(parameters["base_url"].default, inspect.Parameter.empty)


if __name__ == "__main__":
    unittest.main()
