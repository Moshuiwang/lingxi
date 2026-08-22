"""最薄 worker 入口的装配断言（V-执行-18、19、20、22、23，以及 V-执行-17 经装配路径复验）。

对应 Issue #37。这里**不是**又测一遍 ``lingxi.core.execution``：判定与审计逻辑的断言在
``tests/test_execution_tool_gate.py``。本文件要回答的是另一个问题——**那些组件真的被
接上了吗**。Issue #29 交付的屏障在仓库里没有任何调用方，"组件存在 + 单测通过"并不等于
"屏障在生效"。

做法是给整条装配路径喂一个**按脚本驱动的假 SDK**：
``lingxi.apps.worker`` 照常构造 ``ToolPolicy`` / ``TurnAudit`` / ``ToolGateway``，照常经
``lingxi.adapters.claude_agent_session`` 把 matchers 装进 ``ClaudeAgentOptions.hooks``，
假 SDK 则从 ``options.hooks`` 里**找回**这些回调并真的调用它们，拿到 ``deny`` 才不执行。
因此删掉任何一段接线，假 SDK 都会照常"执行"越界工具，用例随之变红——这正是变异验证要的。

假 SDK 只能证明「本侧装配是通的」，**证明不了真实 SDK 会触发这些事件**；后者只有
`biai-stage` 的 L4a（V-执行-21）能回答，门禁表里登记为未验证。

不调用任何真实系统：无网络、无 MCP、无飞书、无模型额度、不读 ``.env``；凭据相关断言
只用固定假凭据探针。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest

READ_ONLY_TOOL = "mcp__query__list_metrics"
# 固定假凭据探针。形状刻意选成「16 字符以上且含数字」，这是自由文本脱敏唯一
# 与键名无关的结构性规则（V-审计-03）。真实凭据永远不进测试。
FAKE_CREDENTIAL = "LINGXI_FAKE_SECRET_a1b2c3d4e5f6g7h8"


# ---------------------------------------------------------------- 假 SDK 形状


class StubHookMatcher:
    def __init__(self, matcher=None, hooks=None, timeout=None) -> None:
        self.matcher = matcher
        self.hooks = hooks or []
        self.timeout = timeout


class StubAgentOptions:
    """刻意**不用** ``**kwargs``：适配器多传一个字段就是 TypeError。

    真实 ``ClaudeAgentOptions`` 是有固定字段的 dataclass，用 ``**kwargs`` 兜住一切的
    桩会把「字段名写错」这类问题藏起来，直到部署才暴露。
    """

    def __init__(
        self,
        *,
        allowed_tools=None,
        max_turns=None,
        disallowed_tools=None,
        hooks=None,
        mcp_servers=None,
        cwd=None,
        model=None,
        system_prompt=None,
        setting_sources=None,
        permission_mode=None,
        stderr=None,
        strict_mcp_config=None,
    ) -> None:
        self.allowed_tools = allowed_tools
        self.max_turns = max_turns
        self.disallowed_tools = disallowed_tools
        self.hooks = hooks
        self.mcp_servers = mcp_servers
        self.cwd = cwd
        self.model = model
        self.system_prompt = system_prompt
        self.setting_sources = setting_sources
        self.permission_mode = permission_mode
        self.stderr = stderr
        self.strict_mcp_config = strict_mcp_config


class StubTextBlock:
    def __init__(self, text) -> None:
        self.text = text


class StubThinkingBlock:
    def __init__(self, thinking="") -> None:
        self.thinking = thinking


class StubToolUseBlock:
    def __init__(self, id, name, input) -> None:  # noqa: A002 - 对齐 SDK 字段名
        self.id = id
        self.name = name
        self.input = input


class StubToolResultBlock:
    def __init__(self, tool_use_id, content=None, is_error=None) -> None:
        self.tool_use_id = tool_use_id
        self.content = content
        self.is_error = is_error


class StubAssistantMessage:
    def __init__(self, content) -> None:
        self.content = content


class StubUserMessage:
    def __init__(self, content) -> None:
        self.content = content


class StubSystemMessage:
    def __init__(self, subtype="init", data=None) -> None:
        self.subtype = subtype
        self.data = data or {}


class StubResultMessage:
    def __init__(
        self,
        subtype="success",
        is_error=False,
        num_turns=None,
        usage=None,
        terminal_reason=None,
        usage_source=None,
    ) -> None:
        self.subtype = subtype
        self.is_error = is_error
        if num_turns is not None:
            self.num_turns = num_turns
        if usage is not None:
            self.usage = usage
        if terminal_reason is not None:
            self.terminal_reason = terminal_reason
        if usage_source is not None:
            self.usage_source = usage_source


class FakeAgentSDK:
    """按脚本回放一个回合的假 SDK，并**真的执行**没有被拒的工具。

    "真的执行"是这套用例的关键：越界探针一旦被放行就会在磁盘上留下文件，因此
    "文件不存在"是可证伪的事实，不是对实现的复述。
    """

    def __init__(
        self,
        script,
        *,
        result_messages: int = 1,
        result_subtype: str = "success",
        result_is_error: bool = False,
        result_num_turns: int | None = None,
        result_usage: dict | None = None,
        terminal_reason: str | None = None,
        usage_source: str = "mock",
        skip_pre_tool_use: bool = False,
    ) -> None:
        self.script = list(script)
        self.options = []
        self.prompts = []
        self.executed = []
        self.child_sessions = 0
        self.child_turns = 0
        # 终止消息可配置：省略/重复 ResultMessage、SDK 自报错误结束，都是
        # 真实链路可能出现的档位（独立复查发现原脚本恒定"一次成功"）。
        self.result_messages = result_messages
        self.result_subtype = result_subtype
        self.result_is_error = result_is_error
        self.result_num_turns = result_num_turns
        self.result_usage = result_usage
        self.terminal_reason = terminal_reason
        self.usage_source = usage_source
        # 只发 PostToolUse 不发 PreToolUse：模拟 hook 未注册/被跳过的屏障失效形状。
        self.skip_pre_tool_use = skip_pre_tool_use
        # 步骤走完后抛异常：模拟「已有绕过调用、随后传输又失败」的叠加形状。
        self.raise_after_steps: Exception | None = None

    def install(self, testcase) -> "FakeAgentSDK":
        module = types.ModuleType("claude_agent_sdk")
        module.HookMatcher = StubHookMatcher
        module.ClaudeAgentOptions = StubAgentOptions
        module.AssistantMessage = StubAssistantMessage
        module.UserMessage = StubUserMessage
        module.SystemMessage = StubSystemMessage
        module.ResultMessage = StubResultMessage
        module.TextBlock = StubTextBlock
        module.ThinkingBlock = StubThinkingBlock
        module.ToolUseBlock = StubToolUseBlock
        module.ToolResultBlock = StubToolResultBlock
        module.ClaudeSDKClient = self._client_class()

        saved = sys.modules.get("claude_agent_sdk")
        sys.modules["claude_agent_sdk"] = module

        def restore() -> None:
            if saved is None:
                sys.modules.pop("claude_agent_sdk", None)
            else:
                sys.modules["claude_agent_sdk"] = saved

        testcase.addCleanup(restore)
        return self

    def _client_class(self):
        harness = self

        class StubClient:
            def __init__(self, options=None) -> None:
                self.options = options
                harness.options.append(options)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            async def query(self, prompt, session_id="default"):
                harness.prompts.append(prompt)

            def receive_response(self):
                return harness._play(self.options)

        return StubClient

    async def _play(self, options):
        counter = 0
        for step in self.script:
            if step["kind"] == "text":
                yield StubAssistantMessage([StubTextBlock(step["text"])])
                continue

            counter += 1
            call_id = f"toolu_{counter}"
            tool_input = step.get("input") or {}
            yield StubAssistantMessage([StubToolUseBlock(call_id, step["tool"], tool_input)])

            if self.skip_pre_tool_use:
                self._execute(step)
                await self._fire(
                    options,
                    "PostToolUse",
                    {
                        "hook_event_name": "PostToolUse",
                        "tool_name": step["tool"],
                        "tool_input": tool_input,
                        "tool_use_id": call_id,
                    },
                    call_id,
                )
                yield StubUserMessage([StubToolResultBlock(call_id, step.get("result"), step.get("is_error"))])
                continue

            response = await self._fire(
                options,
                "PreToolUse",
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": step["tool"],
                    "tool_input": tool_input,
                    "tool_use_id": call_id,
                },
                call_id,
            )
            hook_output = (response or {}).get("hookSpecificOutput", {})
            if hook_output.get("permissionDecision") == "deny":
                # 真实 CLI 会把拒绝理由当成失败回执发回模型；照抄这个形状。
                yield StubUserMessage(
                    [StubToolResultBlock(call_id, hook_output.get("permissionDecisionReason"), True)]
                )
                continue

            self._execute(step)
            if step.get("no_result"):
                # 调用出去了却没有任何回执：审计不得把它当成成功，也不得当成没发生。
                continue
            failure = step.get("fails")
            if failure is None:
                await self._fire(
                    options,
                    "PostToolUse",
                    {
                        "hook_event_name": "PostToolUse",
                        "tool_name": step["tool"],
                        "tool_input": tool_input,
                        "tool_use_id": call_id,
                    },
                    call_id,
                )
            else:
                await self._fire(
                    options,
                    "PostToolUseFailure",
                    {
                        "hook_event_name": "PostToolUseFailure",
                        "tool_name": step["tool"],
                        "tool_input": tool_input,
                        "tool_use_id": call_id,
                        "error": failure,
                    },
                    call_id,
                )
            yield StubUserMessage(
                [
                    StubToolResultBlock(
                        call_id,
                        failure if failure is not None else step.get("result"),
                        True if failure is not None else step.get("is_error"),
                    )
                ]
            )

        if self.raise_after_steps is not None:
            raise self.raise_after_steps
        await self._fire(options, "Stop", {"hook_event_name": "Stop"}, None)
        for _ in range(self.result_messages):
            yield StubResultMessage(
                subtype=self.result_subtype,
                is_error=self.result_is_error,
                num_turns=self.result_num_turns,
                usage=self.result_usage,
                terminal_reason=self.terminal_reason,
                usage_source=self.usage_source,
            )

    def _execute(self, step) -> None:
        self.executed.append(step["tool"])
        if step["tool"] in {"Agent", "Task"}:
            self.child_sessions += 1
            self.child_turns += 1
        target = step.get("writes")
        if target:
            pathlib.Path(target).write_text(step.get("input", {}).get("content", ""), encoding="utf-8")

    async def _fire(self, options, event, payload, call_id):
        hooks = getattr(options, "hooks", None) or {}
        response = {}
        for matcher in hooks.get(event, []):
            for callback in matcher.hooks:
                result = await callback(payload, call_id, None)
                if result:
                    response = result
        return response


# ------------------------------------------------------------------ 测试夹具


def worker_env(**overrides):
    env = {
        "LINGXI_WORKER_QUESTION": "近 7 天的活跃用户数是多少？",
        "LINGXI_WORKER_READONLY_TOOLS": READ_ONLY_TOOL,
        "LINGXI_WORKER_AUDIT_INPUT_FIELDS": "metric",
        "LINGXI_WORKER_TRACE_ID": "01J0000000000000000TEST000",
    }
    env.update({key: value for key, value in overrides.items() if value is not None})
    return env


def run_turn(testcase, script, *, turns=1, **env_overrides):
    """跑完整装配路径，返回 (最后一次回合报告, 假 SDK, executor)。"""

    from lingxi.apps.worker.config import load_config
    from lingxi.apps.worker.turn import WorkerTurnExecutor

    fake = FakeAgentSDK(script).install(testcase)
    config = load_config(worker_env(**env_overrides))
    executor = WorkerTurnExecutor(config)
    report = None
    for _ in range(turns):
        report = asyncio.run(executor.run_turn(config.question))
    return report, fake, executor


def ok_result():
    # #37 A1 的真实 MCP 契约：list_metrics 成功载荷位于顶层 metrics。
    # worker 接线测试必须使用真实形状，不能再用 data 替代而掩盖契约漂移。
    return json.dumps({"metrics": [{"metric": "dau", "value": 1024}]}, ensure_ascii=False)


def business_failure_result():
    return json.dumps({"error": "指标不存在"}, ensure_ascii=False)


# ------------------------------------------------------------------------ 用例


class WorkerWiringTest(unittest.TestCase):
    """V-执行-18：入口实际构造 ToolGateway、装进真实 SDK hooks、每回合清空审计。"""

    def test_v_zhixing_18_entry_installs_the_gateway_into_the_sdk_options(self) -> None:
        from lingxi.core.execution.hooks import HOOK_EVENTS, OBSERVATION_ONLY_EVENTS, ToolGateway

        _, fake, _ = run_turn(self, [{"kind": "text", "text": "已完成。"}])

        self.assertEqual(len(fake.options), 1, "一个回合只应建立一个会话")
        options = fake.options[0]
        self.assertIsNotNone(options.hooks, "hooks 没被装进 ClaudeAgentOptions，屏障根本不会被调用")
        self.assertEqual(set(options.hooks), set(HOOK_EVENTS) | set(OBSERVATION_ONLY_EVENTS))
        callback = options.hooks["PreToolUse"][0].hooks[0]
        self.assertIsInstance(
            getattr(callback, "__self__", None),
            ToolGateway,
            "PreToolUse 上挂的必须是 ToolGateway 的回调，而不是别的什么函数",
        )

    def test_v_zhixing_18_whitelist_is_the_only_configured_read_only_tool(self) -> None:
        _, fake, executor = run_turn(self, [{"kind": "text", "text": "已完成。"}])

        self.assertEqual(executor.policy.allowed_tools, frozenset({READ_ONLY_TOOL}))
        self.assertEqual(fake.options[0].allowed_tools, [READ_ONLY_TOOL], "纵深防御层也只列这一个工具")
        self.assertEqual(fake.options[0].max_turns, 20, "默认轮数必须通过 SDK 选项生效")
        self.assertEqual(
            fake.options[0].disallowed_tools,
            [],
            "disallowed_tools 必须留空：它一旦先拦下调用，就不再经过我们的 PreToolUse，"
            "拒绝反而失去审计证据（V-执行-09）",
        )

    def test_v_zhixing_18_hook_events_and_message_stream_land_in_the_same_turn_audit(self) -> None:
        report, _, _ = run_turn(
            self,
            [
                {"kind": "tool", "tool": READ_ONLY_TOOL, "input": {"metric": "dau"}, "result": ok_result()},
                {"kind": "text", "text": "近 7 天日活是 1024。"},
            ],
        )

        calls = report["audit"]["calls"]
        self.assertEqual(len(calls), 1, "同一次调用必须只有一条记录，不能判定与回执各记一条")
        self.assertTrue(calls[0]["allowed"], "PreToolUse 的判定没有汇入审计")
        self.assertTrue(calls[0]["executed"], "PostToolUse 没有汇入审计")
        self.assertEqual(calls[0]["result_kind"], "ok", "消息流里的工具回执没有汇入审计")
        self.assertEqual(report["turn"]["final_text"], "近 7 天日活是 1024。", "最终正文没有汇入审计")
        self.assertEqual(report["turn"]["terminal_result_count"], 1)
        self.assertEqual(report["turn"]["user_result"], "obtained")
        self.assertTrue(report["turn"]["closed"])

    def test_v_zhixing_18_second_turn_starts_from_a_cleared_audit(self) -> None:
        """``hooks`` 是会话级的：不翻页，第二回合的终止计数必然从 2 起跳。"""

        report, _, _ = run_turn(
            self,
            [
                {"kind": "tool", "tool": READ_ONLY_TOOL, "input": {"metric": "dau"}, "result": ok_result()},
                {"kind": "text", "text": "近 7 天日活是 1024。"},
            ],
            turns=2,
        )

        self.assertEqual(report["turn"]["terminal_result_count"], 1, "上一回合的终止结果被带进了本回合")
        self.assertEqual(len(report["audit"]["calls"]), 1, "上一回合的工具调用被带进了本回合")
        self.assertTrue(report["turn"]["closed"])

    def test_turn_reports_an_explicit_failure_when_the_session_cannot_start(self) -> None:
        """SDK 缺失不得表现成"没有工具调用的成功回合"。"""

        from lingxi.apps.worker.config import load_config
        from lingxi.apps.worker.turn import WorkerTurnExecutor

        saved = sys.modules.get("claude_agent_sdk")
        sys.modules["claude_agent_sdk"] = None  # 触发 ImportError

        def restore() -> None:
            if saved is None:
                sys.modules.pop("claude_agent_sdk", None)
            else:
                sys.modules["claude_agent_sdk"] = saved

        self.addCleanup(restore)

        executor = WorkerTurnExecutor(load_config(worker_env()))
        report = asyncio.run(executor.run_turn("问题"))

        self.assertEqual(report["failure"]["code"], "sdk_unavailable")
        self.assertFalse(report["turn"]["closed"])


class ReadOnlyBoundaryTest(unittest.TestCase):
    """V-执行-19：白名单工具可执行；越界探针在执行前被拒且没有副作用。"""

    def setUp(self) -> None:
        self._workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self._workspace.cleanup)
        self.probe = os.path.join(self._workspace.name, "越界探针.txt")

    def _script(self):
        return [
            {"kind": "tool", "tool": READ_ONLY_TOOL, "input": {"metric": "dau"}, "result": ok_result()},
            {
                "kind": "tool",
                "tool": "Write",
                "input": {"file_path": self.probe, "content": "越界写入"},
                "writes": self.probe,
            },
            {"kind": "text", "text": "日活是 1024；另一部分我无法查询。"},
        ]

    def test_v_zhixing_19_whitelisted_tool_runs_and_the_write_probe_never_executes(self) -> None:
        report, fake, _ = run_turn(self, self._script(), LINGXI_WORKER_WORKSPACE=self._workspace.name)

        self.assertEqual(fake.executed, [READ_ONLY_TOOL], "只有白名单工具应当真的被执行")
        self.assertFalse(os.path.exists(self.probe), "越界工具在执行前没有被拦住，探针文件被写了出来")
        self.assertEqual(report["audit"]["executed_tool_names"], [READ_ONLY_TOOL])

    def test_v_zhixing_19_denial_is_queryable_in_the_audit_summary(self) -> None:
        report, _, _ = run_turn(self, self._script(), LINGXI_WORKER_WORKSPACE=self._workspace.name)

        denied = report["audit"]["denied"]
        self.assertEqual([entry["tool_name"] for entry in denied], ["Write"])
        self.assertEqual(denied[0]["deny_reason_code"], "not_in_whitelist")
        self.assertFalse(denied[0]["executed"])
        self.assertEqual(report["audit"]["denied_count"], 1)

    def test_v_zhixing_19_denied_turn_still_closes_with_text_and_one_terminal_result(self) -> None:
        report, _, _ = run_turn(self, self._script(), LINGXI_WORKER_WORKSPACE=self._workspace.name)

        self.assertTrue(report["turn"]["final_text"].strip())
        self.assertEqual(report["turn"]["terminal_result_count"], 1)
        self.assertTrue(report["turn"]["closed"])

    def test_v_zhixing_23_empty_rule_layer_cannot_replace_the_pre_tool_use_boundary(self) -> None:
        """V-执行-23：规则层为空时，越界写工具仍由 PreToolUse 拒绝。"""

        report, fake, _ = run_turn(
            self,
            [
                {
                    "kind": "tool",
                    "tool": "Write",
                    "input": {"file_path": self.probe, "content": "越界写入"},
                    "writes": self.probe,
                },
                {"kind": "text", "text": "这一部分无法查询。"},
            ],
            LINGXI_WORKER_WORKSPACE=self._workspace.name,
        )

        self.assertEqual(fake.options[0].disallowed_tools, [])
        self.assertEqual(fake.executed, [])
        self.assertFalse(os.path.exists(self.probe))
        self.assertEqual([entry["tool_name"] for entry in report["audit"]["denied"]], ["Write"])
        self.assertFalse(report["turn"]["gate_bypassed"])

    def test_workspace_is_handed_to_the_sdk_so_probes_stay_inside_it(self) -> None:
        _, fake, _ = run_turn(
            self,
            [{"kind": "text", "text": "已完成。"}],
            LINGXI_WORKER_WORKSPACE=self._workspace.name,
        )

        self.assertEqual(fake.options[0].cwd, self._workspace.name)


class SubagentBoundaryTest(unittest.TestCase):
    """V-执行-22：子代理调用不得产生子会话或第二份回合审计。"""

    def test_v_zhixing_22_subagent_denial_keeps_the_main_turn_closed(self) -> None:
        report, fake, _ = run_turn(
            self,
            [
                {"kind": "tool", "tool": "Task", "input": {"prompt": "请派生助手"}},
                {"kind": "text", "text": "该操作不在本次只读范围内。"},
            ],
        )

        self.assertEqual(fake.executed, [])
        self.assertEqual(fake.child_sessions, 0)
        self.assertEqual(fake.child_turns, 0)
        self.assertEqual(report["audit"]["call_count"], 1)
        self.assertEqual(report["audit"]["denied"][0]["tool_name"], "Task")
        self.assertEqual(report["turn"]["terminal_result_count"], 1)
        self.assertEqual(report["turn"]["sdk_result_message_count"], 1)
        self.assertTrue(report["turn"]["closed"])


class MixedTurnThroughTheEntryTest(unittest.TestCase):
    """V-执行-17 经真实装配路径复验：混合回合不得写成"用户拿到了结果"。"""

    def test_success_plus_business_failure_is_not_obtained_through_the_worker(self) -> None:
        report, _, _ = run_turn(
            self,
            [
                {"kind": "tool", "tool": READ_ONLY_TOOL, "input": {"metric": "dau"}, "result": ok_result()},
                {
                    "kind": "tool",
                    "tool": READ_ONLY_TOOL,
                    "input": {"metric": "不存在的指标"},
                    "result": business_failure_result(),
                },
                {"kind": "text", "text": "只查到其中一部分。"},
            ],
        )

        self.assertEqual(report["turn"]["user_result"], "unknown")
        self.assertEqual(report["audit"]["failed_count"], 1)

    def test_success_plus_missing_tool_result_is_not_obtained_through_the_worker(self) -> None:
        report, _, _ = run_turn(
            self,
            [
                {"kind": "tool", "tool": READ_ONLY_TOOL, "input": {"metric": "dau"}, "result": ok_result()},
                {"kind": "tool", "tool": READ_ONLY_TOOL, "input": {"metric": "arpu"}, "no_result": True},
                {"kind": "text", "text": "只查到其中一部分。"},
            ],
        )

        self.assertEqual(report["turn"]["user_result"], "unknown")

    def test_a_turn_with_no_tool_call_is_not_claimed_as_delivered(self) -> None:
        report, _, _ = run_turn(self, [{"kind": "text", "text": "这个问题我不需要查数就能回答。"}])

        self.assertEqual(report["turn"]["user_result"], "no_tool_call")


class WorkerOutputRedactionTest(unittest.TestCase):
    """V-执行-20：固定假凭据不以完整形式进入 worker 输出。"""

    def _run(self):
        script = [
            # 合法形态、但不在白名单里的工具名——模型可控的任意文本
            {"kind": "tool", "tool": FAKE_CREDENTIAL, "input": {"q": "1"}},
            # 未知字段名
            {"kind": "tool", "tool": "Write", "input": {FAKE_CREDENTIAL: "x"}},
            # 白名单字段的值
            {
                "kind": "tool",
                "tool": READ_ONLY_TOOL,
                "input": {"metric": FAKE_CREDENTIAL},
                "fails": f"上游返回 401：token={FAKE_CREDENTIAL}",
            },
            # 最终正文同样是模型可控文本
            {"kind": "text", "text": f"查询失败，凭据 {FAKE_CREDENTIAL} 无效。"},
        ]
        return run_turn(self, script)

    def test_v_zhixing_20_no_fixed_fake_credential_survives_anywhere_in_the_report(self) -> None:
        report, _, _ = self._run()

        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn(FAKE_CREDENTIAL, serialized)
        self.assertIn("[REDACTED", serialized, "什么都没被脱敏，说明探针根本没走到审计里")

    def test_v_zhixing_20_the_unknown_tool_name_is_still_recorded_as_an_attempt(self) -> None:
        """脱敏不得让"模型试图调用过一个未知工具"这条事实整条消失。"""

        report, _, _ = self._run()

        self.assertEqual(report["audit"]["denied_count"], 2, "两次越界都必须留痕")
        names = [entry["tool_name"] for entry in report["audit"]["denied"]]
        self.assertIn("Write", names, "白名单外的常规工具名应原样保留")
        self.assertTrue(
            any(name.startswith("[REDACTED") for name in names),
            "凭据形态的工具名应被投影成长度，而不是原样落进证据",
        )

    def test_v_zhixing_20_stderr_logs_do_not_carry_the_credential_either(self) -> None:
        from lingxi.apps.worker.cli import main

        script = [
            {"kind": "tool", "tool": FAKE_CREDENTIAL, "input": {"q": "1"}},
            {"kind": "text", "text": f"凭据 {FAKE_CREDENTIAL} 无效。"},
        ]
        FakeAgentSDK(script).install(self)
        stdout, stderr = io.StringIO(), io.StringIO()

        main(env=worker_env(), stdout=stdout, stderr=stderr)

        self.assertNotIn(FAKE_CREDENTIAL, stdout.getvalue())
        self.assertNotIn(FAKE_CREDENTIAL, stderr.getvalue())

    def test_final_text_is_redacted_but_still_reported_as_non_empty(self) -> None:
        report, _, _ = self._run()

        self.assertTrue(report["turn"]["final_text"].strip())
        self.assertNotIn(FAKE_CREDENTIAL, report["turn"]["final_text"])
        self.assertGreater(report["turn"]["final_text_bytes"], 0)


class OutputSafetyCanaryTest(unittest.TestCase):
    """S-A-07 r17 / #142：输出安全 canary 必须**确定性**触发，默认关闭且不外泄。

    r17 真实链路失败的根因是把触发条件寄托在"模型恰好复述提示词"上；这里锁定
    新合同：canary 开启时，遮蔽/withheld 终态不依赖模型行为，走与真实泄露完全
    相同的 ``constrain_output`` 检测与投影路径。
    """

    SYNTHETIC_PROMPT = "合成安全验收提示词：本句仅用于受控验收。第二句提供更长的合成片段供切分。"

    def _script(self):
        return [
            {"kind": "tool", "tool": READ_ONLY_TOOL, "input": {"metric": "dau"}, "result": ok_result()},
            {"kind": "text", "text": "业务答案：DAU 为一千零二十四。"},
        ]

    def test_default_off_keeps_the_final_text_untouched(self) -> None:
        report, _, _ = run_turn(
            self, self._script(), LINGXI_WORKER_SYSTEM_PROMPT=self.SYNTHETIC_PROMPT
        )
        safety = report["turn"]["output_safety"]
        self.assertFalse(safety["blocked"])
        self.assertFalse(safety["withheld"])
        self.assertIn("业务答案", report["turn"]["final_text"])

    def test_masked_canary_deterministically_blocks_but_keeps_the_business_text(self) -> None:
        report, _, _ = run_turn(
            self,
            self._script(),
            LINGXI_WORKER_SYSTEM_PROMPT=self.SYNTHETIC_PROMPT,
            LINGXI_WORKER_OUTPUT_SAFETY_CANARY="masked",
        )
        safety = report["turn"]["output_safety"]
        self.assertTrue(safety["blocked"], "masked 档位必须触发遮蔽，与模型输出无关")
        self.assertFalse(safety["withheld"], "业务结论幸存时不得升级成整段拒发")
        final_text = report["turn"]["final_text"]
        self.assertIn("业务答案", final_text, "局部遮蔽不得吞掉幸存的业务结论（合同第 1 条）")
        self.assertIn("受控验收合成正文", final_text, "固定幸存句必须在场（确定性由构造保证）")
        self.assertNotIn("合成安全验收提示词", final_text, "被注入的提示词原文不得离开 worker")
        self.assertIn("已隐藏", final_text, "命中区间必须以占位符呈现")
        self.assertEqual(report["turn"]["user_result"], "obtained")

    def test_masked_stays_masked_even_when_the_model_text_is_entirely_maskable(self) -> None:
        """独立审核 F3（实证复现）：模型正文恰好整段可遮蔽（例如输出
        ``system prompt`` 标记字样）时，第一版实现会因命中区间之外无幸存内容而
        滑进 withheld。确定性必须由构造（固定幸存句）保证，不能由模型行为保证。"""

        report, _, _ = run_turn(
            self,
            [
                {"kind": "tool", "tool": READ_ONLY_TOOL, "input": {"metric": "dau"}, "result": ok_result()},
                {"kind": "text", "text": "system prompt"},
            ],
            LINGXI_WORKER_SYSTEM_PROMPT=self.SYNTHETIC_PROMPT,
            LINGXI_WORKER_OUTPUT_SAFETY_CANARY="masked",
        )
        safety = report["turn"]["output_safety"]
        self.assertTrue(safety["blocked"])
        self.assertFalse(safety["withheld"], "masked 档位不得因模型正文可遮蔽而滑进 withheld")
        self.assertIn("受控验收合成正文", report["turn"]["final_text"])

    def test_a_failed_turn_is_not_injected(self) -> None:
        """独立审核 F1：失败回合不注入——注入会让 withheld 覆盖真实失败原因，
        验收拿到的是假证据（真超时被记成安全拦截）。"""

        from lingxi.apps.worker.config import load_config
        from lingxi.apps.worker.turn import WorkerTurnExecutor

        FakeAgentSDK(
            self._script(), result_subtype="error_max_turns", result_is_error=True
        ).install(self)
        config = load_config(
            worker_env(
                LINGXI_WORKER_SYSTEM_PROMPT=self.SYNTHETIC_PROMPT,
                LINGXI_WORKER_OUTPUT_SAFETY_CANARY="withheld",
            )
        )
        report = asyncio.run(WorkerTurnExecutor(config).run_turn(config.question))

        self.assertIsNotNone(report["failure"], "前提：这一轮必须是失败回合")
        self.assertEqual(report["failure"]["code"], "max_turns_exceeded")
        self.assertFalse(
            report["turn"]["output_safety"]["withheld"],
            "失败回合不得被 canary 改写成 withheld",
        )
        self.assertNotIn("合成安全验收提示词", json.dumps(report, ensure_ascii=False))

    def test_withheld_canary_forces_the_independent_redacted_terminal(self) -> None:
        from lingxi.core.execution.input_safety import WITHHELD_MESSAGE

        report, _, _ = run_turn(
            self,
            self._script(),
            LINGXI_WORKER_SYSTEM_PROMPT=self.SYNTHETIC_PROMPT,
            LINGXI_WORKER_OUTPUT_SAFETY_CANARY="withheld",
        )
        safety = report["turn"]["output_safety"]
        self.assertTrue(safety["withheld"], "withheld 档位必须整段拒发，与模型输出无关")
        self.assertTrue(safety["blocked"])
        self.assertIn("no_safe_content", safety["reasons"])
        self.assertEqual(report["turn"]["final_text"], WITHHELD_MESSAGE)
        self.assertEqual(
            report["turn"]["user_result"],
            "redacted_withheld",
            "withheld 不得伪装成 obtained（#141/#149 独立终态）",
        )
        self.assertNotIn(
            "合成安全验收提示词",
            json.dumps(report, ensure_ascii=False),
            "被注入的提示词原文不得出现在报告任何角落",
        )

    def test_masked_canary_with_an_empty_model_output_still_masks_not_withholds(self) -> None:
        report, _, _ = run_turn(
            self,
            [{"kind": "tool", "tool": READ_ONLY_TOOL, "input": {"metric": "dau"}, "result": ok_result()}],
            LINGXI_WORKER_SYSTEM_PROMPT=self.SYNTHETIC_PROMPT,
            LINGXI_WORKER_OUTPUT_SAFETY_CANARY="masked",
        )
        safety = report["turn"]["output_safety"]
        self.assertTrue(safety["blocked"])
        self.assertFalse(safety["withheld"], "masked + 空正文不得滑进 withheld，档位必须确定")
        self.assertIn("受控验收合成正文", report["turn"]["final_text"])

    def test_the_injection_leaves_a_low_sensitivity_stderr_trace(self) -> None:
        from lingxi.apps.worker.config import load_config
        from lingxi.apps.worker.turn import WorkerTurnExecutor

        FakeAgentSDK(self._script()).install(self)
        stderr = io.StringIO()
        config = load_config(
            worker_env(
                LINGXI_WORKER_SYSTEM_PROMPT=self.SYNTHETIC_PROMPT,
                LINGXI_WORKER_OUTPUT_SAFETY_CANARY="masked",
            )
        )
        executor = WorkerTurnExecutor(config, stderr_stream=stderr)
        asyncio.run(executor.run_turn(config.question))

        records = [json.loads(line) for line in stderr.getvalue().splitlines()]
        injected = [r for r in records if r.get("event") == "worker.output_safety_canary_injected"]
        self.assertEqual(len(injected), 1, "每轮注入必须恰好留一条可回读的低敏痕迹")
        self.assertEqual(injected[0]["mode"], "masked")
        self.assertEqual(injected[0]["trace_id"], config.trace_id)
        self.assertNotIn("合成安全验收提示词", stderr.getvalue(), "痕迹不得携带提示词原文")

    def test_an_invalid_canary_value_fails_at_startup_without_echoing_it(self) -> None:
        """失败关闭 + 不回显（独立审核 F9）：误接进来的可能是口令或提示词原文。"""

        from lingxi.apps.worker.config import WorkerConfigError, load_config

        with self.assertRaises(WorkerConfigError) as raised:
            load_config(
                worker_env(
                    LINGXI_WORKER_SYSTEM_PROMPT=self.SYNTHETIC_PROMPT,
                    LINGXI_WORKER_OUTPUT_SAFETY_CANARY="maskedd",
                )
            )
        self.assertNotIn("maskedd", str(raised.exception), "拒绝的值不得回显进错误文案")

    def test_a_canary_without_a_system_prompt_fails_at_startup(self) -> None:
        from lingxi.apps.worker.config import WorkerConfigError, load_config

        with self.assertRaises(WorkerConfigError):
            load_config(worker_env(LINGXI_WORKER_OUTPUT_SAFETY_CANARY="masked"))

    def test_a_prompt_that_is_a_substring_of_a_terminal_text_fails_at_startup(self) -> None:
        """独立审核 F2（实证复现）：合成提示若是 withheld 固定文案的子串，出口
        约束的终态自检会在每一轮抛 InputSafetyError，把"总是返回一份报告"的契约
        炸掉。必须在启动期确定性拒绝。"""

        from lingxi.apps.worker.config import WorkerConfigError, load_config

        with self.assertRaises(WorkerConfigError) as raised:
            load_config(
                worker_env(
                    LINGXI_WORKER_SYSTEM_PROMPT="安全策略拦截",
                    LINGXI_WORKER_OUTPUT_SAFETY_CANARY="withheld",
                )
            )
        self.assertIn("子串", str(raised.exception))
        self.assertNotIn("安全策略拦截", str(raised.exception), "提示词不回显进错误文案")

    def test_direct_construction_enforces_the_canary_prompt_invariant(self) -> None:
        """独立审核 F7：canary ⇒ system_prompt 的不变量由 WorkerConfig 自身承载，
        绕过 load_config 的直接构造（文档支持的测试/嵌入路径）同样被拒绝，而不是
        让注入静默退化成空字符串、canary 永远不触发。"""

        from lingxi.apps.worker.config import WorkerConfigError

        with self.assertRaises(WorkerConfigError):
            self._build_config(output_safety_canary="withheld", system_prompt=None)

    def _build_config(self, **overrides):
        from lingxi.apps.worker.config import WorkerConfig

        values = {
            "question": "q",
            "read_only_tools": (READ_ONLY_TOOL,),
            "trace_id": "01J0000000000000000TEST000",
            "turn_timeout_seconds": 60.0,
        }
        values.update(overrides)
        return WorkerConfig(**values)  # type: ignore[arg-type]

    def test_direct_construction_rejects_an_invalid_canary_mode(self) -> None:
        """PR #186 补审 P2-2：档位白名单必须由 WorkerConfig 自身承载。只放在
        load_config 时，绕过 loader 的直接构造能带着拼错的档位跑起来——注入分支
        既不是 masked 也不是 withheld，验收拿到的是"安全链路没触发"的假证据。"""

        from lingxi.apps.worker.config import WorkerConfigError

        with self.assertRaises(WorkerConfigError) as raised:
            self._build_config(
                output_safety_canary="withheldd", system_prompt=self.SYNTHETIC_PROMPT
            )
        self.assertNotIn("withheldd", str(raised.exception), "拒绝的值不得回显进错误文案")

    def test_direct_construction_enforces_the_substring_guard(self) -> None:
        """PR #186 补审 P2-2：子串守卫同样由类型承载，直接构造不得绕过。"""

        from lingxi.apps.worker.config import WorkerConfigError

        with self.assertRaises(WorkerConfigError) as raised:
            self._build_config(output_safety_canary="withheld", system_prompt="安全策略拦截")
        self.assertIn("子串", str(raised.exception))
        self.assertNotIn("安全策略拦截", str(raised.exception), "提示词不回显进错误文案")

    def test_a_prompt_containing_a_protected_text_fails_at_startup(self) -> None:
        """PR #186 补审 P2-3：子串守卫必须**双向**。受保护文本出现在合成提示
        之中时，出口约束会把 system prompt 按结构性边界切片当禁词，于是 masked
        固定幸存句被自己派生出的禁词遮蔽、命中区间外再无真实内容，masked 滑进
        withheld；夹的若是终态文案则让终态自检炸掉。构造它不需要恶意——多行合成
        提示中间夹一行幸存句就够了。"""

        from lingxi.apps.worker.config import (
            OUTPUT_SAFETY_CANARY_SURVIVOR_BODY,
            WorkerConfigError,
            load_config,
        )

        prompt = (
            "合成安全验收提示词：本句仅用于受控验收。\n"
            f"{OUTPUT_SAFETY_CANARY_SURVIVOR_BODY}\n"
            "第三句提供更长的合成片段供切分。"
        )
        with self.assertRaises(WorkerConfigError) as raised:
            load_config(
                worker_env(
                    LINGXI_WORKER_SYSTEM_PROMPT=prompt,
                    LINGXI_WORKER_OUTPUT_SAFETY_CANARY="masked",
                )
            )
        self.assertIn("子串", str(raised.exception))
        self.assertNotIn("合成安全验收提示词", str(raised.exception), "提示词不回显进错误文案")
        self.assertNotIn(
            OUTPUT_SAFETY_CANARY_SURVIVOR_BODY,
            str(raised.exception),
            "错误文案不回显收到的 prompt 正文",
        )

    def test_a_normal_multi_sentence_prompt_still_passes_the_two_way_guard(self) -> None:
        """双向守卫不得误伤：正常的多句合成提示仍然能启动并确定性触发 masked。"""

        report, _, _ = run_turn(
            self,
            self._script(),
            LINGXI_WORKER_SYSTEM_PROMPT=self.SYNTHETIC_PROMPT,
            LINGXI_WORKER_OUTPUT_SAFETY_CANARY="masked",
        )
        self.assertTrue(report["turn"]["output_safety"]["blocked"])
        self.assertFalse(report["turn"]["output_safety"]["withheld"])


