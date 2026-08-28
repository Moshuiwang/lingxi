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

**注入前的内容安全校验**（Trace #373 H3 批量审查 P2-5）：``/memory list`` 出口
（``core/conversation/pipeline.py::_render_memory_list``）已经对每条记忆单独过一次
``config.content._validate_user_visible_text``（协议泄漏词表/固定误导性措辞），
撞线时替换成不回显内容的安全占位行——但 worker 注入路径此前没有复用同一道校验，
``memory_key``/``memory_value`` 是用户自己写入的自由文本（``/memory remember``
只限制形状是 ``key => value``，不限制内容，见 ``core/user_memory.py`` 模块文档
「不存数据值」一节），结构上无法保证它不会撞上 ``mcp__``/``trace_id=`` 这类协议
标识或换行注入的「### 系统指令」类内容，被逐字拼进 ``task_system_prompt``
（``apps/worker/service.py``）会让用户自己的记忆变成一次提示词注入。仅限用户自己
的会话（数据边界另有结构性保证，不是新增的跨用户风险面），但同一用户仍然不该
用这条路径把不安全内容送进自己任务的系统提示词。修法：查询后、拼装提示词前，
逐条复用 :func:`lingxi.config.content.validate_user_visible_text` 校验，撞线的
条目跳过注入（不进入 :func:`~lingxi.core.user_memory.render_user_memory_prompt`
的输入），并记一条结构化告警——与 ``/memory list`` 出口"单条撞线不拖累其余记忆"
同一姿态，只是这里是跳过注入而不是替换成占位行（worker 侧没有"占位提示词行"这个
概念）。
"""

from __future__ import annotations

import logging

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.adapters.postgres_conversation import _Transaction
from lingxi.config.content import (
    ContentCatalog,
    ContentSafetyError,
    default_content_catalog,
    validate_user_visible_text,
)
from lingxi.core.user_memory import (
    DEFAULT_MAX_PROMPT_CHARS,
    RenderedUserMemoryPrompt,
    UserMemoryEntry,
    render_user_memory_prompt,
)

logger = logging.getLogger(__name__)


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

        撞上内容安全校验的条目（见模块文档 P2-5）在这里被过滤掉，不会进入拼装；
        全部条目都被过滤后同样返回 ``None``（等价于"这个用户没有可注入的记忆"）。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                entries = _Transaction(connection).list_user_memory(user_id=user_id)
        if not entries:
            return None
        safe_entries = [entry for entry in entries if self._is_entry_safe(entry, user_id=user_id)]
        if not safe_entries:
            return None
        return render_user_memory_prompt(
            safe_entries,
            type_label=self._type_label,
            max_chars=self._max_prompt_chars,
        )

    @staticmethod
    def _is_entry_safe(entry: UserMemoryEntry, *, user_id: str) -> bool:
        """校验一条记忆是否可以安全拼进 worker 注入提示词——同 ``/memory list``
        出口复用的检查器，不回显具体内容，只记 ``memory_id`` 供追查。
        """

        try:
            validate_user_visible_text(f"{entry.memory_key}\n{entry.memory_value}")
        except ContentSafetyError:
            logger.warning(
                "worker.user_memory.entry_unsafe_skipped memory_id=%s user_id=%s",
                entry.memory_id,
                user_id,
            )
            return False
        return True

    def _type_label(self, memory_type: str) -> str:
        return self._catalog.text(f"memory.type_label.{memory_type}").text
