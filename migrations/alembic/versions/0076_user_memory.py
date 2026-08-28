"""用户记忆表：显式登记式术语映射 / 口径偏好 / 惯例模板（Issue #357 S-H3-3，D1 范围）。

Revision ID: 0076_user_memory
Revises: 0073_pending_action_perm_types
Create Date: 2026-08-28

**down_revision 与本 revision 分配的编号不连续，如实登记原因**：本卡的实施蓝图
（Issue #357 最新评论 S-W0-2 设计短文）原假定链头是 ``0075_progress_event_content``。
但 ``scripts/ci/check_alembic_revisions.py``（无需数据库的静态链检查，本次以
``pip install '.[migrate]'`` 装出的独立 venv 实跑核实）给出的真实结论是：**链头是
``0073_pending_action_perm_types``**——``0072``/``0073``/``0074``/``0075`` 四个
revision 的文件名编号与它们在链上的真实先后顺序并不一致（``...0071 → 0075 →
0074 → 0072 → 0073``，``0073`` 是没有任何后继指向它的真正链尾）。本 revision 因此
把 ``down_revision`` 接到 ``0073_pending_action_perm_types``，按调度卡「若链头已变，
顺延编号并在 PR 说明」的指示，仍取下一个未使用的文件名编号 ``0076``（不与既有
``0072``–``0075`` 任何一个数字冲突，保持文件名单调递增的可读性），只是它现在链在
``0073`` 之后而不是设计文假定的 ``0075`` 之后。

## 表结构（照抄迁移 ``0072_local_permission_override.py`` 的注释密度与列约束写法）

- ``user_id``：Lingxi 内部身份锚点（``app_user.id``），``REFERENCES app_user(id)
  ON DELETE CASCADE``——与 ``conversation.user_id``/``local_permission_override.
  user_id``/``publish_outbox.user_id`` 同一惯例，不用 ``feishu_open_id``。
- ``memory_type``：D1 范围固定 ``term_mapping``/``calibration_preference``/
  ``convention_template`` 三值，不留占位扩展槽——M2（模型提议轨）立项时再加迁移
  引入 ``model_proposed_confirmed`` 之类的新 ``source`` 取值，不是在这里预留。
- ``memory_key``/``memory_value``：只做非空白校验（``CHECK (NULLIF(BTRIM(...),
  '') IS NOT NULL)``），与 ``local_permission_override.company_id``/``metric_name``
  同一姿态。**「不存数据值」的红线不落在这两列的数据库约束上**：结构层面无法区分
  「一句话映射描述」（``memory_value``）与「用户手滑粘贴的查询结果文本」——这是
  已知边界，真正的红线落在命令面的登记语法（只接受 ``key => value`` 形状，见
  ``core/conversation/commands.py`` 的 ``parse_memory_command``）与产品文案提示，
  不是本迁移的疏漏。
- ``source``：D1 范围恒为 ``user_explicit``，``CHECK (source = 'user_explicit')``
  只放一个取值——不为 M2 预留占位值，避免"建了字段没人用"。

## 为什么没有 ``entry_status``/软删除

与 ``local_permission_override``（迁移 0072，翻转 ``active``/``revoked``）不同，
本表的 ``/memory clear``、``/memory forget`` 与停用/权限变化触发的清除都是硬
``DELETE``，理由是 ``resume_user`` 「不恢复已清正文」的既有语义（``V-管理-39``）
——记忆被清后同样不应该允许恢复，硬删除让这条不变量在数据库层面自动成立，不需要
额外一条"已清除"判断分支；也没有 ``local_permission_override`` 那样的留痕诉求
（记忆改口径就是改口径，不是需要审计回溯的权限决定）。

## 索引

``user_memory_user_type_key_idx``（唯一索引）：同一用户同一类型同一 key 只保留
一条当前值——重复登记＝更新（``ON CONFLICT ... DO UPDATE``），不是堆历史行。

``user_memory_user_idx``：``/memory list`` 与 worker 注入的天然读路径，按用户取
全部记忆。
"""

from __future__ import annotations

from alembic import op

revision: str = "0076_user_memory"
down_revision: str | None = "0073_pending_action_perm_types"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
CREATE TABLE user_memory (
    id            TEXT        PRIMARY KEY,             -- ULID, mem_*（new_id("mem")）

    -- 隔离键：与 conversation.user_id / local_permission_override.user_id /
    -- publish_outbox.user_id 同一惯例，用 app_user.id（内部锚点），不用 feishu_open_id。
    user_id       TEXT        NOT NULL
        REFERENCES app_user(id) ON DELETE CASCADE,

    -- 三类记忆，D1 范围固定这三个取值，不留占位扩展槽。
    memory_type   TEXT        NOT NULL
        CHECK (memory_type IN ('term_mapping', 'calibration_preference', 'convention_template')),

    -- 「黑话/口径项/惯例触发短语」——只有键名，不含任何查询结果。
    memory_key    TEXT        NOT NULL
        CHECK (NULLIF(BTRIM(memory_key), '') IS NOT NULL),

    -- 「映射目标/口径值/模板正文」——只存映射描述文本本身。
    -- 结构层面**无法**区分「一句话映射描述」与「用户手滑粘贴的查询结果文本」，
    -- 这是已知边界（见文件头部）：真正的红线靠命令面的登记语法（只接受
    -- `key => value` 这种键值对形状）与产品文案提示，不是数据库约束。
    memory_value  TEXT        NOT NULL
        CHECK (NULLIF(BTRIM(memory_value), '') IS NOT NULL),

    -- D1 范围恒为显式登记；只放一个取值，不为 M2（模型提议轨）预留占位值，
    -- 避免「建了字段没人用」——M2 立项时再加迁移引入 'model_proposed_confirmed'。
    source        TEXT        NOT NULL DEFAULT 'user_explicit'
        CHECK (source = 'user_explicit'),

    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 同一用户同一类型同一 key 只保留一条当前值（重复登记=更新，不是堆历史行；
-- 与 local_permission_override 的「同一行状态翻转」不同——这里没有留痕诉求，
-- 用户改口径就是改口径，不需要历史版本）。
CREATE UNIQUE INDEX user_memory_user_type_key_idx
    ON user_memory (user_id, memory_type, memory_key);

-- /memory list 与 worker 注入的天然读路径：按用户取全部记忆。
CREATE INDEX user_memory_user_idx ON user_memory (user_id);
"""

#: 数据破坏操作：一旦部署环境写入过真实用户记忆，DROP 会把它们全部清空，
#: 不是无损回滚。本 revision 未在任何环境应用过（与 0057-0073 同型的既有先例）。
_DOWNGRADE_SQL = r"""
DROP TABLE IF EXISTS user_memory;
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与 0057–0075 同型：不走 ``op.execute()``，避免空参数集触发插值模式。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
