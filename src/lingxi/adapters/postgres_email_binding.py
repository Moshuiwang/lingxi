"""``app_user`` 邮箱绑定的只读回读口。

:class:`PostgresEmailBindingSource` 是
``core.identity.onboarding_ports.EmailBindingSource`` 的真实实现：按规范化邮箱
查出所有已绑定该邮箱的 ``app_user.id``，供
``reject_email_bound_to_another_person`` 判定「这个邮箱是不是已经绑给另一个人」。

单独一个模块，不挂在 ``postgres_identity`` 上：那个类同时是建档写侧与状态推进口，
这道闸是**只读**的、存在的理由是"不信任写侧此刻的不变式"。**与迁移 ``0085``
的口径必须逐字一致**：那条迁移建的是
``app_user (lower(btrim(email))) WHERE email IS NOT NULL AND btrim(email) <> ''``
上的部分唯一索引，本模块的 ``WHERE`` 子句用同一个表达式，改任一侧都必须同时
改另一侧。
"""

from __future__ import annotations

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.identity.onboarding_ports import EmailBinding


class PostgresEmailBindingSource:
    """``EmailBindingSource`` 的 PostgreSQL 实现。**只读**：本类没有任何写语句。"""

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        """记下 DSN 与超时配置；不在构造时连接数据库。"""
        self._dsn = dsn
        self._timeouts = timeouts

    def bindings_for_email(self, email: str) -> tuple[EmailBinding, ...]:
        """按规范化邮箱返回全部命中的 ``(app_user.id, feishu_open_id)``。

        入参已由调用方规范化；这里不再规范化一次，而是把同一个口径写进 SQL 的
        比较侧（``lower(btrim(email))``），两侧各归一各的，避免"应用层认为相等、
        数据库认为不等"的静默漏判。空字符串直接返回空元组、不发查询：没有邮箱
        就没有正式表行键，判它"与谁冲突"没有意义。读取失败原样抛出：调用方的
        合同是"读不到就让整条开通链失败关闭"，吞成空结果会让这道闸静默放行。
        """
        if not email:
            return ()
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                SELECT id, feishu_open_id
                  FROM app_user
                 WHERE email IS NOT NULL
                   AND btrim(email) <> ''
                   AND lower(btrim(email)) = %s
                """,
                (email,),
            )
            rows = cursor.fetchall()
        return tuple(EmailBinding(str(row[0]), row[1]) for row in rows)
