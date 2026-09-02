"""本地权限覆盖（``local_permission_override``，迁移 ``0072``）的唯一 PostgreSQL 落点。

三个职责：

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
3. :meth:`~.daily_activity_stats` —— 供内测每日通报「本地权限覆盖活动」段
   （S-P-1c，#319）复用的哑聚合读路径：当日新增/收回笔数与当前生效总量，
   不返回任何一行的 ``user_id``/``company_id``/``metric_name``/``reason`` 明细，
   只返回计数——与 :meth:`effective_entries` 面向单用户的明细读取是两种不同
   的读路径，不复用同一条 SQL。

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

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.ids import new_id
from lingxi.core.permission.legacy_diff import (
    ALL_COMPANIES_KEY,
    ALL_SCOPE_POSITION_NAME,
    ALL_SCOPE_REFRESH_REASON,
    IMPORT_REASON,
    LEGACY_IMPORT_ACTOR,
    PENDING_ACTION_REASON,
    LegacyImportPlan,
    LegacyImportReport,
)
from lingxi.core.permission.local_override import LocalPermissionOverrideEntry, OverrideDirection

#: 与 ``core/admin/pending_action.PendingActionType.LOCAL_PERMISSION_GRANT`` 取值逐字
#: 相同（本模块不 import ``core/admin``，字面量独立登记，同 ``scripts/ops`` 的既有姿态）。
_ACTION_TYPE_GRANT = "local_permission_grant"


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
    " pending_action_id, created_at, position_name, company_scope, permission_group_id"
)

#: 内测每日通报「本地权限覆盖活动」段的哑聚合（Issue #319 S-P-1c，唯一调用方
#: 见 :meth:`PostgresLocalPermissionOverrideStore.daily_activity_stats`）。与
#: `adapters/postgres_daily_report.py` 其余哑聚合 SQL 同一条纪律：只做
#: `COUNT(*) FILTER`/`COUNT(DISTINCT ...)`，不做任何分类判断或格式化——「当日
#: 零活动且当前生效总数为零时不出现该段」这条判定留给
#: `core/daily_report.py::build_local_override_activity`。
#:
#: `granted_today`/`suppressed_today` 按 `created_at` 落在窗口内的新增行数分
#: 方向计数（`created_at` 是这张表天然的「新增时刻」，与 `task.created_at` 同一
#: 语义）；`revoked_today` 按 `revoked_at` 落在窗口内计数，**不分原方向**——收回
#: 是同一行状态翻转（迁移 `0072` 文件头部「为什么用『同一行状态翻转』」），
#: 一笔收回可能翻转自 grant 也可能翻转自 suppress，管理群需要的是「今天发生了
#: 几次收回操作」这一个数字。`active_grant_total`/`active_suppress_total` **不
#: 限时间窗口**：当前生效（`entry_status = 'active'`）条目总数，历史上分多天
#: 新增、至今未被收回的条目都计入——这是「现在有多少条覆盖在生效」而不是
#: 「今天新增了多少条」。`affected_user_count` 是当前生效条目（两个方向取并集）
#: 覆盖的去重用户数。
_DAILY_ACTIVITY_STATS_SQL = """
SELECT
    COUNT(*) FILTER (
        WHERE direction = 'grant'
          AND created_at >= %(window_start)s AND created_at < %(window_end)s
    ) AS granted_today,
    COUNT(*) FILTER (
        WHERE direction = 'suppress'
          AND created_at >= %(window_start)s AND created_at < %(window_end)s
    ) AS suppressed_today,
    COUNT(*) FILTER (
        WHERE revoked_at >= %(window_start)s AND revoked_at < %(window_end)s
    ) AS revoked_today,
    COUNT(*) FILTER (WHERE direction = 'grant' AND entry_status = 'active') AS active_grant_total,
    COUNT(*) FILTER (WHERE direction = 'suppress' AND entry_status = 'active') AS active_suppress_total,
    COUNT(DISTINCT user_id) FILTER (WHERE entry_status = 'active') AS affected_user_count
  FROM local_permission_override
