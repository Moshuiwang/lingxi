"""调用数据库里的受限保留清理函数。

本模块**不写任何 DELETE**。九十天回收的全部逻辑（到期条件、批量上限、拒绝未来
时间、固定 search_path）都在迁移 `0054_retention_cleanup` 建立的
`public.lingxi_retention_cleanup(timestamptz, integer)` 里，属主是无登录的
`lingxi_retention_owner`。适配器只负责："按当前时间调用一次，把摘要拿回来"。

这条分工是设计的一部分而不是风格偏好（[数据库设计第十节]
(../../../docs/技术设计/数据库设计.md)）：删除权限只存在于那个函数所属的角色上，
应用与 scheduler 角色都没有目标表的 `DELETE`。清理逻辑如果搬到 Python 这一侧，
就必须把 `DELETE` 授给运行时角色，"误获普通 DELETE 也删不掉"这条保障随即消失。

返回摘要里只有表名、删除行数和被删行的到期时间范围——没有任何行内容，
因此摘要与由它产生的日志都不可能带出人员数据（断言 V-保留-14）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect

logger = logging.getLogger("lingxi.adapters.retention")

# 每张表每次调用删多少行。函数侧另有 1000 的硬上限，这里传的是常规节奏：
# 清理是常驻定时职责，宁可多跑几轮，也不要一次拿住大量行锁。
DEFAULT_BATCH_LIMIT = 200

CLEANUP_FUNCTION = "public.lingxi_retention_cleanup"


@dataclass(frozen=True)
class RetentionTableResult:
    """一张表在一次调用里的回收结果。"""

    table: str
    deleted: int
    oldest_expires_at: datetime | None
    newest_expires_at: datetime | None
    # 这一轮该表是否因为拿不到锁而整批让路。与"没有到期行"是两回事：
    # 两者 deleted 都是 0，只有这个标记能把它们分开。
    blocked: bool = False


@dataclass(frozen=True)
class RetentionReport:
    """一次清理调用的完整摘要。"""

    tables: tuple[RetentionTableResult, ...] = ()

    @property
    def deleted(self) -> int:
        return sum(result.deleted for result in self.tables)

    @property
    def blocked_tables(self) -> tuple[str, ...]:
        """本轮因锁等待超时而未能清理的表。非空即这一轮没做完。"""

        return tuple(result.table for result in self.tables if result.blocked)

    def summary(self) -> str:
        """写进日志的一行摘要。只有表名与计数，永远不取行内容。"""

        if not self.tables:
            return "保留清理：本轮无目标表"
        parts = [
            f"{result.table} 删除 {result.deleted} 行" + ("（锁等待超时，本轮让路）" if result.blocked else "")
            for result in self.tables
        ]
        return "保留清理：" + "；".join(parts)


class PostgresRetentionCleaner:
    """受限清理函数的调用方。构造时不连接数据库，每次调用自带事务。"""

    def __init__(
        self,
        dsn: str,
        *,
        batch_limit: int = DEFAULT_BATCH_LIMIT,
        timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS,
    ) -> None:
        if batch_limit <= 0:
            raise ValueError("保留清理的批量必须是正整数")
        self._dsn = dsn
        self._batch_limit = batch_limit
        self._timeouts = timeouts

    @property
    def batch_limit(self) -> int:
        return self._batch_limit

    def run_once(self) -> RetentionReport:
        """调用一次清理函数。

        `p_now` 用数据库自己的 `now()`，不用进程时钟：进程与数据库的时钟可能不同步，
        而函数会拒绝晚于它自己当前时间的 `p_now`——传进程时间等于把一次正常清理
        变成偶发失败。整个调用是一个事务，中途被信号打断只会整体回滚，不会留下
        删了父行没删子行的半删状态（断言 V-保留-17）。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT target_table, deleted_rows, oldest_expires_at, newest_expires_at, blocked "
                f"FROM {CLEANUP_FUNCTION}(now(), %s)",
                (self._batch_limit,),
            )
            rows: list[tuple[Any, ...]] = cursor.fetchall()

        return RetentionReport(
            tables=tuple(
                RetentionTableResult(
                    table=str(row[0]),
                    deleted=int(row[1]),
                    oldest_expires_at=row[2],
                    newest_expires_at=row[3],
                    blocked=bool(row[4]),
                )
                for row in rows
            )
        )
