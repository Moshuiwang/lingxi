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

import inspect
import logging
from dataclasses import dataclass
from typing import Protocol, Sequence

from lingxi.config.content import ContentCatalog, RenderedContent, default_content_catalog
from lingxi.core.admin.commands import (
    AdminCommandKind,
    AdminRejectReason,
    describe_admin_tokens,
    parse_admin_command,
)
from lingxi.core.admin.display_names import AdminDisplayNames
from lingxi.core.admin.pending_action import (
    LOCAL_PERMISSION_ACTION_TYPES,
    PendingAction,
    PendingActionType,
)
from lingxi.core.admin.registry import AdminRegistryEntry, is_authorized_admin
from lingxi.core.admin.views import (
    AdminEventView,
    AdminTraceView,
    AdminUserStatusView,
    GalaxySourceSummary,
    LocalPermissionOverrideView,
)

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

    def resolve_override_id(
        self, *, open_id: str, company_id: str, metric_name: str
    ) -> str | None:
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

#: revoke 新参数形状（#439 A 档：identifier + 公司 + 指标）反查 override_id 零命中/
#: 多命中歧义时的拒绝码——与 ``adapters/postgres_pending_action.
#: TARGET_HAS_PENDING_ACTION_CODE``/``_SELF_TARGET_FORBIDDEN_CODE`` 同一取舍：这是
#: router 层在调用 prepare() 之前就能确定性拒绝的一条独立业务规则，不是
#: ``pending_action.py`` 既有错误码表描述的某个分支，因此不勉强套用现有取值。
_OVERRIDE_NOT_FOUND_CODE = "override_not_found"


