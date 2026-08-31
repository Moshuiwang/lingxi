"""任务失败签名：task 表新增细分失败码与底层异常类型名两列。

Revision ID: 0080_task_failure_signature
Revises: 0079_document_delivery_markdown
Create Date: 2026-08-31

Issue #495。2026-08-31 浸泡窗口取证：8 条任务失败里 **6 条无法归因**——结构化
日志只留下 ``worker.task.terminal error_kind=session_failed failure_code=null``，
底层异常的类型、文本、堆栈一条都没有离开 worker 进程。用户侧是对的（收到诚实的
失败提示，#465 响应覆盖成立），运维侧是全黑的。

**为什么必须落库，而不是只补日志**：worker 与 gateway 是两个独立部署的进程，
不共享文件系统，也没有日志聚合通道。只进 worker 容器 stderr 的线索，管理员在
飞书私聊里用 ``/admin trace <追溯号>`` 永远看不到。这与迁移 ``0070``（token
用量与守卫拒绝计数"只存在于 worker 自己的结构化日志"）是同一个结构性缺口、
同一条解法：在终态收口的同一事务里落进 ``task``。

**形状为什么这样定**：

- ``failure_code``（``TEXT``，可空）——``apps/worker/turn.py``/``service.py``
  给出的**细分**失败码（``session_failed``/``drain_timeout``/``sdk_unavailable``/
  ``cancelled``/``gate_bypassed``/``turn_not_closed`` 等）。既有的
  ``task.error_kind`` 不能替代它：那一列存的是
  ``apps/worker/service.py::_failure_content`` 把失败码压平成**用户文案分类**
  之后的粗粒度值，上面列的六种全部塌进同一个 ``session_failed``，落库之后再也
  分不开。
- ``failure_signature``（``TEXT``，可空）——底层异常的**类型限定名**
  （``psycopg.errors.UniqueViolation`` 这种形状，模块名 + 类名）。这是"未分类
  失败"唯一留得下的线索。

**这两列不装异常正文，这是安全约束不是省事**：``V-花名册-33`` 禁止把 ``ou_``
等外部标识原值写进审计与日志，而 psycopg 等驱动的异常串常见形状正是
``DETAIL: Key (feishu_open_id)=(ou_...)``。rc22 opus 审查 P2-5 已经为
``event.pipeline_failed`` 做过同一次收敛（只记 ``type(error).__name__``），
本列照抄那条既有做法。写入前还要过一道字符白名单（只留 ASCII 字母数字、下划线、
点）与 64 字符上界，见 ``apps/worker/report_extraction.py`` 的失败签名一节。

**两列都允许 ``NULL``，且 ``NULL`` 是精确语义、不是"暂时留空以后补"**：业务
成功的回合没有失败码可写；``turn_timeout``/``drain_timeout``/``cancelled`` 这
几种失败不来自任何异常对象，没有类型名可签。写 ``NULL`` 如实反映"结构性地取
不到"，不编造占位符——与迁移 ``0070`` 的同一条纪律。

**不需要回填历史行**：本 revision 之前产生的全部 ``task`` 行在这两列上原本就
没有任何可靠来源（那些信息当时只进过 worker 容器 stderr，且已随容器重建消失）。
回填成任何值都是编造，回填成 NULL 就是默认值本身。

``downgrade()`` 真实可执行：两列都是本 revision 新增，直接 ``DROP COLUMN``。
"""

from __future__ import annotations

from alembic import op

revision: str = "0080_task_failure_signature"
down_revision: str | None = "0079_document_delivery_markdown"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
ALTER TABLE task ADD COLUMN failure_code TEXT;
ALTER TABLE task ADD COLUMN failure_signature TEXT;
"""

_DOWNGRADE_SQL = r"""
ALTER TABLE task DROP COLUMN IF EXISTS failure_signature;
ALTER TABLE task DROP COLUMN IF EXISTS failure_code;
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与 0057/0058/0059/0060/0061/0062/0070 同型：不走 ``op.execute()``，避免空参数集触发插值模式。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
