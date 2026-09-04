"""会话、话题与任务生命周期的纯领域逻辑（代码框架第一节登记的 ``core/conversation/``）。

本包不 import ``adapters/``、``apps/`` 或任何外部 SDK，也不做网络与数据库 I/O：
外部世界经 ``ports`` 里的 ``Protocol`` 注入进来。本包交付的是 gateway 侧——事件
管线、``/new`` 与 ``/stop``、两小时规则；Agent 会话真正的 resume 行为在别处。
"""

from __future__ import annotations

from .commands import Command, is_unrecognized_slash_message, parse_command
from .onboarding_recovery import OnboardingReconciler
from .pipeline import (
    BUSY_HINT_TEXT,
    DEFAULT_WORKER_VERSION,
    DispatchGates,
    EventPipeline,
    GatewayTexts,
)
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
    PendingPreprovisionNotice,
    UserRecord,
    UserState,
)
from .session_window import SESSION_IDLE_WINDOW, should_resume_session

__all__ = [
    "BUSY_HINT_TEXT",
    "Command",
    "ConversationRecord",
    "DEFAULT_WORKER_VERSION",
    "DispatchGates",
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
    "PendingPreprovisionNotice",
    "SESSION_IDLE_WINDOW",
    "UserRecord",
    "UserState",
    "is_unrecognized_slash_message",
    "parse_command",
    "should_resume_session",
]
