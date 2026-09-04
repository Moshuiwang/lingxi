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

#: 目标标识（open_id 或邮箱）允许的形状：字母、数字、下划线、连字符、点、冒号、
#: ``@``，1–128 字符。刻意排除空白、引号、分号等 SQL / shell 元字符——不是因为
#: 下游会拼接字符串（真实查询走参数化语句），而是让"格式一望而知安全"成为语法
#: 层面的性质，不依赖调用方记得转义。是否真的按邮箱解析、反查到哪个 open_id 是
#: ``router.py``/``adapters/admin_registry.py`` 的职责，本模块只判定形状是否安全。
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_.@:-]{1,128}$")

#: 标识参数进入 :data:`_IDENTIFIER_PATTERN` 之前要先处理的邮箱链接语法——管理员
#: 在飞书私聊中输入邮箱时，客户端可能把它编码成 ``mailto:`` 形式。只接受裸邮箱、
#: 裸 ``mailto:<email>``、显示文本和目标完全一致的 ``[<email>](mailto:<email>)``
#: 三类已有明确边界的输入，归一后的值仍须通过 :data:`_IDENTIFIER_PATTERN`；
#: 链接化 open_id、显示/目标不一致等其余形态一律明确拒绝，不放宽字符集扩大解析面。
_MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\[\]]*)\]\(([^()\s]*)\)")
_ANGLE_AUTOLINK_PATTERN = re.compile(r"<([^<>\s]*)>")
_INLINE_CODE_PATTERN = re.compile(r"`([^`\s]*)`")
_MAILTO_SCHEME = "mailto:"
#: 这里只做最小的邮箱形态判定，不试图实现 RFC 5322 的完整地址语法；值仍会经过
#: ``_IDENTIFIER_PATTERN`` 的长度和安全字符集门。
_EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+@[A-Za-z0-9.-]+$")
#: ``_IDENTIFIER_PATTERN`` 历史上允许冒号（部分内部标识依赖该形状），因此对明显的
#: 非 mailto URI 单独 fail closed，避免 ``http:someone@example.com`` 绕过链接边界。
_UNSUPPORTED_LINK_SCHEME_PATTERN = re.compile(
    r"^(?:https?|ftp|javascript|data|tel|file):", re.IGNORECASE
)

#: 「显示文本 + 链接目标被拆成两段」的多 token 形态——邮箱那一段在正文里占了两段
#: 以上空白分隔的 token 时，单 token 归一化（:func:`_normalize_identifier`）结构
#: 上覆盖不到，因此这里在 ``str.split()`` 之前先做一次文本级归一。归一条件严格是
#: 「显示文本与链接目标是同一个邮箱」：两侧各自剥掉可选的 ``mailto:`` 前缀后都要
#: 通过 :data:`_EMAIL_PATTERN` 且逐字相等，任何一侧不是邮箱或两侧不一致一律不归一。

#: 链接形态里「显示」段与「目标」段各自允许的最大长度，首先是一条安全边界，
#: 其次才是形状约束：这几条 pattern 原本用无上限的 ``+`` 描述两个段，正则引擎在
#: 匹配不上的长 token 上会从每一个起点重新向前扫到底，整体退化成 O(n²)，已登记
#: 管理员只要发一条超长 token 就能让 gateway 单线程事件循环长时间空转。加上确定
#: 的重复上限后整体回到线性；取 160 不改变任何一条能解析成功的输入的行为。
_LINK_SEGMENT_MAX = 160

#: ``<a …>`` 标签里 ``href`` 前后那两段属性文本的上限：属性不参与合并结果，只需要
#: 够容纳真实富文本客户端可能塞进来的样式/追踪属性，因此给得比
#: :data:`_LINK_SEGMENT_MAX` 宽松；有确定上限同样是为了防止这条 pattern 在没有
#: 闭合 ``>`` 的长输入上退化成二次方。
_LINK_ATTRIBUTE_MAX = 512

#: 六条 pattern 各自必须出现的最小标记：``<``（HTML 锚点 / 尖括号自动链接）、
#: ``[``（markdown 链接）、``(``（扁平化括号形态）、``mailto:``（裸 scheme 形态）。
#: 四个标记一个都没有时，六条 pattern 一条也不可能匹配——直接原样返回，避免为
#: 一条注定不匹配的超长文本把整个 gateway 事件循环占住。
_LINK_MARK_CHARS = "<[("
_LINK_MARK_SCHEME = "mailto:"

