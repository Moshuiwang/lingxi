"""``core/admin/commands.py`` 的封闭语法解析（Issue #95 S-M-01；Issue #96 S-M-02
新增 ``suspend``/``resume`` 两个写命令的解析）。

认领断言：V-管理-26（命令面语法封闭：任何不匹配已知形状的输入一律 ``UNKNOWN``，
含典型的命令注入/任意查询形态——SQL 元字符、分号、空白、系统命令关键字——一律拒绝
识别为可执行命令，不产生任何查询条件）。``suspend``/``resume`` 只做**语法**解析，
不在这里判断目标是否存在或当前状态是否允许该动作——那是 ``core/admin/pending_
action.decide_prepare`` 的职责（见 ``tests/test_pending_action.py``）。
"""

from __future__ import annotations

import unittest

from lingxi.core.admin.commands import (
    DEFAULT_AUDIT_WINDOW_HOURS,
    MAX_AUDIT_WINDOW_HOURS,
    AdminCommandKind,
    parse_admin_command,
)
from lingxi.core.ids import new_ulid


class HelpParsingTests(unittest.TestCase):
    def test_help_recognized(self) -> None:
        command = parse_admin_command("/admin help")
        self.assertEqual(command.kind, AdminCommandKind.HELP)

    def test_help_case_insensitive_and_whitespace_tolerant(self) -> None:
        command = parse_admin_command("  /ADMIN Help  ")
        self.assertEqual(command.kind, AdminCommandKind.HELP)

    def test_help_with_extra_arguments_is_unknown(self) -> None:
        command = parse_admin_command("/admin help now")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)


class QueryUserParsingTests(unittest.TestCase):
    def test_valid_identifier_recognized(self) -> None:
        command = parse_admin_command("/admin user ou_abc123")
        self.assertEqual(command.kind, AdminCommandKind.QUERY_USER)
        self.assertEqual(command.identifier, "ou_abc123")

    def test_missing_identifier_is_unknown(self) -> None:
        command = parse_admin_command("/admin user")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_extra_argument_is_unknown(self) -> None:
        command = parse_admin_command("/admin user ou_abc123 extra")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_identifier_with_whitespace_is_unknown(self) -> None:
        command = parse_admin_command("/admin user ou abc")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_email_shaped_identifier_is_recognized(self) -> None:
        """#439 A 档：标识参数支持邮箱。是否真的按邮箱反查是 router.py/
        adapters 的职责，这里只验证语法层放行含 ``@`` 的标识。"""

        command = parse_admin_command("/admin user someone@example.com")
        self.assertEqual(command.kind, AdminCommandKind.QUERY_USER)
        self.assertEqual(command.identifier, "someone@example.com")


