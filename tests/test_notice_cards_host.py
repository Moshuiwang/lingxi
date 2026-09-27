"""宿主监控与发布代理的通知卡（#891 入口 #27–#31）：对照、转义与不导入仓库包。

两个脚本不能导入 ``lingxi`` 包，各自内置一份只用标准库的卡片构造。本文件钉住：

- 对照：同一输入下，脚本内置构造出的卡片 JSON 与共用类型
  :class:`lingxi.core.delivery.notice_card.NoticeCard` 的 ``to_payload()`` 逐键相同；
  共用类型构造时的拒绝规则（链接、提及、交互标签）同时替脚本把了一道关；
- 转义：主机名、错误原文、容器名等自由文本进卡片前转义，不产生可点链接、提及全体；
- 形态：故障与恢复色调不同；卡片无按钮、链接、回调；每张卡带等价纯文本；
  发布通知写「部署已完成」、不写「验收通过」；
- 静态：两个脚本的 import 语句里没有 ``lingxi``。

发送语义（明确拒绝才回落一次、结果不明不补发、成功才落状态）在两个脚本各自的
测试文件里按调用路径钉住。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
import unittest
from pathlib import Path

from lingxi.core.delivery.notice_card import (
    NoticeCard,
    assert_no_actionable_elements,
    escape_markdown,
)

ROOT = Path(__file__).resolve().parents[1]
HOST_SCRIPT = ROOT / "scripts" / "ops" / "host_health_alert.py"
AGENT_SCRIPT = ROOT / "deploy" / "release_pull_agent.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HOST = _load(HOST_SCRIPT, "host_health_alert_notice_cards_under_test")
AGENT = _load(AGENT_SCRIPT, "release_pull_agent_notice_cards_under_test")

#: 恶意自由文本：链接、markdown 链接、提及全体、HTML 标签、markdown 标点。
HOSTILE = "evil https://x.example/a [点我](https://x.example) <at id=all></at> **粗** _斜_"
AT_ALL = "<at id=all>"


def _shared_payload(title: str, tone: str, sections, text: str) -> dict:
    """用共用类型构造同一张卡：构造即执行全部拒绝规则。"""
    return NoticeCard(title=title, tone=tone, sections=sections, fallback_text=text).to_payload()


def _markdown_contents(payload: dict) -> str:
    return "\n".join(
        element["content"]
        for element in payload["body"]["elements"]
        if element.get("tag") == "markdown"
    )


class EscapeParityTests(unittest.TestCase):
    """内置转义与共用 ``escape_markdown`` 逐字相同。"""

    SAMPLES = (
        HOSTILE,
        "lingxi-gateway-1",
        "a\\b`c*d_e~f[g]h(i)j#k+l-m!n|o>p{q}r&s<t",
        "lark://open?x=1",
        "",
        12345,
    )

    def test_host_escape_matches_shared(self) -> None:
        for sample in self.SAMPLES:
            with self.subTest(sample=sample):
                self.assertEqual(HOST.escape_markdown(sample), escape_markdown(sample))

    def test_agent_escape_matches_shared(self) -> None:
        for sample in self.SAMPLES:
            with self.subTest(sample=sample):
                self.assertEqual(AGENT.escape_markdown(sample), escape_markdown(sample))

    def test_builder_payload_matches_shared_for_every_tone(self) -> None:
        sections = ((None, ("第一行", "第二行")), ("下一步", ("请处理。",)))
        for module in (HOST, AGENT):
            for tone in ("处理中", "完成", "需注意", "故障", "恢复"):
                with self.subTest(module=module.__name__, tone=tone):
                    self.assertEqual(
                        module.notice_card_payload("标题", tone, sections),
                        _shared_payload("标题", tone, sections, "纯文本"),
                    )


class HostContainerCardTests(unittest.TestCase):
    """#27 宿主容器告警 / 恢复。"""

    def _notice(self, action: str, reason: str, *, name: str = "lingxi-gateway-1", host="h1"):
        classification = HOST.Classification(name, reason, action == HOST.ACTION_ALERT)
        return HOST.render_notice(
            action, classification, host=host, now="2026-09-27T00:00:00+00:00"
        )

    def test_alert_and_recovery_match_shared_type(self) -> None:
        for action, reason in (
            (HOST.ACTION_ALERT, HOST.REASON_UNHEALTHY),
            (HOST.ACTION_ALERT, HOST.REASON_MISSING),
            (HOST.ACTION_ALERT, HOST.REASON_STOPPED),
            (HOST.ACTION_RECOVERY, HOST.REASON_OK),
        ):
            with self.subTest(action=action, reason=reason):
                notice = self._notice(action, reason, name=HOSTILE, host=HOSTILE)
                self.assertEqual(
                    notice.payload(),
                    _shared_payload(notice.title, notice.tone, notice.sections, notice.text),
                )

    def test_fallback_text_is_todays_plain_text(self) -> None:
        classification = HOST.Classification("c1", HOST.REASON_STOPPED, True)
        notice = HOST.render_notice(HOST.ACTION_ALERT, classification, host="h", now="t")
        self.assertEqual(
            notice.text, HOST.render_message(HOST.ACTION_ALERT, classification, host="h", now="t")
        )

    def test_fault_and_recovery_are_distinguishable(self) -> None:
        alert = self._notice(HOST.ACTION_ALERT, HOST.REASON_UNHEALTHY).payload()
        recovery = self._notice(HOST.ACTION_RECOVERY, HOST.REASON_OK).payload()
        self.assertEqual(alert["header"]["template"], "red")
        self.assertEqual(recovery["header"]["template"], "turquoise")
        self.assertNotEqual(alert["header"]["title"], recovery["header"]["title"])
        self.assertIn("恢复", recovery["header"]["title"]["content"])

    def test_free_text_is_escaped_and_card_has_no_actions(self) -> None:
        payload = self._notice(HOST.ACTION_ALERT, HOST.REASON_MISSING, name=HOSTILE, host=HOSTILE)
        body = payload.payload()
        assert_no_actionable_elements(body)
        content = _markdown_contents(body)
        self.assertNotIn("https://", content)
        self.assertNotIn(AT_ALL, content)
        self.assertNotIn("[点我](", content)
        self.assertIn(escape_markdown(HOSTILE), content)

    def test_next_step_follows_fields(self) -> None:
        payload = self._notice(HOST.ACTION_ALERT, HOST.REASON_UNHEALTHY).payload()
        elements = [e for e in payload["body"]["elements"] if e["tag"] == "markdown"]
        self.assertTrue(elements[-1]["content"].startswith("**下一步**"))
        self.assertIn("lingxi\\-gateway\\-1", elements[1]["content"])


