"""权限变化通知的正文渲染与发送编排（Issue #156 / S-C-03b）。

承担 `V-权限-08` 刷新侧「撤权 → 用户得到范围已更新的告知」这一半的纯逻辑面，以及产品
负责人 2026-08-18 裁定 2/4（范围式文案、有限重试 + 审计）的落点。

否定面（合同的"不得 / 不允许"必须有对应否定测试）：

- **权限文本为空时渲染不出"你还有权限"**，反之亦然——文案种类由权限文本自己决定，
  没有让调用方直接指定的入口；
- 通配公司**不把 ``*`` 给用户看**；
- 发送失败**不抛异常、不改变任何权限状态**，只记审计与计数；
- 重试**次数有限**，且每一次**携带同一个去重键**；
- 审计里**没有正文**，只有内容键与版本；
- 渲染失败（权限文本读不懂）**照常上抛**，不被折成任何一种文案。
"""

from __future__ import annotations

import unittest

from lingxi.config.content import REQUIRED_TEXT_KEYS, ContentSafetyError, default_content_catalog
from lingxi.core.permission.notification import (
    CONTENT_KEY_ALL_COMPANIES,
    CONTENT_KEY_RANGE_REVOKED,
    CONTENT_KEY_RANGE_UPDATED,
    DEFAULT_NOTICE_ATTEMPTS,
    NoticeKind,
    PermissionNoticeDispatcher,
    describe_scope,
    notice_dedupe_key,
    render_scope_notice,
)

USER = "usr_01JQZX3M5N7P9R1T3V5W7Y9A0B"
OPEN_ID = "ou_fake_open_id_for_tests"
GRANTED = '{"1011":["日活","收入"],"1012":["日活","收入"]}'
WILDCARD = '{"*":["日活"]}'
REVOKED = "{}"


