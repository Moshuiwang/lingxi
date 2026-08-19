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
from datetime import date, datetime, timezone
from typing import Protocol

from lingxi.core.identity.org_snapshot import SnapshotBatch, SnapshotIntegrityError

logger = logging.getLogger(__name__)


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

    @property
    def completed_on(self) -> date | None:
        """已完成同步的那一天。``None`` 表示本进程实例今天还没成功过一轮。"""

        return self._completed_on

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        self._stop.set()

    def run_once(self) -> str | None:
        """跑一轮。返回写入的 ``run_id``；未执行或本轮未能提交时返回 ``None``。"""

        if self._stop.is_set():
            return None
        now = self._clock()
        today = now.date()
        if self._completed_on == today:
            return None

        try:
            batch = self._read_snapshot()
        except Exception as error:  # noqa: BLE001 - 只记异常类型，正文可能带响应内容
            self._audit.record("org_snapshot_sync.read_failed", error=type(error).__name__)
            logger.error(
                "组织快照读取失败，保留上一份完成批次，下一轮重试 error=%s",
                type(error).__name__,
            )
            return None

        try:
            run_id = self._store.commit_batch(batch, source_app_id=self._source_app_id, started_at=now)
        except SnapshotIntegrityError as error:
            self._audit.record(
                "org_snapshot_sync.integrity_rejected",
                problems=[problem.value for problem in error.report.problems],
                tenants=error.report.tenant_count,
                departments=error.report.department_count,
                members=error.report.member_count,
            )
            logger.error(
                "组织快照完整性校验未通过，保留上一份完成批次，下一轮重试 problems=%s",
                ",".join(problem.value for problem in error.report.problems),
            )
            return None
        except Exception as error:  # noqa: BLE001 - 只记异常类型，写库失败原样上抛前先留痕
            self._audit.record("org_snapshot_sync.commit_failed", error=type(error).__name__)
            logger.error("组织快照写入失败 error=%s", type(error).__name__)
            raise

        self._completed_on = today
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