class HostThresholdCardTests(unittest.TestCase):
    """#28 宿主阈值告警 / 恢复（资源、拉取代理单元、本地库）。"""

    def test_alert_and_recovery_match_shared_type(self) -> None:
        for action in (HOST.ACTION_ALERT, HOST.ACTION_RECOVERY):
            for category in ("资源监控", "宿主监控", "数据库监控"):
                with self.subTest(action=action, category=category):
                    notice = HOST.render_threshold_notice(
                        action,
                        label="拉取代理状态未知",
                        detail=HOSTILE,
                        host=HOSTILE,
                        now="2026-09-27T00:00:00+00:00",
                        category=category,
                    )
                    self.assertEqual(
                        notice.payload(),
                        _shared_payload(notice.title, notice.tone, notice.sections, notice.text),
                    )
                    self.assertEqual(
                        notice.text,
                        HOST.render_threshold_message(
                            action,
                            label="拉取代理状态未知",
                            detail=HOSTILE,
                            host=HOSTILE,
                            now="2026-09-27T00:00:00+00:00",
                            category=category,
                        ),
                    )

    def test_error_text_in_detail_is_escaped(self) -> None:
        notice = HOST.render_threshold_notice(
            HOST.ACTION_ALERT, label="磁盘用量", detail=HOSTILE, host="h", now="t"
        )
        content = _markdown_contents(notice.payload())
        self.assertNotIn("https://", content)
        self.assertNotIn(AT_ALL, content)
        self.assertEqual(notice.payload()["header"]["template"], "red")

    def test_recovery_is_distinguishable(self) -> None:
        notice = HOST.render_threshold_notice(
            HOST.ACTION_RECOVERY, label="磁盘用量", detail="", host="h", now="t"
        )
        payload = notice.payload()
        self.assertEqual(payload["header"]["template"], "turquoise")
        self.assertIn("恢复", payload["header"]["title"]["content"])

    def test_none_action_rejected(self) -> None:
        with self.assertRaises(ValueError):
            HOST.render_threshold_notice(HOST.ACTION_NONE, label="x", detail="", host="h", now="t")


HOST_INFO = {"host": HOSTILE, "environment": "production"}


