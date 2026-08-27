"""本地权限覆盖（``local_permission_override``，迁移 ``0072``）的唯一 PostgreSQL 落点。

两个职责：

1. :meth:`PostgresLocalPermissionOverrideStore.effective_entries` —— 供 S-P-3
   聚合复用的读路径：按用户取全部当前生效（``entry_status='active'``）条目，
   一次查询命中迁移 ``0072`` 的 ``local_permission_override_user_active_idx``。
   调用方把返回值里的 :attr:`StoredLocalPermissionOverride.entry` 逐条喂给
   :func:`lingxi.core.permission.local_override.resolve_local_overrides`——
   S-P-3 落地后这一步由 :class:`LocalOverrideEntryReader` 统一代劳（两个接线点
   共用同一份适配，见其类文档），调用方不必各自重写这行解包代码。
2. :meth:`~.insert`/:meth:`~.revoke` —— 写路径，供确认卡执行器调用（S-P-1b，
   #319）。真正实现"没有确认卡不能写入"的是迁移 ``0072`` 的
   ``pending_action_id NOT NULL`` 外键（见该迁移文件头部「为什么 pending_action_id
   是 NOT NULL」）；本模块只是把这条约束包装成 Python 接口并在写入前完成字段
   校验，不重新发明一层能够绕开该外键的应用层判断去代替它。

## 为什么 ``insert``/``revoke`` 拆成 ``_insert_locked``/``_revoke_locked`` + 公开包装

S-P-1b 落地后，``adapters/postgres_pending_action.py`` 的 ``_confirm_locked``
需要在**自己已经打开的那个事务、那个 cursor** 上执行这条 INSERT/UPDATE——"写行
先于审计"这条要求（同一姿态见 ``PostgresPendingActionStore.confirm`` 文档「同一
事务」一节）意味着这条写入不能另开一条独立连接，否则审计失败回滚时这条本地权限
覆盖行不会跟着回滚，产生"卡说没执行、库说已执行"的不一致——这正是 S-P-1a 遗留
下来需要补的一课。拆分手法与 ``postgres_pending_action.py`` 的
``confirm``/``_confirm_locked`` 相同：``_insert_locked``/``_revoke_locked`` 是
模块级函数（不是方法，因为它们只需要调用方已经持有的 ``cursor``，不需要
``self._dsn``/``self._timeouts``），供两处调用方复用：

1. 本类的公开 :meth:`~.insert`/:meth:`~.revoke`——各自开一条独立连接/事务，
   行为与拆分前逐字节相同（既有测试 ``tests/test_local_permission_postgres.py``
   不受影响）；
2. ``adapters/postgres_pending_action.py`` 的 ``_confirm_locked``——直接从模块
   导入这两个私有函数，传入自己事务内的 ``cursor``，让本地权限覆盖行与
   ``pending_action`` 终态更新、审计写入落在同一个数据库事务里。与
   ``adapters/postgres_pending_action.py`` 从 ``adapters/postgres_conversation``
   导入私有类型 ``_Transaction`` 是同一个跨模块复用惯例（见该文件的 import 与
   ``postgres_conversation`` 模块文档"既有调用点用到的名字全部保留"），不是本模块
   新开的先例。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.ids import new_id
from lingxi.core.permission.local_override import LocalPermissionOverrideEntry, OverrideDirection


class LocalOverrideEntryReader:
    """把 :class:`PostgresLocalPermissionOverrideStore` 的按用户读取口适配成
    S-P-3（Issue #319）两个调用点各自协议要求的形状：``effective_entries(*, user_id)
    -> Sequence[LocalPermissionOverrideEntry]``——纯类型，不带数据库分配的行标识。

    :meth:`PostgresLocalPermissionOverrideStore.effective_entries` 返回的是
    :class:`StoredLocalPermissionOverride`（``id`` + ``entry``），因为写路径
    （S-P-1b 的收回流程）需要那个 ``id`` 定位要撤销的具体行；S-P-3 的合并只关心
    条目内容，不需要、也不应该知道行标识——两个调用点
    （:mod:`lingxi.apps.scheduler.permission_refresh`、
    :mod:`lingxi.core.identity.onboarding_runner`）各自声明的协议因此只认
    :class:`~lingxi.core.permission.local_override.LocalPermissionOverrideEntry`。
    这个适配器是两处装配（``apps/scheduler/assembly.py``）共用的**唯一**一份，避免
    "从 ``StoredLocalPermissionOverride`` 解出 ``.entry``"这行代码在两处重复、
    迟早漂移。
    """

    def __init__(self, store: PostgresLocalPermissionOverrideStore) -> None:
        self._store = store

    def effective_entries(self, *, user_id: str) -> tuple[LocalPermissionOverrideEntry, ...]:
        return tuple(item.entry for item in self._store.effective_entries(user_id=user_id))


def local_override_reader(
    dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS
) -> LocalOverrideEntryReader:
    """装配层的一步到位入口：新建一个 store 并包上 :class:`LocalOverrideEntryReader`。

    两处装配（``apps/scheduler/assembly.py`` 的每日权限重算与首次开通编排）各自
    调用一次，避免"先建 store 再包一层适配"这两步各自重复一份、迟早漂移；store
    本身轻量、无状态，两处各自新建互不共享是刻意的（与文件里其余 Postgres 读写口
    同一惯例），本函数只是把这两步合成一步，不引入共享实例。
    """

    return LocalOverrideEntryReader(PostgresLocalPermissionOverrideStore(dsn, timeouts=timeouts))


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
        """插入一条新的生效本地覆盖条目，独立开一条连接/事务。

        **先构造** :class:`~lingxi.core.permission.local_override.
        LocalPermissionOverrideEntry`（触发它的 ``__post_init__`` 全部字段校验），
        **再**发出 ``INSERT``——非法字段在这里就响亮失败，不会打到数据库
        （与 ``adapters/admin_registry.seed_admin_registry_entry`` 的"校验先于任何
        写入"同一姿态）。实际 ``INSERT`` 由 :func:`_insert_locked` 执行（模块文档
        「为什么拆分」）；本方法只负责校验、开连接、生成主键。

        撞上迁移 ``0072`` 的部分唯一索引（同用户同极性同公司同指标已有生效条目）
        时 :func:`_insert_locked` 转译为 :class:`DuplicateActiveOverride`，不让
        裸 ``IntegrityError`` 冒泡。一个不存在的 ``pending_action_id``（或已删除
        用户的 ``user_id``）会撞上该迁移的外键约束，本方法不额外捕获——那是
        "没有确认卡/没有这个用户就不能写入"这条结构性保证本身，让它以数据库原生
        异常的形式暴露，好过本方法悄悄把它也翻译成一个看似"正常业务分支"的返回值。
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

        override_id = new_id("lpo")
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            _insert_locked(cursor, override_id=override_id, entry=entry)
        return StoredLocalPermissionOverride(id=override_id, entry=entry)

    def revoke(
        self,
        *,
        override_id: str,
        revoked_pending_action_id: str,
        now: datetime | None = None,
    ) -> bool:
        """把一条生效条目标记为 ``revoked``，独立开一条连接/事务（同一行状态翻转，
        历史留痕，见迁移 ``0072`` 文件头部「为什么用『同一行状态翻转』」）。

        条件更新 ``WHERE entry_status = 'active'``：目标条目不存在、或已经处于
        ``revoked`` 状态时影响 0 行，返回 ``False``——"收回一条已经不存在/已经
        被收回的条目"与"收回失败"是同一个结论，调用方据此判定这次收回是否真的
        改变了状态，而不是无条件当成成功（与 ``adapters/postgres_pending_action.py``
        的 ``mark_card_delivered`` 类条件更新同一姿态：不满足前提时静默影响 0 行，
        由 ``rowcount`` 让调用方自行判断，不在这里猜测"0 行"应该是异常还是正常）。
        实际 ``UPDATE`` 由 :func:`_revoke_locked` 执行（模块文档「为什么拆分」）。

        一个不存在的 ``revoked_pending_action_id`` 会撞上迁移 ``0072`` 的外键
        约束，与 :meth:`insert` 同一姿态，不在本方法内额外捕获或翻译。
        """

        moment = now or datetime.now(timezone.utc)
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            return _revoke_locked(
                cursor, override_id=override_id, revoked_pending_action_id=revoked_pending_action_id, moment=moment
            )


