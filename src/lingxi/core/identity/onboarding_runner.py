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
from lingxi.core.identity.onboarding_steps import FIRST_ONBOARDING_REASON, OnboardingSteps
from lingxi.core.identity.onboarding_support import draft_from_member, roster_row_for
from lingxi.core.identity.onboarding_terminal import (
    _KEYS_REQUIRING_REFERENCE,
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
    _ChainAbortedError,
    _internal,
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
        """认领并交给执行器。**永远不在调用线程上跑链。**

        Returns:
            恒为"已接手"——除非停机中、同一个人已经有链在跑、或执行器拒绝，那三种情况
            各自给出明确结论，不假装接手。
        """
        if self._should_stop():
            # 停机中不再开新链：一条刚起头就被进程退出打断的开通，比压根没开始的更难
            # 收拾——外部副作用已经产生、账本却还没写。
            self._audit.record(
                "onboarding.start_declined_while_stopping",
                event_id=event_id,
                trace_id=trace_id,
            )
            self._notify_admin_of_failure(reason="stopping", event_id=event_id, trace_id=trace_id)
            return _internal("stopping").as_result(trace_id=trace_id)

        already_running = self._claim_open_id(open_id, event_id=event_id, trace_id=trace_id)
        if already_running is not None:
            return already_running
        return self._submit_chain(
            event_id=event_id, open_id=open_id, trace_id=trace_id, claim_token=claim_token
        )

    def _claim_open_id(
        self, open_id: str, *, event_id: str, trace_id: str
    ) -> OnboardingResult | None:
        """登记"这个人正在跑一条链"；已经有一条时返回结论、不开第二条。

        **先登记再提交**：反过来会让两次几乎同时的 ``start`` 各自提交一条链。撞上同人
        事件时这条事件**自己从来没有被执行过**，它的认领必须被释放，否则再也没人捞得到
        它——原因码落在可重试集合里正是为此。

        Returns:
            已经有链在跑时的结论；可以开链时返回 ``None``。
        """
        with self._lock:
            running_for = self._running.get(open_id)
            if running_for is None:
                self._running[open_id] = event_id
                return None
        self._audit.record(
            "onboarding.already_running",
            event_id=event_id,
            running_event_id=running_for,
            trace_id=trace_id,
        )
        return OnboardingResult(state=OnboardingState.STARTED, failure_reason="already_running")

    def _submit_chain(
        self, *, event_id: str, open_id: str, trace_id: str, claim_token: Any
    ) -> OnboardingResult:
        """把整条链投给执行器；队列满或执行器已停时**不假装接手**。"""

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
        except Exception as error:
            # 提交失败必须撤销登记，否则这个人会被"已有链在跑"永久挡住。
            self._audit.record(
                "onboarding.submit_failed",
                event_id=event_id,
                error=type(error).__name__,
                trace_id=trace_id,
            )
        if accepted:
            return OnboardingResult(state=OnboardingState.STARTED)
        self._release(open_id, event_id)
        self._audit.record("onboarding.rejected_by_executor", event_id=event_id, trace_id=trace_id)
        self._notify_admin_of_failure(
            reason="executor_unavailable", event_id=event_id, trace_id=trace_id
        )
        return _internal("executor_unavailable").as_result(trace_id=trace_id)

    def start_system(
        self,
        *,
        email: str,
        trace_id: str,
        origin: str = ORIGIN_PREPROVISION,
        initiated_by_open_id: str,
        preprovision_grant: Any | None = None,
    ) -> OnboardingResult:
        """系统触发的开通入口（预开通，没有入站消息）。

        按邮箱定位、账本空操作、全程静默、**同步返回终态**；判定与写入逐字节共用
        :meth:`_run`。整段实现与参数形状见 :mod:`lingxi.core.identity.preprovision`。
        """
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
        """承诺过「转交管理员处理」的终态，唯一的告警触发点，每条链最多一次。

        与用户通知彼此独立：告警回调失败不得带走这条链本该有的用户结论。三条路径共用它，不允许
        出现第四条漏网的：停机中拒绝开新链、提交执行器失败，以及执行线程自己算出的失败终态。
        前两条是同步返回，此前根本不经过执行线程，于是用户看到「已转交管理员处理」而管理群什么
        都没收到。同步超时也走这里，原因码沿用各自终态的取值，管理员据此能分清「这是同步在等」
        还是「这是本侧真的坏了」；自动恢复仍由迟到就绪恢复职责独立完成，两者不互相依赖。
        """
        if self._onboarding_failed is None:
            return
        try:
            self._onboarding_failed(reason, trace_id)
        except Exception as error:
            self._audit.record(
                "onboarding.alert_callback_failed",
                event_id=event_id,
                error=type(error).__name__,
                trace_id=trace_id,
            )

    def _record_failure_reason(
        self, *, trace_id: str, failure_reason: str, event_type: str
    ) -> None:
        """把这一次失败终态的原因落库，供按追溯号查询消费。

        **最佳努力**：落库失败不得带走已经决定的终态或已经完成的通知与记账，只改记一条
        自己的失败审计。
        """
        if self._failure_reasons is None:
            return
        try:
            self._failure_reasons.record_failure(
                trace_id=trace_id, failure_reason=failure_reason, event_type=event_type
            )
        except Exception as error:
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

        Returns:
            这一次的终态；停机中止时 ``None``。``start()`` 那条路不看它，系统触发那条路
            要把它同步交回给批量脚本。
        """
        # 分水岭之后的"当场收口"挂钩：``_run`` 把这个人推进到 ``provisioning`` 之后才写
        # ``stalled["user_id"]``。用一个跨异常边界都能读到的可变容器，而不是给终态对象加
        # 字段——``_run`` 既可能正常返回终态，也可能让异常穿透到下面，而后者完全不经过
        # 终态对象，两条路径都要能拿到"这条链有没有一个可能被卡住的用户"。
        stalled: dict[str, str | None] = {"user_id": None}
        try:
            terminal = self._run(
                event_id=event_id, open_id=open_id, trace_id=trace_id, stalled=stalled, grant=grant
            )
        except _ChainAbortedError:
            # 停机中止：放回认领，下一轮（或下次启动）从头重跑。整条链的每一步都幂等，
            # 重跑不会重复建档、重复发布或重复通知。
            self._audit.record(
                "onboarding.aborted_while_stopping", event_id=event_id, trace_id=trace_id
            )
            self._release_claim(event_id=event_id, trace_id=trace_id, claim_token=claim_token)
            return None
        except OnboardingChainError as error:
            terminal = _internal(error.code)
        except Exception as error:
            terminal = self._terminal_for_unexpected(error, event_id=event_id, trace_id=trace_id)

        self._report_terminal(terminal, event_id=event_id, trace_id=trace_id)
        self._settle(
            terminal,
            event_id=event_id,
            open_id=open_id,
            trace_id=trace_id,
            claim_token=claim_token,
            stalled_user_id=stalled["user_id"],
        )
        return terminal

    def _terminal_for_unexpected(
        self, error: Exception, *, event_id: str, trace_id: str
    ) -> _Terminal:
        """未预料的失败也必须有用户结论：换成一个带类型名的内部故障终态。

        只记异常**类型名与调用栈帧**，不记异常正文：驱动的唯一键冲突正文会原样带着真实
        外部标识，写进日志就抵触"日志不含外部标识原值"这条纪律。
        """
        self._audit.record(
            "onboarding.chain_failed",
            event_id=event_id,
            error=type(error).__name__,
            trace_id=trace_id,
        )
        logger.error(
            "首次开通编排未预料的失败 event=%s error=%s\n调用栈（不含异常正文）：\n%s",
            event_id,
            type(error).__name__,
            "".join(traceback.format_tb(error.__traceback__)),
        )
        return _internal(f"unexpected_{type(error).__name__}")

    def _report_terminal(self, terminal: _Terminal, *, event_id: str, trace_id: str) -> None:
        """把终态记进审计、落一行失败原因、必要时送一条管理员告警。

        承诺过"转交管理员处理"的两种终态（本侧故障与同步超时）都必须真的送达管理群：
        它们在产品合同里是两句独立措辞，但承诺的是同一件事。原因码沿用各自终态的取值，
        不折叠成同一个值，管理员据此分得清是"同步还在等"还是"本侧真的坏了"。
        """
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
            # 只在真的是一次失败终态时落一行——成功完成的终态从不设置原因。
            self._record_failure_reason(
                trace_id=trace_id, failure_reason=terminal.reason, event_type="onboarding.result"
            )
        if terminal.state in (OnboardingState.INTERNAL_ERROR, OnboardingState.SYNC_TIMEOUT):
            self._notify_admin_of_failure(
                reason=terminal.reason or "unknown", event_id=event_id, trace_id=trace_id
            )

    def _settle(
        self,
        terminal: _Terminal,
        *,
        event_id: str,
        open_id: str,
        trace_id: str,
        claim_token: Any,
        stalled_user_id: str | None,
    ) -> None:
        """把终态告诉用户，然后决定要不要当场收口、要不要记账。

        **只有真的送达才当场收口**：两轮通知全部失败时用户一条终态都没收到，把状态收成
        "已中止"会让这个人从此结构上不可能再被四十五分钟兜底捞到——唯一还欠他一个结论的
        通道被自己关掉了。收窄之后，通知彻底失败的链原样留在中途格，兜底职责会用**独立**
        的通知出口重新尝试。

        送到了、或者已经放回过一次仍然送不到，都记账收口；后者留了一条 ``failed`` 后缀的
        审计，不会无声消失。
        """
        # 系统触发没有入站事件行，两个账本方法都没有对象。
        ledger = NULL_DISPATCH_LEDGER if is_system_trigger(event_id) else self._ledger
        delivered = self._notify(
            open_id=open_id,
            event_id=event_id,
            key=terminal.key,
            values=_with_reference(terminal.key, terminal.values, trace_id),
            suffix="",
            trace_id=trace_id,
        )
        if delivered:
            self._abort_if_stalled(stalled_user_id, terminal, trace_id=trace_id)
        if delivered or not self._release_for_notify(
            event_id=event_id, trace_id=trace_id, claim_token=claim_token
        ):
            try:
                ledger.mark_onboarding_dispatched(event_id=event_id)
            except Exception as error:
                # 记不上账最坏只是被下一轮再捞一次。
                self._audit.record(
                    "onboarding.dispatch_record_failed",
                    event_id=event_id,
                    error=type(error).__name__,
                    trace_id=trace_id,
                )

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
        """主动私聊一条消息，返回是否送达。失败只留响亮审计，不改写终态。

        系统触发（预开通，没有入站消息）**不发消息、按送达处理**。有限重试而不是一次定生死：
        一次平台抖动就让用户永远停在「已收到」，代价与收益完全不成比例；重试之间用注入的等待，
        因此纯单测里一秒都不用等。

        去重键绑定**事件 ＋ 用途**：同一条首聊事件的重复执行只会让用户看到同一条结论一次；而
        进度提示与终态是两个用途，各自一个键，不会互相去重掉。
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
            except Exception as error:
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
        """把认领放回未认领状态。

        停机中止专用，不设次数上限——它每个进程生命周期最多发生一次，而且不放回去这条
        事件就永远没人再看。
        """
        try:
            self._ledger.release_onboarding_claim(event_id=event_id, claim_token=claim_token)
        except Exception as error:
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
        except Exception as error:
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
        """已经确定失败终态、且这个人早前被推进到「开通中」时，**当场**收口，不必等四十五分钟租约。

        **跳过同步超时**：那一路仍然可能就绪，归属迟到就绪恢复职责继续按自己的节奏等待，本链
        **绝不能**抢它的活。**跳过已完成**：那一路已经推进到 ``active``，条件更新天然不会命中。
        用户标识为空表示这条链在推进之前就失败了（定位、匹配、建档），那些失败本来就停在更前面
        的格子，不需要本方法处理。

        条件更新本身幂等且安全：这个人此刻若已经不在中途格（被停摆扫描先一步收口、被另一条并发
        链推进到 ``active``、或账号已被停用），这里就是一次 0 行的空写。
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
        except Exception as error:
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
            raise _ChainAbortedError()

    def _run(
        self,
        *,
        event_id: str,
        open_id: str,
        trace_id: str,
        stalled: dict[str, str | None],
        grant: PreprovisionGrant | None = None,
    ) -> _Terminal:
        """一次开通的固定次序。每一段的失败去向都在那一段里显式返回。

        ``stalled`` 是调用方传入的可变容器：这个人一旦被推进到「开通中」就把用户标识写进去。从
        那一刻起本链任何失败终态都可能让他停在中途格，调用方需要这个信号才能决定要不要当场收口。
        ``grant`` 是预开通带来的预授权，首聊路径恒为 ``None``。
        """
        self._stop_guard()
        rejected = self._gate_innertest(open_id, event_id=event_id, trace_id=trace_id)
        if rejected is not None:
            return rejected

        identified = self._identify(open_id, trace_id=trace_id)
        if isinstance(identified, _Terminal):
            return identified
        request, aggregate = identified

        prepared = self._prepare_account(
            request, aggregate, open_id=open_id, trace_id=trace_id, grant=grant
        )
        if isinstance(prepared, _Terminal):
            return prepared
        user_id, galaxy_map, lookup = prepared

        return self._grant_access(
            user_id,
            request,
            aggregate,
            galaxy_map=galaxy_map,
            lookup=lookup,
            open_id=open_id,
            event_id=event_id,
            trace_id=trace_id,
            stalled=stalled,
        )

    def _gate_innertest(self, open_id: str, *, event_id: str, trace_id: str) -> _Terminal | None:
        """内测名单闸，挡在整条链最前面。

        必须早于组织快照读取、在职状态实时回读（会消耗全系统独占的派生令牌）与任何数据库
        写入：名单外用户不建档、不发布权限、零业务状态残留，只留一条审计。机器人对全员
        可见，名单外的任何真实员工都能私聊触达，因此这道闸不能往后放。

        审计**只带事件标识与追溯号，不带外部标识**（含脱敏形式）：脱敏值按其自身文档只能
        进日志，不可反查也不可比较，放进结构化审计字段会让人误以为它能用于关联或去重。
        需要还原这个人是谁时，凭事件标识回读入站事件表即可。
        """
        if self._innertest_roster_gate(open_id):
            return None
        self._audit.record(
            "onboarding.innertest_roster_rejected", event_id=event_id, trace_id=trace_id
        )
        return _Terminal(
            OnboardingState.NOT_AUTHORIZED,
            KEY_INNERTEST_NOT_OPEN,
            reason="innertest_roster_rejected",
        )

    def _identify(self, open_id: str, *, trace_id: str) -> _Terminal | tuple[Any, Any]:
        """身份定位 → 银河匹配 → 同邮箱是否已绑给另一个人。

        同邮箱那道闸挡在**建档之前**——早于任何数据库写入、早于令牌签发与采纳（采纳会按邮箱把
        正式表里**别人的**密文拿过来）、早于存量差集导入、也早于任何发布意图。放在公共段而不是
        某条入口分支里：这条链会被「收到首聊消息」与「预开通」共同复用，挂在分支上的闸对第二条
        路径等于不存在。

        Returns:
            失败时是终态；成功时是 ``(建档请求, 权限聚合)``。
        """
        located = self._locate(open_id)
        if isinstance(located, _Terminal):
            return located

        self._stop_guard()
        matched = self._match(located, trace_id=trace_id)
        if isinstance(matched, _Terminal):
            return matched
        request, aggregate = matched

        bound_elsewhere = reject_email_bound_to_another_person(
            open_id,
            request.email,
            bindings=self._email_bindings,
            audit=self._audit,
            trace_id=trace_id,
        )
        if bound_elsewhere is not None:
            return bound_elsewhere
        return request, aggregate

    def _prepare_account(
        self,
        request: Any,
        aggregate: Any,
        *,
        open_id: str,
        trace_id: str,
        grant: PreprovisionGrant | None,
    ) -> _Terminal | tuple[str, Any, Any]:
        """建档、复核、翻译银河，并把存量差集与预授权导入进来。

        两处导入都挂在**零银河判定之前**：名单里"银河零权限、靠预授权吃饭"的人，先判零
        权限就会被整批拒绝。银河翻译也算在前面并在失败时立刻关闭——早于令牌签发与用户
        环境创建，不为一个最终会被拒绝的人签发问数令牌、写一份带凭据的用户环境。

        Returns:
            失败时是终态；成功时是 ``(用户标识, 银河翻译结果, 存量令牌查找结果)``。
        """
        self._stop_guard()
        provisioned = self._provision(request)
        if isinstance(provisioned, _Terminal):
            return provisioned
        user_id = provisioned

        recheck = self._recheck_still_provisionable(user_id, aggregate=aggregate, trace_id=trace_id)
        if recheck is not None:
            if grant is not None and recheck.state is OnboardingState.COMPLETED:
                # 已经开通完成的人提前收口，名单预授权没走到落库口。绝不静默扩权，
                # 只如实标注给批量清单。
                return replace(recheck, grant_not_applied=True)
            return recheck

        galaxy_map = self._translate_galaxy(user_id, aggregate)
        if isinstance(galaxy_map, _Terminal):
            return galaxy_map

        self._stop_guard()
        lookup = self._import_existing_grants(
            request,
            aggregate,
            galaxy_map,
            user_id=user_id,
            open_id=open_id,
            trace_id=trace_id,
            grant=grant,
        )
        if not aggregate.granted:
            # 零银河权限：现在才有内部用户标识，查一次本地授权。
            rejected = reject_zero_galaxy_without_local_grant(
                user_id,
                aggregate,
                resolve_local_overrides=self._resolve_local_overrides,
                audit=self._audit,
                trace_id=trace_id,
            )
            if rejected is not None:
                return rejected
        return user_id, galaxy_map, lookup

    def _import_existing_grants(
        self,
        request: Any,
        aggregate: Any,
        galaxy_map: Any,
        *,
        user_id: str,
        open_id: str,
        trace_id: str,
        grant: PreprovisionGrant | None,
    ) -> Any:
        """把存量差集与预开通预授权导进本地覆盖表。

        正式表只读一次，查找结果同时供令牌采纳复用，因此把它返回给调用方。

        Returns:
            存量令牌的查找结果；没有存量源时是 ``None``。
        """
        lookup = self._lookup_stock_token(request.email)
        if lookup is not None and lookup.state == ADOPTABLE:
            self._import_legacy_permissions(
                user_id, lookup, aggregate, galaxy_map, open_id=open_id, trace_id=trace_id
            )
        if grant is not None:
            import_preprovision_grant(
                self._position_grants,
                grant,
                user_id=user_id,
                open_id=open_id,
                now=self._clock(),
                audit=self._audit,
                trace_id=trace_id,
            )
        return lookup

    def _grant_access(
        self,
        user_id: str,
        request: Any,
        aggregate: Any,
        *,
        galaxy_map: Any,
        lookup: Any,
        open_id: str,
        event_id: str,
        trace_id: str,
        stalled: dict[str, str | None],
    ) -> _Terminal:
        """签令牌、建环境、排发布意图、告知同步中，最后等就绪确认。

        推进到"开通中"那一行是**分水岭**：从它起，任何失败终态都会让这个人停在中途格、
        不再自然回到"匹配中"，因此把用户标识交给调用方的可变容器就写在那里——更早的失败
        （定位、匹配、建档）本来就停在更前面的格子，不需要当场收口。
        """
        self._stop_guard()
        issued = self._issue_token(user_id, lookup)
        self._create_environment(user_id, issued)
        self._users.advance_provisioning_state(user_id, to=STATE_PROVISIONING)
        stalled["user_id"] = user_id

        self._stop_guard()
        published = self._publish(
            user_id, request, aggregate, issued, galaxy_map=galaxy_map, trace_id=trace_id
        )
        if isinstance(published, _Terminal):
            return published
        permission_version, permissions = published
        self._users.advance_provisioning_state(user_id, to=STATE_MCP_SYNCING)
        self._notify_syncing(open_id=open_id, event_id=event_id, trace_id=trace_id)

        self._stop_guard()
        return self._confirm(
            user_id=user_id,
            permission_version=permission_version,
            permissions=permissions,
            trace_id=trace_id,
        )

    def _notify_syncing(self, *, open_id: str, event_id: str, trace_id: str) -> None:
        """**合同要求的第二条固定提示**（`V-开通-11`）：权限已经排出去、进入同步等待时，

        用户必须被告知"正在同步、最多十五分钟、无需重复开通"，而不是一直停在第一条
        「正在核对」。它发在**阻塞式就绪确认之前**——那一步最长会等十五分钟，等完再说就
        等于没说；用独立的去重键，不与终态互相去重掉。
        """
        self._notify(
            open_id=open_id,
            event_id=event_id,
            key=KEY_SYNCING,
            values={},
            suffix="syncing:",
            trace_id=trace_id,
        )
