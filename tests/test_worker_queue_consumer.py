"""Issue #90 的队列消费与失败终态断言；Issue #151 起收口方式改为写投递事件 outbox。

真库组覆盖 V-队列-06/08/09、V-会话-07 的状态、隔离与轮询；纯逻辑组覆盖 V-会话-06
与 worker 收口写入的投递事件形状，避免把外部飞书/CardKit L4a 误写成已验证。

**Issue #151 起，``WorkerService`` 不再持有任何出站 transport**：收口只写
``task_delivery_event``（``started``/``progress``/``terminal``）并把任务转入
``awaiting_delivery``，不直接调用飞书或释放话题——话题占用与最终业务状态改由
:mod:`lingxi.adapters.postgres_conversation` 的 ``confirm_delivery`` /
``expire_undelivered_terminals`` 收口，见 ``tests/test_delivery_outbox.py`` 的
真库断言（V-投递-01…06/10）。``CardStream``/``CardTaskDelivery`` 曾经把 Worker
接到飞书 CardKit，与「Worker 只写数据库，不直接调用飞书」的架构边界冲突，已随
本次改动从 ``apps/worker`` 移除；``CardStream`` 本身留在
``core/execution/card_stream.py`` 作为协议无关的可复用组件，供 #152 的 Gateway
消费者注入真实 transport 时使用，下面的 ``CardStreamTests`` 继续直接覆盖它。
"""

from __future__ import annotations

import asyncio
import atexit
import io
import itertools
import json
import os
import signal
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from gateway_fakes import FakeAudit, FakeReactions, FakeReplies, CallLog
from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_conversation import (
    ClaimedTask,
    PostgresGatewayStore,
    PostgresTaskQueue,
    PostgresTaskQueueListener,
    TerminalTask,
    _Transaction,
)
from lingxi.apps.worker.config import WorkerConfig
from lingxi.apps.worker.service import WorkerService
from lingxi.config.content import default_content_catalog
from lingxi.core.innertest_content_capture import CapturedToolCall, ContentCaptureRecord
from lingxi.core.year_grounding_guard import QUERY_METRIC_TOOL_NAME
from lingxi.core.conversation import EventPipeline, InboundMessage
from lingxi.core.execution.card_stream import (
    CARD_AUTO_CLOSE_HANDOFF_SECONDS,
    CardCreated,
    CardRateLimiter,
    CardStream,
    DeliveryRejected,
    KNOWN_QUERY_STEPS,
    PROGRESS_ACTION_COMPOSING,
    PROGRESS_ACTION_PROCESSING,
    PROGRESS_ACTION_QUERYING,
    PROGRESS_ACTION_WORKING,
    decode_progress_action,
    encode_progress_action,
)
from lingxi.core.delivery.ports import PROGRESS_CONTENT_MAX_LENGTH
from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows


DSN = os.environ.get("LINGXI_POSTGRES_DSN")
SKIP_DB = (
    "需要 LINGXI_POSTGRES_DSN 才能运行真库队列断言"
    if not DSN
    else "LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，无法运行真库队列断言"
)
POSTGRES_READY = bool(DSN) and psycopg_available()

# Epic D 闸⑥：_process_task 现在按任务的 user_id 读
# <user_env_root>/<user_id>/.mcp.json，读不到就失败关闭（结构上不存在回退到
# 全进程共用配置的分支，见 apps/worker/service.py）。本文件绝大多数用例走的是
# 固定几个 user_id（in-memory FakeWorkerQueue 用 "usr-1"；真库用例固定用
# "usr-90"/"usr-91"），因此在模块级建一次共用夹具目录，给这几个 user_id 各放
# 一份形状合法的 .mcp.json，而不必逐个用例各自建目录——被 FailClosedByDefault
# 一类的用例覆盖：真正要验证"读不到就失败关闭"的用例会显式传一个不含该用户
# 目录的独立临时目录，见 UserMcpConfigFailClosedTest。
_USER_ENV_ROOT_DIR = tempfile.TemporaryDirectory(prefix="lingxi-worker-user-env-")
atexit.register(_USER_ENV_ROOT_DIR.cleanup)


def _seed_user_mcp_config(user_id: str, *, root: str | None = None) -> None:
    """给 ``user_id`` 放一份形状合法的 ``.mcp.json``（与
    ``adapters/user_environment.py`` 的 ``build_mcp_config`` 同一形状）。"""

    home = Path(root or _USER_ENV_ROOT_DIR.name) / user_id
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


for _fixture_user_id in ("usr-1", "usr-90", "usr-91"):
    _seed_user_mcp_config(_fixture_user_id)


def worker_config(**overrides: object) -> WorkerConfig:
    values: dict[str, object] = {
        "question": "",
        "read_only_tools": ("mcp__q__read",),
        "trace_id": "01J00000000000000000000000",
        "turn_timeout_seconds": 1.0,
        "worker_id": "worker-test",
        "target_worker_version": "stable",
        "heartbeat_interval_seconds": 0.01,
        "poll_interval_seconds": 0.01,
        "user_env_root": _USER_ENV_ROOT_DIR.name,
    }
    values.update(overrides)
    return WorkerConfig(**values)  # type: ignore[arg-type]


class RecordingCards:
    """``error`` 默认 ``DeliveryRejected``（明确失败，独立审核 R-1 白名单）；传入
    ``TimeoutError`` 等其它任何异常类型模拟"结果不明"，见
    ``core.execution.card_stream`` 模块说明。
    """

    def __init__(
        self, *, fail: str | None = None, error: type[BaseException] = DeliveryRejected
    ) -> None:
        self.fail = fail
        self._error = error
        self.calls: list[tuple[str, int | None]] = []
        self.bodies: list[str] = []

    def create(self, **kwargs: object) -> CardCreated:
        self.calls.append(("create", None))
        self.bodies.append(kwargs["card"].body)  # type: ignore[union-attr]
        if self.fail == "create":
            raise self._error("card create")
        return CardCreated(card_id="card-1", message_id="msg-card-1")

    def update(self, *, sequence: int, **kwargs: object) -> None:
        self.calls.append(("update", sequence))
        self.bodies.append(kwargs["card"].body)  # type: ignore[union-attr]
        if self.fail == "update":
            raise self._error("card update")

    def close(self, *, sequence: int, **kwargs: object) -> None:
        self.calls.append(("close", sequence))
        self.bodies.append(kwargs["card"].body)  # type: ignore[union-attr]
        if self.fail == "close":
            raise self._error("card close")


class RecordingText:
    def __init__(self, *, fail: bool = False, error: type[BaseException] = DeliveryRejected) -> None:
        self.texts: list[str] = []
        self.calls: list[dict[str, object]] = []
        self.fail = fail
        self._error = error

    def send_text(
        self,
        *,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
        text: str,
    ) -> str:
        self.calls.append(
            {
                "chat_id": chat_id,
                "thread_id": thread_id,
                "reply_to_message_id": reply_to_message_id,
                "text": text,
            }
        )
        self.texts.append(text)
        if self.fail:
            raise self._error("text fallback")
        return "msg-fallback-1"


class CardStreamTests(unittest.TestCase):
    def test_sequence_is_monotonic_and_topic_updates_are_throttled(self) -> None:
        now = [0.0]
        cards = RecordingCards()
        text = RecordingText()
        stream = CardStream(
            chat_id="chat-a",
            thread_id="topic-a",
            reply_to_message_id="msg-a",
            transport=cards,
            fallback=text,
            monotonic=lambda: now[0],
            rate_limiter=CardRateLimiter(),
        )

        stream.start()
        stream.update(elapsed_seconds=0)
        now[0] = 0.5
        stream.update(elapsed_seconds=1)
        stream.finish(result="结果", elapsed_seconds=1)

        self.assertEqual(
            [sequence for kind, sequence in cards.calls if kind in {"update", "close"}],
            [1, 2, 3],
            "卡片更新与关闭必须使用严格递增序号；同话题 500ms 内的中间帧要被抑制",
        )
        self.assertIn("已完成 · 1 秒", cards.bodies[-2])
        self.assertEqual(text.texts, [])

    def test_querying_and_composing_actions_render_without_leaking_any_tool_identity(
        self,
    ) -> None:
        """Issue #321 方向 C ⑤：语义化进度文案只暴露"第几次查询"这个序数与用时，
        不回显工具名、参数或任何查询内容——``update()`` 的调用方（Gateway）只传
        得进 ``query_count: int``，从数据类型上就不可能把一个工具名字符串传成
        这个参数；这里核对渲染结果确实不含 ``mcp__``/``query__`` 字样。

        Issue #407 方向 B：卡片正文现在是"已走过的步骤名"追加式列表，第二次
        更新的正文必须仍然包含第一次那一行（不是被替换掉），断言同步覆盖这一点。
        """

        now = [0.0]
        cards = RecordingCards()
        text = RecordingText()
        stream = CardStream(
            chat_id="chat-a",
            thread_id="topic-a",
            reply_to_message_id="msg-a",
            transport=cards,
            fallback=text,
            monotonic=lambda: now[0],
            rate_limiter=CardRateLimiter(),
        )

        stream.start()
        now[0] = 1.0
        stream.update(elapsed_seconds=5, action=PROGRESS_ACTION_QUERYING, query_count=3)
        now[0] = 2.0
        stream.update(elapsed_seconds=9, action=PROGRESS_ACTION_COMPOSING)

        self.assertEqual(cards.bodies[-2], "正在第 3 次查询指标数据 · 5 秒")
        self.assertEqual(
            cards.bodies[-1],
            "正在第 3 次查询指标数据 · 5 秒\n正在整理与生成回答 · 9 秒",
            "第二次更新必须追加新行，不能把第一行的历史挤掉",
        )
        for body in cards.bodies:
            self.assertNotIn("mcp__", body)
            self.assertNotIn("query__", body)

    def test_working_action_renders_its_own_distinct_text(self) -> None:
        """Issue #407：其它工具调用（working）现在有独立于 composing 的文案。"""

        now = [0.0]
        cards = RecordingCards()
        text = RecordingText()
        stream = CardStream(
            chat_id="chat-a",
            thread_id="topic-a",
            reply_to_message_id="msg-a",
            transport=cards,
            fallback=text,
            monotonic=lambda: now[0],
            rate_limiter=CardRateLimiter(),
        )
        stream.start()
        now[0] = 1.0
        stream.update(elapsed_seconds=4, action=PROGRESS_ACTION_WORKING)

        self.assertEqual(cards.bodies[-1], "正在处理其它步骤 · 4 秒")

    def test_each_known_query_step_renders_its_own_mapped_text(self) -> None:
        """Issue #407 方向 A：四个已知问数查询子步骤各自选到不同的文案，覆盖
        「工具名→用户语文案」白名单式映射表的正例分支。Issue #407 方向 B：
        每次更新都追加一行，最终正文是四行都在的累积列表，不是只剩最新一行。
        """

        now = [0.0]
        cards = RecordingCards()
        text = RecordingText()
        stream = CardStream(
            chat_id="chat-a",
            thread_id="topic-a",
            reply_to_message_id="msg-a",
            transport=cards,
            fallback=text,
            monotonic=lambda: now[0],
            rate_limiter=CardRateLimiter(),
        )
        stream.start()
        expected_lines = [
            "正在第 1 次查询可用指标列表 · 1 秒",
            "正在第 2 次查询指标说明 · 1 秒",
            "正在第 3 次查询维度信息 · 1 秒",
            "正在第 4 次查询指标数据 · 1 秒",
        ]
        steps = ["list_metrics", "describe_metric", "search_dimension", "query_metric"]
        for index, step in enumerate(steps, start=1):
            now[0] = float(index)
            stream.update(
                elapsed_seconds=1, action=PROGRESS_ACTION_QUERYING, query_count=index, query_step=step
            )
            with self.subTest(step=step):
                self.assertEqual(cards.bodies[-1], "\n".join(expected_lines[:index]))
        # 全部各不相同——四种子步骤确实映射到四句不同的文案，不是巧合地都落回
        # 同一句通用文案。
        self.assertEqual(len(set(expected_lines)), 4)

    def test_the_longest_known_query_step_encoding_stays_within_the_32_byte_contract(
        self,
    ) -> None:
        """P2 顺手（独立审查）：`card_stream.py` 顶部已经把这条不变量钉成一条
        import 期 `assert`（`_WORST_CASE_QUERYING_CONTENT_LENGTH <= 32`），这里
        用真实调用 `encode_progress_action` 的方式再验证一遍——白名单里最长的
        子步骤名（``search_dimension``，16 字节）配两位数计数，编码出的
        ``content`` 必须不超过迁移 0075 CHECK 与
        `core.delivery.ports.PROGRESS_CONTENT_MAX_LENGTH` 约定的 32 字节契约。
        Issue #328 opus 审查 R1 的真实事故正是"编码形状撞上数据库层长度 CHECK、
        写库失败只记日志、卡片静默不动"——这里把它钉成可执行断言，不只是注释。
        """

        longest_step = max(KNOWN_QUERY_STEPS, key=len)
        encoded = encode_progress_action(
            PROGRESS_ACTION_QUERYING, query_count=99, query_step=longest_step
        )

        self.assertLessEqual(len(encoded), PROGRESS_CONTENT_MAX_LENGTH)
        self.assertEqual(encoded, f"querying:99:{longest_step}")

    def test_an_unmapped_query_step_falls_back_to_the_generic_text_without_leaking_it(
        self,
    ) -> None:
        """否定用例（Issue #407 出口安全红线）：即使有人绕过 worker 侧的
        `encode_progress_action` 白名单、直接把一个未登记的 `query_step` 字符串
        传给 `CardStream.update()`（模拟"上游过滤被绕过"这一更坏的场景），卡片
        渲染层本身也必须再挡一次——不得把这个字符串拼进渲染文案。
        """

        now = [0.0]
        cards = RecordingCards()
        text = RecordingText()
        stream = CardStream(
            chat_id="chat-a",
            thread_id="topic-a",
            reply_to_message_id="msg-a",
            transport=cards,
            fallback=text,
            monotonic=lambda: now[0],
            rate_limiter=CardRateLimiter(),
        )
        stream.start()
        now[0] = 1.0
        stream.update(
            elapsed_seconds=7,
            action=PROGRESS_ACTION_QUERYING,
            query_count=1,
            query_step="mcp__query__internal_admin_delete_all",
        )

        self.assertEqual(cards.bodies[-1], "正在第 1 次查询指标数据 · 7 秒")
        for body in cards.bodies:
            self.assertNotIn("internal_admin_delete_all", body)
            self.assertNotIn("mcp__", body)

    def test_approaching_the_auto_close_threshold_hands_off_to_text_before_the_platform_would(
        self,
    ) -> None:
        """Issue #407：G-CARD 10 分钟自动关闭的提前收口。用时跨过阈值后，这一帧
        不再是常规进度文案，而是明确的"即将改用文字消息"提示，并立即降级——
        之后的 `update`/`finish` 都必须直接走文本兜底，不再尝试更新卡片。
        """

        now = [0.0]
        cards = RecordingCards()
        text = RecordingText()
        stream = CardStream(
            chat_id="chat-a",
            thread_id="topic-a",
            reply_to_message_id="msg-a",
            transport=cards,
            fallback=text,
            monotonic=lambda: now[0],
            rate_limiter=CardRateLimiter(),
        )
        stream.start()
        now[0] = 100.0
        stream.update(elapsed_seconds=100, action=PROGRESS_ACTION_QUERYING, query_count=1)
        self.assertFalse(stream.fallback_needed)

        now[0] = CARD_AUTO_CLOSE_HANDOFF_SECONDS
        stream.update(
            elapsed_seconds=int(CARD_AUTO_CLOSE_HANDOFF_SECONDS),
            action=PROGRESS_ACTION_QUERYING,
            query_count=2,
        )

        self.assertTrue(stream.fallback_needed, "跨过阈值后必须立即降级")
        self.assertEqual(
            cards.bodies[-1],
            "本次处理时间较长，此卡片可能很快不再更新；处理完成后结果将以新消息发送，请留意。",
        )
        update_calls_after_handoff = len(cards.calls)

        # 降级之后：后续 progress 更新必须直接跳过卡片，不产生新的 update 调用。
        now[0] = CARD_AUTO_CLOSE_HANDOFF_SECONDS + 30
        stream.update(
            elapsed_seconds=int(CARD_AUTO_CLOSE_HANDOFF_SECONDS + 30),
            action=PROGRESS_ACTION_COMPOSING,
        )
        self.assertEqual(len(cards.calls), update_calls_after_handoff, "降级后不应再调用卡片 update")

        # 终态改走既有文本兜底通道，不再尝试卡片 finish。
        stream.finish(result="最终答案", elapsed_seconds=600)
        self.assertEqual(len(cards.calls), update_calls_after_handoff, "降级后 finish 不应触碰卡片")

    def test_the_auto_close_handoff_notice_fires_at_most_once_per_stream(self) -> None:
        """两次调用都跨过阈值——只有第一次真正触发提示并降级；第二次必须被
        ``update()`` 顶部既有的 ``_fallback_needed`` 早退分支挡下，不再产生第
        二次卡片 ``update`` 调用。

        变异存活证据：把 ``_emit_handoff_notice`` 的 ``finally`` 块里
        ``self._fallback_needed = True`` 这一行删掉，本用例的"只有一次 update"
        断言会变红（会变成 2）。
        """

        now = [0.0]
        cards = RecordingCards()
        text = RecordingText()
        stream = CardStream(
            chat_id="chat-a",
            thread_id="topic-a",
            reply_to_message_id="msg-a",
            transport=cards,
            fallback=text,
            monotonic=lambda: now[0],
            rate_limiter=CardRateLimiter(),
        )
        stream.start()
        for offset in (0, 30):
            now[0] = CARD_AUTO_CLOSE_HANDOFF_SECONDS + offset
            stream.update(
                elapsed_seconds=int(CARD_AUTO_CLOSE_HANDOFF_SECONDS + offset),
                action=PROGRESS_ACTION_QUERYING,
                query_count=1,
            )
        update_calls = sum(1 for kind, _ in cards.calls if kind == "update")
        self.assertEqual(update_calls, 1, "第二次跨阈值调用必须被既有降级状态挡下，不重复发提示")

    def test_finish_counts_toward_the_shared_global_rate_budget(self) -> None:
        """独立审核 P2-2：``finish()`` 刻意不经过单话题 500ms 节流（终态帧是结果
        本身，被吞掉比不节流更糟），但全进程 50 次/秒的预算必须同样计入终态更新
        +关闭这两次调用，否则并发多话题同时终态时全局计数会失真。
        """

        limiter = CardRateLimiter()
        cards = RecordingCards()
        text = RecordingText()
        stream = CardStream(
            chat_id="chat-a",
            thread_id="topic-a",
            reply_to_message_id="msg-a",
            transport=cards,
            fallback=text,
            monotonic=lambda: 0.0,
            rate_limiter=limiter,
        )
        stream.start()  # 消费全局预算第 1 个名额
        for index in range(48):
            self.assertTrue(limiter.allow(topic=f"filler-{index}", now=0.0))
        # 至此全局预算已经用掉 49/50，只剩 1 个名额。

        stream.finish(result="结果", elapsed_seconds=1)  # 终态更新 + 关闭，各占一个名额

        self.assertFalse(
            limiter.allow(topic="topic-brand-new", now=0.0),
            "finish() 的两次调用必须计入全局 50 次/秒预算，否则这里会被误放行",
        )

    def test_card_failure_falls_back_to_same_topic_text(self) -> None:
        cards = RecordingCards(fail="update")
        text = RecordingText()
        stream = CardStream(
            chat_id="chat-a",
            thread_id="topic-a",
            reply_to_message_id="msg-a",
            transport=cards,
            fallback=text,
        )
        stream.start()
        stream.finish(failure=default_content_catalog().text("worker.failed"))
        stream.send_fallback(default_content_catalog().text("worker.failed"))

        self.assertTrue(stream.fallback_needed)
        self.assertEqual(text.texts, ["本次任务未取得可用结果，请稍后重试。"])
        self.assertEqual(
            text.calls[0]["chat_id"],
            "chat-a",
            "V-卡片-03：文本回退必须保留原 chat_id",
        )
        self.assertEqual(
            text.calls[0]["thread_id"],
            "topic-a",
            "V-卡片-03：文本回退必须保留原 thread_id",
        )

    def test_close_failure_alone_does_not_fall_back_to_a_duplicate_text(self) -> None:
        """独立审核 P1-2：终态**更新**已经成功（用户已经能在卡片里看到完整答案），
        只有随后的**关闭**失败——不得整体降级为文本兜底，否则用户会在同一话题里
        看到同一条答案两遍。G-CARD 实测：未手动关闭的流式卡片距上次开启 10 分钟
        后由平台自动关闭，关闭失败本身不构成结果丢失。
        """

        cards = RecordingCards(fail="close")
        text = RecordingText()
        stream = CardStream(
            chat_id="chat-a",
            thread_id="topic-a",
            reply_to_message_id="msg-a",
            transport=cards,
            fallback=text,
        )
        stream.start()
        stream.finish(result="已产生的答案", elapsed_seconds=1)

        self.assertFalse(stream.fallback_needed, "只有关闭失败，不能整体降级为文本通道")
        self.assertEqual(
            [kind for kind, _ in cards.calls],
            ["create", "update", "close"],
            "终态更新必须真实发出；关闭也确实被尝试过（只是失败了）",
        )

        message_id = stream.send_fallback(default_content_catalog().text("worker.failed"))
        self.assertIsNone(message_id, "fallback_needed 为假时 send_fallback 不产生任何外部调用")
        self.assertEqual(text.calls, [], "答案已经在卡片里对用户可见，绝不能再发一条重复文本终态")

    def test_create_timeout_is_not_swallowed_into_a_fallback_downgrade(self) -> None:
        """独立审核 B-1/R-1：``TimeoutError``（真实 adapter 走 ``requests``，其网络
        异常全部是内置 ``OSError`` 的子类）不是"明确失败"——``start()`` 必须原样把
        它抛出去，不能像 ``DeliveryRejected`` 那样吞掉并置位 ``fallback_needed``
        （那会让调用方误以为已经拿到"应该改走文本通道"这个明确结论）。
        """

        cards = RecordingCards(fail="create", error=TimeoutError)
        text = RecordingText()
        stream = CardStream(
            chat_id="chat-a", thread_id="topic-a", reply_to_message_id="msg-a",
            transport=cards, fallback=text,
        )

        with self.assertRaises(TimeoutError):
            stream.start()

        self.assertIsNone(stream.card_id, "没有拿到 card_id，不能假设建卡成功")
        self.assertFalse(stream.fallback_needed, "结果不明绝不能置位 fallback_needed")

    def test_terminal_update_timeout_is_not_swallowed_into_a_fallback_downgrade(self) -> None:
        """独立审核 B-1 场景 1：终态更新超时不得被 ``finish()`` 吞掉后降级为文本
        兜底——必须原样抛出，且不再继续调用 ``close()``。
        """

        cards = RecordingCards(fail="update", error=TimeoutError)
        text = RecordingText()
        stream = CardStream(
            chat_id="chat-a", thread_id="topic-a", reply_to_message_id="msg-a",
            transport=cards, fallback=text,
        )
        stream.start()

        with self.assertRaises(TimeoutError):
            stream.finish(result="已产生的答案", elapsed_seconds=1)

        self.assertFalse(stream.fallback_needed, "结果不明绝不能置位 fallback_needed")
        self.assertEqual(
            [kind for kind, _ in cards.calls], ["create", "update"], "结果不明时不得继续调用 close"
        )

    def test_close_timeout_still_does_not_fall_back_like_any_other_close_failure(self) -> None:
        """``close()`` 步骤的异常分类不延伸到这里（见 ``card_stream.py`` 注释）：
        无论关闭失败是明确拒绝还是网络类异常，都不改变"更新已经成功、答案已对
        用户可见"这个结论，``TimeoutError`` 与 ``DeliveryRejected`` 在这一步行为
        一致。
        """

        cards = RecordingCards(fail="close", error=TimeoutError)
        text = RecordingText()
        stream = CardStream(
            chat_id="chat-a", thread_id="topic-a", reply_to_message_id="msg-a",
            transport=cards, fallback=text,
        )
        stream.start()
        stream.finish(result="已产生的答案", elapsed_seconds=1)  # 不应该抛出

        self.assertFalse(stream.fallback_needed, "关闭失败（无论何种异常）都不整体降级")
        self.assertEqual([kind for kind, _ in cards.calls], ["create", "update", "close"])

    def test_text_fallback_timeout_is_not_swallowed(self) -> None:
        """独立审核 B-1 场景 2：文本兜底发送超时必须原样抛出，调用方据此不清预留位、
        不进入重试退避。
        """

        cards = RecordingCards(fail="create")  # 明确失败，走文本通道
        text = RecordingText(fail=True, error=TimeoutError)
        stream = CardStream(
            chat_id="chat-a", thread_id="topic-a", reply_to_message_id="msg-a",
            transport=cards, fallback=text,
        )
        stream.start()
        self.assertTrue(stream.fallback_needed)

        with self.assertRaises(TimeoutError):
            stream.send_fallback(default_content_catalog().text("worker.failed"))

        self.assertIsNone(stream.message_id, "没有拿到 message_id，不能假设文本已经送达")
        self.assertEqual(len(text.calls), 1, "发送确实被尝试过一次")

    def test_resuming_with_an_existing_card_id_does_not_create_a_second_card(self) -> None:
        """Issue #152：Gateway 消费循环重启后用 ``initial_*`` 恢复，``start()``
        必须是安全的空操作，不产生第二次 ``create()`` 调用（状态合同第 7 条）。"""

        cards = RecordingCards()
        text = RecordingText()
        stream = CardStream(
            chat_id="chat-a",
            thread_id="topic-a",
            reply_to_message_id="msg-a",
            transport=cards,
            fallback=text,
            initial_card_id="card-resumed",
            initial_sequence=2,
            initial_message_id="msg-resumed",
        )

        stream.start()
        self.assertEqual(cards.calls, [], "resume 场景下 start() 必须是空操作")
        self.assertEqual(stream.card_id, "card-resumed")
        self.assertEqual(stream.message_id, "msg-resumed")

        stream.finish(result="结果", elapsed_seconds=1)
        self.assertEqual(
            [sequence for kind, sequence in cards.calls if kind in {"update", "close"}],
            [3, 4],
            "序号从持久化的 initial_sequence 之后继续，不从零重新计数",
        )

    def test_resuming_with_fallback_already_needed_skips_the_card_path_entirely(self) -> None:
        """已经降级为文本通道的任务重启后不得再尝试建卡或更新卡片。"""

        cards = RecordingCards()
        text = RecordingText()
        stream = CardStream(
            chat_id="chat-a",
            thread_id="topic-a",
            reply_to_message_id="msg-a",
            transport=cards,
            fallback=text,
            initial_fallback_needed=True,
        )

        stream.start()
        stream.update(elapsed_seconds=1)
        stream.finish(result="结果")
        self.assertEqual(cards.calls, [], "已降级的任务重启后不得再触碰卡片通道")

        message_id = stream.send_fallback(default_content_catalog().text("worker.failed"))
        self.assertEqual(message_id, "msg-fallback-1")
        self.assertEqual(stream.message_id, "msg-fallback-1")


