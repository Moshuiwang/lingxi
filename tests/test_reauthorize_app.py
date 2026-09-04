"""正式重授权 apps 入口的部署可达与启动安全断言。

另认领 Issue #137 首次建立模式在入口层的 `V-身份-11`：显式确认参数、拒绝语义与
逐次审计留痕。真实授权闭环属于受控 Ops 窗口，不在本地单测里冒充通过。
"""

from __future__ import annotations

import ast
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from lingxi.adapters.feishu_reauthorization import (
    ReauthorizationResult,
    ReauthorizationStart,
    SubjectAlreadyRegisteredError,
)
from lingxi.adapters.oauth_bridge_client import OAuthBridgeMessage
from lingxi.apps import reauthorize as reauthorize_module
from lingxi.apps.reauthorize import (
    bridge_wait_seconds,
    main,
    validate_reauthorization_paths,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ONBOARDING_MODULE = "lingxi.core.identity.onboarding"
BOOTSTRAP_SUBJECT = "ou_bootstrap_subject_for_test"
OTHER_SUBJECT = "ou_other_subject_for_test"


class FakeReauthorizationEntry:
    state = "r" * 43

    def __init__(
        self,
        *,
        begin_error: Exception | None = None,
        result: ReauthorizationResult | None = None,
    ) -> None:
        self.callbacks: list[tuple[str | None, str | None, str | None]] = []
        self.begin_subjects: list[str | None] = []
        self.begin_error = begin_error
        self.result = result or ReauthorizationResult(True, "completed", "专用授权已更新。", False)

    def begin(self, *, expected_subject_open_id: str | None = None) -> ReauthorizationStart:
        self.begin_subjects.append(expected_subject_open_id)
        if self.begin_error is not None:
            raise self.begin_error
        return ReauthorizationStart(
            "https://accounts.example.test/authorize?state=" + self.state,
            self.state,
            datetime.now(UTC),
        )

    def handle_callback(
        self,
        state: str | None,
        *,
        code: str | None = None,
        error: str | None = None,
    ) -> ReauthorizationResult:
        self.callbacks.append((state, code, error))
        return self.result


class RecordingEntryBuilder:
    """替代 ``_build_entry``：记录入口是以哪种模式装配的。"""

    def __init__(self, entry: FakeReauthorizationEntry | None = None) -> None:
        self.entry = entry or FakeReauthorizationEntry()
        self.modes: list[str] = []

    def __call__(
        self,
        env: object,
        *,
        state_path: str,
        credential_path: str,
        mode: str = "renewal",
    ) -> FakeReauthorizationEntry:
        self.modes.append(mode)
        return self.entry


class RefusingEntryBuilder:
    """任何装配都视为越界：用于证明拒绝发生在建立入口之前。"""

    def __call__(self, *args: object, **kwargs: object) -> FakeReauthorizationEntry:
        raise AssertionError("参数不完整时不得装配重授权入口")


class AuditStream(io.StringIO):
    """审计出口替身：``failing_actions`` 里的动作模拟"这条审计写不下去"。"""

    def __init__(self, failing_actions: frozenset[str] = frozenset()) -> None:
        super().__init__()
        self.failing_actions = failing_actions

    def write(self, text: str) -> int:
        if any(f"action={action}" in text for action in self.failing_actions):
            raise OSError("审计出口不可写")
        return super().write(text)

    @property
    def records(self) -> list[dict[str, str]]:
        return [
            dict(field.split("=", 1) for field in line.split(" ") if "=" in field)
            for line in self.getvalue().splitlines()
            if line.startswith("event=")
        ]

    @property
    def actions(self) -> list[str]:
        return [record["action"] for record in self.records]


class FakeBridgeThread:
    def join(self, timeout: float | None = None) -> None:
        return None


class FakeBridge:
    instances: list[FakeBridge] = []

    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.token = token
        self.handlers: dict[str, object] = {}
        self.results: list[tuple[str, str]] = []
        self.stopped = False
        self.instances.append(self)

    def register_state_handler(self, state: str, handler: object) -> None:
        self.handlers[state] = handler

    def start(self) -> FakeBridgeThread:
        state, handler = next(iter(self.handlers.items()))
        assert callable(handler)
        handler(OAuthBridgeMessage("oauth_code", state, "bridge-code-for-test"))
        return FakeBridgeThread()

    def send_result(self, state: str, status: str, **_kwargs: object) -> None:
        self.results.append((state, status))

    def stop(self) -> None:
        self.stopped = True


def _source_for(module_name: str) -> Path | None:
    spec = importlib.util.find_spec(module_name)
    if spec is None or not spec.origin or spec.origin in {"built-in", "frozen"}:
        return None
    path = Path(spec.origin)
    return path if path.suffix == ".py" else None


def _lingxi_imports(module_name: str) -> set[str]:
    source = _source_for(module_name)
    if source is None:
        return set()
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    package = module_name.rpartition(".")[0]
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(
                alias.name
                for alias in node.names
                if alias.name == "lingxi" or alias.name.startswith("lingxi.")
            )
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative = "." * node.level + (node.module or "")
                resolved = importlib.util.resolve_name(relative, package)
            else:
                resolved = node.module or ""
            if resolved == "lingxi" or resolved.startswith("lingxi."):
                imported.add(resolved)
    return imported


def _formal_import_closure(roots: tuple[str, ...]) -> set[str]:
    seen: set[str] = set()
    pending = list(roots)
    while pending:
        module_name = pending.pop()
        if module_name in seen:
            continue
        seen.add(module_name)
        pending.extend(sorted(_lingxi_imports(module_name) - seen))
    return seen


def _bootstrap_env(directory: str, **extra: str) -> dict[str, str]:
    """受控入口的最小环境；真实凭据与真实 Bridge 都不参与本地单测。"""

    base = {
        "LINGXI_DELEGATED_CREDENTIAL_PATH": str(Path(directory) / "delegated.enc"),
        "LINGXI_DELEGATED_REAUTH_STATE_PATH": str(Path(directory) / "reauth-state.json"),
        "LINGXI_OAUTH_BRIDGE_URL": "wss://bridge.example.test/oauth/bridge",
        "LINGXI_OAUTH_BRIDGE_TOKEN": "bridge-token-for-test",
        "LINGXI_OAUTH_BRIDGE_WAIT_SECONDS": "1",
    }
    base.update(extra)
    return base


class ReauthorizeAppTest(unittest.TestCase):
    def test_python_m_entrypoint_starts_and_refuses_missing_configuration(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "lingxi.apps.reauthorize"],
            cwd=REPOSITORY_ROOT,
            env={
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": str(REPOSITORY_ROOT / "src"),
                "PYTHONUNBUFFERED": "1",
            },
            capture_output=True,
            text=True,
            timeout=60,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("重授权入口失败：RuntimeError", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_startup_rejects_state_path_equal_to_credential_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            credential_path = str(Path(directory) / "delegated.enc")
            output, errors = io.StringIO(), io.StringIO()
            result = main(
                env={
                    "LINGXI_DELEGATED_CREDENTIAL_PATH": credential_path,
                    "LINGXI_DELEGATED_REAUTH_STATE_PATH": credential_path,
                },
                stdout=output,
                stderr=errors,
            )

        self.assertEqual(result, 1)
        self.assertIn("ValueError", errors.getvalue())
        self.assertEqual(output.getvalue(), "")

    def test_state_and_credential_lock_paths_cannot_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            credential_path = Path(directory) / "delegated.enc"
            for state_path in (
                credential_path,
                credential_path.with_name(credential_path.name + ".lock"),
            ):
                with self.subTest(state_path=state_path):
                    with self.assertRaises(ValueError):
                        validate_reauthorization_paths(str(state_path), str(credential_path))

    def test_bridge_entrypoint_registers_state_and_routes_code_to_formal_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            credential_path = str(Path(directory) / "delegated.enc")
            state_path = str(Path(directory) / "reauth-state.json")
            entry = FakeReauthorizationEntry()
            FakeBridge.instances.clear()
            with (
                patch("lingxi.apps.reauthorize._build_entry", return_value=entry),
                patch("lingxi.apps.reauthorize.OAuthBridgeClient", FakeBridge),
            ):
                output, errors = io.StringIO(), io.StringIO()
                result = main(
                    env={
                        "LINGXI_DELEGATED_CREDENTIAL_PATH": credential_path,
                        "LINGXI_DELEGATED_REAUTH_STATE_PATH": state_path,
                        "LINGXI_DELEGATED_SUBJECT_OPEN_ID": "ou_subject",
                        "LINGXI_OAUTH_BRIDGE_URL": "wss://bridge.example.test/oauth/bridge",
                        "LINGXI_OAUTH_BRIDGE_TOKEN": "bridge-token-for-test",
                        "LINGXI_OAUTH_BRIDGE_WAIT_SECONDS": "1",
                    },
                    stdout=output,
                    stderr=errors,
                )

        self.assertEqual(result, 0)
        self.assertEqual(entry.callbacks, [(entry.state, "bridge-code-for-test", None)])
        self.assertEqual(FakeBridge.instances[0].results, [(entry.state, "identity_confirmed")])
        self.assertTrue(FakeBridge.instances[0].stopped)
        self.assertNotIn("bridge-code-for-test", output.getvalue())
        self.assertNotIn("bridge-token-for-test", output.getvalue() + errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())

    def test_bridge_wait_seconds_rejects_non_positive_values(self) -> None:
        self.assertEqual(bridge_wait_seconds({}), 600)
        for value in ("0", "-1", "not-an-integer"):
            with self.subTest(value=value):
                with self.assertRaises(RuntimeError):
                    bridge_wait_seconds({"LINGXI_OAUTH_BRIDGE_WAIT_SECONDS": value})

    def test_renewal_run_writes_no_bootstrap_audit_record(self) -> None:
        """无参数即续期：既有语义不变，也不产生首次建立的审计噪音。"""

        with tempfile.TemporaryDirectory() as directory:
            builder = RecordingEntryBuilder()
            audit = AuditStream()
            FakeBridge.instances.clear()
            with (
                patch("lingxi.apps.reauthorize._build_entry", builder),
                patch("lingxi.apps.reauthorize.OAuthBridgeClient", FakeBridge),
            ):
                result = main(
                    [],
                    env=_bootstrap_env(directory),
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                    audit_stream=audit,
                )

        self.assertEqual(result, 0)
        self.assertEqual(builder.modes, ["renewal"])
        self.assertEqual(audit.records, [])

    def test_formal_reauthorization_import_closure_excludes_bot_test_onboarding(self) -> None:
        closure = _formal_import_closure(
            (
                "lingxi.apps.reauthorize",
                "lingxi.apps.reauthorize.__main__",
            )
        )

        self.assertNotIn(ONBOARDING_MODULE, closure)


class BootstrapEntryTest(unittest.TestCase):
    """Issue #137：入口的「首次建立」模式——显式确认、拒绝语义与逐次审计（V-身份-11）。

    Bridge、入口装配和审计出口都是替身；真实授权属于受控 Ops 窗口，不在本地
    单测里冒充通过。
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = io.StringIO()
        self.errors = io.StringIO()
        FakeBridge.instances.clear()

    def _run(
        self,
        argv: list[str],
        *,
        builder: object,
        audit: AuditStream,
        env: dict[str, str] | None = None,
    ) -> int:
        with (
            patch("lingxi.apps.reauthorize._build_entry", builder),
            patch("lingxi.apps.reauthorize.OAuthBridgeClient", FakeBridge),
        ):
            return main(
                argv,
                env=env if env is not None else _bootstrap_env(self.directory.name),
                stdout=self.output,
                stderr=self.errors,
                audit_stream=audit,
            )

    def test_subject_without_confirmation_is_refused_before_any_assembly(self) -> None:
        audit = AuditStream()

        result = self._run(
            ["--bootstrap-subject", BOOTSTRAP_SUBJECT],
            builder=RefusingEntryBuilder(),
            audit=audit,
        )

        self.assertEqual(result, 1)
        self.assertIn("--confirm-bootstrap", self.errors.getvalue())
        self.assertIn("未做任何修改", self.errors.getvalue())
        self.assertEqual(audit.actions, ["rejected"])
        self.assertEqual(FakeBridge.instances, [])

    def test_confirmation_without_subject_is_refused(self) -> None:
        audit = AuditStream()

        result = self._run(["--confirm-bootstrap"], builder=RefusingEntryBuilder(), audit=audit)

        self.assertEqual(result, 1)
        self.assertIn("--bootstrap-subject", self.errors.getvalue())
        self.assertEqual(audit.actions, ["rejected"])
        self.assertEqual(FakeBridge.instances, [])

    def test_subject_conflicting_with_configured_subject_is_refused(self) -> None:
        audit = AuditStream()

        result = self._run(
            ["--bootstrap-subject", BOOTSTRAP_SUBJECT, "--confirm-bootstrap"],
            builder=RefusingEntryBuilder(),
            audit=audit,
            env=_bootstrap_env(self.directory.name, LINGXI_DELEGATED_SUBJECT_OPEN_ID=OTHER_SUBJECT),
        )

        self.assertEqual(result, 1)
        self.assertIn("不一致", self.errors.getvalue())
        self.assertEqual(FakeBridge.instances, [])

    def test_confirmed_bootstrap_assembles_the_bootstrap_mode_and_audits_the_outcome(self) -> None:
        builder = RecordingEntryBuilder()
        audit = AuditStream()

        result = self._run(
            ["--bootstrap-subject", BOOTSTRAP_SUBJECT, "--confirm-bootstrap"],
            builder=builder,
            audit=audit,
        )

        self.assertEqual(result, 0)
        self.assertEqual(builder.modes, ["bootstrap"])
        self.assertEqual(builder.entry.begin_subjects, [BOOTSTRAP_SUBJECT])
        self.assertEqual(audit.actions, ["requested", "completed"])
        self.assertEqual({record["mode"] for record in audit.records}, {"bootstrap"})

    def test_existing_registration_is_reported_and_audited_without_touching_the_bridge(
        self,
    ) -> None:
        builder = RecordingEntryBuilder(
            FakeReauthorizationEntry(begin_error=SubjectAlreadyRegisteredError("已有主体"))
        )
        audit = AuditStream()

        result = self._run(
            ["--bootstrap-subject", BOOTSTRAP_SUBJECT, "--confirm-bootstrap"],
            builder=builder,
            audit=audit,
        )

        self.assertEqual(result, 1)
        self.assertIn("已存在专用授权主体登记", self.errors.getvalue())
        self.assertIn("删除动作", self.errors.getvalue())
        self.assertEqual(audit.actions, ["requested", "rejected"])
        self.assertEqual(audit.records[-1]["outcome"], "subject_exists")
        self.assertEqual(FakeBridge.instances, [], "拒绝时不得占用 OAuth Bridge 通道")

    def test_audit_failure_before_authorization_aborts_without_starting_anything(self) -> None:
        builder = RecordingEntryBuilder()
        audit = AuditStream(failing_actions=frozenset({"requested"}))

        result = self._run(
            ["--bootstrap-subject", BOOTSTRAP_SUBJECT, "--confirm-bootstrap"],
            builder=builder,
            audit=audit,
        )

        self.assertEqual(result, 1)
        self.assertIn("审计留痕失败", self.errors.getvalue())
        self.assertEqual(builder.entry.begin_subjects, [], "审计写不下去就不得发起授权")
        self.assertEqual(FakeBridge.instances, [])
        self.assertEqual(audit.records, [])

    def test_audit_failure_after_creation_is_reported_as_unrecoverable(self) -> None:
        builder = RecordingEntryBuilder()
        audit = AuditStream(failing_actions=frozenset({"completed"}))

        result = self._run(
            ["--bootstrap-subject", BOOTSTRAP_SUBJECT, "--confirm-bootstrap"],
            builder=builder,
            audit=audit,
        )

        self.assertEqual(result, 1, "建立成功但审计失败不得报告为成功")
        self.assertIn("不可恢复", self.errors.getvalue())
        self.assertIn("不要重复运行", self.errors.getvalue())
        self.assertEqual(audit.actions, ["requested"])

    def test_failed_bootstrap_outcome_is_audited_with_its_failure_code(self) -> None:
        builder = RecordingEntryBuilder(
            FakeReauthorizationEntry(
                result=ReauthorizationResult(
                    False, "subject_exists", "已存在专用授权主体登记。", True
                )
            )
        )
        audit = AuditStream()

        result = self._run(
            ["--bootstrap-subject", BOOTSTRAP_SUBJECT, "--confirm-bootstrap"],
            builder=builder,
            audit=audit,
        )

        self.assertEqual(result, 1)
        self.assertEqual(audit.actions, ["requested", "failed"])
        self.assertEqual(audit.records[-1]["outcome"], "subject_exists")

    def test_audit_records_answer_who_when_which_subject_and_stay_redacted(self) -> None:
        builder = RecordingEntryBuilder()
        audit = AuditStream()

        self._run(
            ["--bootstrap-subject", BOOTSTRAP_SUBJECT, "--confirm-bootstrap"],
            builder=builder,
            audit=audit,
        )

        rendered = repr(audit.records) + self.output.getvalue() + self.errors.getvalue()
        for record in audit.records:
            self.assertEqual(record["event"], "reauthorize.bootstrap")
            self.assertEqual(record["mode"], "bootstrap")
            for field in ("actor", "host", "pid", "at"):
                self.assertTrue(record[field], f"审计缺少 {field}")
            datetime.fromisoformat(record["at"])  # 可解析的时间，不是自由文本
            self.assertNotEqual(record["subject"], BOOTSTRAP_SUBJECT)
            self.assertIn("…", record["subject"], "主体必须按既有脱敏出口缩短")
        self.assertNotIn(BOOTSTRAP_SUBJECT, rendered, "完整 open_id 不得进入审计或终端输出")
        self.assertNotIn("bridge-code-for-test", rendered)
        self.assertNotIn("bridge-token-for-test", rendered)

    def test_the_default_audit_outlet_is_the_controlled_terminal(self) -> None:
        """不注入出口时审计仍然要落到受控终端，而不是被日志级别静默丢弃。"""

        builder = RecordingEntryBuilder()
        with (
            patch("lingxi.apps.reauthorize._build_entry", builder),
            patch("lingxi.apps.reauthorize.OAuthBridgeClient", FakeBridge),
        ):
            result = main(
                ["--bootstrap-subject", BOOTSTRAP_SUBJECT, "--confirm-bootstrap"],
                env=_bootstrap_env(self.directory.name),
                stdout=self.output,
                stderr=self.errors,
            )

        self.assertEqual(result, 0)
        written = [
            line for line in self.errors.getvalue().splitlines() if line.startswith("event=")
        ]
        self.assertEqual(len(written), 2)
        self.assertIn("mode=bootstrap", written[0])
        self.assertIn("action=completed", written[1])
        self.assertNotIn(BOOTSTRAP_SUBJECT, self.errors.getvalue())

    def test_an_audit_record_that_never_reaches_the_outlet_counts_as_a_failure(self) -> None:
        """被日志配置过滤掉也是"没留痕"：不能因为没有抛错就当成写成功了。"""

        builder = RecordingEntryBuilder()
        audit = AuditStream()
        reauthorize_module.logger.disabled = True
        self.addCleanup(setattr, reauthorize_module.logger, "disabled", False)

        result = self._run(
            ["--bootstrap-subject", BOOTSTRAP_SUBJECT, "--confirm-bootstrap"],
            builder=builder,
            audit=audit,
        )

        self.assertEqual(result, 1)
        self.assertEqual(audit.records, [])
        self.assertEqual(builder.entry.begin_subjects, [], "审计没留下痕迹就不得发起授权")

    def test_the_audit_outlet_does_not_outlive_the_run(self) -> None:
        """谁建谁清：一次性作业不给宿主进程留下常驻 handler 或改过的日志级别。"""

        handlers_before = list(reauthorize_module.logger.handlers)
        level_before = reauthorize_module.logger.level

        self._run(
            ["--bootstrap-subject", BOOTSTRAP_SUBJECT, "--confirm-bootstrap"],
            builder=RecordingEntryBuilder(),
            audit=AuditStream(),
        )

        self.assertEqual(reauthorize_module.logger.handlers, handlers_before)
        self.assertEqual(reauthorize_module.logger.level, level_before)


if __name__ == "__main__":
    unittest.main()
