"""权限决定与发布意图的 PostgreSQL 存取（Issue #156 / S-C-01）。

表结构与逐条理由以迁移 ``0064_permission_publish_outbox`` 为准，本模块不复述。这里只
落实四件在**代码里**才成立的事：

1. **同事务**：一次权限决定把 ``app_user`` 的权限版本推进与 ``publish_outbox`` 的发布
   意图写在**同一个事务**里。回滚之后库里既没有新版本，也没有孤立的发布意图
   （`V-权限-01`）。:meth:`PostgresPermissionPublishStore.enqueue_publish` 因此
   **必须接收调用方的事务对象**——接口设计[「八、领域服务接口」]
   (../../../docs/技术设计/接口设计.md#八领域服务接口)把这条写成了签名约束，不靠代码
   评审保证：本方法没有任何自己建连接的路径，想绕过同事务在类型上就写不出来。
2. **认领与单飞**：``FOR UPDATE SKIP LOCKED`` 让并发消费者各取各的；额外的「该用户没有
   另一条 ``publishing``」条件让**同一用户同时只有一条发布在途**，否则 v1 的写入落后到
   v2 之后会把旧权限盖回去。第三条是 :meth:`PostgresPermissionPublishStore.claim_next`
   的 ``exclude``——**本轮已经认领过的意图本轮不再取**：一次失败只写回 ``pending``、
   不改 ``created_at``，没有这一条它会在同一轮里被立刻重新认领（Epic C 冻结缺陷 F2）。
3. **到期擦除**：``payload`` 里有邮箱与姓名，九十天上限由 :meth:`redact_expired_payloads`
   把它擦成 ``'{}'`` 落实（迁移文件头部有为什么不进 ``0054`` 受限清理函数的取舍）。
4. **权限变化感知即清**（Trace #328 S-P-5）：:meth:`record_decision` 的
   ``clear_delivered_content=True`` 在**同一事务**里顺带清空该用户全部会话已送达、
   随会话保留的投递正文，接入迁移 ``0061`` 早就给"权限变化感知"预留的
   ``user_cleared`` 分类值。默认关闭，只由每日刷新的授权、撤权两条路径
   （``apps/scheduler/permission_refresh.py``）显式打开——为什么不对首次开通也打开，
   见 :meth:`record_decision` 文档「为什么是调用方显式传入的开关」一节。

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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Protocol

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.adapters.postgres_conversation import _Transaction as _ConversationTransaction
from lingxi.core.identity.roster_audit import ArchivedIdentity
from lingxi.core.ids import new_id
from lingxi.core.permission.mcp_readiness import TERMINAL_OUTCOMES
from lingxi.core.permission.publish import (
    ACCOUNT_STATE_ENABLED,
    ClaimedPublish,
    PermissionDecisionTransientFailure,
    PermissionGrantBlockedByAccountState,
    PublishAttempt,
    PublishOutcome,
)
from lingxi.core.permission.publish_row import (
    CREATED_FIELD_NAMES,
    PUBLISHED_FIELD_NAMES,
    REVOKED_PERMISSIONS_TEXT,
    TOKEN_CIPHER_FIELD,
    PublishRow,
    content_digest,
    is_cipher_shaped,
    permissions_digest,
)

logger = logging.getLogger(__name__)

_UTC = UTC

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
    # 本次决定顺带清掉的「已送达、随会话保留」投递正文事件数（Trace #328 S-P-5）。
    # 只有 ``record_decision(clear_delivered_content=True)`` 且真的推进了版本
    # （``outcome`` 为 ``ENQUEUED``）时才可能非零；``UNCHANGED`` 恒为 0——内容没变，
    # 没有清理动作发生。默认 0 而不是 ``None``：调用方（``permission_refresh.py``）
    # 把它直接写进审计计数字段，``None`` 会让那条审计行需要多一次判空。
    cleared_events: int = 0

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
        self,
        *,
        user_id: str,
        row: PublishRow,
        reason: str,
        require_enabled_account: bool,
        decided_at: datetime | None = None,
        clear_delivered_content: bool = False,
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
        4. ``clear_delivered_content=True`` 且这次真的走到了第 3 步（即将返回
           ``ENQUEUED``）**且 ``permissions`` 列内容真的变了**时，在**同一个事务**
           里追加一次 ``clear_delivered_content_for_user``，清空该用户全部会话已
           送达、随会话保留的投递正文，并排队失效当前 Agent 会话（Trace #328
           S-P-5）。``reason`` 固定传 ``user_cleared``——迁移 ``0061`` 早就给「停用
           感知、权限变化感知」两类触发预留的分类值，这里接入的正是其中"权限变化
           感知"那一半；``UNCHANGED`` 分支不清理，内容没变没有什么需要失效。

           **「``permissions`` 列内容真的变了」是一条独立于 ENQUEUED/UNCHANGED 判定
           的比较**（Trace #328 opus 审查 P1）：ENQUEUED 只说明**整行**（含
           ``record_key``/``email``/``name``/``permissions``/``status``）与上一条
           **仍然有效**（``pending``/``publishing``/``published``）的意图不同，
           不等于"这个人的实际可用权限变了"——两种情形会让 ENQUEUED 成立、但
           ``permissions`` 文本其实没变：① 上一条意图已经 ``failed``/``superseded``
           （不算"仍然有效"，因此按上面第 2 步会照常排新意图，即使内容与那条失效
           意图逐字节相同——这是重试，不是变化）；② 存档身份的 ``email``/
           ``display_name`` 变了（例如改名），导致 ``record_key``/``name`` 两列
           不同、整行判定为"变化"，但这个人的实际可用权限一个字符都没动。清理触发
           因此单独比较 ``row.permissions`` 与该用户**上一条意图**（不论其状态）
           的 ``permissions`` 列文本，与整行 ENQUEUED/UNCHANGED 判定完全分开：
           清理只在这个更窄的比较判真时才发生，见 :func:`_permissions_changed`。

        ``require_enabled_account`` 是**必填**关键字参数：调用方必须声明"我这次落的
        是一份需要账号有效才能生效的授权（``True``），还是一次任何状态都必须放行的
        撤权（``False``）"。声明 ``True`` 时，上面第 1 步那把**已经持有的行锁**里顺带
        读出的 ``account_state`` 一旦不是 :data:`~lingxi.core.permission.publish.
        ACCOUNT_STATE_ENABLED`，本方法**一个字节都不写**、抛
        :class:`~lingxi.core.permission.publish.PermissionGrantBlockedByAccountState`，
        事务整体回滚（Issue #483）。

        **这是消除竞态，不是缩小窗口**：管理员的「停用」写入走的是**同一行的同一把
        ``FOR UPDATE`` 锁**（``adapters/postgres_pending_action.py`` 的 ``_confirm_
        locked`` 先锁 ``app_user`` 再翻转 ``account_state``），两个写入者必然串行；
        先到者提交之后，后到者读到的一定是提交后的状态。原缺陷是"读基线的那一刻他
        还是 enabled、落决定的这一刻他已经被停用"——判据挪进锁里之后，这两个"刻"
        就是同一个。它同时关掉另一条**不需要竞态**的同族路径：管理员对一个已停用
        用户做本地权限动作触发的定向重算（``core/permission/targeted_recompute.py``
        取的身份基线是**花名册审计基线**，有意包含 ``suspended``）。

        **为什么必填而不是给默认值**：一个"默认不检查"的安全开关迟早会被新调用点
        忘掉。本仓为这条纪律付过一次学费——``merge_permission_sources(full_access_
        wildcard=...)`` 的默认值曾是一次真实漏接的根因，Trace #445 之后改成必填
        关键字（``apps/scheduler/permission_refresh.py`` 该调用处的注释原话）。

        **声明与内容必须自洽**：声明 ``False``（不要求账号有效）时，``row.permissions``
        必须是撤权行的空对象（:data:`~lingxi.core.permission.publish_row.
        REVOKED_PERMISSIONS_TEXT`），否则直接 ``ValueError``、不进事务。这让"未来某个
        调用点传错 ``False`` 却排非空授权"在运行期就写不出来——守卫因此不依赖调用方
        声明得对，只依赖它声明得**自洽**。反方向不设限：声明 ``True`` 的撤权只是多要
        一次账号有效，方向是少写、不是多写。

        **失败方向是"少写"**：守卫写错时谁都发不出权限（包括首次开通），那是停服级别
        的误伤，比原缺陷更响——所以判据**只看 ``account_state``**，绝不带
        ``provisioning_state``：首次开通调用本方法时 ``provisioning_state`` 还是
        ``provisioning``（推进到 ``mcp_syncing`` 发生在发布**之后**），多带一列会让
        首次开通 100% 全部失败。``tests/test_permission_publish_postgres.py`` 有一条
        正向真库用例把这一点钉死。

        **为什么是调用方显式传入的开关，不是本方法内部自己判断"这是权限变化所以该
        清"**：本方法同时服务首次开通（``core/identity/onboarding_runner.py``，
        ``reason=first_onboarding``）与每日刷新的授权、撤权两条路径
        （``apps/scheduler/permission_refresh.py``）。首次开通的用户结构上还没有
        任何历史会话——为一个刚建档的人去锁 ``conversation``/``task_delivery_event``
        除了白付一次查询和两把用不上的行锁没有任何效果。默认 ``False`` 让首次开通
        这条路径保持结构上"不碰"，不依赖"反正没数据所以无害"这种偶然；只有明确
        代表权限变化感知的两个调用点（每日刷新的授权、撤权分支）才把它打开。

        **同一事务、同一姿态**：``adapters/postgres_pending_action.py`` 的
        ``PostgresPendingActionStore.confirm()`` 在 ``suspend_user`` 执行分支里，
        用它已经持有 ``app_user`` 行锁的同一个连接构造
        ``postgres_conversation._Transaction`` 并调用
        ``clear_delivered_content_for_user``，让清理排队与 ``account_state`` 翻转、
        审计写入落在同一个数据库事务——这是本方法第 4 步复用的同一个姿态：清理若
        失败，第 3 步刚推进的 ``permission_version`` 与刚入队的发布意图随事务一起
        回滚，不会出现"权限已经变了、旧正文却还留着"的半套状态。

        ``decided_at`` 只用于 ``permission_checked_at``；发布行里的时间戳已经在
        ``row.updated_at`` 里冻结好了（见 ``core/permission/publish_row.py``）。

        **数据库瞬时故障不向上传播成裸 psycopg 异常**（Trace #328 opus 审查 P1，
        "照停用路径已有的捕获形状"）：本方法捕获 ``psycopg.errors.OperationalError``
        并转译为 :class:`~lingxi.core.permission.publish.
        PermissionDecisionTransientFailure`（事务已整体回滚，语义与转译方式见该类
        文档，与 ``adapters/postgres_pending_action.py`` 的 ``confirm()`` 完全同一
        姿态——同样是"先 ``FOR UPDATE`` 锁目标行，再做后续写入"的形状，暴露在同一类
        锁冲突之下）。真正实现"执行体"的是 :meth:`_record_decision_locked`——拆成
        独立方法是为了让这层 ``try/except`` 不必重新缩进整段已经很深的事务体。
        """

        if not isinstance(row, PublishRow):
            raise TypeError("发布行必须是 PublishRow：字段集与文本形态由它保证")
        if not isinstance(require_enabled_account, bool):
            raise TypeError("require_enabled_account 必须显式传 True/False")
        if not require_enabled_account and row.permissions != REVOKED_PERMISSIONS_TEXT:
            # 声明与内容自洽（方法文档「声明与内容必须自洽」一节）：不要求账号有效的
            # 调用只能是撤权。不回显收到的内容——它是这个人的权限范围。
            raise ValueError("声明不要求账号有效的权限决定只能排撤权行（permissions 必须为空对象）")
        moment = decided_at or datetime.now(_UTC)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("权限决定时间必须带时区")

        from psycopg.errors import OperationalError

        try:
            return self._record_decision_locked(
                user_id=user_id,
                row=row,
                reason=reason,
                moment=moment,
                require_enabled_account=require_enabled_account,
                clear_delivered_content=clear_delivered_content,
            )
        except OperationalError as error:
            raise PermissionDecisionTransientFailure(type(error).__name__) from error

    def _record_decision_locked(
        self,
        *,
        user_id: str,
        row: PublishRow,
        reason: str,
        moment: datetime,
        require_enabled_account: bool,
        clear_delivered_content: bool,
    ) -> PermissionDecision:
        """:meth:`record_decision` 的事务体本身，逐字保留自拆分前的
        ``record_decision``——拆分只为了让 :meth:`record_decision` 能在外层包一层
        不影响这里任何缩进的 ``try/except OperationalError``。"""

        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT permission_version, account_state FROM app_user "
                        "WHERE id = %s FOR UPDATE",
                        (user_id,),
                    )
                    current = cursor.fetchone()
                    if current is None:
                        raise LookupError("权限决定的目标用户不存在")
                    version = int(current[0])
                    account_state = str(current[1])
                    if require_enabled_account and account_state != ACCOUNT_STATE_ENABLED:
                        # Issue #483：判据在**这把已经持有的行锁**里，不是锁外先查一次
                        # ——锁外查会把窗口从"整轮"缩到"两条 SQL 之间"，竞态依然存在。
                        # 抛出让事务整体回滚：版本不推进、意图不入队、送达正文不清，
                        # 不留任何半套状态（方法文档 ``require_enabled_account`` 一节）。
                        logger.warning(
                            "账号状态不允许排出非空授权，本次权限决定整体回滚 "
                            "user=%s account_state=%s reason=%s",
                            user_id,
                            account_state,
                            reason,
                        )
                        raise PermissionGrantBlockedByAccountState(account_state)

                    cursor.execute(
                        """SELECT id, payload, status, content_digest, permissions_digest
                             FROM publish_outbox
                            WHERE user_id = %s
                            ORDER BY permission_version DESC
                            LIMIT 1""",
                        (user_id,),
                    )
                    latest = cursor.fetchone()
                    # 清理触发的判据（第 4 步文档）：与上面的 ENQUEUED/UNCHANGED
                    # 判定完全独立，只比较 permissions 列文本，不看 email/name 等
                    # 资料字段，也不看上一条意图的状态。
                    permissions_changed = latest is None or _permissions_changed(
                        latest[1], latest[4], row
                    )
                    if latest is not None and _same_content(latest[1], latest[3], row) and latest[2] in (
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

                cleared_events = 0
                if clear_delivered_content and permissions_changed:
                    # 同一个连接、同一个事务：上面 SELECT ... FOR UPDATE 拿到的
                    # app_user 行锁在这里仍然有效，enqueue_publish 的 INSERT 也还
                    # 没提交。清理若抛异常，version 推进与发布意图入队随事务一起
                    # 回滚（方法文档第 4 步、"同一事务、同一姿态"两节）。
                    # ``permissions_changed`` 收窄（Trace #328 opus 审查 P1）：
                    # ENQUEUED 但 permissions 列文本没变时（改名重发、失败重排同
                    # 内容）不清——见方法文档第 4 步的完整理由。
                    cleared_events = _ConversationTransaction(connection).clear_delivered_content_for_user(
                        user_id=user_id, reason="user_cleared"
                    )
                    # 用户记忆同一姿态一并清除（Issue #357 S-H3-3 c 节）：同一个
                    # 已持有的 connection/事务，version 推进、发布意图入队与本次
                    # 清除失败一起回滚，不产生"权限已变、记忆却还在"的半套状态。
                    _ConversationTransaction(connection).clear_user_memory(user_id=user_id)
        logger.info("权限发布意图已排入 user=%s version=%s reason=%s", user_id, version, reason)
        return PermissionDecision(
            DecisionOutcome.ENQUEUED, user_id, version, outbox_id, cleared_events=cleared_events
        )

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
                     (id, user_id, permission_version, reason, payload, content_expires_at,
                      content_digest, permissions_digest)
                   VALUES (%s, %s, %s, %s, %s, now(), %s, %s)""",
                (
                    outbox_id,
                    user_id,
                    permission_version,
                    reason.strip(),
                    _jsonb(fields),
                    # 摘要与快照同一条语句写入，之后**不随内容擦除消失**（迁移
                    # ``0085``）：擦掉的是邮箱与姓名，留下的是"和另一份一不一样"
                    # 这一个不可反推的答案，见 :func:`_same_content` 文档。
                    content_digest(fields),
                    permissions_digest(fields),
                ),
            )
        return outbox_id

    # ------------------------------------------------------------------
    # 消费侧
    # ------------------------------------------------------------------

    def claim_next(self, *, exclude: Sequence[str] = ()) -> ClaimedPublish | None:
        """认领最早一条可发布的意图，并原子记账（状态转 ``publishing``、尝试次数 +1）。

        ``exclude`` 是**调用方本轮已经认领过**的那些 ``id``，一律跳过（Epic C 冻结缺陷
        F2）。它只作用在**候选**上，不作用在"该用户有没有更早的非终态兄弟行"那一条：
        被跳过的意图仍然是 ``pending``、仍然是非终态，因此仍然挡着同一用户的下一版——
        把它从兄弟行判据里一并排除，等于在同一轮里放行同一个人的两条意图，正是"同一用户
        单飞"要防的那件事。空序列时这个条件恒真（``id <> ALL('{}')``），语句形状不变。

        三道条件缺一不可：

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

        skipped = [str(item) for item in exclude]
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
                                AND c.id <> ALL(%s)
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
                    """,
                    (skipped,),
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

    def published_awaiting_readiness(
        self,
        *,
        reasons: Sequence[str],
        interval_seconds: int,
        budget_seconds: int,
        limit: int = 50,
    ) -> tuple[PendingReadiness, ...]:
        """取「已经发布读回一致、但就绪确认还没收口」的那些 ``(用户, 权限版本)``。

        这是 S-C-03b 的调度职责每轮 tick 的输入（见
        :mod:`lingxi.apps.scheduler.permission_publish`）。

        **``reasons`` 是必填的**：调用方必须说清"我负责确认哪一类意图"。它挡的不是数据
        错误，而是**两个编排者抢同一条意图**——首次开通那条链（``first_onboarding``，
        Epic D 的 ``OnboardingRunner``）自己会做就绪确认并发"开通完成"，如果每日刷新
        这一侧也把它捞起来确认一遍，用户会在"开通完成"之外再收到一条措辞完全不同的
        "可用范围已更新"，而且两个确认还会对同一个 ``(用户, 权限版本)`` 并发探针。
        做成必填而不是给一个默认值：默认值会在新增一种 ``reason`` 时**静默地**把它归给
        某一方，而那正是需要有人明确决定的事。

        **只取"这一轮真的该动"的那些**（二级审查 N3）。``interval_seconds`` /
        ``budget_seconds`` 由调用方从就绪节奏传进来，本方法据此算出"下一次探针什么时候
        到期"并只返回已经到期的行，**还没探过的排在最前面**。少了这一条，``LIMIT`` 窗口
        会被一批"要等三分钟才到期"的候选长期占满：新发布的那一条永远挤不进来，于是合同
        要求的"发布读回一致后立即探一次"名存实亡，收口时间也没有上界。到期时刻的算法与
        :func:`lingxi.core.permission.mcp_readiness.next_probe_due` 必须一致
        （``起点 + 已判定次数 × 间隔``，并以预算封顶——封顶那一步让"计划表已经用尽却
        还没收口"的那种行立刻被取回来收口，而不是再等一个间隔）。

        其余三条筛选各自都不能少：

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
        for label, value in (("轮询间隔", interval_seconds), ("总预算", budget_seconds)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{label}必须是正整数秒")
        wanted = [str(item) for item in reasons if str(item).strip()]
        if not wanted:
            raise ValueError("必须指明本调用方负责确认哪些 reason 的发布意图")
        terminal = sorted(item.value for item in TERMINAL_OUTCOMES)
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT o.user_id, o.permission_version, o.payload ->> 'permissions',
                          o.published_at
                     FROM publish_outbox o
                     LEFT JOIN LATERAL (
                            SELECT count(*) AS attempts, min(c.started_at) AS first_started_at
                              FROM mcp_sync_check c
                             WHERE c.user_id = o.user_id
                               AND c.permission_version = o.permission_version
                          ) progress ON TRUE
                    WHERE o.status = 'published'
                      AND o.reason = ANY(%(reasons)s)
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
                               AND c.result = ANY(%(terminal)s)
                          )
                      AND (
                            progress.attempts = 0
                         OR progress.first_started_at + make_interval(
                                secs => LEAST(
                                    progress.attempts * %(interval)s, %(budget)s
                                )::double precision
                            ) <= now()
                          )
                    ORDER BY (progress.attempts > 0), o.published_at, o.id
                    LIMIT %(limit)s""",
                {
                    "reasons": wanted,
                    "terminal": terminal,
                    "interval": interval_seconds,
                    "budget": budget_seconds,
                    "limit": limit,
                },
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

    def has_publish_footprint(self, user_id: str) -> bool:
        """这个用户在发布链上**有没有留下过足迹**：发布成功过，**或**当前还有意图在途。

        撤权那一侧的判据（`V-权限-08` 的刷新侧）。两半各自都不能少：

        - ``published``：我们真的往发布表写成功过一行，那一行现在还在（我们不删行），
          因此值得为他发一条把 ``permissions`` 清空的更新。
        - ``pending`` / ``publishing``：**还有一条尚未落地的授权意图**。不把它算进来的话
          （二级审查 N7 抓到的时间线）：昨天排的 granted 意图还堵在 pending，今天这个人
          被撤权却因为"还没发布过"而跳过，等发布面把积压消费掉时，那份**已经被收回的
          范围**会被写进外部表，还会触发一条"范围已更新"通知——最长多给一天权限。
          算进来之后，撤权决定推进版本，旧意图被认领时判 ``SUPERSEDED``（**一次外部
          调用都不发**），而撤权意图本身在外部无行时以 ``missing_token_cipher`` 失败
          关闭——两条路的方向都是安全的。

        反过来，**在发布链上一点足迹都没有**的人仍然跳过：为他新建一行空权限没有意义
        （问数 MCP 对查无此人本来就默认拒绝），而新建还需要一份令牌密文。既有 26 行那些
        旧系统写的人也落在这一路——硬切之前他们的撤权由旧系统负责（产品负责人 2026-08-18
        裁定 3 的时效边界）。

        ``failed`` / ``superseded`` **不算足迹**：前者说明那一版从来没有落到外部表，
        后者已经被更新的版本取代。
        """

        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("发布足迹判定必须指明用户")
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT 1 FROM publish_outbox
                    WHERE user_id = %s
                      AND status IN ('published', 'pending', 'publishing')
                    LIMIT 1""",
                (user_id,),
            )
            return cursor.fetchone() is not None

    def notice_recipient_open_id(self, user_id: str) -> str | None:
        """权限变化通知发给谁：该用户的 ``feishu_open_id``；不该发就返回 ``None``。

        **三条过滤是产品约束，不是编码习惯**，口径与花名册比对基线同源
        （``adapters/postgres_roster_audit.ACTIVE_BASELINE_SQL``）再叠加一条
        （Issue #468，2026-08-30）：

        - ``provisioning_state = 'active'``：还没完成开通的人不该收到"你的可用范围已更新"；
        - ``account_state = 'enabled'``：删除中/已删除的账号一律不发（删除编排会清空
          姓名等字段，给这样的账号发消息是数据范围外泄），管理员停用期间同样不发。
          花名册比对基线只服务日报/审计对比、不判定"该不该收通知"，两处口径此前恰好
          都只排除 ``deleting``/``deleted``——纯属巧合，不是同一份合同；本条只影响通知
          收件人，不影响 :class:`PostgresPermissionRefreshBaselineReader` 那份决定
          "这个人今天要不要被重新算一次权限"的独立判据（同一份缺口的另一半，见该类
          文档）。

          **Issue #483 把它从拒绝列表改成正向白名单**：``account_state`` 的 CHECK 约束
          今天只有 ``enabled``/``suspended``/``deleting``/``deleted`` 四个取值，两种
          写法**逐行等价**、行集合一个字不变；改的是演进方向——将来往 CHECK 里加第五个
          状态时，拒绝列表会静默把它当成"可以发通知"，白名单默认拒绝
          （codex 第 2 轮 P2 的"演进防御"处置，判据常量见
          :data:`~lingxi.core.permission.publish.ACCOUNT_STATE_ENABLED`）。

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
                      AND account_state = 'enabled'""",
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


