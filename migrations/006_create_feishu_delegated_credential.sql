-- #16 S2：「四达文档会议助手」专用组织资料授权的正式凭据表。
--
-- **本迁移替代测试资产 migrations/003_create_feishu_user_refresh_token.sql。**
-- 003 是 Bot-Test 受控验证用的 `feishu_user_refresh_token`，它按 open_id 可保存
-- 任意多条用户凭据；正式实现只存在一条专用授权凭据，因此表名、唯一性和用途
-- 约束都变了。003 仍留在仓库里供受控脚本使用，由验收人在 PR 阶段决定废弃时机。
--
-- 与 003 相比的正式化改动：
--   1. 表名改为 feishu_delegated_credential，明确"这是专用授权，不是员工令牌"；
--   2. 新增 purpose 列并对其建唯一索引：全库最多一条专用授权凭据。测试脚本里
--      "查出来不止一条就报错"的运行期检查（scripts/sync_feishu_org_snapshot.py
--      的 saved_user_credential_not_unique）在这里变成数据库约束；
--   3. 明确记录 issued_at（飞书发放时刻），轮换点 refresh_at 由应用按飞书返回
--      有效期的 80% 计算后写入（见 lingxi.core.identity.credentials）；
--   4. 保留 003 已验证的两条边界：只存密文、不设指向 app_user 的外键。
--
-- 绝不入库：access_token、授权码、App Secret、解密密钥、任何明文凭据。
-- 普通员工不经过 OAuth（2026-07-28 决策），因此本表不为员工保存任何令牌。

CREATE TABLE feishu_delegated_credential (
    purpose                   TEXT PRIMARY KEY
        CHECK (purpose = 'org_directory_sync'),
    subject_open_id           TEXT NOT NULL,
    encrypted_refresh_token   BYTEA NOT NULL,
    scope                     TEXT NOT NULL DEFAULT '',
    issued_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    refresh_at                TIMESTAMPTZ NOT NULL,
    refresh_token_expires_at  TIMESTAMPTZ NOT NULL,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (NULLIF(BTRIM(subject_open_id), '') IS NOT NULL),
    CHECK (octet_length(encrypted_refresh_token) > 0),
    -- 轮换点必须严格早于失效点：等于或晚于就意味着这条凭据永远等不到轮换。
    CHECK (refresh_at < refresh_token_expires_at)
);

-- 续期扫描的领取入口（lingxi-scheduler，FOR UPDATE SKIP LOCKED）。
CREATE INDEX feishu_delegated_credential_due_idx
    ON feishu_delegated_credential (refresh_at);
