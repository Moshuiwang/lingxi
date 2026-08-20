"""组织快照同步职责：:class:`OrgSnapshotSyncDuty`（Issue #250，Epic B 缺件）。

四张 ``feishu_org_*`` 组织快照表此前全空，且产品侧没有任何东西会写它们——写入
适配器（``adapters/postgres_identity.py`` 的 ``PostgresOrgSnapshotStore``）与
读取适配器（``adapters/feishu_directory.py`` 的 ``FeishuDirectoryClient`` +
``adapters/feishu_org_snapshot_reader.py`` 的 ``read_org_snapshot``）都已就绪，
本模块是把两者接起来的唯一调用点。首次开通链的第一步是「组织快照定位身份」
（``AutoOnboardingRunner`` 经 ``PostgresOrgSnapshotStore.lookup``）；查不到就直接
落 ``LX-ONBOARD-001``（已转交管理员），因此这份快照是首次开通链能不能真的跑出
结果的前置。

## 每 UTC 日至多一轮

一轮同步要发起数百次分页请求（8 个关联租户 × 约 25 个部门的递归下钻 × 应用/用户
两条路径，Issue #250 编排者 2026-08-19 实测规模），而组织架构的变动频率远低于
花名册的每日节奏。高频跑它只会不必要地打满对端速率与本进程资源，换不来任何新鲜度
收益。因此按**日**节奏，形状照 :class:`~lingxi.apps.scheduler.roster_audit.
RosterAuditDuty` 的 ``_completed_on`` 水位（同一天只做一次，不做才有意义的重复
工作）——与花名册不同的是，这里没有"空差异不发"这类产品概念，只有"今天有没有
成功换一轮"。

**当日水位对进程重启保持——但只靠读库，不靠租约。** ``_completed_on`` 本身仍是
内存值，进程重启会清零；``run_once`` 因此在当天第一次调用时额外问一次
``store.has_complete_run_on(today)``（读 ``feishu_org_sync_run`` 里今天 UTC 有没有
``complete`` 批次），问过之后当天不再重复问。这不是数据库租约或领导权选举——本仓库
当前是**单实例假设**（Epic C 冻结审查已作为知情接受登记），这里只是把"今天做没做过"
这一件事从内存挪到已经在写的表上，避免重启后当天再跑一次数百次分页请求的全量扫描
（Issue #250 编排者复查 F8；那次重复扫描本身会被 ``commit_batch`` 内的
``superseded`` 收敛掉，不是数据风险，纯粹是浪费）。

## 失败就保留上一份，不覆盖

**任何读取失败（传输异常、分页停滞、递归上界、身份字段缺失）都不会调用
``commit_batch``**：``read_org_snapshot`` 对这些情况一律原样抛出异常，本职责
接住之后只记审计、不置位当日水位，让下一轮重试；库里最近一次的完成批次原样保留。
**完整性校验不通过同理**：``commit_batch`` 内部调用
``core/identity/org_snapshot.require_complete_batch``，不通过时只落一条
``failed`` 批次、不提交任何成员行，并重新抛出
``SnapshotIntegrityError``——本职责同样只记审计、不置位水位。两条路径合起来就是
"空源 / 半页 / 超时 / 格式异常不得替换基线"：判据本身不在这里，**这里只负责不去
绕开它**。

## 令牌供给：两条路径复用装配层已有的供给，不新增凭据材料

- **用户身份路径**（``FeishuDirectoryClient.list_collaboration_tenants`` /
  ``list_visible_organization``）用的令牌供给与首次开通编排的在职状态实时回读
  是**同一个** ``Callable[[], str]``（``apps/scheduler/assembly.py`` 里的
  ``supply``，源头是专用授权主体那条一次性 ``refresh_token``）。**不新增第二个
  消费者**——那条一次性令牌全系统只允许一个消费者，2026-08-08 曾因此烧掉产品
  负责人的一次性授权码。``RosterAccessTokenProvider`` 的"手上有新鲜的就直接给"
  语义保证同一天内不管多少个调用方轮流取，实际换取只发生一次。
- **应用身份路径**（``list_collaboration_tenants_as_app`` / ``list_share_entities``）
  用的是 ``tenant_access_token``——它不是一次性凭据、没有消费者数量上限（见
  ``core/permission/tenant_token_supply.py`` 的模块文档），因此这里直接复用
  装配层已经为权限发布表建好的那条应用身份供给（``permission_table_access_token``），
  不再另起一条 ``TenantAccessTokenSupply``：两处消费的是同一个 ``app_id``/
  ``app_secret`` 换来的同一类令牌，与访问哪个资源无关，复用只是省一次多余的换取，
  不产生新的凭据材料或新的失败模式。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

from lingxi.core.identity.org_snapshot import SnapshotBatch, SnapshotIntegrityError

logger = logging.getLogger(__name__)

# 失败重试的退避（Issue #268 F3）：stage 实测每 30 秒重试一轮、连续失败数十轮，
# 按 30 秒 tick 计一天约 2880 次无效外部调用——而本职责的产品语义只要求"每 UTC 日
# 成功一轮"（见上方模块文档「每 UTC 日至多一轮」），失败重试完全不需要贴着 tick
# 节奏。步长与封顶照 `adapters/postgres_late_readiness_recovery.py` 的
# `NOTICE_BACKOFF_STEP_SECONDS`/`NOTICE_BACKOFF_CEILING_SECONDS`（5 分钟起步、封顶
# 1 小时）——同一个"认领/失败即退避"形状的既有先例，不新造机制。按**连续失败次数**
# 线性放大（而不是像那处先例按"认领次数"），因为本职责一次失败就是"整轮读取或整轮
# 完整性校验没通过"，不是逐条重试单个对象。封顶 1 小时把最坏情况从约 2880 次/天
# 压到约 24 次/天，仍留有"当天多次机会自愈"的余量。
#
# **读取失败与完整性校验失败共用同一条退避**（同一个 `_next_attempt_at`/
# `_failure_streak`），不是两套机制：完整性校验只会在两条身份路径都已经读完一整轮
# （数百次分页请求）之后才可能失败，立即重试的外部成本与读取失败同一个量级甚至更
# 高——不存在"这条更便宜所以可以贴着 tick 重试"的理由。任何一次真正往前走了（提交
# 成功、或从持久化水位发现今天已完成）都会把两个字段一起清零，见 `run_once`。
# `_next_attempt_at` 是跨 UTC 日界的绝对时间戳，不在日界特意清零：最坏情况下只会
# 把新一天的第一次尝试再晚半小时到一小时，换来的是代码不必再区分"日界内退避"与
# "日界外退避"两套语义——`_completed_on`/持久化水位已经保证了"当天完成后不会再被
# 这条退避挡住"。
READ_FAILURE_BACKOFF_STEP_SECONDS = 300
READ_FAILURE_BACKOFF_CEILING_SECONDS = 3600


class TokenSupplyFailure(RuntimeError):
    """令牌供给失败的安全分类（Issue #250 编排者复查 F6）。

    应用令牌、用户令牌、真实扫描三种失败此前在 ``org_snapshot_sync.read_failed``
    审计里只留下 ``error=<异常类型>``，分辨不出到底是哪条供给出的问题——运维排障
    因此没法直接判断"该去查哪条令牌"还是"这是一次真实扫描失败"。这里只包一层
    分类标签（``supply``），**不携带原始异常的正文或消息**：调用方（
    ``apps/scheduler/assembly.py``）分别包装两个供给的调用点，抛出时用
    ``raise TokenSupplyFailure(...) from error`` 保留因果链供本地调试，但审计只读
    ``supply`` 与 ``type(error).__name__``，两者都不含令牌值。
    """

    def __init__(self, supply: str) -> None:
        super().__init__(f"组织快照令牌供给失败：{supply}")
        self.supply = supply


class _AuditSink(Protocol):
    def record(self, action: str, /, **fields: object) -> None: ...


class _SnapshotStore(Protocol):
    def commit_batch(
        self,
        batch: SnapshotBatch,
        *,
        source_app_id: str,
        run_id: str | None = None,
        started_at: datetime | None = None,
    ) -> str: ...

    def has_complete_run_on(self, day: date) -> bool: ...


class OrgSnapshotSyncDuty:
    """每 UTC 日至多一轮：读关联组织通讯录 → 校验批次完整性 → 写四张快照表。

    只编排注入的读取函数与写入端口，本身不做任何网络或数据库 I/O——真正的递归
    遍历在 :func:`lingxi.adapters.feishu_org_snapshot_reader.read_org_snapshot`，
    真正的落库与完整性校验在
    :meth:`~lingxi.adapters.postgres_identity.PostgresOrgSnapshotStore.commit_batch`。
    """

    name = "组织快照同步"

    def __init__(
        self,
        *,
        read_snapshot: Callable[[], SnapshotBatch],
        store: _SnapshotStore,
        audit: _AuditSink,
        source_app_id: str,
        clock: Callable[[], datetime] | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        if not source_app_id:
            raise ValueError("source_app_id 不能为空——它是写进 feishu_org_sync_run 的必填列")
        self._read_snapshot = read_snapshot
        self._store = store
        self._audit = audit
        self._source_app_id = source_app_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._stop = threading.Event() if stop is None else stop
        self._completed_on: date | None = None
        # 本进程今天是否已经问过持久化水位（F8）。只问一次：问过之后，"今天到底
        # 完成没有"完全由 `_completed_on`（本进程自己跑出来的结果）决定，不需要
        # 每一轮都去读库——单实例假设下不会有别的进程在这中间把水位从 False 改成
        # True（Epic C 冻结审查已登记的既知前提，见模块文档）。
        self._checked_persisted_watermark_for: date | None = None
        # 失败推进退避（Issue #268 F3，见模块顶部 `READ_FAILURE_BACKOFF_*` 的形状
        # 说明）。两个字段只在读取失败或完整性校验不通过时推进，任何一种"本轮真的
        # 往前走了"（提交成功、或从持久化水位发现今天已完成）都会清零。
        self._next_attempt_at: datetime | None = None
        self._failure_streak = 0

    @property
    def completed_on(self) -> date | None:
        """已完成同步的那一天。``None`` 表示本进程实例今天还没成功过一轮。"""

        return self._completed_on

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        self._stop.set()

    def _advance_backoff(self, now: datetime) -> int:
        """记一次"这轮没能往前走"，返回本次算出的退避秒数（Issue #268 F3）。"""

        self._failure_streak += 1
        backoff_seconds = min(
            self._failure_streak * READ_FAILURE_BACKOFF_STEP_SECONDS,
            READ_FAILURE_BACKOFF_CEILING_SECONDS,
        )
        self._next_attempt_at = now + timedelta(seconds=backoff_seconds)
        return backoff_seconds

    def _reset_backoff(self) -> None:
        self._next_attempt_at = None
        self._failure_streak = 0

    def run_once(self) -> str | None:
        """跑一轮。返回写入的 ``run_id``；未执行或本轮未能提交时返回 ``None``。"""

        if self._stop.is_set():
            return None
        now = self._clock()
        today = now.date()
        if self._completed_on == today:
            return None

        if self._checked_persisted_watermark_for != today:
            # 进程重启后内存水位归零：不补这一步，当天会做第二次全量扫描（数百次
            # 分页请求，外加一个稍后被 `superseded` 收敛掉的重复批次）——不是凭据
            # 风险（一次性 refresh_token 的"每 UTC 日至多消费一次"判据落在凭据文件
            # 而非内存，见 `RosterAccessTokenProvider`），但纯属浪费（Issue #250
            # 编排者复查 F8）。查询失败按"未知"处理、不阻塞本轮：这只是一个避免
            # 重复工作的优化，不是完整性判据本身。
            self._checked_persisted_watermark_for = today
            try:
                already_done = self._store.has_complete_run_on(today)
            except Exception as error:  # noqa: BLE001 - 只记异常类型
                self._audit.record("org_snapshot_sync.watermark_check_failed", error=type(error).__name__)
                logger.warning(
                    "组织快照当日持久化水位检查失败，按未知处理、继续尝试本轮 error=%s",
                    type(error).__name__,
                )
            else:
                if already_done:
                    self._completed_on = today
                    # 本进程自己一次读取都没做就发现"今天已经完成"：不是这里的
                    # 失败推进了退避，但之前若有残留的退避状态（例如昨天读取失败
                    # 到今天才被别的实例补上）已经没有意义，一并清零。
                    self._reset_backoff()
                    self._audit.record("org_snapshot_sync.already_completed_today", source="persisted_watermark")
                    return None

        if self._next_attempt_at is not None and now < self._next_attempt_at:
            # 退避窗口内：安静跳过，不发起外部读取。不留审计——F3 要压的是外部
            # 调用次数，把每 30 秒一次的跳过原样记下来会制造同一量级的审计噪音，
            # 与目标相悖；`read_failed` 审计本身已经带上 `attempt`/`backoff_seconds`，
            # 足够看出"这一轮之后进入了退避"。
            return None

        try:
            batch = self._read_snapshot()
        except Exception as error:  # noqa: BLE001 - 只记异常类型，正文可能带响应内容
            fields: dict[str, object] = {"error": type(error).__name__}
            supply = getattr(error, "supply", None)
            if isinstance(supply, str) and supply:
                # 令牌供给失败时额外标注是哪一条（F6）：不读取原始异常的正文或
                # 消息，只读 `TokenSupplyFailure.supply` 这个安全分类标签。
                fields["supply"] = supply
            code = getattr(error, "code", None)
            if isinstance(code, str) and code:
                # `FeishuDirectoryError.code`（Issue #268 F1）：其类文档承诺该属性
                # "供程序判断，消息里不含任何凭据"——只读这一个分类标签，不读
                # `str(error)`（消息正文）、不读响应正文、不读令牌或 URL 查询串。
                # 非 `FeishuDirectoryError` 的其他异常没有这个属性，`getattr`
                # 拿到 `None`，仍然只靠上面已经记下的 `type(error).__name__` 归类。
                fields["code"] = code
            backoff_seconds = self._advance_backoff(now)
            fields["attempt"] = self._failure_streak
            fields["backoff_seconds"] = backoff_seconds
            self._audit.record("org_snapshot_sync.read_failed", **fields)
            logger.error(
                "组织快照读取失败，保留上一份完成批次，退避 %s 秒后重试 error=%s code=%s supply=%s",
                backoff_seconds,
                type(error).__name__,
                code if isinstance(code, str) and code else "unknown",
                supply if isinstance(supply, str) else "unknown",
            )
            return None

        try:
            run_id = self._store.commit_batch(batch, source_app_id=self._source_app_id, started_at=now)
        except SnapshotIntegrityError as error:
            # 也推进退避（与读取失败共用同一条，见模块顶部常量注释）：走到这一步
            # 说明两条身份路径都已经读完一整轮（数百次分页请求）才在交叉校验上失败，
            # 贴着 tick 立即重试的外部成本与读取失败同一个量级，没有理由只挡一条。
            backoff_seconds = self._advance_backoff(now)
            self._audit.record(
                "org_snapshot_sync.integrity_rejected",
                problems=[problem.value for problem in error.report.problems],
                tenants=error.report.tenant_count,
                departments=error.report.department_count,
                members=error.report.member_count,
                attempt=self._failure_streak,
                backoff_seconds=backoff_seconds,
            )
            logger.error(
                "组织快照完整性校验未通过，保留上一份完成批次，退避 %s 秒后重试 problems=%s",
                backoff_seconds,
                ",".join(problem.value for problem in error.report.problems),
            )
            return None
        except Exception as error:  # noqa: BLE001 - 只记异常类型，写库失败原样上抛前先留痕
            self._audit.record("org_snapshot_sync.commit_failed", error=type(error).__name__)
            logger.error("组织快照写入失败 error=%s", type(error).__name__)
            raise

        self._completed_on = today
        self._reset_backoff()
        self._audit.record(
            "org_snapshot_sync.committed",
            run_id=run_id,
            tenants=len(batch.tenants),
            departments=len(batch.departments),
            members=len(batch.members),
        )
        logger.info(
            "组织快照已同步 run_id=%s 租户=%s 部门=%s 成员=%s",
            run_id,
            len(batch.tenants),
            len(batch.departments),
            len(batch.members),
        )
        return run_id
