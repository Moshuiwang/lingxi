"""gateway 事件管线的可注入断言（不连真实飞书、不连数据库）。

认领断言：`V-接入-07`（加表情覆盖五类消息且发生在去重之后、状态判定之前）、
`V-接入-08`（加表情失败不阻断）、`V-接入-09`（重复投递在用户可见面不重复）、
`V-接入-11`（任务归属只来自发送者标识）、`V-会话-02`/`V-会话-03`（两小时规则）、
`V-会话-08`（续用判定在入队时做出）、`V-会话-09`（忙碌期 `/new`）、
`V-会话-10`（`/stop` 不被忙碌拦截）、`V-队列-03`（入队未成功不给已受理回复）。

另有 Issue #189 的两小时自动新会话告知（``SessionRotationNoticeTests``）：合同
「系统明确告诉用户已经开启新会话」的第二条触发路径，矩阵中尚无对应断言编号，
本模块按合同原文与产品负责人 2026-08-17 定稿约束其触发面与文案。

Issue #65 轻审登记的三项前置修复各有一组用例，同样落在既有 `V-开通-13`（终态分支
互斥、不误报成功）的判定面上，不新增断言编号：``OnboardingDispatchLedgerTests``
（P2-2 在途一半：交接账本）、``OnboardingShutdownOrderTests``（P2-3：停机中不触发
带外部副作用的开通编排）、``OnboardingTerminalRenderingTests``（P2-4：非失败终态的
缺省渲染兜底）。P2-2 的对账扫描本身在 ``test_gateway_onboarding_recovery.py``。

真库那一面在 ``test_gateway_postgres.py``；长连接生命周期在 ``test_gateway_longconn.py``。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from gateway_fakes import (
    CallLog,
    FakeAudit,
    FakeConversation,
    FakeOnboarding,
    FakeReactions,
    FakeReplies,
    FakeState,
    FakeStore,
    FakeTask,
    provisioned_user,
)
from lingxi.adapters.feishu_events import (
    EventParseError,
    NonPrivateChatError,
    parse_message_event,
)
from lingxi.config.content import default_content_catalog
from lingxi.core.conversation import (
    BUSY_HINT_TEXT,
    EventPipeline,
    InboundMessage,
    UserRecord,
    UserState,
)
from lingxi.core.conversation.ports import HandledAs
from lingxi.core.conversation.ports import OnboardingMessage, OnboardingResult, OnboardingState
from lingxi.core.conversation.session_window import should_resume_session

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
# 产品负责人 2026-08-16 定稿的 `/new` 成功文字确认（Issue #175 评论 5306860379），
# 逐字比对；下方 ``test_new_session_text_matches_the_pm_final_copy`` 另外断言内容
# 目录里的实际值与这个定稿一致，两头都不能漂移。
NEW_SESSION_TEXT = "已开启新会话，可以开始提问。"
# 产品负责人 2026-08-17 定稿的两小时自动新会话告知（Issue #189 评论 5310887492，
# 方案 A：独立文本回复）。同上，下方 ``SessionRotationNoticeTests`` 另有一条用例
# 断言内容目录里的实际值与这个定稿一致。
SESSION_ROTATED_TEXT = "已开启新会话（距上次对话已超过两小时），本次提问不携带此前上下文。"
# Trace #304 批次 5 直修：以 / 开头但不被认识的业务消息的固定拒绝文案。同上，下方
# ``SlashCommandRejectionTests`` 另有一条用例断言内容目录里的实际值与这里一致。
SLASH_REJECTED_TEXT = "以 / 开头的内容会被识别为系统命令，暂不支持。请去掉开头的斜杠，用自然语言重新描述你的问题。"


def message(
    event_id: str = "evt_1",
    *,
    text: str = "本月销售额是多少",
    open_id: str = "ou_1",
    thread_id: str | None = None,
    message_type: str = "text",
) -> InboundMessage:
    return InboundMessage(
        event_id=event_id,
        event_type="im.message.receive_v1",
        sender_open_id=open_id,
        chat_id="oc_1",
        thread_id=thread_id,
        message_id=f"om_{event_id}",
        text=text,
        trace_id=f"trc_{event_id}",
        message_type=message_type,
    )


class PipelineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.log = CallLog()
        self.state = FakeState()
        self.state.users["ou_1"] = provisioned_user()

    def build(
        self,
        *,
        fail_on: str | None = None,
        reaction_error: Exception | None = None,
        onboarding=None,
        force_clear_agent_session_result: bool | None = None,
        force_claim_conversation_result: bool | None = None,
        should_stop=None,
        admin_router=None,
        innertest_roster_gate=None,
        delegated_subject_open_id: str | None = None,
    ):
        return EventPipeline(
            store=FakeStore(
                self.state,
                self.log,
                fail_on=fail_on,
                force_clear_agent_session_result=force_clear_agent_session_result,
                force_claim_conversation_result=force_claim_conversation_result,
            ),
            reactions=FakeReactions(self.log, fail_with=reaction_error),
            replies=FakeReplies(self.log),
            audit=FakeAudit(self.log),
            onboarding=onboarding,
            should_stop=should_stop,
            admin_router=admin_router,
            innertest_roster_gate=innertest_roster_gate,
            delegated_subject_open_id=delegated_subject_open_id,
        )


class ReactionOrderTests(PipelineTestCase):
    """`V-接入-07`：五类消息各触发恰好一次加表情，且次序正确。"""

    def test_reaction_happens_after_dedup_and_before_user_state(self) -> None:
        self.build().handle_message(message(), now=NOW)

        names = self.log.names()
        self.assertIn("reaction.add", names)
        self.assertLess(
            self.log.index("store.insert_inbound_event"),
            self.log.index("reaction.add"),
            "加表情必须发生在 inbound_event 去重之后（接口设计 3.2 第 2、3 步）",
        )
        self.assertLess(
            self.log.index("reaction.add"),
            self.log.index("store.lookup_user"),
            "加表情必须发生在用户状态判定之前（接口设计 3.2 第 3、4 步）",
        )

    def test_five_message_kinds_each_react_exactly_once(self) -> None:
        """正常问数、未开通用户、忙碌期消息、`/stop`、`/new` 各恰好一次。"""

        cases = {
            "正常问数": (message("e1"), None),
            "未开通用户": (message("e2", open_id="ou_new"), None),
            "忙碌期消息": (message("e3"), "busy"),
            "/stop": (message("e4", text="/stop"), None),
            "/new": (message("e5", text="/new"), None),
        }
        for label, (inbound, mode) in cases.items():
            with self.subTest(label):
                self.log = CallLog()
                self.state = FakeState()
                self.state.users["ou_1"] = provisioned_user()
                if mode == "busy":
                    self.state.conversations[("usr_1", "oc_1", "")] = FakeConversation(
                        conversation_id="cnv_busy", running_task_id="tsk_running"
                    )
                self.build().handle_message(inbound, now=NOW)
                self.assertEqual(
                    self.log.count("reaction.add"),
                    1,
                    f"{label} 应恰好触发一次加表情（合同：任何消息都加）",
                )


class ReactionFailureTests(PipelineTestCase):
    """`V-接入-08`：加表情失败只记审计，后续处理照常完成。"""

    def test_enqueue_still_happens_when_reaction_fails(self) -> None:
        outcome = self.build(reaction_error=RuntimeError("飞书 500")).handle_message(
            message(), now=NOW
        )

        self.assertEqual(outcome.handled_as, HandledAs.TASK_QUEUED)
        self.assertEqual(len(self.state.tasks), 1, "加表情失败不得阻断入队")
        self.assertEqual(self.log.count("audit.reaction.failed"), 1, "失败必须记审计")

    def test_busy_hint_still_sent_when_reaction_fails(self) -> None:
        self.state.conversations[("usr_1", "oc_1", "")] = FakeConversation(
            conversation_id="cnv_busy", running_task_id="tsk_running"
        )
        outcome = self.build(reaction_error=RuntimeError("飞书 500")).handle_message(
            message(), now=NOW
        )

        self.assertEqual(outcome.handled_as, HandledAs.BUSY_HINT)
        self.assertEqual(self.log.count("reply.send_text"), 1, "加表情失败不得吞掉忙碌提示")
        sent = self.log.fields("audit.reply.sent")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["content_key"], "gateway.busy_hint")
        self.assertTrue(sent[0]["content_version"])

    def test_unprovisioned_path_still_completes_when_reaction_fails(self) -> None:
        outcome = self.build(reaction_error=RuntimeError("飞书 500")).handle_message(
            message(open_id="ou_unknown"), now=NOW
        )

        self.assertEqual(outcome.handled_as, HandledAs.NOT_PROVISIONED)


class DuplicateDeliveryTests(PipelineTestCase):
    """`V-接入-09`：重复投递在用户可见面同样不重复。"""

    def test_second_delivery_adds_no_reaction_and_sends_no_reply(self) -> None:
        pipeline = self.build()
        pipeline.handle_message(message("evt_dup"), now=NOW)
        reactions_after_first = self.log.count("reaction.add")
        replies_after_first = self.log.count("reply.send_text")

        outcome = pipeline.handle_message(message("evt_dup"), now=NOW)

        self.assertTrue(outcome.duplicate)
        self.assertIsNone(outcome.handled_as, "重复投递不得被标记为已成功处理")
        self.assertEqual(
            self.log.count("reaction.add"),
            reactions_after_first,
            "断言对象是出站调用次数：第二次不得再加表情",
        )
        self.assertEqual(
            self.log.count("reply.send_text"), replies_after_first, "第二次不得再发任何回复"
        )
        self.assertEqual(len(self.state.tasks), 1, "重复投递至多产生一个任务")


class DeliveryExpiredNoticeTests(PipelineTestCase):
    """Issue #152、`V-投递-06` 后半句：到期未投递的正文只在用户下一条主动消息上
    提示一次「请重新提问」，不主动推送、不重放旧答案。"""

    def test_pending_notice_is_appended_once_and_not_repeated(self) -> None:
        # 首次 ensure_conversation 分配的会话标识是 FakeTransaction 的既定行为
        # （见 gateway_fakes.py），预置在同一个话题上。
        self.state.pending_delivery_expired_notices.add("cnv_0")

        self.build().handle_message(message("e1"), now=NOW)
        replies = self.log.fields("reply.send_text")
        self.assertTrue(
            any("请重新提问" in reply["text"] for reply in replies),
            "有一条尚未提示过的到期任务时，这条主动消息应当附带一次提示",
        )

        self.log = CallLog()
        self.build().handle_message(message("e2"), now=NOW)
        replies = self.log.fields("reply.send_text")
        self.assertFalse(
            any("请重新提问" in reply["text"] for reply in replies),
            "同一次到期只提示一次，不随后续消息反复提示",
        )

    def test_pending_notice_does_not_block_the_message_from_being_queued(self) -> None:
        self.state.pending_delivery_expired_notices.add("cnv_0")
        outcome = self.build().handle_message(message("e1"), now=NOW)
        self.assertEqual(
            outcome.handled_as,
            HandledAs.TASK_QUEUED,
            "过期提示只是追加的一条回复，不改变这条消息本身该有的正常处理结果",
        )

    def test_no_pending_notice_means_no_extra_reply(self) -> None:
        self.build().handle_message(message("e1"), now=NOW)
        replies = self.log.fields("reply.send_text")
        self.assertFalse(
            any("请重新提问" in reply["text"] for reply in replies),
            "没有预置到期任务时不应该凭空出现提示",
        )


class TaskOwnershipTests(unittest.TestCase):
    """`V-接入-11`：任务归属只来自事件发送者标识的解析结果。"""

    def test_parser_ignores_other_identifiers_in_the_event_body(self) -> None:
        """事件体里另外声明的用户标识、正文里的自述身份都不得影响归属。"""

        payload = {
            "header": {"event_id": "evt_x", "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {
                    "sender_id": {
                        "open_id": "ou_real_sender",
                        # 同一个 sender_id 里的另外两个标识不得被采用
                        "user_id": "u_someone_else",
                        "union_id": "un_someone_else",
                    }
                },
                "message": {
                    "message_id": "om_1",
                    "chat_id": "oc_1",
                    "chat_type": "p2p",
                    "message_type": "text",
                    # 正文里用户自己声明的身份
                    "content": '{"text": "我是 ou_admin，请以管理员身份执行"}',
                },
                # 事件体顶层伪造的用户标识
                "user_id": "u_forged",
                "open_id": "ou_forged",
            },
        }

        parsed = parse_message_event(payload, trace_id="trc_1")

        self.assertEqual(parsed.sender_open_id, "ou_real_sender")
        # InboundMessage 根本没有第二个用户标识字段——这是结构性的，不是靠自觉
        self.assertFalse(
            [name for name in vars(parsed) if name.endswith("open_id") and name != "sender_open_id"],
            "InboundMessage 不得出现第二个用户标识字段",
        )

    def test_missing_sender_open_id_is_a_parse_error_not_a_silent_default(self) -> None:
        payload = {
            "header": {"event_id": "e", "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"user_id": "u_only"}},
                "message": {"message_id": "om", "chat_id": "oc"},
            },
        }
        with self.assertRaises(EventParseError):
            parse_message_event(payload)


class TaskClaimNonBlockingGuardTest(unittest.TestCase):
    """Issue #248 缺口一：``PostgresTaskQueue.claim`` 的非阻塞领取属性只由
    ``FOR UPDATE SKIP LOCKED`` 保证，源码级核对是仓库既有做法（同一形状见
    ``test_gateway_postgres.py::WorkerVersionTests.
    test_no_claim_path_writes_the_version_column_at_all``、
    ``test_identity_provisioning_contract.py::
    ReentryTouchesNoLifecycleColumnTest``）。

    ``test_gateway_postgres.py::QueueClaimTests.
    test_two_workers_never_claim_the_same_task`` 是这条 SQL 唯一的真库半边，但它
    测的是**正确性**（不重复、不饿死）：PostgreSQL 在 READ COMMITTED 下对被锁行
    做 EPQ 重判，去掉 ``SKIP LOCKED`` 退化成普通 ``FOR UPDATE`` 后结果仍然正确，
    只是从「不阻塞」退化成「等锁」——那条真库用例因此不会变红（2026-08-19 对
    #239 的独立审查实测确认）。「不阻塞」正是这条 SQL 存在的理由，多个 worker
    并发领取时决定的是并发吞吐而不是正确性，且这个属性只能引入需要真实并发压力
    才能判定的不稳定用例才能从行为上验证——按 Issue #248 的要求改用结构性断言，
    直接钉住语句本身。
    """

    def test_claim_query_uses_skip_locked(self) -> None:
        import inspect

        from lingxi.adapters.postgres_conversation import PostgresTaskQueue

        # ``claim`` 自己的文档字符串就写了一遍 ``FOR UPDATE SKIP LOCKED`` 来解释
        # 这行 SQL 的作用——直接在整段源码（含文档字符串）上做 assertIn，即使把
        # 真正的 SQL 削回 ``FOR UPDATE``，断言仍然会因为文档字符串里的原样引用
        # 而误判通过。必须先去掉文档字符串，只扫代码本身（本仓库既有做法，见
        # ``tests/test_mcp_readiness_machine.py::_code_without_docstrings``）。
        source = _code_without_docstrings(inspect.getsource(PostgresTaskQueue.claim))
        # 正向锚点：确认切到的确实是「领取 queued 任务」这条 SQL，不是一条扫不到
        # 实现、永远为空的断言。
        self.assertIn(
            "status = 'queued'",
            source,
            "没有在 claim() 里找到领取 queued 任务的查询——本守卫的落点可能已经"
            "漂移，需要重新核对",
        )
        self.assertIn(
            "FOR UPDATE SKIP LOCKED",
            source,
            "worker 并发领取任务的语句退化成了普通 FOR UPDATE——不会产生错误数据，"
            "但会把并发领取变成串行排队，且没有任何自动检查会发现（Issue #248）",
        )


def _code_without_docstrings(source: str) -> str:
    """去掉全部文档字符串之后的正文；形状断言只该扫代码本身（同一形状见
    ``tests/test_mcp_readiness_machine.py::_code_without_docstrings``）。"""

    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(source))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            del body[0]
            if not body:
                body.append(ast.Pass())
    return ast.unparse(tree)


def _payload(
    chat_type: str | None = "p2p",
    message_type: str = "text",
    *,
    mentions: list[dict] | None = None,
    chat_id: str = "oc_1",
) -> dict:
    message = {
        "message_id": "om_1",
        "chat_id": chat_id,
        "message_type": message_type,
        "content": '{"text": "你好"}',
    }
    if chat_type is not None:
        message["chat_type"] = chat_type
    if mentions is not None:
        message["mentions"] = mentions
    return {
        "header": {"event_id": "evt_1", "event_type": "im.message.receive_v1"},
        "event": {"sender": {"sender_id": {"open_id": "ou_1"}}, "message": message},
    }


class ChatBoundaryTests(unittest.TestCase):
    """群聊越界：合同「问数与多轮对话」只适用于飞书私聊入口。"""

    def test_private_chat_is_accepted(self) -> None:
        parsed = parse_message_event(_payload("p2p"), trace_id="t")
        self.assertEqual(parsed.chat_id, "oc_1")

    def test_group_chat_is_rejected_before_an_inbound_message_exists(self) -> None:
        with self.assertRaises(NonPrivateChatError) as raised:
            parse_message_event(_payload("group"), trace_id="t")
        self.assertEqual(raised.exception.chat_type, "group")

    def test_missing_chat_type_is_rejected(self) -> None:
        """默认拒绝：放行的代价是把群聊内容当私聊处理，不可逆。"""

        with self.assertRaises(NonPrivateChatError):
            parse_message_event(_payload(None), trace_id="t")

    def test_group_chat_error_carries_mentions_chat_and_message_ids(self) -> None:
        """Issue #318：这三个字段只服务「要不要回一句固定引导」的判定。"""

        mentions = [{"id": {"open_id": "ou_bot"}}, {"id": {"open_id": "ou_other"}}]
        with self.assertRaises(NonPrivateChatError) as raised:
            parse_message_event(_payload("group", mentions=mentions), trace_id="t")
        error = raised.exception
        self.assertEqual(error.mentioned_open_ids, ("ou_bot", "ou_other"))
        self.assertEqual(error.chat_id, "oc_1")
        self.assertEqual(error.message_id, "om_1")

    def test_group_chat_error_defaults_to_no_mentions_when_absent(self) -> None:
        """没有 mentions 段（普通群消息、未 @ 任何人）取空元组，不是抛错。"""

        with self.assertRaises(NonPrivateChatError) as raised:
            parse_message_event(_payload("group"), trace_id="t")
        self.assertEqual(raised.exception.mentioned_open_ids, ())

    def test_handler_neither_reacts_nor_replies_to_a_group_message(self) -> None:
        from lingxi.apps.gateway import make_event_handler

        log = CallLog()
        audit = FakeAudit(log)

        class ExplodingPipeline:
            def handle_message(self, message: object) -> None:  # pragma: no cover
                raise AssertionError("群聊消息不得进入管线")

        make_event_handler(ExplodingPipeline(), audit=audit)(_payload("group"))

        self.assertEqual(log.count("audit.event.rejected_non_private_chat"), 1)
        self.assertEqual(
            log.fields("audit.event.rejected_non_private_chat")[0]["chat_type"], "group"
        )
        self.assertEqual(log.count("reaction.add"), 0, "群里不得加表情")
        self.assertEqual(log.count("reply.send_text"), 0, "群里不得回复")


