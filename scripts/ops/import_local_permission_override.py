#!/usr/bin/env python3
"""旧系统权限表差集导入为管理员本地授权（受控运行脚本，不属于生产镜像，Issue #441）。

## 产品裁定（PM 2026-08-30）与本工具的定位

旧系统（biai-agent）正式权限多维表格的存量用户权限，不再作为"存量沿用"参与每日
权限合并（该机制已随 Issue #441 退役，见 ``core/permission/merge_sources.py``
模块文档）；改为**一次性差集导入为管理员本地授权**
（``local_permission_override``，``direction=grant``，原因「2.0 迁移导入」）——
与银河当时的授权不冲突，逐行留痕，可单独收回（复用既有 ``/admin`` 本地权限收回
命令，不新增任何撤权机制）。

导入口径是**差集**：`旧表该用户的指标集合 − 银河当前能给这个人的指标集合`，
按公司键分别计算，只把"旧表有、银河没有"的那部分作为一笔额外授权导入；银河已经
覆盖的部分不重复导入（那部分早已经由银河翻译结果本身发布，不需要本地覆盖再给
一次）。

**执行分两步（PM 裁定）**：① 本工具先在 ``biai-stage`` 用权限表测试副本跑通导入
并验证发布效果（stage 演练，L4a，由编排者执行）；② **真实旧表平移在 Issue #263
硬切步执行**——旧表是生产资产，stage 不直接读写，真实导出快照的产生与导入前
的 PM 抽样核对清单由 #263 的切换步骤负责，本工具只负责"给定一份快照文件，正确
计算差集并写入"这一件事。

## 输入一：旧表只读导出快照（CSV，两列 ``email``/``permissions``）

```csv
email,permissions
zhang.san@example.com,"{""1011"": [""日活""]}"
```

- ``email``：该用户在旧表里登记的邮箱（按 :func:`~lingxi.core.permission.
  account_match.normalize_email` 归一后用于匹配，大小写/首尾空白不敏感）。
- ``permissions``：旧表 ``permissions`` 单元格的原始 JSON 文本，`{公司ID:
  [指标名, …]}` 形状——与 :func:`~lingxi.core.permission.publish_row.
  parse_permissions` 的既有读侧解析器同一格式（旧系统与 Lingxi 对这张表用的是
  同一套编码约定，S-P-2 退役前的 ``legacy_source.py`` 读的正是这份文本）。

同一邮箱在导出文件里出现两行视为导出本身有歧义，**整个导入拒绝**，不猜哪一行
为准——与已退役的存量沿用机制"命中多行即失败关闭"同一姿态。真实导出如何从旧
Feishu 多维表格产生这份 CSV、导入前如何抽样核对，写在 #263 的切换清单里，不在
本工具范围。

**任意一行 ``permissions`` 使用了 :data:`~lingxi.core.permission.publish_row.
ALL_COMPANIES_KEY`（``"*"``）键，同样整份导出拒绝导入**（rc21 修复包 B）：旧表
自己写的"*"通配与本工具在银河一侧判定的"通配管理员"是两个不相关的概念，把它
当作普通 ``company_id="*"`` 写进 ``local_permission_override`` 会让该用户凭一条
本地授权行跨公司越权（读侧 ``lookup_metrics`` 的"*"回退制对任何没有具体公司键
命中的查询都会命中这一行）。旧表通配用户的平移方式留 #263 由 PM 单裁，本工具
不猜、不代为决定，命中即整份拒绝——dry-run（含未来的 ``--apply``）都会看到同一
条拒绝原因。

**值同样整份校验，不是只判键**（rc25 S-2d，对抗审查 P-2）：任意一行的
``permissions`` **值**里出现 ``"*"``（``{"1011": ["*"]}``）、空白值、含换行或
零宽等不可见字符的指标名、把 ``"*"`` 混进名字里的指标名（``"日活*"``），一律
**整份导出拒绝导入**，退出码 2、零写入——不是跳过这一条继续。``"*"`` 值一旦
落进 ``local_permission_override``，读侧 ``lookup_metrics`` 的回退制会让它等于
"该公司全部指标（含未来新增）"，而且此后针对单个指标的抑制减不掉它。判定落在
:func:`~lingxi.core.permission.legacy_diff.classify_legacy_permissions`——首聊
自动路径走的是同一个函数，两条路径的取值口径结构上不会分叉。

**只拒值、不拒公司键**：本工具**不**核对公司键在不在翻译映射里。「映射外公司
照常导入」是 PM 2026-09-02 对 rc25 S-1 的明示裁定（「本地是本地的」，问数 MCP
认识 40–43 这些公司）。两者后果不同：值决定**给多大范围**，公司键只决定**给
哪一家公司**——写错公司号导入的是一条谁也用不上的死行，不扩大可见范围。

## 输入二：当前银河快照（从库读，不需要额外文件）

对旧表里出现的每一个邮箱，本工具按**与每日权限重算完全相同**的匹配 + 聚合 +
翻译流水线（:mod:`lingxi.core.permission.account_match`、
:mod:`lingxi.core.permission.publish_row`、
:mod:`lingxi.core.permission.metric_translation`）现算一遍"银河此刻能给这个人
什么"——不复用旧表本身、不复用正式权限发布表（那张表可能已经混有 Lingxi 自己
发布过的内容，拿它当基线会把自己的发布结果当成"银河给的"，见已退役机制的
"有界化"教训）。匹配用的"花名册行"直接由该用户在 ``app_user`` 里已经登记的
``employee_no``/``email``/``display_name`` 现构一行——这些用户结构上已经走过
一次真实开通链的花名册匹配，此刻只需要重新问一次"银河现在还给不给"，不需要
重新读一次花名册快照。

**以下情形整体跳过该用户**（不导入、不猜、计入 dry-run 清单的"跳过"一栏，供
人工核对）：

- 该邮箱在 ``app_user`` 里查不到、或命中多行（判定不了导给谁）；
- 命中的用户 ``account_state`` 不是 ``enabled``（已停用/删除中/已删除的账号
  不需要新导入任何权限）；
- 按工号/邮箱双键在当前银河快照里定位不到这个人（``account_match`` 判
  ``not_found``）；
- 银河判定有权限但翻译层（公司+职能→指标名）覆盖不全（fail-closed，与每日
  重算同一姿态，不产出部分结果）。

银河侧命中通配（``all_companies=True``——不论是 513 通配管理员走「全非」范围，
还是「角色即全公司」B 口径特例，两种形态下 :func:`resolve_galaxy_current` 的
产出都只有 :data:`~lingxi.core.permission.publish_row.ALL_COMPANIES_KEY` 一个
键）时，视为银河已经覆盖旧表可能给出的任何具体公司权限，该用户差集恒为空、
不导入——**登记进 dry-run 清单的"跳过"一栏**（原因码
:data:`REASON_WILDCARD_GALAXY_CURRENT`，rc21 修复包 B 前是静默零输出，人工
核对时分不清"银河已覆盖"与"这个人在旧表里本来就没数据"）；有限通配下差集
语义能否再精细化留 #263 裁定，本工具现在一律按恒空处理。

## 幂等

导入前对每一个 ``(user_id, company_id, metric_name)`` 先查是否已有同极性
（``direction='grant'``）生效行，命中则跳过（计入"已存在"，不重复写入）；
即使这一步之后仍然撞上数据库唯一索引（并发写入的极小概率窗口），本工具捕获
:class:`~lingxi.adapters.postgres_local_permission.DuplicateActiveOverride`
后同样降级为"已存在"，不让异常中断整批导入。同一份导出文件反复执行本工具，
产出的新增行数恒为零。

## 确认卡与 ``pending_action``

``local_permission_override.pending_action_id`` 是结构性 ``NOT NULL`` 外键
（迁移 ``0072``："没有确认卡不能写入"）。本工具不是交互式管理员操作，没有真实
的飞书确认卡片，因此为每一笔导入直接写一行**已经是终态**的合成
``pending_action``（``action_type='local_permission_grant'``,
``status='executed'``，``card_delivered=FALSE`` 如实反映"这里从未真的发过
卡片"，``reason='legacy_import_2_0'`` 供审计一眼分辨这是批量导入产生的而不是
一次真实的管理员点击）；``payload`` 与真实命令面写入的形状完全一致
（``{"company_id", "metric_name", "reason"}``），因此这批记录未来如果被读回
（例如人工核对某条 override 的来龙去脉）不会撞上任何解析假设。

**rc25 S-1 起落库委托 ``PostgresLocalPermissionOverrideStore.import_legacy_plan``**：
合成 ``pending_action`` 的 ``payload`` 形状随之变为
``{"legacy_import_2_0": {"shape", "specific_pairs", "all_scope_metrics",
"permission_group_id"}, "reason": "2.0 迁移导入"}``（此前是逐笔
``{"company_id","metric_name","reason"}``）；按 payload 排查时以此为准。

``initiated_by_open_id``/``decided_by_open_id`` 取本工具 ``--initiated-by``
参数（运行这次导入的责任人飞书 open_id，通常是产品负责人或受托操作者本人）——
不写死任何值，避免审计栏目显示一个无法追溯的占位身份。

**``--initiated-by`` 必须是一位生效的已登记管理员**（rc25 S-2d，对抗审查 P-8）：
在做任何事之前（连 dry-run 也过这一关）按 ``admin_registry`` 核对一次，判据复用
:func:`~lingxi.core.admin.registry.is_authorized_admin` 这条既有的默认拒绝谓词
（条目不存在、非 active、三类角色没有全部授予，一律不是管理员），不另写一套。
不是登记管理员、或登记表读不出来 → 整次运行拒绝，退出码 2、零写入。理由：这个
open_id 会被逐行写进 ``initiated_by_open_id``/``decided_by_open_id``，是审计上
"这批授权是谁批的"的唯一答案，不能是一个谁都能随手填的自由文本。

## 用法

**写入极性（rc21 修复包 B）：默认只出计划，不写入任何一行；要真正写入必须显式
加 `--apply`**——``--dry-run`` 保留为该默认行为的兼容别名。

```
export LINGXI_POSTGRES_DSN='postgresql://...'
PYTHONPATH=src python3 scripts/ops/import_local_permission_override.py \\
    /path/to/legacy_export.csv --initiated-by ou_xxx

# 核对无误后加 --apply 真正写入
PYTHONPATH=src python3 scripts/ops/import_local_permission_override.py \\
    /path/to/legacy_export.csv --initiated-by ou_xxx --apply
```

``--dsn`` 可覆盖 ``LINGXI_POSTGRES_DSN``；两者都缺失时拒绝运行。凭据只从环境
变量/命令行参数读，不落盘、不进日志（本工具的全部输出只含邮箱、公司ID、指标
名与计数，这些是本次迁移要抽样核对的内容本身，不是凭据）。
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from lingxi.core.permission.account_match import MATCHED, match_galaxy_account, normalize_email
from lingxi.core.permission.legacy_diff import (
    SHAPE_SPECIFIC,
    LegacyImportPlan,
    classify_legacy_permissions,
    compute_company_diff,
)
from lingxi.core.permission.metric_translation import (
    UncoveredPermissionCombination,
    translate_company_functions,
)
from lingxi.core.permission.publish_row import (
    ALL_COMPANIES_KEY,
    aggregate_permission,
    parse_permissions,
)

# ``IMPORT_REASON``/``PENDING_ACTION_REASON``/``compute_company_diff`` 自 rc25 S-1
# （Issue #540）起住在 ``core/permission/legacy_diff.py``——首聊自动路径与本脚本共用
# 同一份差集口径与同一个落库方法（``PostgresLocalPermissionOverrideStore.
# import_legacy_plan``），本脚本只保留 CLI、旧表导出解析与 dry-run 编排。上面的
# 三个名字仍从本模块可见（既有测试与文档按此引用）。

#: 与 core/admin/pending_action.py 的 PendingActionType.LOCAL_PERMISSION_GRANT
#: 取值逐字相同——两处不互相 import（该模块在 core/，本脚本不受三层 import
#: 规则约束但没有理由另起一个字符串），字面量各自独立登记。
ACTION_TYPE_GRANT = "local_permission_grant"

#: 与 core/permission/local_override.OverrideDirection.GRANT.value 相同。
DIRECTION_GRANT = "grant"


# ---------------------------------------------------------------------------
# 一、纯逻辑：差集计算 + 用户级判定编排（零 I/O，供单元测试直接调用）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedGrant:
    """差集里的一条待导入授权：一个 (用户, 公司, 指标) 三元组。"""

    email: str
    user_id: str
    feishu_open_id: str
    company_id: str
    metric_name: str


@dataclass(frozen=True)
class SkippedUser:
    """该用户整体跳过，不产出任何 :class:`PlannedGrant`（供 dry-run 清单标注原因，
    人工核对时按 ``reason`` 分类查看）。"""

    email: str
    reason: str


@dataclass(frozen=True)
class ImportPlan:
    """一次导入的完整计划：差集算出来的待办 + 被跳过的用户，dry-run 与真正写入
    共用同一份计划——真正写入只是多做"逐条落库"这一步，判定逻辑不重复一份。"""

    grants: tuple[PlannedGrant, ...]
    skipped: tuple[SkippedUser, ...]


@dataclass(frozen=True)
class AppUserRecord:
    """按邮箱查到的目标用户，字段直接来自 ``app_user`` 表——本工具只信这张表
    此刻记录的身份，不重新读花名册快照。"""

    user_id: str
    employee_no: str
    email: str
    feishu_open_id: str
    display_name: str
    account_state: str


@dataclass(frozen=True)
class UserLookup:
    """按邮箱查 ``app_user`` 的结果：三种互斥情形——找到唯一一行、查无此邮箱、
    命中多行（歧义，判定不了导给谁）。"""

    record: AppUserRecord | None
    ambiguous: bool = False


class GalaxySnapshotLike(Protocol):
    """:class:`~lingxi.adapters.postgres_galaxy_snapshot.GalaxyPermissionSnapshot`
    的只读子集——本模块只用得到这四样，声明协议而不是直接依赖具体类型，方便
    单元测试注入轻量假实现。"""

    user_rows: tuple[Mapping[str, Any], ...]
    country_rows: tuple[Mapping[str, Any], ...]

    def role_rows(self, galaxy_user_id: Any) -> tuple[Mapping[str, Any], ...]: ...

    def datacountry_rows(self, galaxy_user_id: Any) -> tuple[Mapping[str, Any], ...]: ...


#: :func:`resolve_galaxy_current` 跳过原因码，供 dry-run 清单与测试断言引用。
REASON_ACCOUNT_NOT_ENABLED = "account_not_enabled"
REASON_APP_USER_NOT_FOUND = "app_user_not_found"
REASON_APP_USER_AMBIGUOUS = "app_user_ambiguous"
REASON_GALAXY_ACCOUNT_PREFIX = "galaxy_account_"
REASON_TRANSLATION_UNAVAILABLE = "metric_translation_unavailable"
REASON_TRANSLATION_UNCOVERED = "metric_translation_uncovered"

#: :func:`plan_import` 在 ``galaxy_current`` 命中 :data:`~lingxi.core.
#: permission.publish_row.ALL_COMPANIES_KEY` 时登记的跳过原因码（rc21 修复包 B，
#: P1+P2+P3 之 b）——见 :func:`plan_import` 内该分支上方注释。
REASON_WILDCARD_GALAXY_CURRENT = "wildcard_galaxy_current"


def resolve_galaxy_current(
    *,
    app_user: AppUserRecord,
    galaxy: GalaxySnapshotLike,
    role_function_map: Mapping[str, str],
    metric_translation_map: Mapping[str, Mapping[str, Sequence[str]]],
) -> tuple[dict[str, tuple[str, ...]] | None, str | None]:
    """算一遍"银河此刻能给这个人什么"，返回 ``({公司: (指标名, …)}, None)`` 或
    ``(None, 跳过原因)``——与 :mod:`lingxi.apps.scheduler.permission_refresh`
    的 ``_refresh_user`` 走同一条匹配 + 聚合 + 翻译流水线，模块文档「输入二」
    一节说明为什么现算而不是复用正式发布表。

    银河侧判定"无可用权限"（``aggregate.granted`` 为假）时返回 ``({}, None)``
    ——空映射对 :func:`compute_company_diff` 恒等（旧表内容全部计入差集），
    与"零银河权限用户的本地授权兜底"（`V-权限-15`）同一口径：银河这一侧没有
    贡献，不代表这个人不该被单独授权。
    """

    roster_row = {
        "personnel_id": app_user.user_id,
        "employee_no": app_user.employee_no,
        "email": app_user.email,
        "name": app_user.display_name,
    }
    match = match_galaxy_account(app_user.user_id, [roster_row], galaxy.user_rows)
    if match.state != MATCHED:
        return None, f"{REASON_GALAXY_ACCOUNT_PREFIX}{match.reason}"

    aggregate = aggregate_permission(
        galaxy_user_id=match.galaxy_user_id,
        user_role_rows=galaxy.role_rows(match.galaxy_user_id),
        datacountry_rows=galaxy.datacountry_rows(match.galaxy_user_id),
        country_rows=galaxy.country_rows,
        role_function_map=role_function_map,
    )
    if not aggregate.granted:
        return {}, None

    try:
        company_metrics = translate_company_functions(
            companies=aggregate.companies,
            functions=aggregate.functions,
            all_companies=aggregate.all_companies,
            mapping=metric_translation_map,
        )
    except UncoveredPermissionCombination as error:
        reason = REASON_TRANSLATION_UNAVAILABLE if error.mapping_is_empty else REASON_TRANSLATION_UNCOVERED
        return None, reason
    return dict(company_metrics), None


def plan_import(
    *,
    legacy: Mapping[str, Mapping[str, Sequence[str]]],
    galaxy: GalaxySnapshotLike,
    role_function_map: Mapping[str, str],
    metric_translation_map: Mapping[str, Mapping[str, Sequence[str]]],
    lookup_user: Callable[[str], UserLookup],
) -> ImportPlan:
    """把「旧表快照 + 银河快照」编排成一份完整导入计划，零数据库依赖——
    ``lookup_user`` 是唯一的外部输入端口，供单元测试注入假实现；真实装配见
    :func:`_lookup_app_user_by_email`。

    输出按邮箱、公司、指标名排序（:class:`ImportPlan` 的两个元组都是确定顺序），
    dry-run 输出与真正写入的落库顺序因此逐字节可复现，不依赖字典遍历顺序这类
    非承诺行为。
    """

    grants: list[PlannedGrant] = []
    skipped: list[SkippedUser] = []
    for email in sorted(legacy):
        lookup = lookup_user(email)
        if lookup.ambiguous:
            skipped.append(SkippedUser(email=email, reason=REASON_APP_USER_AMBIGUOUS))
            continue
        record = lookup.record
        if record is None:
            skipped.append(SkippedUser(email=email, reason=REASON_APP_USER_NOT_FOUND))
            continue
        if record.account_state != "enabled":
            skipped.append(SkippedUser(email=email, reason=REASON_ACCOUNT_NOT_ENABLED))
            continue

        galaxy_current, skip_reason = resolve_galaxy_current(
            app_user=record,
            galaxy=galaxy,
            role_function_map=role_function_map,
            metric_translation_map=metric_translation_map,
        )
        if skip_reason is not None:
            skipped.append(SkippedUser(email=email, reason=skip_reason))
            continue
        assert galaxy_current is not None  # skip_reason is None ⇒ 有值（上面已返回）

        if ALL_COMPANIES_KEY in galaxy_current:
            # rc21 修复包 B（opus 审查发现，P1+P2+P3 之 b）：银河此刻能给这个人
            # 的结果命中通配——不论走到这里的是「全非」``scope.all_countries``
            # 那一条路径，还是「角色即全公司」B 口径特例（持有
            # ``ADMIN_FULL_ACCESS_FUNCTION`` 时强制 ``all_companies=True``，
            # 详见 ``publish_row.aggregate_permission`` 模块文档），
            # :func:`translate_company_functions` 对两者的产出都只有
            # :data:`ALL_COMPANIES_KEY` 这一个键——`compute_company_diff` 判定
            # 两种通配形态下差集**恒为空**这件事本身不变（旧表可能给出的任何
            # 具体公司权限视为已被银河覆盖）。这里改变的只是**可见性**：以前
            # 直接调 `compute_company_diff` 拿到空字典、不产出任何 grant 也不
            # 产出任何 skip，人工核对 dry-run 清单时完全看不出"这个用户为什么
            # 一条差集都没有"——是银河真的已经覆盖，还是这个用户在
            # `legacy` 里本来就没有数据？现在显式登记进 skipped 清单，原因码
            # 可分辨（:data:`REASON_WILDCARD_GALAXY_CURRENT`）。
            #
            # **有限通配的差集语义留 #263 裁定**：这里按"银河给了通配就当作
            # 已经覆盖旧表任何具体公司权限"处理，不检查通配那份指标列表是否
            # 真的完整覆盖了旧表这个用户在各公司下的每一项——如果将来产品
            # 需要更精细的"通配下也可能有旧表独有指标"语义，判定逻辑要在
            # #263 里另行设计，本工具现在不做这个推测，一律按恒空差集处理。
            skipped.append(SkippedUser(email=email, reason=REASON_WILDCARD_GALAXY_CURRENT))
            continue

        diff = compute_company_diff(legacy[email], galaxy_current)
        for company_id in sorted(diff):
            for metric_name in diff[company_id]:
                grants.append(
                    PlannedGrant(
                        email=email,
                        user_id=record.user_id,
                        feishu_open_id=record.feishu_open_id,
                        company_id=company_id,
                        metric_name=metric_name,
                    )
                )
    return ImportPlan(grants=tuple(grants), skipped=tuple(skipped))


def load_legacy_export(path: Path) -> dict[str, dict[str, tuple[str, ...]]]:
    """读一份旧表只读导出快照（CSV，模块文档「输入一」一节的格式）。

    失败一律拒绝整个导入（不猜、不跳过单行继续）：一份导出本身形状不对或有
    歧义，指望"尽量解析出能用的部分"只会让差集算出一个看起来正常、实际上
    残缺的结果——比响亮拒绝更危险。
    """

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or not {"email", "permissions"} <= set(reader.fieldnames):
            raise ValueError("旧表导出缺少必需列：email, permissions")
        result: dict[str, dict[str, tuple[str, ...]]] = {}
        for row_number, row in enumerate(reader, start=2):
            email = normalize_email(row.get("email"))
            if not email:
                raise ValueError(f"第 {row_number} 行邮箱为空，导出文件本身不合格")
            if email in result:
                raise ValueError(f"邮箱 {email} 在导出文件中出现多行，无法判定以哪一行为准")
            try:
                permissions = dict(parse_permissions(row.get("permissions")))
            except ValueError as error:
                raise ValueError(f"第 {row_number} 行（{email}）permissions 列解析失败：{error}") from error
            if ALL_COMPANIES_KEY in permissions:
                # rc21 修复包 B（opus 审查发现，P1+P2+P3 之 a）：旧表本身出现
                # ALL_COMPANIES_KEY（"*"）键，整份导出拒绝导入——不是这一行
                # 单独跳过。旧表的"*"是 biai-agent 自己的通配写法，与本工具
                # `resolve_galaxy_current` 判定的"银河侧通配"是完全不同的两件
                # 事：把它当成一个普通 ``company_id="*"`` 写进
                # ``local_permission_override``，会让这个人凭一条本地授权行
                # 跨公司越权（``lookup_metrics`` 的"*"回退制对**任何**没有具体
                # 公司键命中的查询都会命中这一行）。旧表通配用户到底该怎么
                # 平移，不是本工具能替 PM 决定的产品口径，留 #263 由 PM 单裁；
                # 本工具在能确定"这份导出里有一个这种用户"的这一刻就整体拒绝，
                # 不猜、不跳过这一行继续导入其余用户——一份导出里混着一个
                # 通配用户，人工核对时也无法只看 dry-run 清单确认其余行没有
                # 被这条本该拒绝的规则污染。
                raise ValueError(
                    f"第 {row_number} 行（{email}）permissions 使用了旧表通配键"
                    f' "{ALL_COMPANIES_KEY}"：旧表通配用户的平移方式留 #263 由'
                    " PM 单裁，本工具拒绝导入整份导出。"
                )
            # rc25 S-2d（对抗审查 P-2，P1）：**值**同样整份校验，不是只判键。上一段
            # 只挡住了 ``{"*": …}`` 这种通配**键**，``{"1011": ["*"]}`` 这种通配**值**
            # 在此之前是直通的——它落库后，读侧 ``lookup_metrics`` 的 "*" 回退制会
            # 让这一条本地授权等于"该公司全部指标（含未来新增）"，而且此后针对单个
            # 指标的抑制对它无效（抑制按 (公司, 指标) 精确匹配，减不掉一个 "*"）。
            # 一份写错的导入文件因此能把某个人的可见范围悄悄放成"全部"。
            #
            # 校验落在 ``classify_legacy_permissions`` 这一层，**不是**本脚本自己另写
            # 一套：首聊自动路径（``core/identity/legacy_permission_import.py`` →
            # ``legacy_diff.plan_legacy_import``）必经同一个函数，两条路径的取值口径
            # 因此结构上不可能分叉。上一段已经把带 "*" 键的整份拒掉，所以这里能走到
            # 的文档只剩两种结局：``SHAPE_SPECIFIC``（键值都不含 "*"）或
            # ``SHAPE_UNSUPPORTED_WILDCARD``（某个具体公司的值里出现了 "*"）；空白、
            # 换行、零宽等不可见字符与"把 * 混进名字里"（``"日活*"``）由该函数抛
            # ``ValueError``，同样整份拒绝。
            #
            # **只拒值、不拒键**（差异在此显式登记，见 ``legacy_diff`` 模块文档同名
            # 一节）：这里**不**核对公司键在不在翻译映射里——「映射外公司照常导入」是
            # PM 2026-09-02 对 rc25 S-1 的明示裁定（「本地是本地的」，问数 MCP 认识
            # 40–43 这些公司），生产上正在用的首聊路径依赖它。两者后果不同：值决定
            # **给多大范围**，键只决定**给哪一家公司**——写错一个公司号导入的是一条
            # 谁也用不上的死行，不扩大任何人的可见范围。顺手把键一起收紧既推翻已裁
            # 定的产品口径，又挡不住 P-2 真正的那条路。
            try:
                shape = classify_legacy_permissions(permissions)
            except ValueError as error:
                raise ValueError(
                    f"第 {row_number} 行（{email}）permissions 的值不合格（{error}）："
                    "本工具拒绝导入整份导出，未写入任何一行。"
                ) from error
            if shape != SHAPE_SPECIFIC:
                raise ValueError(
                    f"第 {row_number} 行（{email}）permissions 的「值」里出现了通配符"
                    f' "{ALL_COMPANIES_KEY}"（形状 {shape}）：它落库后等于该公司全部'
                    "指标且抑制对它无效，本工具拒绝导入整份导出，未写入任何一行。"
                )
            result[email] = permissions
    return result


# ---------------------------------------------------------------------------
# 二、I/O：数据库读写（真实装配，未接单元测试——由 Epic Full 真库门禁与
#    stage 演练覆盖，与 scripts/import_galaxy_permission_export.py 同一分层）
# ---------------------------------------------------------------------------


def resolve_admin_registry_lookup(dsn: str) -> Any:
    """真实装配：读 ``admin_registry`` 的只读查询对象（``PostgresAdminRegistryLookup``，
    与 gateway 管理命令面用的是同一个类）。

    单独成函数是为了给单元测试一个注入点——测试替换本函数即可用假登记表验证
    :func:`initiated_by_is_registered_admin` 的两种结局，不需要真库。
    """

    from lingxi.adapters.admin_registry import PostgresAdminRegistryLookup

    return PostgresAdminRegistryLookup(dsn)


def initiated_by_is_registered_admin(lookup: Any, open_id: str) -> bool:
    """``--initiated-by`` 给的这个 open_id 是不是一位**已登记的管理员**（rc25 S-2d，
    对抗审查 P-8）。

    判定**复用** :func:`lingxi.core.admin.registry.is_authorized_admin` 这条既有的默认
    拒绝谓词（条目不存在、非 active、或三类角色没有全部授予，一律不是管理员），不在本
    脚本另写一套——管理员身份在本仓库只有一个判据，多一份拷贝就多一处会漂移的口径。
    """

    from lingxi.core.admin.registry import is_authorized_admin

    return is_authorized_admin(lookup.active_entry(open_id=open_id))


def _lookup_app_user_by_email(cursor: Any, email: str) -> UserLookup:
    cursor.execute(
        "SELECT id, employee_no, email, feishu_open_id, display_name, account_state"
        " FROM app_user WHERE lower(btrim(email)) = %s",
        (email,),
    )
    rows = cursor.fetchall()
    if not rows:
        return UserLookup(record=None)
    if len(rows) > 1:
        return UserLookup(record=None, ambiguous=True)
    user_id, employee_no, stored_email, feishu_open_id, display_name, account_state = rows[0]
    return UserLookup(
        record=AppUserRecord(
            user_id=user_id,
            employee_no=employee_no or "",
            email=stored_email or "",
            feishu_open_id=feishu_open_id or "",
            display_name=display_name or "",
            account_state=account_state,
        )
    )


@dataclass
class ApplyReport:
    """真正写入阶段的计数——与 dry-run 共用的 :class:`ImportPlan` 已经把"要不要
    导入"这件事判定完；这一层只回答"落库时这一条是新写入还是已经存在"。"""

    imported: int = 0
    already_present: int = 0


