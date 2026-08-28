"""表格分支：``task_document_delivery_request`` 复用同一张检查点表/同一状态机，
新增 ``delivery_type``/``resource_url`` 两列区分文档与表格。

Revision ID: 0078_document_delivery_sheet
Revises: 0073_pending_action_perm_types
Create Date: 2026-08-28

Issue #354 S-H3-2（D2 裁定：同构 #341 文档交付路由；检查点接线明确要求"与
#341 同一张检查点表/同一状态机"，不新建第二张表）。

**批次内并行迁移编号协调（重要，合并时需要人工改链）**：``down_revision``
指向本分支切出时刻的真实链头 ``0073_pending_action_perm_types``——``0074``→
``0072``→``0073`` 这条实际顺序由既有 revision 的 ``down_revision`` 决定，与
文件名编号顺序不一致，``python scripts/ci/check_alembic_revisions.py`` 可
验证当前唯一 head。实施卡最初给出的锚点（``0075_progress_event_content``）
基于一个错误前提（把它当成当前链头），编排者已于 2026-08-28 在 Trace #373
纠偏澄清：down_revision 改按真实链头填、编号仍是 ``0078_``。**H3 批次多个
Story 各自并行新增迁移**，本 revision 很可能不是合并时排在最后落地的那一个
——若同批另一条迁移先合并进 ``epic/h3-features``，编排者在合并排队时需要把
本 revision 的 ``down_revision`` 改指向那一条（保持单一线性链），并同步更新
``migrations/README.md`` 的 head 声明；本地单独验证（`alembic upgrade head`
在一个新建库上跑通、`check_alembic_revisions.py` 单分支通过）已完成，改链
后需要重新跑一次确认。

## 为什么复用 ``document_id``/``paragraphs`` 列而不是新增表格专属列

- **``document_id``**：docx 的 ``document_id`` 与 sheet 的 ``spreadsheet_token``
  语义相同——"外部系统那份检查点资源的唯一标识，一旦拿到就不能二次调用创建接口"
  ——复用同一列、同一个 :meth:`~lingxi.adapters.postgres_document_delivery.
  PostgresDocumentDeliveryStore.mark_document_created` 检查点方法，不新增并行
  列（新增列会让崩溃恢复逻辑需要判断"该看哪一列"，复用列则完全不需要）。
- **``paragraphs``**：docx 的段落文本数组与 sheet 的"行×列单元格文本二维数组"
  都满足既有 CHECK（``jsonb_typeof(paragraphs) = 'array' AND
  jsonb_array_length(paragraphs) > 0`` 或到期擦除后的空数组），复用同一列、
  同一条 CHECK，不需要新迁移改约束——sheet 行只是这个数组的元素本身又是一层
  JSONB 数组（``[["a","b"],["c","d"]]``），既有 CHECK 只检查最外层非空，天然兼容。

## 新增两列

- **``delivery_type``**：区分这一行走哪一条外部调用路径（``docx`` 走
  ``adapters/feishu_docx_delivery.py``，``sheet`` 走
  ``adapters/feishu_sheets_delivery.py``），默认 ``'docx'``——所有历史行与
  docx 既有插入路径不改一个字符就自动落在正确分类上，不需要回填。
- **``resource_url``**：docx 的链接由 ``LarkDocxDelivery.document_url`` 纯本地
  拼接（``tenant_domain`` + ``document_id``），不需要持久化；sheet 的建表响应
  直接带 ``url``（S-W0-3 探针实测，见 ``adapters/feishu_sheets_delivery.py``
  模块文档「与文档交付的差异点」第 1 条），且飞书表格链接的路径格式未经本仓库
  任何探针验证过固定公式，**不做格式猜测**，改为在建表成功那一刻随
  ``document_id`` 一起落检查点，通知发送步直接读这一列。CHECK 约束
  ``delivery_type = 'sheet' OR resource_url IS NULL`` 防止未来误用把这一列的
  值写进 docx 行——docx 的链接语义只能来自本地拼接，不能来自这一列。

``downgrade()`` 真实可执行：直接删除两列与其 CHECK；届时若已有 ``delivery_type
= 'sheet'`` 的行，这些行的类型标记与表格链接会随列一起丢失（不删除行本身），
与 ``0075`` 的既有先例同一姿态（放宽/新增字段的降级本身就是有损的，不做静默
数据修复）。
"""

from __future__ import annotations

from alembic import op

revision: str = "0078_document_delivery_sheet"
down_revision: str | None = "0073_pending_action_perm_types"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
ALTER TABLE task_document_delivery_request
    ADD COLUMN delivery_type TEXT NOT NULL DEFAULT 'docx'
        CHECK (delivery_type IN ('docx', 'sheet'));

ALTER TABLE task_document_delivery_request
    ADD COLUMN resource_url TEXT
        CHECK (delivery_type = 'sheet' OR resource_url IS NULL);

-- 认领查询谓词不变（仍按 status='pending' + created_at 排队，两种 delivery_type
-- 混在同一条队列里由 gateway 消费循环内部按类型分派，见 apps/gateway/
-- document_delivery.py），因此不新增覆盖 delivery_type 的索引——既有
-- task_document_delivery_request_pending_idx 已经服务这条查询，加一列不改变
-- 查询计划。
"""

_DOWNGRADE_SQL = r"""
ALTER TABLE task_document_delivery_request DROP COLUMN IF EXISTS resource_url;
ALTER TABLE task_document_delivery_request DROP COLUMN IF EXISTS delivery_type;
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与既有 revision 同型：不走 ``op.execute()``，避免空参数集触发插值模式。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
