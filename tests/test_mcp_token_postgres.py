"""MCP 令牌与就绪确认记录的真库断言（Issue #156 / S-C-02）。

认领断言：`V-权限-11`（**令牌明文不出现在数据库任何列**——本文件用 catalog 反向证明
"没有任何列能放它"，再用全表扫描证明"确实没有放"）、`V-权限-04` / `V-权限-05`
（每次判定独立成行、次序跨轮次连续，是"十五分钟是不是现实上限"这个复审问题的样本来源）。

只有真库能证伪它们：**表里有没有明文列**是 catalog 的属性，**签发幂等**是
``ON CONFLICT DO NOTHING`` 的属性，**结论取值域五路互斥**与**就绪必须看见过指标**是
CHECK 的属性，**次序不可重号**是 ``UNIQUE`` 加取号语句的属性，**到期时间不可篡改**是
触发器的属性——在假 store 上跑，这几条无论实现怎么写都是绿的。

表结构由 ``migrations/alembic/versions/0065_mcp_token_and_sync_check.py`` 建立，测试库
走 ``ensure_production_schema`` 的整条 alembic 链，与生产同源；迁移的逐条真往返由
``scripts/ci/check_migration_chain.sh`` 在 CI 上覆盖，不在这里重复。

**测试主密钥是 biai-agent 加密规格公开的自验向量，非生产密钥。**
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

from postgres_schema import ensure_production_schema, reset_production_rows

from lingxi.adapters.postgres import connect
from lingxi.adapters.mcp_token_cipher import McpTokenCipher, McpTokenCipherError
from lingxi.adapters.postgres_mcp_token import PostgresMcpTokenStore
from lingxi.core.permission.mcp_readiness import (
    ReadinessAttempt,
    ReadinessBinding,
    ReadinessOutcome,
)

SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，MCP 令牌与就绪记录的真库断言未验证（需真实 PostgreSQL 16）"
)

# 规格公开的测试向量主密钥（= ASCII "0123456789abcdef0123456789abcdef"），**非生产密钥**。
SPEC_MASTER_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
OTHER_MASTER_KEY = "enp6enp6enp6enp6enp6enp6enp6enp6enp6enp6eno="

NOW = datetime(2026, 8, 17, 3, 0, tzinfo=timezone.utc)
USER_A = "usr_token_a"
USER_B = "usr_token_b"
EMAIL_A = "jiaming.jia@example.invalid"
EMAIL_B = "yiming.yi@example.invalid"
VERSION = 3


def _attempt(
    user_id: str = USER_A,
    *,
    version: int = VERSION,
    attempt_no: int = 1,
    outcome: ReadinessOutcome = ReadinessOutcome.WAITING,
    error_code: str | None = "empty_metrics",
    metric_count: int | None = 0,
) -> ReadinessAttempt:
    return ReadinessAttempt(
        binding=ReadinessBinding(user_id, version),
        attempt_no=attempt_no,
        outcome=outcome,
        started_at=NOW,
        finished_at=NOW + timedelta(milliseconds=120),
        error_code=error_code,
        metric_count=metric_count,
    )


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN"), SKIP_REASON)
class McpTokenPostgresTestCase(unittest.TestCase):
    """真库断言的共同底座。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        reset_production_rows(self._dsn)
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            for user_id, email in ((USER_A, EMAIL_A), (USER_B, EMAIL_B)):
                cursor.execute(
                    """INSERT INTO app_user
                         (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name,
                          department, tenant_key, email)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        user_id,
                        f"ou_{user_id}",
                        f"fs_{user_id}",
                        f"on_{user_id}",
                        "化名甲" if user_id == USER_A else "化名乙",
                        "测试部门",
                        "tenant-fake",
                        email,
                    ),
                )
        self.cipher = McpTokenCipher(SPEC_MASTER_KEY)
        self.store = PostgresMcpTokenStore(self._dsn, cipher=self.cipher)

    def _columns(self, table: str) -> set[str]:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT column_name FROM information_schema.columns "
                " WHERE table_schema = 'public' AND table_name = %s",
                (table,),
            )
            return {row[0] for row in cursor.fetchall()}

    def _scan_for(self, needle: str) -> list[str]:
        """在**全部生产表的全部文本列**里找这个字符串，返回命中位置。

        这是「明文不落库」的**主动**证明：不是"我们没写"，而是"整个库里找不到"。
        """

        hits: list[str] = []
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT table_name, column_name
                     FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND data_type IN ('text','character varying','jsonb','json')"""
            )
            targets = cursor.fetchall()
            for table, column in targets:
                cursor.execute(
                    f'SELECT count(*) FROM public."{table}" WHERE "{column}"::text LIKE %s',
                    (f"%{needle}%",),
                )
                if cursor.fetchone()[0]:
                    hits.append(f"{table}.{column}")
        return hits


