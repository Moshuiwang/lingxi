"""管理命令面在 gateway 管线里的分流点（Issue #95 S-M-01）。

认领断言：V-管理-21（私聊分流：登记表有效条目改道进入管理命令面，不进入自动开通）、
V-管理-24（登记表内无有效条目的发送者维持原行为——含"未装配路由"与"路由判定拒绝"
两种落回既有分支的情形；专用主体因数据漂移意外获得 app_user 行时仍必须命中，见
``DelegatedSubjectStructuralExitTests``，opus P3-1 修复）、V-管理-25（分流范围
precisely限定于私聊文本消息，非文本消息与**普通**已开通业务用户完全不触达管理
路由，业务用户路径零改动——专用主体本身是唯一的结构性例外，见下一段）。

``AdminRoutingPipelineTests`` 只测试**管线在给定 ``AdminRouter`` 结果时如何分流**，
不测试路由本身的判定逻辑——后者（默认拒绝、角色判定、命令解析、审计）见
``test_admin_router.py``；真实数据库判定见 ``test_admin_registry_postgres.py``。
三层合起来才是完整证据链，本文件只覆盖管线这一层的接线是否正确，因此这里用一个
可编程的假 ``AdminRouter``，不连真实登记表。

``DelegatedSubjectStructuralExitTests`` 覆盖专用主体结构性出口前置（opus P3-1
修复）：命中配置中已解析好的专用授权主体 open_id 时，判定发生在按用户状态分派
**之前**，不会被 `state` 意外不是 `NOT_PROVISIONED` 这件事带偏。

``NonPrivateChatNeverReachesAdminRoutingTests``（C8）是群聊防线的回归哨兵：证明
非私聊事件在上游（`apps/gateway/__init__.py` 的 `NonPrivateChatError` 分支）就
被挡住、结构上到不了管理路由，防未来重构把这道过滤移走或绕开。
"""

from __future__ import annotations

import unittest

from gateway_fakes import CallLog, FakeAudit, FakeOnboarding, FakeReactions, FakeReplies, FakeState, FakeStore, provisioned_user
from lingxi.apps.gateway import make_event_handler
from lingxi.core.admin.router import AdminRouteOutcome
from lingxi.core.conversation import EventPipeline
from lingxi.core.conversation.ports import HandledAs, OnboardingResult, OnboardingState
from test_gateway_pipeline import message


