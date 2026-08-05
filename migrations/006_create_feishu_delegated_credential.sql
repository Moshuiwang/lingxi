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

-- 撤销语义（独立复查后修订）：撤销**清空密文与轮换日程、保留主体行**。
-- app_user 的 V-身份-02 触发器以本表 subject_open_id 为数据来源；若撤销删行，
-- 「凭据失效但组织快照仍在有效期」的窗口里，专用授权账号就能被建成用户记录。
-- 重新授权由 save() 的 upsert 原地补回密文。
CREATE TABLE feishu_delegated_credential (
    purpose                   TEXT PRIMARY KEY
        CHECK (purpose = 'org_directory_sync'),
    subject_open_id           TEXT NOT NULL,
    encrypted_refresh_token   BYTEA,
    scope                     TEXT NOT NULL DEFAULT '',
    issued_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 一次性令牌的消费标记：领取去续期的那一刻置位。此后旧密文对 load() 与
    -- claim_due() 都不可见——飞书接受续期后进程崩溃时，旧令牌已作废，
    -- 任何重放都必然失败并可能触发风控（Codex 复查发现）。save() 写入新
    -- 凭据时清空；超龄未清的消费中行由 revoke_stale_consumed() 收殓。
    consumed_at               TIMESTAMPTZ,
    refresh_at                TIMESTAMPTZ,
    refresh_token_expires_at  TIMESTAMPTZ,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (NULLIF(BTRIM(subject_open_id), '') IS NOT NULL),
    -- 有密文就必须有完整轮换日程，且轮换点严格早于失效点（等于或晚于意味着
    -- 永远等不到轮换）；撤销后的行三者一并为空。
    CHECK (
        (
            encrypted_refresh_token IS NULL
            AND refresh_at IS NULL
            AND refresh_token_expires_at IS NULL
        ) OR (
            octet_length(encrypted_refresh_token) > 0
            AND refresh_at IS NOT NULL
            AND refresh_token_expires_at IS NOT NULL
            AND refresh_at < refresh_token_expires_at
        )
    )
);

-- 续期扫描的领取入口（lingxi-scheduler，FOR UPDATE SKIP LOCKED）。
CREATE INDEX feishu_delegated_credential_due_idx
    ON feishu_delegated_credential (refresh_at);