class WorkerConfigTest(unittest.TestCase):
    """配置只来自 LINGXI_ 前缀环境变量，构造期就拒绝不合法形态。"""

    def _load(self, **overrides):
        from lingxi.apps.worker.config import load_config

        return load_config(worker_env(**overrides))

    def test_missing_question_fails_with_a_named_variable(self) -> None:
        from lingxi.apps.worker.config import WorkerConfigError, load_config

        env = worker_env()
        env.pop("LINGXI_WORKER_QUESTION")
        with self.assertRaises(WorkerConfigError) as caught:
            load_config(env)
        self.assertIn("LINGXI_WORKER_QUESTION", str(caught.exception))

    def test_read_only_tools_rejects_wildcards_space_separation_and_empty_values(self) -> None:
        """白名单只接受精确名称、逗号分隔（Issue #291 P0）：通配符、空白分隔、
        空字符串都不是"想放行第二个工具"该有的形状，必须在装配期拒绝。"""
        from lingxi.apps.worker.config import WorkerConfigError

        for value in ("mcp__query__x mcp__query__y", "mcp__query__*", "", ","):
            with self.subTest(value=value), self.assertRaises(WorkerConfigError):
                self._load(LINGXI_WORKER_READONLY_TOOLS=value)

    def test_read_only_tools_rejects_a_bare_prefix_with_nothing_after_it(self) -> None:
        """独立审查 codex P1-1：``mcp__query__`` 本身通过合法标识符形态、``mcp__``
        前缀、``QUERY_MCP_TOOL_PREFIX`` 前缀三段校验（它就是那个前缀字符串），但
        前缀后面没有任何工具名——不指向任何真实工具，会被 PreToolUse 逐字比对
        无声拒绝。必须在装配期响亮报错，不能留到用户提问那一刻才发现。"""
        from lingxi.apps.worker.config import QUERY_MCP_TOOL_PREFIX, WorkerConfigError

        with self.assertRaises(WorkerConfigError) as caught:
            self._load(LINGXI_WORKER_READONLY_TOOLS=QUERY_MCP_TOOL_PREFIX)
        self.assertIn(QUERY_MCP_TOOL_PREFIX, str(caught.exception))

    def test_read_only_tools_rejects_empty_segments_between_commas(self) -> None:
        """独立审查 codex P1-1：逗号间的空段（``a,,b``）此前被 ``if part.strip()``
        静默丢弃——配置里写错一个逗号，白名单悄悄少了一项，运维不会收到任何报错。
        配置形状错误必须失败关闭，不能静默丢弃；多余的首尾逗号是同一类形状错误。"""
        from lingxi.apps.worker.config import WorkerConfigError

        for value in (
            "mcp__query__list_metrics,,mcp__query__describe_metric",
            "mcp__query__list_metrics,",
            ",mcp__query__list_metrics",
        ):
            with self.subTest(value=value), self.assertRaises(WorkerConfigError):
                self._load(LINGXI_WORKER_READONLY_TOOLS=value)

    def test_read_only_tools_accepts_multiple_comma_separated_values(self) -> None:
        """Issue #291 P0 核心行为：真实问数 MCP 至少注册 3 个只读工具，白名单
        必须能同时放行全部，不能"放行一个、其余照拒"。"""
        from lingxi.apps.worker.config import load_config

        config = load_config(
            worker_env(
                LINGXI_WORKER_READONLY_TOOLS=(
                    "mcp__query__list_metrics,mcp__query__describe_metric,"
                    "mcp__query__search_dimension"
                )
            )
        )
        self.assertEqual(
            config.read_only_tools,
            (
                "mcp__query__list_metrics",
                "mcp__query__describe_metric",
                "mcp__query__search_dimension",
            ),
        )

    def test_read_only_tools_expected_prefix_derives_from_the_same_constant_as_the_write_side(
        self,
    ) -> None:
        """单一事实来源（Issue #291 P0-2）：worker 的期望前缀常量与
        ``adapters/user_environment.py`` 写 ``.mcp.json`` 时使用的默认服务名
        常量必须是同一个对象，不是两处凑巧写了同一个字符串。"""

        from lingxi.adapters.user_environment import QUERY_MCP_SERVER_NAME
        from lingxi.apps.worker.config import QUERY_MCP_TOOL_PREFIX

        self.assertEqual(QUERY_MCP_TOOL_PREFIX, f"mcp__{QUERY_MCP_SERVER_NAME}__")

    def test_importing_worker_config_does_not_pull_in_the_onboarding_orchestration_chain(
        self,
    ) -> None:
        """独立审查（分支 fix/291-280-user-experience 收尾）：``apps/worker/
        config.py`` 此前为了读 ``QUERY_MCP_SERVER_NAME`` 这一个字符串常量，写的是
        ``from lingxi.adapters.user_environment import QUERY_MCP_SERVER_NAME``——
        而 ``adapters/user_environment.py`` 顶部 import 了 ``core/identity/
        onboarding_runner.py``（首次开通编排，传递拉入身份匹配、花名册、银河、
        建档等约十二个模块）。worker 是处理每一次真实用户提问的热路径进程，这条
        闭包与它的职责毫无关系。常量已经挪到零依赖的 ``lingxi.core.mcp_naming``
        （见该模块文档），本用例在一个干净子解释器里只 import
        ``lingxi.apps.worker.config``，断言 ``lingxi.core.identity.
        onboarding_runner`` 与任何 ``lingxi.adapters.*`` 模块都不在结果的 import
        闭包里——**变异存活证据**：把 ``config.py`` 的 import 改回
        ``from lingxi.adapters.user_environment import QUERY_MCP_SERVER_NAME``
        （修复前的写法），本用例会因为两个模块名重新出现在闭包里而变红。
        """

        probe = (
            "import sys\n"
            "import importlib\n"
            "importlib.import_module('lingxi.apps.worker.config')\n"
            "for name in sorted(sys.modules):\n"
            "    if name.startswith('lingxi.'):\n"
            "        print(name)\n"
        )
        source_root = str(pathlib.Path(__file__).resolve().parents[1] / "src")
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            path for path in (source_root, environment.get("PYTHONPATH")) if path
        )
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            f"探测子进程失败：{completed.stderr}",
        )
        imported = set(completed.stdout.splitlines())
        self.assertNotIn(
            "lingxi.core.identity.onboarding_runner",
            imported,
            "worker 的只读工具白名单装配不该拖进整条首次开通编排链",
        )
        adapter_modules = {name for name in imported if name.startswith("lingxi.adapters.")}
        self.assertEqual(
            adapter_modules,
            set(),
            f"worker 的只读工具白名单装配不该 import 任何 adapters 模块，实际：{adapter_modules}",
        )

    def test_read_only_tools_prefix_must_match_the_query_mcp_server_name(self) -> None:
        """装配期断言（Issue #291 根因 #1，与 adapters/user_environment.py 的
        ``QUERY_MCP_SERVER_NAME`` 同一常量来源）：白名单前缀与用户环境
        ``.mcp.json`` 实际使用的 MCP 服务名不一致时必须启动失败关闭，而不是
        留到用户提问那一刻才被 PreToolUse 无声拒绝——``mcp__bi-metric__...``
        正是 2026-08-21 真实事故里写错的那个值。响亮报错要同时带上期望前缀与
        收到的值，不是只报一个笼统的"配置不合法"。"""
        from lingxi.apps.worker.config import WorkerConfigError

        with self.assertRaises(WorkerConfigError) as caught:
            self._load(LINGXI_WORKER_READONLY_TOOLS="mcp__bi-metric__list_metrics")
        message = str(caught.exception)
        self.assertIn("mcp__query__", message)
        self.assertIn("mcp__bi-metric__list_metrics", message)

    def test_builtin_and_delegating_tools_cannot_be_configured_as_the_read_only_tool(self) -> None:
        from lingxi.apps.worker.config import WorkerConfigError

        for value in ("Skill", "Agent", "Task", "Read", "Bash"):
            with self.subTest(value=value), self.assertRaises(WorkerConfigError):
                self._load(LINGXI_WORKER_READONLY_TOOLS=value)

    def test_trace_id_is_generated_when_not_supplied(self) -> None:
        env = worker_env()
        env.pop("LINGXI_WORKER_TRACE_ID")
        from lingxi.apps.worker.config import load_config

        config = load_config(env)
        self.assertEqual(len(config.trace_id), 26)

    def test_malformed_mcp_server_json_is_rejected_at_load_time(self) -> None:
        from lingxi.apps.worker.config import WorkerConfigError

        with self.assertRaises(WorkerConfigError):
            self._load(LINGXI_WORKER_MCP_SERVERS="[]")

    def test_failure_markers_must_be_a_json_list_of_strings(self) -> None:
        from lingxi.apps.worker.config import WorkerConfigError

        with self.assertRaises(WorkerConfigError):
            self._load(LINGXI_WORKER_FAILURE_MARKERS='{"a": 1}')

    def test_registered_failure_wording_reaches_the_result_rules(self) -> None:
        report, _, _ = run_turn(
            self,
            [
                {"kind": "tool", "tool": READ_ONLY_TOOL, "input": {"metric": "dau"}, "result": "指标不存在"},
                {"kind": "text", "text": "查不到。"},
            ],
            LINGXI_WORKER_FAILURE_MARKERS=json.dumps(["指标不存在"], ensure_ascii=False),
        )

        self.assertEqual(report["audit"]["calls"][0]["result_kind"], "business_failure")
        self.assertEqual(report["turn"]["user_result"], "not_obtained")

    def test_known_metric_description_is_normalized_as_external_text(self) -> None:
        config = self._load(
            LINGXI_WORKER_METRIC_DESCRIPTION="指标目录中的已知描述",
        )

        self.assertEqual(
            config.external_texts,
            (("metric.description", "指标目录中的已知描述"),),
        )

    def test_audit_input_field_whitelist_is_opt_in(self) -> None:
        report, _, _ = run_turn(
            self,
            [
                {
                    "kind": "tool",
                    "tool": READ_ONLY_TOOL,
                    "input": {"metric": "dau", "note": "业务内容"},
                    "result": ok_result(),
                },
                {"kind": "text", "text": "1024。"},
            ],
        )

        recorded = report["audit"]["calls"][0]["tool_input"]
        self.assertEqual(recorded["metric"], "dau")
        self.assertEqual(recorded["note"], {"omitted": True})

    def test_resource_defaults_and_custom_values_are_configurable(self) -> None:
        from lingxi.apps.worker.config import DEFAULT_DRAIN_GRACE_SECONDS

        config = self._load(
            LINGXI_WORKER_MAX_TURNS="7",
            LINGXI_WORKER_TURN_TIMEOUT_SECONDS="12.5",
        )

        self.assertEqual(config.max_turns, 7)
        self.assertEqual(config.turn_timeout_seconds, 12.5)
        # #143：收尾宽限独立配置，未显式设置时落到自己的默认值，不借用业务预算。
        self.assertEqual(config.drain_grace_seconds, DEFAULT_DRAIN_GRACE_SECONDS)

        custom = self._load(LINGXI_WORKER_DRAIN_GRACE_SECONDS="5")
        self.assertEqual(custom.drain_grace_seconds, 5.0)

    def test_resource_values_beyond_hard_limits_fail_at_startup(self) -> None:
        from lingxi.apps.worker.config import WorkerConfigError

        for name, value in (
            ("LINGXI_WORKER_MAX_TURNS", "31"),
            ("LINGXI_WORKER_TURN_TIMEOUT_SECONDS", "900.1"),
            ("LINGXI_WORKER_DRAIN_GRACE_SECONDS", "120.1"),
            ("LINGXI_WORKER_DRAIN_GRACE_SECONDS", "0"),
        ):
            with self.subTest(name=name, value=value):
                with self.assertRaises(WorkerConfigError):
                    self._load(**{name: value})

    def test_max_turns_must_be_a_positive_integer(self) -> None:
        from lingxi.apps.worker.config import WorkerConfigError

        for value in ("0", "-1", "1.5", "not-a-number"):
            with self.subTest(value=value), self.assertRaises(WorkerConfigError):
                self._load(LINGXI_WORKER_MAX_TURNS=value)

    def test_user_env_root_is_optional_and_defaults_to_none(self) -> None:
        config = self._load()
        self.assertIsNone(config.user_env_root)

    def test_user_env_root_reads_the_bare_variable_shared_with_scheduler(self) -> None:
        """闸⑥：worker 与 scheduler 读同一个裸变量名 ``LINGXI_USER_ENV_ROOT``
        （不带 ``LINGXI_WORKER_`` 前缀），两个进程指向同一个持久卷挂载点。"""

        env = worker_env()
        env["LINGXI_USER_ENV_ROOT"] = "/var/lib/lingxi/users"
        from lingxi.apps.worker.config import load_config

        config = load_config(env)
        self.assertEqual(config.user_env_root, "/var/lib/lingxi/users")

    def test_user_env_root_with_internal_whitespace_fails_at_startup(self) -> None:
        """校验姿态照抄 scheduler 侧 ``optional_identifier``：不回显取到的值。"""

        from lingxi.apps.worker.config import WorkerConfigError

        with self.assertRaises(WorkerConfigError) as caught:
            self._load(LINGXI_USER_ENV_ROOT="/var/lib/lingxi/us ers")
        self.assertIn("LINGXI_USER_ENV_ROOT", str(caught.exception))
        self.assertNotIn("us ers", str(caught.exception))

    def test_user_env_root_must_be_an_absolute_normalized_path(self) -> None:
        """外部独立审查 F4：只拒空白字符串不够——相对路径与带 `..`/`.`/连续
        斜杠/多余尾部斜杠的路径此前都会被当成合法值放行，随后被直接拼进每个
        用户的家目录路径。这里逐个构造这些形态，确认都在启动期就被拒绝。"""

        from lingxi.apps.worker.config import WorkerConfigError

        malformed = (
            "relative/path",  # 不是绝对路径
            "var/lib/lingxi/users",  # 同上
            "/var/lib/lingxi/../lingxi/users",  # 路径穿越分量
            "/var/lib/./lingxi/users",  # `.` 分量
            "/var/lib//lingxi/users",  # 连续斜杠
            "/var/lib/lingxi/users/",  # 多余的尾部斜杠
        )
        for value in malformed:
            with self.subTest(value=value):
                with self.assertRaises(WorkerConfigError) as caught:
                    self._load(LINGXI_USER_ENV_ROOT=value)
                self.assertIn("LINGXI_USER_ENV_ROOT", str(caught.exception))
                self.assertNotIn(value, str(caught.exception))

    def test_user_env_root_accepts_a_well_formed_absolute_path(self) -> None:
        config = self._load(LINGXI_USER_ENV_ROOT="/var/lib/lingxi/users")
        self.assertEqual(config.user_env_root, "/var/lib/lingxi/users")

    def test_queue_mode_never_parses_the_shared_mcp_servers_variable(self) -> None:
        """外部独立审查 F3：`queue` 模式的真实任务处理已经改为按用户逐个读取
        MCP 配置（Epic D 闸⑥），`LINGXI_WORKER_MCP_SERVERS`（全进程共用配置）
        只服务没有 user_id 概念的一次性受控回合。`queue_mode=True` 时
        `load_config` 必须**根本不解析**这个变量——即使值是非法 JSON，
        queue 进程也不能因为一个从不会被用到的变量而启动失败；配了合法值，
        它也不能出现在构造出来的 WorkerConfig 里。"""

        from lingxi.apps.worker.config import load_config

        malformed_env = worker_env(LINGXI_WORKER_MCP_SERVERS="{not valid json")
        # turn 模式（默认）下这份畸形值必须让 load_config 失败——这是对照组，
        # 证明下面 queue_mode=True 的"不失败"确实是因为没有解析，不是因为
        # 校验规则本身变松了。
        with self.assertRaises(Exception):
            load_config(malformed_env)

        config = load_config(malformed_env, require_question=False, queue_mode=True)
        self.assertEqual(config.mcp_servers, {})

        legit_shared_servers = '{"shared": {"type": "stdio", "command": "should-not-appear"}}'
        config_with_legit_value = load_config(
            worker_env(LINGXI_WORKER_MCP_SERVERS=legit_shared_servers),
            require_question=False,
            queue_mode=True,
        )
        self.assertEqual(
            config_with_legit_value.mcp_servers,
            {},
            "queue 模式下即使给了合法的共享配置也必须被忽略，不能出现在 WorkerConfig 里",
        )

    def test_turn_mode_still_parses_the_shared_mcp_servers_variable(self) -> None:
        """对照组：`queue_mode=False`（默认，`turn` 模式的真实姿态）必须保持
        既有行为不变——这个变量仍然是 `turn` 模式唯一的 MCP 配置来源。"""

        config = self._load(
            LINGXI_WORKER_MCP_SERVERS='{"query": {"type": "stdio", "command": "x"}}'
        )
        self.assertEqual(config.mcp_servers, {"query": {"type": "stdio", "command": "x"}})


