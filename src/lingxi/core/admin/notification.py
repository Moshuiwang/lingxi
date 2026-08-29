"""确认卡片与管理群终态通知的展示层：纯函数渲染 + 卡片出站 Protocol。

只负责"展示什么"，不负责"怎么发出去"：真实 CardKit 调用住在
``adapters/feishu_admin_card.py``，真实群消息复用既有
``adapters/feishu_group_message.FeishuGroupMessages``。本模块因此不 import 任何飞书
SDK，可以在没有装 SDK 的环境里测试全部渲染断言。

## 为什么不复用 ``core/execution/card_stream.RenderedCard``

那个类型是流式问数结果卡片的展示形状（``button_labels`` 只是纯展示文案，不支持按钮
各自绑定回调值）。确认卡的两个按钮必须分别携带 ``pending_action_id`` 与 ``decision``
（``"confirm"``/``"cancel"``）供 ``card.action.trigger`` 回调识别是哪一个待确认操作、
点的是哪个按钮——这是 ``RenderedCard`` 结构上不支持的新形状，因此为确认卡新增一个
平行的最小类型，而不是给业务结果卡塞进它不需要的字段。出站的"发送失败按明确错误/
结果不明两类"判别方式、按 Protocol 注入换取可测试性这些**做法**仍然完全复用
（``adapters/feishu_admin_card.py`` 与 ``adapters/feishu_delivery.py`` 同构）。

## 本模块生成的文案不经过 ``config/content.toml``

与 ``core/admin/router.py`` 的既有取舍相同：确认卡与管理群通知只面向已登记管理员，
内容随待确认操作动态变化（目标标识、有效期、终态文案），不适合模板变量的强匹配
版本约束。管理群的既有花名册/内测日报（``core/identity/roster_report.py``、
``core/daily_report.py``）同样用纯 Python 函数渲染，不经过 content.toml——本模块与
这两处是同一惯例，不是新例外。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from lingxi.core.admin.pending_action import (
    LOCAL_PERMISSION_ACTION_TYPES,
    PENDING_ACTION_TTL_SECONDS,
    PendingAction,
    PendingActionStatus,
    PendingActionType,
)

_ACTION_LABEL: dict[PendingActionType, str] = {
    PendingActionType.SUSPEND_USER: "停用",
    PendingActionType.RESUME_USER: "恢复",
    PendingActionType.LOCAL_PERMISSION_GRANT: "授权",
    PendingActionType.LOCAL_PERMISSION_SUPPRESS: "抑制",
    PendingActionType.LOCAL_PERMISSION_REVOKE: "收回",
}

#: 零银河权限用户的本地授权边界提示（#319 动机场景，Trace #328 opus 审查 P1）
#: **已随 PM 2026-08-29 裁定（Issue #419）撤销**：四源合并不再挂在 `aggregate.
#: granted` 判据之后——零银河权限用户的本地授权现在无条件参与合并（下一轮重算或
#: 下一次开通链会把它发布出去，见 `permission_refresh.py`/`onboarding_runner.py`
#: 各自新增的分支），因此"暂不生效"这句提示已经不实，直接删除，不再改成条件性
#: 文案（判定这一刻是否零银河需要额外一次聚合查询，成本与确认卡渲染的判定层级
#: 不匹配，且删除后不再需要判定）。历史常量名与位置如实保留在这条注释里，供以后
#: 检索这段沿革。

_IMPACT_TEXT: dict[PendingActionType, str] = {
    PendingActionType.SUSPEND_USER: "该用户将立即无法发起新的问数或开通；已在进行中的任务不受影响、正常完成交付。",
    PendingActionType.RESUME_USER: "该用户将恢复可以正常问数；此前被收回的权限不会自动恢复。",
    PendingActionType.LOCAL_PERMISSION_GRANT: (
        "该用户将获得下方指定公司×指标的问数权限（本地授权，独立于银河翻译结果，"
        "不影响其余已有权限）。"
    ),
    PendingActionType.LOCAL_PERMISSION_SUPPRESS: "该用户将被限制访问下方指定公司×指标（本地抑制优先级最高，即使银河翻译结果授予也会被拦截）。",
    PendingActionType.LOCAL_PERMISSION_REVOKE: "下方指定的本地覆盖行将被收回，不再影响该用户的权限聚合结果（银河翻译结果与其余本地覆盖不受影响）。",
}

#: 迁移 ``0072`` ``direction`` 列取值 → 中文展示文案，只在收回的确认卡/终态卡/
#: 群通知里出现——授权/抑制两类动作本身的 ``_ACTION_LABEL`` 已经隐式表达了方向
#: （模块文档「本地权限授权/抑制两类确认卡/终态卡/群通知的渲染扩展」），只有
#: 收回需要额外说明"被收回的是哪个方向"（卡 B 设计卡「含被收回的方向/公司/
#: 指标」）。
_DIRECTION_LABEL: dict[str, str] = {"grant": "授权", "suppress": "抑制"}


def _permission_payload(pending: PendingAction) -> dict[str, Any] | None:
    """解析 ``pending.payload``（JSON 字符串，仅本地权限三类动作非空，见迁移
    ``0073``）。解析失败或缺失时返回 ``None``——调用方据此跳过范围行的渲染，不让
    一条格式异常的历史行让整个渲染函数崩溃（本模块全程是纯函数，不允许因为一条
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
    """收回的 payload 携带 ``direction``（被收回那一行原本是授权还是抑制，见
    ``adapters/postgres_pending_action.py`` 模块文档「本地权限收回如何复用同一
    套机制」）；授权/抑制两类动作的 payload 从不携带这个键（`.get` 落空返回
    ``""``），对它们完全不改变既有渲染——这条"方向"信息只有收回才需要额外说明。
    """

    direction = payload.get("direction")
    if not direction:
        return ""
    label = _DIRECTION_LABEL.get(direction, direction)
    return f"方向：{label}\n"


