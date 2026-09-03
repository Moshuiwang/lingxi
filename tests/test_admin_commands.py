"""``core/admin/commands.py`` 的封闭语法解析（Issue #95 S-M-01；Issue #96 S-M-02
新增 ``suspend``/``resume`` 两个写命令的解析）。

认领断言：V-管理-26（命令面语法封闭：任何不匹配已知形状的输入一律 ``UNKNOWN``，
含典型的命令注入/任意查询形态——SQL 元字符、分号、空白、系统命令关键字——一律拒绝
识别为可执行命令，不产生任何查询条件）。``suspend``/``resume`` 只做**语法**解析，
不在这里判断目标是否存在或当前状态是否允许该动作——那是 ``core/admin/pending_
action.decide_prepare`` 的职责（见 ``tests/test_pending_action.py``）。
"""

from __future__ import annotations

import time
import unittest

from lingxi.core.admin.commands import (
    DEFAULT_AUDIT_WINDOW_HOURS,
    MAX_AUDIT_WINDOW_HOURS,
    AdminCommandKind,
    AdminRejectReason,
    _collapse_identifier_link_forms,
    describe_admin_tokens,
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


class RetiredPermissionCommandTests(unittest.TestCase):
    """``/admin grant_permission`` 与 ``/admin suppress_permission`` **已撤除**
    （Trace #544 D-5，产品负责人裁定；对抗审查 A-4 / P-4）。

    撤的理由不是语法层不严：这两条命令按"公司×指标"两个自由文本参数收本地权限覆盖，
    语法层只校验字符集、**不核对指标目录**，管理员因此可以写出目录外的公司与指标
    （审查 3 级复现：``company_id="9999", metric="bogus_metric_xyz"`` 确认后落库并被
    聚合发布）。裁定不是补一层目录校验，是撤掉入口——补充授权统一走管理卡的
    「银河职位×公司范围」表单，那里的取值全部来自服务端渲染的目录下拉。

    本类是**否定用例**：撤除之后这两个命令名与任何一个没听说过的命令名待遇相同，
    落在 ``UNKNOWN_SUBCOMMAND`` 上。``router.py`` 侧"不产生任何授权动作、不写任何
    授权审计"的断言在 ``tests/test_admin_router.py`` 的同名主题里。
    """

    RETIRED = ("grant_permission", "suppress_permission")

    def test_retired_commands_are_unknown_subcommand(self) -> None:
        for name in self.RETIRED:
            with self.subTest(name=name):
                command = parse_admin_command(
                    f"/admin {name} ou_abc123 1011 daily_active 特批"
                )
                self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
                self.assertEqual(command.reject_reason, AdminRejectReason.UNKNOWN_SUBCOMMAND)

    def test_retired_commands_are_unknown_regardless_of_argument_shape(self) -> None:
        """参数怎么写都不改变结论——撤的是命令名，不是某一种参数形状。"""

        for text in (
            "/admin grant_permission",
            "/admin grant_permission ou_abc123",
            "/admin GRANT_PERMISSION ou_abc123 1011 daily_active 特批",
            "/admin suppress_permission someone@example.com 1011 新增用户数 特批",
            "/admin suppress_permission ou_abc123 9999 bogus_metric_xyz 越权尝试",
        ):
            with self.subTest(text=text):
                command = parse_admin_command(text)
                self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
                self.assertEqual(command.reject_reason, AdminRejectReason.UNKNOWN_SUBCOMMAND)

    def test_command_kind_enum_no_longer_carries_the_retired_kinds(self) -> None:
        """枚举里也不留半截：没有任何取值能表示这两条命令。"""

        for name in ("GRANT_PERMISSION", "SUPPRESS_PERMISSION"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(AdminCommandKind, name))

    def test_position_scope_grant_still_parses(self) -> None:
        """不误伤：#493 的「职位×公司范围」补充授权仍然是被受理的命令。"""

        command = parse_admin_command("/admin grant_position ou_abc123 销售经理 1011 特批")
        self.assertEqual(command.kind, AdminCommandKind.GRANT_POSITION_PERMISSION)

    def test_revoke_permission_shape_two_still_parses(self) -> None:
        """不误伤：与撤除命令共用参数形状的 ``revoke_permission`` 形状 2 不受影响。"""

        command = parse_admin_command(
            "/admin revoke_permission ou_abc123 1011 daily_active 收回"
        )
        self.assertEqual(command.kind, AdminCommandKind.REVOKE_PERMISSION)
        self.assertEqual(command.company_id, "1011")
        self.assertEqual(command.metric_name, "daily_active")


#: 一个形状合法的本地权限覆盖行标识（``lpo_`` 前缀 + 26 位 Crockford Base32
#: ULID，与 ``core/ids.new_id("lpo")`` 的生成形状逐字对应），供本文件的收回解析
#: 用例复用——固定字面量而不是每个用例现生成一个，保持用例之间的期望值可读。
_VALID_OVERRIDE_ID = "lpo_01JGFJJZ008XSHEADGG8V74SPC"
_VALID_LEGACY_PERMISSION_GROUP_ID = "pac_01JGFJJZ008XSHEADGG8V74SPC"


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

    def test_legacy_pending_action_group_id_remains_a_group_target(self) -> None:
        """0081 基线曾把职位组 ID 写成 ``pac_``；存量不迁移时仍须能撤销整组。"""

        command = parse_admin_command(
            f"/admin revoke_permission {_VALID_LEGACY_PERMISSION_GROUP_ID} 管理卡撤销"
        )
        self.assertEqual(command.kind, AdminCommandKind.REVOKE_PERMISSION)
        self.assertEqual(command.identifier, _VALID_LEGACY_PERMISSION_GROUP_ID)

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


#: 一个只出现在测试里的公开形态邮箱（协作约定：夹具不得出现真实内部标识）。
_PLAIN_EMAIL = "someone@example.com"

#: Issue #492 的邮箱形态矩阵。值是公开邮箱的合成 token，键是形态名；它不是三条真实
#: 失败消息的逐字节 raw 回读，真实信封不再作为实现前置。
#:
#: 自动化矩阵、mutation 和本机门禁先行；候选冻结并部署后才由 PM 在管理员↔Bot-Test
#: p2p 私聊执行一次 L4a。矩阵只保留产品口径允许的裸邮箱、裸 ``mailto`` 和显示/目标
#: 一致的 markdown ``mailto``；尖括号、反引号、open_id 链接、不一致目标、空显示和
#: 任意其它链接由否定用例明确拒绝。
_LINKIFIED_EMAIL_FORMS: dict[str, str] = {
    "plain_email": _PLAIN_EMAIL,
    "markdown_link": f"[{_PLAIN_EMAIL}](mailto:{_PLAIN_EMAIL})",
    "bare_mailto": f"mailto:{_PLAIN_EMAIL}",
    "bare_mailto_uppercase_scheme": f"MAILTO:{_PLAIN_EMAIL}",
    "markdown_link_uppercase_scheme": f"[{_PLAIN_EMAIL}](MAILTO:{_PLAIN_EMAIL})",
}
_REJECTED_EMAIL_FORMS: dict[str, str] = {
    "angle_autolink": f"<{_PLAIN_EMAIL}>",
    "inline_code": f"`{_PLAIN_EMAIL}`",
    "empty_display": f"[](mailto:{_PLAIN_EMAIL})",
    "mismatched_display_and_target": f"[seen@example.com](mailto:{_PLAIN_EMAIL})",
    "linked_open_id": "[ou_abc123](mailto:ou_abc123)",
    "http_markdown_link": f"[{_PLAIN_EMAIL}](https://example.com/user)",
    "arbitrary_markdown_link": f"[{_PLAIN_EMAIL}](example.com)",
    "bare_mailto_open_id": "mailto:ou_abc123",
    "invalid_bare_mailto": "mailto:not-an-email",
    "plain_http_uri": f"http:{_PLAIN_EMAIL}",
}

#: 全部**吃邮箱**的命令入口（Issue #492 的 W0-2 全清单，已含三处校正：``/admin
#: trace`` 吃裸 ULID、``revoke_permission`` 形状 1 吃 ``lpo_``+ULID，两者都**不**
#: 吃邮箱，因此不在本表内）。职位授权的两个公开子命令是同一实际入口的别名，均须
#: 覆盖。值是命令模板，``{identifier}`` 是那一位邮箱参数。
_EMAIL_TAKING_COMMANDS: dict[str, tuple[str, AdminCommandKind]] = {
    "user": ("/admin user {identifier}", AdminCommandKind.QUERY_USER),
    "audit_identifier_only": ("/admin audit {identifier}", AdminCommandKind.QUERY_AUDIT),
    "audit_identifier_and_hours": (
        "/admin audit {identifier} 48",
        AdminCommandKind.QUERY_AUDIT,
    ),
    "suspend": ("/admin suspend {identifier}", AdminCommandKind.SUSPEND_USER),
    "resume": ("/admin resume {identifier}", AdminCommandKind.RESUME_USER),
    "grant_position": (
        "/admin grant_position {identifier} 数据分析师 * 职位授权",
        AdminCommandKind.GRANT_POSITION_PERMISSION,
    ),
    "grant_position_permission": (
        "/admin grant_position_permission {identifier} 数据分析师 * 职位授权",
        AdminCommandKind.GRANT_POSITION_PERMISSION,
    ),
    "revoke_permission_shape_two": (
        "/admin revoke_permission {identifier} 1011 daily_active 撤销 覆盖",
        AdminCommandKind.REVOKE_PERMISSION,
    ),
}


class LinkifiedIdentifierParsingTests(unittest.TestCase):
    """Issue #492：管理员 p2p 邮箱的受控 ``mailto`` 形态必须照常解析。

    真实缺陷：产品负责人 2026-08-31 在飞书里连发三条管理命令、连续三次收到"未识别
    的管理命令"。裁定原话：**不能因为带了 mailto 就未识别**。失败落点是
    ``_IDENTIFIER_PATTERN`` 的字符集不含 ``[]()<>``——链接化后的邮箱不含空白、仍是
    一个整 token，token 数对得上，一路走到字符集校验才被拒。修法是**在校验前归一化**，
    不是放宽字符集（字符集是本模块声明的第二道安全防线）。

    形态清单见 :data:`_LINKIFIED_EMAIL_FORMS` 的说明——它是公开邮箱的合成夹具，不是
    真实信封的逐字节回读；实现前不要求 raw fixture 或群聊 GET。
    """

    def test_every_email_taking_command_accepts_only_supported_email_forms(self) -> None:
        """完成标准 1＋2：受支持形态 × 全部吃邮箱的命令入口，逐格解析出裸邮箱。"""

        for command_name, (template, expected_kind) in _EMAIL_TAKING_COMMANDS.items():
            for form_name, token in _LINKIFIED_EMAIL_FORMS.items():
                with self.subTest(command=command_name, form=form_name):
                    command = parse_admin_command(template.format(identifier=token))
                    self.assertEqual(command.kind, expected_kind)
                    self.assertEqual(command.identifier, _PLAIN_EMAIL)
                    self.assertIsNone(command.reject_reason)

    def test_every_email_taking_command_rejects_unsupported_link_forms(self) -> None:
        """拒绝矩阵也覆盖全部实际入口，防止某个命令旁路邮箱边界。"""

        for command_name, (template, _) in _EMAIL_TAKING_COMMANDS.items():
            for form_name, token in _REJECTED_EMAIL_FORMS.items():
                with self.subTest(command=command_name, form=form_name):
                    command = parse_admin_command(template.format(identifier=token))
                    self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
                    self.assertEqual(command.reject_reason, AdminRejectReason.BAD_IDENTIFIER)

    def test_linkified_identifier_does_not_disturb_the_other_arguments(self) -> None:
        """归一化只作用在标识那一位：公司/指标/原因原样解析，不左移、不吞词。"""

        command = parse_admin_command(
            f"/admin revoke_permission [{_PLAIN_EMAIL}](mailto:{_PLAIN_EMAIL}) "
            "1011 新增用户数 三月特批 走完审批"
        )
        self.assertEqual(command.kind, AdminCommandKind.REVOKE_PERMISSION)
        self.assertEqual(command.identifier, _PLAIN_EMAIL)
        self.assertEqual(command.company_id, "1011")
        self.assertEqual(command.metric_name, "新增用户数")
        self.assertEqual(command.reason, "三月特批 走完审批")

    def test_linkified_open_id_is_rejected(self) -> None:
        """mailto 适配面只对邮箱开放，不能把链接化 open_id 当作邮箱参数放行。"""
        command = parse_admin_command("/admin user [ou_abc123](mailto:ou_abc123)")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
        self.assertEqual(command.reject_reason, AdminRejectReason.BAD_IDENTIFIER)

    def test_display_text_must_match_the_link_target(self) -> None:
        """显示邮箱和 mailto 目标不一致时拒绝，避免"看到 A、操作 B"的错位。"""
        command = parse_admin_command(
            "/admin user [seen@example.com](mailto:hidden@example.com)"
        )
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
        self.assertEqual(command.reject_reason, AdminRejectReason.BAD_IDENTIFIER)

    def test_empty_display_text_is_rejected(self) -> None:
        command = parse_admin_command("/admin user [](mailto:hidden@example.com)")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
        self.assertEqual(command.reject_reason, AdminRejectReason.BAD_IDENTIFIER)


class LinkNormalizationDoesNotWidenTheCharsetTests(unittest.TestCase):
    """否定断言（验证与门禁 §八）：归一化**只剥外壳，不放行内容**。

    ``_IDENTIFIER_PATTERN`` 是本模块声明的第二道独立防线（模块文档：即使未来出现
    绕过身份判定的缺陷，这个解析器也拼不出一条 SQL 或系统命令）。Issue #492 的修法
    刻意选择"在校验前归一化"而不是"放宽字符集"，这一组用例就是那条选择的保险：任何
    被字符集拒绝的**内容**，套上链接外壳之后照样被拒。
    """

    def test_sql_injection_shaped_payload_inside_a_markdown_link_is_still_unknown(self) -> None:
        command = parse_admin_command("/admin user [1;DROP--](mailto:x@example.com)")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
        self.assertEqual(command.reject_reason, AdminRejectReason.BAD_IDENTIFIER)

    def test_shell_metacharacter_payload_inside_inline_code_is_still_unknown(self) -> None:
        command = parse_admin_command("/admin user `$(rm)`")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
        self.assertEqual(command.reject_reason, AdminRejectReason.BAD_IDENTIFIER)

    def test_angle_wrapped_email_is_rejected(self) -> None:
        command = parse_admin_command(f"/admin user <{_PLAIN_EMAIL}>")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
        self.assertEqual(command.reject_reason, AdminRejectReason.BAD_IDENTIFIER)

    def test_inline_code_wrapped_email_is_rejected(self) -> None:
        command = parse_admin_command(f"/admin user `{_PLAIN_EMAIL}`")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
        self.assertEqual(command.reject_reason, AdminRejectReason.BAD_IDENTIFIER)

    def test_quote_payload_inside_an_angle_autolink_is_still_unknown(self) -> None:
        command = parse_admin_command("/admin suspend <ou_a'or'1'='1>")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
        self.assertEqual(command.reject_reason, AdminRejectReason.BAD_IDENTIFIER)

    def test_http_markdown_link_is_rejected(self) -> None:
        command = parse_admin_command(
            f"/admin user [{_PLAIN_EMAIL}](https://example.com/user)"
        )
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
        self.assertEqual(command.reject_reason, AdminRejectReason.BAD_IDENTIFIER)

    def test_arbitrary_markdown_link_is_rejected(self) -> None:
        command = parse_admin_command(f"/admin user [{_PLAIN_EMAIL}](example.com)")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
        self.assertEqual(command.reject_reason, AdminRejectReason.BAD_IDENTIFIER)

    def test_bare_mailto_open_id_is_rejected_instead_of_falling_back_to_identifier(self) -> None:
        command = parse_admin_command("/admin user mailto:ou_abc123")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
        self.assertEqual(command.reject_reason, AdminRejectReason.BAD_IDENTIFIER)

    def test_invalid_mailto_email_is_rejected(self) -> None:
        command = parse_admin_command("/admin user mailto:not-an-email")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
        self.assertEqual(command.reject_reason, AdminRejectReason.BAD_IDENTIFIER)

    def test_plain_http_uri_is_rejected_even_though_colon_is_in_the_legacy_charset(self) -> None:
        command = parse_admin_command(f"/admin user http:{_PLAIN_EMAIL}")
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
        self.assertEqual(command.reject_reason, AdminRejectReason.BAD_IDENTIFIER)

    def test_only_a_whole_token_is_unwrapped_not_a_link_shaped_substring(self) -> None:
        """剥壳锚定整个 token：``[a](b)`` 后面还挂着别的字符时不剥，照旧 UNKNOWN。"""

        command = parse_admin_command(
            f"/admin user [{_PLAIN_EMAIL}](mailto:{_PLAIN_EMAIL})尾巴"
        )
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
        self.assertEqual(command.reject_reason, AdminRejectReason.BAD_IDENTIFIER)

    def test_company_id_is_not_normalized(self) -> None:
        """归一化只装在标识那一位：公司标识不是邮箱形态、没有真实链接化路径，
        不给它开这个口子（多归一化一个位置就是多一份不必要的解析面）。"""

        command = parse_admin_command(
            "/admin revoke_permission ou_abc123 [1011](mailto:1011) daily_active 特批"
        )
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
        self.assertEqual(command.reject_reason, AdminRejectReason.BAD_COMPANY_ID)

    def test_plain_forms_are_untouched(self) -> None:
        """不误伤（完成标准 3）：没有链接外壳的输入逐条保持原有结果。"""

        plain = parse_admin_command(f"/admin user {_PLAIN_EMAIL}")
        self.assertEqual(plain.kind, AdminCommandKind.QUERY_USER)
        self.assertEqual(plain.identifier, _PLAIN_EMAIL)

        open_id = parse_admin_command("/admin user ou_abc123")
        self.assertEqual(open_id.kind, AdminCommandKind.QUERY_USER)
        self.assertEqual(open_id.identifier, "ou_abc123")

        # 含 `@` 但不是邮箱：`_IDENTIFIER_PATTERN` 本来就只判形状不判语义，
        # 归一化前后都原样放行到反查层（那里查无此人，是既有行为）。
        at_shaped = parse_admin_command("/admin user @@@")
        self.assertEqual(at_shaped.kind, AdminCommandKind.QUERY_USER)
        self.assertEqual(at_shaped.identifier, "@@@")


class RejectReasonSegmentationTests(unittest.TestCase):
    """Issue #492 完成标准 4：``UNKNOWN`` 要说清是**哪一段**没看懂。

    此前 ``parse_admin_command`` 只回一个不带原因的 ``UNKNOWN``，两类完全不同的
    错误（邮箱被链接化 / 公司那一段填了中文名）产生逐字相同的回复，管理员无从
    自救——这正是产品负责人连踩三次的体验。回复文案本身在
    ``core/admin/router._render_unknown``，这里只钉解析器回传的落点。
    """

    def test_chinese_company_name_is_attributed_to_the_company_segment(self) -> None:
        """Issue #492 假设 2：公司参数期望公司编号，输中文名**被拒是正确行为**
        （不放宽 ``_IDENTIFIER_PATTERN`` 去接受 CJK，那是语义变更）；缺陷只在于
        此前没说清楚。这条断言钉住"说清楚"这一半。"""

        command = parse_admin_command(
            "/admin revoke_permission ou_abc123 一零一一 daily_active 特批"
        )
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
        self.assertEqual(command.reject_reason, AdminRejectReason.BAD_COMPANY_ID)

    def test_each_segment_reports_its_own_reason(self) -> None:
        expectations: list[tuple[str, AdminRejectReason]] = [
            # 不是 /admin 开头：不是一次命令尝试，保持既有笼统文案（不误伤）。
            ("本月销售额是多少", AdminRejectReason.NOT_A_COMMAND),
            ("", AdminRejectReason.NOT_A_COMMAND),
            ("帮我查一下 /admin help 怎么用", AdminRejectReason.NOT_A_COMMAND),
            # 命令名那一段
            ("/admin", AdminRejectReason.UNKNOWN_SUBCOMMAND),
            ("/admin delete_user ou_1", AdminRejectReason.UNKNOWN_SUBCOMMAND),
            # 参数个数
            ("/admin user", AdminRejectReason.WRONG_ARGUMENT_COUNT),
            ("/admin user ou_a extra", AdminRejectReason.WRONG_ARGUMENT_COUNT),
            ("/admin help now", AdminRejectReason.WRONG_ARGUMENT_COUNT),
            ("/admin revoke_permission ou_a 1011 daily_active", AdminRejectReason.WRONG_ARGUMENT_COUNT),
            # 用户标识那一段
            ("/admin user ou_a;b", AdminRejectReason.BAD_IDENTIFIER),
            ("/admin suspend <a b>", AdminRejectReason.WRONG_ARGUMENT_COUNT),
            ("/admin audit ou_a;b", AdminRejectReason.BAD_IDENTIFIER),
            ("/admin audit ou_a;b 48", AdminRejectReason.BAD_IDENTIFIER),
            # 指标那一段
            ("/admin revoke_permission ou_a 1011 daily;active 特批", AdminRejectReason.BAD_METRIC_NAME),
            # 原因那一段
            ("/admin revoke_permission ou_a 1011 daily_active " + "长" * 501, AdminRejectReason.BAD_REASON),
            (f"/admin revoke_permission {_VALID_OVERRIDE_ID} " + "长" * 501, AdminRejectReason.BAD_REASON),
            # 小时数那一段
            ("/admin audit 0", AdminRejectReason.BAD_WINDOW_HOURS),
            ("/admin audit 721", AdminRejectReason.BAD_WINDOW_HOURS),
            ("/admin audit ou_a notanumber", AdminRejectReason.BAD_WINDOW_HOURS),
            ("/admin audit ou_a 0", AdminRejectReason.BAD_WINDOW_HOURS),
            # 追溯号那一段
            ("/admin trace not-a-ulid", AdminRejectReason.BAD_TRACE_ID),
        ]
        for text, expected in expectations:
            with self.subTest(text=text[:60]):
                command = parse_admin_command(text)
                self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
                self.assertEqual(command.reject_reason, expected)

    def test_non_string_input_is_not_a_command(self) -> None:
        command = parse_admin_command(None)  # type: ignore[arg-type]
        self.assertEqual(command.reject_reason, AdminRejectReason.NOT_A_COMMAND)

    def test_successful_commands_carry_no_reject_reason(self) -> None:
        for text in (
            "/admin help",
            f"/admin user {_PLAIN_EMAIL}",
            "/admin audit",
            "/admin revoke_permission ou_a 1011 daily_active 特批",
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse_admin_command(text).reject_reason)


#: Trace #521 W0-1 的**多 token** 形态矩阵：显示文本与链接目标之间出现了空白，
#: ``str.split()`` 因此把一个邮箱拆成两段以上。这一类是 2026-09-01 那次真实失败
#: （``/admin audit <邮箱> 24`` → ``wrong_argument_count``）唯一可能的结构：
#: ``_parse_audit`` 只在 ``len(rest) >= 3`` 时返回该原因，而三种已适配的单 token
#: 形态实测全部解析成功、五种被拒的单 token 形态实测全部落 ``bad_identifier``。
#:
#: 与 :data:`_LINKIFIED_EMAIL_FORMS` 同一纪律：**公开邮箱的合成夹具，不是真实
#: 信封的逐字节回读**。哪一种真的发生过要靠 ``admin.command.unknown`` 新增的
#: ``token_shapes`` 取证字段在下一次复现时指认，不靠推断。
_MULTI_TOKEN_EMAIL_FORMS: dict[str, str] = {
    "html_anchor": f'<a href="mailto:{_PLAIN_EMAIL}">{_PLAIN_EMAIL}</a>',
    "html_anchor_single_quote": f"<a href='mailto:{_PLAIN_EMAIL}'>{_PLAIN_EMAIL}</a>",
    "html_anchor_extra_attribute": (
        f'<a href="mailto:{_PLAIN_EMAIL}" target="_blank">{_PLAIN_EMAIL}</a>'
    ),
    "display_then_paren_mailto": f"{_PLAIN_EMAIL} (mailto:{_PLAIN_EMAIL})",
    "display_then_paren_bare": f"{_PLAIN_EMAIL} ({_PLAIN_EMAIL})",
    "display_then_bracket_mailto": f"{_PLAIN_EMAIL} [mailto:{_PLAIN_EMAIL}]",
    "display_then_angle_mailto": f"{_PLAIN_EMAIL} <mailto:{_PLAIN_EMAIL}>",
    "display_then_bare_mailto": f"{_PLAIN_EMAIL} mailto:{_PLAIN_EMAIL}",
    "markdown_link_with_space": f"[{_PLAIN_EMAIL}] (mailto:{_PLAIN_EMAIL})",
    "mailto_display_and_target": f"mailto:{_PLAIN_EMAIL} (mailto:{_PLAIN_EMAIL})",
}

#: 同样是多 token，但**显示文本与链接目标不是同一个邮箱**——一律不合并（fail
#: closed）。这条边界是整个归一化能否成立的前提：合并显示≠目标的一对，等于把
#: "管理员看到 A、系统操作 B" 变成一次成功解析。
_REJECTED_MULTI_TOKEN_EMAIL_FORMS: dict[str, str] = {
    "anchor_display_is_a_name": f'<a href="mailto:{_PLAIN_EMAIL}">某人</a>',
    "anchor_target_is_http": f'<a href="https://example.com/u">{_PLAIN_EMAIL}</a>',
    "display_and_target_differ": f"seen@example.com (mailto:{_PLAIN_EMAIL})",
    "target_is_http_link": f"{_PLAIN_EMAIL} (https://example.com/{_PLAIN_EMAIL})",
    "target_is_open_id": f"{_PLAIN_EMAIL} (mailto:ou_abc123)",
    "display_is_open_id": f"ou_abc123 (mailto:ou_abc123)",
    "target_is_not_an_email": f"{_PLAIN_EMAIL} (mailto:not-an-email)",
}


class MultiTokenLinkifiedIdentifierTests(unittest.TestCase):
    """Trace #521 W0-1/F4-2：显示文本与链接目标被拆成多段时也要认得出来。

    #492 的 S-4 只处理了**单 token** 的 ``mailto`` 形态，结构上覆盖不到"一个邮箱
    占了两段"的输入；2026-09-01 那次真实失败落的正是 ``wrong_argument_count``，
    与 S-4 适配的三种形态无关（那三种实测全部解析成功）。
    """

    def test_every_multi_token_form_parses_on_every_email_taking_command(self) -> None:
        for form_name, identifier in _MULTI_TOKEN_EMAIL_FORMS.items():
            for command_name, (template, kind) in _EMAIL_TAKING_COMMANDS.items():
                with self.subTest(form=form_name, command=command_name):
                    command = parse_admin_command(template.format(identifier=identifier))
                    self.assertEqual(command.kind, kind)
                    self.assertEqual(command.identifier, _PLAIN_EMAIL)
                    self.assertIsNone(command.reject_reason)

    def test_the_observed_failure_shape_now_resolves_to_the_same_query(self) -> None:
        """真实失败的等价复现：``/admin audit <锚点形态邮箱> 24``。

        修复前这条输入被切成三段参数 → ``wrong_argument_count``；修复后与管理员
        原样键入裸邮箱**逐字段相同**。
        """

        anchor = f'<a href="mailto:{_PLAIN_EMAIL}">{_PLAIN_EMAIL}</a>'

        command = parse_admin_command(f"/admin audit {anchor} 24")

        self.assertEqual(command, parse_admin_command(f"/admin audit {_PLAIN_EMAIL} 24"))
        self.assertEqual(command.kind, AdminCommandKind.QUERY_AUDIT)
        self.assertEqual(command.identifier, _PLAIN_EMAIL)
        self.assertEqual(command.window_hours, 24)

    def test_display_and_target_must_be_the_same_email(self) -> None:
        """fail closed：显示≠目标一律不合并，也就不可能被当成目标标识执行。"""

        for form_name, identifier in _REJECTED_MULTI_TOKEN_EMAIL_FORMS.items():
            with self.subTest(form=form_name):
                command = parse_admin_command(f"/admin user {identifier}")
                self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
                self.assertIsNone(command.identifier)

    def test_a_mismatched_pair_never_leaks_either_side_as_the_identifier(self) -> None:
        """否定断言：既不能拿显示文本当标识，也不能拿链接目标当标识。"""

        command = parse_admin_command(
            f"/admin user seen@example.com (mailto:{_PLAIN_EMAIL})"
        )

        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
        self.assertIsNone(command.identifier)


class CollapseDoesNotChangeAnythingThatAlreadyParsedTests(unittest.TestCase):
    """不误伤（F4-4）：归一化只在**原样解析失败**时才启用。

    因此"原本能解析成功的输入"这一整类的行为逐字节不变——这不是靠逐条枚举证明的
    偶然性质，而是 :func:`parse_admin_command` 的结构性质，本组用例把它钉死。
    """

    #: 一组原本就能解析成功的输入，覆盖每一种命令形状。
    _ALREADY_PARSING = (
        "/admin help",
        f"/admin user {_PLAIN_EMAIL}",
        "/admin user ou_abc123",
        f"/admin audit {_PLAIN_EMAIL} 24",
        "/admin audit 24",
        "/admin audit",
        f"/admin suspend {_PLAIN_EMAIL}",
        f"/admin resume {_PLAIN_EMAIL}",
        f"/admin revoke_permission {_PLAIN_EMAIL} 1011 daily_active 撤销 覆盖",
        f"/admin grant_position {_PLAIN_EMAIL} 数据分析师 * 职位授权",
        f"/admin revoke_permission {_PLAIN_EMAIL} 1011 daily_active 撤销 覆盖",
    )

    def test_successful_parses_are_unchanged_by_the_new_normalization(self) -> None:
        for text in self._ALREADY_PARSING:
            with self.subTest(text=text):
                command = parse_admin_command(text)
                self.assertNotEqual(command.kind, AdminCommandKind.UNKNOWN)
                # 归一化对这些输入必须是恒等变换（它们压根不会进入归一化）。
                self.assertEqual(_collapse_identifier_link_forms(text), text)

    def test_plain_chat_is_never_normalized_and_keeps_the_generic_failure(self) -> None:
        """闲聊连 ``/admin`` 前缀都没有：第一步就返回 ``NOT_A_COMMAND``。"""

        for text in (
            "不知道说什么",
            f"帮我看看 {_PLAIN_EMAIL} (mailto:{_PLAIN_EMAIL}) 的权限",
            "今天的数据 <a href=\"mailto:a@b.com\">a@b.com</a> 有问题吗",
        ):
            with self.subTest(text=text):
                command = parse_admin_command(text)
                self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
                self.assertEqual(command.reject_reason, AdminRejectReason.NOT_A_COMMAND)

    def test_non_email_at_shaped_input_is_never_rewritten(self) -> None:
        """含非邮箱 ``@`` 的输入（飞书 at 标记、``@全体``、``@1``）行为不变。

        归一化只认"两侧都是同一个邮箱"的一对，因此这些输入连改写都不会发生——
        断言写成"归一化是恒等变换 + 结论与既有逐字相同"，比只断言某个失败枚举更
        贴近"不误伤"这条承诺本身。``@1`` 一直是合法标识形状（``_IDENTIFIER_
        PATTERN`` 允许 ``@``），它本来就解析成功，这里同样必须保持成功。
        """

        for text, expected_kind, expected_reason in (
            (
                '/admin user <at user_id="all"></at>',
                AdminCommandKind.UNKNOWN,
                AdminRejectReason.WRONG_ARGUMENT_COUNT,
            ),
            ("/admin user @所有人", AdminCommandKind.UNKNOWN, AdminRejectReason.BAD_IDENTIFIER),
            ("/admin audit @1 24", AdminCommandKind.QUERY_AUDIT, None),
        ):
            with self.subTest(text=text):
                self.assertEqual(_collapse_identifier_link_forms(text), text)
                command = parse_admin_command(text)
                self.assertEqual(command.kind, expected_kind)
                self.assertEqual(command.reject_reason, expected_reason)

    def test_a_genuinely_unknown_command_keeps_its_original_failure_reason(self) -> None:
        """归一化救不回来时返回**原样解析**的失败原因，不换一个更迷惑的落点。"""

        command = parse_admin_command(
            f"/admin delete_user {_PLAIN_EMAIL} (mailto:{_PLAIN_EMAIL})"
        )

        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
        self.assertEqual(command.reject_reason, AdminRejectReason.UNKNOWN_SUBCOMMAND)

    def test_too_many_real_arguments_still_reports_wrong_argument_count(self) -> None:
        """管理员真的多打了一个参数时，仍然是"参数个数不对"，不被归一化掩盖。"""

        command = parse_admin_command(f"/admin audit {_PLAIN_EMAIL} 24 48")

        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
        self.assertEqual(command.reject_reason, AdminRejectReason.WRONG_ARGUMENT_COUNT)


class AdminTokenShapeForensicsTests(unittest.TestCase):
    """Trace #521 F4-1：``admin.command.unknown`` 的形状取证字段。

    #492 的调查卡在"一条 ``wrong_argument_count`` 只留下一个枚举名"——"客户端把
    邮箱拆成两段"和"管理员真的多打一个参数"产生逐字相同的审计。形状画像让下一次
    复现可以被**指认**，不必再靠推理排除。
    """

    def test_shapes_describe_the_anchor_form_without_any_input_text(self) -> None:
        anchor = f'<a href="mailto:{_PLAIN_EMAIL}">{_PLAIN_EMAIL}</a>'

        described = describe_admin_tokens(f"/admin audit {anchor} 24")

        self.assertTrue(described.is_admin_prefixed)
        self.assertEqual(described.argument_count, 3)
        self.assertEqual(
            described.shapes,
            ("admin_prefix", "bare_word", "html_anchor_open", "html_href_attribute", "digits"),
        )
        # 否定断言：形状串里没有任何一段输入原文。
        self.assertNotIn(_PLAIN_EMAIL, described.shape_summary)
        self.assertNotIn("example.com", described.shape_summary)
        self.assertNotIn("mailto", described.shape_summary)

    def test_shapes_separate_the_two_competing_hypotheses(self) -> None:
        """同一条 ``wrong_argument_count``，两种成因的形状串必须不同。"""

        split_by_client = describe_admin_tokens(
            f"/admin audit {_PLAIN_EMAIL} (mailto:{_PLAIN_EMAIL}) 24"
        )
        typed_by_admin = describe_admin_tokens(f"/admin audit {_PLAIN_EMAIL} 24 48")

        self.assertEqual(split_by_client.argument_count, typed_by_admin.argument_count)
        self.assertNotEqual(split_by_client.shapes, typed_by_admin.shapes)
        self.assertEqual(
            split_by_client.shapes,
            ("admin_prefix", "bare_word", "email", "paren_wrapped", "digits"),
        )
        self.assertEqual(
            typed_by_admin.shapes,
            ("admin_prefix", "bare_word", "email", "digits", "digits"),
        )

    def test_non_admin_text_is_flagged_so_no_forensics_are_recorded(self) -> None:
        described = describe_admin_tokens("今天的日报呢")

        self.assertFalse(described.is_admin_prefixed)

    def test_non_string_input_is_handled_without_raising(self) -> None:
        described = describe_admin_tokens(None)

        self.assertFalse(described.is_admin_prefixed)
        self.assertEqual(described.shapes, ())
        self.assertEqual(described.argument_count, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class LinkCollapseCostTests(unittest.TestCase):
    """A-1（Trace #544，对抗审查面 2）：**已登记管理员可以自己触发的二次方级 ReDoS**。

    ``/admin user `` + 一个超长 token（连链接语法都不需要）此前会让链接形态合并阶段的
    四条 pattern 各自 O(n²)：实测 n=2000 → 340 ms、n=4000 → 1.35 s、n=20000 → 25 s 以上。
    这段代码跑在 gateway 的**单线程事件循环**上，一条消息就能让全体用户的消息排队。

    验收线取 n=20000 ≤ 1.5 s（Trace #544 验收）。这里不断言绝对毫秒数以外的东西——
    机器负载会影响绝对值，1.5 s 相对修复前的 25 s 有一个数量级以上的余量，不会因为
    机器慢一点就假红。
    """

    BUDGET_SECONDS = 1.5

    def _elapsed(self, text: str):
        started = time.perf_counter()
        command = parse_admin_command(text)
        return time.perf_counter() - started, command

    def test_very_long_token_returns_bad_identifier_within_budget(self) -> None:
        elapsed, command = self._elapsed("/admin user " + "a" * 20000 + "@example.com")

        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
        self.assertEqual(command.reject_reason, AdminRejectReason.BAD_IDENTIFIER)
        self.assertLess(elapsed, self.BUDGET_SECONDS)

    def test_long_token_carrying_each_link_marker_stays_within_budget(self) -> None:
        """加上每一种链接标记都不能把成本拉回二次方——必要标记预检只挡掉"一个标记都
        没有"的输入，真正让成本封顶的是显示/目标段的确定上限。"""

        for name, text in (
            ("paren", "/admin user " + "a" * 20000 + "(x)"),
            ("bracket", "/admin user [" + "a" * 20000 + "]"),
            ("angle", "/admin user <" + "a" * 20000 + ">"),
            ("mailto", "/admin user mailto:" + "a" * 20000),
            ("anchor", "/admin user <a href=\"mailto:" + "a" * 20000 + "\">x</a>"),
        ):
            with self.subTest(marker=name):
                elapsed, command = self._elapsed(text)
                self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
                self.assertLess(elapsed, self.BUDGET_SECONDS)

    def test_legitimate_link_forms_still_collapse(self) -> None:
        """不误伤：Issue #492 那批受控链接形态照常合并——限长取 160，而所有标识
        token 的形状上限本来就是 128。"""

        for text in (
            "/admin user [a@b.com](mailto:a@b.com)",
            "/admin user a@b.com (mailto:a@b.com)",
            "/admin user a@b.com mailto:a@b.com",
            "/admin user <a href=\"mailto:a@b.com\">a@b.com</a>",
        ):
            with self.subTest(text=text):
                command = parse_admin_command(text)
                self.assertEqual(command.kind, AdminCommandKind.QUERY_USER)
                self.assertEqual(command.identifier, "a@b.com")

    def test_a_segment_longer_than_the_cap_is_simply_not_collapsed(self) -> None:
        """限长是**无损**的：超过上限的段无论合不合并都过不了标识形状校验，
        结论仍然是同一个 ``bad_identifier``，不是把一个本来能用的输入判死。"""

        long_local_part = "a" * 200
        command = parse_admin_command(
            f"/admin user [{long_local_part}@b.com](mailto:{long_local_part}@b.com)"
        )
        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
        self.assertEqual(command.reject_reason, AdminRejectReason.BAD_IDENTIFIER)


class NonDecimalDigitTokenTests(unittest.TestCase):
    """A-2（Trace #544）：``str.isdigit()`` 对上标数字为真、``int()`` 却抛
    ``ValueError``——``/admin audit ²⁴`` 因此不是落到"小时数不对"这条明确拒绝上，
    而是把整条命令打挂成一句笼统的"本次管理命令处理失败"（``router.py`` 的兜底）。
    """

    SUPERSCRIPT_24 = "²⁴"

    def test_superscript_hours_does_not_raise(self) -> None:
        command = parse_admin_command(f"/admin audit a@b.com {self.SUPERSCRIPT_24}")

        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
        self.assertEqual(command.reject_reason, AdminRejectReason.BAD_WINDOW_HOURS)

    def test_superscript_single_token_falls_back_to_identifier_shape(self) -> None:
        command = parse_admin_command(f"/admin audit {self.SUPERSCRIPT_24}")

        self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
        self.assertEqual(command.reject_reason, AdminRejectReason.BAD_IDENTIFIER)

    def test_other_non_decimal_digit_shapes_are_also_safe(self) -> None:
        for token in ("₁₂", "⑤", "½"):
            with self.subTest(token=token):
                command = parse_admin_command(f"/admin audit a@b.com {token}")
                self.assertEqual(command.kind, AdminCommandKind.UNKNOWN)
                self.assertEqual(command.reject_reason, AdminRejectReason.BAD_WINDOW_HOURS)

    def test_plain_decimal_hours_still_parse(self) -> None:
        command = parse_admin_command("/admin audit a@b.com 48")

        self.assertEqual(command.kind, AdminCommandKind.QUERY_AUDIT)
        self.assertEqual(command.window_hours, 48)
