"""Issue #177：worker 队列模式启动预检——``LINGXI_WORKER_WORKSPACE`` 不存在或
不可写时先尝试创建，创建失败则以清晰的启动失败退出，绝不进入"每个回合泛化
session_failed"的运行状态。

S-A-07 r3 实测：部署配置显式指向一个容器内不存在的目录时，Agent SDK 子进程起
不来，每个回合都在约一秒内落成同一种泛化 ``session_failed``——容器 health 仍然
正常、Gateway 也正常投递失败卡片，运维因此无法从任何可观察面把"本地工作目录
无效"与"会话/模型真的失败了"区分开（见 Issue 正文）。

本文件不接触真实数据库：成功路径通过打桩 ``_run_queue_worker`` 证明 ``main()``
真的越过预检、进入了队列消费循环装配；失败路径在预检本身就返回，从不构造
``PostgresTaskQueue``/发起任何连接。
"""

from __future__ import annotations

import atexit
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lingxi.apps.worker.cli import EXIT_CONFIG_ERROR, _ensure_worker_workspace, main

# 权限位在以 root 运行时不生效（root 无视 rwx 位），这两条负向用例因此只在非
# root 环境下有意义；本仓库 CI（GitHub Actions ubuntu-24.04 runner）与本地开发
# 环境均以非 root 用户跑测试，这里仍显式判断一次，避免环境变化时静默产生假阳性。
_RUNNING_AS_ROOT = hasattr(os, "getuid") and os.getuid() == 0

# Epic D 闸⑥ F4：queue 模式启动现在会真的打开一次 LINGXI_USER_ENV_ROOT 核对
# 它存在且可读（见 tests/test_worker_user_env_root_precheck.py），不再是随便
# 一个格式合法的字符串就能过。本文件测的是工作目录预检本身，需要一个真实
# 存在的目录把这条无关的前置检查垫过去。
_USER_ENV_ROOT_DIR = tempfile.mkdtemp(prefix="lingxi-workspace-precheck-user-env-")
atexit.register(lambda: __import__("shutil").rmtree(_USER_ENV_ROOT_DIR, ignore_errors=True))


def _worker_queue_env(**overrides: str) -> dict[str, str]:
    env = {
        "LINGXI_WORKER_MODE": "queue",
        "LINGXI_WORKER_READONLY_TOOL": "mcp__ci_probe__noop",
        "LINGXI_WORKER_TRACE_ID": "01J00000000000000000000WRK",
        # 队列 worker 的启动预检必须在触达数据库之前就能判定工作目录是否可用；
        # 这个 DSN 语法合法但从不会被真正连接（失败路径在预检处提前返回，
        # 成功路径由下面的 `_run_queue_worker` 打桩接管，两者都不会真的拨号）。
        "LINGXI_POSTGRES_DSN": "postgresql://user:pass@localhost:5432/does-not-matter",
        # Epic D 闸⑥：queue 模式启动现在还要求这个变量真的存在（见
        # tests/test_worker_user_env_root_precheck.py），否则会在工作目录预检
        # 之前就先被这条检查拦下——本文件测的是工作目录预检本身，不该被这条
        # 无关的前置检查挡住。
        "LINGXI_USER_ENV_ROOT": _USER_ENV_ROOT_DIR,
    }
    env.update(overrides)
    return env


async def _stub_run_queue_worker(*args: object, **kwargs: object) -> None:
    """替身：立即返回，不真的跑队列消费循环、不连接数据库。"""

    return None


def _structured_lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


