"""受限通道五个准备工具的适配：转译成命令文本交管理命令路由，本模块不直调任何写口。

角色判定、自我目标防呆、目标状态判定、待确认操作的插入与审计动作名全部由
``AdminCommandRouter`` 沿用私聊路径完成；这里只做三件路由之外的事：撞上「同目标已有在途
操作」时判断是不是同一意图（是则沿用那条待确认操作、不再发第二张卡）、判定拒绝时往运营
审计账写一行 ``rejected``、把结果投影成与 ``get_pending_actions`` 同一形状。
"""

from __future__ import annotations

from datetime import UTC, datetime

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.adapters.postgres_innertest import admin_roles_snapshot
from lingxi.adapters.postgres_operation_audit import record_operation_audit
from lingxi.core.admin.innertest import InnertestError, target_digest
from lingxi.core.admin.operation_audit import EntryPoint, OperationAuditEntry, OperationPhase
from lingxi.core.admin.restricted_tools import (
    MAX_PENDING_ACTIONS,
    PREPARE_ACTION_TYPES,
    PREPARE_TOOL_NAMES,
    TARGET_HAS_PENDING_ACTION_CODE,
    candidate_lists,
    ledger_identifier,
    prepare_code,
    prepare_command_text,
    prepare_result,
    request_intent,
    row_intents,
)

_REUSED_MESSAGE = "同一意图的待确认操作已在途，沿用该操作，不再重复发送确认卡片。"
_CANDIDATE_CODES = frozenset({"position_mapping_unavailable"})


