"""Epic D 闸⑥：queue 模式启动预检——``LINGXI_USER_ENV_ROOT`` 缺失时必须以清晰
的配置错误拒绝启动，绝不带着"每个任务都必然失败关闭"的配置进入运行状态。

理由与 Issue #177 的工作目录预检（``tests/test_worker_workspace_precheck.py``）
同一姿态：queue 模式是唯一真正处理用户任务的路径，每个任务都要按它的 user_id
读 ``<user_env_root>/<user_id>/.mcp.json``（见 ``apps/worker/service.py`` 的
``_process_task``）——缺了这个根目录，队列 worker 领到的**每一个**任务都必然
在读配置这一步失败关闭。与其带着这个必然失败的配置启动、让每个任务分别撞上
同一个原因，不如在启动期一次性拒绝（与 ``LINGXI_POSTGRES_DSN`` 同一姿态：
恰一条日志、只报变量名、不回显取到的值）。

本文件不接触真实数据库：失败路径在 ``LINGXI_USER_ENV_ROOT`` 检查处提前返回，
从不构造 ``PostgresTaskQueue``/发起任何连接；成功路径通过打桩
``_run_queue_worker`` 证明 ``main()`` 真的越过了这条检查、进入了队列消费循环
装配。
"""

from __future__ import annotations

import io
import json
import unittest
from unittest import mock

from lingxi.apps.worker.cli import EXIT_CONFIG_ERROR, main


def _worker_queue_env(**overrides: str) -> dict[str, str]:
    env = {
        "LINGXI_WORKER_MODE": "queue",
        "LINGXI_WORKER_READONLY_TOOL": "mcp__ci_probe__noop",
        "LINGXI_WORKER_TRACE_ID": "01J00000000000000000000WKR",
        # 这个变量名合法、但从不会被真正连接：失败路径在 LINGXI_USER_ENV_ROOT
        # 检查处提前返回，成功路径由 `_run_queue_worker` 打桩接管，两者都不会
        # 真的拨号。
        "LINGXI_POSTGRES_DSN": "postgresql://user:pass@localhost:5432/does-not-matter",
    }
    env.update(overrides)
    return env


async def _stub_run_queue_worker(*args: object, **kwargs: object) -> None:
    """替身：立即返回，不真的跑队列消费循环、不连接数据库。"""

    return None


def _structured_lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


class QueueModeUserEnvRootPrecheckTests(unittest.TestCase):
    def test_a_missing_user_env_root_fails_the_queue_worker_at_startup_before_touching_the_database(
        self,
    ) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()

        # 不打桩 PostgresTaskQueue：如果检查失效、代码真的往下走去连接数据库，
        # 这里会因为连不上一个伪造的 DSN 而以别的方式失败（超时/连接错误），
        # 而不是干净地在配置阶段以 EXIT_CONFIG_ERROR 收口——那本身就是一种
        # 可观察的红。
        code = main(env=_worker_queue_env(), stdout=stdout, stderr=stderr)

        self.assertEqual(code, EXIT_CONFIG_ERROR)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["failure"]["code"], "config_error")
        self.assertIn("USER_ENV_ROOT", payload["failure"]["message"])
        events = [line["event"] for line in _structured_lines(stderr)]
        self.assertIn("worker.queue.config.invalid", events)
        self.assertNotIn(
            "worker.queue.start",
            events,
            "预检失败时不该先宣告队列 worker 已启动，再报启动失败",
        )

    def test_a_blank_user_env_root_is_treated_as_missing(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()

        code = main(
            env=_worker_queue_env(LINGXI_USER_ENV_ROOT="   "), stdout=stdout, stderr=stderr
        )

        self.assertEqual(code, EXIT_CONFIG_ERROR)
        payload = json.loads(stdout.getvalue())
        self.assertIn("USER_ENV_ROOT", payload["failure"]["message"])

    def test_a_configured_user_env_root_lets_the_queue_worker_start_normally(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()

        with mock.patch("lingxi.apps.worker.cli._run_queue_worker", _stub_run_queue_worker):
            code = main(
                env=_worker_queue_env(LINGXI_USER_ENV_ROOT="/var/lib/lingxi/users"),
                stdout=stdout,
                stderr=stderr,
            )

        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue(), "", "队列模式正常启动路径不写 stdout")
        events = [line["event"] for line in _structured_lines(stderr)]
        self.assertNotIn("worker.queue.config.invalid", events)
        self.assertIn("worker.queue.start", events, "必须真的越过预检、进入队列启动日志")

    def test_turn_mode_does_not_require_user_env_root(self) -> None:
        """一次性受控回合模式没有 user_id 概念，不该被这条队列专属的前置检查
        拖累。"""

        stdout, stderr = io.StringIO(), io.StringIO()

        code = main(
            env={
                "LINGXI_WORKER_MODE": "turn",
                "LINGXI_WORKER_QUESTION": "近 7 天的活跃用户数是多少？",
                "LINGXI_WORKER_READONLY_TOOL": "mcp__ci_probe__noop",
            },
            stdout=stdout,
            stderr=stderr,
        )

        # turn 模式没有真实 SDK 时以 EXIT_SESSION_FAILED(4) 收口（sdk_unavailable），
        # 但绝不会是 EXIT_CONFIG_ERROR(3)/"USER_ENV_ROOT"——证明这条检查确实只
        # 挂在 queue 分支上。
        self.assertNotEqual(code, EXIT_CONFIG_ERROR)
        payload = json.loads(stdout.getvalue())
        self.assertNotIn("USER_ENV_ROOT", json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
