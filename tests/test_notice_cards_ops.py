"""#891 丙-1：管理群运维通知卡片化（内测每日通报、花名册审计日报、运行告警 / 恢复、
文案覆盖文件校验告警）。

卡片键由整合路写进内容目录；本模块用测试夹具把 :data:`PROPOSED_OPS_CARDS` 注入一份
目录副本，不改正式目录。钉住的事：

- 卡片键未进目录时，四个入口发出的仍是原文本，逐字相同，且不调用卡片发送；
- 卡片键进目录后改发卡片，等价纯文本就是原文本；飞书明确拒绝才回落纯文本一次，
  结果不明不补发（告警留在重试队列、同一去重键重试得到同一个卡片去重 ID）；
- 日报「先看异常」按 K-4 默认；缺失不填零；统计窗口同时写 UTC 与北京时间；
- 告警故障 / 恢复色调与标题可辨识，字段只限 V-告警-06 允许的范围；
- 否定断言：卡片无按钮 / 链接 / 回调，自由文本转义后不能成为链接或提及。
"""

from __future__ import annotations

import json
import unittest
import unittest.mock
from datetime import UTC, date, datetime
from types import SimpleNamespace

from lingxi.adapters.feishu_group_message import (
    DELIVERY_UUID_PREFIX,
    FeishuGroupMessages,
    card_uuid_prefix_for,
    delivery_uuid,
)
from lingxi.apps.scheduler.daily_report_sections import _render_daily_report_text
from lingxi.config import content as content_module
from lingxi.config.content import ContentCatalog, default_content_catalog
from lingxi.core import alert_card
from lingxi.core.alerting import AlertDispatcher, AlertKind, AlertNotice, AlertPolicy, NoticeAction
from lingxi.core.daily_report import (
    ActiveUserStats,
    DailyReportInputs,
    DeliveryOutcomeStats,
    FailureReasonCount,
    LatencyStats,
    MetricCoverageGap,
    PartialCount,
    Section,
    StatusDistribution,
    TokenUsageStats,
    daily_report_anomalies,
    render_daily_report,
    render_daily_report_card,
    render_daily_report_notice,
)
from lingxi.core.delivery.notice_card import NoticeCard, NoticeTone, assert_no_actionable_elements
from lingxi.core.delivery.ops_notice import (
    CONTENT_OVERRIDE_ALERT_TEXT,
    OPS_CARD_KEYS,
    ops_notice_card,
    send_group_notice,
)
from lingxi.core.identity.roster_audit import ArchivedIdentity, compare_roster
from lingxi.core.identity.roster_report import (
    render_daily_report_card as render_roster_card,
)
from lingxi.core.identity.roster_report import (
    render_daily_report_content,
)
from lingxi.core.identity.roster_report import (
    render_daily_report_notice as render_roster_notice,
)
from lingxi.core.identity.roster_snapshot import RosterSnapshotStatus

NEXT = "**下一步**"

#: 交给整合路的卡片清单：卡片键 → (标题, 正文模板)。色调在代码里固定（日报 / 花名册
#: 日报：有「先看异常」行为「需注意」否则「完成」；告警故障「故障」、恢复「恢复」；
#: 文案覆盖「需注意」）。正文按空行分段，小标题用粗体写在模板里；数据分段由代码
#: 追加在模板分段之后。
PROPOSED_OPS_CARDS: dict[str, tuple[str, str]] = {
    "notice.innertest.daily_report": (
        "内测每日通报",
        "本报告为统计级数据，不含用户对话原文、姓名、工号或邮箱。",
    ),
    "notice.roster.daily_report": (
        "花名册每日资料比对",
        "**日期**：{report_date}（UTC）",
    ),
    "notice.alert.fault": (
        "运行故障：{kind_label}",
        "**类型**：{kind_label}\n**范围**：{scope}\n**次数**：{count}\n"
        "**时间**：{observed_at}\n**{reference_label}**：{reference}\n\n"
        f"{NEXT}\n请按类型与范围排查；本通知不会自动重启、修复或重发。",
    ),
    "notice.alert.recovery": (
        "已恢复：{kind_label}",
        "**类型**：{kind_label}\n**范围**：{scope}\n**次数**：{count}\n"
        "**时间**：{observed_at}\n**{reference_label}**：{reference}\n\n"
        f"{NEXT}\n无需处理；本通知不会自动执行任何操作。",
    ),
    "notice.content.override_rejected": (
        "文案覆盖文件未通过校验",
        "宿主机上的用户可见文案覆盖文件已被整份忽略，用户看到的仍是随镜像发布的那一版"
        "文案；不影响任何在跑的服务。\n**原因码**：{reason}\n\n"
        # 正式目录受「用户可见文案零内部代号」检查约束，不写完整模块路径；完整命令
        # 在等价纯文本里。
        f"{NEXT}\n用随镜像附带的文案校验命令（`config.content_check` 模块）校验该文件后"
        "重新放置，并重启相关服务。",
    ),
}

