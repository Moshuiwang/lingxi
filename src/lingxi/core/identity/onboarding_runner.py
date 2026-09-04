"""正式首次开通编排：把身份、匹配、建档、用户环境、权限发布与就绪确认串成一条链。

**次序不可调换**：每一步的失败去向显式写在 :meth:`AutoOnboardingRunner._run` 的各段里，
每一步具体怎么做住在 :mod:`~lingxi.core.identity.onboarding_steps`。终态分四类——内测名单
外／确定性业务失败与专用主体／本侧故障／等待类超时——**互斥且先到先得**：一旦选定终态，
后置异常不得把它改写成另一种用户结论（`V-开通-13`）。

``start`` 只做三件事：按 ``open_id`` 去重、交给注入的执行器、立刻返回"已接手"。真实编排单次
耗时可达分钟级（合同允许权限同步等到十五分钟），把一轮定时 tick 占住十五分钟会让凭据轮换、
保留清理、权限发布消费全部停摆；执行器因此是本编排**专属**的线程池。终态由本模块自己主动
私聊告诉用户，编排自担通知并保证幂等。

**本模块只排发布意图，不自己写外部权限表格**：外部表的唯一写入方是发布消费职责，两个进程
同时写同一张表是这条链上最贵的并发错误。
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from lingxi.core.conversation.ports import OnboardingResult, OnboardingState
from lingxi.core.identity.onboarding_config import (
    OnboardingActions,
    OnboardingPolicy,
    OnboardingRecords,
    OnboardingRuntime,
    OnboardingSources,
)
from lingxi.core.identity.onboarding_guards import (
    reject_email_bound_to_another_person,
    reject_zero_galaxy_without_local_grant,
)
from lingxi.core.identity.onboarding_ports import EnvironmentResult
from lingxi.core.identity.onboarding_support import draft_from_member, roster_row_for
from lingxi.core.identity.onboarding_steps import FIRST_ONBOARDING_REASON, OnboardingSteps
from lingxi.core.identity.onboarding_terminal import (
    KEY_COMPLETED,
    KEY_DELEGATED_SUBJECT,
    KEY_INNERTEST_NOT_OPEN,
    KEY_INTERNAL_ERROR,
    KEY_NOT_AUTHORIZED,
    KEY_STALLED,
    KEY_SUSPENDED,
    KEY_SYNC_TIMEOUT,
    KEY_SYNCING,
    STATE_ACTIVE,
    STATE_MCP_SYNCING,
    STATE_PROVISIONING,
    OnboardingChainError,
    _ChainAborted,
    _internal,
    _KEYS_REQUIRING_REFERENCE,
    _Terminal,
    _with_reference,
)
from lingxi.core.identity.preprovision import (
    NULL_DISPATCH_LEDGER,
    ORIGIN_PREPROVISION,
    PreprovisionGrant,
    deliver_silently,
    import_preprovision_grant,
    is_system_trigger,
    origin_of,
    run_system_onboarding,
)
from lingxi.core.identity.stock_token_source import ADOPTABLE

logger = logging.getLogger(__name__)

#: 这些符号的实现已经搬到相邻模块，旧的 import 路径继续可用。
__all__ = [
    "FIRST_ONBOARDING_REASON",
    "KEY_COMPLETED",
    "KEY_DELEGATED_SUBJECT",
    "KEY_INNERTEST_NOT_OPEN",
    "KEY_INTERNAL_ERROR",
    "KEY_NOT_AUTHORIZED",
    "KEY_STALLED",
    "KEY_SUSPENDED",
    "KEY_SYNCING",
    "KEY_SYNC_TIMEOUT",
    "STATE_ACTIVE",
    "STATE_MCP_SYNCING",
    "STATE_PROVISIONING",
    "AutoOnboardingRunner",
    "EnvironmentResult",
    "OnboardingActions",
    "OnboardingPolicy",
    "OnboardingRecords",
    "OnboardingRuntime",
    "OnboardingSources",
    "_KEYS_REQUIRING_REFERENCE",
    "draft_from_member",
    "roster_row_for",
]


class AutoOnboardingRunner(OnboardingSteps):
    """正式的开通编排：在 :class:`OnboardingSteps` 之上编排次序与终态收口。"""

    name = "首次开通编排"

    # ------------------------------------------------------------------
    # OnboardingRunner 合同
    # ------------------------------------------------------------------

    def start(
        self,
        *,
        event_id: str,
        open_id: str,
        trace_id: str,
        claim_token: Any = None,
    ) -> OnboardingResult:
        """认领并交给执行器。**永远不在调用线程上跑链**（见模块文档「共用线程复核」）。"""

        if self._should_stop():
            # 停机中不再开新链：一条刚起头就被进程退出打断的开通比压根没开始的更难
            # 收拾（外部副作用已经产生、账本却还没写）。
            self._audit.record(
                "onboarding.start_declined_while_stopping",
                event_id=event_id,
                trace_id=trace_id,
            )
            self._notify_admin_of_failure(reason="stopping", event_id=event_id, trace_id=trace_id)
            return _internal("stopping").as_result(trace_id=trace_id)

        with self._lock:
            running_for = self._running.get(open_id)
            if running_for is not None:
                # 同一个人已经有一条链在跑（认领撞上另一条同人事件）。第二次不再开链，
                # 但这条事件**自己从来没有被执行过**——它的认领必须被释放，不放回去
                # 就再也没人捞得到它（原因码在 ``RETRYABLE_REASONS`` 里）。
                self._audit.record(
                    "onboarding.already_running",
                    event_id=event_id,
                    running_event_id=running_for,
                    trace_id=trace_id,
                )
                return OnboardingResult(
                    state=OnboardingState.STARTED, failure_reason="already_running"
                )
            # **先登记再提交**：反过来会让两次几乎同时的 start 各自提交一条链。
            self._running[open_id] = event_id

        def task() -> None:
            try:
                self._execute(
                    event_id=event_id,
                    open_id=open_id,
                    trace_id=trace_id,
                    claim_token=claim_token,
                )
            finally:
                self._release(open_id, event_id)

        accepted = False
        try:
            accepted = bool(self._submit(task))
        except Exception as error:  # noqa: BLE001 - 提交失败必须撤销登记
            self._audit.record(
                "onboarding.submit_failed",
                event_id=event_id,
                error=type(error).__name__,
                trace_id=trace_id,
            )
        if not accepted:
            self._release(open_id, event_id)
            # 队列满或执行器已停：**不假装接手**，给出明确的内部故障终态。
            self._audit.record(
                "onboarding.rejected_by_executor", event_id=event_id, trace_id=trace_id
            )
            self._notify_admin_of_failure(
                reason="executor_unavailable", event_id=event_id, trace_id=trace_id
            )
            return _internal("executor_unavailable").as_result(trace_id=trace_id)
        return OnboardingResult(state=OnboardingState.STARTED)

    def start_system(
        self,
        *,
        email: str,
        trace_id: str,
        origin: str = ORIGIN_PREPROVISION,
        initiated_by_open_id: str,
        preprovision_grant: Any | None = None,
    ) -> OnboardingResult:
        """系统触发的开通入口（Issue #541 预开通，无入站消息）：按邮箱定位、账本 no-op、全程静默、**同步返回终态**；判定与写入逐字节共用 :meth:`_run`。整段实现、参数形状与立论见 :mod:`lingxi.core.identity.preprovision`。"""

        return run_system_onboarding(
            self,
            email=email,
            trace_id=trace_id,
            origin=origin,
            initiated_by_open_id=initiated_by_open_id,
            preprovision_grant=preprovision_grant,
        )

    def _release(self, open_id: str, event_id: str) -> None:
        with self._lock:
            if self._running.get(open_id) == event_id:
                del self._running[open_id]

    def _notify_admin_of_failure(self, *, reason: str, event_id: str, trace_id: str) -> None:
        """管理员送达（Issue #280 §7.3）的唯一触发点：只在真正走到"承诺过转交
        管理员处理"的终态时调用一次，与用户通知彼此独立——告警回调失败不得
        带走这条链本该有的用户结论。

        独立审查（分支 fix/291-280-user-experience 收尾）：``start()`` 里两条
        **同步**返回 ``INTERNAL_ERROR`` 的分支（``stopping``——停机中拒绝开新链；
        ``executor_unavailable``——提交执行器失败）此前从不调用这个回调，因为它们
        根本不经过 ``_execute``（那里此前是唯一接了这个回调的地方）。用户看到的
        是冻结文案「已转交管理员处理」，管理群却真的什么都没收到——文案承诺与
        实际行为对不上。现在三处（这两条同步分支 + ``_execute`` 自己的
        ``INTERNAL_ERROR`` 分支）共用这一个触发点，不允许再出现第四条漏网路径。

        独立审查 codex P1-3：``_execute`` 的调用点现在同时覆盖
        ``OnboardingState.SYNC_TIMEOUT``——产品合同（``docs/产品合同与外部边界.md``
        「权限同步期间」一节）对十五分钟同步超时的措辞同样是"停止自动等待，
        **转交管理员处理**"，与 ``INTERNAL_ERROR`` 分支承诺的"已转交管理员处理"
        是同一句产品承诺，此前却只有后者真的送达管理群。``reason`` 沿用
        ``_Terminal.reason``（``"mcp_sync_timeout"``），与内部故障的原因码
        （如 ``"directory_unavailable"``）在归一化后的 ``scope`` 里天然可区分，
        管理员据此能分清"这是同步超时在等"还是"这是本侧真的坏了"——**不改变**
        :mod:`lingxi.apps.scheduler.late_readiness_recovery` 的自动恢复语义：
        这条告警只是"让管理群知道"，恢复仍然由该模块的迟到就绪恢复职责独立完成
        （``V-开通-18``），两者不是同一件事，也不互相依赖。
        """

        if self._onboarding_failed is None:
            return
        try:
            self._onboarding_failed(reason, trace_id)
        except Exception as error:  # noqa: BLE001 - 告警是锦上添花，不是链的一部分
            self._audit.record(
                "onboarding.alert_callback_failed",
                event_id=event_id,
                error=type(error).__name__,
                trace_id=trace_id,
            )

    def _record_failure_reason(
        self, *, trace_id: str, failure_reason: str, event_type: str
    ) -> None:
        """把这一次失败终态的原因落库（Issue #337，可选，见
        :class:`~lingxi.core.identity.onboarding_ports.FailureReasonRecorder`
        文档）供 ``/admin trace <追溯号>`` 消费。**最佳努力**：与
        :meth:`_notify_admin_of_failure` 同一条纪律——落库失败不得带走已经决定的
        终态或已经完成的通知/记账，只改记一条自己的失败审计。"""

        if self._failure_reasons is None:
            return
        try:
            self._failure_reasons.record_failure(
                trace_id=trace_id, failure_reason=failure_reason, event_type=event_type
            )
        except Exception as error:  # noqa: BLE001 - 落库失败不得带走已经决定的终态
            self._audit.record(
                "onboarding.failure_reason_record_failed",
                error=type(error).__name__,
                trace_id=trace_id,
            )

    # ------------------------------------------------------------------
    # 执行线程
    # ------------------------------------------------------------------

    def _execute(
        self,
        *,
        event_id: str,
        open_id: str,
        trace_id: str,
        claim_token: Any = None,
        grant: PreprovisionGrant | None = None,
    ) -> _Terminal | None:
        """跑完一条链、通知用户、记账。**异常不外抛**（它跑在执行线程上）。

        返回这一次的终态（停机中止时 ``None``）：``start()`` 那条路不看它；系统触发那条路（Issue #541）要把它同步交回给批量脚本。"""

        # **§7.4 编排层当场收口的挂钩**（Issue #282）：``_run`` 在把这个人推进到
        # ``provisioning``（第 715 行的分水岭）之后才会写 ``stalled["user_id"]``——
        # 见 :meth:`_run` 对应那一行的注释。用一个跨异常边界都能读到的可变容器，而不是
        # 给 ``_Terminal`` 加字段：`_run` 既可能正常返回 ``_Terminal``，也可能让
        # ``OnboardingChainError`` 或未预期异常穿透到下面的 ``except``，两条路径都要能
        # 拿到"这条链到底有没有一个可能被卡住的用户"这件事，而后者完全不经过
        # ``_Terminal``。
        stalled: dict[str, str | None] = {"user_id": None}
        try:
            terminal = self._run(
                event_id=event_id, open_id=open_id, trace_id=trace_id, stalled=stalled, grant=grant
            )
        except _ChainAborted:
            # 停机中止：放回认领，下一轮（或下次启动）从头重跑。整条链的每一步都幂等，
            # 重跑不会重复建档、重复发布或重复通知。
            self._audit.record(
                "onboarding.aborted_while_stopping", event_id=event_id, trace_id=trace_id
            )
            self._release_claim(event_id=event_id, trace_id=trace_id, claim_token=claim_token)
            return
        except OnboardingChainError as error:
            terminal = _internal(error.code)
        except Exception as error:  # noqa: BLE001 - 未预料的失败也必须有用户结论
            self._audit.record(
                "onboarding.chain_failed",
                event_id=event_id,
                error=type(error).__name__,
                trace_id=trace_id,
            )
            # C-6：`logger.exception` 连异常正文一起记，psycopg 唯一键冲突正文带着真实 open_id（违 V-花名册-33）；只记类型名与调用栈帧。本行刻意压成单行，见 tests/test_log_exception_body_leak.py。
            logger.error(
                "首次开通编排未预料的失败 event=%s error=%s\n调用栈（不含异常正文）：\n%s",
                event_id,
                type(error).__name__,
                "".join(traceback.format_tb(error.__traceback__)),
            )
            terminal = _internal(f"unexpected_{type(error).__name__}")

        ledger = (
            NULL_DISPATCH_LEDGER if is_system_trigger(event_id) else self._ledger
        )  # Issue #541：系统触发没有 inbound_event 行，两个账本方法都没有对象
        self._audit.record(
            "onboarding.result",
            event_id=event_id,
            origin=origin_of(event_id),
            state=terminal.state.value,
            failure_reason=terminal.reason,
            content_key=terminal.key,
            trace_id=trace_id,
        )
        if terminal.reason is not None:
            # 失败原因落库（Issue #337）：紧邻上面那条既有审计，只在真的是一次
            # 失败终态（``reason`` 非空，成功完成的 ``_completed()`` 从不设置它）
            # 时落一行——见 :meth:`_record_failure_reason` 与 ``FailureReasonRecorder``
            # 协议文档。
            self._record_failure_reason(
                trace_id=trace_id, failure_reason=terminal.reason, event_type="onboarding.result"
            )
        if terminal.state in (OnboardingState.INTERNAL_ERROR, OnboardingState.SYNC_TIMEOUT):
            # SYNC_TIMEOUT 与 INTERNAL_ERROR 是产品合同里两句独立措辞（`docs/产品
            # 合同与外部边界.md`），但都承诺"转交管理员处理"——两者都必须真的送达
            # 管理群（独立审查 codex P1-3）。reason 沿用各自终态的 terminal.reason，
            # 不折叠成同一个值，管理员据此分得清是哪一类。
            self._notify_admin_of_failure(
                reason=terminal.reason or "unknown", event_id=event_id, trace_id=trace_id
            )
        delivered = self._notify(
            open_id=open_id,
            event_id=event_id,
            key=terminal.key,
            values=_with_reference(terminal.key, terminal.values, trace_id),
            suffix="",
            trace_id=trace_id,
        )
        if delivered:
            # **只有真的送达才当场收口**（外部独立审查 P2-1 修复）：此前的判据是
            # ``delivered or not self._release_for_notify(...)``，其中第二个析取项
            # 覆盖"两轮通知全部失败、放弃"的分支——那种情况下用户**一条终态都没
            # 收到**，却已经把状态收口成 ``aborted``。``aborted`` 不在
            # ``StalledProvisioningDuty`` 的候选判据（``provisioning``/
            # ``mcp_syncing``）里，于是这个人从此**结构上不可能再被 45 分钟兜底
            # 捞到**——唯一还欠他一个结论的通道被自己关掉了。收窄到只在
            # ``delivered`` 为真时收口之后，通知彻底失败的这条链原样留在中途格，
            # 45 分钟后 ``StalledProvisioningDuty`` 会用**独立**的通知出口重新尝试
            # 告诉他，而不是被这里提前判死。
            self._abort_if_stalled(stalled["user_id"], terminal, trace_id=trace_id)
        if delivered or not self._release_for_notify(
            event_id=event_id, trace_id=trace_id, claim_token=claim_token
        ):
            # 送到了，或者已经放回过一次仍然送不到：记账收口。第二种情况留了一条
            # ``failed`` 后缀的审计（见 ``_release_for_notify``），不会无声消失——
            # 但**不再**把它当成"当场收口"的触发条件（见上）。
            try:
                ledger.mark_onboarding_dispatched(event_id=event_id)
            except Exception as error:  # noqa: BLE001 - 记不上账最坏只是被下一轮再捞一次
                self._audit.record(
                    "onboarding.dispatch_record_failed",
                    event_id=event_id,
                    error=type(error).__name__,
                    trace_id=trace_id,
                )
        return terminal

    def _notify(
        self,
        *,
        open_id: str,
        event_id: str,
        key: str,
        values: Mapping[str, object],
        suffix: str,
        trace_id: str,
    ) -> bool:
        """主动私聊一条消息，**返回是否送达**。失败只留响亮审计，不改写终态。
        系统触发（Issue #541 预开通）**不发消息、按送达处理**，见 :func:`~lingxi.core.identity.preprovision.deliver_silently`。

        有限重试而不是一次定生死：一次飞书抖动就让用户永远停在「已收到」，代价与收益完全
        不成比例。重试之间用注入的 ``sleep``，因此纯单测里一秒都不用等。

        ``dedupe_key`` 绑定**事件 + 用途**：同一条首聊事件的重复执行（重新认领、重启后
        重跑）只会让用户看到同一条结论一次；而进度提示与终态是两个用途，各自一个键，
        不会互相去重掉。
        """

        if is_system_trigger(event_id):
            return deliver_silently(key=key, open_id=open_id, users=self._users)
        dedupe_key = f"onboarding:{suffix}{event_id}" if suffix else f"onboarding:{event_id}"
        for attempt in range(1, self._notify_attempts + 1):
            try:
                self._notifier.send(
                    open_id=open_id, key=key, values=dict(values), dedupe_key=dedupe_key
                )
                return True
            except Exception as error:  # noqa: BLE001
                self._audit.record(
                    "onboarding.notify_failed",
                    event_id=event_id,
                    content_key=key,
                    attempt=attempt,
                    error=type(error).__name__,
                    trace_id=trace_id,
                )
                if attempt < self._notify_attempts:
                    self._sleep(float(attempt))
        return False

    def _release_claim(self, *, event_id: str, trace_id: str, claim_token: Any = None) -> None:
        """把认领放回 ``NULL``。停机中止专用，不设次数上限——它每个进程生命周期最多
        发生一次，而且不放回去这条事件就永远没人再看。"""

        try:
            self._ledger.release_onboarding_claim(event_id=event_id, claim_token=claim_token)
        except Exception as error:  # noqa: BLE001
            self._audit.record(
                "onboarding.release_claim_failed",
                event_id=event_id,
                error=type(error).__name__,
                trace_id=trace_id,
            )

    def _release_for_notify(self, *, event_id: str, trace_id: str, claim_token: Any = None) -> bool:
        """通知没送到：把认领放回去，让下一轮重跑整条链。**每条事件只放回一次。**

        返回是否真的放回了。``False`` 表示「这条已经放回过一次、仍然送不到」，调用方据此
        记账收口——本方法在那种情况下留一条 ``failed`` 后缀的审计（审计实现按后缀升到
        ``WARNING``），因此放弃这件事本身不会无声消失。
        """

        with self._lock:
            already = event_id in self._released_for_notify
            if not already:
                self._released_for_notify.add(event_id)
        if already:
            self._audit.record(
                "onboarding.notify_gave_up_failed", event_id=event_id, trace_id=trace_id
            )
            return False
        try:
            self._ledger.release_onboarding_claim(event_id=event_id, claim_token=claim_token)
        except Exception as error:  # noqa: BLE001 - 放不回去只能记账收口
            self._audit.record(
                "onboarding.release_claim_failed",
                event_id=event_id,
                error=type(error).__name__,
                trace_id=trace_id,
            )
            return False
        self._audit.record(
            "onboarding.claim_released_after_notify_failed",
            event_id=event_id,
            trace_id=trace_id,
        )
        return True

    def _abort_if_stalled(self, user_id: str | None, terminal: _Terminal, *, trace_id: str) -> None:
        """本链已经确定失败终态、且早前已经把这个人推进到 ``provisioning``：**当场**
        收口成 ``aborted``，不必等停摆扫描职责的四十五分钟租约（设计「编排层当场收口」，
        Issue #282 §0.2/§2.4 的对称修复）。

        **跳过 ``SYNC_TIMEOUT``**：那一路仍然可能就绪，归属迟到就绪恢复职责
        （``V-开通-18``）继续按自己的节奏等待，本链**绝不能**抢它的活——``provisioning_
        state`` 必须原样留在 ``mcp_syncing``。**跳过 ``COMPLETED``**：那一路已经推进
        到 ``active``，条件更新天然不会命中，跳过只是省一次没有意义的空写。

        ``user_id`` 为 ``None`` 表示这条链在把用户推进到 ``provisioning`` 之前就已经
        失败（身份定位、匹配、建档三步的失败终态）——那些失败已经自然停在
        ``matching``/``guest``，不在 ``_PROVISIONING_IN_FLIGHT`` 里，不需要本方法处理
        （见模块文档「两个洞的共同形状」：卡住的判据不是失败，是失败发生在把用户推进
        到 ``provisioning`` 之后）。

        条件更新本身是幂等且安全的：这个人此刻若已经不在 ``provisioning``/
        ``mcp_syncing``（被停摆扫描先一步收口、被另一条并发链推进到 ``active``、或账号
        已经被停用），这里就是一次 0 行的空写，不会覆盖任何人的真实状态
        （`adapters.postgres_identity.PostgresAppUserStore.abort_stalled_provisioning`
        的 CAS 守卫）。
        """

        if user_id is None or terminal.state in (
            OnboardingState.SYNC_TIMEOUT,
            OnboardingState.COMPLETED,
        ):
            return
        try:
            self._users.abort_stalled_provisioning(
                user_id=user_id,
                expected_states=(STATE_PROVISIONING, STATE_MCP_SYNCING),
                reason=terminal.reason or terminal.key,
            )
        except Exception as error:  # noqa: BLE001 - 收口失败不改写已经决定的终态
            self._audit.record(
                "onboarding.stalled_abort_failed",
                user=user_id,
                error=type(error).__name__,
                trace_id=trace_id,
            )

    # ------------------------------------------------------------------
    # 链
    # ------------------------------------------------------------------

    def _stop_guard(self) -> None:
        """每一步之间问一次停机。**在发起下一个带外部副作用的动作之前**问。"""

        if self._should_stop():
            raise _ChainAborted()

    def _run(
        self,
        *,
        event_id: str,
        open_id: str,
        trace_id: str,
        stalled: dict[str, str | None],
        grant: PreprovisionGrant | None = None,
    ) -> _Terminal:
        """一次开通的固定次序。每一步的失败去向都在这里显式返回。

        ``stalled`` 是调用方（``_execute``）传入的可变容器：一旦这个人被推进到
        ``provisioning``（下面那行 ``advance_provisioning_state(to=STATE_PROVISIONING)``
        之后），就把 ``user_id`` 写进去——从这一刻起，本链任何一种失败终态都可能让他
        停在中途格，调用方需要这个信号才能做「当场收口」（见 :meth:`_abort_if_stalled`）。
        """

        self._stop_guard()
        if not self._innertest_roster_gate(open_id):
            # **内测名单闸（Issue #302 S-N-01），挡在整条链最前面。** Bot-Test
            # 全员可见（G-发现性核查，2026-08-24 编排者 API 回读），任何名单外员工
            # 都能真实私聊触达；因此这里必须早于组织快照读取、在职状态实时回读
            # （会消耗全系统独占的专用授权派生令牌）与任何数据库写入——名单外用户
            # 不建档、不发布权限、零业务状态残留，只留一条审计。
            #
            # **审计只带 event_id/trace_id，不带 open_id（含脱敏形式）**：与本文件
            # 其余每一条 `self._audit.record(...)` 同一条纪律——`redact_identifier()`
            # 的返回值按其自身文档字符串**只能进日志**，不可反查也不可比较，放进
            # 结构化审计字段会让人误以为它能用于关联或去重（`V-花名册-34`，
            # `tests/test_roster_audit_duty.py::RedactedIdentifierUsageTest` 拦着）。
            # 需要还原这个人是谁时，凭 `event_id` 回读 `inbound_event.user_open_id`
            # 即可，不需要在这里重复一份。
            self._audit.record(
                "onboarding.innertest_roster_rejected",
                event_id=event_id,
                trace_id=trace_id,
            )
            return _Terminal(
                OnboardingState.NOT_AUTHORIZED,
                KEY_INNERTEST_NOT_OPEN,
                reason="innertest_roster_rejected",
            )

        located = self._locate(open_id)
        if isinstance(located, _Terminal):
            return located
        member = located

        self._stop_guard()
        matched = self._match(member, trace_id=trace_id)
        if isinstance(matched, _Terminal):
            return matched
        request, aggregate = matched

        # **同邮箱已绑给另一个人**（rc25 S-2a，对抗审查 X-1）：挡在**建档之前**——
        # 早于任何数据库写入、早于令牌签发/采纳（``_issue_token`` 会按邮箱把正式表
        # 存量行里**别人的**密文采纳过来）、早于存量差集导入、也早于任何发布意图。
        # 判据是 ``feishu_open_id``（建档之前本人还没有 ``app_user.id``）；"为什么
        # 不等迁移 0085 的唯一索引在写入时拒绝"见
        # ``core/identity/onboarding_guards.reject_email_bound_to_another_person``。
        # 放在 ``_run`` 的公共段而不是某条入口分支里：这条链会被「收到首聊消息」与
        # Issue #541 的「预开通」（系统触发、无入站消息）共同复用，挂在分支上的闸对
        # 第二条路径等于不存在。
        bound_elsewhere = reject_email_bound_to_another_person(
            open_id,
            request.email,
            bindings=self._email_bindings,
            audit=self._audit,
            trace_id=trace_id,
        )
        if bound_elsewhere is not None:
            return bound_elsewhere

        self._stop_guard()
        provisioned = self._provision(request)
        if isinstance(provisioned, _Terminal):
            return provisioned
        user_id = provisioned

        recheck = self._recheck_still_provisionable(user_id, aggregate=aggregate, trace_id=trace_id)
        if recheck is not None:
            if grant is not None and recheck.state is OnboardingState.COMPLETED:
                # rc25 修复包 F2：已 active 提前收口，名单预授权没走到下面的落库口。
                # 绝不静默扩权，只如实标注给批量清单（语义见 OnboardingResult 文档）。
                return replace(recheck, grant_not_applied=True)
            return recheck

        # 银河翻译只算一次（rc25 S-1），翻译失败在这里 fail-closed（早于令牌与环境）。
        galaxy_map = self._translate_galaxy(user_id, aggregate)
        if isinstance(galaxy_map, _Terminal):
            return galaxy_map

        # 存量差集导入挂在零银河判定**之前**（rc25 S-1，Issue #540）：正式表只读一次，
        # 查找结果同时供 `_issue_token` 采纳令牌。
        self._stop_guard()
        lookup = self._lookup_stock_token(request.email)
        if lookup is not None and lookup.state == ADOPTABLE:
            self._import_legacy_permissions(
                user_id, lookup, aggregate, galaxy_map, open_id=open_id, trace_id=trace_id
            )

        if grant is not None:
            # 预开通那一笔预授权（Issue #541）：与 S-1 差集导入**同一个挂点**，且同样排在
            # 零银河判定**之前**——名单里"银河零权限、靠预授权吃饭"的人，先判零权限就会被
            # 整批拒绝。落库口没装配时整链失败关闭，见该函数文档。
            import_preprovision_grant(
                self._position_grants,
                grant,
                user_id=user_id,
                open_id=open_id,
                now=self._clock(),
                audit=self._audit,
                trace_id=trace_id,
            )

        if not aggregate.granted:
            # 零银河权限：现在才有 `app_user.id`，查一次**本地授权**。放在
            # 令牌签发/用户环境创建**之前**——不为一个最终会被拒绝的人签发
            # 问数 MCP 令牌、写一份带凭据的用户环境。
            rejected = reject_zero_galaxy_without_local_grant(
                user_id,
                aggregate,
                resolve_local_overrides=self._resolve_local_overrides,
                audit=self._audit,
                trace_id=trace_id,
            )
            if rejected is not None:
                return rejected

        self._stop_guard()
        issued = self._issue_token(user_id, lookup)
        self._create_environment(user_id, issued)
        self._users.advance_provisioning_state(user_id, to=STATE_PROVISIONING)
        # **分水岭**（Issue #282 §0.1）：从这一行起，任何失败终态都会让这个人停在
        # ``provisioning``/``mcp_syncing``，不再自然回到 ``matching``。把 ``user_id``
        # 交给调用方的可变容器，供 ``_abort_if_stalled`` 判断「要不要当场收口」——写在
        # 这里而不是更早，是因为更早的失败（身份定位、匹配、建档）本来就停在
        # ``matching``/``guest``，不在 ``_PROVISIONING_IN_FLIGHT`` 里，不需要收口。
        stalled["user_id"] = user_id

        self._stop_guard()
        published = self._publish(
            user_id, request, aggregate, issued, galaxy_map=galaxy_map, trace_id=trace_id
        )
        if isinstance(published, _Terminal):
            return published
        permission_version, permissions = published
        self._users.advance_provisioning_state(user_id, to=STATE_MCP_SYNCING)

        # **合同要求的第二条固定提示**（`V-开通-11`）：权限已经排出去、进入同步等待时，
        # 用户必须被告知"正在同步、最多十五分钟、无需重复开通"，而不是一直停在第一条
        # "正在核对"。它在**阻塞式就绪确认之前**发——那一步最长会等十五分钟，等完再说
        # 就等于没说。用独立的去重键，不与终态互相去重掉。
        self._notify(
            open_id=open_id,
            event_id=event_id,
            key=KEY_SYNCING,
            values={},
            suffix="syncing:",
            trace_id=trace_id,
        )

        self._stop_guard()
        return self._confirm(
            user_id=user_id,
            permission_version=permission_version,
            permissions=permissions,
            trace_id=trace_id,
        )
