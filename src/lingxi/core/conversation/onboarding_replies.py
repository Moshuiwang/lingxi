"""开通编排结果与预开通首聊提示的渲染层：只把结论翻成用户可见内容。

不做任何 I/O，也不触发编排。**任何非"已接手"的结果都必须有话说**：悬空的沉默比一条
不完美的提示更糟——用户既不知道该等还是该重发，也没有任何可以拿去找管理员的线索。
因此渲染不出内容时一律落到冻结的内部故障终态文案，并记一条带 ``failed`` 后缀的审计，
好让审计实现把它升到 ``WARNING``。
"""

from __future__ import annotations

from lingxi.config.content import (
    KEY_PREPROVISIONED_FIRST_CHAT,
    ContentRenderError,
    ContentSafetyError,
    RenderedContent,
)
from lingxi.core.permission.notification import describe_scope
from lingxi.core.permission.publish_row import parse_permissions

from .gateway_texts import GatewayTexts
from .ports import (
    AuditSink,
    InboundMessage,
    OnboardingMessage,
    OnboardingResult,
    OnboardingState,
    PendingPreprovisionNotice,
)

# 编排没有自带 messages 时每种状态的缺省文案。``completed`` 刻意不在表内：那条文案
# 必须带上公司与职能范围，而范围只有编排层知道，补一句说不清范围的「开通完成」等于
# 替它宣告一个未经确认的成功。``started`` 也不在表内，理由相反——它已经由「正在核对」
# 交代完毕。两者的兜底见 :meth:`OnboardingReplyRenderer.render_result`。
_DEFAULT_ONBOARDING_MESSAGES: dict[OnboardingState, OnboardingMessage] = {
    OnboardingState.MATCHED: OnboardingMessage("onboarding.matched"),
    OnboardingState.NOT_AUTHORIZED: OnboardingMessage("onboarding.not_authorized"),
    OnboardingState.SYNC_TIMEOUT: OnboardingMessage("onboarding.sync_timeout"),
    OnboardingState.INTERNAL_ERROR: OnboardingMessage("onboarding.internal_error"),
}

#: 需要追溯号占位（``{reference}``）的文案键。开通编排一侧另有一份同名判据：两条
#: 渲染入口本就是彼此独立的失败关闭桩，靠字面值对齐而不是跨模块 import，两份必须
#: 相等这件事由内容目录对账用例守住。
KEYS_REQUIRING_REFERENCE: frozenset[str] = frozenset(
    {"onboarding.internal_error", "onboarding.sync_timeout"}
)


def _with_reference(key: str, values: dict[str, object], trace_id: str) -> dict[str, object]:
    """给需要追溯号的终态文案补上 ``reference`` 占位值，已有值不覆盖。"""
    merged = dict(values)
    if key in KEYS_REQUIRING_REFERENCE:
        merged.setdefault("reference", trace_id)
    return merged


class OnboardingReplyRenderer:
    """把开通编排的结论与预开通挂起提示翻成用户可见内容。"""

    def __init__(self, *, texts: GatewayTexts, audit: AuditSink) -> None:
        """记住文案取值口与审计出口，两者与调用它的管线同一份实例。"""
        self._texts = texts
        self._audit = audit

    def render_result(
        self, result: OnboardingResult, *, checking_key: str, message: InboundMessage
    ) -> tuple[RenderedContent, ...]:
        """把编排结果翻成用户可见内容，按发送顺序排列。

        ``started`` 是唯一允许没有下文的状态：它表示编排已异步接手，用户刚收到的「正在核对，
        请稍候」就是这一轮的完整交代。其余状态渲染为空时（包括说不出范围的 ``completed``）
        一律落到内部故障终态——「已转交管理员处理」这句话在这里是准确的：编排返回了一个渲染
        不出来的结果，确实需要有人看一眼。``checking_key`` 是那句「正在核对」的文案键，由
        gateway 自己发过一次，这里跳过。
        """
        messages = list(result.messages)
        if not messages:
            default_message = _DEFAULT_ONBOARDING_MESSAGES.get(result.state)
            if default_message is not None:
                messages.append(default_message)

        rendered: list[RenderedContent] = []
        for onboarding_message in messages:
            if onboarding_message.key == checking_key:
                continue
            values = _with_reference(
                onboarding_message.key, onboarding_message.as_values(), message.trace_id
            )
            rendered.append(self._texts.catalog.text(onboarding_message.key, values))
        if not rendered and result.state is not OnboardingState.STARTED:
            return self._render_unrenderable(result, message)
        return tuple(rendered)

    def _render_unrenderable(
        self, result: OnboardingResult, message: InboundMessage
    ) -> tuple[RenderedContent, ...]:
        """渲染为空的非"已接手"结果：回内部故障终态，并留一条必须有人看见的审计。

        **防御性分支，生产不可达**：gateway 恒定接的是一个只返回"已接手"的惰性编排，
        装配期断言挡住任何别的实现，因此这里没有接管理员告警回调。gateway 侧若未来
        装配真实编排，需要同步在这里接上告警出口。
        """
        self._audit.record(
            "onboarding.render_failed",
            event_id=message.event_id,
            state=result.state.value,
            trace_id=message.trace_id,
        )
        return (self._texts.catalog.text("onboarding.internal_error", reference=message.trace_id),)

    def render_preprovision_notice(
        self,
        pending: PendingPreprovisionNotice,
        *,
        event_id: str,
        user_id: str,
        trace_id: str,
    ) -> RenderedContent | None:
        """渲染预开通用户首聊时补的那一句；失败返回 ``None`` 并只记审计。

        公司/职能取值与开通链宣告成功时**同一来源、同一姿势**：该用户当前权限版本已发布的
        权限文档，经同一个范围描述函数说成两串展示文本。

        **不能因为一句附加提示打断这个人的问数**——他这条消息的正常处理与这句话无关，因此
        文案侧失败与权限快照不可用都只留审计、返回 ``None``；调用方据此**不消费**一次性
        标志，下一条消息还会再试（「只提示一次」保持，「失败即永远丢失」消除）。
        """
        try:
            if pending.permissions is None:
                # 内部错误消息不进用户可见面；本文件禁嵌中文字面量（内容目录守卫）。
                raise ValueError("preprovision scope snapshot unavailable")
            company, function = describe_scope(
                parse_permissions(pending.permissions), catalog=self._texts.catalog
            )
            return self._texts.catalog.text(
                KEY_PREPROVISIONED_FIRST_CHAT,
                company_name=company,
                function_name=function,
            )
        except (ContentRenderError, ContentSafetyError):
            # 次序是硬的：内容类异常继承自 ``ValueError``，先窄后宽。文案侧失败在
            # 生产走不到（内容目录加载期要求键集合完全相等），这一桶覆盖内容目录与
            # 代码分两次合入的中间态，以及占位集合不符、出口安全校验拦截。
            self._audit.record(
                "onboarding.preprovision_notice_content_missing",
                event_id=event_id,
                user_id=user_id,
                trace_id=trace_id,
            )
            return None
        except ValueError:
            # 权限快照不可用：过保留期被擦空，或当前版本没有已发布意图。
            self._audit.record(
                "onboarding.preprovision_notice_scope_unavailable",
                event_id=event_id,
                user_id=user_id,
                trace_id=trace_id,
            )
            return None
