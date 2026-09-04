"""「公司 + 职能 → 指标名列表」翻译层（Issue #227，载体先行、内容后填）。

## 这一层解决什么问题

:mod:`lingxi.core.permission.publish_row` 的 :func:`~lingxi.core.permission.
publish_row.serialize_permissions` 把有效权限写成发布表 ``permissions`` 列的文本，
但它现在拿到的值列表是**职能标签**（``运营``、``财务`` 这类，来自
:mod:`lingxi.core.permission.role_function` 的配置），而问数 MCP 认的是**指标名**
（``日活``、``收入`` 这类）。中间缺的正是这一层：把「一个用户在某个公司下持有的职能」
翻译成「这个公司这个职能对应哪些指标名」。

翻译**联合公司**（不是只按职能）：同一个职能标签在不同公司下可能对应不同的指标名
列表（该公司不产某个指标、或指标口径不同），这也是产品负责人把 Issue 命名为「公司 +
职能」而不是「职能」的原因。:func:`~lingxi.core.permission.publish_row.
aggregate_permission` 产出的 ``functions`` 对同一个用户的所有公司是**同一份列表**
（银河的授权模型里职能是用户级的），但翻译之后每个公司拿到的指标名列表可以不同——这是
本模块与聚合层之间刻意的形状差异，也是它必须是**独立的一层**、不能直接塞进
``PermissionAggregate`` 的原因。

## 内容从哪里来（本 Story 不回答）

映射内容（哪个公司的哪个职能对应哪些指标名）只有产品负责人能给，见 Issue
[#227](https://github.com/Moshuiwang/lingxi/issues/227)。本模块只接受**已经解析好的
文档**（:func:`build_company_function_metric_map` 的输入），文件 I/O 在
:mod:`lingxi.adapters.company_function_metric_map_file`，随包发布的配置文件是
``lingxi/config/company_function_metric_map.toml``——它现在**是空的**（只有
``[companies]`` 表头，没有任何条目），这是本 Story 交付时的正常状态，不是缺陷。

## fail-closed：未覆盖的组合不得被猜测，也不得被静默丢弃

:func:`translate_company_functions` 对一个用户全部持有的「公司 + 职能」组合逐一核对：
**只要有一个组合在 mapping 里找不到，整个翻译就失败**，不产出任何指标名——不猜测该
组合应该对应什么、不回落成职能标签本身、也不悄悄只发布"有映射的那一部分"（那是静默
丢弃，方向与「未覆盖就不给」一样安全，但会让运维以为这个人的权限只有一部分，而实际上
是翻译层没跟上）。映射为**空**时，任何用户的任何组合都查不到，因此**每一个原本有效的
授权用户都会在这一步 fail-closed**——这正是「映射为空时维持现有硬闸」要的效果：在
产品负责人填入真实映射之前，没有任何人能经翻译层产出可用于真实发布的 ``permissions``
内容。

调用方（每日权限重算职责）据此把翻译失败当成"这个人本轮不发布"处理，不触碰他已经
发布过的行——翻译覆盖不全是我们这一侧的数据缺口，不是"银河说他没有权限"，因此不应该
被当成撤权信号（撤权语义见 ``publish_row`` 模块文档「撤权行」一节，那是另一条独立的
判定路径，不受本模块影响）。

## 与 ``serialize_permissions`` 的关系

本模块不改、也不复用 ``serialize_permissions`` 的实现——那个函数的形状是"一份职能
列表适用于用户持有的全部公司"，翻译之后的形状是"每个公司一份可能不同的指标名列表"，
两者结构不同。:mod:`lingxi.core.permission.publish_row` 提供了同一套序列化纪律
（``sort_keys``、无空格分隔符、``ensure_ascii=False``、值逐字符透传）的姊妹函数
:func:`~lingxi.core.permission.publish_row.serialize_translated_permissions`，
供本模块的输出对接；``serialize_permissions`` 本身与它的既有用例**一个字节都不改**。
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

#: 「全非」通配在翻译映射里的公司键：与 ``publish_row.ALL_COMPANIES_KEY`` 同一个字面量。
#: 不从那边 import 是为了不让本模块反向依赖 ``publish_row``（依赖方向是
#: 翻译层 → 序列化层，而不是反过来）；两处各留一份同一个字面量的取舍，与
#: ``core/permission/publish_row.py`` 的 ``is_cipher_shaped`` / ``looks_like_cipher``
#: 一致两份判据同一条理由——一致性由 :mod:`tests.test_permission_metric_translation`
#: 与 :mod:`tests.test_permission_publish_row` 各自钉住同一个值。
ALL_COMPANIES_KEY = "*"


def metric_translation_available(
    mapping: Mapping[str, Mapping[str, Sequence[str]]] | None,
) -> bool:
    """翻译层**整体**可用性判据：映射非空才可用（外部独立审查 2026-08-18 坐实的 P1）。

    ``None``（尚未加载、或加载本身失败）与空映射（``{}``，产品负责人尚未填入内容时的
    合法初始状态，见模块文档）在这一层是同一个结论——两者都意味着"这一轮一个组合都
    翻译不出来"。

    本函数是**唯一**允许存在的判据实现，被两个独立的写入点共用：

    - :mod:`lingxi.apps.scheduler.permission_refresh`（每日权限重算，授权/撤权的
      发布意图写入点）在 ``run_once`` 开头用它判定"这一轮要不要跑"；
    - :mod:`lingxi.apps.scheduler.assembly`（首次开通编排，``record_decision`` 的
      第三个调用点）用它判定 ``publish_allowed``。

    两处各自维护一份看起来等价的检查，迟早会漂移——而漂移的方向是错误发布（例如
    重算侧还在等内容、开通侧已经在往外发）。因此调用方一律传入**同一个已加载对象**、
    调用**这同一个函数**，装配层（:func:`~lingxi.apps.scheduler.assembly.build_loop`）
    只加载一次映射文件。
    """

    return bool(mapping)


class UncoveredPermissionCombination(ValueError):
    """存在未被翻译映射覆盖的「公司 + 职能」组合：fail-closed，不猜、不丢弃。

    :attr:`missing` 是缺失的 ``(公司ID或 "*", 职能标签)`` 元组集合，按公司再按职能
    排序，可以直接进审计——公司编号与职能标签都不是人员资料（纪律同
    ``publish_row.PermissionAggregate.audit_facts``）。

    :attr:`mapping_is_empty` 让调用方能在审计里**分辨两种不同的运维状态**，
    不止是"翻译失败"这一件事：

    - ``True``：传入的整份映射一个条目都没有——产品负责人还没有开始填内容
      （随包发布的配置文件当前就是这个状态）；
    - ``False``：映射里**有**内容，但恰好没有覆盖这一次要用的组合——通常意味着
      内容正在被逐步填入，只是还没填到这一条。

    两者的运维含义不同（前者是"整体还没开始"，后者是"已经在填，还差几条"），
    因此调用方（每日权限重算职责）据此记两种不同的跳过原因，见
    :mod:`lingxi.apps.scheduler.permission_refresh` 的
    ``SKIP_METRIC_TRANSLATION_UNAVAILABLE`` / ``SKIP_METRIC_TRANSLATION_UNCOVERED``。
    """

    def __init__(self, missing: Sequence[tuple[str, str]], *, mapping_is_empty: bool) -> None:
        ordered = tuple(sorted(dict.fromkeys(missing)))
        if not ordered:
            raise ValueError("UncoveredPermissionCombination 必须携带至少一个缺失组合")
        self.missing = ordered
        self.mapping_is_empty = mapping_is_empty
        super().__init__(f"未覆盖的公司+职能组合（fail-closed，不猜测、不静默丢弃）：{ordered}")


#: 指标名里一律不允许出现的 Unicode 类别：控制字符（``Cc``——换行、回车、制表都在
#: 里面）、格式字符（``Cf``——零宽空格、双向覆盖）、代理与私用区（``Cs``/``Co``）、
#: 行与段分隔符（``Zl``/``Zp``）。与 ``core/permission/legacy_diff.py`` 的
#: ``_FORBIDDEN_CHARACTER_CATEGORIES`` 同一份清单（Trace #544 P-5 / S-2d 同批）。
_FORBIDDEN_METRIC_CHARACTER_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Zl", "Zp"})


def _malformed_metric_name(value: str) -> str | None:
    """映射文件里一个**指标名取值**的卫生判据：不合格返回原因短语，合格返回 ``None``。

    **为什么这道校验必须在这里**（Trace #544 P-5）：这个文件是权限翻译链的目录源头，
    它产出的指标名会被 ``publish_row`` 原样序列化进权限发布表。此前这一层只判"非空
    字符串"，于是 ``"*"``、``" "``、``"日活\n"``、含零宽字符的名字都能直通落到正式
    表上。其中 ``"*"`` 最危险：读侧把公司下的 ``"*"`` 当作"该公司全部指标（含以后新增
    的）"，一个笔误因此会把范围放到最大，而且**抑制对它无效**（抑制按「公司×指标」
    精确匹配，减不掉一个 ``"*"``）——与 rc21 修复包 B 在导入侧堵掉的是同一个洞，
    只是这一侧的入口是配置文件而不是导出 CSV。

    口径与 ``core/permission/legacy_diff.py::_malformed`` 逐条对齐（**值严格、公司键
    不查目录**）：拒空白、拒不可见字符与普通空格以外的空白、拒把 ``"*"`` 混进名字；
    刻意**不**管名字首尾的普通空格。两处目前各有一份实现——``legacy_diff`` 是本批
    另一条 Story 的改动面，本批不跨面合并，去重登记在 S-3a 收口报告。

    公司键不走这道校验：``"*"`` 在键位是合法的 :data:`ALL_COMPANIES_KEY` 通配，
    只有落到**值**上才是越权。
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


