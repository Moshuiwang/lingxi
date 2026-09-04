"""``card_callback.py`` 依赖的 ``Protocol`` 端口声明与几个跨方法共用的小工具。

只声明调用方需要的最小接口（代码框架第二节），不 import 任何具体实现或
``adapters/``。真实装配、测试假实现均见 ``card_callback.py`` 模块文档与
``apps/gateway/__init__.py``。搬到独立模块只是为了控制 ``card_callback.py``
的文件体量，不改变任何一个端口的语义边界；``AdminCardCallbackHandler`` 从
这里 re-export 全部符号，外部既有导入路径不变。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from lingxi.core.admin.card_dispatch import ManagementCardContext
from lingxi.core.admin.pending_action import PendingAction, PendingActionStatus
from lingxi.core.admin.views import AdminUserStatusView


class _Decision(Protocol):
    """``ConfirmDecision``/``CancelDecision`` 的公共结构面。

    本类只用得到这四个字段/属性，用结构类型而不是 import 两个具体
    dataclass，避免多引入一层耦合。
    """

    ok: bool
    message: str
    terminal_status: PendingActionStatus | None


class _Outcome(Protocol):
    """confirm()/cancel() 的返回形状：这次判定结论 + 对应的 pending action。"""

    decision: _Decision
    pending: PendingAction | None


class PendingActionDecider(Protocol):
    """confirm()/cancel() 两个真正改变状态的调用面，外加 CardKit sequence 记账。

    与 ``adapters.postgres_pending_action.PostgresPendingActionStore`` 结构
    相同，测试注入内存假实现。真实实现在审计写入失败时抛出
    :class:`~lingxi.core.admin.pending_action.PendingActionAuditWriteFailedError`。
    """

    def confirm(self, *, pending_action_id: str, clicker_open_id: str) -> _Outcome:
        """点击「确认」：真正改变 pending action 状态，返回这次判定的结论。"""

    def cancel(self, *, pending_action_id: str, clicker_open_id: str) -> _Outcome:
        """点击「取消」：真正改变 pending action 状态，返回这次判定的结论。"""

    def next_card_sequence(self, *, pending_action_id: str) -> int:
        """取下一个可用于 CardKit 更新调用的严格递增序号。"""


class AuditSink(Protocol):
    """卡片回调的审计记账口。

    与 ``core/admin/router.AuditSink``/``adapters/postgres_pending_action.
    AuditSink`` 结构相同的独立 Protocol——三处不互相 import，见模块与那两处
    各自的文档。
    """

    def record(self, action: str, /, **fields: object) -> None:
        """记一条审计事件。"""


class _ManagementRouteOutcome(Protocol):
    """``AdminRouteOutcome`` 的公共结构面。

    用结构类型而不是 import 具体 dataclass，避免 card_callback.py 与
    router.py 之间多一层强耦合——两者本来就已经通过
    ``PendingActionPreparer``/``ConfirmCardSender`` 两个独立端口分别与
    ``AdminCommandRouter`` 打交道，这里延续同一分工。
    """

    handled: bool
    content_key: str
    reply_text: str


class ManagementActionRouter(Protocol):
    """管理卡表单/按钮回调 → 等价 ``/admin ...`` 命令文本 → 既有路由调用面。

    与 ``PendingActionDecider``/``AdminCardTransport`` 两个既有端口是同一个
    类（真实装配时都指向同一个 ``AdminCommandRouter`` 实例），但作为独立
    声明的 Protocol——本类只用得到 ``route()`` 这一个方法。真正的写路径判定
    （角色核对、自我目标防呆、prepare()、确认卡发送、审计）全部发生在
    ``route()`` 内部，本类新增的两个方法只负责"把管理卡的交互翻译成一条
    等价命令文本"，不重新实现任何一步既有判定。
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
    ) -> _ManagementRouteOutcome:
        """把一条等价命令文本交给既有 ``AdminCommandRouter`` 的写路径判定。"""


class ManagementCardContextReader(Protocol):
    """管理卡持久上下文的读写端口。"""

    def lookup_context(self, *, message_id: str) -> ManagementCardContext | None:
        """按管理卡的消息 ID 读取它的持久上下文；找不到返回 ``None``。"""

    def update_state(
        self,
        *,
        message_id: str,
        state: str | None = None,
        dispatch_status: str | None = None,
        snapshot_fingerprint: str | None = None,
        last_trace_id: str | None = None,
    ) -> ManagementCardContext | None:
        """更新管理卡持久上下文的部分字段，返回更新后的最新快照。"""


