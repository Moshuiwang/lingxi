#!/usr/bin/env python3
"""管理员预开通：按名单在用户首聊之前完成开通与预授权（受控运行脚本，Issue #541）。

## 必须在 `lingxi-scheduler` 容器内运行（硬约束，不是建议）

    docker exec -i lingxi-scheduler-1 \\
        env PYTHONPATH=/app/src python3 -B /app/scripts/ops/preprovision.py \\
        /path/to/roster.csv --initiated-by ou_xxx

本脚本按 `SchedulerConfig.from_env()` + `build_loop(...)` **原样重建一遍 scheduler
启动时那条装配**（见 :func:`resolve_start_system`），因此它要的东西与常驻 scheduler
一字不差。逐项列清单，不只给结论：

| 需要什么 | 谁提供 | 不在容器内跑会怎样 |
| --- | --- | --- |
| `LINGXI_MCP_TOKEN_KEY`（问数令牌加解密主密钥） | scheduler 容器的 secret 注入 | 存量令牌解不开 → `stock_token_decrypt_failed` 整条链失败关闭；新签的令牌用户环境写不出来 |
| 用户环境卷（写 `<user_env_root>/<user>/.mcp.json`） | scheduler 容器的挂载 | 路径不存在 → 建档之后卡在环境创建，人停在 `provisioning` |
| 凭据文件 + `LINGXI_CREDENTIAL_KEY`（在职状态实时回读所需的专用主体派生令牌） | scheduler 容器挂的持久凭据路径 | 拿不到在职状态 → 身份定位失败关闭，一个人也开不出来 |
| `LINGXI_POSTGRES_DSN`、飞书 app_id/secret、管理群 chat_id 等 | scheduler 容器的环境 | `SchedulerConfig.from_env()` 直接 `ValueError`，脚本退出码 2、零写入 |
| 随包发布的两份映射（角色→职能、公司+职能→指标） | 镜像内的 `lingxi/config/` | 与容器内常驻进程读的是同一份，不会出现"名单按 A 版映射核对、链按 B 版展开" |

**关于「一次性 refresh_token 全系统只允许一个消费者」这条红线**：`docker exec` 起的
是同一容器内的**第二个进程**，它有自己的 `DerivedAccessTokenHolder`（进程内、重启即
空），所以在职回读第一次取令牌时会走
`CredentialRotationLoop.refresh_for_supply()` 真的消费一次续期。这**不是** 2026-08-08
那个事故形状：那次是两个客户端抢同一条 OAuth Bridge 长连接，后来者静默踢掉先来者；
而这里的频率上界由 `HostFileDelegatedCredentialVault.claim_due()` 在**凭据文件自己的
文件锁内**判定，该方法文档写明「进程重启、崩溃重启循环、**同一宿主机上的第二个实例**
都绕不过它」。**已知代价（如实登记，不是零成本）**：本脚本跑一批可能占用当天续期预算
里的一次，常驻 scheduler 的日报侧那一次因此可能被推迟到下一个窗口。批量预开通不是
高频操作，这个代价可接受；但它是真的，不要在文档里说成"没有影响"。

本脚本**不自己检测运行环境**（容器内外没有可靠且不误伤的判据），这一条靠运行方式保证；
上表里任何一项缺失的表现都是**失败关闭**，不会静默半开。

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

本脚本**不自己跑开通链、也不自己 new 一个编排**：它解析名单、把每一行冻结成一个
`PositionGrantPlan`，逐人调用 `AutoOnboardingRunner.start_system(...)`，然后汇总结果。
拿到那个编排的**唯一受支持方式**是 `resolve_start_system()` —— 走 scheduler 真实启动
路径、从 `duty.onboarding_runner` 取句柄，见该函数文档。

    start_system(*, email, trace_id, origin="preprovision",
                 initiated_by_open_id: str,
                 preprovision_grant: PositionGrantPlan | None) -> OnboardingResult

- **同步返回终态**（不是 `start()` 那种「立刻返回 STARTED、链在线程池里跑」）：批量
  脚本必须拿到逐人终态才出得了清单。
- **预授权随链落库**，落在链内零银河权限判定与权限发布**之前**（与存量差集导入同一
  挂点）：名单本身带了新权限的人不该被判成零权限而整批拒绝。
- `origin` 只接受 `"preprovision"`，`initiated_by_open_id` 不接受空白——两条都是链侧
  的失败关闭判据，本脚本在 CLI 闸就先挡一次，不把明知会被拒的输入送进去。
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

from lingxi.core.identity.preprovision import (
    ORIGIN_PREPROVISION as _CHAIN_ORIGIN_PREPROVISION,
)
from lingxi.core.permission.account_match import normalize_email
from lingxi.core.permission.position_override import (
    PREPROVISION_PENDING_ACTION_REASON,
    PositionGrantPlan,
    build_preprovision_grant_plan,
    expand_position_scope,
)

#: 贯穿审计与通知抑制判据的来源标记。**从开通链现读，不在脚本里另立一个同值字面量**：
#: `core/identity/preprovision.run_system_onboarding` 对它是**失败关闭**的（不是
#: `ORIGIN_PREPROVISION` 就 `ValueError`），而它同时决定合成事件标识的前缀，也就是
#: 「要不要静默、要不要记账」的判据——两处各写一份字面量，漂移的后果是整批预开通当场
#: 报错，或更糟：变成一条"不静默但也没有账本"的第三形态。
ORIGIN_PREPROVISION = _CHAIN_ORIGIN_PREPROVISION

#: 名单 CSV 的表头，必须逐字相等（多一列少一列都算名单写错）。
ROSTER_COLUMNS = ("email", "position", "company_scope")

#: 逐人结果分类。前两类来自开通链的终态，后两类是本脚本自己的判定。
OUTCOME_PROVISIONED = "provisioned"
OUTCOME_SKIPPED = "skipped"
OUTCOME_FAILED_PREFIX = "failed_"
#: rc25 修复包 F2：开通链在续行前复核发现这个人**已经 active**、提前收口，名单答应
#: 的那笔预授权**没有落库**（落库口排在复核之后）。终态虽是 completed，但把它计成
#: "成功预开通"就是把「权限没给」报成「都办妥了」——单独归类、不进 provisioned。
#: 要不要给已 active 用户补上名单权限是产品语义，等产品负责人裁定后另行处理。
OUTCOME_ALREADY_ACTIVE_GRANT_NOT_APPLIED = "already_active_grant_not_applied"


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

    @property
    def grant_not_applied(self) -> int:
        return sum(
            1
            for item in self.outcomes
            if item.outcome == OUTCOME_ALREADY_ACTIVE_GRANT_NOT_APPLIED
        )


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
        if getattr(result, "grant_not_applied", False):
            # rc25 修复包 F2：已 active、名单授权没落——不计成功，醒目单列
            # （见 OUTCOME_ALREADY_ACTIVE_GRANT_NOT_APPLIED 旁注）。
            return PersonOutcome(
                email=email,
                outcome=OUTCOME_ALREADY_ACTIVE_GRANT_NOT_APPLIED,
                reason="user_already_active",
            )
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
        marker = "  ! " if item.outcome == OUTCOME_ALREADY_ACTIVE_GRANT_NOT_APPLIED else "  - "
        print(f"{marker}{item.email} {item.outcome}{suffix}")
    print(
        f"预开通完成：成功 {report.provisioned}、跳过 {report.skipped}、失败 {report.failed}"
        f"（共 {len(report.outcomes)} 人）。"
    )
    if report.grant_not_applied:
        print(
            f"注意：{report.grant_not_applied} 人已是 active、名单预授权未应用"
            f"（{OUTCOME_ALREADY_ACTIVE_GRANT_NOT_APPLIED}）——不计入成功；"
            "是否给已 active 用户补授权待产品负责人裁定，本脚本不静默扩权。"
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


def resolve_start_system(dsn: str) -> tuple[Callable[..., Any], Callable[[], None]]:
    """真实装配：走 **scheduler 启动时那条一模一样的路径**建出编排，返回
    ``(start_system, 收尾)``。

    **不另写一套简化版装配。** 这里逐行照抄 ``apps/scheduler/__init__.py::main()``
    的前半段（``SchedulerConfig.from_env()`` → ``build_alerting_duty`` →
    ``build_loop``），然后从 ``loop.duties`` 里取那个被断言过的公开句柄
    ``duty.onboarding_runner``（``apps/scheduler/onboarding.py`` 结尾把它挂上去，
    ``tests/test_scheduler_onboarding_assembly.py::test_a_wired_duty_exposes_the_
    onboarding_runner_for_the_preprovision_entry`` 钉住）。理由是这条链的**装配
    不变量**远不止一个 DSN：发布闸与 ``metric_translation_map`` 必须与每日重算共用
    同一个对象、X-1 同邮箱回读口必填（漏接是构造期 ``TypeError``）、存量令牌源与
    差集导入口必须成对、内测名单闸、在职回读的令牌供给……自己 ``new`` 一个
    ``AutoOnboardingRunner``，或者做一个"只吃 dsn"的简化入口，等于在装配层再开一个
    「参数不全也能起来」的口子，而那正是本批在堵的那类缺口。

    **两处刻意与 main() 不同，都不是简化，是必须**：

    1. **不传 ``heartbeat``。** ``main()`` 传的是 ``_combined_heartbeat(...)``，它会
       ``touch_liveness("scheduler")`` —— 同容器内 ``python -m lingxi.apps.healthcheck``
       正是靠这个文件判断常驻主循环还在不在跳。本脚本是 ``docker exec`` 起的第二个
       进程；让它去戳那个文件，等于在常驻 scheduler 已经死掉时替它伪造心跳。
    2. **不 ``run_forever()``、不装信号处理。** 只需要编排这一个句柄；其余职责
       （凭据轮换、清理、重算、发布、快照同步……）**一次都不会 tick**，因为从头到尾
       没有人调用 ``loop.run_once()``。

    ``build_loop`` 唯一有副作用的一步是 ``_build_onboarding_duty`` 里的
    ``executor.start()``（开通执行器的线程池）。返回的收尾函数按 ``main()`` 的
    ``finally`` 同一姿态 ``request_stop()`` + ``join_onboarding_executors(...)``，
    调用方**必须**在 ``finally`` 里调它——谁建谁清。

    ``dsn`` 只用来核对一件事：``--initiated-by`` 的管理员闸查的那个库，与开通链
    实际要写的库是不是同一个。不一致就整次运行拒绝——否则会出现"在 A 库确认了责任人、
    把授权写进 B 库"这种审计上无法解释的组合。
    """

    from lingxi.apps.scheduler.alerting_assembly import build_alerting_duty
    from lingxi.apps.scheduler.assembly import build_loop
    from lingxi.apps.scheduler.audit import StructuredLogAuditSink
    from lingxi.apps.scheduler.config import SchedulerConfig
    from lingxi.apps.scheduler.onboarding import join_onboarding_executors

    config = SchedulerConfig.from_env()
    if str(config.postgres_dsn) != dsn:
        raise RuntimeError(
            "--dsn/LINGXI_POSTGRES_DSN 与 scheduler 配置读到的数据库不是同一个："
            "管理员闸与开通链会落在两个库上，拒绝运行"
        )
    alerting_duty = build_alerting_duty(config, audit=StructuredLogAuditSink())
    loop = build_loop(config, alerting_duty=alerting_duty)

    runners = [
        runner
        for duty in loop.duties
        if (runner := getattr(duty, "onboarding_runner", None)) is not None
    ]
    if len(runners) != 1:
        # 零个＝前置不齐，`_build_onboarding_duty` 整条职责没装配（发布闸没接、
        # 翻译映射不可用、令牌供给缺失……原因它自己已经留过审计）。多于一个＝装配层
        # 变了形状，这里不猜该用哪一个。两种都失败关闭，不退回一个半成品编排。
        raise RuntimeError(
            f"scheduler 装配里的首次开通编排句柄有 {len(runners)} 个（应为 1）："
            "前置不齐时整条职责不装配，此时不能预开通任何人"
        )

    def shutdown() -> None:
        loop.request_stop()
        join_onboarding_executors(loop.duties)

    return runners[0].start_system, shutdown


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
    # allow_abbrev=False（rc25 修复包 F5）：argparse 默认接受前缀缩写，`--a` 会被
    # 解析成 `--apply`——对一个"传了就真写库"的开关，手滑半个词不能等于授权执行。
    # 本项目历史上被外部审查抓到过 `--e` 缩写即触发真实执行的同型缺陷。
    parser = argparse.ArgumentParser(description=_CLI_DESCRIPTION, allow_abbrev=False)
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

    try:
        start_system, shutdown = resolve_start_system(dsn)
    except Exception as error:  # noqa: BLE001 - 装配起不来一律 fail-closed
        print(f"开通编排装配失败，未执行任何一人：{type(error).__name__}: {error}", file=sys.stderr)
        return 2

    # 谁建谁清：`build_loop` 已经 `start()` 了开通执行器的线程池，无论这一批跑成什么
    # 样都必须停掉并等它收工（与 `apps/scheduler/__init__.py::main()` 的 finally 同姿态）。
    try:
        report = run_preprovision(
            items,
            start_system=start_system,
            initiated_by_open_id=initiated_by_open_id,
            # 追溯号与入站事件那条路径同形（裸 ULID，不带前缀），见 adapters/feishu_events.py。
            trace_id_factory=lambda: new_id("trc").split("_", 1)[1],
        )
    finally:
        shutdown()
    print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
