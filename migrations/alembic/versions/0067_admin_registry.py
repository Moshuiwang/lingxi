"""服务端管理员角色登记表：飞书身份 + 三类角色授予状态 + 条目状态，供私聊管理命令面
实时判定（不缓存）。

Revision ID: 0067_admin_registry
Revises: 0066_onboarding_notice_outbox
Create Date: 2026-08-24

[Issue #95](https://github.com/Moshuiwang/lingxi/issues/95) S-M-01（2026-08-24 范围重定，
依据方案 A 终裁：[决策记录](../../../docs/决策记录/2026-08-24-管理员职责集与银河外权限动作边界.md)）。
本 revision 只交付**服务端管理员角色登记表**本身；管理 MCP 身份认证与绑定、登记表的
写路径（授予/撤销 + 本人确认卡）均退场到未来入口或 S-M-02（#96）。

## 为什么是一张表，不是「身份 + 角色」两张表

数据库设计早先为**管理 MCP**入场预留了 `admin_identity`（认证主体指纹）+ `admin_role`
（逐角色授予行）两张表，但 #95 的范围重定明确写着「三类角色模型……落为**登记表字段**」
——单数、字段化，不是拆两张表按角色开行。管理 MCP 认证绑定本身退场（留给未来入口），
`admin_identity` 设计里"只存指纹"的字段（`auth_provider`/`auth_subject`）此刻没有意义；
本表只承接当前真正要用的东西：飞书身份 + 三类角色的授予状态 + 条目状态 + 时间戳。
`admin_identity`/`admin_role` 两个名字继续留给数据库设计文档标记"未建"，供管理 MCP
真正立项时按那时的真实需要设计，不占用本表名字。

## 判定语义（本表存在的唯一理由）

- **默认拒绝**：没有命中一条 `entry_status='active'` 行即视为非管理员，与"查无此人"
  同一结论，不额外区分（呼应产品合同"查询不到目标用户时返回明确的不存在结果"的同一
  取舍——不给探测者可利用的信号）。
- **每次请求实时读表，不缓存**：判定实现（`adapters/admin_registry.py`）对每次管理命令
  都发起一次新查询；`entry_status` 一旦被改成 `revoked`，下一次请求立即读到新结果——
  这条不变量靠"没有任何缓存层"这个事实保证，不是本表结构能单独锁住的，因此消费方
  代码必须不引入缓存（V-管理-2x 断言核对这一点）。
- **条目状态用 TEXT + CHECK**，不用原生 `ENUM`：与数据库设计原则 2 同型，状态集合会
  演进，`CHECK` 增删是普通迁移可回滚。

## 三类角色为什么是布尔字段而不是三条独立行

MVP 唯一条目（组织资料同步的专用授权主体账号）三类角色**合并授予**——决策记录原文
"MVP 登记表唯一条目、三类角色合并授予"。合并成一次写入的最自然表达就是同一行的三个
布尔列，而不是三条各自可能不同时间戳、不同撤销状态的独立行；后者会让"合并授予"这件
事需要额外的应用层不变量去维持（三条行必须永远同生共死），而合并列结构上做不到"只
撤销一个角色、留另外两个"的悄悄分裂——想要真正的逐角色独立生命周期，那是 S-M-02
连同任免工具一起要做的产品决定，不该由这张表提前假设。

## 唯一活跃身份

`admin_registry_active_identity_idx` 是部分唯一索引：只对 `entry_status='active'` 的行
生效。同一个飞书身份可以有多条历史行（例如先撤销、后来因为满足复审条件重新登记），
但同一时刻只允许一条 `active`。**没有全局唯一索引**（不含 `entry_status`）：那会让"撤销
旧行、登记新行"这个未来动作在结构上做不到。

## 本 revision 不做什么

- 不提供任何 `UPDATE`/撤销路径的应用代码或触发器——S-M-01 的实施范围明确排除写动作
  （产品合同"待确认操作"机制尚未接入本表，S-M-02 #96 补齐）；因此**没有**类似
  `0065`/`0066` 那种"一经写入不可改"的触发器：当前唯一的写路径是种子命令的一次性
  `INSERT`，还没有需要防的"错误 UPDATE"存在。
- 不授任何数据库角色权限：与 `0057`/`0059`/`0061`/`0063`/`0064`/`0065`/`0066` 同型，
  运行时进程尚未以四个限权角色连库。
- 不建 `admin_identity`/`admin_role`（管理 MCP 的未来入口，见上）。
- **不落任何真实 open_id**：本迁移只建结构，不插入任何数据；初始条目由
  `python -m lingxi.apps.admin_bootstrap` 在部署环境中按需播种，读取运行时已登记的
  `feishu_delegated_subject.subject_open_id`（Issue #137 同一识别机制），仓库源码中
  不出现任何真实标识。

``downgrade()`` 真实可执行：表与索引都是本 revision 新建的，不存在需要还原的历史行。
"""

