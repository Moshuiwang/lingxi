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
from datetime import UTC, datetime, timedelta
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
        classification = host_health_alert.Classification(
            "c", host_health_alert.REASON_UNHEALTHY, True
        )
        action, state = host_health_alert.decide_action(
            classification, host_health_alert.ContainerState()
        )
        self.assertEqual(action, host_health_alert.ACTION_ALERT)
        self.assertTrue(state.alerting)
        self.assertEqual(state.reason, host_health_alert.REASON_UNHEALTHY)

    def test_repeated_same_reason_does_not_realert(self) -> None:
        classification = host_health_alert.Classification(
            "c", host_health_alert.REASON_UNHEALTHY, True
        )
        prior = host_health_alert.ContainerState(
            alerting=True, reason=host_health_alert.REASON_UNHEALTHY
        )
        action, state = host_health_alert.decide_action(classification, prior)
        self.assertEqual(action, host_health_alert.ACTION_NONE)
        self.assertEqual(state, prior)

    def test_reason_change_while_still_triggering_realerts(self) -> None:
        classification = host_health_alert.Classification(
            "c", host_health_alert.REASON_MISSING, True
        )
        prior = host_health_alert.ContainerState(
            alerting=True, reason=host_health_alert.REASON_UNHEALTHY
        )
        action, state = host_health_alert.decide_action(classification, prior)
        self.assertEqual(action, host_health_alert.ACTION_ALERT)
        self.assertEqual(state.reason, host_health_alert.REASON_MISSING)

    def test_recovery_after_alerting(self) -> None:
        classification = host_health_alert.Classification("c", host_health_alert.REASON_OK, False)
        prior = host_health_alert.ContainerState(
            alerting=True, reason=host_health_alert.REASON_UNHEALTHY
        )
        action, state = host_health_alert.decide_action(classification, prior)
        self.assertEqual(action, host_health_alert.ACTION_RECOVERY)
        self.assertFalse(state.alerting)
        self.assertIsNone(state.reason)

    def test_no_alert_when_never_triggered(self) -> None:
        classification = host_health_alert.Classification("c", host_health_alert.REASON_OK, False)
        action, state = host_health_alert.decide_action(
            classification, host_health_alert.ContainerState()
        )
        self.assertEqual(action, host_health_alert.ACTION_NONE)
        self.assertFalse(state.alerting)

    def test_starting_after_alerting_stays_pending(self) -> None:
        """`starting` 只是"重启已开始、宽限期内"，不等于确认恢复；不应该在这里
        提前发恢复通知——真正恢复要等下一轮拿到 healthy/no_healthcheck。"""

        classification = host_health_alert.Classification(
            "c", host_health_alert.REASON_STARTING, False
        )
        prior = host_health_alert.ContainerState(
            alerting=True, reason=host_health_alert.REASON_UNHEALTHY
        )
        action, state = host_health_alert.decide_action(classification, prior)
        self.assertEqual(action, host_health_alert.ACTION_NONE)
        self.assertTrue(state.alerting)
        self.assertEqual(state.reason, host_health_alert.REASON_UNHEALTHY)


class RenderMessageTests(unittest.TestCase):
    def test_alert_message_contains_container_and_reason(self) -> None:
        classification = host_health_alert.Classification(
            "lingxi-gateway-1", host_health_alert.REASON_UNHEALTHY, True
        )
        text = host_health_alert.render_message(
            host_health_alert.ACTION_ALERT,
            classification,
            host="stage-host",
            now="2026-08-28T00:00:00+00:00",
        )
        self.assertIn("lingxi-gateway-1", text)
        self.assertIn("unhealthy", text)
        self.assertIn("stage-host", text)
        self.assertIn("告警", text)
        # #443 对外名称规范：监控告警群消息不得带内部代号「Lingxi」，对外统一「BI Plus」。
        self.assertIn("BI Plus", text)
        self.assertNotIn("[Lingxi", text)

    def test_recovery_message(self) -> None:
        classification = host_health_alert.Classification(
            "lingxi-gateway-1", host_health_alert.REASON_OK, False
        )
        text = host_health_alert.render_message(
            host_health_alert.ACTION_RECOVERY,
            classification,
            host="stage-host",
            now="2026-08-28T00:00:00+00:00",
        )
        self.assertIn("恢复", text)
        self.assertIn("lingxi-gateway-1", text)
        self.assertIn("BI Plus", text)
        self.assertNotIn("[Lingxi", text)

    def test_none_action_rejected(self) -> None:
        classification = host_health_alert.Classification("c", host_health_alert.REASON_OK, False)
        with self.assertRaises(ValueError):
            host_health_alert.render_message(
                host_health_alert.ACTION_NONE, classification, host="h", now="now"
            )


