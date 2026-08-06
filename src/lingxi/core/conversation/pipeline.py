"""gateway 事件处理管线：[接口设计 3.2](../../../../docs/技术设计/接口设计.md) 的处理次序。

**次序本身是合同要求，不能重排。** 这个模块的全部价值就是把那张次序表变成可判定的代码，
因此每一步上方都标了它对应的断言编号；调换任意两步都应当有用例变红。

    1. 通道级认证        —— 长连接握手期完成，不在逐事件层做（见下方「关于第 1 步」）
    2. event_id 落库     冲突 → 重复投递，直接返回成功        V-接入-01/02/09
    3. 加表情            失败 → 记审计，继续                  V-接入-07/08
    4. 查用户状态        未开通 → 内容一律丢弃；已停用 → 回提示 V-审计-05
    5. 解析命令          /stop /new                            V-会话-05/06
    6. 话题忙碌判定      忙碌 且 非 /stop → 只回提示，不入队    V-会话-04/06a
    7. 入队 + NOTIFY                                           V-队列-01…05

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
from dataclasses import dataclass
from datetime import datetime, timezone

from lingxi.core.ids import new_id

from .commands import Command, parse_command
from .ports import (
    AuditSink,
    GatewayStore,
    HandledAs,
    InboundMessage,
    Outcome,
    Reactions,
    Replies,
    UserState,
    VersionResolver,
)
from .session_window import should_resume_session

logger = logging.getLogger(__name__)

# 合同「问数与多轮对话」逐字给出的提示语，不是本实现发明的文案：
# 「除 /stop 外的后续消息（包括 /new）只收到"当前任务仍在处理中"的提示」。
BUSY_HINT_TEXT = "当前任务仍在处理中"

# #45 的分流规则形态属 S11（决策第 3 条）。本批最小实现固定 stable，
# 断言只约束「入队时固化」与「领取带版本条件」两件事。
DEFAULT_WORKER_VERSION = "stable"


def fixed_stable_version(*, user_id: str, now: datetime) -> str:
    """默认版本求值：恒为 ``stable``。签名保留 #45 要求的两个输入。"""

    return DEFAULT_WORKER_VERSION


@dataclass(frozen=True)
class GatewayTexts:
    """用户可见文案。

    ``busy_hint`` 来自合同原文。``suspended`` **没有**合同原文——接口设计 3.2 只写了
    「回复停用说明」，具体措辞是尚未拍板的用户可见承诺，因此开成可注入字段并在
    Issue 里登记为待产品负责人定夺项，而不是在代码里把某句话固化成事实。
    """

    busy_hint: str = BUSY_HINT_TEXT
    suspended: str = "你的 Lingxi 账号当前已停用，暂时无法发起新的问数。"


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
    ) -> None:
        self._store = store
        self._reactions = reactions
        self._replies = replies
        self._audit = audit
        self._texts = texts or GatewayTexts()
        self._resolve_version = resolve_version

    def handle_message(self, message: InboundMessage, *, now: datetime | None = None) -> Outcome:
        """处理一条 ``im.message.receive_v1``。

        ``now`` 只为注入时钟开放（`V-会话-02`），正常调用不传。
        """

        moment = now or datetime.now(timezone.utc)

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
                # 本批只认领这条**否定面**——正向的自动匹配与开通接线是 #65，不在本批。
                # 注意审计里也不带消息正文：内容"不保存"包括不写进审计。
                self._audit.record(
                    "inbound_event.not_provisioned",
                    event_id=message.event_id,
                    trace_id=message.trace_id,
                )
                tx.mark_handled_as(
                    event_id=message.event_id, handled_as=HandledAs.AUTO_PROVISIONING
                )
                return Outcome(handled_as=HandledAs.AUTO_PROVISIONING)

            assert user is not None  # NOT_PROVISIONED 已在上一分支返回

            if state is UserState.SUSPENDED:
                self._replies.send_text(
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    text=self._texts.suspended,
                )
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

            # —— 第 5 步：解析命令。在忙碌判定**之前**，因为 /stop 不受忙碌拦截。
            command = parse_command(message.text)

            # —— 第 6 步：忙碌判定。
            busy = conversation.running_task_id is not None

            if command is Command.STOP:
                # `V-会话-06a`：3.2 第 6 步的条件是「忙碌 **且非 /stop**」，
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
                # 分支在 /new 之前（`V-会话-05a`）：忙碌时的 /new 不清空上下文。
                self._replies.send_text(
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    text=self._texts.busy_hint,
                )
                tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.BUSY_HINT)
                return Outcome(handled_as=HandledAs.BUSY_HINT)

            if command is Command.NEW:
                # 空闲时的 /new：立即清空当前对话上下文，其他话题不受影响
                # （条件写在 conversation_id 上，天然只影响这一行）。
                tx.clear_agent_session(conversation_id=conversation.conversation_id)
                self._audit.record(
                    "command.new",
                    event_id=message.event_id,
                    user_id=user.user_id,
                    conversation_id=conversation.conversation_id,
                    trace_id=message.trace_id,
                )
                tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.COMMAND)
                return Outcome(handled_as=HandledAs.COMMAND)

            # —— 第 7 步：入队。
            return self._enqueue(tx, message, user_id=user.user_id, conversation=conversation, now=moment)

    # ------------------------------------------------------------------
    # 内部步骤
    # ------------------------------------------------------------------

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

    def _enqueue(self, tx, message: InboundMessage, *, user_id: str, conversation, now: datetime) -> Outcome:
        task_id = new_id("tsk")

        # 抢占与入队同事务：抢不到即忙碌（`V-会话-01`）；抢到之后任何失败都会让
        # 抢占随事务一起回滚，话题不会永久忙碌（`V-队列-02`）。
        if not tx.claim_conversation(
            conversation_id=conversation.conversation_id, task_id=task_id
        ):
            self._replies.send_text(
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                text=self._texts.busy_hint,
            )
            tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.BUSY_HINT)
            return Outcome(handled_as=HandledAs.BUSY_HINT)

        # 续用判定发生在**入队时**并落库（`V-会话-02a`）：排队多久都不再改变它。
        # 只读 last_task_ended_at，读不到任务开始时间或时长（`V-会话-03`）。
        resumed = should_resume_session(
            last_task_ended_at=conversation.last_task_ended_at,
            agent_session_id=conversation.agent_session_id,
            now=now,
        )
        # 目标 worker 版本同样在入队时求值一次并写入（`V-灰度-01`）。
        # 重试、重启、心跳超时回收都不得改写它——数据库触发器兜底。
        version = self._resolve_version(user_id=user_id, now=now)

        tx.insert_task(
            task_id=task_id,
            conversation_id=conversation.conversation_id,
            user_id=user_id,
            inbound_event_id=message.event_id,
            prompt=message.text,
            resumed_session=resumed,
            target_worker_version=version,
        )
        tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.TASK_QUEUED)
        tx.notify_task_queued()

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
