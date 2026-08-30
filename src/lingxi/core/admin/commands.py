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

from lingxi.core.ids import is_ulid

#: 目标标识（open_id 或邮箱，#439 A 档新增邮箱支持）允许的形状：字母、数字、下划线、
#: 连字符、点、冒号、``@``，1–128 字符。刻意排除空白、引号、分号等 SQL / shell 元
#: 字符——不是因为下游会拼接字符串（真实查询走参数化语句），而是让"格式一望而知
#: 安全"成为语法层面的性质，不依赖调用方记得转义。新增 ``@`` 只是为了让邮箱形态的
#: 标识能通过这一层语法门（如 ``name@company.com``），是否真的按邮箱解析、反查
#: 到哪个 open_id 是 ``router.py``/``adapters/admin_registry.py`` 的职责，本模块
#: 继续保持"只判定形状是否安全，不关心语义"的既有分工。
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_.@:-]{1,128}$")

#: 指标名 token 允许的形状（#439 A 档新增中文别名支持）：在 ``_IDENTIFIER_PATTERN``
#: 的基础上额外放行 CJK 统一表意文字区（``一-鿿``）——真实指标目录
#: （``config/company_function_metric_map.toml``）当前全部是英文 snake_case 内部
#: ID（如 ``sub_new_count``），管理员记不住这些内部 ID 是 #439 的真实动机；允许中文
#: token 通过语法门后，才谈得上在 ``router.py``/``adapters/admin_registry.py`` 里
#: 按别名表反查成真正的指标 ID——语法层继续不关心语义，只放宽到"安全字符集"，不
#: 单独为 identifier/company_id 放开同一个口子（那两类 token 目前没有中文形态的
#: 真实需求，维持既有更窄的字符集）。
_METRIC_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_.@:一-鿿-]{1,128}$")

#: 追溯/审计查询默认时间窗（小时）与允许上限（30 天）。上限防止一次查询扫描
#: 出远超"最近关键事件"这个 MVP 承诺的历史范围。
DEFAULT_AUDIT_WINDOW_HOURS = 24
MAX_AUDIT_WINDOW_HOURS = 720

#: 本地权限授权/抑制/收回命令的原因文本上限（授权/抑制：#319 S-P-1b 设计卡；
#: 收回：卡 B 沿用同一上限）：自由文本，非空白、≤500 字符——足够写清楚一次特批
#: 或收回的来龙去脉，同时防止一次输入把审计字段撑成不可读的长文。
_PERMISSION_REASON_MAX_LENGTH = 500

#: ``revoke_permission`` 的目标标识形状：本地权限覆盖行的内部主键前缀
#: （``adapters/postgres_local_permission.py`` 用 ``new_id("lpo")`` 生成），不是
#: open_id——收回命令按行本身定位，不是按用户+公司+指标定位（卡 B 设计卡）。
_OVERRIDE_ID_PREFIX = "lpo_"

_COMMAND_PREFIX = "/admin"


