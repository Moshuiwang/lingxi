"""``scripts/ops/host_maintenance_install.sh`` 的假根目录用例（#884 第 2–7 条宿主侧 / #891，Trace #898 W4）。

测试形是「非 root + ``LINGXI_HM_ROOT=<假根>``」：路径加假根前缀、跳过属主设置，``systemctl`` 走
``tests/support/fake_host_maintenance.py`` 的桩（会模拟巡检单元的下一整分轮），注入点背后是一个假解释器。
输入文件取仓库真文件（单元、巡检脚本、备份脚本），清单 ``SHA256SUMS`` 由用例现算——与编排者从 tag 导出的形状相同。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from support.fake_host_maintenance import write_python, write_systemctl

REPOSITORY_ROOT = Path(__file__).parents[1]
SCRIPT = REPOSITORY_ROOT / "scripts" / "ops" / "host_maintenance_install.sh"
CAND_UNIT = REPOSITORY_ROOT / "deploy" / "monitoring-units" / "lingxi-host-monitor.service"
CAND_MON = REPOSITORY_ROOT / "scripts" / "ops" / "host_health_alert.py"
CAND_BAK = REPOSITORY_ROOT / "scripts" / "ops" / "db_backup.sh"
BACKUP_UNIT = REPOSITORY_ROOT / "deploy" / "monitoring-units" / "lingxi-db-backup.service"
ARGS = "/opt/lingxi/scripts/host_health_alert.py --env-file /opt/lingxi/monitoring/host-monitor.env"
DB_ARGS = " --db-container lingxi-db --db-backup-status-file /var/lib/lingxi/db-backup-status.json"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _old_unit(*, user: str | None) -> str:
    """由仓库单元推出「F17 之前的旧版」：去注释、去 TimeoutStartSec、解释器换系统 python3，预发形把 User= 写进本体。"""
    lines = []
    for line in CAND_UNIT.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or line.startswith("TimeoutStartSec="):
            continue
        if line.startswith("ExecStart="):
            line = "ExecStart=/usr/bin/python3 " + ARGS
        lines.append(line)
        if line == "Type=oneshot" and user:
            lines.append("User=" + user)
    return "\n".join(lines) + "\n"


class HostMaintenanceFakeRootTest(unittest.TestCase):
    """以临时目录充当 ``/``，跑 check / apply / restore / status。"""

    environment = "stage"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="lingxi-898-hm-")
        base = Path(self._tmp.name)
        self.root, self.state, self.inputs = base / "root", base / "state", base / "inputs"
        for d in (self.root, self.state, self.inputs):
            d.mkdir()
        self.systemctl = write_systemctl(base / "bin" / "systemctl")
        # 注入点：/opt/lingxi/bin/python3 → 假 3.12
        real_py = write_python(self.root / "usr" / "bin" / "python3.12")
        (self.root / "opt" / "lingxi" / "bin").mkdir(parents=True)
        (self.root / "opt" / "lingxi" / "bin" / "python3").symlink_to(real_py)
        self.write_contract(self.environment)
        # 输入目录：脚本本身 + 三份仓库文件 + 清单
        for src in (SCRIPT, CAND_UNIT, CAND_MON, CAND_BAK):
            shutil.copy2(src, self.inputs / src.name)
        self.write_manifest()
        # 在位现场：旧单元本体 + 三份 drop-in + 旧脚本 + 备份单元 + 定时器 active
        self.unit_dir = self.root / "etc" / "systemd" / "system"
        self.dropin_dir = self.unit_dir / "lingxi-host-monitor.service.d"
        self.dropin_dir.mkdir(parents=True)
        self.frag = self.unit_dir / "lingxi-host-monitor.service"
        self.frag.write_text(_old_unit(user="deployer"), encoding="utf-8")
        self.frag.chmod(0o644)
        (self.dropin_dir / "10-local.conf").write_text(
            "[Service]\nUser=deployer\n", encoding="utf-8"
        )
        self.py_dropin = self.dropin_dir / "20-python312.conf"
        self.py_dropin.write_text(
            "[Service]\nExecStart=\nExecStart=/opt/lingxi/bin/python3 " + ARGS + "\n",
            encoding="utf-8",
        )
        (self.dropin_dir / "30-lingxi-db-checks.conf").write_text(
            "[Service]\n# 由 s31 生成\nExecStart=\nExecStart=/opt/lingxi/bin/python3 "
            + ARGS
            + DB_ARGS
            + "\n",
            encoding="utf-8",
        )
        scripts = self.root / "opt" / "lingxi" / "scripts"
        scripts.mkdir(parents=True)
        self.mon = scripts / "host_health_alert.py"
        self.mon.write_text("# 旧版巡检脚本\n", encoding="utf-8")
        self.mon.chmod(0o755)
        self.bak = scripts / "db_backup.sh"
        self.bak.write_text("#!/usr/bin/env bash\necho old-backup\n", encoding="utf-8")
        self.bak.chmod(0o755)
        shutil.copy2(BACKUP_UNIT, self.unit_dir / BACKUP_UNIT.name)
        (self.state / "lingxi-host-monitor.timer.active").touch()
        # #886 第 1 处的例行 dump 目录：一份 0644、一份 0600
        self.dumps = self.root / "home" / "deployer" / "backups"
        self.dumps.mkdir(parents=True)
        for name, mode in (("a.dump", 0o644), ("b.dump", 0o600)):
            (self.dumps / name).write_text("x", encoding="utf-8")
            (self.dumps / name).chmod(mode)
        self.targets = [self.frag, self.py_dropin, self.mon, self.bak]
        self.before = self.snapshot()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_contract(self, environment: str) -> None:
        contract = self.root / "opt" / "lingxi" / "control" / "host-contract.json"
        contract.parent.mkdir(parents=True, exist_ok=True)
        contract.write_text(json.dumps({"schema": 1, "environment": environment}), encoding="utf-8")

    def write_manifest(self) -> None:
        names = ("lingxi-host-monitor.service", "host_health_alert.py", "db_backup.sh", SCRIPT.name)
        text = "".join(f"{_sha(self.inputs / n)}  {n}\n" for n in names)
        (self.inputs / "SHA256SUMS").write_text(text, encoding="utf-8")

    def snapshot(self) -> dict[str, tuple[str, int]]:
        """假根下所有文件的 (sha, mode)；链接按链接本身记。"""
        out = {}
        for p in sorted(self.root.rglob("*")):
            if p.is_symlink():
                out[str(p)] = (os.readlink(p), 0)
            elif p.is_file():
                out[str(p)] = (_sha(p), p.stat().st_mode & 0o7777)
        return out

    def run_script(self, *args: str, **env_extra: str) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("LINGXI_")}
        env.update(
            {
                "LC_ALL": "C",
                "FAKE_STATE": str(self.state),
                "FAKE_UNIT_DIR": str(self.unit_dir),
                "LINGXI_HM_ROOT": str(self.root),
                "LINGXI_HM_SYSTEMCTL": str(self.systemctl),
                "LINGXI_HM_ALIGN": "0",
                "LINGXI_HM_POLL_SECONDS": "0",
                "LINGXI_HM_ROUND_TIMEOUT": "20",
                "LINGXI_HM_ROUTINE_DUMP_DIR": str(self.dumps),
            }
        )
        env.update(env_extra)
        return subprocess.run(
            ["bash", str(self.inputs / SCRIPT.name), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def assert_ok(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assert_refused(self, result: subprocess.CompletedProcess[str], needle: str) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(needle, result.stdout + result.stderr)
        self.assertEqual(self.snapshot(), self.before, "拒绝时假根不得有任何改动")

    def assert_installed(self) -> None:
        self.assertEqual(_sha(self.frag), _sha(CAND_UNIT))
        self.assertFalse(self.py_dropin.exists())
        self.assertEqual(_sha(self.mon), _sha(CAND_MON))
        self.assertEqual(_sha(self.bak), _sha(CAND_BAK))

    def assert_back_to_original(self) -> None:
        now = self.snapshot()
        for t in self.targets:
            self.assertEqual(now.get(str(t)), self.before[str(t)], f"{t} 未逐字节回到安装前")

    # ---------------- check：零写入 + 前提拒绝 ----------------

    def test_check_passes_and_writes_nothing(self) -> None:
        result = self.run_script("check")
        self.assert_ok(result)
        self.assertIn("前提全部满足", result.stdout)
        self.assertEqual(result.stdout.count("→ install "), 3, result.stdout)
        self.assertEqual(self.snapshot(), self.before)
        self.assertFalse((self.root / "root" / "lingxi-884-backup").exists())

    def test_refuses_system_python_39(self) -> None:
        (self.state / "py_version").write_text("3.9.25\n", encoding="utf-8")
        self.assert_refused(self.run_script("apply", "--yes"), "低于 3.11")

    def test_refuses_missing_injection_link(self) -> None:
        (self.root / "opt" / "lingxi" / "bin" / "python3").unlink()
        self.before = self.snapshot()
        self.assert_refused(
            self.run_script("apply", "--yes"), "注入点 /opt/lingxi/bin/python3 不存在"
        )

    def test_refuses_injection_point_that_is_not_a_link(self) -> None:
        link = self.root / "opt" / "lingxi" / "bin" / "python3"
        target = link.resolve()
        link.unlink()
        shutil.copy2(target, link)
        self.before = self.snapshot()
        self.assert_refused(self.run_script("check"), "不是链接")

    def test_refuses_input_not_matching_manifest(self) -> None:
        with (self.inputs / "host_health_alert.py").open("a", encoding="utf-8") as f:
            f.write("# 篡改\n")
        self.assert_refused(
            self.run_script("apply", "--yes"), "输入 host_health_alert.py 与清单不符"
        )

    def test_refuses_unexpected_unit_diff(self) -> None:
        self.frag.write_text(_old_unit(user="deployer") + "Restart=on-failure\n", encoding="utf-8")
        self.before = self.snapshot()
        self.assert_refused(self.run_script("apply", "--yes"), "意外差异")

    def test_refuses_candidate_that_cannot_import_under_injection_point(self) -> None:
        (self.state / "py_help_fail").touch()
        self.assert_refused(self.run_script("apply", "--yes"), "在注入点解释器下起不来")

    def test_refuses_dropin_override_off_injection_point(self) -> None:
        (self.dropin_dir / "30-lingxi-db-checks.conf").write_text(
            "[Service]\nExecStart=\nExecStart=/usr/bin/python3 " + ARGS + DB_ARGS + "\n",
            encoding="utf-8",
        )
        self.before = self.snapshot()
        self.assert_refused(self.run_script("apply", "--yes"), "不走注入点")

    def test_apply_requires_yes(self) -> None:
        self.assert_refused(self.run_script("apply"), "apply --yes")

    # ---------------- apply：正常、两处自动回装、幂等、restore ----------------

    def test_apply_installs_all_three_and_waits_two_rounds(self) -> None:
        result = self.run_script("apply", "--yes")
        self.assert_ok(result)
        self.assert_installed()
        self.assertEqual(result.stdout.count("[等轮] 新轮"), 2, result.stdout)
        self.assertIn(
            "ExecStart 解释器=/opt/lingxi/bin/python3 User=deployer TimeoutStartUSec=50s",
            result.stdout,
        )
        self.assertLess(result.stdout.index("① 回读"), result.stdout.index("② 回读"))
        backups = list((self.root / "root" / "lingxi-884-backup").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o700)
        self.assertIn("[下次备份轮回读]", result.stdout)
        self.assertIn("dump 共 2 份，非 0600 的 1 份", result.stdout)

    def test_unit_round_failure_restores_everything(self) -> None:
        (self.state / "rounds.txt").write_text("exit-code 1\n", encoding="utf-8")
        result = self.run_script("apply", "--yes")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("第 ① 步不符 → 自动回装", result.stdout)
        self.assertNotIn("② 回读", result.stdout)
        self.assert_back_to_original()

    def test_script_round_failure_restores_everything(self) -> None:
        (self.state / "rounds.txt").write_text("success 0\nexit-code 1\n", encoding="utf-8")
        result = self.run_script("apply", "--yes")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("第 ② 步不符 → 自动回装", result.stdout)
        self.assert_back_to_original()

    def test_restore_removes_target_that_did_not_exist_before(self) -> None:
        """在位巡检脚本原本不存在：装上后下一轮失败，回装须把它删掉而不是留下新版。"""
        self.mon.unlink()
        self.before = self.snapshot()
        (self.state / "rounds.txt").write_text("success 0\nexit-code 1\n", encoding="utf-8")
        result = self.run_script("apply", "--yes")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(self.mon.exists(), result.stdout)
        self.targets.remove(self.mon)
        self.assert_back_to_original()

    def test_no_round_within_timeout_restores(self) -> None:
        (self.state / "no_rounds").touch()
        result = self.run_script(
            "apply", "--yes", LINGXI_HM_ROUND_TIMEOUT="1", LINGXI_HM_POLL_SECONDS="0.1"
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("1 秒内没有新一轮结束", result.stdout)
        self.assert_back_to_original()

    def test_second_apply_is_all_already(self) -> None:
        self.assert_ok(self.run_script("apply", "--yes"))
        installed = self.snapshot()
        second = self.run_script("apply", "--yes")
        self.assert_ok(second)
        for mark in ("① 单元本体", "② 巡检脚本", "③ 备份脚本"):
            self.assertRegex(second.stdout, rf"\[计划\] {mark} .* → already")
        self.assertIn("三步全部 already", second.stdout)
        self.assertEqual(self.snapshot(), installed, "二跑不得改动任何文件（含不新建备份）")

    def test_restore_is_byte_identical(self) -> None:
        self.assert_ok(self.run_script("apply", "--yes"))
        self.assert_installed()
        result = self.run_script("restore", "--yes")
        self.assert_ok(result)
        self.assert_back_to_original()
        self.assertEqual(result.stdout.count("逐字节一致"), 4, result.stdout)

    def test_status_is_read_only(self) -> None:
        result = self.run_script("status")
        self.assert_ok(result)
        self.assertIn("[#886 第 1 处]", result.stdout)
        self.assertEqual(self.snapshot(), self.before)

    def test_stage_body_user_must_match_local_dropin(self) -> None:
        """预发：本体 User= 与 10-local.conf 不同值即拒绝（生产形由子类的专门用例覆盖）。"""
        if self.environment != "stage":
            self.skipTest("预发专属")
        (self.dropin_dir / "10-local.conf").write_text(
            "[Service]\nUser=someone-else\n", encoding="utf-8"
        )
        self.before = self.snapshot()
        self.assert_refused(self.run_script("check"), "10-local.conf 不同值")


class HostMaintenanceProductionTest(HostMaintenanceFakeRootTest):
    """生产契约：本体不带 User=、sha 须是已知旧版；带 User= 的本体差异拒绝。父类全部用例在生产形下再跑一遍。"""

    environment = "production"

    def setUp(self) -> None:
        super().setUp()
        self.frag.write_text(_old_unit(user=None), encoding="utf-8")
        self.before = self.snapshot()

    def run_script(self, *args: str, **env_extra: str) -> subprocess.CompletedProcess[str]:
        env_extra.setdefault(
            "LINGXI_HM_PROD_OLD_UNIT_SHA", _sha(self.frag) if self.frag.exists() else "0" * 64
        )
        return super().run_script(*args, **env_extra)

    def test_refuses_unexpected_unit_diff(self) -> None:
        self.frag.write_text(_old_unit(user=None) + "Restart=on-failure\n", encoding="utf-8")
        self.before = self.snapshot()
        self.assert_refused(self.run_script("apply", "--yes"), "意外差异")

    def test_production_refuses_body_user_line(self) -> None:
        self.frag.write_text(_old_unit(user="deployer"), encoding="utf-8")
        self.before = self.snapshot()
        self.assert_refused(self.run_script("check"), "生产在位单元本体含 User=")

    def test_production_refuses_unknown_old_unit_sha(self) -> None:
        result = self.run_script("check", LINGXI_HM_PROD_OLD_UNIT_SHA="1" * 64)
        self.assert_refused(result, "既不是已知旧版也不是候选版")

    def test_same_body_user_is_accepted_on_stage_only(self) -> None:
        """同一份「本体带 User=」现场：预发契约下放行、生产契约下拒绝。"""
        self.frag.write_text(_old_unit(user="deployer"), encoding="utf-8")
        self.before = self.snapshot()
        self.assert_refused(self.run_script("check"), "生产在位单元本体含 User=")
        self.write_contract("stage")
        self.assert_ok(self.run_script("check"))


if __name__ == "__main__":
    unittest.main()
