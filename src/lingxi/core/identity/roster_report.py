"""管理群每日审计日报的正文渲染（纯函数）。

展示口径最初由产品负责人 2026-08-06 拍板为**选项 甲**（[Issue #52]
(https://github.com/Moshuiwang/lingxi/issues/52)）：正文只含内部用户标识 + 发生变化的
**字段名** + 转交标注，**不含任何资料原值、也不含新旧值对**。本模块当前渲染的就是这个
形态。

**口径变更登记（2026-08-08，产品负责人 D2 裁定）**：上面这条**已被覆盖**——受控管理群
的日报**允许**展示姓名 / 工号 / 邮箱原值，理由是不给原值时管理员无法准确定位并处理人员
变化；仍然不得含凭据、令牌、认证信息或与处理无关的敏感数据，也仍然没有可执行入口。
**本模块尚未按 D2 改造**（属 Issue #52 的后续 Story），因此不得据本文件现状声称日报
已按 D2 交付，也不得把"正文里没有原值"当成还在生效的产品承诺。

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
    DiffKind,
    PersonDiff,
    RosterAuditReport,
)
from lingxi.config.content import ContentCatalog, RenderedContent, default_content_catalog

_CATALOG = default_content_catalog()
REPORT_TITLE = _CATALOG.text("roster.report_title").text

# 保留模块级导出供既有日报测试与调用方使用；正文来源仍是内容目录。
HANDOVER_MARK = _CATALOG.text("roster.handover_mark").text

_SECTION_TITLE_KEYS = {
    DiffKind.CHANGED: "roster.section.changed",
    DiffKind.REMOVED: "roster.section.removed",
    DiffKind.AMBIGUOUS: "roster.section.ambiguous",
}

_SECTION_NOTE_KEYS = {
    DiffKind.CHANGED: "roster.note.changed",
    DiffKind.REMOVED: "roster.note.removed",
    DiffKind.AMBIGUOUS: "roster.note.ambiguous",
}

# 章节输出顺序固定。
_SECTION_ORDER = (DiffKind.CHANGED, DiffKind.REMOVED, DiffKind.AMBIGUOUS)

_FIELD_LABEL_KEYS = {
    "display_name": "roster.field.display_name",
    "employee_no": "roster.field.employee_no",
    "email": "roster.field.email",
}


def _field_label(field: str, catalog) -> str:
    key = _FIELD_LABEL_KEYS.get(field)
    return catalog.text(key).text if key is not None else field


def _entry_line(entry: PersonDiff, *, catalog) -> str:
    """一条目一行。行首是完整的内部用户标识，其后只有字段名与标注。"""

    if not entry.changed_fields:
        return f"- {entry.app_user_id}"
    # 按 ARCHIVED_FIELDS 的固定次序渲染，不依赖 changed_fields 的构造顺序。
    labels = "、".join(
        _field_label(field, catalog) for field in ARCHIVED_FIELDS if field in entry.changed_fields
    )
    line = catalog.text(
        "roster.entry", user_id=entry.app_user_id, fields=labels
    ).text
    if entry.handover:
        return catalog.text(
            "roster.entry_handover", entry=line, handover_mark=HANDOVER_MARK
        ).text
    return line


def render_daily_report_content(
    report: RosterAuditReport,
    *,
    report_date: date,
    catalog: ContentCatalog | None = None,
) -> RenderedContent:
    """渲染日报正文并保留内容键/版本；空差异日仍只由调用方决定是否发送。"""

    catalog = catalog or default_content_catalog()
    title = catalog.text("roster.report_title").text
    lines = [
        catalog.text(
            "roster.header", title=title, report_date=report_date.isoformat()
        ).text,
        catalog.text(
            "roster.summary", examined=report.examined, entries=len(report.entries)
        ).text,
        catalog.text("roster.disclaimer").text,
    ]

    for kind in _SECTION_ORDER:
        section = tuple(entry for entry in report.entries if entry.kind is kind)
        if not section:
            continue
        section_title = catalog.text(_SECTION_TITLE_KEYS[kind]).text
        note = catalog.text(_SECTION_NOTE_KEYS[kind]).text
        heading = catalog.text(
            "roster.section_heading", title=section_title, count=len(section)
        ).text
        lines.append("")
        lines.append(
            catalog.text(
                "roster.section_heading_with_note", heading=heading, note=note
            ).text
            if note
            else heading
        )
        lines.extend(_entry_line(entry, catalog=catalog) for entry in section)

    return catalog.text("roster.daily_report", body="\n".join(lines))


def render_daily_report(
    report: RosterAuditReport,
    *,
    report_date: date,
    catalog: ContentCatalog | None = None,
) -> str:
    """兼容旧调用方的字符串入口；正式发送链路使用带元数据的函数。"""

    return render_daily_report_content(report, report_date=report_date, catalog=catalog).text
