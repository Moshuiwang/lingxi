"""管理命令面的文本解析：一个封闭的、确定性的语法，不是自由文本解释器。

产品合同「管理 MCP 只提供范围明确的管理能力，不提供任意数据库查询……或绕过既有权限
规则的通用工具」在这里的落点是**语法本身封闭**：识别不出来的输入一律落进
``UNKNOWN``，永远不会被当成可执行的查询条件拼进任何语句——真正的越权防线不在这个
解析器（它甚至不知道调用者是谁），而在 ``router.py`` 先判定身份、角色，才谈得上解析
命令；但语法封闭是第二道独立防线：即使未来出现绕过身份判定的缺陷，这个解析器本身
也拼不出一条 SQL 或系统命令，能返回的只有本模块声明的四种命令之一。

只做「这段文本是哪个命令」的判定，不做查询、不做权限判断——那两件事分别属于
``router.py`` 的注入端口与 ``AdminCommandRouter``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

#: 目标标识（当前只用于 open_id）允许的形状：字母、数字、下划线、连字符、点、冒号，
#: 1–128 字符。刻意排除空白、引号、分号等 SQL / shell 元字符——不是因为下游会拼接
#: 字符串（真实查询走参数化语句），而是让"格式一望而知安全"成为语法层面的性质，
#: 不依赖调用方记得转义。
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

#: 追溯/审计查询默认时间窗（小时）与允许上限（30 天）。上限防止一次查询扫描
#: 出远超"最近关键事件"这个 MVP 承诺的历史范围。
DEFAULT_AUDIT_WINDOW_HOURS = 24
MAX_AUDIT_WINDOW_HOURS = 720

#: 本地权限授权/抑制命令的原因文本上限（#319 S-P-1b 设计卡）：自由文本，非空白、
#: ≤500 字符——足够写清楚一次特批的来龙去脉，同时防止一次输入把审计字段撑成
#: 不可读的长文。
_PERMISSION_REASON_MAX_LENGTH = 500

_COMMAND_PREFIX = "/admin"


class AdminCommandKind(str, Enum):
    HELP = "help"
    QUERY_USER = "query_user"
    QUERY_AUDIT = "query_audit"
    SUSPEND_USER = "suspend_user"
    RESUME_USER = "resume_user"
    GRANT_PERMISSION = "grant_permission"
    SUPPRESS_PERMISSION = "suppress_permission"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AdminCommand:
    """解析结果。``UNKNOWN`` 之外的取值只填各自需要的字段，其余保持默认。

    ``company_id``/``metric_name``/``reason`` 三个字段只有
    ``GRANT_PERMISSION``/``SUPPRESS_PERMISSION`` 会填（``identifier`` 复用做
    目标用户标识，与既有 ``suspend``/``resume`` 同一惯例）。
    """

    kind: AdminCommandKind
    identifier: str | None = None
    window_hours: int | None = None
    company_id: str | None = None
    metric_name: str | None = None
    reason: str | None = None


def _unknown() -> AdminCommand:
    return AdminCommand(kind=AdminCommandKind.UNKNOWN)


def parse_admin_command(text: object) -> AdminCommand:
    """把一条私聊文本解析成三条已知命令之一，或 ``UNKNOWN``。

    语法（大小写不敏感，仅识别整条消息按空白切分后的形状，不在文本中间查找）：

    - ``/admin help``                          → HELP
    - ``/admin user <identifier>``              → QUERY_USER
    - ``/admin audit``                          → QUERY_AUDIT，无过滤、默认时间窗
    - ``/admin audit <identifier>``              → QUERY_AUDIT，按标识过滤、默认时间窗
    - ``/admin audit <identifier> <hours>``      → QUERY_AUDIT，按标识过滤、显式时间窗
    - ``/admin audit <hours>``                   → QUERY_AUDIT，无过滤、显式时间窗
      （单个额外参数全为数字时按小时数解释，否则按标识解释——两者不可能同时成立，
      判据因此是确定性的，不依赖顺序猜测）
    - ``/admin suspend <identifier>``            → SUSPEND_USER（Issue #96 S-M-02：
      只建待确认操作，不直接执行；执行前须经本人飞书确认卡片）
    - ``/admin resume <identifier>``             → RESUME_USER（同上，对称动作）
    - ``/admin grant_permission <identifier> <company_id> <metric_name> <reason...>``
      → GRANT_PERMISSION（#319 S-P-1b：同样只建待确认操作，不直接执行；
      ``company_id``/``metric_name`` 与 ``<identifier>`` 同一形状约束，``reason``
      是尾部剩余全部 token 拼接成的自由文本，非空白、≤500 字符）
    - ``/admin suppress_permission <identifier> <company_id> <metric_name> <reason...>``
      → SUPPRESS_PERMISSION（同上，对称动作）

    任何不匹配以上形状的输入（含空文本、非字符串、未知子命令、参数数量或形状不对、
    小时数越界）一律返回 ``UNKNOWN``——调用方据此回复帮助/拒绝文案，不猜测意图。
    """

    if not isinstance(text, str):
        return _unknown()
    tokens = text.strip().split()
    if len(tokens) < 2 or tokens[0].lower() != _COMMAND_PREFIX:
        return _unknown()

    sub = tokens[1].lower()
    rest = tokens[2:]

    if sub == "help":
        if rest:
            return _unknown()
        return AdminCommand(kind=AdminCommandKind.HELP)

    if sub == "user":
        if len(rest) != 1 or not _IDENTIFIER_PATTERN.fullmatch(rest[0]):
            return _unknown()
        return AdminCommand(kind=AdminCommandKind.QUERY_USER, identifier=rest[0])

    if sub == "audit":
        return _parse_audit(rest)

    if sub == "suspend":
        if len(rest) != 1 or not _IDENTIFIER_PATTERN.fullmatch(rest[0]):
            return _unknown()
        return AdminCommand(kind=AdminCommandKind.SUSPEND_USER, identifier=rest[0])

    if sub == "resume":
        if len(rest) != 1 or not _IDENTIFIER_PATTERN.fullmatch(rest[0]):
            return _unknown()
        return AdminCommand(kind=AdminCommandKind.RESUME_USER, identifier=rest[0])

    if sub == "grant_permission":
        return _parse_permission_command(rest, kind=AdminCommandKind.GRANT_PERMISSION)

    if sub == "suppress_permission":
        return _parse_permission_command(rest, kind=AdminCommandKind.SUPPRESS_PERMISSION)

    return _unknown()


def _parse_permission_command(rest: list[str], *, kind: AdminCommandKind) -> AdminCommand:
    """``grant_permission``/``suppress_permission`` 共用的解析：
    ``<identifier> <company_id> <metric_name> <reason...>``——前三个 token 与既有
    ``user``/``suspend``/``resume`` 同一形状约束（``_IDENTIFIER_PATTERN``），
    第四个及以后全部 token 按空白拼接还原成一段自由文本 ``reason``。

    至少需要 4 个 token（标识 + 公司 + 指标 + 至少一个原因词）；拼接后的 ``reason``
    去除首尾空白后为空，或超过 :data:`_PERMISSION_REASON_MAX_LENGTH` 字符，均视为
    形状不对，返回 ``UNKNOWN``——与本模块"识别不出来的输入一律 UNKNOWN"的既有纪律
    一致，不对越界输入做截断或静默修正。
    """

    if len(rest) < 4:
        return _unknown()
    identifier, company_id, metric_name, *reason_tokens = rest
    if not _IDENTIFIER_PATTERN.fullmatch(identifier):
        return _unknown()
    if not _IDENTIFIER_PATTERN.fullmatch(company_id):
        return _unknown()
    if not _IDENTIFIER_PATTERN.fullmatch(metric_name):
        return _unknown()
    reason = " ".join(reason_tokens).strip()
    if not reason or len(reason) > _PERMISSION_REASON_MAX_LENGTH:
        return _unknown()
    return AdminCommand(
        kind=kind,
        identifier=identifier,
        company_id=company_id,
        metric_name=metric_name,
        reason=reason,
    )


def _parse_audit(rest: list[str]) -> AdminCommand:
    if not rest:
        return AdminCommand(
            kind=AdminCommandKind.QUERY_AUDIT, window_hours=DEFAULT_AUDIT_WINDOW_HOURS
        )
    if len(rest) == 1:
        token = rest[0]
        if token.isdigit():
            hours = _validated_hours(token)
            if hours is None:
                return _unknown()
            return AdminCommand(kind=AdminCommandKind.QUERY_AUDIT, window_hours=hours)
        if not _IDENTIFIER_PATTERN.fullmatch(token):
            return _unknown()
        return AdminCommand(
            kind=AdminCommandKind.QUERY_AUDIT,
            identifier=token,
            window_hours=DEFAULT_AUDIT_WINDOW_HOURS,
        )
    if len(rest) == 2:
        identifier, hours_token = rest
        if not _IDENTIFIER_PATTERN.fullmatch(identifier) or not hours_token.isdigit():
            return _unknown()
        hours = _validated_hours(hours_token)
        if hours is None:
            return _unknown()
        return AdminCommand(
            kind=AdminCommandKind.QUERY_AUDIT, identifier=identifier, window_hours=hours
        )
    return _unknown()


def _validated_hours(token: str) -> int | None:
    value = int(token)
    if value < 1 or value > MAX_AUDIT_WINDOW_HOURS:
        return None
    return value
