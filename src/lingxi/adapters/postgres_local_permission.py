"""本地权限覆盖（``local_permission_override``，迁移 ``0072``）的唯一 PostgreSQL 落点。

两个职责：

1. :meth:`PostgresLocalPermissionOverrideStore.effective_entries` —— 供 S-P-3
   聚合复用的读路径：按用户取全部当前生效（``entry_status='active'``）条目，
   一次查询命中迁移 ``0072`` 的 ``local_permission_override_user_active_idx``。
   调用方把返回值里的 :attr:`StoredLocalPermissionOverride.entry` 逐条喂给
   :func:`lingxi.core.permission.local_override.resolve_local_overrides`。
2. :meth:`~.insert`/:meth:`~.revoke` —— 写路径，供 S-P-1b 的确认卡执行器调用。
   **本卡只交付这两个方法本身，不接调用方**——本仓库当前没有任何代码调用它们
   （AGENTS.md「不做卡外改动」）。

真正实现"没有确认卡不能写入"的是迁移 ``0072`` 的 ``pending_action_id NOT NULL``
外键（见该迁移文件头部「为什么 pending_action_id 是 NOT NULL」）；本模块的
:meth:`~.insert` 只是把这条约束包装成 Python 接口并在写入前完成字段校验，不重新
发明一层能够绕开该外键的应用层判断去代替它。S-P-1b 落地时，确认卡执行器必须
先在同一事务里核实"这次确认卡决策确实通过"，再调用 :meth:`~.insert`——本模块
不做也不能替它做那层判定（同一姿态见 ``adapters/postgres_pending_action.py`` 的
``_confirm_locked``）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.ids import new_id
from lingxi.core.permission.local_override import LocalPermissionOverrideEntry, OverrideDirection


class DuplicateActiveOverride(Exception):
    """同一用户同一极性同一公司同一指标已经有一条生效条目
    （迁移 ``0072`` 的 ``local_permission_override_active_unique_idx``）。

    翻译自 ``psycopg.errors.UniqueViolation``：调用方（S-P-1b 确认卡执行器）据此
    判定"这笔授权/抑制已经生效，不需要重复插入"，而不是让裸 ``IntegrityError``
    冒泡——与 ``adapters/postgres_pending_action.py`` 对同类约束（``prepare()`` 撞
    ``pending_action_single_pending_target_idx``）的处理同一姿态。
    """


@dataclass(frozen=True)
class StoredLocalPermissionOverride:
    """写路径/读路径共同的返回形状：数据库分配的行标识 + 纯类型内容。

    ``id`` 是数据库主键（不是 :class:`~lingxi.core.permission.local_override.
    LocalPermissionOverrideEntry` 的一部分——那个类型描述"一条生效覆盖的内容"，
    不描述"它在数据库里是哪一行"，这条边界与 ``PermissionAggregate`` 不携带任何
    行标识是同一姿态）。S-P-1b 的收回流程按这个 ``id`` 定位要撤销的具体行。
    """

    id: str
    entry: LocalPermissionOverrideEntry


_SELECT_COLUMNS = (
    "id, user_id, direction, company_id, metric_name, reason, initiated_by_open_id,"
    " pending_action_id, created_at"
)


def _row_to_stored(row: tuple) -> StoredLocalPermissionOverride:
    (
        id_,
        user_id,
        direction,
        company_id,
        metric_name,
        reason,
        initiated_by_open_id,
        pending_action_id,
        created_at,
    ) = row
    entry = LocalPermissionOverrideEntry(
        user_id=user_id,
        direction=OverrideDirection(direction),
        company_id=company_id,
        metric_name=metric_name,
        reason=reason,
        initiated_by_open_id=initiated_by_open_id,
        pending_action_id=pending_action_id,
        created_at=created_at,
    )
    return StoredLocalPermissionOverride(id=id_, entry=entry)


class PostgresLocalPermissionOverrideStore:
    """``local_permission_override`` 表的唯一真实实现。"""

    def __init__(
        self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS
    ) -> None:
        self._dsn = dsn
        self._timeouts = timeouts

    def effective_entries(self, *, user_id: str) -> tuple[StoredLocalPermissionOverride, ...]:
        """按用户取全部当前生效条目，供 S-P-3 聚合调用
        :func:`~lingxi.core.permission.local_override.resolve_local_overrides`。

        排序（``created_at, id``）只是为了让返回结果确定，不参与聚合语义——
        :func:`resolve_local_overrides` 对输入次序不敏感（集合运算）。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {_SELECT_COLUMNS} FROM local_permission_override"
                " WHERE user_id = %s AND entry_status = 'active'"
                " ORDER BY created_at, id",
                (user_id,),
            )
            rows = cursor.fetchall()
        return tuple(_row_to_stored(row) for row in rows)

    def insert(
        self,
        *,
        user_id: str,
        direction: OverrideDirection,
        company_id: str,
        metric_name: str,
        reason: str,
        initiated_by_open_id: str,
        pending_action_id: str,
        now: datetime | None = None,
    ) -> StoredLocalPermissionOverride:
        """插入一条新的生效本地覆盖条目。

        **先构造** :class:`~lingxi.core.permission.local_override.
        LocalPermissionOverrideEntry`（触发它的 ``__post_init__`` 全部字段校验），
        **再**发出 ``INSERT``——非法字段在这里就响亮失败，不会打到数据库
        （与 ``adapters/admin_registry.seed_admin_registry_entry`` 的"校验先于任何
        写入"同一姿态）。

        撞上迁移 ``0072`` 的部分唯一索引（同用户同极性同公司同指标已有生效条目）
        时转译为 :class:`DuplicateActiveOverride`，不让裸 ``IntegrityError`` 冒泡。
        一个不存在的 ``pending_action_id``（或已删除用户的 ``user_id``）会撞上该
        迁移的外键约束，本方法不额外捕获——那是"没有确认卡/没有这个用户就不能
        写入"这条结构性保证本身，让它以数据库原生异常的形式暴露，好过本方法悄悄
        把它也翻译成一个看似"正常业务分支"的返回值。
        """

        moment = now or datetime.now(timezone.utc)
        # 校验先于任何写入：direction/company_id/metric_name/reason/
        # initiated_by_open_id/pending_action_id 任意一项为空或形状不对，这里
        # 直接抛 ValueError，INSERT 语句从未发出。
        entry = LocalPermissionOverrideEntry(
            user_id=user_id,
            direction=direction,
            company_id=company_id,
            metric_name=metric_name,
            reason=reason,
            initiated_by_open_id=initiated_by_open_id,
            pending_action_id=pending_action_id,
            created_at=moment,
        )

        from psycopg.errors import UniqueViolation

        override_id = new_id("lpo")
        try:
            with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO local_permission_override
                        (id, user_id, direction, company_id, metric_name, reason,
                         initiated_by_open_id, pending_action_id, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        override_id,
                        entry.user_id,
                        entry.direction.value,
                        entry.company_id,
                        entry.metric_name,
                        entry.reason,
                        entry.initiated_by_open_id,
                        entry.pending_action_id,
                        entry.created_at,
                    ),
                )
        except UniqueViolation as error:
            raise DuplicateActiveOverride(
                "该用户在这个公司×指标上已经有一条同极性的生效本地覆盖"
            ) from error
        return StoredLocalPermissionOverride(id=override_id, entry=entry)

    def revoke(
        self,
        *,
        override_id: str,
        revoked_pending_action_id: str,
        now: datetime | None = None,
    ) -> bool:
        """把一条生效条目标记为 ``revoked``（同一行状态翻转，历史留痕，见迁移
        ``0072`` 文件头部「为什么用『同一行状态翻转』」）。

        条件更新 ``WHERE entry_status = 'active'``：目标条目不存在、或已经处于
        ``revoked`` 状态时影响 0 行，返回 ``False``——"收回一条已经不存在/已经
        被收回的条目"与"收回失败"是同一个结论，调用方（S-P-1b 确认卡执行器）
        据此判定这次收回是否真的改变了状态，而不是无条件当成成功（与
        ``adapters/postgres_pending_action.py`` 的 ``mark_card_delivered`` 类
        条件更新同一姿态：不满足前提时静默影响 0 行，由 ``rowcount`` 让调用方
        自行判断，不在这里猜测"0 行"应该是异常还是正常）。

        一个不存在的 ``revoked_pending_action_id`` 会撞上迁移 ``0072`` 的外键
        约束，与 :meth:`insert` 同一姿态，不在本方法内额外捕获或翻译。
        """

        moment = now or datetime.now(timezone.utc)
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE local_permission_override"
                " SET entry_status = 'revoked', revoked_at = %s,"
                "     revoked_pending_action_id = %s"
                " WHERE id = %s AND entry_status = 'active'",
                (moment, revoked_pending_action_id, override_id),
            )
            return cursor.rowcount == 1
