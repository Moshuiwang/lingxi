"""首次开通编排在 ``lingxi-scheduler`` 的装配。

``core/identity/onboarding_runner.py`` 是判定与次序，本模块把真实存储与外部接口
接上去，并在装配处把几条只会在生产暴露的错配变成启动期失败。**住在 scheduler
而不是 gateway**：在职状态必须实时回读飞书成员详情，需要专用授权主体派生的短期
令牌，那条一次性 ``refresh_token`` 全系统只允许一个消费者、已经在本进程里；让
gateway 去换等于制造第二条凭据通道，代价是首次开通要等一个扫描周期才开始。
合同要求的第一条提示仍由 gateway 即时发出。**前置不齐就不装配**，而不是装
一个一直失败的桩：没有任何人认领事件，比失败关闭桩更安全（桩会"认领即
平账"，把事件永久烧掉）。五条装配断言：执行级硬截止（看门狗防探针永不
返回）、传输超时与就绪节奏单次超时相等、注入单调时钟（防 NTP 回拨把已超窗
的成功判成有效）、认领量必须被执行器剩余容量压住（防超额认领烧掉事件）、
停摆租约必须长于链预算。停机时 :func:`join_onboarding_executors` 让开通
执行器这个独立线程池停止领取新工作、在预算内等在途链收尾。
"""

from __future__ import annotations

import dataclasses
import logging
import queue
import threading
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
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

#: 一条 ``auto_provisioning`` 事件落库多久之后可以被认领。认领即记账、正在跑的
#: 一条会被进程内去重挡住，因此窗口只剩一个用途：让 gateway 的第一条「已收到，
#: 正在核对」先落地。取五秒；首次开通的实际首触延迟 = 这个窗口 + 至多一个
#: ``SchedulerLoop`` 周期。
DISPATCH_AFTER = timedelta(seconds=5)

#: 工作线程在没有新任务时重新检查一次"是否该自行退出"的轮询间隔。哨兵投递在
#: 队列满时会静默失败，``_loop`` 因此用带超时的 ``queue.get()`` 顶替无超时的
#: 阻塞调用，每等待这么久还没有新任务就主动复查停止标志与队列是否已排空，
#: 堵上"最后一条任务被抢先取走、哨兵又丢失"这条竞态。取值 1 秒：生产代价是
#: 空闲线程每秒多醒一次，可忽略不计。
STOP_POLL_INTERVAL_SECONDS = 1.0


# ----------------------------------------------------------------------
# 装配断言 3：单调时钟
# ----------------------------------------------------------------------


