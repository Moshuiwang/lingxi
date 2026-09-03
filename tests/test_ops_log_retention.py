"""宿主机日志的权限与保留（对抗审查 2026-09-02 面 6 的 P3 两条）。

两条缺陷同一形状——**写了但没人回收 / 没人定权限**：

- ``deploy/collect-container-logs.sh`` 用 ``>>`` 新建取证日志文件，权限落在调用者
  当时的 umask 上。cron 与交互 shell 的 umask 常常不同（022 给出 0644），于是同一
  份含容器 stdout/stderr 完整转发内容的日志，在不同宿主机、不同触发方式下权限不
  一致，可能对同机其他账号可读。目录是 0750、轮转后的新文件由 logrotate 的
  ``create 0640`` 保证，唯独**轮转之前的第一份**没人管。
- ``/var/log/lingxi/monitoring`` 下的 ``resource-YYYYMMDD.log`` /
  ``db_business-YYYYMMDD.log`` 每天一个新文件，``lingxi-container-logs.logrotate``
  的名单里没有它们，采样每分钟一轮常驻运行——磁盘占用单调增长直到写满宿主机，
  而写满宿主机会同时打掉容器日志收集与业务本身。

这里用真实脚本 + 伪造 ``docker`` 跑一遍，断言的是文件系统上的实际结果。
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parents[1]
COLLECT_SCRIPT = ROOT / "deploy" / "collect-container-logs.sh"
RESOURCE_SCRIPT = ROOT / "scripts" / "ops" / "monitoring" / "resource_sample.sh"
DB_BUSINESS_SCRIPT = ROOT / "scripts" / "ops" / "monitoring" / "db_business_sample.sh"
PUSH_SCRIPT = ROOT / "scripts" / "ops" / "monitoring" / "push_to_monitoring.sh"

FAKE_DOCKER_FOR_COLLECT = """#!/bin/sh
if [ "$1" = "inspect" ]; then
  echo "true"
  exit 0
fi
if [ "$1" = "logs" ]; then
  echo "2026-09-02T00:00:00Z 容器转发的一行"
  exit 0
fi
exit 0
"""

FAKE_DOCKER_FOR_RESOURCE = """#!/bin/sh
if [ "$1" = "stats" ]; then
  echo '{"Name":"present-container","CPUPerc":"1.23%","MemUsage":"12.3MiB / 512MiB",\
"MemPerc":"2.4%","NetIO":"1kB / 2kB","BlockIO":"3MB / 4MB","PIDs":"7"}'
  exit 0
