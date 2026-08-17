"""gateway 事件处理管线：[接口设计 3.2](../../../../docs/技术设计/接口设计.md) 的处理次序。

**次序本身是合同要求，不能重排。** 这个模块的全部价值就是把那张次序表变成可判定的代码，
因此每一步上方都标了它对应的断言编号；调换任意两步都应当有用例变红。

    1. 通道级认证        —— 长连接握手期完成，不在逐事件层做（见下方「关于第 1 步」）
    2. event_id 落库     冲突 → 重复投递，直接返回成功        V-接入-01/02/09
    3. 加表情            失败 → 记审计，继续                  V-接入-07/08
    4. 查用户状态        未开通 → 丢弃正文并认领开通；已停用 → 回提示 V-审计-05
    5. 解析命令          /stop /new                            V-会话-05/06
    6. 话题忙碌判定      忙碌 且 非 /stop → 只回提示，不入队    V-会话-04/09/10
    7. 入队 + NOTIFY                                           V-队列-01…05

编号以[验证与门禁](../../../../docs/技术设计/验证与门禁.md)的矩阵为准（两位数字，
不用字母后缀）；断言表里的 `V-会话-02a`/`05a`/`06a` 登记时续号为 08/09/10。

**关于第 1 步。** 接口设计原文写的是「验签」，那是 Webhook 语义。本切片按 2026-08-06
决策走官方 ``lark-oapi`` 的长连接，认证发生在**握手期**（应用凭据换取 endpoint 与
wss 地址），单条事件上没有可验的签名。承接同一产品意图的是 `V-接入-10`：进程不监听
任何入站端口，事件只能从那条已认证的长连接进来。判定面比逐事件验签更严——不存在
"签名对了就受理"的旁路，因为根本没有第二个入口。接口设计 3.2 随本切片同步修订。

**关于事务边界。** 第 2 步到第 7 步跑在**同一个事务**里（`V-队列-01`）。这意味着任务
插入失败时 ``inbound_event`` 那一行也不存在，飞书重投时该消息能被重新完整处理；也意味着
抢占会随事务一起回滚，话题不会永久停在"忙碌"（`V-队列-02`）。

第 3 步的加表情是**外部调用，落在事务里且不可回滚**——事务回滚后表情已经加上了。这是
知情取舍，合同允许：表情"只表示已经收到，不表示消息能够执行或任务已经开始"。反过来
不成立：任何表示"已受理"的**回复**都不能在入队成功前发出（`V-队列-03`）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from lingxi.config.content import ContentCatalog, RenderedContent, default_content_catalog
from lingxi.core.ids import new_id

from .commands import Command, parse_command
from .ports import (
    AuditSink,
    GatewayStore,
    HandledAs,
    InboundMessage,
    OnboardingMessage,
    OnboardingResult,
    OnboardingRunner,
    OnboardingState,
    Outcome,
    Reactions,
    Replies,
    UserState,
    VersionResolver,
)
from .session_window import should_resume_session

logger = logging.getLogger(__name__)

# 对外保留这个旧导出，避免调用方为读取固定提示而复制文案；正文唯一来源是内容目录。
BUSY_HINT_TEXT = default_content_catalog().text("gateway.busy_hint").text

# #45 的分流规则形态属 S11（决策第 3 条）。本批最小实现固定 stable，
# 断言只约束「入队时固化」与「领取带版本条件」两件事。
DEFAULT_WORKER_VERSION = "stable"

# 本批唯一会被入队的消息类型。
TEXT_MESSAGE_TYPE = "text"

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


class QueueInsertFailure(RuntimeError):
    """task 写入失败，入队事务必须整体回滚。"""


def fixed_stable_version(*, user_id: str, now: datetime) -> str:
    """默认版本求值：恒为 ``stable``。签名保留 #45 要求的两个输入。"""

    return DEFAULT_WORKER_VERSION