class TerminalOutcomeSinkSeverityTest(unittest.TestCase):
    """Issue #291 独立审查：queue 模式终态收口日志（``worker.task.terminal``）
    在这一回合有 ``PreToolUse`` 拒绝时必须提到 ``warning`` 级别，不能和普通
    成功终态一样淹没在 ``info`` 里——``_terminal_outcome_sink`` 是这条日志
    唯一的落地出口（见 ``cli.py``），删掉它的级别判断分支会让本用例变红。"""

    def test_a_denied_call_raises_the_log_level_to_warning(self) -> None:
        from lingxi.apps.worker.cli import _terminal_outcome_sink

        err = io.StringIO()
        sink = _terminal_outcome_sink(err=err, trace_id="01J00000000000000000000WR3")

        sink(
            {
                "task_id": "tsk-1",
                "failure_code": None,
                "error_kind": None,
                "terminal_kind": "success",
                "output_safety_blocked": False,
                "output_safety_withheld": False,
                "output_safety_reasons": (),
                "denied_count": 1,
                "denied_tool_names": ("Bash",),
                "truncated": False,
            }
        )

        line = json.loads(err.getvalue().strip())
        self.assertEqual(line["level"], "warning")
        self.assertEqual(line["event"], "worker.task.terminal")
        self.assertEqual(line["denied_count"], 1)
        self.assertEqual(line["denied_tool_names"], ["Bash"])

    def test_no_denial_stays_at_info(self) -> None:
        from lingxi.apps.worker.cli import _terminal_outcome_sink

        err = io.StringIO()
        sink = _terminal_outcome_sink(err=err, trace_id="01J00000000000000000000WR3")

        sink(
            {
                "task_id": "tsk-1",
                "failure_code": None,
                "error_kind": None,
                "terminal_kind": "success",
                "output_safety_blocked": False,
                "output_safety_withheld": False,
                "output_safety_reasons": (),
                "denied_count": 0,
                "denied_tool_names": (),
                "truncated": False,
            }
        )

        line = json.loads(err.getvalue().strip())
        self.assertEqual(line["level"], "info")