_LINK_PAIR_PATTERNS: tuple[re.Pattern[str], ...] = (
    # <a href="mailto:E">E</a>（含单引号、附加属性；富文本降级成 HTML 时的形态）
    re.compile(
        r"<a\b[^<>]{0,%(attr)d}?\bhref\s*=\s*(?P<quote>[\"'])(?P<target>[^\"'<>]{0,%(cap)d})(?P=quote)"
        r"[^<>]{0,%(attr)d}>\s*(?P<display>[^<>]{0,%(cap)d}?)\s*</a>"
        % {"cap": _LINK_SEGMENT_MAX, "attr": _LINK_ATTRIBUTE_MAX},
        re.IGNORECASE,
    ),
    # [E] (mailto:E)：markdown 链接被插入空格
    re.compile(
        r"\[\s*(?P<display>[^\[\]\s]{1,%(cap)d})\s*\]\s+\(\s*(?P<target>[^()\s]{1,%(cap)d})\s*\)"
        % {"cap": _LINK_SEGMENT_MAX}
    ),
    # E (mailto:E) / E (E)：「显示文本 (目标)」式扁平化
    re.compile(
        r"(?P<display>[^\s()\[\]<>`]{1,%(cap)d})\s+\(\s*(?P<target>[^()\s]{1,%(cap)d})\s*\)"
        % {"cap": _LINK_SEGMENT_MAX}
    ),
    # E [mailto:E]
    re.compile(
        r"(?P<display>[^\s()\[\]<>`]{1,%(cap)d})\s+\[\s*(?P<target>[^\[\]\s]{1,%(cap)d})\s*\]"
        % {"cap": _LINK_SEGMENT_MAX}
    ),
    # E <mailto:E>：RFC 5322 / 部分客户端的「显示 <目标>」
    re.compile(
        r"(?P<display>[^\s()\[\]<>`]{1,%(cap)d})\s+<\s*(?P<target>[^<>\s]{1,%(cap)d})\s*>"
        % {"cap": _LINK_SEGMENT_MAX}
    ),
    # E mailto:E：显示与目标之间只剩一个空格
    re.compile(
        r"(?P<display>[^\s()\[\]<>`]{1,%(cap)d})\s+(?P<target>mailto:[^\s()\[\]<>`]{1,%(cap)d})"
        % {"cap": _LINK_SEGMENT_MAX},
        re.IGNORECASE,
    ),
)

#: 逐 token 形状分类里用到的 CJK 判定（与 :data:`_METRIC_TOKEN_PATTERN` 同一区段）。
_CJK_PATTERN = re.compile(r"[一-鿿]")

#: 指标名 token 允许的形状：在 ``_IDENTIFIER_PATTERN`` 的基础上额外放行 CJK 统一
#: 表意文字区（``一-鿿``）——真实指标目录当前全部是英文 snake_case 内部 ID，管理员
#: 记不住这些内部 ID，允许中文 token 通过语法门后才谈得上按别名表反查成真正的指标
#: ID；语法层继续不关心语义，只放宽到"安全字符集"，不单独为 identifier/company_id
#: 放开同一个口子（那两类 token 目前没有中文形态的真实需求）。
_METRIC_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_.@:一-鿿-]{1,128}$")
# 银河职位名是配置中的精确自由文本，当前已知角色包含全角括号；仍只允许安全的
# 单 token 字符集，避免把文本命令拼接成开放式语法。
_POSITION_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_.@:一-鿿（）()\-]{1,128}$")

#: 追溯/审计查询默认时间窗（小时）与允许上限（30 天）。上限防止一次查询扫描
#: 出远超"最近关键事件"这个 MVP 承诺的历史范围。
DEFAULT_AUDIT_WINDOW_HOURS = 24
MAX_AUDIT_WINDOW_HOURS = 720

#: 本地权限授权/抑制/收回命令的原因文本上限：自由文本，非空白、≤500 字符——
#: 足够写清楚一次特批或收回的来龙去脉，同时防止一次输入把审计字段撑成不可读的
#: 长文。
_PERMISSION_REASON_MAX_LENGTH = 500

