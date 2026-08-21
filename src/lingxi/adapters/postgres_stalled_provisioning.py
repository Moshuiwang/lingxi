"""开通中途停摆收口的候选查询（Issue #282，[V-开通-19](../../../docs/技术设计/验收矩阵.md)）。

## 只做候选查询，收口写入复用既有方法

本模块**只**提供「谁停摆了」的候选查询。真正把用户收口成 ``aborted`` 的写入复用
:meth:`~lingxi.adapters.postgres_identity.PostgresAppUserStore.abort_stalled_provisioning`
——那是首次开通编排「当场收口」（``core/identity/onboarding_runner.py`` §7.4）与本职责
「租约到期收口」共用的**同一个**方法，不在这里另写一份 CAS。两个调用方触发判据不同
（前者是"已经确定失败终态"，后者是"认领已经超过租约、太久没有任何进展"），但"什么条件
下允许把一个用户判定为『这次开通结束了、没有成功』"这件事只有一处真相来源。

## 候选判据：认领时间 + 超时租约，不是状态列

``pipeline.py`` 那条「这一条正在 scheduler 里跑」的假设当前零校验（Issue #282 正文）。
需要的存活信号已经在库里，不需要新表新列：

- ``inbound_event.onboarding_dispatched_at``（迁移 ``0062``）= **这一次认领的代次**，
  在 ``claim_stale_onboarding`` 取出的同一条 ``UPDATE`` 里写上，天然是租约起点。
- ``inbound_event`` 行**永不删除**：``0054_retention_cleanup.py`` 的清理函数只 DELETE
  ``galaxy_import_batch`` 与 ``feishu_org_sync_run`` 两张表，``inbound_event.expires_at``
  由触发器算出但从未被任何清理消费。⇒ 租约起点是持久事实，不会被清理链偷走。

## 与既有三个恢复/对账职责的边界（不重叠、不抢活）

| 职责 | 候选判据 | 与本职责的交集 |
|---|---|---|
| ``core/conversation/onboarding_recovery.OnboardingReconciler``（未开通首聊交接对账） | 逐**事件**判定：``inbound_event.handled_as='auto_provisioning'`` **AND** ``onboarding_dispatched_at IS NULL``，完全不看 ``app_user`` | **在"该用户最新一条事件"这个粒度上互斥，不是任意历史事件的粒度**（外部独立审查 P2-5 修复，见 :meth:`PostgresStalledProvisioningStore.stalled_provisioning_candidates` 判据 3 的完整推演）：本职责只取该用户**最新**一条事件、且只在那一条的 ``onboarding_dispatched_at`` 非空且已超租约时才选中他；对方只挑 ``onboarding_dispatched_at IS NULL`` 的事件。同一条事件不可能同时满足"非空且超租约"与"为空"，因此两者不会同时认领处理**同一条事件**。这不等于"两个查询永远互不相关"——一个用户名下可能同时存在多条历史事件，本职责刻意只看最新那一条，正是为了不在"最新事件已经因为通知失败被 `AutoOnboardingRunner._release_for_notify` 放回、正等着被对方重新认领"这个窗口里，误把一条更早的历史认领当成"现在的"认领时刻。 |
| ``apps/scheduler/late_readiness_recovery.LateReadinessRecoveryDuty``（迟到就绪恢复，V-开通-18） | ``provisioning_state='mcp_syncing'`` **AND** ``EXISTS(mcp_sync_check.result='timed_out')`` | **空集，由 ``NOT EXISTS(... timed_out)`` 保证**：两个候选查询按同一个子查询取互补，对任意用户不可能同时命中。 |
| ``apps/scheduler/retention``（保留清理、空闲会话清理） | 只 DELETE ``galaxy_import_batch``/``feishu_org_sync_run``；空闲会话与开通无关 | 空集。 |
| （本职责）:class:`~lingxi.apps.scheduler.stalled_provisioning.StalledProvisioningDuty` | ``provisioning_state IN ('provisioning','mcp_syncing')`` AND ``account_state='enabled'`` AND 认领超租约 AND（``mcp_syncing`` 支）``NOT EXISTS timed_out`` | — |

## 幂等与竞态：候选被选中的同时那条链恰好还活着

四道独立防线，任意一道单独失效都不会造成用户可见伤害（详见
:mod:`lingxi.apps.scheduler.stalled_provisioning` 模块文档「幂等」一节的完整推演）：
租约必须长于链预算（装配断言
:func:`~lingxi.apps.scheduler.onboarding.assert_stalled_lease_exceeds_chain_budget`）、
本模块**不释放认领**（``onboarding_dispatched_at`` 保持非空 ⇒ 老事件结构上不可能被
第二次认领）、收口写入的 CAS 只接受 ``provisioning``/``mcp_syncing`` 且账号启用
（永远不可能覆盖 ``active``）、编排自己的进程内去重（按 ``open_id``）。
"""

from __future__ import annotations

from dataclasses import dataclass

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect


