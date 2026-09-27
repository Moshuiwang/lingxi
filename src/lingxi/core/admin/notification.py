"""确认卡片与管理群终态通知的展示层：纯函数渲染 + 卡片出站 Protocol。

只负责"展示什么"，不负责"怎么发出去"：真实 CardKit 调用住在
``adapters/feishu_admin_card.py``，真实群消息复用既有
``adapters/feishu_group_message.FeishuGroupMessages``。本模块因此不 import 任何飞书
SDK，可以在没有装 SDK 的环境里测试全部渲染断言。

不复用 ``core/execution/card_stream.RenderedCard``：那个类型是流式问数结果卡片的
展示形状，不支持按钮各自绑定回调值；确认卡的两个按钮必须分别携带
``pending_action_id`` 与 ``decision`` 供 ``card.action.trigger`` 回调识别，因此
新增一个平行的最小类型，而不是给业务结果卡塞进它不需要的字段。本模块生成的文案
不经过 ``config/content.toml``：确认卡与管理群通知只面向已登记管理员，内容随
待确认操作动态变化，不适合模板变量的强匹配版本约束——与既有花名册/内测日报的
渲染方式同一惯例。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from lingxi.config.content import (
    ContentCatalog,
    ContentError,
    RenderedCard,
    default_content_catalog,
)
from lingxi.core.admin.card_layout import button_row
from lingxi.core.admin.pending_action import (
    LOCAL_PERMISSION_ACTION_TYPES,
    PENDING_ACTION_TTL_SECONDS,
    PendingAction,
    PendingActionStatus,
    PendingActionType,
)
from lingxi.core.delivery.catalog_notice import build_notice_card
from lingxi.core.delivery.notice_card import (
    _HEADER_TEMPLATES,
    NoticeCard,
    NoticeTone,
    escape_markdown,
)

logger = logging.getLogger(__name__)

#: 管理类通知的卡片键。键未进内容目录时各入口逐字沿用原渲染；进目录后标题与「下一步」
#: 取自目录，字段区由本模块按真实状态拼出，动态值一律经 ``escape_markdown`` 转义。
CONFIRM_CARD_KEY = "notice.admin.confirm"
TERMINAL_CARD_KEY = "notice.admin.terminal"
GROUP_NOTICE_CARD_KEY = "notice.admin.group_notice"
INNERTEST_CONFIRM_CARD_KEY = "notice.admin.innertest_confirm"
CORRECTION_SUMMARY_TEXT_KEY = "permission.management_correction_summary"


def _dynamic_notice(
    content_catalog: ContentCatalog | None, key: str, **values: object
) -> RenderedCard | None:
    """动态拼正文的管理类入口：卡片键进了目录就渲染它的标题与「下一步」，否则返回 ``None``。

    这些入口没有原文本键，不走 :mod:`lingxi.core.delivery.catalog_notice` 的「文本键配卡片键」。
    模板与变量对不上属于目录配置错误：只记一条不含正文的日志并返回 ``None``，入口退回
    今天的纯文本 / 原卡片，不让一条管理通知因为展示问题整条丢失。
    """
    source = content_catalog if content_catalog is not None else default_content_catalog()
    if not source.has_card(key):
        return None
    try:
        return source.card(key, **values)
    except ContentError as error:
        logger.warning("通知卡片模板不可用，沿用原展示 key=%s error=%s", key, type(error).__name__)
        return None


#: 术语统一：「新增授权」→「补充授权」、「新增抑制」→「屏蔽指标」、逐行「收回」
#: →「撤销」，与 ``core/admin/management_card._DIRECTION_LABEL``、
#: ``core/admin/router._OVERRIDE_DIRECTION_LABEL`` 三处同步，杜绝同一操作两套
#: 说法。确认卡/终态卡/群通知/标题四处共用这一份映射。
_ACTION_LABEL: dict[PendingActionType, str] = {
    PendingActionType.INNERTEST_ADDITIONS: "加入内测资格",
    PendingActionType.SUSPEND_USER: "停用",
    PendingActionType.RESUME_USER: "恢复",
    PendingActionType.LOCAL_PERMISSION_GRANT: "补充授权",
    PendingActionType.LOCAL_PERMISSION_SUPPRESS: "屏蔽指标",
    PendingActionType.LOCAL_PERMISSION_REVOKE: "撤销",
}

_IMPACT_TEXT: dict[PendingActionType, str] = {
    PendingActionType.SUSPEND_USER: "该用户将立即无法发起新的问数或开通；已在进行中的任务不受影响、正常完成交付。",
    PendingActionType.RESUME_USER: "该用户将恢复可以正常问数；此前被撤销的权限不会自动恢复。",
    PendingActionType.LOCAL_PERMISSION_GRANT: (
        "该用户将获得下方指定公司×指标的问数权限（补充授权，独立于银河翻译结果，"
        "不影响其余已有权限）。"
    ),
    PendingActionType.LOCAL_PERMISSION_SUPPRESS: "该用户将被限制访问下方指定公司×指标（屏蔽指标优先级最高，即使银河翻译结果授予也会被拦截）。",
    PendingActionType.LOCAL_PERMISSION_REVOKE: "下方指定的本地覆盖行将被撤销，不再影响该用户的权限聚合结果（银河翻译结果与其余本地覆盖不受影响）。",
}

#: ``direction`` 列取值 → 中文展示文案，只在收回的确认卡/终态卡/群通知里出现——
#: 补充授权/屏蔽指标两类动作本身的 ``_ACTION_LABEL`` 已经隐式表达了方向，只有
#: 撤销需要额外说明"被撤销的原本是哪个方向"。术语与 :data:`_ACTION_LABEL` 同步，
#: 杜绝同一操作两套说法。
_DIRECTION_LABEL: dict[str, str] = {"grant": "补充授权", "suppress": "屏蔽指标"}

#: 群通知 reason 预览长度：截断长度与管理卡/router.py 的覆盖行回显统一，与
#: ``core/admin/management_card._OVERRIDE_REASON_PREVIEW_LENGTH``/
#: ``core/admin/router._OVERRIDE_REASON_PREVIEW_LENGTH`` 同一取值，独立各自维护。
_GROUP_REASON_PREVIEW_LENGTH = 20


def _permission_payload(pending: PendingAction) -> dict[str, Any] | None:
    """解析 ``pending.payload``（JSON 字符串，仅本地权限三类动作非空）。

    解析失败或缺失时返回 ``None``——调用方据此跳过范围行的渲染，不让一条
    格式异常的历史行让整个渲染函数崩溃（本模块全程是纯函数，不允许因为一条
    脏数据抛出未预期的异常）。
    """
    if not pending.payload:
        return None
    try:
        data = json.loads(pending.payload)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _direction_prefix(payload: dict[str, Any]) -> str:
    """收回场景下渲染一行"方向：..."前缀，其余动作返回空字符串。

    收回的 payload 携带 ``direction``（被收回那一行原本是授权还是抑制）；
    授权/抑制两类动作的 payload 从不携带这个键（``.get`` 落空返回 ``""``），
    对它们完全不改变既有渲染。
    """
    direction = payload.get("direction")
    if not direction:
        return ""
    label = _DIRECTION_LABEL.get(direction, direction)
    return f"方向：{label}\n"


#: payload 异常时的降级文案：非本地权限动作的"范围+原因"区块结构上就不存在
#: （本来就该是空字符串），但本地权限动作的 payload 解析失败不应该静默退化成
#: 同一个空字符串——那会让管理员在不知道自己要操作哪个公司哪个指标的情况下
#: 点确认，因此改成显式的降级提示，不让这条本该出现的信息悄悄消失。
_SCOPE_UNAVAILABLE_TEXT = "范围信息不可用，请取消本卡重新发起。\n"


def permission_scope_ids(pending: PendingAction) -> tuple[str, str] | None:
    """返回该待确认操作 payload 里的 ``(company_id, metric_name)`` 原始值。

    供调用方在渲染确认卡/终态卡/群通知之前，先经 ``AdminDisplayNames`` 解析出
    需要人性化展示的公司中文名/指标中文别名，再传给三个渲染函数的
    ``company_label``/``metric_label`` 参数。非本地权限动作或 payload 不可用
    都返回 ``None``，渲染函数自身仍会走既有的降级路径。
    """
    if pending.action_type not in LOCAL_PERMISSION_ACTION_TYPES:
        return None
    payload = _permission_payload(pending)
    if payload is None:
        return None
    if payload.get("position_name") and payload.get("pairs"):
        first = payload["pairs"][0]
        if isinstance(first, (list, tuple)) and len(first) == 2:
            return str(first[0]), str(first[1])
    return payload.get("company_id", ""), payload.get("metric_name", "")


def _position_company_count(payload: Mapping[str, Any]) -> int:
    """计算职位范围确认卡要展示的实际公司数。

    新 payload 会保存展开后的 ``companies``，但旧/手工构造的 payload 可能只保留
    ``pairs``；两者都按去重后的真实公司键计数，绝不把 ``*`` 当成一家公司的标签。
    """
    raw_companies = payload.get("companies")
    values: list[str] = []
    if isinstance(raw_companies, (list, tuple, set)):
        values.extend(
            value.strip() for value in raw_companies if isinstance(value, str) and value.strip()
        )
    if not values:
        pairs = payload.get("pairs")
        if isinstance(pairs, (list, tuple)):
            for pair in pairs:
                if isinstance(pair, (list, tuple)) and pair and isinstance(pair[0], str):
                    company = pair[0].strip()
                    if company:
                        values.append(company)
    return len(dict.fromkeys(value for value in values if value != "*"))


def _permission_scope_block(
    pending: PendingAction,
    *,
    company_label: str | None = None,
    metric_label: str | None = None,
) -> str:
    """确认卡/终态卡正文里的"范围+原因"多行区块（含结尾换行）。

    非本地权限动作（``suspend``/``resume``）返回空字符串——这类动作结构上就没有
    范围概念，空字符串不产生多余空行。本地权限三类动作的 payload 解析失败时不
    静默返回空字符串（会让管理员误以为不涉及具体范围），改为渲染
    :data:`_SCOPE_UNAVAILABLE_TEXT` 显式降级提示。``company_label``/
    ``metric_label`` 缺省时退回 payload 里的原始 ID；收回额外插入一行
    "方向：..."（见 :func:`_direction_prefix`）。
    """
    if pending.action_type not in LOCAL_PERMISSION_ACTION_TYPES:
        return ""
    payload = _permission_payload(pending)
    if payload is None:
        return _SCOPE_UNAVAILABLE_TEXT
    if payload.get("position_name"):
        role = payload.get("position_name", "")
        scope = payload.get("company_scope", "")
        company_count = _position_company_count(payload)
        scope_display = (
            f"全部（{company_count} 家公司）"
            if scope == "*"
            else (company_label if company_label is not None else scope)
        )
        reason = payload.get("reason", "")
        return (
            f"职位：{role}\n公司范围：{scope_display}\n"
            f"{_reused_pairs_line(payload)}原因：{reason}\n"
        )
    company_display = company_label if company_label is not None else payload.get("company_id", "")
    metric_display = metric_label if metric_label is not None else payload.get("metric_name", "")
    reason = payload.get("reason", "")
    return (
        f"{_direction_prefix(payload)}"
        f"范围：公司 {company_display} · 指标 {metric_display}\n原因：{reason}\n"
    )


def _reused_pairs_line(payload: Mapping[str, Any]) -> str:
    """差集补齐后的一行「本次新增 N 项、已有 M 项沿用」（含结尾换行）。

    只在准备阶段确实沿用了已持有项（``reused_count`` > 0）时出现；整笔都是新项的
    payload 没有这个键或值为 0，卡片正文逐字节不变。``N`` 直接数 ``pairs``——它就是
    确认后真正会写入的行数，不另存一份可能漂移的数字。
    """
    reused = payload.get("reused_count")
    if not isinstance(reused, int) or isinstance(reused, bool) or reused <= 0:
        return ""
    pairs = payload.get("pairs")
    added = len(pairs) if isinstance(pairs, (list, tuple)) else 0
    return f"本次新增 {added} 项、已有 {reused} 项沿用\n"


#: 撤销回执里银河来源回显的两种措辞。``galaxy_retained`` 由准备阶段现算并随
#: payload 持久化：整数是「撤掉之后经银河仍持有几项」，``None`` 是当时读不到银河
#: 来源——两者必须分开说，把读不到写成 0 项会让管理员误以为用户从此查不到。
_GALAXY_RETAINED_TEXT = "该用户经银河来源仍持有其中 {count} 项"
_GALAXY_UNREADABLE_TEXT = "银河来源暂不可读"


def describe_galaxy_retention(pending: PendingAction) -> str | None:
    """撤销动作的银河来源回显句；不是撤销、payload 不可用或没有这项回显时返回 ``None``。

    供发起人终态卡（``card_callback._outcome_text``）与管理群广播
    （:func:`_group_outcome_text`）共用同一句，两处不允许出现不同说法。
    """
    if pending.action_type is not PendingActionType.LOCAL_PERMISSION_REVOKE:
        return None
    payload = _permission_payload(pending)
    if payload is None or "galaxy_retained" not in payload:
        return None
    retained = payload["galaxy_retained"]
    if isinstance(retained, int) and not isinstance(retained, bool):
        return _GALAXY_RETAINED_TEXT.format(count=retained)
    return _GALAXY_UNREADABLE_TEXT


def _permission_scope_suffix(
    pending: PendingAction,
    *,
    company_label: str | None = None,
    metric_label: str | None = None,
) -> str:
    """管理群通知（单行文案）里的范围后缀。

    非本地权限动作返回空字符串。收回的后缀额外带上方向（同上，见
    :func:`_direction_prefix`）。``company_label``/``metric_label`` 同
    :func:`_permission_scope_block`。
    """
    payload = _permission_payload(pending)
    if payload is None:
        return ""
    if payload.get("position_name"):
        role = payload.get("position_name", "")
        scope = payload.get("company_scope", "")
        company_count = _position_company_count(payload)
        scope_display = (
            f"全部（{company_count} 家公司）"
            if scope == "*"
            else (company_label if company_label is not None else scope)
        )
        reason = payload.get("reason", "")
        if len(reason) > _GROUP_REASON_PREVIEW_LENGTH:
            reason = reason[:_GROUP_REASON_PREVIEW_LENGTH] + "…"
        return f"（职位 {role} · 公司范围 {scope_display} · 原因 {reason}）"
    company_display = company_label if company_label is not None else payload.get("company_id", "")
    metric_display = metric_label if metric_label is not None else payload.get("metric_name", "")
    reason = payload.get("reason", "")
    # 截断长度与管理卡的覆盖行回显统一：群通知是多人可见的广播面，不应该比
    # "只发起人一人可见"的确认卡私聊正文（_permission_scope_block，不截断）更宽松。
    if len(reason) > _GROUP_REASON_PREVIEW_LENGTH:
        reason = reason[:_GROUP_REASON_PREVIEW_LENGTH] + "…"
    direction = payload.get("direction")
    direction_infix = f"{_DIRECTION_LABEL.get(direction, direction)} · " if direction else ""
    return f"（{direction_infix}公司 {company_display} · 指标 {metric_display} · 原因 {reason}）"


#: 卡片按钮点击回传的 ``decision`` 取值，与
#: ``adapters/feishu_events.parse_card_action_event``/``core/admin/card_callback``
#: 三处共用同一对字面量。
DECISION_CONFIRM = "confirm"
DECISION_CANCEL = "cancel"


@dataclass(frozen=True)
class ConfirmCardButton:
    """确认卡上一个可点击按钮的展示文本与回传值。"""

    label: str
    #: 绑定在按钮上的回传值；``card.action.trigger`` 事件的 ``action.value`` 原样
    #: 带回这个 mapping。只放 ``pending_action_id`` 与 ``decision`` 两个字段——
    #: 不带凭据、不带目标资料。
    value: Mapping[str, str]


@dataclass(frozen=True)
class RenderedConfirmCard:
    """一张确认卡片的展示内容。``buttons`` 为空元组表示终态卡片（不可再操作）。

    ``tone`` 为 ``None`` 是卡片化之前的原版式（标题加粗写在正文首行）；有值时标题
    进卡片标题栏、按色调着色。
    """

    title: str
    body: str
    buttons: tuple[ConfirmCardButton, ...]
    tone: NoticeTone | None = None

    @property
    def is_terminal(self) -> bool:
        """没有任何按钮即视为终态卡片。"""
        return not self.buttons


def render_card_payload(card: RenderedConfirmCard) -> dict[str, Any]:
    """把 :class:`RenderedConfirmCard` 编码成 CardKit JSON 2.0 的 ``data`` 载荷。

    不 import 飞书 SDK，供真实出站发送、回调应答两个调用点复用。两个按钮横排
    进一个 ``column_set`` 容器，不套已被真实 CardKit 拒绝的
    ``{"tag": "action", "actions": [...]}``。回调形态用 ``"behaviors":
    [{"type": "callback", "value": {...}}]``，按钮不在 ``form`` 容器内不需要
    ``name`` 字段。``buttons`` 为空（终态卡片）时只有一个 markdown 元素。
    """
    head = card.body if card.tone is not None else f"**{card.title}**\n\n{card.body}"
    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": head}]
    if card.buttons:
        buttons = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": button.label},
                "type": "primary" if button.value.get("decision") == "confirm" else "default",
                "behaviors": [
                    {
                        "type": "callback",
                        # 按钮回传值：飞书文档标注 behaviors 里的 value 会原样出现在
                        # card.action.trigger 事件的 action.value 字段（见
                        # adapters/feishu_events.parse_card_action_event）。
                        "value": dict(button.value),
                    }
                ],
            }
            for button in card.buttons
        ]
        elements.append(button_row(buttons))
    payload: dict[str, Any] = {
        "schema": "2.0",
        "config": {"update_multi": True},
        "body": {"elements": elements},
    }
    if card.tone is not None:
        payload["header"] = {
            "title": {"tag": "plain_text", "content": card.title},
            "template": _HEADER_TEMPLATES[card.tone],
        }
    return payload


@dataclass(frozen=True)
class AdminCardCreated:
    """建卡并作为消息发出后的结果。

    与 ``core.execution.card_stream.CardCreated`` 同一形状（``card_id`` 用于
    后续更新，``message_id`` 是可回读标识）。
    """

    card_id: str
    message_id: str


#: **有意不按 N818 改名为 ``...Error`` 后缀**：``ConfirmCardDispatcher`` 的失败
#: 审计把 ``type(error).__name__`` 原样记进审计字段，``SendFailureAuditTests``
#: 断言这个字段等于字面量 ``"AdminCardDeliveryRejected"``——改名会让审计
#: 字段的取值静默改变，是一次真实的行为变化，不是纯移动式重构允许的范围；
#: 宁可登记一条已知的 N818 违规，也不做隐性行为改动。
class AdminCardDeliveryRejected(Exception):
    """服务端已经给出完整响应、并以明确的业务错误码拒绝这次外发——不是"结果不明"。

    与 ``core.execution.card_stream.DeliveryRejectedError`` 同一白名单纪律：真实
    adapter 只在能读到 ``code``/``msg`` 的明确拒绝响应时才抛出它；其余异常（网络类、
    JSON 解析失败、响应缺可回读标识）一律不属于这个类型，落进调用方的"结果不明"
    分支（待确认操作因此保持 ``card_delivered=False``，视为作废，不冒险当成已送达）。
    """

    def __init__(
        self, message: str = "", *, code: int | str | None = None, log_id: str | None = None
    ) -> None:
        """``code``/``log_id`` 是服务端明确拒绝时可回读的诊断字段，可选。"""
        self.code = code
        self.message = message
        self.log_id = log_id
        super().__init__(message or f"服务端明确拒绝：code={code} log_id={log_id}")


class AdminCardTransport(Protocol):
    """确认卡片的出站端口。

    真实实现见 ``adapters/feishu_admin_card.LarkAdminCardTransport``；测试
    注入内存假实现。
    """

    def create(
        self,
        *,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
        card: RenderedConfirmCard,
    ) -> AdminCardCreated:
        """建一张确认卡并作为消息发出。"""

    def update(self, *, card_id: str, sequence: int, card: RenderedConfirmCard) -> None:
        """按持久 sequence 把确认卡刷新成新内容。"""


class GroupNotifier(Protocol):
    """管理群通知端口。

    真实实现是既有 ``adapters.feishu_group_message.FeishuGroupMessages.
    send_text``——本 Protocol 只声明调用方实际用到的这一个方法，不要求注入
    完整的 ``FeishuGroupMessages`` 类型。
    """

    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None:
        """发一条管理群文本通知。"""


def render_confirm_card(
    pending: PendingAction,
    *,
    target_label: str,
    company_label: str | None = None,
    metric_label: str | None = None,
    content_catalog: ContentCatalog | None = None,
) -> RenderedConfirmCard:
    """建卡时的初始展示：动作、目标、影响范围与有效期（卡片键进目录时改用分区版式）。

    ``target_label`` 必须已是姓名+邮箱：调用方经
    ``AdminDisplayNames.user_label`` 解析 ``pending.target_open_id`` 得到——
    全部 5 类管理写操作（含收回，``target_open_id`` 由 ``prepare()`` 按
    override_id 查出真正的目标用户 open_id 后写回）都不再展示原始 open_id。
    ``company_label``/``metric_label`` 经 :func:`permission_scope_ids` 解析，
    缺省时退回 payload 原始 ID（兼容未接线的调用点）。本地权限三类动作额外
    插入一行"范围+原因"；撤销再多一行"方向"，不回显其余任何权限内容。
    """
    action_label = _ACTION_LABEL[pending.action_type]
    ttl_minutes = PENDING_ACTION_TTL_SECONDS // 60
    buttons = _confirm_buttons(pending.id)
    notice = _dynamic_notice(content_catalog, CONFIRM_CARD_KEY, action=action_label)
    if notice is not None:
        fields = [f"**操作对象**：{escape_markdown(target_label)}"]
        fields += _scope_field_lines(
            pending, company_label=company_label, metric_label=metric_label, preview=False
        )
        fields += [
            # 卡片版里范围写在影响之前，原句的「下方」随之改成「上述」。
            f"**影响**：{_IMPACT_TEXT[pending.action_type].replace('下方', '上述')}",
            f"**本次确认有效期**：发起后 {ttl_minutes} 分钟内，过期后需重新查询并发起",
        ]
        return RenderedConfirmCard(
            title=notice.title,
            body="\n".join(fields) + f"\n\n{notice.body}",
            buttons=buttons,
            tone=NoticeTone.ATTENTION,
        )
    body = (
        f"动作：{action_label}用户\n"
        f"目标：{target_label}\n"
        f"{_permission_scope_block(pending, company_label=company_label, metric_label=metric_label)}"
        f"影响：{_IMPACT_TEXT[pending.action_type]}\n"
        f"有效期：{ttl_minutes} 分钟内有效，过期后需重新查询并发起。"
    )
    return RenderedConfirmCard(title=f"待确认：{action_label}用户", body=body, buttons=buttons)


def _confirm_buttons(pending_action_id: str) -> tuple[ConfirmCardButton, ...]:
    """确认 / 取消两个按钮；回传值只带待确认操作 ID 与决定，两种版式共用。"""
    return (
        ConfirmCardButton(
            label="确认执行",
            value={"pending_action_id": pending_action_id, "decision": DECISION_CONFIRM},
        ),
        ConfirmCardButton(
            label="取消",
            value={"pending_action_id": pending_action_id, "decision": DECISION_CANCEL},
        ),
    )


def _scope_display(payload: Mapping[str, Any], company_label: str | None) -> str:
    """职位范围授权的「公司范围」展示：通配写实际公司数，其余优先用人性化标签。"""
    scope = payload.get("company_scope", "")
    if scope == "*":
        return f"全部（{_position_company_count(payload)} 家公司）"
    return company_label if company_label is not None else str(scope)


def _preview(reason: object, preview: bool) -> str:
    """管理群广播截断原因到与管理卡一致的长度；发起人私聊卡不截断。"""
    text = str(reason)
    if preview and len(text) > _GROUP_REASON_PREVIEW_LENGTH:
        return text[:_GROUP_REASON_PREVIEW_LENGTH] + "…"
    return text


def _scope_field_lines(
    pending: PendingAction,
    *,
    company_label: str | None,
    metric_label: str | None,
    preview: bool,
) -> list[str]:
    """卡片版「操作范围 / 原因」字段行；非本地权限动作没有范围，返回空列表。

    与原版 :func:`_permission_scope_block` 同一组数据与降级：payload 不可用时显式写
    「范围信息不可用」，不静默省略。所有动态值（含管理员填写的原因）逐个转义。
    """
    if pending.action_type not in LOCAL_PERMISSION_ACTION_TYPES:
        return []
    payload = _permission_payload(pending)
    if payload is None:
        return ["**操作范围**：范围信息不可用，请取消本卡重新发起"]
    esc = escape_markdown
    if payload.get("position_name"):
        lines = [
            f"**操作范围**：职位 {esc(payload.get('position_name', ''))}"
            f" · 公司范围 {esc(_scope_display(payload, company_label))}"
        ]
        reused = _reused_pairs_line(payload).strip()
        if reused:
            lines.append(f"**沿用**：{reused}")
    else:
        company = company_label if company_label is not None else payload.get("company_id", "")
        metric = metric_label if metric_label is not None else payload.get("metric_name", "")
        lines = [f"**操作范围**：公司 {esc(company)} · 指标 {esc(metric)}"]
        direction = payload.get("direction")
        if direction:
            lines.append(f"**撤销的是**：{esc(_DIRECTION_LABEL.get(direction, direction))}")
    lines.append(f"**原因**：{esc(_preview(payload.get('reason', ''), preview))}")
    return lines


def render_terminal_card(
    pending: PendingAction,
    *,
    target_label: str,
    outcome_text: str,
    company_label: str | None = None,
    metric_label: str | None = None,
    content_catalog: ContentCatalog | None = None,
) -> RenderedConfirmCard:
    """终态更新：不再带任何按钮（合同"更新为不可再次操作的最终状态"）。

    本地权限三类动作同样带上"范围+原因"（撤销再多一行"方向"）这一行，与
    确认卡同一姿态（见 ``render_confirm_card``；``target_label``/
    ``company_label``/``metric_label`` 缺省时退回原始 ID）。卡片键
    :data:`TERMINAL_CARD_KEY` 进目录时改用通知卡片版式：标题直写终态，停用 / 恢复
    写账号结果而不是「权限正在下发」。
    """
    action_label = _ACTION_LABEL[pending.action_type]
    notice = _dynamic_notice(
        content_catalog,
        TERMINAL_CARD_KEY,
        action=action_label,
        outcome=_outcome_headline(pending),
    )
    if notice is not None:
        fields = [f"**操作对象**：{escape_markdown(target_label)}"]
        fields += _scope_field_lines(
            pending, company_label=company_label, metric_label=metric_label, preview=False
        )
        fields.append(f"**结果**：{_notice_result(pending, outcome_text)}")
        return RenderedConfirmCard(
            title=notice.title,
            body="\n".join(fields) + f"\n\n{notice.body}",
            buttons=(),
            tone=_outcome_tone(pending),
        )
    scope_block = _permission_scope_block(
        pending, company_label=company_label, metric_label=metric_label
    )
    body = f"目标：{target_label}\n{scope_block}结果：{outcome_text}"
    return RenderedConfirmCard(title=f"{action_label}用户 · 已结束", body=body, buttons=())


#: 群通知 ``reason`` 渲染前的形状白名单：``pending_action.reason`` 结构上只应
#: 是固定 snake_case 字面量，但本模块拿到的只是数据库读出来的一个字符串，没有
#: 类型系统或 CHECK 约束保证它恒为这个形状，未来改动可能意外把姓名、邮箱、
#: open_id 或原始异常文本写进这一列。不匹配的值一律归入中性文案——这不是"这类
#: 值现在会出现"的证据，是"即使出现也不会泄露"的结构性保证。
_REASON_PATTERN = re.compile(r"[a-z0-9_]{1,64}")


def _safe_reason(reason: str | None) -> str:
    if isinstance(reason, str) and _REASON_PATTERN.fullmatch(reason):
        return reason
    return "other"


#: ``pending_action.reason`` 机器码 → 管理员/群通知都看得懂的中文。终态卡片/
#: 群通知展示持久化状态（不随点击者是谁而变化），不能直接沿用
#: ``decide_confirm``/``decide_cancel`` 那句只对"这次点击"有意义的
#: ``ConfirmDecision.message``——幂等重放时会文不对题。这里单独维护一份词表，
#: 未知取值归入通用占位，不直出任何未登记的内部字面量。
_FAILED_REASON_TEXT: dict[str, str] = {
    "role_revoked": "发起人当时角色已被撤销",
    "target_drifted": "目标状态已发生变化",
    "card_send_failed": "确认卡片发送失败",
    "other": "内部原因",
}


def describe_failed_reason(reason: str | None) -> str:
    """把 ``pending.reason``（``FAILED`` 终态的机器码）翻译成中文。

    供 ``card_callback._outcome_text``（发起人私聊终态卡）与
    :func:`_group_outcome_text`（管理群广播）共用同一份词表——同一个失败
    原因不允许两处出现不同的中文说法。
    """
    return _FAILED_REASON_TEXT.get(_safe_reason(reason), _FAILED_REASON_TEXT["other"])


def _group_outcome_text(pending: PendingAction) -> str:
    """群通知专用的终态文案。

    与 ``core/admin/card_callback._outcome_text`` 同一组状态分支，但
    ``FAILED`` 分支的 ``reason`` 经过上面的形状白名单——群消息是多人可见的
    广播面，风险面比只发给发起管理员本人的私聊终态卡片更大，因此单独在这里
    收窄，不影响展示给发起人本人的原始 reason。
    """
    if pending.status is PendingActionStatus.EXECUTED:
        payload = _permission_payload(pending)
        recorded = "操作已记录，权限正在下发"
        if payload is None or not payload.get("position_name"):
            recorded = f"已确认执行；{recorded}"
        retention = describe_galaxy_retention(pending)
        return f"{recorded}；{retention}" if retention else recorded
    if pending.status is PendingActionStatus.CANCELLED:
        return "已取消"
    if pending.status is PendingActionStatus.EXPIRED:
        return "已过期，未执行"
    if pending.status is PendingActionStatus.FAILED:
        return f"未执行（{describe_failed_reason(pending.reason)}）"
    return "状态未知"  # pragma: no cover - 调用点已确保 status 非 PENDING


def render_group_notice(
    pending: PendingAction,
    *,
    target_label: str,
    company_label: str | None = None,
    metric_label: str | None = None,
) -> str:
    """管理群终态广播正文。

    显示被操作用户身份与可读内容（动作/公司/指标中文名 + 结果），不再显示
    待确认操作内部 ID，群里也没有任何按钮、命令提示或可执行入口。终态文案由
    :func:`_group_outcome_text` 内部计算，确保 ``FAILED`` 分支经过形状白
    名单。本地权限三类动作额外带范围后缀，撤销场景额外带方向文案；管理员
    填写的 ``reason`` 是自由文本，不做形状白名单。
    """
    action_label = _ACTION_LABEL[pending.action_type]
    scope_suffix = _permission_scope_suffix(
        pending, company_label=company_label, metric_label=metric_label
    )
    return f"管理操作：{action_label} {target_label}{scope_suffix} · {_group_outcome_text(pending)}"


#: 账号类动作执行后的真实结果：停用 / 恢复在确认事务里就改了账号状态，不能混称
#: 「权限正在下发」。
_ACCOUNT_RESULT: dict[PendingActionType, str] = {
    PendingActionType.SUSPEND_USER: "账号已停用",
    PendingActionType.RESUME_USER: "账号已恢复",
}


def _outcome_headline(pending: PendingAction) -> str:
    """卡片标题里的一句终态；「已记录」只说正在下发，不说已下发、已生效或可用。"""
    status = pending.status
    if status is PendingActionStatus.EXECUTED:
        if pending.action_type in _ACCOUNT_RESULT:
            return _ACCOUNT_RESULT[pending.action_type]
        if pending.action_type is PendingActionType.INNERTEST_ADDITIONS:
            return "已确认 · 资格与开通结果待逐人查询"
        return "已记录 · 权限正在下发"
    if status is PendingActionStatus.CANCELLED:
        return "已取消"
    if status is PendingActionStatus.EXPIRED:
        return "已过期，未执行"
    if status is PendingActionStatus.FAILED:
        return "未执行"
    return "状态未知"


def _outcome_tone(pending: PendingAction) -> NoticeTone:
    """终态色调：账号动作完成为绿，已记录待下发为蓝，取消 / 过期为橙，失败为红。"""
    status = pending.status
    if status is PendingActionStatus.EXECUTED:
        return NoticeTone.DONE if pending.action_type in _ACCOUNT_RESULT else NoticeTone.PROCESSING
    if status is PendingActionStatus.FAILED:
        return NoticeTone.FAILURE
    return NoticeTone.ATTENTION


def _notice_result(pending: PendingAction, outcome_text: str) -> str:
    """卡片「结果」字段：账号动作改写成账号结果，其余沿用调用方算好的终态文案。"""
    if pending.status is PendingActionStatus.EXECUTED and pending.action_type in _ACCOUNT_RESULT:
        return _ACCOUNT_RESULT[pending.action_type]
    return outcome_text


@dataclass(frozen=True)
class GroupNotice:
    """一条管理群通知：``text`` 是今天的纯文本；``card`` 为 ``None`` 表示只发文本。

    有卡片时 ``text`` 就是卡片的等价纯文本，飞书明确拒绝卡片时由出口补发它。
    """

    text: str
    card: NoticeCard | None = None


def build_group_notice(
    pending: PendingAction,
    *,
    target_label: str,
    company_label: str | None = None,
    metric_label: str | None = None,
    content_catalog: ContentCatalog | None = None,
) -> GroupNotice:
    """管理群终态广播：卡片键 :data:`GROUP_NOTICE_CARD_KEY` 未进目录时只有原文本。

    卡片没有按钮、链接或回调；对象、范围与管理员填写的原因逐个转义，原因沿用群
    广播的截断长度；``FAILED`` 分支沿用 :func:`_group_outcome_text` 的形状白名单。
    """
    text = render_group_notice(
        pending, target_label=target_label, company_label=company_label, metric_label=metric_label
    )
    return group_notice(
        pending,
        text=text,
        target_label=target_label,
        company_label=company_label,
        metric_label=metric_label,
        content_catalog=content_catalog,
    )


def group_notice(
    pending: PendingAction,
    *,
    text: str,
    target_label: str,
    company_label: str | None = None,
    metric_label: str | None = None,
    content_catalog: ContentCatalog | None = None,
) -> GroupNotice:
    """给已渲染好的群广播纯文本 ``text`` 配上卡片（卡片键进目录时）。

    先查目录再读 ``pending``：卡片键未进目录时原样返回纯文本，不触碰其余字段。
    """
    source = content_catalog if content_catalog is not None else default_content_catalog()
    if not source.has_card(GROUP_NOTICE_CARD_KEY):
        return GroupNotice(text=text)
    notice = _dynamic_notice(
        source,
        GROUP_NOTICE_CARD_KEY,
        action=_ACTION_LABEL[pending.action_type],
        outcome=_outcome_headline(pending),
    )
    if notice is None:
        return GroupNotice(text=text)
    fields = [f"**对象**：{escape_markdown(target_label)}"]
    fields += _scope_field_lines(
        pending, company_label=company_label, metric_label=metric_label, preview=True
    )
    result = _notice_result(pending, _group_outcome_text(pending))
    sections = [(None, tuple(fields)), ("结果", (result,)), ("下一步", (notice.body,))]
    return GroupNotice(
        text=text, card=_notice_card_or_none(notice.title, _outcome_tone(pending), sections, text)
    )


def _notice_card_or_none(
    title: str, tone: NoticeTone, sections: Sequence[tuple[str | None, tuple[str, ...]]], text: str
) -> NoticeCard | None:
    """构造通知卡；本地校验不通过（例如转义后仍像链接）时退回纯文本，不丢这条通知。"""
    try:
        return NoticeCard(title=title, tone=tone, sections=tuple(sections), fallback_text=text)
    except ValueError as error:
        logger.warning("通知卡片构造未通过校验，改发纯文本 error=%s", error)
        return None


def deliver_group_notice(
    notifier: Any, *, chat_id: str, notice: GroupNotice, dedupe_key: str
) -> None:
    """有卡片且出口支持 ``send_notice`` 就发卡片，否则按今天的方式发纯文本。

    去重键两条路径相同；卡片的「明确拒绝才回落一次、结果不明上抛不补发」由出口的
    ``send_notice`` 负责。只会记日志的出口（未配置管理群）没有 ``send_notice``，照发文本。
    """
    send_notice = getattr(notifier, "send_notice", None)
    if notice.card is not None and callable(send_notice):
        send_notice(chat_id=chat_id, card=notice.card, dedupe_key=dedupe_key)
        return
    notifier.send_text(chat_id=chat_id, text=notice.text, dedupe_key=dedupe_key)


def correction_summary_notice(
    *, count: int, content_catalog: ContentCatalog | None = None
) -> GroupNotice:
    """每日权限补齐汇总（管理群）：有原文本键，按用户侧同一套「文本键配卡片键」规则配卡。

    卡片键未进目录、或该文本键未登记色调时只有原文本；等价纯文本就是原文本。
    """
    source = content_catalog if content_catalog is not None else default_content_catalog()
    content = source.text(CORRECTION_SUMMARY_TEXT_KEY, count=count)
    return GroupNotice(text=content.text, card=build_notice_card(source, content, {"count": count}))


def render_innertest_confirm_card(
    pending_action_id: str,
    people: Sequence[tuple[str, str | None]],
    *,
    content_catalog: ContentCatalog | None = None,
) -> RenderedConfirmCard:
    """内测入组确认卡（发给发起人本人）：``people`` 是本批待加入的（邮箱, 人员编号）。

    卡片键 :data:`INNERTEST_CONFIRM_CARD_KEY` 未进目录时正文逐字沿用原版；进目录后
    分区展示，并写明入组不授业务权限、开通结果逐人查询，不把入组写成已开通。
    """
    buttons = _confirm_buttons(pending_action_id)
    unresolved = any(personnel is None for _, personnel in people)
    notice = _dynamic_notice(content_catalog, INNERTEST_CONFIRM_CARD_KEY)
    if notice is None:
        body = f"加入内测资格，不授业务权限；资格与开通结果逐人查询。\n本批 {len(people)} 项：\n"
        body += "\n".join(
            f"{email}（{personnel or '待执行阶段解析'}）" for email, personnel in people
        )
        if unresolved:
            body += "\n待解析项仅在执行时确认唯一在职身份后加入资格；未知或冲突不加入。"
        body += "\n请本人在10分钟内确认；取消不新增资格。"
        return RenderedConfirmCard(title="确认加入内测资格", body=body, buttons=buttons)
    lines = ["**影响**：只加入内测资格，不授业务权限；资格与开通结果需逐人查询"]
    lines.append(f"**本批**：{len(people)} 项")
    lines += [
        f"- {escape_markdown(email)}（{escape_markdown(personnel or '待执行阶段解析')}）"
        for email, personnel in people
    ]
    if unresolved:
        lines.append("待解析项仅在执行时确认唯一在职身份后加入资格；未知或冲突不加入")
    lines.append(f"**本次确认有效期**：发起后 {PENDING_ACTION_TTL_SECONDS // 60} 分钟内")
    return RenderedConfirmCard(
        title=notice.title,
        body="\n".join(lines) + f"\n\n{notice.body}",
        buttons=buttons,
        tone=NoticeTone.ATTENTION,
    )