def monotonic_utc_clock() -> Callable[[], datetime]:
    """返回一个**永不倒流**的带时区时钟。

    起点取一次墙钟（因此落库的时刻仍然是可读的真实时间），之后的每一次读取都
    按 ``time.monotonic()`` 的增量推进，NTP 回拨、手工改时间都不会让它往回走
    ——就绪确认的预算、硬上界和"成功来得太晚"三条判定全部是时间差，一次回拨
    足以让已经超窗的成功被算成还在窗口内。代价如实登记：进程长期运行时它与
    墙钟会缓慢漂移，这条链上只消费"两个时刻之间差了多久"，漂移不影响；需要
    绝对时刻的地方另有各自的来源。
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
    """给同步探针套一层**执行级**看门狗。

    就绪状态机的上界是"探针返回之后再看一眼现在几点"，管得住"返回得太晚"，
    管不住"**永远不返回**"——一个没设超时的 HTTP 调用可以挂住整条链。做法
    是把每一次 ``list_metrics`` 放进一条一次性守护线程，主线程按传输超时 +
    余量等它，超时未回就抛 ``McpProbeError``（技术失败，不是"MCP 拒绝"）。
    **被放弃的线程不会被强杀**——Python 没有安全的线程终止原语，它会在底层
    套接字自己超时后结束；线程是 ``daemon``，不会挡住进程退出。
    """

    def __init__(self, *, probe: Any, timeout_seconds: float) -> None:
        """把一个真实探针包进执行级看门狗；`probe` 不得为空。"""
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
        """跑一次真实探针，超过执行级硬截止仍未返回就判技术失败。"""
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
    """开通链的专属线程池。

    **不与长连接线程、投递线程共用任何一条**：真实编排单次可达分钟级（权限
    同步允许等到十五分钟），跑在长连接线程上会让 gateway 十五分钟收不到任何
    消息，跑在投递线程上会让十五分钟一条投递都发不出去。**队列有界**，满了
    就拒绝而不是无限堆积：一次外部故障导致所有链都卡在十五分钟等待时，继续
    排队只会让排在后面的用户更晚收到一条早已过时的结论；拒绝会让调用方返回
    明确的内部故障终态。线程是 ``daemon``：停机预算耗尽时进程不会被一条还在
    等外部响应的链焊住。
    """

    def __init__(
        self,
        *,
        workers: int,
        backlog: int | None = None,
        should_stop: Callable[[], bool] | None = None,
        stop_poll_seconds: float = STOP_POLL_INTERVAL_SECONDS,
    ) -> None:
        """按线程数与队列上限装配一个开通链专属执行器；线程尚未启动。"""
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
        """启动全部工作线程；调用方负责只调用一次。"""
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

        **刻意不排空队列、不丢弃已排队的任务**：那些任务对应的事件在认领那
        一刻就已经被记账了，直接丢掉等于永久烧掉；每条链的第一件事就是问一
        次停机，被取到的那些会立刻中止并把认领放回去，照常出队反而是唯一
        不丢事件的走法。**哨兵投递只是快路径，不是工作线程退出的唯一依据**：
        队列满时哨兵会投递失败，真正兜底的是 :meth:`_loop` 里带超时的
        ``get()``，自己定期复查停止标志与队列是否已排空。
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
        """至多等 `timeout` 秒，让在途任务在预算内收尾；超时不强杀线程。"""
        deadline = time.monotonic() + max(0.0, timeout)
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

    @property
    def alive(self) -> bool:
        """是否还有工作线程活着。"""
        return any(thread.is_alive() for thread in self._threads)

    def _loop(self) -> None:
        """单条工作线程的主循环。

        **不对正确退出依赖一次可能失败的哨兵投递**：``self._queue.get()`` 带上
        ``self._stop_poll_seconds`` 超时，取代无超时的阻塞调用，堵上"最后一条
        任务被另一条工作线程抢先取走、哨兵又因队列满被丢弃"这条竞态——旧实现
        里那次 ``get()`` 没有超时会永久卡住。带超时之后即使扑空，线程最多等
        一段时间就会重新复查停止标志与队列是否已空。**不改变** ``stop()`` 的
        产品语义：已排队的任务照常正常出队执行，只是多了一条定期自查的路径。
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


#: 停机时等待开通执行器收工的预算（见模块文档「停机接线」）。
#: 取值同 ``apps/gateway/config.py::shutdown_timeout_seconds`` 的既有默认值：
#: 在途链靠 ``_stop_guard`` 在检查点之间快速收口，正常情况下远快于这个上限；
#: 执行器线程是 ``daemon``，超时未收工也不会阻塞进程退出，只留一条响亮日志。
ONBOARDING_SHUTDOWN_JOIN_TIMEOUT_SECONDS = 20.0


