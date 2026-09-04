"""确认卡片的发送编排：渲染 → 通过 ``AdminCardTransport`` 发送 → 把结果落回待确认操作。

只依赖注入的 ``Protocol`` 端口，不 import ``adapters/``。真实装配见
``apps/gateway/__init__.py``。

"发送 + 落库标记"合成一个方法：合同"卡片发送失败时本次操作不执行"要求"发送
结果"与"待确认操作是否可被确认"两件事不能出现中间态。两步放进同一次调用，
让调用方只需要处理一个返回值，不需要自己记得"发送成功之后一定要调
``mark_card_delivered``"这类跨对象的调用顺序。``send()`` 的失败分支需要注入
``AuditSink``：只把结果归一为"未送达"不够，诊断故障时唯一能读到的痕迹只有
``pending_action.status='failed'`` 这一行，因此在失败分支记一条审计（异常类名
+ 明确拒绝时的 ``code``/``log_id``），不带卡片正文或外部标识明文。
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from lingxi.core.admin.display_names import AdminDisplayNames
from lingxi.core.admin.management_card import (
    CompanyMetricCatalog,
    ManagementCardTransport,
    render_management_card,
)
from lingxi.core.admin.notification import (
    AdminCardDeliveryRejected,
    AdminCardTransport,
    permission_scope_ids,
    render_confirm_card,
)
from lingxi.core.admin.pending_action import PendingAction
from lingxi.core.admin.views import AdminUserStatusView

# 管理卡上下文只允许在这段时间内接受旧卡回调。它是一个内部保护窗口，不是
# 待确认操作或本地授权的有效期；但无论调用方如何配置，都不能超过 24 小时。
# 这两个常量放在 core 的唯一纯逻辑入口，内存兼容实现与 PostgreSQL 适配器共用同一
# 组边界，避免一边允许更长窗口而另一边已经拒绝的语义分叉。
MANAGEMENT_CARD_CONTEXT_DEFAULT_TTL_SECONDS = 40.0 * 60.0
MANAGEMENT_CARD_CONTEXT_MAX_TTL_SECONDS = 24.0 * 60.0 * 60.0


def bounded_management_card_ttl_seconds(value: float) -> float:
    """把管理卡上下文 TTL 收窄到 ``(0, 24h]``。

    ``ttl_seconds`` 是部署/测试配置入口，不能成为绕过 24 小时硬上限的后门。非有限
    或非正数配置直接失败关闭；超过上限的合法数值收窄到上限。调用方若需要构造一条
    已经过期的历史上下文，应传一个显式的过去 ``context_deadline_at``，而不是配置
    一个非法 TTL。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("管理卡上下文 TTL 必须是有限正数")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("管理卡上下文 TTL 必须是有限正数")
    return min(number, MANAGEMENT_CARD_CONTEXT_MAX_TTL_SECONDS)


def bounded_management_card_deadline(
    *, now: datetime, requested: datetime | None, ttl_seconds: float
) -> datetime:
    """返回不晚于 ``now + 24h`` 的管理卡上下文截止时间。"""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    elif now.utcoffset() is None:
        now = now.replace(tzinfo=UTC)
    if requested is None:
        deadline = now + timedelta(seconds=bounded_management_card_ttl_seconds(ttl_seconds))
    else:
        deadline = requested
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            deadline = deadline.replace(tzinfo=UTC)
    hard_deadline = now + timedelta(seconds=MANAGEMENT_CARD_CONTEXT_MAX_TTL_SECONDS)
    return min(deadline, hard_deadline)


