"""把飞书原始事件体接到管线与管理卡回调上。

进程内只有这一个事件入口（`V-接入-10`）：读不懂的事件体只记审计、继续收下一条，
不抛给长连接当成连接故障（`V-接入-12`）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from lingxi.adapters.feishu_events import (
    CARD_ACTION_TRIGGER_EVENT,
    MESSAGE_RECEIVE_EVENT,
    CardActionParseError,
    EventParseError,
    NonPrivateChatError,
    parse_card_action_event,
    parse_message_event,
)
from lingxi.core.admin.management_card import (
    ADMIN_ACTION_CANCEL,
    ADMIN_ACTION_GRANT,
    ADMIN_ACTION_REVOKE,
    GRANT_SUBMIT_BUTTON_NAME,
)
from lingxi.core.conversation.pipeline import EventPipeline

def _management_card_context(payload: dict) -> tuple[str, str]:
    """从原始事件体的 ``event.context`` 里取出触发这次点击的 ``chat_id``/
    ``message_id``——管理卡表单提交/收回按钮转译成的等价命令文本要经
    ``AdminCommandRouter.route()`` 发一张**新**确认卡，需要知道回复到哪个会话、
    哪一条消息（见 ``core/admin/router.py`` ``route()`` 的 ``chat_id``/
    ``message_id`` 文档）。飞书卡片回调事件体的 ``context.open_chat_id``/
    ``context.open_message_id`` 就是这张管理卡自己所在的会话与消息（依据同一份
    飞书卡片 2.0 公开文档，证据等级同上——未经真实回调验证）。管理卡是私聊卡片，
    不涉及话题群，``thread_id`` 恒传 ``None``（与 ``route()`` 默认值一致）。
    """

    event = payload.get("event") if isinstance(payload, dict) else None
    context = event.get("context") if isinstance(event, dict) else None
    if not isinstance(context, dict):
        return "", ""
    chat_id = context.get("open_chat_id", "")
    message_id = context.get("open_message_id", "")
    return (
        str(chat_id) if isinstance(chat_id, (str, int, float)) else "",
        str(message_id) if isinstance(message_id, (str, int, float)) else "",
    )

def make_event_handler(
    pipeline: EventPipeline,
    *,
    audit: Any,
    on_parse_error: Callable[[str], None] | None = None,
    card_callback_handler: Any = None,
    group_mention_hint: Any = None,
    management_card_context_store: Any = None,
) -> Callable[[dict], dict | None]:
    """把原始事件体接到管线上。

    处理 ``im.message.receive_v1``（业务问数与管理命令面）。``card.action.trigger``
    （管理员确认卡片按钮点击，Issue #96 S-M-02）只在 ``card_callback_handler`` 被
    显式传入时才处理——未传入（``None``，例如尚未完成 gateway 接线的中间态，或
    只测试消息路径的既有用例）时行为与本参数加入之前逐字节一致：仍然只记
    ``event.ignored`` 并返回，不致长连接崩溃（`V-接入-12`）。其余类型
    （``im.chat.member.bot.deleted_v1``）本批仍不处理，同样只记 ``event.ignored``。

    ``group_mention_hint``（Issue #318，可选）：群聊越界分支（见下方
    ``NonPrivateChatError``）记完 ``event.rejected_non_private_chat`` 审计之后，
    如果传入了这个参数就调用它的 ``maybe_respond(error)``——要不要真的发那句
    固定引导由它自己判定（见 ``GroupMentionHintResponder``），本函数不做二次
    判断。未传入（``None``，例如尚未完成 gateway 接线的中间态，或只测试既有拒绝
    行为的用例）时行为与本参数加入之前逐字节一致。

    **卡片回调分支的返回值必须原样透传（Issue #96 卡片回调应答修复）**：
    ``card_callback_handler.handle(...)`` 返回的是要交给飞书的应答字典（见
    ``core/admin/card_callback.py`` 模块文档「载体 #96」），本函数把它
    ``return`` 出去，经 ``LongConnectionSupervisor._dispatch`` 一路传到 SDK。
    此前这里只调用不返回，SDK 收到空应答，飞书按"维持原卡"处理，卡片永远回弹
    为原始带按钮状态——这正是本次要修的缺陷，根因不在这一行本身（本函数从未
    尝试构造应答），而在于它把已经算好的应答值原地丢弃。普通消息事件分支
    （``pipeline.handle_message``）不返回任何东西，隐式 ``None``，行为不变。

    **管理卡表单提交/逐行收回分支（Issue #439 B 档接线）**：
    ``card.action.trigger`` 事件体的 ``action.value`` 里出现 ``admin_action``
    键（``core/admin/management_card.py`` 建卡时写进按钮回调值的那个字段）就是
    管理卡交互，不是确认/取消卡片——两者的 ``action.value`` 形状结构上不相交
    （确认/取消卡片带 ``pending_action_id``/``decision``，管理卡带
    ``admin_action``），因此可以只按这一个键的存在与取值分流，不需要额外的
    卡片来源标记：

    - ``admin_action == "revoke"``（撤销按钮）：新职位+范围项解析出
      ``permission_group_id``，历史行解析出 ``override_id``，调用
      :meth:`~lingxi.core.admin.card_callback.AdminCardCallbackHandler.
      handle_management_revoke`。
    - ``admin_action`` 是 ``"grant"``/``"suppress"``（表单提交）：额外从
      ``action_event.form_value``（``adapters/feishu_events.py`` 集中解析，见
      ``CardActionEvent`` 文档）取出 ``company_id``/``metric_name``/``reason``，
      调用 :meth:`~lingxi.core.admin.card_callback.AdminCardCallbackHandler.
      handle_management_form_submit`。
    - **``admin_action`` 缺失/空时的按钮名后备路由**（W0-1 追加结论，
      2026-08-30，真实点击实测坐实）：form 内提交按钮的真实回调
      ``action.value`` 经常不带 ``admin_action``（缺失或需要反序列化的字符串），
      这时改用按钮自己的 ``action.name``（建卡时写入的
      ``grant_submit``/``suppress_submit``）兜底判定是哪一次提交——不这样做，
      点击后会静默落进下面"未知 decision"分支，管理卡补充授权/屏蔽指标从此
      全部失效（真实点击已实测复现，见 ``adapters/feishu_events.py`` 的
      ``_parse_action_value`` 文档）。逐行「撤销」按钮不受影响——它不在 form
      内，真实回调的 ``value`` 已经带着 ``admin_action`` 正常到达。
    - 两条路径都用不到 ``admin_action`` 时（含按钮名也不认识）：不认识的
      action 维持既有兜底行为——落到下面 ``decision``/``pending_action_id``
      这条既有分支，读不到有效 ``decision`` 时由 ``handle()`` 自己的
      ``unknown_decision`` 分支拒绝，与本次改动之前完全一致。

    **表单提交 ``identifier`` 缺失时的发送侧登记恢复（Trace #469 修复包 B，
    B-1）**：按钮名兜底解决了"识别出这是哪一个提交按钮"，但没有解决
    ``action.value`` 整体缺失时 ``identifier`` 本身也一起丢失的问题——这时
    ``action_event.action_value`` 是空字典，``.get("identifier", "")`` 恒为
    空串。``management_card_context_store``（可选，未传入时行为与本参数加入
    之前逐字节一致）是发送管理卡时登记的 ``message_id -> identifier`` 内存
    TTL 映射（见 ``core/admin/card_dispatch.ManagementCardContextStore``
    模块文档）：``identifier`` 为空且这个参数不是 ``None`` 时，用
    ``_management_card_context`` 已经取出的 ``message_id``（回调事件体
    ``context.open_message_id``，与建卡成功后 ``ManagementCardCreated.
    message_id`` 是同一个值——这条消息正是管理卡自己）去查表补回；查不到
    （未登记/已过期/已被逐出容量上限）时 ``identifier`` 仍是空串，交给
    ``handle_management_form_submit`` 自己既有的必填校验给出「请重新查询
    /admin user」——此时这句指引恰好是准确操作。逐行「撤销」按钮不受影响：
    ``override_id`` 走的是另一个字段，不经过这个恢复路径。
    """

    def handle(payload: dict) -> dict | None:
        header = payload.get("header") if isinstance(payload, dict) else None
        event_type = header.get("event_type") if isinstance(header, dict) else None

        if event_type == CARD_ACTION_TRIGGER_EVENT and card_callback_handler is not None:
            try:
                action_event = parse_card_action_event(payload)
            except CardActionParseError as error:
                # 读不懂的卡片回调事件体记审计后继续收下一条，同 EventParseError
                # 姿态——不当作连接故障，也不当作任何一种业务终态。
                audit.record("event.unparsable", error=str(error))
                if on_parse_error is not None:
                    on_parse_error(str(error))
                return None
            admin_action = action_event.action_value.get("admin_action", "")
            if not admin_action:
                # 表单提交按钮名后备路由（W0-1 追加结论，2026-08-30，真实点击
                # 实测坐实）：form 内提交按钮的真实回调 action.value 经常不带
                # admin_action（缺失或需要反序列化的字符串，见
                # adapters/feishu_events.py 的 _parse_action_value 文档），但
                # 回调事件本就会带回按钮自己的 action.name
                # （grant_submit）——用它兜底识别是哪一个提交按钮，不这样做，
                # 点击后会静默落进下面"未知 decision"分支，管理卡补充授权从此
                # 全部失效（真实点击已实测复现）。表单内自 Trace #544 D-5 起只剩
                # 这一个提交按钮（「屏蔽指标」随 /admin suppress_permission 一起
                # 撤除）。逐行「撤销」按钮不需要这条后备——它不在 form 内，真实
                # 回调的 value 已经带着 admin_action 正常到达。
                if action_event.action_name == GRANT_SUBMIT_BUTTON_NAME:
                    admin_action = ADMIN_ACTION_GRANT
            if admin_action == ADMIN_ACTION_REVOKE:
                chat_id, message_id = _management_card_context(payload)
                revoke_kwargs = dict(
                    operator_open_id=action_event.operator_open_id,
                    override_id=action_event.action_value.get("override_id", ""),
                    chat_id=chat_id,
                    thread_id=None,
                    message_id=message_id,
                    trace_id=action_event.trace_id,
                )
                permission_group_id = action_event.action_value.get("permission_group_id", "")
                if permission_group_id:
                    revoke_kwargs["permission_group_id"] = permission_group_id
                return card_callback_handler.handle_management_revoke(**revoke_kwargs)
            if admin_action == ADMIN_ACTION_CANCEL:
                chat_id, message_id = _management_card_context(payload)
                identifier = action_event.action_value.get("identifier", "")
                if not identifier and management_card_context_store is not None:
                    identifier = management_card_context_store.lookup(message_id=message_id) or ""
                return card_callback_handler.handle_management_cancel(
                    operator_open_id=action_event.operator_open_id,
                    identifier=identifier,
                    chat_id=chat_id,
                    thread_id=None,
                    message_id=message_id,
                    trace_id=action_event.trace_id,
                )
            if admin_action == ADMIN_ACTION_GRANT:
                chat_id, message_id = _management_card_context(payload)
                identifier = action_event.action_value.get("identifier", "")
                if not identifier and management_card_context_store is not None:
                    # 发送侧登记恢复（Trace #469 B-1，见本函数文档该节）：
                    # value 缺失形态下 identifier 唯一的另一个来源。查不到
                    # 时 identifier 维持空串，交给下游既有必填校验拒绝。
                    identifier = (
                        management_card_context_store.lookup(message_id=message_id) or ""
                    )
                form_kwargs = {
                    "operator_open_id": action_event.operator_open_id,
                    "admin_action": admin_action,
                    "identifier": identifier,
                    "company_id": action_event.form_value.get("company_id", ""),
                    "metric_name": action_event.form_value.get("metric_name", ""),
                    "reason": action_event.form_value.get("reason", ""),
                    "chat_id": chat_id,
                    "thread_id": None,
                    "message_id": message_id,
                    "trace_id": action_event.trace_id,
                }
                position_name = action_event.form_value.get("position_name", "")
                company_scope = action_event.form_value.get("company_scope", "")
                if position_name:
                    form_kwargs["position_name"] = position_name
                if company_scope:
                    form_kwargs["company_scope"] = company_scope
                return card_callback_handler.handle_management_form_submit(
                    **form_kwargs,
                )

            # 不认识的 action（含 admin_action 缺失/空）：既有确认/取消分支，
            # 逐字节不变。
            decision = action_event.action_value.get("decision", "")
            pending_action_id = action_event.action_value.get("pending_action_id", "")
            return card_callback_handler.handle(
                operator_open_id=action_event.operator_open_id,
                pending_action_id=pending_action_id,
                decision=decision,
                trace_id=action_event.trace_id,
            )

        if event_type != MESSAGE_RECEIVE_EVENT:
            audit.record("event.ignored", event_type=event_type)
            return
        try:
            message = parse_message_event(payload)
        except NonPrivateChatError as error:
            # 群聊越界：合同「问数与多轮对话」只适用于飞书私聊入口。默认分支
            # 仍然**不加表情、不入队**——加表情本身也是一个用户可见动作，在群里
            # 给一条消息加表情等于宣告「我在这个群里工作」。是否额外回一句固定
            # 引导（Issue #318）交给 group_mention_hint 自己判定，它的失败关闭
            # 语义见 GroupMentionHintResponder。
            audit.record("event.rejected_non_private_chat", chat_type=error.chat_type)
            if group_mention_hint is not None:
                group_mention_hint.maybe_respond(error)
            return
        except EventParseError as error:
            # 读不懂的事件体记审计后继续收下一条，不抛给 supervisor 当成连接故障。
            audit.record("event.unparsable", error=str(error))
            if on_parse_error is not None:
                on_parse_error(str(error))
            return
        pipeline.handle_message(message)

    return handle
