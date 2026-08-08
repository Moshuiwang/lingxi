"""#16 在 PostgreSQL 中保存身份与短期开通进度的最小实现。"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.identity.onboarding import AppUser, IdentityProfile


class PostgresOnboardingStore:
    """进度只存 HMAC，身份只在授权成功后才写入 app_user。"""

    def __init__(
        self,
        dsn: str,
        state_key: str,
        *,
        timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS,
    ) -> None:
        self._dsn = dsn
        self._key = state_key.encode()
        self._timeouts = timeouts

    def _hash(self, value: str) -> str:
        return hmac.new(self._key, value.encode(), hashlib.sha256).hexdigest()

    def _fetchone(self, query: str, parameters: tuple[Any, ...]) -> tuple[Any, ...] | None:
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(query, parameters)
            return cursor.fetchone()

    def _execute(self, query: str, parameters: tuple[Any, ...]) -> None:
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(query, parameters)

    def dismiss_guide(self, open_id: str) -> None:
        self._execute("UPDATE onboarding_progress SET step = 'declined' WHERE subject_hash = %s AND expires_at > now()", (self._hash(open_id),))

    def guide_was_dismissed(self, open_id: str) -> bool:
        return self._fetchone("SELECT 1 FROM onboarding_progress WHERE subject_hash = %s AND step = 'declined' AND expires_at > now() LIMIT 1", (self._hash(open_id),)) is not None

    def clear_dismissed_guide(self, open_id: str) -> None:
        self._execute("DELETE FROM onboarding_progress WHERE subject_hash = %s", (self._hash(open_id),))

    def get_user(self, open_id: str) -> AppUser | None:
        row = self._fetchone(
            "SELECT id, feishu_open_id, feishu_user_id, feishu_union_id, display_name, department, tenant_key, display_name_locale, provisioning_state FROM app_user WHERE feishu_open_id = %s",
            (open_id,),
        )
        if row is None:
            return None
        return AppUser(str(row[0]), IdentityProfile(*[str(value or "") if index in {0, 1, 2, 3} else value for index, value in enumerate(row[1:8])]), str(row[8]))

    def upsert_identity(self, profile: IdentityProfile) -> AppUser:
        user_id = f"usr_{secrets.token_urlsafe(16)}"
        row = self._fetchone(
            """INSERT INTO app_user (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name, department, tenant_key, display_name_locale, provisioning_state)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'matching')
               ON CONFLICT (feishu_open_id) DO UPDATE SET
                 feishu_user_id = EXCLUDED.feishu_user_id, feishu_union_id = EXCLUDED.feishu_union_id,
                 display_name = EXCLUDED.display_name, department = EXCLUDED.department,
                 tenant_key = EXCLUDED.tenant_key, display_name_locale = EXCLUDED.display_name_locale, updated_at = now()
               RETURNING id, provisioning_state""",
            (user_id, profile.open_id, profile.user_id, profile.union_id, profile.display_name, profile.department, profile.tenant_key, profile.display_name_locale),
        )
        assert row is not None
        return AppUser(str(row[0]), profile, str(row[1]))

    def get_authorization_event_user_id(self, event_id: str) -> str | None:
        row = self._fetchone("SELECT app_user_id FROM inbound_event WHERE feishu_event_id = %s", (event_id,))
        return str(row[0]) if row and row[0] else None

    def record_authorization_event(self, event_id: str, user_id: str) -> None:
        self._execute("INSERT INTO inbound_event (feishu_event_id, event_type, app_user_id) VALUES (%s, 'authorization.succeeded', %s) ON CONFLICT DO NOTHING", (event_id, user_id))

    def get_user_by_id(self, user_id: str) -> AppUser | None:
        return None

    def user_count(self) -> int:
        row = self._fetchone("SELECT count(*) FROM app_user", ())
        return int(row[0]) if row else 0

    def saved_business_messages(self) -> tuple[str, ...]:
        return ()


class PostgresCardProgressStore(PostgresOnboardingStore):
    """卡片 nonce 同时绑定原私聊和原点击人，并以 event_id 去重。"""

    def claim_inbound_event(self, event_id: str) -> bool:
        if not event_id:
            return False
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute("INSERT INTO inbound_event (feishu_event_id, event_type) VALUES (%s, 'im.message.receive_v1') ON CONFLICT DO NOTHING RETURNING feishu_event_id", (event_id,))
            return cursor.fetchone() is not None

    def create(self, open_id: str, chat_id: str, step: str) -> str:
        nonce = secrets.token_urlsafe(24)
        subject_hash, chat_hash = self._hash(open_id), self._hash(chat_id)
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT card_nonce FROM onboarding_progress WHERE subject_hash = %s AND chat_hash = %s AND step IN ('authorizing', 'processing') AND expires_at > now() FOR UPDATE",
                (subject_hash, chat_hash),
            )
            active = cursor.fetchone()
            if active is not None:
                return str(active[0])
            cursor.execute("DELETE FROM onboarding_progress WHERE subject_hash = %s AND chat_hash = %s", (subject_hash, chat_hash))
            cursor.execute("INSERT INTO onboarding_progress (card_nonce, subject_hash, chat_hash, step) VALUES (%s, %s, %s, %s)", (nonce, subject_hash, chat_hash, step))
        return nonce

    def valid(self, open_id: str, chat_id: str, nonce: str) -> bool:
        row = self._fetchone("SELECT 1 FROM onboarding_progress WHERE card_nonce = %s AND subject_hash = %s AND chat_hash = %s AND expires_at > now()", (nonce, self._hash(open_id), self._hash(chat_id)))
        return row is not None

    def claim_authorizing_state(self, state: str) -> bool:
        """先原子占用一次性 state，再向飞书换取身份，阻断回调重放。"""
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE onboarding_progress SET step = 'processing' WHERE card_nonce = %s AND step = 'authorizing' AND expires_at > now() RETURNING card_nonce",
                (state,),
            )
            return cursor.fetchone() is not None

    def complete_authorizing_state(self, state: str, open_id: str) -> bool:
        """只在身份与原私聊人相符时，消费已占用的 state。"""
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT subject_hash FROM onboarding_progress WHERE card_nonce = %s AND step = 'processing' AND expires_at > now() FOR UPDATE",
                (state,),
            )
            row = cursor.fetchone()
            if row is None or not hmac.compare_digest(str(row[0]), self._hash(open_id)):
                return False
            cursor.execute("DELETE FROM onboarding_progress WHERE card_nonce = %s", (state,))
            return True

    def cancel_authorizing_state(self, state: str) -> bool:
        row = self._fetchone(
            "DELETE FROM onboarding_progress WHERE card_nonce = %s AND step IN ('authorizing', 'processing') AND expires_at > now() RETURNING card_nonce",
            (state,),
        )
        return row is not None
