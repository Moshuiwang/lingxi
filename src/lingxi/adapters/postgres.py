"""PostgreSQL 连接的仓库级唯一入口。

三项超时（连接、语句、锁等待）写进 libpq 启动参数，第一条业务语句起就受约束，各适配器不再各自决定
边界。本模块只依赖标准库；``psycopg`` 在真正建连的函数内延迟导入，没有驱动的纯逻辑测试仍能导入正式包。

``connect()`` 默认返回一次借用专用的委托句柄（``_BorrowedConnection``）：物理连接归进程内按
(DSN, 超时配置) 分组的空闲栈所有，句柄 ``close()`` 把仍然健康的连接放回栈供下一次同键的 ``connect()``
取用（连接已断、回滚失败或栈已满才真正关闭），同时让该句柄永久失效——归还后旧句柄、从它取得的旧游标
与旧事务上下文一律报「连接已关闭」且不触达数据库，``with`` 退出对已归还句柄是空操作；句柄只转发
显式清单里的驱动接口、不暴露物理连接（清单与游标 / 事务包装见 ``_BorrowedConnection``）。
``dedicated=True`` 或任何额外 psycopg 关键字参数取得驱动原生连接，不进空闲栈、``close()`` 立即真正
关闭（``LISTEN`` 适配器与常驻轮询用它）；进程退出时 ``atexit`` 清空空闲栈，不留悬挂会话。
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

#: 三个常驻进程读数据库连接串用的环境变量名不统一：scheduler/worker 用不带
#: 前缀的 ``LINGXI_POSTGRES_DSN``，gateway 加了 ``LINGXI_GATEWAY_`` 前缀（见
#: ``apps/gateway/config.py`` 的 ``ENV_PREFIX``）。按角色查这张表，不新增
#: 第三套变量名；``apps/trace`` 按固定顺序逐个回退尝试，见该模块的说明。
DSN_ENV_VAR_BY_ROLE: Mapping[str, str] = {
    "scheduler": "LINGXI_POSTGRES_DSN",
    "worker": "LINGXI_POSTGRES_DSN",
    "gateway": "LINGXI_GATEWAY_POSTGRES_DSN",
}

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
        """校验三项等待边界均为正整数且不超过上限。"""
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
    """按 (DSN, 超时配置) 分组的空闲连接栈；只保管此刻没人在用的物理连接。

    在用的连接不在这里登记：``connect()`` 取走即离开栈、交给一个新句柄独占，句柄第一次 ``close()``
    归还才回来。因此同一线程里嵌套的两个 ``with connect()`` 拿到的是两条不同的物理连接，内层退出
    不会把外层事务一并提交。

    不变量：任何时刻一条物理连接要么躺在栈里（最多出现一次），要么被恰好一个未归还的句柄持有。
    出入栈都在锁内；「一次借用只归还一次」由句柄自己的「已归还」位保证（见 ``_BorrowedConnection``）：
    旧句柄迟到的 ``close()`` 是空操作，不会把别人在用的连接压回栈，也不会回滚它的在途写入。
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
            if connection.closed:
                # 已经物理关闭的连接不能只是跳过：显式 discard() 把释放交给驱动，
                # 不依赖引用计数的回收时机（驱动对已关闭连接会直接返回）。
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
        """归还一条连接。

        返回 ``True`` 表示已放回空闲栈；``False`` 表示它不该再被复用，调用方
        必须真正关闭它。顺手回收栈底空闲过久的连接。
        """
        if connection.closed or not connection.reset_for_reuse():
            return False
        now = time.monotonic()
        expired: list[Any] = []
        with self._lock:
            stack = self._idle.setdefault(key, [])
            while stack and now - stack[0][1] > MAX_IDLE_AGE_SECONDS:
                stale, _released_at = stack.pop(0)
                expired.append(stale)
            if len(stack) >= MAX_IDLE_CONNECTIONS_PER_KEY:
                accepted = False
            else:
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
        closed = 0
        for stack in stacks:
            for connection, _released_at in stack:
                try:
                    connection.discard()
                except Exception as error:
                    logger.warning(
                        "关闭空闲连接失败，继续清理其余连接 error=%s", type(error).__name__
                    )
                    continue
                closed += 1
        return closed

    def idle_count(self, key: _PoolKey | None = None) -> int:
        with self._lock:
            if key is not None:
                return len(self._idle.get(key, ()))
            return sum(len(stack) for stack in self._idle.values())


_IDLE_POOL = _IdleConnectionPool()


def close_idle_connections() -> int:
    """关闭本进程空闲栈里的全部连接，返回数量。

    ``apps`` 层停机或测试隔离时调用；正常退出由 ``atexit`` 自动完成。
    """
    return _IDLE_POOL.close_all()


def idle_connection_count() -> int:
    """当前空闲栈里的连接数（观测与测试用）。"""
    return _IDLE_POOL.idle_count()


_reusable_connection_type: type | None = None


