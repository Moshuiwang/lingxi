"""存量用户首聊差集导入的纯逻辑。

旧系统正式权限多维表格里已有行的用户在 Lingxi 首次开通时，把「旧行权限 − 银河当前
翻译」落成管理员本地授权（``local_permission_override``，方向 grant），随后仍按
``(银河 ∪ 本地) − 抑制`` 合成发布——银河是银河的，本地是本地的。本模块零 I/O：
只回答"给定旧行、银河当前翻译与指标映射，要导入哪些行"，真正的读表、落库、编排
都在别处。

旧行的四种形状与落法见 :func:`classify_legacy_permissions`；差集按银河是否通配
分三种口径见 :func:`plan_legacy_import`；公司键与指标名的取值卫生判据（同一套
形状检查、不同的目录态度）见 :func:`_malformed`。
"""

from __future__ import annotations

import unicodedata
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
#: 列出的那几个，语义不是「全部指标」，因此永不随映射自动扩指标——两种形状共用
#: 一个标签会让显式列表用户在次日重算被静默扩成映射全部指标。
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


#: 公司键与指标名里一律不允许出现的 Unicode 类别：控制字符（``Cc``——换行、回车、制表
#: 都在里面）、格式字符（``Cf``——零宽空格、双向覆盖）、代理与私用区（``Cs``/``Co``）、
#: 行与段分隔符（``Zl``/``Zp``）。这些字符肉眼不可见或不可分辨，出现在一份要落成权限的
#: 名字里只有两种可能：导出坏了，或有人在藏东西——两种都该整份拒绝，不该"尽力解析"。
_FORBIDDEN_CHARACTER_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Zl", "Zp"})


def _malformed(value: object) -> str | None:
    """公司键 / 指标名的卫生判据：不合格返回原因短语，合格返回 ``None``。

    只看形状（键与值走同一套），不看目录——公司/指标在不在映射里由调用方各自处理，
    值必须在目录内，公司键不核对。键宽松、值严格是刻意的：值决定给多大范围
    （``"*"`` 落库后回退制会让它等于该公司全部指标含未来新增），公司键只决定给
    哪一家公司，写错了只是一条没人用的死行。同理，刻意不管名字首尾的普通空格：
    真实旧表导出里这类脏数据很常见，为它整份拒绝没有必要。
    """
    if not isinstance(value, str) or not value.strip():
        return "不得为空白"
    for character in value:
        if unicodedata.category(character) in _FORBIDDEN_CHARACTER_CATEGORIES:
            return "不得含换行、制表符或其他不可见字符"
        if character.isspace() and character != " ":
            return "不得含普通空格以外的空白字符"
    if ALL_COMPANIES_KEY in value and value != ALL_COMPANIES_KEY:
        return f"不得把通配符 {ALL_COMPANIES_KEY} 混进名字里"
    return None


def classify_legacy_permissions(document: Mapping[str, Sequence[str]]) -> str:
    """判定旧行形状；键或指标名不合卫生（见 :func:`_malformed`）时抛 ``ValueError``。

    - :data:`SHAPE_SPECIFIC`：键与值都不含 ``"*"``，按公司键求差集，每对一行 grant。
    - :data:`SHAPE_FULL_WILDCARD`：``"*"`` 键的值恰为 ``["*"]``，落一组映射全部
      指标，随映射新增指标补行。
    - :data:`SHAPE_ALL_SCOPE_EXPLICIT`：``"*"`` 键的值是不含 ``"*"`` 的显式列表，
      落一组该列表，永不自动扩指标。
    - :data:`SHAPE_UNSUPPORTED_WILDCARD`：其余含 ``"*"`` 的形状，调用方 fail-closed，
      与解析失败同属 fail-closed、后果相同（整份不导入、零写入），原因不同。
    """
    for company, metrics in document.items():
        problem = _malformed(company)
        if problem is not None:
            raise ValueError(f"旧行权限的公司键{problem}")
        for metric in metrics:
            problem = _malformed(metric)
            if problem is not None:
                raise ValueError(f"旧行权限的指标名{problem}")
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
    """一次存量差集导入的落库结果。

    只有计数与组 ID，不含任何公司键或指标名——它直接进审计。住在本纯模块（而不是
    ``core/identity/onboarding_ports.py``）是为了让
    ``adapters/postgres_local_permission.py`` 不必反向依赖开通编排的端口模块。
    """

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
        """整份计划是否没有任何要导入的内容。"""
        return not self.pairs and not self.all_scope_metrics

    @property
    def all_scope_position_name(self) -> str:
        """「全部」组落库用的职位标签。

        全通配形状用 :data:`ALL_SCOPE_POSITION_NAME`（会随映射补齐指标），显式
        列表形状用 :data:`ALL_SCOPE_EXPLICIT_POSITION_NAME`（永不自动扩指标）。
        """
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
    """把「旧行 + 银河当前翻译 + 映射」编排成导入计划。

    差集按银河是否通配分三种口径：银河非通配——每个具体公司键单独求差集，``"*"``
    组不减；银河有限通配（``"*"`` 键非真全指标）——组与具体键都减去银河 ``"*"``；
    银河真全指标通配（``full_access_wildcard``）——本地源整体不导入。映射外公司
    照常导入，只计数供审计。两种「全部」形状可附带具体公司键，按差集另落无组行——
    公司维度保留 ``*``、指标维度显式展开，新公司无需改表，新指标由重算补齐。
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
    """算出每个生效「全部指标」组里，映射已有但组里还没有的指标。

    只看 ``direction=grant``、``company_id="*"``、``position_name`` 恰为
    :data:`ALL_SCOPE_POSITION_NAME` 的带组 ID 条目；显式列表组
    （:data:`ALL_SCOPE_EXPLICIT_POSITION_NAME`）不参与，自动扩成全部指标就是
    越权。只看传入的（生效）条目，撤销过的组不会复活。返回
    ``{组 ID: (缺的指标, …)}``，没有缺项的组不出现。
    """
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
