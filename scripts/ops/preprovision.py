#!/usr/bin/env python3
"""管理员预开通：按名单在用户首聊之前完成开通与预授权（受控运行脚本，Issue #541）。

## 必须在 `lingxi-scheduler` 容器内运行（硬约束，不是建议）

    docker exec -i lingxi-scheduler-1 \\
        env PYTHONPATH=/app/src python3 -B /app/scripts/ops/preprovision.py \\
        /path/to/roster.csv --initiated-by ou_xxx

这条链要签发/采纳问数令牌（需要 MCP 加密主密钥）、写用户环境 `.mcp.json`（需要用户
环境卷）、做在职状态实时回读（需要专用授权主体的派生令牌，**该令牌全系统只允许一个
消费者**）。**这三样只有 `lingxi-scheduler` 进程持有。** 在宿主机上另起一个进程去做
同样的事，正是仓库明令禁止的「共享外部通道第二消费者」形状——2026-08-08 的真实事故
里，一个临时进程静默劫持并烧掉了产品负责人的一次性授权码。本脚本不自己检测运行环境
（容器内外没有可靠且不误伤的判据），这一条靠运行方式保证。

## 产品裁定（产品负责人 2026-09-02，Issue #541）

1. **输入形态＝职位＋公司范围**，与管理卡同构：名单上写的是产品负责人在银河里核对
   时本来就看到的东西，指标名的翻译留在服务端。职位必须精确命中随包发布的角色映射，
   **写错当场拒**，不是静默生效——这正是不采用「邮箱＋指标 JSON」形态的理由。
2. **权限是叠加**：名单给出的权限作为管理员本地补充授权落库，最终范围仍是
   `银河 ∪ 本地 − 抑制`。**不做「用名单压掉银河已给的权限」**——那是一个新的减法
   方向，需要单独裁定，不在本工具范围。
3. **只做批量脚本，不做逐人管理员命令**：名单天生是批量的，把一次批量操作拆成 N 次
   人工点击，每一次半途而废都会在库里留下一批状态不一致的人。
4. **预开通期间静默**：不向名单内用户发送任何消息；他第一次发消息时才补一句
   `onboarding.preprovisioned_first_chat`（由开通链/会话层负责，不是本脚本）。
5. **逐人失败关闭、不阻塞其他人**：报告以本脚本打印的清单为主。审计出口是结构化
   日志（仓库里**没有** `audit_event` 表），逐人可行动的报告只有这份清单——跑完即散，
   请产品负责人自行保存。

## 输入：名单 CSV，三列 `email` / `position` / `company_scope`

```csv
email,position,company_scope
zhang.san@example.com,A国家总经理,1011
li.si@example.com,A国家财务总监,全部
```

- `email`：该员工的邮箱，按 `account_match.normalize_email` 归一（大小写与首尾空白
  不敏感）。定位链是「邮箱 → 花名册人员ID → 组织快照 open_id」，由开通链的系统触发
  入口负责；**同一邮箱在花名册命中多个人员 ID 时整条跳过、不猜**（裁定⑥：实测 1223
  行花名册里 86 组重复邮箱，其中 3 组是真的不同人；开给同邮箱另一个人的权限在同邮箱
  唯一索引加上之后**无法自愈**）。
- `position`：银河职位，必须精确命中 `config/galaxy_role_function_map.toml` 的角色名，
  不做前缀、同义词或模糊匹配。
- `company_scope`：单个公司键，或「全部」/`*`/`all` 表示全部公司。

**整份拒绝（退出码 2、零写入）的四种情形**——它们全是「名单本身写错了」，而不是
「这个人开通失败了」，在任何一行落库之前就能判定，因此不逐人跳过：

1. 表头不是恰好这三列、或某行字段数不对；
2. 任一字段为空白；
3. 同一邮箱出现两行（导出/编辑本身有歧义，不猜哪一行为准，同 #441 姿态）；
4. 任一行的职位或公司范围展开失败（职位不在角色映射、公司范围不是当前可用公司、
   或某公司×职能没配指标）。第 4 条是选择「职位＋公司范围」形态的**全部意义**：
   名单上没有一列会静默出错的自由文本。

## 输出：`--dry-run` 清单与 `--apply`

**写入极性同 #441：默认只出清单、不写入任何一行**；要真正执行必须显式 `--apply`。
`--dry-run` 是该默认行为的兼容别名；两者同时给出按更保守的 `--dry-run` 处理。

`--dry-run` 逐人打印「邮箱 → 职位 → 公司范围 → 将获得的公司×指标条数」与末尾计数，
**零写入**：这一档结构上根本不调用开通入口，不是"调用了但里面没写"。生产名单在
`--apply` 之前必须先跑一次 dry-run 由产品负责人逐行核对——那是「定位错了人」这一层
风险在「同邮箱多命中一律跳过」之外的唯一前置防线，不是可选步骤。

## 每一笔预授权都合成一条终态 `pending_action`

`local_permission_override.pending_action_id` 是结构性 `NOT NULL` 外键（迁移 `0072`：
「没有确认卡不能写入」），非交互式写入**必须**合成一条已终态的 `pending_action`，
否则一行都写不进去。取值：`action_type='local_permission_grant'`、`status='executed'`、
`card_delivered=FALSE`（如实反映从未发过卡片）、
`reason='preprovision_2_0'`（与存量差集导入的 `legacy_import_2_0` 分开，审计一眼分得清
「首聊时按旧表导入」与「首聊前按名单预授权」）。
`initiated_by_open_id`/`decided_by_open_id` 取 `--initiated-by`，**不写死占位身份**：
审计栏目里出现一个无法追溯的假身份，比没有审计更糟。落库口是
`PostgresLocalPermissionOverrideStore.import_position_grant`，一笔的全部行共享同一个
`lpg_` 组 ID，因此管理卡把它渲染成一个职位+范围项、一次事务性整组撤销。

## 与开通链的接口

本脚本**不自己跑开通链**：它解析名单、把每一行冻结成一个
`PositionGrantPlan`，逐人调用开通编排的系统触发入口，然后汇总结果。预授权必须**随链
落库**（在链内的零银河权限判定与权限发布**之前**），理由与存量差集导入挂在零银河判定
之前完全相同：名单本身带了新权限的人不该被判成零权限而整批拒绝。因此本脚本期望的入口
形状是同步执行、返回终态结果：

    start_system(*, email, trace_id, origin="preprovision",
                 initiated_by_open_id: str,
                 preprovision_grant: PositionGrantPlan | None) -> OnboardingResult

`start()` 那种「立刻返回 STARTED、真正的链在线程池里跑」的语义在这里不适用——批量脚本
必须拿到逐人终态才能出清单。
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lingxi.core.permission.account_match import normalize_email
from lingxi.core.permission.position_override import (
    PREPROVISION_PENDING_ACTION_REASON,
    PositionGrantPlan,
    build_preprovision_grant_plan,
    expand_position_scope,
)

#: 贯穿审计与通知抑制判据的来源标记；开通链按它分辨这条链不是真实首聊。
ORIGIN_PREPROVISION = "preprovision"

#: 名单 CSV 的表头，必须逐字相等（多一列少一列都算名单写错）。
ROSTER_COLUMNS = ("email", "position", "company_scope")

#: 逐人结果分类。前两类来自开通链的终态，后两类是本脚本自己的判定。
OUTCOME_PROVISIONED = "provisioned"
OUTCOME_SKIPPED = "skipped"
OUTCOME_FAILED_PREFIX = "failed_"


class RosterError(ValueError):
    """名单本身写错了——整份拒绝，零写入。"""


# ---------------------------------------------------------------------------
# 一、纯逻辑：名单解析 + 展开校验 + 逐人编排（零 I/O，供单元测试直接调用）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RosterRow:
    """名单里的一行：一个人 + 他这次要拿到的职位与公司范围。"""

    email: str
    position_name: str
    company_scope: str


@dataclass(frozen=True)
class PreprovisionItem:
    """一行名单校验通过之后的待办：归一邮箱 + 已冻结的预授权计划。"""

    email: str
    plan: PositionGrantPlan

    @property
    def pair_count(self) -> int:
        return len(self.plan.pairs)


@dataclass(frozen=True)
class PersonOutcome:
    """逐人结局。``reason`` 对失败只记异常类型名，不记异常正文——正文可能带邮箱、
    姓名等人员数据（同 ``apps/scheduler/permission_refresh`` 的既定姿态）。"""

    email: str
    outcome: str
    reason: str | None = None


@dataclass
class PreprovisionReport:
    """一次 ``--apply`` 的逐人结局与计数。"""

    outcomes: list[PersonOutcome] = field(default_factory=list)

    @property
    def provisioned(self) -> int:
        return sum(1 for item in self.outcomes if item.outcome == OUTCOME_PROVISIONED)

    @property
    def skipped(self) -> int:
        return sum(1 for item in self.outcomes if item.outcome == OUTCOME_SKIPPED)

    @property
    def failed(self) -> int:
        return sum(1 for item in self.outcomes if item.outcome.startswith(OUTCOME_FAILED_PREFIX))


def load_roster(path: Path) -> tuple[RosterRow, ...]:
    """读名单 CSV。表头、字段数、空白字段、重复邮箱四类问题一律整份拒绝。

    重复邮箱**按归一后的邮箱**判定：`Zhang.San@Example.com` 与
    `zhang.san@example.com ` 是同一个人的两行，如果按原文比对就会被放过，随后两笔
    预授权落到同一个人身上、而清单看起来像两个人各拿一笔。
    """

    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream)
        try:
            header = next(reader)
        except StopIteration:
            raise RosterError("名单为空：连表头都没有") from None
        columns = tuple(cell.strip() for cell in header)
        if columns != ROSTER_COLUMNS:
            raise RosterError(
                "名单表头必须恰好是 " + ",".join(ROSTER_COLUMNS) + f"，实际是 {','.join(columns)}"
            )
        rows: list[RosterRow] = []
        seen: dict[str, int] = {}
        for number, raw in enumerate(reader, start=2):
            if not raw or all(not cell.strip() for cell in raw):
                continue
            if len(raw) != len(ROSTER_COLUMNS):
                raise RosterError(f"第 {number} 行有 {len(raw)} 个字段，应为 {len(ROSTER_COLUMNS)} 个")
            email, position_name, company_scope = (cell.strip() for cell in raw)
            if not email or not position_name or not company_scope:
                raise RosterError(f"第 {number} 行有空白字段：三列都必须填")
            normalized = normalize_email(email)
            if not normalized:
                raise RosterError(f"第 {number} 行的邮箱归一后为空")
            if normalized in seen:
                raise RosterError(
                    f"第 {number} 行与第 {seen[normalized]} 行是同一个邮箱：名单本身有歧义，"
                    "不猜哪一行为准，整份拒绝"
                )
            seen[normalized] = number
            rows.append(
                RosterRow(email=normalized, position_name=position_name, company_scope=company_scope)
            )
    if not rows:
        raise RosterError("名单没有任何数据行")
    return tuple(rows)


def plan_preprovision(
    rows: Iterable[RosterRow],
    *,
    role_function_map: Mapping[str, str],
    company_function_metric_map: Mapping[str, Mapping[str, Sequence[str]]],
) -> tuple[PreprovisionItem, ...]:
    """把每一行名单展开成冻结的预授权计划；**任何一行展开失败即整份拒绝**。

    展开复用管理卡那条路径的同一个纯函数
    (:func:`~lingxi.core.permission.position_override.expand_position_scope`)，
    不另写一套——职位与公司范围在本仓库只有一个判据，多一份拷贝就多一处会漂移的口径。

    为什么是整份拒绝而不是逐人跳过：这一步发生在任何写入之前，失败的原因全部是「名单
    这一行写错了」。放行其余行等于让一份已知有错的名单产生部分结果，而产品负责人手上
    那份清单会显示"某某被跳过了"——他要做的仍然是改名单重跑。先拒绝、再重跑，比先写
    一半、再补一半安全。
    """

    items: list[PreprovisionItem] = []
    for row in rows:
        try:
            expansion = expand_position_scope(
                position_name=row.position_name,
                company_scope=row.company_scope,
                role_function_map=role_function_map,
                company_function_metric_map=company_function_metric_map,
                available_companies=tuple(
                    key for key in company_function_metric_map if key != "*"
                ),
            )
            plan = build_preprovision_grant_plan(expansion)
        except (ValueError, TypeError, KeyError) as error:
            raise RosterError(
                f"{row.email} 的职位/公司范围无法展开（职位={row.position_name}"
                f" 公司范围={row.company_scope}）：{error}"
            ) from error
        items.append(PreprovisionItem(email=row.email, plan=plan))
    return tuple(items)


def print_plan(items: Sequence[PreprovisionItem]) -> None:
    """dry-run 清单：逐人一行 + 末尾计数。产品负责人按这份清单逐行核对。"""

    print(f"名单 {len(items)} 人，全部通过职位＋公司范围校验：")
    for item in items:
        print(
            f"  + {item.email} 职位={item.plan.position_name}"
            f" 公司范围={item.plan.company_scope} 将获得 {item.pair_count} 条公司×指标"
        )
    print(f"合计将预授权 {sum(item.pair_count for item in items)} 条公司×指标。")


def run_preprovision(
    items: Sequence[PreprovisionItem],
    *,
    start_system: Callable[..., Any],
    initiated_by_open_id: str,
    trace_id_factory: Callable[[], str],
) -> PreprovisionReport:
    """逐人调用开通链的系统触发入口并汇总结果。

    **逐人失败关闭、不阻塞其他人**（裁定⑤）：每个人各自 ``try/except``，任何异常都
    只让这一个人计入 ``failed_<异常类型名>``，其余人照常继续——一次批量预开通里某个人
    的身份定位失败、令牌解密失败或外部写入失败，都不该让名单上其他二十几个人一起没有
    结果。异常正文不记录（可能带邮箱、姓名）。
    """

    report = PreprovisionReport()
    for item in items:
        try:
            result = start_system(
                email=item.email,
                trace_id=trace_id_factory(),
                origin=ORIGIN_PREPROVISION,
                initiated_by_open_id=initiated_by_open_id,
                preprovision_grant=item.plan,
            )
        except Exception as error:  # noqa: BLE001 - 逐人失败关闭，见方法文档
            report.outcomes.append(
                PersonOutcome(
                    email=item.email,
                    outcome=f"{OUTCOME_FAILED_PREFIX}{type(error).__name__}",
                )
            )
            continue
        report.outcomes.append(_classify(item.email, result))
    return report


def _classify(email: str, result: Any) -> PersonOutcome:
    """把开通链的终态结果翻译成清单上的一行。

    读取用 ``getattr`` 而不是解构：开通链的结果类型属于另一张卡的实现面，本脚本只
    依赖 ``state``/``failure_reason`` 两个字段名，多余的字段变化不该让批量脚本报错。
    """

    state = getattr(result, "state", None)
    state_value = getattr(state, "value", state)
    reason = getattr(result, "failure_reason", None)
    if state_value == "completed":
        return PersonOutcome(email=email, outcome=OUTCOME_PROVISIONED)
    return PersonOutcome(
        email=email,
        outcome=OUTCOME_SKIPPED,
        reason=str(reason) if reason else (str(state_value) if state_value else "unknown"),
    )


def print_report(report: PreprovisionReport) -> None:
    """`--apply` 之后的逐人清单与汇总。**跑完即散，请自行保存。**"""

    print("逐人结果：")
    for item in report.outcomes:
        suffix = f" reason={item.reason}" if item.reason else ""
        print(f"  - {item.email} {item.outcome}{suffix}")
    print(
        f"预开通完成：成功 {report.provisioned}、跳过 {report.skipped}、失败 {report.failed}"
        f"（共 {len(report.outcomes)} 人）。"
    )
    print("本清单不落库（仓库没有 audit_event 表，审计出口是结构化日志），请自行保存。")


# ---------------------------------------------------------------------------
# 二、I/O：真实装配（与 scripts/ops/import_local_permission_override.py 同一分层——
#    未接单元测试，由 stage 演练与真库门禁覆盖）
# ---------------------------------------------------------------------------


def resolve_admin_registry_lookup(dsn: str) -> Any:
    """读 ``admin_registry`` 的只读查询对象；单独成函数是给单测一个注入点。"""

    from lingxi.adapters.admin_registry import PostgresAdminRegistryLookup

    return PostgresAdminRegistryLookup(dsn)


def initiated_by_is_registered_admin(lookup: Any, open_id: str) -> bool:
    """``--initiated-by`` 必须是一位生效的已登记管理员。

    判据**复用** :func:`lingxi.core.admin.registry.is_authorized_admin` 这条既有的
    默认拒绝谓词（条目不存在、非 active、三类角色没有全部授予，一律不是管理员），
    与 ``scripts/ops/import_local_permission_override.py`` 同一条：管理员身份在本仓库
    只有一个判据，两个脚本都只是它的调用点。
    """

    from lingxi.core.admin.registry import is_authorized_admin

    return is_authorized_admin(lookup.active_entry(open_id=open_id))


def resolve_start_system(dsn: str) -> Callable[..., Any]:
    """真实装配：拿到开通编排的**系统触发**入口。

    住在 scheduler 的装配层（``apps/scheduler/assembly.py``）——本脚本必须在
    `lingxi-scheduler` 容器内运行，正是因为这条链的密钥、用户环境卷与在职回读令牌
    都只有那个进程持有（见模块文档第一节）。
    """

    from lingxi.apps.scheduler.assembly import build_system_onboarding_entry

    return build_system_onboarding_entry(dsn)


# ---------------------------------------------------------------------------
# 三、CLI
# ---------------------------------------------------------------------------


_CLI_DESCRIPTION = (
    "管理员预开通：按名单在用户首聊之前完成开通与预授权（Issue #541）。"
    " 【硬约束】本脚本必须在 lingxi-scheduler 容器内运行"
    "（docker exec -i lingxi-scheduler-1 env PYTHONPATH=/app/src python3 -B ...）："
    "签发/采纳问数令牌的 MCP 主密钥、用户环境卷、在职状态实时回读所用的专用授权主体"
    "令牌（全系统只允许一个消费者）都只有该进程持有；在宿主机另起进程会与正式入口"
    "抢占同一条外部通道。"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=_CLI_DESCRIPTION)
    parser.add_argument("roster", type=Path, help="名单 CSV，三列 email/position/company_scope")
    parser.add_argument(
        "--initiated-by",
        required=True,
        dest="initiated_by_open_id",
        help="本次预开通的责任人飞书 open_id，写入每一行的 initiated_by_open_id/"
        "decided_by_open_id；必须是 admin_registry 里一位生效的已登记管理员，"
        "否则整次运行拒绝、零写入",
    )
    parser.add_argument("--dsn", default=None, help="PostgreSQL DSN；缺省读 LINGXI_POSTGRES_DSN")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正执行这份名单；不传时（默认）只出清单，不写入任何一行",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="兼容别名：与不传 --apply 时的默认行为等价，只出清单、零写入"
        "（与 --apply 同时给出时，按更保守的 --dry-run 处理）",
    )
    parser.add_argument("--role-function-map", type=Path, default=None, help="覆盖随包发布的角色→职能映射文件")
    parser.add_argument(
        "--company-function-metric-map", type=Path, default=None, help="覆盖随包发布的公司+职能→指标名映射文件"
    )
    arguments = parser.parse_args(argv)

    dsn = arguments.dsn or os.environ.get("LINGXI_POSTGRES_DSN")
    if not dsn:
        print("缺少 DSN：既未传 --dsn，也未设置环境变量 LINGXI_POSTGRES_DSN。", file=sys.stderr)
        return 2

    # 闸门放在最前面（连 dry-run 也过这一关），理由同 #441：一次连责任人都填不对的
    # 运行，它打印出来的清单同样不该被当作"已核对过的名单"拿去 --apply。
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

    from lingxi.adapters.company_function_metric_map_file import load_company_function_metric_map
    from lingxi.adapters.role_function_map_file import load_role_function_map

    try:
        role_function_map = load_role_function_map(arguments.role_function_map)
    except (OSError, ValueError) as error:
        print(f"角色职能映射配置不可用，未做任何操作：{type(error).__name__}", file=sys.stderr)
        return 2
    try:
        company_function_metric_map = load_company_function_metric_map(
            arguments.company_function_metric_map
        )
    except (OSError, ValueError) as error:
        print(f"公司+职能→指标名映射配置不可用，未做任何操作：{type(error).__name__}", file=sys.stderr)
        return 2

    try:
        rows = load_roster(arguments.roster)
        items = plan_preprovision(
            rows,
            role_function_map=role_function_map,
            company_function_metric_map=company_function_metric_map,
        )
    except (OSError, RosterError) as error:
        print(f"名单不可用，未做任何操作：{error}", file=sys.stderr)
        return 2

    print_plan(items)
    print(f"每一笔预授权都会合成一条终态 pending_action（reason={PREPROVISION_PENDING_ACTION_REASON}）。")

    if arguments.apply and arguments.dry_run:
        print("同时给出 --apply 与 --dry-run：按更保守的 --dry-run 处理，未执行任何一人。")
        return 0
    if not arguments.apply:
        print(
            "默认只出清单，不执行任何一人；产品负责人逐行核对无误后加 --apply"
            "（--dry-run 是该默认行为的兼容别名）。"
        )
        return 0

    from lingxi.core.ids import new_id

    report = run_preprovision(
        items,
        start_system=resolve_start_system(dsn),
        initiated_by_open_id=initiated_by_open_id,
        # 追溯号与入站事件那条路径同形（裸 ULID，不带前缀），见 adapters/feishu_events.py。
        trace_id_factory=lambda: new_id("trc").split("_", 1)[1],
    )
    print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
