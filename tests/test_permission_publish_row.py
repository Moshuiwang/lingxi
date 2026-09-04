"""权限聚合与发布行形状的纯逻辑断言（Issue #156 / S-C-01）。

认领断言：`V-权限-02`（发布内容与数据库当前记录逐字段一致——本文件覆盖"内容怎么算出来
的、怎么比对"这半边）、`V-权限-10`（逐字段读回按**字符串值**比对）、`V-权限-11`
（**新建集必须含 ``token_cipher``、更新集一律不含**；明文不进任何落库形态）、
`V-权限-06` 的本地判定面（``permissions`` 按**回退制**读，不取并集）。

后两条随 [#156](https://github.com/Moshuiwang/lingxi/issues/156) 的 S-C-02 加入
（产品负责人 2026-08-17 裁定，见 ``core/permission/publish_row.py`` 模块文档）。

否定面（合同的"不得 / 不允许"必须有对应否定测试，验证与门禁第八节）：

- 没有受支持职能、没有公司范围时**不产生**任何发布内容；
- fail-closed 的聚合结果**不可能**携带公司或职能（类型层就构造不出来）；
- ``token_cipher`` **不在更新集**里；缺它时新建集**构造不出来**（不静默退回六字段）；
- 令牌**明文**永远过不了密文形状校验，因此在 ``core``（拿不到主密钥的地方）就被挡住；
- 权限判定**不取并集**：显式公司键存在时不回退通配，空列表不等于缺键；
- 读回比对**不按 Python 类型严格相等**：数字被读成字符串时仍然算一致。
"""

from __future__ import annotations

import base64
import json
import secrets
import unittest
from datetime import UTC, datetime, timedelta, timezone

from lingxi.core.permission.publish_row import (
    ALL_COMPANIES_KEY,
    CREATED_FIELD_NAMES,
    DIGEST_FIELD_NAMES,
    PUBLISHED_FIELD_NAMES,
    REASON_NO_COMPANY_SCOPE,
    REASON_NO_ROLES,
    REASON_NO_SUPPORTED_FUNCTION,
    STATUS_APPROVED,
    PermissionAggregate,
    PublishRow,
    aggregate_permission,
    build_publish_row,
    build_translated_publish_row,
    compare_readback,
    content_digest,
    format_updated_at,
    is_cipher_shaped,
    lookup_metrics,
    parse_permissions,
    permissions_digest,
    readback_text,
    serialize_permissions,
    serialize_translated_permissions,
)

#: biai-agent 加密规格 v1 的**公开测试向量密文**（非生产密钥、非生产令牌）。
#: 这里只需要一份形状合法的密文，不解开它。
TOKEN_CIPHER = "RklYRURJVjEyMzQ1Njc4OX5gpf2vKqJiLgzu2n4kug1V1rz6DDt1OCgAZVpg1pL+"

FAKE_GALAXY_USER = "9001"
FAKE_EMAIL = "jiaming.jia@example.invalid"
FAKE_NAME = "化名甲"
DECIDED_AT = datetime(2026, 8, 17, 3, 0, tzinfo=UTC)

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
        aggregate = _aggregate(
            roles=_roles("A商务", "A运营（OTT）"), countries=_countries("11", "12")
        )
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

        forward = _aggregate(
            roles=_roles("A商务", "A运营（OTT）"), countries=_countries("11", "12")
        )
        backward = _aggregate(
            roles=_roles("A运营（OTT）", "A商务"), countries=_countries("12", "11")
        )
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

    def test_audit_facts_reports_the_actual_company_count_when_not_wildcard(self) -> None:
        aggregate = _aggregate(
            roles=_roles("A商务", "A运营（OTT）"), countries=_countries("11", "12")
        )
        facts = aggregate.audit_facts()
        self.assertEqual(facts["companies"], 2)
        self.assertFalse(facts["all_companies"])

    def test_audit_facts_blanks_the_company_count_under_the_wildcard(self) -> None:
        """审计输出的矛盾修正（Trace #328 opus 审查 P2）：``all_companies=True`` 时
        ``companies`` 具体数量与实际覆盖范围毫无关系（可能来自银河「全非」通配
        展开出的一大串，也可能来自「角色即全公司」特例下只解释出一两个具体公司），
        继续输出会读出一句自相矛盾的话（"companies: 1, all_companies: true"）。
        置空之后不再有这个数字，读者不会被误导成"范围只有这么大"。"""

        aggregate = _aggregate(countries=_countries("0"))  # 银河「全非」通配
        self.assertTrue(aggregate.all_companies)
        self.assertEqual(
            aggregate.companies, ("1011", "1012", "1013"), "companies 字段本身不受影响"
        )

        facts = aggregate.audit_facts()

        self.assertIsNone(facts["companies"], "all_companies=True 时不输出具体数量")
        self.assertTrue(facts["all_companies"])


