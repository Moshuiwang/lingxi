"""两源合并的集中合并（纯函数）：银河 ∪ 本地授权 − 本地抑制。

上游已经分别把「银河这一侧解释出什么」（:mod:`lingxi.core.permission.publish_row`、
:mod:`lingxi.core.permission.metric_translation`）与「本地覆盖这一侧解决出什么」
（:mod:`lingxi.core.permission.local_override`，``suppress`` 赢已经内化）分别答完；
本模块只回答最后一步："最终真实拥有的权限"。真实权限 ``= (银河 ∪ 本地授权) −
本地抑制``，在 ``{公司ID 或全公司通配 "*"：值字符串列表}`` 粒度上做集合运算——
本函数不关心值字符串的语义，因此 ``galaxy`` 无论来自哪一层都能正确合并。
``galaxy`` 出现 :data:`ALL_COMPANIES_KEY` 通配键时有两种互相独立、字符串层面
不可分的成因（银河后台管理员的真全指标通配，或全公司范围但职能有限的有限指标
通配），调用方必须显式传入 ``full_access_wildcard`` 声明是哪一种，不猜测、无
默认值。本地授权若带 ``company_id="*"``（「全部」组）在银河无通配键时走独立
分支，能被减到空的公司登记进 ``unrepresentable_companies`` 交调用方 fail-closed；
**减到空集合的键一律丢弃，不产出空列表**。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from lingxi.core.permission.local_override import ResolvedLocalOverrides, to_company_metric_map

#: 「全非」通配在合并输入/输出里的公司键。与 ``publish_row.ALL_COMPANIES_KEY``、
#: ``metric_translation.ALL_COMPANIES_KEY`` 是同一字面量的独立拷贝——三处既有先例
#: 不建立反向依赖，一致性由各自测试分别钉住同一个值。
ALL_COMPANIES_KEY = "*"

#: 真全指标通配下，本地授权被跳过（冗余，通配已经覆盖全部指标）。
REASON_GRANT_REDUNDANT_WILDCARD = "grant_redundant_wildcard"

#: 真全指标通配下，本地抑制被跳过（拦不住通配，抑制的是具体公司键）。
REASON_SUPPRESS_INAPPLICABLE_WILDCARD = "suppress_inapplicable_wildcard"

#: 读本地覆盖条目时数据库异常：调用方据此让该用户本轮跳过本地源，而不是让异常
#: 冒泡带走整轮/整次开通。两个调用点共用同一个原因码字面量，审计动作名各自不同，
#: 见各自模块文档。
REASON_LOCAL_OVERRIDE_READ_FAILED = "local_override_read_failed"


@dataclass(frozen=True)
class MergedPermissionSources:
    """一次两源合并的结果：最终权限映射 + 为什么某些输入被跳过。

    :attr:`skipped_reasons` 只在**真全指标通配**（``full_access_wildcard=True``）
    生效时非空；**有限指标通配**（``full_access_wildcard=False``）下本地授权/抑制
    改为参与合并，恒不登记跳过原因。本模块是纯函数、不做任何 I/O，真正的审计调用
    留给两个调用点各自完成。
    """

    permissions: Mapping[str, tuple[str, ...]]
    skipped_reasons: tuple[str, ...]
    #: 本地「全部」组下被本地抑制减到**空**的具体公司键：读侧回退制没有"某公司零
    #: 指标、其余公司按 "*""的可表示形状，调用方必须 fail-closed（不发布、不撤权），
    #: 理由码 ``suppression_on_all_scope_unrepresentable``。非空时 ``permissions``
    #: 仍是"假如没有这些公司"的合成结果，调用方不得拿去发布。
    unrepresentable_companies: tuple[str, ...] = ()


def merge_permission_sources(
    *,
    galaxy: Mapping[str, Sequence[str]],
    local: ResolvedLocalOverrides | None,
    full_access_wildcard: bool,
) -> MergedPermissionSources:
    """真实权限 ``= (银河 ∪ 本地授权) − 本地抑制``。

    ``local=None`` 表示"这一轮本地源不参与"，对结果恒等：产出与 ``galaxy`` 逐
    字节相同。``full_access_wildcard`` 必填、无默认值——``galaxy`` 没有
    :data:`ALL_COMPANIES_KEY` 键时取值不影响结果但仍须显式传入。三条分支：真全
    指标通配下本地整体不参与、只登记跳过原因；有限指标通配下本地并/减到同一个
    ``"*"`` 键，不产出具体公司键；其余情况按银河键 ∪ 本地授权键逐键取
    ``(银河∪本地授权) − 本地抑制``，本地「全部」组另有防扩权的最低限（见
    :func:`_merge_local_all_group`）。
    """
    galaxy_map: dict[str, tuple[str, ...]] = {key: tuple(values) for key, values in galaxy.items()}

    if ALL_COMPANIES_KEY in galaxy_map:
        if full_access_wildcard:
            return _merge_full_wildcard(galaxy_map, local)
        return _merge_limited_wildcard(galaxy_map, local)

    local_grants = to_company_metric_map(local.grants) if local is not None else {}
    local_suppressions = to_company_metric_map(local.suppressions) if local is not None else {}

    if ALL_COMPANIES_KEY in local_grants:
        result = _merge_local_all_group(galaxy_map, local_grants, local_suppressions)
        if result is not None:
            return result
        # 组被 "*" 抑制减到空：本地没有「全部」授权了，回到下面的非通配代数
        # （与非通配分支的空结果同一语义：丢键、不写空列表）。
        local_grants = {
            company: values
            for company, values in local_grants.items()
            if company != ALL_COMPANIES_KEY
        }

    return _merge_default(galaxy_map, local_grants, local_suppressions)


def _merge_full_wildcard(
    galaxy_map: dict[str, tuple[str, ...]], local: ResolvedLocalOverrides | None
) -> MergedPermissionSources:
    """真全指标通配：本地整体不参与，只登记为什么被跳过。"""
    skipped: list[str] = []
    if local is not None and local.grants:
        skipped.append(REASON_GRANT_REDUNDANT_WILDCARD)
    if local is not None and local.suppressions:
        skipped.append(REASON_SUPPRESS_INAPPLICABLE_WILDCARD)
    return MergedPermissionSources(permissions=galaxy_map, skipped_reasons=tuple(skipped))


def _merge_limited_wildcard(
    galaxy_map: dict[str, tuple[str, ...]], local: ResolvedLocalOverrides | None
) -> MergedPermissionSources:
    """有限指标通配：把本地条目并/减到同一个 ``"*"`` 键，不产出具体公司键。

    全部本地 grant/suppress 指标名（忽略各自的 company_id）直接在 ``"*"`` 这
    一份清单上做并集/减集——防窄化回归：具体键会让读侧对该公司不再回退通配。
    """
    baseline = set(galaxy_map[ALL_COMPANIES_KEY])
    grant_metrics = {metric for _, metric in local.grants} if local is not None else set()
    suppress_metrics = {metric for _, metric in local.suppressions} if local is not None else set()
    wildcard_values = (baseline | grant_metrics) - suppress_metrics

    wildcard_permissions: dict[str, tuple[str, ...]] = {}
    if wildcard_values:
        wildcard_permissions[ALL_COMPANIES_KEY] = tuple(sorted(wildcard_values))
    # wildcard_values 为空：丢弃 "*" 键，不写空列表——缺键与空列表对读侧的
    # lookup_metrics 等价（缺键回退通配，这里通配本就不存在，两者都收敛到空）。
    return MergedPermissionSources(permissions=wildcard_permissions, skipped_reasons=())


def _merge_local_all_group(
    galaxy_map: dict[str, tuple[str, ...]],
    local_grants: dict[str, tuple[str, ...]],
    local_suppressions: dict[str, tuple[str, ...]],
) -> MergedPermissionSources | None:
    """本地「全部」组：银河侧没有 ``"*"`` 键而本地授权带 ``"*"`` 公司键时的合并。

    ``"*"`` 键＝本地 ``"*"`` 指标 − ``"*"`` 抑制（组本身）；具体键＝``"*"`` ∪
    该公司银河值 ∪ 该公司本地授权 − 该公司抑制，与 ``"*"`` 相同则省略。每个
    具体键都 ⊇ ``"*"``（不会无故更窄，抑制咬住时更窄是正确语义），公司专有的
    指标只留在该公司键下、不被抹平到全部公司（不扩权）。某公司减到空不可表示，
    登记进 ``unrepresentable_companies`` 交调用方 fail-closed。组本身被 ``"*"``
    抑制减到空时返回 ``None``，调用方回退到非通配代数。
    """
    star = set(local_grants[ALL_COMPANIES_KEY]) - set(local_suppressions.get(ALL_COMPANIES_KEY, ()))
    if not star:
        return None
    collapsed: dict[str, tuple[str, ...]] = {ALL_COMPANIES_KEY: tuple(sorted(star))}
    unrepresentable: list[str] = []
    companies = (
        set(galaxy_map)
        | {company for company in local_grants if company != ALL_COMPANIES_KEY}
        | {company for company in local_suppressions if company != ALL_COMPANIES_KEY}
    )
    for company in sorted(companies):
        values = star | set(galaxy_map.get(company, ())) | set(local_grants.get(company, ()))
        values -= set(local_suppressions.get(company, ()))
        if not values:
            unrepresentable.append(company)
        elif values != star:
            collapsed[company] = tuple(sorted(values))
    return MergedPermissionSources(
        permissions=collapsed,
        skipped_reasons=(),
        unrepresentable_companies=tuple(unrepresentable),
    )


def _merge_default(
    galaxy_map: dict[str, tuple[str, ...]],
    local_grants: dict[str, tuple[str, ...]],
    local_suppressions: dict[str, tuple[str, ...]],
) -> MergedPermissionSources:
    """非通配代数：结果键 = 银河键 ∪ 本地授权键，逐键取并集减抑制，空键丢弃。"""
    keys = set(galaxy_map) | set(local_grants)
    merged: dict[str, tuple[str, ...]] = {}
    for key in keys:
        values = set(galaxy_map.get(key, ())) | set(local_grants.get(key, ()))
        values -= set(local_suppressions.get(key, ()))
        if values:
            merged[key] = tuple(sorted(values))
        # values 为空：丢弃这个键，不写空列表——与翻译层"写侧不产出空列表"同一纪律。

    return MergedPermissionSources(permissions=merged, skipped_reasons=())


__all__ = [
    "ALL_COMPANIES_KEY",
    "REASON_GRANT_REDUNDANT_WILDCARD",
    "REASON_LOCAL_OVERRIDE_READ_FAILED",
    "REASON_SUPPRESS_INAPPLICABLE_WILDCARD",
    "MergedPermissionSources",
    "merge_permission_sources",
]
