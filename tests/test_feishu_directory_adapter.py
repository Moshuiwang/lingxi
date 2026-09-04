"""飞书目录 API 客户端壳的形状测试：分页、错误映射、凭据不外泄。

**这些用例证明的是本侧的形状，不是飞书的行为。** 真实调用属 L4a，留给
`biai-stage` + `Bot-Test`；这里的传输层是注入的假实现，不发起任何网络请求。
"""

from __future__ import annotations

import logging
import queue
import threading
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

from lingxi.adapters.feishu_directory import (
    FEISHU_RATE_LIMIT_ERROR_CODE,
    RATE_LIMIT_RETRY_BACKOFFS_SECONDS,
    REQUEST_PAUSE_JITTER_SECONDS,
    REQUEST_PAUSE_SECONDS,
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


def page(
    items: list[dict[str, object]],
    *,
    key: str,
    has_more: bool = False,
    page_token: str | None = None,
) -> dict[str, object]:
    data: dict[str, object] = {key: items, "has_more": has_more}
    if page_token:
        data["page_token"] = page_token
    return {"code": 0, "data": data}


def multi_page(
    lists: dict[str, list[dict[str, object]]],
    *,
    has_more: bool = False,
    page_token: str | None = None,
) -> dict[str, object]:
    data: dict[str, object] = {**lists, "has_more": has_more}
    if page_token:
        data["page_token"] = page_token
    return {"code": 0, "data": data}


def _client(transport, **overrides: object) -> FeishuDirectoryClient:
    """测试专用的客户端工厂（Issue #271）。

    生产默认构造出的 :class:`FeishuDirectoryClient` 会在每次请求前真实等待
    ``REQUEST_PAUSE_SECONDS``（节流的理由见 ``_PagedClient._throttle`` 的文档
    字符串）——这是刻意的生产默认，但 2944 个测试的套件不能被 0.12 秒 × 每次
    请求拖慢。这里默认注入一个不阻塞的假 sleeper：``request_pause_seconds``
    仍是生产默认值，``_throttle`` 仍然照常被调用，只是不真的等待，因此本文件
    里绝大多数用例仍然间接验证了"节流调用路径确实会跑"，只有
    ``RequestThrottleTest`` 需要显式断言调用次数、步长与可关闭性，那里绕开
    本工厂直接构造。"""

    overrides.setdefault("sleep", lambda seconds: None)
    return FeishuDirectoryClient(base_url=BASE_URL, transport=transport, **overrides)


class DirectoryPaginationTest(unittest.TestCase):
    def test_all_pages_are_followed_until_has_more_is_false(self) -> None:
        transport = RecordingTransport(
            [
                page(
                    [{"tenant_key": "tenant_a"}],
                    key="target_tenant_list",
                    has_more=True,
                    page_token="p2",
                ),
                page([{"tenant_key": "tenant_b"}], key="target_tenant_list"),
            ]
        )
        client = _client(transport)

        tenants = client.list_collaboration_tenants(token="fake-user-token")

        self.assertEqual([item["tenant_key"] for item in tenants], ["tenant_a", "tenant_b"])
        self.assertEqual(len(transport.calls), 2)
        self.assertIn("page_token=p2", transport.calls[1][1])

    def test_a_repeated_page_token_raises_instead_of_returning_partial_data(self) -> None:
        """has_more=true 但游标停滞：服务端明确说还有数据，把半截结果当成功返回
        会让调用方拿它替换旧快照（Codex 复查发现，语义由「静默截断」改为报错）。"""
        transport = RecordingTransport(
            [
                page(
                    [{"tenant_key": "tenant_a"}],
                    key="target_tenant_list",
                    has_more=True,
                    page_token="same",
                ),
                page(
                    [{"tenant_key": "tenant_b"}],
                    key="target_tenant_list",
                    has_more=True,
                    page_token="same",
                ),
            ]
        )
        client = _client(transport)

        with self.assertRaises(FeishuDirectoryError) as context:
            client.list_collaboration_tenants(token="fake-user-token")
        self.assertEqual(context.exception.code, "pagination_stalled")

    def test_the_real_probe_field_name_is_accepted(self) -> None:
        """真实探针（verify_feishu_association.sh）实测主字段是 target_tenant_list；
        此前的 collaboration_tenant_list 在真实响应里不存在（Codex 复查发现）。"""
        transport = RecordingTransport(
            [page([{"tenant_key": "tenant_a"}], key="target_tenant_list")]
        )
        client = _client(transport)

        self.assertEqual(
            client.list_collaboration_tenants(token="fake-user-token")[0]["tenant_key"], "tenant_a"
        )

    def test_a_non_object_page_item_is_a_shape_error(self) -> None:
        """终轮 Codex：静默丢弃畸形项会让被丢的租户躲过完整性校验。"""
        transport = RecordingTransport(
            [page([{"tenant_key": "tenant_a"}, "碎片"], key="target_tenant_list")]
        )
        client = _client(transport)

        with self.assertRaises(FeishuDirectoryError) as context:
            client.list_collaboration_tenants(token="fake-user-token")
        self.assertEqual(context.exception.code, "invalid_page_item")

    def test_an_http_base_url_is_rejected_before_any_request(self) -> None:
        """飞书出站必须 HTTPS：误配 http:// 会把 Bearer token 与 App Secret
        明文上路（Codex 复查发现）。"""
        with self.assertRaises(ValueError):
            FeishuDirectoryClient(
                base_url="http://open.feishu.invalid", transport=RecordingTransport([])
            )

    def test_a_business_error_code_is_mapped_to_a_directory_error(self) -> None:
        transport = RecordingTransport([{"code": 99991663, "msg": "permission denied"}])
        client = _client(transport)

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
        client = _client(transport)

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_collaboration_tenants(token="fake-user-token")

        self.assertEqual(raised.exception.code, "feishu_code_invalid")
        self.assertNotIn(forged, raised.exception.code)
        self.assertNotIn("run_id=forged", str(raised.exception))

    def test_a_response_that_is_not_a_mapping_is_refused(self) -> None:
        transport = RecordingTransport([["not", "a", "mapping"]])
        client = _client(transport)

        with self.assertRaises(FeishuDirectoryError):
            client.list_collaboration_tenants(token="fake-user-token")

    def test_visible_organization_separates_departments_from_members(self) -> None:
        entities = [
            {"open_department_id": "od_1", "name": "测试部门"},
            {"open_id": "ou_1", "user_id": "user_1", "union_id": "union_1", "name": "张一"},
        ]
        transport = RecordingTransport([page(entities, key="collaboration_entity_list")])
        client = _client(transport)

        departments, members = client.list_visible_organization(
            token="fake-user-token", tenant_key="tenant_a"
        )

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
        client = _client(transport)

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
        client = _client(transport)

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
                client = _client(transport)
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
        self.assertEqual(
            department_identifier({"department_id": "dept_1"}), ("dept_1", "department_id")
        )
        self.assertIsNone(department_identifier({"name": "测试部门"}))

    def test_visible_organization_business_error_stays_a_failure(self) -> None:
        """补齐参数只修正确请求；飞书明确失败仍按原语义抛错，不自动重试。"""

        transport = RecordingTransport([{"code": 40001, "msg": "bad department id type"}])
        client = _client(transport)

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
        client = _client(transport)

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
        self.assertIn(
            "tenant_key",
            inspect.signature(FeishuDirectoryClient.list_visible_organization).parameters,
        )


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
        client = _client(transport)

        self.assertEqual(client.list_collaboration_tenants(token="fake-user-token"), [])

    def test_an_unexpected_non_empty_list_key_on_the_first_page_raises_and_names_it(self) -> None:
        """独立审查必修 A 的核心场景：真实列表键名（这里模拟成
        ``associated_tenants``）不在候选表里时，不能被"空结果"判据静默吞掉——
        必须抛错，且错误码直接带出陌生字段名，这正是 #268 缺的诊断信息。变异
        锚点：把这条"陌生非空列表键必须抛错"的检查删掉，本用例会从"抛错"变红成
        "静默返回 []"。"""
        transport = RecordingTransport(
            [
                {
                    "code": 0,
                    "data": {"associated_tenants": [{"tenant_key": "tenant_a"}], "has_more": False},
                }
            ]
        )
        client = _client(transport)

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
        client = _client(transport)

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_collaboration_tenants(token="fake-user-token")
        code = raised.exception.code
        self.assertTrue(code.startswith("unexpected_list_key_"))
        self.assertEqual(
            len(code) - len("unexpected_list_key_"), 40, "陌生字段名必须截断到 40 字符"
        )
        self.assertNotIn(" ", code)
        self.assertNotIn("=", code)

    def test_an_unexpected_empty_list_key_does_not_count_as_a_stray_signal(self) -> None:
        """陌生字段是空列表（不是"有数据但键名不对"）时，仍然按合法空结果放行——
        真正触发 unexpected_list_key 的信号是"有数据但没被候选键接住"，不是任意
        陌生字段的存在。"""
        transport = RecordingTransport(
            [{"code": 0, "data": {"associated_tenants": [], "has_more": False}}]
        )
        client = _client(transport)

        self.assertEqual(client.list_collaboration_tenants(token="fake-user-token"), [])

    def test_has_more_true_without_any_list_key_still_raises(self) -> None:
        """否定断言：服务端明确说"还有更多"却没给列表键，不是"这批恰好是空的"。"""
        transport = RecordingTransport([{"code": 0, "data": {"has_more": True}}])
        client = _client(transport)

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
        client = _client(transport)

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_collaboration_tenants(token="fake-user-token")
        self.assertEqual(raised.exception.code, "missing_target_tenant_list")

    def test_has_more_missing_with_no_candidate_keys_still_raises_not_silently_empty(self) -> None:
        """独立审查「应修 E」降级后：``has_more`` 缺失本身不再单独抛
        ``has_more_invalid``（只记一条 warning，见下方 ``HasMoreLeniencyTest``），
        但"空结果"判据要求 ``has_more`` **严格为** ``False``——缺失（``None``）
        不满足，所以仍然落进 ``missing_<key>``，不会被空结果判据顺手放行。"""
        transport = RecordingTransport([{"code": 0, "data": {}}])
        client = _client(transport)

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_collaboration_tenants(token="fake-user-token")
        self.assertEqual(raised.exception.code, "missing_target_tenant_list")

    def test_has_more_as_a_non_boolean_truthy_value_with_no_candidate_keys_still_raises(
        self,
    ) -> None:
        """同上：``has_more`` 是字符串 ``"false"``（真值但不是合法 bool）时同样不
        满足"严格为 False"，第一页没有候选键仍然抛 ``missing_<key>``。"""
        transport = RecordingTransport([{"code": 0, "data": {"has_more": "false"}}])
        client = _client(transport)

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
                page(
                    [{"tenant_key": "tenant_a"}],
                    key="target_tenant_list",
                    has_more=True,
                    page_token="p2",
                ),
                {"code": 0, "data": {"has_more": False}},
            ]
        )
        client = _client(transport)

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_collaboration_tenants(token="fake-user-token")
        self.assertEqual(raised.exception.code, "missing_target_tenant_list")

    def test_visible_organization_also_accepts_an_empty_result(self) -> None:
        """`_pages` 的空结果判据是共享实现，`list_visible_organization` 同样受益——
        这条用户身份路径下的租户如果确实没有可见组织，不该被判成响应形状错误。"""
        transport = RecordingTransport([{"code": 0, "data": {"has_more": False}}])
        client = _client(transport)

        departments, members = client.list_visible_organization(
            token="fake-user-token", tenant_key="tenant_a"
        )
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
        transport = RecordingTransport(
            [{"code": 0, "data": {"target_tenant_list": [{"tenant_key": "tenant_a"}]}}]
        )
        client = _client(transport)

        tenants = client.list_collaboration_tenants(token="fake-user-token")
        self.assertEqual([item["tenant_key"] for item in tenants], ["tenant_a"])

    def test_a_non_boolean_truthy_has_more_with_real_data_is_leniently_treated_as_the_last_page(
        self,
    ) -> None:
        """同上，``has_more`` 是字符串 ``"true"``（非法类型，真值）时同样按"读完了"
        处理，不继续翻页、不抛错——这是有意接受的已知限制，不是回归。"""
        transport = RecordingTransport(
            [
                {
                    "code": 0,
                    "data": {
                        "target_tenant_list": [{"tenant_key": "tenant_a"}],
                        "has_more": "true",
                    },
                }
            ]
        )
        client = _client(transport)

        tenants = client.list_collaboration_tenants(token="fake-user-token")
        self.assertEqual([item["tenant_key"] for item in tenants], ["tenant_a"])
        self.assertEqual(len(transport.calls), 1, "非法类型的 has_more 不得触发继续翻页")

    def test_a_non_boolean_has_more_logs_a_warning_without_raising(self) -> None:
        """降级不等于沉默：非法类型仍然留痕，只是不失败关闭。这里的 ``has_more``
        非法（字符串 ``"false"``）且第一页没有候选键，仍然会因为"不满足空结果的
        严格 False 条件"而抛 ``missing_<key>``——但 warning 日志与是否抛错是两件
        独立的事，只证明 warning 确实被记下来了。"""
        transport = RecordingTransport([{"code": 0, "data": {"has_more": "false"}}])
        client = _client(transport)

        with self.assertLogs("lingxi.adapters.feishu_paged_client", level="WARNING") as captured:
            with self.assertRaises(FeishuDirectoryError):
                client.list_collaboration_tenants(token="fake-user-token")
        self.assertTrue(any("has_more" in line for line in captured.output))


