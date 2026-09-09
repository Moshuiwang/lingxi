"""逐用户探针只在读取密文时占数据库槽，等待与查询共用有限截止。"""

import math
import time
from dataclasses import replace

from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_mcp_token import PostgresMcpTokenStore
from lingxi.core.admin.followup_budget import FollowupDatabaseBudget
from lingxi.core.permission.mcp_readiness_base import McpProbeError


class InnertestMcpTokens(PostgresMcpTokenStore):
    """保留既有令牌解密端口，仅约束短数据库读取的资源生命周期。"""

    def __init__(self, *args, db_slots=None, should_stop=None, timeout_seconds=5, **kwargs):
        """Scheduler 注入同一进程预算；独立运维调用使用自己的有限预算。"""
        super().__init__(*args, **kwargs)
        self.db_slots = db_slots if db_slots is not None else FollowupDatabaseBudget()
        self.should_stop = should_stop or (lambda: False)
        self.timeout_seconds = timeout_seconds

    def _remaining(self, deadline):
        """停止或截止后禁止取得新连接，也不继续执行下一条语句。"""
        seconds = deadline - time.monotonic()
        if self.should_stop() or seconds <= 0:
            raise McpProbeError("probe_cancelled_or_expired")
        return seconds

    def token_cipher(self, user_id):
        """借槽只包短读取；解密和 HTTP 等待均在归还后进行。"""
        deadline = time.monotonic() + self.timeout_seconds
        while not self.db_slots.acquire(timeout=min(0.1, self._remaining(deadline))):
            pass
        try:
            self._remaining(deadline)
            return self._read_cipher(user_id, deadline)
        finally:
            self.db_slots.release()

    def _read_cipher(self, user_id, deadline):
        """建连有限等待，SQL 再按实际剩余毫秒收紧服务端截止。"""
        seconds = self._remaining(deadline)
        timeouts = replace(
            self._timeouts,
            connect_timeout_seconds=min(
                self._timeouts.connect_timeout_seconds, max(1, math.ceil(seconds))
            ),
        )
        with connect(self._dsn, timeouts=timeouts) as connection, connection.cursor() as cursor:
            milliseconds = max(
                1,
                min(
                    self._timeouts.statement_timeout_seconds * 1000,
                    int(self._remaining(deadline) * 1000),
                ),
            )
            cursor.execute("SELECT set_config('statement_timeout',%s,true)", (f"{milliseconds}ms",))
            self._remaining(deadline)
            cursor.execute("SELECT token_cipher FROM mcp_access_token WHERE user_id=%s", (user_id,))
            row = cursor.fetchone()
            self._remaining(deadline)
        return None if row is None else str(row[0])
