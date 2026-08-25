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
from lingxi.adapters.mcp_token_cipher import McpTokenCipher, McpTokenCipherError, new_token
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

    def test_the_database_itself_refuses_a_plaintext_token(self) -> None:
        """`V-权限-11` 的结构性证明：**绕过全部应用层、直接 SQL 也写不进明文**。

        只声明"表里没有明文列"是不够的——``token_cipher`` 本身是可写的裸 ``TEXT``。
        CHECK（标准 base64 字母表 + 长度是 4 的倍数且 ≥ 44）让 43 个字符、URL 安全
        字母表的 ``token_urlsafe(32)`` 明文一定过不去。
        """

        for plaintext in [new_token() for _ in range(8)] + ["明文令牌", "short", "abc="]:
            with self.subTest(length=len(plaintext)):
                with self.assertRaises(Exception):
                    with connect(self._dsn) as connection, connection.cursor() as cursor:
                        cursor.execute(
                            "INSERT INTO mcp_access_token (user_id, token_cipher) VALUES (%s, %s)",
                            (USER_B, plaintext),
                        )
        self.assertIsNone(self.store.token_cipher(USER_B))

    def test_the_check_pins_our_exact_envelope(self) -> None:
        """**G6**：CHECK 钉的是我方签发格式的精确 envelope，不是泛化的"像不像密文"。

        明文恒 43 字符 → 补到 48 字节 → 16B IV + 48B 密文 = 64 字节 → base64 恒 88 字符
        且恒以 ``==`` 结尾。因此半截值、旧口径的 64 字符密文、长度不对的合规 base64 都进不来。
        """

        for _ in range(16):
            self.assertRegex(self.cipher.encrypt(new_token()), r"^[A-Za-z0-9+/]{86}==$")

        rejected = (
            # 长度对但不是我们的格式（旧口径 64 字符：16B IV + 32B 密文）。
            "RklYRURJVjEyMzQ1Njc4OX5gpf2vKqJiLgzu2n4kug1V1rz6DDt1OCgAZVpg1pL+",
            "A" * 87 + "=",  # 87+1：补位数不对
            "A" * 88,  # 长度对、没有 == 结尾
            "A" * 84 + "====",  # 补位过多
            "A" * 85 + "-" + "==",  # URL 安全字母表
            "",
        )
        for value in rejected:
            with self.subTest(length=len(value)):
                with self.assertRaises(Exception):
                    with connect(self._dsn) as connection, connection.cursor() as cursor:
                        cursor.execute(
                            "INSERT INTO mcp_access_token (user_id, token_cipher) VALUES (%s, %s)",
                            (USER_B, value),
                        )
        self.assertIsNone(self.store.token_cipher(USER_B))

    def test_the_check_does_not_prove_the_content_was_encrypted(self) -> None:
        """**诚实边界**：一段恰好 88 字符的合规 base64 文本仍写得进去。

        SQL 层证明的是"不是原样令牌、形状对得上"，**不是**"内容真的经过加密"。内容正确性
        由解密路径负责——写进去解不开的值，读取时会响亮失败，而不是被当成有效令牌放行。
        迁移注释与数据库设计的措辞按这条边界收敛，不宣称超出它能力的事。
        """

        junk = "A" * 86 + "=="
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO mcp_access_token (user_id, token_cipher) VALUES (%s, %s)",
                (USER_B, junk),
            )
        self.assertEqual(self.store.token_cipher(USER_B), junk)
        # 但它解不开——不放行、也不静默重签。
        with self.assertRaises(McpTokenCipherError):
            self.store.read_token(USER_B)
        with self.assertRaises(McpTokenCipherError):
            self.store.issue_token(USER_B)

    def test_the_database_itself_refuses_to_overwrite_a_cipher(self) -> None:
        """签发过的密文**改不掉**：绕过应用层的 UPDATE 会被触发器拒绝。

        覆盖会让库里的密文与已经发布到外部表格的那一份分叉，而更新既有发布行时我们
        不写那一列（`V-权限-11`），新值永远送不出去——用户侧表现为"某天开始问数忽然
        没有权限"。
        """

        issued = self.store.issue_token(USER_A)
        other = self.cipher.encrypt(new_token())
        for column, value in (
            ("token_cipher", other),
            ("user_id", USER_B),
            ("issued_at", NOW),
        ):
            with self.subTest(column=column):
                with self.assertRaises(Exception):
                    with connect(self._dsn) as connection, connection.cursor() as cursor:
                        cursor.execute(
                            f"UPDATE mcp_access_token SET {column} = %s WHERE user_id = %s",
                            (value, USER_A),
                        )
        self.assertEqual(self.store.token_cipher(USER_A), issued.token_cipher)