#: ``revoke_permission`` 的目标标识形状：历史本地权限覆盖行的内部主键前缀，或
#: 新职位+范围授权组的内部主键前缀（``permission_group_id``）。两者都不是
#: open_id——前者按历史行定位，后者按一笔职位+公司范围授权整体定位。
_OVERRIDE_ID_PREFIX = "lpo_"
_PERMISSION_GROUP_ID_PREFIX = "lpg_"
# The fixed-base implementation used the pending-action id (``pac_``) as the
# group id for already-created position grants.  Keep accepting that legacy
# shape so the no-data-migration decision does not strand existing group cards;
# newly-created grants always use the dedicated ``lpg_`` prefix.
_LEGACY_PERMISSION_GROUP_ID_PREFIX = "pac_"

_COMMAND_PREFIX = "/admin"


class AdminCommandKind(str, Enum):
    """管理命令面识别出的命令种类。"""

    HELP = "help"
    QUERY_USER = "query_user"
    QUERY_AUDIT = "query_audit"
    QUERY_TRACE = "query_trace"
    SUSPEND_USER = "suspend_user"
    RESUME_USER = "resume_user"
    GRANT_POSITION_PERMISSION = "grant_position_permission"
    REVOKE_PERMISSION = "revoke_permission"
    UNKNOWN = "unknown"


class AdminRejectReason(str, Enum):
    """一条 ``UNKNOWN`` 是哪一段没看懂：把失败落点带出解析器，让回复能指名道姓。

    这些取值只描述形状判定的落点，不含任何用户输入内容：它们会被原样写进审计
    字段（``admin.command.unknown`` 的 ``reject_reason``），审计与出站回复都不得
    回显管理员输入的原文。``NOT_A_COMMAND`` 与其余取值有一条重要分界：管理命令面
    没有 ``/admin`` 前缀预检，已登记管理员发的任何不成形文本都会走到这里；不是以
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
    #: 公司标识形状不对——中文公司名走的就是这一条：公司参数期望公司编号，
    #: 拒绝中文名是正确行为。
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

    ``company_id``/``metric_name``/``reason`` 三个字段只由 ``REVOKE_PERMISSION``
    形状 2 填。``REVOKE_PERMISSION`` 有两种形状：形状 1（旧，按行定位）只填
    ``identifier``（复用同一字段承载 override_id，不是 open_id）与 ``reason``；
    形状 2（新，与 grant/suppress 同一参数形状）``identifier`` 是目标用户标识、
    ``company_id``/``metric_name``/``reason`` 均非空，``router.py`` 据此先反查出
    override_id 再退化成与形状 1 相同的下游调用。两种形状对下游保持逐字节相同的
    调用面，`command.company_id is not None` 是唯一的判据。
    """

    kind: AdminCommandKind
    identifier: str | None = None
    window_hours: int | None = None
    company_id: str | None = None
    metric_name: str | None = None
    reason: str | None = None
    position_name: str | None = None
    company_scope: str | None = None
    #: 仅 ``UNKNOWN`` 填：这条输入是哪一段没看懂，供 ``router.py`` 回一句能让
    #: 管理员自救的分段报错，并写进审计。其余 ``kind`` 恒为 ``None``——解析成功
    #: 的命令没有"失败原因"可言。
    reject_reason: AdminRejectReason | None = None


def _unknown(reason: AdminRejectReason) -> AdminCommand:
    """构造一条带失败原因的 ``UNKNOWN``。

    ``reason`` 没有默认值，新增一条拒绝分支时不可能"忘了填"。
    """
    return AdminCommand(kind=AdminCommandKind.UNKNOWN, reject_reason=reason)


