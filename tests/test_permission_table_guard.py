"""权限发布表宿主守卫的离线用例与可变异证据。"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest import mock


REPOSITORY_ROOT = Path(__file__).parents[1]
GUARD_PATH = (REPOSITORY_ROOT / "deploy" / "permission_table_guard.py").resolve()
print(f"loaded_permission_table_guard={GUARD_PATH}")
MODULE_NAME = "permission_table_guard_under_test"
SPEC = importlib.util.spec_from_file_location(MODULE_NAME, GUARD_PATH)
assert SPEC is not None and SPEC.loader is not None
GUARD = importlib.util.module_from_spec(SPEC)
sys.modules[MODULE_NAME] = GUARD
SPEC.loader.exec_module(GUARD)


APP_ID = "cli_synthetic"
APP_SECRET = "app-secret-sentinel"
APP_TOKEN = "app-token-sentinel"
TABLE_ID = "tbl_synthetic"
DB_PASSWORD = "db-password-sentinel"
EMAIL = "stage-user@example.invalid"
CIPHER = "cipher-sentinel"
FIELDS = tuple(GUARD.FIELD_NAMES)


def _fields(number: int, *, cipher: str = CIPHER, email: str = EMAIL) -> dict[str, str]:
    return {
        "record_key": email,
        "email": email,
        "name": f"测试人员-{number}",
        "permissions": '{"company":"synthetic"}',
        "status": "approved",
        "updated_at": f"2026-09-14T00:00:0{number}Z",
        "token_cipher": cipher,
    }


def _write_env(directory: Path) -> Path:
    path = directory / "scheduler.env"
    path.write_text(
        "\n".join(
            (
                f"LINGXI_FEISHU_APP_ID='{APP_ID}'",
                f"LINGXI_FEISHU_APP_SECRET='{APP_SECRET}'",
                f"LINGXI_PERMISSION_BITABLE_APP_TOKEN='{APP_TOKEN}'",
                f"LINGXI_PERMISSION_BITABLE_TABLE_ID='{TABLE_ID}'",
                "LINGXI_FEISHU_BASE_URL='https://open.feishu.test/open-apis'",
                f"LINGXI_POSTGRES_DSN='postgresql://dbuser:{DB_PASSWORD}@db.invalid/lingxi'",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    return path


def _config(directory: Path) -> GUARD.GuardConfig:
    return GUARD.load_config(_write_env(directory), need_feishu=True)


class FakeTransport:
    """记录请求的假飞书传输，可模拟多页、读回异常和删除。"""

    def __init__(self, rows: dict[str, dict[str, str]] | None = None) -> None:
        self.rows = {key: dict(value) for key, value in (rows or {}).items()}
        self.calls: list[tuple[str, str, object]] = []
        self.pages: list[list[dict[str, object]]] | None = None
        self.mismatch_ids: set[str] = set()
        self._mismatch_once: set[str] = set()

    def __call__(self, method: str, url: str, *, body: object, headers: object) -> object:
        self.calls.append((method, url, body))
        if method == "POST" and url.endswith("/auth/v3/tenant_access_token/internal"):
            return {"code": 0, "tenant_access_token": "tenant-token-sentinel"}
        if method == "GET" and urlsplit(url).path.endswith("/records"):
            return self._list(url)
        if "/records/" not in url:
            raise AssertionError(url)
        record_id = urlsplit(url).path.rsplit("/", 1)[1]
        if method == "GET":
            if record_id not in self.rows:
                return GUARD.HttpResponse(404, {})
            return {
                "code": 0,
                "data": {"record": {"record_id": record_id, "fields": self.rows[record_id]}},
            }
        if method == "PUT":
            if record_id not in self.rows:
                return GUARD.HttpResponse(404, {})
            fields = dict(body["fields"])  # type: ignore[index]
            if record_id in self.mismatch_ids and record_id not in self._mismatch_once:
                self._mismatch_once.add(record_id)
                fields.pop("token_cipher", None)
            self.rows[record_id].update(fields)
            return {"code": 0, "data": {}}
        if method == "DELETE":
            self.rows.pop(record_id, None)
            return {"code": 0, "data": {}}
        raise AssertionError(method)

    def _list(self, url: str) -> object:
        if self.pages is not None:
            query = parse_qs(urlsplit(url).query)
            token = query.get("page_token", [""])[0]
            index = 0 if not token else int(token.removeprefix("page-"))
            items = self.pages[index]
            has_more = index + 1 < len(self.pages)
            result: dict[str, object] = {"items": items, "has_more": has_more}
            if has_more:
                result["page_token"] = f"page-{index + 1}"
            return {"code": 0, "data": result}
        items = [
            {"record_id": record_id, "fields": fields}
            for record_id, fields in sorted(self.rows.items())
        ]
        return {"code": 0, "data": {"items": items, "has_more": False}}


def _client(config: GUARD.GuardConfig, transport: FakeTransport, **kwargs: object) -> GUARD.PermissionTableClient:
    return GUARD.PermissionTableClient(config, transport=transport, **kwargs)


def _backup(directory: Path, rows: dict[str, dict[str, str]], *, transport: FakeTransport) -> Path:
    config = _config(directory)
    client = _client(config, transport)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        self_check = GUARD.run_backup(config, directory, client=client)
    assert self_check == 0
    files = sorted(directory.glob("backup-*.json"))
    assert len(files) == 1
    return files[0]


def _ledger_file(directory: Path, entries: list[dict[str, object]]) -> Path:
    return GUARD._secure_json_write(directory, "ledger-input", entries)


def _plan_digest(directory: Path) -> str:
    plans = sorted(directory.glob("revert-plan-*.json"))
    assert plans
    return json.loads(plans[-1].read_text(encoding="utf-8"))["summary_sha256"]


def _write_diff_state(
    directory: Path,
    backup_rows: dict[str, dict[str, str]],
    current_rows: dict[str, dict[str, str]],
    ledger_entries: list[dict[str, object]],
) -> tuple[GUARD.GuardConfig, Path, Path, FakeTransport]:
    transport_for_backup = FakeTransport(backup_rows)
    backup_path = _backup(directory, backup_rows, transport=transport_for_backup)
    transport = FakeTransport(current_rows)
    ledger_path = _ledger_file(directory, ledger_entries)
    return _config(directory), backup_path, ledger_path, transport


class PermissionTableGuardTest(unittest.TestCase):
    def test_env_file_permission_and_owner_are_enforced_and_values_never_echoed(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            env_path = _write_env(directory)
            os.chmod(env_path, 0o644)
            with self.assertRaisesRegex(GUARD.GuardError, "env_file_permission_unsafe") as context:
                GUARD.load_config(env_path, need_feishu=True)
            self.assertNotIn(APP_SECRET, str(context.exception))
            os.chmod(env_path, 0o600)
            with mock.patch.object(GUARD.os, "getuid", return_value=os.getuid() + 1):
                with self.assertRaisesRegex(GUARD.GuardError, "env_file_owner_mismatch"):
                    GUARD.load_config(env_path, need_feishu=True)
            config = GUARD.load_config(env_path, need_feishu=True)
            self.assertNotIn(APP_SECRET, repr(config))
            self.assertNotIn(APP_TOKEN, repr(config))

    def test_backup_writes_0600_and_row_count_matches_pages(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            rows = {f"rec-{index}": _fields(index) for index in range(3)}
            transport = FakeTransport(rows)
            transport.pages = [
                [{"record_id": "rec-0", "fields": rows["rec-0"]}],
                [{"record_id": "rec-1", "fields": rows["rec-1"]}],
                [{"record_id": "rec-2", "fields": rows["rec-2"]}],
            ]
            backup_path = _backup(directory, rows, transport=transport)
            self.assertEqual(stat.S_IMODE(backup_path.stat().st_mode), 0o600)
            payload = json.loads(backup_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["row_count"], 3)
            self.assertEqual(payload["row_count"], len(payload["rows"]))
            self.assertEqual(
                len([call for call in transport.calls if call[0] == "GET" and "/records?" in call[1]]),
                3,
            )
            config = _config(directory)
            upper_bound = FakeTransport(rows)
            upper_bound.pages = [*transport.pages, transport.pages[-1]]
            with self.assertRaisesRegex(GUARD.GuardError, "feishu_pagination_limit"):
                GUARD.run_backup(
                    config,
                    directory,
                    client=_client(config, upper_bound, max_pages=3),
                )

    def test_backup_stores_only_app_token_digest(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            backup_path = _backup(directory, {"rec-1": _fields(1)}, transport=FakeTransport({"rec-1": _fields(1)}))
            payload = backup_path.read_text(encoding="utf-8")
            self.assertIn(CIPHER, payload)
            self.assertNotIn(APP_TOKEN, payload)
            self.assertNotIn(APP_SECRET, payload)
            parsed = json.loads(payload)
            self.assertEqual(
                parsed["table"]["app_token_sha256"],
                __import__("hashlib").sha256(APP_TOKEN.encode()).hexdigest(),
            )

    def test_ledger_includes_only_published_rows_since_and_lists_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            config = GUARD.load_config(_write_env(directory), need_database=True)
            fake_psql = directory / "fake-psql"
            fake_psql.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                f"assert {DB_PASSWORD!r} not in ' '.join(sys.argv)\n"
                f"assert os.environ.get('PGPASSWORD') == {DB_PASSWORD!r}\n"
                "print(json.dumps([\n"
                " {'record_id':'rec-1','record_key':'stage-user@example.invalid','published_at':'2026-09-14T00:00:01Z','outbox_id':'out-1'},\n"
                " {'record_id':None,'record_key':'unresolved@example.invalid','published_at':'2026-09-14T00:00:02Z','outbox_id':'out-2'}\n"
                "]))\n",
                encoding="utf-8",
            )
            os.chmod(fake_psql, 0o700)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                result = GUARD.run_ledger(
                    config,
                    directory,
                    "2026-09-14T00:00:00Z",
                    psql_bin=str(fake_psql),
                )
            self.assertEqual(result, 0)
            self.assertEqual(output.getvalue().count("ledger="), 1)
            ledger_path = sorted(directory.glob("ledger-*.json"))[0]
            entries = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertEqual(entries[0]["record_id"], "rec-1")
            self.assertTrue(entries[1]["unresolved"])
            self.assertIsNone(entries[1]["record_id"])

    def test_revert_is_dry_run_by_default_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            original = {"rec-1": _fields(1), "rec-2": _fields(2)}
            current = dict(original)
            current["rec-1"] = {**current["rec-1"], "permissions": "changed"}
            current["rec-3"] = _fields(3)
            config, backup_path, ledger_path, transport = _write_diff_state(
                directory,
                original,
                current,
                [{"record_id": "rec-1", "record_key": EMAIL, "published_at": "2026-09-14T00:01:00Z", "outbox_id": "out-1"}],
            )
            result = GUARD.run_revert(config, directory, backup_path, ledger_path, client=_client(config, transport))
            self.assertEqual(result, 0)
            self.assertEqual([call[0] for call in transport.calls if call[0] in ("PUT", "DELETE")], [])
            plan_path = sorted(directory.glob("revert-plan-*.json"))[0]
            self.assertEqual(stat.S_IMODE(plan_path.stat().st_mode), 0o600)
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["restore"], ["rec-1"])
            self.assertEqual(plan["skip"], ["rec-3"])

    def test_revert_refuses_wrong_confirm_digest(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            config, backup_path, ledger_path, transport = _write_diff_state(
                directory,
                {"rec-1": _fields(1)},
                {"rec-1": {**_fields(1), "status": "changed"}},
                [{"record_id": "rec-1", "record_key": EMAIL, "published_at": "2026-09-14T00:01:00Z", "outbox_id": "out-1"}],
            )
            result = GUARD.run_revert(
                config,
                directory,
                backup_path,
                ledger_path,
                confirm="0" * 64,
                client=_client(config, transport),
            )
            self.assertEqual(result, 1)
            self.assertEqual([call[0] for call in transport.calls if call[0] in ("PUT", "DELETE")], [])

    def test_revert_restores_all_seven_fields_for_ledger_rows_in_backup(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            original = {"rec-1": _fields(1)}
            current = {"rec-1": {**_fields(1), "record_key": "changed@example.invalid", "token_cipher": "old-cipher"}}
            config, backup_path, ledger_path, transport = _write_diff_state(
                directory,
                original,
                current,
                [{"record_id": "rec-1", "record_key": EMAIL, "published_at": "2026-09-14T00:01:00Z", "outbox_id": "out-1"}],
            )
            dry_result = GUARD.run_revert(config, directory, backup_path, ledger_path, client=_client(config, transport))
            self.assertEqual(dry_result, 0)
            digest = _plan_digest(directory)
            result = GUARD.run_revert(
                config,
                directory,
                backup_path,
                ledger_path,
                confirm=digest,
                client=_client(config, transport),
            )
            self.assertEqual(result, 0)
            put_calls = [call for call in transport.calls if call[0] == "PUT"]
            self.assertEqual(len(put_calls), 1)
            self.assertEqual(set(put_calls[0][2]["fields"]), set(FIELDS))  # type: ignore[index]
            self.assertEqual(put_calls[0][2]["fields"]["token_cipher"], CIPHER)  # type: ignore[index]
            self.assertEqual(transport.rows["rec-1"], original["rec-1"])

    def test_revert_deletes_ledger_rows_absent_from_backup(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            config, backup_path, ledger_path, transport = _write_diff_state(
                directory,
                {"rec-1": _fields(1)},
                {"rec-1": _fields(1), "rec-new": _fields(2)},
                [{"record_id": "rec-new", "record_key": EMAIL, "published_at": "2026-09-14T00:01:00Z", "outbox_id": "out-new"}],
            )
            GUARD.run_revert(config, directory, backup_path, ledger_path, client=_client(config, transport))
            digest = _plan_digest(directory)
            result = GUARD.run_revert(
                config,
                directory,
                backup_path,
                ledger_path,
                confirm=digest,
                client=_client(config, transport),
            )
            self.assertEqual(result, 0)
            self.assertNotIn("rec-new", transport.rows)
            self.assertEqual(len([call for call in transport.calls if call[0] == "DELETE"]), 1)

    def test_revert_never_touches_rows_outside_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            original = {"rec-1": _fields(1)}
            current = {
                "rec-1": {**_fields(1), "permissions": "production-change"},
                "rec-production-new": _fields(2),
            }
            config, backup_path, ledger_path, transport = _write_diff_state(
                directory, original, current, []
            )
            GUARD.run_revert(config, directory, backup_path, ledger_path, client=_client(config, transport))
            plan = json.loads(sorted(directory.glob("revert-plan-*.json"))[0].read_text(encoding="utf-8"))
            self.assertEqual(plan["restore"], [])
            self.assertEqual(plan["delete"], [])
            self.assertEqual(plan["skip"], ["rec-1", "rec-production-new"])
            result = GUARD.run_revert(
                config,
                directory,
                backup_path,
                ledger_path,
                confirm=plan["summary_sha256"],
                client=_client(config, transport),
            )
            self.assertEqual(result, 0)
            self.assertEqual([call[0] for call in transport.calls if call[0] in ("PUT", "DELETE")], [])

    def test_revert_stops_on_readback_mismatch_and_is_rerunnable(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            original = {"rec-1": _fields(1), "rec-2": _fields(2)}
            current = {
                "rec-1": {**_fields(1), "permissions": "changed-1"},
                "rec-2": {**_fields(2), "permissions": "changed-2", "token_cipher": "old-cipher"},
            }
            config, backup_path, ledger_path, transport = _write_diff_state(
                directory,
                original,
                current,
                [
                    {"record_id": "rec-1", "record_key": EMAIL, "published_at": "2026-09-14T00:01:00Z", "outbox_id": "out-1"},
                    {"record_id": "rec-2", "record_key": EMAIL, "published_at": "2026-09-14T00:01:01Z", "outbox_id": "out-2"},
                ],
            )
            GUARD.run_revert(config, directory, backup_path, ledger_path, client=_client(config, transport))
            digest = _plan_digest(directory)
            transport.mismatch_ids = {"rec-2"}
            first = GUARD.run_revert(
                config, directory, backup_path, ledger_path, confirm=digest, client=_client(config, transport)
            )
            self.assertEqual(first, 1)
            self.assertEqual(len([call for call in transport.calls if call[0] == "PUT" and "/rec-2" in call[1]]), 1)
            self.assertEqual(transport.rows["rec-1"], original["rec-1"])
            self.assertNotEqual(transport.rows["rec-2"], original["rec-2"])
            rec1_puts_before = len([call for call in transport.calls if call[0] == "PUT" and "/rec-1" in call[1]])
            transport.mismatch_ids = set()
            second = GUARD.run_revert(
                config, directory, backup_path, ledger_path, confirm=digest, client=_client(config, transport)
            )
            self.assertEqual(second, 0)
            rec1_puts_after = len([call for call in transport.calls if call[0] == "PUT" and "/rec-1" in call[1]])
            self.assertEqual(rec1_puts_after, rec1_puts_before)
            self.assertEqual(transport.rows, original)

    def test_no_secret_in_logs_or_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            fields = _fields(1)
            transport = FakeTransport({"rec-1": fields})
            config = _config(directory)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = GUARD.run_backup(config, directory, client=_client(config, transport))
            self.assertEqual(result, 0)
            backup_path = sorted(directory.glob("backup-*.json"))[0]
            self.assertIn(CIPHER, backup_path.read_text(encoding="utf-8"))
            self.assertNotIn(APP_SECRET, stdout.getvalue() + stderr.getvalue())
            self.assertNotIn(CIPHER, stdout.getvalue() + stderr.getvalue())
            ledger_path = _ledger_file(
                directory,
                [{"record_id": "rec-1", "record_key": EMAIL, "published_at": "2026-09-14T00:01:00Z", "outbox_id": "out-1"}],
            )
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                GUARD.run_revert(
                    config,
                    directory,
                    backup_path,
                    ledger_path,
                    client=_client(config, FakeTransport({"rec-1": fields})),
                )
            for path in directory.glob("*.json"):
                if path == Path(backup_path):
                    continue
                contents = path.read_text(encoding="utf-8")
                self.assertNotIn(APP_SECRET, contents, path.name)
                self.assertNotIn(CIPHER, contents, path.name)


if __name__ == "__main__":
    unittest.main()
