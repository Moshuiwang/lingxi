"""``python -m lingxi.apps.admin_bootstrap`` 的种子命令（Issue #95 S-M-01）。

单元层测参数校验、默认只读预演、显式确认后才写入，以及输出脱敏；真实数据库写入
路径见 ``test_admin_registry_postgres.py``（``seed_admin_registry_entry`` 本身）。
本文件只用注入的假 ``lookup_delegated_subject``/``seed`` 回调，不连数据库。
"""

from __future__ import annotations

import io
import unittest

from lingxi.apps import admin_bootstrap
from lingxi.core.admin.registry import AdminRegistrySeedConflictError


class MissingDsnTests(unittest.TestCase):
    def test_missing_dsn_exits_one(self) -> None:
        err = io.StringIO()

        code = admin_bootstrap.run([], env={}, stderr=err)

        self.assertEqual(code, 1)
        self.assertIn(admin_bootstrap.DSN_ENV_VAR, err.getvalue())


class ReadOnlyPreviewTests(unittest.TestCase):
    """不带 ``--confirm`` 时只报告、不落库——``seed`` 回调必须一次都不被调用。"""

    def test_preview_reports_subject_without_writing(self) -> None:
        out = io.StringIO()
        seed_calls: list[str] = []

        code = admin_bootstrap.run(
            [],
            env={"LINGXI_POSTGRES_DSN": "postgresql://u:p@x/y"},
            stdout=out,
            lookup_delegated_subject=lambda: "ou_real_secret_identifier",
            seed=lambda open_id: seed_calls.append(open_id) or True,
        )

        self.assertEqual(code, 0)
        self.assertEqual(seed_calls, [])
        self.assertIn("只读预演", out.getvalue())
        # 输出脱敏：完整 open_id 不得出现在标准输出里。
        self.assertNotIn("ou_real_secret_identifier", out.getvalue())

    def test_missing_delegated_subject_fails_closed_without_writing(self) -> None:
        err = io.StringIO()
        seed_calls: list[str] = []

        code = admin_bootstrap.run(
            ["--confirm"],
            env={"LINGXI_POSTGRES_DSN": "postgresql://u:p@x/y"},
            stderr=err,
            lookup_delegated_subject=lambda: None,
            seed=lambda open_id: seed_calls.append(open_id) or True,
        )

        self.assertEqual(code, 1)
        self.assertEqual(seed_calls, [])
        self.assertIn("尚未登记", err.getvalue())

    def test_lookup_failure_fails_closed_without_writing(self) -> None:
        err = io.StringIO()
        seed_calls: list[str] = []

        def exploding_lookup() -> str | None:
            raise RuntimeError("connection refused")

        code = admin_bootstrap.run(
            ["--confirm"],
            env={"LINGXI_POSTGRES_DSN": "postgresql://u:p@x/y"},
            stderr=err,
            lookup_delegated_subject=exploding_lookup,
            seed=lambda open_id: seed_calls.append(open_id) or True,
        )

        self.assertEqual(code, 1)
        self.assertEqual(seed_calls, [])


class ConfirmedWriteTests(unittest.TestCase):
    def test_confirmed_run_calls_seed_with_the_looked_up_subject(self) -> None:
        out = io.StringIO()
        seed_calls: list[str] = []

        code = admin_bootstrap.run(
            ["--confirm"],
            env={"LINGXI_POSTGRES_DSN": "postgresql://u:p@x/y"},
            stdout=out,
            lookup_delegated_subject=lambda: "ou_real_secret_identifier",
            seed=lambda open_id: seed_calls.append(open_id) or True,
        )

        self.assertEqual(code, 0)
        self.assertEqual(seed_calls, ["ou_real_secret_identifier"])
        self.assertIn("已登记", out.getvalue())
        self.assertNotIn("ou_real_secret_identifier", out.getvalue())

    def test_seed_returning_false_reports_already_registered(self) -> None:
        out = io.StringIO()

        code = admin_bootstrap.run(
            ["--confirm"],
            env={"LINGXI_POSTGRES_DSN": "postgresql://u:p@x/y"},
            stdout=out,
            lookup_delegated_subject=lambda: "ou_real_secret_identifier",
            seed=lambda open_id: False,
        )

        self.assertEqual(code, 0)
        self.assertIn("已存在有效登记", out.getvalue())

    def test_seed_failure_exits_one(self) -> None:
        err = io.StringIO()

        def exploding_seed(open_id: str) -> bool:
            raise RuntimeError("constraint violation")

        code = admin_bootstrap.run(
            ["--confirm"],
            env={"LINGXI_POSTGRES_DSN": "postgresql://u:p@x/y"},
            stderr=err,
            lookup_delegated_subject=lambda: "ou_x",
            seed=exploding_seed,
        )

        self.assertEqual(code, 1)

    def test_seed_conflict_with_an_inconsistent_existing_row_exits_one(self) -> None:
        """已有不一致行→非零退出（opus 批量审查 P2 修复）：`seed` 检测到已存在
        一条 active 登记、但字段与本次意图播种的内容不一致时，此前会被
        `seed_returning_false...` 那条无条件当成"已存在有效登记"报告成功——
        这条用例证明现在必须响亮拒绝，且报的差异只列字段名，不回显 open_id。"""

        err = io.StringIO()

        def conflicting_seed(open_id: str) -> bool:
            raise AdminRegistrySeedConflictError(mismatched_fields=("label",))

        code = admin_bootstrap.run(
            ["--confirm"],
            env={"LINGXI_POSTGRES_DSN": "postgresql://u:p@x/y"},
            stderr=err,
            lookup_delegated_subject=lambda: "ou_real_secret_identifier",
            seed=conflicting_seed,
        )

        self.assertEqual(code, 1)
        message = err.getvalue()
        self.assertIn("不一致", message)
        self.assertIn("label", message)
        self.assertNotIn("ou_real_secret_identifier", message)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