class QueryAuditParsingTests(unittest.TestCase):
    def test_bare_audit_uses_defaults(self) -> None:
        command = parse_admin_command("/admin audit")
        self.assertEqual(command.kind, AdminCommandKind.QUERY_AUDIT)
        self.assertIsNone(command.identifier)
        self.assertEqual(command.window_hours, DEFAULT_AUDIT_WINDOW_HOURS)

    def test_audit_with_identifier_only(self) -> None:
        command = parse_admin_command("/admin audit ou_abc123")
        self.assertEqual(command.kind, AdminCommandKind.QUERY_AUDIT)
        self.assertEqual(command.identifier, "ou_abc123")
        self.assertEqual(command.window_hours, DEFAULT_AUDIT_WINDOW_HOURS)

    def test_audit_with_hours_only_all_digit_token(self) -> None:
        """单个额外参数全为数字时按小时数解释，不按标识解释——判据是确定性的。"""

        command = parse_admin_command("/admin audit 48")
        self.assertEqual(command.kind, AdminCommandKind.QUERY_AUDIT)
        self.assertIsNone(command.identifier)
        self.assertEqual(command.window_hours, 48)

    def test_audit_with_identifier_and_hours(self) -> None:
        command = parse_admin_command("/admin audit ou_abc123 72")
        self.assertEqual(command.kind, AdminCommandKind.QUERY_AUDIT)
        self.assertEqual(command.identifier, "ou_abc123")
        self.assertEqual(command.window_hours, 72)

    def test_hours_at_lower_bound_accepted(self) -> None:
        command = parse_admin_command("/admin audit 1")
        self.assertEqual(command.kind, AdminCommandKind.QUERY_AUDIT)
        self.assertEqual(command.window_hours, 1)

    def test_hours_at_upper_bound_accepted(self) -> None:
        command = parse_admin_command(f"/admin audit {MAX_AUDIT_WINDOW_HOURS}")
        self.assertEqual(command.kind, AdminCommandKind.QUERY_AUDIT)
        self.assertEqual(command.window_hours, MAX_AUDIT_WINDOW_HOURS)

    def test_hours_zero_rejected(self) -> None:
        command = parse_admin_command("/admin audit 0")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_hours_over_upper_bound_rejected(self) -> None:
        command = parse_admin_command(f"/admin audit {MAX_AUDIT_WINDOW_HOURS + 1}")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_negative_looking_token_is_treated_as_identifier_not_hours(self) -> None:
        # "-5" 不是全数字（`str.isdigit()` 对带符号的字符串为 False），因此走标识
        # 分支而不是小时数分支；标识形状允许连字符，需要确认它不会被误判成
        # "合法但奇怪的小时数"，也不会被当成负数小时静默钳制。
        command = parse_admin_command("/admin audit -5")
        self.assertEqual(command.kind, AdminCommandKind.QUERY_AUDIT)
        self.assertEqual(command.identifier, "-5")
        self.assertEqual(command.window_hours, DEFAULT_AUDIT_WINDOW_HOURS)

    def test_too_many_arguments_rejected(self) -> None:
        command = parse_admin_command("/admin audit ou_abc123 72 extra")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)


class SuspendResumeParsingTests(unittest.TestCase):
    def test_suspend_with_valid_identifier_recognized(self) -> None:
        command = parse_admin_command("/admin suspend ou_abc123")
        self.assertEqual(command.kind, AdminCommandKind.SUSPEND_USER)
        self.assertEqual(command.identifier, "ou_abc123")

    def test_resume_with_valid_identifier_recognized(self) -> None:
        command = parse_admin_command("/admin resume ou_abc123")
        self.assertEqual(command.kind, AdminCommandKind.RESUME_USER)
        self.assertEqual(command.identifier, "ou_abc123")

    def test_suspend_missing_identifier_is_unknown(self) -> None:
        command = parse_admin_command("/admin suspend")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_resume_missing_identifier_is_unknown(self) -> None:
        command = parse_admin_command("/admin resume")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_suspend_extra_argument_is_unknown(self) -> None:
        command = parse_admin_command("/admin suspend ou_abc123 extra")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_resume_extra_argument_is_unknown(self) -> None:
        command = parse_admin_command("/admin resume ou_abc123 extra")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_suspend_identifier_with_whitespace_is_unknown(self) -> None:
        command = parse_admin_command("/admin suspend ou abc")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_suspend_case_insensitive_and_whitespace_tolerant(self) -> None:
        command = parse_admin_command("  /ADMIN Suspend  ou_abc123  ")
        self.assertEqual(command.kind, AdminCommandKind.SUSPEND_USER)
        self.assertEqual(command.identifier, "ou_abc123")

    def test_suspend_sql_injection_shaped_identifier_rejected(self) -> None:
        command = parse_admin_command("/admin suspend 1; DROP TABLE app_user;--")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_resume_shell_metacharacter_identifier_rejected(self) -> None:
        command = parse_admin_command("/admin resume $(rm -rf /)")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)


