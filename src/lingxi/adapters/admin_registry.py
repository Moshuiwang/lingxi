"""管理员角色登记表（``admin_registry``，迁移 ``0067``）的唯一 PostgreSQL 落点。

三个职责：

1. :class:`PostgresAdminRegistryLookup` —— 供 :class:`lingxi.core.admin.router.
   AdminCommandRouter` 注入的默认拒绝判定端口，**每次调用都是一条新查询**，不持有
   连接、不缓存结果（"角色收回后新请求立即拒绝"这条不变量的数据库侧兑现）。
2. :class:`PostgresAdminQueries` —— 只读查询命令组的查询实现，只读 `app_user`/
   `inbound_event`/`local_permission_override`（后者只为 `user_status()` 回显
   「当前生效本地覆盖」段，#319 S-P-1b 卡 B），不提供任意 SQL 拼接入口。
3. :func:`seed_admin_registry_entry` —— 唯一的写路径，供
   ``apps/admin_bootstrap`` 一次性种子命令调用；幂等（同一 open_id 已有 active
   条目时不重复插入），本 Story 之外没有任何其它写入口。
"""

from __future__ import annotations

from typing import Sequence

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.adapters.postgres_local_permission import PostgresLocalPermissionOverrideStore
from lingxi.core.admin.registry import (
    AdminRegistryEntry,
    AdminRegistrySeedConflict,
    AdminRole,
)
from lingxi.core.admin.views import (
    AdminEventView,
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
