"""受限通道的工具全集与只读三工具的纯逻辑：入参校验、返回形状、查无语义。

只读工具不进待确认载体、不写库、不猜相似人：标识查无就是 ``not_found``。权限来源的
合成沿用发布链同一纯函数（翻译 + 两源合并），银河一侧读不到时明确报「不可读」而不是
算成零权限。``reused_count`` 与 ``retained_by_galaxy`` 由差集补齐切片填值，这里先固定
位置、值为 ``None``。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any

from lingxi.core.admin.innertest import (
    INNERTEST_TOOLS,
    InnertestError,
    ToolRegistry,
    ToolSpec,
    envelope,
)
from lingxi.core.permission.local_override import ResolvedLocalOverrides
from lingxi.core.permission.merge_sources import merge_permission_sources
from lingxi.core.permission.metric_translation import (
    UncoveredPermissionCombinationError,
    translate_company_functions,
)
from lingxi.core.permission.publish_row import ADMIN_FULL_ACCESS_FUNCTION

READ_ONLY_TOOL_NAMES = ("get_user_status", "get_user_permission_sources", "get_pending_actions")
PENDING_ACTION_STATUSES = ("pending", "executed", "cancelled", "expired", "failed")
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

#: 受限通道对外的全集：扩员三工具 + 只读三工具；写动作的准备工具由后续切片追加。
CHANNEL_TOOLS = ToolRegistry(INNERTEST_TOOLS + READ_ONLY_TOOLS)


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


def pending_action_item(row: Mapping[str, Any], followups: Sequence[Any]) -> dict[str, Any]:
    """一条待确认动作的只读投影；阶段状态如实映射，不把「已确认」说成「已生效」。"""
    payload = row.get("payload")
    try:
        parsed = json.loads(payload) if payload else None
    except ValueError:
        parsed = None
    return dict(
        pending_action_id=row["id"],
        action_type=row["action_type"],
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
        reused_count=None,
        retained_by_galaxy=None,
    )


def pending_actions_result(*, trace_id, items, single) -> dict[str, Any]:
    """按编号查询时查无即 ``not_found``；列表查询允许为空。"""
    if single and not items:
        raise InnertestError("not_found")
    return envelope(trace_id=trace_id, actions=list(items), count=len(items))