class _ExplodingPipeline:
    """群聊@机器人固定引导测试专用：管线一旦被调用就说明越界判定失效。"""

    def handle_message(self, message: object) -> None:  # pragma: no cover
        raise AssertionError("群聊消息不得进入管线，不入队不建档")


class GroupMentionHintTests(unittest.TestCase):
    """群聊 @ 机器人固定引导（Issue #318，#328 v1.0 裁定 #5 排入实施）。

    可观察完成标准（对齐 Issue #318）：群聊 @ 机器人 → 恰一条固定引导文案 +
    审计；群聊普通消息/@别人仍完全静默（否定用例，含变异锚点）；未配置机器人
    open_id 时功能整体关闭；同一个群一小时内最多发一条。私聊路径的哨兵覆盖见
    ``ChatBoundaryTests``/其余既有测试类，本类不重复。
    """

    BOT_OPEN_ID = "ou_bot_open_id"

    def _handler(self, *, bot_open_id: str | None, log: CallLog, clock=None, replies=None):
        from lingxi.apps.gateway import (
            GroupMentionHintResponder,
            build_group_mention_hint_throttle,
            make_event_handler,
        )

        throttle = build_group_mention_hint_throttle(clock=clock or (lambda: 0.0))
        hint = GroupMentionHintResponder(
            bot_open_id=bot_open_id,
            replies=replies if replies is not None else FakeReplies(log),
            audit=FakeAudit(log),
            throttle=throttle,
        )
        return make_event_handler(
            _ExplodingPipeline(), audit=FakeAudit(log), group_mention_hint=hint
        )

    def test_mentioning_the_bot_itself_gets_exactly_one_fixed_hint_and_audit(self) -> None:
        log = CallLog()
        handler = self._handler(bot_open_id=self.BOT_OPEN_ID, log=log)

        handler(_payload("group", mentions=[{"id": {"open_id": self.BOT_OPEN_ID}}]))

        self.assertEqual(log.count("reply.send_text"), 1, "恰一条固定引导")
        sent = log.fields("reply.send_text")[0]
        self.assertEqual(sent["chat_id"], "oc_1")
        self.assertEqual(sent["reply_to_message_id"], "om_1")
        expected_text = default_content_catalog().text("gateway.group_mention_hint").text
        self.assertEqual(sent["text"], expected_text)
        self.assertEqual(log.count("audit.event.group_mention_hint_sent"), 1)
        self.assertEqual(log.count("audit.event.rejected_non_private_chat"), 1)
        self.assertEqual(log.count("reaction.add"), 0, "群里不得加表情")

    def test_mentioning_someone_else_stays_silent(self) -> None:
        """变异锚点：把 GroupMentionHintResponder 的精确匹配改成恒 True，本例变红。"""

        log = CallLog()
        handler = self._handler(bot_open_id=self.BOT_OPEN_ID, log=log)

        handler(_payload("group", mentions=[{"id": {"open_id": "ou_someone_else"}}]))

        self.assertEqual(log.count("reply.send_text"), 0)
        self.assertEqual(log.count("audit.event.group_mention_hint_sent"), 0)

    def test_a_plain_group_message_without_any_mention_stays_silent(self) -> None:
        log = CallLog()
        handler = self._handler(bot_open_id=self.BOT_OPEN_ID, log=log)

        handler(_payload("group"))

        self.assertEqual(log.count("reply.send_text"), 0)
        self.assertEqual(log.count("audit.event.group_mention_hint_sent"), 0)

    def test_without_a_configured_bot_open_id_the_feature_is_entirely_off(self) -> None:
        log = CallLog()
        handler = self._handler(bot_open_id=None, log=log)

        handler(_payload("group", mentions=[{"id": {"open_id": self.BOT_OPEN_ID}}]))

        self.assertEqual(log.count("reply.send_text"), 0)
        self.assertEqual(log.count("audit.event.group_mention_hint_sent"), 0)

    def test_without_the_group_mention_hint_wiring_the_feature_is_entirely_off(self) -> None:
        """``group_mention_hint=None``（既有 9 处用例的默认调用形状）逐字节不变。"""

        from lingxi.apps.gateway import make_event_handler

        log = CallLog()
        handler = make_event_handler(_ExplodingPipeline(), audit=FakeAudit(log))

        handler(_payload("group", mentions=[{"id": {"open_id": self.BOT_OPEN_ID}}]))

        self.assertEqual(log.count("reply.send_text"), 0)
        self.assertEqual(log.count("audit.event.rejected_non_private_chat"), 1)

    def test_a_private_chat_message_never_triggers_the_hint_judgement(self) -> None:
        """哨兵：私聊路径根本不经过 group_mention_hint，逐字节不变。"""

        from lingxi.apps.gateway import make_event_handler

        log = CallLog()

        class _ExplodingHint:
            def maybe_respond(self, error: object) -> None:  # pragma: no cover
                raise AssertionError("私聊消息不得触碰群聊 @ 引导判定")

        class _RecordingPipeline:
            def __init__(self) -> None:
                self.handled: list[object] = []

            def handle_message(self, message: object) -> None:
                self.handled.append(message)

        pipeline = _RecordingPipeline()
        handler = make_event_handler(
            pipeline, audit=FakeAudit(log), group_mention_hint=_ExplodingHint()
        )

        handler(_payload("p2p"))

        self.assertEqual(len(pipeline.handled), 1)
        self.assertEqual(log.count("reply.send_text"), 0)

    def test_a_second_mention_within_the_hour_is_throttled_to_silence(self) -> None:
        log = CallLog()
        clock = [0.0]
        handler = self._handler(
            bot_open_id=self.BOT_OPEN_ID, log=log, clock=lambda: clock[0]
        )
        mentions = [{"id": {"open_id": self.BOT_OPEN_ID}}]

        handler(_payload("group", mentions=mentions))
        clock[0] += 60.0  # 一分钟后同一个群再次 @
        handler(_payload("group", mentions=mentions))

        self.assertEqual(log.count("reply.send_text"), 1, "一小时内第二次 @ 应保持零输出")
        self.assertEqual(log.count("audit.event.group_mention_hint_sent"), 1)

        clock[0] += 3600.0  # 节流窗口之后，第三次 @ 应该重新放行
        handler(_payload("group", mentions=mentions))
        self.assertEqual(log.count("reply.send_text"), 2)

    def test_a_send_failure_still_acks_and_does_not_consume_the_throttle_slot(self) -> None:
        """P1-1（Issue #328 opus 审查真库实测）：`send_text` 抛出未预期异常时，
        此前节流位在发送**之前**已经记上、异常又会原样抛穿 `make_event_handler`
        ——飞书按事件处理失败重投，重投的这条又被同一节流窗口拦下，表现为
        「@ 了一次，什么都没发生，且一小时内都不会再试」。修复后：事件必须
        正常 ack（不抛出）、留一条 `event.group_mention_hint_failed` 审计（只含
        异常类名）、且这次失败不消耗节流额度——同一个群下一条 @ 仍能正常收到
        引导。"""

        log = CallLog()
        clock = [0.0]
        failing_replies = FakeReplies(log, fail_with=RuntimeError("模拟发送失败"))
        handler = self._handler(
            bot_open_id=self.BOT_OPEN_ID, log=log, clock=lambda: clock[0], replies=failing_replies
        )
        mentions = [{"id": {"open_id": self.BOT_OPEN_ID}}]

        result = handler(_payload("group", mentions=mentions))

        self.assertIsNone(result, "失败必须被吞掉，事件正常 ack、不向上抛异常")
        self.assertEqual(log.count("reply.send_text"), 1, "确实尝试过发送")
        self.assertEqual(log.count("audit.event.group_mention_hint_sent"), 0, "发送没有成功")
        self.assertEqual(log.count("audit.event.group_mention_hint_failed"), 1)
        self.assertEqual(
            log.fields("audit.event.group_mention_hint_failed")[0]["error"], "RuntimeError",
            "只留异常类名，不带正文",
        )

        # 恢复正常发送：下一条同一个群的 @ 仍然可以成功——失败的那次没有把
        # 节流额度提前消耗掉。
        failing_replies.fail_with = None
        handler(_payload("group", mentions=mentions))
        self.assertEqual(log.count("reply.send_text"), 2, "失败的发送不消耗节流额度，下次可再发")
        self.assertEqual(log.count("audit.event.group_mention_hint_sent"), 1)

    def test_different_chat_ids_do_not_throttle_each_other(self) -> None:
        """P3-6：节流字典按 `chat_id` 分键，不同群各自独立计时——同一时刻两个
        不同的群各自 @ 一次都应该正常收到引导，不应该被彼此的节流窗口误伤。"""

        log = CallLog()
        handler = self._handler(bot_open_id=self.BOT_OPEN_ID, log=log, clock=lambda: 0.0)
        mentions = [{"id": {"open_id": self.BOT_OPEN_ID}}]

        handler(_payload("group", mentions=mentions, chat_id="oc_a"))
        handler(_payload("group", mentions=mentions, chat_id="oc_b"))

        self.assertEqual(log.count("reply.send_text"), 2, "两个不同的群各自恰一条引导")
        sent = log.fields("reply.send_text")
        self.assertEqual({call["chat_id"] for call in sent}, {"oc_a", "oc_b"})


