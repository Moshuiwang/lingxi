"""``lingxi-scheduler``：定时职责进程。

进程现在跑**十二个**职责，由 :class:`SchedulerLoop` 按同一个周期依次驱动：

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
   有差异（或快照超龄 / 无快照）就向管理群发一条日报（Issue #52）。管理群 ID、花名册
   Base ``app_token``、``table_id`` 三个前置任缺其一，或读取令牌供给未接线，就不注册
   （断言 ``V-花名册-29``，留一条指名原因的审计），进程照常启动、其余职责照常运行。

   **管理群未配置时，写快照的这一半不随之停摆**（Issue #275）：``roster_snapshot``
   表是首次开通链第二步（``core/identity/onboarding_runner.py::_match``）与每日
   权限重算（职责 6）的共同数据前提，两者都直接读这张表，不经过本职责。此前三个前置
   捆在一起，导致"日报发到哪个群"这个纯通知配置决定"员工能不能被开通"
   （2026-08-21 首触冒烟实测坐实）。因此本职责未装配时，``build_loop`` 会改为尝试
   装配 :class:`~lingxi.apps.scheduler.roster_audit.RosterSnapshotSyncDuty`——只依赖
   Base 坐标与令牌供给、不比对不发送的"只写"职责；两者**互斥**注册，同一时刻至多一个
   在触发花名册读取，因此花名册这一组本身仍然只占一个职责位，不因为互斥切换而变成
   两个。见
   :func:`~lingxi.apps.scheduler.assembly._build_roster_snapshot_sync_duty` 与
   :class:`~lingxi.apps.scheduler.roster_audit.RosterSnapshotSyncDuty` 的文档字符串。

   **令牌供给自 Issue #215 起由本模块装配**（方案 C 主接线，产品负责人 2026-08-18
   裁定）：凭据轮换职责按需消费一次一次性 ``refresh_token``，把派生的短期
   ``access_token`` 放进**进程内**持有者（不落盘、不进日志与审计），日报侧（或与它
   互斥的快照写入职责）按新鲜度取用。消费频率随之从约 5.6 天一次变成按需（令牌寿命
   约 2 小时，正常一天约 12 次）。频率上界随凭据落盘、进程内不留第二份账本副本：
   Issue #276（产品负责人 2026-08-21 裁定）解除了此前"每 UTC 日至多消费一次"的
   自设限制，改为两次消费的最小间隔（默认 5 分钟）与每日消费次数上界（默认 100 次）
   双重保护，判据仍是随凭据落盘的 ``refresh_consumed_at`` / ``refresh_consumed_count``。
   **唯一消费者的边界没有变，且与频率上界是两件不同的事**——2026-08-08 授权码被烧
   那次事故的形状是**两个客户端抢占同一条通道**，不是"换取太频繁"（详见
   ``core/identity/access_token_supply.py`` 模块文档）。
   **代码层接上不等于真的读得到花名册**：真实读取所需的专用主体凭据自 2026-08-09 起
   未落盘（Issue #52 的 G-READ 判定），当前部署也还没配 Base 坐标那两个变量，因此
   这条链在真实环境里一轮都没跑过。
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

8. **组织快照同步**（:class:`~lingxi.apps.scheduler.org_snapshot_sync.
   OrgSnapshotSyncDuty`，Issue #250）——每 UTC 日至多一轮：递归遍历关联组织的应用身份
   与专用授权用户身份两条路径 → 校验批次完整性（不通过不提交半轮）→ 写四张
   ``feishu_org_*`` 快照表。它是首次开通链身份定位那一步唯一的数据来源；此前四张表
   全空、产品侧没有任何东西写它们，是 Epic B 一处未被发现的缺件（写入/读取适配器都已
   就绪，缺的只是这条生产调用点）。第八个职责同样是**条件注册**的：两个令牌供给
   （用户身份、应用身份）任一未接线就不注册（**恰一条**审计），生产装配路径下两条都是
   默认值因此不会真的触发。位置排在权限发布消费之后、首次开通编排之前，理由见
   :func:`~lingxi.apps.scheduler.assembly._build_org_snapshot_sync_duty` 调用点的
   注释。
9. **首次开通编排**（``apps/scheduler/onboarding.py`` + :class:`~lingxi.core.conversation.
   onboarding_recovery.OnboardingReconciler`）——认领 gateway 记下的未开通首聊事件，在
   **自己的线程池**上跑完整条开通链（身份 → 匹配 → 建档 → 用户环境 → 权限发布 →
   MCP 就绪 → ``active``），并自己私聊通知用户。第九个职责同样是**条件注册**的：缺 MCP
   令牌主密钥、问数 MCP 端点、用户环境根目录或在职状态令牌供给任一项就不注册（**恰一条**
   审计），此时**没有任何人认领**那些事件——它们原样留在库里，不会被认领走再烧掉。

   **装配不变量（外部集成面审查坐实的 F1）**：``onboarding != None ⇒ permission_publish
   != None 且 permission_publish.publish_wired``——第七个职责（权限发布消费）的**发布面**
   如果因为自己的前置不齐没有装配，开通链排出的发布意图此后没有任何职责会消费；第十个
   职责（迟到就绪恢复）救不了这个缺口，它只能确认"已经进入就绪等待"的用户。判据是
   ``publish_wired`` 而不是 ``is not None``：第七个职责在只缺权限表 Base 坐标时会**照常
   注册**一个只有就绪面的半个职责（那是它自己刻意的设计），旧判据会把它当成"发布执行者
   在"而放行开通——冻结候选审查 2026-08-21 的 F1，由产品负责人当天第二次真实开通失败
   （``publish_not_completed``）坐实。两种情形下首次开通编排都**不注册**，并各留一条
   与其它前置同一形状的**恰一条**审计（``reason=permission_publish_not_assembled`` /
   ``permission_publish_not_wired``），在**认领任何用户之前**挡住，而不是让用户先建档、
   建了用户环境再永远卡住。见
   :func:`~lingxi.apps.scheduler.assembly._build_onboarding_duty` 文档字符串。

   **为什么它住在本进程而不是 gateway**（产品负责人 2026-08-18 裁定，决策记录见
   ``docs/决策记录/2026-08-18-首次开通编排住在scheduler.md``）：在职状态必须实时回读飞书
   成员详情，而那需要专用授权主体派生的短期令牌；那条一次性 ``refresh_token`` 全系统只
   允许一个消费者，它已经在本进程里（职责 1 + 进程内持有者）。让 gateway 去换等于制造
   第二条凭据通道——2026-08-08 授权码被烧那次事故的形状。

   **它不占 tick**：单条链最长会阻塞十七分钟（发布等待 + 就绪预算），因此跑在专属线程池
   上；``run_once`` 只做"认领若干条并提交"，认领量由执行器剩余容量压住。

10. **迟到就绪恢复**（:class:`~lingxi.apps.scheduler.late_readiness_recovery.
    LateReadinessRecoveryDuty`，[V-开通-18](../../../../docs/技术设计/验收矩阵-开通与身份.md)）——
    首次开通编排（职责 9）的阻塞式就绪确认判超时之后，``provisioning_state`` 停在
    ``mcp_syncing``，此前没有任何东西会再回来看这个人。第十个职责每轮按十五分钟一次
    的节奏（在候选查询里判到期，不是每轮都真的发探针）把这些用户重新捞回来再探一次，
    就绪就推进 ``active`` 并主动通知「开通完成」；未就绪的人**不**被推进、也**不**收到
    任何暗示已经可用的消息。**总能注册**：候选查询、状态推进与通知只需要
    ``LINGXI_POSTGRES_DSN``/飞书应用凭据（两者都是必填项）；只有需要真探针才能推进的
    那一路会在缺 MCP 令牌主密钥或问数 MCP 端点时不装配，并留下**恰一条**审计——已经
    探到就绪但因崩溃未推进的候选仍会被续做。判定复用
    :mod:`lingxi.core.permission.mcp_readiness` 的探针与分类（新增
    :class:`~lingxi.core.permission.mcp_readiness.ReadinessRecoveryTicker`，与既有的
    阻塞式/tick 式就绪确认同一份判定实现），不新造第二套"就绪"的定义。语义、放在哪、
    节奏与"要试到什么时候为止"的产品决定缺口，见该模块自己的文档字符串。
11. **内测每日通报**（:class:`~lingxi.apps.scheduler.daily_report.DailyReportDuty`，
    Issue #303 S-O-01）——每天（UTC 日界）跑一轮：分四段独立读取昨日的
    ``task``/``task_delivery_event`` 统计（活跃用户与任务量分布、成功/失败/超时/停止
    分布与失败分类 Top、Agent 执行耗时分布、投递结果分布）→ 纯函数聚合、对失败分类
    做连续在榜天数节流 → 渲染统计级正文 → 发送管理群。**唯一前置是管理群 chat_id**
    （**恰一条**审计，形状照职责 5），没有其它外部标识或凭据依赖——本职责只读 Lingxi
    自己的两张表。

    **token 用量与成本估算、工具调用拒绝计数**这两段在当前架构下**恒为**「不可判定」：
    它们只存在于 worker 进程自己的结构化日志（``worker.task.terminal`` 的
    ``resources.usage``/``audit.denied_count``），scheduler 与 worker 是独立进程、
    不共享文件系统或日志聚合通道，没有任何代码路径能读到——不是本轮查询失败，是这条
    数据源在这个架构下压根不存在，须由 worker 侧新增落库字段才能取得（超出本 Story
    范围，见 :mod:`lingxi.core.daily_report` 模块文档）。其余四段各自独立读取、独立
    失败：任一段本轮读不出来只让**那一段**显式标「不可判定」，不拖累其余段落、也不
    拖累整轮发送（与职责 5 的"整轮读不出来就整轮重试"不同，是 #303 明确要求的行为）。

    判重水位（同一天至多发一条）与节流状态都是进程内存，重启清零，与职责 5 的
    ``_completed_on`` 同一条已知残留，见该模块自己的文档字符串。发送失败记结构化
    审计并通过与职责 5 共用的告警接线触发运行告警，不静默。

12. **内测采集到期删除**（:class:`ContentCaptureRetentionDuty`，对抗审查 2026-09-02
    C-7）——删掉 ``innertest_content_capture`` 表里过了九十天的行。这张表存的是用户
    问题原文、模型回答原文与工具调用详情，迁移 ``0069`` 建了 ``expires_at`` 触发器和
    到期扫描索引，却**没有任何调用方**：九十天上限只存在于一个没人读的列里。这与职责
    4 的成因是同一个形状（机制交付了、调用点没接），处置也照它：应用层小批量 DELETE、
    每轮一次、不循环到删空、失败关闭。**无条件装配**——只需要连接串。生产该表恒空
    （内容采集在生产一律不生效，见 ``apps/worker/config.py::declares_production``），
    这条职责在生产每轮删 0 行、不打日志。

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

首次开通编排的执行器有自己独立的线程池（``apps/scheduler/onboarding.py::
OnboardingExecutor``），不跑在 ``SchedulerLoop`` 的调用线程上——``run_forever()``
返回（或抛出未处理异常）只说明各职责不再被 tick 调用，不代表这些独立线程也已经
停止领取新工作、收工退出。``main()`` 因此用 ``try``/``finally`` 包住
``run_forever()``，在 finally 里无条件调用一次
:func:`~lingxi.apps.scheduler.onboarding.join_onboarding_executors`（Issue #284
C 组 #8，Trace #373 D7 裁定修复；P1-B，Trace #373 H1 批终修复包② codex 外审），
把"停止领取、等在途工作在预算内收尾"这条退出语义显式接到这条独立线程池上——正常
返回与主循环抛出未处理异常两条退出路径都会走到，不再只覆盖正常返回那一条；收尾
本身失败只记日志，不覆盖 ``run_forever()`` 的原始异常。见该函数文档字符串。
"""

