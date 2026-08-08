"""PostgreSQL 连接的仓库级唯一入口。

正式代码不能在各个适配器里分别决定连接、语句和锁等待的边界。把三项设置放进
libpq 的启动参数，连接建立后的第一条业务语句也已经受约束，不依赖调用方记得
额外执行一条 ``SET``。

本模块只依赖标准库；``psycopg`` 在真正建连的函数内延迟导入，保持没有数据库驱动
的纯逻辑测试仍能导入正式包。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

DEFAULT_CONNECT_TIMEOUT_SECONDS = 5
DEFAULT_STATEMENT_TIMEOUT_SECONDS = 3
DEFAULT_LOCK_TIMEOUT_SECONDS = 2

# 这是仓库级受控覆盖范围，不是部署配置。上限防止某个调用方用“覆盖”重新引入
# 长时间无界等待；一次性迁移有自己的有限配置，见迁移入口模块。
#
# 上限还必须与 scheduler 的 150s stop_grace_period 相容：门禁按最坏的 5 次数据库操作
# 建模，每次是建连、语句和提交各按 MAX_TIMEOUT_SECONDS 的合法上界，另有续期 HTTP
# 20s、落盘退避 4.2s，并乘 1.5 安全系数。因此要求
# (20 + 4.2 + 5 * (MAX_TIMEOUT_SECONDS + 2 * MAX_TIMEOUT_SECONDS)) * 1.5 <= 150；
# MAX=5 时为 148.8s（取整要求 149s），MAX=6 时已为 171.3s，故合法上界只能是 5s。
MAX_TIMEOUT_SECONDS = 5


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
    ) -> "PostgresTimeouts":
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
        raise PostgresTimeoutConfigError(f"{name} 必须是 1 到 {MAX_TIMEOUT_SECONDS} 的整数秒") from None
    return _validate_timeout(name, value)


def connect(
    dsn: str,
    *,
    timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS,
    **kwargs: Any,
) -> Any:
    """按仓库约定建立一个 PostgreSQL 连接。

    ``connect_timeout`` 与 ``options`` 不允许由调用方通过 ``kwargs`` 覆盖；需要改变
    边界时必须先构造经过校验的 :class:`PostgresTimeouts`。其余 psycopg 连接参数（例如
    ``autocommit`` 或 ``user``）可以用于测试与受控适配场景。
    """

    if "connect_timeout" in kwargs or "options" in kwargs:
        raise TypeError("数据库连接的超时参数只能通过 PostgresTimeouts 提供")
    if not isinstance(timeouts, PostgresTimeouts):
        raise TypeError("数据库连接超时必须使用 PostgresTimeouts")
    import psycopg

    return psycopg.connect(
        dsn,
        connect_timeout=timeouts.connect_timeout_seconds,
        options=timeouts.libpq_options,
        **kwargs,
    )
