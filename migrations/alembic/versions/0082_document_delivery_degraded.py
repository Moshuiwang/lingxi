"""文档交付明示降级：``task_document_delivery_request`` 新增可空
``body_degraded_reason`` 列，持久化"这一行的正文是降级写进去的"这件事。

Revision ID: 0082_document_delivery_degraded
Revises: 0081_management_card_context
Create Date: 2026-08-31

Issue #499（rc23 S-6）。产品负责人 2026-08-31 裁定：docx 正文命中本仓库不支持的
嵌套结构（``unsupported_nested_blocks``，典型是 markdown 表格）时，**回退纯文本
段落路径交付 ＋ 如实告知格式已简化**，不再整次交付失败（翻转 Issue #467 默认值
之后实测 22 次投递中 4 次、18.2% 整次失败，见 #499 的 W0-1 评论）。

## 为什么必须落库，而不是只在内存里传一个布尔值

裁定的成立条件是**明示降级**——用户必须知道这次拿到的排版被简化了。但"文档已
就绪"这条追加消息有**三条**互不相同的发送路径，只有一条能看见同一次调用里的内存
状态：

1. **原发送路径**——刚跑完四步流程的那次调用（``apps/gateway/document_delivery.py
   ::_process_docx_claim`` → ``_finalize_claim`` → ``_send_ready_notice``）。这条
   路径能直接拿到本次写正文的返回值（当时是 ``WriteBodyOutcome``，rc25 换机制
   之后是一次建档的返回值，见文末后记）。
2. **补发通知路径**——``succeeded`` 但 ``notified_at IS NULL``（第一次
   ``send_text`` 遇到瞬时出站故障）的行，由消费循环下一轮
   （``claim_unnotified_succeeded``）补发。**这是另一次进程调用，没有任何内存
   状态**。
3. **检查点恢复路径**——写正文之后、落 ``succeeded`` 之前进程崩溃/被
   ``reclaim_stale_processing`` 回收的行，下一次认领时 ``read_body_children``
   判定"正文已经写过"直接跳过写正文步，**因此永远不会再走一次写正文、也就永远
   拿不到那个内存信号**。

后两条路径读不到这一列时会发出**不带降级说明的**"文档已生成"——那正是本次裁定
明令要消灭的静默降级。因此这个信号必须与 ``document_id``（迁移 0074 的检查点列）
同一姿态：**写进库里，跨进程、跨崩溃可读**。

## 写入时机与语义

由 ``adapters/postgres_document_delivery.py::mark_body_degraded`` 在**降级正文写
入成功之后立刻单独提交**（独立事务，同 ``mark_document_created`` 的检查点纪律，
不与后续"授权/读回/落终态"共享事务）。``NULL`` = 没有降级（开关关时的段落
路径、或排版路径原样成功——两者都是这套部署本该给出的排版，不是降级）；非
``NULL`` = 这次交付的排版不是本该给出的那一份，取值是原因码（本迁移当时的唯一
取值是 ``unsupported_nested_blocks``；**rc25 起换成三个，见文末后记**）。

**残留窗口如实登记**：飞书写入与本地提交是两次独立往返，"降级正文已写入飞书、
但本地这一列还没提交"的窗口无法封死（与迁移 0074 文件头部「写正文步的幂等判据」
登记的是同一类非原子窗口）。命中这个窗口的行在恢复路径上会发出不带降级说明的
就绪通知——这是已知残留，不是本列失效：它把"必然错"收窄成"崩溃在那一瞬间才错"。

## 为什么不复用 ``last_error``

``last_error`` 的既有语义是"这一行最后一次失败的分类码"，且**会被回收/死信路径
写入**（``reclaim_stale_processing``/``fail_exhausted_pending``）。一行完全可能
先被回收写过 ``last_error``、再重试成功——那时 ``status='succeeded' AND
last_error IS NOT NULL`` 表达的是"曾经卡住过"，不是"这次降级了"。用它承载降级
信号会把两种完全不同的事实压进同一列，通知路径据此选文案就会误报降级。新增一列
是"两个不同的事实各有各的列"，不是洁癖。

## 与 ``V-投递-06`` 到期擦除的关系：**不擦**

24 小时到期擦除（``adapters/postgres_document_delivery.py::redact_expired_
content``）擦的是 ``title``/``paragraphs``/``markdown``——**用户资料**。本列是
一个固定枚举形状的原因码，与 ``status``/``attempts``/``last_error``/时间戳同类，
属于迁移 0074 文件头部登记的"行保留运行事实"，不含任何用户内容，因此不进擦除
语句。

## CHECK 约束：只有 docx 行可以有取值

同迁移 0079 ``markdown`` 列的 ``IS NOT DISTINCT FROM`` 姿态（NULL-safe 比较，
不依赖 ``delivery_type`` 的 ``NOT NULL`` 恰好还在）：sheet 分支走
``PUT`` 覆盖式写值接口，没有"markdown 转换"这个概念，也就没有"转换被拒绝所以
降级"这件事，数据库层直接拒绝这种自相矛盾的行。

``downgrade()`` 真实可执行：直接删除该列与其 CHECK。届时若已有非空
``body_degraded_reason`` 的行，随列一起丢失（不删除行本身）——与迁移 0078/0079
既有先例同一姿态，新增字段的降级本身就有损，不做静默数据修复。

## rc25 后记（Trace #544 S-7c，2026-09-03）：取值换了一套，列与约束一个字没改

本文件头部保留的是当时的事实，不改写历史；这里只声明两件此后发生的变化，
免得读者按名字去仓库里找一个已经不存在的符号：

1. **``WriteBodyOutcome``/``write_body``/``MAX_CONVERTED_BLOCKS`` 已删除**。正文
   写入整条换成飞书**服务端一次建档**（``POST /open-apis/docs_ai/v1/documents``），
   客户端不再做 markdown→blocks 转换、拼树与逐块写入。上文第 1 条说的"能拿到
   写正文的返回值"仍然成立，只是返回的类型换了。
2. **取值从一个换成三个**：``unsupported_nested_blocks`` **不再产生新行**（历史
   行原样保留、照常展示——本列的 CHECK 只约束"只有 docx 行能有取值"、不约束取值
   本身，所以换码不需要新迁移）。现在会写进来的是 ``body_too_long``（正文超过
   20 000 字符）与 ``title_not_embeddable``（标题含尖括号）两个**前置守卫**码
   （发出任何请求之前判定、改走两步段落路径，降级检查点先于写正文提交），
   以及 ``server_simplified_body``（飞书服务端自陈简化：文档已经建好也写好了，
   不改路、不重试，检查点在建档检查点之后紧接着单独提交）。

**上文那句"非 NULL = 正文改用纯文本段落路径写入"因此收窄成"这次交付的排版不是
本该给出的那一份"**：三个码里只有前两个仍然对应段落路径，``server_simplified_body``
那篇文档是带格式的。读侧据此**分派两条用户文案**（rc25 S-5c），本列因此不只是
运维追查用的记录——它决定用户看到哪一句话。
"""

from __future__ import annotations

from alembic import op

revision: str = "0082_document_delivery_degraded"
down_revision: str | None = "0081_management_card_context"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
ALTER TABLE task_document_delivery_request
    ADD COLUMN body_degraded_reason TEXT
        CHECK (body_degraded_reason IS NULL OR delivery_type IS NOT DISTINCT FROM 'docx');
"""

_DOWNGRADE_SQL = r"""
ALTER TABLE task_document_delivery_request DROP COLUMN IF EXISTS body_degraded_reason;
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与既有 revision 同型：不走 ``op.execute()``，避免空参数集触发插值模式。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