class FakeSender:
    """按脚本决定第几次成功：``failures`` 是"前几次抛异常"。"""

    def __init__(self, *, failures: int = 0, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._failures = failures
        self._error = error or RuntimeError("注入的发送失败")

    def send_text(self, *, open_id: str, text: str, dedupe_key: str) -> None:
        self.calls.append({"open_id": open_id, "text": text, "dedupe_key": dedupe_key})
        if len(self.calls) <= self._failures:
            raise self._error


class FakeAudit:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.entries.append((action, dict(fields)))

    def fields_for(self, action: str) -> list[dict]:
        return [fields for name, fields in self.entries if name == action]


class _CodedError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ContentRegistrationTest(unittest.TestCase):
    """三个键必须登记在案，并且真的能渲染出来（仓库纪律：不允许无消费方的键）。"""

    def test_the_three_keys_are_registered(self) -> None:
        for key in (CONTENT_KEY_RANGE_UPDATED, CONTENT_KEY_RANGE_REVOKED, CONTENT_KEY_ALL_COMPANIES):
            with self.subTest(key):
                self.assertIn(key, REQUIRED_TEXT_KEYS)

    def test_the_approved_wording_is_verbatim(self) -> None:
        """产品负责人 2026-08-18 **逐字批准**的两条文案，一个字都不改。"""

        catalog = default_content_catalog()

        self.assertEqual(
            catalog.text(CONTENT_KEY_RANGE_UPDATED, company_name="甲", function_name="乙").text,
            "你的数据查询可用范围已更新。当前可用范围——公司：甲；职能：乙。"
            "你可以直接发起一次查询验证，如有疑问请联系管理员。",
        )
        self.assertEqual(
            catalog.text(CONTENT_KEY_RANGE_REVOKED).text,
            "你的数据查询可用范围已更新，当前暂无可用的数据范围。如有疑问请联系管理员。",
        )

    def test_the_wording_does_not_trip_the_user_visible_safety_filter(self) -> None:
        """两条文案刻意避开了"权限不足 / 无权限 / 没有权限"这类被禁的固定表达。"""

        for key in (CONTENT_KEY_RANGE_REVOKED, CONTENT_KEY_ALL_COMPANIES):
            with self.subTest(key):
                text = default_content_catalog().text(key).text
                for marker in ("权限不足", "无权限", "没有权限"):
                    self.assertNotIn(marker, text)


class RenderTest(unittest.TestCase):
    def test_a_granted_document_renders_the_updated_notice(self) -> None:
        notice = render_scope_notice(GRANTED)

        self.assertEqual(notice.kind, NoticeKind.RANGE_UPDATED)
        self.assertEqual(notice.key, CONTENT_KEY_RANGE_UPDATED)
        self.assertIn("1011、1012", notice.text)
        # 值列表排序恒定（``lookup_metrics`` 的存在性分支已排序去重），因此同一份权限
        # 永远渲染出同一串文字——重试与重复投递比对的前提。
        self.assertIn("收入、日活", notice.text)

    def test_an_empty_document_renders_the_revoked_notice(self) -> None:
        notice = render_scope_notice(REVOKED)

        self.assertEqual(notice.kind, NoticeKind.RANGE_REVOKED)
        self.assertEqual(notice.key, CONTENT_KEY_RANGE_REVOKED)

    def test_a_document_whose_only_company_has_no_metric_is_revoked_too(self) -> None:
        """否定断言：``{"1011":[]}`` 一样是"没有可用范围"——MCP 侧查不到任何指标。"""

        self.assertEqual(render_scope_notice('{"1011":[]}').kind, NoticeKind.RANGE_REVOKED)

    def test_the_wildcard_is_never_shown_to_the_user(self) -> None:
        notice = render_scope_notice(WILDCARD)

        self.assertNotIn("*", notice.text)
        self.assertIn("全部公司", notice.text)

    def test_the_wildcard_wins_over_specific_companies(self) -> None:
        """通配已经覆盖了具体公司；并列展示会给用户一句自相矛盾的范围。"""

        companies, _ = describe_scope({"*": ["日活"], "1011": ["日活"]})

        self.assertEqual(companies, "全部公司")

    def test_metric_names_are_passed_through_verbatim(self) -> None:
        """零归一：全半角与大小写在这条链上一个字符都不动。"""

        notice = render_scope_notice('{"1011":["ＯＴＴ","ott","OTT"]}')

        for metric in ("ＯＴＴ", "ott", "OTT"):
            self.assertIn(metric, notice.text)

    def test_an_unreadable_permission_document_raises(self) -> None:
        """否定断言：读不懂的权限文本**不**被折成任何一种文案。"""

        for broken in ("", "   ", "不是 JSON", "[1,2]"):
            with self.subTest(broken):
                with self.assertRaises(ValueError):
                    render_scope_notice(broken)

    def test_the_notice_audit_facts_carry_no_body(self) -> None:
        facts = render_scope_notice(GRANTED).audit_facts()

        self.assertEqual(set(facts), {"kind", "content_key", "content_version"})
        self.assertNotIn("日活", str(facts))


class DedupeKeyTest(unittest.TestCase):
    def test_the_key_binds_user_and_version(self) -> None:
        self.assertEqual(notice_dedupe_key(USER, 3), f"{USER}:3")

    def test_a_new_version_gets_a_new_key(self) -> None:
        self.assertNotEqual(notice_dedupe_key(USER, 3), notice_dedupe_key(USER, 4))

    def test_an_unbound_key_is_rejected(self) -> None:
        for user, version in (("", 1), ("   ", 1), (USER, True), (USER, "3")):
            with self.subTest(user=user, version=version):
                with self.assertRaises((ValueError, TypeError)):
                    notice_dedupe_key(user, version)  # type: ignore[arg-type]


class DispatcherTest(unittest.TestCase):
    def _dispatcher(self, sender, audit=None, **kwargs):
        self.waits: list[float] = []
        return PermissionNoticeDispatcher(
            sender=sender,
            audit=audit or FakeAudit(),
            sleep=self.waits.append,
            **kwargs,
        )

    def test_a_successful_notice_is_sent_once_and_audited(self) -> None:
        sender, audit = FakeSender(), FakeAudit()
        dispatcher = self._dispatcher(sender, audit)

        result = dispatcher.notify(
            user_id=USER, open_id=OPEN_ID, permission_version=3, permissions=GRANTED
        )

        self.assertTrue(result.delivered)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(self.waits, [], "首发不退避")
        sent = audit.fields_for("permission_notice.sent")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["kind"], "range_updated")
        self.assertEqual(sent[0]["content_key"], CONTENT_KEY_RANGE_UPDATED)

    def test_a_transient_failure_is_retried_with_the_same_uuid(self) -> None:
        sender, audit = FakeSender(failures=1), FakeAudit()
        dispatcher = self._dispatcher(sender, audit)

        result = dispatcher.notify(
            user_id=USER, open_id=OPEN_ID, permission_version=3, permissions=GRANTED
        )

        self.assertTrue(result.delivered)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(
            {call["dedupe_key"] for call in sender.calls},
            {f"{USER}:3"},
            "重试必须携带同一个去重键",
        )
        self.assertEqual(self.waits, [0.2])

    def test_the_retry_count_is_bounded(self) -> None:
        """否定断言：**重试有上限**，不会无限重试下去。"""

        sender, audit = FakeSender(failures=99), FakeAudit()
        dispatcher = self._dispatcher(sender, audit)

        result = dispatcher.notify(
            user_id=USER, open_id=OPEN_ID, permission_version=3, permissions=REVOKED
        )

        self.assertFalse(result.delivered)
        self.assertEqual(len(sender.calls), DEFAULT_NOTICE_ATTEMPTS)
        self.assertEqual(result.attempts, DEFAULT_NOTICE_ATTEMPTS)
        failed = audit.fields_for("permission_notice.failed")
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["error_code"], "RuntimeError")
        self.assertEqual(audit.fields_for("permission_notice.sent"), [])

    def test_a_final_failure_does_not_raise(self) -> None:
        """否定断言：通知失败**不阻塞权限生效**——它连一个异常都不往上抛。"""

        dispatcher = self._dispatcher(FakeSender(failures=99))

        result = dispatcher.notify(
            user_id=USER, open_id=OPEN_ID, permission_version=3, permissions=GRANTED
        )

        self.assertFalse(result.delivered)

    def test_the_adapter_error_code_is_preferred_over_the_exception_name(self) -> None:
        sender = FakeSender(failures=99, error=_CodedError("feishu_code_230002"))
        audit = FakeAudit()
        dispatcher = self._dispatcher(sender, audit)

        result = dispatcher.notify(
            user_id=USER, open_id=OPEN_ID, permission_version=3, permissions=GRANTED
        )

        self.assertEqual(result.error_code, "feishu_code_230002")
        self.assertEqual(audit.fields_for("permission_notice.failed")[0]["error_code"], "feishu_code_230002")

    def test_no_audit_entry_carries_the_body(self) -> None:
        sender, audit = FakeSender(), FakeAudit()

        self._dispatcher(sender, audit).notify(
            user_id=USER, open_id=OPEN_ID, permission_version=3, permissions=GRANTED
        )

        rendered = str(audit.entries)
        for secret in ("日活", "收入", "1011", OPEN_ID):
            self.assertNotIn(secret, rendered, "审计只记内容键与版本，不记正文与收件人")

    def test_a_render_failure_is_raised_not_swallowed(self) -> None:
        """否定断言：渲染失败是**本侧缺陷**，不能被吞成"这个人偶尔收不到通知"。"""

        sender = FakeSender()
        dispatcher = self._dispatcher(sender)

        with self.assertRaises(ValueError):
            dispatcher.notify(
                user_id=USER, open_id=OPEN_ID, permission_version=3, permissions="坏掉的文本"
            )
        self.assertEqual(sender.calls, [], "渲染失败时一次都不发")

    def test_a_company_name_that_trips_the_safety_filter_fails_loudly(self) -> None:
        """否定断言：撞上用户可见性检查的取值**响亮失败**，不会被发给用户。"""

        dispatcher = self._dispatcher(FakeSender())

        with self.assertRaises(ContentSafetyError):
            dispatcher.notify(
                user_id=USER,
                open_id=OPEN_ID,
                permission_version=3,
                permissions='{"权限不足":["日活"]}',
            )

    def test_a_missing_sleeper_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            PermissionNoticeDispatcher(sender=FakeSender(), audit=FakeAudit(), sleep=None)  # type: ignore[arg-type]

    def test_an_illegal_retry_cap_is_rejected(self) -> None:
        for value in (0, -1, True, "3"):
            with self.subTest(value):
                with self.assertRaises((ValueError, TypeError)):
                    PermissionNoticeDispatcher(
                        sender=FakeSender(),
                        audit=FakeAudit(),
                        sleep=lambda seconds: None,
                        max_attempts=value,  # type: ignore[arg-type]
                    )

    def test_the_dispatcher_cannot_touch_any_permission_state(self) -> None:
        """形状断言：**它手上只有发送口、审计口、退避与内容目录**。

        发布 outbox、就绪记录、用户状态一个端口都注入不进来，因此"通知失败顺手把发布
        状态改回去"这件事在装配上就写不出来——这条比在源码里搜关键字更难被绕过。
        """

        import inspect

        parameters = set(inspect.signature(PermissionNoticeDispatcher.__init__).parameters)

        self.assertEqual(
            parameters,
            {"self", "sender", "audit", "sleep", "catalog", "max_attempts", "backoff_seconds"},
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
