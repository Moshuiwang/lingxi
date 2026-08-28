"""管理员角色登记表（``admin_registry``，迁移 ``0067``）的唯一 PostgreSQL 落点。

三个职责：

1. :class:`PostgresAdminRegistryLookup` —— 供 :class:`lingxi.core.admin.router.
   AdminCommandRouter` 注入的默认拒绝判定端口，**每次调用都是一条新查询**，不持有
   连接、不缓存结果（"角色收回后新请求立即拒绝"这条不变量的数据库侧兑现）。
2. :class:`PostgresAdminQueries` —— 只读查询命令组的查询实现，只读 `app_user`/
   `inbound_event`/`local_permission_override`（后者只为 `user_status()` 回显
   「当前生效本地覆盖」段，#319 S-P-1b 卡 B）/`onboarding_failure`（`trace_lookup()`
   回显失败原因，Issue #337 迁移 `0077`），不提供任意 SQL 拼接入口。
3. :func:`seed_admin_registry_entry` —— 唯一的写路径，供
   ``apps/admin_bootstrap`` 一次性种子命令调用；幂等（同一 open_id 已有 active
   条目时不重复插入），本 Story 之外没有任何其它写入口。
"""

from __future__ import annotations

from typing import Sequence

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.adapters.postgres_local_permission import PostgresLocalPermissionOverrideStore
from lingxi.adapters.postgres_onboarding_failure import fetch_failure_reason
from lingxi.core.admin.registry import (
    AdminRegistryEntry,
    AdminRegistrySeedConflict,
    AdminRole,
)
from lingxi.core.admin.views import (
    AdminEventView,
    AdminTraceView,
    AdminUserStatusView,
    LocalPermissionOverrideView,
)
from lingxi.core.ids import new_id


#: 从一行 ``SELECT feishu_open_id, label, permission_admin_granted,
#: ops_admin_granted, super_admin_granted, entry_status FROM admin_registry ...``
#: 构造 :class:`AdminRegistryEntry`——列的顺序与取值形状是本模块与调用方之间的唯一
#: 契约。导出（不加下划线前缀）供 ``adapters/postgres_pending_action.py`` 复用：
#: 那个模块的 ``confirm()`` 需要在**自己的连接、自己的事务**里对这张表加
#: ``FOR SHARE`` 重新查一次（避免独立注入的登记表查询端口带来的 TOCTOU 窗口，外部
#: 审查交叉裁定，codex P1-4），SQL 本身必须留在调用方以便携带那个连接特有的锁子句，
#: 但"行怎么解析成值对象"这段逻辑不必因此复制第二份。
def admin_registry_entry_from_row(row: tuple) -> AdminRegistryEntry:
    found_open_id, label, permission_granted, ops_granted, super_granted, entry_status = row
    roles: set[AdminRole] = set()
    if permission_granted:
        roles.add(AdminRole.PERMISSION_ADMIN)
    if ops_granted:
        roles.add(AdminRole.OPS_ADMIN)
    if super_granted:
        roles.add(AdminRole.SUPER_ADMIN)
    return AdminRegistryEntry(
        feishu_open_id=found_open_id,
        label=label,
        roles=frozenset(roles),
        entry_status=entry_status,
    )


