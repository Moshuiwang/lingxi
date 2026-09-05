"""#585 用户可见文案外置覆盖：叠加、四类坏文件整份拒绝、摘要与校验命令。"""

from __future__ import annotations

import io
import logging
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from lingxi.config.content import (
    REQUIRED_CARD_KEYS,
    REQUIRED_TEXT_KEYS,
    ContentCatalog,
    ContentRenderError,
    ContentValidationError,
)
from lingxi.config.content_check import EXIT_OK, EXIT_REJECTED, EXIT_USAGE
from lingxi.config.content_check import main as content_check_main
from lingxi.config.content_override import (
    CONTENT_OVERRIDE_PATH_ENV,
    REASON_INVALID_TOML,
    REASON_INVALID_VALUE,
    REASON_PLACEHOLDER_MISMATCH,
    REASON_UNKNOWN_KEY,
    REASON_UNKNOWN_SECTION,
    REASON_UNSAFE_TEXT,
    ContentOverrideError,
    load_content_source,
    log_content_source,
    parse_override_path,
)

# 逐字取自 content.toml 的两个键：一个带两个占位符、一个不带。挑固定键而不是
# 「第一个键」，是为了让占位符集合比对这条断言不会因为键顺序调整而悄悄失效。
KEY_WITH_PLACEHOLDERS = "onboarding.completed"
KEY_WITHOUT_PLACEHOLDERS = "gateway.new_session"

GOOD_OVERRIDE = """
[texts]
"onboarding.completed" = "已经开通好了。可用范围——公司：{company_name}；职能：{function_name}。"
"gateway.new_session" = "新会话开好了，直接问吧。"
"""


def _write(directory: Path, body: str, name: str = "content.override.toml") -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


class OverrideOverlayTest(unittest.TestCase):
    """覆盖文件合法时：正文换掉，版本、卡片与键集合原样。"""

    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.base = ContentCatalog.from_file()

    def test_a_valid_override_replaces_only_the_named_texts(self) -> None:
        source = load_content_source(_write(self.directory, GOOD_OVERRIDE))
        self.assertIsNone(source.rejection)
        self.assertEqual(
            source.catalog.text(KEY_WITHOUT_PLACEHOLDERS).text, "新会话开好了，直接问吧。"
        )
        rendered = source.catalog.text(
            KEY_WITH_PLACEHOLDERS, company_name="甲公司", function_name="财务"
        )
        self.assertIn("已经开通好了", rendered.text)
        self.assertIn("甲公司", rendered.text)

    def test_the_catalog_version_is_not_touched_by_an_override(self) -> None:
        """``[meta] version`` 是镜像内文案的追溯依据，外置覆盖不得改动它。

        这条不是形式主义：`content_version` 会随投递与审计事件落库，如果外置
        文件能改它，那个字段就会同时描述两种来源、彻底失去追溯价值。
        """
        source = load_content_source(_write(self.directory, GOOD_OVERRIDE))
        self.assertEqual(source.catalog.version, self.base.version)

    def test_cards_and_key_registry_survive_an_override(self) -> None:
        source = load_content_source(_write(self.directory, GOOD_OVERRIDE))
        self.assertEqual(set(source.catalog.text_keys()), set(REQUIRED_TEXT_KEYS))
        self.assertEqual(set(source.catalog.card_keys()), set(REQUIRED_CARD_KEYS))

    def test_untouched_keys_stay_byte_identical(self) -> None:
        source = load_content_source(_write(self.directory, GOOD_OVERRIDE))
        overridden = {KEY_WITH_PLACEHOLDERS, KEY_WITHOUT_PLACEHOLDERS}
        for key in REQUIRED_TEXT_KEYS:
            if key in overridden:
                continue
            with self.subTest(key=key):
                self.assertEqual(source.catalog.text_template(key), self.base.text_template(key))

    def test_the_source_reports_path_digest_and_overridden_keys(self) -> None:
        path = _write(self.directory, GOOD_OVERRIDE)
        source = load_content_source(path)
        self.assertEqual(source.override_path, str(path))
        self.assertEqual(source.override_keys, (KEY_WITHOUT_PLACEHOLDERS, KEY_WITH_PLACEHOLDERS))
        self.assertEqual(len(source.digest), 12)
        self.assertNotEqual(source.digest, load_content_source(None).digest)


