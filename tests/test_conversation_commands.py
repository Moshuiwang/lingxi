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
        self.assertEqual(parse_memory_command("/memory list extra").kind, MemoryCommandKind.NONE)

    def test_clear_with_extra_argument_is_none(self) -> None:
        self.assertEqual(parse_memory_command("/memory clear now").kind, MemoryCommandKind.NONE)

    def test_bare_memory_with_no_subcommand_is_none(self) -> None:
        self.assertEqual(parse_memory_command("/memory").kind, MemoryCommandKind.NONE)
        self.assertEqual(parse_memory_command("/memory   ").kind, MemoryCommandKind.NONE)

    def test_tab_separated_subcommand_is_recognized(self) -> None:
        """P2-4（opus 审查）：词边界判定不能只认 ASCII 半角空格——``/memory``
        后面紧跟 Tab 时，这条消息的第一个词依然是 ``/memory``，必须落进 /memory
        命令面（哪怕子命令解析结果仍然可能是 NONE，见
        ``IsMemoryCommandMessageTests``/``UnrecognizedSlashInteractionTests`` 的
        对应用例），不能被当成完全无关的斜杠输入。

        变异锚点：把 ``is_memory_command_message`` 的 ``str.split()`` 词边界
        判定改回 ``startswith(prefix + " ")``，本用例会从 LIST 变红成 NONE。
        """

        self.assertEqual(
            parse_memory_command("/memory\tlist"), MemoryCommand(kind=MemoryCommandKind.LIST)
        )


class NewlineInjectionGuardTests(unittest.TestCase):
    """P2（Trace #373 H3 批 codex 外审②修复②）：``/memory`` 的子命令/参数
    词边界只在**第一行**内认水平空白（``[ \\t]+``）——消息里若存在换行且换行
    之后还有非空内容，一律判定为不构成合法 /memory 命令，落 ``NONE``（调用方
    渲染 usage_help），不能被当成"第一行是前缀、第二行是子命令"这样跨行拼出
    一个真实命令来执行。上一修复包把词边界判定改成 ``str.split()`` 默认空白
    语义（含换行）是这条洞口的成因：``/memory\\nclear`` 这种两行消息的第一行
    恰好是 ``/memory``、第二行恰好是 ``clear``，会被旧逻辑当成合法 clear 命令。

    变异锚点：把 ``parse_memory_command`` 里「``has_newline and
    after_newline.strip()`` → 直接 ``NONE``」这段判据删掉，本组用例会从
    ``NONE`` 变红成 ``CLEAR``/``FORGET``。
    """

    def test_newline_then_clear_is_none_not_a_real_clear(self) -> None:
        self.assertEqual(parse_memory_command("/memory\nclear").kind, MemoryCommandKind.NONE)

    def test_newline_then_forget_with_a_valid_id_is_none(self) -> None:
        valid_id = "mem_01ARZ3NDEKTSV4RRFFQ69G5FAV"
        self.assertEqual(
            parse_memory_command(f"/memory\nforget {valid_id}").kind, MemoryCommandKind.NONE
        )

    def test_a_complete_first_line_command_with_trailing_prose_is_none(self) -> None:
        """更典型的意外触发场景：多行粘贴消息，第一行本身就是一条完整命令
        ``/memory clear``，第二行往后是用户真正想问的正文——这不是"跨行拼出
        子命令"（``first_line`` 已经足够识别出 ``clear``），必须靠「换行后还
        有非空内容」这条独立判据拦下，否则粘贴一段以 ``/memory clear`` 开头
        的多行消息会被误判成真实清空指令。

        变异锚点：这条用例专门用来区分"用 first_line 而不是整条消息做子命令
        提取"与「换行后有非空内容 → NONE」这两道防线——只去掉后者（前者仍在），
        本用例会从 ``NONE`` 变红成 ``CLEAR``。
        """

        self.assertEqual(
            parse_memory_command("/memory clear\n请帮我看看这段话怎么翻译").kind,
            MemoryCommandKind.NONE,
        )

    def test_trailing_newline_with_only_whitespace_after_is_unaffected(self) -> None:
        """反向哨兵：单行命令后面只是跟了个换行/空白收尾（常见于聊天客户端
        自动加的结尾换行），不该被这道新判据误伤——依然是合法的 clear。"""

        self.assertEqual(parse_memory_command("/memory clear\n").kind, MemoryCommandKind.CLEAR)
        self.assertEqual(parse_memory_command("/memory clear\n   \n").kind, MemoryCommandKind.CLEAR)

    def test_single_line_tab_separated_subcommand_still_recognized(self) -> None:
        """反向哨兵：本修复只收紧「跨行」，不影响同一行内的 Tab 分隔词边界
        （上一修复包 P2-4 的既有意图，见 ``ListClearParsingTests``
        对应用例）。"""

        self.assertEqual(parse_memory_command("/memory\tlist").kind, MemoryCommandKind.LIST)

    def test_still_belongs_to_the_memory_command_surface_not_unrecognized_slash(self) -> None:
        """即使子命令解析因跨行而判 NONE，这条消息依然属于 /memory 命令面
        （渲染专属 usage_help，不是与 /config 共用的泛用拒绝文案）——回归
        ``is_unrecognized_slash_message`` 的既有豁免边界。"""

        self.assertFalse(is_unrecognized_slash_message("/memory\nclear"))


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