class FakeWorkerQueue:
    def __init__(self, *, stopped: bool = False) -> None:
        self.claimed = ClaimedTask(
            task_id="tsk-1",
            conversation_id="cnv-1",
            user_id="usr-1",
            prompt="问题",
            resumed_session=True,
            target_worker_version="stable",
            attempts=1,
            reply_to_message_id="msg-1",
            stop_requested=stopped,
        )
        from lingxi.adapters.postgres_conversation import TaskContext

        self.context = TaskContext(
            task_id="tsk-1",
            conversation_id="cnv-1",
            user_id="usr-1",
            prompt="问题",
            resumed_session=True,
            target_worker_version="stable",
            attempts=1,
            reply_to_message_id="msg-1",
            chat_id="chat-1",
            thread_id="topic-1",
            agent_session_id="old-session",
            stop_requested=stopped,
            side_effect_state="none",
        )
        self.events: list[dict[str, object]] = []
        self.terminals: list[dict[str, object]] = []
        self.marked = 0

    def claim(self, **kwargs: object) -> list[ClaimedTask]:
        if self.claimed is None:  # type: ignore[comparison-overlap]
            return []
        claimed, self.claimed = self.claimed, None  # type: ignore[assignment]
        return [claimed]

    def task_context(self, **kwargs: object):
        return self.context

    def mark_side_effect(self, **kwargs: object) -> bool:
        self.marked += 1
        return True

    def heartbeat(self, **kwargs: object) -> bool:
        return True

    def stop_requested(self, **kwargs: object) -> bool:
        return self.context.stop_requested

    def append_delivery_event(self, **kwargs: object) -> None:
        self.events.append(kwargs)
        return None

    def write_terminal_event(self, **kwargs: object) -> None:
        self.terminals.append(kwargs)
        return None


class LateStopWorkerQueue(FakeWorkerQueue):
    """开工时没有 stop、执行途中队列侧才被置上停止标志的队列替身（Issue #195）。

    ``claimed``/``context`` 上的 ``stop_requested`` 保持 ``False``——否则任务在
    ``_process_task`` 开头就直接收口成 ``stopped``，根本走不到终态选择那一段。
    只有 ``stop_requested()`` 这个**轮询出口**返回 ``True``，它正是 ``_monitor``
    读的那一个，也是旧收口逻辑额外回读、并据此改写终态的那一个。
    """

    def stop_requested(self, **kwargs: object) -> bool:
        return True


class DroppingNotifyListener:
    """真库消费者测试用的丢通知监听器：等待轮询上限后仍返回未唤醒。"""

    def __init__(self) -> None:
        self.wait_started = threading.Event()
        self.wait_calls: list[float] = []

    def __enter__(self) -> "DroppingNotifyListener":
        return self

    def wait(self, *, timeout_seconds: float) -> bool:
        self.wait_calls.append(timeout_seconds)
        self.wait_started.set()
        time.sleep(timeout_seconds)
        return False

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        return None


class RecordingTerminalOutcomeSink:
    """``WorkerService(on_terminal_outcome=...)`` 的测试替身（Issue #90 评论
    5306860255 独立复核 P1）：真实装配（``apps/worker/cli.py``）接的是结构化
    stderr 出口，不是 stdlib ``logging``——单测因此不能再用 ``assertLogs``
    间接验证「代码里调用了 logging」，那只能证明调用存在，证明不了运维在真实
    队列 worker 进程里真的能看到它（真实进程从不调用 ``logging.basicConfig()``，
    见 ``cli.py`` 的 ``_LogOnlyAlertSender`` 说明）。这里直接断言注入回调收到的
    字段，与生产装配走同一条注入协议。"""

    def __init__(self) -> None:
        self.calls: list[Mapping[str, object]] = []

    def __call__(self, fields: Mapping[str, object]) -> None:
        self.calls.append(dict(fields))


