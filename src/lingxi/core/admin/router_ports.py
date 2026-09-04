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
    def record(self, action: str, /, **fields: object) -> None: ...


class AdminRegistryLookup(Protocol):
    """实时读一次登记表；**不得**在实现里加任何跨调用缓存。"""

    def active_entry(self, *, open_id: str) -> AdminRegistryEntry | None: ...


class AdminQueries(Protocol):
    def user_status(self, *, identifier: str) -> AdminUserStatusView | None: ...

    def recent_events(
        self, *, identifier: str | None, window_hours: int, limit: int
    ) -> Sequence[AdminEventView]: ...

    def trace_lookup(self, *, trace_id: str) -> AdminTraceView | None:
        """按追溯号查开通失败原因 + 事件时间线 + 开通状态（Issue #337）。查无
        任何入站事件返回 ``None``——``core/admin/router._render_trace`` 据此回复
        「查无此追溯号」。真实实现见
        ``adapters/admin_registry.PostgresAdminQueries.trace_lookup``。"""
        ...

    def resolve_identifier(self, *, identifier: str) -> str:
        """把一个可能是邮箱的标识反查成 open_id（#439 A 档）。

        ``identifier`` 不是邮箱形态（不含 ``@``），或反查零命中/多命中歧义时，
        **原样返回输入**——不返回 ``None``、不发明一套"邮箱查无"的独立错误分支：
        下游（``user_status``/``prepare()``）本来就会对一个查无此 open_id 的输入
        给出既有的"未找到"结论，让这条路径继续复用同一个出口，比新增一条并行的
        错误分支更不容易在两条路径之间产生用词或行为的分叉。真实实现见
        ``adapters/admin_registry.PostgresAdminQueries.resolve_identifier``。
        """
        ...

    def resolve_metric_name(self, *, metric_token: str) -> str:
        """把一个可能是中文别名的指标 token 反查成真正的指标 ID（#439 A 档）。

        ``metric_token`` 不在别名表里时**原样返回输入**——与 ``resolve_identifier``
        同一姿态（fail-open 到"就当它已经是真正的指标 ID"，交给下游按既有语义处理，
        不新增并行的错误分支）。别名表内容当前允许为空（产品负责人尚未填入内容时的
        合法初始状态，与 ``config/company_function_metric_map.toml`` 同一先例）：
        为空时本方法对任何输入都恒等。真实实现见 ``adapters/admin_registry.
        PostgresAdminQueries.resolve_metric_name``。
        """
        ...

    def resolve_override_id(self, *, open_id: str, company_id: str, metric_name: str) -> str | None:
        """按「open_id + 公司 + 指标」反查当前生效的本地覆盖行 override_id（#439 A
        档，revoke 新参数形状的服务端反查）。

        零命中或多命中（同一用户同一公司同一指标理论上可能同时有一条生效的授权行
        与一条生效的抑制行，见迁移 ``0072`` 的唯一索引按 ``direction`` 再分——见
        ``adapters/postgres_local_permission.py`` 模块文档）都返回 ``None``——多
        命中时不猜测该收回哪一条方向，也不同时收回两条；调用方据此回复"存在多条
        匹配，请改用管理卡撤销按钮或改用 override_id 直接指定"，不静默选择任意一条。
        真实实现见 ``adapters/admin_registry.PostgresAdminQueries.
        resolve_override_id``。"""
        ...


class _PrepareDecision(Protocol):
    ok: bool
    message: str
    code: str


class _PrepareOutcome(Protocol):
    decision: _PrepareDecision
    pending: PendingAction | None


class PendingActionPreparer(Protocol):
    """``prepare_action``（接口设计「八、领域服务接口」）的调用面：只建待确认操作，
    不直接改变业务状态。与
    ``adapters.postgres_pending_action.PostgresPendingActionStore.prepare`` 结构
    相同，测试注入内存假实现。

    ``company_id``/``metric_name``/``reason`` 三个参数（#319 S-P-1b 新增）只有
    ``REVOKE_PERMISSION`` 形状 2 会填——``suspend``/
    ``resume`` 沿用既有调用形状，不传这三个参数。``REVOKE_PERMISSION``（卡 B）
    只传 ``target_open_id``（复用同一形参承载 override_id，不是 open_id——
    真正的目标用户 open_id 由 ``adapters/postgres_pending_action.py`` 的
    ``prepare()`` 按这个 override_id 查表得出，写回 ``PendingAction.
    target_open_id`` 供卡片展示）与 ``reason``，不传 ``company_id``/
    ``metric_name``。"""

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
    ) -> _PrepareOutcome: ...


class _CardDispatchResult(Protocol):
    delivered: bool


class ConfirmCardSender(Protocol):
    """把已经 prepare 好的待确认操作发送到发起管理员本人私聊——作为触发这条命令
    的消息的回复（真实实现见 ``core/admin/card_dispatch.ConfirmCardDispatcher``）。
    """

    def send(
        self,
        *,
        pending: PendingAction,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
    ) -> _CardDispatchResult: ...


class ManagementCardSender(Protocol):
    """把 ``/admin user`` 查到的用户权限管理卡（#439 B 档）发送到发起管理员本人
    私聊——作为触发这条查询命令的消息的回复。与 :class:`ConfirmCardSender` 是两个
    独立端口（真实实现见 ``core/admin/card_dispatch.ManagementCardDispatcher``）：
    管理卡本身不是一个"待确认操作"，发送失败也不影响 ``/admin user`` 既有的文本
    回复这条主路径——见 :meth:`AdminCommandRouter._send_management_card` 文档。
    """

    def send(
        self,
        *,
        status: AdminUserStatusView,
        display_identifier: str,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
    ) -> object: ...


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

    ``chat_id``/``thread_id``/``message_id`` 均带默认值（Issue #96 S-M-02 新增）：
    只有 ``SUSPEND_USER``/``RESUME_USER`` 这类需要发送确认卡片的写命令才用得到
    它们（卡片必须回复触发命令的那条消息），只读命令（``help``/``user``/``audit``）
    完全不受影响。默认值保证既有调用点（不传这三个参数）行为逐字节不变。
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
    ) -> AdminRouteOutcome: ...
