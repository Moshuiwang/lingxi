"""发布之后的「当前用户 MCP 是否就绪」确认：五路互斥分流（纯编排）。

[Issue #156](https://github.com/Moshuiwang/lingxi/issues/156) 的 S-C-02。发布怎么做、
一行长什么样，见 :mod:`lingxi.core.permission.publish` 与
:mod:`lingxi.core.permission.publish_row`；本模块只回答一句话：**权限发布读回一致之后，
凭什么说这个人的问数 MCP 已经就绪，以及等多久算等不到了**。

本模块住在 ``core/``，因此不 import 任何适配器、不发请求、不连数据库、不读时钟：探针、
就绪记录存储与时钟都以 Protocol / 可调用对象注入，全部断言可以在没有网络也没有数据库的
机器上跑完，且**不需要真的等十五分钟**。

## 判定依据是**探针**，不是回读比对（G-155 终判）

产品合同要求「明确确认该用户应有的公司和职能权限已经同步且可以问数后，才宣告开通成功」。
架构设计原先的落法是「回读 MCP 的 ``permission_version`` 与生效范围、与数据库逐项比对
一致后再发一次最小查询」。**这条路走不通**：2026-08-17 对正式发布表的全表回源核对确认
**表侧没有任何版本字段**，写进去的一行不带版本，MCP 也就无从回读一个可比对的版本号。

因此就绪的唯一依据改为**探针法**：用该用户**自己的**明文令牌对问数 MCP 真实执行一次
``list_metrics``，**返回的可见指标条数大于零**才算就绪。这条判定保住了原断言的全部要害：

- 它是**以目标用户身份**发起的真实调用，不是"服务在线"或"他人可用"；
- **明确空结果不算就绪**（``metric_count == 0`` 判 ``waiting``）——一次没报错的空查询
  只证明连接通了，证明不了这个人的权限范围已经生效；
- 结论**绑定 (用户, 权限版本)**，见 :class:`ReadinessBinding` 与
  :meth:`ReadinessSession.applies_to`。

## 五路互斥

| 结果 | 什么时候 | 是不是终态 |
|---|---|---|
| ``ready`` | 探针以该用户令牌成功返回，且看见的指标条数 > 0 | 终态（唯一的成功） |
| ``no_permission`` | **数据库侧**该用户当前权限聚合为空——压根没有可等的东西 | 终态，**一次探针都不发** |
| ``waiting`` | 发布已读回一致，但探针还没成功，预算未耗尽 | 中间态 |
| ``timed_out`` | 十五分钟（配置值）预算耗尽仍未成功 | 终态，上游据此转运维 |
| ``technical_failure`` | 探针基础设施异常（网络、协议、解密、数据库） | 中间态，但**独立记录** |

三条不能合并的理由：

1. **``no_permission`` 只能从数据库侧得出，绝不从 MCP 的拒绝反推。** MCP 拒绝一次可能是
   它还没刷新缓存（每十五分钟拉一次表），把它读成"这个人没权限"会让一个权限完全正常的
   用户拿到"请去银河申请权限"的终态提示。因此本模块的 ``no_permission`` 分支**在探针之前**
   由 :func:`evaluate_permission_presence` 判定，判定输入是我们自己发布出去的那份
   ``permissions`` 文本。
2. **``technical_failure`` 不是 ``waiting``。** 前者要人去看网络、凭据或数据库；后者
   通常下一轮就好了。混在一起会让一次探针根本没跑起来的部署，表现成"权限同步比较慢"。
3. **``technical_failure`` 更不是 ``ready`` 或 ``no_permission``。** 这是本模块最重要的
   失败关闭方向：任何我们看不懂的响应形状、任何解不开的令牌，都只能落进技术失败，
   绝不能凑成一次"就绪"。

## 权限判定按**回退制**

需要本地回答"这个人有没有权限"时，一律走
:func:`lingxi.core.permission.publish_row.lookup_metrics`：先看
``permissions[company_id]``，该键不存在才回退 ``permissions["*"]``，**不取并集**
（产品负责人 2026-08-17 权威设计，留痕见 #155）。旧系统 biai-agent 的并集归一是错的，
方向是多给权限，本仓库任何一处都不得沿用。

## 节奏与预算是**受控可配置的合法值**

「立即一次，此后每三分钟一次，十五分钟停止」写在 :class:`ReadinessSchedule` 里，
默认值就是合同值。它**只接受合法区间内的取值**（见该类的校验）：零、负数、超上限、
布尔、非整数一律拒绝，且**没有"配错了就回退默认"的分支**——一个静默回退的配置项等于
没有配置项，而这里配错的方向是"无上界地一直探下去"。

**"发几次"与"最晚什么时候收口"是两件事，分别由两个东西决定**：

- **发几次**：只看 :meth:`ReadinessSchedule.attempt_offsets` 这张计划表
  （默认 ``(0, 180, 360, 540, 720, 900)``，六次）。执行时**不**再拿"现在过没过预算"
  去二次否决某一次尝试——真实 ``sleep(180)`` 每次都会多回来几毫秒，累计到第六次时
  ``now`` 必然略大于 ``started + 900``，于是合同要求的最后一次探针永远被跳过，
  实际只发五次。二级独立审查用抖动时钟实测到了这个偏差。
- **最晚什么时候收口**：``预算 + 单次探针超时``（:attr:`ReadinessSchedule.hard_deadline`）。
  配置层强制单次探针超时 ≤ 轮询间隔，因此最后一次探针最迟在这个时刻之前结束；
  在这个窗口内返回的成功**仍然有效**。硬上界只在探针链真的失控（注入的探针无视超时）
  时才触发，正常与抖动时钟下永远不触发。

验收窗口用**最小合法配置**（``ReadinessSchedule(interval_seconds=1,
budget_seconds=1, probe_timeout_seconds=1)``）把等待缩短到两次尝试，不靠真的等十五分钟；
时钟与 ``sleep`` 都是注入的，纯单测里连一秒都不用等。**``sleep`` 是必填参数**：缺省会让
六次探针背靠背发完、立刻判超时，十五分钟的等待被压成毫秒级，而日志与记录看起来完全正常。

## 本模块不做的事

- **不判断权限版本有没有被推进。** 结论只带着 :class:`ReadinessBinding`，上游在使用前
  必须调 :meth:`ReadinessSession.applies_to`。要在这里判断就得读库，而 ``core`` 不读库；
  引入第六种"已被取代"的结果也会破坏五路互斥这条合同。
- **不发告警、不改用户状态。** ``on_alert`` 只是注入点，形状与
  ``core/permission/publish.PermissionPublishExecutor`` 一致。把 ``provisioning_state``
  推到 ``active`` / ``manual_review`` 属开通编排（Epic D），不在本 Story。
- **不装配任何常驻消费者。** 真实调用方是 Epic D 的 ``OnboardingRunner`` 与每日权限刷新
  职责，当前进程闭包里没有它——与 ``core/permission/publish.py`` 同一姿态。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Protocol

from lingxi.core.permission.publish_row import lookup_metrics, parse_permissions

logger = logging.getLogger(__name__)

_UTC = timezone.utc

#: 合同节奏：立即一次，此后每三分钟一次，十五分钟停止（产品合同「首次开通」第 8 条）。
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

#: 一次确认最多允许发多少次探针。它挡的是 ``interval=1, budget=3600`` 这类**合法却荒谬**
#: 的组合：3601 次调用会把问数 MCP 的配额打光。做法是**拒绝这样的配置**，不是悄悄截断——
#: 截断会让实际节奏与配置说的不是一回事，而那种偏差没人看得见。
MAX_ATTEMPTS = 64


class ReadinessOutcome(Enum):
    """一次判定的结果。五路互斥，语义见模块文档的表。"""

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

    ``denied`` 区分两件完全不同的事：

    - ``True``：问数 MCP **完整返回并明确拒绝**（鉴权或权限错误）。它还没看见我们发布的
      那一行——这是"同步中"，落 ``waiting``。
    - ``False``：**结果不明**（网络、协议、响应形状、令牌解密、数据库）。我们的探针根本
      没跑起来，落 ``technical_failure``。

    分类纪律沿用 ``adapters/feishu_delivery.py`` 的白名单口径：只有"完整返回 + 明确拒绝"
    才算明确，其余一切都是结果不明。方向是安全的——把明确拒绝误判成技术失败只会多等
    几轮，反过来则会让一次网络抖动被读成"这个人被 MCP 拒了"。
    """

    def __init__(self, code: str, *, denied: bool = False) -> None:
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
    """轮询节奏与总预算。**只接受合法区间内的取值，没有回退默认的分支。**

    ``interval_seconds`` 是两次探针之间的间隔，``budget_seconds`` 是从第一次探针算起的
    总预算。第一次探针在 ``t=0``（发布读回一致后**立即**一次），之后每 ``interval``
    一次，直到下一次的时刻**超过**预算为止。
    """

    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    budget_seconds: int = DEFAULT_BUDGET_SECONDS
    probe_timeout_seconds: int = DEFAULT_PROBE_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        _require_seconds(
            "轮询间隔", self.interval_seconds, MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS
        )
        _require_seconds("总预算", self.budget_seconds, MIN_BUDGET_SECONDS, MAX_BUDGET_SECONDS)
        _require_seconds(
            "单次探针超时", self.probe_timeout_seconds, MIN_PROBE_TIMEOUT_SECONDS, MAX_INTERVAL_SECONDS
        )
        if self.interval_seconds > self.budget_seconds:
            raise ValueError("轮询间隔不得大于总预算：那样只会发出一次探针，配置在说谎")
        if self.probe_timeout_seconds > self.interval_seconds:
            # 这条约束是**结论落地时刻的上界**（见 :attr:`hard_deadline` 与模块文档
            # 「节奏与预算」）。单次探针允许比一个间隔还久时，慢探针会一路把后续尝试
            # 往后推，整轮确认可以远远超过十五分钟才收口——而上游正是按十五分钟给用户
            # 承诺的。装配层必须让探针传输层的超时与这里一致。
            raise ValueError("单次探针超时不得大于轮询间隔：否则整轮确认会无上界地拖长")
        planned = self.budget_seconds // self.interval_seconds + 1
        if planned > MAX_ATTEMPTS:
            raise ValueError(
                f"这组节奏会发出 {planned} 次探针，超过上限 {MAX_ATTEMPTS}；"
                "请调大间隔或调小预算（不截断，因为截断会让实际节奏与配置不一致）"
            )

    def attempt_offsets(self) -> tuple[int, ...]:
        """每次探针相对起点的秒偏移，例如默认值给出 ``(0, 180, 360, 540, 720, 900)``。

        **边界是"小于等于预算"而不是"小于"**：合同说的是"最多等待十五分钟"，落在第
        十五分钟整点的那一次仍在窗口内。把它写成一个能被单独看见、也能被单独改坏的
        纯函数，是为了让"到底会探几次、最后一次在第几秒"可以直接断言，而不用去读一遍
        循环体去推断。

        **这份计划表就是"发几次"的唯一判据。** 执行时不再拿"现在过没过预算"去二次否决
        某一次尝试——真实时钟下 ``sleep(180)`` 每次都会多回来几毫秒，累计到第六次时
        ``now`` 必然略大于 ``started + 900``，于是合同要求的最后一次探针**永远被跳过**，
        实际只发五次（二级独立审查用抖动时钟实测到这一点）。
        """

        offsets: list[int] = []
        offset = 0
        while offset <= self.budget_seconds:
            offsets.append(offset)
            offset += self.interval_seconds
        return tuple(offsets)

    @property
    def max_attempts(self) -> int:
        return len(self.attempt_offsets())

    @property
    def budget(self) -> timedelta:
        return timedelta(seconds=self.budget_seconds)

    @property
    def hard_deadline(self) -> timedelta:
        """结论**最晚**落地的时刻（相对起点）：预算 + 一次探针超时。

        计划表决定"发几次"，这条决定"最坏拖到什么时候"。两者分工明确：

        - 正常与抖动时钟下它永远不触发，因此第六次探针照发（`V-权限-05` 的节奏）；
        - 探针链真的失控时（注入的探针无视超时、每次跑几分钟），它把整轮确认收口在
          ``预算 + 单次超时`` 之内，而不是让"十五分钟"变成没有上界的说法。

        因为 :meth:`__post_init__` 已经要求单次超时 ≤ 间隔，正常路径上最后一次探针
        最迟在 ``budget + probe_timeout`` 结束——**这就是对上游承诺的收口上界**。

        **它在两个时刻各查一次**：发起下一次探针**之前**（还没超才发），以及探针
        **返回之后**（超了就不许判就绪，见
        :meth:`McpReadinessConfirmation._probe_once`）。只查前者是不够的——最后一次探针
        可以在 ``deadline - 1 秒`` 发起、``deadline + 20 秒`` 才返回成功，而那时窗口
        早已关闭。返回后这一查同时也是唯一不依赖"传输层真的遵守了超时"的防线。
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
        if not isinstance(self.user_id, str) or not self.user_id.strip():
            raise ValueError("就绪确认必须绑定用户")
        if isinstance(self.permission_version, bool) or not isinstance(
            self.permission_version, int
        ):
            raise ValueError("权限版本必须是整数")
        if self.permission_version <= 0:
            raise ValueError("权限版本必须为正：0 表示还没有过任何权限决定")


def _require_attempt_shape(
    outcome: "ReadinessOutcome", error_code: str | None, metric_count: int | None
) -> None:
    """五路结论各自的**精确形状**。与迁移 ``0065`` 的 CHECK 一一对应，两处必须同时改。

    这不是防御性冗余，而是把"这条记录到底代表什么"钉死在类型层：二级独立审查实测过，
    只挡 ``ready`` 那一条时，``waiting`` 可以带着 ``metric_count=5`` 落库、
    ``technical_failure`` 可以带着观察值落库——两者读起来都像"探针跑通了看见了指标"，
    而它们恰恰是"没就绪"。

    | 结论 | 错误码 | 观察值 |
    |---|---|---|
    | ``ready`` | **必须没有** | 必须有，且 > 0 |
    | ``waiting`` | 必须有 | 没有（明确拒绝）或恰为 0（空结果） |
    | ``technical_failure`` | 必须有 | **必须没有**（探针没跑通，任何数字都是假的） |
    | ``no_permission`` / ``timed_out`` | 必须有 | **必须没有**（这两路压根不发探针） |
    """

    if isinstance(metric_count, bool) or not isinstance(metric_count, (int, type(None))):
        raise ValueError("可见指标条数必须是整数或缺省")
    if metric_count is not None and metric_count < 0:
        raise ValueError("可见指标条数不得为负")
    if error_code is not None and (not isinstance(error_code, str) or not error_code.strip()):
        # 空串与纯空白等同于"没写"：它们能满足"非 None"，却什么都没说明，而下面正是靠
        # "有没有错误码"判断"未就绪有没有给出原因"。数据库侧同口径（迁移 0065 的
        # ``BTRIM(error_code) <> ''``）。
        raise ValueError("错误码必须是非空字符串，空白不算")
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
        if isinstance(self.attempt_no, bool) or not isinstance(self.attempt_no, int):
            raise ValueError("尝试次序必须是整数")
        if self.attempt_no < 1:
            raise ValueError("尝试次序从 1 开始")
        for label, moment in (("开始", self.started_at), ("结束", self.finished_at)):
            if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
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
        return self.outcome in TERMINAL_OUTCOMES

    @property
    def elapsed_ms(self) -> int:
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    def audit_facts(self) -> dict[str, Any]:
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
        if self.outcome not in TERMINAL_OUTCOMES:
            # 一轮确认只能以终态收口：中间态收口等于让上游拿到一个"还没结束"的结论
            # 却以为可以据此行动。
            raise ValueError("确认结果必须是终态：ready / no_permission / timed_out")
        if not self.attempts:
            raise ValueError("确认结果必须至少包含一次判定记录")
        # 下面两条挡的是**用别人的成功拼出一个自己的成功**（二级独立审查自设计变异）：
        # 没有它们，拿用户 B 的 ready 记录 + 用户 A 的 binding 就能构造出一个
        # `applies_to(A, v)` 为真的对象，而 `applies_to` 只核 binding、不核证据。
        foreign = [item for item in self.attempts if item.binding != self.binding]
        if foreign:
            raise ValueError("确认结果里的判定记录必须全部绑定同一个（用户，权限版本）")
        if self.attempts[-1].outcome is not self.outcome:
            # 收口结论只能是**最后一次判定**本身。允许两者不一致，等于允许一轮以
            # `timed_out` 收尾的确认对外宣称 `ready`。
            raise ValueError("确认结果必须与最后一次判定的结论一致")

    @property
    def ready(self) -> bool:
        return self.outcome is ReadinessOutcome.READY

    @property
    def attempt_count(self) -> int:
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

    输入是我们自己发布出去的那份 ``permissions`` 文本（``PublishRow.permissions``，
    也就是 outbox 快照里的那一串），判定走
    :func:`lingxi.core.permission.publish_row.lookup_metrics` 的**回退制**：给定公司时
    先看该公司键、不存在才回退 ``"*"``，**不取并集**。

    **刻意不接受"从 MCP 拒绝反推"这条输入。** MCP 拒绝一次可能只是它还没刷新缓存
    （每十五分钟拉一次表），据此宣布"这个人没权限"会让一个权限完全正常的用户收到
    "请去银河申请权限"的终态提示——一个我们自己造出来的、用户无法自救的死结。

    权限文本读不懂时抛 ``ValueError`` 而不是判"没有权限"：读不懂是本侧缺陷，
    把它折成一个用户可见的终态，等于用一句确定的错话掩盖一次不确定的故障。
    """

    return bool(lookup_metrics(parse_permissions(permissions), company_id))


