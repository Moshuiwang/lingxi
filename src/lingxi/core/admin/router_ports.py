"""管理命令面的端口协议与返回形状。

只放形状，不放实现：``core/admin`` 应当能被未来的管理入口独立复用，因此这里刻意不 import
``core/conversation`` 子包，两处结构相同的审计端口各自独立声明。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from lingxi.core.admin.pending_action import PendingAction, PendingActionType
from lingxi.core.admin.registry import AdminRegistryEntry
from lingxi.core.admin.views import AdminEventView, AdminTraceView, AdminUserStatusView


#: import conversation 子包（``core/admin`` 应当能被未来的管理入口——例如触发式管理
#: MCP——单独复用，不与飞书私聊管线耦合），两处签名一致靠约定，不是共享同一个类型。
class AuditSink(Protocol):
    """审计出口。与会话侧的同名端口结构一致，靠约定而不是共享同一个类型。"""

    def record(self, action: str, /, **fields: object) -> None:
        """记一条审计。"""
        ...


class AdminRegistryLookup(Protocol):
    """实时读一次登记表；**不得**在实现里加任何跨调用缓存。"""

    def active_entry(self, *, open_id: str) -> AdminRegistryEntry | None:
        """读这个人当前有效的登记条目；没有则返回 ``None``。"""
        ...


class AdminQueries(Protocol):
    """管理命令面用到的全部只读查询。"""

    def user_status(self, *, identifier: str) -> AdminUserStatusView | None:
        """查一个人的当前开通与权限状态；查无此人返回 ``None``。"""
        ...

    def recent_events(
        self, *, identifier: str | None, window_hours: int, limit: int
    ) -> Sequence[AdminEventView]:
        """查最近关键事件；不给标识时查全局。"""
        ...

    def trace_lookup(self, *, trace_id: str) -> AdminTraceView | None:
        """按追溯号查失败原因、事件时间线与开通状态。

        Returns:
            查无任何入站事件时返回 ``None``，渲染层据此回「查无此追溯号」。
        """
        ...

    def resolve_identifier(self, *, identifier: str) -> str:
        """把一个可能是邮箱的标识反查成外部标识。

        不是邮箱形态、或反查零命中与多命中歧义时**原样返回输入**——不返回空、也不发明
        一套"邮箱查无"的独立错误分支：下游本来就会对一个查不到的输入给出既有的"未找到"
        结论，复用同一个出口比新增一条并行分支更不容易在两条路径之间产生用词分叉。
        """
        ...

    def resolve_metric_name(self, *, metric_token: str) -> str:
        """把一个可能是中文别名的指标标记反查成真正的指标标识。

        不在别名表里时**原样返回输入**，与标识反查同一姿态。别名表允许为空（尚未填内容
        时的合法初始状态），为空时本方法对任何输入都恒等。
        """
        ...

    def resolve_override_id(self, *, open_id: str, company_id: str, metric_name: str) -> str | None:
        """按「人 + 公司 + 指标」反查当前生效的本地覆盖行。

        Returns:
            零命中或多命中都返回 ``None``。多命中时不猜测该收回哪一条方向，也不同时收回
            两条——同一键理论上可能同时有一条生效的授权与一条生效的抑制；调用方据此回复
            「存在多条匹配，请精确指定」，不静默选择任意一条。
        """
        ...


class _PrepareDecision(Protocol):
    """准备阶段的判定结论。"""

    ok: bool
    message: str
    code: str


class _PrepareOutcome(Protocol):
    """准备阶段的完整返回：判定结论 ＋ 建出来的待确认操作。"""

    decision: _PrepareDecision
    pending: PendingAction | None


class PendingActionPreparer(Protocol):
    """待确认操作的准备口：只建操作，不直接改变业务状态。

    收回覆盖这一支复用 ``target_open_id`` 形参承载覆盖行标识——真正的目标用户标识由实现
    按它查表得出，写回待确认操作供卡片展示。可选字段各支按需填，不传的沿用既有调用形状。
    """

    def prepare(
        self,
        *,
        action_type: PendingActionType,
        target_open_id: str,
        initiated_by_open_id: str,
        company_id: str | None = None,
        metric_name: str | None = None,
        reason: str | None = None,
        position_name: str | None = None,
        company_scope: str | None = None,
        origin_card_message_id: str | None = None,
    ) -> _PrepareOutcome:
        """建一条待确认操作；判定不通过时在结论里说明原因。"""
        ...


class _CardDispatchResult(Protocol):
    """卡片发送结果。"""

    delivered: bool


class ConfirmCardSender(Protocol):
    """把已经准备好的待确认操作发到发起管理员本人私聊，作为触发命令那条消息的回复。"""

    def send(
        self,
        *,
        pending: PendingAction,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
    ) -> _CardDispatchResult:
        """发一张确认卡；结果里说明有没有真的送达。"""
        ...


class ManagementCardSender(Protocol):
    """把查询到的用户权限管理卡发到发起管理员本人私聊。

    与确认卡是两个独立端口：管理卡本身不是一次"待确认操作"，发送失败也不影响查询命令
    既有的文本回复这条主路径。
    """

    def send(
        self,
        *,
        status: AdminUserStatusView,
        display_identifier: str,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
    ) -> object:
        """发一张管理卡。"""
        ...


@dataclass(frozen=True)
class AdminRouteOutcome:
    """一次 ``route()`` 调用的结论。

    ``handled=False`` 表示这条消息**不是**一次有效的管理交互（发送者未登记、已撤销，
    或判定本身失败），调用方（gateway 管线）据此原样落回既有业务/专用账号提示流程，
    不产生任何管理面回复。``handled=True`` 时 ``reply_text`` 恒非空——已确认是管理员
    的每一次交互都必须有回话，包括拒绝与未知命令，不能悬空沉默。
    """

    handled: bool
    content_key: str = ""
    content_version: str = "internal"
    reply_text: str = ""


class AdminRouter(Protocol):
    """``core/conversation/pipeline.EventPipeline`` 依赖的最小接口。

    :class:`AdminCommandRouter` 结构上满足它；测试与调用方按这个签名注入假实现，
    不需要 import 具体类。

    会话、话题与消息三个标识都带默认值：只有需要发确认卡片的写命令才用得到它们（卡片必须
    回复触发命令的那条消息），只读命令完全不受影响。
    """

    def route(
        self,
        *,
        open_id: str,
        text: str,
        trace_id: str,
        chat_id: str = "",
        thread_id: str | None = None,
        message_id: str = "",
        origin_card_message_id: str | None = None,
    ) -> AdminRouteOutcome:
        """处理一条可能是管理命令的私聊文本。"""
        ...