class TokenIssuanceTest(McpTokenPostgresTestCase):
    def test_table_has_no_column_that_could_hold_a_plaintext(self) -> None:
        """`V-权限-11`：明文**没有列可落**（结构性证明，不是"我们记得不写"）。"""

        columns = self._columns("mcp_access_token")
        self.assertEqual(columns, {"user_id", "token_cipher", "issued_at", "created_at"})
        for forbidden in ("token", "token_plain", "plaintext", "secret", "token_hash", "fingerprint"):
            self.assertNotIn(forbidden, columns)

    def test_issue_stores_only_ciphertext(self) -> None:
        issued = self.store.issue_token(USER_A)
        self.assertTrue(issued.created)
        self.assertTrue(issued.reveal())
        self.assertNotEqual(issued.token_cipher, issued.reveal())
        self.assertEqual(self.cipher.decrypt(issued.token_cipher), issued.reveal())
        # 明文在整个库里找不到（全部文本 / JSON 列全扫）。
        self.assertEqual(self._scan_for(issued.reveal()), [])

    def test_plaintext_never_appears_in_the_object_repr(self) -> None:
        issued = self.store.issue_token(USER_A)
        self.assertNotIn(issued.reveal(), repr(issued))

    def test_issue_is_idempotent_and_never_overwrites(self) -> None:
        """幂等：已经发布出去的令牌不能被一次重复签发悄悄换掉。"""

        first = self.store.issue_token(USER_A)
        second = self.store.issue_token(USER_A)
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.token_cipher, second.token_cipher)
        self.assertEqual(first.reveal(), second.reveal())
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM mcp_access_token WHERE user_id = %s", (USER_A,))
            self.assertEqual(cursor.fetchone()[0], 1)

    def test_one_row_per_user_is_structural(self) -> None:
        """主键即 ``user_id``：同一个人第二条令牌在结构上不可表达。"""

        self.store.issue_token(USER_A)
        with self.assertRaises(Exception):
            with connect(self._dsn) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO mcp_access_token (user_id, token_cipher) VALUES (%s, %s)",
                    (USER_A, "另一份密文"),
                )

    def test_two_users_get_different_tokens(self) -> None:
        a = self.store.issue_token(USER_A)
        b = self.store.issue_token(USER_B)
        self.assertNotEqual(a.token_cipher, b.token_cipher)
        self.assertNotEqual(a.reveal(), b.reveal())

    def test_unknown_user_is_rejected_by_the_foreign_key(self) -> None:
        with self.assertRaises(Exception):
            self.store.issue_token("usr_不存在")

    def test_reading_back_round_trips(self) -> None:
        issued = self.store.issue_token(USER_A)
        self.assertEqual(self.store.token_cipher(USER_A), issued.token_cipher)
        self.assertEqual(self.store.read_token(USER_A), issued.reveal())
        self.assertIsNone(self.store.token_cipher(USER_B))
        self.assertIsNone(self.store.read_token(USER_B))

    def test_wrong_master_key_fails_loudly_instead_of_reissuing(self) -> None:
        """解密失败**不放行**、也不静默重签：那会把配置错误变成不可逆的数据破坏。"""

        self.store.issue_token(USER_A)
        other = PostgresMcpTokenStore(self._dsn, cipher=McpTokenCipher(OTHER_MASTER_KEY))
        with self.assertRaises(McpTokenCipherError):
            other.read_token(USER_A)
        with self.assertRaises(McpTokenCipherError):
            other.issue_token(USER_A)
        # 那一行原封不动。
        self.assertEqual(
            self.store.token_cipher(USER_A), self.store.issue_token(USER_A).token_cipher
        )

    def test_deleting_the_user_takes_the_token_with_it(self) -> None:
        self.store.issue_token(USER_A)
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM app_user WHERE id = %s", (USER_A,))
        self.assertIsNone(self.store.token_cipher(USER_A))

    def test_store_refuses_a_raw_key(self) -> None:
        with self.assertRaises(TypeError):
            PostgresMcpTokenStore(self._dsn, cipher=SPEC_MASTER_KEY)  # type: ignore[arg-type]


