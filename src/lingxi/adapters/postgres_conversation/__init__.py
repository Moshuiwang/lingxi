"""会话、任务队列与入站事件的 PostgreSQL 存取。

沿用仓库既有惯例（``adapters/postgres_identity.py``）：``psycopg`` 在 ``__init__`` 里
延迟导入，构造时不连库，每次调用自带连接。

**事务边界是这个模块最重要的部分。** ``transaction()`` 交出的对象上才有写方法，
拿不到"事务外顺手写一条"的入口——`V-队列-01` 要求 ``inbound_event`` 插入、
``conversation`` 抢占、``task`` 插入落在同一事务里，这条约束由类型形状承担，
不靠调用方记得。

**包结构（Issue #239 拆分）**：本文件原是单个 1951 行的模块，现按读写边界拆成
本包下的多个子模块，公开名字表保持不变——``from lingxi.adapters.postgres_conversation
import ...`` 的既有调用点无需改动。子模块划分：

- ``_transaction.py``：入站事件幂等去重、会话与话题（``_Transaction``）。
- ``_gateway_store.py``：事务工厂与开通交接账本（``PostgresGatewayStore``）。
- ``_dataclasses.py``：``PostgresTaskQueue`` 与 Gateway 消费共用的返回值形状。
- ``_queue_base.py``/``_queue_lifecycle.py``/``_queue_outbox.py``/
  ``_queue_session_cleanup.py``/``_queue_gateway_delivery.py``：``PostgresTaskQueue``
  按「任务领取与收口」「投递 outbox」「Agent 会话物理清理队列」「Gateway 投递
  消费」四条读写边界拆开的 mixin，在 ``_task_queue.py`` 里组合回同一个类。
- ``_listener.py``：``task_queued`` 的 LISTEN 适配器。
"""

from __future__ import annotations

import logging

from ._dataclasses import (
    AppendedEvent,
    ClaimedTask,
    DeliveryEventRecord,
    PendingDeliveryTask,
    SessionCleanupTask,
    TaskContext,
    TerminalTask,
    UncertainDeliveryTask,
)
from ._gateway_store import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_STATEMENT_TIMEOUT_MS,
    PostgresGatewayStore,
)
from ._listener import PostgresTaskQueueListener
from ._task_queue import PostgresTaskQueue
from ._transaction import TASK_QUEUED_CHANNEL, _Transaction

logger = logging.getLogger(__name__)

__all__ = [
    "TASK_QUEUED_CHANNEL",
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_STATEMENT_TIMEOUT_MS",
    "PostgresGatewayStore",
    "ClaimedTask",
    "TaskContext",
    "TerminalTask",
    "AppendedEvent",
    "DeliveryEventRecord",
    "PendingDeliveryTask",
    "UncertainDeliveryTask",
    "SessionCleanupTask",
    "PostgresTaskQueue",
    "PostgresTaskQueueListener",
    "_Transaction",
]
