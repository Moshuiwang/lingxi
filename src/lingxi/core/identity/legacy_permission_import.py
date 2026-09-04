"""存量用户首聊差集导入与「银河翻译只算一次」的两个步骤（rc25 S-1，Issue #540）。

从 ``core/identity/onboarding_runner.py`` 拆出（该文件受 1500 行体量棘轮约束）：两个函数
都只编排注入进来的协作者，不做 I/O；``AutoOnboardingRunner`` 的同名私有方法只是委派。
口径见 ``core/permission/legacy_diff.py`` 模块文档，产品裁定见
``docs/决策记录/2026-09-02-存量用户首开差集导入与发布只在变化时回写.md``。
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, NoReturn

from lingxi.core.identity.onboarding_ports import LegacyPermissionImporter, _AuditSink
from lingxi.core.identity.onboarding_terminal import OnboardingChainError, _internal, _Terminal
from lingxi.core.permission.legacy_diff import (
    REASON_ALL_METRICS_UNAVAILABLE,
    SHAPE_UNSUPPORTED_WILDCARD,
    plan_legacy_import,
)
from lingxi.core.permission.metric_translation import (
    UncoveredPermissionCombination,
    translate_company_functions,
)
from lingxi.core.permission.publish_row import parse_permissions

logger = logging.getLogger(__name__)


def translate_galaxy(
    *,
    audit: _AuditSink,
    metric_translation_map: Mapping[str, Mapping[str, Sequence[str]]],
    user_id: str,
    aggregate: Any,
) -> dict[str, tuple[str, ...]] | _Terminal:
    """把银河聚合结果翻译成「公司 → 指标名」，供存量差集导入与发布共用同一份结果。

    零银河权限恒返回 ``{}``（``aggregate.companies``/``functions`` 此时必为空，不调用
    翻译——对空输入它会按"参数缺失"拒绝）。翻译失败按本侧故障 fail-closed（#346）：
    存在未覆盖的「公司 + 职能」组合时整条链拒绝发布，不猜测、不回落成职能标签、不
    产出部分结果；这不是"银河说他没有权限"，是我们这一侧的翻译内容缺口，归
    ``_internal``，审计动作与理由码与 rc25 之前 `_publish` 内逐字相同。
    """

    if not aggregate.granted:
        return {}
    try:
        company_metrics = translate_company_functions(
            companies=aggregate.companies,
            functions=aggregate.functions,
            all_companies=aggregate.all_companies,
            mapping=metric_translation_map,
        )
    except UncoveredPermissionCombination as error:
        reason = (
            "permission_translation_unavailable"
            if error.mapping_is_empty
            else "permission_translation_uncovered"
        )
        audit.record("onboarding.publish_gate_closed", user=user_id, reason=reason)
        return _internal(reason)
    return dict(company_metrics)


def import_legacy_permissions(
    *,
    importer: LegacyPermissionImporter,
    audit: _AuditSink,
    metric_translation_map: Mapping[str, Mapping[str, Sequence[str]]],
    now: datetime,
    user_id: str,
    permissions_text: str | None,
    full_access_wildcard: bool,
    galaxy_map: Mapping[str, Sequence[str]],
    open_id: str,
    trace_id: str,
) -> None:
    """存量用户首聊差集导入。只在正式表有该用户的行且密文可采纳时被调用：把「旧行权限 −
    银河当前翻译」落成管理员本地授权（原因「2.0 迁移导入」，逐行/整组可撤销），随后的零
    银河判定与发布从本地覆盖表读回、按既有公式合并——这里不加第二套合并逻辑。

    **fail-closed**：形状不受支持、原文解析失败、映射无指标可展开、导入事务失败四种情况
    一律抛 :class:`OnboardingChainError`（用户侧 ``LX-ONBOARD-001``、管理群通知），外部表
    零写入——一旦发布，旧行会被不可逆覆盖。审计只记形状与计数，不记原文、公司键或指标名。
    """

    try:
        # 空白单元格按空对象处理（独立审核 P2-2）：它没有任何会被发布覆盖的内容，
        # fail-closed 的理由在这里不成立；解析失败（非法 JSON、空白键/指标）仍 fail-closed。
        text = permissions_text.strip() if isinstance(permissions_text, str) else ""
        document = parse_permissions(text) if text else {}
        plan = plan_legacy_import(
            legacy=document,
            galaxy_current=galaxy_map,
            full_access_wildcard=full_access_wildcard,
            mapping=metric_translation_map,
        )
    except ValueError:
        _fail(audit, user_id, "legacy_permissions_unparseable", trace_id)
    if plan.shape == SHAPE_UNSUPPORTED_WILDCARD:
        _fail(audit, user_id, "legacy_wildcard_shape_unsupported", trace_id)
    if REASON_ALL_METRICS_UNAVAILABLE in plan.skipped_reasons:
        _fail(audit, user_id, REASON_ALL_METRICS_UNAVAILABLE, trace_id)
    if plan.nothing_to_import:
        audit.record(
            "onboarding.legacy_permission_import_skipped",
            user=user_id,
            shape=plan.shape,
            reasons=list(plan.skipped_reasons),
            trace_id=trace_id,
        )
        return
    try:
        report = importer.import_plan(user_id=user_id, target_open_id=open_id, plan=plan, now=now)
    except Exception as error:  # 落库失败一律 fail-closed
        # 异常类型进审计理由码；调用栈只进日志（不含原文/公司键/指标名），
        # 让运维能区分「接口漏接」与「库故障」。
        #
        # C-6（对抗审查 2026-09-02）：这里原本用 `logger.exception`——上面那句
        # "完整 traceback 不含原文" 对 `logger.exception` **不成立**：它连异常自己
        # 写的那句话一起记，而 psycopg 的唯一键冲突正文带着
        # `Key (feishu_open_id)=(ou_…)`（违 V-花名册-33），连接失败正文带
        # host/user/dbname。改成只记类型名 + 调用栈帧后，那句承诺才为真。
        logger.error(
            "存量差集导入落库失败 user=%s error=%s\n调用栈（不含异常正文）：\n%s",
            user_id,
            type(error).__name__,
            "".join(traceback.format_tb(error.__traceback__)),
        )
        _fail(audit, user_id, f"legacy_permission_import_failed_{type(error).__name__}", trace_id, error)
    audit.record(
        "onboarding.legacy_permission_import",
        user=user_id,
        shape=plan.shape,
        pairs=len(plan.pairs),
        all_scope_metrics=len(plan.all_scope_metrics),
        imported=report.imported,
        already_present=report.already_present,
        group_created=report.group_created,
        group_skipped_revoked=report.group_skipped_revoked,
        revoked_skipped=report.revoked_skipped,
        unmapped_companies_kept=plan.unmapped_companies_kept,
        trace_id=trace_id,
    )


def _fail(
    audit: _AuditSink, user_id: str, code: str, trace_id: str, cause: BaseException | None = None
) -> NoReturn:
    audit.record(
        "onboarding.legacy_permission_import_failed", user=user_id, reason=code, trace_id=trace_id
    )
    raise OnboardingChainError(code) from cause


__all__ = ["import_legacy_permissions", "translate_galaxy"]
