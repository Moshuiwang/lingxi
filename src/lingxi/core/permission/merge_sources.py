"""四源聚合的集中合并（纯函数）：银河 ∪ 本地授权 ∪ 存量沿用 − 本地抑制。

[Issue #319](https://github.com/Moshuiwang/lingxi/issues/319) 的 S-P-3（Trace #328
E-P 批次）。上游已经分别把「银河这一侧解释出什么」（:mod:`lingxi.core.permission.
publish_row`、:mod:`lingxi.core.permission.metric_translation`）与「本地覆盖这一侧
解决出什么」（:mod:`lingxi.core.permission.local_override` 的
:func:`~lingxi.core.permission.local_override.resolve_local_overrides`，``suppress``
赢已经内化——``grants`` 字段本身就是本地净授权）分别答完；本模块只回答最后一步：
**这个用户最终真实拥有的权限，是把三个已经算好的来源按什么规则拼成一份**。

真实权限 ``= (银河 ∪ 本地授权 ∪ 存量沿用) − 本地抑制``，在 ``{公司ID 或全公司通配
"*"：值字符串列表}`` 这个粒度上做集合运算——即 :func:`merge_permission_sources` 的
输出形状。

## 挂点与输入类型：与设计初稿的差异（本卡定稿，编排者授权由实施方按代码实况修正）

设计初稿把 ``galaxy`` 参数的类型定为 :class:`~lingxi.core.permission.publish_row.
PermissionAggregate`，隐含假设是"两个调用点都消费翻译层产出的 ``{公司:[指标名]}``
映射"。核对当前实现（读 ``core/identity/onboarding_runner.py`` 与
``apps/scheduler/permission_refresh.py`` 的现状代码）发现这个假设只对**其中一个**
调用点成立：

1. **只有 ``permission_refresh.py`` 的 ``_refresh_user`` 真正调用了翻译层**
   （:func:`~lingxi.core.permission.metric_translation.translate_company_functions`），
   产出 ``{公司: (指标名, …)}``。``core/identity/onboarding_runner.py`` 的
   ``AutoOnboardingRunner._publish`` 目前用的是
   :func:`~lingxi.core.permission.publish_row.build_publish_row`（**未翻译**，值列表
   是 :attr:`PermissionAggregate.functions` 的职能标签，从未调用
   ``translate_company_functions``）——这不是本卡引入的新事实，是当前生产代码已经
   如此（``V-权限-13`` 矩阵条目里"``OnboardingRunner`` 生产唯一实现是失败关闭桩"
   一句写于 2026-08-18，晚于它的是 2026-08-19 把真实 ``PostgresPermissionPublishStore``
   接上开通链的那次合并——矩阵那句注记因此已经过期，不能作为"开通侧也翻译过"的依据）。
   因此"银河翻译产出 ``{公司:[指标名]}``"并**不是**两个调用点共享的唯一位置。

   本模块的取舍：把 ``galaxy`` 参数的类型从 ``PermissionAggregate`` 放宽成通用的
   ``Mapping[str, Sequence[str]]``（公司ID 或通配键 ``"*"`` → 值字符串列表）——本函数
   对值字符串的**语义**完全不关心，只做集合代数，因此两种输入天然都能正确合并：
   ``permission_refresh`` 一侧传入翻译后的指标名映射，``onboarding`` 一侧传入从
   ``aggregate.companies``/``functions``/``all_companies`` 现算出的职能标签映射（在
   ``_publish`` 里现算，形状与 :func:`~lingxi.core.permission.publish_row.
   serialize_permissions` 内部构造 ``document`` 的逻辑一致，只是这里不经过
   ``json.dumps``）。**修复开通侧"从未翻译"这件事本身不在本卡范围内**——那是一次
   远超"四源聚合手术"的行为变更（会改变开通首次发布的实际内容），S-P-3 只负责让
   本地覆盖在开通侧"无论银河那一侧给的是职能标签还是指标名"都能正确生效：本地覆盖
   条目本身永远是具体指标名，把它们并入职能标签列表虽然让该列表出现"标签与指标名并存"
   的过渡态，但**local 那一份被并入/减去的每一个字符串仍然是精确的指标名**，问数 MCP
   按公司键取值列表做逐字匹配，因此这一个用户在这一个公司下被本地覆盖显式点名的那个
   指标名，无论周围混着什么职能标签，都会被正确放行/拒绝——这正是本地覆盖机制要保证
   的最小承诺，不依赖开通侧翻译问题何时被修。

2. **本地覆盖条目的 ``user_id`` 是内部 ``app_user.id``**（迁移 ``0072``，见
   :mod:`lingxi.adapters.postgres_local_permission` 的建表与
   ``tests/test_local_permission_postgres.py`` 的用例），不是银河账号标识。
   ``onboarding_runner.py`` 的聚合（``_match``，读银河快照）发生在建档
   （``_provision``）**之前**，这时候还没有 ``app_user.id``；真正拿到它、也真正要
   结算发布行的位置是 ``_publish``（建档、复核、令牌签发都已完成之后）。因此本卡
   在开通侧的实际接线点是 ``_publish``，不是设计初稿写的"聚合后"那一行——聚合发生
   的地方还没有本地覆盖查询所需要的键。

## 通配角 v1 语义（编排者裁定，逐字执行）

目标用户聚合为 ``all_companies=True``（当前唯一形态：银河「后台管理员」角色，见
``publish_row.ADMIN_FULL_ACCESS_FUNCTION``，`V-权限-14`）时，即 ``galaxy`` 里出现
:data:`ALL_COMPANIES_KEY` 这个键：

- **本地 ``suppress`` 不生效**——通配已经覆盖全公司全指标，一条按具体公司登记的
  抑制既拦不住通配（抑制的是具体公司键，通配走的是 ``"*"`` 键，`lookup_metrics`
  按公司键精确匹配，一条 ``"1011"`` 的抑制条目对 ``"*"`` 键完全无感）。
- **本地 ``grant`` 同样跳过**——通配已经含全部指标，追加具体指标名是纯冗余。

两者都**跳过**而不是"尝试合并后发现没有变化"：如果把具体公司的授权/抑制条目并入
通配映射，会在结果里额外长出一个具体公司键，与原本只有 ``"*"`` 一个键的形状不同——
读侧 :func:`~lingxi.core.permission.publish_row.lookup_metrics` 对**存在的**具体公司
键不再回退通配，那个具体公司会失去通配本该覆盖的其余指标，方向是**少给权限**，且
只发生在"恰好也被本地覆盖点过名"的那个公司，是一个极难被发现的窄范围回归。因此
通配下本地源**整体不参与合并**，产出的 ``permissions`` 与银河原始映射逐字节相同，
只把"为什么被跳过"作为事实告诉调用方（:attr:`MergedPermissionSources.skipped_reasons`），
由调用方决定用什么审计事件名记下来（两个调用点各自的动作名不同，命名姿态相同，
见两处调用点模块文档）。

``legacy``（存量沿用）在通配下同一条理由，同样不参与合并——即便本卡不接线任何
真实的 ``legacy`` 数据源（见下）。

## ``legacy`` 参数：本卡只定签名，不接数据源

`Issue #319` 的存量迁移（把旧系统已经生效的权限「沿用」进来）是 S-P-2 批二的范围，
不在本卡。这里提前定死签名（``Mapping[str, Sequence[str]] | None``，默认 ``None``），
避免 S-P-2 落地时需要改这个纯函数的对外签名、牵连两个已经接好线的调用点。``None``
时对结果**恒等**（不参与任何键、不影响任何值）——见 ``tests/test_permission_merge_sources.py``
的 ``LegacyIdentityTests``。非通配、非 ``None`` 时的合并规则与本地授权对称：
参与并集，**不参与抑制**（``legacy`` 没有"抑制"概念——它只是"这个键本来就该在"，
被本地抑制命中同样会被减掉，因为减法作用于并集之后的结果，与这个键是从银河、本地
授权还是存量沿用来的无关）。

## 空结果：合并后某个公司的值集合被抑制到空时**丢弃这个键**，不写空列表

与 :func:`~lingxi.core.permission.publish_row.serialize_translated_permissions` 现有
纪律的边界配合：那个函数的既有测试
（``TranslatedSerializationTest.test_an_empty_metric_list_for_any_company_is_rejected``）
钉着"写侧不产出空列表"，本卡刻意不去碰这条纪律、不去改它的测试——因为**不需要**。
非通配分支里，``galaxy``（无论是翻译后的指标名映射还是开通侧现算的职能标签映射）
从不含 ``"*"`` 键（:func:`~lingxi.core.permission.metric_translation.
translate_company_functions` 的产出与本模块调用点在 ``onboarding_runner.py`` 里现算
的映射都是"要么整份是 ``{"*": …}``、要么整份是具体公司键"，两者不会在同一份里混），
因此在非通配分支里，丢弃一个被抑制到空的公司键与保留它写成空列表，对读侧
:func:`~lingxi.core.permission.publish_row.lookup_metrics` 是**完全等价**的结果
（该公司键缺失时回退查 ``"*"``，而这一支里 ``"*"`` 根本不存在，回退查不到，两条路径
都收敛到空元组）。选择丢弃键而不是保留空列表，换来的是不用去放宽
``serialize_translated_permissions`` 已经写了详尽理由、且被测试钉住的既有约束——
一处新增行为不需要的改动越少，越不容易在无关路径上引入回归。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from lingxi.core.permission.local_override import ResolvedLocalOverrides, to_company_metric_map

#: 「全非」通配在合并输入/输出里的公司键。与 ``publish_row.ALL_COMPANIES_KEY``、
#: ``metric_translation.ALL_COMPANIES_KEY`` 是同一个字面量的第三份独立拷贝——两处
#: 既有先例（见 ``metric_translation.py`` 模块文档「为什么不从 publish_row import」）
#: 已经把"每个模块各留一份，不建立反向依赖"定为本仓库对这个字面量的既定姿态，本模块
#: 处在翻译层与序列化层之间，同一条理由继续成立：不让本模块反过来被上下游任何一层
#: 的常量位置约束住。一致性由 ``tests/test_permission_merge_sources.py`` 与另外两个
#: 模块各自的测试分别钉住同一个值。
ALL_COMPANIES_KEY = "*"

#: 通配角 v1：本地授权在通配下被跳过（冗余，通配已经覆盖全部指标）。
REASON_GRANT_REDUNDANT_WILDCARD = "grant_redundant_wildcard"

#: 通配角 v1：本地抑制在通配下被跳过（拦不住通配，抑制的是具体公司键）。
REASON_SUPPRESS_INAPPLICABLE_WILDCARD = "suppress_inapplicable_wildcard"

#: 读本地覆盖条目时数据库异常：调用方据此让该用户本轮跳过本地源，而不是让异常
#: 冒泡带走整轮/整次开通。两个调用点共用同一个原因码字面量，审计动作名各自不同
#: （``permission_refresh.local_override_skipped`` / ``onboarding.local_override_skipped``），
#: 见各自模块文档。
REASON_LOCAL_OVERRIDE_READ_FAILED = "local_override_read_failed"


@dataclass(frozen=True)
class MergedPermissionSources:
    """一次四源合并的结果：最终权限映射 + 为什么某些输入被跳过。

    :attr:`skipped_reasons` 只在**通配角 v1** 生效时非空（本地授权/抑制因为通配而
    被整体跳过，见模块文档）。它是**事实**，不是审计动作——本模块是纯函数、不做
    任何 I/O，真正的 ``audit.record(...)`` 调用留给两个调用点各自完成（与
    :meth:`~lingxi.core.permission.publish_row.PermissionAggregate.audit_facts` 同一
    姿态：纯函数只回答"发生了什么"，调用方决定"记成哪一条审计"）。
    """

    permissions: Mapping[str, tuple[str, ...]]
    skipped_reasons: tuple[str, ...]


def merge_permission_sources(
    *,
    galaxy: Mapping[str, Sequence[str]],
    local: ResolvedLocalOverrides | None,
    legacy: Mapping[str, Sequence[str]] | None = None,
) -> MergedPermissionSources:
    """真实权限 ``= (银河 ∪ 本地授权 ∪ 存量沿用) − 本地抑制``。

    ``galaxy``：银河这一侧已经算好的 ``{公司ID 或 "*"：值字符串列表}``——可以是
    :func:`~lingxi.core.permission.metric_translation.translate_company_functions`
    翻译后的指标名映射，也可以是调用方从 ``PermissionAggregate`` 现算出的职能标签
    映射（开通侧当前的实况，见模块文档「挂点」一节）。本函数不关心值字符串的语义，
    只做集合代数，因此两种输入都能正确合并。

    ``local``：:func:`~lingxi.core.permission.local_override.resolve_local_overrides`
    的结果，``None`` 表示"这一轮本地源不参与"（store 未装配、或读取失败后调用方
    降级传入）——对结果**恒等**：产出与 ``galaxy`` 逐字节相同（键集合与值集合都
    只由 ``galaxy``/``legacy`` 决定）。

    ``legacy``：本卡只定签名、不接数据源（S-P-2 批二接线），默认 ``None``，恒等，
    详见模块文档。

    **通配角 v1**：``galaxy`` 出现 :data:`ALL_COMPANIES_KEY` 键时，``local``（与
    ``legacy``）整体不参与合并，产出与 ``galaxy`` 逐字节相同；:attr:`MergedPermissionSources.
    skipped_reasons` 按"``local`` 是否带着非空授权/抑制"分别登记
    :data:`REASON_GRANT_REDUNDANT_WILDCARD`/:data:`REASON_SUPPRESS_INAPPLICABLE_WILDCARD`。

    **非通配**：结果键集合 = ``galaxy`` 键 ∪ 本地授权命中的公司键 ∪ ``legacy`` 键。
    每个键的值 = ``(galaxy[key] ∪ 本地授权[key] ∪ legacy[key]) − 本地抑制[key]``
    （缺席一律按空集合处理），按字符串排序去重成 ``tuple``。**减到空集合的键会被
    丢弃**，不产出空列表——理由与"为什么不产出空列表不违反读侧语义"见模块文档
    「空结果」一节。
    """

    galaxy_map: dict[str, tuple[str, ...]] = {
        key: tuple(values) for key, values in galaxy.items()
    }

    if ALL_COMPANIES_KEY in galaxy_map:
        skipped: list[str] = []
        if local is not None and local.grants:
            skipped.append(REASON_GRANT_REDUNDANT_WILDCARD)
        if local is not None and local.suppressions:
            skipped.append(REASON_SUPPRESS_INAPPLICABLE_WILDCARD)
        return MergedPermissionSources(permissions=galaxy_map, skipped_reasons=tuple(skipped))

    local_grants = to_company_metric_map(local.grants) if local is not None else {}
    local_suppressions = to_company_metric_map(local.suppressions) if local is not None else {}
    legacy_map: dict[str, tuple[str, ...]] = {
        key: tuple(values) for key, values in (legacy or {}).items()
    }

    keys = set(galaxy_map) | set(local_grants) | set(legacy_map)
    merged: dict[str, tuple[str, ...]] = {}
    for key in keys:
        values = (
            set(galaxy_map.get(key, ()))
            | set(local_grants.get(key, ()))
            | set(legacy_map.get(key, ()))
        )
        values -= set(local_suppressions.get(key, ()))
        if values:
            merged[key] = tuple(sorted(values))
        # values 为空：丢弃这个键，不写空列表（模块文档「空结果」一节）。

    return MergedPermissionSources(permissions=merged, skipped_reasons=())


__all__ = [
    "ALL_COMPANIES_KEY",
    "REASON_GRANT_REDUNDANT_WILDCARD",
    "REASON_LOCAL_OVERRIDE_READ_FAILED",
    "REASON_SUPPRESS_INAPPLICABLE_WILDCARD",
    "MergedPermissionSources",
    "merge_permission_sources",
]