def join_onboarding_executors(duties: Sequence[Any]) -> None:
    """停机收尾：让每一个挂了开通执行器的职责停止领取新链，并在预算内等它收工。

    由 ``main()`` 在 ``SchedulerLoop.run_forever()`` 返回之后调用一次——此时
    全部职责已经停止领取新一轮工作，正是"完成或安全中断在途工作"这条纪律
    该收口的时机。只认**挂在 duty 上的 ``onboarding_executor`` 属性**，不是
    "扫描全部职责找 ``OnboardingReconciler`` 实例"：前置不齐时整个职责都不
    装配，这里只需要认这一个约定好的接缝，不需要重新认识那个类型本身。没有
    任何职责挂了这个属性时本函数是纯粹的空操作。
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
        """包住一个花名册持久快照读取口。"""
        self._store = store

    def rows(self) -> Sequence[Mapping[str, Any]] | None:
        """当前全部花名册行，没有快照时返回 ``None``。"""
        snapshot = self._store.load()
        return None if snapshot is None else snapshot.rows


class CatalogNotifier:
    """把终态的内容目录 key 渲染成正文，主动私聊给用户本人。

    渲染走 ``ContentCatalog``，因此每一条用户可见正文都经过版本纪律与可见性检查
    （``config/content.py``）——编排层给的是 key 与变量，不是拼好的句子。
    """

    def __init__(self, *, sender: Any, catalog: Any) -> None:
        """包住一个私聊发送口与一份内容目录。"""
        self._sender = sender
        self._catalog = catalog

    def send(
        self, *, open_id: str, key: str, values: Mapping[str, object], dedupe_key: str
    ) -> None:
        """渲染内容目录里的一条 key 并私聊发给用户本人。"""
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
    """装配断言：**认领量必须被执行器的剩余容量压住**。

    ``claim_stale_onboarding`` 是「取出即记账」，而执行器满位时只能拒绝。两者
    不联动时，差额就是被**永久烧掉**的事件数——用户只剩一个「已收到」的表情，
    不建档、不发权限、也收不到任何终态。断言的对象是**已经构造好的**认领
    循环真的绑上了**这一个**执行器的 ``free_slots``，而不是"装配时传过一个
    函数"——绑错执行器与没绑一样危险。
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
    """装配断言：停摆租约必须严格长于一条链从最近一次认领起可能停留的最长时间。

    "从哪一次认领起算"：候选查询按最新一条事件的分发时刻判到期，不是第一次
    触发开通的那一刻——重新认领会拿到全新时刻，历史上跑过几轮不会让预算越
    滚越大。"覆盖到哪里"：发布等待上界 + 就绪预算 + 单次探针超时 + 执行级
    硬截止余量按真实执行顺序相加，是链算出终态需要多久的上界；认领到分水岭
    之间没有独立上界的几步靠这段余量吸收。终态确定之后的通知重试耗时不计入
    公式，相对余量可忽略不计。
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


def _stock_token_unwired_reason(config: Any, access_token: Callable[[], str] | None) -> str | None:
    """存量令牌只读源缺前置的原因；``None`` 表示前置齐全。

    ``"disabled"`` 是坐标都未配置（能力默认关闭，不记审计）；其余三个字符串是
    :func:`build_stock_token_source` 直接使用的审计 ``reason`` 取值，各自对应
    一种半开的错误配置。
    """
    app_token = config.stock_token_app_token
    table_id = config.stock_token_table_id
    if not app_token and not table_id:
        return "disabled"
    if not app_token or not table_id:
        return "partial_coordinates"
    if not config.mcp_token_encrypt_key:
        return "missing_encrypt_key"
    if access_token is None:
        return "missing_access_token_supply"
    return None


def build_stock_token_source(
    config: Any,
    *,
    access_token: Callable[[], str] | None,
    audit: AuditSink | None = None,
) -> Any | None:
    """装配存量令牌只读源，坐标/主密钥/令牌供给缺一即不装配。

    返回 ``None`` 时开通链原样走原签发路径，与改动前逐字节一致。坐标**都**
    未配置是唯一保持零信号的分支：该能力默认关闭，本函数只返回 ``None``、
    调用方原样往下走。其余返回 ``None`` 的分支都是**半开的错误配置**，各留
    恰一条审计（只报症状分类，不回显任何配置值）：坐标只配一半、坐标齐但
    主密钥没接线、坐标与主密钥都齐但调用方没有交出令牌供给。复用权限发布表
    的应用身份令牌供给，不新增任何凭据材料。
    """
    unwired = _stock_token_unwired_reason(config, access_token)
    if unwired is not None:
        if audit is not None and unwired != "disabled":
            audit.record("onboarding.stock_token_source_not_wired", reason=unwired)
        return None

    app_token = config.stock_token_app_token
    table_id = config.stock_token_table_id

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


def _onboarding_missing_prerequisite(
    config: SchedulerConfig, employment_access_token: Callable[[], str] | None
) -> tuple[str, str] | None:
    """检查权限发布/角色映射/用户环境之外的前置；``None`` 表示齐全。"""
    from lingxi.adapters.mcp_token_cipher import MASTER_KEY_ENV

    for variable, value in (
        (MASTER_KEY_ENV, config.mcp_token_encrypt_key),
        ("LINGXI_QUERY_MCP_ENDPOINT", config.query_mcp_endpoint),
        ("LINGXI_USER_ENV_ROOT", config.user_env_root),
    ):
        if not value:
            return ("missing_environment_variable", variable)
    if employment_access_token is None:
        # 在职状态是产品合同的硬门槛（非在职不建档、不发权限），没有它就没有
        # 合法的开通判定，不能"先跳过这一步"。
        return ("employment_access_token_unwired", "")
    return None


def _load_role_function_map_or_none(audit: AuditSink) -> Mapping[str, Any] | None:
    """加载角色职能映射；失败时记审计并返回 ``None``（不注册整个职责）。"""
    from lingxi.adapters.role_function_map_file import load_role_function_map

    try:
        return load_role_function_map()
    except (OSError, ValueError) as error:
        # 只记异常类型：配置解析失败的正文可能带上文件内容片段。读不出来时
        # **不能**退化成空映射——那会让所有角色变成"未映射"，每个人都被算成
        # 无可用权限。
        audit.record(
            "onboarding.duty_not_registered",
            reason="role_function_map_unavailable",
            error=type(error).__name__,
        )
        logger.error("角色职能映射配置不可用，首次开通编排不注册 error=%s", type(error).__name__)
        return None


def _build_onboarding_readiness_probe(
    config: SchedulerConfig,
) -> tuple[Any, ReadinessSchedule, Any]:
    """装配就绪判定的令牌读写口、节奏与带执行级硬截止的探针。"""
    from lingxi.adapters.mcp_token_cipher import McpTokenCipher
    from lingxi.adapters.postgres_mcp_token import PostgresMcpTokenStore, token_cipher_provider
    from lingxi.adapters.query_mcp_probe import QueryMcpProbe, content_text_metrics_reader

    dsn = config.postgres_dsn
    timeouts = config.postgres_timeouts
    tokens = PostgresMcpTokenStore(
        dsn, cipher=McpTokenCipher(config.mcp_token_encrypt_key), timeouts=timeouts
    )
    schedule = ReadinessSchedule(probe_timeout_seconds=config.query_mcp_timeout_seconds)
    probe = QueryMcpProbe(
        endpoint=config.query_mcp_endpoint,
        token_provider=token_cipher_provider(tokens),
        timeout_seconds=config.query_mcp_timeout_seconds,
        # 已验证的 reader：真实问数 MCP 的 list_metrics 返回没有
        # structuredContent，指标挂在 result.content[0].text 的一段 JSON
        # 字符串里，不注入的话首次开通那次阻塞式确认在真实 MCP 上会永远
        # 技术失败。
        metrics_reader=content_text_metrics_reader,
    )
    assert_probe_timeouts_agree(probe=probe, schedule=schedule)
    guarded_probe = HardDeadlineProbe(
        probe=probe,
        timeout_seconds=schedule.probe_timeout_seconds + PROBE_WATCHDOG_MARGIN_SECONDS,
    )
    return tokens, schedule, guarded_probe


def _sweep_onboarding_user_environment(config: SchedulerConfig, audit: AuditSink) -> Any | None:
    """启动期清扫用户环境目录；失败时不注册本职责。

    被 ``SIGKILL`` 留下的写入临时文件里有明文令牌，目录内清扫只在"那个用户
    下一次再走开通"时发生——一个不再重试的用户意味着没有时间上界。这里跑一次
    把上界压到至多一个进程生命周期。扫不动就不注册：那意味着管不了这个即将
    接收明文凭据的目录。
    """
    from lingxi.adapters.user_environment import LocalUserEnvironment, UserEnvironmentError

    environment = LocalUserEnvironment(
        root=config.user_env_root, mcp_endpoint=config.query_mcp_endpoint
    )
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
    return environment


def _onboarding_identity_kwargs(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    employment_access_token: Callable[[], str],
    tokens: Any,
    environment: Any,
    stock_tokens: Any | None,
) -> dict[str, Any]:
    """身份定位与权限差集这一半的 ``AutoOnboardingRunner`` 构造参数。"""
    from lingxi.adapters.feishu_directory import FeishuDirectoryClient, FeishuEmploymentReader
    from lingxi.adapters.postgres_galaxy_snapshot import PostgresGalaxySnapshotReader
    from lingxi.adapters.postgres_identity import PostgresAppUserStore, PostgresOrgSnapshotStore
    from lingxi.adapters.postgres_roster_snapshot import PostgresRosterSnapshotStore

    dsn = config.postgres_dsn
    timeouts = config.postgres_timeouts

    return {
        "directory": PostgresOrgSnapshotStore(dsn, timeouts=timeouts),
        "employment": FeishuEmploymentReader(
            # 节流/限频退避里的等待能被 SIGTERM 立刻打断，这里刻意用裸传，不像
            # 组织快照那侧包一层中止：开通链工作线程处理的是已认领的用户，
            # 停机预算内完成当前链避免用户结果丢失；单用户请求量级小，置位后
            # 等待归零的暴露窗口有界。
            client=FeishuDirectoryClient(base_url=config.feishu_base_url, sleep=stop.wait),
            access_token=employment_access_token,
        ),
        "roster": RosterRows(PostgresRosterSnapshotStore(dsn, timeouts=timeouts)),
        "galaxy": PostgresGalaxySnapshotReader(dsn, timeouts=timeouts),
        "provisioning": PostgresAppUserStore(dsn, timeouts=timeouts),
        "users": PostgresAppUserStore(dsn, timeouts=timeouts),
        "environment": environment,
        "tokens": tokens,
        "stock_tokens": stock_tokens,
        # 存量差集导入口：与存量令牌源同进同出——源装了、导入口没装时构造期
        # 直接 TypeError，不会静默退回"只复制令牌不读权限"的旧行为。
        "legacy_importer": (
            PostgresLocalPermissionOverrideStore(dsn, timeouts=timeouts)
            if stock_tokens is not None
            else None
        ),
        # 预授权落库口：与上面那个存量差集导入口是同一张表、同一份适配器，只是
        # 合成 pending_action 的 reason 不同。无条件装配：它与存量令牌源无关，
        # 系统触发带了预授权却发现它没装时是整链失败关闭，不该由部署配置决定。
        "position_grants": PostgresLocalPermissionOverrideStore(dsn, timeouts=timeouts),
    }


def _onboarding_readiness_kwargs(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
    tokens: Any,
    guarded_probe: Any,
    schedule: ReadinessSchedule,
    store: Any,
    role_function_map: Mapping[str, Any],
    metric_translation_map: Mapping[str, Mapping[str, Sequence[str]]] | None,
    clock: Callable[[], datetime],
) -> dict[str, Any]:
    """就绪确认与账本这一份的 ``AutoOnboardingRunner`` 构造参数。

    ``clock`` 由调用方传入并原样使用——见 :func:`_build_onboarding_runner` 的
    文档字符串：这里不得自建第二个单调时钟实例。
    """
    from lingxi.adapters.feishu_user_message import FeishuUserMessages
    from lingxi.adapters.postgres_permission_publish import PostgresPermissionPublishStore
    from lingxi.config.content import default_content_catalog
    from lingxi.core.permission.mcp_readiness import McpReadinessConfirmation

    dsn = config.postgres_dsn
    timeouts = config.postgres_timeouts

    return {
        "decisions": PostgresPermissionPublishStore(dsn, timeouts=timeouts),
        "readiness": McpReadinessConfirmation(
            probe=guarded_probe,
            store=tokens,
            audit=audit,
            clock=clock,
            # 阻塞式确认真的会等三分钟，但它跑在开通执行器自己的线程上，不挡住
            # SchedulerLoop 的任何一轮 tick；用 stop.wait 而不是 time.sleep 让
            # SIGTERM 能立刻打断等待。
            sleep=stop.wait,
            schedule=schedule,
        ),
        "notifier": CatalogNotifier(
            sender=FeishuUserMessages(
                base_url=config.feishu_base_url,
                app_id=config.feishu_app_id,
                app_secret=config.feishu_app_secret,
            ),
            catalog=default_content_catalog(),
        ),
        "ledger": store,
        "audit": audit,
        "role_function_map": role_function_map,
        # 与 publish_allowed 闸门共用同一个已加载对象（不在这里另读一份文件），
        # 供 AutoOnboardingRunner._publish 翻译发布行的值列表用。
        "metric_translation_map": metric_translation_map,
    }


def _onboarding_publish_kwargs(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    metric_translation_map: Mapping[str, Mapping[str, Sequence[str]]] | None,
    executor: OnboardingExecutor,
    onboarding_failed: Callable[[str, str], None] | None,
    clock: Callable[[], datetime],
) -> dict[str, Any]:
    """发布闸门与终态收口这一份的 ``AutoOnboardingRunner`` 构造参数。

    ``clock`` 必须与 :func:`_onboarding_readiness_kwargs` 用的是同一个实例，
    理由同上。
    """
    from lingxi.adapters.delegated_credentials import registered_delegated_subject_open_id
    from lingxi.adapters.postgres_email_binding import PostgresEmailBindingSource
    from lingxi.adapters.postgres_onboarding_failure import PostgresFailureReasonRecorder

    dsn = config.postgres_dsn
    timeouts = config.postgres_timeouts

    return {
        "innertest_roster_gate": build_innertest_roster_gate(config.innertest_roster_open_ids),
        # 每次判定现读一次登记表（只读，不碰凭据文件、不碰 refresh_token）：
        # 换主体之后旧值会让新的专用授权账号落回普通员工路径。
        "delegated_subject": lambda: registered_delegated_subject_open_id(dsn, timeouts=timeouts),
        "submit": executor.submit,
        "sleep": stop.wait,
        "clock": clock,
        "should_stop": stop.is_set,
        "publish_wait_seconds": config.onboarding_publish_wait_seconds,
        # 发布闸门：翻译映射整体为空时本轮一条发布意图都不排，撤权也不例外。
        # 本编排是 record_decision 的第三个调用点，必须自己带同一道闸，否则
        # 绕过去的后果是往正式权限表写一行消费方读不懂的记录，外部表不可回滚。
        "publish_allowed": lambda: metric_translation_available(metric_translation_map),
        # 管理员送达：调用方没有装配告警职责时保持 None——"已转交管理员处理"
        # 这句话此前就是这个默认值，行为不变。
        "onboarding_failed": onboarding_failed,
        "failure_reasons": PostgresFailureReasonRecorder(dsn, timeouts=timeouts),
        "local_overrides": local_override_reader(dsn, timeouts=timeouts),
        # 「同邮箱已绑给另一个人」的只读回读口。必填、没有哨兵值：漏接在构造期
        # 就是 TypeError，不会静默退回"这道闸不存在"的旧行为。
        "email_bindings": PostgresEmailBindingSource(dsn, timeouts=timeouts),
    }


@dataclass(frozen=True)
class _OnboardingRunnerInputs:
    """:func:`_build_onboarding_runner` 收到的可选输入。

    打包成一个值对象只是为了让该函数的参数个数落在 ``PLR0913`` 的上限内——
    字段本身仍在各自的构造点被直接使用，不是无意义的透传。
    """

    stock_tokens: Any | None
    metric_translation_map: Mapping[str, Mapping[str, Sequence[str]]] | None
    onboarding_failed: Callable[[str, str], None] | None


def _build_onboarding_runner(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
    employment_access_token: Callable[[], str],
    readiness_probe: tuple[Any, ReadinessSchedule, Any],
    environment: Any,
    ledger: tuple[Any, OnboardingExecutor],
    role_function_map: Mapping[str, Any],
    inputs: _OnboardingRunnerInputs,
) -> Any:
    """装配 :class:`AutoOnboardingRunner` 本体：合并三份构造参数并接上全部端口。

    ``readiness`` 与顶层 ``clock`` 必须共用**同一个**单调时钟实例：两者都参与
    「成功来得太晚」的判定，一份对象各自持有会让两处时间基准悄悄分叉。
    """
    from lingxi.core.identity.onboarding_runner import AutoOnboardingRunner

    tokens, schedule, guarded_probe = readiness_probe
    store, executor = ledger
    clock = monotonic_utc_clock()
    kwargs = _onboarding_identity_kwargs(
        config,
        stop=stop,
        employment_access_token=employment_access_token,
        tokens=tokens,
        environment=environment,
        stock_tokens=inputs.stock_tokens,
    )
    kwargs.update(
        _onboarding_readiness_kwargs(
            config,
            stop=stop,
            audit=audit,
            tokens=tokens,
            guarded_probe=guarded_probe,
            schedule=schedule,
            store=store,
            role_function_map=role_function_map,
            metric_translation_map=inputs.metric_translation_map,
            clock=clock,
        )
    )
    kwargs.update(
        _onboarding_publish_kwargs(
            config,
            stop=stop,
            metric_translation_map=inputs.metric_translation_map,
            executor=executor,
            onboarding_failed=inputs.onboarding_failed,
            clock=clock,
        )
    )
    return AutoOnboardingRunner(**_group_runner_kwargs(kwargs))


def _group_runner_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """把三段扁平的构造参数按字段名装进 runner 的五个参数对象。

    任何对不上字段的键都必须响亮失败——静默丢弃会让某个端口在运行期才暴露为缺失。
    """
    from lingxi.core.identity.onboarding_config import (
        OnboardingActions,
        OnboardingPolicy,
        OnboardingRecords,
        OnboardingRuntime,
        OnboardingSources,
    )

    remaining = dict(kwargs)
    grouped: dict[str, Any] = {}
    for name, cls in (
        ("sources", OnboardingSources),
        ("actions", OnboardingActions),
        ("records", OnboardingRecords),
        ("policy", OnboardingPolicy),
        ("runtime", OnboardingRuntime),
    ):
        fields = {field.name for field in dataclasses.fields(cls)}
        grouped[name] = cls(**{key: remaining.pop(key) for key in list(remaining) if key in fields})
    if remaining:
        raise TypeError(f"AutoOnboardingRunner 不认识的构造参数：{sorted(remaining)}")
    return grouped


def _finalize_onboarding_duty(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
    store: Any,
    runner: Any,
    executor: OnboardingExecutor,
    schedule: ReadinessSchedule,
) -> Any:
    """把 runner 包成 ``OnboardingReconciler``，接上执行器容量断言与动态属性挂载。"""
    from lingxi.core.conversation.onboarding_recovery import OnboardingReconciler

    duty = OnboardingReconciler(
        store=store,
        onboarding=runner,
        audit=audit,
        stale_after=DISPATCH_AFTER,
        # 本进程的 SchedulerLoop 已经按 interval_seconds 定速，认领循环不再
        # 自限——自限会把"首次开通最多等一个扫描周期"变成"一个扫描周期或
        # 一分钟，取大的那个"。
        min_interval_seconds=0.0,
        should_stop=stop.is_set,
        capacity=executor.free_slots,
    )
    assert_claim_limit_follows_capacity(duty, executor)
    executor.start()
    # 把执行器挂成 duty 的一个动态属性，供 main() 退出流程用
    # join_onboarding_executors 接线 stop()/join()——不改 OnboardingReconciler
    # 的类定义，见模块文档「停机接线」一节。
    duty.onboarding_executor = executor
    # 预开通（系统触发批量入口）：把已经装配好的编排挂成一个公开属性，供系统
    # 触发的批量入口按名单逐人调用。形状照上一行的执行器挂载，不改
    # OnboardingReconciler 的类定义；这是脚本拿到编排的唯一受支持方式——自己
    # new 一个 AutoOnboardingRunner 会绕过这里十几个装配不变量。
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


def _onboarding_prerequisites_missing(
    config: SchedulerConfig,
    *,
    audit: AuditSink,
    employment_access_token: Callable[[], str] | None,
    permission_publish: PermissionPublishDuty | None,
) -> bool:
    """检查权限发布执行者与其余环境前置；缺项记**恰一条**审计并返回 ``True``。

    **判据是 ``publish_wired``，不是 ``is not None``**：发布面缺权限表坐标时
    仍会返回一个"仅就绪"的半装配对象，``is not None`` 会误放行开通、让发布
    意图排进 outbox 却没有任何职责会再看它一眼。
    """
    if permission_publish is None or not permission_publish.publish_wired:
        # 恰一条审计：只报「发布执行者不在」这个结构性原因，不夹带 `permission_publish`
        # 自己那一层的原因（那一层已经在自己的装配点留过审计）——两条审计合起来
        # 才是完整的因果链，各自只认领自己那一段，原因码**可分辨**。
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
        return True

    unwired = _onboarding_missing_prerequisite(config, employment_access_token)
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
        return True
    return False


def _build_onboarding_ledger_and_executor(
    config: SchedulerConfig, stop: threading.Event
) -> tuple[Any, OnboardingExecutor]:
    """装配开通事件账本存取口与专属线程池执行器。"""
    from lingxi.adapters.postgres_conversation import PostgresGatewayStore

    store = PostgresGatewayStore(config.postgres_dsn, timeouts=config.postgres_timeouts)
    executor = OnboardingExecutor(workers=config.onboarding_workers, should_stop=stop.is_set)
    return store, executor


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
    """装配首次开通编排；前置不齐就**不注册**并留下**恰一条**审计。

    缺项时返回 ``None``，于是没有任何人认领 ``auto_provisioning`` 事件，比装
    一个失败关闭桩安全。装配不变量见 :func:`_onboarding_prerequisites_missing`。
    """
    if _onboarding_prerequisites_missing(
        config,
        audit=audit,
        employment_access_token=employment_access_token,
        permission_publish=permission_publish,
    ):
        return None

    role_function_map = _load_role_function_map_or_none(audit)
    if role_function_map is None:
        return None
    tokens, schedule, guarded_probe = _build_onboarding_readiness_probe(config)
    environment = _sweep_onboarding_user_environment(config, audit)
    if environment is None:
        return None

    store, executor = _build_onboarding_ledger_and_executor(config, stop)
    runner = _build_onboarding_runner(
        config,
        stop=stop,
        audit=audit,
        employment_access_token=employment_access_token,
        readiness_probe=(tokens, schedule, guarded_probe),
        environment=environment,
        ledger=(store, executor),
        role_function_map=role_function_map,
        inputs=_OnboardingRunnerInputs(
            stock_tokens=stock_tokens,
            metric_translation_map=metric_translation_map,
            onboarding_failed=onboarding_failed,
        ),
    )
    return _finalize_onboarding_duty(
        config,
        stop=stop,
        audit=audit,
        store=store,
        runner=runner,
        executor=executor,
        schedule=schedule,
    )
