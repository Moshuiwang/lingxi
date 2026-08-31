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

#: 标识参数进入 :data:`_IDENTIFIER_PATTERN` **之前**要先剥掉的链接语法（Issue #492）。
#:
#: 真实缺陷：管理员在飞书客户端里输入邮箱，客户端把它自动变成了链接；服务端仍然收到
#: ``message_type: "text"``（Trace #502 W0-2 的 L6 负面证据：这位管理员 8 天 42 条入站
#: 事件全部是 text、零丢弃，全量日志 ``message.unsupported_type`` 零条，而同期他客户端
#: 确实发生了链接化），链接语法因此被编码进 ``content.text`` 字符串里。链接化之后的
#: 邮箱不含空白、仍然是一个整 token，token 数量对得上，直到 :data:`_IDENTIFIER_PATTERN`
#: 才因为字符集不含 ``[]()<>`` 而落进 ``UNKNOWN``——管理员看到的却只是"未识别的管理
#: 命令"，无从自救（产品负责人 2026-08-31 连踩三次）。
#:
#: **修法是归一化，不是放宽字符集**：本模块开头声明的"语法封闭是第二道独立防线"依赖
#: :data:`_IDENTIFIER_PATTERN` 保持窄，放宽它等于削掉那道防线。因此这里只做一件事——
#: 把公认的链接**包装**剥掉，剥完得到的内容**仍然要原样通过** :data:`_IDENTIFIER_
#: PATTERN`。所以 ``[;DROP--](mailto:x)`` 剥成 ``;DROP--`` 之后照样是 ``UNKNOWN``：
#: 归一化不放行任何一个此前被字符集拒绝的**内容**，只放行它的**外壳**。
#:
#: 覆盖的形态与依据：
#:
#: - ``[显示文本](链接)``——飞书官方文档《发送消息内容结构》明确写明文本消息
#:   （``msg_type=text``）的超链接使用格式就是 ``[文本](链接)``，并提醒链接文本里不要
#:   嵌套 ``[]``（本正则因此不允许嵌套括号，与官方约束同一形状）。这是**有官方文档
#:   依据**的一条。
#: - ``mailto:`` 前缀——邮箱链接的 URI scheme。注意 ``:`` 本来就在
#:   :data:`_IDENTIFIER_PATTERN` 的字符集里，所以 ``mailto:a@b.com`` 此前不会落
#:   ``UNKNOWN``，而是被当成标识原样送去反查、查无此人——同一个缺陷的另一副面孔。
#: - ``<...>`` 与 `````...`````——通用 markdown 的自动链接与行内代码包装，**飞书官方
#:   文档未声明**文本消息支持这两种；纳入是防御性覆盖（剥掉之后仍走同一道字符集门，
#:   代价为零），不得对外声称是实测到的飞书形态。
#:
#: 以上形态**均来自「L6 负面证据 + 官方文档」的推定**，不是对真实信封的逐字节回读——
#: 三条失败消息的正文在 stage 上结构性不可得（``inbound_event`` 没有正文列、也没存
#: ``chat_id``/``message_id``，飞书没有枚举机器人↔用户私聊的接口），见 Issue #492 的
#: W0-2 两条评论。真实形态的最终确认由 stage 真人复现（L4a）兜底。
_MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\[\]]*)\]\(([^()\s]*)\)")
_ANGLE_AUTOLINK_PATTERN = re.compile(r"<([^<>\s]*)>")
_INLINE_CODE_PATTERN = re.compile(r"`([^`\s]*)`")
_MAILTO_SCHEME = "mailto:"

#: 剥壳最多重复几轮。链接语法可以互相嵌套（``<mailto:a@b.com>`` 要剥两轮，
#: ``[a@b.com](mailto:a@b.com)`` 一轮就够）。循环本身不靠这个上界终止——每剥一层
#: token 至少短两个字符，剥不动了就相等退出；这个常数是**愿意解释多深的包装**的
#: 上限，不是防死循环的保险。三轮覆盖上面列出的全部组合。
_MAX_LINK_UNWRAP_ROUNDS = 3

