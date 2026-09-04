"""worker 每轮的巡检职责：版本不可用、排队超时、心跳超时、投递过期与会话清理。

这些动作与"领取并执行一个任务"完全独立，共同点是「每一轮顺手看一眼世界」。队列适配器
不支持某一项时（旧的测试假队列）整项跳过，不半途认领又做不了事。

四类滞留各自独立上报，不合并成一个告警：运维看到"目标版本压根没有可用实例"与"单纯积压"
要做的诊断动作完全不同。
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
        """记住队列、配置与会话清理落点；首轮立即回收（上次回收时刻留空）。"""
        self._config = config
        self._queue = queue
        self._monotonic = monotonic
        self._session_root = session_root
        self._session_cleanup_batch_limit = session_cleanup_batch_limit
        self._on_task_stuck = on_task_stuck
        self._last_session_reclaim_at: float | None = None

    def run(self) -> list[TerminalTask]:
        """跑一轮巡检。

        Returns:
            这一轮被强制收敛出来的终态任务，供调用方判断这一轮有没有观察到任何终态。
        """
        terminals: list[TerminalTask] = []
        terminals.extend(self._fail_unavailable_versions())
        terminals.extend(self._reclaim_queued())
        terminals.extend(self._reclaim_stale_running())
        terminals.extend(self._expire_undelivered())
        self._cleanup_agent_sessions()
        self._reclaim_session_transcripts()
        return terminals

    def _fail_unavailable_versions(self) -> list[TerminalTask]:
        """收敛那些目标版本压根没有可用实例的任务。"""
        fail_versions = getattr(self._queue, "fail_unavailable_versions", None)
        if fail_versions is None:
            return []
        unavailable = fail_versions(
            available_versions=(self._config.target_worker_version,),
            unavailable_for=timedelta(seconds=self._config.worker_version_unavailable_seconds),
        )
        self._report_task_stuck("worker_version_unavailable", len(unavailable))
        return list(unavailable)

    def _reclaim_queued(self) -> list[TerminalTask]:
        """收敛排队太久的任务。"""
        reclaim_queued = getattr(self._queue, "reclaim_queued", None)
        if reclaim_queued is None:
            return []
        queued = reclaim_queued(max_wait=timedelta(seconds=self._config.queue_max_wait_seconds))
        self._report_task_stuck("queued_stuck", len(queued))
        return list(queued)

    def _reclaim_stale_running(self) -> list[TerminalTask]:
        """回收心跳超时的在途任务：还能重试的重排，重试用尽的收成终态。"""
        reclaim_stale = getattr(self._queue, "reclaim_stale_with_outcomes", None)
        if reclaim_stale is None:
            return []
        requeued, stale_terminals = reclaim_stale(
            older_than=timedelta(seconds=self._config.running_heartbeat_timeout_seconds),
            max_auto_retries=self._config.max_auto_retries,
        )
        self._report_task_stuck("running_heartbeat_timeout", len(requeued) + len(stale_terminals))
        self._report_task_stuck(
            "retry_exhausted",
            sum(item.error_kind == "retry_exhausted" for item in stale_terminals),
        )
        return list(stale_terminals)

    def _expire_undelivered(self) -> list[TerminalTask]:
        """收敛二十四小时到期仍未确认送达的投递终态（`V-投递-06`）。

        这一步只强制收敛任务状态、释放话题并清空事件正文；把结果对外展现为「投递已过期，
        请重新提问」仍是 gateway 在用户下一次主动发消息时的职责。二十四小时上限**不接受
        这里传参**：它由数据库触发器锁在事件行的过期时间列上，调用方不再持有另一份可以
        让它漂移的窗口配置。
        """
        expire_undelivered = getattr(self._queue, "expire_undelivered_terminals", None)
        if expire_undelivered is None:
            return []
        expired = expire_undelivered()
        self._report_task_stuck("awaiting_delivery_stuck", len(expired))
        return list(expired)

    def _reclaim_session_transcripts(self) -> None:
        """按配置节流的会话转录容量回收：磁盘预算超了才动手。"""
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
        """认领一批到期的会话转录，**先归档、再**物理清理。

        直接物理删除会把验收与取证现场需要的原始转录一并销毁，因此归档先于删除。

        没有可用的会话根目录、或队列不支持这组方法时整体跳过。单条处理失败不标记完成：
        软领取窗口到点会让下一个进程重新认领它。
        """
        if self._session_root is None:
            return
        claim = getattr(self._queue, "claim_session_cleanups", None)
        mark_done = getattr(self._queue, "mark_session_cleanups_done", None)
        if claim is None or mark_done is None:
            return
        try:
            pending = claim(limit=self._session_cleanup_batch_limit)
        except Exception as error:
            logger.error("Agent 会话清理队列认领失败 error=%s", type(error).__name__)
            return
        if not pending:
            return
        # 「根目录不存在」与「根目录存在、这个会话确实没有文件」是两件不同的事：删除函数
        # 对两者都返回 0（幂等设计），但把它们合并成同一个"标记完成"分支是错的——一个
        # 写错的会话根目录配置会让这一批本该被清理的会话被静默标记完成，而完成行不会被
        # 重新排队，事后改对配置也补不回来。这里已经认领过，因此不能什么都不做就返回：
        # 只要不标记完成，软领取窗口到点就会让它被重新认领。
        if not self._session_root.is_dir():
            logger.error(
                "Agent 会话清理根目录不存在，本轮跳过、不标记完成 pending=%d", len(pending)
            )
            return
        done_ids = [item.id for item in pending if self._delete_one_session(item)]
        if not done_ids:
            return
        try:
            mark_done(ids=done_ids)
        except Exception as error:
            # 标记失败只影响是否重试，不影响正确性：文件已经归档并删除，重复处理是幂等的。
            logger.error("Agent 会话清理标记完成失败 error=%s", type(error).__name__)

    def _delete_one_session(self, item: Any) -> bool:
        """归档并删除一条会话的转录；单条失败不影响本轮其余条目。"""
        try:
            delete_agent_session_files(
                self._session_root,
                item.agent_session_id,
                user_env_root=self._config.user_env_root,
                user_id=item.user_id,
            )
        except Exception as error:
            logger.error(
                "Agent 会话 JSONL 归档/物理删除失败 reason=%s error=%s",
                item.reason,
                type(error).__name__,
            )
            return False
        return True

    def _report_task_stuck(self, kind: str, count: int) -> None:
        """上报一类滞留；告警失败不应改变任务状态。"""
        if self._on_task_stuck is None or count <= 0:
            return
        try:
            self._on_task_stuck(kind, count)
        except Exception as error:
            logger.error(
                "任务滞留告警记录失败，任务状态保持由队列收口 error=%s", type(error).__name__
            )