#: 每日权限重算真正遍历的那一份基线（Issue #468）。**刻意不是**
#: ``adapters/postgres_roster_audit.ACTIVE_BASELINE_SQL``——那份服务花名册日报/审计
#: 对比，口径必须覆盖包括 ``suspended`` 在内的一切"还没删除"的已开通用户，否则一个人
#: 被停用期间，他的姓名/邮箱/工号漂移会从日报里悄悄消失（另一职责，不为本 Story 改）。
#: 发权批的正确口径与它在"删除中/已删除"这两态上重合、但必须**额外**排除
#: ``suspended``：停用是管理员对"这个人现在能不能用问数"的显式裁定，银河与花名册都
#: 不知道这件事——``PermissionRefreshDuty`` 逐人重算时只看得到银河那一侧还有效的角色，
#: 一旦把停用的人留在遍历集合里，第二天的批量重算会重新聚合出这个人的银河权限、
#: 翻译、结算发布行，把停用当天已经就地清空的那条发布行重新写回一份真实权限——
#: 停用承诺被静默突破（`V-权限-08` 撤权侧原本只管"银河没权限"，没有覆盖"银河有权限、
#: 但管理员在应用层单独按停"这一种情形）。
#:
#: **正向白名单，不是拒绝列表**（Issue #483，codex 第 2 轮 P2）：``account_state`` 的
#: CHECK 约束今天只有四个取值，``= 'enabled'`` 与原来的
#: ``NOT IN ('deleting','deleted','suspended')`` **逐行等价**、行集合一个字不变；改的
#: 是演进方向——将来往 CHECK 里加第五个状态时，拒绝列表会静默把它当成"可以发权"。
#: 判据常量见 :data:`~lingxi.core.permission.publish.ACCOUNT_STATE_ENABLED`。
#: ``adapters/postgres_roster_audit.ACTIVE_BASELINE_SQL`` **刻意不跟着改**：它
#: **有意包含** ``suspended``（上一段），改成白名单会改变它的行为。
PERMISSION_REFRESH_BASELINE_SQL = """
SELECT id, feishu_user_id, display_name, employee_no, email
  FROM app_user
 WHERE provisioning_state = 'active'
   AND account_state = 'enabled'
 ORDER BY id
"""


