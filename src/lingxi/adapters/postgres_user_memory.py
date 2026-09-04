"""worker 侧用户记忆只读拼装。

worker 每个任务按 ``claimed.user_id`` 查一次 ``user_memory``，拼成一段附加提示词
文本插进 ``task_system_prompt``。**只读**：本模块不提供任何写入方法，写入路径
唯一入口是 ``/memory`` 命令面。**连接姿态**：不复用 gateway/scheduler 常驻轮询
那条连接——那条通路的重试/提交语义是为"抢占式发现查询"量身定制的，混用会让
一次记忆查询失败的重试行为意外影响任务领取；本查询每次调用现开一条连接、用完
即关。**失败姿态是 fail-open，但不在本模块内吞异常**：真正的降级由调用方
（``apps/worker/service.py``）负责，本模块只管"查+拼"，异常原样向上抛。

**注入前的内容安全校验**见 :meth:`PostgresUserMemoryReader._is_entry_safe`：
用户自己写入的记忆自由文本结构上可能撞上协议标识或提示词注入内容，逐字拼进
``task_system_prompt`` 前必须先过一遍安全校验。
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
        """记下 DSN、超时、内容目录与最大提示词长度；不在构造时连接数据库。"""
        self._dsn = dsn
        self._timeouts = timeouts
        self._catalog = catalog or default_content_catalog()
        self._max_prompt_chars = max_prompt_chars

    def fetch_prompt_segment(self, *, user_id: str) -> RenderedUserMemoryPrompt | None:
        """查询该用户的全部记忆并拼成提示词段落。

        该用户没有任何记忆时返回 ``None``（与"查询失败"区分：后者由异常向上
        抛出）。撞上内容安全校验的条目（见 :meth:`_is_entry_safe`）在这里被
        过滤掉，全部条目都被过滤后同样返回 ``None``。
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
        """校验一条记忆是否可以安全拼进 worker 注入提示词。

        与 ``/memory list`` 出口复用同一个检查器：``memory_key``/``memory_value``
        是用户自己写入的自由文本，形状不受内容限制，结构上无法保证不会撞上
        ``mcp__``/``trace_id=`` 这类协议标识或换行注入的「### 系统指令」类内容
        ——被逐字拼进 ``task_system_prompt`` 会让用户自己的记忆变成一次提示词
        注入（仅限用户自己的会话，不是跨用户风险面，但同一用户仍不该借这条
        路径污染自己任务的系统提示词）。撞线条目在这里被跳过，不进入拼装；
        不回显具体内容，只记 ``memory_id`` 供追查。
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