class _ReusableConnectionMixin:
    """池持有的物理连接：归还前复位、取用前探活、真正关闭；借用者拿不到它。

    与 ``psycopg.Connection`` 组合成 ``ReusableConnection``（见 :func:`_build_reusable_connection_type`）。
    借用者看到的合同由 ``_BorrowedConnection`` 兑现；这里不重写 ``close()`` / ``closed`` 等驱动接口，
    物理连接的生命周期只由池与句柄经 ``discard()`` / ``reset_for_reuse()`` 驱动。
    """

    def discard(self) -> None:
        """真正关闭，不再归还。"""
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
        """Socket 上有没有待读数据。

        空闲连接本不该有：有就是服务端来过话（FATAL 后关连接），当作可疑，
        交给 ``probe()`` 定夺。看不了 socket 也算可疑。
        """
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
    """延迟构造 psycopg ``Connection`` 子类：模块顶层不能 import 驱动。

    混入类里引用驱动异常/状态枚举的方法也各自延迟导入；``super()`` 沿拼接出的 MRO 解析到
    ``psycopg.Connection``，行为与直接继承一致。
    """
    global _reusable_connection_type
    if _reusable_connection_type is not None:
        return _reusable_connection_type

    import psycopg

    class ReusableConnection(_ReusableConnectionMixin, psycopg.Connection):  # type: ignore[type-arg]
        pass

    _reusable_connection_type = ReusableConnection
    return ReusableConnection


def _closed_error() -> Exception:
    """归还后的旧句柄、旧游标与旧事务上下文统一报驱动同文案的「连接已关闭」，不触达数据库。"""
    import psycopg

    return psycopg.OperationalError("the connection is closed")


def _unforwarded(owner: object, name: str) -> AttributeError:
    return AttributeError(
        f"{type(owner).__name__} 不转发 {name}：借用句柄只提供 lingxi.adapters.postgres"
        " 模块说明列出的驱动接口"
    )


class _BorrowedConnection:
    """一次借用的委托句柄：持有物理连接引用与自己的「已归还」位，借用者只拿到它。

    转发面是显式清单：``cursor`` / ``execute``（返回的游标同样包装）、``commit`` / ``rollback`` /
    ``transaction``（上下文包装）、``close`` / ``closed``、``autocommit`` / ``read_only`` /
    ``isolation_level``（读写）、``info`` / ``prepare_threshold`` / ``notifies`` 与 ``with``；清单外
    的属性（含 ``pgconn``）一律 ``AttributeError``。第一次 ``close()`` 归还物理连接并永久置「已归还」：
    此后 ``close()`` 空操作、``closed`` 恒真、``with`` 退出空操作，其余转发一律报「连接已关闭」且不
    触达数据库；归还前取得的游标与事务上下文随句柄一起失效。一个句柄只有一位借用者，不设锁。
    """

    __slots__ = ("_connection", "_key", "_returned")

    def __init__(self, connection: Any, key: _PoolKey) -> None:
        self._connection = connection
        self._key = key
        self._returned = False

    def _borrowed(self) -> Any:
        """仍在借用期的物理连接；已归还则报「连接已关闭」。"""
        if self._returned:
            raise _closed_error()
        return self._connection

    def __getattr__(self, name: str) -> Any:
        raise _unforwarded(self, name)

    @property
    def closed(self) -> bool:
        return self._returned or self._connection.closed

    def close(self) -> None:
        if self._returned:
            return
        self._returned = True
        if not _IDLE_POOL.release(self._key, self._connection):
            self._connection.discard()

    def __enter__(self) -> _BorrowedConnection:
        self._borrowed()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """与驱动一致：异常回滚、否则提交，再归还；已归还或已断开的句柄不再发语句。"""
        if self._returned:
            return
        if not self._connection.closed:
            if exc_type:
                try:
                    self._connection.rollback()
                except Exception as error:
                    logger.warning(
                        "异常退出时回滚失败，忽略后照常归还 error=%s", type(error).__name__
                    )
            else:
                self._connection.commit()
        self.close()

    def cursor(self, *args: Any, **kwargs: Any) -> _BorrowedCursor:
        return _BorrowedCursor(self, self._borrowed().cursor(*args, **kwargs))

    def execute(self, query: Any, params: Any = None, **kwargs: Any) -> _BorrowedCursor:
        return _BorrowedCursor(self, self._borrowed().execute(query, params, **kwargs))

    def commit(self) -> None:
        self._borrowed().commit()

    def rollback(self) -> None:
        self._borrowed().rollback()

    def transaction(
        self, savepoint_name: str | None = None, force_rollback: bool = False
    ) -> _BorrowedTransaction:
        return _BorrowedTransaction(
            self, self._borrowed().transaction(savepoint_name, force_rollback)
        )

    def notifies(self, *args: Any, **kwargs: Any) -> Any:
        return self._borrowed().notifies(*args, **kwargs)

    @property
    def info(self) -> Any:
        return self._borrowed().info

    @property
    def prepare_threshold(self) -> int | None:
        return self._borrowed().prepare_threshold

    @property
    def autocommit(self) -> bool:
        return self._borrowed().autocommit

    @autocommit.setter
    def autocommit(self, value: bool) -> None:
        self._borrowed().autocommit = value

    @property
    def read_only(self) -> bool | None:
        return self._borrowed().read_only

    @read_only.setter
    def read_only(self, value: bool | None) -> None:
        self._borrowed().read_only = value

    @property
    def isolation_level(self) -> Any:
        return self._borrowed().isolation_level

    @isolation_level.setter
    def isolation_level(self, value: Any) -> None:
        self._borrowed().isolation_level = value


