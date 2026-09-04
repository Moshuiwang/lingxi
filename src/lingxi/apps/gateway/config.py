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
    # 排队可感知（Issue #465，rc22 S-3）：入队后超过这个阈值仍未被任何 worker
    # 领取时补发一条"前面还有任务在排队"。定值理由（10~15 秒区间取中值 12 秒）
    # 见 ``apps/gateway/delivery.DeliveryConsumer.DEFAULT_QUEUE_DELAY_HINT_
    # SECONDS`` 上方注释；两处各自独立登记默认值、互不 import（`apps/config`
    # 与 `apps/delivery` 之间不建立仅为一个常量的依赖边），改其一记得同步改
    # 另一处。
    queue_delay_hint_seconds: float = 12.0
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
    # 内测名单闸的 gateway 侧前移一份（Issue #302 S-N-01 的纵深，opus 批量审查
    # P1 修复）：与 scheduler 侧 `SchedulerConfig.innertest_roster_open_ids`
    # 读**同一个**环境变量名 `LINGXI_INNERTEST_ROSTER_OPEN_IDS`——刻意不套
    # `LINGXI_GATEWAY_` 前缀，因为这不是 gateway 私有配置，是两个进程各自独立
    # 部署但必须表达"同一份内测名单"的共享概念；两处进程分别配置同一个变量名，
    # 值理应一致（运维纪律，代码不做跨进程一致性校验）。**默认空集合＝全拒**，
    # 语义与 scheduler 侧逐字一致，见 `lingxi.core.identity.innertest_roster_gate`
    # 模块文档「默认关闭＝全拒」。`field(repr=False)`（opus 批量审查 P2 修复）：
    # 名单本身是一批飞书用户 open_id，与本文件 `_Secret` 字段同一条纪律——不进
    # `repr(config)`，不会随手一个 `logger.info("配置 %s", config)` 就把内测
    # 名单整份写进日志。
    innertest_roster_open_ids: frozenset[str] = field(default_factory=frozenset, repr=False)
    # 文档投递独立消费循环（Issue #341 S-ES-3）的租户域名——不是密钥，是拼文档
    # 链接用的裸域名（例如 gv3qfk4q2rp.feishu.cn，见
    # ``adapters/feishu_docx_delivery.py`` 模块文档「文档 URL 的构造」）。**可选，
    # 默认 ``None``**：未配置时 ``apps/gateway/document_delivery.py`` 的
    # ``assemble_document_delivery_consumer`` 整体不注册这条循环（失败关闭，与
    # ``roster_audit.duty_not_registered`` 等既有姿态一致），不是"用一个猜测的
    # 域名硬跑"。飞书没有开放接口能查询"当前租户的裸域名"，只能由运维在部署时
    # 显式提供。
    tenant_domain: str | None = None

    # markdown 官方转换开关（Issue #408 正式方案接线；Issue #467／rc22 S-4 起
    # 代码默认开启）——不套 `LINGXI_GATEWAY_` 前缀，环境变量名固定为
    # `LINGXI_DOCX_MARKDOWN_CONVERT`（任务合同明确给定的名字，未来若有第二个
    # 进程需要判断同一份开关，两处读同一个变量名，同 `bot_open_id`/
    # `innertest_roster_open_ids` 不套前缀的理由一致）。默认 `True`（开，
    # Issue #467 执行 PM 裁定：docx 转换已通过 rc21 stage 探针验证，不再需要
    # 运维每次显式开启）。**Trace #544 S-7c 换了它管的是哪条路，没有换它本身**：
    # 现在 `True` ＝ ``apps/gateway/document_delivery.py`` 的
    # ``_create_docx_body`` 走 ``adapters/feishu_docx_delivery.py::
    # LarkDocxDelivery.create_document_with_markdown``（服务端 ``docs_ai`` 一次
    # 建档写全文），`False` ＝ 两步纯文本段落路径。显式关闭用精确值 `"0"`；
    # 历史值 `"1"`（翻转前唯一的开启值，已经写进现网 stage 配置）**必须继续
    # 解析成开启**，翻转默认值不得让这些既有配置的语义漂移。
    # **它保留的理由是止损**：``docs_ai`` 在飞书开放平台没有公开文档页，限流与
    # 长度上限官方无契约——留一个不改代码、不重新构建镜像就能退回纯段落路径的
    # 闸门，代价只是一个已经存在的配置项。打开前提**不再包含**
    # ``docx:document.block:convert``（convert 端点已不再被调用；一次建档用
    # ``tenant_access_token`` 即可，stage 探针实测无需新增 scope）。失败语义
    # （Issue #499 裁定，rc25 沿用）：判定为降级时**降级交付**——如实告知用户
    # 格式已简化，不是整次失败；其余一切失败仍然一律交付失败/结果不明（失败
    # 关闭），**不静默退回段落路径**。见 ``deploy/.env.example`` 对应条目与
    # ``feishu_docx_delivery`` 模块文档「服务端一次建档写全文」一节。
    markdown_convert_enabled: bool = True

    # 群聊@机器人固定引导（Issue #318，#328 v1.0 裁定 #5）：机器人自身 open_id，
    # 只用于精确判定"这条群消息是不是 @ 了机器人本身"。刻意不套 `LINGXI_GATEWAY_`
    # 前缀——命名由裁定 #5 拍板，这是机器人这个身份本身的事实，不是 gateway 进程的
    # 私有配置项，与 `innertest_roster_open_ids` 不套前缀同一条纪律。**未配置＝这
    # 条功能整体关闭＝维持此前"群聊完全静默"的现状（失败关闭）**；部署时把它填成
    # 什么值（经 bot info 接口取一次落 env）不在本次改动范围内。
    bot_open_id: str | None = None

    # 「公司+职能→指标名」翻译映射的外置路径（Issue #320 的 gateway 侧接线，
    # Trace #544 S-2c 修复对抗审查 P-1）。与 `innertest_roster_open_ids` 同一条
    # 纪律：**刻意不套 `LINGXI_GATEWAY_` 前缀**，读的是 scheduler 侧同一个变量名
    # `LINGXI_COMPANY_FUNCTION_METRIC_MAP_PATH`——它不是 gateway 私有配置，而是
    # 「这台机器认哪一份指标映射」这个必须两个进程一致的共享事实。
    #
    # **为什么 gateway 必须读它**：管理员在管理卡上确认一个本地权限动作后，gateway
    # 侧当场做定向重算并**立即发布**权限范围；scheduler 侧次日日批再算一次。此前
    # gateway 三个调用点硬读随包默认映射，只有 scheduler 读外置文件——外置文件一旦
    # 启用，同一个人的权限范围会在"管理动作立即发布"与"次日日批"之间来回翻转，每次
    # 翻转都是用户可见的真实权限变化。
    #
    # **可选，默认 `None`＝未配置＝落回随包默认映射**（生产刻意不配这个变量，与外置
    # 能力交付前逐字节一致，见 `deploy/.env.example` 该条目）——不配**不是**启动
    # 失败。**错配不是未配**：配了但指向的文件缺失或格式非法，三个调用点各自按自己
    # 既有的失败关闭姿态处理（定向重算不发布任何范围并留痕、职位展开拒绝这次管理
    # 动作、管理卡目录降级为空），**没有一处静默回落随包默认**——那正是双真相的来源。
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