#: 指标名 token 允许的形状（#439 A 档新增中文别名支持）：在 ``_IDENTIFIER_PATTERN``
#: 的基础上额外放行 CJK 统一表意文字区（``一-鿿``）——真实指标目录
#: （``config/company_function_metric_map.toml``）当前全部是英文 snake_case 内部
#: ID（如 ``sub_new_count``），管理员记不住这些内部 ID 是 #439 的真实动机；允许中文
#: token 通过语法门后，才谈得上在 ``router.py``/``adapters/admin_registry.py`` 里
#: 按别名表反查成真正的指标 ID——语法层继续不关心语义，只放宽到"安全字符集"，不
#: 单独为 identifier/company_id 放开同一个口子（那两类 token 目前没有中文形态的
#: 真实需求，维持既有更窄的字符集）。
_METRIC_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_.@:一-鿿-]{1,128}$")
# 银河职位名是配置中的精确自由文本，当前已知角色包含全角括号；仍只允许安全的
# 单 token 字符集，避免把文本命令拼接成开放式语法。
_POSITION_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_.@:一-鿿（）()\-]{1,128}$")

#: 追溯/审计查询默认时间窗（小时）与允许上限（30 天）。上限防止一次查询扫描
#: 出远超"最近关键事件"这个 MVP 承诺的历史范围。
DEFAULT_AUDIT_WINDOW_HOURS = 24
MAX_AUDIT_WINDOW_HOURS = 720

#: 本地权限授权/抑制/收回命令的原因文本上限（授权/抑制：#319 S-P-1b 设计卡；
#: 收回：卡 B 沿用同一上限）：自由文本，非空白、≤500 字符——足够写清楚一次特批
#: 或收回的来龙去脉，同时防止一次输入把审计字段撑成不可读的长文。
_PERMISSION_REASON_MAX_LENGTH = 500

#: ``revoke_permission`` 的目标标识形状：历史本地权限覆盖行的内部主键前缀，或
#: 新职位+范围授权组的内部主键前缀（``permission_group_id``）。两者都不是
#: open_id——前者按历史行定位，后者按一笔职位+公司范围授权整体定位。
_OVERRIDE_ID_PREFIX = "lpo_"
_PERMISSION_GROUP_ID_PREFIX = "lpg_"

_COMMAND_PREFIX = "/admin"


class AdminCommandKind(str, Enum):
    HELP = "help"
    QUERY_USER = "query_user"
    QUERY_AUDIT = "query_audit"
    QUERY_TRACE = "query_trace"
    SUSPEND_USER = "suspend_user"
    RESUME_USER = "resume_user"
    GRANT_PERMISSION = "grant_permission"
    GRANT_POSITION_PERMISSION = "grant_position_permission"
    SUPPRESS_PERMISSION = "suppress_permission"
    REVOKE_PERMISSION = "revoke_permission"
    UNKNOWN = "unknown"


