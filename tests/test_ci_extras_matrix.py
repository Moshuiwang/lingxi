"""extras 三方对账的用例：制品 ``Provides-Extra`` ↔ 本脚本条目 ↔ ci.yml 矩阵。

独立复查实测过一个漏洞：把新组加进 ``pyproject.toml`` 和 ``PROCESS_RUNTIME_IMPORTS``、
**唯独漏掉 ci.yml 的矩阵行**，CI 就从没在干净环境里装过它，而 gate 全绿。当时
代码框架写着「这条不靠自觉」，实际只对了一半——脚本半边有对账，矩阵半边靠自觉。
本文件锁住补上的那半边。

绝大多数用例把 ci.yml 内容**作为参数传入**，不依赖真实文件当前长什么样（否则
改一行 CI 配置就会连带改坏这些用例）；真实文件的自洽另有最后一条用例单独覆盖。
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "check_installed_package.py"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _load_script():
    """按 tests/test_galaxy_import_script.py 的既有写法加载门禁脚本。"""

    spec = importlib.util.spec_from_file_location("check_installed_package_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CHECKER = _load_script()


def _workflow(matrix_line: str) -> str:
    """最小 ci.yml 片段，只保留矩阵行周围的形状。"""

    return "jobs:\n  extras:\n    strategy:\n      matrix:\n" + matrix_line + "\n"


class CiMatrixReconciliationTest(unittest.TestCase):
    def test_extra_missing_from_the_matrix_is_reported(self) -> None:
        """独立复查抓到的那个漏洞：漏加矩阵行必须报错，不能静默通过。"""

        failures = CHECKER.check_ci_matrix(
            {"scheduler", "worker", "bot-test"},
            _workflow("        extra: [scheduler, worker]"),
        )

        self.assertTrue(failures)
        self.assertIn("bot-test", " ".join(failures))

    def test_matrix_in_sync_passes(self) -> None:
        failures = CHECKER.check_ci_matrix(
            {"scheduler", "worker", "bot-test"},
            _workflow("        extra: [scheduler, worker, bot-test]"),
        )

        self.assertEqual([], failures)

    def test_quoted_names_are_accepted(self) -> None:
        """YAML 允许给字符串加引号；加了引号不该被误判成漏加。"""

        failures = CHECKER.check_ci_matrix(
            {"scheduler", "bot-test"},
            _workflow("        extra: ['scheduler', \"bot-test\"]"),
        )

        self.assertEqual([], failures)

    def test_matrix_leg_for_an_undeclared_extra_is_reported(self) -> None:
        """反向：矩阵里有制品没声明的组，那条腿必然失败，应提前报出来。"""

        failures = CHECKER.check_ci_matrix(
            {"scheduler"},
            _workflow("        extra: [scheduler, gateway]"),
        )

        self.assertTrue(failures)
        self.assertIn("gateway", " ".join(failures))

    def test_matrix_written_as_a_yaml_block_list_fails_loudly(self) -> None:
        """本脚本只认单行写法；改成多行列表时必须失败，而不是当作「没问题」。"""

        failures = CHECKER.check_ci_matrix(
            {"scheduler"},
            _workflow("        extra:\n          - scheduler"),
        )

        self.assertTrue(failures)

    def test_unreadable_workflow_fails(self) -> None:
        """读不到 CI 配置时同样失败——静默跳过比不检查更危险。"""

        self.assertTrue(CHECKER.check_ci_matrix({"scheduler"}, None))


class RealRepositoryStateTest(unittest.TestCase):
    def test_real_ci_yml_matrix_covers_every_process_group(self) -> None:
        """仓库当前状态必须自洽：每个进程组都在 CI 矩阵里。

        这一条刻意不读已安装制品的元数据（跑单测的环境未必装了 lingxi），直接拿
        ``PROCESS_RUNTIME_IMPORTS`` 当 extras 全集；它与制品声明的一致性由
        ``check_declared_extras`` 在 CI 里保证。
        """

        matrix = CHECKER.ci_matrix_extras(CI_WORKFLOW.read_text(encoding="utf-8"))

        self.assertIsNotNone(matrix, "ci.yml 里找不到 `extra: [...]` 矩阵行")
        self.assertEqual(
            set(CHECKER.PROCESS_RUNTIME_IMPORTS) - CHECKER.MATRIX_EXEMPT_EXTRAS,
            matrix,
        )


if __name__ == "__main__":
    unittest.main()
