"""本地权限覆盖（``local_permission_override``，迁移 ``0072``）的唯一 PostgreSQL 落点。

三个职责：:meth:`~.effective_entries`（按用户读全部当前生效条目，供每日重算/
开通链聚合复用）、:meth:`~.insert`/:meth:`~.revoke`（写路径，供确认卡执行器
调用；"没有确认卡不能写入"由迁移 ``0072`` 的 ``pending_action_id NOT NULL``
外键落实，本模块只是把这条约束包装成 Python 接口）、
:meth:`~.daily_activity_stats`（内测每日通报的哑聚合读路径，只返回计数）。

``insert``/``revoke`` 拆成 ``_insert_locked``/``_revoke_locked``（模块级函数）
加公开包装：``adapters/postgres_pending_action.py`` 的 ``_confirm_locked``
需要在自己已经打开的那个事务、那个 cursor 上执行这条写入（写行必须与审计
同一事务，否则审计失败回滚时这行不会跟着回滚）。这两个函数因此只接受
调用方持有的 ``cursor``，供本类公开方法与该外部调用方共用。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.ids import new_id
from lingxi.core.permission.legacy_diff import (
    ALL_COMPANIES_KEY,
    ALL_SCOPE_REFRESH_REASON,
    IMPORT_REASON,
    LEGACY_IMPORT_ACTOR,
    LegacyImportPlan,
    LegacyImportReport,
)
from lingxi.core.permission.local_override import LocalPermissionOverrideEntry, OverrideDirection
from lingxi.core.permission.position_override import PositionGrantPlan

from .postgres_local_permission_import import (
    _apply_position_grant_locked,
    _diff_legacy_plan,
    _insert_all_scope_group_rows,
    _insert_legacy_import_rows,
    _load_existing_grant_state,
)

#: 与 ``core/admin/pending_action.PendingActionType.LOCAL_PERMISSION_GRANT`` 取值逐字
#: 相同（本模块不 import ``core/admin``，字面量独立登记，同 ``scripts/ops`` 的既有姿态）。
_ACTION_TYPE_GRANT = "local_permission_grant"


class LocalOverrideEntryReader:
    """把按用户读取口适配成两个调用点各自协议要求的形状。

    ``effective_entries(*, user_id) -> Sequence[LocalPermissionOverrideEntry]``
    ——纯类型，不带数据库分配的行标识：写路径（收回流程）需要 ``id`` 定位要
    撤销的具体行，但合并聚合只关心条目内容。两个调用点
    （``apps/scheduler/permission_refresh``、``core/identity/onboarding_runner``）
    各自声明的协议因此只认 ``LocalPermissionOverrideEntry``；这个适配器是两处
    装配共用的唯一一份，避免"从 ``StoredLocalPermissionOverride`` 解出
    ``.entry``"这行代码在两处重复、迟早漂移。
    """

    def __init__(self, store: PostgresLocalPermissionOverrideStore) -> None:
        """包装一个已构造好的 store；不新建连接、不做任何 I/O。"""
        self._store = store

    def effective_entries(self, *, user_id: str) -> tuple[LocalPermissionOverrideEntry, ...]:
        """按用户取全部当前生效条目，解出纯 ``LocalPermissionOverrideEntry``（丢弃行标识）。"""
        return tuple(item.entry for item in self._store.effective_entries(user_id=user_id))


def local_override_reader(
    dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS
) -> LocalOverrideEntryReader:
    """装配层的一步到位入口。

    新建一个 store 并包上 :class:`LocalOverrideEntryReader`。两处装配
    （``apps/scheduler/assembly.py`` 的每日权限重算与首次开通编排）各自
    调用一次，避免"先建 store 再包一层适配"这两步各自重复一份、迟早漂移；store
    本身轻量、无状态，两处各自新建互不共享是刻意的（与文件里其余 Postgres 读写口
    同一惯例），本函数只是把这两步合成一步，不引入共享实例。
    """
    return LocalOverrideEntryReader(PostgresLocalPermissionOverrideStore(dsn, timeouts=timeouts))


class DuplicateActiveOverrideError(Exception):
    """同一用户同一极性同一公司同一指标已经有一条生效条目。

    迁移 ``0072`` 的 ``local_permission_override_active_unique_idx``；
    翻译自 ``psycopg.errors.UniqueViolation``：调用方（确认卡执行器）据此
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
    行标识是同一姿态）。收回流程按这个 ``id`` 定位要撤销的具体行。
    """

    id: str
    entry: LocalPermissionOverrideEntry


_SELECT_COLUMNS = (
    "id, user_id, direction, company_id, metric_name, reason, initiated_by_open_id,"
    " pending_action_id, created_at, position_name, company_scope, permission_group_id"
)

#: 内测每日通报「本地权限覆盖活动」段的哑聚合，只做 COUNT 不做分类判断
#: （见 :meth:`PostgresLocalPermissionOverrideStore.daily_activity_stats`）。
#: `granted_today`/`suppressed_today` 按 created_at 窗口内新增分方向计数；
#: `revoked_today` 按 revoked_at 计数、不分原方向（同一行状态翻转）；
#: `active_*_total` 不限窗口，是当前生效总数；`affected_user_count` 是去重用户数。
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

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        """记录连接参数；不在构造时打开任何连接。"""
        self._dsn = dsn
        self._timeouts = timeouts

    def effective_entries(self, *, user_id: str) -> tuple[StoredLocalPermissionOverride, ...]:
        """按用户取全部当前生效条目。

        供聚合调用 :func:`~lingxi.core.permission.local_override.resolve_local_overrides`；
        排序（``created_at, id``）只是为了让返回结果确定，不参与聚合语义——
        该函数对输入次序不敏感（集合运算）。
        """
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
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
        """内测每日通报「本地权限覆盖活动」段的哑聚合。

        返回 ``(granted_today, suppressed_today, revoked_today,
        active_grant_total, active_suppress_total, affected_user_count)``——
        六个字段的精确定义见 :data:`_DAILY_ACTIVITY_STATS_SQL` 文档；本方法
        只读，不做任何写入，也不做「是否该整段判不可判定」这类分类判断，那层
        判定留给 ``core/daily_report.py::build_local_override_activity``。
        """
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
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

        **先构造** ``LocalPermissionOverrideEntry``（触发字段校验）**再**发出
        ``INSERT``——非法字段响亮失败，不会打到数据库。实际写入由
        :func:`_insert_locked` 执行。撞上迁移 ``0072`` 的部分唯一索引时转译为
        :class:`DuplicateActiveOverrideError`；一个不存在的 ``pending_action_id``
        （或已删除用户）会撞上外键约束，本方法不额外捕获——让"没有确认卡/没有
        这个用户就不能写入"以数据库原生异常的形式暴露，不悄悄翻译成一个看似
        正常的业务分支返回值。
        """
        moment = now or datetime.now(UTC)
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
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            _insert_locked(cursor, override_id=override_id, entry=entry)
        return StoredLocalPermissionOverride(id=override_id, entry=entry)

    def revoke(
        self,
        *,
        override_id: str,
        revoked_pending_action_id: str,
        now: datetime | None = None,
    ) -> bool:
        """把一条生效条目标记为 ``revoked``，独立开一条连接/事务。

        同一行状态翻转、历史留痕。条件更新 ``WHERE entry_status = 'active'``：
        目标不存在或已 revoked 时影响 0 行，返回 ``False``——"收回一条已经
        不存在/已被收回的条目"与"收回失败"是同一个结论，由调用方按
        ``rowcount`` 自行判断，不在这里猜测该算异常还是正常。实际 ``UPDATE``
        由 :func:`_revoke_locked` 执行；一个不存在的
        ``revoked_pending_action_id`` 会撞上外键约束，与 :meth:`insert`
        同一姿态，不额外捕获。
        """
        moment = now or datetime.now(UTC)
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            return _revoke_locked(
                cursor,
                override_id=override_id,
                revoked_pending_action_id=revoked_pending_action_id,
                moment=moment,
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
        moment = now or datetime.now(UTC)
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            return _revoke_group_locked(
                cursor,
                permission_group_id=permission_group_id,
                revoked_pending_action_id=revoked_pending_action_id,
                moment=moment,
                expected_override_ids=expected_override_ids,
            )

    def import_plan(
        self, *, user_id: str, target_open_id: str, plan: LegacyImportPlan, now: datetime
    ) -> LegacyImportReport:
        """``core/identity/onboarding_ports.LegacyPermissionImporter`` 端口的实现名。

        开通链按这个名字调用（此前只有 :meth:`import_legacy_plan`，装配把本类
        直接当导入口注入 → 每个存量用户首聊 ``AttributeError`` fail-closed）。
        """
        return self.import_legacy_plan(
            user_id=user_id, target_open_id=target_open_id, plan=plan, now=now
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
        """存量差集导入的唯一落库口。

        **每用户一事务**：先锁 ``app_user`` 行、读出现状
        （:func:`_load_existing_grant_state`）、算出真正缺的键
        （:func:`_diff_legacy_plan`，撤销过的键不复活）；一条都不缺时零写入
        直接返回，否则合成一条已终态的 ``pending_action`` 并逐行插入
        （:func:`_insert_legacy_import_rows`），一行都没新增时删掉它、不留
        孤儿终态记录。任何其他异常原样上抛、事务整体回滚，fail-closed。
        """
        pairs = tuple(plan.pairs)
        group_metrics = tuple(plan.all_scope_metrics)
        if not pairs and not group_metrics:
            return LegacyImportReport(imported=0, already_present=0)
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            # 用户级行锁：同一用户两条并发开通链不会各建半个「全部」组。
            cursor.execute("SELECT id FROM app_user WHERE id = %s FOR UPDATE", (user_id,))
            state = _load_existing_grant_state(cursor, user_id=user_id)
            diff = _diff_legacy_plan(plan, state)
            if not diff.missing_pairs and not diff.missing_group:
                return LegacyImportReport(
                    imported=0,
                    already_present=diff.already_present,
                    group_id=state.existing_group,
                    group_skipped_revoked=diff.group_skipped_revoked,
                    revoked_skipped=diff.revoked_skipped,
                )
            outcome = _insert_legacy_import_rows(
                cursor,
                user_id=user_id,
                target_open_id=target_open_id,
                initiated_by_open_id=initiated_by_open_id,
                now=now,
                plan=plan,
                diff=diff,
                state=state,
            )
            return LegacyImportReport(
                imported=outcome.imported,
                already_present=outcome.already_present,
                group_id=outcome.group_id,
                group_created=outcome.group_created,
                group_skipped_revoked=diff.group_skipped_revoked,
                revoked_skipped=diff.revoked_skipped,
            )

    def expand_all_scope_group(
        self, *, user_id: str, group_id: str, metrics: Sequence[str], now: datetime
    ) -> int:
        """新指标进入映射后给「全部」组补缺行。

        调用方是每日重算与定向重算，缺项由
        ``core/permission/legacy_diff.missing_all_scope_metrics`` 算出。只加
        不减；同组同指标**任何状态**（含 ``revoked``）已有行都不再插入——
        管理员单独撤销过的指标不复活。合成一条已终态 ``pending_action``，
        目标 open_id 从 ``app_user`` 现读；用户不存在或一行都没新增时零
        写入。返回实际新增行数。
        """
        wanted = tuple(dict.fromkeys(metric for metric in metrics if metric))
        if not group_id or not wanted:
            return 0
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT feishu_open_id FROM app_user WHERE id = %s FOR UPDATE", (user_id,)
            )
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
            added = _insert_all_scope_group_rows(
                cursor,
                user_id=user_id,
                group_id=group_id,
                missing=missing,
                pending_id=pending_id,
                now=now,
            )
            if added == 0:
                cursor.execute("DELETE FROM pending_action WHERE id = %s", (pending_id,))
            return added

    def import_position_grant(
        self,
        *,
        user_id: str,
        target_open_id: str,
        grant: PositionGrantPlan,
        now: datetime,
        initiated_by_open_id: str,
    ) -> LegacyImportReport:
        """「职位＋公司范围」预授权的落库口。

        与 :meth:`import_legacy_plan` 是同一条纪律的两个来源，刻意不合并成一个
        带开关的方法：差集导入的输入是旧表内容，本方法的输入是产品负责人核对
        过的名单，两者的 reason 不同、在审计上必须分得开，但共用同一套"没有
        确认卡不能写入"的落点（见 :func:`_apply_position_grant_locked`）。本笔
        全部行共享同一个新 ``lpg_`` 组 ID，因此管理卡把它渲染成**一个**职位+
        范围项、一次事务性整组撤销。
        """
        if not grant.pairs:
            return LegacyImportReport(imported=0, already_present=0)
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            return _apply_position_grant_locked(
                cursor,
                user_id=user_id,
                target_open_id=target_open_id,
                plan=grant,
                now=now,
                initiated_by_open_id=initiated_by_open_id,
            )


def _insert_synthetic_pending_action(
    cursor,
    *,
    target_open_id: str,
    initiated_by_open_id: str,
    reason: str,
    moment: datetime,
    payload: dict,
) -> str:
    """在调用方事务内插入一条**已终态**的合成 ``pending_action``，返回其 ID。

    迁移 ``0072`` 的 ``pending_action_id NOT NULL`` 外键要求每一行本地覆盖都
    指向一次确认动作；批量导入没有真实卡片，``card_delivered=FALSE`` 如实
    反映这一点。
    """
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
    """SAVEPOINT 包住一次 :func:`_insert_locked`。

    撞唯一索引回滚到保存点、返回 ``False``（已存在），事务其余部分不受影响。
    """
    cursor.execute("SAVEPOINT legacy_override_row")
    try:
        _insert_locked(cursor, override_id=new_id("lpo"), entry=entry)
    except DuplicateActiveOverrideError:
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
    :class:`DuplicateActiveOverrideError`，两处调用方按各自需要处理这个异常（本类的
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
        raise DuplicateActiveOverrideError(
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