class WorkerResourceGuardTest(unittest.TestCase):
    """V-护栏-01…07：worker 资源上限与 usage 出口。"""

    def _run_with_sdk(
        self,
        script,
        *,
        sdk_kwargs=None,
        env_overrides=None,
        clock=None,
    ):
        from lingxi.apps.worker.config import load_config
        from lingxi.apps.worker.turn import WorkerTurnExecutor

        fake = FakeAgentSDK(script, **(sdk_kwargs or {})).install(self)
        config = load_config(worker_env(**(env_overrides or {})))
        executor = WorkerTurnExecutor(config, clock=clock)
        report = asyncio.run(executor.run_turn(config.question))
        return report, fake, executor

    def test_v_hulan_01_max_turns_is_passed_to_sdk_and_has_distinct_guard_state(self) -> None:
        report, fake, _ = self._run_with_sdk(
            [{"kind": "text", "text": "尚未完成。"}],
            sdk_kwargs={"result_subtype": "error_max_turns", "result_is_error": True, "result_num_turns": 2},
            env_overrides={"LINGXI_WORKER_MAX_TURNS": "2"},
        )

        self.assertEqual(fake.options[0].max_turns, 2)
        self.assertEqual(report["failure"]["code"], "max_turns_exceeded")
        self.assertEqual(report["turn"]["termination_state"], "guarded")
        self.assertEqual(report["turn"]["termination_reason"], "max_turns_exceeded")
        self.assertTrue(report["turn"]["guard_triggered"])
        self.assertEqual(report["turn"]["result_delivery"], "not_confirmed")

    def test_v_hulan_02_wall_clock_and_cancel_are_distinct_from_max_turns(self) -> None:
        import asyncio as _asyncio

        hanging = FakeAgentSDK([{"kind": "text", "text": "占位"}]).install(self)
        module = sys.modules["claude_agent_sdk"]

        class HangingClient(module.ClaudeSDKClient):  # type: ignore[misc]
            def receive_response(self):
                async def _gen():
                    await _asyncio.sleep(3600)
                    yield  # pragma: no cover

                return _gen()

        module.ClaudeSDKClient = HangingClient
        from lingxi.apps.worker.config import load_config
        from lingxi.apps.worker.turn import WorkerTurnExecutor

        config = load_config(worker_env(LINGXI_WORKER_TURN_TIMEOUT_SECONDS="0.01"))
        timeout_report = _asyncio.run(WorkerTurnExecutor(config).run_turn(config.question))
        self.assertEqual(timeout_report["failure"]["code"], "turn_timeout")
        self.assertEqual(timeout_report["turn"]["termination_reason"], "turn_timeout")

        class CancelledClient(module.ClaudeSDKClient):  # type: ignore[misc]
            def receive_response(self):
                async def _gen():
                    raise _asyncio.CancelledError()
                    yield  # pragma: no cover

                return _gen()

        module.ClaudeSDKClient = CancelledClient
        cancel_report = _asyncio.run(WorkerTurnExecutor(config).run_turn(config.question))
        self.assertEqual(cancel_report["failure"]["code"], "cancelled")
        self.assertEqual(cancel_report["turn"]["termination_reason"], "cancelled")
        self.assertNotEqual(cancel_report["turn"]["termination_reason"], "turn_timeout")
        del hanging

    def test_v_hulan_03_guarded_turn_with_confirmed_result_is_marked_early(self) -> None:
        report, _, _ = self._run_with_sdk(
            [
                {"kind": "tool", "tool": READ_ONLY_TOOL, "input": {"metric": "dau"}, "result": ok_result()},
                {"kind": "text", "text": "近 7 天日活是 1024。"},
            ],
            sdk_kwargs={"result_subtype": "error_max_turns", "result_is_error": True, "result_num_turns": 2},
        )

        self.assertFalse(report["turn"]["closed"])
        self.assertTrue(report["turn"]["guard_triggered"])
        self.assertEqual(report["turn"]["result_delivery"], "confirmed")
        self.assertEqual(report["turn"]["user_result"], "obtained")
        self.assertEqual(report["audit"]["termination_reason"], "max_turns_exceeded")

    def test_v_hulan_04_resource_counters_match_injected_execution(self) -> None:
        from lingxi.apps.worker.config import DEFAULT_TURN_TIMEOUT_SECONDS

        # 四次取值对应 turn.py 的 started_at、适配器的 business_start、业务阶段
        # 结束、turn.py 的收口耗时；business_duration=1.2，drain_duration=0.05，
        # 二者相加等于沿用已久的 duration_seconds=1.25（#143：三者分开记录）。
        ticks = iter((100.0, 100.0, 101.2, 101.25))
        report, _, _ = self._run_with_sdk(
            [
                {"kind": "tool", "tool": READ_ONLY_TOOL, "input": {"metric": "dau"}, "result": ok_result()},
                {"kind": "tool", "tool": "Write", "input": {"file_path": "probe"}},
                {"kind": "text", "text": "已完成。"},
            ],
            sdk_kwargs={"result_num_turns": 3},
            clock=lambda: next(ticks),
        )

        resources = report["resources"]
        self.assertEqual(resources["agent_turns"], 3)
        self.assertEqual(resources["agent_turns_status"], "known")
        self.assertEqual(resources["duration_seconds"], 1.25)
        self.assertEqual(resources["tool_call_count"], 2)
        self.assertEqual(resources["executed_tool_call_count"], 1)
        self.assertAlmostEqual(resources["business_duration_seconds"], 1.2, places=9)
        self.assertAlmostEqual(resources["drain_duration_seconds"], 0.05, places=9)
        self.assertEqual(resources["business_execution_budget_seconds"], DEFAULT_TURN_TIMEOUT_SECONDS)

    def test_v_hulan_04_missing_num_turns_is_unknown_not_zero(self) -> None:
        """ResultMessage 没有 num_turns 时必须保留未知，不能用 0 填洞。"""
        report, _, _ = self._run_with_sdk(
            [{"kind": "text", "text": "已完成。"}],
        )

        resources = report["resources"]
        self.assertIsNone(resources["agent_turns"])
        self.assertEqual(resources["agent_turns_status"], "unknown")

    def test_v_hulan_05_unknown_usage_is_explicit_and_never_zero_filled(self) -> None:
        report, _, _ = self._run_with_sdk(
            [{"kind": "text", "text": "没有拿到可确认结果。"}],
            sdk_kwargs={"result_num_turns": 1},
        )

        usage = report["resources"]["usage"]
        self.assertEqual(usage["status"], "unknown")
        self.assertEqual(usage["source"], "mock")
        self.assertNotIn("fields", usage)
        self.assertNotIn("input_tokens", json.dumps(report))
        self.assertNotIn('"output_tokens": 0', json.dumps(report))

    def test_v_hulan_05_unrecognised_usage_dict_is_unknown_not_zero_filled(self) -> None:
        """SDK 给出 usage 字典但没有可识别 token 字段时不得伪造 0。"""
        report, _, _ = self._run_with_sdk(
            [{"kind": "text", "text": "没有拿到可确认结果。"}],
            sdk_kwargs={"result_num_turns": 1, "result_usage": {"foo": 1}},
        )

        usage = report["resources"]["usage"]
        self.assertEqual(
            usage,
            {
                "status": "unknown",
                "source": "mock",
                "reason": "no_recognized_token_counts",
            },
        )

    def test_v_hulan_05_mock_usage_is_numeric_summary_only(self) -> None:
        report, _, _ = self._run_with_sdk(
            [{"kind": "text", "text": "已完成。"}],
            sdk_kwargs={
                "result_num_turns": 1,
                "result_usage": {
                    "input_tokens": 12,
                    "output_tokens": 7,
                    "secret": FAKE_CREDENTIAL,
                    "result": "模型正文不得进入 usage",
                },
            },
        )

        usage = report["resources"]["usage"]
        self.assertEqual(usage, {"status": "known", "source": "mock", "fields": {"input_tokens": 12, "output_tokens": 7}})
        self.assertNotIn(FAKE_CREDENTIAL, json.dumps(report, ensure_ascii=False))
        self.assertNotIn("模型正文不得进入 usage", json.dumps(report, ensure_ascii=False))

    def test_v_hulan_06_mock_usage_carries_mock_source_and_no_billing_claim(self) -> None:
        from lingxi.core.execution.audit import TurnAudit
        from lingxi.core.execution.message_stream import TurnStreamRecorder

        recorder = TurnStreamRecorder(TurnAudit())
        recorder.handle(
            {
                "kind": "result",
                "is_error": False,
                "subtype": "success",
                "usage_source": "mock",
                "usage": {"input_tokens": 1, "output_tokens": 2},
            }
        )

        self.assertEqual(recorder.usage_summary["source"], "mock")
        self.assertEqual(recorder.usage_summary["status"], "known")

    def test_v_hulan_07_minimum_turn_guard_reaches_sdk_and_ends_early(self) -> None:
        """最小轮数仍传入 SDK，并以护栏终态结束；无重试/绕过是结构性论证。"""
        report, fake, _ = self._run_with_sdk(
            [{"kind": "text", "text": "尚未完成。"}],
            sdk_kwargs={
                "result_subtype": "error_max_turns",
                "result_is_error": True,
                "result_num_turns": 1,
            },
            env_overrides={
                "LINGXI_WORKER_MAX_TURNS": "1",
                "LINGXI_WORKER_TURN_TIMEOUT_SECONDS": "0.01",
            },
        )

        self.assertEqual(fake.options[0].max_turns, 1)
        self.assertEqual(fake.prompts, ["近 7 天的活跃用户数是多少？"])
        self.assertEqual(report["failure"]["code"], "max_turns_exceeded")
        self.assertEqual(report["turn"]["termination_state"], "guarded")
        self.assertTrue(report["turn"]["guard_triggered"])


