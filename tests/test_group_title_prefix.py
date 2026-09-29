"""管理群消息统一前缀 ``[lingxi] ``（#909）与告警时间双标注（#911）。

管理群唯一出口 ``FeishuGroupMessages`` 给卡片标题与纯文本首行加前缀；个人私聊
不经该出口，标题不变。时间格式的期望值用字面量断言，固定输入时刻。
"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime

from lingxi.adapters.feishu_group_message import FeishuGroupMessages, with_group_prefix
from lingxi.config.content import default_content_catalog
from lingxi.core.alert_card import alert_card_values, alert_notice_card, format_utc_with_beijing
from lingxi.core.alerting import AlertKind, AlertNotice, NoticeAction
from lingxi.core.delivery.catalog_notice import send_catalog_notice
from lingxi.core.delivery.notice_card import NoticeCard, NoticeSection, NoticeTone

_OK = {"code": 0, "data": {"message_id": "om_1"}}
_REJECTED = {"code": 230099, "msg": "rejected"}


class _Transport:
    def __init__(self, *, card_response: dict = _OK) -> None:
        self.messages: list[dict] = []
        self._card_response = card_response

    def __call__(self, method, url, *, body=None, token=None, **kwargs):
        if "tenant_access_token" in url:
            return {"code": 0, "tenant_access_token": "t"}
        self.messages.append(body)
        return self._card_response if body["msg_type"] == "interactive" else _OK


def _sender(transport: _Transport) -> FeishuGroupMessages:
    return FeishuGroupMessages(
        base_url="https://feishu.invalid/open-apis",
        app_id="app",
        app_secret="secret",
        transport=transport,
    )


def _card(title: str, fallback: str) -> NoticeCard:
    return NoticeCard(
        title=title,
        tone=NoticeTone.FAILURE,
        sections=(NoticeSection(heading=None, lines=("正文",)),),
        fallback_text=fallback,
    )


class GroupPrefixTests(unittest.TestCase):
    def test_prefix_function(self) -> None:
        self.assertEqual(with_group_prefix("内测每日通报"), "[lingxi] 内测每日通报")
        self.assertEqual(
            with_group_prefix("[BI Plus 运行告警] 故障\n类型：x"), "[lingxi] 运行告警 故障\n类型：x"
        )
        self.assertEqual(with_group_prefix("[lingxi] 已带"), "[lingxi] 已带")
        self.assertEqual(with_group_prefix("[BI Plus] 后文"), "[lingxi] 后文")
        self.assertEqual(with_group_prefix("[BI Plus 资源监控] 告警"), "[lingxi] 资源监控 告警")
        self.assertEqual(
            with_group_prefix(with_group_prefix("[BI Plus 宿主监控] 恢复")),
            "[lingxi] 宿主监控 恢复",
        )

    def test_card_title_prefixed_and_not_stacked(self) -> None:
        for title in ("内测每日通报", "[lingxi] 内测每日通报"):
            with self.subTest(title=title):
                transport = _Transport()
                _sender(transport).send_notice(
                    chat_id="oc_1", card=_card(title, "回落"), dedupe_key="k"
                )
                sent = json.loads(transport.messages[0]["content"])
                self.assertEqual(sent["header"]["title"]["content"], "[lingxi] 内测每日通报")

    def test_text_and_rejected_card_fallback_prefixed(self) -> None:
        transport = _Transport()
        _sender(transport).send_text(chat_id="oc_1", text="[BI Plus 运行告警] 故障", dedupe_key="k")
        self.assertEqual(
            json.loads(transport.messages[0]["content"])["text"], "[lingxi] 运行告警 故障"
        )
        transport = _Transport(card_response=_REJECTED)
        _sender(transport).send_notice(
            chat_id="oc_1", card=_card("标题", "[BI Plus 运行告警] 故障\n类型：x"), dedupe_key="k2"
        )
        self.assertEqual([m["msg_type"] for m in transport.messages], ["interactive", "text"])
        self.assertEqual(
            json.loads(transport.messages[1]["content"])["text"], "[lingxi] 运行告警 故障\n类型：x"
        )

    def test_private_notice_title_unchanged(self) -> None:
        calls: list[dict] = []

        class PrivateSender:
            def send_notice(self, **kwargs) -> None:
                calls.append(kwargs)

            def send_text(self, **kwargs) -> None:
                calls.append(kwargs)

        send_catalog_notice(
            PrivateSender(), default_content_catalog(), "ou_1", "gateway.suspended", {}, "d1"
        )
        self.assertEqual(len(calls), 1)
        if "card" in calls[0]:
            self.assertNotIn("[lingxi]", calls[0]["card"].title)
            self.assertNotIn("[lingxi]", calls[0]["card"].fallback_text)
        else:
            self.assertNotIn("[lingxi]", calls[0]["text"])


class AlertTimeTests(unittest.TestCase):
    def test_same_day_and_cross_day(self) -> None:
        self.assertEqual(
            format_utc_with_beijing(datetime(2026, 9, 28, 3, 15, 59, tzinfo=UTC)),
            "2026-09-28 03:15 UTC（北京 11:15）",
        )
        self.assertEqual(
            format_utc_with_beijing(datetime(2026, 9, 28, 17, 15, tzinfo=UTC)),
            "2026-09-28 17:15 UTC（北京 09-29 01:15）",
        )

    def test_naive_time_rejected(self) -> None:
        with self.assertRaises(ValueError):
            format_utc_with_beijing(datetime(2026, 9, 28, 3, 15))

    def _notice(self, at: datetime) -> AlertNotice:
        return AlertNotice(
            action=NoticeAction.ALERT,
            kind=AlertKind.PROCESS_INACTIVE,
            scope="scheduler",
            observed_at=at,
            count=1,
            trace_id=None,
            dedupe_key="d",
        )

    def test_card_and_fallback_text_carry_both_times(self) -> None:
        notice = self._notice(datetime(2026, 9, 28, 17, 15, tzinfo=UTC))
        self.assertEqual(
            alert_card_values(notice)["observed_at"], "2026-09-28 17:15 UTC（北京 09-29 01:15）"
        )
        self.assertIn("时间：2026-09-28 17:15 UTC（北京 09-29 01:15）", notice.text)
        card = alert_notice_card(notice)
        self.assertIsNotNone(card)
        body = card.to_payload()["body"]["elements"][0]["content"]
        # 卡片 markdown 会把连字符转义，飞书显示时还原。
        self.assertIn("**时间**：2026\\-09\\-28 17:15 UTC（北京 09\\-29 01:15）", body)


if __name__ == "__main__":
    unittest.main()