class PostgresPermissionRefreshBaselineReader:
    """每日权限重算（:class:`~lingxi.apps.scheduler.permission_refresh.
    PermissionRefreshDuty`）专用的基线读取，实现
    :class:`~lingxi.apps.scheduler.permission_refresh._BaselineReader` 协议。

    行形状与 :class:`~lingxi.adapters.postgres_roster_audit.
    PostgresRosterBaselineReader` 逐字段相同（同样是 :class:`ArchivedIdentity`
    的五个字段），**唯一差别是过滤条件多排除一个 `suspended`**（模块级常量
    :data:`PERMISSION_REFRESH_BASELINE_SQL` 顶部注释）。

    **为什么不直接复用 ``PostgresRosterBaselineReader``**（Issue #468 修复之前，
    ``permission_refresh.py`` 的 ``_BaselineReader`` 协议文档原话是"复用而不是照抄
    它的 SQL……第二份实现迟早会与它分叉，而分叉的方向可能是给一个正在删除的用户
    重新发了权限"——这条顾虑本身没有错，只是没预见到分叉也可能发生在**另一个
    方向**：两个调用方现在需要的过滤**不再相同**，继续共用同一条 SQL 才是那个真正
    会把停用用户重新发权限的缺口）：

    - 花名册审计（日报/审计对比）需要包含 ``suspended`` 用户——停用是可逆的行政
      动作，这段时间里他的花名册字段照样可能漂移，日报不该因为他被停用就对这类
      漂移视而不见；
    - 发权每日批不能包含 ``suspended`` 用户——停用当天已经由 ``suspend_user``
      即时撤销路径清空过一次发布内容（`adapters/postgres_permission_recompute_
      trigger.py` 的 ``force_revoke``），次日批量重算如果还把这个人算进遍历集合，
      银河那一侧完全不知道"停用"这件事、会照常聚合出他的有效权限并重新发布，
      停用承诺就在数据库层面被这一条批处理悄悄推翻。

    两条查询因此**必须**各自独立、各自可以按自己的产品口径演进，不能共用一份
    "看起来一样"的 SQL——这正是本类存在的理由，也是本类没有做成
    ``PostgresRosterBaselineReader`` 的一个参数化选项的理由：参数化会让两条互相
    独立的产品判据在同一处代码里耦合，下一次任何一条口径变化都要先确认"会不会
    影响另一边"。
    """

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        self._dsn = dsn
        self._timeouts = timeouts

    def load_active_baseline(self) -> tuple[ArchivedIdentity, ...]:
        """返回本轮重算集。只取五列：内部标识、人员 ID 与存档三字段——与
        ``PostgresRosterBaselineReader.load_active_baseline`` 取的列逐字段相同，
        只是行集合按 :data:`PERMISSION_REFRESH_BASELINE_SQL` 多排除 ``suspended``。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(PERMISSION_REFRESH_BASELINE_SQL)
            rows = cursor.fetchall()

        baseline = tuple(
            ArchivedIdentity(
                app_user_id=str(row[0]),
                personnel_id=_text(row[1]),
                display_name=_text(row[2]),
                employee_no=_text(row[3]),
                email=_text(row[4]),
            )
            for row in rows
        )
        # 只记条数，同 `PostgresRosterBaselineReader`（`V-花名册-33`：审计与日志
        # 不含花名册字段值）。
        logger.info("每日权限重算基线已读取 已开通且未停用用户=%s", len(baseline))
        return baseline


def _text(value: object) -> str:
    """``NULL`` 与空白归一为空串。与 ``postgres_roster_audit._text`` 同一姿态——
    各自一份是因为两个模块刻意不互相 import 对方的私有辅助函数。"""

    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _same_content(payload: Any, digest: Any, row: PublishRow) -> bool:
    """上一条意图的内容与这一次的决定是否逐字段相同（``updated_at`` 不参与）。

    **优先用摘要列，摘要列活过内容擦除**（Trace #544 P-3）：``payload`` 过了九十天会
    被 :meth:`PostgresPermissionPublishStore.redact_expired_payloads` 擦成 ``'{}'``，
    此后按 payload 比较必然判成"变了"——一份内容完全没变的权限于是被重排一条发布意图，
    并连带触发 :func:`_permissions_changed` 那一侧的记忆与已送达正文清空。摘要列不参与
    擦除，因此擦除之后这个判断仍然成立。

    ``digest`` 为 ``NULL`` 的只有**迁移 0085 之前就已经被擦除**的历史行（没有内容可
    回填）：那时退回原来的 payload 比较，行为与本次改动之前逐字相同——不为了新判据去
    编造一个说不出来的答案。

    payload 比较前先把两边都归一成 ``{字段名: 文本}``：payload 从 JSONB 回来时数字会
    变成 Python 数字，而发布行永远是文本——按类型严格相等会让一次没有变化的刷新被判成
    变化。
    """

    if isinstance(digest, str) and digest:
        return digest == content_digest(row.content_fields)
    if not isinstance(payload, Mapping):
        return False
    expected = row.content_fields
    return all(str(payload.get(name, "")) == value for name, value in expected.items())


def _permissions_changed(payload: Any, digest: Any, row: PublishRow) -> bool:
    """已送达正文清理触发的专属判据（Trace #328 opus 审查 P1）：这次决定的
    ``permissions`` 文本是否与该用户**上一条意图**（不论其状态——失败、被取代、
    仍然有效都一样，见 :meth:`PostgresPermissionPublishStore.record_decision`
    文档第 4 步）不同。

    刻意不复用 :func:`_same_content`：那个函数比较的是**整行**（含
    ``record_key``/``email``/``name``），服务的是 ENQUEUED/UNCHANGED 判定——回答
    "要不要排一条新的发布意图"；这里回答的是完全不同的问题——"这个人**实际可用
    权限**变了吗"，只有这一个答案能决定要不要清空已送达正文。改名（``email``/
    ``display_name`` 变化）或对同一份权限内容的重试发布都会让前者判真，但都不该
    触发清理——分开两个函数，让"哪个判定服务哪个决定"在类型上说得清楚，不是靠
    调用方记得只取子集字段。

    **同样优先用摘要列**（Trace #544 P-3）：这一侧的误判后果最重——擦除之后按空 payload
    比较恒判"变了"，于是把该用户的 ``user_memory`` 与全部会话已送达正文清空，用户侧
    表现为"什么都没做，记忆和历史答案没了"。``digest`` 为 ``NULL``（迁移 0085 之前
    就已擦除的历史行）时退回原来的 payload 比较，行为逐字不变。
    """

    if isinstance(digest, str) and digest:
        return digest != permissions_digest(row.content_fields)
    if not isinstance(payload, Mapping):
        return True
    return str(payload.get("permissions", "")) != row.permissions


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