def _normalize_identifier(token: str) -> str | None:
    """归一化管理员 p2p 邮箱参数的受控 ``mailto`` 形态。

    只有裸邮箱、裸 ``mailto:<email>`` 和显示文本/目标一致的
    ``[<email>](mailto:<email>)`` 会被归一化，其它链接包装、链接化 open_id、空
    显示文本及显示/目标不一致都返回 ``None``，由调用方落到 ``BAD_IDENTIFIER``；
    裸 open_id 和既有的非邮箱标识原样返回。返回值随后仍须通过
    :data:`_IDENTIFIER_PATTERN`；本函数本身不做查询、权限判断或出站处理。
    """
    if _EMAIL_PATTERN.fullmatch(token):
        return token

    # Markdown 链接只接受 mailto 目标，且显示文本必须就是同一个邮箱。先用通用形状
    # 锚定整个 token，避免从任意正文中寻找并剥掉一段子串。
    match = _MARKDOWN_LINK_PATTERN.fullmatch(token)
    if match is not None:
        display, target = match.group(1), match.group(2)
        if not target[: len(_MAILTO_SCHEME)].casefold() == _MAILTO_SCHEME:
            return None
        target_email = target[len(_MAILTO_SCHEME) :]
        if (
            _EMAIL_PATTERN.fullmatch(display)
            and _EMAIL_PATTERN.fullmatch(target_email)
            and display == target_email
        ):
            return display
        return None

    # ``mailto:`` 本身是 _IDENTIFIER_PATTERN 可接受的字符组合，故必须在字符集门前
    # 处理；非法 payload 不能退回原 token，否则 ``mailto:ou_...`` 会被当作 open_id。
    if token[: len(_MAILTO_SCHEME)].casefold() == _MAILTO_SCHEME:
        payload = token[len(_MAILTO_SCHEME) :]
        return payload if _EMAIL_PATTERN.fullmatch(payload) else None

    # 这些包装没有产品依据，尤其不能把尖括号/反引号里的值当作目标标识。
    if _ANGLE_AUTOLINK_PATTERN.fullmatch(token) or _INLINE_CODE_PATTERN.fullmatch(token):
        return None

    # 历史标识形状允许冒号；仅拦明显的 URL/URI scheme，避免把任意链接当成邮箱或
    # 标识透传，同时不改动其它内部标识的既有解析语义。
    if _UNSUPPORTED_LINK_SCHEME_PATTERN.match(token) or "://" in token:
        return None
    return token


def _link_payload_email(token: str) -> str | None:
    """剥掉可选的 ``mailto:`` 前缀后，这一段是不是一个邮箱？不是就返回 ``None``。

    显示侧与目标侧共用同一个判定，因此 ``mailto:E`` 与 ``E`` 会归一到同一个值——
    ``E (mailto:E)`` 与 ``mailto:E (mailto:E)`` 都算「显示与目标一致」。归一后的值
    仍要通过 :data:`_IDENTIFIER_PATTERN`，本函数不放宽字符集。
    """
    if token[: len(_MAILTO_SCHEME)].casefold() == _MAILTO_SCHEME:
        token = token[len(_MAILTO_SCHEME) :]
    return token if _EMAIL_PATTERN.fullmatch(token) else None


def _collapse_link_pair(match: re.Match[str]) -> str:
    """显示与目标是同一个邮箱才合并成一段；否则原样退回（fail closed）。"""
    display = _link_payload_email(match.group("display"))
    target = _link_payload_email(match.group("target"))
    if display is None or target is None or display != target:
        return match.group(0)
    return display


def _collapse_identifier_link_forms(text: str) -> str:
    """把「显示文本 + 链接目标」这类多段形态合并回一个邮箱 token。

    见 :data:`_LINK_PAIR_PATTERNS`。只做合并，不改写邮箱本身的大小写或内容；一次
    都没合并成功时返回的字符串与入参逐字相等。成本对输入长度必须是线性的：先做
    一次必要标记预检（一个都没有就原样返回），六条 pattern 的显示/目标段也都带
    确定上限（见 :data:`_LINK_SEGMENT_MAX`），不留二次方回溯面。
    """
    if not any(mark in text for mark in _LINK_MARK_CHARS) and (
        _LINK_MARK_SCHEME not in text.casefold()
    ):
        # 六条 pattern 的必要标记一个都没有：不可能合并出任何东西，原样返回。
        return text
    for pattern in _LINK_PAIR_PATTERNS:
        text = pattern.sub(_collapse_link_pair, text)
    return text


#: 逐 token 形状分类的取值。只描述形状，不含任何输入原文，因此可以原样写进审计
#: 字段 ``token_shapes``——下一次真人踩到时，这一串就足以指认客户端到底把邮箱
#: 渲染成了哪一种纯文本形态，不必再靠推理排除。
_TOKEN_SHAPE_OTHER = "other"

#: ``raw_text`` 的长度上限：取证要的是"客户端把命令渲染成了什么"，不是让一条审计
#: 行被一次超长输入撑爆。超出部分截断并留可见标记。
_RAW_ADMIN_TEXT_LIMIT = 512
_RAW_ADMIN_TEXT_TRUNCATION_MARK = "…[truncated]"


