"""worker 每轮的巡检职责：版本不可用、排队超时、心跳超时、投递过期与会话清理。

这些动作与"领取并执行一个任务"完全独立，共同点是「每一轮顺手看一眼世界」；队列适配器
不支持某一项时（旧的测试假队列）整项跳过，不半途认领又做不了事。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from lingxi.adapters.postgres_conversation import TerminalTask
from lingxi.apps.worker.config import WorkerConfig
from lingxi.apps.worker.service_ports import TaskStuckCallback
from lingxi.apps.worker.session_cleanup import (
    delete_agent_session_files,
    run_session_transcript_reclaim,
)

logger = logging.getLogger("lingxi.apps.worker.service")


class QueueHousekeeper:
    """把每一轮的巡检动作集中在一个对象上。"""

    def __init__(
        self,
        *,
        config: WorkerConfig,
        queue: Any,
        monotonic: Callable[[], float],
        session_root: Path | None,
        session_cleanup_batch_limit: int,
        on_task_stuck: TaskStuckCallback | None,
    ) -> None:
        self._config = config
        self._queue = queue
        self._monotonic = monotonic
        self._session_root = session_root
        self._session_cleanup_batch_limit = session_cleanup_batch_limit
        self._on_task_stuck = on_task_stuck
        self._last_session_reclaim_at: float | None = None

    def run(self) -> list[TerminalTask]:
        terminals: list[TerminalTask] = []
        fail_versions = getattr(self._queue, "fail_unavailable_versions", None)
        if fail_versions is not None:
            unavailable = fail_versions(
                available_versions=(self._config.target_worker_version,),
                unavailable_for=timedelta(seconds=self._config.worker_version_unavailable_seconds),
            )
            terminals.extend(unavailable)
            # 独立于"排队太久"（queued_stuck）：这一类是"目标 worker 版本压根没有
            # 可用实例"，运维需要看到的诊断动作不同（部署缺一个版本 vs 单纯积压），
            # 因此用独立的告警类型（Issue #153 最小可观测性第四类）。
            self._report_task_stuck("worker_version_unavailable", len(unavailable))
        reclaim_queued = getattr(self._queue, "reclaim_queued", None)
        if reclaim_queued is not None:
            queued = reclaim_queued(max_wait=timedelta(seconds=self._config.queue_max_wait_seconds))
            terminals.extend(queued)
            self._report_task_stuck("queued_stuck", len(queued))
        reclaim_stale = getattr(self._queue, "reclaim_stale_with_outcomes", None)
        if reclaim_stale is not None:
            requeued, stale_terminals = reclaim_stale(
                older_than=timedelta(seconds=self._config.running_heartbeat_timeout_seconds),
                max_auto_retries=self._config.max_auto_retries,
            )
            terminals.extend(stale_terminals)
            self._report_task_stuck(
                "running_heartbeat_timeout", len(requeued) + len(stale_terminals)
            )
            self._report_task_stuck(
                "retry_exhausted",
                sum(item.error_kind == "retry_exhausted" for item in stale_terminals),
            )
        # 二十四小时到期仍未确认送达的投递终态：状态合同第 8 条、V-投递-06。
        # 这一步只强制收敛任务状态、释放话题并清空事件正文；把清理结果对外展现
        # 为"投递已过期，请重新提问"仍是 Gateway（下一次用户主动消息触发）的职责。
        # 二十四小时上限不接受这里传参：它由迁移 0059 的触发器锁定在
        # task_delivery_event.expires_at 列上，调用方不再持有另一份可以让它
        # 漂移的窗口配置（内审 P2-1）。
        expire_undelivered = getattr(self._queue, "expire_undelivered_terminals", None)
        if expire_undelivered is not None:
            expired = expire_undelivered()
            terminals.extend(expired)
            # 与 core.alerting.AlertKind.AWAITING_DELIVERY_STUCK 对齐（Issue #153
            # 最小可观测性第二类：queued/running/awaiting-delivery 滞留三选一）。
            self._report_task_stuck("awaiting_delivery_stuck", len(expired))
        self._cleanup_agent_sessions()
        self._reclaim_session_transcripts()
        return terminals

    def _reclaim_session_transcripts(self) -> None:
        """Run the independent, throttled Issue #494 capacity-reclaim path."""
        if self._session_root is None or self._config.session_disk_budget_bytes <= 0:
            return
        now = self._monotonic()
        last = self._last_session_reclaim_at
        if last is not None and now - last < self._config.session_reclaim_interval_seconds:
            return
        self._last_session_reclaim_at = now
        run_session_transcript_reclaim(
            self._session_root,
            budget_bytes=self._config.session_disk_budget_bytes,
            low_water_ratio=self._config.session_disk_low_water_ratio,
            min_age_seconds=self._config.session_reclaim_min_age_seconds,
        )

    def _cleanup_agent_sessions(self) -> None:
        """认领并（先归档、再）物理清理一批到期的 Agent 会话 JSONL（Issue #153；
        归档见 Issue #291 L6 取证结论、``session_cleanup.py`` 模块文档「删除前先
        归档」——``/new`` 等触发点排的清理不再直接销毁原始转录）。

        没有配置可用的会话根目录，或队列适配器不支持这组方法（旧测试用的假队列）
        时整体跳过——不半途认领又做不了事，让请求继续排队给下一个真正能处理它的
        进程。单条处理失败不清 ``done_at``：下一轮的十分钟软领取窗口会重试，见迁移
        0061 头部注释。
        """

        if self._session_root is None:
            return
        claim = getattr(self._queue, "claim_session_cleanups", None)
        mark_done = getattr(self._queue, "mark_session_cleanups_done", None)
        if claim is None or mark_done is None:
            return
        try:
            pending = claim(limit=self._session_cleanup_batch_limit)
        except Exception as error:  # noqa: BLE001 - 清理认领失败不能带走任务职责
            logger.error("Agent 会话清理队列认领失败 error=%s", type(error).__name__)
            return
        if not pending:
            return
        # 根目录本身不存在与"根目录存在、这个会话确实没有文件"是两件不同的事
        # （PR #173 独立复核 P2-6）：`delete_agent_session_files` 对两者都返回
        # `0`（幂等设计，理由见该函数文档），但把这两种情况合并成同一个"标记
        # 完成"分支是错的——`.env.example` 里一个写错的 `LINGXI_WORKER_
        # SESSION_ROOT`（例如示例值 `/var/lib/lingxi/users/.claude/projects`，
        # 而镜像固定 `HOME=/tmp`）会让这一批本该被清理的会话被静默标记完成；
        # `agent_session_cleanup.agent_session_id` 是唯一索引 +
        # `ON CONFLICT DO NOTHING`，标记完成的行不会被重新排队，事后改对配置
        # 也补不回来。这里已经认领（`claimed_at`/`worker_id` 已写），因此不能
        # 简单地什么都不做就返回——十分钟软领取窗口本来就是为这类"认领了但
        # 这次处理不了"设计的重试兜底，只需要不调用 `mark_done` 即可让它到点
        # 被下一个进程重新认领。
        if not self._session_root.is_dir():
            logger.error(
                "Agent 会话清理根目录不存在，本轮跳过、不标记完成 pending=%d",
                len(pending),
            )
            return
        done_ids: list[str] = []
        for item in pending:
            try:
                # 归档先于删除（Issue #291 L6 取证结论，见 session_cleanup.py
                # 模块文档「删除前先归档」）：`/new` 等触发点排的清理如果直接
                # 物理删除，会把验收/取证现场需要的原始 JSONL 一并销毁。
                delete_agent_session_files(
                    self._session_root,
                    item.agent_session_id,
                    user_env_root=self._config.user_env_root,
                    user_id=item.user_id,
                )
            except Exception as error:  # noqa: BLE001 - 单条失败不影响本轮其余条目
                logger.error(
                    "Agent 会话 JSONL 归档/物理删除失败 reason=%s error=%s",
                    item.reason,
                    type(error).__name__,
                )
                continue
            done_ids.append(item.id)
        if done_ids:
            try:
                mark_done(ids=done_ids)
            except Exception as error:  # noqa: BLE001 - 标记失败只影响是否重试，不影响正确性
                logger.error("Agent 会话清理标记完成失败 error=%s", type(error).__name__)

    def _report_task_stuck(self, kind: str, count: int) -> None:
        if self._on_task_stuck is None or count <= 0:
            return
        try:
            self._on_task_stuck(kind, count)
        except Exception as error:  # noqa: BLE001 - 告警失败不应改变任务状态
            import logging

            logging.getLogger(__name__).error(
                "任务滞留告警记录失败，任务状态保持由队列收口 error=%s", type(error).__name__
            )