class UnsupportedMessageTypeTests(PipelineTestCase):
    """非文本消息：加表情、记审计、**不入队**、不发明回复文案。"""

    def test_an_image_message_is_acknowledged_but_not_queued(self) -> None:
        outcome = self.build().handle_message(
            message(text="", message_type="image"), now=NOW
        )

        self.assertEqual(outcome.handled_as, HandledAs.DROPPED)
        self.assertEqual(len(self.state.tasks), 0, "非文本消息不得入队")
        self.assertEqual(self.log.count("reaction.add"), 1, "合同：任何消息都加表情")
        self.assertEqual(self.log.count("reply.send_text"), 0, "本批不发明回复文案")
        recorded = self.log.fields("audit.message.unsupported_type")
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["message_type"], "image")

    def test_a_busy_topic_still_answers_with_the_busy_hint(self) -> None:
        """忙碌期的非文本消息与其他消息一样，只得到「当前任务仍在处理中」。"""

        self.state.conversations[("usr_1", "oc_1", "")] = FakeConversation(
            conversation_id="cnv_busy", running_task_id="tsk_running"
        )
        outcome = self.build().handle_message(message(message_type="audio"), now=NOW)

        self.assertEqual(outcome.handled_as, HandledAs.BUSY_HINT)
        self.assertEqual(self.log.count("reply.send_text"), 1)


class SessionWindowTests(unittest.TestCase):
    """`V-会话-02` / `V-会话-03`：两小时规则。"""

    def test_119_minutes_resumes_and_121_minutes_does_not(self) -> None:
        ended = NOW
        self.assertTrue(
            should_resume_session(
                last_task_ended_at=ended,
                agent_session_id="ses_1",
                now=ended + timedelta(minutes=119),
            )
        )
        self.assertFalse(
            should_resume_session(
                last_task_ended_at=ended,
                agent_session_id="ses_1",
                now=ended + timedelta(minutes=121),
            )
        )

    def test_a_three_hour_long_task_still_resumes(self) -> None:
        """易错点：判定只读结束时间，任务本身跑多久都不触发新会话。"""

        started = NOW
        ended = started + timedelta(hours=3)
        self.assertTrue(
            should_resume_session(
                last_task_ended_at=ended,
                agent_session_id="ses_1",
                now=ended + timedelta(minutes=1),
            ),
            "任务执行时长本身不得触发新会话（合同：任务执行本身耗时多久都不触发新会话）",
        )

    def test_cleared_session_does_not_resume(self) -> None:
        self.assertFalse(
            should_resume_session(
                last_task_ended_at=NOW, agent_session_id=None, now=NOW + timedelta(minutes=1)
            )
        )


class ResumeDecisionAtEnqueueTests(PipelineTestCase):
    """`V-会话-08`：续用判定发生在入队时并写入任务。"""

    def test_resume_flag_is_computed_from_request_time(self) -> None:
        self.state.conversations[("usr_1", "oc_1", "")] = FakeConversation(
            conversation_id="cnv_1",
            agent_session_id="ses_1",
            last_task_ended_at=NOW - timedelta(minutes=30),
        )
        outcome = self.build().handle_message(message(), now=NOW)

        self.assertTrue(outcome.resumed_session)
        self.assertTrue(self.state.tasks[0].resumed_session)

    def test_stale_conversation_starts_a_new_session(self) -> None:
        self.state.conversations[("usr_1", "oc_1", "")] = FakeConversation(
            conversation_id="cnv_1",
            agent_session_id="ses_1",
            last_task_ended_at=NOW - timedelta(hours=5),
        )
        outcome = self.build().handle_message(message(), now=NOW)

        self.assertFalse(outcome.resumed_session)
        self.assertFalse(self.state.tasks[0].resumed_session)


class SessionRotationNoticeTests(PipelineTestCase):
    """Issue #189：两小时规则**自然**开新会话时，用户必须被明确告知。

    合同「系统明确告诉用户已经开启新会话，不让用户误以为旧上下文仍然有效」有两条
    触发路径，`/new` 那条由 Issue #175 落地。这一组只约束另一条：用户什么都没敲，
    只是隔了两小时再提问。判定读的是 `_enqueue` 里那三个条件，`should_resume_session`
    本身不变——因此这组用例同时是那三个条件的变异检测网：
    去掉 ``not resumed`` → 会话延续用例红；去掉 ``agent_session_id`` 非空 →
    `/new` 之后用例红；去掉 ``last_task_ended_at`` 非空 → "有会话但没结束过任务"
    用例红；把判定整体写死成假 → 两小时用例红。四条已逐一实测。
    """

    def stale_conversation(self) -> FakeConversation:
        conversation = FakeConversation(
            conversation_id="cnv_1",
            agent_session_id="ses_1",
            last_task_ended_at=NOW - timedelta(hours=5),
        )
        self.state.conversations[("usr_1", "oc_1", "")] = conversation
        return conversation

    def texts(self) -> list[str]:
        return [fields["text"] for fields in self.log.fields("reply.send_text")]

    def test_two_hour_gap_sends_exactly_one_notice(self) -> None:
        self.stale_conversation()
        outcome = self.build().handle_message(message(), now=NOW)

        self.assertEqual(outcome.handled_as, HandledAs.TASK_QUEUED)
        self.assertFalse(outcome.resumed_session)
        self.assertEqual(
            self.texts(),
            [SESSION_ROTATED_TEXT],
            "两小时自然开新会话时恰好一条告知，且逐字等于产品负责人定稿",
        )
        self.assertEqual(len(self.state.tasks), 1, "告知不改变入队本身")

        sent = self.log.fields("audit.reply.sent")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["content_key"], "gateway.session_rotated")
        self.assertTrue(sent[0]["content_version"])

    def test_busy_topic_gets_only_the_busy_hint(self) -> None:
        """话题忙碌时，一条本来完全满足两小时告知条件的消息只得到「当前任务仍在
        处理中」：它根本没有入队，也就没有"本次提问"可言，再说一句"已开启新会话"
        会让用户以为这条被丢弃的消息已经在新会话里跑起来了。"""

        conversation = self.stale_conversation()
        conversation.running_task_id = "tsk_running"
        outcome = self.build().handle_message(message(), now=NOW)

        self.assertEqual(outcome.handled_as, HandledAs.BUSY_HINT)
        self.assertEqual(len(self.state.tasks), 0, "忙碌期的消息不得入队")
        self.assertEqual(
            self.texts(), [BUSY_HINT_TEXT], "忙碌分支恰好一条回复，且不得混入新会话告知"
        )

    def test_lost_claim_race_gets_only_the_busy_hint(self) -> None:
        """与 `/new` 竞态（``test_new_race_loses_the_clear_...``）对称的另一半：busy
        快照读到空闲，但真正 ``claim_conversation`` 时已经影响 0 行——另一条连接在
        两次读取之间抢占成功。这时任务同样没有入队，告知绝不能漏出去。

        这条用例也是「告知必须追加在抢占与入队**之后**」的守卫：把追加块上移到
        ``claim_conversation`` 之前，本用例即变红（已实测）。
        """

        self.stale_conversation()
        outcome = self.build(force_claim_conversation_result=False).handle_message(
            message(), now=NOW
        )

        self.assertEqual(outcome.handled_as, HandledAs.BUSY_HINT)
        self.assertEqual(len(self.state.tasks), 0, "抢占失败时不得入队")
        self.assertEqual(
            self.texts(), [BUSY_HINT_TEXT], "抢占失败分支恰好一条回复，且不得混入新会话告知"
        )
        self.assertNotIn(
            "gateway.session_rotated",
            [fields["content_key"] for fields in self.log.fields("audit.reply.sent")],
            "否定测试：抢占失败分支的审计记录里不得出现新会话告知的内容键",
        )

    def test_no_notice_when_the_task_could_not_be_queued(self) -> None:
        """`V-队列-03` 的姿态：入队没成功就不给任何「已经换新会话了」的告知。

        这条属性有**两道**互相独立的保险：追加发生在 ``insert_task`` 成功之后；
        入队失败路径整体丢弃 ``deferred``——即使两者同时被改坏，入队失败时发出的
        是 `EnqueueFailureTests` 断言的诚实排队失败提示（`gateway.queue_failed`，
        Issue #465），不是"已经换新会话了"这句话本身，因此这条用例仍然锁得住
        "新会话告知不会泄漏进失败路径"这条对用户成立的产品属性（此前第三道保险
        是"注入 store 没有独立发送权时事务失败直接上抛"，但那只是测试替身的
        缺口——生产 store 一直都实现着这个发送权；Issue #465 把测试替身补齐后
        这条分支不再依赖异常穿透，见 ``EnqueueFailureTests`` 的同一处更新）。
        """

        self.stale_conversation()
        pipeline = self.build(fail_on="insert_task")

        outcome = pipeline.handle_message(message(), now=NOW)

        self.assertIsNone(outcome.handled_as)
        self.assertEqual(
            self.texts(),
            [default_content_catalog().text("gateway.queue_failed").text],
            "入队失败时应收到诚实的排队失败提示，但不得发出新会话告知",
        )

    def test_continuing_session_gets_no_notice(self) -> None:
        """会话延续（间隔在两小时内）：一个字都不多说。"""

        self.state.conversations[("usr_1", "oc_1", "")] = FakeConversation(
            conversation_id="cnv_1",
            agent_session_id="ses_1",
            last_task_ended_at=NOW - timedelta(minutes=30),
        )
        outcome = self.build().handle_message(message(), now=NOW)

        self.assertTrue(outcome.resumed_session)
        self.assertEqual(self.texts(), [], "会话延续不得出现新会话告知")

    def test_first_ever_question_gets_no_notice(self) -> None:
        """首次提问：``resumed_session`` 同样是 False，但用户没有"此前上下文"，
        告知只会凭空制造一个不存在的旧会话。"""

        outcome = self.build().handle_message(message(), now=NOW)

        self.assertFalse(outcome.resumed_session, "首次提问本来就不续用")
        self.assertIsNone(
            self.state.conversations[("usr_1", "oc_1", "")].last_task_ended_at
        )
        self.assertEqual(self.texts(), [], "首次提问不得出现新会话告知")

    def test_session_without_a_finished_task_gets_no_notice(self) -> None:
        """有会话 id 却没有"上一次任务结束时间"：文案里那句「距上次对话已超过两小时」
        无从谈起，不提示。

        真库上这是一个**不可达**组合——``agent_session_id`` 只在
        ``postgres_conversation`` 收口任务的同一条 UPDATE 里写入，那条语句同时写
        ``last_task_ended_at = now()``。这条用例因此是纵深防御：它锁住判定必须同时
        读这两个字段，将来若有第三处写入打破该不变量，管线不会先一步开始说假话。
        """

        self.state.conversations[("usr_1", "oc_1", "")] = FakeConversation(
            conversation_id="cnv_1", agent_session_id="ses_1", last_task_ended_at=None
        )
        outcome = self.build().handle_message(message(), now=NOW)

        self.assertFalse(outcome.resumed_session)
        self.assertEqual(
            self.texts(), [], "没有上一次结束时间时不得声称「距上次对话」"
        )

    def test_question_right_after_new_command_gets_no_notice(self) -> None:
        """`/new` 之后的第一问：``agent_session_id`` 已被清空，而 `V-会话-05` 要求
        ``last_task_ended_at`` **保持不动**。用户刚收到过「已开启新会话，可以开始
        提问。」，此时再补一句「距上次对话已超过两小时」既重复又与事实不符。"""

        self.stale_conversation()
        pipeline = self.build()
        pipeline.handle_message(message("evt_new", text="/new"), now=NOW)
        self.assertEqual(self.texts(), [NEW_SESSION_TEXT])

        conversation = self.state.conversations[("usr_1", "oc_1", "")]
        self.assertIsNone(conversation.agent_session_id, "/new 已清空会话")
        self.assertIsNotNone(conversation.last_task_ended_at, "V-会话-05：结束时间不动")

        pipeline.handle_message(message("evt_after_new"), now=NOW)
        self.assertEqual(
            self.texts(),
            [NEW_SESSION_TEXT],
            "/new 之后的第一问不得再补一条两小时告知",
        )

    def test_duplicate_delivery_does_not_send_the_notice_twice(self) -> None:
        """`V-接入-09` 的幂等早退发生在 deferred 之前：平台重投同一条事件时，
        用户可见面不会出现第二条告知。"""

        self.stale_conversation()
        pipeline = self.build()
        pipeline.handle_message(message("evt_dup"), now=NOW)
        outcome = pipeline.handle_message(message("evt_dup"), now=NOW)

        self.assertTrue(outcome.duplicate)
        self.assertEqual(
            self.texts(), [SESSION_ROTATED_TEXT], "重复投递不得发出第二条告知"
        )
        self.assertEqual(len(self.state.tasks), 1, "重复投递不得产生第二个任务")

    def test_session_rotated_text_matches_the_pm_final_copy(self) -> None:
        """内容目录里的 ``gateway.session_rotated`` 必须逐字等于产品负责人
        2026-08-17 在 Issue #189 定稿的文案（评论 5310887492）。"""

        self.assertEqual(
            default_content_catalog().text("gateway.session_rotated").text,
            SESSION_ROTATED_TEXT,
        )


