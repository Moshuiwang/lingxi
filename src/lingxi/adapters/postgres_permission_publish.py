"""权限决定与发布意图的 PostgreSQL 存取。

表结构与逐条理由以迁移 ``0064_permission_publish_outbox`` 为准，本模块不复述。
这里只落实四件在**代码里**才成立的事：一、权限版本推进与发布意图写在同一
事务（见 :mod:`lingxi.adapters.postgres_permission_publish_decision`）；
二、``FOR UPDATE SKIP LOCKED`` 认领 + 同一用户单飞 + 本轮已认领的排除
（见 :meth:`~.claim_next`）；三、九十天到期擦除（见 :meth:`~.redact_expired_payloads`）；
四、权限变化感知即清（见 :meth:`~.record_decision` 的开关）。

**没有新增 ``app_user.publish_state`` 列**：发布进度已完整由
``publish_outbox.status`` 承载，再加一列等于制造第二个真相来源。**不写
``permission_record_id``**：匹配确认前它为 ``NULL``，发布成功后写进去的
是数据库权限记录而不是外部表格记录标识（后者落在 ``external_record_id``）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.adapters.postgres_conversation import (
    _Transaction as _postgres_conversation_transaction,
)
from lingxi.adapters.postgres_permission_publish_decision import (
    DecisionOutcome as DecisionOutcome,
)
from lingxi.adapters.postgres_permission_publish_decision import (
    PermissionDecision as PermissionDecision,
)
from lingxi.adapters.postgres_permission_publish_decision import _DecisionMixin
from lingxi.core.identity.roster_audit import ArchivedIdentity
from lingxi.core.permission.mcp_readiness_base import TERMINAL_OUTCOMES
from lingxi.core.permission.publish import ClaimedPublish, PublishAttempt, PublishOutcome

logger = logging.getLogger(__name__)

_UTC = UTC

#: 一条 ``publishing`` 卡多久之后可以放回 ``pending``。取值远大于一次外部写读回的耗时，
#: 又远小于一天：太短会让一次慢调用被重复执行（安全但浪费配额），太长会让一次进程崩溃
#: 把该用户的后续发布堵到第二天。
DEFAULT_RECLAIM_AFTER = timedelta(minutes=15)


@dataclass(frozen=True)
class PendingReadiness:
    """一条已经发布读回一致、但就绪确认还没收口的意图（每轮 tick 输入）。

    ``permissions`` 是**当初决定发布、并且已经逐字段读回核对过**的那一串文本。就绪判定
    的 ``no_permission`` 分支与通知正文都只认它，因此这条链上不存在"通知说的范围"与
    "消费方读到的范围"来自两次不同计算的可能。
    """

    user_id: str
    permission_version: int
    permissions: str
    published_at: datetime | None = None


@dataclass(frozen=True)
class StoredIntent:
    """回读出来的一条发布意图（供运维排查与用例断言）。"""

    outbox_id: str
    user_id: str
    permission_version: int
    reason: str
    payload: Mapping[str, Any]
    status: str
    attempts: int
    last_outcome: str | None
    last_error: str | None
    external_record_id: str | None
    published_at: datetime | None


class _Transaction(Protocol):
    """调用方已经开启事务的数据库连接。

    只要求一个 ``cursor()``：本模块不 ``commit``、不 ``rollback``、不建连接——事务边界
    完全由调用方掌握，这正是「审计与状态变更同事务」这条合同在类型上的落点。
    """

    def cursor(self) -> Any: ...


class PublishClaimLostError(RuntimeError):
    """记账时那条意图已经不在 ``publishing``（被回收或被人改过）。

    不静默吞掉：外部表格**可能已经写过**，而库里的状态没跟上。抛出来让调用方留痕并
    停手；那条意图会被重新认领并重发一次，重发安全（先查后写，收敛到同一行）。
    """


#: 向后兼容别名：迁移前的名字不满足 ruff N818（异常类需以 ``Error`` 结尾）。
PublishClaimLost = PublishClaimLostError

#: 本文件正文不直接调用它（真正的调用在 postgres_permission_publish_decision.py），
#: 但 tests/test_permission_publish_postgres.py 按
#: postgres_permission_publish._ConversationTransaction 这个路径打桩注入清理
#: 失败——两个模块引用的是同一个类对象，桩在这一处生效对两边同时有效，留一个
#: 模块级名字只是让 mock.patch 的属性解析能找到它。
_ConversationTransaction = _postgres_conversation_transaction

_CLAIM_NEXT_SQL = """
UPDATE publish_outbox AS o
   SET status = 'publishing',
       attempts = o.attempts + 1,
       claimed_at = now()
 WHERE o.id = (
         SELECT c.id
           FROM publish_outbox c
          WHERE c.status = 'pending'
            AND c.id <> ALL(%s)
            AND NOT EXISTS (
                  SELECT 1
                    FROM publish_outbox b
                   WHERE b.user_id = c.user_id
                     AND b.status IN ('pending', 'publishing')
                     AND (b.created_at, b.id) < (c.created_at, c.id)
                )
          ORDER BY c.created_at, c.id
          LIMIT 1
            FOR UPDATE SKIP LOCKED
       )