#: payload 异常时的降级文案（Trace #328 opus 审查 P2）：非本地权限动作的
#: "范围+原因"区块结构上就不存在（本来就该是空字符串），但**本地权限动作**的
#: payload 解析失败（缺失、非法 JSON、非对象）此前会静默退化成同一个空字符串——
#: 管理员看到的确认卡直接漏掉"范围：公司 ... · 指标 ..."这一行，等于让他在不知道
#: 自己要授权/抑制/收回哪个公司哪个指标的情况下点确认。改成显式的降级提示，
#: 而不是让这条本该出现的信息悄悄消失。
_SCOPE_UNAVAILABLE_TEXT = "范围信息不可用，请取消本卡重新发起。\n"


def _permission_scope_block(pending: PendingAction) -> str:
    """确认卡/终态卡正文里的"范围+原因"多行区块（含结尾换行）。

    非本地权限动作（``suspend``/``resume``）返回空字符串——这类动作结构上就没有
    范围概念，调用方按行拼接，空字符串不产生多余空行。

    本地权限三类动作（授权/抑制/收回）的 payload 解析失败时**不再静默返回空
    字符串**（Trace #328 opus 审查 P2：那会让管理员在确认卡上完全看不到"范围"
    这一行，误以为这张卡不涉及具体范围，实际是数据异常被吞掉了）——改为渲染
    :data:`_SCOPE_UNAVAILABLE_TEXT` 这行显式降级提示，指引管理员取消本卡重新
    发起，而不是带着一个他没看到的范围去确认。

    正常解析成功时，收回额外插入一行"方向：..."（说明被收回的是授权还是抑制，
    卡 B 设计卡「含被收回的方向/公司/指标」），授权/抑制两类动作不受影响
    （见 :func:`_direction_prefix`）。
    """

    if pending.action_type not in LOCAL_PERMISSION_ACTION_TYPES:
        return ""
    payload = _permission_payload(pending)
    if payload is None:
        return _SCOPE_UNAVAILABLE_TEXT
    company_id = payload.get("company_id", "")
    metric_name = payload.get("metric_name", "")
    reason = payload.get("reason", "")
    return (
        f"{_direction_prefix(payload)}"
        f"范围：公司 {company_id} · 指标 {metric_name}\n原因：{reason}\n"
    )