class NowIsoTimezoneAnnotationTests(unittest.TestCase):
    """B-8 遗留第 3 项（Trace #469 修复包 B）核实结论：``_now_iso()`` 用于
    `render_message`/`render_threshold_message` 里「时间：{now}」这一行，已经
    是带显式 UTC 偏移量的 ISO-8601 字符串（``datetime.now(timezone.utc)
    .astimezone().isoformat()``——先转成本机时区的 aware datetime，再序列化，
    偏移量永远和序列化时刻的本机时钟一致），与 ``core/alerting.py`` S-1 修复
    的告警范式（``AlertNotice.text`` 的 ``self.observed_at.isoformat()``，
    同样是 aware datetime 直接 isoformat，同一套"数字偏移量而非人类可读时区名"
    表达方式）完全一致——不是本批新引入的裸 ``datetime.now()`` 无时区字符串。
    核实证据，不改代码。"""

    def test_now_iso_includes_an_explicit_utc_offset(self) -> None:
        text = host_health_alert._now_iso()

        parsed = datetime.fromisoformat(text)

        self.assertIsNotNone(parsed.tzinfo, "时间戳必须带显式时区，不能是裸 naive datetime")
        self.assertIsNotNone(parsed.utcoffset())


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

    def _fake_docker(
        self, *, stderr: str, exit_code: int = 1, record_argv: Path | None = None
    ) -> Path:
        path = Path(self._tmp.name) / "fake-docker"
        record_line = f'echo "$@" > "{record_argv}"\n' if record_argv is not None else ""
        path.write_text(
            f'#!/bin/sh\n{record_line}echo "{stderr}" >&2\nexit {exit_code}\n',
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
        docker_bin = self._fake_docker(stderr="Error: No such container: x", record_argv=argv_path)
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
            "--env-file",
            str(self.env_path),
            "--containers",
            "target-container",
            "--state-file",
            str(self.state_path),
            "--log-file",
            str(self.log_path),
            "--lock-file",
            str(self.lock_path),
            "--docker-bin",
            str(self.docker_bin),
            # 本类只关心容器主线：关掉默认开启的拉取代理单元检查，不让本机 systemd
            # 的真实状态（本机没有这个单元）混进 `sender.call_count`。
            "--disable-release-pull-check",
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


class ClassifyThresholdTests(unittest.TestCase):
    """`classify_threshold` 的连续计数与去重/恢复判定（S-RC20-410，Issue #410）。"""

    def test_single_breach_below_requirement_does_not_alert(self) -> None:
        action, state = host_health_alert.classify_threshold(
            True, host_health_alert.ThresholdState(), consecutive_required=3
        )
        self.assertEqual(action, host_health_alert.ACTION_NONE)
        self.assertEqual(state, host_health_alert.ThresholdState(alerting=False, consecutive=1))

    def test_reaching_requirement_alerts_once(self) -> None:
        state = host_health_alert.ThresholdState()
        for _ in range(2):
            _, state = host_health_alert.classify_threshold(True, state, consecutive_required=3)
        action, state = host_health_alert.classify_threshold(True, state, consecutive_required=3)
        self.assertEqual(action, host_health_alert.ACTION_ALERT)
        self.assertEqual(state, host_health_alert.ThresholdState(alerting=True, consecutive=3))

    def test_continuing_breach_after_alert_does_not_realert(self) -> None:
        alerting_state = host_health_alert.ThresholdState(alerting=True, consecutive=3)
        action, state = host_health_alert.classify_threshold(
            True, alerting_state, consecutive_required=3
        )
        self.assertEqual(action, host_health_alert.ACTION_NONE)
        self.assertEqual(state, host_health_alert.ThresholdState(alerting=True, consecutive=4))

    def test_single_non_breach_resets_consecutive_count(self) -> None:
        mid_count_state = host_health_alert.ThresholdState(alerting=False, consecutive=2)
        action, state = host_health_alert.classify_threshold(
            False, mid_count_state, consecutive_required=3
        )
        self.assertEqual(action, host_health_alert.ACTION_NONE)
        self.assertEqual(state, host_health_alert.ThresholdState(alerting=False, consecutive=0))

    def test_recovery_after_alerting(self) -> None:
        alerting_state = host_health_alert.ThresholdState(alerting=True, consecutive=5)
        action, state = host_health_alert.classify_threshold(
            False, alerting_state, consecutive_required=3
        )
        self.assertEqual(action, host_health_alert.ACTION_RECOVERY)
        self.assertEqual(state, host_health_alert.ThresholdState(alerting=False, consecutive=0))

    def test_consecutive_required_one_alerts_immediately(self) -> None:
        # 磁盘/停更两项用 consecutive_required=1：单次超阈值即告警,不需要"持续"
        # 多轮确认——只有负载检查按 issue 原文要求"持续"用更大的 required。
        action, _ = host_health_alert.classify_threshold(
            True, host_health_alert.ThresholdState(), consecutive_required=1
        )
        self.assertEqual(action, host_health_alert.ACTION_ALERT)


class ThresholdIOTests(unittest.TestCase):
    """磁盘/负载/采样文件停更三个 I/O 读取函数，与阈值状态文件读写、消息渲染。"""

    def test_read_disk_usage_percent_returns_reasonable_value(self) -> None:
        percent = host_health_alert.read_disk_usage_percent("/")
        self.assertGreaterEqual(percent, 0.0)
        self.assertLessEqual(percent, 100.0)

    def test_read_disk_usage_percent_raises_for_missing_mount(self) -> None:
        with self.assertRaises(OSError):
            host_health_alert.read_disk_usage_percent("/this/path/does/not/exist/at/all")

    def test_read_load_per_cpu_returns_positive_cpu_count(self) -> None:
        load1, cpu_count = host_health_alert.read_load_per_cpu()
        self.assertGreaterEqual(load1, 0.0)
        self.assertGreaterEqual(cpu_count, 1)

    def test_read_sample_age_seconds_missing_both_candidates_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            age = host_health_alert.read_sample_age_seconds(
                Path(tmp), "resource", now=datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)
            )
        self.assertIsNone(age)

    def test_read_sample_age_seconds_uses_todays_file_when_present(self) -> None:
        now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            today_file = tmp_path / "resource-20260829.log"
            today_file.write_text("{}\n", encoding="utf-8")
            written_at = (now - timedelta(minutes=3)).timestamp()
            os.utime(today_file, (written_at, written_at))
            age = host_health_alert.read_sample_age_seconds(tmp_path, "resource", now=now)
        self.assertIsNotNone(age)
        self.assertAlmostEqual(age, 180, delta=1)

    def test_read_sample_age_seconds_falls_back_to_yesterdays_file_near_midnight(self) -> None:
        # 刚过 UTC 零点，今天的文件还不存在——不应该被误判成"停更"，应该拿昨天
        # 文件的 mtime 作为参照（见函数文档「同时看两个候选」）。
        now = datetime(2026, 8, 29, 0, 2, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            yesterday_file = tmp_path / "resource-20260828.log"
            yesterday_file.write_text("{}\n", encoding="utf-8")
            written_at = (now - timedelta(minutes=1)).timestamp()
            os.utime(yesterday_file, (written_at, written_at))
            age = host_health_alert.read_sample_age_seconds(tmp_path, "resource", now=now)
        self.assertIsNotNone(age)
        self.assertAlmostEqual(age, 60, delta=1)

    def test_render_threshold_alert_and_recovery(self) -> None:
        alert_text = host_health_alert.render_threshold_message(
            host_health_alert.ACTION_ALERT, label="磁盘用量", detail="已用 90%", host="h", now="t"
        )
        self.assertIn("告警", alert_text)
        self.assertIn("磁盘用量", alert_text)
        self.assertIn("已用 90%", alert_text)
        # #443 对外名称规范：监控告警群消息不得带内部代号「Lingxi」，对外统一「BI Plus」。
        self.assertIn("BI Plus", alert_text)
        self.assertNotIn("[Lingxi", alert_text)

        recovery_text = host_health_alert.render_threshold_message(
            host_health_alert.ACTION_RECOVERY,
            label="磁盘用量",
            detail="已用 90%",
            host="h",
            now="t",
        )
        self.assertIn("恢复", recovery_text)
        self.assertIn("BI Plus", recovery_text)
        self.assertNotIn("[Lingxi", recovery_text)

        with self.assertRaises(ValueError):
            host_health_alert.render_threshold_message(
                host_health_alert.ACTION_NONE, label="x", detail="y", host="h", now="t"
            )

    def test_threshold_state_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "threshold-state.json"
            states = {
                host_health_alert.THRESHOLD_DISK: host_health_alert.ThresholdState(
                    alerting=True, consecutive=2
                ),
                host_health_alert.THRESHOLD_LOAD: host_health_alert.ThresholdState(),
            }
            host_health_alert.save_threshold_state(path, states)
            loaded = host_health_alert.load_threshold_state(path)
        self.assertEqual(loaded, states)

    def test_threshold_state_missing_file_returns_empty(self) -> None:
        self.assertEqual(host_health_alert.load_threshold_state(Path("/no/such/file.json")), {})


class RunThresholdIntegrationTests(unittest.TestCase):
    """`run(--enable-resource-thresholds)` 端到端：monkeypatch 三个采集函数，验证
    去重/持续确认/恢复/发送失败重试在真正的 `run()` 调用路径上成立，且默认关闭
    时完全不影响既有容器检查行为（S-RC20-410）。
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

        # 容器检查部分用一个"运行中、无 healthcheck 配置"的伪造 docker——这个
        # 状态不满足任何触发条件（见 `classify`），保持容器层安静、不干扰阈值
        # 检查的断言（本测试类只关心 `_run_threshold_checks` 这条独立路径）。
        self.docker_bin = tmp_path / "fake-docker"
        self.docker_bin.write_text(
            "#!/bin/sh\necho '{\"Running\": true}'\nexit 0\n", encoding="utf-8"
        )
        os.chmod(self.docker_bin, 0o755)

        self.state_path = tmp_path / "state.json"
        self.threshold_state_path = tmp_path / "threshold-state.json"
        self.log_path = tmp_path / "log.txt"
        self.lock_path = tmp_path / "lock"
        self.monitoring_dir = tmp_path / "monitoring"
        self.monitoring_dir.mkdir()
        # 默认放两份"新鲜"的采样文件，让不针对停更判据的用例不会被停更检查的
        # 告警噪声污染 `sender.call_count`；专门测停更的用例自己删掉这两个文件。
        today = datetime.now(UTC).strftime("%Y%m%d")
        (self.monitoring_dir / f"resource-{today}.log").write_text("{}\n", encoding="utf-8")
        (self.monitoring_dir / f"db_business-{today}.log").write_text("{}\n", encoding="utf-8")

    def _run(self, extra_argv: list[str] | None = None) -> int:
        argv = [
            "--env-file",
            str(self.env_path),
            "--containers",
            "unused-container",
            "--state-file",
            str(self.state_path),
            "--log-file",
            str(self.log_path),
            "--lock-file",
            str(self.lock_path),
            "--docker-bin",
            str(self.docker_bin),
            "--enable-resource-thresholds",
            "--threshold-state-file",
            str(self.threshold_state_path),
            "--monitoring-dir",
            str(self.monitoring_dir),
            "--load-consecutive",
            "2",
            # 同上：本类只关心资源三项，拉取代理单元检查另有专门的测试类。
            "--disable-release-pull-check",
        ]
        if extra_argv:
            argv.extend(extra_argv)
        return host_health_alert.run(argv)

    def test_disabled_by_default_no_threshold_state_file_written(self) -> None:
        argv = [
            "--env-file",
            str(self.env_path),
            "--containers",
            "unused-container",
            "--state-file",
            str(self.state_path),
            "--log-file",
            str(self.log_path),
            "--lock-file",
            str(self.lock_path),
            "--docker-bin",
            str(self.docker_bin),
            "--disable-release-pull-check",
        ]
        with mock.patch.object(host_health_alert, "feishu_send_text") as sender:
            exit_code = host_health_alert.run(argv)
        self.assertEqual(exit_code, 0)
        sender.assert_not_called()
        self.assertFalse(self.threshold_state_path.exists())

    def test_disk_breach_alerts_once_then_dedupes_then_recovers(self) -> None:
        with (
            mock.patch.object(host_health_alert, "read_disk_usage_percent", return_value=90.0),
            mock.patch.object(host_health_alert, "read_load_per_cpu", return_value=(0.1, 4)),
            mock.patch.object(host_health_alert, "feishu_send_text") as sender,
        ):
            self._run()
        self.assertEqual(sender.call_count, 1)
        sent_text = sender.call_args.kwargs["text"]
        self.assertIn("磁盘用量", sent_text)
        self.assertIn("告警", sent_text)

        # 同样超阈值：不应该重复告警。
        with (
            mock.patch.object(host_health_alert, "read_disk_usage_percent", return_value=91.0),
            mock.patch.object(host_health_alert, "read_load_per_cpu", return_value=(0.1, 4)),
            mock.patch.object(host_health_alert, "feishu_send_text") as sender,
        ):
            self._run()
        self.assertEqual(sender.call_count, 0)

        # 恢复到阈值以下：应该收到一条恢复通知。
        with (
            mock.patch.object(host_health_alert, "read_disk_usage_percent", return_value=10.0),
            mock.patch.object(host_health_alert, "read_load_per_cpu", return_value=(0.1, 4)),
            mock.patch.object(host_health_alert, "feishu_send_text") as sender,
        ):
            self._run()
        self.assertEqual(sender.call_count, 1)
        self.assertIn("恢复", sender.call_args.kwargs["text"])

    def test_load_requires_consecutive_breaches_before_alerting(self) -> None:
        # --load-consecutive 2：第一轮超阈值不应该告警，第二轮才应该。
        with (
            mock.patch.object(host_health_alert, "read_disk_usage_percent", return_value=1.0),
            mock.patch.object(host_health_alert, "read_load_per_cpu", return_value=(20.0, 4)),
            mock.patch.object(host_health_alert, "feishu_send_text") as sender,
        ):
            self._run()
        self.assertEqual(sender.call_count, 0)

        with (
            mock.patch.object(host_health_alert, "read_disk_usage_percent", return_value=1.0),
            mock.patch.object(host_health_alert, "read_load_per_cpu", return_value=(20.0, 4)),
            mock.patch.object(host_health_alert, "feishu_send_text") as sender,
        ):
            self._run()
        self.assertEqual(sender.call_count, 1)
        self.assertIn("系统负载", sender.call_args.kwargs["text"])

    def test_staleness_breach_when_sample_file_missing(self) -> None:
        for sample_file in self.monitoring_dir.glob("*.log"):
            sample_file.unlink()
        with (
            mock.patch.object(host_health_alert, "read_disk_usage_percent", return_value=1.0),
            mock.patch.object(host_health_alert, "read_load_per_cpu", return_value=(0.1, 4)),
            mock.patch.object(host_health_alert, "feishu_send_text") as sender,
        ):
            self._run()
        sent_labels = {
            call.kwargs["text"].splitlines()[1].split("：")[0] for call in sender.call_args_list
        }
        self.assertIn("资源采样文件停更", sent_labels)
        self.assertIn("数据库/业务采样文件停更", sent_labels)

    def test_send_failure_does_not_advance_alerting_but_keeps_consecutive_count(self) -> None:
        with (
            mock.patch.object(host_health_alert, "read_disk_usage_percent", return_value=90.0),
            mock.patch.object(host_health_alert, "read_load_per_cpu", return_value=(0.1, 4)),
            mock.patch.object(
                host_health_alert,
                "feishu_send_text",
                side_effect=host_health_alert.HostMonitorError("simulated_send_failure"),
            ),
        ):
            self._run()
        state = host_health_alert.load_threshold_state(self.threshold_state_path)
        self.assertFalse(state[host_health_alert.THRESHOLD_DISK].alerting)

        with (
            mock.patch.object(host_health_alert, "read_disk_usage_percent", return_value=90.0),
            mock.patch.object(host_health_alert, "read_load_per_cpu", return_value=(0.1, 4)),
            mock.patch.object(host_health_alert, "feishu_send_text") as sender,
        ):
            self._run()
        # 上一轮发送失败但连续计数已经达标（consecutive_required=1），这一轮应
        # 该重新尝试发送并成功。
        disk_calls = [c for c in sender.call_args_list if "磁盘用量" in c.kwargs["text"]]
        self.assertEqual(len(disk_calls), 1)
        state = host_health_alert.load_threshold_state(self.threshold_state_path)
        self.assertTrue(state[host_health_alert.THRESHOLD_DISK].alerting)

    def test_dry_run_does_not_send_or_persist_threshold_state(self) -> None:
        with (
            mock.patch.object(host_health_alert, "read_disk_usage_percent", return_value=90.0),
            mock.patch.object(host_health_alert, "read_load_per_cpu", return_value=(0.1, 4)),
            mock.patch.object(host_health_alert, "feishu_send_text") as sender,
        ):
            self._run(["--dry-run"])
        sender.assert_not_called()
        self.assertFalse(self.threshold_state_path.exists())


def _systemd_stamp(moment: datetime) -> str:
    """`systemctl show` 在 `TZ=UTC` 下打印的时间戳文本，例如 `Sun 2026-09-20 06:40:04 UTC`。"""
    return moment.astimezone(UTC).strftime("%a %Y-%m-%d %H:%M:%S UTC")


class ParseSystemdTimestampTests(unittest.TestCase):
    def test_pretty_utc_timestamp_parses_to_aware_datetime(self) -> None:
        parsed = host_health_alert.parse_systemd_timestamp("Sun 2026-09-20 06:40:04 UTC")
        self.assertEqual(parsed, datetime(2026, 9, 20, 6, 40, 4, tzinfo=UTC))

    def test_weekday_is_optional_and_fraction_is_tolerated(self) -> None:
        parsed = host_health_alert.parse_systemd_timestamp("2026-09-20 06:40:04.123456 UTC")
        self.assertEqual(parsed, datetime(2026, 9, 20, 6, 40, 4, tzinfo=UTC))

    def test_unset_values_return_none(self) -> None:
        for value in ("", "n/a", "0", None, "  "):
            with self.subTest(value=value):
                self.assertIsNone(host_health_alert.parse_systemd_timestamp(value))

    def test_garbage_or_non_utc_zone_raises(self) -> None:
        for value in ("yesterday", "Sun 2026-09-20 14:40:04 CST", "1758350404"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    host_health_alert.parse_systemd_timestamp(value)


class JudgeReleasePullTests(unittest.TestCase):
    """`judge_release_pull` 三个判据的纯逻辑边界；本卡的变异验红目标之一。"""

    NOW = datetime(2026, 9, 20, 7, 0, 0, tzinfo=UTC)

    def _observation(self, **overrides) -> host_health_alert.ReleasePullObservation:
        fields = {
            "unit": "lingxi-release-pull",
            "timer_active_state": "active",
            "timer_load_state": "loaded",
            "last_trigger": self.NOW - timedelta(minutes=5),
            "service_active_state": "inactive",
            "service_result": "success",
            "service_exit_status": 0,
            "service_exit_at": self.NOW - timedelta(minutes=4),
        }
        fields.update(overrides)
        return host_health_alert.ReleasePullObservation(**fields)

    def _judge(self, observation, *, stale_minutes: float = 15.0) -> dict[str, bool]:
        checks = host_health_alert.judge_release_pull(
            observation, now=self.NOW, stale_minutes=stale_minutes
        )
        return {key: breached for key, _label, breached, _detail, _required in checks}

    def test_healthy_observation_breaches_nothing(self) -> None:
        breaches = self._judge(self._observation())
        self.assertEqual(
            breaches,
            {
                host_health_alert.RELEASE_PULL_TIMER_INACTIVE: False,
                host_health_alert.RELEASE_PULL_TRIGGER_STALE: False,
                host_health_alert.RELEASE_PULL_LAST_RUN_FAILED: False,
            },
        )

    def test_timer_not_active_breaches_and_staleness_is_not_judged(self) -> None:
        for state in ("inactive", "failed", "activating", "deactivating"):
            with self.subTest(state=state):
                breaches = self._judge(self._observation(timer_active_state=state))
                self.assertTrue(breaches[host_health_alert.RELEASE_PULL_TIMER_INACTIVE])
                self.assertNotIn(host_health_alert.RELEASE_PULL_TRIGGER_STALE, breaches)

    def test_staleness_boundary_is_strictly_greater_than_threshold(self) -> None:
        at_threshold = self._observation(
            last_trigger=self.NOW - timedelta(minutes=15),
            service_exit_at=self.NOW - timedelta(minutes=15),
        )
        self.assertFalse(self._judge(at_threshold)[host_health_alert.RELEASE_PULL_TRIGGER_STALE])
        past_threshold = self._observation(
            last_trigger=self.NOW - timedelta(minutes=15, seconds=1),
            service_exit_at=self.NOW - timedelta(minutes=15, seconds=1),
        )
        self.assertTrue(self._judge(past_threshold)[host_health_alert.RELEASE_PULL_TRIGGER_STALE])

    def test_recent_service_exit_rescues_an_old_trigger(self) -> None:
        # 一轮 40 分钟的长部署刚刚结束、下一个五分钟刻度还没到：触发时刻已旧，但
        # service 刚结束就是最新留痕，不算过期。
        observation = self._observation(
            last_trigger=self.NOW - timedelta(minutes=40),
            service_exit_at=self.NOW - timedelta(minutes=2),
        )
        self.assertFalse(self._judge(observation)[host_health_alert.RELEASE_PULL_TRIGGER_STALE])

    def test_running_service_is_neither_stale_nor_failed(self) -> None:
        observation = self._observation(
            last_trigger=self.NOW - timedelta(minutes=40),
            service_active_state="activating",
            service_exit_at=None,
        )
        breaches = self._judge(observation)
        self.assertFalse(breaches[host_health_alert.RELEASE_PULL_TRIGGER_STALE])
        self.assertNotIn(host_health_alert.RELEASE_PULL_LAST_RUN_FAILED, breaches)

    def test_never_triggered_and_never_finished_is_stale(self) -> None:
        observation = self._observation(last_trigger=None, service_exit_at=None)
        self.assertTrue(self._judge(observation)[host_health_alert.RELEASE_PULL_TRIGGER_STALE])

    def test_failure_requires_non_success_result_and_non_zero_exit_status(self) -> None:
        cases = {
            ("exit-code", 1): True,
            ("timeout", 15): True,
            ("exit-code", 0): False,
            ("success", 1): False,
            ("success", 0): False,
        }
        for (result, status), expected in cases.items():
            with self.subTest(result=result, status=status):
                observation = self._observation(service_result=result, service_exit_status=status)
                self.assertEqual(
                    self._judge(observation)[host_health_alert.RELEASE_PULL_LAST_RUN_FAILED],
                    expected,
                )

    def test_detail_names_unit_states_trigger_result_and_threshold(self) -> None:
        observation = self._observation(
            timer_active_state="inactive",
            timer_load_state="not-found",
            service_result="exit-code",
            service_exit_status=1,
        )
        detail = host_health_alert.describe_release_pull(
            observation, now=self.NOW, stale_minutes=15.0
        )
        self.assertIn("单元 lingxi-release-pull", detail)
        self.assertIn("timer=inactive（not-found）", detail)
        self.assertIn("service=inactive", detail)
        self.assertIn("上一次触发 2026-09-20T06:55:00Z", detail)
        self.assertIn("上一轮 exit-code/1", detail)
        self.assertIn("阈值 15 分钟", detail)

    def test_release_pull_messages_use_host_monitor_category(self) -> None:
        text = host_health_alert.render_threshold_message(
            host_health_alert.ACTION_ALERT,
            label="拉取代理定时器未激活",
            detail="x",
            host="h",
            now="t",
            category=host_health_alert._THRESHOLD_CATEGORY[
                host_health_alert.RELEASE_PULL_TIMER_INACTIVE
            ],
        )
        self.assertTrue(text.startswith("[BI Plus 宿主监控] 告警\n"))
        # 资源三项不传分类词，首行保持既有文案。
        legacy = host_health_alert.render_threshold_message(
            host_health_alert.ACTION_ALERT, label="磁盘用量", detail="x", host="h", now="t"
        )
        self.assertTrue(legacy.startswith("[BI Plus 资源监控] 告警\n"))


class RunReleasePullIntegrationTests(unittest.TestCase):
    """`run()` 端到端：`systemctl` 用可执行桩注入固定属性输出，验证拉取代理单元检查
    默认开启、三态告警 / 去重 / 恢复、未知形态、逃生口与脱敏否定在真正的调用路径
    上成立。容器主线用"运行中、无 healthcheck"的伪造 docker 保持安静。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)

        self.env_path = self.tmp_path / "env"
        self.env_path.write_text(
            "LINGXI_FEISHU_APP_ID=a\nLINGXI_FEISHU_APP_SECRET=b\nLINGXI_ADMIN_GROUP_CHAT_ID=oc_x\n",
            encoding="utf-8",
        )
        os.chmod(self.env_path, 0o600)

        self.docker_bin = self.tmp_path / "fake-docker"
        self.docker_bin.write_text(
            "#!/bin/sh\necho '{\"Running\": true}'\nexit 0\n", encoding="utf-8"
        )
        os.chmod(self.docker_bin, 0o755)

        # 伪造的 `systemctl`：按最后一个参数（单元名）的后缀吐出对应属性文件，退出码
        # 也由文件控制；每次调用追加一行到 calls.log，供「逃生口关闭时不得调用」断言。
        self.timer_props = self.tmp_path / "timer.props"
        self.timer_rc = self.tmp_path / "timer.rc"
        self.service_props = self.tmp_path / "service.props"
        self.service_rc = self.tmp_path / "service.rc"
        self.calls_log = self.tmp_path / "calls.log"
        self.systemctl_bin = self.tmp_path / "fake-systemctl"
        self.systemctl_bin.write_text(
            "#!/bin/sh\n"
            f'echo "$*" >> "{self.calls_log}"\n'
            'for last in "$@"; do :; done\n'
            'case "$last" in\n'
            f'  *.timer) cat "{self.timer_props}"; echo "stderr /opt/SENTINEL_STDERR" >&2; '
            f'exit "$(cat "{self.timer_rc}")" ;;\n'
            f'  *.service) cat "{self.service_props}"; exit "$(cat "{self.service_rc}")" ;;\n'
            "esac\n"
            "exit 2\n",
            encoding="utf-8",
        )
        os.chmod(self.systemctl_bin, 0o755)
        self.now = datetime.now(UTC)
        self._set_timer()
        self._set_service()

        self.state_path = self.tmp_path / "state.json"
        self.threshold_state_path = self.tmp_path / "threshold-state.json"
        self.log_path = self.tmp_path / "log.txt"
        self.lock_path = self.tmp_path / "lock"

    def _set_timer(
        self,
        *,
        active_state: str = "active",
        load_state: str = "loaded",
        last_trigger: datetime | None | str = "default",
        rc: int = 0,
        extra_lines: tuple[str, ...] = (),
        omit: tuple[str, ...] = (),
    ) -> None:
        if last_trigger == "default":
            last_trigger = self.now - timedelta(minutes=5)
        lines = {
            "ActiveState": active_state,
            "LoadState": load_state,
            "LastTriggerUSec": _systemd_stamp(last_trigger) if last_trigger else "",
        }
        body = [f"{k}={v}" for k, v in lines.items() if k not in omit] + list(extra_lines)
        self.timer_props.write_text("\n".join(body) + "\n", encoding="utf-8")
        self.timer_rc.write_text(f"{rc}\n", encoding="utf-8")

    def _set_service(
        self,
        *,
        active_state: str = "inactive",
        result: str = "success",
        exit_status: int = 0,
        exit_at: datetime | None | str = "default",
        rc: int = 0,
        extra_lines: tuple[str, ...] = (),
    ) -> None:
        if exit_at == "default":
            exit_at = self.now - timedelta(minutes=4)
        lines = {
            "ActiveState": active_state,
            "Result": result,
            "ExecMainStatus": str(exit_status),
            "ExecMainExitTimestamp": _systemd_stamp(exit_at) if exit_at else "",
        }
        body = [f"{k}={v}" for k, v in lines.items()] + list(extra_lines)
        self.service_props.write_text("\n".join(body) + "\n", encoding="utf-8")
        self.service_rc.write_text(f"{rc}\n", encoding="utf-8")

    def _run(self, extra_argv: list[str] | None = None) -> int:
        argv = [
            "--env-file",
            str(self.env_path),
            "--containers",
            "unused-container",
            "--state-file",
            str(self.state_path),
            "--log-file",
            str(self.log_path),
            "--lock-file",
            str(self.lock_path),
            "--docker-bin",
            str(self.docker_bin),
            "--threshold-state-file",
            str(self.threshold_state_path),
            "--systemctl-bin",
            str(self.systemctl_bin),
        ]
        if extra_argv:
            argv.extend(extra_argv)
        return host_health_alert.run(argv)

    def _round(self, extra_argv: list[str] | None = None) -> list[str]:
        with mock.patch.object(host_health_alert, "feishu_send_text") as sender:
            exit_code = self._run(extra_argv)
        self.assertEqual(exit_code, 0)
        return [call.kwargs["text"] for call in sender.call_args_list]

    def _alerting_keys(self) -> set[str]:
        state = host_health_alert.load_threshold_state(self.threshold_state_path)
        return {key for key, value in state.items() if value.alerting}

    def test_healthy_unit_yields_zero_alerts_and_no_state_file(self) -> None:
        self.assertEqual(self._round(), [])
        self.assertFalse(self.threshold_state_path.exists())
        # 默认开启：桩确实被问过 timer 与 service 各一次。
        calls = self.calls_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(calls), 2)
        self.assertTrue(any("lingxi-release-pull.timer" in c for c in calls))
        self.assertTrue(any("lingxi-release-pull.service" in c for c in calls))
        self.assertTrue(all("--all" in c and "show" in c for c in calls))

    def test_timer_inactive_alerts_once_dedupes_then_recovers_once(self) -> None:
        self._set_timer(active_state="inactive")
        texts = self._round()
        self.assertEqual(len(texts), 1)
        self.assertTrue(texts[0].startswith("[BI Plus 宿主监控] 告警\n"))
        self.assertIn("拉取代理定时器未激活", texts[0])
        self.assertIn("单元 lingxi-release-pull", texts[0])
        self.assertIn("timer=inactive", texts[0])
        self.assertIn("上一次触发 ", texts[0])
        self.assertIn("上一轮 success/0", texts[0])
        self.assertEqual(self._alerting_keys(), {host_health_alert.RELEASE_PULL_TIMER_INACTIVE})

        # 同一状态第二轮：去重，不再发。
        self.assertEqual(self._round(), [])

        # 恢复：一条恢复通知，随后静默。
        self._set_timer(active_state="active")
        texts = self._round()
        self.assertEqual(len(texts), 1)
        self.assertTrue(texts[0].startswith("[BI Plus 宿主监控] 恢复\n"))
        self.assertIn("拉取代理定时器未激活：已恢复正常", texts[0])
        self.assertEqual(self._round(), [])
        self.assertEqual(self._alerting_keys(), set())

    def test_stale_trigger_alerts_once(self) -> None:
        self._set_timer(last_trigger=self.now - timedelta(minutes=20))
        self._set_service(exit_at=self.now - timedelta(minutes=20))
        texts = self._round()
        self.assertEqual(len(texts), 1)
        self.assertIn("拉取代理留痕过期", texts[0])
        self.assertIn("阈值 15 分钟", texts[0])
        self.assertEqual(self._round(), [])
        self.assertEqual(self._alerting_keys(), {host_health_alert.RELEASE_PULL_TRIGGER_STALE})

    def test_stale_threshold_is_configurable(self) -> None:
        self._set_timer(last_trigger=self.now - timedelta(minutes=20))
        self._set_service(exit_at=self.now - timedelta(minutes=20))
        self.assertEqual(self._round(["--release-pull-stale-minutes", "30"]), [])

    def test_running_deployment_is_not_stale(self) -> None:
        # 触发 4 分钟前、service 正在运行：零告警。
        self._set_timer(last_trigger=self.now - timedelta(minutes=4))
        self._set_service(active_state="activating", exit_at=None)
        self.assertEqual(self._round(), [])
        # 一轮长部署跑了 40 分钟仍在运行：触发时刻早已超过阈值，但不算过期。
        self._set_timer(last_trigger=self.now - timedelta(minutes=40))
        self.assertEqual(self._round(), [])
        self.assertFalse(self.threshold_state_path.exists())

    def test_last_run_failure_alerts_once_then_recovers_after_a_good_run(self) -> None:
        self._set_service(result="exit-code", exit_status=1)
        texts = self._round()
        self.assertEqual(len(texts), 1)
        self.assertIn("拉取代理上一轮失败", texts[0])
        self.assertIn("上一轮 exit-code/1", texts[0])
        self.assertEqual(self._round(), [])
        self._set_service(result="success", exit_status=0)
        texts = self._round()
        self.assertEqual(len(texts), 1)
        self.assertIn("拉取代理上一轮失败：已恢复正常", texts[0])

    def test_systemctl_failure_alerts_unknown_once_dedupes_then_recovers(self) -> None:
        self._set_timer(rc=1)
        texts = self._round()
        self.assertEqual(len(texts), 1)
        self.assertIn("拉取代理状态未知", texts[0])
        self.assertIn("systemctl_exit_1", texts[0])
        self.assertEqual(self._alerting_keys(), {host_health_alert.RELEASE_PULL_UNKNOWN})
        self.assertEqual(self._round(), [])
        self._set_timer(rc=0)
        texts = self._round()
        self.assertEqual(len(texts), 1)
        self.assertIn("拉取代理状态未知：已恢复正常", texts[0])
        self.assertEqual(self._alerting_keys(), set())

    def test_missing_property_alerts_unknown(self) -> None:
        self._set_timer(omit=("ActiveState",))
        texts = self._round()
        self.assertEqual(len(texts), 1)
        self.assertIn("拉取代理状态未知", texts[0])
        self.assertIn("property_missing:ActiveState", texts[0])

    def test_unparseable_timestamp_alerts_unknown(self) -> None:
        self._set_timer(extra_lines=("LastTriggerUSec=yesterday",), omit=("LastTriggerUSec",))
        texts = self._round()
        self.assertEqual(len(texts), 1)
        self.assertIn("property_unparseable:LastTriggerUSec", texts[0])

    def test_missing_systemctl_binary_alerts_unknown_without_exit_code_two(self) -> None:
        self._set_timer(active_state="inactive")
        with mock.patch.object(host_health_alert, "feishu_send_text") as sender:
            exit_code = self._run(["--systemctl-bin", str(self.tmp_path / "no-such-systemctl")])
        self.assertEqual(exit_code, 0)
        self.assertEqual(sender.call_count, 1)
        text = sender.call_args.kwargs["text"]
        self.assertIn("拉取代理状态未知", text)
        self.assertIn("systemctl_not_found", text)
        self.assertNotIn("no-such-systemctl", text)

    def test_disable_flag_skips_check_and_never_invokes_systemctl(self) -> None:
        self._set_timer(active_state="inactive")
        self._set_service(result="exit-code", exit_status=1)
        self.assertEqual(self._round(["--disable-release-pull-check"]), [])
        self.assertFalse(self.calls_log.exists())
        self.assertFalse(self.threshold_state_path.exists())

    def test_dry_run_does_not_send_or_persist(self) -> None:
        self._set_timer(active_state="inactive")
        self.assertEqual(self._round(["--dry-run"]), [])
        self.assertFalse(self.threshold_state_path.exists())
        self.assertIn("release_pull_timer_inactive", self.log_path.read_text(encoding="utf-8"))

    def test_alert_text_never_leaks_paths_tokens_or_command_lines(self) -> None:
        # 桩输出里塞满哨兵：ExecStart 命令原文、环境里的令牌、路径；进程环境也塞一个。
        leaky_lines = (
            "ExecStart={ path=/opt/SENTINEL_PATH/release_pull_agent.py ; argv[]=/opt/lingxi/bin/"
            "python3 -B /opt/SENTINEL_PATH/release_pull_agent.py --host-contract /opt/SENTINEL_PATH/"
            "host-contract.json --env-file /opt/SENTINEL_PATH/gh.env run }",
            "Environment=GH_TOKEN=SENTINEL_TOKEN_9f3a LINGXI_X=SENTINEL_ENV_VALUE",
        )
        self._set_timer(active_state="inactive", extra_lines=leaky_lines)
        self._set_service(result="exit-code", exit_status=1, extra_lines=leaky_lines)
        with mock.patch.dict(os.environ, {"LINGXI_LEAKY": "SENTINEL_PROCESS_ENV"}):
            texts = self._round()
            self._set_timer(rc=1, extra_lines=leaky_lines)
            texts.extend(self._round())
        # 三条告警都发了：定时器未激活、上一轮失败、然后读取失败转未知。
        self.assertEqual(len(texts), 3)
        forbidden = (
            "SENTINEL",
            "/opt/",
            "--env-file",
            "--host-contract",
            "argv[]",
            "GH_TOKEN",
            str(self.tmp_path),
            str(self.systemctl_bin),
        )
        for text in texts:
            for needle in forbidden:
                with self.subTest(needle=needle, text=text):
                    self.assertNotIn(needle, text)


if __name__ == "__main__":
    unittest.main()
