"""飞书目录 API 客户端壳的形状测试：分页、错误映射、凭据不外泄。

**这些用例证明的是本侧的形状，不是飞书的行为。** 真实调用属 L4a，留给
`biai-stage` + `Bot-Test`；这里的传输层是注入的假实现，不发起任何网络请求。
"""

from __future__ import annotations

import logging
import unittest
from urllib.parse import parse_qs, urlparse

from lingxi.adapters.feishu_directory import (
    AuthorizationExchange,
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


def multi_page(
    lists: dict[str, list[dict[str, object]]], *, has_more: bool = False, page_token: str | None = None
) -> dict[str, object]:
    data: dict[str, object] = {**lists, "has_more": has_more}
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


class AppIdentityPathTest(unittest.TestCase):
    """Issue #250：应用身份路径（``directory/v1/*``），只服务与用户身份路径的交叉校验。"""

    def test_list_collaboration_tenants_as_app_follows_pagination(self) -> None:
        transport = RecordingTransport(
            [
                page([{"tenant_key": "tenant_a"}], key="tenant_list", has_more=True, page_token="p2"),
                page([{"tenant_key": "tenant_b"}], key="tenant_list"),
            ]
        )
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        tenants = client.list_collaboration_tenants_as_app(token="fake-app-token")

        self.assertEqual([item["tenant_key"] for item in tenants], ["tenant_a", "tenant_b"])
        self.assertIn("/directory/v1/collaboration_tenants", transport.calls[0][1])

    def test_share_entities_splits_departments_from_users(self) -> None:
        transport = RecordingTransport(
            [
                multi_page(
                    {
                        "share_departments": [{"open_department_id": "od_1", "name": "研发部"}],
                        "share_users": [{"open_user_id": "ou_1", "name": "张一"}],
                    }
                )
            ]
        )
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        departments, members = client.list_share_entities(
            token="fake-app-token", tenant_key="tenant_a", department_id="0"
        )

        self.assertEqual([item["open_department_id"] for item in departments], ["od_1"])
        self.assertEqual([item["open_user_id"] for item in members], ["ou_1"])
        query = parse_qs(urlparse(transport.calls[0][1]).query)
        self.assertEqual(query["target_tenant_key"], ["tenant_a"])
        self.assertEqual(query["is_select_subject"], ["false"])
        self.assertEqual(query["target_department_id"], ["0"])

    def test_share_entities_requires_both_lists_on_every_page(self) -> None:
        """一页只返回其中一个列表是响应形状错误，不静默按空列表处理。"""

        transport = RecordingTransport([{"code": 0, "data": {"share_departments": [], "has_more": False}}])
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_share_entities(token="fake-app-token", tenant_key="tenant_a", department_id="0")
        self.assertEqual(raised.exception.code, "missing_share_users")

    def test_share_entities_rejects_a_non_boolean_has_more(self) -> None:
        """F4 变异锚点：``has_more`` 缺失或类型异常（例如字符串 ``"true"``）此前会被
        ``is not True`` 当成"读完了"，半截数据当成功返回。把
        ``feishu_directory.py::_pages_multi`` 的判据临时改回
        ``if data.get("has_more") is not True: return collected`` 就会让本用例变红：
        字符串 ``"true"`` 满足 ``is not True``（不同对象），会被当成最后一页直接
        返回，而不是抛错。"""

        transport = RecordingTransport(
            [
                {
                    "code": 0,
                    "data": {
                        "share_departments": [{"open_department_id": "od_1"}],
                        "share_users": [],
                        "has_more": "true",
                    },
                }
            ]
        )
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_share_entities(token="fake-app-token", tenant_key="tenant_a", department_id="0")
        self.assertEqual(raised.exception.code, "has_more_invalid")

    def test_share_entities_rejects_a_missing_has_more(self) -> None:
        transport = RecordingTransport(
            [{"code": 0, "data": {"share_departments": [], "share_users": [{"open_user_id": "ou_1"}]}}]
        )
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_share_entities(token="fake-app-token", tenant_key="tenant_a", department_id="0")
        self.assertEqual(raised.exception.code, "has_more_invalid")

    def test_share_entities_accepts_a_strict_false_as_the_last_page(self) -> None:
        """正向对照：真正的 bool ``False`` 才是"读完了"，不受 F4 修复影响。"""

        transport = RecordingTransport(
            [multi_page({"share_departments": [], "share_users": [{"open_user_id": "ou_1"}]}, has_more=False)]
        )
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        departments, members = client.list_share_entities(
            token="fake-app-token", tenant_key="tenant_a", department_id="0"
        )
        self.assertEqual([item["open_user_id"] for item in members], ["ou_1"])

    def test_share_entities_follows_pagination_across_both_lists(self) -> None:
        transport = RecordingTransport(
            [
                multi_page(
                    {
                        "share_departments": [{"open_department_id": "od_1"}],
                        "share_users": [],
                    },
                    has_more=True,
                    page_token="p2",
                ),
                multi_page({"share_departments": [], "share_users": [{"open_user_id": "ou_1"}]}),
            ]
        )
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        departments, members = client.list_share_entities(
            token="fake-app-token", tenant_key="tenant_a", department_id="0"
        )

        self.assertEqual([item["open_department_id"] for item in departments], ["od_1"])
        self.assertEqual([item["open_user_id"] for item in members], ["ou_1"])
        self.assertEqual(len(transport.calls), 2)

    def test_share_entities_rejects_an_empty_department_id(self) -> None:
        transport = RecordingTransport([])
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        with self.assertRaises(ValueError):
            client.list_share_entities(token="fake-app-token", tenant_key="tenant_a", department_id="")
        self.assertEqual(transport.calls, [], "非法请求不得发出真实调用")


class AuthorizationClientTest(unittest.TestCase):
    def test_authorization_code_exchange_returns_identity_and_only_long_term_grant(self) -> None:
        code = "fake-one-time-code"
        access_token = "fake-access-token"
        refresh_token = "fake-refresh-token"
        transport = RecordingTransport(
            [
                {
                    "code": 0,
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "refresh_token_expires_in": 604800,
                    "scope": "auth:user.id:read offline_access",
                },
                {
                    "code": 0,
                    "data": {
                        "open_id": "ou_delegated",
                        "user_id": "u_delegated",
                        "union_id": "on_delegated",
                        "name": "四达文档会议助手",
                    },
                },
            ]
        )
        client = FeishuAuthorizationClient(
            base_url=BASE_URL,
            app_id="cli_fake",
            app_secret="secret_fake",
            transport=transport,
        )

        exchanged = client.exchange_authorization_code(
            code,
            redirect_uri="https://example.test/callback",
            required_scope="auth:user.id:read offline_access",
        )

        self.assertIsInstance(exchanged, AuthorizationExchange)
        self.assertEqual(exchanged.subject_open_id, "ou_delegated")
        self.assertEqual(exchanged.grant.refresh_token.reveal(), refresh_token)
        self.assertFalse(hasattr(exchanged, "access_token"), "短期 access_token 不得成为正式结果")
        self.assertEqual(len(transport.calls), 2)
        method, url, body, token = transport.calls[0]
        self.assertEqual(method, "POST")
        self.assertNotIn(code, url)
        self.assertNotIn(refresh_token, url)
        self.assertIsNone(token)
        assert body is not None
        self.assertEqual(body["code"], code)
        self.assertEqual(transport.calls[1][3], access_token)

    def test_authorization_code_without_requested_scope_is_rejected_before_user_info(self) -> None:
        transport = RecordingTransport(
            [
                {
                    "code": 0,
                    "access_token": "fake-access-token",
                    "refresh_token": "fake-refresh-token",
                    "refresh_token_expires_in": 604800,
                    "scope": "auth:user.id:read",
                }
            ]
        )
        client = FeishuAuthorizationClient(
            base_url=BASE_URL,
            app_id="cli_fake",
            app_secret="secret_fake",
            transport=transport,
        )

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.exchange_authorization_code(
                "fake-one-time-code",
                redirect_uri="https://example.test/callback",
                required_scope="auth:user.id:read offline_access",
            )

        self.assertEqual(raised.exception.code, "scope_incomplete")
        self.assertEqual(len(transport.calls), 1, "没有可轮换授权时不得读取身份或继续写入")

    def test_authorization_code_with_offline_access_but_missing_directory_scope_is_rejected(self) -> None:
        """P1-A：只返回 offline_access 的子集授权不能写入正式凭据。"""

        required_scope = "auth:user.id:read auth:directory:readonly offline_access"
        returned_scope = "auth:user.id:read offline_access"
        transport = RecordingTransport(
            [
                {
                    "code": 0,
                    "access_token": "fake-access-token",
                    "refresh_token": "fake-refresh-token",
                    "refresh_token_expires_in": 604800,
                    "scope": returned_scope,
                }
            ]
        )
        client = FeishuAuthorizationClient(
            base_url=BASE_URL,
            app_id="cli_fake",
            app_secret="secret_fake",
            transport=transport,
        )

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.exchange_authorization_code(
                "fake-one-time-code",
                redirect_uri="https://example.test/callback",
                required_scope=required_scope,
            )

        self.assertEqual(raised.exception.code, "scope_incomplete")
        self.assertEqual(len(transport.calls), 1, "scope 不足时不得读取身份或继续写入")

    def test_authorization_code_exchange_rejects_non_https_redirect_before_network(self) -> None:
        transport = RecordingTransport([])
        client = FeishuAuthorizationClient(
            base_url=BASE_URL,
            app_id="cli_fake",
            app_secret="secret_fake",
            transport=transport,
        )

        with self.assertRaises(ValueError):
            client.exchange_authorization_code(
                "fake-one-time-code",
                redirect_uri="http://example.test/callback",
                required_scope="auth:user.id:read offline_access",
            )

        self.assertEqual(transport.calls, [])

    def test_a_successful_refresh_returns_a_wrapped_secret(self) -> None:
        transport = RecordingTransport(
            [{"code": 0, "access_token": "fake-access", "expires_in": 7200, "refresh_token": "fake-next", "refresh_token_expires_in": 604800, "scope": "offline_access"}]
        )
        client = FeishuAuthorizationClient(base_url=BASE_URL, app_id="cli_fake", app_secret="secret_fake", transport=transport)

        grant, derived = client.refresh(AuthorizationGrant(SecretToken(FAKE_TOKEN), 3600))

        self.assertEqual(grant.refresh_token.reveal(), "fake-next")
        self.assertEqual(grant.refresh_token_expires_in, 604800)
        self.assertEqual(grant.scope, "offline_access")
        self.assertEqual(derived.token.reveal(), "fake-access")
        # Issue #215：派生短期令牌的寿命此前从未被读取，日报改成按日取令牌之后，
        # 「缓存的这份还能不能用」没有别的判据。
        self.assertEqual(derived.expires_in, 7200)
        self.assertTrue(derived.lifetime_known)

    def test_a_missing_access_token_lifetime_leaves_the_credential_alive(self) -> None:
        """飞书没给短期令牌寿命时**不判整轮续期失败**（Issue #215）。

        续期成功这件事已经发生，新的一次性 ``refresh_token`` 必须照常交给调用方落盘：
        为一个附带字段把凭据判死，会让"需人工重新授权"因为一个非凭据原因而发生。
        寿命未知的后果留在令牌供给那一侧（持有者拒绝缓存 → 供给失败关闭）。
        """

        for lifetime in ({}, {"expires_in": 0}, {"expires_in": "7200"}, {"expires_in": True}):
            with self.subTest(lifetime=lifetime):
                transport = RecordingTransport(
                    [{"code": 0, "access_token": "fake-access", "refresh_token": "fake-next", "refresh_token_expires_in": 604800, **lifetime}]
                )
                client = FeishuAuthorizationClient(
                    base_url=BASE_URL, app_id="cli_fake", app_secret="secret_fake", transport=transport
                )

                grant, derived = client.refresh(AuthorizationGrant(SecretToken(FAKE_TOKEN), 3600))

                self.assertEqual(grant.refresh_token.reveal(), "fake-next")
                self.assertIsNone(derived.expires_in)
                self.assertFalse(derived.lifetime_known)

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