class SyncCheckRecordTest(McpTokenPostgresTestCase):
    def test_every_attempt_gets_its_own_row(self) -> None:
        for _ in range(3):
            self.store.record_attempt(_attempt())
        checks = self.store.load_checks(USER_A, VERSION)
        self.assertEqual([item.attempt_no for item in checks], [1, 2, 3])
        self.assertEqual({item.result for item in checks}, {"waiting"})

    def test_attempt_number_is_assigned_by_the_database(self) -> None:
        """进程重启后恢复出来的确认从 1 重号，库里的次序必须**继续往下走**。"""

        self.store.record_attempt(_attempt(attempt_no=1))
        self.store.record_attempt(_attempt(attempt_no=2))
        # 一次"重启"：状态机的次序又从 1 开始。
        self.store.record_attempt(_attempt(attempt_no=1))
        self.assertEqual(
            [item.attempt_no for item in self.store.load_checks(USER_A, VERSION)], [1, 2, 3]
        )

    def test_records_are_scoped_to_user_and_version(self) -> None:
        self.store.record_attempt(_attempt(USER_A, version=1))
        self.store.record_attempt(_attempt(USER_A, version=2))
        self.store.record_attempt(_attempt(USER_B, version=1))
        self.assertEqual(len(self.store.load_checks(USER_A, 1)), 1)
        self.assertEqual(len(self.store.load_checks(USER_A, 2)), 1)
        self.assertEqual(len(self.store.load_checks(USER_B, 1)), 1)
        self.assertEqual(self.store.load_checks(USER_B, 2), ())

    def test_ready_row_keeps_its_observation(self) -> None:
        self.store.record_attempt(
            _attempt(outcome=ReadinessOutcome.READY, error_code=None, metric_count=4)
        )
        stored = self.store.load_checks(USER_A, VERSION)[0]
        self.assertEqual(stored.result, "ready")
        self.assertEqual(stored.metric_count, 4)
        self.assertIsNone(stored.error_code)

    def test_database_rejects_a_ready_row_without_metrics(self) -> None:
        """CHECK 层的防线：绕过状态机直接写也建不出"没看见任何指标的就绪"。"""

        with self.assertRaises(Exception):
            with connect(self._dsn) as connection, connection.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO mcp_sync_check
                         (id, user_id, permission_version, attempt_no, result, content_expires_at)
                       VALUES ('syn_x', %s, 1, 1, 'ready', now())""",
                    (USER_A,),
                )

    def test_database_rejects_an_unknown_result(self) -> None:
        with self.assertRaises(Exception):
            with connect(self._dsn) as connection, connection.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO mcp_sync_check
                         (id, user_id, permission_version, attempt_no, result, content_expires_at)
                       VALUES ('syn_y', %s, 1, 1, 'confirmed', now())""",
                    (USER_A,),
                )

    def test_database_rejects_observations_on_non_probe_results(self) -> None:
        for result in ("no_permission", "timed_out"):
            with self.subTest(result=result):
                with self.assertRaises(Exception):
                    with connect(self._dsn) as connection, connection.cursor() as cursor:
                        cursor.execute(
                            """INSERT INTO mcp_sync_check
                                 (id, user_id, permission_version, attempt_no, result,
                                  metric_count, content_expires_at)
                               VALUES ('syn_z', %s, 1, 1, %s, 3, now())""",
                            (USER_A, result),
                        )

    def test_expiry_is_derived_and_cannot_be_moved(self) -> None:
        self.store.record_attempt(_attempt())
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT started_at, content_expires_at FROM mcp_sync_check WHERE user_id = %s",
                (USER_A,),
            )
            started, expires = cursor.fetchone()
        # 调用方传的是 now()，触发器一律改写成 started_at + 2160 小时。
        self.assertEqual(expires - started, timedelta(hours=2160))

    def test_anchors_cannot_be_rewritten(self) -> None:
        self.store.record_attempt(_attempt())
        for column, value in (
            ("started_at", NOW + timedelta(days=1)),
            ("user_id", USER_B),
            ("permission_version", VERSION + 1),
            ("attempt_no", 9),
        ):
            with self.subTest(column=column):
                with self.assertRaises(Exception):
                    with connect(self._dsn) as connection, connection.cursor() as cursor:
                        cursor.execute(
                            f"UPDATE mcp_sync_check SET {column} = %s WHERE user_id = %s",
                            (value, USER_A),
                        )

    def test_records_carry_no_person_data(self) -> None:
        """这张表**没有任何可识别内容列**：只有内部 ULID、版本、次序、时间、结论与错误码。"""

        columns = self._columns("mcp_sync_check")
        self.assertEqual(
            columns,
            {
                "id",
                "user_id",
                "permission_version",
                "attempt_no",
                "started_at",
                "finished_at",
                "result",
                "error_code",
                "metric_count",
                "content_expires_at",
            },
        )
        self.assertNotIn("detail", columns)

    def test_purge_removes_only_expired_rows(self) -> None:
        self.store.record_attempt(_attempt())
        self.assertEqual(self.store.purge_expired_checks(now=NOW), 0)
        self.assertEqual(len(self.store.load_checks(USER_A, VERSION)), 1)
        self.assertEqual(self.store.purge_expired_checks(now=NOW + timedelta(days=91)), 1)
        self.assertEqual(self.store.load_checks(USER_A, VERSION), ())

    def test_deleting_the_user_takes_the_checks_with_it(self) -> None:
        self.store.record_attempt(_attempt())
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM app_user WHERE id = %s", (USER_A,))
        self.assertEqual(self.store.load_checks(USER_A, VERSION), ())

    def test_record_refuses_foreign_objects(self) -> None:
        with self.assertRaises(TypeError):
            self.store.record_attempt({"user_id": USER_A})  # type: ignore[arg-type]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
