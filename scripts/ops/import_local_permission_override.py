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

银河侧命中 513 通配管理员（``all_companies=True``）时，视为银河已经覆盖旧表
可能给出的任何具体公司权限，该用户差集恒为空，不导入。

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

``initiated_by_open_id``/``decided_by_open_id`` 取本工具 ``--initiated-by``
参数（运行这次导入的责任人飞书 open_id，通常是产品负责人或受托操作者本人）——
不写死任何值，避免审计栏目显示一个无法追溯的占位身份。

## 用法

```
export LINGXI_POSTGRES_DSN='postgresql://...'
PYTHONPATH=src python3 scripts/ops/import_local_permission_override.py \\
    /path/to/legacy_export.csv --initiated-by ou_xxx --dry-run

# 核对无误后去掉 --dry-run 真正写入
PYTHONPATH=src python3 scripts/ops/import_local_permission_override.py \\
    /path/to/legacy_export.csv --initiated-by ou_xxx
```

``--dsn`` 可覆盖 ``LINGXI_POSTGRES_DSN``；两者都缺失时拒绝运行。凭据只从环境
变量/命令行参数读，不落盘、不进日志（本工具的全部输出只含邮箱、公司ID、指标
名与计数，这些是本次迁移要抽样核对的内容本身，不是凭据）。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from lingxi.core.permission.account_match import MATCHED, match_galaxy_account, normalize_email
from lingxi.core.permission.metric_translation import (
    UncoveredPermissionCombination,
    translate_company_functions,
)
from lingxi.core.permission.publish_row import ALL_COMPANIES_KEY, aggregate_permission, parse_permissions

#: local_permission_override.reason（迁移 0072 列）——每一行导入产生的授权
#: 用这句话标注来源，供管理员在 `/admin user` 回显里一眼分辨。
IMPORT_REASON = "2.0 迁移导入"

#: 合成 pending_action.reason（迁移 0068 列，与上面的 local_permission_override.
#: reason 是两张表的两个不同字段）——标注这是批量导入产生的终态记录，不是一次
#: 真实的管理员确认卡点击。
PENDING_ACTION_REASON = "legacy_import_2_0"

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