class ForgetSerialParsingTests(unittest.TestCase):
    """rc22 B-8-1（#439 TOP-10）：``/memory forget`` 新增的短序号参数形状——序号
    到具体 ``memory_id`` 的解析是 I/O（需要查一次记忆列表），不在本模块完成，
    这里只钉「哪些字面量能/不能被识别成序号」，解析后置行为见
    ``tests/test_gateway_pipeline.py`` 的 ``MemoryCommandDispatchTests``。

    变异锚点：把 ``commands._parse_memory_serial`` 的「``token != str(int(token))``
    前导零/符号拒绝」这段判据删掉，``test_leading_zero_is_rejected``/
    ``test_plus_sign_is_rejected`` 两条会从 ``NONE`` 变红成非 ``NONE``。
    """

    def test_single_digit_serial_is_recognized(self) -> None:
        self.assertEqual(
            parse_memory_command("/memory forget 1"),
            MemoryCommand(kind=MemoryCommandKind.FORGET, memory_serial=1),
        )

    def test_multi_digit_serial_is_recognized(self) -> None:
        self.assertEqual(
            parse_memory_command("/memory forget 42"),
            MemoryCommand(kind=MemoryCommandKind.FORGET, memory_serial=42),
        )

    def test_zero_is_rejected(self) -> None:
        """序号从 1 开始，``0`` 不对应任何展示行。"""

        self.assertEqual(parse_memory_command("/memory forget 0").kind, MemoryCommandKind.NONE)

    def test_leading_zero_is_rejected(self) -> None:
        """``01`` 不消歧——不猜测它是序号 1 还是格式错误，直接判 NONE。"""

        self.assertEqual(parse_memory_command("/memory forget 01").kind, MemoryCommandKind.NONE)

    def test_plus_sign_is_rejected(self) -> None:
        self.assertEqual(parse_memory_command("/memory forget +1").kind, MemoryCommandKind.NONE)

    def test_negative_sign_is_rejected(self) -> None:
        self.assertEqual(parse_memory_command("/memory forget -1").kind, MemoryCommandKind.NONE)

    def test_decimal_is_rejected(self) -> None:
        self.assertEqual(parse_memory_command("/memory forget 1.0").kind, MemoryCommandKind.NONE)

    def test_trailing_extra_token_after_serial_is_none(self) -> None:
        self.assertEqual(
            parse_memory_command("/memory forget 1 extra").kind, MemoryCommandKind.NONE
        )

    def test_serial_result_carries_no_memory_id(self) -> None:
        """``memory_id``/``memory_serial`` 二选一——序号形态解析结果不带 id。"""

        result = parse_memory_command("/memory forget 7")
        self.assertIsNone(result.memory_id)
        self.assertEqual(result.memory_serial, 7)

    def test_a_full_mem_id_that_happens_to_be_all_digits_after_prefix_still_parses_as_id(
        self,
    ) -> None:
        """互斥优先级：先按 ``mem_`` 前缀 + ULID 判定，只有不匹配时才退而解析
        成序号——两种形状在语法层面互不相容（ULID 不是纯十进制数字），这里只
        证明分支顺序不会把一个本该被识别为 id 的合法 id 误判成序号。"""

        valid_id = "mem_01ARZ3NDEKTSV4RRFFQ69G5FAV"
        result = parse_memory_command(f"/memory forget {valid_id}")
        self.assertEqual(result.memory_id, valid_id)
        self.assertIsNone(result.memory_serial)


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

    def test_tab_or_newline_separated_is_true(self) -> None:
        """P2-4（opus 审查）：词边界不只认半角空格。"""

        self.assertTrue(is_memory_command_message("/memory\tlist"))
        self.assertTrue(is_memory_command_message("/memory\nlist"))

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


class NonDecimalDigitMemorySerialTests(unittest.TestCase):
    """A-2 同类（Trace #544）：``/memory forget ²⁴`` 这条路径**任何用户**都能走到。

    ``"²⁴".isdigit()`` 为真而 ``int("²⁴")`` 抛 ``ValueError``——序号解析必须用
    ``isdecimal()``，不能靠上层把一个奇怪字符兜成一次异常。
    """

    def test_superscript_serial_is_not_a_command_and_does_not_raise(self) -> None:
        command = parse_memory_command("/memory forget ²⁴")

        self.assertEqual(command.kind, MemoryCommandKind.NONE)

    def test_plain_decimal_serial_still_parses(self) -> None:
        command = parse_memory_command("/memory forget 3")

        self.assertEqual(command.memory_serial, 3)
