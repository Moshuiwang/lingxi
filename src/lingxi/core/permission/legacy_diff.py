"""存量用户首聊差集导入的纯逻辑（rc25 S-1，Issue #540；沿用 #441 差集口径）。

旧系统正式权限多维表格里已有行的用户在 Lingxi 首次开通时，把「旧行权限 − 银河当前
翻译」落成管理员本地授权（``local_permission_override``，方向 grant，原因
:data:`IMPORT_REASON`），随后仍按 ``(银河 ∪ 本地) − 抑制`` 合成发布——「银河是银河的，
本地是本地的」（PM 2026-09-02 裁定）。本模块**零 I/O**：只回答"给定旧行、银河当前翻译
与指标映射，要导入哪些行"，真正的读表在 ``adapters/stock_token_bitable.py``、落库在
``adapters/postgres_local_permission.py::import_legacy_plan``、编排在
``core/identity/onboarding_runner.py``。

## 旧行的四种形状（:func:`classify_legacy_permissions`）

| 形状 | 判据 | 落法 |
| --- | --- | --- |
| :data:`SHAPE_SPECIFIC` | 键与值都不含 ``"*"``（含空对象 ``{}``） | 按公司键逐一求差集，每对一行 grant（无组） |
| :data:`SHAPE_FULL_WILDCARD` | ``"*"`` 键的值恰为 ``["*"]`` | 一组 ``company_id="*"`` × 映射全部指标（:func:`all_metrics`），标签 :data:`ALL_SCOPE_POSITION_NAME`，随映射新增指标补行 |
| :data:`SHAPE_ALL_SCOPE_EXPLICIT` | ``"*"`` 键的值是不含 ``"*"`` 的显式指标列表 | 一组 ``company_id="*"`` × 该列表，标签 :data:`ALL_SCOPE_EXPLICIT_POSITION_NAME`，**永不**自动扩指标 |
| :data:`SHAPE_UNSUPPORTED_WILDCARD` | 其余含 ``"*"`` 的形状（``["*","x"]``、具体公司值里出现 ``"*"``） | 调用方 fail-closed，外部表零写入 |

两种「全部」形状都可以附带具体公司键（如 ``{"*": [...], "40": [...]}``）：具体键按差集
另落无组行。**公司维度保留 ``*``、指标维度显式展开**是 PM 的明示裁定：新公司无需改表
（问数 MCP 读侧按 ``"*"`` 回退），新指标进入映射后由每日/定向重算补齐同组缺行
（:func:`missing_all_scope_metrics`）。

## 差集口径

- 银河**非通配**：每个具体公司键 ``旧行[公司] − 银河[公司]``；``"*"`` 组不减（银河没有
  ``"*"``，合并层会把组与银河各公司值一起收敛到 ``"*"`` 键）。
- 银河**有限通配**（``"*"`` 键但非真全指标，Issue #440 v2）：合并层把全部本地 grant 指标
  并进 ``"*"`` 清单，因此组与具体键都减去 ``银河["*"]``。
- 银河**真全指标通配**（``full_access_wildcard``，后台管理员）：本地源整体不参与合并，
  什么都不导入，理由码 :data:`REASON_WILDCARD_GALAXY_CURRENT`。
- **映射外公司照导入**（PM「本地是本地的」；问数 MCP 认识这些公司），只计数
  :attr:`LegacyImportPlan.unmapped_companies_kept` 供审计。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from lingxi.core.permission.local_override import LocalPermissionOverrideEntry, OverrideDirection

#: 「全非」通配键；与 ``publish_row``/``metric_translation``/``merge_sources`` 各自的
#: 同名常量是同一个字面量的独立拷贝（仓库既定姿态，不建反向依赖）。
ALL_COMPANIES_KEY = "*"

#: local_permission_override.reason——每一行导入产生的授权用这句话标注来源。
IMPORT_REASON = "2.0 迁移导入"

#: 合成 pending_action.reason（首聊导入）。
PENDING_ACTION_REASON = "legacy_import_2_0"

#: 合成 pending_action.reason（新指标进入映射后重算补齐「全部」组）。
ALL_SCOPE_REFRESH_REASON = "legacy_all_scope_refresh"

#: ``{"*":["*"]}`` 落成的「全部」组在管理卡上显示的职位标签；同时是识别该组的判据
#: （``company_id="*"`` 且 ``position_name`` 恰为此值）。**只有这个标签的组**会随映射
#: 新增指标自动补行（:func:`missing_all_scope_metrics`）——它的语义是「全部指标」。
ALL_SCOPE_POSITION_NAME = "2.0 迁移导入·全部"

#: ``{"*":[显式列表]}`` 落成的组的职位标签：公司维度同样保留 ``*``，但指标是旧行
#: **列出的那几个**，语义不是「全部指标」，因此**永不**随映射自动扩指标（独立审核
#: P1 坐实：两种形状共用一个标签会让显式列表用户在次日重算被静默扩成映射全部指标）。
ALL_SCOPE_EXPLICIT_POSITION_NAME = "2.0 迁移导入·全部公司（指定指标）"

#: 导入行的 ``initiated_by_open_id``/``decided_by_open_id``：系统常量，不冒充任何真人。
LEGACY_IMPORT_ACTOR = "lingxi:legacy_import_2_0"

SHAPE_SPECIFIC = "specific"
SHAPE_FULL_WILDCARD = "full_wildcard"
SHAPE_ALL_SCOPE_EXPLICIT = "all_scope_explicit"
SHAPE_UNSUPPORTED_WILDCARD = "unsupported_wildcard"

#: 银河侧为真全指标通配：整份不导入。
REASON_WILDCARD_GALAXY_CURRENT = "wildcard_galaxy_current"
#: 差集为空：没有可导入的内容（不是失败）。
REASON_NOTHING_TO_IMPORT = "nothing_to_import"
#: ``{"*":["*"]}`` 需要展开成映射全部指标，但映射里一个指标都没有：调用方 fail-closed。
REASON_ALL_METRICS_UNAVAILABLE = "legacy_all_metrics_unavailable"
#: 形状不受支持：调用方 fail-closed。
REASON_SHAPE_UNSUPPORTED = "legacy_wildcard_shape_unsupported"


def _blank(value: object) -> bool:
    return not isinstance(value, str) or not value.strip()


def classify_legacy_permissions(document: Mapping[str, Sequence[str]]) -> str:
    """判定旧行形状（模块文档的表）。键或指标名为空白时抛 ``ValueError``——那是解析
    失败，与「形状不受支持」是两种不同的 fail-closed 原因。"""

    for company, metrics in document.items():
        if _blank(company):
            raise ValueError("旧行权限的公司键不得为空白")
        for metric in metrics:
            if _blank(metric):
                raise ValueError("旧行权限的指标名不得为空白")
    star = document.get(ALL_COMPANIES_KEY)
    for company, metrics in document.items():
        if company != ALL_COMPANIES_KEY and ALL_COMPANIES_KEY in metrics:
            return SHAPE_UNSUPPORTED_WILDCARD
    if star is None:
        return SHAPE_SPECIFIC
    star_values = tuple(star)
    if star_values and set(star_values) == {ALL_COMPANIES_KEY}:
        return SHAPE_FULL_WILDCARD
    if ALL_COMPANIES_KEY in star_values:
        return SHAPE_UNSUPPORTED_WILDCARD
    return SHAPE_ALL_SCOPE_EXPLICIT


def all_metrics(mapping: Mapping[str, Mapping[str, Sequence[str]]]) -> tuple[str, ...]:
    """映射里全部公司、全部职能的指标名并集（排序去重）——``{"*":["*"]}`` 的显式展开。"""

    collected: set[str] = set()
    for functions in mapping.values():
        for metrics in functions.values():
            collected.update(metric for metric in metrics if isinstance(metric, str) and metric)
    return tuple(sorted(collected))


def compute_company_diff(
    legacy: Mapping[str, Sequence[str]], galaxy_current: Mapping[str, Sequence[str]]
) -> dict[str, tuple[str, ...]]:
    """按公司键计算「旧表 − 银河当前能给的」差集，减到空的公司键整体丢弃。

    ``galaxy_current`` 出现 :data:`ALL_COMPANIES_KEY` 时整份差集恒为空——这是
    ``scripts/ops/import_local_permission_override.py`` 沿用至今的保守口径（脚本无法
    分辨通配形态）；首聊自动路径请用 :func:`plan_legacy_import`，它按
    ``full_access_wildcard`` 区分两种通配。
    """

    if ALL_COMPANIES_KEY in galaxy_current:
        return {}
    result: dict[str, tuple[str, ...]] = {}
    for company_id, metrics in legacy.items():
        remaining = set(metrics) - set(galaxy_current.get(company_id, ()))
        if remaining:
            result[company_id] = tuple(sorted(remaining))
    return result


@dataclass(frozen=True)
class LegacyImportReport:
    """一次存量差集导入的落库结果：只有计数与组 ID，不含任何公司键或指标名——它直接
    进审计。住在本纯模块（而不是 ``core/identity/onboarding_ports.py``）是为了让
    ``adapters/postgres_local_permission.py`` 不必反向依赖开通编排的端口模块。"""

    imported: int
    already_present: int
    group_id: str | None = None
    group_created: bool = False
    #: 该用户曾有一个「全部」组被管理员整组撤销，本次不再重建（撤销过的组不复活）。
    group_skipped_revoked: bool = False
    #: 计划里因「同键曾被管理员撤销」而没有重建的行数（组内单条 + 具体公司行）。
    revoked_skipped: int = 0


@dataclass(frozen=True)
class LegacyImportPlan:
    """一次首聊差集导入的完整计划：形状、具体公司行、「全部」组指标、跳过理由与审计计数。"""

    shape: str
    pairs: tuple[tuple[str, str], ...]
    all_scope_metrics: tuple[str, ...]
    skipped_reasons: tuple[str, ...]
    unmapped_companies_kept: int

    @property
    def nothing_to_import(self) -> bool:
        return not self.pairs and not self.all_scope_metrics

    @property
    def all_scope_position_name(self) -> str:
        """「全部」组落库用的职位标签：全通配形状用 :data:`ALL_SCOPE_POSITION_NAME`
        （会随映射补齐指标），显式列表形状用 :data:`ALL_SCOPE_EXPLICIT_POSITION_NAME`
        （永不自动扩指标）。"""

        if self.shape == SHAPE_FULL_WILDCARD:
            return ALL_SCOPE_POSITION_NAME
        return ALL_SCOPE_EXPLICIT_POSITION_NAME


def plan_legacy_import(
    *,
    legacy: Mapping[str, Sequence[str]],
    galaxy_current: Mapping[str, Sequence[str]],
    full_access_wildcard: bool,
    mapping: Mapping[str, Mapping[str, Sequence[str]]],
) -> LegacyImportPlan:
    """把「旧行 + 银河当前翻译 + 映射」编排成导入计划（模块文档「差集口径」）。

    ``legacy`` 是 :func:`~lingxi.core.permission.publish_row.parse_permissions` 的产出；
    ``galaxy_current`` 是 :func:`~lingxi.core.permission.metric_translation.
    translate_company_functions` 的产出（零银河用户传 ``{}``）；``full_access_wildcard``
    与 :func:`~lingxi.core.permission.merge_sources.merge_permission_sources` 同一判据。
    """

    shape = classify_legacy_permissions(legacy)
    if shape == SHAPE_UNSUPPORTED_WILDCARD:
        return LegacyImportPlan(shape, (), (), (REASON_SHAPE_UNSUPPORTED,), 0)

    galaxy_has_star = ALL_COMPANIES_KEY in galaxy_current
    if galaxy_has_star and full_access_wildcard:
        return LegacyImportPlan(shape, (), (), (REASON_WILDCARD_GALAXY_CURRENT,), 0)
    star_baseline = set(galaxy_current.get(ALL_COMPANIES_KEY, ())) if galaxy_has_star else None

    skipped: list[str] = []
    if shape == SHAPE_FULL_WILDCARD:
        group_candidates = set(all_metrics(mapping))
        if not group_candidates:
            skipped.append(REASON_ALL_METRICS_UNAVAILABLE)
    elif shape == SHAPE_ALL_SCOPE_EXPLICIT:
        group_candidates = set(legacy[ALL_COMPANIES_KEY])
    else:
        group_candidates = set()
    if star_baseline is not None:
        group_candidates -= star_baseline
    group = tuple(sorted(group_candidates))

    pairs: list[tuple[str, str]] = []
    unmapped = 0
    for company in sorted(key for key in legacy if key != ALL_COMPANIES_KEY):
        remaining = set(legacy[company])
        if star_baseline is not None:
            remaining -= star_baseline
        else:
            remaining -= set(galaxy_current.get(company, ()))
        # 与「全部」组重叠的指标不再逐公司落行：合并层会把组与具体键一起收敛到 "*"。
        remaining -= group_candidates
        if not remaining:
            continue
        if company not in mapping:
            unmapped += 1
        pairs.extend((company, metric) for metric in sorted(remaining))

    if not pairs and not group and not skipped:
        skipped.append(REASON_NOTHING_TO_IMPORT)
    return LegacyImportPlan(shape, tuple(pairs), group, tuple(skipped), unmapped)


def missing_all_scope_metrics(
    entries: Iterable[LocalPermissionOverrideEntry],
    mapping: Mapping[str, Mapping[str, Sequence[str]]],
) -> dict[str, tuple[str, ...]]:
    """对当前生效的「全部指标」组（``direction=grant``、``company_id="*"``、
    ``position_name`` **恰为** :data:`ALL_SCOPE_POSITION_NAME`、带组 ID），算出映射里
    已有、组里还没有的指标——新指标进来时重算据此补行。显式列表组
    （:data:`ALL_SCOPE_EXPLICIT_POSITION_NAME`）**不参与**：它的指标是旧行列出的那几个，
    自动扩成映射全部指标就是越权。只看传入的（生效）条目：撤销过的组不在其中，因此不会
    复活。返回 ``{组 ID: (缺的指标, …)}``，没有缺项的组不出现。"""

    present: dict[str, set[str]] = {}
    for entry in entries:
        if (
            entry.direction is OverrideDirection.GRANT
            and entry.company_id == ALL_COMPANIES_KEY
            and entry.position_name == ALL_SCOPE_POSITION_NAME
            and entry.permission_group_id
        ):
            present.setdefault(entry.permission_group_id, set()).add(entry.metric_name)
    if not present:
        return {}
    universe = set(all_metrics(mapping))
    missing: dict[str, tuple[str, ...]] = {}
    for group_id, metrics in present.items():
        gap = universe - metrics
        if gap:
            missing[group_id] = tuple(sorted(gap))
    return missing


__all__ = [
    "ALL_COMPANIES_KEY",
    "ALL_SCOPE_EXPLICIT_POSITION_NAME",
    "ALL_SCOPE_POSITION_NAME",
    "ALL_SCOPE_REFRESH_REASON",
    "IMPORT_REASON",
    "LEGACY_IMPORT_ACTOR",
    "LegacyImportPlan",
    "LegacyImportReport",
    "PENDING_ACTION_REASON",
    "REASON_ALL_METRICS_UNAVAILABLE",
    "REASON_NOTHING_TO_IMPORT",
    "REASON_SHAPE_UNSUPPORTED",
    "REASON_WILDCARD_GALAXY_CURRENT",
    "SHAPE_ALL_SCOPE_EXPLICIT",
    "SHAPE_FULL_WILDCARD",
    "SHAPE_SPECIFIC",
    "SHAPE_UNSUPPORTED_WILDCARD",
    "all_metrics",
    "classify_legacy_permissions",
    "compute_company_diff",
    "missing_all_scope_metrics",
    "plan_legacy_import",
]
