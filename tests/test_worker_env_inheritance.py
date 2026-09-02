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

因此唯一能真正生效的位置是 ``apps/worker/cli.py``：**在起任何回合之前**，把它从
本进程自己的 ``os.environ`` 里摘掉。本文件把这条钉成断言，验收口径与审查报告的
上线前预检一致——``/proc/<pid>/environ`` 里含 DSN 的子进程数 = 0，只不过这里用
本地构造的子进程复现，不连任何机器。
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
    _detach_inherited_database_credentials,
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
        stderr = io.StringIO()

        removed = _detach_inherited_database_credentials(
            err=stderr, trace_id="01J0000000000000000TEST000"
        )

        self.assertEqual(removed, (DSN_VAR,))
        self.assertNotIn(DSN_VAR, os.environ)
        environ_bytes = _child_environ_bytes()
        self.assertNotIn(f"{DSN_VAR}=".encode(), environ_bytes)
        self.assertNotIn(FAKE_DSN.encode(), environ_bytes, "值也不许以别的变量名混进去")

    def test_detaching_is_idempotent_and_silent_when_there_is_nothing_to_remove(self) -> None:
        os.environ.pop(DSN_VAR, None)
        stderr = io.StringIO()

        self.assertEqual(
            _detach_inherited_database_credentials(
                err=stderr, trace_id="01J0000000000000000TEST000"
            ),
            (),
        )
        self.assertEqual(stderr.getvalue(), "", "没摘掉任何东西时不该有日志噪声")

    def test_the_log_records_the_variable_name_but_never_the_value(self) -> None:
        os.environ[DSN_VAR] = FAKE_DSN
        stderr = io.StringIO()

        _detach_inherited_database_credentials(
            err=stderr, trace_id="01J0000000000000000TEST000"
        )

        output = stderr.getvalue()
        self.assertIn(DSN_VAR, output)
        self.assertNotIn("hunter2", output)
        self.assertNotIn("db.invalid", output)
        record = json.loads(output.strip().splitlines()[-1])
        self.assertEqual(record["variables"], [DSN_VAR])


class DetachHappensBeforeAnythingCanSpawnTest(unittest.TestCase):
    """摘除的**位置**是这条修复的全部：晚一步就等于没做。

    源码级断言，因为真正跑一次 queue 模式要连真库、起 SDK。判据是"在 `main` 的
    queue 分支里，摘除调用出现在任何可能起子进程的东西之前"——`PostgresTaskQueue`
    之后才摘，第一个回合就已经把 DSN 带出去了。
    """

    def _queue_branch_calls(self) -> list[str]:
        source = (
            Path(__file__).parents[1] / "src/lingxi/apps/worker/cli.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        main = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )
        names: list[tuple[int, str]] = []
        for node in ast.walk(main):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    names.append((node.lineno, func.id))
                elif isinstance(func, ast.Attribute):
                    names.append((node.lineno, func.attr))
        return [name for _, name in sorted(names)]

    def test_the_detach_precedes_every_adapter_and_service_construction(self) -> None:
        order = self._queue_branch_calls()

        self.assertIn("_detach_inherited_database_credentials", order)
        detach_at = order.index("_detach_inherited_database_credentials")
        for later in ("PostgresTaskQueue", "WorkerService"):
            with self.subTest(after=later):
                self.assertIn(later, order, f"{later} 应当仍在 main 的装配里")
                self.assertLess(
                    detach_at,
                    order.index(later),
                    f"摘除必须早于 {later}——晚一步，第一个回合就已经把 DSN 带出去了",
                )

    def test_the_uninheritable_list_is_not_silently_emptied(self) -> None:
        self.assertIn(DSN_VAR, _UNINHERITABLE_ENV_VARS)


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
