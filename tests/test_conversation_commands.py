"""``core/conversation/commands.py`` 的 ``/memory`` 解析器断言（Issue #357 S-H3-3）。

``/new``/``/stop``（``parse_command``）此前零覆盖是既有事实，不在本卡范围内补；
只新增 ``parse_memory_command``/``is_memory_command_message`` 与它们对
``is_unrecognized_slash_message`` 判定的影响——后者是安全相关分流（Trace #304
批次 5 直修：以 / 开头但不被认识的文本必须被拦下，不能被执行层当系统命令解析），
新增豁免必须证明"精确豁免 /memory，不放宽到其它任意 / 开头文本"。
"""

from __future__ import annotations

import unittest

from lingxi.core.conversation.commands import (
    Command,
    MemoryCommand,
    MemoryCommandKind,
    is_memory_command_message,
    is_unrecognized_slash_message,
    parse_command,
    parse_memory_command,
)


class ListClearParsingTests(unittest.TestCase):
    def test_list_recognized(self) -> None:
        self.assertEqual(
            parse_memory_command("/memory list"), MemoryCommand(kind=MemoryCommandKind.LIST)
        )

    def test_clear_recognized(self) -> None:
        self.assertEqual(
            parse_memory_command("/memory clear"), MemoryCommand(kind=MemoryCommandKind.CLEAR)
        )

    def test_case_insensitive_and_surrounding_whitespace_tolerant(self) -> None:
        self.assertEqual(
            parse_memory_command("  /MEMORY   List  "), MemoryCommand(kind=MemoryCommandKind.LIST)
        )

    def test_list_with_extra_argument_is_none(self) -> None:
        self.assertEqual(
            parse_memory_command("/memory list extra").kind, MemoryCommandKind.NONE
        )

    def test_clear_with_extra_argument_is_none(self) -> None:
        self.assertEqual(
            parse_memory_command("/memory clear now").kind, MemoryCommandKind.NONE
        )

    def test_bare_memory_with_no_subcommand_is_none(self) -> None:
        self.assertEqual(parse_memory_command("/memory").kind, MemoryCommandKind.NONE)
        self.assertEqual(parse_memory_command("/memory   ").kind, MemoryCommandKind.NONE)


class ForgetParsingTests(unittest.TestCase):
    VALID_ID = "mem_01ARZ3NDEKTSV4RRFFQ69G5FAV"

    def test_valid_memory_id_recognized(self) -> None:
        result = parse_memory_command(f"/memory forget {self.VALID_ID}")
        self.assertEqual(
            result, MemoryCommand(kind=MemoryCommandKind.FORGET, memory_id=self.VALID_ID)
        )

    def test_missing_memory_id_is_none(self) -> None:
        self.assertEqual(parse_memory_command("/memory forget").kind, MemoryCommandKind.NONE)

    def test_wrong_prefix_is_none(self) -> None:
        self.assertEqual(
            parse_memory_command("/memory forget lpo_01ARZ3NDEKTSV4RRFFQ69G5FAV").kind,
            MemoryCommandKind.NONE,
        )

    def test_malformed_ulid_suffix_is_none(self) -> None:
        self.assertEqual(
            parse_memory_command("/memory forget mem_not-a-ulid").kind, MemoryCommandKind.NONE
        )

    def test_trailing_extra_token_is_none(self) -> None:
        self.assertEqual(
            parse_memory_command(f"/memory forget {self.VALID_ID} extra").kind,
            MemoryCommandKind.NONE,
        )

    def test_sql_injection_shaped_id_rejected(self) -> None:
        self.assertEqual(
            parse_memory_command("/memory forget mem_x; DROP TABLE user_memory;--").kind,
            MemoryCommandKind.NONE,
        )