class SerializationTest(unittest.TestCase):
    """格式依据：2026-08-17 编排者对正式表的全表只读回源核对（26/26 行）。

    形状是 `{公司ID: [职能, …]}`，通配用 `"*"` 键，无版本字段——这是**消费方现行的**
    约定，不是我们自拟的。
    """

    def test_permissions_maps_company_id_to_function_list(self) -> None:
        aggregate = _aggregate(
            roles=_roles("A运营（OTT）", "A商务"), countries=_countries("12", "11")
        )
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

        forward = _aggregate(
            roles=_roles("A商务", "A运营（OTT）"), countries=_countries("11", "12")
        )
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


class MetricNameSensitivityTest(unittest.TestCase):
    """值列表元素**逐字敏感**：零归一守卫（产品负责人 2026-08-17 对 #155 的答复）。

    MCP 按字面匹配指标名，大小写与全半角都算数。这组用例钉住的是「这条链路上没有任何
    顺手归一」——一旦有人在聚合层或序列化层加一次 `strip()` / `casefold()` /
    `unicodedata.normalize()`，它们立刻变红。错的方向是**静默给错范围**而不是报错，
    所以必须由用例守，不能靠代码评审记得。

    注：写侧当前放进值列表的还是职能标签（「公司+职能→指标名」翻译层未实现）；本组
    守的是**字符串透传纪律**，它与列表内容将来换成真正的指标名无关。
    """

    def _serialize(self, *values: str) -> str:
        aggregate = PermissionAggregate(
            granted=True, reason="granted", companies=("1011",), functions=values
        )
        return serialize_permissions(aggregate)

    def test_case_difference_produces_a_different_payload(self) -> None:
        self.assertNotEqual(self._serialize("OTT"), self._serialize("ott"))
        self.assertIn('"OTT"', self._serialize("OTT"))
        self.assertIn('"ott"', self._serialize("ott"))

    def test_full_width_characters_survive_verbatim(self) -> None:
        # 全角 ＯＴＴ 与半角 OTT 在 MCP 眼里是两个指标，不得被任何宽度转换抹平。
        self.assertNotEqual(self._serialize("ＯＴＴ"), self._serialize("OTT"))
        self.assertIn('"ＯＴＴ"', self._serialize("ＯＴＴ"))

    def test_surrounding_and_inner_whitespace_is_not_trimmed(self) -> None:
        # 连 strip 都不做：带空白的指标名是数据问题，不该由发布层悄悄"修好"。
        self.assertIn('" 日活 "', self._serialize(" 日活 "))
        self.assertIn('"日 活"', self._serialize("日 活"))

    def test_chinese_metric_names_are_not_escaped(self) -> None:
        # ensure_ascii=False 是「全半角敏感」的前提：转义会把原字符藏进 \uXXXX。
        self.assertIn('"日活"', self._serialize("日活"))
        self.assertNotIn("\\u", self._serialize("日活"))

    def test_none_and_non_string_entries_fail_loudly(self) -> None:
        for bad in (None, 123, ("日活",), ""):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self._serialize(bad)  # type: ignore[arg-type]

    def test_a_star_inside_the_value_list_is_passed_through_unchanged(self) -> None:
        """``"*"`` 在**值列表内**表示「该公司下所有指标」。

        我方权限模型**不产出**这个形状（值来自角色名映射配置，配置里没有 `*`），
        但读侧可能遇到它，而序列化层不得对它做任何特殊处理——特判会让「透传」这条
        纪律出现一个例外，例外早晚会长出第二个。
        """

        self.assertEqual(self._serialize("*"), '{"1011":["*"]}')

    def test_wildcard_key_and_wildcard_value_are_different_positions(self) -> None:
        key_wildcard = serialize_permissions(
            PermissionAggregate(
                granted=True,
                reason="granted",
                companies=("1011",),
                functions=("日活",),
                all_companies=True,
            )
        )
        self.assertEqual(key_wildcard, '{"*":["日活"]}')
        self.assertEqual(self._serialize("*"), '{"1011":["*"]}')
        self.assertNotEqual(key_wildcard, self._serialize("*"))

    def test_writer_side_never_produces_an_empty_value_list(self) -> None:
        """空列表=「该公司下无任何指标」，是读侧的合法形状；写侧走 fail-closed，不产出它。"""

        with self.assertRaises(ValueError):
            PermissionAggregate(granted=True, reason="granted", companies=("1011",), functions=())

    def test_updated_at_is_second_precision_utc(self) -> None:
        moment = datetime(2026, 8, 17, 11, 0, 0, 123456, tzinfo=timezone(timedelta(hours=8)))
        self.assertEqual(format_updated_at(moment), "2026-08-17T03:00:00Z")

    def test_naive_time_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            format_updated_at(datetime(2026, 8, 17, 3, 0))


