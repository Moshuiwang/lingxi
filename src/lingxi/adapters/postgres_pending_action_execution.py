"""``prepare``/``confirm`` 两条链路里"拿到游标之后做什么"的纯计算与写入。

从 ``postgres_pending_action.py`` 按体量棘轮纯移动拆出：不自己开连接（不出现
``with connect(...)``）、只接收调用方已打开的 ``cursor``/``connection`` 的辅助
方法都搬到这里；自己开连接的方法原地不动——测试用
``mock.patch("...postgres_pending_action.connect", ...)`` 换入假连接，只有
``connect`` 这个名字留在 ``postgres_pending_action`` 模块自己的命名空间才生效。

``_ExecutionMixin`` 与 ``PostgresPendingActionStore`` 组合进同一个类：
``self._audit``/``self._metric_map_path`` 是宿主类属性，拆分只搬动方法的物理
位置，查找与调用顺序同拆分前逐位相同。本模块反过来被
``postgres_pending_action.py`` 顶层导入，为避免循环导入，本模块对该文件的
``_SELECT_COLUMNS``/``_row_to_pending_action``/两个常量/终态更新 SQL 一律用
**函数体内延迟导入**。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from lingxi.adapters.admin_registry import admin_registry_entry_from_row
from lingxi.adapters.postgres_conversation import _Transaction
from lingxi.adapters.postgres_local_permission import (
    DuplicateActiveOverrideError,
    _insert_locked,
    _revoke_group_locked,
    _revoke_locked,
)
from lingxi.core.admin.pending_action import (
    PENDING_ACTION_TTL_SECONDS,
    ConfirmDecision,
    ConfirmResultKind,
    PendingAction,
    PendingActionAuditWriteFailedError,
    PendingActionStatus,
    PendingActionType,
    PrepareDecision,
    format_in_flight_conflict_message,
)
from lingxi.core.ids import new_id
from lingxi.core.permission.local_override import (
    LocalPermissionOverrideEntry,
    OverrideDirection,
)
from lingxi.core.permission.position_override import (
    PositionPermissionExpansion,
    expand_position_scope,
)

#: 收回的自我目标防呆拒绝码/文案：取值与 ``core/admin/router.
#: _SELF_TARGET_FORBIDDEN_CODE``/拒绝文案相同字符串——两个模块结构上不互相
#: import（``core/admin/router.py`` 不 import adapters/），因此各自独立定义
#: 同一取值，与全仓库既有的"结构相同、不共享导入"的 Protocol/常量惯例一致。
_REVOKE_SELF_TARGET_FORBIDDEN_CODE = "self_target_forbidden"
_REVOKE_SELF_TARGET_FORBIDDEN_MESSAGE = "不能对自己发起该操作。"

#: 本地权限授权/抑制两类动作类型 → 迁移 ``0072`` ``direction`` 列取值的映射。
#: ``REVOKE`` 不在这张表里，且这不是"未登记"的历史遗留——收回的 ``direction``
#: 不是一个固定值，而是要按 override_id 从被收回的那一行本身读出来。
#: ``prepare()`` 的基线观测查询、``confirm()`` 的 EXECUTE 写入分支共用同一张表。
_DIRECTION_BY_ACTION_TYPE: dict[PendingActionType, OverrideDirection] = {
    PendingActionType.LOCAL_PERMISSION_GRANT: OverrideDirection.GRANT,
    PendingActionType.LOCAL_PERMISSION_SUPPRESS: OverrideDirection.SUPPRESS,
}

_GROUP_REVOKE_ROWS_SQL = """SELECT lpo.id, lpo.entry_status, lpo.direction,
       lpo.company_id, lpo.metric_name, lpo.position_name,
       lpo.company_scope, lpo.reason, lpo.created_at,
       au.feishu_open_id
  FROM local_permission_override lpo
  JOIN app_user au ON au.id = lpo.user_id
 WHERE lpo.permission_group_id = %s
 ORDER BY lpo.id"""

_SINGLE_REVOKE_ROW_SQL = """SELECT lpo.entry_status, lpo.direction, lpo.company_id,
       lpo.metric_name, au.feishu_open_id
  FROM local_permission_override lpo
  JOIN app_user au ON au.id = lpo.user_id
 WHERE lpo.id = %s"""

_LAZY_EXPIRE_SQL = """
UPDATE pending_action
   SET status = 'expired', reason = 'expired', decided_at = %s
 WHERE target_open_id = %s AND status = 'pending'
   AND confirm_deadline_at <= %s