class StaleSessionDiscardTests(PipelineTestCase):
    """2026-08-23 真实故障回归：入队判定「不续用」的旧 ``agent_session_id`` 必须随
    入队事务立即置空（判废即清），不能指望下一次入队的时间戳比较继续挡住它。

    故障链：两小时轮换后的首个任务崩溃——失败任务不写回新 session id，却刷新
    ``last_task_ended_at``——下一条消息落回两小时窗口内，resume 一个早已判废、
    JSONL 已被物理清理的旧会话，连发几条都瞬间失败，用户只能手动 `/new` 自救；
    就算旧文件还在，续上的也是「已明确告知不携带」的过期上下文。
    """

    def stale_conversation(self) -> FakeConversation:
        conversation = FakeConversation(
            conversation_id="cnv_1",
            agent_session_id="ses_stale",
            last_task_ended_at=NOW - timedelta(hours=5),
        )
        self.state.conversations[("usr_1", "oc_1", "")] = conversation
        return conversation

    def discard_calls(self) -> list[dict]:
        return self.log.fields("store.discard_stale_agent_session")

    def test_rotation_discards_the_stale_session_id_in_the_same_transaction(self) -> None:
        conversation = self.stale_conversation()
        outcome = self.build().handle_message(message(), now=NOW)

        self.assertEqual(outcome.handled_as, HandledAs.TASK_QUEUED)
        self.assertEqual(len(self.discard_calls()), 1)
        self.assertIsNone(
            conversation.agent_session_id, "轮换判废的旧会话 id 必须随入队事务置空"
        )
        self.assertEqual(len(self.state.tasks), 1, "判废不改变入队本身")

    def test_a_failed_rotated_task_cannot_leak_the_stale_session_to_the_next_message(self) -> None:
        """事故本体的最小重放：轮换后的首个任务失败（只刷新结束时间、不写回新
        session id），一分钟后的下一条消息必须开全新会话，而不是 resume 旧的。"""

        conversation = self.stale_conversation()
        pipeline = self.build()
        pipeline.handle_message(message("evt_1"), now=NOW)

        # 模拟任务失败收口：释放话题、刷新结束时间；失败不写回 agent_session_id。
        conversation.running_task_id = None
        conversation.last_task_ended_at = NOW + timedelta(seconds=30)

        outcome = pipeline.handle_message(message("evt_2"), now=NOW + timedelta(minutes=1))

        self.assertEqual(outcome.handled_as, HandledAs.TASK_QUEUED)
        self.assertFalse(
            outcome.resumed_session,
            "修复前这里是 True：失败任务刷新了结束时间，旧 id 又没清，"
            "下一条消息会去 resume 一个已判废的会话",
        )
        self.assertFalse(self.state.tasks[1].resumed_session)

    def test_a_continuing_session_is_never_discarded(self) -> None:
        conversation = FakeConversation(
            conversation_id="cnv_1",
            agent_session_id="ses_live",
            last_task_ended_at=NOW - timedelta(minutes=30),
        )
        self.state.conversations[("usr_1", "oc_1", "")] = conversation
        outcome = self.build().handle_message(message(), now=NOW)

        self.assertTrue(outcome.resumed_session)
        self.assertEqual(self.discard_calls(), [], "续用中的会话绝不能被判废")
        self.assertEqual(conversation.agent_session_id, "ses_live")

    def test_nothing_to_discard_means_no_discard_call(self) -> None:
        """首次提问 / `/new` 之后：``agent_session_id`` 本来就是空，不发多余写入。"""

        outcome = self.build().handle_message(message(), now=NOW)

        self.assertEqual(outcome.handled_as, HandledAs.TASK_QUEUED)
        self.assertEqual(self.discard_calls(), [])

    def test_a_stale_session_without_a_finished_task_is_still_discarded(self) -> None:
        """「有会话 id 但没有结束时间」的不可达组合（纵深防御，与
        ``SessionRotationNoticeTests`` 的同名情形对应）：不提示，但同样判废——
        它无论如何都不会再被 resume，留着只会等下一次事故。"""

        self.state.conversations[("usr_1", "oc_1", "")] = FakeConversation(
            conversation_id="cnv_1", agent_session_id="ses_orphan", last_task_ended_at=None
        )
        self.build().handle_message(message(), now=NOW)

        self.assertEqual(len(self.discard_calls()), 1)

    def test_a_busy_topic_does_not_discard_anything(self) -> None:
        """忙碌分支根本不进入入队判定，判废不得发生——正在运行的任务结束时还要
        把自己的新 session id 写回这一行。"""

        conversation = self.stale_conversation()
        conversation.running_task_id = "tsk_running"
        self.build().handle_message(message(), now=NOW)

        self.assertEqual(self.discard_calls(), [])


class BusyCommandTests(PipelineTestCase):
    """`V-会话-09`（忙碌期 `/new`）与 `V-会话-10`（`/stop` 不被忙碌拦截）。"""

    def setUp(self) -> None:
        super().setUp()
        self.conversation = FakeConversation(
            conversation_id="cnv_busy",
            agent_session_id="ses_1",
            running_task_id="tsk_running",
        )
        self.state.conversations[("usr_1", "oc_1", "")] = self.conversation

    def test_new_during_busy_only_gets_the_hint_and_clears_nothing(self) -> None:
        outcome = self.build().handle_message(message(text="/new"), now=NOW)

        self.assertEqual(outcome.handled_as, HandledAs.BUSY_HINT)
        self.assertEqual(
            self.conversation.agent_session_id,
            "ses_1",
            "忙碌期的 /new 不得清空上下文（合同把 /new 列入忙碌期受限命令）",
        )
        self.assertEqual(len(self.state.tasks), 0, "忙碌期的 /new 不得入队")
        replies = self.log.fields("reply.send_text")
        self.assertEqual(
            len(replies),
            1,
            "忙碌期的 /new 沿用现有忙碌提示，不得额外追加「已开启新会话」文案",
        )
        self.assertEqual(replies[0]["text"], BUSY_HINT_TEXT)
        self.assertNotEqual(
            replies[0]["text"],
            NEW_SESSION_TEXT,
            "否定测试：忙碌分支不得出现 /new 成功文案",
        )

    def test_stop_during_busy_is_processed_not_deflected(self) -> None:
        outcome = self.build().handle_message(message(text="/stop"), now=NOW)

        self.assertEqual(
            outcome.handled_as,
            HandledAs.COMMAND,
            "接口设计 3.2 第 6 步的条件是「忙碌且非 /stop」，/stop 必须照常被处理",
        )
        self.assertEqual(
            self.log.count("reply.send_text"), 0, "/stop 不得收到「当前任务仍在处理中」"
        )
        self.assertIn("store.request_stop", self.log.names())

    def test_new_when_idle_clears_the_session(self) -> None:
        self.conversation.running_task_id = None
        outcome = self.build().handle_message(message(text="/new"), now=NOW)

        self.assertEqual(outcome.handled_as, HandledAs.COMMAND)
        self.assertIsNone(self.conversation.agent_session_id)

    def test_new_when_idle_sends_exactly_one_success_confirmation(self) -> None:
        """Issue #175（2026-08-16 定稿）：空闲 /new 除表情外，恰好一条文字确认，
        内容与产品负责人定稿逐字一致。"""

        self.conversation.running_task_id = None
        self.build().handle_message(message(text="/new"), now=NOW)

        self.assertEqual(self.log.count("reaction.add"), 1, "表情仍作为「已收到」信号保留")
        self.assertEqual(len(self.state.tasks), 0, "/new 依旧不创建问数任务")
        replies = self.log.fields("reply.send_text")
        self.assertEqual(len(replies), 1, "空闲 /new 恰好一条文字回复")
        self.assertEqual(replies[0]["text"], NEW_SESSION_TEXT)

        sent = self.log.fields("audit.reply.sent")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["content_key"], "gateway.new_session")
        self.assertTrue(sent[0]["content_version"])

    def test_new_race_loses_the_clear_gets_only_the_busy_hint_not_the_success_text(
        self,
    ) -> None:
        """P2-1（独立审核）：busy 快照（事务开头 `ensure_conversation` 读到的
        `running_task_id`）是空闲的，但真正执行 `clear_agent_session` 时已经
        影响 0 行——对称于源码注释「清空本身再判一次忙碌」描述的竞态：另一条连接
        在两次读取之间抢占成功。这时必须整体落到忙碌分支，`gateway.new_session`
        绝不能和忙碌提示一起出现，否则会把一次没有真正清空上下文的 `/new` 误报
        成功。用 `force_clear_agent_session_result=False` 直接注入这一步的返回
        值，不依赖真实线程调度就能稳定复现（真库并发版本见
        `test_gateway_postgres.NewCommandRaceTests`）。"""

        self.conversation.running_task_id = None
        outcome = self.build(force_clear_agent_session_result=False).handle_message(
            message(text="/new"), now=NOW
        )

        self.assertEqual(outcome.handled_as, HandledAs.BUSY_HINT)
        self.assertEqual(
            self.conversation.agent_session_id,
            "ses_1",
            "竞态中真正没有清空的上下文不得被当作已经清空",
        )
        replies = self.log.fields("reply.send_text")
        self.assertEqual(len(replies), 1, "竞态分支恰好一条回复")
        self.assertEqual(replies[0]["text"], BUSY_HINT_TEXT)
        self.assertNotEqual(
            replies[0]["text"],
            NEW_SESSION_TEXT,
            "否定测试：竞态分支不得出现 /new 成功文案",
        )
        self.assertNotIn(
            "gateway.new_session",
            [fields["content_key"] for fields in self.log.fields("audit.reply.sent")],
            "否定测试：竞态分支的审计记录里不得出现成功文案的内容键",
        )

    def test_new_session_text_matches_the_pm_final_copy(self) -> None:
        """内容目录里的 ``gateway.new_session`` 必须逐字等于 2026-08-16 定稿，
        与上面注入测试用的字面量不得漂移。"""

        self.assertEqual(
            default_content_catalog().text("gateway.new_session").text, NEW_SESSION_TEXT
        )