class TranslatedSerializationTest(unittest.TestCase):
    """:func:`serialize_translated_permissions`（Issue #227 翻译层的姊妹序列化函数）。

    与 ``SerializationTest``/``MetricNameSensitivityTest`` 对 ``serialize_permissions``
    的覆盖对称：证明「值列表逐字符原样透传」与「恒等序列化」这两条纪律在翻译层之后
    依然成立——只是输入形状从「一份职能列表套所有公司」换成了「每个公司一份可能
    不同的指标名列表」。
    """

    def test_each_company_can_carry_a_different_metric_list(self) -> None:
        text = serialize_translated_permissions({"1011": ["日活", "收入"], "1012": ["日活"]})

        self.assertEqual(text, '{"1011":["日活","收入"],"1012":["日活"]}')

    def test_company_keys_are_sorted_same_as_the_untranslated_path(self) -> None:
        text = serialize_translated_permissions({"9": ["日活"], "10": ["日活"]})

        self.assertEqual(text, '{"10":["日活"],"9":["日活"]}')

    def test_a_wildcard_company_key_passes_through_like_any_other_key(self) -> None:
        text = serialize_translated_permissions({ALL_COMPANIES_KEY: ["日活"]})

        self.assertEqual(text, '{"*":["日活"]}')

    def test_case_difference_produces_a_different_payload(self) -> None:
        upper = serialize_translated_permissions({"1011": ["OTT"]})
        lower = serialize_translated_permissions({"1011": ["ott"]})

        self.assertNotEqual(upper, lower)
        self.assertIn('"OTT"', upper)
        self.assertIn('"ott"', lower)

    def test_full_width_characters_survive_verbatim(self) -> None:
        text = serialize_translated_permissions({"1011": ["ＯＴＴ"]})

        self.assertIn('"ＯＴＴ"', text)
        self.assertNotEqual(text, serialize_translated_permissions({"1011": ["OTT"]}))

    def test_surrounding_whitespace_is_not_trimmed(self) -> None:
        text = serialize_translated_permissions({"1011": [" 日活 "]})

        self.assertIn('" 日活 "', text)

    def test_chinese_metric_names_are_not_escaped(self) -> None:
        text = serialize_translated_permissions({"1011": ["日活"]})

        self.assertIn('"日活"', text)
        self.assertNotIn("\\u", text)

    def test_none_and_non_string_entries_fail_loudly(self) -> None:
        for bad in (None, 123, ("日活",), ""):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    serialize_translated_permissions({"1011": [bad]})  # type: ignore[list-item]

    def test_an_empty_metric_list_for_any_company_is_rejected(self) -> None:
        """写侧不产出空列表——与 ``serialize_permissions`` 的
        ``test_writer_side_never_produces_an_empty_value_list`` 同一条纪律。"""

        with self.assertRaises(ValueError):
            serialize_translated_permissions({"1011": []})

    def test_completely_empty_input_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            serialize_translated_permissions({})

    def test_a_blank_company_key_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            serialize_translated_permissions({"": ["日活"]})

    def test_serialization_is_byte_identical_regardless_of_dict_insertion_order(self) -> None:
        """恒等序列化：字典插入次序不影响输出字节（读回按字符串比对的前提）。"""

        forward = serialize_translated_permissions({"1011": ["日活", "收入"], "1012": ["日活"]})
        reversed_input = serialize_translated_permissions(
            {"1012": ["日活"], "1011": ["日活", "收入"]}
        )

        self.assertEqual(forward, reversed_input)

    def test_output_matches_the_untranslated_path_when_every_company_shares_one_list(self) -> None:
        """当每个公司恰好拿到同一份值列表时（翻译层的一种特殊情形），产出的字节形态
        与既有的 ``serialize_permissions`` 完全一致——两个函数出自同一套序列化纪律，
        不是两套互相独立的格式。"""

        aggregate = PermissionAggregate(
            granted=True, reason="granted", companies=("1011", "1012"), functions=("OTT", "商务")
        )

        self.assertEqual(
            serialize_permissions(aggregate),
            serialize_translated_permissions({"1011": ["OTT", "商务"], "1012": ["OTT", "商务"]}),
        )


