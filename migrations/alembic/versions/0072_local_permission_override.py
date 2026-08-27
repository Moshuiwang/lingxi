"""本地权限覆盖表：管理员经确认卡对个别用户补授（grant）或收窄（suppress）的
公司×指标级权限，供每日权限重算/开通链聚合与银河翻译结果取并集再减集。

Revision ID: 0072_local_permission_override
Revises: 0071_daily_report_watermark
Create Date: 2026-08-27

[Issue #319](https://github.com/Moshuiwang/lingxi/issues/319)（产品负责人
2026-08-26 裁定，推翻 [2026-08-24 决策记录](../../../docs/决策记录/2026-08-24-管理员职责集与银河外权限动作边界.md)
第 4 条「本地开通/扩权：不做」）的 S-P-1a：本 revision 只交付**表结构本身**。
纯函数语义（去重/冲突判定）在 :mod:`lingxi.core.permission.local_override`；
读写适配器在 :mod:`lingxi.adapters.postgres_local_permission`；**命令面（管理员
如何发起一笔授权/收回）与聚合点接线均不在本 revision 范围**，分别留给 S-P-1b
与 S-P-3。

## 为什么是「一张表双极性」，不是 grant/suppress 两张表（编排者裁定，覆盖 #319 字面）

[#319](https://github.com/Moshuiwang/lingxi/issues/319) 正文把「本地授权」（本
Issue 新增）与「本地抑制」（2026-08-24 决策记录已经采纳、但从未建表）并列描述，
字面上容易读成两个独立机制。但两者在聚合语义上是同一枚硬币的两面——真实权限
`= (银河翻译 ∪ 本地授权) − 本地抑制`，抑制优先级最高——且共享**完全相同的形状**
（目标用户 + 公司 + 指标名 + 原因 + 发起人 + 确认卡留痕 + 时间戳），唯一的区别是
一个 `direction` 取值。拆两张表会让「同一 user×公司×指标 同时有一条 grant 与一条
suppress」这种需要合并判定的情形（`suppress` 赢）被迫写成一次跨表 UNION 查询与
两套几乎相同的 CRUD，而这本该是同一张表按 `direction` 分区的一个 `WHERE` 条件。
本迁移因此采纳单表双极性；后续实现如与 #319 字面出现更细的分歧，以本迁移与
`docs/决策记录/2026-08-24-管理员职责集与银河外权限动作边界.md` 的同步修订为准。

## 为什么 `pending_action_id` 是 `NOT NULL`（结构化地堵死「无确认卡写入」）

[#319](https://github.com/Moshuiwang/lingxi/issues/319)「可观察完成标准」第二条：
"非管理员/未经确认卡的任何路径无法写入本地权限（默认拒绝+审计，变异验红）"。
如果这条只由应用层（S-P-1b 的确认卡执行器）自觉遵守，一次未来的重构完全可能新增
一条绕过确认卡的写入路径而不被任何检查发现。本迁移把它写成结构约束：
`pending_action_id REFERENCES pending_action(id)` 且 `NOT NULL`——任何一次 `INSERT`
如果不先有一条真实存在的 `pending_action` 行可引用，在数据库层面就会失败，不依赖
调用方记得校验。收回同理，见下「`revoked_pending_action_id`」一节。

**已知边界**：这条 FK 只核对"确认卡这一行存在"，不核对该 `pending_action` 的
`action_type`/`status`/`decided_by_open_id` 是否真的对应"这笔授权已经过确认"——
那层语义判定是 S-P-1b 确认卡执行器的职责（在同一事务内先核实决策结果、再插入本表，
与 `adapters/postgres_pending_action.py` 的 `_confirm_locked` 同一姿态）。当前
`pending_action.action_type` 的 `CHECK` 只允许 `suspend_user`/`resume_user`
（迁移 `0068`）——S-P-1b 落地时需要一次新增迁移把 `local_permission_grant`/
`local_permission_suppress`/`local_permission_revoke` 等取值加入该 `CHECK`，本
revision 不做这件事，也不因此新建任何占位取值。

## 为什么用「同一行状态翻转」而不是「插入撤销行」表达收回

与 `admin_registry`（迁移 `0067`）同一姿态：`entry_status` 在 `active`/`revoked`
两态之间翻转，撤销时间与撤销所用的确认卡记在同一行，而不是插入第二行「反向条目」。
理由相同——「查当前有效条目」永远是同一个 `WHERE entry_status = 'active'`，不需要
调用方自己去做「最新一条为准」的时间排序，也不会因为一次重放/重试插入出第二条
矛盾的历史行。历史行本身不删除、不覆盖，满足 [#319](https://github.com/Moshuiwang/lingxi/issues/319)
「收回走同一确认卡机制，审计对称」的留痕要求。

## 为什么没有 `revoked_by_open_id` 列

收回该笔覆盖时使用的确认卡（`revoked_pending_action_id`）本身已经记录了
`decided_by_open_id`（迁移 `0068`）；MVP 阶段唯一管理员意味着这个人恒等于
`initiated_by_open_id`，新增一列去重复存放同一份身份没有带来任何新事实，只会在
两处出现漂移的可能（例如一处更新另一处忘记同步）。需要"谁点击了收回"时经
`revoked_pending_action_id` 关联查询即可。

## 为什么没有任何有效期/到期/复核相关列

[#319](https://github.com/Moshuiwang/lingxi/issues/319) 治理口径第二条：产品负责人
明确裁定本地授权「不设有效期、不设定期复核」。这不是本 revision 遗漏，是刻意不建——
`pending_action` 那类 `confirm_deadline_at`（十分钟确认窗口）或未来可能出现的
"到期自动失效"字段都不适用于本表：一笔本地覆盖一旦确认生效，只能被同一套确认卡
机制显式收回，不会因为时间流逝自动改变状态。

## 表结构定稿（按 #319 粒度：目标用户 + 公司 × 指标）

- `user_id`：Lingxi 内部身份锚点（`app_user.id`），`REFERENCES app_user(id) ON
  DELETE CASCADE`——与 `publish_outbox.user_id`（迁移 `0064`）同一惯例：账号删除
  编排把这行删掉时，该用户的本地覆盖历史一并消失，不留孤儿行。**不用
  `feishu_open_id`**：`pending_action`/`admin_registry` 按 `open_id` 判定是因为
  它们服务的是"飞书身份能不能操作/被操作"这个问题；本表服务的是"这个人的权限聚合
  要不要叠加这一条"，聚合链路（`apps/scheduler/permission_refresh.py`）全程用
  `identity.app_user_id` 做键，取同一个键让 S-P-3 接线时不需要多一次身份转换。
- `company_id` / `metric_name`：与翻译层（`core/permission/metric_translation.py`）
  产出的 `{公司: [指标名]}` 同一粒度——本地覆盖与银河翻译结果要做集合运算，
  维度必须完全对齐，否则「并集」/「减集」无从谈起。两列都只做「非空白」校验
  （`CHECK (NULLIF(BTRIM(...), '') IS NOT NULL)`），**不做大小写/全半角归一**：
  与 `publish_row.py` 模块文档「零归一」纪律同一姿态，指标名要与翻译映射的取值
  逐字匹配，本侧提前归一只会制造静默错范围。
- `direction`：`'grant'`/`'suppress'` 二值 `CHECK`，见上「一张表双极性」。
- `reason`：#319「审计归属设计」要求的原因文本，`NOT NULL` 非空白。
- `initiated_by_open_id`：发起人（唯一管理员）的飞书身份，与
  `pending_action.initiated_by_open_id` 同一惯例。

## 索引

`local_permission_override_user_active_idx`：`(user_id) WHERE entry_status =
'active'`——聚合链路的读路径是"按用户取全部当前生效条目"（一次查询, 供 S-P-3
复用），这是它的天然覆盖索引；已撤销的历史行不参与聚合，因此过滤掉。

`local_permission_override_active_unique_idx`：`(user_id, direction, company_id,
metric_name) WHERE entry_status = 'active'`——同一用户同一极性同一公司同一指标
同一时刻只允许一条生效条目，防止重复发起同一笔授权/抑制堆出多条冗余历史行（数据库
层面的去重，与 `core/permission/local_override.py` 的纯函数去重是纵深防线的两层，
互不替代：纯函数那层还要处理"同一键同时有 grant 与 suppress"的跨极性冲突，
这条唯一索引不管，也管不了——那正是 `direction` 在索引列里的原因，两条 direction
不同的 active 行必须能够共存，是「suppress 赢」判定的输入，不是异常状态）。
"""

