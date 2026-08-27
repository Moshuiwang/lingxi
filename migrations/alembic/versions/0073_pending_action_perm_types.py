"""待确认操作扩展本地权限动作类型：``action_type`` CHECK 扩容五值 + 新增 ``payload`` 列。

Revision ID: 0073_pending_action_perm_types
Revises: 0072_local_permission_override
Create Date: 2026-08-27

（revision id 缩短为 ``perm_types``——``alembic_version.version_num`` 是
``VARCHAR(32)``，逐字节的 ``pending_action_permission_types`` 加上
``0073_`` 前缀共 36 字符，超出该列宽度；真库回归实测（本卡验证）直接暴露
这条边界，其余全部注释/文档仍称"本地权限动作类型扩容"，不改称呼，只改这一个
标识符本身的拼写。）

[Issue #319](https://github.com/Moshuiwang/lingxi/issues/319) S-P-1b（编排者审定的
设计卡，2026-08-27）：迁移 ``0072`` 文件头部「已知边界」一节已经登记了这条计划——
"S-P-1b 落地时需要一次新增迁移把 `local_permission_grant`/`local_permission_
suppress`/`local_permission_revoke` 等取值加入该 CHECK"。本 revision 正是那次
新增迁移；本地权限覆盖的收回（revoke）执行分支留给卡 B，见下文「为什么 revoke
取值本次一并加入」。

## 为什么用 DROP CONSTRAINT + ADD CONSTRAINT 而不是原地改写

与 ``0059`` 同型（该迁移扩充 ``task.status`` 的 CHECK 时的既定写法）：PostgreSQL
不支持直接 ``ALTER`` 一个已存在 CHECK 约束的表达式，只能整体替换。这条 CHECK 的
名字 ``pending_action_action_type_check`` 是 PostgreSQL 对迁移 ``0068`` 内联列级
CHECK 的默认命名（``<表名>_<列名>_check``），与 ``0059`` 的 ``task_status_check``
同一惯例，本迁移直接引用这个默认名，不显式重命名。

## 为什么 revoke 取值本次一并加入 CHECK，但没有配套的执行分支

设计卡把 revoke 的执行留给卡 B（``core/admin/pending_action.py`` 的
``VALID_SOURCE_STATES[REVOKE]``/``REQUIRED_ROLE[REVOKE]`` 均未在本批次登记，
``commands.py``/``router.py`` 都不解析或派发 revoke 命令），但 CHECK 约束本身一次性
扩到全部五值（而不是先加两个、卡 B 再加第三个），理由是**数据库约束是一次性成本，
应用层执行分支是渐进成本**：分两次迁移改同一条 CHECK 除了多一次迁移窗口没有任何
额外收益，卡 B 落地时反而需要再写一条几乎一样的 DROP+ADD 迁移去重复这件事。

## 为什么 ``payload`` 列是 ``TEXT NULL`` 而不是 ``JSONB``

与全库既有惯例一致（``pending_action.reason``/``result`` 都是 ``TEXT``，非结构化）：
``payload`` 的读写方只有 ``adapters/postgres_pending_action.py`` 一处（写在
``prepare()``，读在 ``confirm()``），双方都在 Python 层用 ``json.dumps``/
``json.loads`` 处理，数据库不需要对内容做结构化查询或索引——``JSONB`` 的校验/索引
能力在这里没有消费方，``TEXT`` 更简单，也与本表既有列的类型选择一致。

## 自洽 CHECK：为什么是 ``(action_type IN (三新值)) = (NULLIF(BTRIM(payload),'') IS NOT NULL)``

与迁移 ``0068`` 的 ``CHECK ((status = 'pending') = (decided_at IS NULL))`` 同一
"双向等价"写法：本地权限三类动作（授权/抑制/收回）**必须**携带非空白 ``payload``
（``confirm()`` 需要从中解析出公司×指标×原因才能执行），``suspend_user``/
``resume_user`` 两类**必须不**携带 ``payload``（它们的执行参数已经是
``target_open_id``/``target_state_snapshot`` 本身，不需要额外结构化数据）。

**这条 CHECK 只管「存在性」，不校验内容形状（措辞更正，Trace #328 opus 审查）**：
两种「存在性」层面自相矛盾的行——本地权限三类动作却没有 ``payload``（或整段
空白），或 ``suspend_user``/``resume_user`` 却带了 ``payload``——在数据库层面
被拒绝，不依赖应用层每次都记得同步维护这条最基本的对应关系。但它**不**校验
``payload`` 是不是合法 JSON、键是否齐全（``company_id``/``metric_name``/
``reason``）——一段非空白但形状不对的文本（例如 ``"x"``）照样能通过这条 CHECK。
那部分校验是应用层的职责：写路径 ``adapters/postgres_pending_action.py`` 的
``prepare()`` 自己构造 payload、形状天然正确；读路径的容错在展示层
（``core/admin/notification.py`` 的 ``_permission_payload``：``json.loads`` 失败
时不崩溃，渲染降级提示而不是静默丢弃「范围」段）。此前的措辞（"两种自相矛盾的
行都在数据库层面拒绝"）读起来像是这条 CHECK 兜底了全部形状异常，是一句超额声明，
这里改写成如实的范围。
"""

from __future__ import annotations

from alembic import op

revision: str = "0073_pending_action_perm_types"
down_revision: str | None = "0072_local_permission_override"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
ALTER TABLE pending_action DROP CONSTRAINT pending_action_action_type_check;
ALTER TABLE pending_action ADD CONSTRAINT pending_action_action_type_check
    CHECK (action_type IN (
        'suspend_user', 'resume_user',
        'local_permission_grant', 'local_permission_suppress', 'local_permission_revoke'
    ));

-- 本地权限三类动作（授权/抑制/收回）确认执行所需的结构化参数：
-- {"company_id": "...", "metric_name": "...", "reason": "..."}（JSON 字符串）。
-- suspend_user/resume_user 不使用这一列，见下方自洽 CHECK（文件头部）。
ALTER TABLE pending_action ADD COLUMN payload TEXT NULL;

ALTER TABLE pending_action ADD CONSTRAINT pending_action_payload_matches_action_type
    CHECK (
        (action_type IN ('local_permission_grant', 'local_permission_suppress', 'local_permission_revoke'))
        = (NULLIF(BTRIM(payload), '') IS NOT NULL)
    );
"""

#: 数据破坏操作的对称收窄：若届时仍有 local_permission_* 取值或非空 payload 的行，
#: 收窄会如实失败（与 0059 的既有先例一致，不做静默数据修复）。
_DOWNGRADE_SQL = r"""
ALTER TABLE pending_action DROP CONSTRAINT IF EXISTS pending_action_payload_matches_action_type;
ALTER TABLE pending_action DROP COLUMN IF EXISTS payload;

ALTER TABLE pending_action DROP CONSTRAINT IF EXISTS pending_action_action_type_check;
ALTER TABLE pending_action ADD CONSTRAINT pending_action_action_type_check
    CHECK (action_type IN ('suspend_user', 'resume_user'));
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与 0057–0072 同型：不走 ``op.execute()``，避免空参数集触发插值模式。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