class TranslatedPublishRowTest(unittest.TestCase):
    """:func:`build_translated_publish_row`：翻译产物结算成发布行。"""

    def _row(self, *, token_cipher: str | None = None) -> PublishRow:
        return build_translated_publish_row(
            company_metrics={"1011": ["日活", "收入"]},
            email=" Jia.Ming@Example.INVALID ",
            display_name="化名甲",
            decided_at=datetime(2026, 8, 17, 3, 0, tzinfo=UTC),
            token_cipher=token_cipher,
        )

    def test_permissions_carries_the_translated_metrics_not_the_function_label(self) -> None:
        row = self._row()

        self.assertEqual(row.permissions, '{"1011":["日活","收入"]}')

    def test_record_key_and_email_share_the_normalized_value(self) -> None:
        row = self._row()

        self.assertEqual(row.record_key, "jia.ming@example.invalid")
        self.assertEqual(row.email, "jia.ming@example.invalid")

    def test_missing_email_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_translated_publish_row(
                company_metrics={"1011": ["日活"]},
                email="   ",
                display_name="化名甲",
                decided_at=datetime(2026, 8, 17, 3, 0, tzinfo=UTC),
            )

    def test_missing_display_name_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_translated_publish_row(
                company_metrics={"1011": ["日活"]},
                email="jia.ming@example.invalid",
                display_name="  ",
                decided_at=datetime(2026, 8, 17, 3, 0, tzinfo=UTC),
            )

    def test_uncovered_translation_input_cannot_produce_a_row(self) -> None:
        """空的翻译产物构造不出发布行——调用方必须先经过翻译层的 fail-closed 出口，
        不能把"翻译失败"悄悄伪装成"翻译出一个空权限"。"""

        with self.assertRaises(ValueError):
            build_translated_publish_row(
                company_metrics={},
                email="jia.ming@example.invalid",
                display_name="化名甲",
                decided_at=datetime(2026, 8, 17, 3, 0, tzinfo=UTC),
            )

    def test_token_cipher_is_optional_like_the_untranslated_path(self) -> None:
        row = self._row(token_cipher=TOKEN_CIPHER)

        self.assertEqual(row.token_cipher, TOKEN_CIPHER)
        self.assertIsNone(self._row().token_cipher)