RETURNING o.id, o.user_id, o.permission_version, o.payload, o.attempts,
          o.created_record_id
"""

_COMPLETE_PUBLISH_SQL = """UPDATE publish_outbox
      SET status = %(status)s,
          last_outcome = %(outcome)s,
          last_error = %(detail)s,
          external_record_id = COALESCE(%(record_id)s, external_record_id),
          created_record_id = COALESCE(%(created_id)s, created_record_id),
          published_at = CASE WHEN %(status)s = 'published' THEN now() ELSE NULL END
    WHERE id = %(id)s
      AND status = 'publishing'
      AND attempts = %(attempts)s"""

_RECLAIM_STALE_PUBLISH_SQL = """UPDATE publish_outbox
      SET status = 'pending', claimed_at = NULL, last_outcome = 'reclaimed'
    WHERE status = 'publishing' AND claimed_at < now() - %s::interval"""

_REDACT_EXPIRED_PAYLOADS_SQL = """UPDATE publish_outbox
      SET payload = '{}'::jsonb, last_error = NULL
    WHERE id IN (
            SELECT id FROM publish_outbox
             WHERE content_expires_at <= %s AND payload <> '{}'::jsonb
             ORDER BY content_expires_at
             LIMIT %s
          )"""

# 三条筛选缺一不可：status='published' 只有发布读回一致的那一版才进入就绪
# 确认与通知；「该用户没有更新的意图」防止据一份已经过时的版本发通知；
# 「没有终态就绪记录」是这条链唯一的"已经处理过"水位。到期时刻算法须与
# `core.permission.mcp_readiness_tick.next_probe_due` 一致（起点 + 已判定次数
# × 间隔，预算封顶）。

# 跨读 mcp_sync_check（属 postgres_mcp_token 的表）是一处知情的例外，只读，
# 写仍然只有 postgres_mcp_token.record_attempt 一处——做成一条语句而不是
# 先取回再在 Python 过滤，是为了不让"已确认完的人一直占着取回窗口、当天
# 靠后发布的人可能永远排不进来"。
_PUBLISHED_AWAITING_READINESS_SQL = """
SELECT o.user_id, o.permission_version, o.payload ->> 'permissions',
       o.published_at
  FROM publish_outbox o
  LEFT JOIN LATERAL (
         SELECT count(*) AS attempts, min(c.started_at) AS first_started_at
           FROM mcp_sync_check c
          WHERE c.user_id = o.user_id
            AND c.permission_version = o.permission_version
       ) progress ON TRUE
 WHERE o.status = 'published'
   AND o.reason = ANY(%(reasons)s)
   AND o.payload ? 'permissions'
   AND NOT EXISTS (
         SELECT 1 FROM publish_outbox newer
          WHERE newer.user_id = o.user_id
            AND newer.permission_version > o.permission_version
       )
   AND NOT EXISTS (
         SELECT 1 FROM mcp_sync_check c
          WHERE c.user_id = o.user_id
            AND c.permission_version = o.permission_version
            AND c.result = ANY(%(terminal)s)
       )
   AND (
         progress.attempts = 0
      OR progress.first_started_at + make_interval(
             secs => LEAST(
                 progress.attempts * %(interval)s, %(budget)s
             )::double precision
         ) <= now()
       )
 ORDER BY (progress.attempts > 0), o.published_at, o.id
 LIMIT %(limit)s