from __future__ import annotations

from alembic import op

revision: str = "0072_local_permission_override"
down_revision: str | None = "0074_task_document_delivery"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
CREATE TABLE local_permission_override (
    id                        TEXT        PRIMARY KEY,            -- ULID, lpo_*

    -- 目标用户：Lingxi 内部身份锚点，与 publish_outbox.user_id（迁移 0064）同一
    -- 惯例——账号删除编排删掉 app_user 那一行时，该用户的本地覆盖历史一并清除。
    user_id                   TEXT        NOT NULL
        REFERENCES app_user(id) ON DELETE CASCADE,

    -- 极性：本地授权（grant）与本地抑制（suppress）同表（文件头部「一张表双极性」）。
    direction                 TEXT        NOT NULL
        CHECK (direction IN ('grant', 'suppress')),

    -- 授权/抑制内容：公司 + 指标名，与翻译层输出同一粒度（文件头部）。只做非空白
    -- 校验，不做任何大小写/全半角归一——指标名要与翻译映射逐字匹配。
    company_id                TEXT        NOT NULL
        CHECK (NULLIF(BTRIM(company_id), '') IS NOT NULL),
    metric_name               TEXT        NOT NULL
        CHECK (NULLIF(BTRIM(metric_name), '') IS NOT NULL),

    -- 原因文本（#319「审计归属设计」要求，全量入审计的一部分）。
    reason                    TEXT        NOT NULL
        CHECK (NULLIF(BTRIM(reason), '') IS NOT NULL),

    -- 发起人：唯一管理员的飞书身份，与 admin_registry.feishu_open_id /
    -- pending_action.initiated_by_open_id 同一惯例。
    initiated_by_open_id      TEXT        NOT NULL
        CHECK (NULLIF(BTRIM(initiated_by_open_id), '') IS NOT NULL),

    -- 确认卡留痕：这笔覆盖是凭哪一次确认卡生效的。NOT NULL 是结构性的"无确认卡
    -- 无法写入"（文件头部「为什么 pending_action_id 是 NOT NULL」）。
    pending_action_id         TEXT        NOT NULL
        REFERENCES pending_action(id),

    -- 条目状态：与 admin_registry（迁移 0067）同一惯例，'active'/'revoked' 两态，
    -- 收回是同一行状态翻转（软删除，历史留痕），不是插入反向行。**没有第三态
    -- 'expired'，也没有任何到期列**——产品负责人明确裁定本地覆盖不设有效期、
    -- 不设定期复核（文件头部）。
    entry_status              TEXT        NOT NULL DEFAULT 'active'
        CHECK (entry_status IN ('active', 'revoked')),

    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at                TIMESTAMPTZ,
    -- 收回时使用的确认卡——与生效时的 pending_action_id 是两笔不同的待确认操作
    -- （"收回走同一确认卡机制"指同一套机制，不是同一行）。没有单独的
    -- revoked_by_open_id 列：这张卡自己的 pending_action.decided_by_open_id 已经
    -- 记录了这个事实，见文件头部。
    revoked_pending_action_id TEXT
        REFERENCES pending_action(id),

    -- revoked_at / revoked_pending_action_id 存在当且仅当条目已撤销：两种自相
    -- 矛盾的行（标了撤销却缺其一，或反过来）都在数据库层面拒绝，与 admin_registry
    -- 的 entry_status/revoked_at 一致性 CHECK 同一姿态。
    CHECK ((entry_status = 'revoked') = (revoked_at IS NOT NULL)),
    CHECK ((entry_status = 'revoked') = (revoked_pending_action_id IS NOT NULL))
);

-- 聚合链路的天然读路径：按用户取全部当前生效条目（文件头部「索引」）。
CREATE INDEX local_permission_override_user_active_idx
    ON local_permission_override (user_id) WHERE entry_status = 'active';

-- 同一用户同一极性同一公司同一指标同一时刻只允许一条生效条目（文件头部「索引」）。
CREATE UNIQUE INDEX local_permission_override_active_unique_idx
    ON local_permission_override (user_id, direction, company_id, metric_name)
    WHERE entry_status = 'active';
"""

#: 数据破坏操作，与 0067/0068 同型：一旦部署环境写入过真实本地覆盖，DROP 会把
#: 它们连同确认卡留痕一起清空，不是无损回滚。本 revision 未在任何环境应用过。
_DOWNGRADE_SQL = r"""
DROP TABLE IF EXISTS local_permission_override;
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与 0057–0071 同型：不走 ``op.execute()``，避免空参数集触发插值模式。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
