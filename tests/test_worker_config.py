"""问答留存语料开关的构造期判定（Issue #664）。

只测配置层：``_qa_corpus_retention`` 的四种取值、``load_config`` 把它接进配置、以及
「任一通道开启即构造收集器」的判据。写入点与真库断言见 ``tests/test_qa_corpus_wiring.py``。
"""

from __future__ import annotations

import unittest

from lingxi.apps.worker.config import (
    CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VALUE,
    CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VAR,
    CONTENT_CAPTURE_FLAG_VAR,
    QA_CORPUS_RETENTION_VAR,
    WorkerConfig,
    WorkerConfigError,
    _qa_corpus_retention,
    load_config,
)

_READ_ONLY_TOOL = "mcp__query__list_metrics"


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "LINGXI_WORKER_QUESTION": "上周活跃用户数是多少？",
        "LINGXI_WORKER_READONLY_TOOLS": _READ_ONLY_TOOL,
        "LINGXI_WORKER_TRACE_ID": "01J0000000000000000TEST000",
        "LINGXI_QUERY_MCP_ENDPOINT": "https://mcp.example.invalid/query",
    }
    env.update(overrides)
    return env


class QaCorpusRetentionSwitchTests(unittest.TestCase):
    """四种取值：未配 / 精确 "1" / 其它值 / 空白。"""

    def test_unset_is_disabled(self) -> None:
        self.assertFalse(_qa_corpus_retention({}))

    def test_exact_one_enables(self) -> None:
        self.assertTrue(_qa_corpus_retention({QA_CORPUS_RETENTION_VAR: "1"}))

    def test_anything_other_than_exact_one_fails_startup(self) -> None:
        """错配不是未配：``true`` / ``yes`` / ``0`` 都不能被悄悄当成关闭。

        **变异验红**：把 ``_exact_flag`` 的 ``flag != "1"`` 放宽成 ``flag.lower() not in
        {"1", "true"}`` 之类，本用例必须变红。
        """
        for value in ("true", "yes", "0", "on", " 1x", "１"):
            with self.subTest(value=value):
                with self.assertRaises(WorkerConfigError):
                    _qa_corpus_retention({QA_CORPUS_RETENTION_VAR: value})

    def test_blank_is_treated_as_unset(self) -> None:
        self.assertFalse(_qa_corpus_retention({QA_CORPUS_RETENTION_VAR: "   "}))

    def test_error_message_names_the_variable_without_echoing_the_value(self) -> None:
        with self.assertRaises(WorkerConfigError) as caught:
            _qa_corpus_retention({QA_CORPUS_RETENTION_VAR: "secret-looking-value"})
        self.assertIn(QA_CORPUS_RETENTION_VAR, str(caught.exception))
        self.assertNotIn("secret-looking-value", str(caught.exception))


class LoadConfigTests(unittest.TestCase):
    def test_default_env_leaves_the_channel_off(self) -> None:
        config = load_config(_env())
        self.assertFalse(config.qa_corpus_retention_enabled)
        self.assertFalse(config.captures_raw_content)

    def test_declared_switch_turns_the_channel_on(self) -> None:
        config = load_config(_env(**{QA_CORPUS_RETENTION_VAR: "1"}))
        self.assertTrue(config.qa_corpus_retention_enabled)
        self.assertTrue(config.captures_raw_content)

    def test_a_garbage_value_fails_config_loading(self) -> None:
        with self.assertRaises(WorkerConfigError):
            load_config(_env(**{QA_CORPUS_RETENTION_VAR: "enabled"}))

    def test_the_corpus_switch_is_independent_of_the_innertest_capture_switch(self) -> None:
        """两条通道各自独立：内测采集开着不代表语料开着，反之亦然；收集器任一开就要。"""
        capture_only = load_config(
            _env(
                **{
                    CONTENT_CAPTURE_FLAG_VAR: "1",
                    CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VAR: (
                        CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VALUE
                    ),
                }
            )
        )
        self.assertTrue(capture_only.innertest_content_capture_enabled)
        self.assertFalse(capture_only.qa_corpus_retention_enabled)
        self.assertTrue(capture_only.captures_raw_content)

        corpus_only = load_config(_env(**{QA_CORPUS_RETENTION_VAR: "1"}))
        self.assertFalse(corpus_only.innertest_content_capture_enabled)
        self.assertTrue(corpus_only.captures_raw_content)


class DirectConstructionTests(unittest.TestCase):
    def test_directly_constructed_config_defaults_to_off(self) -> None:
        config = WorkerConfig(
            question="q",
            read_only_tools=(_READ_ONLY_TOOL,),
            trace_id="01J0000000000000000TEST000",
            turn_timeout_seconds=1.0,
        )
        self.assertFalse(config.qa_corpus_retention_enabled)
        self.assertFalse(config.captures_raw_content)


if __name__ == "__main__":
    unittest.main()
