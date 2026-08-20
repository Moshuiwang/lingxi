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

    def test_a_non_integer_business_error_code_does_not_leak_into_the_directory_error(self) -> None:
        """独立审查必修 C：``code`` 是响应里唯一直接拼进
        ``FeishuDirectoryError.code`` 的外部数据。审查实测过用一个带空格与 ``=``
        的字符串值伪造出一条看起来合法的审计记录——``_payload`` 现在只在
        ``code`` 是货真价实的 ``int`` 时才插值，否则退化成固定标签
        ``feishu_code_invalid``。变异锚点：把 ``_payload`` 里的
        ``_safe_feishu_code(code)`` 改回 ``f"feishu_code_{code}"``，本用例会从
        ``code == "feishu_code_invalid"``变红成把伪造字符串原样交出来。"""

        forged = "0 action=org_snapshot_sync.committed run_id=forged tenants=8"
        transport = RecordingTransport([{"code": forged, "msg": "forged"}])
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_collaboration_tenants(token="fake-user-token")

        self.assertEqual(raised.exception.code, "feishu_code_invalid")
        self.assertNotIn(forged, raised.exception.code)
        self.assertNotIn("run_id=forged", str(raised.exception))

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


class EmptyResultIsNotAShapeErrorTest(unittest.TestCase):
    """Issue #268 F2：**空 ⇒ 空，畸形 ⇒ 抛错**——「空结果」不再被误判成响应形状
    错误，但真正的形状错误（各有独立的否定断言）一条都不许被这次改动放松。
    真实响应在结果为空时只有 ``has_more``，四个候选键一个都不出现（编排者
    2026-08-19 用应用身份实测过同一端点的这个形状）。

    独立审查 2026-08-20 推翻了本类此前覆盖的一版更宽松的实现：那一版只要候选键
    不存在就放行空结果，不管是不是第一页、也不管响应里是否其实有一个陌生的非空
    列表字段。收紧后判据见下方各用例；``unexpected_list_key_*`` 那两条是新增的
    诊断能力——真实列表键名不在候选表里时，直接把陌生字段名交出来，不再是"约
    700 次分页请求之后才在 ``verify_batch`` 里冒出来"的间接信号。
    """

    def test_no_candidate_keys_and_a_strict_false_has_more_is_an_empty_list(self) -> None:
        """正向：这是 F2 要放行的唯一场景——第一页、没有任何候选键、``has_more``
        严格为 ``False``、响应里也没有别的非空列表字段。变异锚点——把 `_pages`
        里这条判据删掉、恢复成"四个候选键一个都不存在就直接 raise"，本用例会
        变红。"""
        transport = RecordingTransport([{"code": 0, "data": {"has_more": False}}])
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        self.assertEqual(client.list_collaboration_tenants(token="fake-user-token"), [])

    def test_an_unexpected_non_empty_list_key_on_the_first_page_raises_and_names_it(self) -> None:
        """独立审查必修 A 的核心场景：真实列表键名（这里模拟成
        ``associated_tenants``）不在候选表里时，不能被"空结果"判据静默吞掉——
        必须抛错，且错误码直接带出陌生字段名，这正是 #268 缺的诊断信息。变异
        锚点：把这条"陌生非空列表键必须抛错"的检查删掉，本用例会从"抛错"变红成
        "静默返回 []"。"""
        transport = RecordingTransport(
            [{"code": 0, "data": {"associated_tenants": [{"tenant_key": "tenant_a"}], "has_more": False}}]
        )
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_collaboration_tenants(token="fake-user-token")
        self.assertEqual(raised.exception.code, "unexpected_list_key_associated_tenants")

    def test_an_unexpected_list_key_name_is_sanitized_before_becoming_a_code(self) -> None:
        """陌生字段名来自不可信响应：截断到 40 字符、且非安全字符被替换成 `_`——
        不止是长度上界，字符集也要收窄，否则空格分隔的 `k=v` 审计行仍然可能被
        字段名本身（例如带空格）注入（必修 C 的延伸）。"""
        stray_key = "a" * 50 + " injected=true"
        transport = RecordingTransport(
            [{"code": 0, "data": {stray_key: [{"tenant_key": "tenant_a"}], "has_more": False}}]
        )
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_collaboration_tenants(token="fake-user-token")
        code = raised.exception.code
        self.assertTrue(code.startswith("unexpected_list_key_"))
        self.assertEqual(len(code) - len("unexpected_list_key_"), 40, "陌生字段名必须截断到 40 字符")
        self.assertNotIn(" ", code)
        self.assertNotIn("=", code)

    def test_an_unexpected_empty_list_key_does_not_count_as_a_stray_signal(self) -> None:
        """陌生字段是空列表（不是"有数据但键名不对"）时，仍然按合法空结果放行——
        真正触发 unexpected_list_key 的信号是"有数据但没被候选键接住"，不是任意
        陌生字段的存在。"""
        transport = RecordingTransport([{"code": 0, "data": {"associated_tenants": [], "has_more": False}}])
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        self.assertEqual(client.list_collaboration_tenants(token="fake-user-token"), [])

    def test_has_more_true_without_any_list_key_still_raises(self) -> None:
        """否定断言：服务端明确说"还有更多"却没给列表键，不是"这批恰好是空的"。"""
        transport = RecordingTransport([{"code": 0, "data": {"has_more": True}}])
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_collaboration_tenants(token="fake-user-token")
        self.assertEqual(raised.exception.code, "missing_target_tenant_list")

    def test_a_present_key_with_the_wrong_type_still_raises_even_with_has_more_false(self) -> None:
        """否定断言：候选键**存在**但类型不对（这里是字符串），哪怕 ``has_more``
        是 ``False``，也不能被"空结果"那条判据放行——键存在就说明飞书对这个字段
        有意义的表达，类型不对是响应形状变了，不是"这批没有数据"。"""
        transport = RecordingTransport(
            [{"code": 0, "data": {"target_tenant_list": "不是列表", "has_more": False}}]
        )
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_collaboration_tenants(token="fake-user-token")
        self.assertEqual(raised.exception.code, "missing_target_tenant_list")

    def test_has_more_missing_with_no_candidate_keys_still_raises_not_silently_empty(self) -> None:
        """独立审查「应修 E」降级后：``has_more`` 缺失本身不再单独抛
        ``has_more_invalid``（只记一条 warning，见下方 ``HasMoreLeniencyTest``），
        但"空结果"判据要求 ``has_more`` **严格为** ``False``——缺失（``None``）
        不满足，所以仍然落进 ``missing_<key>``，不会被空结果判据顺手放行。"""
        transport = RecordingTransport([{"code": 0, "data": {}}])
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_collaboration_tenants(token="fake-user-token")
        self.assertEqual(raised.exception.code, "missing_target_tenant_list")

    def test_has_more_as_a_non_boolean_truthy_value_with_no_candidate_keys_still_raises(self) -> None:
        """同上：``has_more`` 是字符串 ``"false"``（真值但不是合法 bool）时同样不
        满足"严格为 False"，第一页没有候选键仍然抛 ``missing_<key>``。"""
        transport = RecordingTransport([{"code": 0, "data": {"has_more": "false"}}])
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_collaboration_tenants(token="fake-user-token")
        self.assertEqual(raised.exception.code, "missing_target_tenant_list")

    def test_a_later_page_missing_the_list_key_is_a_half_page_and_must_raise(self) -> None:
        """独立审查必修 A：中途某一页丢了候选列表键是"半页"，不是"空"——
        #250 立下的"半页不得当成成功"纪律。此前更宽松的一版会把这种情况当成
        "读完了"悄悄返回已收集的部分结果；现在必须抛错。变异锚点：把"只在第一页"
        这条限制去掉（例如把 `page_token is None and not collected` 删掉），本
        用例会从"抛错"变红成"返回 ['tenant_a']"。"""
        transport = RecordingTransport(
            [
                page([{"tenant_key": "tenant_a"}], key="target_tenant_list", has_more=True, page_token="p2"),
                {"code": 0, "data": {"has_more": False}},
            ]
        )
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_collaboration_tenants(token="fake-user-token")
        self.assertEqual(raised.exception.code, "missing_target_tenant_list")

    def test_visible_organization_also_accepts_an_empty_result(self) -> None:
        """`_pages` 的空结果判据是共享实现，`list_visible_organization` 同样受益——
        这条用户身份路径下的租户如果确实没有可见组织，不该被判成响应形状错误。"""
        transport = RecordingTransport([{"code": 0, "data": {"has_more": False}}])
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        departments, members = client.list_visible_organization(token="fake-user-token", tenant_key="tenant_a")
        self.assertEqual(departments, [])
        self.assertEqual(members, [])