class PublishRowTest(unittest.TestCase):
    def _row(self, *, token_cipher: str | None = None) -> PublishRow:
        return build_publish_row(
            aggregate=_aggregate(),
            email="  Jiaming.Jia@Example.INVALID ",
            display_name=FAKE_NAME,
            decided_at=DECIDED_AT,
            token_cipher=token_cipher,
        )

    def test_record_key_and_email_share_the_normalized_value(self) -> None:
        row = self._row()
        self.assertEqual(row.record_key, FAKE_EMAIL)
        self.assertEqual(row.email, FAKE_EMAIL)

    def test_status_matches_the_value_every_existing_row_uses(self) -> None:
        # 既有 26 行全是 approved（2026-08-17 全表回源核对）；这一列的取值域由消费方定义。
        self.assertEqual(STATUS_APPROVED, "approved")
        self.assertEqual(self._row().status, "approved")

    def test_update_field_set_never_includes_token_cipher(self) -> None:
        """`V-权限-11` 后半：更新集里没有 ``token_cipher``，既有值因此不会被清空。"""

        self.assertNotIn("token_cipher", PUBLISHED_FIELD_NAMES)
        row = self._row(token_cipher=TOKEN_CIPHER)
        self.assertNotIn("token_cipher", row.fields)
        self.assertEqual(set(row.fields), set(PUBLISHED_FIELD_NAMES))

    def test_create_field_set_requires_token_cipher(self) -> None:
        """`V-权限-11` 前半：新建集 = 更新集 + ``token_cipher``，缺它就构造不出来。"""

        self.assertEqual(set(CREATED_FIELD_NAMES), set(PUBLISHED_FIELD_NAMES) | {"token_cipher"})
        row = self._row(token_cipher=TOKEN_CIPHER)
        self.assertEqual(set(row.create_fields), set(CREATED_FIELD_NAMES))
        self.assertEqual(row.create_fields["token_cipher"], TOKEN_CIPHER)
        with self.assertRaises(ValueError):
            self._row(token_cipher=None).create_fields

    def test_snapshot_fields_follow_whether_a_token_exists(self) -> None:
        self.assertEqual(
            set(self._row(token_cipher=TOKEN_CIPHER).snapshot_fields), set(CREATED_FIELD_NAMES)
        )
        self.assertEqual(
            set(self._row(token_cipher=None).snapshot_fields), set(PUBLISHED_FIELD_NAMES)
        )

    def test_plaintext_token_can_never_be_stored_as_a_cipher(self) -> None:
        """明文当密文传进来必须在 ``core`` 就被拦住（拿不到主密钥的地方也要拦得住）。

        ``secrets.token_urlsafe(32)`` 的形状（URL 安全字母表、长度不是 4 的倍数）过不了
        标准 base64 校验，因此 ``is_cipher_shaped`` 判否，构造直接失败。
        """

        for plaintext in (secrets.token_urlsafe(32) for _ in range(32)):
            with self.subTest(plaintext_length=len(plaintext)):
                self.assertFalse(is_cipher_shaped(plaintext))
                with self.assertRaises(ValueError) as caught:
                    self._row(token_cipher=plaintext)
                # 不回显收到的值：它是凭据材料。
                self.assertNotIn(plaintext, str(caught.exception))

    def test_malformed_cipher_is_rejected(self) -> None:
        for value in ("", "   ", "not base64!", base64.b64encode(b"x" * 16).decode(), 42):
            with self.subTest(value=repr(value)):
                self.assertFalse(is_cipher_shaped(value))
        with self.assertRaises(ValueError):
            self._row(token_cipher="not base64!")

    def test_content_fields_drop_only_the_timestamp(self) -> None:
        row = self._row()
        self.assertEqual(set(row.content_fields), set(PUBLISHED_FIELD_NAMES) - {"updated_at"})

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
        with_token = self._row(token_cipher=TOKEN_CIPHER)
        self.assertEqual(PublishRow.from_fields(with_token.create_fields), with_token)
        with self.assertRaises(ValueError):
            PublishRow.from_fields({**row.fields, "刻意夹带": "x"})
        with self.assertRaises(ValueError):
            PublishRow.from_fields({name: "x" for name in PUBLISHED_FIELD_NAMES[:-1]})

    def test_from_fields_never_invents_a_token(self) -> None:
        """一份没有令牌的快照还原出来仍然没有令牌：**不补齐**。

        补齐等于把"只更新既有行"这一版决定，悄悄变成"可以新建"。
        """

        restored = PublishRow.from_fields(self._row().fields)
        self.assertIsNone(restored.token_cipher)
        with self.assertRaises(ValueError):
            restored.create_fields


