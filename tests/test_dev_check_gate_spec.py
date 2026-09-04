"""`scripts/dev/gate_spec.py` 的钉住用例（Issue #236）。

覆盖两件事：
1. 对**真实** `.github/workflows/{ci,story}.yml` 解析出的当前配方——这条用例会在
   工作流真正改动 extras / shellcheck 版本 / Python 版本 / 真库参数时提醒维护者
   同步更新本用例的期望值，避免「解析逻辑改了但没人核对解析结果」。
2. 喂违规输入（把 gate 的 pip install 行改成解析器不认识的形态）验红——这是
   Issue #236「extras 清单与门禁同一事实源，两处不一致时门禁变红」的具体落点：
   `scripts/dev/check.sh` 不会另抄一份 extras 清单，它现读 ci.yml；因此这里要验证
   的不是「两份清单互相核对」，而是「读不出来就必须响亮失败，不能安静地退回旧值」——
   退回旧值等价于本工具自己制造出一次它要消灭的漂移。
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
SCRIPT = REPO_ROOT / "scripts" / "dev" / "gate_spec.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("gate_spec_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


GATE_SPEC = _load_script()

MINIMAL_GATE_JOB = """\
jobs:
  classify:
    name: Epic / classify
  gate:
    name: Epic Full / gate
    services:
      postgres:
        image: postgres:16-alpine
        env:
          POSTGRES_HOST_AUTH_METHOD: trust
          POSTGRES_DB: lingxi_test
    steps:
      - name: 配置 Python 运行时
        with:
          python-version: '3.12'
      - name: 安装测试依赖与锁定版本的 shellcheck
        run: python3 -m pip install '.[scheduler,migrate]' 'shellcheck-py==0.11.0.1' 'ruff==0.16.4'
      - name: 真实 Agent SDK 冒烟（不调模型、不用凭据）
        run: |
          python3 -m pip install '.[worker]'
          python3 "${GITHUB_WORKSPACE}/scripts/ci/check_agent_sdk_binding.py"
  extras:
    name: Epic Full / extras
"""

MINIMAL_FAST_JOB = """\
jobs:
  classify:
    name: Story / classify
  fast:
    name: Story / code fast
    steps:
      - name: 配置 Python 运行时
        with:
          python-version: '3.12'
      - name: 安装快速门禁依赖
        run: python3 -m pip install '.[scheduler,migrate,worker]' 'shellcheck-py==0.11.0.1' 'ruff==0.16.4'
  full:
    name: Story / high-risk full