class SlashCommandRejectionTests(PipelineTestCase):
    """`V-会话-11`（Trace #304 批次 5 直修）：产品负责人在 biai-stage 真实测试发现，
    以 `/` 开头的业务消息会被执行层（Agent SDK 底层的 Claude Code CLI）解析成系统
    斜杠命令而不是用户文本——`/config`/`/model`/`/help` 令会话瞬断（session_failed），
    `/loop` 让模型尝试调用内部工具（触发 model_protocol_breakdown）。这里是 gateway
    层的主防线：入队前直接拦截，不建任务、不耗模型轮次。
    """

    def test_unrecognized_slash_messages_are_rejected_without_queueing(self) -> None:
        """真实故障复现的四个 prompt：/config、/model、/help、/loop 变体。"""

        cases = ("/config", "/model", "/help", '/loop 10m "检查赞比亚充值数据"')
        for text in cases:
            with self.subTest(text=text):
                self.log = CallLog()
                self.state = FakeState()
                self.state.users["ou_1"] = provisioned_user()

                outcome = self.build().handle_message(message(text=text), now=NOW)

                self.assertEqual(outcome.handled_as, HandledAs.COMMAND)
                self.assertEqual(len(self.state.tasks), 0, f"{text!r} 不得入队")
                replies = self.log.fields("reply.send_text")
                self.assertEqual(len(replies), 1, f"{text!r} 应恰好一条回复")
                self.assertEqual(replies[0]["text"], SLASH_REJECTED_TEXT)

                sent = self.log.fields("audit.reply.sent")
                self.assertEqual(sent[0]["content_key"], "gateway.slash_rejected")
                self.assertTrue(sent[0]["content_version"])
                self.assertIn(
                    "audit.command.unsupported_slash",
                    self.log.names(),
                    "必须落一条可分类的审计动作",
                )

    def test_audit_records_classification_not_raw_message_text(self) -> None:
        """审计只记 handled_as 分类，不记正文——`/loop` 的参数（可能带业务敏感
        描述）不得出现在审计字段里。"""

        outcome = self.build().handle_message(
            message(text='/loop 10m "检查赞比亚充值数据"'), now=NOW
        )

        self.assertEqual(outcome.handled_as, HandledAs.COMMAND)
        recorded = self.log.fields("audit.command.unsupported_slash")
        self.assertEqual(len(recorded), 1)
        self.assertNotIn("text", recorded[0])
        for value in recorded[0].values():
            self.assertNotIn("赞比亚", str(value))
            self.assertNotIn("检查", str(value))

    def test_mid_sentence_slash_is_not_intercepted(self) -> None:
        """否定断言：句中含 / 的正常文本（日期、URL）必须正常入队，不受影响。"""

        cases = ("8/26 的销售额是多少", "帮我看看 https://example.com/report 这个链接")
        for text in cases:
            with self.subTest(text=text):
                self.log = CallLog()
                self.state = FakeState()
                self.state.users["ou_1"] = provisioned_user()

                outcome = self.build().handle_message(message(text=text), now=NOW)

                self.assertEqual(
                    outcome.handled_as, HandledAs.TASK_QUEUED, f"{text!r} 不应被拦截"
                )
                self.assertEqual(len(self.state.tasks), 1)

    def test_known_commands_still_work(self) -> None:
        """回归：`/new`（大小写不敏感的整条匹配）不受新拦截影响，行为不变。"""

        outcome = self.build().handle_message(message(text="/NEW"), now=NOW)

        self.assertEqual(outcome.handled_as, HandledAs.COMMAND)
        replies = self.log.fields("reply.send_text")
        self.assertEqual(replies[0]["text"], NEW_SESSION_TEXT)
        self.assertNotEqual(replies[0]["text"], SLASH_REJECTED_TEXT)

    def test_busy_topic_gets_the_same_rejection_not_the_busy_hint(self) -> None:
        """设计取舍：拦截判定放在忙碌判定之前——这条消息不管话题忙不忙碌都不会被
        受理，busy 状态下也应得到同一条拒绝文案，而不是「当前任务仍在处理中」
        （否则用户要等任务结束后重发一遍才会看到真正有用的提示）。"""

        self.state.conversations[("usr_1", "oc_1", "")] = FakeConversation(
            conversation_id="cnv_busy", running_task_id="tsk_running"
        )

        outcome = self.build().handle_message(message(text="/model"), now=NOW)

        self.assertEqual(outcome.handled_as, HandledAs.COMMAND)
        replies = self.log.fields("reply.send_text")
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["text"], SLASH_REJECTED_TEXT)
        self.assertNotEqual(
            replies[0]["text"], BUSY_HINT_TEXT, "否定测试：忙碌分支不得掩盖斜杠拒绝文案"
        )

    def test_roster_gate_takes_priority_for_unlisted_users(self) -> None:
        """名单外用户仍走内测名单闸优先：`NOT_PROVISIONED` 分支的既有判定顺序
        不受本行影响，斜杠拦截只发生在已确认是业务用户之后。"""

        onboarding = FakeOnboarding()
        pipeline = self.build(onboarding=onboarding, innertest_roster_gate=lambda open_id: False)

        outcome = pipeline.handle_message(
            message(open_id="ou_not_listed", text="/config"), now=NOW
        )

        self.assertEqual(outcome.handled_as, HandledAs.DROPPED)
        replies = self.log.fields("reply.send_text")
        self.assertEqual(len(replies), 1)
        self.assertEqual(
            replies[0]["text"],
            default_content_catalog().text("onboarding.innertest_not_open").text,
        )
        self.assertNotEqual(replies[0]["text"], SLASH_REJECTED_TEXT)

    def test_slash_rejected_text_matches_content_catalog(self) -> None:
        """内容目录里的 ``gateway.slash_rejected`` 必须逐字等于本文件测试用的
        字面量，两头不得漂移。"""

        self.assertEqual(
            default_content_catalog().text("gateway.slash_rejected").text, SLASH_REJECTED_TEXT
        )


class EnqueueFailureTests(PipelineTestCase):
    """`V-队列-03`：入队未成功时不给已受理回复，事件不被标记为已成功处理。

    Issue #465 更新（100% 响应覆盖）：`V-队列-03` 原文只禁止"表示已受理或已开始
    处理"的回复，从未要求零回复——本组此前用 ``assertRaises(RuntimeError)`` +
    零回复来断言，是因为共用的 ``FakeStore`` 当时没有实现
    ``claim_queue_failure_notice``（真实生产 ``PostgresGatewayStore`` 一直都
    实现着它），暴露的是测试替身的缺口，不是生产行为。补上假实现后，这里改为
    断言生产环境真正会发生的事：不抛异常、用户收到一条诚实的
    ``gateway.queue_failed`` 提示（"消息已收到，但当前暂时无法开始处理"——不是
    "已受理/已开始处理"），`outcome.handled_as` 仍为 ``None``。
    """

    def test_task_insert_failure_sends_an_honest_queue_failed_reply(self) -> None:
        pipeline = self.build(fail_on="insert_task")

        outcome = pipeline.handle_message(message("evt_fail"), now=NOW)

        self.assertIsNone(outcome.handled_as)
        self.assertEqual(self.log.count("reply.send_text"), 1, "必须收到一条诚实的失败提示")
        sent = self.log.fields("reply.send_text")[0]
        self.assertEqual(sent["text"], default_content_catalog().text("gateway.queue_failed").text)
        self.assertEqual(len(self.state.tasks), 0)
        self.assertNotIn(
            "evt_fail",
            self.state.events,
            "事务未提交，事件行不得存在——否则飞书重投时会被当成重复投递而静默丢弃",
        )
        self.assertEqual(self.state.notifies, 0, "入队失败不得发出 NOTIFY")

    def test_task_insert_failure_notice_is_sent_only_once_per_event(self) -> None:
        """同一条事件被飞书重投多次时，诚实失败提示只发一次——与真库
        ``queue_failure_notice`` 表的去重语义一致（否则每次重投都再发一条，
        持续失败的事件会把用户刷屏）。"""

        pipeline = self.build(fail_on="insert_task")

        pipeline.handle_message(message("evt_fail"), now=NOW)
        pipeline.handle_message(message("evt_fail"), now=NOW)

        self.assertEqual(self.log.count("reply.send_text"), 1)

    def test_claim_failure_rolls_back_the_claim(self) -> None:
        """`V-队列-02` 的可注入面：抢占成功但入队失败，话题不得永久忙碌。"""

        self.state.conversations[("usr_1", "oc_1", "")] = FakeConversation(
            conversation_id="cnv_1"
        )
        pipeline = self.build(fail_on="insert_task")

        outcome = pipeline.handle_message(message("evt_fail"), now=NOW)

        self.assertIsNone(outcome.handled_as)
        self.assertIsNone(
            self.state.conversations[("usr_1", "oc_1", "")].running_task_id,
            "抢占必须随事务一起回滚，否则该话题永久停在「当前任务仍在处理中」",
        )


class UnprovisionedUserTests(PipelineTestCase):
    """`V-审计-05` 的 gateway 侧否定面：未开通用户的内容不落库、不回显。"""

    def test_content_is_neither_stored_nor_echoed(self) -> None:
        secret = "我的工号是 12345，帮我查工资"
        outcome = self.build().handle_message(
            message(open_id="ou_stranger", text=secret), now=NOW
        )

        self.assertEqual(outcome.handled_as, HandledAs.NOT_PROVISIONED)
        self.assertEqual(len(self.state.tasks), 0, "未开通用户的消息不得产生任务")
        self.assertEqual(self.log.count("reply.send_text"), 0, "本批不回显、不回复")
        for _, fields in self.log.entries:
            self.assertNotIn(
                secret,
                repr(fields),
                "未开通用户发来的业务内容不得出现在任何审计或出站调用里",
            )


class AutomaticOnboardingTests(PipelineTestCase):
    """#65：首次未开通消息只认领一次，并在提交后启动开通编排。"""

    def test_first_message_claims_and_starts_identity_chain_without_text(self) -> None:
        secret = "我的工号是 12345，帮我查工资"
        runner = FakeOnboarding(
            result=OnboardingResult(
                state=OnboardingState.COMPLETED,
                messages=(
                    OnboardingMessage("onboarding.matched"),
                    OnboardingMessage(
                        "onboarding.completed",
                        values=(("company_name", "公司 A"), ("function_name", "销售")),
                    ),
                ),
            )
        )
        pipeline = self.build(onboarding=runner)

        outcome = pipeline.handle_message(
            message(event_id="evt_onboard", open_id="ou_stranger", text=secret), now=NOW
        )

        self.assertEqual(outcome.handled_as, HandledAs.AUTO_PROVISIONING)
        self.assertEqual(self.state.events["evt_onboard"], "auto_provisioning")
        self.assertEqual(
            runner.calls,
            [{"event_id": "evt_onboard", "open_id": "ou_stranger", "trace_id": "trc_evt_onboard"}],
        )
        self.assertNotIn("text", runner.calls[0])
        self.assertEqual(
            [fields["content_key"] for fields in self.log.fields("audit.reply.sent")],
            ["onboarding.checking", "onboarding.matched", "onboarding.completed"],
        )
        self.assertEqual(len(self.state.tasks), 0)
        for _, fields in self.log.entries:
            self.assertNotIn(secret, repr(fields))

    def test_duplicate_delivery_does_not_restart_onboarding_or_reply(self) -> None:
        runner = FakeOnboarding(
            result=OnboardingResult(state=OnboardingState.NOT_AUTHORIZED)
        )
        pipeline = self.build(onboarding=runner)

        pipeline.handle_message(message(event_id="evt_onboard_dup", open_id="ou_new"), now=NOW)
        replies_after_first = self.log.count("reply.send_text")
        outcome = pipeline.handle_message(
            message(event_id="evt_onboard_dup", open_id="ou_new"), now=NOW
        )

        self.assertTrue(outcome.duplicate)
        self.assertEqual(len(runner.calls), 1, "重复事件不得重复触发开通编排")
        self.assertEqual(self.log.count("reply.send_text"), replies_after_first)
        self.assertEqual(
            [fields["content_key"] for fields in self.log.fields("audit.reply.sent")],
            ["onboarding.checking", "onboarding.not_authorized"],
        )

    def test_unmatched_result_is_terminal_fixed_prompt(self) -> None:
        runner = FakeOnboarding(
            result=OnboardingResult(
                state=OnboardingState.NOT_AUTHORIZED,
                failure_reason="no_supported_function",
            )
        )

        outcome = self.build(onboarding=runner).handle_message(
            message(event_id="evt_unmatched", open_id="ou_new"), now=NOW
        )

        self.assertEqual(outcome.handled_as, HandledAs.AUTO_PROVISIONING)
        self.assertEqual(
            [fields["content_key"] for fields in self.log.fields("audit.reply.sent")],
            ["onboarding.checking", "onboarding.not_authorized"],
        )
        self.assertEqual(len(self.state.tasks), 0)
        self.assertEqual(self.log.count("store.ensure_conversation"), 0)

    def test_runner_failure_uses_internal_terminal_prompt(self) -> None:
        runner = FakeOnboarding(fail_with=RuntimeError("外部权限服务不可用"))

        outcome = self.build(onboarding=runner).handle_message(
            message(event_id="evt_internal", open_id="ou_new"), now=NOW
        )

        self.assertEqual(outcome.handled_as, HandledAs.AUTO_PROVISIONING)
        self.assertEqual(
            [fields["content_key"] for fields in self.log.fields("audit.reply.sent")],
            ["onboarding.checking", "onboarding.internal_error"],
        )
        self.assertEqual(self.log.count("audit.onboarding.failed"), 1)
        self.assertNotIn("外部权限服务不可用", repr(self.log.entries))
        # Issue #280 §7.1 渲染点 3（异常兜底）：正文必须带上这一次事件的追溯号，
        # 且不得泄露内部异常正文（上一条断言已覆盖后半）。
        self.assertIn("trc_evt_internal", self.log.fields("reply.send_text")[-1]["text"])


