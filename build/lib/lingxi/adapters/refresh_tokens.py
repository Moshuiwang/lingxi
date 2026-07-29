"""Bot-Test 受控验证用的飞书用户授权加密续期凭据库。

只持久保存轮换的 refresh_token；user_access_token 只在一次 API 调用中存在。
它是正式开发的参考实现，不属于正式 Lingxi 用户入口。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Protocol
from urllib.request import Request

from lingxi.adapters.oauth_bridge import FeishuOAuthIdentityLoader, OAuthTokenGrant


logger = logging.getLogger(__name__)


class RefreshTokenVault(Protocol):
    def due(self) -> list[tuple[str, OAuthTokenGrant]]: ...
    def replace(self, open_id: str, grant: OAuthTokenGrant) -> None: ...
    def remove(self, open_id: str) -> None: ...


class PostgresRefreshTokenVault:
    """密文在 PostgreSQL，解密密钥只在 biai-stage 的受控环境变量中。"""

    def __init__(self, dsn: str, encryption_key: str) -> None:
        try:
            from cryptography.fernet import Fernet
            import psycopg
        except ImportError as error:  # 绝不降级为明文保存。
            raise RuntimeError("缺少 Bot-Test 加密续期凭据依赖") from error
        try:
            self._cipher = Fernet(encryption_key.encode())
        except Exception as error:
            raise ValueError("LINGXI_OAUTH_REFRESH_TOKEN_KEY 必须是有效的 Fernet 密钥") from error
        self._dsn = dsn
        self._psycopg = psycopg

    @staticmethod
    def _refresh_at(seconds: int) -> datetime:
        # 完全按飞书实际返回的有效期，80% 前刷新；绝不写死有效期。
        return datetime.now(timezone.utc) + timedelta(seconds=max(0, min(seconds - 1, int(seconds * 0.8))))

    def save(self, open_id: str, grant: OAuthTokenGrant) -> None:
        ciphertext = self._cipher.encrypt(grant.refresh_token.encode())
        refresh_at = self._refresh_at(grant.refresh_token_expires_in)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=grant.refresh_token_expires_in)
        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO feishu_user_refresh_token
                    (feishu_open_id, encrypted_refresh_token, scope, refresh_at, refresh_token_expires_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (feishu_open_id) DO UPDATE SET
                      encrypted_refresh_token = EXCLUDED.encrypted_refresh_token,
                      scope = EXCLUDED.scope, refresh_at = EXCLUDED.refresh_at,
                      refresh_token_expires_at = EXCLUDED.refresh_token_expires_at, updated_at = now()""",
                (open_id, ciphertext, grant.scope, refresh_at, expires_at),
            )
        logger.info("OAuth refresh timeline: encrypted credential saved or rotated")

    def due(self) -> list[tuple[str, OAuthTokenGrant]]:
        """先占用记录，避免同一个一次性 refresh_token 被并发使用。"""
        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """WITH picked AS (
                    SELECT feishu_open_id FROM feishu_user_refresh_token
                    WHERE refresh_at <= now() AND refresh_token_expires_at > now()
                    ORDER BY refresh_at FOR UPDATE SKIP LOCKED
                )
                UPDATE feishu_user_refresh_token token SET refresh_at = now() + INTERVAL '5 minutes'
                FROM picked WHERE token.feishu_open_id = picked.feishu_open_id
                RETURNING token.feishu_open_id, token.encrypted_refresh_token, token.scope,
                          EXTRACT(EPOCH FROM token.refresh_token_expires_at - now())::integer"""
            )
            rows = cursor.fetchall()
        results: list[tuple[str, OAuthTokenGrant]] = []
        for open_id, encrypted, scope, seconds in rows:
            try:
                value = self._cipher.decrypt(bytes(encrypted)).decode()
            except Exception:
                self.remove(str(open_id))
                logger.warning("OAuth refresh credential could not be decrypted; authorization removed")
                continue
            results.append((str(open_id), OAuthTokenGrant(value, max(1, int(seconds)), str(scope or ""))))
        return results

    def replace(self, open_id: str, grant: OAuthTokenGrant) -> None:
        self.save(open_id, grant)

    def remove(self, open_id: str) -> None:
        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM feishu_user_refresh_token WHERE feishu_open_id = %s", (open_id,))


class FeishuUserTokenRefresher:
    """每分钟检查续期；结果不明时删除旧凭据，要求用户下次重新授权。"""

    def __init__(self, vault: RefreshTokenVault, app_id: str, app_secret: str, interval_seconds: int = 60) -> None:
        self._vault = vault
        self._app_id = app_id
        self._app_secret = app_secret
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()

    def _refresh(self, refresh_token: str) -> OAuthTokenGrant:
        response = FeishuOAuthIdentityLoader._json(Request(  # noqa: SLF001 -- 复用严格的 JSON 读取器
            "https://open.feishu.cn/open-apis/authen/v2/oauth/token",
            data=json.dumps({"grant_type": "refresh_token", "client_id": self._app_id, "client_secret": self._app_secret, "refresh_token": refresh_token}).encode(),
            headers={"Content-Type": "application/json; charset=utf-8"}, method="POST",
        ))
        token, expires = response.get("refresh_token"), response.get("refresh_token_expires_in")
        if response.get("code") not in (0, "0") or not isinstance(token, str) or not isinstance(expires, int) or expires <= 0:
            raise ValueError("飞书未返回新的续期凭证")
        scope = response.get("scope")
        return OAuthTokenGrant(token, expires, scope if isinstance(scope, str) else "")

    def refresh_due_once(self) -> None:
        due = self._vault.due()
        if due:
            logger.info("OAuth refresh timeline: due credentials claimed for refresh count=%s", len(due))
        for open_id, previous in due:
            try:
                self._vault.replace(open_id, self._refresh(previous.refresh_token))
                logger.info("OAuth refresh timeline: credential refresh completed")
            except Exception as error:
                # refresh_token 单次有效；结果不明也不重放旧凭据。
                self._vault.remove(open_id)
                logger.warning("OAuth refresh failed; authorization removed: %s", type(error).__name__)

    def start(self) -> threading.Thread:
        def run() -> None:
            while not self._stop.is_set():
                self.refresh_due_once()
                self._stop.wait(self._interval_seconds)
        thread = threading.Thread(target=run, name="lingxi-oauth-refresh", daemon=True)
        thread.start()
        logger.info("OAuth refresh timeline: scheduler started interval_seconds=%s", self._interval_seconds)
        return thread

    def stop(self) -> None:
        self._stop.set()
