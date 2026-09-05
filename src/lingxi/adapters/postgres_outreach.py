"""主动发送的持久面：发送记录的认领/记账/回查，以及收件人事实的一次读齐。

两个类刻意分开：:class:`PostgresOutreachStore` 是幂等与审计的落点（写），
:class:`PostgresOutreachSubjects` 只读，把「邮箱 → 花名册 → app_user → 已发布权限」
这条定位链一次查完。跨表读放在 adapters 是既定例外（同
``postgres_late_readiness_recovery``）：定位链天然要联合判断，拆成几处各读一截
反而让"哪一步把谁挡住了"不可审查。

**幂等靠数据库，不靠调用方记性**：``reserve`` 是一条
``INSERT ... ON CONFLICT (dedupe_key)``，已经 ``delivered`` 的行原样返回、不递增
尝试次数，调用方据此跳过。正文、姓名、邮箱都不进这张表，也不进日志。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.ids import new_id
from lingxi.core.outreach.audience import SubjectFacts
from lingxi.core.outreach.dispatch import ReservedRecord

logger = logging.getLogger(__name__)

#: 认领一条记录：新行直接算第一次尝试；已存在且**未送达、且收件人没变**的行递增
#: 尝试次数、回到 ``pending``，并把内容版本与样式刷成这一次真正要发的那一版（重试
#: 发的是现在这份内容，记录却留着上一次的版本，回查就成了假账）。已送达的行、以及
#: 记着另一个收件人的行都落进 ``WHERE`` 之外，一行不改、什么都不返回。
_RESERVE_SQL = """
INSERT INTO outreach_message
     (id, recipient_open_id, user_id, purpose, content_key, content_version,
      card_style, dedupe_key, attempts)
VALUES (%(id)s, %(open_id)s, %(user)s, %(purpose)s, %(content_key)s,
        %(content_version)s, %(card_style)s, %(dedupe)s, 1)
ON CONFLICT (dedupe_key) DO UPDATE
   SET attempts = outreach_message.attempts + 1,
       status = 'pending',
       content_version = EXCLUDED.content_version,
       card_style = EXCLUDED.card_style
 WHERE outreach_message.status <> 'delivered'
   AND outreach_message.recipient_open_id = EXCLUDED.recipient_open_id
RETURNING id, status, attempts, recipient_open_id
"""

_EXISTING_SQL = (
    "SELECT id, status, attempts, recipient_open_id FROM outreach_message WHERE dedupe_key = %s"
)

#: 真发之前重读一次状态：装配与发送之间这个人可能已被停用。只取两列，不重跑定位链。
_STATE_AT_SEND_SQL = "SELECT provisioning_state, account_state FROM app_user WHERE id = %s"

_DELIVERED_KEYS_SQL = (
    "SELECT dedupe_key FROM outreach_message WHERE dedupe_key = ANY(%s) AND status = 'delivered'"
)

#: 回查只取事实，**不取正文**（这张表里本来也没有正文）。
_RECENT_SQL = """
SELECT recipient_open_id, purpose, content_key, content_version, card_style,
       status, attempts, created_at, delivered_at, message_id, last_error
  FROM outreach_message
 ORDER BY created_at DESC, id DESC
 LIMIT %s
"""

#: 定位链的一次读齐：邮箱在 app_user 上已有唯一索引（迁移 ``0085``），因此这里
#: 不需要"多命中就跳过"的第二道判定；花名册那一侧才可能一个邮箱多行，由
#: ``core/outreach/audience`` 按姓名是否唯一决定发不发。
_SUBJECT_SQL = """
SELECT u.id, u.feishu_open_id, u.provisioning_state, u.account_state,
       (SELECT o.payload ->> 'permissions'
          FROM publish_outbox o
         WHERE o.user_id = u.id
           AND o.permission_version = u.permission_version
           AND o.status = 'published'
         ORDER BY o.created_at DESC
         LIMIT 1)
  FROM app_user u
 WHERE lower(btrim(u.email)) = %s
 LIMIT 1
