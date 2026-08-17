"""gateway 事件管线的可注入断言（不连真实飞书、不连数据库）。

认领断言：`V-接入-07`（加表情覆盖五类消息且发生在去重之后、状态判定之前）、
`V-接入-08`（加表情失败不阻断）、`V-接入-09`（重复投递在用户可见面不重复）、
`V-接入-11`（任务归属只来自发送者标识）、`V-会话-02`/`V-会话-03`（两小时规则）、
`V-会话-08`（续用判定在入队时做出）、`V-会话-09`（忙碌期 `/new`）、
`V-会话-10`（`/stop` 不被忙碌拦截）、`V-队列-03`（入队未成功不给已受理回复）。

另有 Issue #189 的两小时自动新会话告知（``SessionRotationNoticeTests``）：合同
「系统明确告诉用户已经开启新会话」的第二条触发路径，矩阵中尚无对应断言编号，
本模块按合同原文与产品负责人 2026-08-17 定稿约束其触发面与文案。

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


def _payload(chat_type: str | None = "p2p", message_type: str = "text") -> dict:
    message = {
        "message_id": "om_1",
        "chat_id": "oc_1",
        "message_type": message_type,
        "content": '{"text": "你好"}',
    }
    if chat_type is not None:
        message["chat_type"] = chat_type
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

        这条属性有**三道**互相独立的保险：追加发生在 ``insert_task`` 成功之后；
        入队失败路径整体丢弃 ``deferred``；注入 store 没有独立发送权时事务失败直接
        上抛。因此改坏其中任意一道（甚至前两道同时）都不会让本用例变红——它锁的是
        那条对用户成立的产品属性本身，不是某一处实现位置，与
        ``EnqueueFailureTests`` 对整条入队失败路径的断言同一姿态。
        """

        self.stale_conversation()
        pipeline = self.build(fail_on="insert_task")

        with self.assertRaises(RuntimeError):
            pipeline.handle_message(message(), now=NOW)

        self.assertEqual(self.texts(), [], "入队失败时不得发出新会话告知")

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


class EnqueueFailureTests(PipelineTestCase):
    """`V-队列-03`：入队未成功时不给已受理回复，事件不被标记为已成功处理。"""

    def test_task_insert_failure_leaves_no_acceptance_signal(self) -> None:
        pipeline = self.build(fail_on="insert_task")

        with self.assertRaises(RuntimeError):
            pipeline.handle_message(message("evt_fail"), now=NOW)

        self.assertEqual(
            self.log.count("reply.send_text"), 0, "入队未成功时用户不得收到任何回复"
        )
        self.assertEqual(len(self.state.tasks), 0)
        self.assertNotIn(
            "evt_fail",
            self.state.events,
            "事务未提交，事件行不得存在——否则飞书重投时会被当成重复投递而静默丢弃",
        )
        self.assertEqual(self.state.notifies, 0, "入队失败不得发出 NOTIFY")

    def test_claim_failure_rolls_back_the_claim(self) -> None:
        """`V-队列-02` 的可注入面：抢占成功但入队失败，话题不得永久忙碌。"""

        self.state.conversations[("usr_1", "oc_1", "")] = FakeConversation(
            conversation_id="cnv_1"
        )
        pipeline = self.build(fail_on="insert_task")

        with self.assertRaises(RuntimeError):
            pipeline.handle_message(message("evt_fail"), now=NOW)

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


if __name__ == "__main__":
    unittest.main()
