"""组织快照同步职责：:class:`OrgSnapshotSyncDuty`。

四张 ``feishu_org_*`` 组织快照表此前全空，本模块是把已就绪的读写适配器接起来
的唯一调用点——首次开通链第一步「组织快照定位身份」查不到就直接落
``LX-ONBOARD-001``（已转交管理员），这份快照是首次开通链能不能跑出结果的前置。
**每 UTC 日至多一轮**：一轮要发起数百次分页请求，组织架构变动频率远低于花名册。
**当日水位对进程重启保持，但只靠读库、不靠租约**：内存水位重启归零后，当天
第一次调用额外查一次持久化的完成批次记录，问过之后不再重复问（单实例假设，
重复扫描本身会被 ``commit_batch`` 的 ``superseded`` 收敛，不是数据风险）。
**任何读取失败或完整性校验不通过都不提交**：只记审计、不置位水位，库里最近
一次成功批次原样保留——空源/半页/超时/格式异常不得替换基线。令牌供给复用装配层已有的两条（用户身份、应用身份），不新增消费者。**整轮读取
＋校验＋提交派进后台线程**：一轮同步实测约 345 秒会同时撞穿容器健康检查阈值
与心跳超时，主线程只同步等一个很短的上限就拿回控制权，跑满整轮时提前返回、
后台线程自己收口。失败按连续失败次数线性退避（封顶 1 小时）。
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from lingxi.apps.scheduler.audit import AuditSink
from lingxi.apps.scheduler.config import SchedulerConfig
from lingxi.core.identity.org_snapshot import SnapshotBatch, SnapshotIntegrityError

logger = logging.getLogger(__name__)

# 审计写入前的白名单：`code` 是本模块唯一可能间接携带外部响应内容的字段——
# 通过 `__cause__` 多追了一层，可能碰到还没做同等收紧的错误码。审计出口是空格
# 分隔的 `k=v` 结构化行，一个带空格或 `=` 的值能拼出一条伪造记录。这里当最后
# 一道闸：不匹配就整个不写这个字段，不做"部分保留"的净化——净化后的半个值
# 同样可能被拼出看起来合理但错误的分类。
_SAFE_AUDIT_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")

# 失败重试的退避：产品语义只要求"每 UTC 日成功一轮"，不需要贴着 tick 节奏。
# 按连续失败次数线性放大（封顶 1 小时）；读取失败、完整性校验失败与写库失败
# 共用同一条退避（同一对 `_next_attempt_at`/`_failure_streak`）。基准时刻是
# "失败发生的那一刻"，在 `_advance_backoff` 内部现取，不接受调用方传入的轮
# 开始时刻——否则一整轮全量扫描耗时若长于第一档退避，退避事实上从不生效。
READ_FAILURE_BACKOFF_STEP_SECONDS = 300
READ_FAILURE_BACKOFF_CEILING_SECONDS = 3600

# 连续撞「整轮预算」的升级阈值。`round_budget_exceeded` 只是众多 `read_failed`
# 的 code 之一，正常靠退避静默重试即可自愈；但连续出现说明的不是一次网络抖动，
# 而是预算配置本身小于真实一轮耗时——快照会静默地永远停在旧数据上，运维不会
# 自己发现。三轮升级为一次显式告警，与启动期下限校验是两道独立防线：那道挡
# "明显不可能"的误配，这道挡"启动时合理、但对当前规模而言其实不够"的配置。
CONSECUTIVE_ROUND_BUDGET_EXCEEDED_ESCALATION_THRESHOLD = 3

# 派发后台"跑一轮"线程后，主线程愿意同步等待的上限。不是这一轮本身的超时
# ——那一轮该跑多久跑多久，交给既有的两道墙钟兜底；这里只是"调用方最多为
# 这一次调用愿意占用多久"，远小于心跳超时默认值，给其余职责留出充裕余量，
# 也远大于既有测试假读取的实际耗时，因此快路径行为不变。
DEFAULT_ROUND_JOIN_TIMEOUT_SECONDS = 2.0

# 后台"跑一轮"线程存活超过这个秒数就判定为疑似僵死，额外记一条独立审计 +
# WARNING 日志（只记一次）。取值等于退避封顶（3600 秒）：线程如果真的还在
# 忙于重试或读取，正常情况下不可能连续存活超过这个数字还没有任何一次成功或
# 失败收口，且严格大于整轮预算的默认值，避免正常一轮跑满预算时被误报。
ROUND_THREAD_STUCK_AFTER_SECONDS = 3600


class TokenSupplyFailureError(RuntimeError):
    """令牌供给失败的安全分类。

    应用令牌、用户令牌、真实扫描三种失败此前在 ``org_snapshot_sync.read_failed``
    审计里只留下 ``error=<异常类型>``，分辨不出到底是哪条供给出的问题。这里只包
    一层分类标签（``supply``），**不携带原始异常的正文或消息**：调用方分别包装
    两个供给的调用点，抛出时用 ``raise TokenSupplyFailureError(...) from error``
    保留因果链供本地调试，但审计只读 ``supply`` 与 ``type(error).__name__``，
    两者都不含令牌值。
    """

    def __init__(self, supply: str) -> None:
        super().__init__(f"组织快照令牌供给失败：{supply}")
        self.supply = supply


#: 向后兼容别名：异常类改名前的旧名字，供未随批次同步更新的调用方与测试导入。
TokenSupplyFailure = TokenSupplyFailureError


class _AuditSink(Protocol):
    def record(self, action: str, /, **fields: object) -> None: ...


class _SnapshotStore(Protocol):
    def commit_batch(
        self,
        batch: SnapshotBatch,
        *,
        source_app_id: str,
        run_id: str | None = None,
        started_at: datetime | None = None,
    ) -> str: ...

    def has_complete_run_on(self, day: date) -> bool: ...


class OrgSnapshotSyncDuty:
    """每 UTC 日至多一轮：读关联组织通讯录 → 校验批次完整性 → 写四张快照表。

    只编排注入的读取函数与写入端口，本身不做任何网络或数据库 I/O——真正的递归
    遍历在 :func:`lingxi.adapters.feishu_org_snapshot_reader.read_org_snapshot`，
    真正的落库与完整性校验在
    :meth:`~lingxi.adapters.postgres_identity.PostgresOrgSnapshotStore.commit_batch`。
    """

    name = "组织快照同步"

    def __init__(
        self,
        *,
        read_snapshot: Callable[[], SnapshotBatch],
        store: _SnapshotStore,
        audit: _AuditSink,
        source_app_id: str,
        clock: Callable[[], datetime] | None = None,
        stop: threading.Event | None = None,
        round_join_timeout_seconds: float = DEFAULT_ROUND_JOIN_TIMEOUT_SECONDS,
    ) -> None:
        if not source_app_id:
            raise ValueError("source_app_id 不能为空——它是写进 feishu_org_sync_run 的必填列")
        self._read_snapshot = read_snapshot
        self._store = store
        self._audit = audit
        self._source_app_id = source_app_id
        self._clock = clock or (lambda: datetime.now(UTC))
        self._stop = threading.Event() if stop is None else stop
        self._round_join_timeout_seconds = round_join_timeout_seconds
        # 上一次派发的后台"跑一轮"线程。``None`` 或已经跑完都表示"可以派发下一轮"；
        # 还活着就说明上一轮还没收工，本轮 `run_once` 的单飞守卫不重复派发。
        self._pending_thread: threading.Thread | None = None
        # `_pending_thread` 派发那一刻的时钟读数（用注入时钟，不是墙钟）——仅用于
        # 判断"这条线程存活了多久"，供僵尸线程告警使用。
        self._pending_thread_started_at: datetime | None = None
        # 当前这条 `_pending_thread` 是否已经因为「存活超硬上限」告警过一次
        # （`ROUND_THREAD_STUCK_AFTER_SECONDS`）。每派发一条新线程时重置为
        # ``False``——同一条线程只告警一次，不同线程各自独立计。
        self._round_thread_stuck_alerted = False
        self._completed_on: date | None = None
        # 本进程今天是否已经问过持久化水位。只问一次：问过之后，"今天到底完成
        # 没有"完全由 `_completed_on`（本进程自己跑出来的结果）决定，不需要每
        # 一轮都去读库——单实例假设下不会有别的进程在这中间把水位改成 True。
        self._checked_persisted_watermark_for: date | None = None
        # 失败推进退避（见模块顶部 `READ_FAILURE_BACKOFF_*` 的形状说明）。两个
        # 字段只在读取失败或完整性校验不通过时推进，任何一种"本轮真的往前走了"
        # （提交成功、或从持久化水位发现今天已完成）都会清零。
        self._next_attempt_at: datetime | None = None
        self._failure_streak = 0
        # 连续撞「整轮预算」计数，只在 `read_failed` 分支里推进/清零，与
        # `_failure_streak`（统计"连续多少轮没能往前走"，任何原因）分开计：
        # 这个字段统计"连续多少轮恰好都是撞预算"，中间夹一次其他原因的失败
        # 就清零，不算作预算配置问题的证据。
        self._consecutive_round_budget_exceeded = 0

    @property
    def completed_on(self) -> date | None:
        """已完成同步的那一天。``None`` 表示本进程实例今天还没成功过一轮。"""

        return self._completed_on

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        self._stop.set()

    def _advance_backoff(self) -> int:
        """记一次"这轮没能往前走"，返回本次算出的退避秒数。

        **退避基准是"失败发生的那一刻"，在这里现取**，不接受调用方传进来的
        时刻：一整轮全量扫描实测比第一档退避（300 秒）还长，若基准取轮开始
        时刻，失败发生时退避窗口已经过去，门禁立刻放行、审计却写着仍在退避，
        误导排障。在方法内部取而不是让每个调用点各自取，是为了让这条不变量
        没有调用点可以忘记。
        """

        self._failure_streak += 1
        backoff_seconds = min(
            self._failure_streak * READ_FAILURE_BACKOFF_STEP_SECONDS,
            READ_FAILURE_BACKOFF_CEILING_SECONDS,
        )
        self._next_attempt_at = self._clock() + timedelta(seconds=backoff_seconds)
        return backoff_seconds

    def _reset_backoff(self) -> None:
        self._next_attempt_at = None
        self._failure_streak = 0
        self._consecutive_round_budget_exceeded = 0

    def _run_round(self, now: datetime, today: date) -> str | None:
        """跑一轮：读一整份组织通讯录 → 校验批次完整性 → 写四张快照表 → 收口。

        ``now``/``today`` 由调用方（``run_once`` 派发后台线程时）取好原样传入，
        与方法内部现取是同一个时刻，行为不变。返回写入的 ``run_id``；未执行或
        本轮未能提交时返回 ``None``。
        """

        if self._stop.is_set():
            return None
        if self._completed_on == today:
            return None

        if self._checked_persisted_watermark_for != today and self._already_done_today(today):
            return None

        if self._next_attempt_at is not None and now < self._next_attempt_at:
            # 退避窗口内：安静跳过，不发起外部读取。不留审计——压的是外部调用
            # 次数，`read_failed` 审计本身已经带上 `attempt`/`backoff_seconds`，
            # 足够看出"这一轮之后进入了退避"。
            return None

        batch = self._read_snapshot_or_record_failure()
        if batch is None:
            return None
        return self._commit_batch_or_record_failure(batch, now=now, today=today)

    def _already_done_today(self, today: date) -> bool:
        """问一次持久化水位（每天只问一次）；今天已完成则收口并返回 ``True``。

        进程重启后内存水位归零：不补这一步，当天会做第二次全量扫描（数百次
        分页请求，外加一个稍后被 ``superseded`` 收敛掉的重复批次），纯属浪费，
        不是凭据风险。查询失败按"未知"处理、不阻塞本轮：这只是一个避免重复
        工作的优化，不是完整性判据本身。
        """

        self._checked_persisted_watermark_for = today
        try:
            already_done = self._store.has_complete_run_on(today)
        except Exception as error:  # 只记异常类型
            self._audit.record(
                "org_snapshot_sync.watermark_check_failed", error=type(error).__name__
            )
            logger.warning(
                "组织快照当日持久化水位检查失败，按未知处理、继续尝试本轮 error=%s",
                type(error).__name__,
            )
            return False
        if not already_done:
            return False
        self._completed_on = today
        # 本进程自己一次读取都没做就发现"今天已经完成"：之前若有残留的退避
        # 状态（例如昨天读取失败到今天才被别的实例补上）已经没有意义，一并清零。
        self._reset_backoff()
        self._audit.record(
            "org_snapshot_sync.already_completed_today", source="persisted_watermark"
        )
        return True

    def _read_snapshot_or_record_failure(self) -> SnapshotBatch | None:
        """读一整份组织通讯录；失败时记审计、推进退避，返回 ``None``。"""

        try:
            return self._read_snapshot()
        except Exception as error:  # 只记异常类型，正文可能带响应内容
            fields: dict[str, object] = {"error": type(error).__name__}
            supply = getattr(error, "supply", None)
            if isinstance(supply, str) and supply:
                # 令牌供给失败时额外标注是哪一条：不读取原始异常的正文或消息，
                # 只读 `TokenSupplyFailureError.supply` 这个安全分类标签。
                fields["supply"] = supply
            code = getattr(error, "code", None)
            if not (isinstance(code, str) and code):
                # `TokenSupplyFailureError` 本身没有 `.code`（它只有 `.supply`），但它
                # 用 `raise TokenSupplyFailureError(...) from error` 包住的原始异常
                # 可能带 `code`：只看一层 `__cause__`（不递归），同样只取安全
                # 分类标签本身。
                cause = getattr(error, "__cause__", None)
                code = getattr(cause, "code", None)
            if isinstance(code, str) and code and _SAFE_AUDIT_CODE.fullmatch(code):
                # `FeishuDirectoryError.code` 承诺"供程序判断，消息里不含任何
                # 凭据"——只读这一个分类标签，不读消息正文、响应正文、令牌或
                # URL 查询串。白名单核对见模块顶部 `_SAFE_AUDIT_CODE`：不匹配
                # 就整个不写这个字段，不做部分保留。
                fields["code"] = code
            backoff_seconds = self._advance_backoff()
            fields["attempt"] = self._failure_streak
            fields["backoff_seconds"] = backoff_seconds
            self._audit.record("org_snapshot_sync.read_failed", **fields)
            logger.error(
                "组织快照读取失败，保留上一份完成批次，退避 %s 秒后重试 error=%s code=%s supply=%s",
                backoff_seconds,
                type(error).__name__,
                # 日志与审计读同一个已经过白名单的值：不分别各判一次，否则白
                # 名单只挡住审计行，日志行仍能被同一个不安全的 `code` 值注入。
                fields.get("code", "unknown"),
                supply if isinstance(supply, str) else "unknown",
            )
            self._track_round_budget_streak(code)
            return None

    def _track_round_budget_streak(self, code: object) -> None:
        """连续撞「整轮预算」达到阈值时升一条独立审计与告警（打破静默活锁）。"""

        if code == "round_budget_exceeded":
            self._consecutive_round_budget_exceeded += 1
        else:
            # 中间夹了一次其他原因的失败：不是"预算持续不够"的证据，清零计数。
            self._consecutive_round_budget_exceeded = 0
        if (
            self._consecutive_round_budget_exceeded
            >= CONSECUTIVE_ROUND_BUDGET_EXCEEDED_ESCALATION_THRESHOLD
        ):
            # 连续撞预算不再只是普通 `read_failed` 里埋着的一个 `code` 值，
            # 额外升一条独立的审计 + WARNING 日志，运维扫审计或日志时能直接
            # 看到这句话，不需要先数 `read_failed` 记录里的出现次数。
            self._audit.record(
                "org_snapshot_sync.round_budget_persistently_exceeded",
                consecutive=self._consecutive_round_budget_exceeded,
            )
            logger.warning(
                "组织快照连续 %s 轮撞上整轮预算上界（round_budget_exceeded）——"
                "预算持续低于单轮实际耗时，快照将永远无法更新，这是配置错误信号："
                "请调大 LINGXI_ORG_SNAPSHOT_ROUND_BUDGET_SECONDS",
                self._consecutive_round_budget_exceeded,
            )

    def _commit_batch_or_record_failure(
        self, batch: SnapshotBatch, *, now: datetime, today: date
    ) -> str | None:
        """提交批次；完整性校验失败或写库失败都推进退避，成功则收口。"""

        try:
            run_id = self._store.commit_batch(
                batch, source_app_id=self._source_app_id, started_at=now
            )
        except SnapshotIntegrityError as error:
            # 也推进退避（与读取失败共用同一条）：走到这一步说明两条身份路径
            # 都已经读完一整轮才在交叉校验上失败，贴着 tick 立即重试的外部
            # 成本与读取失败同一个量级。
            backoff_seconds = self._advance_backoff()
            # 本轮读取其实已经完整跑完（两条身份路径都读完了才会交叉校验），
            # 不是撞预算——清零连续计数，避免拼成一条假的"连续"证据。
            self._consecutive_round_budget_exceeded = 0
            self._audit.record(
                "org_snapshot_sync.integrity_rejected",
                problems=[problem.value for problem in error.report.problems],
                tenants=error.report.tenant_count,
                departments=error.report.department_count,
                members=error.report.member_count,
                attempt=self._failure_streak,
                backoff_seconds=backoff_seconds,
            )
            logger.error(
                "组织快照完整性校验未通过，保留上一份完成批次，退避 %s 秒后重试 problems=%s",
                backoff_seconds,
                ",".join(problem.value for problem in error.report.problems),
            )
            return None
        except Exception as error:  # 只记异常类型，写库失败原样上抛前先留痕
            # 也推进退避：与 `integrity_rejected` 处在流水线同一个位置。`raise`
            # 本身不变——写库失败仍然原样冒泡，这里只是在冒泡前先把退避记上。
            backoff_seconds = self._advance_backoff()
            self._consecutive_round_budget_exceeded = 0
            self._audit.record(
                "org_snapshot_sync.commit_failed",
                error=type(error).__name__,
                attempt=self._failure_streak,
                backoff_seconds=backoff_seconds,
            )
            logger.error(
                "组织快照写入失败，退避 %s 秒后重试 error=%s", backoff_seconds, type(error).__name__
            )
            raise

        self._record_committed(run_id, batch, today)
        return run_id

    def _record_committed(self, run_id: str, batch: SnapshotBatch, today: date) -> None:
        """收口成功批次：置位当日水位、清退避、记审计与摘要日志。"""

        self._completed_on = today
        self._reset_backoff()
        self._audit.record(
            "org_snapshot_sync.committed",
            run_id=run_id,
            tenants=len(batch.tenants),
            departments=len(batch.departments),
            members=len(batch.members),
        )
        logger.info(
            "组织快照已同步 run_id=%s 租户=%s 部门=%s 成员=%s",
            run_id,
            len(batch.tenants),
            len(batch.departments),
            len(batch.members),
        )

    def run_once(self) -> str | None:
        """调用方（`SchedulerLoop`）每个 tick 调用的入口。只做本地、无 I/O 的
        判断决定"该不该起一轮"；真正的一轮（`_run_round`）整体派进后台线程，
        本方法只同步等一个很短的上限就把控制权交还调用方。正常情况下（既有
        测试的假读取都是微秒级）行为与同步调用完全一致；真正跑满整轮预算的
        那一轮，这里提前返回 ``None``——不是"这一轮失败了"，只是"调用方不再
        等"，本轮真正的结果由后台线程自己收口。本方法只被 `SchedulerLoop`
        那一条调度线程串行调用，检查-派发之间没有竞态窗口。
        """

        if self._stop.is_set():
            return None
        now = self._clock()
        today = now.date()
        if self._completed_on == today:
            # 当日水位已满：纯内存比较，不需要看一眼后台线程或退避状态。
            return None

        if self._round_in_flight(now):
            return None

        if self._next_attempt_at is not None and now < self._next_attempt_at:
            # 退避门禁留在主线程（纯本地时刻比较，与 `_run_round` 内部同一条
            # 判据一致）：退避窗口内连线程都不派，不产生任何线程创建开销，也
            # 不重复记审计。
            return None

        return self._dispatch_round(now, today)

    def _round_in_flight(self, now: datetime) -> bool:
        """单飞守卫：上一次派发的后台线程还没收工时返回 ``True``。

        两条线程同时读一整份组织通讯录、同时可能 ``commit_batch``，是比"这
        一轮慢"本身更糟的形状（数据竞争、外部调用量翻倍）。存活超硬上限时
        疑似僵死，响亮告警一次——线程死没死是同一个持续事实，每个 tick 都
        重复上报只会刷屏。
        """

        if self._pending_thread is None or not self._pending_thread.is_alive():
            return False
        if (
            not self._round_thread_stuck_alerted
            and self._pending_thread_started_at is not None
            and (now - self._pending_thread_started_at).total_seconds()
            > ROUND_THREAD_STUCK_AFTER_SECONDS
        ):
            self._round_thread_stuck_alerted = True
            alive_seconds = int((now - self._pending_thread_started_at).total_seconds())
            self._audit.record("org_snapshot_sync.round_thread_stuck", alive_seconds=alive_seconds)
            logger.warning(
                "组织快照后台线程存活 %s 秒仍未收工，超过僵死判定上限 %s 秒——"
                "单飞守卫会继续拒绝派发新线程，快照将持续停在旧数据上，"
                "需要人工介入排查或重启进程 alive_seconds=%s",
                alive_seconds,
                ROUND_THREAD_STUCK_AFTER_SECONDS,
                alive_seconds,
            )
        return True

    def _dispatch_round(self, now: datetime, today: date) -> str | None:
        """派发后台线程跑一轮，同步等一个很短的上限就把控制权交还调用方。"""

        result: dict[str, str | None] = {}

        def worker() -> None:
            try:
                result["run_id"] = self._run_round(now, today)
            except BaseException as error:  # 线程异常不能无声消失
                # 只记异常类型，不记正文：本轮读一整份组织通讯录已经整体挪进
                # 了这条后台线程，`SchedulerLoop` 自己再也看不到这个异常。
                logger.error(
                    "组织快照后台线程出现未预期异常，其余定时职责与下一轮不受影响 error=%s",
                    type(error).__name__,
                )

        thread = threading.Thread(target=worker, name="lingxi-org-snapshot-round", daemon=True)
        self._pending_thread = thread
        self._pending_thread_started_at = now
        self._round_thread_stuck_alerted = False
        thread.start()
        thread.join(timeout=self._round_join_timeout_seconds)
        if thread.is_alive():
            # 仍在跑：不再等——一轮的耗时不得占用调用方所在的线程（`SchedulerLoop`
            # 主循环：心跳与其余职责都在这条线程上）。线程会自己收工，下一个
            # tick 由内存水位或持久化水位观察到。
            logger.info(
                "组织快照仍在后台线程运行，主循环不再等待 timeout=%ss",
                self._round_join_timeout_seconds,
            )
            return None
        return result.get("run_id")


def _stop_aware_sleep(stop: threading.Event) -> Callable[[float], None]:
    """把 `stop.wait` 包一层，让「停机置位后的等待」变成「中止」而不是
    「假装等过了、照样放行」。

    直接把 `stop.wait` 当 `sleep` 注入时，`stop` 一旦置位，限频重试的等待
    会立即返回、与"真的等过了"在调用方看来无法区分——节流因此悄悄失效。
    这里改成：`stop.wait(seconds)` 返回 `True` 时抛出
    `FeishuDirectoryError("stopping")` 中止当前调用，异常冒泡进
    `_run_round()` 既有的 read_failed→退避→保留上一份完成批次路径。只挡
    下一次等待/请求，不中断正在进行中的单次 HTTP 请求。
    """

    from lingxi.adapters.feishu_directory import FeishuDirectoryError

    def sleep(seconds: float) -> None:
        if stop.wait(seconds):
            raise FeishuDirectoryError("stopping")

    return sleep


def _build_org_snapshot_sync_duty(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
    user_access_token: Callable[[], str] | None,
    app_access_token: Callable[[], str] | None,
) -> Any | None:
    """装配组织快照同步职责；前置不齐就**不注册**并留下**恰一条**审计。

    两个令牌供给都是调用方必须交出的前置，``None`` 表示调用方真的没有交出
    任何供给（"未接线"）；正式装配路径 ``build_loop`` 总会建出两条，因此这
    两条分支正常不会在生产触发。**"配了但拿不到令牌"不走这里**：那时职责
    照常注册，失败发生在 ``run_once`` 内部并按分类审计——把运行期的授权
    失败记成"未注册"会让排障去找配置，反过来会让"还没接线"看起来像"接线
    了但一直失败"。
    """

    if user_access_token is None:
        audit.record("org_snapshot_sync.duty_not_registered", reason="user_access_token_unwired")
        logger.warning(
            "调用方未提供组织快照用户身份读取令牌供给，组织快照同步职责不注册；其余定时职责照常运行"
        )
        return None
    if app_access_token is None:
        audit.record("org_snapshot_sync.duty_not_registered", reason="app_access_token_unwired")
        logger.warning(
            "调用方未提供组织快照应用身份读取令牌供给，组织快照同步职责不注册；其余定时职责照常运行"
        )
        return None

    from lingxi.adapters.postgres_identity import PostgresOrgSnapshotStore

    return OrgSnapshotSyncDuty(
        read_snapshot=_build_read_snapshot(config, stop, user_access_token, app_access_token),
        store=PostgresOrgSnapshotStore(config.postgres_dsn, timeouts=config.postgres_timeouts),
        audit=audit,
        source_app_id=config.feishu_app_id,
        stop=stop,
    )


def _build_read_snapshot(
    config: SchedulerConfig,
    stop: threading.Event,
    user_access_token: Callable[[], str],
    app_access_token: Callable[[], str],
) -> Callable[[], Any]:
    """装配整趟递归遍历的读取闭包，含令牌重分类与整轮预算包装。"""

    from lingxi.adapters.feishu_directory import FeishuDirectoryClient
    from lingxi.adapters.feishu_org_snapshot_reader import read_org_snapshot

    # `sleep=_stop_aware_sleep(stop)`：不能直接注入裸的 `stop.wait`——那会让
    # 节流/限频退避在停机置位后的每一次等待都立刻返回 `True` 而不是真的等过
    # 那么久，见 `_stop_aware_sleep` 的文档字符串。不中断进行中的单次 HTTP
    # 请求——中止点仍然只在两次请求之间的等待里。
    client = FeishuDirectoryClient(base_url=config.feishu_base_url, sleep=_stop_aware_sleep(stop))

    def read_snapshot() -> Any:
        # 令牌各解析一次、用于本轮整趟递归遍历（数百次分页请求），不逐次请求
        # 都重新取——两条供给都会在有效期内直接返回缓存值。两个供给分别包
        # 一层 `TokenSupplyFailureError`：不这样做的话，应用令牌、用户令牌、真实
        # 扫描三种失败在审计里全都只剩 `error=<异常类型>`，分辨不出该去查
        # 哪一条；原始异常仍通过 `from error` 保留因果链，审计只读安全标签。
        try:
            app_token = app_access_token()
        except Exception as error:  # 立即重分类，不吞
            raise TokenSupplyFailureError("app_access_token") from error
        try:
            user_token = user_access_token()
        except Exception as error:  # 立即重分类，不吞
            raise TokenSupplyFailureError("user_access_token") from error
        # 整轮预算只包住这一整趟递归遍历，`round_budget` 是 `client` 这一个
        # 实例的作用域化状态，不影响开通链那个独立的 client。撞线时抛出的
        # `FeishuDirectoryError("round_budget_exceeded")` 原样冒泡，落进
        # `_run_round()` 既有的「记 read_failed 审计→退避→保留上一份完成
        # 批次」路径，这里不需要再单独处理。
        with client.round_budget(seconds=config.org_snapshot_round_budget_seconds):
            return read_org_snapshot(client=client, app_token=app_token, user_token=user_token)

    return read_snapshot