class TokenAdoptionTest(McpTokenPostgresTestCase):
    """``adopt_token``（Issue #281 改道，Trace #304 批次 3）：`V-开通-24` 的真库半边。

    语义与 ``issue_token`` 完全相同（幂等、绝不覆盖、明文只在内存），下面只覆盖
    ``adopt_token`` 独有的三件事：候选明文来自调用方而不是本模块生成、它与
    ``issue_token`` 共用同一张表时的**跨方法**幂等（库里已经有一份，不管是哪个方法
    签发的，采纳都不会覆盖）、以及它与 ``issue_token`` 共用同一条 ``token_cipher``
    CHECK（`test_adopting_a_secret_with_a_different_shape_is_rejected_by_the_database`
    ——本条**真实撞过一次**：初版测试固件用任意长度字符串当候选明文，被数据库真实拒绝，
    坐实 ``adopt_token`` 文档字符串里"CHECK 依然生效"那句话不是猜测）。其余（触发器
    拒绝覆盖、外键、全库扫描证明明文不落库）已由 ``TokenIssuanceTest`` 通过共用的
    ``_insert_new_token`` 覆盖，不在这里重复断言同一段实现。

    候选明文统一用 :func:`new_token` 生成——与 2026-08-21 #281 改道评论对真实存量令牌的
    实测形状一致（``secrets.token_urlsafe(32)``，43 字符），不是随手一串字母。
    """

    def test_adopt_stores_only_ciphertext_and_returns_the_given_plaintext(self) -> None:
        secret = new_token()
        adopted = self.store.adopt_token(USER_A, secret)
        self.assertTrue(adopted.created)
        self.assertEqual(adopted.reveal(), secret)
        self.assertNotEqual(adopted.token_cipher, secret)
        self.assertEqual(self.cipher.decrypt(adopted.token_cipher), secret)
        self.assertEqual(self._scan_for(secret), [])

    def test_adopt_is_idempotent_and_never_overwrites(self) -> None:
        first_candidate, second_candidate = new_token(), new_token()
        first = self.store.adopt_token(USER_A, first_candidate)
        second = self.store.adopt_token(USER_A, second_candidate)
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.token_cipher, second.token_cipher)
        self.assertEqual(second.reveal(), first_candidate, "库内既有那份优先，候选被丢弃")
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM mcp_access_token WHERE user_id = %s", (USER_A,))
            self.assertEqual(cursor.fetchone()[0], 1)

    def test_adopting_over_an_issued_token_keeps_the_issued_one(self) -> None:
        """跨方法幂等：这个人已经走过 ``issue_token`` 签发，再采纳存量令牌**不覆盖**。"""

        issued = self.store.issue_token(USER_A)
        adopted = self.store.adopt_token(USER_A, new_token())
        self.assertFalse(adopted.created)
        self.assertEqual(adopted.token_cipher, issued.token_cipher)
        self.assertEqual(adopted.reveal(), issued.reveal())

    def test_issuing_after_an_adoption_keeps_the_adopted_one(self) -> None:
        """反向同样成立：先采纳过存量令牌，之后开通链任何路径重新签发都拿到同一份。"""

        legacy_secret = new_token()
        adopted = self.store.adopt_token(USER_A, legacy_secret)
        issued = self.store.issue_token(USER_A)
        self.assertFalse(issued.created)
        self.assertEqual(issued.reveal(), legacy_secret)
        self.assertEqual(issued.token_cipher, adopted.token_cipher)

    def test_adopt_rejects_an_empty_secret(self) -> None:
        with self.assertRaises(ValueError):
            self.store.adopt_token(USER_A, "")
        self.assertIsNone(self.store.token_cipher(USER_A))

    def test_adopt_rejects_a_blank_user_id(self) -> None:
        with self.assertRaises(ValueError):
            self.store.adopt_token("   ", new_token())

    def test_adopt_unknown_user_is_rejected_by_the_foreign_key(self) -> None:
        with self.assertRaises(Exception):
            self.store.adopt_token("usr_不存在", new_token())

    def test_adopting_a_secret_with_a_different_shape_is_rejected_by_the_database(self) -> None:
        """真实撞过一次：``token_cipher`` CHECK 钉的是"明文 43 字符"这一形状
        （迁移 ``0065``），与 ``issue_token`` 共用同一列、同一条约束。**PKCS7 补位到
        16 字节的整数倍**，因此这条 CHECK 实际接受的是「UTF-8 字节数在 32–47 之间」
        的候选明文（都补到 48 字节密文）——本用例特意选一个**远短于**这个区间的
        候选（12 字节），确保真的撞上 CHECK 而不是恰好落进同一个补位桶。加密后的
        信封长度不落在这个区间时，写不进这一列——数据库层面就地拒绝，不会静默存
        一份"形状不对"的密文。"""

        short_secret = "short-secret"  # 12 字节，远短于 32–47 字节的合规区间。
        self.assertLess(len(short_secret.encode("utf-8")), 32)
        with self.assertRaises(Exception):
            self.store.adopt_token(USER_A, short_secret)
        self.assertIsNone(self.store.token_cipher(USER_A), "拒绝的候选不得留下半截行")

    def test_two_users_adopting_get_independent_rows(self) -> None:
        secret_a, secret_b = new_token(), new_token()
        a = self.store.adopt_token(USER_A, secret_a)
        b = self.store.adopt_token(USER_B, secret_b)
        self.assertNotEqual(a.token_cipher, b.token_cipher)
        self.assertEqual(a.reveal(), secret_a)
        self.assertEqual(b.reveal(), secret_b)

    def test_adopted_plaintext_never_appears_in_the_object_repr(self) -> None:
        adopted = self.store.adopt_token(USER_A, new_token())
        self.assertNotIn(adopted.reveal(), repr(adopted))

    def test_wrong_master_key_fails_loudly_on_readback_instead_of_silently_reissuing(self) -> None:
        legacy_secret = new_token()
        self.store.adopt_token(USER_A, legacy_secret)
        other = PostgresMcpTokenStore(self._dsn, cipher=McpTokenCipher(OTHER_MASTER_KEY))
        with self.assertRaises(McpTokenCipherError):
            other.read_token(USER_A)
        # 那一行原封不动——不会被"读不懂就重签"悄悄破坏。
        with self.assertRaises(McpTokenCipherError):
            other.adopt_token(USER_A, new_token())
        self.assertEqual(self.store.read_token(USER_A), legacy_secret)


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

    def test_database_pins_the_exact_shape_of_every_result(self) -> None:
        """五路结论各自的**精确形状**由一条 CHECK 全表达完（与 core 的校验一一对应）。

        只挡"ready 没观察值"是不够的：``waiting`` 带着 ``metric_count=5``、
        ``technical_failure`` 带着观察值，读起来都像"探针跑通了看见了指标"，
        而它们恰恰是"没就绪"。这一组逐条构造这些非法形状，要求数据库全部拒绝。
        """

        illegal = (
            # (result, error_code, metric_count)
            ("ready", "empty_metrics", 3),  # 就绪不得带错误码
            ("ready", None, 0),  # 就绪必须看见 > 0
            ("ready", None, None),  # 就绪必须有观察值
            ("waiting", "empty_metrics", 5),  # 等待中不得声称看见了指标
            ("waiting", None, 0),  # 未就绪必须有错误码
            ("technical_failure", "transport_error", 0),  # 探针没跑通，任何数字都是假的
            ("technical_failure", "transport_error", 3),
            ("technical_failure", None, None),
            ("no_permission", "x", 3),
            ("no_permission", None, None),
            ("timed_out", "budget_exhausted", 0),
            ("timed_out", None, None),
        )
        for index, (result, error_code, metric_count) in enumerate(illegal):
            with self.subTest(result=result, error_code=error_code, metric_count=metric_count):
                with self.assertRaises(Exception):
                    with connect(self._dsn) as connection, connection.cursor() as cursor:
                        cursor.execute(
                            """INSERT INTO mcp_sync_check
                                 (id, user_id, permission_version, attempt_no, result,
                                  error_code, metric_count, content_expires_at)
                               VALUES (%s, %s, 1, 1, %s, %s, %s, now())""",
                            (f"syn_bad{index}", USER_A, result, error_code, metric_count),
                        )
        self.assertEqual(self.store.load_checks(USER_A, 1), ())

    def test_database_rejects_blank_error_codes(self) -> None:
        """**G5**：空串与纯空白满足 ``IS NOT NULL``，却什么都没说明——形状 CHECK 靠它。"""

        for blank in ("", "   ", "\t"):
            with self.subTest(blank=repr(blank)):
                with self.assertRaises(Exception):
                    with connect(self._dsn) as connection, connection.cursor() as cursor:
                        cursor.execute(
                            """INSERT INTO mcp_sync_check
                                 (id, user_id, permission_version, attempt_no, result,
                                  error_code, content_expires_at)
                               VALUES ('syn_blank', %s, 1, 1, 'timed_out', %s, now())""",
                            (USER_A, blank),
                        )

    def test_database_rejects_an_error_code_longer_than_a_code(self) -> None:
        """错误码是**码**不是消息：放宽长度就会有人往里塞异常正文（含凭据与人员资料）。"""

        with self.assertRaises(Exception):
            with connect(self._dsn) as connection, connection.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO mcp_sync_check
                         (id, user_id, permission_version, attempt_no, result,
                          error_code, content_expires_at)
                       VALUES ('syn_long', %s, 1, 1, 'timed_out', %s, now())""",
                    (USER_A, "x" * 201),
                )

    def test_database_accepts_every_legal_shape(self) -> None:
        """对照：五路各自的合法形状都写得进去（否则上面那组证明不了什么）。"""

        legal = (
            ("ready", None, 4),
            ("waiting", "empty_metrics", 0),
            ("waiting", "http_403", None),
            ("technical_failure", "transport_error", None),
            ("no_permission", "no_publishable_permission", None),
            ("timed_out", "budget_exhausted", None),
        )
        for index, (result, error_code, metric_count) in enumerate(legal):
            with connect(self._dsn) as connection, connection.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO mcp_sync_check
                         (id, user_id, permission_version, attempt_no, result,
                          error_code, metric_count, content_expires_at)
                       VALUES (%s, %s, 1, %s, %s, %s, %s, now())""",
                    (f"syn_ok{index}", USER_A, index + 1, result, error_code, metric_count),
                )
        self.assertEqual(len(self.store.load_checks(USER_A, 1)), len(legal))

    def test_attempt_numbering_is_serialised_against_concurrent_writers(self) -> None:
        """取号必须串行化：并发记账不得读到同一个 MAX，各自算出同一个 N+1。

        证明方式是**从另一条连接握住同一把 advisory lock**，然后要求记账在受限的
        ``lock_timeout`` 内失败——只有真的去取那把锁才会等、才会超时。锁释放后照常成功。
        没有这道串行化时，两个并发记账里会有一个撞上 ``UNIQUE`` 中止，而它对应的那次
        探针**已经真的发出去了**，却不会留下任何记录。
        """

        import threading

        key = f"mcp_sync_check:{USER_A}:{VERSION}"
        holding = threading.Event()
        release = threading.Event()

        def hold_the_lock() -> None:
            with connect(self._dsn) as connection:
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))
                    holding.set()
                    release.wait(timeout=30)

        holder = threading.Thread(target=hold_the_lock, daemon=True)
        holder.start()
        try:
            self.assertTrue(holding.wait(timeout=10))
            with self.assertRaises(Exception):
                self.store.record_attempt(_attempt())
        finally:
            release.set()
            holder.join(timeout=10)
        # 锁放开之后照常记账。
        self.store.record_attempt(_attempt())
        self.assertEqual(
            [item.attempt_no for item in self.store.load_checks(USER_A, VERSION)], [1]
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
