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
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

logger = logging.getLogger(__name__)

_UTC = timezone.utc

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
            except BaseException as error:  # noqa: BLE001 - 原样带回主线程再抛
                failure.append(error)

        worker = threading.Thread(
            target=call, name="lingxi-gateway-mcp-probe", daemon=True
        )
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
        self._queue: queue.Queue[Callable[[], None] | None] = queue.Queue(
            maxsize=self._backlog
        )
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
            except BaseException:  # noqa: BLE001 - 一条链的失败不得带走这条线程
                logger.exception("开通链在执行线程上抛出未捕获异常")
            finally:
                self._queue.task_done()
            if self._stopping.is_set() and self._queue.empty():
                return


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
    """装配断言 5（本轮新增，Issue #282）：**停摆租约必须严格长于一条链在
    ``provisioning``/``mcp_syncing`` 两格上可能停留的最长时间**。

    链在这两格上的最长自然停留 = 发布等待上界（``publish_wait_seconds``） + 就绪预算
    （``schedule.budget_seconds``） + 单次探针超时（``schedule.probe_timeout_seconds``）
    + 执行级硬截止余量（:data:`PROBE_WATCHDOG_MARGIN_SECONDS`）。默认组合约 20 分钟，
    停摆扫描职责（:class:`~lingxi.apps.scheduler.stalled_provisioning.
    StalledProvisioningDuty`）取 45 分钟租约，留出约 25 分钟余量。

    不成立时，停摆扫描会把一条**正在正常跑**的开通判成僵尸并给用户推一条
    ``LX-ONBOARD-001``。这条断言真正挡住的不是今天的默认值算错，而是**以后有人把就绪
    预算从十五分钟调到三十分钟**——那一次改动完全不会碰
    ``stalled_provisioning.py`` 一个字，却会静默地把租约变成假的；装配期在这里炸出来，
    比生产上把一批正在跑的开通误判成僵尸便宜得多。
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