def compute_company_diff(
    legacy: Mapping[str, Sequence[str]], galaxy_current: Mapping[str, Sequence[str]]
) -> dict[str, tuple[str, ...]]:
    """按公司键计算「旧表 − 银河当前能给的」差集，减到空的公司键整体丢弃
    （不产出空列表——与 :mod:`lingxi.core.permission.merge_sources`「空结果」
    一节同一姿态）。

    ``galaxy_current`` 出现 :data:`~lingxi.core.permission.publish_row.
    ALL_COMPANIES_KEY`（513 通配管理员）时，视为银河已经覆盖旧表可能给出的
    任何具体公司权限，整份差集恒为空——往通配之外再叠一层具体公司键，是已退役
    的存量沿用机制在通配下会犯的同一个错误（`merge_sources.py` 模块文档
    「通配角」一节），这里从一开始就不让它发生。
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
                result[email] = dict(parse_permissions(row.get("permissions")))
            except ValueError as error:
                raise ValueError(f"第 {row_number} 行（{email}）permissions 列解析失败：{error}") from error
    return result


# ---------------------------------------------------------------------------
# 二、I/O：数据库读写（真实装配，未接单元测试——由 Epic Full 真库门禁与
#    stage 演练覆盖，与 scripts/import_galaxy_permission_export.py 同一分层）
# ---------------------------------------------------------------------------


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
    未命中才依次插入合成的 ``pending_action`` 终态行与
    ``local_permission_override`` 行（模块文档「确认卡与 pending_action」
    一节）。返回 ``True`` 表示这次真的新写入了一行，``False`` 表示该键已经
    存在——包括这次检查之后、真正插入之前撞上数据库唯一索引这个极小概率窗口：
    :class:`~lingxi.adapters.postgres_local_permission.DuplicateActiveOverride`
    从这条连接的事务里冒泡出去时，``with connect(...) as connection`` 按
    psycopg 既定语义整体回滚（与 :meth:`~.insert` 让同一异常直接冒泡出连接
    作用域同一姿态），因此半路失败的这两条 ``INSERT`` 不会留下一个没有对应
    ``local_permission_override`` 行的孤儿 ``pending_action``；本函数在这里
    捕获这同一个异常，只是为了把它降级成"已存在"，不让它中断整批导入的循环。
    """

    from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, connect
    from lingxi.adapters.postgres_local_permission import DuplicateActiveOverride, _insert_locked
    from lingxi.core.ids import new_id
    from lingxi.core.permission.local_override import LocalPermissionOverrideEntry, OverrideDirection

    payload = json.dumps(
        {"company_id": grant.company_id, "metric_name": grant.metric_name, "reason": IMPORT_REASON},
        ensure_ascii=False,
    )
    pending_id = new_id("pac")
    override_id = new_id("lpo")
    confirm_deadline_at = now + timedelta(seconds=1)

    try:
        with connect(dsn, timeouts=timeouts or DEFAULT_POSTGRES_TIMEOUTS) as connection:
            with connection.cursor() as cursor:
                if _existing_active_grant(
                    cursor,
                    user_id=grant.user_id,
                    company_id=grant.company_id,
                    metric_name=grant.metric_name,
                ):
                    return False
                cursor.execute(
                    """
                    INSERT INTO pending_action
                        (id, action_type, target_open_id, target_state_snapshot,
                         initiated_by_open_id, status, card_delivered, reason,
                         created_at, confirm_deadline_at, decided_at, decided_by_open_id,
                         payload)
                    VALUES (%s, %s, %s, %s, %s, 'executed', FALSE, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        pending_id,
                        ACTION_TYPE_GRANT,
                        grant.feishu_open_id,
                        "absent",
                        initiated_by_open_id,
                        PENDING_ACTION_REASON,
                        now,
                        confirm_deadline_at,
                        now,
                        initiated_by_open_id,
                        payload,
                    ),
                )
                entry = LocalPermissionOverrideEntry(
                    user_id=grant.user_id,
                    direction=OverrideDirection.GRANT,
                    company_id=grant.company_id,
                    metric_name=grant.metric_name,
                    reason=IMPORT_REASON,
                    initiated_by_open_id=initiated_by_open_id,
                    pending_action_id=pending_id,
                    created_at=now,
                )
                _insert_locked(cursor, override_id=override_id, entry=entry)
    except DuplicateActiveOverride:
        return False
    return True


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
    parser = argparse.ArgumentParser(description="旧表权限差集导入为管理员本地授权（Issue #441）")
    parser.add_argument("legacy_export", type=Path, help="旧表只读导出快照（CSV，见脚本文档「输入一」）")
    parser.add_argument(
        "--initiated-by", required=True, dest="initiated_by_open_id",
        help="本次导入的责任人飞书 open_id，写入每一行的 initiated_by_open_id/decided_by_open_id",
    )
    parser.add_argument("--dsn", default=None, help="PostgreSQL DSN；缺省读 LINGXI_POSTGRES_DSN")
    parser.add_argument("--dry-run", action="store_true", help="只计算并打印差集清单，不写入任何一行")
    parser.add_argument("--role-function-map", type=Path, default=None, help="覆盖随包发布的角色→职能映射文件")
    parser.add_argument("--metric-translation-map", type=Path, default=None, help="覆盖随包发布的公司+职能→指标名映射文件")
    arguments = parser.parse_args(argv)

    dsn = arguments.dsn or os.environ.get("LINGXI_POSTGRES_DSN")
    if not dsn:
        print("缺少 DSN：既未传 --dsn，也未设置环境变量 LINGXI_POSTGRES_DSN。", file=sys.stderr)
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

    if arguments.dry_run:
        print("--dry-run：以上仅为计划，未写入任何一行。")
        return 0

    # 写入阶段：每条 PlannedGrant 各自开一条连接/事务（见 apply_grant 文档），
    # 不复用上面的只读连接——避免让整批导入共享同一个长事务、同一把连接级锁。
    now = datetime.now(timezone.utc)
    report = ApplyReport()
    for grant in plan.grants:
        if apply_grant(dsn, grant=grant, initiated_by_open_id=arguments.initiated_by_open_id, now=now):
            report.imported += 1
        else:
            report.already_present += 1

    print(f"导入完成：新增 {report.imported} 行，已存在跳过 {report.already_present} 行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
