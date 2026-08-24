"""管理员角色登记表（``admin_registry``，迁移 ``0067``）的唯一 PostgreSQL 落点。

三个职责：

1. :class:`PostgresAdminRegistryLookup` —— 供 :class:`lingxi.core.admin.router.
   AdminCommandRouter` 注入的默认拒绝判定端口，**每次调用都是一条新查询**，不持有
   连接、不缓存结果（"角色收回后新请求立即拒绝"这条不变量的数据库侧兑现）。
2. :class:`PostgresAdminQueries` —— 只读查询命令组的两条查询实现，只读 `app_user`
   与 `inbound_event`，不提供任意 SQL 拼接入口。
3. :func:`seed_admin_registry_entry` —— 唯一的写路径，供
   ``apps/admin_bootstrap`` 一次性种子命令调用；幂等（同一 open_id 已有 active
   条目时不重复插入），本 Story 之外没有任何其它写入口。
"""

from __future__ import annotations

from typing import Sequence

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.admin.registry import ALL_ADMIN_ROLES, AdminRegistryEntry, AdminRole
from lingxi.core.admin.views import AdminEventView, AdminUserStatusView
from lingxi.core.ids import new_id


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


class PostgresAdminQueries:
    """``AdminQueries`` 端口的真实实现：只读 ``app_user``/``inbound_event``。"""

    def __init__(
        self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS
    ) -> None:
        self._dsn = dsn
        self._timeouts = timeouts

    def user_status(self, *, identifier: str) -> AdminUserStatusView | None:
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT provisioning_state, account_state, permission_version, updated_at
                  FROM app_user
                 WHERE feishu_open_id = %s
                """,
                (identifier,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        provisioning_state, account_state, permission_version, updated_at = row
        return AdminUserStatusView(
            identifier=identifier,
            provisioning_state=provisioning_state,
            account_state=account_state,
            permission_version=permission_version,
            updated_at=_isoformat(updated_at),
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
    roles: frozenset[AdminRole] = ALL_ADMIN_ROLES,
    timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS,
) -> bool:
    """幂等地登记一条管理员条目。

    ``ON CONFLICT`` 的推断目标精确匹配迁移 ``0067`` 的部分唯一索引
    ``admin_registry_active_identity_idx``：同一 ``feishu_open_id`` 已存在一条
    ``entry_status='active'`` 的行时本次不插入、不覆盖，返回 ``False``——**不存在
    覆盖既有条目的路径**，换角色或撤销是 S-M-02 的写动作范围，本函数只负责"从零到
    一"的首次播种。

    这是本表**唯一**的写入口：不提供任何更新已有行的路径。调用方（
    ``apps/admin_bootstrap``）负责取得不含真实值的 ``feishu_open_id``（读取
    ``feishu_delegated_subject`` 登记表，见 Issue #137 同一识别机制），本函数本身
    不关心它从哪里来，也不做任何格式假设之外的校验。
    """

    if not feishu_open_id or not feishu_open_id.strip():
        raise ValueError("feishu_open_id 不能为空")
    if not label or not label.strip():
        raise ValueError("label 不能为空")

    row_id = new_id("adm")
    with connect(dsn, timeouts=timeouts) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO admin_registry
                (id, feishu_open_id, label, permission_admin_granted,
                 ops_admin_granted, super_admin_granted, entry_status)
            VALUES (%s, %s, %s, %s, %s, %s, 'active')
            ON CONFLICT (feishu_open_id) WHERE entry_status = 'active' DO NOTHING
            RETURNING id
            """,
            (
                row_id,
                feishu_open_id.strip(),
                label.strip(),
                AdminRole.PERMISSION_ADMIN in roles,
                AdminRole.OPS_ADMIN in roles,
                AdminRole.SUPER_ADMIN in roles,
            ),
        )
        inserted = cursor.fetchone() is not None
    return inserted
