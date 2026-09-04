"""管理命令面路由：身份判定 → 角色判定 → 命令解析 → 执行只读查询 → 审计。

只依赖注入的端口，不 import 适配器。端口协议与返回形状住在 ``router_ports``，文本回复
的渲染住在 ``router_render``。

**审计覆盖每一种结论**，含"未登记／已撤销／角色不全"的拒绝：产品合同「管理员处理入口与
安全确认」要求"每次管理查询和操作都记录"，门禁要求默认拒绝规则必须能用一个不在任何名单
中的未知对象证明；两者在这里合成同一条路径——拒绝分支与放行分支共用同一个审计出口，不是
只在"成功"时才留痕。

**审计器本身抛异常时，路由不得跟着崩溃或误放行**：每一次审计写入都包一层保护，失败时改记
一条安全的结构化日志，并让本次交互的结论**统一退化为确定性拒绝**——不区分原本这一分支是
打算拒绝还是打算放行，因为"记不上审计"本身已经违反了那条硬承诺。
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import Any

from lingxi.core.admin.commands import (
    AdminCommandKind,
    describe_admin_tokens,
    parse_admin_command,
)
from lingxi.core.admin.display_names import AdminDisplayNames
from lingxi.core.admin.pending_action import (
    LOCAL_PERMISSION_ACTION_TYPES,
    PendingActionType,
)
from lingxi.core.admin.registry import AdminRegistryEntry, is_authorized_admin
from lingxi.core.admin.router_ports import (
    AdminQueries,
    AdminRegistryLookup,
    AdminRouteOutcome,
    AdminRouter,
    AuditSink,
    ConfirmCardSender,
    ManagementCardSender,
    PendingActionPreparer,
)
from lingxi.core.admin.router_render import (
    render_audit_query,
    render_help,
    render_trace,
    render_unknown,
    render_user_status,
)
from lingxi.core.admin.views import (
    AdminEventView,
    AdminTraceView,
    AdminUserStatusView,
    LocalPermissionOverrideView,
)

logger = logging.getLogger(__name__)

#: 端口与返回形状搬到 ``router_ports``、文本渲染搬到 ``router_render``；旧 import 路径
#: 继续可用。
__all__ = [
    "DEFAULT_EVENT_LIMIT",
    "AdminCommandRouter",
    "AdminEventView",
    "AdminQueries",
    "AdminRegistryLookup",
    "AdminRouteOutcome",
    "AdminRouter",
    "AdminTraceView",
    "AdminUserStatusView",
    "AuditSink",
    "ConfirmCardSender",
    "LocalPermissionOverrideView",
    "ManagementCardSender",
    "PendingActionPreparer",
]


#: 追溯/审计查询单次最多回显的事件行数——"最近关键事件"的 MVP 承诺，不是完整审计
#: 检索；超出的历史需要更大范围的能力，留给未来入口。
DEFAULT_EVENT_LIMIT = 20

#: 本模块生成的回复不经过 ``config/content.toml`` 的版本化目录（那套治理面向需要
#: 产品评审的用户承诺文案；这里是仅对已登记管理员可见的操作性诊断输出，随查询内容
#: 动态变化，不适合模板变量的强匹配约束）。version 字段固定这个字面量，审计里能一眼
#: 区分"这条内容来自目录"还是"来自管理命令面"。
_CONTENT_VERSION = "internal"

#: 写命令 → 审计动作名，供写命令编排统一查表。
_WRITE_ACTION_NAMES: dict[PendingActionType, str] = {
    PendingActionType.SUSPEND_USER: "admin.command.suspend_user",
    PendingActionType.RESUME_USER: "admin.command.resume_user",
    PendingActionType.LOCAL_PERMISSION_GRANT: "admin.command.grant_permission",
    PendingActionType.LOCAL_PERMISSION_SUPPRESS: "admin.command.suppress_permission",
    PendingActionType.LOCAL_PERMISSION_REVOKE: "admin.command.revoke_permission",
}

#: 自我目标防呆的拒绝码。不套用既有错误码表：那张表描述的是准备与确认链内部的判定
#: 分支，这里是在准备之前就能确定性拒绝的一条独立业务规则，新的拒绝原因就该有自己的
#: 字面量，不勉强套进不描述它的既有取值。
_SELF_TARGET_FORBIDDEN_CODE = "self_target_forbidden"

#: 按「标识+公司+指标」反查覆盖行时零命中或多命中歧义的拒绝码，取舍同上。
_OVERRIDE_NOT_FOUND_CODE = "override_not_found"


@dataclass(frozen=True)
class _Dispatch:
    """一次命令分派的公共上下文：谁发的、回复到哪、追溯号。"""

    entry: AdminRegistryEntry
    roles: tuple[str, ...]
    trace_id: str
    chat_id: str = ""
    thread_id: str | None = None
    message_id: str = ""
    origin_card_message_id: str | None = None


@dataclass(frozen=True)
class _WriteAction:
    """一次写命令的全部输入；只读命令用不到它。"""

    action_type: PendingActionType
    target_identifier: str
    company_id: str | None = None
    metric_name: str | None = None
    reason: str | None = None
    position_name: str | None = None
    company_scope: str | None = None
    origin_card_message_id: str | None = None


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
        """装配路由。

        ``display_names`` **必填**：全部文本回复端都经它翻译外部标识、公司编号与指标
        标识，不允许"未装配就原样回显内部标识"的兜底。

        待确认操作端与确认卡端可选、成对未装配时安全兜底：写命令仍会被识别，但回复
        「该功能当前不可用」，不假装已经创建了待确认操作。管理卡端未装配时用户查询只回
        文本、不发卡——它不是写路径，不需要与前两者成对存在。
        """
        self._registry = registry
        self._queries = queries
        self._audit = audit
        self._display_names = display_names
        self._pending_actions = pending_actions
        self._confirm_cards = confirm_cards
        self._management_cards = management_cards

    def _safe_record(self, action: str, /, **fields: object) -> bool:
        """给审计写入包一层保护：审计器本身抛异常不得让路由跟着崩溃。

        它的异常不能变成一次未处理的崩溃逃出去——那样调用方拿到的既不是「拒绝」也不是「放行」，
        破坏的是「判定或执行失败均失败关闭」这条更基础的承诺。失败时改用标准库日志留一条安全
        记录（不经过可能已经损坏的审计器），只带动作名与异常类型，不带任何业务取值。

        Returns:
            是否记成功。记不上时调用方必须把这次交互当成确定性拒绝。
        """
        try:
            self._audit.record(action, **fields)
            return True
        except Exception as error:
            logger.error(
                "admin.router.audit_failed original_action=%s error=%s",
                action,
                type(error).__name__,
            )
            return False

    def _record_or_reject(
        self, action: str, outcome: AdminRouteOutcome, /, **fields: object
    ) -> AdminRouteOutcome:
        """记一条审计；记成功才放行给定结论，否则退化为确定性拒绝。

        **不区分**原本这条分支是打算拒绝还是打算放行：记不上审计本身就违反了"每次管理
        查询和操作都记录"这条硬承诺，让一次本该有记录的放行悄悄发生，比多拒绝一次更糟。
        """
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
        """判定发送者是否为当前有效管理员，是则解析并执行命令。

        已确认是管理员之后的执行失败必须给出明确的错误回复，不能沉默；这条承诺不得被
        审计器自身的异常打破。``chat_id``/``thread_id``/``message_id`` 只有写命令用得到
        ——确认卡片必须作为触发这条命令的那条消息的回复发出；``origin_card_message_id``
        只在命令由管理卡转译而来时非空。
        """
        resolved = self._resolve_admin(open_id, trace_id=trace_id)
        if isinstance(resolved, AdminRouteOutcome):
            return resolved
        context = _Dispatch(
            entry=resolved,
            roles=tuple(sorted(role.value for role in resolved.roles)),
            trace_id=trace_id,
            chat_id=chat_id,
            thread_id=thread_id,
            message_id=message_id,
            origin_card_message_id=origin_card_message_id,
        )
        try:
            return self._dispatch(parse_admin_command(text), text, context)
        except Exception as error:
            return self._record_or_reject(
                "admin.command.internal_error",
                AdminRouteOutcome(
                    handled=True,
                    content_key="admin.internal_error",
                    content_version=_CONTENT_VERSION,
                    reply_text="本次管理命令处理失败，请稍后重试。",
                ),
                actor=resolved.feishu_open_id,
                error=type(error).__name__,
                trace_id=trace_id,
            )

    def _resolve_admin(
        self, open_id: str, *, trace_id: str
    ) -> AdminRegistryEntry | AdminRouteOutcome:
        """**每次调用都发起一次新的登记表读取**，不持有也不复用任何此前的判定结果。

        这是"角色收回后新请求立即拒绝"在应用层的唯一落点。判定失败按"不是管理员"处理，
        与真实的未登记者得到同一条回落，不额外暴露"内部出错了"这类信号。未登记、已撤销、
        角色不全同样**不区分**——不给探测者提供"你快登记成功了"一类的可利用信号。

        Returns:
            通过时是登记条目；否则是直接可以返回给调用方的拒绝结论。
        """
        try:
            entry = self._registry.active_entry(open_id=open_id)
        except Exception as error:
            return self._record_or_reject(
                "admin.command.lookup_failed",
                AdminRouteOutcome(handled=False),
                error=type(error).__name__,
                trace_id=trace_id,
            )
        if not is_authorized_admin(entry):
            return self._record_or_reject(
                "admin.command.rejected",
                AdminRouteOutcome(handled=False),
                reason="not_authorized",
                trace_id=trace_id,
            )
        assert entry is not None  # is_authorized_admin 已确认非空
        return entry

    def _dispatch(self, command: Any, text: str, ctx: _Dispatch) -> AdminRouteOutcome:
        """按命令种类分派。语法已经在解析层钉死为有限几种，这里只负责各自的执行与回复。"""
        if command.kind is AdminCommandKind.HELP:
            return self._reply("admin.command.help", "admin.help", render_help(ctx.roles), ctx)
        if command.kind is AdminCommandKind.QUERY_USER:
            return self._query_user(command, ctx)
        if command.kind is AdminCommandKind.QUERY_AUDIT:
            return self._query_audit(command, ctx)
        if command.kind is AdminCommandKind.QUERY_TRACE:
            return self._query_trace(command, ctx)
        if command.kind is AdminCommandKind.SUSPEND_USER:
            return self._dispatch_write_action(
                self._user_action(command, PendingActionType.SUSPEND_USER), ctx
            )
        if command.kind is AdminCommandKind.RESUME_USER:
            return self._dispatch_write_action(
                self._user_action(command, PendingActionType.RESUME_USER), ctx
            )
        if command.kind is AdminCommandKind.GRANT_POSITION_PERMISSION:
            return self._grant_position(command, ctx)
        if command.kind is AdminCommandKind.REVOKE_PERMISSION:
            return self._revoke_permission(command, ctx)
        return self._unknown(command, text, ctx)

    def _reply(
        self, action: str, content_key: str, reply_text: str, ctx: _Dispatch, **fields: object
    ) -> AdminRouteOutcome:
        """把一次成功的只读回复连同它的审计一起落下去。"""
        return self._record_or_reject(
            action,
            AdminRouteOutcome(
                handled=True,
                content_key=content_key,
                content_version=_CONTENT_VERSION,
                reply_text=reply_text,
            ),
            actor=ctx.entry.feishu_open_id,
            roles=list(ctx.roles),
            trace_id=ctx.trace_id,
            **fields,
        )

    def _query_user(self, command: Any, ctx: _Dispatch) -> AdminRouteOutcome:
        """``/admin user``：文本回复 ＋ 附带一张管理卡。

        管理卡与文本回复并存、不替代：卡片发送失败或未装配都不影响文本回复这条主路径。
        """
        assert command.identifier is not None
        resolved_identifier = self._queries.resolve_identifier(identifier=command.identifier)
        status = self._queries.user_status(identifier=resolved_identifier)
        if status is not None:
            self._send_management_card(
                status=status,
                display_identifier=command.identifier,
                initiated_by_open_id=ctx.entry.feishu_open_id,
                chat_id=ctx.chat_id,
                thread_id=ctx.thread_id,
                message_id=ctx.message_id,
                trace_id=ctx.trace_id,
            )
        return self._reply(
            "admin.command.query_user",
            "admin.user_status",
            render_user_status(command.identifier, status, display_names=self._display_names),
            ctx,
            target=command.identifier,
            found=status is not None,
        )

    def _query_audit(self, command: Any, ctx: _Dispatch) -> AdminRouteOutcome:
        """``/admin audit``：最近关键事件，按窗口回显有限条数。"""
        window_hours = command.window_hours or 24
        resolved = (
            self._queries.resolve_identifier(identifier=command.identifier)
            if command.identifier
            else None
        )
        events = self._queries.recent_events(
            identifier=resolved, window_hours=window_hours, limit=DEFAULT_EVENT_LIMIT
        )
        return self._reply(
            "admin.command.query_audit",
            "admin.audit_query",
            render_audit_query(resolved, window_hours, events),
            ctx,
            target=command.identifier,
            window_hours=window_hours,
            result_count=len(events),
        )

    def _query_trace(self, command: Any, ctx: _Dispatch) -> AdminRouteOutcome:
        """``/admin trace``：按追溯号回一条链的经过。

        渲染层不展示外部标识、公司或指标（只展示追溯号本身与状态翻译），因此不需要额外
        传入展示名端口。
        """
        assert command.identifier is not None
        trace = self._queries.trace_lookup(trace_id=command.identifier)
        return self._reply(
            "admin.command.query_trace",
            "admin.trace_query",
            render_trace(command.identifier, trace),
            ctx,
            target=command.identifier,
            found=trace is not None,
        )

    def _user_action(self, command: Any, action_type: PendingActionType) -> _WriteAction:
        """把一条按人操作的命令翻成写命令输入。"""
        assert command.identifier is not None
        return _WriteAction(
            action_type=action_type,
            target_identifier=self._queries.resolve_identifier(identifier=command.identifier),
        )

    def _grant_position(self, command: Any, ctx: _Dispatch) -> AdminRouteOutcome:
        """``职位 × 公司范围``的补充授权。"""
        assert command.identifier is not None
        assert command.position_name is not None
        assert command.company_scope is not None
        assert command.reason is not None
        return self._dispatch_write_action(
            _WriteAction(
                action_type=PendingActionType.LOCAL_PERMISSION_GRANT,
                target_identifier=self._queries.resolve_identifier(identifier=command.identifier),
                position_name=command.position_name,
                company_scope=command.company_scope,
                reason=command.reason,
                origin_card_message_id=ctx.origin_card_message_id,
            ),
            ctx,
        )

    def _revoke_permission(self, command: Any, ctx: _Dispatch) -> AdminRouteOutcome:
        """收回一条本地覆盖。

        两种输入形状：直接给覆盖行标识，或者给"人 + 公司 + 指标"反查。反查不到就直接
        回复、不产生任何待确认操作——这一步发生在准备之前（还没有标识可以喂给它），是
        一道独立的前置判断，不计入准备阶段的判定结论。

        这里**不**重复自我目标防呆：传下去的是覆盖行的内部标识而不是外部标识，与管理员
        自己的外部标识结构上不会相等，判断恒假、形同虚设。真正等价的防呆在准备阶段按
        覆盖行查到属主之后再核对——同语义、检查点位置不同。
        """
        assert command.identifier is not None
        assert command.reason is not None
        override_id = command.identifier
        if command.company_id is not None:
            assert command.metric_name is not None
            found = self._queries.resolve_override_id(
                open_id=self._queries.resolve_identifier(identifier=command.identifier),
                company_id=command.company_id,
                metric_name=self._queries.resolve_metric_name(metric_token=command.metric_name),
            )
            if found is None:
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
                    actor=ctx.entry.feishu_open_id,
                    roles=list(ctx.roles),
                    target=command.identifier,
                    code=_OVERRIDE_NOT_FOUND_CODE,
                    trace_id=ctx.trace_id,
                )
            override_id = found
        return self._dispatch_write_action(
            _WriteAction(
                action_type=PendingActionType.LOCAL_PERMISSION_REVOKE,
                target_identifier=override_id,
                reason=command.reason,
            ),
            ctx,
        )

    def _unknown(self, command: Any, text: str, ctx: _Dispatch) -> AdminRouteOutcome:
        """不认识的命令：回帮助或拒绝文案，绝不会被当成任何查询条件执行。

        拒绝原因只是一个枚举名、不含管理员输入的原文，记进审计是为了让下一次"真人踩到
        但现场取不到正文"的调查至少知道是哪一段没通过——曾经有一次调查正是卡在这里：
        三条失败只留下一个不带原因的"未知命令"，两个竞争假设至今无法区分。

        取证字段只对 ``/admin`` 开头的**命令尝试**留证，形状分类不含原文；闲聊一个字
        都不记。
        """
        shapes = describe_admin_tokens(text)
        forensics: dict[str, object] = {}
        if shapes.is_admin_prefixed:
            forensics = {
                "token_count": shapes.argument_count,
                "token_shapes": shapes.shape_summary,
                "raw_admin_text": shapes.raw_text,
            }
        rendered = render_unknown(command.reject_reason, shapes.argument_count)
        return self._record_or_reject(
            "admin.command.unknown",
            AdminRouteOutcome(
                handled=True,
                content_key=rendered.key,
                content_version=rendered.version,
                reply_text=rendered.text,
            ),
            actor=ctx.entry.feishu_open_id,
            roles=list(ctx.roles),
            reject_reason=(
                command.reject_reason.value if command.reject_reason is not None else None
            ),
            trace_id=ctx.trace_id,
            **forensics,
        )

    # ------------------------------------------------------------------
    # 写命令
    # ------------------------------------------------------------------

    def _dispatch_write_action(self, action: _WriteAction, ctx: _Dispatch) -> AdminRouteOutcome:
        """写命令的共用编排：角色核对 → 自我目标防呆 → 建待确认操作 → 发确认卡。

        真正的业务变更只发生在管理员本人点击卡片之后；这里最多调用一次准备、最多发一次
        卡片，任何一步不通过都只回复文案，不产生第二条待确认操作或第二张卡片。

        不在这里重复核对具体角色：入口的管理员判定已经要求三类角色全部为真，"已通过顶层
        判定却缺某个角色"在当前语义下结构上不可能成立，写一条测不到的分支就是死代码。
        真正的"准备与确认之间角色被撤销"防线在确认时刻重新判定——那一刻才是唯一有意义的
        重新核验点，因为两者之间存在真实的时间窗口。
        """
        action_name = _WRITE_ACTION_NAMES[action.action_type]
        blocked = self._reject_write_action(action, ctx, action_name)
        if blocked is not None:
            return blocked
        assert self._pending_actions is not None
        assert self._confirm_cards is not None

        outcome = self._pending_actions.prepare(**self._prepare_kwargs(action, ctx))  # type: ignore[arg-type]
        if not outcome.decision.ok or outcome.pending is None:
            return self._write_action_reply(
                action_name,
                "admin.write_action_rejected",
                outcome.decision.message,
                action,
                ctx,
                code=outcome.decision.code,
            )

        dispatch_result = self._confirm_cards.send(
            pending=outcome.pending,
            chat_id=ctx.chat_id,
            thread_id=ctx.thread_id,
            reply_to_message_id=ctx.message_id,
        )
        if not dispatch_result.delivered:
            return self._write_action_reply(
                action_name,
                "admin.write_action_card_send_failed",
                "确认卡片发送失败，本次操作不会执行，请稍后重试。",
                action,
                ctx,
                pending_action_id=outcome.pending.id,
            )
        return self._write_action_reply(
            action_name,
            "admin.write_action_pending",
            (
                "已提交，请在下方确认卡片上确认（10 分钟内有效）。"
                if action.position_name is not None
                else "已生成待确认操作，请查收你的飞书私聊确认卡片（十分钟内有效）。"
            ),
            action,
            ctx,
            pending_action_id=outcome.pending.id,
        )

    def _reject_write_action(
        self, action: _WriteAction, ctx: _Dispatch, action_name: str
    ) -> AdminRouteOutcome | None:
        """写命令的三道前置拒绝；都不产生任何待确认操作。

        自我目标防呆放在准备**之前**：这条拒绝不依赖端口是否已装配，不合法的意图不应该
        先创建一条记录再补救，而是从一开始就不让它进入准备的调用面。端口未装配时安全
        兜底，不假装已经创建了待确认操作。没有可回复的消息标识就没有地方挂载确认卡片，
        同样失败关闭。

        Returns:
            需要拒绝时的结论；可以继续时返回 ``None``。
        """
        if (
            action.action_type in LOCAL_PERMISSION_ACTION_TYPES
            and action.target_identifier == ctx.entry.feishu_open_id
        ):
            return self._write_action_reply(
                action_name,
                "admin.write_action_rejected",
                "不能对自己发起该操作。",
                action,
                ctx,
                code=_SELF_TARGET_FORBIDDEN_CODE,
            )
        if self._pending_actions is None or self._confirm_cards is None:
            return self._write_action_reply(
                action_name,
                "admin.write_action_unavailable",
                "该功能当前不可用，请联系技术支持。",
                action,
                ctx,
            )
        if not ctx.message_id:
            return self._write_action_reply(
                action_name,
                "admin.write_action_unavailable",
                "当前上下文无法发送确认卡片，请重新发送该命令。",
                action,
                ctx,
            )
        return None

    def _write_action_reply(
        self,
        action_name: str,
        content_key: str,
        reply_text: str,
        action: _WriteAction,
        ctx: _Dispatch,
        **fields: object,
    ) -> AdminRouteOutcome:
        """写命令的统一回复出口：文案、审计动作名与目标字段只有一份。"""
        return self._record_or_reject(
            action_name,
            AdminRouteOutcome(
                handled=True,
                content_key=content_key,
                content_version=_CONTENT_VERSION,
                reply_text=reply_text,
            ),
            actor=ctx.entry.feishu_open_id,
            roles=list(ctx.roles),
            target=action.target_identifier,
            trace_id=ctx.trace_id,
            **fields,
        )

    def _prepare_kwargs(self, action: _WriteAction, ctx: _Dispatch) -> dict[str, object]:
        """按端口实际接受的参数形状拼准备调用的入参。

        旧的注入式端口只接受公司与指标字段；职位表单才附加扩展参数，避免无关的旧测试
        因可选字段破坏接口兼容。来源卡片消息号只随授权（职位表单）动作下传——收回动作
        不引用管理卡行，否则会撞上不存在的外键。追溯号也只在端口确实接受它时才传。
        """
        kwargs: dict[str, object] = {
            "action_type": action.action_type,
            "target_open_id": action.target_identifier,
            "initiated_by_open_id": ctx.entry.feishu_open_id,
            "company_id": action.company_id,
            "metric_name": action.metric_name,
            "reason": action.reason,
        }
        if action.position_name is not None:
            kwargs["position_name"] = action.position_name
        if action.company_scope is not None:
            kwargs["company_scope"] = action.company_scope
        if action.origin_card_message_id is not None:
            kwargs["origin_card_message_id"] = action.origin_card_message_id
        if action.position_name is not None and self._accepts_trace_id():
            kwargs["trace_id"] = ctx.trace_id
        return kwargs

    def _accepts_trace_id(self) -> bool:
        """准备端口是否接受追溯号；签名读不出来时按不接受处理。"""
        assert self._pending_actions is not None
        try:
            parameters = inspect.signature(self._pending_actions.prepare).parameters
        except (TypeError, ValueError):
            return False
        return "trace_id" in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
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
        """用户查询附带发一张权限管理卡，尽力而为。

        管理卡**不是**一次待确认操作——它只是一张承载「查看 ＋ 发起」的交互卡，本身不改变
        任何状态。合同里"卡片只承担最终确认，不承担复杂信息填写"那条只约束**确认卡**；
        管理卡是它的前一步，专门承担复杂信息填写，两者是两张不同的卡。

        因此发送失败、或没有可回复的消息标识，都只记一条尽力而为的审计，不影响用户查询
        既有的文本回复这条主路径，也不让整次路由调用失败。
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
            # 旧的注入式实现没有发起人字段时保持兼容；生产实现会持久化真实发起人，
            # 供回调的身份校验与审计恢复。
            try:
                supports_initiator = (
                    "initiated_by_open_id"
                    in inspect.signature(self._management_cards.send).parameters
                )
            except (TypeError, ValueError):
                supports_initiator = False
            if supports_initiator:
                send_kwargs["initiated_by_open_id"] = initiated_by_open_id
            self._management_cards.send(
                **send_kwargs,
            )
        except Exception as error:
            self._safe_record(
                "admin.command.management_card_send_failed",
                target=display_identifier,
                error=type(error).__name__,
                trace_id=trace_id,
            )
