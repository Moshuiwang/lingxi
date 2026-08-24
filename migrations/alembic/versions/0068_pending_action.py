"""待确认操作：管理员写动作先建待确认事项，本人飞书确认卡片回调后才最多成功执行一次。

Revision ID: 0068_pending_action
Revises: 0067_admin_registry
Create Date: 2026-08-24

[Issue #96](https://github.com/Moshuiwang/lingxi/issues/96) S-M-02，依据 2026-08-24
范围确认评论：本 revision 只交付 MVP 两条写动作（`suspend_user`/`resume_user`）落地
所需的通用待确认操作表；本地抑制、单用户重同步、登记表变更命令等未来消费方复用同一张表，
不新增字段（届时如需要按需迁移）。

## 为什么"最多成功一次"不需要分布式锁

[数据库设计「六、管理与待确认操作」](../../../docs/技术设计/数据库设计.md#六管理与待确认操作)
定的机制是条件更新的影响行数：`UPDATE ... WHERE status='pending' AND expires_at > now()
AND initiated_by_open_id=$clicker`，配合 `SELECT ... FOR UPDATE` 先锁行，第二次并发确认
在同一事务序列化下只能读到第一次已经写下的终态，天然收敛为"返回既有结果"而不是重复执行
（真实实现见 `adapters/postgres_pending_action.py`，真库并发用例见
`tests/test_pending_action_postgres.py`）。本表因此不需要任何应用层分布式锁或额外的
"claimed"中间状态。

## 为什么是 `target_state_snapshot`（字符串快照）而不是整数版本号

`app_user.permission_version` 是权限发布链路的乐观锁锚点，与本表的漂移检测不是同一件事：
`suspend_user`/`resume_user` 关心的是 `account_state` 这个五取值之一的字段本身有没有在
prepare 到 confirm 之间被别的路径改变，不是权限版本。直接快照 `account_state` 的字符串
取值，confirm 时重读比对，比引入第二套版本号更直接，也更贴合"目标状态已经变化"这句合同
原文——变化指的就是这个字段的值变了，不是一个单调计数器。

## 为什么 `card_delivered` 是布尔列而不是从 `card_id IS NOT NULL` 推断

送卡片这一步允许"建卡成功但发送失败"这类结果不明的中间态（与
`core/execution/card_stream.py` 的 `DeliveryRejected` 白名单同一姿态）：`card_id` 有值
不代表这张卡片真的作为消息送达了管理员私聊。`card_delivered` 是应用层在真正确认发送
成功后才置真的独立标记，`card_id` 只是"如果送成功了，用哪个 ID 去更新"这条数据本身，
两者语义不同，合一列会让"建卡成功但发送状态不明"无处安放。

## 为什么不做本地行的过期后台清理

`expires_at` 到期后的失效由 confirm/cancel 路径**惰性判定**（点击时发现已过期则原地
转 `expired` 并拒绝执行）——这是"过期后确认→影响 0 行"这条否定断言需要的最小机制。
主动扫描到期未点击的行并推送卡片终态更新，是本 Story 明确未覆盖的已知缺口（见 PR
描述"未验证事项"）：MVP 有效期设置为十分钟，管理员是这条链路里唯一会点击的人，
未点击的卡片停在可视化"待确认"状态直到下次点击或人工核实，不产生任何业务后果——
一次点击仍会被正确拒绝并转入终态，只是没有人点击时卡片不会自己在飞书里变灰。

## 与九十天保留规则的关系

[数据库设计「九、保留与删除」](../../../docs/技术设计/数据库设计.md#九保留与删除)
已经把 `pending_action` 列入"+90 days 前清空或删除"的清单——这条规则本 revision 之前
就已经写好，本表建成后自动适用；本 Story 未新增或调整清理调度（不在 #96 S-M-02 范围内，
由既有 retention 基础设施在未来批次接入）。
"""

from __future__ import annotations

from alembic import op

revision: str = "0068_pending_action"
down_revision: str | None = "0067_admin_registry"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
CREATE TABLE pending_action (
    id                        TEXT        PRIMARY KEY,            -- ULID, pac_*

    -- MVP 两条写动作；未来批次（本地抑制/单用户重同步/登记表变更）新增时按普通迁移
    -- 追加取值，不需要改表结构。
    action_type               TEXT        NOT NULL
        CHECK (action_type IN ('suspend_user', 'resume_user')),

    -- 目标用户的飞书身份锚点，与 admin_registry/app_user 的既有判定同一惯例
    -- （只用 open_id，不用内部 ULID——命令面的 <标识> 参数本就是 open_id）。
    target_open_id            TEXT        NOT NULL
        CHECK (NULLIF(BTRIM(target_open_id), '') IS NOT NULL),

    -- prepare() 时刻读到的 app_user.account_state，confirm() 时重读比对；不一致即
    -- "目标状态已经变化"，一律不执行（见文件头部「为什么是 target_state_snapshot」）。
    target_state_snapshot     TEXT        NOT NULL,

    -- 发起该操作的管理员飞书身份（= 命中 admin_registry 判定时的 feishu_open_id）。
    initiated_by_open_id      TEXT        NOT NULL
        CHECK (NULLIF(BTRIM(initiated_by_open_id), '') IS NOT NULL),

    -- 'pending' 是唯一非终态；其余四个都是终态，一旦写入不可再变。
    status                     TEXT        NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'executed', 'cancelled', 'expired', 'failed')),

    -- 卡片是否已确认发送成功；FALSE 视为整个待确认操作作废，confirm/cancel 一律拒绝
    -- 且行为与"未找到"不可区分（见文件头部）。
    card_delivered             BOOLEAN     NOT NULL DEFAULT FALSE,
    card_id                    TEXT,

    -- 终态原因，供审计与人工核对（例如 role_revoked/target_drifted/card_send_failed/
    -- cancelled_by_admin/expired）；不是面向用户展示的文案本身。
    reason                     TEXT,
    -- 执行结果的最小必要描述；不落凭据、不落目标资料原值（花名册字段、姓名等）。
    result                     TEXT,

    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at                 TIMESTAMPTZ NOT NULL,
    decided_at                 TIMESTAMPTZ,
    decided_by_open_id         TEXT,

    -- 终态必须带 decided_at；仍在 pending 的行不得有 decided_at——两种自相矛盾的行
    -- 都在数据库层面拒绝，不依赖应用层每次都记得同步写两个字段。
    CHECK ((status = 'pending') = (decided_at IS NULL))
);

-- 供未来"查询本人发起的待确认操作"复用（当前批次未提供该查询命令，索引先行不占用
-- 额外迁移窗口）；也是 confirm/cancel 路径按 initiated_by_open_id 过滤时的天然索引。
CREATE INDEX pending_action_initiator_idx ON pending_action (initiated_by_open_id, status);

-- 供未来"该用户当前是否有在途待确认操作"复用；当前批次的 confirm 路径按主键
-- （待确认操作 id）访问，本索引不是热路径必需，但符合数据库设计原则 1（尽量落约束/
-- 索引而非只靠应用代码自觉）。
CREATE INDEX pending_action_target_idx ON pending_action (target_open_id);
"""

#: 数据破坏操作，与 0067 同型：一旦部署环境写入过真实待确认操作，DROP 会把它们
#: 一并清空，不是无损回滚。
_DOWNGRADE_SQL = r"""
DROP TABLE IF EXISTS pending_action;
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与 0057–0067 同型：不走 ``op.execute()``，避免空参数集触发插值模式。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
