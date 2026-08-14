"""Gateway 投递消费的本地记账：消费游标、送达标识与外发预留位。

Revision ID: 0060_gateway_delivery_dispatch
Revises: 0059_delivery_outbox
Create Date: 2026-08-14

Issue #152。#151 冻结的 ``task_delivery_event`` outbox 只记录 Worker 一侧「写了什么」，
不记录 Gateway 一侧「消费到哪、卡片是哪张、这次投递绑定的可回读标识是什么」——这些是
Gateway 消费循环自己的记账，与 outbox 事件表的所有权、幂等键和到期语义是两回事，因此
新开一条 revision 而不是改 0059。

四个新列全部加在 ``task`` 上，与 0057 已经预留但从未启用的 ``card_id``/``card_seq``/
``fallback_text`` 同表，理由一致：这些字段描述的是「这个任务的投递正在被谁、按什么进度
处理」，天然是 task 的一部分，不需要单独一张 1:1 的表。

1. **``delivery_consumed_sequence``**：Gateway 已经**durable**处理过的
   ``task_delivery_event.sequence`` 上限（不区分该事件是否真的产生了一次外部调用——
   被限流跳过的 ``progress`` 帧同样推进游标）。重启后从这里恢复，不重新处理已经处理过
   的事件（`V-投递-02`）。
2. **``delivery_message_id``**：飞书发送接口返回的、"平台已接收"的可回读标识
   （G-CARD 实测：卡片与文本共用同一发送接口与响应结构，都回 ``message_id``）。卡片路径
   在创建并发送出那一刻捕获一次，此后的流式更新不产生新标识，因此只需要一个字段服务
   两条通道；写入 ``confirm_delivery`` 时作为 ``platform_message_id``。
3. **``dispatch_reserved_kind``**：外发前预留位（P3-6：#151 审核明确要求 #152 在没有
   飞书原生幂等键的调用点上自建"发送前预留位"），取值 ``card_create``/``card_finish``/
   ``text_send``。建卡（``POST cardkit/v1/cards`` 无客户端幂等键）与纯文本兜底发送同理
   没有天然的重复保护，崩溃重启后不知道上一次是否已经外发成功。**终态卡片更新+关闭
   （``card_finish``）同样需要预留位**——这一点起初被误判为"CardKit 整卡级严格递增
   ``sequence`` 天然兜底、重放同一序号会被平台以 300317 拒绝，不需要预留位"，但
   300317 只保证"不产生第二次可见的卡片帧"，不保证"消费循环的错误处理不会把这次拒绝
   误判成卡片链路整体失败进而降级到文本兜底"——那样反而会在卡片已经成功送达之后又
   多发一条文本终态，构成真实的重复投递。因此把 ``finish()``（更新+关闭两次外部调用）
   整体纳入预留位保护范围：崩溃发生在预留位提交之后、``finish()`` 返回之前的任意时刻
   （包括某次外部调用已经成功但结果尚未被消费循环处理完）都会被下一轮识别为
   ``uncertain``，而不是重新调用 ``finish()`` 撞上一次已经成功的尝试。
   调用前提交这一列非空、调用有了明确结果后立即清空；
   **进程崩溃发生在提交与清空之间时，重启后这一列仍非空，消费循环必须把该任务视为
   ``uncertain``、停止对它的自动处理并告警，不得凭空猜测上一次调用是否成功后自动重发**
   （Issue 状态合同第 6 条：不得自动重发造成重复结果）。恢复需要人工核对后清空。
4. **``delivery_expired_notice_sent_at``**：`V-投递-06` 后半句——正文因二十四小时未
   投递到期后，只在用户下一条主动消息时提示一次"请重新提问"，不主动推送。这一列记录
   "已经在哪一次任务上提示过"，同一次到期只提示一次；不影响 ``expire_undelivered_terminals``
   本身的到期判定（那条仍然只看 ``task_delivery_event.expires_at``）。

四列都是 Gateway 自己的进度记账，不是审计事实，因此不加冻结触发器——与 outbox 表里
``created_at``/``sequence``/``event_type`` 那类"一经写入不可改"的锚点性质不同，这里
每一列都需要在消费过程中被正常推进/覆盖。

``downgrade()`` 真实可执行：四列都是本 revision 新增，直接 ``DROP COLUMN``，不存在
需要回填的历史值。
"""

from __future__ import annotations

from alembic import op

revision: str = "0060_gateway_delivery_dispatch"
down_revision: str | None = "0059_delivery_outbox"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
ALTER TABLE task ADD COLUMN delivery_consumed_sequence INT NOT NULL DEFAULT 0;
ALTER TABLE task ADD COLUMN delivery_message_id TEXT;
ALTER TABLE task ADD COLUMN dispatch_reserved_kind TEXT
    CHECK (dispatch_reserved_kind IS NULL OR dispatch_reserved_kind IN
           ('card_create', 'card_finish', 'text_send'));
ALTER TABLE task ADD COLUMN delivery_expired_notice_sent_at TIMESTAMPTZ;
"""

_DOWNGRADE_SQL = r"""
ALTER TABLE task DROP COLUMN IF EXISTS delivery_expired_notice_sent_at;
ALTER TABLE task DROP COLUMN IF EXISTS dispatch_reserved_kind;
ALTER TABLE task DROP COLUMN IF EXISTS delivery_message_id;
ALTER TABLE task DROP COLUMN IF EXISTS delivery_consumed_sequence;
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与 0057/0058/0059 同型：不走 ``op.execute()``，避免空参数集触发插值模式。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
