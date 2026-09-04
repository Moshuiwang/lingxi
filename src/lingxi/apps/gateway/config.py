"""gateway 入口的类型化配置：只从 ``LINGXI_GATEWAY_`` 前缀环境变量读一次。

[代码框架「三、横切约定」](../../../../docs/技术设计/代码框架.md)要求配置在 ``apps``
入口一次性读取并构造成类型化对象往下传，``core`` 与 ``adapters`` 不碰 ``os.environ``；
主机、端口、路径、密钥不得硬编码（`V-部署-01`）。

校验放在**构造期**：一条退避参数写错（零间隔）在运行期才发现，意味着已经对着飞书
打出一轮忙循环了。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from lingxi.adapters.postgres import (
    DEFAULT_POSTGRES_TIMEOUTS,
    PostgresTimeoutConfigError,
    PostgresTimeouts,
)
from lingxi.core.alerting import AlertPolicy

ENV_PREFIX = "LINGXI_GATEWAY_"

# 飞书开放平台地址来自配置，代码里只有一个可被覆盖的默认值（断言 V-部署-01）；
# 只有告警出口的 FeishuGroupMessages（走标准库 urllib，不经 lark-oapi）需要
# 这个原始 URL，与 apps/scheduler/config.py 的同名常量各自独立登记、互不 import。
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
    # 投递消费循环没有独立的 NOTIFY 监听——outbox 事件的写入方是 Worker 进程，
    # 不在本进程内——轮询间隔是唯一的发现机制，因此默认取一个较短的值。
    delivery_poll_interval_seconds: float = 1.0
    delivery_batch_limit: int = 20
    # 排队可感知：入队后超过这个阈值仍未被任何 worker 领取时补发一条"前面还有
    # 任务在排队"。定值理由见 ``apps/gateway/delivery.DeliveryConsumer.
    # DEFAULT_QUEUE_DELAY_HINT_SECONDS`` 上方注释；两处各自独立登记默认值、
    # 互不 import，改其一记得同步改另一处。
    queue_delay_hint_seconds: float = 12.0
    # 最小告警装配：管理群 chat_id **可选**——没有它进程照常启动，只是告警只
    # 落到结构化日志、不真正发进管理群，与 scheduler 的 `admin_group_chat_id`
    # 同一取舍（一个尚未接线的可选职责不该让整个进程起不来）。
    admin_group_chat_id: str | None = None
    alert_policy: AlertPolicy = field(default_factory=AlertPolicy)
    feishu_base_url: str = DEFAULT_FEISHU_BASE_URL
    # 受控验收专用开关：注入一次确定性的卡片投递拒绝，用于在没有真实故障可
    # 复现的情况下证伪「关闭卡片路径 + 同话题一次完整文本终态」的降级路径。
    # 默认 `None`（不注入，装配路径与此前逐字节一致）；合法值只有四个，非法值
    # 必须启动即失败（失败关闭），不允许一个拼错的值悄悄在生产环境里长期放行。
    card_failure_injection: str | None = None
    # 内测名单闸的 gateway 侧前移一份：与 scheduler 侧读**同一个**环境变量名
    # `LINGXI_INNERTEST_ROSTER_OPEN_IDS`——不套 `LINGXI_GATEWAY_` 前缀，这是
    # 两个进程必须表达"同一份内测名单"的共享概念。默认空集合＝全拒（同
    # `innertest_roster_gate` 模块文档口径）；`repr=False` 避免一条日志把名单
    # 整份写出去（同 `_Secret` 字段纪律）。
    innertest_roster_open_ids: frozenset[str] = field(default_factory=frozenset, repr=False)
    # 文档投递独立消费循环的租户域名——不是密钥，是拼文档链接用的裸域名（见
    # `adapters/feishu_docx_delivery.py` 模块文档「文档 URL 的构造」）。可选，
    # 默认 `None`：未配置时 `document_delivery.assemble_document_delivery_
    # consumer` 整体不注册这条循环（失败关闭），不是"用一个猜测的域名硬跑"。
    # 飞书没有开放接口能查询"当前租户的裸域名"，只能由运维在部署时显式提供。
    tenant_domain: str | None = None

    # markdown 官方转换开关，环境变量固定为 `LINGXI_DOCX_MARKDOWN_CONVERT`（不
    # 套 `LINGXI_GATEWAY_` 前缀，理由同 `bot_open_id`）。默认开启：`True` 时走
    # 服务端一次建档写全文（`_create_docx_body`），`False` 时退回两步纯文本
    # 段落路径；解析细节与失败语义见 `_markdown_convert_enabled` 文档。
    markdown_convert_enabled: bool = True

    # 群聊@机器人固定引导：机器人自身 open_id，只用于精确判定"这条群消息是不是
    # @ 了机器人本身"。刻意不套 `LINGXI_GATEWAY_` 前缀——这是机器人身份本身的
    # 事实，不是 gateway 进程私有配置，同 `innertest_roster_open_ids` 纪律。
    # 未配置＝功能整体关闭＝维持"群聊完全静默"现状（失败关闭）。
    bot_open_id: str | None = None

    # 「公司+职能→指标名」翻译映射的外置路径，不套 `LINGXI_GATEWAY_` 前缀——
    # 读的是 scheduler 侧同一个变量名 `LINGXI_COMPANY_FUNCTION_METRIC_MAP_PATH`。
    # gateway 必须读它：管理卡定向重算立即发布权限，与 scheduler 次日日批必须
    # 用同一份映射，否则同一个人的权限范围会在两条路径间来回翻转。默认 `None`
    # ＝落回随包默认映射；解析与失败语义见 `_metric_map_path` 文档。
    metric_map_path: Path | None = None


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
    # 而它还被用来推导空闲轮询间隔与出站超时，负值会一路传染下去。
    if value <= 0:
        raise GatewayConfigError(f"{ENV_PREFIX}{name} 必须是正数")
    return value


# 合法值集合是产品合同的一部分：每个值对应 `CardStream` 生命周期里的一步
# （建卡/流式更新/终态关闭，详见 apps/gateway/_RejectingCards 的文档）。
# create/all 在正常单轮场景下等价，只有从已持久化 card_id 恢复时 all 才真正
# 命中 update/close；覆盖"建卡成功之后"降级路径的是 update，不是 all；
# close 单独命中不产生降级（`V-卡片-03`），是这条否定断言的验收入口。
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


def _innertest_roster_open_ids(env: Mapping[str, str]) -> frozenset[str]:
    """内测名单闸的 gateway 侧解析。

    刻意直接读 ``env.get("LINGXI_INNERTEST_ROSTER_OPEN_IDS")``——不经过 `_text()`
    那层 ``LINGXI_GATEWAY_`` 前缀包装：这不是 gateway 私有配置，是与 scheduler
    共享同一个变量名的名单（见 :class:`GatewayConfig` 该字段的文档）。解析规则
    委托给 :func:`~lingxi.core.identity.innertest_roster_gate.parse_innertest_
    roster`，与 scheduler 侧复用同一个函数，不写第二份解析逻辑等着两边漂移。
    """
    from lingxi.core.identity.innertest_roster_gate import (
        InnerTestRosterConfigError,
        parse_innertest_roster,
    )

    try:
        return parse_innertest_roster(env.get("LINGXI_INNERTEST_ROSTER_OPEN_IDS"))
    except InnerTestRosterConfigError as error:
        # 错配不是未配：与本文件其余校验错误同一条纪律，只报变量名，不回显
        # 取到的原始条目（那是身份标识）。
        raise GatewayConfigError(
            f"环境变量 LINGXI_INNERTEST_ROSTER_OPEN_IDS 不合法：{error}"
        ) from None


def _tenant_domain(env: Mapping[str, str]) -> str | None:
    """文档投递独立消费循环的租户域名。

    未配置即 ``None``（循环不注册，见 :class:`GatewayConfig` 该字段的文档）；
    配了就在构造期校验形状——裸域名、不含协议/路径/空白，与
    ``feishu_docx_delivery._require_tenant_domain`` 同一条校验，提前跑一遍是
    为了让拼错的域名在启动期就失败关闭，而不是等到真正建文档时才发现。
    """
    raw = _text(env, "TENANT_DOMAIN")
    if raw is None:
        return None
    from lingxi.adapters.feishu_docx_delivery import _require_tenant_domain

    try:
        return _require_tenant_domain(raw)
    except ValueError as error:
        raise GatewayConfigError(f"{ENV_PREFIX}TENANT_DOMAIN 不合法：{error}") from None


def _markdown_convert_enabled(env: Mapping[str, str]) -> bool:
    """Markdown 官方转换开关的解析。

    未配置或为空——``True``；历史值 ``"1"``（翻转默认值前唯一的开启值，已写
    进既有 stage 配置）——同样 ``True``；精确值 ``"0"``——``False``；其余取值
    启动即失败（错配不是未配）。

    **保留理由是止损**：``docs_ai`` 在飞书开放平台没有公开文档页，限流与
    长度上限无官方契约，这是不改代码、不重建镜像就能退回段落路径的闸门。
    判定为降级时**降级交付**，其余失败仍然失败关闭、不静默退回段落路径。
    """
    flag = (env.get("LINGXI_DOCX_MARKDOWN_CONVERT") or "").strip()
    if not flag or flag == "1":
        return True
    if flag != "0":
        raise GatewayConfigError(
            'LINGXI_DOCX_MARKDOWN_CONVERT 只接受 "0"（关闭）或 "1"（开启，'
            "历史值兼容，效果与未配置相同）（不回显收到的值）"
        )
    return False


def _metric_map_path(env: Mapping[str, str]) -> Path | None:
    """外置指标映射路径的解析。

    刻意直接读 ``env.get(METRIC_MAP_PATH_ENV)``，不经过本文件 ``_text()`` 的
    前缀包装——理由见 :class:`GatewayConfig` 该字段的文档。解析规则委托给
    scheduler 侧复用的同一个函数，避免两边解释逻辑漂移。

    未配置 → ``None``（落回随包默认映射，不是启动失败）；配了但指向的文件
    缺失或格式非法 → 三个调用点各自按自身既有姿态失败关闭，没有一处静默
    回落随包默认——错配不是未配。
    """
    from lingxi.adapters.company_function_metric_map_file import (
        METRIC_MAP_PATH_ENV,
        parse_metric_map_path,
    )

    try:
        return parse_metric_map_path(env.get(METRIC_MAP_PATH_ENV))
    except ValueError as error:
        raise GatewayConfigError(str(error)) from None


def _bot_open_id(env: Mapping[str, str]) -> str | None:
    """机器人自身 open_id：群聊 @ 机器人固定引导判定用。

    刻意直接读 ``env.get("LINGXI_BOT_OPEN_ID")``，不经过本文件 ``_text()`` 的
    ``LINGXI_GATEWAY_`` 前缀包装——理由见 :class:`GatewayConfig` 该字段的文档。
    未配置或空白都当作"未配置"（功能整体关闭），不校验取值形状：读到的值只用于
    跟事件体里的 mentions 做字符串精确比较，格式不对顶多是永远比对不上、不产生
    任何额外风险，因此不必像 `LINGXI_INNERTEST_ROSTER_OPEN_IDS` 那样失败关闭拒绝
    启动。
    """
    raw = env.get("LINGXI_BOT_OPEN_ID")
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


def _require_env_vars(env: Mapping[str, str]) -> None:
    """必填变量早失败：三者缺一都不能起进程。"""
    missing = [name for name in ("APP_ID", "APP_SECRET", "POSTGRES_DSN") if not _text(env, name)]
    if missing:
        raise GatewayConfigError(
            "缺少必填环境变量：" + "、".join(f"{ENV_PREFIX}{name}" for name in missing)
        )


def _load_postgres_timeouts(env: Mapping[str, str]) -> PostgresTimeouts:
    try:
        return PostgresTimeouts.from_env(env, prefix=f"{ENV_PREFIX}POSTGRES_")
    except PostgresTimeoutConfigError as error:
        raise GatewayConfigError(str(error)) from None


def _admin_group_chat_id(env: Mapping[str, str]) -> str | None:
    raw_chat_id = _text(env, "ADMIN_GROUP_CHAT_ID")
    if not raw_chat_id:
        return None
    from lingxi.adapters.feishu_group_message import validate_group_chat_id

    try:
        return validate_group_chat_id(raw_chat_id, variable_name=f"{ENV_PREFIX}ADMIN_GROUP_CHAT_ID")
    except ValueError as error:
        raise GatewayConfigError(str(error)) from None


def _load_alert_policy(env: Mapping[str, str]) -> AlertPolicy:
    try:
        return AlertPolicy.from_mapping(env, prefix=f"{ENV_PREFIX}ALERT_")
    except ValueError as error:
        raise GatewayConfigError(str(error)) from None


def _validate_backoff(config: GatewayConfig) -> None:
    """退避参数合法性（factor > 1、base > 0）由 ``BackoffPolicy`` 定义。

    构造期就地校验，免得进程起来之后才在第一次断线时抛。
    """
    from lingxi.adapters.feishu_longconn import BackoffPolicy

    try:
        BackoffPolicy(
            base_seconds=config.reconnect_base_seconds,
            factor=config.reconnect_factor,
            ceiling_seconds=config.reconnect_ceiling_seconds,
        )
    except ValueError as error:
        raise GatewayConfigError(f"重连退避配置不合法：{error}") from None


def load_config(env: Mapping[str, str]) -> GatewayConfig:
    """从环境变量构造配置。缺失或不合法时抛 :class:`GatewayConfigError`。"""
    _require_env_vars(env)
    config = GatewayConfig(
        app_id=_text(env, "APP_ID") or "",
        app_secret=_Secret(_text(env, "APP_SECRET") or ""),
        postgres_dsn=_Secret(_text(env, "POSTGRES_DSN") or ""),
        postgres_timeouts=_load_postgres_timeouts(env),
        reconnect_base_seconds=_number(env, "RECONNECT_BASE_SECONDS", 1.0),
        reconnect_factor=_number(env, "RECONNECT_FACTOR", 2.0),
        reconnect_ceiling_seconds=_number(env, "RECONNECT_CEILING_SECONDS", 60.0),
        shutdown_timeout_seconds=_number(env, "SHUTDOWN_TIMEOUT_SECONDS", 20.0),
        delivery_poll_interval_seconds=_number(env, "DELIVERY_POLL_INTERVAL_SECONDS", 1.0),
        delivery_batch_limit=_positive_int(env, "DELIVERY_BATCH_LIMIT", 20),
        queue_delay_hint_seconds=_number(env, "QUEUE_DELAY_HINT_SECONDS", 12.0),
        admin_group_chat_id=_admin_group_chat_id(env),
        alert_policy=_load_alert_policy(env),
        feishu_base_url=_text(env, "FEISHU_BASE_URL") or DEFAULT_FEISHU_BASE_URL,
        card_failure_injection=_card_failure_injection(env),
        innertest_roster_open_ids=_innertest_roster_open_ids(env),
        tenant_domain=_tenant_domain(env),
        markdown_convert_enabled=_markdown_convert_enabled(env),
        bot_open_id=_bot_open_id(env),
        metric_map_path=_metric_map_path(env),
    )
    _validate_backoff(config)
    return config