class FakeAdminRouter:
    """可编程的管理路由假实现：按 ``open_id`` 查表返回预设结论，记录每次调用。

    ``chat_id``/``thread_id``/``message_id``（Issue #96 S-M-02 新增，均带默认值，
    与真实 ``AdminCommandRouter.route`` 签名同步）：``pipeline.py`` 现在无条件把
    ``InboundMessage`` 的这三个字段传给 ``route()``，本假实现必须能接住这几个
    关键字参数，否则全文件既有用例会在本 Story 落地后因 ``TypeError`` 集体失败。
    """

    def __init__(self, outcomes: dict[str, AdminRouteOutcome] | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self._outcomes = outcomes or {}

    def route(
        self,
        *,
        open_id: str,
        text: str,
        trace_id: str,
        chat_id: str = "",
        thread_id: str | None = None,
        message_id: str = "",
    ) -> AdminRouteOutcome:
        self.calls.append(
            {
                "open_id": open_id,
                "text": text,
                "trace_id": trace_id,
                "chat_id": chat_id,
                "thread_id": thread_id,
                "message_id": message_id,
            }
        )
        return self._outcomes.get(open_id, AdminRouteOutcome(handled=False))


class AdminRoutingPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.log = CallLog()
        self.state = FakeState()
        # 唯一一个已开通用户，用来证明已开通用户完全不触达管理路由（`ou_1`）。
        self.state.users["ou_1"] = provisioned_user(open_id="ou_1", user_id="usr_1")

    def _pipeline(
        self,
        *,
        admin_router,
        onboarding=None,
        innertest_roster_gate=None,
        delegated_subject_open_id: str | None = None,
    ) -> EventPipeline:
        return EventPipeline(
            store=FakeStore(self.state, self.log),
            reactions=FakeReactions(self.log),
            replies=FakeReplies(self.log),
            audit=FakeAudit(self.log),
            onboarding=onboarding,
            admin_router=admin_router,
            innertest_roster_gate=innertest_roster_gate,
            delegated_subject_open_id=delegated_subject_open_id,
        )

    def test_active_admin_short_circuits_before_auto_provisioning(self) -> None:
        """登记表当前有效条目：分流进入管理命令面，AUTO_PROVISIONING 完全不触发。"""

        router = FakeAdminRouter(
            {
                "ou_admin": AdminRouteOutcome(
                    handled=True,
                    content_key="admin.help",
                    content_version="internal",
                    reply_text="Lingxi 管理命令：...",
                )
            }
        )
        onboarding = FakeOnboarding()
        pipeline = self._pipeline(admin_router=router, onboarding=onboarding)

        outcome = pipeline.handle_message(message(open_id="ou_admin", text="/admin help"))

        self.assertEqual(outcome.handled_as, HandledAs.COMMAND)
        # 路由确实被以正确的身份、正文、追溯号调用了一次。
        self.assertEqual(len(router.calls), 1)
        self.assertEqual(router.calls[0]["open_id"], "ou_admin")
        self.assertEqual(router.calls[0]["text"], "/admin help")
        # 开通编排完全没有被触发——不是"触发了但被覆盖"，是根本没调用。
        self.assertEqual(onboarding.calls, [])
        # 既有的未开通/自动开通审计动作一条都没有产生。
        self.assertNotIn("audit.inbound_event.not_provisioned", self.log.names())
        self.assertNotIn("audit.inbound_event.auto_provisioning", self.log.names())
        self.assertIn("audit.inbound_event.admin_command", self.log.names())
        # 回复内容确实是路由给出的那段文字，直接发送，不经过内容目录。
        replies = self.log.fields("reply.send_text")
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["text"], "Lingxi 管理命令：...")

    def test_unregistered_sender_falls_back_to_existing_auto_provisioning(self) -> None:
        """未登记/已撤销发送者（路由判定 handled=False）：落回既有开通分支，
        行为与本 Story 加入之前逐字节一致——这是"登记表内无有效条目维持原行为"
        的直接证据。"""

        router = FakeAdminRouter()  # 空表：任何 open_id 都得到默认的 handled=False
        onboarding = FakeOnboarding(
            result=OnboardingResult(state=OnboardingState.STARTED)
        )
        pipeline = self._pipeline(admin_router=router, onboarding=onboarding)

        outcome = pipeline.handle_message(message(open_id="ou_unknown", text="随便问点什么"))

        # 路由被调用过（真实读了一次表），但结论不改变既有分支。
        self.assertEqual(len(router.calls), 1)
        self.assertEqual(outcome.handled_as, HandledAs.AUTO_PROVISIONING)
        self.assertEqual(len(onboarding.calls), 1)
        self.assertIn("audit.inbound_event.auto_provisioning", self.log.names())
        self.assertNotIn("audit.inbound_event.admin_command", self.log.names())

    def test_no_router_configured_matches_pre_existing_behavior(self) -> None:
        """完全不装配 ``admin_router``（``None``）：行为与本项加入之前逐字节一致。"""

        onboarding = FakeOnboarding(result=OnboardingResult(state=OnboardingState.STARTED))
        pipeline = self._pipeline(admin_router=None, onboarding=onboarding)

        outcome = pipeline.handle_message(message(open_id="ou_unknown"))

        self.assertEqual(outcome.handled_as, HandledAs.AUTO_PROVISIONING)
        self.assertEqual(len(onboarding.calls), 1)

    def test_non_text_message_never_reaches_admin_router(self) -> None:
        """分流范围限定于私聊文本消息：非文本消息即便发送者已登记也不触达路由，
        维持既有"非文本不入队"行为。"""

        router = FakeAdminRouter(
            {"ou_admin": AdminRouteOutcome(handled=True, reply_text="不应该被看到")}
        )
        onboarding = FakeOnboarding()
        pipeline = self._pipeline(admin_router=router, onboarding=onboarding)

        outcome = pipeline.handle_message(
            message(open_id="ou_admin", message_type="image")
        )

        self.assertEqual(router.calls, [])
        # 非文本 + 未开通：仍旧走既有的 AUTO_PROVISIONING 记事件路径（gateway 对
        # 未开通用户的消息类型不做二次过滤，见 pipeline.py 第 5 步注释）。
        self.assertEqual(outcome.handled_as, HandledAs.AUTO_PROVISIONING)

    def test_already_provisioned_user_never_reaches_admin_router(self) -> None:
        """已开通的**普通业务用户**完全不触达管理路由，这是"不改变业务用户
        开通/问数行为"的结构性保证，不依赖路由自身的判断——``ou_1`` 不是本
        管线配置的专用授权主体（`delegated_subject_open_id` 本用例未装配，
        默认 `None`），第 4 步的专用主体判定对它恒不命中；第 5 步的管理面
        分流又只在 `NOT_PROVISIONED` 分支内，已开通用户同样不落进去。两层
        结构性保证合起来才是完整证据；专用主体本身即便"意外"取得 app_user
        行仍必须进管理面/拒绝出口而不是这里，见
        ``DelegatedSubjectStructuralExitTests``（opus P3-1 修复）。"""

        router = FakeAdminRouter(
            {"ou_1": AdminRouteOutcome(handled=True, reply_text="不应该被看到")}
        )
        pipeline = self._pipeline(admin_router=router)

        outcome = pipeline.handle_message(message(open_id="ou_1", text="本月销售额"))

        self.assertEqual(router.calls, [])
        self.assertEqual(outcome.handled_as, HandledAs.TASK_QUEUED)


