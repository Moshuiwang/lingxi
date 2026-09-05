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
**整份拒绝（退出码 2、零发送）的五种情形**——它们全是「名单本身写错了」：表头没有
`email` 列；表头归一后有重复列（`email,email` 这种写法里"哪一列才是邮箱"没有答案）；
某一行的字段数与表头列数不等（对不上就不知道哪一格是邮箱）；某行的邮箱为空白；同一
邮箱出现两行（归一后比对，导出/编辑本身有歧义，不猜哪一行为准）。

## 四档运行

- 默认 **dry-run**：逐人打印内容键、姓名、公司范围折叠结果、指标数、是否 active、
  是否已发过，末尾计数。**零发送、零写入**——这一档结构上根本不构造出站口。
- `--precheck --to admin|ou_…`：把同一张卡真发到管理员私聊。`admin` 表示从
  `admin_registry` 的 active 条目里取唯一一位；多于一位时拒绝，要求显式 `--to`。
  收件人必须是一位**生效的已登记管理员**，否则整次运行拒绝。
- `--apply`：按名单向 active 用户发送并写记录。同名单重跑零新增（幂等键在
  `core/outreach/dispatch.outreach_dedupe_key`），重试沿用同一去重键。
- `--list`：回查「发给谁 / 内容键＋版本 / 何时 / 结果」。**正文不打印，也不在库里**。

**两条真发路径必须带 `--initiated-by <责任人 open_id>`**（判据同 `preprovision.py`：
`admin_registry` 里一位生效的已登记管理员，缺失、非 active 或登记表读不出来一律
退出码 2、零发送）。发起人落进每一条审计行的 `initiated_by`，**不进 `outreach_message`
新增列**——它是"这一次运行是谁按下的"，属于运行审计，不是那条消息本身的属性。
dry-run 与 `--list` 不要求它：这两档不发送任何东西。

退出码：`0` 跑完（逐人结果看清单），`2` **什么都没做**（名单、参数、配置或收件人闸门
不合格），`3` **发出去了但收尾没做干净**（卡片飞书已经收下却没能记成已送达，或者这一批
攒下的告警没投递出去）。`3` 与 `2` 必须分开：把"已经发出去了"报成"什么都没做"会让人
原样重跑，而对已经收到卡片的人重跑不是无害的。
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

from lingxi.core.outreach.audience import (
    ACTIVE_PROVISIONING_STATE,
    ENABLED_ACCOUNT_STATE,
    AudiencePlan,
    SubjectFacts,
    plan_outreach,
)
from lingxi.core.outreach.dispatch import (
    OutreachOutcome,
    OutreachPurpose,
    OutreachRecordingError,
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

#: 发送前重读状态没通过的跳过原因。与装配阶段的 `not_active` 分开登记：装配时还是
#: active、真要发的时候不是了，这是两件不同的事，清单上必须能分辨。
SKIP_NOT_ACTIVE_AT_SEND = "not_active_at_send"

#: 卡片已经送达飞书、却没能记成已送达时的逐人状态。它**不是** failed：算成失败会让
#: 人原样重跑，而这个人已经收到卡片了。
STATUS_DELIVERED_NOT_RECORDED = "delivered_not_recorded"


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
    """读名单 CSV，返回归一后的邮箱。表头、字段数或邮箱有一项说不清楚就整份拒绝。

    用 ``csv.reader`` 而不是 ``DictReader``：后者把重复表头折成一个键（后一列静默
    覆盖前一列），又把多出来的字段塞进一个 ``None`` 键，两种形态都让"这一格是不是
    邮箱"变成猜测。逐行字段数与表头列数严格相等，是"按列取值"能成立的前提。
    """
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream))
    if not rows:
        raise RosterError("名单为空：连表头都没有")
    header = rows[0]
    return _normalized_emails(header, rows[1:], _email_column_index(header))