"""

_ROSTER_NAMES_SQL = "SELECT DISTINCT name FROM roster_snapshot_row WHERE lower(btrim(email)) = %s"


@dataclass(frozen=True)
class OutreachRecordView:
    """一条可回查的发送记录。**没有正文**，因此可以直接打印。"""

    recipient_open_id: str
    purpose: str
    content_key: str
    content_version: str
    card_style: str
    status: str
    attempts: int
    created_at: datetime
    delivered_at: datetime | None
    message_id: str | None
    last_error: str | None


class PostgresOutreachStore:
    """发送记录的读写口。构造时不连接数据库，每次调用自带连接（adapters 既有惯例）。"""

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        """记下 DSN 与超时配置；不在构造时连接数据库。"""
        self._dsn = dsn
        self._timeouts = timeouts

    def reserve(
        self,
        *,
        recipient_open_id: str,
        user_id: str | None,
        purpose: str,
        dedupe_key: str,
        content_key: str,
        content_version: str,
        card_style: str,
    ) -> ReservedRecord:
        """认领或复用一条记录。

        已经 ``delivered`` 的键**一行都不改**（``ON CONFLICT DO UPDATE`` 带 ``WHERE``
        守卫），因此同一份名单重跑既不新增记录也不再发一次；返回的状态让调用方
        直接跳过。记着另一个收件人的键同样一行不改，返回记录里那个 open_id，由调用方
        拒发。
        """
        parameters = {
            "id": new_id("omr"),
            "open_id": recipient_open_id,
            "user": user_id,
            "purpose": purpose,
            "content_key": content_key,
            "content_version": content_version,
            "card_style": card_style,
            "dedupe": dedupe_key,
        }
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(_RESERVE_SQL, parameters)
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(_EXISTING_SQL, (dedupe_key,))
                    row = cursor.fetchone()
        if row is None:  # 既没插进去也查不到：不当成"可以发"，让调用方看见异常
            raise LookupError("发送记录认领失败：既未新建也未查到既有记录")
        return ReservedRecord(
            record_id=str(row[0]),
            dedupe_key=dedupe_key,
            status=str(row[1]),
            attempts=int(row[2]),
            recipient_open_id=str(row[3]),
        )

    def mark_delivered(self, record_id: str, *, message_id: str | None) -> bool:
        """记成已送达（终态），并留下平台回读标识供双通道核对。

        带 ``status <> 'delivered'`` 守卫并核对影响行数：一条已经是终态（或已被账号
        删除带走）的记录不再改写，返回 ``False`` 让调用方留一条审计。**不抛**——卡片
        此时已经发出去了，把"记账没改到行"抛成异常会让它被读成一次投递失败。
        """
        updated = self._execute(
            "UPDATE outreach_message"
            "   SET status = 'delivered', delivered_at = now(), message_id = %s,"
            "       last_error = NULL"
            " WHERE id = %s AND status <> 'delivered'",
            (message_id, record_id),
        )
        if not updated:
            logger.error("发送记录已是终态或已不存在，未改写 记录=%s", record_id)
        return updated

    def mark_failed(self, record_id: str, *, error: str) -> None:
        """记一次失败与错误码；这一条仍可重试（下一次 ``reserve`` 会递增尝试次数）。

        带 ``status <> 'delivered'`` 守卫：一条已经送达的记录不允许被后来的失败
        改写成 ``failed``，那会让回查读成"这个人没收到"。
        """
        self._execute(
            "UPDATE outreach_message SET status = 'failed', last_error = %s"
            " WHERE id = %s AND status <> 'delivered'",
            (str(error)[:500], record_id),
        )

    def delivered_dedupe_keys(self, keys: Sequence[str]) -> frozenset[str]:
        """这些幂等键里哪些已经送达——dry-run 的"是否已发过"就是它。"""
        if not keys:
            return frozenset()
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(_DELIVERED_KEYS_SQL, (list(keys),))
            rows = cursor.fetchall()
        return frozenset(str(row[0]) for row in rows)

    def recent_records(self, *, limit: int = 200) -> tuple[OutreachRecordView, ...]:
        """回查最近的发送记录（发给谁 / 内容键＋版本 / 何时 / 结果）。"""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(_RECENT_SQL, (limit,))
            rows = cursor.fetchall()
        return tuple(_row_to_view(row) for row in rows)

    def _execute(self, sql: str, parameters: tuple[Any, ...]) -> bool:
        """执行一条写，返回是否真的改到了行。"""
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(sql, parameters)
            return cursor.rowcount > 0


def _row_to_view(row: Any) -> OutreachRecordView:
    return OutreachRecordView(
        recipient_open_id=str(row[0]),
        purpose=str(row[1]),
        content_key=str(row[2]),
        content_version=str(row[3]),
        card_style=str(row[4]),
        status=str(row[5]),
        attempts=int(row[6]),
        created_at=row[7],
        delivered_at=row[8],
        message_id=str(row[9]) if row[9] else None,
        last_error=str(row[10]) if row[10] else None,
    )


class PostgresOutreachSubjects:
    """按邮箱一次读齐收件人事实：app_user 一行 + 已发布权限 + 花名册姓名。"""

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        """记下 DSN 与超时配置；不在构造时连接数据库。"""
        self._dsn = dsn
        self._timeouts = timeouts

    def facts_for(self, email: str) -> SubjectFacts:
        """读一个人的全部事实；查不到时返回只带邮箱的空壳，由装配层判跳过。

        邮箱按 ``lower(btrim(...))`` 归一比对，与 ``account_match.normalize_email``
        的口径一致；查不到不抛异常——"这个人不在库里"是一条清单上要显示的结论，
        不是一次故障。
        """
        normalized = (email or "").strip().lower()
        if not normalized:
            raise ValueError("邮箱不能为空")
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(_SUBJECT_SQL, (normalized,))
            user_row = cursor.fetchone()
            cursor.execute(_ROSTER_NAMES_SQL, (normalized,))
            name_rows = cursor.fetchall()
        names = tuple(str(row[0]) for row in name_rows if row[0])
        if user_row is None:
            return SubjectFacts(email=normalized, roster_names=names)
        return SubjectFacts(
            email=normalized,
            user_id=str(user_row[0]),
            open_id=str(user_row[1]) if user_row[1] else None,
            provisioning_state=str(user_row[2]) if user_row[2] else None,
            account_state=str(user_row[3]) if user_row[3] else None,
            permissions=str(user_row[4]) if user_row[4] else None,
            roster_names=names,
        )

    def state_for(self, user_id: str) -> tuple[str | None, str | None]:
        """真发之前重读这个人的开通与账号状态（一次短查询）。

        与 :meth:`facts_for` 分开：那一条是"这个人该不该在名单里"的定位链，这一条只
        回答"就在按下发送的这一刻，他还是不是 active"。查不到人时返回两个 ``None``，
        由调用方按不可发送处理。
        """
        if not (user_id or "").strip():
            raise ValueError("用户标识不能为空")
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(_STATE_AT_SEND_SQL, (user_id,))
            row = cursor.fetchone()
        if row is None:
            return None, None
        return (str(row[0]) if row[0] else None, str(row[1]) if row[1] else None)


__all__ = [
    "OutreachRecordView",
    "PostgresOutreachStore",
    "PostgresOutreachSubjects",
]