@dataclass(frozen=True)
class GatewayTexts:
    """用户可见文案。

    文字字段保留为 ``str``，兼容现有注入式测试；默认值来自版本化内容目录。发送前通过
    ``*_content`` 方法补回键和版本，审计不记录渲染后的用户正文。
    """

    busy_hint: str = field(
        default_factory=lambda: default_content_catalog().text("gateway.busy_hint").text
    )
    suspended: str = field(
        default_factory=lambda: default_content_catalog().text("gateway.suspended").text
    )
    catalog: ContentCatalog = field(default_factory=default_content_catalog, repr=False, compare=False)

    def busy_hint_content(self) -> RenderedContent:
        return _as_content(self.catalog, "gateway.busy_hint", self.busy_hint)

    def suspended_content(self) -> RenderedContent:
        return _as_content(self.catalog, "gateway.suspended", self.suspended)

    def queue_failed_content(self) -> RenderedContent:
        return self.catalog.text("gateway.queue_failed")


class EventPipeline:
    """把一条入站消息按 3.2 的次序处理完。"""

    def __init__(
        self,
        *,
        store: GatewayStore,
        reactions: Reactions,
        replies: Replies,
        audit: AuditSink,
        texts: GatewayTexts | None = None,
        resolve_version: VersionResolver = fixed_stable_version,
        should_stop: Callable[[], bool] | None = None,
        onboarding: OnboardingRunner | None = None,
    ) -> None:
        self._store = store
        self._reactions = reactions
        self._replies = replies
        self._audit = audit
        self._texts = texts or GatewayTexts()
        self._resolve_version = resolve_version
        # The runner is an application boundary: it owns the #89 identity result,
        # account matching and the #17 environment/permission/MCP orchestration.
        # Keeping it optional preserves the old negative-only assembly for callers
        # that have not opted into the #65 path; the gateway app passes it explicitly
        # when the product path is enabled.
        self._onboarding = onboarding
        # 停机位。停机时**已提交的结论不动**，只跳过提交之后那些尽力而为的动作——
        # 中途放弃一个快要提交完的事务只会把工作丢掉再让平台重投一次。
        # 跳过的有两样：出站回复，以及开通编排的触发（Issue #65 轻审 P2-3）。后者
        # 跳过之后会留下一条待对账的事件，不会丢——见 ``handle_message``。
        self._should_stop = should_stop or (lambda: False)

    def handle_message(self, message: InboundMessage, *, now: datetime | None = None) -> Outcome:
        """处理一条 ``im.message.receive_v1``。

        ``now`` 只为注入时钟开放（`V-会话-02`），正常调用不传。

        **回复一律在事务提交之后才发出。** 早先的写法在事务里就把"当前任务仍在处理中"
        发出去了，于是回复成功、``mark_handled_as`` 或提交失败时：事务回滚 → 平台重投
        → 提示重发；更糟的是**原任务如果这时已经结束**，这条本应"不生效"的消息会在
        重投时被正常入队执行——直接违反合同「该消息不进入对话历史、不排队，也不会在
        当前任务结束后自动提交或自动生效」。

        改成先把 ``handled_as`` 结论持久化并提交、再发回复之后：重投时事件行已经在
        库里，幂等去重挡住重处理；回复失败只记审计。这是知情取舍——用户少收一条提示
        可以接受，合同的硬承诺是"不自动生效"，那一条现在由已提交的事件行保证。
        """

        moment = now or datetime.now(timezone.utc)
        deferred: list[RenderedContent] = []

        try:
            outcome = self._within_transaction(message, moment, deferred)
        except QueueInsertFailure as error:
            # 真正的 PostgreSQL store 通过独立事务取得一次发送权；没有该能力的旧注入
            # store 继续抛出原始异常，以免把一个仅测试事务回滚的假实现冒充生产发送器。
            claim_notice = getattr(self._store, "claim_queue_failure_notice", None)
            if claim_notice is None:
                raise error.__cause__ or error
            if claim_notice(event_id=message.event_id):
                content = self._texts.queue_failed_content()
                if not self._should_stop():
                    try:
                        self._replies.send_text(
                            chat_id=message.chat_id,
                            thread_id=message.thread_id,
                            reply_to_message_id=message.message_id,
                            text=content.text,
                        )
                        self._audit.record(
                            "reply.sent",
                            event_id=message.event_id,
                            content_key=content.key,
                            content_version=content.version,
                            trace_id=message.trace_id,
                        )
                    except Exception as send_error:  # noqa: BLE001 - 结论已回滚，提示尽力而为
                        self._audit.record(
                            "reply.failed",
                            event_id=message.event_id,
                            content_key=content.key,
                            content_version=content.version,
                            error=f"{type(send_error).__name__}: {send_error}",
                            trace_id=message.trace_id,
                        )
            self._audit.record(
                "task.enqueue_failed",
                event_id=message.event_id,
                error=f"{type(error.__cause__ or error).__name__}",
                trace_id=message.trace_id,
            )
            return Outcome(handled_as=None)

        # 到这里事务已经提交。现在才允许产生用户可见的出站副作用。
        if outcome.handled_as is HandledAs.AUTO_PROVISIONING:
            if self._should_stop():
                # 停机中**不触发**开通编排（Issue #65 轻审 P2-3）。此前这里无条件调用
                # runner，再由下面那段停机检查把渲染结果整批丢掉——正式 runner 有外部
                # 副作用（建档、建环境、发权限、MCP 同步，合同允许到十五分钟），那等于
                # 在停机窗口里发起一串不可回滚的外部动作，然后把用户唯一能看到的结论
                # 扔掉；停机预算（默认二十秒量级）也压根装不下它。
                #
                # 事件行已经提交、``handled_as`` 已经是 ``auto_provisioning``，但账本上
                # 的 ``onboarding_dispatched_at`` 仍是空——这条事件因此是一条**故意**
                # 留下的孤儿，由 P2-2 的对账扫描在下次启动后重新交接。这是知情取舍：
                # 晚几分钟开通，好过在停机中途开一半。
                self._audit.record(
                    "onboarding.deferred_while_stopping",
                    event_id=message.event_id,
                    trace_id=message.trace_id,
                )
            else:
                self._start_onboarding(message, deferred)

        if deferred and self._should_stop():
            # 停机中：结论已经落库，提示是尽力而为的那一部分。此时再发一次出站
            # HTTP 只会把停机拖过预算（出站默认 30 秒 > 停机 20 秒），而用户少收
            # 一条提示不改变任何硬承诺。
            for content in deferred:
                self._audit.record(
                    "reply.skipped_while_stopping",
                    event_id=message.event_id,
                    content_key=content.key,
                    content_version=content.version,
                    trace_id=message.trace_id,
                )
            return outcome
        for content in deferred:
            try:
                self._replies.send_text(
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    reply_to_message_id=message.message_id,
                    text=content.text,
                )
                self._audit.record(
                    "reply.sent",
                    event_id=message.event_id,
                    content_key=content.key,
                    content_version=content.version,
                    trace_id=message.trace_id,
                )
            except Exception as error:  # noqa: BLE001 - 回复失败不改变已提交的结论
                self._audit.record(
                    "reply.failed",
                    event_id=message.event_id,
                    content_key=content.key,
                    content_version=content.version,
                    error=f"{type(error).__name__}: {error}",
                    trace_id=message.trace_id,
                )
        return outcome

    def _within_transaction(
        self, message: InboundMessage, moment: datetime, deferred: list[RenderedContent]
    ) -> Outcome:
        """第 2 步到第 7 步，全部落在同一个事务里。"""

        with self._store.transaction() as tx:
            # —— 第 2 步：幂等。冲突即重复投递，**在此立刻返回**。
            # 早退发生在加表情之前，因此重复投递在用户可见面同样不重复：不再加表情、
            # 不再发任何回复（`V-接入-09` 断的是出站调用次数，不只是数据库行数）。
            first_time = tx.insert_inbound_event(
                event_id=message.event_id,
                event_type=message.event_type,
                user_open_id=message.sender_open_id,
                trace_id=message.trace_id,
            )
            if not first_time:
                self._audit.record(
                    "inbound_event.duplicate",
                    event_id=message.event_id,
                    trace_id=message.trace_id,
                )
                return Outcome(handled_as=None, duplicate=True)

            # —— 第 3 步：加表情。合同：任何消息都加，失败不阻断后续处理。
            self._add_reaction(message)

            # —— 第 4 步：用户状态。
            # 任务归属只由发送者标识解析而来（`V-接入-11`）：这里传的是
            # message.sender_open_id，而 InboundMessage 里根本没有第二个用户标识可传。
            user = tx.lookup_user(open_id=message.sender_open_id)
            state = user.state if user is not None else UserState.NOT_PROVISIONED

            if state is UserState.NOT_PROVISIONED:
                # 合同：未开通用户发来的业务内容不进入问数、不保存也不回显（`V-审计-05`）。
                # 注意审计里也不带消息正文：内容"不保存"包括不写进审计。
                if self._onboarding is not None:
                    self._audit.record(
                        "inbound_event.auto_provisioning",
                        event_id=message.event_id,
                        trace_id=message.trace_id,
                    )
                    tx.mark_handled_as(
                        event_id=message.event_id, handled_as=HandledAs.AUTO_PROVISIONING
                    )
                    return Outcome(handled_as=HandledAs.AUTO_PROVISIONING)

                # 未配置正向编排的旧装配仍然保持明确的否定终态；正式 gateway
                # 通过 apps.gateway.build_supervisor 的 onboarding 注入口启用 #65。
                self._audit.record(
                    "inbound_event.not_provisioned",
                    event_id=message.event_id,
                    trace_id=message.trace_id,
                )
                tx.mark_handled_as(
                    event_id=message.event_id, handled_as=HandledAs.NOT_PROVISIONED
                )
                return Outcome(handled_as=HandledAs.NOT_PROVISIONED)

            assert user is not None  # NOT_PROVISIONED 已在上一分支返回

            if state is UserState.SUSPENDED:
                deferred.append(self._texts.suspended_content())
                self._audit.record(
                    "inbound_event.suspended",
                    event_id=message.event_id,
                    user_id=user.user_id,
                    trace_id=message.trace_id,
                )
                tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.DROPPED)
                return Outcome(handled_as=HandledAs.DROPPED)

            conversation = tx.ensure_conversation(
                user_id=user.user_id,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
            )

            # 用户这次主动发消息：如果这个话题上一次问数因二十四小时未获得
            # platform_received 而到期（`delivery_expired`），且还没提示过，就在这里
            # 提示一次「请重新提问」（Issue #152、`V-投递-06` 后半句）。这条检查
            # **不影响**当前消息接下来按第 5～7 步的正常处理——用户这条消息该入队
            # 还是入队，该被判忙碌还是判忙碌，过期提示只是额外追加的一条回复，且
            # 只提示一次，不主动推送、不重放旧答案。
            if tx.consume_delivery_expired_notice(conversation_id=conversation.conversation_id):
                deferred.append(self._texts.catalog.text("gateway.delivery_expired"))

            # —— 第 5 步：解析命令。在忙碌判定**之前**，因为 /stop 不受忙碌拦截。
            command = parse_command(message.text)

            # —— 第 6 步：忙碌判定。
            busy = conversation.running_task_id is not None

            if command is Command.STOP:
                # `V-会话-10`：3.2 第 6 步的条件是「忙碌 **且非 /stop**」，
                # 因此 /stop 在忙碌时照常被处理，而不是收到"当前任务仍在处理中"。
                stopped = tx.request_stop(conversation_id=conversation.conversation_id)
                self._audit.record(
                    "command.stop",
                    event_id=message.event_id,
                    user_id=user.user_id,
                    conversation_id=conversation.conversation_id,
                    stopped_task_id=stopped,
                    trace_id=message.trace_id,
                )
                tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.COMMAND)
                return Outcome(handled_as=HandledAs.COMMAND)

            if busy:
                # 忙碌期：只回提示。合同——该消息不进入对话历史、不排队，也不会在当前
                # 任务结束后自动提交或自动生效。`/new` 被合同明确列入受限命令，因此这条
                # 分支在 /new 之前（`V-会话-09`）：忙碌时的 /new 不清空上下文。
                deferred.append(self._texts.busy_hint_content())
                tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.BUSY_HINT)
                return Outcome(handled_as=HandledAs.BUSY_HINT)

            if command is Command.NEW:
                # 空闲时的 /new：立即清空当前对话上下文，其他话题不受影响
                # （条件写在 conversation_id 上，天然只影响这一行）。
                #
                # 清空本身**再判一次忙碌**，因为上面那个 busy 读的是事务开始时的快照：
                # 另一条连接可能在这中间抢占成功并已经在跑。条件更新影响 0 行就说明
                # 话题已经忙了，走忙碌分支——否则会把一个正在执行的任务的上下文清掉。
                if not tx.clear_agent_session(conversation_id=conversation.conversation_id):
                    deferred.append(self._texts.busy_hint_content())
                    tx.mark_handled_as(
                        event_id=message.event_id, handled_as=HandledAs.BUSY_HINT
                    )
                    return Outcome(handled_as=HandledAs.BUSY_HINT)
                self._audit.record(
                    "command.new",
                    event_id=message.event_id,
                    user_id=user.user_id,
                    conversation_id=conversation.conversation_id,
                    trace_id=message.trace_id,
                )
                # 产品合同「系统明确告诉用户已经开启新会话」：表情继续充当「已收到」
                # 信号，这里追加一条明确的文字确认，随事务提交后统一发送循环发出
                # （不改变事务边界，见类顶部说明）。
                deferred.append(self._texts.catalog.text("gateway.new_session"))
                tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.COMMAND)
                return Outcome(handled_as=HandledAs.COMMAND)

            # 非文本消息（图片、语音、富文本……）：表情已经加过（合同：任何消息都加），
            # 但**不入队**——把一条语音当成空问题排进队列，用户只会拿到一个莫名其妙的
            # 失败，而且会白占一次话题串行名额。
            #
            # 刻意**不回复任何文案**：「是否要明确告诉用户暂不支持这种消息」是一条新的
            # 用户可见承诺，合同没有写，本批不发明（与入队失败的处理同一姿态），
            # 已登记为待产品负责人定夺项。
            #
            # 位置在忙碌判定**之后**：忙碌期的非文本消息与其他消息一样只得到
            # 「当前任务仍在处理中」，不因为类型不同而给出第二种回应。
            if message.message_type != TEXT_MESSAGE_TYPE:
                self._audit.record(
                    "message.unsupported_type",
                    event_id=message.event_id,
                    user_id=user.user_id,
                    message_type=message.message_type,
                    trace_id=message.trace_id,
                )
                tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.DROPPED)
                return Outcome(handled_as=HandledAs.DROPPED)

            # —— 第 7 步：入队。
            return self._enqueue(
                tx,
                message,
                user_id=user.user_id,
                conversation=conversation,
                now=moment,
                deferred=deferred,
            )

    # ------------------------------------------------------------------
    # 内部步骤
    # ------------------------------------------------------------------

    def _start_onboarding(
        self, message: InboundMessage, deferred: list[RenderedContent]
    ) -> None:
        """提交后启动一次自动开通，并把结果限制在内容目录内。

        事务只负责认领 ``event_id``；身份读取、匹配、开通和 MCP 同步由 runner
        自己用独立的幂等边界完成。这样 gateway 不会把长耗时外部调用放进队列事务，
        也不会把用户原文传入权限链。
        """

        assert self._onboarding is not None
        checking = self._texts.catalog.text("onboarding.checking")
        deferred.append(checking)

        try:
            result = self._onboarding.start(
                event_id=message.event_id,
                open_id=message.sender_open_id,
                trace_id=message.trace_id,
            )
            if not isinstance(result, OnboardingResult):
                raise TypeError("onboarding runner returned an invalid result")
            rendered = self._render_onboarding_result(
                result, checking_key=checking.key, message=message
            )
        except Exception as error:  # noqa: BLE001 - 失败必须落到统一终态文案
            internal = self._texts.catalog.text("onboarding.internal_error")
            deferred.append(internal)
            self._audit.record(
                "onboarding.failed",
                event_id=message.event_id,
                state=OnboardingState.INTERNAL_ERROR.value,
                error=type(error).__name__,
                trace_id=message.trace_id,
            )
            # 编排确实被调用过（异常来自它内部），账本必须记上：不记的话对账扫描会
            # 把一条已经得到冻结失败终态的事件再交接一次，用户会收到第二遍 LX-ONBOARD-001。
            self._mark_onboarding_dispatched(message)
            return

        deferred.extend(rendered)
        self._audit.record(
            "onboarding.result",
            event_id=message.event_id,
            state=result.state.value,
            failure_reason=result.failure_reason,
            content_keys=tuple(content.key for content in rendered),
            trace_id=message.trace_id,
        )
        self._mark_onboarding_dispatched(message)

    def _mark_onboarding_dispatched(self, message: InboundMessage) -> None:
        """记账：这条事件已经交给开通编排了（Issue #65 轻审 P2-2）。

        **失败只记审计，绝不向上抛。** 记不上账的最坏后果是对账扫描过一会儿再交接
        一次，而 ``OnboardingRunner.start`` 按合同幂等；反过来，让一次已经拿到结论的
        开通因为一条簿记 ``UPDATE`` 失败而炸掉，会把用户可见的终态提示也一起带走。
        旧注入 store 没有这个方法时同样落进这里（``AttributeError``），行为一致：
        账本没记上，孤儿由扫描兜底。

        动作名带 ``failed`` 后缀是为了让 ``apps/gateway`` 的审计实现把它升到
        ``WARNING``：这是一次真实的数据库写失败，淹没在 INFO 流水里就等于没记
        （#175/#185 的教训）。
        """

        try:
            self._store.mark_onboarding_dispatched(event_id=message.event_id)
        except Exception as error:  # noqa: BLE001 - 见 docstring
            self._audit.record(
                "onboarding.dispatch_record_failed",
                event_id=message.event_id,
                error=type(error).__name__,
                trace_id=message.trace_id,
            )

    def _render_onboarding_result(
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
            rendered.append(
                self._texts.catalog.text(
                    onboarding_message.key, onboarding_message.as_values()
                )
            )
        if not rendered and result.state is not OnboardingState.STARTED:
            self._audit.record(
                "onboarding.render_failed",
                event_id=message.event_id,
                state=result.state.value,
                trace_id=message.trace_id,
            )
            return (self._texts.catalog.text("onboarding.internal_error"),)
        return tuple(rendered)

    def _add_reaction(self, message: InboundMessage) -> None:
        """第 3 步。失败只记审计，绝不向上抛（`V-接入-08`）。

        捕获 ``Exception`` 是刻意的：这一步的产品语义就是"尽力而为"，任何失败形态都
        不该改变后续处理。把它收窄成某几个异常类型，等于让没预料到的失败形态重新获得
        阻断后续处理的能力。
        """

        try:
            self._reactions.add(message_id=message.message_id)
        except Exception as error:  # noqa: BLE001 - 见 docstring
            self._audit.record(
                "reaction.failed",
                event_id=message.event_id,
                message_id=message.message_id,
                error=f"{type(error).__name__}: {error}",
                trace_id=message.trace_id,
            )

    def _enqueue(
        self,
        tx,
        message: InboundMessage,
        *,
        user_id: str,
        conversation,
        now: datetime,
        deferred: list[RenderedContent],
    ) -> Outcome:
        task_id = new_id("tsk")

        # 抢占与入队同事务：抢不到即忙碌（`V-会话-01`）；抢到之后任何失败都会让
        # 抢占随事务一起回滚，话题不会永久忙碌（`V-队列-02`）。
        if not tx.claim_conversation(
            conversation_id=conversation.conversation_id, task_id=task_id
        ):
            deferred.append(self._texts.busy_hint_content())
            tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.BUSY_HINT)
            return Outcome(handled_as=HandledAs.BUSY_HINT)

        # 续用判定发生在**入队时**并落库（`V-会话-08`）：排队多久都不再改变它。
        # 只读 last_task_ended_at，读不到任务开始时间或时长（`V-会话-03`）。
        resumed = should_resume_session(
            last_task_ended_at=conversation.last_task_ended_at,
            agent_session_id=conversation.agent_session_id,
            now=now,
        )
        # 产品合同「系统明确告诉用户已经开启新会话」的**第二条触发路径**（Issue #189）：
        # 不是用户敲的 `/new`，而是两小时空闲后下一条消息自然开的新会话。判定**不重算
        # 窗口、不改 `should_resume_session`**，只把"不续用"的三种成因区分开来：
        #
        # - 本来有会话可续（``agent_session_id`` 非空）、也确实结束过上一次任务
        #   （``last_task_ended_at`` 非空），却仍然判为不续用 → 唯一可能的成因就是
        #   间隔超过了两小时，提示；
        # - 首次提问（两者皆空）→ 不提示，用户没有任何"此前上下文"可言；
        # - `/new` 之后（``agent_session_id`` 已被清空，``last_task_ended_at`` 按
        #   `V-会话-05` 保持不动）→ 不提示，用户刚收到过 `gateway.new_session` 的确认，
        #   再补一句「距上次对话已超过两小时」既重复又与事实不符。
        #
        # 空闲会话到点清除（`sweep_idle_conversations`）刻意**不清空** ``agent_session_id``，
        # 因此隔了两小时才回来的用户仍然落在第一种情形里，不会被误判成 `/new` 之后。
        session_rotated = (
            not resumed
            and bool(conversation.agent_session_id)
            and conversation.last_task_ended_at is not None
        )
        # 目标 worker 版本同样在入队时求值一次并写入（`V-灰度-01`）。
        # 重试、重启、心跳超时回收都不得改写它——数据库触发器兜底。
        version = self._resolve_version(user_id=user_id, now=now)

        try:
            tx.insert_task(
                task_id=task_id,
                conversation_id=conversation.conversation_id,
                user_id=user_id,
                inbound_event_id=message.event_id,
                prompt=message.text,
                resumed_session=resumed,
                target_worker_version=version,
                reply_to_message_id=message.message_id,
            )
        except Exception as error:  # noqa: BLE001 - 事务外只做一次失败提示
            raise QueueInsertFailure("task insert failed") from error
        tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.TASK_QUEUED)
        tx.notify_task_queued()

        if session_rotated:
            # 追加在入队**成功之后**：与 `/new`、忙碌两条分支同一姿态——只有真正
            # 生效的那一步才追加它的文案。入队失败路径另有一道保险（整体丢弃
            # ``deferred``，`V-队列-03`），两道合起来保证用户不会收到一条"已经换
            # 新会话了"却其实没有任何任务在跑的告知。发送本身仍在事务提交后的
            # 统一循环里，不改变事务边界。
            deferred.append(self._texts.catalog.text("gateway.session_rotated"))

        self._audit.record(
            "task.queued",
            event_id=message.event_id,
            user_id=user_id,
            conversation_id=conversation.conversation_id,
            task_id=task_id,
            resumed_session=resumed,
            target_worker_version=version,
            trace_id=message.trace_id,
        )
        return Outcome(
            handled_as=HandledAs.TASK_QUEUED,
            task_id=task_id,
            resumed_session=resumed,
            target_worker_version=version,
        )


def _as_content(catalog: ContentCatalog, key: str, value: str) -> RenderedContent:
    """把兼容旧注入口的字符串包成可追溯内容；默认值仍来自目录。"""

    configured = catalog.text(key)
    if value == configured.text:
        return configured
    return RenderedContent(key=key, version=catalog.version, text=value)