class PostCallbackExecutor(Protocol):
    """回调应答之后才执行的那批后处理的执行口。

    ``submit`` 返回 ``True`` 表示"我接住了，会执行"；返回 ``False`` 表示
    "没接住"（例如队列已满），调用方据此原地同步执行——见
    :meth:`AdminCardCallbackHandler._run_after_response`。实现必须**立即
    返回**，不得在调用线程里执行任务，否则这个端口就没有意义。
    """

    def submit(self, task: Callable[[], None]) -> bool:
        """接住一个后处理任务并安排异步执行；没接住时返回 ``False``。"""


class ManagementCardRefresher(Protocol):
    """在原管理卡实体上按持久 sequence 更新最新状态。

    并发保护只用 ``expected_card_sequence`` 一把 CAS：每一次 ``state_version``
    递增都与 ``card_sequence`` 递增写在同一条 UPDATE 里，而 ``card_sequence``
    还会被 ``next_card_sequence()`` 单独推进，因此 ``card_sequence`` 的判别力
    严格覆盖 ``state_version``，多一把 CAS 只是多一处要维护的等价条件。
    ``state_version`` 列本身保留（生产 drop column 不可逆）。
    """

    def update(
        self,
        *,
        context: ManagementCardContext,
        status: AdminUserStatusView,
        state: str,
        dispatch_status: str | None = None,
        status_message: str | None = None,
        expected_card_sequence: int | None = None,
    ) -> bool | None:
        """按 CAS 条件把原管理卡刷新成传入的最新状态。"""


@dataclass(frozen=True)
class _ManagementContextCheck:
    """一次管理卡懒快照校验的结论：上下文/最新状态，以及是否已失效。"""

    context: ManagementCardContext | None
    status: AdminUserStatusView | None
    stale: bool = False
    forbidden: bool = False


#: 管理卡撤销按钮没有独立的原因输入框——一键撤销，服务端补一个固定原因供
#: 审计与确认卡回显，取值与按钮标签「撤销」保持术语统一（不写「收回」）。
#: **已落库的旧值一律不回改**：这段文本进 ``pending_action.payload`` 的
#: ``reason`` 键，渲染侧只做原样展示，全仓没有任何一处按字面量比较或解析
#: 它——审计文本是历史事实，不篡改"当时管理员看到的是什么"。
_MANAGEMENT_CARD_REVOKE_REASON = "管理卡逐行撤销"
_MANAGEMENT_CARD_GROUP_REVOKE_REASON = "管理卡撤销职位范围授权"

#: 表单提交成功创建待确认操作时 ``AdminRouteOutcome.content_key`` 的取值——与
#: ``router.py`` 里 ``_dispatch_write_action`` 最终成功分支写死的字面量一致，
#: 是本模块判断"这次提交要不要提示成功 toast"的唯一依据（见 ``router.py`` 该
#: 分支的 ``content_key="admin.write_action_pending"``）。
_WRITE_ACTION_PENDING_CONTENT_KEY = "admin.write_action_pending"


def _toast_error(content: str) -> dict[str, Any]:
    """错误类应答的公共形状：只有 toast，不带 ``card``。

    这三类分支都不产生可以安全展示给这次点击者的卡片，要么点击者不是发起
    人，要么这次点击对应的动作根本不存在或没有留下可信状态。
    """
    return {"toast": {"type": "error", "content": content}}


def _toast_from_route_outcome(outcome: _ManagementRouteOutcome) -> dict[str, Any]:
    """把 ``AdminCommandRouter.route()`` 的结论翻译成管理卡交互的 toast 应答。

    表单/撤销按钮的提交结果先以 toast 返回；原管理卡的不可操作状态由出带外
    持久化更新负责，见 ``core/admin/management_card.py`` 与 gateway 装配。
    """
    if not outcome.handled:
        # 结构上只会发生在"点击这一刻当前角色恰好已被撤销"——route() 内部会重新
        # 判定一次身份，不假设管理卡的历史交互权限仍然有效。
        return _toast_error("当前身份已无权限执行该操作，请重新查询 /admin user")
    toast_type = "success" if outcome.content_key == _WRITE_ACTION_PENDING_CONTENT_KEY else "error"
    return {"toast": {"type": toast_type, "content": outcome.reply_text}}


class PermissionRecomputeTrigger(Protocol):
    """确认执行成功后，对目标用户即时触发一次定向权限重算/发布的端口。

    真实实现见 ``adapters/postgres_permission_recompute_trigger.
    PermissionRecomputeAdapter``；它自己决定"这个 ``PendingAction`` 该按哪种
    方式重算"，``card_callback.py`` 不需要、也不应该知道这些细节——本类只
    负责"确认执行成功了，在恰当的时刻调用它一次，失败了就记审计"，业务规则
    全部留在权限模块（代码框架第二节：``core/admin`` 与 ``core/permission``
    是两个平级领域模块，互不下沉对方的规则）。
    """

    def trigger(self, pending: PendingAction) -> None:
        """对这次确认执行成功的操作触发一次定向权限重算。"""