class RememberParsingTests(unittest.TestCase):
    def test_term_mapping_recognized(self) -> None:
        result = parse_memory_command("/memory remember term_mapping 大尼日 => 尼日利亚")
        self.assertEqual(
            result,
            MemoryCommand(
                kind=MemoryCommandKind.REMEMBER,
                memory_type="term_mapping",
                memory_key="大尼日",
                memory_value="尼日利亚",
            ),
        )

    def test_all_three_types_recognized(self) -> None:
        for memory_type in ("term_mapping", "calibration_preference", "convention_template"):
            with self.subTest(memory_type=memory_type):
                result = parse_memory_command(f"/memory remember {memory_type} k => v")
                self.assertEqual(result.kind, MemoryCommandKind.REMEMBER)
                self.assertEqual(result.memory_type, memory_type)

    def test_type_token_case_insensitive(self) -> None:
        result = parse_memory_command("/memory remember TERM_MAPPING k => v")
        self.assertEqual(result.memory_type, "term_mapping")

    def test_unknown_memory_type_is_none(self) -> None:
        self.assertEqual(
            parse_memory_command("/memory remember not_a_type k => v").kind,
            MemoryCommandKind.NONE,
        )

    def test_missing_separator_is_none(self) -> None:
        """否定断言（核心红线）：不接受任意自由文本，只接受 ``key => value`` 形状——
        一条没有 ``=>`` 的登记，即便看起来像"用户想存点什么"，也必须被拒绝。"""

        self.assertEqual(
            parse_memory_command("/memory remember term_mapping 这是一段没有分隔符的自由文本").kind,
            MemoryCommandKind.NONE,
        )

    def test_key_only_no_value_is_none(self) -> None:
        self.assertEqual(
            parse_memory_command("/memory remember term_mapping key =>").kind,
            MemoryCommandKind.NONE,
        )

    def test_value_only_no_key_is_none(self) -> None:
        self.assertEqual(
            parse_memory_command("/memory remember term_mapping => value").kind,
            MemoryCommandKind.NONE,
        )

    def test_blank_key_after_strip_is_none(self) -> None:
        self.assertEqual(
            parse_memory_command("/memory remember term_mapping    =>value").kind,
            MemoryCommandKind.NONE,
        )

    def test_value_may_itself_contain_the_arrow_token(self) -> None:
        """``=>`` 只在第一次出现处切分，value 侧允许再出现形似箭头的文本。"""

        result = parse_memory_command("/memory remember term_mapping k => v => still value")
        self.assertEqual(result.memory_key, "k")
        self.assertEqual(result.memory_value, "v => still value")

    def test_key_over_length_limit_is_none(self) -> None:
        long_key = "k" * 201
        self.assertEqual(
            parse_memory_command(f"/memory remember term_mapping {long_key} => v").kind,
            MemoryCommandKind.NONE,
        )

    def test_key_at_length_limit_is_accepted(self) -> None:
        key = "k" * 200
        result = parse_memory_command(f"/memory remember term_mapping {key} => v")
        self.assertEqual(result.kind, MemoryCommandKind.REMEMBER)

    def test_value_over_length_limit_is_none(self) -> None:
        long_value = "v" * 2001
        self.assertEqual(
            parse_memory_command(f"/memory remember term_mapping k => {long_value}").kind,
            MemoryCommandKind.NONE,
        )

    def test_value_at_length_limit_is_accepted(self) -> None:
        value = "v" * 2000
        result = parse_memory_command(f"/memory remember term_mapping k => {value}")
        self.assertEqual(result.kind, MemoryCommandKind.REMEMBER)

    def test_missing_type_token_is_none(self) -> None:
        self.assertEqual(
            parse_memory_command("/memory remember k => v").kind, MemoryCommandKind.NONE
        )


class NonMatchingInputTests(unittest.TestCase):
    def test_non_string_input_is_none(self) -> None:
        self.assertEqual(parse_memory_command(None).kind, MemoryCommandKind.NONE)
        self.assertEqual(parse_memory_command(123).kind, MemoryCommandKind.NONE)

    def test_plain_business_question_is_none(self) -> None:
        self.assertEqual(
            parse_memory_command("上个月大尼日的销售额是多少").kind, MemoryCommandKind.NONE
        )

    def test_prefix_without_word_boundary_is_none(self) -> None:
        """``/memoryabc`` 不是 /memory 命令——没有词边界。"""

        self.assertEqual(parse_memory_command("/memoryabc list").kind, MemoryCommandKind.NONE)

    def test_unrelated_slash_command_is_none(self) -> None:
        self.assertEqual(parse_memory_command("/config").kind, MemoryCommandKind.NONE)


class IsMemoryCommandMessageTests(unittest.TestCase):
    def test_bare_prefix_is_true(self) -> None:
        self.assertTrue(is_memory_command_message("/memory"))

    def test_prefix_with_trailing_content_is_true(self) -> None:
        self.assertTrue(is_memory_command_message("/memory anything even malformed"))

    def test_case_insensitive(self) -> None:
        self.assertTrue(is_memory_command_message("/MEMORY list"))

    def test_no_word_boundary_is_false(self) -> None:
        self.assertFalse(is_memory_command_message("/memoryabc"))

    def test_unrelated_text_is_false(self) -> None:
        self.assertFalse(is_memory_command_message("/config"))
        self.assertFalse(is_memory_command_message("普通问题"))

    def test_non_string_is_false(self) -> None:
        self.assertFalse(is_memory_command_message(None))


class UnrecognizedSlashInteractionTests(unittest.TestCase):
    """安全边界：新增 /memory 豁免必须精确，不能让其它 / 开头文本一并被放行
    （Trace #304 批次 5 直修的既有红线，回归防护）。"""

    def test_well_formed_memory_command_is_not_flagged_unrecognized(self) -> None:
        self.assertFalse(is_unrecognized_slash_message("/memory list"))

    def test_malformed_memory_command_is_still_not_flagged_unrecognized(self) -> None:
        """格式写错的 /memory 消息同样豁免——它该得到 /memory 专属用法提示，
        不是与 /config 共用的泛用拒绝文案（见 commands.py 文档）。"""

        self.assertFalse(is_unrecognized_slash_message("/memory rember typo => oops"))
        self.assertFalse(is_unrecognized_slash_message("/memory"))

    def test_unrelated_slash_commands_are_still_flagged_unrecognized(self) -> None:
        for text in ("/config", "/model", "/help", "/loop", "/memoryabc"):
            with self.subTest(text=text):
                self.assertTrue(is_unrecognized_slash_message(text))

    def test_existing_known_commands_are_unaffected(self) -> None:
        self.assertFalse(is_unrecognized_slash_message("/new"))
        self.assertFalse(is_unrecognized_slash_message("/stop"))
        self.assertEqual(parse_command("/new"), Command.NEW)
        self.assertEqual(parse_command("/stop"), Command.STOP)

    def test_plain_text_containing_a_slash_mid_sentence_is_unaffected(self) -> None:
        self.assertFalse(is_unrecognized_slash_message("8/26 的数据是多少"))


if __name__ == "__main__":
    unittest.main()
