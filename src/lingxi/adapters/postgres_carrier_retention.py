"""四个内容载体的九十天到期清理：任务问题原文、入站事件、待确认操作、队列失败通知。

产品合同「数据保留与删除」把"任务问题"与"待确认操作参数"点名写进了九十天上限，并要求
每份可识别内容自写入起不得晚于九十天删除**或**不可逆脱敏。这四个载体此前的缺陷形状与
``mcp_sync_check``/``innertest_content_capture`` 当年一样——机制交付了、调用点没接：期限
躺在没人读的列里，或者连列都还没有（``pending_action``，由迁移 ``0089`` 补上）。

处置分两类，按"能擦的东西"分：``inbound_event``（``user_open_id`` 是它唯一的可识别列）
与 ``queue_failure_notice``（只有事件标识与期限）整行删除；``task`` 与 ``pending_action``
**保留行、只擦内容**——两张表都被别的行以外键或业务事实依赖着，删整行会带走继续提供
服务所需的当前状态。逐条理由随各方法的文档字符串。**不进迁移 ``0054`` 的受限清理函数**：
那条防线只挂在两张父表上，本模块走应用层小批量语句，与 ``V-保留-26`` 同一先例，不动
函数、属主授权面与任何角色的删除权限。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect

logger = logging.getLogger(__name__)

# 四条纪律，四个方法共用：到期判据只有 ``<到期列> <= now`` 一个条件（期限由触发器写死，
# 多加一个业务条件就等于开一个"某些内容可以留过九十天"的口子）；小批量、每轮一次，一次
# 调用就是一个事务、积压交给下一轮；两条脱敏各有自己的水位判据，重复执行不重复计数；
# 返回值只有条数、不取任何行内容（同 ``V-保留-14``）。

DEFAULT_CARRIER_BATCH_LIMIT = 200

#: 任务问题原文脱敏后的取值。空串而不是占位文案：``task.prompt`` 是 ``TEXT NOT NULL``
#: 且没有非空 CHECK，空串既满足约束，又让"这一行还欠一次脱敏"可以直接用
#: ``prompt <> ''`` 判定，不必再加一列水位。
REDACTED_PROMPT = ""

#: 待确认操作参数脱敏后的取值。不置 NULL、不置空串：迁移 ``0073`` 的
#: ``pending_action_payload_matches_action_type`` 要求本地权限三类动作必须携带非空白
#: ``payload``。擦成 ``'{}'`` 与 ``publish_outbox.payload`` 的既有做法同一形态。
REDACTED_PAYLOAD = "{}"

#: 三列 ``open_id`` 脱敏值的前缀，实际写入 ``前缀 || pending_action.id``。
#: **必须逐行唯一**：``pending_action_single_pending_target_idx`` 是
#: ``target_open_id`` 上的部分唯一索引（``WHERE status = 'pending'``），多条到期
#: 但仍停在 ``pending`` 的行如果被脱敏成同一个常量就会撞唯一索引，把一次本该静默
#: 完成的合规动作变成每轮都失败。``id`` 是内部 ULID，不是可识别内容。
REDACTED_OPEN_ID_PREFIX = "redacted:"


#: 待确认操作的到期脱敏语句。放在模块层而不是方法体里：逐列的"为什么"要写清楚，塞进
#: 方法里会让一个只做「校验参数 → 执行一条语句 → 记条数」的方法看起来很长。
_REDACT_PENDING_ACTIONS_SQL = """
UPDATE pending_action
       -- 三列 open_id 擦成「前缀 || id」，逐行唯一的理由见 REDACTED_OPEN_ID_PREFIX；
       -- decided_by_open_id 本来是 NULL 的保持 NULL——没有内容可擦，写一个脱敏值
       -- 等于凭空造出「有人决策过」。
   SET target_open_id = %(prefix)s || id,
       initiated_by_open_id = %(prefix)s || id,
       decided_by_open_id =
           CASE WHEN decided_by_open_id IS NULL THEN NULL ELSE %(prefix)s || id END,
       -- 仍停在 pending 且早过确认窗口的旧行**同批**结清成 expired。确认窗口是分钟级，
       -- 一条九十天前的待确认动作留在 pending 本身就不对；更要紧的是
       -- core.admin.pending_action.decide_confirm 的 not_initiator 判据排在 expired
       -- **之前**且零业务变更——脱敏改掉 initiated_by_open_id 之后，发起人再点三个月前
       -- 那张仍留在聊天记录里的卡只会永远得到「不是发起人」，那行**再也不会**转成
       -- expired，V-管理-30 承诺的「已过期 → 首次发现即转终态」就此不成立。
       -- decided_at 必须一起写（0068 的 CHECK：终态必须带 decided_at），取本轮判定时刻，
       -- 与 card_send_failed 那条系统侧终态同一姿态；decided_by_open_id 保持 NULL——
       -- 没有人做过这个决定。reason 沿用既有的 expired 原因码，不新造。
       status =
           CASE WHEN status = 'pending' AND confirm_deadline_at <= %(now)s
                THEN 'expired' ELSE status END,
       reason =
           CASE WHEN status = 'pending' AND confirm_deadline_at <= %(now)s
                THEN 'expired' ELSE reason END,
       decided_at =
           CASE WHEN status = 'pending' AND confirm_deadline_at <= %(now)s
                THEN %(now)s ELSE decided_at END,
       -- payload **只在真有内容时**才擦：0073 的
       -- pending_action_payload_matches_action_type 是双向等价约束，非权限类动作必须
       -- 保持空白。只判 IS NULL 会把空串/纯空格的 suspend_user 行擦成 '{}' 而违反
       -- CHECK；这是一条批量 UPDATE，一行违约会让同批合法的到期行一起回滚，坏行每轮
       -- 又被重新选中，于是这一面永远收不走任何东西。空白 payload 没有可识别内容。
       payload =
           CASE WHEN BTRIM(payload) <> '' THEN %(payload)s ELSE payload END,
       content_redacted_at = %(now)s
 WHERE id IN (
       SELECT id FROM pending_action
        WHERE retention_expires_at <= %(now)s
          AND content_redacted_at IS NULL
        ORDER BY retention_expires_at
        LIMIT %(limit)s
 )
