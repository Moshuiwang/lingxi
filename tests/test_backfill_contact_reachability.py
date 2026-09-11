"""`scripts/ops/backfill_contact_reachability.py` 的真库断言（#673 完成标准 ⑦）。

覆盖：四个来源各自的计数、dry-run 与实跑计数逐项一致、按时间升序回放使
"旧成功不覆盖新失败"在回填侧同样成立、中断后重跑（本文件用"重复整份运行"
代理）不重复、不把已经更新的状态往回拨。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from postgres_schema import psycopg_available, reset_production_rows

from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_identity import (
    CONTACT_REACHABLE,
    CONTACT_UNAVAILABLE,
    PostgresAppUserStore,
)
from lingxi.core.ids import new_id

REPOSITORY_ROOT = Path(__file__).parents[1]
SCRIPT = REPOSITORY_ROOT / "scripts" / "ops" / "backfill_contact_reachability.py"


def _load_script() -> Any:
    module_name = "backfill_contact_reachability_under_test"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


TOOL = _load_script()

SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，数据库约束类断言未验证（需真实 PostgreSQL）"
    if not os.environ.get("LINGXI_POSTGRES_DSN")
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，数据库约束类断言未验证"
)


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN") and psycopg_available(), SKIP_REASON)
class BackfillTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]

    def setUp(self) -> None:
        reset_production_rows(self._dsn)
        self.store = PostgresAppUserStore(self._dsn)
        self.open_id = f"ou_{new_id('bf')}"
        self.user_id = new_id("usr")
        self._insert_app_user()

    def _insert_app_user(self) -> None:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO app_user
                     (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name,
                      department, tenant_key, provisioning_state)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 'active')""",
                (
                    self.user_id,
                    self.open_id,
                    f"{self.open_id}_u",
                    f"{self.open_id}_un",
                    "测试用户",
                    "测试部门",
                    "t1",
                ),
            )

    def _insert_inbound_event(self, *, event_id: str, at: datetime) -> None:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO inbound_event
                     (feishu_event_id, received_at, event_type, user_open_id, trace_id)
                   VALUES (%s, %s, 'im.message.receive_v1', %s, %s)""",
                (event_id, at, self.open_id, f"trc_{event_id}"),
            )

    def _insert_conversation(self, *, at: datetime) -> None:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO conversation (id, user_id, feishu_chat_id, created_at)
                   VALUES (%s, %s, %s, %s)""",
                (new_id("cnv"), self.user_id, "oc_test_chat", at),
            )

    def _insert_task(self, *, at: datetime) -> str:
        conv_id = new_id("cnv")
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO conversation (id, user_id, feishu_chat_id) VALUES (%s, %s, %s)""",
                (conv_id, self.user_id, f"oc_task_{conv_id}"),
            )
            cursor.execute(
                """INSERT INTO task
                     (id, conversation_id, user_id, inbound_event_id, prompt,
                      target_worker_version, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (new_id("tsk"), conv_id, self.user_id, "evt_placeholder", "测试问题", "stable", at),
            )
        return conv_id

    def _insert_outreach_message(
        self, *, at: datetime, status: str, last_error: str | None = None
    ) -> None:
        delivered_at = at if status == "delivered" else None
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO outreach_message
                     (id, recipient_open_id, user_id, purpose, content_key, content_version,
                      card_style, status, last_error, created_at, delivered_at, dedupe_key)
                   VALUES (%s, %s, %s, 'apply', 'outreach.welcome', '2026-01-01', 'field_list',
                           %s, %s, %s, %s, %s)""",
                (
                    new_id("omr"),
                    self.open_id,
                    self.user_id,
                    status,
                    last_error,
                    at,
                    delivered_at,
                    f"dedupe_{new_id('k')}",
                ),
            )

    def _column(self, column: str) -> object:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {column} FROM app_user WHERE feishu_open_id = %s", (self.open_id,)
            )
            return cursor.fetchone()[0]

    def test_dry_run_and_apply_report_the_same_per_source_counts(self) -> None:
        now = datetime.now(UTC)
        self._insert_inbound_event(event_id="evt_bf1", at=now - timedelta(days=10))
        self._insert_conversation(at=now - timedelta(days=9))
        self._insert_task(at=now - timedelta(days=8))
        self._insert_outreach_message(at=now - timedelta(days=7), status="delivered")

        evidence = TOOL.load_evidence(self._dsn)
        dry_run_counts = TOOL.source_counts(evidence)

        touched = TOOL.apply_evidence(self._dsn, evidence)
        evidence_after = TOOL.load_evidence(self._dsn)
        apply_counts = TOOL.source_counts(evidence_after)

        self.assertEqual(dry_run_counts, apply_counts)
        self.assertEqual(touched, 1)
        for source in TOOL.SOURCES:
            self.assertEqual(dry_run_counts[source], 1, source)

    def test_a_failed_outreach_message_older_than_a_success_does_not_win(self) -> None:
        """回填侧的时间升序回放同样兑现"旧成功不覆盖新失败"（反之亦然）。"""
        now = datetime.now(UTC)
        self._insert_outreach_message(
            at=now - timedelta(hours=2), status="failed", last_error="feishu_code_230013"
        )
        self._insert_outreach_message(at=now - timedelta(hours=1), status="delivered")

        evidence = TOOL.load_evidence(self._dsn)
        TOOL.apply_evidence(self._dsn, evidence)

        self.assertEqual(self.store.contact_reachability(open_id=self.open_id), CONTACT_REACHABLE)

    def test_a_failed_outreach_message_newer_than_a_success_wins(self) -> None:
        now = datetime.now(UTC)
        self._insert_outreach_message(at=now - timedelta(hours=2), status="delivered")
        self._insert_outreach_message(
            at=now - timedelta(hours=1), status="failed", last_error="feishu_code_230013"
        )

        evidence = TOOL.load_evidence(self._dsn)
        TOOL.apply_evidence(self._dsn, evidence)

        self.assertEqual(self.store.contact_reachability(open_id=self.open_id), CONTACT_UNAVAILABLE)

    def test_rerunning_after_interruption_does_not_duplicate_or_regress(self) -> None:
        now = datetime.now(UTC)
        self._insert_inbound_event(event_id="evt_bf1", at=now - timedelta(days=5))
        self._insert_inbound_event(event_id="evt_bf2", at=now - timedelta(days=1))

        evidence = TOOL.load_evidence(self._dsn)
        TOOL.apply_evidence(self._dsn, evidence)
        first_first = self._column("first_inbound_at")
        first_last = self._column("last_inbound_at")

        # 代理"中断后重跑"：同一份证据（真实场景是脚本被杀掉后重新执行，从库里
        # 重新读一遍）再回放一次。
        evidence_again = TOOL.load_evidence(self._dsn)
        TOOL.apply_evidence(self._dsn, evidence_again)

        self.assertEqual(self._column("first_inbound_at"), first_first)
        self.assertEqual(self._column("last_inbound_at"), first_last)

    def test_an_orphan_open_id_with_no_app_user_row_is_not_counted(self) -> None:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO inbound_event
                     (feishu_event_id, received_at, event_type, user_open_id, trace_id)
                   VALUES ('evt_orphan', now(), 'im.message.receive_v1', 'ou_ghost', 'trc_orphan')"""
            )

        evidence = TOOL.load_evidence(self._dsn)

        self.assertNotIn("ou_ghost", {item.open_id for item in evidence})


if __name__ == "__main__":
    unittest.main()
