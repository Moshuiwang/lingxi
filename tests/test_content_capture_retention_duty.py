"""``innertest_content_capture`` 的九十天到期删除职责（对抗审查 2026-09-02 C-7）。

这张表（迁移 ``0069``）存的是**用户问题原文、模型回答原文与工具调用详情**，是全仓库
内容密度最高的一张表。迁移里 ``expires_at`` 触发器（固定 ``created_at + 2160 小时``）
与到期扫描索引都建好了，却**没有任何调用方**：九十天上限只存在于一个没人读的列里。
这与权限链那三张表当年是同一个形状的缺陷（机制交付了、调用点没接），因此本文件的
断言分层照抄 ``tests/test_permission_retention_duty.py``：

1. **职责本身**（纯逻辑、假适配器）：每轮调一次、停止中一条都不删、失败关闭并留一条
   只含异常类型的审计；
2. **装配**：``build_loop`` 真的把它接进了 ``lingxi-scheduler``——这一层就是缺陷的正身，
   把注册摘掉必须变红；
3. **真库**（有 ``LINGXI_POSTGRES_DSN`` 时）：过期行真的被删、**未过期的一行都不动**。
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lingxi.apps.scheduler import (
    ContentCaptureRetentionDuty,
    SchedulerConfig,
    build_loop,
)

OWNER_ID = "usr_capture_retention"
CONVERSATION_ID = "cnv_capture_retention"
TASK_ID = "tsk_capture_retention"

BASE_ENV = {
    "LINGXI_POSTGRES_DSN": "postgresql://user@localhost:5432/lingxi",
    "LINGXI_FEISHU_APP_ID": "cli_fake",
    "LINGXI_FEISHU_APP_SECRET": "secret_fake",
}


class FakeCaptures:
    def __init__(self, *, purged: int = 0, error: Exception | None = None) -> None:
        self._purged = purged
        self._error = error
        self.calls = 0

    def purge_expired(self) -> int:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._purged


class RecordingAudit:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.entries.append((action, dict(fields)))

    @property
    def actions(self) -> list[str]:
        return [action for action, _ in self.entries]


class DutyBehaviourTest(unittest.TestCase):
    def test_one_call_per_round_and_the_count_is_reported(self) -> None:
        captures = FakeCaptures(purged=7)
        audit = RecordingAudit()
        duty = ContentCaptureRetentionDuty(captures=captures, audit=audit)

        self.assertEqual(duty.run_once(), 7)

        self.assertEqual(captures.calls, 1, "每轮只调一次，不循环到删空")
        self.assertEqual(audit.actions, ["content_capture_retention.completed"])
        self.assertEqual(audit.entries[0][1], {"purged": 7})

    def test_nothing_is_deleted_once_stopping(self) -> None:
        captures = FakeCaptures(purged=3)
        duty = ContentCaptureRetentionDuty(captures=captures, audit=RecordingAudit())
        duty.request_stop()

        self.assertIsNone(duty.run_once(), "停止中返回 None 而不是 0——两者含义不同")
        self.assertEqual(captures.calls, 0)
        self.assertTrue(duty.stopping)

    def test_a_shared_stop_event_stops_this_duty_too(self) -> None:
        stop = threading.Event()
        duty = ContentCaptureRetentionDuty(
            captures=FakeCaptures(), audit=RecordingAudit(), stop=stop
        )

        stop.set()

        self.assertTrue(duty.stopping)

    def test_failure_is_closed_and_the_audit_carries_only_the_exception_type(self) -> None:
        """失败关闭：既不吞也不折成 0。一条被吞掉的异常会让"到期内容一直没被删"
        表现为每轮一条正常的完成审计——保留违规最不该有的形态就是它悄无声息。"""

        secret = "question_content=用户问了一句不该进日志的话"
        captures = FakeCaptures(error=RuntimeError(secret))
        audit = RecordingAudit()
        duty = ContentCaptureRetentionDuty(captures=captures, audit=audit)

        with self.assertRaises(RuntimeError):
            duty.run_once()

        self.assertEqual(audit.actions, ["content_capture_retention.sweep_failed"])
        fields = audit.entries[0][1]
        self.assertEqual(fields["error"], "RuntimeError")
        self.assertEqual(fields["table"], "innertest_content_capture")
        self.assertNotIn(
            "用户问了一句", repr(fields), "异常正文可能带上被删那一行的原文，不得进审计"
        )


@unittest.skipUnless(
    importlib.util.find_spec("psycopg") and importlib.util.find_spec("cryptography"),
    "跳过：build_loop 会真的构造凭据保管与清理适配器，需要 psycopg 与 cryptography",
)
class AssemblyTest(unittest.TestCase):
    """缺陷的正身：机制在、没人调。把注册从 ``build_loop`` 摘掉，这一组立刻变红。"""

    def _config(self) -> SchedulerConfig:
        from cryptography.fernet import Fernet

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = {
            **BASE_ENV,
            "LINGXI_DELEGATED_CREDENTIAL_KEY": Fernet.generate_key().decode(),
            "LINGXI_DELEGATED_CREDENTIAL_PATH": str(Path(directory.name) / "delegated.enc"),
        }
        return SchedulerConfig.from_env(environment)

    def test_the_duty_is_assembled_into_the_scheduler_process(self) -> None:
        loop = build_loop(self._config())
        matching = [
            duty for duty in loop.duties if isinstance(duty, ContentCaptureRetentionDuty)
        ]

        self.assertEqual(len(matching), 1, "内测采集到期删除必须恰好注册一条")
        self.assertIn("内测采集到期删除", [duty.name for duty in loop.duties])
        loop.request_stop()
        self.assertTrue(matching[0].stopping, "一个停止标志贯穿全部职责")

    def test_it_is_wired_unconditionally_and_holds_the_real_adapter(self) -> None:
        """无条件装配、且不是空壳。**没有任何配置前置**：删自己库里的到期内容只
        需要连接串，给它加一个能关掉的开关等于给保留上界加一条旁路。"""

        from lingxi.adapters.postgres_content_capture_retention import (
            PostgresContentCaptureRetention,
        )

        loop = build_loop(self._config())
        (duty,) = [
            item for item in loop.duties if isinstance(item, ContentCaptureRetentionDuty)
        ]

        self.assertIsInstance(duty._captures, PostgresContentCaptureRetention)

    def test_the_purge_adapter_does_not_drag_the_execution_layer_into_scheduler(self) -> None:
        """删除侧刻意与写入侧分成两个模块：写入侧的 ``ContentCaptureRecord`` 会把
        整个 ``core.execution``（工具判定 + 审计脱敏）拉进 scheduler 的 import 闭包。"""

        import ast

        source = (
            Path(__file__).parents[1]
            / "src/lingxi/adapters/postgres_content_capture_retention.py"
        ).read_text(encoding="utf-8")
        imported = {
            node.module
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom) and node.module
        }

        self.assertNotIn("lingxi.core.innertest_content_capture", imported)
        self.assertFalse(
            [name for name in imported if name.startswith("lingxi.core.execution")],
            f"删几行不该把 worker 的执行层拉进 scheduler：{sorted(imported)}",
        )


@unittest.skipUnless(
    os.environ.get("LINGXI_POSTGRES_DSN") and importlib.util.find_spec("psycopg"),
    "跳过：未设置 LINGXI_POSTGRES_DSN 或未安装 psycopg，到期删除的真库断言未验证",
)
class RealDatabaseTest(unittest.TestCase):
    """真库：过期的删掉、未过期的一行都不动。"""

    def setUp(self) -> None:
        from postgres_schema import ensure_production_schema, reset_production_rows

        from lingxi.adapters.postgres import connect

        self._dsn = os.environ["LINGXI_POSTGRES_DSN"]
        ensure_production_schema(self._dsn)
        reset_production_rows(self._dsn)
        self._connect = connect
        self._seed_owner()

    def _seed_owner(self) -> None:
        """种一行合法的 ``app_user`` + 会话 + 任务。

        ``app_user`` 有一条「六个身份字段全有或全无」的 CHECK（迁移 ``008``）：
        ``feishu_open_id``/``feishu_user_id``/``feishu_union_id``/``display_name``/
        ``department``/``tenant_key`` 必须一起给。少给几个不是"字段可空"，是直接
        ``app_user_check`` 违约（本轮 CI 实测）。

        ``email`` **留空**：迁移 ``0085`` 给它加了 ``lower(btrim(email))`` 的部分
        唯一索引，条件是 ``email IS NOT NULL AND btrim(email) <> ''``——不写就落在
        唯一性之外，本用例也不需要邮箱。

        列名照 ``tests/test_postgres_content_capture.py`` 的同一张表夹具，不自造。
        """

        with self._connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO app_user
                     (id, feishu_open_id, feishu_user_id, feishu_union_id,
                      display_name, department, tenant_key, provisioning_state)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 'active')
                   ON CONFLICT (id) DO NOTHING""",
                (
                    OWNER_ID,
                    "ou_capture_retention",
                    "u_capture_retention",
                    "un_capture_retention",
                    "化名甲",
                    "数据部",
                    "tk_capture_retention",
                ),
            )
            cursor.execute(
                """INSERT INTO conversation (id, user_id, feishu_chat_id, feishu_thread_id)
                   VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
                (CONVERSATION_ID, OWNER_ID, "chat_capture_retention", "topic_capture_retention"),
            )
            cursor.execute(
                """INSERT INTO task
                     (id, conversation_id, user_id, inbound_event_id, prompt, status,
                      target_worker_version, attempts, content_expires_at)
                   VALUES (%s, %s, %s, %s, '问题', 'succeeded', 'stable', 1, now())
                   ON CONFLICT (id) DO NOTHING""",
                (TASK_ID, CONVERSATION_ID, OWNER_ID, "event_capture_retention"),
            )
            connection.commit()

    def _insert(self, row_id: str, *, created_at: datetime) -> None:
        with self._connect(self._dsn) as connection, connection.cursor() as cursor:
            # created_at 显式给出；expires_at 由迁移 0069 的触发器按
            # created_at + 2160 小时固定（传什么都会被覆盖），因此"造一条已过期
            # 的行"的唯一正确姿势是把 created_at 推到 90 天以前。
            cursor.execute(
                """INSERT INTO innertest_content_capture
                     (id, task_id, worker_id, question_content, answer_content,
                      created_at, expires_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (row_id, TASK_ID, "wkr", "问题原文", "回答原文", created_at, created_at),
            )
            connection.commit()

    def _ids(self) -> set[str]:
        with self._connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM innertest_content_capture")
            return {row[0] for row in cursor.fetchall()}

    def test_expired_rows_go_and_fresh_rows_stay(self) -> None:
        from lingxi.adapters.postgres_content_capture_retention import (
            PostgresContentCaptureRetention,
        )

        now = datetime.now(UTC)
        self._insert("icc_expired", created_at=now - timedelta(days=91))
        self._insert("icc_fresh", created_at=now - timedelta(days=1))

        purged = PostgresContentCaptureRetention(self._dsn).purge_expired(now=now)

        self.assertEqual(purged, 1)
        self.assertEqual(self._ids(), {"icc_fresh"}, "未到期的一行都不许动")

        # 幂等：再跑一轮什么都不删。
        self.assertEqual(PostgresContentCaptureRetention(self._dsn).purge_expired(now=now), 0)
        self.assertEqual(self._ids(), {"icc_fresh"})

    def test_a_naive_moment_and_a_bad_limit_are_refused_before_any_delete(self) -> None:
        from lingxi.adapters.postgres_content_capture_retention import (
            PostgresContentCaptureRetention,
        )

        store = PostgresContentCaptureRetention(self._dsn)
        with self.assertRaises(ValueError):
            store.purge_expired(now=datetime(2026, 9, 2, 12, 0))
        for bad in (0, -1, True):
            with self.subTest(limit=bad):
                with self.assertRaises(ValueError):
                    store.purge_expired(limit=bad)  # type: ignore[arg-type]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