fi
exit 1
"""


def _fake_bin(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return directory


@unittest.skipUnless(shutil.which("bash"), "需要 bash")
class ContainerLogPermissionsTest(unittest.TestCase):
    """取证日志的权限不再取决于调用者的 umask（P3）。"""

    def test_the_log_file_is_0640_even_under_a_wide_umask(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            bin_dir = _fake_bin(workspace / "bin", "docker", FAKE_DOCKER_FOR_COLLECT)
            log_dir = workspace / "logs"

            # umask 000 是最宽的一档：不修的话新建文件会是 0666。
            result = subprocess.run(
                ["bash", "-c", f"umask 000; exec {COLLECT_SCRIPT}"],
                env={
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "LINGXI_LOG_COLLECT_DIR": str(log_dir),
                    "HOME": str(workspace),
                },
                capture_output=True,
                text=True,
                timeout=120,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            produced = sorted(log_dir.glob("*.log"))
            self.assertTrue(produced, f"没有产出任何日志文件：{result.stderr}")
            for path in produced:
                with self.subTest(path=path.name):
                    mode = stat.S_IMODE(path.stat().st_mode)
                    self.assertEqual(
                        mode,
                        0o640,
                        f"{path.name} 权限 {oct(mode)}，取证日志不得随 umask 变宽",
                    )
            self.assertEqual(stat.S_IMODE(log_dir.stat().st_mode), 0o750)

    def test_an_existing_over_permissive_file_is_tightened(self) -> None:
        """umask 只作用于**新建**；历史上用更宽 umask 建出来的文件也要被收紧。"""

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            bin_dir = _fake_bin(workspace / "bin", "docker", FAKE_DOCKER_FOR_COLLECT)
            log_dir = workspace / "logs"
            log_dir.mkdir()
            stale = log_dir / "scheduler.log"
            stale.write_text("旧内容\n", encoding="utf-8")
            stale.chmod(0o666)

            subprocess.run(
                ["bash", str(COLLECT_SCRIPT)],
                env={
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "LINGXI_LOG_COLLECT_DIR": str(log_dir),
                    "HOME": str(workspace),
                },
                capture_output=True,
                text=True,
                timeout=120,
                check=True,
            )

            self.assertEqual(stat.S_IMODE(stale.stat().st_mode), 0o640)


@unittest.skipUnless(shutil.which("bash") and shutil.which("python3"), "需要 bash 与 python3")
class MonitoringSampleRetentionTest(unittest.TestCase):
    """``/var/log/lingxi/monitoring`` 下的样本文件不再无限增长（P3）。"""

    def _run_resource_sample(self, output_dir: Path, workspace: Path, **extra: str) -> None:
        bin_dir = _fake_bin(workspace / "bin", "docker", FAKE_DOCKER_FOR_RESOURCE)
        result = subprocess.run(
            ["bash", str(RESOURCE_SCRIPT)],
            env={
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "LINGXI_MONITORING_DIR": str(output_dir),
                "LINGXI_MONITORING_CONTAINERS": "present-container",
                "HOME": str(workspace),
                **extra,
            },
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_old_sample_files_are_pruned_and_recent_ones_are_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            output_dir = workspace / "monitoring"
            output_dir.mkdir()
            now = time.time()
            ancient = output_dir / "resource-20260101.log"
            recent = output_dir / "resource-20260901.log"
            other_family = output_dir / "db_business-20260101.log"
            unrelated = output_dir / "keep-me.txt"
            for path in (ancient, recent, other_family, unrelated):
                path.write_text("{}\n", encoding="utf-8")
            for path in (ancient, other_family, unrelated):
                os.utime(path, (now - 60 * 86400, now - 60 * 86400))
            os.utime(recent, (now - 2 * 86400, now - 2 * 86400))

            self._run_resource_sample(output_dir, workspace)

            self.assertFalse(ancient.exists(), "超过保留天数的同族样本必须被清掉")
            self.assertTrue(recent.exists(), "保留窗口内的样本不许动")
            self.assertTrue(
                other_family.exists(),
                "只清自己那一族：db_business 由它自己的脚本负责，不能越界代删",
            )
            self.assertTrue(unrelated.exists(), "不匹配文件名的东西一个都不许动")

            today = datetime.now(timezone.utc).strftime("%Y%m%d")
            self.assertTrue((output_dir / f"resource-{today}.log").exists(), "本轮样本要写出来")

    def test_the_retention_window_is_configurable_and_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            output_dir = workspace / "monitoring"
            output_dir.mkdir()
            now = time.time()
            old = output_dir / "resource-20260101.log"
            old.write_text("{}\n", encoding="utf-8")
            os.utime(old, (now - 10 * 86400, now - 10 * 86400))

            # 保留 30 天（默认）时这份 10 天前的文件应当留下。
            self._run_resource_sample(output_dir, workspace)
            self.assertTrue(old.exists())

            # 收紧到 5 天就该被清掉。
            self._run_resource_sample(
                output_dir, workspace, LINGXI_MONITORING_RETENTION_DAYS="5"
            )
            self.assertFalse(old.exists())

            # 0 表示关掉这条清理（不是"全删"）。
            revived = output_dir / "resource-20260102.log"
            revived.write_text("{}\n", encoding="utf-8")
            os.utime(revived, (now - 400 * 86400, now - 400 * 86400))
            self._run_resource_sample(
                output_dir, workspace, LINGXI_MONITORING_RETENTION_DAYS="0"
            )
            self.assertTrue(revived.exists(), "0 是关掉清理，不是无条件删除")


class EveryMonitoringWriterHasARetentionRuleTest(unittest.TestCase):
    """两个采样脚本各清自己那一族，推送脚本给 push.log 定上限——一个都不许漏。

    ``db_business_sample.sh`` 与 ``push_to_monitoring.sh`` 都要 ``psql`` 才跑得起来
    （真实上推属 L4a，留给受控验收），因此这一条用源码断言兜住"有没有这段"，
    行为那一半由上面的 ``resource_sample.sh`` 同型实现代表。
    """

    def test_both_sample_scripts_prune_their_own_family(self) -> None:
        for script, pattern in (
            (RESOURCE_SCRIPT, "resource-*.log"),
            (DB_BUSINESS_SCRIPT, "db_business-*.log"),
        ):
            with self.subTest(script=script.name):
                text = script.read_text(encoding="utf-8")
                self.assertIn("LINGXI_MONITORING_RETENTION_DAYS", text)
                self.assertRegex(
                    text,
                    re.compile(
                        rf"find .*OUTPUT_DIR.*-maxdepth 1 -type f -name '{re.escape(pattern)}'",
                        re.DOTALL,
                    ),
                    "必须按自己那一族的文件名前缀清理，不得通配整个目录",
                )
                self.assertIn("-mtime", text)

    def test_the_push_log_has_a_size_ceiling(self) -> None:
        text = PUSH_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("LINGXI_MONITORING_PUSH_LOG_MAX_BYTES", text)
        self.assertIn("tail -c", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
