"""Bot-Test 续期凭据库的真实 PostgreSQL 测试；不访问飞书。"""

from __future__ import annotations

import os
from pathlib import Path
import unittest

from lingxi.adapters.oauth_bridge import OAuthTokenGrant


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN"), "仅在 CI 或本机受控 PostgreSQL 中运行")
class RefreshTokenPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        cls._psycopg = psycopg
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]
        migration = (Path(__file__).parents[1] / "migrations/003_create_feishu_user_refresh_token.sql").read_text()
        with psycopg.connect(cls._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS feishu_user_refresh_token")
            cursor.execute(migration)

    def setUp(self) -> None:
        from cryptography.fernet import Fernet
        from lingxi.adapters.refresh_tokens import PostgresRefreshTokenVault

        self._key = Fernet.generate_key().decode()
        self.vault = PostgresRefreshTokenVault(self._dsn, self._key)

    def _row(self) -> tuple[bytes, str]:
        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT encrypted_refresh_token, scope FROM feishu_user_refresh_token WHERE feishu_open_id = %s",
                ("ou_probe",),
            )
            row = cursor.fetchone()
        self.assertIsNotNone(row)
        return bytes(row[0]), str(row[1])  # type: ignore[index]

    def test_encrypted_credential_is_replaced_then_removed(self) -> None:
        self.vault.save("ou_probe", OAuthTokenGrant("refresh-first", 3600, "offline_access"))
        first_ciphertext, first_scope = self._row()
        self.assertNotIn(b"refresh-first", first_ciphertext)
        self.assertEqual(first_scope, "offline_access")

        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE feishu_user_refresh_token SET refresh_at = now() - interval '1 minute'")
        due = self.vault.due()
        self.assertEqual(len(due), 1)
        open_id, grant = due[0]
        self.assertEqual(open_id, "ou_probe")
        self.assertEqual(grant.refresh_token, "refresh-first")
        self.assertEqual(grant.scope, "offline_access")
        self.assertGreater(grant.refresh_token_expires_in, 0)

        self.vault.replace("ou_probe", OAuthTokenGrant("refresh-second", 3600, "offline_access"))
        second_ciphertext, _ = self._row()
        self.assertNotEqual(first_ciphertext, second_ciphertext)
        self.assertNotIn(b"refresh-second", second_ciphertext)

        self.vault.remove("ou_probe")
        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM feishu_user_refresh_token")
            self.assertEqual(cursor.fetchone()[0], 0)