def management_card_fingerprint(status: AdminUserStatusView) -> str:
    """返回管理卡所依据状态的稳定指纹。

    指纹只包含权限状态、银河摘要和当前本地覆盖内容，不包含卡片/消息 ID 或操作者
    身份。回调在执行写操作前重新取当前状态并比较它，防止旧卡在数据已经变化后继续
    写入；排序只用于稳定序列化，不改变权限语义。
    """
    payload = {
        "identifier": status.identifier,
        "provisioning_state": status.provisioning_state,
        "account_state": status.account_state,
        "updated_at": status.updated_at,
        "local_overrides": [
            {
                "override_id": item.override_id,
                "direction": item.direction,
                "company_id": item.company_id,
                "metric_name": item.metric_name,
                "reason": item.reason,
                "created_at": item.created_at,
                "position_name": item.position_name,
                "company_scope": item.company_scope,
                "group_id": item.group_id,
            }
            for item in status.local_overrides
        ],
        "galaxy_source": None
        if status.galaxy_source is None
        else {
            "granted": status.galaxy_source.granted,
            "reason": status.galaxy_source.reason,
            "companies": sorted(status.galaxy_source.companies),
            "functions": sorted(status.galaxy_source.functions),
            "all_companies": status.galaxy_source.all_companies,
        },
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class PendingActionDeliveryTracker(Protocol):
    """卡片发送结果的落库口，与生产适配器的对应两个方法结构相同。"""

    def mark_card_delivered(self, *, pending_action_id: str, card_id: str) -> None:
        """记下这次卡片已经确认送达，附带 CardKit 分配的卡片 ID。"""
        ...

    def mark_send_failed(self, *, pending_action_id: str) -> None:
        """记下这次卡片发送未能确认送达。"""
        ...


class AuditSink(Protocol):
    """与其余三处结构相同的独立审计出口 Protocol，四处不互相 import。"""

    def record(self, action: str, /, **fields: object) -> None:
        """记一条结构化审计。"""
        ...


@dataclass(frozen=True)
class CardDispatchResult:
    """一次确认卡发送的结果：卡片是否已确认送达。"""

    delivered: bool


class ConfirmCardDispatcher:
    """把一条刚 ``prepare`` 好的待确认操作渲染成确认卡片并发到发起管理员本人私聊。

    作为触发这条命令的消息的回复，并把发送结果同步落回待确认操作表。
    """

    def __init__(
        self,
        *,
        transport: AdminCardTransport,
        tracker: PendingActionDeliveryTracker,
        audit: AuditSink,
        display_names: AdminDisplayNames,
    ) -> None:
        """装配确认卡发送所需的传输、落库口、审计与展示名端口。"""
        self._transport = transport
        self._tracker = tracker
        self._audit = audit
        # 必填：确认卡「目标：」字段一律显示姓名+邮箱，不能有一条"未装配则退回
        # open_id"的安全兜底路径，见 core/admin/display_names.AdminDisplayNames
        # 模块文档「安全边界」一节。
        self._display_names = display_names

    def send(
        self,
        *,
        pending: PendingAction,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
    ) -> CardDispatchResult:
        """渲染并发送这条待确认操作的确认卡，把结果同步落回待确认操作表。"""
        target_label = self._display_names.user_label(open_id=pending.target_open_id)
        scope_ids = permission_scope_ids(pending)
        company_label = (
            self._display_names.company_label(company_id=scope_ids[0]) if scope_ids else None
        )
        metric_label = (
            self._display_names.metric_label(metric_id=scope_ids[1]) if scope_ids else None
        )
        card = render_confirm_card(
            pending,
            target_label=target_label,
            company_label=company_label,
            metric_label=metric_label,
        )
        try:
            created = self._transport.create(
                chat_id=chat_id,
                thread_id=thread_id,
                reply_to_message_id=reply_to_message_id,
                card=card,
            )
        except Exception as error:
            # 明确拒绝（AdminCardDeliveryRejected）与结果不明（其余异常）在这里
            # 得到同一处理——两者都不能确定卡片已经送达，一律按"未送达"处理，
            # 让本次操作作废（合同"卡片发送失败时本次操作不执行，不根据失败
            # 原因推断人员状态"）。下面的分类判断只影响审计带不带 code/log_id，
            # 不改变这条"一律按未送达处理"的判定。
            if isinstance(error, AdminCardDeliveryRejected):
                self._audit.record(
                    "admin.card_dispatch.send_failed",
                    pending_action_id=pending.id,
                    error=type(error).__name__,
                    code=error.code,
                    log_id=error.log_id,
                )
            else:
                self._audit.record(
                    "admin.card_dispatch.send_failed",
                    pending_action_id=pending.id,
                    error=type(error).__name__,
                )
            self._tracker.mark_send_failed(pending_action_id=pending.id)
            return CardDispatchResult(delivered=False)

        self._tracker.mark_card_delivered(pending_action_id=pending.id, card_id=created.card_id)
        return CardDispatchResult(delivered=True)


@dataclass(frozen=True)
class ManagementCardDispatchResult:
    """一次管理卡发送的结果：卡片是否已确认送达。"""

    delivered: bool


@dataclass(frozen=True)
class ManagementCardContext:
    """一张管理卡的可恢复上下文。

    ``context_deadline_at`` 只用于回调时的懒检查；后台恢复 scanner 只处理已落库但尚未
    成功回写的视觉水位，不主动清扫到期上下文，也不改变已经确认生效的本地授权有效期。
    ``card_sequence`` 是整卡更新序号，必须由持久层原子递增。
    """

    message_id: str
    card_id: str
    identifier: str
    chat_id: str
    initiated_by_open_id: str
    card_sequence: int
    snapshot_fingerprint: str
    context_deadline_at: datetime
    state: str = "ready"
    dispatch_status: str = "idle"
    last_trace_id: str | None = None
    # 仅用于每日批补齐汇总的幂等水位；只有持久层确认 daily refresh/batch 已经补齐
    # 一条此前即时失败的操作时才置上，普通即时成功不会获得这项资格。
    daily_correction_reported_at: datetime | None = None
    # 只有真实 daily refresh/batch 发布补齐过一条此前即时失败的操作时才为真；迟到
    # 的 instant outbox 成功会清掉它，不能触发“每日纠偏补齐”群摘要。
    daily_correction_pending: bool = False
    # ``needs_refresh`` 是持久的视觉回写水位：数据库状态已经改变但 CardKit 尚未
    # 成功接受最新卡片时为真。``visual_sequence`` 只在 CardKit 成功后推进，不能在
    # 取号或网络调用失败时提前推进。
    needs_refresh: bool = False
    visual_sequence: int = 2
    # 业务状态代数与 CardKit 的外部 sequence 分开记账。scanner 只能用它读到的
    # 代数回写；状态在渲染期间改变时，CAS 会拒绝旧视觉。card_sequence 仍是
    # CardKit 的整卡序号，不能拿它单独充当状态版本。
    state_version: int = 1


class ManagementCardContextStore:
    """兼容旧调用方/测试的 ``message_id -> identifier`` 内存 TTL 映射。

    生产 gateway 使用持久实现；本类保留为纯逻辑适配器，便于旧调用方和无数据库
    单测验证同一套回调语义。发送侧登记：建卡成功后拿到的 ``message_id`` 与
    回调事件体里这张卡片自己的消息 ID 是同一个值，发送成功那一刻就能确定性地
    知道这条 ``message_id`` 对应哪一次查询、查的是谁，不需要在回调时反查任何
    外部状态。TTL 默认 40 分钟、条目数上限默认 512（超过逐出最早写入的一条），
    仅是测试适配器的内部默认值，不构成产品承诺。
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = MANAGEMENT_CARD_CONTEXT_DEFAULT_TTL_SECONDS,
        max_entries: int = 512,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """按给定的 TTL、容量上限与时钟建立一个空的内存映射。"""
        self._ttl_seconds = bounded_management_card_ttl_seconds(ttl_seconds)
        self._max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[str, tuple[ManagementCardContext, float]] = OrderedDict()
        # The production adapter gets atomic increments from PostgreSQL.  This
        # compatibility implementation is also used by gateway-style tests and
        # must not hand out the same sequence twice when two callbacks race.
        self._lock = threading.RLock()

    def remember(
        self,
        *,
        message_id: str,
        identifier: str,
        card_id: str = "",
        chat_id: str = "",
        initiated_by_open_id: str = "",
        snapshot_fingerprint: str = "",
        card_sequence: int = 2,
        state_version: int = 1,
        context_deadline_at: datetime | None = None,
        state: str = "ready",
        dispatch_status: str = "idle",
        last_trace_id: str | None = None,
    ) -> None:
        """管理卡发送成功后登记一条映射。

        ``message_id``/``identifier`` 任一为空都不登记——空 ``message_id`` 无法
        在回调时被查到，空 ``identifier`` 登记了也没有意义（回调侧本来就是靠它
        非空才判定"命中"）。
        """
        if not message_id or not identifier:
            return
        context = self._build_context(
            message_id=message_id,
            card_id=card_id,
            identifier=identifier,
            chat_id=chat_id,
            initiated_by_open_id=initiated_by_open_id,
            snapshot_fingerprint=snapshot_fingerprint,
            card_sequence=card_sequence,
            state_version=state_version,
            context_deadline_at=context_deadline_at,
            state=state,
            dispatch_status=dispatch_status,
            last_trace_id=last_trace_id,
        )
        expires_at = self._clock() + self._ttl_seconds
        self._store_or_merge(message_id, context, expires_at)

    def _build_context(
        self,
        *,
        message_id: str,
        card_id: str,
        identifier: str,
        chat_id: str,
        initiated_by_open_id: str,
        snapshot_fingerprint: str,
        card_sequence: int,
        state_version: int,
        context_deadline_at: datetime | None,
        state: str,
        dispatch_status: str,
        last_trace_id: str | None,
    ) -> ManagementCardContext:
        """按 :meth:`remember` 的入参构造一条新上下文；不接触共享状态。"""
        deadline = bounded_management_card_deadline(
            now=datetime.now(UTC),
            requested=context_deadline_at,
            ttl_seconds=self._ttl_seconds,
        )
        return ManagementCardContext(
            message_id=message_id,
            card_id=card_id,
            identifier=identifier,
            chat_id=chat_id,
            initiated_by_open_id=initiated_by_open_id,
            card_sequence=max(1, int(card_sequence)),
            snapshot_fingerprint=snapshot_fingerprint,
            context_deadline_at=deadline,
            state=state,
            dispatch_status=dispatch_status,
            last_trace_id=last_trace_id,
            needs_refresh=False,
            visual_sequence=max(1, int(card_sequence)),
            state_version=max(1, int(state_version)),
        )

    def _store_or_merge(
        self, message_id: str, context: ManagementCardContext, expires_at: float
    ) -> None:
        """把新构造的上下文写入映射，或与既有条目合并；随后按容量上限逐出最旧项。

        同一个 message_id 重复登记（理论上不会发生——每次 `/admin user` 都会
        建一张新卡、拿到新的 message_id）只允许抬高 sequence 并延续已有状态；
        不能让一次重放把已关闭/已提交的卡重新变成 ready。
        """
        with self._lock:
            existing = self._entries.get(message_id)
            if existing is not None:
                previous, previous_expires_at = existing
                if context.card_sequence > previous.card_sequence:
                    previous = ManagementCardContext(
                        **{**previous.__dict__, "card_sequence": context.card_sequence}
                    )
                if context.state_version > previous.state_version:
                    previous = ManagementCardContext(
                        **{**previous.__dict__, "state_version": context.state_version}
                    )
                self._entries[message_id] = (previous, previous_expires_at)
            else:
                self._entries[message_id] = (context, expires_at)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def lookup(self, *, message_id: str) -> str | None:
        """回调侧按 ``message_id`` 查回 ``identifier``。

        未登记、已过期都返回 ``None``，不抛异常——调用方把 ``None`` 与"从未
        登记过"同等对待。
        """
        if not message_id:
            return None
        with self._lock:
            entry = self._entries.get(message_id)
            if entry is None:
                return None
            context, expires_at = entry
            if self._clock() >= expires_at:
                return None
            return context.identifier

    def lookup_context(self, *, message_id: str) -> ManagementCardContext | None:
        """按消息 ID 找回完整上下文。

        这里保留已过内部缓存窗口的上下文，供回调层做一次惰性关闭/刷新；严格的
        ``lookup()`` 仍会把过期项视为未命中。容量上限负责避免这类保留无限增长。
        """
        if not message_id:
            return None
        with self._lock:
            entry = self._entries.get(message_id)
            if entry is None:
                return None
            context, _expires_at = entry
            return context

    def next_card_sequence(
        self,
        *,
        message_id: str,
        expected_card_sequence: int | None = None,
    ) -> int | None:
        """在进程内原子递增 sequence，并可按 scanner 快照的序号做 CAS。

        旧调用方不传期望值时保留 ``KeyError``/整数返回的兼容姿态。恢复路径必须
        带上它读到的 CardKit 序号；序号在渲染期间被任何写入方推进就返回
        ``None``，调用方不得把旧卡片发给 CardKit。

        与生产适配器同构：只用 ``card_sequence`` 一把 CAS。状态写入把两列写在
        同一处（见 :meth:`update_state`），而本方法只推进
        ``card_sequence``，所以 ``card_sequence`` 的判别力严格覆盖 ``state_version``。
        """
        if expected_card_sequence is not None and (
            isinstance(expected_card_sequence, bool)
            or not isinstance(expected_card_sequence, int)
            or expected_card_sequence <= 0
        ):
            raise ValueError("expected_card_sequence 必须是正整数")

        with self._lock:
            entry = self._entries.get(message_id)
            if entry is None:
                if expected_card_sequence is not None:
                    return None
                raise KeyError(message_id)
            context, expires_at = entry
            if (
                expected_card_sequence is not None
                and context.card_sequence != expected_card_sequence
            ):
                return None
            next_value = context.card_sequence + 1
            self._entries[message_id] = (
                ManagementCardContext(**{**context.__dict__, "card_sequence": next_value}),
                expires_at,
            )
            return next_value

    def update_state(
        self,
        *,
        message_id: str,
        state: str | None = None,
        dispatch_status: str | None = None,
        snapshot_fingerprint: str | None = None,
        last_trace_id: str | None = None,
    ) -> ManagementCardContext | None:
        """就地更新一条上下文的状态字段，非空传入的字段覆盖旧值。"""
        with self._lock:
            entry = self._entries.get(message_id)
            if entry is None:
                return None
            context, expires_at = entry
            changed = any(
                value is not None
                for value in (state, dispatch_status, snapshot_fingerprint, last_trace_id)
            )
            updated = ManagementCardContext(
                message_id=context.message_id,
                card_id=context.card_id,
                identifier=context.identifier,
                chat_id=context.chat_id,
                initiated_by_open_id=context.initiated_by_open_id,
                # 每次持久状态改变都占用一个新的整卡版本。除了让 CardKit 的更新
                # 顺序严格单调，这还使旧 scanner 在新状态写入后不能误把 needs_refresh
                # 清掉——判据是 ``card_sequence``（这里与 state_version 同时 +1，另外
                # 还会被 next_card_sequence() 单独推进）。``state_version`` 自 rc25 S-4a
                # 起不再参与 CAS，只作为状态代数继续维护，与生产表保持同一形状。
                card_sequence=context.card_sequence + (1 if changed else 0),
                state_version=context.state_version + (1 if changed else 0),
                snapshot_fingerprint=(
                    snapshot_fingerprint
                    if snapshot_fingerprint is not None
                    else context.snapshot_fingerprint
                ),
                context_deadline_at=context.context_deadline_at,
                state=state if state is not None else context.state,
                dispatch_status=dispatch_status
                if dispatch_status is not None
                else context.dispatch_status,
                last_trace_id=last_trace_id if last_trace_id is not None else context.last_trace_id,
                daily_correction_reported_at=(
                    context.daily_correction_reported_at
                    if state != "effective" or context.daily_correction_pending
                    else context.daily_correction_reported_at or datetime.now(UTC)
                ),
                # ``incomplete`` 只是失败状态，不足以证明每日批已补齐；保留既有
                # qualification，只有 settle_published_contexts 才能置位它。
                daily_correction_pending=context.daily_correction_pending,
                needs_refresh=context.needs_refresh or changed,
                visual_sequence=context.visual_sequence,
            )
            self._entries[message_id] = (updated, expires_at)
            return updated

    def list_needing_refresh(self, *, limit: int = 20) -> tuple[ManagementCardContext, ...]:
        """返回尚未被 CardKit 成功回写的上下文。"""
        if limit < 1:
            return ()
        with self._lock:
            return tuple(
                context for context, _expires_at in self._entries.values() if context.needs_refresh
            )[:limit]

    def mark_visual_refreshed(
        self,
        *,
        message_id: str,
        sequence: int,
        expected_card_sequence: int | None = None,
    ) -> bool:
        """仅在外部 CardKit update 成功后清除视觉回写水位。

        恢复路径传入本次实际领到的 CardKit 序号。任何写入方（状态写入或另一个
        恢复者领号）推进了 ``card_sequence``，CAS 就失败并保留 ``needs_refresh``，
        让下一轮从当前行重新渲染。状态写入必然同时推进 ``card_sequence``，因此
        不再单独判 ``state_version``。
        """
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise ValueError("sequence 必须是正整数")
        if expected_card_sequence is not None and (
            isinstance(expected_card_sequence, bool)
            or not isinstance(expected_card_sequence, int)
            or expected_card_sequence <= 0
        ):
            raise ValueError("expected_card_sequence 必须是正整数")

        with self._lock:
            entry = self._entries.get(message_id)
            if entry is None:
                return False
            context, expires_at = entry
            if (
                expected_card_sequence is not None
                and context.card_sequence != expected_card_sequence
            ):
                return False
            if sequence < context.visual_sequence:
                return False
            updated = ManagementCardContext(
                **{
                    **context.__dict__,
                    # 与 PostgreSQL ``CASE WHEN card_sequence <= sequence`` 同一
                    # 语义：若另一个回调已经拿到更高的序号，较早那次成功回写不能
                    # 清掉较新的待刷新水位。
                    "needs_refresh": context.card_sequence > sequence,
                    "visual_sequence": max(context.visual_sequence, sequence),
                }
            )
            self._entries[message_id] = (updated, expires_at)
            return True

    # 内存兼容实现没有可观测的发布 outbox，以下四个方法只是保留生产适配器的
    # 可选观测面，让 gateway 测试与旧调用方能不带分支地注入本类。
    def latest_publish_state_for_message(self, *, message_id: str) -> str | None:
        """内存实现没有发布 outbox 可观测，恒返回 ``None``。"""
        del message_id
        return None

    def settle_published_contexts(self) -> tuple[str, ...]:
        """内存实现没有待结算的发布上下文，恒返回空元组。"""
        return ()

    def unreported_daily_correction_ids(self) -> tuple[str, ...]:
        """内存实现没有每日纠偏待上报项，恒返回空元组。"""
        return ()

    def mark_daily_corrections_reported(self, *, message_ids: tuple[str, ...]) -> None:
        """内存实现无需记录每日纠偏上报状态，空操作。"""
        del message_ids


class ManagementCardDispatcher:
    """把 ``/admin user`` 查到的用户权限管理卡渲染并发到发起管理员本人私聊。

    与 :class:`ConfirmCardDispatcher` 是两个独立类：管理卡不是一次待确认操作，
    没有"发送结果需要落回某一行状态"这一步——发送成功与否只影响这次查询是否
    额外附带了一张卡，不影响任何数据库行的状态机，因此不需要注入
    ``PendingActionDeliveryTracker`` 这一类落库口。发送失败只记一条审计，
    调用方据此把失败当 best-effort 处理，不影响既有的文本回复。
    """

    def __init__(
        self,
        *,
        transport: ManagementCardTransport,
        catalog: CompanyMetricCatalog,
        audit: AuditSink,
        display_names: AdminDisplayNames,
        context_store: ManagementCardContextStore | None = None,
    ) -> None:
        """装配管理卡发送所需的传输、目录、审计、展示名与可选上下文登记口。"""
        self._transport = transport
        self._catalog = catalog
        self._audit = audit
        self._display_names = display_names
        # 发送侧登记上下文：``None``（既有调用点、未升级的测试）时行为与本参数
        # 加入之前逐字节一致——不登记任何映射，回调侧 ``identifier`` 缺失时
        # 维持既有「请重新查询 /admin user」拒绝路径。
        self._context_store = context_store

    def send(
        self,
        *,
        status: AdminUserStatusView,
        display_identifier: str,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
        initiated_by_open_id: str = "",
    ) -> ManagementCardDispatchResult:
        """渲染并发送这个用户的管理卡；发送成功时按需登记回调上下文。"""
        card = render_management_card(
            status,
            display_identifier=display_identifier,
            catalog=self._catalog,
            display_names=self._display_names,
        )
        try:
            created = self._transport.create(
                chat_id=chat_id,
                thread_id=thread_id,
                reply_to_message_id=reply_to_message_id,
                card=card,
            )
        except Exception as error:
            # 与 ConfirmCardDispatcher.send 同一白名单纪律：明确拒绝与结果不明
            # 在这里得到同一处理——管理卡发送失败不影响任何业务状态（它本来就
            # 不是一次待确认操作），因此不需要"作废"任何东西，只记一条审计。
            if isinstance(error, AdminCardDeliveryRejected):
                self._audit.record(
                    "admin.management_card_dispatch.send_failed",
                    target=display_identifier,
                    error=type(error).__name__,
                    code=error.code,
                    log_id=error.log_id,
                )
            else:
                self._audit.record(
                    "admin.management_card_dispatch.send_failed",
                    target=display_identifier,
                    error=type(error).__name__,
                )
            return ManagementCardDispatchResult(delivered=False)
        if self._context_store is not None:
            # 只在真正发出去之后登记：发送失败/结果不明的分支已经在上面
            # return，不会走到这里——没有实际送达的卡片就没有"这条 message_id
            # 对应哪次查询"这件事可以登记。
            self._context_store.remember(
                message_id=created.message_id,
                identifier=display_identifier,
                card_id=created.card_id,
                chat_id=chat_id,
                initiated_by_open_id=initiated_by_open_id,
                snapshot_fingerprint=management_card_fingerprint(status),
                # CardKit's create + reply consume the first two entities in the
                # card's sequence stream; the first in-place management update
                # must therefore start at 3.
                card_sequence=2,
            )
        return ManagementCardDispatchResult(delivered=True)
