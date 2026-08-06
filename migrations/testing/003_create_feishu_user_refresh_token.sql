-- #19 Bot-Test 受控验证：用户明确同意 offline_access 后，保存可轮换 refresh_token 的密文。
-- user_access_token、授权码与解密密钥绝不入库。
CREATE TABLE feishu_user_refresh_token (
    feishu_open_id            TEXT PRIMARY KEY,
    encrypted_refresh_token   BYTEA NOT NULL,
    scope                     TEXT NOT NULL DEFAULT '',
    refresh_at                TIMESTAMPTZ NOT NULL,
    refresh_token_expires_at  TIMESTAMPTZ NOT NULL,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (refresh_at < refresh_token_expires_at)
);

CREATE INDEX feishu_user_refresh_token_due_idx
    ON feishu_user_refresh_token (refresh_at);