class OverrideRejectionTest(unittest.TestCase):
    """四类坏文件：整份忽略、退回镜像内文案，只留原因码。"""

    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.base = ContentCatalog.from_file()

    def _rejection(self, body: str) -> str:
        path = _write(self.directory, body)
        with self.assertLogs("lingxi.config.content_override", level=logging.ERROR) as logs:
            source = load_content_source(path)
        self.assertNotIn("拒绝的正文", "\n".join(logs.output))
        self.assertEqual(source.override_keys, ())
        # 退回镜像内文案：全部键逐字与随包发布的那份相同。
        for key in REQUIRED_TEXT_KEYS:
            self.assertEqual(source.catalog.text_template(key), self.base.text_template(key))
        self.assertIsNotNone(source.rejection)
        return str(source.rejection)

    def test_broken_toml_is_rejected(self) -> None:
        self.assertEqual(self._rejection("[texts\n"), REASON_INVALID_TOML)

    def test_an_unknown_key_is_rejected(self) -> None:
        self.assertEqual(self._rejection('[texts]\n"onboarding.nope" = "x"\n'), REASON_UNKNOWN_KEY)

    def test_a_top_level_table_other_than_texts_is_rejected(self) -> None:
        """`[meta]`/`[cards]` 不可外置：版本号与卡片结构只随镜像发布。"""
        self.assertEqual(
            self._rejection('[meta]\nversion = "9999-01-01"\n'), REASON_UNKNOWN_SECTION
        )
        self.assertEqual(
            self._rejection('[cards]\n[cards."query.status"]\ntitle = "x"\n'),
            REASON_UNKNOWN_SECTION,
        )

    def test_a_different_placeholder_set_is_rejected(self) -> None:
        """rc25 抓到的 P1：占位符没被传值 → 渲染必然失败且一次性标志被烧掉。"""
        self.assertEqual(
            self._rejection('[texts]\n"onboarding.completed" = "开通好了。"\n'),
            REASON_PLACEHOLDER_MISMATCH,
        )

    def test_an_extra_placeholder_is_rejected_too(self) -> None:
        self.assertEqual(
            self._rejection('[texts]\n"gateway.new_session" = "新会话 {trace}。"\n'),
            REASON_PLACEHOLDER_MISMATCH,
        )

    def test_text_hitting_the_content_safety_rules_is_rejected(self) -> None:
        self.assertEqual(
            self._rejection('[texts]\n"gateway.new_session" = "新会话已开，预计剩余 3 分钟。"\n'),
            REASON_UNSAFE_TEXT,
        )

    def test_a_non_string_value_is_rejected(self) -> None:
        self.assertEqual(
            self._rejection('[texts]\n"gateway.new_session" = 3\n'), REASON_INVALID_VALUE
        )

    def test_an_empty_value_is_rejected(self) -> None:
        self.assertEqual(
            self._rejection('[texts]\n"gateway.new_session" = ""\n'), REASON_INVALID_VALUE
        )

    def test_one_bad_key_discards_the_whole_file_including_the_good_ones(self) -> None:
        """否定断言：不做"跳过坏的那条、用剩下的"。

        部分生效会让运维以为改动全部落地，而用户看到的是两版文案的混合——比
        整份退回难排查得多。
        """
        body = (
            "[texts]\n"
            '"gateway.new_session" = "这条本身合法。"\n'
            '"onboarding.completed" = "缺占位符。"\n'
        )
        self.assertEqual(self._rejection(body), REASON_PLACEHOLDER_MISMATCH)


