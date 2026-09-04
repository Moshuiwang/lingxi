"""管理卡的视觉状态收敛：更新、重启后恢复、后台重算结果回写。

管理卡状态写库与卡片平台更新是两个外部系统，不能由进程内 observer 维持一致。
这里的三件事共用同一条幂等路径——失败只留下持久水位并等下一轮，成功才清水位，
因此重启、短暂平台故障和重复扫描都不会产生第二张卡或第二条业务投递。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from lingxi.core.admin.card_dispatch import ManagementCardContext
from lingxi.core.admin.management_card import render_management_card

from .management_status import rendered_dispatch_status

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
