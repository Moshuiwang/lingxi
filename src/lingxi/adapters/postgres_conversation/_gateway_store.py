"""事务工厂与开通交接账本（Issue #239 从 ``postgres_conversation.py`` 按读写边界
拆分而来）：``PostgresGatewayStore`` 既是 :class:`~lingxi.adapters.postgres_conversation._transaction._Transaction`
的唯一事务入口，也承载未开通首聊交接对账（Issue #65 轻审 P2-2）与队列失败提示的
独立发送权。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.conversation.ports import PendingOnboarding

from ._transaction import _Transaction

logger = logging.getLogger(__name__)

# 保留旧名称作为兼容导出；默认值的唯一来源是 adapters.postgres。
DEFAULT_CONNECT_TIMEOUT_SECONDS = DEFAULT_POSTGRES_TIMEOUTS.connect_timeout_seconds
DEFAULT_STATEMENT_TIMEOUT_MS = DEFAULT_POSTGRES_TIMEOUTS.statement_timeout_seconds * 1000


def _seconds_from_milliseconds(name: str, value: int) -> int:
    """把旧构造参数转成统一配置；不允许丢失精度地改变等待边界。"""

    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value % 1000:
        raise ValueError(f"{name} 必须是正整数秒的毫秒数")
    return value // 1000


class PostgresGatewayStore:
    """实现 ``core.conversation.ports.GatewayStore``。"""

    def __init__(
        self,
        dsn: str,
        *,
        timeouts: PostgresTimeouts | None = None,
        connect_timeout: int | None = None,
        statement_timeout_ms: int | None = None,
        lock_timeout_ms: int | None = None,
    ) -> None:
        self._dsn = dsn
        if timeouts is not None and any(
            value is not None for value in (connect_timeout, statement_timeout_ms, lock_timeout_ms)
        ):
            raise ValueError("PostgreSQL 超时只能通过 timeouts 或兼容参数中的一种提供")
        if timeouts is not None:
            self._timeouts = timeouts
        else:
            self._timeouts = PostgresTimeouts(
                connect_timeout_seconds=(
                    DEFAULT_CONNECT_TIMEOUT_SECONDS if connect_timeout is None else connect_timeout
                ),
                statement_timeout_seconds=(
                    DEFAULT_POSTGRES_TIMEOUTS.statement_timeout_seconds
                    if statement_timeout_ms is None
                    else _seconds_from_milliseconds("statement_timeout_ms", statement_timeout_ms)
                ),
                lock_timeout_seconds=(
                    DEFAULT_POSTGRES_TIMEOUTS.lock_timeout_seconds
                    if lock_timeout_ms is None
                    else _seconds_from_milliseconds("lock_timeout_ms", lock_timeout_ms)
                ),
            )

    @contextmanager
    def transaction(self) -> Iterator[_Transaction]:
        """一个连接、一个事务。异常时整体回滚。"""

        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                yield _Transaction(connection)

    def claim_queue_failure_notice(self, *, event_id: str) -> bool:
        """在独立事务取得一次队列失败提示的发送权。"""

        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    """
                    INSERT INTO queue_failure_notice (feishu_event_id, expires_at)
                    VALUES (%s, now())
                    ON CONFLICT (feishu_event_id) DO NOTHING
                    RETURNING feishu_event_id
                    """,
                    (event_id,),
                )
                return cursor.fetchone() is not None

    def mark_onboarding_dispatched(self, *, event_id: str) -> None:
        """记下"这条事件已经交给开通编排了"（迁移 0062、Issue #65 轻审 P2-2）。

        条件里带上 ``onboarding_dispatched_at IS NULL``：真正的交接时刻是第一次，
        重复调用不把时间戳往后推——那个时间戳是"账上什么时候平的"的唯一证据。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    """
                    UPDATE inbound_event
                       SET onboarding_dispatched_at = now()
                     WHERE feishu_event_id = %s
                       AND onboarding_dispatched_at IS NULL
                    """,
                    (event_id,),
                )

    def release_onboarding_claim(
        self, *, event_id: str, claim_token: datetime | None = None
    ) -> None:
        """把**自己那一次**认领放回 ``NULL``（Epic D / S-D-02 修复包）。

        它是 :meth:`claim_stale_onboarding` 唯一的反向路径——在它存在之前，任何「认领了却
        没跑成」的交错都会把事件永久烧掉。

        **条件是 CAS，不是 ``IS NOT NULL``。** 只按事件标识清空会有 ABA：A 释放 → B 重新
        认领 → A 的重试再释放一次 → **B 的认领被清掉**，那条链于是在没人看着的情况下被
        第三方解锁，可能被并发认领两次并重复触发外部开通。``onboarding_dispatched_at``
        本身就是认领代次（认领语句写进去的那个时刻），拿它比对即可，不需要新列。

        ``claim_token`` 为 ``None`` 时**什么都不做**：调用方没有认领代次，宁可留着不放，
        也不能撤销别人的认领。
        """

        if claim_token is None:
            logger.warning("释放开通认领缺少认领代次，本次不动账本 event=%s", event_id)
            return
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    """
                    UPDATE inbound_event
                       SET onboarding_dispatched_at = NULL
                     WHERE feishu_event_id = %s
                       AND onboarding_dispatched_at = %s
                    """,
                    (event_id, claim_token),
                )

    def claim_stale_onboarding(self, *, older_than: timedelta) -> PendingOnboarding | None:
        """认领一条超时仍未交接的未开通首聊事件，并原子记账。

        ``FOR UPDATE SKIP LOCKED`` + ``onboarding_dispatched_at IS NULL`` 两道条件
        一起保证多实例扫描时同一条只被一个实例拿走：跳过被别人锁住的行，拿到锁之后
        再确认它还没被记账。少了后半句，两个实例可以先后拿到同一行并各触发一次开通。

        ``user_open_id`` 理论上可空（列本身允许 NULL），这里显式排除：交给编排的
        三元组缺了它就没有任何可匹配的身份，取回来也只能立刻失败。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    """
                    UPDATE inbound_event
                       SET onboarding_dispatched_at = now()
                     WHERE feishu_event_id IN (
                               SELECT feishu_event_id
                                 FROM inbound_event
                                WHERE handled_as = 'auto_provisioning'
                                  AND onboarding_dispatched_at IS NULL
                                  AND user_open_id IS NOT NULL
                                  AND received_at < now() - %s::interval
                                ORDER BY received_at
                                LIMIT 1
                                  FOR UPDATE SKIP LOCKED
                           )
                       AND onboarding_dispatched_at IS NULL
                    RETURNING feishu_event_id, user_open_id, trace_id,
                              onboarding_dispatched_at
                    """,
                    (older_than,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                # 第四列是**这一次认领的代次**：释放时拿它做 CAS，只能撤销自己那一次
                # （见 :meth:`release_onboarding_claim`）。
                return PendingOnboarding(
                    event_id=row[0], open_id=row[1], trace_id=row[2], claim_token=row[3]
                )
