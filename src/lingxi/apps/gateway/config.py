"""gateway 入口的类型化配置：只从 ``LINGXI_GATEWAY_`` 前缀环境变量读一次。

[代码框架「三、横切约定」](../../../../docs/技术设计/代码框架.md)要求配置在 ``apps``
入口一次性读取并构造成类型化对象往下传，``core`` 与 ``adapters`` 不碰 ``os.environ``；
主机、端口、路径、密钥不得硬编码（`V-部署-01`）。

校验放在**构造期**：一条退避参数写错（零间隔）在运行期才发现，意味着已经对着飞书
打出一轮忙循环了。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping

ENV_PREFIX = "LINGXI_GATEWAY_"


class GatewayConfigError(ValueError):
    """配置不合法。启动即失败，不留到连接建立之后。"""


class _Secret(str):
    """凭据字符串。覆盖 ``__repr__``，避免它随 dataclass 的默认 repr 进日志。

    合同：凭据不进代码、日志、数据库、用户环境。
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 只为防泄漏，无分支
        return "'<已隐去>'"


@dataclass(frozen=True)
class GatewayConfig:
    """gateway 进程需要的全部输入。"""

    app_id: str
    app_secret: _Secret = field(repr=False)
    postgres_dsn: _Secret = field(repr=False)
    reconnect_base_seconds: float = 1.0
    reconnect_factor: float = 2.0
    reconnect_ceiling_seconds: float = 60.0
    # 收到 SIGTERM 后等待在途事件落库的上限（`V-部署-03`）。
    shutdown_timeout_seconds: float = 20.0


def _text(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(f"{ENV_PREFIX}{name}")
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _number(env: Mapping[str, str], name: str, default: float) -> float:
    raw = _text(env, name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        # 只报变量名，不回显值——这些变量里有凭据的邻居，养成回显习惯迟早会漏。
        raise GatewayConfigError(f"{ENV_PREFIX}{name} 不是一个数字") from None
    # ``float("nan")`` / ``float("inf")`` 都是合法的 Python 字面量，会一路通过
    # 后面所有的比较：``nan > 0`` 为假、``nan <= x`` 也为假，于是 BackoffPolicy 的
    # 校验放它过去，然后进程在第一次断线时睡 ``inf`` 秒——一个永远不会恢复、
    # 也不会报错的挂起。这类值必须在启动期就拒掉。
    if not math.isfinite(value):
        raise GatewayConfigError(f"{ENV_PREFIX}{name} 必须是有限数字")
    return value


def load_config(env: Mapping[str, str]) -> GatewayConfig:
    """从环境变量构造配置。缺失或不合法时抛 :class:`GatewayConfigError`。"""

    missing = [
        name for name in ("APP_ID", "APP_SECRET", "POSTGRES_DSN") if not _text(env, name)
    ]
    if missing:
        raise GatewayConfigError(
            "缺少必填环境变量：" + "、".join(f"{ENV_PREFIX}{name}" for name in missing)
        )

    config = GatewayConfig(
        app_id=_text(env, "APP_ID") or "",
        app_secret=_Secret(_text(env, "APP_SECRET") or ""),
        postgres_dsn=_Secret(_text(env, "POSTGRES_DSN") or ""),
        reconnect_base_seconds=_number(env, "RECONNECT_BASE_SECONDS", 1.0),
        reconnect_factor=_number(env, "RECONNECT_FACTOR", 2.0),
        reconnect_ceiling_seconds=_number(env, "RECONNECT_CEILING_SECONDS", 60.0),
        shutdown_timeout_seconds=_number(env, "SHUTDOWN_TIMEOUT_SECONDS", 20.0),
    )

    # 退避参数的合法性由 BackoffPolicy 定义（factor > 1、base > 0），在这里就地校验，
    # 免得进程起来之后才在第一次断线时抛。
    from lingxi.adapters.feishu_longconn import BackoffPolicy

    try:
        BackoffPolicy(
            base_seconds=config.reconnect_base_seconds,
            factor=config.reconnect_factor,
            ceiling_seconds=config.reconnect_ceiling_seconds,
        )
    except ValueError as error:
        raise GatewayConfigError(f"重连退避配置不合法：{error}") from None

    return config
