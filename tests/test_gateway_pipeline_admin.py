"""管理命令面在 gateway 管线里的分流点（Issue #95 S-M-01）。

认领断言：V-管理-21（私聊分流：登记表有效条目改道进入管理命令面，不进入自动开通）、
V-管理-24（登记表内无有效条目的发送者维持原行为——含"未装配路由"与"路由判定拒绝"
两种落回既有分支的情形）、V-管理-25（分流范围precisely限定于私聊文本消息，非文本
消息与已开通用户完全不触达管理路由，业务用户路径零改动）。

只测试**管线在给定 ``AdminRouter`` 结果时如何分流**，不测试路由本身的判定逻辑——
后者（默认拒绝、角色判定、命令解析、审计）见 ``test_admin_router.py``；真实数据库
判定见 ``test_admin_registry_postgres.py``。三层合起来才是完整证据链，本文件只覆盖
管线这一层的接线是否正确，因此这里用一个可编程的假 ``AdminRouter``，不连真实登记表。
"""

from __future__ import annotations

import unittest

from gateway_fakes import CallLog, FakeAudit, FakeOnboarding, FakeReactions, FakeReplies, FakeState, FakeStore, provisioned_user
from lingxi.core.admin.router import AdminRouteOutcome
from lingxi.core.conversation import EventPipeline
from lingxi.core.conversation.ports import HandledAs, OnboardingResult, OnboardingState
from test_gateway_pipeline import message


class FakeAdminRouter:
    """可编程的管理路由假实现：按 ``open_id`` 查表返回预设结论，记录每次调用。"""

    def __init__(self, outcomes: dict[str, AdminRouteOutcome] | None = None) -> None:
        self.calls: list[dict[str, str]] = []
        self._outcomes = outcomes or {}

    def route(self, *, open_id: str, text: str, trace_id: str) -> AdminRouteOutcome:
        self.calls.append({"open_id": open_id, "text": text, "trace_id": trace_id})
        return self._outcomes.get(open_id, AdminRouteOutcome(handled=False))


class AdminRoutingPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.log = CallLog()
        self.state = FakeState()
        # 唯一一个已开通用户，用来证明已开通用户完全不触达管理路由（`ou_1`）。
        self.state.users["ou_1"] = provisioned_user(open_id="ou_1", user_id="usr_1")

    def _pipeline(self, *, admin_router, onboarding=None) -> EventPipeline:
        return EventPipeline(
            store=FakeStore(self.state, self.log),
            reactions=FakeReactions(self.log),
            replies=FakeReplies(self.log),
            audit=FakeAudit(self.log),
            onboarding=onboarding,
            admin_router=admin_router,
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
        # 未开通用户的消息类型不做二次过滤，见 pipeline.py 第 4 步注释）。
        self.assertEqual(outcome.handled_as, HandledAs.AUTO_PROVISIONING)

    def test_already_provisioned_user_never_reaches_admin_router(self) -> None:
        """已开通业务用户完全不触达管理路由——分流点只在 NOT_PROVISIONED 分支内，
        这是"不改变业务用户开通/问数行为"的结构性保证，不依赖路由自身的判断。"""

        router = FakeAdminRouter(
            {"ou_1": AdminRouteOutcome(handled=True, reply_text="不应该被看到")}
        )
        pipeline = self._pipeline(admin_router=router)

        outcome = pipeline.handle_message(message(open_id="ou_1", text="本月销售额"))

        self.assertEqual(router.calls, [])
        self.assertEqual(outcome.handled_as, HandledAs.TASK_QUEUED)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