def _existing_active_grant(cursor: Any, *, user_id: str, company_id: str, metric_name: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM local_permission_override"
        " WHERE user_id = %s AND direction = %s AND company_id = %s"
        "   AND metric_name = %s AND entry_status = 'active'",
        (user_id, DIRECTION_GRANT, company_id, metric_name),
    )
    return cursor.fetchone() is not None


def apply_grant(
    dsn: str,
    *,
    grant: PlannedGrant,
    initiated_by_open_id: str,
    now: datetime,
    timeouts: Any = None,
) -> bool:
    """把一条 :class:`PlannedGrant` 落库：**独立开一条连接/事务**（与
    :meth:`~lingxi.adapters.postgres_local_permission.
    PostgresLocalPermissionOverrideStore.insert` 同一惯例——每一笔写入各自
    提交或回滚，一笔失败不牵连其余已经成功导入的行，脚本中途中断只丢失尚未
    处理的部分，已提交的行不需要重跑）。

    先查是否已有同键生效行（幂等的快路径，命中就直接返回，什么都不写）；
    未命中才委托 ``PostgresLocalPermissionOverrideStore.import_legacy_plan``（rc25
    S-1 起与首聊自动路径同一份落库方法）在同一事务里插入合成的 ``pending_action``
    终态行与 ``local_permission_override`` 行（模块文档「确认卡与 pending_action」
    一节）。返回 ``True`` 表示这次真的新写入了一行，``False`` 表示该键已经
    存在——包括这次检查之后、真正插入之前撞上数据库唯一索引这个极小概率窗口
    （该方法用 SAVEPOINT 把它降级成"已存在"，并回收刚合成的 ``pending_action``，
    不留孤儿终态记录）。
    """

    from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, connect
    from lingxi.adapters.postgres_local_permission import PostgresLocalPermissionOverrideStore

    resolved_timeouts = timeouts or DEFAULT_POSTGRES_TIMEOUTS
    with connect(dsn, timeouts=resolved_timeouts) as connection, connection.cursor() as cursor:
        if _existing_active_grant(
            cursor,
            user_id=grant.user_id,
            company_id=grant.company_id,
            metric_name=grant.metric_name,
        ):
            return False
    # rc25 S-1（Issue #540）：落库委托给首聊自动路径同一个方法——合成终态
    # ``pending_action`` + 本地覆盖行同事务，撞索引降级为已存在，全仓库只有一份写法。
    plan = LegacyImportPlan(
        shape=SHAPE_SPECIFIC,
        pairs=((grant.company_id, grant.metric_name),),
        all_scope_metrics=(),
        skipped_reasons=(),
        unmapped_companies_kept=0,
    )
    report = PostgresLocalPermissionOverrideStore(dsn, timeouts=resolved_timeouts).import_legacy_plan(
        user_id=grant.user_id,
        target_open_id=grant.feishu_open_id,
        plan=plan,
        now=now,
        initiated_by_open_id=initiated_by_open_id,
    )
    return report.imported == 1