"""


def _row_to_claimed_publish(row: Any, current_version: int | None) -> ClaimedPublish:
    return ClaimedPublish(
        outbox_id=str(row[0]),
        user_id=str(row[1]),
        permission_version=int(row[2]),
        payload=dict(row[3] or {}),
        attempts=int(row[4]),
        current_permission_version=current_version,
        # **这条意图自己建过的那一行**（``created_record_id``，不是审计用的
        # ``external_record_id``）。判定层用它回答"这一行是不是我们建的"；既有
        # 历史行永远是 NULL，因此"密文被改写"那条判定不会误伤它们。
        created_record_id=None if row[5] is None else str(row[5]),
    )


class PostgresPermissionPublishStore(_DecisionMixin):
    """发布意图 outbox 的读写。构造时不连接数据库，每次调用自带连接（adapters 既有惯例）。"""

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        """记下 DSN 与超时配置；不在构造时连接数据库。"""
        self._dsn = dsn
        self._timeouts = timeouts

    # ------------------------------------------------------------------
    # 消费侧
    # ------------------------------------------------------------------

    def claim_next(self, *, exclude: Sequence[str] = ()) -> ClaimedPublish | None:
        """认领最早一条可发布的意图，并原子记账（状态转 ``publishing``、尝试次数 +1）。

        三道条件缺一不可：``FOR UPDATE SKIP LOCKED``；「该用户没有更早的非
        终态兄弟行」而不是「有没有 publishing」（后者在 READ COMMITTED 下
        有真实漏洞窗口，见 :data:`_CLAIM_NEXT_SQL` 上方注记）；``exclude``
        本轮已认领过的 id，且必须同时排除在兄弟行判据外，否则同一用户的
        两条意图会在同一轮里都被放行。一并取回该用户当前版本，使"旧版本
        不覆盖新版本"成为纯判定。
        """
        skipped = [str(item) for item in exclude]
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(_CLAIM_NEXT_SQL, (skipped,))
                row = cursor.fetchone()
                if row is None:
                    return None
                # 当前权限版本在**同一个事务**里读，与认领是同一时刻的事实；分成两次
                # 连接会让「认领到的是不是最新版本」建立在两个不同时刻的观察上。
                cursor.execute("SELECT permission_version FROM app_user WHERE id = %s", (row[1],))
                current = cursor.fetchone()
        return _row_to_claimed_publish(row, None if current is None else int(current[0]))

    def complete(self, attempt: PublishAttempt, *, status: str) -> None:
        """把一次尝试的结果记回意图行，绑定到本次认领。

        只判 ``status = 'publishing'`` 不够：一条被 :meth:`reclaim_stale`
        放回 ``pending``、又被**另一个**消费者重新认领的意图，此刻状态恰好
        也是 ``publishing``——旧认领者迟到的记账会命中它，把新认领者正在
        进行的那一次改写掉。``attempts`` 每次认领自增，是"哪一次认领"的
        天然版本号，因此加进 ``WHERE`` 就能让旧认领者如实拿到
        :class:`PublishClaimLostError`。两列语义见 :func:`_created_record_id`。
        """
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                _COMPLETE_PUBLISH_SQL,
                {
                    "status": status,
                    "outcome": attempt.outcome.value,
                    "detail": _error_detail(attempt),
                    "record_id": attempt.external_record_id,
                    "created_id": _created_record_id(attempt),
                    "id": attempt.outbox_id,
                    "attempts": attempt.attempts,
                },
            )
            if cursor.rowcount != 1:
                raise PublishClaimLostError(
                    f"发布意图记账失败，认领已丢失：outbox={attempt.outbox_id}"
                )

    def reclaim_stale(self, *, older_than: timedelta = DEFAULT_RECLAIM_AFTER) -> int:
        """把卡在 ``publishing`` 超过 ``older_than`` 的意图放回 ``pending``，返回条数。

        这是**重启恢复**：进程在外部写入与记账之间崩溃时，那条意图会一直占着该用户的
        单飞名额。放回去安全，因为发布本身幂等（先查后写，收敛到同一行、同一份内容）。
        """
        if not isinstance(older_than, timedelta) or older_than <= timedelta(0):
            raise ValueError("回收阈值必须是正的时间间隔")
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(_RECLAIM_STALE_PUBLISH_SQL, (older_than,))
            reclaimed = cursor.rowcount
        if reclaimed:
            logger.warning("回收滞留的发布意图 条数=%s", reclaimed)
        return reclaimed

    def redact_expired_payloads(self, *, now: datetime | None = None, limit: int = 500) -> int:
        """把过了九十天上限的内容快照擦成空对象，返回擦除条数。

        擦的是 ``payload``（含邮箱与姓名）与 ``last_error``；``user_id``、权限版本、
        状态与时间戳留下——它们是「谁的哪一版权限什么时候发布成功过」这类运行事实，
        本身不可再映射到人。**过期的意图擦掉内容之后就发不出去了**，这是对的：
        一份九十天前决定的权限不该在今天被写进外部表格；它下一次被认领时会以
        ``invalid`` 收敛并停止重试。
        """
        moment = now or datetime.now(_UTC)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("到期判定时间必须带时区")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(_REDACT_EXPIRED_PAYLOADS_SQL, (moment, limit))
            redacted = cursor.rowcount
        if redacted:
            logger.info("发布意图内容已到期擦除 条数=%s", redacted)
        return redacted

    # ------------------------------------------------------------------
    # 发布之后：该给谁做就绪确认、该给谁发通知
    # ------------------------------------------------------------------

    def published_awaiting_readiness(
        self,
        *,
        reasons: Sequence[str],
        interval_seconds: int,
        budget_seconds: int,
        limit: int = 50,
    ) -> tuple[PendingReadiness, ...]:
        """取「已经发布读回一致、但就绪确认还没收口」的那些 ``(用户, 权限版本)``。

        每轮 tick 的输入。``reasons`` 必填：挡的不是数据错误，而是两个编排者
        抢同一条意图——不同 reason 各自负责各自的确认与通知，见
        :meth:`_validate_readiness_query` 与 :data:`_PUBLISHED_AWAITING_READINESS_SQL`
        上方的四条筛选注记。``payload`` 过期会被 :meth:`redact_expired_payloads`
        擦成 ``'{}'``，因此这里要求 ``payload`` 里确有 ``permissions`` 键。
        """
        wanted = self._validate_readiness_query(
            reasons=reasons,
            interval_seconds=interval_seconds,
            budget_seconds=budget_seconds,
            limit=limit,
        )
        terminal = sorted(item.value for item in TERMINAL_OUTCOMES)
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                _PUBLISHED_AWAITING_READINESS_SQL,
                {
                    "reasons": wanted,
                    "terminal": terminal,
                    "interval": interval_seconds,
                    "budget": budget_seconds,
                    "limit": limit,
                },
            )
            rows = cursor.fetchall()
        return tuple(
            PendingReadiness(
                user_id=str(row[0]),
                permission_version=int(row[1]),
                permissions=str(row[2]),
                published_at=row[3],
            )
            for row in rows
        )

    @staticmethod
    def _validate_readiness_query(
        *, reasons: Sequence[str], interval_seconds: int, budget_seconds: int, limit: int
    ) -> list[str]:
        """校验就绪确认查询的四个参数，返回规范化后的 ``reasons`` 列表。

        全部必填而不给默认值：默认值会在新增一种 reason 时**静默地**把它
        归给某一方，而那正是需要有人明确决定的事。返回**列表**而不是元组：
        psycopg 把 ``= ANY(%(reasons)s)`` 的参数适配成 PostgreSQL 数组字面量
        只认 list（或其他显式数组类型），tuple 会被适配成复合类型语法、
        在这条语句里报 ``malformed array literal``。
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        for label, value in (("轮询间隔", interval_seconds), ("总预算", budget_seconds)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{label}必须是正整数秒")
        wanted = [str(item) for item in reasons if str(item).strip()]
        if not wanted:
            raise ValueError("必须指明本调用方负责确认哪些 reason 的发布意图")
        return wanted

    def has_publish_footprint(self, user_id: str) -> bool:
        """这个用户在发布链上**有没有留下过足迹**：发布成功过，**或**当前还有意图在途。

        两半各自都不能少：``published``——真的往发布表写成功过一行，那一
        行现在还在，值得为他发一条清空 ``permissions`` 的更新；
        ``pending``/``publishing``——还有一条尚未落地的授权意图，不算进来
        会让"昨天排的授权还堵在 pending、今天被撤权却因为没发布过而跳过"
        这种积压在发布面消费时把已收回的范围重新写进外部表。``failed``/
        ``superseded`` 不算足迹：前者从未落到外部表，后者已被更新版本取代。
        """
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("发布足迹判定必须指明用户")
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """SELECT 1 FROM publish_outbox
                    WHERE user_id = %s
                      AND status IN ('published', 'pending', 'publishing')
                    LIMIT 1""",
                (user_id,),
            )
            return cursor.fetchone() is not None

    def notice_recipient_open_id(self, user_id: str) -> str | None:
        """权限变化通知发给谁：该用户的 ``feishu_open_id``；不该发就返回 ``None``。

        两条过滤是产品约束：``provisioning_state = 'active'``（还没完成
        开通的人不该收到通知）；``account_state = 'enabled'`` 正向白名单
        （删除中/已删除/已停用一律不发，与 :data:`PERMISSION_REFRESH_BASELINE_SQL`
        上方注记同一条演进防御）。本方法只读一列，不影响
        :class:`PostgresPermissionRefreshBaselineReader` 那份独立判据。
        """
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("通知收件人查询必须指明用户")
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """SELECT feishu_open_id
                     FROM app_user
                    WHERE id = %s
                      AND provisioning_state = 'active'
                      AND account_state = 'enabled'""",
                (user_id,),
            )
            row = cursor.fetchone()
        if row is None or row[0] is None:
            return None
        open_id = str(row[0]).strip()
        return open_id or None

    def load(self, outbox_id: str) -> StoredIntent | None:
        """回读一条意图。给运维排查与用例断言用，不在发布链路上。"""
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """SELECT id, user_id, permission_version, reason, payload, status, attempts,
                          last_outcome, last_error, external_record_id, published_at
                     FROM publish_outbox WHERE id = %s""",
                (outbox_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return StoredIntent(
            outbox_id=str(row[0]),
            user_id=str(row[1]),
            permission_version=int(row[2]),
            reason=str(row[3]),
            payload=dict(row[4] or {}),
            status=str(row[5]),
            attempts=int(row[6]),
            last_outcome=row[7],
            last_error=row[8],
            external_record_id=row[9],
            published_at=row[10],
        )


#: 每日权限重算真正遍历的那一份基线。**刻意不是**
#: ``adapters/postgres_roster_audit.ACTIVE_BASELINE_SQL``——那份服务花名册
#: 日报/审计对比，必须覆盖包括 ``suspended`` 在内的一切"还没删除"的用户；
#: 发权批必须**额外**排除 ``suspended``，否则次日批量重算会把停用当天
#: 已清空的发布行重新写回一份真实权限，见 :class:`PostgresPermissionRefreshBaselineReader`。

#: **正向白名单，不是拒绝列表**：``= 'enabled'`` 与原来的
#: ``NOT IN ('deleting','deleted','suspended')`` 逐行等价，但将来 CHECK
#: 新增第五个状态时，白名单默认拒绝，拒绝列表会默默放行。
PERMISSION_REFRESH_BASELINE_SQL = """
SELECT id, feishu_user_id, display_name, employee_no, email
  FROM app_user
 WHERE provisioning_state = 'active'
   AND account_state = 'enabled'
 ORDER BY id
"""


class PostgresPermissionRefreshBaselineReader:
    """每日权限重算专用的基线读取，实现调用方 ``_BaselineReader`` 协议。

    行形状与 ``adapters/postgres_roster_audit.PostgresRosterBaselineReader``
    逐字段相同（同样是 :class:`ArchivedIdentity` 的五个字段），**唯一差别
    是过滤条件多排除一个 `suspended`**（见 :data:`PERMISSION_REFRESH_BASELINE_SQL`
    上方注记）。两条查询**必须**各自独立、按各自产品口径演进——共用一份
    "看起来一样"的 SQL 正是停用当天被清空的发布行次日又被重新聚合出来
    这个缺口的根源，不能靠给 ``PostgresRosterBaselineReader`` 加一个参数化
    选项收敛。
    """

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        """记下 DSN 与超时配置；不在构造时连接数据库。"""
        self._dsn = dsn
        self._timeouts = timeouts

    def load_active_baseline(self) -> tuple[ArchivedIdentity, ...]:
        """返回本轮重算集：内部标识 + 人员 ID 与存档三字段。

        与 ``PostgresRosterBaselineReader.load_active_baseline`` 取的列
        逐字段相同，只是行集合按 :data:`PERMISSION_REFRESH_BASELINE_SQL`
        多排除 ``suspended``。
        """
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(PERMISSION_REFRESH_BASELINE_SQL)
            rows = cursor.fetchall()

        baseline = tuple(
            ArchivedIdentity(
                app_user_id=str(row[0]),
                personnel_id=_text(row[1]),
                display_name=_text(row[2]),
                employee_no=_text(row[3]),
                email=_text(row[4]),
            )
            for row in rows
        )
        # 只记条数，同 `PostgresRosterBaselineReader`（`V-花名册-33`：审计与日志
        # 不含花名册字段值）。
        logger.info("每日权限重算基线已读取 已开通且未停用用户=%s", len(baseline))
        return baseline


def _text(value: object) -> str:
    """``NULL`` 与空白归一为空串。

    与 ``postgres_roster_audit._text`` 同一姿态——各自一份是因为两个模块
    刻意不互相 import 对方的私有辅助函数。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _created_record_id(attempt: PublishAttempt) -> str | None:
    """出身只由"create 明确返回了记录标识"这一种事实设置。

    ``created_record_id``（自己建过的那一行）与 ``external_record_id``
    （审计，任何尝试都写）不能合并——混用会让既有历史行的一次更新读回
    不明被永久判成 ``mismatch``。创建结果不明时保持 ``NULL``：那时无法
    区分"自己建的"与"并发写入方建的"，重试按普通路径收敛。重复创建取
    最近一次，让检查作用在真正活着的那一行上。
    """
    return attempt.external_record_id if attempt.action == "create" else None


def _error_detail(attempt: PublishAttempt) -> str | None:
    """记进 ``last_error`` 的诊断串：**只有错误码与字段名**，没有任何字段值。"""
    if attempt.outcome is PublishOutcome.PUBLISHED:
        return None
    parts = [attempt.error_code or attempt.outcome.value]
    if attempt.mismatch_fields:
        parts.append("fields=" + ",".join(attempt.mismatch_fields))
    if attempt.detail:
        parts.append(attempt.detail)
    return " ".join(parts)[:500]