class AdminCommandRouter:
    """管理命令面的唯一入口。见模块与 :meth:`route` 文档。"""

    def __init__(
        self,
        *,
        registry: AdminRegistryLookup,
        queries: AdminQueries,
        audit: AuditSink,
        display_names: AdminDisplayNames,
        pending_actions: PendingActionPreparer | None = None,
        confirm_cards: ConfirmCardSender | None = None,
        management_cards: ManagementCardSender | None = None,
    ) -> None:
        self._registry = registry
        self._queries = queries
        self._audit = audit
        # 必填（Trace #469 S-1）：文本回复端（_render_user_status/
        # _render_local_overrides/_render_galaxy_source/_render_audit_query/
        # _render_trace）全部经这个端口翻译 open_id/公司编号/指标 ID，不允许
        # "未装配则原样回显内部标识"的安全兜底——见 core/admin/display_names.
        # AdminDisplayNames 模块文档「安全边界」一节。
        self._display_names = display_names
        # 两者均可选、成对未装配时安全兜底（Issue #96 S-M-02）：suspend/resume
        # 命令会被识别，但回复"该功能当前不可用"，不假装已经创建了待确认操作——
        # 与既有"未装配=安全兜底而不是崩溃"的惯例同一姿态（见
        # ``_dispatch_write_action``）。
        self._pending_actions = pending_actions
        self._confirm_cards = confirm_cards
        # 未装配时 `/admin user` 只回复既有文本，不发送管理卡（#439 B 档，同一条
        # "未装配=安全兜底"惯例）——不是写路径，不需要与 pending_actions 成对存在。
        self._management_cards = management_cards

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
        origin_card_message_id: str | None = None,
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
                origin_card_message_id=origin_card_message_id,
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
        origin_card_message_id: str | None = None,
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
            resolved_identifier = self._queries.resolve_identifier(identifier=command.identifier)
            status = self._queries.user_status(identifier=resolved_identifier)
            if status is not None:
                # 管理卡（#439 B 档）：与文本回复并存，不替代——管理卡发送失败或未
                # 装配都不影响既有文本回复这条主路径（"未装配=安全兜底"既有惯例）。
                self._send_management_card(
                    status=status,
                    display_identifier=command.identifier,
                    initiated_by_open_id=entry.feishu_open_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    message_id=message_id,
                    trace_id=trace_id,
                )
            return self._record_or_reject(
                "admin.command.query_user",
                AdminRouteOutcome(
                    handled=True,
                    content_key="admin.user_status",
                    content_version=_CONTENT_VERSION,
                    reply_text=_render_user_status(
                        command.identifier, status, display_names=self._display_names
                    ),
                ),
                actor=entry.feishu_open_id,
                roles=roles,
                target=command.identifier,
                found=status is not None,
                trace_id=trace_id,
            )

        if command.kind is AdminCommandKind.QUERY_AUDIT:
            window_hours = command.window_hours or 24
            resolved_audit_identifier = (
                self._queries.resolve_identifier(identifier=command.identifier)
                if command.identifier
                else None
            )
            events = self._queries.recent_events(
                identifier=resolved_audit_identifier,
                window_hours=window_hours,
                limit=DEFAULT_EVENT_LIMIT,
            )
            return self._record_or_reject(
                "admin.command.query_audit",
                AdminRouteOutcome(
                    handled=True,
                    content_key="admin.audit_query",
                    content_version=_CONTENT_VERSION,
                    reply_text=_render_audit_query(resolved_audit_identifier, window_hours, events),
                ),
                actor=entry.feishu_open_id,
                roles=roles,
                target=command.identifier,
                window_hours=window_hours,
                result_count=len(events),
                trace_id=trace_id,
            )

        if command.kind is AdminCommandKind.QUERY_TRACE:
            assert command.identifier is not None
            trace = self._queries.trace_lookup(trace_id=command.identifier)
            return self._record_or_reject(
                "admin.command.query_trace",
                AdminRouteOutcome(
                    handled=True,
                    content_key="admin.trace_query",
                    content_version=_CONTENT_VERSION,
                    reply_text=_render_trace(command.identifier, trace),
                ),
                # _render_trace 不展示 open_id/公司/指标（只展示追溯号本身与
                # 已经过 _render_user_status 同款状态翻译的开通/账号状态，见
                # 该函数实现），因此不需要额外传入 display_names。
                actor=entry.feishu_open_id,
                roles=roles,
                target=command.identifier,
                found=trace is not None,
                trace_id=trace_id,
            )

        if command.kind is AdminCommandKind.SUSPEND_USER:
            assert command.identifier is not None
            return self._dispatch_write_action(
                entry=entry,
                roles=roles,
                action_type=PendingActionType.SUSPEND_USER,
                target_identifier=self._queries.resolve_identifier(identifier=command.identifier),
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
                target_identifier=self._queries.resolve_identifier(identifier=command.identifier),
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
                target_identifier=self._queries.resolve_identifier(identifier=command.identifier),
                chat_id=chat_id,
                thread_id=thread_id,
                message_id=message_id,
                trace_id=trace_id,
                company_id=command.company_id,
                metric_name=self._queries.resolve_metric_name(metric_token=command.metric_name),
                reason=command.reason,
            )

        if command.kind is AdminCommandKind.GRANT_POSITION_PERMISSION:
            assert command.identifier is not None
            assert command.position_name is not None
            assert command.company_scope is not None
            assert command.reason is not None
            return self._dispatch_write_action(
                entry=entry,
                roles=roles,
                action_type=PendingActionType.LOCAL_PERMISSION_GRANT,
                target_identifier=self._queries.resolve_identifier(identifier=command.identifier),
                chat_id=chat_id,
                thread_id=thread_id,
                message_id=message_id,
                trace_id=trace_id,
                position_name=command.position_name,
                company_scope=command.company_scope,
                reason=command.reason,
                origin_card_message_id=origin_card_message_id,
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
                target_identifier=self._queries.resolve_identifier(identifier=command.identifier),
                chat_id=chat_id,
                thread_id=thread_id,
                message_id=message_id,
                trace_id=trace_id,
                company_id=command.company_id,
                metric_name=self._queries.resolve_metric_name(metric_token=command.metric_name),
                reason=command.reason,
            )

        if command.kind is AdminCommandKind.REVOKE_PERMISSION:
            assert command.identifier is not None
            assert command.reason is not None
            override_id = command.identifier
            if command.company_id is not None:
                # 形状 2（#439 A 档新增，见 commands.py 文档）：identifier 是目标
                # 用户标识（open_id 或邮箱），company_id/metric_name 一起反查
                # override_id；反查不到就直接回复，不产生任何待确认操作——这一步
                # 发生在 prepare() 之前（还没有 override_id 可以喂给它），因此不
                # 计入 `decide_prepare` 的 "not_found" 结论，是独立的一道前置判断。
                assert command.metric_name is not None
                resolved_open_id = self._queries.resolve_identifier(
                    identifier=command.identifier
                )
                resolved_metric = self._queries.resolve_metric_name(
                    metric_token=command.metric_name
                )
                found_override_id = self._queries.resolve_override_id(
                    open_id=resolved_open_id,
                    company_id=command.company_id,
                    metric_name=resolved_metric,
                )
                if found_override_id is None:
                    return self._record_or_reject(
                        _WRITE_ACTION_NAMES[PendingActionType.LOCAL_PERMISSION_REVOKE],
                        AdminRouteOutcome(
                            handled=True,
                            content_key="admin.write_action_rejected",
                            content_version=_CONTENT_VERSION,
                            reply_text=(
                                "未找到匹配的当前生效本地覆盖（标识/公司/指标不匹配，"
                                "或已被撤销，或同一键同时存在补充授权与屏蔽指标两条"
                                "需要精确指定），请用 /admin user 查询后核对，或使用"
                                "查询结果附带的管理卡撤销按钮精确指定撤销。"
                            ),
                        ),
                        actor=entry.feishu_open_id,
                        roles=roles,
                        target=command.identifier,
                        code=_OVERRIDE_NOT_FOUND_CODE,
                        trace_id=trace_id,
                    )
                override_id = found_override_id
            # 自我目标防呆（#319 S-P-1b 设计卡新增；卡 B 沿用同一姿态）：这里
            # **不**重复卡 A 的 ``target_identifier == entry.feishu_open_id``
            # 判断——传给 ``_dispatch_write_action`` 的 ``override_id`` 是覆盖行的
            # 内部标识，不是 open_id，与管理员自己的 ``feishu_open_id`` 结构上不会
            # 相等，判断也就恒假、形同虚设。真正等价的防呆在
            # ``adapters/postgres_pending_action.py`` 的 ``prepare()``：按
            # override_id 查到覆盖行的属主 open_id 之后再核对，理由是"收回输入
            # 是 id，须查库后才知属主"（同语义、检查点位置不同，见
            # ``core/admin/pending_action.py`` 模块文档）。
            return self._dispatch_write_action(
                entry=entry,
                roles=roles,
                action_type=PendingActionType.LOCAL_PERMISSION_REVOKE,
                target_identifier=override_id,
                chat_id=chat_id,
                thread_id=thread_id,
                message_id=message_id,
                trace_id=trace_id,
                reason=command.reason,
            )

        # UNKNOWN：语法封闭的落点——不认识的命令得到帮助/拒绝文案，绝不会被当成
        # 任何查询条件执行（`commands.py` 已把语法钉死为六选一，这里只负责回复）。
        # `reject_reason`（Issue #492）只是一个枚举名，不含任何管理员输入的原文，
        # 记进审计是为了让下一次"真人踩到但现场取不到正文"的调查至少知道是哪一段
        # 没通过——Trace #502 W0-2 那次调查正是卡在这里（三条失败只留下一个不带
        # 原因的 UNKNOWN，两个竞争假设至今无法区分）。
        # 取证字段（#521 F4-1）：只有 ``/admin`` 开头的**命令尝试**留证。形状分类不含
        # 原文；原文本身产品负责人已裁定本项目验证范围内不按隐私数据处理（凭据/授权码
        # 从不出现在命令面）。闲聊一个字都不记，见 ``describe_admin_tokens``。
        shapes = describe_admin_tokens(text)
        forensics: dict[str, object] = {}
        if shapes.is_admin_prefixed:
            forensics = {
                "token_count": shapes.argument_count,
                "token_shapes": shapes.shape_summary,
                "raw_admin_text": shapes.raw_text,
            }
        rendered = _render_unknown(command.reject_reason, shapes.argument_count)
        return self._record_or_reject(
            "admin.command.unknown",
            AdminRouteOutcome(
                handled=True,
                content_key=rendered.key,
                content_version=rendered.version,
                reply_text=rendered.text,
            ),
            actor=entry.feishu_open_id,
            roles=roles,
            reject_reason=(
                command.reject_reason.value if command.reject_reason is not None else None
            ),
            trace_id=trace_id,
            **forensics,
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
        position_name: str | None = None,
        company_scope: str | None = None,
        origin_card_message_id: str | None = None,
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
                    reply_text="该功能当前不可用，请联系技术支持。",
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
        prepare_kwargs: dict[str, object] = {
            "action_type": action_type,
            "target_open_id": target_identifier,
            "initiated_by_open_id": entry.feishu_open_id,
            "company_id": company_id,
            "metric_name": metric_name,
            "reason": reason,
        }
        # 旧的注入式 preparer 仍只接受公司×指标字段；新职位表单才附加扩展
        # 参数，避免无关的旧只读/命令测试因可选字段破坏接口兼容。
        if position_name is not None:
            prepare_kwargs["position_name"] = position_name
        if company_scope is not None:
            prepare_kwargs["company_scope"] = company_scope
        if origin_card_message_id is not None:
            prepare_kwargs["origin_card_message_id"] = origin_card_message_id
        if position_name is not None:
            try:
                prepare_parameters = inspect.signature(self._pending_actions.prepare).parameters
                accepts_trace = "trace_id" in prepare_parameters or any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in prepare_parameters.values()
                )
            except (TypeError, ValueError):
                accepts_trace = False
            if accepts_trace:
                prepare_kwargs["trace_id"] = trace_id
        outcome = self._pending_actions.prepare(**prepare_kwargs)  # type: ignore[arg-type]
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
                reply_text=(
                    "已提交，请在下方确认卡片上确认（10 分钟内有效）。"
                    if position_name is not None
                    else "已生成待确认操作，请查收你的飞书私聊确认卡片（十分钟内有效）。"
                ),
            ),
            actor=entry.feishu_open_id,
            roles=roles,
            target=target_identifier,
            pending_action_id=outcome.pending.id,
            trace_id=trace_id,
        )

    def _send_management_card(
        self,
        *,
        status: AdminUserStatusView,
        display_identifier: str,
        initiated_by_open_id: str = "",
        chat_id: str,
        thread_id: str | None,
        message_id: str,
        trace_id: str,
    ) -> None:
        """`/admin user` 附带发送用户权限管理卡（#439 B 档），best-effort。

        与写命令的确认卡片不同，管理卡**不是**一次待确认操作——它只是一张承载
        「查看 + 发起」的交互卡，本身不改变任何状态（合同"待确认操作发送到发起
        管理员本人的飞书私聊，卡片只承担最终确认，不承担搜索、比较、审批流或
        复杂信息填写"这条只约束**确认卡**；管理卡是确认卡的前一步，专门承担
        "复杂信息填写"，两者是两张不同的卡，见本 Story 报告"同卡二次确认"裁定）。
        因此发送失败、或没有可回复的 ``message_id``（结构上只会发生在调用方没有
        把触发消息 ID 传下来的时候）都只记一条 best-effort 审计，不影响
        `/admin user` 既有的文本回复这条主路径，也不让整次 `route()` 调用失败——
        与 `_notify_group`/`_update_card_to_terminal` 两处既有 best-effort 分支
        （`core/admin/card_callback.py`）同一姿态。
        """

        if self._management_cards is None or not message_id:
            return
        try:
            send_kwargs: dict[str, object] = {
                "status": status,
                "display_identifier": display_identifier,
                "chat_id": chat_id,
                "thread_id": thread_id,
                "reply_to_message_id": message_id,
            }
            # 旧的测试/插件 sender 没有发起人字段时保持兼容；生产 dispatcher
            # 会持久化真实 initiator，供回调的身份校验与审计恢复。
            try:
                supports_initiator = "initiated_by_open_id" in inspect.signature(self._management_cards.send).parameters
            except (TypeError, ValueError):
                supports_initiator = False
            if supports_initiator:
                send_kwargs["initiated_by_open_id"] = initiated_by_open_id
            self._management_cards.send(
                **send_kwargs,
            )
        except Exception as error:  # noqa: BLE001 - 管理卡发送失败不影响文本回复
            self._safe_record(
                "admin.command.management_card_send_failed",
                target=display_identifier,
                error=type(error).__name__,
                trace_id=trace_id,
            )