class AppIdentityPathTest(unittest.TestCase):
    """Issue #250：应用身份路径（``directory/v1/*``），只服务与用户身份路径的交叉校验。"""

    def test_list_collaboration_tenants_as_app_follows_pagination(self) -> None:
        transport = RecordingTransport(
            [
                page(
                    [{"tenant_key": "tenant_a"}], key="tenant_list", has_more=True, page_token="p2"
                ),
                page([{"tenant_key": "tenant_b"}], key="tenant_list"),
            ]
        )
        client = _client(transport)

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
        client = _client(transport)

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
            [
                {
                    "code": 0,
                    "data": {
                        "share_departments": [{"open_department_id": "od_1"}],
                        "has_more": False,
                    },
                }
            ]
        )
        client = _client(transport)

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
        client = _client(transport)

        departments, members = client.list_share_entities(
            token="fake-app-token", tenant_key="tenant_a", department_id="0"
        )

        self.assertEqual(departments, [])
        self.assertEqual([item["open_user_id"] for item in members], ["ou_1"])

    def test_share_entities_both_keys_missing_first_page_no_more_is_a_legal_empty_result(
        self,
    ) -> None:
        """全部候选键都缺失 + 第一页 + ``has_more=False`` + 无陌生列表键 ⇒ 合法
        空结果，不抛（Issue #270 场景 4）。"""

        transport = RecordingTransport([{"code": 0, "data": {"has_more": False}}])
        client = _client(transport)

        departments, members = client.list_share_entities(
            token="fake-app-token", tenant_key="tenant_a", department_id="0"
        )

        self.assertEqual(departments, [])
        self.assertEqual(members, [])

    def test_share_entities_both_keys_missing_and_has_more_absent_is_a_legal_empty_result(
        self,
    ) -> None:
        """**冻结候选审查 2026-08-21 的 F3**：整页什么都没有——两个候选列表键缺失，
        连 ``has_more`` 本身也整个不出现 ⇒ 合法空结果，不抛。

        Issue #268 应修 E 记下的实测事实是「结果为空时字段整个不出现」，那条事实同样
        适用于 ``has_more`` 自己。此前这一支硬要求 ``has_more is False``，于是这种页
        会抛 ``missing_share_departments`` 让整轮作废，把 #270 修掉的形状原样退回来。

        变异验红：把 ``_pages_multi`` 空结果分支的判据改回
        ``and data.get("has_more") is False`` 之后重跑本用例，会抛
        ``FeishuDirectoryError: missing_share_departments``。
        """

        transport = RecordingTransport([{"code": 0, "data": {}}])
        client = _client(transport)

        departments, members = client.list_share_entities(
            token="fake-app-token", tenant_key="tenant_a", department_id="0"
        )

        self.assertEqual(departments, [])
        self.assertEqual(members, [])
        self.assertEqual(len(transport.calls), 1, "空结果一次请求就收工，不再翻页")

    def test_share_entities_empty_page_with_a_zero_has_more_still_raises(self) -> None:
        """F3 的**身份比较锚点**：``has_more: 0`` 不是"缺失"，也不是 ``False``。

        判据必须写成 ``x is False or x is None``。改成 ``x in (False, None)`` 会让
        本用例变绿（``0 == False`` 为真而 ``in`` 用的是 ``==``），一个数字 ``0`` 就
        冒充成了合法空结果。
        """

        transport = RecordingTransport([{"code": 0, "data": {"has_more": 0}}])
        client = _client(transport)

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_share_entities(
                token="fake-app-token", tenant_key="tenant_a", department_id="0"
            )
        self.assertEqual(raised.exception.code, "missing_share_departments")

    def test_share_entities_stray_list_key_without_has_more_is_still_rejected(self) -> None:
        """放宽只发生在"响应里真的什么列表都没有"的窄口里：``has_more`` 缺失也一样，
        有陌生的非空列表键仍然抛 ``unexpected_list_key_*``（真实字段名不在候选表里的
        直接信号，不能静默吞成空结果）。"""

        transport = RecordingTransport(
            [{"code": 0, "data": {"associated_tenants": [{"open_department_id": "od_x"}]}}]
        )
        client = _client(transport)

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_share_entities(
                token="fake-app-token", tenant_key="tenant_a", department_id="0"
            )
        self.assertEqual(raised.exception.code, "unexpected_list_key_associated_tenants")

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
        client = _client(transport)

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_share_entities(
                token="fake-app-token", tenant_key="tenant_a", department_id="0"
            )
        self.assertTrue(raised.exception.code.startswith("unexpected_list_key_"))
        self.assertNotIn(" ", raised.exception.code)

    def test_share_entities_both_keys_missing_with_more_data_still_raises(self) -> None:
        """全部候选键缺失，但 ``has_more=True``：服务端明确说还有数据，没有列表
        放不进"这批是空的"这个解释，仍然抛错（Issue #270 场景 6）。"""

        transport = RecordingTransport(
            [{"code": 0, "data": {"has_more": True, "page_token": "p2"}}]
        )
        client = _client(transport)

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_share_entities(
                token="fake-app-token", tenant_key="tenant_a", department_id="0"
            )
        self.assertEqual(raised.exception.code, "missing_share_departments")

    def test_share_entities_wrong_typed_candidate_key_still_raises(self) -> None:
        """候选键存在但类型不是列表（字符串）⇒ 仍抛 ``missing_share_users``，即使
        另一个候选键完全缺失——"存在但类型不对"不受"至少一个命中"影响
        （Issue #270 场景 7）。"""

        transport = RecordingTransport(
            [{"code": 0, "data": {"share_users": "x", "has_more": False}}]
        )
        client = _client(transport)

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_share_entities(
                token="fake-app-token", tenant_key="tenant_a", department_id="0"
            )
        self.assertEqual(raised.exception.code, "missing_share_users")

    def test_share_entities_wrong_typed_candidate_key_null_still_raises(self) -> None:
        """同上，类型为 ``null``（Issue #270 场景 7 的第二种类型）。"""

        transport = RecordingTransport(
            [{"code": 0, "data": {"share_users": None, "has_more": False}}]
        )
        client = _client(transport)

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_share_entities(
                token="fake-app-token", tenant_key="tenant_a", department_id="0"
            )
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
        client = _client(transport)

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
        client = _client(transport)

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_share_entities(
                token="fake-app-token", tenant_key="tenant_a", department_id="0"
            )
        self.assertEqual(raised.exception.code, "has_more_invalid")

    def test_share_entities_rejects_a_missing_has_more(self) -> None:
        transport = RecordingTransport(
            [
                {
                    "code": 0,
                    "data": {"share_departments": [], "share_users": [{"open_user_id": "ou_1"}]},
                }
            ]
        )
        client = _client(transport)

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_share_entities(
                token="fake-app-token", tenant_key="tenant_a", department_id="0"
            )
        self.assertEqual(raised.exception.code, "has_more_invalid")

    def test_share_entities_accepts_a_strict_false_as_the_last_page(self) -> None:
        """正向对照：真正的 bool ``False`` 才是"读完了"，不受 F4 修复影响。"""

        transport = RecordingTransport(
            [
                multi_page(
                    {"share_departments": [], "share_users": [{"open_user_id": "ou_1"}]},
                    has_more=False,
                )
            ]
        )
        client = _client(transport)

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
        client = _client(transport)

        departments, members = client.list_share_entities(
            token="fake-app-token", tenant_key="tenant_a", department_id="0"
        )

        self.assertEqual([item["open_department_id"] for item in departments], ["od_1"])
        self.assertEqual([item["open_user_id"] for item in members], ["ou_1"])
        self.assertEqual(len(transport.calls), 2)

    def test_share_entities_rejects_an_empty_department_id(self) -> None:
        transport = RecordingTransport([])
        client = _client(transport)

        with self.assertRaises(ValueError):
            client.list_share_entities(
                token="fake-app-token", tenant_key="tenant_a", department_id=""
            )
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

    def test_authorization_code_with_offline_access_but_missing_directory_scope_is_rejected(
        self,
    ) -> None:
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
            [
                {
                    "code": 0,
                    "access_token": "fake-access",
                    "expires_in": 7200,
                    "refresh_token": "fake-next",
                    "refresh_token_expires_in": 604800,
                    "scope": "offline_access",
                }
            ]
        )
        client = FeishuAuthorizationClient(
            base_url=BASE_URL, app_id="cli_fake", app_secret="secret_fake", transport=transport
        )

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
                    [
                        {
                            "code": 0,
                            "access_token": "fake-access",
                            "refresh_token": "fake-next",
                            "refresh_token_expires_in": 604800,
                            **lifetime,
                        }
                    ]
                )
                client = FeishuAuthorizationClient(
                    base_url=BASE_URL,
                    app_id="cli_fake",
                    app_secret="secret_fake",
                    transport=transport,
                )

                grant, derived = client.refresh(AuthorizationGrant(SecretToken(FAKE_TOKEN), 3600))

                self.assertEqual(grant.refresh_token.reveal(), "fake-next")
                self.assertIsNone(derived.expires_in)
                self.assertFalse(derived.lifetime_known)

    def test_an_incomplete_response_is_indeterminate_rather_than_silently_accepted(self) -> None:
        incomplete = (
            {"code": 0, "access_token": "fake-access", "refresh_token_expires_in": 604800},
            {"code": 0, "access_token": "fake-access", "refresh_token": "fake-next"},
            {
                "code": 0,
                "access_token": "fake-access",
                "refresh_token": "fake-next",
                "refresh_token_expires_in": 0,
            },
            {"code": 0, "refresh_token": "fake-next", "refresh_token_expires_in": 604800},
        )
        for response in incomplete:
            with self.subTest(response=sorted(response)):
                client = FeishuAuthorizationClient(
                    base_url=BASE_URL,
                    app_id="cli_fake",
                    app_secret="secret_fake",
                    transport=RecordingTransport([response]),
                )
                with self.assertRaises(FeishuDirectoryError):
                    client.refresh(AuthorizationGrant(SecretToken(FAKE_TOKEN), 3600))

    def test_credentials_never_appear_in_the_url_or_in_logs(self) -> None:
        transport = RecordingTransport(
            [
                {
                    "code": 0,
                    "access_token": "fake-access",
                    "refresh_token": "fake-next",
                    "refresh_token_expires_in": 604800,
                }
            ]
        )
        client = FeishuAuthorizationClient(
            base_url=BASE_URL, app_id="cli_fake", app_secret="secret_fake", transport=transport
        )

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