#: 每个卡片键模板的占位集合（交给整合路的清单里同样写明）。
PROPOSED_VARIABLES: dict[str, frozenset[str]] = {
    "notice.innertest.daily_report": frozenset(),
    "notice.roster.daily_report": frozenset({"report_date"}),
    "notice.alert.fault": frozenset(
        {"kind_label", "scope", "count", "observed_at", "reference_label", "reference"}
    ),
    "notice.alert.recovery": frozenset(
        {"kind_label", "scope", "count", "observed_at", "reference_label", "reference"}
    ),
    "notice.content.override_rejected": frozenset({"reason"}),
}

_OFFICIAL = default_content_catalog()


def _catalog_without_notice_cards() -> ContentCatalog:
    """正式目录去掉全部 ``notice.*`` 卡片键：代表「卡片键未进目录」。"""
    cards = {k: v for k, v in _OFFICIAL._cards.items() if not k.startswith("notice.")}  # noqa: SLF001
    return ContentCatalog(version=_OFFICIAL.version, texts=_OFFICIAL._texts, cards=cards)  # noqa: SLF001


#: 「键未进目录」的目录：正式目录已登记运维卡片键，这里剔除 ``notice.*``。
_BASE = _catalog_without_notice_cards()
CHAT_ID = "oc_fake_chat_for_ops_tests"
WINDOW_START = datetime(2026, 9, 26, tzinfo=UTC)
WINDOW_END = datetime(2026, 9, 27, tzinfo=UTC)
DELIVERY_START = datetime(2026, 9, 25, tzinfo=UTC)
DELIVERY_END = datetime(2026, 9, 26, tzinfo=UTC)
ACCEPTED = {"code": 0, "data": {"message_id": "om-fake"}}
REJECTED = {"code": 230099, "msg": "card rejected"}
ACTIONABLE_MARKERS = ("://", "](", "<at", "button", "callback", "behaviors", '"url"')


def catalog_with_ops_cards(
    cards: dict[str, tuple[str, str]] | None = None,
    *,
    buttons: tuple[str, ...] = (),
    base: ContentCatalog = _BASE,
) -> ContentCatalog:
    """测试夹具：不传参数时就是正式目录（运维卡片键已登记）；传入模板或按钮文字时
    在剔除通知卡片键的目录上追加这些模板，走同一道模板校验。"""
    if cards is None and not buttons and base is _BASE:
        return _OFFICIAL
    registered = dict(base._cards)  # noqa: SLF001 - 夹具只读正式目录的已校验模板
    for key, (title, body) in (PROPOSED_OPS_CARDS if cards is None else cards).items():
        registered[key] = content_module._parse_card_template(  # noqa: SLF001
            key, {"title": title, "body": body, "button_labels": list(buttons)}
        )
    return ContentCatalog(version=base.version, texts=base._texts, cards=registered)  # noqa: SLF001


def payload_text(card: NoticeCard) -> str:
    payload = card.to_payload()
    parts = [payload["header"]["title"]["content"]]
    parts += [element.get("content", "") for element in payload["body"]["elements"]]
    return "\n".join(parts)


def assert_read_only_card(case: unittest.TestCase, card: NoticeCard) -> None:
    """管理群卡片否定断言：载荷里没有按钮、链接、回调或提及。"""
    payload = card.to_payload()
    assert_no_actionable_elements(payload)
    serialized = json.dumps(payload, ensure_ascii=False)
    for marker in ACTIONABLE_MARKERS:
        case.assertNotIn(marker, serialized)


class RecordingGroupSender:
    """只记录的群发送口；``notice_error`` 模拟卡片结果不明。"""

    def __init__(self, *, notice_error: Exception | None = None) -> None:
        self.texts: list[dict[str, str]] = []
        self.notices: list[dict[str, object]] = []
        self.notice_error = notice_error

    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None:
        self.texts.append({"chat_id": chat_id, "text": text, "dedupe_key": dedupe_key})

    def send_notice(self, *, chat_id: str, card: NoticeCard, dedupe_key: str) -> None:
        self.notices.append({"chat_id": chat_id, "card": card, "dedupe_key": dedupe_key})
        if self.notice_error is not None:
            raise self.notice_error