class WriteCommandContextThreadingTests(unittest.TestCase):
    """Issue #96 S-M-02：``chat_id``/``thread_id``/``message_id`` 必须原样从
    ``InboundMessage`` 传到 ``AdminRouter.route()``——``suspend``/``resume`` 这类
    写命令要把确认卡片回复到触发命令的那条消息上，这三个字段是唯一的信息来源。
    """

    def setUp(self) -> None:
        self.log = CallLog()
        self.state = FakeState()

    def test_route_receives_the_triggering_messages_chat_thread_and_message_id(
        self,
    ) -> None:
        router = FakeAdminRouter(
            {"ou_admin": AdminRouteOutcome(handled=True, reply_text="已生成待确认操作")}
        )
        pipeline = EventPipeline(
            store=FakeStore(self.state, self.log),
            reactions=FakeReactions(self.log),
            replies=FakeReplies(self.log),
            audit=FakeAudit(self.log),
            admin_router=router,
        )

        pipeline.handle_message(
            message(
                event_id="evt_suspend_1",
                open_id="ou_admin",
                text="/admin suspend ou_target",
                thread_id="thread_xyz",
            )
        )

        self.assertEqual(len(router.calls), 1)
        call = router.calls[0]
        self.assertEqual(call["chat_id"], "oc_1")
        self.assertEqual(call["thread_id"], "thread_xyz")
        self.assertEqual(call["message_id"], "om_evt_suspend_1")

    def test_main_window_message_passes_thread_id_none(self) -> None:
        router = FakeAdminRouter(
            {"ou_admin": AdminRouteOutcome(handled=True, reply_text="ok")}
        )
        pipeline = EventPipeline(
            store=FakeStore(self.state, self.log),
            reactions=FakeReactions(self.log),
            replies=FakeReplies(self.log),
            audit=FakeAudit(self.log),
            admin_router=router,
        )

        pipeline.handle_message(
            message(event_id="evt_suspend_2", open_id="ou_admin", text="/admin suspend ou_target")
        )

        self.assertIsNone(router.calls[0]["thread_id"])


