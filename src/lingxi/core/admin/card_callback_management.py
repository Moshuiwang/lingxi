"""管理卡（职位+公司范围表单/撤销/取消）交互处理的独立责任面。

以 mixin 形式提供给 :class:`~lingxi.core.admin.card_callback.AdminCardCallbackHandler`，
与该类共享同一份 ``self`` 状态（在 ``AdminCardCallbackHandler.__init__`` 里统一
初始化），这里不重新声明 ``__init__``。搬到独立模块只是为了控制
``card_callback.py`` 的文件体量，不改变任何一个方法的语义边界——管理卡表单
提交、撤销、取消三类交互与确认/取消 pending action 的核心流程是两个可以
独立阅读的关注点，只共享同一批注入端口。
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any

from lingxi.core.admin.card_callback_ports import (
    _MANAGEMENT_CARD_GROUP_REVOKE_REASON,
    _MANAGEMENT_CARD_REVOKE_REASON,
    _WRITE_ACTION_PENDING_CONTENT_KEY,
    AuditSink,
    ManagementActionRouter,
    ManagementCardContextReader,
    ManagementCardRefresher,
    _ManagementContextCheck,
    _ManagementRouteOutcome,
    _toast_error,
    _toast_from_route_outcome,
)
from lingxi.core.admin.card_dispatch import ManagementCardContext, management_card_fingerprint
from lingxi.core.admin.management_card import ADMIN_ACTION_GRANT
from lingxi.core.admin.views import AdminUserStatusView


class _ManagementCardCallbackMixin:
    """管理卡表单提交/撤销/取消三类按钮交互，见模块文档。"""

    _management_actions: ManagementActionRouter | None
    _management_context_store: ManagementCardContextReader | None
    _management_state_lookup: Any
    _management_card_refresher: ManagementCardRefresher | None
    _audit: AuditSink

    def _check_management_action_and_context(
        self,
        *,
        operator_open_id: str,
        admin_action: str,
        identifier: str,
        message_id: str,
        trace_id: str,
    ) -> dict[str, Any] | _ManagementContextCheck:
        """校验动作路由是否装配、动作是否是 GRANT、卡片上下文是否可信。"""
        if self._management_actions is None:
            return _toast_error("该功能当前不可用，请改用文本命令")
        if admin_action != ADMIN_ACTION_GRANT:
            self._audit.record(
                "admin.card_callback.management_unknown_action",
                admin_action=admin_action,
                trace_id=trace_id,
            )
            return _toast_error("操作不存在或已失效")
        context_check = self._management_context(
            message_id=message_id,
            identifier=identifier,
            operator_open_id=operator_open_id,
            trace_id=trace_id,
        )
        if context_check.forbidden:
            return _toast_error("只有发起该管理卡的管理员本人可以操作")
        if context_check.stale:
            return _toast_error("数据已变化，请重新查询")
        return context_check

    def _resolve_management_form_prerequisites(
        self,
        *,
        operator_open_id: str,
        admin_action: str,
        identifier: str,
        company_id: str,
        metric_name: str,
        position_name: str,
        company_scope: str,
        message_id: str,
        trace_id: str,
    ) -> (
        dict[str, Any] | tuple[bool, str, ManagementCardContext | None, AdminUserStatusView | None]
    ):
        """校验表单提交的公共前置条件。

        成功时返回 ``(position_form, identifier, context, status)``。
        ``identifier`` 有上下文时一律改用上下文的值——卡片上下文是权威来源，
        绝不允许一个被篡改的隐藏 identifier 把管理卡动作重定向到另一个用户。
        """
        position_form = bool(position_name or company_scope)
        context_check = self._check_management_action_and_context(
            operator_open_id=operator_open_id,
            admin_action=admin_action,
            identifier=identifier,
            message_id=message_id,
            trace_id=trace_id,
        )
        if isinstance(context_check, dict):
            return context_check
        context = context_check.context
        if context is not None:
            identifier = context.identifier
        if not identifier:
            self._audit.record(
                "admin.card_callback.management_missing_identifier",
                admin_action=admin_action,
                trace_id=trace_id,
            )
            return _toast_error("未识别到目标用户标识，请重新查询 /admin user 后再操作")
        if not position_form and not company_id and not metric_name:
            # 常规路径不会走到这里：三个字段都是 required=True，飞书客户端会
            # 先拦住空提交。真正会走到这里的是"表单值整体没到服务端"这一类
            # 形态——这时候无法判断这是哪一张表单，因此给一句对两张卡都成立
            # 的话，并留一条可检索的审计，不新造可能指错卡的措辞。
            self._audit.record(
                "admin.card_callback.management_empty_form",
                admin_action=admin_action,
                trace_id=trace_id,
            )
            return _toast_error("没有收到你在卡片上的选择，请重新选择后再提交")
        return position_form, identifier, context, context_check.status

    def _build_position_grant_command(
        self, *, identifier: str, position_name: str, company_scope: str, reason: str, trace_id: str
    ) -> dict[str, Any] | str:
        """校验职位+范围表单字段，成功时返回等价 ``/admin`` 命令文本。"""
        if not reason.strip():
            return _toast_error("请填写原因")
        if not position_name.strip():
            return _toast_error("请选择银河职位")
        if not company_scope.strip():
            return _toast_error("请选择公司范围")
        if any(ch.isspace() for ch in position_name) or any(ch.isspace() for ch in company_scope):
            return _toast_error("职位或公司范围无效，请重新选择")
        if any(ch.isspace() for ch in identifier):
            # 当前 identifier 一律来自服务端管理卡上下文，结构上不可达；
            # 不假设未来的调用点也一样受控，纵深加固保留。
            self._audit.record(
                "admin.card_callback.management_missing_identifier", trace_id=trace_id
            )
            return _toast_error("未识别到目标用户标识，请重新查询 /admin user 后再操作")
        return (
            f"/admin grant_position {identifier} {position_name} {company_scope} {reason.strip()}"
        )

    def handle_management_form_submit(
        self,
        *,
        operator_open_id: str,
        admin_action: str,
        identifier: str,
        company_id: str,
        metric_name: str,
        reason: str,
        chat_id: str,
        thread_id: str | None,
        message_id: str,
        trace_id: str,
        position_name: str = "",
        company_scope: str = "",
    ) -> dict[str, Any]:
        """管理卡「职位+公司范围补充授权」表单提交。

        参数是已经从飞书表单回传值解析出来的干净字段（原始事件体解析是
        gateway 接线层的职责）。任何一个必填字段为空都在这里直接拒绝，给出
        "请选择/填写哪一项"这种更具体的 toast，而不是笼统的"未识别的管理
        命令"。
        """
        prerequisites = self._resolve_management_form_prerequisites(
            operator_open_id=operator_open_id,
            admin_action=admin_action,
            identifier=identifier,
            company_id=company_id,
            metric_name=metric_name,
            position_name=position_name,
            company_scope=company_scope,
            message_id=message_id,
            trace_id=trace_id,
        )
        if isinstance(prerequisites, dict):
            return prerequisites
        position_form, identifier, context, current_status = prerequisites
        if not position_form:
            return self._legacy_management_form_unavailable(
                admin_action=admin_action, trace_id=trace_id
            )
        return self._submit_position_grant_form(
            identifier=identifier,
            position_name=position_name,
            company_scope=company_scope,
            reason=reason,
            operator_open_id=operator_open_id,
            trace_id=trace_id,
            chat_id=chat_id,
            thread_id=thread_id,
            message_id=message_id,
            context=context,
            current_status=current_status,
        )

    def _submit_position_grant_form(
        self,
        *,
        identifier: str,
        position_name: str,
        company_scope: str,
        reason: str,
        operator_open_id: str,
        trace_id: str,
        chat_id: str,
        thread_id: str | None,
        message_id: str,
        context: ManagementCardContext | None,
        current_status: AdminUserStatusView | None,
    ) -> dict[str, Any]:
        """校验并提交职位+范围授权表单。

        是 :meth:`handle_management_form_submit` 位于 ``position_form`` 为真
        那一支的具体实现。
        """
        command = self._build_position_grant_command(
            identifier=identifier,
            position_name=position_name,
            company_scope=company_scope,
            reason=reason,
            trace_id=trace_id,
        )
        if isinstance(command, dict):
            return command
        return self._submit_management_command(
            command=command,
            operator_open_id=operator_open_id,
            trace_id=trace_id,
            chat_id=chat_id,
            thread_id=thread_id,
            message_id=message_id,
            context=context,
            current_status=current_status,
        )

    def _legacy_management_form_unavailable(
        self, *, admin_action: str, trace_id: str
    ) -> dict[str, Any]:
        """「公司×指标」表单入口已撤除的兼容分支。

        那两条文本命令的语法层只校验字符集、不核对指标目录，目录外的公司与
        指标能一路走进正式发布链。补充授权统一走「银河职位×公司范围」表单。
        生产的管理卡只渲染职位×范围表单，这条分支正常不可达，保留它是为了
        让历史卡片上残留的旧按钮得到一句明确的话；审计留一条可检索的动作名。
        """
        self._audit.record(
            "admin.card_callback.management_retired_form",
            admin_action=admin_action,
            trace_id=trace_id,
        )
        return _toast_error("该入口已下线，请重新发送 /admin user 后用新卡片的「补充授权」表单")

    def _submit_management_command(
        self,
        *,
        command: str,
        operator_open_id: str,
        trace_id: str,
        chat_id: str,
        thread_id: str | None,
        message_id: str,
        context: ManagementCardContext | None,
        current_status: AdminUserStatusView | None,
    ) -> dict[str, Any]:
        """把已经校验通过的等价 ``/admin`` 命令交给 router。

        成功进入待确认态时顺带把原管理卡标记为已提交。
        """
        outcome = self._route_management_action(
            operator_open_id=operator_open_id,
            text=command,
            trace_id=trace_id,
            chat_id=chat_id,
            thread_id=thread_id,
            message_id=message_id,
        )
        if outcome.handled and outcome.content_key == _WRITE_ACTION_PENDING_CONTENT_KEY:
            self._mark_management_submitted(
                context=context,
                status=current_status,
                message_id=message_id,
                trace_id=trace_id,
            )
        return _toast_from_route_outcome(outcome)

    def _route_management_action(
        self,
        *,
        operator_open_id: str,
        text: str,
        trace_id: str,
        chat_id: str,
        thread_id: str | None,
        message_id: str,
    ) -> _ManagementRouteOutcome:
        """Route a management-card action and preserve old injected fakes.

        The production router accepts ``origin_card_message_id`` so the pending
        confirmation can link back to the management card. Historical test/plugin
        routers may expose an older signature; introspection keeps those callers
        source-compatible while the real router still receives the reverse link.
        """
        if self._management_actions is None:  # guarded by each public caller
            raise RuntimeError("管理卡动作路由未装配")
        kwargs: dict[str, object] = {
            "open_id": operator_open_id,
            "text": text,
            "trace_id": trace_id,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "message_id": message_id,
        }
        try:
            parameters = inspect.signature(self._management_actions.route).parameters
            supports_origin = "origin_card_message_id" in parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
            )
        except (TypeError, ValueError):
            supports_origin = False
        if supports_origin:
            kwargs["origin_card_message_id"] = message_id or None
        return self._management_actions.route(**kwargs)  # type: ignore[arg-type]

    def _lookup_management_context(
        self, *, message_id: str, trace_id: str
    ) -> tuple[ManagementCardContext | None, _ManagementContextCheck | None]:
        """读一次持久化的管理卡上下文；找不到或读失败时直接给出早退结果。

        没有持久绑定（伪造的 message id、迁移前的旧卡、已被清理的保留行）一律
        失败关闭，绝不信任隐藏 identifier 而放行一次写操作。
        """
        store = self._management_context_store
        if store is None or not message_id:
            return None, _ManagementContextCheck(context=None, status=None)
        try:
            context = store.lookup_context(message_id=message_id)
        except Exception as error:  # 读上下文失败不得放行
            self._audit.record(
                "admin.card_callback.management_context_lookup_failed",
                error=type(error).__name__,
                trace_id=trace_id,
            )
            return None, _ManagementContextCheck(context=None, status=None, stale=True)
        if context is None:
            self._audit.record("admin.card_callback.management_context_missing", trace_id=trace_id)
            return None, _ManagementContextCheck(context=None, status=None, stale=True)
        return context, None

    def _check_management_context_ownership(
        self,
        context: ManagementCardContext,
        *,
        identifier: str,
        operator_open_id: str,
        trace_id: str,
    ) -> _ManagementContextCheck | None:
        """核对这次点击的操作人/目标标识是否与卡片持久绑定一致。

        以及卡片是否仍处于可操作状态。
        """
        if context.initiated_by_open_id and context.initiated_by_open_id != operator_open_id:
            self._audit.record("admin.card_callback.management_not_initiator", trace_id=trace_id)
            return _ManagementContextCheck(context=context, status=None, forbidden=True)
        if identifier and identifier != context.identifier:
            self._audit.record(
                "admin.card_callback.management_identifier_mismatch", trace_id=trace_id
            )
            return _ManagementContextCheck(context=context, status=None, forbidden=True)
        if context.state in {"closed", "submitted", "dispatching"}:
            # 已关闭的卡不得因为一次过期回调重放而重新变成写入口；一次表单
            # 提交已经产生确认卡后，重复投递不得再创建第二个逻辑操作。终态
            # 生效/不完整仍保持可操作——渲染层会对着最新快照恢复表单。
            self._audit.record(
                "admin.card_callback.management_context_not_actionable",
                trace_id=trace_id,
                state=context.state,
            )
            return _ManagementContextCheck(context=context, status=None, stale=True)
        return None

    def _close_stale_management_card(
        self,
        context: ManagementCardContext,
        *,
        status: AdminUserStatusView,
        message_id: str,
        fingerprint: str,
        trace_id: str,
    ) -> None:
        """快照已过期/已变化时尽力把原卡刷新到最新状态。

        无论成败都不再对着旧快照继续写。
        """
        store = self._management_context_store
        assert store is not None
        try:
            refreshed_context = (
                store.update_state(
                    message_id=message_id,
                    state="closed",
                    dispatch_status="idle",
                    snapshot_fingerprint=fingerprint,
                    last_trace_id=trace_id,
                )
                or context
            )
        except Exception as error:  # card refresh is best effort
            self._audit.record(
                "admin.card_callback.management_context_close_failed",
                error=type(error).__name__,
                trace_id=trace_id,
            )
            refreshed_context = context
        self._refresh_management_card(
            context=refreshed_context,
            status=status,
            state="closed",
            status_message="数据已变化，请重新查询",
            trace_id=trace_id,
        )

    def _resolve_management_status_and_staleness(
        self, context: ManagementCardContext, *, message_id: str, trace_id: str
    ) -> _ManagementContextCheck:
        """读一次目标用户最新状态并判断这份快照是否已经过期或漂移。

        调用前提是卡片已确认仍归当前操作人所有。
        """
        expired = context.context_deadline_at <= datetime.now(UTC)
        if expired:
            self._audit.record("admin.card_callback.management_context_expired", trace_id=trace_id)
        status = None
        if self._management_state_lookup is not None:
            try:
                status = self._management_state_lookup(context.identifier)
            except Exception as error:  # fail closed on state read errors
                self._audit.record(
                    "admin.card_callback.management_state_lookup_failed",
                    error=type(error).__name__,
                    trace_id=trace_id,
                )
                return _ManagementContextCheck(context=context, status=None, stale=True)
            if status is None:
                return _ManagementContextCheck(context=context, status=None, stale=True)
            fingerprint = management_card_fingerprint(status)
            if expired or (
                context.snapshot_fingerprint and fingerprint != context.snapshot_fingerprint
            ):
                self._close_stale_management_card(
                    context,
                    status=status,
                    message_id=message_id,
                    fingerprint=fingerprint,
                    trace_id=trace_id,
                )
                return _ManagementContextCheck(context=context, status=status, stale=True)
        if expired:
            return _ManagementContextCheck(context=context, status=status, stale=True)
        return _ManagementContextCheck(context=context, status=status)

    def _management_context(
        self,
        *,
        message_id: str,
        identifier: str,
        operator_open_id: str,
        trace_id: str,
    ) -> _ManagementContextCheck:
        """读取管理卡上下文并做回调时的懒快照校验。

        返回 ``stale=True`` 时调用方不得继续构造写命令；刷新动作已尽力完成，管理员
        需要重新查询以获得新的卡片。没有上下文（旧卡/已过保留窗口）同样失败关闭，
        但不把它当成数据发生变化来误导用户。
        """
        context, early_result = self._lookup_management_context(
            message_id=message_id, trace_id=trace_id
        )
        if early_result is not None:
            return early_result
        assert context is not None
        ownership_issue = self._check_management_context_ownership(
            context, identifier=identifier, operator_open_id=operator_open_id, trace_id=trace_id
        )
        if ownership_issue is not None:
            return ownership_issue
        return self._resolve_management_status_and_staleness(
            context, message_id=message_id, trace_id=trace_id
        )

    def _mark_management_submitted(
        self,
        *,
        context: ManagementCardContext | None,
        status: AdminUserStatusView | None,
        message_id: str,
        trace_id: str,
    ) -> None:
        if context is None or self._management_context_store is None:
            return
        try:
            updated = self._management_context_store.update_state(
                message_id=message_id,
                state="submitted",
                dispatch_status="publishing",
                last_trace_id=trace_id,
            )
            if updated is not None and status is not None:
                self._refresh_management_card(
                    context=updated,
                    status=status,
                    state="submitted",
                    dispatch_status="publishing",
                    trace_id=trace_id,
                )
        except Exception as error:  # database/card update is best effort
            self._audit.record(
                "admin.card_callback.management_card_refresh_failed",
                error=type(error).__name__,
                trace_id=trace_id,
            )

    def _refresh_management_card(
        self,
        *,
        context: ManagementCardContext,
        status: AdminUserStatusView,
        state: str,
        dispatch_status: str | None = None,
        status_message: str | None = None,
        trace_id: str,
    ) -> None:
        if self._management_card_refresher is None:
            return
        try:
            self._management_card_refresher.update(
                context=context,
                status=status,
                state=state,
                dispatch_status=dispatch_status,
                status_message=status_message,
            )
        except Exception as error:  # management card is best effort
            self._audit.record(
                "admin.card_callback.management_card_refresh_failed",
                error=type(error).__name__,
                trace_id=trace_id,
            )

    def handle_management_cancel(
        self,
        *,
        operator_open_id: str,
        identifier: str,
        chat_id: str,
        thread_id: str | None,
        message_id: str,
        trace_id: str,
    ) -> dict[str, Any]:
        """管理卡第三个按钮：只关闭/标记当前卡，不创建任何权限操作。"""
        context_check = self._management_context(
            message_id=message_id,
            identifier=identifier,
            operator_open_id=operator_open_id,
            trace_id=trace_id,
        )
        if context_check.forbidden:
            return _toast_error("只有发起该管理卡的管理员本人可以操作")
        if context_check.stale:
            return _toast_error("数据已变化，请重新查询")
        context = context_check.context
        status = context_check.status
        if context is None or self._management_context_store is None:
            return _toast_error("管理卡已失效，请重新查询 /admin user")
        try:
            updated = self._management_context_store.update_state(
                message_id=message_id,
                state="closed",
                dispatch_status="idle",
                last_trace_id=trace_id,
            )
            if updated is not None and status is not None:
                self._refresh_management_card(
                    context=updated,
                    status=status,
                    state="closed",
                    trace_id=trace_id,
                )
        except Exception as error:
            self._audit.record(
                "admin.card_callback.management_cancel_failed",
                error=type(error).__name__,
                trace_id=trace_id,
            )
            return _toast_error("系统繁忙，请稍后重试")
        self._audit.record(
            "admin.card_callback.management_cancelled",
            operator=operator_open_id,
            trace_id=trace_id,
        )
        return {"toast": {"type": "info", "content": "已取消"}}

    def _resolve_revoke_target(
        self,
        *,
        permission_group_id: str,
        override_id: str,
        context: ManagementCardContext | None,
        current_status: AdminUserStatusView | None,
        trace_id: str,
    ) -> dict[str, Any] | tuple[str, str]:
        """校验撤销目标是否存在、是否仍在当前快照里。

        成功时返回 ``(target_id, reason)``。不校验就拼命令文本时，空
        target_id 会拼出连续空白交给
        ``parse_admin_command`` 解析——当前固定原因文案不含空格，恰好只会落进
        token 数不足的 UNKNOWN 分支，但拼接前拦住不依赖这个偶然事实。
        """
        target_id = permission_group_id or override_id
        not_found_message = (
            "未识别到待撤销的权限组，请重新查询 /admin user 后再操作"
            if permission_group_id
            else "未识别到待撤销的覆盖行，请重新查询 /admin user 后再操作"
        )
        if not target_id:
            self._audit.record(
                "admin.card_callback.management_missing_override_id", trace_id=trace_id
            )
            return _toast_error(not_found_message)
        if context is not None and current_status is not None:
            found = (
                any(item.group_id == permission_group_id for item in current_status.local_overrides)
                if permission_group_id
                else any(item.override_id == override_id for item in current_status.local_overrides)
            )
            if not found:
                self._audit.record(
                    "admin.card_callback.management_override_mismatch", trace_id=trace_id
                )
                return _toast_error(not_found_message)
        reason = (
            _MANAGEMENT_CARD_GROUP_REVOKE_REASON
            if permission_group_id
            else _MANAGEMENT_CARD_REVOKE_REASON
        )
        return target_id, reason

    def handle_management_revoke(
        self,
        *,
        operator_open_id: str,
        override_id: str,
        chat_id: str,
        thread_id: str | None,
        message_id: str,
        trace_id: str,
        permission_group_id: str = "",
    ) -> dict[str, Any]:
        """管理卡「撤销」按钮点击。

        新职位+范围授权按钮携带 ``permission_group_id``，按一笔授权组整体撤销；
        历史无组行继续携带 ``override_id``，逐行撤销。两者都直接复用
        ``/admin revoke_permission <目标> <原因>`` 的封闭解析形状。
        """
        if self._management_actions is None:
            return _toast_error("该功能当前不可用，请改用文本命令")
        context_check = self._management_context(
            message_id=message_id,
            identifier="",
            operator_open_id=operator_open_id,
            trace_id=trace_id,
        )
        if context_check.forbidden:
            return _toast_error("只有发起该管理卡的管理员本人可以操作")
        if context_check.stale:
            return _toast_error("数据已变化，请重新查询")
        context = context_check.context
        current_status = context_check.status
        target = self._resolve_revoke_target(
            permission_group_id=permission_group_id,
            override_id=override_id,
            context=context,
            current_status=current_status,
            trace_id=trace_id,
        )
        if isinstance(target, dict):
            return target
        target_id, reason = target
        command = f"/admin revoke_permission {target_id} {reason}"
        return self._submit_management_command(
            command=command,
            operator_open_id=operator_open_id,
            trace_id=trace_id,
            chat_id=chat_id,
            thread_id=thread_id,
            message_id=message_id,
            context=context,
            current_status=current_status,
        )