class AdminRejectReason(str, Enum):
    """一条 ``UNKNOWN`` 是**哪一段**没看懂（Issue #492）。

    此前 :func:`parse_admin_command` 只回一个不带任何原因的 ``UNKNOWN``，
    ``router.py`` 因此只能回一句"未识别的管理命令，请发送 /admin help"——管理员
    无从自救，产品负责人 2026-08-31 连踩三次正是这个体验。本枚举把失败落点带出
    解析器，让回复能指名道姓说清是用户标识、公司标识、指标、原因还是命令名没通过。

    这些取值**只描述形状判定的落点，不含任何用户输入内容**：它们会被原样写进审计
    字段（``admin.command.unknown`` 的 ``reject_reason``），而审计与出站回复都不得
    回显管理员输入的原文（回显会把 ``<at user_id="all">`` 一类飞书文本标记反射回
    出站消息，见 ``router._render_unknown`` 的说明）。

    ``NOT_A_COMMAND`` 与其余取值有一条重要分界：``router.py`` 的管理命令面**没有
    ``/admin`` 前缀预检**，已登记管理员发的任何不成形文本都会走到这里。不是以
    ``/admin`` 开头的文本（普通聊天、问数）只能得到既有那句笼统文案——对它做分段
    报错等于对每一句闲聊都解释命令语法，是误伤。
    """

    #: 整条文本根本不是 ``/admin`` 开头（或不是字符串）——不是一次命令尝试。
    NOT_A_COMMAND = "not_a_command"
    #: ``/admin`` 之后的命令名不在已知清单里（含只发了一个裸 ``/admin``）。
    UNKNOWN_SUBCOMMAND = "unknown_subcommand"
    #: 命令名认出来了，但参数个数与该命令的形状对不上。
    WRONG_ARGUMENT_COUNT = "wrong_argument_count"
    #: 目标用户标识（open_id 或邮箱）形状不对。
    BAD_IDENTIFIER = "bad_identifier"
    #: 公司标识形状不对——**中文公司名走的就是这一条**（公司参数期望公司编号，
    #: 拒绝中文名是正确行为，缺陷只在于此前没有说清楚，见 Issue #492 裁定二）。
    BAD_COMPANY_ID = "bad_company_id"
    #: 指标名 / 中文别名形状不对。
    BAD_METRIC_NAME = "bad_metric_name"
    #: 原因文本为空或超长。
    BAD_REASON = "bad_reason"
    #: 时间窗小时数不是数字或越界。
    BAD_WINDOW_HOURS = "bad_window_hours"
    #: 追溯号不是裸 ULID 形状。
    BAD_TRACE_ID = "bad_trace_id"


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
    position_name: str | None = None
    company_scope: str | None = None
    #: 仅 ``UNKNOWN`` 填（Issue #492）：这条输入是**哪一段**没看懂，供
    #: ``router.py`` 回一句能让管理员自救的分段报错，并写进审计。其余 ``kind``
    #: 恒为 ``None``——解析成功的命令没有"失败原因"可言。
    reject_reason: AdminRejectReason | None = None


def _unknown(reason: AdminRejectReason) -> AdminCommand:
    """每一条 ``UNKNOWN`` 都必须说明是哪一段没看懂——``reason`` 没有默认值，
    新增一条拒绝分支时不可能"忘了填"（Issue #492）。"""

    return AdminCommand(kind=AdminCommandKind.UNKNOWN, reject_reason=reason)


def _unwrap_link_syntax_once(token: str) -> str:
    """剥掉一层链接语法外壳；没有可剥的外壳时原样返回。

    ``[显示文本](链接)`` 取的是**显示文本**，不是链接目标——管理员看到的是显示
    文本，命令应当作用在他看到的那个标识上（自动链接化的场景里两者本来就相同）。
    只有显示文本为空（``[](mailto:a@b.com)``）时才退到链接目标，因为那时显示文本
    不承载任何信息。反过来优先取链接目标会引入"看到的是 A、实际操作 B"这种钓鱼
    形状的错位，即使输入来自已登记管理员也不值得引入。
    """

    match = _MARKDOWN_LINK_PATTERN.fullmatch(token)
    if match is not None:
        display, target = match.group(1), match.group(2)
        return display or target
    match = _ANGLE_AUTOLINK_PATTERN.fullmatch(token)
    if match is not None:
        return match.group(1)
    match = _INLINE_CODE_PATTERN.fullmatch(token)
    if match is not None:
        return match.group(1)
    if token[: len(_MAILTO_SCHEME)].lower() == _MAILTO_SCHEME:
        return token[len(_MAILTO_SCHEME) :]
    return token