"""


def _checked_moment(now: datetime | None) -> datetime:
    """到期判定时间必须带时区，否则在任何 ``DELETE``/``UPDATE`` 之前就拒绝。

    朴素时刻与数据库里的 ``timestamptz`` 比较会按服务端时区静默解释，做出的判定
    可能差整整八小时——那正是"提前删掉还没到期的内容"与"到期内容留着不删"两种
    保留违规。宁可响亮失败。
    """
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("到期判定时间必须带时区")
    return moment


def _checked_limit(limit: int) -> int:
    """批量上限必须是正整数（``bool`` 是 ``int`` 的子类，单独挡掉）。"""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit 必须是正整数")
    return limit


class PostgresCarrierRetention:
    """四个内容载体到期处置的唯一入口。构造时不连接数据库，每个方法自带事务。"""

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        """记下 DSN 与超时配置；不在构造时连接数据库。"""
        self._dsn = dsn
        self._timeouts = timeouts

    # ---- 逐载体处置 -------------------------------------------------------

    def redact_expired_task_prompts(
        self, *, now: datetime | None = None, limit: int = DEFAULT_CARRIER_BATCH_LIMIT
    ) -> int:
        """把过了九十天上限的 ``task.prompt`` 擦成空串，返回处置行数。

        **脱敏而不是删整行**：``task`` 是投递记录、文档交付检查点与任务指标的父行，
        删掉它会把"这次交付确实发生过"这件事一并带走——那属于继续提供服务与对账
        所需的事实，不在九十天删除范围里。合同给的是"删除**或**不可逆脱敏"。

        ``prompt <> ''`` 既是幂等判据也是索引条件（``task_content_expiry_idx``）：
        已经脱敏的行不会被再算一次，也不会再进扫描面。
        """
        moment = _checked_moment(now)
        batch = _checked_limit(limit)
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    """
                    UPDATE task
                       SET prompt = %s
                     WHERE id IN (
                           SELECT id FROM task
                            WHERE content_expires_at <= %s
                              AND prompt <> ''
                            ORDER BY content_expires_at
                            LIMIT %s
                     )
                    """,
                    (REDACTED_PROMPT, moment, batch),
                )
                redacted = cursor.rowcount
        if redacted:
            logger.info("任务问题原文已到期脱敏 条数=%s", redacted)
        return redacted

    def purge_expired_inbound_events(
        self, *, now: datetime | None = None, limit: int = DEFAULT_CARRIER_BATCH_LIMIT
    ) -> int:
        """删除过了九十天上限的 ``inbound_event`` 行，返回删除条数。

        **删整行**：这张表除了 ``user_open_id``（可识别身份）以外只剩事件标识、类型与
        追溯号，擦光身份等于留一具空壳。去重语义不受影响——重投窗口远小于九十天。
        ``task.inbound_event_id`` 刻意不设外键（迁移 ``0057`` 文件头），删除不会
        级联到任务。
        """
        moment = _checked_moment(now)
        batch = _checked_limit(limit)
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    """
                    DELETE FROM inbound_event
                     WHERE feishu_event_id IN (
                           SELECT feishu_event_id FROM inbound_event
                            WHERE expires_at <= %s
                            ORDER BY expires_at
                            LIMIT %s
                     )
                    """,
                    (moment, batch),
                )
                purged = cursor.rowcount
        if purged:
            logger.info("入站事件已到期删除 条数=%s", purged)
        return purged

    def redact_expired_pending_actions(
        self, *, now: datetime | None = None, limit: int = DEFAULT_CARRIER_BATCH_LIMIT
    ) -> int:
        """脱敏过了九十天上限的 ``pending_action`` 行，返回处置行数。

        逐列的处置与各自的理由随 :data:`_REDACT_PENDING_ACTIONS_SQL` 里的注释；这里只
        说**为什么保留行**：``local_permission_override.pending_action_id`` 是 NOT NULL
        外键，删整行会把一条现行本地权限覆盖的成立依据带走。
        """
        moment = _checked_moment(now)
        batch = _checked_limit(limit)
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    _REDACT_PENDING_ACTIONS_SQL,
                    {
                        "prefix": REDACTED_OPEN_ID_PREFIX,
                        "payload": REDACTED_PAYLOAD,
                        "now": moment,
                        "limit": batch,
                    },
                )
                redacted = cursor.rowcount
        if redacted:
            logger.info("待确认操作已到期脱敏 条数=%s", redacted)
        return redacted

    def purge_expired_queue_failure_notices(
        self, *, now: datetime | None = None, limit: int = DEFAULT_CARRIER_BATCH_LIMIT
    ) -> int:
        """删除过了九十天上限的 ``queue_failure_notice`` 行，返回删除条数。

        这张表只有事件标识、创建时间与期限，不含用户正文（迁移 ``0058``），到期整行
        删除。它此前既没有清理调用方，也**不在数据库设计第九节的载体表里**——文档与
        迁移不一致，本批一并补上。
        """
        moment = _checked_moment(now)
        batch = _checked_limit(limit)
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    """
                    DELETE FROM queue_failure_notice
                     WHERE feishu_event_id IN (
                           SELECT feishu_event_id FROM queue_failure_notice
                            WHERE expires_at <= %s
                            ORDER BY expires_at
                            LIMIT %s
                     )
                    """,
                    (moment, batch),
                )
                purged = cursor.rowcount
        if purged:
            logger.info("队列失败通知已到期删除 条数=%s", purged)
        return purged
