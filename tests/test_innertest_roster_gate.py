"""内测名单闸的纯判定层用例（Issue #302 S-N-01）。

覆盖 :mod:`lingxi.core.identity.innertest_roster_gate` 的两个公开函数：
``parse_innertest_roster``（配置解析，fail-closed 对象是"整份配置"）与
``is_open_id_innertest_allowed``（判定，纯集合成员测试）。不连数据库、不发请求。

**默认拒绝的否定断言**（验证与门禁 §八第 4 条）：用一个从未出现在任何名单中的
`UNKNOWN_OPEN_ID` 证明默认拒绝，而不是只证明某个"已知在名单里"的对象被放行——
`DefaultDenyTests` 专门覆盖这一条。
"""

from __future__ import annotations

import unittest

from lingxi.core.identity.innertest_roster_gate import (
    InnerTestRosterConfigError,
    is_open_id_innertest_allowed,
    parse_innertest_roster,
)

#: 名单里两名"存量用户"的化名 open_id（不是真实值，仅用于测试）。形状必须真的满足
#: `_looks_like_open_id` 的正则（`ou_` 后接 20~64 位英文字母或数字，不含下划线这类
#: 分隔符）——否则 `ParseValidConfigTests` 会在 `parse_innertest_roster` 这一步就
#: 被当成格式非法，而不是走到真正要测的"合法配置正确解析"这条路径。
ROSTERED_A = "ou_rostermembera00000000000"
ROSTERED_B = "ou_rostermemberb00000000000"
ROSTER = frozenset({ROSTERED_A, ROSTERED_B})

#: 从未出现在任何名单、任何测试夹具中的未知对象——用于默认拒绝的否定断言。
UNKNOWN_OPEN_ID = "ou_never_listed_anywhere_0000"


class ParseAbsentConfigTests(unittest.TestCase):
    """未配置（``None``/空白）解析成空集合，不是错误。"""

    def test_none_parses_to_empty_set(self) -> None:
        self.assertEqual(parse_innertest_roster(None), frozenset())

    def test_empty_string_parses_to_empty_set(self) -> None:
        self.assertEqual(parse_innertest_roster(""), frozenset())

    def test_whitespace_only_parses_to_empty_set(self) -> None:
        self.assertEqual(parse_innertest_roster("   \n\t  "), frozenset())

    def test_only_separators_parses_to_empty_set(self) -> None:
        """全是逗号/换行、没有任何非空条目：仍是"未配置"，不是格式错误。"""

        self.assertEqual(parse_innertest_roster(",,,\n,\n"), frozenset())


class ParseValidConfigTests(unittest.TestCase):
    """合法配置解析成去重后的 open_id 集合。"""

    def test_comma_separated(self) -> None:
        self.assertEqual(
            parse_innertest_roster(f"{ROSTERED_A},{ROSTERED_B}"),
            frozenset({ROSTERED_A, ROSTERED_B}),
        )

    def test_newline_separated(self) -> None:
        self.assertEqual(
            parse_innertest_roster(f"{ROSTERED_A}\n{ROSTERED_B}"),
            frozenset({ROSTERED_A, ROSTERED_B}),
        )

    def test_mixed_separators_with_blank_entries_and_surrounding_whitespace(self) -> None:
        raw = f"  {ROSTERED_A} ,\n\n{ROSTERED_B},,\n "
        self.assertEqual(parse_innertest_roster(raw), frozenset({ROSTERED_A, ROSTERED_B}))

    def test_duplicate_entries_deduplicate(self) -> None:
        self.assertEqual(
            parse_innertest_roster(f"{ROSTERED_A},{ROSTERED_A},{ROSTERED_A}"),
            frozenset({ROSTERED_A}),
        )

    def test_single_entry(self) -> None:
        self.assertEqual(parse_innertest_roster(ROSTERED_A), frozenset({ROSTERED_A}))


class ParseInvalidConfigTests(unittest.TestCase):
    """格式非法：整份配置作废，不做部分采纳（fail-closed 的对象是"这份配置"）。"""

    def test_missing_prefix_is_rejected(self) -> None:
        with self.assertRaises(InnerTestRosterConfigError):
            parse_innertest_roster("not_a_feishu_open_id")

    def test_bare_prefix_with_nothing_after_it_is_rejected(self) -> None:
        with self.assertRaises(InnerTestRosterConfigError):
            parse_innertest_roster("ou_")

    def test_entry_containing_internal_whitespace_is_rejected(self) -> None:
        with self.assertRaises(InnerTestRosterConfigError):
            parse_innertest_roster("ou_has internal space")

    def test_one_bad_entry_invalidates_the_whole_list_not_just_itself(self) -> None:
        """否定断言：不静默丢弃单条坏值，也不部分放行——一条不合法就整份拒绝。

        这里混入一条合法条目（``ROSTERED_A``），证明"部分正确"不能让配置通过。
        """

        with self.assertRaises(InnerTestRosterConfigError) as ctx:
            parse_innertest_roster(f"{ROSTERED_A},not_valid,{ROSTERED_B}")
        self.assertEqual(ctx.exception.invalid_count, 1)

    def test_all_entries_bad_reports_full_count(self) -> None:
        with self.assertRaises(InnerTestRosterConfigError) as ctx:
            parse_innertest_roster("bad_one,bad_two,bad_three")
        self.assertEqual(ctx.exception.invalid_count, 3)

    def test_error_message_does_not_echo_the_raw_invalid_entry(self) -> None:
        """不回显取到的原始条目——同本仓库其余标识类环境变量的错误提示纪律。"""

        secret_looking_garbage = "not_an_open_id_but_looks_sensitive_12345"
        with self.assertRaises(InnerTestRosterConfigError) as ctx:
            parse_innertest_roster(secret_looking_garbage)
        self.assertNotIn(secret_looking_garbage, str(ctx.exception))


