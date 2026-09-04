"""``PostgresTaskQueue`` 的组合定义。

把 ``_queue_base.py``/``_queue_lifecycle.py``/``_queue_outbox.py``/
``_queue_session_cleanup.py``/``_queue_gateway_delivery.py`` 五个按读写边界拆开
的基类与 mixin 组合成对外唯一可见的类名；各 mixin 互不重名，
``self.<method>`` 的解析结果由 MRO 唯一确定。
"""

from __future__ import annotations

from ._queue_base import _TaskQueueBase
from ._queue_gateway_delivery import _GatewayDeliveryMixin
from ._queue_lifecycle import _TaskLifecycleMixin
from ._queue_outbox import _OutboxMixin
from ._queue_session_cleanup import _SessionCleanupMixin


class PostgresTaskQueue(
    _TaskLifecycleMixin,
    _OutboxMixin,
    _SessionCleanupMixin,
    _GatewayDeliveryMixin,
    _TaskQueueBase,
):
    """worker 与 scheduler 侧的队列操作。

    worker 与 scheduler 共用这些原子操作。所有连接都经过仓库唯一的 PostgreSQL
    factory；没有连接字符串或 psycopg 直连旁路。
    """
