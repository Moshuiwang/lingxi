"""``card.action.trigger`` 回调的编排：解析出的按钮点击 → confirm/cancel → 卡片终态更新 → 管理群脱敏通知。

只依赖注入的 ``Protocol`` 端口（见 :mod:`lingxi.core.admin.card_callback_ports`），
不 import ``adapters/``（代码框架第二节）。真实装配见 ``apps/gateway/__init__.py``。
**``handle()`` 的返回值就是飞书卡片回调的应答帧**：返回空会被平台按"维持
原卡"处理，即使出带外调用了 ``update()`` 也会被同一次点击的空应答盖掉，
应答形状见 :meth:`~AdminCardCallbackHandler.handle` 文档；出带外更新分支
降级为冗余纵深。

本类与 ``AdminCommandRouter``（私聊文本命令）分开。卡片更新只要"这一行现在
确实是终态"就做，重复调用无害；群通知只在这次点击**首次**让它落进终态时
才发，避免幂等重放刷屏管理群。**已知边界**：回调不核对触发点击的具体
``card_id``；停用有被动提示、恢复没有主动通知。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from lingxi.core.admin.card_callback_management import _ManagementCardCallbackMixin
from lingxi.core.admin.card_callback_ports import (
    _MANAGEMENT_CARD_REVOKE_REASON as _MANAGEMENT_CARD_REVOKE_REASON,
)
from lingxi.core.admin.card_callback_ports import (
    AuditSink,
    ManagementActionRouter,
    ManagementCardContextReader,
    ManagementCardRefresher,
    PendingActionDecider,
    PermissionRecomputeTrigger,
    PostCallbackExecutor,
    _Outcome,
    _toast_error,
)
from lingxi.core.admin.card_dispatch import management_card_fingerprint
from lingxi.core.admin.display_names import AdminDisplayNames
from lingxi.core.admin.notification import (
    DECISION_CANCEL,
    DECISION_CONFIRM,
    AdminCardTransport,
    GroupNotifier,
    describe_failed_reason,
    permission_scope_ids,
    render_card_payload,
    render_group_notice,
    render_terminal_card,
)
from lingxi.core.admin.pending_action import (
    PendingAction,
    PendingActionAuditWriteFailedError,
    PendingActionStatus,
    PendingActionTransientFailureError,
    PendingActionType,
)
from lingxi.core.admin.views import AdminUserStatusView


class AdminCardCallbackHandler(_ManagementCardCallbackMixin):
    """``card.action.trigger`` 事件的唯一处理入口。见模块文档。

    管理卡表单提交/撤销/取消三类方法由 :class:`_ManagementCardCallbackMixin`
    提供（见 :mod:`lingxi.core.admin.card_callback_management`），本类只声明
    ``__init__`` 与确认/取消 pending action 的核心流程。
    """

    def __init__(
        self,
        *,
        pending_actions: PendingActionDecider,
        confirm_cards: AdminCardTransport,
        group_notifier: GroupNotifier | None,
        group_chat_id: str | None,
        audit: AuditSink,
        display_names: AdminDisplayNames,
        management_actions: ManagementActionRouter | None = None,
        recompute_trigger: PermissionRecomputeTrigger | None = None,
        management_context_store: ManagementCardContextReader | None = None,
        management_state_lookup: Callable[[str], AdminUserStatusView | None] | None = None,
        management_card_refresher: ManagementCardRefresher | None = None,
        post_callback_executor: PostCallbackExecutor | None = None,
    ) -> None:
        """除 ``pending_actions``/``confirm_cards``/``audit``/``display_names`` 外的注入端口均可选，未装配时对应的可选功能各自降级或禁用。"""
        self._pending_actions = pending_actions
        self._confirm_cards = confirm_cards
        self._group_notifier = group_notifier
        self._group_chat_id = group_chat_id
        self._audit = audit
        # 必填：终态卡「目标：」字段与管理群广播都不再允许退回展示 open_id，
        # 与 ConfirmCardDispatcher 同一条纪律。
        self._display_names = display_names
        # 未装配时管理卡的表单/按钮交互回复"该功能当前不可用"，不假装已经
        # 路由过任何命令——与本类既有确认/取消两个端口"未装配=构造时必须
        # 传入"不同，不传这个参数时行为逐字节不变。
        self._management_actions = management_actions
        # 定向权限重算触发口：``None`` 表示装配层还没接（与
        # ``group_notifier=None`` 同一姿态）——不报错、不重试，异步下发这一层
        # 纵深缺席时，每日批仍是保底，确认事务本身的结果不被改变。
        self._recompute_trigger = recompute_trigger
        self._management_context_store = management_context_store
        self._management_state_lookup = management_state_lookup
        self._management_card_refresher = management_card_refresher
        # 回调应答之后才做的那批网络往返的执行器：``None`` = 原地同步做，与本
        # 参数加入之前逐字节一致。见 :meth:`_run_after_response`。
        self._post_callback_executor = post_callback_executor

    def _run_after_response(self, task: Callable[[], None], *, pending: PendingAction) -> None:
        """把"应答之后才做的事"交给注入的执行器；没接执行器就原地同步跑。

        **为什么要有这一层**：``handle()`` 的返回值就是飞书要的回调应答帧，应答
        窗口是秒级的，而确认之后原地要做的三件事全是网络往返——大批量授权实测
        可能超出应答窗口，让管理员看到「超时未响应」并重复点击。搬到应答之后做，
        **顺序一个字不变**：原管理卡必须先被推进成「下发中」再入队重算，否则
        重算完成回调刷出的「已生效」会被随后到达的「下发中」盖回去。执行器缺席
        或它自己表示"没接住"时**原地同步执行**，不能让终态卡等结果整批消失。
        """
        if self._post_callback_executor is not None:
            try:
                if self._post_callback_executor.submit(task):
                    return
            except Exception as error:  # 执行器自身故障不得吞掉这批后处理
                self._audit.record(
                    "admin.card_callback.post_callback_submit_failed",
                    pending_action_id=pending.id,
                    error=type(error).__name__,
                )
        task()

    def _decide_pending_action(
        self, *, decision: str, pending_action_id: str, operator_open_id: str, trace_id: str
    ) -> dict[str, Any] | tuple[_Outcome, PendingAction]:
        """校验 ``decision`` 合法性，调用 ``confirm()``/``cancel()``。

        把两类可重试的数据库故障统一转成一句「系统繁忙」toast。两类故障
        事务均已整体回滚，pending_action 与目标账号均未改变——这次
        点击结构上"没有发生过"，管理员可以直接重新点击重试，因此不更新卡片、
        不带 ``card``；分开记审计动作名与文案便于事后区分。找不到对应的待
        确认操作（含伪造回调）同样在这里判定，只记审计。
        """
        if decision not in (DECISION_CONFIRM, DECISION_CANCEL):
            self._audit.record("admin.card_callback.unknown_decision", trace_id=trace_id)
            return _toast_error("操作不存在或已失效")

        try:
            if decision == DECISION_CONFIRM:
                outcome = self._pending_actions.confirm(
                    pending_action_id=pending_action_id, clicker_open_id=operator_open_id
                )
            else:
                outcome = self._pending_actions.cancel(
                    pending_action_id=pending_action_id, clicker_open_id=operator_open_id
                )
        except PendingActionAuditWriteFailedError:
            self._audit.record(
                "admin.card_callback.audit_write_failed",
                pending_action_id=pending_action_id,
                trace_id=trace_id,
            )
            return _toast_error("系统繁忙请重试")
        except PendingActionTransientFailureError as error:
            self._audit.record(
                "admin.card_callback.transient_failure",
                pending_action_id=pending_action_id,
                trace_id=trace_id,
                classification=error.classification,
            )
            return _toast_error("系统繁忙，请稍后重试")

        if outcome.pending is None:
            self._audit.record(
                "admin.card_callback.not_found",
                pending_action_id=pending_action_id,
                trace_id=trace_id,
            )
            return _toast_error("操作不存在或已失效")
        return outcome, outcome.pending

    def _apply_post_decision_side_effects(
        self, *, pending: PendingAction, outcome: _Outcome, terminal_card: Any | None, trace_id: str
    ) -> None:
        """应答之后才做的三/四件事，次序不可变，见 :meth:`_run_after_response`。

        群通知/刷新原管理卡/入队定向重算只在这次点击**首次**让它落进终态时
        才做，避免幂等重放刷屏；原管理卡必须先推进成「下发中」再入队重算，
        否则重算完成回调刷出的「已生效」会被随后到达的「下发中」盖回去。
        定向权限重算额外要求 ``status is EXECUTED``——``CANCELLED``/
        ``EXPIRED``/``FAILED`` 都不该让任何用户的发布内容发生变化。
        """
        if terminal_card is not None:
            self._push_terminal_card(pending, terminal_card)
        if outcome.decision.terminal_status is not None:
            self._notify_group(pending)
            self._refresh_origin_management_card(pending=pending, trace_id=trace_id)
            if pending.status is PendingActionStatus.EXECUTED:
                self._trigger_recompute(pending)

    def _build_confirm_response(
        self, *, pending: PendingAction, outcome: _Outcome, card_payload: dict[str, Any] | None
    ) -> dict[str, Any]:
        """把这次点击的结论渲染成飞书要的应答帧（toast + 可选终态卡）。

        到这里仍是 pending 的唯一分支是点击人不是发起人。toast 直接用这次
        点击产生的 ``decision.message``（``decide_confirm``/``decide_cancel``
        已经为每个分支写好完整的友好中文），不用只看持久状态派生的
        ``_outcome_text``——两者在幂等重放等场景下措辞不同，toast 本来就该
        描述"这次点击的结果"而不是"当前持久状态"；职位范围授权例外：确认
        结果先是事务已记录、随后才由后台重算发布，不能把成功 toast 误写成
        已经生效。
        """
        if pending.status is PendingActionStatus.PENDING:
            return _toast_error("只有发起人本人可以操作此卡片")

        toast_content = outcome.decision.message
        if pending.status is PendingActionStatus.EXECUTED and _is_position_scope_pending(pending):
            toast_content = _outcome_text(pending)
        response: dict[str, Any] = {
            "toast": {
                "type": "success" if pending.status is PendingActionStatus.EXECUTED else "info",
                "content": toast_content,
            }
        }
        if card_payload is not None:
            response["card"] = {"type": "raw", "data": card_payload}
        return response

    def handle(
        self, *, operator_open_id: str, pending_action_id: str, decision: str, trace_id: str
    ) -> dict[str, Any]:
        """处理一次卡片按钮点击，返回飞书卡片回调的应答载荷（见模块文档）。

        返回值不是内部结论，是**要直接交给飞书的应答帧**，一路透传到 SDK。
        五类应答形状：(1) 点击后达到/已处于终态：toast 带终态文案 + 终态卡
        JSON；(2) 点击人不是发起人：``pending_action`` 保持 ``pending``，
        toast error 不带 ``card``；(3) 动作不存在：toast error 不带
        ``card``；(4)/(5) 审计写入失败/数据库瞬时故障：toast error「系统
        繁忙」不带 ``card``，事务已整体回滚，可直接重试。``decision`` 只
        识别 ``"confirm"``/``"cancel"``；不认识的取值记审计后原样拒绝。
        """
        decided = self._decide_pending_action(
            decision=decision,
            pending_action_id=pending_action_id,
            operator_open_id=operator_open_id,
            trace_id=trace_id,
        )
        if isinstance(decided, dict):
            return decided
        outcome, pending = decided
        self._audit.record(
            "admin.card_callback.handled",
            pending_action_id=pending.id,
            decision=decision,
            status=pending.status.value,
            trace_id=trace_id,
        )

        # 卡片视觉收敛：只要这一行**现在**是终态就渲染终态卡 JSON，不要求这次
        # 点击本身是让它第一次落进终态的那一次（含幂等重放）。终态卡 JSON
        # 必须**同步**算出来：它就是这次应答要带回去的那张卡；真正的网络往返
        # （出带外换卡）与其余后处理一起搬到应答之后，见
        # :meth:`_run_after_response`。
        terminal_card: Any | None = None
        card_payload: dict[str, Any] | None = None
        if pending.status is not PendingActionStatus.PENDING:
            terminal_card, card_payload = self._render_terminal_card(pending)

        self._run_after_response(
            lambda: self._apply_post_decision_side_effects(
                pending=pending, outcome=outcome, terminal_card=terminal_card, trace_id=trace_id
            ),
            pending=pending,
        )

        return self._build_confirm_response(
            pending=pending, outcome=outcome, card_payload=card_payload
        )

    def _resolve_scope_labels(self, pending: PendingAction) -> tuple[str | None, str | None]:
        """本地权限三类动作的「公司/指标」人性化展示标签。

        非本地权限动作或 payload 不可用时返回 ``(None, None)``，调用方据此
        让渲染函数走既有的降级路径。
        """
        scope_ids = permission_scope_ids(pending)
        if scope_ids is None:
            return None, None
        company_id, metric_id = scope_ids
        return (
            self._display_names.company_label(company_id=company_id),
            self._display_names.metric_label(metric_id=metric_id),
        )

    def _update_card_to_terminal(self, pending: PendingAction) -> dict[str, Any] | None:
        """渲染终态卡的 CardKit JSON，并尽力而为地出带外更新已发出的那张卡片。

        **返回值与出带外调用的成败无关**：``card_payload`` 已在进入 ``try`` 块
        之前算好，``update()`` 失败只记审计、不影响返回值——应答换卡是主路径，
        出带外更新是冗余纵深。「目标：」/「范围」区块经 ``AdminDisplayNames``
        翻译成人类可读文本；持久化终态卡展示 ``pending`` 本身的状态，不随
        点击者是谁而变化，``outcome_text`` 由 :func:`_outcome_text` 按状态派生
        （与 ``handle()`` 里 toast 用 ``outcome.decision.message`` 是两件事）。
        """
        card, card_payload = self._render_terminal_card(pending)
        if card is not None:
            self._push_terminal_card(pending, card)
        return card_payload

    def _render_terminal_card(
        self, pending: PendingAction
    ) -> tuple[Any | None, dict[str, Any] | None]:
        """只渲染终态卡，不做任何网络调用；返回 ``(card, card_payload)``。

        与 :meth:`_push_terminal_card` 拆开，是因为两者的时机不同：
        ``card_payload`` 是这次回调应答要带回去的载荷，必须同步算出来；出带外的
        ``update()`` 是冗余纵深，可以等应答发出之后再做。
        """
        if pending.card_id is None:
            return None, None
        target_label = self._display_names.user_label(open_id=pending.target_open_id)
        company_label, metric_label = self._resolve_scope_labels(pending)
        card = render_terminal_card(
            pending,
            target_label=target_label,
            outcome_text=_outcome_text(pending),
            company_label=company_label,
            metric_label=metric_label,
        )
        return card, render_card_payload(card)

    def _push_terminal_card(self, pending: PendingAction, card: Any) -> None:
        """出带外把已经发出的那张卡片更新成终态。失败只记审计。"""
        if pending.card_id is None:
            return
        try:
            # 换一个本次调用专用的 sequence：同一张卡片可能因为回调重投被多次
            # 调用 update，CardKit 要求每次调用携带严格递增的 sequence。
            sequence = self._pending_actions.next_card_sequence(pending_action_id=pending.id)
            self._confirm_cards.update(card_id=pending.card_id, sequence=sequence, card=card)
        except Exception as error:  # 卡片视觉更新失败不影响已经落库的业务结果
            self._audit.record(
                "admin.card_callback.card_update_failed",
                pending_action_id=pending.id,
                error=type(error).__name__,
            )

    def _trigger_recompute(self, pending: PendingAction) -> None:
        if self._recompute_trigger is None:
            return
        try:
            self._recompute_trigger.trigger(pending)
        except Exception as error:  # 失败降级回每日批，不影响已经落库的确认结果
            self._audit.record(
                "admin.card_callback.recompute_trigger_failed",
                pending_action_id=pending.id,
                error=type(error).__name__,
            )

    def _refresh_origin_management_card(self, *, pending: PendingAction, trace_id: str) -> None:
        origin_message_id = getattr(pending, "origin_card_message_id", None)
        store = self._management_context_store
        lookup = self._management_state_lookup
        if not origin_message_id or store is None or lookup is None:
            return
        try:
            context = store.lookup_context(message_id=origin_message_id)
            if context is None:
                return
            status = lookup(context.identifier)
            if status is None:
                return
            if pending.status is PendingActionStatus.EXECUTED:
                state, dispatch_status = "dispatching", "publishing"
            elif pending.status is PendingActionStatus.CANCELLED:
                # 这里是**确认卡**上的取消：确认卡处理完后原管理卡回到最新只读
                # 快照并恢复表单。管理卡自身的"取消"按钮走
                # ``handle_management_cancel``，才会把上下文置为 ``closed``。
                state, dispatch_status = "ready", "idle"
            elif pending.status is PendingActionStatus.FAILED:
                state, dispatch_status = "incomplete", "incomplete"
            else:
                state, dispatch_status = "closed", "idle"
            updated = store.update_state(
                message_id=origin_message_id,
                state=state,
                dispatch_status=dispatch_status,
                snapshot_fingerprint=management_card_fingerprint(status),
                last_trace_id=trace_id,
            )
            if updated is not None:
                self._refresh_management_card(
                    context=updated,
                    status=status,
                    state=state,
                    # ``idle`` 是持久层机器状态，不应原样出现在卡片的人类可见
                    # 文案；ready 分支传 None 让 renderer 只展示恢复后的表单。
                    dispatch_status=None if state == "ready" else dispatch_status,
                    trace_id=trace_id,
                )
        except Exception as error:  # refresh is best effort after commit
            self._audit.record(
                "admin.card_callback.management_card_refresh_failed",
                error=type(error).__name__,
                trace_id=trace_id,
            )

    def _notify_group(self, pending: PendingAction) -> None:
        if self._group_notifier is None or not self._group_chat_id:
            return
        # 群通知只用 render_group_notice 内部按形状白名单渲染出的脱敏摘要，
        # 不转发本次点击结论的原始 message 文本。目标用户身份/公司/指标同样
        # 经 AdminDisplayNames 翻译成姓名+邮箱与可读内容。
        target_label = self._display_names.user_label(open_id=pending.target_open_id)
        company_label, metric_label = self._resolve_scope_labels(pending)
        text = render_group_notice(
            pending,
            target_label=target_label,
            company_label=company_label,
            metric_label=metric_label,
        )
        try:
            self._group_notifier.send_text(
                chat_id=self._group_chat_id, text=text, dedupe_key=pending.id
            )
        except Exception as error:  # 群通知失败不影响已经落库的业务结果
            self._audit.record(
                "admin.card_callback.group_notify_failed",
                pending_action_id=pending.id,
                error=type(error).__name__,
            )


def _is_position_scope_pending(pending: PendingAction) -> bool:
    """识别职位范围授权，供即时回执采用异步下发文案。"""
    if pending.action_type is not PendingActionType.LOCAL_PERMISSION_GRANT:
        return False
    if not pending.payload:
        return False
    try:
        payload = json.loads(pending.payload)
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and bool(payload.get("position_name"))


def _outcome_text(pending: PendingAction) -> str:
    if pending.status is PendingActionStatus.EXECUTED:
        # 数据库事务已记录，但重算/发布由后台队列异步完成；不能把「已记录」
        # 误报为「即时生效」。完成后由后台回调把原管理卡刷新为最终状态。
        return "操作已记录，权限正在下发"
    if pending.status is PendingActionStatus.CANCELLED:
        return "已取消"
    if pending.status is PendingActionStatus.EXPIRED:
        return "已过期，未执行"
    if pending.status is PendingActionStatus.FAILED:
        # ``pending.reason`` 是数据库写入的机器码（如 "role_revoked"），改用
        # ``describe_failed_reason`` 翻译成中文，与群通知共用同一份词表，
        # 不允许两处出现不同说法。
        return f"未执行（{describe_failed_reason(pending.reason)}）"
    return "状态未知"  # pragma: no cover - 调用点已确保 status 非 PENDING