def _email_column_index(header: Sequence[str]) -> int:
    """定位 ``email`` 列；归一后重复的表头整份拒绝。

    归一取「去首尾空白 + casefold」：``Email`` 与 ``email `` 是同一列。两列同名时
    "哪一列为准"没有答案，按后一列取值会把收件人静默换成另一个人。
    """
    columns = [(name or "").strip().casefold() for name in header]
    duplicates = sorted({name for name in columns if columns.count(name) > 1})
    if duplicates:
        shown = "、".join(name or "(空列名)" for name in duplicates)
        raise RosterError(f"名单表头归一后有重复列：{shown}；不猜哪一列为准，整份拒绝")
    if EMAIL_COLUMN not in columns:
        raise RosterError(f"名单表头必须包含 {EMAIL_COLUMN} 列，实际是 {','.join(columns)}")
    return columns.index(EMAIL_COLUMN)


def _normalized_emails(
    header: Sequence[str], rows: Sequence[Sequence[str]], index: int
) -> tuple[str, ...]:
    """逐行取邮箱并归一；字段数、空白与重复任一不合格都整份拒绝。

    重复按**归一后的邮箱**判定：`Zhang.San@Example.com` 与 `zhang.san@example.com `
    是同一个人的两行，按原文比对会被放过，随后同一个人被发两次。
    """
    emails: list[str] = []
    seen: dict[str, int] = {}
    for number, row in enumerate(rows, start=2):
        if not any((cell or "").strip() for cell in row):
            continue
        if len(row) != len(header):
            raise RosterError(
                f"第 {number} 行有 {len(row)} 个字段、表头是 {len(header)} 列："
                "对不上时无法确定哪一格是邮箱，整份拒绝"
            )
        normalized = _normalized_email(row[index], number)
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


