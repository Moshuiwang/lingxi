"""``core/admin/card_callback.py`` 的 ``PermissionRecomputeTrigger`` 端口的唯一真实实现。

确认卡执行成功后，对目标用户即时触发一次定向重算/发布。住在 ``adapters/``
而不是 ``apps/gateway`` 内联一段：六个依赖全部是既有
Postgres 适配器，独立成模块能让真实装配点只 import 一个类，且能脱离整段
gateway 装配代码单独测试。gateway 进程能安全构造这六个依赖——纯 Postgres
读写只需要既有 DSN，两份静态映射随包发布、无需任何密钥；**刻意不接**令牌
密文读取口（需要只活在 scheduler 进程里的 MCP 加密主密钥）——为一个纵深字段
把钥匙也接进暴露在飞书长连接的 gateway 进程不成比例，固定传
``token_cipher=None``，真撞上时 ``ValueError`` 原样冒泡按降级回每日批处理；
这条系统边界选择留给产品/架构另行评估。

:class:`BackgroundPermissionRecomputeTrigger` 把同步触发包成"提交即返回、后台
单线程串行执行"，避免同步调用拖慢飞书卡片应答；完整取舍见该类自己的 docstring。
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
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
from lingxi.core.permission.targeted_recompute import (
    AuditSink,
    RecomputeKind,
    TargetedPermissionRecompute,
    TargetedRecomputeOutcome,
)


class _BaselineIdentityLookup:
    """把全量结果适配成按单个 ``user_id`` 查询的形状。

    包装 ``PostgresRosterBaselineReader.load_active_baseline()``，不新增 SQL，
    只做客户端过滤。定向重算只在罕见的管理员点击时触发，不在任何高频路径上；
    一次全表扫描换来"不用再写、再维护第二条口径必须逐字保持一致的 SQL"，
    这笔账划算。
    """

    def __init__(self, baseline: Sequence[ArchivedIdentity]) -> None:
        self._by_id = {identity.app_user_id: identity for identity in baseline}

    def find_active(self, *, user_id: str) -> ArchivedIdentity | None:
        return self._by_id.get(user_id)


#: 撤权专用身份查询：与 ``postgres_roster_audit.ACTIVE_BASELINE_SQL`` 取同样五列，
#: 但不要求 ``provisioning_state = 'active'``——停用要服务的是任何还可能有一条
#: 发布内容在外面的人，包括首聊开通到一半就被停用的那个。仍然排除已删除账号：
#: 那些行的内容已经随删除流程清理。理由全文见
#: ``core/permission/targeted_recompute.TargetedPermissionRecompute.force_revoke``。
_REVOCATION_IDENTITY_SQL = """
SELECT id, feishu_user_id, display_name, employee_no, email
  FROM app_user
 WHERE id = %s
   AND account_state NOT IN ('deleting', 'deleted')
"""


class _RevocationIdentityLookup:
    """按 ``user_id`` 现查一行，不预载基线。

    撤权是单点动作，一次一个人；预载全表基线在这里没有意义，而"现查"还顺带保证读到
    的是**这一刻**的行（停用写入刚提交，紧接着这一查一定看得到）。
    """

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts) -> None:
        self._dsn = dsn
        self._timeouts = timeouts

    def find_for_revocation(self, *, user_id: str) -> ArchivedIdentity | None:
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(_REVOCATION_IDENTITY_SQL, (user_id,))
            row = cursor.fetchone()
        if row is None:
            return None
        return ArchivedIdentity(
            app_user_id=str(row[0]),
            personnel_id=_identity_text(row[1]),
            display_name=_identity_text(row[2]),
            employee_no=_identity_text(row[3]),
            email=_identity_text(row[4]),
        )


def _identity_text(value: object) -> str:
    """``NULL`` 与空白归一成空串——与 ``adapters/postgres_roster_audit._text`` 同口径。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


class _RosterRowsAdapter:
    def __init__(self, store: Any) -> None:
        self._store = store

    def load_rows(self) -> Sequence[Mapping[str, Any]] | None:
        snapshot = self._store.load()
        return None if snapshot is None else snapshot.rows