RETURNING id
"""

_INSERT_PENDING_ACTION_SQL = """
INSERT INTO pending_action
    (id, action_type, target_open_id, target_state_snapshot,
    initiated_by_open_id, status, card_delivered, created_at,
     confirm_deadline_at, payload, origin_card_message_id)
VALUES (%s, %s, %s, %s, %s, 'pending', FALSE, %s, %s, %s, %s)
"""

_LOCK_ADMIN_REGISTRY_ENTRY_SQL = """
SELECT feishu_open_id, label, permission_admin_granted,
       ops_admin_granted, super_admin_granted, entry_status
  FROM admin_registry
 WHERE feishu_open_id = %s AND entry_status = 'active'
 FOR SHARE
"""

_UPDATE_ACCOUNT_STATE_SQL = (
    "UPDATE app_user SET account_state = %s, updated_at = now()"
    " WHERE feishu_open_id = %s AND account_state = %s"
)


@dataclass(frozen=True)
class PrepareOutcome:
    """:meth:`~lingxi.adapters.postgres_pending_action.PostgresPendingActionStore.prepare` 的返回形状。"""

    decision: PrepareDecision
    pending: PendingAction | None = None


@dataclass(frozen=True)
class _PendingInsertFields:
    """``_finalize_prepare_insert``/``_insert_pending_action_row`` 共用的一批新行字段。

    纯参数打包，不改变任何一个字段的取值或用途——只是把两个方法原本各自
    单独接收的多个关键字参数收进一个对象，让两个方法的形参个数回到仓库
    既有的单文件参数上限（``PLR0913``）之内；``postgres_pending_action.py``
    对本文件所在这类"存量违规"的豁免登记只覆盖它自己，不随拆分自动搬到
    这个新文件，因此这里选择不再制造新的违规，而不是登记新的豁免项。
    """

    pending_id: str
    action_type: PendingActionType
    resolved_target_open_id: str
    current_state: str | None
    initiated_by_open_id: str
    moment: datetime
    payload: str | None
    origin_card_message_id: str | None


class _ExecutionMixin:
    """``prepare``/``confirm`` 两条链路的纯计算与写入辅助方法。"""

    def _prepare_inputs(
        self,
        *,
        action_type: PendingActionType,
        target_open_id: str,
        initiated_by_open_id: str,
        position_name: str | None,
        company_scope: str | None,
        company_id: str | None,
        metric_name: str | None,
        reason: str | None,
    ) -> PrepareOutcome | tuple[bool, bool, bool, PositionPermissionExpansion | None, str | None]:
        """算出三个动作类型判据 + 职位展开 + payload，供 ``_prepare_locked`` 使用。

        返回 ``(is_revoke_action, is_local_permission_action,
        is_group_revoke_action, position_expansion, payload)``，或职位展开
        失败时的拒绝 :class:`PrepareOutcome`。
        """
        is_local_permission_action = action_type in _DIRECTION_BY_ACTION_TYPE
        is_revoke_action = action_type is PendingActionType.LOCAL_PERMISSION_REVOKE
        # 0081 基线曾把职位展开授权的 permission_group_id 写成 pending_action
        # 的 pac_ id。新授权使用专用 lpg_ 前缀，但存量不迁移，所以旧组卡仍须按组
        # 撤销，而不能被误当成历史 NULL 组的单行撤销。
        is_group_revoke_action = is_revoke_action and target_open_id.startswith(("lpg_", "pac_"))

        expansion = self._expand_position_grant(
            action_type=action_type,
            position_name=position_name,
            company_scope=company_scope,
            target_open_id=target_open_id,
            initiated_by_open_id=initiated_by_open_id,
        )
        if isinstance(expansion, PrepareOutcome):
            return expansion
        payload = self._build_local_permission_payload(
            is_local_permission_action=is_local_permission_action,
            position_expansion=expansion,
            company_id=company_id,
            metric_name=metric_name,
            reason=reason,
        )
        return (
            is_revoke_action,
            is_local_permission_action,
            is_group_revoke_action,
            expansion,
            payload,
        )

    def _finalize_prepare_insert(
        self,
        connection: Any,
        cursor: Any,
        fields: _PendingInsertFields,
        *,
        trace_id: str | None,
        decision: PrepareDecision,
    ) -> PrepareOutcome | PrepareDecision:
        """懒清扫兜底之后插入新行；成功时原样返回传入的 ``decision``。"""
        confirm_deadline_at = fields.moment + timedelta(seconds=PENDING_ACTION_TTL_SECONDS)
        self._lazy_expire_stale_pending(
            cursor,
            resolved_target_open_id=fields.resolved_target_open_id,
            moment=fields.moment,
            initiated_by_open_id=fields.initiated_by_open_id,
            trace_id=trace_id,
        )
        conflict = self._insert_pending_action_row(
            connection, cursor, fields, confirm_deadline_at=confirm_deadline_at
        )
        return conflict if conflict is not None else decision

    def _resolve_prepare_state(
        self,
        cursor: Any,
        inputs: tuple[bool, bool, bool, PositionPermissionExpansion | None, str | None],
        *,
        action_type: PendingActionType,
        target_open_id: str,
        initiated_by_open_id: str,
        reason: str | None,
        company_id: str | None,
        metric_name: str | None,
        resolved_target_open_id: str,
    ) -> PrepareOutcome | tuple[str | None, str | None, str]:
        """按动作类型分派到三种"目标当前状态"解析方式。

        ``inputs`` 是 ``_prepare_inputs`` 的返回值。统一返回
        ``(current_state, payload, resolved_target_open_id)`` 三元组，或一个
        拒绝的 :class:`PrepareOutcome`（收回分支的自我目标防呆）。
        """
        (
            is_revoke_action,
            is_local_permission_action,
            is_group_revoke_action,
            position_expansion,
            payload,
        ) = inputs
        if is_revoke_action:
            return self._resolve_revoke_target(
                cursor,
                target_open_id=target_open_id,
                initiated_by_open_id=initiated_by_open_id,
                reason=reason,
                is_group_revoke_action=is_group_revoke_action,
            )
        if is_local_permission_action:
            current_state = self._resolve_local_permission_current_state(
                cursor,
                target_open_id=target_open_id,
                action_type=action_type,
                position_expansion=position_expansion,
                company_id=company_id,
                metric_name=metric_name,
            )
            return current_state, payload, resolved_target_open_id
        current_state = self._resolve_account_state_current(cursor, target_open_id=target_open_id)
        return current_state, payload, resolved_target_open_id

    def _expand_position_grant(
        self,
        *,
        action_type: PendingActionType,
        position_name: str | None,
        company_scope: str | None,
        target_open_id: str,
        initiated_by_open_id: str,
    ) -> PositionPermissionExpansion | PrepareOutcome | None:
        """职位+公司范围展开，仅 ``LOCAL_PERMISSION_GRANT`` 支持。

        不涉及任一形参时返回 ``None``（普通路径，跳过）。失败关闭且**留痕**：外置映射文件读不出来时这次管理动作一行权限都不
        写，也**不会**退回随包默认映射悄悄放一批"看起来正常"的授权出去——
        审计只记异常类型，不记路径与文件内容，外置文件配错与"这个职位本来
        就没配映射"在管理员看到的文案上是同一句，运维靠这条审计分辨该找谁。
        """
        if position_name is None and company_scope is None:
            return None
        if action_type is not PendingActionType.LOCAL_PERMISSION_GRANT:
            return PrepareOutcome(
                decision=PrepareDecision(
                    ok=False,
                    code="position_grant_only",
                    message="职位+公司范围只支持补充授权。",
                )
            )
        try:
            from lingxi.adapters.company_function_metric_map_file import (
                load_company_function_metric_map,
            )
            from lingxi.adapters.role_function_map_file import load_role_function_map

            company_map = load_company_function_metric_map(self._metric_map_path)
            return expand_position_scope(
                position_name=position_name or "",
                company_scope=company_scope or "",
                role_function_map=load_role_function_map(),
                company_function_metric_map=company_map,
                available_companies=tuple(key for key in company_map if key != "*"),
            )
        except (OSError, ValueError, KeyError, TypeError) as error:
            self._audit.record(
                "admin.pending_action.position_mapping_unavailable",
                target=target_open_id,
                initiated_by=initiated_by_open_id,
                error=type(error).__name__,
            )
            return PrepareOutcome(
                decision=PrepareDecision(
                    ok=False,
                    code="position_mapping_unavailable",
                    message="职位或公司范围当前不可用，请重新查询后再试。",
                )
            )

    @staticmethod
    def _build_local_permission_payload(
        *,
        is_local_permission_action: bool,
        position_expansion: PositionPermissionExpansion | None,
        company_id: str | None,
        metric_name: str | None,
        reason: str | None,
    ) -> str | None:
        """授权/抑制动作的 ``payload`` JSON。

        收回动作的 payload 在 ``_resolve_revoke_target`` 里单独构造，不经过
        这里。
        """
        if not is_local_permission_action:
            return None
        body = (
            {
                "position_name": position_expansion.position_name,
                "function": position_expansion.function,
                "company_scope": position_expansion.company_scope,
                "companies": position_expansion.companies,
                "pairs": position_expansion.pairs,
                "reason": reason,
                "permission_group_id": new_id("lpg"),
            }
            if position_expansion is not None
            else {
                "company_id": company_id,
                "metric_name": metric_name,
                "reason": reason,
            }
        )
        return json.dumps(body, ensure_ascii=False)

    def _resolve_revoke_target(
        self,
        cursor: Any,
        *,
        target_open_id: str,
        initiated_by_open_id: str,
        reason: str | None,
        is_group_revoke_action: bool,
    ) -> PrepareOutcome | tuple[str | None, str | None, str]:
        """收回动作的目标解析。

        返回 ``(current_state, payload, resolved_target_open_id)``，或一个
        拒绝的 :class:`PrepareOutcome`（自我目标防呆）。新职位+范围授权在
        管理卡上是一个整体，一次收回必须覆盖该组全部展开行
        （``_resolve_group_revoke_target``）；历史 ``permission_group_id
        IS NULL`` 行按单条 override 定位（``_resolve_single_revoke_target``）。
        自我目标防呆放在这里、不在 router 层：router 拿到的只是一个不透明
        override_id，查库之前无法判断属主是不是操作者本人。
        """
        if is_group_revoke_action:
            return self._resolve_group_revoke_target(
                cursor,
                group_id=target_open_id,
                initiated_by_open_id=initiated_by_open_id,
                reason=reason,
            )
        return self._resolve_single_revoke_target(
            cursor,
            override_id=target_open_id,
            initiated_by_open_id=initiated_by_open_id,
            reason=reason,
        )

    @staticmethod
    def _resolve_group_revoke_target(
        cursor: Any, *, group_id: str, initiated_by_open_id: str, reason: str | None
    ) -> PrepareOutcome | tuple[str | None, str | None, str]:
        """整组解析；confirm() 会在同一目标用户锁下再次核对这组行。

        避免部分撤销或误伤并发新授权；partial/revoked 的组不会被当成一个
        新操作重建（判 ``"mixed"``）。
        """
        cursor.execute(_GROUP_REVOKE_ROWS_SQL, (group_id,))
        group_rows = cursor.fetchall()
        if not group_rows:
            return None, None, group_id
        owner_ids = {row[9] for row in group_rows}
        statuses = {row[1] for row in group_rows}
        directions = {row[2] for row in group_rows}
        if len(owner_ids) != 1 or len(directions) != 1 or statuses != {"active"}:
            return "mixed", None, group_id
        owner_open_id = next(iter(owner_ids))
        if owner_open_id == initiated_by_open_id:
            return PrepareOutcome(
                decision=PrepareDecision(
                    ok=False,
                    code=_REVOKE_SELF_TARGET_FORBIDDEN_CODE,
                    message=_REVOKE_SELF_TARGET_FORBIDDEN_MESSAGE,
                )
            )
        first = group_rows[0]
        payload = json.dumps(
            {
                "permission_group_id": group_id,
                "override_ids": [row[0] for row in group_rows],
                "direction": first[2],
                "position_name": first[5],
                "company_scope": first[6],
                "companies": list(dict.fromkeys(row[3] for row in group_rows)),
                "pairs": [[row[3], row[4]] for row in group_rows],
                "reason": reason,
            },
            ensure_ascii=False,
        )
        return "active", payload, owner_open_id

    @staticmethod
    def _resolve_single_revoke_target(
        cursor: Any, *, override_id: str, initiated_by_open_id: str, reason: str | None
    ) -> PrepareOutcome | tuple[str | None, str | None, str]:
        """历史 ``permission_group_id IS NULL`` 行按单条 override 定位。"""
        cursor.execute(_SINGLE_REVOKE_ROW_SQL, (override_id,))
        revoke_row = cursor.fetchone()
        if revoke_row is None:
            return None, None, override_id
        (
            entry_status,
            direction_value,
            found_company_id,
            found_metric_name,
            owner_open_id,
        ) = revoke_row
        if owner_open_id == initiated_by_open_id:
            return PrepareOutcome(
                decision=PrepareDecision(
                    ok=False,
                    code=_REVOKE_SELF_TARGET_FORBIDDEN_CODE,
                    message=_REVOKE_SELF_TARGET_FORBIDDEN_MESSAGE,
                )
            )
        payload = json.dumps(
            {
                "override_id": override_id,
                "direction": direction_value,
                "company_id": found_company_id,
                "metric_name": found_metric_name,
                "reason": reason,
            },
            ensure_ascii=False,
        )
        return entry_status, payload, owner_open_id

    @staticmethod
    def _resolve_local_permission_current_state(
        cursor: Any,
        *,
        target_open_id: str,
        action_type: PendingActionType,
        position_expansion: PositionPermissionExpansion | None,
        company_id: str | None,
        metric_name: str | None,
    ) -> str | None:
        """授权/抑制动作的"目标当前状态"。

        按 ``payload`` 里的公司×指标键，查是否已有 ``entry_status='active'``
        的同极性同键行，得到 ``"absent"``/``"present"`` 写入
        ``target_state_snapshot``——与 ``account_state`` 是同一列，只是取值域
        不同，``decide_prepare``/``decide_confirm`` 不需要关心这个区别。
        """
        cursor.execute(
            "SELECT id, account_state FROM app_user WHERE feishu_open_id = %s",
            (target_open_id,),
        )
        row = cursor.fetchone()
        user_id = row[0] if row is not None else None
        if user_id is None:
            return None
        direction = _DIRECTION_BY_ACTION_TYPE[action_type]
        pairs = (
            position_expansion.pairs
            if position_expansion is not None
            else ((company_id, metric_name),)
        )
        for pair_company_id, pair_metric_name in pairs:
            cursor.execute(
                "SELECT 1 FROM local_permission_override"
                " WHERE user_id = %s AND direction = %s AND company_id = %s"
                "   AND metric_name = %s AND entry_status = 'active'",
                (user_id, direction.value, pair_company_id, pair_metric_name),
            )
            if cursor.fetchone() is not None:
                return "present"
        return "absent"

    @staticmethod
    def _resolve_account_state_current(cursor: Any, *, target_open_id: str) -> str | None:
        cursor.execute(
            "SELECT id, account_state FROM app_user WHERE feishu_open_id = %s",
            (target_open_id,),
        )
        row = cursor.fetchone()
        return row[1] if row is not None else None

    def _lazy_expire_stale_pending(
        self,
        cursor: Any,
        *,
        resolved_target_open_id: str,
        moment: datetime,
        initiated_by_open_id: str,
        trace_id: str | None,
    ) -> None:
        """懒清扫兜底。

        这个目标如果有一条早过期却从未被点击的 ``pending`` 行，原地翻转为
        ``expired``，让出下面那条唯一索引的名额。``WHERE`` 子句本身就是
        并发安全网：同一目标同一时刻至多一条
        ``'pending'`` 行，真实并发下另一个翻转会等这把行锁、等到后发现状态
        已不是 ``'pending'``，于是影响 0 行。真的翻转了一行时补一条可分辨
        审计（``decided_by_open_id`` 留空：系统按时钟发现过期，不是管理员
        的决定）。
        """
        cursor.execute(_LAZY_EXPIRE_SQL, (moment, resolved_target_open_id, moment))
        if cursor.rowcount == 1:
            (expired_pending_action_id,) = cursor.fetchone()
            self._audit.record(
                "admin.pending_action.lazily_expired",
                pending_action_id=expired_pending_action_id,
                target=resolved_target_open_id,
                triggered_by=initiated_by_open_id,
                trace_id=trace_id,
            )

    @staticmethod
    def _insert_pending_action_row(
        connection: Any, cursor: Any, fields: _PendingInsertFields, *, confirm_deadline_at: datetime
    ) -> PrepareOutcome | None:
        """插入新的 ``pending`` 行。

        ``None`` 表示成功，否则是携带在途操作摘要的拒绝 :class:`PrepareOutcome`。
        套一层独立 SAVEPOINT：撞上同目标在途唯一索引时只回滚这条 INSERT
        本身，外层事务（懒清扫 UPDATE、之前的只读 SELECT）保持可继续提交
        的状态；走到这里仍撞上，说明这个目标真的还有一条未过期的在途操作。
        """
        from psycopg.errors import UniqueViolation

        from lingxi.adapters.postgres_pending_action import (
            _SELECT_COLUMNS,
            TARGET_HAS_PENDING_ACTION_CODE,
            TARGET_HAS_PENDING_ACTION_MESSAGE,
            _row_to_pending_action,
        )

        try:
            with connection.transaction():
                cursor.execute(
                    _INSERT_PENDING_ACTION_SQL,
                    (
                        fields.pending_id,
                        fields.action_type.value,
                        fields.resolved_target_open_id,
                        fields.current_state,
                        fields.initiated_by_open_id,
                        fields.moment,
                        confirm_deadline_at,
                        fields.payload,
                        fields.origin_card_message_id,
                    ),
                )
            return None
        except UniqueViolation:
            cursor.execute(
                f"SELECT {_SELECT_COLUMNS} FROM pending_action"
                " WHERE target_open_id = %s AND status = 'pending'",
                (fields.resolved_target_open_id,),
            )
            blocking_row = cursor.fetchone()
            message = (
                format_in_flight_conflict_message(blocking=_row_to_pending_action(blocking_row))
                if blocking_row is not None
                # 结构上不应该发生——刚撞过这条唯一索引，说明这一刻必然存在
                # 一条 'pending' 行；留一个不依赖它的兜底文案，响亮地退化，
                # 不静默吞掉查不到行这件怪事。
                else TARGET_HAS_PENDING_ACTION_MESSAGE
            )
            return PrepareOutcome(
                decision=PrepareDecision(
                    ok=False, code=TARGET_HAS_PENDING_ACTION_CODE, message=message
                )
            )

    @staticmethod
    def _lock_pending_action_row(cursor: Any, pending_action_id: str) -> PendingAction | None:
        from lingxi.adapters.postgres_pending_action import (
            _SELECT_COLUMNS,
            _row_to_pending_action,
        )

        cursor.execute(
            f"SELECT {_SELECT_COLUMNS} FROM pending_action WHERE id = %s FOR UPDATE",
            (pending_action_id,),
        )
        row = cursor.fetchone()
        return _row_to_pending_action(row) if row is not None else None

    @staticmethod
    def _lock_admin_registry_entry(cursor: Any, pending: PendingAction) -> Any | None:
        """``FOR SHARE``：允许其它事务同时也持共享锁读。

        但会与任何试图 ``UPDATE`` 该行（撤权）的并发事务互斥——撤权事务若
        已拿到排它锁但还没提交，这里会等它提交或回滚，读到的必然是"撤权前"
        或"撤权后"两个确定状态之一，不会出现中间态。
        """
        cursor.execute(_LOCK_ADMIN_REGISTRY_ENTRY_SQL, (pending.initiated_by_open_id,))
        registry_row = cursor.fetchone()
        return admin_registry_entry_from_row(registry_row) if registry_row is not None else None

    @staticmethod
    def _lock_target_account(cursor: Any, pending: PendingAction) -> tuple[str | None, str | None]:
        """锁定目标 ``app_user`` 行，返回 ``(target_user_id, current_account_state)``。

        本地权限三类动作（授权/抑制/收回）的"当前状态"不是 ``account_state``
        ——那一列在这里只用来判断目标用户是否还存在；真正的漂移检测不经过
        字符串比较，而是让 EXECUTE 分支的写入直接撞索引或看条件更新的
        ``rowcount``，这里把 prepare 时刻的快照原样喂回 ``decide_confirm``
        即可，除非目标用户已被删除，那种情况下必须仍然判定为漂移。
        """
        cursor.execute(
            "SELECT id, account_state FROM app_user WHERE feishu_open_id = %s FOR UPDATE",
            (pending.target_open_id,),
        )
        target_row = cursor.fetchone()
        target_user_id = target_row[0] if target_row is not None else None
        current_account_state = target_row[1] if target_row is not None else None

        if (
            pending.action_type in _DIRECTION_BY_ACTION_TYPE
            or pending.action_type is PendingActionType.LOCAL_PERMISSION_REVOKE
        ):
            current_account_state = (
                pending.target_state_snapshot if target_user_id is not None else None
            )
        return target_user_id, current_account_state

    def _execute_confirm_decision(
        self,
        cursor: Any,
        connection: Any,
        *,
        pending: PendingAction,
        decision: ConfirmDecision,
        current_account_state: str | None,
        target_user_id: str | None,
        moment: datetime,
    ) -> ConfirmDecision:
        """``decision.ok`` 为真时按 ``action_type`` 分派到对应 EXECUTE 分支。

        三条分支各自独立：账号停用/恢复（``_execute_account_state_change``）、
        本地权限授权/抑制（``_execute_local_permission_grant``）、本地权限
        收回（``_execute_local_permission_revoke``）。后两者可能把决策
        **降级**为 ``TARGET_DRIFTED``（prepare 到 confirm 之间目标状态已经
        变化），返回值即为最终生效的决策。
        """
        if pending.action_type in (
            PendingActionType.SUSPEND_USER,
            PendingActionType.RESUME_USER,
        ):
            self._execute_account_state_change(
                cursor,
                connection,
                pending=pending,
                decision=decision,
                current_account_state=current_account_state,
                target_user_id=target_user_id,
            )
            return decision
        if pending.action_type in _DIRECTION_BY_ACTION_TYPE:
            downgraded = self._execute_local_permission_grant(
                cursor,
                connection,
                pending=pending,
                target_user_id=target_user_id,
                moment=moment,
            )
            return downgraded if downgraded is not None else decision
        if pending.action_type is PendingActionType.LOCAL_PERMISSION_REVOKE:
            downgraded = self._execute_local_permission_revoke(
                cursor, pending=pending, moment=moment
            )
            return downgraded if downgraded is not None else decision
        return decision

    @staticmethod
    def _execute_account_state_change(
        cursor: Any,
        connection: Any,
        *,
        pending: PendingAction,
        decision: ConfirmDecision,
        current_account_state: str | None,
        target_user_id: str | None,
    ) -> None:
        """停用/恢复的 EXECUTE 分支：翻转 ``account_state``，停用额外触发「感知即清」。

        条件更新只匹配锁定时读到的 ``current_account_state``；本方法全程
        持有目标行锁，``rowcount != 1`` 结构上不可能发生。停用「感知即清」
        （产品合同：停用一经感知即触发统一、全部、不可逆清除）——"感知"这
        一刻就是这里：在 ``account_state`` 真正翻转的同一事务上，为该用户
        排队保留正文清理并清除记忆；之后任何一步导致回滚时，这里已排队的
        清理请求随事务一起撤销。
        """
        assert decision.new_account_state is not None
        cursor.execute(
            _UPDATE_ACCOUNT_STATE_SQL,
            (decision.new_account_state, pending.target_open_id, current_account_state),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"并发修改目标账号状态，预期影响 1 行，实际 {cursor.rowcount} 行")

        if pending.action_type is PendingActionType.SUSPEND_USER:
            assert target_user_id is not None
            _Transaction(connection).clear_delivered_content_for_user(
                user_id=target_user_id, reason="user_cleared"
            )
            # 记忆没有"resume 恢复"语义（硬 DELETE），与上面的保留正文清理
            # 同一条不变量。
            _Transaction(connection).clear_user_memory(user_id=target_user_id)

    @staticmethod
    def _execute_local_permission_grant(
        cursor: Any,
        connection: Any,
        *,
        pending: PendingAction,
        target_user_id: str | None,
        moment: datetime,
    ) -> ConfirmDecision | None:
        """授权/抑制的 EXECUTE 分支：逐对公司×指标插入 ``local_permission_override`` 行。

        不改 ``app_user``；返回降级后的决策，或 ``None`` 表示无需降级。
        ``target_user_id`` 在这里结构上不可能为 ``None``（目标用户已删除的
        场景会让 ``decide_confirm`` 判定 ``TARGET_DRIFTED`` 而不是 EXECUTE，
        走不到这个分支）。套一层独立 SAVEPOINT：撞上部分唯一索引（prepare
        到这一刻之间已有另一笔在同一个键上抢先落地）时只回滚这条 INSERT，
        外层事务仍可正常提交，降级为既有 ``TARGET_DRIFTED``/``FAILED``。
        """
        assert target_user_id is not None
        assert pending.payload is not None
        payload_data = json.loads(pending.payload)
        try:
            with connection.transaction():
                pairs = payload_data.get("pairs")
                if not pairs:
                    pairs = ((payload_data["company_id"], payload_data["metric_name"]),)
                for pair_company_id, pair_metric_name in pairs:
                    entry = LocalPermissionOverrideEntry(
                        user_id=target_user_id,
                        direction=_DIRECTION_BY_ACTION_TYPE[pending.action_type],
                        company_id=pair_company_id,
                        metric_name=pair_metric_name,
                        reason=payload_data["reason"],
                        initiated_by_open_id=pending.initiated_by_open_id,
                        pending_action_id=pending.id,
                        created_at=moment,
                        position_name=payload_data.get("position_name"),
                        company_scope=payload_data.get("company_scope"),
                        permission_group_id=payload_data.get("permission_group_id"),
                    )
                    _insert_locked(cursor, override_id=new_id("lpo"), entry=entry)
        except DuplicateActiveOverrideError:
            return ConfirmDecision(
                kind=ConfirmResultKind.TARGET_DRIFTED,
                message="目标用户状态已经变化，请重新查询后再发起。",
                terminal_status=PendingActionStatus.FAILED,
                reason="target_drifted",
            )
        return None

    @staticmethod
    def _execute_local_permission_revoke(
        cursor: Any, *, pending: PendingAction, moment: datetime
    ) -> ConfirmDecision | None:
        """收回的 EXECUTE 分支：条件更新 ``local_permission_override``。

        payload 在 ``prepare()`` 时刻已经把 override_id/组 ID 连同被收回行的
        方向/公司/指标一起写入。与授权/抑制的 :class:`DuplicateActiveOverrideError` 降级同一姿态，
        只是这里的冲突信号是条件更新的影响行数（``rowcount == 0``）而不是
        唯一索引异常——prepare 到这一刻之间，这一行已经被另一条路径抢先
        收回（``entry_status`` 不再是 ``'active'``）。
        """
        assert pending.payload is not None
        payload_data = json.loads(pending.payload)
        if payload_data.get("permission_group_id"):
            revoked = _revoke_group_locked(
                cursor,
                permission_group_id=payload_data["permission_group_id"],
                revoked_pending_action_id=pending.id,
                moment=moment,
                expected_override_ids=tuple(payload_data.get("override_ids", ())),
            )
        else:
            revoked = _revoke_locked(
                cursor,
                override_id=payload_data["override_id"],
                revoked_pending_action_id=pending.id,
                moment=moment,
            )
        if not revoked:
            return ConfirmDecision(
                kind=ConfirmResultKind.TARGET_DRIFTED,
                message="目标用户状态已经变化，请重新查询后再发起。",
                terminal_status=PendingActionStatus.FAILED,
                reason="target_drifted",
            )
        return None

    def _finalize_confirm_decision(
        self,
        cursor: Any,
        *,
        pending: PendingAction,
        decision: ConfirmDecision,
        clicker_open_id: str,
        moment: datetime,
    ) -> None:
        """写审计并落终态状态；审计失败或行数异常都响亮失败。

        本方法从取得行锁到这里全程持有它，正常情况下不可能有别的事务在期间
        把状态改离 ``'pending'``，不能静默当成"改成功了"。
        """
        try:
            self._audit.record(
                "admin.pending_action.confirmed",
                pending_action_id=pending.id,
                action_type=pending.action_type.value,
                outcome=decision.kind.value,
                initiated_by=pending.initiated_by_open_id,
                clicker=clicker_open_id,
            )
        except Exception as error:  # 见类文档，失败即整体回滚
            raise PendingActionAuditWriteFailedError(
                "确认操作的审计写入失败，事务已回滚，操作未执行"
            ) from error

        from lingxi.adapters.postgres_pending_action import _UPDATE_TERMINAL_STATUS_SQL

        assert decision.terminal_status is not None
        cursor.execute(
            _UPDATE_TERMINAL_STATUS_SQL,
            (
                decision.terminal_status.value,
                decision.reason,
                moment,
                clicker_open_id,
                pending.id,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                f"待确认操作状态在本事务持锁期间被改变，预期影响 1 行，实际 {cursor.rowcount} 行"
            )
