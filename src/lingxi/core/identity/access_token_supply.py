"""花名册日报所用**短期令牌供给**的规则（Issue #215 主接线，方案 C；Issue #276 改
频率上界为最小间隔 + 每日次数）。

本模块只有规则，没有 I/O：真正的续期与落盘由 ``lingxi.apps.scheduler`` 的凭据轮换
职责执行，这里只回答三个问题——**手上这份令牌还能不能用、该不该现在去换一次、换不到
时对外说什么**。这样"按需消费一次性凭据"的全部边界都能在没有数据库、没有网络、没有
真实凭据的 CI 里被完整证伪。

## 为什么需要它

花名册日报**每天**要多次短期 ``access_token``（令牌寿命约 2 小时，需要持续保鲜），
而它唯一的来源是专用授权主体那条**一次性** ``refresh_token`` 的续期，后者此前只在
有效期 80%（约 5.6 天）才被消费一次。产品负责人 2026-08-18 裁定接受**按需节奏**：
消费频率改为按需（事实上一天多次），**凭据轮换职责仍是唯一消费者**——身份边界、
scope 与进程数量都不变。

## 三条守住的边界

1. **派生令牌只在进程内**（:class:`DerivedAccessTokenHolder`）：不落盘、不进数据库、
   不进日志与审计。进程重启后为空，由下一次按需刷新重新填充。
2. **两道频率上界**（Issue #276，产品负责人 2026-08-21 裁定）——两次消费的最小间隔
   （默认 5 分钟）与每 UTC 日消费次数上界（默认 100 次），判据**只有一份**：凭据
   文件里的 ``refresh_consumed_at`` / ``refresh_consumed_count``，由凭据库在自己的
   文件锁内判定（``claim_due(for_supply=True)``，见该方法 docstring）。**不能有
   第二份进程内副本**：副本不认识凭据代际，人工重授权换来一条全新的、没有消费标记的
   凭据之后，旧账本会继续把它拒到第二天，与"重授权当天即可恢复"的已接受语义直接冲突。

   这条边界此前的形态是"每 UTC 日至多消费一次"，措辞曾把它与下一条"唯一消费者"
   捆在同一句话里——**这是本次被推翻、且必须讲清楚的地方**：2026-08-08 授权码被烧
   那次事故的形状是**两个客户端抢占同一条通道**（一个临时诊断进程与正式入口抢占
   同一条 OAuth Bridge），不是"换取太频繁"；把两者绑在一句话里，会让下一个人在放宽
   频率上界时误以为连"唯一消费者"也可以一并放宽。两条边界因此在本文档里分开表述：
   "唯一消费者"（本条第 2 点开头）防的是"通道被第二个客户端抢占"，与消费快慢无关，
   **本次不动**；频率上界防的是"同一个（唯一的）消费者自己失控地高频消费"——一个
   崩溃重启循环会让唯一消费者本身变成那个高频源，这才是频率上界要挡的东西，且它
   本来就没有必要收紧到"一天一次"，那是自己给自己加的限制。
3. **拿不到令牌就失败关闭**（:class:`AccessTokenUnavailable`）：对外只报**分类**，
   不回显任何令牌、凭据或响应正文，也不留原因链。绝不返回空串或占位符——那会让
   "没有凭据"伪装成"花名册读取失败"，把排障指向错误的地方。

## 已知并接受的残留（两路审查交叉裁定，不修）

- **人工重授权会重置频率上界**：新授权不带消费标记与计数（凭据层刻意如此），因此
  补授权当天立刻可以再换。它需要一次人工授权动作才能发生，人为门槛本身就是保护，接受；
- **"唯一消费者"的源码扫描是绊线不是安全边界**：AST 扫的是几种已知写法，绕开它并不难
  （换个变量名调用、动态取属性）。它的价值在于"有人不经意地加了第二个消费者时会响"，
  不承诺拦住有意为之。

以下一条随 Issue #276 解除**已经消解**，不再是残留（收口轮核实结论，[#215 登记]
(https://github.com/Moshuiwang/lingxi/issues/215)）：

- ~~"日报发送失败 + 当天进程重启"会丢掉那一天的日报~~：旧因是"每 UTC 日至多消费
  一次"——重启后进程内持有者清空，再要令牌必须重新消费一次续期，若当天已经消费过
  就被持久上界拒绝，直到次日。解除该上界后，重启后的再次消费只受**最小间隔**（默认
  5 分钟，一次性的瞬时等待，不是整天）与**每日上界**（默认 100 次，正常一天约 12 次，
  单次重启不会撞上）约束，因此不再会把整天的日报堵死。这条残留因此随本次解除自动消解。
"""

from __future__ import annotations

