"""「四达文档会议助手」专用授权凭据的生命周期规则。

本模块只有规则，没有 I/O：加密存取在 :mod:`lingxi.adapters.delegated_credentials`，
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


class RefreshAlreadyConsumedToday(RuntimeError):
    """今天已经消费过一次续期凭据，本次领取被拒。

    这是**每日频率上界唯一的权威判据**（Issue #215）：日报改成按日消费一次性
    ``refresh_token`` 之后，"每 UTC 日至多一次"必须有一个进程重启也抹不掉的判据，
    否则一个崩溃重启循环会每分钟消费一次——一次性令牌被高频消费正是 2026-08-08
    授权码被烧那次事故的形状。判定发生在凭据文件的锁内、用锁内的当前时刻，因此
    等锁跨过 UTC 午夜也不会错位，而且**只认凭据自己**：换了新凭据（人工重授权）就
    没有消费标记，当天立刻可以再换一次，这正是已接受的语义。

    与"没有可领取的凭据"刻意分成两件事：前者是"今天已经换过了"，后者是"凭据没了"。
    压成同一个 ``None`` 会让审计指向错误的地方（去查凭据丢失，而其实一切正常）。

    ``consumed_at`` 是被拒时凭据上记着的那个**消费时刻**（不是本次领取的时刻）。
    它由锁内生成、随凭据落盘，因此可以唯一地指认"是哪一次续期消费掉了今天的额度"——
    调用方据此判断自己手上那条"这一代令牌不可用"的记号还算不算数。异常正文只有一句
    固定文案，不含任何凭据值。
    """

    def __init__(self, *, consumed_at: datetime) -> None:
        super().__init__("今天已经消费过一次续期凭据，本次领取被拒")
        self.consumed_at = consumed_at


@dataclass(frozen=True)
class DerivedAccessToken:
    """一次续期顺带派生出来的**短期** ``access_token`` 及其寿命。

    只在进程内存活：不落盘、不进数据库、不进日志与审计（产品合同「凭据不进代码、
    日志、数据库、用户环境」）。带上 ``expires_in`` 是因为"缓存下来的令牌现在还能不能
    用"没有别的判据——飞书返回的寿命此前从未被读取过，把它丢掉就只能靠"用坏了再说"，
    而那时报出来的会是一次花名册读取失败，指向错误的地方。

    ``expires_in`` 允许为 ``None``：飞书没给寿命时**不把整轮续期判成失败**。续期成功
    这件事已经发生，新的 ``refresh_token`` 必须照常落盘——为一个附带字段把一条一次性
    凭据判死，方向是反的。寿命未知的派生令牌由持有者拒绝缓存，失败因此响亮地留在
    令牌供给这一侧。
    """

    token: SecretToken
    expires_in: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.token, SecretToken):
            raise TypeError("access_token 必须包在 SecretToken 里，避免明文进入日志")
        if self.expires_in is None:
            return
        if not isinstance(self.expires_in, int) or isinstance(self.expires_in, bool):
            raise ValueError("短期令牌有效期必须是正整数秒或未知")
        if self.expires_in <= 0:
            raise ValueError("短期令牌有效期必须是正整数秒或未知")

    @property
    def lifetime_known(self) -> bool:
        return self.expires_in is not None


@dataclass(frozen=True)
class AuthorizationGrant:
    """飞书一次授权或续期返回的、需要长期持有的那一部分。

    刻意**不含** ``access_token``：短期令牌只存在于一次 API 调用中，既不落库
    也不进入本类型，免得它顺着凭据一起被持久化。派生短期令牌的进程内载体是
    :class:`DerivedAccessToken`，它同样不进任何持久载体。
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


class CredentialSaveOutcome(str, Enum):
    """一次"把新凭据写回去"的结果。**三态，不是布尔**。

    压成布尔正是 Issue #215 一级 / 二级审查交叉抓到的 P1：``SUPERSEDED``（世代不符或
    主体登记 CAS 未通过）与 ``SAVED`` 都被折成"真"，因为对轮换收尾来说两者都算"已妥善
    收尾、不必撤销"。但对**派生短期令牌**来说它们是相反的两件事——``SUPERSEDED`` 意味着
    新的 ``refresh_token`` 被丢弃、本链已死（旧的那条已经在飞书那边被消费掉），此时把
    派生令牌交出去，日报会照常工作到令牌过期为止，把"凭据丢失"整整盖住那么久。

    - ``SAVED``：新凭据确实落盘了，本链继续有效；
    - ``SUPERSEDED``：期间发生了新授权，本链结果作废——**不撤销**（不能把新凭据连带
      删掉），但也**什么都不交出**；
    - ``FAILED``：写不进去。旧凭据已被飞书作废，按不可恢复撤销并要求人工重新授权。
    """

    SAVED = "saved"
    SUPERSEDED = "superseded"
    FAILED = "failed"


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