class RequestThrottleTest(unittest.TestCase):
    """Issue #271：组织快照全量遍历合计 550+ 次突发请求会打穿飞书的累计频率
    限制（回源实测坐实，见 ``_PagedClient._throttle`` 文档字符串）。这里覆盖
    ``_pages`` / ``_pages_multi`` / ``get_member_detail`` 三处真实请求出口，
    默认值本身生效——不靠调用方记得手动打开。"""

    def test_pages_throttles_before_every_request_with_the_default_pause(self) -> None:
        """``_pages``（单列表，``list_collaboration_tenants`` 走这条）三页
        请求 ⇒ 注入的假 sleeper 恰好被调用 3 次，每次都是生产默认步长
        ``REQUEST_PAUSE_SECONDS``——覆盖第 2、3 页，不只是第一页。"""

        pauses: list[float] = []
        transport = RecordingTransport(
            [
                page(
                    [{"tenant_key": "tenant_a"}],
                    key="target_tenant_list",
                    has_more=True,
                    page_token="p2",
                ),
                page(
                    [{"tenant_key": "tenant_b"}],
                    key="target_tenant_list",
                    has_more=True,
                    page_token="p3",
                ),
                page([{"tenant_key": "tenant_c"}], key="target_tenant_list"),
            ]
        )
        client = FeishuDirectoryClient(
            base_url=BASE_URL,
            transport=transport,
            sleep=pauses.append,
            request_pause_jitter_seconds=0,
        )

        tenants = client.list_collaboration_tenants(token="fake-user-token")

        self.assertEqual(
            [item["tenant_key"] for item in tenants], ["tenant_a", "tenant_b", "tenant_c"]
        )
        self.assertEqual(pauses, [REQUEST_PAUSE_SECONDS] * 3)

    def test_pages_multi_throttles_before_every_page(self) -> None:
        """``_pages_multi``（``share_entities`` 走这条）两页请求 ⇒ 2 次节流
        调用——递归遍历里每一层下钻都会重新进这个方法，覆盖到"每一层"不需要
        在 :mod:`feishu_org_snapshot_reader` 里另外接线。"""

        pauses: list[float] = []
        transport = RecordingTransport(
            [
                multi_page(
                    {"share_departments": [{"open_department_id": "od_1"}], "share_users": []},
                    has_more=True,
                    page_token="p2",
                ),
                multi_page({"share_departments": [], "share_users": [{"open_user_id": "ou_1"}]}),
            ]
        )
        client = FeishuDirectoryClient(
            base_url=BASE_URL,
            transport=transport,
            sleep=pauses.append,
            request_pause_jitter_seconds=0,
        )

        client.list_share_entities(token="fake-app-token", tenant_key="tenant_a", department_id="0")

        self.assertEqual(pauses, [REQUEST_PAUSE_SECONDS] * 2)

    def test_get_member_detail_throttles_once(self) -> None:
        """``get_member_detail``（在职状态实时读取）单次调用 ⇒ 1 次节流调用。"""

        pauses: list[float] = []
        transport = RecordingTransport(
            [{"code": 0, "data": {"target_user": {"status": {"is_activated": True}}}}]
        )
        client = FeishuDirectoryClient(
            base_url=BASE_URL,
            transport=transport,
            sleep=pauses.append,
            request_pause_jitter_seconds=0,
        )

        client.get_member_detail(token="fake-app-token", tenant_key="tenant_a", member_id="ou_1")

        self.assertEqual(pauses, [REQUEST_PAUSE_SECONDS])

    def test_the_default_pause_is_the_production_value_not_silently_off(self) -> None:
        """默认构造（不传 ``request_pause_seconds``）时步长必须等于生产默认
        ``REQUEST_PAUSE_SECONDS``（且该默认值本身非零），不能默认关掉靠调用方
        记得打开——否则节流形同虚设（Issue #271 判据 1）。"""

        pauses: list[float] = []
        transport = RecordingTransport(
            [page([{"tenant_key": "tenant_a"}], key="target_tenant_list")]
        )
        client = FeishuDirectoryClient(
            base_url=BASE_URL,
            transport=transport,
            sleep=pauses.append,
            request_pause_jitter_seconds=0,
        )

        client.list_collaboration_tenants(token="fake-user-token")

        self.assertGreater(REQUEST_PAUSE_SECONDS, 0)
        self.assertEqual(pauses, [REQUEST_PAUSE_SECONDS])

    def test_an_explicit_zero_pause_disables_the_sleeper_entirely(self) -> None:
        """显式把 ``request_pause_seconds`` 设成 0 ⇒ 一次都不调用 sleeper（不是
        调用 ``sleep(0)``）——证明节流可以被显式关闭（Issue #271 场景 3）。用一个
        "被调用就报错"的 sleeper 而不是记录调用次数，让这条断言更硬：即使实现
        改成"调用但传 0"也会被这里抓到。"""

        def _forbidden(seconds: float) -> None:
            raise AssertionError(f"节流关闭后不应再调用 sleeper（收到 {seconds}）")

        transport = RecordingTransport(
            [page([{"tenant_key": "tenant_a"}], key="target_tenant_list")]
        )
        client = FeishuDirectoryClient(
            base_url=BASE_URL, transport=transport, request_pause_seconds=0, sleep=_forbidden
        )

        tenants = client.list_collaboration_tenants(token="fake-user-token")

        self.assertEqual([item["tenant_key"] for item in tenants], ["tenant_a"])

    def test_the_test_suite_itself_never_calls_the_real_sleeper(self) -> None:
        """既有测试构造一律走本文件顶部的 ``_client()`` 工厂，默认注入不阻塞的
        假 sleeper（Issue #271 要求 4：套件不能被节流拖慢）。这里用
        ``unittest.mock.patch`` 顶替真实 ``time.sleep`` 并断言它一次都没被真正
        调用过，直接证伪"套件会被节流拖慢"这个顾虑，而不是只凭套件跑得快
        去推测。"""

        with mock.patch("lingxi.adapters.feishu_paged_client.time.sleep") as real_sleep:
            transport = RecordingTransport(
                [page([{"tenant_key": "tenant_a"}], key="target_tenant_list")]
            )
            client = _client(transport)
            client.list_collaboration_tenants(token="fake-user-token")
        real_sleep.assert_not_called()