class TextOnlySender:
    def __init__(self) -> None:
        self.texts: list[dict[str, str]] = []

    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None:
        self.texts.append({"chat_id": chat_id, "text": text, "dedupe_key": dedupe_key})


class TransportTimeoutError(Exception):
    """模拟传输层超时：结果不明。"""


class FakeFeishu:
    """按消息类型脚本化响应的假传输层，记录每次发消息请求。"""

    def __init__(self, *, card=ACCEPTED, text=ACCEPTED) -> None:
        self.calls: list[dict] = []
        self._scripts = {"interactive": card, "text": text}

    def __call__(self, method, url, *, body=None, token=None, **kwargs):
        if "tenant_access_token" in url:
            return {"code": 0, "tenant_access_token": "t-fake"}
        self.calls.append(body)
        step = self._scripts[body["msg_type"]]
        if isinstance(step, BaseException):
            raise step
        return step

    def sent(self, msg_type: str) -> list[dict]:
        return [body for body in self.calls if body["msg_type"] == msg_type]


def feishu_group(fake: FakeFeishu, *, prefix: str = DELIVERY_UUID_PREFIX) -> FeishuGroupMessages:
    return FeishuGroupMessages(
        base_url="https://open.feishu.cn/open-apis",
        app_id="cli_fake",
        app_secret="fake-secret",
        transport=fake,
        uuid_prefix=prefix,
    )


# --------------------------------------------------------------------------
# 交给整合路的清单本身
# --------------------------------------------------------------------------


class ProposedCardsTest(unittest.TestCase):
    def test_every_proposed_card_passes_the_catalog_template_checks(self) -> None:
        catalog = catalog_with_ops_cards()
        self.assertEqual(set(PROPOSED_OPS_CARDS), set(OPS_CARD_KEYS))
        for key in OPS_CARD_KEYS:
            with self.subTest(key=key):
                self.assertTrue(catalog.has_card(key))
                template = catalog._cards[key]  # noqa: SLF001
                self.assertEqual(template.variables, PROPOSED_VARIABLES[key])
                self.assertEqual(template.button_labels, ())

    def test_the_official_catalog_registers_exactly_the_proposed_templates(self) -> None:
        """整合路已把清单写进正式目录：模板逐字等于清单，且无按钮文字。"""
        for key, (title, body) in PROPOSED_OPS_CARDS.items():
            with self.subTest(key=key):
                template = _OFFICIAL._cards[key]  # noqa: SLF001
                self.assertEqual((template.title.template, template.body.template), (title, body))
                self.assertEqual(template.button_labels, ())

    def test_the_plain_catalog_has_none_of_the_ops_card_keys(self) -> None:
        """剔除卡片键后的目录没有这些键：各入口仍发原文本。"""
        for key in OPS_CARD_KEYS:
            self.assertFalse(_BASE.has_card(key))

    def test_a_template_with_button_labels_falls_back_to_text(self) -> None:
        catalog = catalog_with_ops_cards(buttons=("立即处理",))
        card = ops_notice_card(
            catalog,
            "notice.innertest.daily_report",
            tone=NoticeTone.DONE,
            fallback_text="原文本",
        )
        self.assertIsNone(card)


class SendGroupNoticeTest(unittest.TestCase):
    def test_without_a_card_only_send_text_is_called(self) -> None:
        sender = RecordingGroupSender()
        send_group_notice(sender, chat_id=CHAT_ID, text="原文本", card=None, dedupe_key="k")
        self.assertEqual(sender.texts, [{"chat_id": CHAT_ID, "text": "原文本", "dedupe_key": "k"}])
        self.assertEqual(sender.notices, [])

    def test_a_card_to_a_text_only_sender_goes_out_as_text(self) -> None:
        card = NoticeCard(
            title="内测每日通报", tone=NoticeTone.DONE, sections=[(None, ("x",))], fallback_text="t"
        )
        sender = TextOnlySender()
        send_group_notice(sender, chat_id=CHAT_ID, text="原文本", card=card, dedupe_key="k")
        self.assertEqual([item["text"] for item in sender.texts], ["原文本"])

    def test_an_unclear_card_result_raises_and_sends_no_text(self) -> None:
        card = NoticeCard(
            title="内测每日通报", tone=NoticeTone.DONE, sections=[(None, ("x",))], fallback_text="t"
        )
        sender = RecordingGroupSender(notice_error=TransportTimeoutError("timeout"))
        with self.assertRaises(TransportTimeoutError):
            send_group_notice(sender, chat_id=CHAT_ID, text="原文本", card=card, dedupe_key="k")
        self.assertEqual(sender.texts, [])


