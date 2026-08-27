"""管理命令面路由：身份判定 → 角色判定 → 命令解析 → 执行只读查询 → 审计。

只依赖注入的 ``Protocol`` 端口，不 import ``adapters/``（代码框架第二节）。真实
PostgreSQL 实现见 ``adapters/admin_registry.py``；调用方（``core/conversation/pipeline``）
只依赖本模块的 :class:`AdminCommandRouter` 与 :class:`AdminRouteOutcome`。

**审计覆盖每一种结论**，含"未登记/已撤销/角色不全"的拒绝——产品合同「管理员处理入口与
安全确认」要求"每次管理查询和操作都记录"，[验证与门禁 §八](../../../../docs/技术设计/验证与门禁.md)
要求默认拒绝规则必须能用一个不在任何名单中的未知对象证明；两者在这里合成同一条
路径：拒绝分支与放行分支共用同一个 ``self._audit``，不是只在"成功"时才留痕。

**审计器本身抛异常时，路由不得跟着崩溃或误放行**（opus 批量审查 P2 修复）：
:meth:`AdminCommandRouter._safe_record` 包住每一次 ``self._audit.record`` 调用，
失败时改记一条安全的结构化日志（标准库 ``logging``，不经过可能已损坏的
``self._audit``），并让本次交互的结论**统一退化为确定性拒绝**（`handled=False`）
——不区分原本这一分支是打算拒绝还是打算放行，因为"记不上审计"本身已经违反了
"每次都记录"这条硬承诺，让一次本该有审计记录的放行悄悄发生，比多拒绝一次更糟。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, Sequence

from lingxi.core.admin.commands import AdminCommandKind, parse_admin_command
from lingxi.core.admin.pending_action import (
    LOCAL_PERMISSION_ACTION_TYPES,
    PendingAction,
    PendingActionType,
)
from lingxi.core.admin.registry import AdminRegistryEntry, is_authorized_admin
from lingxi.core.admin.views import AdminEventView, AdminUserStatusView, LocalPermissionOverrideView

logger = logging.getLogger(__name__)

#: 与 ``core/conversation/ports.AuditSink`` 结构相同的独立 Protocol：本模块刻意不
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
    ``GRANT_PERMISSION``/``SUPPRESS_PERMISSION`` 两条写命令会填——``suspend``/
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
    ) -> AdminRouteOutcome: ...


#: 追溯/审计查询单次最多回显的事件行数——"最近关键事件"的 MVP 承诺，不是完整审计
#: 检索；超出的历史需要更大范围的能力，留给未来入口。
DEFAULT_EVENT_LIMIT = 20

#: 本模块生成的回复不经过 ``config/content.toml`` 的版本化目录（那套治理面向需要
#: 产品评审的用户承诺文案；这里是仅对已登记管理员可见的操作性诊断输出，随查询内容
#: 动态变化，不适合模板变量的强匹配约束）。version 字段固定这个字面量，审计里能一眼
#: 区分"这条内容来自目录"还是"来自管理命令面"。
_CONTENT_VERSION = "internal"

#: 写命令 → 审计动作名，供 :meth:`AdminCommandRouter._dispatch_write_action`
#: 统一查表（#319 S-P-1b 从原先的二选一 suspend/resume 三元表达式泛化而来）。
#: ``LOCAL_PERMISSION_REVOKE`` 由卡 B 登记。
_WRITE_ACTION_NAMES: dict[PendingActionType, str] = {
    PendingActionType.SUSPEND_USER: "admin.command.suspend_user",
    PendingActionType.RESUME_USER: "admin.command.resume_user",
    PendingActionType.LOCAL_PERMISSION_GRANT: "admin.command.grant_permission",
    PendingActionType.LOCAL_PERMISSION_SUPPRESS: "admin.command.suppress_permission",
    PendingActionType.LOCAL_PERMISSION_REVOKE: "admin.command.revoke_permission",
}

#: 自我目标防呆的拒绝码（#319 S-P-1b 设计卡新增）：不属于接口设计「通用约定·
#: 错误模型」的既有错误码表——那张表描述的是 prepare/confirm 核对链内部的判定
#: 分支，这里是 router 层在调用 prepare() 之前就能确定性拒绝的一条独立业务规则，
#: 与 ``adapters/postgres_pending_action.TARGET_HAS_PENDING_ACTION_CODE`` 同一
#: 取舍（新的拒绝原因就该有自己的字面量，不勉强套进不描述它的既有取值）。
_SELF_TARGET_FORBIDDEN_CODE = "self_target_forbidden"


class AdminCommandRouter:
    """管理命令面的唯一入口。见模块与 :meth:`route` 文档。"""

    def __init__(
        self,
        *,
        registry: AdminRegistryLookup,
        queries: AdminQueries,
        audit: AuditSink,
        pending_actions: PendingActionPreparer | None = None,
        confirm_cards: ConfirmCardSender | None = None,
    ) -> None:
        self._registry = registry
        self._queries = queries
        self._audit = audit
        # 两者均可选、成对未装配时安全兜底（Issue #96 S-M-02）：suspend/resume
        # 命令会被识别，但回复"该功能当前不可用"，不假装已经创建了待确认操作——
        # 与既有"未装配=安全兜底而不是崩溃"的惯例同一姿态（见
        # ``_dispatch_write_action``）。
        self._pending_actions = pending_actions
        self._confirm_cards = confirm_cards

    def _safe_record(self, action: str, /, **fields: object) -> bool:
        """给 ``self._audit.record`` 包一层保护（opus 批量审查 P2 修复）。

        审计器本身是外部注入的协作对象，它抛异常不得让路由跟着崩溃、也不得让
        一次本该有确定性结论的交互变成一个未处理的异常从 :meth:`route` 里
        逃出去——那样调用方（gateway 管线）拿到的不是"拒绝"或"放行"，而是一次
        意料之外的崩溃，破坏的是"判定或执行失败均失败关闭"这条更基础的承诺。

        返回 ``True``：记成功。返回 ``False``：审计器本身抛了异常，调用方
        （:meth:`route`/:meth:`_dispatch` 的每一个分支）必须把这次交互当成
        确定性拒绝处理——不能假装记上了，也不能因为业务判定本身其实是"放行"
        就把回复照常发出去："每次管理查询和操作都记录"是产品合同的硬承诺，
        记不上就不能假装这次交互正常发生过。

        失败时改用标准库 ``logging`` 留一条安全的结构化日志（不经过可能已经
        损坏的 ``self._audit``），字段只带原本要记的动作名与异常类型，不带
        任何业务取值（``open_id``/查询目标等）——这条兜底日志的定位是"审计器
        坏的那段时间发生过什么类型的交互"，不是替代真正的审计记录本身。
        """

        try:
            self._audit.record(action, **fields)
            return True
        except Exception as error:  # noqa: BLE001 - 审计器本身的异常不得向上传染
            logger.error(
                "admin.router.audit_failed original_action=%s error=%s",
                action,
                type(error).__name__,
            )
            return False

    def _record_or_reject(
        self, action: str, outcome: AdminRouteOutcome, /, **fields: object
    ) -> AdminRouteOutcome:
        """记一条审计成功则放行 ``outcome``；审计器本身失败则退化为确定性拒绝
        （``AdminRouteOutcome(handled=False)``），不区分原本这条分支是打算拒绝
        还是打算放行——两者在审计器坏掉之后必须收敛成同一个安全结论。"""

        if not self._safe_record(action, **fields):
            return AdminRouteOutcome(handled=False)
        return outcome

    def route(
        self,
        *,
        open_id: str,
        text: str,
        trace_id: str,
        chat_id: str = "",
        thread_id: str | None = None,
        message_id: str = "",
    ) -> AdminRouteOutcome:
        """判定 ``open_id`` 是否为当前有效管理员，是则解析并执行命令。

        **每次调用都发起一次新的登记表读取**（通过注入的 ``registry``），不持有、
        不复用任何此前的判定结果——这是"角色收回后新请求立即拒绝"在应用层的唯一
        落点。判定或执行失败均失败关闭：判定失败按"不是管理员"处理（`handled=False`，
        与真实的未登记者得到同一条业务回落，不额外暴露"内部出错了"这类信号）；
        已确认是管理员之后的执行失败则必须给出明确的错误回复，不能沉默。

        以上两条承诺都不得被审计器自身的异常打破——见 :meth:`_record_or_reject`。

        ``chat_id``/``thread_id``/``message_id``（Issue #96 S-M-02 新增，均带默认值）
        只有 ``suspend``/``resume`` 这类写命令才用得到——确认卡片必须作为触发这条
        命令的消息的回复发出（真实 CardTransport 的结构性要求，见
        ``adapters/feishu_admin_card.py``）。只读命令完全不读取这三个参数。
        """

        try:
            entry = self._registry.active_entry(open_id=open_id)
        except Exception as error:  # noqa: BLE001 - 判定失败必须失败关闭，不得放行
            return self._record_or_reject(
                "admin.command.lookup_failed",
                AdminRouteOutcome(handled=False),
                error=type(error).__name__,
                trace_id=trace_id,
            )

        if not is_authorized_admin(entry):
            # 未登记 / 已撤销 / 角色不全，默认拒绝且**不区分**这些原因——
            # 与产品合同"查询不到目标用户时返回明确的不存在结果，不猜测"同一姿态，
            # 不给探测者提供"你快登记成功了"一类的可利用信号。
            return self._record_or_reject(
                "admin.command.rejected",
                AdminRouteOutcome(handled=False),
                reason="not_authorized",
                trace_id=trace_id,
            )

        assert entry is not None  # is_authorized_admin 已确认非空

        try:
            return self._dispatch(
                entry=entry,
                text=text,
                trace_id=trace_id,
                chat_id=chat_id,
                thread_id=thread_id,
                message_id=message_id,
            )
        except Exception as error:  # noqa: BLE001 - 已确认是管理员，失败也必须有回复
            return self._record_or_reject(
                "admin.command.internal_error",
                AdminRouteOutcome(
                    handled=True,
                    content_key="admin.internal_error",
                    content_version=_CONTENT_VERSION,
                    reply_text="本次管理命令处理失败，请稍后重试。",
                ),
                actor=entry.feishu_open_id,
                error=type(error).__name__,
                trace_id=trace_id,
            )

    def _dispatch(
        self,
        *,
        entry: AdminRegistryEntry,
        text: str,
        trace_id: str,
        chat_id: str = "",
        thread_id: str | None = None,
        message_id: str = "",
    ) -> AdminRouteOutcome:
        command = parse_admin_command(text)
        roles = sorted(role.value for role in entry.roles)

        if command.kind is AdminCommandKind.HELP:
            return self._record_or_reject(
                "admin.command.help",
                AdminRouteOutcome(
                    handled=True,
                    content_key="admin.help",
                    content_version=_CONTENT_VERSION,
                    reply_text=_render_help(roles),
                ),
                actor=entry.feishu_open_id,
                roles=roles,
                trace_id=trace_id,
            )

        if command.kind is AdminCommandKind.QUERY_USER:
            assert command.identifier is not None
            status = self._queries.user_status(identifier=command.identifier)
            return self._record_or_reject(
                "admin.command.query_user",
                AdminRouteOutcome(
                    handled=True,
                    content_key="admin.user_status",
                    content_version=_CONTENT_VERSION,
                    reply_text=_render_user_status(command.identifier, status),
                ),
                actor=entry.feishu_open_id,
                roles=roles,
                target=command.identifier,
                found=status is not None,
                trace_id=trace_id,
            )

        if command.kind is AdminCommandKind.QUERY_AUDIT:
            window_hours = command.window_hours or 24
            events = self._queries.recent_events(
                identifier=command.identifier,
                window_hours=window_hours,
                limit=DEFAULT_EVENT_LIMIT,
            )
            return self._record_or_reject(
                "admin.command.query_audit",
                AdminRouteOutcome(
                    handled=True,
                    content_key="admin.audit_query",
                    content_version=_CONTENT_VERSION,
                    reply_text=_render_audit_query(command.identifier, window_hours, events),
                ),
                actor=entry.feishu_open_id,
                roles=roles,
                target=command.identifier,
                window_hours=window_hours,
                result_count=len(events),
                trace_id=trace_id,
            )

        if command.kind is AdminCommandKind.SUSPEND_USER:
            assert command.identifier is not None
            return self._dispatch_write_action(
                entry=entry,
                roles=roles,
                action_type=PendingActionType.SUSPEND_USER,
                target_identifier=command.identifier,
                chat_id=chat_id,
                thread_id=thread_id,
                message_id=message_id,
                trace_id=trace_id,
            )

        if command.kind is AdminCommandKind.RESUME_USER:
            assert command.identifier is not None
            return self._dispatch_write_action(
                entry=entry,
                roles=roles,
                action_type=PendingActionType.RESUME_USER,
                target_identifier=command.identifier,
                chat_id=chat_id,
                thread_id=thread_id,
                message_id=message_id,
                trace_id=trace_id,
            )

        if command.kind is AdminCommandKind.GRANT_PERMISSION:
            assert command.identifier is not None
            assert command.company_id is not None
            assert command.metric_name is not None
            assert command.reason is not None
            return self._dispatch_write_action(
                entry=entry,
                roles=roles,
                action_type=PendingActionType.LOCAL_PERMISSION_GRANT,
                target_identifier=command.identifier,
                chat_id=chat_id,
                thread_id=thread_id,
                message_id=message_id,
                trace_id=trace_id,
                company_id=command.company_id,
                metric_name=command.metric_name,
                reason=command.reason,
            )

        if command.kind is AdminCommandKind.SUPPRESS_PERMISSION:
            assert command.identifier is not None
            assert command.company_id is not None
            assert command.metric_name is not None
            assert command.reason is not None
            return self._dispatch_write_action(
                entry=entry,
                roles=roles,
                action_type=PendingActionType.LOCAL_PERMISSION_SUPPRESS,
                target_identifier=command.identifier,
                chat_id=chat_id,
                thread_id=thread_id,
                message_id=message_id,
                trace_id=trace_id,
                company_id=command.company_id,
                metric_name=command.metric_name,
                reason=command.reason,
            )

        if command.kind is AdminCommandKind.REVOKE_PERMISSION:
            assert command.identifier is not None
            assert command.reason is not None
            # 自我目标防呆（#319 S-P-1b 设计卡新增；卡 B 沿用同一姿态）：这里
            # **不**重复卡 A 的 ``target_identifier == entry.feishu_open_id``
            # 判断——``command.identifier`` 对收回命令而言是覆盖行的内部标识
            # （override_id），不是 open_id，与管理员自己的 ``feishu_open_id``
            # 结构上不会相等，判断也就恒假、形同虚设。真正等价的防呆在
            # ``adapters/postgres_pending_action.py`` 的 ``prepare()``：按
            # override_id 查到覆盖行的属主 open_id 之后再核对，理由是"收回输入
            # 是 id，须查库后才知属主"（同语义、检查点位置不同，见
            # ``core/admin/pending_action.py`` 模块文档）。
            return self._dispatch_write_action(
                entry=entry,
                roles=roles,
                action_type=PendingActionType.LOCAL_PERMISSION_REVOKE,
                target_identifier=command.identifier,
                chat_id=chat_id,
                thread_id=thread_id,
                message_id=message_id,
                trace_id=trace_id,
                reason=command.reason,
            )

        # UNKNOWN：语法封闭的落点——不认识的命令得到帮助/拒绝文案，绝不会被当成
        # 任何查询条件执行（`commands.py` 已把语法钉死为六选一，这里只负责回复）。
        return self._record_or_reject(
            "admin.command.unknown",
            AdminRouteOutcome(
                handled=True,
                content_key="admin.unknown_command",
                content_version=_CONTENT_VERSION,
                reply_text=_render_unknown(),
            ),
            actor=entry.feishu_open_id,
            roles=roles,
            trace_id=trace_id,
        )

    def _dispatch_write_action(
        self,
        *,
        entry: AdminRegistryEntry,
        roles: Sequence[str],
        action_type: PendingActionType,
        target_identifier: str,
        chat_id: str,
        thread_id: str | None,
        message_id: str,
        trace_id: str,
        company_id: str | None = None,
        metric_name: str | None = None,
        reason: str | None = None,
    ) -> AdminRouteOutcome:
        """``suspend``/``resume``/``grant_permission``/``suppress_permission``/
        ``revoke_permission`` 共用的写命令编排：角色核对 → 自我目标防呆 →
        ``prepare_action``（只建待确认操作，不改业务状态）→ 发送确认卡片 →
        回复管理员"已生成待确认操作，请查收卡片"。真正的业务变更只发生在管理员
        本人点击卡片之后，见 ``core/admin/card_callback.AdminCardCallbackHandler``。

        ``company_id``/``metric_name``/``reason`` 三个参数（#319 S-P-1b 新增）只有
        ``LOCAL_PERMISSION_GRANT``/``LOCAL_PERMISSION_SUPPRESS`` 会传全部三个；
        ``LOCAL_PERMISSION_REVOKE``（卡 B）只传 ``reason``，``company_id``/
        ``metric_name`` 保持 ``None``——``suspend``/``resume`` 三个都不传，保持
        既有调用形状不变。

        本方法下面的自我目标防呆判断（``target_identifier == entry.
        feishu_open_id``）对 ``LOCAL_PERMISSION_REVOKE`` 结构上恒假：收回命令的
        ``target_identifier`` 是覆盖行的内部标识（override_id，``lpo_*``），不是
        open_id，与管理员自己的 ``feishu_open_id`` 不会撞同一种形状——真正的
        自我目标防呆在 ``adapters/postgres_pending_action.py`` 的 ``prepare()``
        里（同语义、检查点位置不同，见 ``core/admin/pending_action.py`` 模块
        文档），这里的判断分支对 revoke 保留只是因为它是显式条件门、无害地
        永远不命中，不是又实现了第二遍。

        任何一步不通过都只回复文案、不产生第二条待确认操作或第二张卡片——本方法
        对每种命令最多调用一次 ``prepare()``、最多调用一次卡片发送。
        """

        action_name = _WRITE_ACTION_NAMES[action_type]

        if action_type in LOCAL_PERMISSION_ACTION_TYPES and target_identifier == entry.feishu_open_id:
            # 自我目标防呆（#319 S-P-1b 设计卡）：管理员不能对自己发起本地权限
            # 授权/抑制/收回——显式条件门，suspend/resume 恒为假（它们不在
            # ``LOCAL_PERMISSION_ACTION_TYPES`` 里），不改变既有写命令的行为。
            # 放在 prepare() 之前：这条拒绝不依赖 pending_actions/confirm_cards
            # 是否已装配，也不产生任何待确认操作——不合法的意图不应该先创建一条
            # 记录再补救，而是从一开始就不让它进入 prepare() 的调用面。
            return self._record_or_reject(
                action_name,
                AdminRouteOutcome(
                    handled=True,
                    content_key="admin.write_action_rejected",
                    content_version=_CONTENT_VERSION,
                    reply_text="不能对自己发起该操作。",
                ),
                actor=entry.feishu_open_id,
                roles=roles,
                target=target_identifier,
                code=_SELF_TARGET_FORBIDDEN_CODE,
                trace_id=trace_id,
            )

        if self._pending_actions is None or self._confirm_cards is None:
            # 未装配（例如尚未完成 gateway 接线的中间态，或测试只想覆盖只读命令）：
            # 安全兜底，不假装已经创建了待确认操作——与本类既有"未装配=安全兜底"
            # 的惯例同一姿态（对照 `core/conversation/pipeline.py` 的
            # `admin_router is None` 分支）。
            return self._record_or_reject(
                action_name,
                AdminRouteOutcome(
                    handled=True,
                    content_key="admin.write_action_unavailable",
                    content_version=_CONTENT_VERSION,
                    reply_text="该功能当前不可用，请联系 Ops。",
                ),
                actor=entry.feishu_open_id,
                roles=roles,
                target=target_identifier,
                trace_id=trace_id,
            )

        if not message_id:
            # 没有可回复的消息 ID 就没有地方挂载确认卡片——真实 CardTransport
            # 只能回复一条已知消息（见 adapters/feishu_admin_card.py）。结构上只会
            # 发生在调用方没有把 InboundMessage.message_id 传下来的时候，失败关闭。
            return self._record_or_reject(
                action_name,
                AdminRouteOutcome(
                    handled=True,
                    content_key="admin.write_action_unavailable",
                    content_version=_CONTENT_VERSION,
                    reply_text="当前上下文无法发送确认卡片，请重新发送该命令。",
                ),
                actor=entry.feishu_open_id,
                roles=roles,
                target=target_identifier,
                trace_id=trace_id,
            )

        # 不在这里重复核对 ``REQUIRED_ROLE[action_type]``：``route()`` 顶层的
        # ``is_authorized_admin(entry)`` 已经要求 MVP 三类角色**全部**为真才能
        # 走到 ``_dispatch``，因此"已通过顶层判定、但缺少某个具体角色"在当前
        # 判定语义下结构上不可能同时成立——写一条测不到、也不该测到的分支会是
        # 死代码。真正的"prepare 与 confirm 之间角色被撤销"防线在确认时刻重新
        # 判定（``core/admin/pending_action.decide_confirm`` 的 ``ROLE_REVOKED``
        # 分支，见 ``tests/test_pending_action.py``）——那一刻是唯一有意义的
        # "角色是否仍然有效"的重新核验点，因为 prepare 到 confirm 之间存在真实的
        # 时间窗口，而 prepare 内部这两步之间没有。
        outcome = self._pending_actions.prepare(
            action_type=action_type,
            target_open_id=target_identifier,
            initiated_by_open_id=entry.feishu_open_id,
            company_id=company_id,
            metric_name=metric_name,
            reason=reason,
        )
        if not outcome.decision.ok or outcome.pending is None:
            return self._record_or_reject(
                action_name,
                AdminRouteOutcome(
                    handled=True,
                    content_key="admin.write_action_rejected",
                    content_version=_CONTENT_VERSION,
                    reply_text=outcome.decision.message,
                ),
                actor=entry.feishu_open_id,
                roles=roles,
                target=target_identifier,
                code=outcome.decision.code,
                trace_id=trace_id,
            )

        dispatch_result = self._confirm_cards.send(
            pending=outcome.pending,
            chat_id=chat_id,
            thread_id=thread_id,
            reply_to_message_id=message_id,
        )
        if not dispatch_result.delivered:
            return self._record_or_reject(
                action_name,
                AdminRouteOutcome(
                    handled=True,
                    content_key="admin.write_action_card_send_failed",
                    content_version=_CONTENT_VERSION,
                    reply_text="确认卡片发送失败，本次操作不会执行，请稍后重试。",
                ),
                actor=entry.feishu_open_id,
                roles=roles,
                target=target_identifier,
                pending_action_id=outcome.pending.id,
                trace_id=trace_id,
            )

        return self._record_or_reject(
            action_name,
            AdminRouteOutcome(
                handled=True,
                content_key="admin.write_action_pending",
                content_version=_CONTENT_VERSION,
                reply_text="已生成待确认操作，请查收你的飞书私聊确认卡片（十分钟内有效）。",
            ),
            actor=entry.feishu_open_id,
            roles=roles,
            target=target_identifier,
            pending_action_id=outcome.pending.id,
            trace_id=trace_id,
        )


def _render_help(roles: Sequence[str]) -> str:
    role_line = "、".join(roles) if roles else "(无)"
    return (
        "Lingxi 管理命令：\n"
        "/admin help — 显示本帮助\n"
        "/admin user <标识> — 查询用户开通状态\n"
        "/admin audit [标识] [小时数] — 查询最近事件（默认 24 小时）\n"
        "/admin suspend <标识> — 发起停用该用户（需本人飞书确认卡片）\n"
        "/admin resume <标识> — 发起恢复该用户（需本人飞书确认卡片）\n"
        "/admin grant_permission <标识> <公司> <指标> <原因> — 发起本地授权（需本人飞书确认卡片）\n"
        "/admin suppress_permission <标识> <公司> <指标> <原因> — 发起本地抑制（需本人飞书确认卡片）\n"
        "/admin revoke_permission <覆盖ID> <原因> — 发起收回本地覆盖（需本人飞书确认卡片；"
        "覆盖ID 见 /admin user 查询结果）\n"
        f"当前角色：{role_line}"
    )


#: 覆盖行原因文本在 ``/admin user`` 回显时的截断长度（#319 S-P-1b 卡 B 设计
#: 卡）：不回显 reason 全文，只给足够定位这是哪一次特批/收回的前 20 字预览——
#: 与群通知的脱敏纪律（``core/admin/notification.render_group_notice``）同一
#: 精神，管理员查询回显不是审计全文检索入口，完整原因见对应的确认卡终态文案
#: 或未来的审计检索。
_OVERRIDE_REASON_PREVIEW_LENGTH = 20

#: 迁移 ``0072`` ``direction`` 列取值 → 中文展示文案，只在这里（展示层）出现，
#: 不引入对 ``core/permission/local_override.OverrideDirection`` 的依赖——本模块
#: 拿到的是 ``LocalPermissionOverrideView.direction`` 这个已经解出来的字符串，
#: 与 ``core/admin/notification.py`` 的 ``_ACTION_LABEL`` 同一取舍（展示文案就地
#: 维护一份，不反向依赖纯逻辑层的枚举类型）。
_OVERRIDE_DIRECTION_LABEL: dict[str, str] = {"grant": "授权", "suppress": "抑制"}


def _render_local_overrides(overrides: Sequence[LocalPermissionOverrideView]) -> str:
    """``/admin user`` 回显的「当前生效本地覆盖」段——每行一条，无覆盖时一行
    「无本地覆盖」（#319 S-P-1b 卡 B）。"""

    if not overrides:
        return "无本地覆盖"
    lines = []
    for override in overrides:
        direction_label = _OVERRIDE_DIRECTION_LABEL.get(override.direction, override.direction)
        reason = override.reason
        if len(reason) > _OVERRIDE_REASON_PREVIEW_LENGTH:
            reason = reason[:_OVERRIDE_REASON_PREVIEW_LENGTH] + "…"
        lines.append(
            f"- {override.override_id}（{direction_label}）"
            f"公司 {override.company_id} · 指标 {override.metric_name} · "
            f"原因 {reason} · {override.created_at}"
        )
    return "\n".join(lines)


def _render_user_status(identifier: str, status: AdminUserStatusView | None) -> str:
    if status is None:
        return f"未找到标识为 {identifier} 的用户记录。"
    return (
        f"用户 {identifier}：\n"
        f"开通状态：{status.provisioning_state}\n"
        f"账号状态：{status.account_state}\n"
        f"权限版本：{status.permission_version}\n"
        f"更新时间：{status.updated_at}\n"
        f"当前生效本地覆盖：\n{_render_local_overrides(status.local_overrides)}"
    )


def _render_audit_query(
    identifier: str | None, window_hours: int, events: Sequence[AdminEventView]
) -> str:
    scope = f"标识 {identifier} 的" if identifier else ""
    if not events:
        return f"最近 {window_hours} 小时内没有找到{scope}相关事件。"
    header = f"最近 {window_hours} 小时内{scope}最近事件（{len(events)} 条）："
    lines = [
        f"- {event.received_at} {event.event_type} → "
        f"{event.handled_as or '(未标记)'}（追溯号 {event.trace_id}）"
        for event in events
    ]
    return "\n".join((header, *lines))


def _render_unknown() -> str:
    return "未识别的管理命令，请发送 /admin help 查看可用命令。"
