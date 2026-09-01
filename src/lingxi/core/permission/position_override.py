"""职位 + 公司范围本地补充授权的纯逻辑。

管理卡收集的是产品语义上的「银河职位」和「公司范围」，而权限发布层仍然消费
``(company_id, metric_name)`` 行。本模块只做两步确定性的展开，不读数据库、不读文件：
职位必须在银河职位映射中精确命中，随后按公司×职能映射取得指标。调用方负责把配置
文件加载为 mapping，并在确认事务中保存展开结果，避免映射在等待确认期间漂移。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


ALL_COMPANIES_SCOPE = "*"


@dataclass(frozen=True)
class PositionPermissionExpansion:
    """一次职位范围选择对应的确定性权限集合。"""

    position_name: str
    function: str
    company_scope: str
    companies: tuple[str, ...]
    pairs: tuple[tuple[str, str], ...]


def expand_position_scope(
    *,
    position_name: str,
    company_scope: str,
    role_function_map: Mapping[str, str],
    company_function_metric_map: Mapping[str, Mapping[str, Sequence[str]]],
    available_companies: Sequence[str] | None = None,
) -> PositionPermissionExpansion:
    """把一个职位+范围选择展开成公司×指标对。

    ``company_scope='*'`` 是表单的「全部」提交值；展示数量由实际可用公司键计算，
    不在本函数中硬编码。任何职位、范围或公司+职能缺失都会响亮失败，避免把配置
    缺口当成空权限或部分成功。
    """

    if not isinstance(position_name, str) or not position_name.strip():
        raise ValueError("职位不能为空")
    if not isinstance(company_scope, str) or not company_scope.strip():
        raise ValueError("公司范围不能为空")

    role = position_name.strip()
    scope = company_scope.strip()
    function = role_function_map.get(role)
    if not isinstance(function, str) or not function.strip():
        raise ValueError("职位未配置映射")

    if scope == ALL_COMPANIES_SCOPE or scope.casefold() in {"all", "全部"}:
        if available_companies is None:
            companies = tuple(sorted(key for key in company_function_metric_map if key != ALL_COMPANIES_SCOPE))
        else:
            companies = tuple(dict.fromkeys(str(value) for value in available_companies if str(value).strip()))
    else:
        if available_companies is not None:
            allowed_companies = {
                value.strip()
                for value in available_companies
                if isinstance(value, str) and value.strip()
            }
            if scope not in allowed_companies:
                raise ValueError("公司范围不是当前可用公司")
        companies = (scope,)
    if not companies:
        raise ValueError("公司范围没有可用公司")

    pairs: list[tuple[str, str]] = []
    for company in companies:
        mapping = company_function_metric_map.get(company)
        metrics = mapping.get(function) if mapping is not None else None
        if not metrics:
            raise ValueError(f"公司 {company} 未覆盖职位职能映射")
        for metric in metrics:
            if not isinstance(metric, str) or not metric.strip():
                raise ValueError("职位职能映射包含空指标")
            pairs.append((company, metric))

    return PositionPermissionExpansion(
        position_name=role,
        function=function.strip(),
        company_scope=ALL_COMPANIES_SCOPE if scope.casefold() in {"all", "全部"} else scope,
        companies=companies,
        pairs=tuple(dict.fromkeys(pairs)),
    )


__all__ = ["ALL_COMPANIES_SCOPE", "PositionPermissionExpansion", "expand_position_scope"]
