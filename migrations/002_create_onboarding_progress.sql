-- #16：授权前的最小、短期、不可逆开通进度。
-- 本表绝不保存飞书身份原文、姓名、权限、令牌或业务消息。
CREATE TABLE onboarding_progress (
    card_nonce       TEXT PRIMARY KEY,
    subject_hash     TEXT NOT NULL,
    chat_hash        TEXT NOT NULL,
    step             TEXT NOT NULL CHECK (step IN ('guide', 'declined', 'authorizing')),
    expires_at       TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '24 hours',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (char_length(subject_hash) = 64),
    CHECK (char_length(chat_hash) = 64)
);

CREATE INDEX onboarding_progress_subject_active_idx
    ON onboarding_progress (subject_hash, expires_at);
