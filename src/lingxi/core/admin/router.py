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
from typing import Sequence

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
                    reply_text=render_help(roles),
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
                    reply_text=render_user_status(
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
                    reply_text=render_audit_query(resolved_audit_identifier, window_hours, events),
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
                    reply_text=render_trace(command.identifier, trace),
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
                resolved_open_id = self._queries.resolve_identifier(identifier=command.identifier)
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
        rendered = render_unknown(command.reject_reason, shapes.argument_count)
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
        """``suspend``/``resume``/``grant_position``/``revoke_permission``
        共用的写命令编排：角色核对 → 自我目标防呆 →
        ``prepare_action``（只建待确认操作，不改业务状态）→ 发送确认卡片 →
        回复管理员"已生成待确认操作，请查收卡片"。真正的业务变更只发生在管理员
        本人点击卡片之后，见 ``core/admin/card_callback.AdminCardCallbackHandler``。

        ``company_id``/``metric_name``/``reason`` 三个参数（#319 S-P-1b 新增）自
        Trace #544 D-5 起只剩 ``LOCAL_PERMISSION_REVOKE`` 形状 2 会传：
        ``LOCAL_PERMISSION_GRANT`` 现在只由管理卡「职位×公司范围」表单发起，传
        ``position_name``/``company_scope``/``reason``；``LOCAL_PERMISSION_
        SUPPRESS`` 已经没有任何调用方（``/admin suppress_permission`` 撤除后没有
        第二个入口，历史行仍由确认/通知侧按类型渲染，见 ``_WRITE_ACTION_NAMES``
        与 ``core/admin/notification.py``）——``suspend``/``resume`` 三个都不传，
        保持既有调用形状不变。

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

        if (
            action_type in LOCAL_PERMISSION_ACTION_TYPES
            and target_identifier == entry.feishu_open_id
        ):
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
        except Exception as error:  # noqa: BLE001 - 管理卡发送失败不影响文本回复
            self._safe_record(
                "admin.command.management_card_send_failed",
                target=display_identifier,
                error=type(error).__name__,
                trace_id=trace_id,
            )