from __future__ import annotations

import logging
import sys
import traceback

from lingxi.apps.scheduler.alerting_assembly import _combined_heartbeat, build_alerting_duty
from lingxi.apps.scheduler.assembly import (
    _build_late_readiness_recovery_duty,
    _build_onboarding_duty,
    _build_org_snapshot_sync_duty,
    _build_permission_publish_duty,
    _build_permission_refresh_duty,
    _build_permission_retention_duty,
    _build_readiness_follow_up,
    _build_roster_audit_duty,
    _build_roster_snapshot_sync_duty,
    _build_stalled_provisioning_duty,
    build_loop,
)
from lingxi.apps.scheduler.audit import AuditSink, StructuredLogAuditSink
from lingxi.apps.scheduler.config import (
    DEFAULT_FEISHU_BASE_URL,
    DEFAULT_INTERVAL_SECONDS,
    SchedulerConfig,
    _Secret,
)
from lingxi.apps.scheduler.credential_rotation import (
    SAVE_RETRY_BACKOFF_SECONDS,
    CredentialRotationLoop,
    RotationReport,
    _is_definite_failure,
)
from lingxi.apps.scheduler.daily_report import DailyReportDuty, _build_daily_report_duty
from lingxi.apps.scheduler.late_readiness_recovery import (
    DEFAULT_NOTICE_DRAIN_LIMIT,
    DEFAULT_RECOVERY_INTERVAL_SECONDS,
    DEFAULT_RECOVERY_LIMIT,
    LateReadinessRecoveryDuty,
    LateReadinessRecoveryReport,
)
from lingxi.apps.scheduler.loop import SchedulerLoop, install_signal_handlers
from lingxi.apps.scheduler.onboarding import join_onboarding_executors
from lingxi.apps.scheduler.org_snapshot_sync import OrgSnapshotSyncDuty
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
from lingxi.apps.scheduler.retention import (
    IDLE_CONVERSATION_SWEEP_AFTER,
    ContentCaptureRetentionDuty,
    IdleConversationSweepDuty,
    PermissionRetentionReport,
    PermissionRetentionSweepDuty,
    RetentionCleanupDuty,
)
from lingxi.apps.scheduler.roster_audit import RosterAuditDuty, RosterSnapshotSyncDuty
from lingxi.apps.scheduler.stalled_provisioning import (
    DEFAULT_STALLED_LEASE_SECONDS,
    DEFAULT_STALLED_LIMIT,
    StalledProvisioningDuty,
    StalledProvisioningReport,
)
from lingxi.core.alerting import (
    AlertDispatcher,
    AlertingDuty,
    AlertManager,
    AlertPolicy,
)
from lingxi.core.alerting import (
    AlertSender as _AlertSender,
)

