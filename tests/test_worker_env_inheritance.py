"""生产数据库连接串不得被 Claude CLI 子进程继承（对抗审查 2026-09-02 R6-D2）。

事实链：

- ``adapters/claude_agent_session.py`` 不传 ``options.env``；
- ``claude-agent-sdk`` 起 CLI 子进程时用
  ``inherited_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}``
  再叠 ``**self._options.env``（``_internal/transport/subprocess_cli.py``）——
  也就是说 ``options.env`` **只能往上加、不能往下减**，SDK 侧没有任何参数能删掉
  一个已经在进程环境里的变量；
- ``deploy/compose.yaml`` 给 worker-queue 的 env 文件里有 ``LINGXI_POSTGRES_DSN``。

三条叠起来：worker-queue 起的每一个 ``claude`` 与 MCP 子进程都带着生产库的读写
连接串。当前模型侧 Bash/Read 是拒的，因此不可达；与 hook 超时失败开放（报告 W-1）
叠加时后果直接扩大到生产库。

因此唯一能真正生效的位置是本进程自己的 ``os.environ``。本文件把这条钉成断言，
验收口径与审查报告的上线前预检一致——``/proc/<pid>/environ`` 里含 DSN 的子进程数
= 0，只不过这里用本地构造的子进程复现，不连任何机器。

**摘除发生在真实进程入口（``apps/worker/__main__.py``）而不是 ``cli.main()`` 里**：
``main()`` 是一个会被单测在同一个解释器里反复调用的普通函数，把 ``os.environ.pop``
放进去，跑完几条队列模式启动用例之后，同一个进程里所有真库用例的 DSN 就都没了
（2026-09-02 CI 实测：40 个 ``setUpClass`` KeyError，外加 ``verify_repository.sh``
的「容器在、DSN 没了」守卫判红）。``MainDoesNotMutateProcessEnvironmentTest`` 就是
那次回归的守门用例。
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from lingxi.apps.worker.cli import (
    _UNINHERITABLE_ENV_VARS,
    detach_process_environment,
    main,
)

DSN_VAR = "LINGXI_POSTGRES_DSN"
FAKE_DSN = "postgresql://lingxi_app:hunter2@db.invalid:5432/lingxi"


def _child_environ_bytes() -> bytes:
    """起一个子进程（默认继承环境，与 SDK 同一姿态），读它的 ``/proc/<pid>/environ``。

    这就是审查报告里那条上线前预检的本地形态：数的是"子进程的环境里到底有没有
    这个变量"，不是"我们以为传了什么"。
    """

    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; print('ready', flush=True); sys.stdin.readline()",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        # 先等子进程自己报到：Popen 返回时它可能还没走完 exec，此时
        # /proc/<pid>/environ 读出来是空的——那会让下面的断言"因为什么都没读到"
        # 而通过，正是这类用例最典型的假绿。
        assert child.stdout is not None
        ready = child.stdout.readline()
        assert ready.strip() == "ready", f"子进程没有正常起来：{ready!r}"
        data = Path(f"/proc/{child.pid}/environ").read_bytes()
        assert data, "/proc/<pid>/environ 读出来是空的，本用例什么都没验证"
        return data
    finally:
        assert child.stdin is not None
        child.stdin.close()
        child.wait(timeout=10)
        if child.stdout is not None:
            child.stdout.close()


@unittest.skipUnless(Path("/proc/self/environ").exists(), "需要 /proc（Linux）")
class ChildProcessDoesNotInheritTheDsnTest(unittest.TestCase):
    def setUp(self) -> None:
        self._previous = os.environ.get(DSN_VAR)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._previous is None:
            os.environ.pop(DSN_VAR, None)
        else:
            os.environ[DSN_VAR] = self._previous

    def test_the_control_shows_the_child_really_would_inherit_it(self) -> None:
        """先证明这条用例抓得住：不摘掉时子进程环境里确实有它。

        没有这个对照，下面那条"摘掉后就没有了"可能只是因为子进程压根没继承
        任何东西——那样它永远是绿的，什么都没验证。
        """

        os.environ[DSN_VAR] = FAKE_DSN

        self.assertIn(f"{DSN_VAR}=".encode(), _child_environ_bytes())

    def test_after_detaching_no_child_process_carries_the_dsn(self) -> None:
        """验收口径：``/proc/<pid>/environ`` 含 DSN 的子进程数 = 0。"""

        os.environ[DSN_VAR] = FAKE_DSN

        snapshot, removed = detach_process_environment()

        self.assertEqual(removed, (DSN_VAR,))
        self.assertNotIn(DSN_VAR, os.environ)
        environ_bytes = _child_environ_bytes()
        self.assertNotIn(f"{DSN_VAR}=".encode(), environ_bytes)
        self.assertNotIn(FAKE_DSN.encode(), environ_bytes, "值也不许以别的变量名混进去")

        # 摘除**之前**的快照仍然带着值：本进程照常读得到配置，这正是"摘掉了却
        # 还能工作"的全部机制。
        self.assertEqual(snapshot[DSN_VAR], FAKE_DSN)

    def test_detaching_is_idempotent_when_there_is_nothing_to_remove(self) -> None:
        os.environ.pop(DSN_VAR, None)

        snapshot, removed = detach_process_environment()

        self.assertEqual(removed, ())
        self.assertNotIn(DSN_VAR, snapshot)

    def test_the_startup_log_records_the_variable_name_but_never_the_value(self) -> None:
        """摘除本身不记日志（那时还没有 trace id）；由 ``main()`` 用入口传进来的
        变量名记一条。这里直接驱动 ``main()`` 的队列分支验证那一条的形状。"""

        stderr = io.StringIO()
        stdout = io.StringIO()

        # 故意缺 LINGXI_USER_ENV_ROOT：队列模式会在读完 DSN、记完这条日志之后
        # 以配置错误退出，不需要任何数据库。
        main(
            env={
                "LINGXI_WORKER_MODE": "queue",
                "LINGXI_WORKER_READONLY_TOOLS": "mcp__query__noop",
                "LINGXI_WORKER_TRACE_ID": "01J0000000000000000TEST000",
                "LINGXI_POSTGRES_DSN": FAKE_DSN,
            },
            stdout=stdout,
            stderr=stderr,
            detached_env_vars=(DSN_VAR,),
        )

        lines = [line for line in stderr.getvalue().splitlines() if "env_detached" in line]
        self.assertEqual(len(lines), 1, stderr.getvalue())
        record = json.loads(lines[0])
        self.assertEqual(record["variables"], [DSN_VAR])
        self.assertNotIn("hunter2", lines[0])
        self.assertNotIn("db.invalid", lines[0])


class DetachHappensAtTheRealProcessEntryTest(unittest.TestCase):
    """摘除的**位置**是这条修复的全部：晚一步等于没做，放错层则毒化整个进程。

    源码级断言，因为真正跑一次 queue 模式要连真库、起 SDK。
    """

    ENTRY = Path(__file__).parents[1] / "src/lingxi/apps/worker/__main__.py"
    CLI = Path(__file__).parents[1] / "src/lingxi/apps/worker/cli.py"

    def test_the_entry_point_detaches_before_it_calls_main(self) -> None:
        tree = ast.parse(self.ENTRY.read_text(encoding="utf-8"))
        calls: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                calls.append((node.lineno, node.func.id))
        order = [name for _, name in sorted(calls)]

        self.assertIn("detach_process_environment", order)
        self.assertIn("main", order)
        self.assertLess(
            order.index("detach_process_environment"),
            order.index("main"),
            "必须先摘、再进 main——反过来 main 就已经带着 DSN 起了第一个回合",
        )

    def test_the_entry_point_feeds_main_the_pre_detach_snapshot(self) -> None:
        """摘掉之后 ``main()`` 还得读得到配置，靠的就是这份快照。

        入口如果不传 ``env=``，``main()`` 会退回读已经被摘空的 ``os.environ``，
        队列 worker 会以"缺少 LINGXI_POSTGRES_DSN"启动失败——一个把自己饿死的修复。
        """

        source = self.ENTRY.read_text(encoding="utf-8")

        self.assertRegex(source, r"main\(\s*env=", "main 必须收到入口给的快照")
        self.assertIn("detached_env_vars=", source, "摘掉了什么要传给 main 记日志")

    def test_the_uninheritable_list_is_not_silently_emptied(self) -> None:
        self.assertIn(DSN_VAR, _UNINHERITABLE_ENV_VARS)


class MainDoesNotMutateProcessEnvironmentTest(unittest.TestCase):
    """2026-09-02 CI 回归的守门用例。

    上一版把 ``os.environ.pop`` 放在 ``cli.main()`` 里。``main()`` 会被单测在同一个
    解释器里反复调用（``test_worker_workspace_precheck`` 等三个文件都走队列模式的
    启动路径），于是跑完那几条之后，同一个进程里**所有**真库用例的
    ``LINGXI_POSTGRES_DSN`` 就没了：40 个 ``setUpClass`` KeyError，
    ``verify_repository.sh`` 的「设了容器却没有 DSN」守卫也准确判红。

    这条用例直接钉住"``main()`` 不碰进程环境"，任何人把 pop 挪回去都必然变红。
    """

    def test_running_the_queue_branch_leaves_os_environ_untouched(self) -> None:
        previous = os.environ.get(DSN_VAR)
        self.addCleanup(
            lambda: os.environ.__setitem__(DSN_VAR, previous)
            if previous is not None
            else os.environ.pop(DSN_VAR, None)
        )
        os.environ[DSN_VAR] = FAKE_DSN
        before = dict(os.environ)

        # 缺 LINGXI_USER_ENV_ROOT，队列模式会在配置检查处退出；这已经走过了
        # 读 DSN 的那一段，也就是原来 pop 所在的位置。
        main(
            env={
                "LINGXI_WORKER_MODE": "queue",
                "LINGXI_WORKER_READONLY_TOOLS": "mcp__query__noop",
                "LINGXI_WORKER_TRACE_ID": "01J0000000000000000TEST000",
                "LINGXI_POSTGRES_DSN": FAKE_DSN,
            },
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )

        self.assertEqual(
            dict(os.environ), before, "main() 不得改动进程环境——它不是进程入口"
        )
        self.assertEqual(os.environ[DSN_VAR], FAKE_DSN)

    def test_the_real_entry_still_reads_the_dsn_after_detaching(self) -> None:
        """真实进程端到端：``python -m lingxi.apps.worker`` 队列模式，
        DSN 在环境里、``LINGXI_USER_ENV_ROOT`` 故意缺失。

        失败理由必须是"缺少 LINGXI_USER_ENV_ROOT"。如果入口摘掉 DSN 之后没有把
        快照喂给 ``main()``，这里会变成"缺少 LINGXI_POSTGRES_DSN"——那正是
        "摘得太干净把自己饿死"的形状。不连任何数据库：这一步在建连接之前。
        """

        result = subprocess.run(
            [sys.executable, "-m", "lingxi.apps.worker"],
            cwd=Path(__file__).parents[1],
            env={
                "PATH": os.environ["PATH"],
                "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "LINGXI_WORKER_MODE": "queue",
                "LINGXI_WORKER_READONLY_TOOLS": "mcp__query__noop",
                "LINGXI_WORKER_TRACE_ID": "01J0000000000000000TEST000",
                "LINGXI_POSTGRES_DSN": FAKE_DSN,
            },
            capture_output=True,
            text=True,
            timeout=120,
        )

        combined = result.stdout + result.stderr
        self.assertIn("LINGXI_USER_ENV_ROOT", combined, combined[-800:])
        self.assertNotIn(
            "缺少 LINGXI_POSTGRES_DSN", combined, "入口摘完之后 main 必须仍读得到 DSN"
        )
        self.assertIn("env_detached", combined, "摘除必须在启动日志里留痕")
        self.assertNotIn("hunter2", combined, "启动日志不得回显连接串取值")


@unittest.skipUnless(
    importlib.util.find_spec("claude_agent_sdk"), "跳过：本环境未安装 claude-agent-sdk"
)
class SdkOptionsEnvCannotDeleteAnInheritedVariableTest(unittest.TestCase):
    """回源确认这条修复的前提：SDK 的 ``options.env`` **删不掉**已继承的变量。

    如果哪天上游改成"env 是完整替换"，那就存在一个比改 ``os.environ`` 更干净的
    做法，这条用例会提醒我们回来重看。反过来，只要这一行还在，把修复挪到
    ``adapters/claude_agent_session.py`` 传 ``options.env`` 就是无效的。
    """

    def test_process_env_is_built_by_merging_os_environ_then_options_env(self) -> None:
        import claude_agent_sdk

        source_root = Path(claude_agent_sdk.__file__).parent
        transport = source_root / "_internal" / "transport" / "subprocess_cli.py"
        if not transport.exists():  # pragma: no cover - 上游改了文件布局
            self.skipTest("上游 SDK 文件布局已变，需人工复核这条前提")
        text = transport.read_text(encoding="utf-8")

        self.assertIn("os.environ.items()", text, "SDK 仍然整份继承 os.environ")
        self.assertIn("**self._options.env", text, "options.env 是叠加，不是替换")
        self.assertNotIn(
            "process_env = dict(self._options.env)",
            text,
            "若上游改成完整替换，本条修复的位置需要重新评估",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
