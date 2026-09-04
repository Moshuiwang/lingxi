"""scheduler 进程的配置装配：环境变量 → :class:`SchedulerConfig`。

从 :mod:`lingxi.apps.scheduler`（#237 拆分）搬出——原模块头部docstring 与退出语义等
进程级说明仍在包的 ``__init__.py``，这里只留配置对象本身与它的校验规则。
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

#: 首次开通编排的默认执行线程数。**每条链最长阻塞十五分钟**（发布等待 + 就绪预算），
#: 因此它就是「同一时刻最多几个人在开通」。
#:
#: 取 8 的依据（710 人规模的排空账，全部按最坏侧算）：
#:
#: - 首期服务规模是 710 人的关联组织（组织快照实测值），而首次开通是**一次性事件**
#:   ——每个人一辈子只走一次，不是持续负载。真正要扛的只有"上线当天大量员工同时首聊"。
#: - 典型一条链是秒级到三分钟（就绪探针在 t=0 或 t=180s 命中），8 条线程 ≈ 每小时
#:   160–480 人，710 人在 1.5–4.5 小时内排空。
#: - 病态一条链是 17 分钟（发布等待 120s + 就绪预算 900s，即每一次探针都不成功），
#:   8 条线程 ≈ 每小时 28 人；这种情况下 710 人要排一天多——**但一条都不会丢**：
#:   没被认领的事件原样留在库里，认领量由执行器剩余容量压住，下一轮照捞。
#: - 病态情形本身也不该靠加线程解决：每条链都探满十五分钟意味着 MCP 侧根本没同步，
#:   多开线程只是让更多人同时等一个不会来的结果。
#:
#: 线程绝大多数时间阻塞在 ``sleep`` 与网络等待上，8 条线程对 scheduler 进程的常驻开销
#: 可以忽略；需要更快排空时调 ``LINGXI_ONBOARDING_WORKERS``（启动日志会把线程数与队列
#: 深度一起报出来，不必去猜当前上限是多少）。
#:
#: 上界 64 是防御而不是容量规划：撞上它说明配置写错了，而不是「这次要开通很多人」。
DEFAULT_ONBOARDING_WORKERS = 8
MAX_ONBOARDING_WORKERS = 64

#: 组织快照同步整轮预算的默认值（秒，Issue #284 A 组 #2）。只挂在组织快照专用的
#: ``FeishuDirectoryClient`` 实例上（见 ``apps/scheduler/assembly.py`` 的
#: ``_build_org_snapshot_sync_duty``），不影响开通链 employment reader 的令牌读取
#: 语义——两者是互不共享状态的独立 client 实例。
#:
#: **默认 1200 秒（20 分钟）的依据**，也是运维调整这个值时的参照系：
#:
#: - stage 实测一整轮全量遍历成功耗时约 345 秒，1200 秒留出约 3.5 倍余量，
#:   覆盖限频重试与网络抖动；
#: - 明显小于专用授权用户身份令牌约 2 小时的寿命，让一轮无论成功失败都倾向于在
#:   同一个令牌有效期内结束，不太会中途因令牌过期而变成一个更难诊断的错误；
#: - 撞预算后走既有的 ``READ_FAILURE_BACKOFF_STEP_SECONDS``（5 分钟起步、封顶 1
#:   小时）退避，20 分钟预算 + 退避在同一个 UTC 日内仍有多次自愈机会。
#:
#: **这是一个可运维调整的默认值，不是需要另行拍板的产品判断**：可以按
#: ``LINGXI_ORG_SNAPSHOT_ROUND_BUDGET_SECONDS`` 覆盖（见 :meth:`SchedulerConfig.from_env`）
#: ——例如关联组织规模显著增长、345 秒的基线不再成立时，运维可以直接调大这个数，
#: 不需要因为改一个运行参数而回到产品决策流程；调小同理，只要仍然满足上面两条
#: 力学关系（远大于实际一轮耗时、明显小于令牌寿命），配置本身不作强制校验。
DEFAULT_ORG_SNAPSHOT_ROUND_BUDGET_SECONDS = 1200.0

#: 组织快照整轮预算的下限（秒，独立审查二轮 P2-B4）。**60 秒不是把 stage 实测的
#: 345 秒基线硬编码成校验值**——组织规模变化时真实基线会变，这里只挡"明显不可能
#: 跑完一轮"的误配：任何真实一整轮全量遍历都不可能在一分钟内完成，配成低于这个数
#: 必然导致每一轮都撞 ``round_budget_exceeded``，而失败路径的语义是"保留上一份、
#: 不覆盖基线"——快照因此会**静默地**永远停在旧数据上（旧数据仍然摆在那，表面看
#: 起来"有数据"，不会像空表那样明显）。``OrgSnapshotSyncDuty`` 对连续撞线单独再加
#: 一层响亮告警（见 ``org_snapshot_sync.py`` 的
#: ``CONSECUTIVE_ROUND_BUDGET_EXCEEDED_ESCALATION_THRESHOLD``），这里的下限校验是
#: 第一道更早的防线：错配在进程启动时就快速失败，不必等到连续三轮撞线才发现。
MIN_ORG_SNAPSHOT_ROUND_BUDGET_SECONDS = 60.0


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
    # 存量令牌只读源的 Base 与表标识（Issue #281 载体，Trace #304 批次 3）。**可选，
    # 与上面这对同一姿态**：缺任一个，开通链的 adopt-or-issue 能力就不装配，直接走
    # 原签发路径——不是"这一格必须先配好首次开通才能跑"（`V-开通-24`）。生产环境里
    # 这两个坐标很可能与上面那对 permission_* 指向同一个 Base/表（同一份正式表），
    # 但刻意用独立的环境变量注入：这条只读能力可以单独打开/关闭，不与发布面的读写
    # 坐标绑在一起。
    stock_token_app_token: str | None = None
    stock_token_table_id: str | None = None
    # 问数 MCP 的就绪探针端点。**可选**：缺了只是**就绪与通知那一面**不装配，发布面照常
    # ——发布不依赖探针。配了但不是 https 则快速失败：误配 http 会让用户令牌明文上路。
    query_mcp_endpoint: str | None = None
    # 单次就绪探针的传输超时。它同时是 `ReadinessSchedule` 算「结论最晚什么时候落地」的
    # 输入，因此装配层必须让探针传输层与就绪节奏用**同一个数**（见
    # `lingxi.adapters.query_mcp_probe.QueryMcpProbe.timeout_seconds` 的文档）。
    query_mcp_timeout_seconds: int = DEFAULT_PROBE_TIMEOUT_SECONDS
    # 用户环境根目录（`/var/lib/lingxi/users`）。首次开通编排要在它下面建家目录并写
    # 按用户的 `.mcp.json`（产品负责人 2026-08-17 裁定）。**可选**：缺了只是首次开通
    # 编排不注册，其余职责照常。
    user_env_root: str | None = None
    # 开通编排的执行线程数。**每条链最长会阻塞十五分钟**，因此它就是"同一时刻最多几个人
    # 在开通"；认领量由执行器剩余容量压住（见 apps/scheduler/onboarding.py）。
    onboarding_workers: int = DEFAULT_ONBOARDING_WORKERS
    # 内测名单闸（Issue #302 S-N-01）：开通链最前端的白名单，只在 open_id 命中时才继续
    # 走身份定位；命中之外一律得到「内测未开放」且零建档（详见
    # `lingxi.core.identity.innertest_roster_gate` 模块文档）。**默认空集合＝全拒**，
    # 不是"未启用"——空集合本身就是失败关闭的全部实现，不需要另一个开关字段。
    # 配了但含无法识别条目会在 `from_env` 里快速失败（错配不是未配，同本文件其余
    # 标识类变量的既有纪律），不会静默退化成一份"看起来配了、其实放行了别人"的名单。
    # `field(repr=False)`（opus 批量审查 P2 修复）：名单本身是一批飞书用户 open_id，
    # 与本文件其余凭据字段同一条纪律——不进 `repr(config)`，也就不会随手一个
    # `logger.info("配置 %s", config)` 就把内测名单整份写进日志。
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
    # 「公司+职能→指标名」翻译映射的外置路径（Issue #320）。**可选**：缺省时 loader
    # 回落随包发布的默认文件（`lingxi/config/company_function_metric_map.toml`）,
    # 与此前行为逐字节一致。配了就**优先**于包内默认——不是"包内文件缺失时的兜底"，
    # 是"只要配了这个变量，这台机器就只认这个文件"，让指标映射表的维护人（产品负责人，
    # 见决策记录 2026-08-24《管理员职责集与银河体系外权限动作边界》「归属」一节）能够
    # 编辑即生效，不必再为一行映射改动走一次完整镜像构建发布。**错配不是未配**：
    # 配了但指向的文件缺失或格式非法，与主密钥/花名册坐标那几个变量同一条纪律——
    # `_build_permission_refresh_duty` 在 `load_company_function_metric_map` 抛出
    # `OSError`/`ValueError` 时既有的 `metric_translation_map_unavailable` 分支原样
    # 覆盖这条路径，职责响亮地不注册，不静默回落包内默认（与「翻译映射不可用→告警」
    # 的既有纪律一致，见 `adapters/company_function_metric_map_file.py` 模块文档）。
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

        raw_workers = (source.get("LINGXI_ONBOARDING_WORKERS") or "").strip()
        if raw_workers:
            try:
                onboarding_workers = int(raw_workers)
            except ValueError:
                raise ValueError("环境变量 LINGXI_ONBOARDING_WORKERS 必须是正整数") from None
            if onboarding_workers < 1 or onboarding_workers > MAX_ONBOARDING_WORKERS:
                raise ValueError(
                    f"环境变量 LINGXI_ONBOARDING_WORKERS 必须在 1 到 {MAX_ONBOARDING_WORKERS} 之间"
                )
        else:
            onboarding_workers = DEFAULT_ONBOARDING_WORKERS

        from lingxi.core.identity.innertest_roster_gate import (
            InnerTestRosterConfigError,
            parse_innertest_roster,
        )

        try:
            # 未设置/空白解析成空集合（闸对任何人拒绝，见该模块「默认关闭＝全拒」）；
            # 含无法识别条目在这里**快速失败**，与本文件其余「配了但格式不对」变量
            # 同一条纪律——错配不是未配，静默降级会让人以为闸在正常放行内测名单。
            innertest_roster_open_ids = parse_innertest_roster(
                source.get("LINGXI_INNERTEST_ROSTER_OPEN_IDS")
            )
        except InnerTestRosterConfigError as error:
            raise ValueError(
                f"环境变量 LINGXI_INNERTEST_ROSTER_OPEN_IDS 不合法：{error}"
            ) from None

        raw_org_snapshot_round_budget = (
            source.get("LINGXI_ORG_SNAPSHOT_ROUND_BUDGET_SECONDS") or ""
        ).strip()
        if raw_org_snapshot_round_budget:
            try:
                org_snapshot_round_budget_seconds = float(raw_org_snapshot_round_budget)
            except ValueError as error:
                raise ValueError(
                    "环境变量 LINGXI_ORG_SNAPSHOT_ROUND_BUDGET_SECONDS 必须是正数秒，"
                    f"收到 {raw_org_snapshot_round_budget!r}"
                ) from error
            # `float()` 会把 `"inf"`/`"nan"`/`"1e309"`（超出 float 表示范围，溢出为
            # inf）这类字符串原样解析成非有限值——独立审查二轮 P2-B2：`<= 0` 的判据
            # 挡不住它们（`inf > 0`、`nan` 的比较恒为 False），一旦漏过去，
            # `round_budget()` 算出的截止时间会变成 `now + inf`（或 `now + nan`，
            # 比较行为同样不可靠），整轮请求前的预算检查因此恒不成立，等于**悄悄
            # 关闭**了 Issue #284 引入这道上界的初衷。这里显式拒绝非有限值，
            # 错配在进程启动时快速失败，报错写清实际收到的值（不是凭据，回显无害，
            # 且是诊断错配所必需的）。
            if not math.isfinite(org_snapshot_round_budget_seconds):
                raise ValueError(
                    "环境变量 LINGXI_ORG_SNAPSHOT_ROUND_BUDGET_SECONDS 必须是有限的正数秒，"
                    f"收到 {raw_org_snapshot_round_budget!r}（解析为 {org_snapshot_round_budget_seconds!r}）"
                )
            if org_snapshot_round_budget_seconds < MIN_ORG_SNAPSHOT_ROUND_BUDGET_SECONDS:
                # 下限校验（独立审查二轮 P2-B4）：见
                # :data:`MIN_ORG_SNAPSHOT_ROUND_BUDGET_SECONDS` 的取值依据。
                raise ValueError(
                    "环境变量 LINGXI_ORG_SNAPSHOT_ROUND_BUDGET_SECONDS 必须至少 "
                    f"{MIN_ORG_SNAPSHOT_ROUND_BUDGET_SECONDS:.0f} 秒"
                    "（预算必须大于全量轮实际耗时——stage 实测基线约 345 秒；低于任何"
                    "真实一轮耗时的预算是配置错误，会让快照永远无法更新），"
                    f"收到 {org_snapshot_round_budget_seconds!r}"
                )
        else:
            org_snapshot_round_budget_seconds = DEFAULT_ORG_SNAPSHOT_ROUND_BUDGET_SECONDS

        # 外置指标映射路径：与 LINGXI_USER_ENV_ROOT 同一条纪律（`optional_identifier`——
        # 不得包含空白字符），文件是否真的存在/合法留给 loader 在读取时判定（错配不是
        # 未配，这里不重复做存在性检查，否则两处判据会漂移）。
        company_function_metric_map_path = optional_identifier(METRIC_MAP_PATH_ENV)

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
            stock_token_app_token=optional_identifier("LINGXI_STOCK_TOKEN_BITABLE_APP_TOKEN"),
            stock_token_table_id=optional_identifier("LINGXI_STOCK_TOKEN_BITABLE_TABLE_ID"),
            query_mcp_endpoint=query_mcp_endpoint,
            query_mcp_timeout_seconds=probe_timeout,
            user_env_root=optional_identifier("LINGXI_USER_ENV_ROOT"),
            onboarding_workers=onboarding_workers,
            innertest_roster_open_ids=innertest_roster_open_ids,
            org_snapshot_round_budget_seconds=org_snapshot_round_budget_seconds,
            company_function_metric_map_path=company_function_metric_map_path,
        )

    @property
    def metric_map_path(self) -> Path | None:
        """``company_function_metric_map_path`` 解析成 ``Path``；未配置为 ``None``。

        供 ``apps/scheduler/assembly.py`` 直接传给 ``load_company_function_metric_map``
        ——把字符串转 ``Path`` 的这一步放在配置对象自己身上，而不是每个调用点各写一次
        三元表达式，理由见 :data:`company_function_metric_map_path` 字段文档。

        **解释规则本身不写在这里**：整段委托给
        :func:`~lingxi.adapters.company_function_metric_map_file.parse_metric_map_path`
        ——``apps/gateway/config.py`` 读同一个变量时用的是同一个函数（Trace #544
        S-2c），两个进程因此不可能对"该读哪一份映射"给出不同答案，见该函数文档。
        """

        return parse_metric_map_path(self.company_function_metric_map_path)