class AdminCommandKind(str, Enum):
    HELP = "help"
    QUERY_USER = "query_user"
    QUERY_AUDIT = "query_audit"
    QUERY_TRACE = "query_trace"
    SUSPEND_USER = "suspend_user"
    RESUME_USER = "resume_user"
    GRANT_PERMISSION = "grant_permission"
    SUPPRESS_PERMISSION = "suppress_permission"
    REVOKE_PERMISSION = "revoke_permission"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AdminCommand:
    """解析结果。``UNKNOWN`` 之外的取值只填各自需要的字段，其余保持默认。

    ``company_id``/``metric_name``/``reason`` 三个字段由 ``GRANT_PERMISSION``/
    ``SUPPRESS_PERMISSION`` 填（``identifier`` 复用做目标用户标识，与既有
    ``suspend``/``resume`` 同一惯例）。``REVOKE_PERMISSION`` 有两种形状（#439 A
    档新增第二种，见 ``_parse_revoke_permission_command`` 文档）：

    - 形状 1（旧，按行定位）：只填 ``identifier``（复用同一字段承载 override_id，
      不是 open_id）与 ``reason``，``company_id``/``metric_name`` 保持默认
      ``None``——收回按行本身定位，这两项会在 ``adapters/postgres_pending_
      action.py`` 的 ``prepare()`` 里从这一行本身查出来；
    - 形状 2（新，与 grant/suppress 同一参数形状）：``identifier`` 是目标用户标识
      （open_id 或邮箱，不是 override_id）、``company_id``/``metric_name``/
      ``reason`` 均非空——``router.py`` 据此在调用 ``prepare()`` 之前先反查出
      override_id（``AdminQueries.resolve_override_id``），再退化成与形状 1 完全
      相同的下游调用。两种形状对下游（``pending_action.py``/``router.py`` 之外
      的代码）保持逐字节相同的调用面，`command.company_id is not None` 是唯一
      的判据。
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
    - ``/admin trace <追溯号>``                   → QUERY_TRACE（Issue #337：按追溯号
      查开通失败原因 + 入站事件时间线 + 开通状态，脱敏输出。``<追溯号>`` 必须是裸
      ULID——``core/ids.is_ulid`` 同一形状校验，不加前缀，与 ``inbound_event.
      trace_id``/``onboarding_failure.trace_id`` 的存储形状一致；不合形状一律
      ``UNKNOWN``，不当成任意标识去查库）
    - ``/admin suspend <identifier>``            → SUSPEND_USER（Issue #96 S-M-02：
      只建待确认操作，不直接执行；执行前须经本人飞书确认卡片）
    - ``/admin resume <identifier>``             → RESUME_USER（同上，对称动作）
    - ``/admin grant_permission <identifier> <company_id> <metric_name> <reason...>``
      → GRANT_PERMISSION（#319 S-P-1b：同样只建待确认操作，不直接执行；
      ``company_id``/``metric_name`` 与 ``<identifier>`` 同一形状约束，``reason``
      是尾部剩余全部 token 拼接成的自由文本，非空白、≤500 字符）
    - ``/admin suppress_permission <identifier> <company_id> <metric_name> <reason...>``
      → SUPPRESS_PERMISSION（同上，对称动作）
    - ``/admin revoke_permission <override_id> <reason...>``
      → REVOKE_PERMISSION（卡 B：``override_id`` 是本地权限覆盖行的内部标识
      ``lpo_*``——26 位 Crockford Base32 ULID 前缀，与 ``core/ids.is_ulid`` 同一
      形状校验；``reason`` 是尾部剩余全部 token 拼接成的自由文本，非空白、
      ≤500 字符，与 grant/suppress 同一纪律）
    - ``/admin revoke_permission <identifier> <company_id> <metric_name> <reason...>``
      → REVOKE_PERMISSION（#439 A 档新增：与 grant/suppress **同一参数形状**，
      不再要求管理员先查出 ``lpo_`` 内部 ID；``identifier``/``company_id``/
      ``metric_name`` 三段服务端反查覆盖 ID，见 ``router.py``。两种 revoke 形状
      按第一个 token 是否形似 override_id 分辨，见 ``_parse_revoke_permission_
      command`` 文档）

    任何不匹配以上形状的输入（含空文本、非字符串、未知子命令、参数数量或形状不对、
    小时数越界）一律返回 ``UNKNOWN``——调用方据此回复帮助/拒绝文案，不猜测意图。

    **标识参数支持邮箱（#439 A 档）**：``user``/``audit``/``suspend``/``resume``/
    ``grant_permission``/``suppress_permission`` 与 revoke 新形状里标记目标用户的
    ``<identifier>``，既可以是 open_id 也可以是邮箱——本函数只按 ``_IDENTIFIER_
    PATTERN`` 判定"形状是否安全"，不区分两者；把邮箱反查成 open_id 是
    ``router.py``/``adapters/admin_registry.py`` 的职责（``AdminQueries.
    resolve_identifier``），本模块继续不做任何查询。
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

    if sub == "trace":
        if len(rest) != 1 or not is_ulid(rest[0]):
            return _unknown()
        return AdminCommand(kind=AdminCommandKind.QUERY_TRACE, identifier=rest[0])

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

    if sub == "revoke_permission":
        return _parse_revoke_permission_command(rest)

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
    if not _METRIC_TOKEN_PATTERN.fullmatch(metric_name):
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


def _is_override_id(token: str) -> bool:
    """``lpo_`` 前缀 + 26 位 Crockford Base32 ULID——与 ``core/ids.new_id("lpo")``
    的生成形状逐字对应，复用 ``core/ids.is_ulid`` 而不是自己重写一份大小写/
    字母表校验（全仓库唯一一份 ULID 实现，见该模块文档）。"""

    if not token.startswith(_OVERRIDE_ID_PREFIX):
        return False
    return is_ulid(token[len(_OVERRIDE_ID_PREFIX) :])


def _parse_revoke_permission_command(rest: list[str]) -> AdminCommand:
    """``revoke_permission`` 的解析，支持两种形状（#439 A 档新增第二种）：

    1. ``<override_id> <reason...>``——原有形状，按行本身定位（见 ``AdminCommand``
       文档），供已经知道 override_id 的调用方直接使用（例如 B 档管理卡逐行「收回」
       按钮，回调时本来就携带这一行的 override_id，不需要再走反查）。
    2. ``<identifier> <company_id> <metric_name> <reason...>``——与
       ``grant_permission``/``suppress_permission`` **同一个参数形状**（#439 卡内
       证据：形状不同致管理员两次真实误用），``identifier`` 是目标用户标识（open_id
       或邮箱）、``company_id``/``metric_name`` 定位要收回的那一条本地覆盖；服务端
       反查出 override_id 的职责在 ``router.py``（经 ``AdminQueries.
       resolve_override_id``），本模块只负责识别出"这是第二种形状"并原样透传三个
       字段，不做任何查库。

    判据：第一个 token 是否符合 ``lpo_`` + ULID 的形状（:func:`_is_override_id`）。
    两种形状的 token 数量域不重叠时也能分辨（形状 1 至少 2 个 token，形状 2 至少
    4 个），但判据本身用"第一个 token 长什么样"而不是"数了多少个 token"——后者会让
    一个只填了 2 个 token 的形状 2 输入（identifier 打错导致 reason 被吃掉一部分）
    被误判成形状 1 去校验 override_id 形状，报错信息文不对题；先判形状能让两条分支
    各自只处理自己的、路径清晰的失败信息。
    """

    if len(rest) < 2:
        return _unknown()

    first = rest[0]
    if _is_override_id(first):
        # 形状 1：<override_id> <reason...>
        reason = " ".join(rest[1:]).strip()
        if not reason or len(reason) > _PERMISSION_REASON_MAX_LENGTH:
            return _unknown()
        return AdminCommand(
            kind=AdminCommandKind.REVOKE_PERMISSION, identifier=first, reason=reason
        )

    # 形状 2：<identifier> <company_id> <metric_name> <reason...>——与
    # _parse_permission_command 同一套校验，但结果落在 REVOKE_PERMISSION 上。
    if len(rest) < 4:
        return _unknown()
    identifier, company_id, metric_name, *reason_tokens = rest
    if not _IDENTIFIER_PATTERN.fullmatch(identifier):
        return _unknown()
    if not _IDENTIFIER_PATTERN.fullmatch(company_id):
        return _unknown()
    if not _METRIC_TOKEN_PATTERN.fullmatch(metric_name):
        return _unknown()
    reason = " ".join(reason_tokens).strip()
    if not reason or len(reason) > _PERMISSION_REASON_MAX_LENGTH:
        return _unknown()
    return AdminCommand(
        kind=AdminCommandKind.REVOKE_PERMISSION,
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