class McpProbe(Protocol):
    """就绪探针的可注入面。实现见 ``adapters/query_mcp_probe.py``。

    **签名里没有令牌。** 探针自己按 ``user_id`` 去取该用户的明文令牌（并在用完后丢弃），
    因此明文一次都不进 ``core``，也不可能被塞进任何结果对象、审计事实或日志。这是
    "明文只在签发瞬间与按需解密时存在于内存"这条纪律在类型上的落点。
    """

    def list_metrics(self, *, user_id: str) -> int:
        """以该用户身份执行一次 ``list_metrics``，返回**可见指标条数**。

        失败一律抛 :class:`McpProbeError`；其余异常原样上抛（未预期的异常不该被折成
        "技术失败"，那会让真正的缺陷每轮安静地重试下去）。
        """
        ...


class ReadinessCheckStore(Protocol):
    """就绪判定记录的最小写入面。实现见 ``adapters/postgres_mcp_token.py``。"""

    def record_attempt(self, attempt: ReadinessAttempt) -> str: ...


class _AuditSink(Protocol):
    def record(self, action: str, /, **fields: object) -> None: ...


def classify_probe(metric_count: object) -> tuple[ReadinessOutcome, str | None]:
    """把一次**成功返回**的探针结果分成 ``ready`` 或 ``waiting``（纯函数）。

    - 条数 > 0 → ``ready``；
    - 条数 == 0 → ``waiting``。**明确空结果只证明这次查询没有报错**，证明不了这个人的
      权限范围已经生效（产品合同「问数 MCP」一节，`V-权限-06`）。MCP 每十五分钟才拉一次
      发布表，刚发布完拿到空列表是常态；把它读成就绪，等于在权限还没生效时宣告开通成功。
    - 不是非负整数 → ``technical_failure``。适配器返回了我们读不懂的东西，属本侧缺陷，
      **绝不能凑成一次就绪**。
    """

    if isinstance(metric_count, bool) or not isinstance(metric_count, int) or metric_count < 0:
        return ReadinessOutcome.TECHNICAL_FAILURE, "invalid_metric_count"
    if metric_count > 0:
        return ReadinessOutcome.READY, None
    return ReadinessOutcome.WAITING, "empty_metrics"


