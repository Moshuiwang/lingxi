"""``PostgresTaskQueue`` 的组合定义（Issue #239）：把 ``_queue_base.py``/
``_queue_lifecycle.py``/``_queue_outbox.py``/``_queue_session_cleanup.py``/
``_queue_gateway_delivery.py`` 五个按读写边界拆开的基类与 mixin 组合成对外唯一
可见的类名。拆分只搬动方法定义的物理位置，不改变任何方法体、调用顺序或 MRO
以外的行为——各 mixin 互不重名，`self.<method>` 的解析结果与拆分前完全一致。
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
