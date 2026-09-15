"""撤销回执的只读回显：一批本地公司×指标对里，有几项经银河来源仍然持有（纯函数）。

撤销只撤本地补充库里的行，不碰银河；用户最终能不能查到，仍由「银河 ∪ 本地授权 −
本地抑制」决定。管理员看到「撤销成功」时需要知道其中有几项其实被银河照样覆盖，
否则「撤销成功但用户照样能查」会被当成故障。本模块只回答这个计数，不参与任何权限
判定与发布；通配语义逐字对齐 :mod:`lingxi.core.permission.merge_sources` 的三条分支。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from lingxi.core.permission.metric_translation import translate_company_functions
from lingxi.core.permission.publish_row import ADMIN_FULL_ACCESS_FUNCTION

#: 与 ``merge_sources.ALL_COMPANIES_KEY`` 同一字面量的独立拷贝（既有惯例：不建立
#: 反向依赖，一致性由各自测试钉住同一个值）。
ALL_COMPANIES_KEY = "*"


@dataclass(frozen=True)
class GalaxyMetricMap:
    """一个用户银河来源当前翻译出的「公司（或 ``"*"``）→ 指标名」映射。

    ``full_access_wildcard`` 与 :func:`merge_permission_sources` 的同名参数同义：
    ``"*"`` 键有两种字符串层面不可分的成因（后台管理员的真全指标通配、全公司范围
    但职能有限的有限指标通配），必须由构造方显式声明，本模块不猜。
    """

    permissions: Mapping[str, tuple[str, ...]]
    full_access_wildcard: bool


def build_galaxy_metric_map(
    *,
    companies: Sequence[str],
    functions: Sequence[str],
    all_companies: bool,
    mapping: Mapping[str, Mapping[str, Sequence[str]]],
) -> GalaxyMetricMap:
    """把银河聚合结果（公司范围 + 职能标签）翻译成指标映射，并标出通配成因。

    翻译与发布链走同一个 :func:`translate_company_functions`；映射覆盖不全时它会
    响亮失败，调用方据此把结果记成「读不到」而不是 0 项。
    """
    permissions = translate_company_functions(
        companies=companies, functions=functions, all_companies=all_companies, mapping=mapping
    )
    return GalaxyMetricMap(
        permissions=permissions,
        full_access_wildcard=ADMIN_FULL_ACCESS_FUNCTION in functions,
    )


def retained_by_galaxy(pairs: Iterable[tuple[str, str]], galaxy_map: GalaxyMetricMap) -> int:
    """``pairs`` 里有几项在 ``galaxy_map`` 下仍被银河来源覆盖。

    三条分支与 :func:`merge_permission_sources` 逐一对应：真全指标通配下每一项都
    仍持有；有限指标通配下按 ``"*"`` 键的指标清单判定（读侧缺键回退通配）；无通配
    时按具体公司键判定，本地「全部」组（公司为 ``"*"``）的项不算仍持有——银河没有
    覆盖全部公司，撤掉后该指标至少在某些公司上会消失。
    """
    permissions = galaxy_map.permissions
    if ALL_COMPANIES_KEY in permissions:
        if galaxy_map.full_access_wildcard:
            return sum(1 for _ in pairs)
        wildcard = set(permissions[ALL_COMPANIES_KEY])
        return sum(1 for _, metric in pairs if metric in wildcard)
    return sum(
        1
        for company, metric in pairs
        if company != ALL_COMPANIES_KEY and metric in permissions.get(company, ())
    )


__all__ = ["ALL_COMPANIES_KEY", "GalaxyMetricMap", "build_galaxy_metric_map", "retained_by_galaxy"]