def build_company_function_metric_map(
    document: Mapping[str, Any],
) -> dict[str, dict[str, tuple[str, ...]]]:
    """校验解析后的映射文档，返回 ``{公司ID或"*": {职能标签: (指标名, …)}}``。

    文档形态：``{"companies": {"<公司ID或\\"*\\">": {"<职能标签>": ["<指标名>", …]}}}``，
    与 :mod:`lingxi.core.permission.role_function` 的 ``build_role_function_map``
    同一套校验姿态（配置未覆盖的组合返回不到值，不是猜出一个值）。

    校验规则，任何一条不满足都失败关闭（抛 ``ValueError``，不做宽容修补）：

    - 顶层必须有 ``companies`` 表，值必须是映射（可以是空映射——**这是合法的初始
      状态**，代表内容尚未填入，见模块文档）；
    - 公司键必须是非空字符串（含 ``"*"``），且**不去重、不归一**——它要么是
      ``sys_country.boss_company_id`` 的原始取值，要么是 ``"*"`` 通配，两者都是
      精确匹配的键，不做大小写或空白处理；
    - 每个公司下的职能表必须是映射，职能标签必须是非空字符串，且是
      :mod:`lingxi.core.permission.role_function` 产出的**原样**标签
      （精确匹配，不猜测同义词）；
    - 每个职能对应的指标名列表：必须是非空列表，元素必须是非空字符串，且要过
      :func:`_malformed_metric_name` 的卫生判据（Trace #544 P-5）。
      **允许包含重复元素**（:func:`translate_company_functions` 会去重排序），但不
      允许空字符串或非字符串——那是配置错误，不是数据。

    **不做的事**：不校验指标名是否真的存在于问数 MCP（那需要真实回源，属受控窗口
    的核对工作，不是配置解析）；不校验职能标签是否出现在
    ``galaxy_role_function_map.toml`` 里（两个配置文件由不同人分别维护，互相校验
    会让改一个文件牵连另一个文件的解析）；**不核对公司键在不在任何目录里**——
    公司键要么是银河原始取值、要么是 ``"*"``，这个文件本身就是目录，没有第二份
    可以拿来比对（与 ``core/permission/legacy_diff.py``「值严格、公司键不查目录」
    同一条口径）。
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
                    raise ValueError(
                        f"公司 {company} 职能 {function} 的指标名列表元素必须是非空字符串"
                    )
                problem = _malformed_metric_name(item)
                if problem is not None:
                    raise ValueError(f"公司 {company} 职能 {function} 的指标名{problem}")
                metrics.append(item)
            functions[function] = tuple(metrics)
        result[company] = functions
    return result


def translate_company_functions(
    *,
    companies: Sequence[str],
    functions: Sequence[str],
    all_companies: bool,
    mapping: Mapping[str, Mapping[str, Sequence[str]]],
) -> dict[str, tuple[str, ...]]:
    """把「公司范围 + 职能标签」翻译成发布表要的「公司 → 指标名列表」。

    输入是 :func:`~lingxi.core.permission.publish_row.aggregate_permission` 的产出
    （``companies`` / ``functions`` / ``all_companies``），输出可以直接交给
    :func:`~lingxi.core.permission.publish_row.serialize_translated_permissions`。

    **两遍扫描，先查后算**：第一遍只判断"每个公司键下的每个职能是否都能在 mapping
    里查到"，任何一个查不到就立即抛 :class:`UncoveredPermissionCombination`、
    **不产出任何部分结果**——这是"失败关闭"与"部分发布"之间的分界线，先查完再决定
    要不要算，而不是边算边攒、算到哪缺哪就跳过哪。第二遍才真正取值、去重、排序。

    通配（``all_companies=True``）时只查 ``mapping`` 的 ``"*"`` 键，与
    ``publish_row.serialize_permissions`` 对「全非」通配的处理同一条口径：**不展开**
    成具体公司——mapping 里 ``"*"`` 键下的职能→指标名同样要产品负责人显式填写，
    翻译层不会替"持有全非通配的人到底能看哪些指标"这件事做主。

    职能列表内的重复与次序**不影响结果**：同一个职能被查两次、或次序不同，翻译出的
    指标名集合逐字节相同（去重靠 ``dict.fromkeys``、排序靠显式 ``sorted``，与
    ``aggregate_permission`` 对职能列表的处理同一套纪律，保证恒等序列化）。
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
        raise UncoveredPermissionCombination(missing, mapping_is_empty=not mapping)

    result: dict[str, tuple[str, ...]] = {}
    for company in keys:
        function_map = mapping[company]
        metrics: list[str] = []
        for function in function_names:
            metrics.extend(function_map[function])
        result[company] = tuple(sorted(dict.fromkeys(metrics)))
    return result


__all__ = [
    "ALL_COMPANIES_KEY",
    "UncoveredPermissionCombination",
    "build_company_function_metric_map",
    "metric_translation_available",
    "translate_company_functions",
]
