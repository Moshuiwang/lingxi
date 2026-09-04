"""存量差集导入与职位范围预授权的落库细节。

``import_legacy_plan``/``import_position_grant`` 从 ``postgres_local_permission.py``
按"两套输入不同、reason 不同，但共用同一套写入原语"的边界拆分而来。

只依赖 ``postgres_local_permission.py`` 里两个共用写入原语
（``_insert_synthetic_pending_action``、``_insert_with_savepoint``）；这两个
原语同时也被该模块自己的 ``insert()``/``revoke()`` 使用，因此仍然住在那边。
本模块反过来被 ``postgres_local_permission.py`` 顶层导入（供
:meth:`~lingxi.adapters.postgres_local_permission.PostgresLocalPermissionOverrideStore.import_legacy_plan`
等方法调用），为避免循环导入，本模块对那两个原语一律用**函数体内延迟导入**
（调用时才 import，而不是模块顶层），不在本模块顶层 import 该文件。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from lingxi.core.ids import new_id
from lingxi.core.permission.legacy_diff import (
    ALL_COMPANIES_KEY,
    ALL_SCOPE_EXPLICIT_POSITION_NAME,
    ALL_SCOPE_POSITION_NAME,
    IMPORT_REASON,
    LEGACY_IMPORT_ACTOR,
    PENDING_ACTION_REASON,
    LegacyImportPlan,
    LegacyImportReport,
)
from lingxi.core.permission.local_override import LocalPermissionOverrideEntry, OverrideDirection
from lingxi.core.permission.position_override import PositionGrantPlan


@dataclass(frozen=True)
class _ExistingGrantState:
    """:func:`_load_existing_grant_state` 的返回形状，见其文档。"""

    existing: dict[tuple[str, str], tuple[str | None, str | None]]
    revoked: set[tuple[str, str]]
    existing_group: str | None
    revoked_group: bool


def _load_existing_grant_state(cursor, *, user_id: str) -> _ExistingGrantState:
    """读出该用户全部 ``direction='grant'`` 行，按当前状态分流为生效/已撤销。

    ``existing_group``/``revoked_group`` 只识别"全部"组行（``company_id`` 为
    :data:`ALL_COMPANIES_KEY`、``position_name`` 属于两个全通配标签之一、且带
    group）：用于判断是否已有一个生效的「全部」组可以沿用其组 ID 与标签补缺
    行，或曾被整组撤销过、本次不该重建。
    """
    cursor.execute(
        "SELECT company_id, metric_name, permission_group_id, position_name, entry_status"
        " FROM local_permission_override"
        " WHERE user_id = %s AND direction = %s",
        (user_id, OverrideDirection.GRANT.value),
    )
    existing: dict[tuple[str, str], tuple[str | None, str | None]] = {}
    revoked: set[tuple[str, str]] = set()
    existing_group: str | None = None
    revoked_group = False
    for company, metric, group, position, status in cursor.fetchall():
        is_all_scope_row = (
            company == ALL_COMPANIES_KEY
            and position in (ALL_SCOPE_POSITION_NAME, ALL_SCOPE_EXPLICIT_POSITION_NAME)
            and group
        )
        if status == "active":
            existing[(company, metric)] = (group, position)
            if is_all_scope_row and existing_group is None:
                existing_group = group
        else:
            revoked.add((company, metric))
            if is_all_scope_row:
                revoked_group = True
    return _ExistingGrantState(
        existing=existing,
        revoked=revoked,
        existing_group=existing_group,
        revoked_group=revoked_group,
    )


@dataclass(frozen=True)
class _LegacyPlanDiff:
    """:func:`_diff_legacy_plan` 的返回形状，见其文档。"""

    missing_pairs: list[tuple[str, str]]
    missing_group: list[str]
    already_present: int
    revoked_skipped: int
    group_skipped_revoked: bool


def _diff_legacy_plan(plan: LegacyImportPlan, state: _ExistingGrantState) -> _LegacyPlanDiff:
    """算出这次导入真正缺的键。

    撤销过的键不复活：单独撤销过的组内指标或具体公司行不重建
    （``revoked_skipped`` 计数）；管理员整组撤销过、当前没有生效「全部」组
    时同样不重建（``group_skipped_revoked``），即使映射后来新增了指标。
    """
    pairs = tuple(plan.pairs)
    group_metrics = tuple(plan.all_scope_metrics)
    missing_pairs = [
        pair for pair in pairs if pair not in state.existing and pair not in state.revoked
    ]
    missing_group = [
        metric
        for metric in group_metrics
        if (ALL_COMPANIES_KEY, metric) not in state.existing
        and (ALL_COMPANIES_KEY, metric) not in state.revoked
    ]
    revoked_skipped = sum(
        1 for pair in pairs if pair not in state.existing and pair in state.revoked
    ) + sum(
        1
        for metric in group_metrics
        if (ALL_COMPANIES_KEY, metric) not in state.existing
        and (ALL_COMPANIES_KEY, metric) in state.revoked
    )
    group_skipped_revoked = False
    if group_metrics and state.existing_group is None and state.revoked_group:
        # 管理员整组撤销过、现在没有生效的「全部」组：撤销过的组不复活——重新
        # 开通不会把它按新组 ID 全量重建（连映射后来新增的指标也不建）；具体
        # 公司行照常处理。
        revoked_skipped += len(missing_group)
        missing_group = []
        group_skipped_revoked = True
    already_present = sum(1 for pair in pairs if pair in state.existing) + sum(
        1 for metric in group_metrics if (ALL_COMPANIES_KEY, metric) in state.existing
    )
    return _LegacyPlanDiff(
        missing_pairs=missing_pairs,
        missing_group=missing_group,
        already_present=already_present,
        revoked_skipped=revoked_skipped,
        group_skipped_revoked=group_skipped_revoked,
    )


@dataclass(frozen=True)
class _LegacyImportOutcome:
    """:func:`_insert_legacy_import_rows` 的返回形状，见其文档。"""

    imported: int
    already_present: int
    group_id: str | None
    group_created: bool


def _insert_legacy_import_rows(
    cursor,
    *,
    user_id: str,
    target_open_id: str,
    initiated_by_open_id: str,
    now: datetime,
    plan: LegacyImportPlan,
    diff: _LegacyPlanDiff,
    state: _ExistingGrantState,
) -> _LegacyImportOutcome:
    """合成终态 ``pending_action`` 并逐行插入缺失的具体公司行与「全部」组行。

    具体公司行无组；「全部」组共享同一 ``lpg_`` 组 ID，标签沿用既有生效组的
    标签（见 :func:`_resolve_all_scope_position_name`）。逐行用 SAVEPOINT
    包住：并发撞上唯一索引时降级为已存在，不让整批回滚。
    """
    group_id = (
        state.existing_group
        if state.existing_group
        else (new_id("lpg") if diff.missing_group else None)
    )
    pending_id = _insert_legacy_import_pending_action(
        cursor,
        target_open_id=target_open_id,
        initiated_by_open_id=initiated_by_open_id,
        now=now,
        plan=plan,
        diff=diff,
        group_id=group_id,
    )
    imported_pairs = _insert_missing_pair_rows(
        cursor,
        user_id=user_id,
        initiated_by_open_id=initiated_by_open_id,
        now=now,
        missing_pairs=diff.missing_pairs,
        pending_id=pending_id,
    )
    position_name = _resolve_all_scope_position_name(plan, state)
    imported_group = _insert_missing_group_rows(
        cursor,
        user_id=user_id,
        initiated_by_open_id=initiated_by_open_id,
        now=now,
        missing_group=diff.missing_group,
        position_name=position_name,
        group_id=group_id,
        pending_id=pending_id,
    )
    return _build_legacy_import_outcome(
        cursor,
        pending_id=pending_id,
        group_id=group_id,
        diff=diff,
        state=state,
        imported_pairs=imported_pairs,
        imported_group=imported_group,
    )


def _insert_legacy_import_pending_action(
    cursor,
    *,
    target_open_id: str,
    initiated_by_open_id: str,
    now: datetime,
    plan: LegacyImportPlan,
    diff: _LegacyPlanDiff,
    group_id: str | None,
) -> str:
    """合成本笔差集导入的终态 ``pending_action``，返回其 ID。"""
    from .postgres_local_permission import _insert_synthetic_pending_action

    return _insert_synthetic_pending_action(
        cursor,
        target_open_id=target_open_id,
        initiated_by_open_id=initiated_by_open_id,
        reason=PENDING_ACTION_REASON,
        moment=now,
        payload={
            "legacy_import_2_0": {
                "shape": plan.shape,
                "specific_pairs": [list(pair) for pair in diff.missing_pairs],
                "all_scope_metrics": list(diff.missing_group),
                "permission_group_id": group_id,
            },
            "reason": IMPORT_REASON,
        },
    )


def _build_legacy_import_outcome(
    cursor,
    *,
    pending_id: str,
    group_id: str | None,
    diff: _LegacyPlanDiff,
    state: _ExistingGrantState,
    imported_pairs: int,
    imported_group: int,
) -> _LegacyImportOutcome:
    """汇总两段插入的结果；一行都没新增时删掉合成的 ``pending_action``。"""
    imported = imported_pairs + imported_group
    already_present = (
        diff.already_present
        + (len(diff.missing_pairs) - imported_pairs)
        + (len(diff.missing_group) - imported_group)
    )
    if imported == 0:
        cursor.execute("DELETE FROM pending_action WHERE id = %s", (pending_id,))
        return _LegacyImportOutcome(
            imported=0,
            already_present=already_present,
            group_id=state.existing_group,
            group_created=False,
        )
    return _LegacyImportOutcome(
        imported=imported,
        already_present=already_present,
        group_id=group_id if (diff.missing_group or state.existing_group) else None,
        group_created=bool(diff.missing_group) and state.existing_group is None,
    )


def _resolve_all_scope_position_name(
    plan: LegacyImportPlan, state: _ExistingGrantState
) -> str | None:
    """已有生效「全部」组时沿用它的标签，不把「全部指标」组改写成「指定指标」组或反之。"""
    if state.existing_group is not None:
        for (_company, _metric), (group, position) in state.existing.items():
            if group == state.existing_group and position:
                return position
    return plan.all_scope_position_name


def _insert_missing_pair_rows(
    cursor,
    *,
    user_id: str,
    initiated_by_open_id: str,
    now: datetime,
    missing_pairs: list[tuple[str, str]],
    pending_id: str,
) -> int:
    """逐行插入缺失的具体公司行，SAVEPOINT 包住并发撞唯一索引的情形。"""
    from .postgres_local_permission import _insert_with_savepoint

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
    return imported


def _insert_missing_group_rows(
    cursor,
    *,
    user_id: str,
    initiated_by_open_id: str,
    now: datetime,
    missing_group: list[str],
    position_name: str | None,
    group_id: str | None,
    pending_id: str,
) -> int:
    """逐个指标插入缺失的「全部」组行，SAVEPOINT 包住并发撞唯一索引的情形。"""
    from .postgres_local_permission import _insert_with_savepoint

    imported = 0
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
            position_name=position_name,
            company_scope=ALL_COMPANIES_KEY,
            permission_group_id=group_id,
        )
        if _insert_with_savepoint(cursor, entry):
            imported += 1
    return imported


def _insert_all_scope_group_rows(
    cursor, *, user_id: str, group_id: str, missing: list[str], pending_id: str, now: datetime
) -> int:
    """逐个指标插入「全部」组补缺行，SAVEPOINT 包住并发撞唯一索引的情形。"""
    from .postgres_local_permission import _insert_with_savepoint

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
    return added


def _apply_position_grant_locked(
    cursor,
    *,
    user_id: str,
    target_open_id: str,
    plan: PositionGrantPlan,
    now: datetime,
    initiated_by_open_id: str,
) -> LegacyImportReport:
    """在调用方已经持有的 ``cursor``（及其所在事务）上执行一笔职位范围预授权。

    模块级函数而不是实例方法，理由同 ``postgres_local_permission._insert_locked``：
    它只需要一个已经打开的 ``cursor``。拆出来还有第二个作用——"合成的
    ``pending_action`` 到底带的是哪个 ``reason``"这件事因此可以在**不连数据库**
    的单测里用假 cursor 逐参数断言，而不是只能靠真库门禁或 stage 演练发现它
    被改掉。
    """
    cursor.execute("SELECT id FROM app_user WHERE id = %s FOR UPDATE", (user_id,))
    active, revoked = _load_existing_grant_pairs(cursor, user_id=user_id)
    missing, already_present, revoked_skipped = _diff_position_grant(plan, active, revoked)
    if not missing:
        return LegacyImportReport(
            imported=0, already_present=already_present, revoked_skipped=revoked_skipped
        )

    group_id = new_id("lpg")
    pending_id = _insert_position_grant_pending_action(
        cursor,
        target_open_id=target_open_id,
        initiated_by_open_id=initiated_by_open_id,
        now=now,
        plan=plan,
        missing=missing,
        group_id=group_id,
    )
    imported = _insert_position_grant_rows(
        cursor,
        user_id=user_id,
        initiated_by_open_id=initiated_by_open_id,
        now=now,
        plan=plan,
        missing=missing,
        pending_id=pending_id,
        group_id=group_id,
    )
    return _build_position_grant_report(
        cursor,
        pending_id=pending_id,
        group_id=group_id,
        imported=imported,
        already_present=already_present,
        revoked_skipped=revoked_skipped,
    )


def _insert_position_grant_pending_action(
    cursor,
    *,
    target_open_id: str,
    initiated_by_open_id: str,
    now: datetime,
    plan: PositionGrantPlan,
    missing: list[tuple[str, str]],
    group_id: str,
) -> str:
    """合成本笔职位范围预授权的终态 ``pending_action``，返回其 ID。"""
    from .postgres_local_permission import _insert_synthetic_pending_action

    return _insert_synthetic_pending_action(
        cursor,
        target_open_id=target_open_id,
        initiated_by_open_id=initiated_by_open_id,
        reason=plan.pending_action_reason,
        moment=now,
        payload={
            "preprovision": {
                "position_name": plan.position_name,
                "company_scope": plan.company_scope,
                "pairs": [list(pair) for pair in missing],
                "permission_group_id": group_id,
            },
            "reason": plan.override_reason,
        },
    )


def _build_position_grant_report(
    cursor,
    *,
    pending_id: str,
    group_id: str,
    imported: int,
    already_present: int,
    revoked_skipped: int,
) -> LegacyImportReport:
    """一行都没新增时删掉合成的 ``pending_action``，否则报告新建的组。"""
    if imported == 0:
        cursor.execute("DELETE FROM pending_action WHERE id = %s", (pending_id,))
        return LegacyImportReport(
            imported=0, already_present=already_present, revoked_skipped=revoked_skipped
        )
    return LegacyImportReport(
        imported=imported,
        already_present=already_present,
        group_id=group_id,
        group_created=True,
        revoked_skipped=revoked_skipped,
    )


def _diff_position_grant(
    plan: PositionGrantPlan, active: set[tuple[str, str]], revoked: set[tuple[str, str]]
) -> tuple[list[tuple[str, str]], int, int]:
    """算出这次职位范围预授权真正缺的键。

    返回 ``(missing, already_present, revoked_skipped)``；曾被管理员撤销的键
    不复活，计入 ``revoked_skipped`` 而不是 ``missing``。
    """
    wanted = tuple(dict.fromkeys(plan.pairs))
    missing = [pair for pair in wanted if pair not in active and pair not in revoked]
    already_present = sum(1 for pair in wanted if pair in active)
    revoked_skipped = sum(1 for pair in wanted if pair not in active and pair in revoked)
    return missing, already_present, revoked_skipped


def _load_existing_grant_pairs(
    cursor, *, user_id: str
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """读出该用户全部 grant 行的键，按当前状态分流。

    返回 ``(company, metric)`` 键组成的 ``(active, revoked)`` 两个集合。
    """
    cursor.execute(
        "SELECT company_id, metric_name, entry_status FROM local_permission_override"
        " WHERE user_id = %s AND direction = %s",
        (user_id, OverrideDirection.GRANT.value),
    )
    active: set[tuple[str, str]] = set()
    revoked: set[tuple[str, str]] = set()
    for company, metric, status in cursor.fetchall():
        (active if status == "active" else revoked).add((company, metric))
    return active, revoked


def _insert_position_grant_rows(
    cursor,
    *,
    user_id: str,
    initiated_by_open_id: str,
    now: datetime,
    plan: PositionGrantPlan,
    missing: list[tuple[str, str]],
    pending_id: str,
    group_id: str,
) -> int:
    """逐行插入职位+范围预授权缺行，SAVEPOINT 包住并发撞唯一索引的情形。"""
    from .postgres_local_permission import _insert_with_savepoint

    imported = 0
    for company, metric in missing:
        entry = LocalPermissionOverrideEntry(
            user_id=user_id,
            direction=OverrideDirection.GRANT,
            company_id=company,
            metric_name=metric,
            reason=plan.override_reason,
            initiated_by_open_id=initiated_by_open_id,
            pending_action_id=pending_id,
            created_at=now,
            position_name=plan.position_name,
            company_scope=plan.company_scope,
            permission_group_id=group_id,
        )
        if _insert_with_savepoint(cursor, entry):
            imported += 1
    return imported
