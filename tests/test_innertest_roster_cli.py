"""``python -m lingxi.apps.innertest_roster`` 的参数解析与调度：注入假回调，
不连数据库。真实落库路径见 ``test_innertest_roster_postgres.py``。
"""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from lingxi.adapters.postgres_innertest_roster import RosterImportPlan, RosterImportRejectedError
from lingxi.apps import innertest_roster
from lingxi.core.admin.innertest import InnertestError


class _FileFixture:
    """三份文件参数的临时目录：每个用例独立目录，互不覆盖。"""

    def __init__(self):
        self._dir = tempfile.TemporaryDirectory()

    def write(self, name, lines):
        path = Path(self._dir.name) / name
        path.write_text("\n".join(lines), encoding="utf-8")
        return str(path)

    def cleanup(self):
        self._dir.cleanup()


class MissingDsnTests(unittest.TestCase):
    def test_missing_dsn_exits_one(self) -> None:
        err = io.StringIO()

        code = innertest_roster.run(
            [
                "plan",
                "--scope",
                "s",
                "--gateway-legacy-file",
                "a",
                "--scheduler-legacy-file",
                "b",
                "--emails-file",
                "c",
            ],
            env={},
            stderr=err,
        )

        self.assertEqual(code, 1)
        self.assertIn(innertest_roster.DSN_ENV_VAR, err.getvalue())


class PlanCommandTests(unittest.TestCase):
    def setUp(self):
        self.fixture = _FileFixture()
        self.addCleanup(self.fixture.cleanup)

    def test_missing_source_file_exits_two_without_calling_plan(self) -> None:
        err = io.StringIO()
        calls = []

        code = innertest_roster.run(
            [
                "plan",
                "--scope",
                "s",
                "--gateway-legacy-file",
                "/no/such/file",
                "--scheduler-legacy-file",
                "/no/such/file",
                "--emails-file",
                "/no/such/file",
            ],
            env={"LINGXI_POSTGRES_DSN": "postgresql://u:p@x/y"},
            stderr=err,
            plan_import=lambda *a, **k: calls.append((a, k)) or RosterImportPlan("s", (), "d"),
        )

        self.assertEqual(code, 2)
        self.assertEqual(calls, [])

    def test_success_prints_digest_and_emails_without_writing(self) -> None:
        out = io.StringIO()
        gateway = self.fixture.write("gateway.txt", ["ou_1"])
        scheduler = self.fixture.write("scheduler.txt", ["ou_1"])
        emails = self.fixture.write("emails.txt", ["# 备注", "", "a@x.test", "b@x.test"])
        calls = []

        def fake_plan(dsn, **kwargs):
            calls.append((dsn, kwargs))
            return RosterImportPlan("s", (("a@x.test", "ou_a"), ("b@x.test", "ou_b")), "digest123")

        code = innertest_roster.run(
            [
                "plan",
                "--scope",
                "s",
                "--gateway-legacy-file",
                gateway,
                "--scheduler-legacy-file",
                scheduler,
                "--emails-file",
                emails,
            ],
            env={"LINGXI_POSTGRES_DSN": "postgresql://u:p@x/y"},
            stdout=out,
            plan_import=fake_plan,
        )

        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        _, kwargs = calls[0]
        self.assertEqual(kwargs["gateway_legacy"], ("ou_1",))
        self.assertEqual(kwargs["emails"], ("a@x.test", "b@x.test"))
        self.assertIn("digest123", out.getvalue())
        self.assertIn("a@x.test", out.getvalue())

    def test_rejection_reports_code_and_detail_on_stderr(self) -> None:
        err = io.StringIO()
        gateway = self.fixture.write("gateway.txt", ["ou_1"])
        scheduler = self.fixture.write("scheduler.txt", ["ou_1"])
        emails = self.fixture.write("emails.txt", ["missing@x.test"])

        def fake_plan(dsn, **kwargs):
            raise RosterImportRejectedError(
                "email_resolution_failed", detail=(("missing@x.test", "email_not_in_roster"),)
            )

        code = innertest_roster.run(
            [
                "plan",
                "--scope",
                "s",
                "--gateway-legacy-file",
                gateway,
                "--scheduler-legacy-file",
                scheduler,
                "--emails-file",
                emails,
            ],
            env={"LINGXI_POSTGRES_DSN": "postgresql://u:p@x/y"},
            stderr=err,
            plan_import=fake_plan,
        )

        self.assertEqual(code, 1)
        self.assertIn("email_resolution_failed", err.getvalue())
        self.assertIn("missing@x.test", err.getvalue())
        self.assertIn("email_not_in_roster", err.getvalue())