# --------------------------------------------------------------------------
# #23 内测每日通报
# --------------------------------------------------------------------------


def daily_inputs(**overrides: object) -> DailyReportInputs:
    values: dict[str, object] = {
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
        "active_users": Section.of(ActiveUserStats(8, (("1-5 次", 8),))),
        "status_distribution": Section.of(
            StatusDistribution(success=18, failed=0, timeout=0, stopped=0, in_progress=0)
        ),
        "failure_top": Section.of(()),
        "guard_triggered": Section.of(0),
        "denied_count": Section.of(PartialCount(total=0, covered_tasks=20, uncovered_tasks=0)),
        "latency": Section.of(
            LatencyStats(
                sample_count=18,
                average_seconds=13.0,
                median_seconds=12.0,
                p90_seconds=20.0,
                max_seconds=30.0,
            )
        ),
        "resource_usage": Section.of(TokenUsageStats(10, 20, 0, 0, 20, 0)),
        "delivery_outcome": Section.of(DeliveryOutcomeStats(12, 0, 0, 1)),
        "delivery_window_start": DELIVERY_START,
        "delivery_window_end": DELIVERY_END,
    }
    values.update(overrides)
    return DailyReportInputs(**values)  # type: ignore[arg-type]


def sections_of(inputs: DailyReportInputs, throttled=None) -> SimpleNamespace:
    """scheduler 段落聚合结果的形状：各段 ``Section`` 加 ``throttled_lines``。"""
    return SimpleNamespace(
        active_users=inputs.active_users,
        status_distribution=inputs.status_distribution,
        failure_top=inputs.failure_top,
        guard_triggered=inputs.guard_triggered,
        denied_count=inputs.denied_count,
        latency=inputs.latency,
        resource_usage=inputs.resource_usage,
        delivery_outcome=inputs.delivery_outcome,
        metric_coverage_gap=inputs.metric_coverage_gap,
        local_override_activity=inputs.local_override_activity,
        throttled_lines=throttled,
    )


def daily_notice(inputs: DailyReportInputs, catalog: ContentCatalog):
    return render_daily_report_notice(
        sections_of(inputs),
        window_start=inputs.window_start,
        window_end=inputs.window_end,
        delivery_window_start=inputs.delivery_window_start,
        delivery_window_end=inputs.delivery_window_end,
        catalog=catalog,
    )


def headings(card: NoticeCard) -> list[str | None]:
    return [section.heading for section in card.sections]


def section_lines(card: NoticeCard, heading: str) -> tuple[str, ...]:
    for section in card.sections:
        if section.heading == heading:
            return section.lines
    raise AssertionError(f"卡片没有分段 {heading}")


