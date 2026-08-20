"""迟到就绪恢复的「开通完成」通知 outbox：状态推进与待发通知同一个事务落库。

Revision ID: 0066_onboarding_notice_outbox
Revises: 0065_mcp_token_and_sync_check
Create Date: 2026-08-20

外部独立审查（codex）F1 坐实的缺口：`V-开通-18` 的迟到就绪恢复职责若只把
`provisioning_state` 推进到 `active`、再在同一次调用里同步发一次通知，任何一种中途失败
（CAS 后进程崩溃、收件人查询异常、渲染异常、飞书发送失败）都会让用户**永久退出候选集
却收不到「开通完成」**——「active 但永远不告知」与「永远不 active」对用户来说是同一种
失败：他仍然收不到那句话。

## 为什么必须同一个事务，不能先推进状态、再另起一次写入排通知

验收矩阵 `V-开通-18` 断言的是「就绪就写 active 并通知」，这是一件事的两个面，不是先后两件事：状态已
经推进却排不出通知，等于对用户撒了一半的谎（数据库说他能用了，他自己却不知道）；通知
排出去了状态却没推进，等于给一个还没真正开通完成的人发错误的确认。与 `0064` 里
`publish_outbox` 的立论完全同构（"权限决定"与"发布意图"必须一起成立或一起不成立），
这里是"状态推进"与"通知排出"必须一起成立或一起不成立——把两条 SQL 语句放进同一个数据库
事务，此刻起两者要么都发生、要么都没发生。

## 通知本身是一张**独立的 outbox**，靠持久重试保证最终送达

`onboarding_completion_notice` 只有一种正常生命周期：`pending`（排出但还没送达）→
`delivered`（送达，终态）。第三态 `skipped` 是唯一的旁路终态——推进到 `active` 之后，
收件人查询查不到 `open_id`（账号在极短窗口内又被停用或删除），这个人已经没有"收件人"这
个概念了，无限重试没有意义，因此登记一个可分辨的终态而不是永远 `pending`。

**没有 `sending`/`publishing` 这类中间锁定态**（与 `publish_outbox` 的三态不同）：认领与
处理在同一次调用里背靠背发生（不像发布消费与决策是分开的两步），因此不存在"认领了却还没
处理完就崩溃"的独立窗口需要专门的态来标记；认领步骤本身（`attempts + 1` 与
`next_attempt_at` 前移）已经把重试节奏钉住，一次崩溃最坏只是让这一条在下一次到期时被重新
认领，不会丢也不会重复送达（幂等靠稳定的 `dedupe_key`，由适配器折成飞书投递的去重键）。

## 退避：认领本身推进下一次到期时间

`attempts` 每次认领 +1，`next_attempt_at` 同时前移到
`now() + LEAST(attempts * 300, 3600)` 秒（5 分钟起步，封顶一小时）——认领这一步本身就是
退避的落点，不需要另一次写入去"记一次失败"。这与 `publish_outbox_pending_idx` 的既有形状
同构：**认领即记账**，避免"没更新时间戳的失败候选立刻被下一轮重新捞起"（外部独立审查
F4 在恢复候选查询里点名的同一类问题，这里在通知 outbox 上同样成立）。

## 九十天到期：与 `mcp_sync_check` 同一形状，不延后到"后续 Story"

本表不含邮箱或姓名（`company_name`/`function_name` 是发布出去的公司编号与职能标签的展示
文本，来自 `payload` 快照渲染，不是人员资料本身），但仍然是一张会无限增长的运行记录表，
按数据库设计第九节的通用纪律做九十天到期：`content_expires_at` 由触发器固定为
`created_at + 2160 hours`，`purge_expired_notices` 删除已到期且已收口（`delivered` /
`skipped`）的整行——**未收口（`pending`）的行永远不到期删除**，即使触发器已经给它算出了
一个 `content_expires_at`：删掉一条还在等待送达的通知，等于让一个已经写成 `active` 的
用户真的永远收不到那句话，这正是本迁移要堵住的那个洞。做法与 `mcp_sync_check`
（`0065`）同型：没有可识别列可擦，到期即整行删除，而不是像 `publish_outbox`
（`0064`）那样把某一列擦成占位符。

## 不授任何表权限

与 `0059`/`0061`/`0063`/`0064`/`0065` 同型：四个数据库角色的表级授权当前只在 `0054`
里针对它自己那两张表出现，运行时进程也尚未以这些角色连库。

``downgrade()`` 真实可执行：表与触发器函数都是本 revision 新建的，按依赖反序整体删除，
不存在需要还原的历史行。
"""

from __future__ import annotations

from alembic import op