def _insert_locked(cursor, *, override_id: str, entry: LocalPermissionOverrideEntry) -> None:
    """在调用方已经持有的 ``cursor``（及其所在事务）上执行实际 ``INSERT``。

    模块级函数而不是实例方法：调用方（本类的 :meth:`~PostgresLocalPermissionOverrideStore.insert`
    与 ``adapters/postgres_pending_action.py`` 的 ``_confirm_locked``）都只需要
    传入自己已经打开的 ``cursor``，不需要 ``self._dsn``/``self._timeouts``——见
    模块文档「为什么拆分」。撞上迁移 ``0072`` 的部分唯一索引时转译为
    :class:`DuplicateActiveOverride`，两处调用方按各自需要处理这个异常（本类的
    ``insert()`` 直接让它冒泡；``_confirm_locked`` 捕获后降级为
    ``ConfirmResultKind.TARGET_DRIFTED``，见该方法文档）。
    """

    from psycopg.errors import UniqueViolation

    try:
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


def _revoke_locked(
    cursor, *, override_id: str, revoked_pending_action_id: str, moment: datetime
) -> bool:
    """在调用方已经持有的 ``cursor``（及其所在事务）上执行实际收回 ``UPDATE``。

    模块级函数，理由与 :func:`_insert_locked` 相同（模块文档「为什么拆分」）。
    """

    cursor.execute(
        "UPDATE local_permission_override"
        " SET entry_status = 'revoked', revoked_at = %s,"
        "     revoked_pending_action_id = %s"
        " WHERE id = %s AND entry_status = 'active'",
        (moment, revoked_pending_action_id, override_id),
    )
    return cursor.rowcount == 1