class PostgresAdminRegistryLookup:
    """``AdminRegistryLookup`` 端口的真实实现。"""

    def __init__(
        self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS
    ) -> None:
        self._dsn = dsn
        self._timeouts = timeouts

    def active_entry(self, *, open_id: str) -> AdminRegistryEntry | None:
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT feishu_open_id, label, permission_admin_granted,
                       ops_admin_granted, super_admin_granted, entry_status
                  FROM admin_registry
                 WHERE feishu_open_id = %s AND entry_status = 'active'
                """,
                (open_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return admin_registry_entry_from_row(row)


class PostgresAdminQueries:
    """``AdminQueries`` 端口的真实实现：只读 ``app_user``/``inbound_event``/
    ``local_permission_override``（#319 S-P-1b 卡 B：``/admin user`` 新增
    「当前生效本地覆盖」段，是 ``/admin revoke_permission`` 的 UX 前置）。"""

    def __init__(
        self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS
    ) -> None:
        self._dsn = dsn
        self._timeouts = timeouts
        # 复用已有的读路径（``effective_entries``），不在本类里重新拼一遍同样的
        # SQL——见 ``core/admin/pending_action.py`` 与卡 B 设计卡「数据经
        # PostgresLocalPermissionOverrideStore.effective_entries（已有）」。
        self._local_overrides = PostgresLocalPermissionOverrideStore(dsn, timeouts=timeouts)

    def user_status(self, *, identifier: str) -> AdminUserStatusView | None:
        # 登记式收敛（#371-8，2026-08-28 复审订正——下面这段原始评估被 Trace #373
        # H1 批终修复包 opus 审查 P2-9 证明是伪二选一，订正见后半段）：
        # `PostgresAppUserStore`（postgres_identity.py）没有一个现成方法能一次原子
        # 查询出本方法需要的 (id, provisioning_state, account_state,
        # permission_version, updated_at) 五列——`get_by_open_id` 按 open_id 查但
        # 不带 account_state/updated_at，`read_status` 带 account_state 但按内部
        # user_id 查、且不返回 updated_at。拆成"先 get_by_open_id 拿 id，再
        # read_status 查状态"两次查询会丢掉单条 SELECT 的原子快照、多一次往返；
        # 给 core 的 UserProvisioningStatus 加 updated_at 会改动 onboarding_runner
        # 等其它消费方共享的形状。
        #
        # 订正：上面只权衡了"改 PostgresAppUserStore 的两种方式"，漏掉了第三条
        # 路——`apps/trace/__init__.py::_fetch_user` 存在逐字节相同的这条 SELECT
        # （同一张表、同一组列、同一个 WHERE），抽一个两边共用的只读 helper 本可
        # 行为等价地消掉这处重复，原「两条路径都不是……因此保留」的结论是伪二选一。
        # 本轮仍不抽取：`apps/trace` 的内联 SQL 刚在同一批里按产品负责人裁定登记为
        # 受控只读 CLI 例外（`docs/技术设计/代码框架.md` §二「在案例外」，
        # 2026-08-28，Issue #371）——这时候抽公共 helper 会让 trace 侧不再是"内联
        # SQL"，部分推翻刚落的登记，在同一批修复包里自相矛盾，因此本轮接受这一处
        # 重复。复议条件：下一次拆分批一并评估抽取两边共用的只读 helper，并同步
        # 修订 `docs/技术设计/代码框架.md` §二对应的例外条目（届时如果真的抽取，
        # trace 那条例外要么撤销，要么改写为"仅其余 3 条内联 SELECT"）。
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, provisioning_state, account_state, permission_version, updated_at
                  FROM app_user
                 WHERE feishu_open_id = %s
                """,
                (identifier,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        user_id, provisioning_state, account_state, permission_version, updated_at = row
        # 独立的一次读（``effective_entries`` 自己开连接）：只读查询不需要与上面
        # 这次 ``app_user`` 读共享事务，见该方法文档"供 S-P-3 聚合复用的读路径"。
        local_overrides = tuple(
            LocalPermissionOverrideView(
                override_id=stored.id,
                direction=stored.entry.direction.value,
                company_id=stored.entry.company_id,
                metric_name=stored.entry.metric_name,
                reason=stored.entry.reason,
                created_at=_isoformat(stored.entry.created_at),
            )
            for stored in self._local_overrides.effective_entries(user_id=user_id)
        )
        return AdminUserStatusView(
            identifier=identifier,
            provisioning_state=provisioning_state,
            account_state=account_state,
            permission_version=permission_version,
            updated_at=_isoformat(updated_at),
            local_overrides=local_overrides,
        )

    def recent_events(
        self, *, identifier: str | None, window_hours: int, limit: int
    ) -> Sequence[AdminEventView]:
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            if identifier:
                cursor.execute(
                    """
                    SELECT received_at, event_type, handled_as, trace_id
                      FROM inbound_event
                     WHERE user_open_id = %s
                       AND received_at >= now() - make_interval(hours => %s)
                     ORDER BY received_at DESC
                     LIMIT %s
                    """,
                    (identifier, window_hours, limit),
                )
            else:
                cursor.execute(
                    """
                    SELECT received_at, event_type, handled_as, trace_id
                      FROM inbound_event
                     WHERE received_at >= now() - make_interval(hours => %s)
                     ORDER BY received_at DESC
                     LIMIT %s
                    """,
                    (window_hours, limit),
                )
            rows = cursor.fetchall()
        return tuple(
            AdminEventView(
                received_at=_isoformat(received_at),
                event_type=event_type,
                handled_as=handled_as,
                trace_id=trace_id,
            )
            for received_at, event_type, handled_as, trace_id in rows
        )

    def trace_lookup(self, *, trace_id: str) -> AdminTraceView | None:
        """``/admin trace <追溯号>`` 的查询实现（Issue #337）。

        「按 trace_id 查 inbound_event」这条 SELECT 与 ``apps/trace/__init__.py::
        _fetch_events`` 的既有查询逐字节同型（同一张表、同一组列、同一个
        WHERE）——与本类 :meth:`user_status` 文档字符串登记的既有重复同一性质：
        ``apps/trace`` 的内联 SQL 已经在 2026-08-28（Issue #371）被产品负责人
        登记为「受控只读 CLI」的架构例外，本轮不重新抽取共用 helper（会推翻同一批
        刚落的登记，见该例外的复议条件）。本方法只负责"给定追溯号，查一次
        ``inbound_event``"，不 import ``apps.trace``——``core``/``adapters`` 层
        不依赖 ``apps`` 模块（代码框架第二/三节）。

        「开通状态」复用既有的 :meth:`user_status`（同一个已装配的适配器实例，
        不重新拼一遍 ``app_user`` 查询）；「失败原因」复用
        ``adapters/postgres_onboarding_failure.fetch_failure_reason``（写入方
        :class:`~lingxi.adapters.postgres_onboarding_failure.
        PostgresFailureReasonRecorder` 是同一张表的唯一写入口）。

        取「最近一条」事件的 ``event_type``/``handled_as``/``onboarding_
        dispatched_at`` 展示：同一个 trace_id 结构上通常只对应一条入站事件
        （首次开通首聊），取最近一条只是为了在理论上出现多条时不崩溃，不是
        本命令承诺聚合展示全部历史事件——完整时间线仍然是 ``/admin audit``
        与 ``python -m lingxi.apps.trace`` 的职责。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT received_at, event_type, handled_as, user_open_id,
                       onboarding_dispatched_at
                  FROM inbound_event
                 WHERE trace_id = %s
                 ORDER BY received_at
                """,
                (trace_id,),
            )
            rows = cursor.fetchall()
        if not rows:
            return None

        event_count = len(rows)
        first_received_at = _isoformat(rows[0][0])
        _, last_event_type, last_handled_as, last_open_id, last_dispatched_at = rows[-1]

        status = self.user_status(identifier=last_open_id) if last_open_id else None
        failure = fetch_failure_reason(self._dsn, trace_id=trace_id, timeouts=self._timeouts)

        return AdminTraceView(
            trace_id=trace_id,
            event_count=event_count,
            first_received_at=first_received_at,
            last_event_type=last_event_type,
            last_handled_as=last_handled_as,
            dispatched=last_dispatched_at is not None,
            provisioning_state=status.provisioning_state if status is not None else None,
            account_state=status.account_state if status is not None else None,
            failure_reason=failure.failure_reason if failure is not None else None,
            failure_event_type=failure.event_type if failure is not None else None,
            failure_occurred_at=failure.occurred_at if failure is not None else None,
        )


