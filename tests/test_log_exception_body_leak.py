"""异常正文不得进日志（对抗审查 2026-09-02 C-6）。

``logger.exception(...)`` 与 ``logger.error(..., exc_info=…)`` 记的是**完整异常链**，
其中包含异常自己写的那句话。本仓库的异常大量来自 psycopg：

- 唯一键冲突的正文形如 ``DETAIL: Key (feishu_open_id)=(ou_…) already exists``，
  把一个真实飞书标识原样写进日志，违 ``V-花名册-33``（标识只以脱敏形式进日志）；
- 连接失败的正文带着 host / user / dbname。

审查在 ``onboarding_runner.py``、``apps/scheduler/onboarding.py``、
``apps/scheduler/__init__.py`` 三处点名了这个形态；本文件把它变成**整仓库的棘轮**：
任何新写的 ``logger.exception`` 或 ``exc_info=`` 都会在这里变红。

替代写法是「类型名 + 调用栈帧」——``type(error).__name__`` 回答"失败的是哪一类"，
``traceback.format_tb(error.__traceback__)`` 回答"在哪失败"，两者合起来保住了全部
排障价值，而异常自己写的那句话一个字都不进日志。

**已知边界（有意接受）**：``format_tb`` 会带上每一帧的**本仓库源码行**。仓库源码里
不存在硬编码的凭据或用户标识（镜像本身就随 GHCR 公开，见部署面 D-1），因此这不是
泄露面；但它意味着"把秘密写成源码字面量"这件事本身会经由调用栈进日志——这条约束
本来就成立，这里只是明确登记。
"""

from __future__ import annotations

import ast
import logging
import threading
import unittest
from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[1] / "src" / "lingxi"

#: 被判定为「日志器」的名字。取最后一段：``logger``、``self._logger``、
#: ``logging`` 都算，``concurrent.futures.Future.exception()`` 这类同名方法不算
#: （``finished.exception()`` 在 ``apps/worker/service.py`` 里是 Future 的取值方法，
#: 不是日志调用——按名字区分是唯一不需要类型推断的可靠判据）。
LOGGER_NAMES = frozenset({"logger", "log", "logging", "_logger", "LOGGER", "_log"})


def _receiver_name(node: ast.expr) -> str | None:
    """取调用接收者的最后一段名字。"""

    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _scan(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    findings: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        receiver = _receiver_name(node.func.value)
        if receiver not in LOGGER_NAMES:
            continue
        if node.func.attr == "exception":
            findings.append(f"{path}:{node.lineno} {receiver}.exception(…)")
        for keyword in node.keywords:
            if keyword.arg == "exc_info":
                findings.append(
                    f"{path}:{node.lineno} {receiver}.{node.func.attr}(…, exc_info=…)"
                )
    return findings


class NoExceptionBodyInLogsTest(unittest.TestCase):
    """整仓库棘轮：没有任何日志调用把异常正文写出去。"""

    def test_no_source_file_logs_an_exception_body(self) -> None:
        findings: list[str] = []
        for path in sorted(SOURCE_ROOT.rglob("*.py")):
            findings.extend(_scan(path))

        self.assertEqual(
            findings,
            [],
            "异常正文会带出 psycopg 的 `Key (feishu_open_id)=(ou_…)` 与连接串信息；"
            "改用 `type(error).__name__` + `traceback.format_tb(error.__traceback__)`。",
        )

    def test_the_scanner_actually_catches_both_shapes(self) -> None:
        """棘轮自己要有反例，否则它可能只是**扫不到东西**而绿。"""

        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            sample = Path(directory) / "sample.py"
            sample.write_text(
                "import logging\n"
                "logger = logging.getLogger(__name__)\n"
                "def a(error):\n"
                "    logger.exception('boom')\n"
                "def b(error):\n"
                "    logger.error('boom', exc_info=error)\n"
                "def c(self, finished):\n"
                "    return finished.exception()\n",
                encoding="utf-8",
            )

            findings = _scan(sample)

        self.assertEqual(len(findings), 2, f"应恰好抓到两条，实际 {findings}")
        self.assertTrue(any("exception(…)" in item for item in findings))
        self.assertTrue(any("exc_info" in item for item in findings))
        self.assertFalse(
            any("finished" in item for item in findings),
            "Future.exception() 与日志无关，误判会逼着后来的人给它加豁免",
        )


class OnboardingExecutorFailureLogTest(unittest.TestCase):
    """真实路径断言：开通链在执行线程上抛异常时，异常正文不进日志。

    ``apps/scheduler/onboarding.py`` 的这条兜底捕获接的是**整条开通链**的任意异常，
    真库上最常见的就是 psycopg 的唯一键冲突——正文里带着真实 ``open_id``。
    """

    def test_a_failing_chain_logs_the_type_and_frames_but_not_the_body(self) -> None:
        from lingxi.apps.scheduler.onboarding import OnboardingExecutor

        # 秘密只经**变量**进入异常，绝不写成源码字面量：`format_tb` 会带上源码行，
        # 字面量会让这条用例自己制造出它要检测的泄露（模块文档「已知边界」一节）。
        secret = "Key (feishu_open_id)=(ou_MUST_NOT_REACH_THE_LOG) already exists"
        finished = threading.Event()

        executor = OnboardingExecutor(workers=1, stop_poll_seconds=0.05)
        self.addCleanup(executor.stop)
        executor.start()

        def failing_chain() -> None:
            raise RuntimeError(secret)

        with self.assertLogs("lingxi.apps.scheduler.onboarding", level=logging.ERROR) as captured:
            self.assertTrue(executor.submit(failing_chain))
            # 单线程 + FIFO：这一条一定排在失败那条**之后**执行，因此它跑完就说明
            # 上一条的 except 与 finally 都已经走完，日志也已经写出。
            self.assertTrue(executor.submit(finished.set))
            self.assertTrue(finished.wait(timeout=5), "执行线程没有在预算内收尾")

        message = "\n".join(captured.output)

        self.assertNotIn("ou_MUST_NOT_REACH_THE_LOG", message, "真实标识不得进日志")
        self.assertNotIn("already exists", message, "异常正文不得进日志")
        self.assertIn("RuntimeError", message, "失败的是哪一类必须留下")
        self.assertIn("failing_chain", message, "在哪失败必须留下")
        self.assertIn("onboarding.py", message, "调用栈必须指向真实代码位置")

    def test_the_thread_survives_the_failure(self) -> None:
        """脱敏改写不得顺手改掉「一条链的失败不带走这条线程」这条既有语义。"""

        from lingxi.apps.scheduler.onboarding import OnboardingExecutor

        after = threading.Event()
        executor = OnboardingExecutor(workers=1, stop_poll_seconds=0.05)
        self.addCleanup(executor.stop)
        executor.start()

        with self.assertLogs("lingxi.apps.scheduler.onboarding", level=logging.ERROR):
            executor.submit(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
            executor.submit(after.set)
            self.assertTrue(after.wait(timeout=5))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
