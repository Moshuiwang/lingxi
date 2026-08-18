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
2. **每 UTC 日至多消费一次**（:class:`DailyRefreshBudget`）：一次性令牌被高频消费正是
   2026-08-08 授权码被烧那次事故的形状，一个崩溃重启循环或一个每轮都失败的缺陷都能
   造出这个形状，因此频率上界是守卫而不是优化。
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
        # 今天已经消费过一次续期凭据（持久判据，重启也抹不掉）。
        "refresh_already_consumed_today",
        # 今天已经消费过一次续期凭据（进程内判据，不必去动凭据文件就能拒绝）。
        "daily_refresh_budget_exhausted",
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


def _require_date(day: date) -> date:
    # datetime 是 date 的子类，误传会让"今天"变成一个带时刻的对象，比较时永不相等。
    if not isinstance(day, date) or isinstance(day, datetime):
        raise ValueError("频率上界只接受已经归一到 UTC 的日期")
    return day


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
        """存入一份刚派生出来的令牌；寿命未知时**拒绝缓存**并返回 ``False``。

        寿命未知却照存，等于缓存一个不知道何时过期的令牌：它会在某个说不清的时刻
        开始让花名册读取失败，而报出来的原因会是"花名册读取失败"而不是"令牌供给有
        问题"。拒绝缓存把失败留在这一侧，分类因此仍然准确。

        拒绝时**不动**已经持有的那一份：一次没带寿命的响应不该把手上还新鲜的令牌
        一起作废。
        """

        moment = _require_utc(now, "now")
        if not isinstance(derived, DerivedAccessToken):
            raise TypeError("只接受 DerivedAccessToken，避免明文令牌以裸字符串流转")
        if derived.expires_in is None:
            return False
        self._token = derived.token
        self._expires_at = moment + timedelta(seconds=derived.expires_in)
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


class DailyRefreshBudget:
    """按需刷新的频率上界：每 UTC 日至多一次。

    **记账点是"领取成功"那一刻**，不是"打算试一次"那一刻。两端各有一个坑，这里刻意
    站在中间：

    - 记在**成功之后**：一个每轮都失败的缺陷会每 60 秒去消费一次一次性令牌——那正是
      2026-08-08 授权码被烧那次事故的形状；
    - 记在**尝试之前**：`no_credential_available` / `scheduler_stopping` 这类**可证明
      零消费**的失败也会占掉当日名额。后果很具体：产品负责人早上补完授权，当天全天都会
      被"今天已经换过了"拒绝，而凭据层恰恰刻意让新授权不带消费标记（两路审查交叉确认）。

  领取成功等于凭据文件里的消费标记已经原子置位——从那一刻起这条一次性令牌无论如何都
  回不来了，因此它既是"确实消费了"的最早时刻，也是"零消费"与"已消费"之间唯一说得清的
  分界。领取之后的任何失败（飞书拒绝、结果不明确、写盘失败）都照常记账。

    这是**进程内**的那一半守卫，重启即清零；重启也抹不掉的那一半由凭据文件里的
    ``refresh_consumed_at`` 承担（见 :class:`~lingxi.core.identity.credentials.
    RefreshAlreadyConsumedToday`）。两道都要有：只有进程内那道，崩溃重启循环可以绕过；
    只有文件那道，每一次拒绝都要先去开锁读文件。

    只认 :class:`datetime.date`：**UTC 归一由调用方负责**，因为"今天"这件事必须与凭据
    文件那一侧用同一把尺子量（`_utc_day`），在两个地方各算一次就会在午夜前后错位。
    """

    __slots__ = ("_spent_on",)

    def __init__(self) -> None:
        self._spent_on: date | None = None

    @property
    def spent_on(self) -> date | None:
        return self._spent_on

    def is_spent(self, day: date) -> bool:
        return self._spent_on == _require_date(day)

    def charge(self, day: date) -> None:
        """记下"这一天已经消费过一次"。同日重复调用无副作用。"""

        self._spent_on = _require_date(day)


class RosterAccessTokenProvider:
    """花名册读取的短期令牌供给：``Callable[[], str]``，交给 ``BitableRosterPages``。

    一次调用的判定次序是刻意的：

    1. **手上有新鲜的就直接给**——花名册一轮要读很多页，每页现取一次，不能每页都去
       消费凭据；
    2. 没有或已临期 → 触发一次轮换职责内的受控续期。**频率上界不在这里**：它属于真正
       消费凭据的那一侧（:class:`DailyRefreshBudget` 由凭据轮换职责在领取成功之后记账），
       放在这里会变成"打算试一次就扣一次"，把可证明零消费的失败也算进去；
    3. 续期与落盘全部成功之后，才可能拿到令牌。**顺序不能反**：新的
       ``refresh_token`` 先成功落盘、再交出派生令牌，把"续期成功但写盘失败＝凭据丢失"
       的窗口压到最小且方向安全（落盘失败就不交出，并按凭据丢失风险留痕）。

    失败一律抛 :class:`AccessTokenUnavailable`，**绝不返回空串**：
    ``BitableRosterPages`` 刻意把 provider 自己抛的异常原样上抛而不折成"花名册读取
    失败"，因为拿不到凭据是本侧的授权问题，不是源头异常。日报侧因此按职责级失败隔离
    处理——不注销职责、水位不置位、下一轮再试。

    **异常不带原因链**（``raise ... from None``）：``__cause__`` 里挂着的原始
    transport / provider 异常会在任何一次 traceback 打印时把响应正文乃至令牌带进日志，
    而本类对外承诺的恰恰是"只有分类"。需要排障时给的是净化字段（异常**类名**）。

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
