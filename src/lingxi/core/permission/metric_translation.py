"""「公司 + 职能 → 指标名列表」翻译层。

``publish_row.serialize_permissions`` 拿到的值列表是职能标签，问数 MCP 认的是
指标名，中间缺的正是这一层。翻译联合公司：同一个职能标签在不同公司下可能对应
不同的指标名列表，因此本模块必须是独立一层，不能直接塞进 ``PermissionAggregate``。
映射内容只有产品负责人能给，随包配置文件现在是空的，这是交付时的正常状态。

fail-closed：:func:`translate_company_functions` 对全部持有的组合逐一核对，只要
有一个查不到就整体失败、不产出任何指标名，不猜测、不回落、不静默丢弃"有映射的
那一部分"。映射为空时任何组合都查不到，因此产品负责人填入真实映射之前，翻译层
不会产出任何可用于真实发布的内容；调用方据此把翻译失败当成"这个人本轮不发布"，
不当成撤权信号。不复用 ``serialize_permissions``（两者形状不同），本模块用姊妹
函数 :func:`~lingxi.core.permission.publish_row.serialize_translated_permissions` 对接。
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

#: 「全非」通配在翻译映射里的公司键：与 ``publish_row.ALL_COMPANIES_KEY`` 同一个
#: 字面量。不从那边 import 是为了不让本模块反向依赖 ``publish_row``（依赖方向是
#: 翻译层 → 序列化层，而不是反过来）；两处各留一份同一个字面量，一致性由各自
#: 的测试钉住同一个值。
ALL_COMPANIES_KEY = "*"


def metric_translation_available(
    mapping: Mapping[str, Mapping[str, Sequence[str]]] | None,
) -> bool:
    """翻译层整体可用性判据：映射非空才可用。

    ``None``（尚未加载或加载失败）与空映射（产品负责人尚未填入内容时的合法初始
    状态）在这一层是同一个结论——都意味着这一轮一个组合都翻译不出来。本函数是
    唯一允许存在的判据实现，被每日权限重算与首次开通编排两个独立写入点共用：
    各自维护一份看起来等价的检查迟早会漂移，漂移方向是错误发布，因此调用方一律
    传入同一个已加载对象、调用同一个函数。
    """
    return bool(mapping)


class UncoveredPermissionCombinationError(ValueError):
    """存在未被翻译映射覆盖的「公司 + 职能」组合：fail-closed，不猜、不丢弃。

    :attr:`missing` 是缺失的 ``(公司ID或 "*", 职能标签)`` 元组集合，按公司再按职能
    排序，可以直接进审计——公司编号与职能标签都不是人员资料。

    :attr:`mapping_is_empty` 让调用方能在审计里分辨两种不同的运维状态：``True``
    是整份映射一个条目都没有（产品负责人还没开始填内容），``False`` 是映射里有
    内容但没覆盖这一次要用的组合（内容正在逐步填入）。两者运维含义不同，调用方
    据此记两种不同的跳过原因。
    """

    def __init__(self, missing: Sequence[tuple[str, str]], *, mapping_is_empty: bool) -> None:
        """用排序去重后的缺失组合与映射是否整体为空构造。"""
        ordered = tuple(sorted(dict.fromkeys(missing)))
        if not ordered:
            raise ValueError("UncoveredPermissionCombinationError 必须携带至少一个缺失组合")
        self.missing = ordered
        self.mapping_is_empty = mapping_is_empty
        super().__init__(f"未覆盖的公司+职能组合（fail-closed，不猜测、不静默丢弃）：{ordered}")


#: 指标名里一律不允许出现的 Unicode 类别：控制字符（``Cc``）、格式字符
#: （``Cf``——零宽空格、双向覆盖）、代理与私用区（``Cs``/``Co``）、行与段
#: 分隔符（``Zl``/``Zp``）。与 ``core/permission/legacy_diff.py`` 的
#: ``_FORBIDDEN_CHARACTER_CATEGORIES`` 同一份清单。
_FORBIDDEN_METRIC_CHARACTER_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Zl", "Zp"})


def _malformed_metric_name(value: str) -> str | None:
    """映射文件里一个指标名取值的卫生判据：不合格返回原因短语，合格返回 ``None``。

    这个文件是权限翻译链的目录源头，产出的指标名会被 ``publish_row`` 原样序列化
    进权限发布表，因此不能只判"非空字符串"。``"*"`` 最危险：读侧把公司下的
    ``"*"`` 当作该公司全部指标（含以后新增的），且抑制对它无效。口径与
    ``core/permission/legacy_diff.py::_malformed`` 逐条对齐：拒空白、拒不可见
    字符、拒把 ``"*"`` 混进名字，刻意不管普通空格。公司键不走这道校验：``"*"``
    在键位是合法的 :data:`ALL_COMPANIES_KEY` 通配，只有落到值上才是越权。
    """
    if not value.strip():
        return "不得为空白"
    for character in value:
        if unicodedata.category(character) in _FORBIDDEN_METRIC_CHARACTER_CATEGORIES:
            return "不得含换行、制表符或其他不可见字符"
        if character.isspace() and character != " ":
            return "不得含普通空格以外的空白字符"
    if ALL_COMPANIES_KEY in value:
        return f"不得为通配符 {ALL_COMPANIES_KEY}，也不得把它混进名字里"
    return None


def _build_functions_for_company(
    company: str, raw_functions: Mapping[str, Any]
) -> dict[str, tuple[str, ...]]:
    """校验并规范化一个公司下的「职能标签 → 指标名列表」子表。"""
    functions: dict[str, tuple[str, ...]] = {}
    for raw_function, raw_metrics in raw_functions.items():
        function = raw_function.strip() if isinstance(raw_function, str) else ""
        if not function:
            raise ValueError(f"公司 {company} 下存在空的职能标签")
        if function in functions:
            raise ValueError(f"公司 {company} 下存在重复的职能标签：{function}")
        if not isinstance(raw_metrics, (list, tuple)) or not raw_metrics:
            raise ValueError(f"公司 {company} 职能 {function} 的指标名列表必须是非空列表")
        metrics: list[str] = []
        for item in raw_metrics:
            if not isinstance(item, str) or not item:
                raise ValueError(f"公司 {company} 职能 {function} 的指标名列表元素必须是非空字符串")
            problem = _malformed_metric_name(item)
            if problem is not None:
                raise ValueError(f"公司 {company} 职能 {function} 的指标名{problem}")
            metrics.append(item)
        functions[function] = tuple(metrics)
    return functions


def build_company_function_metric_map(
    document: Mapping[str, Any],
) -> dict[str, dict[str, tuple[str, ...]]]:
    """校验解析后的映射文档，返回 ``{公司ID或"*": {职能标签: (指标名, …)}}``。

    文档形态：``{"companies": {"<公司ID或"*">": {"<职能标签>": ["<指标名>", …]}}}``。
    任何一条不满足都失败关闭（抛 ``ValueError``，不做宽容修补）：公司键必须非空
    且不去重不归一（银河原始取值或 ``"*"`` 通配，精确匹配）；职能标签必须是
    :mod:`lingxi.core.permission.role_function` 产出的原样标签；指标名列表非空、
    元素非空字符串，且过 :func:`_malformed_metric_name` 卫生判据。不校验指标名/
    职能标签是否真实存在（那是受控回源工作，不是配置解析），也不核对公司键在不
    在任何目录里——这个文件本身就是目录。
    """
    companies = document.get("companies")
    if not isinstance(companies, Mapping):
        raise ValueError("公司+职能→指标名映射缺少 [companies] 表")

    result: dict[str, dict[str, tuple[str, ...]]] = {}
    for raw_company, raw_functions in companies.items():
        company = raw_company.strip() if isinstance(raw_company, str) else ""
        if not company:
            raise ValueError("公司+职能→指标名映射存在空的公司键")
        if company in result:
            raise ValueError(f"公司+职能→指标名映射存在重复的公司键：{company}")
        if not isinstance(raw_functions, Mapping):
            raise ValueError(f"公司 {company} 下的职能表必须是映射")
        result[company] = _build_functions_for_company(company, raw_functions)
    return result


def translate_company_functions(
    *,
    companies: Sequence[str],
    functions: Sequence[str],
    all_companies: bool,
    mapping: Mapping[str, Mapping[str, Sequence[str]]],
) -> dict[str, tuple[str, ...]]:
    """把「公司范围 + 职能标签」翻译成发布表要的「公司 → 指标名列表」。

    输入是 ``aggregate_permission`` 的产出，输出可直接交给
    ``serialize_translated_permissions``。两遍扫描，先查后算：第一遍判断每个
    组合是否都能在 mapping 里查到，任何一个查不到就立即抛
    :class:`UncoveredPermissionCombinationError`，不产出任何部分结果；第二遍
    才真正取值、去重、排序。通配（``all_companies=True``）时只查 ``"*"`` 键，
    不展开成具体公司——mapping 里 ``"*"`` 键下的内容同样要显式填写。职能列表
    内的重复与次序不影响结果。
    """
    keys = (ALL_COMPANIES_KEY,) if all_companies else tuple(dict.fromkeys(companies))
    if not keys:
        raise ValueError("翻译需要至少一个公司键")
    function_names = tuple(dict.fromkeys(functions))
    if not function_names:
        raise ValueError("翻译需要至少一个职能标签")

    missing: list[tuple[str, str]] = []
    for company in keys:
        function_map = mapping.get(company)
        for function in function_names:
            if function_map is None or function not in function_map:
                missing.append((company, function))
    if missing:
        raise UncoveredPermissionCombinationError(missing, mapping_is_empty=not mapping)

    result: dict[str, tuple[str, ...]] = {}
    for company in keys:
        function_map = mapping[company]
        metrics: list[str] = []
        for function in function_names:
            metrics.extend(function_map[function])
        result[company] = tuple(sorted(dict.fromkeys(metrics)))
    return result


#: 向后兼容别名，供既有导入方使用。
UncoveredPermissionCombination = UncoveredPermissionCombinationError

__all__ = [
    "ALL_COMPANIES_KEY",
    "UncoveredPermissionCombination",
    "UncoveredPermissionCombinationError",
    "build_company_function_metric_map",
    "metric_translation_available",
    "translate_company_functions",
]
