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
from pathlib import Path
from unittest.mock import patch

from lingxi.apps.reauthorize import (
    main,
    read_callback_url,
    validate_reauthorization_paths,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ONBOARDING_MODULE = "lingxi.core.identity.onboarding"


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

    def test_callback_url_is_read_with_terminal_echo_disabled(self) -> None:
        with patch(
            "lingxi.apps.reauthorize.getpass.getpass",
            return_value="opaque-callback-for-test",
        ) as reader:
            self.assertEqual(read_callback_url(), "opaque-callback-for-test")
        reader.assert_called_once()

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