class WorkerServiceTests(unittest.TestCase):
    """Issue #151：``_process_task`` 只写 ``task_delivery_event``，不再调用任何出站
    transport；断言因此改看 ``queue.events``/``queue.terminals`` 记录了什么，而不是
    ``delivery`` 对象收到了什么调用。"""

    def test_success_writes_started_and_terminal_events(self) -> None:
        queue = FakeWorkerQueue()

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                self.kwargs = kwargs
                kwargs["on_stream_event"]({"kind": "assistant_message"})  # type: ignore[index]
                return {
                    "turn": {"closed": True, "final_text": "结果", "session_id": "new-session"},
                    "failure": None,
                }

        executor = Executor()
        # Issue #321 方向 C：progress 更新之间要求 >=5 秒的节流窗口（防频控），
        # 这里用一个每次调用都前进 6 秒的假单调时钟，确保这次 assistant_message
        # 触发的更新不会被节流吞掉——本用例只关心"确实产生了一条 progress 事件"，
        # 语义化文案本身由下面新增的 SemanticProgressTests 覆盖。
        service = WorkerService(
            config=worker_config(
                external_texts=(("metric.description", "指标目录中的已知描述"),),
            ),
            queue=queue,
            executor_factory=lambda config, marker: executor,
            monotonic=itertools.count(0.0, 6.0).__next__,
        )
        asyncio.run(service.process_once())

        self.assertEqual(queue.events[0]["event_type"], "started")
        self.assertEqual(queue.events[1]["event_type"], "progress")
        terminal = queue.terminals[0]
        self.assertEqual(terminal["terminal_kind"], "success")
        self.assertIsNone(terminal["error_kind"])
        self.assertEqual(terminal["content"], "结果")
        self.assertEqual(terminal["agent_session_id"], "new-session")
        self.assertIsNotNone(executor.kwargs["resume_session_id"])
        self.assertEqual(
            executor.kwargs["external_texts"],
            (("metric.description", "指标目录中的已知描述"),),
        )

    def test_the_executor_receives_this_users_own_mcp_config_not_the_shared_one(self) -> None:
        """Epic D 闸⑥：每个用户的问数必须用他自己的那份 MCP 配置——即使全进程
        共用的那份配置（``self._config.mcp_servers``）也配了值，executor 收到
        的必须是从这个用户自己的 ``.mcp.json`` 读出来的那一份，两者不能相等。
        """

        queue = FakeWorkerQueue()  # user_id="usr-1"，夹具见模块顶部 _seed_user_mcp_config
        received: list[Mapping[str, object]] = []

        class Executor:
            def __init__(self, config: WorkerConfig) -> None:
                received.append(config.mcp_servers)

            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {"closed": True, "final_text": "结果", "session_id": None},
                    "failure": None,
                }

        shared_sentinel = {"shared": {"type": "stdio", "command": "should-never-be-used"}}
        service = WorkerService(
            config=worker_config(mcp_servers=shared_sentinel),
            queue=queue,
            executor_factory=lambda config, marker: Executor(config),
        )
        asyncio.run(service.process_once())

        self.assertEqual(len(received), 1)
        self.assertEqual(
            received[0],
            {
                "query": {
                    "type": "http",
                    "url": "https://example.invalid/mcp",
                    "headers": {"Authorization": "Bearer test-token"},
                }
            },
        )
        self.assertNotEqual(received[0], shared_sentinel)

    def test_missing_user_mcp_config_fails_closed_without_ever_constructing_an_executor(
        self,
    ) -> None:
        """Epic D 闸⑥红线：读不到用户自己的 MCP 配置必须失败关闭，且**结构上
        不存在**回退到全进程共用配置的路径——executor 从未被构造、run_turn
        从未被调用，terminal 必须是失败而不是成功。

        变异存活证据：把 ``_process_task`` 里 ``except UserMcpConfigError`` 的
        处理改成"读不到就退回 self._config"（例如
        ``task_config = self._config`` 后继续往下走，而不是直接构造失败
        report），本用例会变红——``executor_calls`` 会从 0 变成 1，且
        ``terminal_kind`` 会从 ``"failed"`` 变成 ``"success"``、正文变成
        "不该被用到的结果"。
        """

        queue = FakeWorkerQueue()  # user_id="usr-1"
        with tempfile.TemporaryDirectory() as empty_root:
            # 这个目录里没有任何 usr-1 的 .mcp.json——刻意不复用模块级共享夹具。
            executor_calls: list[None] = []

            class Executor:
                def __init__(self) -> None:
                    executor_calls.append(None)

                async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                    return {
                        "turn": {
                            "closed": True,
                            "final_text": "不该被用到的结果",
                            "session_id": None,
                        },
                        "failure": None,
                    }

            sink = RecordingTerminalOutcomeSink()
            service = WorkerService(
                config=worker_config(user_env_root=empty_root),
                queue=queue,
                executor_factory=lambda config, marker: Executor(),
                on_terminal_outcome=sink,
            )
            asyncio.run(service.process_once())

        self.assertEqual(executor_calls, [], "读不到用户自己的配置时绝不能构造 executor")
        terminal = queue.terminals[0]
        self.assertEqual(terminal["terminal_kind"], "failed")
        self.assertEqual(terminal["error_kind"], "session_failed")
        self.assertNotIn("不该被用到的结果", terminal["content"])
        self.assertEqual(len(sink.calls), 1)
        self.assertEqual(sink.calls[0]["failure_code"], "user_mcp_config_unavailable")

    def test_a_root_that_is_configured_but_unset_on_worker_config_also_fails_closed(self) -> None:
        """``user_env_root=None``（例如漏配 ``LINGXI_USER_ENV_ROOT``）必须同样
        失败关闭，不是被当成"没有约束、什么都能用"。"""

        queue = FakeWorkerQueue()
        executor_calls: list[None] = []

        class Executor:
            def __init__(self) -> None:
                executor_calls.append(None)

            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                raise AssertionError("不该被调用")

        service = WorkerService(
            config=worker_config(user_env_root=None),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
        )
        asyncio.run(service.process_once())

        self.assertEqual(executor_calls, [])
        self.assertEqual(queue.terminals[0]["terminal_kind"], "failed")

    def test_stop_and_timeout_write_distinct_terminal_kinds(self) -> None:
        # Issue #90 评论 5306860255：turn 模式（apps/worker/turn.py 的
        # `_sdk_termination_failure`）早就把撞满 Agent 轮数上限分类成
        # `max_turns_exceeded`，但 queue 收口此前落进 `_failure_content` 的默认
        # 分支，被压平成通用 `session_failed` + 「本次任务未取得可用结果，请稍后
        # 重试」——用户看不出重试无意义。这里补一行覆盖 queue 链路的专属终态。
        for stopped, failure_code, expected_terminal_kind, expected_error, expected_content_key in (
            (True, None, "stopped", "stopped", "worker.stopped"),
            (False, "turn_timeout", "timeout", "running_timeout", "worker.running_timeout"),
            (False, "max_turns_exceeded", "failed", "max_turns_exceeded", "worker.max_turns"),
            # 2026-08-23 真实故障：回执超过 SDK 读流缓冲上限（result_too_large，
            # 分类在 apps/worker/turn.py）同样不得压平成「请稍后重试」。
            (False, "result_too_large", "failed", "result_too_large", "worker.result_too_large"),
        ):
            with self.subTest(expected_terminal_kind=expected_terminal_kind, failure_code=failure_code):
                queue = FakeWorkerQueue(stopped=stopped)

                class Executor:
                    async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                        return {
                            "turn": {"closed": False, "final_text": ""},
                            "failure": {"code": failure_code} if failure_code else None,
                        }

                service = WorkerService(
                    config=worker_config(),
                    queue=queue,
                    executor_factory=lambda config, marker: Executor(),
                )
                asyncio.run(service.process_once())
                terminal = queue.terminals[0]
                self.assertEqual(terminal["terminal_kind"], expected_terminal_kind)
                self.assertEqual(terminal["error_kind"], expected_error)
                self.assertEqual(
                    terminal["content"], default_content_catalog().text(expected_content_key).text
                )

    def test_withheld_output_writes_redacted_withheld_terminal_not_success(self) -> None:
        """#141/#149：整段正文因安全策略被拒发时，即使 closed=True 也不得写成
        ``terminal_kind='success'``——用户没有拿到结果，必须走独立、可查询的
        ``redacted_withheld`` 终态。改坏这条路由（例如去掉 withheld 判断）必须让
        本用例变红。"""

        queue = FakeWorkerQueue()

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {
                        "closed": True,
                        "final_text": "本次结果涉及需要保护的内容，已被安全策略拦截，未能提供结果。",
                        "session_id": "new-session",
                        "output_safety": {"blocked": True, "withheld": True, "reasons": ("forbidden_value",)},
                        "user_result": "redacted_withheld",
                    },
                    "failure": None,
                }

        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
        )
        asyncio.run(service.process_once())

        terminal = queue.terminals[0]
        self.assertEqual(terminal["terminal_kind"], "redacted_withheld")
        self.assertEqual(terminal["error_kind"], "redacted_withheld")
        self.assertNotEqual(terminal["terminal_kind"], "success")
        # withheld 的原始 final_text 不得进入投递事件：正文只能是目录里的固定安全
        # 文案，不是模型给出的（已经被判定不可展示的）片段。
        self.assertEqual(
            terminal["content"], default_content_catalog().text("worker.redacted_withheld").text
        )

    def test_a_failed_turn_keeps_its_failure_terminal_even_when_withheld_is_set(self) -> None:
        """PR #186 独立审核 F1：withheld 只对"本来会成功交付内容"的回合有意义。
        超时/失败回合的残余正文即使触发了出口安全（真实泄露片段或受控 canary
        注入），终态也必须保留真实失败原因——把真超时写成 ``redacted_withheld``，
        运维丢失失败终态，验收拿到假阳性安全证据。把 withheld 分支挪回失败判定
        之前必须让本用例变红。"""

        queue = FakeWorkerQueue()

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {
                        "closed": False,
                        "final_text": "残余正文",
                        "session_id": "new-session",
                        "output_safety": {
                            "blocked": True,
                            "withheld": True,
                            "reasons": ("forbidden_value",),
                        },
                        "user_result": "redacted_withheld",
                    },
                    "failure": {"code": "turn_timeout", "message": "任务提前结束：达到墙钟上限"},
                }

        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
        )
        asyncio.run(service.process_once())

        terminal = queue.terminals[0]
        self.assertEqual(terminal["terminal_kind"], "timeout", "真实失败终态不得被 withheld 覆盖")
        self.assertNotEqual(terminal["terminal_kind"], "redacted_withheld")

    def test_a_closed_turn_whose_body_leaks_an_internal_tool_name_is_not_success(self) -> None:
        """P0 护栏（Issue #291 L6 取证结论，2026-08-22）：真实事故形状——模型
        （qwen3.7-plus）把工具调用非确定性地写成了正文散文，回合仍然
        ``closed=True``、没有 ``failure``，出口净化层正确遮蔽了内部工具名与过程
        标记（``blocked=True``，``reasons`` 同时命中 ``internal_tool_name`` 与
        ``process_marker``——真实取证记录的原始形状），但 ``withheld`` 没有置位
        （遮蔽后仍有"幸存"的业务内容——遮蔽后的 JSON 骨架），旧实现据此判定
        ``deliverable`` 并把这段协议残骸当成「查询完成」交付给用户。改坏
        `_protocol_breakdown_reasons` 判定（例如删掉这条检查，或只在
        `withheld=True` 时才生效）必须让本用例变红。"""

        queue = FakeWorkerQueue()
        leaked_text = (
            "好的，我将为你查询。【内部能力已隐藏】【内部标识已隐藏】"
            "{\"metric\": \"日活\", \"value\": 1024}"
        )

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {
                        "closed": True,
                        "final_text": leaked_text,
                        "session_id": "new-session",
                        "output_safety": {
                            "blocked": True,
                            "withheld": False,
                            "reasons": ("internal_tool_name", "process_marker"),
                        },
                        "user_result": "obtained",
                    },
                    "audit": {"tool_result_count": 0, "denied_count": 0, "denied": []},
                    "failure": None,
                }

        sink = RecordingTerminalOutcomeSink()
        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
            on_terminal_outcome=sink,
        )
        asyncio.run(service.process_once())

        terminal = queue.terminals[0]
        self.assertNotEqual(terminal["terminal_kind"], "success", "协议残骸不得被判定为成功交付")
        self.assertEqual(terminal["terminal_kind"], "failed")
        self.assertEqual(terminal["error_kind"], "model_protocol_breakdown")
        self.assertNotIn(leaked_text, terminal["content"], "泄漏正文不得进入投递事件")
        self.assertIsNone(terminal.get("agent_session_id"), "未交付成功时不应持久化新会话")

        self.assertEqual(len(sink.calls), 1)
        fields = sink.calls[0]
        self.assertEqual(fields["failure_code"], "model_protocol_breakdown")
        self.assertEqual(fields["terminal_kind"], "failed")
        self.assertIn("internal_tool_name", fields["output_safety_reasons"])
        self.assertIn("process_marker", fields["output_safety_reasons"])

    def test_a_closed_turn_whose_body_leaks_a_process_marker_is_not_success(self) -> None:
        """同上，覆盖另一个原因码 ``process_marker``（``mcp__``/``tool_use_id``/
        ``trace_id`` 这类过程标记）——两个原因码任一命中都必须触发本护栏，不能
        只认 ``internal_tool_name`` 一个。"""

        queue = FakeWorkerQueue()

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {
                        "closed": True,
                        "final_text": "执行中【内部标识已隐藏】，结果如下：1024",
                        "session_id": "new-session",
                        "output_safety": {
                            "blocked": True,
                            "withheld": False,
                            "reasons": ("process_marker",),
                        },
                        "user_result": "obtained",
                    },
                    "failure": None,
                }

        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
        )
        asyncio.run(service.process_once())

        terminal = queue.terminals[0]
        self.assertEqual(terminal["terminal_kind"], "failed")
        self.assertEqual(terminal["error_kind"], "model_protocol_breakdown")

    def test_a_normal_successful_turn_with_tool_calls_stays_success(self) -> None:
        """反向用例：正常成功回合（有工具调用结果、``output_safety.reasons``
        为空）不得被新护栏误伤。"""

        queue = FakeWorkerQueue()

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {
                        "closed": True,
                        "final_text": "日活是 1024。",
                        "session_id": "new-session",
                        "output_safety": {"blocked": False, "withheld": False, "reasons": ()},
                        "user_result": "obtained",
                    },
                    "audit": {"tool_result_count": 1, "denied_count": 0, "denied": []},
                    "failure": None,
                }

        sink = RecordingTerminalOutcomeSink()
        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
            on_terminal_outcome=sink,
        )
        asyncio.run(service.process_once())

        terminal = queue.terminals[0]
        self.assertEqual(terminal["terminal_kind"], "success")
        self.assertEqual(terminal["content"], "日活是 1024。")
        self.assertEqual(sink.calls[0]["tool_result_count"], 1)

    def test_a_zero_tool_result_chit_chat_turn_stays_success_on_its_own(self) -> None:
        """反向用例：``tool_result_count == 0`` 单独不构成失败——闲聊问题不需要
        调用任何工具，不能被误伤成协议异常。护栏只认 ``output_safety.reasons``，
        与工具调用次数无关。"""

        queue = FakeWorkerQueue()

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {
                        "closed": True,
                        "final_text": "你好，有什么可以帮你？",
                        "session_id": "new-session",
                        "output_safety": {"blocked": False, "withheld": False, "reasons": ()},
                        "user_result": "not_applicable",
                    },
                    "audit": {"tool_result_count": 0, "denied_count": 0, "denied": []},
                    "failure": None,
                }

        sink = RecordingTerminalOutcomeSink()
        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
            on_terminal_outcome=sink,
        )
        asyncio.run(service.process_once())

        terminal = queue.terminals[0]
        self.assertEqual(terminal["terminal_kind"], "success")
        self.assertEqual(sink.calls[0]["tool_result_count"], 0)

    def test_terminal_outcome_callback_reports_the_real_tool_result_count(self) -> None:
        """P1 可观测性（Issue #291 L6 取证结论）：``report["audit"][
        "tool_result_count"]`` 必须随终态收口一起写进 ``on_terminal_outcome``
        回调（生产装配落到 ``worker.task.terminal``）——2026-08-22 那次取证，
        运维定位"这一轮到底有没有真的调用过工具"花了 40 分钟，就是因为这个字段
        此前从未离开进程，只能翻完整条 SDK 事件流去数。"""

        queue = FakeWorkerQueue()

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {
                        "closed": True,
                        "final_text": "查询完成，共 3 条工具调用。",
                        "session_id": "new-session",
                        "output_safety": {"blocked": False, "withheld": False, "reasons": ()},
                    },
                    "audit": {"tool_result_count": 3, "denied_count": 0, "denied": []},
                    "failure": None,
                }

        sink = RecordingTerminalOutcomeSink()
        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
            on_terminal_outcome=sink,
        )
        asyncio.run(service.process_once())

        self.assertEqual(len(sink.calls), 1)
        self.assertEqual(sink.calls[0]["tool_result_count"], 3)

    def test_terminal_outcome_callback_defaults_tool_result_count_to_zero_when_absent(
        self,
    ) -> None:
        """否定测试：``report["audit"]`` 缺失时（早退分支，例如未预期异常）如实
        记 0，不假装有据可查——避免上一条用例的字段被恒真实现（写死为某个非零
        值）蒙混过关。"""

        queue = FakeWorkerQueue()

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {"closed": True, "final_text": "结果", "session_id": "s"},
                    "failure": None,
                }

        sink = RecordingTerminalOutcomeSink()
        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
            on_terminal_outcome=sink,
        )
        asyncio.run(service.process_once())

        self.assertEqual(sink.calls[0]["tool_result_count"], 0)

    # Issue #195 的收口组合用例共用的两种「stop 到达方式」。两种都要覆盖：
    # ``stop_event`` 是 ``_monitor`` 置位的进程内信号（用户 ``/stop`` 与 SIGTERM
    # 共用同一个事件）；``queue_flag`` 是队列侧 ``stop_requested()`` 轮询出口，
    # 旧收口逻辑在写终态前会额外回读它一次并据此改写终态，本次修复把那次回读
    # 一并删除，因此这一路必须有独立守卫，否则回读被加回来不会有任何东西变红。
    _STOP_ARRIVALS = ("stop_event", "queue_flag")

    def _terminal_when_stop_lands_mid_turn(
        self, report: Mapping[str, object], *, stop_arrival: str
    ) -> Mapping[str, object]:
        """跑一次"回合已经产出 ``report``、stop 在同一时刻落地"的收口，返回终态事件。"""

        queue = LateStopWorkerQueue() if stop_arrival == "queue_flag" else FakeWorkerQueue()

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                if stop_arrival == "stop_event":
                    # 真实链路里这一步由 `_monitor` 完成：轮询发现 stop 就置位
                    # 同一个 stop_event。这里直接置位，等价于"stop 与回合终点
                    # 赛跑，且 stop 这一侧赢在了报告已经生成之后"。
                    kwargs["stop_event"].set()  # type: ignore[union-attr]
                return dict(report)

        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
        )
        asyncio.run(service.process_once())
        self.assertEqual(len(queue.terminals), 1, "一个回合只能写一条终态事件")
        return queue.terminals[0]

    def test_a_failed_turn_keeps_its_failure_terminal_even_when_stop_lands_concurrently(
        self,
    ) -> None:
        """Issue #195 场景 1：回合已经因 ``turn_timeout`` 失败，用户 ``/stop``
        或停机信号在回合终点**并发**到达。终态必须保留真实失败原因，残余正文
        不得经 ``worker.stopped_result`` 交付。

        把终态优先级改回 ``if stop_requested or failure_code == "interrupted"``
        必须让本用例变红：终态会变成 ``stopped``，运维丢失超时这个真实失败原因，
        用户还会收到一段本不该交付的残余正文。
        """

        leftover_text = "残余正文：这轮超时前打出来的半截内容"
        for stop_arrival in self._STOP_ARRIVALS:
            with self.subTest(stop_arrival=stop_arrival):
                terminal = self._terminal_when_stop_lands_mid_turn(
                    {
                        "turn": {
                            "closed": False,
                            "final_text": leftover_text,
                            "session_id": None,
                        },
                        "failure": {
                            "code": "turn_timeout",
                            "message": "任务提前结束：达到墙钟上限",
                        },
                    },
                    stop_arrival=stop_arrival,
                )

                self.assertEqual(
                    terminal["terminal_kind"], "timeout", "并发到达的 stop 不得改写真实失败终态"
                )
                self.assertNotEqual(terminal["terminal_kind"], "stopped")
                self.assertEqual(terminal["error_kind"], "running_timeout")
                self.assertEqual(
                    terminal["content"],
                    default_content_catalog().text("worker.running_timeout").text,
                    "失败终态只发失败文案",
                )
                self.assertNotIn(
                    leftover_text, str(terminal["content"]), "失败回合的残余正文不得交付"
                )

    def test_a_succeeded_turn_still_delivers_its_result_and_session_id_when_stop_lands_late(
        self,
    ) -> None:
        """Issue #195 场景 2：回合已经 ``closed=True`` 拿到结果，``/stop`` 或停机
        信号晚到。已产出的结果必须照常交付，且只有成功分支才写的
        ``agent_session_id`` 必须照常持久化——否则用户白等一轮、方案 B 的会话内
        追问也失去依据（「重启与重试不得造成用户结果丢失」红线）。

        把终态优先级改回 ``if stop_requested or failure_code == "interrupted"``
        必须让本用例变红：终态被降级成 ``stopped``，``agent_session_id`` 为空。
        """

        for stop_arrival in self._STOP_ARRIVALS:
            with self.subTest(stop_arrival=stop_arrival):
                terminal = self._terminal_when_stop_lands_mid_turn(
                    {
                        "turn": {
                            "closed": True,
                            "final_text": "结果",
                            "session_id": "new-session",
                        },
                        "failure": None,
                    },
                    stop_arrival=stop_arrival,
                )

                self.assertEqual(
                    terminal["terminal_kind"], "success", "晚到的 stop 不得吃掉已产出的结果"
                )
                self.assertNotEqual(terminal["terminal_kind"], "stopped")
                self.assertIsNone(terminal["error_kind"])
                self.assertEqual(terminal["content"], "结果")
                self.assertEqual(
                    terminal["agent_session_id"],
                    "new-session",
                    "成功回合的 session_id 必须照常持久化",
                )

    def test_an_unclosed_turn_without_a_named_failure_code_is_still_a_failure_under_stop(
        self,
    ) -> None:
        """codex 一级独立审查 P1-1：``failure_code is None`` **不等于**「没有失败
        事实」——``closed=False`` 本身就是失败事实。上一版实现用
        ``failure_code is None`` 反推「那就当作是 stop 造成的」，于是同一份报告
        在有 stop 时收口成 ``stopped``（还可能经 ``worker.stopped_result`` 把残余
        正文交付出去），没有 stop 时却是 ``failed``/``session_failed`` 且不交付
        正文——同一个事实两种终态，其中一种在说谎。

        三种「没有可用失败码」的真实形状都必须与无 stop 时一致地收口成失败：
        报告干脆没有 failure、failure 非空但缺 ``code``、以及屏障失效
        （``gate_bypassed``——``Stop`` hook 没触发，是安全屏障失效唯一的可观察
        形状，绝不能被写成"用户停止了任务"）。
        """

        leftover_text = "残余正文：不该被当成「已停止」交付出去的半截内容"
        for label, turn_extra, failure in (
            ("没有 failure", {}, None),
            ("failure 非空但缺 code", {}, {"message": "某个没有归类的失败"}),
            ("屏障失效 gate_bypassed", {"gate_bypassed": True}, None),
        ):
            for stop_arrival in self._STOP_ARRIVALS:
                with self.subTest(shape=label, stop_arrival=stop_arrival):
                    terminal = self._terminal_when_stop_lands_mid_turn(
                        {
                            "turn": {
                                "closed": False,
                                "final_text": leftover_text,
                                "session_id": None,
                                **turn_extra,
                            },
                            "failure": failure,
                        },
                        stop_arrival=stop_arrival,
                    )

                    self.assertEqual(
                        terminal["terminal_kind"],
                        "failed",
                        "未收口的回合不因为「没有失败码」就变成用户停止",
                    )
                    self.assertNotEqual(terminal["terminal_kind"], "stopped")
                    self.assertEqual(terminal["error_kind"], "session_failed")
                    self.assertEqual(
                        terminal["content"],
                        default_content_catalog().text("worker.failed").text,
                    )
                    self.assertNotIn(
                        leftover_text,
                        str(terminal["content"]),
                        "未收口回合的残余正文不得交付",
                    )

    def test_an_sdk_cancelled_turn_stays_a_failure_terminal_with_or_without_a_stop(
        self,
    ) -> None:
        """codex 一级独立审查 P1-2：``cancelled`` 不是 stop 的别名。它来自
        ``apps/worker/turn.py`` 的 ``_sdk_termination_failure`` 对 **SDK 自报**的
        ``aborted_streaming``/``aborted_tools`` 的归类，与本侧 ``stop_event`` /
        ``client.interrupt()`` 之间没有任何因果绑定——SDK 完全可能在没人 stop 的
        时候自行 abort。把它当作 stop 等价码，一次晚到的 stop 就能把真实的 SDK
        终止失败掩盖成"任务已停止"。

        因此两种情况都必须是失败终态：无 stop（既有行为的回归锁）与晚到的 stop
        （本次修复确立的语义）。
        """

        cancelled_report: dict[str, object] = {
            "turn": {"closed": False, "final_text": "", "session_id": None},
            "failure": {"code": "cancelled", "message": "任务已取消，未继续执行"},
        }
        expected_content = default_content_catalog().text("worker.failed").text

        # 无 stop：既有行为，不得因本次修复而改变。
        queue = FakeWorkerQueue()

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return dict(cancelled_report)

        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
        )
        asyncio.run(service.process_once())
        terminal = queue.terminals[0]
        self.assertEqual(terminal["terminal_kind"], "failed")
        self.assertEqual(terminal["error_kind"], "session_failed")
        self.assertEqual(terminal["content"], expected_content)

        # 晚到的 stop：结论必须与无 stop 时**完全一致**。
        for stop_arrival in self._STOP_ARRIVALS:
            with self.subTest(stop_arrival=stop_arrival):
                terminal = self._terminal_when_stop_lands_mid_turn(
                    cancelled_report, stop_arrival=stop_arrival
                )
                self.assertEqual(
                    terminal["terminal_kind"],
                    "failed",
                    "晚到的 stop 不得把 SDK 自行 abort 掩盖成「已停止」",
                )
                self.assertNotEqual(terminal["terminal_kind"], "stopped")
                self.assertEqual(terminal["error_kind"], "session_failed")
                self.assertEqual(terminal["content"], expected_content)

    def test_withheld_output_stays_redacted_withheld_when_stop_lands_concurrently(
        self,
    ) -> None:
        """Issue #195：withheld 分支自身语义不变（#186 F1 已定），并发到达的 stop
        也不得把它改写成 ``stopped``——那会让"整段正文因安全策略被拒发"这个可
        查询的独立终态消失，运维再也分不清用户是被拦了还是自己停的。

        无 stop 的同一形状由
        ``test_withheld_output_writes_redacted_withheld_terminal_not_success``
        覆盖，本用例只补 stop 并发这一维。
        """

        withheld_text = "本次结果涉及需要保护的内容，已被安全策略拦截，未能提供结果。"
        for stop_arrival in self._STOP_ARRIVALS:
            with self.subTest(stop_arrival=stop_arrival):
                terminal = self._terminal_when_stop_lands_mid_turn(
                    {
                        "turn": {
                            "closed": True,
                            "final_text": withheld_text,
                            "session_id": "new-session",
                            "output_safety": {
                                "blocked": True,
                                "withheld": True,
                                "reasons": ("forbidden_value",),
                            },
                            "user_result": "redacted_withheld",
                        },
                        "failure": None,
                    },
                    stop_arrival=stop_arrival,
                )

                self.assertEqual(terminal["terminal_kind"], "redacted_withheld")
                self.assertNotEqual(terminal["terminal_kind"], "stopped")
                self.assertEqual(terminal["error_kind"], "redacted_withheld")
                self.assertEqual(
                    terminal["content"],
                    default_content_catalog().text("worker.redacted_withheld").text,
                )
                self.assertNotIn(
                    withheld_text, str(terminal["content"]), "被拒发的正文不得交付"
                )

    def test_a_genuinely_interrupted_turn_keeps_the_stopped_terminal(self) -> None:
        """Issue #195：纯 stop 的语义不变——执行层确认这一轮真被中断
        （``failure_code == "interrupted"``，即 r4 已通过的 ``/stop`` 旅程所走的
        那条路径）时，终态仍是 ``stopped``；已经打出来的半截结果仍随
        ``worker.stopped_result`` 一起交付，没有正文时用 ``worker.stopped``。

        把 ``interrupted`` 从终态优先级最前面挪走必须让本用例变红。
        """

        catalog = default_content_catalog()
        for partial_text, expected_content in (
            ("已产出的半截结果", catalog.text("worker.stopped_result", result="已产出的半截结果").text),
            ("", catalog.text("worker.stopped").text),
        ):
            with self.subTest(partial_text=bool(partial_text)):
                queue = FakeWorkerQueue()

                class Executor:
                    async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                        return {
                            "turn": {"closed": False, "final_text": partial_text, "session_id": None},
                            "failure": {"code": "interrupted", "message": "AgentSessionInterrupted"},
                        }

                service = WorkerService(
                    config=worker_config(),
                    queue=queue,
                    executor_factory=lambda config, marker: Executor(),
                )
                asyncio.run(service.process_once())

                terminal = queue.terminals[0]
                self.assertEqual(terminal["terminal_kind"], "stopped")
                self.assertEqual(terminal["error_kind"], "stopped")
                self.assertEqual(terminal["content"], expected_content)

    def test_a_stopped_turn_with_everyday_wording_in_the_partial_result_does_not_crash(
        self,
    ) -> None:
        """Issue #322：``worker.stopped_result`` 渲染模型生成的残余正文时，不能
        再被为固定模板设计的自然语言词表（「还需」等）拦截。此前会在这里直接抛
        ``ContentSafetyError``，让 worker 在收口前就崩溃——比投递层误伤更严重：
        投递层好歹能转 uncertain 等人工抢救，这里是整个任务处理直接失败。"""

        partial_text = "已查到部分结果，还需要继续挖掘吗"
        queue = FakeWorkerQueue()

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {"closed": False, "final_text": partial_text, "session_id": None},
                    "failure": {"code": "interrupted", "message": "AgentSessionInterrupted"},
                }

        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
        )
        asyncio.run(service.process_once())

        terminal = queue.terminals[0]
        self.assertEqual(terminal["terminal_kind"], "stopped")
        self.assertIn(partial_text, terminal["content"])

    def test_terminal_outcome_callback_receives_failure_code_and_reasons_without_leaking_content(
        self,
    ) -> None:
        """Issue #90 评论 5306860255：queue 链路收口此前失败码与安全命中规则完全
        不可回读，r13 只能靠猜直接原因。独立复核 P1 之后，真实装配把这条低敏
        审计事件接到 ``apps/worker/cli.py`` 的结构化 stderr 出口（``worker.task.
        terminal``），不再直接调用 stdlib ``logging``——真实队列 worker 进程
        从不调用 ``logging.basicConfig()``，经 ``logging`` 发出的调用会被默认
        阈值悄悄吞掉，运维在容器 stderr 里永远看不到（见 ``cli.py`` 的
        ``_LogOnlyAlertSender`` 说明）。因此本用例改为断言注入的
        ``on_terminal_outcome`` 回调收到的字段，与生产装配走同一条协议；
        端到端的 CLI 接线级证据见 ``tests/test_worker_process.py`` 的
        ``QueueModeTerminalOutcomeLoggingTest``。

        同时做否定测试：正文样本与 ``user_id``（``open_id`` 的替身）、``prompt``
        一律不得出现在回调收到的任何字段里。删掉 ``_log_terminal_outcome`` 里对
        ``self._on_terminal_outcome(...)`` 的调用，或删掉 ``_finish_terminal``
        里对 ``_log_terminal_outcome`` 的调用，都必须让本用例变红。"""

        queue = FakeWorkerQueue()
        forbidden_content_sample = "本次结果涉及需要保护的内容，已被安全策略拦截，未能提供结果。"

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {
                        "closed": True,
                        "final_text": forbidden_content_sample,
                        "session_id": "new-session",
                        "output_safety": {
                            "blocked": True,
                            "withheld": True,
                            "reasons": ("forbidden_value",),
                        },
                        "user_result": "redacted_withheld",
                    },
                    "failure": None,
                }

        sink = RecordingTerminalOutcomeSink()
        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
            on_terminal_outcome=sink,
        )
        asyncio.run(service.process_once())

        self.assertEqual(len(sink.calls), 1)
        fields = sink.calls[0]
        self.assertEqual(fields["task_id"], "tsk-1")
        self.assertEqual(fields["error_kind"], "redacted_withheld")
        self.assertEqual(fields["terminal_kind"], "redacted_withheld")
        self.assertIs(fields["output_safety_blocked"], True)
        self.assertIs(fields["output_safety_withheld"], True)
        self.assertIn("forbidden_value", fields["output_safety_reasons"])
        self.assertIs(fields["truncated"], False)
        # 否定测试：正文样本、user_id（open_id 的替身）与 prompt 一律不得出现在
        # 回调收到的字段里——这条审计事件的存在不能反过来变成新的敏感信息
        # 泄漏面。对整个字典做字符串化检查，覆盖任何字段而不是逐个枚举。
        serialized = repr(fields)
        self.assertNotIn(forbidden_content_sample, serialized)
        self.assertNotIn(queue.context.user_id, serialized)
        self.assertNotIn(queue.context.prompt, serialized)

    def test_terminal_outcome_callback_reports_denied_tool_calls_even_on_a_successful_turn(
        self,
    ) -> None:
        """Issue #291 独立审查：``tool_policy.py`` 的拒绝文案对用户承诺"这是
        系统侧的临时限制、问题已经被记录"，但此前 queue 链路从未把
        ``report["audit"]["denied_count"]``（``report.py`` 早就算出来了）写进
        任何运维可见的地方——模型撞上一次越界工具、自己绕过继续把回合正常
        收口时（这正是设计要它做的事），运维在生产 stderr 里看不到任何一次
        拒绝的痕迹，只有靠用户反馈才会发现白名单配错（真实事故复现路径）。

        本用例模拟这个"回合仍然成功收口，但中途有一次拒绝"的真实形状：
        断言 ``on_terminal_outcome`` 收到的字段里出现了这次拒绝的计数与工具
        名，且不泄漏被拒调用的入参。删掉 ``service.py`` 里 ``_denied_tool_
        summary``/``_process_task`` 对它的调用，或 ``_log_terminal_outcome``
        里对应的字段，都必须让本用例变红。"""

        queue = FakeWorkerQueue()

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {
                        "closed": True,
                        "final_text": "日活是 1024；另一部分我无法查询。",
                        "session_id": "new-session",
                        "output_safety": {"blocked": False, "withheld": False, "reasons": ()},
                    },
                    "audit": {
                        "denied_count": 1,
                        "denied": [
                            {
                                "tool_name": "Bash",
                                "deny_reason_code": "not_in_whitelist",
                                "allowed": False,
                                "executed": False,
                                "tool_input": {},
                                "error": None,
                                "result_kind": None,
                            }
                        ],
                    },
                    "failure": None,
                }

        sink = RecordingTerminalOutcomeSink()
        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
            on_terminal_outcome=sink,
        )
        asyncio.run(service.process_once())

        self.assertEqual(len(sink.calls), 1)
        fields = sink.calls[0]
        self.assertEqual(fields["terminal_kind"], "success")
        self.assertEqual(fields["denied_count"], 1)
        self.assertEqual(fields["denied_tool_names"], ("Bash",))

    def test_terminal_outcome_callback_reports_no_denials_when_none_happened(self) -> None:
        """否定测试：正常回合（没有任何 ``PreToolUse`` 拒绝）不得凭空报出拒绝，
        避免上一条用例的字段被恒真实现（例如把 ``denied_count`` 写死为 1）
        蒙混过关。"""

        queue = FakeWorkerQueue()

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {"closed": True, "final_text": "日活是 1024。", "session_id": "s"},
                    "audit": {"denied_count": 0, "denied": []},
                    "failure": None,
                }

        sink = RecordingTerminalOutcomeSink()
        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
            on_terminal_outcome=sink,
        )
        asyncio.run(service.process_once())

        fields = sink.calls[0]
        self.assertEqual(fields["denied_count"], 0)
        self.assertEqual(fields["denied_tool_names"], ())

    def test_write_terminal_event_receives_token_usage_and_guard_denied_count_for_report(
        self,
    ) -> None:
        """通报补数（Issue #303/#304 批次 4，迁移 0070）：终态写入调用必须携带
        从 ``report`` 里取出的 ``token_usage``/``guard_denied_count``——这两个
        值与低敏日志用的 ``denied_count``（同一份 report 里的同一个数字）故意
        分开求值，这里钉住它们确实被传给了 ``queue.write_terminal_event``。"""

        queue = FakeWorkerQueue()

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {
                        "closed": True,
                        "final_text": "日活是 1024。",
                        "session_id": "s",
                        "output_safety": {"blocked": False, "withheld": False, "reasons": ()},
                    },
                    "audit": {"denied_count": 2, "denied": [], "usage": {"status": "known", "fields": {}}},
                    "resources": {
                        "usage": {
                            "status": "known",
                            "source": "sdk",
                            "fields": {"input_tokens": 1000, "output_tokens": 200},
                        }
                    },
                    "failure": None,
                }

        service = WorkerService(
            config=worker_config(), queue=queue, executor_factory=lambda config, marker: Executor()
        )
        asyncio.run(service.process_once())

        self.assertEqual(len(queue.terminals), 1)
        terminal = queue.terminals[0]
        self.assertEqual(terminal["guard_denied_count"], 2)
        self.assertEqual(terminal["token_usage"], {"input_tokens": 1000, "output_tokens": 200})

    def test_write_terminal_event_treats_a_negative_denied_count_as_untrustworthy(self) -> None:
        """批次 4 opus 审查 P3-2：``guard_denied_count`` 不存在"负几次"——一个负数
        只可能是上游数据被破坏，与 ``_report_token_usage`` 对 token 字段的
        ``candidate >= 0`` 校验对称，``_report_guard_denied_count`` 也必须拒绝
        负数、按结构性不可信处理（返回 ``None``），不能把它当成一个"合法但奇怪"
        的整数原样落库——那会让 ``core/daily_report.py`` 的聚合把一个坏数字当
        真实拒绝次数计入总和。"""

        queue = FakeWorkerQueue()

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {
                        "closed": True,
                        "final_text": "日活是 1024。",
                        "session_id": "s",
                        "output_safety": {"blocked": False, "withheld": False, "reasons": ()},
                    },
                    "audit": {"denied_count": -1, "denied": []},
                    "failure": None,
                }

        service = WorkerService(
            config=worker_config(), queue=queue, executor_factory=lambda config, marker: Executor()
        )
        asyncio.run(service.process_once())

        self.assertEqual(len(queue.terminals), 1)
        terminal = queue.terminals[0]
        self.assertIsNone(terminal["guard_denied_count"], "负数必须按结构性不可信处理，不能原样落库")

    def test_write_terminal_event_gets_none_not_zero_when_the_turn_never_really_ran(self) -> None:
        """早退分支（这里用执行器抛未预期异常模拟）从未真正跑过一次回合，
        ``report`` 不带 ``audit``/``resources``——``token_usage``/
        ``guard_denied_count`` 必须是 ``None``（结构性地取不到），不能被悄悄
        编造成 0 或空字典。删掉 ``_report_guard_denied_count``/``_report_
        token_usage`` 里"结构不符就返回 None"的判断，会让本用例变红。"""

        queue = FakeWorkerQueue()

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                raise RuntimeError("模拟执行器未预期异常")

        service = WorkerService(
            config=worker_config(), queue=queue, executor_factory=lambda config, marker: Executor()
        )
        asyncio.run(service.process_once())

        self.assertEqual(len(queue.terminals), 1)
        terminal = queue.terminals[0]
        self.assertIsNone(terminal["guard_denied_count"])
        self.assertIsNone(terminal["token_usage"])

    def test_terminal_outcome_callback_caps_failure_code_and_reason_length(self) -> None:
        """Issue #90 评论 5306860255 独立复核 P3-2：``failure_code`` 与
        ``output_safety`` 的每个原因码目前都来自本仓库固定的枚举式常量，但审计
        事件是低敏信息的唯一出口——不设长度上界，就是给"未来某次改动不小心把
        一段自由文本塞进这两个字段"留了一条不设防的泄漏面。截到 64 字符并标记
        ``truncated=True``。"""

        queue = FakeWorkerQueue()
        oversized_failure_code = "x" * 100
        oversized_reason = "y" * 80

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {
                        "closed": False,
                        "final_text": "",
                        "output_safety": {
                            "blocked": True,
                            "withheld": False,
                            "reasons": (oversized_reason,),
                        },
                    },
                    "failure": {"code": oversized_failure_code},
                }

        sink = RecordingTerminalOutcomeSink()
        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
            on_terminal_outcome=sink,
        )
        asyncio.run(service.process_once())

        self.assertEqual(len(sink.calls), 1)
        fields = sink.calls[0]
        self.assertEqual(fields["failure_code"], oversized_failure_code[:64])
        self.assertEqual(len(fields["failure_code"]), 64)
        self.assertEqual(fields["output_safety_reasons"], (oversized_reason[:64],))
        self.assertIs(fields["truncated"], True)

    def test_a_global_stop_signal_interrupts_an_in_flight_turn_within_the_poll_interval(
        self,
    ) -> None:
        """Issue #153：SIGTERM 落地为 ``run(stop_event=...)`` 收到停止信号时，在途
        任务的 ``_monitor`` 必须把这次全局停机与用户 ``/stop`` 同等对待——不是等
        任务自然结束（例如撞满 turn_timeout），而是在一个 ``stop_poll_interval``
        量级的时间内就让执行器收到中断请求，并把任务收口为 ``stopped`` 终态。
        改掉 ``_monitor`` 里对 ``self._global_stop`` 的检查（或不在 ``run()`` 里
        赋值它）都必须让本用例变红：要么在途回合永远等不到中断（用例超时），
        要么虽然结束但终态不是 ``stopped``。
        """

        queue = FakeWorkerQueue()
        turn_started = threading.Event()

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                turn_started.set()
                # 真实执行器在 stop_event 被置位时会向 Agent SDK 发起 interrupt()
                # 并很快返回；这里直接用同一个协作式信号模拟"回合已经响应中断"。
                await kwargs["stop_event"].wait()  # type: ignore[union-attr]
                return {
                    "turn": {"closed": False, "final_text": ""},
                    "failure": {"code": "interrupted"},
                }

        service = WorkerService(
            config=worker_config(stop_poll_interval_seconds=0.02),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
        )

        async def scenario() -> float:
            global_stop = asyncio.Event()
            run_task = asyncio.create_task(service.run(stop_event=global_stop))
            started = await asyncio.to_thread(turn_started.wait, 2.0)
            self.assertTrue(started, "回合应先真正开始执行，才能验证停机信号能中断它")
            began_stopping_at = time.monotonic()
            global_stop.set()
            await asyncio.wait_for(run_task, timeout=2.0)
            return time.monotonic() - began_stopping_at

        elapsed = asyncio.run(scenario())

        self.assertLess(
            elapsed,
            1.0,
            "全局停机信号必须在一个 stop_poll_interval 量级内让在途回合收到中断"
            "请求并收口，不能等到轮询周期之外的更长预算",
        )
        terminal = queue.terminals[0]
        self.assertEqual(terminal["terminal_kind"], "stopped")
        self.assertEqual(terminal["error_kind"], "stopped")

    def test_a_sigterm_that_lands_during_the_synchronous_housekeeping_stretch_stops_claiming(
        self,
    ) -> None:
        """PR #173 独立复核 P2-1：`run()` 的 `while not stop.is_set()` 判定与
        `claim()` 之间没有任何 `await`（心跳、告警 tick、`_housekeep()` 全是同步
        代码），而 `loop.add_signal_handler` 装的回调靠自管道机制投递，只有
        事件循环真正让出控制权时才会被处理——纯同步的代码段中间没有能被它
        插进来的缝隙。真实 SIGTERM 如果恰好在这段同步窗口内被操作系统送达，
        Python 侧当时只是把回调排进了事件循环的就绪队列，`process_once()`
        如果不主动让出一次就直接判定，读到的仍是旧值，会把一条还在排队、
        从未执行过的任务领走，直接收口成 ``stopped`` 且不会被重排。

        复现手法：把 ``_housekeep`` 换成一个"在这次同步调用期间，把停机信号
        排进事件循环就绪队列"的版本——这精确模拟了"信号回调已排队、尚未
        执行"这个状态，不依赖真实操作系统信号的时序抖动。

        变异验证：把 `process_once()` 里 `claim()` 前的 `await
        asyncio.sleep(0)` + 重新判定 `is_set()` 整段删掉，本用例会变红
        （``claim_calls`` 从 0 变成 1，任务被领走后从未真正执行就收口成
        ``stopped``）。
        """

        queue = FakeWorkerQueue()
        claim_calls = 0
        original_claim = queue.claim

        def counting_claim(**kwargs: object) -> list[ClaimedTask]:
            nonlocal claim_calls
            claim_calls += 1
            return original_claim(**kwargs)

        queue.claim = counting_claim  # type: ignore[assignment]

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {"closed": True, "final_text": "结果", "session_id": "s"},
                    "failure": None,
                }

        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
        )

        async def scenario() -> None:
            stop = asyncio.Event()

            # 精确模拟"SIGTERM 的自管道回调已经在事件循环就绪队列里排队、
            # 但还没被处理"：这次同步的 `_housekeep()` 调用期间，操作系统
            # 送达了信号，`loop.call_soon` 把 `stop.set` 排进了就绪队列。
            original_housekeep = service._housekeep

            def housekeeping_that_races_with_sigterm() -> list[object]:
                asyncio.get_running_loop().call_soon(stop.set)
                return original_housekeep()

            service._housekeep = housekeeping_that_races_with_sigterm  # type: ignore[method-assign]

            run_task = asyncio.ensure_future(service.run(stop_event=stop))
            await asyncio.wait_for(run_task, timeout=2.0)

        asyncio.run(scenario())

        self.assertEqual(
            claim_calls,
            0,
            "停机信号已经排队后，claim() 不应该再被调用——一条从未执行过的"
            "排队任务不该被领走后直接收口成 stopped",
        )
        self.assertIsNotNone(
            queue.claimed, "任务必须仍留在可领取状态，留给下一次启动的 worker 领走"
        )
        self.assertEqual(queue.terminals, [], "没有任何任务应该被写成终态")

    def test_liveness_stays_fresh_through_a_long_in_flight_turn(self) -> None:
        """PR #173 独立复核 P1-5：``_emit_heartbeat()``（进而 ``touch_liveness``）
        此前只在 ``process_once()`` 开头调用一次，而 ``process_once()`` 会
        ``await asyncio.gather(...)`` 等完整批任务才返回——任何明显长于活性
        阈值的正常回合都会把活性文件晾在原地不动，直到该批任务结束，健康检查
        在系统最忙的时候持续说谎（生产比例：`turn_timeout_seconds` 默认
        600s、worker 活性阈值默认 60s）。

        本用例用远比生产宽松的比例复现（回合 0.4s、阈值 0.15s，宽松约 4 倍）：
        ``_monitor`` 本来就按 ``stop_poll_interval_seconds`` 在跳、贯穿整个
        在途任务的生命周期，是"进程仍在做正确的事"这个信号真正应该来源的
        地方。

        变异存活证据：把 ``_monitor`` 循环里那次 ``self._emit_heartbeat()``
        删掉，本用例会变红（活性年龄峰值追上回合时长，超过阈值）。
        """

        from lingxi.apps.liveness import read_liveness_age_seconds, touch_liveness

        queue = FakeWorkerQueue()
        turn_seconds = 0.4
        threshold_seconds = 0.15

        class SlowExecutor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                await asyncio.sleep(turn_seconds)
                return {
                    "turn": {"closed": True, "final_text": "结果", "session_id": "s"},
                    "failure": None,
                }

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            max_age_seen = 0.0

            service = WorkerService(
                config=worker_config(stop_poll_interval_seconds=0.02),
                queue=queue,
                executor_factory=lambda config, marker: SlowExecutor(),
                heartbeat=lambda: touch_liveness("worker", directory=directory),
            )

            async def poll_liveness_during_the_turn() -> None:
                nonlocal max_age_seen
                deadline = time.monotonic() + turn_seconds + 0.3
                while time.monotonic() < deadline:
                    age = read_liveness_age_seconds("worker", directory=directory)
                    if age is not None:
                        max_age_seen = max(max_age_seen, age)
                    await asyncio.sleep(0.02)

            async def scenario() -> None:
                poller = asyncio.ensure_future(poll_liveness_during_the_turn())
                await service.process_once()
                poller.cancel()
                try:
                    await poller
                except asyncio.CancelledError:
                    pass

            asyncio.run(scenario())

        self.assertLess(
            max_age_seen,
            threshold_seconds,
            "在途任务执行期间活性文件年龄不应超过阈值——否则健康检查会在"
            "完全正常的长回合期间把容器判定为 unhealthy",
        )

    def test_a_real_sigterm_that_lands_during_the_synchronous_housekeeping_stretch_stops_claiming(
        self,
    ) -> None:
        """PR #173 独立复核第二轮 P2-1：上一轮 `test_a_sigterm_that_lands_
        during_the_synchronous_housekeeping_stretch_stops_claiming` 用
        `loop.call_soon(stop.set)` 模拟"信号回调已排队未执行"被复核证明与
        真实信号路径不等价——`loop.call_soon` 直接进就绪队列，跳过了
        `loop.add_signal_handler` 自管道机制的两跳，一次 `sleep(0)` 就够；真实
        SIGTERM 经自管道投递需要三轮让出才会被观测到（见
        ``service.py`` 模块顶部 ``_STOP_SIGNAL_DRAIN_YIELDS`` 的机制说明）。
        本用例改用**真实 POSIX 信号**（进程内 ``os.kill(os.getpid(),
        signal.SIGTERM)``）经真实 ``_run_queue_worker`` 里真实的
        ``loop.add_signal_handler`` 路径复现，不再依赖任何模拟。

        复现手法：``os.kill`` 从 ``_housekeep()`` 内部、在它返回前触发——这
        精确复现"SIGTERM 恰好在 claim() 前的同步窗口内被操作系统送达"这个
        场景，且是真实内核信号投递，不是任何形式的模拟。

        变异验证：把 `process_once()` 里 claim() 前新增的多轮让出+复判整段
        删掉（或把 `_STOP_SIGNAL_DRAIN_YIELDS` 改回 1），本用例会变红
        （``claim_calls`` 从 0 变成 1，任务被领走后从未真正执行就收口成
        ``stopped``，且不会被重排）。
        """

        from lingxi.adapters.postgres_conversation import ClaimedTask as _ClaimedTask
        from lingxi.apps.worker.cli import _run_queue_worker

        queue = FakeWorkerQueue()
        claim_calls = 0
        original_claim = queue.claim

        def counting_claim(**kwargs: object) -> list[_ClaimedTask]:
            nonlocal claim_calls
            claim_calls += 1
            return original_claim(**kwargs)

        queue.claim = counting_claim  # type: ignore[assignment]

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {"closed": True, "final_text": "结果", "session_id": "s"},
                    "failure": None,
                }

        service = WorkerService(
            config=worker_config(poll_interval_seconds=0.01),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
        )

        original_housekeep = service._housekeep
        signal_sent = False

        def housekeeping_that_sends_a_real_sigterm() -> list[object]:
            nonlocal signal_sent
            result = original_housekeep()
            if not signal_sent:
                signal_sent = True
                # 真实内核信号，不是模拟。此时 `_run_queue_worker` 已经用真实
                # `loop.add_signal_handler` 装好了 SIGTERM 处理器，同进程内
                # 自己发给自己是让真实信号落在这段同步窗口内最直接、最可控的
                # 方式——处理器已接管，不会走到默认终止行为，不影响测试进程
                # 本身的存活。
                os.kill(os.getpid(), signal.SIGTERM)
            return result

        service._housekeep = housekeeping_that_sends_a_real_sigterm  # type: ignore[method-assign]

        err = io.StringIO()

        async def scenario() -> None:
            await _run_queue_worker(
                service,
                shutdown_timeout_seconds=2.0,
                err=err,
                trace_id="01J00000000000000000000SIG",
            )

        asyncio.run(asyncio.wait_for(scenario(), timeout=8.0))

        self.assertIn(
            "worker.queue.signal_received",
            err.getvalue(),
            "真实信号处理器必须确实跑过一次，否则本用例没有测到真实信号路径",
        )
        self.assertEqual(
            claim_calls,
            0,
            "真实 SIGTERM 已经在 housekeeping 同步窗口内送达后，claim() 不应该"
            "再被调用——一条从未执行过的排队任务不该被领走后直接收口成"
            " stopped",
        )
        self.assertIsNotNone(
            queue.claimed, "任务必须仍留在可领取状态，留给下一次启动的 worker 领走"
        )
        self.assertEqual(queue.terminals, [], "没有任何任务应该被写成终态")


