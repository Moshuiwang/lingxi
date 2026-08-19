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

from lingxi.adapters.postgres import (
    DEFAULT_POSTGRES_TIMEOUTS,
    PostgresTimeoutConfigError,
    PostgresTimeouts,
)
from lingxi.core.alerting import AlertPolicy

ENV_PREFIX = "LINGXI_GATEWAY_"

# 飞书开放平台地址来自配置，代码里只有一个可被覆盖的默认值（断言 V-部署-01）；
# 与 apps/scheduler/config.py（#237 拆分后的新位置）的 DEFAULT_FEISHU_BASE_URL 同一取舍——只有告警
# 出口的 FeishuGroupMessages（走标准库 urllib，不经 lark-oapi）需要这个原始 URL。
DEFAULT_FEISHU_BASE_URL = "https://open.feishu.cn/open-apis"


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
    postgres_timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS
    reconnect_base_seconds: float = 1.0
    reconnect_factor: float = 2.0
    reconnect_ceiling_seconds: float = 60.0
    # 收到 SIGTERM 后等待在途事件落库的上限（`V-部署-03`）。
    shutdown_timeout_seconds: float = 20.0
    # 投递消费循环（Issue #152）没有独立的 NOTIFY 监听——outbox 事件的写入方是
    # Worker 进程，不在本进程内——轮询间隔是唯一的发现机制，因此默认取一个较短的值。
    delivery_poll_interval_seconds: float = 1.0
    delivery_batch_limit: int = 20
    # 最小告警装配（Issue #153）：管理群 chat_id **可选**——没有它进程照常启动，
    # 只是告警只落到结构化日志、不真正发进管理群，与 scheduler 的
    # `admin_group_chat_id` 同一取舍（一个尚未接线的可选职责不该让整个进程起不来）。
    admin_group_chat_id: str | None = None
    alert_policy: AlertPolicy = field(default_factory=AlertPolicy)
    feishu_base_url: str = DEFAULT_FEISHU_BASE_URL
    # S-A-07 受控验收专用开关（Issue #152 验收缺口，#154 评论 5306860510、
    # #162 E-022 已批准）：注入一次确定性的卡片投递拒绝，用于在没有真实故障可
    # 复现的情况下证伪「关闭卡片路径 + 同话题一次完整文本终态」的降级路径——
    # 默认没有任何部署态注入点，这条降级此前只能靠真实故障偶然触发。默认
    # `None`（不注入，装配路径与此前逐字节一致）；合法值只有四个，非法值必须
    # 启动即失败（失败关闭），不允许一个拼错的值悄悄在生产环境里长期放行。
    card_failure_injection: str | None = None

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
    # 本组数值全部是「时长」或「倍数」，没有一个在 0 或负数上有意义。
    # 尤其是停机超时：``<= 0`` 会让「在超时内退出」这条承诺退化成「立刻放弃在途事件」，
    # 而它还被用来推导空闲轮询间隔与出站超时，负值会一路传染下去（codex 二轮 P2-B）。
    if value <= 0:
        raise GatewayConfigError(f"{ENV_PREFIX}{name} 必须是正数")
    return value


# 合法值集合是产品合同的一部分（S-A-07 卡片故障注入开关第 1 点），不是随口列举：
# 每个值对应 `CardStream` 生命周期里的一步（建卡 / 流式更新 / 终态关闭）。
# 四个值的实测语义（独立审核 P2-1，详见 apps/gateway/_RejectingCards 的文档）：
#   - create/all 在正常单轮场景下等价（建卡先被拒即整体降级，update/close
#     没有机会被调用到），只有从已持久化 card_id 恢复时 all 才会真的命中
#     update/close 那一支；
#   - 覆盖"建卡成功之后"降级路径的是 update，不是 all；
#   - close 单独命中不产生降级（V-卡片-03：关闭失败不构成结果丢失），是
#     "关闭失败不得产生第二条文本终态"这条否定断言的验收入口。
_CARD_FAILURE_INJECTION_VALUES = frozenset({"create", "update", "close", "all"})


def _card_failure_injection(env: Mapping[str, str]) -> str | None:
    raw = _text(env, "CARD_FAILURE_INJECT")
    if raw is None:
        return None
    if raw not in _CARD_FAILURE_INJECTION_VALUES:
        # 不回显收到的值：和其余校验错误同一习惯——这个变量名紧挨着凭据变量，
        # 养成回显习惯迟早会在别的变量上漏出秘密。
        raise GatewayConfigError(
            f"{ENV_PREFIX}CARD_FAILURE_INJECT 不合法，只接受："
            + "、".join(sorted(_CARD_FAILURE_INJECTION_VALUES))
        )
    return raw


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _text(env, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise GatewayConfigError(f"{ENV_PREFIX}{name} 不是一个整数") from None
    if value <= 0:
        raise GatewayConfigError(f"{ENV_PREFIX}{name} 必须是正整数")
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

    try:
        postgres_timeouts = PostgresTimeouts.from_env(
            env, prefix=f"{ENV_PREFIX}POSTGRES_"
        )
    except PostgresTimeoutConfigError as error:
        raise GatewayConfigError(str(error)) from None

    raw_chat_id = _text(env, "ADMIN_GROUP_CHAT_ID")
    if raw_chat_id:
        from lingxi.adapters.feishu_group_message import validate_group_chat_id

        try:
            admin_group_chat_id: str | None = validate_group_chat_id(
                raw_chat_id, variable_name=f"{ENV_PREFIX}ADMIN_GROUP_CHAT_ID"
            )
        except ValueError as error:
            raise GatewayConfigError(str(error)) from None
    else:
        admin_group_chat_id = None

    try:
        alert_policy = AlertPolicy.from_mapping(env, prefix=f"{ENV_PREFIX}ALERT_")
    except ValueError as error:
        raise GatewayConfigError(str(error)) from None

    config = GatewayConfig(
        app_id=_text(env, "APP_ID") or "",
        app_secret=_Secret(_text(env, "APP_SECRET") or ""),
        postgres_dsn=_Secret(_text(env, "POSTGRES_DSN") or ""),
        postgres_timeouts=postgres_timeouts,
        reconnect_base_seconds=_number(env, "RECONNECT_BASE_SECONDS", 1.0),
        reconnect_factor=_number(env, "RECONNECT_FACTOR", 2.0),
        reconnect_ceiling_seconds=_number(env, "RECONNECT_CEILING_SECONDS", 60.0),
        shutdown_timeout_seconds=_number(env, "SHUTDOWN_TIMEOUT_SECONDS", 20.0),
        delivery_poll_interval_seconds=_number(env, "DELIVERY_POLL_INTERVAL_SECONDS", 1.0),
        delivery_batch_limit=_positive_int(env, "DELIVERY_BATCH_LIMIT", 20),
        admin_group_chat_id=admin_group_chat_id,
        alert_policy=alert_policy,
        feishu_base_url=_text(env, "FEISHU_BASE_URL") or DEFAULT_FEISHU_BASE_URL,
        card_failure_injection=_card_failure_injection(env),
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