class ReviewHardeningTest(unittest.TestCase):
    """PR #47 独立复查后补的收口信号用例：SDK 自报错误、终止双计数、屏障绕过、
    配置错误脱敏、中断契约。全部先按复查报告的复现步骤确认原实现确实说谎/泄漏，
    再修复并以本组用例钉住。"""

    def _run_cli(self, sdk: FakeAgentSDK):
        from lingxi.apps.worker.cli import main

        sdk.install(self)
        stdout, stderr = io.StringIO(), io.StringIO()
        code = main(env=worker_env(), stdout=stdout, stderr=stderr)
        return code, json.loads(stdout.getvalue()), stderr.getvalue()

    def test_an_sdk_error_result_is_not_reported_as_closed(self) -> None:
        """SDK 自己说这轮错了（如 error_max_turns 截断），不得报成正常收口。"""
        script = [{"kind": "text", "text": "我正在查询，请稍等……"}]
        code, payload, stderr_text = self._run_cli(
            FakeAgentSDK(script, result_subtype="error_max_turns", result_is_error=True)
        )

        self.assertEqual(code, 4)
        self.assertFalse(payload["turn"]["closed"])
        self.assertTrue(payload["turn"]["sdk_result_is_error"])
        self.assertEqual(payload["turn"]["sdk_result_subtype"], "error_max_turns")
        self.assertEqual(payload["failure"]["code"], "max_turns_exceeded")
        self.assertIn('"level": "error"', stderr_text.replace(": ", ": "))

    def test_a_missing_result_message_does_not_close_the_turn(self) -> None:
        """Stop 到了但没有 ResultMessage：双计数缺一即不收口（原实现无此用例，
        删掉 result_message_count == 1 条件测试曾全绿）。"""
        script = [{"kind": "text", "text": "答案。"}]
        code, payload, _ = self._run_cli(FakeAgentSDK(script, result_messages=0))

        self.assertEqual(code, 2)
        self.assertFalse(payload["turn"]["closed"])
        self.assertEqual(payload["turn"]["sdk_result_message_count"], 0)
        self.assertEqual(payload["turn"]["terminal_result_count"], 1)

    def test_a_duplicate_result_message_does_not_close_the_turn(self) -> None:
        """桩级接线守卫：若本侧计数/条件被改坏（合并计数、删条件），本用例变红。
        注意观测边界：真实 SDK 的 receive_response() 在首条 ResultMessage 后即返回，
        底层重复投递在本层结构性不可见——本用例不声称能检测那种情况（终轮 Codex）。"""
        script = [{"kind": "text", "text": "答案。"}]
        code, payload, _ = self._run_cli(FakeAgentSDK(script, result_messages=2))

        self.assertEqual(code, 2)
        self.assertFalse(payload["turn"]["closed"])
        self.assertEqual(payload["turn"]["sdk_result_message_count"], 2)

    def test_a_gate_bypass_is_a_distinct_failure_signal(self) -> None:
        """只发 PostToolUse 不发 PreToolUse（hook 未注册/被跳过的形状）：
        ungated_count>0 必须以专属退出码与 error 日志暴露，不得报成功。"""
        script = [
            {"kind": "tool", "tool": "Write", "input": {"file_path": "x"}, "result": "done"},
            {"kind": "text", "text": "已写入。"},
        ]
        code, payload, stderr_text = self._run_cli(FakeAgentSDK(script, skip_pre_tool_use=True))

        self.assertEqual(code, 5)
        self.assertFalse(payload["turn"]["closed"])
        self.assertTrue(payload["turn"]["gate_bypassed"])
        self.assertGreaterEqual(payload["audit"]["ungated_count"], 1)
        self.assertIn('"gate_bypassed": true', stderr_text)
        self.assertIn('"level": "error"', stderr_text)

    def test_a_normal_turn_reports_result_and_receipt_counters(self) -> None:
        """把此前"只上报不断言"的字段钉住：正常回合恰好一条非错误终止消息、
        回执计数与调用数一致。"""
        script = [
            {"kind": "tool", "tool": READ_ONLY_TOOL, "input": {"metric": "dau"}, "result": ok_result()},
            {"kind": "text", "text": "日活 1024。"},
        ]
        code, payload, _ = self._run_cli(FakeAgentSDK(script))

        self.assertEqual(code, 0)
        self.assertIs(payload["turn"]["sdk_result_is_error"], False)
        self.assertEqual(payload["turn"]["sdk_result_subtype"], "success")
        self.assertEqual(payload["audit"]["tool_result_count"], 1)

    def test_a_config_error_never_echoes_a_token_shaped_value(self) -> None:
        """配置错误文案与模型侧同一标准：出口脱敏（原实现把原值同时打进
        stdout 与 stderr，独立复查已实测复现）。"""
        from lingxi.apps.worker.cli import main

        probe = "Bearer LINGXI_FAKE_SECRET_a1b2c3d4e5f6g7h8"
        stdout, stderr = io.StringIO(), io.StringIO()
        code = main(env=worker_env(LINGXI_WORKER_READONLY_TOOLS=probe), stdout=stdout, stderr=stderr)

        self.assertEqual(code, 3)
        self.assertNotIn(probe, stdout.getvalue())
        self.assertNotIn(probe, stderr.getvalue())
        self.assertNotIn("LINGXI_FAKE_SECRET_a1b2c3d4e5f6g7h8", stdout.getvalue() + stderr.getvalue())

    def test_a_hung_transport_times_out_as_an_explicit_failure(self) -> None:
        """Codex 复查：SDK 传输挂住不发终止消息时必须墙钟超时并出失败报告，
        不得永久等待。"""
        import asyncio as _asyncio

        sdk = FakeAgentSDK([{"kind": "text", "text": "占位"}])
        sdk.install(self)
        module = sys.modules["claude_agent_sdk"]

        class HangingClient(module.ClaudeSDKClient):  # type: ignore[misc]
            def receive_response(self):
                async def _gen():
                    await _asyncio.sleep(3600)
                    yield  # pragma: no cover

                return _gen()

        module.ClaudeSDKClient = HangingClient

        from lingxi.apps.worker.cli import main

        stdout, stderr = io.StringIO(), io.StringIO()
        code = main(env=worker_env(LINGXI_WORKER_TURN_TIMEOUT_SECONDS="0.05"), stdout=stdout, stderr=stderr)

        self.assertEqual(code, 4)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["failure"]["code"], "turn_timeout")

    def test_an_invalid_trace_id_is_rejected_without_echoing_it(self) -> None:
        """Codex 复查：外部 trace_id 只收 26 位 Crockford ULID；误接的令牌
        不得随错误输出原样外泄。"""
        from lingxi.apps.worker.cli import main

        probe = "Bearer_FAKE_TOKEN_a1b2c3d4e5f6g7h8i9"
        stdout, stderr = io.StringIO(), io.StringIO()
        code = main(env=worker_env(LINGXI_WORKER_TRACE_ID=probe), stdout=stdout, stderr=stderr)

        self.assertEqual(code, 3)
        combined = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(probe, combined)
        payload = json.loads(stdout.getvalue())
        self.assertNotEqual(payload["trace_id"], probe)
        self.assertEqual(len(payload["trace_id"]), 26)

    def test_sdk_stderr_lines_are_redacted_and_structured(self) -> None:
        """Codex 复查：SDK 子进程 stderr 必须经脱敏回调进结构化日志，
        不得裸继承 fd 2。"""
        from lingxi.apps.worker.config import load_config
        from lingxi.apps.worker.turn import WorkerTurnExecutor

        sdk = FakeAgentSDK([{"kind": "text", "text": "占位"}])
        sdk.install(self)

        captured = io.StringIO()
        config = load_config(worker_env())
        executor = WorkerTurnExecutor(config, stderr_stream=captured)
        options = executor.build_session_options()

        self.assertTrue(callable(options.stderr))
        options.stderr("boot failed: token LINGXI_FAKE_SECRET_z9y8x7w6v5u4t3s2")

        line = json.loads(captured.getvalue())
        self.assertEqual(line["event"], "worker.sdk.stderr")
        self.assertEqual(line["trace_id"], config.trace_id)
        self.assertNotIn("LINGXI_FAKE_SECRET_z9y8x7w6v5u4t3s2", captured.getvalue())

    def test_gate_bypass_outranks_a_session_failure_in_the_exit_code(self) -> None:
        """终轮 Codex：绕过之后传输又失败时，退出码必须先报安全边界失效（5），
        不得被通用会话失败（4）吞掉。"""
        sdk = FakeAgentSDK(
            [{"kind": "tool", "tool": "Write", "input": {"file_path": "x"}, "result": "done"}],
            skip_pre_tool_use=True,
        )
        sdk.raise_after_steps = RuntimeError("模拟传输失败")
        code, payload, _ = self._run_cli(sdk)

        self.assertEqual(code, 5)
        self.assertIsNotNone(payload["failure"])
        self.assertTrue(payload["turn"]["gate_bypassed"])

    def test_a_non_finite_turn_timeout_is_rejected(self) -> None:
        """终轮 Codex：inf/1e999 会让 asyncio.timeout 永不触发，配置期就拒绝。"""
        from lingxi.apps.worker.cli import main

        for value in ("inf", "Infinity", "1e999", "nan"):
            with self.subTest(value=value):
                stdout, stderr = io.StringIO(), io.StringIO()
                code = main(env=worker_env(LINGXI_WORKER_TURN_TIMEOUT_SECONDS=value), stdout=stdout, stderr=stderr)
                self.assertEqual(code, 3)

    def test_an_interruption_still_produces_a_report(self) -> None:
        """CancelledError 属 BaseException：不接住就没有报告，违反 stdout 契约。"""
        import asyncio as _asyncio

        from lingxi.apps.worker.config import load_config
        from lingxi.apps.worker.turn import WorkerTurnExecutor

        sdk = FakeAgentSDK([{"kind": "text", "text": "半截"}])
        sdk.install(self)

        module = sys.modules["claude_agent_sdk"]

        class InterruptingClient(module.ClaudeSDKClient):  # type: ignore[misc]
            def receive_response(self):
                async def _gen():
                    raise _asyncio.CancelledError()
                    yield  # pragma: no cover

                return _gen()

        module.ClaudeSDKClient = InterruptingClient

        config = load_config(worker_env())
        report = _asyncio.run(WorkerTurnExecutor(config).run_turn(config.question))

        self.assertIsNotNone(report["failure"])
        self.assertEqual(report["failure"]["code"], "cancelled")
        self.assertFalse(report["turn"]["closed"])


