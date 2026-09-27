"""``scripts/ops/db_backup_install.sh`` 的假根目录用例（#896 入仓；#886 第 2 处回读判据）。

脚本的测试形是「非 root + ``LINGXI_S31_ROOT=<假根>``」：所有落位路径加假根前缀、跳过属主设置，
``systemctl`` / ``ssh`` / ``scp`` 走 ``tests/support/fake_db_switch.py`` 里的桩。这里只跑不碰真库的子命令
（dry / apply / monitor-script / monitor-enable / 输入校验），断言文件系统上的实际结果与脚本打印的判据行。
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from support.fake_db_switch import base_env, install_stubs, write_executable

REPOSITORY_ROOT = Path(__file__).parents[1]
SCRIPT = REPOSITORY_ROOT / "scripts" / "ops" / "db_backup_install.sh"
SECRET_SENTINEL = "s31-sentinel-9f3c"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DbBackupInstallFakeRootTest(unittest.TestCase):
    """以临时目录充当 ``/``，跑安装脚本的幂等安装、回读判据与输入校验。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="lingxi-896-s31-")
        base = Path(self._tmp.name)
        self.root = base / "root"
        self.state = base / "state"
        self.src = base / "src"
        self.bin = base / "bin"
        for directory in (self.root, self.state, self.src):
            directory.mkdir()
        self.stubs = install_stubs(self.bin)
        self.unit_dir = self.root / "etc" / "systemd" / "system"
        self.unit_dir.mkdir(parents=True)
        # 四份来源副本：内容是假的，sha 由用例现算，脚本只按 sha 与形状判断
        write_executable(self.src / "db_backup.sh", "#!/usr/bin/env bash\necho fake-backup\n")
        (self.src / "lingxi-db-backup.service").write_text(
            "[Service]\nType=oneshot\nUser=root\nExecStart=/opt/lingxi/scripts/db_backup.sh\n",
            encoding="utf-8",
        )
        (self.src / "lingxi-db-backup.timer").write_text(
            "[Timer]\nOnCalendar=*-*-* 18:30:00\n", encoding="utf-8"
        )
        write_executable(
            self.src / "host_health_alert.py",
            "#!/usr/bin/env python3\n# 支持 --db-container 等本地库检查项\n",
        )
        (self.unit_dir / "lingxi-host-monitor.service").write_text(
            "[Service]\nExecStart=/opt/lingxi/bin/python3 /opt/lingxi/scripts/host_health_alert.py"
            " --once\n",
            encoding="utf-8",
        )
        self.env_file = base / "db_backup_install.env"
        self.write_inputs()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_inputs(self, **overrides: str) -> None:
        values = {
            "LINGXI_S31_SCRIPT_SRC": str(self.src / "db_backup.sh"),
            "LINGXI_S31_UNIT_SRC_DIR": str(self.src),
            "LINGXI_S31_MONITOR_SCRIPT_SRC": str(self.src / "host_health_alert.py"),
            "LINGXI_S31_SCRIPT_SHA": _sha(self.src / "db_backup.sh"),
            "LINGXI_S31_SERVICE_SHA": _sha(self.src / "lingxi-db-backup.service"),
            "LINGXI_S31_TIMER_SHA": _sha(self.src / "lingxi-db-backup.timer"),
            "LINGXI_S31_MONITOR_SCRIPT_SHA": _sha(self.src / "host_health_alert.py"),
            "LINGXI_S31_TRANSFER": "none",
            "LINGXI_S31_REMOTE_LABEL": "offsite",
        }
        values.update(overrides)
        text = "# 用例输入\n" + "".join(f"{k}={v}\n" for k, v in values.items())
        self.env_file.write_text(text, encoding="utf-8")

    def run_script(self, sub: str) -> subprocess.CompletedProcess[str]:
        env = base_env(self.bin, self.state, self.unit_dir)
        env.update(
            {
                "LINGXI_S31_ROOT": str(self.root),
                "LINGXI_S31_ENV_FILE": str(self.env_file),
                "LINGXI_S31_SYSTEMCTL": str(self.stubs["systemctl"]),
                "LINGXI_S31_SSH": str(self.stubs["ssh"]),
                "LINGXI_S31_SCP": str(self.stubs["scp"]),
            }
        )
        return subprocess.run(
            ["bash", str(SCRIPT), sub],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def installed_files(self) -> list[Path]:
        return sorted(p for p in self.root.rglob("*") if p.is_file())

    def test_apply_installs_and_readback_criteria_pass(self) -> None:
        result = self.run_script("apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        env_dst = self.root / "opt" / "lingxi" / "monitoring" / "db-backup.env"
        script_dst = self.root / "opt" / "lingxi" / "scripts" / "db_backup.sh"
        self.assertEqual(_sha(script_dst), _sha(self.src / "db_backup.sh"))
        self.assertEqual(oct(env_dst.stat().st_mode & 0o777), "0o600")
        self.assertIn("s31 apply 完成", result.stdout)
        self.assertTrue((self.state / "lingxi-db-backup.timer.active").exists())

    def test_generated_env_readback_uses_printed_value_not_input_sha(self) -> None:
        """#886 第 2 处：现场生成的 db-backup.env 不与输入文件 sha 比对，只比本次打印的 installed 值。"""
        result = self.run_script("apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        env_dst = self.root / "opt" / "lingxi" / "monitoring" / "db-backup.env"
        installed = re.search(r"\[env\] installed sha=([0-9a-f]{64})", result.stdout)
        self.assertIsNotNone(installed, result.stdout)
        printed = installed.group(1)
        self.assertEqual(_sha(env_dst), printed)
        self.assertNotEqual(printed, _sha(self.env_file))
        self.assertRegex(
            result.stdout,
            rf"\[回读判据\] env 现场生成：在位 sha={printed} = 本次打印的 installed / already_in_place 值"
            r"（不与输入文件 sha 比对）→ ok",
        )
        for name in ("脚本", "service", "timer"):
            self.assertRegex(result.stdout, rf"\[回读判据\] {name} 输入副本：在位 sha=[0-9a-f]{{64}} = 输入文件声明的 sha → ok")
        self.assertNotIn("不符", result.stdout)

    def test_second_apply_is_idempotent(self) -> None:
        first = self.run_script("apply")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        snapshot = {p: _sha(p) for p in self.installed_files()}
        second = self.run_script("apply")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        for name in ("脚本", "service", "timer", "env"):
            self.assertIn(f"s31 [{name}] already_in_place sha=", second.stdout)
        self.assertNotIn("installed sha=", second.stdout)
        self.assertEqual({p: _sha(p) for p in self.installed_files()}, snapshot)
        self.assertFalse((self.root / "root" / "lingxi-s31-backup").exists())
        self.assertRegex(second.stdout, r"\[回读判据\] env 现场生成：.*→ ok")
        dry = self.run_script("dry")
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertIn("→ already_in_place", dry.stdout)

    def test_malformed_remote_is_rejected_without_echoing_value(self) -> None:
        self.write_inputs(
            LINGXI_S31_TRANSFER="scp",
            LINGXI_S31_REMOTE=f"{SECRET_SENTINEL}-no-colon-form",
        )
        result = self.run_script("apply")
        self.assertEqual(result.returncode, 1)
        self.assertIn("LINGXI_S31_REMOTE 须形如", result.stderr)
        self.assertNotIn(SECRET_SENTINEL, result.stdout + result.stderr)
        self.assertEqual(self.installed_files(), [self.unit_dir / "lingxi-host-monitor.service"])

    def test_missing_declared_sha_refuses_before_any_change(self) -> None:
        self.write_inputs(LINGXI_S31_SCRIPT_SHA="")
        result = self.run_script("apply")
        self.assertEqual(result.returncode, 1)
        self.assertIn("前置不满足", result.stdout)
        self.assertIn("未做任何改动", result.stderr)
        self.assertEqual(self.installed_files(), [self.unit_dir / "lingxi-host-monitor.service"])

    def test_monitor_enable_dropin_readback_uses_printed_value(self) -> None:
        self.assertEqual(self.run_script("apply").returncode, 0)
        script = self.run_script("monitor-script")
        self.assertEqual(script.returncode, 0, script.stdout + script.stderr)
        self.assertRegex(script.stdout, r"\[回读判据\] 巡检脚本 输入副本：.*→ ok")
        enable = self.run_script("monitor-enable")
        self.assertEqual(enable.returncode, 0, enable.stdout + enable.stderr)
        dropin = self.unit_dir / "lingxi-host-monitor.service.d" / "30-lingxi-db-checks.conf"
        printed = re.search(r"\[巡检 drop-in\] installed sha=([0-9a-f]{64})", enable.stdout)
        self.assertIsNotNone(printed, enable.stdout)
        self.assertEqual(_sha(dropin), printed.group(1))
        self.assertRegex(enable.stdout, r"\[回读判据\] 巡检 drop-in 现场生成：.*→ ok")
        again = self.run_script("monitor-enable")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn(f"[巡检 drop-in] already_in_place sha={printed.group(1)}", again.stdout)
        self.assertRegex(again.stdout, r"\[回读判据\] 巡检 drop-in 现场生成：.*→ ok")


@unittest.skipUnless(shutil.which("bash"), "需要 bash")
class DbBackupInstallUsageTest(unittest.TestCase):
    """不带子命令只打印用法段（入仓后改为按标记取段，不依赖行号）。"""

    def test_usage_block_printed(self) -> None:
        result = subprocess.run(
            ["bash", str(SCRIPT)], capture_output=True, text=True, timeout=30, check=False
        )
        self.assertEqual(result.returncode, 2)
        self.assertTrue(result.stderr.startswith("# 用法：sudo -n bash db_backup_install.sh"))
        self.assertIn("#   status ", result.stderr)


if __name__ == "__main__":
    unittest.main()
