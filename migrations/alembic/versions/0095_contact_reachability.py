"""``app_user`` 新增四列：首次/最近入站时间与明确投递不可用的时刻/原因码。

Revision ID: 0095_contact_reachability
Revises: 0094_document_creation_attempted

「跟我们说过话」这一事实此前只能从 ``inbound_event`` 推导，而该表按九十天上限
整行删除（``0057`` 的触发器）。四列把这一事实、连同「主动发送是否可达」的最近
结论，落成不按九十天删除的当前状态——呼应数据库设计第九节里 ``app_user`` 已经
登记的当前状态例外，不新增一类例外、也不新建表。

新增 ``inbound_event`` 的 ``AFTER INSERT`` 触发器：每一条真实入站都把命中的
``app_user`` 行推进 ``first_inbound_at``/``last_inbound_at`` 并清空「不可达」标记。
再新增 ``app_user`` 的 ``AFTER INSERT`` 触发器：首聊用户的第一条入站往往早于
建档（先插事件、后建档案），建档那一刻把此前已经存在的入站事件认领进新行，
否则这个人会被读成「从没说过话」。两个触发器是新列独立于 ``inbound_event``
生存期的唯二写入点，不由应用层各自散着补写，测试也因此只能经真实调用点走到
这四列，直接 UPDATE ``app_user`` 绕不过它。

## 为什么不在本迁移里回填历史行

一次性历史回填是 ``scripts/ops`` 的独立脚本（四个来源逐一核对前后计数），本迁移
只建立结构与「此后每一次入站」的实时接线，不在 DDL 里掺一次性数据搬运。
"""

from __future__ import annotations

from alembic import op

revision: str = "0095_contact_reachability"
down_revision: str | None = "0094_document_creation_attempted"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
ALTER TABLE app_user
    ADD COLUMN first_inbound_at TIMESTAMPTZ,
    ADD COLUMN last_inbound_at TIMESTAMPTZ,
    ADD COLUMN outbound_unavailable_at TIMESTAMPTZ,
    ADD COLUMN outbound_unavailable_code TEXT;

CREATE OR REPLACE FUNCTION app_user_record_real_inbound() RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.user_open_id IS NULL THEN
        RETURN NEW;
    END IF;
    UPDATE app_user SET
        first_inbound_at = LEAST(COALESCE(first_inbound_at, NEW.received_at), NEW.received_at),
        last_inbound_at = GREATEST(COALESCE(last_inbound_at, NEW.received_at), NEW.received_at),
        outbound_unavailable_at = CASE
            WHEN outbound_unavailable_at IS NULL OR outbound_unavailable_at <= NEW.received_at
            THEN NULL ELSE outbound_unavailable_at END,
        outbound_unavailable_code = CASE
            WHEN outbound_unavailable_at IS NULL OR outbound_unavailable_at <= NEW.received_at
            THEN NULL ELSE outbound_unavailable_code END
    WHERE feishu_open_id = NEW.user_open_id
      AND (last_inbound_at IS NULL OR last_inbound_at <= NEW.received_at);
    RETURN NEW;
END;
$$;

CREATE TRIGGER inbound_event_record_contact
    AFTER INSERT ON inbound_event
    FOR EACH ROW EXECUTE FUNCTION app_user_record_real_inbound();

CREATE OR REPLACE FUNCTION app_user_adopt_prior_inbound() RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    earliest TIMESTAMPTZ;
    latest TIMESTAMPTZ;
BEGIN
    IF NEW.feishu_open_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT MIN(received_at), MAX(received_at) INTO earliest, latest
      FROM inbound_event WHERE user_open_id = NEW.feishu_open_id;
    IF earliest IS NULL THEN
        RETURN NEW;
    END IF;
    UPDATE app_user SET
        first_inbound_at = LEAST(COALESCE(first_inbound_at, earliest), earliest),
        last_inbound_at = GREATEST(COALESCE(last_inbound_at, latest), latest)
    WHERE id = NEW.id;
    RETURN NEW;
END;
$$;

CREATE TRIGGER app_user_adopt_prior_inbound
    AFTER INSERT ON app_user
    FOR EACH ROW EXECUTE FUNCTION app_user_adopt_prior_inbound();
"""

_DOWNGRADE_SQL = r"""
DROP TRIGGER IF EXISTS app_user_adopt_prior_inbound ON app_user;
DROP FUNCTION IF EXISTS app_user_adopt_prior_inbound();
DROP TRIGGER IF EXISTS inbound_event_record_contact ON inbound_event;
DROP FUNCTION IF EXISTS app_user_record_real_inbound();
ALTER TABLE app_user
    DROP COLUMN IF EXISTS outbound_unavailable_code,
    DROP COLUMN IF EXISTS outbound_unavailable_at,
    DROP COLUMN IF EXISTS last_inbound_at,
    DROP COLUMN IF EXISTS first_inbound_at;
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与既有 revision 同型：不走 ``op.execute()``，避免空参数集触发插值模式。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
