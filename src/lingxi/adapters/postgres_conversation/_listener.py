"""``task_queued`` 通道的 LISTEN 适配器。

兜底轮询仍是必需的，见 ``_transaction.py`` 里 ``TASK_QUEUED_CHANNEL`` 的说明。
"""

from __future__ import annotations

from typing import Any

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect

from ._transaction import TASK_QUEUED_CHANNEL


class PostgresTaskQueueListener:
    """短生命周期 LISTEN 适配器；服务仍需配合轮询，不能只信 NOTIFY。"""

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        """记下 DSN 与超时配置；不在构造时连接数据库。"""
        self._dsn = dsn
        self._timeouts = timeouts
        self._connection: Any | None = None

    def __enter__(self) -> PostgresTaskQueueListener:
        self._connection = connect(
            self._dsn, timeouts=self._timeouts, autocommit=True, dedicated=True
        )
        self._connection.execute(f"LISTEN {TASK_QUEUED_CHANNEL}")
        return self

    def wait(self, *, timeout_seconds: float) -> bool:
        if self._connection is None:
            raise RuntimeError("监听器尚未进入上下文")
        for _notify in self._connection.notifies(timeout=timeout_seconds, stop_after=1):
            return True
        return False

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