"""


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
        position_name,
        company_scope,
        permission_group_id,
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
        position_name=position_name,
        company_scope=company_scope,
        permission_group_id=permission_group_id,
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

    def daily_activity_stats(
        self, *, window_start: datetime, window_end: datetime
    ) -> tuple[int, int, int, int, int, int]:
        """内测每日通报「本地权限覆盖活动」段的哑聚合（Issue #319 S-P-1c，唯一
        调用方是 ``apps/scheduler/daily_report.py`` 装配的可选取数回调）。

        返回 ``(granted_today, suppressed_today, revoked_today,
        active_grant_total, active_suppress_total, affected_user_count)``——
        六个字段的精确定义见 :data:`_DAILY_ACTIVITY_STATS_SQL` 文档；本方法
        只读，不做任何写入，也不做「是否该整段判不可判定」这类分类判断，那层
        判定留给 ``core/daily_report.py::build_local_override_activity``。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                _DAILY_ACTIVITY_STATS_SQL,
                {"window_start": window_start, "window_end": window_end},
            )
            row = cursor.fetchone()
        assert row is not None  # 聚合查询恒返回恰一行，即使表为空（COUNT 的 0）
        (
            granted_today,
            suppressed_today,
            revoked_today,
            active_grant_total,
            active_suppress_total,
            affected_user_count,
        ) = row
        return (
            int(granted_today),
            int(suppressed_today),
            int(revoked_today),
            int(active_grant_total),
            int(active_suppress_total),
            int(affected_user_count),
        )

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
        position_name: str | None = None,
        company_scope: str | None = None,
        permission_group_id: str | None = None,
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
            position_name=position_name,
            company_scope=company_scope,
            permission_group_id=permission_group_id,
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

    def revoke_group(
        self,
        *,
        permission_group_id: str,
        revoked_pending_action_id: str,
        expected_override_ids: tuple[str, ...],
        now: datetime | None = None,
    ) -> bool:
        """事务性收回一笔职位+范围授权的全部展开行。

        ``expected_override_ids`` 是管理卡展示时看到的完整行集合。函数在同一
        事务中先对该组当前生效行加行锁，再核对集合完全一致，最后一次性翻转所有
        行；因此并发新授权不会被误伤，且组已发生漂移时整个操作原子失败。历史
        ``permission_group_id IS NULL`` 行仍由 :meth:`revoke` 按行收回。
        """

        if not permission_group_id or not expected_override_ids:
            return False
        moment = now or datetime.now(timezone.utc)
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            return _revoke_group_locked(
                cursor,
                permission_group_id=permission_group_id,
                revoked_pending_action_id=revoked_pending_action_id,
                moment=moment,
                expected_override_ids=expected_override_ids,
            )


    def import_legacy_plan(
        self,
        *,
        user_id: str,
        target_open_id: str,
        plan: LegacyImportPlan,
        now: datetime,
        initiated_by_open_id: str = LEGACY_IMPORT_ACTOR,
    ) -> LegacyImportReport:
        """存量差集导入的唯一落库口（rc25 S-1，Issue #540；``scripts/ops/
        import_local_permission_override.py`` 的 ``apply_grant`` 委托同一方法）。

        **每用户一事务**：先一次查出该用户全部生效 grant 行算出真正缺的键；一条都不缺
        直接返回（零写入）；否则在同一事务里合成一条**已终态**的 ``pending_action``
        （``action_type='local_permission_grant'``、``status='executed'``、
        ``card_delivered=FALSE`` 如实反映从未发过卡片、``reason='legacy_import_2_0'``、
        ``target_state_snapshot='absent'``）并逐行插入；具体公司行无组，「全部」组
        （``company_id="*"``）共享同一 ``lpg_`` 组 ID——已有生效的「全部」组则沿用其
        组 ID 补缺行，不建第二组。逐行用 SAVEPOINT 包住：并发撞上迁移 ``0072`` 的部分
        唯一索引时降级为 ``already_present``，不让整批回滚；最终一行都没新增时删掉
        刚合成的 ``pending_action``，不留孤儿终态记录。任何其他异常原样上抛，事务整体
        回滚——调用方按本侧故障 fail-closed。
        """

        pairs = tuple(plan.pairs)
        group_metrics = tuple(plan.all_scope_metrics)
        if not pairs and not group_metrics:
            return LegacyImportReport(imported=0, already_present=0)
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT company_id, metric_name, permission_group_id, position_name"
                " FROM local_permission_override"
                " WHERE user_id = %s AND direction = %s AND entry_status = 'active'",
                (user_id, OverrideDirection.GRANT.value),
            )
            existing: dict[tuple[str, str], tuple[str | None, str | None]] = {
                (company, metric): (group, position)
                for company, metric, group, position in cursor.fetchall()
            }
            existing_group: str | None = None
            for (company, _metric), (group, position) in existing.items():
                if company == ALL_COMPANIES_KEY and position == ALL_SCOPE_POSITION_NAME and group:
                    existing_group = group
                    break
            missing_pairs = [pair for pair in pairs if pair not in existing]
            missing_group = [metric for metric in group_metrics if (ALL_COMPANIES_KEY, metric) not in existing]
            already_present = (len(pairs) - len(missing_pairs)) + (len(group_metrics) - len(missing_group))
            if not missing_pairs and not missing_group:
                return LegacyImportReport(
                    imported=0, already_present=already_present, group_id=existing_group
                )

            group_id = existing_group if existing_group else (new_id("lpg") if missing_group else None)
            pending_id = _insert_synthetic_pending_action(
                cursor,
                target_open_id=target_open_id,
                initiated_by_open_id=initiated_by_open_id,
                reason=PENDING_ACTION_REASON,
                moment=now,
                payload={
                    "legacy_import_2_0": {
                        "shape": plan.shape,
                        "specific_pairs": [list(pair) for pair in missing_pairs],
                        "all_scope_metrics": list(missing_group),
                        "permission_group_id": group_id,
                    },
                    "reason": IMPORT_REASON,
                },
            )
            imported = 0
            for company, metric in missing_pairs:
                entry = LocalPermissionOverrideEntry(
                    user_id=user_id,
                    direction=OverrideDirection.GRANT,
                    company_id=company,
                    metric_name=metric,
                    reason=IMPORT_REASON,
                    initiated_by_open_id=initiated_by_open_id,
                    pending_action_id=pending_id,
                    created_at=now,
                )
                if _insert_with_savepoint(cursor, entry):
                    imported += 1
                else:
                    already_present += 1
            for metric in missing_group:
                entry = LocalPermissionOverrideEntry(
                    user_id=user_id,
                    direction=OverrideDirection.GRANT,
                    company_id=ALL_COMPANIES_KEY,
                    metric_name=metric,
                    reason=IMPORT_REASON,
                    initiated_by_open_id=initiated_by_open_id,
                    pending_action_id=pending_id,
                    created_at=now,
                    position_name=ALL_SCOPE_POSITION_NAME,
                    company_scope=ALL_COMPANIES_KEY,
                    permission_group_id=group_id,
                )
                if _insert_with_savepoint(cursor, entry):
                    imported += 1
                else:
                    already_present += 1
            if imported == 0:
                cursor.execute("DELETE FROM pending_action WHERE id = %s", (pending_id,))
                return LegacyImportReport(
                    imported=0, already_present=already_present, group_id=existing_group
                )
            return LegacyImportReport(
                imported=imported,
                already_present=already_present,
                group_id=group_id if (missing_group or existing_group) else None,
                group_created=bool(missing_group) and existing_group is None,
            )

    def expand_all_scope_group(
        self, *, user_id: str, group_id: str, metrics: Sequence[str], now: datetime
    ) -> int:
        """新指标进入映射后给「全部」组补缺行（rc25 S-1 方案 E；调用方是每日重算与
        定向重算，缺项由 ``core/permission/legacy_diff.missing_all_scope_metrics`` 算出）。

        只加不减；同组同指标**任何状态**（含 ``revoked``）已有行都不再插入——管理员
        单独撤销过的指标不复活。合成一条已终态 ``pending_action``
        （``reason='legacy_all_scope_refresh'``），目标 open_id 从 ``app_user`` 现读；
        用户不存在或一行都没新增时零写入。返回实际新增行数。
        """

        wanted = tuple(dict.fromkeys(metric for metric in metrics if metric))
        if not group_id or not wanted:
            return 0
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT feishu_open_id FROM app_user WHERE id = %s", (user_id,))
            row = cursor.fetchone()
            if row is None or not row[0]:
                return 0
            target_open_id = str(row[0])
            cursor.execute(
                "SELECT metric_name FROM local_permission_override"
                " WHERE user_id = %s AND permission_group_id = %s AND company_id = %s",
                (user_id, group_id, ALL_COMPANIES_KEY),
            )
            seen = {str(item[0]) for item in cursor.fetchall()}
            missing = [metric for metric in wanted if metric not in seen]
            if not missing:
                return 0
            pending_id = _insert_synthetic_pending_action(
                cursor,
                target_open_id=target_open_id,
                initiated_by_open_id=LEGACY_IMPORT_ACTOR,
                reason=ALL_SCOPE_REFRESH_REASON,
                moment=now,
                payload={
                    "legacy_all_scope_refresh": {
                        "permission_group_id": group_id,
                        "all_scope_metrics": list(missing),
                    },
                    "reason": IMPORT_REASON,
                },
            )
            added = 0
            for metric in missing:
                entry = LocalPermissionOverrideEntry(
                    user_id=user_id,
                    direction=OverrideDirection.GRANT,
                    company_id=ALL_COMPANIES_KEY,
                    metric_name=metric,
                    reason=IMPORT_REASON,
                    initiated_by_open_id=LEGACY_IMPORT_ACTOR,
                    pending_action_id=pending_id,
                    created_at=now,
                    position_name=ALL_SCOPE_POSITION_NAME,
                    company_scope=ALL_COMPANIES_KEY,
                    permission_group_id=group_id,
                )
                if _insert_with_savepoint(cursor, entry):
                    added += 1
            if added == 0:
                cursor.execute("DELETE FROM pending_action WHERE id = %s", (pending_id,))
            return added


