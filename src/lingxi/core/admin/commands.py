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

_COMMAND_PREFIX = "/admin"


class AdminCommandKind(str, Enum):
    HELP = "help"
    QUERY_USER = "query_user"
    QUERY_AUDIT = "query_audit"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AdminCommand:
    """解析结果。``UNKNOWN`` 之外的取值只填各自需要的字段，其余保持默认。"""

    kind: AdminCommandKind
    identifier: str | None = None
    window_hours: int | None = None


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

    return _unknown()


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
