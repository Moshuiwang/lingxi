"""gateway 事件处理管线：[接口设计 3.2](../../../../docs/技术设计/接口设计.md) 的处理次序。

**次序本身是合同要求，不能重排。** 这个模块的全部价值就是把那张次序表变成可判定的代码，
因此每一步上方都标了它对应的断言编号；调换任意两步都应当有用例变红。

第 2 步到第 8 步跑在**同一个事务**里（`V-队列-01`）：任务插入失败时 ``inbound_event``
那一行也不存在，飞书重投能把这条消息重新完整处理；抢占也随事务一起回滚，话题不会永久
停在"忙碌"。第 3 步的加表情是事务里唯一不可回滚的外部调用，这是知情取舍——表情"只表示
已经收到，不表示消息能够执行"。反过来不成立：任何表示"已受理"的**回复**都必须等事务
提交之后才发出（`V-队列-03`），因此本模块把它们攒进 ``deferred`` 列表统一发送。

通道级认证（第 1 步）发生在长连接握手期，单条事件上没有可验的签名；承接同一产品意图的
是 `V-接入-10`——进程不监听任何入站端口，事件只能从那条已认证的长连接进来。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from lingxi.config.content import RenderedContent
from lingxi.core.ids import new_id

from .commands import (
    Command,
    MemoryCommandKind,
    is_memory_command_message,
    is_unrecognized_slash_message,
    parse_command,
    parse_memory_command,
)
from .gateway_texts import BUSY_HINT_TEXT, GatewayTexts
from .memory_commands import MemoryCommandHandler
from .onboarding_replies import KEYS_REQUIRING_REFERENCE, OnboardingReplyRenderer
from .ports import (
    AuditSink,
    GatewayStore,
    HandledAs,
    InboundMessage,
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

#: 搬到 ``onboarding_replies`` 的旧名转发：跨渲染入口对账按这个名字读取。
_KEYS_REQUIRING_REFERENCE = KEYS_REQUIRING_REFERENCE

__all__ = [
    "BUSY_HINT_TEXT",
    "DEFAULT_WORKER_VERSION",
    "TEXT_MESSAGE_TYPE",
    "DispatchGates",
    "EventPipeline",
    "GatewayTexts",
    "QueueInsertError",
    "fixed_stable_version",
]

# 分流规则形态尚未展开，这里固定 stable；断言只约束「入队时固化」与「领取带版本条件」。
DEFAULT_WORKER_VERSION = "stable"

#: 唯一会被入队的消息类型；其余类型加过表情后丢弃。
TEXT_MESSAGE_TYPE = "text"


class QueueInsertError(RuntimeError):
    """task 写入失败，入队事务必须整体回滚。"""


class _InboundEventInsertError(RuntimeError):
    """``insert_inbound_event`` 落库**之前**就失败的内部标记。

    这是唯一一种"重投真能带来恢复"的失败：幂等记录还没写进 ``inbound_event``，
    平台重投这条事件会被当成全新事件正常处理。包一层 ``__cause__`` 原样带上原始
    异常，只为了让 ``handle_message`` 的顶层兜底能把它与其余异常分开（见
    :meth:`EventPipeline._handle_unexpected_failure` 的 ``retryable``）。
    不对外暴露，也不在本模块之外被捕获或构造。
    """


def fixed_stable_version(*, user_id: str, now: datetime) -> str:
    """默认版本求值：恒为 ``stable``。

    签名保留 ``user_id`` / ``now`` 两个输入，供按用户或按时间分流的实现替换。
    """
    return DEFAULT_WORKER_VERSION


@dataclass(frozen=True)
class DispatchGates:
    """普通业务路径之前的三道分流闸，全部由装配层解析好后传入。

    三项都留空（默认）时，管线行为与这三道闸加入之前逐字节一致。管理命令面的类型刻意是
    ``Any``——只用得到 ``route(...)`` 与返回值的四个属性，鸭子类型足够；import 具体类型
    会让每个只想要会话类型的调用方（含 worker）平白多一条 ``core.admin`` 依赖边。名单闸
    收的是装配期解析好的名单，这里不重新解析、不读环境变量。专用主体**刻意是一个已解析
    好的值而不是每次都查登记表的回调**：对全体消息只做一次内存字符串比较。
    """

    admin_router: Any = None
    innertest_roster_gate: Callable[[str], bool] | None = None
    delegated_subject_open_id: str | None = None


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
        gates: DispatchGates | None = None,
    ) -> None:
        """装配一条管线；除 ``store`` 等四个端口外都可以留默认值。"""
        self._store = store
        self._reactions = reactions
        self._replies = replies
        self._audit = audit
        self._texts = texts or GatewayTexts()
        self._memory = MemoryCommandHandler(texts=self._texts, audit=audit)
        self._onboarding_replies = OnboardingReplyRenderer(texts=self._texts, audit=audit)
        self._resolve_version = resolve_version
        # 开通编排是一条应用边界：身份结果、账号匹配、环境/权限/MCP 编排都归它。
        # 保持可选，是为了让没有启用正向开通的装配继续拿到原来的否定终态。
        self._onboarding = onboarding
        self._gates = gates or DispatchGates()
        # 停机位。停机时**已提交的结论不动**，只跳过提交之后那些尽力而为的动作：
        # 出站回复，以及开通编排的触发。中途放弃一个快要提交完的事务，只会把工作
        # 丢掉再让平台重投一次；被跳过的开通会留下一条待对账的事件，不会丢。
        self._should_stop = should_stop or (lambda: False)

    @property
    def onboarding(self) -> OnboardingRunner | None:
        """这条管线实际拿到的开通编排。**只读**，供装配层回读。

        装配断言要回读构造好的对象**实际持有**的那个引用——比较传进去的变量两次，
        什么也证明不了。
        """
        return self._onboarding

    def handle_message(self, message: InboundMessage, *, now: datetime | None = None) -> Outcome:
        """处理一条入站消息；``now`` 只为注入时钟开放（`V-会话-02`），正常调用不传。

        **本方法对绝大多数失败是全函数：不向调用方抛出异常。** 已被识别的失败各自落到对应
        的诚实提示，这一层只兜住剩余异常——否则它们会一路穿到长连接调度层，那里只记审计、
        什么都不回给用户，而一个稳定复现的缺陷会让平台重投一直撞在同一个异常上。

        Raises:
            Exception: 只在幂等记录尚未落库时破例穿出原始异常，见
                :meth:`_handle_unexpected_failure` 的 ``retryable``。
        """
        try:
            return self._handle_message(message, now=now)
        except _InboundEventInsertError as error:
            return self._handle_unexpected_failure(
                message, error.__cause__ or error, retryable=True
            )
        except Exception as error:
            return self._handle_unexpected_failure(message, error)

    def _handle_unexpected_failure(
        self, message: InboundMessage, error: BaseException, *, retryable: bool = False
    ) -> Outcome:
        """兜底出口：记审计＋尽力而为发一条诚实失败提示。

        **不重新进入事务**——异常已经把这一轮的写入连同事务一起回滚了，这里只用消息自带的、
        与数据库状态无关的字段送提示。审计只记异常类名不记正文：驱动的异常串可能原样带着
        外部标识原值。``retryable=True``（幂等记录尚未落库）时发完提示重新抛出原始异常，让
        调度层向平台报失败以触发重投——这是唯一一种重投真能恢复的失败；其余出口一律吞掉，
        重投只会撞上已经写好的去重行，白刷一遍日志。
        """
        content = self._texts.catalog.text("gateway.unexpected_error", reference=message.trace_id)
        self._audit.record(
            "event.pipeline_failed",
            event_id=message.event_id,
            error=type(error).__name__,
            trace_id=message.trace_id,
        )
        if self._should_stop():
            self._audit_reply_skipped(message, content)
        else:
            self._send_reply(message, content)
        if retryable:
            raise error
        return Outcome(handled_as=None)

    def _handle_message(self, message: InboundMessage, *, now: datetime | None = None) -> Outcome:
        """``handle_message`` 的实际业务逻辑；异常安全网见调用方。"""
        moment = now or datetime.now(UTC)
        deferred: list[RenderedContent] = []

        try:
            outcome = self._within_transaction(message, moment, deferred)
        except QueueInsertError as error:
            return self._report_queue_failure(message, error)

        # 到这里事务已经提交。现在才允许产生用户可见的出站副作用。
        if outcome.handled_as is HandledAs.AUTO_PROVISIONING:
            self._trigger_onboarding(message, deferred)
        self._deliver_deferred(message, deferred)
        return outcome

    def _report_queue_failure(self, message: InboundMessage, error: QueueInsertError) -> Outcome:
        """入队失败：取得一次发送权后回一条诚实提示，并记 ``task.enqueue_failed``。

        发送权由 store 用独立事务发放，保证平台重投时用户也只收到一次提示；没有该
        能力的旧注入 store 继续抛出原始异常，以免把一个仅测试事务回滚的假实现冒充
        生产发送器。
        """
        claim_notice = getattr(self._store, "claim_queue_failure_notice", None)
        if claim_notice is None:
            raise error.__cause__ or error
        if claim_notice(event_id=message.event_id):
            content = self._texts.queue_failed_content()
            if not self._should_stop():
                self._send_reply(message, content)
        self._audit.record(
            "task.enqueue_failed",
            event_id=message.event_id,
            error=f"{type(error.__cause__ or error).__name__}",
            trace_id=message.trace_id,
        )
        return Outcome(handled_as=None)

    def _trigger_onboarding(self, message: InboundMessage, deferred: list[RenderedContent]) -> None:
        """提交之后才触发开通编排；停机中改为留下一条待对账的事件。

        正式编排有建档、建环境、发权限、MCP 同步这一串不可回滚的外部副作用，合同
        允许它跑到十五分钟——停机预算（二十秒量级）装不下，中途开一半更糟。事件行
        已提交、结论已是"认领开通"，但账本上的派发时间仍是空，于是这条事件是一条
        **故意**留下的孤儿，由对账扫描在下次启动后重新交接：晚几分钟开通，好过在
        停机中途开一半。
        """
        if self._should_stop():
            self._audit.record(
                "onboarding.deferred_while_stopping",
                event_id=message.event_id,
                trace_id=message.trace_id,
            )
            return
        self._start_onboarding(message, deferred)

    def _deliver_deferred(self, message: InboundMessage, deferred: list[RenderedContent]) -> None:
        """把攒下的回复在事务提交后统一发出；停机中只记一条跳过审计。

        结论已经落库，提示是尽力而为的那一部分：停机时再发一次出站 HTTP 只会把停机
        拖过预算（出站默认 30 秒 > 停机 20 秒），而用户少收一条提示不改变硬承诺。
        """
        if deferred and self._should_stop():
            for content in deferred:
                self._audit_reply_skipped(message, content)
            return
        for content in deferred:
            self._send_reply(message, content)

    def _send_reply(self, message: InboundMessage, content: RenderedContent) -> None:
        """尽力而为地回一条文本：成功记 ``reply.sent``，失败只记 ``reply.failed``。

        捕获 ``Exception`` 是刻意的——回复失败不得改变任何已经提交的结论。
        """
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
        except Exception as error:
            self._audit.record(
                "reply.failed",
                event_id=message.event_id,
                content_key=content.key,
                content_version=content.version,
                error=f"{type(error).__name__}: {error}",
                trace_id=message.trace_id,
            )

    def _audit_reply_skipped(self, message: InboundMessage, content: RenderedContent) -> None:
        """停机中跳过一条回复时的留痕。"""
        self._audit.record(
            "reply.skipped_while_stopping",
            event_id=message.event_id,
            content_key=content.key,
            content_version=content.version,
            trace_id=message.trace_id,
        )

    # ------------------------------------------------------------------
    # 第 2 步到第 8 步：事务内的次序
    # ------------------------------------------------------------------

    def _within_transaction(
        self, message: InboundMessage, moment: datetime, deferred: list[RenderedContent]
    ) -> Outcome:
        """按 3.2 的次序走完第 2 步到第 8 步，全部落在同一个事务里。"""
        with self._store.transaction() as tx:
            # 第 2 步（`V-接入-01/02/09`）：幂等。早退发生在加表情之前，因此重复
            # 投递在用户可见面同样不重复——断言断的是出站调用次数，不只是行数。
            if not self._insert_inbound_event(tx, message):
                return Outcome(handled_as=None, duplicate=True)

            # 第 3 步（`V-接入-07/08`）：任何消息都加表情，失败不阻断后续处理。
            self._add_reaction(message)

            # 第 4 步（`V-管理-24`）：专用授权主体只能进管理命令面或确定性拒绝出口。
            # 判定必须在按状态分派**之前**——数据漂移让专用主体意外拿到 app_user 行
            # 时，状态就不再是"未开通"，嵌在那个分支里的判定会被整段跳过、直接落进
            # 业务队列。这里只做一次内存字符串比较，不查库。
            delegated = self._gates.delegated_subject_open_id
            if delegated is not None and message.sender_open_id == delegated:
                return self._route_delegated_subject(tx, message, deferred)

            # 第 5 步（`V-审计-05`）：用户状态。任务归属只由发送者标识解析而来
            # （`V-接入-11`）：``InboundMessage`` 里根本没有第二个用户标识可传。
            user = tx.lookup_user(open_id=message.sender_open_id)
            state = user.state if user is not None else UserState.NOT_PROVISIONED
            if state is UserState.NOT_PROVISIONED:
                return self._handle_not_provisioned(tx, message, deferred)

            assert user is not None  # NOT_PROVISIONED 已在上一分支返回
            if state is UserState.PROVISIONING:
                return self._handle_onboarding_in_flight(tx, message, user, deferred)
            if state is UserState.SUSPENDED:
                return self._handle_suspended(tx, message, user, deferred)

            conversation = tx.ensure_conversation(
                user_id=user.user_id,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
            )
            self._append_one_shot_notices(tx, message, user, conversation, deferred)
            return self._handle_active_message(tx, message, user, conversation, moment, deferred)

    def _insert_inbound_event(self, tx, message: InboundMessage) -> bool:
        """第 2 步：落幂等记录。返回 ``False`` 表示这条事件此前已经处理过。

        Raises:
            _InboundEventInsertError: 落库调用本身失败——见该异常的文档。
        """
        try:
            first_time = tx.insert_inbound_event(
                event_id=message.event_id,
                event_type=message.event_type,
                user_open_id=message.sender_open_id,
                trace_id=message.trace_id,
            )
        except Exception as error:
            raise _InboundEventInsertError(error) from error
        if not first_time:
            self._audit.record(
                "inbound_event.duplicate",
                event_id=message.event_id,
                trace_id=message.trace_id,
            )
            return False
        return True

    def _handle_not_provisioned(
        self, tx, message: InboundMessage, deferred: list[RenderedContent]
    ) -> Outcome:
        """未开通用户：先看管理命令面，再过内测名单闸，最后才认领开通。

        登记表里当前有效的管理员发来的私聊文本改道进入管理命令面，完全不进入自动
        开通这条链；真正的判定发生在路由内部的实时读表，这里只按结果分流。名单闸
        放在发「正在核对」之前——名单外（含未装配、名单为空）一律落到既有的确定性
        拒绝出口，零建档、零开通派发。合同：未开通用户发来的业务内容不进入问数、
        不保存也不回显（`V-审计-05`），因此审计里同样不带消息正文。
        """
        if self._try_admin_route(tx, message, deferred):
            return Outcome(handled_as=HandledAs.COMMAND)

        if self._onboarding is None:
            # 未配置正向编排的旧装配仍然保持明确的否定终态。
            self._audit.record(
                "inbound_event.not_provisioned",
                event_id=message.event_id,
                trace_id=message.trace_id,
            )
            tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.NOT_PROVISIONED)
            return Outcome(handled_as=HandledAs.NOT_PROVISIONED)

        roster_gate = self._gates.innertest_roster_gate
        if roster_gate is not None and not roster_gate(message.sender_open_id):
            deferred.append(self._texts.catalog.text("onboarding.innertest_not_open"))
            self._audit.record(
                "onboarding.innertest_roster_rejected",
                event_id=message.event_id,
                trace_id=message.trace_id,
            )
            tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.DROPPED)
            return Outcome(handled_as=HandledAs.DROPPED)

        self._audit.record(
            "inbound_event.auto_provisioning",
            event_id=message.event_id,
            trace_id=message.trace_id,
        )
        tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.AUTO_PROVISIONING)
        return Outcome(handled_as=HandledAs.AUTO_PROVISIONING)

    def _handle_onboarding_in_flight(
        self, tx, message: InboundMessage, user, deferred: list[RenderedContent]
    ) -> Outcome:
        """开通正在进行中，用户又发了一条：只回同步中的固定提示。

        合同：「权限同步期间，卡片明确显示『权限正在同步，预计最多需要十五分钟』，
        用户无需重复开通」。**不重新触发编排**（那一条正在别处跑），也不入队。
        """
        deferred.append(self._texts.catalog.text("onboarding.matched"))
        self._audit.record(
            "inbound_event.onboarding_in_flight",
            event_id=message.event_id,
            user_id=user.user_id,
            trace_id=message.trace_id,
        )
        tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.NOT_PROVISIONED)
        return Outcome(handled_as=HandledAs.NOT_PROVISIONED)

    def _handle_suspended(
        self, tx, message: InboundMessage, user, deferred: list[RenderedContent]
    ) -> Outcome:
        """已停用用户：回停用提示并丢弃这条消息。"""
        deferred.append(self._texts.suspended_content())
        self._audit.record(
            "inbound_event.suspended",
            event_id=message.event_id,
            user_id=user.user_id,
            trace_id=message.trace_id,
        )
        tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.DROPPED)
        return Outcome(handled_as=HandledAs.DROPPED)

    def _append_one_shot_notices(
        self, tx, message: InboundMessage, user, conversation, deferred: list[RenderedContent]
    ) -> None:
        """追加两条"消费一次、只提示一次"的附加回复。

        两条都**不影响**这条消息接下来按第 6～8 步的正常处理——该入队入队、该判忙碌
        判忙碌，只是额外多一条回复：

        - 上一次问数因二十四小时未获得平台回执而到期时，提示一次「请重新提问」
          （`V-投递-06` 后半句），不主动推送、不重放旧答案；
        - 预开通用户的首聊补一句。次序是硬的：**先渲染、渲染成功才消费一次性标志**
          ——先消费再渲染会在渲染失败时白白烧掉标志，这个人**永远**收不到那句话。
        """
        if tx.consume_delivery_expired_notice(conversation_id=conversation.conversation_id):
            deferred.append(self._texts.catalog.text("gateway.delivery_expired"))

        pending_notice = tx.peek_preprovision_notice(user_id=user.user_id)
        if pending_notice is None:
            return
        rendered_notice = self._onboarding_replies.render_preprovision_notice(
            pending_notice,
            event_id=message.event_id,
            user_id=user.user_id,
            trace_id=message.trace_id,
        )
        if rendered_notice is not None and tx.consume_preprovision_notice(user_id=user.user_id):
            deferred.append(rendered_notice)

    def _handle_active_message(
        self,
        tx,
        message: InboundMessage,
        user,
        conversation,
        moment: datetime,
        deferred: list[RenderedContent],
    ) -> Outcome:
        """第 6 到第 8 步：解析命令、判忙碌、入队。

        次序是合同的：``/stop`` 与 ``/memory`` 都是元数据操作，放在忙碌判定**之前**
        （`V-会话-10`）；不被认识的斜杠文本同样不受忙碌影响——这条消息不管忙不忙碌
        都不会被受理，没有理由先让用户等一轮忙碌提示再重发一遍同样会被拒的输入。
        ``/new`` 反过来被合同列入受限命令，因此排在忙碌判定**之后**（`V-会话-09`）。
        """
        command = parse_command(message.text)
        memory_command = parse_memory_command(message.text)

        if is_unrecognized_slash_message(message.text):
            return self._reject_unrecognized_slash(tx, message, user, conversation, deferred)
        if command is Command.STOP:
            return self._request_stop(tx, message, user, conversation)
        if memory_command.kind is not MemoryCommandKind.NONE or is_memory_command_message(
            message.text
        ):
            return self._memory.handle(tx, message, user, conversation, memory_command, deferred)

        if conversation.running_task_id is not None:
            # 第 7 步（`V-会话-04/09/10`）：忙碌期只回提示。合同——该消息不进入对话
            # 历史、不排队，也不会在当前任务结束后自动提交或自动生效。
            deferred.append(self._busy_hint_for(conversation))
            tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.BUSY_HINT)
            return Outcome(handled_as=HandledAs.BUSY_HINT)

        if command is Command.NEW:
            return self._start_new_session(tx, message, user, conversation, deferred)
        if message.message_type != TEXT_MESSAGE_TYPE:
            return self._drop_unsupported_type(tx, message, user)
        return self._enqueue(
            tx,
            message,
            user_id=user.user_id,
            conversation=conversation,
            now=moment,
            deferred=deferred,
        )

    def _reject_unrecognized_slash(
        self, tx, message: InboundMessage, user, conversation, deferred: list[RenderedContent]
    ) -> Outcome:
        """以 ``/`` 开头但不被认识的文本：直接回绝，不入队（`V-会话-05/06/11`）。

        执行层（Agent SDK 底下的 CLI）会把这类文本解析成系统斜杠命令而不是用户
        问题：``/config`` ``/model`` ``/help`` 令会话在一两秒内瞬断，``/loop`` 会让
        模型尝试调用内部工具。只看整条消息去掉首尾空白后的第一个字符，句子中间的
        ``/``（日期、URL）不受影响；``/new`` ``/stop`` 由整条匹配语义排除在外。
        管理员的命令面分流发生在更早的第 4/5 步，到这里发送者已经确认是普通用户。
        """
        deferred.append(self._texts.catalog.text("gateway.slash_rejected"))
        self._audit.record(
            "command.unsupported_slash",
            event_id=message.event_id,
            user_id=user.user_id,
            conversation_id=conversation.conversation_id,
            trace_id=message.trace_id,
        )
        tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.COMMAND)
        return Outcome(handled_as=HandledAs.COMMAND)

    def _request_stop(self, tx, message: InboundMessage, user, conversation) -> Outcome:
        """``/stop``：忙碌时照常受理（`V-会话-10`），不回「当前任务仍在处理中」。"""
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

    def _start_new_session(
        self, tx, message: InboundMessage, user, conversation, deferred: list[RenderedContent]
    ) -> Outcome:
        """空闲时的 ``/new``：清空当前话题的上下文，其他话题不受影响。

        清空本身**再判一次忙碌**：上面那次判定读的是事务开始时的快照，另一条连接
        可能在这中间抢占成功并已经在跑。条件更新影响 0 行就说明话题已经忙了，走
        忙碌分支——否则会把一个正在执行的任务的上下文清掉。这条竞态下抢占方的任务
        状态从未被本次事务读到，无法诚实判定"排队中"还是"处理中"，因此保留默认的
        「处理中」文案，不去猜。
        """
        if not tx.clear_agent_session(conversation_id=conversation.conversation_id):
            deferred.append(self._texts.busy_hint_content())
            tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.BUSY_HINT)
            return Outcome(handled_as=HandledAs.BUSY_HINT)
        self._audit.record(
            "command.new",
            event_id=message.event_id,
            user_id=user.user_id,
            conversation_id=conversation.conversation_id,
            trace_id=message.trace_id,
        )
        # 合同「系统明确告诉用户已经开启新会话」：表情只表示「已收到」，这里补一条
        # 明确的文字确认，随事务提交后统一发送。
        deferred.append(self._texts.catalog.text("gateway.new_session"))
        tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.COMMAND)
        return Outcome(handled_as=HandledAs.COMMAND)

    def _drop_unsupported_type(self, tx, message: InboundMessage, user) -> Outcome:
        """非文本消息（图片、语音、富文本……）：表情已经加过，但**不入队**。

        把一条语音当成空问题排进队列，用户只会拿到一个莫名其妙的失败，还白占一次
        话题串行名额。刻意**不回复任何文案**：「是否要明确告诉用户暂不支持这种
        消息」是一条新的用户可见承诺，合同没有写，本模块不发明。位置在忙碌判定
        之后——忙碌期的非文本消息与其他消息一样只得到「当前任务仍在处理中」。
        """
        self._audit.record(
            "message.unsupported_type",
            event_id=message.event_id,
            user_id=user.user_id,
            message_type=message.message_type,
            trace_id=message.trace_id,
        )
        tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.DROPPED)
        return Outcome(handled_as=HandledAs.DROPPED)

    # ------------------------------------------------------------------
    # 提交之后：开通编排与出站回复
    # ------------------------------------------------------------------

    def _start_onboarding(self, message: InboundMessage, deferred: list[RenderedContent]) -> None:
        """提交后启动一次自动开通，并把结果限制在内容目录内。

        事务只负责认领 ``event_id``；身份读取、匹配、开通和 MCP 同步由编排自己用
        独立的幂等边界完成。这样 gateway 不会把长耗时外部调用放进队列事务，也不会
        把用户原文传入权限链。
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
            rendered = self._onboarding_replies.render_result(
                result, checking_key=checking.key, message=message
            )
        except Exception as error:
            self._report_onboarding_failure(message, error, deferred)
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
        if result.state is OnboardingState.STARTED:
            # **``started`` 的账由编排自己记。** 它表示编排已经异步接手、结论还没有
            # 产生；这里就记成"已交接"，会让一次跑到一半的崩溃变成谁都不会再看的
            # 悬空状态——对账扫描被账本挡在门外，用户永远停在「正在核对」。同步返回
            # 终态的编排不受影响：它们的结论此刻已经产生，账照记。
            return
        self._mark_onboarding_dispatched(message)

    def _report_onboarding_failure(
        self, message: InboundMessage, error: Exception, deferred: list[RenderedContent]
    ) -> None:
        """编排本身抛异常或返回坏结果：回冻结的内部故障文案并把事件记成已交接。

        **这条分支在生产不可达**：gateway 装配期硬性只接受一种恒定返回"已接手"且
        永不抛异常的惰性编排实现，真正会失败的编排结构上装不到这条管线上。因此这里
        没有接管理员告警回调；gateway 侧若未来装配真实编排，须同步在这里补上告警。
        编排确实被调用过，账必须记上——不记的话对账扫描会把一条已经得到冻结失败终态
        的事件再交接一次，用户会收到第二遍同样的内部故障提示。
        """
        deferred.append(
            self._texts.catalog.text("onboarding.internal_error", reference=message.trace_id)
        )
        self._audit.record(
            "onboarding.failed",
            event_id=message.event_id,
            state=OnboardingState.INTERNAL_ERROR.value,
            error=type(error).__name__,
            trace_id=message.trace_id,
        )
        self._mark_onboarding_dispatched(message)

    def _mark_onboarding_dispatched(self, message: InboundMessage) -> None:
        """记账：这条事件已经交给开通编排了。

        **失败只记审计，绝不向上抛。** 记不上账的最坏后果是对账扫描过一会儿再交接
        一次，而 ``OnboardingRunner.start`` 按合同幂等；反过来，让一次已经拿到结论的
        开通因为一条簿记 ``UPDATE`` 失败而炸掉，会把用户可见的终态提示也一起带走。
        旧注入 store 没有这个方法时同样落进这里（``AttributeError``），行为一致。

        动作名带 ``failed`` 后缀，让审计实现把它升到 ``WARNING``：这是一次真实的
        数据库写失败，淹没在 INFO 流水里等于没记。
        """
        try:
            self._store.mark_onboarding_dispatched(event_id=message.event_id)
        except Exception as error:
            self._audit.record(
                "onboarding.dispatch_record_failed",
                event_id=message.event_id,
                error=type(error).__name__,
                trace_id=message.trace_id,
            )

    def _busy_hint_for(self, conversation) -> RenderedContent:
        """按话题当前占用任务的真实阶段选忙碌文案。

        只信 ``running_task_status == "queued"`` 这一个精确值：其余取值共同的事实是
        "已经有人在处理这个任务，不是单纯在队列里等"，沿用「处理中」文案不算说谎；
        读不到状态时同样保守地落回这一桶，不对不认识的取值猜成"排队中"。
        """
        if conversation.running_task_status == "queued":
            return self._texts.busy_hint_queued_content()
        return self._texts.busy_hint_content()

    def _add_reaction(self, message: InboundMessage) -> None:
        """第 3 步。失败只记审计，绝不向上抛（`V-接入-08`）。

        捕获 ``Exception`` 是刻意的：这一步的产品语义就是"尽力而为"，任何失败形态
        都不该改变后续处理。收窄成某几个异常类型，等于让没预料到的失败形态重新
        获得阻断后续处理的能力。
        """
        try:
            self._reactions.add(message_id=message.message_id)
        except Exception as error:
            self._audit.record(
                "reaction.failed",
                event_id=message.event_id,
                message_id=message.message_id,
                error=f"{type(error).__name__}: {error}",
                trace_id=message.trace_id,
            )

    def _try_admin_route(
        self, tx, message: InboundMessage, deferred: list[RenderedContent]
    ) -> bool:
        """尝试把这条私聊文本消息交给管理命令面；两个调用点共用这一份接线。

        Returns:
            ``True`` 表示已经处理完（回执、审计、事件终态都已落好，调用方直接返回命令
            终态）；``False`` 表示未命中——非文本消息、未装配路由，或登记表判定不通过
            ——调用方按各自的下一步兜底继续，这里不产生任何副作用。
        """
        router = self._gates.admin_router
        if router is None or message.message_type != TEXT_MESSAGE_TYPE:
            return False
        admin_outcome = router.route(
            open_id=message.sender_open_id,
            text=message.text,
            trace_id=message.trace_id,
            # 写命令要把确认卡片回复到触发它的那条私聊消息上，需要这三个字段；
            # 只读命令忽略它们。
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
        )
        if not admin_outcome.handled:
            return False
        deferred.append(
            RenderedContent(
                key=admin_outcome.content_key,
                version=admin_outcome.content_version,
                text=admin_outcome.reply_text,
            )
        )
        self._audit.record(
            "inbound_event.admin_command",
            event_id=message.event_id,
            trace_id=message.trace_id,
        )
        tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.COMMAND)
        return True

    def _route_delegated_subject(
        self, tx, message: InboundMessage, deferred: list[RenderedContent]
    ) -> Outcome:
        """第 4 步命中专用授权主体之后的分流：管理命令面，或确定性拒绝出口。

        **绝无业务路径**——不查状态、不进开通、不入队。先试管理命令面（登记表实时
        判定，命中即回话）；未命中则回落到确定性拒绝文案。该文案与开通链里的同一份
        产品文案同源，只是此前只能异步经开通链才能触达；专用主体既然不再进入开通链，
        这条文案就必须由本模块直接同步发出，不能再指望开通链替它发。
        """
        if self._try_admin_route(tx, message, deferred):
            return Outcome(handled_as=HandledAs.COMMAND)

        deferred.append(self._texts.catalog.text("onboarding.delegated_subject"))
        self._audit.record(
            "inbound_event.delegated_subject_rejected",
            event_id=message.event_id,
            trace_id=message.trace_id,
        )
        tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.DROPPED)
        return Outcome(handled_as=HandledAs.DROPPED)

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
        """第 8 步：抢占话题并插入任务，成功后 NOTIFY（`V-队列-01…05`）。

        抢占与入队同事务：抢不到即忙碌（`V-会话-01`）；抢到之后任何失败都会让抢占
        随事务一起回滚，话题不会永久忙碌（`V-队列-02`）。抢占竞态下抢占方的任务
        状态从未被本次事务读到，因此保留默认的「处理中」文案。

        Raises:
            QueueInsertError: 任务插入失败，调用方据此整体回滚并回一条诚实提示。
        """
        task_id = new_id("tsk")
        if not tx.claim_conversation(conversation_id=conversation.conversation_id, task_id=task_id):
            deferred.append(self._texts.busy_hint_content())
            tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.BUSY_HINT)
            return Outcome(handled_as=HandledAs.BUSY_HINT)

        resumed, session_rotated = self._resolve_session_reuse(tx, conversation, now)
        # 目标 worker 版本同样在入队时求值一次并写入（`V-灰度-01`）。重试、重启、
        # 心跳超时回收都不得改写它——数据库触发器兜底。
        version = self._resolve_version(user_id=user_id, now=now)
        self._insert_queued_task(
            tx,
            message,
            task_id=task_id,
            user_id=user_id,
            conversation_id=conversation.conversation_id,
            resumed=resumed,
            version=version,
        )

        if session_rotated:
            # 追加在入队**成功之后**：只有真正生效的那一步才追加它的文案，用户不会
            # 收到一条"已经换新会话了"却其实没有任何任务在跑的告知。
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

    def _insert_queued_task(
        self,
        tx,
        message: InboundMessage,
        *,
        task_id: str,
        user_id: str,
        conversation_id: str,
        resumed: bool,
        version: str,
    ) -> None:
        """写入任务行、标记事件终态并 NOTIFY。

        Raises:
            QueueInsertError: 插入失败——包一层让调用方整体回滚，事务外只做一次
                失败提示，而不是让驱动异常穿到管线顶层的通用兜底里。
        """
        try:
            tx.insert_task(
                task_id=task_id,
                conversation_id=conversation_id,
                user_id=user_id,
                inbound_event_id=message.event_id,
                prompt=message.text,
                resumed_session=resumed,
                target_worker_version=version,
                reply_to_message_id=message.message_id,
            )
        except Exception as error:
            raise QueueInsertError("task insert failed") from error
        tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.TASK_QUEUED)
        tx.notify_task_queued()

    def _resolve_session_reuse(self, tx, conversation, now: datetime) -> tuple[bool, bool]:
        """判定本次任务是否续用旧会话，并顺手清掉已经判废的旧会话。

        续用判定发生在**入队时**并落库（`V-会话-08`），排队多久都不再改变它，且只读上一次
        任务的结束时刻（`V-会话-03`）。判废即清：只发提示不清旧会话时，轮换后的首个任务一旦
        失败就会刷新那个时刻却不写新会话标识，下一条消息落回窗口内、把早已判废的旧会话当作
        可续用。
        Returns:
            ``(是否续用旧会话, 是否需要告知用户已换新会话)``。后者只对「两小时空闲后自然
            开的新会话」为真——首次提问没有上下文，``/new`` 之后用户刚收到过确认。
        """
        resumed = should_resume_session(
            last_task_ended_at=conversation.last_task_ended_at,
            agent_session_id=conversation.agent_session_id,
            now=now,
        )
        session_rotated = (
            not resumed
            and bool(conversation.agent_session_id)
            and conversation.last_task_ended_at is not None
        )
        if not resumed and conversation.agent_session_id:
            tx.discard_stale_agent_session(conversation_id=conversation.conversation_id)
        return resumed, session_rotated