def _permission_scope_suffix(pending: PendingAction) -> str:
    """管理群通知（单行文案）里的范围后缀；非本地权限动作返回空字符串。收回的
    后缀额外带上方向（同上，见 :func:`_direction_prefix`）。"""

    payload = _permission_payload(pending)
    if payload is None:
        return ""
    company_id = payload.get("company_id", "")
    metric_name = payload.get("metric_name", "")
    reason = payload.get("reason", "")
    direction = payload.get("direction")
    direction_infix = f"{_DIRECTION_LABEL.get(direction, direction)} · " if direction else ""
    return f"（{direction_infix}公司 {company_id} · 指标 {metric_name} · 原因 {reason}）"

#: 卡片按钮点击回传的 ``decision`` 取值，与
#: ``adapters/feishu_events.parse_card_action_event``/``core/admin/card_callback``
#: 三处共用同一对字面量。
DECISION_CONFIRM = "confirm"
DECISION_CANCEL = "cancel"


@dataclass(frozen=True)
class ConfirmCardButton:
    label: str
    #: 绑定在按钮上的回传值；``card.action.trigger`` 事件的 ``action.value`` 原样
    #: 带回这个 mapping。只放 ``pending_action_id`` 与 ``decision`` 两个字段——
    #: 不带凭据、不带目标资料。
    value: Mapping[str, str]


@dataclass(frozen=True)
class RenderedConfirmCard:
    """一张确认卡片的展示内容。``buttons`` 为空元组表示终态卡片（不可再操作）。"""

    title: str
    body: str
    buttons: tuple[ConfirmCardButton, ...]

    @property
    def is_terminal(self) -> bool:
        return not self.buttons


def render_card_payload(card: RenderedConfirmCard) -> dict[str, Any]:
    """把 :class:`RenderedConfirmCard` 编码成 CardKit JSON 2.0 的 ``data`` 载荷。

    从 ``adapters/feishu_admin_card.py`` 迁移到这里（Issue #96 卡片回调应答修复）：
    ``card.action.trigger`` 回调的应答帧需要携带同一份终态卡 JSON（见
    ``core/admin/card_callback.py`` 的 ``handle()`` 文档），而那个调用方按代码框架
    第二节不得 import ``adapters/``。载荷构造本身是纯字典拼接、不依赖任何飞书 SDK，
    移到这个不 import SDK 的展示层模块后，两个调用点（真实出站发送、回调应答）
    复用同一份实现，不允许分叉出第二份。

    按钮是 ``body.elements`` 的顶层元素，不套 ``{"tag": "action", "actions": [...]}``
    容器——后者已被真实 CardKit 拒绝（``code=200861``，见
    ``adapters/feishu_admin_card.py`` 模块文档「2026-08-25 建卡环节已被真实探针
    证伪并修复」）。回调形态用 ``"behaviors": [{"type": "callback", "value": {...}}]``
    （飞书文档《配置卡片交互》描述的 2.0 标准写法），``value`` 内容仍是
    ``pending_action_id``/``decision`` 两个键。

    ``buttons`` 为空（终态卡片）时只有一个 markdown 元素、没有任何按钮元素——这就是
    "卡片更新为不可再次操作的最终状态"在 CardKit 层面的落点：终态卡片结构上不存在
    任何可点击的按钮，不是靠禁用态按钮或前端约定"这张卡片已经不能点了"。
    """

    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": f"**{card.title}**\n\n{card.body}"}
    ]
    for button in card.buttons:
        elements.append(
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
        )
    return {"schema": "2.0", "config": {"update_multi": True}, "body": {"elements": elements}}


@dataclass(frozen=True)
class AdminCardCreated:
    """建卡并作为消息发出后的结果，与 ``core.execution.card_stream.CardCreated``
    同一形状（``card_id`` 用于后续更新，``message_id`` 是可回读标识）。"""

    card_id: str
    message_id: str


