"""花名册日报所用**短期令牌供给**的规则（Issue #215 主接线，方案 C）。

本模块只有规则，没有 I/O：真正的续期与落盘由 ``lingxi.apps.scheduler`` 的凭据轮换
职责执行，这里只回答三个问题——**手上这份令牌还能不能用、该不该现在去换一次、换不到
时对外说什么**。这样"按日消费一次性凭据"的全部边界都能在没有数据库、没有网络、没有
真实凭据的 CI 里被完整证伪。

## 为什么需要它

花名册日报**每天**要一次短期 ``access_token``，而它唯一的来源是专用授权主体那条
**一次性** ``refresh_token`` 的续期，后者此前只在有效期 80%（约 5.6 天）才被消费一次。
产品负责人 2026-08-18 裁定接受**按日节奏**：消费频率改为按需（事实上按日），
**凭据轮换职责仍是唯一消费者**——身份边界、scope 与进程数量都不变。

## 三条守住的边界

1. **派生令牌只在进程内**（:class:`DerivedAccessTokenHolder`）：不落盘、不进数据库、
   不进日志与审计。进程重启后为空，由下一次按需刷新重新填充。
2. **每 UTC 日至多消费一次**，判据**只有一份**：凭据文件里的消费标记，由凭据库在
   自己的文件锁内判定（``claim_due(for_supply=True)``）。一次性令牌被高频消费正是
   2026-08-08 授权码被烧那次事故的形状，因此频率上界是守卫而不是优化——但**不能有
   第二份进程内副本**：副本不认识凭据代际，人工重授权换来一条全新的、没有消费标记的
   凭据之后，旧账本会继续把它拒到第二天，与"重授权当天即可恢复"的已接受语义直接冲突。
3. **拿不到令牌就失败关闭**（:class:`AccessTokenUnavailable`）：对外只报**分类**，
   不回显任何令牌、凭据或响应正文，也不留原因链。绝不返回空串或占位符——那会让
   "没有凭据"伪装成"花名册读取失败"，把排障指向错误的地方。

## 三条**已知并接受**的残留（两路审查交叉裁定，不修）

- **人工重授权会重置当日额度**：新授权不带消费标记（凭据层刻意如此），因此补授权当天
  可以再换一次。它需要一次人工授权动作才能发生，人为门槛本身就是保护，接受；
- **"唯一消费者"的源码扫描是绊线不是安全边界**：AST 扫的是几种已知写法，绕开它并不难
  （换个变量名调用、动态取属性）。它的价值在于"有人不经意地加了第二个消费者时会响"，
  不承诺拦住有意为之；
- **"日报发送失败 + 当天进程重启"会丢掉那一天的日报**：重启后再要令牌会被持久上界拒绝。
  维持失败关闭（放宽就等于放宽"每 UTC 日至多一次"），由编排者提请产品负责人知情接受。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

from lingxi.core.identity.credentials import DerivedAccessToken, SecretToken

#: 判定"还能不能用"时预留的安全余量。飞书返回的是发放那一刻的寿命，而令牌要经过
#: 一整轮分页读取；卡着过期点交出去，等于把失败推给读到一半的那一页。
DEFAULT_ACCESS_TOKEN_SAFETY_MARGIN = timedelta(minutes=5)

#: 供给失败的**全部**分类。刻意用固定词表而不是自由文本：分类会进审计与异常正文，
#: 一个 f-string 就足以把令牌值、响应体或群 ID 带出去（`V-花名册-33` 的同一条理由）。
#: 新增分类要同时想清楚"它会出现在审计里"这件事，因此必须显式加进本表。
SUPPLY_FAILURE_REASONS = frozenset(
    {
        # 进程正在停止：停止之后不再开启任何一次续期（半途中断的续期等于凭据丢失）。
        "scheduler_stopping",
        # 凭据库里没有可领取的凭据（未授权、已撤销、或正被另一条链消费中）。
        "no_credential_available",
        # 今天已经消费过一次续期凭据。判据在凭据文件里、由凭据库在锁内判定，
        # 因此进程重启、崩溃重启循环与第二个实例都绕不过它。
        "refresh_already_consumed_today",
        # 飞书明确拒绝了这次续期。
        "refresh_failed",
        # 这次续期结果不明确（超时、连接中断、响应不完整）。
        "refresh_indeterminate",
        # 续期成功但新凭据没能落盘：**不交出令牌**，并按凭据丢失风险响亮留痕。
        "credential_persist_failed",
        # 续期成功、凭据已落盘，但派生令牌不可用（飞书未返回寿命，或刚拿到就已临期）。
        "derived_token_unusable",
        # 注入的续期实现抛了本模块不认识的异常：只记这一个分类，不记异常正文。
        "refresh_error",
    }
)


class AuditSink(Protocol):
    """审计出口。与 ``core/alerting.py``、``apps/scheduler`` 的同名 Protocol 结构一致。"""

    def record(self, action: str, /, **fields: object) -> None: ...


class AccessTokenUnavailable(RuntimeError):
    """短期令牌供给失败。

    ``reason`` 必须取自 :data:`SUPPLY_FAILURE_REASONS`——**构造期就校验**，因此
    "异常正文里不会出现令牌值"是一条结构事实，不是靠调用点自觉。
    """

    def __init__(self, reason: str) -> None:
        if reason not in SUPPLY_FAILURE_REASONS:
            raise ValueError("短期令牌供给的失败分类必须取自固定词表（不回显收到的值）")
        super().__init__(reason)
        self.reason = reason


def _require_utc(moment: datetime, name: str) -> datetime:
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{name} 必须是带时区的 UTC 时间")
    return moment.astimezone(timezone.utc)


class DerivedAccessTokenHolder:
    """派生短期令牌的**进程内**持有者。

    只有两个动作：轮换成功后存进来、供给方按当前时刻取出来。没有任何持久化路径——
    重启即空，这是刻意的：短期令牌落盘会给"凭据不进数据库、不进用户环境"的产品合同
    开一个新口子，而重新取一份的代价只是当天的一次按需刷新。
    """

    __slots__ = ("_token", "_expires_at", "_safety_margin")

    def __init__(self, *, safety_margin: timedelta = DEFAULT_ACCESS_TOKEN_SAFETY_MARGIN) -> None:
        if not isinstance(safety_margin, timedelta) or safety_margin < timedelta(0):
            raise ValueError("短期令牌的安全余量必须是非负的时间长度")
        self._token: SecretToken | None = None
        self._expires_at: datetime | None = None
        self._safety_margin = safety_margin

    @property
    def safety_margin(self) -> timedelta:
        return self._safety_margin

    @property
    def has_token(self) -> bool:
        """是否持有令牌。**只回答有无，不回答值**，也不判断新鲜与否。"""

        return self._token is not None

    def store(self, derived: DerivedAccessToken, *, now: datetime) -> bool:
        """存入一份刚派生出来的令牌；**存不成一份马上就能用的**就返回 ``False``。

        两种存不成的情况，处理相同：

        - **寿命未知**：缓存一个不知道何时过期的令牌，等于把失败推迟到某个说不清的
          时刻，而那时报出来的原因会是"花名册读取失败"而不是"令牌供给有问题"；
        - **一到手就已经临期**（剩余寿命不足安全余量）：存进去也一次都取不出来，
          ``fresh()`` 会立刻判它不新鲜。返回"成功"会让调用方以为拿到了令牌，
          于是"这一代令牌不可用"的记号被错误地清掉，真实原因再也不会出现在审计里。

        返回值的含义因此是**能不能用**，不是"有没有写进去"：调用方拿它当作
        "这一代派生令牌可用与否"的判据（收口轮 P2-c①）。

        拒绝时**不动**已经持有的那一份：一次没带寿命的响应不该把手上还新鲜的令牌
        一起作废。
        """

        moment = _require_utc(now, "now")
        if not isinstance(derived, DerivedAccessToken):
            raise TypeError("只接受 DerivedAccessToken，避免明文令牌以裸字符串流转")
        if derived.expires_in is None:
            return False
        expires_at = moment + timedelta(seconds=derived.expires_in)
        if moment + self._safety_margin >= expires_at:
            return False
        self._token = derived.token
        self._expires_at = expires_at
        return True

    def fresh(self, *, now: datetime) -> SecretToken | None:
        """取出**仍然新鲜**的令牌；没有或已临期时返回 ``None``。

        临期按安全余量提前判死（默认 5 分钟），不是等到过期那一刻。
        """

        moment = _require_utc(now, "now")
        if self._token is None or self._expires_at is None:
            return None
        if moment + self._safety_margin >= self._expires_at:
            return None
        return self._token

    def clear(self) -> None:
        self._token = None
        self._expires_at = None

    def __repr__(self) -> str:  # pragma: no cover - 由 HolderTest 的形状断言覆盖
        return f"DerivedAccessTokenHolder(has_token={self._token is not None})"


class RosterAccessTokenProvider:
    """花名册读取的短期令牌供给：``Callable[[], str]``，交给 ``BitableRosterPages``。

    一次调用的判定次序是刻意的：

    1. **手上有新鲜的就直接给**——花名册一轮要读很多页，每页现取一次，不能每页都去
       消费凭据；
    2. 没有或已临期 → 触发一次轮换职责内的受控续期。**频率上界不在这里**：它属于凭据
       文件那一侧（凭据库在锁内判"今天这条凭据是不是已经被消费过一次"），放在这里会
       变成"打算试一次就扣一次"，把可证明零消费的失败也算进去，还会认不出人工重授权
       换来的新凭据；
    3. 续期与落盘全部成功之后，才可能拿到令牌。**顺序不能反**：新的
       ``refresh_token`` 先成功落盘、再交出派生令牌，把"续期成功但写盘失败＝凭据丢失"
       的窗口压到最小且方向安全（落盘失败就不交出，并按凭据丢失风险留痕）。

    失败一律抛 :class:`AccessTokenUnavailable`，**绝不返回空串**：
    ``BitableRosterPages`` 刻意把 provider 自己抛的异常原样上抛而不折成"花名册读取
    失败"，因为拿不到凭据是本侧的授权问题，不是源头异常。日报侧因此按职责级失败隔离
    处理——不注销职责、水位不置位、下一轮再试。

    **异常不展示原始异常**（``raise ... from None``）：原始 transport / provider 异常
    的正文可能带响应体乃至令牌，而标准 traceback 会把它一路打印出来。``from None``
    置上 ``__suppress_context__``，标准 traceback 因此不再展示它——**注意它仍然挂在
    对象的 ``__context__`` 上**（Python 的语义如此），只是不进打印路径；对外承诺的是
    "不展示"，不是"对象里没有"。需要排障时给的是净化字段（异常**类名**）。

    审计**只记分类**，且同一 UTC 日同一分类只记一条：拒绝会在每一轮定时循环里重复
    发生（默认 60 秒一轮），逐次记会把审计淹掉。
    """

    def __init__(
        self,
        *,
        holder: DerivedAccessTokenHolder,
        refresh: Callable[[], None],
        audit: AuditSink | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(refresh):
            raise ValueError("refresh 必须是执行一次受控续期的可调用对象")
        self._holder = holder
        self._refresh = refresh
        self._audit = audit
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._audited_on: date | None = None
        self._audited_reasons: set[str] = set()

    def __call__(self) -> str:
        now = _require_utc(self._clock(), "now")
        token = self._holder.fresh(now=now)
        if token is not None:
            return token.reveal()

        try:
            self._refresh()
        except AccessTokenUnavailable as error:
            self._record(error.reason, now)
            raise
        except Exception as error:  # noqa: BLE001 - 只保留分类，异常正文可能带响应内容
            self._record("refresh_error", now, error_type=type(error).__name__)
            raise AccessTokenUnavailable("refresh_error") from None

        moment = _require_utc(self._clock(), "now")
        token = self._holder.fresh(now=moment)
        if token is None:
            # 续期与落盘都成功了，只是这份派生令牌用不了（寿命未知，或刚拿到就临期）。
            # 凭据没有丢，因此**不撤销**；今天的预算已经用掉，明天再试。
            raise self._unavailable("derived_token_unusable", moment)
        self._record_success(moment)
        return token.reveal()

    # ---- 内部 -------------------------------------------------------------

    def _unavailable(self, reason: str, now: datetime) -> AccessTokenUnavailable:
        self._record(reason, now)
        return AccessTokenUnavailable(reason)

    def _record(self, reason: str, now: datetime, **sanitized: str) -> None:
        """记一条失败审计；同一天同一分类只记一条。

        ``sanitized`` 只接受已经确认不含任何值的补充字段（目前只有异常**类名**）。
        """

        today = now.date()
        if self._audited_on != today:
            self._audited_on = today
            self._audited_reasons = set()
        if reason in self._audited_reasons:
            return
        self._audited_reasons.add(reason)
        if self._audit is not None:
            # 只有分类与日期。令牌、凭据、响应正文一个字节都不进审计。
            self._audit.record(
                "roster_access_token.unavailable",
                reason=reason,
                report_date=today.isoformat(),
                **sanitized,
            )

    def _record_success(self, now: datetime) -> None:
        if self._audit is not None:
            self._audit.record(
                "roster_access_token.refreshed",
                mode="on_demand",
                report_date=now.date().isoformat(),
            )
