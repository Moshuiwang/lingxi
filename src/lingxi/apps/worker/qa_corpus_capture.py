"""问答留存语料的旁路记录：终态收口之后把这一轮的素材写进正式留存表。

与内测采集记录器并存、互不知道对方；排在全部终态分支之后，自己包一层独立的
try/except——语料写没写进去都不能影响已经写好的任务终态。日志只记任务标识与异常
类型名：这条通道的每一段正文都不得进日志。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from lingxi.adapters.postgres_conversation import ClaimedTask
from lingxi.apps.worker.config import WorkerConfig
from lingxi.apps.worker.turn import WorkerTurnExecutor
from lingxi.core.delivery.turn_outcome import TerminalDecision
from lingxi.core.qa_corpus import QaCorpusRecord, build_qa_corpus_record

logger = logging.getLogger("lingxi.apps.worker.service")


class QaCorpusRecorder:
    """把一轮回合的语料写进正式留存表；没有装配写入口就整体跳过。"""

    def __init__(
        self, *, config: WorkerConfig, writer: Callable[[QaCorpusRecord], bool] | None
    ) -> None:
        """写入口可留空：开关未开时不构造记录、不碰执行器。"""
        self._config = config
        self._writer = writer

    def record(
        self,
        claimed: ClaimedTask,
        executor: WorkerTurnExecutor | None,
        question: str,
        decision: TerminalDecision,
        report: Mapping[str, Any],
        system_prompt_digest: str | None,
    ) -> None:
        """写一条语料；失败整体降级为一条只含任务标识与异常类型名的日志。

        ``decision`` 必须是终态收口**之后**的那一份（失败追溯引用已追加），语料里的实收
        正文才与写进投递事件的逐字同源。执行器为 ``None``（回合没跑起来）或任务没有标识
        （一次性回合）时没有可留存的素材，不是错误、不记日志。
        """
        if executor is None or self._writer is None or not claimed.task_id:
            return
        try:
            capture = executor.build_content_capture_record(
                task_id=claimed.task_id, worker_id=self._config.worker_id, question=question
            )
            if capture is None:
                return
            self._writer(
                build_qa_corpus_record(
                    capture,
                    task=claimed,
                    decision=decision,
                    report=report,
                    system_prompt_digest=system_prompt_digest,
                    model=self._config.model,
                )
            )
        except Exception as error:
            logger.error(
                "问答留存语料写入失败，任务结果不受影响 task_id=%s error=%s",
                claimed.task_id,
                type(error).__name__,
            )
