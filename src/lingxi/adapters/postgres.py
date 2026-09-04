"""PostgreSQL 连接的仓库级唯一入口。

正式代码不能在各个适配器里分别决定连接、语句和锁等待的边界。三项超时写进 libpq
启动参数，连接建立后的第一条业务语句已经受约束。本模块只依赖标准库；``psycopg``
在真正建连的函数内延迟导入，保持没有数据库驱动的纯逻辑测试仍能导入正式包。

``connect()`` 默认返回本进程内可复用的连接：``close()`` 不真正断开，而是把仍然
健康的连接放回按 (DSN, 超时配置) 分组的空闲栈，供下一次同 DSN、同超时配置的
``connect()`` 直接取用；连接已断、回滚失败或空闲栈已满时才真正关闭。借用者看到
的合同与驱动一致，区别只在物理连接未必真的断开；取用前的健康检查、归还前的
会话属性复位见 ``_IdleConnectionPool`` 与 ``_ReusableConnectionMixin``。
``dedicated=True``（或任何额外 psycopg 关键字参数）取得驱动原生连接，不进空闲栈，
``close()`` 立即真正关闭——``LISTEN`` 适配器与常驻轮询的独占连接用这条路径；
进程退出时 ``atexit`` 清空空闲栈，不给数据库连接池留悬挂会话。
"""

from __future__ import annotations

import atexit
import logging
import select
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT_SECONDS = 5
DEFAULT_STATEMENT_TIMEOUT_SECONDS = 3
DEFAULT_LOCK_TIMEOUT_SECONDS = 2

# 仓库级受控覆盖范围，不是部署配置。上限须与 scheduler 停机宽限期 150s 相容：
# (20 + 4.2 + 5 * (MAX_TIMEOUT_SECONDS + 2 * MAX_TIMEOUT_SECONDS)) * 1.5 <= 150
# （5 次数据库操作各按建连/语句/提交的合法上界计、另加续期 HTTP 20s、落盘退避
# 4.2s、1.5 倍安全系数）；MAX=5 时 148.8s，MAX=6 已到 171.3s，故上界只能是 5s。
MAX_TIMEOUT_SECONDS = 5

#: 每个 (DSN, 超时配置) 组合最多保留的空闲连接数；超出的连接归还时真正关闭。在用
#: 连接不设上界，进程连接数上界是"同时在用数 + 组合数 × 本值"。取 2：Supavisor
#: session 模式下每个 user+db 客户端会话上限即 pool_size（stage 实测 15），三个
#: 常驻进程稳态各持 2–3 条外还要给健康检查、监控与人工排查留余量；突发并发超过 2
#: 只是多握手几次，不会退化回"每次操作都建连"。
MAX_IDLE_CONNECTIONS_PER_KEY = 2

#: 空闲超过这个秒数的连接，再次取用前先发一条 ``SELECT 1`` 探活；更短的空闲期只看
#: socket 而不按时间探（常驻轮询 1–2 秒一次，每次都探等于把往返翻倍）——服务端
#: 掐断连接前一定先送 FATAL 再关 socket，取用前用零成本的 ``select()`` 看一眼，
#: 可读才探活。两条路径下坏连接都在这里被丢弃重建，调用方看不到失败。
IDLE_PROBE_AFTER_SECONDS = 30.0

#: 空闲超过这个秒数仍没被再次取用的连接，在下一次归还时顺手真正关闭。空闲栈是后进
#: 先出：一次并发突发留下的多条连接里，只有栈顶那条会被反复取用，其余会一直躺在
#: 栈底白占 pooler 的一个会话。按空闲时长回收让稳态连接数自动收敛到实际并发。
MAX_IDLE_AGE_SECONDS = 300.0

#: 长连接的 TCP 保活与无响应上限（libpq 连接参数，内核层面生效）：连接长期存活时，
#: 中间设备静默丢弃（NAT / 防火墙 / pooler 宿主宕机无 RST）会让一条语句阻塞到内核
#: TCP 重传放弃（分钟到十几分钟级），打破 scheduler 停机宽限期对单次数据库操作
#: 有界的假设。30s 空闲后每 10s 探一次、3 次无应答判死；已发数据 15s 内无 ACK 同判。
TCP_KEEPALIVE_PARAMETERS: Mapping[str, int] = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
    "tcp_user_timeout": 15_000,
}


