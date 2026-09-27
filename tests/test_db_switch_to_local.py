"""``scripts/ops/db_switch_to_local.sh`` 的假根目录用例（#896 入仓；#886 第 2 处回读判据）。

脚本的测试形是「非 root + ``LINGXI_S30_ROOT=<假根>``」：宿主路径全部加假根前缀、跳过属主设置，
``docker`` / ``systemctl`` 走 ``tests/support/fake_db_switch.py`` 的桩。这里覆盖：运行身份与前置检查的拒绝
路径、preflight 的来源连接串不外泄、来源连接串文件可改（预发保底导出替身库用）、install-pg 的幂等与
回读判据、switch-dsn 在改任何文件之前的前置门、status 汇总。停写 / dump / restore / verify / postcheck
依赖真实库，不在本文件范围（预发实跑见 #896）。
"""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

from support.fake_db_switch import base_env, install_recreate_docker, install_stubs

REPOSITORY_ROOT = Path(__file__).parents[1]
SCRIPT = REPOSITORY_ROOT / "scripts" / "ops" / "db_switch_to_local.sh"
COMPOSE = REPOSITORY_ROOT / "deploy" / "compose.db.yaml"
SECRET = "s30-sentinel-pw-7c1e"
SOURCE_HOST = "source-host.invalid:6543"
ENV_SERVICES = ("scheduler", "gateway", "worker", "worker-queue", "migrate", "reauthorize")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _FakeRootBase(unittest.TestCase):
    """假根目录现场：以临时目录充当 ``/``，桩替换 docker / systemctl。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="lingxi-896-s30-")
        base = Path(self._tmp.name)
        self.base = base
        self.root = base / "root"
        self.state = base / "state"
        self.bin = base / "bin"
        self.root.mkdir()
        self.state.mkdir()
        self.stubs = install_stubs(self.bin)
        contract = self.root / "opt" / "lingxi" / "control" / "host-contract.json"
        contract.parent.mkdir(parents=True)
        contract.write_text(
            json.dumps(
                {"project": "lingxi", "environment": "stage", "config_root": "/opt/lingxi/config"}
            ),
            encoding="utf-8",
        )
        self.config = self.root / "opt" / "lingxi" / "config"
        self.config.mkdir(parents=True)
        dsn = f"postgresql+psycopg://lingxi_app:{SECRET}@{SOURCE_HOST}/postgres?sslmode=require"
        (self.config / ".env.stage").write_text("LINGXI_ENVIRONMENT=stage\n", encoding="utf-8")
        for name in ENV_SERVICES:
            key = "LINGXI_MIGRATION_DSN" if name == "migrate" else "LINGXI_POSTGRES_DSN"
            (self.config / f".env.stage.{name}").write_text(f"{key}='{dsn}'\n", encoding="utf-8")
        self.monitor_env = self.root / "opt" / "lingxi" / "monitoring" / "db-business.env"
        self.monitor_env.parent.mkdir(parents=True)
        self.monitor_env.write_text(f"LINGXI_POSTGRES_DSN={dsn}\n", encoding="utf-8")
        self.hosts = self.root / "etc" / "hosts"
        self.hosts.parent.mkdir(parents=True)
        self.hosts.write_text("127.0.0.1 localhost", encoding="utf-8")  # 故意无尾换行
        self.env_file = base / "db_switch_to_local.env"
        self.port = _free_port()
        self.write_inputs()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_inputs(self, **overrides: str) -> None:
        values = {
            "LINGXI_S30_ENV_SUFFIX": "stage",
            "LINGXI_S30_DOCKER": str(self.stubs["docker"]),
            "LINGXI_S30_SYSTEMCTL": str(self.stubs["systemctl"]),
            "LINGXI_S30_COMPOSE_SRC": str(COMPOSE),
            "LINGXI_S30_COMPOSE_SHA": _sha(COMPOSE),
            "LINGXI_S30_DB_HOST_PORT": str(self.port),
            "LINGXI_S30_MIN_FREE_GB": "0",
        }
        values.update(overrides)
        text = "# 用例输入\n" + "".join(f"{k}={v}\n" for k, v in values.items())
        self.env_file.write_text(text, encoding="utf-8")

    def run_script(self, *args: str, root: bool = True) -> subprocess.CompletedProcess[str]:
        env = base_env(self.bin, self.state, self.base / "units")
        env["LINGXI_S30_ENV_FILE"] = str(self.env_file)
        env["FAKE_DB_HOST_PORT"] = str(self.port)
        if root:
            env["LINGXI_S30_ROOT"] = str(self.root)
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def env_snapshot(self) -> dict[Path, str]:
        files = [self.config / ".env.stage", self.monitor_env]
        files += [self.config / f".env.stage.{name}" for name in ENV_SERVICES]
        return {p: _sha(p) for p in files}

    def calls_log(self) -> str:
        log = self.state / "calls.log"
        return log.read_text(encoding="utf-8") if log.exists() else ""


class DbSwitchToLocalFakeRootTest(_FakeRootBase):
    """跑切换脚本不碰真库的那几步。"""

    # --- 运行身份与前置检查 ---

    def test_refuses_non_root_without_fake_root(self) -> None:
        result = self.run_script("status", root=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("必须以 root 运行", result.stderr)

    def test_unknown_subcommand_and_usage(self) -> None:
        usage = self.run_script()
        self.assertEqual(usage.returncode, 2)
        self.assertTrue(usage.stderr.startswith("# 用法：sudo -n bash db_switch_to_local.sh"))
        self.assertIn("#   smoke-test ", usage.stderr)
        self.assertIn("#   recreate-services ", usage.stderr)
        unknown = self.run_script("frobnicate")
        self.assertEqual(unknown.returncode, 1)
        self.assertIn("未知子命令", unknown.stderr)

    def test_preflight_rejects_bad_compose_sha_and_missing_network(self) -> None:
        self.write_inputs(LINGXI_S30_COMPOSE_SHA="0" * 64, LINGXI_S30_APP_NETWORK="no_such_net")
        result = self.run_script("preflight")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("前置不满足：compose 副本 sha ≠ 期望", result.stdout)
        self.assertIn("前置不满足：应用网络不存在：no_such_net", result.stdout)
        self.assertNotIn(SECRET, result.stdout + result.stderr)

    def test_preflight_passes_without_leaking_source_dsn(self) -> None:
        result = self.run_script("preflight")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("preflight 通过", result.stdout)
        self.assertIn(f"来源连接串文件：{self.config / '.env.stage.migrate'}", result.stdout)
        self.assertNotIn(SECRET, result.stdout + result.stderr)
        self.assertNotIn(SECRET, self.calls_log())  # 连接串只经 --env-file 进容器，不进命令行参数
        seen = (self.state / "source_dsn_seen").read_text(encoding="utf-8").strip()
        self.assertEqual(
            seen, f"postgresql://lingxi_app:{SECRET}@{SOURCE_HOST}/postgres?sslmode=require"
        )

    def test_source_env_file_override_reads_stand_in(self) -> None:
        """#896：来源不可读时改读替身库连接串文件；缺省仍是 .env.<env>.migrate。"""
        stand_in = self.base / "source-stand-in.env"
        stand_in.write_text(
            "LINGXI_MIGRATION_DSN=postgresql://postgres@127.0.0.1:55433/postgres\n",
            encoding="utf-8",
        )
        self.write_inputs(LINGXI_S30_SOURCE_ENV_FILE=str(stand_in))
        result = self.run_script("preflight")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"来源连接串文件：{stand_in}", result.stdout)
        seen = (self.state / "source_dsn_seen").read_text(encoding="utf-8").strip()
        self.assertEqual(seen, "postgresql://postgres@127.0.0.1:55433/postgres")
        missing = self.base / "absent.env"
        self.write_inputs(LINGXI_S30_SOURCE_ENV_FILE=str(missing))
        refused = self.run_script("preflight")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn(f"前置不满足：来源连接串文件不存在：{missing}", refused.stdout)

    # --- install-pg：幂等与回读判据 ---

    def test_install_pg_readback_criteria_and_idempotence(self) -> None:
        self.assertEqual(self.run_script("preflight").returncode, 0)
        first = self.run_script("install-pg")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn("[hosts] added", first.stdout)
        hosts_lines = self.hosts.read_text(encoding="utf-8").splitlines()
        self.assertEqual(hosts_lines[0], "127.0.0.1 localhost")  # 原文无尾换行：先补换行，不粘连
        self.assertTrue(hosts_lines[1].startswith("127.0.0.1 lingxi-db  #"), hosts_lines)
        self.assertEqual(len(hosts_lines), 2)
        compose_env = self.root / "etc" / "lingxi" / "db" / "compose.env"
        printed = _sha(compose_env)
        self.assertIn(f"[compose.env] written sha={printed}", first.stdout)
        self.assertIn(
            f"[回读判据] compose.env 现场生成：在位 sha={printed} = 本次打印值（不与输入文件 sha 比对）→ ok",
            first.stdout,
        )
        self.assertIn(
            f"[回读判据] compose.db.yaml 输入副本：在位 sha={_sha(COMPOSE)} = LINGXI_S30_COMPOSE_SHA → ok",
            first.stdout,
        )
        self.assertIn(
            "[回读判据] .env.db 现场生成：存在、非空、0600（内容与 sha 不打印）→ ok", first.stdout
        )
        self.assertIn(
            "[回读判据] hosts 行 现场生成：127.0.0.1 lingxi-db 匹配行 1 → ok", first.stdout
        )
        password = (self.root / "etc" / "lingxi" / "db" / ".env.db").read_text(encoding="utf-8")
        self.assertNotIn(password.split("=", 1)[1].strip(), first.stdout + first.stderr)

        hosts_before = self.hosts.read_bytes()
        second = self.run_script("install-pg")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn(f"[compose.env] already_in_place sha={printed}", second.stdout)
        self.assertIn("[口令] already_in_place", second.stdout)
        self.assertIn(f"[compose] already_in_place sha={_sha(COMPOSE)}", second.stdout)
        self.assertIn("[hosts] already_in_place（匹配行 1）", second.stdout)
        self.assertIn("角色级 search_path already_in_place", second.stdout)
        self.assertEqual(self.hosts.read_bytes(), hosts_before)
        self.assertNotIn("不符", first.stdout + second.stdout)

    # --- switch-dsn 前置门与 status ---

    def test_switch_dsn_refuses_before_touching_env_files(self) -> None:
        self.assertEqual(self.run_script("preflight").returncode, 0)
        before = self.env_snapshot()
        no_hosts = self.run_script("switch-dsn")
        self.assertEqual(no_hosts.returncode, 1)
        self.assertIn("先 install-pg", no_hosts.stderr)
        with self.hosts.open("a", encoding="utf-8") as handle:
            handle.write("\n127.0.0.1 lingxi-db\n")
        no_verify = self.run_script("switch-dsn")
        self.assertEqual(no_verify.returncode, 1)
        self.assertIn("缺 verify.json", no_verify.stderr)
        self.assertEqual(self.env_snapshot(), before)
        self.assertFalse((self.root / "root" / "lingxi-s30-backup").exists())

    def test_status_summarizes_without_secrets(self) -> None:
        self.assertEqual(self.run_script("preflight").returncode, 0)
        self.assertEqual(self.run_script("install-pg").returncode, 0)
        result = self.run_script("status")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("本地库 lingxi-db=healthy", result.stdout)
        self.assertIn("hosts 行=1", result.stdout)
        self.assertIn("env .env.stage.gateway：DSN 行 1、指向本地库 0", result.stdout)
        self.assertNotIn(SECRET, result.stdout + result.stderr)


