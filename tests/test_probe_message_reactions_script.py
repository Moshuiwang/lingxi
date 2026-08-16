"""S-A-07 受控验收夹具（Issue #175/#185 验收缺口）：``scripts/probe_message_reactions.py``
的纯逻辑单测。

只测不需要真实飞书的部分：摘要折叠（不含用户标识）、分页、退出码与输出形状。
真正回读一条真实消息属 L4a，留给 biai-stage/Bot-Test 受控执行。

加载方式照抄既有先例 ``tests/test_replay_inbound_event_script.py``。
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / "scripts" / "probe_message_reactions.py"

FAKE_OPEN_ID = "ou_fake_operator_do_not_print"

_LARK_MODULE_NAMES = ("lark_oapi", "lark_oapi.api", "lark_oapi.api.im", "lark_oapi.api.im.v1")


def _install_fake_lark_sdk(testcase: unittest.TestCase) -> None:
    """按 ``tests/test_worker_entry.py`` 的假 SDK 先例，为分页请求构造提供最小
    ``ListMessageReactionRequest`` 桩——这些测试必须能在没装 lark-oapi 的机器上跑。
    """

    class _Request:
        def __init__(self) -> None:
            self.message_id = None
            self.page_size = None
            self.page_token = None

    class _Builder:
        def __init__(self) -> None:
            self._request = _Request()

        def message_id(self, value):
            self._request.message_id = value
            return self

        def page_size(self, value):
            self._request.page_size = value
            return self

        def page_token(self, value):
            self._request.page_token = value
            return self

        def build(self):
            return self._request

    class ListMessageReactionRequest:
        @staticmethod
        def builder() -> "_Builder":
            return _Builder()

    saved = {name: sys.modules.get(name) for name in _LARK_MODULE_NAMES}
    for name in _LARK_MODULE_NAMES:
        sys.modules[name] = types.ModuleType(name)
    sys.modules["lark_oapi.api.im.v1"].ListMessageReactionRequest = ListMessageReactionRequest

    def restore() -> None:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    testcase.addCleanup(restore)


def _load_script():
    spec = importlib.util.spec_from_file_location("probe_message_reactions_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _reaction(emoji: str, operator_type: str):
    return types.SimpleNamespace(
        reaction_id="rid_1",
        action_time=0,
        reaction_type=types.SimpleNamespace(emoji_type=emoji),
        operator=types.SimpleNamespace(operator_type=operator_type, operator_id=FAKE_OPEN_ID),
    )


class _FakeResponse:
    def __init__(self, *, items, has_more=False, page_token=None, ok=True):
        self._ok = ok
        self.code = 0 if ok else 99991672
        self.msg = "" if ok else "permission denied"
        self.data = types.SimpleNamespace(items=items, has_more=has_more, page_token=page_token)

    def success(self):
        return self._ok

    def get_log_id(self):
        return "lgid_test"


class _FakeClient:
    """形状对齐 ``client.im.v1.message_reaction.list(request)`` 的最小桩。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []
        resource = types.SimpleNamespace(list=self._list)
        self.im = types.SimpleNamespace(v1=types.SimpleNamespace(message_reaction=resource))

    def _list(self, request):
        self.requests.append(request)
        return self._responses.pop(0)


class SummarizeReactionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_script()

    def test_counts_are_grouped_by_emoji_and_operator_type(self) -> None:
        summary = self.module.summarize_reactions(
            [_reaction("OnIt", "app"), _reaction("OnIt", "user"), _reaction("THUMBSUP", "app")]
        )
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["app_reactions"], 2)
        self.assertEqual(
            summary["by_emoji_and_operator_type"],
            {"OnIt/app": 1, "OnIt/user": 1, "THUMBSUP/app": 1},
        )

    def test_summary_never_contains_the_operator_id(self) -> None:
        """输出不含任何用户标识——这是脚本的验收纪律，不是顺手的实现细节。"""

        summary = self.module.summarize_reactions([_reaction("OnIt", "app")])
        self.assertNotIn(FAKE_OPEN_ID, json.dumps(summary, ensure_ascii=False))


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_script()
        _install_fake_lark_sdk(self)

    def _run(self, argv, client):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = self.module.main(argv, client=client)
        return code, out.getvalue(), err.getvalue()

    def test_prints_a_json_summary_and_only_the_message_id_suffix(self) -> None:
        client = _FakeClient([_FakeResponse(items=[_reaction("OnIt", "app")])])
        code, out, _ = self._run(["om_1234567890abc"], client)
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["app_reactions"], 1)
        self.assertNotIn("om_1234567890abc", out, "整段消息标识不得进输出")
        self.assertTrue(payload["message_id_suffix"].endswith("90abc"))
        self.assertNotIn(FAKE_OPEN_ID, out)

    def test_zero_reactions_is_a_successful_read(self) -> None:
        client = _FakeClient([_FakeResponse(items=[])])
        code, out, _ = self._run(["om_x"], client)
        self.assertEqual(code, 0, "回读到 0 个反应是有效结论，不是失败")
        self.assertEqual(json.loads(out)["total"], 0)

    def test_pagination_is_followed_until_has_more_is_false(self) -> None:
        client = _FakeClient(
            [
                _FakeResponse(items=[_reaction("OnIt", "app")], has_more=True, page_token="p2"),
                _FakeResponse(items=[_reaction("OnIt", "app")]),
            ]
        )
        code, out, _ = self._run(["om_x"], client)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["total"], 2)
        self.assertEqual(len(client.requests), 2)

    def test_api_failure_exits_1_and_reports_the_platform_error(self) -> None:
        client = _FakeClient([_FakeResponse(items=[], ok=False)])
        code, _, err = self._run(["om_x"], client)
        self.assertEqual(code, 1)
        self.assertIn("99991672", err)
        self.assertIn("lgid_test", err)

    def test_missing_credentials_exit_2_without_touching_the_network(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with patch.dict("os.environ", {}, clear=True):
            with redirect_stdout(out), redirect_stderr(err):
                code = self.module.main(["om_x"], client=None)
        self.assertEqual(code, 2)
        self.assertIn("LINGXI_GATEWAY_APP_ID", err.getvalue())


if __name__ == "__main__":
    unittest.main()
