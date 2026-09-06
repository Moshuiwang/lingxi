"""权威决定链上「本地覆盖」这条来源：读取与完整性检查，三个入口共用一份。

真实权限 =（银河 ∪ 本地授权）− 本地抑制。四段职责：**读取来源**与**检查完整性**
在本模块；**计算**在 ``local_override.resolve_local_overrides`` 与
``merge_sources.merge_permission_sources``；**提交**在 ``record_decision``（版本
推进、账号状态复核与用户行锁都在那一层）。前两段此前三处各一份拷贝，"读不出来时
怎么收敛"曾因此不一致，收拢之后判据只剩一份。

**入口特有的东西不进来**：身份从哪里查、留痕事件叫什么名字、失败时通知谁、收敛到
哪个终态，全部留在各自入口；本模块不认识任何事件名，也不持有发送端口。

**如实登记的残留边界**：完整性检查的无条件重读只关掉了"补齐停在行锁上"撑开的无界
窗口；重读到提交之间仍有毫秒级窗口，此间提交的撤销仍可能被这一轮的旧结论盖过一轮。
彻底关掉它要在提交事务内复核来源代次，属设计改动，不在本模块职责里。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from lingxi.core.permission.legacy_diff import missing_all_scope_metrics
from lingxi.core.permission.local_override import (
    LocalOverrideReadError,
    LocalPermissionOverrideEntry,
    ResolvedLocalOverrides,
    resolve_local_overrides,
)


class LocalOverrideReader(Protocol):
    """按用户读回当前生效的本地覆盖条目。"""

    def effective_entries(self, *, user_id: str) -> Sequence[LocalPermissionOverrideEntry]:
        """这个人此刻生效的全部条目。"""
        ...


class AllScopeExpander(Protocol):
    """「2.0 迁移导入·全部」组的补行口。"""

    def expand_all_scope_group(
        self, *, user_id: str, group_id: str, metrics: Sequence[str], now: datetime
    ) -> int:
        """给该组补齐 ``metrics`` 里缺的指标；返回实际新增的行数。"""
        ...


@dataclass(frozen=True)
class AllScopeBackfill:
    """「检查完整性」这一段的协作者：补行口 + 入口自己的两条留痕回调。

    ``on_failed(user_id, error)`` 在某个组补行失败时调用（本次按既有条目继续算）；
    ``on_succeeded(user_id, added)`` 在补行成功时调用，带上实际新增的行数。
    """

    expander: AllScopeExpander
    on_failed: Callable[[str, Exception], None]
    on_succeeded: Callable[[str, int], None]


class LocalOverrideDecisionSource:
    """本地覆盖这条来源的**唯一**读取口：读一次、按需补齐、解析成生效集合。

    ``reader`` 为 ``None`` 表示装配层没接这条来源，:meth:`resolve` 返回 ``None``
    （合并对"没有本地源"恒等）。**读不出来一律抛**，不返回 ``None``、也不返回空集。
    """

    def __init__(
        self,
        *,
        reader: LocalOverrideReader | None,
        on_read_failed: Callable[[str, Exception], None],
        metric_translation_map: Mapping[str, Mapping[str, Sequence[str]]],
        clock: Callable[[], datetime],
        backfill: AllScopeBackfill | None = None,
    ) -> None:
        """接线读取口、补齐协作者、指标映射、时钟与入口自己的读失败留痕。"""
        self._reader = reader
        self._on_read_failed = on_read_failed
        self._metric_translation_map = metric_translation_map
        self._clock = clock
        self._backfill = backfill

    def resolve(self, user_id: str) -> ResolvedLocalOverrides | None:
        """读这个人当前生效的本地覆盖。**未装配**返回 ``None``，**读不出来一律抛**。

        两种状态刻意不同：未装配是部署事实（合并按"没有本地源"处理，静默）；读取
        失败是故障，一旦也返回 ``None`` 就与"确实没有本地覆盖"坍缩成同一件事，而
        合并对此恒等——一次数据库抖动会产出一份少了本地补授却看起来完整的权限决定。

        Raises:
            LocalOverrideReadError: 本地覆盖来源读取失败。
        """
        if self._reader is None:
            return None
        entries = self.read_entries(user_id)
        entries = self._complete_all_scope(user_id, entries)
        return resolve_local_overrides(user_id=user_id, entries=entries)

    def read_entries(self, user_id: str) -> tuple[LocalPermissionOverrideEntry, ...]:
        """读一次条目；**读不出来一律回调入口留痕后抛**。

        合并前的首次读取与补齐之后的重读都走这里，两次读取的失败姿态因此天然一致。
        曾经不一致过——重读失败原地退回旧条目照旧发布，于是刚补进的那条指标不在本
        次的权限决定里，产出的仍然是一份少了本地补授、看起来却完整的决定。

        Raises:
            LocalOverrideReadError: 本地覆盖来源读取失败。
        """
        if self._reader is None:
            return ()
        try:
            return tuple(self._reader.effective_entries(user_id=user_id))
        except Exception as error:
            self._on_read_failed(user_id, error)
            raise LocalOverrideReadError() from error

    def _complete_all_scope(
        self, user_id: str, entries: tuple[LocalPermissionOverrideEntry, ...]
    ) -> tuple[LocalPermissionOverrideEntry, ...]:
        """给「全部」组随当前映射补齐新指标，**只要有缺项就重读一次条目**。

        缺才补、同组标识、撤销过的组不参与。补行失败只留痕、不抛。**重读无条件**：
        补行口报告"一行都没新增"最常见的成因，恰恰是这个组在首次读取之后被整组撤销
        了；退回旧条目等于拿撤销之前的事实提交权限决定，把已收回的指标重新发布出去。
        代价是有缺项时每人每轮多读一次。重读失败照抛，这一轮不发布这个人。

        Raises:
            LocalOverrideReadError: 补齐之后的重读失败。
        """
        if self._backfill is None:
            return entries
        missing = missing_all_scope_metrics(entries, self._metric_translation_map)
        if not missing:
            return entries
        for group_id, metrics in missing.items():
            self._expand_one_group(user_id, group_id, metrics)
        return self.read_entries(user_id)

    def _expand_one_group(self, user_id: str, group_id: str, metrics: Sequence[str]) -> None:
        """补一个组；失败只回调入口留痕，本次照常继续（补不进去不等于算不出来）。"""
        assert self._backfill is not None  # 调用点已判过
        try:
            added = self._backfill.expander.expand_all_scope_group(
                user_id=user_id, group_id=group_id, metrics=metrics, now=self._clock()
            )
        except Exception as error:
            self._backfill.on_failed(user_id, error)
            return
        self._backfill.on_succeeded(user_id, added)


__all__ = [
    "AllScopeBackfill",
    "AllScopeExpander",
    "LocalOverrideDecisionSource",
    "LocalOverrideReader",
]