class SemanticProgressTests(unittest.TestCase):
    """Issue #321 方向 C：语义化等待进度——工具调用阶段文案 + 30 秒兜底刷新
    （产品负责人 2026-08-27 裁定，留痕 #321 评论 5434086490）。

    只测数据管路与节流判据本身（谁写了几条 progress 事件、``content`` 字段解码
    后是什么语义），不测 CardKit 真实渲染（L4a）；文案本身不回显工具名/参数的
    校验见 ``CardStreamTests.
    test_querying_and_composing_actions_render_without_leaking_any_tool_identity``。
    """

    def test_two_spaced_out_tool_calls_produce_two_correctly_numbered_query_updates(
        self,
    ) -> None:
        """①：长任务两次工具调用（间隔远超过 5 秒节流窗口）——各自产生一条独立
        更新，计数正确递增。

        变异存活证据：把 ``on_tool_call`` 里的 ``query_count += 1`` 删掉（改成
        恒为某个固定值），本用例第二条更新的计数断言必须变红。
        """

        queue = FakeWorkerQueue()
        clock = {"now": 0.0}

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                on_tool_call = kwargs["on_tool_call"]
                clock["now"] = 10.0
                on_tool_call("mcp__query__list_metrics")  # type: ignore[misc]
                clock["now"] = 20.0
                on_tool_call("mcp__query__list_metrics")  # type: ignore[misc]
                return {
                    "turn": {"closed": True, "final_text": "结果", "session_id": "s"},
                    "failure": None,
                }

        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
            monotonic=lambda: clock["now"],
        )
        asyncio.run(service.process_once())

        progress_events = [e for e in queue.events if e["event_type"] == "progress"]
        self.assertEqual(len(progress_events), 2, "两次间隔充分的工具调用应各产生一条更新")
        self.assertEqual(
            decode_progress_action(progress_events[0]["content"]),
            (PROGRESS_ACTION_QUERYING, 1, "list_metrics"),
        )
        self.assertEqual(
            decode_progress_action(progress_events[1]["content"]),
            (PROGRESS_ACTION_QUERYING, 2, "list_metrics"),
        )

    def test_a_burst_of_tool_calls_within_five_seconds_is_merged_into_one_update(
        self,
    ) -> None:
        """③：5 秒内密集工具事件——不能逐条落库，必须合并；最终写入的一条必须
        反映最新（不是最早）的调用计数，被合并掉的调用不能凭空消失。

        变异存活证据：把节流判据 ``now - last_progress_write_at < min_gap_
        seconds`` 改成恒 ``False``（从不节流），本用例的"只有一条 progress"
        断言必须变红（会变成 4 条）。
        """

        queue = FakeWorkerQueue()
        clock = {"now": 0.0}

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                on_tool_call = kwargs["on_tool_call"]
                for offset in (0.5, 1.5, 2.5):  # 全部落在 5 秒节流窗口内，应被合并
                    clock["now"] = offset
                    on_tool_call("mcp__query__list_metrics")  # type: ignore[misc]
                clock["now"] = 12.0  # 跨过节流窗口，触发一次真正的写入
                # 步骤名换成另一个已知子步骤，核对合并后落库的是最新一次的
                # 步骤名，不是被合并掉的早期调用留下的旧值。
                on_tool_call("mcp__query__query_metric")  # type: ignore[misc]
                return {
                    "turn": {"closed": True, "final_text": "结果", "session_id": "s"},
                    "failure": None,
                }

        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
            monotonic=lambda: clock["now"],
        )
        asyncio.run(service.process_once())

        progress_events = [e for e in queue.events if e["event_type"] == "progress"]
        self.assertEqual(len(progress_events), 1, "5 秒内的密集调用必须合并，不能逐条写库")
        self.assertEqual(
            decode_progress_action(progress_events[0]["content"]),
            (PROGRESS_ACTION_QUERYING, 4, "query_metric"),
            "被合并掉的三次调用不能凭空消失——最终写入的必须是最新的调用计数与步骤名",
        )

    def test_a_non_query_tool_call_is_reported_as_working_not_composing(self) -> None:
        """Issue #407：区分三类文案。非问数工具（含被拒绝的越界调用）现在归入
        独立的"working"文案，不再与"模型正在输出文本"的 composing 共用一句话；
        不单独计数、不回显工具名。"""

        queue = FakeWorkerQueue()
        clock = {"now": 0.0}

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                clock["now"] = 10.0
                kwargs["on_tool_call"]("Bash")  # type: ignore[index]
                return {
                    "turn": {"closed": True, "final_text": "结果", "session_id": "s"},
                    "failure": None,
                }

        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
            monotonic=lambda: clock["now"],
        )
        asyncio.run(service.process_once())

        progress_events = [e for e in queue.events if e["event_type"] == "progress"]
        self.assertEqual(len(progress_events), 1)
        self.assertEqual(
            decode_progress_action(progress_events[0]["content"]),
            (PROGRESS_ACTION_WORKING, None, None),
        )

    def test_model_text_output_is_reported_as_composing_distinct_from_working(self) -> None:
        """Issue #407：模型文本输出（``assistant_message``）与其它工具调用现在
        各自独立文案——同一回合先后触发两种信号，必须解码出两种不同的动作码，
        不能像改动前那样都归并成同一句"生成阶段"文案。"""

        queue = FakeWorkerQueue()
        clock = {"now": 0.0}

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                clock["now"] = 10.0
                kwargs["on_tool_call"]("Bash")  # type: ignore[index]
                clock["now"] = 20.0
                kwargs["on_stream_event"]({"kind": "assistant_message"})  # type: ignore[index]
                return {
                    "turn": {"closed": True, "final_text": "结果", "session_id": "s"},
                    "failure": None,
                }

        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
            monotonic=lambda: clock["now"],
        )
        asyncio.run(service.process_once())

        progress_events = [e for e in queue.events if e["event_type"] == "progress"]
        self.assertEqual(len(progress_events), 2)
        self.assertEqual(
            decode_progress_action(progress_events[0]["content"]),
            (PROGRESS_ACTION_WORKING, None, None),
        )
        self.assertEqual(
            decode_progress_action(progress_events[1]["content"]),
            (PROGRESS_ACTION_COMPOSING, None, None),
        )

    def test_an_unmapped_query_tool_suffix_falls_back_to_the_generic_querying_text_without_leaking_it(
        self,
    ) -> None:
        """否定用例（Issue #407 出口安全红线）：模型可能调用一个仍然落在
        ``mcp__query__`` 前缀下、但不是四个已知子步骤之一的工具名（臆造的工具名、
        或注入的内部标识）。这条子步骤名必须在编码这一步就被白名单挡下，进度
        事件的 ``content`` 字段里不得出现这个原始字符串——否则 Gateway 侧后续
        任何处理疏漏都可能把它带进用户可见卡片。"""

        queue = FakeWorkerQueue()
        clock = {"now": 0.0}
        injected_suffix = "mcp__query__internal_admin_delete_all"

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                clock["now"] = 10.0
                kwargs["on_tool_call"](injected_suffix)  # type: ignore[index]
                return {
                    "turn": {"closed": True, "final_text": "结果", "session_id": "s"},
                    "failure": None,
                }

        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
            monotonic=lambda: clock["now"],
        )
        asyncio.run(service.process_once())

        progress_events = [e for e in queue.events if e["event_type"] == "progress"]
        self.assertEqual(len(progress_events), 1)
        raw_content = progress_events[0]["content"]
        self.assertNotIn(
            "internal_admin_delete_all",
            raw_content or "",
            "未知子步骤名不得原样落进 outbox content 字段",
        )
        self.assertEqual(
            decode_progress_action(raw_content),
            (PROGRESS_ACTION_QUERYING, 1, None),
            "未映射的子步骤退回不带步骤名的通用查询状态，不是拒绝或报错",
        )

    def test_a_stalled_long_task_gets_exactly_one_thirty_second_fallback_update(
        self,
    ) -> None:
        """②：30 秒无任何事件——``_monitor`` 的兜底计时必须恰好推一次纯用时更新，
        不多不少。走真实的 ``_monitor`` 循环（不是直接调用 ``on_stall_tick``），
        用一个只由 ``sleep()`` 推进的虚拟时钟把 30 秒虚拟等待压缩进近乎零的真实
        墙钟时间——``monotonic()`` 只读、``sleep()`` 才是唯一的推进点，因此耗时
        完全由 ``_monitor`` 自己的循环次数（乘以 ``stop_poll_interval_seconds``）
        决定，不依赖真实时钟或调度器的时序抖动。

        变异存活证据：把 ``_monitor`` 里 ``on_stall_tick()`` 那次调用整段删掉，
        本用例会等到 500 次让出用尽、拿到 0 条 progress 事件，断言变红。
        """

        queue = FakeWorkerQueue()

        class _VirtualClock:
            def __init__(self) -> None:
                self.now = 0.0

            def monotonic(self) -> float:
                return self.now

            async def sleep(self, seconds: float) -> None:
                self.now += seconds
                await asyncio.sleep(0)

        clock = _VirtualClock()

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                for _ in range(500):
                    if len(queue.events) >= 2:
                        break
                    await asyncio.sleep(0)
                return {
                    "turn": {"closed": True, "final_text": "结果", "session_id": "s"},
                    "failure": None,
                }

        service = WorkerService(
            config=worker_config(stop_poll_interval_seconds=5.0),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        asyncio.run(service.process_once())

        progress_events = [e for e in queue.events if e["event_type"] == "progress"]
        self.assertEqual(len(progress_events), 1, "30 秒无事件应恰好触发一次兜底用时更新")
        self.assertEqual(
            decode_progress_action(progress_events[0]["content"]),
            (PROGRESS_ACTION_PROCESSING, None, None),
            "本用例从未发生任何可分类信号，兜底更新沿用默认 processing 语义",
        )
        self.assertGreaterEqual(progress_events[0]["elapsed_seconds"], 30)

    def test_a_short_task_with_no_tool_calls_keeps_its_terminal_content_byte_for_byte(
        self,
    ) -> None:
        """④哨兵：短任务（没有工具调用、没有 assistant_message 中间事件）的终态
        内容必须逐字节不变，也不应该凭空产生进度更新——语义化进度只改变卡片
        中途的样子，不改变最终答案，也不该在什么都没发生时无中生有。
        """

        queue = FakeWorkerQueue()

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {"closed": True, "final_text": "结果", "session_id": "s"},
                    "failure": None,
                }

        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
        )
        asyncio.run(service.process_once())

        terminal = queue.terminals[0]
        self.assertEqual(terminal["terminal_kind"], "success")
        self.assertIsNone(terminal["error_kind"])
        self.assertEqual(terminal["content"], "结果")
        progress_events = [e for e in queue.events if e["event_type"] == "progress"]
        self.assertEqual(
            progress_events, [], "没有任何工具调用/文本事件时不应凭空产生进度更新"
        )