class _BorrowedCursor:
    """句柄发出的游标包装：每次触达数据库前先核对句柄未归还；``connection`` 回指句柄而非物理连接。

    转发面：``execute`` / ``executemany`` / ``fetchone`` / ``fetchmany`` / ``fetchall`` / 迭代 /
    ``scroll`` / ``close`` / ``with``，只读属性 ``rowcount`` / ``description`` / ``statusmessage`` /
    ``rownumber``；``closed`` 在句柄归还后恒真（状态读取不触达数据库，与句柄自身一致）。清单外
    （含 ``copy`` / ``stream`` / ``pgresult``）一律 ``AttributeError``。
    """

    __slots__ = ("_cursor", "_handle")

    def __init__(self, handle: _BorrowedConnection, cursor: Any) -> None:
        self._handle = handle
        self._cursor = cursor

    def _live(self) -> Any:
        self._handle._borrowed()
        return self._cursor

    def __getattr__(self, name: str) -> Any:
        raise _unforwarded(self, name)

    @property
    def connection(self) -> _BorrowedConnection:
        return self._handle

    @property
    def closed(self) -> bool:
        return self._handle._returned or self._cursor.closed

    @property
    def rowcount(self) -> int:
        return self._live().rowcount

    @property
    def description(self) -> Any:
        return self._live().description

    @property
    def statusmessage(self) -> str | None:
        return self._live().statusmessage

    @property
    def rownumber(self) -> int | None:
        return self._live().rownumber

    def execute(self, *args: Any, **kwargs: Any) -> _BorrowedCursor:
        self._live().execute(*args, **kwargs)
        return self

    def executemany(self, *args: Any, **kwargs: Any) -> None:
        self._live().executemany(*args, **kwargs)

    def fetchone(self) -> Any:
        return self._live().fetchone()

    def fetchmany(self, size: int = 0) -> list[Any]:
        return self._live().fetchmany(size)

    def fetchall(self) -> list[Any]:
        return self._live().fetchall()

    def scroll(self, value: int, mode: str = "relative") -> None:
        self._live().scroll(value, mode)

    def __iter__(self) -> Any:
        rows = iter(self._live())
        while True:
            self._handle._borrowed()
            try:
                row = next(rows)
            except StopIteration:
                return
            yield row

    def __next__(self) -> Any:
        return next(self._live())

    def close(self) -> None:
        self._live().close()

    def __enter__(self) -> _BorrowedCursor:
        self._live()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._live().close()


class _BorrowedTransaction:
    """``transaction()`` 的上下文包装：进出都先核对句柄未归还，其余逐字交给驱动的事务上下文。

    ``with … as tx`` 拿到的是本对象：``connection`` 回指句柄，``savepoint_name`` / ``force_rollback`` /
    ``status`` 读自驱动事务，块内 ``raise psycopg.Rollback(tx)`` 与驱动语义相同。句柄已归还时
    ``__exit__`` 报「连接已关闭」而不去提交或回滚——那条物理连接可能已经属于别人。
    """

    __slots__ = ("_context", "_handle", "_transaction")

    def __init__(self, handle: _BorrowedConnection, context: Any) -> None:
        self._handle = handle
        self._context = context
        self._transaction: Any = None

    def __enter__(self) -> _BorrowedTransaction:
        self._handle._borrowed()
        self._transaction = self._context.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        self._handle._borrowed()
        import psycopg

        if isinstance(exc_val, psycopg.Rollback) and exc_val.transaction is self:
            exc_val = psycopg.Rollback(self._transaction)
        return bool(self._context.__exit__(exc_type, exc_val, exc_tb))

    @property
    def connection(self) -> _BorrowedConnection:
        return self._handle

    @property
    def savepoint_name(self) -> str | None:
        return self._transaction.savepoint_name

    @property
    def force_rollback(self) -> bool:
        return self._transaction.force_rollback

    @property
    def status(self) -> Any:
        return self._transaction.status


def connect(
    dsn: str,
    *,
    timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS,
    dedicated: bool = False,
    **kwargs: Any,
) -> Any:
    """按仓库约定取得一个 PostgreSQL 连接。

    默认返回一次借用专用的委托句柄（见模块说明）：优先取空闲栈里的健康物理连接，没有才真正
    建连，并关掉服务端预编译（``prepare_threshold=None``）避免长连接触发"cached plan
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
    if connection is None:
        connection = _build_reusable_connection_type().connect(
            dsn,
            connect_timeout=timeouts.connect_timeout_seconds,
            options=timeouts.libpq_options,
            prepare_threshold=None,
            **TCP_KEEPALIVE_PARAMETERS,
        )
    return _BorrowedConnection(connection, key)