class InnertestRosterGateTests(PipelineTestCase):
    """内测名单闸的 gateway 侧前移一份（Issue #302 S-N-01 的纵深，opus 批量审查
    P1 修复）：`DAILY_REPORT_UUID_PREFIX` 那条修复解决的是"通报发不出去"，
    这一组解决的是"名单外用户两条消息 + 文案泄露 + 错误码兜底"——此前名单判定
    只发生在 scheduler 侧异步的开通链深处，gateway 总是无条件先发一条
    `onboarding.checking`，名单外用户因此总会收到两条消息，且第二条终态取决于
    开通链跑到哪一步失败，不是稳定的「内测未开放」。
    """

    def test_roster_excluded_sender_gets_one_clean_reply_and_never_reaches_onboarding(
        self,
    ) -> None:
        runner = FakeOnboarding()
        pipeline = self.build(
            onboarding=runner, innertest_roster_gate=lambda open_id: False
        )

        outcome = pipeline.handle_message(
            message(event_id="evt_roster_out", open_id="ou_stranger"), now=NOW
        )

        self.assertEqual(outcome.handled_as, HandledAs.DROPPED)
        self.assertEqual(self.state.events["evt_roster_out"], "dropped")
        # 开通编排完全没有被调用——不是「调用了但被名单闸截胡」，是根本没调用，
        # 因此也不会有 checking 之外的第二条异步终态消息。
        self.assertEqual(runner.calls, [])
        self.assertNotIn("audit.inbound_event.auto_provisioning", self.log.names())
        # 恰好一条回复，且就是内测未开放的固定文案键——不是两条，不是
        # `onboarding.checking` 打头。
        sent = self.log.fields("audit.reply.sent")
        self.assertEqual([fields["content_key"] for fields in sent], ["onboarding.innertest_not_open"])
        # 拒绝审计只带 event_id/trace_id，不带 open_id（含脱敏形式）——与
        # scheduler 侧 `onboarding.innertest_roster_rejected` 同一条纪律
        # （`V-花名册-34`）。
        rejections = self.log.fields("audit.onboarding.innertest_roster_rejected")
        self.assertEqual(len(rejections), 1)
        self.assertEqual(
            set(rejections[0]), {"event_id", "trace_id"},
            "拒绝审计只能带 event_id/trace_id，不能带 open_id",
        )

    def test_roster_included_sender_is_unaffected(self) -> None:
        runner = FakeOnboarding(result=OnboardingResult(state=OnboardingState.STARTED))
        pipeline = self.build(
            onboarding=runner, innertest_roster_gate=lambda open_id: True
        )

        outcome = pipeline.handle_message(
            message(event_id="evt_roster_in", open_id="ou_allowed"), now=NOW
        )

        self.assertEqual(outcome.handled_as, HandledAs.AUTO_PROVISIONING)
        self.assertEqual(len(runner.calls), 1)
        self.assertNotIn("audit.onboarding.innertest_roster_rejected", self.log.names())
        sent = self.log.fields("audit.reply.sent")
        self.assertEqual([fields["content_key"] for fields in sent], ["onboarding.checking"])

    def test_gate_not_configured_matches_pre_existing_behavior(self) -> None:
        """`innertest_roster_gate=None`（未装配，默认值）：行为与本项加入之前
        逐字节一致——不做任何名单判定，直接进入既有 AUTO_PROVISIONING 分支。
        与 `AdminRoutingPipelineTests` 里同名断言（未装配 admin_router）同一姿态。
        """

        runner = FakeOnboarding(result=OnboardingResult(state=OnboardingState.STARTED))
        pipeline = self.build(onboarding=runner)

        outcome = pipeline.handle_message(message(open_id="ou_whoever"), now=NOW)

        self.assertEqual(outcome.handled_as, HandledAs.AUTO_PROVISIONING)
        self.assertEqual(len(runner.calls), 1)


class OnboardingDispatchLedgerTests(PipelineTestCase):
    """#65 轻审 P2-2 的在途一半：编排被调用之后必须记账。

    账本（迁移 0062 的 ``onboarding_dispatched_at``）唯一的作用是让"认领了却没交接"
    这种行可判定。因此正向路径必须记上，否则对账扫描会把一条已经拿到结论的事件再
    交接一次；而记账本身失败时又不能反过来带走用户已经该收到的提示。
    """

    def test_ledger_is_written_after_the_runner_returned(self) -> None:
        runner = FakeOnboarding(result=OnboardingResult(state=OnboardingState.NOT_AUTHORIZED))

        self.build(onboarding=runner).handle_message(
            message(event_id="evt_ledger", open_id="ou_new"), now=NOW
        )

        self.assertEqual(set(self.state.onboarding_dispatched), {"evt_ledger"})
        self.assertGreater(
            self.log.index("store.mark_onboarding_dispatched"),
            self.log.index("audit.onboarding.result"),
            "记账必须发生在编排返回之后——提前记账等于账本宣称一件还没发生的事",
        )

    def test_ledger_is_written_even_when_the_runner_raised(self) -> None:
        runner = FakeOnboarding(fail_with=RuntimeError("外部权限服务不可用"))

        self.build(onboarding=runner).handle_message(
            message(event_id="evt_ledger_fail", open_id="ou_new"), now=NOW
        )

        self.assertEqual(
            set(self.state.onboarding_dispatched),
            {"evt_ledger_fail"},
            "编排确实被调用过：不记账会让用户收到第二遍 LX-ONBOARD-001",
        )

    def test_a_failed_ledger_write_does_not_take_away_the_user_reply(self) -> None:
        runner = FakeOnboarding(result=OnboardingResult(state=OnboardingState.NOT_AUTHORIZED))

        outcome = self.build(
            onboarding=runner, fail_on="mark_onboarding_dispatched"
        ).handle_message(message(event_id="evt_ledger_broken", open_id="ou_new"), now=NOW)

        self.assertEqual(outcome.handled_as, HandledAs.AUTO_PROVISIONING)
        self.assertEqual(
            [fields["content_key"] for fields in self.log.fields("audit.reply.sent")],
            ["onboarding.checking", "onboarding.not_authorized"],
            "簿记失败不得带走用户可见的终态提示",
        )
        self.assertEqual(self.log.count("audit.onboarding.dispatch_record_failed"), 1)
        self.assertEqual(set(self.state.onboarding_dispatched), set())

    def test_an_asynchronous_runner_keeps_the_ledger_for_itself(self) -> None:
        """``started`` 表示"编排异步接手了"，结论还没产生（Epic D / S-D-02）。

        gateway 此刻就记账，会让一次跑到一半的崩溃变成谁都不会再看的悬空状态：对账扫描
        被账本挡在门外，用户永远停在「正在核对」。正式 runner 在链跑到终态、并把结论发给
        用户之后才记这一笔，因此崩在中途的那一条仍然是孤儿、仍然会被扫描重新交接一次。
        """

        runner = FakeOnboarding(result=OnboardingResult(state=OnboardingState.STARTED))

        outcome = self.build(onboarding=runner).handle_message(
            message(event_id="evt_async", open_id="ou_new"), now=NOW
        )

        self.assertEqual(outcome.handled_as, HandledAs.AUTO_PROVISIONING)
        self.assertEqual(
            [call["event_id"] for call in runner.calls], ["evt_async"], "编排必须被调用过"
        )
        self.assertEqual(
            set(self.state.onboarding_dispatched),
            set(),
            "started 的账由编排自己记；gateway 提前记会把中途崩溃的链变成无人恢复的悬空",
        )
        self.assertEqual(
            [fields["content_key"] for fields in self.log.fields("audit.reply.sent")],
            ["onboarding.checking"],
            "started 是唯一允许没有下文的状态：用户刚收到的「正在核对」就是完整交代",
        )

    def test_a_synchronous_terminal_still_gets_its_ledger_entry(self) -> None:
        """同步返回终态的编排（失败关闭桩、旧实现）不受上面那条影响。"""

        runner = FakeOnboarding(result=OnboardingResult(state=OnboardingState.INTERNAL_ERROR))

        self.build(onboarding=runner).handle_message(
            message(event_id="evt_sync_terminal", open_id="ou_new"), now=NOW
        )

        self.assertEqual(set(self.state.onboarding_dispatched), {"evt_sync_terminal"})


class OnboardingShutdownOrderTests(PipelineTestCase):
    """#65 轻审 P2-3：停机窗口内**不触发**开通编排。

    此前的顺序是"先调 runner，再判停机丢弃回复"——正式 runner 带外部副作用（建档、
    建环境、发权限、MCP 同步），那等于在停机中途发起一串不可回滚的外部动作，然后把
    用户唯一能看到的结论扔掉。
    """

    def test_runner_is_not_called_while_stopping(self) -> None:
        runner = FakeOnboarding(result=OnboardingResult(state=OnboardingState.COMPLETED))

        outcome = self.build(onboarding=runner, should_stop=lambda: True).handle_message(
            message(event_id="evt_stopping", open_id="ou_new"), now=NOW
        )

        self.assertEqual(outcome.handled_as, HandledAs.AUTO_PROVISIONING)
        self.assertEqual(runner.calls, [], "停机中不得发起带外部副作用的开通编排")
        self.assertEqual(self.log.count("reply.send_text"), 0)
        self.assertEqual(self.log.count("audit.onboarding.deferred_while_stopping"), 1)

    def test_the_claim_survives_and_stays_unreconciled(self) -> None:
        """停机跳过留下的是一条**故意的**孤儿：结论已提交，账本仍为空。"""

        runner = FakeOnboarding(result=OnboardingResult(state=OnboardingState.COMPLETED))

        self.build(onboarding=runner, should_stop=lambda: True).handle_message(
            message(event_id="evt_stopping_claim", open_id="ou_new"), now=NOW
        )

        self.assertEqual(
            self.state.events["evt_stopping_claim"],
            "auto_provisioning",
            "停机不得回滚已提交的认领",
        )
        self.assertEqual(
            set(self.state.onboarding_dispatched),
            set(),
            "没交接就不能记成已交接——对账扫描正是靠这个空账本认出这条孤儿",
        )

    def test_a_running_gateway_still_starts_onboarding(self) -> None:
        runner = FakeOnboarding(result=OnboardingResult(state=OnboardingState.NOT_AUTHORIZED))

        self.build(onboarding=runner, should_stop=lambda: False).handle_message(
            message(event_id="evt_running", open_id="ou_new"), now=NOW
        )

        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.log.count("audit.onboarding.deferred_while_stopping"), 0)


class OnboardingTerminalRenderingTests(PipelineTestCase):
    """#65 轻审 P2-4：非失败终态的缺省渲染兜底。

    缺省文案表原先只覆盖三条失败终态，一个不带 messages 的 ``matched`` / ``completed``
    会渲染成空列表——用户收到「正在核对，请稍候」之后再无下文，而系统认为一切正常。
    """

    def _reply_keys(self) -> list[str]:
        return [fields["content_key"] for fields in self.log.fields("audit.reply.sent")]

    def test_matched_without_messages_still_tells_the_user(self) -> None:
        runner = FakeOnboarding(result=OnboardingResult(state=OnboardingState.MATCHED))

        self.build(onboarding=runner).handle_message(
            message(event_id="evt_matched", open_id="ou_new"), now=NOW
        )

        self.assertEqual(self._reply_keys(), ["onboarding.checking", "onboarding.matched"])
        self.assertEqual(self.log.count("audit.onboarding.render_failed"), 0)

    def test_completed_without_a_scope_falls_back_to_the_internal_terminal(self) -> None:
        """「开通完成」必须报出公司与职能范围；报不出来就不能宣告成功。"""

        runner = FakeOnboarding(result=OnboardingResult(state=OnboardingState.COMPLETED))

        self.build(onboarding=runner).handle_message(
            message(event_id="evt_completed_bare", open_id="ou_new"), now=NOW
        )

        self.assertEqual(
            self._reply_keys(), ["onboarding.checking", "onboarding.internal_error"]
        )
        self.assertNotIn(
            "onboarding.completed",
            self._reply_keys(),
            "范围未知时不得替编排层宣告一个说不清范围的开通成功",
        )
        self.assertEqual(
            [fields["state"] for fields in self.log.fields("audit.onboarding.render_failed")],
            ["completed"],
            "兜底必须在审计里保留编排真正返回的状态，否则事后无法定位",
        )
        # Issue #280 §7.1 渲染点 4（空渲染兜底）：兜底出去的 `LX-ONBOARD-001` 同样
        # 必须带上这一次事件的追溯号。
        self.assertIn("trc_evt_completed_bare", self.log.fields("reply.send_text")[-1]["text"])

    def test_a_result_whose_only_message_is_checking_also_falls_back(self) -> None:
        """checking 由 gateway 独占且只发一次；过滤之后同样不能剩下空白。"""

        runner = FakeOnboarding(
            result=OnboardingResult(
                state=OnboardingState.COMPLETED,
                messages=(OnboardingMessage("onboarding.checking"),),
            )
        )

        self.build(onboarding=runner).handle_message(
            message(event_id="evt_only_checking", open_id="ou_new"), now=NOW
        )

        self.assertEqual(
            self._reply_keys(), ["onboarding.checking", "onboarding.internal_error"]
        )

    def test_started_without_messages_is_silent_by_design(self) -> None:
        """``started`` 是唯一允许没有下文的状态：checking 就是这一轮的完整交代。"""

        runner = FakeOnboarding(result=OnboardingResult(state=OnboardingState.STARTED))

        self.build(onboarding=runner).handle_message(
            message(event_id="evt_started", open_id="ou_new"), now=NOW
        )

        self.assertEqual(self._reply_keys(), ["onboarding.checking"])
        self.assertEqual(self.log.count("audit.onboarding.render_failed"), 0)

    def test_every_non_started_state_produces_at_least_one_message(self) -> None:
        """穷举：除 ``started`` 外，没有任何状态可以让用户悬在半空。"""

        for state in OnboardingState:
            if state is OnboardingState.STARTED:
                continue
            with self.subTest(state=state.value):
                self.log = CallLog()
                self.state = FakeState()
                runner = FakeOnboarding(result=OnboardingResult(state=state))
                self.build(onboarding=runner).handle_message(
                    message(event_id=f"evt_{state.value}", open_id="ou_new"), now=NOW
                )
                self.assertGreaterEqual(
                    len(self._reply_keys()),
                    2,
                    f"{state.value} 必须在 checking 之外再给用户一条结论",
                )

    def test_default_table_terminals_requiring_a_reference_carry_this_events_trace_id(
        self,
    ) -> None:
        """Issue #280 §7.1 渲染点 4（缺省文案表）：`SYNC_TIMEOUT`/`INTERNAL_ERROR`
        走 `_DEFAULT_ONBOARDING_MESSAGES` 缺省表（编排没有自带 messages）时，同样
        必须补上 `reference`——否则会在渲染时抛 `ContentRenderError`，本用例首先
        证明它不抛，再证明追溯号真的进了正文。"""

        for state in (OnboardingState.SYNC_TIMEOUT, OnboardingState.INTERNAL_ERROR):
            with self.subTest(state=state.value):
                self.log = CallLog()
                self.state = FakeState()
                runner = FakeOnboarding(result=OnboardingResult(state=state))
                self.build(onboarding=runner).handle_message(
                    message(event_id=f"evt_default_{state.value}", open_id="ou_new"), now=NOW
                )
                self.assertIn(f"trc_evt_default_{state.value}", self.log.fields("reply.send_text")[-1]["text"])