def _token_shape(token: str) -> str:
    """把一个空白分隔的 token 归到一个固定的形状名上。见 :data:`_TOKEN_SHAPE_OTHER`。"""
    lowered = token.casefold()
    if lowered == _COMMAND_PREFIX:
        return "admin_prefix"
    if lowered.startswith("</a"):
        return "html_anchor_close"
    if lowered.startswith("<a"):
        return "html_anchor_open"
    if lowered.startswith("href="):
        # ``<a href="mailto:E">E</a>`` 被空白切开后的第二段同时以 ``href=`` 开头、
        # 以 ``</a>`` 结尾；属性名是更有信息量的那一半，先判它。
        return "html_href_attribute"
    if "</a>" in lowered:
        return "html_anchor_close"
    if token.isdigit():
        return "digits"
    if _EMAIL_PATTERN.fullmatch(token):
        return "email"
    if lowered.startswith(_MAILTO_SCHEME):
        payload = token[len(_MAILTO_SCHEME) :]
        return "mailto_email" if _EMAIL_PATTERN.fullmatch(payload) else "mailto_other"
    if _MARKDOWN_LINK_PATTERN.fullmatch(token):
        return "markdown_link"
    if _ANGLE_AUTOLINK_PATTERN.fullmatch(token):
        return "angle_wrapped"
    if _INLINE_CODE_PATTERN.fullmatch(token):
        return "backtick_wrapped"
    if token.startswith("(") and token.endswith(")") and len(token) > 1:
        return "paren_wrapped"
    if token.startswith("[") and token.endswith("]") and len(token) > 1:
        return "bracket_wrapped"
    if _UNSUPPORTED_LINK_SCHEME_PATTERN.match(token) or "://" in token:
        return "url"
    if is_ulid(token):
        return "ulid"
    if token.startswith("ou_"):
        return "open_id_like"
    if token.startswith(
        (_OVERRIDE_ID_PREFIX, _PERMISSION_GROUP_ID_PREFIX, _LEGACY_PERMISSION_GROUP_ID_PREFIX)
    ):
        return "override_id_like"
    if _CJK_PATTERN.search(token):
        return "cjk_text"
    if "@" in token:
        return "at_shaped"
    if _IDENTIFIER_PATTERN.fullmatch(token):
        return "bare_word"
    return _TOKEN_SHAPE_OTHER


@dataclass(frozen=True)
class AdminTokenShapes:
    """一条管理命令输入的形状画像，不含任何输入原文。

    ``router.py`` 在 ``admin.command.unknown`` 分支把它写进审计，并用
    ``argument_count`` 让回复能说出"实际收到 N 段参数"——一条 ``wrong_argument_
    count`` 只留下一个枚举名，无法区分"客户端把邮箱拆成了两段"和"管理员真的
    多打了一个参数"，两个假设都能产生逐字相同的审计，因此值得单独取证。
    """

    #: 整条文本是不是以 ``/admin`` 开头。不是就不做任何取证（闲聊不留原文）。
    is_admin_prefixed: bool
    #: ``/admin <子命令>`` 之后还剩几段（归一化**之前**的原始分段数）。
    argument_count: int
    #: 逐 token 形状，顺序与原文一致；长度等于整条文本的原始分段数。
    shapes: tuple[str, ...]
    #: 供审计留证的命令原文（超长按 :data:`_RAW_ADMIN_TEXT_LIMIT` 截断）。**不是
    #: ``/admin`` 开头时恒为空串**——闲聊不留原文这条纪律在取样这一步就已经生效，
    #: 不依赖调用方记得判断。
    raw_text: str

    @property
    def shape_summary(self) -> str:
        """审计字段用的一行摘要（逗号分隔的形状名，无原文）。"""
        return ",".join(self.shapes)


def describe_admin_tokens(text: object) -> AdminTokenShapes:
    """按 :class:`AdminTokenShapes` 描述一条输入的分段形状。

    刻意在**归一化之前**取样：走到 ``UNKNOWN`` 说明归一化要么没触发、要么没能救回
    这条输入，此时真正需要留证的正是客户端发过来的原始分段。
    """
    if not isinstance(text, str):
        return AdminTokenShapes(is_admin_prefixed=False, argument_count=0, shapes=(), raw_text="")
    tokens = text.strip().split()
    is_admin_prefixed = bool(tokens) and tokens[0].casefold() == _COMMAND_PREFIX
    raw_text = ""
    if is_admin_prefixed:
        raw_text = (
            text
            if len(text) <= _RAW_ADMIN_TEXT_LIMIT
            else text[:_RAW_ADMIN_TEXT_LIMIT] + _RAW_ADMIN_TEXT_TRUNCATION_MARK
        )
    return AdminTokenShapes(
        is_admin_prefixed=is_admin_prefixed,
        argument_count=max(0, len(tokens) - 2),
        shapes=tuple(_token_shape(token) for token in tokens),
        raw_text=raw_text,
    )


