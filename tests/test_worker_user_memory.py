"""``WorkerService._process_task`` 的用户记忆注入断言（Issue #357 S-H3-3 d 节）。

只测注入点本身（拼接、fail-open、未装配时零行为变化），不重复真库层面的取数/
隔离断言——那些在 ``tests/test_postgres_user_memory.py``。自建最小假队列，不依赖
``tests/test_worker_queue_consumer.py`` 的模块级夹具（保持独立、不跨测试文件耦合）。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from lingxi.adapters.postgres_conversation import ClaimedTask, TaskContext
from lingxi.apps.worker.config import WorkerConfig
from lingxi.apps.worker.service import WorkerService
from lingxi.core.user_memory import RenderedUserMemoryPrompt


def _seed_user_mcp_config(root: str, user_id: str) -> None:
    home = Path(root) / user_id
    home.mkdir(parents=True, exist_ok=True)
    (home / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "query": {
                        "type": "http",
                        "url": "https://example.invalid/mcp",
                        "headers": {"Authorization": "Bearer test-token"},
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class _FakeQueue:
    def __init__(self, *, user_id: str = "usr-1") -> None:
        self.claimed = ClaimedTask(
            task_id="tsk-1",
            conversation_id="cnv-1",
            user_id=user_id,
            prompt="问题",
            resumed_session=False,
            target_worker_version="stable",
            attempts=1,
            reply_to_message_id="msg-1",
            stop_requested=False,
        )
        self.context = TaskContext(
            task_id="tsk-1",
            conversation_id="cnv-1",
            user_id=user_id,
            prompt="问题",
            resumed_session=False,
            target_worker_version="stable",
            attempts=1,
            reply_to_message_id="msg-1",
            chat_id="chat-1",
            thread_id="topic-1",
            agent_session_id=None,
            stop_requested=False,
            side_effect_state="none",
        )
        self.events: list[dict[str, object]] = []
        self.terminals: list[dict[str, object]] = []

    def claim(self, **kwargs: object) -> list[ClaimedTask]:
        if self.claimed is None:  # type: ignore[comparison-overlap]
            return []
        claimed, self.claimed = self.claimed, None  # type: ignore[assignment]
        return [claimed]

    def task_context(self, **kwargs: object):
        return self.context

    def mark_side_effect(self, **kwargs: object) -> bool:
        return True

    def heartbeat(self, **kwargs: object) -> bool:
        return True

    def stop_requested(self, **kwargs: object) -> bool:
        return self.context.stop_requested

    def append_delivery_event(self, **kwargs: object) -> None:
        self.events.append(kwargs)

    def write_terminal_event(self, **kwargs: object) -> None:
        self.terminals.append(kwargs)


class _RecordingExecutor:
    """把构造时收到的 ``WorkerConfig`` 存起来供断言，``run_turn`` 恒定成功。"""

    received_configs: list[WorkerConfig] = []

    def __init__(self, config: WorkerConfig) -> None:
        type(self).received_configs.append(config)

    async def run_turn(self, prompt: str, **kwargs: object) -> dict:
        return {
            "turn": {"closed": True, "final_text": "结果", "session_id": None},
            "failure": None,
        }


class _RaisingMemoryReader:
    def fetch_prompt_segment(self, *, user_id: str) -> RenderedUserMemoryPrompt | None:
        raise RuntimeError("模拟真库连接失败")


class _RecordingMemoryReader:
    """记录调用参数，返回预设结果——用于断言 fetch_prompt_segment 收到的
    user_id 确实来自 claimed.user_id。"""

    def __init__(self, result: RenderedUserMemoryPrompt | None) -> None:
        self.result = result
        self.calls: list[str] = []

    def fetch_prompt_segment(self, *, user_id: str) -> RenderedUserMemoryPrompt | None:
        self.calls.append(user_id)
        return self.result


def _run_one_task(**service_kwargs: object) -> tuple[_FakeQueue, WorkerConfig]:
    _RecordingExecutor.received_configs = []
    queue = service_kwargs.pop("queue", None) or _FakeQueue()
    with tempfile.TemporaryDirectory() as root:
        _seed_user_mcp_config(root, queue.context.user_id)
        config = WorkerConfig(
            question="",
            read_only_tools=("mcp__query__list_metrics",),
            trace_id="01J00000000000000000000000",
            turn_timeout_seconds=1.0,
            worker_id="worker-test",
            target_worker_version="stable",
            heartbeat_interval_seconds=0.01,
            poll_interval_seconds=0.01,
            user_env_root=root,
            system_prompt="固定系统提示词",
        )
        service = WorkerService(
            config=config,
            queue=queue,
            executor_factory=lambda cfg, marker: _RecordingExecutor(cfg),
            **service_kwargs,
        )
        asyncio.run(service.process_once())
    assert len(_RecordingExecutor.received_configs) == 1
    return queue, _RecordingExecutor.received_configs[0]


class NoReaderConfiguredTests(unittest.TestCase):
    def test_no_reader_means_zero_behavior_change(self) -> None:
        """``user_memory_reader`` 默认 ``None``：system_prompt 与未加入本项之前
        逐字节一致，任务照常成功。"""

        queue, received = _run_one_task()

        self.assertEqual(received.system_prompt, "固定系统提示词")
        self.assertEqual(queue.terminals[0]["terminal_kind"], "success")


class SuccessfulInjectionTests(unittest.TestCase):
    def test_reader_result_is_appended_to_the_system_prompt(self) -> None:
        reader = _RecordingMemoryReader(
            RenderedUserMemoryPrompt(
                text="## 已登记的用户记忆\n- [术语映射] 大尼日 => 尼日利亚（登记于 2026-08-20）",
                truncated=False,
                total_entries=1,
                kept_entries=1,
            )
        )

        queue, received = _run_one_task(user_memory_reader=reader)

        self.assertIn("固定系统提示词", received.system_prompt)
        self.assertIn("大尼日", received.system_prompt)
        self.assertIn("尼日利亚", received.system_prompt)
        self.assertEqual(reader.calls, ["usr-1"], "必须用 claimed.user_id 查询")
        self.assertEqual(queue.terminals[0]["terminal_kind"], "success")

    def test_reader_is_queried_with_this_tasks_own_user_id(self) -> None:
        reader = _RecordingMemoryReader(None)
        queue = _FakeQueue(user_id="usr-specific")

        _run_one_task(user_memory_reader=reader, queue=queue)

        self.assertEqual(reader.calls, ["usr-specific"])

    def test_no_memory_registered_leaves_the_prompt_untouched(self) -> None:
        """``fetch_prompt_segment`` 返回 ``None``（该用户没有任何记忆）：
        system_prompt 不追加任何内容。"""

        reader = _RecordingMemoryReader(None)

        _, received = _run_one_task(user_memory_reader=reader)

        self.assertEqual(received.system_prompt, "固定系统提示词")

    def test_empty_text_result_also_leaves_the_prompt_untouched(self) -> None:
        reader = _RecordingMemoryReader(
            RenderedUserMemoryPrompt(text="", truncated=False, total_entries=0, kept_entries=0)
        )

        _, received = _run_one_task(user_memory_reader=reader)

        self.assertEqual(received.system_prompt, "固定系统提示词")


class FailOpenTests(unittest.TestCase):
    def test_a_query_failure_does_not_fail_the_task_and_the_prompt_is_unaffected(self) -> None:
        """fail-open（与 ``.mcp.json`` 的失败关闭相反）：记忆查询异常时任务照常
        成功，system_prompt 不带任何记忆段落，也不抛出异常中断任务。"""

        queue, received = _run_one_task(user_memory_reader=_RaisingMemoryReader())

        self.assertEqual(received.system_prompt, "固定系统提示词")
        self.assertEqual(
            queue.terminals[0]["terminal_kind"],
            "success",
            "记忆查询失败不得让任务本身失败",
        )


if __name__ == "__main__":
    unittest.main()