class NoOverrideIsZeroChangeTest(unittest.TestCase):
    """未配变量或文件缺失：与随镜像发布的行为逐字相同，且不告警。"""

    def setUp(self) -> None:
        self.base = ContentCatalog.from_file()

    def _assert_identical_to_image(self, catalog: ContentCatalog) -> None:
        self.assertEqual(catalog.version, self.base.version)
        for key in REQUIRED_TEXT_KEYS:
            self.assertEqual(catalog.text_template(key), self.base.text_template(key))
        self.assertEqual(set(catalog.card_keys()), set(REQUIRED_CARD_KEYS))

    def test_unset_variable_is_byte_identical(self) -> None:
        source = load_content_source(None)
        self._assert_identical_to_image(source.catalog)
        self.assertIsNone(source.rejection)
        self.assertIsNone(source.override_path)
        self.assertEqual(source.override_keys, ())

    def test_a_missing_file_is_not_a_failure_and_does_not_alert(self) -> None:
        """删文件正是登记在案的回滚手段，不该每次回滚都刷一条假告警。"""
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with self.assertNoLogs("lingxi.config.content_override", level=logging.WARNING):
            source = load_content_source(directory / "never-written.toml")
        self._assert_identical_to_image(source.catalog)
        self.assertIsNone(source.rejection)
        self.assertEqual(source.digest, load_content_source(None).digest)

    def test_the_digest_only_changes_when_content_changes(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        first = load_content_source(_write(directory, GOOD_OVERRIDE, "a.toml"))
        same = load_content_source(_write(directory, GOOD_OVERRIDE, "b.toml"))
        other = load_content_source(
            _write(directory, '[texts]\n"gateway.new_session" = "别的说法。"\n', "c.toml")
        )
        self.assertEqual(first.digest, same.digest)
        self.assertNotEqual(first.digest, other.digest)


class OverridePathParsingTest(unittest.TestCase):
    def test_blank_means_not_configured(self) -> None:
        self.assertIsNone(parse_override_path(None))
        self.assertIsNone(parse_override_path("   "))

    def test_whitespace_inside_the_value_is_refused_without_echoing_it(self) -> None:
        with self.assertRaises(ValueError) as raised:
            parse_override_path("/etc/lingxi/runtime/content override.toml")
        message = str(raised.exception)
        self.assertIn(CONTENT_OVERRIDE_PATH_ENV, message)
        self.assertNotIn("content override.toml", message)


class CatalogOverlayApiTest(unittest.TestCase):
    """``ContentCatalog.with_text_overrides`` 自身的失败关闭断言。"""

    def setUp(self) -> None:
        self.base = ContentCatalog.from_file()

    def test_unknown_key_raises(self) -> None:
        with self.assertRaises(ContentValidationError):
            self.base.with_text_overrides({"onboarding.nope": "x"})

    def test_placeholder_mismatch_raises(self) -> None:
        with self.assertRaises(ContentRenderError):
            self.base.with_text_overrides({KEY_WITH_PLACEHOLDERS: "没有占位符。"})

    def test_the_original_catalog_is_not_mutated(self) -> None:
        before = self.base.text_template(KEY_WITHOUT_PLACEHOLDERS)
        self.base.with_text_overrides({KEY_WITHOUT_PLACEHOLDERS: "换掉了。"})
        self.assertEqual(self.base.text_template(KEY_WITHOUT_PLACEHOLDERS), before)


class ContentCheckCommandTest(unittest.TestCase):
    """校验命令与运行时判据同源：这里过了就不会在线上被拒。"""

    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _run(self, body: str | None, *, name: str = "o.toml") -> tuple[int, str, str]:
        path = self.directory / name if body is None else _write(self.directory, body, name)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = content_check_main([str(path)])
        return code, out.getvalue(), err.getvalue()

    def test_a_valid_file_exits_zero_and_lists_the_keys(self) -> None:
        code, out, _ = self._run(GOOD_OVERRIDE)
        self.assertEqual(code, EXIT_OK)
        self.assertIn(KEY_WITH_PLACEHOLDERS, out)
        self.assertIn(KEY_WITHOUT_PLACEHOLDERS, out)

    def test_each_bad_file_exits_non_zero_with_its_reason(self) -> None:
        cases = {
            "[texts\n": REASON_INVALID_TOML,
            '[texts]\n"onboarding.nope" = "x"\n': REASON_UNKNOWN_KEY,
            '[meta]\nversion = "9999-01-01"\n': REASON_UNKNOWN_SECTION,
            '[texts]\n"onboarding.completed" = "少了占位符。"\n': REASON_PLACEHOLDER_MISMATCH,
            '[texts]\n"gateway.new_session" = "预计剩余 3 分钟。"\n': REASON_UNSAFE_TEXT,
        }
        for index, (body, reason) in enumerate(cases.items()):
            with self.subTest(reason=reason):
                code, _, err = self._run(body, name=f"bad-{index}.toml")
                self.assertEqual(code, EXIT_REJECTED)
                self.assertIn(reason, err)

    def test_the_command_and_the_runtime_reject_exactly_the_same_files(self) -> None:
        """回归：空正文只被目录自身的失败关闭校验拦下。

        校验命令一度只跑分类判据那一段、不跑目录叠加，于是一份空正文的覆盖文件
        能拿到退出码 0，放上宿主机后却被运行时整份拒绝——正是本命令承诺不会发生
        的"这里过了、线上被拒"。两边现在共用 ``apply_override_document``。
        """
        from lingxi.config.content_override import load_content_source

        body = '[texts]\n"gateway.new_session" = ""\n'
        code, _, err = self._run(body, name="empty.toml")
        self.assertEqual(code, EXIT_REJECTED)
        self.assertIn(REASON_INVALID_VALUE, err)
        runtime = load_content_source(self.directory / "empty.toml")
        self.assertEqual(runtime.rejection, REASON_INVALID_VALUE)

    def test_the_command_never_prints_the_rejected_text(self) -> None:
        code, out, err = self._run('[texts]\n"gateway.new_session" = "预计剩余 3 分钟。"\n')
        self.assertEqual(code, EXIT_REJECTED)
        self.assertNotIn("预计剩余 3 分钟", out + err)

    def test_a_missing_file_and_a_wrong_argument_count_are_usage_errors(self) -> None:
        code, _, _ = self._run(None, name="absent.toml")
        self.assertEqual(code, EXIT_USAGE)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(content_check_main([]), EXIT_USAGE)


class _RecordingSender:
    """记录一次群发调用的假出站；构造参数一并留下，用于断言没有回显凭据。"""

    calls: list[dict[str, object]] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs

    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None:
        _RecordingSender.calls.append({"chat_id": chat_id, "text": text, "dedupe_key": dedupe_key})


class _RecordingAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, object]]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, fields))


