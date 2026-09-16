"""在调用方短事务内读取同一完整花名册，供无实时凭据的邮箱入口核对绑定。"""

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from lingxi.core.identity.email_resolver import EmailIdentitySnapshot

logger = logging.getLogger(__name__)


def record_binding_check(check, *, origin):
    """共享绑定核对的来源与结论，不记录邮箱、姓名、权限或凭据。"""
    logger.info(
        "identity.email_binding_checked origin=%s facts=%s",
        origin,
        json.dumps(check.audit_facts(), ensure_ascii=False),
    )


def load_email_snapshot(connection, *, max_age=None):
    """只读锁避免元信息与行跨快照；缺失、计数不一致或到期均保持不可用。"""
    with connection.cursor() as cursor:
        cursor.execute("SELECT id,captured_at,row_count FROM roster_snapshot FOR SHARE")
        facts = cursor.fetchone()
        if facts is None:
            return EmailIdentitySnapshot(None, None, None, available=False)
        cursor.execute(
            "SELECT personnel_id,email,employee_no,name FROM roster_snapshot_row WHERE snapshot_id=%s ORDER BY row_index",
            (facts[0],),
        )
        rows = tuple(
            dict(personnel_id=r[0], email=r[1], employee_no=r[2], name=r[3])
            for r in cursor.fetchall()
        )
    available = len(rows) == facts[2] and facts[2] > 0
    if max_age is not None and facts[1] <= datetime.now(UTC) - max_age:
        available = False
    return EmailIdentitySnapshot(rows, facts[0], facts[1], available=available)


class RosterRows:
    """把花名册持久快照折成"当前全部行或 ``None``"。

    **不加新鲜度判据**：每日重算要求快照是"今天的"，因为它是一次全量重算；而一次首聊
    开通如果因为快照晚了两小时就告诉用户"没有可用的银河权限"，那是一句错话。快照的新鲜度
    由花名册审计职责保证，这里只区分"有"和"根本没有"。
    """

    def __init__(self, store: Any) -> None:
        """包住一个花名册持久快照读取口。"""
        self._store = store

    def rows(self) -> Sequence[Mapping[str, Any]] | None:
        """当前全部花名册行，没有快照时返回 ``None``。"""
        snapshot = self._store.load()
        return None if snapshot is None else snapshot.rows

    def identity_snapshot(self):
        """一次读取保留身份判定所需的版本、时间与完整行，不伪造新鲜度。"""
        snapshot = self._store.load()
        if snapshot is None:
            return EmailIdentitySnapshot(None, None, None, available=False)
        return EmailIdentitySnapshot(
            snapshot.rows, snapshot.facts.snapshot_id, snapshot.facts.captured_at
        )