class McpReadinessConfirmation:
    """把「判权限 → 立即探一次 → 每三分钟再探 → 十五分钟收口」串起来。

    只编排注入的接口，不做 I/O：探针、记录存储、审计、时钟与 ``sleep`` 全部注入
    （形状与 ``core/permission/publish.PermissionPublishExecutor`` 一致）。因此
    "十五分钟到底会探几次、什么时候判超时"能在纯单测里用假时钟证伪，不用真的等。

    **记录写失败不吞**：先留一条审计说明这次记账没成功，再原样上抛。一次没记下来的探针
    会让"十五分钟是不是现实的上限"这个复审问题失去样本，而那正是这张表存在的理由。
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
        if not isinstance(schedule, ReadinessSchedule):
            # 只接受**已经过校验的**配置对象：允许在这里传一对裸整数，等于给每个调用点
            # 各开一次绕过校验的口子。
            raise TypeError("轮询节奏必须是已校验的 ReadinessSchedule")
        if not callable(clock):
            raise TypeError("时钟必须可调用：注入它才能不真的等十五分钟")
        if not callable(sleep):
            # ``sleep`` **必填**，且没有"缺省就不等"的分支。此前它可以是 ``None``，
            # 默认构造出来的确认会把六次探针背靠背发完、立刻判超时——十五分钟的等待
            # 被压成毫秒级，而每一条日志、每一条记录看起来都完全正常。装配层漏注入一个
            # 参数就静默塌成这样，是不能接受的失败形态，因此在类型上就不给它这个选项。
            raise TypeError("sleep 必须可调用：缺省会把十五分钟的等待压成瞬时")
        self._probe = probe
        self._store = store
        self._audit = audit
        self._clock = clock
        self._sleep = sleep
        self._schedule = schedule
        self._on_alert = on_alert

    @property
    def schedule(self) -> ReadinessSchedule:
        return self._schedule

    def confirm(
        self, binding: ReadinessBinding, *, permissions: str, company_id: Any = None
    ) -> ReadinessSession:
        """跑完一轮确认，返回终态结果。

        ``permissions`` 是**我们发布出去的那一版权限文本**（``PublishRow.permissions``）。
        它先决定 ``no_permission`` 分支：聚合为空就没有任何可等的东西，**一次探针都不发**。

        次序是刻意的：

        1. **先判有没有权限，再谈就绪。** 反过来会让一个本来就没有权限的人白等十五分钟，
           最后以"超时"转运维——而正确结论是"去银河申请权限"，两者的用户出口完全不同。
        2. **立即探一次**（``t=0``）。发布刚读回一致，MCP 有可能已经在上一轮拉表里带上了
           这个人；不试就等三分钟是白等。
        3. **此后按节奏探，直到预算耗尽判超时。** 超时是**预算耗尽**才成立的判定，
           不能因为连续几次技术失败就提前收口——那会把一次网络抖动说成"权限同步失败"。
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

        # **计划表决定发几次，硬上界只防失控的探针链**（见 ``ReadinessSchedule``）。
        # 不再用"现在过没过预算"去二次否决某一次尝试：真实 ``sleep`` 每次多回来几毫秒，
        # 累计到第六次时 ``now`` 必然略过 ``started + 900``，合同要求的最后一次探针
        # 就永远发不出去（二级独立审查用抖动时钟实测到）。
        hard_deadline = started + self._schedule.hard_deadline
        attempts: list[ReadinessAttempt] = []
        for attempt_no, offset in enumerate(self._schedule.attempt_offsets(), start=1):
            if attempt_no > 1:
                self._wait_until(started + timedelta(seconds=offset))
                if self._now() > hard_deadline:
                    # 只有探针链真的失控（注入的探针无视超时）才会走到这里：正常与抖动
                    # 时钟下 ``now`` 离硬上界还差一整个探针超时。
                    break
            attempt = self._probe_once(binding, attempt_no, hard_deadline)
            attempts.append(attempt)
            if attempt.ready:
                return self._finish(binding, ReadinessOutcome.READY, tuple(attempts))
            if attempt.outcome is ReadinessOutcome.TIMED_OUT:
                # 探针**返回得太晚**：成功已经落在承诺窗口之外（见 :meth:`_probe_once`）。
                return self._finish(binding, ReadinessOutcome.TIMED_OUT, tuple(attempts))
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

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        moment = self._clock()
        if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
            # naive 时间会让跨时区部署算出互相矛盾的预算，而预算正是"等了多久"的唯一依据。
            raise ValueError("注入的时钟必须返回带时区的时间")
        return moment.astimezone(_UTC)

    def _wait_until(self, due: datetime) -> None:
        """等到计划时刻。**按绝对时刻等，不按间隔累加**——后者会把每一次的调度误差
        累积下去，第六次就漂到预算之外。已经过了计划时刻就立刻开始（慢探针的情形）。"""

        remaining = (due - self._now()).total_seconds()
        if remaining > 0:
            self._sleep(remaining)

    def _probe_once(
        self, binding: ReadinessBinding, attempt_no: int, hard_deadline: datetime
    ) -> ReadinessAttempt:
        started = self._now()
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
        outcome, error_code = classify_probe(observed)
        # 只有被 :func:`classify_probe` 认可的观察值才进记录：读不懂的返回值落技术失败，
        # 那一路必须不带观察值（否则它看起来像一次跑通了的探针，且负数还会撞上
        # 迁移 0065 的 CHECK，把一次本该记下来的失败变成整轮确认崩溃）。
        counted = None if outcome is ReadinessOutcome.TECHNICAL_FAILURE else int(observed)
        finished = self._now()
        if outcome is ReadinessOutcome.READY and finished > hard_deadline:
            # **成功来得太晚了。** 承诺是"结论最晚在 预算 + 单次探针超时 内落地"，而这次
            # 探针在窗口之外才返回——发起前的检查管不住它（那时还没超），只有返回后再看
            # 一眼才管得住。这也是唯一不依赖"传输层真的遵守了超时"的防线：探针超时配大了、
            # 或注入的探针根本不看超时，上界都只能靠这里成立。
            #
            # 降级成 ``timed_out`` 而不是记一条 ``ready`` 再让上层否决：那样
            # ``mcp_sync_check`` 里会留下一行 ``ready``，而这一轮的结论是超时，
            # 读表的人会看到两个互相矛盾的事实。观察值一并丢弃（``timed_out`` 不得携带）。
            outcome, error_code, counted = (
                ReadinessOutcome.TIMED_OUT,
                "success_after_deadline",
                None,
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


__all__ = [
    "CONTRACT_SCHEDULE",
    "DEFAULT_BUDGET_SECONDS",
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_PROBE_TIMEOUT_SECONDS",
    "MAX_ATTEMPTS",
    "MIN_PROBE_TIMEOUT_SECONDS",
    "MAX_BUDGET_SECONDS",
    "MAX_INTERVAL_SECONDS",
    "MIN_BUDGET_SECONDS",
    "MIN_INTERVAL_SECONDS",
    "McpProbe",
    "McpProbeError",
    "McpReadinessConfirmation",
    "ReadinessAttempt",
    "ReadinessBinding",
    "ReadinessCheckStore",
    "ReadinessOutcome",
    "ReadinessSchedule",
    "ReadinessSession",
    "TERMINAL_OUTCOMES",
    "classify_probe",
    "evaluate_permission_presence",
]