class RestrictedAdminPrepare:
    """五个准备工具；每个方法的签名与只读工具一致，供通道服务按名字分发。"""

    def __init__(
        self,
        dsn,
        *,
        router,
        queries,
        readonly,
        timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS,
    ):
        """``router`` 是唯一写入口；``queries`` 只用于反查标识，``readonly`` 只用于投影。"""
        self._dsn, self._timeouts = dsn, timeouts
        self._router, self._queries, self._readonly = router, queries, readonly

    def prepare_suspend_user(self, principal, *, call_trace_id, identifier):
        """``/admin suspend <identifier>``。"""
        return self._prepare(
            principal, PREPARE_TOOL_NAMES[0], dict(identifier=identifier), call_trace_id
        )

    def prepare_resume_user(self, principal, *, call_trace_id, identifier):
        """``/admin resume <identifier>``。"""
        return self._prepare(
            principal, PREPARE_TOOL_NAMES[1], dict(identifier=identifier), call_trace_id
        )

    def prepare_grant_position(
        self, principal, *, call_trace_id, identifier, position_name, company_scope, reason
    ):
        """``/admin grant_position <identifier> <职位> <范围> <原因>``。"""
        args = dict(
            identifier=identifier,
            position_name=position_name,
            company_scope=company_scope,
            reason=reason,
        )
        return self._prepare(principal, PREPARE_TOOL_NAMES[2], args, call_trace_id)

    def prepare_revoke_permission(
        self, principal, *, call_trace_id, reason, override_id=None, group_id=None
    ):
        """``/admin revoke_permission <覆盖或组编号> <原因>``。"""
        args = dict(reason=reason)
        if override_id is not None:
            args["override_id"] = override_id
        if group_id is not None:
            args["group_id"] = group_id
        return self._prepare(principal, PREPARE_TOOL_NAMES[3], args, call_trace_id)

    def prepare_revoke_permission_by_scope(
        self, principal, *, call_trace_id, identifier, company_id, metric_name, reason
    ):
        """``/admin revoke_permission <identifier> <公司> <指标> <原因>``。"""
        args = dict(
            identifier=identifier, company_id=company_id, metric_name=metric_name, reason=reason
        )
        return self._prepare(principal, PREPARE_TOOL_NAMES[4], args, call_trace_id)

    def _prepare(self, principal, name, args, trace_id):
        """转译 → 路由 → 按结论投影；判定拒绝写 ``rejected`` 行，撞在途时再判同一意图。"""
        outcome = self._router.route(
            open_id=principal.open_id, text=prepare_command_text(name, args), trace_id=trace_id
        )
        code = prepare_code(outcome)
        if code == "not_authorized":
            raise InnertestError(code)
        if code == "ok":
            return self._prepared(
                principal, outcome.pending_action_id, trace_id, outcome.reply_text
            )
        if code == TARGET_HAS_PENDING_ACTION_CODE:
            same = self._same_intent_in_flight(principal, name, args)
            if same is not None:
                return self._prepared(principal, same, trace_id, _REUSED_MESSAGE, reused=True)
        if outcome.decision_code:
            self._record_rejected(principal, name, args, code=code, trace_id=trace_id)
        action = None
        if outcome.pending_action_id:
            action = self._readonly.action_item(principal.open_id, outcome.pending_action_id)
        return prepare_result(
            trace_id=trace_id,
            code=code,
            message=outcome.reply_text,
            action=action,
            candidates=self._candidates() if code in _CANDIDATE_CODES else None,
        )

    def _prepared(self, principal, pending_action_id, trace_id, message, *, reused=False):
        """成功或沿用在途：结果里带与 ``get_pending_actions`` 同一形状的投影。"""
        action = self._readonly.action_item(principal.open_id, pending_action_id)
        if action is None:
            raise InnertestError("query_unavailable")
        return prepare_result(
            trace_id=trace_id, code="ok", message=message, action=action, reused=reused
        )

    def _candidates(self):
        """职位 / 公司候选来自与准备判定同两份映射文件。"""
        metric_map, positions = self._readonly.catalog()
        return candidate_lists(metric_map=metric_map, positions=positions)

    def _resolved_target(self, name, args):
        """把请求里的目标反查成与在途行同一口径：邮箱 → open_id，指标别名 → 指标标识。"""
        if name == PREPARE_TOOL_NAMES[3]:
            return args.get("override_id") or args.get("group_id"), None
        target = self._queries.resolve_identifier(identifier=args["identifier"])
        metric = None
        if name == PREPARE_TOOL_NAMES[4]:
            metric = self._queries.resolve_metric_name(metric_token=args["metric_name"])
        return target, metric

    def _same_intent_in_flight(self, principal, name, args):
        """本人在途且未过期的行里，意图摘要与本次相同的那一条；没有即 ``None``。"""
        target, metric = self._resolved_target(name, args)
        intent = request_intent(name, args, target=target, metric_name=metric)
        now = datetime.now(UTC)
        rows = self._readonly.pending_rows(
            principal.open_id, pending_action_id=None, limit=MAX_PENDING_ACTIONS, status="pending"
        )
        for row in rows:
            deadline = row.get("confirm_deadline_at")
            if deadline is not None and deadline <= now:
                continue
            if intent in row_intents(row):
                return row["id"]
        return None

    def _record_rejected(self, principal, name, args, *, code, trace_id):
        """判定拒绝也进运营审计账：操作号用本次追溯号，账写不进即按审计不可用拒绝。"""
        target, _ = self._resolved_target(name, args)
        kind = "user"
        if name == PREPARE_TOOL_NAMES[3]:
            kind = "override_group" if str(target).startswith(("lpg_", "pac_")) else "override"
        try:
            with connect(self._dsn, timeouts=self._timeouts) as connection:
                with connection.transaction():
                    entry = OperationAuditEntry(
                        operation_id=trace_id,
                        operation="admin." + PREPARE_ACTION_TYPES[name].value,
                        phase=OperationPhase.REJECTED,
                        initiated_by=principal.open_id,
                        actor_roles=admin_roles_snapshot(connection, principal.open_id),
                        entry_point=EntryPoint.RESTRICTED_CHANNEL,
                        target_kind=kind,
                        target_count=1,
                        target_digest="sha256:" + target_digest([str(target)]),
                        target_user_id=ledger_identifier(target) if kind == "user" else None,
                        result_code=code,
                        trace_id=trace_id,
                    )
                    record_operation_audit(connection, entry)
        except Exception as error:
            raise InnertestError("audit_unavailable") from error


def build_restricted_prepare(dsn, *, audit, metric_map_path, timeouts, queries, readonly, store):
    """组合准备工具：路由 + 待确认仓储 + 持久阶段发卡口，全部沿用私聊路径同一批实现。"""
    from lingxi.adapters.admin_registry import PostgresAdminRegistryLookup
    from lingxi.adapters.followup_confirm_card_sender import FollowupConfirmCardSender
    from lingxi.adapters.postgres_pending_action import PostgresPendingActionStore
    from lingxi.core.admin.router import AdminCommandRouter

    pending_actions = PostgresPendingActionStore(
        dsn, timeouts=timeouts, audit=audit, metric_map_path=metric_map_path
    )
    router = AdminCommandRouter(
        registry=PostgresAdminRegistryLookup(dsn, timeouts=timeouts),
        queries=queries,
        audit=audit,
        display_names=queries,
        pending_actions=pending_actions,
        confirm_cards=FollowupConfirmCardSender(store=store, tracker=pending_actions, audit=audit),
    )
    return RestrictedAdminPrepare(
        dsn, router=router, queries=queries, readonly=readonly, timeouts=timeouts
    )