class AdminCardDeliveryRejected(Exception):
    """服务端已经给出完整响应、并以明确的业务错误码拒绝这次外发——不是"结果不明"。

    与 ``core.execution.card_stream.DeliveryRejected`` 同一白名单纪律：真实 adapter
    只在能读到 ``code``/``msg`` 的明确拒绝响应时才抛出它；其余异常（网络类、JSON
    解析失败、响应缺可回读标识）一律不属于这个类型，落进调用方的"结果不明"分支
    （待确认操作因此保持 ``card_delivered=False``，视为作废，不冒险当成已送达）。
    """

    def __init__(
        self, message: str = "", *, code: int | str | None = None, log_id: str | None = None
    ) -> None:
        self.code = code
        self.message = message
        self.log_id = log_id
        super().__init__(message or f"服务端明确拒绝：code={code} log_id={log_id}")


class AdminCardTransport(Protocol):
    """确认卡片的出站端口。真实实现见 ``adapters/feishu_admin_card.LarkAdminCardTransport``；
    测试注入内存假实现。"""

    def create(
        self,
        *,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
        card: RenderedConfirmCard,
    ) -> AdminCardCreated: ...

    def update(self, *, card_id: str, sequence: int, card: RenderedConfirmCard) -> None: ...


class GroupNotifier(Protocol):
    """管理群通知端口。真实实现是既有
    ``adapters.feishu_group_message.FeishuGroupMessages.send_text``——本 Protocol
    只声明调用方（``core/admin/card_callback.py``）实际用到的这一个方法，不要求
    注入完整的 ``FeishuGroupMessages`` 类型。"""

    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None: ...


def render_confirm_card(pending: PendingAction, *, target_label: str) -> RenderedConfirmCard:
    """建卡时的初始展示：动作、目标（回显管理员自己刚输入的标识，不引入新的资料
    披露）、影响范围与有效期。本地权限三类动作（授权/抑制/收回）额外插入一行
    "范围+原因"（公司×指标 + 管理员填写的理由，解析自 ``pending.payload``，见
    迁移 ``0073``）；收回再多一行"方向"（说明被收回的原本是授权还是抑制，卡 B
    设计卡）。不回显该用户的其余任何权限内容，只讲这一次动作本身涉及的键。"""

    action_label = _ACTION_LABEL[pending.action_type]
    ttl_minutes = PENDING_ACTION_TTL_SECONDS // 60
    body = (
        f"动作：{action_label}用户\n"
        f"目标：{target_label}\n"
        f"{_permission_scope_block(pending)}"
        f"影响：{_IMPACT_TEXT[pending.action_type]}\n"
        f"有效期：{ttl_minutes} 分钟内有效，过期后需重新查询并发起。"
    )
    return RenderedConfirmCard(
        title=f"待确认：{action_label}用户",
        body=body,
        buttons=(
            ConfirmCardButton(
                label="确认执行",
                value={"pending_action_id": pending.id, "decision": DECISION_CONFIRM},
            ),
            ConfirmCardButton(
                label="取消",
                value={"pending_action_id": pending.id, "decision": DECISION_CANCEL},
            ),
        ),
    )


def render_terminal_card(
    pending: PendingAction, *, target_label: str, outcome_text: str
) -> RenderedConfirmCard:
    """终态更新：不再带任何按钮（合同"更新为不可再次操作的最终状态"）。本地权限
    三类动作同样带上"范围+原因"（收回再多一行"方向"）这一行，与确认卡同一姿态
    （模块文档 ``render_confirm_card``）。"""

    action_label = _ACTION_LABEL[pending.action_type]
    body = f"目标：{target_label}\n{_permission_scope_block(pending)}结果：{outcome_text}"
    return RenderedConfirmCard(title=f"{action_label}用户 · 已结束", body=body, buttons=())