class PermissionFallbackTest(unittest.TestCase):
    """``permissions`` 的**读侧**判定：回退制，**不取并集**（`V-权限-06` 的本地判定面）。

    产品负责人 2026-08-17 的权威设计（留痕见 #155）：先看 ``permissions[company_id]``，
    **该键存在就到此为止**（哪怕是空列表）；不存在才回退 ``permissions["*"]``。
    旧系统 biai-agent 的并集归一是**错的**，方向是多给权限，不得沿用。
    """

    def test_explicit_company_key_wins_and_is_not_unioned(self) -> None:
        document = parse_permissions('{"1011":["日活"],"*":["收入"]}')
        # 并集实现会给出 ("日活","收入")——这一条正是挡它的锚点。
        self.assertEqual(lookup_metrics(document, "1011"), ("日活",))
        self.assertNotIn("收入", lookup_metrics(document, "1011"))

    def test_missing_company_key_falls_back_to_wildcard(self) -> None:
        document = parse_permissions('{"1011":["日活"],"*":["收入"]}')
        self.assertEqual(lookup_metrics(document, "1012"), ("收入",))

    def test_present_but_empty_list_is_not_a_miss(self) -> None:
        """``{"1011":[]}`` 表示"该公司下无任何指标"，与缺键不同：**不回退通配**。"""

        document = parse_permissions('{"1011":[],"*":["收入"]}')
        self.assertEqual(lookup_metrics(document, "1011"), ())
        self.assertEqual(lookup_metrics(document, "1012"), ("收入",))

    def test_no_company_and_no_wildcard_is_empty(self) -> None:
        self.assertEqual(lookup_metrics(parse_permissions('{"1011":["日活"]}'), "1012"), ())

    def test_wildcard_inside_values_is_a_metric_wildcard_not_a_company_key(self) -> None:
        # ``"*"`` 看位置：键上=所有公司，值列表内=该公司下所有指标（读侧形状）。
        self.assertEqual(lookup_metrics(parse_permissions('{"1011":["*"]}'), "1011"), ("*",))

    def test_existence_question_takes_the_union_on_purpose(self) -> None:
        """``company_id=None`` 问的是"有没有任何指标"，那是存在性判定，不是范围判定。"""

        document = parse_permissions('{"1011":["日活"],"1012":["收入"]}')
        self.assertEqual(lookup_metrics(document), ("收入", "日活"))
        self.assertEqual(lookup_metrics(parse_permissions('{"1011":[]}')), ())

    def test_parse_rejects_shapes_it_cannot_read(self) -> None:
        for text in ("", "   ", "不是 JSON", "[]", '{"1011":"日活"}', '{"1011":[1]}', '{"":["x"]}'):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_permissions(text)

    def test_round_trip_with_the_writer(self) -> None:
        aggregate = _aggregate(
            roles=_roles("A商务", "A运营（OTT）"), countries=_countries("11", "12")
        )
        document = parse_permissions(serialize_permissions(aggregate))
        self.assertEqual(lookup_metrics(document, "1011"), ("OTT", "商务"))
        self.assertEqual(lookup_metrics(document, "9999"), ())


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


