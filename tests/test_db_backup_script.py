"""``scripts/ops/db_backup.sh`` 在异常输入 / 异常时序下对状态文件的两条否定断言（#884）。

- 第 2 条（F13）：timer 与手工并发时，抢不到目录锁的那一轮**不得**改写状态文件——
  内容逐字节不变、mtime 不变；退出码非 0 让 systemd 记失败。
- 第 3 条（F16）：输入校验失败时，状态文件**不得**出现被拒的原值（用连接串形态的注入值
  证明零命中），只写固定错误码 ``invalid_input``，且仍是合法 JSON。

伪造 ``docker``（``inspect`` 回 healthy，其余调用一律失败）经 PATH 注入；备份目录是本账户
0700 的临时目录。不起容器、不连库。

变异对照：把 ``flock -n 9 || …`` 一行恢复成 ``fail already_running 1 …``，
``test_a_round_that_cannot_take_the_lock_leaves_the_status_file_untouched`` 必红；
把 ``write_status`` 的 mode / label 改回直接取 ``TRANSFER`` / ``REMOTE_LABEL``，
``test_rejected_input_values_never_reach_the_status_file`` 必红。
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "ops" / "db_backup.sh"

#: 连接串形态的注入值：口令、主机两段各带一个哨兵，任一出现在状态文件里即判红。
INJECTED = "postgresql://svc:SENTINEL_SECRET@SENTINEL_HOST.example:5432/postgres"


class DbBackupStatusFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.backup_dir = tmp / "backups"
        self.backup_dir.mkdir()
        os.chmod(self.backup_dir, 0o700)
        self.status_file = tmp / "status" / "db-backup-status.json"
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        fake_docker = bin_dir / "docker"
        fake_docker.write_text(
            '#!/bin/sh\nif [ "$1" = "inspect" ]; then echo healthy; exit 0; fi\nexit 1\n',
            encoding="utf-8",
        )
        os.chmod(fake_docker, 0o755)
        self.env = {
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "LINGXI_DB_BACKUP_DIR": str(self.backup_dir),
            "LINGXI_DB_BACKUP_STATUS_FILE": str(self.status_file),
            "LINGXI_DB_BACKUP_CONTAINER": "fake-db",
        }

    def _run(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT)],
            env={**self.env, **overrides},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def test_a_round_that_cannot_take_the_lock_leaves_the_status_file_untouched(self) -> None:
        self.status_file.parent.mkdir()
        previous = b'{"schema":1,"ok":true,"finished_at":"2026-09-27T00:00:00Z"}\n'
        self.status_file.write_bytes(previous)
        os.utime(self.status_file, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        before = self.status_file.stat().st_mtime_ns

        with open(self.backup_dir / ".lock", "w", encoding="utf-8") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self._run()

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("上一轮尚未结束", result.stderr)
        self.assertEqual(self.status_file.read_bytes(), previous, "抢锁失败的一轮改写了状态文件")
        self.assertEqual(self.status_file.stat().st_mtime_ns, before, "状态文件 mtime 被改动")
        self.assertEqual(
            sorted(p.name for p in self.status_file.parent.iterdir()),
            [self.status_file.name],
            "不得留下临时状态文件",
        )

    def test_rejected_input_values_never_reach_the_status_file(self) -> None:
        cases = {
            "TRANSFER": {"LINGXI_DB_BACKUP_TRANSFER": INJECTED},
            "REMOTE_LABEL": {"LINGXI_DB_BACKUP_REMOTE_LABEL": INJECTED},
            "REMOTE": {"LINGXI_DB_BACKUP_TRANSFER": "scp", "LINGXI_DB_BACKUP_REMOTE": INJECTED},
            "KEEP": {"LINGXI_DB_BACKUP_KEEP": INJECTED},
        }
        for name, overrides in cases.items():
            with self.subTest(rejected=name):
                self.status_file.unlink(missing_ok=True)
                result = self._run(**overrides)
                self.assertEqual(result.returncode, 2, result.stderr)
                text = self.status_file.read_text(encoding="utf-8")
                for needle in ("SENTINEL", "postgresql://", "svc:"):
                    self.assertNotIn(needle, text, f"被拒的原值进了状态文件：{text!r}")
                status = json.loads(text)
                self.assertIs(status["ok"], False)
                self.assertEqual(status["error"], "invalid_input")
                self.assertEqual(status["transfer"]["mode"], "invalid")
                self.assertEqual(status["transfer"]["label"], "invalid")
                self.assertIsNone(status["transfer"]["ok"])


if __name__ == "__main__":
    unittest.main()
