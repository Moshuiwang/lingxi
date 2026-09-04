"""管理卡的视觉状态收敛：更新、重启后恢复、后台重算结果回写。

管理卡状态写库与卡片平台更新是两个外部系统，不能由进程内的观察者维持一致。这里的
三件事共用同一条幂等路径——失败只留下持久水位并等下一轮，成功才清水位，因此重启、
短暂平台故障和重复扫描都收敛到同一个结果，不会产生第二张卡或第二条业务投递。

并发保护只有卡片序号一把 CAS：状态版本号没有独有判别力，两把 CAS 的收敛见持久
上下文端口的说明。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from lingxi.core.admin.card_dispatch import ManagementCardContext, management_card_fingerprint
from lingxi.core.admin.management_card import render_management_card
from lingxi.core.permission.targeted_recompute import RecomputeKind

from .management_status import (
    PUBLISHING_STATUS_TEXT,
    rendered_dispatch_status,
    skipped_recompute_status_message,
)

# 内部观察窗口，不是产品承诺：观察期间管理员看到的是真话「正在下发」；观察放弃之后
# 留在库里的「未完成」由每日批纠正。
MANAGEMENT_PUBLISH_OBSERVE_SECONDS = 60.0
MANAGEMENT_PUBLISH_POLL_SECONDS = 1.0


class ManagementCardRefresher:
    """把管理卡状态更新集中到同一个 transport ＋ 持久序号端口。"""

    def __init__(
        self, *, transport: Any, catalog: Any, display_names: Any, context_store: Any
    ) -> None:
        """记住渲染与发送这张卡所需的四个端口。"""
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
        """重渲染并更新这张管理卡。

        Args:
            context: 这张卡的持久上下文（目标、卡片实体、序号快照）。
            status: 目标用户的当前权限状态。
            state: 卡片状态机取值；只有等待中的提交态继续隐藏表单以避免重复点击，
                执行已结束（已生效／不完整）后恢复成可重新查询提交的表单，取消则关卡。
            dispatch_status: 展示用的下发状态文本。
            status_message: 追加的说明文本。
            expected_card_sequence: CAS 期望的序号；留空时取上下文快照里的那个。

        Returns:
            ``True`` 表示这次更新真的发出去了；``False`` 表示 CAS 落空，本次放弃。
        """
        if expected_card_sequence is None:
            expected_card_sequence = getattr(context, "card_sequence", None)
        card = render_management_card(
            status,
            display_identifier=context.identifier,
            catalog=self._catalog,
            display_names=self._display_names,
            submitted=state in {"submitted", "dispatching"},
            dispatch_status=rendered_dispatch_status(
                status=status,
                state=state,
                dispatch_status=dispatch_status,
                status_message=status_message,
            ),
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
        return self._mark_refreshed(context, sequence, expected_card_sequence)

    def _mark_refreshed(
        self, context: ManagementCardContext, sequence: int, expected_card_sequence: int | None
    ) -> bool:
        """清掉这张卡的刷新水位；端口没有这个能力时（旧测试替身）视为已清。"""
        mark_visual_refreshed = getattr(self._context_store, "mark_visual_refreshed", None)
        if not callable(mark_visual_refreshed):
            return True
        mark_kwargs: dict[str, Any] = {"message_id": context.message_id, "sequence": sequence}
        if expected_card_sequence is not None:
            # 回写要 CAS 的是本次实际领到的号，不是渲染时读到的快照号。
            mark_kwargs["expected_card_sequence"] = sequence
        return mark_visual_refreshed(**mark_kwargs) is not False


class ManagementCardRecoveryScanner:
    """用持久的"待刷新"水位恢复管理卡的最终视觉状态。

    进程启动时先跑一次，之后由长连接心跳按短间隔惰性触发；失败只留下水位等下一轮，
    成功则由刷新器在平台返回后清水位。
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
        """记住持久上下文端口、刷新器、状态查询口与扫描节流参数。"""
        self._context_store = context_store
        self._refresher = refresher
        self._status_lookup = status_lookup
        self._audit = audit
        self._interval_seconds = max(0.1, float(interval_seconds))
        self._clock = clock
        self._next_scan_at = 0.0

    @staticmethod
    def _dispatch_status_for(context: ManagementCardContext) -> str | None:
        """从持久状态推出这张卡该显示哪一种下发状态。"""
        if context.dispatch_status in {"publishing", "effective", "incomplete"}:
            return context.dispatch_status
        if context.state in {"dispatching", "submitted"}:
            return "publishing"
        if context.state in {"effective", "incomplete"}:
            return context.state
        return None

    def scan(self) -> int:
        """扫一批待刷新的卡片并逐张更新。

        Returns:
            本轮真正刷新成功的张数；读水位失败时返回 0 并保留水位等下一轮。
        """
        try:
            contexts = self._context_store.list_needing_refresh(limit=20)
        except Exception as error:
            self._audit.record(
                "admin.management_card.recovery_scan_failed", error=type(error).__name__
            )
            return 0
        recovered = sum(self._recover_one(context) for context in contexts)
        self._next_scan_at = self._clock() + self._interval_seconds
        return recovered

    def _recover_one(self, context: ManagementCardContext) -> int:
        """恢复一张卡；单张失败只留水位，不连累同一批的其余卡片。"""
        try:
            status = self._status_lookup(context.identifier)
            if status is None:
                return 0
            refreshed = self._refresher.update(
                context=context,
                status=status,
                state=context.state,
                dispatch_status=self._dispatch_status_for(context),
            )
            return 0 if refreshed is False else 1
        except Exception as error:
            self._audit.record(
                "admin.management_card.recovery_refresh_failed",
                error=type(error).__name__,
                message_id=getattr(context, "message_id", ""),
            )
            return 0

    def scan_if_due(self) -> int:
        """到点才扫；供长连接心跳每轮调用。"""
        if self._clock() < self._next_scan_at:
            return 0
        return self.scan()


