"""``core/user_memory.py`` 的纯函数断言（Issue #357 S-H3-3）。

只测渲染/截断逻辑本身，不碰数据库——真库层面的隔离/清除断言在
``tests/test_postgres_user_memory.py``（真库存取）、
``tests/test_pending_action_postgres.py``（停用清除）、
``tests/test_permission_publish_postgres.py``（权限变化清除）。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from lingxi.core.user_memory import (
    MEMORY_TYPES,
    RenderedUserMemoryPrompt,
    UserMemoryEntry,
    render_user_memory_prompt,
)

_LABELS = {
    "term_mapping": "术语映射",
    "calibration_preference": "口径偏好",
    "convention_template": "惯例模板",
}


def _label(memory_type: str) -> str:
    return _LABELS[memory_type]


def _entry(
    *,
    memory_id: str = "mem_1",
    memory_type: str = "term_mapping",
    memory_key: str = "大尼日",
    memory_value: str = "尼日利亚",
    created_at: datetime = datetime(2026, 8, 20, tzinfo=timezone.utc),
) -> UserMemoryEntry:
    return UserMemoryEntry(
        memory_id=memory_id,
        memory_type=memory_type,
        memory_key=memory_key,
        memory_value=memory_value,
        created_at=created_at,
    )


class RenderEmptyTests(unittest.TestCase):
    def test_no_entries_renders_empty_text(self) -> None:
        result = render_user_memory_prompt((), type_label=_label)

        self.assertEqual(result, RenderedUserMemoryPrompt(
            text="", truncated=False, total_entries=0, kept_entries=0
        ))


class RenderContentTests(unittest.TestCase):
    def test_renders_header_and_one_line_per_entry(self) -> None:
        result = render_user_memory_prompt((_entry(),), type_label=_label)

        self.assertIn("已登记的用户记忆", result.text)
        self.assertIn("[术语映射] 大尼日 => 尼日利亚（登记于 2026-08-20）", result.text)
        self.assertFalse(result.truncated)
        self.assertEqual(result.total_entries, 1)
        self.assertEqual(result.kept_entries, 1)

    def test_orders_entries_by_created_at_ascending_regardless_of_input_order(self) -> None:
        newer = _entry(
            memory_id="mem_new",
            memory_key="新条目",
            created_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
        )
        older = _entry(
            memory_id="mem_old",
            memory_key="老条目",
            created_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
        )

        result = render_user_memory_prompt((newer, older), type_label=_label)

        self.assertLess(result.text.index("老条目"), result.text.index("新条目"))

    def test_all_three_memory_types_use_their_own_label(self) -> None:
        entries = tuple(
            _entry(memory_id=f"mem_{t}", memory_type=t, memory_key=t)
            for t in MEMORY_TYPES
        )

        result = render_user_memory_prompt(entries, type_label=_label)

        for memory_type in MEMORY_TYPES:
            self.assertIn(f"[{_label(memory_type)}]", result.text)


class TruncationTests(unittest.TestCase):
    def test_stays_under_the_limit_drops_nothing(self) -> None:
        entries = tuple(
            _entry(memory_id=f"mem_{i}", memory_key=f"key{i}") for i in range(3)
        )

        result = render_user_memory_prompt(entries, type_label=_label, max_chars=100_000)

        self.assertFalse(result.truncated)
        self.assertEqual(result.kept_entries, 3)

    def test_over_the_limit_drops_the_oldest_entries_first(self) -> None:
        base = datetime(2026, 8, 1, tzinfo=timezone.utc)
        entries = tuple(
            _entry(
                memory_id=f"mem_{i}",
                memory_key=f"极长关键词占位以撑大单行长度{i:03d}" * 3,
                memory_value="极长说明占位以撑大单行长度" * 3,
                created_at=base + timedelta(days=i),
            )
            for i in range(20)
        )

        result = render_user_memory_prompt(entries, type_label=_label, max_chars=400)

        self.assertTrue(result.truncated)
        self.assertEqual(result.total_entries, 20)
        self.assertLess(result.kept_entries, 20)
        # 保留的是最新的若干条：最旧一条（mem_0）的 key 不应出现在结果里，
        # 最新一条（mem_19）必须出现。
        self.assertNotIn("关键词占位以撑大单行长度000", result.text)
        self.assertIn("关键词占位以撑大单行长度019", result.text)

    def test_even_the_single_newest_entry_over_the_limit_is_not_dropped_entirely(self) -> None:
        huge = _entry(memory_value="超长说明" * 2000)

        result = render_user_memory_prompt((huge,), type_label=_label, max_chars=100)

        # 宁可提示范围被截断，也不整体丢弃——文本必须非空、仍然带表头。
        self.assertNotEqual(result.text, "")
        self.assertIn("已登记的用户记忆", result.text)
        self.assertEqual(result.kept_entries, 1)
        self.assertTrue(result.truncated)


if __name__ == "__main__":
    unittest.main()
