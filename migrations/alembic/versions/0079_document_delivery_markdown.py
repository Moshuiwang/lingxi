"""Issue #408 正式方案管线接线：``task_document_delivery_request`` 新增可空
``markdown`` 列，持久化模型传入的原始 markdown 全文。

Revision ID: 0079_document_delivery_markdown
Revises: 0078_document_delivery_sheet
Create Date: 2026-08-29

产品负责人 2026-08-29 分两步裁定的第二步（第一步是立即停止字符剥离，已随
``core/execution/document_delivery.py`` 的 ``normalize_markdown`` 修复交付、
未改数据库）：正式排版走飞书官方 markdown→blocks 转换接口
（``adapters/feishu_docx_delivery.py::LarkDocxDelivery.write_body``），但那条
接口需要**原始 markdown 全文**，检查点表此前只持久化归一化后的段落数组
（``paragraphs``），转换所需的原文没有落盘——本迁移补上这一列，是"能力已经
做好、开关默认关"（Issue #408 上一批）到"真正接进生产调用路径"（本批）之间
缺的最后一块持久化。

## 为什么是新增列而不是复用/替换 ``paragraphs``

``paragraphs``（迁移 0074）继续是两件事的唯一依据，两者都不能被 markdown
替代：

1. **段落路径的兜底内容**——转换开关关闭、或转换失败关闭（业务错误码/结果
   不明/超过 :data:`~lingxi.adapters.feishu_docx_delivery.MAX_CONVERTED_BLOCKS`
   一律失败关闭，见该模块「markdown 官方转换开关」一节，不静默退回段落路径）
   时，``write_paragraphs`` 仍然只认 ``paragraphs``；
2. **检查点幂等判据的既有语义**——``adapters/feishu_docx_delivery.py::
   read_body_children`` 判断"是否已经写过正文"看的是飞书那一侧根 block 的
   子块是否非空，与本地存的是段落还是 markdown 无关，因此新增列不影响这条
   既有判据（迁移 0074 文件头部「写正文步的幂等判据」）。

``markdown`` 因此是**追加**的原文快照，不是段落列的替代：两列在真实内容态
可以同时非空（docx 类型、转换开关未来打开时）；``markdown IS NULL`` 是历史
默认——所有既有行与开关关闭状态下新插入的行都保持 ``NULL``，gateway 侧读到
``NULL`` 一律按段落路径处理（零行为变化，与本迁移之前逐字相同）。

## 为什么 ``sheet`` 行恒为 ``NULL``（CHECK 约束）

``markdown``/blocks 转换只服务 docx 分支——``adapters/feishu_sheets_delivery.py``
的写值走 ``PUT`` 覆盖式接口，没有"markdown 排版"这个概念，``rows``（同样存在
``paragraphs`` 这一列里，见迁移 0078 文件头部「为什么复用」）不需要、也不应该
有对应的原文快照。``CHECK (markdown IS NULL OR delivery_type IS NOT DISTINCT
FROM 'docx')`` 与迁移 0078 的 ``resource_url`` CHECK（``delivery_type =
'sheet' OR resource_url IS NULL``）同一姿态：把"哪个类型该有哪个附加列"从
应用层自觉收紧成数据库层的结构性拒绝。

**为什么是 ``IS NOT DISTINCT FROM`` 而不是 ``delivery_type = 'docx'``**（P2
顺手，独立审查）：``delivery_type`` 目前有迁移 0078 的 ``NOT NULL`` 兜底，
理论上不会真的出现 ``NULL``，但 CHECK 约束不应该依赖"另一张列的约束恰好还在"
才成立——用普通 ``=`` 比较时，若 ``delivery_type`` 一旦是 ``NULL``（例如未来
某次迁移放宽了 0078 的 ``NOT NULL``），``delivery_type = 'docx'`` 求值为
``UNKNOWN``，而 Postgres 的 CHECK 语义是"只有明确 ``FALSE`` 才拒绝、
``UNKNOWN`` 一律放行"——原写法因此会在那种假设不成立时悄悄放行一行
``delivery_type IS NULL AND markdown IS NOT NULL`` 的自相矛盾数据，绕开这条
本该挡住它的约束。``IS NOT DISTINCT FROM``（NULL-safe 比较，`NULL IS NOT
DISTINCT FROM 'docx'` 明确求值为 ``FALSE``）不依赖这个假设，任何时候
``delivery_type`` 不等于 ``'docx'``（含 ``NULL``）都会被明确拒绝。

## 到期擦除同步覆盖

``V-投递-06`` 的 24 小时正文到期擦除（``adapters/postgres_document_delivery.py``
的 ``redact_expired_content``）同批扩到这一列——``markdown`` 是"原始正文"这
个概念的另一种形态（信息量不小于 ``paragraphs``），不能只擦段落列却把原文
留在库里过期不清。擦除态统一写回 ``NULL``（与"从未提供 markdown"是同一个可
表达状态，不需要为擦除态另设哨兵值）。

``downgrade()`` 真实可执行：直接删除该列与其 CHECK。届时若已有非空
``markdown`` 的行，随列一起丢失（不删除行本身）——与迁移 0078 的既有先例
（``resource_url``/``delivery_type``）同一姿态，是新增/放宽字段的降级本身
就有损，不做静默数据修复。
"""

from __future__ import annotations

from alembic import op

revision: str = "0079_document_delivery_markdown"
down_revision: str | None = "0078_document_delivery_sheet"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
ALTER TABLE task_document_delivery_request
    ADD COLUMN markdown TEXT
        CHECK (markdown IS NULL OR delivery_type IS NOT DISTINCT FROM 'docx');
"""

_DOWNGRADE_SQL = r"""
ALTER TABLE task_document_delivery_request DROP COLUMN IF EXISTS markdown;
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与既有 revision 同型：不走 ``op.execute()``，避免空参数集触发插值模式。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
