#!/usr/bin/env python3
"""主动告知：按名单把欢迎卡发给已开通的人（受控运行脚本，Issue #586）。

## 必须在 `lingxi-scheduler` 容器内运行，且**脚本本体经 stdin 喂进去**

`.dockerignore` 排除了 `scripts/`，镜像里没有这个文件——写成容器内路径照抄就会
`No such file`（`preprovision.py` 的 docstring 正是这样写错的，生产 runbook 已把它
登记为自述错误）。正确姿势是脚本不进镜像、经标准输入执行：

    docker compose --env-file deploy/.env.prod \\
      -f deploy/compose.yaml -f deploy/compose.prod.yaml \\
      exec -T scheduler python -B - /tmp/roster.csv --precheck --to ou_xxx \\
      < scripts/ops/outreach.py

名单同样经 stdin 投放到容器的 `/tmp`（只读 rootfs 上 `docker compose cp` 会被拒）；
容器重建会清空 `/tmp`，重建后须重新投放。

与 `scripts/ops/preprovision.py` 同一形态与同一理由：本脚本按
`SchedulerConfig.from_env()` 读它需要的全部东西，容器外那些值一个都不在。逐项列清单：

| 需要什么 | 谁提供 | 不在容器内跑会怎样 |
| --- | --- | --- |
| `LINGXI_POSTGRES_DSN` | scheduler 容器的环境 | `SchedulerConfig.from_env()` 直接 `ValueError`，退出码 2、零发送 |
| 飞书 `app_id`/`app_secret`/`base_url` | 同上 | 换不到应用身份令牌，一张卡也发不出去 |
| 管理群 `chat_id` | 同上 | 发送失败的告警退化成只进日志（`build_alerting_duty` 的既有降级） |
| 随包发布的公司+职能→指标映射 | 镜像内的 `lingxi/config/` | 公司总数说不出来，通配范围的人整批跳过 |

**比 `preprovision.py` 轻**：本脚本不需要 `build_loop`，因此不启动任何线程池、不消费
一次性续期凭据、不碰用户环境卷；它只读库、渲染、发一条消息、写一行记录。

## 产品裁定（Issue #586）

1. **触发入口只有这个脚本**（D-4）：不做群发广播、不做定时推送、不做管理员命令。
2. **预检必须真发**：与正式发送**同一渲染函数、同一数据装配**，通过 Bot-Test 真实
   送达管理员私聊；记录标记为 `precheck`，不算正式送达。静态断言见
   `tests/test_outreach_ops.py`。
3. **只对已开通的人发**：判据是 `provisioning_state=active` 且 `account_state=enabled`
   这两个**状态**，不猜时间；因此经迟到就绪恢复才激活的人在下一次 `--apply` 自然
   被捞到。
4. **首聊「补一句」保留**（D-2）：本脚本不改 `onboarding.preprovisioned_first_chat`
   那条路径，一次性的重复告知是无害的。
5. **逐人失败关闭、不阻塞其他人**：一个人的定位失败或发送失败只让他自己计入失败。

## 输入：名单 CSV，至少一列 `email`

沿用 `preprovision.py` 的名单文件即可（多出来的 `position`/`company_scope` 列被忽略）。
**整份拒绝（退出码 2、零发送）的三种情形**——它们全是「名单本身写错了」：表头没有
`email` 列；某行的邮箱为空白；同一邮箱出现两行（归一后比对，导出/编辑本身有歧义，
不猜哪一行为准）。

## 四档运行

- 默认 **dry-run**：逐人打印内容键、姓名、公司范围折叠结果、指标数、是否 active、
  是否已发过，末尾计数。**零发送、零写入**——这一档结构上根本不构造出站口。
- `--precheck --to admin|ou_…`：把同一张卡真发到管理员私聊。`admin` 表示从
  `admin_registry` 的 active 条目里取唯一一位；多于一位时拒绝，要求显式 `--to`。
  收件人必须是一位**生效的已登记管理员**，否则整次运行拒绝。
- `--apply`：按名单向 active 用户发送并写记录。同名单重跑零新增（幂等键在
  `core/outreach/dispatch.outreach_dedupe_key`），重试沿用同一去重键。
- `--list`：回查「发给谁 / 内容键＋版本 / 何时 / 结果」。**正文不打印，也不在库里**。

退出码：`0` 跑完（逐人结果看清单），`2` 什么都没做（名单、配置或收件人闸门不合格）。
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lingxi.core.outreach.audience import AudiencePlan, SubjectFacts, plan_outreach
from lingxi.core.outreach.dispatch import (
    OutreachOutcome,
    OutreachPurpose,
    OutreachTarget,
    outreach_dedupe_key,
)
from lingxi.core.outreach.welcome_card import WELCOME_CONTENT_KEY
from lingxi.core.permission.account_match import normalize_email

#: 名单里唯一必需的列。其余列（`preprovision.py` 的 position/company_scope）被忽略，
#: 这样同一份名单可以先预开通再主动告知，不必另存一份必然会漂移的副本。
EMAIL_COLUMN = "email"

#: `--to admin` 的字面量：从登记表里取那位管理员，不写死任何 open_id。
TO_ADMIN = "admin"


class RosterError(ValueError):
    """名单本身写错了——整份拒绝，零发送。"""


@dataclass(frozen=True)
class Recipient:
    """一个人：库里读到的事实 + 装配结果。两者一起留着，清单与发送各取所需。"""

    facts: SubjectFacts
    plan: AudiencePlan


@dataclass(frozen=True)
class PersonResult:
    """逐人结局。``detail`` 对失败只记异常类型名，不记异常正文（正文可能带资料值）。"""

    email: str
    status: str
    detail: str | None = None


# ---------------------------------------------------------------------------
# 一、纯逻辑：名单解析 + 装配 + 清单（零 I/O 之外只调注入进来的端口）
# ---------------------------------------------------------------------------


def load_recipients(path: Path) -> tuple[str, ...]:
    """读名单 CSV，返回归一后的邮箱。表头缺列、空白邮箱、重复邮箱一律整份拒绝。

    重复按**归一后的邮箱**判定：`Zhang.San@Example.com` 与 `zhang.san@example.com `
    是同一个人的两行，按原文比对会被放过，随后同一个人被发两次。
    """
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise RosterError("名单为空：连表头都没有")
        columns = [(name or "").strip() for name in reader.fieldnames]
        if EMAIL_COLUMN not in columns:
            raise RosterError(f"名单表头必须包含 {EMAIL_COLUMN} 列，实际是 {','.join(columns)}")
        index = columns.index(EMAIL_COLUMN)
        emails: list[str] = []
        seen: dict[str, int] = {}
        for number, row in enumerate(reader, start=2):
            raw = (row.get(reader.fieldnames[index]) or "").strip()
            if not raw and not any((value or "").strip() for value in row.values()):
                continue
            if not raw:
                raise RosterError(f"第 {number} 行的 {EMAIL_COLUMN} 是空白")
            normalized = normalize_email(raw)
            if not normalized:
                raise RosterError(f"第 {number} 行的邮箱归一后为空")
            if normalized in seen:
                raise RosterError(
                    f"第 {number} 行与第 {seen[normalized]} 行是同一个邮箱：名单本身有歧义，"
                    "不猜哪一行为准，整份拒绝"
                )
            seen[normalized] = number
            emails.append(normalized)
    if not emails:
        raise RosterError("名单没有任何数据行")
    return tuple(emails)


def build_recipients(
    emails: Iterable[str],
    *,
    facts_for: Callable[[str], SubjectFacts],
    company_names: dict[str, str],
    total_company_count: int,
) -> tuple[Recipient, ...]:
    """**唯一的数据装配点**：预检、正式发送与 dry-run 三档都从这里拿取值。

    它是 Issue #586 第四节「同一数据形状」的落点——分出第二个装配等于让预检验证
    的不是将要发出去的那张卡。静态断言见 ``tests/test_outreach_ops.py``。
    """
    recipients: list[Recipient] = []
    for email in emails:
        facts = facts_for(email)
        plan = plan_outreach(
            facts, company_names=company_names, total_company_count=total_company_count
        )
        recipients.append(Recipient(facts=facts, plan=plan))
    return tuple(recipients)


def apply_subject(facts: SubjectFacts) -> str:
    """正式发送的幂等键主体：``user_id``。同名单重跑因此落在同一个键上。"""
    if not facts.user_id:
        raise ValueError("正式发送的幂等键必须绑定用户")
    return facts.user_id


def build_target(
    recipient: Recipient, *, purpose: OutreachPurpose, admin_open_id: str | None, run_id: str
) -> OutreachTarget:
    """把一个装配结果变成一次发送的收件人。

    **两档只差收件人与幂等键主体，取值（``audience``）逐字节相同**：预检发给管理员、
    幂等键带上本次运行号（产品负责人要按样式反复预检，钉死成一次就把定稿路径堵上）；
    正式发送发给本人、幂等键是 ``user_id``。
    """
    assert recipient.plan.audience is not None  # 调用方已筛掉不可发送的人
    if purpose is OutreachPurpose.PRECHECK:
        if not admin_open_id:
            raise ValueError("预检必须指明收件的管理员")
        return OutreachTarget(
            recipient_open_id=admin_open_id,
            subject=f"{admin_open_id}:{run_id}",
            audience=recipient.plan.audience,
        )
    return OutreachTarget(
        recipient_open_id=recipient.facts.open_id or "",
        subject=apply_subject(recipient.facts),
        audience=recipient.plan.audience,
        user_id=recipient.facts.user_id,
    )


def print_plan(recipients: Sequence[Recipient], *, delivered: frozenset[str]) -> None:
    """dry-run 清单：逐人一行 + 末尾计数。产品负责人按这份清单逐行核对。"""
    print(f"名单 {len(recipients)} 人，内容键 {WELCOME_CONTENT_KEY}：")
    for recipient in recipients:
        plan = recipient.plan
        already = "已发过" if _apply_key(recipient) in delivered else "未发过"
        if plan.audience is None:
            print(f"  - {plan.email} 跳过={plan.skip_reason} active={plan.active} {already}")
            continue
        print(
            f"  + {plan.email} 姓名={plan.audience.display_name}"
            f" 公司范围={plan.company_scope} 指标数={plan.metric_count}"
            f" active={plan.active} {already}"
        )
    sendable = sum(1 for item in recipients if item.plan.sendable)
    pending = sum(
        1 for item in recipients if item.plan.sendable and _apply_key(item) not in delivered
    )
    print(
        f"合计：可发送 {sendable} 人、跳过 {len(recipients) - sendable} 人，"
        f"其中尚未发过的 {pending} 人。"
    )


def _apply_key(recipient: Recipient) -> str:
    """这个人在正式发送里的幂等键；定位不到用户时给一个不会命中任何记录的占位。"""
    if not recipient.facts.user_id:
        return ""
    return outreach_dedupe_key(
        content_key=WELCOME_CONTENT_KEY,
        purpose=OutreachPurpose.APPLY,
        subject=recipient.facts.user_id,
    )


def run_outreach(
    recipients: Sequence[Recipient],
    *,
    dispatcher: Any,
    purpose: OutreachPurpose,
    admin_open_id: str | None,
    run_id: str,
) -> list[PersonResult]:
    """**唯一的发送点**：预检与正式发送共用这一条路径，只差 ``purpose`` 与收件人。

    逐人失败关闭：任何异常只让这一个人计入 ``failed_<异常类型名>``，其余人照常继续。
    异常正文不记录（可能带邮箱、姓名）。
    """
    results: list[PersonResult] = []
    for recipient in recipients:
        if recipient.plan.audience is None:
            results.append(
                PersonResult(recipient.plan.email, "skipped", recipient.plan.skip_reason)
            )
            continue
        try:
            target = build_target(
                recipient, purpose=purpose, admin_open_id=admin_open_id, run_id=run_id
            )
            outcome = dispatcher.deliver(target, purpose=purpose)
        except Exception as error:  # noqa: BLE001 - 逐人失败关闭，见方法文档
            results.append(PersonResult(recipient.plan.email, f"failed_{type(error).__name__}"))
            continue
        results.append(_classify(recipient.plan.email, outcome))
    return results


def _classify(email: str, outcome: OutreachOutcome) -> PersonResult:
    """把一次发送的结局翻译成清单上的一行。"""
    if outcome.skipped:
        return PersonResult(email, "already_delivered")
    if outcome.status == "delivered":
        return PersonResult(email, "delivered", outcome.message_id)
    return PersonResult(email, "failed", outcome.error_code)


def print_results(results: Sequence[PersonResult], *, purpose: OutreachPurpose) -> None:
    """发送之后的逐人清单与汇总。**跑完即散，请自行保存。**"""
    label = "预检" if purpose is OutreachPurpose.PRECHECK else "正式发送"
    print(f"{label}逐人结果：")
    for item in results:
        suffix = f" {item.detail}" if item.detail else ""
        print(f"  - {item.email} {item.status}{suffix}")
    delivered = sum(1 for item in results if item.status == "delivered")
    already = sum(1 for item in results if item.status == "already_delivered")
    failed = sum(1 for item in results if item.status.startswith("failed"))
    skipped = sum(1 for item in results if item.status == "skipped")
    print(
        f"{label}完成：送达 {delivered}、此前已送达 {already}、失败 {failed}、"
        f"跳过 {skipped}（共 {len(results)} 人）。"
    )
    if purpose is OutreachPurpose.PRECHECK:
        print("预检记录标记为 precheck，不算正式送达；正式发送仍需 --apply。")


def print_records(records: Sequence[Any]) -> None:
    """`--list` 的回查清单：发给谁 / 内容键＋版本 / 何时 / 结果。正文不在其中。"""
    if not records:
        print("没有任何主动发送记录。")
        return
    print(f"最近 {len(records)} 条主动发送记录（不含正文）：")
    for record in records:
        moment = record.delivered_at or record.created_at
        detail = f" error={record.last_error}" if record.last_error else ""
        print(
            f"  - {record.recipient_open_id} {record.purpose} {record.content_key}"
            f"@{record.content_version} 样式={record.card_style} {record.status}"
            f" 尝试={record.attempts} 时间={moment.isoformat()}"
            f" message_id={record.message_id or '-'}{detail}"
        )


# ---------------------------------------------------------------------------
# 二、I/O：真实装配（与 scripts/ops/preprovision.py 同一分层——未接单元测试，
#    由 stage 演练与真库门禁覆盖）
# ---------------------------------------------------------------------------


def resolve_scope_catalog(dsn: str, metric_map_path: Path | None) -> tuple[dict[str, str], int]:
    """一次读齐「公司编号 → 中文名」与公司总数。

    公司总数取随包/外置映射里非通配的键数，**不是**某个人权限文档里的键数：欢迎卡
    在通配范围下要说「全部公司（N 家）」，N 是这个系统当前能查的公司数。名字批量取
    一次，不随名单人数线性增长连接数。
    """
    from lingxi.adapters.company_function_metric_map_file import load_company_function_metric_map
    from lingxi.adapters.postgres_galaxy_snapshot import PostgresCompanyNames

    mapping = load_company_function_metric_map(metric_map_path)
    available = tuple(key for key in mapping if key != "*")
    if not available:
        raise RuntimeError("公司+职能→指标名映射里没有任何公司，无法说清楚公司范围")
    resolved = PostgresCompanyNames(dsn).names_for(company_ids=list(available))
    names = {key: value for key, value in resolved.items() if value}
    return names, len(available)


def resolve_admin_open_id(dsn: str, requested: str) -> str:
    """决定预检卡发给谁；**必须是一位生效的已登记管理员**，否则整次运行拒绝。

    判据复用 :func:`lingxi.core.admin.registry.is_authorized_admin` 这条既有的默认
    拒绝谓词（条目不存在、非 active、三类角色没有全部授予，一律不是管理员），与
    `scripts/ops/preprovision.py` 的 `--initiated-by` 闸同一条：管理员身份在本仓库
    只有一个判据。``admin`` 字面量表示从登记表里取，多于一位时**不猜**。
    """
    from lingxi.adapters.admin_registry import PostgresAdminRegistryLookup
    from lingxi.core.admin.registry import is_authorized_admin

    lookup = PostgresAdminRegistryLookup(dsn)
    if requested == TO_ADMIN:
        authorized = [entry for entry in lookup.active_entries() if is_authorized_admin(entry)]
        if len(authorized) != 1:
            raise RuntimeError(
                f"登记表里有 {len(authorized)} 位生效管理员（需要恰好 1 位才能省略收件人），"
                "请用 --to ou_… 显式指定"
            )
        return authorized[0].feishu_open_id
    if not is_authorized_admin(lookup.active_entry(open_id=requested)):
        raise RuntimeError("--to 给出的 open_id 不是一位生效的已登记管理员")
    return requested


def build_dispatcher(config: Any, dsn: str) -> Any:
    """装出发送编排：真实出站口 + 真实记录口 + 结构化审计 + 既有告警接线。

    告警走 ``build_alerting_duty``（与常驻 scheduler 同一装配），发送失败经
    ``send_outcome_callback`` 进入告警状态机。**只 flush dispatcher、不调
    ``AlertingDuty.run_once()``**：那个方法还会检查心跳，而本脚本是 `docker exec`
    起的第二个进程，替常驻进程判定心跳只会制造假告警。
    """
    from lingxi.adapters.feishu_user_card import FeishuUserCards
    from lingxi.adapters.postgres_outreach import PostgresOutreachStore
    from lingxi.apps.scheduler.alerting_assembly import build_alerting_duty
    from lingxi.apps.scheduler.audit import StructuredLogAuditSink
    from lingxi.core.outreach.dispatch import OutreachDispatcher

    audit = StructuredLogAuditSink()
    alerting = build_alerting_duty(config, audit=audit)
    dispatcher = OutreachDispatcher(
        sender=FeishuUserCards(
            base_url=config.feishu_base_url,
            app_id=config.feishu_app_id,
            app_secret=config.feishu_app_secret,
        ),
        store=PostgresOutreachStore(dsn),
        audit=audit,
        send_outcome=alerting.send_outcome_callback(),
    )
    return dispatcher, alerting


# ---------------------------------------------------------------------------
# 三、CLI
# ---------------------------------------------------------------------------


_CLI_DESCRIPTION = (
    "主动告知：按名单把欢迎卡发给已开通的人（Issue #586）。"
    " 【硬约束】本脚本必须在 lingxi-scheduler 容器内运行，且脚本本体经 stdin 喂进去"
    "（... exec -T scheduler python -B - <名单路径> ... < scripts/ops/outreach.py）："
    "镜像里没有 scripts/ 目录，数据库连接串、飞书应用凭据、管理群与随包映射只有该进程持有。"
    " 默认只出清单、零发送；真发必须显式 --apply，预检必须显式 --precheck --to。"
)


def _build_parser() -> argparse.ArgumentParser:
    # allow_abbrev=False：argparse 默认接受前缀缩写，`--a` 会被解析成 `--apply`——
    # 对一个"传了就真发消息、且消息不可撤回"的开关，手滑半个词不能等于授权执行。
    parser = argparse.ArgumentParser(description=_CLI_DESCRIPTION, allow_abbrev=False)
    parser.add_argument(
        "roster", type=Path, nargs="?", help="名单 CSV，至少一列 email；--list 时不需要"
    )
    parser.add_argument("--dsn", default=None, help="PostgreSQL DSN；缺省读 LINGXI_POSTGRES_DSN")
    parser.add_argument(
        "--apply", action="store_true", help="真正按名单发送；不传时（默认）只出清单、零发送"
    )
    parser.add_argument(
        "--precheck",
        action="store_true",
        help="把同一张卡真发到管理员私聊做预检；记录标记为 precheck，不算正式送达",
    )
    parser.add_argument(
        "--to",
        default=None,
        help=f"预检收件人：{TO_ADMIN}（从登记表取唯一一位生效管理员）或显式 ou_…",
    )
    parser.add_argument("--list", action="store_true", help="回查发送记录，不发送任何东西")
    parser.add_argument("--limit", type=int, default=200, help="--list 回查多少条（默认 200）")
    parser.add_argument(
        "--company-function-metric-map",
        type=Path,
        default=None,
        help="覆盖随包发布的公司+职能→指标名映射文件",
    )
    return parser


def _reject_conflicting_modes(arguments: argparse.Namespace) -> str | None:
    """互斥档位的守卫；返回拒绝理由，`None` 表示这组开关合法。"""
    chosen = [arguments.apply, arguments.precheck, arguments.list]
    if sum(1 for flag in chosen if flag) > 1:
        return "--apply / --precheck / --list 三档互斥，一次只能选一个。"
    if arguments.precheck and not arguments.to:
        return f"--precheck 必须同时给出 --to（{TO_ADMIN} 或 ou_…）。"
    if arguments.to and not arguments.precheck:
        return "--to 只在 --precheck 时有意义。"
    if not arguments.list and arguments.roster is None:
        return "缺少名单 CSV。"
    return None


def _run_listing(dsn: str, limit: int) -> int:
    from lingxi.adapters.postgres_outreach import PostgresOutreachStore

    print_records(PostgresOutreachStore(dsn).recent_records(limit=limit))
    return 0


def _prepare(arguments: argparse.Namespace, dsn: str) -> tuple[Any, tuple[Recipient, ...]]:
    """读名单、读配置、装配取值。任何一步失败都抛异常，由 :func:`main` 收口成退出码 2。"""
    from lingxi.adapters.postgres_outreach import PostgresOutreachSubjects
    from lingxi.apps.scheduler.config import SchedulerConfig

    config = SchedulerConfig.from_env()
    if str(config.postgres_dsn) != dsn:
        raise RuntimeError(
            "--dsn/LINGXI_POSTGRES_DSN 与 scheduler 配置读到的数据库不是同一个："
            "会出现「在 A 库判定谁该收到、把记录写进 B 库」，拒绝运行"
        )
    metric_map_path = arguments.company_function_metric_map or config.metric_map_path
    company_names, total_company_count = resolve_scope_catalog(dsn, metric_map_path)
    recipients = build_recipients(
        load_recipients(arguments.roster),
        facts_for=PostgresOutreachSubjects(dsn).facts_for,
        company_names=company_names,
        total_company_count=total_company_count,
    )
    return config, recipients


def _send(
    arguments: argparse.Namespace, config: Any, dsn: str, recipients: Sequence[Recipient]
) -> int:
    """预检或正式发送。两档共用同一条 :func:`run_outreach`。"""
    from lingxi.core.ids import new_id

    purpose = OutreachPurpose.PRECHECK if arguments.precheck else OutreachPurpose.APPLY
    admin_open_id = resolve_admin_open_id(dsn, arguments.to) if arguments.precheck else None
    dispatcher, alerting = build_dispatcher(config, dsn)
    results = run_outreach(
        recipients,
        dispatcher=dispatcher,
        purpose=purpose,
        admin_open_id=admin_open_id,
        run_id=new_id("prk"),
    )
    # 谁建谁清：把这一批攒下的告警真的投出去，再让本进程退出。
    alerting.dispatcher.run_once()
    print_results(results, purpose=purpose)
    print("本清单不落库（仓库没有 audit_event 表），请自行保存；记录回查用 --list。")
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    rejection = _reject_conflicting_modes(arguments)
    if rejection is not None:
        print(rejection, file=sys.stderr)
        return 2

    dsn = arguments.dsn or os.environ.get("LINGXI_POSTGRES_DSN")
    if not dsn:
        print("缺少 DSN：既未传 --dsn，也未设置环境变量 LINGXI_POSTGRES_DSN。", file=sys.stderr)
        return 2

    if arguments.list:
        return _run_listing(dsn, arguments.limit)

    try:
        config, recipients = _prepare(arguments, dsn)
    except (OSError, RosterError, ValueError, RuntimeError) as error:
        print(f"未做任何操作：{type(error).__name__}: {error}", file=sys.stderr)
        return 2

    if not arguments.apply and not arguments.precheck:
        from lingxi.adapters.postgres_outreach import PostgresOutreachStore

        keys = tuple(key for key in (_apply_key(item) for item in recipients) if key)
        print_plan(recipients, delivered=PostgresOutreachStore(dsn).delivered_dedupe_keys(keys))
        print(
            "默认只出清单、不发送任何人；产品负责人逐行核对无误后先 --precheck --to 看真机"
            "渲染，再 --apply。"
        )
        return 0

    try:
        return _send(arguments, config, dsn, recipients)
    except RuntimeError as error:  # 收件人闸门不合格：整次运行拒绝，零发送
        print(f"未做任何操作：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