def _innertest_roster_open_ids(env: Mapping[str, str]) -> frozenset[str]:
    """内测名单闸的 gateway 侧解析（Issue #302 S-N-01 的纵深，opus 批量审查 P1）。

    刻意直接读 ``env.get("LINGXI_INNERTEST_ROSTER_OPEN_IDS")``——不经过 `_text()`
    那层 ``LINGXI_GATEWAY_`` 前缀包装：这不是 gateway 私有配置，是与 scheduler
    共享同一个变量名的名单（见 :class:`GatewayConfig` 该字段的文档）。解析规则
    也不重新实现：整段委托给
    :func:`lingxi.core.identity.innertest_roster_gate.parse_innertest_roster`，
    与 ``apps/scheduler/config.py`` 的 ``SchedulerConfig.from_env`` 复用同一个
    函数——同一套「未配置/空白→空集合＝全拒；任意条目不合法→整份拒绝」的语义，
    不写第二份解析逻辑等着两边慢慢漂移。
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
    """文档投递独立消费循环的租户域名（Issue #341 S-ES-3）：未配置即 ``None``
    （循环不注册，见 :class:`GatewayConfig` 该字段的文档）；配了就在构造期校验
    形状——裸域名、不含协议/路径/空白，与
    ``adapters.feishu_docx_delivery._require_tenant_domain`` 同一条校验，这里
    提前跑一遍是为了让一个拼错的域名在**启动期**就失败关闭，而不是等到第一次
    真正建文档、准备拼链接时才发现。
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
    """markdown 官方转换开关（Issue #408 正式方案接线；Issue #467／rc22 S-4
    翻转默认值）：未配置或为空——``True``（代码默认开启：docx 转换已通过
    rc21 stage 探针验证，见 Issue #442，不再需要运维每次显式开启）。显式关闭
    用精确值 ``"0"``——``False``。历史值 ``"1"``（翻转前唯一的开启值）——仍然
    ``True``，与"未配置"同义，保证已经写了 ``LINGXI_DOCX_MARKDOWN_CONVERT=1``
    的既有 stage 配置在这次翻转后行为不变。配置了但不是 ``""``/``"0"``/
    ``"1"``——启动即失败，与 ``apps/worker/config.py::_document_delivery_
    enabled`` 同一姿态（错配不是未配，一个拼错的值不该被静默当成任一状态
    长期放行）。

    刻意直接读 ``env.get("LINGXI_DOCX_MARKDOWN_CONVERT")``，不经过本文件
    ``_text()`` 的 ``LINGXI_GATEWAY_`` 前缀包装——变量名由任务合同显式给定，
    理由同 ``bot_open_id``/``innertest_roster_open_ids`` 不套前缀。
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
    """外置指标映射路径（Trace #544 S-2c）。

    刻意直接读 ``env.get(METRIC_MAP_PATH_ENV)``，不经过本文件 ``_text()`` 的
    ``LINGXI_GATEWAY_`` 前缀包装——理由见 :class:`GatewayConfig` 该字段的文档。
    解析规则也不重新实现：整段委托给
    :func:`lingxi.adapters.company_function_metric_map_file.parse_metric_map_path`，
    与 ``apps/scheduler/config.py`` 的 ``SchedulerConfig.metric_map_path`` 复用
    **同一个函数**——同一个变量值在两个进程里只可能解释成同一份文件，不写第二套
    解释逻辑等着两边慢慢漂移（这正是本项要根治的缺陷本身）。

    未配置 → ``None``（落回随包默认映射，进程照常启动）；配了但含空白字符 →
    启动即失败关闭，与本文件其余校验错误同一条纪律，只报变量名不回显值。
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
    """机器人自身 open_id（Issue #318 群聊@机器人固定引导）。

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
        queue_delay_hint_seconds=_number(env, "QUEUE_DELAY_HINT_SECONDS", 12.0),
        admin_group_chat_id=admin_group_chat_id,
        alert_policy=alert_policy,
        feishu_base_url=_text(env, "FEISHU_BASE_URL") or DEFAULT_FEISHU_BASE_URL,
        card_failure_injection=_card_failure_injection(env),
        innertest_roster_open_ids=_innertest_roster_open_ids(env),
        tenant_domain=_tenant_domain(env),
        markdown_convert_enabled=_markdown_convert_enabled(env),

        bot_open_id=_bot_open_id(env),
        metric_map_path=_metric_map_path(env),
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
