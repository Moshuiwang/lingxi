"""``lingxi-scheduler``：定时职责进程。

进程现在跑**七个**职责，由 :class:`SchedulerLoop` 按同一个周期依次驱动：

1. **专用授权凭据轮换**（:class:`CredentialRotationLoop`）——「四达文档会议助手」
   ``refresh_token`` 的到期续期；
2. **九十天保留清理**（:class:`RetentionCleanupDuty`）——调用数据库里的受限清理
   函数回收到期内容（Issue #54 / S9）；
3. **空闲会话到点清理**（:class:`IdleConversationSweepDuty`）——会话空闲满两小时
   由 scheduler 周期扫描并主动清除已送达的投递正文，不依赖下一次任务入队
   （Issue #151、2026-08-14 补充决定、`V-投递-10`）；
4. **权限链九十天到期清理**（:class:`PermissionRetentionSweepDuty`）——擦掉
   ``publish_outbox`` 到期的内容快照（里面有邮箱与姓名）、删掉 ``mcp_sync_check``
   的到期行。两个应用层方法随 Issue #156 的 S-C-01/S-C-02 一起交付，但**当时没有接
   任何生产调用方**（Epic C 冻结缺陷 F1，与 #151 空闲会话清理是同一个形状的缺陷）：
   跑满九十天后含个人数据的行会原样留存，而文档已经在以现在时描述它在跑。
   本职责是这两条的调用点；判定记录那一面缺 MCP 令牌主密钥时不装配，内容擦除
   那一面无条件运行。
5. **花名册资料比对与管理群审计日报**（:class:`RosterAuditDuty`）——每天（UTC 日界）
   跑一轮：读一整轮花名册 → 更新持久快照或保留上一份 → 与建档存档三字段比对 →
   有差异（或快照超龄 / 无快照）就向管理群发一条日报（Issue #52）。第五个职责是
   **条件注册**的，四个前置任缺其一就不注册，并留下一条指名原因的审计
   （断言 ``V-花名册-29``）：管理群 ID、花名册 Base ``app_token``、花名册
   ``table_id``（三者都是可选环境变量，见 :class:`SchedulerConfig`），以及花名册读取
   所用的短期令牌供给。不注册时进程照常启动、其余职责照常运行。

   **令牌供给自 Issue #215 起由本模块装配**（方案 C 主接线，产品负责人 2026-08-18
   裁定）：凭据轮换职责按需消费一次一次性 ``refresh_token``，把派生的短期
   ``access_token`` 放进**进程内**持有者（不落盘、不进日志与审计），日报侧按新鲜度
   取用。因此第四个前置现在只取决于配置，三个环境变量配齐即注册。消费频率随之从约
   5.6 天一次变成按日一次，上界由两道守卫钉住：进程内的每日预算，以及随凭据落盘、
   重启也抹不掉的 ``refresh_consumed_at``。**唯一消费者的边界没有变**——日报不自己
   换令牌，两个消费者共用一条一次性令牌正是 2026-08-08 授权码被烧那次事故的形状。
   **代码层接上不等于真的读得到花名册**：真实读取所需的专用主体凭据自 2026-08-09 起
   未落盘（Issue #52 的 G-READ 判定），当前部署也还没配那三个变量，因此这条链在真实
   环境里一轮都没跑过。
6. **每日权限重算**（:class:`~lingxi.apps.scheduler.permission_refresh.
   PermissionRefreshDuty`）——每天（UTC 日界）跑一轮：花名册快照必须是今天取的
   → 读当前有效银河批次 → 逐个已开通用户重算权限并排发布意图（Issue #156 的
   S-C-03a）。第六个职责同样是**条件注册**的：缺 MCP 令牌主密钥就不注册，并留下
   **恰一条**指名原因的审计。

   它排在花名册审计日报**之后**，而「先花名册、再银河重算」（`V-权限-07`）不只靠
   这个位置：职责自己还有一条数据判据——花名册快照的 ``captured_at`` 不是今天就整轮
   不跑。位置只保证同一轮里的先后，判据才保证"用的是今天的花名册"。#215 把令牌供给接上
   之后，这条判据**第一次有可能为真**：日报职责一旦注册并真的读到一轮花名册，快照就被换成
   本轮时刻那一份，同一轮里紧随其后的重算即可通过——在那之前它恒为假（供给写死 ``None``、
   日报根本不注册）。
7. **权限发布与就绪确认**（:class:`~lingxi.apps.scheduler.permission_publish.
   PermissionPublishDuty`）——**每轮**跑：收殓崩溃留下的在途意图 → 消费待发布意图
   （写权限表 + 逐字段读回核对）→ 对已发布的每一条按 tick 节奏推进一次就绪探针 →
   探针成功（或权限已清空）就给用户发一条范围变化通知（Issue #156 的 S-C-03b）。
   它是 S-C-01 那个发布执行器的**第一个生产调用方**。

   第七个职责是**分两面条件注册**的：发布面缺权限表 Base / 表标识 / 令牌供给任一项
   就整个不注册（**恰一条**审计）；就绪与通知那一面缺 MCP 令牌主密钥或问数 MCP 端点时
   **只有那一面不装配**（同样**恰一条**审计），发布面照常——发布不依赖探针。

   **为什么它与每日重算是两个职责**：重算靠一个当日水位保证同日至多一轮，而发布消费
   必须每轮都跑；合并会让当天第一轮之后排进来的意图一直等到第二天。
   **单一写入负责人**：发布执行、就绪探针与用户通知全部在本进程内，不另起消费者。

架构设计把定时职责单独分给本进程，理由是"定时职责与请求路径无关，混在一起会让
重启语义不清"。2026-08-05 在 `tz` 的复验实测到这条正好被违反：测试资产把续期扫描
挂在飞书长连接进程内的常驻线程上，把长连接进程 kill 掉后扫描线程无声停止，没有
任何独立信号提示"续期已经不再运行"（[Issue #16 复验记录]
(https://github.com/Moshuiwang/lingxi/issues/16#issuecomment-5188063325)）。

**职责之间互不牵连**（断言 V-保留-15）。``SchedulerLoop.run_once`` 逐个职责地捕获
异常：清理连续失败不会让凭据轮换这一轮被跳过，反之亦然。这一条必须由代码结构
保证而不是靠"两个职责都不会抛异常"——保留清理会因为数据库权限、连接、锁等待失败，
而它失败时最不该发生的事情就是把一条一次性凭据的续期窗口一起拖没。

本模块只做组装：配置从环境变量来，轮换规则在
:mod:`lingxi.core.identity.credentials`，存取在
:mod:`lingxi.adapters.delegated_credentials`（宿主机文件保管，选项 A 决策），飞书调用在
:mod:`lingxi.adapters.feishu_directory`，受限清理函数调用在
:mod:`lingxi.adapters.retention`，权限链两张表的应用层到期处置在
:mod:`lingxi.adapters.postgres_permission_publish` 与
:mod:`lingxi.adapters.postgres_mcp_token`。

退出语义（断言 V-部署-03、V-保留-17）：收到 ``SIGTERM`` / ``SIGINT`` 后**停止领取
新工作**，把已经领取的那一次做完，然后退出。半途中断一次轮换会留下一个"已经向飞书
换过、但没写回数据库"的窗口，而 ``refresh_token`` 一次性有效——那个窗口等于凭据丢失。
清理侧没有对应窗口：一次调用就是一个数据库事务，被打断只会整体回滚。
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Protocol

from lingxi.adapters.postgres import (
    DEFAULT_POSTGRES_TIMEOUTS,
    PostgresTimeoutConfigError,
    PostgresTimeouts,
)
from lingxi.core.identity.access_token_supply import (
    AccessTokenUnavailable,
    DerivedAccessTokenHolder,
    RosterAccessTokenProvider,
)
from lingxi.core.identity.credentials import (
    AuthorizationGrant,
    CredentialAction,
    CredentialSaveOutcome,
    DerivedAccessToken,
    RefreshAlreadyConsumedToday,
    RefreshOutcome,
    decide_after_refresh,
)
from lingxi.core.identity.identifiers import redact_identifier
from lingxi.core.identity.roster_audit import ArchivedIdentity, RosterAuditReport, compare_roster
from lingxi.core.identity.roster_report import render_daily_report_content
from lingxi.core.identity.roster_snapshot import (
    DEFAULT_SNAPSHOT_STALE_AFTER,
    DailyRosterSource,
    RosterRound,
    RosterSnapshotUpdater,
    SnapshotDecision,
)
from lingxi.apps.scheduler.permission_publish import (
    PermissionPublishDuty,
    PermissionPublishReport,
    ReadinessFollowUp,
)
from lingxi.apps.scheduler.permission_refresh import (
    PERMISSION_REFRESH_REASON,
    PERMISSION_REVOKE_REASON,
    PermissionRefreshDuty,
    PermissionRefreshReport,
)
from lingxi.core.permission.mcp_readiness import (
    DEFAULT_PROBE_TIMEOUT_SECONDS,
    ReadinessSchedule,
)
from lingxi.core.alerting import (
    AlertDispatcher,
    AlertingDuty,
    AlertManager,
    AlertPolicy,
    AlertSender as _AlertSender,
    AuditSink,
)

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 60

# 新凭据写库的退避重试间隔（秒）。飞书侧旧凭据在续期成功那一刻已作废，
# 这里的每一次重试都是在挽救一条一次性凭据；用 _stop.wait 而不是 sleep，
# 让 SIGTERM 仍能立即打断等待。
SAVE_RETRY_BACKOFF_SECONDS = (0.2, 1.0, 3.0)
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


@dataclass(frozen=True)
class RotationReport:
    claimed: int = 0
    rotated: int = 0
    revoked: int = 0
    # 领取到了、也换到了新凭据，但**新凭据没有落盘**——期间发生了新授权，本链结果作废。
    # 与写盘失败同一条口径：当前生效的凭据不是本次产生的，因此不计 ``rotated``；
    # 但也不撤销（新凭据不能被旧链连带删掉），因此不计 ``revoked``。
    superseded: int = 0


class _Vault(Protocol):
    def claim_due(self, *, for_supply: bool = ...) -> Any: ...
    def save(
        self,
        *,
        subject_open_id: str,
        grant: AuthorizationGrant,
        issued_at: Any = ...,
        replacing_generation: Any = ...,
        expected_registered_subject_open_id: Any = ...,
        refresh_consumed_at: Any = ...,
    ) -> Any: ...
    def revoke(self, *, reason: str, generation: Any = ...) -> bool: ...


class _Authorization(Protocol):
    def refresh(self, current: AuthorizationGrant) -> tuple[AuthorizationGrant, Any]: ...


class CredentialRotationLoop:
    """按飞书返回有效期的 80% 触发轮换的扫描循环，**兼一次性 ``refresh_token`` 的唯一消费者**。

    循环本身不判断"该不该轮换"——到期判定写在 SQL 的领取条件里（轮换点已由
    :func:`lingxi.core.identity.credentials.rotation_deadline` 算好并落库），
    失败后的处置写在 :func:`decide_after_refresh`。这里只负责编排与退出。

    Issue #215 给它加了第二个入口 :meth:`refresh_for_supply`：花名册日报每天要一次派生
    短期令牌，而换取它必须消费同一条一次性 ``refresh_token``。**唯一消费者这条边界因此
    没有变**——日报不自己去换，而是让本职责按需换一次，再从进程内持有者取。产品负责人
    2026-08-18 裁定接受由此带来的按日消费节奏（原为约 5.6 天一次）。

    两个入口共用同一套纪律：领取（原子置位消费标记）→ 换新 → **先成功落盘、再交出**。
    次序不能反：``refresh_token`` 一次性有效，"换到了但没落盘"等于凭据丢失，而先把派生
    令牌交出去会让这条丢失路径在日报正常工作的表象下发生。
    """

    name = "凭据轮换"

    def __init__(
        self,
        *,
        vault: _Vault,
        authorization: _Authorization,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        stop: threading.Event | None = None,
        holder: DerivedAccessTokenHolder | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._vault = vault
        self._authorization = authorization
        self._interval_seconds = interval_seconds
        # 与同一进程内的其他职责共享停止标志：SIGTERM 必须一次让所有职责都停止
        # 领取新工作，而不是只停下恰好持有信号处理函数的那一个。
        self._stop = threading.Event() if stop is None else stop
        # 派生短期令牌的进程内持有者。没有它时轮换照常工作，只是派生令牌被丢弃
        # （接线之前的行为）；有它时到期轮换与按需续期都把令牌喂进去。
        self._holder = holder
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # 「上一次消费换回来的那份派生令牌不可用」的记号，值是**那一次消费的权威时刻**。
        # 没有它的话，当天后续每一轮都只会看到「今天已经换过了」，而真正的原因（飞书没给
        # 寿命、或令牌一到手就临期）再也不会出现在审计里——一个真实故障被一个例行拒绝
        # 盖住（两路审查 O7）。
        #
        # 用消费时刻而不是凭据世代号来绑定：这个时刻由凭据库在锁内生成、随新凭据一起
        # 落盘，因此**每一次消费都唯一**——换了凭据代际（人工重授权、或另一个进程完成
        # 的轮换）必然带来不同的消费时刻或干脆没有消费标记，记号自动失效。世代号做不到
        # 这一点：``save`` 生成的新世代号根本不会回到这里（收口轮 P2-c②）。
        self._derived_unusable_at: datetime | None = None

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    @property
    def derived_token_holder(self) -> DerivedAccessTokenHolder | None:
        """本职责写入派生短期令牌的那个持有者。

        只读暴露，供装配处断言"日报侧读的正是轮换职责写的那一份"——这条链断了不会有
        任何用例变红，除非它有一个可观察的接缝（两路审查 O4）。
        """

        return self._holder

    def request_stop(self) -> None:
        self._stop.set()

    def run_once(self) -> RotationReport:
        """领取至多一条到期凭据并处理它。已经在停止中则一条都不领。"""

        if self._stop.is_set():
            return RotationReport()
        # 先收殓崩溃窗口留下的「已消费未落库」行：它们的旧令牌已被飞书作废，
        # 不收殓就会在租期结束后被当成正常凭据再领取一次（Codex 复查发现）。
        stale_collector = getattr(self._vault, "revoke_stale_consumed", None)
        if callable(stale_collector):
            stale_collector()
        if self._stop.is_set():
            # SIGTERM 可能在收殓等待文件锁期间到达：领取前必须再看一次，
            # 否则会在关闭宽限期里再启动一条最长 20 秒的续期请求（终轮 Codex）。
            return RotationReport()
        claim = self._vault.claim_due()
        if claim is None:
            return RotationReport()

        # 权威消费时刻由凭据库在文件锁内生成并随领取返回；这里绝不自己再算一个。
        consumed_at = getattr(claim, "consumed_at", None) or self._clock()
        derived: Any = None
        try:
            replacement, derived = self._authorization.refresh(claim.grant)
            outcome = RefreshOutcome.ROTATED
        except Exception as error:  # noqa: BLE001 - 任何异常都不足以证明"旧凭据还能用"
            replacement = None
            outcome = RefreshOutcome.FAILED if _is_definite_failure(error) else RefreshOutcome.INDETERMINATE
            logger.warning("专用授权续期未成功 outcome=%s error=%s", outcome.value, type(error).__name__)

        claim_generation = getattr(claim, "generation", None) or None
        if decide_after_refresh(outcome) is CredentialAction.ROTATE and replacement is not None:
            saved = self._save_with_retry(
                subject_open_id=claim.subject_open_id,
                replacement=replacement,
                replacing_generation=claim_generation,
                refresh_consumed_at=consumed_at,
            )
            if saved is CredentialSaveOutcome.SAVED:
                logger.info("专用授权凭据已轮换 subject=%s", redact_identifier(claim.subject_open_id))
                # **落盘成功之后**才把派生令牌交给进程内持有者：反过来会让"续期成功但
                # 写盘失败＝凭据丢失"这条路径在日报照常工作的表象下发生。
                self._remember_derived(derived, consumed_at=consumed_at)
                return RotationReport(claimed=1, rotated=1)
            if saved is CredentialSaveOutcome.SUPERSEDED:
                # 期间发生了新授权：本链的新凭据被丢弃，而旧的那条已经在飞书那边消费掉。
                # 不撤销（不能连带删掉新凭据），但**一个令牌都不交出**——交出去日报会照常
                # 工作到令牌过期为止，把"这条链已经死了"整整盖住那么久（两路审查 P1）。
                logger.warning(
                    "轮换结果已被期间的新授权取代，本链结果作废，派生短期令牌不予交出 subject=%s",
                    redact_identifier(claim.subject_open_id),
                )
                # 不计 rotated：当前生效的凭据不是本次产生的（与写盘失败同一条口径）。
                return RotationReport(claimed=1, superseded=1)
            # 新凭据没能落库：旧的此刻已被飞书作废，继续留着只会让下一轮拿死
            # 凭据再撞一次墙。撤销并用可与普通失败区分的日志请求人工重新授权
            # （独立复查发现：此前这里的异常会带着仅存于内存的新凭据一起消失）。
            logger.error(
                "不可恢复：续期成功但新凭据写库失败，旧凭据已被飞书作废，需人工重新授权 subject=%s",
                redact_identifier(claim.subject_open_id),
            )
            self._vault.revoke(reason="rotation_persist_failed", generation=claim_generation)
            return RotationReport(claimed=1, revoked=1)

        # 只撤销领取到的那一代：期间的新授权不得被旧链失败连带删除（终轮 Codex）。
        self._vault.revoke(reason=f"refresh_{outcome.value}", generation=claim_generation)
        return RotationReport(claimed=1, revoked=1)

    def refresh_for_supply(self) -> None:
        """**按需**消费一次续期，把派生短期令牌交给进程内持有者（Issue #215）。

        由花名册日报的令牌供给（:class:`~lingxi.core.identity.access_token_supply.
        RosterAccessTokenProvider`）调用，本身不返回令牌——令牌只经持有者流转，而持有者
        只在**新凭据成功落盘之后**才被写入。这条次序是本方法存在的全部理由：日报不自己
        去消费一次性 ``refresh_token``，唯一消费者仍是本职责。

        与到期轮换的唯一不同写在一个参数里：``for_supply=True``。它把"放开到期判定"
        与"每 UTC 日至多一次"捆成一件事，由凭据库在**自己的文件锁内、用锁内的当前时刻**
        判定。上界因此只有一份判据、只有一个时钟：

        - 进程重启、崩溃重启循环、同一宿主机上的第二个实例都绕不过它；
        - 人工重授权换来的新凭据没有消费标记，**当天立刻可以再换一次**——这是已接受的
          语义，而任何一份进程内账本副本都会认不出新凭据、继续拒到第二天（收口轮 P1）。

        每一次调用都要去开一次文件锁（拒绝时也是）。这是刻意的取舍：正常情况下日报一天
        只问一次（持有者里有新鲜令牌时根本不会走到这里），异常情况下每轮一次文件锁的
        代价，换的是"上界只有一个权威"。

        失败一律抛 :class:`AccessTokenUnavailable`，**只带分类、不带值、traceback 里
        不展示原始异常**；凭据的处置与到期轮换完全一致（续期失败撤销、写盘失败撤销并按
        不可恢复留痕），因为"这条一次性凭据还能不能用"与是谁触发的这次续期无关。
        """

        if self._stop.is_set():
            # 停止中不再开启任何一次续期：半途中断的续期等于凭据丢失（模块头注释）。
            raise AccessTokenUnavailable("scheduler_stopping")
        try:
            claim = self._vault.claim_due(for_supply=True)
        except RefreshAlreadyConsumedToday as error:
            raise AccessTokenUnavailable(self._refusal_reason(error)) from None
        if claim is None:
            # 没有可领取的凭据（未授权、已撤销、或正被另一条链消费中）。
            raise AccessTokenUnavailable("no_credential_available")
        consumed_at = getattr(claim, "consumed_at", None) or self._clock()

        claim_generation = getattr(claim, "generation", None) or None
        try:
            replacement, derived = self._authorization.refresh(claim.grant)
        except Exception as error:  # noqa: BLE001 - 任何异常都不足以证明"旧凭据还能用"
            outcome = RefreshOutcome.FAILED if _is_definite_failure(error) else RefreshOutcome.INDETERMINATE
            logger.warning("按需续期未成功 outcome=%s error=%s", outcome.value, type(error).__name__)
            self._vault.revoke(reason=f"refresh_{outcome.value}", generation=claim_generation)
            # from None：原始异常留在 __cause__ 里，任何一次 traceback 打印都会把响应
            # 正文（可能含令牌）带进日志。排障信息以净化过的类名走上面那行日志。
            raise AccessTokenUnavailable(f"refresh_{outcome.value}") from None

        saved = self._save_with_retry(
            subject_open_id=claim.subject_open_id,
            replacement=replacement,
            replacing_generation=claim_generation,
            refresh_consumed_at=consumed_at,
        )
        if saved is CredentialSaveOutcome.FAILED:
            logger.error(
                "不可恢复：按需续期成功但新凭据写库失败，旧凭据已被飞书作废，需人工重新授权 subject=%s",
                redact_identifier(claim.subject_open_id),
            )
            self._vault.revoke(reason="rotation_persist_failed", generation=claim_generation)
            # 落盘失败就**不交出令牌**：让日报这一轮失败，好过在凭据已经丢失的情况下
            # 照常发出日报、把丢失掩盖到下一次轮换才被发现。
            raise AccessTokenUnavailable("credential_persist_failed")
        if saved is CredentialSaveOutcome.SUPERSEDED:
            # 期间有新授权：本链的新凭据被丢弃，旧的已在飞书那边作废。不撤销（新凭据
            # 不能被旧链连带删掉），但同样**什么都不交出**——这条链已经没有活着的凭据了。
            logger.warning(
                "按需续期的结果已被期间的新授权取代，本链结果作废，派生短期令牌不予交出 subject=%s",
                redact_identifier(claim.subject_open_id),
            )
            raise AccessTokenUnavailable("no_credential_available")

        logger.info("专用授权凭据已按需轮换 subject=%s", redact_identifier(claim.subject_open_id))
        if not self._remember_derived(derived, consumed_at=consumed_at):
            # 凭据没有丢（已落盘），只是这份派生令牌不可用。不撤销、不重试。
            raise AccessTokenUnavailable("derived_token_unusable")

    def _refusal_reason(self, error: RefreshAlreadyConsumedToday) -> str:
        """今天已经换过一次时，对外报哪个分类。

        默认报上界本身；但如果**正是那一次**换到的派生令牌不可用，真实原因是它，不是
        "今天已经换过了"。原因漂移会让一个真实故障看起来像例行拒绝（O7）。

        比对的是消费时刻：凭据上记着的那一刻必须与本进程记下"不可用"的那一刻是同一个。
        换了凭据代际（人工重授权、或另一个进程完成的轮换）时两者必然不同，记号自动失效
        ——那种情况下本进程对新凭据一无所知，只能如实报上界。
        """

        if self._derived_unusable_at is not None and self._derived_unusable_at == error.consumed_at:
            return "derived_token_unusable"
        return "refresh_already_consumed_today"

    def _remember_derived(self, derived: Any, *, consumed_at: datetime) -> bool:
        """把派生令牌交给进程内持有者。没有持有者或令牌不可用时返回 ``False``。

        "可用"的判据由持有者给：它只在这份令牌**当场就能通过新鲜度判定**时才算成功，
        因此一份一到手就临期的令牌不会被误当成拿到了（收口轮 P2-c①）。

        令牌值不进日志：这里只记"有没有拿到可用的一份"，以及"哪一次消费换回来的那份
        不可用"。
        """

        if self._holder is None:
            # 没有装配持有者（接线之前的形态）。不是"这一份不可用"，不记那个状态。
            return False
        if not isinstance(derived, DerivedAccessToken):
            logger.warning("续期未交出派生短期令牌，花名册读取本轮无令牌可用")
            self._derived_unusable_at = consumed_at
            return False
        if not self._holder.store(derived, now=self._clock()):
            logger.warning("派生短期令牌不可用（寿命未知或一到手就临期），不予缓存")
            self._derived_unusable_at = consumed_at
            return False
        self._derived_unusable_at = None
        return True

    def _save_with_retry(
        self,
        *,
        subject_open_id: str,
        replacement: Any,
        replacing_generation: str | None = None,
        refresh_consumed_at: datetime | None = None,
    ) -> CredentialSaveOutcome:
        """新凭据落盘带短退避重试：一次瞬时抖动不该报废一条一次性凭据。

        返回**三态**而不是布尔（:class:`CredentialSaveOutcome`）：轮换收尾只关心"要不要
        撤销"，因此"落盘了"与"被新授权取代"曾被压成同一个真值；但派生短期令牌关心的是
        "这条链还有没有活着的凭据"，两者在那个问题上是相反的答案。
        """

        for delay_seconds in (0.0, *SAVE_RETRY_BACKOFF_SECONDS):
            if delay_seconds:
                self._stop.wait(delay_seconds)
            try:
                saved = self._vault.save(
                    subject_open_id=subject_open_id,
                    grant=replacement,
                    replacing_generation=replacing_generation,
                    expected_registered_subject_open_id=subject_open_id,
                    refresh_consumed_at=refresh_consumed_at,
                )
                if saved is False:
                    # 世代不符或主体登记 CAS 未通过＝期间有新授权：**新凭据没有落盘**。
                    # 对撤销判定来说这算已妥善收尾，对派生令牌来说这条链已经死了。
                    return CredentialSaveOutcome.SUPERSEDED
                return CredentialSaveOutcome.SAVED
            except Exception as error:  # noqa: BLE001 - 记录后重试，最终失败由调用方处置
                logger.warning("新凭据写库失败，将重试 error=%s", type(error).__name__)
        return CredentialSaveOutcome.FAILED

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as error:  # noqa: BLE001 - 定时职责不因一轮异常而终止
                logger.error("本轮续期扫描异常，下一轮继续 error=%s", type(error).__name__)
            if self._stop.is_set():
                break
            self._stop.wait(self._interval_seconds)
        logger.info("续期扫描已停止领取并退出")


class _Cleaner(Protocol):
    def run_once(self) -> Any: ...


class RetentionCleanupDuty:
    """九十天保留清理职责：每轮调用一次受限清理函数。

    每轮**只调用一次**，不在职责内部循环到删空。理由有两条：一次调用就是一个
    数据库事务，单次调用因此天然没有半删状态；而"删空为止"会让一个积压了很多
    到期行的库在单轮里长时间持锁，也让 ``SIGTERM`` 的退出时间不再有上界。
    积压由下一轮继续，清理本来就是幂等的（断言 V-保留-10）。
    """

    name = "保留清理"

    def __init__(self, *, cleaner: _Cleaner, stop: threading.Event | None = None) -> None:
        self._cleaner = cleaner
        self._stop = threading.Event() if stop is None else stop

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        self._stop.set()

    def run_once(self) -> Any:
        """已经在停止中就一批都不领。返回 ``None`` 表示本轮未执行。"""

        if self._stop.is_set():
            return None
        report = self._cleaner.run_once()
        # 摘要只有表名与计数。清理函数的返回里根本没有行内容，日志因此不可能
        # 带出人员数据（断言 V-保留-14）。
        summary = getattr(report, "summary", None)
        rendered = summary() if callable(summary) else "保留清理：本轮完成"
        # 有表因为拿不到锁而让路时，这一轮**没有做完**，不能记成正常完成。
        # 两者的删除数都可能是 0，只有日志级别与标记能把它们分开：一张长期被占的表
        # 会在 INFO 流水里表现为一切正常，而内容一直没被回收——保留违规最不该有的
        # 形态就是它悄无声息（codex 二轮 P1-3）。
        blocked = getattr(report, "blocked_tables", ())
        if blocked:
            logger.warning("%s；本轮未清理完：%s 因锁等待超时让路，下一轮重试", rendered, "、".join(blocked))
        else:
            logger.info("%s", rendered)
        return report


class IdleConversationSweepDuty:
    """会话空闲满两小时的到点清理职责：每轮调用一次
    ``PostgresTaskQueue.sweep_idle_conversations``。

    2026-08-14 补充决定（数据库设计「问数结果投递事件与会话保留 Outbox」、
    `V-投递-10`）：会话空闲满两小时后，即使用户未再发起新的问数任务，已经送达
    的安全结果正文也必须由定时清理机制主动清除，不依赖下一次任务入队。#151 落地
    时只交付了应用层方法本身，没有接上任何生产调用方（内审 P2-2）；本职责补上
    这一条调用点，写法与 :class:`RetentionCleanupDuty` 同型——每轮只清一次，
    天然幂等，被打断只回滚这一批 ``UPDATE``，不留半清状态。
    """

    name = "空闲会话清理"

    def __init__(
        self, *, queue: Any, idle_after: timedelta, stop: threading.Event | None = None
    ) -> None:
        self._queue = queue
        self._idle_after = idle_after
        self._stop = threading.Event() if stop is None else stop

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        self._stop.set()

    def run_once(self) -> int | None:
        """已经在停止中就一批都不领。返回 ``None`` 表示本轮未执行，否则返回本轮
        清除了已送达正文的会话数（供日志/断言，不承载业务语义）。"""

        if self._stop.is_set():
            return None
        cleared = self._queue.sweep_idle_conversations(idle_after=self._idle_after)
        if cleared:
            logger.info("空闲会话清理：本轮清除 %s 个会话的已送达投递正文", cleared)
        return cleared


#: 会话空闲清理的固定窗口（产品合同「数据保留与删除」、`V-投递-10`）：不设配置，
#: 与 P2-1 修复同一取舍——业务常量不应该有一个能让它漂移的环境变量。
IDLE_CONVERSATION_SWEEP_AFTER = timedelta(hours=2)


class AuditSink(Protocol):
    """审计出口。

    ``audit_event`` 表属 S9，尚未建立；当前实现写结构化日志。职责只依赖这个签名，
    届时换实现不动职责代码。签名与 #57 网关侧的同名 Protocol 一致（结构化类型，
    两边互相满足），合并后可收敛成一份，不需要现在跨切片耦合。
    """

    def record(self, action: str, /, **fields: object) -> None: ...


class StructuredLogAuditSink:
    """把审计动作写成一行结构化日志。

    字段按**键名排序**输出：审计行会被比对和 grep，顺序随 ``PYTHONHASHSEED`` 变化的
    日志没法稳定断言。本类原样输出收到的字段——「不带资料值」这条约束属于调用方，
    对应断言 ``V-花名册-33`` 因此断在调用方产生的那几行上。
    """

    def record(self, action: str, /, **fields: object) -> None:
        rendered = " ".join(f"{key}={value}" for key, value in sorted(fields.items()))
        logger.info("审计 action=%s %s", action, rendered)


class _ExpiredPayloadRedactor(Protocol):
    """``publish_outbox`` 到期内容擦除的最小端口。实现是
    :class:`lingxi.adapters.postgres_permission_publish.PostgresPermissionPublishStore`。"""

    def redact_expired_payloads(self) -> int: ...


class _ExpiredCheckPurger(Protocol):
    """``mcp_sync_check`` 到期整行删除的最小端口。实现是
    :class:`lingxi.adapters.postgres_mcp_token.PostgresMcpTokenStore`。"""

    def purge_expired_checks(self) -> int: ...


@dataclass(frozen=True)
class PermissionRetentionReport:
    """一轮权限链到期处置的摘要。**只有计数与接线状态，没有任何行内容**。

    与 :class:`~lingxi.adapters.retention.RetentionReport` 同一条纪律（断言
    ``V-保留-14``）：进日志与审计的只能是"擦了几条、删了几行"，两个适配器方法的返回值
    本来也只有条数。
    """

    #: 本轮被擦成 ``'{}'`` 的 ``publish_outbox.payload`` 条数。
    redacted: int = 0
    #: 本轮被删掉的 ``mcp_sync_check`` 行数。
    purged: int = 0
    #: 判定记录那一面这一轮有没有装配（缺 MCP 令牌主密钥时为 ``False``）。
    checks_wired: bool = True

    def audit_facts(self) -> dict[str, Any]:
        return {
            "redacted": self.redacted,
            "purged": self.purged,
            "checks_wired": self.checks_wired,
        }


class PermissionRetentionSweepDuty:
    """权限发布链两张表的九十天到期内容处置职责：每轮各调用一次。

    两张表的处置方式不同，因为它们**能擦的东西**不同：

    - ``publish_outbox.payload`` 里有邮箱与姓名 → 擦成 ``'{}'``（
      :meth:`~lingxi.adapters.postgres_permission_publish.PostgresPermissionPublishStore.
      redact_expired_payloads`）。行本身留着——``user_id``、权限版本、状态与时间戳是
      "谁的哪一版权限什么时候发布成功过"这类运行事实。
    - ``mcp_sync_check`` **没有可识别内容列**（只有内部 ULID、权限版本、次序、时间、结论与
      错误码），没有列可擦 → 删整行（
      :meth:`~lingxi.adapters.postgres_mcp_token.PostgresMcpTokenStore.
      purge_expired_checks`）。

    到期判据在两个适配器方法自己的 SQL 里（各自的 ``content_expires_at``，由迁移
    ``0064``/``0065`` 的触发器固定），本职责**一个条件都不加**：清理职责能扩大删除面的
    唯一方式就是"顺手多删一点"，而多删掉的东西没有任何恢复路径。

    **为什么是一个独立职责，而不是并进 :class:`RetentionCleanupDuty`**：后者的删除逻辑
    整个在数据库里——迁移 ``0054`` 建的受限清理函数，属主是无登录角色
    ``lingxi_retention_owner``，而适配器 :mod:`lingxi.adapters.retention` **一条 DELETE 都
    不写**；"应用与 scheduler 角色即使误获普通 DELETE 也删不掉那些表"这条保障靠的正是
    这条分工。本职责这两条是**应用层**语句（为什么它们没有进那个受限函数，理由在迁移
    ``0064``/``0065`` 的文件头部），塞进那个适配器会让它模块文档第一句话当场不成立，
    两套权限模型也会混进同一个返回摘要里。形状照 :class:`IdleConversationSweepDuty`
    ——它是**同一个缺陷的先例**：#151 只交付了应用层清理方法、没有接任何生产调用方
    （内审 P2-2），补救办法就是在这里给它一个每轮都会跑的调用点。

    **每轮只调用一次，不循环到擦空 / 删空**（同 :class:`RetentionCleanupDuty` 的理由）：
    一次调用就是一个数据库事务，因此没有半擦状态；积压由下一轮继续，两条都是幂等的
    （断言 ``V-保留-10``），到期时间也不会因为处置迟到而后移（断言 ``V-保留-16``）。

    **失败关闭**：任何一面抛异常都先留一条**只有异常类型**的审计，再原样上抛，由
    :meth:`SchedulerLoop.run_once` 逐职责隔离。既不吞掉也不折成 0——一条被吞掉的异常会
    让"到期内容一直没被处置"表现为每轮一条正常的完成审计，而保留违规最不该有的形态
    就是它悄无声息。
    """

    name = "权限链到期清理"

    def __init__(
        self,
        *,
        outbox: _ExpiredPayloadRedactor,
        checks: _ExpiredCheckPurger | None,
        audit: AuditSink,
        stop: threading.Event | None = None,
    ) -> None:
        self._outbox = outbox
        self._checks = checks
        self._audit = audit
        self._stop = threading.Event() if stop is None else stop

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    @property
    def checks_wired(self) -> bool:
        return self._checks is not None

    def request_stop(self) -> None:
        self._stop.set()

    def run_once(self) -> PermissionRetentionReport | None:
        """已经在停止中就一条都不处置。返回 ``None`` 表示本轮未执行。"""

        if self._stop.is_set():
            return None
        redacted = self._sweep("publish_outbox", self._outbox.redact_expired_payloads)
        purged = 0
        if self._checks is not None:
            purged = self._sweep("mcp_sync_check", self._checks.purge_expired_checks)
        report = PermissionRetentionReport(
            redacted=redacted, purged=purged, checks_wired=self._checks is not None
        )
        self._audit.record("permission_retention.completed", **report.audit_facts())
        if redacted or purged:
            # 一轮什么都没到期时不打日志：这条职责每分钟跑一次。
            logger.info(
                "权限链到期清理：publish_outbox 擦除 %s 条内容快照，mcp_sync_check 删除 %s 行",
                redacted,
                purged,
            )
        return report

    def _sweep(self, table: str, call: Callable[[], int]) -> int:
        try:
            return int(call())
        except Exception as error:
            # 只记表名与异常类型：异常正文可能带上被处置那一行的内容。
            self._audit.record(
                "permission_retention.sweep_failed",
                table=table,
                error=type(error).__name__,
            )
            logger.error("权限链到期清理失败 table=%s error=%s", table, type(error).__name__)
            raise


# _AlertSender/_PendingAlert/AlertDispatcher/AlertingDuty/_alert_utc 已迁移到
# lingxi.core.alerting（Issue #153，见本文件顶部的导入）：gateway 与 worker 也
# 需要装配同一套告警编排，三个 apps/<name> 互不 import，因此这段编排放进 core/
# （只编排注入接口，不直接做 I/O，与 core/conversation/pipeline.py 的
# EventPipeline 同一形状）。本模块沿用既有的 `_AlertSender`/`AlertDispatcher`/
# `AlertingDuty` 名字，既有测试不必改动。


class _BaselineReader(Protocol):
    def load_active_baseline(self) -> Sequence[ArchivedIdentity]: ...


class _GroupSender(Protocol):
    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None: ...


class _RosterSource(Protocol):
    """一轮花名册取用：交出用于比对的行与快照状态。

    实现是 :class:`lingxi.core.identity.roster_snapshot.DailyRosterSource`；职责只依赖
    这一个方法，因此全部调度与日报断言都能在没有数据库、没有网络的机器上跑完。
    """

    def current(self, *, now: datetime) -> RosterRound: ...


class RosterAuditDuty:
    """每日花名册资料比对与管理群审计日报（Issue #52）。

    一轮做四件事：读比对基线 → 取本轮花名册（读一整轮 + 更新或保留持久快照）→
    纯函数比对 → 该发就发一条日报。**存档三字段这一侧只读**（`V-花名册-14`）；
    唯一的写库发生在花名册快照那一侧，而它写的是花名册的副本，不是 `app_user`。

    **"该发就发"不等于"有差异才发"**。空差异日本来不发（`V-花名册-25`），但有两种
    情形下「今天没有差异」这句话本身不可信，沉默因此是最危险的输出：

    - **快照超龄**（`V-花名册-47`）：源头连续读不到，比对用的还是几天前那份花名册。
      产品负责人 2026-08-17 裁定：始终保留最近一份、超龄**按日报告警提醒、不自动删**；
    - **没有任何可用快照**（`V-花名册-48`）：这一天**不比对**。拿空行去比对会把全体
      已开通用户报成「花名册查无此人」——那正是空源保护要挡的形状。

    日报正文按产品负责人 2026-08-08 的 **D2 裁定**渲染：受控管理群可含用于定位的存档
    身份（姓名、工号），并写明快照时间与同步状态、日期一律 UTC（渲染细节见
    :mod:`lingxi.core.identity.roster_report`）。**日志与审计没有随之放宽**
    （`V-花名册-33`）：它们流向排障、CI 输出与工单，不在受控管理群那个范围里。

    **同日至多一次**（`V-花名册-31`）。判重靠 ``_completed_on`` 这个进程内的日期水位：

    - 单轮一次、同进程跨轮一次：由这个水位**硬保证**，与轮询周期无关；
    - 跨重启：**判重水位没有持久载体**，新进程的水位是空的，因此**重启当日会重发一份
      内容完全相同的日报**。这是产品负责人 2026-08-06 知情接受的残留（裁定 C2 / R2）：
      A 方案下报告由「花名册现值 + 存档」唯一确定，补跑产出同一份，重复是噪声不是错误。
      **原表述「零新表定案下没有持久载体」的前提已被 2026-08-08 的 D2 裁定覆盖**——
      仓库现在确有持久载体（`roster_snapshot`，迁移 `0063`），但它存的是**花名册读取
      结果**，不是判重水位；结论因此没变：跨重启的真幂等要等判重水位本身也有持久载体
      （或 ``audit_event`` 表落地）后再补。

      **快照的 ``captured_at`` 不能顶替判重水位**（S-B-04 核对过并放弃了这条捷径）：
      它回答的是"快照哪天换的"，而水位要回答的是"日报哪天发出去的"，两者只在最顺利的
      那条路径上重合。发送失败的那一天快照照样已经换成当天的——拿 ``captured_at``
      当水位，重启后会认为"今天已经做完"，于是**那一天的日报永远不会发出去**。用一个
      每天至多重发一份相同日报的噪声，换一个整天静默的失败，方向是反的。真幂等需要
      判重水位自己的持久列，属新迁移，不在本 Story 的授权范围内。

    水位在**发送成功之后**才置位。发送失败不算已发送，下一轮重试（`V-花名册-30`）。
    空差异日也置位：那一天的审计已经记过了，重复记只是噪声（`V-花名册-25`）。

    **重试与"不确定态"**。"发送失败就重试"这条规则有一个它自己看不见的缺口：HTTP
    请求已经被飞书收下、而响应在回程超时的那一刻，进程拿到的是异常，事实却是消息已经
    发出去了。重试于是重复投递一条日报。同一天的每一次发送——首次与全部重试——因此共用
    一个去重键（当日 UTC 日期），由 :func:`~lingxi.adapters.feishu_group_message.delivery_uuid`
    折成飞书的投递 `uuid` 交服务端去重。平台侧的去重窗口未经验证（属 L4a），所以这里
    承诺的是"重试携带同一个 `uuid`"这个代码事实，不是"平台一定不会重复投递"。
    """

    name = "花名册审计日报"

    def __init__(
        self,
        *,
        baseline_reader: _BaselineReader,
        roster_source: _RosterSource,
        sender: _GroupSender,
        audit: AuditSink,
        chat_id: str,
        clock: Callable[[], datetime] | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        self._baseline_reader = baseline_reader
        self._roster_source = roster_source
        self._sender = sender
        self._audit = audit
        self._chat_id = chat_id
        # 时钟注入：跨轮判重的用例要能自己决定「今天」是哪天，不能靠等到明天。
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._stop = threading.Event() if stop is None else stop
        self._completed_on: date | None = None

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    @property
    def completed_on(self) -> date | None:
        """已完成日报的那一天。``None`` 表示本进程实例今天还没发过。"""

        return self._completed_on

    def request_stop(self) -> None:
        self._stop.set()

    def run_once(self) -> RosterAuditReport | None:
        """跑一轮。返回 ``None`` 表示本轮没有执行比对（停止中，或今天已经做完）。"""

        if self._stop.is_set():
            # 已经在停止中：一轮都不开。停止之后必须 0 次发送（`V-花名册-20`）。
            return None
        now = self._clock()
        today = now.date()
        if self._completed_on == today:
            return None

        baseline = self._baseline_reader.load_active_baseline()
        # 读一整轮花名册并结算持久快照。回读到不自洽的快照
        # （:class:`~lingxi.adapters.postgres_roster_snapshot.RosterSnapshotInconsistent`）
        # 或写快照失败时，异常原样上抛，由 :class:`SchedulerLoop` 做职责级隔离并在
        # 下一轮重试（`V-花名册-17`）；水位不置位，因此这一天还没算做完。
        round_result = self._roster_source.current(now=now)
        snapshot = round_result.snapshot

        if snapshot.available:
            report = compare_roster(baseline, round_result.rows)
        else:
            # 一份快照都没有：**不比对**（`V-花名册-48`）。空行集会把全体已开通用户
            # 报成「花名册查无此人」，那是比"今天没日报"严重得多的错误输出。
            report = RosterAuditReport(examined=len(baseline))

        if report.is_empty and not snapshot.needs_attention:
            # 空差异日**不发日报**，只记一条审计。合同 :85 的语义是「通知待办」，
            # 没有待办却每天发一条「今天没事」，会让管理群很快学会忽略这个通知。
            self._audit.record(
                "roster_audit.no_difference",
                report_date=today.isoformat(),
                examined=report.examined,
                **snapshot.audit_facts(),
            )
            self._completed_on = today
            return report

        if self._stop.is_set():
            # 停止信号落在读取阶段（读库 + 读花名册都可能耗时）。这里再看一次，
            # 让这一轮成为**干净中断**：不发送、不置水位，什么都没发生。
            # 停止之后必须 0 次发送（`V-花名册-20`），而入口处那一次检查挡不住
            # "进来时还没停、读完才停"这条时序。重发由裁定 C2 的知情接受覆盖。
            logger.info("停止信号在花名册读取期间到达，本轮不发送日报")
            return report

        content = render_daily_report_content(
            report,
            report_date=today,
            # D2 的存档身份段取自**本轮基线**，不是花名册：管理员要定位的是 Lingxi
            # 这一侧的记录，而花名册当前值本来就要他自己去核实。
            identities={person.app_user_id: person for person in baseline},
            snapshot=snapshot,
        )
        try:
            # 同一天的日报（含失败重试）共用一个去重键：不确定态下的重试因此携带
            # 同一个投递 `uuid`，由飞书服务端去重，而不是必然重复投递。
            self._sender.send_text(
                chat_id=self._chat_id, text=content.text, dedupe_key=today.isoformat()
            )
        except Exception as error:  # noqa: BLE001 - 发送失败不得带走同一轮的其他职责
            # 只记审计与异常类型：异常正文可能带上群 ID 或响应体。水位不置位，
            # 因此这一天**不算已发送**，下一轮会重试（`V-花名册-30`）。
            self._audit.record(
                "roster_audit.send_failed",
                report_date=today.isoformat(),
                content_key=content.key,
                content_version=content.version,
                error=type(error).__name__,
                **snapshot.audit_facts(),
            )
            logger.error("管理群审计日报发送失败，下一轮重试 error=%s", type(error).__name__)
            return report

        self._audit.record(
            "roster_audit.report_sent",
            report_date=today.isoformat(),
            content_key=content.key,
            content_version=content.version,
            examined=report.examined,
            entries=len(report.entries),
            handover=report.handover_count,
            removed=report.removed_count,
            ambiguous=report.ambiguous_count,
            **snapshot.audit_facts(),
        )
        self._completed_on = today
        # 摘要只有计数，依据是 `V-花名册-33`（审计与日志不含花名册字段值）。日报正文的
        # 展示口径已被 2026-08-08 的 D2 裁定放宽到「受控管理群可含原值」，日志侧没有
        # 随之放宽：日志流向排障、CI 输出与工单，不在受控管理群那个范围里。
        logger.info(
            "管理群审计日报已发送 已开通用户=%s 条目=%s 疑似转交=%s 花名册查无=%s "
            "快照可用=%s 快照超龄=%s",
            report.examined,
            len(report.entries),
            report.handover_count,
            report.removed_count,
            snapshot.available,
            snapshot.stale,
        )
        return report


class SchedulerLoop:
    """按同一周期驱动多个定时职责，并把它们的失败互相隔离。

    ``build_loop`` 此前直接返回单个 :class:`CredentialRotationLoop`，"进程只有一个
    职责"这件事被硬编码在装配里。加入第二个职责必须改的正是这里：需要一个能容纳
    职责集合、并且**逐职责**捕获异常的结构。
    """

    def __init__(
        self,
        *,
        duties: Sequence[Any],
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        stop: threading.Event | None = None,
        heartbeat: Callable[[], None] | None = None,
    ) -> None:
        if not duties:
            raise ValueError("定时职责进程至少要有一个职责")
        self._duties = tuple(duties)
        self._interval_seconds = interval_seconds
        self._stop = threading.Event() if stop is None else stop
        self._heartbeat = heartbeat

    @property
    def duties(self) -> tuple[Any, ...]:
        return self._duties

    @property
    def stop_event(self) -> threading.Event:
        return self._stop

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        self._stop.set()

    def run_once(self) -> tuple[Any, ...]:
        """依次跑一遍每个职责。任何一个职责抛异常都不影响其余职责本轮执行。"""

        reports: list[Any] = []
        if self._heartbeat is not None:
            try:
                self._heartbeat()
            except Exception as error:  # noqa: BLE001 - 心跳失败不能跳过定时职责
                logger.error("scheduler 心跳记录失败，职责继续运行 error=%s", type(error).__name__)
        for duty in self._duties:
            if self._stop.is_set():
                # 已经在停止中：不再让后面的职责领取新工作（断言 V-保留-17）。
                reports.append(None)
                continue
            try:
                reports.append(duty.run_once())
            except Exception as error:  # noqa: BLE001 - 一个职责失败不能带走另一个
                # 只记异常类型，不记异常正文：正文可能带上被处理对象的内容。
                logger.error(
                    "定时职责本轮异常，其余职责与下一轮不受影响 duty=%s error=%s",
                    getattr(duty, "name", type(duty).__name__),
                    type(error).__name__,
                )
                reports.append(None)
        return tuple(reports)

    def run_forever(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            if self._stop.is_set():
                break
            self._stop.wait(self._interval_seconds)
        logger.info("定时职责已停止领取并退出")


def _is_definite_failure(error: BaseException) -> bool:
    """区分"飞书明确拒绝"与"结果不明确"。

    两者的处置**相同**（都撤销），区分只为了让日志与后续审计能分辨这两件事。
    分类是协议细节，由 adapters 层以 ``definite`` 属性给出（代码框架第二节：
    协议细节不进 apps 层）；没有该属性的异常一律视为"结果不明确"。
    """

    definite = getattr(error, "definite", None)
    return definite is True


class _Stoppable(Protocol):
    def request_stop(self) -> None: ...


def install_signal_handlers(loop: _Stoppable) -> None:
    """把 ``SIGTERM`` / ``SIGINT`` 接到"停止领取"上。

    处理函数只设一个事件标志，不做任何 I/O：信号处理函数里写库或发网络请求会在
    退出路径上引入新的失败模式，而这条路径恰恰是最不该出错的地方。
    """

    def handle(signal_number: int, _frame: Any) -> None:
        logger.info("收到信号，停止领取新的到期凭据 signal=%s", signal_number)
        loop.request_stop()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)


def _log_snapshot_alert(decision: SnapshotDecision) -> None:
    """快照保旧时的告警出口：一条只含分类与错误码的结构化警告。

    **面向管理员的那一份提醒走每日日报**（`V-花名册-47` 的裁定原文是「按日报告警提醒、
    不自动删」），这里补的是运维侧那一半——日报一天只发一次，而运维需要在当轮就看到
    "今天的花名册读取失败了"。刻意不接 ``core/alerting.py`` 的状态机：它只认心跳、
    任务滞留与发送连续失败三类信号，把一件每日节奏的数据新鲜度事实塞进去，会让阈值、
    去重与恢复计时三套语义同时失真。

    只记分类与错误码，不记任何行内容（`V-花名册-33`）。
    """

    logger.warning(
        "花名册本轮读取未成功，保留上一份快照 status=%s alert=%s failure_code=%s 上一份行数=%s",
        decision.status,
        decision.alert.value if decision.alert is not None else None,
        decision.failure_code,
        decision.previous_row_count,
    )


def _build_roster_audit_duty(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
    roster_access_token: Callable[[], str] | None,
    on_send_outcome: Callable[[str, bool], None] | None = None,
) -> RosterAuditDuty | None:
    """装配审计日报职责；前置不齐就**不注册**并留下**恰一条**审计，返回 ``None``。

    四个前置按固定次序检查，缺第一个就返回：`V-花名册-29` 要求「缺群 ID → 审计
    **恰 1 条**」，逐条报会让一个什么都没配的部署一次刷出四条审计，反而看不出该先配哪个。
    """

    if not config.admin_group_chat_id:
        # 只报变量名，不回显任何值（`V-花名册-29`）。
        audit.record(
            "roster_audit.duty_not_registered",
            reason="missing_environment_variable",
            variable="LINGXI_ADMIN_GROUP_CHAT_ID",
        )
        logger.warning(
            "未配置 LINGXI_ADMIN_GROUP_CHAT_ID，花名册审计日报职责不注册；其余定时职责照常运行"
        )
        return None
    for variable, value in (
        ("LINGXI_ROSTER_BITABLE_APP_TOKEN", config.roster_app_token),
        ("LINGXI_ROSTER_BITABLE_TABLE_ID", config.roster_table_id),
    ):
        if not value:
            audit.record(
                "roster_audit.duty_not_registered",
                reason="missing_environment_variable",
                variable=variable,
            )
            logger.warning("未配置 %s，花名册审计日报职责不注册；其余定时职责照常运行", variable)
            return None
    if roster_access_token is None:
        # 调用方没有交出任何令牌供给。**这条分支现在的含义是"真的没有供给"**，
        # 不再是"这条链还没接线"：Issue #215 之后 `build_loop` 总会建出一个供给
        # （凭据轮换职责按需换、进程内持有者转交），因此正式装配路径不会走到这里。
        #
        # 「配了但拿不到令牌」不走这条分支——那时职责照常注册，失败发生在运行期并按
        # 分类审计（`roster_access_token.unavailable`）。两者必须可分辨：把运行期的
        # 授权失败记成「未注册」，会让排障去找配置；反过来则会让「还没接线」看起来
        # 像「接线了但一直失败」（R3 的原始教训）。
        audit.record("roster_audit.duty_not_registered", reason="missing_access_token_supply")
        logger.warning("调用方未提供花名册读取令牌供给，花名册审计日报职责不注册")
        return None

    from lingxi.adapters.feishu_group_message import FeishuGroupMessages
    from lingxi.adapters.feishu_roster_bitable import BitableRosterPages, read_roster_snapshot
    from lingxi.adapters.postgres_roster_audit import PostgresRosterBaselineReader
    from lingxi.adapters.postgres_roster_snapshot import PostgresRosterSnapshotStore

    pages = BitableRosterPages(
        base_url=config.feishu_base_url,
        app_token=config.roster_app_token,
        table_id=config.roster_table_id,
        access_token=roster_access_token,
    )
    store = PostgresRosterSnapshotStore(config.postgres_dsn, timeouts=config.postgres_timeouts)
    roster_source = DailyRosterSource(
        # 走 `read_roster_snapshot` 而不是逐页归一：日报必须能区分「花名册真的空了」
        # 与「这一轮没读完」，而只有前者会让保旧判定拿到 `EMPTY_SOURCE`。
        read_round=lambda: read_roster_snapshot(pages),
        updater=RosterSnapshotUpdater(store=store, audit=audit, on_alert=_log_snapshot_alert),
        load_snapshot=store.load,
        stale_after=config.roster_snapshot_stale_after,
    )

    return RosterAuditDuty(
        baseline_reader=PostgresRosterBaselineReader(
            config.postgres_dsn, timeouts=config.postgres_timeouts
        ),
        roster_source=roster_source,
        sender=FeishuGroupMessages(
            base_url=config.feishu_base_url,
            app_id=config.feishu_app_id,
            app_secret=config.feishu_app_secret,
            on_send_outcome=on_send_outcome,
        ),
        audit=audit,
        chat_id=config.admin_group_chat_id,
        stop=stop,
    )


def _build_permission_refresh_duty(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
) -> PermissionRefreshDuty | None:
    """装配每日权限重算职责；前置不齐就**不注册**并留下**恰一条**审计，返回 ``None``。

    形状照 :func:`_build_roster_audit_duty`（`V-花名册-29` 的同一条纪律：缺项只报变量名、
    审计恰一条、其余职责照常运行）。前置有三个，逐个说明为什么它是真前置：

    1. **MCP 令牌主密钥**（``LINGXI_MCP_TOKEN_ENCRYPT_KEY``）。重算要读该用户**已有**的
       令牌密文，而唯一的读取口
       :class:`~lingxi.adapters.postgres_mcp_token.PostgresMcpTokenStore` 只接受已经校验
       过主密钥的加解密对象（它同时承载解密路径，构造时就要求密钥）。**没有它就没有令牌
       读取口**，而"读不到"与"这个人没有令牌"在下游是同一个 ``None``——那会让每个需要
       新建发布行的人都以 ``missing_token_cipher`` 失败关闭，表现成"接线了但一直失败"，
       正是 R3 那条注释要避免的伪装。因此这里显式不注册并留痕。

       **本职责一次都不解密、也不签发**：密钥在这里只用于构造那个读取口
       （见 :mod:`lingxi.apps.scheduler.permission_refresh` 的模块文档）。
    2. **角色职能映射配置**。它随包发布（``lingxi/config/galaxy_role_function_map.toml``）。
       读不出来时**不能**退化成空映射——那会让所有角色变成"未映射"，于是全员被算成无可用
       权限，是一种看起来正常的失败（``role_function_map_file`` 的模块文档同一条理由）。
    3. **公司+职能→指标名翻译映射配置**（Issue #227）。它同样随包发布
       （``lingxi/config/company_function_metric_map.toml``），**文件读不出来或格式不对**
       才不注册——**空映射本身是合法内容**（``[companies]`` 表存在但没有条目，代表映射
       内容尚未由产品负责人填入），不是一种要拒绝注册的前置缺失：职责本该正常跑起来，
       只是每个人都会在翻译那一步 fail-closed 并跳过（模块文档「翻译」一节），这与"配置
       文件本身损坏"是两件不同的事，必须分开判断——前者是"内容还没到"，后者是"部署配置
       本身有问题"，把两者混在一起会让"运维发现配置文件语法错了"和"产品负责人还没填映射"
       表现成同一种"职责不注册"，无从分辨该找谁。

    数据库连接串是必需配置，进程起得来就一定有，因此它不构成一个能变红的前置判定；
    职责真正的运行前置（花名册今天更新过、银河有当前有效批次）是**数据**而不是配置，
    由 ``run_once`` 每轮重新判定。
    """

    if not config.mcp_token_encrypt_key:
        from lingxi.adapters.mcp_token_cipher import MASTER_KEY_ENV

        # 只报变量名，不回显任何值（`V-花名册-29` 的同一条纪律；它还是一把主密钥）。
        audit.record(
            "permission_refresh.duty_not_registered",
            reason="missing_environment_variable",
            variable=MASTER_KEY_ENV,
        )
        logger.warning(
            "未配置 %s，每日权限重算职责不注册；其余定时职责照常运行", MASTER_KEY_ENV
        )
        return None

    from lingxi.adapters.mcp_token_cipher import McpTokenCipher
    from lingxi.adapters.postgres_galaxy_snapshot import PostgresGalaxySnapshotReader
    from lingxi.adapters.postgres_mcp_token import PostgresMcpTokenStore
    from lingxi.adapters.postgres_permission_publish import PostgresPermissionPublishStore
    from lingxi.adapters.postgres_roster_audit import PostgresRosterBaselineReader
    from lingxi.adapters.postgres_roster_snapshot import PostgresRosterSnapshotStore
    from lingxi.adapters.company_function_metric_map_file import (
        load_company_function_metric_map,
    )
    from lingxi.adapters.role_function_map_file import load_role_function_map

    try:
        role_function_map = load_role_function_map()
    except (OSError, ValueError) as error:
        # 只记异常类型：配置解析失败的正文可能带上文件内容片段。
        audit.record(
            "permission_refresh.duty_not_registered",
            reason="role_function_map_unavailable",
            error=type(error).__name__,
        )
        logger.error(
            "角色职能映射配置不可用，每日权限重算职责不注册 error=%s", type(error).__name__
        )
        return None

    try:
        metric_translation_map = load_company_function_metric_map()
    except (OSError, ValueError) as error:
        # 同上：只记异常类型。**空映射不会走到这里**——它是合法内容，解析成功即返回；
        # 这里挡的是文件缺失或格式不对，二者都是部署配置问题，不是"内容还没填"。
        audit.record(
            "permission_refresh.duty_not_registered",
            reason="metric_translation_map_unavailable",
            error=type(error).__name__,
        )
        logger.error(
            "公司+职能→指标名翻译映射配置不可用，每日权限重算职责不注册 error=%s",
            type(error).__name__,
        )
        return None

    publish_store = PostgresPermissionPublishStore(
        config.postgres_dsn, timeouts=config.postgres_timeouts
    )
    return PermissionRefreshDuty(
        baseline_reader=PostgresRosterBaselineReader(
            config.postgres_dsn, timeouts=config.postgres_timeouts
        ),
        roster_snapshot=PostgresRosterSnapshotStore(
            config.postgres_dsn, timeouts=config.postgres_timeouts
        ),
        galaxy=PostgresGalaxySnapshotReader(config.postgres_dsn, timeouts=config.postgres_timeouts),
        decisions=publish_store,
        # 同一个存储对象喂两个端口：一个只写权限决定，一个只读"发布过没有"。分成两个
        # 参数是为了让撤权那条判据在类型上说得清楚（见 permission_refresh 的两个协议）。
        publish_history=publish_store,
        token_ciphers=PostgresMcpTokenStore(
            config.postgres_dsn,
            cipher=McpTokenCipher(config.mcp_token_encrypt_key),
            timeouts=config.postgres_timeouts,
        ),
        role_function_map=role_function_map,
        metric_translation_map=metric_translation_map,
        audit=audit,
        stop=stop,
    )


def _build_permission_retention_duty(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
) -> PermissionRetentionSweepDuty:
    """装配权限链到期清理职责。**它总是注册**，只有判定记录那一面按前置条件装配。

    **``publish_outbox`` 那一面没有任何配置前置**：擦 ``payload`` 只需要数据库连接串，
    而连接串是必需配置、进程起得来就一定有。这一面恰恰是带个人数据的那一面（邮箱与
    姓名），因此它必须无条件跑起来——给一条纯粹是"擦自己库里的内容"的路径加一个能让它
    不注册的开关，等于给保留上界加了一个可以被关掉的旁路。

    **``mcp_sync_check`` 那一面的前置是 MCP 令牌主密钥**（``LINGXI_MCP_TOKEN_ENCRYPT_KEY``）：
    唯一的读写口 :class:`~lingxi.adapters.postgres_mcp_token.PostgresMcpTokenStore` 构造时
    就要求一个**已经校验过主密钥**的加解密对象（它同时承载解密路径）。删过期行本身用不到
    密钥，但绕过那个构造约束就得给这个调用点单开一个不校验密钥的口子——那正是该类刻意
    拒绝的事（它对非 :class:`McpTokenCipher` 直接抛 ``TypeError``）。缺密钥时这一面**不装配**
    并留下**恰一条**审计，形状照 :func:`_build_readiness_follow_up` 的探针那一面（缺项只报
    变量名、不回显任何值——它还是一把主密钥），发布 outbox 那一面照常。

    这条取舍的产品后果是可接受的、也已写明：``mcp_sync_check`` **没有可识别内容列**，
    到期不删的后果是一张只含内部 ULID 与结论码的表继续变长；而真正含邮箱与姓名的
    ``publish_outbox.payload`` 一轮都不会少擦。
    """

    from lingxi.adapters.postgres_permission_publish import PostgresPermissionPublishStore

    checks: Any = None
    if config.mcp_token_encrypt_key:
        from lingxi.adapters.mcp_token_cipher import McpTokenCipher
        from lingxi.adapters.postgres_mcp_token import PostgresMcpTokenStore

        checks = PostgresMcpTokenStore(
            config.postgres_dsn,
            cipher=McpTokenCipher(config.mcp_token_encrypt_key),
            timeouts=config.postgres_timeouts,
        )
    else:
        from lingxi.adapters.mcp_token_cipher import MASTER_KEY_ENV

        # **恰一条**审计：只关掉判定记录那一面，发布 outbox 的内容擦除照常。
        audit.record(
            "permission_retention.checks_not_wired",
            reason="missing_environment_variable",
            variable=MASTER_KEY_ENV,
        )
        logger.warning(
            "未配置 %s，mcp_sync_check 的到期删除不装配；publish_outbox 的到期内容擦除照常运行",
            MASTER_KEY_ENV,
        )

    return PermissionRetentionSweepDuty(
        outbox=PostgresPermissionPublishStore(
            config.postgres_dsn, timeouts=config.postgres_timeouts
        ),
        checks=checks,
        audit=audit,
        stop=stop,
    )


def _build_readiness_follow_up(
    config: SchedulerConfig,
    *,
    audit: AuditSink,
    stop: threading.Event,
) -> ReadinessFollowUp | None:
    """装配「就绪确认 + 变化通知」这一面；前置不齐就**不装配**并留下**恰一条**审计。

    **两个前置各自只关掉自己那一块**（二级审查 N6）：

    1. **MCP 令牌主密钥**（``LINGXI_MCP_TOKEN_ENCRYPT_KEY``）——**整面的前置**。它不只是
       解密令牌用：就绪判定记录（``mcp_sync_check``）的读写口在同一个存储类上，而"这条
       变化通知过了"这个**唯一水位**就落在那张表里。没有它，连撤权通知都没有"只发一次"
       的载体，只能整面不装配。
    2. **问数 MCP 端点**（``LINGXI_QUERY_MCP_ENDPOINT``）——**只关掉探针**。撤权通知不
       依赖探针（权限文本为空的那一路本来就不发探针），因此端点没配时这一面照常装配，
       只是把 ``probe=None`` 交给 :class:`ReadinessTicker`：需要探针的那一路本轮不推进、
       不落任何记录，端点配好后从库里的进度原样继续。装一个指向空地址的假探针则相反，
       会让每条确认以技术失败耗满预算再转运维——把"还没接线"伪装成"接线了但一直失败"。

    **探针超时与就绪节奏用同一个数**：``ReadinessSchedule(probe_timeout_seconds=…)`` 与
    ``QueryMcpProbe(timeout_seconds=…)`` 都取 ``config.query_mcp_timeout_seconds``。
    两边不一致时，就绪那一侧算出来的"结论最晚什么时候落地"就是假的，因此这里在装配后
    立刻断言相等——装配层的错配不该等到生产才暴露。
    """

    if not config.mcp_token_encrypt_key:
        from lingxi.adapters.mcp_token_cipher import MASTER_KEY_ENV

        # 只报变量名，不回显任何值（`V-花名册-29` 的同一条纪律；它还是一把主密钥）。
        audit.record(
            "permission_readiness.not_wired",
            reason="missing_environment_variable",
            variable=MASTER_KEY_ENV,
        )
        logger.warning(
            "未配置 %s，MCP 就绪确认与权限变化通知不装配；权限发布照常运行", MASTER_KEY_ENV
        )
        return None

    from lingxi.adapters.feishu_user_message import FeishuUserMessages
    from lingxi.adapters.mcp_token_cipher import McpTokenCipher
    from lingxi.adapters.postgres_mcp_token import PostgresMcpTokenStore, token_cipher_provider
    from lingxi.adapters.query_mcp_probe import QueryMcpProbe
    from lingxi.core.permission.mcp_readiness import ReadinessTicker
    from lingxi.core.permission.notification import PermissionNoticeDispatcher

    tokens = PostgresMcpTokenStore(
        config.postgres_dsn,
        cipher=McpTokenCipher(config.mcp_token_encrypt_key),
        timeouts=config.postgres_timeouts,
    )
    schedule = ReadinessSchedule(probe_timeout_seconds=config.query_mcp_timeout_seconds)
    probe = None
    if config.query_mcp_endpoint:
        probe = QueryMcpProbe(
            endpoint=config.query_mcp_endpoint,
            token_provider=token_cipher_provider(tokens),
            timeout_seconds=config.query_mcp_timeout_seconds,
        )
        if probe.timeout_seconds != schedule.probe_timeout_seconds:  # pragma: no cover - 装配自证
            raise RuntimeError(
                "探针传输超时必须与就绪节奏的单次超时一致，否则收口上界是假的"
            )
    else:
        # **恰一条**审计：只关掉探针，撤权通知照常。
        audit.record(
            "permission_readiness.probe_not_wired",
            reason="missing_environment_variable",
            variable="LINGXI_QUERY_MCP_ENDPOINT",
        )
        logger.warning(
            "未配置 LINGXI_QUERY_MCP_ENDPOINT，MCP 就绪探针不装配；"
            "撤权通知与权限发布照常运行，已发布的授权待端点配好后继续确认"
        )
    return ReadinessFollowUp(
        ticker=ReadinessTicker(
            probe=probe,
            store=tokens,
            audit=audit,
            clock=lambda: datetime.now(timezone.utc),
            schedule=schedule,
        ),
        checks=tokens,
        notices=PermissionNoticeDispatcher(
            sender=FeishuUserMessages(
                base_url=config.feishu_base_url,
                app_id=config.feishu_app_id,
                app_secret=config.feishu_app_secret,
            ),
            audit=audit,
            # 退避用 `stop.wait` 而不是 `time.sleep`：SIGTERM 能立刻打断它
            # （同 `CredentialRotationLoop._save_with_retry`）。
            sleep=stop.wait,
        ),
    )


def _build_permission_publish_duty(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
    permission_table_access_token: Callable[[], str] | None,
) -> PermissionPublishDuty | None:
    """装配权限发布消费职责；**三个面各按自身依赖装配，缺谁只停谁**（二级审查 N6）。

    形状照 :func:`_build_roster_audit_duty`（`V-花名册-29` 的同一条纪律：缺项只报变量名、
    每一面**恰一条**审计、其余职责照常运行）。

    **发布面**的三个前置：

    1. **权限发布 Base ``app_token``** 与 2. **表 ``table_id``**：写哪张表不能进代码，
       只从环境变量来。
    3. **发布表读写所用的短期令牌供给**。花名册那条已由 Issue
       [#215](https://github.com/Moshuiwang/lingxi/issues/215) 接上（凭据轮换职责按需派生、
       进程内持有者转交），**这条还没有**：默认 ``None``，仍登记为 R3。理由没变——换取
       令牌要消费专用授权主体那条**一次性** ``refresh_token``，而同一进程里的凭据轮换
       职责是它唯一的合法消费者，两个消费者共用一条一次性令牌就是 2026-08-08 授权码被烧
       那次事故的形状；而发布表在**另一个 Base** 上，是否由同一个授权主体读写还是一次
       没做的产品决定。因此这里只留注入点，**不在这里现造一个**。

    **缺发布面前置时职责仍然注册**，只要就绪/通知那一面装得起来：已经发布出去的那些
    权限还等着被确认、被通知，没有理由因为"暂时写不了新的一行"就把它们一起停掉。
    反过来也一样——MCP 端点没配时发布照常。两个面都装不起来才不注册。
    """

    executor = None
    unwired: tuple[str, str] | None = None
    for variable, value in (
        ("LINGXI_PERMISSION_BITABLE_APP_TOKEN", config.permission_app_token),
        ("LINGXI_PERMISSION_BITABLE_TABLE_ID", config.permission_table_id),
    ):
        if not value:
            unwired = ("missing_environment_variable", variable)
            break
    if unwired is None and permission_table_access_token is None:
        unwired = ("permission_table_access_token_unwired", "")

    from lingxi.adapters.postgres_permission_publish import PostgresPermissionPublishStore

    store = PostgresPermissionPublishStore(
        config.postgres_dsn, timeouts=config.postgres_timeouts
    )
    if unwired is None:
        from lingxi.adapters.feishu_permission_bitable import BitablePermissionTable
        from lingxi.core.permission.publish import PermissionPublishExecutor

        executor = PermissionPublishExecutor(
            store=store,
            transport=BitablePermissionTable(
                base_url=config.feishu_base_url,
                app_token=config.permission_app_token,
                table_id=config.permission_table_id,
                access_token=permission_table_access_token,
            ),
            audit=audit,
        )

    readiness = _build_readiness_follow_up(config, audit=audit, stop=stop)
    if executor is None and readiness is None:
        # 两面都装不起来：注册一个什么都做不了的职责只会每分钟记一条空报告。
        reason, variable = unwired or ("unknown", "")
        facts = {"reason": reason}
        if variable:
            facts["variable"] = variable
        audit.record("permission_publish.duty_not_registered", **facts)
        logger.warning("权限发布与就绪确认两面都未装配，职责不注册；其余定时职责照常运行")
        return None
    if executor is None:
        reason, variable = unwired or ("unknown", "")
        facts = {"reason": reason}
        if variable:
            facts["variable"] = variable
        # **恰一条**：只说发布面没装配，就绪与通知面照常。
        audit.record("permission_publish.publish_not_wired", **facts)
        logger.warning(
            "权限发布面未装配（%s），已发布权限的就绪确认与变化通知照常运行", reason
        )

    return PermissionPublishDuty(
        executor=executor,
        intents=store,
        audit=audit,
        readiness=readiness,
        on_alert=_permission_readiness_alert(audit),
        stop=stop,
    )


def _permission_readiness_alert(audit: AuditSink) -> Callable[[str, str], None]:
    """刷新链就绪超时的告警出口：一条**可告警的结构化事实**。

    **刻意不接 ``core/alerting.py`` 的状态机**（与 ``core/permission/publish.py`` 的
    ``on_alert`` 同一条已登记理由，也与 ``_log_snapshot_alert`` 同一姿态）：那套状态机
    只认心跳、任务滞留与飞书发送连续失败三类信号，把"某个用户的权限同步没能在十五分钟
    内确认"塞进其中任何一类，都会让那一类的阈值、去重与恢复计时同时失真——尤其是塞进
    ``FEISHU_SEND_FAILED``，会让真正的发送故障被权限超时淹没。

    它属于"权限发布失败是新增一类信号还是复用一类"这个尚未做出的决定；在那之前，这里
    先把事实**留成可 grep、可进工单的一条审计 + 一条 WARNING**，而不是让刷新链的超时
    只剩计数。用户标识是内部 ULID，不含人员资料。
    """

    def report(kind: str, user_id: str) -> None:
        audit.record("permission_readiness.alert", kind=kind, user=user_id)
        logger.warning("权限就绪确认需要人工关注 kind=%s user=%s", kind, user_id)

    return report


def build_loop(
    config: SchedulerConfig,
    *,
    roster_access_token: Callable[[], str] | None = None,
    permission_table_access_token: Callable[[], str] | None = None,
    audit: AuditSink | None = None,
    alerting_duty: AlertingDuty | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> SchedulerLoop:
    """装配进程的全部定时职责。

    职责顺序有意为之：凭据轮换在前。它处理的是一次性有效、有硬期限的凭据，
    而三类清理晚一轮都没有任何后果——到期行/空闲会话/到期内容快照下一轮照清，到期时间
    不会因为清理迟到而后移（断言 V-保留-16），空闲会话清理本身也是幂等的。审计日报与每日
    权限重算排在三类清理之后：它们一天只做一次事，晚一轮毫无影响；而这两个之间的先后
    **不是随意的**——权限重算要用今天那份花名册快照，而换快照的是审计日报那一轮
    （`V-权限-07`）。告警职责注册在**最后**（基线既有形状）：它汇总本轮观察到的信号，
    排在被观察者后面才看得到这一轮的事实。

    ``roster_access_token`` 是花名册读取所用的**短期令牌供给**（返回已就绪令牌的可调用
    对象）。**默认不再是 ``None``**：Issue #215 主接线之后，这里建出一条进程内供给——
    凭据轮换职责按需消费一次 ``refresh_token``、把派生短期令牌放进进程内持有者，日报侧
    经 :class:`~lingxi.core.identity.access_token_supply.RosterAccessTokenProvider`
    按新鲜度取用。**唯一消费者这条边界没有变**：日报自己不碰 ``refresh_token``，两个
    消费者共用一条一次性令牌正是 2026-08-08 授权码被烧那次事故的形状。

    参数保留是为了让调用方（主要是测试）**替换**供给；``None`` 现在的含义是"用装配好的
    那条"，而不是"没有供给"。进程内持有者与频率上界都建在这里，**每个进程一份**：它们是
    "这个进程今天换过没有"的账本，跨进程共享会让上界失去意义；而重启也抹不掉的那道上界
    在凭据文件里（``refresh_consumed_at``）。

    ``permission_table_access_token`` 是**权限发布表**读写所用的短期令牌供给，它**没有随
    #215 一起接上**：默认仍是 ``None``，因此当前部署下权限发布面不注册（仍登记为 R3），
    但发布 Base 与表标识、Outbox 消费、就绪确认与通知都已按配置装配。两条各留各的注入点
    而不是共用一个参数：两张表在不同的 Base 上，将来完全可能由不同的授权主体读写，把它们
    并成一个参数等于提前替那个决定做主——也正因为如此，花名册那条接上了不代表这条自动
    就有，它要么复用同一个授权主体（那是一次还没做的产品决定），要么另有自己的主体。
    """

    from lingxi.adapters.delegated_credentials import HostFileDelegatedCredentialVault
    from lingxi.adapters.feishu_directory import FeishuAuthorizationClient
    from lingxi.adapters.postgres_conversation import PostgresTaskQueue
    from lingxi.adapters.retention import RETENTION_CLEANUP_TIMEOUTS, PostgresRetentionCleaner

    # 一个停止标志贯穿所有职责：SIGTERM 只设它一次，全部职责同时停止领取新工作。
    stop = threading.Event()
    sink = audit if audit is not None else StructuredLogAuditSink()

    # 派生短期令牌的进程内持有者：不落盘、不进日志与审计，重启即空。
    holder = DerivedAccessTokenHolder()
    rotation = CredentialRotationLoop(
        vault=HostFileDelegatedCredentialVault(
            config.postgres_dsn,
            config.credential_key,
            config.credential_path,
            timeouts=config.postgres_timeouts,
        ),
        authorization=FeishuAuthorizationClient(
            base_url=config.feishu_base_url,
            app_id=config.feishu_app_id,
            app_secret=config.feishu_app_secret,
        ),
        interval_seconds=config.interval_seconds,
        stop=stop,
        holder=holder,
    )
    cleanup = RetentionCleanupDuty(
        # 清理函数内部两张表各有 2s lock_timeout，不能沿用 scheduler 通用的 3s
        # statement_timeout；适配器专用覆盖要大于 2×2s 累计并留出删批余量。
        cleaner=PostgresRetentionCleaner(config.postgres_dsn, timeouts=RETENTION_CLEANUP_TIMEOUTS),
        stop=stop,
    )
    idle_sweep = IdleConversationSweepDuty(
        queue=PostgresTaskQueue(config.postgres_dsn, timeouts=config.postgres_timeouts),
        idle_after=IDLE_CONVERSATION_SWEEP_AFTER,
        stop=stop,
    )
    # 权限链到期清理与上面两类清理同组，排在它们之后、发布消费之前。位置的唯一含义是
    # 同一轮内的先后：一条 payload 已经过期的意图应当先被擦成 ``'{}'``，再轮到发布面
    # 认领它并以 ``invalid`` 失败关闭——反过来会让一份九十天前决定的权限在到期当轮还被
    # 写进外部表格一次。晚一轮清理没有任何后果（到期时间不因清理迟到而后移，断言
    # ``V-保留-16``）。
    permission_retention = _build_permission_retention_duty(config, stop=stop, audit=sink)

    duties: list[Any] = [rotation, cleanup, idle_sweep, permission_retention]
    # 令牌供给：日报侧要令牌 → 持有者里有新鲜的就直接给，没有就让**凭据轮换职责**
    # 按需换一次（每 UTC 日至多一次）。日报自己不碰一次性 refresh_token。
    supply = (
        roster_access_token
        if roster_access_token is not None
        else RosterAccessTokenProvider(
            holder=holder,
            refresh=rotation.refresh_for_supply,
            audit=sink,
        )
    )
    roster_audit = _build_roster_audit_duty(
        config,
        stop=stop,
        audit=sink,
        roster_access_token=supply,
        on_send_outcome=(alerting_duty.send_outcome_callback() if alerting_duty else None),
    )
    if roster_audit is not None:
        duties.append(roster_audit)
    # 每日权限重算排在花名册审计**之后**：同一轮里花名册快照先被换成今天的那一份，
    # 重算才可能通过它自己的新鲜度判据（`V-权限-07` 的「先花名册、再银河」）。
    # 位置只保证同一轮内的先后；"用的是今天的花名册"由职责自己的判据保证。
    permission_refresh = _build_permission_refresh_duty(config, stop=stop, audit=sink)
    if permission_refresh is not None:
        duties.append(permission_refresh)
    # 发布消费排在每日重算**之后**：同一轮里重算先把当天的意图排进来，发布紧接着就能把
    # 它推出去，而不是白等一个调度周期。位置只保证同一轮内的先后；一条意图晚一轮被消费
    # 没有任何产品后果（outbox 本来就是异步的）。
    permission_publish = _build_permission_publish_duty(
        config,
        stop=stop,
        audit=sink,
        permission_table_access_token=permission_table_access_token,
    )
    if permission_publish is not None:
        duties.append(permission_publish)
    if alerting_duty is not None:
        duties.append(alerting_duty)
        if heartbeat is None:
            heartbeat = alerting_duty.heartbeat_callback("scheduler")

    return SchedulerLoop(
        duties=tuple(duties),
        interval_seconds=config.interval_seconds,
        stop=stop,
        heartbeat=heartbeat,
    )


class _LogOnlyAlertSender:
    """未配置管理群时的告警出口：只记结构化日志，不发起网络请求。

    与 ``apps/gateway/__init__.py``、``apps/worker/cli.py`` 的同名类同一姿态——
    没有配置目标群不等于告警关闭（Issue #153：合同要求"告警不可用时主流程行为
    有明确定义"，这里的定义是"状态机照常运行，只是暂时没有群通知"）。
    """

    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None:
        del chat_id, dedupe_key
        logging.getLogger("lingxi.apps.scheduler.alert").warning(text)


def build_alerting_duty(config: SchedulerConfig, *, audit: AuditSink | None = None) -> AlertingDuty:
    """装配 scheduler 自己的告警状态机（Issue #153）。

    此前 ``AlertingDuty`` 虽已建成（Issue #92），但 ``main()`` 从未真的实例化它
    ——当前能力已经登记这个缺口："三进程 main() 均未装配告警"。这是补上 scheduler
    这一份的地方；gateway 与 worker 各自在自己的 ``apps/<name>`` 里装配同型的一份，
    三者互不共享状态（各自是独立部署单元）。
    """

    if config.admin_group_chat_id:
        from lingxi.adapters.feishu_group_message import FeishuGroupMessages

        sender: Any = FeishuGroupMessages(
            base_url=config.feishu_base_url,
            app_id=config.feishu_app_id,
            app_secret=config.feishu_app_secret,
        )
        chat_id = config.admin_group_chat_id
    else:
        sender = _LogOnlyAlertSender()
        chat_id = "scheduler-log-only"

    return AlertingDuty(
        manager=AlertManager(policy=config.alert_policy),
        dispatcher=AlertDispatcher(
            sender=sender, chat_id=chat_id, policy=config.alert_policy, audit=audit
        ),
        audit=audit,
    )


def _combined_heartbeat(alerting_duty: AlertingDuty, liveness_role: str) -> Callable[[], None]:
    """把"记进 AlertManager"与"戳一下活性文件"合成一个心跳回调（Issue #153）。

    与 ``apps/gateway/__init__.py`` 的同名函数同一形状：``AlertManager`` 供跨进程
    重启也能观察到的阈值/去重状态机，活性文件供同容器内的
    ``python -m lingxi.apps.healthcheck`` 判断主循环是否还在跳动。
    """

    from lingxi.apps.liveness import touch_liveness

    beat = alerting_duty.heartbeat_callback(liveness_role)

    def combined() -> None:
        beat()
        touch_liveness(liveness_role)

    return combined


def main(argv: list[str] | None = None) -> int:
    # 日志只到 stdout / stderr，不写文件、不自行轮转（断言 V-部署-04）。
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        config = SchedulerConfig.from_env()
    except ValueError as error:
        print(f"lingxi-scheduler 启动失败：{error}", file=sys.stderr)
        return 2
    try:
        alerting_duty = build_alerting_duty(config, audit=StructuredLogAuditSink())
        loop = build_loop(
            config,
            alerting_duty=alerting_duty,
            heartbeat=_combined_heartbeat(alerting_duty, "scheduler"),
        )
    except (RuntimeError, ValueError) as error:
        print(f"lingxi-scheduler 启动失败：{error}", file=sys.stderr)
        return 2

    install_signal_handlers(loop)
    logger.info("lingxi-scheduler 已启动 interval_seconds=%s", config.interval_seconds)
    loop.run_forever()
    return 0
