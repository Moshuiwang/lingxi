"""首次开通编排的装配（Epic D / S-D-02）。

``core/identity/onboarding_runner.py`` 是判定与次序，本模块是**把真实存储与外部接口接上
去的那一层**：读配置、建适配器、把它们喂给 :class:`~lingxi.core.identity.onboarding_runner.
AutoOnboardingRunner`，并在装配处就把几条只会在生产暴露的错配变成启动期失败。

## 前置不齐就**不装配**，而不是装一个一直失败的

形状照 ``apps/scheduler`` 的 ``_build_permission_refresh_duty``（`V-花名册-29` 的同一条
纪律：缺项只报变量名、审计**恰一条**、其余职责照常运行）。缺任何一项前置时
:func:`build_onboarding_runner` 返回 ``None``，``main()`` 于是保留失败关闭桩——用户仍然
拿到冻结的 ``LX-ONBOARD-001``，但**启动日志里有一条指名道姓的原因**。

装一个"接线了但每次都在第三步炸"的 runner 更糟：它会让漏配表现成"开通链路有 bug"，
而不是"这个部署还没配齐"。

## 三条装配断言（Epic C 在 PR #218 / #221 交给 Epic D 的清单）

1. **执行级硬截止**：:class:`HardDeadlineProbe` 给同步探针套一层看门狗。就绪状态机自己
   的上界是"探针**返回之后**再看一眼时刻"，它挡不住一个**永不返回**的探针——那条链会把
   一个开通线程永久占住，而所有账面（记录、日志）都看起来只是"还在等"。
2. **``probe_timeout ≤ interval`` 且与传输超时相等**：两者不一致时，就绪那一侧算出来的
   "结论最晚什么时候落地"就是假的。前半句由 ``ReadinessSchedule`` 自己守，这里仍然显式
   断言一次——它是被交办的清单项，不能因为"上游大概会管"而消失。
3. **注入单调时钟**：:func:`monotonic_utc_clock`。就绪确认的预算、硬上界与"成功来得太晚"
   全部靠时间差判定，而 ``datetime.now()`` 会因为 NTP 回拨往回走——一次回拨足以让一条
   已经超窗的成功被判成有效。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from lingxi.core.conversation.ports import OnboardingRunner

from .config import GatewayConfig

logger = logging.getLogger(__name__)

_UTC = timezone.utc

#: 执行级硬截止相对单次探针超时的余量。给传输层自己的超时留出返回的机会——看门狗只该在
#: 传输层**根本不遵守**超时时才动手，不该抢在它前面把一次正常的慢响应判死。
PROBE_WATCHDOG_MARGIN_SECONDS = 5.0


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

    def __init__(self, *, workers: int, backlog: int | None = None) -> None:
        if isinstance(workers, bool) or not isinstance(workers, int) or workers <= 0:
            raise ValueError("开通执行器至少要有一条线程")
        self._queue: queue.Queue[Callable[[], None] | None] = queue.Queue(
            maxsize=backlog if backlog is not None else workers * 2
        )
        self._stopping = threading.Event()
        self._threads = [
            threading.Thread(
                target=self._loop, name=f"lingxi-gateway-onboarding-{index}", daemon=True
            )
            for index in range(workers)
        ]

    def start(self) -> None:
        for thread in self._threads:
            thread.start()

    def submit(self, task: Callable[[], None]) -> bool:
        """排一条链。队列满或已停机时返回 ``False``，**不阻塞调用线程**。"""

        if self._stopping.is_set():
            return False
        try:
            self._queue.put_nowait(task)
        except queue.Full:
            logger.warning("开通执行器队列已满，本次开通不受理")
            return False
        return True

    def stop(self) -> None:
        """停止领取新链。**不打断在途的那一条**——它自己会看停止标志收口。"""

        self._stopping.set()
        for _ in self._threads:
            try:
                self._queue.put_nowait(None)
            except queue.Full:  # pragma: no cover - 满队列时工作线程自己会看到标志
                pass

    def join(self, timeout: float) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

    @property
    def alive(self) -> bool:
        return any(thread.is_alive() for thread in self._threads)

    def _loop(self) -> None:
        while True:
            task = self._queue.get()
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


class _RosterRows:
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


class _CatalogNotifier:
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
# 装配
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class WiredOnboarding:
    """装配好的一套：**同一个** runner 实例 + 它专属的执行器。"""

    runner: OnboardingRunner
    executor: OnboardingExecutor


def build_onboarding_runner(
    config: GatewayConfig,
    *,
    audit: Any,
    should_stop: Callable[[], bool],
    employment_access_token: Callable[[], str] | None = None,
) -> WiredOnboarding | None:
    """装出正式的首次开通编排；前置不齐就返回 ``None`` 并留下**恰一条**审计。

    ``employment_access_token`` 是在职状态实时回读所用的**专用授权主体派生令牌**供给。
    它**默认为 ``None``，当前部署下也没有合法的值可填**：那条 ``refresh_token`` 是一次性
    的，全系统只允许一个消费者（凭据轮换职责，住在 ``lingxi-scheduler``），2026-08-08
    的授权码被烧事故正是"两个客户端抢同一条独占通道"的形状。因此这一格是一个**显式的
    注入点 + 一条已登记的待决策**，不是这里现造一个供给就能补上的洞——见 Story 报告与
    《当前能力》。

    与 Epic C 的 ``permission_table_access_token`` 是同一姿态：留注入点、失败关闭、
    留痕，不替产品负责人做"谁来消费那条一次性凭据"的决定。
    """

    missing = _missing_prerequisite(config, employment_access_token)
    if missing is not None:
        reason, variable = missing
        facts: dict[str, str] = {"reason": reason}
        if variable:
            facts["variable"] = variable
        # **恰一条**审计，只报变量名，不回显任何值。
        audit.record("onboarding.runner_not_wired", **facts)
        logger.warning(
            "首次开通编排未装配（%s%s）；未开通用户仍会收到冻结的 LX-ONBOARD-001，"
            "gateway 其余职责照常运行",
            reason,
            f"：{variable}" if variable else "",
        )
        return None

    from lingxi.adapters.delegated_credentials import registered_delegated_subject_open_id
    from lingxi.adapters.feishu_directory import FeishuDirectoryClient, FeishuEmploymentReader
    from lingxi.adapters.feishu_user_message import FeishuUserMessages
    from lingxi.adapters.mcp_token_cipher import McpTokenCipher
    from lingxi.adapters.postgres_conversation import PostgresGatewayStore
    from lingxi.adapters.postgres_galaxy_snapshot import PostgresGalaxySnapshotReader
    from lingxi.adapters.postgres_identity import PostgresAppUserStore, PostgresOrgSnapshotStore
    from lingxi.adapters.postgres_mcp_token import PostgresMcpTokenStore, token_cipher_provider
    from lingxi.adapters.postgres_permission_publish import PostgresPermissionPublishStore
    from lingxi.adapters.postgres_roster_snapshot import PostgresRosterSnapshotStore
    from lingxi.adapters.query_mcp_probe import QueryMcpProbe
    from lingxi.adapters.role_function_map_file import load_role_function_map
    from lingxi.adapters.user_environment import LocalUserEnvironment
    from lingxi.config.content import default_content_catalog
    from lingxi.core.identity.onboarding_runner import AutoOnboardingRunner
    from lingxi.core.permission.mcp_readiness import McpReadinessConfirmation, ReadinessSchedule

    try:
        role_function_map = load_role_function_map()
    except (OSError, ValueError) as error:
        # 只记异常类型：配置解析失败的正文可能带上文件内容片段。读不出来时**不能**退化成
        # 空映射——那会让所有角色变成"未映射"，于是每个人都被算成无可用权限，是一种看起来
        # 完全正常的失败。
        audit.record(
            "onboarding.runner_not_wired",
            reason="role_function_map_unavailable",
            error=type(error).__name__,
        )
        logger.error("角色职能映射配置不可用，首次开通编排不装配 error=%s", type(error).__name__)
        return None

    dsn = str(config.postgres_dsn)
    timeouts = config.postgres_timeouts
    clock = monotonic_utc_clock()

    tokens = PostgresMcpTokenStore(
        dsn, cipher=McpTokenCipher(str(config.mcp_token_encrypt_key)), timeouts=timeouts
    )
    schedule = ReadinessSchedule(probe_timeout_seconds=config.query_mcp_timeout_seconds)
    probe = QueryMcpProbe(
        endpoint=str(config.query_mcp_endpoint),
        token_provider=token_cipher_provider(tokens),
        timeout_seconds=config.query_mcp_timeout_seconds,
    )
    _assert_probe_timeouts_agree(probe=probe, schedule=schedule)
    guarded_probe = HardDeadlineProbe(
        probe=probe,
        timeout_seconds=schedule.probe_timeout_seconds + PROBE_WATCHDOG_MARGIN_SECONDS,
    )

    executor = OnboardingExecutor(workers=config.onboarding_workers)
    runner = AutoOnboardingRunner(
        directory=PostgresOrgSnapshotStore(dsn, timeouts=timeouts),
        employment=FeishuEmploymentReader(
            client=FeishuDirectoryClient(base_url=config.feishu_base_url),
            access_token=employment_access_token,  # type: ignore[arg-type]  # 前置已保证非空
        ),
        roster=_RosterRows(PostgresRosterSnapshotStore(dsn, timeouts=timeouts)),
        galaxy=PostgresGalaxySnapshotReader(dsn, timeouts=timeouts),
        provisioning=PostgresAppUserStore(dsn, timeouts=timeouts),
        users=PostgresAppUserStore(dsn, timeouts=timeouts),
        environment=LocalUserEnvironment(
            root=str(config.user_env_root), mcp_endpoint=str(config.query_mcp_endpoint)
        ),
        tokens=tokens,
        decisions=PostgresPermissionPublishStore(dsn, timeouts=timeouts),
        readiness=McpReadinessConfirmation(
            probe=guarded_probe,
            store=tokens,
            audit=audit,
            clock=clock,
            # 阻塞式确认真的会等三分钟。它跑在开通执行器自己的线程上，因此这里等待
            # 不挡住任何一条 gateway 循环；用 `time.sleep` 而不是停止标志的 `wait`，
            # 是因为 `AutoOnboardingRunner` 自己在每一步之间看停止标志，而一次已经
            # 发出去的探针不该被停机信号中途丢弃（结论会落库）。
            sleep=time.sleep,
            schedule=schedule,
        ),
        notifier=_CatalogNotifier(
            sender=FeishuUserMessages(
                base_url=config.feishu_base_url,
                app_id=config.app_id,
                app_secret=str(config.app_secret),
            ),
            catalog=default_content_catalog(),
        ),
        ledger=PostgresGatewayStore(dsn, timeouts=timeouts),
        audit=audit,
        role_function_map=role_function_map,
        # 每次判定现读一次登记表（只读 `feishu_delegated_subject`，不碰凭据文件、
        # 不碰 refresh_token）：换主体之后旧值会让新的专用授权账号落回普通员工路径。
        delegated_subject=lambda: registered_delegated_subject_open_id(dsn, timeouts=timeouts),
        submit=executor.submit,
        sleep=time.sleep,
        clock=clock,
        should_stop=should_stop,
        publish_wait_seconds=config.onboarding_publish_wait_seconds,
    )
    logger.info(
        "首次开通编排已装配 线程数=%s 就绪节奏=%s/%s/%s",
        config.onboarding_workers,
        schedule.attempt_offsets()[0],
        schedule.interval_seconds,
        schedule.budget_seconds,
    )
    return WiredOnboarding(runner=runner, executor=executor)


def _missing_prerequisite(
    config: GatewayConfig, employment_access_token: Callable[[], str] | None
) -> tuple[str, str] | None:
    """按固定次序找出第一项缺失的前置。次序固定是为了让审计里的原因可预期。"""

    from lingxi.adapters.mcp_token_cipher import MASTER_KEY_ENV

    for variable, value in (
        (MASTER_KEY_ENV, config.mcp_token_encrypt_key),
        ("LINGXI_QUERY_MCP_ENDPOINT", config.query_mcp_endpoint),
        ("LINGXI_USER_ENV_ROOT", config.user_env_root),
    ):
        if not value:
            return "missing_environment_variable", variable
    if employment_access_token is None:
        # 在职状态是**产品合同的硬门槛**（`V-开通-07`：非在职不建档、不发权限），
        # 没有它就没有合法的开通判定，不能"先跳过这一步"。
        return "employment_access_token_unwired", ""
    return None


def _assert_probe_timeouts_agree(*, probe: Any, schedule: Any) -> None:
    """装配断言 2：传输超时与就绪节奏的单次超时必须逐值相等，且不超过轮询间隔。

    不相等时，就绪那一侧算出来的"结论最晚什么时候落地"就是假的：它按
    ``预算 + 单次超时`` 给上游承诺收口，而真正卡住链路的是传输层那个数。
    """

    if probe.timeout_seconds != schedule.probe_timeout_seconds:
        raise RuntimeError("探针传输超时必须与就绪节奏的单次超时一致，否则收口上界是假的")
    if schedule.probe_timeout_seconds > schedule.interval_seconds:  # pragma: no cover - 上游已守
        raise RuntimeError("单次探针超时不得大于轮询间隔：否则整轮确认会无上界地拖长")


def assert_single_onboarding_runner(*runners: object) -> None:
    """**双注入点必须拿到同一个 runner 实例**（#65 开工卡必含项，PR #205 二级审查 P3-2）。

    ``build_supervisor`` 与 ``build_onboarding_reconciler`` 是两个独立注入点，各自有自己的
    失败关闭缺省。只喂其中一个，另一个就会静默落回桩：对账扫描认领到的孤儿会被桩"认领即
    平账"地烧掉——它返回一个内部故障结果，扫描如实记账、这条事件到此为止，而唯一的证据是
    一行 INFO 级 ``onboarding.reconciled state=internal_error``。用户那边只剩一个「已收到」
    的表情，永远等不到开通。

    因此装配之后立刻比对身份（``is``，不是相等）：不是同一个实例就**启动即失败**。

    **至少要两个**：只传一个（例如某个注入点的回调根本没触发、调用方漏了一路）时这条
    断言会退化成永远成立的空话，因此少于两个直接判失败。
    """

    if len(runners) < 2:
        raise RuntimeError(
            "开通编排装配断言必须拿到两个注入点各自采用的实例：少于两个说明有一路没有报告，"
            "断言会退化成空话"
        )
    first = runners[0]
    for other in runners[1:]:
        if other is not first:
            raise RuntimeError(
                "build_supervisor 与 build_onboarding_reconciler 必须拿到同一个 "
                "OnboardingRunner 实例：不同实例会让对账孤儿被失败关闭桩静默平账"
            )
