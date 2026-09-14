"""受限通道的工具全集与只读 / 准备工具的纯逻辑：入参校验、命令转译、返回形状。

只读工具不进待确认载体、不写库、不猜相似人：标识查无就是 ``not_found``。权限来源的
合成沿用发布链同一纯函数（翻译 + 两源合并），银河一侧读不到时明确报「不可读」而不是
算成零权限。五个准备工具不直调任何仓储：结构化参数逐字转译成私聊命令文本，交管理命令
路由走同一条角色判定、自我目标防呆、准备判定与审计；转译前的字段约束就是命令语法本身
（转译结果解析不出命令即拒绝），两条入口对同一意图必然得到同一结论。通道里没有任何
工具能确认、取消或执行：确认只发生在发起人本人的飞书卡片上。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from functools import partial
from typing import Any

from lingxi.core.admin.commands import AdminCommandKind, parse_admin_command
from lingxi.core.admin.innertest import (
    INNERTEST_TOOLS,
    InnertestError,
    ToolRegistry,
    ToolSpec,
    envelope,
    target_digest,
)
from lingxi.core.admin.pending_action import (
    LOCAL_PERMISSION_ACTION_TYPES,
    PendingActionType,
    local_permission_pairs,
)
from lingxi.core.admin.router_ports import AdminRouteOutcome
from lingxi.core.permission.local_override import ResolvedLocalOverrides
from lingxi.core.permission.merge_sources import merge_permission_sources
from lingxi.core.permission.metric_translation import (
    UncoveredPermissionCombinationError,
    translate_company_functions,
)
from lingxi.core.permission.publish_row import ADMIN_FULL_ACCESS_FUNCTION

READ_ONLY_TOOL_NAMES = ("get_user_status", "get_user_permission_sources", "get_pending_actions")
PREPARE_TOOL_NAMES = (
    "prepare_suspend_user",
    "prepare_resume_user",
    "prepare_grant_position",
    "prepare_revoke_permission",
    "prepare_revoke_permission_by_scope",
)
#: 每个准备工具对应的待确认动作类型；抑制没有发起入口，因此没有准备工具。
PREPARE_ACTION_TYPES: dict[str, PendingActionType] = {
    PREPARE_TOOL_NAMES[0]: PendingActionType.SUSPEND_USER,
    PREPARE_TOOL_NAMES[1]: PendingActionType.RESUME_USER,
    PREPARE_TOOL_NAMES[2]: PendingActionType.LOCAL_PERMISSION_GRANT,
    PREPARE_TOOL_NAMES[3]: PendingActionType.LOCAL_PERMISSION_REVOKE,
    PREPARE_TOOL_NAMES[4]: PendingActionType.LOCAL_PERMISSION_REVOKE,
}
PENDING_ACTION_STATUSES = ("pending", "executed", "cancelled", "expired", "failed")
REASON_MAX_LENGTH = 500
#: 撞上同目标在途唯一索引时路由给出的判定码；准备工具据此再判一次是否同一意图。
TARGET_HAS_PENDING_ACTION_CODE = "target_has_pending_action"
#: 路由回复键 → 工具结果码；拒绝键的结果码取判定码本身，不在这张表里。
_OUTCOME_CODES = {
    "admin.write_action_pending": "ok",
    "admin.write_action_unavailable": "unavailable",
    "admin.write_action_card_send_failed": "card_send_failed",
    "admin.internal_error": "internal_error",
}
_REJECTED_KEY = "admin.write_action_rejected"
_LEDGER_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,64}")
#: 「已确认」不等于「已生效」：发布观察阶段的状态单独映射，不并进动作状态。
_PUBLISH_STATES = {
    "succeeded": "published",
    "failed": "failed",
    "unknown": "unknown",
    "skipped": "skipped",
}
MAX_PENDING_ACTIONS = 20
DEFAULT_PENDING_ACTIONS = 10

#: 银河摘要里「算不出来」的三种原因：这时合成结果不可得，不能报成本地权限就是全部。
GALAXY_UNAVAILABLE_REASONS = frozenset(
    {"roster_snapshot_unavailable", "galaxy_snapshot_unavailable", "role_function_map_unavailable"}
)

_IDENTIFIER = {"type": "string", "minLength": 1, "maxLength": 128}


def _text(args, key):
    """非空且不超长的字符串字段；缺省返回 ``None``。"""
    if key not in args:
        return None
    value = args[key]
    if not isinstance(value, str) or not value or len(value) > 128:
        raise InnertestError("invalid_request")
    return value


def _validate_get_user_status(args):
    """标识与追溯号二选一，两者都给或都不给都拒绝。"""
    identifier, trace_id = _text(args, "identifier"), _text(args, "trace_id")
    if (identifier is None) == (trace_id is None):
        raise InnertestError("invalid_request")
    return args


def _validate_get_user_permission_sources(args):
    """只接受标识。"""
    if _text(args, "identifier") is None:
        raise InnertestError("invalid_request")
    return args


def _validate_get_pending_actions(args):
    """按编号查一条时不带列表参数；列表参数只有条数与状态。"""
    if _text(args, "pending_action_id") is not None:
        if len(args) != 1:
            raise InnertestError("invalid_request")
        return args
    limit = args.get("limit", DEFAULT_PENDING_ACTIONS)
    if type(limit) is not int or not 1 <= limit <= MAX_PENDING_ACTIONS:
        raise InnertestError("invalid_request")
    if "status" in args and args["status"] not in PENDING_ACTION_STATUSES:
        raise InnertestError("invalid_request")
    return args


READ_ONLY_TOOLS = (
    ToolSpec(
        name=READ_ONLY_TOOL_NAMES[0],
        description="查询用户开通与账号状态、最近事件；或按追溯号查一次交互的收口结果",
        input_schema=dict(
            type="object",
            properties={"identifier": _IDENTIFIER, "trace_id": _IDENTIFIER},
            additionalProperties=False,
            oneOf=[{"required": ["identifier"]}, {"required": ["trace_id"]}],
        ),
        validate=_validate_get_user_status,
    ),
    ToolSpec(
        name=READ_ONLY_TOOL_NAMES[1],
        description="查询用户权限来源：银河摘要展开、本地覆盖按组、合成结果与职位/公司候选",
        input_schema=dict(
            type="object",
            properties={"identifier": _IDENTIFIER},
            additionalProperties=False,
            required=["identifier"],
        ),
        validate=_validate_get_user_permission_sources,
    ),
    ToolSpec(
        name=READ_ONLY_TOOL_NAMES[2],
        description="查询本人发起的待确认动作：按编号查一条，或按状态列最近若干条",
        input_schema=dict(
            type="object",
            properties={
                "pending_action_id": _IDENTIFIER,
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_PENDING_ACTIONS,
                    "default": DEFAULT_PENDING_ACTIONS,
                },
                "status": {"type": "string", "enum": list(PENDING_ACTION_STATUSES)},
            },
            additionalProperties=False,
        ),
        validate=_validate_get_pending_actions,
    ),
)


def _single_token(args, key):
    """单段参数：非空、不含任何空白、不超长；缺省即拒绝。"""
    value = args.get(key)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or value != "".join(value.split())
    ):
        raise InnertestError("invalid_request")
    return value


def _reason_text(args):
    """原因文本：折叠空白后非空且不超过 500 字，与命令解析对多段原因的还原一致。"""
    value = args.get("reason")
    if not isinstance(value, str):
        raise InnertestError("invalid_request")
    collapsed = " ".join(value.split())
    if not collapsed or len(collapsed) > REASON_MAX_LENGTH:
        raise InnertestError("invalid_request")
    return collapsed


def prepare_command_text(name, args):
    """把准备工具的结构化参数逐字转译成私聊命令文本；未知工具名即拒绝。"""
    if name in PREPARE_TOOL_NAMES[:2]:
        verb = "suspend" if name == PREPARE_TOOL_NAMES[0] else "resume"
        return f"/admin {verb} {_single_token(args, 'identifier')}"
    if name == PREPARE_TOOL_NAMES[2]:
        return "/admin grant_position " + " ".join(
            (
                _single_token(args, "identifier"),
                _single_token(args, "position_name"),
                _single_token(args, "company_scope"),
                _reason_text(args),
            )
        )
    if name == PREPARE_TOOL_NAMES[3]:
        key = "override_id" if "override_id" in args else "group_id"
        return f"/admin revoke_permission {_single_token(args, key)} {_reason_text(args)}"
    if name == PREPARE_TOOL_NAMES[4]:
        return "/admin revoke_permission " + " ".join(
            (
                _single_token(args, "identifier"),
                _single_token(args, "company_id"),
                _single_token(args, "metric_name"),
                _reason_text(args),
            )
        )
    raise InnertestError("invalid_request")


def _validate_prepare(name, args):
    """转译后的文本必须能解析成对应命令：字段形状约束就是命令语法本身。"""
    if name == PREPARE_TOOL_NAMES[3] and ("override_id" in args) == ("group_id" in args):
        raise InnertestError("invalid_request")
    parsed = parse_admin_command(prepare_command_text(name, args))
    if parsed.kind is AdminCommandKind.UNKNOWN:
        raise InnertestError("invalid_request")
    return args


_REASON = {"type": "string", "minLength": 1, "maxLength": REASON_MAX_LENGTH}
_PREPARE_SCHEMAS = (
    ("准备停用一个用户，等待本人飞书确认", {"identifier": _IDENTIFIER}, ["identifier"], None),
    ("准备恢复一个已停用用户，等待本人飞书确认", {"identifier": _IDENTIFIER}, ["identifier"], None),
    (
        "准备按银河职位 × 公司范围补充本地权限（范围为公司编号或 *），等待本人飞书确认",
        {
            "identifier": _IDENTIFIER,
            "position_name": _IDENTIFIER,
            "company_scope": _IDENTIFIER,
            "reason": _REASON,
        },
        ["identifier", "position_name", "company_scope", "reason"],
        None,
    ),
    (
        "准备按覆盖行（lpo_）或授权组（lpg_ / 旧 pac_）编号撤销本地权限，等待本人飞书确认",
        {"override_id": _IDENTIFIER, "group_id": _IDENTIFIER, "reason": _REASON},
        ["reason"],
        [{"required": ["override_id"]}, {"required": ["group_id"]}],
    ),
    (
        "准备按用户 × 公司 × 指标撤销一条本地权限，等待本人飞书确认",
        {
            "identifier": _IDENTIFIER,
            "company_id": _IDENTIFIER,
            "metric_name": _IDENTIFIER,
            "reason": _REASON,
        },
        ["identifier", "company_id", "metric_name", "reason"],
        None,
    ),
)


def _prepare_spec(name, description, properties, required, one_of):
    schema = dict(
        type="object", properties=properties, additionalProperties=False, required=required
    )
    if one_of is not None:
        schema["oneOf"] = one_of
    return ToolSpec(
        name=name,
        description=description,
        input_schema=schema,
        validate=partial(_validate_prepare, name),
    )


PREPARE_TOOLS = tuple(
    _prepare_spec(name, *fields)
    for name, fields in zip(PREPARE_TOOL_NAMES, _PREPARE_SCHEMAS, strict=True)
)

#: 受限通道对外的全集：扩员三工具 + 只读三工具 + 准备五工具，共十一个。
CHANNEL_TOOLS = ToolRegistry(INNERTEST_TOOLS + READ_ONLY_TOOLS + PREPARE_TOOLS)


def _iso(value):
    """时间戳统一成 ISO 文本；已是文本或缺省的原样返回。"""
    return value.isoformat() if hasattr(value, "isoformat") else value


def followup_items(followups: Sequence[Any]) -> list[dict[str, Any]]:
    """阶段引用的最小可见字段。"""
    return [
        dict(
            id=ref.id,
            stage=ref.stage,
            status=ref.status,
            result_code=ref.result_code,
            updated_at=_iso(ref.updated_at),
        )
        for ref in followups
    ]


def user_status_result(*, trace_id, identifier, open_id, status, events) -> dict[str, Any]:
    """用户状态 + 最近事件；``status`` 为空即查无。"""
    if status is None:
        raise InnertestError("not_found")
    return envelope(
        trace_id=trace_id,
        query=dict(identifier=identifier, resolved_open_id=open_id),
        user=asdict(status),
        recent_events=[asdict(event) for event in events],
    )


def trace_result(*, trace_id, lookup, trace) -> dict[str, Any]:
    """追溯号视图；``trace`` 为空即查无。"""
    if trace is None:
        raise InnertestError("not_found")
    view = asdict(trace)
    view["followups"] = followup_items(trace.followups)
    return envelope(trace_id=trace_id, query=dict(trace_id=lookup), trace=view)


def _galaxy_block(summary, metric_map):
    """银河摘要展开成「公司 → 指标」；不可读、未授权、未覆盖各自说明。"""
    if summary is None or summary.reason in GALAXY_UNAVAILABLE_REASONS:
        reason = "galaxy_unavailable" if summary is None else summary.reason
        return dict(available=False, granted=False, reason=reason, company_metrics=None), None
    block = dict(
        available=True,
        granted=summary.granted,
        reason=summary.reason,
        companies=list(summary.companies),
        functions=list(summary.functions),
        all_companies=summary.all_companies,
        company_metrics=None,
    )
    if not summary.granted:
        return block, {}
    if metric_map is None:
        block["translation"] = "metric_map_unavailable"
        return block, None
    try:
        translated = translate_company_functions(
            companies=summary.companies,
            functions=summary.functions,
            all_companies=summary.all_companies,
            mapping=metric_map,
        )
    except UncoveredPermissionCombinationError:
        block["translation"] = "uncovered"
        return block, None
    block["translation"] = "translated"
    block["company_metrics"] = {company: list(v) for company, v in translated.items()}
    return block, translated


def _local_groups(overrides):
    """本地覆盖按组列出；历史无组行各自成组。"""
    groups: dict[str, dict[str, Any]] = {}
    for view in overrides:
        key = view.group_id or view.override_id
        group = groups.setdefault(
            key,
            dict(
                group_id=view.group_id,
                position_name=view.position_name,
                company_scope=view.company_scope,
                direction=view.direction,
                entries=[],
            ),
        )
        group["entries"].append(
            dict(
                override_id=view.override_id,
                company_id=view.company_id,
                metric_name=view.metric_name,
                reason=view.reason,
                created_at=view.created_at,
            )
        )
    return list(groups.values())


def _resolved_local(overrides) -> ResolvedLocalOverrides:
    """同键同时授权与抑制时抑制赢，与发布链的解析口径一致。"""
    suppressions = {(v.company_id, v.metric_name) for v in overrides if v.direction == "suppress"}
    grants = {(v.company_id, v.metric_name) for v in overrides if v.direction == "grant"}
    return ResolvedLocalOverrides(
        grants=frozenset(grants - suppressions), suppressions=frozenset(suppressions)
    )


def permission_sources_result(
    *, trace_id, identifier, open_id, status, metric_map, candidates
) -> dict[str, Any]:
    """三段：银河展开、本地按组、合成结果；任一输入不可得时合成为空并说明原因。"""
    if status is None:
        raise InnertestError("not_found")
    galaxy, galaxy_metrics = _galaxy_block(status.galaxy_source, metric_map)
    merged, merged_reason = None, None
    if galaxy_metrics is None:
        merged_reason = galaxy.get("translation") or galaxy["reason"]
    else:
        outcome = merge_permission_sources(
            galaxy=galaxy_metrics,
            local=_resolved_local(status.local_overrides),
            full_access_wildcard=ADMIN_FULL_ACCESS_FUNCTION in galaxy.get("functions", ()),
        )
        merged = dict(
            permissions={company: list(v) for company, v in outcome.permissions.items()},
            skipped_reasons=list(outcome.skipped_reasons),
            unrepresentable_companies=list(outcome.unrepresentable_companies),
        )
    return envelope(
        trace_id=trace_id,
        query=dict(identifier=identifier, resolved_open_id=open_id),
        galaxy=galaxy,
        local=dict(groups=_local_groups(status.local_overrides)),
        merged=merged,
        merged_reason=merged_reason,
        candidates=candidates,
    )


def candidate_lists(*, metric_map, positions) -> dict[str, Any]:
    """职位与公司候选来自同两份映射文件；读不到时不猜。"""
    if metric_map is None or positions is None:
        return dict(positions=None, companies=None, reason="catalog_unavailable")
    return dict(
        positions=sorted(positions),
        companies=sorted(company for company in metric_map if company != "*"),
        reason=None,
    )


def parsed_payload(payload) -> dict[str, Any] | None:
    """待确认动作的 JSON 载荷；缺省或损坏都按「没有载荷」处理。"""
    try:
        parsed = json.loads(payload) if payload else None
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def publish_state(followups: Sequence[Any]) -> str | None:
    """发布观察阶段的结论：只有它成功才是「已生效」；没有该阶段时为 ``None``。"""
    states = [ref.status for ref in followups if ref.stage == "publish_observe"]
    if not states:
        return None
    return _PUBLISH_STATES.get(states[-1], "in_progress")


def pending_action_item(row: Mapping[str, Any], followups: Sequence[Any]) -> dict[str, Any]:
    """一条待确认动作的只读投影；阶段状态如实映射，不把「已确认」说成「已生效」。"""
    parsed = parsed_payload(row.get("payload"))
    action_type = row["action_type"]
    pairs = local_permission_pairs(parsed) if parsed is not None else ()
    is_grant = action_type == PendingActionType.LOCAL_PERMISSION_GRANT.value
    is_revoke = action_type == PendingActionType.LOCAL_PERMISSION_REVOKE.value
    return dict(
        pending_action_id=row["id"],
        action_type=action_type,
        status=row["status"],
        target_open_id=row["target_open_id"],
        initiated_by_open_id=row["initiated_by_open_id"],
        card_delivered=row["card_delivered"],
        reason=row.get("reason"),
        created_at=_iso(row.get("created_at")),
        confirm_deadline_at=_iso(row.get("confirm_deadline_at")),
        decided_at=_iso(row.get("decided_at")),
        decided_by_open_id=row.get("decided_by_open_id"),
        payload=parsed,
        followups=followup_items(followups),
        publish_state=publish_state(followups),
        new_count=len(pairs) if is_grant else None,
        reused_count=(parsed or {}).get("reused_count", 0) if is_grant else None,
        retained_by_galaxy=(parsed or {}).get("galaxy_retained") if is_revoke else None,
    )


def pending_actions_result(*, trace_id, items, single) -> dict[str, Any]:
    """按编号查询时查无即 ``not_found``；列表查询允许为空。"""
    if single and not items:
        raise InnertestError("not_found")
    return envelope(trace_id=trace_id, actions=list(items), count=len(items))


def prepare_code(outcome: AdminRouteOutcome) -> str:
    """路由结论 → 工具结果码：未按管理员处理即 ``not_authorized``，拒绝键取判定码。"""
    if not outcome.handled:
        return "not_authorized"
    if outcome.content_key == _REJECTED_KEY:
        return outcome.decision_code or "rejected"
    return _OUTCOME_CODES.get(outcome.content_key, "invalid_request")


def prepare_result(*, trace_id, code, message, action=None, candidates=None, reused=False):
    """准备工具的结果信封：成功时附待确认动作投影，拒绝时附路由的回复文案。"""
    result = envelope(
        code,
        trace_id=trace_id,
        state="pending" if code == "ok" else "rejected",
        message=message,
        pending_action_id=None if action is None else action["pending_action_id"],
        action=action,
        reused_pending_action=reused,
        candidates=candidates,
    )
    if code == "ok":
        result["next_action"] = "await_admin_confirmation"
    return result


def request_intent(name, args, *, target, metric_name=None) -> tuple[str, ...]:
    """一次准备请求的意图摘要；``target`` 是已反查的目标（用户或覆盖 / 组编号）。"""
    action = PREPARE_ACTION_TYPES[name].value
    if name in PREPARE_TOOL_NAMES[:2]:
        return (action, target)
    reason = _reason_text(args)
    if name == PREPARE_TOOL_NAMES[2]:
        scope = args["company_scope"]
        if scope.casefold() in {"all", "全部"}:
            scope = "*"
        return (action, target, args["position_name"], scope, reason)
    if name == PREPARE_TOOL_NAMES[3]:
        return (action, target, reason)
    return (action, target, args["company_id"], metric_name or args["metric_name"], reason)


def row_intents(row: Mapping[str, Any]) -> frozenset[tuple[str, ...]]:
    """一条在途待确认动作可以对应的全部意图摘要（撤销有按编号与按范围两种形状）。"""
    action, target = row["action_type"], row["target_open_id"]
    payload = parsed_payload(row.get("payload")) or {}
    if action == PendingActionType.LOCAL_PERMISSION_GRANT.value:
        fields = (payload.get("position_name"), payload.get("company_scope"), payload.get("reason"))
        return frozenset({(action, target, *fields)})
    if action != PendingActionType.LOCAL_PERMISSION_REVOKE.value:
        return frozenset({(action, target)})
    reason = payload.get("reason")
    intents = {
        (action, payload[key], reason)
        for key in ("override_id", "permission_group_id")
        if payload.get(key)
    }
    if payload.get("company_id") and payload.get("metric_name"):
        intents.add((action, target, payload["company_id"], payload["metric_name"], reason))
    return frozenset(intents)


def operation_target(pending) -> dict[str, Any]:
    """五种写动作在运营审计账上的目标字段：种类、数量、摘要、目标用户与计数。"""
    payload = parsed_payload(pending.payload) or {}
    pairs = local_permission_pairs(payload)
    ids, kind, counts = [pending.target_open_id], "user", {}
    if pending.action_type is PendingActionType.LOCAL_PERMISSION_REVOKE:
        ids = list(payload.get("override_ids") or [payload.get("override_id")])
        kind = "override_group" if payload.get("permission_group_id") else "override"
        counts = {"revoked": len(pairs)}
        retained = payload.get("galaxy_retained")
        if isinstance(retained, int) and not isinstance(retained, bool):
            counts["galaxy_retained"] = retained
    elif pending.action_type in LOCAL_PERMISSION_ACTION_TYPES:
        counts = {"new": len(pairs), "reused": int(payload.get("reused_count") or 0)}
    ids = [str(value) for value in ids if value]
    return dict(
        target_kind=kind,
        target_count=len(ids),
        target_digest="sha256:" + target_digest(ids),
        target_user_id=ledger_identifier(pending.target_open_id),
        result_counts=counts,
    )


def ledger_identifier(value) -> str | None:
    """账上的标识列只收固定形状；邮箱这类原文不进账（调用方只留摘要）。"""
    return value if isinstance(value, str) and _LEDGER_IDENTIFIER.fullmatch(value) else None
