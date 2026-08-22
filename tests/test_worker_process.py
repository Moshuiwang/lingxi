"""``lingxi-worker`` 队列模式进程入口：真实子进程 + 真实 SIGTERM（Issue #153）。

认领断言：
- V-部署-03（队列模式收到 ``SIGTERM`` 后停止领取新任务、在预算内退出）——用
  **真实子进程 + 真实信号**验证，不用 mock 信号；
- V-部署-01（不硬编码主机、端口、密钥；全部来自环境变量）；
- V-部署-12（有在途任务时，进程级仍必须在 ``LINGXI_WORKER_SHUTDOWN_TIMEOUT_SECONDS``
  预算内退出，任务保持 ``running`` 交给未来心跳超时回收——PR #173 独立复核 P1-3：
  这条红线此前只在 ``_monitor`` 的协作式中断这一段被验证过，``_run_queue_worker``
  自己的 ``asyncio.wait_for`` 预算从未被任何用例真正跑过；把它整体替换成"立即
  cancel 不等待"，原有 28 条用例全绿不受影响）。

在途 Agent 回合的中断行为（``_monitor`` 把全局停机信号等同 ``/stop``）已在
``tests/test_worker_queue_consumer.py`` 用可控 ``Executor`` 桩在进程内验证——
真实回合需要真实 Claude Agent SDK + 模型调用，不在 CI/测试范围（协作约定）。
本文件只验证**真实子进程入口**本身确实安装了信号处理器、确实停止领取、确实
在远小于停机预算的时间内退出——这是"代码里写了 _run_queue_worker"与"python -m
lingxi.apps.worker 真的这样表现"之间的那一段，只有真实子进程能证明。

``QueueModeSigtermWithInFlightTaskTest`` 需要一个**不响应中断请求、永不产出
终止消息**的 Agent SDK 会话——这是真实 SDK 传输挂起时的最坏情况，也正是
停机预算存在的理由。做法是在 ``PYTHONPATH`` 最前面插入一个不含任何网络/模型
调用的假 ``claude_agent_sdk`` 模块（见 ``_write_hanging_agent_sdk``），子进程
``import claude_agent_sdk`` 时实际拿到的是这个假模块，不触达真实 SDK、不需要
真实凭据、不调用真实模型（协作约定：测试与 CI 不调用真实飞书/MCP/模型）。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from postgres_schema import ensure_production_schema, reset_production_rows

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

DSN = os.environ.get("LINGXI_POSTGRES_DSN")
SKIP_REASON = "跳过：未设置 LINGXI_POSTGRES_DSN，队列 worker 真实子进程断言未验证"


def _make_user_env_root(testcase: unittest.TestCase, *, seed_user_id: str | None = None) -> str:
    """Epic D 闸⑥：queue 模式启动要求 ``LINGXI_USER_ENV_ROOT``（见
    ``apps/worker/cli.py`` 的队列模式前置检查），本文件的真实子进程用例因此都
    需要一个可用的根目录。``seed_user_id`` 非空时额外放一份形状合法的
    ``.mcp.json``，供那些真的会走到"任务执行"这一步、需要读到用户自己配置的
    用例使用；只测启动/空队列/提前停止分支的用例不需要种子文件（那些任务从不
    会读到这一步），但仍然需要根目录本身存在，否则连启动都过不去。
    """

    directory = Path(tempfile.mkdtemp(prefix="lingxi-worker-user-env-root-"))
    testcase.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
    if seed_user_id:
        home = directory / seed_user_id
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
    return str(directory)


@unittest.skipUnless(DSN, SKIP_REASON)
class QueueModeSigtermTest(unittest.TestCase):
    """真实子进程：空队列（没有可领取的任务）下，SIGTERM 必须让进程迅速、
    干净地退出——不等到 45 秒的停机预算耗尽（那个预算是"在途任务收口"的上限，
    不该在无事可做时也被用满）。
    """

    def test_sigterm_on_an_idle_queue_worker_exits_promptly(self) -> None:
        # 队列必须真的是空的：留一条陈旧的 target_worker_version='stable' 任务会让
        # 真实子进程去真的领取并驱动 Claude Agent SDK（本用例没有配置模型凭据），
        # 那样测的就不是"空队列下退出多快"，而是一次不受控的真实执行尝试。
        assert DSN is not None
        ensure_production_schema(DSN)
        reset_production_rows(DSN)

        environment = {
            **os.environ,
            "PYTHONPATH": str(REPOSITORY_ROOT / "src"),
            "PYTHONUNBUFFERED": "1",
            "LINGXI_WORKER_MODE": "queue",
            "LINGXI_WORKER_READONLY_TOOLS": "mcp__query__noop",
            "LINGXI_WORKER_TRACE_ID": "01J00000000000000000000WRK",
            "LINGXI_WORKER_ID": "worker-sigterm-test",
            "LINGXI_WORKER_POLL_INTERVAL_SECONDS": "0.2",
            "LINGXI_WORKER_HEARTBEAT_INTERVAL_SECONDS": "5",
            "LINGXI_POSTGRES_DSN": DSN,
            # 空队列下从不会真的处理任务，因此不需要种子 .mcp.json；但队列模式
            # 启动本身要求这个变量存在（Epic D 闸⑥），缺了会在启动期就拒绝。
            "LINGXI_USER_ENV_ROOT": _make_user_env_root(self),
        }
        process = subprocess.Popen(
            [sys.executable, "-m", "lingxi.apps.worker"],
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # 队列模式没有 "ready" 行可等；用一段真实运行时间确保进程已经真的
            # 跑进 run() 循环（而不是还在配置解析阶段），再发信号。
            time.sleep(1.0)
            self.assertIsNone(process.poll(), "进程应仍在运行，才谈得上验证 SIGTERM 退出")
            sent_at = time.monotonic()
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=15)
        finally:
            if process.poll() is None:  # pragma: no cover - 只在断言失败路径上发生
                process.kill()
                process.communicate()

        elapsed = time.monotonic() - sent_at
        self.assertEqual(process.returncode, 0, msg=f"stdout={stdout}\nstderr={stderr}")
        self.assertLess(
            elapsed,
            10.0,
            "空队列下 SIGTERM 必须迅速退出，不该等到 45 秒的在途任务收口预算耗尽",
        )
        self.assertIn("worker.queue.signal_received", stderr)


# ---------------------------------------------------------------------------
# 假 claude_agent_sdk：一个永不响应中断、永不产出终止消息的会话（PR #173 复核
# P1-3）。真实 SDK 传输挂起、迟迟不发终止消息正是 `_run_queue_worker` 的
# `asyncio.wait_for(run_task, timeout=shutdown_timeout_seconds)` 要防的最坏情况；
# 这里只模拟"挂起不响应"这一个形状，不涉及任何网络、凭据或模型调用。
_HANGING_AGENT_SDK_SOURCE = textwrap.dedent(
    '''
    """测试专用假 claude_agent_sdk：模拟一个永不产出终止消息的挂起会话。

    只被 tests/test_worker_process.py 的真实子进程通过 PYTHONPATH 注入使用，
    不是生产依赖，不含任何网络/模型调用。
    """

    from __future__ import annotations

    import asyncio


    class HookMatcher:
        def __init__(self, matcher=None, hooks=None, timeout=None):
            self.matcher = matcher
            self.hooks = hooks or []
            self.timeout = timeout


    class ClaudeAgentOptions:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)
            self.hooks = kwargs.get("hooks")


    class TextBlock:
        def __init__(self, text):
            self.text = text


    class ToolResultBlock:
        def __init__(self, tool_use_id, content=None, is_error=None):
            self.tool_use_id = tool_use_id
            self.content = content
            self.is_error = is_error


    class AssistantMessage:
        def __init__(self, content=None):
            self.content = content or []


    class UserMessage:
        def __init__(self, content=None):
            self.content = content or []


    class ResultMessage:
        def __init__(self, subtype="success", is_error=False):
            self.subtype = subtype
            self.is_error = is_error


    class ClaudeSDKClient:
        """模拟一个建连成功、之后再也不响应任何请求的会话——不实现
        ``interrupt()``（真实 SDK 传输挂起时同样可能对中断请求没有反应），
        ``receive_response()`` 永远不产出消息、也永远不返回。"""

        def __init__(self, options=None):
            self.options = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def query(self, prompt, session_id="default"):
            return None

        async def receive_response(self):
            await asyncio.Event().wait()
            yield  # pragma: no cover - 不可达；只为让本函数是 async generator
    '''
)


def _write_hanging_agent_sdk(directory: Path) -> None:
    (directory / "claude_agent_sdk.py").write_text(_HANGING_AGENT_SDK_SOURCE, encoding="utf-8")


@unittest.skipUnless(DSN, SKIP_REASON)
class QueueModeSigtermWithInFlightTaskTest(unittest.TestCase):
    """真实子进程 + 在途任务：SIGTERM 后必须在停机预算内退出，任务保持
    ``running``（PR #173 独立复核 P1-3）。

    变异存活证据：把 ``_run_queue_worker`` 的
    ``await asyncio.wait_for(run_task, timeout=shutdown_timeout_seconds)`` 整体
    替换成"立即 ``run_task.cancel()`` 不等待"后，本用例会在远小于
    ``shutdown_timeout_seconds`` 的时间内看到进程退出，``elapsed >=
    shutdown_timeout_seconds * 0.6`` 断言随之变红——这正是本用例要守住的分辨力。
    """

    WORKER_ID = "worker-sigterm-inflight-test"
    TASK_ID = "tsk-sigterm-inflight"
    CONVERSATION_ID = "cnv-sigterm-inflight"

    def setUp(self) -> None:
        assert DSN is not None
        ensure_production_schema(DSN)
        reset_production_rows(DSN)
        self._insert_queued_task()

    def _insert_queued_task(self) -> None:
        assert DSN is not None
        from psycopg import connect

        with connect(DSN) as connection:
            with connection.transaction():
                connection.execute(
                    """INSERT INTO app_user
                       (id, feishu_open_id, feishu_user_id, feishu_union_id,
                        display_name, department, tenant_key, provisioning_state)
                       VALUES ('usr-sigterm-inflight','ou-sigterm-inflight',
                               'u-sigterm-inflight','un-sigterm-inflight',
                               '张三','数据部','tk-sigterm-inflight','active')"""
                )
                connection.execute(
                    """INSERT INTO conversation
                       (id,user_id,feishu_chat_id,feishu_thread_id)
                       VALUES (%s,'usr-sigterm-inflight',%s,%s)""",
                    (self.CONVERSATION_ID, "chat-sigterm-inflight", "topic-sigterm-inflight"),
                )
                connection.execute(
                    """INSERT INTO task
                       (id,conversation_id,user_id,inbound_event_id,prompt,status,
                        target_worker_version,attempts,created_at,scheduled_at,
                        side_effect_state,content_expires_at)
                       VALUES (%s,%s,'usr-sigterm-inflight','event-sigterm-inflight',
                               '这是一条会挂住的问题','queued','stable',0,
                               now()-interval '1 minute',now()-interval '1 minute',
                               'none',now())""",
                    (self.TASK_ID, self.CONVERSATION_ID),
                )

    def _task_status(self) -> tuple[str, str | None]:
        assert DSN is not None
        from psycopg import connect

        with connect(DSN) as connection:
            row = connection.execute(
                "SELECT status, worker_id FROM task WHERE id=%s", (self.TASK_ID,)
            ).fetchone()
            assert row is not None
            return row[0], row[1]

    def test_sigterm_with_an_in_flight_task_exits_within_the_shutdown_budget_and_leaves_it_running(
        self,
    ) -> None:
        assert DSN is not None
        fake_sdk_dir = Path(
            __import__("tempfile").mkdtemp(prefix="lingxi-review173fix-fakesdk-")
        )
        self.addCleanup(
            lambda: __import__("shutil").rmtree(fake_sdk_dir, ignore_errors=True)
        )
        _write_hanging_agent_sdk(fake_sdk_dir)

        shutdown_timeout_seconds = 4.0
        environment = {
            **os.environ,
            # 假 SDK 目录必须排在 src 前面才能真的把 import 挡住。
            "PYTHONPATH": os.pathsep.join(
                [str(fake_sdk_dir), str(REPOSITORY_ROOT / "src")]
            ),
            "PYTHONUNBUFFERED": "1",
            "LINGXI_WORKER_MODE": "queue",
            "LINGXI_WORKER_READONLY_TOOLS": "mcp__query__noop",
            "LINGXI_WORKER_TRACE_ID": "01J00000000000000000000WR2",
            "LINGXI_WORKER_ID": self.WORKER_ID,
            "LINGXI_WORKER_TARGET_VERSION": "stable",
            "LINGXI_WORKER_POLL_INTERVAL_SECONDS": "0.2",
            "LINGXI_WORKER_STOP_POLL_INTERVAL_SECONDS": "0.2",
            "LINGXI_WORKER_HEARTBEAT_INTERVAL_SECONDS": "30",
            "LINGXI_WORKER_SHUTDOWN_TIMEOUT_SECONDS": str(shutdown_timeout_seconds),
            "LINGXI_POSTGRES_DSN": DSN,
            # 这个任务真的会被领走、真的会走到 _process_task 里按 user_id 读
            # .mcp.json 那一步（Epic D 闸⑥）——种子文件必须存在，否则任务会在
            # 碰到假 SDK 之前就被判定 user_mcp_config_unavailable 而失败关闭，
            # 测不到本用例真正要验证的"挂起会话下的停机预算"这件事。
            "LINGXI_USER_ENV_ROOT": _make_user_env_root(
                self, seed_user_id="usr-sigterm-inflight"
            ),
        }
        process = subprocess.Popen(
            [sys.executable, "-m", "lingxi.apps.worker"],
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # 轮询数据库直到任务被真的领走（status 变成 running），确认信号会打在
            # "有在途任务"这个窗口上，而不是打在还没领到任务的空转期。
            deadline = time.monotonic() + 10.0
            status = None
            while time.monotonic() < deadline:
                status, worker_id = self._task_status()
                if status == "running" and worker_id == self.WORKER_ID:
                    break
                time.sleep(0.1)
            self.assertEqual(
                status,
                "running",
                "任务必须先被真实子进程领走、进入 running，才谈得上验证"
                "「SIGTERM 时有在途任务」这个场景",
            )
            self.assertIsNone(process.poll(), "进程应仍在运行，才谈得上验证 SIGTERM 退出")

            sent_at = time.monotonic()
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=shutdown_timeout_seconds + 20.0)
        finally:
            if process.poll() is None:  # pragma: no cover - 只在断言失败路径上发生
                process.kill()
                process.communicate()

        elapsed = time.monotonic() - sent_at
        self.assertEqual(process.returncode, 0, msg=f"stdout={stdout}\nstderr={stderr}")
        self.assertIn("worker.queue.signal_received", stderr)
        self.assertIn(
            "worker.queue.shutdown_budget_exhausted",
            stderr,
            "假 SDK 永不响应中断、永不产出终止消息，进程必须走到"
            "「预算耗尽、不再等待」这条分支",
        )
        # 核心分辨力：假 SDK 完全不响应中断，若停机路径被替换成"立即 cancel
        # 不等待"，进程会在远小于 shutdown_timeout_seconds 的时间内退出——
        # 这条断言就是用来抓住那个变异的。
        self.assertGreaterEqual(
            elapsed,
            shutdown_timeout_seconds * 0.6,
            "进程退出得太快：停机预算应该被真的用满（在途任务从不主动收口），"
            "而不是收到信号就立即放弃等待",
        )
        self.assertLess(
            elapsed,
            shutdown_timeout_seconds + 15.0,
            "进程没有在停机预算耗尽后的合理时间内退出，停机预算形同虚设",
        )

        status, worker_id = self._task_status()
        self.assertEqual(
            status,
            "running",
            "预算耗尽后任务必须保持 running（交给未来一次心跳超时回收），"
            "不得被写成任何终态——终态意味着结果在没有真实收口的情况下被凭空"
            "落库，且这次的领取不会被重排，等价于用户结果丢失",
        )
        self.assertEqual(worker_id, self.WORKER_ID, "任务仍应记着是这次领取，没有被别的路径抢走")


@unittest.skipUnless(DSN, SKIP_REASON)
class QueueModeTerminalOutcomeLoggingTest(unittest.TestCase):
    """真实子进程 + 真实 PostgreSQL：终态收口低敏审计事件必须真正落到队列
    worker 进程的 stderr（Issue #90 评论 5306860255 独立复核 P1）。

    ``tests/test_worker_queue_consumer.py`` 的白盒单测只能证明"注入的
    ``on_terminal_outcome`` 回调被调用"，证明不了"真实队列 worker 进程接线
    之后运维能在 stderr 里看到它"——真实进程从不调用 ``logging.
    basicConfig()``，经 stdlib ``logging`` 发出的调用会被默认阈值悄悄吞掉
    （见 ``apps/worker/cli.py`` 的 ``_LogOnlyAlertSender`` 说明）。这里插入一条
    **建库时就已经** ``stop_requested = TRUE`` 的任务：worker 领到后立刻走
    ``_process_task`` 最早的停止分支收口，不需要真实 Claude Agent SDK 或模型
    凭据（协作约定：测试与 CI 不调用真实模型/飞书）。

    变异存活证据：删掉 ``cli.py`` 里 ``WorkerService(...)`` 的
    ``on_terminal_outcome=_terminal_outcome_sink(...)`` 参数，或删掉
    ``_log_terminal_outcome`` 里对回调的调用，本用例都会因为 stderr 里找不到
    ``worker.task.terminal`` 事件而变红。
    """

    WORKER_ID = "worker-terminal-log-test"
    TASK_ID = "tsk-terminal-log"
    CONVERSATION_ID = "cnv-terminal-log"
    TRACE_ID = "01J00000000000000000000WR3"

    def setUp(self) -> None:
        assert DSN is not None
        ensure_production_schema(DSN)
        reset_production_rows(DSN)
        self._insert_already_stopped_task()

    def _insert_already_stopped_task(self) -> None:
        assert DSN is not None
        from psycopg import connect

        with connect(DSN) as connection:
            with connection.transaction():
                connection.execute(
                    """INSERT INTO app_user
                       (id, feishu_open_id, feishu_user_id, feishu_union_id,
                        display_name, department, tenant_key, provisioning_state)
                       VALUES ('usr-terminal-log','ou-terminal-log',
                               'u-terminal-log','un-terminal-log',
                               '李四','数据部','tk-terminal-log','active')"""
                )
                connection.execute(
                    """INSERT INTO conversation
                       (id,user_id,feishu_chat_id,feishu_thread_id)
                       VALUES (%s,'usr-terminal-log',%s,%s)""",
                    (self.CONVERSATION_ID, "chat-terminal-log", "topic-terminal-log"),
                )
                # stop_requested 在建库时就是 TRUE：worker 领到后不需要真实
                # Agent SDK 会话就能走到最早的停止分支收口（`_process_task`
                # 判 `claimed.stop_requested` 那一段），本用例只关心终态收口
                # 审计事件是否真的落到了 stderr，不关心执行器本身。
                connection.execute(
                    """INSERT INTO task
                       (id,conversation_id,user_id,inbound_event_id,prompt,status,
                        target_worker_version,attempts,created_at,scheduled_at,
                        side_effect_state,content_expires_at,stop_requested)
                       VALUES (%s,%s,'usr-terminal-log','event-terminal-log',
                               '这是一条已经被要求停止的问题','queued','stable',0,
                               now()-interval '1 minute',now()-interval '1 minute',
                               'none',now(),TRUE)""",
                    (self.TASK_ID, self.CONVERSATION_ID),
                )

    def _task_status(self) -> str:
        assert DSN is not None
        from psycopg import connect

        with connect(DSN) as connection:
            row = connection.execute(
                "SELECT status FROM task WHERE id=%s", (self.TASK_ID,)
            ).fetchone()
            assert row is not None
            return row[0]

    def test_terminal_outcome_event_reaches_stderr_with_trace_id(self) -> None:
        assert DSN is not None
        environment = {
            **os.environ,
            "PYTHONPATH": str(REPOSITORY_ROOT / "src"),
            "PYTHONUNBUFFERED": "1",
            "LINGXI_WORKER_MODE": "queue",
            "LINGXI_WORKER_READONLY_TOOLS": "mcp__query__noop",
            "LINGXI_WORKER_TRACE_ID": self.TRACE_ID,
            "LINGXI_WORKER_ID": self.WORKER_ID,
            "LINGXI_WORKER_TARGET_VERSION": "stable",
            "LINGXI_WORKER_POLL_INTERVAL_SECONDS": "0.2",
            "LINGXI_WORKER_HEARTBEAT_INTERVAL_SECONDS": "30",
            "LINGXI_POSTGRES_DSN": DSN,
            # 这条任务建库时就已 stop_requested=TRUE，_process_task 在最早的
            # 停止分支就收口返回，从不会走到按 user_id 读 .mcp.json 那一步——
            # 不需要种子文件，但队列模式启动仍要求这个变量存在。
            "LINGXI_USER_ENV_ROOT": _make_user_env_root(self),
        }
        process = subprocess.Popen(
            [sys.executable, "-m", "lingxi.apps.worker"],
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # 轮询数据库直到终态真正写入（status 转 awaiting_delivery），确认
            # 断言打在"终态已经收口"这个窗口上，而不是任务还在排队。
            deadline = time.monotonic() + 10.0
            status = None
            while time.monotonic() < deadline:
                status = self._task_status()
                if status == "awaiting_delivery":
                    break
                time.sleep(0.1)
            self.assertEqual(
                status,
                "awaiting_delivery",
                "已带 stop_requested 的任务应被迅速领取并走停止分支收口",
            )
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=15)
        finally:
            if process.poll() is None:  # pragma: no cover - 只在断言失败路径上发生
                process.kill()
                process.communicate()

        self.assertEqual(process.returncode, 0, msg=f"stdout={stdout}\nstderr={stderr}")

        terminal_events = []
        for line in stderr.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("event") == "worker.task.terminal":
                terminal_events.append(record)

        self.assertEqual(
            len(terminal_events),
            1,
            f"应恰好出现一条 worker.task.terminal 事件，实际 stderr={stderr}",
        )
        event = terminal_events[0]
        self.assertEqual(event["trace_id"], self.TRACE_ID)
        self.assertEqual(event["task_id"], self.TASK_ID)
        self.assertEqual(event["terminal_kind"], "stopped")
        self.assertEqual(event["error_kind"], "stopped")


if __name__ == "__main__":
    unittest.main()
