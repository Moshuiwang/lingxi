"""管理群每日审计日报的正文渲染（纯函数）。

脱敏口径由产品负责人 2026-08-06 拍板为**选项 甲**（[Issue #52]
(https://github.com/Moshuiwang/lingxi/issues/52)）：正文只含内部用户标识 + 发生变化的
**字段名** + 转交标注，**不含任何资料原值、也不含新旧值对**。这不是渲染层的风格偏好，
而是产品合同 :75「管理群通知只展示脱敏摘要和待办标识」的落地；管理员据此到花名册或
管理后台核实。

**人员标识用 `app_user.id`（ULID `usr_*`）完整值**（Issue #52 裁定 C1，验收者定稿
`V-花名册-23`）。三条否定面都是刻意的：

- 不用 :func:`lingxi.core.identity.identifiers.redact_identifier` 的缩短形态（``…(N)``）。
  那个函数的 docstring 写明返回值**不可反查也不可比较**，管理员拿它没法定位到人，
  日报也就失去了「可行动」这一半价值；
- 不写飞书外部标识（花名册人员 ID、`open_id`、`user_id`、`union_id`）原值。
  `core/ids.py` 采用 ULID 的设计意图正是不把外部系统标识带进内部标识，与甲同向；
- 不对 ULID 截断。截断后的前缀会碰撞，「唯一定位」随即失效。

正文里**没有任何可执行入口**（`V-花名册-24`；合同 :75/:85/:237、`V-管理-11`）：没有按钮、
没有链接、没有回调。管理群是通知面，处置一律回到管理 MCP 走确认流程。

渲染结果对同样的输入**逐字节一致**：不取当前时间、不遍历集合、不用随机顺序。
`V-花名册-31` 第③面要求「重启后当日首轮的重发，其载荷与首次逐字段完全一致」，
那条断言最终落在这里。
"""

from __future__ import annotations

from datetime import date

from lingxi.core.identity.roster_audit import (
    ARCHIVED_FIELDS,
    FIELD_LABELS,
    DiffKind,
    PersonDiff,
    RosterAuditReport,
)

REPORT_TITLE = "花名册每日资料比对"

# 转交标注。做成独立后缀而不是混进字段名列表，管理员一眼能挑出来（`V-花名册-21`）。
HANDOVER_MARK = "疑似转交（姓名与邮箱同时变化）"

_SECTION_TITLES = {
    DiffKind.CHANGED: "资料变化",
    DiffKind.REMOVED: "花名册查无此人",
    DiffKind.AMBIGUOUS: "花名册同一人员 ID 存在多行",
}

_SECTION_NOTES = {
    DiffKind.CHANGED: "",
    # 「移除」只上报，不停用、不撤权、不产生待办（`V-花名册-26`）。把这句写进正文，
    # 是为了让管理员不会以为系统已经处理过了。
    DiffKind.REMOVED: "仅提示，本系统未做任何自动处置",
    DiffKind.AMBIGUOUS: "未取任意一行比对，请先在花名册消歧",
}

# 章节输出顺序固定。
_SECTION_ORDER = (DiffKind.CHANGED, DiffKind.REMOVED, DiffKind.AMBIGUOUS)


def _field_label(field: str) -> str:
    return FIELD_LABELS.get(field, field)


def _entry_line(entry: PersonDiff) -> str:
    """一条目一行。行首是完整的内部用户标识，其后只有字段名与标注。"""

    if not entry.changed_fields:
        return f"- {entry.app_user_id}"
    # 按 ARCHIVED_FIELDS 的固定次序渲染，不依赖 changed_fields 的构造顺序。
    labels = "、".join(_field_label(field) for field in ARCHIVED_FIELDS if field in entry.changed_fields)
    line = f"- {entry.app_user_id} 变化字段：{labels}"
    return f"{line}｜{HANDOVER_MARK}" if entry.handover else line


def render_daily_report(report: RosterAuditReport, *, report_date: date) -> str:
    """渲染日报正文。空差异日不该走到这里——那天不发日报，只记审计。"""

    lines = [
        f"{REPORT_TITLE} {report_date.isoformat()}",
        f"比对范围：已开通用户 {report.examined} 人；本次发现 {len(report.entries)} 条需要人工核实。",
        # 措辞不用「原值/新值/旧值」这类词：`V-花名册-22` 对新旧值形态做模式扫描，
        # 一句自我说明的免责话术会把扫描变成必须开豁免的检查，而豁免正是它失效的开始。
        "正文只列内部用户标识与发生变化的字段名，不含姓名、工号、邮箱等任何资料内容；请到花名册或管理后台核实后，在管理 MCP 处置。",
    ]

    for kind in _SECTION_ORDER:
        section = tuple(entry for entry in report.entries if entry.kind is kind)
        if not section:
            continue
        note = _SECTION_NOTES[kind]
        heading = f"{_SECTION_TITLES[kind]} {len(section)} 条"
        lines.append("")
        lines.append(f"{heading}（{note}）" if note else heading)
        lines.extend(_entry_line(entry) for entry in section)

    return "\n".join(lines)
