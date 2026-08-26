"""会话、话题与任务生命周期的纯领域逻辑（代码框架第一节登记的 ``core/conversation/``）。

本包不 import ``adapters/``、``apps/`` 或任何外部 SDK，也不做网络与数据库 I/O：
外部世界经 ``ports`` 里的 ``Protocol`` 注入进来。S4 前半（Issue #57）交付其中的
gateway 侧——事件管线、``/new`` 与 ``/stop``、两小时规则；Agent 会话真正的 resume
行为属 S4 下半。
"""

from __future__ import annotations

from .commands import Command, is_unrecognized_slash_message, parse_command
from .onboarding_recovery import OnboardingReconciler
from .pipeline import BUSY_HINT_TEXT, DEFAULT_WORKER_VERSION, EventPipeline, GatewayTexts
from .ports import (
    ConversationRecord,
    HandledAs,
    InboundMessage,
    OnboardingMessage,
    OnboardingResult,
    OnboardingRunner,
    OnboardingState,
    Outcome,
    PendingOnboarding,
    UserRecord,
    UserState,
)
from .session_window import SESSION_IDLE_WINDOW, should_resume_session

__all__ = [
    "BUSY_HINT_TEXT",
    "Command",
    "ConversationRecord",
    "DEFAULT_WORKER_VERSION",
    "EventPipeline",
    "GatewayTexts",
    "HandledAs",
    "InboundMessage",
    "OnboardingMessage",
    "OnboardingReconciler",
    "OnboardingResult",
    "OnboardingRunner",
    "OnboardingState",
    "Outcome",
    "PendingOnboarding",
    "SESSION_IDLE_WINDOW",
    "UserRecord",
    "UserState",
    "is_unrecognized_slash_message",
    "parse_command",
    "should_resume_session",
]
