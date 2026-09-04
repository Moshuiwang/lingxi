"""scheduler 进程的配置装配：环境变量 → :class:`SchedulerConfig`。

原模块头部说明与退出语义等进程级说明仍在包的 ``__init__.py``，这里只留配置
对象本身与它的校验规则。
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from lingxi.adapters.company_function_metric_map_file import (
    METRIC_MAP_PATH_ENV,
    parse_metric_map_path,
)
from lingxi.adapters.postgres import (
    DEFAULT_POSTGRES_TIMEOUTS,
    PostgresTimeoutConfigError,
    PostgresTimeouts,
)
from lingxi.core.alerting import AlertPolicy
from lingxi.core.identity.roster_snapshot import DEFAULT_SNAPSHOT_STALE_AFTER
from lingxi.core.permission.mcp_readiness import (
    DEFAULT_PROBE_TIMEOUT_SECONDS,
    ReadinessSchedule,
)

DEFAULT_INTERVAL_SECONDS = 60

# 飞书开放平台地址来自配置，代码里只有一个可被覆盖的默认值（断言 V-部署-01）。
DEFAULT_FEISHU_BASE_URL = "https://open.feishu.cn/open-apis"

#: 首次开通编排的默认执行线程数。**每条链最长阻塞十五分钟**（发布等待 + 就绪
#: 预算），因此它就是「同一时刻最多几个人在开通」；认领量由执行器剩余容量压住，
#: 超额事件原样留库、下一轮照捞，不会丢。取 8 是按关联组织规模在最坏情形下于约
#: 一天内排空推算的默认值，可用 ``LINGXI_ONBOARDING_WORKERS`` 调大调小。上界
#: 64 是防御性上限，不是容量规划。
DEFAULT_ONBOARDING_WORKERS = 8
MAX_ONBOARDING_WORKERS = 64

#: 组织快照同步整轮预算的默认值（秒）。只挂在组织快照专用的
#: ``FeishuDirectoryClient`` 实例上，不影响开通链令牌读取语义。默认 1200 秒
#: （约 3.5 倍于实测一轮全量遍历耗时，且明显小于令牌寿命），可运维调大调小
#: （``LINGXI_ORG_SNAPSHOT_ROUND_BUDGET_SECONDS``），不强制校验，只要仍然远大于
#: 实际一轮耗时、明显小于令牌寿命。
DEFAULT_ORG_SNAPSHOT_ROUND_BUDGET_SECONDS = 1200.0

#: 组织快照整轮预算的下限（秒）。**不是把实测基线硬编码成校验值**——只挡"明显
#: 不可能跑完一轮"的误配：配成低于这个数必然导致每一轮都撞预算，而失败路径的
#: 语义是"保留上一份、不覆盖基线"，快照会静默地永远停在旧数据上。这是比
#: ``OrgSnapshotSyncDuty`` 连续撞线告警更早的一道防线：错配在进程启动时就快速
#: 失败。
MIN_ORG_SNAPSHOT_ROUND_BUDGET_SECONDS = 60.0


class _Secret(str):
    """只影响 ``repr`` 的字符串子类：配置对象被打印时不吐出凭据。"""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 行为由 SchedulerConfigTest 覆盖
        return "'<已脱敏>'"


def _required(source: Mapping[str, str], name: str) -> str:
    """必需的环境变量：缺失快速失败，只报变量名，不回显任何值。"""

    value = (source.get(name) or "").strip()
    if not value:
        raise ValueError(f"缺少必需的环境变量：{name}")
    return value


def _parse_optional_identifier(source: Mapping[str, str], name: str) -> str | None:
    """可选的外部标识：缺失返回 ``None``，配了但带空白就快速失败。

    **错配不是未配**——一个带了换行的 Base token 静默降级成"没配"，会让相应职责
    悄悄不注册，而运维那边看到的是"我明明配了"。错误消息只报变量名，不回显值。
    """

    value = (source.get(name) or "").strip()
    if not value:
        return None
    if any(character.isspace() for character in value):
        raise ValueError(f"环境变量 {name} 不得包含空白字符（不回显取到的值）")
    return value


def _parse_interval_seconds(source: Mapping[str, str]) -> int:
    raw = (source.get("LINGXI_SCHEDULER_INTERVAL_SECONDS") or "").strip()
    if not raw:
        return DEFAULT_INTERVAL_SECONDS
    try:
        interval = int(raw)
    except ValueError as error:
        raise ValueError("环境变量 LINGXI_SCHEDULER_INTERVAL_SECONDS 必须是正整数秒") from error
    if interval <= 0:
        raise ValueError("环境变量 LINGXI_SCHEDULER_INTERVAL_SECONDS 必须是正整数秒")
    return interval


def _parse_roster_snapshot_stale_after(source: Mapping[str, str]) -> timedelta:
    raw = (source.get("LINGXI_ROSTER_SNAPSHOT_STALE_AFTER_HOURS") or "").strip()
    if not raw:
        return DEFAULT_SNAPSHOT_STALE_AFTER
    try:
        stale_hours = float(raw)
    except ValueError as error:
        raise ValueError(
            "环境变量 LINGXI_ROSTER_SNAPSHOT_STALE_AFTER_HOURS 必须是 0 到 8760 之间的小时数"
        ) from error
    # 上界一年：一个大到没有边的阈值等于把超龄告警关掉，而"关掉了"这件事
    # 不该由一个看起来像数字的配置悄悄完成。
    if not 0 < stale_hours <= 24 * 365:
        raise ValueError(
            "环境变量 LINGXI_ROSTER_SNAPSHOT_STALE_AFTER_HOURS 必须是 0 到 8760 之间的小时数"
        )
    return timedelta(hours=stale_hours)


def _parse_mcp_token_encrypt_key(source: Mapping[str, str]) -> str | None:
    raw = (source.get("LINGXI_MCP_TOKEN_ENCRYPT_KEY") or "").strip()
    if not raw:
        return None

    from lingxi.adapters.mcp_token_cipher import load_master_key

    # 只为**校验形状**（base64 的 32 字节），解出来的字节立刻丢弃：配置对象里
    # 存的仍是原始字符串，真正的加解密对象在装配那一步才构造。校验函数不回显
    # 收到的值。错配在这里快速失败，而不是等到某一轮重算才炸。
    load_master_key(raw)
    return _Secret(raw)


def _parse_query_mcp_endpoint(source: Mapping[str, str]) -> str | None:
    endpoint = _parse_optional_identifier(source, "LINGXI_QUERY_MCP_ENDPOINT")
    if endpoint is not None and not endpoint.startswith("https://"):
        # 误配 http:// 会让用户自己的 MCP 令牌明文上路。只报变量名，不回显取到的值。
        raise ValueError(
            "环境变量 LINGXI_QUERY_MCP_ENDPOINT 必须以 https:// 开头（不回显取到的值）"
        )
    return endpoint


def _parse_query_mcp_timeout_seconds(source: Mapping[str, str]) -> int:
    raw = (source.get("LINGXI_QUERY_MCP_TIMEOUT_SECONDS") or "").strip()
    if raw:
        try:
            probe_timeout = int(raw)
        except ValueError as error:
            raise ValueError("环境变量 LINGXI_QUERY_MCP_TIMEOUT_SECONDS 必须是正整数秒") from error
    else:
        probe_timeout = DEFAULT_PROBE_TIMEOUT_SECONDS
    try:
        # 用**合同节奏**加这个超时构造一次：不合法的组合（超过一个轮询间隔、非正
        # 整数）在**进程启动时**就失败，而不是等到某个用户第一次需要就绪确认时
        # 才炸。节奏本身没有环境变量——它是合同值，不该有一个能让它漂移的配置项。
        ReadinessSchedule(probe_timeout_seconds=probe_timeout)
    except ValueError as error:
        raise ValueError(f"环境变量 LINGXI_QUERY_MCP_TIMEOUT_SECONDS 不合法：{error}") from None
    return probe_timeout


def _parse_onboarding_workers(source: Mapping[str, str]) -> int:
    raw = (source.get("LINGXI_ONBOARDING_WORKERS") or "").strip()
    if not raw:
        return DEFAULT_ONBOARDING_WORKERS
    try:
        onboarding_workers = int(raw)
    except ValueError:
        raise ValueError("环境变量 LINGXI_ONBOARDING_WORKERS 必须是正整数") from None
    if onboarding_workers < 1 or onboarding_workers > MAX_ONBOARDING_WORKERS:
        raise ValueError(
            f"环境变量 LINGXI_ONBOARDING_WORKERS 必须在 1 到 {MAX_ONBOARDING_WORKERS} 之间"
        )
    return onboarding_workers


def _parse_innertest_roster_open_ids(source: Mapping[str, str]) -> frozenset[str]:
    from lingxi.core.identity.innertest_roster_gate import (
        InnerTestRosterConfigError,
        parse_innertest_roster,
    )

    try:
        # 未设置/空白解析成空集合（闸对任何人拒绝，见该模块「默认关闭＝全拒」）；
        # 含无法识别条目在这里**快速失败**，与本文件其余"配了但格式不对"变量
        # 同一条纪律——错配不是未配，静默降级会让人以为闸在正常放行内测名单。
        return parse_innertest_roster(source.get("LINGXI_INNERTEST_ROSTER_OPEN_IDS"))
    except InnerTestRosterConfigError as error:
        raise ValueError(f"环境变量 LINGXI_INNERTEST_ROSTER_OPEN_IDS 不合法：{error}") from None


def _parse_org_snapshot_round_budget_seconds(source: Mapping[str, str]) -> float:
    raw = (source.get("LINGXI_ORG_SNAPSHOT_ROUND_BUDGET_SECONDS") or "").strip()
    if not raw:
        return DEFAULT_ORG_SNAPSHOT_ROUND_BUDGET_SECONDS
    try:
        budget_seconds = float(raw)
    except ValueError as error:
        raise ValueError(
            f"环境变量 LINGXI_ORG_SNAPSHOT_ROUND_BUDGET_SECONDS 必须是正数秒，收到 {raw!r}"
        ) from error
    # `float()` 会把 `"inf"`/`"nan"`/`"1e309"`（超出表示范围，溢出为 inf）这类
    # 字符串原样解析成非有限值：`<= 0` 的判据挡不住它们（`inf > 0`、`nan` 的
    # 比较恒为 False），一旦漏过去，预算检查会恒不成立，等于悄悄关闭了这道上界。
    if not math.isfinite(budget_seconds):
        raise ValueError(
            "环境变量 LINGXI_ORG_SNAPSHOT_ROUND_BUDGET_SECONDS 必须是有限的正数秒，"
            f"收到 {raw!r}（解析为 {budget_seconds!r}）"
        )
    if budget_seconds < MIN_ORG_SNAPSHOT_ROUND_BUDGET_SECONDS:
        raise ValueError(
            "环境变量 LINGXI_ORG_SNAPSHOT_ROUND_BUDGET_SECONDS 必须至少 "
            f"{MIN_ORG_SNAPSHOT_ROUND_BUDGET_SECONDS:.0f} 秒"
            "（预算必须大于全量轮实际耗时；低于任何真实一轮耗时的预算是配置错误，"
            "会让快照永远无法更新），"
            f"收到 {budget_seconds!r}"
        )
    return budget_seconds


def _parse_admin_group_chat_id(source: Mapping[str, str]) -> str | None:
    raw = (source.get("LINGXI_ADMIN_GROUP_CHAT_ID") or "").strip()
    if not raw:
        return None
    from lingxi.adapters.feishu_group_message import validate_group_chat_id

    # 校验函数不回显取到的值，只报变量名与期望形状。
    return validate_group_chat_id(raw)


def _parse_bitable_coordinates(source: Mapping[str, str]) -> tuple[str | None, ...]:
    """权限表与存量令牌源的四个 Base/表标识，均可选、均按同一条纪律解析。"""

    return (
        _parse_optional_identifier(source, "LINGXI_PERMISSION_BITABLE_APP_TOKEN"),
        _parse_optional_identifier(source, "LINGXI_PERMISSION_BITABLE_TABLE_ID"),
        _parse_optional_identifier(source, "LINGXI_STOCK_TOKEN_BITABLE_APP_TOKEN"),
        _parse_optional_identifier(source, "LINGXI_STOCK_TOKEN_BITABLE_TABLE_ID"),
    )


def _parse_postgres_timeouts(source: Mapping[str, str]) -> PostgresTimeouts:
    try:
        return PostgresTimeouts.from_env(source)
    except PostgresTimeoutConfigError as error:
        raise ValueError(str(error)) from None


def _parse_alert_policy(source: Mapping[str, str]) -> AlertPolicy:
    try:
        return AlertPolicy.from_mapping(source)
    except ValueError as error:
        raise ValueError(str(error)) from None


@dataclass(frozen=True)
class SchedulerConfig:
    postgres_dsn: str = field(repr=False)
    credential_key: str = field(repr=False)
    # 凭据文件的宿主机路径。部署契约：必须指向跨部署持久的挂载路径，镜像替换与
    # 重启不得丢失，否则每次部署都要重新授权。
    credential_path: str
    feishu_app_id: str
    feishu_app_secret: str = field(repr=False)
    feishu_base_url: str
    interval_seconds: int
    postgres_timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS
    # 管理群 chat_id。**可选**：没有它进程照常启动，只是不注册审计日报职责。
    # 做成可选而不是必需，是因为它只服务三个职责中的一个——为一个尚未接线的职责
    # 让整个 scheduler 起不来，会把「日报没配」升级成「凭据轮换也停了」。
    # 配了但格式不对则**快速失败**：那是错配，不是未配，静默降级会让人以为在发日报。
    admin_group_chat_id: str | None = None
    alert_policy: AlertPolicy = field(default_factory=AlertPolicy)
    # 花名册多维表格的 Base 与表标识。**可选**，与群 ID 同一姿态：缺了只是不注册
    # 日报职责，不让整个 scheduler 起不来。它们是外部标识而不是凭据，但同样只从
    # 环境变量来，不进代码（`V-花名册-28` 的同一条理由：外部标识一旦入码就会被
    # 日志、CI 输出和工单一路复制出去）。
    roster_app_token: str | None = None
    roster_table_id: str | None = None
    # 快照超龄阈值。默认 48 小时，理由写在
    # :data:`lingxi.core.identity.roster_snapshot.DEFAULT_SNAPSHOT_STALE_AFTER`。
    roster_snapshot_stale_after: timedelta = DEFAULT_SNAPSHOT_STALE_AFTER
    # MCP 令牌主密钥（base64 的 32 字节）。**可选**，与群 ID 同一姿态：缺了只是不注册
    # 每日权限重算职责，不让整个 scheduler 起不来；配了但不是合法主密钥则**快速失败**
    # （错配不是未配）。它是凭据，因此不进 ``repr``（`_Secret`）。
    mcp_token_encrypt_key: str | None = field(default=None, repr=False)
    # 当前权限多维表格的 Base 与表标识。**可选**，与花名册那一对同一姿态：缺了
    # 只是不注册权限发布职责。它们是外部标识不是凭据，但同样只从环境变量来
    # （`V-花名册-28` 的同一条理由）。
    permission_app_token: str | None = None
    permission_table_id: str | None = None
    # 存量令牌只读源的 Base 与表标识。**可选，与上面这对同一姿态**：缺任一个，
    # 开通链的 adopt-or-issue 能力就不装配，直接走原签发路径。生产环境里这两个
    # 坐标很可能与上面那对 permission_* 指向同一个 Base/表，但刻意用独立的环境
    # 变量注入：这条只读能力可以单独打开/关闭，不与发布面的读写坐标绑在一起。
    stock_token_app_token: str | None = None
    stock_token_table_id: str | None = None
    # 问数 MCP 的就绪探针端点。**可选**：缺了只是**就绪与通知那一面**不装配，发布面照常
    # ——发布不依赖探针。配了但不是 https 则快速失败：误配 http 会让用户令牌明文上路。
    query_mcp_endpoint: str | None = None
    # 单次就绪探针的传输超时。它同时是 `ReadinessSchedule` 算「结论最晚什么时候落地」的
    # 输入，因此装配层必须让探针传输层与就绪节奏用**同一个数**（见
    # `lingxi.adapters.query_mcp_probe.QueryMcpProbe.timeout_seconds` 的文档）。
    query_mcp_timeout_seconds: int = DEFAULT_PROBE_TIMEOUT_SECONDS
    # 用户环境根目录（``/var/lib/lingxi/users``）。首次开通编排要在它下面建家目录
    # 并写按用户的 ``.mcp.json``。**可选**：缺了只是首次开通编排不注册，其余职责
    # 照常。
    user_env_root: str | None = None
    # 开通编排的执行线程数。**每条链最长会阻塞十五分钟**，因此它就是"同一时刻最多几个人
    # 在开通"；认领量由执行器剩余容量压住（见 apps/scheduler/onboarding.py）。
    onboarding_workers: int = DEFAULT_ONBOARDING_WORKERS
    # 内测名单闸：开通链最前端的白名单，只在 open_id 命中时才继续走身份定位，
    # 命中之外一律得到「内测未开放」且零建档。**默认空集合＝全拒**，不是
    # "未启用"——空集合本身就是失败关闭的全部实现。配了但含无法识别条目在
    # `from_env` 里快速失败，不会静默放行别人。``repr=False``：名单是一批
    # open_id，与本文件其余凭据字段同一条纪律，不进 `repr(config)`。
    innertest_roster_open_ids: frozenset[str] = field(default_factory=frozenset, repr=False)
    # 排完发布意图之后，等发布消费职责把它真的写出去并读回一致的上限。等不到是本侧故障
    # （`LX-ONBOARD-001`），不是 MCP 同步超时。
    onboarding_publish_wait_seconds: float = 120.0
    # 组织快照同步整轮预算（秒）。**可选**，默认值与取值依据见
    # :data:`DEFAULT_ORG_SNAPSHOT_ROUND_BUDGET_SECONDS`；运维可按需调大或调小，
    # 不需要另行产品拍板。撞线后本轮读取原样中止，走既有的
    # `org_snapshot_sync.read_failed` → 退避 → 保留上一份完成批次路径（不覆盖库里
    # 最近一次成功批次），只影响"一轮最多愿意为限频重试花多久"，不改变任何产品语义。
    org_snapshot_round_budget_seconds: float = DEFAULT_ORG_SNAPSHOT_ROUND_BUDGET_SECONDS
    # 「公司+职能→指标名」翻译映射的外置路径。**可选**：缺省时 loader 回落随包
    # 发布的默认文件；配了就**优先**于包内默认，让指标映射表的维护人能够编辑即
    # 生效，不必为一行映射改动走一次完整镜像构建发布。**错配不是未配**：配了但
    # 指向的文件缺失或格式非法时，职责响亮地不注册，不静默回落包内默认，见
    # `adapters/company_function_metric_map_file.py` 模块文档。
    company_function_metric_map_path: str | None = None

    ENVIRONMENT_KEYS = (
        "LINGXI_POSTGRES_DSN",
        "LINGXI_POSTGRES_CONNECT_TIMEOUT_SECONDS",
        "LINGXI_POSTGRES_STATEMENT_TIMEOUT_SECONDS",
        "LINGXI_POSTGRES_LOCK_TIMEOUT_SECONDS",
        "LINGXI_DELEGATED_CREDENTIAL_KEY",
        "LINGXI_DELEGATED_CREDENTIAL_PATH",
        "LINGXI_FEISHU_APP_ID",
        "LINGXI_FEISHU_APP_SECRET",
        "LINGXI_FEISHU_BASE_URL",
        "LINGXI_SCHEDULER_INTERVAL_SECONDS",
        "LINGXI_ADMIN_GROUP_CHAT_ID",
        "LINGXI_ROSTER_BITABLE_APP_TOKEN",
        "LINGXI_ROSTER_BITABLE_TABLE_ID",
        "LINGXI_ROSTER_SNAPSHOT_STALE_AFTER_HOURS",
        "LINGXI_MCP_TOKEN_ENCRYPT_KEY",
        "LINGXI_PERMISSION_BITABLE_APP_TOKEN",
        "LINGXI_PERMISSION_BITABLE_TABLE_ID",
        "LINGXI_STOCK_TOKEN_BITABLE_APP_TOKEN",
        "LINGXI_STOCK_TOKEN_BITABLE_TABLE_ID",
        "LINGXI_QUERY_MCP_ENDPOINT",
        "LINGXI_QUERY_MCP_TIMEOUT_SECONDS",
        "LINGXI_USER_ENV_ROOT",
        "LINGXI_ONBOARDING_WORKERS",
        "LINGXI_INNERTEST_ROSTER_OPEN_IDS",
        "LINGXI_ORG_SNAPSHOT_ROUND_BUDGET_SECONDS",
        METRIC_MAP_PATH_ENV,
        "LINGXI_ALERT_HEARTBEAT_TIMEOUT_SECONDS",
        "LINGXI_ALERT_QUEUED_TIMEOUT_SECONDS",
        "LINGXI_ALERT_RUNNING_HEARTBEAT_TIMEOUT_SECONDS",
        "LINGXI_ALERT_SEND_FAILURE_WINDOW_SECONDS",
        "LINGXI_ALERT_SEND_FAILURE_THRESHOLD",
        "LINGXI_ALERT_DEDUPE_WINDOW_SECONDS",
        "LINGXI_ALERT_RECOVERY_STABLE_SECONDS",
        "LINGXI_ALERT_RETRY_BASE_SECONDS",
        "LINGXI_ALERT_RETRY_FACTOR",
        "LINGXI_ALERT_RETRY_CEILING_SECONDS",
    )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> SchedulerConfig:
        """一次性读完全部配置。缺项只报变量名，绝不回显取到的值。"""

        source = os.environ if environ is None else environ

        interval = _parse_interval_seconds(source)
        roster_snapshot_stale_after = _parse_roster_snapshot_stale_after(source)
        mcp_token_encrypt_key = _parse_mcp_token_encrypt_key(source)
        query_mcp_endpoint = _parse_query_mcp_endpoint(source)
        probe_timeout = _parse_query_mcp_timeout_seconds(source)
        onboarding_workers = _parse_onboarding_workers(source)
        innertest_roster_open_ids = _parse_innertest_roster_open_ids(source)
        org_snapshot_round_budget_seconds = _parse_org_snapshot_round_budget_seconds(source)
        company_function_metric_map_path = _parse_optional_identifier(source, METRIC_MAP_PATH_ENV)
        admin_group_chat_id = _parse_admin_group_chat_id(source)
        (
            permission_app_token,
            permission_table_id,
            stock_token_app_token,
            stock_token_table_id,
        ) = _parse_bitable_coordinates(source)
        postgres_timeouts = _parse_postgres_timeouts(source)
        alert_policy = _parse_alert_policy(source)

        return cls(
            postgres_dsn=_Secret(_required(source, "LINGXI_POSTGRES_DSN")),
            postgres_timeouts=postgres_timeouts,
            credential_key=_Secret(_required(source, "LINGXI_DELEGATED_CREDENTIAL_KEY")),
            credential_path=_required(source, "LINGXI_DELEGATED_CREDENTIAL_PATH"),
            feishu_app_id=_required(source, "LINGXI_FEISHU_APP_ID"),
            feishu_app_secret=_Secret(_required(source, "LINGXI_FEISHU_APP_SECRET")),
            feishu_base_url=(source.get("LINGXI_FEISHU_BASE_URL") or "").strip()
            or DEFAULT_FEISHU_BASE_URL,
            interval_seconds=interval,
            admin_group_chat_id=admin_group_chat_id,
            alert_policy=alert_policy,
            roster_app_token=_parse_optional_identifier(source, "LINGXI_ROSTER_BITABLE_APP_TOKEN"),
            roster_table_id=_parse_optional_identifier(source, "LINGXI_ROSTER_BITABLE_TABLE_ID"),
            roster_snapshot_stale_after=roster_snapshot_stale_after,
            mcp_token_encrypt_key=mcp_token_encrypt_key,
            permission_app_token=permission_app_token,
            permission_table_id=permission_table_id,
            stock_token_app_token=stock_token_app_token,
            stock_token_table_id=stock_token_table_id,
            query_mcp_endpoint=query_mcp_endpoint,
            query_mcp_timeout_seconds=probe_timeout,
            user_env_root=_parse_optional_identifier(source, "LINGXI_USER_ENV_ROOT"),
            onboarding_workers=onboarding_workers,
            innertest_roster_open_ids=innertest_roster_open_ids,
            org_snapshot_round_budget_seconds=org_snapshot_round_budget_seconds,
            company_function_metric_map_path=company_function_metric_map_path,
        )

    @property
    def metric_map_path(self) -> Path | None:
        """``company_function_metric_map_path`` 解析成 ``Path``；未配置为 ``None``。

        供 ``apps/scheduler/assembly.py`` 直接传给 ``load_company_function_metric_map``
        ——把字符串转 ``Path`` 的这一步放在配置对象自己身上，而不是每个调用点各写
        一次三元表达式。解释规则本身不写在这里：整段委托给
        :func:`~lingxi.adapters.company_function_metric_map_file.parse_metric_map_path`
        ——``apps/gateway/config.py`` 读同一个变量时用的是同一个函数，两个进程因此
        不可能对"该读哪一份映射"给出不同答案，见该函数文档。
        """

        return parse_metric_map_path(self.company_function_metric_map_path)