def _resolve_target_user_id(
    dsn: str, timeouts: PostgresTimeouts, pending: PendingAction
) -> str | None:
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
        metric_map_path: Path | None,
    ) -> None:
        """``metric_map_path``：「公司+职能→指标名」映射的外置路径。

        由装配层从 ``LINGXI_COMPANY_FUNCTION_METRIC_MAP_PATH`` 读出后注入；
        ``None`` 表示这台机器没配外置文件，落回随包默认。**刻意没有默认值**：
        本类翻译出来的指标集合会被直接发布成用户的真实权限范围，一个"忘了传"
        的默认值就是双真相本身。
        """
        self._dsn = dsn
        self._timeouts = timeouts
        self._audit = audit
        self._metric_map_path = metric_map_path

    def _build_recompute(
        self, *, role_function_map: Any, metric_translation_map: Any
    ) -> TargetedPermissionRecompute:
        """用已加载的两份静态映射，装配一次 :class:`TargetedPermissionRecompute`。

        延迟导入：与仓库既有的 Postgres 适配器同一惯例（构造时不连接数据库，
        调用时才建连接），也让"哪些依赖真的被这条调用路径用到"在 import
        时机上一目了然。
        """
        from lingxi.adapters.postgres_galaxy_snapshot import PostgresGalaxySnapshotReader
        from lingxi.adapters.postgres_local_permission import (
            PostgresLocalPermissionOverrideStore,
            local_override_reader,
        )
        from lingxi.adapters.postgres_permission_publish import PostgresPermissionPublishStore
        from lingxi.adapters.postgres_roster_audit import PostgresRosterBaselineReader
        from lingxi.adapters.postgres_roster_snapshot import PostgresRosterSnapshotStore

        baseline = PostgresRosterBaselineReader(
            self._dsn, timeouts=self._timeouts
        ).load_active_baseline()
        publish_store = PostgresPermissionPublishStore(self._dsn, timeouts=self._timeouts)
        return TargetedPermissionRecompute(
            identities=_BaselineIdentityLookup(baseline),
            roster_snapshot=_RosterRowsAdapter(
                PostgresRosterSnapshotStore(self._dsn, timeouts=self._timeouts)
            ),
            galaxy=PostgresGalaxySnapshotReader(self._dsn, timeouts=self._timeouts),
            decisions=publish_store,
            publish_history=publish_store,
            role_function_map=role_function_map,
            metric_translation_map=metric_translation_map,
            audit=self._audit,
            local_overrides=local_override_reader(self._dsn, timeouts=self._timeouts),
            legacy_all_scope=PostgresLocalPermissionOverrideStore(
                self._dsn, timeouts=self._timeouts
            ),
            revocation_identities=_RevocationIdentityLookup(self._dsn, timeouts=self._timeouts),
        )

    def trigger(self, pending: PendingAction) -> TargetedRecomputeOutcome:
        """``card_callback.py`` 在确认执行成功后调用，best-effort。

        本方法自己**不**吞任何异常——吞了调用方就没有机会记"这次触发失败了"
        这条响亮审计。
        """
        from lingxi.adapters.company_function_metric_map_file import (
            load_company_function_metric_map,
        )
        from lingxi.adapters.role_function_map_file import load_role_function_map

        # 两份静态映射先读，读不出来就一次库都不碰、一行权限都不发布：异常原样
        # 冒泡给调用方，由 BackgroundPermissionRecomputeTrigger/card_callback.py
        # 记审计并降级回每日批。不静默回落随包默认映射——放在最前面而不是原来
        # 的构造行里，是为了让"配置坏了"在任何数据库读写之前就失败。
        role_function_map = load_role_function_map()
        metric_translation_map = load_company_function_metric_map(self._metric_map_path)

        user_id = _resolve_target_user_id(self._dsn, self._timeouts, pending)
        if user_id is None:
            self._audit.record(
                "permission_targeted_recompute.target_unresolved",
                pending_action_id=pending.id,
                action_type=pending.action_type.value,
            )
            return TargetedRecomputeOutcome(kind=RecomputeKind.SKIPPED, reason="target_unresolved")

        recompute = self._build_recompute(
            role_function_map=role_function_map, metric_translation_map=metric_translation_map
        )
        if pending.action_type is PendingActionType.SUSPEND_USER:
            return recompute.force_revoke(user_id=user_id)
        return recompute.recompute_and_publish(user_id=user_id)


#: 单工作线程队列容量上下界（模块文档「有界队列 + 丢弃」一节）。
_MIN_QUEUE_MAXSIZE = 1
_MAX_QUEUE_MAXSIZE = 4
_DEFAULT_QUEUE_MAXSIZE = 4

# 确认后给定向重算留出的内部处理窗口。它只控制管理卡上的等待/未完成状态，
# 不改变 pending_action 的确认窗口或本地授权的有效期，也不是产品向管理员承诺的
# 硬上限；超时后日批仍可恢复并留下修正摘要。
_DEFAULT_RECOMPUTE_TIMEOUT_SECONDS = 60.0

#: 与 ``card_callback.py::AdminCardCallbackHandler._trigger_recompute`` 同步
#: 分支使用的字面量逐字相同——运维按这一个动作名检索"这次定向重算触发失败了"，
#: 不需要区分背后到底是同步调用失败还是本类的后台线程执行失败。
_RECOMPUTE_TRIGGER_FAILED_ACTION = "admin.card_callback.recompute_trigger_failed"