def _normalized_email(raw: str, number: int) -> str:
    """一格邮箱的归一；空白与归一后为空都是名单错误，不是一个可以跳过的人。"""
    value = (raw or "").strip()
    if not value:
        raise RosterError(f"第 {number} 行的 {EMAIL_COLUMN} 是空白")
    normalized = normalize_email(value)
    if not normalized:
        raise RosterError(f"第 {number} 行的邮箱归一后为空")
    return normalized


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
    幂等键带上本次运行号**与被预检的那个人**（一次预检多行名单要发出多张卡，只按运行
    号折叠会让第二个人起就被当成"此前已送达"、一张也发不出来）；正式发送发给本人、
    幂等键是 ``user_id``。
    """
    assert recipient.plan.audience is not None  # 调用方已筛掉不可发送的人
    if purpose is OutreachPurpose.PRECHECK:
        if not admin_open_id:
            raise ValueError("预检必须指明收件的管理员")
        return OutreachTarget(
            recipient_open_id=admin_open_id,
            subject=f"{admin_open_id}:{run_id}:{apply_subject(recipient.facts)}",
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
    state_at_send: Callable[[str], tuple[str | None, str | None]] | None = None,
) -> list[PersonResult]:
    """**唯一的发送点**：预检与正式发送共用这一条路径，只差 ``purpose`` 与收件人。

    逐人失败关闭：任何异常只让这一个人计入 ``failed_<异常类型名>``，其余人照常继续。
    异常正文不记录（可能带邮箱、姓名）。给出 ``state_at_send`` 时，正式发送在每个人
    真发之前重读一次他的状态。
    """
    results: list[PersonResult] = []
    for recipient in recipients:
        if recipient.plan.audience is None:
            results.append(
                PersonResult(recipient.plan.email, "skipped", recipient.plan.skip_reason)
            )
            continue
        try:
            stale = _state_gate(recipient, purpose=purpose, state_at_send=state_at_send)
            if stale is not None:
                results.append(PersonResult(recipient.plan.email, "skipped", stale))
                continue
            target = build_target(
                recipient, purpose=purpose, admin_open_id=admin_open_id, run_id=run_id
            )
            outcome = dispatcher.deliver(target, purpose=purpose)
        except OutreachRecordingError as error:
            results.append(
                PersonResult(recipient.plan.email, STATUS_DELIVERED_NOT_RECORDED, error.message_id)
            )
            continue
        except Exception as error:  # noqa: BLE001 - 逐人失败关闭，见方法文档
            results.append(PersonResult(recipient.plan.email, f"failed_{type(error).__name__}"))
            continue
        results.append(_classify(recipient.plan.email, outcome))
    return results


def _state_gate(
    recipient: Recipient,
    *,
    purpose: OutreachPurpose,
    state_at_send: Callable[[str], tuple[str | None, str | None]] | None,
) -> str | None:
    """真发之前重读一次这个人的状态；返回跳过原因，``None`` 表示可以发。

    读名单、装配取值与真正发出去之间隔着一段时间，人在这段时间里可能被停用——发出去
    的卡片不可撤回，因此判据在发送那一刻再取一次。预检不做这道闸：收件人是管理员
    本人。读不出来时上抛，由逐人失败关闭接住：分不清"已停用"与"读不到"时不发送。
    """
    if purpose is not OutreachPurpose.APPLY or state_at_send is None:
        return None
    user_id = recipient.facts.user_id
    if not user_id:
        return SKIP_NOT_ACTIVE_AT_SEND
    provisioning, account = state_at_send(user_id)
    if provisioning == ACTIVE_PROVISIONING_STATE and account == ENABLED_ACCOUNT_STATE:
        return None
    return SKIP_NOT_ACTIVE_AT_SEND


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
    unrecorded = sum(1 for item in results if item.status == STATUS_DELIVERED_NOT_RECORDED)
    print(
        f"{label}完成：送达 {delivered}、此前已送达 {already}、失败 {failed}、"
        f"跳过 {skipped}、已送达但未记账 {unrecorded}（共 {len(results)} 人）。"
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


def resolve_admin_registry_lookup(dsn: str) -> Any:
    """读 ``admin_registry`` 的只读查询对象；单独成函数是给单测一个注入点。"""
    from lingxi.adapters.admin_registry import PostgresAdminRegistryLookup

    return PostgresAdminRegistryLookup(dsn)


def resolve_state_at_send(dsn: str) -> Callable[[str], tuple[str | None, str | None]]:
    """真发前重读状态的查询口；单独成函数是给单测一个注入点。"""
    from lingxi.adapters.postgres_outreach import PostgresOutreachSubjects

    return PostgresOutreachSubjects(dsn).state_for


def initiated_by_is_registered_admin(lookup: Any, open_id: str) -> bool:
    """``--initiated-by`` 必须是一位生效的已登记管理员。

    判据**复用** :func:`lingxi.core.admin.registry.is_authorized_admin` 这条既有的
    默认拒绝谓词（条目不存在、非 active、三类角色没有全部授予，一律不是管理员），
    与 ``scripts/ops/preprovision.py`` 的同名闸逐字一致：管理员身份在本仓库只有
    一个判据，两个脚本都只是它的调用点。
    """
    from lingxi.core.admin.registry import is_authorized_admin

    return is_authorized_admin(lookup.active_entry(open_id=open_id))


class _AuditWithInitiator:
    """给每一条审计行补上发起人。

    ``outreach_message`` **不为此新增列**：发起人是"这一次运行是谁按下的"，属于
    运行审计，不是那条消息本身的属性——把它写进记录表会让同一个人被不同管理员重跑
    时产生两种真相。审计出口是结构化日志（仓库没有 ``audit_event`` 表）。
    """

    def __init__(self, inner: Any, *, initiated_by: str) -> None:
        """包住真实审计出口，记下本次运行的发起人。"""
        self._inner = inner
        self._initiated_by = initiated_by

    def record(self, action: str, /, **fields: object) -> None:
        """转发一条审计，附带 ``initiated_by``。"""
        self._inner.record(action, initiated_by=self._initiated_by, **fields)


def build_dispatcher(config: Any, dsn: str, *, initiated_by: str) -> Any:
    """装出发送编排：真实出站口 + 真实记录口 + 结构化审计 + 既有告警接线。

    告警走 ``build_alerting_duty``（与常驻 scheduler 同一装配），发送失败经
    ``send_outcome_callback`` 进入告警状态机。**只 flush dispatcher、不调
    ``AlertingDuty.run_once()``**：那个方法还会检查心跳，而本脚本是 `docker exec`
    起的第二个进程，替常驻进程判定心跳只会制造假告警。

    内容目录与它的摘要在这里一次读齐：审计里的 ``content_digest`` 必须是**真正拿去
    渲染的那一份内容**的摘要，配了宿主机覆盖文件时它与 ``content_version`` 不再相等，
    追溯"这个人到底收到的是哪一版字"只能看它。
    """
    from lingxi.adapters.feishu_user_card import FeishuUserCards
    from lingxi.adapters.postgres_outreach import PostgresOutreachStore
    from lingxi.apps.scheduler.alerting_assembly import build_alerting_duty
    from lingxi.apps.scheduler.audit import StructuredLogAuditSink
    from lingxi.config.content_override import default_content_source
    from lingxi.core.outreach.dispatch import OutreachDispatcher

    audit = StructuredLogAuditSink()
    # 告警那一侧拿不带发起人的原始出口：告警是系统故障事实，不属于某一次人工发起。
    alerting = build_alerting_duty(config, audit=audit)
    source = default_content_source()
    dispatcher = OutreachDispatcher(
        sender=FeishuUserCards(
            base_url=config.feishu_base_url,
            app_id=config.feishu_app_id,
            app_secret=config.feishu_app_secret,
        ),
        store=PostgresOutreachStore(dsn),
        audit=_AuditWithInitiator(audit, initiated_by=initiated_by),
        send_outcome=alerting.send_outcome_callback(),
        catalog=source.catalog,
        content_digest=source.digest,
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
        "--initiated-by",
        default=None,
        dest="initiated_by_open_id",
        help="本次发送的责任人飞书 open_id，落进每一条审计行；必须是 admin_registry 里"
        "一位生效的已登记管理员，否则整次运行拒绝、零发送。--precheck / --apply 两条"
        "真发路径必填；dry-run 与 --list 不要求",
    )
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
    parser.add_argument(
        "--limit", type=_positive_limit, default=200, help="--list 回查多少条（默认 200）"
    )
    parser.add_argument(
        "--company-function-metric-map",
        type=Path,
        default=None,
        help="覆盖随包发布的公司+职能→指标名映射文件",
    )
    return parser


def _positive_limit(value: str) -> int:
    """``--limit`` 只接受正整数。

    ``0`` 与负数不是"回查零条"，是把参数写错了：让它退化成一次空清单，人会以为库里
    真的没有记录。argparse 的类型校验直接把它收口成退出码 2 的参数错误。
    """
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("--limit 必须是正整数") from error
    if number < 1:
        raise argparse.ArgumentTypeError(f"--limit 必须是正整数，收到 {number}")
    return number


def _reject_conflicting_modes(arguments: argparse.Namespace) -> str | None:
    """互斥档位的守卫；返回拒绝理由，`None` 表示这组开关合法。"""
    chosen = [arguments.apply, arguments.precheck, arguments.list]
    if sum(1 for flag in chosen if flag) > 1:
        return "--apply / --precheck / --list 三档互斥，一次只能选一个。"
    if arguments.precheck and not arguments.to:
        return f"--precheck 必须同时给出 --to（{TO_ADMIN} 或 ou_…）。"
    if arguments.to and not arguments.precheck:
        return "--to 只在 --precheck 时有意义。"
    if (arguments.apply or arguments.precheck) and not (
        arguments.initiated_by_open_id or ""
    ).strip():
        return "--precheck / --apply 必须同时给出 --initiated-by <责任人 open_id>。"
    if not arguments.list and arguments.roster is None:
        return "缺少名单 CSV。"
    return None


def _reject_initiator(dsn: str, initiated_by: str) -> str | None:
    """责任人闸：返回拒绝理由，``None`` 表示这位发起人合格。

    闸放在读名单之前、任何一次发送之前：一次连责任人都填不对的运行，它发出去的
    消息同样不该存在。登记表读不出来一律失败关闭——分辨不出"这个人不是管理员"与
    "库暂时读不到"时，放行的那一侧是不可撤回的。

    入口自己再 ``strip`` 一次：一个纯空白的取值必须在**读库之前**被挡住，靠"连库连
    不上顺带失败"是拿运气当闸。
    """
    initiated_by = (initiated_by or "").strip()
    if not initiated_by:
        return "--initiated-by 不能为空白，未做任何操作。"
    try:
        authorized = initiated_by_is_registered_admin(
            resolve_admin_registry_lookup(dsn), initiated_by
        )
    except Exception as error:  # noqa: BLE001 - 登记表读不出来一律 fail-closed
        return f"管理员登记表不可读，未做任何操作：{type(error).__name__}"
    if not authorized:
        return (
            "--initiated-by 给出的 open_id 不是一位生效的已登记管理员"
            "（admin_registry 里没有 active 条目，或三类角色没有全部授予），未做任何操作。"
        )
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
    arguments: argparse.Namespace,
    config: Any,
    dsn: str,
    recipients: Sequence[Recipient],
    *,
    initiated_by: str,
) -> int:
    """预检或正式发送。两档共用同一条 :func:`run_outreach`。"""
    from lingxi.core.ids import new_id

    purpose = OutreachPurpose.PRECHECK if arguments.precheck else OutreachPurpose.APPLY
    admin_open_id = resolve_admin_open_id(dsn, arguments.to) if arguments.precheck else None
    dispatcher, alerting = build_dispatcher(config, dsn, initiated_by=initiated_by)
    results = run_outreach(
        recipients,
        dispatcher=dispatcher,
        purpose=purpose,
        admin_open_id=admin_open_id,
        run_id=new_id("prk"),
        state_at_send=resolve_state_at_send(dsn),
    )
    # 谁建谁清：把这一批攒下的告警真的投出去，再让本进程退出。
    alert_error = _flush_alerts(alerting)
    print_results(results, purpose=purpose)
    print(
        f"发起人（落进每条审计行的 initiated_by）：{initiated_by}。"
        "本清单不落库（仓库没有 audit_event 表），请自行保存；记录回查用 --list。"
    )
    return _exit_code(results, alert_error=alert_error)


def _flush_alerts(alerting: Any) -> str | None:
    """把这一批攒下的告警真的投出去；返回失败的异常类型名，``None`` 表示投出去了。

    告警投递失败**不改变"卡片已经发出去"这件事**，因此不能让它把整次运行退化成名单
    错误那一档退出码——那会让人以为一条都没发、原样重跑。
    """
    try:
        alerting.dispatcher.run_once()
    except Exception as error:  # noqa: BLE001 - 告警投递失败不改变发送结论
        return type(error).__name__
    return None


def _exit_code(results: Sequence[PersonResult], *, alert_error: str | None) -> int:
    """收口退出码：``0`` 跑完，``3`` 发出去了但收尾没做干净。

    两种收尾不干净都要指名道姓地说清下一步：已送达未记账的人按 ``message_id`` 人工
    核对，告警没投出去要说明发送本身不受影响。
    """
    unrecorded = [item for item in results if item.status == STATUS_DELIVERED_NOT_RECORDED]
    delivered = sum(1 for item in results if item.status == "delivered")
    failed = sum(1 for item in results if item.status.startswith("failed"))
    for item in unrecorded:
        print(
            f"{item.email} 的卡片飞书已经收下（message_id={item.detail or '-'}），"
            "但记账失败：先 --list 核对再重跑，不要当成未发送。",
            file=sys.stderr,
        )
    if alert_error is not None:
        print(
            f"已发送 {delivered} 条 / 失败 {failed} 条；仅告警投递失败：{alert_error}。"
            "发送本身不受影响，不必重跑名单。",
            file=sys.stderr,
        )
    return 3 if unrecorded or alert_error is not None else 0


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

    initiated_by = (arguments.initiated_by_open_id or "").strip()
    if (arguments.apply or arguments.precheck) and (gate := _reject_initiator(dsn, initiated_by)):
        print(gate, file=sys.stderr)
        return 2

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
        return _send(arguments, config, dsn, recipients, initiated_by=initiated_by)
    except RuntimeError as error:  # 收件人闸门不合格：整次运行拒绝，零发送
        print(f"未做任何操作：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
