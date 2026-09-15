"""宿主侧 systemd 单元与采样脚本只认同一个 Python 注入点（Trace #770 W4 F17）。

2026-09-15 预发实读：主机默认 ``python3`` 是 3.9，而宿主侧脚本与控制包要求 3.11 以上；
仓库单元写死 ``/usr/bin/python3``、``resource_sample.sh`` 从 PATH 取 ``python3``，结果
资源采样每分钟失败 80 分钟无人察觉、阈值告警候选版无法启用。修法是把解释器收成
**一个**注入点：固定符号链接 ``/opt/lingxi/bin/python3``，由引导安装用该机的
``<PYTHON_ABSOLUTE_PATH>`` 建链；仓库单元逐字相同，两台主机只差链接目标。

这里钉住的是"不会再漂回去"：任何 ``.service`` 的活动行不得再出现系统 ``python3``
（含 ``/usr/bin/python3.12`` 这类把某台机器的路径写进仓库的写法）、采样脚本不得裸调
``python3``、drop-in 样例不得用 ``ExecStart=`` 覆盖来换解释器。

变异实测：把 ``lingxi-host-monitor.service`` 的 ``ExecStart`` 改回 ``/usr/bin/python3``
跑本文件应判红；还原后复绿。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
UNITS_DIR = ROOT / "deploy" / "monitoring-units"
RESOURCE_SAMPLE_SH = ROOT / "scripts" / "ops" / "monitoring" / "resource_sample.sh"
DROP_IN_EXAMPLE = UNITS_DIR / "10-local.conf.example"

#: 唯一注入点：引导安装建的固定符号链接，目标是前提核对定下的 <PYTHON_ABSOLUTE_PATH>。
PYTHON_INJECTION_POINT = "/opt/lingxi/bin/python3"

#: 派发卡点名的五个单元：监控四单元 + 拉取代理。前三个真的起 Python，后两个只起 bash + psql，
#: 一并钉住是为了让"以后有人往里加一条 python3"也立刻判红。
PINNED_UNITS = (
    "lingxi-host-monitor.service",
    "lingxi-resource-sample.service",
    "lingxi-release-pull.service",
    "lingxi-db-business-sample.service",
    "lingxi-monitoring-push.service",
)
EXEC_START_VIA_INJECTION_POINT = ("lingxi-host-monitor.service", "lingxi-release-pull.service")


def _active_lines(text: str) -> list[str]:
    """去掉整行注释与空行；systemd 只认行首 ``#`` / ``;`` 为注释。"""

    return [
        line
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", ";"))
    ]


def _unit_text(name: str) -> str:
    return (UNITS_DIR / name).read_text(encoding="utf-8")


class MonitoringUnitsPythonPinTest(unittest.TestCase):
    """五个 ``.service`` 不再依赖系统 ``python3``。"""

    def test_the_pinned_units_exist(self) -> None:
        for name in PINNED_UNITS:
            self.assertTrue((UNITS_DIR / name).is_file(), name)

    def test_no_service_unit_hardcodes_a_system_python(self) -> None:
        # 目录下全部 .service 都查（派发卡点名的五个是其子集），新加单元同样受约束。
        for path in sorted(UNITS_DIR.glob("*.service")):
            for line in _active_lines(path.read_text(encoding="utf-8")):
                self.assertNotIn(
                    "/usr/bin/python",
                    line,
                    f"{path.name} 写死了系统 Python：{line!r}——两台主机路径可能不同，"
                    f"仓库单元只能指向注入点 {PYTHON_INJECTION_POINT}",
                )
                self.assertNotRegex(
                    line,
                    r"/usr/bin/env\s+python",
                    f"{path.name} 从 PATH 取 python：{line!r}",
                )

    def test_python_units_exec_start_begins_with_the_injection_point(self) -> None:
        for name in EXEC_START_VIA_INJECTION_POINT:
            exec_lines = [
                line for line in _active_lines(_unit_text(name)) if line.startswith("ExecStart=")
            ]
            self.assertEqual(len(exec_lines), 1, name)
            argv = exec_lines[0].removeprefix("ExecStart=").split()
            self.assertEqual(argv[0], PYTHON_INJECTION_POINT, f"{name}: {exec_lines[0]!r}")

    def test_release_pull_keeps_no_bytecode_flags(self) -> None:
        """换解释器不能顺手丢掉 F12 的两道禁写字节码缓存。"""

        text = _unit_text("lingxi-release-pull.service")
        exec_line = next(line for line in _active_lines(text) if line.startswith("ExecStart="))
        self.assertEqual(exec_line.removeprefix("ExecStart=").split()[1], "-B")
        self.assertIn("Environment=PYTHONDONTWRITEBYTECODE=1", _active_lines(text))

    def test_resource_sample_unit_injects_the_same_interpreter(self) -> None:
        self.assertIn(
            f"Environment=LINGXI_PYTHON={PYTHON_INJECTION_POINT}",
            _active_lines(_unit_text("lingxi-resource-sample.service")),
        )

    def test_bash_only_units_do_not_mention_python_at_all(self) -> None:
        for name in ("lingxi-db-business-sample.service", "lingxi-monitoring-push.service"):
            for line in _active_lines(_unit_text(name)):
                self.assertNotIn("python", line.lower(), f"{name}: {line!r}")


