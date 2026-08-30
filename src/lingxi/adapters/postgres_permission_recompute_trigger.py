"""``core/admin/card_callback.py`` 的 ``PermissionRecomputeTrigger`` 端口的唯一真实
实现（Issue #438）：确认卡执行成功后，对目标用户即时触发一次定向重算/发布。

## 为什么住在 ``adapters/``，不是 ``apps/gateway`` 内联一段

``core/permission/targeted_recompute.py`` 只编排纯业务规则（模块文档），需要的
六个只读/读写口全部是既有 Postgres 适配器（模块级函数/类），把它们逐个构造起来
再喂给 ``TargetedPermissionRecompute`` 是"装配"而不是"业务规则"——按代码框架
第二节，这类装配可以留在 ``apps/`` 里做，但独立成一个 ``adapters/`` 模块能让
``card_callback.py`` 的真实装配点（``apps/gateway/__init__.py``）只 import 一个
类，且这个类可以脱离 gateway 的整段装配代码单独被测试（真库集成测试只需要
``PermissionRecomputeAdapter`` 一个对象）。

## 为什么 gateway 进程可以安全构造这些 store（不新增任何密钥/凭据面）

六个依赖分两类：

- **纯 Postgres 读写**（``PostgresRosterBaselineReader``、``PostgresRosterSnapshot
  Store``、``PostgresGalaxySnapshotReader``、``PostgresPermissionPublishStore``、
  ``local_override_reader``）：构造只需要 ``dsn``/``timeouts``，gateway 进程本来
  就持有一份用于待确认操作状态机的 Postgres DSN（同一个数据库）。
- **静态映射文件**（``load_role_function_map``/``load_company_function_metric_map``）：
  随包发布、无需任何密钥，任何进程都能安全读取。

**刻意不接的一项**：令牌密文读取口（``PostgresMcpTokenStore``）需要 MCP 令牌
加密主密钥（``LINGXI_MCP_TOKEN_ENCRYPT_KEY``）——这把主密钥目前只活在 scheduler
进程里，是首次开通编排解密专用凭据的同一把钥匙。为了这一个纵深字段（``token_
cipher`` 只在**新建**发布行时才需要，见 ``core/permission/publish_row.py``）把
它也接进 gateway（一个直接暴露在飞书长连接、处理外部回调的进程），是明显不成
比例的攻击面扩大，因此 ``TargetedPermissionRecompute`` 固定传 ``token_cipher=
None``（见该模块文档「三处刻意不同」第 3 条）——真的撞上"要新建行却没有密文"，
``record_decision`` 会失败，本类让异常原样冒泡，由 ``card_callback.py`` 的
best-effort 包裹按「降级回每日批」处理。**这条选择改变的是系统边界（哪个进程
持有哪把密钥），本卡不擅自扩大，本模块也因此明确排除了这一角，留给产品/架构
在需要时另行评估。**
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts
from lingxi.adapters.postgres_targeted_recompute_lookup import (
    resolve_local_override_target,
    resolve_open_id_target,
)
from lingxi.core.admin.pending_action import (
    LOCAL_PERMISSION_ACTION_TYPES,
    PendingAction,
    PendingActionType,
)
from lingxi.core.identity.roster_audit import ArchivedIdentity
from lingxi.core.permission.targeted_recompute import AuditSink, TargetedPermissionRecompute


class _BaselineIdentityLookup:
    """把 ``PostgresRosterBaselineReader.load_active_baseline()`` 的全量结果适配成
    按单个 ``user_id`` 查询的形状——不新增 SQL，只做客户端过滤（模块文档「刻意
    不同」第 1 条已经说明本卡不追加新的花名册查询口径）。

    定向重算只在**罕见的管理员点击**时触发，不在任何高频路径上；一次全表扫描
    换来"不用再写、再维护第二条与 `V-花名册-10`/`V-花名册-11` 口径必须逐字保持
    一致的 SQL"，这笔账划算。
    """

    def __init__(self, baseline: Sequence[ArchivedIdentity]) -> None:
        self._by_id = {identity.app_user_id: identity for identity in baseline}

    def find_active(self, *, user_id: str) -> ArchivedIdentity | None:
        return self._by_id.get(user_id)


class _RosterRowsAdapter:
    def __init__(self, store: Any) -> None:
        self._store = store

    def load_rows(self) -> Sequence[Mapping[str, Any]] | None:
        snapshot = self._store.load()
        return None if snapshot is None else snapshot.rows


def _resolve_target_user_id(dsn: str, timeouts: PostgresTimeouts, pending: PendingAction) -> str | None:
    if pending.action_type in LOCAL_PERMISSION_ACTION_TYPES:
        return resolve_local_override_target(dsn, pending.id, timeouts=timeouts)
    return resolve_open_id_target(dsn, pending.target_open_id, timeouts=timeouts)


class PermissionRecomputeAdapter:
    """``core/admin/card_callback.PermissionRecomputeTrigger`` 的真实实现。"""

    def __init__(
        self,
        dsn: str,
        *,
        timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS,
        audit: AuditSink,
    ) -> None:
        self._dsn = dsn
        self._timeouts = timeouts
        self._audit = audit

    def trigger(self, pending: PendingAction) -> None:
        """``card_callback.py`` 在确认执行成功后调用，best-effort（异常由调用方
        捕获并降级，见该模块「载体 #96」旁的执行成功钩子）。本方法自己**不**吞
        任何异常——吞了调用方就没有机会记"这次触发失败了"这条响亮审计。
        """

        # 延迟导入：与仓库既有的 Postgres 适配器同一惯例（构造时不连接数据库，
        # 调用时才建连接），也让"哪些依赖真的被这条调用路径用到"在 import 时机
        # 上一目了然。
        from lingxi.adapters.company_function_metric_map_file import load_company_function_metric_map
        from lingxi.adapters.postgres_galaxy_snapshot import PostgresGalaxySnapshotReader
        from lingxi.adapters.postgres_local_permission import local_override_reader
        from lingxi.adapters.postgres_permission_publish import PostgresPermissionPublishStore
        from lingxi.adapters.postgres_roster_audit import PostgresRosterBaselineReader
        from lingxi.adapters.postgres_roster_snapshot import PostgresRosterSnapshotStore
        from lingxi.adapters.role_function_map_file import load_role_function_map

        user_id = _resolve_target_user_id(self._dsn, self._timeouts, pending)
        if user_id is None:
            self._audit.record(
                "permission_targeted_recompute.target_unresolved",
                pending_action_id=pending.id,
                action_type=pending.action_type.value,
            )
            return

        baseline = PostgresRosterBaselineReader(self._dsn, timeouts=self._timeouts).load_active_baseline()
        publish_store = PostgresPermissionPublishStore(self._dsn, timeouts=self._timeouts)
        recompute = TargetedPermissionRecompute(
            identities=_BaselineIdentityLookup(baseline),
            roster_snapshot=_RosterRowsAdapter(
                PostgresRosterSnapshotStore(self._dsn, timeouts=self._timeouts)
            ),
            galaxy=PostgresGalaxySnapshotReader(self._dsn, timeouts=self._timeouts),
            decisions=publish_store,
            publish_history=publish_store,
            role_function_map=load_role_function_map(),
            metric_translation_map=load_company_function_metric_map(None),
            audit=self._audit,
            local_overrides=local_override_reader(self._dsn, timeouts=self._timeouts),
        )

        if pending.action_type is PendingActionType.SUSPEND_USER:
            recompute.force_revoke(user_id=user_id)
        else:
            recompute.recompute_and_publish(user_id=user_id)


__all__ = ["PermissionRecomputeAdapter"]