class DailyReportCardTest(unittest.TestCase):
    def test_without_the_card_key_the_text_is_byte_identical_and_no_card(self) -> None:
        inputs = daily_inputs()
        notice = daily_notice(inputs, _BASE)
        expected = _render_daily_report_text(
            sections_of(inputs),
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            delivery_window_start=DELIVERY_START,
            delivery_window_end=DELIVERY_END,
        )
        self.assertEqual(notice.text, expected)
        self.assertIsNone(notice.card)
        sender = RecordingGroupSender()
        notice.send(sender, chat_id=CHAT_ID, dedupe_key="daily-report:2026-09-27")
        self.assertEqual([item["text"] for item in sender.texts], [expected])
        self.assertEqual(sender.notices, [])

    def test_anomalies_come_first_and_the_tone_is_attention(self) -> None:
        inputs = daily_inputs(
            status_distribution=Section.of(
                StatusDistribution(success=18, failed=2, timeout=0, stopped=0, in_progress=0)
            ),
            failure_top=Section.of((FailureReasonCount("turn_timeout", 2),)),
            resource_usage=Section.undetermined("采集数据未取得"),
        )
        notice = daily_notice(inputs, catalog_with_ops_cards())
        card = notice.card
        assert card is not None
        self.assertEqual(card.tone, NoticeTone.ATTENTION)
        self.assertEqual(card.title, "内测每日通报")
        self.assertEqual(card.fallback_text, notice.text)
        self.assertEqual(headings(card)[:3], [None, "统计窗口", "先看异常"])
        anomalies = "\n".join(section_lines(card, "先看异常"))
        self.assertIn("失败 2", anomalies)
        self.assertIn("token 用量：不可判定（原因：采集数据未取得）", anomalies)
        assert_read_only_card(self, card)

    def test_a_clean_day_says_no_anomaly_and_is_done(self) -> None:
        card = daily_notice(daily_inputs(), catalog_with_ops_cards()).card
        assert card is not None
        self.assertEqual(card.tone, NoticeTone.DONE)
        self.assertEqual(daily_report_anomalies(daily_inputs()), [])
        self.assertTrue(section_lines(card, "先看异常")[0].startswith("无："))

    def test_guard_and_denied_counts_above_zero_are_anomalies(self) -> None:
        inputs = daily_inputs(
            guard_triggered=Section.of(3),
            denied_count=Section.of(PartialCount(total=4, covered_tasks=20, uncovered_tasks=0)),
        )
        lines = daily_report_anomalies(inputs)
        self.assertEqual(len(lines), 2)
        self.assertIn("3 次", lines[0])
        self.assertIn("4 次", lines[1])

    def test_the_permanent_call_count_gap_is_not_an_anomaly(self) -> None:
        """调用次数对照恒为不可判定，是结构性缺口；不能让每天都被标成「需注意」。"""
        self.assertEqual(daily_report_anomalies(daily_inputs()), [])
        text = render_daily_report(daily_inputs())
        self.assertIn("调用次数对照", text)

    def test_missing_data_is_never_rendered_as_zero(self) -> None:
        inputs = daily_inputs(
            latency=Section.of(None),
            active_users=Section.undetermined("数据库超时"),
            denied_count=Section.undetermined("字段全为空"),
        )
        card = daily_notice(inputs, catalog_with_ops_cards()).card
        assert card is not None
        body = payload_text(card)
        self.assertIn("无样本", body)
        self.assertIn("活跃用户与任务量分布：不可判定（原因：数据库超时）", body)
        self.assertIn("不可判定（原因：字段全为空）", "\n".join(section_lines(card, "先看异常")))
        self.assertNotIn("活跃用户：0", body)
        self.assertNotIn("拒绝计数（PreToolUse 拒绝）：0", body)

    def test_both_windows_carry_utc_and_beijing_with_full_dates(self) -> None:
        card = daily_notice(daily_inputs(), catalog_with_ops_cards()).card
        assert card is not None
        window = section_lines(card, "统计窗口")
        self.assertEqual(window[0], "UTC：2026\\-09\\-26 00:00 至 2026\\-09\\-27 00:00")
        self.assertEqual(window[1], "北京时间：2026\\-09\\-26 08:00 至 2026\\-09\\-27 08:00")
        delivery = section_lines(card, "投递结果（使用更早的窗口）")
        self.assertIn("2026\\-09\\-25 00:00", delivery[0])
        self.assertIn("早一天", "\n".join(delivery))

    def test_free_text_in_metric_ids_is_escaped_and_cannot_become_a_link(self) -> None:
        gap = MetricCoverageGap(
            uncovered_metric_ids=("[点我](https://evil.example)", "<at id=all>")
        )
        inputs = daily_inputs(metric_coverage_gap=Section.of(gap))
        card = daily_notice(inputs, catalog_with_ops_cards()).card
        assert card is not None
        self.assertIn("待分配", headings(card))
        assert_read_only_card(self, card)

    def test_render_card_without_the_key_returns_none(self) -> None:
        self.assertIsNone(render_daily_report_card(daily_inputs(), text="t", catalog=_BASE))


# --------------------------------------------------------------------------
# #24 花名册审计日报
# --------------------------------------------------------------------------

REPORT_DATE = date(2026, 9, 27)
USER = "usr_01JQZX3M5N7P9R1T3V5W7Y9A0B"
SNAPSHOT_MOMENT = datetime(2026, 9, 27, 1, 30, tzinfo=UTC)


