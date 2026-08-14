"""``lingxi-worker`` 队列模式进程入口：真实子进程 + 真实 SIGTERM（Issue #153）。

认领断言：
- V-部署-03（队列模式收到 ``SIGTERM`` 后停止领取新任务、在预算内退出）——用
  **真实子进程 + 真实信号**验证，不用 mock 信号；
- V-部署-01（不硬编码主机、端口、密钥；全部来自环境变量）。

在途 Agent 回合的中断行为（``_monitor`` 把全局停机信号等同 ``/stop``）已在
``tests/test_worker_queue_consumer.py`` 用可控 ``Executor`` 桩在进程内验证——
真实回合需要真实 Claude Agent SDK + 模型调用，不在 CI/测试范围（协作约定）。
本文件只验证**真实子进程入口**本身确实安装了信号处理器、确实停止领取、确实
在远小于停机预算的时间内退出——这是"代码里写了 _run_queue_worker"与"python -m
lingxi.apps.worker 真的这样表现"之间的那一段，只有真实子进程能证明。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

from postgres_schema import ensure_production_schema, reset_production_rows

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

DSN = os.environ.get("LINGXI_POSTGRES_DSN")
SKIP_REASON = "跳过：未设置 LINGXI_POSTGRES_DSN，队列 worker 真实子进程断言未验证"


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
            "LINGXI_WORKER_READONLY_TOOL": "mcp__ci_probe__noop",
            "LINGXI_WORKER_TRACE_ID": "01J00000000000000000000WRK",
            "LINGXI_WORKER_ID": "worker-sigterm-test",
            "LINGXI_WORKER_POLL_INTERVAL_SECONDS": "0.2",
            "LINGXI_WORKER_HEARTBEAT_INTERVAL_SECONDS": "5",
            "LINGXI_POSTGRES_DSN": DSN,
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


if __name__ == "__main__":
    unittest.main()
