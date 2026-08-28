"""worker 侧用户记忆只读拼装（Issue #357 S-H3-3 d 节）。

worker 每个任务按 ``claimed.user_id`` 查一次 ``user_memory``，拼成一段附加提示词
文本插进 ``task_system_prompt``。**只读**：本模块不提供任何写入方法——写入路径唯一
入口是 ``/memory`` 命令面（``core/conversation/pipeline.py`` 经
``adapters.postgres_conversation._Transaction`` 的四个方法），worker 不需要、
也不应该获得写用户记忆的能力。

**连接姿态**：不复用 gateway/scheduler 常驻轮询那条连接（``_TaskQueueBase.
_run_polling_operation``）——那条通路的重试/提交语义是为 ``claim()`` 这一类
"抢占式发现查询"量身定制的（见该方法文档），本查询是每任务一次性的旁路读取，
语义不同，混用会让一次记忆查询失败的重试行为意外影响任务领取的正确性。按调度卡
裁定的默认方案，走与 ``PostgresContentCaptureWriter``（Issue #251/#304 批次 3）
同一姿态——每次调用现开一条连接、走 ``lingxi.adapters.postgres.connect`` 的既定
超时策略，用完即关。

**失败姿态是 fail-open**，但**不在本模块内吞异常**——真正的降级（结构化告警、
不带记忆继续跑）由调用方（``apps/worker/service.py`` 的 ``_process_task``）负责，
与该模块处理 ``system_prompt_file`` 读取失败（``prompt_degraded``）同一姿态、同一
调用层级。本模块只管"查+拼"，异常原样向上抛，保持单一职责。
"""

from __future__ import annotations

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.adapters.postgres_conversation import _Transaction
from lingxi.config.content import ContentCatalog, default_content_catalog
from lingxi.core.user_memory import (
    DEFAULT_MAX_PROMPT_CHARS,
    RenderedUserMemoryPrompt,
    render_user_memory_prompt,
)


class PostgresUserMemoryReader:
    """按用户查询并拼装记忆提示词段落的唯一入口。"""

    def __init__(
        self,
        dsn: str,
        *,
        timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS,
        catalog: ContentCatalog | None = None,
        max_prompt_chars: int = DEFAULT_MAX_PROMPT_CHARS,
    ) -> None:
        self._dsn = dsn
        self._timeouts = timeouts
        self._catalog = catalog or default_content_catalog()
        self._max_prompt_chars = max_prompt_chars

    def fetch_prompt_segment(self, *, user_id: str) -> RenderedUserMemoryPrompt | None:
        """查询该用户的全部记忆并拼成提示词段落；该用户没有任何记忆时返回
        ``None``（与"查询失败"区分：后者由异常向上抛出，调用方另行处理）。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                entries = _Transaction(connection).list_user_memory(user_id=user_id)
        if not entries:
            return None
        return render_user_memory_prompt(
            entries,
            type_label=self._type_label,
            max_chars=self._max_prompt_chars,
        )

    def _type_label(self, memory_type: str) -> str:
        return self._catalog.text(f"memory.type_label.{memory_type}").text