class DelegatedSubjectStructuralExitTests(unittest.TestCase):
    """专用主体结构性出口前置（opus P3-1 修复）：`core/conversation/pipeline.py`
    此前把管理分流嵌在 `NOT_PROVISIONED` 分支内——若专用主体因数据漂移意外获得
    `app_user` 行，`state` 就不再是 `NOT_PROVISIONED`，整段判定被跳过，专用主体
    会直接落入业务队列。修复把专用主体识别移到按用户状态分派**之前**，命中即
    只能进管理命令面或既有确定性拒绝出口，绝无业务路径。

    认领断言：V-管理-24（此前被跨 Story 打破，本组用例是修复后的复证）。
    """

    def setUp(self) -> None:
        self.log = CallLog()
        self.state = FakeState()

    def _pipeline(
        self,
        *,
        admin_router=None,
        onboarding=None,
        innertest_roster_gate=None,
        delegated_subject_open_id: str = "ou_delegated",
    ) -> EventPipeline:
        return EventPipeline(
            store=FakeStore(self.state, self.log),
            reactions=FakeReactions(self.log),
            replies=FakeReplies(self.log),
            audit=FakeAudit(self.log),
            onboarding=onboarding,
            admin_router=admin_router,
            innertest_roster_gate=innertest_roster_gate,
            delegated_subject_open_id=delegated_subject_open_id,
        )

    def test_drifted_app_user_row_still_reaches_the_admin_face_not_the_business_queue(
        self,
    ) -> None:
        """①主体带着 app_user 行仍进管理面，不进业务队列。

        模拟数据漂移：专用主体的 open_id 意外有了一条 `app_user` 行（正常情况下
        结构上不该发生，`V-身份-02`）。命中登记表里的有效管理员条目时，必须仍然
        走管理命令面，而不是被 `state` 已经不是 `NOT_PROVISIONED` 这件事带偏，
        直接当成一个已开通的正常业务用户入队问数。
        """

        self.state.users["ou_delegated"] = provisioned_user(
            open_id="ou_delegated", user_id="usr_delegated"
        )
        router = FakeAdminRouter(
            {
                "ou_delegated": AdminRouteOutcome(
                    handled=True,
                    content_key="admin.help",
                    content_version="internal",
                    reply_text="Lingxi 管理命令：...",
                )
            }
        )
        pipeline = self._pipeline(admin_router=router)

        outcome = pipeline.handle_message(
            message(open_id="ou_delegated", text="/admin help")
        )

        self.assertEqual(outcome.handled_as, HandledAs.COMMAND)
        self.assertEqual(len(router.calls), 1)
        self.assertEqual(router.calls[0]["open_id"], "ou_delegated")
        # 绝无业务路径：没有任何任务被排进队列。
        self.assertEqual(self.state.tasks, [])
        self.assertIn("audit.inbound_event.admin_command", self.log.names())

    def test_not_on_the_innertest_roster_still_gets_the_admin_or_dedicated_exit_reply(
        self,
    ) -> None:
        """②主体不在内测名单时得到管理面/专用出口应答，而不是「内测未开放」。

        顺序要求（派发卡）：主体判定 → （`NOT_PROVISIONED` 时）名单闸 → 开通链。
        专用主体本来就不会出现在人类内测名单里；如果名单闸先判定，会把它错误地
        挡成「内测未开放」，而不是它真正对应的确定性拒绝文案。这里用一个恒为
        `False` 的名单闸复现"名单更严"的场景，证明专用主体判定确实排在名单闸
        之前——不受名单结果影响。
        """

        # 未装配管理路由（或路由判定不通过）：命中主体但没有管理员身份，必须
        # 落到既有确定性拒绝出口，不是名单闸的「内测未开放」。
        onboarding = FakeOnboarding()
        pipeline = self._pipeline(
            admin_router=None,
            onboarding=onboarding,
            innertest_roster_gate=lambda open_id: False,
        )

        outcome = pipeline.handle_message(message(open_id="ou_delegated", text="你好"))

        self.assertEqual(outcome.handled_as, HandledAs.DROPPED)
        # 开通链完全没有被触碰——不是「触发了又被顶回来」，是根本没进去。
        self.assertEqual(onboarding.calls, [])
        sent = self.log.fields("audit.reply.sent")
        self.assertEqual(
            [fields["content_key"] for fields in sent], ["onboarding.delegated_subject"]
        )
        self.assertNotIn("audit.onboarding.innertest_roster_rejected", self.log.names())
        self.assertNotIn("audit.inbound_event.auto_provisioning", self.log.names())

    def test_normal_user_path_makes_no_extra_registry_lookup(self) -> None:
        """③普通用户路径零额外登记表查询（性能面，opus P3-5）。

        专用主体判定比对的是装配期已经解析好的单个 open_id，对着**全体消息**
        都只是一次内存里的字符串比较——不应该让每一条普通用户消息都多出一次
        `AdminRouter.route()` 调用。用两类普通用户分别证明：已开通业务用户
        （既有结构性保证：`router.calls` 恒为空）与未开通陌生人（既有机制下
        本就有一次登记表读取，装配本项新增的 `delegated_subject_open_id` 之后
        次数不得从 1 变成 2）。
        """

        self.state.users["ou_1"] = provisioned_user(open_id="ou_1", user_id="usr_1")
        router = FakeAdminRouter(
            {"ou_1": AdminRouteOutcome(handled=True, reply_text="不应该被看到")}
        )
        pipeline = self._pipeline(admin_router=router)

        provisioned_outcome = pipeline.handle_message(
            message(event_id="evt_normal_business", open_id="ou_1", text="本月销售额")
        )

        self.assertEqual(router.calls, [])
        self.assertEqual(provisioned_outcome.handled_as, HandledAs.TASK_QUEUED)

        # 未开通的陌生人：既有的 NOT_PROVISIONED 内层管理面判定本来就会读一次表
        # （与专用主体判定无关），这里只断言总次数没有从 1 变成 2。
        onboarding = FakeOnboarding()
        pipeline_for_stranger = self._pipeline(admin_router=router, onboarding=onboarding)

        stranger_outcome = pipeline_for_stranger.handle_message(
            message(event_id="evt_normal_stranger", open_id="ou_stranger", text="随便问点什么")
        )

        self.assertEqual(len(router.calls), 1, "专用主体判定不得为普通未开通用户多加一次登记表查询")
        self.assertEqual(stranger_outcome.handled_as, HandledAs.AUTO_PROVISIONING)