class RecomputeResultReporter:
    """把后台定向重算与发布的真实结果回写到触发它的那张管理卡上。

    定向重算只负责排出意图，不能把"已入队"直接翻译成「已生效」；真正的发布在另一个
    进程里完成，因此这里通过共享数据库状态观察结果。观察线程有界且守护化，超时后留在
    库里的「未完成」由每日批纠正。
    """

    def __init__(
        self, *, context_store: Any, refresher: Any, status_lookup: Callable[[str], Any], audit: Any
    ) -> None:
        """记住持久上下文端口、刷新器、状态查询口与审计出口。"""
        self._context_store = context_store
        self._refresher = refresher
        self._status_lookup = status_lookup
        self._audit = audit

    def on_completed(self, pending: Any) -> None:
        """重算当场完成：直接推进到「已生效」。"""
        self._refresh(pending, complete=True)

    def on_queued(self, pending: Any, outcome: Any) -> None:
        """重算已入队：先显示「正在下发」，再起一条有界观察等真实发布结果。

        判定为"无变化"时不进观察：权限行已经就是目标结果，没有新意图产生；去观察一条
        更早的、与本次无关的已发布记录既是竞态又会误导，而等到观察超时又会把一次成功的
        空操作说成「未完成」。
        """
        if getattr(outcome, "kind", None) is RecomputeKind.UNCHANGED:
            self._refresh(pending, complete=True)
            return
        self._refresh(
            pending,
            complete=False,
            state_override="dispatching",
            status_message=PUBLISHING_STATUS_TEXT,
        )
        self._start_publish_observer(pending)

    def on_skipped(self, pending: Any, outcome: Any) -> None:
        """重算判"跳过"：这是常态出口不是故障，只有部分原因有专属真话可说。

        拿不到专属文案的跳过原因逐字节不变，仍走原失败文案。本地覆盖照常落库——
        判定阶段不读账号状态是既有产品语义，这里只是如实告知这一次不下发。
        """
        self._refresh(
            pending, complete=False, status_message=skipped_recompute_status_message(outcome)
        )

    def on_failed(self, pending: Any, error: Exception | None) -> None:
        """重算失败：卡片落到「未完成」，由每日批纠正。"""
        del error
        self._refresh(pending, complete=False)

    def on_timeout(self, pending: Any) -> None:
        """重算超时：先落「未完成」，同时仍起观察——它可能只是慢，没有失败。"""
        self._refresh(pending, complete=False)
        self._start_publish_observer(pending)

    def _refresh(
        self,
        pending: Any,
        *,
        complete: bool,
        status_message: str | None = None,
        state_override: str | None = None,
    ) -> None:
        """把一次后台结果落成卡片的持久状态并更新视觉；失败只记审计。

        取消后即使重算线程晚到，也不能把已经关闭的管理卡重新打开：关闭是持久状态，
        后台结果只允许推进仍然可见的卡片。
        """
        origin_message_id = getattr(pending, "origin_card_message_id", None)
        if not origin_message_id:
            return
        try:
            context = self._context_store.lookup_context(message_id=origin_message_id)
            if context is None or context.state == "closed":
                return
            status = self._status_lookup(context.identifier)
            if status is None:
                return
            state = "effective" if complete else (state_override or "incomplete")
            display, machine = self._status_texts(
                context, complete=complete, state=state, status_message=status_message
            )
            updated = self._context_store.update_state(
                message_id=origin_message_id,
                state=state,
                dispatch_status=machine,
                snapshot_fingerprint=management_card_fingerprint(status),
            )
            if updated is not None:
                self._refresher.update(
                    context=updated, status=status, state=state, dispatch_status=display
                )
        except Exception as error:
            self._audit.record(
                "admin.card_callback.management_card_refresh_failed",
                error=type(error).__name__,
                pending_action_id=getattr(pending, "id", ""),
            )

    @staticmethod
    def _status_texts(
        context: Any, *, complete: bool, state: str, status_message: str | None
    ) -> tuple[str, str]:
        """算出这次回写的展示文本与机器可读状态。

        Returns:
            ``(展示给管理员的文本, 落库的机器状态)``。
        """
        if complete:
            return "已生效", "effective"
        if state == "dispatching":
            return status_message or PUBLISHING_STATUS_TEXT, "publishing"
        trace = context.last_trace_id or "当前操作"
        display = status_message or f"下发未完成，最迟次日自动纠正 · 追溯号 {trace}"
        return display, "incomplete"

    def _start_publish_observer(self, pending: Any) -> None:
        """起一条有界的守护线程，观察发布状态直到出结果或超时。"""
        origin_message_id = getattr(pending, "origin_card_message_id", None)
        if not origin_message_id:
            return
        threading.Thread(
            target=self._observe_publish,
            args=(pending, origin_message_id),
            name="lingxi-gateway-management-publish-observer",
            daemon=True,
        ).start()

    def _observe_publish(self, pending: Any, origin_message_id: str) -> None:
        """轮询发布状态；超时按「未完成」收口，交给每日批纠正。"""
        deadline = time.monotonic() + MANAGEMENT_PUBLISH_OBSERVE_SECONDS
        while time.monotonic() < deadline:
            publish_state = self._read_publish_state(origin_message_id)
            if publish_state == "published":
                self._refresh(pending, complete=True)
                return
            if publish_state in {"failed", "superseded"}:
                self._refresh(pending, complete=False)
                return
            threading.Event().wait(MANAGEMENT_PUBLISH_POLL_SECONDS)
        self._refresh(pending, complete=False)

    def _read_publish_state(self, origin_message_id: str) -> str | None:
        """读一次发布状态；瞬时读失败只记审计，由下一轮重试。"""
        try:
            return self._context_store.latest_publish_state_for_message(
                message_id=origin_message_id
            )
        except Exception as error:
            self._audit.record(
                "admin.card_callback.management_publish_state_lookup_failed",
                error=type(error).__name__,
            )
            return None