#: 群通知 ``reason`` 渲染前的形状白名单（外部审查交叉裁定，opus P3-8）。套用
#: ``core/daily_report.py`` 的 ``_safe_reason_code`` 同一形状与同一姿态：
#: ``pending_action.reason`` 结构上只应是 ``core/admin/pending_action.py`` 里的固定
#: snake_case 字面量（``expired``/``role_revoked``/``target_drifted``/
#: ``cancelled_by_admin``/``card_send_failed``），但本模块拿到的只是数据库读出来的
#: 一个字符串，不持有任何保证它恒为这个形状的类型系统约束——这一列没有 CHECK 约束
#: 限定取值域（迁移 ``0068``），未来任何一次改动都可能意外把姓名、邮箱、open_id 或
#: 原始异常文本写进这一列。渲染前用这个白名单兜底，不匹配的值一律归入中性文案，
#: 不让任何不符合预期形状的取值直接进入群发的正文。这不是"这类值现在会出现"的
#: 证据，是"即使出现也不会泄露"的结构性保证。
_REASON_PATTERN = re.compile(r"[a-z0-9_]{1,64}")


def _safe_reason(reason: str | None) -> str:
    if isinstance(reason, str) and _REASON_PATTERN.fullmatch(reason):
        return reason
    return "other"


def _group_outcome_text(pending: PendingAction) -> str:
    """群通知专用的终态文案。与 ``core/admin/card_callback._outcome_text`` 同一组
    状态分支，但 ``FAILED`` 分支的 ``reason`` 经过上面的形状白名单——群消息是多人
    可见的广播面，风险面比只发给发起管理员本人的私聊终态卡片更大，因此单独在这里
    收窄，不影响 ``card_callback._outcome_text`` 展示给发起人本人的原始 reason。
    """

    if pending.status is PendingActionStatus.EXECUTED:
        return "已确认执行"
    if pending.status is PendingActionStatus.CANCELLED:
        return "已取消"
    if pending.status is PendingActionStatus.EXPIRED:
        return "已过期，未执行"
    if pending.status is PendingActionStatus.FAILED:
        return f"未执行（{_safe_reason(pending.reason)}）"
    return "状态未知"  # pragma: no cover - 调用点已确保 status 非 PENDING


def render_group_notice(pending: PendingAction) -> str:
    """管理群脱敏通知正文：不含 open_id 明文、不含姓名，只含内部标识（``pac_*``）
    与结果摘要——与 ``core/identity/roster_report.py`` 用内部 ULID 而非外部平台
    标识做定位的既有取舍一致。群里没有任何按钮、命令提示或可执行入口（`V-管理-11`
    同一要求，复用 ``FeishuGroupMessages.send_text`` 本身就结构性不支持卡片）。

    终态文案由本函数内部按 :func:`_group_outcome_text` 计算（不再接受调用方传入的
    ``outcome_text``），确保 ``FAILED`` 分支的 ``reason`` 一定会经过形状白名单——
    交给调用方传入拼好的文案，等于把"这段文本安不安全群发"的判断权交还给一个不了解
    群通知安全要求的调用方（外部审查交叉裁定，opus P3-8）。

    本地权限三类动作额外带一个"（公司 ... · 指标 ... · 原因 ...）"后缀（#319
    S-P-1b：执行广播需要含公司/指标/方向/理由）——授权/抑制两类的方向已经由
    ``action_label`` 本身表达，不重复渲染；收回的 ``action_label``（"收回"）
    不能表达被收回的原本是授权还是抑制，因此后缀在收回场景下额外多带一段方向
    文案（卡 B 设计卡「含被收回的方向/公司/指标」，见 :func:`_permission_scope_
    suffix`）。仍然只讲这一次动作涉及的单一键，不回显该用户的其余任何权限内容。
    管理员填写的 ``reason`` 是自由文本，本函数不对它做形状白名单（与
    ``pending.reason`` 那条机器可读的状态码不同，这段文本来自当前唯一管理员本人
    的输入，信任级别与确认卡私聊正文相同）。
    """

    action_label = _ACTION_LABEL[pending.action_type]
    scope_suffix = _permission_scope_suffix(pending)
    return (
        f"管理操作 {pending.id}：{action_label}用户{scope_suffix} · {_group_outcome_text(pending)}"
    )
