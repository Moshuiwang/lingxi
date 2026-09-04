"""管理员确认卡执行成功后定位「要对哪个内部用户做定向重算」的两条只读查询。

单独成一个模块，不并进 ``postgres_pending_action.py``：那个文件是
``pending_action`` 状态机的唯一真实实现，本模块只做**事后**的只读查询，与
状态机写入路径没有任何耦合。**两种目标标识，两条查询**：
``PendingAction.target_open_id`` 这个字段名具有误导性——它只在
``SUSPEND_USER``/``RESUME_USER`` 两类动作里才真的是飞书 ``open_id``，对本地
权限三类动作装的是 ``local_permission_override.id``；两条查询各自的判据见
:func:`resolve_open_id_target` 与 :func:`resolve_local_override_target`。
"""

from __future__ import annotations

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect


def resolve_open_id_target(
    dsn: str, open_id: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS
) -> str | None:
    """``feishu_open_id`` → ``app_user.id``。

    查无此人（账号已被删除编排清走，或从未建档）返回 ``None``——调用方按
    "跳过，审计说明原因"处理，不是异常。
    """
    with connect(dsn, timeouts=timeouts) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT id FROM app_user WHERE feishu_open_id = %s", (open_id,))
        row = cursor.fetchone()
    return str(row[0]) if row is not None else None


def resolve_local_override_target(
    dsn: str, pending_action_id: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS
) -> str | None:
    """按触发本次回调的 ``pending_action_id`` 定位这条本地权限覆盖归属的用户。

    ``local_permission_override``（迁移 ``0072``）的
    ``pending_action_id``/``revoked_pending_action_id`` 两列分别记录"创建"与
    "收回"这条覆盖的确认卡，都指向一个真实存在过的 ``pending_action.id``——
    两列各查一次并集，对 grant/suppress/revoke 三种动作类型是同一条查询、不
    需要分支。结构上应当恰好命中一行；查无该行返回 ``None``（伪造或已被清理
    的回调，调用方按跳过处理，不抛异常）。
    """
    with connect(dsn, timeouts=timeouts) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT user_id FROM local_permission_override"
            " WHERE pending_action_id = %s OR revoked_pending_action_id = %s"
            " LIMIT 1",
            (pending_action_id, pending_action_id),
        )
        row = cursor.fetchone()
    return str(row[0]) if row is not None else None


__all__ = ["resolve_local_override_target", "resolve_open_id_target"]