class GrantSuppressPermissionParsingTests(unittest.TestCase):
    """``/admin grant_permission``/``/admin suppress_permission`` 的解析
    （#319 S-P-1b 设计卡）：``<identifier> <company_id> <metric_name> <reason...>``，
    前三个 token 复用 ``_IDENTIFIER_PATTERN``，``reason`` 是尾部剩余全部 token
    拼接的自由文本，非空白、≤500 字符——解析防呆覆盖空白 reason 与形状不对的
    company_id/metric_name（非法字符）两类输入。"""

    def test_grant_with_single_word_reason_recognized(self) -> None:
        command = parse_admin_command("/admin grant_permission ou_abc123 1011 daily_active 特批")
        self.assertEqual(command.kind, AdminCommandKind.GRANT_PERMISSION)
        self.assertEqual(command.identifier, "ou_abc123")
        self.assertEqual(command.company_id, "1011")
        self.assertEqual(command.metric_name, "daily_active")
        self.assertEqual(command.reason, "特批")

    def test_suppress_with_multi_word_reason_is_joined_back_together(self) -> None:
        command = parse_admin_command(
            "/admin suppress_permission ou_abc123 1011 daily_active 离职 交接 期间 收回"
        )
        self.assertEqual(command.kind, AdminCommandKind.SUPPRESS_PERMISSION)
        self.assertEqual(command.reason, "离职 交接 期间 收回")

    def test_case_insensitive_and_whitespace_tolerant(self) -> None:
        command = parse_admin_command(
            "  /ADMIN Grant_Permission  ou_abc123  1011  daily_active  特批  "
        )
        self.assertEqual(command.kind, AdminCommandKind.GRANT_PERMISSION)
        self.assertEqual(command.reason, "特批")

    def test_missing_reason_is_unknown(self) -> None:
        command = parse_admin_command("/admin grant_permission ou_abc123 1011 daily_active")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_missing_metric_name_is_unknown(self) -> None:
        command = parse_admin_command("/admin grant_permission ou_abc123 1011")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_blank_reason_after_strip_is_unknown(self) -> None:
        """reason 全为空白字符（拼接后 strip 为空）→ UNKNOWN，不当成合法的空原因。"""

        command = parse_admin_command("/admin grant_permission ou_abc123 1011 daily_active    ")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_reason_over_length_limit_is_unknown(self) -> None:
        long_reason = "字" * 501
        command = parse_admin_command(
            f"/admin grant_permission ou_abc123 1011 daily_active {long_reason}"
        )
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_reason_at_length_limit_is_accepted(self) -> None:
        boundary_reason = "字" * 500
        command = parse_admin_command(
            f"/admin grant_permission ou_abc123 1011 daily_active {boundary_reason}"
        )
        self.assertEqual(command.kind, AdminCommandKind.GRANT_PERMISSION)
        self.assertEqual(command.reason, boundary_reason)

    def test_company_id_with_illegal_character_is_unknown(self) -> None:
        """公司标识形状不对（复用 ``_IDENTIFIER_PATTERN``，含空白/引号等）→
        UNKNOWN——与既有 ``suspend``/``resume`` 的形状封闭纪律一致。"""

        command = parse_admin_command("/admin grant_permission ou_abc123 '1011' daily_active 特批")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_metric_name_with_illegal_character_is_unknown(self) -> None:
        command = parse_admin_command(
            "/admin grant_permission ou_abc123 1011 daily;active 特批"
        )
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_target_identifier_sql_injection_shaped_is_unknown(self) -> None:
        command = parse_admin_command(
            "/admin grant_permission 1; DROP TABLE app_user;-- 1011 daily_active 特批"
        )
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_grant_and_suppress_are_distinct_kinds(self) -> None:
        grant = parse_admin_command("/admin grant_permission ou_abc123 1011 daily_active 特批")
        suppress = parse_admin_command("/admin suppress_permission ou_abc123 1011 daily_active 特批")
        self.assertEqual(grant.kind, AdminCommandKind.GRANT_PERMISSION)
        self.assertEqual(suppress.kind, AdminCommandKind.SUPPRESS_PERMISSION)

    def test_identifier_accepts_email_shape(self) -> None:
        """#439 A 档：标识参数支持邮箱——``_IDENTIFIER_PATTERN`` 新增 ``@``。"""

        command = parse_admin_command(
            "/admin grant_permission someone@example.com 1011 daily_active 特批"
        )
        self.assertEqual(command.kind, AdminCommandKind.GRANT_PERMISSION)
        self.assertEqual(command.identifier, "someone@example.com")

    def test_metric_name_accepts_chinese_alias(self) -> None:
        """#439 A 档：指标支持中文别名——``_METRIC_TOKEN_PATTERN`` 放行 CJK，
        与 ``identifier``/``company_id`` 仍然只认 ``_IDENTIFIER_PATTERN``（更窄）
        分开验证。是否真的命中别名表是 router.py/adapters 的职责，这里只验证
        语法层放行。"""

        command = parse_admin_command(
            "/admin grant_permission ou_abc123 1011 新增用户数 特批"
        )
        self.assertEqual(command.kind, AdminCommandKind.GRANT_PERMISSION)
        self.assertEqual(command.metric_name, "新增用户数")

    def test_company_id_does_not_accept_chinese_even_though_metric_name_does(self) -> None:
        """否定断言：company_id 复用更窄的 ``_IDENTIFIER_PATTERN``（不含 CJK），
        中文放宽只发生在 metric_name 这一个字段上，不是全局放开。"""

        command = parse_admin_command(
            "/admin grant_permission ou_abc123 一零一一 daily_active 特批"
        )
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)