#: 有界队列满时的丢弃审计——与上面那条失败审计是两件不同的事（这条从未真正
#: 执行过，谈不上"失败"），动作名因此独立登记，运维可以分辨"这次触发到底是
#: 执行失败了，还是压根没排上队"。
_RECOMPUTE_TRIGGER_DROPPED_ACTION = "admin.card_callback.recompute_trigger_dropped"
_RECOMPUTE_TRIGGER_TIMEOUT_ACTION = "admin.card_callback.recompute_trigger_timeout"


class _ExecutionWatch:
    """让超时回调与后台结果回调互斥，避免超时后迟到结果把卡片改回已生效。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._finished = False
        self._timed_out = False

    def timeout(self) -> bool:
        with self._lock:
            if self._finished:
                return False
            self._timed_out = True
            return True

    def finish(self) -> bool:
        with self._lock:
            if self._finished or self._timed_out:
                return False
            self._finished = True
            return True


class BackgroundPermissionRecomputeTrigger:
    """把任意 ``PermissionRecomputeTrigger`` 实现包成异步执行器。

    生产环境即 :class:`PermissionRecomputeAdapter`；包成"提交即返回、后台
    单线程串行执行"是为了不让同步调用拖慢飞书卡片应答（见模块 docstring）。
    本类只编排排队与执行，不编排也不修改任何定向重算的业务判定——那些规则
    完全在被包装的 ``delegate`` 里。
    """

    def __init__(
        self,
        delegate: Any,
        *,
        audit: AuditSink,
        queue_maxsize: int = _DEFAULT_QUEUE_MAXSIZE,
        on_completed: Callable[[PendingAction], None] | None = None,
        on_queued: Callable[[PendingAction, TargetedRecomputeOutcome], None] | None = None,
        on_failed: Callable[[PendingAction, Exception | None], None] | None = None,
        #: ``SKIPPED`` 专用回调：定向重算的 ``SKIPPED`` 是常态出口，不是故障
        #: （例如管理员对一个已停用用户做本地权限动作）。传入后 ``SKIPPED`` 走
        #: 它并带上完整 outcome；不传（``None``）时行为与本参数加入之前逐字节
        #: 一致，仍回落 ``on_failed(pending, None)``。本类不解释 ``reason``。
        on_skipped: Callable[[PendingAction, TargetedRecomputeOutcome], None] | None = None,
        on_timeout: Callable[[PendingAction], None] | None = None,
        timeout_seconds: float = _DEFAULT_RECOMPUTE_TIMEOUT_SECONDS,
    ) -> None:
        """校验队列容量与超时配置，装配委托对象与各回调，并启动工作线程。"""
        if not _MIN_QUEUE_MAXSIZE <= queue_maxsize <= _MAX_QUEUE_MAXSIZE:
            raise ValueError(
                f"queue_maxsize 必须在 {_MIN_QUEUE_MAXSIZE}~{_MAX_QUEUE_MAXSIZE} 之间"
                "（模块文档「有界队列 + 丢弃」一节：容量存在的意义是扛住短时间内"
                "连续几次点击，不是扛住持续高吞吐）"
            )
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ValueError("timeout_seconds 必须是正数")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须是正数")
        self._delegate = delegate
        self._audit = audit
        self._on_completed = on_completed
        self._on_queued = on_queued
        self._on_failed = on_failed
        self._on_skipped = on_skipped
        self._on_timeout = on_timeout
        self._timeout_seconds = float(timeout_seconds)
        self._queue: queue.Queue[tuple[PendingAction, _ExecutionWatch, threading.Timer]] = (
            queue.Queue(maxsize=queue_maxsize)
        )
        self._worker = threading.Thread(
            target=self._run,
            name="lingxi-gateway-permission-recompute",
            daemon=True,  # 模块文档「daemon 线程」一节。
        )
        self._worker.start()

    def trigger(self, pending: PendingAction) -> None:
        """立即返回：只把这条待确认操作放进队列，真正的重算在后台线程里跑。

        队列已满时丢弃并响亮审计，从不阻塞调用方，也从不向调用方冒泡任何
        异常——``card_callback.py`` 因此不需要跟着改一个字。
        """
        watch = _ExecutionWatch()
        timer = threading.Timer(
            self._timeout_seconds, self._on_timeout_fired, args=(pending, watch)
        )
        timer.daemon = True
        try:
            self._queue.put_nowait((pending, watch, timer))
        except queue.Full:
            self._audit.record(
                _RECOMPUTE_TRIGGER_DROPPED_ACTION,
                pending_action_id=pending.id,
            )
            if self._on_failed is not None:
                try:
                    self._on_failed(pending, None)
                except Exception as error:  # callback must not kill worker
                    self._audit.record(
                        _RECOMPUTE_TRIGGER_FAILED_ACTION,
                        pending_action_id=pending.id,
                        error=type(error).__name__,
                    )
            return
        # Start only after the item owns a queue slot, so a dropped item can never
        # race its timeout callback.  The worker may finish before ``start``; a
        # pre-cancelled Timer is safe and keeps the callback mutually exclusive.
        timer.start()

    def _on_timeout_fired(self, pending: PendingAction, watch: _ExecutionWatch) -> None:
        if not watch.timeout():
            return
        self._audit.record(
            _RECOMPUTE_TRIGGER_TIMEOUT_ACTION,
            pending_action_id=pending.id,
            timeout_seconds=self._timeout_seconds,
        )
        if self._on_timeout is not None:
            try:
                self._on_timeout(pending)
            except Exception as error:  # timeout callback must not kill timer
                self._audit.record(
                    _RECOMPUTE_TRIGGER_FAILED_ACTION,
                    pending_action_id=pending.id,
                    error=type(error).__name__,
                )

    def _handle_trigger_exception(
        self, pending: PendingAction, watch: _ExecutionWatch, error: Exception
    ) -> None:
        """``_delegate.trigger`` 抛出异常时的记账与回调。

        与 card_callback.py 同一条 best-effort 姿态。
        """
        self._audit.record(
            _RECOMPUTE_TRIGGER_FAILED_ACTION,
            pending_action_id=pending.id,
            error=type(error).__name__,
        )
        if watch.finish() and self._on_failed is not None:
            try:
                self._on_failed(pending, error)
            except Exception as callback_error:  # callback must not kill worker
                self._audit.record(
                    _RECOMPUTE_TRIGGER_FAILED_ACTION,
                    pending_action_id=pending.id,
                    error=type(callback_error).__name__,
                )

    def _dispatch_outcome(
        self, pending: PendingAction, watch: _ExecutionWatch, outcome: Any
    ) -> None:
        """``_delegate.trigger`` 正常返回时按结果种类分流到对应回调。

        TargetedPermissionRecompute only records a publish *intent* here;
        ENQUEUED/REVOKED don't mean the external table has read back the
        row, so treat those as waiting, not effective. Only legacy
        delegates without a typed outcome keep the old completed callback.
        """
        typed_outcome = outcome if isinstance(outcome, TargetedRecomputeOutcome) else None
        completed = typed_outcome is None
        queued = typed_outcome is not None and typed_outcome.kind is not RecomputeKind.SKIPPED
        # ``SKIPPED`` 只有在调用方显式登记 ``on_skipped`` 时才单独分流；未登记
        # 时仍走下面那条 ``on_failed`` 老路（见 ``on_skipped`` 参数文档）。
        skipped = typed_outcome is not None and typed_outcome.kind is RecomputeKind.SKIPPED
        if not watch.finish():
            return
        if queued:
            try:
                if self._on_queued is not None:
                    self._on_queued(pending, typed_outcome)  # type: ignore[arg-type]
            except Exception as error:  # callback must not kill worker
                self._audit.record(
                    _RECOMPUTE_TRIGGER_FAILED_ACTION,
                    pending_action_id=pending.id,
                    error=type(error).__name__,
                )
        elif skipped and self._on_skipped is not None:
            try:
                self._on_skipped(pending, typed_outcome)  # type: ignore[arg-type]
            except Exception as error:  # callback must not kill worker
                self._audit.record(
                    _RECOMPUTE_TRIGGER_FAILED_ACTION,
                    pending_action_id=pending.id,
                    error=type(error).__name__,
                )
        else:
            callback = self._on_completed if completed else self._on_failed
            if callback is not None:
                try:
                    if completed:
                        callback(pending)  # type: ignore[misc]
                    else:
                        callback(pending, None)  # type: ignore[misc]
                except Exception as error:  # callback must not kill worker
                    self._audit.record(
                        _RECOMPUTE_TRIGGER_FAILED_ACTION,
                        pending_action_id=pending.id,
                        error=type(error).__name__,
                    )

    def _run(self) -> None:
        """工作线程主体：按入队顺序串行执行，单条失败只记审计、不影响下一条。"""
        while True:
            pending, watch, timer = self._queue.get()
            try:
                outcome = self._delegate.trigger(pending)
            except Exception as error:
                self._handle_trigger_exception(pending, watch, error)
            else:
                self._dispatch_outcome(pending, watch, outcome)
            finally:
                timer.cancel()
                self._queue.task_done()


__all__ = ["BackgroundPermissionRecomputeTrigger", "PermissionRecomputeAdapter"]