def parse_admin_command(text: object) -> AdminCommand:
    """把一条私聊文本解析成已知命令之一，或 ``UNKNOWN``。

    语法（大小写不敏感，仅识别整条消息按空白切分后的形状）：``help``；
    ``user <identifier>``；``audit [<identifier>] [<hours>]``；``trace <追溯号>``；
    ``suspend``/``resume <identifier>``；``revoke_permission`` 两种形状（见
    :func:`_parse_revoke_permission_command`）。不匹配以上形状一律返回
    ``UNKNOWN`` 并带上哪一段没看懂（见 :class:`AdminRejectReason`）。标识参数先经
    :func:`_normalize_identifier` 容忍受控邮箱链接语法，原样解析失败时还会用
    :func:`_collapse_identifier_link_forms` 重试一次。
    """
    if not isinstance(text, str):
        return _unknown(AdminRejectReason.NOT_A_COMMAND)

    command = _parse_tokens(text.strip().split())
    if (
        command.kind is not AdminCommandKind.UNKNOWN
        or command.reject_reason is AdminRejectReason.NOT_A_COMMAND
    ):
        return command

    collapsed = _collapse_identifier_link_forms(text)
    if collapsed == text:
        return command
    retried = _parse_tokens(collapsed.strip().split())
    if retried.kind is AdminCommandKind.UNKNOWN:
        return command
    return retried


def _parse_tokens(tokens: list[str]) -> AdminCommand:
    """已经按空白切好的一条命令的解析主体，见 :func:`parse_admin_command`。"""
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

    if sub in {"grant_position", "grant_position_permission"}:
        return _parse_position_permission_command(rest)

    if sub == "revoke_permission":
        return _parse_revoke_permission_command(rest)

    return _unknown(AdminRejectReason.UNKNOWN_SUBCOMMAND)


def _parse_single_identifier(rest: list[str], *, kind: AdminCommandKind) -> AdminCommand:
    """``user``/``suspend``/``resume`` 共用的"恰好一个目标标识"解析。

    三处并成一处，避免在标识归一化前先插入链接语法归一化、之后区分"参数个数
    不对"与"标识形状不对"两种失败原因时漏改其中一处。
    """
    if len(rest) != 1:
        return _unknown(AdminRejectReason.WRONG_ARGUMENT_COUNT)
    identifier = _normalize_identifier(rest[0])
    if identifier is None or not _IDENTIFIER_PATTERN.fullmatch(identifier):
        return _unknown(AdminRejectReason.BAD_IDENTIFIER)
    return AdminCommand(kind=kind, identifier=identifier)


def _parse_permission_command(rest: list[str], *, kind: AdminCommandKind) -> AdminCommand:
    """``revoke_permission`` 形状 2（见 :func:`_parse_revoke_permission_command`）的解析。

    ``grant_permission``/``suppress_permission`` 已撤除，这个形状只剩收回一个
    使用者：``<identifier> <company_id> <metric_name> <reason...>``——前三个
    token 同 ``user``/``suspend``/``resume`` 形状约束，第四个及以后拼接还原成
    ``reason``。``identifier`` 先过 :func:`_normalize_identifier`（邮箱位会被
    自动链接化）；至少 4 个 token，``reason`` 为空或超长均返回 ``UNKNOWN``。
    """
    if len(rest) < 4:
        return _unknown(AdminRejectReason.WRONG_ARGUMENT_COUNT)
    identifier, company_id, metric_name, *reason_tokens = rest
    identifier = _normalize_identifier(identifier)
    if identifier is None or not _IDENTIFIER_PATTERN.fullmatch(identifier):
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
    if identifier is None or not _IDENTIFIER_PATTERN.fullmatch(identifier):
        return _unknown(AdminRejectReason.BAD_IDENTIFIER)
    if not _POSITION_TOKEN_PATTERN.fullmatch(position_name):
        return _unknown(AdminRejectReason.BAD_METRIC_NAME)
    if not _IDENTIFIER_PATTERN.fullmatch(company_scope) and company_scope not in {
        "*",
        "全部",
        "all",
    }:
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
            for candidate in (
                _OVERRIDE_ID_PREFIX,
                _PERMISSION_GROUP_ID_PREFIX,
                _LEGACY_PERMISSION_GROUP_ID_PREFIX,
            )
            if token.startswith(candidate)
        ),
        None,
    )
    if prefix is None:
        return False
    return is_ulid(token[len(prefix) :])