#: 一个形状合法的本地权限覆盖行标识（``lpo_`` 前缀 + 26 位 Crockford Base32
#: ULID，与 ``core/ids.new_id("lpo")`` 的生成形状逐字对应），供本文件的收回解析
#: 用例复用——固定字面量而不是每个用例现生成一个，保持用例之间的期望值可读。
_VALID_OVERRIDE_ID = "lpo_01JGFJJZ008XSHEADGG8V74SPC"


class RevokePermissionParsingTests(unittest.TestCase):
    """``/admin revoke_permission`` 的解析（卡 B 设计卡）：
    ``<override_id> <reason...>``——比 ``grant_permission``/``suppress_permission``
    少两个 token（``company_id``/``metric_name``，收回按行本身定位，不按键定位）。
    ``override_id`` 的形状是 ``lpo_`` 前缀 ULID，不是 ``_IDENTIFIER_PATTERN``。"""

    def test_valid_override_id_and_single_word_reason_recognized(self) -> None:
        command = parse_admin_command(f"/admin revoke_permission {_VALID_OVERRIDE_ID} 离职")
        self.assertEqual(command.kind, AdminCommandKind.REVOKE_PERMISSION)
        self.assertEqual(command.identifier, _VALID_OVERRIDE_ID)
        self.assertEqual(command.reason, "离职")
        # 收回命令不填 company_id/metric_name——按行本身定位，见 AdminCommand 文档。
        self.assertIsNone(command.company_id)
        self.assertIsNone(command.metric_name)

    def test_multi_word_reason_is_joined_back_together(self) -> None:
        command = parse_admin_command(
            f"/admin revoke_permission {_VALID_OVERRIDE_ID} 离职 交接 期间 收回"
        )
        self.assertEqual(command.kind, AdminCommandKind.REVOKE_PERMISSION)
        self.assertEqual(command.reason, "离职 交接 期间 收回")

    def test_case_insensitive_and_whitespace_tolerant(self) -> None:
        command = parse_admin_command(
            f"  /ADMIN Revoke_Permission  {_VALID_OVERRIDE_ID}  离职  "
        )
        self.assertEqual(command.kind, AdminCommandKind.REVOKE_PERMISSION)
        self.assertEqual(command.identifier, _VALID_OVERRIDE_ID)
        self.assertEqual(command.reason, "离职")

    def test_missing_reason_is_unknown(self) -> None:
        command = parse_admin_command(f"/admin revoke_permission {_VALID_OVERRIDE_ID}")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_blank_reason_after_strip_is_unknown(self) -> None:
        command = parse_admin_command(f"/admin revoke_permission {_VALID_OVERRIDE_ID}    ")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_reason_over_length_limit_is_unknown(self) -> None:
        long_reason = "字" * 501
        command = parse_admin_command(
            f"/admin revoke_permission {_VALID_OVERRIDE_ID} {long_reason}"
        )
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_reason_at_length_limit_is_accepted(self) -> None:
        boundary_reason = "字" * 500
        command = parse_admin_command(
            f"/admin revoke_permission {_VALID_OVERRIDE_ID} {boundary_reason}"
        )
        self.assertEqual(command.kind, AdminCommandKind.REVOKE_PERMISSION)
        self.assertEqual(command.reason, boundary_reason)

    def test_open_id_shaped_identifier_is_rejected_not_an_override_id(self) -> None:
        """否定断言：``override_id`` 的形状是 ``lpo_`` 前缀 ULID，一个形状合法的
        open_id（``suspend``/``resume``/``user`` 等命令使用的标识）不满足这个更
        窄的形状——两种标识不能互相冒充。"""

        command = parse_admin_command("/admin revoke_permission ou_abc123 离职")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_lpo_prefix_without_a_valid_ulid_suffix_is_rejected(self) -> None:
        command = parse_admin_command("/admin revoke_permission lpo_not_a_real_ulid 离职")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_sql_injection_shaped_override_id_rejected(self) -> None:
        command = parse_admin_command(
            "/admin revoke_permission 1; DROP TABLE local_permission_override;-- 离职"
        )
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_missing_override_id_is_unknown(self) -> None:
        command = parse_admin_command("/admin revoke_permission")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)


