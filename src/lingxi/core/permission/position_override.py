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

#: 预开通预授权（Issue #541，rc25 S-8b）合成 ``pending_action`` 的 ``reason``。
#: 与存量差集导入的 ``legacy_import_2_0`` **分开**：审计要一眼分得清「首聊时按旧表
#: 导入」与「首聊前按名单预授权」，这是两次来源不同、责任人不同的授权。
PREPROVISION_PENDING_ACTION_REASON = "preprovision_2_0"

#: 预授权落到 ``local_permission_override.reason`` 的展示原因（管理卡会显示它）。
PREPROVISION_OVERRIDE_REASON = "预开通预授权"


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


@dataclass(frozen=True)
class PositionGrantPlan:
    """一笔「职位＋公司范围」预授权的落库计划（Issue #541，rc25 S-8b）。

    :func:`expand_position_scope` 已经把职位与公司范围确定性地展开成公司×指标对；
    本类型只是把那份展开**冻结**成"要写哪些行、按什么原因写"，随后原样穿过开通链
    交给落库口（``adapters/postgres_local_permission.import_position_grant``）。
    冻结的理由与管理卡确认事务一致：展开结果在准备时算一次，落库时不重新解释配置，
    否则名单核对过的内容与真正写进去的内容之间会多出一个会漂移的解释步骤。

    ``pending_action_reason`` 随计划走而不是写死在落库口里：这条 reason 是审计上
    "这批授权是哪条路径产生的"的唯一答案，让它跟着计划一起被构造、被断言，比藏在
    适配器内部更难被无声改掉。
    """

    position_name: str
    company_scope: str
    pairs: tuple[tuple[str, str], ...]
    pending_action_reason: str = PREPROVISION_PENDING_ACTION_REASON
    override_reason: str = PREPROVISION_OVERRIDE_REASON


def build_preprovision_grant_plan(expansion: PositionPermissionExpansion) -> PositionGrantPlan:
    """把一次职位范围展开冻结成预开通预授权计划。

    展开为空（映射覆盖不全时 :func:`expand_position_scope` 已经响亮失败，这里只是
    结构性兜底）一律拒绝：一笔"零行"的预授权写进去会得到一条谁也解释不了的空组。
    """

    if not expansion.pairs:
        raise ValueError("职位范围展开为空，不构造预授权计划")
    return PositionGrantPlan(
        position_name=expansion.position_name,
        company_scope=expansion.company_scope,
        pairs=tuple(expansion.pairs),
        pending_action_reason=PREPROVISION_PENDING_ACTION_REASON,
        override_reason=PREPROVISION_OVERRIDE_REASON,
    )


__all__ = [
    "ALL_COMPANIES_SCOPE",
    "PREPROVISION_OVERRIDE_REASON",
    "PREPROVISION_PENDING_ACTION_REASON",
    "PositionGrantPlan",
    "PositionPermissionExpansion",
    "build_preprovision_grant_plan",
    "expand_position_scope",
]
