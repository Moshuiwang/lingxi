"""``scripts/ops/host_health_alert.py`` 的纯逻辑单测（S-H2-3，Trace #373 H2）。

只测状态判定、去重与恢复通知这条决策链——不 mock docker CLI 或飞书 HTTP 全链路
（真实 docker inspect / 真实发送属 L4a，留给 biai-stage 受控注入取证）。凭据文件
权限校验、env 解析与状态文件读写这类纯 I/O 也在无 docker/无网络的机器上直接测。

加载方式沿用既有先例 ``tests/test_replay_inbound_event_script.py``：``scripts/``
不是一个包，用 ``importlib.util.spec_from_file_location`` 按路径直接装载模块。
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).parents[1] / "scripts" / "ops" / "host_health_alert.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("host_health_alert_script_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # 模块用了 @dataclass；dataclasses 内部按 ``sys.modules[cls.__module__]`` 解析
    # 注解，装载期必须先在 sys.modules 挂号，否则会在类体求值时抛
    # ``AttributeError: 'NoneType' object has no attribute '__dict__'``（沿用
    # ``tests/test_acceptance_fixtures_contract.py`` 的既有先例）。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


host_health_alert = _load_script()


class ParseInspectEntryTests(unittest.TestCase):
    def test_missing_container_has_exists_false(self) -> None:
        observation = host_health_alert.parse_inspect_entry("lingxi-scheduler-1", None)
        self.assertFalse(observation.exists)
        self.assertIsNone(observation.running)
        self.assertIsNone(observation.health_status)

    def test_running_healthy_container(self) -> None:
        # 入参就是 `docker container inspect --format '{{json .State}}'` 的
        # 输出本身——已经只剩 State 那一段，不再包了一层 "State" 键。
        state = {"Running": True, "Health": {"Status": "healthy"}}
        observation = host_health_alert.parse_inspect_entry("c", state)
        self.assertTrue(observation.exists)
        self.assertTrue(observation.running)
        self.assertEqual(observation.health_status, "healthy")

    def test_no_healthcheck_configured(self) -> None:
        state = {"Running": True}
        observation = host_health_alert.parse_inspect_entry("c", state)
        self.assertTrue(observation.running)
        self.assertIsNone(observation.health_status)

    def test_malformed_state_does_not_raise(self) -> None:
        observation = host_health_alert.parse_inspect_entry("c", "not-a-mapping")
        self.assertTrue(observation.exists)
        self.assertIsNone(observation.running)
        self.assertIsNone(observation.health_status)


class ClassifyTests(unittest.TestCase):
    def test_missing_triggers(self) -> None:
        c = host_health_alert.classify(host_health_alert.Observation("c", exists=False))
        self.assertTrue(c.trigger)
        self.assertEqual(c.reason, host_health_alert.REASON_MISSING)

    def test_not_running_triggers(self) -> None:
        c = host_health_alert.classify(
            host_health_alert.Observation("c", exists=True, running=False)
        )
        self.assertTrue(c.trigger)
        self.assertEqual(c.reason, host_health_alert.REASON_STOPPED)

    def test_unhealthy_triggers(self) -> None:
        c = host_health_alert.classify(
            host_health_alert.Observation("c", exists=True, running=True, health_status="unhealthy")
        )
        self.assertTrue(c.trigger)
        self.assertEqual(c.reason, host_health_alert.REASON_UNHEALTHY)

    def test_starting_does_not_trigger(self) -> None:
        c = host_health_alert.classify(
            host_health_alert.Observation("c", exists=True, running=True, health_status="starting")
        )
        self.assertFalse(c.trigger)
        self.assertEqual(c.reason, host_health_alert.REASON_STARTING)

    def test_healthy_does_not_trigger(self) -> None:
        c = host_health_alert.classify(
            host_health_alert.Observation("c", exists=True, running=True, health_status="healthy")
        )
        self.assertFalse(c.trigger)
        self.assertEqual(c.reason, host_health_alert.REASON_OK)

    def test_no_healthcheck_does_not_trigger(self) -> None:
        c = host_health_alert.classify(
            host_health_alert.Observation("c", exists=True, running=True, health_status=None)
        )
        self.assertFalse(c.trigger)
        self.assertEqual(c.reason, host_health_alert.REASON_NO_HEALTHCHECK)


class DecideActionTests(unittest.TestCase):
    """去重、恢复通知的状态机——本 Story 的变异验红目标（详见 PR 描述）。"""

    def test_first_trigger_alerts(self) -> None:
        classification = host_health_alert.Classification("c", host_health_alert.REASON_UNHEALTHY, True)
        action, state = host_health_alert.decide_action(classification, host_health_alert.ContainerState())
        self.assertEqual(action, host_health_alert.ACTION_ALERT)
        self.assertTrue(state.alerting)
        self.assertEqual(state.reason, host_health_alert.REASON_UNHEALTHY)

    def test_repeated_same_reason_does_not_realert(self) -> None:
        classification = host_health_alert.Classification("c", host_health_alert.REASON_UNHEALTHY, True)
        prior = host_health_alert.ContainerState(alerting=True, reason=host_health_alert.REASON_UNHEALTHY)
        action, state = host_health_alert.decide_action(classification, prior)
        self.assertEqual(action, host_health_alert.ACTION_NONE)
        self.assertEqual(state, prior)

    def test_reason_change_while_still_triggering_realerts(self) -> None:
        classification = host_health_alert.Classification("c", host_health_alert.REASON_MISSING, True)
        prior = host_health_alert.ContainerState(alerting=True, reason=host_health_alert.REASON_UNHEALTHY)
        action, state = host_health_alert.decide_action(classification, prior)
        self.assertEqual(action, host_health_alert.ACTION_ALERT)
        self.assertEqual(state.reason, host_health_alert.REASON_MISSING)

    def test_recovery_after_alerting(self) -> None:
        classification = host_health_alert.Classification("c", host_health_alert.REASON_OK, False)
        prior = host_health_alert.ContainerState(alerting=True, reason=host_health_alert.REASON_UNHEALTHY)
        action, state = host_health_alert.decide_action(classification, prior)
        self.assertEqual(action, host_health_alert.ACTION_RECOVERY)
        self.assertFalse(state.alerting)
        self.assertIsNone(state.reason)

    def test_no_alert_when_never_triggered(self) -> None:
        classification = host_health_alert.Classification("c", host_health_alert.REASON_OK, False)
        action, state = host_health_alert.decide_action(classification, host_health_alert.ContainerState())
        self.assertEqual(action, host_health_alert.ACTION_NONE)
        self.assertFalse(state.alerting)

    def test_starting_after_alerting_stays_pending(self) -> None:
        """`starting` 只是"重启已开始、宽限期内"，不等于确认恢复；不应该在这里
        提前发恢复通知——真正恢复要等下一轮拿到 healthy/no_healthcheck。"""

        classification = host_health_alert.Classification("c", host_health_alert.REASON_STARTING, False)
        prior = host_health_alert.ContainerState(alerting=True, reason=host_health_alert.REASON_UNHEALTHY)
        action, state = host_health_alert.decide_action(classification, prior)
        self.assertEqual(action, host_health_alert.ACTION_NONE)
        self.assertTrue(state.alerting)
        self.assertEqual(state.reason, host_health_alert.REASON_UNHEALTHY)


class RenderMessageTests(unittest.TestCase):
    def test_alert_message_contains_container_and_reason(self) -> None:
        classification = host_health_alert.Classification("lingxi-gateway-1", host_health_alert.REASON_UNHEALTHY, True)
        text = host_health_alert.render_message(
            host_health_alert.ACTION_ALERT, classification, host="stage-host", now="2026-08-28T00:00:00+00:00"
        )
        self.assertIn("lingxi-gateway-1", text)
        self.assertIn("unhealthy", text)
        self.assertIn("stage-host", text)
        self.assertIn("告警", text)

    def test_recovery_message(self) -> None:
        classification = host_health_alert.Classification("lingxi-gateway-1", host_health_alert.REASON_OK, False)
        text = host_health_alert.render_message(
            host_health_alert.ACTION_RECOVERY, classification, host="stage-host", now="2026-08-28T00:00:00+00:00"
        )
        self.assertIn("恢复", text)
        self.assertIn("lingxi-gateway-1", text)

    def test_none_action_rejected(self) -> None:
        classification = host_health_alert.Classification("c", host_health_alert.REASON_OK, False)
        with self.assertRaises(ValueError):
            host_health_alert.render_message(
                host_health_alert.ACTION_NONE, classification, host="h", now="now"
            )


class CredentialLoadingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.env_path = Path(self._tmp.name) / "env"

    def _write(self, content: str, *, mode: int = 0o600) -> None:
        self.env_path.write_text(content, encoding="utf-8")
        os.chmod(self.env_path, mode)

    def test_valid_file_loads_three_fields(self) -> None:
        self._write(
            "LINGXI_FEISHU_APP_ID=cli_test\n"
            "LINGXI_FEISHU_APP_SECRET=secret_test\n"
            "LINGXI_ADMIN_GROUP_CHAT_ID=oc_test123\n"
        )
        credentials = host_health_alert.load_credentials(self.env_path)
        self.assertEqual(credentials["app_id"], "cli_test")
        self.assertEqual(credentials["app_secret"], "secret_test")
        self.assertEqual(credentials["chat_id"], "oc_test123")

    def test_rejects_world_readable_file(self) -> None:
        self._write(
            "LINGXI_FEISHU_APP_ID=a\nLINGXI_FEISHU_APP_SECRET=b\nLINGXI_ADMIN_GROUP_CHAT_ID=oc_x\n",
            mode=0o644,
        )
        with self.assertRaises(host_health_alert.HostMonitorError) as ctx:
            host_health_alert.load_credentials(self.env_path)
        self.assertIn("permission", str(ctx.exception))
        # 错误信息不回显任何取值。
        self.assertNotIn("a", str(ctx.exception).replace("permission_unsafe", ""))

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(host_health_alert.HostMonitorError):
            host_health_alert.load_credentials(Path(self._tmp.name) / "does-not-exist")

    def test_rejects_file_not_owned_by_caller(self) -> None:
        # 独立审查 P2-4：文档口径是"0600 且属主为运行 cron 的账户"，此前的实现
        # 只核对了权限位。这里用 monkeypatch `os.getuid` 模拟"文件确实是 0600，
        # 但属主不是当前运行账户"这一错配——不依赖能否真的 chown 成另一个账户
        # （测试环境通常没有权限这么做）。
        self._write(
            "LINGXI_FEISHU_APP_ID=a\nLINGXI_FEISHU_APP_SECRET=b\nLINGXI_ADMIN_GROUP_CHAT_ID=oc_x\n"
        )
        with mock.patch.object(host_health_alert.os, "getuid", return_value=os.getuid() + 999):
            with self.assertRaises(host_health_alert.HostMonitorError) as ctx:
                host_health_alert.load_credentials(self.env_path)
        self.assertIn("owner", str(ctx.exception))

    def test_missing_required_key_raises(self) -> None:
        self._write("LINGXI_FEISHU_APP_ID=a\nLINGXI_FEISHU_APP_SECRET=b\n")
        with self.assertRaises(host_health_alert.HostMonitorError) as ctx:
            host_health_alert.load_credentials(self.env_path)
        self.assertIn("missing_keys", str(ctx.exception))

    def test_invalid_chat_id_format_raises(self) -> None:
        self._write(
            "LINGXI_FEISHU_APP_ID=a\nLINGXI_FEISHU_APP_SECRET=b\nLINGXI_ADMIN_GROUP_CHAT_ID=not-a-chat-id\n"
        )
        with self.assertRaises(host_health_alert.HostMonitorError):
            host_health_alert.load_credentials(self.env_path)

    def test_quoted_values_are_unwrapped(self) -> None:
        self._write(
            'LINGXI_FEISHU_APP_ID="cli_test"\n'
            "LINGXI_FEISHU_APP_SECRET='secret_test'\n"
            "LINGXI_ADMIN_GROUP_CHAT_ID=oc_test123\n"
        )
        credentials = host_health_alert.load_credentials(self.env_path)
        self.assertEqual(credentials["app_id"], "cli_test")
        self.assertEqual(credentials["app_secret"], "secret_test")


class StatePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_path = Path(self._tmp.name) / "nested" / "state.json"

    def test_round_trip(self) -> None:
        states = {
            "lingxi-gateway-1": host_health_alert.ContainerState(alerting=True, reason="unhealthy"),
            "lingxi-scheduler-1": host_health_alert.ContainerState(),
        }
        host_health_alert.save_state(self.state_path, states)
        loaded = host_health_alert.load_state(self.state_path)
        self.assertEqual(loaded["lingxi-gateway-1"].alerting, True)
        self.assertEqual(loaded["lingxi-gateway-1"].reason, "unhealthy")
        self.assertEqual(loaded["lingxi-scheduler-1"].alerting, False)

    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(host_health_alert.load_state(self.state_path), {})

    def test_corrupt_file_returns_empty_not_raise(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text("{not valid json", encoding="utf-8")
        self.assertEqual(host_health_alert.load_state(self.state_path), {})

    def test_state_file_created_with_restrictive_parent_permissions(self) -> None:
        host_health_alert.save_state(self.state_path, {})
        mode = stat.S_IMODE(os.stat(self.state_path.parent).st_mode)
        self.assertEqual(mode, 0o700)


class DockerInspectTests(unittest.TestCase):
    """`docker_inspect_one` 改用 `docker container inspect --format
    '{{json .State}}'` 后的行为（独立审查 P2-1/P2-2/P2-3）：不依赖真实 docker
    （真实 docker 属 L4a），用一个自制的伪 `docker_bin` 脚本模拟三种情形。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _fake_docker(self, *, stderr: str, exit_code: int = 1, record_argv: Path | None = None) -> Path:
        path = Path(self._tmp.name) / "fake-docker"
        record_line = f'echo "$@" > "{record_argv}"\n' if record_argv is not None else ""
        path.write_text(
            "#!/bin/sh\n"
            f"{record_line}"
            f'echo "{stderr}" >&2\n'
            f"exit {exit_code}\n",
            encoding="utf-8",
        )
        os.chmod(path, 0o755)
        return path

    def test_missing_binary_raises_host_monitor_error(self) -> None:
        with self.assertRaises(host_health_alert.HostMonitorError):
            host_health_alert.docker_inspect_one(
                "whatever", docker_bin="lingxi-definitely-not-a-real-binary-xyz"
            )

    def test_nonexistent_container_returns_none(self) -> None:
        # stderr 含 "No such container"（`docker container inspect` 对不存在
        # 的容器名的真实文案）——这是正常情况，不是脚本故障。
        docker_bin = self._fake_docker(stderr="Error: No such container: whatever")
        result = host_health_alert.docker_inspect_one("whatever", docker_bin=str(docker_bin))
        self.assertIsNone(result)

    def test_daemon_unreachable_raises_host_monitor_error(self) -> None:
        # daemon 不可达（或权限不足）时 stderr 不含 "No such container"/"No
        # such object"——必须区分对待，抛出脚本级故障，不能悄悄当成"容器不
        # 存在"（P2-3：否则 daemon 抖动又恢复会被误判成一次假恢复）。
        docker_bin = self._fake_docker(
            stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
            "Is the docker daemon running?"
        )
        with self.assertRaises(host_health_alert.HostMonitorError) as ctx:
            host_health_alert.docker_inspect_one("whatever", docker_bin=str(docker_bin))
        self.assertIn("daemon_error", str(ctx.exception))

    def test_uses_container_inspect_subcommand_with_state_only_format(self) -> None:
        # 核对确实调用的是 `container inspect --format '{{json .State}}'`，
        # 不是裸 `inspect`（P2-2：裸 inspect 跨对象类型查找，可能被同名的
        # 镜像/网络/卷对象误命中；且只取 State，不该出现 Config 字样）。
        argv_path = Path(self._tmp.name) / "argv.txt"
        docker_bin = self._fake_docker(
            stderr="Error: No such container: x", record_argv=argv_path
        )
        host_health_alert.docker_inspect_one("x", docker_bin=str(docker_bin))
        recorded = argv_path.read_text(encoding="utf-8")
        self.assertIn("container", recorded.split())
        self.assertIn("inspect", recorded.split())
        self.assertIn("--format", recorded)
        self.assertIn(".State", recorded)
        self.assertNotIn("Config", recorded)


