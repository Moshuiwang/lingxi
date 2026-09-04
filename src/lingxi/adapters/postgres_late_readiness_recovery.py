"""迟到就绪恢复的持久化：候选查询、同事务推进 + 排通知、通知 outbox 的 claim / complete / purge。

三条硬约束：**F1** ``activate_after_late_readiness`` 把状态推进到 ``active``
与排一条待发通知放进**同一个数据库事务**——两者要么一起成立、要么一起不成立
（此前先推进状态、再同步发通知，中途任何失败都会让用户永久退出候选集却收不到
「开通完成」）；通知本身由 :meth:`claim_one_due_notice`/:meth:`mark_notice_delivered`/
:meth:`mark_notice_failed` 这条持久 outbox 重试到送达为止。**F2** 候选查询不再
判断"历史上是否出现过一次 ready"——那会被停用后重新启用的账号永久命中，跳过
当前探针直接写 active。**F3** 推进用的 ``UPDATE`` 带精确匹配
``permission_version``：候选查到之后、真正推进之前，账号可能已被推进到新版本，
CAS 失败时不写任何东西、不排任何通知。

模块住在 ``adapters/`` 且跨读跨写多张表是刻意的例外：候选判定天然需要三张表
联合判断，拆成多个模块各读一截反而让"哪条筛选挡住了谁"不可审查。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.ids import new_id

_UTC = UTC

#: 通知认领时的退避节奏：5 分钟起步，封顶一小时。认领本身就是记账，见
#: :meth:`PostgresLateReadinessStore.claim_one_due_notice` 的文档。
NOTICE_BACKOFF_STEP_SECONDS = 300
NOTICE_BACKOFF_CEILING_SECONDS = 3600


@dataclass(frozen=True)
class LateOnboardingCandidate:
    """一个「首次开通已经超时、仍卡在 ``mcp_syncing``」的恢复候选。

    ``next_attempt_no`` 是"这个人这一版权限已经判定过几次 + 1"，真实序号仍由
    数据库落库时重新算一遍，这里只是让审计里的次序好看。**不再有
    ``already_ready`` 字段**：候选一律重新探一次，"探过 ready 但没推进"这种
    中途态已经被同事务推进消灭。
    """

    user_id: str
    permission_version: int
    permissions: str
    next_attempt_no: int
    #: 这条链是不是系统触发（预开通）：判据与 ``postgres_stalled_provisioning``
    #: 的候选查询同一条线——该用户名下没有 ``handled_as = 'auto_provisioning'``
    #: 的入站事件。为真时恢复完成不发「开通完成」私聊（预开通全程静默、首聊时
    #: 才补一句），改在激活的同一个事务里挂起首聊补一句；用户自己发起的链
    #: 一字不变。
    system_triggered: bool = False


@dataclass(frozen=True)
class StoredCompletionNotice:
    """一条被认领到的待发「开通完成」通知，供 ``apps.scheduler.late_readiness_recovery`` 发送。"""

    notice_id: str
    user_id: str
    permission_version: int
    company_name: str
    function_name: str
    dedupe_key: str


#: 五条筛选缺一不可：状态在 mcp_syncing 且账号 enabled；已发布且 reason/
#: permission_version 对齐当前这一版；EXISTS timed_out（真的判过超时）；
#: progress.last_started_at 到期判据（节奏由调用方给的 recovery_interval_
#: seconds 决定）；payload ? 'permissions'（九十天到期擦除后会变 '{}'）。
_LATE_ONBOARDING_CANDIDATES_SQL = """
SELECT u.id, u.permission_version, o.payload ->> 'permissions',
       progress.attempt_count,
       NOT EXISTS (
             SELECT 1 FROM inbound_event ie
              WHERE ie.user_open_id = u.feishu_open_id
                AND ie.handled_as = 'auto_provisioning'
           ) AS system_triggered
  FROM app_user u
  JOIN publish_outbox o
    ON o.user_id = u.id
   AND o.permission_version = u.permission_version
  JOIN LATERAL (
         SELECT count(*) AS attempt_count,
                max(c.started_at) AS last_started_at
           FROM mcp_sync_check c
          WHERE c.user_id = u.id
            AND c.permission_version = u.permission_version
       ) progress ON TRUE
 WHERE u.provisioning_state = 'mcp_syncing'
   AND u.account_state = 'enabled'
   AND o.status = 'published'
   AND o.reason = %(reason)s
   AND o.payload ? 'permissions'
   AND EXISTS (
         SELECT 1 FROM mcp_sync_check t
          WHERE t.user_id = u.id
            AND t.permission_version = u.permission_version
            AND t.result = 'timed_out'
       )
   AND progress.last_started_at <= now() - make_interval(
           secs => %(interval)s::double precision
       )
 ORDER BY progress.last_started_at
 LIMIT %(limit)s