class EnsureWorkerWorkspaceUnitTests(unittest.TestCase):
    """直接单测预检函数本身：快、确定，不经过 main()、不需要真实数据库。"""

    def test_a_missing_directory_is_created_and_reports_success(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            target = Path(base) / "nested" / "workspace"
            self.assertFalse(target.exists())
            err = io.StringIO()

            ok = _ensure_worker_workspace(str(target), err=err, trace_id="trace-1")

            self.assertTrue(ok)
            self.assertTrue(target.is_dir())
            self.assertEqual(err.getvalue(), "", "成功路径不该产生错误日志")

    def test_an_existing_writable_directory_is_accepted_without_recreating(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            err = io.StringIO()

            ok = _ensure_worker_workspace(base, err=err, trace_id="trace-2")

            self.assertTrue(ok)
            self.assertEqual(err.getvalue(), "")

    @unittest.skipIf(_RUNNING_AS_ROOT, "root 无视权限位，负向断言在此环境下不成立")
    def test_an_existing_but_read_only_directory_fails_with_an_identifiable_event(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as base:
            target = Path(base) / "readonly"
            target.mkdir()
            target.chmod(stat.S_IRUSR | stat.S_IXUSR)  # r-x：去掉写权限
            err = io.StringIO()
            try:
                ok = _ensure_worker_workspace(str(target), err=err, trace_id="trace-3")
            finally:
                target.chmod(stat.S_IRWXU)  # 清理前恢复可写，避免临时目录删除失败

            self.assertFalse(ok)
            lines = _structured_lines(err)
            failure_line = next(
                line for line in lines if line["event"] == "worker.queue.workspace.unavailable"
            )
            self.assertEqual(failure_line["reason"], "not_a_writable_directory")
            self.assertEqual(failure_line["trace_id"], "trace-3")

    @unittest.skipIf(_RUNNING_AS_ROOT, "root 无视权限位，负向断言在此环境下不成立")
    def test_an_uncreatable_path_fails_with_an_identifiable_event(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            parent = Path(base) / "locked-parent"
            parent.mkdir()
            parent.chmod(stat.S_IRUSR | stat.S_IXUSR)  # r-x：不能在其下创建子目录
            target = parent / "workspace"
            err = io.StringIO()
            try:
                ok = _ensure_worker_workspace(str(target), err=err, trace_id="trace-4")
            finally:
                parent.chmod(stat.S_IRWXU)

            self.assertFalse(ok)
            self.assertFalse(target.exists())
            lines = _structured_lines(err)
            failure_line = next(
                line for line in lines if line["event"] == "worker.queue.workspace.unavailable"
            )
            self.assertEqual(failure_line["reason"], "create_failed")


class QueueModeWorkspacePrecheckThroughMainTests(unittest.TestCase):
    """经真实 ``main()`` 入口验证——证明预检真的接在队列模式启动路径上，不是只有
    一个没被调用的私有函数。
    """

    @unittest.skipIf(_RUNNING_AS_ROOT, "root 无视权限位，负向断言在此环境下不成立")
    def test_an_uncreatable_workspace_fails_the_queue_worker_at_startup(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            parent = Path(base) / "locked-parent"
            parent.mkdir()
            parent.chmod(stat.S_IRUSR | stat.S_IXUSR)
            target = parent / "workspace"
            stdout, stderr = io.StringIO(), io.StringIO()
            try:
                code = main(
                    env=_worker_queue_env(LINGXI_WORKER_WORKSPACE=str(target)),
                    stdout=stdout,
                    stderr=stderr,
                )
            finally:
                parent.chmod(stat.S_IRWXU)

            self.assertEqual(code, EXIT_CONFIG_ERROR)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["failure"]["code"], "config_error")
            self.assertIn("WORKSPACE", payload["failure"]["message"])
            events = [line["event"] for line in _structured_lines(stderr)]
            self.assertIn("worker.queue.workspace.unavailable", events)
            self.assertNotIn(
                "worker.queue.start",
                events,
                "预检失败时不该先宣告队列 worker 已启动，再报启动失败",
            )

    def test_a_missing_but_creatable_workspace_lets_the_queue_worker_start_normally(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as base:
            target = Path(base) / "nested" / "workspace"
            self.assertFalse(target.exists())
            stdout, stderr = io.StringIO(), io.StringIO()

            with mock.patch(
                "lingxi.apps.worker.cli._run_queue_worker", _stub_run_queue_worker
            ):
                code = main(
                    env=_worker_queue_env(LINGXI_WORKER_WORKSPACE=str(target)),
                    stdout=stdout,
                    stderr=stderr,
                )

            self.assertEqual(code, 0)
            self.assertTrue(target.is_dir(), "预检必须真的创建了这个目录")
            self.assertEqual(stdout.getvalue(), "", "队列模式正常启动路径不写 stdout")
            events = [line["event"] for line in _structured_lines(stderr)]
            self.assertNotIn("worker.queue.workspace.unavailable", events)
            self.assertIn("worker.queue.start", events, "必须真的越过预检、进入队列启动日志")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
