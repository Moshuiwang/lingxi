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
``PermissionRecomputeAdapter.trigger`` 会让 ``ValueError`` 原样冒泡，调用方
（``BackgroundPermissionRecomputeTrigger`` 或 ``card_callback.py`` 的
best-effort 包裹，取决于是否接了下面这层异步执行器）按「降级回每日批」处理。
**这条选择改变的是系统边界（哪个进程持有哪把密钥），本卡不擅自扩大，本模块也
因此明确排除了这一角，留给产品/架构在需要时另行评估。**

## ``BackgroundPermissionRecomputeTrigger``：把同步触发包成"提交即返回"（Trace #445 opus 审查坐实并修复）

``card_callback.py::AdminCardCallbackHandler._trigger_recompute`` 在
``handle()`` 的主路径里**同步**调用注入的 ``recompute_trigger.trigger(pending)``
——而 ``PermissionRecomputeAdapter.trigger`` 内部有五到六次网络往返的 Postgres
查询/写入（花名册基线、花名册快照、银河快照、权限发布表读写、本地覆盖读取），
一旦数据库这一刻抖动变慢，管理员点确认按钮之后就要一直等到这整条重算链路跑完
才能收到卡片应答——``handle()`` 的返回值就是飞书要的应答帧（``card_callback.py``
模块文档「载体 #96」），这条延迟直接体现为飞书卡片按钮转圈。定向重算是「即时
生效」这一层纵深，每日批本来就是保底，没有理由让它的延迟拖累回调应答本身。

**为什么在这里包一层，不让 ``card_callback.py`` 自己起线程**：``card_callback.py``
只依赖注入的 ``PermissionRecomputeTrigger`` Protocol 端口，不该知道"这个端口的
真实实现要不要异步执行"这种装配层细节——``_trigger_recompute`` 的 EXECUTED-only、
幂等去重判据一个字都不改（仍然只在这次点击首次让操作执行成功时调用一次
``trigger()``），本类只是装配层塞进另一层的 ``PermissionRecomputeTrigger``
实现：``card_callback.py`` 看到的仍然是"调用 ``trigger()``，失败会抛异常"这同一个
契约，只是这次 ``trigger()`` 本身从不冒泡异常（只做入队）——真正可能失败的执行
在后台线程里跑，用与 ``card_callback.py`` 同一个动作名/字段形状记一条审计（见
:meth:`_run`），运维检索方式不需要跟着改。

**有界队列 + 丢弃，不是无界排队**：单工作线程按入队顺序串行执行——真实数据库
连接不该被并发重算请求以不可控并发数打满。队列容量固定在 1~4 之间（默认 4）：
管理员确认卡片是低频人工操作，正常情况下队列几乎总是空的，容量存在的意义是
"扛住短时间内连续几次点击"，不是"扛住持续高吞吐"。真的堆满时选择**丢弃并响亮
审计**，不是无界增长——无界队列会把"数据库变慢"变成"进程内存持续增长直到
OOM"，且被丢弃的这一条重算本来就有每日批兜底，不丢反而更不安全。

