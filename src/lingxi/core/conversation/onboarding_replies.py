"""开通编排结果与预开通首聊提示的渲染层：只把结论翻成用户可见内容。

不做任何 I/O，也不触发编排；失败一律落到内容目录里的终态文案并记审计，绝不让
用户停在没有下文的沉默里。
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

# 编排层没有自带 messages 时，每种状态各自的缺省文案（`OnboardingState` → 内容目录 key）。
# ``completed`` 刻意不在表内：那条文案必须带上公司与职能范围，而范围只有编排层知道，
# 补一句说不清范围的「开通完成」等于替它宣告一个未经确认的成功。``started`` 同样不在
# 表内，但理由相反——它已经由 ``onboarding.checking`` 交代完毕。两者的兜底见
# ``EventPipeline._render_onboarding_result``。
_DEFAULT_ONBOARDING_MESSAGES: dict[OnboardingState, OnboardingMessage] = {
    OnboardingState.MATCHED: OnboardingMessage("onboarding.matched"),
    OnboardingState.NOT_AUTHORIZED: OnboardingMessage("onboarding.not_authorized"),
    OnboardingState.SYNC_TIMEOUT: OnboardingMessage("onboarding.sync_timeout"),
    OnboardingState.INTERNAL_ERROR: OnboardingMessage("onboarding.internal_error"),
}

#: 需要追溯号占位（``{reference}``）的文案键（Issue #280 §7.1）。与
#: ``core/identity/onboarding_runner.py`` 里同名判据各自维护一份——两条渲染入口
#: 此前就是彼此独立的失败关闭桩（本模块只在 gateway 侧 runner 惰性桩/测试注入下
#: 才会真的走到，见该模块「共用线程复核」一节），靠字面值对齐而不是跨模块 import。
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
        self._texts = texts
        self._audit = audit

    def render_result(
        self, result: OnboardingResult, *, checking_key: str, message: InboundMessage
    ) -> tuple[RenderedContent, ...]:
        """把编排结果翻成用户可见内容；**任何非 ``started`` 的结果都必须有话说**。

        默认文案表只覆盖三条失败终态时（Issue #65 轻审 P2-4），一个不带 messages 的
        ``matched`` / ``completed`` 会渲染出空列表：用户收到「正在核对，请稍候」之后
        再也没有下文，而系统这边认为一切正常。悬空的沉默比一条不完美的提示更糟——
        用户既不知道该等还是该重发，也没有任何可以拿去找管理员的线索。

        因此：

        - ``matched`` 补默认文案。它本身不需要任何变量，含义与状态完全一致
          （已核对到权限、正在完成开通、稍后通知）。
        - ``completed`` **不补**「开通完成」文案。那条文案必须报出公司与职能范围
          （产品合同：成功只在开通链路最终确认后报告范围），而这两个值只有编排层
          知道；gateway 编不出来，也不允许宣告一个说不清范围的成功。
        - 于是 ``completed`` 连同任何其他渲染为空的非 ``started`` 结果，一起落到冻结的
          ``LX-ONBOARD-001`` 内部故障终态：它的原文正是「已转交管理员处理」，而
          「编排说开通完成却说不出范围」确实需要管理员看一眼。
        - ``started`` 是唯一允许没有下文的状态：它表示编排已异步接手，用户刚收到的
          「正在核对，请稍候」就是这一轮的完整交代。

        兜底时记一条 ``onboarding.render_failed``，字段里保留编排真正返回的状态。
        动作名带 ``failed`` 后缀，让审计实现把它升到 ``WARNING``——用户虽然拿到了
        提示，但「编排返回了一个渲染不出来的结果」是必须有人看见的内部缺陷。
        """

        messages = list(result.messages)
        if not messages:
            default_message = _DEFAULT_ONBOARDING_MESSAGES.get(result.state)
            if default_message is not None:
                messages.append(default_message)

        rendered: list[RenderedContent] = []
        for onboarding_message in messages:
            if onboarding_message.key == checking_key:
                # checking is owned by the gateway and is sent exactly once.
                continue
            values = _with_reference(
                onboarding_message.key, onboarding_message.as_values(), message.trace_id
            )
            rendered.append(self._texts.catalog.text(onboarding_message.key, values))
        if not rendered and result.state is not OnboardingState.STARTED:
            # 独立审查 codex P1-2（已核实，见 commit 说明的核实证据）：同一类兜底——
            # 「已转交管理员处理」没有接 ONBOARDING_FAILED 管理员告警回调。
            # **防御性分支，生产不可达**：理由同上一处兜底（`_start_onboarding` 的
            # `except`）——生产 gateway 恒定接的是 `_RecordingOnboarding`，它只
            # 返回 `state=STARTED`（本条件的 `result.state is not STARTED` 恒假），
            # 装配期断言挡住任何会返回非 `STARTED` 结果的其他实现。告警缺失因此
            # **无生产影响**；gateway 侧若未来装配真实 runner，需要同步在这里接上
            # 告警出口。
            self._audit.record(
                "onboarding.render_failed",
                event_id=message.event_id,
                state=result.state.value,
                trace_id=message.trace_id,
            )
            return (
                self._texts.catalog.text("onboarding.internal_error", reference=message.trace_id),
            )
        return tuple(rendered)

    def render_preprovision_notice(
        self,
        pending: PendingPreprovisionNotice,
        *,
        event_id: str,
        user_id: str,
        trace_id: str,
    ) -> RenderedContent | None:
        """渲染预开通首聊那句「你的 BI Plus 已经开通……」；失败返回 ``None``、只记审计。

        公司/职能取值与开通链发 ``onboarding.completed`` 时**同一来源、同一姿势**：
        该用户当前权限版本已发布的权限文档，经 ``describe_scope(parse_permissions(...))``
        说成两串展示文本（``core/identity/onboarding_runner._completed`` 与
        ``apps/scheduler/late_readiness_recovery`` 逐字同一调用）。

        **不能因为一句附加提示打断这个人的问数**——他这条消息的正常处理与这句话无关，
        因此两类失败都只留审计、返回 ``None``；调用方据此**不消费**一次性标志，下一条
        消息重试（rc25 修复包 F1：「只提示一次」保持，「失败即永远丢失」消除）：

        - 文案侧失败 → 既有 ``onboarding.preprovision_notice_content_missing``。生产
          上键缺失走不到（内容目录加载期要求键集合完全相等，缺键的构建起不来），这一
          桶覆盖内容目录与代码分两次合入的中间态，以及占位集合不符、出口安全校验拦截；
        - 权限快照不可用（``publish_outbox.payload`` 过九十天保留期被擦成 ``'{}'``、
          或当前版本没有已发布意图）或读不懂 →
          ``onboarding.preprovision_notice_scope_unavailable``。
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
            # 注意 except 次序：ContentError 继承自 ValueError，先窄后宽。
            self._audit.record(
                "onboarding.preprovision_notice_content_missing",
                event_id=event_id,
                user_id=user_id,
                trace_id=trace_id,
            )
            return None
        except ValueError:
            self._audit.record(
                "onboarding.preprovision_notice_scope_unavailable",
                event_id=event_id,
                user_id=user_id,
                trace_id=trace_id,
            )
            return None
