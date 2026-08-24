"""待确认操作：管理员写动作先建待确认事项，本人飞书确认卡片回调后才最多成功执行一次。

Revision ID: 0068_pending_action
Revises: 0067_admin_registry
Create Date: 2026-08-24

[Issue #96](https://github.com/Moshuiwang/lingxi/issues/96) S-M-02，依据 2026-08-24
范围确认评论：本 revision 只交付 MVP 两条写动作（`suspend_user`/`resume_user`）落地
所需的通用待确认操作表；本地抑制、单用户重同步、登记表变更命令等未来消费方复用同一张表，
不新增字段（届时如需要按需迁移）。**本 revision 尚未在任何环境应用过**，批次二审查
修复（外部审查交叉裁定）原地修改本文件，不追加新迁移。

## 为什么"最多成功一次"的真正机制是行锁，不是条件更新的影响行数

[数据库设计「六、管理与待确认操作」](../../../docs/技术设计/数据库设计.md#六管理与待确认操作)
本身写得准确——"不依赖单条 `UPDATE` 语句的影响行数"。**本 revision 之前的这段文件头部
文字与该设计条目不一致**：曾经把机制描述成"条件更新的影响行数：`UPDATE ... WHERE
status='pending' AND expires_at > now() AND initiated_by_open_id=$clicker`，配合
`SELECT ... FOR UPDATE` 先锁行"，但真实实现（`adapters/postgres_pending_action.py`）
从未发出一条这种形态的单条原子语句去**决定**是否执行——`confirm()`/`cancel()` 先
`SELECT ... FOR UPDATE` 锁定待确认操作（`confirm()` 还额外锁定目标 `app_user` 行），
在 Python 里用纯函数 (`decide_confirm`/`decide_cancel`) 读锁定后的行内容做出决策，
通过才执行 `UPDATE`。真正提供"至多成功一次"保证的是**行锁本身**：第二个并发事务的
`SELECT ... FOR UPDATE` 会阻塞到第一个事务提交为止，届时它读到的已经是第一个事务写下
的终态，`decide_confirm`/`decide_cancel` 会在纯函数层面判定 `ALREADY_TERMINAL` 并
拒绝——天然收敛为"返回既有结果"而不是重复执行，不依赖任何应用层分布式锁或"claimed"
中间状态（真库并发用例见 `tests/test_pending_action_postgres.py`）。本节把文件头部的
描述改回与数据库设计文档、与真实实现一致（外部审查交叉裁定，opus P3-2）。

外部审查交叉裁定（opus P3-2）之前，最终的 `UPDATE pending_action SET status = ...
WHERE id = %s` 没有带 `AND status = 'pending'` 条件——本 revision 起补上这道条件更新
守卫，但它是**锁失效场景下的纵深防线**（例如未来代码改动意外弱化了行锁语义），不是
"至多成功一次"本身依赖的机制；即使去掉这道守卫，行锁仍然独立成立这条不变量。

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

## 为什么是 `confirm_deadline_at` 而不是沿用 `expires_at`

外部审查交叉裁定（opus P2-2）：本仓全库的 `expires_at` 是**保留到期**语义——由
`BEFORE INSERT` 触发器写成锚点时间 + 2160 小时（或问数结果正文的 24 小时），到期即
删除或不可逆脱敏（见[数据库设计「九、保留与删除」](../../../docs/技术设计/数据库设计.md#九保留与删除)）。
本表这一列的语义完全相反——十分钟的**动作确认窗口**，到期不删除任何数据，只是让
confirm/cancel 路径把这条待确认操作判定为过期终态。两者同名反义：读者看到
`expires_at` 会默认套用"到期由触发器写入、由 retention 基础设施清理"这条全库惯例，
但本列由应用层 `PENDING_ACTION_TTL_SECONDS` 直接写入、由 confirm/cancel 路径**惰性
判定**（下一节），不接任何 `*_expiry` 保留触发器——安全回归风险是把两套语义误当同一套
维护。改名为 `confirm_deadline_at` 消除这个混淆；本表刻意不适用全库 `*_expiry` 保留
触发器的理由到此为止，不代表本表不受"九十天"上限约束（见文件尾「与九十天保留规则的
关系」一节，那是另一套、真正的保留到期机制，届时若要接入会是一个新增的、独立的
`expires_at`/`retention_expires_at` 列，不会复用本列）。

## 为什么不做本地行的过期后台清理

`confirm_deadline_at` 到期后的失效由 confirm/cancel 路径**惰性判定**（点击时发现已过期
则原地转 `expired` 并拒绝执行）——这是"过期后确认→影响 0 行"这条否定断言需要的最小
机制。主动扫描到期未点击的行并推送卡片终态更新，是本 Story 明确未覆盖的已知缺口（见
PR 描述"未验证事项"）：MVP 有效期设置为十分钟，管理员是这条链路里唯一会点击的人，
未点击的卡片停在可视化"待确认"状态直到下次点击或人工核实，不产生任何业务后果——
一次点击仍会被正确拒绝并转入终态，只是没有人点击时卡片不会自己在飞书里变灰。

## 为什么同一目标同一时刻只允许一条在途待确认操作

外部审查交叉裁定（codex P1-5，ABA）：`prepare()` 此前不检查同一 `target_open_id` 是否
已经有一条 `status='pending'` 的在途行，导致两个怪象——(1) 同一目标可以同时存在多条
`pending` 行；(2) ABA 交错（管理员 A 对某用户发起停用、随后另一路径 resume、旧的 B 卡
此刻仍然有效，再次点击会对一个已经不代表当前状态的快照生效）。本 revision 用**部分
唯一索引**（`pending_action_single_pending_target_idx`，见下）在数据库层面直接堵死：
同一 `target_open_id` 至多一条 `status='pending'` 的行，已终态化的历史行不受约束。
`prepare()` 撞上这条唯一索引时转译为友好拒绝（`target_has_pending_action`），不是让
`IntegrityError` 原样冒泡。

## 与九十天保留规则的关系

[数据库设计「九、保留与删除」](../../../docs/技术设计/数据库设计.md#九保留与删除)
已经把 `pending_action` 列入"+90 days 前清空或删除"的清单——这条规则本 revision 之前
就已经写好，本表建成后自动适用；本 Story 未新增或调整清理调度（不在 #96 S-M-02 范围内，
由既有 retention 基础设施在未来批次接入）。

**已知边界，登记不新建职责（外部审查交叉裁定，opus P2-2 附带项）**：`target_open_id`/
`initiated_by_open_id`/`decided_by_open_id` 三列是身份行（飞书 `open_id` 明文），在
上面这条尚未接入的九十天保留清理落地之前会随本表的行数无限期堆积——这与全库其余表
「身份数据到期即清空或脱敏」的既有原则一致地存在同一缺口，不是本表独有的新问题。本
revision 只登记这条去向（未来接入 retention 基础设施时，`pending_action` 走与
`task.prompt`/`manual_review.detail` 相同的"+90 days 前清空或删除"路径，届时清空的是
整行还是仅脱敏身份列由那一批次决定），不在本 revision 里新建 scheduler 清理职责。
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

    -- CardKit 整卡级 sequence 记账（外部审查交叉裁定，opus P2-1）：确认卡建卡后唯一
    -- 会发生的更新是终态更新（`core/admin/card_callback._update_card_to_terminal`），
    -- 但同一张卡片可能因为回调重投被多次调用 update——CardKit 要求同一 card_id 的
    -- 每次更新调用携带严格递增的 sequence，重复调用必须各自拿到不同的值。这张表按
    -- `PendingActionDeliveryTracker`/`LarkAdminCardTransport` 同样的粒度（一个
    -- pending_action 对应一张卡片）持有这个计数器；建卡（`create`）不消耗它，起始值
    -- 0，每次调用 `PostgresPendingActionStore.next_card_sequence()`（`UPDATE ...
    -- SET card_sequence = card_sequence + 1 RETURNING card_sequence`，单语句原子
    -- 自增）才递增并取号，供 `core/admin/card_callback.py` 在终态更新前换取本次要用
    -- 的 sequence。选型对照：`core/execution/card_stream.CardStream` 把 sequence 存在
    -- 内存里（一个任务一个实例，靠 `task` 表持久化用于 resume）——确认卡回调是无状态
    -- 的 HTTP 处理器，没有等价的"一个实例"可以持有内存计数器，因此选择直接持久化在
    -- 本表而不是复制 `CardStream` 的内存 + resume 模式。
    card_sequence               INTEGER     NOT NULL DEFAULT 0,

    -- 终态原因，供审计与人工核对（例如 role_revoked/target_drifted/card_send_failed/
    -- cancelled_by_admin/expired）；不是面向用户展示的文案本身。渲染进管理群通知前必须
    -- 经过形状白名单（`core/admin/notification.py`），不得原样拼进广播文本。
    reason                     TEXT,
    -- 执行结果的最小必要描述；不落凭据、不落目标资料原值（花名册字段、姓名等）。
    -- **已知边界**：本 revision 的 confirm()/cancel() 均不写入这一列，是为未来消费
    -- 方（本地抑制/单用户重同步/登记表变更等）预留的空列，当前始终为 NULL。
    result                     TEXT,

    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 十分钟动作确认窗口，与全库保留到期语义的 `expires_at` 同名反义——改名为
    -- `confirm_deadline_at`（见文件头部「为什么是 confirm_deadline_at」）。
    confirm_deadline_at        TIMESTAMPTZ NOT NULL,
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

-- 同一目标用户同一时刻只允许一条在途待确认操作（外部审查交叉裁定，codex P1-5，ABA；
-- 见文件头部「为什么同一目标同一时刻只允许一条在途待确认操作」）。部分唯一索引，
-- 已终态化（非 'pending'）的历史行不受约束——同一目标可以有任意多条历史终态行，
-- 只是不能同时有第二条仍在 pending 的。
CREATE UNIQUE INDEX pending_action_single_pending_target_idx
    ON pending_action (target_open_id) WHERE status = 'pending';
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