class ApplyCommandTests(unittest.TestCase):
    def setUp(self):
        self.fixture = _FileFixture()
        self.addCleanup(self.fixture.cleanup)
        self.gateway = self.fixture.write("gateway.txt", ["ou_1"])
        self.scheduler = self.fixture.write("scheduler.txt", ["ou_1"])
        self.emails = self.fixture.write("emails.txt", ["a@x.test"])

    def _argv(self, digest="digest123", binding_id="iab_given_by_caller"):
        return [
            "apply",
            "--scope",
            "s",
            "--gateway-legacy-file",
            self.gateway,
            "--scheduler-legacy-file",
            self.scheduler,
            "--emails-file",
            self.emails,
            "--confirm-digest",
            digest,
            "--binding-id",
            binding_id,
        ]

    def test_success_redacts_admin_open_id_and_prints_binding(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        calls = []

        def fake_apply(dsn, **kwargs):
            calls.append(kwargs)
            return RosterImportPlan("s", (("a@x.test", "ou_a"),), "digest123")

        code = innertest_roster.run(
            self._argv(),
            env={"LINGXI_POSTGRES_DSN": "postgresql://u:p@x/y"},
            stdout=out,
            stderr=err,
            lookup_delegated_subject=lambda: "ou_real_secret_identifier",
            apply_import=fake_apply,
        )

        self.assertEqual(code, 0)
        self.assertEqual(calls[0]["admin_open_id"], "ou_real_secret_identifier")
        # 绑定标识必须原样用调用方给的那个：受限入口按它查行，自行生成的对不上。
        self.assertEqual(calls[0]["binding_id"], "iab_given_by_caller")
        self.assertNotIn("ou_real_secret_identifier", out.getvalue())
        # 只封锁标准输出等于只锁半扇门：标识漏到标准错误一样是泄漏。
        self.assertNotIn("ou_real_secret_identifier", err.getvalue())
        self.assertIn("digest123", out.getvalue())
        self.assertIn(calls[0]["binding_id"], out.getvalue())

    def test_binding_id_is_required_and_is_not_generated(self) -> None:
        """不给绑定标识就不许写。

        自行生成的标识永远对不上受限入口要求的那一个（绑定文件与环境变量里
        登记的值），装配完认证不了任何请求——那正是这个参数存在的理由。
        """
        argv = [a for a in self._argv() if a not in ("--binding-id", "iab_given_by_caller")]
        with self.assertRaises(SystemExit) as raised:
            innertest_roster.run(
                argv,
                env={"LINGXI_POSTGRES_DSN": "postgresql://u:p@x/y"},
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertEqual(raised.exception.code, 2)

    def test_lookup_failure_fails_closed_without_calling_apply(self) -> None:
        err = io.StringIO()
        calls = []

        def exploding_lookup():
            raise RuntimeError("connection refused")

        code = innertest_roster.run(
            self._argv(),
            env={"LINGXI_POSTGRES_DSN": "postgresql://u:p@x/y"},
            stderr=err,
            lookup_delegated_subject=exploding_lookup,
            apply_import=lambda *a, **k: calls.append(1) or RosterImportPlan("s", (), "d"),
        )

        self.assertEqual(code, 1)
        self.assertEqual(calls, [])

    def test_adapter_rejection_surfaces_code(self) -> None:
        err = io.StringIO()

        def fake_apply(dsn, **kwargs):
            raise RosterImportRejectedError("digest_mismatch")

        code = innertest_roster.run(
            self._argv(digest="wrong"),
            env={"LINGXI_POSTGRES_DSN": "postgresql://u:p@x/y"},
            stderr=err,
            lookup_delegated_subject=lambda: "ou_admin",
            apply_import=fake_apply,
        )

        self.assertEqual(code, 1)
        self.assertIn("digest_mismatch", err.getvalue())


class VerifyCommandTests(unittest.TestCase):
    def test_success_prints_json_with_status_fields(self) -> None:
        out = io.StringIO()

        code = innertest_roster.run(
            ["verify", "--scope", "s"],
            env={"LINGXI_POSTGRES_DSN": "postgresql://u:p@x/y"},
            stdout=out,
            verify_status=lambda dsn, *, scope: dict(
                mode="database", version=1, import_digest="digest123", member_count=2
            ),
        )

        self.assertEqual(code, 0)
        payload = out.getvalue()
        self.assertIn('"mode": "database"', payload)
        self.assertIn('"member_count": 2', payload)

    def test_binding_readback_is_included_when_binding_id_is_given(self) -> None:
        out = io.StringIO()

        code = innertest_roster.run(
            ["verify", "--scope", "s", "--binding-id", "iab_1"],
            env={"LINGXI_POSTGRES_DSN": "postgresql://u:p@x/y"},
            stdout=out,
            verify_status=lambda dsn, *, scope: dict(
                mode="database", version=1, import_digest="digest123", member_count=2
            ),
            binding_status=lambda dsn, *, scope, binding_id: dict(
                binding_id=binding_id, version=1, enabled=True, authorized=True
            ),
        )

        self.assertEqual(code, 0)
        self.assertIn('"binding"', out.getvalue())
        self.assertIn('"enabled": true', out.getvalue())

    def test_binding_readback_failure_is_not_reported_as_success(self) -> None:
        """点名要回读绑定却没读成，不能靠退出码或 ok 让部署步骤放行。"""
        out = io.StringIO()

        def exploding_binding(dsn, *, scope, binding_id):
            raise RuntimeError("模拟绑定回读时连接断开")

        code = innertest_roster.run(
            ["verify", "--scope", "s", "--binding-id", "iab_1"],
            env={"LINGXI_POSTGRES_DSN": "postgresql://u:p@x/y"},
            stdout=out,
            verify_status=lambda dsn, *, scope: dict(
                mode="database", version=1, import_digest="digest123", member_count=2
            ),
            binding_status=exploding_binding,
        )

        self.assertEqual(code, 1)
        self.assertIn('"ok": false', out.getvalue())
        self.assertIn('"binding_error": "RuntimeError"', out.getvalue())
        # 名单那一半已经读到的结果照旧输出，不因为绑定失败就一起丢掉。
        self.assertIn('"member_count": 2', out.getvalue())

    def test_status_failure_exits_one(self) -> None:
        err = io.StringIO()

        def exploding_status(dsn, *, scope):
            raise InnertestError("roster_unavailable")

        code = innertest_roster.run(
            ["verify", "--scope", "s"],
            env={"LINGXI_POSTGRES_DSN": "postgresql://u:p@x/y"},
            stderr=err,
            verify_status=exploding_status,
        )

        self.assertEqual(code, 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