@dataclass(frozen=True)
class StalledProvisioningCandidate:
    """一个「认领已经超过租约、仍卡在 ``provisioning``/``mcp_syncing``」的收口候选。"""

    user_id: str
    open_id: str
    event_id: str
    trace_id: str
    provisioning_state: str


class PostgresStalledProvisioningStore:
    """候选查询的只读口。构造时不连接数据库，每次调用自带连接（adapters 既有惯例）。"""

    def __init__(
        self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS
    ) -> None:
        self._dsn = dsn
        self._timeouts = timeouts

    def stalled_provisioning_candidates(
        self, *, lease_seconds: int, limit: int = 50
    ) -> tuple[StalledProvisioningCandidate, ...]:
        """取「认领超过租约、仍停在中途格」的停摆候选。

        五条筛选缺一不可（模块文档「与既有三个恢复/对账职责的边界」一节的可执行形式）：

        1. ``u.provisioning_state IN ('provisioning', 'mcp_syncing')``——只看两个中途格；
        2. ``u.account_state = 'enabled'``——与收口写入的 CAS 守卫口径一致，不选一个
           CAS 注定会拒绝的候选；
        3. 取**该 open_id 最新一条 ``auto_provisioning`` 事件**（``ORDER BY
           received_at DESC LIMIT 1`` 的横向子查询，**不**按认领状态过滤）：
           必须先拿到"这个人最后一次相关事件到底是哪一条"这个事实，再判它有没有被
           认领、认领了多久——外部独立审查 P2-5 修复：此前的写法在子查询里就先过滤
           ``onboarding_dispatched_at IS NOT NULL``，等于默认"最新一条事件一定已经
           被认领"。真实反例：一条事件被认领后因为通知发不出去被
           ``_release_for_notify`` 放回（``onboarding_dispatched_at`` 变回
           ``NULL``，等待 ``OnboardingReconciler`` 重新认领），此时如果这个人名下
           还有一条**更早**、认领时间很久以前的历史事件，旧写法会跳过刚被放回的这条
           （因为它现在是 NULL），转而捞到那条历史事件、拿它早已过期的认领时刻当
           "现在的租约起点"——于是一条**正在被对账重新认领的正常链**被误判成
           已经停摆 45 分钟的僵尸。修法是子查询不做认领状态过滤，只按"最新"取一条；
           那一条如果还没被认领（``onboarding_dispatched_at IS NULL``），下面第 4 条
           判据天然为假（``NULL < ...`` 在 SQL 里恒不成立），本职责就不会选中他——
           这个窗口正确地留给 ``OnboardingReconciler`` 处理，不是本职责该抢的活；
        4. ``onboarding_dispatched_at IS NOT NULL AND onboarding_dispatched_at <
           now() - lease``——到期判据，**必须先判非空**（同一条修复：`NULL` 参与比较
           结果是 `NULL`，SQL 里天然被当假处理，这里显式写出来只是为了让判据在文档
           里可读，不依赖隐式转换的读者自己推一遍）；
        5. ``NOT EXISTS(... result = 'timed_out')``——与迟到就绪恢复职责（V-开通-18）
           的候选集合互补，同时不去碰 ``V-开通-18`` 已经认领的那条恢复路径。

        ``ORDER BY e.onboarding_dispatched_at`` 让最先超期的候选先被处理，与
        ``late_onboarding_recovery_candidates`` 同一条纪律。
        """

        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds < 1:
            raise ValueError("租约必须是正整数秒")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT u.id, u.feishu_open_id, e.feishu_event_id, e.trace_id,
                       u.provisioning_state
                  FROM app_user u
                  JOIN LATERAL (
                         SELECT feishu_event_id, trace_id, onboarding_dispatched_at
                           FROM inbound_event
                          WHERE user_open_id = u.feishu_open_id
                            AND handled_as = 'auto_provisioning'
                          ORDER BY received_at DESC
                          LIMIT 1
                       ) e ON TRUE
                 WHERE u.provisioning_state IN ('provisioning', 'mcp_syncing')
                   AND u.account_state = 'enabled'
                   AND e.onboarding_dispatched_at IS NOT NULL
                   AND e.onboarding_dispatched_at < now() - make_interval(
                           secs => %(lease)s::double precision
                       )
                   AND NOT EXISTS (
                         SELECT 1 FROM mcp_sync_check t
                          WHERE t.user_id = u.id
                            AND t.permission_version = u.permission_version
                            AND t.result = 'timed_out'
                       )
                 ORDER BY e.onboarding_dispatched_at
                 LIMIT %(limit)s
                """,
                {"lease": lease_seconds, "limit": limit},
            )
            rows = cursor.fetchall()
        return tuple(
            StalledProvisioningCandidate(
                user_id=str(row[0]),
                open_id=str(row[1]),
                event_id=str(row[2]),
                trace_id=str(row[3]),
                provisioning_state=str(row[4]),
            )
            for row in rows
        )


__all__ = ["PostgresStalledProvisioningStore", "StalledProvisioningCandidate"]
