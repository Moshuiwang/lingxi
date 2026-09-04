"""首次开通编排在 ``lingxi-scheduler`` 的装配（Epic D / S-D-02）。

``core/identity/onboarding_runner.py`` 是判定与次序，本模块是**把真实存储与外部接口接上去
的那一层**：读配置、建适配器、把它们喂给 :class:`~lingxi.core.identity.onboarding_runner.
AutoOnboardingRunner`，并在装配处就把几条只会在生产暴露的错配变成启动期失败。

## 为什么住在 scheduler 而不是 gateway

产品负责人 2026-08-18 裁定（决策记录见
``docs/决策记录/2026-08-18-首次开通编排住在scheduler.md``）。判据是**凭据边界**：在职状态
必须实时回读飞书成员详情（`V-开通-07`），而那需要专用授权主体派生的短期令牌；那条一次性
``refresh_token`` 全系统只允许一个消费者，而它已经在这个进程里（``CredentialRotationLoop``
+ ``DerivedAccessTokenHolder``，#215 的形状）。让 gateway 去换等于制造第二条凭据通道——
2026-08-08 授权码被烧那次事故的形状。

因此进程边界是：

```
gateway   ：首聊落 inbound_event（handled_as='auto_provisioning'）+ 立刻回「已收到，正在核对」
scheduler ：本模块每轮认领若干条 → AutoOnboardingRunner（专属线程池）→ 编排自己私聊通知用户
```

代价是**首次开通要等一个扫描周期**才开始（产品负责人已知情并接受）。合同要求的第一条提示
仍然由 gateway 即时发出，不受这段延迟影响。

## 前置不齐就**不装配**，而不是装一个一直失败的

形状照 ``_build_permission_refresh_duty``（`V-花名册-29` 的同一条纪律：缺项只报变量名、审计
**恰一条**、其余职责照常运行）。缺任何一项前置时 :func:`build_onboarding_duty` 返回 ``None``，
于是**没有任何人认领** ``auto_provisioning`` 事件——它们原样留在库里，不会被一个装不全的
编排认领走再烧掉。这比 gateway 时代的失败关闭桩更安全：桩会「认领即平账」。

## 三条装配断言（Epic C 在 PR #218 / #221 交给 Epic D 的清单）

1. **执行级硬截止**：:class:`HardDeadlineProbe` 给同步探针套一层看门狗。就绪状态机自己的
   上界是「探针**返回之后**再看一眼时刻」，它挡不住一个**永不返回**的探针——那条链会把一个
   开通线程永久占住，而所有账面（记录、日志）都看起来只是「还在等」。
2. **``probe_timeout ≤ interval`` 且与传输超时相等**：两者不一致时，就绪那一侧算出来的
   「结论最晚什么时候落地」就是假的。前半句由 ``ReadinessSchedule`` 自己守，这里仍然显式
   断言一次——它是被交办的清单项，不能因为「上游大概会管」而消失。
3. **注入单调时钟**：:func:`monotonic_utc_clock`。就绪确认的预算、硬上界与「成功来得太晚」
   全部靠时间差判定，而 ``datetime.now()`` 会因为 NTP 回拨往回走——一次回拨足以让一条已经
   超窗的成功被判成有效。

## 第四条断言（本轮新增）：**认领量必须被执行器容量压住**

:func:`assert_claim_limit_follows_capacity` 在装配处确认认领循环真的绑上了执行器的剩余容量。
认领即记账，而执行器满位时只能拒绝——两者不联动时，差额就是被**永久烧掉**的事件数
（默认值曾经是「一轮最多认领 20 条、执行器容量 12」，第 13 条起必然被烧）。这条断言接替了
搬迁前那条「双注入点必须同一实例」：那条挡的是「对账落回失败关闭桩、认领即平账」，
而搬迁之后只剩一个注入点，同样的伤害改从容量差额这个入口进来。

## 第五条断言（本轮新增，Issue #282）：**停摆租约必须长于链预算**

:func:`assert_stalled_lease_exceeds_chain_budget` 挡的是
:class:`~lingxi.apps.scheduler.stalled_provisioning.StalledProvisioningDuty` 的租约常量
与就绪节奏、发布等待上界之间的隐性耦合——三个数字分别定义在三个不同的模块里，靠人记住
"改这个之前要看那个"迟早会有一次漏看。断言把这条耦合从注释升级成装配期真的会炸的事实。

## 停机接线（Issue #284 C 组 #8，Trace #373 D7 裁定修复）

:meth:`OnboardingExecutor.stop`/:meth:`OnboardingExecutor.join` 是 #266 就修好的停机竞态
路径（见两方法各自文档字符串），但在本次修复之前**生产没有任何调用方**——``main()`` 只是
让 :class:`~lingxi.apps.scheduler.loop.SchedulerLoop` 停止再调用各职责的 ``run_once()``，
从来没人回头问过开通执行器自己那 ``config.onboarding_workers`` 条工作线程收没收工。它们是
``daemon`` 线程，进程退出时会被直接砍断——不是数据风险（``_stop_guard`` 已经让在途链在
检查点之间安全收口并释放认领，见 ``core/identity/onboarding_runner.py``），但确实违背
"停止领取新工作、把已经领取的那一次做完，再退出"这条既有停机纪律（本包 ``__init__.py``
模块文档「退出语义」一节）；不接线时那句承诺只对同一线程内跑的职责成立，对开通执行器
自己的独立线程池是空话。

修法照抄 ``apps/gateway/__init__.py`` 对投递线程的既有形状（"先停止领取、再在预算内等它
收工，超时不再等"）：:func:`_build_onboarding_duty` 把构造好的 ``executor`` 挂在返回的
``duty``（``OnboardingReconciler``）对象上一个动态属性 ``onboarding_executor``——不改
``core/conversation/onboarding_recovery.py`` 的类定义（那是纯 core 层，不该认识
``OnboardingExecutor`` 这个 apps/scheduler 层的概念），只在本模块（apps 层）从外部挂一个
后续可以取回的句柄。``main()`` 在 :meth:`~lingxi.apps.scheduler.loop.SchedulerLoop.
run_forever` 返回之后调用 :func:`join_onboarding_executors`，遍历 ``loop.duties`` 找到
挂了这个句柄的那个（当前只可能有一个，未来若有第二个开通编排实例也天然支持），依次
``stop()`` 再 ``join(timeout=ONBOARDING_SHUTDOWN_JOIN_TIMEOUT_SECONDS)``。收工预算取值
同 ``apps/gateway/config.py::shutdown_timeout_seconds`` 的既有默认值（20 秒）——同一条
"给在途工作一个有界窗口，不是无限等、也不是完全不等"纪律的第三次应用（gateway 投递线程、
组织快照同步的整轮 join 之后，这是第三处）。超时未收工只留一条响亮日志，不阻塞进程退出：
线程本身是 ``daemon``，进程退出时无论如何都不会被它焊住。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from lingxi.adapters.postgres_local_permission import (
    PostgresLocalPermissionOverrideStore,
    local_override_reader,
)
from lingxi.apps.scheduler.audit import AuditSink
from lingxi.apps.scheduler.config import SchedulerConfig
from lingxi.apps.scheduler.permission_publish import PermissionPublishDuty
from lingxi.core.identity.innertest_roster_gate import build_innertest_roster_gate
from lingxi.core.permission.mcp_readiness import ReadinessSchedule
from lingxi.core.permission.metric_translation import metric_translation_available

logger = logging.getLogger(__name__)

_UTC = UTC

#: 执行级硬截止相对单次探针超时的余量。给传输层自己的超时留出返回的机会——看门狗只该在
#: 传输层**根本不遵守**超时时才动手，不该抢在它前面把一次正常的慢响应判死。
PROBE_WATCHDOG_MARGIN_SECONDS = 5.0

#: 一条 ``auto_provisioning`` 事件落库多久之后可以被认领。
#:
#: 搬迁**之前**这个窗口是三十分钟：那时 gateway 在自己的进程里同步跑开通，窗口必须宽于
#: 十五分钟的同步预算，否则对账会把一条**正在正常执行**的开通判成孤儿再触发一次。
#: 搬迁**之后**那条理由消失了——认领即记账，正在跑的那一条 ``onboarding_dispatched_at``
#: 已经非空，压根不在候选里；同一个人的并发也被编排的进程内去重挡住。
#:
#: 于是窗口只剩一个用途：**让 gateway 的第一条「已收到，正在核对」先落地**。取五秒。
#: 首次开通的实际首触延迟 = 这个窗口 + 至多一个 ``SchedulerLoop`` 周期。
DISPATCH_AFTER = timedelta(seconds=5)

#: 工作线程在没有新任务时重新检查一次"是否该自行退出"的轮询间隔（外部集成面审查 F2）。
#:
#: ``OnboardingExecutor.stop()`` 尝试给每条线程放一个哨兵（``None``），但队列满时那次
#: ``put_nowait`` 会静默失败——工作线程因此不能把"正确退出"完全托付给一次可能失败的
#: 哨兵投递。取而代之，``_loop`` 用带超时的 ``queue.get()`` 顶替原来无超时的阻塞调用：
#: 每等待这么久还没有新任务，就主动复查一次 ``_stopping`` 与队列是否已排空。
#:
#: 这也堵上了一条不需要队列满就能触发的**竞态**：线程在"队列还有活"这个判断和真正
#: 调用 ``get()`` 之间，最后一条任务被另一条线程抢先取走——旧实现里这次 ``get()``
#: 没有超时，会永久挂起（哨兵已经在 ``stop()`` 时被丢弃，不会再补）。取值 1 秒：
#: 停机预算通常以秒计，1 秒的额外等待可以接受；生产代价是空闲线程每秒多醒一次，
#: 可忽略不计。
STOP_POLL_INTERVAL_SECONDS = 1.0


# ----------------------------------------------------------------------
# 装配断言 3：单调时钟
# ----------------------------------------------------------------------


def monotonic_utc_clock() -> Callable[[], datetime]:
    """返回一个**永不倒流**的带时区时钟。

    起点取一次墙钟（因此落库的时刻仍然是可读的真实时间），之后的每一次读取都按
    ``time.monotonic()`` 的增量推进。NTP 回拨、手工改时间都不会让它往回走。

    为什么这件事在这条链上要紧：就绪确认的预算、硬上界和"成功来得太晚"三条判定全部是
    时间差，而阻塞式确认会真的等十五分钟——那正是一次 NTP 校正最可能落进来的窗口。
    时钟往回跳一分钟，一次**已经超窗**的成功就会被算成还在窗口内。

    代价如实登记：进程长期运行时它与墙钟会缓慢漂移（monotonic 不含休眠校正）。这条链上
    唯一消费它的是"两个时刻之间差了多久"，漂移不影响；需要绝对时刻的地方（``updated_at``、
    审计时间）另有各自的来源。
    """

    base_wall = datetime.now(_UTC)
    base_monotonic = time.monotonic()

    def now() -> datetime:
        return base_wall + timedelta(seconds=time.monotonic() - base_monotonic)

    return now


# ----------------------------------------------------------------------
# 装配断言 1：执行级硬截止
# ----------------------------------------------------------------------


class HardDeadlineProbe:
    """给同步探针套一层**执行级**看门狗（Epic C 交办清单第 ①条）。

    就绪状态机的上界是"探针返回之后再看一眼现在几点"，因此它管得住"返回得太晚"，
    管不住"**永远不返回**"。后者在生产里是真实形状：一个没设超时（或超时被中间件吃掉）
    的 HTTP 调用可以挂住整条链，而记录与日志看上去只是"还在等待"——十五分钟的承诺
    因此变成没有上界的说法，那个开通线程也再也回不来。

    做法是把每一次 ``list_metrics`` 放进一条**一次性的守护线程**，主线程按
    ``单次探针超时 + 余量`` 等它。超时未回就抛 ``McpProbeError``（``denied=False``，
    因此落**技术失败**这一路：它是"我们的探针没跑起来"，不是"MCP 拒绝了这个人"）。

    **被放弃的那条线程不会被强杀**——Python 没有安全的线程终止原语，强杀会让传输层
    的锁与连接留在不确定状态。它会在底层套接字自己超时后结束；线程是 ``daemon``，
    因此即使它真的永不结束，也不会挡住进程退出。这条代价如实登记：真正的修复是让传输层
    自己遵守超时，看门狗只保证**这条开通链**不被它拖住。
    """

    def __init__(self, *, probe: Any, timeout_seconds: float) -> None:
        if probe is None:
            raise TypeError("硬截止看门狗必须包住一个真实探针")
        if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise ValueError("硬截止必须是正数秒")
        self._probe = probe
        self._timeout_seconds = float(timeout_seconds)

    @property
    def timeout_seconds(self) -> float:
        """透传被包住那个探针的传输超时，供装配断言 2 逐值比对。"""

        return getattr(self._probe, "timeout_seconds", self._timeout_seconds)

    def list_metrics(self, *, user_id: str) -> int:
        from lingxi.core.permission.mcp_readiness import McpProbeError

        outcome: list[Any] = []
        failure: list[BaseException] = []

        def call() -> None:
            try:
                outcome.append(self._probe.list_metrics(user_id=user_id))
            except BaseException as error:  # 原样带回主线程再抛
                failure.append(error)

        worker = threading.Thread(target=call, name="lingxi-gateway-mcp-probe", daemon=True)
        worker.start()
        worker.join(timeout=self._timeout_seconds)
        if worker.is_alive():
            # 探针没有在硬截止内返回。**不等它**，也不假装它失败得有理由——
            # 只留一个专用错误码，让"探针链失控"在记录里能被单独数出来。
            logger.error("问数 MCP 探针超过执行级硬截止仍未返回，本次判技术失败")
            raise McpProbeError("probe_hard_deadline_exceeded")
        if failure:
            raise failure[0]
        return int(outcome[0])


# ----------------------------------------------------------------------
# 执行器
# ----------------------------------------------------------------------


class OnboardingExecutor:
    """开通链的专属线程池。**不与长连接线程、投递线程共用任何一条**。

    这是 Issue #65 钉在开工卡上的「共用线程复核」的落地：真实编排单次可达分钟级
    （产品合同允许权限同步等到十五分钟），跑在长连接线程上会让 gateway 十五分钟收不到
    任何消息，跑在投递线程上会让十五分钟一条投递都发不出去。

    **队列有界**，满了就拒绝而不是无限堆积：一次外部故障导致所有链都卡在十五分钟等待时，
    继续排队只会让排在后面的用户在半小时后才收到一条早已过时的结论。拒绝会让
    ``AutoOnboardingRunner.start`` 返回明确的内部故障终态——用户当场知道要找管理员。

    线程是 ``daemon``：停机预算耗尽时进程不会被一条还在等外部响应的链焊住
    （与 ``apps/gateway`` 投递线程同一条取舍）。
    """

    def __init__(
        self,
        *,
        workers: int,
        backlog: int | None = None,
        should_stop: Callable[[], bool] | None = None,
        stop_poll_seconds: float = STOP_POLL_INTERVAL_SECONDS,
    ) -> None:
        if isinstance(workers, bool) or not isinstance(workers, int) or workers <= 0:
            raise ValueError("开通执行器至少要有一条线程")
        if not isinstance(stop_poll_seconds, (int, float)) or stop_poll_seconds <= 0:
            raise ValueError("停机轮询间隔必须是正数秒")
        self._workers = workers
        self._backlog = backlog if backlog is not None else workers * 2
        self._queue: queue.Queue[Callable[[], None] | None] = queue.Queue(maxsize=self._backlog)
        self._stop_poll_seconds = float(stop_poll_seconds)
        self._inflight = 0
        self._inflight_lock = threading.Lock()
        # **入队闸**：把「检查停机」与「入队」串成一个不可分割的动作，`stop()` 置位与
        # 放哨兵也在同一把锁内。没有它存在一条确定性竞态：submit 检查停机为假 → 被调度
        # 出去 → stop() 置位并放哨兵 → 工作线程取走哨兵全部退出 → submit 恢复、入队成功
        # 并返回 True → **已经没有任何线程会执行它**，那条链既不通知也不释放认领，事件
        # 被永久烧掉。
        self._gate = threading.Lock()
        self._stopping = threading.Event()
        self._should_stop = should_stop or (lambda: False)
        self._threads = [
            threading.Thread(
                target=self._loop, name=f"lingxi-gateway-onboarding-{index}", daemon=True
            )
            for index in range(workers)
        ]

    def start(self) -> None:
        for thread in self._threads:
            thread.start()

    def free_slots(self) -> int:
        """本轮还能收下几条 = 队列剩余位（保守估计）。

        **认领方必须按这个数认领**（:func:`assert_claim_limit_follows_capacity`）：认领即记账，
        而满位时 ``submit`` 只能拒绝；两者不联动时差额就是被永久烧掉的事件数。

        取**保守的下界**：只算队列里还空着的位置，不把"正在跑的那几条马上就会腾出线程"
        算进去。少认领一条下一轮照捞，多认领一条就要靠释放路径救回来——两种错的代价
        完全不对称。
        """

        if self._stopping.is_set() or self._should_stop():
            return 0
        # 只算队列里还空着的位置，**不把「正在跑的那几条马上会腾出线程」算进去**。
        # 它只是给认领方的上界；真正的原子判定在 :meth:`submit` 的入队闸里，因此即使这个
        # 数在读到与用掉之间过时，最坏结果也只是 submit 返回 False → 认领方释放认领 →
        # 下一轮重捞，不会丢事件。
        return max(0, self._backlog - self._queue.qsize())

    def submit(self, task: Callable[[], None]) -> bool:
        """排一条链。队列满或已停机时返回 ``False``，**不阻塞调用线程**。"""

        with self._gate:
            # 检查与入队必须在同一把锁内，理由见 ``self._gate`` 的注释。
            if self._stopping.is_set() or self._should_stop():
                return False
            try:
                self._queue.put_nowait(task)
            except queue.Full:
                logger.warning("开通执行器队列已满，本次开通不受理")
                return False
            return True

    def stop(self) -> None:
        """停止领取新链。

        **刻意不排空队列、不丢弃已排队的任务。** 那些任务对应的事件在认领那一刻就已经被
        记账了，直接丢掉等于把它们永久烧掉；而每条链的第一件事就是问一次停机
        （``AutoOnboardingRunner._run`` 的 ``_stop_guard``），停机之后被取到的那些会立刻
        中止**并把认领放回去**。因此让它们照常出队反而是唯一不丢事件的走法。**这条产品
        语义本次未改动**：下面仍然尝试给每条线程放一个哨兵、仍然不排空队列、``queue.Full``
        仍然被静默忽略。

        在途那一条同样不打断：它在每一步之间看停止标志，自己收口并释放。

        **哨兵投递只是快路径，不是工作线程退出的唯一依据**（外部集成面审查 F2）：队列满时
        某几条哨兵会投递失败，这里不再假装"工作线程自己会看到标志"就一定成立——真正兜底
        的是 :meth:`_loop` 里带超时的 ``get()``，每 ``STOP_POLL_INTERVAL_SECONDS`` 秒自己
        复查一次 ``_stopping`` 与队列是否已排空，不依赖这次投递是否成功。
        """

        with self._gate:
            # 与 ``submit`` 同一把锁：置位之后就不可能再有新任务排到哨兵后面去。
            self._stopping.set()
            for _ in self._threads:
                try:
                    self._queue.put_nowait(None)
                except queue.Full:
                    # 队列满：这条哨兵投不进去。**不是问题**——工作线程不靠它退出，
                    # 见 :meth:`_loop` 的超时轮询。
                    pass

    def join(self, timeout: float) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

    @property
    def alive(self) -> bool:
        return any(thread.is_alive() for thread in self._threads)

    def _loop(self) -> None:
        """单条工作线程的主循环。

        **不对正确退出依赖一次可能失败的哨兵投递**（外部集成面审查 F2）：``self._queue.get()``
        带上 ``self._stop_poll_seconds`` 超时，取代原来无超时的阻塞调用。这堵上一条不需要
        队列满也能触发的竞态——线程在"队列还有活，因此循环回去再 ``get()`` 一次"这个判断
        和真正调用 ``get()`` 之间，最后一条任务被另一条工作线程抢先取走：旧实现里那次
        ``get()`` 没有超时，会永久卡住（哨兵已经在 ``stop()`` 时因为队列满被丢弃，不会再
        补一个进来救它）。带超时之后，即使这次扑空，线程最多等 ``stop_poll_seconds`` 就会
        重新复查一次停止标志与队列是否已空，而不是无限期挂起。

        **不改变** ``stop()`` 的产品语义：已排队的任务照常经由 ``get()`` 正常出队执行，
        本函数从不主动丢弃队列里的内容，只是多了一条"没有新任务时定期看一眼该不该退出"
        的路径。
        """

        while True:
            try:
                task = self._queue.get(timeout=self._stop_poll_seconds)
            except queue.Empty:
                # 这一轮没有等到新任务。若已经进入停机且队列此刻确实空了，自行退出——
                # 不再等一个可能永远不会到来的哨兵。否则回到循环顶端继续等。
                if self._stopping.is_set() and self._queue.empty():
                    return
                continue
            if task is None:
                return
            try:
                task()
            except BaseException as error:  # 一条链的失败不得带走这条线程
                # C-6：不用 `logger.exception`——它会把异常正文写进日志，而这条
                # 兜底捕获的是**整条开通链**的任意异常，psycopg 的唯一键冲突正文里
                # 带着 `Key (feishu_open_id)=(ou_…)`。只记类型名与调用栈帧。
                logger.error(
                    "开通链在执行线程上抛出未捕获异常 error=%s\n调用栈（不含异常正文）：\n%s",
                    type(error).__name__,
                    "".join(traceback.format_tb(error.__traceback__)),
                )
            finally:
                self._queue.task_done()
            if self._stopping.is_set() and self._queue.empty():
                return


#: 停机时等待开通执行器收工的预算（Issue #284 C 组 #8，见模块文档「停机接线」）。
#: 取值同 ``apps/gateway/config.py::shutdown_timeout_seconds`` 的既有默认值：
#: 在途链靠 ``_stop_guard`` 在检查点之间快速收口，正常情况下远快于这个上限；
#: 执行器线程是 ``daemon``，超时未收工也不会阻塞进程退出，只留一条响亮日志。
ONBOARDING_SHUTDOWN_JOIN_TIMEOUT_SECONDS = 20.0


def join_onboarding_executors(duties: Sequence[Any]) -> None:
    """停机收尾：让每一个挂了开通执行器的职责停止领取新链，并在预算内等它收工。

    （Issue #284 C 组 #8，Trace #373 D7 裁定修复；见模块文档「停机接线」一节的完整
    理由。）由 ``main()`` 在 ``SchedulerLoop.run_forever()`` 返回之后调用一次——此时
    全部职责已经停止领取新一轮工作，正是"完成或安全中断在途工作，不是立刻放弃"
    （``V-部署-03``）这条纪律该收口的时机。

    只认**挂在 duty 上的 ``onboarding_executor`` 属性**，不是"扫描全部职责找
    ``OnboardingReconciler`` 实例"——:func:`_build_onboarding_duty` 已经把这层判断
    做过一次（前置不齐时整个职责都不装配，``duty`` 根本不会出现在 ``duties`` 里），
    这里只需要认这一个约定好的接缝，不需要重新认识 ``OnboardingReconciler`` 这个
    类型本身（保持与 ``core/conversation/onboarding_recovery.py`` 的隔离，见模块
    文档「停机接线」）。没有任何职责挂了这个属性时（未装配开通编排、或调用方传入
    了自己的测试替身）本函数是纯粹的空操作。
    """

    for duty in duties:
        executor = getattr(duty, "onboarding_executor", None)
        if executor is None:
            continue
        executor.stop()
        executor.join(timeout=ONBOARDING_SHUTDOWN_JOIN_TIMEOUT_SECONDS)
        if executor.alive:
            logger.error(
                "开通执行器未能在停机预算内收工（%s 秒），进程将不再等待、直接退出",
                ONBOARDING_SHUTDOWN_JOIN_TIMEOUT_SECONDS,
            )


# ----------------------------------------------------------------------
# 小适配器
# ----------------------------------------------------------------------


class RosterRows:
    """把花名册持久快照折成"当前全部行或 ``None``"。

    **不加新鲜度判据**：每日重算要求快照是"今天的"，因为它是一次全量重算；而一次首聊
    开通如果因为快照晚了两小时就告诉用户"没有可用的银河权限"，那是一句错话。快照的新鲜度
    由花名册审计职责保证，这里只区分"有"和"根本没有"。
    """

    def __init__(self, store: Any) -> None:
        self._store = store

    def rows(self) -> Sequence[Mapping[str, Any]] | None:
        snapshot = self._store.load()
        return None if snapshot is None else snapshot.rows


class CatalogNotifier:
    """把终态的内容目录 key 渲染成正文，主动私聊给用户本人。

    渲染走 ``ContentCatalog``，因此每一条用户可见正文都经过版本纪律与可见性检查
    （``config/content.py``）——编排层给的是 key 与变量，不是拼好的句子。
    """

    def __init__(self, *, sender: Any, catalog: Any) -> None:
        self._sender = sender
        self._catalog = catalog

    def send(
        self, *, open_id: str, key: str, values: Mapping[str, object], dedupe_key: str
    ) -> None:
        content = self._catalog.text(key, **values)
        self._sender.send_text(open_id=open_id, text=content.text, dedupe_key=dedupe_key)


# ----------------------------------------------------------------------
# 装配断言
# ----------------------------------------------------------------------


def assert_probe_timeouts_agree(*, probe: Any, schedule: Any) -> None:
    """装配断言 2：传输超时与就绪节奏的单次超时必须逐值相等，且不超过轮询间隔。

    不相等时，就绪那一侧算出来的「结论最晚什么时候落地」就是假的：它按
    ``预算 + 单次超时`` 给上游承诺收口，而真正卡住链路的是传输层那个数。
    """

    if probe.timeout_seconds != schedule.probe_timeout_seconds:
        raise RuntimeError("探针传输超时必须与就绪节奏的单次超时一致，否则收口上界是假的")
    if schedule.probe_timeout_seconds > schedule.interval_seconds:  # pragma: no cover - 上游已守
        raise RuntimeError("单次探针超时不得大于轮询间隔：否则整轮确认会无上界地拖长")


def assert_claim_limit_follows_capacity(reconciler: Any, executor: OnboardingExecutor) -> None:
    """装配断言 4（本轮新增）：**认领量必须被执行器的剩余容量压住**。

    ``claim_stale_onboarding`` 是「取出即记账」，而执行器满位时只能拒绝。两者不联动时，
    差额就是被**永久烧掉**的事件数——用户只剩一个「已收到」的表情，不建档、不发权限、
    也收不到任何终态。默认值曾经是「一轮最多认领 20 条、执行器容量 12」，第 13 条起必然
    如此。

    这条断言接替了搬迁前那条「双注入点必须拿到同一实例」：那条挡的是「对账落回失败关闭
    桩、认领即平账」；搬迁之后只剩一个注入点，同样的伤害改从容量差额这个入口进来。

    断言的对象是**已经构造好的**认领循环真的绑上了**这一个**执行器的 ``free_slots``，
    而不是「装配时传过一个函数」——绑错执行器与没绑一样危险。
    """

    source = getattr(reconciler, "capacity_source", None)
    if source is None:
        raise RuntimeError(
            "开通认领循环必须绑定执行器剩余容量：认领即记账，多认领的那些会被永久烧掉"
        )
    if getattr(source, "__self__", None) is not executor:
        raise RuntimeError("开通认领循环绑定的必须是本次装配的那个执行器")


def assert_stalled_lease_exceeds_chain_budget(
    *, lease_seconds: float, publish_wait_seconds: float, schedule: Any
) -> None:
    """装配断言 5（本轮新增，Issue #282）：**停摆租约必须严格长于一条链从最近一次
    认领起、在 ``provisioning``/``mcp_syncing`` 两格上可能停留的最长时间**。

    **口径（外部独立审查 P3-2 如实改写，此前的措辞没有说清"从哪一次认领起算"
    与"覆盖到哪一步为止"）**：

    - **从哪一次认领起算**：停摆候选查询（`adapters/postgres_stalled_provisioning.py`）
      按用户**最新一条** ``auto_provisioning`` 事件的 ``onboarding_dispatched_at``
      判到期，不是这个人第一次触发开通的那一刻。一条链因为通知发不出去被
      ``AutoOnboardingRunner._release_for_notify`` 放回、被
      ``OnboardingReconciler`` 重新认领后，会拿到一个**全新**的
      ``onboarding_dispatched_at``——本断言只需要覆盖"从这**一次**认领起，链跑到
      终态最长要多久"，不需要把"这个人可能已经被这样重跑过几轮"累加进同一个租约
      预算：候选查询天然会用最新那一次认领重新起跑约束，历史上跑过几轮不会让
      预算越滚越大（`abort_stalled_provisioning` 的 P2-5 修复保证了这一点）。
    - **覆盖到分水岭之后、终态确定为止**：发布等待上界（``publish_wait_seconds``）
      + 就绪预算（``schedule.budget_seconds``） + 单次探针超时
      （``schedule.probe_timeout_seconds``） + 执行级硬截止余量
      （:data:`PROBE_WATCHDOG_MARGIN_SECONDS`）——这四项按 ``_run`` 里从
      ``advance(provisioning)`` 到 :meth:`~lingxi.core.identity.onboarding_runner.
      AutoOnboardingRunner._confirm` 返回终态的真实执行顺序相加，是"这条链算出
      一个终态需要多久"的上界。默认组合约 20 分钟，45 分钟租约留出约 25 分钟余量。
      认领到分水岭之间的几步（组织资料读取、建档、令牌签发、用户环境落盘）没有
      独立上界，靠这 25 分钟余量吸收（编排者第二轮定向复核 P3-2 concern）。
    - **不包含的部分，及为什么不必包含**：终态算出来**之后**的
      :meth:`~lingxi.core.identity.onboarding_runner.AutoOnboardingRunner._notify`
      重试耗时（默认 3 次尝试、``sleep(1)``+``sleep(2)`` ≈ 3 秒）**没有**计入公式——
      这段时间发生在终态已经确定之后，不影响"链跑到终态需要多久"这件事本身，而且
      3 秒相对 25 分钟余量可以忽略不计，不值得为它污染这条公式的可读性。
    """

    chain_budget = (
        float(publish_wait_seconds)
        + float(schedule.budget_seconds)
        + float(schedule.probe_timeout_seconds)
        + PROBE_WATCHDOG_MARGIN_SECONDS
    )
    if float(lease_seconds) <= chain_budget:
        raise RuntimeError(
            "停摆租约必须严格长于一条链在 provisioning/mcp_syncing 两格上可能停留的"
            f"最长时间（租约={lease_seconds}s，链预算={chain_budget}s）：不成立时，"
            "停摆扫描会把一条正在正常跑的开通误判成僵尸"
        )


def build_stock_token_source(
    config: Any,
    *,
    access_token: Callable[[], str] | None,
    audit: AuditSink | None = None,
) -> Any | None:
    """装配存量令牌只读源（Issue #281 改道，产品负责人 2026-08-25）；坐标、MCP 令牌
    主密钥或令牌供给缺一即不装配，返回 ``None``——开通链因此原样走原签发路径，与改动前
    逐字节一致（``V-开通-24``）。

    坐标（``config.stock_token_app_token``/``stock_token_table_id``）**都**未配置是唯一
    保持零信号的分支：该能力当前默认关闭，不是首次开通编排的硬前提，因此不在
    ``_build_onboarding_duty`` 那组「缺了整个职责都不注册」的前置里，本函数只返回
    ``None``、调用方原样往下走。

    其余三个返回 ``None`` 的分支都是**半开的错误配置**，不是「这个能力被关掉了」，
    各留恰一条 ``onboarding.stock_token_source_not_wired`` 审计（形状与注入方式照
    :mod:`~lingxi.apps.scheduler.assembly` 里六处既有 ``*_duty_not_registered``：
    只报症状分类，不回显任何配置值；`audit` 未传时静默跳过，不新增硬依赖）：

    - ``partial_coordinates``：两个坐标只配了一个——这是部署时最容易手误留下的形状
      （改一个环境变量时漏改另一个），改动前与「两个都没配」共用零信号，运维看不出
      这里其实是半开的错误配置；
    - ``missing_encrypt_key``：坐标齐但 MCP 令牌主密钥没接线；
    - ``missing_access_token_supply``：坐标与主密钥都齐，但调用方没有交出令牌供给
      （与 ``roster_audit.duty_not_registered`` 的同名原因码同一个含义：调用方真的
      没有供给，不是「配了但取不到令牌」那种运行期失败）。

    复用**权限发布表的应用身份令牌供给**（Issue #226 裁定 3，调用方传入
    ``access_token``），不新增任何凭据材料：生产环境里这条只读能力很可能与发布表指向
    同一个 Base，但坐标独立配置（见 ``SchedulerConfig.stock_token_app_token`` 的字段
    注释），可以单独打开/关闭而不影响发布面。
    """

    app_token = config.stock_token_app_token
    table_id = config.stock_token_table_id
    if not app_token and not table_id:
        return None
    if not app_token or not table_id:
        if audit is not None:
            audit.record("onboarding.stock_token_source_not_wired", reason="partial_coordinates")
        return None
    if not config.mcp_token_encrypt_key:
        if audit is not None:
            audit.record("onboarding.stock_token_source_not_wired", reason="missing_encrypt_key")
        return None
    if access_token is None:
        if audit is not None:
            audit.record(
                "onboarding.stock_token_source_not_wired", reason="missing_access_token_supply"
            )
        return None

    from lingxi.adapters.mcp_token_cipher import McpTokenCipher
    from lingxi.adapters.stock_token_bitable import (
        BitableStockTokenSource,
        DecryptingStockTokenSource,
    )

    return DecryptingStockTokenSource(
        BitableStockTokenSource(
            base_url=config.feishu_base_url,
            app_token=app_token,
            table_id=table_id,
            access_token=access_token,
        ),
        cipher=McpTokenCipher(config.mcp_token_encrypt_key),
    )


def _build_onboarding_duty(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
    employment_access_token: Callable[[], str] | None,
    metric_translation_map: Mapping[str, Mapping[str, Sequence[str]]] | None,
    permission_publish: PermissionPublishDuty | None,
    stock_tokens: Any | None = None,
    onboarding_failed: Callable[[str, str], None] | None = None,
) -> Any | None:
    """装配首次开通编排（Epic D / S-D-02）；前置不齐就**不注册**并留下**恰一条**审计。

    形状照 :func:`_build_permission_refresh_duty`。缺项时返回 ``None``，于是**没有任何人
    认领** ``auto_provisioning`` 事件——它们原样留在 ``inbound_event`` 里等配置齐了再跑。
    这比装一个失败关闭桩安全：桩会「认领即平账」，把事件永久烧掉。

    **装配不变量（外部集成面审查坐实的 F1）**：``onboarding != None ⇒ permission_publish
    != None 且 permission_publish.publish_wired``。首次开通链最终会把一条发布意图排进
    ``publish_outbox``（``AutoOnboardingRunner`` 经 ``publish_allowed`` 闸），但真正把
    那条意图从 outbox 写进外部权限表、推进就绪确认的执行者是权限发布消费职责的**发布面**
    （:func:`_build_permission_publish_duty` 里的 ``PermissionPublishExecutor``）。那一面
    如果因为**它自己的**前置不齐而没有装配，开通排出的每一条发布意图此后**没有任何职责
    会再看它一眼**——迟到就绪恢复职责（V-开通-18）救不了，它只能确认"已经进入就绪等待"
    的用户，替代不了缺失的发布执行者。不挡在这里的话，失败关闭会发生在"已经认领、已经
    建档、已经建了用户环境"之后，用户表现成"接受了开通却永远走不到可用"，还可能留下
    需要人工收拾的半开记录。

    **判据是 ``publish_wired``，不是 ``is not None``**（冻结候选审查 2026-08-21 的 F1，
    由产品负责人当天第二次真实开通失败 ``publish_not_completed`` 坐实）：
    :func:`_build_permission_publish_duty` 在缺 ``LINGXI_PERMISSION_BITABLE_APP_TOKEN``
    或 ``LINGXI_PERMISSION_BITABLE_TABLE_ID`` 而就绪面装得起来时，**照常返回一个
    ``executor=None`` 的"仅就绪"职责**（那是它自己刻意的设计：已经发布出去的权限还等着
    被确认、被通知，没有理由因为暂时写不了新的一行就把它们一起停掉）。``is not None``
    这条旧判据会把那个只剩半面的职责当成"发布执行者在"而放行开通，于是用户被认领、
    被建档、发布意图被排进 outbox，而 outbox 那一侧根本没有执行者——正是上面描述的
    半开形状。反过来，``is None`` 这一支在可达配置下几乎不成立：两面都装不起来才返回
    ``None``，而那需要连 MCP 主密钥都缺，那本来就是开通编排自己的前置。两个分支都保留，
    各留一条**可分辨**的审计原因码（``permission_publish_not_assembled`` /
    ``permission_publish_not_wired``），排障时一眼看出该去补哪一组配置。

    因此这里**在认领任何用户之前**校验调用方（``build_loop``）已经装配好的
    ``permission_publish`` 对象本身，而不是重新判断"两者前置是否恰好相同"——即使两者
    前置未来分道扬镳，这条依赖仍然成立；这也是**不能只依赖** ``PermissionPublishDuty.
    _publish()`` 内部 ``publish_allowed`` 闸的原因：那道闸在**认领之后**才会被摸到，
    这里要挡的是认领本身。

    ``employment_access_token`` 是在职状态实时回读所用的**专用授权主体派生令牌**供给。
    它就是这次搬迁的**唯一理由**：那条一次性 ``refresh_token`` 全系统只允许一个消费者，
    而它已经在本进程里（``CredentialRotationLoop.refresh_for_supply`` +
    ``DerivedAccessTokenHolder``，#215 的形状）。**这里不新建供给**，只消费传进来的那一个。

    ``metric_translation_map`` 是「公司+职能→指标名」翻译映射（Issue #227），供构造
    ``publish_allowed`` 用——**不是**新的文件读取点，而是 :func:`_build_permission_refresh_duty`
    已经加载过的**同一个对象**（``None`` 代表那一次加载没有发生或失败，与空映射
    同一个结论：不可用）。调用方（``build_loop``）负责只加载一次、原样转发。
    """

    if permission_publish is None or not permission_publish.publish_wired:
        # 恰一条审计：只报「发布执行者不在」这个结构性原因，不夹带 `permission_publish`
        # 自己那一层的原因（那一层已经在自己的装配点留过审计：`permission_publish.
        # duty_not_registered` 或 `permission_publish.publish_not_wired`）——两条审计
        # 合起来才是完整的因果链，各自只认领自己那一段。两个分支的原因码**可分辨**：
        # 「整个职责没装配」要去补 MCP 那一组配置，「只有发布面没装配」要去补权限表 Base 坐标。
        reason = (
            "permission_publish_not_assembled"
            if permission_publish is None
            else "permission_publish_not_wired"
        )
        audit.record("onboarding.duty_not_registered", reason=reason)
        logger.warning(
            "权限发布执行者不可用（%s），首次开通编排不注册（开通排出的发布意图不会有任何"
            "职责消费）；未开通用户的首聊事件原样留在库里等待配置齐备，其余定时职责照常运行",
            reason,
        )
        return None

    from lingxi.adapters.mcp_token_cipher import MASTER_KEY_ENV

    unwired: tuple[str, str] | None = None
    for variable, value in (
        (MASTER_KEY_ENV, config.mcp_token_encrypt_key),
        ("LINGXI_QUERY_MCP_ENDPOINT", config.query_mcp_endpoint),
        ("LINGXI_USER_ENV_ROOT", config.user_env_root),
    ):
        if not value:
            unwired = ("missing_environment_variable", variable)
            break
    if unwired is None and employment_access_token is None:
        # 在职状态是**产品合同的硬门槛**（`V-开通-07`：非在职不建档、不发权限），
        # 没有它就没有合法的开通判定，不能"先跳过这一步"。
        unwired = ("employment_access_token_unwired", "")
    if unwired is not None:
        reason, variable = unwired
        facts: dict[str, str] = {"reason": reason}
        if variable:
            facts["variable"] = variable
        audit.record("onboarding.duty_not_registered", **facts)
        logger.warning(
            "首次开通编排未装配（%s%s）；未开通用户的首聊事件原样留在库里等待配置齐备，"
            "其余定时职责照常运行",
            reason,
            f"：{variable}" if variable else "",
        )
        return None

    from lingxi.adapters.delegated_credentials import registered_delegated_subject_open_id
    from lingxi.adapters.feishu_directory import FeishuDirectoryClient, FeishuEmploymentReader
    from lingxi.adapters.feishu_user_message import FeishuUserMessages
    from lingxi.adapters.mcp_token_cipher import McpTokenCipher
    from lingxi.adapters.postgres_conversation import PostgresGatewayStore
    from lingxi.adapters.postgres_email_binding import PostgresEmailBindingSource
    from lingxi.adapters.postgres_galaxy_snapshot import PostgresGalaxySnapshotReader
    from lingxi.adapters.postgres_identity import PostgresAppUserStore, PostgresOrgSnapshotStore
    from lingxi.adapters.postgres_mcp_token import PostgresMcpTokenStore, token_cipher_provider
    from lingxi.adapters.postgres_onboarding_failure import PostgresFailureReasonRecorder
    from lingxi.adapters.postgres_permission_publish import PostgresPermissionPublishStore
    from lingxi.adapters.postgres_roster_snapshot import PostgresRosterSnapshotStore
    from lingxi.adapters.query_mcp_probe import QueryMcpProbe, content_text_metrics_reader
    from lingxi.adapters.role_function_map_file import load_role_function_map
    from lingxi.adapters.user_environment import LocalUserEnvironment, UserEnvironmentError
    from lingxi.config.content import default_content_catalog
    from lingxi.core.conversation.onboarding_recovery import OnboardingReconciler
    from lingxi.core.identity.onboarding_runner import AutoOnboardingRunner
    from lingxi.core.permission.mcp_readiness import McpReadinessConfirmation

    try:
        role_function_map = load_role_function_map()
    except (OSError, ValueError) as error:
        # 只记异常类型：配置解析失败的正文可能带上文件内容片段。读不出来时**不能**退化成
        # 空映射——那会让所有角色变成"未映射"，于是每个人都被算成无可用权限。
        audit.record(
            "onboarding.duty_not_registered",
            reason="role_function_map_unavailable",
            error=type(error).__name__,
        )
        logger.error("角色职能映射配置不可用，首次开通编排不注册 error=%s", type(error).__name__)
        return None

    dsn = config.postgres_dsn
    timeouts = config.postgres_timeouts
    clock = monotonic_utc_clock()
    should_stop = stop.is_set

    tokens = PostgresMcpTokenStore(
        dsn, cipher=McpTokenCipher(config.mcp_token_encrypt_key), timeouts=timeouts
    )
    schedule = ReadinessSchedule(probe_timeout_seconds=config.query_mcp_timeout_seconds)
    probe = QueryMcpProbe(
        endpoint=config.query_mcp_endpoint,
        token_provider=token_cipher_provider(tokens),
        timeout_seconds=config.query_mcp_timeout_seconds,
        # 已验证的 reader（Issue #253 / L4a），同 ``_build_readiness_follow_up`` 那一份：
        # 真实问数 MCP 的 ``list_metrics`` 返回没有 ``structuredContent``，指标挂在
        # ``result.content[0].text`` 的一段 JSON 字符串里。这里此前遗漏了这一处注入
        # （只有刷新链那一侧在 #253 修复时接上），后果是首次开通那次阻塞式确认在真实
        # MCP 上永远技术失败、每个人都会走满十五分钟同步超时——与 #253 提交说明里描述
        # 的症状（"每个用户走满 15 分钟同步超时也拿不到开通完成"）完全一致，只是那次
        # 修复没有覆盖到这个调用点。本次随 V-开通-18 的恢复路径一并补上：不补的话，
        # 恢复职责会成为整条链上**唯一**能探到真实就绪的地方，首次开通自己的那一轮
        # 阻塞确认形同虚设。
        metrics_reader=content_text_metrics_reader,
    )
    assert_probe_timeouts_agree(probe=probe, schedule=schedule)
    guarded_probe = HardDeadlineProbe(
        probe=probe,
        timeout_seconds=schedule.probe_timeout_seconds + PROBE_WATCHDOG_MARGIN_SECONDS,
    )

    environment = LocalUserEnvironment(
        root=config.user_env_root, mcp_endpoint=config.query_mcp_endpoint
    )
    # **启动期全量清扫**：被 ``SIGKILL`` 留下的写入临时文件里有明文令牌，而目录内清扫只在
    # 「那个用户下一次再走开通」时发生——一个不再重试的用户意味着**没有时间上界**。在这里
    # 跑一次把上界压到「至多一个进程生命周期」。扫不动就**不注册本职责**：那意味着我们管
    # 不了这个目录，而它接下来正要接收明文凭据。
    try:
        environment.sweep_all()
    except UserEnvironmentError as error:
        audit.record(
            "onboarding.duty_not_registered",
            reason="user_environment_sweep_failed",
            error=error.code,
        )
        logger.error("用户环境启动期清扫失败，首次开通编排不注册 code=%s", error.code)
        return None

    store = PostgresGatewayStore(dsn, timeouts=timeouts)
    executor = OnboardingExecutor(workers=config.onboarding_workers, should_stop=should_stop)
    runner = AutoOnboardingRunner(
        directory=PostgresOrgSnapshotStore(dsn, timeouts=timeouts),
        employment=FeishuEmploymentReader(
            # `sleep=stop.wait`（Issue #284 A 组 #4）：节流/限频退避里的等待能被
            # SIGTERM 立刻打断。这里刻意用裸传，不像组织快照那侧包
            # `_stop_aware_sleep` 中止（登记不修，独立审查二轮 P2-B1）：开通链
            # 工作线程处理的是**已认领的用户**，停机预算内完成当前链避免用户
            # 结果丢失（重启不得造成结果丢失的红线）；其单用户请求量级小（数次
            # 调用），与组织快照整轮数百次不同；置位后等待归零的暴露窗口有界。
            # `test_scheduler_onboarding_assembly.py::…uses_stop_wait_as_its_sleeper`
            # 已锁定此行为是刻意的。
            client=FeishuDirectoryClient(base_url=config.feishu_base_url, sleep=stop.wait),
            access_token=employment_access_token,
        ),
        roster=RosterRows(PostgresRosterSnapshotStore(dsn, timeouts=timeouts)),
        galaxy=PostgresGalaxySnapshotReader(dsn, timeouts=timeouts),
        provisioning=PostgresAppUserStore(dsn, timeouts=timeouts),
        users=PostgresAppUserStore(dsn, timeouts=timeouts),
        environment=environment,
        tokens=tokens,
        stock_tokens=stock_tokens,
        # 存量差集导入口（rc25 S-1，Issue #540）：与存量令牌源**同进同出**——源装了、
        # 导入口没装时 ``AutoOnboardingRunner`` 构造期直接 ``TypeError``（与
        # ``full_access_wildcard`` 必填同一条结构性防漏接纪律），不会静默退回
        # "只复制令牌不读权限"的旧行为。
        legacy_importer=(
            PostgresLocalPermissionOverrideStore(dsn, timeouts=timeouts)
            if stock_tokens is not None
            else None
        ),
        # 预授权落库口（Issue #541 预开通）：与上面那个存量差集导入口是**同一张表、
        # 同一份适配器**，只是合成 ``pending_action`` 的 ``reason`` 不同（审计要一眼
        # 分得清"首聊时导入"与"首聊前预授权"）。**无条件装配**：它与存量令牌源无关，
        # 而系统触发带了预授权却发现它没装时是整链失败关闭，不该由部署配置决定。
        position_grants=PostgresLocalPermissionOverrideStore(dsn, timeouts=timeouts),
        decisions=PostgresPermissionPublishStore(dsn, timeouts=timeouts),
        readiness=McpReadinessConfirmation(
            probe=guarded_probe,
            store=tokens,
            audit=audit,
            clock=clock,
            # 阻塞式确认真的会等三分钟，但它跑在开通执行器**自己的**线程上，不挡住
            # SchedulerLoop 的任何一轮 tick。用 `stop.wait` 而不是 `time.sleep`：
            # SIGTERM 能立刻打断等待（同 `PermissionNoticeDispatcher`）。
            sleep=stop.wait,
            schedule=schedule,
        ),
        notifier=CatalogNotifier(
            sender=FeishuUserMessages(
                base_url=config.feishu_base_url,
                app_id=config.feishu_app_id,
                app_secret=config.feishu_app_secret,
            ),
            catalog=default_content_catalog(),
        ),
        ledger=store,
        audit=audit,
        role_function_map=role_function_map,
        # 「公司+职能→指标名」翻译映射（Issue #227 / #346 修复）：与下面 ``publish_
        # allowed`` 闸门共用**同一个已加载对象**（不在这里另读一份文件），供
        # ``AutoOnboardingRunner._publish`` 调用 ``translate_company_functions`` 时
        # 使用——此前 ``_publish`` 只用这个对象构造了布尔闸门、从未真正拿它翻译过
        # 发布行的值列表（#346 坐实的缺陷）。
        metric_translation_map=metric_translation_map,
        # 内测名单闸（Issue #302 S-N-01）：判据与理由见该静态方法与 innertest_roster_gate 模块文档。
        innertest_roster_gate=build_innertest_roster_gate(config.innertest_roster_open_ids),
        # 每次判定现读一次登记表（只读 `feishu_delegated_subject`，不碰凭据文件、不碰
        # refresh_token）：换主体之后旧值会让新的专用授权账号落回普通员工路径。
        delegated_subject=lambda: registered_delegated_subject_open_id(dsn, timeouts=timeouts),
        submit=executor.submit,
        sleep=stop.wait,
        clock=clock,
        should_stop=should_stop,
        publish_wait_seconds=config.onboarding_publish_wait_seconds,
        # ------------------------------------------------------------------
        # **发布闸门（Issue #227 开通侧整合）。**
        # ------------------------------------------------------------------
        # 「职能标签 → 指标名」的翻译层判据是「翻译映射整体为空时，本轮一条发布意图
        # 都不排，撤权也不例外」（见 ``permission_refresh`` 模块文档「翻译」一节，
        # 外部独立审查 2026-08-18 坐实的 P1）。本编排是 ``record_decision`` 的**第三个**
        # 调用点（前两个是每日重算的授权与撤权），因此必须自己带同一道闸，否则它就是
        # 那条判据的绕行入口，而绕过去的后果是往正式权限表写一行消费方读不懂的记录
        # ——外部表不可回滚。
        #
        # 判据实现是 :func:`~lingxi.core.permission.metric_translation.
        # metric_translation_available`——两个独立写入点共用的**唯一**一份；
        # ``metric_translation_map`` 是**同一个已加载对象**：``build_loop`` 只调用
        # :func:`_build_permission_refresh_duty` 读取一次
        # ``lingxi/config/company_function_metric_map.toml``，本函数收到的是那次读取
        # 的返回值，不在这里另读一份文件、不另做一次解析（两份来源迟早会漂移，而漂移
        # 的方向是错误发布）。**不碰 ``core``**：``AutoOnboardingRunner`` 只认一个
        # ``Callable[[], bool]``，判据属装配层。
        #
        # 变异锚点见 ``tests/test_onboarding_runner.PublishGateTests``：把这一行改回
        # ``lambda: True`` 必须让它变红；映射为空时，一个身份与权限都正常的用户走完
        # ``_match`` 后必须停在 ``permission_translation_unavailable``（用户看到
        # ``LX-ONBOARD-001``，已转交管理员，**不是**「没有银河权限」——后者会把一个
        # 权限完全正常的人引去银河申请一个他已经有的权限），**且 ``publish_outbox``
        # 零新增行、``app_user`` 零新增行**；映射非空时同一个用户照常推进到发布等待。
        publish_allowed=lambda: metric_translation_available(metric_translation_map),
        # 管理员送达（Issue #280 §7.3）：调用方（``build_loop``）没有装配告警职责时
        # 保持 ``None``——「已转交管理员处理」这句话此前就是这个默认值，行为不变；
        # 生产 main() 总会传一份真实回调（见 ``build_loop`` 调用点）。
        onboarding_failed=onboarding_failed,
        # 失败原因落库（Issue #337）：供 `/admin trace <追溯号>` 消费，见
        # `core/identity/onboarding_ports.FailureReasonRecorder` 协议文档。
        failure_reasons=PostgresFailureReasonRecorder(dsn, timeouts=timeouts),
        local_overrides=local_override_reader(dsn, timeouts=timeouts),
        # 「同邮箱已绑给另一个人」的只读回读口（rc25 S-2a，对抗审查 X-1）。
        # **必填、没有哨兵值**：漏接在 ``AutoOnboardingRunner`` 构造期就是 TypeError，
        # 不会静默退回"这道闸不存在"的旧行为。判定层见
        # ``core/identity/onboarding_guards.reject_email_bound_to_another_person``，
        # 数据库侧的结构性保证是迁移 ``0085`` 的部分唯一索引，两者是纵深关系。
        email_bindings=PostgresEmailBindingSource(dsn, timeouts=timeouts),
    )
    duty = OnboardingReconciler(
        store=store,
        onboarding=runner,
        audit=audit,
        stale_after=DISPATCH_AFTER,
        # 本进程的 SchedulerLoop 已经按 `interval_seconds` 定速，认领循环不再自限——自限会把「首次开通最多等一个扫描周期」这句承诺变成「一个扫描周期或一分钟，取大的那个」。
        min_interval_seconds=0.0,
        should_stop=should_stop,
        capacity=executor.free_slots,
    )
    assert_claim_limit_follows_capacity(duty, executor)
    executor.start()
    # Issue #284 C 组 #8：把执行器挂成 duty 的一个动态属性，供 `main()` 退出流程
    # 用 `join_onboarding_executors` 接线 stop()/join()——不改 `OnboardingReconciler`
    # 的类定义，见模块文档「停机接线」一节。
    duty.onboarding_executor = executor
    # 预开通（Issue #541 / rc25 S-8b 的 ops 入口）：把已经装配好的编排挂成一个**公开**
    # 属性，供「系统触发」的批量入口按名单逐人调用 ``start_system(open_id=…, trace_id=…)``。
    # 形状照上一行的执行器挂载，不改 ``OnboardingReconciler`` 的类定义；**这是脚本拿到
    # 编排的唯一受支持方式**——自己 new 一个 ``AutoOnboardingRunner`` 会绕过这里十几个
    # 装配不变量（发布闸、X-1 回读口、存量令牌源与差集导入口成对、内测名单闸）。
    duty.onboarding_runner = runner
    logger.info(
        "首次开通编排已装配 线程数=%s 队列深度=%s 认领窗口=%s 就绪节奏=0/%s/%s",
        config.onboarding_workers,
        config.onboarding_workers * 2,
        DISPATCH_AFTER,
        schedule.interval_seconds,
        schedule.budget_seconds,
    )
    return duty