# 以下名字本文件不直接使用，只是 re-export：调用方历史上一直从
# ``lingxi.apps.scheduler`` 这个包顶层导入它们，而不是各自的实现模块。
__all__ = [
    "AlertDispatcher",
    "AlertManager",
    "AlertPolicy",
    "AlertingDuty",
    "AuditSink",
    "ContentCaptureRetentionDuty",
    "CredentialRotationLoop",
    "DEFAULT_FEISHU_BASE_URL",
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_NOTICE_DRAIN_LIMIT",
    "DEFAULT_RECOVERY_INTERVAL_SECONDS",
    "DEFAULT_RECOVERY_LIMIT",
    "DEFAULT_STALLED_LEASE_SECONDS",
    "DEFAULT_STALLED_LIMIT",
    "DailyReportDuty",
    "IDLE_CONVERSATION_SWEEP_AFTER",
    "IdleConversationSweepDuty",
    "LateReadinessRecoveryDuty",
    "LateReadinessRecoveryReport",
    "OrgSnapshotSyncDuty",
    "PERMISSION_REFRESH_REASON",
    "PERMISSION_REVOKE_REASON",
    "PermissionPublishDuty",
    "PermissionPublishReport",
    "PermissionRefreshDuty",
    "PermissionRefreshReport",
    "PermissionRetentionReport",
    "PermissionRetentionSweepDuty",
    "ReadinessFollowUp",
    "RetentionCleanupDuty",
    "RosterAuditDuty",
    "RosterSnapshotSyncDuty",
    "RotationReport",
    "SAVE_RETRY_BACKOFF_SECONDS",
    "SchedulerLoop",
    "StalledProvisioningDuty",
    "StalledProvisioningReport",
    "_AlertSender",
    "_Secret",
    "_build_daily_report_duty",
    "_build_late_readiness_recovery_duty",
    "_build_onboarding_duty",
    "_build_org_snapshot_sync_duty",
    "_build_permission_publish_duty",
    "_build_permission_refresh_duty",
    "_build_permission_retention_duty",
    "_build_readiness_follow_up",
    "_build_roster_audit_duty",
    "_build_roster_snapshot_sync_duty",
    "_build_stalled_provisioning_duty",
    "_is_definite_failure",
]

