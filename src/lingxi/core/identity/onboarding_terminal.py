"""首次开通编排（``onboarding_runner.py``）的内部终态：``_Terminal``/停机与本侧故障两个
异常类/``_with_reference``/两个失败工厂/全部 ``STATE_*``、``KEY_*`` 常量。

从 ``core/identity/onboarding_runner.py`` 纯移动拆出（Trace #358 S-H-1，Issue #350 Gate
G-3 裁定 Option A）：只搬定义，不改任何签名、判据或文档字符串；``AutoOnboardingRunner``
通过 ``from .onboarding_terminal import (...)`` 取回这些名字，因此本模块的公开名字（含
``_KEYS_REQUIRING_REFERENCE``，供 ``tests/test_content_catalog.py`` 按模块属性核对）都会
作为 ``onboarding_runner`` 模块的属性再次可见。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from lingxi.core.conversation.ports import (
    OnboardingMessage,
    OnboardingResult,
    OnboardingState,
)

#: ``app_user.provisioning_state`` 在本链上的推进次序。只前进、不回退（`V-开通-04`）。
STATE_MATCHING = "matching"
STATE_PROVISIONING = "provisioning"
STATE_MCP_SYNCING = "mcp_syncing"
STATE_ACTIVE = "active"

#: 冻结文案的内容目录 key。用常量而不是散落字面量：这几条是产品负责人逐字批准过的终态，
#: 改一个 key 就等于换一条用户可见结论。
KEY_MATCHED = "onboarding.matched"
#: 权限发布已排出、进入同步等待时的**进度**提示。合同「权限同步期间，卡片明确显示
#: 『权限正在同步，预计最多需要十五分钟』，用户无需重复开通」的落点。
KEY_SYNCING = "onboarding.syncing"
KEY_COMPLETED = "onboarding.completed"
KEY_NOT_AUTHORIZED = "onboarding.not_authorized"
KEY_SYNC_TIMEOUT = "onboarding.sync_timeout"
KEY_INTERNAL_ERROR = "onboarding.internal_error"
#: 开通中途停摆收口（``apps/scheduler/stalled_provisioning.py``，Issue #282）专用文案键
#: （Issue #280 裁定 B2-2）。此前该职责复用 ``KEY_INTERNAL_ERROR`` 逐字发送；产品负责人
#: 要求换成专门说明"等待已久、可再发一条消息重试"的措辞，不再套用一般性的内部故障话术。
KEY_STALLED = "onboarding.stalled"
KEY_DELEGATED_SUBJECT = "onboarding.delegated_subject"
KEY_SUSPENDED = "gateway.suspended"
#: 内测名单闸拒绝时的文案键（Issue #302 S-N-01）。名单外的 open_id 在身份定位、
#: 花名册与银河匹配、建档、用户环境与权限发布**发生之前**得到这句结论——不是
#: 「无可用银河权限」（那会误导用户去银河申请一个与本闸无关的权限），也不带
#: 追溯号（这是确定性业务结论，不是需要管理员介入的故障，见
#: ``lingxi.core.identity.innertest_roster_gate`` 模块文档）。
KEY_INNERTEST_NOT_OPEN = "onboarding.innertest_not_open"

#: 需要追溯号占位（``{reference}``）的文案键集合（Issue #280 §7.1）。占位名**不能**叫
#: ``trace_id``——那会命中 ``config/content.py`` 的内容安全正则，在目录加载期就让三个
#: 进程全部起不来（联合设计 §0.1）。集中在一处维护，供 :func:`_with_reference` 与
#: ``core/conversation/pipeline.py`` 各自的同名辅助函数共用同一份判据来源（后者是
#: 不同的渲染入口，各自维护一份字面量集合，靠这份常量的字面值对齐，不做跨模块 import
#: ——两条渲染路径此前就是各自独立的失败关闭桩，见模块文档「共用线程复核」）。
_KEYS_REQUIRING_REFERENCE: frozenset[str] = frozenset({KEY_INTERNAL_ERROR, KEY_SYNC_TIMEOUT})


def _with_reference(
    key: str, values: Mapping[str, object] | Sequence[tuple[str, object]], trace_id: str
) -> dict[str, object]:
    """给需要追溯号的终态文案补上 ``reference`` 占位值。

    只在键属于 :data:`_KEYS_REQUIRING_REFERENCE` 时补；已经带了值就不覆盖（防止
    调用方已经显式传过一次导致 ``ContentRenderError`` 的重复变量错误——虽然当前
    没有任何调用方会这样做，用 ``setdefault`` 而不是无条件覆盖仍然是更安全的形状）。
    """

    merged = dict(values)
    if key in _KEYS_REQUIRING_REFERENCE:
        merged.setdefault("reference", trace_id)
    return merged


class _ChainAborted(Exception):
    """停机信号落在链的中途：**不通知、不记账、把认领放回去**。

    不能当成一次失败终态告诉用户——那会在每次滚动部署时给正在开通的人推一条
    ``LX-ONBOARD-001``，而他其实什么问题都没有，下一轮就会被重新捞起来跑完。
    """


class OnboardingChainError(RuntimeError):
    """链上某一步的**本侧故障**：走 ``LX-ONBOARD-001``，不冒充业务结论。

    ``code`` 只有错误码，从不含身份原值、令牌或外部响应正文——它会进审计与日志。
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class _Terminal:
    """一次开通的内部终态：状态 + 要发的那条文案 + 内部原因码。

    **每一个终态都要通知**，没有"这一路不说话"的分支：一条跑完却不说话的链，与一条被
    烧掉的链在用户那边完全一样。重复推送由绑定事件的去重键挡住，不靠这里少发一次。
    """

    state: OnboardingState
    key: str
    values: tuple[tuple[str, object], ...] = ()
    reason: str | None = None
    #: rc25 修复包 F2：见 ``core/conversation/ports.OnboardingResult.grant_not_applied``。
    #: 刻意不复用 ``reason``——``reason`` 非空在 ``_execute`` 里意味着"失败终态"
    #: （触发失败原因落库与 ``onboarding.result`` 审计的 failure_reason 栏），而这
    #: 一格是**成功终态上的清单标注**，混用会把一次正常收口记成一次失败。
    grant_not_applied: bool = False

    def as_result(self, *, trace_id: str | None = None) -> OnboardingResult:
        """转成 gateway 消费的受控结果。

        ``trace_id`` 只在调用方真的持有它时传（本类的两个调用点都在
        ``AutoOnboardingRunner.start`` 里，那里天然有它）；缺省 ``None`` 保持旧行为
        不变——不是所有 ``_Terminal`` 都对应一次已知的追溯号（例如尚未真正开始跑链
        就被拒绝的场景仍然有 ``trace_id``，这里留 ``None`` 只是防御性缺省，不代表
        生产中真的会用到它）。
        """

        values = self.values
        if trace_id is not None:
            values = tuple(_with_reference(self.key, values, trace_id).items())
        return OnboardingResult(
            state=self.state,
            messages=(OnboardingMessage(self.key, values),),
            failure_reason=self.reason,
            grant_not_applied=self.grant_not_applied,
        )


def _not_authorized(reason: str) -> _Terminal:
    return _Terminal(OnboardingState.NOT_AUTHORIZED, KEY_NOT_AUTHORIZED, reason=reason)


def _internal(reason: str) -> _Terminal:
    return _Terminal(OnboardingState.INTERNAL_ERROR, KEY_INTERNAL_ERROR, reason=reason)