class RunIntegrationTests(unittest.TestCase):
    """端到端跑一遍 ``run()``：伪造 ``docker_bin``、monkeypatch 飞书发送，验证
    「发送失败不落盘状态、下一轮据此重试」与「去重/恢复通知」这两条决策纪律在真正
    的调用路径上成立——不依赖真实 docker 或真实网络（真实链路属 L4a）。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp_path = Path(self._tmp.name)

        self.env_path = tmp_path / "env"
        self.env_path.write_text(
            "LINGXI_FEISHU_APP_ID=a\nLINGXI_FEISHU_APP_SECRET=b\nLINGXI_ADMIN_GROUP_CHAT_ID=oc_x\n",
            encoding="utf-8",
        )
        os.chmod(self.env_path, 0o600)

        # 伪造的 `docker` 可执行文件：`container inspect` 子命令原样吐出预先
        # 写好的 `State` JSON（与真实 `--format '{{json .State}}'` 的输出形状
        # 一致——只是 State 那一段，不包一层数组或 "State" 键），让 run() 走
        # 真实的 subprocess 调用路径，但结果由测试完全控制。
        self.inspect_output = tmp_path / "inspect_output.json"
        self.docker_bin = tmp_path / "fake-docker"
        self.docker_bin.write_text(
            "#!/bin/sh\n"
            f'if [ "$1" = "container" ] && [ "$2" = "inspect" ]; then '
            f'cat "{self.inspect_output}"; exit 0; fi\n'
            "exit 1\n",
            encoding="utf-8",
        )
        os.chmod(self.docker_bin, 0o755)

        self.state_path = tmp_path / "state.json"
        self.log_path = tmp_path / "log.txt"
        self.lock_path = tmp_path / "lock"

    def _set_container_state(self, *, running: bool, health_status: str | None) -> None:
        state: dict = {"Running": running}
        if health_status is not None:
            state["Health"] = {"Status": health_status}
        self.inspect_output.write_text(json.dumps(state), encoding="utf-8")

    def _run(self, extra_argv: list[str] | None = None) -> int:
        argv = [
            "--env-file", str(self.env_path),
            "--containers", "target-container",
            "--state-file", str(self.state_path),
            "--log-file", str(self.log_path),
            "--lock-file", str(self.lock_path),
            "--docker-bin", str(self.docker_bin),
        ]
        if extra_argv:
            argv.extend(extra_argv)
        return host_health_alert.run(argv)

    def test_send_failure_does_not_persist_state_and_retries_next_run(self) -> None:
        self._set_container_state(running=True, health_status="unhealthy")

        with mock.patch.object(
            host_health_alert,
            "feishu_send_text",
            side_effect=host_health_alert.HostMonitorError("simulated_send_failure"),
        ) as sender:
            exit_code = self._run()
        self.assertEqual(exit_code, 0)
        self.assertEqual(sender.call_count, 1)
        # 发送失败：状态文件不应该被创建/更新——下一轮必须能重新判定为"首次触发"。
        self.assertEqual(host_health_alert.load_state(self.state_path), {})

        with mock.patch.object(host_health_alert, "feishu_send_text") as sender:
            exit_code = self._run()
        self.assertEqual(exit_code, 0)
        self.assertEqual(sender.call_count, 1)
        state = host_health_alert.load_state(self.state_path)
        self.assertTrue(state["target-container"].alerting)
        self.assertEqual(state["target-container"].reason, host_health_alert.REASON_UNHEALTHY)

    def test_unexpected_send_exception_does_not_crash_or_persist_state(self) -> None:
        # 独立审查 P2-5：发送路径此前只兜住 `HostMonitorError`，任何其它异常类型
        # （标准库网络/JSON 原语可能抛出的、没被脚本主动枚举到的那些）会让
        # `run()` 整体崩溃退出，退化成"这一轮别的容器也没被检查"，而不是"这一个
        # 容器的发送失败被记录并等待下一轮重试"。这里故意用一个不属于
        # `HostMonitorError` 家族的普通异常验证兜底生效。
        self._set_container_state(running=True, health_status="unhealthy")

        with mock.patch.object(
            host_health_alert,
            "feishu_send_text",
            side_effect=ValueError("simulated_unexpected_error"),
        ) as sender:
            exit_code = self._run()
        self.assertEqual(exit_code, 0)
        self.assertEqual(sender.call_count, 1)
        # 状态未落盘：下一轮仍会重新判定为"首次触发"并重试发送。
        self.assertEqual(host_health_alert.load_state(self.state_path), {})

        with mock.patch.object(host_health_alert, "feishu_send_text") as sender:
            exit_code = self._run()
        self.assertEqual(exit_code, 0)
        self.assertEqual(sender.call_count, 1)
        state = host_health_alert.load_state(self.state_path)
        self.assertTrue(state["target-container"].alerting)

    def test_successful_alert_then_dedupe_then_recovery_round_trip(self) -> None:
        self._set_container_state(running=True, health_status="unhealthy")
        with mock.patch.object(host_health_alert, "feishu_send_text") as sender:
            self._run()
        self.assertEqual(sender.call_count, 1)

        # 原因不变：同一事件不应该再发一次。
        with mock.patch.object(host_health_alert, "feishu_send_text") as sender:
            self._run()
        self.assertEqual(sender.call_count, 0)

        # 恢复健康：应该发一条恢复通知，并清空记忆状态。
        self._set_container_state(running=True, health_status="healthy")
        with mock.patch.object(host_health_alert, "feishu_send_text") as sender:
            self._run()
        self.assertEqual(sender.call_count, 1)
        state = host_health_alert.load_state(self.state_path)
        self.assertFalse(state["target-container"].alerting)

    def test_dry_run_never_calls_sender_or_persists_state(self) -> None:
        self._set_container_state(running=False, health_status=None)
        with mock.patch.object(host_health_alert, "feishu_send_text") as sender:
            exit_code = self._run(["--dry-run"])
        self.assertEqual(exit_code, 0)
        sender.assert_not_called()
        self.assertFalse(self.state_path.exists())

    def test_missing_env_file_returns_exit_code_two_without_touching_docker(self) -> None:
        os.remove(self.env_path)
        with mock.patch.object(host_health_alert, "docker_inspect_one") as inspector:
            exit_code = self._run()
        self.assertEqual(exit_code, 2)
        inspector.assert_not_called()


if __name__ == "__main__":
    unittest.main()