class ContentDigestTests(unittest.TestCase):
    """P-3（Trace #544，对抗审查面 3）：内容摘要必须**独立于九十天擦除**。

    ``publish_outbox.payload`` 里有邮箱与姓名，到期必须被擦成空对象；可是"这一版权限
    和上一版一样吗"此前只能读那份 payload，擦过之后读到空对象，一份内容完全没变的权限
    被判成"变了"——重排一条发布意图，并按「权限变化感知即清」把该用户的 ``user_memory``
    与全部会话已送达正文清空。用户侧表现为"什么都没做，记忆和历史答案却没了"。

    摘要是单向的：它说不出邮箱、姓名或权限内容，只能回答"和另一份一不一样"，因此可以
    留在被擦除之后（迁移 ``0085``）。
    """

    def _fields(self, **overrides):
        fields = {
            "record_key": "rec_1",
            "email": "a@b.com",
            "name": "张三",
            "permissions": '{"1011": ["daily_active"]}',
            "status": "启用",
            "updated_at": "2026-09-02 10:00",
        }
        fields.update(overrides)
        return fields

    def test_same_content_same_digest(self) -> None:
        self.assertEqual(content_digest(self._fields()), content_digest(self._fields()))

    def test_updated_at_does_not_participate(self) -> None:
        """时间戳每轮都不同：算进去会让每日刷新天天判成"变了"。"""

        self.assertEqual(
            content_digest(self._fields()),
            content_digest(self._fields(updated_at="2099-01-01 00:00")),
        )

    def test_token_cipher_does_not_participate(self) -> None:
        """七字段新建快照与六字段更新快照的内容摘要必须一致——令牌不是"内容"。"""

        with_cipher = self._fields()
        with_cipher["token_cipher"] = "v1:xxxx"
        self.assertEqual(content_digest(self._fields()), content_digest(with_cipher))

    def test_每个内容字段变化都会改变摘要(self) -> None:
        base = content_digest(self._fields())
        for field, value in (
            ("record_key", "rec_2"),
            ("email", "c@d.com"),
            ("name", "李四"),
            ("permissions", '{"1011": ["x"]}'),
            ("status", "已停用"),
        ):
            with self.subTest(field=field):
                self.assertNotEqual(base, content_digest(self._fields(**{field: value})))

    def test_permissions_digest_only_tracks_permissions(self) -> None:
        """改名不该清记忆：``permissions_digest`` 对 email/name 变化恒定。"""

        base = permissions_digest(self._fields())
        self.assertEqual(base, permissions_digest(self._fields(email="c@d.com", name="李四")))
        self.assertNotEqual(base, permissions_digest(self._fields(permissions="{}")))

    def test_erased_payload_digests_differ_from_real_content(self) -> None:
        """被擦成空对象的 payload 算不出与真实内容相同的摘要——这正是"擦除之后判据
        还成立"要靠**存下来的**摘要而不是现算的原因。"""

        self.assertNotEqual(content_digest({}), content_digest(self._fields()))

    def test_digest_is_stable_across_jsonb_roundtrip_types(self) -> None:
        """payload 从 JSONB 回来时数字会变成 Python 数字，而发布行永远是文本；
        摘要归一必须让两者算出同一个值。"""

        numeric = self._fields(record_key=1011)
        textual = self._fields(record_key="1011")
        self.assertEqual(content_digest(numeric), content_digest(textual))

    def test_digest_field_order_is_pinned(self) -> None:
        """顺序是算法的一部分（迁移 0085 的 SQL 回填按同一顺序拼串）。"""

        self.assertEqual(
            DIGEST_FIELD_NAMES, ("record_key", "email", "name", "permissions", "status")
        )
