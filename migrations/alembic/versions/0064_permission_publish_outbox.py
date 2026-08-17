"""权限发布意图 outbox：权限决定与「要发布什么」在同一个事务里落库。

Revision ID: 0064_permission_publish_outbox
Revises: 0063_roster_snapshot
Create Date: 2026-08-17

[Issue #156](https://github.com/Moshuiwang/lingxi/issues/156) 的 S-C-01（Epic C 首个
Story）。数据库设计[「四、开通与发布」](../../../docs/技术设计/数据库设计.md#四开通与发布)
冻结的 outbox 语义在这条 revision 落地。

**为什么必须是 outbox 而不是「改完权限直接调飞书」**：后者在飞书调用失败时会留下
「数据库已改、发布未做」的静默不一致，而产品合同要求两者不一致时**不得视为发布完成**。
把发布意图与权限决定放进同一个事务，让「决定了要发布」这件事和「权限变成了这一版」
一起成立或一起不成立——回滚之后库里不会剩下一条孤立的发布意图（`V-权限-01`）。

## 与数据库设计原文的四处差异（本 revision 是权威，设计文档已同步）

1. **``id`` 是 ULID ``TEXT`` 而不是 ``BIGSERIAL``**：与 ``0057``–``0063`` 建的每一张
   表一致（接口设计「二、通用约定」：内部主键一律 ULID）。设计文档写 ``BIGSERIAL`` 是
   蓝本期的遗留，序列号会把「这个部署发布过多少次权限」变成一个能从主键上读出来的数字。
2. **多了 ``superseded`` 状态**：旧版本的发布意图被更新版本取代时，它既不是「发布成功」
   也不是「失败」。挤进 ``failed`` 会让告警把一次正常的版本推进报成故障；挤进
   ``published`` 则是彻底的假话——那一版根本没写出去。
3. **多了 ``UNIQUE (user_id, permission_version)``**：同一用户同一权限版本只允许一条
   发布意图（Issue #156 范围第 4 条「相同权限版本幂等」）。没有这条约束，一次重复的
   权限决定就会排出两条内容完全相同的意图，两个消费者各写一次外部表格。
4. **多了 ``claimed_at`` / ``last_outcome`` / ``external_record_id``**：分别服务
   「``publishing`` 卡住多久了、能不能放回 ``pending``」「上一次是哪一类失败」
   「写到了外部哪一行」。前两个是重启恢复与告警分类的唯一依据，第三个让读回核对
   与人工排查不必再查一次外部表格。

## ``payload`` 存的是**内容快照**，不是引用

``payload`` 是当初决定发布的那一整行文本字段（``record_key``/``email``/``name``/
``permissions``/``status``/``updated_at``，形状与序列化格式见
``src/lingxi/core/permission/publish_row.py``）。重试因此发布的一定是当初那一版，
不会被后续变更污染，也不需要在重试时重新聚合一次权限。

**``token_cipher`` 不在 payload 里，也永远不会被写进发布表**：它是业务侧既有字段，
名字表明它承载凭据，而产品合同与 Issue #156 范围第 8 条都要求凭据不入 outbox 与日志。

## 九十天保留：``content_expires_at`` + 应用层擦除

``payload`` 里有邮箱与姓名，属于**可识别个人数据**，因此按数据库设计第九节走九十天
上限：触发器把 ``content_expires_at`` 固定为 ``created_at + 2160 hours``，调用方传什么
都会被覆盖；到期后由 ``adapters/postgres_permission_publish.py`` 的
``redact_expired_payloads`` 把 ``payload`` 擦成 ``'{}'`` 并清掉 ``last_error``，只留下
「哪个用户的哪一版权限在什么时候发布成功过」这类**不可再映射到人**的运行事实。

**为什么是应用层擦除而不是进 ``0054`` 的受限清理函数**：那个函数是 ``SECURITY DEFINER``
+ 专用属主角色的高治理面，当前只覆盖 ``galaxy_import_batch`` / ``feishu_org_sync_run``
两张表；把新表挂进去要动角色与授权面，属独立的治理变更。与 ``0059``
（``task_delivery_event``）当时的取舍一致：先把**真正含个人数据的那一列**做出可验证的
到期擦除，行本身的物理删除路径与角色升级留给同一个后续治理 Story。差别是本表比
``0059`` 更进一步——它真的有一条到期擦除路径，而不只是登记留白。

行本身还有一条**已经存在**的删除路径：``user_id`` 上的 ``ON DELETE CASCADE``。账号删除
编排删掉 ``app_user`` 那一行时，该用户的全部发布意图一并消失，不需要第二次清理动作。

## 不授任何表权限

与 ``0059``/``0061``/``0063`` 同型：四个数据库角色的表级授权当前只在 ``0054`` 里针对
它自己那两张表出现，运行时进程也尚未以这些角色连库（当前能力已登记）。这里凭空授权会
造出一份与实际连接身份对不上的授权面。

``downgrade()`` 真实可执行：表与触发器函数都是本 revision 新建的，按依赖反序整体删除，
不存在需要还原的历史行。
"""

from __future__ import annotations

from alembic import op

