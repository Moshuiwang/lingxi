"""一次性数据清理脚本（P2 修复：外部独立审查 2026-08-18 坐实）。

``scripts/invalidate_pretranslation_publish_intents.py`` 作废 #227 翻译闸落地之前
遗留在 ``publish_outbox`` 里的未终态发布意图。真库那一半只有设置了
``LINGXI_POSTGRES_DSN`` 才跑（与其余真库用例同一姿态）；不需要数据库的那一半（缺 DSN
时的失败关闭）总是跑。
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import unittest
from io import StringIO
from unittest import mock

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "invalidate_pretranslation_publish_intents.py"

_spec = importlib.util.spec_from_file_location("invalidate_script", SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)


class MissingDsnTest(unittest.TestCase):
    def test_missing_dsn_fails_closed_without_touching_a_database(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("sys.stderr", new_callable=StringIO) as captured:
                exit_code = _module.main([])

        self.assertEqual(exit_code, 2)
        self.assertIn("LINGXI_POSTGRES_DSN", captured.getvalue())

    def test_a_blank_dsn_is_treated_as_missing(self) -> None:
        with mock.patch.dict(os.environ, {"LINGXI_POSTGRES_DSN": "   "}, clear=True):
            exit_code = _module.main([])

        self.assertEqual(exit_code, 2)


@unittest.skipUnless(
    os.environ.get("LINGXI_POSTGRES_DSN"),
    "跳过：未设置 LINGXI_POSTGRES_DSN，脚本的真库行为未验证（需真实 PostgreSQL 16）",
)
class RealDatabaseTest(unittest.TestCase):
    """真库对照：演练模式不写、``--apply`` 才写、且幂等。"""

    @classmethod
    def setUpClass(cls) -> None:
        from postgres_schema import ensure_production_schema

        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        from postgres_schema import reset_production_rows

        reset_production_rows(self._dsn)
        self._insert_fixture_rows()

    #: 一个用户同一时刻只允许一条非终态发布意图（``publish_outbox`` 的
    #: ``(user_id, permission_version)`` 唯一约束），因此每个状态各配一个化名用户，
    #: 而不是同一个用户堆五行。
    _STATUSES = ("pending", "publishing", "published", "superseded", "failed")

    def _insert_fixture_rows(self) -> None:
        from lingxi.adapters.postgres import connect

        with connect(self._dsn) as connection, connection.cursor() as cursor:
            for status in self._STATUSES:
                user_id = f"usr_invalidate_script_test_{status}"
                cursor.execute(
                    """INSERT INTO app_user
                           (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name,
                            department, tenant_key, employee_no, email,
                            provisioning_state, account_state, permission_version)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', 'enabled', 1)""",
                    (
                        user_id,
                        f"ou_{status}",
                        f"u_{status}",
                        f"on_{status}",
                        "化名",
                        "测试部门",
                        "tenant-fake",
                        f"E_{status}",
                        f"invalidate-script-{status}@example.invalid",
                    ),
                )
                published_at = "now()" if status == "published" else "NULL"
                cursor.execute(
                    f"""INSERT INTO publish_outbox
                           (id, user_id, permission_version, reason, status, payload, published_at)
                        VALUES (%s, %s, 1, 'daily_permission_refresh', %s, %s, {published_at})""",
                    (
                        f"outbox_invalidate_script_test_{status}",
                        user_id,
                        status,
                        '{"record_key":"x","email":"x","name":"x","permissions":"{}",'
                        '"status":"approved","updated_at":"2026-08-17T03:00:00Z"}',
                    ),
                )
            connection.commit()

    def _statuses(self) -> list[str]:
        from lingxi.adapters.postgres import connect

        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM publish_outbox WHERE user_id LIKE 'usr_invalidate_script_test_%' ORDER BY status"
            )
            return sorted(str(row[0]) for row in cursor.fetchall())

    def test_dry_run_does_not_write(self) -> None:
        before = self._statuses()

        exit_code = _module.main([])

        self.assertEqual(exit_code, 0)
        self.assertEqual(self._statuses(), before, "演练模式不得改动任何行")

    def test_apply_supersedes_only_pending_and_publishing(self) -> None:
        exit_code = _module.main(["--apply"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            self._statuses(),
            ["failed", "published", "superseded", "superseded", "superseded"],
            "pending 与 publishing 都转 superseded；published/failed/既有 superseded 不动",
        )

    def test_a_second_apply_is_a_safe_no_op(self) -> None:
        _module.main(["--apply"])
        after_first = self._statuses()

        exit_code = _module.main(["--apply"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(self._statuses(), after_first, "第二次执行不改变任何状态")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