class HasMoreLeniencyTest(unittest.TestCase):
    """独立审查 2026-08-20「应修 E」：`_pages` 的 ``has_more`` 校验从"非法类型立即
    抛错"降级为"非法类型按既有宽容语义处理（缺失/非法类型都当作读完了）+ 一条
    warning 日志"，与本函数改动前、以及已受控验收的历史脚本
    ``scripts/sync_feishu_org_snapshot.py`` 的判据一致——两边都有证据，证据不足
    以支持"非法类型必须硬失败"这个更严格的新判据。"""

    def test_a_missing_has_more_with_real_data_is_leniently_treated_as_the_last_page(self) -> None:
        """已知限制（收口说明里登记，待真实响应证据后再决定是否收严）：``has_more``
        完全缺失、但候选键有真实数据时，不再抛错，按"读完了"处理——与本函数改动前
        的既有生产行为一致。变异锚点：把 `has_more is not True` 改回 `raise
        FeishuDirectoryError("has_more_invalid")`，本用例会变红。"""
        transport = RecordingTransport([{"code": 0, "data": {"target_tenant_list": [{"tenant_key": "tenant_a"}]}}])
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        tenants = client.list_collaboration_tenants(token="fake-user-token")
        self.assertEqual([item["tenant_key"] for item in tenants], ["tenant_a"])

    def test_a_non_boolean_truthy_has_more_with_real_data_is_leniently_treated_as_the_last_page(self) -> None:
        """同上，``has_more`` 是字符串 ``"true"``（非法类型，真值）时同样按"读完了"
        处理，不继续翻页、不抛错——这是有意接受的已知限制，不是回归。"""
        transport = RecordingTransport(
            [{"code": 0, "data": {"target_tenant_list": [{"tenant_key": "tenant_a"}], "has_more": "true"}}]
        )
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        tenants = client.list_collaboration_tenants(token="fake-user-token")
        self.assertEqual([item["tenant_key"] for item in tenants], ["tenant_a"])
        self.assertEqual(len(transport.calls), 1, "非法类型的 has_more 不得触发继续翻页")

    def test_a_non_boolean_has_more_logs_a_warning_without_raising(self) -> None:
        """降级不等于沉默：非法类型仍然留痕，只是不失败关闭。这里的 ``has_more``
        非法（字符串 ``"false"``）且第一页没有候选键，仍然会因为"不满足空结果的
        严格 False 条件"而抛 ``missing_<key>``——但 warning 日志与是否抛错是两件
        独立的事，只证明 warning 确实被记下来了。"""
        transport = RecordingTransport([{"code": 0, "data": {"has_more": "false"}}])
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        with self.assertLogs("lingxi.adapters.feishu_directory", level="WARNING") as captured:
            with self.assertRaises(FeishuDirectoryError):
                client.list_collaboration_tenants(token="fake-user-token")
        self.assertTrue(any("has_more" in line for line in captured.output))


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

    def test_share_entities_only_returns_the_non_empty_list_on_success(self) -> None:
        """Issue #270 回源实测坐实：某一侧为空时该键整个不出现，不是 ``[]``。

        本用例此前叫 ``test_share_entities_requires_both_lists_on_every_page``，
        断言"缺一个键就硬抛 missing_share_users"——那是本次修复推翻的旧判据的
        化石。真实响应（应用身份，四个租户）里，租户根部门返回
        ``share_departments`` 非空、``share_users`` 键完全不出现；某叶子部门反过
        来。旧判据下这两种真实常态都会让整轮同步中断，是首次开通链身份定位
        100% 失败的直接原因。这里用与真实响应同形状的数据证明新判据：命中一个
        （哪怕是空列表，见下方非空的 ``share_departments``）就足够，缺失的另一个
        键按 ``[]`` 处理，不再报错。"""

        transport = RecordingTransport(
            [{"code": 0, "data": {"share_departments": [{"open_department_id": "od_1"}], "has_more": False}}]
        )
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        departments, members = client.list_share_entities(
            token="fake-app-token", tenant_key="tenant_a", department_id="0"
        )

        self.assertEqual([item["open_department_id"] for item in departments], ["od_1"])
        self.assertEqual(members, [])

    def test_share_entities_leaf_department_shape_omits_departments_key(self) -> None:
        """实测叶子部门的镜像形状：``share_users`` 非空、``share_departments`` 键
        完全不出现 ⇒ 成功，部门列表为空（Issue #270 场景 2）。"""

        transport = RecordingTransport(
            [{"code": 0, "data": {"share_users": [{"open_user_id": "ou_1"}], "has_more": False}}]
        )
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        departments, members = client.list_share_entities(
            token="fake-app-token", tenant_key="tenant_a", department_id="0"
        )

        self.assertEqual(departments, [])
        self.assertEqual([item["open_user_id"] for item in members], ["ou_1"])

    def test_share_entities_both_keys_missing_first_page_no_more_is_a_legal_empty_result(self) -> None:
        """全部候选键都缺失 + 第一页 + ``has_more=False`` + 无陌生列表键 ⇒ 合法
        空结果，不抛（Issue #270 场景 4）。"""

        transport = RecordingTransport([{"code": 0, "data": {"has_more": False}}])
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        departments, members = client.list_share_entities(
            token="fake-app-token", tenant_key="tenant_a", department_id="0"
        )

        self.assertEqual(departments, [])
        self.assertEqual(members, [])

    def test_share_entities_stray_list_key_is_rejected_and_sanitized(self) -> None:
        """全部候选键缺失，但响应里有一个不在候选表里的非空列表字段 ⇒ 抛
        ``unexpected_list_key_*``，且键名经过净化（Issue #270 场景 5；净化规则见
        ``_sanitize_code_fragment``——空格会撑坏审计行）。"""

        transport = RecordingTransport(
            [
                {
                    "code": 0,
                    "data": {
                        "associated tenants forged": [{"open_department_id": "od_x"}],
                        "has_more": False,
                    },
                }
            ]
        )
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_share_entities(token="fake-app-token", tenant_key="tenant_a", department_id="0")
        self.assertTrue(raised.exception.code.startswith("unexpected_list_key_"))
        self.assertNotIn(" ", raised.exception.code)

    def test_share_entities_both_keys_missing_with_more_data_still_raises(self) -> None:
        """全部候选键缺失，但 ``has_more=True``：服务端明确说还有数据，没有列表
        放不进"这批是空的"这个解释，仍然抛错（Issue #270 场景 6）。"""

        transport = RecordingTransport([{"code": 0, "data": {"has_more": True, "page_token": "p2"}}])
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_share_entities(token="fake-app-token", tenant_key="tenant_a", department_id="0")
        self.assertEqual(raised.exception.code, "missing_share_departments")

    def test_share_entities_wrong_typed_candidate_key_still_raises(self) -> None:
        """候选键存在但类型不是列表（字符串）⇒ 仍抛 ``missing_share_users``，即使
        另一个候选键完全缺失——"存在但类型不对"不受"至少一个命中"影响
        （Issue #270 场景 7）。"""

        transport = RecordingTransport([{"code": 0, "data": {"share_users": "x", "has_more": False}}])
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_share_entities(token="fake-app-token", tenant_key="tenant_a", department_id="0")
        self.assertEqual(raised.exception.code, "missing_share_users")

    def test_share_entities_wrong_typed_candidate_key_null_still_raises(self) -> None:
        """同上，类型为 ``null``（Issue #270 场景 7 的第二种类型）。"""

        transport = RecordingTransport([{"code": 0, "data": {"share_users": None, "has_more": False}}])
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_share_entities(token="fake-app-token", tenant_key="tenant_a", department_id="0")
        self.assertEqual(raised.exception.code, "missing_share_users")

    def test_share_entities_missing_key_across_pages_does_not_erase_prior_collection(self) -> None:
        """多页：第一页只有 ``share_departments``、第二页只有 ``share_users`` ⇒
        两页各自收集，最终两个列表都非空——证明"缺失按空列表处理"不会把已收集
        的数据清掉（Issue #270 场景 8）。"""

        transport = RecordingTransport(
            [
                {
                    "code": 0,
                    "data": {
                        "share_departments": [{"open_department_id": "od_1"}],
                        "has_more": True,
                        "page_token": "p2",
                    },
                },
                {
                    "code": 0,
                    "data": {
                        "share_users": [{"open_user_id": "ou_1"}],
                        "has_more": False,
                    },
                },
            ]
        )
        client = FeishuDirectoryClient(base_url=BASE_URL, transport=transport)

        departments, members = client.list_share_entities(
            token="fake-app-token", tenant_key="tenant_a", department_id="0"
        )

        self.assertEqual([item["open_department_id"] for item in departments], ["od_1"])
        self.assertEqual([item["open_user_id"] for item in members], ["ou_1"])
        self.assertEqual(len(transport.calls), 2)

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