class ResourceSampleScriptPythonPinTest(unittest.TestCase):
    """``resource_sample.sh`` 不再裸调 ``python3``，且版本不足不静默。"""

    def _active_script_lines(self) -> list[str]:
        return [
            line
            for line in RESOURCE_SAMPLE_SH.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

    def test_no_bare_python3_invocation(self) -> None:
        # 裸 python3：不以 / - _ . 等接在前面、后面也不接 } . - 的独立词——
        # 排除 "/opt/lingxi/bin/python3" 与 "${LINGXI_PYTHON:-/opt/lingxi/bin/python3}"。
        bare = re.compile(r"(?<![\w/.\-])python3(?![\w.\-}])")
        for line in self._active_script_lines():
            self.assertIsNone(bare.search(line), f"resource_sample.sh 裸调了 python3：{line!r}")

    def test_default_interpreter_is_the_injection_point_and_is_configurable(self) -> None:
        text = RESOURCE_SAMPLE_SH.read_text(encoding="utf-8")
        self.assertIn(f'PYTHON="${{LINGXI_PYTHON:-{PYTHON_INJECTION_POINT}}}"', text)
        self.assertIn('"${PYTHON}" "${SCRIPT_DIR}/_resource_sample.py"', text)

    def test_version_floor_is_checked_before_sampling(self) -> None:
        lines = self._active_script_lines()
        check_index = next(
            index for index, line in enumerate(lines) if "sys.version_info >= (3, 11)" in line
        )
        sample_index = next(
            index for index, line in enumerate(lines) if "_resource_sample.py" in line
        )
        self.assertLess(check_index, sample_index, "版本核对必须在采样调用之前")


class DropInExampleTest(unittest.TestCase):
    """本机 drop-in 只提供 ``User=``，不承载解释器路径。"""

    def test_drop_in_example_does_not_override_exec_start(self) -> None:
        active = _active_lines(DROP_IN_EXAMPLE.read_text(encoding="utf-8"))
        self.assertEqual(active, ["[Service]", "User=<部署用户>"])

    def test_drop_in_example_explains_where_python_comes_from(self) -> None:
        text = DROP_IN_EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("Python 从哪来", text)
        self.assertIn(PYTHON_INJECTION_POINT, text)


class DocumentsNameTheInjectionPointTest(unittest.TestCase):
    """安装文档写明建链、核对与前提，运维不用回头猜单元里那条路径从哪来。"""

    def test_monitoring_doc_has_the_injection_point_section(self) -> None:
        text = (ROOT / "deploy" / "监控告警.md").read_text(encoding="utf-8")
        self.assertIn("Python 注入点", text)
        self.assertIn(f"ln -sfn <PYTHON_ABSOLUTE_PATH> {PYTHON_INJECTION_POINT}", text)
        self.assertIn(f"{PYTHON_INJECTION_POINT} --version", text)
        # 备选 crontab 与 --dry-run 示例同样走注入点，不再示范系统 python3。
        self.assertNotIn("/usr/bin/python3", text)

    def test_bootstrap_doc_ties_units_to_the_same_python(self) -> None:
        text = (ROOT / "deploy" / "control" / "引导安装.md").read_text(encoding="utf-8")
        self.assertIn(PYTHON_INJECTION_POINT, text)
        self.assertIn("<PYTHON_ABSOLUTE_PATH>", text)

    def test_pull_agent_doc_and_runbook_mention_the_readback(self) -> None:
        for relative in ("deploy/拉取代理.md", "deploy/生产部署runbook.md"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn(PYTHON_INJECTION_POINT, text, relative)


if __name__ == "__main__":
    unittest.main()