revision: str = "0064_permission_publish_outbox"
down_revision: str | None = "0063_roster_snapshot"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
CREATE TABLE publish_outbox (
    id                 TEXT        PRIMARY KEY,            -- ULID, pub_*
    -- CASCADE：账号删除编排删掉 app_user 那一行时，该用户的发布意图（含 payload 里的
    -- 邮箱与姓名）一并消失。留着它们等于在一张"已删除用户"的表外面另存一份可识别数据。
    user_id            TEXT        NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    -- 发布的是哪一版。0 是 app_user 的初始值（还没有过任何权限决定），因此意图上恒为正。
    permission_version BIGINT      NOT NULL CHECK (permission_version > 0),
    -- 为什么产生这条意图（首次开通 / 每日刷新 / 人工重发……）。只进审计，不含正文。
    reason             TEXT        NOT NULL CHECK (BTRIM(reason) <> ''),
    -- 内容快照，见文件头部。擦除后为 '{}'，因此不能加"非空对象"的 CHECK。
    payload            JSONB       NOT NULL,
    status             TEXT        NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','publishing','published','failed','superseded')),
    attempts           INT         NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    -- 上一次尝试的结果分类（published/mismatch/rejected/uncertain/conflict/invalid/
    -- superseded）。不加 CHECK：取值域由应用层的 PublishOutcome 定义，在数据库里再抄
    -- 一份会制造第二个真相来源（同 task.error_kind 的既有取舍）。
    last_outcome       TEXT,
    last_error         TEXT,
    -- 写到了外部表格的哪一行。外部记录标识不是人员资料，可长期保留。
    external_record_id TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at         TIMESTAMPTZ,
    published_at       TIMESTAMPTZ,
    content_expires_at TIMESTAMPTZ NOT NULL,               -- 触发器固定为 created_at + 2160 小时

    -- 同一用户同一权限版本只有一条意图（Issue #156 范围第 4 条）。
    UNIQUE (user_id, permission_version),
    -- published_at 是"这一版权限被证明写进了外部表格"的唯一标记，不允许出现在
    -- 任何其它状态上：一条 failed 却带着发布时间的行会让下游读成发布成功。
    CHECK (published_at IS NULL OR status = 'published'),
    CHECK (status <> 'published' OR published_at IS NOT NULL)
);

-- 认领查询按 created_at 取最早的 pending。
CREATE INDEX publish_outbox_pending_idx
    ON publish_outbox (created_at, id)
    WHERE status = 'pending';

-- 「同一用户单飞」的判据索引：认领候选必须是该用户**最老的非终态意图**，因此要按
-- (user_id, created_at, id) 找"有没有更早的 pending/publishing 兄弟行"。
--
-- 判据不是"有没有 publishing"：READ COMMITTED 下，另一个消费者尚未提交的认领对本
-- 事务不可见（它读到的仍是提交态的 pending），那条判据会放行同一用户的更新版本，
-- 两个消费者同时对外发布、谁后写谁生效——用户侧就是"已经收回的权限被静默恢复"。
-- 覆盖 pending 与 publishing 两态，正是为了让未提交窗口里的 pending 也挡得住。
CREATE INDEX publish_outbox_active_idx
    ON publish_outbox (user_id, created_at, id)
    WHERE status IN ('pending', 'publishing');

-- 滞留回收的扫描键：只按 claimed_at 找卡住的 publishing，不带 user_id 前缀
-- （回收是全表按时间扫，不是按用户查）。
CREATE INDEX publish_outbox_reclaim_idx
    ON publish_outbox (claimed_at)
    WHERE status = 'publishing';

-- 到期擦除的扫描键。不加谓词：已擦除的行也要能被"还没到期的行有多少"这类核对读到，
-- 而一条谓词里带 payload 比较的部分索引会随每次 payload 更新重建索引项。
CREATE INDEX publish_outbox_content_expiry_idx
    ON publish_outbox (content_expires_at);

-- 与 0057/0058/0059 同型：到期时间由来源时间推导，调用方传什么都会被覆盖；
-- created_at / user_id / permission_version 一经写入不可改——它们是幂等键与
-- "发布的是哪一版"的锚点，改写任一项都等于伪造历史。
CREATE OR REPLACE FUNCTION publish_outbox_fix_expiry() RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.content_expires_at := NEW.created_at + INTERVAL '2160 hours';
    IF TG_OP = 'UPDATE' THEN
        IF NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION '不允许修改发布意图的创建时间';
        END IF;
        IF NEW.user_id IS DISTINCT FROM OLD.user_id THEN
            RAISE EXCEPTION '不允许修改发布意图所属的用户';
        END IF;
        IF NEW.permission_version IS DISTINCT FROM OLD.permission_version THEN
            RAISE EXCEPTION '不允许修改发布意图的权限版本';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER publish_outbox_expiry
    BEFORE INSERT OR UPDATE ON publish_outbox
    FOR EACH ROW EXECUTE FUNCTION publish_outbox_fix_expiry();
"""

_DOWNGRADE_SQL = r"""
DROP TABLE IF EXISTS publish_outbox;
DROP FUNCTION IF EXISTS publish_outbox_fix_expiry();
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与 0057/0058/0059/0060/0061/0062/0063 同型：不走 ``op.execute()``，避免空参数集
    触发插值模式（本段 DDL 的 ``RAISE EXCEPTION`` 文案将来若加 ``%`` 占位符会被拒绝）。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
