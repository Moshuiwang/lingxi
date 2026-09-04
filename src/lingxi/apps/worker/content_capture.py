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
        self._config = config
        self._content_capture_writer = content_capture_writer
        self._on_year_grounding_suspect = on_year_grounding_suspect

    def capture(
        self, claimed: ClaimedTask, *, executor: WorkerTurnExecutor | None, question: str
    ) -> None:
        """内测轮内容级采集的写入点（Issue #251/#304 批次 3）。

        失败必须整体降级为一条结构化审计日志、不得向上抛——采集是旁路观测，
        不是任务能否完成的一部分（结构约束「采集失败不影响任务主流程」，见
        docs/技术设计/数据库设计.md 与 apps/worker/config.py 的模块文档）。

        ``executor`` 为 ``None``（进入 try 主体前就失败——例如
        ``UserMcpConfigError`` 从未走到构造 executor 那一步，或任务在开头就
        因带着 ``stop_requested`` 提前收口）时没有任何可采集的回合内容，直接
        跳过；``self._content_capture_writer`` 为 ``None``（未装配写入方，见
        ``apps/worker/cli.py`` 只在开关开启时才构造）同样跳过——两个判断分别
        兜住"这次没有回合内容"与"这次没有落库出口"，都不是错误，不记日志。

        成功构造出记录后还会调用 :meth:`_check_year_grounding_suspect`（Issue
        #326 批次 5 卡 E，年份接地护栏第二层检测），复用同一个 ``record`` 里
        已经解析好的问句与工具调用，不重新解析一遍。
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
        except Exception as error:  # noqa: BLE001 - 采集失败降级为日志，不丢用户结果
            logger.error(
                "内测轮内容级采集写入失败，任务结果不受影响 task_id=%s error=%s",
                claimed.task_id,
                type(error).__name__,
            )
        # 年份接地护栏第二层（Issue #326）：独立于上面采集写入的 try/except——
        # 检测本身的缺陷不能连带影响"记录有没有落库"的判断，也不能与写库失败
        # 共用同一条日志、分不清是采集坏了还是检测坏了。写库失败但记录已经在
        # 内存里构造出来时（`record is not None`）仍然照常检测：本护栏只依赖
        # 内存中的问句与工具调用，不依赖这次落库是否成功。
        if record is not None:
            self._check_year_grounding_suspect(record)

    def _check_year_grounding_suspect(self, record: ContentCaptureRecord) -> None:
        """年份接地护栏第二层：结构性检测 + 告警（Issue #326，批次 5 卡 E）。

        只做检测与告警，**不拦截、不改答案投递路径**——调用方
        ``_capture_content_if_enabled`` 已经在全部终态分支收口之后才调用本方法
        （见该方法末尾的调用点），本方法自身再包一层独立 try/except，双重保证
        检测代码的任何异常都不可能影响任务终态或已经完成的内容采集写入。

        判定逻辑（相对时间词表、年份提取、三条件与）全部在 ``core/
        year_grounding_guard.py``——本方法只负责"取当前年份、调用纯逻辑判定、
        把结果交给装配层注入的告警出口"这三步组装，不重复任何判定规则。
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
        except Exception as error:  # noqa: BLE001 - 检测是旁路，异常不得影响任务终态
            logger.error(
                "年份接地护栏检测异常，任务结果不受影响 task_id=%s error=%s",
                record.task_id,
                type(error).__name__,
            )
