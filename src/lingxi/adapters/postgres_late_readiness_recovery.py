"""迟到就绪恢复的持久化：候选查询、「推进 active + 排通知」同事务、通知 outbox 的
claim / complete / purge（[V-开通-18](../../../docs/技术设计/验收矩阵-开通与身份.md)）。

外部独立审查（codex）三条必修在这里落地：

- **F1**：``activate_after_late_readiness`` 把 ``app_user.provisioning_state`` 推进到
  ``active`` 与向 ``onboarding_completion_notice`` 排一条待发通知放进**同一个数据库
  事务**——两者要么一起成立、要么一起不成立。此前的版本先推进状态、再在同一次调用里
  同步发一次通知，中途任何失败（进程崩溃、收件人查询异常、渲染异常、飞书发送失败）都
  会让用户**永久退出候选集却收不到「开通完成」**——「active 但永远不告知」与「永远不
  active」对用户是同一种失败。通知本身由 :meth:`claim_one_due_notice` /
  :meth:`mark_notice_delivered` / :meth:`mark_notice_failed` 这条持久 outbox 重试到
  送达为止，形状照 ``publish_outbox``（认领 + 记账 + 幂等键），不新造机制。
- **F2**：候选查询**不再**判断"历史上是否出现过一次 ``ready``"。旧版本的
  ``bool_or(c.result = 'ready')`` 会被任意一次历史 ``ready`` 永久命中——即使那次
  ``ready`` 之后账号被停用、CAS 因此失败，再之后账号重新启用，``bool_or`` 依然为真，
  于是**跳过当前探针直接写 active**，这正是红线「绝不把没就绪的人写成 active」要挡的
  东西。修法是删掉这条快捷路径：F1 的原子事务已经保证"状态推进"与"排通知"不会分家，
  不再需要"探过 ready 但没来得及推进"这种中途态去特殊处理，因此这条判断本身也失去了
  存在的理由。
- **F3**：``activate_after_late_readiness`` 的 ``UPDATE`` 带
  ``AND permission_version = %(expected)s``。候选查询读取时锁定的是用户**当时**那一版，
  探针也绑定同一版；但候选查到之后、这里真正推进之前，理论上可能有另一条链把用户推进
  到新的权限版本（例如同一用户被并发重新交接一次开通）。没有这条守卫，本方法可能凭一版
  已经过时的 ``ready`` 判定把人写成 ``active``。CAS 失败（版本已经变了/账号已停用/状态
  已经不是 ``mcp_syncing``）**不写任何东西、不排任何通知**，下一轮候选查询会按当前的
  真实版本重新判断。

模块住在 ``adapters/``：与其余每个 Story 一张表、一份读写口的既有形状一致
（``postgres_mcp_token.py`` 之于 ``mcp_sync_check``/``mcp_access_token``，
``postgres_roster_snapshot.py`` 之于花名册持久快照）。**本模块跨读
``publish_outbox``/``mcp_sync_check``/``app_user``、跨写 ``app_user`` +
``onboarding_completion_notice``，是刻意的例外**：候选判定天然需要三张表联合判断
"这个人现在该不该被恢复"，拆成三个模块各读一截反而会让"到底哪条筛选挡住了谁"变得
不可审查（与 ``postgres_permission_publish.published_awaiting_readiness`` 跨读
``mcp_sync_check`` 是同一条已登记的理由）。

``activate_after_late_readiness`` 之所以没有放进 ``postgres_identity.py`` 或
``postgres_permission_publish.py``：前者拥有 ``advance_provisioning_state`` 那个
通用的"任意状态间只前进"条件更新，本方法的守卫更窄（只接受从 ``mcp_syncing`` 到
``active`` 且要求精确匹配 ``expected_permission_version``），硬塞进那个通用方法会
让它的签名为一个特化调用点变形；后者已经承载了发布 outbox 的全部读写，再加一张全新的
通知表会让文件的"一个文件一个领域"边界继续模糊。给 V-开通-18 一整块**独立**的持久化
面，是这条恢复路径本身复杂度（三张表读 + 两张表写 + 一张 outbox 的 claim/complete/
purge）应得的边界。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.ids import new_id

_UTC = UTC

#: 通知认领时的退避节奏：5 分钟起步，封顶一小时。认领本身就是记账，见
#: :meth:`PostgresLateReadinessStore.claim_one_due_notice` 的文档。
NOTICE_BACKOFF_STEP_SECONDS = 300
NOTICE_BACKOFF_CEILING_SECONDS = 3600

@dataclass(frozen=True)
class LateOnboardingCandidate:
    """一个「首次开通已经超时、仍卡在 ``mcp_syncing``」的恢复候选（V-开通-18）。

    ``next_attempt_no`` 是"这个人这一版权限已经判定过几次 + 1"，供
    :meth:`~lingxi.core.permission.mcp_readiness.ReadinessRecoveryTicker.
    probe_after_timeout` 的 ``attempt_no`` 参数直接使用（真实序号仍由数据库落库时
    用 ``COALESCE(MAX(attempt_no), 0) + 1`` 重新算一遍，这里只是让审计里的次序好看）。

    **不再有 ``already_ready`` 字段**（外部独立审查 F2）：候选一律重新探一次，"探过
    ready 但没推进"这种中途态已经被 F1 的原子事务消灭。
    """

    user_id: str
    permission_version: int
    permissions: str
    next_attempt_no: int
    #: 这条链是不是系统触发（预开通，rc25 修复包 F3）：判据与
    #: ``postgres_stalled_provisioning`` 的候选查询同一条线——该用户名下**没有**
    #: ``handled_as = 'auto_provisioning'`` 的入站事件（预开通刻意不插假事件行，
    #: 见 :mod:`lingxi.core.identity.preprovision`）。为真时恢复完成**不发**
    #: 「开通完成」私聊（产品负责人裁定 4：预开通全程静默、首聊时补一句），改在
    #: 激活的同一个事务里挂起首聊补一句；用户自己发起的链一字不变。
    system_triggered: bool = False


@dataclass(frozen=True)
class StoredCompletionNotice:
    """一条被认领到的待发「开通完成」通知（供 :mod:`lingxi.apps.scheduler.
    late_readiness_recovery` 发送）。"""

    notice_id: str
    user_id: str
    permission_version: int
    company_name: str
    function_name: str
    dedupe_key: str


class PostgresLateReadinessStore:
    """V-开通-18 恢复路径的读写口：构造时不连接数据库，每次调用自带连接
    （adapters 既有惯例）。"""

    def __init__(
        self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS
    ) -> None:
        self._dsn = dsn
        self._timeouts = timeouts

    # ------------------------------------------------------------------
    # 候选查询
    # ------------------------------------------------------------------

    def late_onboarding_recovery_candidates(
        self, *, reason: str, recovery_interval_seconds: int, limit: int = 50
    ) -> tuple[LateOnboardingCandidate, ...]:
        """取「首次开通已经判过 ``timed_out``、人还停在 ``mcp_syncing``」的恢复候选。

        这是 V-开通-18 恢复路径的每轮 tick 输入。``reason`` 是必填的（与
        :meth:`~lingxi.adapters.postgres_permission_publish.
        PostgresPermissionPublishStore.published_awaiting_readiness` 同一条纪律）：
        调用方必须显式说出自己在找哪一类意图，不给默认值——默认值会在新增一种
        ``reason`` 时静默地把恢复语义套用到不该套用的意图上。

        五条筛选缺一不可：

        1. ``u.provisioning_state = 'mcp_syncing'`` + ``account_state = 'enabled'``
           ——只有还没被推进、账号仍启用的人才需要恢复；这条口径与
           :meth:`activate_after_late_readiness` 的 CAS 守卫**必须一致**，否则会出现
           "候选选中了、CAS 却总是拒绝"的死候选。
        2. ``o.status = 'published'`` 且 ``o.reason = %(reason)s``，且
           ``o.permission_version = u.permission_version``——只认当前这一版已经发布
           读回一致、属于调用方负责的那一类意图，锁定到用户**当前**这一版。
        3. ``EXISTS ... result = 'timed_out'``——只有真的判过超时的人才进候选集，天然
           避开与 ``AutoOnboardingRunner`` 自己那次阻塞式确认的并发探针。
        4. ``progress.last_started_at`` 到期判据——节奏由调用方给的
           ``recovery_interval_seconds`` 决定，只取到期的那些。
        5. ``o.payload ? 'permissions'``——payload 过了九十天会被
           ``redact_expired_payloads`` 擦成 ``'{}'``，擦过的快照渲染不出正确的公司/
           职能展示文本。这条边界与本职责模块文档「已知边界」F5 一节互相印证：见那里
           关于"重试受 outbox 保留期约束"的准确措辞。
        """

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
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
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
                """,
                {
                    "reason": reason.strip(),
                    "interval": recovery_interval_seconds,
                    "limit": limit,
                },
            )
            rows = cursor.fetchall()
        return tuple(
            LateOnboardingCandidate(
                user_id=str(row[0]),
                permission_version=int(row[1]),
                permissions=str(row[2]),
                next_attempt_no=int(row[3]) + 1,
                system_triggered=bool(row[4]),
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # 推进 active + 排通知（同一个事务，外部独立审查 F1/F3）
    # ------------------------------------------------------------------

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

        ``silent_system_trigger``（rc25 修复包 F3）：这条链是系统触发（预开通）时为
        真——产品负责人裁定 4 要求预开通**全程静默、首聊时才补一句**，因此不排
        「开通完成」私聊；改在**同一个事务**里挂起首聊补一句（守卫与
        ``postgres_identity.mark_preprovision_notice_pending`` 逐字相同：只挂一次、
        且只挂给从没跟我们说过话的人——已经在聊的人挂不上，保持彻底静默）。原子
        不变量（外部独立审查 F1/F3）不变：状态推进与"用户会得到什么"仍然要么一起
        成立、要么一起不成立。缺省 ``False`` 保持既有行为逐字节不变。

        返回是否真的推进了（``False`` = CAS 失败：这个人在候选查到之后账号被停用、
        状态已经不是 ``mcp_syncing``，或权限版本已经变了——三种情况**都不写任何东西，
        也不排任何通知**，调用方不得据此发送任何消息）。

        CAS 的四个条件对应四条独立理由：

        - ``provisioning_state = 'mcp_syncing'``：只允许从这一个状态推进，不是泛化的
          "任意更早状态都行"——候选查询本身就是按这个状态选出来的，CAS 收紧到与查询
          完全一致的口径，不留"查询选中了但 CAS 用了更宽松的判据"的缝隙。
        - ``account_state = 'enabled'``：与 ``advance_provisioning_state`` 推到
          ``active`` 时的既有守卫（`V-开通-04`）同一条口径。
        - ``permission_version = %(expected)s``（**F3**）：候选查到的是某一版的
          ``ready``；这一步之前若已经有别的写入方把用户推进到新版本，凭旧版本的
          ``ready`` 写 ``active`` 就是把没验证过的新版本悄悄冒充成已验证——CAS 因此
          必须重新核对版本，不信任候选行里携带的那个快照值。
        - ``ON CONFLICT (dedupe_key) DO NOTHING``（通知插入）：即使调用方对同一个
          ``(user_id, permission_version)`` 重复调用本方法（不应该发生，但不依赖"不
          会发生"），也不会排出第二条通知——去重键落在数据库约束上，不是靠调用方
          自己不重复调用。

        **通知内容在这里一次性快照**（``company_name``/``function_name``）：与
        ``publish_outbox.payload`` 同一条道理——重试送出的必须是当初决定的那一份，
        不依赖一份日后可能被到期擦除的外部快照。
        """

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
                    # 预开通（rc25 修复包 F3）：不排私聊，改挂首聊补一句。两道守卫
                    # 与 mark_preprovision_notice_pending 一致；0 行（已挂过 / 已经
                    # 在聊）不是失败，激活本身照常提交。
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
                else:
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
                            "version": expected_permission_version,
                            "company": company_name,
                            "function": function_name,
                            "dedupe": dedupe_key.strip(),
                        },
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

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE onboarding_completion_notice "
                "SET status = 'delivered', delivered_at = now() WHERE id = %s",
                (notice_id,),
            )

    def mark_notice_failed(self, notice_id: str, *, error: str) -> None:
        """记一次没能送达的错误码，**这条通知仍然留在 ``pending``**，按既有退避重试。

        外部独立审查第三轮 G1：本方法现在是"收件人暂时查不到"与"飞书发送失败"两种
        情况**共用**的落点——早期版本给前者单独配了一个 ``skipped`` 终态，理由是
        "这个人已经没有收件人概念了"，但 :meth:`~lingxi.adapters.
        postgres_permission_publish.PostgresPermissionPublishStore.
        notice_recipient_open_id` 的返回值 ``str | None`` 分辨不出"暂时查不到"与
        "永久不会再有"：账号 ``account_state`` 被标成停用完全可能是暂时的。把一个
        分辨不出来的情况提前判成永久放弃，原路复活了 F1 要堵的洞。真正"这个人不用
        再等了"只有一种事实来源——``user_id`` 上的 ``ON DELETE CASCADE``。

        **不改变 ``next_attempt_at``**——退避已经在 :meth:`claim_one_due_notice`
        认领时算好了，这里只留痕，不重复退避。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE onboarding_completion_notice SET last_error = %s WHERE id = %s",
                (str(error)[:500], notice_id),
            )

    def purge_expired_notices(
        self, *, now: datetime | None = None, limit: int = 500
    ) -> int:
        """删除已经送达（``delivered``）且过了九十天上限的通知，返回删除条数。
        **``pending`` 的行永远不在这条语句的候选范围内**——删掉一条还在等待送达的
        通知，等于让一个已经写成 ``active`` 的用户真的永远收不到那句话，这正是
        本表存在的理由要堵住的洞。到期判据在 ``content_expires_at``（迁移 ``0066``
        的触发器固定为 ``created_at + 2160 小时``），本方法不加第二个条件。
        """

        moment = now or datetime.now(_UTC)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("到期判定时间必须带时区")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
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
