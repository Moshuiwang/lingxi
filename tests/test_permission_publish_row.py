"""权限聚合与发布行形状的纯逻辑断言（Issue #156 / S-C-01）。

认领断言：`V-权限-02`（发布内容与数据库当前记录逐字段一致——本文件覆盖"内容怎么算出来
的、怎么比对"这半边）、`V-权限-10`（逐字段读回按**字符串值**比对）、`V-权限-11`
（发布字段集不含 ``token_cipher``）。

否定面（合同的"不得 / 不允许"必须有对应否定测试，验证与门禁第八节）：

- 没有受支持职能、没有公司范围时**不产生**任何发布内容；
- fail-closed 的聚合结果**不可能**携带公司或职能（类型层就构造不出来）；
- ``token_cipher`` **不在**发布字段集里，也不可能混进内容快照；
- 读回比对**不按 Python 类型严格相等**：数字被读成字符串时仍然算一致。
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from lingxi.core.permission.publish_row import (
    ALL_COMPANIES_KEY,
    PUBLISHED_FIELD_NAMES,
    REASON_NO_COMPANY_SCOPE,
    REASON_NO_ROLES,
    REASON_NO_SUPPORTED_FUNCTION,
    STATUS_APPROVED,
    PermissionAggregate,
    PublishRow,
    aggregate_permission,
    build_publish_row,
    compare_readback,
    format_updated_at,
    readback_text,
    serialize_permissions,
)

FAKE_GALAXY_USER = "9001"
FAKE_EMAIL = "jiaming.jia@example.invalid"
FAKE_NAME = "化名甲"
DECIDED_AT = datetime(2026, 8, 17, 3, 0, tzinfo=timezone.utc)

# 只登记两个受支持角色：`APP产品运营` / `A海外本地员工营业厅` 刻意不在映射里
# （Issue #17 明确它们不映射），用例据此证明未映射角色不产生职能。
ROLE_MAP = {"A商务": "商务", "A运营（OTT）": "OTT"}

COUNTRY_ROWS = [
    {"country_key": "0", "name": "ALL", "name_cn": "全非", "boss_company_id": "0"},
    {"country_key": "11", "name": "KE", "name_cn": "肯尼亚", "boss_company_id": "1011"},
    {"country_key": "12", "name": "NG", "name_cn": "尼日利亚", "boss_company_id": "1012"},
    # 没有 country_key 的行：只在通配展开时可达（galaxy_scope 的既有语义）。
    {"country_key": "", "name": "ZM", "name_cn": "赞比亚", "boss_company_id": "1013"},
]


def _roles(*names: str) -> list[dict[str, str]]:
    return [
        {"user_id": FAKE_GALAXY_USER, "role_id": str(index), "role_name": name}
        for index, name in enumerate(names, start=1)
    ]


def _countries(*keys: str) -> list[dict[str, str]]:
    return [{"user_id": FAKE_GALAXY_USER, "datacountry_id": key} for key in keys]


def _aggregate(
    *,
    roles: list[dict[str, str]] | None = None,
    countries: list[dict[str, str]] | None = None,
    country_rows: list[dict[str, str]] | None = None,
) -> PermissionAggregate:
    return aggregate_permission(
        galaxy_user_id=FAKE_GALAXY_USER,
        user_role_rows=_roles("A商务") if roles is None else roles,
        datacountry_rows=_countries("11") if countries is None else countries,
        country_rows=COUNTRY_ROWS if country_rows is None else country_rows,
        role_function_map=ROLE_MAP,
    )


class AggregateTest(unittest.TestCase):
    def test_supported_role_and_country_yield_company_and_function(self) -> None:
        aggregate = _aggregate(roles=_roles("A商务", "A运营（OTT）"), countries=_countries("11", "12"))
        self.assertTrue(aggregate.granted)
        # 公司用 boss_company_id（产品负责人 2026-08-05 决策 3），不是 country_key、
        # 也不是展示用的 name_cn。
        self.assertEqual(aggregate.companies, ("1011", "1012"))
        self.assertEqual(aggregate.functions, ("OTT", "商务"))
        self.assertFalse(aggregate.all_companies)

    def test_wildcard_expands_to_every_company_without_the_sentinel_itself(self) -> None:
        aggregate = _aggregate(countries=_countries("0"))
        self.assertTrue(aggregate.all_companies)
        # 哨兵自己（boss_company_id="0"）不作为一个公司出现；没有 country_key 的行照常
        # 参与展开（galaxy_scope 的既有语义，独立复查发现的那一处）。
        self.assertEqual(aggregate.companies, ("1011", "1012", "1013"))

    def test_broken_sentinel_fails_closed_instead_of_granting_everything(self) -> None:
        broken = [dict(row) for row in COUNTRY_ROWS]
        broken[0]["name_cn"] = "全部"  # 哨兵形态损坏
        aggregate = _aggregate(countries=_countries("0"), country_rows=broken)
        self.assertFalse(aggregate.granted)
        self.assertEqual(aggregate.reason, REASON_NO_COMPANY_SCOPE)
        self.assertEqual(aggregate.companies, ())

    def test_no_roles_fails_closed(self) -> None:
        aggregate = _aggregate(roles=[])
        self.assertFalse(aggregate.granted)
        self.assertEqual(aggregate.reason, REASON_NO_ROLES)

    def test_only_unmapped_roles_fail_closed_and_are_counted(self) -> None:
        aggregate = _aggregate(roles=_roles("APP产品运营", "A海外本地员工营业厅"))
        self.assertFalse(aggregate.granted)
        self.assertEqual(aggregate.reason, REASON_NO_SUPPORTED_FUNCTION)
        self.assertEqual(aggregate.functions, ())
        self.assertEqual(aggregate.unmapped_role_count, 2)

    def test_supported_role_ignores_unmapped_siblings(self) -> None:
        aggregate = _aggregate(roles=_roles("A商务", "APP产品运营"))
        self.assertTrue(aggregate.granted)
        self.assertEqual(aggregate.functions, ("商务",))
        self.assertEqual(aggregate.unmapped_role_count, 1)

    def test_no_country_scope_fails_closed(self) -> None:
        aggregate = _aggregate(countries=[])
        self.assertFalse(aggregate.granted)
        self.assertEqual(aggregate.reason, REASON_NO_COMPANY_SCOPE)

    def test_scope_ordering_is_stable_regardless_of_input_order(self) -> None:
        """聚合结果自己的次序合同：公司与职能都排过序，与输入次序无关。

        序列化那一层的 ``sort_keys=True`` 只管 JSON **键**（公司），管不到列表**值**
        （职能）；而 `audit_facts` 与 `PermissionAggregate.companies` 也会被上层直接
        读。因此两条次序都要在这里各自成立，不能靠下游那一次排序兜底。
        """

        forward = _aggregate(roles=_roles("A商务", "A运营（OTT）"), countries=_countries("11", "12"))
        backward = _aggregate(roles=_roles("A运营（OTT）", "A商务"), countries=_countries("12", "11"))
        self.assertEqual(forward.companies, ("1011", "1012"))
        self.assertEqual(backward.companies, ("1011", "1012"))
        self.assertEqual(forward.functions, ("OTT", "商务"))
        self.assertEqual(backward.functions, ("OTT", "商务"))

    def test_unresolved_country_key_narrows_instead_of_denying(self) -> None:
        """陈旧外键只让那一个国家掉出范围，不否决整个用户（方向安全：少给不多给）。"""

        aggregate = _aggregate(countries=_countries("11", "99"))
        self.assertTrue(aggregate.granted)
        self.assertEqual(aggregate.companies, ("1011",))
        self.assertEqual(aggregate.unresolved_country_keys, ("99",))

    def test_country_without_company_id_is_dropped_and_counted(self) -> None:
        rows = [{"country_key": "13", "name": "TZ", "name_cn": "坦桑尼亚", "boss_company_id": ""}]
        aggregate = _aggregate(countries=_countries("13"), country_rows=rows)
        self.assertFalse(aggregate.granted)
        self.assertEqual(aggregate.countries_without_company_id, 1)

    def test_denied_aggregate_cannot_carry_scope(self) -> None:
        with self.assertRaises(ValueError):
            PermissionAggregate(granted=False, reason="x", companies=("1011",))

    def test_granted_aggregate_requires_both_axes(self) -> None:
        with self.assertRaises(ValueError):
            PermissionAggregate(granted=True, reason="granted", companies=("1011",), functions=())

    def test_audit_facts_carry_no_personal_values(self) -> None:
        facts = _aggregate().audit_facts()
        rendered = json.dumps(facts, ensure_ascii=False)
        self.assertNotIn(FAKE_EMAIL, rendered)
        self.assertNotIn(FAKE_NAME, rendered)
        self.assertNotIn(FAKE_GALAXY_USER, rendered)


class SerializationTest(unittest.TestCase):
    """格式依据：2026-08-17 编排者对正式表的全表只读回源核对（26/26 行）。

    形状是 `{公司ID: [职能, …]}`，通配用 `"*"` 键，无版本字段——这是**消费方现行的**
    约定，不是我们自拟的。
    """

    def test_permissions_maps_company_id_to_function_list(self) -> None:
        aggregate = _aggregate(roles=_roles("A运营（OTT）", "A商务"), countries=_countries("12", "11"))
        text = serialize_permissions(aggregate)
        self.assertEqual(text, '{"1011":["OTT","商务"],"1012":["OTT","商务"]}')
        self.assertNotIn("\n", text)
        # 消费方没有版本字段约定：塞一个它不认识的键，最好被忽略，最坏解析失败。
        self.assertNotIn('"v"', text)
        self.assertNotIn("all_companies", text)

    def test_wildcard_scope_writes_a_single_star_key(self) -> None:
        aggregate = _aggregate(countries=_countries("0"))
        self.assertTrue(aggregate.all_companies)
        self.assertEqual(serialize_permissions(aggregate), '{"*":["商务"]}')
        self.assertEqual(ALL_COMPANIES_KEY, "*")

    def test_serialization_is_byte_identical_regardless_of_input_order(self) -> None:
        """恒等序列化：读回按字符串比对的前提。

        两个聚合来自**次序不同**的同一份授权；公司键与职能列表都必须排到同一串字节，
        否则一次没有任何变化的重发会被读回比对判成不一致。
        """

        forward = _aggregate(roles=_roles("A商务", "A运营（OTT）"), countries=_countries("11", "12"))
        reversed_input = _aggregate(
            roles=_roles("A运营（OTT）", "A商务"), countries=_countries("12", "11")
        )
        self.assertEqual(serialize_permissions(forward), serialize_permissions(reversed_input))
        self.assertEqual(serialize_permissions(forward), serialize_permissions(forward))

    def test_company_keys_are_sorted(self) -> None:
        rows = [
            {"country_key": "21", "name": "A", "name_cn": "甲", "boss_company_id": "9"},
            {"country_key": "22", "name": "B", "name_cn": "乙", "boss_company_id": "10"},
        ]
        aggregate = _aggregate(countries=_countries("21", "22"), country_rows=rows)
        # 字符串序（"10" < "9"）——重点是**恒定**，不是数值大小。
        self.assertEqual(serialize_permissions(aggregate), '{"10":["商务"],"9":["商务"]}')

    def test_denied_aggregate_cannot_be_serialized(self) -> None:
        with self.assertRaises(ValueError):
            serialize_permissions(_aggregate(roles=[]))

    def test_updated_at_is_second_precision_utc(self) -> None:
        moment = datetime(2026, 8, 17, 11, 0, 0, 123456, tzinfo=timezone(timedelta(hours=8)))
        self.assertEqual(format_updated_at(moment), "2026-08-17T03:00:00Z")

    def test_naive_time_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            format_updated_at(datetime(2026, 8, 17, 3, 0))


class PublishRowTest(unittest.TestCase):
    def _row(self) -> PublishRow:
        return build_publish_row(
            aggregate=_aggregate(),
            email="  Jiaming.Jia@Example.INVALID ",
            display_name=FAKE_NAME,
            decided_at=DECIDED_AT,
        )

    def test_record_key_and_email_share_the_normalized_value(self) -> None:
        row = self._row()
        self.assertEqual(row.record_key, FAKE_EMAIL)
        self.assertEqual(row.email, FAKE_EMAIL)

    def test_status_matches_the_value_every_existing_row_uses(self) -> None:
        # 既有 26 行全是 approved（2026-08-17 全表回源核对）；这一列的取值域由消费方定义。
        self.assertEqual(STATUS_APPROVED, "approved")
        self.assertEqual(self._row().status, "approved")

    def test_published_fields_never_include_token_cipher(self) -> None:
        """`V-权限-11` 的正面：字段集里没有 ``token_cipher``，更新集因此不会清空它。"""

        self.assertNotIn("token_cipher", PUBLISHED_FIELD_NAMES)
        self.assertNotIn("token_cipher", self._row().fields)
        self.assertEqual(set(self._row().fields), set(PUBLISHED_FIELD_NAMES))

    def test_content_fields_drop_only_the_timestamp(self) -> None:
        row = self._row()
        self.assertEqual(
            set(row.content_fields), set(PUBLISHED_FIELD_NAMES) - {"updated_at"}
        )

    def test_denied_user_cannot_produce_a_row(self) -> None:
        with self.assertRaises(ValueError):
            build_publish_row(
                aggregate=_aggregate(roles=[]),
                email=FAKE_EMAIL,
                display_name=FAKE_NAME,
                decided_at=DECIDED_AT,
            )

    def test_missing_email_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_publish_row(
                aggregate=_aggregate(), email="   ", display_name=FAKE_NAME, decided_at=DECIDED_AT
            )

    def test_newline_in_any_field_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_publish_row(
                aggregate=_aggregate(),
                email=FAKE_EMAIL,
                display_name="化名\n甲",
                decided_at=DECIDED_AT,
            )

    def test_from_fields_round_trips_and_rejects_extra_keys(self) -> None:
        row = self._row()
        self.assertEqual(PublishRow.from_fields(row.fields), row)
        with self.assertRaises(ValueError):
            PublishRow.from_fields({**row.fields, "token_cipher": "secret"})
        with self.assertRaises(ValueError):
            PublishRow.from_fields({name: "x" for name in PUBLISHED_FIELD_NAMES[:-1]})


class ReadbackTest(unittest.TestCase):
    def test_number_readback_compares_by_string_value(self) -> None:
        """G-BIT 移交的实现约束：Number 字段被读取接口字符串化，不得按类型严格相等。"""

        self.assertEqual(readback_text(1011), "1011")
        self.assertEqual(compare_readback({"record_key": "1011"}, {"record_key": 1011}), ())

    def test_missing_and_none_readback_are_reported(self) -> None:
        expected = {"record_key": "a", "email": "b"}
        self.assertEqual(compare_readback(expected, {"record_key": "a"}), ("email",))
        self.assertEqual(compare_readback(expected, {"record_key": "a", "email": None}), ("email",))

    def test_mismatch_lists_field_names_in_registered_order(self) -> None:
        expected = {name: "x" for name in PUBLISHED_FIELD_NAMES}
        actual = {name: "x" for name in PUBLISHED_FIELD_NAMES}
        actual["status"] = "y"
        actual["email"] = "y"
        self.assertEqual(compare_readback(expected, actual), ("email", "status"))

    def test_boolean_never_passes_as_text(self) -> None:
        # 布尔不是文本列的合法回读形态；归空串让比对红出来而不是静默吞掉。
        self.assertEqual(readback_text(True), "")
        self.assertEqual(compare_readback({"status": "active"}, {"status": True}), ("status",))

    def test_identical_row_reads_back_clean(self) -> None:
        row = build_publish_row(
            aggregate=_aggregate(), email=FAKE_EMAIL, display_name=FAKE_NAME, decided_at=DECIDED_AT
        )
        self.assertEqual(compare_readback(row.fields, dict(row.fields)), ())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
