"""``app_user`` 邮箱绑定的只读回读口（rc25 S-2a，对抗审查 X-1）。

:class:`PostgresEmailBindingSource` 是
``core.identity.onboarding_ports.EmailBindingSource`` 的真实实现：按**规范化邮箱**
查出所有已经绑定这个邮箱的 ``app_user.id``，供
``core.identity.onboarding_guards.reject_email_bound_to_another_person`` 在开通链上
判定「这个邮箱是不是已经绑给另一个人了」。

## 为什么单独一个模块，而不是挂在 ``postgres_identity`` 上

``PostgresAppUserStore`` 同时是建档写侧（``IdentityProvisioning``）与状态推进口
（``UserStateStore``）。这道闸是**只读**的、且它存在的理由是"不信任写侧此刻的
不变式"——把它做成同一个类的第 N 个方法，会让"闸"和"被闸挡的写入"共用同一份
连接与同一份代码所有权。单独一个只读模块也让它可以被别的只读消费方（例如今后
的运维核对脚本）复用而不必拖进整个建档写侧。

## 与迁移 ``0085`` 的口径必须逐字一致

迁移 ``0085`` 建的是 ``app_user (lower(btrim(email))) WHERE email IS NOT NULL AND
btrim(email) <> ''`` 上的部分唯一索引。本模块的 ``WHERE`` 子句用同一个表达式，
因此这条查询能走那个索引，也保证"应用层判为冲突"与"数据库判为冲突"是同一个集合。
改任一侧都必须同时改另一侧，否则闸会与约束对不齐。
"""

from __future__ import annotations

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.identity.onboarding_ports import EmailBinding


class PostgresEmailBindingSource:
    """``EmailBindingSource`` 的 PostgreSQL 实现。**只读**：本类没有任何写语句。"""

    def __init__(
        self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS
    ) -> None:
        self._dsn = dsn
        self._timeouts = timeouts

    def bindings_for_email(self, email: str) -> tuple[EmailBinding, ...]:
        """按规范化邮箱返回全部命中的 ``(app_user.id, feishu_open_id)``。

        入参已由调用方按 ``normalize_email``（去首尾空白 + 转小写）规范化；这里
        **不再规范化一次**，而是把同一个口径写进 SQL 的比较侧
        （``lower(btrim(email))``）——两侧各归一各的，才不会出现"应用层认为相等、
        数据库认为不等"的静默漏判。

        空字符串直接返回空元组、**不发查询**：没有邮箱就没有正式表行键，也不进
        迁移 ``0085`` 那条部分索引，判它"与谁冲突"没有意义（判定层同样先短路，
        这里再挡一次是为了让本类单独被复用时也守同一条口径）。

        读取失败**原样抛出**：调用方（``reject_email_bound_to_another_person``）
        的合同是"读不到就让整条开通链失败关闭"，把异常吞成空结果会让这道闸在
        数据库抖动时静默放行。
        """

        if not email:
            return ()
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
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
