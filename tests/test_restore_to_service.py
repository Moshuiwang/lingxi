"""``scripts/ops/restore_to_service.sh`` 的用例（#885：从每日备份恢复到三服务可问数）。

假根目录形（非 root + ``LINGXI_S30_ROOT``）：docker / 切换脚本走 ``tests/support/fake_restore_to_service.py`` 的桩，
systemctl 沿用 ``fake_db_switch.py`` 的桩（只读引用，不改那份文件）。覆盖：wipe 的守门（缺 ``--yes`` / 非预发 /
移动目标与备份目录重叠）与「不触碰备份目录」、角色骨架从 dump 引用派生（不写死个数）、run 的空白核对与
连接串核对、分段时长表、rollback 回到 wipe 前。

真库形（设 ``LINGXI_POSTGRES_DSN`` 与 ``LINGXI_TEST_PG_CONTAINER`` 时跑，否则 skip）：用仓库的
``db_backup.sh`` 对一次性测试库产出真实日备份三件 → 清空 → ``run --from=roles --until=verify`` 恢复并核对零差异；
篡改清单后 verify 判红。
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

from support.fake_db_switch import FAKE_SYSTEMCTL, base_env, write_executable
from support.fake_restore_to_service import COUNTS_JSON, SCHEMA_ROLES, install_fakes

REPOSITORY_ROOT = Path(__file__).parents[1]
SCRIPT = REPOSITORY_ROOT / "scripts" / "ops" / "restore_to_service.sh"
BACKUP_SCRIPT = REPOSITORY_ROOT / "scripts" / "ops" / "db_backup.sh"
COMPOSE = REPOSITORY_ROOT / "deploy" / "compose.db.yaml"
OLD_PW = "oldpwsentinel7c1e"
NEW_PW = "newpwsentinel9a2b"
ENV_SERVICES = ("scheduler", "gateway", "worker", "worker-queue", "migrate", "reauthorize")
DUMP_NAME = "lingxi-db-20260927T114654Z.dump"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_digest(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): _sha(p) for p in sorted(root.rglob("*")) if p.is_file()}


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="lingxi-885-rts-")
        base = Path(self._tmp.name)
        self.base, self.root, self.state = base, base / "root", base / "state"
        self.root.mkdir()
        self.state.mkdir()
        self.fakes = install_fakes(base / "bin", base / "support")
        write_executable(base / "bin" / "systemctl", FAKE_SYSTEMCTL)
        self.contract = self.root / "opt" / "lingxi" / "control" / "host-contract.json"
        self.write_contract("stage")
        self.config = self.root / "opt" / "lingxi" / "config"
        self.config.mkdir(parents=True)
        app_dsn = f"'postgresql+psycopg://postgres:{OLD_PW}@lingxi-db:5432/postgres'"
        (self.config / ".env.stage").write_text("LINGXI_ENVIRONMENT=stage\n", encoding="utf-8")
        for name in ENV_SERVICES:
            key = "LINGXI_MIGRATION_DSN" if name == "migrate" else "LINGXI_POSTGRES_DSN"
            (self.config / f".env.stage.{name}").write_text(
                f"A=1\n{key}={app_dsn}\n", encoding="utf-8"
            )
        self.monitor_env = self.root / "opt" / "lingxi" / "monitoring" / "db-business.env"
        self.monitor_env.parent.mkdir(parents=True)
        self.monitor_env.write_text(
            f"LINGXI_POSTGRES_DSN=postgresql://postgres:{OLD_PW}@127.0.0.1:5432/postgres\n",
            encoding="utf-8",
        )
        self.hosts = self.root / "etc" / "hosts"
        self.hosts.parent.mkdir(parents=True)
        self.hosts.write_text(
            "127.0.0.1 localhost\n127.0.0.1 lingxi-db  # lingxi-s30：旧行\n::1 ip6-localhost\n",
            encoding="utf-8",
        )
        # 现役本地库：数据目录、口令、compose.env、compose 副本，容器在跑
        self.pgdata = self.root / "var" / "lib" / "lingxi" / "pgdata"
        self.pgdata.mkdir(parents=True)
        (self.pgdata / "PG_VERSION").write_text("old\n", encoding="utf-8")
        self.dbconf = self.root / "etc" / "lingxi" / "db"
        self.dbconf.mkdir(parents=True)
        (self.dbconf / ".env.db").write_text(f"POSTGRES_PASSWORD={OLD_PW}\n", encoding="utf-8")
        (self.dbconf / "compose.env").write_text("LINGXI_DB_ENV_FILE=old\n", encoding="utf-8")
        self.dbinst = self.root / "opt" / "lingxi" / "db"
        self.dbinst.mkdir(parents=True)
        (self.dbinst / "compose.db.yaml").write_text("old compose\n", encoding="utf-8")
        (self.state / "db.up").touch()
        # 每日备份目录（三件）与异机副本目录：任何子命令都不得改动
        self.backups = self.root / "var" / "lib" / "lingxi" / "backups"
        self.backups.mkdir()
        self.dump = self.backups / DUMP_NAME
        self.dump.write_bytes(b"PGDMP fake custom archive")
        (self.backups / f"{DUMP_NAME}.sha256").write_text(
            f"{_sha(self.dump)}  {DUMP_NAME}\n", encoding="utf-8"
        )
        (self.backups / f"{DUMP_NAME}.counts.json").write_text(COUNTS_JSON, encoding="utf-8")
        self.offsite = self.root / "home" / "deploy" / "lingxi-backups"
        self.offsite.mkdir(parents=True)
        (self.offsite / DUMP_NAME).write_bytes(b"offsite copy")
        self.overrides: dict[str, str] = {}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_contract(self, environment: str) -> None:
        self.contract.parent.mkdir(parents=True, exist_ok=True)
        self.contract.write_text(
            json.dumps(
                {
                    "project": "lingxi",
                    "environment": environment,
                    "config_root": "/opt/lingxi/config",
                }
            ),
            encoding="utf-8",
        )

    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = base_env(self.base / "bin", self.state, self.base / "units")
        env.update(
            LINGXI_S30_ROOT=str(self.root),
            LINGXI_S30_ENV_FILE=str(self.base / "absent.env"),
            LINGXI_S30_DOCKER=str(self.fakes["docker"]),
            LINGXI_S30_SYSTEMCTL=str(self.base / "bin" / "systemctl"),
            LINGXI_S30_COMPOSE_SRC=str(COMPOSE),
            LINGXI_RESTORE_SWITCH_SCRIPT=str(self.fakes["switch"]),
            FAKE_SUPPORT=str(self.base / "support"),
            FAKE_NEW_PW=NEW_PW,
        )
        env.update(self.overrides)
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def calls(self) -> str:
        log = self.state / "calls.log"
        return log.read_text(encoding="utf-8") if log.exists() else ""

    def drill(self) -> Path:
        return Path(
            (self.root / "var" / "lib" / "lingxi" / "restore-drill" / "current")
            .read_text(encoding="utf-8")
            .strip()
        )

    def env_digest(self) -> dict[str, str]:
        files = [self.config / ".env.stage", self.monitor_env] + [
            self.config / f".env.stage.{n}" for n in ENV_SERVICES
        ]
        return {str(p): _sha(p) for p in files}

    def protected_digest(self) -> dict[str, str]:
        return {
            **_tree_digest(self.backups),
            **{"offsite/" + k: v for k, v in _tree_digest(self.offsite).items()},
        }


class WipeGuardTest(_Base):
    def assert_untouched(self) -> None:
        self.assertTrue((self.pgdata / "PG_VERSION").exists())
        self.assertTrue((self.dbconf / ".env.db").exists())
        self.assertIn("lingxi-db", self.hosts.read_text(encoding="utf-8"))
        self.assertNotIn("compose", self.calls())
        self.assertNotIn("switch", self.calls())

    def test_wipe_refuses_without_yes(self) -> None:
        result = self.run_script("wipe")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("需显式 --yes", result.stderr)
        self.assert_untouched()

    def test_wipe_refuses_non_stage(self) -> None:
        self.write_contract("production")
        result = self.run_script("wipe", "--yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("只限预发", result.stderr)
        self.assert_untouched()
        self.write_contract("stage")
        self.overrides["LINGXI_S30_ENV_SUFFIX"] = "prod"
        result = self.run_script("wipe", "--yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("只限预发", result.stderr)
        self.assert_untouched()

    def test_wipe_refuses_target_overlapping_backups(self) -> None:
        before = self.protected_digest()
        self.overrides["LINGXI_S30_DB_DATA_DIR"] = "/var/lib/lingxi"  # 备份目录的父目录
        result = self.run_script("wipe", "--yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("受保护的备份目录", result.stderr)
        self.assert_untouched()
        self.assertEqual(self.protected_digest(), before)

    def test_wipe_moves_host_side_and_leaves_backups(self) -> None:
        before = self.protected_digest()
        result = self.run_script("wipe", "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        drill = self.drill()
        self.assertFalse(self.pgdata.exists())
        self.assertFalse(self.dbconf.exists())
        self.assertFalse(self.dbinst.exists())
        self.assertEqual(
            (drill / "wiped" / "pgdata" / "PG_VERSION").read_text(encoding="utf-8"), "old\n"
        )
        self.assertTrue((drill / "wiped" / "db-config" / ".env.db").exists())
        hosts = self.hosts.read_text(encoding="utf-8")
        self.assertNotIn("lingxi-db", hosts)
        self.assertIn("127.0.0.1 localhost", hosts)
        self.assertIn("::1 ip6-localhost", hosts)
        self.assertIn("lingxi-db", (drill / "wiped" / "hosts.lines").read_text(encoding="utf-8"))
        calls = self.calls()
        self.assertIn("switch stop-write", calls)
        self.assertIn("systemctl stop lingxi-db-backup.timer", calls)
        self.assertRegex(calls, r"docker compose --project-name lingxi-db .* down")
        self.assertFalse((self.state / "db.up").exists())
        self.assertEqual(self.protected_digest(), before)
        self.assertNotIn(
            "backups", "".join(line for line in calls.splitlines() if line.startswith("docker"))
        )
        again = self.run_script("wipe", "--yes")
        self.assertEqual(again.returncode, 1)
        self.assertIn("未收口", again.stderr)


class RunTest(_Base):
    def test_roles_derived_from_dump_references(self) -> None:
        result = self.run_script("roles", str(self.dump))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"派生的角色 {len(SCHEMA_ROLES)} 个", result.stdout)
        for role in SCHEMA_ROLES:
            self.assertIn(role, result.stdout)
        self.assertNotIn(" public ", result.stdout.split("个：", 1)[1])
        # 多一个被授权角色，清单跟着变多：个数来自 dump，不是写死的
        schema = self.base / "support" / "schema.sql"
        schema.write_text(
            schema.read_text(encoding="utf-8")
            + "GRANT SELECT ON TABLE public.t1 TO service_role;\n",
            encoding="utf-8",
        )
        result = self.run_script("roles", str(self.dump))
        self.assertIn(f"派生的角色 {len(SCHEMA_ROLES) + 1} 个", result.stdout)
        self.assertIn("service_role", result.stdout)

    def test_run_refuses_when_not_blank(self) -> None:
        result = self.run_script("run", str(self.dump))
        self.assertEqual(result.returncode, 1)
        self.assertIn("不是空白", result.stderr)
        self.assertNotIn("switch install-pg", self.calls())

    def test_run_rejects_checksum_mismatch(self) -> None:
        self.run_script("wipe", "--yes")
        (self.backups / f"{DUMP_NAME}.sha256").write_text(
            "0" * 64 + f"  {DUMP_NAME}\n", encoding="utf-8"
        )
        result = self.run_script("run", str(self.dump))
        self.assertEqual(result.returncode, 1)
        self.assertIn("校验和不符", result.stderr)

    def test_full_run_from_blank(self) -> None:
        self.assertEqual(self.run_script("wipe", "--yes").returncode, 0)
        before = self.protected_digest()
        result = self.run_script("run", str(self.dump))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        seen = (self.state / "preflight.env.seen").read_text(encoding="utf-8")
        self.assertIn("S30_ROLES=''", seen)
        self.assertIn("S30_DATLOCALE='en-US'", seen)
        self.assertIn("S30_DATLOCPROVIDER='i'", seen)
        created = (self.state / "created_roles.log").read_text(encoding="utf-8").split("\n")
        self.assertEqual(
            sorted(filter(None, created)),
            sorted(set(SCHEMA_ROLES) - {"postgres", "pg_database_owner"}),
        )
        self.assertTrue((self.state / "restored").exists())
        self.assertIn("SCHEMA extensions", result.stdout)  # 目标已在位的创建条目被去掉
        self.assertIn("verify 零差异", result.stdout)
        for path in [self.monitor_env] + [self.config / f".env.stage.{n}" for n in ENV_SERVICES]:
            text = path.read_text(encoding="utf-8")
            self.assertIn(NEW_PW, text)
            self.assertNotIn(OLD_PW, text)
        self.assertNotIn(NEW_PW, result.stdout + result.stderr)
        calls = self.calls()
        self.assertIn("switch recreate-services", calls)
        self.assertIn(
            "systemctl start lingxi-db-business-sample.timer lingxi-release-pull.timer lingxi-db-backup.timer",
            calls,
        )
        # 分段时长表：本地库 10:00:00 起、10:00:07 首次 healthy；三服务最晚 10:05:12 起、10:05:44 首次 healthy（docker 记录）
        self.assertIn("2026-09-27T10:00:07Z", result.stdout)
        self.assertIn("2026-09-27T10:05:12Z", result.stdout)
        self.assertIn("2026-09-27T10:05:44Z", result.stdout)
        self.assertIn("RTO（恢复开始 → 三服务全部 healthy", result.stdout)
        self.assertEqual(self.protected_digest(), before)

    def test_verify_diff_stops_before_dsn(self) -> None:
        self.run_script("wipe", "--yes")
        target = self.base / "support" / "target.json"
        data = json.loads(target.read_text(encoding="utf-8"))
        data["tables"]["public.t1"] = 1
        data["rel_owners"]["t1"] = "postgres"
        target.write_text(json.dumps(data), encoding="utf-8")
        env_before = self.env_digest()
        result = self.run_script("run", str(self.dump))
        self.assertEqual(result.returncode, 1)
        self.assertIn("tables|public.t1|清单=2|目标=1", result.stdout)
        self.assertIn('owner|relation:t1|清单="lingxi_app"|目标="postgres"', result.stdout)
        self.assertIn("verify 未通过", result.stderr)
        self.assertEqual(self.env_digest(), env_before)

    def test_dsn_refuses_foreign_target_without_touching_files(self) -> None:
        self.run_script("wipe", "--yes")
        (self.config / ".env.stage.gateway").write_text(
            f"LINGXI_POSTGRES_DSN=postgresql://postgres:{OLD_PW}@db.example.invalid:6543/postgres\n",
            encoding="utf-8",
        )
        env_before = self.env_digest()
        result = self.run_script("run", str(self.dump), "--until=dsn")
        self.assertEqual(result.returncode, 1)
        self.assertIn("连接串核对不符", result.stderr)
        self.assertIn(".env.stage.gateway", result.stderr)
        self.assertEqual(self.env_digest(), env_before)
        self.assertNotIn(OLD_PW, result.stdout + result.stderr)

    def test_dsn_refuses_param_override_without_touching_files(self) -> None:
        # authority 指向本地库、查询串里带 libpq 覆盖键（host / hostaddr / dbname …）实际连到别处：同样判「指向别处」
        self.assertEqual(self.run_script("wipe", "--yes").returncode, 0)
        ready = self.run_script("run", str(self.dump), "--until=verify")
        self.assertEqual(ready.returncode, 0, ready.stdout + ready.stderr)
        cases = {
            ".env.stage.gateway": "?host=db.example.invalid",
            ".env.stage.worker": "?sslmode=disable&hostaddr=192.0.2.10",
            ".env.stage.scheduler": "?DBName=other",
            ".env.stage.migrate": "?ho%73t=db.example.invalid",
            ".env.stage.reauthorize": "?sslmode=disable&db%6eame=other",
        }
        for name, query in cases.items():
            with self.subTest(query=query):
                (self.config / name).write_text(
                    f"LINGXI_POSTGRES_DSN='postgresql+psycopg://postgres:{OLD_PW}@lingxi-db:5432/postgres{query}'\n",
                    encoding="utf-8",
                )
                env_before = self.env_digest()
                result = self.run_script("run", str(self.dump), "--from=dsn", "--until=dsn")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("连接串核对不符", result.stderr)
                self.assertIn(name, result.stderr)
                self.assertEqual(self.env_digest(), env_before)
                self.assertNotIn(OLD_PW, result.stdout + result.stderr)
                (self.config / name).write_text(
                    f"LINGXI_POSTGRES_DSN='postgresql+psycopg://postgres:{OLD_PW}@lingxi-db:5432/postgres'\n",
                    encoding="utf-8",
                )

    def test_dsn_keeps_harmless_query_string(self) -> None:
        self.run_script("wipe", "--yes")
        (self.config / ".env.stage.gateway").write_text(
            f"LINGXI_POSTGRES_DSN='postgresql+psycopg://postgres:{OLD_PW}@lingxi-db:5432/postgres?sslmode=disable'\n",
            encoding="utf-8",
        )
        result = self.run_script("run", str(self.dump), "--until=dsn")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            (self.config / ".env.stage.gateway").read_text(encoding="utf-8"),
            f"LINGXI_POSTGRES_DSN='postgresql+psycopg://postgres:{NEW_PW}@lingxi-db:5432/postgres?sslmode=disable'\n",
        )

    def test_from_past_verify_requires_this_rounds_pass(self) -> None:
        # B1：--from 越过 verify（dsn / services / timers）须有本轮、同一 dump 的 verify 通过记录
        fresh = self.run_script("run", str(self.dump), "--from=dsn")  # 新轮次、无任何记录
        self.assertEqual(fresh.returncode, 1, fresh.stdout + fresh.stderr)
        self.assertIn("先从 verify 跑", fresh.stderr)
        self.assertNotIn("switch recreate-services", self.calls())
        self.assertEqual(self.run_script("wipe", "--yes").returncode, 0)
        target = self.base / "support" / "target.json"
        good = target.read_text(encoding="utf-8")
        data = json.loads(good)
        data["tables"]["public.t1"] = 1
        target.write_text(json.dumps(data), encoding="utf-8")
        failed = self.run_script("run", str(self.dump))
        self.assertEqual(failed.returncode, 1)
        self.assertIn("verify 未通过", failed.stderr)
        env_before = self.env_digest()
        for step in ("dsn", "services", "timers"):
            with self.subTest(step=step):
                skipped = self.run_script("run", str(self.dump), f"--from={step}")
                self.assertEqual(skipped.returncode, 1, skipped.stdout + skipped.stderr)
                self.assertIn("先从 verify 跑", skipped.stderr)
                self.assertEqual(self.env_digest(), env_before)
        self.assertNotIn("switch recreate-services", self.calls())
        target.write_text(good, encoding="utf-8")
        passed = self.run_script("run", str(self.dump), "--from=verify", "--until=verify")
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        resumed = self.run_script("run", str(self.dump), "--from=services")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertIn("switch recreate-services", self.calls())

    def test_verify_compares_acl_content_not_presence(self) -> None:
        # B10：目标上某张表的授权内容变了（仍非空）→ verify 报差异、不进 dsn
        self.assertEqual(self.run_script("wipe", "--yes").returncode, 0)
        target_sql = self.base / "support" / "target_schema.sql"
        text = target_sql.read_text(encoding="utf-8")
        self.assertIn('GRANT SELECT ON TABLE public.t1 TO "Weird Role";', text)
        target_sql.write_text(
            text.replace(
                'GRANT SELECT ON TABLE public.t1 TO "Weird Role";',
                'GRANT INSERT ON TABLE public.t1 TO "Weird Role";',
            ),
            encoding="utf-8",
        )
        env_before = self.env_digest()
        result = self.run_script("run", str(self.dump))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("acl|ACL public TABLE t1|", result.stdout)
        self.assertIn("verify 未通过", result.stderr)
        self.assertEqual(self.env_digest(), env_before)
        self.assertNotIn("switch recreate-services", self.calls())

    def test_rollback_restores_dir_moved_before_wipe_was_interrupted(self) -> None:
        # B3：wipe 在把 pgdata 移进保留目录之后、记账之前被杀 → rollback 仍能把它移回
        write_executable(
            self.base / "bin" / "mv",
            '#!/usr/bin/env bash\n/usr/bin/mv "$@" || exit $?\n'
            'if [[ -n "${FAKE_MV_KILL_DEST:-}" && "${*: -1}" == *"$FAKE_MV_KILL_DEST" ]]; then kill -9 "$PPID"; fi\n',
        )
        self.overrides["FAKE_MV_KILL_DEST"] = "/wiped/pgdata"
        crashed = self.run_script("wipe", "--yes")
        self.assertEqual(crashed.returncode, -9, crashed.stdout + crashed.stderr)
        self.assertFalse(self.pgdata.exists())
        del self.overrides["FAKE_MV_KILL_DEST"]
        result = self.run_script("rollback", "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.pgdata / "PG_VERSION").read_text(encoding="utf-8"), "old\n")
        self.assertTrue((self.dbconf / ".env.db").exists())

    def test_rollback_rerun_after_partial_failure_is_idempotent(self) -> None:
        # B2：rollback 移回目录后在后续步骤失败 → 二跑成功，已恢复的原目录不被再次移走、failed-run 不嵌套
        self.assertEqual(self.run_script("wipe", "--yes").returncode, 0)
        self.assertEqual(self.run_script("run", str(self.dump), "--until=dsn").returncode, 0)
        drill = self.drill()
        self.overrides["FAKE_SWITCH_FAIL"] = "recreate-services"
        partial = self.run_script("rollback", "--yes")
        self.assertEqual(partial.returncode, 1, partial.stdout + partial.stderr)
        self.assertEqual((self.pgdata / "PG_VERSION").read_text(encoding="utf-8"), "old\n")
        del self.overrides["FAKE_SWITCH_FAIL"]
        again = self.run_script("rollback", "--yes")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("already", again.stdout)
        self.assertEqual((self.pgdata / "PG_VERSION").read_text(encoding="utf-8"), "old\n")
        self.assertEqual(
            (drill / "failed-run" / "pgdata" / "PG_VERSION").read_text(encoding="utf-8"), "new\n"
        )
        self.assertFalse((drill / "failed-run" / "pgdata" / "pgdata").exists())
        self.assertIn("rollback 完成", again.stdout)

    def test_rollback_does_not_nest_into_existing_failed_run(self) -> None:
        # B2：failed-run/<名> 已存在时另起带后缀的目录，不嵌套进去
        self.assertEqual(self.run_script("wipe", "--yes").returncode, 0)
        self.assertEqual(self.run_script("run", str(self.dump), "--until=dsn").returncode, 0)
        drill = self.drill()
        earlier = drill / "failed-run" / "pgdata"
        earlier.mkdir(parents=True)
        (earlier / "PG_VERSION").write_text("earlier\n", encoding="utf-8")
        result = self.run_script("rollback", "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(sorted(p.name for p in earlier.iterdir()), ["PG_VERSION"])
        self.assertEqual((earlier / "PG_VERSION").read_text(encoding="utf-8"), "earlier\n")
        moved = [p for p in (drill / "failed-run").iterdir() if p.name.startswith("pgdata.")]
        self.assertEqual(len(moved), 1, list((drill / "failed-run").iterdir()))
        self.assertEqual((moved[0] / "PG_VERSION").read_text(encoding="utf-8"), "new\n")
        self.assertEqual((self.pgdata / "PG_VERSION").read_text(encoding="utf-8"), "old\n")

    def test_rollback_returns_to_pre_wipe(self) -> None:
        env_before = self.env_digest()
        hosts_before = self.hosts.read_text(encoding="utf-8")
        before = self.protected_digest()
        self.assertEqual(self.run_script("wipe", "--yes").returncode, 0)
        partial = self.run_script("run", str(self.dump), "--until=dsn")
        self.assertEqual(partial.returncode, 0, partial.stdout + partial.stderr)
        self.assertNotEqual(self.env_digest(), env_before)
        drill = self.drill()
        refused = self.run_script("rollback")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("需显式 --yes", refused.stderr)
        result = self.run_script("rollback", "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.pgdata / "PG_VERSION").read_text(encoding="utf-8"), "old\n")
        self.assertEqual(
            (self.dbconf / ".env.db").read_text(encoding="utf-8"), f"POSTGRES_PASSWORD={OLD_PW}\n"
        )
        self.assertEqual(
            (drill / "failed-run" / "pgdata" / "PG_VERSION").read_text(encoding="utf-8"), "new\n"
        )
        self.assertEqual(self.env_digest(), env_before)
        hosts = self.hosts.read_text(encoding="utf-8")
        self.assertEqual(hosts.count("lingxi-db"), 1)
        self.assertIn("127.0.0.1 localhost", hosts)
        self.assertIn("::1 ip6-localhost", hosts)
        self.assertEqual(len(hosts.splitlines()), len(hosts_before.splitlines()))
        self.assertEqual(self.calls().count("switch recreate-services"), 1)
        self.assertTrue((self.state / "db.up").exists())
        self.assertIn("rollback 完成", result.stdout)
        self.assertEqual(self.protected_digest(), before)
        again = self.run_script("rollback", "--yes")
        self.assertEqual(again.returncode, 1)
        self.assertIn("已回退过", again.stderr)


DSN = os.environ.get("LINGXI_POSTGRES_DSN")
CONTAINER = os.environ.get("LINGXI_TEST_PG_CONTAINER")
SEED = """
CREATE ROLE lingxi_app NOLOGIN; CREATE ROLE lingxi_retention_owner NOLOGIN; CREATE ROLE anon NOLOGIN;
CREATE ROLE supabase_admin NOLOGIN; CREATE ROLE "Weird Role" NOLOGIN; CREATE ROLE "Mixed Owner" NOLOGIN;
CREATE SCHEMA extensions; CREATE EXTENSION pg_trgm WITH SCHEMA extensions;
CREATE TABLE public.t1(id serial PRIMARY KEY, name text); INSERT INTO public.t1(name) VALUES ('a'), ('b'), ('c');
CREATE INDEX t1_trgm ON public.t1 USING gin (name extensions.gin_trgm_ops);
CREATE TABLE public.alembic_version(version_num text PRIMARY KEY); INSERT INTO public.alembic_version VALUES ('0098_test');
ALTER TABLE public.t1 OWNER TO lingxi_app;
CREATE FUNCTION public.lingxi_retention_cleanup(p_now timestamptz, p_days integer) RETURNS integer LANGUAGE sql SECURITY DEFINER AS 'select 1';
ALTER FUNCTION public.lingxi_retention_cleanup(timestamptz, integer) OWNER TO lingxi_retention_owner;
GRANT SELECT, DELETE ON public.t1 TO lingxi_retention_owner; GRANT SELECT ON public.t1 TO "Weird Role";
REVOKE ALL ON FUNCTION public.lingxi_retention_cleanup(timestamptz, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.lingxi_retention_cleanup(timestamptz, integer) TO lingxi_app;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON TABLES TO anon;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT SELECT ON SEQUENCES TO "Mixed Owner";
GRANT USAGE ON SCHEMA public TO anon;
CREATE POLICY p1 ON public.t1 TO anon USING (true);
"""
RESET = """
DROP SCHEMA IF EXISTS extensions CASCADE; DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;
DO $$ DECLARE r text; BEGIN
  FOR r IN SELECT rolname FROM pg_roles WHERE rolname IN ('lingxi_app', 'lingxi_retention_owner', 'anon', 'supabase_admin', 'Weird Role', 'Mixed Owner') LOOP
    EXECUTE format('DROP OWNED BY %I', r); EXECUTE format('DROP ROLE %I', r);
  END LOOP; END $$;
"""


@unittest.skipUnless(
    DSN and CONTAINER,
    "需要真库：设 LINGXI_POSTGRES_DSN 与 LINGXI_TEST_PG_CONTAINER（docker exec 目标）",
)
class RealDatabaseTest(_Base):
    def psql(self, sql: str) -> str:
        done = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                CONTAINER,
                "psql",
                "-U",
                "postgres",
                "-d",
                "postgres",
                "-X",
                "-Atq",
                "-v",
                "ON_ERROR_STOP=1",
                "-f",
                "-",
            ],
            input=sql,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def setUp(self) -> None:
        super().setUp()
        self.psql(RESET)
        self.psql(SEED)
        real = self.base / "real-backups"
        real.mkdir(mode=0o700)
        env = {k: v for k, v in os.environ.items() if not k.startswith("LINGXI_")}
        env.update(
            LINGXI_DB_BACKUP_CONTAINER=CONTAINER,
            LINGXI_DB_BACKUP_DIR=str(real),
            LINGXI_DB_BACKUP_STATUS_FILE=str(self.base / "status.json"),
        )
        done = subprocess.run(
            ["bash", str(BACKUP_SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.real_dump = next(real.glob("lingxi-db-*.dump"))
        self.psql(RESET)  # 目标回到「新 initdb + install-pg 已预建」形
        self.psql("CREATE SCHEMA extensions; CREATE EXTENSION pg_trgm WITH SCHEMA extensions;")
        # 真 docker 取绝对路径：用例环境把桩目录排在 PATH 最前，裸 docker 会落到桩上
        self.overrides.update(
            LINGXI_S30_DOCKER=shutil.which("docker") or "docker", LINGXI_S30_DB_CONTAINER=CONTAINER
        )

    def tearDown(self) -> None:
        self.psql(RESET)
        super().tearDown()

    def test_restore_and_verify_real_backup(self) -> None:
        result = self.run_script("run", str(self.real_dump), "--from=roles", "--until=verify")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("verify 零差异", result.stdout)
        self.assertIn("新建 NOLOGIN 6", result.stdout)  # 含只在默认权限行出现的 "Mixed Owner"
        self.assertEqual(
            self.psql("SELECT count(*) FROM pg_roles WHERE rolname = 'Mixed Owner'"), "1"
        )
        self.assertEqual(self.psql("SELECT count(*) FROM public.t1"), "3")
        self.assertEqual(
            self.psql(
                "SELECT proowner::regrole::text || '|' || prosecdef FROM pg_proc WHERE proname = 'lingxi_retention_cleanup'"
            ),
            "lingxi_retention_owner|true",
        )
        self.assertEqual(
            self.psql("SELECT relowner::regrole::text FROM pg_class WHERE relname = 't1'"),
            "lingxi_app",
        )

    def test_tampered_manifest_fails_verify(self) -> None:
        counts = Path(f"{self.real_dump}.counts.json")
        data = json.loads(counts.read_text(encoding="utf-8"))
        data["tables"]["public.t1"] = 99
        counts.write_text(json.dumps(data), encoding="utf-8")
        result = self.run_script("run", str(self.real_dump), "--from=roles", "--until=verify")
        self.assertEqual(result.returncode, 1)
        self.assertIn("tables|public.t1|清单=99|目标=3", result.stdout)

    def test_changed_grant_content_fails_verify(self) -> None:
        # B10：恢复后把一张表的授权改成另一组（仍非空）→ verify 报差异
        restored = self.run_script("run", str(self.real_dump), "--from=roles", "--until=verify")
        self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
        self.psql(
            'REVOKE SELECT ON public.t1 FROM "Weird Role"; GRANT INSERT ON public.t1 TO "Weird Role";'
        )
        result = self.run_script("run", str(self.real_dump), "--from=verify", "--until=verify")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("acl|ACL public TABLE t1|", result.stdout)
        self.assertIn("verify 未通过", result.stderr)


if __name__ == "__main__":
    unittest.main()