def _group_chat_admin_command_payload(*, open_id: str) -> dict:
    """一条形状上"看起来像"管理命令、但发在群聊里的原始事件体。上游
    （``apps/gateway/__init__.py`` 的 ``make_event_handler``，`NonPrivateChatError`
    分支，2026-08-24 落点在该文件第 226 行附近）必须在这条事件到达
    `EventPipeline`/管理路由之前就拒绝它——本文件其余用例全部走
    ``pipeline.handle_message(InboundMessage)``，天然假设"已经是私聊"，验证不到
    这一层，因此这里刻意从**原始事件体**开始，经过真实的 `make_event_handler`。
    """

    return {
        "header": {"event_id": "evt_group_admin", "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": open_id}},
            "message": {
                "message_id": "om_group_admin",
                "chat_id": "oc_some_group",
                "chat_type": "group",
                "message_type": "text",
                "content": '{"text": "/admin help"}',
            },
        },
    }


class NonPrivateChatNeverReachesAdminRoutingTests(unittest.TestCase):
    """群聊防线（C8）：非私聊事件不进管理路由，即便发送者是登记表里当前有效的
    管理员、正文形状看起来完全是一条合法的管理命令。上游过滤
    （`apps/gateway/__init__.py` 的 `NonPrivateChatError` 分支）已经挡住了这个
    场景，这条用例是**防未来重构把这道过滤移走或绕开**的回归哨兵——用真实的
    `make_event_handler` + 真实的 `EventPipeline`（接了一个配置成"这个人是有效
    管理员"的假路由）证明：``AdminCommandRouter.route`` 在这个场景下**一次都
    没被调用过**，不是靠管线内部某个分支恰好返回了拒绝结果。
    """

    def test_a_registered_admins_group_message_never_reaches_the_router(self) -> None:
        log = CallLog()
        state = FakeState()
        admin_open_id = "ou_admin_in_a_group_chat"
        router = FakeAdminRouter(
            {
                admin_open_id: AdminRouteOutcome(
                    handled=True, reply_text="不应该被看到——群聊不该走到这里"
                )
            }
        )
        pipeline = EventPipeline(
            store=FakeStore(state, log),
            reactions=FakeReactions(log),
            replies=FakeReplies(log),
            audit=FakeAudit(log),
            admin_router=router,
        )
        handler = make_event_handler(pipeline, audit=FakeAudit(log))

        handler(_group_chat_admin_command_payload(open_id=admin_open_id))

        self.assertEqual(router.calls, [], "群聊事件绝不能触达管理路由")
        self.assertEqual(log.count("reaction.add"), 0, "群里不得加表情")
        self.assertEqual(log.count("reply.send_text"), 0, "群里不得回复")
        self.assertIn("audit.event.rejected_non_private_chat", log.names())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