"""


class GateSpecOnSyntheticWorkflowTest(unittest.TestCase):
    """用最小片段固定解析规则；真实文件改了不会连带改坏这些用例。"""

    def test_parses_gate_job_extras_shellcheck_python_and_postgres(self) -> None:
        spec = GATE_SPEC.parse_gate_spec(MINIMAL_GATE_JOB)

        self.assertEqual(spec.extras, ["scheduler", "migrate", "worker"])
        self.assertEqual(spec.shellcheck_version, "0.11.0.1")
        self.assertEqual(spec.ruff_version, "0.16.4")
        self.assertEqual(spec.python_version, "3.12")
        self.assertEqual(
            spec.postgres,
            {"image": "postgres:16-alpine", "auth_method": "trust", "db": "lingxi_test"},
        )

    def test_parses_fast_job(self) -> None:
        spec = GATE_SPEC.parse_fast_spec(MINIMAL_FAST_JOB)

        self.assertEqual(spec.extras, ["scheduler", "migrate", "worker"])
        self.assertEqual(spec.shellcheck_version, "0.11.0.1")
        self.assertEqual(spec.ruff_version, "0.16.4")
        self.assertEqual(spec.python_version, "3.12")


class GateSpecFailsLoudOnUnexpectedShapeTest(unittest.TestCase):
    """喂违规输入验红：解析器认不出的写法必须报错，不能吃掉差异退回旧值。"""

    def test_missing_gate_job_raises(self) -> None:
        with self.assertRaises(GATE_SPEC.GateSpecError):
            GATE_SPEC.parse_gate_spec("jobs:\n  classify:\n    name: x\n")

    def test_pip_install_written_as_multiline_list_is_rejected(self) -> None:
        """ci.yml 若把 extras 从单行 `'.[a,b]'` 改成本解析器不认的形态，必须失败。"""

        broken = MINIMAL_GATE_JOB.replace(
            "run: python3 -m pip install '.[scheduler,migrate]' 'shellcheck-py==0.11.0.1' 'ruff==0.16.4'",
            "run: python3 -m pip install --requirement gate-requirements.txt",
        )
        with self.assertRaises(GATE_SPEC.GateSpecError):
            GATE_SPEC.parse_gate_spec(broken)

    def test_missing_shellcheck_pin_is_rejected(self) -> None:
        broken = MINIMAL_GATE_JOB.replace(
            "run: python3 -m pip install '.[scheduler,migrate]' 'shellcheck-py==0.11.0.1' 'ruff==0.16.4'",
            "run: python3 -m pip install '.[scheduler,migrate]' 'ruff==0.16.4'",
        )
        with self.assertRaises(GATE_SPEC.GateSpecError):
            GATE_SPEC.parse_gate_spec(broken)

    def test_missing_ruff_pin_is_rejected(self) -> None:
        broken = MINIMAL_GATE_JOB.replace(
            "run: python3 -m pip install '.[scheduler,migrate]' 'shellcheck-py==0.11.0.1' 'ruff==0.16.4'",
            "run: python3 -m pip install '.[scheduler,migrate]' 'shellcheck-py==0.11.0.1'",
        )
        with self.assertRaises(GATE_SPEC.GateSpecError):
            GATE_SPEC.parse_gate_spec(broken)

    def test_missing_postgres_service_is_rejected(self) -> None:
        broken = MINIMAL_GATE_JOB.replace(
            "      env:\n          POSTGRES_HOST_AUTH_METHOD: trust\n          POSTGRES_DB: lingxi_test\n",
            "",
        )
        with self.assertRaises(GATE_SPEC.GateSpecError):
            GATE_SPEC.parse_gate_spec(broken)

    def test_missing_python_version_is_rejected(self) -> None:
        broken = MINIMAL_GATE_JOB.replace("          python-version: '3.12'\n", "")
        with self.assertRaises(GATE_SPEC.GateSpecError):
            GATE_SPEC.parse_gate_spec(broken)


class GateSpecOnRealWorkflowFilesTest(unittest.TestCase):
    """对当前仓库真实的 ci.yml / story.yml 解析一次，锁住「现在读到的就是这些值」。

    这条用例会随门禁真正的 extras / 版本变化而需要同步更新期望值——这正是意图：
    它是「本机与门禁同一事实源」这条断言唯一会随门禁一起变红的用例。
    """

    def test_real_gate_job_matches_currently_declared_recipe(self) -> None:
        spec = GATE_SPEC.load_gate_spec()

        self.assertEqual(spec.extras, ["scheduler", "migrate", "worker"])
        self.assertEqual(spec.shellcheck_version, "0.11.0.1")
        self.assertEqual(spec.ruff_version, "0.16.4")
        self.assertEqual(spec.python_version, "3.12")
        self.assertEqual(spec.postgres["image"], "postgres:16-alpine")
        self.assertEqual(spec.postgres["auth_method"], "trust")
        self.assertEqual(spec.postgres["db"], "lingxi_test")

    def test_real_fast_job_matches_currently_declared_recipe(self) -> None:
        spec = GATE_SPEC.load_fast_spec()

        self.assertEqual(spec.extras, ["scheduler", "migrate", "worker"])
        self.assertEqual(spec.shellcheck_version, "0.11.0.1")
        self.assertEqual(spec.ruff_version, "0.16.4")
        self.assertEqual(spec.python_version, "3.12")


if __name__ == "__main__":
    unittest.main()