class AgentCardTests(unittest.TestCase):
    """#29 发布结果通知、#30 代理自替换告警、#31 发布令牌到期提醒。"""

    def _assert_parity(self, notice) -> dict:
        self.assertIsInstance(notice, str)
        payload = notice.card_payload()
        self.assertEqual(
            payload, _shared_payload(notice.title, notice.tone, notice.sections, str(notice))
        )
        assert_no_actionable_elements(payload)
        content = _markdown_contents(payload)
        self.assertNotIn("https://", content)
        self.assertNotIn(AT_ALL, content)
        return payload

    def test_release_results_match_shared_type(self) -> None:
        cases = (
            ("verified", "完成"),
            ("recovered", "恢复"),
            ("downgrade_refused", "故障"),
            ("failed", "故障"),
            ("unknown", "需注意"),
            ("deploy_timeout", "需注意"),
            ("release_list_unavailable", "故障"),
            ("external_change_detected", "故障"),
        )
        for result, tone in cases:
            with self.subTest(result=result):
                notice = AGENT._alert_message(
                    HOST_INFO, "v2.6.1", "deploy", result, "plan-1", HOSTILE
                )
                self._assert_parity(notice)
                self.assertEqual(notice.tone, tone)
                self.assertIn("结果码：" + result, str(notice))

    def test_plain_text_is_unchanged(self) -> None:
        notice = AGENT._alert_message(
            {"host": "h", "environment": "stage"}, None, "deploy", "failed", None
        )
        self.assertEqual(
            str(notice),
            "主机：h\n环境：stage\n标签：未知\n阶段：deploy\n结果码：failed\n计划 ID：未知",
        )

    def test_verified_says_deployed_never_accepted(self) -> None:
        notice = AGENT._alert_message(HOST_INFO, "v2.6.1", "deploy", "verified", "plan-1")
        payload = self._assert_parity(notice)
        rendered = json.dumps(payload, ensure_ascii=False) + str(notice)
        self.assertIn("部署已完成", rendered)
        self.assertNotIn("验收通过", rendered)
        self.assertEqual(payload["header"]["template"], "green")

    def test_failure_and_recovery_are_distinguishable(self) -> None:
        failed = AGENT._alert_message(HOST_INFO, "v1.0.0", "deploy", "failed", None)
        recovered = AGENT._alert_message(HOST_INFO, "v1.0.0", "deploy", "recovered", None)
        self.assertEqual(failed.card_payload()["header"]["template"], "red")
        self.assertEqual(recovered.card_payload()["header"]["template"], "turquoise")
        self.assertIn("恢复", recovered.card_payload()["header"]["title"]["content"])

    def test_self_update_alerts(self) -> None:
        for result in ("agent_self_update_rejected", "agent_self_update_failed"):
            with self.subTest(result=result):
                notice = AGENT._alert_message(
                    HOST_INFO, "v2.6.1", "agent_self_update", result, None
                )
                payload = self._assert_parity(notice)
                self.assertEqual(payload["header"]["template"], "red")
                self.assertIn("自替换", payload["header"]["title"]["content"])

    def test_pat_alerts(self) -> None:
        cases = (
            (
                "pat_expiring",
                {"expires_on": "2026-10-01", "days_left": 4, "reason": None},
                "需注意",
            ),
            ("pat_expired", {"expires_on": "2026-09-01", "days_left": 0, "reason": None}, "故障"),
            (
                "pat_expiry_unknown",
                {"expires_on": None, "days_left": None, "reason": "schema"},
                "需注意",
            ),
            (
                "pat_expiry_recovered",
                {"expires_on": "2026-12-01", "days_left": 60, "reason": None},
                "恢复",
            ),
        )
        for code, observation, tone in cases:
            with self.subTest(code=code):
                notice = AGENT._pat_alert_message(HOST_INFO, observation, code)
                self._assert_parity(notice)
                self.assertEqual(notice.tone, tone)
                self.assertIn("结果码：" + code, str(notice))


class NoLingxiImportTests(unittest.TestCase):
    """两个脚本不得导入 ``lingxi`` 包（静态检查全部 import 语句，含函数内）。"""

    def test_scripts_do_not_import_lingxi(self) -> None:
        for path in (HOST_SCRIPT, AGENT_SCRIPT):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    names.append(node.module or "")
            with self.subTest(path=path.name):
                self.assertTrue(names)
                self.assertFalse([name for name in names if name.split(".")[0] == "lingxi"])


if __name__ == "__main__":
    unittest.main()
