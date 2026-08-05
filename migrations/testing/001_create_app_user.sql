-- #16：统一用户记录的身份与开通进度基础。
-- 权限匹配、发布和问数由后续切片补充；本迁移不保存任何令牌或凭据。
CREATE TABLE app_user (
    id                      TEXT PRIMARY KEY,
    feishu_open_id          TEXT UNIQUE,
    feishu_user_id          TEXT,
    feishu_union_id         TEXT,
    display_name            TEXT,
    display_name_locale     TEXT,
    department              TEXT,
    tenant_key              TEXT,
    provisioning_state      TEXT NOT NULL DEFAULT 'guest'
        CHECK (provisioning_state IN (
            'guest', 'authorizing', 'matching', 'manual_review',
            'provisioning', 'mcp_syncing', 'active', 'aborted'
        )),
    account_state           TEXT NOT NULL DEFAULT 'enabled'
        CHECK (account_state IN ('enabled', 'suspended', 'deleting', 'deleted')),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (
            NULLIF(BTRIM(feishu_open_id), '') IS NULL
            AND NULLIF(BTRIM(feishu_user_id), '') IS NULL
            AND NULLIF(BTRIM(feishu_union_id), '') IS NULL
            AND NULLIF(BTRIM(display_name), '') IS NULL
        ) OR (
            NULLIF(BTRIM(feishu_open_id), '') IS NOT NULL
            AND NULLIF(BTRIM(feishu_user_id), '') IS NOT NULL
            AND NULLIF(BTRIM(feishu_union_id), '') IS NOT NULL
            AND NULLIF(BTRIM(display_name), '') IS NOT NULL
        )
    )
);

CREATE INDEX app_user_provisioning_state_pending_idx
    ON app_user (provisioning_state)
    WHERE provisioning_state <> 'active';

-- 飞书授权回调的幂等键。重复 event_id 必须返回首次身份结果，不能再次建档。
CREATE TABLE inbound_event (
    feishu_event_id  TEXT PRIMARY KEY,
    received_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_type       TEXT NOT NULL,
    user_open_id     TEXT,
    app_user_id      TEXT REFERENCES app_user(id),
    expires_at       TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '90 days'
);

CREATE OR REPLACE FUNCTION record_authorized_identity(
    callback_event_id TEXT,
    new_user_id TEXT,
    new_open_id TEXT,
    new_feishu_user_id TEXT,
    new_union_id TEXT,
    new_display_name TEXT,
    new_department TEXT,
    new_tenant_key TEXT,
    new_display_name_locale TEXT
) RETURNS TEXT
LANGUAGE plpgsql
AS $$
DECLARE
    recorded_user_id TEXT;
BEGIN
    INSERT INTO inbound_event (feishu_event_id, event_type, user_open_id)
    VALUES (callback_event_id, 'authorization.succeeded', new_open_id)
    ON CONFLICT DO NOTHING;

    SELECT app_user_id
      INTO recorded_user_id
      FROM inbound_event
     WHERE feishu_event_id = callback_event_id
     FOR UPDATE;

    IF recorded_user_id IS NOT NULL THEN
        RETURN recorded_user_id;
    END IF;

    INSERT INTO app_user (
        id, feishu_open_id, feishu_user_id, feishu_union_id, display_name,
        department, tenant_key, display_name_locale, provisioning_state
    ) VALUES (
        new_user_id, new_open_id, new_feishu_user_id, new_union_id, new_display_name,
        new_department, new_tenant_key, new_display_name_locale, 'matching'
    )
    ON CONFLICT (feishu_open_id) DO UPDATE SET
        feishu_user_id = EXCLUDED.feishu_user_id,
        feishu_union_id = EXCLUDED.feishu_union_id,
        display_name = EXCLUDED.display_name,
        department = EXCLUDED.department,
        tenant_key = EXCLUDED.tenant_key,
        display_name_locale = EXCLUDED.display_name_locale,
        updated_at = now()
    RETURNING id INTO recorded_user_id;

    UPDATE inbound_event
       SET app_user_id = recorded_user_id
     WHERE feishu_event_id = callback_event_id;

    RETURN recorded_user_id;
END;
$$;
