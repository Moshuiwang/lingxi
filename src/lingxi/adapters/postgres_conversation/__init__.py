"""会话、任务队列与入站事件的 PostgreSQL 存取。

沿用仓库既有惯例（``adapters/postgres_identity.py``）：``psycopg`` 在 ``__init__`` 里
延迟导入，构造时不连库，每次调用自带连接。

**事务边界是这个模块最重要的部分。** ``transaction()`` 交出的对象上才有写方法，
拿不到"事务外顺手写一条"的入口——`V-队列-01` 要求 ``inbound_event`` 插入、
``conversation`` 抢占、``task`` 插入落在同一事务里，这条约束由类型形状承担，
不靠调用方记得。

**包结构（Issue #239 拆分）**：本文件原是单个 1951 行的模块，现按读写边界拆成
本包下的多个子模块。**准确边界**：老单文件模块级命名空间里能查到 43 个非双下划线
名字（既有本模块自己定义的类/函数/常量，也含它从别处 ``import`` 进来、因此顺带
可从这个模块路径拿到的名字，如 ``connect``、``PostgresTimeouts``、
``ConversationRecord``、``UserState``、``new_id``）；本包 ``__init__.py`` 只显式
re-export 其中的 15 个（见下方 ``__all__``），另有 25 个不再从包顶层可导入——它们
要么是仅供内部使用的私有实现细节（``_user_state``、``_seconds_from_milliseconds``、
``_IDLE_SESSION_CLEANUP_SWEEP_LIMIT``、``_SYSTEM_DELIVERY_WORKER_ID``、
``_SYSTEM_TERMINAL_CONTENT_KEYS``，现分别在各自子模块内），要么是本来就该从其
本源模块导入、只是原单文件的模块级 import 顺带让它们可从这个路径拿到的名字
（``connect``/``PostgresTimeouts``/``DEFAULT_POSTGRES_TIMEOUTS`` 见
``lingxi.adapters.postgres``；``ConversationRecord``/``HandledAs``/
``PendingOnboarding``/``UserRecord``/``UserState`` 见
``lingxi.core.conversation.ports``；``ContentCatalog``/``default_content_catalog``
见 ``lingxi.config.content``；``DeliveryEventType``/``TerminalKind``/
``resolve_delivered_outcome`` 见 ``lingxi.core.delivery.ports``；``new_id`` 见
``lingxi.core.ids``；``Any``/``Iterator``/``Sequence``/``timedelta``/``dataclass``/
``contextmanager`` 均为标准库）。**既有调用点用到的名字全部保留**：全仓
（`src`/`tests`/`scripts`/`migrations`）扫描确认，本仓库里从
``lingxi.adapters.postgres_conversation`` 实际导入过的名字只有
``PostgresTaskQueue``/``PostgresTaskQueueListener``/``PostgresGatewayStore``/
``ClaimedTask``/``TerminalTask``/``TaskContext``/``AppendedEvent``/``_Transaction``/
``TASK_QUEUED_CHANNEL``，全部在 ``__all__`` 里；不代表其他未纳入本仓库扫描范围的
调用方（若存在）同样不受影响。子模块划分：

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
