"""MCP 就绪确认的 **tick 驱动**形态：每轮只发起"已经到期"的那一次探针，**从不等待**。

存在的理由是一条硬约束：:class:`~lingxi.core.permission.mcp_readiness_base.
McpReadinessConfirmation` 会把调用线程阻塞最长十五分钟，而 ``lingxi-scheduler`` 是
**单进程顺序驱动多个职责**的循环，不能被一轮 tick 拖住其余定时职责。

**换掉的只有"等待"，没有第二套语义**：分类、五路取值、绑定、落库与审计全部走
:class:`~lingxi.core.permission.mcp_readiness_base._ReadinessProbeRunner` 的同一份实现。
**唯一的行为差别**：探针的实际发起时刻比计划晚最多一个调度周期，因此中途尝试的超窗
成功落非终态 ``technical_failure``，只有计划表最后一次或预算耗尽才收成 ``timed_out``。

:class:`ReadinessRecoveryTicker` 是给已经 ``timed_out`` 的绑定做周期性复检的独立形态。
全部状态从 ``mcp_sync_check`` 重建（:class:`ReadinessProgress`），因此不需要新表，
进程重启也不会让任何一条确认丢失或从头再来。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from lingxi.core.permission.mcp_readiness_base import (
    TERMINAL_OUTCOMES,
    ReadinessAttempt,
    ReadinessBinding,
    ReadinessOutcome,
    ReadinessSchedule,
    _ReadinessProbeRunner,
    evaluate_permission_presence,
    logger,
)


@dataclass(frozen=True)
class ReadinessProgress:
    """一个 ``(用户, 权限版本)`` 到目前为止的就绪确认进度。**可从库里重建**。

    这是 tick 形态的全部状态，而它**不需要新表**：``mcp_sync_check`` 本来就逐次记录了
    每一次判定的次序、开始时间与结论，三个字段合起来足以回答「还要不要探、下一次什么
    时候到期、是不是已经收口了」。节奏按**绝对时刻**算（起点 + 偏移），不按"上一次
    之后再等三分钟"累加——后者会把每一轮调度误差累积下去。
    """

    attempt_count: int = 0
    first_started_at: datetime | None = None
    terminal: bool = False

    def __post_init__(self) -> None:
        """校验次数与起点时刻是否自洽。"""
        if isinstance(self.attempt_count, bool) or not isinstance(self.attempt_count, int):
            raise ValueError("已判定次数必须是整数")
        if self.attempt_count < 0:
            raise ValueError("已判定次数不得为负")
        if self.attempt_count and self.first_started_at is None:
            # 有判定记录却说不出起点，就没有任何依据算"下一次什么时候到期"。
            # 静默按"现在"当起点会让整轮确认的窗口跟着每一次读取往后漂。
            raise ValueError("已经判定过的确认必须带着它的起点时刻")
        if self.first_started_at is not None and (
            not isinstance(self.first_started_at, datetime)
            or self.first_started_at.tzinfo is None
            or self.first_started_at.utcoffset() is None
        ):
            raise ValueError("确认起点必须是带时区的时间")

    @classmethod
    def from_checks(cls, checks: Any) -> ReadinessProgress:
        """从 ``mcp_sync_check`` 回读出来的判定行重建进度。

        只读三样东西：条数、第一行的 ``started_at``、有没有终态结论。刻意按**鸭子类型**
        接受（只要每个元素有 ``result`` 与 ``started_at``），因为 ``core`` 不 import
        ``adapters``。读不懂的 ``result`` 值**不当成终态**：方向选"多探几次"，不选
        "提前宣告结束"。
        """
        rows = tuple(checks)
        if not rows:
            return cls()
        terminal_values = {item.value for item in TERMINAL_OUTCOMES}
        return cls(
            attempt_count=len(rows),
            first_started_at=rows[0].started_at,
            terminal=any(str(row.result) in terminal_values for row in rows),
        )


def next_probe_due(schedule: ReadinessSchedule, progress: ReadinessProgress) -> datetime | None:
    """下一次探针**什么时候到期**；``None`` 表示不该再探（已收口或预算用尽）。

    纯函数，因此"tick 形态到底按什么节奏探"可以被直接断言。计划表与阻塞形态**同一份**
    （:meth:`ReadinessSchedule.attempt_offsets`）。本函数**要求已经有过至少一次判定**：
    第一次不需要到期判定，发布读回一致后立即发起。
    """
    if not isinstance(progress, ReadinessProgress):
        raise TypeError("进度必须是 ReadinessProgress")
    if progress.attempt_count < 1 or progress.first_started_at is None:
        raise ValueError("第一次探针不需要到期判定：发布读回一致后立即发起")
    if progress.terminal:
        return None
    offsets = schedule.attempt_offsets()
    if progress.attempt_count >= len(offsets):
        return None
    return progress.first_started_at + timedelta(seconds=offsets[progress.attempt_count])


#: tick 形态发起探针时的超窗处置：**非终态**，让计划表继续往下走。单写一份是为了让
#: "tick 的每一次探针都用同一套超窗语义"可被指着看，也可被一次改坏。
_TICK_OVERRUN = {
    "overrun_outcome": ReadinessOutcome.TECHNICAL_FAILURE,
    "overrun_error": "probe_overran_timeout",
}


class ReadinessTicker(_ReadinessProbeRunner):
    """同一套就绪语义的 tick 驱动形态：每轮只发起"已经到期"的那一次探针，从不等待。

    见模块文档。**停机追赶**：进程停了半小时再起来，本形态按 offset 补做欠下的探针，任何一次
    成功都算就绪，即使在"发布后十五分钟"之外——合同十五分钟约束的是阻塞形态那条首次
    开通链；刷新链用户已在用 Lingxi，停机恢复后确认成功再通知，好过永远不告诉他。

    **探针未接线**（``probe=None``）：只处理 ``no_permission`` 一路，需要探针的一路
    本轮不推进、不落记录，端点配好后原样继续——装假探针会把"没接线"伪装成"接了但一直失败"。
    """

    name = "MCP 就绪确认（tick）"

    def advance(
        self,
        binding: ReadinessBinding,
        *,
        permissions: str,
        progress: ReadinessProgress,
        company_id: Any = None,
    ) -> ReadinessAttempt | None:
        """把这一条确认往前推**至多一步**，返回本轮新落库的判定；``None`` = 本轮什么都没做。

        次序与阻塞形态逐条对应：已收口就不再动（终态，也是"通知只发一次"的载体）；
        还没探过就先判有没有可等的权限，否则立即探第一次；探过但没到期就返回 ``None``；
        到期就探一次，若这已是计划表最后一次而未就绪，同一轮内立刻补 ``timed_out``；
        计划表已用尽却还没收口（进程恰好重启、或节奏被调小）时直接补 ``timed_out``。
        """
        if not isinstance(binding, ReadinessBinding):
            raise TypeError("就绪确认必须绑定 (用户, 权限版本)")
        if not isinstance(progress, ReadinessProgress):
            raise TypeError("进度必须是 ReadinessProgress")
        if progress.terminal:
            return None
        if progress.attempt_count == 0:
            return self._advance_first(binding, permissions, company_id)
        return self._advance_subsequent(binding, progress)

    def _advance_first(
        self, binding: ReadinessBinding, permissions: str, company_id: Any
    ) -> ReadinessAttempt | None:
        """还没探过：先判权限（撤权一路不需要探针），否则立即探第一次。"""
        started = self._now()
        if not evaluate_permission_presence(permissions, company_id=company_id):
            return self._attempt(
                binding,
                1,
                ReadinessOutcome.NO_PERMISSION,
                started,
                self._now(),
                error_code="no_publishable_permission",
            )
        if self._probe is None:
            return None
        offsets = self._schedule.attempt_offsets()
        return self._settle(binding, self._probe_once(binding, 1, **_TICK_OVERRUN), offsets)

    def _advance_subsequent(
        self, binding: ReadinessBinding, progress: ReadinessProgress
    ) -> ReadinessAttempt | None:
        """已经探过至少一次：探针未接线不收口，到期才探，计划表用尽就补超时。"""
        if self._probe is None:
            # 探针没接线时连收口都不做：预算耗尽的判定建立在"我们真的探过"之上，
            # 而这条确认一次都没探成。端点配好后从当前进度原样继续。
            return None
        offsets = self._schedule.attempt_offsets()
        if progress.attempt_count >= len(offsets):
            return self._timed_out(binding, progress.attempt_count + 1)
        due = next_probe_due(self._schedule, progress)
        if due is None or self._now() < due:
            return None
        return self._settle(
            binding,
            self._probe_once(binding, progress.attempt_count + 1, **_TICK_OVERRUN),
            offsets,
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _settle(
        self,
        binding: ReadinessBinding,
        attempt: ReadinessAttempt,
        offsets: tuple[int, ...],
    ) -> ReadinessAttempt:
        """刚发的这一次如果是计划表里的最后一次而且没成，**当场**补上收口记录。"""
        if attempt.terminal or attempt.attempt_no < len(offsets):
            return attempt
        return self._timed_out(binding, attempt.attempt_no + 1)

    def _timed_out(self, binding: ReadinessBinding, attempt_no: int) -> ReadinessAttempt:
        moment = self._now()
        logger.warning(
            "MCP 就绪确认预算耗尽 user=%s version=%s 已判定=%s",
            binding.user_id,
            binding.permission_version,
            attempt_no - 1,
        )
        return self._attempt(
            binding,
            attempt_no,
            ReadinessOutcome.TIMED_OUT,
            moment,
            self._now(),
            error_code="budget_exhausted",
        )


class ReadinessRecoveryTicker(_ReadinessProbeRunner):
    """给「已经判过 ``timed_out``」的绑定做**周期性复检**（``V-开通-18``）。

    首次开通那条链预算耗尽即返回 ``timed_out``，此后没有东西会再回来看这个人；
    :class:`ReadinessTicker` 看见终态就永远不再推进，而 ``timed_out`` 对首次开通链
    不是"处理完了"，只是合同的十五分钟到了。节奏与终止条件是调用方的产品决定（见
    :mod:`lingxi.apps.scheduler.late_readiness_recovery`），本类只回答"现在再探一次
    算不算就绪"。**从不**产出 ``timed_out`` 或 ``no_permission``；超窗成功降级为
    ``technical_failure``，永远不会升级成 ``timed_out``。
    """

    name = "MCP 就绪复检（超时后恢复）"

    def probe_after_timeout(
        self, binding: ReadinessBinding, *, attempt_no: int
    ) -> ReadinessAttempt | None:
        """对一个已经 ``timed_out`` 的绑定再探一次；``None`` = 探针未接线，本轮不推进。

        ``attempt_no`` 由调用方给出（这个人这一版权限一共判定过几次 + 1）：本类不读
        ``mcp_sync_check``（``core`` 不读库），落库时数据库会用
        ``COALESCE(MAX(attempt_no), 0) + 1`` 重新算一遍真实序号——这里传入的值只影响
        这一次审计事实里 ``attempt_no`` 好不好看，不影响落库结果。
        """
        if not isinstance(binding, ReadinessBinding):
            raise TypeError("就绪复检必须绑定 (用户, 权限版本)")
        if isinstance(attempt_no, bool) or not isinstance(attempt_no, int) or attempt_no < 1:
            raise ValueError("尝试次序必须是正整数")
        if self._probe is None:
            # 探针未接线：与 ReadinessTicker 同一条纪律，本轮不落任何记录、不烧预算，
            # 端点配好后从库里的进度原样继续（进度全在 mcp_sync_check 里）。
            return None
        return self._probe_once(binding, attempt_no, **_TICK_OVERRUN)

    def record_processing_failure(
        self, binding: ReadinessBinding, *, attempt_no: int, code: str
    ) -> ReadinessAttempt:
        """记一条**没有真的发起探针**、但需要占住这一次调度窗口的技术失败。

        恢复职责在探针**之后**的步骤抛出未预期异常时，如果这次窗口不留下任何记录，
        下一个调度周期会立刻把同一个人重新选中——候选查询看的是最后一次判定的时刻。
        一个持续失败的"毒候选"因此会一直占着窗口最前面，饿死排在它后面的候选。

        本方法**不调用探针**，只把这次失败按 ``technical_failure`` 记一行；``code``
        是错误分类，不是异常正文。
        """
        if not isinstance(binding, ReadinessBinding):
            raise TypeError("就绪复检必须绑定 (用户, 权限版本)")
        if isinstance(attempt_no, bool) or not isinstance(attempt_no, int) or attempt_no < 1:
            raise ValueError("尝试次序必须是正整数")
        if not isinstance(code, str) or not code.strip():
            raise ValueError("失败记录必须带错误码")
        started = self._now()
        return self._attempt(
            binding,
            attempt_no,
            ReadinessOutcome.TECHNICAL_FAILURE,
            started,
            self._now(),
            error_code=code.strip(),
        )