revision: str = "0066_onboarding_notice_outbox"
down_revision: str | None = "0065_mcp_token_and_sync_check"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
CREATE TABLE onboarding_completion_notice (
    id                 TEXT        PRIMARY KEY,            -- ULID, obn_*
    -- CASCADE：账号删除编排删掉 app_user 那一行时，待发通知一并消失——没有收件人的
    -- 通知没有意义。
    user_id            TEXT        NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    -- 通知对应哪一版权限：只用来渲染审计与排障，不参与去重（去重键是独立的一列）。
    permission_version BIGINT      NOT NULL CHECK (permission_version > 0),
    -- 渲染「开通完成」文案要用的两个占位变量，创建时一次性快照（与 publish_outbox
    -- 的 payload 同一条道理：重试送出的必须是当初决定的那一份，不随后续权限变化漂移，
    -- 也不依赖一份可能已经被 0064 到期擦除的 publish_outbox.payload）。不是人员资料
    -- （邮箱/姓名），是这个人可用范围的展示文本（公司编号、职能标签）。
    company_name       TEXT        NOT NULL,
    function_name      TEXT        NOT NULL,
    status             TEXT        NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'delivered', 'skipped')),
    attempts           INT         NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error         TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 下一次可以被认领的时刻。认领步骤本身把它前移（见文件头部「退避」一节），
    -- 因此这一列既是"到期判据"也是"重试节流"，不需要第二套机制。
    next_attempt_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at       TIMESTAMPTZ,
    content_expires_at TIMESTAMPTZ NOT NULL,               -- 触发器固定为 created_at + 2160 小时

    -- 同一用户同一版权限只应该有一条待发通知：这就是幂等键，适配器插入时
    -- ON CONFLICT (dedupe_key) DO NOTHING，因此"推进 active"这一步无论重试多少次，
    -- 通知只会被排出一次。
    dedupe_key         TEXT        NOT NULL UNIQUE,

    -- delivered_at 是"这条通知已经送达"的唯一标记，不允许出现在其它状态上——一条
    -- pending/skipped 却带着送达时间的行会让下游读成已经送达。
    CHECK (delivered_at IS NULL OR status = 'delivered'),
    CHECK (status <> 'delivered' OR delivered_at IS NOT NULL)
);

-- 认领查询的扫描键：只取到期的 pending，按创建时间给早排出的人优先权。
CREATE INDEX onboarding_completion_notice_due_idx
    ON onboarding_completion_notice (next_attempt_at, created_at, id)
    WHERE status = 'pending';

-- 到期删除的扫描键：只覆盖已收口的两态，pending 永远不出现在这个索引要照顾的查询里
-- （purge 的 WHERE 子句本身也把 pending 排除在外，索引只是让"哪些已收口的行到期了"
-- 这个问题不用扫全表）。
CREATE INDEX onboarding_completion_notice_purge_idx
    ON onboarding_completion_notice (content_expires_at)
    WHERE status IN ('delivered', 'skipped');

-- 与 0057/0058/0059/0064/0065 同型：到期时间由来源时间推导，调用方传什么都会被覆盖；
-- created_at / user_id / permission_version / dedupe_key 一经写入不可改——它们是幂等键
-- 与"通知对应谁的哪一版"的锚点，改写任一项都等于伪造历史。
CREATE OR REPLACE FUNCTION onboarding_completion_notice_fix_expiry() RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.content_expires_at := NEW.created_at + INTERVAL '2160 hours';
    IF TG_OP = 'UPDATE' THEN
        IF NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION '不允许修改待发通知的创建时间';
        END IF;
        IF NEW.user_id IS DISTINCT FROM OLD.user_id THEN
            RAISE EXCEPTION '不允许修改待发通知所属的用户';
        END IF;
        IF NEW.permission_version IS DISTINCT FROM OLD.permission_version THEN
            RAISE EXCEPTION '不允许修改待发通知对应的权限版本';
        END IF;
        IF NEW.dedupe_key IS DISTINCT FROM OLD.dedupe_key THEN
            RAISE EXCEPTION '不允许修改待发通知的去重键';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER onboarding_completion_notice_expiry
    BEFORE INSERT OR UPDATE ON onboarding_completion_notice
    FOR EACH ROW EXECUTE FUNCTION onboarding_completion_notice_fix_expiry();
"""

_DOWNGRADE_SQL = r"""
DROP TABLE IF EXISTS onboarding_completion_notice;
DROP FUNCTION IF EXISTS onboarding_completion_notice_fix_expiry();
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与 0057/0058/0059/0060/0061/0062/0063/0064/0065 同型：不走 ``op.execute()``，
    避免空参数集触发插值模式（本段 DDL 的 ``RAISE EXCEPTION`` 文案将来若加 ``%``
    占位符会被拒绝）。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
