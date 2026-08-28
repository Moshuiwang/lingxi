"""``onboarding_failure`` 表（迁移 ``0077``）的唯一 PostgreSQL 落点：写入方
:class:`PostgresFailureReasonRecorder`（[Issue #337](https://github.com/Moshuiwang/lingxi/issues/337)
两个写出点共用同一个实现，见
``lingxi.core.identity.onboarding_ports.FailureReasonRecorder`` 协议文档）与
只读查询 :func:`fetch_failure_reason`（供 ``/admin trace`` 消费）。

## 为什么写入方是「单条语句 INSERT」，不是加入某个更大的事务

`onboarding.result`/`stalled_provisioning.aborted` 两个审计写出点本身只是结构化
日志调用（``_AuditSink.record``），紧邻它们的调用栈里没有一个已经打开、可供加入的
数据库事务——本仓库的适配器写入约定本来就是每次调用各自 ``with connect(...) as
connection`` 独立开合（见 ``代码框架.md`` 「六、依赖与迁移」表格与本仓库其余
adapters 模块的既有写法）。这里是在现有写入模型下能做到的最强原子性：一条同步执行
的单语句 INSERT，紧邻既有审计调用，不是异步、不是"最终会补写"的旁路。
"""

from __future__ import annotations

from dataclasses import dataclass

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect


class PostgresFailureReasonRecorder:
    """``FailureReasonRecorder`` 协议的真实实现：``INSERT ... ON CONFLICT
    (trace_id) DO NOTHING``——``trace_id`` 是主键，同一条链正常只产生一次终态；
    若同一个 ``trace_id`` 被处理第二次（例如进程重启后重跑），先落的那一行保持
    不变，不覆盖、不报错（见迁移 ``0077`` 文件头部「幂等」一节）。"""

    def __init__(
        self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS
    ) -> None:
        self._dsn = dsn
        self._timeouts = timeouts

    def record_failure(self, *, trace_id: str, failure_reason: str, event_type: str) -> None:
        if not isinstance(trace_id, str) or not trace_id.strip():
            raise ValueError("trace_id 不能为空")
        if not isinstance(failure_reason, str) or not failure_reason.strip():
            raise ValueError("failure_reason 不能为空")
        if event_type not in _KNOWN_EVENT_TYPES:
            # 与迁移 0077 的 CHECK 约束同一取值范围；提前在应用层拒绝，不依赖
            # 打到数据库才发现字面量拼错——这是唯一两个已知调用方各自的固定
            # 字面量，不接受任意字符串（见迁移文件头部「数据来源」一节）。
            raise ValueError(f"未知的 event_type：{event_type!r}")
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO onboarding_failure (trace_id, failure_reason, event_type)
                VALUES (%s, %s, %s)
                ON CONFLICT (trace_id) DO NOTHING
                """,
                (trace_id.strip(), failure_reason.strip(), event_type),
            )


#: 与迁移 0077 的 CHECK 约束逐字一致——见该迁移文件头部「数据来源」一节列出的
#: 两个调用方。
_KNOWN_EVENT_TYPES = frozenset({"onboarding.result", "stalled_provisioning.aborted"})


@dataclass(frozen=True)
class FailureReasonRow:
    """``onboarding_failure`` 一行的只读投影，供 ``/admin trace`` 消费。"""

    failure_reason: str
    event_type: str
    occurred_at: str


def fetch_failure_reason(
    dsn: str, *, trace_id: str, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS
) -> FailureReasonRow | None:
    """按 ``trace_id`` 查一行失败原因；查无返回 ``None``（"这条链没有失败记录"，
    不是"查询失败"——两者由调用方分别处理，本函数不用异常表达"没找到"）。"""

    with connect(dsn, timeouts=timeouts) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT failure_reason, event_type, occurred_at
              FROM onboarding_failure
             WHERE trace_id = %s
            """,
            (trace_id,),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    failure_reason, event_type, occurred_at = row
    isoformat = getattr(occurred_at, "isoformat", None)
    return FailureReasonRow(
        failure_reason=failure_reason,
        event_type=event_type,
        occurred_at=isoformat() if callable(isoformat) else str(occurred_at),
    )


__all__ = ["FailureReasonRow", "PostgresFailureReasonRecorder", "fetch_failure_reason"]