def _parse_revoke_permission_command(rest: list[str]) -> AdminCommand:
    """``revoke_permission`` 的解析，支持两种形状。

    1. ``<override_id|permission_group_id> <reason...>``——管理卡按钮形状。
    2. ``<identifier> <company_id> <metric_name> <reason...>``——三段定位要
       收回的本地覆盖；服务端反查在 ``router.py``，本模块只识别形状、原样透传。

    判据是第一个 token 是否符合 override_id 的形状（:func:`_is_override_id`），
    不是数 token 个数——按数量分辨会让 identifier 打错的形状 2 输入误判成
    形状 1，报错信息文不对题。
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

    # 形状 2：<identifier> <company_id> <metric_name> <reason...>——直接复用
    # _parse_permission_command 同一套校验，只换结果 kind，不逐字复制第二份
    # （两份都要插入标识归一化才不漏，复制反而更容易漏）。
    return _parse_permission_command(rest, kind=AdminCommandKind.REVOKE_PERMISSION)


def _parse_audit(rest: list[str]) -> AdminCommand:
    if not rest:
        return AdminCommand(
            kind=AdminCommandKind.QUERY_AUDIT, window_hours=DEFAULT_AUDIT_WINDOW_HOURS
        )
    if len(rest) == 1:
        token = rest[0]
        if token.isdecimal():
            hours = _validated_hours(token)
            if hours is None:
                return _unknown(AdminRejectReason.BAD_WINDOW_HOURS)
            return AdminCommand(kind=AdminCommandKind.QUERY_AUDIT, window_hours=hours)
        identifier = _normalize_identifier(token)
        if identifier is None or not _IDENTIFIER_PATTERN.fullmatch(identifier):
            return _unknown(AdminRejectReason.BAD_IDENTIFIER)
        return AdminCommand(
            kind=AdminCommandKind.QUERY_AUDIT,
            identifier=identifier,
            window_hours=DEFAULT_AUDIT_WINDOW_HOURS,
        )
    if len(rest) == 2:
        identifier, hours_token = rest
        identifier = _normalize_identifier(identifier)
        # 拆成两条判断而不是合在一个 `or` 里：合并判断报错说不清是标识不对
        # 还是小时数不对。
        if identifier is None or not _IDENTIFIER_PATTERN.fullmatch(identifier):
            return _unknown(AdminRejectReason.BAD_IDENTIFIER)
        if not hours_token.isdecimal():
            return _unknown(AdminRejectReason.BAD_WINDOW_HOURS)
        hours = _validated_hours(hours_token)
        if hours is None:
            return _unknown(AdminRejectReason.BAD_WINDOW_HOURS)
        return AdminCommand(
            kind=AdminCommandKind.QUERY_AUDIT, identifier=identifier, window_hours=hours
        )
    return _unknown(AdminRejectReason.WRONG_ARGUMENT_COUNT)


def _validated_hours(token: str) -> int | None:
    """把已经过形状判定的小时数 token 换算成整数；换算不出来一律 ``None``。

    判定用 ``str.isdecimal()`` 而不是 ``str.isdigit()``：``isdigit()`` 对上标/
    下标数字（``"²⁴"``、``"₁₂"``）与圈号数字为真，``int()`` 对它们却直接抛
    ``ValueError``，会把整条命令打挂成一句笼统的处理失败而不是"小时数不对"这条
    明确拒绝。``isdecimal()`` 只对真正能被 ``int()`` 接受的十进制数字为真。外面那层
    ``try`` 是纵深：形状判定与换算分处两个函数，未来任何一处放宽都不该重新把
    一个奇怪字符变成一次异常。
    """
    try:
        value = int(token)
    except ValueError:
        return None
    if value < 1 or value > MAX_AUDIT_WINDOW_HOURS:
        return None
    return value
