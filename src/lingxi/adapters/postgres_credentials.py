"""专用授权凭据的加密存取（PostgreSQL + Fernet）。

吸收 `src/lingxi/adapters/refresh_tokens.py`（Bot-Test 测试资产）已经受控验证过的
四条模式，按 `migrations/006` 的正式表重写：

1. **只有密文落库**，解密密钥来自受控环境变量，绝不入库；
2. 缺加密依赖时**构造期就失败**，绝不降级为明文保存；
3. 领取到期凭据用 ``FOR UPDATE SKIP LOCKED`` 并**立即把轮换点挪后**——
   ``refresh_token`` 一次性有效，同一条被并发领取两次等于把它用废；
4. 解密失败、续期失败或结果不明确一律撤销（清空密文），不重放。

轮换点怎么算是领域规则，在 :mod:`lingxi.core.identity.credentials`，本模块只负责
把它写进数据库。

与测试资产的差别：003 的 ``feishu_user_refresh_token`` 按 ``open_id`` 可存任意多条
用户凭据；正式表 006 用 ``purpose`` 做主键，全库最多一条专用授权凭据。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from lingxi.core.identity.credentials import (
    AuthorizationGrant,
    SecretToken,
    expiry_moment,
    rotation_deadline,
)
from lingxi.core.identity.identifiers import redact_identifier

logger = logging.getLogger(__name__)

# 006 里唯一允许的用途。新增用途要走迁移，不能靠调用方传字符串。
DELEGATED_PURPOSE = "org_directory_sync"

# 领取后把轮换点挪后的租期。挪后是为了防并发重复使用同一个一次性凭据；
# 上限被数据库的 CHECK (refresh_at < refresh_token_expires_at) 夹住。
DEFAULT_LEASE_SECONDS = 300


@dataclass(frozen=True)
class StoredCredential:
    subject_open_id: str
    grant: AuthorizationGrant
    refresh_at: datetime
    expires_at: datetime


class PostgresDelegatedCredentialVault:
    def __init__(self, dsn: str, encryption_key: str) -> None:
        try:
            from cryptography.fernet import Fernet
            import psycopg
        except ImportError as error:  # 绝不降级为明文保存。
            raise RuntimeError("缺少专用授权凭据的加密依赖") from error
        try:
            self._cipher = Fernet(encryption_key.encode())
        except Exception as error:
            raise ValueError("专用授权凭据的加密密钥必须是有效的 Fernet 密钥") from error
        self._dsn = dsn
        self._psycopg = psycopg

    # ---- 写入 -------------------------------------------------------------

    def save(self, *, subject_open_id: str, grant: AuthorizationGrant, issued_at: datetime | None = None) -> None:
        """写入或轮换唯一一条专用授权凭据。成功续期后的第一项持久化动作就是它。"""

        moment = issued_at or datetime.now(timezone.utc)
        ciphertext = self._cipher.encrypt(grant.refresh_token.reveal().encode())
        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO feishu_delegated_credential
                     (purpose, subject_open_id, encrypted_refresh_token, scope,
                      issued_at, refresh_at, refresh_token_expires_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (purpose) DO UPDATE SET
                     subject_open_id = EXCLUDED.subject_open_id,
                     encrypted_refresh_token = EXCLUDED.encrypted_refresh_token,
                     scope = EXCLUDED.scope,
                     issued_at = EXCLUDED.issued_at,
                     refresh_at = EXCLUDED.refresh_at,
                     refresh_token_expires_at = EXCLUDED.refresh_token_expires_at,
                     consumed_at = NULL,
                     updated_at = now()""",
                (
                    DELEGATED_PURPOSE,
                    subject_open_id,
                    ciphertext,
                    grant.scope,
                    moment,
                    rotation_deadline(moment, grant.refresh_token_expires_in),
                    expiry_moment(moment, grant.refresh_token_expires_in),
                ),
            )
        logger.info("专用授权凭据已加密写入 subject=%s", redact_identifier(subject_open_id))

    def revoke(self, *, reason: str) -> bool:
        """清空密文与轮换日程，但保留主体行。

        撤销不能删行：`app_user` 的 V-身份-02 触发器以本表的 `subject_open_id`
        为数据来源，删行等于在「凭据失效、组织快照仍在有效期」的窗口里拆掉那道
        绕不过去的防线（独立复查发现）。凭据侧的效果与删除等价——`load()` 与
        `claim_due()` 都只认有密文的行；重新授权由 `save()` 的 upsert 原地补回。
        """

        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE feishu_delegated_credential
                      SET encrypted_refresh_token = NULL,
                          refresh_at = NULL,
                          refresh_token_expires_at = NULL,
                          updated_at = now()
                    WHERE purpose = %s AND encrypted_refresh_token IS NOT NULL""",
                (DELEGATED_PURPOSE,),
            )
            removed = cursor.rowcount > 0
        if removed:
            logger.warning("专用授权凭据已撤销 reason=%s", reason)
        return removed

    def revoke_stale_consumed(self, *, max_age_seconds: int = 600, now: datetime | None = None) -> bool:
        """收殓「已消费但一直没写回新凭据」的行。

        这种行意味着进程在续期后、落库前死掉：旧令牌已被飞书作废，留着密文
        只会诱使未来的代码路径重放它。清空并留下与普通失败可区分的日志，
        由人工重新授权（Codex 复查发现的崩溃窗口）。"""

        moment = now or datetime.now(timezone.utc)
        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE feishu_delegated_credential
                      SET encrypted_refresh_token = NULL,
                          refresh_at = NULL,
                          refresh_token_expires_at = NULL,
                          consumed_at = NULL,
                          updated_at = now()
                    WHERE purpose = %(purpose)s
                      AND encrypted_refresh_token IS NOT NULL
                      AND consumed_at IS NOT NULL
                      AND consumed_at < %(cutoff)s""",
                {
                    "purpose": DELEGATED_PURPOSE,
                    "cutoff": moment - timedelta(seconds=max_age_seconds),
                },
            )
            cleared = cursor.rowcount > 0
        if cleared:
            logger.error("不可恢复：发现续期后未落库的消费中凭据，已清空，需人工重新授权")
        return cleared

    # ---- 读取 -------------------------------------------------------------

    def load(self, *, now: datetime | None = None) -> StoredCredential | None:
        """取出当前凭据供同步使用。解密失败或已失效时撤销并返回 ``None``。"""

        moment = now or datetime.now(timezone.utc)
        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT subject_open_id, encrypted_refresh_token, scope, refresh_at, refresh_token_expires_at
                     FROM feishu_delegated_credential
                    WHERE purpose = %s AND encrypted_refresh_token IS NOT NULL
                      AND consumed_at IS NULL""",
                (DELEGATED_PURPOSE,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        credential = self._decrypt(row, moment)
        if credential is None:
            return None
        if credential.expires_at <= moment:
            self.revoke(reason="credential_expired")
            return None
        return credential

    def claim_due(self, *, lease_seconds: int = DEFAULT_LEASE_SECONDS, now: datetime | None = None) -> StoredCredential | None:
        """领取一条到期待轮换的凭据，并立刻把轮换点挪后。

        ``FOR UPDATE SKIP LOCKED``：另一个 scheduler 正在处理时直接返回空，
        而不是排队等待——排队等于两个进程先后拿到同一个一次性凭据。
        """

        moment = now or datetime.now(timezone.utc)
        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """WITH picked AS (
                       SELECT purpose FROM feishu_delegated_credential
                        WHERE purpose = %(purpose)s
                          AND encrypted_refresh_token IS NOT NULL
                          AND consumed_at IS NULL
                          AND refresh_at <= %(now)s
                          AND refresh_token_expires_at > %(now)s
                        FOR UPDATE SKIP LOCKED
                   )
                   UPDATE feishu_delegated_credential credential
                      SET consumed_at = %(now)s::timestamptz,
                          refresh_at = LEAST(
                              %(now)s::timestamptz + make_interval(secs => %(lease)s),
                              credential.refresh_token_expires_at - INTERVAL '1 second'
                          )
                     FROM picked
                    WHERE credential.purpose = picked.purpose
                RETURNING credential.subject_open_id, credential.encrypted_refresh_token, credential.scope,
                          credential.refresh_at, credential.refresh_token_expires_at""",
                {"purpose": DELEGATED_PURPOSE, "now": moment, "lease": lease_seconds},
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return self._decrypt(row, moment)

    # ---- 内部 -------------------------------------------------------------

    def _decrypt(self, row: tuple[Any, ...], moment: datetime) -> StoredCredential | None:
        subject_open_id, ciphertext, scope, refresh_at, expires_at = row
        try:
            plaintext = self._cipher.decrypt(bytes(ciphertext)).decode()
        except Exception:
            # 解不开的密文不是"待重试"，是"这条凭据已经用不了了"。
            self.revoke(reason="credential_undecryptable")
            logger.warning("专用授权凭据无法解密，已撤销 subject=%s", redact_identifier(subject_open_id))
            return None
        remaining = int((expires_at - moment).total_seconds())
        if remaining <= 0:
            self.revoke(reason="credential_expired")
            return None
        return StoredCredential(
            subject_open_id=str(subject_open_id),
            grant=AuthorizationGrant(SecretToken(plaintext), remaining, str(scope or "")),
            refresh_at=refresh_at,
            expires_at=expires_at,
        )
