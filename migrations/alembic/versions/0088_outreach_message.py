"""主动发送的发送记录：一张卡发给谁、发的哪一版内容、何时、结果如何（Issue #586）。

Revision ID: 0088_outreach_message
Revises: 0087_preprovision_seams
Create Date: 2026-09-05

主动发出去的消息**不可撤回**，因此这张表要同时承担两件事：让"发给谁、发了什么、
何时、成功与否"可回查，以及让"同一份名单重跑不再发第二次"结构性成立。

## 为什么是新表，不复用 `onboarding_completion_notice`

那张表（`0066`）的每一列都绑死在一种通知上：`permission_version` 非空、
`company_name`/`function_name` 是那句话的两个占位变量、去重键的语义是「同一用户同一版
权限只发一条」。主动发送是**通用能力**（给定收件人 + 一张卡片就能发），第一个调用方
才是预开通告知；把它塞进那张表要么让那三列对新用途失去意义（写占位值），要么改动一张
已经在生产跑着的通知表的约束。两者的重试语义也不同：那张表是常驻 outbox（认领即退避、
到期自动重发），这张表是 ops 脚本按名单驱动的记录（重试由人再跑一次 `--apply` 触发）。

## 幂等锚点是 `dedupe_key` 的唯一约束

`dedupe_key` 由「内容键 + 用途 + 收件人」拼成（`core/outreach/dispatch.outreach_dedupe_
key`）。正式发送的收件人位是 `user_id`，因此同一份名单重跑落在同一个键上：`INSERT ...
ON CONFLICT (dedupe_key)` 命中已经 `delivered` 的行时**一行不写、一条不发**。重试用同一个
`dedupe_key`，出站适配器据此折出同一个平台去重 `uuid`，飞书侧因此仍然只有一条消息。
预检的收件人位额外带一个本次运行号：产品负责人要按样式反复预检，把预检也钉死成"一生
一次"等于把 D-1 的定稿路径堵上。

## 不做九十天到期，因为**这张表本身就是幂等性事实**

`content_expires_at` 那一套（`0065`/`0066`/`0069` 的形状）在这里是错的：删掉一条已送达
的记录，等于让下一次 `--apply` 对同一个人重新发一张欢迎卡——这正是本表要堵的那件事。
按数据库设计第九节「当前状态与历史内容分开」，它归入 `app_user`、`mcp_access_token`
那一类**继续服务所需的当前状态**，不按九十天删除。它也确实没有可识别内容可擦：没有
姓名、没有邮箱、没有卡片正文，只有 `open_id`（与 `app_user.feishu_open_id` 同一标识、
同为不按九十天删除的当前状态）、内容键与版本、样式、结果与错误码。用户触发删除仍然
成立：`user_id` 上的 `ON DELETE CASCADE` 让账号删除编排一并带走属于他的记录。

`content_version` 兼作审计需要的 `content_digest`：`content.lock.toml` 已经把版本号与
整份内容目录的 sha256 绑定（`check_content_version.py` 判红），另算一份哈希只会多一处
会漂移的事实。

## 不授任何表权限

与 `0059`/`0064`/`0065`/`0066` 同型：四个数据库角色的表级授权当前只在 `0054` 里针对它
自己那两张表出现，运行时进程也尚未以这些角色连库。

``downgrade()`` 真实可执行：表是本 revision 新建的，不存在需要还原的历史行。
"""

from __future__ import annotations

from alembic import op

revision: str = "0088_outreach_message"
down_revision: str | None = "0087_preprovision_seams"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
CREATE TABLE outreach_message (
    id                 TEXT        PRIMARY KEY,            -- ULID, omr_*
    -- 真正决定这条消息去了哪里的就是这一列（出站接口按 open_id 投递），因此它必填，
    -- 而不是从 user_id 现查——记录必须能独立回答"发给谁"，包括收件人不是本系统
    -- 用户的预检。
    recipient_open_id  TEXT        NOT NULL CHECK (recipient_open_id <> ''),
    -- 可空：预检发给管理员本人，他不一定是 app_user 里的一行。CASCADE：账号删除
    -- 编排删掉那个人时，属于他的发送记录一并消失。
    user_id            TEXT        REFERENCES app_user(id) ON DELETE CASCADE,
    -- 预检不算正式送达（Issue #586 第四节），因此它是一列事实，不是一个可以从别处
    -- 推断出来的属性。
    purpose            TEXT        NOT NULL CHECK (purpose IN ('apply', 'precheck')),
    -- 发的是哪一张卡的哪一版文案。版本号即内容摘要（见文件头）。
    content_key        TEXT        NOT NULL CHECK (content_key <> ''),
    content_version    TEXT        NOT NULL CHECK (content_version <> ''),
    -- D-1 定稿前样式会变，记录里必须留下"这个人当时收到的是哪一版样式"。
    card_style         TEXT        NOT NULL CHECK (card_style <> ''),
    status             TEXT        NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'delivered', 'failed')),
    attempts           INT         NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error         TEXT,
    -- 平台回读标识，L4a 双通道核对用（平台 API 回读 + 服务端记录）。取不到时留空，
    -- 不伪造。
    message_id         TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at       TIMESTAMPTZ,

    -- 幂等锚点：同一张卡、同一种用途、同一个收件人只有一行（见文件头）。
    dedupe_key         TEXT        NOT NULL UNIQUE,

    -- delivered_at 是"已送达"的唯一标记，不允许出现在其它状态上——一条 failed 却
    -- 带着送达时间的行会让回查读成已经送达。
    CHECK (delivered_at IS NULL OR status = 'delivered'),
    CHECK (status <> 'delivered' OR delivered_at IS NOT NULL)
);

-- 回查（`scripts/ops/outreach.py --list`）按时间倒序翻页，不扫全表。
CREATE INDEX outreach_message_recent_idx ON outreach_message (created_at DESC, id);

-- "这个人收到过哪些主动发送"：账号删除编排与逐人回查都走这条。
CREATE INDEX outreach_message_user_idx ON outreach_message (user_id)
    WHERE user_id IS NOT NULL;

-- created_at / dedupe_key / purpose 一经写入不可改：它们是幂等键与"这一条是不是正式
-- 送达"的锚点，改写任一项都等于伪造历史。与 0066 的同型触发器一个理由。
CREATE OR REPLACE FUNCTION outreach_message_freeze_anchors() RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION '不允许修改发送记录的创建时间';
    END IF;
    IF NEW.dedupe_key IS DISTINCT FROM OLD.dedupe_key THEN
        RAISE EXCEPTION '不允许修改发送记录的去重键';
    END IF;
    IF NEW.purpose IS DISTINCT FROM OLD.purpose THEN
        RAISE EXCEPTION '不允许把预检记录改写成正式送达';
    END IF;
    IF OLD.status = 'delivered' AND NEW.status <> 'delivered' THEN
        RAISE EXCEPTION '已送达的发送记录不允许退回其它状态';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER outreach_message_freeze
    BEFORE UPDATE ON outreach_message
    FOR EACH ROW EXECUTE FUNCTION outreach_message_freeze_anchors();
"""

_DOWNGRADE_SQL = r"""
DROP TABLE IF EXISTS outreach_message;
DROP FUNCTION IF EXISTS outreach_message_freeze_anchors();
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与既有 revision 同型：直接使用 psycopg cursor 执行 DDL，不走 ``op.execute()``。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