class SchedulerAlertTest(unittest.TestCase):
    """被拒时 scheduler 发**一条**管理群告警；正常与未配群时都不发。"""

    def setUp(self) -> None:
        from lingxi.adapters import feishu_group_message
        from lingxi.apps.scheduler import content_override_notice
        from lingxi.config import content_override

        self.notice = content_override_notice
        self.audit = _RecordingAudit()
        _RecordingSender.calls = []
        original = feishu_group_message.FeishuGroupMessages
        feishu_group_message.FeishuGroupMessages = _RecordingSender
        self.addCleanup(setattr, feishu_group_message, "FeishuGroupMessages", original)
        self._content_override = content_override
        self._original_source = content_override.default_content_source

    def _stub_source(self, *, rejection: str | None) -> None:
        base = ContentCatalog.from_file()
        source = self._content_override.ContentSource(
            catalog=base,
            digest="abcdef012345",
            override_path="/etc/lingxi/runtime/content.override.toml",
            override_digest="0123456789ab",
            rejection=rejection,
        )
        self._content_override.default_content_source = lambda: source
        self.addCleanup(
            setattr, self._content_override, "default_content_source", self._original_source
        )

    @staticmethod
    def _config(chat_id: str | None) -> SimpleNamespace:
        return SimpleNamespace(
            admin_group_chat_id=chat_id,
            feishu_base_url="https://open.feishu.cn/open-apis",
            feishu_app_id="cli_test",
            feishu_app_secret="secret",
        )

    def test_a_rejection_sends_exactly_one_group_message(self) -> None:
        self._stub_source(rejection=REASON_UNSAFE_TEXT)
        self.notice.notify_content_override_rejection(self._config("oc_group"), audit=self.audit)
        self.assertEqual(len(_RecordingSender.calls), 1)
        call = _RecordingSender.calls[0]
        self.assertEqual(call["chat_id"], "oc_group")
        self.assertIn(REASON_UNSAFE_TEXT, str(call["text"]))
        self.assertIn(REASON_UNSAFE_TEXT, str(call["dedupe_key"]))
        self.assertIn("0123456789ab", str(call["dedupe_key"]))
        self.assertIn(
            ("content.override_rejected", {"reason": REASON_UNSAFE_TEXT}), self.audit.records
        )

    def test_a_healthy_load_sends_nothing_and_audits_nothing(self) -> None:
        self._stub_source(rejection=None)
        self.notice.notify_content_override_rejection(self._config("oc_group"), audit=self.audit)
        self.assertEqual(_RecordingSender.calls, [])
        self.assertEqual(self.audit.records, [])

    def test_without_an_admin_group_only_the_audit_is_left(self) -> None:
        """没配管理群不该让 scheduler 起不来，与本进程其它可选出站同一取舍。"""
        self._stub_source(rejection=REASON_INVALID_TOML)
        self.notice.notify_content_override_rejection(self._config(None), audit=self.audit)
        self.assertEqual(_RecordingSender.calls, [])
        self.assertEqual(
            self.audit.records, [("content.override_rejected", {"reason": REASON_INVALID_TOML})]
        )

    def test_a_failing_send_is_audited_and_does_not_escalate(self) -> None:
        self._stub_source(rejection=REASON_INVALID_TOML)

        class _Boom(_RecordingSender):
            def send_text(self, **_kwargs: object) -> None:
                raise RuntimeError("飞书拒绝")

        from lingxi.adapters import feishu_group_message

        feishu_group_message.FeishuGroupMessages = _Boom
        self.notice.notify_content_override_rejection(self._config("oc_group"), audit=self.audit)
        actions = [action for action, _ in self.audit.records]
        self.assertIn("content.override_alert_failed", actions)


class StartupLoggingTest(unittest.TestCase):
    def test_log_content_source_emits_one_line_with_digest_and_key_count(self) -> None:
        with self.assertLogs("lingxi.config.content_override", level=logging.INFO) as logs:
            source = log_content_source("probe")
        line = "\n".join(logs.output)
        self.assertIn("probe 内容目录", line)
        self.assertIn(source.digest, line)


class RejectionErrorTest(unittest.TestCase):
    def test_the_rejection_message_carries_the_reason_but_no_body(self) -> None:
        error = ContentOverrideError(REASON_UNSAFE_TEXT)
        self.assertEqual(error.reason, REASON_UNSAFE_TEXT)
        self.assertIn(REASON_UNSAFE_TEXT, str(error))


if __name__ == "__main__":
    unittest.main()
