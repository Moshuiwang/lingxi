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
from lingxi.core.admin.registry import AdminRegistryEntry, is_authorized_admin
from lingxi.core.admin.views import AdminEventView, AdminUserStatusView

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
    不需要 import 具体类。"""

    def route(self, *, open_id: str, text: str, trace_id: str) -> AdminRouteOutcome: ...


#: 追溯/审计查询单次最多回显的事件行数——"最近关键事件"的 MVP 承诺，不是完整审计
#: 检索；超出的历史需要更大范围的能力，留给未来入口。
DEFAULT_EVENT_LIMIT = 20

#: 本模块生成的回复不经过 ``config/content.toml`` 的版本化目录（那套治理面向需要
#: 产品评审的用户承诺文案；这里是仅对已登记管理员可见的操作性诊断输出，随查询内容
#: 动态变化，不适合模板变量的强匹配约束）。version 字段固定这个字面量，审计里能一眼
#: 区分"这条内容来自目录"还是"来自管理命令面"。
_CONTENT_VERSION = "internal"


class AdminCommandRouter:
    """管理命令面的唯一入口。见模块与 :meth:`route` 文档。"""

    def __init__(
        self, *, registry: AdminRegistryLookup, queries: AdminQueries, audit: AuditSink
    ) -> None:
        self._registry = registry
        self._queries = queries
        self._audit = audit

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

    def route(self, *, open_id: str, text: str, trace_id: str) -> AdminRouteOutcome:
        """判定 ``open_id`` 是否为当前有效管理员，是则解析并执行命令。

        **每次调用都发起一次新的登记表读取**（通过注入的 ``registry``），不持有、
        不复用任何此前的判定结果——这是"角色收回后新请求立即拒绝"在应用层的唯一
        落点。判定或执行失败均失败关闭：判定失败按"不是管理员"处理（`handled=False`，
        与真实的未登记者得到同一条业务回落，不额外暴露"内部出错了"这类信号）；
        已确认是管理员之后的执行失败则必须给出明确的错误回复，不能沉默。

        以上两条承诺都不得被审计器自身的异常打破——见 :meth:`_record_or_reject`。
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
            return self._dispatch(entry=entry, text=text, trace_id=trace_id)
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
        self, *, entry: AdminRegistryEntry, text: str, trace_id: str
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

        # UNKNOWN：语法封闭的落点——不认识的命令得到帮助/拒绝文案，绝不会被当成
        # 任何查询条件执行（`commands.py` 已把语法钉死为四选一，这里只负责回复）。
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


def _render_help(roles: Sequence[str]) -> str:
    role_line = "、".join(roles) if roles else "(无)"
    return (
        "Lingxi 管理命令：\n"
        "/admin help — 显示本帮助\n"
        "/admin user <标识> — 查询用户开通状态\n"
        "/admin audit [标识] [小时数] — 查询最近事件（默认 24 小时）\n"
        f"当前角色：{role_line}"
    )


def _render_user_status(identifier: str, status: AdminUserStatusView | None) -> str:
    if status is None:
        return f"未找到标识为 {identifier} 的用户记录。"
    return (
        f"用户 {identifier}：\n"
        f"开通状态：{status.provisioning_state}\n"
        f"账号状态：{status.account_state}\n"
        f"权限版本：{status.permission_version}\n"
        f"更新时间：{status.updated_at}"
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