class TypoSeparatorRegressionTests(unittest.TestCase):
    """opus 批量审查 P2 修复：`_looks_like_open_id` 此前只要求"以 ou_ 开头、不含
    空白字符"——分号、全角逗号、顿号、句点这类粘连手误全部"看起来像"一个合法条目，
    不会触发整份拒绝，而是被当成一个真实的、只是永远匹配不到任何人的 open_id
    悄悄收进名单：名单静默变小，对应的人静默被全拒，部署日志里看不到任何异常
    （opus 实测四种手误全部复现"静默全拒"）。收紧成 `ou_[0-9A-Za-z]{20,64}` 之后，
    这四类手误都必须在**启动期**让整份配置响亮报错，而不是被悄悄接纳。
    """

    def test_semicolon_typo_used_as_a_separator_is_rejected(self) -> None:
        with self.assertRaises(InnerTestRosterConfigError):
            parse_innertest_roster(f"{ROSTERED_A};{ROSTERED_B}")

    def test_fullwidth_comma_typo_used_as_a_separator_is_rejected(self) -> None:
        with self.assertRaises(InnerTestRosterConfigError):
            parse_innertest_roster(f"{ROSTERED_A}，{ROSTERED_B}")

    def test_ideographic_comma_typo_used_as_a_separator_is_rejected(self) -> None:
        with self.assertRaises(InnerTestRosterConfigError):
            parse_innertest_roster(f"{ROSTERED_A}、{ROSTERED_B}")

    def test_full_stop_typo_used_as_a_separator_is_rejected(self) -> None:
        with self.assertRaises(InnerTestRosterConfigError):
            parse_innertest_roster(f"{ROSTERED_A}。{ROSTERED_B}")


class DefaultDenyTests(unittest.TestCase):
    """默认关闭＝全拒：空集合对任何输入都返回 ``False``。

    **否定断言用未知对象证明**（验证与门禁 §八第 4 条）：``UNKNOWN_OPEN_ID`` 从未
    出现在任何测试夹具的名单里，不是"已知在名单里又被移出"的对象——这条断言证明
    的是结构性默认拒绝，不是"这一个特定值恰好不在名单"。
    """

    def test_empty_roster_rejects_unknown_open_id(self) -> None:
        self.assertFalse(is_open_id_innertest_allowed(UNKNOWN_OPEN_ID, frozenset()))

    def test_empty_roster_rejects_even_a_well_formed_looking_open_id(self) -> None:
        """空名单不是"格式校验通过就放行"——任何人都拒绝，包括看起来完全合法的 open_id。"""

        self.assertFalse(is_open_id_innertest_allowed("ou_looks_totally_legitimate", frozenset()))

    def test_non_empty_roster_still_rejects_unknown_open_id(self) -> None:
        """名单非空时，不在名单里的未知对象依然被拒绝（不是"非空即放行"）。"""

        self.assertFalse(is_open_id_innertest_allowed(UNKNOWN_OPEN_ID, ROSTER))


class MembershipMatchTests(unittest.TestCase):
    """比对语义：首尾裁剪、精确相等，不做大小写归一化、不做前缀/模糊匹配。"""

    def test_exact_member_is_allowed(self) -> None:
        self.assertTrue(is_open_id_innertest_allowed(ROSTERED_A, ROSTER))
        self.assertTrue(is_open_id_innertest_allowed(ROSTERED_B, ROSTER))

    def test_surrounding_whitespace_is_trimmed(self) -> None:
        self.assertTrue(is_open_id_innertest_allowed(f"  {ROSTERED_A}\n", ROSTER))

    def test_case_difference_is_not_normalized(self) -> None:
        different_case = ROSTERED_A.upper()
        self.assertNotEqual(different_case, ROSTERED_A)
        self.assertFalse(is_open_id_innertest_allowed(different_case, ROSTER))

    def test_prefix_of_a_member_is_not_a_match(self) -> None:
        """否定断言：前缀匹配会把 ``ROSTERED_A`` 的一个真前缀误判为命中。"""

        self.assertFalse(is_open_id_innertest_allowed(ROSTERED_A[:-1], ROSTER))

    def test_member_with_extra_suffix_is_not_a_match(self) -> None:
        """否定断言：模糊/前缀匹配会把 `ROSTERED_A` 加了后缀的字符串误判为命中。"""

        self.assertFalse(is_open_id_innertest_allowed(ROSTERED_A + "_extra", ROSTER))

    def test_empty_string_is_rejected(self) -> None:
        self.assertFalse(is_open_id_innertest_allowed("", ROSTER))

    def test_non_string_input_is_rejected(self) -> None:
        self.assertFalse(is_open_id_innertest_allowed(None, ROSTER))  # type: ignore[arg-type]
        self.assertFalse(is_open_id_innertest_allowed(12345, ROSTER))  # type: ignore[arg-type]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