def _insert_synthetic_pending_action(
    cursor,
    *,
    target_open_id: str,
    initiated_by_open_id: str,
    reason: str,
    moment: datetime,
    payload: dict,
) -> str:
    """在调用方事务内插入一条**已终态**的合成 ``pending_action``（迁移 ``0072`` 的
    ``pending_action_id NOT NULL`` 外键要求每一行本地覆盖都指向一次确认动作；批量导入
    没有真实卡片，``card_delivered=FALSE`` 如实反映这一点）。返回其 ID。"""

    pending_id = new_id("pac")
    cursor.execute(
        """
        INSERT INTO pending_action
            (id, action_type, target_open_id, target_state_snapshot,
             initiated_by_open_id, status, card_delivered, reason,
             created_at, confirm_deadline_at, decided_at, decided_by_open_id, payload)
        VALUES (%s, %s, %s, %s, %s, 'executed', FALSE, %s, %s, %s, %s, %s, %s)
        """,
        (
            pending_id,
            _ACTION_TYPE_GRANT,
            target_open_id,
            "absent",
            initiated_by_open_id,
            reason,
            moment,
            moment + timedelta(seconds=1),
            moment,
            initiated_by_open_id,
            json.dumps(payload, ensure_ascii=False),
        ),
    )
    return pending_id