@unittest.skipUnless(POSTGRES_READY, SKIP_DB)
class RealQueueTerminalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert DSN is not None
        ensure_production_schema(DSN)

    def setUp(self) -> None:
        assert DSN is not None
        reset_production_rows(DSN)
        self.queue = PostgresTaskQueue(DSN)
        with connect(DSN) as connection:
            with connection.transaction():
                connection.execute(
                    """INSERT INTO app_user
                       (id, feishu_open_id, feishu_user_id, feishu_union_id,
                        display_name, department, tenant_key, provisioning_state)
                       VALUES ('usr-90','ou-90','u-90','un-90','张三','数据部','tk-90','active')"""
                )

    def _insert_old_task(
        self,
        *,
        task_id: str,
        conversation_id: str,
        version: str = "stable",
        status: str = "queued",
        side_effect_state: str = "none",
        attempts: int = 0,
    ) -> None:
        assert DSN is not None
        with connect(DSN) as connection:
            with connection.transaction():
                connection.execute(
                    """INSERT INTO conversation
                       (id,user_id,feishu_chat_id,feishu_thread_id,running_task_id)
                       VALUES (%s,'usr-90',%s,%s,%s)""",
                    (
                        conversation_id,
                        f"chat-{conversation_id}",
                        f"topic-{conversation_id}",
                        task_id,
                    ),
                )
                connection.execute(
                    """INSERT INTO task
                       (id,conversation_id,user_id,inbound_event_id,prompt,status,
                        target_worker_version,worker_id,heartbeat_at,attempts,
                        created_at,scheduled_at,side_effect_state,content_expires_at)
                       VALUES (%s,%s,'usr-90',%s,'问题',%s,%s,%s,
                               now()-interval '5 minutes',%s,
                               now()-interval '5 minutes',now()-interval '5 minutes',%s,now())""",
                    (
                        task_id,
                        conversation_id,
                        f"event-{task_id}",
                        status,
                        version,
                        "worker-old" if status == "running" else None,
                        attempts,
                        side_effect_state,
                    ),
                )

    def _scalar(self, sql: str, parameters: tuple[object, ...] = ()) -> object:
        assert DSN is not None
        with connect(DSN) as connection:
            return connection.execute(sql, parameters).fetchone()[0]

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> None:
        assert DSN is not None
        with connect(DSN) as connection:
            with connection.transaction():
                connection.execute(sql, parameters)

    def _terminal_event(self, task_id: str) -> tuple[str, str, str] | None:
        """按 ``task_id`` 读回它的 ``terminal`` 投递事件；不存在则 ``None``。

        返回 ``(terminal_kind, error_kind, content)`` 三元组，用于 Issue #178
        的一组断言：系统代为收口路径必须写出唯一、可投递、内容诚实的终态事件，
        不能只改 ``task.status``。
        """

        with connect(DSN) as connection:
            row = connection.execute(
                """SELECT terminal_kind, error_kind, content FROM task_delivery_event
                   WHERE task_id = %s AND event_type = 'terminal'""",
                (task_id,),
            ).fetchone()
        return tuple(row) if row is not None else None

    def _event_count(self, task_id: str) -> int:
        with connect(DSN) as connection:
            return connection.execute(
                "SELECT count(*) FROM task_delivery_event WHERE task_id = %s", (task_id,)
            ).fetchone()[0]

    def test_queued_without_worker_writes_an_honest_terminal_event_and_keeps_the_topic(
        self,
    ) -> None:
        """Issue #178（红线）：系统代为收口不得只改 task.status——必须写出唯一、
        可投递的用户终态事件，交给 Gateway 正常投递，confirm 后才释放话题。
        """

        self._insert_old_task(task_id="tsk-q", conversation_id="cnv-q")
        terminals = self.queue.reclaim_queued(max_wait=timedelta(minutes=3))
        self.assertEqual([item.error_kind for item in terminals], ["queued_timeout"])
        self.assertEqual([item.status for item in terminals], ["awaiting_delivery"])
        with connect(DSN) as connection:
            self.assertEqual(
                connection.execute("SELECT status FROM task WHERE id='tsk-q'").fetchone()[0],
                "awaiting_delivery",
            )
            # 话题继续占用：还没有人确认送达，同一话题的下一条消息不该被放行
            # 进来抢占——与真实 worker 写 terminal 事件后的既有语义一致。
            self.assertEqual(
                connection.execute(
                    "SELECT running_task_id FROM conversation WHERE id='cnv-q'"
                ).fetchone()[0],
                "tsk-q",
            )
        terminal_kind, error_kind, content = self._terminal_event("tsk-q")
        self.assertEqual(terminal_kind, "failed")
        self.assertEqual(error_kind, "queued_timeout")
        self.assertEqual(content, default_content_catalog().text("worker.queued_timeout").text)
        # 唯一：started + terminal，不多不少；确认 Gateway 真的有内容可读。
        self.assertEqual(self._event_count("tsk-q"), 2)

        # 幂等：housekeeping 重复轮询（进程重启、下一轮 poll）不得再产生第二条
        # 终态——命中已判过的这一批时，`reclaim_queued` 的查询条件
        # （`status='queued'`）天然不会再选中它，返回空列表；事件表行数不变。
        again = self.queue.reclaim_queued(max_wait=timedelta(minutes=3))
        self.assertEqual(again, [])
        self.assertEqual(self._event_count("tsk-q"), 2)

        # 闭环：Gateway 消费 outbox 后调用 confirm_delivery，业务终态才真正收敛
        # 为 failed 并释放话题——不是这条收口路径自己直接释放。
        confirmed = self.queue.confirm_delivery(
            task_id="tsk-q", platform_message_kind="text", platform_message_id="om_fake_q"
        )
        self.assertTrue(confirmed)
        with connect(DSN) as connection:
            row = connection.execute(
                "SELECT status, error_kind FROM task WHERE id='tsk-q'"
            ).fetchone()
            self.assertEqual(tuple(row), ("failed", "queued_timeout"))
            self.assertIsNone(
                connection.execute(
                    "SELECT running_task_id FROM conversation WHERE id='cnv-q'"
                ).fetchone()[0]
            )

    def test_unavailable_version_writes_an_honest_terminal_event_and_does_not_change_version(
        self,
    ) -> None:
        self._insert_old_task(task_id="tsk-c", conversation_id="cnv-c", version="canary")
        self._insert_old_task(task_id="tsk-s", conversation_id="cnv-s", version="stable")
        terminals = self.queue.fail_unavailable_versions(
            available_versions=("stable",), unavailable_for=timedelta(minutes=3)
        )
        self.assertEqual([item.task_id for item in terminals], ["tsk-c"])
        with connect(DSN) as connection:
            row = connection.execute(
                "SELECT status,target_worker_version FROM task WHERE id='tsk-c'"
            ).fetchone()
            self.assertEqual(row, ("awaiting_delivery", "canary"))
            self.assertEqual(
                connection.execute("SELECT status FROM task WHERE id='tsk-s'").fetchone()[0],
                "queued",
            )
        terminal_kind, error_kind, content = self._terminal_event("tsk-c")
        self.assertEqual(terminal_kind, "failed")
        self.assertEqual(error_kind, "worker_version_unavailable")
        self.assertEqual(
            content, default_content_catalog().text("worker.version_unavailable").text
        )
        # 没被判定不可用的 canary 版本本身不该凭空出现终态事件。
        self.assertIsNone(self._terminal_event("tsk-s"))

    def test_stale_safe_retry_then_exhaustion_writes_terminal_events_and_no_replay(
        self,
    ) -> None:
        self._insert_old_task(
            task_id="tsk-r", conversation_id="cnv-r", status="running", attempts=1
        )
        self.assertEqual(
            self.queue.reclaim_stale(older_than=timedelta(seconds=90)), ["tsk-r"]
        )
        # 安全重试的第一轮回到 queued，不写终态事件——不是这条路径要收口的对象。
        self.assertIsNone(self._terminal_event("tsk-r"))
        claimed = self.queue.claim(worker_id="worker-2", target_worker_version="stable")
        self.assertEqual(claimed[0].attempts, 2)
        with connect(DSN) as connection:
            with connection.transaction():
                connection.execute(
                    "UPDATE task SET heartbeat_at=now()-interval '2 minutes' WHERE id='tsk-r'"
                )
        self.assertEqual(self.queue.reclaim_stale(older_than=timedelta(seconds=90)), [])
        with connect(DSN) as connection:
            self.assertEqual(
                connection.execute("SELECT status FROM task WHERE id='tsk-r'").fetchone()[0],
                "awaiting_delivery",
            )
            # 重试耗尽是终态收口，话题继续占用直到投递解析。
            self.assertEqual(
                connection.execute(
                    "SELECT running_task_id FROM conversation WHERE id='cnv-r'"
                ).fetchone()[0],
                "tsk-r",
            )
        terminal_kind, error_kind, content = self._terminal_event("tsk-r")
        self.assertEqual(terminal_kind, "failed")
        self.assertEqual(error_kind, "retry_exhausted")
        self.assertEqual(content, default_content_catalog().text("worker.running_timeout").text)

        self._insert_old_task(
            task_id="tsk-x",
            conversation_id="cnv-x",
            status="running",
            side_effect_state="possible",
            attempts=1,
        )
        self.assertEqual(self.queue.reclaim_stale(older_than=timedelta(seconds=90)), [])
        with connect(DSN) as connection:
            self.assertEqual(
                connection.execute("SELECT status FROM task WHERE id='tsk-x'").fetchone()[0],
                "awaiting_delivery",
            )
        x_terminal_kind, x_error_kind, x_content = self._terminal_event("tsk-x")
        self.assertEqual(x_terminal_kind, "failed")
        self.assertEqual(x_error_kind, "side_effect_uncertain")
        self.assertEqual(
            x_content, default_content_catalog().text("worker.side_effect_uncertain").text
        )

    def test_listener_receives_committed_notify(self) -> None:
        assert DSN is not None
        with PostgresTaskQueueListener(DSN) as listener:
            with connect(DSN) as connection:
                with connection.transaction():
                    connection.execute("NOTIFY task_queued")
            self.assertTrue(listener.wait(timeout_seconds=2.0))

    def test_dropped_notify_is_recovered_by_polling_within_configured_bound(self) -> None:
        """V-队列-06：丢弃 NOTIFY 后仍在 poll_interval 内领取任务。"""

        assert DSN is not None
        listener = DroppingNotifyListener()
        executor_claimed = threading.Event()
        claimed_at: list[float] = []

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                claimed_at.append(time.monotonic())
                executor_claimed.set()
                return {
                    "turn": {"closed": True, "final_text": "轮询结果", "session_id": None},
                    "failure": None,
                }

        poll_interval = 0.05
        service = WorkerService(
            config=worker_config(
                worker_id="worker-poll",
                poll_interval_seconds=poll_interval,
                max_concurrency=1,
            ),
            queue=self.queue,
            executor_factory=lambda config, marker: Executor(),
            listener_factory=lambda: listener,
        )

        async def scenario() -> float:
            stop_event = asyncio.Event()
            consumer = asyncio.create_task(service.run(stop_event=stop_event))
            started = await asyncio.to_thread(listener.wait_started.wait, 2.0)
            self.assertTrue(started, "消费者应先进入等待，才能模拟通知丢失")
            queued_at = time.monotonic()
            pipeline = EventPipeline(
                store=PostgresGatewayStore(DSN),
                reactions=FakeReactions(CallLog()),
                replies=FakeReplies(CallLog()),
                audit=FakeAudit(CallLog()),
            )
            pipeline.handle_message(
                InboundMessage(
                    "evt-dropped-notify",
                    "im.message.receive_v1",
                    "ou-90",
                    "chat-poll",
                    "topic-poll",
                    "msg-poll",
                    "问题",
                    "trace-poll",
                )
            )
            claimed = await asyncio.to_thread(executor_claimed.wait, 2.0)
            self.assertTrue(claimed, "丢通知后消费者仍应通过轮询领取")
            stop_event.set()
            await asyncio.wait_for(consumer, timeout=2.0)
            return queued_at

        queued_at = asyncio.run(scenario())
        self.assertTrue(listener.wait_calls)
        self.assertTrue(claimed_at)
        self.assertLessEqual(
            claimed_at[0] - queued_at,
            # 0.2 → 0.3：Issue #151 给 _housekeep() 每轮多加了一次真实数据库往返
            # （expire_undelivered_terminals），在这条真库用例的调度余量上留出对应
            # 空间，仍然远小于用户可感知的延迟（V-队列-06 只关心"不会无限期悬挂"）。
            poll_interval + 0.3,
            "丢弃 NOTIFY 后领取不得超过配置轮询上限（含测试调度余量）",
        )
        self.assertEqual(
            self.queue.claim(worker_id="probe", target_worker_version="stable"),
            [],
            "任务已由消费者领取并完成，不应仍留在 queued",
        )

    def test_two_users_keep_session_and_delivery_scope_separate(self) -> None:
        """V-会话-07：不同用户的任务不会串用会话或投递定位。"""

        assert DSN is not None
        with connect(DSN) as connection:
            with connection.transaction():
                connection.execute(
                    """INSERT INTO app_user
                       (id, feishu_open_id, feishu_user_id, feishu_union_id,
                        display_name, department, tenant_key, provisioning_state)
                       VALUES ('usr-91','ou-91','u-91','un-91','李四','销售部','tk-91','active')"""
                )
        log = CallLog()
        pipeline = EventPipeline(
            store=PostgresGatewayStore(DSN),
            reactions=FakeReactions(log),
            replies=FakeReplies(log),
            audit=FakeAudit(log),
        )
        pipeline.handle_message(
            InboundMessage(
                "evt-user-a",
                "im.message.receive_v1",
                "ou-90",
                "chat-user-a",
                "topic-user-a",
                "msg-user-a",
                "答案 A",
                "trace-user-a",
            )
        )
        pipeline.handle_message(
            InboundMessage(
                "evt-user-b",
                "im.message.receive_v1",
                "ou-91",
                "chat-user-b",
                "topic-user-b",
                "msg-user-b",
                "答案 B",
                "trace-user-b",
            )
        )

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {"closed": True, "final_text": prompt, "session_id": None},
                    "failure": None,
                }

        service = WorkerService(
            config=worker_config(max_concurrency=2),
            queue=self.queue,
            executor_factory=lambda config, marker: Executor(),
        )
        asyncio.run(service.process_once())

        # Issue #151：Worker 不再持有出站 transport，投递意图只落在
        # ``task_delivery_event``。按用户回读各自的终态事件与话题定位，验证
        # 两个用户互不串用会话或投递定位（V-会话-07）。
        with connect(DSN) as connection:
            rows = connection.execute(
                """
                SELECT t.user_id, c.feishu_chat_id, c.feishu_thread_id, e.content, t.status
                  FROM task_delivery_event AS e
                  JOIN task AS t ON t.id = e.task_id
                  JOIN conversation AS c ON c.id = t.conversation_id
                 WHERE e.event_type = 'terminal'
                 ORDER BY t.user_id
                """
            ).fetchall()
        by_user = {row[0]: row for row in rows}
        self.assertEqual(set(by_user), {"usr-90", "usr-91"})
        self.assertEqual(
            (by_user["usr-90"][1], by_user["usr-90"][2]), ("chat-user-a", "topic-user-a")
        )
        self.assertEqual(
            (by_user["usr-91"][1], by_user["usr-91"][2]), ("chat-user-b", "topic-user-b")
        )
        self.assertEqual(by_user["usr-90"][3], "答案 A")
        self.assertEqual(by_user["usr-91"][3], "答案 B")
        self.assertEqual(by_user["usr-90"][4], "awaiting_delivery")
        self.assertEqual(by_user["usr-91"][4], "awaiting_delivery")

    def test_context_too_long_suggests_new_without_replacing_agent_session(self) -> None:
        """上下文超限提示 /new，原 agent_session_id 保持不变；Issue #151 起失败
        终态也进入 ``awaiting_delivery`` 并继续占用话题，不再立即释放——释放要等
        投递解析（confirm_delivery / expire_undelivered_terminals，见
        ``tests/test_delivery_outbox.py``）。"""

        assert DSN is not None
        pipeline = EventPipeline(
            store=PostgresGatewayStore(DSN),
            reactions=FakeReactions(CallLog()),
            replies=FakeReplies(CallLog()),
            audit=FakeAudit(CallLog()),
        )
        outcome = pipeline.handle_message(
            InboundMessage(
                "evt-context-too-long",
                "im.message.receive_v1",
                "ou-90",
                "chat-context",
                "topic-context",
                "msg-context",
                "继续追问",
                "trace-context",
            )
        )
        assert outcome.task_id is not None
        conversation_id = self._scalar(
            "SELECT conversation_id FROM task WHERE id=%s", (outcome.task_id,)
        )
        self.execute(
            "UPDATE conversation SET agent_session_id='session-original' WHERE id=%s",
            (conversation_id,),
        )

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {"closed": False, "final_text": "", "session_id": None},
                    "failure": {"code": "context_too_long"},
                }

        service = WorkerService(
            config=worker_config(worker_id="worker-context"),
            queue=self.queue,
            executor_factory=lambda config, marker: Executor(),
        )
        asyncio.run(service.process_once())

        self.assertEqual(self._scalar("SELECT status FROM task"), "awaiting_delivery")
        terminal = self._scalar(
            "SELECT content FROM task_delivery_event WHERE event_type='terminal'"
        )
        self.assertIn("/new", terminal)
        self.assertEqual(
            self._scalar("SELECT error_kind FROM task"), "context_too_long"
        )
        self.assertEqual(
            self._scalar(
                "SELECT agent_session_id FROM conversation WHERE id=%s", (conversation_id,)
            ),
            "session-original",
        )
        # 失败终态同样进入 awaiting_delivery，话题继续占用直到投递解析——本
        # Story 的状态合同第 2 条明确覆盖失败/停止/超时/withheld，不只是成功路径。
        self.assertIsNotNone(self._scalar("SELECT running_task_id FROM conversation"))

    def test_claim_context_keeps_reply_scope_and_finish_persists_session(self) -> None:
        assert DSN is not None
        pipeline = EventPipeline(
            store=PostgresGatewayStore(DSN),
            reactions=FakeReactions(CallLog()),
            replies=FakeReplies(CallLog()),
            audit=FakeAudit(CallLog()),
        )
        outcome = pipeline.handle_message(
            InboundMessage(
                "evt-session",
                "im.message.receive_v1",
                "ou-90",
                "chat-session",
                "topic-session",
                "msg-session",
                "问题",
                "trace-session",
            )
        )
        claimed = self.queue.claim(worker_id="worker-session", target_worker_version="stable")
        self.assertEqual(claimed[0].task_id, outcome.task_id)
        context = self.queue.task_context(
            task_id=claimed[0].task_id, worker_id="worker-session"
        )
        assert context is not None
        self.assertEqual(context.reply_to_message_id, "msg-session")
        self.assertTrue(
            self.queue.finish(
                task_id=claimed[0].task_id,
                conversation_id=claimed[0].conversation_id,
                status="succeeded",
                worker_id="worker-session",
                agent_session_id="session-saved",
            )
        )
        with connect(DSN) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT agent_session_id,running_task_id FROM conversation WHERE id=%s",
                    (claimed[0].conversation_id,),
                ).fetchone(),
                ("session-saved", None),
            )

    def test_finish_queues_the_overwritten_agent_session_for_cleanup(self) -> None:
        """PR #173 独立复核 P2-4：``finish()`` 用
        ``agent_session_id = COALESCE(%s, agent_session_id)`` 写回，旧值被新值
        覆盖时，此前三个既有触发点（``/new``、空闲到点、停用/权限变化）都不会把
        这个旧 session id 排队做物理清理——话题闲置未满两小时就被新一轮任务
        覆盖，或真实 CLI 的 ``--resume`` 返回了新 session id，都会让旧的 JSONL
        永久留在磁盘上。

        用一个已经带着 ``agent_session_id='old-session'`` 的会话验证：新任务
        ``finish(agent_session_id='new-session')`` 之后，``old-session`` 必须
        出现在 ``agent_session_cleanup`` 里、原因是 ``session_overwritten``，
        而 ``new-session`` 不应该被排队（它是活跃会话，不该被清理）。
        """

        self._insert_old_task(task_id="tsk-overwrite", conversation_id="cnv-overwrite")
        self.execute(
            "UPDATE conversation SET agent_session_id='old-session' WHERE id='cnv-overwrite'"
        )

        claimed = self.queue.claim(worker_id="worker-overwrite", target_worker_version="stable")
        self.assertEqual(claimed[0].conversation_id, "cnv-overwrite")

        finished = self.queue.finish(
            task_id=claimed[0].task_id,
            conversation_id="cnv-overwrite",
            status="succeeded",
            worker_id="worker-overwrite",
            agent_session_id="new-session",
        )
        self.assertTrue(finished)

        with connect(DSN) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT agent_session_id FROM conversation WHERE id='cnv-overwrite'"
                ).fetchone()[0],
                "new-session",
            )
            row = connection.execute(
                "SELECT reason FROM agent_session_cleanup WHERE agent_session_id='old-session'"
            ).fetchone()
            self.assertIsNotNone(
                row, "被覆盖的旧 session id 必须被排队做物理清理，否则永久留在磁盘上"
            )
            self.assertEqual(row[0], "session_overwritten")
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM agent_session_cleanup WHERE agent_session_id='new-session'"
                ).fetchone(),
                "刚写入的新 session id 是活跃会话，不该被排队清理",
            )

    def test_enqueue_failure_has_one_catalog_notice_and_reprocesses_after_recovery(self) -> None:
        assert DSN is not None
        log = CallLog()
        replies = FakeReplies(log)
        pipeline = EventPipeline(
            store=PostgresGatewayStore(DSN),
            reactions=FakeReactions(log),
            replies=replies,
            audit=FakeAudit(log),
        )
        message = InboundMessage(
            "evt-queue-failure",
            "im.message.receive_v1",
            "ou-90",
            "chat-90",
            "topic-90",
            "msg-queue-failure",
            "问题",
            "trace-queue-failure",
        )
        original = _Transaction.insert_task

        def fail_insert(self, **kwargs: object) -> None:
            raise RuntimeError("fault injection")

        _Transaction.insert_task = fail_insert  # type: ignore[assignment]
        try:
            first = pipeline.handle_message(message)
            second = pipeline.handle_message(message)
        finally:
            _Transaction.insert_task = original

        self.assertIsNone(first.handled_as)
        self.assertIsNone(second.handled_as)
        self.assertEqual(log.count("reply.send_text"), 1)
        self.assertIn("LX-QUEUE-001", log.fields("reply.send_text")[0]["text"])
        self.assertEqual(
            pipeline.handle_message(message).handled_as.value,
            "task_queued",
            "失败事务没有落 inbound_event，故故障恢复后重投必须能够完整入队",
        )