def roster_scenario(name: str = "张三"):
    baseline = [ArchivedIdentity(USER, "ou_pfxAAAA1111", name, "E1001", "a@example.com")]
    rows = [
        {
            "personnel_id": "ou_pfxAAAA1111",
            "name": "改名",
            "employee_no": "E1001",
            "email": "a@example.com",
        }
    ]
    return compare_roster(baseline, rows), {USER: baseline[0]}


def snapshot(*, action: str = "replace", age: float = 0.0, captured: bool = True):
    return RosterSnapshotStatus(
        action=action,
        read_status="complete" if action == "replace" else "empty_source",
        stale_after_seconds=48 * 3600,
        alert=None if action == "replace" else "empty_source",
        captured_at=SNAPSHOT_MOMENT if captured else None,
        row_count=1206 if captured else 0,
        age_seconds=age if captured else None,
    )


class RosterReportCardTest(unittest.TestCase):
    def test_without_the_card_key_the_text_is_unchanged_and_no_card(self) -> None:
        report, identities = roster_scenario()
        kwargs = {"report_date": REPORT_DATE, "identities": identities, "snapshot": snapshot()}
        notice = render_roster_notice(report, catalog=_BASE, **kwargs)
        content = render_daily_report_content(report, catalog=_BASE, **kwargs)
        self.assertEqual(
            (notice.key, notice.version, notice.text), (content.key, content.version, content.text)
        )
        self.assertIsNone(notice.card)
        sender = RecordingGroupSender()
        notice.send(sender, chat_id=CHAT_ID, dedupe_key=REPORT_DATE.isoformat())
        self.assertEqual([item["text"] for item in sender.texts], [content.text])
        self.assertEqual(sender.notices, [])

    def test_entries_to_verify_are_the_first_anomaly(self) -> None:
        report, identities = roster_scenario()
        notice = render_roster_notice(
            report,
            report_date=REPORT_DATE,
            identities=identities,
            snapshot=snapshot(),
            catalog=catalog_with_ops_cards(),
        )
        card = notice.card
        assert card is not None
        self.assertEqual(card.tone, NoticeTone.ATTENTION)
        self.assertEqual(card.fallback_text, notice.text)
        self.assertEqual(headings(card)[:3], [None, "先看异常", "比对概况"])
        self.assertIn("1 条需要人工核实", section_lines(card, "先看异常")[0])
        self.assertEqual(headings(card)[-1], "下一步")
        self.assertIn("2026\\-09\\-27", payload_text(card))
        self.assertIn(USER.replace("_", "\\_"), payload_text(card))
        assert_read_only_card(self, card)

    def test_stale_kept_and_missing_snapshots_are_anomalies(self) -> None:
        report, identities = roster_scenario()
        catalog = catalog_with_ops_cards()
        for status, expected in (
            (snapshot(action="keep_previous", age=3600.0), "继续使用上一份快照"),
            (snapshot(age=72 * 3600.0), "已超过"),
            (snapshot(captured=False), "本次未进行比对"),
        ):
            with self.subTest(expected=expected):
                card = render_roster_card(
                    report,
                    report_date=REPORT_DATE,
                    text="t",
                    identities=identities,
                    snapshot=status,
                    catalog=catalog,
                )
                assert card is not None
                self.assertEqual(card.tone, NoticeTone.ATTENTION)
                self.assertIn(expected, "\n".join(section_lines(card, "先看异常")))

    def test_names_with_markup_are_escaped_and_cannot_mention_or_link(self) -> None:
        report, identities = roster_scenario(name="<at id=all>[点我](https://evil.example)")
        card = render_roster_card(
            report,
            report_date=REPORT_DATE,
            text="t",
            identities=identities,
            snapshot=snapshot(),
            catalog=catalog_with_ops_cards(),
        )
        assert card is not None
        assert_read_only_card(self, card)


# --------------------------------------------------------------------------
# #25 进程内运行告警 / 恢复
# --------------------------------------------------------------------------

OBSERVED = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)


def alert(action: NoticeAction = NoticeAction.ALERT, **overrides: object) -> AlertNotice:
    values: dict[str, object] = {
        "action": action,
        "kind": AlertKind.FEISHU_SEND_FAILED,
        "scope": "feishu_send",
        "observed_at": OBSERVED,
        "count": 3,
        "trace_id": "trace-abc",
        "dedupe_key": "d" * 64,
    }
    values.update(overrides)
    return AlertNotice(**values)  # type: ignore[arg-type]


class AlertCardTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = unittest.mock.patch.object(
            alert_card, "default_content_catalog", return_value=catalog_with_ops_cards()
        )
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_without_the_card_key_the_alert_text_is_unchanged(self) -> None:
        with unittest.mock.patch.object(alert_card, "default_content_catalog", return_value=_BASE):
            sender = RecordingGroupSender()
            alert_card.deliver_alert(sender, CHAT_ID, alert())
        self.assertEqual(
            sender.texts, [{"chat_id": CHAT_ID, "text": alert().text, "dedupe_key": "d" * 64}]
        )
        self.assertEqual(sender.notices, [])

    def test_fault_and_recovery_are_distinguishable(self) -> None:
        fault = alert_card.alert_notice_card(alert())
        recovery = alert_card.alert_notice_card(alert(NoticeAction.RECOVERY))
        assert fault is not None and recovery is not None
        self.assertEqual((fault.tone, recovery.tone), (NoticeTone.FAILURE, NoticeTone.RECOVERY))
        self.assertEqual(fault.title, "运行故障：飞书发送失败")
        self.assertEqual(recovery.title, "已恢复：飞书发送失败")
        self.assertNotEqual(
            fault.to_payload()["header"]["template"], recovery.to_payload()["header"]["template"]
        )
        self.assertEqual(fault.fallback_text, alert().text)
        assert_read_only_card(self, fault)
        assert_read_only_card(self, recovery)

    def test_the_card_carries_only_the_safe_summary_fields(self) -> None:
        card = alert_card.alert_notice_card(alert())
        assert card is not None
        body = payload_text(card)
        for field in ("**类型**", "**范围**", "**次数**", "**时间**", "**追溯号**"):
            self.assertIn(field, body)
        self.assertIn("2026\\-09\\-27 10:00:00 UTC", body)
        self.assertIn("trace\\-abc", body)
        self.assertNotIn("错误类型", body)  # K-3 默认：不新增字段

    def test_a_task_alert_shows_the_task_reference(self) -> None:
        task_id = "tsk_01JQZX3M5N7P9R1T3V5W7Y9A0B"
        notice = alert(task_id=task_id, trace_id=None)
        values = alert_card.alert_card_values(notice)
        self.assertEqual(values["reference_label"], "任务参考号")
        self.assertIn(f"任务参考号：{values['reference']}", notice.text)

    def test_the_dispatcher_sends_one_card_and_no_text_when_accepted(self) -> None:
        fake = FakeFeishu()
        dispatcher = AlertDispatcher(
            sender=feishu_group(fake), chat_id=CHAT_ID, clock=lambda: OBSERVED
        )
        dispatcher.submit([alert()])
        self.assertEqual(dispatcher.run_once(at=OBSERVED), 1)
        self.assertEqual(len(fake.sent("interactive")), 1)
        self.assertEqual(fake.sent("text"), [])

    def test_a_definite_card_rejection_falls_back_to_text_exactly_once(self) -> None:
        fake = FakeFeishu(card=REJECTED)
        dispatcher = AlertDispatcher(
            sender=feishu_group(fake), chat_id=CHAT_ID, clock=lambda: OBSERVED
        )
        dispatcher.submit([alert()])
        self.assertEqual(dispatcher.run_once(at=OBSERVED), 1)
        texts = fake.sent("text")
        self.assertEqual(len(texts), 1)
        self.assertEqual(json.loads(texts[0]["content"])["text"], alert().text)
        self.assertEqual(
            texts[0]["uuid"], delivery_uuid(CHAT_ID, "d" * 64, prefix=DELIVERY_UUID_PREFIX)
        )
        self.assertEqual(dispatcher.pending_count, 0)

    def test_an_unclear_card_result_sends_no_text_and_retries_with_the_same_card_id(self) -> None:
        fake = FakeFeishu(card=TransportTimeoutError("timeout"))
        policy = AlertPolicy()
        dispatcher = AlertDispatcher(
            sender=feishu_group(fake), chat_id=CHAT_ID, policy=policy, clock=lambda: OBSERVED
        )
        dispatcher.submit([alert()])
        self.assertEqual(dispatcher.run_once(at=OBSERVED), 0)
        self.assertEqual(fake.sent("text"), [])
        self.assertEqual(dispatcher.pending_count, 1)
        later = datetime(2026, 9, 27, 11, 0, tzinfo=UTC)
        dispatcher.run_once(at=later)
        uuids = {body["uuid"] for body in fake.sent("interactive")}
        card_prefix = card_uuid_prefix_for(DELIVERY_UUID_PREFIX)
        self.assertEqual(uuids, {delivery_uuid(CHAT_ID, "d" * 64, prefix=card_prefix)})
        self.assertEqual(len(fake.sent("interactive")), 2)
        self.assertEqual(fake.sent("text"), [])

    def test_a_log_only_sender_still_gets_the_plain_text(self) -> None:
        sender = TextOnlySender()
        dispatcher = AlertDispatcher(sender=sender, chat_id="log-only", clock=lambda: OBSERVED)
        dispatcher.submit([alert()])
        dispatcher.run_once(at=OBSERVED)
        self.assertEqual([item["text"] for item in sender.texts], [alert().text])

    def test_a_catalog_that_fails_to_load_does_not_block_the_alert(self) -> None:
        error = content_module.ContentValidationError("坏目录")
        with unittest.mock.patch.object(alert_card, "default_content_catalog", side_effect=error):
            sender = RecordingGroupSender()
            alert_card.deliver_alert(sender, CHAT_ID, alert())
        self.assertEqual([item["text"] for item in sender.texts], [alert().text])