# ---------------------------------------------------------------------------
# 三、CLI
# ---------------------------------------------------------------------------


def _print_plan(plan: ImportPlan) -> None:
    print(f"计划导入 {len(plan.grants)} 条授权，跳过 {len(plan.skipped)} 个用户：")
    for grant in plan.grants:
        print(f"  + user={grant.email} company={grant.company_id} metric={grant.metric_name}")
    if plan.skipped:
        print("跳过明细（供人工核对，见脚本文档「输入二」一节的跳过原因说明）：")
        for skipped in plan.skipped:
            print(f"  - user={skipped.email} reason={skipped.reason}")


def main(argv: list[str] | None = None) -> int:
    # allow_abbrev=False（rc25 修复包 F5）：与 scripts/ops/preprovision.py 同一条
    # 纪律——`--a`/`--ap` 这类前缀缩写不得被解析成 `--apply`，手滑不等于授权执行。
    parser = argparse.ArgumentParser(
        description="旧表权限差集导入为管理员本地授权（Issue #441）", allow_abbrev=False
    )
    parser.add_argument("legacy_export", type=Path, help="旧表只读导出快照（CSV，见脚本文档「输入一」）")
    parser.add_argument(
        "--initiated-by", required=True, dest="initiated_by_open_id",
        help="本次导入的责任人飞书 open_id，写入每一行的 initiated_by_open_id/decided_by_open_id；"
        "必须是 admin_registry 里一位生效的已登记管理员，否则整次运行拒绝、零写入",
    )
    parser.add_argument("--dsn", default=None, help="PostgreSQL DSN；缺省读 LINGXI_POSTGRES_DSN")
    parser.add_argument(
        "--apply", action="store_true",
        help="真正写入这份差集计划；不传时（默认）只计算并打印计划，不写入任何一行",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="兼容别名：与不传 --apply 时的默认行为等价，只计算并打印计划，不写入任何一行"
        "（与 --apply 同时给出时，按更保守的 --dry-run 处理）",
    )
    parser.add_argument("--role-function-map", type=Path, default=None, help="覆盖随包发布的角色→职能映射文件")
    parser.add_argument("--metric-translation-map", type=Path, default=None, help="覆盖随包发布的公司+职能→指标名映射文件")
    arguments = parser.parse_args(argv)

    dsn = arguments.dsn or os.environ.get("LINGXI_POSTGRES_DSN")
    if not dsn:
        print("缺少 DSN：既未传 --dsn，也未设置环境变量 LINGXI_POSTGRES_DSN。", file=sys.stderr)
        return 2

    # rc25 S-2d（对抗审查 P-8）：``--initiated-by`` 以前是一个自由文本 open_id，谁都能
    # 随手填一个——它却会被逐行写进 initiated_by_open_id/decided_by_open_id，成为审计上
    # "这批授权是谁批的"的唯一答案。这里在**做任何事之前**核对它是不是 admin_registry
    # 里一位生效的已登记管理员：不是就整次运行拒绝，退出码 2、零写入。
    #
    # 闸门放在最前面（连 dry-run 也过这一关）而不是只挡 --apply：一次连责任人都填不对
    # 的运行，它打印出来的计划同样不该被当作"已核对过的计划"拿去 --apply。登记表读不出
    # 来时按 fail-closed 处理——查不到答案不等于答案是"是"。
    initiated_by_open_id = (arguments.initiated_by_open_id or "").strip()
    if not initiated_by_open_id:
        print("--initiated-by 不能为空白，未做任何操作。", file=sys.stderr)
        return 2
    try:
        authorized = initiated_by_is_registered_admin(
            resolve_admin_registry_lookup(dsn), initiated_by_open_id
        )
    except Exception as error:  # noqa: BLE001 - 登记表读不出来一律 fail-closed
        print(f"管理员登记表不可读，未做任何操作：{type(error).__name__}", file=sys.stderr)
        return 2
    if not authorized:
        print(
            "--initiated-by 给出的 open_id 不是一位生效的已登记管理员"
            "（admin_registry 里没有 active 条目，或三类角色没有全部授予），未做任何操作。",
            file=sys.stderr,
        )
        return 2

    try:
        legacy = load_legacy_export(arguments.legacy_export)
    except (OSError, ValueError) as error:
        print(f"旧表导出读取失败，未做任何操作：{error}", file=sys.stderr)
        return 2
    if not legacy:
        print("旧表导出为空（零行），没有可导入的内容。")
        return 0

    from lingxi.adapters.company_function_metric_map_file import load_company_function_metric_map
    from lingxi.adapters.postgres import connect
    from lingxi.adapters.postgres_galaxy_snapshot import PostgresGalaxySnapshotReader
    from lingxi.adapters.role_function_map_file import load_role_function_map

    try:
        role_function_map = load_role_function_map(arguments.role_function_map)
    except (OSError, ValueError) as error:
        print(f"角色职能映射配置不可用，未做任何操作：{type(error).__name__}", file=sys.stderr)
        return 2
    try:
        metric_translation_map = load_company_function_metric_map(arguments.metric_translation_map)
    except (OSError, ValueError) as error:
        print(f"公司+职能→指标名翻译映射配置不可用，未做任何操作：{type(error).__name__}", file=sys.stderr)
        return 2

    galaxy = PostgresGalaxySnapshotReader(dsn).load_current()
    if galaxy is None:
        print("当前没有有效银河批次，无法安全计算差集，未做任何操作。", file=sys.stderr)
        return 3

    # 只读阶段共用一条连接：匹配/差集计算全程不写任何数据，多条并发游标读同一份
    # 数据没有隔离级别顾虑，重复开连接只会白白增加往返。
    with connect(dsn) as connection:
        def lookup_user(email: str) -> UserLookup:
            with connection.cursor() as cursor:
                return _lookup_app_user_by_email(cursor, email)

        plan = plan_import(
            legacy=legacy,
            galaxy=galaxy,
            role_function_map=role_function_map,
            metric_translation_map=metric_translation_map,
            lookup_user=lookup_user,
        )
    _print_plan(plan)

    # 写入极性（rc21 修复包 B，P1+P2+P3 之 c）：默认只出计划，不写入任何一行；
    # 必须显式传 --apply 才真正落库——与改动前"默认写入、要传 --dry-run 才不写"
    # 相反。反转理由：一次导入的默认后果理应是"什么都不发生"，误操作（漏传
    # --dry-run）造成的是"该核对的计划没核对就已经写库"，比"想写却忘了加
    # --apply、只看了一遍计划"危险得多。--dry-run 保留为该默认行为的兼容别名
    # （旧的调用脚本/文档继续可用，不会因为这次改动突然报"未知参数"）；两者
    # 同时给出时按更保守的 --dry-run 处理，不写入。
    if arguments.apply and arguments.dry_run:
        print("同时给出 --apply 与 --dry-run：按更保守的 --dry-run 处理，未写入任何一行。")
        return 0
    if not arguments.apply:
        print("默认只出计划，不写入任何一行；确认无误后加 --apply 真正写入（--dry-run 是该默认行为的兼容别名）。")
        return 0

    print(f"即将写入 {len(plan.grants)} 行。")

    # 写入阶段：每条 PlannedGrant 各自开一条连接/事务（见 apply_grant 文档），
    # 不复用上面的只读连接——避免让整批导入共享同一个长事务、同一把连接级锁。
    now = datetime.now(UTC)
    report = ApplyReport()
    for grant in plan.grants:
        if apply_grant(dsn, grant=grant, initiated_by_open_id=initiated_by_open_id, now=now):
            report.imported += 1
        else:
            report.already_present += 1

    print(f"导入完成：新增 {report.imported} 行，已存在跳过 {report.already_present} 行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