class RequestPauseJitterTest(unittest.TestCase):
    """Issue #284 A 组 #3（Trace #373 D7 裁定修复）：首次开通编排的 8 条工作线程
    共享同一个 client 实例（`apps/scheduler/onboarding.py` 的
    `FeishuEmploymentReader`），固定停顿量会让并发调用同步醒来、在飞书那侧形成
    尖峰。这里覆盖抖动的默认生效、可注入、可关闭三条纪律（与 `RequestThrottleTest`
    对 `request_pause_seconds` 本身的覆盖同一个形状），外加"多条线程真的会被
    错开"的构造性证据。"""

    def test_the_default_jitter_is_a_real_margin_not_silently_off(self) -> None:
        """默认抖动上限非零——同 REQUEST_PAUSE_SECONDS 本身"默认就生效"的
        纪律，不靠调用方记得手动打开。"""

        self.assertGreater(REQUEST_PAUSE_JITTER_SECONDS, 0)
        self.assertEqual(REQUEST_PAUSE_JITTER_SECONDS, REQUEST_PAUSE_SECONDS / 2)

    def test_an_injected_random_source_at_the_lower_edge_reproduces_the_bare_pause(self) -> None:
        """注入恒返回 0.0 的 random_source ⇒ 停顿量退化到抖动区间下界，恰好等于
        不带抖动的固定步长——证明抖动是**叠加**在固定步长之上，不是替换它。"""

        pauses: list[float] = []
        transport = RecordingTransport(
            [page([{"tenant_key": "tenant_a"}], key="target_tenant_list")]
        )
        client = FeishuDirectoryClient(
            base_url=BASE_URL, transport=transport, sleep=pauses.append, random_source=lambda: 0.0
        )

        client.list_collaboration_tenants(token="fake-user-token")

        self.assertEqual(pauses, [REQUEST_PAUSE_SECONDS])

    def test_an_injected_random_source_at_the_upper_edge_adds_the_full_jitter(self) -> None:
        """注入恒返回 1.0 的 random_source ⇒ 停顿量落在抖动区间上界（固定步长
        + 完整抖动上限）。"""

        pauses: list[float] = []
        transport = RecordingTransport(
            [page([{"tenant_key": "tenant_a"}], key="target_tenant_list")]
        )
        client = FeishuDirectoryClient(
            base_url=BASE_URL, transport=transport, sleep=pauses.append, random_source=lambda: 1.0
        )

        client.list_collaboration_tenants(token="fake-user-token")

        self.assertEqual(pauses, [REQUEST_PAUSE_SECONDS + REQUEST_PAUSE_JITTER_SECONDS])

    def test_zero_jitter_disables_the_random_source_entirely(self) -> None:
        """把 ``request_pause_jitter_seconds`` 显式设成 0 ⇒ 一次都不调用
        ``random_source``（不是调用它再乘 0），停顿量退化回固定值——与
        ``request_pause_seconds`` 为假值时"完全不调用 sleeper"同一条纪律。"""

        def _forbidden() -> float:
            raise AssertionError("抖动关闭后不应再调用 random_source")

        pauses: list[float] = []
        transport = RecordingTransport(
            [page([{"tenant_key": "tenant_a"}], key="target_tenant_list")]
        )
        client = FeishuDirectoryClient(
            base_url=BASE_URL,
            transport=transport,
            sleep=pauses.append,
            request_pause_jitter_seconds=0,
            random_source=_forbidden,
        )

        client.list_collaboration_tenants(token="fake-user-token")

        self.assertEqual(pauses, [REQUEST_PAUSE_SECONDS])

    def test_concurrent_threads_sharing_one_client_wake_up_at_different_moments(self) -> None:
        """构造性证据：8 条线程共享同一个 client 实例（与开通执行器默认线程数
        同一个量级，见 ``SchedulerConfig.onboarding_workers``），用一道
        ``Barrier`` 让它们几乎同时进入节流——**默认真实随机源**下，记录到的
        停顿量不会全部相同，证明抖动确实把"同一时刻同步醒来"的尖峰打散了。
        （固定步长本身当然是恒定的：没有抖动时这 8 个值原本会逐字节相同。）"""

        worker_count = 8
        transport = RecordingTransport(
            [
                {"code": 0, "data": {"target_user": {"status": {"is_activated": True}}}}
                for _ in range(worker_count)
            ]
        )
        pauses: queue.Queue[float] = queue.Queue()
        # 不注入假 random_source：这里就是要用生产默认（`random.random`）证明
        # 真实随机源下确实会产生离散的停顿量，不是靠测试自己钉死的可预测序列。
        client = FeishuDirectoryClient(
            base_url=BASE_URL, transport=transport, sleep=lambda seconds: pauses.put(seconds)
        )
        barrier = threading.Barrier(worker_count)

        def worker() -> None:
            barrier.wait(timeout=5.0)
            client.get_member_detail(
                token="fake-app-token", tenant_key="tenant_a", member_id="ou_1"
            )

        threads = [threading.Thread(target=worker) for _ in range(worker_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5.0)
            self.assertFalse(thread.is_alive(), "节流抖动测试的工作线程未能在预算内收工")

        collected = [pauses.get_nowait() for _ in range(worker_count)]
        self.assertEqual(len(collected), worker_count)
        for value in collected:
            self.assertGreaterEqual(value, REQUEST_PAUSE_SECONDS)
            self.assertLess(value, REQUEST_PAUSE_SECONDS + REQUEST_PAUSE_JITTER_SECONDS)
        # 构造性核心断言：8 个几乎同时发起的停顿量不会全部相同（无抖动时它们
        # 原本逐字节相同——正是 A3 要解决的"同步醒来"形状）。
        self.assertGreater(len(set(collected)), 1)


def _rate_limited_response() -> dict[str, object]:
    """飞书业务层面的频率限制响应（``_payload`` 会把 ``code=99991400`` 渲染成
    ``FeishuDirectoryError("feishu_code_99991400")``，与
    ``FEISHU_RATE_LIMIT_ERROR_CODE`` 精确相等）。"""

    return {"code": 99991400, "msg": "too many requests"}


class RateLimitRetryTest(unittest.TestCase):
    """Issue #271 编排者 2026-08-20 补充的真实规模压测证据：0.12 秒节流本身余量
    不厚、配额机制未回源确认，且撞限的失败代价严重不对称（整轮 550+ 次调用作废，
    要等下一次 UTC 日界的令牌窗口）。这里只对 ``feishu_code_99991400`` 加一道
    窄而有界的重试，节流仍是主手段，重试是第二道防线。"""

    def test_a_rate_limit_error_recovers_on_retry(self) -> None:
        """第一次撞限、第二次成功 ⇒ 调用方拿到正确结果；节流与退避交替出现——
        重试前也照常先过一次基础节流，不绕开它。"""

        pauses: list[float] = []
        transport = RecordingTransport(
            [_rate_limited_response(), page([{"tenant_key": "tenant_a"}], key="target_tenant_list")]
        )
        client = FeishuDirectoryClient(
            base_url=BASE_URL,
            transport=transport,
            sleep=pauses.append,
            request_pause_jitter_seconds=0,
        )

        tenants = client.list_collaboration_tenants(token="fake-user-token")

        self.assertEqual([item["tenant_key"] for item in tenants], ["tenant_a"])
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(
            pauses,
            [REQUEST_PAUSE_SECONDS, RATE_LIMIT_RETRY_BACKOFFS_SECONDS[0], REQUEST_PAUSE_SECONDS],
        )

    def test_rate_limit_retries_are_bounded_and_eventually_raise(self) -> None:
        """持续撞限 ⇒ 用尽默认的三次退避后仍然抛出，且抛出的还是
        ``feishu_code_99991400``——重试耗尽不改变"失败就保留上一份"的语义，
        只决定"要不要再试一次"。"""

        pauses: list[float] = []
        attempts = len(RATE_LIMIT_RETRY_BACKOFFS_SECONDS) + 1
        transport = RecordingTransport([_rate_limited_response() for _ in range(attempts)])
        client = FeishuDirectoryClient(
            base_url=BASE_URL,
            transport=transport,
            sleep=pauses.append,
            request_pause_jitter_seconds=0,
        )

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_collaboration_tenants(token="fake-user-token")

        self.assertEqual(raised.exception.code, FEISHU_RATE_LIMIT_ERROR_CODE)
        self.assertEqual(len(transport.calls), attempts)
        expected_pauses: list[float] = []
        for backoff in RATE_LIMIT_RETRY_BACKOFFS_SECONDS:
            expected_pauses.append(REQUEST_PAUSE_SECONDS)
            expected_pauses.append(backoff)
        expected_pauses.append(REQUEST_PAUSE_SECONDS)
        self.assertEqual(pauses, expected_pauses)

    def test_only_the_rate_limit_code_is_retried(self) -> None:
        """否定断言：另一个业务错误码（权限拒绝）不重试，立即原样抛出——
        范围收得很窄，不是"失败就重试"。"""

        pauses: list[float] = []
        transport = RecordingTransport([{"code": 99991663, "msg": "permission denied"}])
        client = FeishuDirectoryClient(
            base_url=BASE_URL,
            transport=transport,
            sleep=pauses.append,
            request_pause_jitter_seconds=0,
        )

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_collaboration_tenants(token="fake-user-token")

        self.assertEqual(raised.exception.code, "feishu_code_99991663")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(pauses, [REQUEST_PAUSE_SECONDS])

    def test_pages_own_shape_errors_are_not_retried(self) -> None:
        """否定断言：``_pages`` 自己判定的形状错误（这里是候选键存在但类型不是
        列表——不满足 Issue #268 F2 的合法空结果条件，仍然硬抛）发生在
        ``_request`` 返回**之后**，根本不在重试的 ``try`` 覆盖范围内——不应触发
        任何退避重试，只有一次基础节流。防的是"以后有人把 try/except 的范围
        不小心扩大到整个分页循环"这类回归。"""

        pauses: list[float] = []
        transport = RecordingTransport(
            [{"code": 0, "data": {"target_tenant_list": "not-a-list", "has_more": False}}]
        )
        client = FeishuDirectoryClient(
            base_url=BASE_URL,
            transport=transport,
            sleep=pauses.append,
            request_pause_jitter_seconds=0,
        )

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_collaboration_tenants(token="fake-user-token")

        self.assertEqual(raised.exception.code, "missing_target_tenant_list")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(pauses, [REQUEST_PAUSE_SECONDS])

    def test_retry_backoffs_are_injectable(self) -> None:
        """退避序列可注入且**替换**默认序列（不是叠加）：注入只有一个 backoff
        的序列 ⇒ 恰好重试一次后仍然失败就直接抛出，不会用默认的三次。"""

        pauses: list[float] = []
        transport = RecordingTransport([_rate_limited_response(), _rate_limited_response()])
        client = FeishuDirectoryClient(
            base_url=BASE_URL,
            transport=transport,
            sleep=pauses.append,
            rate_limit_retry_backoffs=(3.0,),
            request_pause_jitter_seconds=0,
        )

        with self.assertRaises(FeishuDirectoryError) as raised:
            client.list_collaboration_tenants(token="fake-user-token")

        self.assertEqual(raised.exception.code, FEISHU_RATE_LIMIT_ERROR_CODE)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(pauses, [REQUEST_PAUSE_SECONDS, 3.0, REQUEST_PAUSE_SECONDS])


class _FakeClock:
    """可手动推进的假单调时钟，供 ``round_budget`` 测试注入。"""

    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _AdvancingTransport:
    """记录调用并在指定的第 N 次调用**返回之后**推进假时钟——用来模拟
    "这一页处理花了很久" 而不需要真的等待。"""

    def __init__(
        self, responses: list[object], *, clock: _FakeClock, advance_after: dict[int, float]
    ) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, object] | None, str | None]] = []
        self._clock = clock
        self._advance_after = advance_after

    def __call__(self, method: str, url: str, *, body=None, token=None):
        index = len(self.calls) + 1
        self.calls.append((method, url, body, token))
        if not self._responses:
            raise AssertionError("假传输层收到了超出预置数量的调用")
        response = self._responses.pop(0)
        seconds = self._advance_after.get(index)
        if seconds:
            self._clock.advance(seconds)
        if isinstance(response, Exception):
            raise response
        return response


