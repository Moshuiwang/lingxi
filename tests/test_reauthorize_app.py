"""正式重授权 apps 入口的部署可达与启动安全断言。"""

from __future__ import annotations

import ast
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from lingxi.adapters.feishu_reauthorization import ReauthorizationResult, ReauthorizationStart
from lingxi.adapters.oauth_bridge_client import OAuthBridgeMessage
from lingxi.apps.reauthorize import (
    bridge_wait_seconds,
    main,
    validate_reauthorization_paths,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ONBOARDING_MODULE = "lingxi.core.identity.onboarding"


class FakeReauthorizationEntry:
    state = "r" * 43

    def __init__(self) -> None:
        self.callbacks: list[tuple[str | None, str | None, str | None]] = []

    def begin(self, *, expected_subject_open_id: str | None = None) -> ReauthorizationStart:
        return ReauthorizationStart(
            "https://accounts.example.test/authorize?state=" + self.state,
            self.state,
            datetime.now(timezone.utc),
        )

    def handle_callback(
        self,
        state: str | None,
        *,
        code: str | None = None,
        error: str | None = None,
    ) -> ReauthorizationResult:
        self.callbacks.append((state, code, error))
        return ReauthorizationResult(True, "completed", "专用授权已更新。", False)


class FakeBridgeThread:
    def join(self, timeout: float | None = None) -> None:
        return None


class FakeBridge:
    instances: list["FakeBridge"] = []

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
            imported.update(alias.name for alias in node.names if alias.name == "lingxi" or alias.name.startswith("lingxi."))
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
            with patch("lingxi.apps.reauthorize._build_entry", return_value=entry), patch(
                "lingxi.apps.reauthorize.OAuthBridgeClient", FakeBridge
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

    def test_formal_reauthorization_import_closure_excludes_bot_test_onboarding(self) -> None:
        closure = _formal_import_closure(
            (
                "lingxi.apps.reauthorize",
                "lingxi.apps.reauthorize.__main__",
            )
        )

        self.assertNotIn(ONBOARDING_MODULE, closure)


if __name__ == "__main__":
    unittest.main()
