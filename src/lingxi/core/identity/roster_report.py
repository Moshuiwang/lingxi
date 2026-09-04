"""管理群每日审计日报的正文渲染（纯函数）。

受控管理群的日报**允许**展示姓名/工号/邮箱原值——不给原值时管理员无法准确定位并
处理人员变化，但**花名册当前值不进正文**：字段名已经指明"去看哪一列"，核实当前
值仍要到花名册去看。正文含（`V-花名册-21`/`22`）：内部用户标识、存档身份字段
（姓名、工号）、发生变化的字段名、转交标注、快照读取时间与同步状态；不含凭据/
令牌/认证信息、未开通人员的任何资料。**人员标识用 `app_user.id`（ULID `usr_*`）
完整值**（`V-花名册-23`）：不用
:func:`~lingxi.core.identity.identifiers.redact_identifier` 的缩短形态（管理员
拿它没法定位到人）、不写飞书外部标识原值、不对 ULID 截断（截断前缀会碰撞）。
正文里**没有任何可执行入口**（`V-花名册-24`）：没有按钮、没有链接、没有回调，
管理群是通知面，处置一律回到管理 MCP 走确认流程。**时间一律 UTC 标注**：一份
跨时区被转述的日报，日期含义必须只有一种。渲染结果对同样的输入**逐字节一致**：
不取当前时间、不遍历集合、不用随机顺序。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date

from lingxi.config.content import ContentCatalog, RenderedContent, default_content_catalog
from lingxi.core.identity.roster_audit import (
    ARCHIVED_FIELDS,
    ArchivedIdentity,
    DiffKind,
    PersonDiff,
    RosterAuditReport,
)
from lingxi.core.identity.roster_snapshot import RosterSnapshotStatus

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

# 保旧告警分类（`core/identity/roster_snapshot.SnapshotAlertKind` 的字面量）到中文说明。
# 四类互不合并：合并任意两类，管理员就失去了「该去看哪里」这条信息。
_SNAPSHOT_REASON_KEYS = {
    "empty_source": "roster.snapshot_reason.empty_source",
    "incomplete": "roster.snapshot_reason.incomplete",
    "failed_definite": "roster.snapshot_reason.failed_definite",
    "failed_indeterminate": "roster.snapshot_reason.failed_indeterminate",
}

# 时间一律按 UTC 渲染，格式不含时区偏移字样——时区由文案里的「UTC」说明，
# 避免同一行出现两种时区表达。
_MOMENT_FORMAT = "%Y-%m-%d %H:%M:%S"


def _field_label(field: str, catalog) -> str:
    key = _FIELD_LABEL_KEYS.get(field)
    return catalog.text(key).text if key is not None else field


def _identity_text(identity: ArchivedIdentity | None, *, catalog) -> str:
    """一条目的存档身份（D2）：姓名与工号。

    只取这两个字段：它们合起来足以在花名册与管理后台唯一定位一个人，而工号还是银河
    账号匹配的主键。邮箱不进这一段——它是三个比对字段之一，「邮箱变了」由字段名说明，
    把它的值也列出来只会扩大每日进入群聊的资料面，对定位没有额外帮助。

    存档字段可以为空（建档时就没留工号的用户确实存在），空值渲染成固定占位符而不是空白：
    一行 ``姓名 张三｜工号`` 会让人以为是渲染坏了，而「存档为空」本身是管理员要看到的事实。
    """
    if identity is None:
        # 基线里查不到这个标识。理论上不会发生（比对集就是基线），真发生了也要如实说，
        # 不能悄悄渲染成一个看起来正常的空身份。
        return catalog.text("roster.identity_unknown").text
    absent = catalog.text("roster.value_absent").text
    return catalog.text(
        "roster.identity",
        name=identity.display_name.strip() or absent,
        employee_no=identity.employee_no.strip() or absent,
    ).text


def _entry_line(
    entry: PersonDiff,
    *,
    catalog,
    identities: Mapping[str, ArchivedIdentity],
) -> str:
    """一条目一行：完整内部标识 + 存档身份 + 变化字段名（+ 转交标注）。"""
    identity = _identity_text(identities.get(entry.app_user_id), catalog=catalog)
    if not entry.changed_fields:
        # 花名册查无此人 / 同一人员 ID 多行：没有「变化字段」可言，但身份仍要给全，
        # 否则管理员拿到一串 ULID 无从下手。
        return catalog.text("roster.entry_plain", user_id=entry.app_user_id, identity=identity).text
    # 按 ARCHIVED_FIELDS 的固定次序渲染，不依赖 changed_fields 的构造顺序。
    labels = "、".join(
        _field_label(field, catalog) for field in ARCHIVED_FIELDS if field in entry.changed_fields
    )
    line = catalog.text(
        "roster.entry", user_id=entry.app_user_id, identity=identity, fields=labels
    ).text
    if entry.handover:
        return catalog.text("roster.entry_handover", entry=line, handover_mark=HANDOVER_MARK).text
    return line


def _hours(seconds: float) -> str:
    """秒换算成小时的展示文本。

    保留一位小数：整数会把「刚过 48 小时」和「过了三天」渲染成同样的紧迫感，
    而这两者管理员的处理优先级不同。
    """
    return f"{seconds / 3600:.1f}"


def _snapshot_reason(status: RosterSnapshotStatus, *, catalog) -> str:
    key = _SNAPSHOT_REASON_KEYS.get(status.alert or "")
    if key is None:
        # 读取层将来多一个告警分类时，这里给出一句如实的「没分过类」，而不是把它
        # 归到四类中的任意一类——归错类会让管理员去查一个没坏的地方。
        return catalog.text("roster.snapshot_reason.unknown").text
    return catalog.text(key).text


def _snapshot_lines(status: RosterSnapshotStatus, *, catalog) -> list[str]:
    """快照时间与同步状态（D2 要求日报写明），必要时附超龄提醒（`V-花名册-47`）。"""
    if not status.available:
        # 没有任何可用快照：本轮根本没有比对。这一句必须是警告口吻——「没有差异」
        # 在这一天是不可信的（`V-花名册-48`）。
        return [
            catalog.text(
                "roster.snapshot_unavailable", reason=_snapshot_reason(status, catalog=catalog)
            ).text
        ]

    moment = status.captured_at.astimezone(UTC).strftime(_MOMENT_FORMAT)
    if status.refreshed:
        lines = [
            catalog.text(
                "roster.snapshot_fresh", captured_at=moment, row_count=status.row_count
            ).text
        ]
    else:
        lines = [
            catalog.text(
                "roster.snapshot_kept",
                captured_at=moment,
                row_count=status.row_count,
                age_hours=_hours(status.age_seconds or 0.0),
                reason=_snapshot_reason(status, catalog=catalog),
            ).text
        ]
    if status.stale:
        # 超龄只提醒，不删快照：始终保留最近一份，超龄按日报告警提醒、不自动删。
        lines.append(
            catalog.text(
                "roster.snapshot_stale",
                stale_after_hours=_hours(status.stale_after_seconds),
                age_hours=_hours(status.age_seconds or 0.0),
            ).text
        )
    return lines


def render_daily_report_content(
    report: RosterAuditReport,
    *,
    report_date: date,
    identities: Mapping[str, ArchivedIdentity] | None = None,
    snapshot: RosterSnapshotStatus | None = None,
    catalog: ContentCatalog | None = None,
) -> RenderedContent:
    """渲染日报正文并保留内容键/版本；空差异日仍只由调用方决定是否发送。

    ``identities`` 是本轮比对基线按 `app_user.id` 建的索引，供 D2 的存档身份段取值；
    ``snapshot`` 是本轮的快照状态（读取时间、同步状态、是否超龄）。两者都可省略，
    省略时正文退化成「只有标识与字段名」的形态——那是 D2 之前的口径，**不是**当前
    产品承诺，只供不涉及这两段的单元测试使用。
    """
    catalog = catalog or default_content_catalog()
    index: Mapping[str, ArchivedIdentity] = identities or {}
    title = catalog.text("roster.report_title").text
    lines = [catalog.text("roster.header", title=title, report_date=report_date.isoformat()).text]

    if snapshot is not None and not snapshot.available:
        # 没比对就不能说「本次发现 N 条」——那句话在这一天是假的。
        lines.append(catalog.text("roster.summary_not_compared").text)
    else:
        lines.append(
            catalog.text(
                "roster.summary", examined=report.examined, entries=len(report.entries)
            ).text
        )
    if snapshot is not None:
        lines.extend(_snapshot_lines(snapshot, catalog=catalog))
    lines.append(catalog.text("roster.disclaimer").text)

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
            catalog.text("roster.section_heading_with_note", heading=heading, note=note).text
            if note
            else heading
        )
        lines.extend(_entry_line(entry, catalog=catalog, identities=index) for entry in section)

    return catalog.text("roster.daily_report", body="\n".join(lines))


def render_daily_report(
    report: RosterAuditReport,
    *,
    report_date: date,
    identities: Mapping[str, ArchivedIdentity] | None = None,
    snapshot: RosterSnapshotStatus | None = None,
    catalog: ContentCatalog | None = None,
) -> str:
    """兼容旧调用方的字符串入口；正式发送链路使用带元数据的函数。"""
    return render_daily_report_content(
        report,
        report_date=report_date,
        identities=identities,
        snapshot=snapshot,
        catalog=catalog,
    ).text