class RevokePermissionShapeTwoParsingTests(unittest.TestCase):
    """``/admin revoke_permission`` 形状 2（#439 A 档新增）：
    ``<identifier> <company_id> <metric_name> <reason...>``——与 grant/suppress
    同一参数形状，服务端反查覆盖 ID（router.py 职责，这里只验证语法层解析）。"""

    def test_identifier_company_metric_reason_shape_is_recognized(self) -> None:
        command = parse_admin_command(
            "/admin revoke_permission ou_abc123 1011 daily_active 离职"
        )
        self.assertEqual(command.kind, AdminCommandKind.REVOKE_PERMISSION)
        self.assertEqual(command.identifier, "ou_abc123")
        self.assertEqual(command.company_id, "1011")
        self.assertEqual(command.metric_name, "daily_active")
        self.assertEqual(command.reason, "离职")

    def test_email_identifier_and_chinese_metric_alias_shape_is_recognized(self) -> None:
        command = parse_admin_command(
            "/admin revoke_permission someone@example.com 1011 新增用户数 离职"
        )
        self.assertEqual(command.kind, AdminCommandKind.REVOKE_PERMISSION)
        self.assertEqual(command.identifier, "someone@example.com")
        self.assertEqual(command.metric_name, "新增用户数")

    def test_multi_word_reason_is_joined_back_together(self) -> None:
        command = parse_admin_command(
            "/admin revoke_permission ou_abc123 1011 daily_active 离职 交接 期间 收回"
        )
        self.assertEqual(command.kind, AdminCommandKind.REVOKE_PERMISSION)
        self.assertEqual(command.reason, "离职 交接 期间 收回")

    def test_missing_reason_is_unknown(self) -> None:
        command = parse_admin_command("/admin revoke_permission ou_abc123 1011 daily_active")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_an_override_id_shaped_first_token_never_falls_through_to_shape_two(self) -> None:
        """否定断言：第一个 token 形似 override_id 时，即使凑够 4 个 token，也必须
        走形状 1（按 override_id 定位），不会被形状 2 的解析规则接管——两种形状
        的判据是"第一个 token 长什么样"，不是"数了多少个 token"（见
        ``_parse_revoke_permission_command`` 文档）。"""

        command = parse_admin_command(
            f"/admin revoke_permission {_VALID_OVERRIDE_ID} 1011 daily_active 离职"
        )
        self.assertEqual(command.kind, AdminCommandKind.REVOKE_PERMISSION)
        # 形状 1 的解析把 rest[1:] 全部拼成 reason，不会把 "1011"/"daily_active"
        # 单独解释成 company_id/metric_name。
        self.assertEqual(command.identifier, _VALID_OVERRIDE_ID)
        self.assertEqual(command.reason, "1011 daily_active 离职")
        self.assertIsNone(command.company_id)
        self.assertIsNone(command.metric_name)