from __future__ import annotations

from alembic import op

revision: str = "0067_admin_registry"
down_revision: str | None = "0066_onboarding_notice_outbox"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
CREATE TABLE admin_registry (
    id                        TEXT        PRIMARY KEY,            -- ULID, adm_*

    -- 管理员的飞书身份锚点。判定与路由只用 open_id（与全仓其余身份判定同一惯例——
    -- first_contact.decide_first_contact 的专用授权账号排除同样只比 open_id）。
    feishu_open_id            TEXT        NOT NULL
        CHECK (NULLIF(BTRIM(feishu_open_id), '') IS NOT NULL),

    -- 脱敏标签，供审计与人工核对时识别"这是哪一类登记"，**不是真实姓名**——本表在
    -- MVP 阶段只有一条 'delegated_subject' 值，但字段本身不作真实姓名假设，未来真人
    -- 管理员登记时同样只填角色化标签（如 'ops-oncall'），不填个人姓名，减少一处
    -- 会随人员变化而需要维护、且不参与任何判定逻辑的可识别字段。
    label                     TEXT        NOT NULL
        CHECK (NULLIF(BTRIM(label), '') IS NOT NULL),

    -- 三类角色的授予状态：产品合同「管理员处理入口与安全确认」定义的权限管理员 /
    -- 运维管理员 / 超级管理员，MVP 唯一条目三者合并为 TRUE（见文件头部）。
    permission_admin_granted  BOOLEAN     NOT NULL DEFAULT FALSE,
    ops_admin_granted         BOOLEAN     NOT NULL DEFAULT FALSE,
    super_admin_granted       BOOLEAN     NOT NULL DEFAULT FALSE,

    -- 条目状态：默认拒绝判定只认 'active'。'revoked' 是终态之一，不是可逆的
    -- "临时挂起"——真正的撤销/恢复语义与确认卡机制一起留给 S-M-02。
    entry_status              TEXT        NOT NULL DEFAULT 'active'
        CHECK (entry_status IN ('active', 'revoked')),

    granted_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at                TIMESTAMPTZ,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- revoked_at 存在当且仅当条目已撤销：防止"标了撤销却没有时间戳"或反过来
    -- "还是 active 却带着撤销时间"这两种自相矛盾的行被写入。
    CHECK ((entry_status = 'revoked') = (revoked_at IS NOT NULL))
);

-- 判定与路由的唯一入口索引：按 open_id 找当前有效条目。默认拒绝判定
-- （adapters/admin_registry.py）只发一条 `WHERE feishu_open_id = $1 AND
-- entry_status = 'active'` 查询，这个索引让它是一次索引命中而不是全表扫描。
CREATE INDEX admin_registry_open_id_idx ON admin_registry (feishu_open_id);

-- 同一飞书身份同一时刻只允许一条有效登记；历史撤销行不受此索引约束，允许
-- 未来"撤销旧条目、登记新条目"的形态在结构上成立。
CREATE UNIQUE INDEX admin_registry_active_identity_idx
    ON admin_registry (feishu_open_id) WHERE entry_status = 'active';
"""

_DOWNGRADE_SQL = r"""
DROP TABLE IF EXISTS admin_registry;
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与 0057–0066 同型：不走 ``op.execute()``，避免空参数集触发插值模式。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
