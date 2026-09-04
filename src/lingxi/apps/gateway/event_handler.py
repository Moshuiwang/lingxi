"""把飞书原始事件体接到管线与管理卡回调上。

进程内只有这一个事件入口（`V-接入-10`）：读不懂的事件体只记审计、继续收下一条，不抛给
长连接当成连接故障（`V-接入-12`）；不处理的事件类型同样只记一条 ``event.ignored``。

**卡片回调分支的返回值必须原样透传**：回调处理器返回的是要交给平台的应答字典，这里
``return`` 出去，一路传到 SDK。曾经这里只调用不返回，SDK 收到空应答，平台按"维持原卡"
处理，卡片永远回弹为原始带按钮状态。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
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
    """从事件体里取出触发这次点击的会话与消息标识。

    管理卡的表单提交/收回按钮会被转译成等价的命令文本并发一张**新**确认卡，因此需要
    知道回复到哪个会话、哪一条消息。回调事件体里的这两个字段就是这张管理卡自己所在的
    会话与消息。管理卡是私聊卡片，不涉及话题群，话题标识恒为空。

    Returns:
        ``(chat_id, message_id)``；事件体形状不对时两个都是空串。
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


@dataclass(frozen=True)
class _EventRouter:
    """一条入站事件的分流：管理卡回调走回调处理器，私聊消息走管线。

    可选依赖留空时，行为与该能力加入之前逐字节一致：不装回调处理器则卡片事件只记
    ``event.ignored``；不装群聊引导则群聊消息只记审计后静默；不装管理卡上下文端口则
    标识缺失时不做恢复。
    """

    pipeline: EventPipeline
    audit: Any
    on_parse_error: Callable[[str], None] | None
    card_callback_handler: Any
    group_mention_hint: Any
    management_card_context_store: Any

    def handle(self, payload: dict) -> dict | None:
        """处理一条原始事件体。

        Returns:
            卡片回调分支返回要交给平台的应答字典；消息分支返回 ``None``。
        """
        header = payload.get("header") if isinstance(payload, dict) else None
        event_type = header.get("event_type") if isinstance(header, dict) else None

        if event_type == CARD_ACTION_TRIGGER_EVENT and self.card_callback_handler is not None:
            return self._handle_card_action(payload)
        if event_type != MESSAGE_RECEIVE_EVENT:
            self.audit.record("event.ignored", event_type=event_type)
            return None
        self._handle_message(payload)
        return None

    # ------------------------------------------------------------------
    # 管理卡回调
    # ------------------------------------------------------------------

    def _handle_card_action(self, payload: dict) -> dict | None:
        """卡片按钮点击：按 ``admin_action`` 分流，认不出就落回确认/取消分支。"""
        try:
            action_event = parse_card_action_event(payload)
        except CardActionParseError as error:
            self._record_unparsable(error)
            return None

        admin_action = self._resolve_admin_action(action_event)
        if admin_action == ADMIN_ACTION_REVOKE:
            return self._handle_revoke(action_event, payload)
        if admin_action == ADMIN_ACTION_CANCEL:
            return self._handle_cancel(action_event, payload)
        if admin_action == ADMIN_ACTION_GRANT:
            return self._handle_form_submit(action_event, payload)
        # 不认识的 action（含 ``admin_action`` 缺失或为空）：落回确认/取消这条既有
        # 分支，读不到有效 decision 时由回调处理器自己的未知分支拒绝。
        return self.card_callback_handler.handle(
            operator_open_id=action_event.operator_open_id,
            pending_action_id=action_event.action_value.get("pending_action_id", ""),
            decision=action_event.action_value.get("decision", ""),
            trace_id=action_event.trace_id,
        )

    @staticmethod
    def _resolve_admin_action(action_event: Any) -> str:
        """判定这次点击是哪一个管理动作；按钮回调值缺失时用按钮名兜底。

        表单内提交按钮的真实回调值经常不带这个字段（缺失，或是一个需要反序列化的
        字符串），而回调事件本就会带回按钮自己的名字。不用它兜底的话，点击会静默落进
        "未知 decision"分支，管理卡的补充授权从此全部失效——真实点击已经实测复现过。
        表单外的逐行「撤销」按钮不需要这条后备，它的回调值一直是正常的。
        """
        admin_action = action_event.action_value.get("admin_action", "")
        if not admin_action and action_event.action_name == GRANT_SUBMIT_BUTTON_NAME:
            return ADMIN_ACTION_GRANT
        return admin_action

    def _handle_revoke(self, action_event: Any, payload: dict) -> dict | None:
        """逐行「撤销」：新职位范围项按权限组撤，历史行按覆盖记录撤。"""
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
        return self.card_callback_handler.handle_management_revoke(**revoke_kwargs)

    def _handle_cancel(self, action_event: Any, payload: dict) -> dict | None:
        """管理卡「取消」：关掉这张卡。"""
        chat_id, message_id = _management_card_context(payload)
        return self.card_callback_handler.handle_management_cancel(
            operator_open_id=action_event.operator_open_id,
            identifier=self._resolve_identifier(action_event, message_id),
            chat_id=chat_id,
            thread_id=None,
            message_id=message_id,
            trace_id=action_event.trace_id,
        )

    def _handle_form_submit(self, action_event: Any, payload: dict) -> dict | None:
        """管理卡表单提交：把表单值转成一次等价的管理命令。"""
        chat_id, message_id = _management_card_context(payload)
        form_kwargs = {
            "operator_open_id": action_event.operator_open_id,
            "admin_action": ADMIN_ACTION_GRANT,
            "identifier": self._resolve_identifier(action_event, message_id),
            "company_id": action_event.form_value.get("company_id", ""),
            "metric_name": action_event.form_value.get("metric_name", ""),
            "reason": action_event.form_value.get("reason", ""),
            "chat_id": chat_id,
            "thread_id": None,
            "message_id": message_id,
            "trace_id": action_event.trace_id,
        }
        for optional in ("position_name", "company_scope"):
            value = action_event.form_value.get(optional, "")
            if value:
                form_kwargs[optional] = value
        return self.card_callback_handler.handle_management_form_submit(**form_kwargs)

    def _resolve_identifier(self, action_event: Any, message_id: str) -> str:
        """取这次操作的目标标识；回调值整体缺失时用发送侧登记补回。

        按钮名兜底只解决了"识别出这是哪一个按钮"，没有解决回调值整体缺失时目标标识
        也一起丢失的问题。发送管理卡时登记过"消息标识 → 目标"，而回调事件带回的消息
        标识正是这张管理卡自己，因此可以据此补回。补不回（未登记／已过期）时仍是空串，
        交给下游既有的必填校验给出「请重新查询」——此时这句指引恰好是准确操作。
        """
        identifier = action_event.action_value.get("identifier", "")
        if identifier or self.management_card_context_store is None:
            return identifier
        return self.management_card_context_store.lookup(message_id=message_id) or ""

    # ------------------------------------------------------------------
    # 私聊消息
    # ------------------------------------------------------------------

    def _handle_message(self, payload: dict) -> None:
        """私聊消息：解析成功就交给管线，两类解析失败各自有既定出口。"""
        try:
            message = parse_message_event(payload)
        except NonPrivateChatError as error:
            # 群聊越界：合同「问数与多轮对话」只适用于私聊入口。默认**不加表情、不入队**
            # ——加表情本身也是一个用户可见动作，在群里给一条消息加表情等于宣告「我在
            # 这个群里工作」。是否额外回一句固定引导交给群聊引导自己判定。
            self.audit.record("event.rejected_non_private_chat", chat_type=error.chat_type)
            if self.group_mention_hint is not None:
                self.group_mention_hint.maybe_respond(error)
            return
        except EventParseError as error:
            self._record_unparsable(error)
            return
        self.pipeline.handle_message(message)

    def _record_unparsable(self, error: Exception) -> None:
        """读不懂的事件体：记审计后继续收下一条，不当作连接故障也不当作业务终态。"""
        self.audit.record("event.unparsable", error=str(error))
        if self.on_parse_error is not None:
            self.on_parse_error(str(error))


def make_event_handler(
    pipeline: EventPipeline,
    *,
    audit: Any,
    on_parse_error: Callable[[str], None] | None = None,
    card_callback_handler: Any = None,
    group_mention_hint: Any = None,
    management_card_context_store: Any = None,
) -> Callable[[dict], dict | None]:
    """把原始事件体接到管线上，返回可以直接交给长连接的处理函数。

    三个可选依赖留空时行为与该能力加入之前逐字节一致：不装回调处理器则卡片事件只记
    ``event.ignored``；不装群聊引导则群聊消息静默；不装管理卡上下文端口则标识缺失时
    不做恢复。
    """
    return _EventRouter(
        pipeline=pipeline,
        audit=audit,
        on_parse_error=on_parse_error,
        card_callback_handler=card_callback_handler,
        group_mention_hint=group_mention_hint,
        management_card_context_store=management_card_context_store,
    ).handle
