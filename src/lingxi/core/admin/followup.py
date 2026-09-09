"""管理动作后处理的固定阶段与持久交接契约。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

GATEWAY_STAGES = frozenset(
    {
        "permission_recompute",
        "publish_observe",
        "terminal_card_refresh",
        "management_card_refresh",
        "group_notify",
        "confirmation_card_send",
    }
)
SCHEDULER_STAGES = frozenset({"innertest_preprovision", "innertest_readiness_check"})
STAGES = GATEWAY_STAGES | SCHEDULER_STAGES
EXTERNAL_STAGES = frozenset({"group_notify", "confirmation_card_send"})
TERMINAL_STATUSES = frozenset({"succeeded", "skipped", "failed", "unknown"})


class FollowupCapacityError(RuntimeError):
    """未完成阶段已满，调用方必须回滚整个确认。"""


@dataclass(frozen=True, kw_only=True)
class FollowupSpec:
    """稳定业务身份，不接受可覆盖批次关联的外部身份。"""

    subject_key: str
    stage: str
    target_user_id: str | None = None
    target_version: int | None = None
    batch_id: str | None = None
    batch_item_id: str | None = None
    depends_on_id: str | None = None

    def __post_init__(self) -> None:
        """陌生阶段和可偷换的批次身份在接收前拒绝。"""
        if self.stage not in STAGES or not self.subject_key:
            raise ValueError("阶段或目标无效")
        if self.batch_item_id is not None and self.subject_key != self.batch_item_id:
            raise ValueError("阶段目标必须使用原批次项")
        if self.stage in SCHEDULER_STAGES and not self.batch_item_id:
            raise ValueError("首次开通阶段必须关联批次项")


@dataclass(frozen=True, kw_only=True)
class FollowupRef:
    """确认后可以回查的阶段引用。"""

    id: str
    stage: str
    status: str
    result_code: str | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, kw_only=True)
class ClaimedFollowup(FollowupSpec):
    """只允许当前领取代数写回的执行快照。"""

    id: str
    pending_action_id: str
    trace_id: str | None
    attempt: int
    lease_owner: str
    created_at: datetime
    effect_started_at: datetime | None = None
    external_ref: str | None = None
    contract_version: int = 1


@dataclass(frozen=True)
class RecoveryCounts:
    """过期领取的明确去向。"""

    recoverable: int = 0
    unknown: int = 0
    failed: int = 0


@dataclass(frozen=True)
class ShutdownReport:
    """停止报告不把仍在运行的线程说成已经取消。"""

    accepted: int = 0
    finished: int = 0
    recoverable: int = 0
    unknown: int = 0
    still_running: int = 0


class FollowupLifecycle(Protocol):
    """阶段消费者与 scheduler listener 共用的退出端口。"""

    def request_stop(self) -> None:
        """幂等关闭接收与领取。"""
        ...

    def drain_until(self, deadline_monotonic: float) -> ShutdownReport:
        """所有职责共享一个绝对截止时刻。"""
        ...