@unittest.skipUnless(POSTGRES_READY, SKIP_DB)
class SessionCleanupPipelineIntegrationTests(unittest.TestCase):
    """Issue #153：从"数据库里排了一条待清理"到"物理文件真的被删、行被标记完成"
    的完整链路——真库 + 真实临时目录，不在任何一段打桩。三个触发点各自排队的
    正确性已在 ``tests/test_delivery_outbox.py`` 的 ``AgentSessionCleanupQueueTests``
    覆盖，本文件只覆盖 ``WorkerService._cleanup_agent_sessions`` 这一段消费。
    """

    @classmethod
    def setUpClass(cls) -> None:
        assert DSN is not None
        ensure_production_schema(DSN)

    def setUp(self) -> None:
        assert DSN is not None
        reset_production_rows(DSN)
        self.queue = PostgresTaskQueue(DSN)
        with connect(DSN) as connection:
            with connection.transaction():
                connection.execute(
                    """INSERT INTO app_user
                       (id, feishu_open_id, feishu_user_id, feishu_union_id,
                        display_name, department, tenant_key, provisioning_state)
                       VALUES ('usr-90','ou-90','u-90','un-90','张三','数据部','tk-90','active')"""
                )

    def _queue_cleanup(self, *, cleanup_id: str, agent_session_id: str, reason: str = "new_command") -> None:
        with connect(DSN) as connection:
            with connection.transaction():
                connection.execute(
                    """INSERT INTO agent_session_cleanup (id, user_id, agent_session_id, reason)
                       VALUES (%s, 'usr-90', %s, %s)""",
                    (cleanup_id, agent_session_id, reason),
                )

    def test_deletes_the_matching_file_and_marks_the_row_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_root = Path(tmp)
            jsonl = session_root / "01J00000000000000000000SESS.jsonl"
            jsonl.write_text("{}", encoding="utf-8")
            self._queue_cleanup(cleanup_id="asc-int-1", agent_session_id="01J00000000000000000000SESS")

            service = WorkerService(
                config=worker_config(),
                queue=self.queue,
                session_root=session_root,
            )
            service._cleanup_agent_sessions()

            self.assertFalse(jsonl.exists())
            with connect(DSN) as connection:
                done = connection.execute(
                    "SELECT done_at IS NOT NULL FROM agent_session_cleanup WHERE id='asc-int-1'"
                ).fetchone()[0]
            self.assertTrue(done)

    def test_a_cleanup_for_a_session_that_never_produced_a_file_is_still_marked_done(self) -> None:
        """任务在建会话前就失败，从未真正落过盘——这不是清理失败，必须照常
        标记完成，不能让一条永远匹配不到文件的记录卡住队列。"""

        with tempfile.TemporaryDirectory() as tmp:
            self._queue_cleanup(cleanup_id="asc-int-2", agent_session_id="01J00000000000000000NEVER")

            service = WorkerService(
                config=worker_config(),
                queue=self.queue,
                session_root=Path(tmp),
            )
            service._cleanup_agent_sessions()

            with connect(DSN) as connection:
                done = connection.execute(
                    "SELECT done_at IS NOT NULL FROM agent_session_cleanup WHERE id='asc-int-2'"
                ).fetchone()[0]
            self.assertTrue(done)

    def test_without_a_configured_session_root_the_queue_is_left_untouched(self) -> None:
        """没有可用会话根目录时整体跳过——不半途认领又做不了事（模块说明）。"""

        self._queue_cleanup(cleanup_id="asc-int-3", agent_session_id="01J00000000000000000SKIP")

        service = WorkerService(config=worker_config(), queue=self.queue, session_root=None)
        service._cleanup_agent_sessions()

        with connect(DSN) as connection:
            row = connection.execute(
                "SELECT claimed_at, done_at FROM agent_session_cleanup WHERE id='asc-int-3'"
            ).fetchone()
        self.assertIsNone(row[0])
        self.assertIsNone(row[1])

    def test_a_configured_but_nonexistent_session_root_is_not_marked_done(self) -> None:
        """PR #173 独立复核 P2-6：``LINGXI_WORKER_SESSION_ROOT`` **配置了**、但指向
        一个不存在的目录（照抄旧版 ``.env.example`` 的示例值就会这样，镜像固定
        ``HOME=/tmp``），与"根目录存在、这个会话确实没有文件"必须是两种不同的
        结果。前者是"这次没法处理"，理应留给下一次十分钟软窗口重试；后者才是
        "已确认无事可做"。改动前两者被合并成同一个 `mark_done` 分支——
        `agent_session_cleanup.agent_session_id` 是唯一索引 + `ON CONFLICT DO
        NOTHING`，一旦被标记完成就再也不会被重新排队，事后把配置改对也补不回来。
        """

        self._queue_cleanup(cleanup_id="asc-int-5", agent_session_id="01J00000000000000NOTREAL")

        with tempfile.TemporaryDirectory() as tmp:
            missing_root = Path(tmp) / "does-not-exist"
            self.assertFalse(missing_root.exists())

            service = WorkerService(
                config=worker_config(), queue=self.queue, session_root=missing_root
            )
            service._cleanup_agent_sessions()

        with connect(DSN) as connection:
            row = connection.execute(
                "SELECT claimed_at, done_at FROM agent_session_cleanup WHERE id='asc-int-5'"
            ).fetchone()
        self.assertIsNotNone(
            row[0], "这一条应该已经被认领——不该半途放弃到连 claimed_at 都不写"
        )
        self.assertIsNone(
            row[1],
            "根目录不存在时不能标记完成：事后把配置改对也补不回一条已经被"
            "标记完成的行（唯一索引 + ON CONFLICT DO NOTHING）",
        )

    def test_runs_end_to_end_through_process_once_via_the_housekeep_round(self) -> None:
        """确认接线到了真正的入口——``process_once()`` 而不是只有直接调用私有
        方法才生效（否则装配一处没接对，生产环境永远不会真正清理）。"""

        with tempfile.TemporaryDirectory() as tmp:
            session_root = Path(tmp)
            jsonl = session_root / "01J00000000000000000PROCE.jsonl"
            jsonl.write_text("{}", encoding="utf-8")
            self._queue_cleanup(cleanup_id="asc-int-4", agent_session_id="01J00000000000000000PROCE")

            service = WorkerService(
                config=worker_config(),
                queue=self.queue,
                session_root=session_root,
            )
            asyncio.run(service.process_once())

            self.assertFalse(jsonl.exists())