# --------------------------------------------------------------------------
# #26 文案覆盖文件校验告警
# --------------------------------------------------------------------------


class _Audit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, object]]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, fields))


class ContentOverrideCardTest(unittest.TestCase):
    REASON = "unsafe_text"

    def _run(self, catalog: ContentCatalog, transport: FakeFeishu) -> _Audit:
        from lingxi.adapters import feishu_group_message
        from lingxi.apps.scheduler import content_override_notice
        from lingxi.config import content_override

        source = content_override.ContentSource(
            catalog=catalog,
            digest="abcdef012345",
            override_path="/etc/lingxi/runtime/content.override.toml",
            override_digest="0123456789ab",
            rejection=self.REASON,
        )
        created: list[FeishuGroupMessages] = []

        def factory(**kwargs: object) -> FeishuGroupMessages:
            created.append(FeishuGroupMessages(transport=transport, **kwargs))  # type: ignore[arg-type]
            return created[-1]

        audit = _Audit()
        config = SimpleNamespace(
            admin_group_chat_id=CHAT_ID,
            feishu_base_url="https://open.feishu.cn/open-apis",
            feishu_app_id="cli_test",
            feishu_app_secret="secret",
        )
        with (
            unittest.mock.patch.object(content_override, "default_content_source", lambda: source),
            unittest.mock.patch.object(feishu_group_message, "FeishuGroupMessages", factory),
        ):
            content_override_notice.notify_content_override_rejection(config, audit=audit)
        return audit

    def _text(self) -> str:
        return CONTENT_OVERRIDE_ALERT_TEXT.format(reason=self.REASON)

    def test_without_the_card_key_the_text_is_unchanged(self) -> None:
        fake = FakeFeishu()
        self._run(_BASE, fake)
        self.assertEqual(fake.sent("interactive"), [])
        self.assertEqual(
            [json.loads(b["content"])["text"] for b in fake.sent("text")], [self._text()]
        )

    def test_with_the_card_key_a_read_only_card_is_sent(self) -> None:
        fake = FakeFeishu()
        audit = self._run(catalog_with_ops_cards(), fake)
        self.assertEqual(fake.sent("text"), [])
        cards = fake.sent("interactive")
        self.assertEqual(len(cards), 1)
        payload = json.loads(cards[0]["content"])
        self.assertEqual(payload["header"]["template"], "orange")
        body = "\n".join(element.get("content", "") for element in payload["body"]["elements"])
        self.assertIn("unsafe\\_text", body)
        assert_no_actionable_elements(payload)
        self.assertIn(("content.override_alert_sent", {"reason": self.REASON}), audit.records)

    def test_a_definite_rejection_falls_back_to_the_same_text_once(self) -> None:
        fake = FakeFeishu(card=REJECTED)
        self._run(catalog_with_ops_cards(), fake)
        self.assertEqual(
            [json.loads(b["content"])["text"] for b in fake.sent("text")], [self._text()]
        )

    def test_an_unclear_result_sends_no_text_and_is_audited_as_failed(self) -> None:
        fake = FakeFeishu(card=TransportTimeoutError("timeout"))
        audit = self._run(catalog_with_ops_cards(), fake)
        self.assertEqual(fake.sent("text"), [])
        self.assertIn("content.override_alert_failed", [action for action, _ in audit.records])


if __name__ == "__main__":
    unittest.main()
