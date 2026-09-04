"""内测轮内容级采集与年份接地护栏第二层：两条旁路观测。

两者都排在全部终态分支之后，且各自包一层独立的 try/except——它们的任何异常都不得
影响已经写好的任务终态。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from lingxi.adapters.postgres_conversation import ClaimedTask
from lingxi.apps.worker.config import WorkerConfig
from lingxi.apps.worker.service_ports import YearGroundingSuspectCallback
from lingxi.apps.worker.turn import WorkerTurnExecutor
from lingxi.core.innertest_content_capture import ContentCaptureRecord
from lingxi.core.year_grounding_guard import detect_year_grounding_suspect

logger = logging.getLogger("lingxi.apps.worker.service")


class ContentCaptureRecorder:
    """采集一轮回合的内容并顺带跑一次年份接地护栏检测。"""

    def __init__(
        self,
        *,
        config: WorkerConfig,
        content_capture_writer: Callable[[ContentCaptureRecord], None] | None,
        on_year_grounding_suspect: YearGroundingSuspectCallback | None,
    ) -> None:
        """两个出口都可留空：没有装配方就整体跳过，不构造记录、不尝试写库。"""
        self._config = config
        self._content_capture_writer = content_capture_writer
        self._on_year_grounding_suspect = on_year_grounding_suspect

    def capture(
        self, claimed: ClaimedTask, *, executor: WorkerTurnExecutor | None, question: str
    ) -> None:
        """采集这一轮的内容并顺带跑一次年份接地检测。

        失败整体降级为一条结构化日志、不得向上抛：采集是旁路观测，不是任务能否完成的
        一部分。执行器为 ``None``（在构造出它之前就失败，或任务开头就被停止）时没有任何
        可采集的回合内容；没有装配写入口时同样跳过——两者都不是错误，不记日志。

        年份检测独立于采集写入的异常处理：检测本身的缺陷不能连带影响"记录有没有落库"
        的判断，也不能与写库失败共用同一条日志、让人分不清是哪一边坏了。写库失败但记录
        已经构造出来时照常检测——检测只依赖内存里的问句与工具调用。
        """
        if executor is None or self._content_capture_writer is None:
            return
        record: ContentCaptureRecord | None = None
        try:
            record = executor.build_content_capture_record(
                task_id=claimed.task_id,
                worker_id=self._config.worker_id,
                question=question,
            )
            if record is not None:
                self._content_capture_writer(record)
        except Exception as error:
            logger.error(
                "内测轮内容级采集写入失败，任务结果不受影响 task_id=%s error=%s",
                claimed.task_id,
                type(error).__name__,
            )
        if record is not None:
            self._check_year_grounding_suspect(record)

    def _check_year_grounding_suspect(self, record: ContentCaptureRecord) -> None:
        """年份接地护栏第二层：结构性检测＋告警。

        只做检测与告警，**不拦截、不改答案投递路径**。调用方已经在全部终态分支收口之后
        才走到这里，这里再包一层独立的异常处理，双重保证检测代码的任何异常都不可能影响
        任务终态或已经完成的采集写入。

        判定规则全部住在纯逻辑层，这里只负责"取当前年份、调用判定、把结果交给装配层注入
        的告警出口"这三步组装，不重复任何规则。
        """
        if self._on_year_grounding_suspect is None:
            return
        try:
            suspect = detect_year_grounding_suspect(
                task_id=record.task_id,
                question=record.question_content,
                tool_calls=record.tool_calls,
                current_year=datetime.now().year,
            )
            if suspect is not None:
                self._on_year_grounding_suspect(suspect.to_alert_fields())
        except Exception as error:
            logger.error(
                "年份接地护栏检测异常，任务结果不受影响 task_id=%s error=%s",
                record.task_id,
                type(error).__name__,
            )