def _isoformat(value: object) -> str:
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


def seed_admin_registry_entry(
    dsn: str,
    *,
    feishu_open_id: str,
    label: str,
    timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS,
) -> bool:
    """幂等地登记一条管理员条目。三类角色固定合并授予，不接受角色子集。

    **不再接受 ``roles`` 入参**（opus 批量审查 P2）：PM 2026-08-24 终裁「三类角色
    合并授予」不该是调用方"记得传 `ALL_ADMIN_ROLES`"的自觉，而应该是这个函数
    结构上唯一能做到的事——本函数只负责"从零到一"的首次播种，播种的对象要么是
    三类角色齐全的管理员，要么不存在，没有第三种"部分角色"的中间态可以通过这个
    入口写出来。与迁移 ``0067`` 新增的 ``CHECK`` 是同一条终裁在两层的编码。

    ``ON CONFLICT`` 的推断目标精确匹配迁移 ``0067`` 的部分唯一索引
    ``admin_registry_active_identity_idx``：同一 ``feishu_open_id`` 已存在一条
    ``entry_status='active'`` 的行时本次不插入、不覆盖。**但"没插入"不能直接当成
    "幂等成功"**（opus 批量审查 P2）：那一行有可能是别的原因写进去的、字段并不是
    这次意图播种的内容（例如 label 不同，或——理论上——三类角色不全，尽管迁移
    ``0067`` 的 ``CHECK`` 已经在数据库层面挡住这种写入）。因此这里必须回读那一行、
    逐字段核验，核验不通过时抛 :class:`~lingxi.core.admin.registry.
    AdminRegistrySeedConflict`，调用方（``apps/admin_bootstrap``）据此非零退出并
    说明哪些字段不一致，而不是把一次真正的不一致误报成一次安静的成功。

    返回 ``True``：本次真的新插入了一行。返回 ``False``：已存在的行逐字段核验
    通过，是真正的幂等成功。

    这是本表**唯一**的写入口：不提供任何更新已有行的路径。调用方（
    ``apps/admin_bootstrap``）负责取得不含真实值的 ``feishu_open_id``（读取
    ``feishu_delegated_subject`` 登记表，见 Issue #137 同一识别机制），本函数本身
    不关心它从哪里来，也不做任何格式假设之外的校验。
    """

    if not feishu_open_id or not feishu_open_id.strip():
        raise ValueError("feishu_open_id 不能为空")
    if not label or not label.strip():
        raise ValueError("label 不能为空")

    normalized_open_id = feishu_open_id.strip()
    normalized_label = label.strip()

    row_id = new_id("adm")
    with connect(dsn, timeouts=timeouts) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO admin_registry
                (id, feishu_open_id, label, permission_admin_granted,
                 ops_admin_granted, super_admin_granted, entry_status)
            VALUES (%s, %s, %s, TRUE, TRUE, TRUE, 'active')
            ON CONFLICT (feishu_open_id) WHERE entry_status = 'active' DO NOTHING
            RETURNING id
            """,
            (row_id, normalized_open_id, normalized_label),
        )
        if cursor.fetchone() is not None:
            return True

        # 没插入：读回那一条已存在的 active 行，逐字段核验是不是真的"幂等成功"。
        # 查询条件精确匹配触发冲突的那个部分唯一索引（feishu_open_id +
        # entry_status='active'）——同一个索引保证这里至多读回一行，不需要
        # LIMIT，也不会因为历史撤销行而读错对象。
        cursor.execute(
            """
            SELECT label, permission_admin_granted, ops_admin_granted,
                   super_admin_granted
              FROM admin_registry
             WHERE feishu_open_id = %s AND entry_status = 'active'
            """,
            (normalized_open_id,),
        )
        existing = cursor.fetchone()

    if existing is None:
        # 结构上不应该发生：ON CONFLICT 命中即说明上面那一刻这一行存在，而本表
        # 唯一的写入口就是本函数本身，两条语句之间没有任何删除路径。响亮失败，
        # 好过把"读不到"悄悄当成某种默认结论。
        raise AdminRegistrySeedConflict(mismatched_fields=("row_disappeared_between_statements",))

    # 不再核对 entry_status：上面那条 SELECT 已经用 `entry_status = 'active'`
    # 过滤，读到行就意味着它是 'active'，再比一次是永远为真的死分支。
    existing_label, permission_granted, ops_granted, super_granted = existing
    mismatched: list[str] = []
    if existing_label != normalized_label:
        mismatched.append("label")
    if not (permission_granted and ops_granted and super_granted):
        mismatched.append("roles")
    if mismatched:
        raise AdminRegistrySeedConflict(mismatched_fields=tuple(mismatched))
    return False