import threading
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
        # 距上一次消费还未满最小间隔（默认 5 分钟）。判据在凭据文件里、由凭据库在
        # 锁内判定，因此进程重启、崩溃重启循环与第二个实例都绕不过它。与下面的
        # "当日上界"是两个不同的运维处置：这个通常是瞬时的，等一会儿再试就好。
        "refresh_min_interval_not_elapsed",
        # 当日消费次数已达上界（默认 100 次）。判据同样在凭据文件里、锁内判定。
        # 撞上它说明系统已经异常了数小时（最小间隔已把崩溃循环压到至多 12 次/小时），
        # 与"最小间隔未到"不是同一件事——这个要等到下一个 UTC 日才会恢复。
        "refresh_daily_limit_reached",
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

    **跨线程并发访问需要锁**（Issue #284 C 组 #7，Trace #373 D7 裁定修复）：装配层
    把同一个持有者交给多个并发读者——主循环线程（花名册审计/组织快照同步的后台线程）
    与开通链的 8 条工作线程都会调用 :meth:`fresh`，而 :meth:`store` 可能同时被"到点
    轮换"（主循环线程）与"按需供给"（任一读者线程触发的续期）两条路径调用。``_token``/
    ``_expires_at`` 是**两个字段**，``store()`` 依次赋值两行、``fresh()`` 依次读两行，
    GIL 只保证单条字节码的原子性，不保证这两行连在一起不被打断——`fresh()` 有可能读到
    "新 `_token` 配旧 `_expires_at`"或反过来的半更新组合，把一次真正新鲜的令牌误判为
    已过期（或反过来）。最坏后果如实登记：多一次不必要的供给触发，或漏记一条按需消费
    审计，**不触及凭据消费面**（那侧的 ``refresh_consumed_at``/``refresh_consumed_count``
    由 :meth:`~lingxi.adapters.delegated_credentials.HostFileDelegatedCredentialVault.
    claim_due` 在自己的文件锁内判定，不经过这个持有者）。锁只包住 ``_token``/
    ``_expires_at`` 这两个字段本身的读写，**不覆盖**调用方在锁外做的任何决策（例如
    "要不要发起一次续期"）——那部分并发正确性由凭据库的文件锁与
    :class:`RosterAccessTokenProvider` 自己的审计去重锁分别负责，不是本持有者的职责。

    **已知边界：并发 ``store()`` 不保证新旧顺序（P2-C，codex 外审 · Trace #373
    H1 批终修复包②，编排者裁定登记不改行为）**——``_lock`` 只保证单次 ``store()``
    调用内 ``_token``/``_expires_at`` 两行赋值成对可见，不保证两次并发 ``store()``
    谁先进锁。如果"到点轮换"（主循环线程）与"按需供给触发的续期"（任一读者线程）
    两条路径几乎同时各自派生出一份新令牌，较旧的那份派生结果有可能后进锁，把刚
    写入的、更新的那份覆盖掉——持有者里最终留下的会是**较旧**的一份，不一定是
    **最新**的一份。本类不给派生结果排序或去重，两次 ``store()`` 之间没有共享的
    生成序号或时间戳可比较。

    冻结这条边界不改的理由：本轮修复冻结凭据供给面的行为语义，且后果具备自愈能力
    ——:meth:`fresh` 有过期检查兜底（较旧的一份即便覆盖了较新的一份，仍会在自己
    真实的到期时间前保持可用，只是安全余量的起点稍早），消费侧
    （:class:`RosterAccessTokenProvider`\\ ``.__call__``）遇到失败会触发下一次
    按需续期，不会永久卡死在一份错误令牌上。

    **开放问题（未验证，需产品/外部规范确认）**：飞书签发一份新的派生
    ``access_token`` 时，是否会让同一 ``refresh_token`` 此前签发的旧
    ``access_token`` 立即失效？若是，上面这个覆盖窗口就不只是"用了一份稍旧但仍
    然有效的令牌"，而是"用了一份已经被服务端主动作废的令牌"——那种情形下每次
    调用会当场失败而不是等到自然过期，需要升级为按生成序号/时间戳排序、拒绝较
    旧结果覆盖较新结果。

    **复议条件**：出现由此类覆盖导致的真实供给失败（审计或线上报错可关联到"较新
    派生结果被较旧结果覆盖"这一具体形状），或上面的开放问题被证实为"是"，则需要
    重新评估是否引入生成序号/时间戳比较来拒绝旧值覆盖新值。
    """

    __slots__ = ("_token", "_expires_at", "_safety_margin", "_lock")

    def __init__(self, *, safety_margin: timedelta = DEFAULT_ACCESS_TOKEN_SAFETY_MARGIN) -> None:
        if not isinstance(safety_margin, timedelta) or safety_margin < timedelta(0):
            raise ValueError("短期令牌的安全余量必须是非负的时间长度")
        self._token: SecretToken | None = None
        self._expires_at: datetime | None = None
        self._safety_margin = safety_margin
        self._lock = threading.Lock()

    @property
    def safety_margin(self) -> timedelta:
        return self._safety_margin

    @property
    def has_token(self) -> bool:
        """是否持有令牌。**只回答有无，不回答值**，也不判断新鲜与否。"""

        with self._lock:
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

        **两行赋值在锁内完成**（Issue #284 C 组 #7）：`_token`/`_expires_at` 必须
        同时对读者可见，不能出现"新令牌配旧过期时间"或反过来的半更新组合。
        """

        moment = _require_utc(now, "now")
        if not isinstance(derived, DerivedAccessToken):
            raise TypeError("只接受 DerivedAccessToken，避免明文令牌以裸字符串流转")
        if derived.expires_in is None:
            return False
        expires_at = moment + timedelta(seconds=derived.expires_in)
        if moment + self._safety_margin >= expires_at:
            return False
        with self._lock:
            self._token = derived.token
            self._expires_at = expires_at
        return True

    def fresh(self, *, now: datetime) -> SecretToken | None:
        """取出**仍然新鲜**的令牌；没有或已临期时返回 ``None``。

        临期按安全余量提前判死（默认 5 分钟），不是等到过期那一刻。

        **两个字段在同一把锁内成对读出**（Issue #284 C 组 #7）：与 :meth:`store` 共用
        同一把锁，保证读到的 ``_token``/``_expires_at`` 一定来自同一次 :meth:`store`
        调用，不会读到跨调用拼出来的半更新组合。
        """

        moment = _require_utc(now, "now")
        with self._lock:
            token = self._token
            expires_at = self._expires_at
        if token is None or expires_at is None:
            return None
        if moment + self._safety_margin >= expires_at:
            return None
        return token

    def clear(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = None

    def __repr__(self) -> str:  # pragma: no cover - 由 HolderTest 的形状断言覆盖
        with self._lock:
            has_token = self._token is not None
        return f"DerivedAccessTokenHolder(has_token={has_token})"


class RosterAccessTokenProvider:
    """花名册读取的短期令牌供给：``Callable[[], str]``，交给 ``BitableRosterPages``。

    一次调用的判定次序是刻意的：

    1. **手上有新鲜的就直接给**——花名册一轮要读很多页，每页现取一次，不能每页都去
       消费凭据；
    2. 没有或已临期 → 触发一次轮换职责内的受控续期。**频率上界不在这里**：它属于凭据
       文件那一侧（凭据库在锁内判"距上一次消费是否已过最小间隔、当日消费次数是否已达
       上界"），放在这里会变成"打算试一次就扣一次"，把可证明零消费的失败也算进去，
       还会认不出人工重授权换来的新凭据；
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

    **审计去重的读改写需要加锁**（Issue #284 C 组 #7，Trace #373 D7 裁定修复）：
    本供给是**同一个** ``Callable[[], str]``，被主循环线程（花名册审计/组织快照同步
    的后台线程）与开通链的 8 条工作线程并发调用（见 ``apps/scheduler/assembly.py``
    对 ``supply`` 的复用注释）。``_audited_on``/``_audited_reasons`` 的"今天变了就
    清空、清空之后再判重"是一次读改写，未加锁时两个线程同时撞上同一个新的一天可能
    互相踩踏——一个线程刚把 ``_audited_reasons`` 清空、还没来得及把自己的原因加回去，
    另一个线程就读到"空集合"并各自都判定"这个原因今天还没记过"，于是**同一天同一
    分类被重复审计两次**。最坏后果同样有界（多几条重复的分类审计行，不影响任何
    权限、凭据或用户可见结果），但既然已经在为并发接线，一并锁住。
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
        # 审计去重专用锁（Issue #284 C 组 #7）：只包住 `_record` 内部对
        # `_audited_on`/`_audited_reasons` 的读改写，不包住 `__call__` 里对
        # `_refresh()`/`_holder` 的调用——那两者各自已有自己的并发防线（凭据库的
        # 文件锁、持有者自己的锁），不需要再借用这把锁去覆盖它们。
        self._audit_lock = threading.Lock()

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
            # 凭据没有丢，因此**不撤销**；这次消费已经计入频率上界，下一次重试受最小
            # 间隔（默认 5 分钟）约束，不是要等到明天。
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

        判重的读改写在 ``_audit_lock`` 内完成（Issue #284 C 组 #7）：并发调用者
        共享同一份 ``_audited_on``/``_audited_reasons``，不加锁会在"今天变了"这个
        判断上互相踩踏，见类文档字符串。实际发出审计（``self._audit.record``）
        刻意留在锁外——审计出口自己的 I/O 不需要占着这把只保护内存去重状态的锁。

        **``_audited_on`` 只单调前进**（P2-D，codex 外审 · Trace #373 H1 批终修复
        包②）：``now`` 在 :meth:`__call__` 开头取，本方法却迟后才进锁——跨 UTC
        午夜的迟到调用（例如线程调度延迟，或先经历了一次耗时的 ``refresh()``）
        可能带着一个比当前 ``_audited_on`` 更旧的日期到达。旧写法只判断
        ``_audited_on != today`` 就重置，会把日期"倒退"回前一天，重置掉当天
        （更新的那一天）已经积累的去重集合，导致后续同一天同一分类被再次审计一
        遍。改为只在 ``today`` 严格晚于当前 ``_audited_on`` 时才前进/重置；一个
        比当前记录更旧（或相同）的日期到达时，不重置，直接按当前（更新）这一天
        已审计的口径判重——迟到调用自己过期的日期戳被忽略，不倒退状态。
        """

        today = now.date()
        with self._audit_lock:
            if self._audited_on is None or today > self._audited_on:
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