class StopCausalTerminationTest(unittest.TestCase):
    """#201：``/stop`` 恰好落在回合终点、SDK 以 ``aborted_*`` 收尾时的失败归类。

    正常情况下本地 stop 会让适配器抛 ``AgentSessionInterrupted``（→ ``interrupted``）。
    但 SDK 完全可能抢在 ``client.interrupt()`` 返回之前就把这一轮收完，此时回合从
    形状上看就是"SDK 自己 abort 了"，原实现一律归为 ``cancelled``，队列侧按 #195
    的终态优先级收口成 ``failed/session_failed``，用户主动停止却看到「请稍后重试」
    类通用失败文案。

    修法是**本地事实因果接线**，不是反推：只有本进程真的发出过 ``interrupt()``
    才改判。三个用例分别锁住这条因果的两端（有因、无因）与"晚到的 stop 从未成为
    本地中断"。``interrupted`` 之后的终态（``stopped``）由
    ``tests/test_worker_queue_consumer.py`` 的
    ``test_a_genuinely_interrupted_turn_keeps_the_stopped_terminal`` 守住，本文件
    不重复 service 层的终态优先级判定。
    """

    def _aborted_result(self, terminal_reason: str):
        return StubResultMessage(
            subtype="error_during_execution", is_error=True, terminal_reason=terminal_reason
        )

    def test_a_local_interrupt_makes_an_aborted_termination_a_stop(self) -> None:
        """本地已发出 interrupt + SDK 以 aborted_* 收尾 → ``interrupted``。

        把 ``turn.py`` 里的 ``interrupt_requested`` 接线撤掉（或让适配器在
        ``await interrupt()`` 之后才记事实）必须让本用例变红。
        """

        from lingxi.apps.worker.config import load_config
        from lingxi.apps.worker.turn import WorkerTurnExecutor

        FakeAgentSDK([]).install(self)
        module = sys.modules["claude_agent_sdk"]
        interrupt_sent = asyncio.Event()
        aborted_result = self._aborted_result("aborted_streaming")

        class RacingClient(module.ClaudeSDKClient):  # type: ignore[misc]
            async def interrupt(self):
                # 中断请求已经送到 SDK，这次调用却永远不返回：用确定的同步点
                # 复现竞态，不依赖事件循环的调度巧合。
                interrupt_sent.set()
                await asyncio.sleep(3600)

            def receive_response(self):
                async def _gen():
                    await interrupt_sent.wait()
                    yield aborted_result

                return _gen()

        module.ClaudeSDKClient = RacingClient
        config = load_config(worker_env())
        stderr = io.StringIO()
        executor = WorkerTurnExecutor(config, stderr_stream=stderr)

        async def scenario():
            stop_event = asyncio.Event()
            stop_event.set()
            return await executor.run_turn(config.question, stop_event=stop_event)

        report = asyncio.run(scenario())

        self.assertEqual(report["failure"]["code"], "interrupted")
        self.assertEqual(report["turn"]["termination_reason"], "interrupted")
        self.assertFalse(report["turn"]["closed"])
        # 竞态每发生一次留一条可计数的结构化痕迹，用来观察真实频率（#201）。
        lines = [json.loads(line) for line in stderr.getvalue().splitlines() if line.strip()]
        races = [line for line in lines if line["event"] == "worker.stop.interrupt_race"]
        self.assertEqual(len(races), 1)
        self.assertEqual(races[0]["terminal_reason"], "aborted_streaming")
        self.assertEqual(races[0]["trace_id"], config.trace_id)

    def test_an_sdk_abort_without_any_local_interrupt_stays_a_failure(self) -> None:
        """回归锁：无人 stop 时 SDK 自行 ``aborted_*`` 仍是 ``cancelled``（失败分支）。

        ``aborted_*`` 是 SDK 自报的，可以在无 stop 时出现；把它当成 stop 的别名
        会掩盖真实的 SDK 终止失败（PR #198 一级独立审查 P1-2 裁定）。
        """

        from lingxi.apps.worker.config import load_config
        from lingxi.apps.worker.turn import WorkerTurnExecutor

        for terminal_reason in ("aborted_streaming", "aborted_tools"):
            with self.subTest(terminal_reason=terminal_reason):
                FakeAgentSDK(
                    [{"kind": "text", "text": "半截正文"}], terminal_reason=terminal_reason
                ).install(self)
                config = load_config(worker_env())
                stderr = io.StringIO()
                report = asyncio.run(
                    WorkerTurnExecutor(config, stderr_stream=stderr).run_turn(config.question)
                )

                self.assertEqual(report["failure"]["code"], "cancelled")
                self.assertEqual(report["turn"]["termination_reason"], "cancelled")
                self.assertNotIn("worker.stop.interrupt_race", stderr.getvalue())

    def test_a_stop_that_never_became_a_local_interrupt_keeps_the_failure(self) -> None:
        """回归锁：stop 晚到、本进程从未发出 ``interrupt()`` → 仍是 ``cancelled``。

        因果接的是"本地真的中断过"，不是"某一刻有人请求过停止"。
        """

        from lingxi.apps.worker.config import load_config
        from lingxi.apps.worker.turn import WorkerTurnExecutor

        FakeAgentSDK([]).install(self)
        module = sys.modules["claude_agent_sdk"]
        stop_event = asyncio.Event()
        interrupts: list = []
        aborted_result = self._aborted_result("aborted_tools")

        class LateStopClient(module.ClaudeSDKClient):  # type: ignore[misc]
            async def interrupt(self):  # pragma: no cover - 被调用即说明因果接错
                interrupts.append("called")

            def receive_response(self):
                async def _gen():
                    yield aborted_result
                    # 回合已经收完之后才有人 stop：本进程没有、也不该再发中断。
                    stop_event.set()

                return _gen()

        module.ClaudeSDKClient = LateStopClient
        config = load_config(worker_env())
        stderr = io.StringIO()
        report = asyncio.run(
            WorkerTurnExecutor(config, stderr_stream=stderr).run_turn(
                config.question, stop_event=stop_event
            )
        )

        self.assertEqual(interrupts, [])
        self.assertEqual(report["failure"]["code"], "cancelled")
        self.assertNotIn("worker.stop.interrupt_race", stderr.getvalue())