class RoundBudgetTest(unittest.TestCase):
    """Issue #284 A 组 #2：组织快照专用 client 的整轮预算——只作用于持有它的
    这一个实例，默认关闭，撞线抛 ``round_budget_exceeded`` 且不再发出下一次
    请求。机制在这里按单元测试覆盖；装配层是否真的用上了它见
    ``tests/test_scheduler_org_snapshot_assembly.py::HardeningWiringTests``。
    """

    def test_a_round_that_finishes_within_budget_is_unaffected(self) -> None:
        """回归：预算内完成的多页请求正常返回完整数据，与不设预算时的既有断言
        （``DirectoryPaginationTest``）结果一致——`round_budget` 打开本身不改变
        任何成功路径的行为。"""

        clock = _FakeClock()
        transport = RecordingTransport(
            [
                page(
                    [{"tenant_key": "tenant_a"}],
                    key="target_tenant_list",
                    has_more=True,
                    page_token="p2",
                ),
                page([{"tenant_key": "tenant_b"}], key="target_tenant_list"),
            ]
        )
        client = FeishuDirectoryClient(
            base_url=BASE_URL,
            transport=transport,
            sleep=lambda seconds: None,
            round_deadline_clock=clock,
        )

        with client.round_budget(seconds=1200.0):
            tenants = client.list_collaboration_tenants(token="fake-user-token")

        self.assertEqual([item["tenant_key"] for item in tenants], ["tenant_a", "tenant_b"])
        self.assertEqual(len(transport.calls), 2)

    def test_exceeding_the_budget_raises_and_stops_issuing_further_requests(self) -> None:
        """撞预算 ⇒ 下一次 `_request` 必须抛 ``round_budget_exceeded``，且假
        transport 的调用计数在那之后不再增长——没有再发任何请求（设计 §5
        表格 #2 行 (a)）。"""

        clock = _FakeClock()
        transport = _AdvancingTransport(
            [
                page(
                    [{"tenant_key": "tenant_a"}],
                    key="target_tenant_list",
                    has_more=True,
                    page_token="p2",
                ),
                page([{"tenant_key": "tenant_b"}], key="target_tenant_list"),
            ],
            clock=clock,
            advance_after={1: 11.0},  # 第一页处理完之后，时间已经超过 10 秒的预算。
        )
        client = FeishuDirectoryClient(
            base_url=BASE_URL,
            transport=transport,
            sleep=lambda seconds: None,
            round_deadline_clock=clock,
        )

        with self.assertRaises(FeishuDirectoryError) as raised:
            with client.round_budget(seconds=10.0):
                client.list_collaboration_tenants(token="fake-user-token")

        self.assertEqual(raised.exception.code, "round_budget_exceeded")
        self.assertEqual(len(transport.calls), 1, "撞预算之后不应该再发出下一次请求")

    def test_round_budget_exceeded_is_an_indeterminate_failure_not_a_definite_one(self) -> None:
        """``round_budget_exceeded`` 不以 ``feishu_code_`` 开头 ⇒
        ``FeishuDirectoryError.definite`` 按既有构造逻辑自动判为 ``False``
        （"结果不明确"），与 `_pages` 的 `missing_*`/`pagination_stalled` 等
        既有分类码走同一条既定处置，不需要新增分支（设计 §2）。"""

        error = FeishuDirectoryError("round_budget_exceeded")

        self.assertFalse(error.definite)

    def test_the_budget_is_scoped_to_the_with_block_and_restored_on_exit(self) -> None:
        """`round_budget` 的 `with` 块退出后必须把截止时间还原（默认关闭）——
        块外一次远超预算秒数的调用不受影响，即便假时钟已经走到很晚（设计 §2
        「限定『从进入这个 with 块起』」）。"""

        clock = _FakeClock(start=1000.0)
        transport = RecordingTransport(
            [page([{"tenant_key": "tenant_a"}], key="target_tenant_list")]
        )
        client = FeishuDirectoryClient(
            base_url=BASE_URL,
            transport=transport,
            sleep=lambda seconds: None,
            round_deadline_clock=clock,
        )

        with client.round_budget(seconds=1.0):
            pass  # 立即退出，不发起任何请求。

        clock.advance(1000.0)  # 远超刚才那个已经退出的 1 秒预算。
        tenants = client.list_collaboration_tenants(token="fake-user-token")

        self.assertEqual([item["tenant_key"] for item in tenants], ["tenant_a"])

    def test_without_entering_round_budget_a_huge_clock_jump_never_raises_it(self) -> None:
        """否定断言：从未调用过 `round_budget()` 的 client（默认关闭状态），即使
        注入的假时钟被推到很晚，也绝不会抛 `round_budget_exceeded`——预算不
        影响任何正常轮（本任务的核心否定断言之一）。故意破坏实现确认变红：
        把 `_PagedClient.__init__` 里 `self._round_deadline` 的默认值从 `None`
        改成非 `None` 会让这条测试失败，证明它确实在测"默认关闭"这件事。"""

        clock = _FakeClock(start=0.0)
        clock.advance(10_000_000.0)  # 远超任何合理预算，模拟"进程已经跑了很久"。
        transport = RecordingTransport(
            [page([{"tenant_key": "tenant_a"}], key="target_tenant_list")]
        )
        client = FeishuDirectoryClient(
            base_url=BASE_URL,
            transport=transport,
            sleep=lambda seconds: None,
            round_deadline_clock=clock,
        )

        tenants = client.list_collaboration_tenants(token="fake-user-token")

        self.assertEqual([item["tenant_key"] for item in tenants], ["tenant_a"])


if __name__ == "__main__":
    unittest.main()
