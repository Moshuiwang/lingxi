"""「四达文档会议助手」专用授权凭据的生命周期规则。

本模块只有规则，没有 I/O：加密存取在 :mod:`lingxi.adapters.postgres_credentials`，
向飞书换新凭据在 :mod:`lingxi.adapters.feishu_directory`，定时扫描在
``lingxi.apps.scheduler``。这样"什么时候该轮换、失败了怎么办"可以在没有数据库、
没有网络、没有真实凭据的 CI 里被完整证伪。

三条规则来自 2026-08-05 在 `tz` 的真实复验（[Issue #16 复验记录]
(https://github.com/Moshuiwang/lingxi/issues/16#issuecomment-5188063325)）：

1. 轮换点按飞书**实际返回**的 ``refresh_token_expires_in`` 的 80% 计算，不写死周期；
2. ``refresh_token`` 一次性有效，因此续期结果不明确时**不得重放旧凭据**；
3. 轮换失败或结果不明确一律撤销，要求专用授权用户重新授权。

产品合同要求凭据不进代码、日志、数据库明文与用户环境，因此明文一律包在
:class:`SecretToken` 里：它的 ``repr`` / ``str`` / ``format`` 都不吐明文，
只有显式 :meth:`SecretToken.reveal` 才拿得到。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

# 轮换点相对于飞书返回有效期的比例。2026-08-05 实测有效期 7 天、计划轮换点
# 在发放后 5.6 天，与本比例吻合。改这个数等于改一次真实验证过的行为。
ROTATION_TRIGGER_RATIO = 0.8


class SecretToken:
    """凭据明文的进程内载体：不可打印、不可格式化，只能显式 reveal。

    不用 ``str`` 直接传递的理由很具体：日志、异常、``repr`` 和 dataclass 的
    默认 ``repr`` 都会把普通字符串原样吐出来，而"凭据不进日志"是产品合同的
    硬要求，靠代码评审保证不住。
    """

    __slots__ = ("_value",)

    _MASK = "SecretToken(<已脱敏>)"

    def __init__(self, value: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("凭据明文不能为空")
        self._value = value

    def reveal(self) -> str:
        """取出明文。调用点必须只把它交给外部平台或加密函数，不得落库、落日志。"""

        return self._value

    def __repr__(self) -> str:
        return self._MASK

    def __str__(self) -> str:
        return self._MASK

    def __format__(self, _format_spec: str) -> str:
        return self._MASK


@dataclass(frozen=True)
class AuthorizationGrant:
    """飞书一次授权或续期返回的、需要长期持有的那一部分。

    刻意**不含** ``access_token``：短期令牌只存在于一次 API 调用中，既不落库
    也不进入本类型，免得它顺着凭据一起被持久化。
    """

    refresh_token: SecretToken
    refresh_token_expires_in: int
    scope: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.refresh_token, SecretToken):
            raise TypeError("refresh_token 必须包在 SecretToken 里，避免明文进入日志")
        if not isinstance(self.refresh_token_expires_in, int) or isinstance(self.refresh_token_expires_in, bool):
            raise ValueError("飞书未返回有效的续期凭据有效期")
        if self.refresh_token_expires_in <= 0:
            raise ValueError("飞书未返回有效的续期凭据有效期")


class CredentialState(str, Enum):
    ACTIVE = "active"
    DUE = "due"
    EXPIRED = "expired"


class RefreshOutcome(str, Enum):
    """一次续期尝试的结果。刻意区分"失败"与"结果不明确"，两者处理相同但审计不同。"""

    ROTATED = "rotated"
    FAILED = "failed"
    INDETERMINATE = "indeterminate"


class CredentialAction(str, Enum):
    ROTATE = "rotate"
    REVOKE = "revoke"


def _require_aware(moment: datetime, name: str) -> datetime:
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{name} 必须是带时区的 UTC 时间")
    return moment


def rotation_deadline(issued_at: datetime, refresh_token_expires_in: int) -> datetime:
    """凭据应当被轮换的时刻。

    取 ``有效期 × 80%`` 与 ``有效期 − 1 秒`` 中较小的那个：比例保证留出足够的
    重试窗口，减一秒保证极短有效期下轮换点仍严格早于失效点。
    """

    _require_aware(issued_at, "issued_at")
    if not isinstance(refresh_token_expires_in, int) or refresh_token_expires_in <= 0:
        raise ValueError("飞书未返回有效的续期凭据有效期")
    seconds = max(0, min(refresh_token_expires_in - 1, int(refresh_token_expires_in * ROTATION_TRIGGER_RATIO)))
    return issued_at + timedelta(seconds=seconds)


def expiry_moment(issued_at: datetime, refresh_token_expires_in: int) -> datetime:
    """凭据的失效时刻，同样完全跟随飞书返回值。"""

    _require_aware(issued_at, "issued_at")
    if not isinstance(refresh_token_expires_in, int) or refresh_token_expires_in <= 0:
        raise ValueError("飞书未返回有效的续期凭据有效期")
    return issued_at + timedelta(seconds=refresh_token_expires_in)


def credential_state(*, refresh_at: datetime, expires_at: datetime, now: datetime) -> CredentialState:
    """按两个已存时刻判定当前状态。失效优先于到期轮换，避免重放死凭据。"""

    _require_aware(refresh_at, "refresh_at")
    _require_aware(expires_at, "expires_at")
    _require_aware(now, "now")
    if now >= expires_at:
        return CredentialState.EXPIRED
    if now >= refresh_at:
        return CredentialState.DUE
    return CredentialState.ACTIVE


def decide_after_refresh(outcome: RefreshOutcome) -> CredentialAction:
    """续期之后该做什么。

    只有明确成功才写入新凭据；其余一律撤销。``refresh_token`` 单次有效，
    "再试一次旧的"在飞书那边等价于用一个已经作废的凭据，且会让下一轮扫描
    继续拿到同一条无效记录。
    """

    return CredentialAction.ROTATE if outcome is RefreshOutcome.ROTATED else CredentialAction.REVOKE