class SystemPromptFileTests(unittest.TestCase):
    """默认提示词文件（2026-08-23，产品负责人裁定：提示词不进代码、不进镜像，
    随时可改、快速验证）：worker **每任务现读**文件，编辑后下一条消息即生效；
    读不到时该任务降级为无提示词执行（提示词是行为调优，不是安全屏障，屏障是
    PreToolUse 白名单）；每轮的提示词摘要随 ``worker.task.terminal`` 可追溯。"""

    def _service(self, prompt_path: str, sink=None):
        queue = FakeWorkerQueue()
        captured: list[WorkerConfig] = []

        class Executor:
            def __init__(self, config: WorkerConfig) -> None:
                captured.append(config)

            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {"closed": True, "final_text": "结果", "session_id": None},
                    "failure": None,
                }

        service = WorkerService(
            config=worker_config(system_prompt_file=prompt_path),
            queue=queue,
            executor_factory=lambda config, marker: Executor(config),
            on_terminal_outcome=sink,
        )
        return service, queue, captured

    def _rearm(self, queue: FakeWorkerQueue, task_id: str) -> None:
        queue.claimed = ClaimedTask(
            task_id=task_id,
            conversation_id="cnv-1",
            user_id="usr-1",
            prompt="问题",
            resumed_session=True,
            target_worker_version="stable",
            attempts=1,
            reply_to_message_id="msg-1",
            stop_requested=False,
        )

    def test_the_file_is_read_per_task_so_an_edit_takes_effect_on_the_next_message(self) -> None:
        """机制的全部意义所在：改文件不改进程。同一个常驻 service、不重启，
        两个任务分别拿到编辑前后的两版提示词。"""

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "system_prompt.md")
            Path(path).write_text("第一版：回答必须注明查询的时间范围。", encoding="utf-8")
            service, queue, captured = self._service(path)
            asyncio.run(service.process_once())
            self.assertEqual(captured[0].system_prompt, "第一版：回答必须注明查询的时间范围。")

            Path(path).write_text("第二版：查询前必须先确认指标的可用维度。", encoding="utf-8")
            self._rearm(queue, "tsk-2")
            asyncio.run(service.process_once())
            self.assertEqual(captured[1].system_prompt, "第二版：查询前必须先确认指标的可用维度。")

    def test_a_missing_file_degrades_to_no_prompt_and_the_task_still_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "absent.md")
            sink = RecordingTerminalOutcomeSink()
            service, queue, captured = self._service(path, sink=sink)
            asyncio.run(service.process_once())

        self.assertIsNone(captured[0].system_prompt, "读不到文件时本任务以无提示词执行")
        self.assertEqual(queue.terminals[0]["terminal_kind"], "success", "降级不得带走任务")
        self.assertIsNone(sink.calls[0]["system_prompt_digest"])

    def test_the_digest_of_the_served_prompt_lands_in_the_terminal_audit_event(self) -> None:
        """「用户这一轮用的哪版提示词」的追溯依据；只记摘要，正文不进审计。"""

        import hashlib as _hashlib

        prompt = "查询纪律：必须限定时间范围与必要维度。"
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "system_prompt.md")
            Path(path).write_text(prompt, encoding="utf-8")
            sink = RecordingTerminalOutcomeSink()
            service, queue, _ = self._service(path, sink=sink)
            asyncio.run(service.process_once())

        expected = _hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
        self.assertEqual(sink.calls[0]["system_prompt_digest"], expected)
        self.assertNotIn(prompt, str(sink.calls[0]), "提示词正文不得进入低敏审计事件")
        # 摘要断言必须与「任务真的成功」并联：首版实现里 replace() 重跑
        # __post_init__ 炸掉任务，摘要仍会随失败终态如期出现，光看摘要会假绿。
        self.assertEqual(queue.terminals[0]["terminal_kind"], "success")

    def test_a_prompt_colliding_with_a_fixed_terminal_text_is_dropped_for_the_task(self) -> None:
        """出口安全会把提示词逐句派生成禁词：提示词若含固定终态文案，空产出回合
        的终态自检会抛异常、炸掉「总是返回一份报告」的契约。现读时预演一遍，坏
        提示词只废掉自己（降级），不废掉回合。"""

        from lingxi.core.execution.input_safety import SAFE_OUTPUT_FALLBACK

        prompt = f"规则一：必须限定时间范围。\n{SAFE_OUTPUT_FALLBACK}\n规则二：不得编造数据。"
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "system_prompt.md")
            Path(path).write_text(prompt, encoding="utf-8")
            sink = RecordingTerminalOutcomeSink()
            service, queue, captured = self._service(path, sink=sink)
            asyncio.run(service.process_once())

        self.assertIsNone(captured[0].system_prompt, "与固定终态文案互相命中的提示词必须整体弃用")
        self.assertEqual(queue.terminals[0]["terminal_kind"], "success")
        self.assertIsNone(sink.calls[0]["system_prompt_digest"])

    def test_an_oversized_file_is_refused_rather_than_stuffed_into_every_context(self) -> None:
        """必须用**单字节**内容并锁定原因码（二级独立审查 2026-08-23 P2-2 变异
        实测）：多字节内容在有界读取下会被截成非法 UTF-8、走 not_utf8 降级，
        长度守卫删掉了用例照样绿——ASCII 截断后是合法文本，唯一能拦住它的就是
        长度守卫本身；只断言 ``system_prompt is None`` 同样锁不住，必须断言
        降级原因确实是 oversized。"""

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "system_prompt.md")
            Path(path).write_text("x" * (64 * 1024 + 1), encoding="ascii")
            service, queue, captured = self._service(path)
            with self.assertLogs("lingxi.apps.worker.service", level="WARNING") as logs:
                asyncio.run(service.process_once())

        self.assertIsNone(captured[0].system_prompt)
        self.assertEqual(queue.terminals[0]["terminal_kind"], "success")
        self.assertTrue(
            any("reason=oversized" in line for line in logs.output),
            f"降级原因必须是 oversized，实际告警：{logs.output}",
        )

    def test_the_digest_is_kept_on_a_failed_turn_as_the_attempted_version(self) -> None:
        """字段口径的另一半（二级独立审查 2026-08-23 P3-1）：「本轮选定并交给
        执行器装配的版本」——装配后失败的回合带着摘要落失败终态，它回答的是
        "失败那一轮试图使用哪版"。"""

        import hashlib as _hashlib

        prompt = "查询纪律：必须限定时间范围。"
        queue = FakeWorkerQueue()
        sink = RecordingTerminalOutcomeSink()

        class FailingExecutor:
            def __init__(self, config: WorkerConfig) -> None:
                pass

            async def run_turn(self, prompt_text: str, **kwargs: object) -> dict:
                raise RuntimeError("装配后失败")

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "system_prompt.md")
            Path(path).write_text(prompt, encoding="utf-8")
            service = WorkerService(
                config=worker_config(system_prompt_file=path),
                queue=queue,
                executor_factory=lambda config, marker: FailingExecutor(config),
                on_terminal_outcome=sink,
            )
            asyncio.run(service.process_once())

        self.assertEqual(queue.terminals[0]["terminal_kind"], "failed")
        self.assertEqual(
            sink.calls[0]["system_prompt_digest"],
            _hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12],
        )

    def test_a_symlink_is_refused_even_when_it_points_at_a_readable_file(self) -> None:
        """外部独立审查 2026-08-23 P1-2：worker 同时挂着含用户 MCP 令牌的用户
        环境卷，一个指向 .mcp.json 的符号链接会把凭据喂进模型上下文，而出口安全
        撤不回已发送的系统提示。O_NOFOLLOW 结构性拒绝，不区分链接指向什么。"""

        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "looks-innocent.md")
            Path(target).write_text("正常内容", encoding="utf-8")
            link = os.path.join(directory, "system_prompt.md")
            os.symlink(target, link)
            service, queue, captured = self._service(link)
            asyncio.run(service.process_once())

        self.assertIsNone(captured[0].system_prompt, "符号链接必须被拒绝而不是被跟随")
        self.assertEqual(queue.terminals[0]["terminal_kind"], "success")

    def test_a_fifo_does_not_block_the_worker(self) -> None:
        """外部独立审查 2026-08-23 P1-3：对 FIFO 的普通 open 会无限阻塞——心跳
        与停止处理一起停摆。O_NONBLOCK + 普通文件校验把它变成一次立即降级。"""

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "system_prompt.md")
            os.mkfifo(path)
            service, queue, captured = self._service(path)
            asyncio.run(asyncio.wait_for(service.process_once(), timeout=10))

        self.assertIsNone(captured[0].system_prompt)