def _normalize_identifier(token: str) -> str:
    """把一个可能被飞书客户端自动链接化的标识 token 还原成裸标识（Issue #492）。

    只在**目标用户标识**这一个位置调用（``user``/``audit``/``suspend``/``resume``/
    ``grant_permission``/``suppress_permission``/``revoke_permission`` 形状 2 的第一个
    参数）——这些是唯一可能承载邮箱、因而唯一会被客户端自动链接化的参数。公司标识、
    指标名、原因、追溯号、覆盖ID 都不走这里：它们不是邮箱形态，没有真实的链接化路径，
    多归一化一个位置就是多一份不必要的解析面。

    归一化**不放松任何校验**：返回值随后照样要通过 :data:`_IDENTIFIER_PATTERN`，
    见该常量的说明。
    """

    for _ in range(_MAX_LINK_UNWRAP_ROUNDS):
        unwrapped = _unwrap_link_syntax_once(token)
        if unwrapped == token:
            break
        token = unwrapped
    return token


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

    **标识参数容忍链接语法（Issue #492）**：同一批标识参数在做形状校验之前，会先
    经 :func:`_normalize_identifier` 剥掉飞书客户端自动加上的链接外壳
    （``[a@b.com](mailto:a@b.com)``、``mailto:a@b.com`` 等），剥完仍要通过
    ``_IDENTIFIER_PATTERN``——字符集本身没有放宽，见该常量说明。

    **``UNKNOWN`` 会带上是哪一段没看懂（Issue #492）**：见
    :class:`AdminRejectReason` 与 ``AdminCommand.reject_reason``。这只增加一个
    仅在 ``UNKNOWN`` 时填写的字段，命令的语义、权限与角色门槛一概不变。
    """

    if not isinstance(text, str):
        return _unknown(AdminRejectReason.NOT_A_COMMAND)
    tokens = text.strip().split()
    if not tokens or tokens[0].lower() != _COMMAND_PREFIX:
        return _unknown(AdminRejectReason.NOT_A_COMMAND)
    if len(tokens) < 2:
        # 只发了一个裸 `/admin`：确实在试着用命令面，缺的是命令名。
        return _unknown(AdminRejectReason.UNKNOWN_SUBCOMMAND)

    sub = tokens[1].lower()
    rest = tokens[2:]

    if sub == "help":
        if rest:
            return _unknown(AdminRejectReason.WRONG_ARGUMENT_COUNT)
        return AdminCommand(kind=AdminCommandKind.HELP)

    if sub == "user":
        return _parse_single_identifier(rest, kind=AdminCommandKind.QUERY_USER)

    if sub == "audit":
        return _parse_audit(rest)

    if sub == "trace":
        if len(rest) != 1:
            return _unknown(AdminRejectReason.WRONG_ARGUMENT_COUNT)
        if not is_ulid(rest[0]):
            return _unknown(AdminRejectReason.BAD_TRACE_ID)
        return AdminCommand(kind=AdminCommandKind.QUERY_TRACE, identifier=rest[0])

    if sub == "suspend":
        return _parse_single_identifier(rest, kind=AdminCommandKind.SUSPEND_USER)

    if sub == "resume":
        return _parse_single_identifier(rest, kind=AdminCommandKind.RESUME_USER)

    if sub == "grant_permission":
        return _parse_permission_command(rest, kind=AdminCommandKind.GRANT_PERMISSION)

    if sub in {"grant_position", "grant_position_permission"}:
        return _parse_position_permission_command(rest)

    if sub == "suppress_permission":
        return _parse_permission_command(rest, kind=AdminCommandKind.SUPPRESS_PERMISSION)

    if sub == "revoke_permission":
        return _parse_revoke_permission_command(rest)

    return _unknown(AdminRejectReason.UNKNOWN_SUBCOMMAND)


def _parse_single_identifier(rest: list[str], *, kind: AdminCommandKind) -> AdminCommand:
    """``user``/``suspend``/``resume`` 共用的"恰好一个目标标识"解析。

    三处此前各写了一遍逐字相同的两行（``len(rest) != 1 or not _IDENTIFIER_PATTERN
    .fullmatch(...)``）；Issue #492 要在这一步之前插入链接语法归一化、之后区分
    "参数个数不对"与"标识形状不对"两种失败原因，三处并成一处才不会漏掉其中一处。
    """

    if len(rest) != 1:
        return _unknown(AdminRejectReason.WRONG_ARGUMENT_COUNT)
    identifier = _normalize_identifier(rest[0])
    if not _IDENTIFIER_PATTERN.fullmatch(identifier):
        return _unknown(AdminRejectReason.BAD_IDENTIFIER)
    return AdminCommand(kind=kind, identifier=identifier)


def _parse_permission_command(rest: list[str], *, kind: AdminCommandKind) -> AdminCommand:
    """``grant_permission``/``suppress_permission``（以及 ``revoke_permission``
    形状 2，见 :func:`_parse_revoke_permission_command`）共用的解析：
    ``<identifier> <company_id> <metric_name> <reason...>``——前三个 token 与既有
    ``user``/``suspend``/``resume`` 同一形状约束（``_IDENTIFIER_PATTERN``），
    第四个及以后全部 token 按空白拼接还原成一段自由文本 ``reason``。

    ``identifier`` 在校验之前先过一次 :func:`_normalize_identifier`（Issue #492）：
    这一位是邮箱位，会被飞书客户端自动链接化；``company_id``/``metric_name``
    刻意**不**归一化——它们不是邮箱形态，没有真实的链接化路径。

    至少需要 4 个 token（标识 + 公司 + 指标 + 至少一个原因词）；拼接后的 ``reason``
    去除首尾空白后为空，或超过 :data:`_PERMISSION_REASON_MAX_LENGTH` 字符，均视为
    形状不对，返回 ``UNKNOWN``——与本模块"识别不出来的输入一律 UNKNOWN"的既有纪律
    一致，不对越界输入做截断或静默修正。
    """

    if len(rest) < 4:
        return _unknown(AdminRejectReason.WRONG_ARGUMENT_COUNT)
    identifier, company_id, metric_name, *reason_tokens = rest
    identifier = _normalize_identifier(identifier)
    if not _IDENTIFIER_PATTERN.fullmatch(identifier):
        return _unknown(AdminRejectReason.BAD_IDENTIFIER)
    if not _IDENTIFIER_PATTERN.fullmatch(company_id):
        return _unknown(AdminRejectReason.BAD_COMPANY_ID)
    if not _METRIC_TOKEN_PATTERN.fullmatch(metric_name):
        return _unknown(AdminRejectReason.BAD_METRIC_NAME)
    reason = " ".join(reason_tokens).strip()
    if not reason or len(reason) > _PERMISSION_REASON_MAX_LENGTH:
        return _unknown(AdminRejectReason.BAD_REASON)
    return AdminCommand(
        kind=kind,
        identifier=identifier,
        company_id=company_id,
        metric_name=metric_name,
        reason=reason,
    )


def _parse_position_permission_command(rest: list[str]) -> AdminCommand:
    """解析管理卡使用的「银河职位 + 公司范围 + 原因」授权命令。

    该命令不是新的权限类型，只是把产品表单的两个维度封装进受控文本路由；真正
    的职位映射和公司范围展开由待确认操作适配器在服务端完成。公司范围 ``*``
    表示「全部」，实际公司数量始终由当前映射目录计算。
    """

    if len(rest) < 4:
        return _unknown(AdminRejectReason.WRONG_ARGUMENT_COUNT)
    identifier, position_name, company_scope, *reason_tokens = rest
    identifier = _normalize_identifier(identifier)
    if not _IDENTIFIER_PATTERN.fullmatch(identifier):
        return _unknown(AdminRejectReason.BAD_IDENTIFIER)
    if not _POSITION_TOKEN_PATTERN.fullmatch(position_name):
        return _unknown(AdminRejectReason.BAD_METRIC_NAME)
    if not _IDENTIFIER_PATTERN.fullmatch(company_scope) and company_scope not in {"*", "全部", "all"}:
        return _unknown(AdminRejectReason.BAD_COMPANY_ID)
    reason = " ".join(reason_tokens).strip()
    if not reason or len(reason) > _PERMISSION_REASON_MAX_LENGTH:
        return _unknown(AdminRejectReason.BAD_REASON)
    return AdminCommand(
        kind=AdminCommandKind.GRANT_POSITION_PERMISSION,
        identifier=identifier,
        position_name=position_name,
        company_scope="*" if company_scope.casefold() in {"all", "全部"} else company_scope,
        reason=reason,
    )


def _is_override_id(token: str) -> bool:
    """历史 ``lpo_`` 行 ID 或新授权组 ``lpg_`` ID。"""

    prefix = next(
        (
            candidate
            for candidate in (_OVERRIDE_ID_PREFIX, _PERMISSION_GROUP_ID_PREFIX)
            if token.startswith(candidate)
        ),
        None,
    )
    if prefix is None:
        return False
    return is_ulid(token[len(prefix) :])


def _parse_revoke_permission_command(rest: list[str]) -> AdminCommand:
    """``revoke_permission`` 的解析，支持两种形状（#439 A 档新增第二种）：

    1. ``<override_id|permission_group_id> <reason...>``——管理卡按钮形状：历史
       ``lpo_`` 按行定位，新授权组 ``lpg_`` 按职位+范围整体定位。
    2. ``<identifier> <company_id> <metric_name> <reason...>``——与
       ``grant_permission``/``suppress_permission`` **同一个参数形状**（#439 卡内
       证据：形状不同致管理员两次真实误用），``identifier`` 是目标用户标识（open_id
       或邮箱）、``company_id``/``metric_name`` 定位要收回的那一条本地覆盖；服务端
       反查出 override_id 的职责在 ``router.py``（经 ``AdminQueries.
       resolve_override_id``），本模块只负责识别出"这是第二种形状"并原样透传三个
       字段，不做任何查库。

    判据：第一个 token 是否符合 ``lpo_``/``lpg_`` + ULID 的形状
    （:func:`_is_override_id`）。
    两种形状的 token 数量域不重叠时也能分辨（形状 1 至少 2 个 token，形状 2 至少
    4 个），但判据本身用"第一个 token 长什么样"而不是"数了多少个 token"——后者会让
    一个只填了 2 个 token 的形状 2 输入（identifier 打错导致 reason 被吃掉一部分）
    被误判成形状 1 去校验 override_id 形状，报错信息文不对题；先判形状能让两条分支
    各自只处理自己的、路径清晰的失败信息。
    """

    if len(rest) < 2:
        return _unknown(AdminRejectReason.WRONG_ARGUMENT_COUNT)

    first = rest[0]
    if _is_override_id(first):
        # 形状 1：<override_id> <reason...>
        reason = " ".join(rest[1:]).strip()
        if not reason or len(reason) > _PERMISSION_REASON_MAX_LENGTH:
            return _unknown(AdminRejectReason.BAD_REASON)
        return AdminCommand(
            kind=AdminCommandKind.REVOKE_PERMISSION, identifier=first, reason=reason
        )

    # 形状 2：<identifier> <company_id> <metric_name> <reason...>——与
    # _parse_permission_command 同一套校验（含 Issue #492 的标识链接语法归一化），
    # 但结果落在 REVOKE_PERMISSION 上。
    # 形状 2 与 grant/suppress 逐条同一套校验（标识/公司/指标/原因，含 Issue #492
    # 的标识链接语法归一化与分段失败原因），此前是逐字复制的第二份；两份都要插入
    # 归一化才不漏，复制反而更容易漏，因此改为直接复用同一个实现、只换结果 kind。
    return _parse_permission_command(rest, kind=AdminCommandKind.REVOKE_PERMISSION)


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
                return _unknown(AdminRejectReason.BAD_WINDOW_HOURS)
            return AdminCommand(kind=AdminCommandKind.QUERY_AUDIT, window_hours=hours)
        identifier = _normalize_identifier(token)
        if not _IDENTIFIER_PATTERN.fullmatch(identifier):
            return _unknown(AdminRejectReason.BAD_IDENTIFIER)
        return AdminCommand(
            kind=AdminCommandKind.QUERY_AUDIT,
            identifier=identifier,
            window_hours=DEFAULT_AUDIT_WINDOW_HOURS,
        )
    if len(rest) == 2:
        identifier, hours_token = rest
        identifier = _normalize_identifier(identifier)
        # 此前这两项合在一个 `or` 里判，报错说不清是标识不对还是小时数不对
        # （Issue #492 完成标准 4）；拆成两条判断，语义逐字不变。
        if not _IDENTIFIER_PATTERN.fullmatch(identifier):
            return _unknown(AdminRejectReason.BAD_IDENTIFIER)
        if not hours_token.isdigit():
            return _unknown(AdminRejectReason.BAD_WINDOW_HOURS)
        hours = _validated_hours(hours_token)
        if hours is None:
            return _unknown(AdminRejectReason.BAD_WINDOW_HOURS)
        return AdminCommand(
            kind=AdminCommandKind.QUERY_AUDIT, identifier=identifier, window_hours=hours
        )
    return _unknown(AdminRejectReason.WRONG_ARGUMENT_COUNT)


def _validated_hours(token: str) -> int | None:
    value = int(token)
    if value < 1 or value > MAX_AUDIT_WINDOW_HOURS:
        return None
    return value
