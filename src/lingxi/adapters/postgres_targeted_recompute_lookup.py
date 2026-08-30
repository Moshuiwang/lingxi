"""管理员确认卡执行成功后，把 ``PendingAction`` 定位到「要对哪个内部用户做定向
重算」的两条只读查询（Issue #438）。

**为什么单独成一个模块，不并进 ``postgres_pending_action.py``**：那个文件是
``pending_action`` 状态机（``prepare``/``confirm``/``cancel``）的唯一真实实现，
是另一张工作卡（#437）的独占领地；本模块只做**事后**的只读查询，与状态机写入
路径没有任何耦合，独立成文件既避免并行改动冲突，也让
``adapters/postgres_permission_recompute_trigger.py`` 的 import 闭包只拉进两条
简单 SELECT，不是整个状态机实现。

## 两种目标标识，两条查询

``PendingAction.target_open_id`` 这个字段名具有误导性——它只在 ``SUSPEND_USER``/
``RESUME_USER`` 两类动作里才真的是飞书 ``open_id``；对本地权限三类动作
（``LOCAL_PERMISSION_GRANT``/``SUPPRESS``/``REVOKE``），这一格装的是
``local_permission_override.id``（override_id，见 ``core/admin/pending_action.py``
模块文档「``_NOT_FOUND_MESSAGE``」一节的说明）。因此定位「这次动作影响的是哪个
内部用户（``app_user.id``）」必须按动作类型走两条不同的查询：

1. ``SUSPEND_USER``/``RESUME_USER``：``target_open_id`` 就是飞书 ``open_id``，
   直接按 ``app_user.feishu_open_id`` 查 ``id``——与
   ``adapters/postgres_pending_action.py``、``adapters/postgres_conversation/
   _transaction.py::lookup_user`` 已经各自独立写过的同一条查询同一姿态（这类
   单列 ``SELECT id FROM app_user WHERE feishu_open_id = %s`` 在仓库里已有至少
   两处独立实现，这里是第三处只读查询，不是新发明一种机制）。
2. 本地权限三类动作：``local_permission_override`` 表（迁移 ``0072``）的
   ``pending_action_id``/``revoked_pending_action_id`` 两列分别记录"创建这条
   覆盖的确认卡"与"收回这条覆盖的确认卡"，**都**指向一个真实存在过的
   ``pending_action.id``——比起解析 ``target_open_id``（对 grant/suppress 是
   override_id 本身，含义还要看 ``entry_status`` 才能确定该查哪一列），直接拿
   触发本次回调的 ``pending.id`` 去两列各查一次并集，对三种动作类型是同一条
   查询、不需要分支。
"""

from __future__ import annotations

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect


def resolve_open_id_target(
    dsn: str, open_id: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS
) -> str | None:
    """``feishu_open_id`` → ``app_user.id``。查无此人（账号已被删除编排清走，或
    从未建档）返回 ``None``——调用方按"跳过，审计说明原因"处理，不是异常。
    """

    with connect(dsn, timeouts=timeouts) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT id FROM app_user WHERE feishu_open_id = %s", (open_id,))
        row = cursor.fetchone()
    return str(row[0]) if row is not None else None


def resolve_local_override_target(
    dsn: str, pending_action_id: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS
) -> str | None:
    """按触发本次回调的 ``pending_action_id`` 定位这条本地权限覆盖归属的
    ``app_user.id``。结构上应当恰好命中一行（``pending_action_id``/
    ``revoked_pending_action_id`` 各自最多指向一条覆盖行的一次事件）；查无
    该行时返回 ``None``（伪造或已被清理的回调，调用方按跳过处理，不抛异常）。
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