class ContentCaptureWiringTests(unittest.TestCase):
    """内测轮内容级采集的写入点（Issue #251/#304 批次 3，`_capture_content_if_enabled`）。

    默认（不传 ``content_capture_writer``）时行为必须与本文件其余全部既有测试
    的 fake ``Executor`` 完全兼容——那些类都**没有**定义 ``build_content_capture_record``
    方法，如果关闭状态下仍然调用它，整个文件会因 ``AttributeError`` 集体报错。
    本组第一条用例把这条隐含前提显式断言出来（`test_default_disabled_never_
    touches_the_executor_capture_hook` 就是 V-采集-01/02 的变异验红锚点）。
    """

    def _sample_record(self, *, task_id: str = "tsk-1", worker_id: str = "worker-test") -> ContentCaptureRecord:
        return ContentCaptureRecord(
            task_id=task_id,
            worker_id=worker_id,
            question_content="问题",
            question_redaction_count=0,
            answer_content="结果",
            answer_redaction_count=0,
            tool_calls=(),
        )

    def test_default_disabled_never_touches_the_executor_capture_hook(self) -> None:
        """默认关闭可被断言证明：不配置 ``content_capture_writer`` 时，
        ``executor.build_content_capture_record`` 从未被调用——用一个**压根不
        定义**该方法的 Executor 证明，调用了就会 ``AttributeError``。

        **变异验红**：把 ``WorkerService._capture_content_if_enabled`` 的守卫条件
        从 ``executor is None or self._content_capture_writer is None`` 改成
        只判 ``executor is None``（即默认也尝试调用），本用例必须变红
        （``AttributeError: 'Executor' object has no attribute
        'build_content_capture_record'``）。
        """

        queue = FakeWorkerQueue()

        class Executor:  # 刻意不定义 build_content_capture_record
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                kwargs["on_stream_event"]({"kind": "assistant_message"})  # type: ignore[index]
                return {
                    "turn": {"closed": True, "final_text": "结果", "session_id": "s1"},
                    "failure": None,
                }

        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
        )

        asyncio.run(service.process_once())  # 不抛 AttributeError 即通过

        self.assertEqual(queue.terminals[0]["terminal_kind"], "success")

    def test_writer_receives_the_record_built_by_the_executor_when_capture_enabled(self) -> None:
        queue = FakeWorkerQueue()
        record = self._sample_record()
        received: list[ContentCaptureRecord] = []
        build_calls: list[dict[str, object]] = []

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {"closed": True, "final_text": "结果", "session_id": "s1"},
                    "failure": None,
                }

            def build_content_capture_record(self, **kwargs: object) -> ContentCaptureRecord:
                build_calls.append(kwargs)
                return record

        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
            content_capture_writer=received.append,
        )
        asyncio.run(service.process_once())

        self.assertEqual(received, [record])
        # question 必须来自这次任务真实的 context.prompt（FakeWorkerQueue 固定
        # 为 "问题"），task_id/worker_id 必须来自这次任务本身，不是随便传的值。
        self.assertEqual(
            build_calls,
            [{"task_id": "tsk-1", "worker_id": "worker-test", "question": "问题"}],
        )
        self.assertEqual(queue.terminals[0]["terminal_kind"], "success")

    def test_writer_is_not_called_when_the_executor_reports_nothing_to_capture(self) -> None:
        """``build_content_capture_record`` 返回 ``None``（执行器自己判断这次没有
        可采集内容，见 ``WorkerTurnExecutor`` 未开启 ``capture_raw_content`` 时的
        真实行为）时，写入方不得被调用——不能拿一个 ``None`` 硬写进数据库。"""

        queue = FakeWorkerQueue()
        received: list[ContentCaptureRecord] = []

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {"closed": True, "final_text": "结果", "session_id": "s1"},
                    "failure": None,
                }

            def build_content_capture_record(self, **kwargs: object) -> None:
                return None

        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
            content_capture_writer=received.append,
        )
        asyncio.run(service.process_once())

        self.assertEqual(received, [])

    def test_capture_failure_does_not_affect_the_task_terminal_outcome(self) -> None:
        """结构约束「采集失败不影响任务主流程」：写入方抛异常时，真实的回合
        结果（成功、含正文、含 session_id）必须原样收口，不受采集失败牵连。"""

        queue = FakeWorkerQueue()
        record = self._sample_record()

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {"closed": True, "final_text": "结果", "session_id": "s1"},
                    "failure": None,
                }

            def build_content_capture_record(self, **kwargs: object) -> ContentCaptureRecord:
                return record

        def failing_writer(record: ContentCaptureRecord) -> None:
            raise RuntimeError("模拟数据库写入失败")

        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
            content_capture_writer=failing_writer,
        )
        asyncio.run(service.process_once())  # 不得向上抛出 RuntimeError

        terminal = queue.terminals[0]
        self.assertEqual(terminal["terminal_kind"], "success")
        self.assertEqual(terminal["content"], "结果")
        self.assertEqual(terminal["agent_session_id"], "s1")

    def test_capture_is_attempted_even_when_the_turn_itself_failed(self) -> None:
        """失败/超时回合同样值得采集——问题原文与已尝试的工具调用是"以日志分析
        缺陷"要看的信号，不只是成功回合才有采集价值。"""

        queue = FakeWorkerQueue()
        record = self._sample_record()
        received: list[ContentCaptureRecord] = []

        class FailingExecutor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                raise RuntimeError("装配后失败")

            def build_content_capture_record(self, **kwargs: object) -> ContentCaptureRecord:
                return record

        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: FailingExecutor(),
            content_capture_writer=received.append,
        )
        asyncio.run(service.process_once())

        self.assertEqual(queue.terminals[0]["terminal_kind"], "failed")
        self.assertEqual(received, [record])

    def test_capture_is_skipped_when_the_executor_was_never_constructed(self) -> None:
        """Epic D 闸⑥红线路径（读不到用户自己的 MCP 配置）从不构造 executor；
        采集必须随之跳过，不得因为 ``executor is None`` 而抛异常。"""

        queue = FakeWorkerQueue()
        received: list[ContentCaptureRecord] = []
        executor_calls: list[None] = []
        unused_record = self._sample_record()

        class Executor:
            def __init__(self) -> None:
                executor_calls.append(None)

            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {"closed": True, "final_text": "不该被用到", "session_id": None},
                    "failure": None,
                }

            def build_content_capture_record(self, **kwargs: object) -> ContentCaptureRecord:
                # 本用例断言这个方法从未被调用；给一个可用的返回值只是防止
                # "万一真的被调用了" 时报错混淆了断言失败的真正原因。
                return unused_record

        with tempfile.TemporaryDirectory() as empty_root:
            service = WorkerService(
                config=worker_config(user_env_root=empty_root),
                queue=queue,
                executor_factory=lambda config, marker: Executor(),
                content_capture_writer=received.append,
            )
            asyncio.run(service.process_once())

        self.assertEqual(executor_calls, [])
        self.assertEqual(received, [])
        self.assertEqual(queue.terminals[0]["terminal_kind"], "failed")


class YearGroundingSuspectAlertTests(unittest.TestCase):
    """年份接地护栏第二层的 worker 侧接线（Issue #326，批次 5 卡 E）。

    覆盖 ``WorkerService._capture_content_if_enabled``/``_check_year_grounding_
    suspect`` 这一层的组装：检测到位、告警经既有 ``on_year_grounding_suspect``
    出口发出、检测异常不传染任务终态或内容采集写入。纯逻辑判定本身（词表命中、
    年份提取、三条件与、已知边界）由 ``tests/test_year_grounding_guard.py`` 覆盖，
    这里不重复断言判定细节，只用一次"全 2025 查询"的正例与三次否定用例核对接线。
    """

    def _record_with_query(
        self,
        *,
        task_id: str = "tsk-1",
        question: str,
        start_date: str | None = "2025-01-01",
        end_date: str | None = "2025-08-25",
        with_query_call: bool = True,
    ) -> ContentCaptureRecord:
        tool_calls: tuple[CapturedToolCall, ...] = ()
        if with_query_call:
            tool_input: dict[str, object] = {}
            if start_date is not None:
                tool_input["start_date"] = start_date
            if end_date is not None:
                tool_input["end_date"] = end_date
            tool_calls = (
                CapturedToolCall(
                    tool_use_id="call-1",
                    tool_name=QUERY_METRIC_TOOL_NAME,
                    tool_input=tool_input,
                    result_summary={"result_kind": "success", "allowed": True, "executed": True},
                    redaction_count=0,
                ),
            )
        return ContentCaptureRecord(
            task_id=task_id,
            worker_id="worker-test",
            question_content=question,
            question_redaction_count=0,
            answer_content="结果",
            answer_redaction_count=0,
            tool_calls=tool_calls,
        )

    def _run_with_record(
        self,
        record: ContentCaptureRecord,
        *,
        on_year_grounding_suspect: Any = None,
        content_capture_writer: Any = None,
    ) -> "FakeWorkerQueue":
        queue = FakeWorkerQueue()

        class Executor:
            async def run_turn(self, prompt: str, **kwargs: object) -> dict:
                return {
                    "turn": {"closed": True, "final_text": "结果", "session_id": "s1"},
                    "failure": None,
                }

            def build_content_capture_record(self, **kwargs: object) -> ContentCaptureRecord:
                return record

        service = WorkerService(
            config=worker_config(),
            queue=queue,
            executor_factory=lambda config, marker: Executor(),
            content_capture_writer=(
                content_capture_writer if content_capture_writer is not None else (lambda _r: None)
            ),
            on_year_grounding_suspect=on_year_grounding_suspect,
        )
        asyncio.run(service.process_once())
        return queue

    def test_alert_fires_for_relative_wording_with_all_non_current_year_queries(self) -> None:
        """①正例：相对时间问句 + 全部查询年份都不是当前年份（2025，固定写死的
        过去年份，恒不等于任何真实运行时的当前年份）→ 告警必发，且携带
        task_id/命中词/查询年份，不携带问句与答案正文。"""

        record = self._record_with_query(
            task_id="tsk-year-suspect",
            question="最近，尤其是7月之后数据下滑得厉害",
            start_date="2025-01-01",
            end_date="2025-08-25",
        )
        received: list[Mapping[str, object]] = []
        queue = self._run_with_record(record, on_year_grounding_suspect=received.append)

        self.assertEqual(len(received), 1)
        fields = received[0]
        self.assertEqual(fields["task_id"], "tsk-year-suspect")
        self.assertEqual(fields["matched_relative_time_terms"], ["最近", "7月之后"])
        self.assertEqual(fields["query_years"], [2025])
        self.assertNotIn("question", fields)
        self.assertNotIn("answer", fields)
        self.assertNotIn("question_content", fields)
        self.assertNotIn("answer_content", fields)
        # 检测是旁路：答案照常投递，不拦截。
        self.assertEqual(queue.terminals[0]["terminal_kind"], "success")
        self.assertEqual(queue.terminals[0]["content"], "结果")

    def test_no_alert_when_a_query_year_is_the_current_year(self) -> None:
        """②-a 否定：相对时间问句命中，但查询年份含真实当前年份 → 零告警。"""

        current_year = datetime.now().year
        record = self._record_with_query(
            question="最近的数据表现怎么样",
            start_date=f"{current_year}-01-01",
            end_date=f"{current_year}-08-25",
        )
        received: list[Mapping[str, object]] = []
        self._run_with_record(record, on_year_grounding_suspect=received.append)

        self.assertEqual(received, [])

    def test_no_alert_without_relative_time_wording(self) -> None:
        """②-b 否定：全部查询年份都不是当前年份，但问句没有相对时间表述 →
        零告警。"""

        record = self._record_with_query(
            question="查一下2025年1月到8月的充值数据",
            start_date="2025-01-01",
            end_date="2025-08-25",
        )
        received: list[Mapping[str, object]] = []
        self._run_with_record(record, on_year_grounding_suspect=received.append)

        self.assertEqual(received, [])

    def test_no_alert_with_zero_queries(self) -> None:
        """②-c 否定：相对时间问句命中，但本任务零次 query_metric 调用 → 零告警。"""

        record = self._record_with_query(question="最近的数据表现怎么样", with_query_call=False)
        received: list[Mapping[str, object]] = []
        self._run_with_record(record, on_year_grounding_suspect=received.append)

        self.assertEqual(received, [])

    def test_default_disabled_never_calls_the_pure_detector(self) -> None:
        """默认（不传 ``on_year_grounding_suspect``）时整体跳过——用
        ``unittest.mock.patch`` 直接证明 ``detect_year_grounding_suspect`` 从未
        被调用，不只是"调用了但没人处理"。即使这次任务的问句/查询组合本该判定
        可疑，没有装配告警出口就没有检测，与 ``_on_terminal_outcome``/
        ``_content_capture_writer`` 同一姿态（Issue #326 批次 5 卡 E）。
        """

        from unittest.mock import patch

        record = self._record_with_query(
            question="最近，尤其是7月之后数据下滑得厉害",
            start_date="2025-01-01",
            end_date="2025-08-25",
        )
        with patch("lingxi.apps.worker.service.detect_year_grounding_suspect") as mock_detect:
            queue = self._run_with_record(record)  # 不传 on_year_grounding_suspect

        mock_detect.assert_not_called()
        self.assertEqual(queue.terminals[0]["terminal_kind"], "success")

    def test_detector_exception_does_not_affect_task_terminal_or_content_capture(self) -> None:
        """③防御用例：告警出口异常时，任务终态与内容采集写入均不受影响，异常
        不得向上传播（检测代码异常不得影响任务终态——包一层防御并留结构化
        日志）。"""

        record = self._record_with_query(
            question="最近，尤其是7月之后数据下滑得厉害",
            start_date="2025-01-01",
            end_date="2025-08-25",
        )

        def _raising_sink(_fields: Mapping[str, object]) -> None:
            raise RuntimeError("模拟告警出口异常")

        captured: list[ContentCaptureRecord] = []
        queue = self._run_with_record(
            record,
            on_year_grounding_suspect=_raising_sink,
            content_capture_writer=captured.append,
        )  # 不得向上抛出 RuntimeError

        terminal = queue.terminals[0]
        self.assertEqual(terminal["terminal_kind"], "success")
        self.assertEqual(terminal["content"], "结果")
        self.assertEqual(terminal["agent_session_id"], "s1")
        # 检测/告警失败不影响内容采集本身已经成功写入。
        self.assertEqual(captured, [record])

    def test_detector_exception_inside_pure_logic_does_not_affect_task_terminal(self) -> None:
        """③防御用例（补充）：纯判定函数本身抛异常（而不是告警出口）时同样必须
        被兜住——两处独立的防御缺一不可。"""

        from unittest.mock import patch

        record = self._record_with_query(
            question="最近，尤其是7月之后数据下滑得厉害",
            start_date="2025-01-01",
            end_date="2025-08-25",
        )
        received: list[Mapping[str, object]] = []
        with patch(
            "lingxi.apps.worker.service.detect_year_grounding_suspect",
            side_effect=RuntimeError("模拟纯判定函数异常"),
        ):
            queue = self._run_with_record(record, on_year_grounding_suspect=received.append)

        self.assertEqual(received, [])  # 判定失败，告警出口从未被调用
        terminal = queue.terminals[0]
        self.assertEqual(terminal["terminal_kind"], "success")
        self.assertEqual(terminal["content"], "结果")