"""


def _validate_candidate_query_params(
    reason: str, recovery_interval_seconds: int, limit: int
) -> None:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("必须指明本调用方负责恢复哪一类发布意图")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit 必须是正整数")
    if (
        isinstance(recovery_interval_seconds, bool)
        or not isinstance(recovery_interval_seconds, int)
        or recovery_interval_seconds < 1
    ):
        raise ValueError("复检节奏必须是正整数秒")


def _validate_activation_params(
    user_id: str,
    expected_permission_version: int,
    company_name: str,
    function_name: str,
    dedupe_key: str,
) -> None:
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("推进开通完成必须指明用户")
    if (
        isinstance(expected_permission_version, bool)
        or not isinstance(expected_permission_version, int)
        or expected_permission_version <= 0
    ):
        raise ValueError("期望的权限版本必须是正整数")
    if not isinstance(company_name, str) or not company_name:
        raise ValueError("公司展示文本不得为空")
    if not isinstance(function_name, str) or not function_name:
        raise ValueError("职能展示文本不得为空")
    if not isinstance(dedupe_key, str) or not dedupe_key.strip():
        raise ValueError("通知去重键不得为空")


def _row_to_late_onboarding_candidate(row: Any) -> LateOnboardingCandidate:
    return LateOnboardingCandidate(
        user_id=str(row[0]),
        permission_version=int(row[1]),
        permissions=str(row[2]),
        next_attempt_no=int(row[3]) + 1,
        system_triggered=bool(row[4]),
    )


class PostgresLateReadinessStore:
    """V-开通-18 恢复路径的读写口。

    构造时不连接数据库，每次调用自带连接（adapters 既有惯例）。
    """

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        """记下 DSN 与超时配置；不在构造时连接数据库。"""
        self._dsn = dsn
        self._timeouts = timeouts

    # ------------------------------------------------------------------
    # 候选查询
    # ------------------------------------------------------------------

    def late_onboarding_recovery_candidates(
        self, *, reason: str, recovery_interval_seconds: int, limit: int = 50
    ) -> tuple[LateOnboardingCandidate, ...]:
        """取「首次开通已经判过 ``timed_out``、人还停在 ``mcp_syncing``」的恢复候选。

        这是恢复路径的每轮 tick 输入。``reason`` 是必填的：调用方必须显式说出
        自己在找哪一类意图，不给默认值——默认值会在新增一种 ``reason`` 时静默
        地把恢复语义套用到不该套用的意图上。五条筛选判据见
        :data:`_LATE_ONBOARDING_CANDIDATES_SQL` 旁的注释；判据 1 必须与
        :meth:`activate_after_late_readiness` 的 CAS 守卫一致，否则会出现
        "候选选中了、CAS 却总是拒绝"的死候选。
        """
        _validate_candidate_query_params(reason, recovery_interval_seconds, limit)
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                _LATE_ONBOARDING_CANDIDATES_SQL,
                {
                    "reason": reason.strip(),
                    "interval": recovery_interval_seconds,
                    "limit": limit,
                },
            )
            rows = cursor.fetchall()
        return tuple(_row_to_late_onboarding_candidate(row) for row in rows)

    # ------------------------------------------------------------------
    # 推进 active + 排通知（同一个事务）
    # ------------------------------------------------------------------

    @staticmethod
    def _arm_preprovision_notice(cursor: Any, *, user_id: str) -> None:
        """预开通：不排私聊，改挂首聊补一句。

        两道守卫与 ``postgres_identity.mark_preprovision_notice_pending`` 一致
        （只挂一次、只挂给从没跟我们说过话的人）；0 行（已挂过/已经在聊）不是
        失败，激活本身照常提交。
        """
        cursor.execute(
            """UPDATE app_user
                  SET preprovision_notice_armed_at = now()
                WHERE id = %(user)s
                  AND preprovision_notice_armed_at IS NULL
                  AND NOT EXISTS (
                        SELECT 1 FROM inbound_event ie
                         WHERE ie.user_open_id = app_user.feishu_open_id
                      )""",
            {"user": user_id},
        )

    @staticmethod
    def _insert_completion_notice(
        cursor: Any,
        *,
        user_id: str,
        permission_version: int,
        company_name: str,
        function_name: str,
        dedupe_key: str,
    ) -> None:
        """插入一条待发「开通完成」通知。

        ``ON CONFLICT (dedupe_key) DO NOTHING`` 让重复调用不会排出第二条。
        """
        cursor.execute(
            """INSERT INTO onboarding_completion_notice
                 (id, user_id, permission_version, company_name, function_name,
                  dedupe_key)
               VALUES (%(id)s, %(user)s, %(version)s, %(company)s, %(function)s,
                       %(dedupe)s)
               ON CONFLICT (dedupe_key) DO NOTHING""",
            {
                "id": new_id("obn"),
                "user": user_id,
                "version": permission_version,
                "company": company_name,
                "function": function_name,
                "dedupe": dedupe_key.strip(),
            },
        )

    def activate_after_late_readiness(
        self,
        *,
        user_id: str,
        expected_permission_version: int,
        company_name: str,
        function_name: str,
        dedupe_key: str,
        silent_system_trigger: bool = False,
    ) -> bool:
        """就绪之后，**同一个事务**里把状态推进到 ``active``、并排一条待发通知。

        ``silent_system_trigger`` 为真（系统触发/预开通）时不排「开通完成」
        私聊，改在同一个事务里挂起首聊补一句（缺省 ``False`` 行为逐字节不变）。
        返回是否真的推进了——``False`` 是 CAS 失败（账号已停用/状态已不是
        ``mcp_syncing``/权限版本已变），此时不写任何东西、也不排任何通知；
        ``permission_version`` 这一条不信任候选行携带的快照值，必须重新核对。
        通知内容在这里一次性快照，与 ``publish_outbox.payload`` 同一条道理。
        """
        _validate_activation_params(
            user_id, expected_permission_version, company_name, function_name, dedupe_key
        )
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    """UPDATE app_user
                          SET provisioning_state = 'active', updated_at = now()
                        WHERE id = %(user)s
                          AND provisioning_state = 'mcp_syncing'
                          AND account_state = 'enabled'
                          AND permission_version = %(expected)s""",
                    {"user": user_id, "expected": expected_permission_version},
                )
                if cursor.rowcount != 1:
                    return False
                if silent_system_trigger:
                    self._arm_preprovision_notice(cursor, user_id=user_id)
                else:
                    self._insert_completion_notice(
                        cursor,
                        user_id=user_id,
                        permission_version=expected_permission_version,
                        company_name=company_name,
                        function_name=function_name,
                        dedupe_key=dedupe_key,
                    )
        return True

    # ------------------------------------------------------------------
    # 通知 outbox：claim / complete / purge
    # ------------------------------------------------------------------

    def claim_one_due_notice(self) -> StoredCompletionNotice | None:
        """认领一条到期的待发通知；``None`` 表示没有到期的。

        **认领即记账**：同一条 ``UPDATE`` 把 ``attempts`` +1、把 ``next_attempt_at``
        前移到 ``now() + LEAST(attempts * 300, 3600)`` 秒——这一步本身就是退避，调用方
        发送失败时不需要再写一次「记一次失败」，下一次到期时间已经在这里定好了。
        ``FOR UPDATE SKIP LOCKED`` 与 ``publish_outbox.claim_next`` 同一条并发纪律
        （本仓库当前单实例假设下不是必需，但同一份代码将来若被多实例复用不会因为
        少了这一层而静默出错，见模块「已知边界」A1 的登记）。
        """
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE onboarding_completion_notice AS n
                       SET attempts = n.attempts + 1,
                           next_attempt_at = now() + make_interval(
                               secs => LEAST(
                                   (n.attempts + 1) * %(step)s, %(ceiling)s
                               )::double precision
                           )
                     WHERE n.id = (
                             SELECT c.id FROM onboarding_completion_notice c
                              WHERE c.status = 'pending' AND c.next_attempt_at <= now()
                              ORDER BY c.created_at, c.id
                              LIMIT 1
                                FOR UPDATE SKIP LOCKED
                           )
                    RETURNING n.id, n.user_id, n.permission_version, n.company_name,
                              n.function_name, n.dedupe_key
                    """,
                    {
                        "step": NOTICE_BACKOFF_STEP_SECONDS,
                        "ceiling": NOTICE_BACKOFF_CEILING_SECONDS,
                    },
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return StoredCompletionNotice(
            notice_id=str(row[0]),
            user_id=str(row[1]),
            permission_version=int(row[2]),
            company_name=str(row[3]),
            function_name=str(row[4]),
            dedupe_key=str(row[5]),
        )

    def mark_notice_delivered(self, notice_id: str) -> None:
        """把一条通知记成已送达（终态）。"""
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "UPDATE onboarding_completion_notice "
                "SET status = 'delivered', delivered_at = now() WHERE id = %s",
                (notice_id,),
            )

    def mark_notice_failed(self, notice_id: str, *, error: str) -> None:
        """记一次没能送达的错误码，**这条通知仍然留在 ``pending``**，按既有退避重试。

        本方法是"收件人暂时查不到"与"飞书发送失败"两种情况共用的落点：账号
        ``account_state`` 被标成停用完全可能是暂时的，分辨不出"暂时查不到"与
        "永久不会再有"就提前判成永久放弃，等于放弃了本该重试的机会——真正
        "这个人不用再等了"只有一种事实来源，即 ``user_id`` 上的
        ``ON DELETE CASCADE``。不改变 ``next_attempt_at``：退避已经在
        :meth:`claim_one_due_notice` 认领时算好了，这里只留痕。
        """
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "UPDATE onboarding_completion_notice SET last_error = %s WHERE id = %s",
                (str(error)[:500], notice_id),
            )

    def purge_expired_notices(self, *, now: datetime | None = None, limit: int = 500) -> int:
        """删除已经送达（``delivered``）且过了九十天上限的通知，返回删除条数。

        ``pending`` 的行永远不在候选范围内——删掉一条还在等待送达的通知，等于
        让一个已经写成 ``active`` 的用户真的永远收不到那句话。到期判据在
        ``content_expires_at``（迁移 ``0066`` 的触发器固定写死），本方法不加
        第二个条件。
        """
        moment = now or datetime.now(_UTC)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("到期判定时间必须带时区")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """DELETE FROM onboarding_completion_notice
                    WHERE id IN (
                            SELECT id FROM onboarding_completion_notice
                             WHERE status = 'delivered'
                               AND content_expires_at <= %s
                             ORDER BY content_expires_at
                             LIMIT %s
                          )""",
                (moment, limit),
            )
            purged = cursor.rowcount
        return purged


__all__ = [
    "LateOnboardingCandidate",
    "NOTICE_BACKOFF_CEILING_SECONDS",
    "NOTICE_BACKOFF_STEP_SECONDS",
    "PostgresLateReadinessStore",
    "StoredCompletionNotice",
]
