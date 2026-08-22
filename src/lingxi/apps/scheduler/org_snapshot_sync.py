"""组织快照同步职责：:class:`OrgSnapshotSyncDuty`（Issue #250，Epic B 缺件）。

四张 ``feishu_org_*`` 组织快照表此前全空，且产品侧没有任何东西会写它们——写入
适配器（``adapters/postgres_identity.py`` 的 ``PostgresOrgSnapshotStore``）与
读取适配器（``adapters/feishu_directory.py`` 的 ``FeishuDirectoryClient`` +
``adapters/feishu_org_snapshot_reader.py`` 的 ``read_org_snapshot``）都已就绪，
本模块是把两者接起来的唯一调用点。首次开通链的第一步是「组织快照定位身份」
（``AutoOnboardingRunner`` 经 ``PostgresOrgSnapshotStore.lookup``）；查不到就直接
落 ``LX-ONBOARD-001``（已转交管理员），因此这份快照是首次开通链能不能真的跑出
结果的前置。

## 每 UTC 日至多一轮

一轮同步要发起数百次分页请求（8 个关联租户 × 约 25 个部门的递归下钻 × 应用/用户
两条路径，Issue #250 编排者 2026-08-19 实测规模），而组织架构的变动频率远低于
花名册的每日节奏。高频跑它只会不必要地打满对端速率与本进程资源，换不来任何新鲜度
收益。因此按**日**节奏，形状照 :class:`~lingxi.apps.scheduler.roster_audit.
RosterAuditDuty` 的 ``_completed_on`` 水位（同一天只做一次，不做才有意义的重复
工作）——与花名册不同的是，这里没有"空差异不发"这类产品概念，只有"今天有没有
成功换一轮"。

**当日水位对进程重启保持——但只靠读库，不靠租约。** ``_completed_on`` 本身仍是
内存值，进程重启会清零；``run_once`` 因此在当天第一次调用时额外问一次
``store.has_complete_run_on(today)``（读 ``feishu_org_sync_run`` 里今天 UTC 有没有
``complete`` 批次），问过之后当天不再重复问。这不是数据库租约或领导权选举——本仓库
当前是**单实例假设**（Epic C 冻结审查已作为知情接受登记），这里只是把"今天做没做过"
这一件事从内存挪到已经在写的表上，避免重启后当天再跑一次数百次分页请求的全量扫描
（Issue #250 编排者复查 F8；那次重复扫描本身会被 ``commit_batch`` 内的
``superseded`` 收敛掉，不是数据风险，纯粹是浪费）。

## 失败就保留上一份，不覆盖

**任何读取失败（传输异常、分页停滞、递归上界、身份字段缺失）都不会调用
``commit_batch``**：``read_org_snapshot`` 对这些情况一律原样抛出异常，本职责
接住之后只记审计、不置位当日水位，让下一轮重试；库里最近一次的完成批次原样保留。
**完整性校验不通过同理**：``commit_batch`` 内部调用
``core/identity/org_snapshot.require_complete_batch``，不通过时只落一条
``failed`` 批次、不提交任何成员行，并重新抛出
``SnapshotIntegrityError``——本职责同样只记审计、不置位水位。两条路径合起来就是
"空源 / 半页 / 超时 / 格式异常不得替换基线"：判据本身不在这里，**这里只负责不去
绕开它**。

## 令牌供给：两条路径复用装配层已有的供给，不新增凭据材料

- **用户身份路径**（``FeishuDirectoryClient.list_collaboration_tenants`` /
  ``list_visible_organization``）用的令牌供给与首次开通编排的在职状态实时回读
  是**同一个** ``Callable[[], str]``（``apps/scheduler/assembly.py`` 里的
  ``supply``，源头是专用授权主体那条一次性 ``refresh_token``）。**不新增第二个
  消费者**——那条一次性令牌全系统只允许一个消费者，2026-08-08 曾因此烧掉产品
  负责人的一次性授权码。``RosterAccessTokenProvider`` 的"手上有新鲜的就直接给"
  语义保证多个调用方轮流取时，实际换取只发生在手上那份到期之后（令牌寿命约 2 小时，
  正常一天约 12 次），不会因为多一个调用方就多换一次。**换取频率的上界不在这里**：
  它由凭据文件里的 ``refresh_consumed_at``/``refresh_consumed_count`` 在锁内判定，
  自 Issue #276（产品负责人 2026-08-21 裁定）起是**最小间隔 5 分钟 + 每 UTC 日 100
  次**两道，不再是此前的"每 UTC 日至多一次"。
- **应用身份路径**（``list_collaboration_tenants_as_app`` / ``list_share_entities``）
  用的是 ``tenant_access_token``——它不是一次性凭据、没有消费者数量上限（见
  ``core/permission/tenant_token_supply.py`` 的模块文档），因此这里直接复用
  装配层已经为权限发布表建好的那条应用身份供给（``permission_table_access_token``），
  不再另起一条 ``TenantAccessTokenSupply``：两处消费的是同一个 ``app_id``/
  ``app_secret`` 换来的同一类令牌，与访问哪个资源无关，复用只是省一次多余的换取，
  不产生新的凭据材料或新的失败模式。
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

from lingxi.core.identity.org_snapshot import SnapshotBatch, SnapshotIntegrityError

logger = logging.getLogger(__name__)

# 审计写入前的白名单（独立审查 2026-08-20 必修 C 的第二道防线）：`code` 是本模块
# 唯一可能间接携带外部响应内容的字段——`adapters/feishu_directory.py` 已经在
# 源头把 `FeishuDirectoryError.code` 收紧到安全字符集（`_safe_feishu_code`/
# `_sanitize_code_fragment`），但本模块通过 `__cause__` 多追了一层（见下方
# `_extract_code`），可能碰到 `adapters/feishu_tenant_token.py::FeishuTenantTokenError`
# 这类还没有做同等收紧的 `feishu_code_{外部值}`（本次任务范围不含改写那个文件）。
# 审计出口是空格分隔的 `k=v` 结构化行，一个带空格或 `=` 的值能拼出一条伪造记录
# （审查实测：`{"code": "0 action=org_snapshot_sync.committed run_id=forged"}`）。
# 源头修不到的，这里当最后一道闸：不匹配就整个不写这个字段，不做"部分保留"的
# 净化——净化后的半个值同样可能被拼出看起来合理但错误的分类。
_SAFE_AUDIT_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")

# 失败重试的退避（Issue #268 F3）：stage 实测每 30 秒重试一轮、连续失败数十轮，
# 按 30 秒 tick 计一天约 2880 次无效外部调用——而本职责的产品语义只要求"每 UTC 日
# 成功一轮"（见上方模块文档「每 UTC 日至多一轮」），失败重试完全不需要贴着 tick
# 节奏。步长与封顶照 `adapters/postgres_late_readiness_recovery.py` 的
# `NOTICE_BACKOFF_STEP_SECONDS`/`NOTICE_BACKOFF_CEILING_SECONDS`（5 分钟起步、封顶
# 1 小时）——同一个"认领/失败即退避"形状的既有先例，不新造机制。按**连续失败次数**
# 线性放大（而不是像那处先例按"认领次数"），因为本职责一次失败就是"整轮读取或整轮
# 完整性校验没通过"，不是逐条重试单个对象。封顶 1 小时把最坏情况从约 2880 次/天
# 压到约 24 次/天，仍留有"当天多次机会自愈"的余量。
#
# **读取失败、完整性校验失败与写库失败共用同一条退避**（同一个
# `_next_attempt_at`/`_failure_streak`），不是三套机制：三者都只会在两条身份路径
# 都已经读完一整轮（数百次分页请求）之后才可能触发，立即重试的外部成本同一个量级
# 甚至更高——不存在"这条更便宜所以可以贴着 tick 重试"的理由（独立审查 2026-08-20
# 必修 D：给 `commit_failed` 补齐时，用的正是当初给 `integrity_rejected` 扩大
# 范围的同一条理由）。写库失败的 `raise` 本身不受影响，仍然原样冒泡——这里只是在
# 冒泡前把状态记好，下一次 `run_once()` 调用时退避门禁自然生效。任何一次真正往前
# 走了（提交成功、或从持久化水位发现今天已完成）都会把两个字段一起清零，见
# `run_once`。
# `_next_attempt_at` 是跨 UTC 日界的绝对时间戳，不在日界特意清零：最坏情况下只会
# 把新一天的第一次尝试再晚半小时到一小时，换来的是代码不必再区分"日界内退避"与
# "日界外退避"两套语义——`_completed_on`/持久化水位已经保证了"当天完成后不会再被
# 这条退避挡住"。
#
# **基准时刻是"失败发生的那一刻"，不是轮开始时刻**（冻结候选审查 2026-08-21 的 F4）：
# 一整轮全量扫描 stage 2026-08-21 实测约 345 秒，比第一档退避（300 秒）还长；用轮开始
# 时刻当基准时，`_next_attempt_at` 在失败发生时就已经在过去，第一档退避事实上从不生效。
# 判据落在 `_advance_backoff` 内部（它自己现取 `self._clock()`），见该方法文档字符串。
READ_FAILURE_BACKOFF_STEP_SECONDS = 300
READ_FAILURE_BACKOFF_CEILING_SECONDS = 3600

# 连续撞「整轮预算」的升级阈值（独立审查二轮 P2-B4）。`round_budget_exceeded`
# 只是众多 `read_failed` 的 code 之一，正常靠上面共用的退避静默重试即可自愈——
# 但如果它**连续**出现，说明的不是一次网络抖动，而是
# `LINGXI_ORG_SNAPSHOT_ROUND_BUDGET_SECONDS` 配的预算本身就小于真实一轮耗时：
# 每一轮都会在完全相同的地方撞线，而失败路径的语义是「保留上一份、不覆盖基线」，
# 这意味着快照会**静默地**永远停在旧数据上——库里仍然"有数据"，不会像空表那样
# 显眼，运维不会自己发现。三轮（约 5+10+15=30 分钟，按第一档起步的线性退避）
# 是"给一次真实的偶发拥堵机会自愈，但不再继续假装这是偶发"之间的折中，与
# `MIN_ORG_SNAPSHOT_ROUND_BUDGET_SECONDS`（`apps/scheduler/config.py`）的启动期
# 下限校验是两道独立防线：那道挡的是"明显不可能"的误配，这道挡的是"启动时看起来
# 合理、但对当前关联组织规模而言其实不够"的配置在运行期持续暴露出来。
CONSECUTIVE_ROUND_BUDGET_EXCEEDED_ESCALATION_THRESHOLD = 3


class TokenSupplyFailure(RuntimeError):
    """令牌供给失败的安全分类（Issue #250 编排者复查 F6）。

    应用令牌、用户令牌、真实扫描三种失败此前在 ``org_snapshot_sync.read_failed``
    审计里只留下 ``error=<异常类型>``，分辨不出到底是哪条供给出的问题——运维排障
    因此没法直接判断"该去查哪条令牌"还是"这是一次真实扫描失败"。这里只包一层
    分类标签（``supply``），**不携带原始异常的正文或消息**：调用方（
    ``apps/scheduler/assembly.py``）分别包装两个供给的调用点，抛出时用
    ``raise TokenSupplyFailure(...) from error`` 保留因果链供本地调试，但审计只读
    ``supply`` 与 ``type(error).__name__``，两者都不含令牌值。
    """

    def __init__(self, supply: str) -> None:
        super().__init__(f"组织快照令牌供给失败：{supply}")
        self.supply = supply


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
    ) -> None:
        if not source_app_id:
            raise ValueError("source_app_id 不能为空——它是写进 feishu_org_sync_run 的必填列")
        self._read_snapshot = read_snapshot
        self._store = store
        self._audit = audit
        self._source_app_id = source_app_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._stop = threading.Event() if stop is None else stop
        self._completed_on: date | None = None
        # 本进程今天是否已经问过持久化水位（F8）。只问一次：问过之后，"今天到底
        # 完成没有"完全由 `_completed_on`（本进程自己跑出来的结果）决定，不需要
        # 每一轮都去读库——单实例假设下不会有别的进程在这中间把水位从 False 改成
        # True（Epic C 冻结审查已登记的既知前提，见模块文档）。
        self._checked_persisted_watermark_for: date | None = None
        # 失败推进退避（Issue #268 F3，见模块顶部 `READ_FAILURE_BACKOFF_*` 的形状
        # 说明）。两个字段只在读取失败或完整性校验不通过时推进，任何一种"本轮真的
        # 往前走了"（提交成功、或从持久化水位发现今天已完成）都会清零。
        self._next_attempt_at: datetime | None = None
        self._failure_streak = 0
        # 连续撞「整轮预算」计数（P2-B4，见模块顶部
        # `CONSECUTIVE_ROUND_BUDGET_EXCEEDED_ESCALATION_THRESHOLD` 的取值依据）。
        # 只在 `read_failed` 分支里推进/清零，与 `_failure_streak` 分开计——
        # `_failure_streak` 统计"连续多少轮没能往前走"（任何原因），这个字段
        # 统计"连续多少轮**恰好都是**撞预算"，中间夹一次其他原因的失败
        # （例如一次纯网络抖动的 transport_error）就清零，不算作预算配置问题
        # 的证据。
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
        """记一次"这轮没能往前走"，返回本次算出的退避秒数（Issue #268 F3）。

        **退避基准是"失败发生的那一刻"，在这里现取**（冻结候选审查 2026-08-21 的 F4），
        不接受调用方传进来的时刻。此前三个失败分支传的都是 ``run_once`` 顶部取的
        **轮开始**时刻，而一整轮全量扫描 stage 2026-08-21 实测约 **345 秒**——比第一档
        退避（300 秒）还长。于是失败发生时 ``_next_attempt_at = 轮开始 + 300s`` 已经
        在过去，下一 tick 的退避门禁立刻放行，整轮数百次外部调用照发，而审计里却明明
        白白写着 ``backoff_seconds=300``：第一档退避事实上从不生效，读审计的人还会被
        它误导。时刻在这里现取，"退避 N 秒"就是从失败那一刻起的真实 N 秒。

        在方法内部取而不是让每个调用点各自取，是为了让这条不变量**没有第四个调用点
        可以忘记**：新增失败分支时不可能再传错基准。
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

    def run_once(self) -> str | None:
        """跑一轮。返回写入的 ``run_id``；未执行或本轮未能提交时返回 ``None``。"""

        if self._stop.is_set():
            return None
        now = self._clock()
        today = now.date()
        if self._completed_on == today:
            return None

        if self._checked_persisted_watermark_for != today:
            # 进程重启后内存水位归零：不补这一步，当天会做第二次全量扫描（数百次
            # 分页请求，外加一个稍后被 `superseded` 收敛掉的重复批次）——不是凭据
            # 风险（一次性 refresh_token 的换取频率上界落在凭据文件而非内存，自
            # Issue #276 起是"最小间隔 5 分钟 + 每 UTC 日 100 次"两道，见
            # `RosterAccessTokenProvider`），但纯属浪费（Issue #250
            # 编排者复查 F8）。查询失败按"未知"处理、不阻塞本轮：这只是一个避免
            # 重复工作的优化，不是完整性判据本身。
            self._checked_persisted_watermark_for = today
            try:
                already_done = self._store.has_complete_run_on(today)
            except Exception as error:  # noqa: BLE001 - 只记异常类型
                self._audit.record("org_snapshot_sync.watermark_check_failed", error=type(error).__name__)
                logger.warning(
                    "组织快照当日持久化水位检查失败，按未知处理、继续尝试本轮 error=%s",
                    type(error).__name__,
                )
            else:
                if already_done:
                    self._completed_on = today
                    # 本进程自己一次读取都没做就发现"今天已经完成"：不是这里的
                    # 失败推进了退避，但之前若有残留的退避状态（例如昨天读取失败
                    # 到今天才被别的实例补上）已经没有意义，一并清零。
                    self._reset_backoff()
                    self._audit.record("org_snapshot_sync.already_completed_today", source="persisted_watermark")
                    return None

        if self._next_attempt_at is not None and now < self._next_attempt_at:
            # 退避窗口内：安静跳过，不发起外部读取。不留审计——F3 要压的是外部
            # 调用次数，把每 30 秒一次的跳过原样记下来会制造同一量级的审计噪音，
            # 与目标相悖；`read_failed` 审计本身已经带上 `attempt`/`backoff_seconds`，
            # 足够看出"这一轮之后进入了退避"。
            return None

        try:
            batch = self._read_snapshot()
        except Exception as error:  # noqa: BLE001 - 只记异常类型，正文可能带响应内容
            fields: dict[str, object] = {"error": type(error).__name__}
            supply = getattr(error, "supply", None)
            if isinstance(supply, str) and supply:
                # 令牌供给失败时额外标注是哪一条（F6）：不读取原始异常的正文或
                # 消息，只读 `TokenSupplyFailure.supply` 这个安全分类标签。
                fields["supply"] = supply
            code = getattr(error, "code", None)
            if not (isinstance(code, str) and code):
                # `TokenSupplyFailure` 本身没有 `.code`（它只有 `.supply`），但它
                # 用 `raise TokenSupplyFailure(...) from error` 包住的原始异常
                # 可能是带 `code` 的 `FeishuTenantTokenError`（应修 F，独立审查
                # 2026-08-20 可选建议）：不追这一层，令牌供给失败时审计只剩
                # `supply=app_access_token`，分辨不出令牌那侧具体是哪种失败。
                # 只看一层 `__cause__`（不递归），且同样只取安全分类标签本身。
                cause = getattr(error, "__cause__", None)
                code = getattr(cause, "code", None)
            if isinstance(code, str) and code and _SAFE_AUDIT_CODE.fullmatch(code):
                # `FeishuDirectoryError.code`（Issue #268 F1）：其类文档承诺该属性
                # "供程序判断，消息里不含任何凭据"——只读这一个分类标签，不读
                # `str(error)`（消息正文）、不读响应正文、不读令牌或 URL 查询串。
                # 非 `FeishuDirectoryError` 的其他异常没有这个属性，`getattr`
                # 拿到 `None`，仍然只靠上面已经记下的 `type(error).__name__` 归类。
                # 白名单核对见模块顶部 `_SAFE_AUDIT_CODE` 的注释——不匹配就整个
                # 不写这个字段，不做部分保留。
                fields["code"] = code
            backoff_seconds = self._advance_backoff()
            fields["attempt"] = self._failure_streak
            fields["backoff_seconds"] = backoff_seconds
            self._audit.record("org_snapshot_sync.read_failed", **fields)
            logger.error(
                "组织快照读取失败，保留上一份完成批次，退避 %s 秒后重试 error=%s code=%s supply=%s",
                backoff_seconds,
                type(error).__name__,
                # 日志与审计读同一个已经过白名单的值（`fields.get`），不是分别
                # 各判一次——否则白名单只挡住了审计行，日志行仍然能被同一个
                # 不安全的 `code` 值注入（独立审查必修 C 的同一个风险，只是换了
                # 个出口）。
                fields.get("code", "unknown"),
                supply if isinstance(supply, str) else "unknown",
            )
            if code == "round_budget_exceeded":
                self._consecutive_round_budget_exceeded += 1
            else:
                # 中间夹了一次其他原因的失败：不是"预算持续不够"的证据，清零
                # 计数——见 `__init__` 里 `_consecutive_round_budget_exceeded`
                # 的字段注释。
                self._consecutive_round_budget_exceeded = 0
            if (
                self._consecutive_round_budget_exceeded
                >= CONSECUTIVE_ROUND_BUDGET_EXCEEDED_ESCALATION_THRESHOLD
            ):
                # 打破静默活锁（P2-B4）：连续撞预算不再只是普通 `read_failed` 里
                # 埋着的一个 `code` 值，额外升一条独立的审计 action + WARNING
                # 日志——运维扫审计或日志时能直接看到这句话，不需要先数
                # `read_failed` 记录里 `code=round_budget_exceeded` 出现了几次。
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
            return None

        try:
            run_id = self._store.commit_batch(batch, source_app_id=self._source_app_id, started_at=now)
        except SnapshotIntegrityError as error:
            # 也推进退避（与读取失败共用同一条，见模块顶部常量注释）：走到这一步
            # 说明两条身份路径都已经读完一整轮（数百次分页请求）才在交叉校验上失败，
            # 贴着 tick 立即重试的外部成本与读取失败同一个量级，没有理由只挡一条。
            backoff_seconds = self._advance_backoff()
            # 走到这里说明本轮读取其实已经完整跑完（两条身份路径都读完了才会
            # 交叉校验），不是撞预算——清零连续计数，不让它跟前面偶然出现的
            # `round_budget_exceeded` 拼成一条假的"连续"证据。
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
        except Exception as error:  # noqa: BLE001 - 只记异常类型，写库失败原样上抛前先留痕
            # 也推进退避（独立审查 2026-08-20 必修 D）：这里与 `integrity_rejected`
            # 处在流水线同一个位置——同样是两条身份路径都已经读完一整轮（数百次
            # 分页请求）之后才撞上的失败，贴着 tick 立即重试的外部成本同一个量级，
            # 上面给 `integrity_rejected` 扩大退避范围的理由逐字适用于这里。
            # `raise` 本身不变：写库失败仍然原样冒泡，让 `SchedulerLoop` 按既有
            # 纪律记录并隔离，这里只是在冒泡前先把退避记上。
            backoff_seconds = self._advance_backoff()
            # 同上：本轮读取已经完整跑完，不是撞预算，清零连续计数。
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
        return run_id