logger = logging.getLogger(__name__)


# _AlertSender/_PendingAlert/AlertDispatcher/AlertingDuty/_alert_utc 已迁移到
# lingxi.core.alerting（Issue #153，见本文件顶部的导入）：gateway 与 worker 也
# 需要装配同一套告警编排，三个 apps/<name> 互不 import，因此这段编排放进 core/
# （只编排注入接口，不直接做 I/O，与 core/conversation/pipeline.py 的
# EventPipeline 同一形状）。本模块沿用既有的 `_AlertSender`/`AlertDispatcher`/
# `AlertingDuty` 名字，既有测试不必改动。


def main(argv: list[str] | None = None) -> int:
    # 日志只到 stdout / stderr，不写文件、不自行轮转（断言 V-部署-04）。
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
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
    try:
        loop.run_forever()
    finally:
        # Issue #284 C 组 #8 + P1-B（codex 外审 · Trace #373 H1 批终修复包②）：
        # `run_forever()` 只保证不再有新一轮 tick，开通执行器自己的独立线程池
        # 需要单独接线停止领取新工作、并在预算内等在途链收尾（见模块文档「退出
        # 语义」一节）。此前这一行放在 `run_forever()` 之后、没有 finally 覆盖：
        # 主循环抛出未处理异常时这一行会被绕过，daemon 线程只能靠解释器退出时
        # 被任意截断，而不是走"停止领取、等在途工作在预算内收尾"这条退出语义。
        # 收尾自身的异常不得覆盖原始故障——记一条日志后放行，让 `run_forever()`
        # 的原始异常（如果有）继续原样向上传播。
        try:
            join_onboarding_executors(loop.duties)
        except Exception as error:
            # C-6：同上，只记类型名与调用栈帧，异常正文不进日志。
            logger.error(
                "lingxi-scheduler 收尾 join_onboarding_executors 失败 error=%s\n调用栈（不含异常正文）：\n%s",
                type(error).__name__,
                "".join(traceback.format_tb(error.__traceback__)),
            )
    return 0