def _insert_with_savepoint(cursor, entry: LocalPermissionOverrideEntry) -> bool:
    """SAVEPOINT 包住一次 :func:`_insert_locked`：撞唯一索引回滚到保存点、返回
    ``False``（已存在），事务其余部分不受影响。"""

    cursor.execute("SAVEPOINT legacy_override_row")
    try:
        _insert_locked(cursor, override_id=new_id("lpo"), entry=entry)
    except DuplicateActiveOverride:
        cursor.execute("ROLLBACK TO SAVEPOINT legacy_override_row")
        return False
    cursor.execute("RELEASE SAVEPOINT legacy_override_row")
    return True


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
                 initiated_by_open_id, pending_action_id, created_at,
                 position_name, company_scope, permission_group_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                entry.position_name,
                entry.company_scope,
                entry.permission_group_id,
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


def _revoke_group_locked(
    cursor,
    *,
    permission_group_id: str,
    revoked_pending_action_id: str,
    moment: datetime,
    expected_override_ids: tuple[str, ...],
) -> bool:
    """在调用方事务内锁定并收回一整个新职位授权组。

    锁定后比较期望集合是并发安全的关键：如果另一笔确认已经为同组追加了行，
    或组内某行已被先收回，返回 ``False``，调用方回滚整笔待确认操作，不会只收
    回半组或把并发新授权带走。调用方应在同一事务中持有目标用户锁，以序列化
    常规 grant/revoke；这里的集合比较则是数据库级纵深防线。
    """

    if not permission_group_id or not expected_override_ids:
        return False
    expected = tuple(dict.fromkeys(expected_override_ids))
    if len(expected) != len(expected_override_ids):
        return False
    cursor.execute(
        "SELECT id FROM local_permission_override"
        " WHERE permission_group_id = %s AND entry_status = 'active'"
        " ORDER BY id FOR UPDATE",
        (permission_group_id,),
    )
    current_ids = tuple(row[0] for row in cursor.fetchall())
    if set(current_ids) != set(expected) or len(current_ids) != len(expected):
        return False
    placeholders = ", ".join("%s" for _ in expected)
    cursor.execute(
        "UPDATE local_permission_override"
        " SET entry_status = 'revoked', revoked_at = %s,"
        "     revoked_pending_action_id = %s"
        f" WHERE permission_group_id = %s AND entry_status = 'active'"
        f"   AND id IN ({placeholders})",
        (moment, revoked_pending_action_id, permission_group_id, *expected),
    )
    return cursor.rowcount == len(expected)