class PostgresTimeoutConfigError(ValueError):
    """数据库超时缺失以外的格式、正值或安全范围错误。"""


def _validate_timeout(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PostgresTimeoutConfigError(f"{name} 必须是 1 到 {MAX_TIMEOUT_SECONDS} 的整数秒")
    if value <= 0 or value > MAX_TIMEOUT_SECONDS:
        raise PostgresTimeoutConfigError(f"{name} 必须是 1 到 {MAX_TIMEOUT_SECONDS} 的整数秒")
    return value


@dataclass(frozen=True)
class PostgresTimeouts:
    """正式业务连接的三项有限等待边界，覆盖只能通过这个类型进入。"""

    connect_timeout_seconds: int = DEFAULT_CONNECT_TIMEOUT_SECONDS
    statement_timeout_seconds: int = DEFAULT_STATEMENT_TIMEOUT_SECONDS
    lock_timeout_seconds: int = DEFAULT_LOCK_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        for name in (
            "connect_timeout_seconds",
            "statement_timeout_seconds",
            "lock_timeout_seconds",
        ):
            _validate_timeout(name, getattr(self, name))

    @classmethod
    def from_env(
        cls,
        environment: Mapping[str, str],
        *,
        prefix: str = "LINGXI_POSTGRES_",
    ) -> PostgresTimeouts:
        """从已由 ``apps`` 传入的环境映射构造配置，不直接读取进程环境。"""

        return cls(
            connect_timeout_seconds=_read_timeout(
                environment, f"{prefix}CONNECT_TIMEOUT_SECONDS", DEFAULT_CONNECT_TIMEOUT_SECONDS
            ),
            statement_timeout_seconds=_read_timeout(
                environment, f"{prefix}STATEMENT_TIMEOUT_SECONDS", DEFAULT_STATEMENT_TIMEOUT_SECONDS
            ),
            lock_timeout_seconds=_read_timeout(
                environment, f"{prefix}LOCK_TIMEOUT_SECONDS", DEFAULT_LOCK_TIMEOUT_SECONDS
            ),
        )

    @property
    def libpq_options(self) -> str:
        """返回不可由调用点修改的 PostgreSQL 会话启动参数。"""

        return (
            f"-c statement_timeout={self.statement_timeout_seconds}s "
            f"-c lock_timeout={self.lock_timeout_seconds}s"
        )


DEFAULT_POSTGRES_TIMEOUTS = PostgresTimeouts()


def _read_timeout(environment: Mapping[str, str], name: str, default: int) -> int:
    raw = environment.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        raise PostgresTimeoutConfigError(
            f"{name} 必须是 1 到 {MAX_TIMEOUT_SECONDS} 的整数秒"
        ) from None
    return _validate_timeout(name, value)


_PoolKey = tuple[str, PostgresTimeouts]


class _IdleConnectionPool:
    """按 (DSN, 超时配置) 分组的空闲连接栈；只保管此刻没人在用的连接。

    在用的连接不在这里登记：``connect()`` 取走即离开栈，``close()`` 归还才回来。
    因此同一线程里嵌套的两个 ``with connect()`` 拿到的是两条不同的物理连接，
    不会出现内层退出把外层事务一并提交的情况。

    不变量：一个连接对象在栈里最多出现一次。由连接自己的 ``_lingxi_idle`` 标志
    维护——压栈时置真、弹栈时置假，都在锁内完成；``close()`` 看到标志已真就直接
    返回，所以重复 ``close()`` 不会把同一对象压栈两次、让两位借用者共用一条连接。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._idle: dict[_PoolKey, list[tuple[Any, float]]] = {}
        self._atexit_registered = False

    def acquire(self, key: _PoolKey) -> Any | None:
        """取一条可用的空闲连接；没有则返回 ``None``，由调用方新建。"""

        while True:
            with self._lock:
                stack = self._idle.get(key)
                if not stack:
                    return None
                connection, released_at = stack.pop()
                connection._lingxi_idle = False
            if connection.closed:
                # 已经物理关闭的连接不能只是跳过：底层 PGconn 必须显式释放，
                # 否则要等 GC 才收。
                connection.discard()
                continue
            suspicious = (
                time.monotonic() - released_at > IDLE_PROBE_AFTER_SECONDS
                or connection.has_pending_input()
            )
            if suspicious and not connection.probe():
                logger.info("空闲数据库连接探活失败，丢弃重建")
                connection.discard()
                continue
            return connection

    def release(self, key: _PoolKey, connection: Any) -> bool:
        """归还一条连接。返回 ``True`` 表示已放回空闲栈；``False`` 表示它不该再被
        复用，调用方必须真正关闭它。顺手回收栈底空闲过久的连接。"""

        if connection.closed or not connection.reset_for_reuse():
            return False
        now = time.monotonic()
        expired: list[Any] = []
        with self._lock:
            stack = self._idle.setdefault(key, [])
            while stack and now - stack[0][1] > MAX_IDLE_AGE_SECONDS:
                stale, _released_at = stack.pop(0)
                stale._lingxi_idle = False
                expired.append(stale)
            if len(stack) >= MAX_IDLE_CONNECTIONS_PER_KEY:
                accepted = False
            else:
                connection._lingxi_idle = True
                stack.append((connection, now))
                accepted = True
                if not self._atexit_registered:
                    atexit.register(self.close_all)
                    self._atexit_registered = True
        for stale in expired:
            stale.discard()
        return accepted

    def close_all(self) -> int:
        """真正关闭全部空闲连接，返回关闭的数量。进程退出时经 ``atexit`` 调用。"""

        with self._lock:
            stacks = list(self._idle.values())
            self._idle.clear()
            for stack in stacks:
                for connection, _released_at in stack:
                    connection._lingxi_idle = False
        closed = 0
        for stack in stacks:
            for connection, _released_at in stack:
                connection.discard()
                closed += 1
        return closed

    def idle_count(self, key: _PoolKey | None = None) -> int:
        with self._lock:
            if key is not None:
                return len(self._idle.get(key, ()))
            return sum(len(stack) for stack in self._idle.values())


_IDLE_POOL = _IdleConnectionPool()


def close_idle_connections() -> int:
    """关闭本进程空闲栈里的全部连接，返回数量。``apps`` 层停机或测试隔离时调用；
    正常退出由 ``atexit`` 自动完成。"""

    return _IDLE_POOL.close_all()


def idle_connection_count() -> int:
    """当前空闲栈里的连接数（观测与测试用）。"""

    return _IDLE_POOL.idle_count()


_reusable_connection_type: type | None = None


class _ReusableConnectionMixin:
    """``close()`` 改为归还进程内空闲栈的连接，与 ``psycopg.Connection`` 组合成
    ``ReusableConnection``（见 :func:`_build_reusable_connection_type`）。

    借用者看到的合同与驱动一致：``with`` 退出由驱动 ``commit()`` / ``rollback()``
    再 ``close()``；``close()`` 之后 ``closed`` 为真、重复 ``close()`` 空操作、再开
    游标或提交报"连接已关闭"。区别只在物理连接没有断开，而是等下一位借用者。
    本类不依赖 ``psycopg``：模块顶层不能 import 第三方 SDK，需要引用驱动异常或
    状态枚举的方法各自延迟导入；``super()`` 在实际拼接出的 ``ReusableConnection``
    的 MRO 上解析到 ``psycopg.Connection``，与直接继承时行为一致。
    """

    _lingxi_pool_key: _PoolKey | None = None
    #: 为真表示对象此刻躺在空闲栈里（或正被池回收）：借用者手里的引用已经失效。
    _lingxi_idle: bool = False

    @property
    def closed(self) -> bool:  # type: ignore[override]
        return self._lingxi_idle or super().closed

    def close(self) -> None:
        if self._lingxi_idle:
            return
        key = self._lingxi_pool_key
        if key is None or super().closed:
            super().close()
            return
        if _IDLE_POOL.release(key, self):
            return
        self.discard()

    def cursor(self, *args: Any, **kwargs: Any) -> Any:
        if self._lingxi_idle:
            import psycopg

            raise psycopg.OperationalError("the connection is closed")
        return super().cursor(*args, **kwargs)

    def commit(self) -> None:
        if self._lingxi_idle:
            import psycopg

            raise psycopg.OperationalError("the connection is closed")
        super().commit()

    def rollback(self) -> None:
        if self._lingxi_idle:
            import psycopg

            raise psycopg.OperationalError("the connection is closed")
        super().rollback()

    def discard(self) -> None:
        """真正关闭，不再归还。"""

        self._lingxi_idle = False
        self._lingxi_pool_key = None
        super().close()

    def reset_for_reuse(self) -> bool:
        """把连接恢复到"没有事务、默认会话属性"的初始形状。

        返回 ``False`` 表示恢复不了（回滚失败、状态未知或语句仍在执行），这条
        连接必须丢弃。回滚是这里唯一会发数据库语句的动作：调用方经 ``close()``
        直接归还而没有先 ``commit()`` 时，未提交的改动与直接关闭一样被丢弃——
        不能带着别人的半截事务给下一位借用者。属性复位都是本地赋值，不发语句。
        """

        from psycopg.pq import TransactionStatus

        status = self.pgconn.transaction_status
        if status in (TransactionStatus.ACTIVE, TransactionStatus.UNKNOWN):
            return False
        try:
            if status != TransactionStatus.IDLE:
                super().rollback()
            if self.autocommit:
                self.autocommit = False
            if self.read_only is not None:
                self.read_only = None
            if self.isolation_level is not None:
                self.isolation_level = None
            if self.deferrable is not None:
                self.deferrable = None
        except Exception as error:  # 复位失败只意味着不复用，不需要区分原因
            logger.info("数据库连接归还前复位失败，改为关闭：%s", type(error).__name__)
            return False
        return True

    def has_pending_input(self) -> bool:
        """socket 上有没有待读数据。空闲连接本不该有：有就是服务端来过话
        （FATAL 后关连接），当作可疑，交给 ``probe()`` 定夺。看不了 socket 也算可疑。"""

        try:
            readable, _writable, _errors = select.select([self.fileno()], [], [], 0)
        except (OSError, ValueError):
            return True
        return bool(readable)

    def probe(self) -> bool:
        """一次往返确认服务端还在；失败返回 ``False``。"""

        try:
            self.autocommit = True
            try:
                with super().cursor() as cursor:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
            finally:
                self.autocommit = False
        except Exception:  # 探活失败的原因不重要，结论都是丢弃
            return False
        return True


def _build_reusable_connection_type() -> type:
    """延迟构造 psycopg ``Connection`` 子类：模块顶层不能 import 驱动。"""

    global _reusable_connection_type
    if _reusable_connection_type is not None:
        return _reusable_connection_type

    import psycopg

    class ReusableConnection(_ReusableConnectionMixin, psycopg.Connection):  # type: ignore[type-arg]
        pass

    _reusable_connection_type = ReusableConnection
    return ReusableConnection


def connect(
    dsn: str,
    *,
    timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS,
    dedicated: bool = False,
    **kwargs: Any,
) -> Any:
    """按仓库约定取得一个 PostgreSQL 连接。

    默认返回可复用连接（见模块说明）：优先取空闲栈里的健康连接，没有才真正建连，
    并关掉服务端预编译（``prepare_threshold=None``）避免长连接触发"cached plan
    must not change result type"。``dedicated=True`` 或任何额外 psycopg 关键字参数
    表示要一条驱动原生独占连接：不进空闲栈，``close()`` 立即真正关闭。

    ``connect_timeout``/``options``/TCP 保活参数不接受调用方覆盖；需要改变超时
    边界必须先构造经过校验的 :class:`PostgresTimeouts`。
    """

    if "connect_timeout" in kwargs or "options" in kwargs:
        raise TypeError("数据库连接的超时参数只能通过 PostgresTimeouts 提供")
    if kwargs.keys() & TCP_KEEPALIVE_PARAMETERS.keys():
        raise TypeError("数据库连接的 TCP 保活参数由 adapters.postgres 固定，不接受覆盖")
    if not isinstance(timeouts, PostgresTimeouts):
        raise TypeError("数据库连接超时必须使用 PostgresTimeouts")
    import psycopg

    if dedicated or kwargs:
        return psycopg.connect(
            dsn,
            connect_timeout=timeouts.connect_timeout_seconds,
            options=timeouts.libpq_options,
            **TCP_KEEPALIVE_PARAMETERS,
            **kwargs,
        )

    key: _PoolKey = (dsn, timeouts)
    connection = _IDLE_POOL.acquire(key)
    if connection is not None:
        return connection
    connection = _build_reusable_connection_type().connect(
        dsn,
        connect_timeout=timeouts.connect_timeout_seconds,
        options=timeouts.libpq_options,
        prepare_threshold=None,
        **TCP_KEEPALIVE_PARAMETERS,
    )
    connection._lingxi_pool_key = key
    return connection
