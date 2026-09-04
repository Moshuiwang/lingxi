"""开通中途停摆收口的候选查询。

本模块只提供"谁停摆了"的候选查询；真正把用户收口成 ``aborted`` 的写入复用
:meth:`~lingxi.adapters.postgres_identity.PostgresAppUserStore.
abort_stalled_provisioning`——首次开通编排的"当场收口"与本职责的"租约到期收口"
共用同一个方法，判定"这次开通结束了、没有成功"只有一处真相来源。

候选判据是**认领时间 + 超时租约**，不是状态列：租约起点取自
``inbound_event.onboarding_dispatched_at``（该行永不删除，是持久事实）；预开通
没有 ``inbound_event`` 行的用户改取 ``provisioning_started_at`` 兜底。与另外两个
恢复/对账职责（``OnboardingReconciler``、``LateReadinessRecoveryDuty``）候选集合
的互斥关系、认领竞态下的幂等防线，论证与反例见
:meth:`PostgresStalledProvisioningStore.stalled_provisioning_candidates` 与
``tests/test_postgres_stalled_provisioning.py``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.identity.preprovision import SYSTEM_EVENT_PREFIX


@dataclass(frozen=True)
class StalledProvisioningCandidate:
    """一个「认领已经超过租约、仍卡在 ``provisioning``/``mcp_syncing``」的收口候选。"""

    user_id: str
    open_id: str
    event_id: str
    trace_id: str
    provisioning_state: str


#: 五条筛选缺一不可：状态在两个中途格；账号 enabled（与收口写入的 CAS 守卫口径
#: 一致）；取该 open_id **最新**一条 auto_provisioning 事件且不按认领状态过滤
#: （避免把"最新事件正被 OnboardingReconciler 重新认领"误判成停摆，见
#: test_a_released_latest_claim_hides_an_older_expired_one）；租约起点非空且已
#: 超期；与迟到就绪恢复职责的候选集合互补。

# 预开通用户没有 inbound_event 行（见 core.identity.preprovision），LEFT JOIN
# LATERAL 取不到行时改用 provisioning_started_at 作租约起点，不用 updated_at
# 兜底（会被任何无关更新刷新、租约永远不到期）；合成的 preprovision:<user_id>
# 只作通知去重键与审计追溯号，不回写任何表的事件列。
_STALLED_PROVISIONING_CANDIDATES_SQL = """
    SELECT u.id, u.feishu_open_id,
           COALESCE(e.feishu_event_id, %(synthetic)s || u.id),
           COALESCE(e.trace_id, %(synthetic)s || u.id),
           u.provisioning_state
      FROM app_user u
      LEFT JOIN LATERAL (
             SELECT feishu_event_id, trace_id, onboarding_dispatched_at
               FROM inbound_event
              WHERE user_open_id = u.feishu_open_id
                AND handled_as = 'auto_provisioning'
              ORDER BY received_at DESC
              LIMIT 1
           ) e ON TRUE
      CROSS JOIN LATERAL (
             SELECT CASE
                      WHEN e.feishu_event_id IS NULL
                      THEN u.provisioning_started_at
                      ELSE e.onboarding_dispatched_at
                    END AS lease_start
           ) s
     WHERE u.provisioning_state IN ('provisioning', 'mcp_syncing')
       AND u.account_state = 'enabled'
       AND s.lease_start IS NOT NULL
       AND s.lease_start < now() - make_interval(
               secs => %(lease)s::double precision
           )
       AND NOT EXISTS (
             SELECT 1 FROM mcp_sync_check t
              WHERE t.user_id = u.id
                AND t.permission_version = u.permission_version
                AND t.result = 'timed_out'
           )
     ORDER BY s.lease_start
     LIMIT %(limit)s
    """


def _validate_candidate_query_params(lease_seconds: int, limit: int) -> None:
    """租约秒数与返回条数上限均须为正整数（非 ``bool``）。"""
    if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds < 1:
        raise ValueError("租约必须是正整数秒")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit 必须是正整数")


def _row_to_candidate(row: Any) -> StalledProvisioningCandidate:
    return StalledProvisioningCandidate(
        user_id=str(row[0]),
        open_id=str(row[1]),
        event_id=str(row[2]),
        trace_id=str(row[3]),
        provisioning_state=str(row[4]),
    )


class PostgresStalledProvisioningStore:
    """候选查询的只读口。构造时不连接数据库，每次调用自带连接（adapters 既有惯例）。"""

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        """记下 DSN 与超时配置；不在构造时连接数据库。"""
        self._dsn = dsn
        self._timeouts = timeouts

    def stalled_provisioning_candidates(
        self, *, lease_seconds: int, limit: int = 50
    ) -> tuple[StalledProvisioningCandidate, ...]:
        """取「认领超过租约、仍停在中途格」的停摆候选。

        五条筛选条件见 :data:`_STALLED_PROVISIONING_CANDIDATES_SQL` 旁的注释；
        ``ORDER BY`` 租约起点让最先超期的候选先被处理，与
        ``late_onboarding_recovery_candidates`` 同一条纪律。判据背后的竞态反例
        与两个恢复职责互斥关系的证明，见
        ``tests/test_postgres_stalled_provisioning.py``。
        """
        _validate_candidate_query_params(lease_seconds, limit)
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                _STALLED_PROVISIONING_CANDIDATES_SQL,
                {"lease": lease_seconds, "limit": limit, "synthetic": SYSTEM_EVENT_PREFIX},
            )
            rows = cursor.fetchall()
        return tuple(_row_to_candidate(row) for row in rows)


__all__ = ["PostgresStalledProvisioningStore", "StalledProvisioningCandidate"]