class QueryTraceParsingTests(unittest.TestCase):
    """``/admin trace <追溯号>``（Issue #337）：``<追溯号>`` 必须是裸 ULID，
    与 ``inbound_event.trace_id``/``onboarding_failure.trace_id`` 的存储形状
    一致——不加前缀，与 ``user``/``suspend`` 等命令用的 ``_IDENTIFIER_PATTERN``
    是两条独立的校验（那条允许任意 1–128 字符标识形状，本命令刻意更窄）。"""

    def test_valid_ulid_recognized(self) -> None:
        trace_id = new_ulid()
        command = parse_admin_command(f"/admin trace {trace_id}")
        self.assertEqual(command.kind, AdminCommandKind.QUERY_TRACE)
        self.assertEqual(command.identifier, trace_id)

    def test_lowercase_ulid_recognized(self) -> None:
        """``is_ulid`` 大小写不敏感——与 ``core/ids.is_ulid`` 文档一致。"""

        trace_id = new_ulid().lower()
        command = parse_admin_command(f"/admin trace {trace_id}")
        self.assertEqual(command.kind, AdminCommandKind.QUERY_TRACE)
        self.assertEqual(command.identifier, trace_id)

    def test_missing_identifier_is_unknown(self) -> None:
        self.assertEqual(parse_admin_command("/admin trace").kind, AdminCommandKind.UNKNOWN)

    def test_extra_argument_is_unknown(self) -> None:
        trace_id = new_ulid()
        command = parse_admin_command(f"/admin trace {trace_id} extra")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_non_ulid_identifier_is_unknown(self) -> None:
        """否定断言：不是合法 ULID 形状（长度不对、含非 Crockford Base32 字符）
        一律 ``UNKNOWN``——``/admin user`` 那种宽松的 ``_IDENTIFIER_PATTERN``
        不适用于本命令，防止把任意字符串当追溯号去查库。"""

        command = parse_admin_command("/admin trace ou_abc123")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_ulid_shaped_but_wrong_length_is_unknown(self) -> None:
        trace_id = new_ulid()[:-1]  # 少一位
        command = parse_admin_command(f"/admin trace {trace_id}")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_sql_injection_shaped_trace_id_rejected(self) -> None:
        command = parse_admin_command("/admin trace 1; DROP TABLE onboarding_failure;--")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)


class UnknownAndInjectionShapedInputTests(unittest.TestCase):
    """否定断言（验证与门禁 §八）：命令注入/任意查询形态一律得到 ``UNKNOWN``，
    永远不会被解析成任何可执行的查询条件。"""

    def test_empty_text_is_unknown(self) -> None:
        self.assertEqual(parse_admin_command("").kind, AdminCommandKind.UNKNOWN)

    def test_non_string_input_is_unknown(self) -> None:
        self.assertEqual(parse_admin_command(None).kind, AdminCommandKind.UNKNOWN)  # type: ignore[arg-type]
        self.assertEqual(parse_admin_command(12345).kind, AdminCommandKind.UNKNOWN)  # type: ignore[arg-type]

    def test_plain_business_question_is_unknown(self) -> None:
        """普通问数式文本（专用账号非管理路径时的历史行为）不被误判成任何命令。"""

        self.assertEqual(
            parse_admin_command("本月销售额是多少").kind, AdminCommandKind.UNKNOWN
        )

    def test_unknown_subcommand_is_unknown(self) -> None:
        self.assertEqual(parse_admin_command("/admin delete_user ou_1").kind, AdminCommandKind.UNKNOWN)

    def test_sql_injection_shaped_identifier_rejected(self) -> None:
        command = parse_admin_command("/admin user 1; DROP TABLE app_user;--")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_sql_select_star_attempt_rejected(self) -> None:
        command = parse_admin_command("/admin user' OR '1'='1")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_arbitrary_sql_as_subcommand_rejected(self) -> None:
        command = parse_admin_command("/admin SELECT * FROM app_user")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_shell_metacharacter_identifier_rejected(self) -> None:
        command = parse_admin_command("/admin user $(rm -rf /)")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)

    def test_prefix_without_leading_slash_is_unknown(self) -> None:
        self.assertEqual(parse_admin_command("admin help").kind, AdminCommandKind.UNKNOWN)

    def test_command_embedded_mid_message_is_unknown(self) -> None:
        """只认整条消息即命令本身的形状（与 `/stop`/`/new` 的既有判断同一姿态）：
        「帮我查一下 /admin help 怎么用」不能被当成命令执行。"""

        command = parse_admin_command("帮我查一下 /admin help 怎么用")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