def _fingerprint(value: object) -> str:
    """与部署器 ``deploy_state.fingerprint`` 同一编码。"""
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    return hashlib.sha256(text.encode()).hexdigest()


class RecreateServicesFakeRootTest(_FakeRootBase):
    """recreate-services（#896）：从三容器标签与部署器状态账回读参数，按部署器 start 同一 argv 重建。"""

    BUNDLE = "b" * 64
    PLAN_ID = "20260927-fake0001"
    TAG = "v2.6.1-rc.1"
    STATE = "/opt/lingxi/state"
    CONFIG_ROOT = "/opt/lingxi/config"
    PUBLIC_SECRET = "recreate-sentinel-pw-91ab"

    def setUp(self) -> None:
        super().setUp()
        contract = self.root / "opt" / "lingxi" / "control" / "host-contract.json"
        contract.write_text(
            json.dumps(
                {
                    "project": "lingxi",
                    "environment": "stage",
                    "config_root": self.CONFIG_ROOT,
                    "deploy_root": "/opt/lingxi",
                    "bundle_root": "/opt/lingxi/bundles",
                }
            ),
            encoding="utf-8",
        )
        self.docker = install_recreate_docker(self.bin, self.state)
        deploy_dir = self.root / "opt" / "lingxi" / "bundles" / self.BUNDLE / "deploy"
        deploy_dir.mkdir(parents=True)
        (deploy_dir / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
        (deploy_dir / "compose.stage.yaml").write_text("services: {}\n", encoding="utf-8")
        state_dir = self.root / self.STATE.lstrip("/")
        state_dir.mkdir(parents=True)
        (state_dir / f"{self.PLAN_ID}.channel.compose.json").write_text("{}\n", encoding="utf-8")
        self.public_config = {
            "schema": 1,
            "values": {
                "LINGXI_WORKER_MAX_CONCURRENCY": "4",
                "LINGXI_FAKE_DSN": f"postgresql://u:{self.PUBLIC_SECRET}@h/db",
            },
            "files": {"scheduler": {}, "worker": {}},
        }
        control = self.root / "opt" / "lingxi" / "control"
        (control / "public-config.json").write_text(
            json.dumps(self.public_config), encoding="utf-8"
        )
        (control / "release-pull-agent.json").write_text(
            json.dumps({"public_config": "/opt/lingxi/control/public-config.json"}),
            encoding="utf-8",
        )
        self.config_sha = _fingerprint(self.public_config)
        self.images = {
            name: f"ghcr.io/moshuiwang/lingxi-{name}:{self.TAG}@sha256:{digit * 64}"
            for name, digit in (("scheduler", "1"), ("gateway", "2"), ("worker", "3"), ("migrate", "4"))
        }
        plan = {
            "id": self.PLAN_ID,
            "project": "lingxi",
            "environment": "stage",
            "config_sha256": self.config_sha,
            "new": {
                "schema": 2,
                "repository": "Moshuiwang/lingxi",
                "tag": self.TAG,
                "control_bundle": {"sha256": self.BUNDLE},
                "images": self.images,
            },
        }
        (state_dir / f"{self.PLAN_ID}.plan.json").write_text(json.dumps(plan), encoding="utf-8")
        self.config_files = [
            f"/opt/lingxi/bundles/{self.BUNDLE}/deploy/compose.yaml",
            f"/opt/lingxi/bundles/{self.BUNDLE}/deploy/compose.stage.yaml",
            f"{self.STATE}/{self.PLAN_ID}.channel.compose.json",
        ]
        containers = []
        for service in ("scheduler", "gateway", "worker-queue"):
            image_key = "worker" if service == "worker-queue" else service
            containers.append(
                {
                    "id": f"id-{service}",
                    "name": f"lingxi-{service}-1",
                    "health": "healthy",
                    "image": self.images[image_key],
                    "labels": {
                        "com.docker.compose.project": "lingxi",
                        "com.docker.compose.service": service,
                        "com.docker.compose.project.working_dir": self.CONFIG_ROOT,
                        "com.docker.compose.project.config_files": ",".join(self.config_files),
                        "io.lingxi.config-sha256": self.config_sha,
                        "io.lingxi.bundle-sha256": self.BUNDLE,
                        "io.lingxi.deployment-id": self.PLAN_ID,
                    },
                }
            )
        self.world = {"containers": containers, "after_health": "healthy"}
        self.save_world()
        self.write_inputs(LINGXI_S30_DOCKER=str(self.docker))

    def save_world(self) -> None:
        (self.state / "world.json").write_text(json.dumps(self.world), encoding="utf-8")

    def compose_calls(self) -> list[dict]:
        record = self.state / "compose_up.json"
        return json.loads(record.read_text(encoding="utf-8")) if record.exists() else []

    def expected_env(self) -> dict[str, str]:
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/opt/lingxi",
            "LINGXI_ENV_ROOT": self.CONFIG_ROOT,
            **self.public_config["values"],
            "LINGXI_IMAGE_REGISTRY": "ghcr.io/moshuiwang",
            "LINGXI_IMAGE_TAG": self.TAG,
        }
        for name, image in self.images.items():
            env[f"LINGXI_{name.upper()}_IMAGE_DIGEST"] = "@" + image.split("@")[1]
        return env

    def expected_compose_args(self) -> list[str]:
        args = ["compose", "--project-name", "lingxi", "--project-directory", self.CONFIG_ROOT]
        for path in self.config_files:
            args += ["-f", path]
        args += ["--profile", "mvp", "up", "-d", "--force-recreate", "--no-build"]
        return args + ["--pull", "never", "scheduler", "gateway", "worker-queue"]

    def test_dry_run_prints_full_argv_without_secret(self) -> None:
        result = self.run_script("recreate-services", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        printed = [
            line.split("] ", 1)[1].split(": ", 1)[1]
            for line in result.stdout.splitlines()
            if line.startswith("s30 [argv] ")
        ]
        env_items = [
            f"{key}=<已隐去>" if "DSN" in key else f"{key}={value}"
            for key, value in self.expected_env().items()
        ]
        expected = ["env", "-i", *env_items, str(self.docker), *self.expected_compose_args()]
        self.assertEqual(printed, expected)
        self.assertNotIn(self.PUBLIC_SECRET, result.stdout + result.stderr)
        self.assertIn("dry-run：未执行", result.stdout)
        self.assertEqual(self.compose_calls(), [])

    def test_refuses_inconsistent_labels(self) -> None:
        self.world["containers"][1]["labels"]["io.lingxi.deployment-id"] = "20260926-other"
        self.save_world()
        result = self.run_script("recreate-services")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("前置不满足：三容器标签不一致", result.stdout + result.stderr)
        self.assertEqual(self.compose_calls(), [])

    def test_refuses_missing_config_file(self) -> None:
        missing = self.root / self.config_files[1].lstrip("/")
        missing.unlink()
        result = self.run_script("recreate-services")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"前置不满足：compose 文件不存在：{missing}", result.stdout + result.stderr)
        self.assertEqual(self.compose_calls(), [])

    def test_recreates_once_with_deployer_argv_and_env(self) -> None:
        result = self.run_script("recreate-services")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.compose_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["argv"], self.expected_compose_args())
        self.assertEqual(calls[0]["env"], self.expected_env())
        self.assertIn("[回读判据] 三容器 Recreate ×3（容器 id 全部更换）→ ok", result.stdout)
        self.assertIn("[回读判据] 三容器 healthy", result.stdout)
        self.assertIn("[回读判据] 三容器标签与镜像与重建前逐一相同 → ok", result.stdout)
        self.assertNotIn(self.PUBLIC_SECRET, result.stdout + result.stderr)

    def test_health_timeout_fails(self) -> None:
        self.world["after_health"] = "starting"
        self.save_world()
        self.write_inputs(
            LINGXI_S30_DOCKER=str(self.docker),
            LINGXI_S30_RECREATE_TIMEOUT="2",
            LINGXI_S30_RECREATE_POLL="1",
        )
        result = self.run_script("recreate-services")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("超时", result.stderr)
        self.assertEqual(len(self.compose_calls()), 1)


if __name__ == "__main__":
    unittest.main()
