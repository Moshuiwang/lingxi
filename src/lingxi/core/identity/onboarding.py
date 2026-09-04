"""首次开通的纯领域逻辑。

本模块故意不调用飞书、数据库或问数 MCP。外部适配器负责把真实事件和
授权回调转为这里的输入；这里负责保证用户在确认前不会被建档或进入问数。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ResponseKind(str, Enum):
    """一次开通交互该回什么。"""

    FULL_GUIDE = "full_guide"
    SHORT_GUIDE = "short_guide"
    AUTHORIZATION_REQUIRED = "authorization_required"
    AUTHORIZATION_CANCELLED = "authorization_cancelled"
    IDENTITY_CONFIRMED = "identity_confirmed"


@dataclass(frozen=True)
class OnboardingResponse:
    """一次交互该给用户看什么。"""

    kind: ResponseKind


@dataclass(frozen=True)
class IdentityProfile:
    """授权成功后由飞书返回、且首期身份关联所必需的资料。"""

    open_id: str
    user_id: str
    union_id: str
    display_name: str
    department: str | None
    tenant_key: str | None
    display_name_locale: str | None


@dataclass(frozen=True)
class AppUser:
    """统一用户记录的最小投影。"""

    id: str
    profile: IdentityProfile
    provisioning_state: str = "matching"


class InMemoryOnboardingStore:
    """测试用存储，明确区分引导偏好与统一用户身份记录。"""

    def __init__(self) -> None:
        """初始化空存储。"""
        self._dismissed_guides: set[str] = set()
        self._users_by_open_id: dict[str, AppUser] = {}
        self._authorization_events: dict[str, str] = {}
        self._business_messages: list[str] = []

    def dismiss_guide(self, open_id: str) -> None:
        """记下该用户已经关闭过引导。"""
        self._dismissed_guides.add(open_id)

    def guide_was_dismissed(self, open_id: str) -> bool:
        """该用户是否已经关闭过引导。"""
        return open_id in self._dismissed_guides

    def clear_dismissed_guide(self, open_id: str) -> None:
        """清掉该用户的引导关闭标记。"""
        self._dismissed_guides.discard(open_id)

    def upsert_identity(self, profile: IdentityProfile) -> AppUser:
        """按 ``open_id`` 新建或更新统一用户身份记录。"""
        existing = self._users_by_open_id.get(profile.open_id)
        user = AppUser(
            id=existing.id if existing else f"usr_test_{len(self._users_by_open_id) + 1:02d}",
            profile=profile,
        )
        self._users_by_open_id[profile.open_id] = user
        return user

    def get_authorization_event_user_id(self, event_id: str) -> str | None:
        """这次授权事件此前是否已经处理过，处理过则返回对应用户标识。"""
        return self._authorization_events.get(event_id)

    def record_authorization_event(self, event_id: str, user_id: str) -> None:
        """记下这次授权事件已经处理过、对应哪个用户。"""
        self._authorization_events[event_id] = user_id

    def get_user_by_id(self, user_id: str) -> AppUser | None:
        """按内部标识取用户，不存在则 ``None``。"""
        return next((user for user in self._users_by_open_id.values() if user.id == user_id), None)

    def get_user(self, open_id: str) -> AppUser | None:
        """按 ``open_id`` 取用户，不存在则 ``None``。"""
        return self._users_by_open_id.get(open_id)

    def user_count(self) -> int:
        """当前已记录的用户数。"""
        return len(self._users_by_open_id)

    def saved_business_messages(self) -> tuple[str, ...]:
        """测试断言用：已保存的业务消息快照。"""
        return tuple(self._business_messages)


class OnboardingService:
    """落实用户可见的开通边界。"""

    _START_CONFIRMATIONS = frozenset(
        {
            "开始使用",
            "我想开始使用",
            "我要开始使用",
            "好的开始使用",
            "好开始使用",
            "确认开始使用",
            "我想使用",
            "我要使用",
            "确认使用",
            "确认",
        }
    )

    def __init__(self, store: InMemoryOnboardingStore) -> None:
        """装配开通服务，存储由调用方注入。"""
        self._store = store

    def receive_message(self, open_id: str, _content: str) -> OnboardingResponse:
        """未开通用户的任何消息均不进入业务历史。"""
        if self._store.get_user(open_id) is not None:
            return OnboardingResponse(ResponseKind.IDENTITY_CONFIRMED)
        if self._store.guide_was_dismissed(open_id):
            return OnboardingResponse(ResponseKind.SHORT_GUIDE)
        return OnboardingResponse(ResponseKind.FULL_GUIDE)

    def decline_guide(self, open_id: str) -> OnboardingResponse:
        """记录用户不想立即开通的偏好，但不创建统一用户身份记录。"""
        self._store.dismiss_guide(open_id)
        return OnboardingResponse(ResponseKind.SHORT_GUIDE)

    def confirm_start(self, open_id: str, text: str) -> OnboardingResponse:
        """仅明确确认文本才开始授权；其他文本继续展示引导。"""
        if not self._is_start_confirmation(text):
            return self.receive_message(open_id, text)
        self._store.clear_dismissed_guide(open_id)
        return OnboardingResponse(ResponseKind.AUTHORIZATION_REQUIRED)

    def authorization_cancelled(self, open_id: str) -> OnboardingResponse:
        """授权取消不写入任何不完整身份资料。"""
        if self._store.get_user(open_id) is None:
            return OnboardingResponse(ResponseKind.AUTHORIZATION_CANCELLED)
        return OnboardingResponse(ResponseKind.IDENTITY_CONFIRMED)

    def authorization_succeeded(
        self, event_id: str, profile: IdentityProfile
    ) -> OnboardingResponse:
        """授权完成后再一次性写入完整身份资料，重复回调只更新同一用户。"""
        recorded_user_id = self._store.get_authorization_event_user_id(event_id)
        if recorded_user_id is not None:
            return OnboardingResponse(ResponseKind.IDENTITY_CONFIRMED)
        self._validate_profile(profile)
        user = self._store.upsert_identity(profile)
        self._store.record_authorization_event(event_id, user.id)
        self._store.clear_dismissed_guide(profile.open_id)
        return OnboardingResponse(ResponseKind.IDENTITY_CONFIRMED)

    @classmethod
    def _is_start_confirmation(cls, text: str) -> bool:
        compact = "".join(
            character for character in text.strip() if character not in " ，。！？、.!?\t\n"
        )
        compact = compact.translate(
            str.maketrans(
                {"開": "开", "始": "始", "確": "确", "認": "认", "想": "想", "使": "使", "用": "用"}
            )
        )
        return compact in cls._START_CONFIRMATIONS

    @staticmethod
    def _validate_profile(profile: IdentityProfile) -> None:
        required_values = {
            "open_id": profile.open_id,
            "user_id": profile.user_id,
            "union_id": profile.union_id,
            "display_name": profile.display_name,
        }
        missing = [name for name, value in required_values.items() if not value.strip()]
        if missing:
            joined = ", ".join(missing)
            raise ValueError(f"授权资料不完整：{joined}")
