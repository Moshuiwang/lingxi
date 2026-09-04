"""MCP 就绪确认的共享模型：五路结果、轮询节奏、判定记录与探针执行引擎。

产品合同要求「明确确认该用户应有的公司和职能权限已经同步且可以问数后，才宣告开通成功」。
判定依据因此是**探针**而不是回读比对：发布表没有版本字段可比对，因此改用该用户**自己的**
明文令牌真实执行一次 ``list_metrics``，可见指标条数 > 0 才算就绪，明确空结果不算
（见 :func:`classify_probe`）。结论按 (用户, 权限版本) 绑定，见 :class:`ReadinessBinding`。

五路结果互斥且不可合并：``no_permission`` 只从数据库侧判定，绝不从 MCP 拒绝反推；
``technical_failure`` 是独立中间态，绝不能凑成 ``ready``。

``_ReadinessProbeRunner`` 是「发一次探针并把结论落库」这一步的**唯一**实现，阻塞式
（:class:`McpReadinessConfirmation`，本模块）与 tick 驱动式
（:mod:`lingxi.core.permission.mcp_readiness_tick`）共用同一份，避免"第二套就绪语义"
这条最贵的错误。本模块住在 ``core/``：不连数据库，探针与时钟均以 Protocol 注入。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Protocol

from lingxi.core.permission.publish_row import lookup_metrics, parse_permissions

logger = logging.getLogger(__name__)

_UTC = UTC

#: 合同节奏：立即一次，此后每三分钟一次，十五分钟停止（产品合同「首次开通」）。
DEFAULT_INTERVAL_SECONDS = 180
DEFAULT_BUDGET_SECONDS = 900

#: 合法区间。下界取 1 秒是为了让验收窗口能用**最小合法配置**把等待压到两次尝试；
#: 上界是防御而不是容量规划——撞上它是配置错误，不是"这次要等很久"。
MIN_INTERVAL_SECONDS = 1
MAX_INTERVAL_SECONDS = 900
MIN_BUDGET_SECONDS = 1
MAX_BUDGET_SECONDS = 3600

#: 单次探针的传输超时。默认值与 ``adapters/query_mcp_probe.REQUEST_TIMEOUT_SECONDS``
#: 一致；装配层把两者接到一起时必须保持相等，否则这里算出来的收口上界是假的。
#: 上界不是独立常量：它被 :class:`ReadinessSchedule` 强制 ≤ 轮询间隔。
DEFAULT_PROBE_TIMEOUT_SECONDS = 20
MIN_PROBE_TIMEOUT_SECONDS = 1

#: 错误码的长度上限，与迁移 ``0065`` 的 ``mcp_sync_check.error_code`` CHECK 是同一个数。
#: 改动必须两处同时改，否则超限值会在数据库那一侧才失败，把一次可记录的失败变成
#: 整轮确认中断。
MAX_ERROR_CODE_LENGTH = 200

#: 一次确认最多允许发多少次探针。它挡的是 ``interval=1, budget=3600`` 这类**合法却荒谬**
#: 的组合：3601 次调用会把问数 MCP 的配额打光。做法是**拒绝这样的配置**，不是悄悄截断——
#: 截断会让实际节奏与配置说的不是一回事，而那种偏差没人看得见。
MAX_ATTEMPTS = 64


class ReadinessOutcome(Enum):
    """一次判定的结果。五路互斥，语义见模块文档。"""

    READY = "ready"
    NO_PERMISSION = "no_permission"
    WAITING = "waiting"
    TIMED_OUT = "timed_out"
    TECHNICAL_FAILURE = "technical_failure"


#: 终态：上游可以据此收口。``waiting`` 与 ``technical_failure`` 都是中间态——预算还在时
#: 它们只意味着"再试一次"。
TERMINAL_OUTCOMES = frozenset(
    {ReadinessOutcome.READY, ReadinessOutcome.NO_PERMISSION, ReadinessOutcome.TIMED_OUT}
)


class McpProbeError(RuntimeError):
    """就绪探针调用失败。``code`` 供程序判断，消息里**没有令牌、URL 与人员资料**。

    ``denied`` 区分两件事：``True`` 是问数 MCP **完整返回并明确拒绝**（鉴权或权限
    错误），代表"同步中"，落 ``waiting``；``False`` 是**结果不明**（网络、协议、
    响应形状、令牌解密、数据库），探针根本没跑起来，落 ``technical_failure``。只有
    "完整返回 + 明确拒绝"才算明确，其余一律结果不明——方向是安全的：把明确拒绝误判
    成技术失败只会多等几轮，反过来则会让一次网络抖动被读成"这个人被 MCP 拒了"。
    """

    def __init__(self, code: str, *, denied: bool = False) -> None:
        """记录失败分类码与「是否为明确拒绝」。"""
        super().__init__(f"问数 MCP 就绪探针失败：{code}")
        self.code = code
        self.denied = denied


def _require_seconds(label: str, value: object, low: int, high: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label}必须是 {low} 到 {high} 之间的整数秒")
    if value < low or value > high:
        raise ValueError(f"{label}必须是 {low} 到 {high} 之间的整数秒，收到 {value}")


@dataclass(frozen=True)
class ReadinessSchedule:
    """轮询节奏与总预算，只接受合法区间内的取值。

    **没有回退默认的分支**——一个静默回退的配置项等于没有配置项，而这里配错的方向是
    "无上界地一直探下去"。``interval_seconds`` 是两次探针之间的间隔，``budget_seconds``
    是从第一次探针算起的总预算。第一次探针在 ``t=0``（发布读回一致后**立即**一次），
    之后每 ``interval`` 一次，直到下一次的时刻**超过**预算为止。
    """

    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    budget_seconds: int = DEFAULT_BUDGET_SECONDS
    probe_timeout_seconds: int = DEFAULT_PROBE_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        """校验节奏与预算落在合法区间内，且组合不会隐含荒谬的探测次数。"""
        _require_seconds(
            "轮询间隔", self.interval_seconds, MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS
        )
        _require_seconds("总预算", self.budget_seconds, MIN_BUDGET_SECONDS, MAX_BUDGET_SECONDS)
        _require_seconds(
            "单次探针超时",
            self.probe_timeout_seconds,
            MIN_PROBE_TIMEOUT_SECONDS,
            MAX_INTERVAL_SECONDS,
        )
        if self.interval_seconds > self.budget_seconds:
            raise ValueError("轮询间隔不得大于总预算：那样只会发出一次探针，配置在说谎")
        if self.probe_timeout_seconds > self.interval_seconds:
            # 上界是结论落地时刻的上界（见 hard_deadline）：慢探针会一路把后续尝试往后
            # 推，整轮确认可以远远超过十五分钟才收口。装配层必须让探针传输层的超时与
            # 这里一致。
            raise ValueError("单次探针超时不得大于轮询间隔：否则整轮确认会无上界地拖长")
        planned = self.budget_seconds // self.interval_seconds + 1
        if planned > MAX_ATTEMPTS:
            raise ValueError(
                f"这组节奏会发出 {planned} 次探针，超过上限 {MAX_ATTEMPTS}；"
                "请调大间隔或调小预算（不截断，因为截断会让实际节奏与配置不一致）"
            )

    def attempt_offsets(self) -> tuple[int, ...]:
        """每次探针相对起点的秒偏移，例如默认值给出 ``(0, 180, 360, 540, 720, 900)``。

        边界是"小于等于预算"而不是"小于"：合同说的是"最多等待十五分钟"，落在第十五分钟
        整点的那一次仍在窗口内。**这份计划表是"发几次"的唯一判据**：执行时不再拿"现在
        过没过预算"去二次否决某一次尝试——真实时钟下每次多回来几毫秒会在第六次时把
        ``now`` 推过预算，导致最后一次探针被误跳过（实测发现的偏差，见改法说明）。
        """
        offsets: list[int] = []
        offset = 0
        while offset <= self.budget_seconds:
            offsets.append(offset)
            offset += self.interval_seconds
        return tuple(offsets)

    @property
    def max_attempts(self) -> int:
        """计划表里的探测次数上限。"""
        return len(self.attempt_offsets())

    @property
    def budget(self) -> timedelta:
        """总预算，转换为 ``timedelta``。"""
        return timedelta(seconds=self.budget_seconds)

    @property
    def hard_deadline(self) -> timedelta:
        """结论**最晚**落地的时刻（相对起点）：预算 + 一次探针超时。

        计划表决定"发几次"，这条决定"最坏拖到什么时候"：正常与抖动时钟下它永远不触发，
        只有探针链真的失控（探针无视超时）时才把整轮确认收口在这个上界之内。

        它在两个时刻各查一次：发起下一次探针**之前**（还没超才发），以及探针**返回之后**
        （超了就不许判就绪）——只查前者不够，最后一次探针可能在临界前发起、临界后才
        返回成功。
        """
        return timedelta(seconds=self.budget_seconds + self.probe_timeout_seconds)


#: 合同默认节奏。单独取个名字，让"用的是不是合同值"能被断言。
CONTRACT_SCHEDULE = ReadinessSchedule()


@dataclass(frozen=True)
class ReadinessBinding:
    """就绪结论绑定的对象：**哪个用户的哪一版权限**。

    没有它，一次成功的探针会变成一句无限期有效的"这个人可以用了"——而权限随时会被收回
    或扩大。绑定让结论有明确的作用域，:meth:`ReadinessSession.applies_to` 是它的出口。
    """

    user_id: str
    permission_version: int

    def __post_init__(self) -> None:
        """校验用户与权限版本都是有意义的取值。"""
        if not isinstance(self.user_id, str) or not self.user_id.strip():
            raise ValueError("就绪确认必须绑定用户")
        if isinstance(self.permission_version, bool) or not isinstance(
            self.permission_version, int
        ):
            raise ValueError("权限版本必须是整数")
        if self.permission_version <= 0:
            raise ValueError("权限版本必须为正：0 表示还没有过任何权限决定")


def _require_attempt_shape(
    outcome: ReadinessOutcome, error_code: str | None, metric_count: int | None
) -> None:
    """五路结论各自的**精确形状**，与迁移 ``0065`` 的 CHECK 一一对应，两处必须同时改。

    | 结论 | 错误码 | 观察值 |
    |---|---|---|
    | ``ready`` | **必须没有** | 必须有，且 > 0 |
    | ``waiting`` | 必须有 | 没有（明确拒绝）或恰为 0（空结果） |
    | ``technical_failure`` | 必须有 | **必须没有**（探针没跑通，任何数字都是假的） |
    | ``no_permission`` / ``timed_out`` | 必须有 | **必须没有** |
    """
    if isinstance(metric_count, bool) or not isinstance(metric_count, (int, type(None))):
        raise ValueError("可见指标条数必须是整数或缺省")
    if metric_count is not None and metric_count < 0:
        raise ValueError("可见指标条数不得为负")
    if error_code is not None:
        # 空串与纯空白等同于"没写"：下面正是靠"有没有错误码"判断"未就绪有没有给出原因"。
        if not isinstance(error_code, str) or not error_code.strip():
            raise ValueError("错误码必须是非空字符串，空白不算")
        # 长度上限与迁移 0065 的 CHECK 同一个数：超限时数据库那一侧抛的是记账失败，
        # 会中断整轮确认——这本该只是一次可记录的失败。错误码是码不是消息，放宽就会
        # 有人往里塞异常正文（含凭据与人员资料）。
        if len(error_code) > MAX_ERROR_CODE_LENGTH:
            raise ValueError(f"错误码不得超过 {MAX_ERROR_CODE_LENGTH} 个字符：它是码，不是消息")
    if outcome is ReadinessOutcome.READY:
        if metric_count is None or metric_count <= 0:
            raise ValueError("就绪必须带着大于零的可见指标条数（明确空结果不算就绪）")
        if error_code is not None:
            raise ValueError("就绪不得同时携带错误码")
        return
    if not error_code:
        raise ValueError("未就绪的结论必须带错误码，否则运维无从下手")
    if outcome is ReadinessOutcome.WAITING:
        if metric_count is not None and metric_count != 0:
            # 等待中只有两种来源：明确拒绝（没看见任何东西）与空结果（恰好看见 0 条）。
            # 一条"等待中却看见 5 条"的记录自相矛盾——看见了就该是就绪。
            raise ValueError("等待中的观察值只能缺省或为零")
        return
    if metric_count is not None:
        raise ValueError("未发起探针或探针未跑通的结论不得携带可见指标条数")


@dataclass(frozen=True)
class ReadinessAttempt:
    """一次判定的完整结果，可直接进审计与就绪记录表。

    **不含任何人员资料值，也不含令牌**：只有内部标识、权限版本、次序、时间、结果分类、
    错误码与**指标条数**（一个计数，不是指标名——指标名是权限范围，不该随审计到处走）。
    """

    binding: ReadinessBinding
    attempt_no: int
    outcome: ReadinessOutcome
    started_at: datetime
    finished_at: datetime
    error_code: str | None = None
    metric_count: int | None = None

    def __post_init__(self) -> None:
        """校验次序、时间跨度与五路结论的形状是否自洽。"""
        if isinstance(self.attempt_no, bool) or not isinstance(self.attempt_no, int):
            raise ValueError("尝试次序必须是整数")
        if self.attempt_no < 1:
            raise ValueError("尝试次序从 1 开始")
        for label, moment in (("开始", self.started_at), ("结束", self.finished_at)):
            if (
                not isinstance(moment, datetime)
                or moment.tzinfo is None
                or moment.utcoffset() is None
            ):
                raise ValueError(f"就绪判定的{label}时间必须是带时区的时间")
        if self.finished_at < self.started_at:
            raise ValueError("就绪判定的结束时间不得早于开始时间")
        _require_attempt_shape(self.outcome, self.error_code, self.metric_count)

    @property
    def ready(self) -> bool:
        """这一版权限是否**被证明**在当前用户的问数 MCP 上可用。唯一的成功信号。"""
        return self.outcome is ReadinessOutcome.READY

    @property
    def terminal(self) -> bool:
        """这次结论是不是终态（``ready`` / ``no_permission`` / ``timed_out``）。"""
        return self.outcome in TERMINAL_OUTCOMES

    @property
    def elapsed_ms(self) -> int:
        """这一次探针从发起到结算耗时多少毫秒。"""
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    def audit_facts(self) -> dict[str, Any]:
        """这次判定可安全写进审计事实的字段（不含令牌与人员资料）。"""
        return {
            "outcome": self.outcome.value,
            "user_id": self.binding.user_id,
            "permission_version": self.binding.permission_version,
            "attempt_no": self.attempt_no,
            "error_code": self.error_code,
            "metric_count": self.metric_count,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass(frozen=True)
class ReadinessSession:
    """一整轮确认（可能只有一次尝试，也可能耗尽预算）的收口结果。"""

    binding: ReadinessBinding
    outcome: ReadinessOutcome
    attempts: tuple[ReadinessAttempt, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """校验收口结论是终态、判定记录非空且都绑定同一个 (用户, 权限版本)。"""
        if self.outcome not in TERMINAL_OUTCOMES:
            # 一轮确认只能以终态收口：中间态收口等于让上游拿到一个"还没结束"的结论
            # 却以为可以据此行动。
            raise ValueError("确认结果必须是终态：ready / no_permission / timed_out")
        if not self.attempts:
            raise ValueError("确认结果必须至少包含一次判定记录")
        # 下面两条挡的是"用别人的成功拼出一个自己的成功"：没有它们，拿用户 B 的 ready
        # 记录 + 用户 A 的 binding 就能构造出一个 applies_to(A, v) 为真的对象，而
        # applies_to 只核 binding、不核证据。
        foreign = [item for item in self.attempts if item.binding != self.binding]
        if foreign:
            raise ValueError("确认结果里的判定记录必须全部绑定同一个（用户，权限版本）")
        if self.attempts[-1].outcome is not self.outcome:
            # 收口结论只能是最后一次判定本身。允许两者不一致，等于允许一轮以 timed_out
            # 收尾的确认对外宣称 ready。
            raise ValueError("确认结果必须与最后一次判定的结论一致")

    @property
    def ready(self) -> bool:
        """这一轮确认是否以就绪收口。"""
        return self.outcome is ReadinessOutcome.READY

    @property
    def attempt_count(self) -> int:
        """这一轮一共落了多少次判定记录。"""
        return len(self.attempts)

    @property
    def technical_failures(self) -> int:
        """其中有多少次是技术失败。超时收口时它回答"到底是等不到，还是压根没探成"。"""
        return sum(
            1 for item in self.attempts if item.outcome is ReadinessOutcome.TECHNICAL_FAILURE
        )

    def applies_to(self, user_id: str, permission_version: int) -> bool:
        """这条结论**对不对得上**给定的用户与权限版本。

        上游在把"就绪"当作开通成功依据之前**必须**调它。探针成功只对当次绑定的那一版
        权限有效：权限在探针之后被收回或扩大时，这条结论立刻失效，而本模块不读库、
        因此发现不了那件事——把判断放在出口上，比在这里悄悄多读一次库更诚实。
        """
        return (
            self.ready
            and self.binding.user_id == user_id
            and self.binding.permission_version == permission_version
        )

    def audit_facts(self) -> dict[str, Any]:
        """这一轮确认可安全写进审计事实的字段。"""
        return {
            "outcome": self.outcome.value,
            "user_id": self.binding.user_id,
            "permission_version": self.binding.permission_version,
            "attempts": self.attempt_count,
            "technical_failures": self.technical_failures,
            "last_error": self.attempts[-1].error_code,
        }


def evaluate_permission_presence(permissions: str, *, company_id: Any = None) -> bool:
    """**数据库侧**这个人到底有没有可等的权限（``no_permission`` 分支的唯一依据）。

    输入是我们自己发布出去的那份 ``permissions`` 文本，判定走 :func:`lingxi.core.
    permission.publish_row.lookup_metrics` 的**回退制**：给定公司时先看该公司键、
    不存在才回退 ``"*"``，**不取并集**。**刻意不接受"从 MCP 拒绝反推"这条输入**：
    MCP 拒绝一次可能只是它还没刷新缓存，据此宣布"没权限"会让权限正常的用户收到一个
    无法自救的终态提示。权限文本读不懂时抛 ``ValueError`` 而不是判"没有权限"：读不懂
    是本侧缺陷，折成用户可见的终态等于用一句确定的错话掩盖一次不确定的故障。
    """
    return bool(lookup_metrics(parse_permissions(permissions), company_id))


class McpProbe(Protocol):
    """就绪探针的可注入面。实现见 ``adapters/query_mcp_probe.py``。

    **签名里没有令牌。** 探针自己按 ``user_id`` 去取该用户的明文令牌（并在用完后丢弃），
    因此明文一次都不进 ``core``，也不可能被塞进任何结果对象、审计事实或日志。
    """

    def list_metrics(self, *, user_id: str) -> int:
        """以该用户身份执行一次 ``list_metrics``，返回**可见指标条数**。

        失败一律抛 :class:`McpProbeError`；其余异常原样上抛（未预期的异常不该被折成
        "技术失败"，那会让真正的缺陷每轮安静地重试下去）。
        """
        ...


class ReadinessCheckStore(Protocol):
    """就绪判定记录的最小写入面。实现见 ``adapters/postgres_mcp_token.py``。"""

    def record_attempt(self, attempt: ReadinessAttempt) -> str:
        """把一次判定记录写进 ``mcp_sync_check``，返回该行的主键。"""
        ...


class _AuditSink(Protocol):
    def record(self, action: str, /, **fields: object) -> None: ...


def classify_probe(metric_count: object) -> tuple[ReadinessOutcome, str | None]:
    """把一次**成功返回**的探针结果分成 ``ready`` 或 ``waiting``（纯函数）。

    - 条数 > 0 → ``ready``；
    - 条数 == 0 → ``waiting``——明确空结果只证明这次查询没有报错，证明不了这个人的权限
      范围已经生效（MCP 每十五分钟才拉一次发布表，刚发布完拿到空列表是常态）；
    - 不是非负整数 → ``technical_failure``——适配器返回了读不懂的东西，属本侧缺陷，
      绝不能凑成一次就绪。
    """
    if isinstance(metric_count, bool) or not isinstance(metric_count, int) or metric_count < 0:
        return ReadinessOutcome.TECHNICAL_FAILURE, "invalid_metric_count"
    if metric_count > 0:
        return ReadinessOutcome.READY, None
    return ReadinessOutcome.WAITING, "empty_metrics"


class _ReadinessProbeRunner:
    """「发一次探针并把结论落库」这一步的**唯一**实现，两种等待形态共用。

    两个子类的区别只有"怎么等到下一次探针"（tick 驱动形态见
    :mod:`lingxi.core.permission.mcp_readiness_tick`，阻塞形态见
    :class:`McpReadinessConfirmation`）。**分类、五路取值、绑定、记账与审计全部在这里，
    一份实现**——分叉一次就会有一批用户拿到假就绪。
    """

    def __init__(
        self,
        *,
        probe: McpProbe | None,
        store: ReadinessCheckStore,
        audit: _AuditSink,
        clock: Callable[[], datetime],
        schedule: ReadinessSchedule = CONTRACT_SCHEDULE,
    ) -> None:
        if not isinstance(schedule, ReadinessSchedule):
            # 只接受**已经过校验的**配置对象：允许在这里传一对裸整数，等于给每个调用点
            # 各开一次绕过校验的口子。
            raise TypeError("轮询节奏必须是已校验的 ReadinessSchedule")
        if not callable(clock):
            raise TypeError("时钟必须可调用：注入它才能不真的等十五分钟")
        self._probe = probe
        self._store = store
        self._audit = audit
        self._clock = clock
        self._schedule = schedule

    @property
    def schedule(self) -> ReadinessSchedule:
        return self._schedule

    @property
    def probe_wired(self) -> bool:
        """探针装配了没有。

        见 :mod:`lingxi.core.permission.mcp_readiness_tick` 的「探针未接线」一节。
        """
        return self._probe is not None

    # ------------------------------------------------------------------
    # 共用的一步：发一次探针 → 分类 → 落库 → 审计
    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        moment = self._clock()
        if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
            # naive 时间会让跨时区部署算出互相矛盾的预算，而预算正是"等了多久"的唯一依据。
            raise ValueError("注入的时钟必须返回带时区的时间")
        return moment.astimezone(_UTC)

    def _probe_once(
        self,
        binding: ReadinessBinding,
        attempt_no: int,
        hard_deadline: datetime | None = None,
        *,
        overrun_outcome: ReadinessOutcome = ReadinessOutcome.TIMED_OUT,
        overrun_error: str = "success_after_deadline",
    ) -> ReadinessAttempt:
        """发一次探针并结算成一条判定记录。

        ``hard_deadline`` 是"结论最晚什么时候还算数"：阻塞形态传整轮上界，tick 形态传
        ``None``（改用这一次探针自己的上界，因为 tick 的调度粒度会让整轮上界误杀一次
        刚好晚到的成功）。``overrun_outcome``/``overrun_error`` 决定超窗的成功落哪一路，
        两种形态刻意不同，见 :mod:`lingxi.core.permission.mcp_readiness_tick`。

        超窗的边界是"严格大于"：``finished == limit`` 仍算窗口内。
        """
        started = self._now()
        limit = (
            hard_deadline
            if hard_deadline is not None
            else started + timedelta(seconds=self._schedule.probe_timeout_seconds)
        )
        try:
            observed = self._probe.list_metrics(user_id=binding.user_id)
        except McpProbeError as error:
            # 明确拒绝 = MCP 还没看见这一行（同步中）；结果不明 = 我们的探针没跑起来。
            outcome = (
                ReadinessOutcome.WAITING if error.denied else ReadinessOutcome.TECHNICAL_FAILURE
            )
            return self._attempt(
                binding, attempt_no, outcome, started, self._now(), error_code=error.code
            )
        outcome, error_code, counted, finished = self._settle_probe_result(
            observed, limit, overrun_outcome, overrun_error
        )
        return self._attempt(
            binding,
            attempt_no,
            outcome,
            started,
            finished,
            error_code=error_code,
            metric_count=counted,
        )

    def _settle_probe_result(
        self,
        observed: object,
        limit: datetime,
        overrun_outcome: ReadinessOutcome,
        overrun_error: str,
    ) -> tuple[ReadinessOutcome, str | None, int | None, datetime]:
        """把一次**成功返回**的探针分类，并在结果太晚才到时降级为超窗结论。

        降级而不是记一条 ``ready`` 再让上层否决：那样 ``mcp_sync_check`` 会留下一行
        ``ready``，而这一轮的结论不是就绪，读表的人会看到两个互相矛盾的事实。观察值
        一并丢弃（超窗结论都不得携带）。
        """
        outcome, error_code = classify_probe(observed)
        # 只有 classify_probe 认可的观察值才进记录：读不懂的返回值落技术失败，那一路
        # 必须不带观察值（否则看起来像跑通了的探针，负数还会撞上迁移 0065 的 CHECK）。
        counted = None if outcome is ReadinessOutcome.TECHNICAL_FAILURE else int(observed)
        finished = self._now()
        if outcome is ReadinessOutcome.READY and finished > limit:
            outcome, error_code, counted = (overrun_outcome, overrun_error, None)
        return outcome, error_code, counted, finished

    def _attempt(
        self,
        binding: ReadinessBinding,
        attempt_no: int,
        outcome: ReadinessOutcome,
        started_at: datetime,
        finished_at: datetime,
        *,
        error_code: str | None = None,
        metric_count: int | None = None,
    ) -> ReadinessAttempt:
        attempt = ReadinessAttempt(
            binding=binding,
            attempt_no=attempt_no,
            outcome=outcome,
            started_at=started_at,
            finished_at=finished_at,
            error_code=error_code,
            metric_count=metric_count,
        )
        try:
            self._store.record_attempt(attempt)
        except Exception as error:
            self._audit.record(
                "mcp_readiness.record_failed",
                # 只记异常类型：异常正文可能带上连接串或响应片段。
                error=type(error).__name__,
                **attempt.audit_facts(),
            )
            raise
        self._audit.record(f"mcp_readiness.{outcome.value}", **attempt.audit_facts())
        return attempt


class McpReadinessConfirmation(_ReadinessProbeRunner):
    """把「判权限 → 立即探一次 → 每三分钟再探 → 十五分钟收口」串起来（**阻塞式**）。

    只编排注入的接口，不做 I/O：探针、存储、审计、时钟与 ``sleep`` 全部注入，因此
    可用假时钟在纯单测里证伪节奏，不用真的等。**本类会阻塞调用线程最长十五分钟**，
    不能塞进常驻循环的一轮 tick（那要用 :class:`~lingxi.core.permission.
    mcp_readiness_tick.ReadinessTicker`），只用于调用方自己在线程里等结论的场景。
    记录写失败不吞：先留一条审计再原样上抛。
    """

    name = "MCP 就绪确认"

    def __init__(
        self,
        *,
        probe: McpProbe,
        store: ReadinessCheckStore,
        audit: _AuditSink,
        clock: Callable[[], datetime],
        sleep: Callable[[float], None],
        schedule: ReadinessSchedule = CONTRACT_SCHEDULE,
        on_alert: Callable[[ReadinessSession], None] | None = None,
    ) -> None:
        """装配阻塞式确认；``probe`` 与 ``sleep`` 是必填项，缺一不可。"""
        super().__init__(probe=probe, store=store, audit=audit, clock=clock, schedule=schedule)
        if probe is None:
            # 阻塞形态没有"探针未接线"这种状态：它的调用方是一次开通编排，拿不到结论
            # 就没法继续。可缺省的探针只对 tick 形态有意义。
            raise TypeError("阻塞式就绪确认必须注入探针")
        if not callable(sleep):
            # sleep 必填，且没有"缺省就不等"的分支：缺省会把六次探针背靠背发完、立刻
            # 判超时，十五分钟的等待被压成毫秒级，而每一条记录看起来都完全正常。
            raise TypeError("sleep 必须可调用：缺省会把十五分钟的等待压成瞬时")
        self._sleep = sleep
        self._on_alert = on_alert

    def confirm(
        self, binding: ReadinessBinding, *, permissions: str, company_id: Any = None
    ) -> ReadinessSession:
        """跑完一轮确认，返回终态结果。

        ``permissions`` 是我们发布出去的那一版权限文本，先决定 ``no_permission`` 分支：
        聚合为空就没有任何可等的东西，一次探针都不发（先判有没有权限，再谈就绪——反过来
        会让一个本来就没有权限的人白等十五分钟才被转运维）。之后立即探一次（发布刚读回
        一致，MCP 有可能已经在上一轮拉表里带上这个人），此后按节奏探，直到预算耗尽判超时
        （超时是预算耗尽才成立的判定，不因连续技术失败提前收口）。
        """
        if not isinstance(binding, ReadinessBinding):
            raise TypeError("就绪确认必须绑定 (用户, 权限版本)")
        started = self._now()
        if not evaluate_permission_presence(permissions, company_id=company_id):
            attempt = self._attempt(
                binding,
                1,
                ReadinessOutcome.NO_PERMISSION,
                started,
                self._now(),
                error_code="no_publishable_permission",
            )
            return self._finish(binding, ReadinessOutcome.NO_PERMISSION, (attempt,))

        # 计划表决定发几次，硬上界只防失控的探针链（见 ReadinessSchedule）。
        hard_deadline = started + self._schedule.hard_deadline
        attempts = self._run_schedule(binding, started, hard_deadline)
        if attempts[-1].outcome in (ReadinessOutcome.READY, ReadinessOutcome.TIMED_OUT):
            return self._finish(binding, attempts[-1].outcome, tuple(attempts))
        timed_out = self._attempt(
            binding,
            len(attempts) + 1,
            ReadinessOutcome.TIMED_OUT,
            self._now(),
            self._now(),
            error_code="budget_exhausted",
        )
        attempts.append(timed_out)
        return self._finish(binding, ReadinessOutcome.TIMED_OUT, tuple(attempts))

    def _run_schedule(
        self, binding: ReadinessBinding, started: datetime, hard_deadline: datetime
    ) -> list[ReadinessAttempt]:
        """按计划表逐次探，探到就绪或探针返回超时即提前返回。"""
        attempts: list[ReadinessAttempt] = []
        for attempt_no, offset in enumerate(self._schedule.attempt_offsets(), start=1):
            if attempt_no > 1:
                self._wait_until(started + timedelta(seconds=offset))
                if self._now() > hard_deadline:
                    # 只有探针链真的失控才会走到这里：正常与抖动时钟下 now 离硬上界
                    # 还差一整个探针超时。
                    break
            attempt = self._probe_once(binding, attempt_no, hard_deadline)
            attempts.append(attempt)
            if attempt.ready or attempt.outcome is ReadinessOutcome.TIMED_OUT:
                return attempts
        return attempts

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _wait_until(self, due: datetime) -> None:
        """等到计划时刻。

        **按绝对时刻等，不按间隔累加**——后者会把每一次的调度误差累积下去，第六次就
        漂到预算之外。已经过了计划时刻就立刻开始（慢探针的情形）。
        """
        remaining = (due - self._now()).total_seconds()
        if remaining > 0:
            self._sleep(remaining)

    def _finish(
        self,
        binding: ReadinessBinding,
        outcome: ReadinessOutcome,
        attempts: tuple[ReadinessAttempt, ...],
    ) -> ReadinessSession:
        session = ReadinessSession(binding=binding, outcome=outcome, attempts=attempts)
        if outcome is not ReadinessOutcome.READY:
            logger.warning(
                "MCP 就绪确认未成功 outcome=%s user=%s version=%s attempts=%s 技术失败=%s error=%s",
                outcome.value,
                binding.user_id,
                binding.permission_version,
                session.attempt_count,
                session.technical_failures,
                attempts[-1].error_code,
            )
            if self._on_alert is not None:
                self._on_alert(session)
        return session