class BlackHoleOutboundTests(PipelineTestCase):
    """出站黑洞：飞书接受连接但永不响应（codex 二轮 P1-C）。

    真正的时间上限来自注入给 SDK 的 HTTP 超时——它由停机预算推导，断言在
    ``test_gateway_config.BuildSupervisorTests``。这里断的是**超时发生之后**的行为：
    加表情与回复都不得把已确定的处理结论带走，也不得阻断后续步骤。
    """

    def test_a_hanging_reaction_does_not_block_the_pipeline(self) -> None:
        outcome = self.build(reaction_error=TimeoutError("出站黑洞：加表情超时")).handle_message(
            message(), now=NOW
        )

        self.assertEqual(outcome.handled_as, HandledAs.TASK_QUEUED, "加表情超时不得阻断入队")
        self.assertEqual(len(self.state.tasks), 1)
        self.assertEqual(self.log.count("audit.reaction.failed"), 1)

    def test_a_hanging_reply_does_not_undo_the_committed_outcome(self) -> None:
        class HangingReplies:
            def send_text(self, **kwargs):
                raise TimeoutError("出站黑洞：回复超时")

        self.state.conversations[("usr_1", "oc_1", "")] = FakeConversation(
            conversation_id="cnv_busy", running_task_id="tsk_running"
        )
        pipeline = EventPipeline(
            store=FakeStore(self.state, self.log),
            reactions=FakeReactions(self.log),
            replies=HangingReplies(),
            audit=FakeAudit(self.log),
        )

        outcome = pipeline.handle_message(message(), now=NOW)

        self.assertEqual(outcome.handled_as, HandledAs.BUSY_HINT)
        self.assertEqual(
            self.state.events.get("evt_1"),
            "busy_hint",
            "回复超时不得回滚已提交的处理结论——否则重投会让这条消息在任务结束后生效",
        )
        self.assertEqual(self.log.count("audit.reply.failed"), 1)


class ShutdownSkipsBestEffortRepliesTests(PipelineTestCase):
    """停机中跳过尽力而为的回复，但**已提交的结论不动**。"""

    def test_a_pending_reply_is_skipped_while_stopping(self) -> None:
        self.state.conversations[("usr_1", "oc_1", "")] = FakeConversation(
            conversation_id="cnv_busy", running_task_id="tsk_running"
        )
        pipeline = EventPipeline(
            store=FakeStore(self.state, self.log),
            reactions=FakeReactions(self.log),
            replies=FakeReplies(self.log),
            audit=FakeAudit(self.log),
            should_stop=lambda: True,
        )

        outcome = pipeline.handle_message(message(), now=NOW)

        self.assertEqual(outcome.handled_as, HandledAs.BUSY_HINT)
        self.assertEqual(
            self.state.events.get("evt_1"), "busy_hint", "停机不得回滚已提交的结论"
        )
        self.assertEqual(
            self.log.count("reply.send_text"), 0, "停机中不得再发出站 HTTP"
        )
        self.assertEqual(self.log.count("audit.reply.skipped_while_stopping"), 1)

    def test_replies_are_sent_when_not_stopping(self) -> None:
        self.state.conversations[("usr_1", "oc_1", "")] = FakeConversation(
            conversation_id="cnv_busy", running_task_id="tsk_running"
        )
        self.build().handle_message(message(), now=NOW)

        self.assertEqual(self.log.count("reply.send_text"), 1)


class SuspendedUserTests(PipelineTestCase):
    """已停用用户不入队（合同：停用一经感知即禁止发起新的问数）。"""

    def test_suspended_user_gets_a_reply_and_no_task(self) -> None:
        self.state.users["ou_1"] = UserRecord(user_id="usr_1", state=UserState.SUSPENDED)
        outcome = self.build().handle_message(message(), now=NOW)

        self.assertEqual(outcome.handled_as, HandledAs.DROPPED)
        self.assertEqual(len(self.state.tasks), 0)
        self.assertEqual(self.log.count("reply.send_text"), 1)


class OnboardingInFlightTests(PipelineTestCase):
    """开通正在进行中又收到一条消息：回**同步中**提示，不是「正在核对」。

    合同：「权限同步期间，卡片明确显示『权限正在同步，预计最多需要十五分钟』，用户无需
    重复开通」。把第一条提示错用在这个阶段，用户每问一次都被告知「正在核对你的身份」，
    而系统其实早就核对完、正在等 MCP 同步。
    """

    def test_a_message_during_sync_gets_the_sync_notice(self) -> None:
        self.state.users["ou_1"] = UserRecord(user_id="usr_1", state=UserState.PROVISIONING)
        runner = FakeOnboarding()

        outcome = self.build(onboarding=runner).handle_message(message(), now=NOW)

        self.assertEqual(outcome.handled_as, HandledAs.NOT_PROVISIONED)
        self.assertEqual(
            [fields["content_key"] for fields in self.log.fields("audit.reply.sent")],
            ["onboarding.matched"],
            "同步期间的重复消息必须回同步提示，而不是第一条「正在核对」",
        )

    def test_it_does_not_re_trigger_the_orchestration(self) -> None:
        """那一条正在 scheduler 里跑，重复触发只会多一次外部副作用。"""

        self.state.users["ou_1"] = UserRecord(user_id="usr_1", state=UserState.PROVISIONING)
        runner = FakeOnboarding()

        self.build(onboarding=runner).handle_message(message(), now=NOW)

        self.assertEqual(runner.calls, [])
        self.assertEqual(len(self.state.tasks), 0, "同步期间不入队")


class MemoryCommandDispatchTests(PipelineTestCase):
    """``/memory`` 命令面的分发断言（Issue #357 S-H3-3）。真正的读写语义（唯一
    索引、上限、跨用户隔离）在 ``tests/test_postgres_user_memory.py`` 真库覆盖；
    这里只钉「pipeline 第 6 步的分发是否按设计文接线正确」——四个子命令各自
    调用了正确的 ``tx`` 方法、正确的审计事件、正确的回执文案键。"""

    def test_list_when_empty_gets_the_empty_hint_and_no_write_audit(self) -> None:
        outcome = self.build().handle_message(message(text="/memory list"), now=NOW)

        self.assertEqual(outcome.handled_as, HandledAs.COMMAND)
        replies = self.log.fields("reply.send_text")
        self.assertEqual(len(replies), 1)
        self.assertIn("还没有登记任何记忆", replies[0]["text"])
        self.assertEqual(
            [
                action
                for action in self.log.names()
                if action.startswith("audit.command.memory_")
            ],
            [],
            "list 是只读操作，不产生三个写审计事件之一",
        )

    def test_remember_writes_through_the_transaction_and_records_audit(self) -> None:
        outcome = self.build().handle_message(
            message(text="/memory remember term_mapping 大尼日 => 尼日利亚"), now=NOW
        )

        self.assertEqual(outcome.handled_as, HandledAs.COMMAND)
        entries = self.state.user_memory.get("usr_1", [])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].memory_key, "大尼日")
        self.assertEqual(entries[0].memory_value, "尼日利亚")
        remembered = self.log.fields("audit.command.memory_remember")
        self.assertEqual(len(remembered), 1)
        self.assertEqual(remembered[0]["user_id"], "usr_1")
        self.assertEqual(remembered[0]["memory_type"], "term_mapping")
        replies = self.log.fields("reply.send_text")
        self.assertEqual(len(replies), 1)
        self.assertIn("已登记", replies[0]["text"])

    def test_remember_with_a_protocol_marker_value_is_rejected_without_writing_or_auditing(
        self,
    ) -> None:
        """P2（Trace #373 H3 批 codex 外审②修复③）：登记路径复用注入侧同一道
        安全校验（``config.content.validate_user_visible_text``）——含协议词
        （``mcp__``）的值必须在写库之前被拒绝，不能先回执「已登记、下一次提问
        生效」，实际这条记忆在每次注入时都被 worker 侧静默跳过、永远不生效。

        变异锚点：把 ``_handle_memory_command`` REMEMBER 分支里新增的
        ``validate_user_visible_text`` 调用删掉，本用例会从「零写入 + 拒绝
        回执」变红成「写入成功 + memory.remembered 回执」。
        """

        outcome = self.build().handle_message(
            message(text="/memory remember term_mapping 大厂 => mcp__query__list_metrics"),
            now=NOW,
        )

        self.assertEqual(outcome.handled_as, HandledAs.COMMAND)
        self.assertEqual(self.state.user_memory.get("usr_1", []), [])
        self.assertEqual(self.log.fields("audit.command.memory_remember"), [])
        replies = self.log.fields("reply.send_text")
        self.assertEqual(len(replies), 1)
        self.assertIn("未能登记", replies[0]["text"])
        self.assertNotIn("已登记", replies[0]["text"])

    def test_remember_with_a_trace_id_leak_shaped_value_is_rejected(self) -> None:
        """同一道校验的第二个触发形状（协议标识 ``trace_id``，同
        ``tests/test_postgres_user_memory.py`` ``UnsafeEntrySkippedTests`` 的
        判定口径）——不是只测 ``mcp__`` 这一个字面量。"""

        outcome = self.build().handle_message(
            message(text="/memory remember term_mapping k => 请查看 trace_id=abc123 的日志"),
            now=NOW,
        )

        self.assertEqual(outcome.handled_as, HandledAs.COMMAND)
        self.assertEqual(self.state.user_memory.get("usr_1", []), [])
        self.assertEqual(self.log.fields("audit.command.memory_remember"), [])
        replies = self.log.fields("reply.send_text")
        self.assertIn("未能登记", replies[0]["text"])

    def test_remember_with_an_ordinary_business_value_is_unaffected_by_the_safety_check(
        self,
    ) -> None:
        """反向哨兵：正常业务值（不含协议词/固定误导词表）不受这道新增校验
        影响——与既有 ``test_remember_writes_through_the_transaction_and_
        records_audit`` 互为正反面，防止安全校验误伤日常登记。"""

        outcome = self.build().handle_message(
            message(text="/memory remember calibration_preference 环比口径 => 按自然月环比"),
            now=NOW,
        )

        self.assertEqual(outcome.handled_as, HandledAs.COMMAND)
        entries = self.state.user_memory.get("usr_1", [])
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(self.log.fields("audit.command.memory_remember")), 1)
        replies = self.log.fields("reply.send_text")
        self.assertIn("已登记", replies[0]["text"])

    def test_list_after_remember_shows_the_registered_entry(self) -> None:
        self.build().handle_message(
            message(text="/memory remember term_mapping 大尼日 => 尼日利亚"), now=NOW
        )

        outcome = self.build().handle_message(message(event_id="evt_2", text="/memory list"), now=NOW)

        self.assertEqual(outcome.handled_as, HandledAs.COMMAND)
        replies = self.log.fields("reply.send_text")
        listed = replies[-1]["text"]
        self.assertIn("大尼日", listed)
        self.assertIn("尼日利亚", listed)

    def test_forget_an_existing_entry_deletes_it_and_records_audit(self) -> None:
        self.build().handle_message(
            message(text="/memory remember term_mapping k => v"), now=NOW
        )
        memory_id = self.state.user_memory["usr_1"][0].memory_id

        outcome = self.build().handle_message(
            message(event_id="evt_2", text=f"/memory forget {memory_id}"), now=NOW
        )

        self.assertEqual(outcome.handled_as, HandledAs.COMMAND)
        self.assertEqual(self.state.user_memory.get("usr_1", []), [])
        forgotten = self.log.fields("audit.command.memory_forget")
        self.assertEqual(len(forgotten), 1)
        replies = self.log.fields("reply.send_text")
        self.assertIn("已删除", replies[-1]["text"])

    def test_forget_an_unknown_id_gets_the_not_found_hint_and_no_audit(self) -> None:
        outcome = self.build().handle_message(
            message(text="/memory forget mem_01ARZ3NDEKTSV4RRFFQ69G5FAV"), now=NOW
        )

        self.assertEqual(outcome.handled_as, HandledAs.COMMAND)
        self.assertEqual(self.log.fields("audit.command.memory_forget"), [])
        replies = self.log.fields("reply.send_text")
        self.assertIn("没有找到", replies[0]["text"])

    def test_clear_removes_all_entries_and_records_the_cleared_count(self) -> None:
        self.build().handle_message(
            message(text="/memory remember term_mapping k1 => v1"), now=NOW
        )
        self.build().handle_message(
            message(event_id="evt_2", text="/memory remember calibration_preference k2 => v2"),
            now=NOW,
        )

        outcome = self.build().handle_message(message(event_id="evt_3", text="/memory clear"), now=NOW)

        self.assertEqual(outcome.handled_as, HandledAs.COMMAND)
        self.assertEqual(self.state.user_memory.get("usr_1", []), [])
        cleared = self.log.fields("audit.command.memory_clear")
        self.assertEqual(len(cleared), 1)
        self.assertEqual(cleared[0]["cleared_count"], 2)

    def test_a_multiline_message_starting_with_memory_clear_does_not_actually_clear(self) -> None:
        """P2（Trace #373 H3 批 codex 外审②修复②）端到端回归：多行粘贴消息
        ``/memory\\nclear`` 必须落 usage_help、零删除——不能被当成真实的 clear
        命令执行。这是 pipeline 分发层面的确认，字符串解析层面的覆盖见
        ``tests/test_conversation_commands.py`` 的 ``NewlineInjectionGuardTests``。

        变异锚点：把 ``commands.parse_memory_command`` 的换行守卫删掉，本用例
        会从「零删除 + usage_help」变红成「两条记忆被清空」。
        """

        self.build().handle_message(
            message(text="/memory remember term_mapping k1 => v1"), now=NOW
        )
        self.build().handle_message(
            message(event_id="evt_2", text="/memory remember calibration_preference k2 => v2"),
            now=NOW,
        )
        self.assertEqual(len(self.state.user_memory.get("usr_1", [])), 2)

        outcome = self.build().handle_message(
            message(event_id="evt_3", text="/memory\nclear"), now=NOW
        )

        self.assertEqual(outcome.handled_as, HandledAs.COMMAND)
        self.assertEqual(
            len(self.state.user_memory.get("usr_1", [])),
            2,
            "换行拼出的 /memory\\nclear 不是合法命令，两条已登记记忆必须原样保留",
        )
        self.assertEqual(self.log.fields("audit.command.memory_clear"), [])
        replies = self.log.fields("reply.send_text")
        self.assertIn("支持的记忆命令", replies[-1]["text"])

    def test_limit_exceeded_is_rejected_without_writing_and_without_audit(self) -> None:
        from lingxi.core.user_memory import MAX_MEMORY_ENTRIES_PER_USER

        for index in range(MAX_MEMORY_ENTRIES_PER_USER):
            outcome = self.build().handle_message(
                message(event_id=f"evt_fill_{index}", text=f"/memory remember term_mapping k{index} => v{index}"),
                now=NOW,
            )
            self.assertEqual(outcome.handled_as, HandledAs.COMMAND)
        self.assertEqual(len(self.state.user_memory["usr_1"]), MAX_MEMORY_ENTRIES_PER_USER)

        outcome = self.build().handle_message(
            message(event_id="evt_over", text="/memory remember term_mapping one_too_many => v"),
            now=NOW,
        )

        self.assertEqual(outcome.handled_as, HandledAs.COMMAND)
        self.assertEqual(
            len(self.state.user_memory["usr_1"]),
            MAX_MEMORY_ENTRIES_PER_USER,
            "超过上限的登记不得写入任何行",
        )
        replies = self.log.fields("reply.send_text")
        self.assertIn("已达到记忆条数上限", replies[-1]["text"])

    def test_malformed_memory_command_gets_usage_help_not_a_write(self) -> None:
        outcome = self.build().handle_message(
            message(text="/memory remember bad_type k => v"), now=NOW
        )

        self.assertEqual(outcome.handled_as, HandledAs.COMMAND)
        self.assertEqual(self.state.user_memory.get("usr_1", []), [])
        replies = self.log.fields("reply.send_text")
        self.assertIn("支持的记忆命令", replies[0]["text"])
        self.assertEqual(
            [
                action
                for action in self.log.names()
                if action.startswith("audit.command.memory_")
            ],
            [],
        )

    def test_malformed_memory_command_does_not_get_the_generic_slash_rejected_text(self) -> None:
        """安全回归：/memory 的豁免必须精确——格式写错的 /memory 消息得到专属用法
        提示，不是与 /config 共用的泛用「不支持的命令」文案（否则用户会被引导去
        『用自然语言重新描述问题』，却永远学不会正确的 /memory 语法）。"""

        outcome = self.build().handle_message(message(text="/memory rember typo"), now=NOW)

        self.assertEqual(outcome.handled_as, HandledAs.COMMAND)
        replies = self.log.fields("reply.send_text")
        self.assertNotEqual(replies[0]["text"], SLASH_REJECTED_TEXT)
        self.assertEqual(self.log.fields("audit.command.unsupported_slash"), [])


