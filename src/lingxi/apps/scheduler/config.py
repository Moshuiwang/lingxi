"""scheduler 进程的配置装配：环境变量 → :class:`SchedulerConfig`。

从 :mod:`lingxi.apps.scheduler`（#237 拆分）搬出——原模块头部docstring 与退出语义等
进程级说明仍在包的 ``__init__.py``，这里只留配置对象本身与它的校验规则。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Mapping

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


class _Secret(str):
    """只影响 ``repr`` 的字符串子类：配置对象被打印时不吐出凭据。"""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 行为由 SchedulerConfigTest 覆盖
        return "'<已脱敏>'"


@dataclass(frozen=True)
class SchedulerConfig:
    postgres_dsn: str = field(repr=False)
    credential_key: str = field(repr=False)
    # 凭据文件的宿主机路径。部署契约：必须指向跨部署持久的挂载路径，
    # 镜像替换与重启不得丢失——否则每次部署都要重新授权（产品负责人
    # 2026-08-05 明确以「无需特殊处理」为目标、重新授权仅作保底）。
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
    # 当前权限多维表格的 Base 与表标识（Issue #156 / S-C-03b）。**可选**，与花名册那一对
    # 同一姿态：缺了只是不注册权限发布职责。它们是外部标识不是凭据，但同样只从环境变量来
    # （`V-花名册-28` 的同一条理由）。
    permission_app_token: str | None = None
    permission_table_id: str | None = None
    # 问数 MCP 的就绪探针端点。**可选**：缺了只是**就绪与通知那一面**不装配，发布面照常
    # ——发布不依赖探针。配了但不是 https 则快速失败：误配 http 会让用户令牌明文上路。
    query_mcp_endpoint: str | None = None
    # 单次就绪探针的传输超时。它同时是 `ReadinessSchedule` 算「结论最晚什么时候落地」的
    # 输入，因此装配层必须让探针传输层与就绪节奏用**同一个数**（见
    # `lingxi.adapters.query_mcp_probe.QueryMcpProbe.timeout_seconds` 的文档）。
    query_mcp_timeout_seconds: int = DEFAULT_PROBE_TIMEOUT_SECONDS

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
        "LINGXI_QUERY_MCP_ENDPOINT",
        "LINGXI_QUERY_MCP_TIMEOUT_SECONDS",
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
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "SchedulerConfig":
        """一次性读完全部配置。缺项只报变量名，绝不回显取到的值。"""

        source = os.environ if environ is None else environ

        def required(name: str) -> str:
            value = (source.get(name) or "").strip()
            if not value:
                raise ValueError(f"缺少必需的环境变量：{name}")
            return value

        raw_interval = (source.get("LINGXI_SCHEDULER_INTERVAL_SECONDS") or "").strip()
        if raw_interval:
            try:
                interval = int(raw_interval)
            except ValueError as error:
                raise ValueError("环境变量 LINGXI_SCHEDULER_INTERVAL_SECONDS 必须是正整数秒") from error
            if interval <= 0:
                raise ValueError("环境变量 LINGXI_SCHEDULER_INTERVAL_SECONDS 必须是正整数秒")
        else:
            interval = DEFAULT_INTERVAL_SECONDS

        def optional_identifier(name: str) -> str | None:
            """可选的外部标识：缺失返回 ``None``，配了但带空白就快速失败。

            与群 ID 同一条纪律——**错配不是未配**。一个带了换行的 Base token 静默降级
            成"没配"，会让日报职责悄悄不注册，而运维那边看到的是"我明明配了"。
            错误消息只报变量名，不回显取到的值。
            """

            value = (source.get(name) or "").strip()
            if not value:
                return None
            if any(character.isspace() for character in value):
                raise ValueError(f"环境变量 {name} 不得包含空白字符（不回显取到的值）")
            return value

        raw_stale_hours = (source.get("LINGXI_ROSTER_SNAPSHOT_STALE_AFTER_HOURS") or "").strip()
        if raw_stale_hours:
            try:
                stale_hours = float(raw_stale_hours)
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
            roster_snapshot_stale_after = timedelta(hours=stale_hours)
        else:
            roster_snapshot_stale_after = DEFAULT_SNAPSHOT_STALE_AFTER

        raw_token_key = (source.get("LINGXI_MCP_TOKEN_ENCRYPT_KEY") or "").strip()
        if raw_token_key:
            from lingxi.adapters.mcp_token_cipher import load_master_key

            # 只为**校验形状**（base64 的 32 字节），解出来的字节立刻丢弃：配置对象里
            # 存的仍是原始字符串，真正的加解密对象在装配那一步才构造。校验函数不回显
            # 收到的值。错配在这里快速失败，而不是等到某一轮重算才炸。
            load_master_key(raw_token_key)
            mcp_token_encrypt_key: str | None = _Secret(raw_token_key)
        else:
            mcp_token_encrypt_key = None

        query_mcp_endpoint = optional_identifier("LINGXI_QUERY_MCP_ENDPOINT")
        if query_mcp_endpoint is not None and not query_mcp_endpoint.startswith("https://"):
            # 误配 http:// 会让用户自己的 MCP 令牌明文上路。只报变量名，不回显取到的值。
            raise ValueError("环境变量 LINGXI_QUERY_MCP_ENDPOINT 必须以 https:// 开头（不回显取到的值）")

        raw_probe_timeout = (source.get("LINGXI_QUERY_MCP_TIMEOUT_SECONDS") or "").strip()
        if raw_probe_timeout:
            try:
                probe_timeout = int(raw_probe_timeout)
            except ValueError as error:
                raise ValueError(
                    "环境变量 LINGXI_QUERY_MCP_TIMEOUT_SECONDS 必须是正整数秒"
                ) from error
        else:
            probe_timeout = DEFAULT_PROBE_TIMEOUT_SECONDS
        try:
            # 用**合同节奏**加这个超时构造一次：不合法的组合（超过一个轮询间隔、非正整数）
            # 在**进程启动时**就失败，而不是等到某个用户第一次需要就绪确认时才炸。
            # 节奏本身（立即 / 每 180 秒 / 900 秒预算）没有环境变量——它是合同值，
            # 不该有一个能让它漂移的配置项（同 `IDLE_CONVERSATION_SWEEP_AFTER`）。
            ReadinessSchedule(probe_timeout_seconds=probe_timeout)
        except ValueError as error:
            raise ValueError(f"环境变量 LINGXI_QUERY_MCP_TIMEOUT_SECONDS 不合法：{error}") from None

        raw_chat_id = (source.get("LINGXI_ADMIN_GROUP_CHAT_ID") or "").strip()
        if raw_chat_id:
            from lingxi.adapters.feishu_group_message import validate_group_chat_id

            # 校验函数不回显取到的值，只报变量名与期望形状。
            admin_group_chat_id: str | None = validate_group_chat_id(raw_chat_id)
        else:
            admin_group_chat_id = None

        try:
            postgres_timeouts = PostgresTimeouts.from_env(source)
        except PostgresTimeoutConfigError as error:
            raise ValueError(str(error)) from None
        try:
            alert_policy = AlertPolicy.from_mapping(source)
        except ValueError as error:
            raise ValueError(str(error)) from None

        return cls(
            postgres_dsn=_Secret(required("LINGXI_POSTGRES_DSN")),
            postgres_timeouts=postgres_timeouts,
            credential_key=_Secret(required("LINGXI_DELEGATED_CREDENTIAL_KEY")),
            credential_path=required("LINGXI_DELEGATED_CREDENTIAL_PATH"),
            feishu_app_id=required("LINGXI_FEISHU_APP_ID"),
            feishu_app_secret=_Secret(required("LINGXI_FEISHU_APP_SECRET")),
            feishu_base_url=(source.get("LINGXI_FEISHU_BASE_URL") or "").strip() or DEFAULT_FEISHU_BASE_URL,
            interval_seconds=interval,
            admin_group_chat_id=admin_group_chat_id,
            alert_policy=alert_policy,
            roster_app_token=optional_identifier("LINGXI_ROSTER_BITABLE_APP_TOKEN"),
            roster_table_id=optional_identifier("LINGXI_ROSTER_BITABLE_TABLE_ID"),
            roster_snapshot_stale_after=roster_snapshot_stale_after,
            mcp_token_encrypt_key=mcp_token_encrypt_key,
            permission_app_token=optional_identifier("LINGXI_PERMISSION_BITABLE_APP_TOKEN"),
            permission_table_id=optional_identifier("LINGXI_PERMISSION_BITABLE_TABLE_ID"),
            query_mcp_endpoint=query_mcp_endpoint,
            query_mcp_timeout_seconds=probe_timeout,
        )