class WorkerCliTest(unittest.TestCase):
    """入口必须真的能以 ``python -m lingxi.apps.worker`` 启动。"""

    def test_module_entry_exits_with_a_config_error_and_a_json_report(self) -> None:
        import lingxi

        package_root = str(pathlib.Path(lingxi.__file__).resolve().parents[1])
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("LINGXI_") and key != "PYTHONPATH"
        }
        env["PYTHONPATH"] = package_root

        completed = subprocess.run(
            [sys.executable, "-m", "lingxi.apps.worker"],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

        self.assertEqual(completed.returncode, 3, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["failure"]["code"], "config_error")
        self.assertIn("LINGXI_WORKER_QUESTION", payload["failure"]["message"])

    def test_cli_prints_one_json_object_on_stdout_and_structured_logs_on_stderr(self) -> None:
        from lingxi.apps.worker.cli import main

        script = [
            {"kind": "tool", "tool": READ_ONLY_TOOL, "input": {"metric": "dau"}, "result": ok_result()},
            {"kind": "text", "text": "日活 1024。"},
        ]
        FakeAgentSDK(script).install(self)
        stdout, stderr = io.StringIO(), io.StringIO()

        code = main(env=worker_env(), stdout=stdout, stderr=stderr)

        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["turn"]["user_result"], "obtained")
        self.assertEqual(payload["trace_id"], "01J0000000000000000TEST000")
        lines = [json.loads(line) for line in stderr.getvalue().splitlines() if line.strip()]
        self.assertTrue(lines, "结构化日志必须写到 stderr")
        for line in lines:
            self.assertEqual(line["trace_id"], "01J0000000000000000TEST000")
        self.assertIn("worker.turn.finished", [line["event"] for line in lines])

    def test_cli_passes_the_known_metric_description_as_external_text(self) -> None:
        from lingxi.apps.worker.cli import main

        fake = FakeAgentSDK([{"kind": "text", "text": "已完成。"}]).install(self)
        stdout, stderr = io.StringIO(), io.StringIO()

        code = main(
            env=worker_env(
                LINGXI_WORKER_METRIC_DESCRIPTION="指标描述来自已知外部目录。",
            ),
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(code, 0)
        self.assertEqual(len(fake.prompts), 1)
        self.assertIn('role="data"', fake.prompts[0])
        self.assertIn("[待分析内容]", fake.prompts[0])
        self.assertIn("指标描述来自已知外部目录。", fake.prompts[0])

    def test_cli_exit_code_reports_an_unclosed_turn(self) -> None:
        from lingxi.apps.worker.cli import main

        FakeAgentSDK([{"kind": "text", "text": "   "}]).install(self)
        stdout, stderr = io.StringIO(), io.StringIO()

        code = main(env=worker_env(), stdout=stdout, stderr=stderr)

        self.assertEqual(code, 2)
        self.assertFalse(json.loads(stdout.getvalue())["turn"]["closed"])


if __name__ == "__main__":
    unittest.main()