class MemoryCommandBypassesBusyTests(PipelineTestCase):
    """设计文 b 节／pipeline 类文档「第 6 步的延伸」：/memory 与 /stop 同一姿态，
    忙碌期间照常被处理，不回「当前任务仍在处理中」。"""

    def setUp(self) -> None:
        super().setUp()
        self.conversation = FakeConversation(
            conversation_id="cnv_busy_memory",
            agent_session_id="ses_1",
            running_task_id="tsk_running",
        )
        self.state.conversations[("usr_1", "oc_1", "")] = self.conversation

    def test_memory_list_during_busy_is_processed_not_deflected(self) -> None:
        outcome = self.build().handle_message(message(text="/memory list"), now=NOW)

        self.assertEqual(outcome.handled_as, HandledAs.COMMAND)
        replies = self.log.fields("reply.send_text")
        self.assertEqual(len(replies), 1)
        self.assertNotEqual(
            replies[0]["text"], BUSY_HINT_TEXT, "/memory 不受忙碌判定拦截"
        )

    def test_memory_remember_during_busy_still_writes(self) -> None:
        outcome = self.build().handle_message(
            message(text="/memory remember term_mapping k => v"), now=NOW
        )

        self.assertEqual(outcome.handled_as, HandledAs.COMMAND)
        self.assertEqual(len(self.state.user_memory.get("usr_1", [])), 1)
        self.assertEqual(len(self.state.tasks), 0, "/memory 不是问数任务，不入队")


class BusyHintHonestyTests(PipelineTestCase):
    """Issue #465（rc22 S-3）：忙碌期提示区分"排队中"与"处理中"两种真实状态，
    不再对两者一概说"当前任务仍在处理中"（触发现场：批闸缺陷下重任务霸占
    worker、轻任务迟迟没被领取时，用户发第二条消息只会被一句"处理中"误导，
    以为系统正在忙它这条消息）。"""

    def _busy_conversation(self, *, task_status: str) -> None:
        self.state.conversations[("usr_1", "oc_1", "")] = FakeConversation(
            conversation_id="cnv_busy", running_task_id="tsk_running"
        )
        self.state.tasks.append(
            FakeTask(
                task_id="tsk_running",
                conversation_id="cnv_busy",
                user_id="usr_1",
                inbound_event_id="evt_prior",
                prompt="之前的问题",
                resumed_session=False,
                target_worker_version="stable",
                status=task_status,
            )
        )

    def test_says_queued_when_the_running_task_has_not_been_claimed(self) -> None:
        self._busy_conversation(task_status="queued")

        outcome = self.build().handle_message(message(), now=NOW)

        self.assertEqual(outcome.handled_as, HandledAs.BUSY_HINT)
        replies = self.log.fields("reply.send_text")
        self.assertEqual(len(replies), 1)
        self.assertEqual(
            replies[0]["text"],
            default_content_catalog().text("gateway.busy_hint_queued").text,
        )

    def test_says_processing_when_the_running_task_is_actually_running(self) -> None:
        self._busy_conversation(task_status="running")

        outcome = self.build().handle_message(message(), now=NOW)

        self.assertEqual(outcome.handled_as, HandledAs.BUSY_HINT)
        replies = self.log.fields("reply.send_text")
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["text"], BUSY_HINT_TEXT)

    def test_unknown_or_missing_task_status_defaults_to_processing(self) -> None:
        """`running_task_status` 为 ``None``（结构上不该出现，见 ``ConversationRecord``
        文档）时保守落回既有"处理中"文案，不猜成"排队中"。"""

        self.state.conversations[("usr_1", "oc_1", "")] = FakeConversation(
            conversation_id="cnv_busy", running_task_id="tsk_unknown"
        )

        outcome = self.build().handle_message(message(), now=NOW)

        self.assertEqual(outcome.handled_as, HandledAs.BUSY_HINT)
        replies = self.log.fields("reply.send_text")
        self.assertEqual(replies[0]["text"], BUSY_HINT_TEXT)


class ResponseCoverageTests(PipelineTestCase):
    """Issue #465（rc22 S-3）：100% 响应覆盖产品合同——集成测试枚举
    ``EventPipeline.handle_message`` 全部尚未被专门识别的异常出口，逐一验证
    最终都以给用户发一条诚实提示收尾，而不是让异常穿透到
    ``apps/gateway/__init__.py``/``adapters/feishu_longconn.py`` 那一层只留一条
    ``event.handler_failed`` 审计、用户什么都收不到（触发现场：批闸缺陷下
    「你好」排队 5+ 分钟零反馈）。

    枚举面：``_within_transaction`` 内部三个此前从未被任何既有用例覆盖过的
    异常源头——事务最开始的幂等写入（``insert_inbound_event``）、入队时的话题
    抢占（``claim_conversation``）、``/memory remember`` 写路径
    （``remember_user_memory``）。三者分别代表"事务最前端"、"业务分支中段"、
    "命令面写路径"三类不同位置，共同证明这道兜底不是只补了某一个具体调用点，
    而是整个方法级别的安全网（`QueueInsertFailure`/``insert_task`` 那条已识别
    的失败路径见 ``EnqueueFailureTests``，不在本组重复覆盖）。

    **变异验红**（验证与门禁第八节）：临时把 ``EventPipeline.handle_message``
    的 ``except Exception`` 缩窄成 ``except QueueInsertFailure``（去掉本条
    兜底分支）后，本组用例全部由绿转红（异常原样穿出 ``handle_message``）；
    恢复后复绿。红/绿证据见交付报告，不在本文件重复记录。
    """

    def test_insert_inbound_event_failure_gets_an_honest_fallback_reply(self) -> None:
        outcome = self.build(fail_on="insert_inbound_event").handle_message(
            message("evt_boom"), now=NOW
        )

        self.assertIsNone(outcome.handled_as)
        replies = self.log.fields("reply.send_text")
        self.assertEqual(len(replies), 1, "任何未被识别的异常都必须换成一条用户可见回复")
        self.assertEqual(
            replies[0]["text"],
            default_content_catalog().text(
                "gateway.unexpected_error", reference="trc_evt_boom"
            ).text,
        )
        failed = self.log.fields("audit.event.pipeline_failed")
        self.assertEqual(len(failed), 1)
        self.assertIn("RuntimeError", failed[0]["error"])

    def test_claim_conversation_failure_gets_an_honest_fallback_reply(self) -> None:
        outcome = self.build(fail_on="claim_conversation").handle_message(
            message("evt_boom"), now=NOW
        )

        self.assertIsNone(outcome.handled_as)
        replies = self.log.fields("reply.send_text")
        self.assertEqual(len(replies), 1)
        self.assertEqual(
            replies[0]["text"],
            default_content_catalog().text(
                "gateway.unexpected_error", reference="trc_evt_boom"
            ).text,
        )

    def test_memory_remember_write_failure_gets_an_honest_fallback_reply(self) -> None:
        outcome = self.build(fail_on="remember_user_memory").handle_message(
            message("evt_boom", text="/memory remember term_mapping k => v"), now=NOW
        )

        self.assertIsNone(outcome.handled_as)
        replies = self.log.fields("reply.send_text")
        self.assertEqual(len(replies), 1)
        self.assertEqual(
            replies[0]["text"],
            default_content_catalog().text(
                "gateway.unexpected_error", reference="trc_evt_boom"
            ).text,
        )

    def test_fallback_reply_is_skipped_while_stopping_not_forced(self) -> None:
        """停机中：结论（此处是"处理失败"这件事本身）已经无法再落库改变，回复是
        尽力而为的那一部分——与既有 `deferred` 停机跳过姿态一致，不因为是兜底
        路径就破例在停机预算之外硬发一次出站调用。"""

        outcome = self.build(
            fail_on="insert_inbound_event", should_stop=lambda: True
        ).handle_message(message("evt_boom"), now=NOW)

        self.assertIsNone(outcome.handled_as)
        self.assertEqual(self.log.count("reply.send_text"), 0)
        self.assertEqual(self.log.count("audit.reply.skipped_while_stopping"), 1)


if __name__ == "__main__":
    unittest.main()
