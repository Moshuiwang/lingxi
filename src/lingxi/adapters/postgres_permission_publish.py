"""权限决定与发布意图的 PostgreSQL 存取（Issue #156 / S-C-01）。

表结构与逐条理由以迁移 ``0064_permission_publish_outbox`` 为准，本模块不复述。这里只
落实三件在**代码里**才成立的事：

1. **同事务**：一次权限决定把 ``app_user`` 的权限版本推进与 ``publish_outbox`` 的发布
   意图写在**同一个事务**里。回滚之后库里既没有新版本，也没有孤立的发布意图
   （`V-权限-01`）。:meth:`PostgresPermissionPublishStore.enqueue_publish` 因此
   **必须接收调用方的事务对象**——接口设计[「八、领域服务接口」]
   (../../../docs/技术设计/接口设计.md#八领域服务接口)把这条写成了签名约束，不靠代码
   评审保证：本方法没有任何自己建连接的路径，想绕过同事务在类型上就写不出来。
2. **认领与单飞**：``FOR UPDATE SKIP LOCKED`` 让并发消费者各取各的；额外的「该用户没有
   另一条 ``publishing``」条件让**同一用户同时只有一条发布在途**，否则 v1 的写入落后到
   v2 之后会把旧权限盖回去。
3. **到期擦除**：``payload`` 里有邮箱与姓名，九十天上限由 :meth:`redact_expired_payloads`
   把它擦成 ``'{}'`` 落实（迁移文件头部有为什么不进 ``0054`` 受限清理函数的取舍）。

**没有新增 ``app_user.publish_state`` 列**：数据库设计蓝本里有这一列，当前迁移链没有
建它。发布进度已经完整地由 ``publish_outbox.status`` 承载，再加一列等于制造第二个真相
来源，而两个真相来源迟早会分叉（这正是「五个独立状态字段」那条设计原则要避免的反面）。
需要它的时候由承接 ``app_user`` 状态机的 Story 一并落地。

**不写 ``permission_record_id``**：`V-开通-01` 要求匹配确认前它为 ``NULL``、不先占位
再回填；发布成功后写进去的应当是「数据库权限记录」而不是外部表格的记录标识——后者已经
落在 ``publish_outbox.external_record_id``。这一列在本 Story 里一次都不出现在语句中。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping, Protocol

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.ids import new_id
from lingxi.core.permission.mcp_readiness import TERMINAL_OUTCOMES
from lingxi.core.permission.publish import (
    ClaimedPublish,
    PublishAttempt,
    PublishOutcome,
)
from lingxi.core.permission.publish_row import (
    CREATED_FIELD_NAMES,
    PUBLISHED_FIELD_NAMES,
    TOKEN_CIPHER_FIELD,
    PublishRow,
    is_cipher_shaped,
)

logger = logging.getLogger(__name__)

_UTC = timezone.utc

#: 一条 ``publishing`` 卡多久之后可以放回 ``pending``。取值远大于一次外部写读回的耗时，
#: 又远小于一天：太短会让一次慢调用被重复执行（安全但浪费配额），太长会让一次进程崩溃
#: 把该用户的后续发布堵到第二天。
DEFAULT_RECLAIM_AFTER = timedelta(minutes=15)


class DecisionOutcome(Enum):
    """一次权限决定对发布链路的影响。两态，不合并。"""

    # 权限内容与上一条有效意图不同（或此前没有意图）：推进版本并排出一条新的发布意图。
    ENQUEUED = "enqueued"
    # 权限内容与上一条**仍然有效**的意图逐字段相同：只刷新检查时间，不推进版本、
    # 不排新意图。没有这一分支，每天一轮的权限刷新会天天写一次外部表格。
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class PermissionDecision:
    """一次权限决定落库之后的结果。"""

    outcome: DecisionOutcome
    user_id: str
    permission_version: int
    outbox_id: str | None = None

    @property
    def enqueued(self) -> bool:
        return self.outcome is DecisionOutcome.ENQUEUED


@dataclass(frozen=True)
class PendingReadiness:
    """一条已经发布读回一致、但就绪确认还没收口的意图（S-C-03b 的每轮 tick 输入）。

    ``permissions`` 是**当初决定发布、并且已经逐字段读回核对过**的那一串文本。就绪判定
    的 ``no_permission`` 分支与通知正文都只认它，因此这条链上不存在"通知说的范围"与
    "消费方读到的范围"来自两次不同计算的可能。
    """

    user_id: str
    permission_version: int
    permissions: str
    published_at: datetime | None = None


@dataclass(frozen=True)
class StoredIntent:
    """回读出来的一条发布意图（供运维排查与用例断言）。"""

    outbox_id: str
    user_id: str
    permission_version: int
    reason: str
    payload: Mapping[str, Any]
    status: str
    attempts: int
    last_outcome: str | None
    last_error: str | None
    external_record_id: str | None
    published_at: datetime | None


class _Transaction(Protocol):
    """调用方已经开启事务的数据库连接。

    只要求一个 ``cursor()``：本模块不 ``commit``、不 ``rollback``、不建连接——事务边界
    完全由调用方掌握，这正是「审计与状态变更同事务」这条合同在类型上的落点。
    """

    def cursor(self) -> Any: ...


class PublishClaimLost(RuntimeError):
    """记账时那条意图已经不在 ``publishing``（被回收或被人改过）。

    不静默吞掉：外部表格**可能已经写过**，而库里的状态没跟上。抛出来让调用方留痕并
    停手；那条意图会被重新认领并重发一次，重发安全（先查后写，收敛到同一行）。
    """


class PostgresPermissionPublishStore:
    """发布意图 outbox 的读写。构造时不连接数据库，每次调用自带连接（adapters 既有惯例）。"""

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        self._dsn = dsn
        self._timeouts = timeouts

    # ------------------------------------------------------------------
    # 写侧：权限决定与发布意图同事务
    # ------------------------------------------------------------------

    def record_decision(
        self, *, user_id: str, row: PublishRow, reason: str, decided_at: datetime | None = None
    ) -> PermissionDecision:
        """把一次权限决定与它的发布意图写进**同一个事务**。

        次序是刻意的：

        1. ``SELECT ... FOR UPDATE`` 锁住这一行 ``app_user``。同一用户的两次并发决定
           因此串行化——少了这把锁，两次决定会读到同一个旧版本号、各自 +1，于是两条
           意图拿到同一个 ``permission_version``，被 ``UNIQUE`` 约束拒掉一条（更糟的
           是被拒的那条可能是新的那一份）。
        2. 取该用户**最近一条**发布意图，逐字段比对内容（不含 ``updated_at``）。相同
           且那条意图仍然有效（``pending``/``publishing``/``published``）时判
           ``UNCHANGED``：只刷新 ``permission_checked_at``，不推进版本、不排新意图。
           相同但那条意图已经 ``failed``/``superseded`` 时**照常排新意图**——内容没变
           不等于已经发布成功，把它压成"无变化"会让一次失败的发布永远没人再试。
        3. 推进 ``app_user.permission_version`` 并插入意图。

        ``decided_at`` 只用于 ``permission_checked_at``；发布行里的时间戳已经在
        ``row.updated_at`` 里冻结好了（见 ``core/permission/publish_row.py``）。
        """

        if not isinstance(row, PublishRow):
            raise TypeError("发布行必须是 PublishRow：字段集与文本形态由它保证")
        moment = decided_at or datetime.now(_UTC)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("权限决定时间必须带时区")

        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT permission_version FROM app_user WHERE id = %s FOR UPDATE",
                        (user_id,),
                    )
                    current = cursor.fetchone()
                    if current is None:
                        raise LookupError("权限决定的目标用户不存在")
                    version = int(current[0])

                    cursor.execute(
                        """SELECT id, payload, status
                             FROM publish_outbox
                            WHERE user_id = %s
                            ORDER BY permission_version DESC
                            LIMIT 1""",
                        (user_id,),
                    )
                    latest = cursor.fetchone()
                    if latest is not None and _same_content(latest[1], row) and latest[2] in (
                        "pending",
                        "publishing",
                        "published",
                    ):
                        cursor.execute(
                            "UPDATE app_user SET permission_checked_at = %s, updated_at = now() "
                            "WHERE id = %s",
                            (moment, user_id),
                        )
                        logger.info(
                            "权限无变化，不排新的发布意图 user=%s version=%s", user_id, version
                        )
                        return PermissionDecision(
                            DecisionOutcome.UNCHANGED, user_id, version, str(latest[0])
                        )

                    version += 1
                    cursor.execute(
                        "UPDATE app_user SET permission_version = %s, permission_checked_at = %s, "
                        "updated_at = now() WHERE id = %s",
                        (version, moment, user_id),
                    )
                    outbox_id = self.enqueue_publish(
                        user_id=user_id,
                        reason=reason,
                        # 快照取 ``snapshot_fields``：有令牌就连密文一起冻结，重试因此
                        # 能原样重放"当初决定发布的那一版"，不必在重试那一刻回查一次
                        # 令牌表（那会让内容取决于重试时刻的库状态）。
                        payload=row.snapshot_fields,
                        permission_version=version,
                        tx=connection,
                    )
        logger.info("权限发布意图已排入 user=%s version=%s reason=%s", user_id, version, reason)
        return PermissionDecision(DecisionOutcome.ENQUEUED, user_id, version, outbox_id)

    def enqueue_publish(
        self,
        *,
        user_id: str,
        reason: str,
        payload: Mapping[str, str],
        permission_version: int,
        tx: _Transaction,
    ) -> str:
        """在**调用方的事务**里排一条发布意图，返回意图标识。

        签名与接口设计的 ``enqueue_publish(user_id, reason, *, tx)`` 对齐，多出来的两个
        参数是它省略的实现细节（发什么内容、发的是哪一版）。``tx`` 是必填关键字参数：
        本方法不建连接、不提交、不回滚，事务边界完全在调用方手里。

        ``payload`` 的键集必须**恰好**是更新集 :data:`PUBLISHED_FIELD_NAMES` 或新建集
        :data:`CREATED_FIELD_NAMES`（= 更新集 + ``token_cipher``）二者之一。多一个键就
        意味着有人在这里往外部表格夹带字段，少一个键会让消费侧拿到一份残缺的快照；
        "六个里少一个再补上 token_cipher 凑成七个"这种混合形状同样被拒。两种都直接
        失败，不做静默补齐或过滤。

        **进快照的是密文，不是明文**：令牌明文与主密钥一步都不进 outbox
        （`V-权限-11`）。快照带密文的理由见
        :attr:`lingxi.core.permission.publish_row.PublishRow.snapshot_fields`。
        """

        if tx is None or not hasattr(tx, "cursor"):
            raise TypeError("enqueue_publish 必须接收调用方的事务对象（同事务合同）")
        if not user_id:
            raise ValueError("发布意图必须绑定用户")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("发布意图必须写明原因")
        if isinstance(permission_version, bool) or not isinstance(permission_version, int):
            raise ValueError("权限版本必须是整数")
        if permission_version <= 0:
            raise ValueError("权限版本必须为正：0 表示还没有过任何权限决定")
        fields = dict(payload)
        if set(fields) not in (set(PUBLISHED_FIELD_NAMES), set(CREATED_FIELD_NAMES)):
            raise ValueError("发布内容快照的字段集必须与已登记的发布字段完全一致")
        # **键存在就校验形状**，不是"值非 None 才校验"：一份带着 ``token_cipher: None``
        # 的七键快照曾经能过——它既不是合法的六键更新快照，也不是可用的七键新建快照，
        # 还会在还原成 ``PublishRow`` 之后表现得像"没有令牌"。
        if TOKEN_CIPHER_FIELD in fields and not is_cipher_shaped(fields[TOKEN_CIPHER_FIELD]):
            # 明文长得不像密文（``token_urlsafe`` 是 URL 安全 base64，长度也不对齐），
            # 因此这道形状校验就是"明文不进 outbox"的最后一关。不回显收到的值。
            raise ValueError("发布内容快照的 token_cipher 形状不合法（不回显收到的值）")

        outbox_id = new_id("pub")
        with tx.cursor() as cursor:
            cursor.execute(
                """INSERT INTO publish_outbox
                     (id, user_id, permission_version, reason, payload, content_expires_at)
                   VALUES (%s, %s, %s, %s, %s, now())""",
                (outbox_id, user_id, permission_version, reason.strip(), _jsonb(fields)),
            )
        return outbox_id

    # ------------------------------------------------------------------
    # 消费侧
    # ------------------------------------------------------------------

    def claim_next(self) -> ClaimedPublish | None:
        """认领最早一条可发布的意图，并原子记账（状态转 ``publishing``、尝试次数 +1）。

        两道条件缺一不可：

        - ``FOR UPDATE SKIP LOCKED``：并发消费者各取各的，既不重复也不互相阻塞
          （同 ``postgres_conversation.claim_tasks`` 的既有形态）；
        - **该用户不存在更早的非终态兄弟行**（``pending`` 或 ``publishing``）：
          这就是「同一用户单飞」。少了它，同一用户的 v1 与 v2 会被两个消费者同时拿走，
          v1 的外部写入落后到 v2 之后就把旧权限盖了回去——而外部表格没有版本号，
          谁也发现不了；用户侧表现为**已经收回的权限被静默恢复**。

        **为什么判据是「更早的非终态兄弟行」而不是「有没有 ``publishing``」**（二级
        独立审查 P2-1）：后者在 ``READ COMMITTED`` 下有一个真实的漏洞窗口——消费者 A
        认领 v1 的 ``UPDATE`` 尚未提交时，它写的 ``publishing`` 对消费者 B **不可见**，
        B 读到的仍是 v1 提交态的 ``pending``，于是 ``NOT EXISTS (status='publishing')``
        成立，B 领走同一用户的 v2。两个消费者同时对外发布，谁后写谁生效。
        改成「更早的非终态兄弟行」之后，A 未提交期间 v1 在**任何**快照里都还是
        ``pending``，正好落进判据，B 因此拒领 v2；A 提交后 v1 变 ``publishing``，
        仍是非终态、仍然挡着。只有 v1 走到终态（``published``/``failed``/
        ``superseded``）之后，v2 才成为该用户最老的非终态意图。次序用
        ``(created_at, id)`` 行比较，与下面的 ``ORDER BY`` 同一把尺子。

        一并取回该用户**当前**的权限版本，让「旧版本不覆盖新版本」成为纯判定
        （见 ``core/permission/publish.publish_claim``）。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE publish_outbox AS o
                       SET status = 'publishing',
                           attempts = o.attempts + 1,
                           claimed_at = now()
                     WHERE o.id = (
                             SELECT c.id
                               FROM publish_outbox c
                              WHERE c.status = 'pending'
                                AND NOT EXISTS (
                                      SELECT 1
                                        FROM publish_outbox b
                                       WHERE b.user_id = c.user_id
                                         AND b.status IN ('pending', 'publishing')
                                         AND (b.created_at, b.id) < (c.created_at, c.id)
                                    )
                              ORDER BY c.created_at, c.id
                              LIMIT 1
                                FOR UPDATE SKIP LOCKED
                           )
                    RETURNING o.id, o.user_id, o.permission_version, o.payload, o.attempts,
                              o.created_record_id
                    """
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                # 当前权限版本在**同一个事务**里读，与认领是同一时刻的事实；分成两次
                # 连接会让「认领到的是不是最新版本」建立在两个不同时刻的观察上。
                cursor.execute(
                    "SELECT permission_version FROM app_user WHERE id = %s", (row[1],)
                )
                current = cursor.fetchone()
        return ClaimedPublish(
            outbox_id=str(row[0]),
            user_id=str(row[1]),
            permission_version=int(row[2]),
            payload=dict(row[3] or {}),
            attempts=int(row[4]),
            current_permission_version=None if current is None else int(current[0]),
            # **这条意图自己建过的那一行**（``created_record_id``，不是审计用的
            # ``external_record_id``）。判定层用它回答"这一行是不是我们建的"；既有 26 行
            # 永远是 NULL，因此"密文被改写"那条判定不会误伤它们——哪怕它们的某次更新
            # 读回不明、行 ID 已经进了 ``external_record_id``。
            created_record_id=None if row[5] is None else str(row[5]),
        )

    def complete(self, attempt: PublishAttempt, *, status: str) -> None:
        """把一次尝试的结果记回意图行。

        ``WHERE status = 'publishing' AND attempts = …`` 是防线而不是装饰：它把这次
        记账**绑定到本次认领**。

        只判 ``status = 'publishing'`` 不够（二级独立审查 P3-1）：一条被
        :meth:`reclaim_stale` 放回 ``pending``、又被**另一个**消费者重新认领的意图，
        此刻状态恰好也是 ``publishing``——旧认领者迟到的记账会命中它，把新认领者正在
        进行的那一次改写成 ``published``；而新认领者随后的记账反而扑空，被报成
        :class:`PublishClaimLost`，合法的那一方成了报警对象。``attempts`` 在每次认领时
        自增，因此它是「哪一次认领」的天然版本号：旧认领者带的是旧值，命中不到，
        如实拿到 :class:`PublishClaimLost`；新认领者带的是新值，正常记账。

        **两列记录的是两件不同的事，不能合并**：

        - ``external_record_id``：**审计**——上一次尝试操作的是哪一行。任何尝试都会写，
          包括既有行更新失败（``mismatch``/``uncertain``）。S-C-01 的既有语义，不变。
        - ``created_record_id``：**出身**——这条意图**自己建过**的那一行。只有
          ``action == 'create'`` **且真的拿到了记录标识**时才写；更新尝试传 ``None``，
          因此 ``COALESCE(新值, 旧值)`` 对它是"保持不变"。判定层拿它回答"这一行是不是
          我们建的"。

          **重复创建时取最近一次**（而不是保留第一次）：能走到第二次 ``create`` 说明
          上一行已经查不到了，出身若停在那个失效的标识上，"我们建的行密文被改写"这道
          检查就永远命不中当前这一行——保护静默失效。取最近一次则让检查作用在真正活着
          的那一行上，且不会产生假阳性：条件本来就是"这一行是我们建的且密文不是我们的"。

        混用会造出一个真实的回归：既有 26 行只要有一次更新读回不明，行 ID 就进了
        ``external_record_id``，重试时"这一行是我们建的"成立、而它们的旧密文当然不等于
        我方快照，于是被判成永久 ``mismatch``——S-C-01 的"更新可重试收敛"就被打断了。
        """

        detail = _error_detail(attempt)
        # 出身只由"create 明确返回了记录标识"这一种事实设置。创建结果不明（没拿到 ID）
        # 时保持 NULL：那时我们无法把"自己建的"与"并发写入方建的"区分开，重试按普通
        # 路径收敛，就绪探针是最终的门。
        created = attempt.external_record_id if attempt.action == "create" else None
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE publish_outbox
                      SET status = %(status)s,
                          last_outcome = %(outcome)s,
                          last_error = %(detail)s,
                          external_record_id = COALESCE(%(record_id)s, external_record_id),
                          created_record_id = COALESCE(%(created_id)s, created_record_id),
                          published_at = CASE WHEN %(status)s = 'published' THEN now() ELSE NULL END
                    WHERE id = %(id)s
                      AND status = 'publishing'
                      AND attempts = %(attempts)s""",
                {
                    "status": status,
                    "outcome": attempt.outcome.value,
                    "detail": detail,
                    "record_id": attempt.external_record_id,
                    "created_id": created,
                    "id": attempt.outbox_id,
                    "attempts": attempt.attempts,
                },
            )
            if cursor.rowcount != 1:
                raise PublishClaimLost(
                    f"发布意图记账失败，认领已丢失：outbox={attempt.outbox_id}"
                )

    def reclaim_stale(self, *, older_than: timedelta = DEFAULT_RECLAIM_AFTER) -> int:
        """把卡在 ``publishing`` 超过 ``older_than`` 的意图放回 ``pending``，返回条数。

        这是**重启恢复**：进程在外部写入与记账之间崩溃时，那条意图会一直占着该用户的
        单飞名额。放回去安全，因为发布本身幂等（先查后写，收敛到同一行、同一份内容）。
        """

        if not isinstance(older_than, timedelta) or older_than <= timedelta(0):
            raise ValueError("回收阈值必须是正的时间间隔")
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE publish_outbox
                      SET status = 'pending', claimed_at = NULL, last_outcome = 'reclaimed'
                    WHERE status = 'publishing' AND claimed_at < now() - %s::interval""",
                (older_than,),
            )
            reclaimed = cursor.rowcount
        if reclaimed:
            logger.warning("回收滞留的发布意图 条数=%s", reclaimed)
        return reclaimed

    def redact_expired_payloads(self, *, now: datetime | None = None, limit: int = 500) -> int:
        """把过了九十天上限的内容快照擦成空对象，返回擦除条数。

        擦的是 ``payload``（含邮箱与姓名）与 ``last_error``；``user_id``、权限版本、
        状态与时间戳留下——它们是「谁的哪一版权限什么时候发布成功过」这类运行事实，
        本身不可再映射到人（``user_id`` 是内部 ULID，且随 ``app_user`` 删除一并 CASCADE）。

        **过期的意图擦掉内容之后就发不出去了**，这是对的：一份九十天前决定的权限不该
        在今天被写进外部表格。它下一次被认领时会以 ``invalid`` 收敛并停止重试。
        """

        moment = now or datetime.now(_UTC)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("到期判定时间必须带时区")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE publish_outbox
                      SET payload = '{}'::jsonb, last_error = NULL
                    WHERE id IN (
                            SELECT id FROM publish_outbox
                             WHERE content_expires_at <= %s AND payload <> '{}'::jsonb
                             ORDER BY content_expires_at
                             LIMIT %s
                          )""",
                (moment, limit),
            )
            redacted = cursor.rowcount
        if redacted:
            logger.info("发布意图内容已到期擦除 条数=%s", redacted)
        return redacted

    # ------------------------------------------------------------------
    # 发布之后：该给谁做就绪确认、该给谁发通知
    # ------------------------------------------------------------------

    def published_awaiting_readiness(self, *, limit: int = 50) -> tuple[PendingReadiness, ...]:
        """取「已经发布读回一致、但就绪确认还没收口」的那些 ``(用户, 权限版本)``。

        这是 S-C-03b 的调度职责每轮 tick 的输入（见
        :mod:`lingxi.apps.scheduler.permission_publish`）。三条筛选各自都不能少：

        1. ``status = 'published'``——**只有发布读回一致的那一版**才进入就绪确认与通知。
           这条同时就是「撤权通知只在读回一致之后发」这条产品裁定在 SQL 里的落点：
           一条还在 ``pending`` 的撤权意图取不到，也就发不出任何通知。
        2. **该用户没有更新的意图**。权限在确认期间又变了时，旧的那一版不该再被确认、
           更不该据它给用户发一条已经过时的范围通知；新的那一版会自己走一遍。
        3. **没有终态就绪记录**（``ready`` / ``no_permission`` / ``timed_out``）。
           终态是这条链**唯一**的"已经处理过"水位——它同时保证了同一次变化的通知只发
           一次，不需要第二张表、也不需要在 outbox 上再加一列状态。

        **本方法跨读 ``mcp_sync_check``（属 ``postgres_mcp_token`` 的表），是一处知情的
        例外**：两张表同属"权限发布与就绪确认"这一个领域，本方法**只读**，而 ``mcp_sync_check``
        的**写**仍然只有 ``postgres_mcp_token.record_attempt`` 一处。做成一条语句是必需
        而不是省事——把第三条筛选挪到 Python 里做，就得先把"全部已发布的最新意图"取回来
        再逐条过滤，于是已经确认完的人会一直占着取回窗口，当天靠后发布的那些人可能永远
        排不进来（饿死），而这种缺陷在小数据量的用例里完全看不出来。

        ``payload`` 过了九十天会被 :meth:`redact_expired_payloads` 擦成 ``'{}'``，
        因此这里要求 ``payload`` 里确有 ``permissions`` 键：擦过的快照说不出"当时发布的
        范围是什么"，据它渲染通知只会渲染出一句错话。
        """

        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        terminal = sorted(item.value for item in TERMINAL_OUTCOMES)
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT o.user_id, o.permission_version, o.payload ->> 'permissions',
                          o.published_at
                     FROM publish_outbox o
                    WHERE o.status = 'published'
                      AND o.payload ? 'permissions'
                      AND NOT EXISTS (
                            SELECT 1 FROM publish_outbox newer
                             WHERE newer.user_id = o.user_id
                               AND newer.permission_version > o.permission_version
                          )
                      AND NOT EXISTS (
                            SELECT 1 FROM mcp_sync_check c
                             WHERE c.user_id = o.user_id
                               AND c.permission_version = o.permission_version
                               AND c.result = ANY(%s)
                          )
                    ORDER BY o.published_at, o.id
                    LIMIT %s""",
                (terminal, limit),
            )
            rows = cursor.fetchall()
        return tuple(
            PendingReadiness(
                user_id=str(row[0]),
                permission_version=int(row[1]),
                permissions=str(row[2]),
                published_at=row[3],
            )
            for row in rows
        )

    def has_published_intent(self, user_id: str) -> bool:
        """这个用户**有没有过**一条发布成功的意图。

        撤权那一侧的判据（`V-权限-08` 的刷新侧）：只有"我们真的往发布表写成功过一行"的
        用户，才值得为他发一条把 ``permissions`` 清空的更新。这是**本地能拿到的、关于
        "发布表里有没有他那一行"最直接的证据**——发布链路只有一条，写成功过就等于那一行
        存在（我们不删行）。

        反过来，从来没有过 ``published`` 意图的人**跳过**：为他新建一行空权限没有意义
        （问数 MCP 对查无此人本来就默认拒绝），而新建还需要一份令牌密文。既有 26 行那些
        旧系统写的人也落在这一路——硬切之前他们的撤权由旧系统负责（产品负责人 2026-08-18
        裁定 3 的时效边界）。
        """

        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("发布历史判定必须指明用户")
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM publish_outbox WHERE user_id = %s AND status = 'published' LIMIT 1",
                (user_id,),
            )
            return cursor.fetchone() is not None

    def notice_recipient_open_id(self, user_id: str) -> str | None:
        """权限变化通知发给谁：该用户的 ``feishu_open_id``；不该发就返回 ``None``。

        **两条过滤是产品约束，不是编码习惯**，口径与花名册比对基线同源
        （``adapters/postgres_roster_audit.ACTIVE_BASELINE_SQL``）：

        - ``provisioning_state = 'active'``：还没完成开通的人不该收到"你的可用范围已更新"；
        - ``account_state NOT IN ('deleting','deleted')``：正在删除或已删除的账号一律不发。
          删除编排会清空姓名等字段，给这样的账号发消息是数据范围外泄。

        **为什么这条查询住在本模块**：这条链本来就在这里读 ``app_user``（权限版本），
        而把它放进 ``postgres_identity`` 会把那个模块的建档写侧闭包（``provisioning`` /
        ``org_snapshot`` / ``first_contact``）整个拉进 scheduler 镜像的依赖清单，
        只为一次只读查询。本方法**只读一列**，不写、不改任何状态。
        """

        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("通知收件人查询必须指明用户")
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT feishu_open_id
                     FROM app_user
                    WHERE id = %s
                      AND provisioning_state = 'active'
                      AND account_state NOT IN ('deleting', 'deleted')""",
                (user_id,),
            )
            row = cursor.fetchone()
        if row is None or row[0] is None:
            return None
        open_id = str(row[0]).strip()
        return open_id or None

    def load(self, outbox_id: str) -> StoredIntent | None:
        """回读一条意图。给运维排查与用例断言用，不在发布链路上。"""

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT id, user_id, permission_version, reason, payload, status, attempts,
                          last_outcome, last_error, external_record_id, published_at
                     FROM publish_outbox WHERE id = %s""",
                (outbox_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return StoredIntent(
            outbox_id=str(row[0]),
            user_id=str(row[1]),
            permission_version=int(row[2]),
            reason=str(row[3]),
            payload=dict(row[4] or {}),
            status=str(row[5]),
            attempts=int(row[6]),
            last_outcome=row[7],
            last_error=row[8],
            external_record_id=row[9],
            published_at=row[10],
        )


def _same_content(payload: Any, row: PublishRow) -> bool:
    """上一条意图的内容与这一次的决定是否逐字段相同（``updated_at`` 不参与）。

    比较前先把两边都归一成 ``{字段名: 文本}``：payload 从 JSONB 回来时数字会变成
    Python 数字，而发布行永远是文本——按类型严格相等会让一次没有变化的刷新被判成变化。
    """

    if not isinstance(payload, Mapping):
        return False
    expected = row.content_fields
    return all(str(payload.get(name, "")) == value for name, value in expected.items())


def _error_detail(attempt: PublishAttempt) -> str | None:
    """记进 ``last_error`` 的诊断串：**只有错误码与字段名**，没有任何字段值。"""

    if attempt.outcome is PublishOutcome.PUBLISHED:
        return None
    parts = [attempt.error_code or attempt.outcome.value]
    if attempt.mismatch_fields:
        parts.append("fields=" + ",".join(attempt.mismatch_fields))
    if attempt.detail:
        parts.append(attempt.detail)
    return " ".join(parts)[:500]


def _jsonb(value: Any) -> Any:
    """把内容快照交给 psycopg 的 JSONB 适配。延迟导入：没有驱动的机器仍能 import 本模块。"""

    from psycopg.types.json import Jsonb

    return Jsonb(value)