**daemon 线程**：与 gateway 进程既有的两条投递消费线程同一姿态
（``apps/gateway/__init__.py`` 的 ``delivery_thread``/``document_delivery_
thread`` 均 ``daemon=True``）——进程收到停机信号时不应该被一条卡在数据库调用里
的后台重算线程拖住退出，`V-部署-03` 的停机预算只覆盖长连接与两条投递消费循环，
本类新增的这条后台线程不参与那份预算记账——它本来就是"尽力而为"的纵深，不是
必须完成才能安全退出的在途工作（真正的业务写入已经在 ``confirm()``/``cancel()``
那次数据库事务里落定，重算只是让结果更快对外可见）。
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Callable, Mapping, Sequence

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
from lingxi.core.permission.targeted_recompute import (
    AuditSink,
    RecomputeKind,
    TargetedPermissionRecompute,
    TargetedRecomputeOutcome,
)


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

    def trigger(self, pending: PendingAction) -> TargetedRecomputeOutcome:
        """``card_callback.py`` 在确认执行成功后调用，best-effort（异常由调用方
        捕获并降级，见该模块「载体 #96」旁的执行成功钩子）。本方法自己**不**吞
        任何异常——吞了调用方就没有机会记"这次触发失败了"这条响亮审计。
        """

        # 延迟导入：与仓库既有的 Postgres 适配器同一惯例（构造时不连接数据库，
        # 调用时才建连接），也让"哪些依赖真的被这条调用路径用到"在 import 时机
        # 上一目了然。
        from lingxi.adapters.company_function_metric_map_file import load_company_function_metric_map
        from lingxi.adapters.postgres_galaxy_snapshot import PostgresGalaxySnapshotReader
        from lingxi.adapters.postgres_local_permission import (
            PostgresLocalPermissionOverrideStore,
            local_override_reader,
        )
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
            return TargetedRecomputeOutcome(kind=RecomputeKind.SKIPPED, reason="target_unresolved")

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
            legacy_all_scope=PostgresLocalPermissionOverrideStore(self._dsn, timeouts=self._timeouts),
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
    """把任意 ``PermissionRecomputeTrigger`` 实现（生产环境即
    :class:`PermissionRecomputeAdapter`）包成"提交即返回、后台单线程串行执行"
    的异步执行器（Trace #445 opus 审查坐实并修复）。完整取舍见模块文档
    「``BackgroundPermissionRecomputeTrigger``」一节；本类只编排排队与执行，
    不编排也不修改任何定向重算的业务判定——那些规则完全在被包装的
    ``delegate`` 里。
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
        #: ``SKIPPED`` 专用回调（Trace #521 F5，#493 P1-3）。定向重算的 ``SKIPPED``
        #: 是**常态出口**，不是故障——最典型的一种是管理员对一个已停用用户做本地
        #: 权限动作（``account_not_enabled``）。此前它与真正的执行失败共用
        #: ``on_failed``，调用方拿不到 ``reason``，只能渲染同一句"将在次日批处理
        #: 修正"，而停用用户根本不进日批遍历集合，那句话是假承诺。传入这个回调后
        #: ``SKIPPED`` 走它并带上完整 outcome，调用方自己决定怎么如实告知；
        #: **不传（``None``）时行为与本参数加入之前逐字节一致**——仍然回落
        #: ``on_failed(pending, None)``。本类不解释任何 ``reason``，只负责分流。
        on_skipped: Callable[[PendingAction, TargetedRecomputeOutcome], None] | None = None,
        on_timeout: Callable[[PendingAction], None] | None = None,
        timeout_seconds: float = _DEFAULT_RECOMPUTE_TIMEOUT_SECONDS,
    ) -> None:
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
        self._queue: queue.Queue[tuple[PendingAction, _ExecutionWatch, threading.Timer]] = queue.Queue(
            maxsize=queue_maxsize
        )
        self._worker = threading.Thread(
            target=self._run,
            name="lingxi-gateway-permission-recompute",
            daemon=True,  # 模块文档「daemon 线程」一节。
        )
        self._worker.start()

    def trigger(self, pending: PendingAction) -> None:
        """立即返回：只把这条待确认操作放进队列，真正的重算在后台线程里跑
        （:meth:`_run`）。队列已满时丢弃并响亮审计，从不阻塞调用方，也从不
        向调用方冒泡任何异常——``card_callback.py`` 因此不需要跟着改一个字。
        """

        watch = _ExecutionWatch()
        timer = threading.Timer(self._timeout_seconds, self._on_timeout_fired, args=(pending, watch))
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
                except Exception as error:  # noqa: BLE001 - callback must not kill worker
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
            except Exception as error:  # noqa: BLE001 - timeout callback must not kill timer
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
            except Exception as error:  # noqa: BLE001 - 与 card_callback.py 同一条 best-effort 姿态
                self._audit.record(
                    _RECOMPUTE_TRIGGER_FAILED_ACTION,
                    pending_action_id=pending.id,
                    error=type(error).__name__,
                )
                if watch.finish() and self._on_failed is not None:
                    try:
                        self._on_failed(pending, error)
                    except Exception as callback_error:  # noqa: BLE001
                        self._audit.record(
                            _RECOMPUTE_TRIGGER_FAILED_ACTION,
                            pending_action_id=pending.id,
                            error=type(callback_error).__name__,
                        )
            else:
                # ``TargetedPermissionRecompute`` only records a publish *intent* here;
                # ``ENQUEUED``/``REVOKED`` do not mean the external permission table has
                # accepted and read back the row.  Treat those outcomes as waiting, not
                # effective.  ``UNCHANGED`` is also left to the status observer: an old
                # in-flight intent may still be pending.  Only legacy delegates that do
                # not return a typed outcome retain the historical completed callback.
                typed_outcome = (
                    outcome if isinstance(outcome, TargetedRecomputeOutcome) else None
                )
                completed = typed_outcome is None
                queued = typed_outcome is not None and typed_outcome.kind is not RecomputeKind.SKIPPED
                # ``SKIPPED`` 只有在调用方**显式登记** ``on_skipped`` 时才单独分流；
                # 未登记时仍走下面那条 ``on_failed`` 老路（见 ``on_skipped`` 文档）。
                skipped = typed_outcome is not None and typed_outcome.kind is RecomputeKind.SKIPPED
                if watch.finish():
                    if queued:
                        try:
                            if self._on_queued is not None:
                                self._on_queued(pending, typed_outcome)  # type: ignore[arg-type]
                        except Exception as error:  # noqa: BLE001 - callback must not kill worker
                            self._audit.record(
                                _RECOMPUTE_TRIGGER_FAILED_ACTION,
                                pending_action_id=pending.id,
                                error=type(error).__name__,
                            )
                    elif skipped and self._on_skipped is not None:
                        try:
                            self._on_skipped(pending, typed_outcome)  # type: ignore[arg-type]
                        except Exception as error:  # noqa: BLE001 - callback must not kill worker
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
                            except Exception as error:  # noqa: BLE001 - callback must not kill worker
                                self._audit.record(
                                    _RECOMPUTE_TRIGGER_FAILED_ACTION,
                                    pending_action_id=pending.id,
                                    error=type(error).__name__,
                                )
            finally:
                timer.cancel()
                self._queue.task_done()


__all__ = ["BackgroundPermissionRecomputeTrigger", "PermissionRecomputeAdapter"]