def _render_help(roles: Sequence[str]) -> str:
    """术语统一（Trace #469 S-1，PM 补充裁定第 4 条）：命令说明改用「补充授权」
    「屏蔽指标」「撤销」，与管理卡按钮、确认卡/终态卡/群通知同一份说法。最后一行
    不再声称"覆盖ID 见 /admin user 查询结果"——`/admin user` 回显自本批起不再
    展示裸 override_id/permission_group_id（内部 ID 只留审计，见
    ``_render_local_overrides``），已知覆盖ID 时仍可直接使用，但多数场景请改用上一行
    的「标识+公司+指标」形式或管理卡「撤销」按钮。
    """

    role_line = "、".join(roles) if roles else "(无)"
    return (
        "BI Plus 管理命令：\n"
        "/admin help — 显示本帮助\n"
        "/admin user <标识> — 查询用户开通状态并调出权限管理卡（标识支持邮箱或 open_id）\n"
        "/admin audit [标识] [小时数] — 查询最近事件（默认 24 小时，标识支持邮箱或 open_id）\n"
        "/admin trace <追溯号> — 按追溯号查开通失败原因与事件时间线\n"
        "/admin suspend <标识> — 发起停用该用户（需本人飞书确认卡片）\n"
        "/admin resume <标识> — 发起恢复该用户（需本人飞书确认卡片）\n"
        "/admin grant_permission <标识> <公司> <指标> <原因> — 发起补充授权"
        "（需本人飞书确认卡片；指标支持已配置的中文别名）\n"
        "/admin suppress_permission <标识> <公司> <指标> <原因> — 发起屏蔽指标（同上）\n"
        "/admin revoke_permission <标识> <公司> <指标> <原因> — 发起撤销本地覆盖"
        "（需本人飞书确认卡片；与 grant/suppress 同一参数形状，服务端反查覆盖ID）\n"
        "/admin revoke_permission <覆盖ID/权限组ID> <原因> — 已知 ID 时直接发起撤销"
        "（多数场景请改用上一行的标识+公司+指标形式，或使用管理卡撤销按钮）\n"
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
#: 维护一份，不反向依赖纯逻辑层的枚举类型）。术语与 ``notification._ACTION_
#: LABEL``、``management_card._DIRECTION_LABEL`` 三处同步（Trace #469 S-1）。
_OVERRIDE_DIRECTION_LABEL: dict[str, str] = {"grant": "补充授权", "suppress": "屏蔽指标"}


def _render_local_overrides(
    overrides: Sequence[LocalPermissionOverrideView], *, display_names: AdminDisplayNames
) -> str:
    """``/admin user`` 回显的「当前生效本地覆盖」段。

    新职位+范围授权的展开行共享 ``permission_group_id``，因此在用户可见文本中也
    聚合成一个职位+范围项；只有历史 ``permission_group_id IS NULL`` 行维持逐行
    展示。无覆盖时返回一行「无本地覆盖」（#319 S-P-1b 卡 B）。

    自 Trace #469 S-1 起**不再展示 override_id**（内部 ID 只留审计，管理员需要
    发起撤销时用「标识+公司+指标」形式或管理卡撤销按钮，均不需要先看到这个内部
    ID）；公司/指标经 ``display_names`` 翻译成人类可读文本。
    """

    if not overrides:
        return "无本地覆盖"
    groups: dict[str, list[LocalPermissionOverrideView]] = {}
    for override in overrides:
        if override.permission_group_id:
            groups.setdefault(override.permission_group_id, []).append(override)

    lines: list[str] = []
    rendered_groups: set[str] = set()
    for override in overrides:
        group_id = override.permission_group_id
        if group_id:
            if group_id in rendered_groups:
                continue
            rendered_groups.add(group_id)
            first = groups[group_id][0]
            reason = first.reason
            if len(reason) > _OVERRIDE_REASON_PREVIEW_LENGTH:
                reason = reason[:_OVERRIDE_REASON_PREVIEW_LENGTH] + "…"
            scope = first.company_scope or first.company_id
            scope_label = "全部" if scope == "*" else display_names.company_label(company_id=scope)
            lines.append(
                f"- （补充授权）职位 {first.position_name or '（未知职位）'} ·"
                f" 公司范围 {scope_label} · 覆盖 {len(groups[group_id])} 项权限 ·"
                f" 原因 {reason} · {first.created_at}"
            )
            continue
        direction_label = _OVERRIDE_DIRECTION_LABEL.get(override.direction, override.direction)
        reason = override.reason
        if len(reason) > _OVERRIDE_REASON_PREVIEW_LENGTH:
            reason = reason[:_OVERRIDE_REASON_PREVIEW_LENGTH] + "…"
        company_label = display_names.company_label(company_id=override.company_id)
        metric_label = display_names.metric_label(metric_id=override.metric_name)
        lines.append(
            f"- （{direction_label}）"
            f"公司 {company_label} · 指标 {metric_label} · "
            f"原因 {reason} · {override.created_at}"
        )
    return "\n".join(lines)


#: 零银河权限用户的本地授权边界提示（#319 动机场景，Trace #328 opus 审查 P1）
#: **已随 PM 2026-08-29 裁定（Issue #419）撤销**：`_refresh_user`/`_publish` 的
#: 四源合并不再挂在 `aggregate.granted` 判据之后，管理员对这类用户发起的本地
#: 授权现在无条件参与合并（下一轮重算或下一次开通链会把它发布出去），"暂不
#: 生效"这句提示已经不实，直接删除——不需要在 `_render_user_status` 里再判断
#: 是否附加它，也不再需要 `decide_prepare` 额外查一次银河权限才能决定要不要
#: 提示（那正是删除前留着这句提示的唯一理由）。


#: 银河来源摘要（#439 B 档）"算不出来"的三个原因码 → 中文提示，与
#: ``PermissionAggregate.reason`` 取值域（"算出来了、结论是没有"）分开处理——
#: 后者直接展示原始 reason 字面量即可（内部原因码，运维/管理员共用同一份词表，
#: 与本模块其余展示层惯例一致，不额外维护一份中文翻译）。
_GALAXY_SOURCE_UNAVAILABLE_REASONS: frozenset[str] = frozenset(
    {
        "roster_snapshot_unavailable",
        "galaxy_snapshot_unavailable",
        "role_function_map_unavailable",
    }
)


def _render_galaxy_source(
    summary: GalaxySourceSummary | None, *, display_names: AdminDisplayNames
) -> str:
    """``/admin user`` 回显的「银河来源」段（#439 B 档，见
    ``views.GalaxySourceSummary`` 文档）。仅供展示，不参与任何权限判定。公司
    编号经 ``display_names.company_label`` 翻译成「中文名（编号）」（Trace
    #469 S-1）。"""

    if summary is None or summary.reason in _GALAXY_SOURCE_UNAVAILABLE_REASONS:
        return "银河来源不可用（无法计算，不代表该用户没有银河权限）"
    if not summary.granted:
        return f"当前没有可用的银河权限（原因：{summary.reason}）"
    if summary.all_companies:
        company_label = "全部公司"
    else:
        company_label = "、".join(
            display_names.company_label(company_id=cid) for cid in summary.companies
        )
    function_label = "、".join(summary.functions)
    return f"公司范围 {company_label} · 职能 {function_label}（职能标签，非最终指标名）"


#: 内部标识前缀白名单（Trace #469 S-1）：管理员可见文案零 ou_/lpo_/lpg_/pac_ 是
#: 结构性硬要求，即使这个值是管理员自己刚刚敲进来的输入也不例外——真正需要
#: 隐藏的是"这串文本长得像系统内部标识"这件事本身，与它的来源（系统生成 /
#: 管理员键入）无关。非内部 ID 形状的输入（多数情况下是邮箱，或管理员的一次
#: 手误）原样回显，不额外做资料查找。
_INTERNAL_ID_PREFIXES: tuple[str, ...] = ("ou_", "lpo_", "lpg_", "pac_")


def _safe_identifier_echo(identifier: str) -> str:
    if identifier.startswith(_INTERNAL_ID_PREFIXES):
        return "该用户"
    return identifier


#: 开通/账号状态英文机器码 → 中文（Trace #469 S-1 TOP-6）：与迁移基线里
#: ``app_user`` 表 ``provisioning_state``/``account_state`` 两个 CHECK 约束的
#: 取值域一一对应。未登记的取值原样展示，不当成异常——两个 CHECK 约束已经在
#: 数据库层面把取值收窄到这张表列出的全部成员，这里的 "未登记" 分支结构上只在
#: 约束本身被修改、而这份词表忘了同步时才会命中，失败开放比拒绝渲染整条回复
#: 更安全。
_PROVISIONING_STATE_LABEL: dict[str, str] = {
    "guest": "访客（尚未开始开通）",
    "matching": "银河权限匹配中",
    "manual_review": "待人工复核",
    "provisioning": "开通中",
    "mcp_syncing": "问数权限同步中",
    "active": "已开通",
    "aborted": "开通已中止",
}
_ACCOUNT_STATE_LABEL: dict[str, str] = {
    "enabled": "启用",
    "suspended": "已停用",
    "deleting": "删除中",
    "deleted": "已删除",
}

#: ``/admin trace`` 回显里的入站事件类型 → 中文（Trace #469 修复包 B，B-6）：
#: 与 ``adapters/feishu_events.py`` 的 ``MESSAGE_RECEIVE_EVENT``/
#: ``CARD_ACTION_TRIGGER_EVENT`` 两个字面量一一对应——本模块历来不 import
#: ``adapters/``（模块文档「只依赖注入的 Protocol 端口」），因此这里独立登记
#: 一份取值，不反向依赖那个模块。未登记的取值走 :func:`_display_or_unregistered`
#: 回退，不假装认识每一个未来可能新增的事件类型。
_EVENT_TYPE_LABEL: dict[str, str] = {
    "im.message.receive_v1": "用户消息",
    "card.action.trigger": "卡片按钮/表单交互",
}

#: ``inbound_event.handled_as`` 枚举 → 中文（Trace #469 修复包 B，B-6）：与
#: ``core/conversation/ports.HandledAs`` 的六个取值一一对应，同上一条注释
#: 同一理由不反向 import 该枚举。
_HANDLED_AS_LABEL: dict[str, str] = {
    "task_queued": "已入队等待处理",
    "busy_hint": "系统繁忙提示",
    "not_provisioned": "未开通，未受理",
    "auto_provisioning": "自动开通编排中",
    "command": "管理命令",
    "dropped": "重复投递，已丢弃",
}

#: 开通失败原因机器码 → 中文（Trace #469 修复包 B，B-6）：覆盖
#: ``core/identity/onboarding_runner.py``/``apps/scheduler/stalled_
#: provisioning.py`` 现有登记的全部原因码；与上面两张表同一姿态——白名单式
#: 展示层翻译，不反向依赖产生这些字面量的具体模块。未登记的取值（未来新增
#: 但这里忘了同步）走 :func:`_display_or_unregistered` 回退，不崩、不假装
#: 认识。
_FAILURE_REASON_LABEL: dict[str, str] = {
    "account_not_enabled": "账号未启用",
    "already_running": "该追溯号的开通已在处理中（重复触发）",
    "app_access_token_unwired": "应用访问令牌未接线",
    "delegated_subject": "专用主体，不走个人开通流程",
    "executor_unavailable": "开通执行器不可用",
    "innertest_roster_rejected": "不在内测名单中",
    "mcp_sync_timeout": "问数权限同步超时",
    "metric_translation_map_unavailable": "指标翻译映射不可用",
    "missing_access_token_supply": "缺少访问令牌来源配置",
    "missing_encrypt_key": "缺少加密密钥配置",
    "missing_environment_variable": "缺少必需的环境变量",
    "partial_coordinates": "银河权限坐标不完整",
    "role_function_map_unavailable": "角色功能映射不可用",
    "rotation_persist_failed": "凭据轮换结果落库失败",
    "stalled_lease_expired": "认领租约已过期（长时间无进展）",
    "stopping": "进程正在停机",
    "user_access_token_unwired": "用户访问令牌未接线",
    "user_environment_sweep_failed": "用户环境清理失败",
}


#: ``task.status`` 枚举 → 中文（Issue #495）：与迁移 ``0059`` 把 ``task`` 的
#: status CHECK 扩成六个取值一一对应。与本文件其余词表同一姿态——白名单式展示层
#: 翻译，未登记走 :func:`_display_or_unregistered` 回退。
_TASK_STATUS_LABEL: dict[str, str] = {
    "queued": "排队中",
    "running": "执行中",
    "awaiting_delivery": "已收口，等待答复送达",
    "succeeded": "成功",
    "failed": "失败",
    "stopped": "已停止",
}

#: 任务失败机器码 → 中文（Issue #495）。**同时覆盖两列**：``task.failure_code``
#: （迁移 ``0080`` 新增，worker 给出的**细分**失败码）与 ``task.error_kind``
#: （被 ``apps/worker/service.py::_failure_content`` 压平成用户文案分类之后的粗
#: 粒度值）。两列是不同的取值域：``drain_timeout``/``sdk_unavailable``/
#: ``cancelled``/``gate_bypassed`` 在 ``error_kind`` 那一列全部塌进同一个
#: ``session_failed``，正是本 Issue 要消灭的那种"什么都看不出来"；反过来
#: ``error_kind`` 也有 ``failure_code`` 覆盖不到的取值——**没有经过
#: ``write_terminal_event`` 的失败终态**（心跳超时回收 ``retry_exhausted``/
#: ``side_effect_uncertain``、投递到期 ``delivery_expired``、排队超时
#: ``queued_timeout`` 等，写入方是 ``adapters/postgres_conversation/
#: _queue_lifecycle.py``）在新列上恒为 ``NULL``，只有 ``error_kind`` 说得出原因。
#: 因此回显按「有 ``failure_code`` 用它，否则退回 ``error_kind``」取值，一张表
#: 服务两列，不维护两份会各自漂移的词表。
#:
#: ``core/daily_report.py`` 另有一份只翻译 ``task.error_kind`` 的词表，服务的是
#: 「失败分类 Top」榜单——两处对**共有**的词刻意逐字保持同一措辞，改动其一时请
#: 同步另一处。**唯一一处有意分岔**：``session_failed`` 在那边是「会话执行失败」，
#: 这边加了「（未分类，见底层异常）」——榜单是聚合计数、下面没有别的行可看，而
#: 这里紧接着就是「失败签名/底层异常类型」那一行，把读者指过去正是本 Issue 的要点。
_TASK_FAILURE_LABEL: dict[str, str] = {
    "cancelled": "执行被取消",
    "config_error": "worker 配置错误",
    "context_too_long": "上下文过长",
    "delivery_expired": "投递已过期",
    "drain_timeout": "收尾超时",
    "gate_bypassed": "工具调用绕过了判定屏障（屏障失效）",
    "interrupted": "用户主动停止",
    "max_turns_exceeded": "对话轮数超限",
    "model_protocol_breakdown": "模型输出协议异常",
    "queued_timeout": "排队超时未领取",
    "redacted_withheld": "内容因安全策略被拦截",
    "result_too_large": "查询结果过大",
    "mcp_bad_gateway": "指标 MCP 网关返回 502（建连失败）",
    "retry_exhausted": "重试次数耗尽",
    "running_timeout": "执行超时",
    "sdk_unavailable": "Agent SDK 不可用",
    "session_failed": "会话执行失败（未分类，见底层异常）",
    "side_effect_uncertain": "执行结果不确定（需人工核实是否已生效）",
    "stopped": "用户主动停止",
    "turn_not_closed": "回合未收口，且没有留下失败码",
    "turn_timeout": "单轮对话超时",
    "unnamed_failure": "失败记录缺失分类码",
    "user_mcp_config_unavailable": "用户问数配置不可用",
    "worker_version_unavailable": "目标执行版本不可用",
}

# ``task_document_delivery_request.status`` 枚举 → 中文（Issue #499）：文档消费在
# gateway 独立进程完成，任务本身成功不等于文档已经成功交付；``/admin trace`` 必须
# 把这条独立状态显示出来，而不是让管理员只看到一个成功的 task。
_DOCUMENT_DELIVERY_STATUS_LABEL: dict[str, str] = {
    "pending": "排队中",
    "processing": "处理中",
    "succeeded": "成功",
    "uncertain": "结果不明（需人工核实）",
    "failed": "失败",
}

# 文档投递原因码 → 中文。未知取值仍由 ``_display_or_unregistered`` 保留原码并标记，
# 与任务失败码采用同一条白名单展示纪律；原因码本身不含用户正文或外部标识。
_DOCUMENT_DELIVERY_REASON_LABEL: dict[str, str] = {
    "attempts_exhausted": "重试次数耗尽",
    "pending_expired_unconsumed": "排队超时未被消费",
    "permission_not_confirmed": "授权结果未能读回确认",
    "unsupported_nested_blocks": "正文含暂不支持的嵌套结构",
}


def _display_or_unregistered(value: str, table: dict[str, str]) -> str:
    """未登记的机器码既不原样吞掉、也不假装认识——统一回退成"原值（未登记
    显示名）"这个样式（Trace #469 修复包 B，B-6，产品负责人裁定的兜底样式）：
    管理员至少能看到原始取值用于排查/反馈，同时明确知道这是词表遗漏而不是
    真的没有这个状态，不会误以为系统坏了。"""

    label = table.get(value)
    if label is None:
        return f"{value}（未登记显示名）"
    return label


def _render_user_status(
    identifier: str, status: AdminUserStatusView | None, *, display_names: AdminDisplayNames
) -> str:
    """``/admin user`` 的文本回复（与管理卡并存，见 ``_dispatch`` 调用点）。

    Trace #469 S-1 起，查到用户时头部一律显示 ``display_names.user_label``
    解析出的「姓名（邮箱）」，不再回显管理员自己输入的标识——即使那是他自己
    刚打进来的 ``open_id``，也必须满足"管理员可见文案零 ou_"这条结构性要求
    （见 :data:`_INTERNAL_ID_PREFIXES` 上方注释）。查无记录时退回
    :func:`_safe_identifier_echo`：非内部 ID 形状的输入原样回显（多数是邮箱，
    帮助管理员核对是不是打错了），内部 ID 形状则退化为通用占位。
    """

    if status is None:
        return f"未找到标识为 {_safe_identifier_echo(identifier)} 的用户记录。"
    label = display_names.user_label(open_id=status.identifier)
    return (
        f"用户 {label}：\n"
        f"开通状态：{_PROVISIONING_STATE_LABEL.get(status.provisioning_state, status.provisioning_state)}\n"
        f"账号状态：{_ACCOUNT_STATE_LABEL.get(status.account_state, status.account_state)}\n"
        f"权限版本：{status.permission_version}\n"
        f"更新时间：{status.updated_at}\n"
        f"银河来源：{_render_galaxy_source(status.galaxy_source, display_names=display_names)}\n"
        f"当前生效本地覆盖：\n{_render_local_overrides(status.local_overrides, display_names=display_names)}"
    )


def _render_audit_query(
    identifier: str | None, window_hours: int, events: Sequence[AdminEventView]
) -> str:
    """``identifier`` 已经是 :meth:`AdminQueries.resolve_identifier` 反查过的
    结果（``_dispatch`` 调用点传入 ``resolved_audit_identifier``）——邮箱形态的
    输入反查失败时原样是那个邮箱，反查成功或管理员直接输入 open_id 时可能是
    open_id。这里不做一次额外的用户资料查找（审计查询是高频诊断动作，多一次
    DB 往返成本不值得）：非内部 ID 形状的值（多数是邮箱）原样展示，内部 ID
    形状（``ou_``/``lpo_``/``pac_``）退化为通用占位——满足"管理员可见文案零
    ou_"这条结构性要求（Trace #469 S-1），代价是 open_id 场景下不显示姓名，
    这与 ``_render_user_status`` 会经 ``display_names.user_label`` 完整翻译不
    同（那里已经确认这是一个真实存在的用户，多一次查找换来更好的可读性；这里
    只是一次事件列表查询，不需要为了展示效果额外查一次 ``app_user``）。
    """

    scope = f"标识 {_safe_identifier_echo(identifier)} 的" if identifier else ""
    if not events:
        return f"最近 {window_hours} 小时内没有找到{scope}相关事件。"
    header = f"最近 {window_hours} 小时内{scope}最近事件（{len(events)} 条）："
    lines = [
        f"- {event.received_at} {event.event_type} → "
        f"{event.handled_as or '(未标记)'}（追溯号 {event.trace_id}）"
        for event in events
    ]
    return "\n".join((header, *lines))


def _render_trace(trace_id: str, trace: AdminTraceView | None) -> str:
    """``/admin trace`` 的回显（Issue #337 范围条目 4）：

    - ``trace`` 为 ``None``（``inbound_event`` 里查无这个追溯号）→ 明确的
      「不存在」文案，不是空白也不是报错。
    - ``trace`` 非空但 ``failure_reason`` 为空 → 如实回「无失败记录」并带上
      当前能查到的开通状态（如果定位得到用户的话）——不能因为没有失败原因
      就假装这条追溯号也查无此人。
    - ``failure_reason`` 非空 → 这正是 Issue #337 的验收关键：管理员能凭追溯号
      拿到此前只能靠检索容器日志才能拿到的答案。
    """

    if trace is None:
        return f"追溯号 {trace_id}：查无此追溯号"

    lines = [
        f"追溯号 {trace_id}：{trace.event_count} 条入站事件",
        f"首次接收: {trace.first_received_at}",
        # 事件类型/处理方式机器码 → 中文（Trace #469 修复包 B，B-6）：此前
        # 直出 im.message.receive_v1/not_provisioned 这类内部枚举取值。
        f"最近事件类型: {_display_or_unregistered(trace.last_event_type, _EVENT_TYPE_LABEL)}",
        f"最近处理方式: "
        f"{_display_or_unregistered(trace.last_handled_as, _HANDLED_AS_LABEL) if trace.last_handled_as else '(未标记)'}",
        f"是否已认领: {'是' if trace.dispatched else '否'}",
    ]
    if trace.provisioning_state is not None:
        # 英文状态码 → 中文（Trace #469 S-1 TOP-6），复用 _render_user_status
        # 同一份词表，不允许两处出现不同翻译。
        lines.append(
            f"开通状态: {_PROVISIONING_STATE_LABEL.get(trace.provisioning_state, trace.provisioning_state)}"
        )
        lines.append(
            f"账号状态: {_ACCOUNT_STATE_LABEL.get(trace.account_state, trace.account_state)}"
        )
    if trace.failure_reason is not None:
        # 失败原因机器码 → 中文（Trace #469 修复包 B，B-6）：此前直出
        # role_revoked 这类内部原因码。
        lines.append(
            f"失败原因: {_display_or_unregistered(trace.failure_reason, _FAILURE_REASON_LABEL)}"
            f"（{trace.failure_event_type}，{trace.failure_occurred_at}）"
        )
    else:
        lines.append("无开通失败记录")
    if trace.task_status is not None:
        # 任务收口结果（Issue #495）：这条追溯号派生的任务失败时，管理员此前
        # 唯一能拿到的是「无失败记录」——开通没失败，问数任务失败了，而任务
        # 那一侧的分类码与失败签名只进 worker 容器 stderr，管理员看不到。
        # 迁移 0080 落库之后这里才有东西可显示；没有派生任务时整段省略，不摆
        # 一排空值。
        suffix = f"（{trace.task_ended_at}）" if trace.task_ended_at is not None else ""
        lines.append(
            f"任务结果: {_display_or_unregistered(trace.task_status, _TASK_STATUS_LABEL)}{suffix}"
        )
        # 有细分失败码用它，否则退回 `error_kind`：没有经过 `write_terminal_
        # event` 的失败终态（心跳超时回收、投递到期、排队超时）在新列上恒为
        # NULL，只有 `error_kind` 说得出原因，不能因此整行消失。
        task_failure = trace.task_failure_code or trace.task_error_kind
        if task_failure is not None:
            lines.append(
                f"任务失败原因: {_display_or_unregistered(task_failure, _TASK_FAILURE_LABEL)}"
            )
        if trace.task_failure_signature is not None:
            # 通常是底层异常**类型名**，不是异常正文；结构化外因也可使用固定分类
            # 签名（例如 `mcp.query.http_502`），同样不是自由文本（`V-花名册-33`：
            # 审计与日志不含外部标识原值；psycopg 的异常串常见形状 `DETAIL: Key
            # (feishu_open_id)=(ou_...)`）。这里不翻译——它是稳定的低敏标识，没有
            # 可枚举的取值域，翻译只能靠猜；管理员把它原样贴给研发就是最有用的一手
            # 信息。
            signature_label = (
                "失败签名" if trace.task_failure_code == "mcp_bad_gateway" else "底层异常类型"
            )
            lines.append(f"{signature_label}: {trace.task_failure_signature}")
    if trace.document_delivery_status is not None:
        # 文档投递是 task 收口之后由 gateway 独立消费循环完成的另一条状态机。
        # 因此不能把 task.status == succeeded 当作文档已成功；尤其 #499 的降级
        # 事实只存在检查点列里，必须在同一条 trace 回显中明确区分。
        lines.append(
            "文档交付结果: "
            + _display_or_unregistered(
                trace.document_delivery_status, _DOCUMENT_DELIVERY_STATUS_LABEL
            )
        )
        if trace.document_delivery_last_error is not None:
            lines.append(
                "文档交付原因: "
                + _display_or_unregistered(
                    trace.document_delivery_last_error, _DOCUMENT_DELIVERY_REASON_LABEL
                )
            )
        if trace.document_body_degraded_reason is not None:
            lines.append(
                "文档正文处理: 已降级（"
                + _display_or_unregistered(
                    trace.document_body_degraded_reason, _DOCUMENT_DELIVERY_REASON_LABEL
                )
                + "，已回退纯文本段落路径）"
            )
    return "\n".join(lines)


#: 「以 ``/admin`` 开头但没解析成功」时，按失败落点告诉管理员**哪一段**没看懂
#: （Issue #492 完成标准 4）。
#:
#: 缺陷现场：产品负责人 2026-08-31 连发三条管理命令，三条都只收到一句"未识别的
#: 管理命令，请发送 /admin help 查看可用命令"——这句话不含任何可据以修正的信息，
#: 他无从自救，也无法判断是邮箱被客户端自动链接化了（Issue #492 假设 1）还是公司
#: 那一段填了中文名（假设 2）。两种情形此前产生**逐字相同**的回复。
#:
#: **刻意不回显管理员输入的原文**：回显最直观，但出站是一条飞书文本消息，而飞书
#: 文本消息里的 ``<at user_id="all"></at>`` 一类标记是有语义的——把输入原样拼进
#: 回复等于把一段可控文本反射进出站消息。段名 + 期望形状已经足够自救，不值得为
#: 这点便利开一个反射面。
_REJECT_HINTS: dict[AdminRejectReason, str] = {
    AdminRejectReason.UNKNOWN_SUBCOMMAND: "没有认出命令名",
    AdminRejectReason.WRONG_ARGUMENT_COUNT: "参数个数与这条命令的格式对不上",
    AdminRejectReason.BAD_IDENTIFIER: (
        "没有认出用户标识（命令里的第 1 个参数）——这一段请填用户邮箱或 open_id，中间不要有空格"
    ),
    AdminRejectReason.BAD_COMPANY_ID: (
        "没有认出公司标识（命令里的第 2 个参数）——这一段要填公司编号，不是公司中文名称"
    ),
    AdminRejectReason.BAD_METRIC_NAME: (
        "没有认出指标（命令里的第 3 个参数）——这一段请填指标名或已配置的中文别名，中间不要有空格"
    ),
    AdminRejectReason.BAD_REASON: "没有看懂原因（命令里的最后一段）——原因不能为空，且不超过 500 字",
    AdminRejectReason.BAD_WINDOW_HOURS: "没有看懂小时数——这一段请填 1 到 720 之间的整数",
    AdminRejectReason.BAD_TRACE_ID: "没有认出追溯号——这一段请填完整的 26 位追溯号，不要带前缀",
}

#: 闲聊得到的既有笼统文案键（#492 完成标准 3，正文逐字不变；#521 F4-3 把它移进
#: ``config/content.toml`` 的版本化目录）。管理命令面**没有 ``/admin`` 前缀预检**，
#: 管理员的任何一句闲聊都会走到 UNKNOWN；对它们做分段报错等于对每句闲聊解释语法。
_UNKNOWN_COMMAND_KEY = "admin.unknown_command"
#: 已判定出"哪一段没看懂"时的分段报错键。
_UNKNOWN_COMMAND_DETAIL_KEY = "admin.unknown_command_detail"


def _render_unknown(
    reject_reason: AdminRejectReason | None,
    segment_count: int,
    catalog: ContentCatalog | None = None,
) -> RenderedContent:
    """``UNKNOWN`` 的回复：说清哪一段没看懂 + 实际分成了几段参数（#521 F4-3）。

    ``segment_count`` 是管理员自救的关键事实——#492 那次，管理员发的是"一个邮箱
    + 24"两段、解析器数出三段；只有把这个数字说出来，才可能意识到"客户端把邮箱
    拆开了"，而不是反复重发同一条命令。它来自分段计数，**不回显任何输入原文**。
    """

    catalog = catalog if catalog is not None else default_content_catalog()
    hint = _REJECT_HINTS.get(reject_reason) if reject_reason is not None else None
    if hint is None:
        return catalog.text(_UNKNOWN_COMMAND_KEY)
    return catalog.text(
        _UNKNOWN_COMMAND_DETAIL_KEY, hint=hint, segment_count=segment_count
    )
