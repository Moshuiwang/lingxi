"""``lingxi-gateway`` 进程：飞书长连接接入与任务入队。

[代码框架第一节](../../../../docs/技术设计/代码框架.md)登记的四进程之一，本切片
（Issue #57，S4 前半）建立它。``apps/`` 只做组装：读配置、建连接、把 adapters 注入
core、处理信号与退出，不写业务规则——处理次序住在
``lingxi.core.conversation.pipeline``，协议细节住在 ``lingxi.adapters``。

**这个进程不监听任何入站端口**（`V-接入-10`）。事件的唯一来源是那条已认证的长连接：
``LongConnectionSupervisor`` 从 ``transport.stream()` 拿事件，进程内没有第二个投递入口，
也不 ``bind`` / ``listen`` 任何套接字。接口设计 3.2 第 1 步的"验签"在长连接语义下由
握手期的通道级认证承担，逐条事件上没有可验的签名。

停机语义（`V-部署-03`）：收到 ``SIGTERM`` 后停止接收新事件、把在途事件处理完（它们
各自是一个数据库事务，要么落库要么整体回滚）、在超时内退出。信号处理器只 ``set``
一个 ``Event``，不做任何 I/O。
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping
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
from lingxi.adapters.feishu_longconn import (
    BackoffPolicy,
    LongConnectionSupervisor,
    TerminationReason,
)
from lingxi.apps.liveness import touch_liveness
from lingxi.core.admin.card_dispatch import ManagementCardContext, management_card_fingerprint
from lingxi.core.admin.management_card import (
    ADMIN_ACTION_CANCEL,
    ADMIN_ACTION_GRANT,
    ADMIN_ACTION_REVOKE,
    GRANT_SUBMIT_BUTTON_NAME,
    render_management_card,
)
from lingxi.core.conversation.pipeline import EventPipeline
from lingxi.core.conversation.ports import OnboardingResult, OnboardingRunner, OnboardingState
from lingxi.core.execution.card_stream import CardCreated, CardTransport, DeliveryRejected
from lingxi.core.permission.targeted_recompute import RecomputeKind

from .config import GatewayConfig, GatewayConfigError, load_config
from .delivery import LOOP_ALERT_TRACE_ID
from .document_delivery import (
    LOOP_ALERT_TRACE_ID as DOCUMENT_DELIVERY_LOOP_ALERT_TRACE_ID,
)
from .document_delivery import (
    assemble_document_delivery_consumer,
)
from .group_mention_hint import GroupMentionHintResponder, build_group_mention_hint_throttle
from .log_redaction import install_credential_redaction
from .management_status import (
    PUBLISHING_STATUS_TEXT,
    rendered_dispatch_status,
    skipped_recompute_status_message,
)
from .onboarding import assert_gateway_onboarding_is_inert

logger = logging.getLogger(__name__)

#: 管理群终态通知（Issue #96 S-M-02）专用去重前缀——与花名册日报
#: （``DELIVERY_UUID_PREFIX``）、内测每日通报（``DAILY_REPORT_UUID_PREFIX``）共用
#: 同一个 `im/v1/messages` 接口，但是另一条独立投递语义，必须使用自己的前缀（同一
#: 纪律见 ``adapters/feishu_group_message.py`` 的 ``uuid_prefix`` 参数文档）。命名
#: 以 ``_UUID_PREFIX`` 结尾，落在 ``tests/test_scheduler_daily_report_assembly.py``
#: 的 AST 预算扫描范围内（外部审查交叉裁定，opus P2-3：此前是函数体内联字符串，
#: 不受该预算测试覆盖）。取值 13 + 32 = 45 字符，在飞书 50 字符上限内。
ADMIN_NOTICE_UUID_PREFIX = "lingxi-admin-"


def _combined_heartbeat(
    alerting_duty: Any, liveness_role: str, *, watchdog: Callable[[], None] | None = None
) -> Callable[[], None]:
    """把"记进 AlertManager"与"戳一下活性文件"合成一个心跳回调（Issue #153）。

    两件事共用同一个触发时机——都是"这条循环这一轮还活着"的证据——但服务不同的
    消费者：``AlertManager`` 供跨进程重启也能观察到的阈值/去重状态机，活性文件
    供同容器内的 ``python -m lingxi.apps.healthcheck`` 判断"主循环是否还在跳动"
    （见 ``apps/liveness.py`` 模块说明）。

    ``watchdog``（Issue #191）是搭在同一个时机上的第三件事：**这条循环还活着的
    时候，顺手确认另一条循环也还活着**。放在心跳之后调用，一次看门狗异常不会
    连累前面两件真正的心跳工作。
    """

    beat = alerting_duty.heartbeat_callback(liveness_role)

    def combined() -> None:
        beat()
        touch_liveness(liveness_role)
        if watchdog is not None:
            watchdog()

    return combined


def run_delivery_loop(
    consumer: Any,
    *,
    stop: threading.Event,
    poll_interval_seconds: float,
    heartbeat: Callable[[], None] | None = None,
    on_tick: Callable[[], None] | None = None,
    on_dead: Callable[[str], None],
) -> None:
    """投递消费后台线程的实际入口：跑循环，并保证它一旦退出一定有人知道（#191）。

    ``DeliveryConsumer.run_forever`` 自己已经把单轮异常隔离掉，不会因为一次瞬时
    数据库错误退出；但"线程没了"这件事不能只由那一层保证——解释器级错误
    （``MemoryError`` 一类 ``BaseException``）、或将来某次改动把隔离改漏，都会让
    这条线程无声消失。此前 ``main()`` 只在 ``supervisor.run()`` 返回后才 ``join``，
    于是"Gateway 照常收消息、却一条也投不出去"要等容器活性文件过期才被健康检查
    间接发现，而 plain Docker Compose 并不会因为 unhealthy 重启容器（Issue #191）。

    因此 ``on_dead`` 覆盖两条退出路径：异常退出（连 ``BaseException`` 一起）、以及
    "没有收到停机信号却正常返回"。停机信号已置位时的正常返回是**预期**退出，
    不上报——那是优雅停机，不是故障。
    """

    try:
        consumer.run_forever(
            stop=stop,
            poll_interval_seconds=poll_interval_seconds,
            heartbeat=heartbeat,
            on_tick=on_tick,
        )
    except BaseException as error:
        # 原样抛回线程：Python 会打印完整栈，排查时那份栈是唯一能定位到具体
        # 语句的证据；告警只带异常类型，不带正文。
        on_dead(type(error).__name__)
        raise
    if not stop.is_set():
        on_dead("returned_without_stop")


def delivery_thread_watchdog(
    thread: threading.Thread, *, stop: threading.Event, on_dead: Callable[[str], None]
) -> Callable[[], None]:
    """给长连接主线程用的看门狗（Issue #191）：确认投递线程还活着。

    ``main()`` 的主线程一直阻塞在 ``supervisor.run()`` 里直到停机，它没有别的机会
    发现后台线程已经死亡。长连接每一轮（含没有用户消息时的空闲心跳）都会调用一次
    心跳回调，把这次检查挂在同一个时机上：不新增线程、不新增定时器，也不改变任何
    既有的停机语义。

    只上报一次——线程死亡是不可逆状态，每一轮重复上报只会刷屏。线程还没 ``start()``
    时 ``ident`` 为 ``None``，那是"尚未开始"而不是"已经死亡"，不上报。
    """

    reported = [False]

    def check() -> None:
        if reported[0] or stop.is_set():
            return
        if thread.ident is None or thread.is_alive():
            return
        reported[0] = True
        on_dead("thread_not_alive")

    return check


def _combined_watchdog(*checks: Callable[[], None]) -> Callable[[], None]:
    """把多个看门狗检查合成一个（Issue #341 S-ES-3）：长连接主线程的心跳回调只
    接受一个 ``watchdog`` 参数，而现在最多有两条独立的后台线程（既有投递消费
    循环、新增的文档投递消费循环）各自需要被确认还活着。一次检查异常不能连累
    另一条的检查——`delivery_thread_watchdog` 返回的 ``check`` 各自已经把"只
    上报一次"的状态封装在自己的闭包里，这里只是顺序调用，不需要额外的异常隔离
    （`_combined_heartbeat` 的调用方已经把整个 ``watchdog`` 调用包在
    try/except 里）。
    """

    def check() -> None:
        for one in checks:
            one()

    return check


class _LoggingAudit:
    """审计出口的当前实现：结构化日志。

    ``audit_event`` 表属后续切片；管线只依赖 ``AuditSink`` 的签名，届时换实现不动管线。
    这里**不记录消息正文**——未开通用户的内容"不保存"包括不写进日志。

    失败类动作（``reaction.failed``、``reply.failed``、``event.handler_failed``、
    ``event.unparsable`` 等）记 ``WARNING`` 而不是 ``INFO``：S-A-07 r15/r19 真实验收
    发现「已收到」表情缺失（#175/#185）时，唯一能回答"加表情调用到底怎么失败的"
    的证据就是 ``reaction.failed`` 这一行审计——它淹没在 INFO 级正常流水里，验收
    没有捕获到，问题因此无法定位。级别只影响日志可见性，动作名与字段不变，
    重放脚本 ``_AuditCapture``（level=INFO 的 Handler）仍照常收到这些记录。

    后缀规则之外还有一个显式名单（独立审核 F5）：``message.unsupported_type``
    不以失败后缀结尾，但它是"用户发了消息却什么都没发生"的唯一入站侧证据
    （非文本消息被判不支持、不建任务）——r19 首轮误判正是这一类。名单只收
    **用户本应得到回应却什么都没发生**的动作。据此：未开通、已停用这类有明确
    用户回复的拒绝分支不在此列；``event.rejected_non_private_chat`` 也不在此列，
    但理由不同（PR #186 补审 P3-6，Issue #318 修订边界描述）——**群聊边界从
    "完全静默"收窄为"默认静默，精确 @ 机器人本身时回一句固定引导"**：绝大多数
    群聊消息（未 @、@别人、未配置机器人 open_id、或同群刚发过还在节流窗口内）
    仍然不加表情、不回复、不入队，机器人不在群里暴露除这一句固定引导之外的任何
    工作痕迹；只有精确 @ 到机器人本身且未被节流的那一条消息，才会额外触发一次
    ``event.group_mention_hint_sent``（见 ``group_mention_hint.py`` 的
    ``GroupMentionHintResponder``）。
    后者本身带用户可见回复，不是"什么都没发生"，因此也不进
    ``_EXTRA_WARNING_ACTIONS``。``event.rejected_non_private_chat`` 这条审计在
    两种情况下都照常记录，维持 ``INFO``。停机期间的 ``reply.skipped_while_
    stopping`` 属正常停机路径，同样不在此列。
    """

    _EXTRA_WARNING_ACTIONS = frozenset({"message.unsupported_type"})

    def record(self, action: str, /, **fields: object) -> None:
        promote = (
            action.endswith(("failed", "error", "unparsable"))
            or action in self._EXTRA_WARNING_ACTIONS
        )
        log = logger.warning if promote else logger.info
        log("audit %s %s", action, fields)


class _RecordingOnboarding:
    """gateway 侧的开通"编排"：**只记事件，一个外部动作都不做**。

    产品负责人 2026-08-18 裁定把首次开通编排整体移进 ``lingxi-scheduler``（决策记录见
    ``docs/决策记录/2026-08-18-首次开通编排住在scheduler.md``）。gateway 从此只做两件事：
    把首聊事件落进 ``inbound_event`` 并标成 ``auto_provisioning``（这一步由管线的事务完成），
    以及立刻回一条合同要求的「已收到，正在核对」。真正的编排由 scheduler 按
    ``claim_stale_onboarding`` 认领。

    因此本类返回 ``STARTED``：它的字面含义正是"编排已异步接手、这一轮没有别的话要说"
    （见 ``ports.OnboardingState``），而管线对 ``started`` **刻意不记账**——账本留给真正
    跑完的那一方，中途崩溃的链因此仍然可以被重新认领。

    **不是失败关闭桩。** 桩会返回 ``INTERNAL_ERROR``，让每个未开通用户当场看到
    ``LX-ONBOARD-001``；而这里的语义是"收到了、正在处理"，是真话。
    """

    def start(
        self, *, event_id: str, open_id: str, trace_id: str, claim_token: Any = None
    ) -> OnboardingResult:
        del event_id, open_id, trace_id, claim_token
        return OnboardingResult(state=OnboardingState.STARTED)


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


class _GatewayManagementCardRefresher:
    """把管理卡状态更新集中到同一个 transport + 持久 sequence 端口。

    并发保护只有 ``expected_card_sequence`` 一把 CAS（#493 双 CAS 收敛，rc25 S-4a）；
    ``state_version`` 为什么没有独有判别力，见 ``next_card_sequence()`` 的收敛说明。
    """

    def __init__(self, *, transport: Any, catalog: Any, display_names: Any, context_store: Any) -> None:
        self._transport = transport
        self._catalog = catalog
        self._display_names = display_names
        self._context_store = context_store

    def update(
        self,
        *,
        context: ManagementCardContext,
        status: Any,
        state: str,
        dispatch_status: str | None = None,
        status_message: str | None = None,
        expected_card_sequence: int | None = None,
    ) -> bool:
        # Use the snapshot sequence when available; legacy test doubles may omit it.
        expected_card_sequence = getattr(context, "card_sequence", None) if expected_card_sequence is None else expected_card_sequence
        # 执行已结束（已生效/不完整）后，原管理卡恢复为可重新查询/提交的表单；
        # 只有等待中的提交态继续隐藏表单，避免重复点击。取消则关闭这张卡。
        submitted = state in {"submitted", "dispatching"}
        rendered_status = rendered_dispatch_status(
            status=status,
            state=state,
            dispatch_status=dispatch_status,
            status_message=status_message,
        )
        card = render_management_card(
            status,
            display_identifier=context.identifier,
            catalog=self._catalog,
            display_names=self._display_names,
            submitted=submitted,
            dispatch_status=rendered_status,
            status_message=status_message,
            closed=state == "closed",
        )
        sequence_kwargs: dict[str, Any] = {"message_id": context.message_id}
        if expected_card_sequence is not None:
            sequence_kwargs["expected_card_sequence"] = expected_card_sequence
        sequence = self._context_store.next_card_sequence(**sequence_kwargs)
        if sequence is None:
            return False
        self._transport.update(card_id=context.card_id, sequence=sequence, card=card)
        mark_visual_refreshed = getattr(self._context_store, "mark_visual_refreshed", None)
        if callable(mark_visual_refreshed):
            mark_kwargs: dict[str, Any] = {"message_id": context.message_id, "sequence": sequence}
            if expected_card_sequence is not None:
                # 回写要 CAS 的是本次实际领到的号，不是渲染时读到的快照号。
                mark_kwargs["expected_card_sequence"] = sequence
            marked = mark_visual_refreshed(**mark_kwargs)
            if marked is False:
                return False
        return True


class _ManagementCardRecoveryScanner:
    """用持久 ``needs_refresh`` 水位恢复管理卡最终视觉状态。

    管理卡状态写库与 CardKit 更新是两个外部系统，不能由进程内 observer 维持
    一致。scanner 在 gateway 启动时先跑一次，之后由长连接心跳按短间隔惰性触发；
    失败只留下水位并等待下一轮，成功则由 refresher 在 CardKit 返回后清水位。
    因而重启、短暂 CardKit 故障和重复扫描都收敛到同一条幂等路径，不会产生第二张
    卡或第二条业务投递。
    """

    def __init__(
        self,
        *,
        context_store: Any,
        refresher: Any,
        status_lookup: Callable[[str], Any],
        audit: Any,
        interval_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._context_store = context_store
        self._refresher = refresher
        self._status_lookup = status_lookup
        self._audit = audit
        self._interval_seconds = max(0.1, float(interval_seconds))
        self._clock = clock
        self._next_scan_at = 0.0

    @staticmethod
    def _dispatch_status_for(context: ManagementCardContext) -> str | None:
        if context.dispatch_status in {"publishing", "effective", "incomplete"}:
            return context.dispatch_status
        if context.state in {"dispatching", "submitted"}:
            return "publishing"
        if context.state == "effective":
            return "effective"
        if context.state == "incomplete":
            return "incomplete"
        return None

    def scan(self) -> int:
        try:
            contexts = self._context_store.list_needing_refresh(limit=20)
        except Exception as error:  # noqa: BLE001 - preserve watermark for retry
            self._audit.record(
                "admin.management_card.recovery_scan_failed", error=type(error).__name__
            )
            return 0
        recovered = 0
        for context in contexts:
            try:
                status = self._status_lookup(context.identifier)
                if status is None:
                    continue
                dispatch_status = self._dispatch_status_for(context)
                refreshed = self._refresher.update(
                    context=context,
                    status=status,
                    state=context.state,
                    dispatch_status=dispatch_status,
                )
                if refreshed is False:
                    continue
                recovered += 1
            except Exception as error:  # noqa: BLE001 - retry this row next scan
                self._audit.record(
                    "admin.management_card.recovery_refresh_failed",
                    error=type(error).__name__,
                    message_id=getattr(context, "message_id", ""),
                )
        self._next_scan_at = self._clock() + self._interval_seconds
        return recovered

    def scan_if_due(self) -> int:
        now = self._clock()
        if now < self._next_scan_at:
            return 0
        return self.scan()


# This is an internal observer window, not a product promise.  The administrator sees
# the truthful "正在下发" state while the scheduler consumes the outbox; after the
# observer gives up, the persisted incomplete state is corrected by the daily batch.
_MANAGEMENT_PUBLISH_OBSERVE_SECONDS = 60.0
_MANAGEMENT_PUBLISH_POLL_SECONDS = 1.0


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


def install_signal_handlers(
    stop_event: threading.Event, *, on_stop: Callable[[], None] | None = None
) -> None:
    """``SIGTERM`` / ``SIGINT`` 只置一个标志位（外加可选的一次性回调）。

    处理器里不做 I/O：信号可能落在任意一条语句之间，在那里写库或发网络请求会把
    "停机"变成一个新的故障源。``on_stop``（独立审核 P3-5）只做一件轻量的事——
    记一个内存里的时间戳，供 ``main()`` 精确计量"停机预算从信号到达那一刻起
    用掉了多少"，不是新的 I/O 故障源，风险与 ``stop_event.set()`` 本身同级。
    """

    def handler(signum: int, _frame: Any) -> None:
        logger.info("收到信号 %s，开始停机", signum)
        stop_event.set()
        if on_stop is not None:
            on_stop()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def build_supervisor(
    config: GatewayConfig,
    *,
    transport: Any = None,
    should_stop: Callable[[], bool] | None = None,
    onboarding: OnboardingRunner | None = None,
    heartbeat: Callable[[], None] | None = None,
    on_onboarding_assembled: Callable[[OnboardingRunner], None] | None = None,
) -> LongConnectionSupervisor:
    """按配置装出一个 supervisor。

    ``transport`` 和 ``onboarding`` 都是注入口：真实长连接与真实身份/权限外部链属
    L4a，全部 L2/L3 断言注入假实现。``onboarding`` 未提供时采用失败关闭 runner，
    不会把未开通正文悄悄交给下游，也不会误报已开通。
    adapters 在函数体内延迟 import，与 ``apps/scheduler`` 的 ``build_loop`` 同惯例。

    ``on_onboarding_assembled`` 回调**只报告本函数最终采用的那个 runner**，供装配层做
    开通装配断言（``apps/gateway/onboarding.assert_gateway_onboarding_is_inert``）。
    为什么要一个回调而不是从返回的 supervisor 上读回来：``LongConnectionSupervisor``
    的公开面被 `V-接入-10` 的结构断言冻结成 ``{run, reconnect_attempts,
    observed_delays}``——它不得出现第二个可以投递事件的公开入口，因此也不该为了装配
    自证而长出新的公开属性。回调不接触事件通路，只把"本函数决定用哪一个"这件事说出来，
    而那正是断言要验的东西（缺省落桩就发生在这一行）。
    """

    from lingxi.adapters.admin_post_callback import BackgroundPostCallbackExecutor
    from lingxi.adapters.admin_registry import PostgresAdminQueries, PostgresAdminRegistryLookup

    # 刻意从 `delegated_subject_lookup`（不是 `delegated_credentials`）导入：后者
    # 的其它函数用到 cryptography（Fernet 密文读写），而 `pyproject.toml` 的
    # `gateway` extras 组明确不含 cryptography——gateway 不碰 Fernet（2026-08-18
    # 裁定，首次开通编排住在 scheduler）。两个模块名对同一个只读查询各自的取舍
    # 见 `adapters/delegated_subject_lookup.py` 模块文档。
    from lingxi.adapters.delegated_subject_lookup import registered_delegated_subject_open_id
    from lingxi.adapters.feishu_admin_card import (
        LarkAdminCardTransport,
        LarkAdminManagementCardTransport,
        TomlCompanyMetricCatalog,
    )
    from lingxi.adapters.feishu_group_message import FeishuGroupMessages
    from lingxi.adapters.feishu_longconn import LarkEventTransport
    from lingxi.adapters.feishu_outbound import LarkReactions, LarkReplies, build_client
    from lingxi.adapters.postgres_conversation import PostgresGatewayStore
    from lingxi.adapters.postgres_management_card_context import (
        PostgresManagementCardContextStore,
    )
    from lingxi.adapters.postgres_pending_action import PostgresPendingActionStore
    from lingxi.adapters.postgres_permission_recompute_trigger import (
        BackgroundPermissionRecomputeTrigger,
        PermissionRecomputeAdapter,
    )
    from lingxi.core.admin.card_callback import AdminCardCallbackHandler
    from lingxi.core.admin.card_dispatch import ConfirmCardDispatcher, ManagementCardDispatcher
    from lingxi.core.admin.router import AdminCommandRouter
    from lingxi.core.identity.innertest_roster_gate import is_open_id_innertest_allowed

    audit = _LoggingAudit()
    effective_onboarding = onboarding or _RecordingOnboarding()
    if on_onboarding_assembled is not None:
        on_onboarding_assembled(effective_onboarding)

    # 出站 HTTP 的超时从停机预算里分配，而不是用 SDK 的 30 秒默认值——后者比预算
    # 本身还长，一次卡住的加表情或回复就能让停机超出承诺（codex 二轮 P1-C）。取
    # 四分之一：一条事件最多经历「加表情 + 一次回复」两次出站，各留一份余量。
    # 提前到这里构造（此前紧跟在 pipeline 之前）：确认卡片（Issue #96 S-M-02）
    # 与业务问数共用同一个 SDK 客户端实例，不为两个用途各建一份。
    outbound_timeout = max(1.0, config.shutdown_timeout_seconds / 4)
    client = build_client(
        app_id=config.app_id,
        app_secret=str(config.app_secret),
        timeout_seconds=outbound_timeout,
    )

    # 管理命令面（Issue #95 S-M-01）：无条件装配，不受任何 feature flag 控制——安全
    # 落点在数据判定，不在装配开关。登记表为空（尚未播种，例如新环境或 biai-stage
    # 升级前）时 ``active_entry`` 对任何 open_id 都返回 None，`route()` 恒
    # ``handled=False``，管线原样落回既有业务/专用账号提示分支，行为与完全不装配
    # 这个参数时逐字节一致。查询端口各自开自己的连接（与 ``registered_
    # delegated_subject_open_id`` 同型），不共享 pipeline 自己的 ``PostgresGatewayStore``
    # 事务——管理查询是只读的，不需要参与入站事件那个写事务。
    admin_registry_lookup = PostgresAdminRegistryLookup(
        str(config.postgres_dsn), timeouts=config.postgres_timeouts
    )
    # 待确认操作（Issue #96 S-M-02）：confirm() 在确认时刻重新读一次登记表（合同
    # "确认时重新读取……当前角色"），但不复用 admin_registry_lookup 这个独立查询口
    # ——那条查询走另一条连接，读到的角色不受任何行锁保护，会在"读到角色"与"提交
    # 这次确认"之间留出一个 TOCTOU 窗口（外部审查交叉裁定，codex P1-4）。
    # PostgresPendingActionStore 自己在 confirm() 的同一事务、同一连接上对
    # admin_registry 取 FOR SHARE，因此不再需要在这里注入 registry。
    pending_action_store = PostgresPendingActionStore(
        str(config.postgres_dsn),
        timeouts=config.postgres_timeouts,
        audit=audit, metric_map_path=config.metric_map_path,
    )
    # 管理员可见展示名解析口（Trace #469 S-1）：open_id→姓名+邮箱、公司编号→
    # 中文名、指标 ID→中文别名三个真库/真配置查询集中在 ``PostgresAdminQueries``
    # 一个实例上（结构性实现 ``AdminDisplayNames``，不继承），与下面 ``queries=``
    # 复用同一个对象——两个 Protocol 的调用面不同，但没有理由为了"各自独立声明
    # Protocol"这条既有惯例而在这里也建两份连接。
    admin_display_names = PostgresAdminQueries(
        str(config.postgres_dsn), timeouts=config.postgres_timeouts
    )
    # 确认卡片的出站发送与回调后的终态更新共用同一个 CardKit 传输实例。
    admin_card_transport = LarkAdminCardTransport(client)
    confirm_card_dispatcher = ConfirmCardDispatcher(
        transport=admin_card_transport,
        tracker=pending_action_store,
        audit=audit,
        display_names=admin_display_names,
    )
    # 用户权限管理卡发送侧（#439 B 档，Trace #445 opus 审查坐实并修复）：此前
    # 只有渲染层（`core/admin/management_card.render_management_card`）接进
    # `AdminCommandRouter`，从未有任何调用点真正装配 `ManagementCardDispatcher`/
    # 发送 transport——`management_cards` 恒为 ``None``，`/admin user` 的管理卡
    # 因此结构上从未真正发出过（`AdminCommandRouter._send_management_card` 对
    # ``None`` 直接短路返回，见该方法文档）。与确认卡片共用同一个 SDK 客户端
    # 实例（同上 `confirm_card_dispatcher` 的取舍，两者生命周期相同、都随本次
    # 装配一起建立）；发送失败只降级（`ManagementCardDispatcher` 自己的姿态，
    # 见该类文档），不影响 `/admin user` 既有的文本回复这条主路径。
    # 管理卡上下文（#493）由 PostgreSQL 持久保存；发送侧登记与回调侧读取共用同一
    # 个 store，gateway 重启后仍能恢复目标、卡片实体与 sequence。
    management_card_transport = LarkAdminManagementCardTransport(client)
    management_card_catalog = TomlCompanyMetricCatalog(metric_map_path=config.metric_map_path)
    # #493：message_id→目标、card_id、快照和 sequence 必须跨 gateway 重启保留，
    # 生产装配使用 PostgreSQL，而不是旧的进程内 TTL 映射。
    management_card_context_store = PostgresManagementCardContextStore(
        str(config.postgres_dsn), timeouts=config.postgres_timeouts
    )
    management_card_dispatcher = ManagementCardDispatcher(
        transport=management_card_transport,
        catalog=management_card_catalog,
        audit=audit,
        display_names=admin_display_names,
        context_store=management_card_context_store,
    )
    management_card_refresher = _GatewayManagementCardRefresher(
        transport=management_card_transport,
        catalog=management_card_catalog,
        display_names=admin_display_names,
        context_store=management_card_context_store,
    )

    def _lookup_management_status(identifier: str) -> Any:
        """按管理卡上下文中的展示标识读取当前状态。

        ``/admin user`` may have been addressed by邮箱。上下文要保留这个原始标识，
        让卡片刷新时继续显示同一输入；读数据库前则复用已有邮箱→open_id 解析，
        否则重启后/异步刷新时会把邮箱误当成 ``feishu_open_id`` 而读不到目标。
        """

        resolver = getattr(admin_display_names, "resolve_identifier", None)
        resolved = resolver(identifier=identifier) if callable(resolver) else identifier
        return admin_display_names.user_status(identifier=resolved)

    management_card_recovery = _ManagementCardRecoveryScanner(
        context_store=management_card_context_store,
        refresher=management_card_refresher,
        status_lookup=_lookup_management_status,
        audit=audit,
    )
    # 启动时先恢复一次；失败只留 needs_refresh 水位，不能阻止 gateway 建立长连接。
    # 后续每次长连接心跳由 scan_if_due() 重试，CardKit 成功后才清水位。
    management_card_recovery.scan()

    def _refresh_management_after_recompute(
        pending: Any,
        *,
        complete: bool,
        status_message: str | None = None,
        state_override: str | None = None,
    ) -> None:
        """后台重算/发布观察后把原管理卡推进到真实状态。"""

        origin_message_id = getattr(pending, "origin_card_message_id", None)
        if not origin_message_id:
            return
        try:
            context = management_card_context_store.lookup_context(message_id=origin_message_id)
            if context is None:
                return
            # 取消后即使重算线程晚到，也不能把已经关闭的管理卡重新打开；
            # 关闭是持久状态，后台结果只允许推进仍可见的卡片。
            if context.state == "closed":
                return
            status = _lookup_management_status(context.identifier)
            if status is None:
                return
            state = "effective" if complete else (state_override or "incomplete")
            if complete:
                dispatch_status = "已生效"
            elif state == "dispatching":
                dispatch_status = status_message or PUBLISHING_STATUS_TEXT
            else:
                trace = context.last_trace_id or "当前操作"
                dispatch_status = (
                    status_message
                    or f"下发未完成，最迟次日自动纠正 · 追溯号 {trace}"
                )
            machine_dispatch_status = (
                "effective"
                if complete
                else "publishing"
                if state == "dispatching"
                else "incomplete"
            )
            updated = management_card_context_store.update_state(
                message_id=origin_message_id,
                state=state,
                dispatch_status=machine_dispatch_status,
                snapshot_fingerprint=management_card_fingerprint(status),
            )
            if updated is not None:
                management_card_refresher.update(
                    context=updated,
                    status=status,
                    state=state,
                    dispatch_status=dispatch_status,
                )
        except Exception as error:  # noqa: BLE001 - result refresh is best effort
            audit.record(
                "admin.card_callback.management_card_refresh_failed",
                error=type(error).__name__,
                pending_action_id=getattr(pending, "id", ""),
            )

    def _recompute_completed(pending: Any) -> None:
        _refresh_management_after_recompute(pending, complete=True)

    def _start_management_publish_observer(pending: Any) -> None:
        """在 gateway 内部短暂观察 outbox，直到真实发布读回一致。

        定向重算只负责排出意图，不能把 ``ENQUEUED``/``REVOKED`` 直接翻译为「已生效」。
        发布消费在 scheduler 进程中完成，所以这里通过共享 PostgreSQL 状态观察结果；
        观察线程有界且 daemon 化，超时后留下的 ``incomplete`` 由每日批修正。
        """

        origin_message_id = getattr(pending, "origin_card_message_id", None)
        if not origin_message_id:
            return

        def observe() -> None:
            deadline = time.monotonic() + _MANAGEMENT_PUBLISH_OBSERVE_SECONDS
            while time.monotonic() < deadline:
                try:
                    publish_state = management_card_context_store.latest_publish_state_for_message(
                        message_id=origin_message_id
                    )
                except Exception as error:  # noqa: BLE001 - transient reads are retried
                    audit.record(
                        "admin.card_callback.management_publish_state_lookup_failed",
                        error=type(error).__name__,
                    )
                    publish_state = None
                if publish_state == "published":
                    _refresh_management_after_recompute(pending, complete=True)
                    return
                if publish_state in {"failed", "superseded"}:
                    _refresh_management_after_recompute(pending, complete=False)
                    return
                threading.Event().wait(_MANAGEMENT_PUBLISH_POLL_SECONDS)
            _refresh_management_after_recompute(pending, complete=False)

        threading.Thread(
            target=observe,
            name="lingxi-gateway-management-publish-observer",
            daemon=True,
        ).start()

    def _recompute_queued(pending: Any, outcome: Any) -> None:
        # ``UNCHANGED`` means the permission row already represented the desired
        # result and no new outbox intent was created. It is therefore effective
        # immediately; observing an unrelated older published outbox row would
        # otherwise be both racy and misleading, while waiting for the observer
        # would eventually turn a successful no-op into "未完成".
        if getattr(outcome, "kind", None) is RecomputeKind.UNCHANGED:
            _refresh_management_after_recompute(pending, complete=True)
            return
        _refresh_management_after_recompute(
            pending,
            complete=False,
            state_override="dispatching",
            status_message=PUBLISHING_STATUS_TEXT,
        )
        _start_management_publish_observer(pending)

    def _recompute_skipped(pending: Any, outcome: Any) -> None:
        """定向重算判 ``SKIPPED`` 的回执（Trace #521 F5，#493 P1-3）：这是常态出口不是故障，
        只有 ``account_not_enabled`` 有专属真话，其余跳过原因拿到 ``None``、逐字节不变仍走
        原失败文案（判据与措辞见 `.management_status`）。本地覆盖照常落库——``prepare`` 不读
        账号状态是既有产品语义，本次只是如实告知这一次不下发。"""

        _refresh_management_after_recompute(
            pending, complete=False, status_message=skipped_recompute_status_message(outcome)
        )

    def _recompute_failed(pending: Any, error: Exception | None) -> None:
        del error
        _refresh_management_after_recompute(pending, complete=False)

    def _recompute_timeout(pending: Any) -> None:
        _refresh_management_after_recompute(pending, complete=False)
        _start_management_publish_observer(pending)
    admin_router = AdminCommandRouter(
        registry=admin_registry_lookup,
        queries=admin_display_names,
        audit=audit,
        display_names=admin_display_names,
        pending_actions=pending_action_store,
        confirm_cards=confirm_card_dispatcher,
        management_cards=management_card_dispatcher,
    )
    # 管理群脱敏通知（Issue #96 S-M-02）：``admin_group_chat_id`` 未配置时——与既有
    # `admin_group_chat_id: str | None = None` 的既定取舍相同——这是"一个尚未接线
    # 的可选职责"，不让整个进程起不来；``AdminCardCallbackHandler`` 收到
    # ``group_notifier=None`` 时直接跳过群通知，不报错、不重试（V-管理-11：管理群
    # 从真实回调中只能收到脱敏通知，不能触发管理动作——本通知走
    # ``FeishuGroupMessages.send_text``，结构上只发纯文本，不支持卡片或按钮）。
    group_notifier: Any = None
    if config.admin_group_chat_id is not None:
        group_notifier = FeishuGroupMessages(
            base_url=config.feishu_base_url,
            app_id=config.app_id,
            app_secret=str(config.app_secret),
            # 与花名册日报、内测日报各自独立的 uuid 前缀同一纪律（见
            # adapters/feishu_group_message.py 的 delivery_uuid 文档）：13 字符，
            # 在全仓已钉住的 ≤18 字符预算内。
            uuid_prefix=ADMIN_NOTICE_UUID_PREFIX,
        )
    card_callback_handler = AdminCardCallbackHandler(
        pending_actions=pending_action_store,
        confirm_cards=admin_card_transport,
        group_notifier=group_notifier,
        group_chat_id=config.admin_group_chat_id,
        audit=audit,
        display_names=admin_display_names,
        # 管理卡表单提交/逐行收回（Issue #439 B 档接线）：复用已经在上面装好的
        # 同一个 AdminCommandRouter 实例——`handle_management_form_submit`/
        # `handle_management_revoke` 把管理卡交互转译成等价的 `/admin ...`
        # 命令文本，交给它的 `route()` 走全部既有写路径判定（角色核对、自我
        # 目标防呆、prepare()、确认卡发送、审计），不重新实现一遍（见
        # `core/admin/card_callback.py` `ManagementActionRouter` 文档）。
        management_actions=admin_router,
        management_context_store=management_card_context_store,
        management_state_lookup=_lookup_management_status,
        management_card_refresher=management_card_refresher,
        # 定向权限重算（Issue #438）：无条件装配，不受任何前置配置控制——它内部
        # 的每一个依赖都只需要 gateway 本来就有的 Postgres DSN 与随包发布的静态
        # 映射文件（见该适配器模块文档），失败降级回每日批,不影响确认结果本身。
        # **不直接注入 `PermissionRecomputeAdapter`**（Trace #445 opus 审查坐实
        # 并修复）：它的 `.trigger()` 有五到六次网络往返，同步调用会让回调应答
        # 等它跑完——包一层 `BackgroundPermissionRecomputeTrigger`，`trigger()`
        # 只入队立即返回，真正的重算在后台单线程里串行执行，失败仍走同一个
        # `admin.card_callback.recompute_trigger_failed` 审计姿态（见该适配器
        # 模块文档「BackgroundPermissionRecomputeTrigger」一节）。
        recompute_trigger=BackgroundPermissionRecomputeTrigger(
            PermissionRecomputeAdapter(
                str(config.postgres_dsn), timeouts=config.postgres_timeouts, audit=audit, metric_map_path=config.metric_map_path
            ),
            audit=audit,
            on_completed=_recompute_completed,
            on_queued=_recompute_queued,
            on_failed=_recompute_failed,
            on_skipped=_recompute_skipped,
            on_timeout=_recompute_timeout,
        ),
        # 应答之后才做那批网络往返（#493 块 B，见该适配器模块文档）。
        post_callback_executor=BackgroundPostCallbackExecutor(audit=audit),
    )
    # 专用主体结构性出口前置（opus P3-1）：装配期读**一次**登记表，把结果算成一个
    # 普通字符串交给管线——管线自己不再持有任何查询能力，对全体消息都只是内存
    # 比较（见 `EventPipeline.__init__` 的 `delegated_subject_open_id` 文档）。
    # `None`（登记表还没有行，或本次读取失败）时管线这一步整体是惰性的：不是
    # "失败关闭挡住所有消息"，而是"这一次没有额外的结构性防护"，退回到本项加入
    # 之前的行为——数据漂移场景本身极端罕见（结构上专用主体不该有 app_user 行），
    # 用它换取"gateway 启动不因为一次瞬时数据库故障而整体失败"更划算：真正兜底
    # 的仍然是 `V-身份-02` 的数据库触发器，这里只是纵深的一层。
    try:
        delegated_subject_open_id = registered_delegated_subject_open_id(
            str(config.postgres_dsn), timeouts=config.postgres_timeouts
        )
    except Exception as error:  # noqa: BLE001 - 见上方注释，读取失败按"本次无此防护"处理
        delegated_subject_open_id = None
        audit.record(
            "gateway.delegated_subject_lookup_failed", error=type(error).__name__
        )
    # 内测名单闸的 gateway 侧前移一份（Issue #302 S-N-01 的纵深）：装配期把已解析
    # 好的 `config.innertest_roster_open_ids` 包成判定口，不在管线里重新读环境
    # 变量或重新解析。空集合（未配置）＝对任何人返回 False，与该模块「默认关闭
    # ＝全拒」的既有语义一致。
    def innertest_roster_gate(open_id: str) -> bool:
        return is_open_id_innertest_allowed(open_id, config.innertest_roster_open_ids)

    # ``client``/``outbound_timeout`` 已在函数前部构造（供确认卡片传输复用，见上）。
    # 群聊@机器人固定引导（Issue #318）复用同一个 Replies 实现/同一个 SDK 客户端，
    # 不为这条边界分支单独开一条出站路径。无条件装配——与本文件其余「安全落点
    # 在数据判定，不在装配开关」同一姿态（见上方管理命令面同类注释）：未配置
    # `bot_open_id` 时 `GroupMentionHintResponder.maybe_respond` 对任何消息都
    # 直接返回，装配它本身不产生任何外部副作用或可观察行为变化。
    replies = LarkReplies(client)
    group_mention_hint = GroupMentionHintResponder(
        bot_open_id=config.bot_open_id,
        replies=replies,
        audit=audit,
        throttle=build_group_mention_hint_throttle(),
    )
    pipeline = EventPipeline(
        store=PostgresGatewayStore(str(config.postgres_dsn), timeouts=config.postgres_timeouts),
        reactions=LarkReactions(client),
        replies=replies,
        audit=audit,
        onboarding=effective_onboarding,
        admin_router=admin_router,
        innertest_roster_gate=innertest_roster_gate,
        delegated_subject_open_id=delegated_subject_open_id,
        # 停机时跳过尽力而为的出站回复，不让它把停机拖过预算。
        should_stop=should_stop,
    )
    def _management_card_heartbeat() -> None:
        management_card_recovery.scan_if_due()
        if heartbeat is not None:
            heartbeat()

    return LongConnectionSupervisor(
        transport=transport
        or LarkEventTransport(
            app_id=config.app_id,
            app_secret=str(config.app_secret),
            # 停机信号最晚在一个空闲轮询间隔之后被看见，因此这个间隔必须由停机
            # 超时推导，而不是取一个与超时无关的常数——否则配置里的超时就是一句
            # 没有实现的承诺（独立复查 F4）。取四分之一，给「处理完在途事件 + 退出」
            # 留余量：实际退出耗时 ≈ 轮询间隔 + 一条在途事件的处理时间。
            poll_seconds=max(0.1, config.shutdown_timeout_seconds / 4),
            # 单条事件从收到到落库有结果的上限。超过就让 SDK 向飞书回失败、由平台
            # 重投，而不是无限期占住它的接收协程。取停机超时本身：比它更长的话，
            # 一条卡住的事件就能让停机超出承诺。
            ack_timeout_seconds=config.shutdown_timeout_seconds,
            # 建连截止时间：超时未连上即判失败进重连，堵住「从未连上」的活性黑洞。
            handshake_timeout_seconds=config.shutdown_timeout_seconds,
        ),
        handle_event=make_event_handler(
            pipeline,
            audit=audit,
            card_callback_handler=card_callback_handler,
            group_mention_hint=group_mention_hint,
            management_card_context_store=management_card_context_store,
        ),
        backoff=BackoffPolicy(
            base_seconds=config.reconnect_base_seconds,
            factor=config.reconnect_factor,
            ceiling_seconds=config.reconnect_ceiling_seconds,
        ),
        audit=audit.record,
        heartbeat=_management_card_heartbeat,
    )


class _RejectingCards:
    """S-A-07 受控验收缺口专用：让配置命中的那一步卡片外发调用确定性收到
    ``DeliveryRejected``（服务端明确拒绝），用于在没有真实故障可复现的情况下证伪
    #152「关闭卡片路径 + 同话题一次完整文本终态」的降级路径（验收缺口登记于 #152、
    #154 评论 5306860510、#162 E-022）。**只在显式设置
    ``LINGXI_GATEWAY_CARD_FAILURE_INJECT`` 时才会被装配**；未设置（默认）时
    ``assemble_delivery_consumer`` 走 ``build_delivery_consumer`` 的默认参数，
    与本类加入之前的装配路径逐字节一致。

    设计取舍：只让被选中的那一步失败，未选中的步骤直通真实 transport——
    ``create`` 命中时 ``CardStream.start()`` 只会捕获 ``DeliveryRejected`` 并整体
    降级为文本兜底，``update``/``close`` 根本不会再被这次任务调用到。

    **四个值的实测语义（独立审核 P2-1 修正，覆盖此前文档的错误描述）**：

    - ``create``/``all`` 在正常单轮场景下**等价**：``all`` 虽然三步都会拒绝，但
      建卡这一步先被拒、任务立即整体降级，``update``/``close`` 根本没有机会被
      调用到；只有任务从已持久化 ``card_id`` 恢复（上一轮建卡已经成功、这一轮
      从终态更新起步）时，``all`` 才会真的命中 update/close 那一支，此时才与
      ``create`` 单独命中的效果不同。
    - **覆盖"注入发生在建卡成功之后"（终态更新阶段）降级路径的是 ``update``**，
      不是 ``all``：终态更新失败会被 ``CardStream.finish()`` 捕获并整体降级为
      文本兜底。
    - ``close`` 单独命中时**不产生降级**：终态更新已经成功、只是收尾关闭失败，
      ``CardStream.finish()`` 对这种情况刻意不触发文本兜底（否则会在卡片已经
      显示正确答案之后再发一条重复文本，见该方法文档）。``close`` 因此是
      "关闭失败不得产生第二条文本终态"这条否定断言的验收入口，不是产生降级
      的正向用例。

    选最简单、语义最清晰的实现，不建一个"命中一次之后这个任务全部转失败"的
    状态机。
    """

    def __init__(self, real: CardTransport, *, inject: str) -> None:
        self._real = real
        self._inject = inject

    def create(self, **kwargs: object) -> CardCreated:
        if self._inject in ("create", "all"):
            raise DeliveryRejected("card failure injected for acceptance (create)", code=-1)
        return self._real.create(**kwargs)  # type: ignore[arg-type]

    def update(self, **kwargs: object) -> None:
        if self._inject in ("update", "all"):
            raise DeliveryRejected("card failure injected for acceptance (update)", code=-1)
        self._real.update(**kwargs)  # type: ignore[arg-type]

    def close(self, **kwargs: object) -> None:
        if self._inject in ("close", "all"):
            raise DeliveryRejected("card failure injected for acceptance (close)", code=-1)
        self._real.close(**kwargs)  # type: ignore[arg-type]


def assemble_delivery_consumer(
    config: GatewayConfig, *, queue: Any = None, alerting_duty: Any = None
) -> Any:
    """装配投递消费循环（Issue #152）：读 outbox、驱动 CardKit 流式卡片与文本兜底。

    独立建一个飞书 SDK 客户端，而不是复用 ``build_supervisor`` 内部那一个——两者
    生命周期不同（这一个要跟着后台线程一起停），共用同一个客户端对象反而会让"谁
    负责关它"变得含糊；多一个轻量 SDK 客户端对象本身不产生任何网络连接，直到第一次
    真正调用才建立 HTTP 连接。

    ``alerting_duty``（Issue #153）不为 ``None`` 时，把它的
    ``delivery_alert_callback()`` 接到 ``DeliveryConsumer.on_alert``——这是最小告警
    装配合同点名的注入点："把 #152 的 on_alert 注入点接到真实告警路由"。

    ``config.card_failure_injection``（S-A-07 受控验收缺口）命中时，装配一个包一层
    "确定性拒绝"的 ``_RejectingCards`` 代替真实 ``LarkCardTransport``；未设置
    （默认）时这段分支完全不执行，装配结果与本开关加入之前逐字节一致。
    """

    from lingxi.adapters.feishu_outbound import build_client
    from lingxi.adapters.postgres_conversation import PostgresTaskQueue
    from lingxi.apps.gateway.delivery import build_delivery_consumer

    outbound_timeout = max(1.0, config.shutdown_timeout_seconds / 4)
    client = build_client(
        app_id=config.app_id,
        app_secret=str(config.app_secret),
        timeout_seconds=outbound_timeout,
    )

    cards: CardTransport | None = None
    if config.card_failure_injection is not None:
        from lingxi.adapters.feishu_delivery import LarkCardTransport

        # 显眼的结构化告知（S-A-07 卡片故障注入开关第 3 点）：这个开关一旦被遗忘在
        # 开启状态，生产环境里每一条问数结果都会被强制降级成文本终态——必须让它在
        # 启动日志里足够扎眼，而不是混在普通 INFO 审计日志里被忽略。默认关闭时
        # （本分支不执行）不会有这条日志，与既有装配路径完全一致。
        logger.warning(
            "gateway.delivery.card_failure_injection_enabled inject=%s "
            "此开关仅供 S-A-07 受控验收使用，默认应为关闭；如果这不是一次受控验收"
            "启动，请立即核实并清空 LINGXI_GATEWAY_CARD_FAILURE_INJECT",
            config.card_failure_injection,
        )
        cards = _RejectingCards(LarkCardTransport(client), inject=config.card_failure_injection)

    return build_delivery_consumer(
        client=client,
        queue=queue
        or PostgresTaskQueue(
            str(config.postgres_dsn),
            timeouts=config.postgres_timeouts,
            # S-H1-6（#359 根因取证方案第 2 条）：本实例只服务这一条单线程投递
            # 消费循环（见模块说明「同一进程内跑两条独立职责」），安全打开常驻
            # 轮询连接复用——`list_pending_delivery_tasks`/
            # `list_uncertain_delivery_tasks` 从此持有并复用同一条连接，不再
            # 每 poll_interval_seconds（默认 1s）新建一条物理连接。
            reuse_polling_connection=True,
        ),
        cards=cards,
        limit=config.delivery_batch_limit,
        on_alert=alerting_duty.delivery_alert_callback() if alerting_duty is not None else None,
        queue_delay_hint_seconds=config.queue_delay_hint_seconds,
    )


class _LogOnlyAlertSender:
    """gateway 未配置管理群时的告警出口：只记结构化日志，不发起网络请求。

    与 ``apps/worker/cli.py`` 的同名类同一姿态——没有配置目标群不等于告警关闭，
    只是"发送"这一步落到日志（Issue #153：合同要求"告警不可用时主流程行为有
    明确定义"，这里的定义是"继续跑，只是暂时没有群通知"）。
    """

    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None:
        del chat_id, dedupe_key
        logging.getLogger("lingxi.apps.gateway.alert").warning(text)


def build_alerting_duty(config: GatewayConfig) -> Any:
    """装配 gateway 自己的告警状态机（Issue #153）。

    gateway 与 scheduler、worker 是三个独立部署单元，进程间不直接通信，因此各自
    持有一份 ``AlertManager``——这不是重复造轮子，是"告警状态机跟着部署单元走"
    这条既有约束的自然结果（``core/alerting.py`` 模块说明）。配置了
    ``LINGXI_GATEWAY_ADMIN_GROUP_CHAT_ID`` 时真正发进管理群；没配时状态机照常
    运行（阈值、去重、恢复计时都真实生效），只是发送端退化为结构化日志。
    """

    from lingxi.core.alerting import AlertDispatcher, AlertingDuty, AlertManager

    if config.admin_group_chat_id:
        from lingxi.adapters.feishu_group_message import FeishuGroupMessages

        sender: Any = FeishuGroupMessages(
            base_url=config.feishu_base_url,
            app_id=config.app_id,
            app_secret=str(config.app_secret),
        )
        chat_id = config.admin_group_chat_id
    else:
        sender = _LogOnlyAlertSender()
        chat_id = "gateway-log-only"

    return AlertingDuty(
        manager=AlertManager(policy=config.alert_policy),
        dispatcher=AlertDispatcher(sender=sender, chat_id=chat_id, policy=config.alert_policy),
        audit=_LoggingAudit(),
    )


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    """进程入口。返回退出码。

    **同一进程内跑两条独立职责**：主线程承载长连接接入（``supervisor.run``，
    阻塞到收到停机信号）；投递消费循环（Issue #152）在一个后台线程里跑，共享同一个
    ``stop_event``。两者不共享任何可变状态——投递消费只读写数据库与飞书出站接口，
    不碰长连接管线的内存对象，因此不需要额外的跨线程同步。这是 #152 停止条件里
    "同一 Gateway 进程能否在不阻塞长连接的情况下可靠消费" 的落地方式：后台线程的
    数据库轮询与出站调用不会阻塞长连接协程接收下一条事件。

    **两条循环互相看着对方**（Issue #191）：投递线程经 ``run_delivery_loop`` 起跑，
    它的任何非预期退出都会立刻上报告警；长连接主线程每一轮心跳顺带跑一次
    ``delivery_thread_watchdog``。此前投递线程死掉之后没有任何进程内的东西会发现，
    只能等容器活性文件过期、健康检查变红，而 plain Docker Compose 不会因 unhealthy
    重启容器——用户侧看到的就是"发了没反应"。
    """

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    # Issue #176（安全）：第三方飞书 SDK 建立长连接后会以 INFO 级别打印带认证
    # 查询参数的完整 URL；必须在它有机会真正记一条日志之前就把两层脱敏都装好，
    # 因此紧跟在 basicConfig 之后、任何可能触发连接的代码之前调用。见
    # apps/gateway/log_redaction.py 模块头部说明。
    install_credential_redaction()
    try:
        config = load_config(env if env is not None else os.environ)
    except GatewayConfigError as error:
        print(f"gateway 配置不可用：{error}", file=sys.stderr)
        return 2

    stop_event = threading.Event()
    # 独立审核 P3-5：只在信号处理器里记一个时间戳，标记"停机预算从这一刻开始
    # 计"。不能用"进入 main() 之后的耗时"当近似——`supervisor.run()` 在收到
    # 停机信号之前会正常阻塞任意长时间（可能是几天），那段时间不属于停机预算。
    shutdown_requested_at: list[float | None] = [None]

    def _mark_shutdown_requested() -> None:
        if shutdown_requested_at[0] is None:
            shutdown_requested_at[0] = time.monotonic()

    install_signal_handlers(stop_event, on_stop=_mark_shutdown_requested)

    # 最小告警装配（Issue #153）：一份 AlertingDuty 服务两条循环，各自用不同的
    # 心跳/活性 key（见 build_alerting_duty、apps.liveness 的角色说明），因此
    # 任一条循环停摆都能被单独发现，不会被另一条仍然健康掩盖。
    alerting_duty = build_alerting_duty(config)
    delivery_alert = alerting_duty.delivery_alert_callback()
    delivery_death_reported: list[bool] = [False]

    def report_delivery_thread_dead(cause: str) -> None:
        """投递线程退出的唯一上报出口（Issue #191）。

        两个调用点（后台线程自己的退出路径、长连接主线程的看门狗）共用它，告警
        文案与"只报一次"的口径因此只有一份。上报之后**立刻**把告警冲出去：告警
        投递平时由投递循环的 ``on_tick`` 驱动，而此刻正是那条循环已经不在了，
        不主动冲一次的话这条告警只会躺在待投递队列里没人发——那就又回到了
        Issue #191 要消灭的"无声"。
        """

        if delivery_death_reported[0]:
            return
        delivery_death_reported[0] = True
        logger.error(
            "投递消费线程已退出，Gateway 仍在收消息但不会再有任何投递 cause=%s", cause
        )
        try:
            delivery_alert("delivery_loop_dead:" + cause, LOOP_ALERT_TRACE_ID)
            alerting_duty.run_once()
        except Exception as error:  # noqa: BLE001 - 告警自身失败不能再抛回退出路径
            logger.error("投递线程退出告警发送失败 error=%s", type(error).__name__)

    document_delivery_death_reported: list[bool] = [False]

    def report_document_delivery_thread_dead(cause: str) -> None:
        """文档投递消费线程退出的唯一上报出口（Issue #341 S-ES-3）。

        与 ``report_delivery_thread_dead`` 同一姿态、独立成一份——两条循环各自
        独立部署、独立轮询、互不阻塞（见 ``apps/gateway/document_delivery.py``
        模块说明「独立于既有 DeliveryConsumer」），共用同一个上报函数会让管理群
        收到的告警文案分不清究竟是哪一条循环死了。
        """

        if document_delivery_death_reported[0]:
            return
        document_delivery_death_reported[0] = True
        logger.error(
            "文档投递消费线程已退出，Gateway 仍在收消息但不会再有新文档被交付 cause=%s",
            cause,
        )
        try:
            delivery_alert(
                "document_delivery_loop_dead:" + cause, DOCUMENT_DELIVERY_LOOP_ALERT_TRACE_ID
            )
            alerting_duty.run_once()
        except Exception as error:  # noqa: BLE001 - 告警自身失败不能再抛回退出路径
            logger.error("文档投递线程退出告警发送失败 error=%s", type(error).__name__)

    # 首次开通（Epic D / S-D-02）：**gateway 只记事件**。真正的编排住在
    # `lingxi-scheduler`（产品负责人 2026-08-18 裁定），按 `claim_stale_onboarding`
    # 认领这里落下的 `auto_provisioning` 事件。本进程因此不持有任何会产生外部副作用的
    # 开通实现，也不再有开通线程池——分钟级的等待一条都不落在长连接线程或投递线程上。
    onboarding_runner: OnboardingRunner = _RecordingOnboarding()

    consumer = assemble_delivery_consumer(config, alerting_duty=alerting_duty)
    delivery_thread = threading.Thread(
        target=run_delivery_loop,
        args=(consumer,),
        kwargs={
            "stop": stop_event,
            "poll_interval_seconds": config.delivery_poll_interval_seconds,
            "heartbeat": _combined_heartbeat(alerting_duty, "gateway-delivery"),
            "on_tick": alerting_duty.run_once,
            "on_dead": report_delivery_thread_dead,
        },
        name="lingxi-gateway-delivery",
        # 独立审核 P3-5：非守护线程会让解释器在退出时等它结束——如果它没能在
        # 停机预算内 join 完，下面"进程仍将继续关闭"这句话在 daemon=False 时其实
        # 不成立（CPython 会一直等非守护线程）。改成守护线程，让"预算耗尽就不再
        # 等"这句承诺对进程本身也成立：预算内能 join 完就是干净退出；耗尽了就是
        # 进程退出时硬收掉这个线程，不会让一个卡住的外部调用把整个进程焊住。
        daemon=True,
    )

    # 文档投递独立消费循环（Issue #341 S-ES-3）：``assemble_document_delivery_
    # consumer`` 未配置 ``LINGXI_GATEWAY_TENANT_DOMAIN`` 时返回 ``None``——本进程
    # 不起第二条后台线程，watchdog 只看既有的投递消费线程，行为与本能力加入
    # 之前逐字节一致（失败关闭，见该函数文档）。**不与既有 ``delivery_thread``
    # 共用同一条循环**：见 ``apps/gateway/document_delivery.py`` 模块说明——建档
    # 四步里的真实飞书 HTTP 调用不得阻塞其他用户的终态卡片/文本送达。
    document_delivery_consumer = assemble_document_delivery_consumer(
        config, alerting_duty=alerting_duty
    )
    document_delivery_thread: threading.Thread | None = None
    if document_delivery_consumer is not None:
        document_delivery_thread = threading.Thread(
            target=run_delivery_loop,
            args=(document_delivery_consumer,),
            kwargs={
                "stop": stop_event,
                "poll_interval_seconds": config.delivery_poll_interval_seconds,
                "heartbeat": _combined_heartbeat(alerting_duty, "gateway-document-delivery"),
                "on_tick": alerting_duty.run_once,
                "on_dead": report_document_delivery_thread_dead,
            },
            name="lingxi-gateway-document-delivery",
            # 同 delivery_thread 的取舍（独立审核 P3-5）：守护线程，停机预算耗尽
            # 就不再等它，不会让一个卡住的外部调用把整个进程焊住。
            daemon=True,
        )

    # 长连接的心跳回调里挂上投递线程的看门狗（Issue #191），因此 supervisor 必须在
    # 线程对象存在之后再装配。两者之间没有别的依赖，换顺序不改变任何既有行为。
    watchdog_checks = [
        delivery_thread_watchdog(delivery_thread, stop=stop_event, on_dead=report_delivery_thread_dead)
    ]
    if document_delivery_thread is not None:
        watchdog_checks.append(
            delivery_thread_watchdog(
                document_delivery_thread,
                stop=stop_event,
                on_dead=report_document_delivery_thread_dead,
            )
        )
    supervisor_onboarding: list[OnboardingRunner] = []
    supervisor = build_supervisor(
        config,
        should_stop=stop_event.is_set,
        onboarding=onboarding_runner,
        on_onboarding_assembled=supervisor_onboarding.append,
        heartbeat=_combined_heartbeat(
            alerting_duty,
            "gateway-longconn",
            watchdog=_combined_watchdog(*watchdog_checks),
        ),
    )
    # **gateway 侧的开通装配断言**（搬迁后的形态）：本进程实际接到管线上的那个实现
    # 必须是"只记事件"的那一个。接上任何会产生外部副作用的编排，都会把分钟级的等待
    # 放回长连接线程——那正是 #65 开工卡「共用线程复核」要消灭的形状，而它在这里
    # 只会表现为"gateway 忽然收不到消息"。
    assert_gateway_onboarding_is_inert(*supervisor_onboarding)
    delivery_thread.start()
    if document_delivery_thread is not None:
        document_delivery_thread.start()

    try:
        reason = supervisor.run(should_stop=stop_event.is_set)
    finally:
        # 长连接侧已经收到停机信号（或者提前异常退出）：确保投递线程也收到同一个
        # 信号，再在停机预算内等它退出——不能让进程在后台线程还在跑外部调用时
        # 直接退出（`V-部署-03`：完成或安全中断在途工作，不是立刻放弃）。
        stop_event.set()
        if shutdown_requested_at[0] is None:
            # 没收到 SIGTERM/SIGINT 就走到这里——例如 supervisor.run() 自己因
            # 终止型错误提前返回。这种情况下还没有任何"优雅停机"流程被启动过，
            # 预算从现在开始计，而不是当成"已经用掉全部预算"。
            shutdown_requested_at[0] = time.monotonic()
        # 独立审核 P3-5：``supervisor.run()`` 从收到信号到返回，自己内部也会按
        # 同一个 ``shutdown_timeout_seconds`` 等在途事件处理完，已经消耗了停机
        # 预算的一部分。这里的 join 只用**剩余**预算，而不是重新给满一份——否则
        # 两段各按完整预算计时，最坏情况下总停机时间是承诺值的两倍。
        elapsed = time.monotonic() - shutdown_requested_at[0]
        remaining_budget = max(0.0, config.shutdown_timeout_seconds - elapsed)
        delivery_thread.join(timeout=remaining_budget)
        if delivery_thread.is_alive():
            logger.error(
                "投递消费线程未能在停机预算内退出，进程将不再等待它、直接关闭"
            )
        if document_delivery_thread is not None:
            # 与上面同一条纪律：用**剩余**预算，从信号那一刻起算，不是重新给满
            # 一份——两条线程顺序 join，第二次 join 前重新算一次剩余预算，避免
            # 第一条线程的等待时间被第二条线程重复计费。
            elapsed_after_first_join = time.monotonic() - shutdown_requested_at[0]
            remaining_after_first_join = max(
                0.0, config.shutdown_timeout_seconds - elapsed_after_first_join
            )
            document_delivery_thread.join(timeout=remaining_after_first_join)
            if document_delivery_thread.is_alive():
                logger.error(
                    "文档投递消费线程未能在停机预算内退出，进程将不再等待它、直接关闭"
                )

    if reason is TerminationReason.TERMINAL_ERROR:
        # 终止型错误（403 / 514 超连接数上限）：进程进入明确的终止态，退出码非 0，
        # 由编排层决定是否告警。**不重连**——重试一个被拒的凭据不会变成通过。
        print("gateway 因终止型接入错误退出，不再重连", file=sys.stderr)
        return 3
    return 0
