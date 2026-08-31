"""``card.action.trigger`` 回调的编排：解析出的按钮点击 → confirm/cancel → 卡片终态
更新 → 管理群脱敏通知。

只依赖注入的 ``Protocol`` 端口，不 import ``adapters/``（代码框架第二节）。真实装配
见 ``apps/gateway/__init__.py``；测试注入内存假实现。

## 载体 #96：``handle()`` 的返回值就是飞书卡片回调的应答帧

**根因（编排者用真实探针 + SDK 源码坐实）**：产品负责人真实点击确认卡后，业务执行
成功、群通知成功，但卡片永远回弹为原始带按钮状态。飞书卡片 2.0 的
``card.action.trigger`` 回调期望应答帧携带处理结果——lark SDK ws client
（``lark_oapi/ws/client.py`` 的 ``_handle_data_frame``，实测 1.7.1）把
``event_handler._do_without_validation`` 的**返回值** marshal 进应答帧
``resp.data``；``adapters/feishu_longconn.py`` 的 ``_RawEventSink.
_do_without_validation`` 此前对一切事件都返回 ``None``（=空 OK），平台收到空
``resp.data`` 便按"维持原卡"处理，视觉回滚——即使 ``_update_card_to_terminal``
出带外调用了 ``AdminCardTransport.update()`` 也一样：同一次点击的响应窗口内，
出带外更新会被这次回调自己的空应答盖掉（窗口外的同一实体更新则正常生效，两组真实
探针已证）。

修复方式：:meth:`AdminCardCallbackHandler.handle` 不再返回 ``CardCallbackOutcome``
这类内部结构，而是**直接返回飞书要的应答字典**——``apps/gateway/__init__.py`` 的
``make_event_handler`` 把它原样 ``return``，经
``lingxi.adapters.feishu_longconn.LongConnectionSupervisor._dispatch`` 一路透传到
``_RawEventSink._do_without_validation`` 的返回值，SDK 据此在应答帧里换上新卡片。
应答形状（SDK ``lark_oapi/event/callback/model/p2_card_action_trigger.py`` 的
``P2CardActionTriggerResponse``）：
``{"toast": {"type": "success"|"error"|"info", "content": "..."},
"card": {"type": "raw", "data": <卡片 2.0 JSON 对象>}}``；不带 ``card`` 键时飞书
不改变卡片当前展示，四类分支的具体形状见 :meth:`~AdminCardCallbackHandler.handle`
方法文档。出带外的 ``_update_card_to_terminal``/``_notify_group`` 两个 best-effort
分支保留不变——应答换卡是本次修复后的主路径，出带外更新降级为冗余纵深（即使它
失败，只要终态卡渲染本身没有异常，应答里仍然带着正确的终态卡）。

## 为什么是一个独立类而不是塞进 ``core/admin/router.py``

``AdminCommandRouter`` 处理的是私聊**文本**命令；本类处理的是**卡片按钮回调**，
两者的输入形状（``open_id``+``text`` vs. ``open_id``+``pending_action_id``+
``decision``）、返回形状（一段回复文本 vs. "卡片要不要更新、群里要不要通知"）都不
相同，唯一的耦合点是"发起 suspend/resume 时经 router 调用同一个
``PendingActionPreparer`` 端口"——这条耦合已经在 ``router.py`` 里，不需要两个编排器
互相 import。

## 卡片更新与群通知为什么分别用不同的判据触发

卡片更新（渲染成终态、去掉按钮）只要"这一行现在确实是终态"就该做，即使这次点击
本身没有让它**新**产生终态（例如同一张卡片被点了两次，第二次只是幂等重放）——重复
调用 ``AdminCardTransport.update()`` 是无害的，用它换取"即使第一次更新因为网络原因
失败，后续点击仍有机会让卡片视觉收敛"这一点好处。

群通知则完全不同：向管理群发一条"已确认执行"的通知，如果每次重复点击都重发一条，
管理群会被同一件事刷屏。因此群通知只在"这次点击**首次**让它落进终态"（即
``ConfirmDecision``/``CancelDecision`` 的 ``terminal_status`` 字段非空）时才发送，
幂等重放（``ALREADY_TERMINAL``）与未改变任何状态的拒绝（``NOT_INITIATOR``）都不
触发第二条通知。

## 已知边界（外部审查交叉裁定，不是本轮要修的缺陷）

- **回调未绑定具体是哪一张卡片（``card_id``）触发的点击（opus，纵深项）**：
  :meth:`AdminCardCallbackHandler.handle` 只核对回调事件体里飞书自己标注的
  ``operator.open_id`` 与回传值里的 ``pending_action_id``/``decision``（见类文档），
  不会额外核对这次 ``card.action.trigger`` 事件是不是真的来自当初为这条
  ``pending_action`` 建的那张卡片。这条纵深只在"``operator.open_id`` 已经能被
  伪造"这个前提下才有意义——而那个前提一旦成立，飞书回调本身的身份来源已经失守，
  是比"少校验一个 card_id"严重得多的问题（已排查为净，不是本模块能够补救的层次）。
  真正阻止越权确认的是 :func:`~lingxi.core.admin.pending_action.decide_confirm`
  对 ``initiated_by_open_id`` 的核对，不依赖 ``card_id`` 是否匹配。
- **停用有被动提示，恢复没有主动通知（评估后不做，opus P3-1）**：用户被停用后
  下一次问数会看到既有被动文案 ``gateway.suspended``（``core/conversation/
  pipeline.py``）；恢复后用户不会主动收到"你现在可以问数了"这类推送——本模块没有
  任何注入端口能够主动给一个任意 ``open_id`` 发私聊消息（``confirm_cards`` 只能
  回复触发命令那条消息，``group_notifier`` 只能发进固定配置的管理群），要做到这一点
  需要新增一个"主动私聊用户"端口（可复用 ``adapters/feishu_user_message.py`` 的
  出站实现，成本可控）**并且**在 ``config/content.toml`` 里新增一条面向用户的文案
  ——后者按既有纪律（见 ``core/permission/notification.py`` 模块文档）必须由产品
  负责人逐字批准才能进版本锁定的内容目录，不是实现代理可以单方面决定的产品措辞。
  因此本轮只登记这条边界，不新增端口或文案。
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from lingxi.core.admin.display_names import AdminDisplayNames
from lingxi.core.admin.management_card import (
    ADMIN_ACTION_CANCEL,
    ADMIN_ACTION_GRANT,
    ADMIN_ACTION_SUPPRESS,
)
from lingxi.core.admin.card_dispatch import (
    ManagementCardContext,
    management_card_fingerprint,
)
from lingxi.core.admin.notification import (
    DECISION_CANCEL,
    DECISION_CONFIRM,
    AdminCardTransport,
    GroupNotifier,
    describe_failed_reason,
    permission_scope_ids,
    render_card_payload,
    render_group_notice,
    render_terminal_card,
)
from lingxi.core.admin.pending_action import (
    PendingAction,
    PendingActionAuditWriteFailed,
    PendingActionStatus,
    PendingActionTransientFailure,
    PendingActionType,
)
from lingxi.core.admin.views import AdminUserStatusView


class _Decision(Protocol):
    """``ConfirmDecision``/``CancelDecision`` 的公共结构面：本类只用得到这四个
    字段/属性，用结构类型而不是 import 两个具体 dataclass，避免多引入一层耦合。
    """

    ok: bool
    message: str
    terminal_status: PendingActionStatus | None


class _Outcome(Protocol):
    decision: _Decision
    pending: PendingAction | None


class PendingActionDecider(Protocol):
    """confirm()/cancel() 两个真正改变状态的调用面，外加 CardKit sequence 记账。与
    ``adapters.postgres_pending_action.PostgresPendingActionStore`` 结构相同，
    测试注入内存假实现。真实实现在审计写入失败时抛出
    :class:`~lingxi.core.admin.pending_action.PendingActionAuditWriteFailed`。
    """

    def confirm(self, *, pending_action_id: str, clicker_open_id: str) -> _Outcome: ...

    def cancel(self, *, pending_action_id: str, clicker_open_id: str) -> _Outcome: ...

    def next_card_sequence(self, *, pending_action_id: str) -> int: ...


class AuditSink(Protocol):
    """与 ``core/admin/router.AuditSink``/``adapters/postgres_pending_action.
    AuditSink`` 结构相同的独立 Protocol——三处不互相 import，见模块与那两处各自
    的文档。"""

    def record(self, action: str, /, **fields: object) -> None: ...


class _ManagementRouteOutcome(Protocol):
    """``AdminRouteOutcome`` 的公共结构面（#439 B 档新增两个方法用得到的四个
    字段）：用结构类型而不是 import 具体 dataclass，避免card_callback.py 与
    router.py 之间多一层强耦合——两者本来就已经通过 ``PendingActionPreparer``/
    ``ConfirmCardSender`` 两个独立端口分别与 ``AdminCommandRouter`` 打交道（见
    ``router.py`` 模块文档），这里延续同一分工。"""

    handled: bool
    content_key: str
    reply_text: str


class ManagementActionRouter(Protocol):
    """管理卡（#439 B 档）表单/按钮回调 → 等价 ``/admin ...`` 命令文本 → 既有
    ``AdminCommandRouter.route()`` 的调用面。与 ``PendingActionDecider``/
    ``AdminCardTransport`` 两个既有端口是同一个类（真实装配时都指向同一个
    ``AdminCommandRouter`` 实例），但作为独立声明的 Protocol——本类只用得到
    ``route()`` 这一个方法，不需要 import 具体的 ``AdminCommandRouter`` 类型。

    真正的写路径判定（角色核对、自我目标防呆、prepare()、确认卡发送、审计）
    全部发生在 ``route()`` 内部——本类新增的两个方法只负责"把管理卡的交互翻译成
    一条等价命令文本"，不重新实现任何一步既有判定（#437/#438 领地不变）。
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
    ) -> _ManagementRouteOutcome: ...


class ManagementCardContextReader(Protocol):
    def lookup_context(self, *, message_id: str) -> ManagementCardContext | None: ...

    def update_state(
        self,
        *,
        message_id: str,
        state: str | None = None,
        dispatch_status: str | None = None,
        snapshot_fingerprint: str | None = None,
        last_trace_id: str | None = None,
    ) -> ManagementCardContext | None: ...


class ManagementCardRefresher(Protocol):
    """在原管理卡实体上按持久 sequence 更新最新状态。"""

    def update(
        self,
        *,
        context: ManagementCardContext,
        status: AdminUserStatusView,
        state: str,
        dispatch_status: str | None = None,
        status_message: str | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class _ManagementContextCheck:
    context: ManagementCardContext | None
    status: AdminUserStatusView | None
    stale: bool = False
    forbidden: bool = False


#: 管理卡撤销按钮（组或历史行）没有独立的原因输入框（issue #439 B 档设计：一键
#: 撤销，不为撤销单独加一个表单）——服务端补一个固定原因，供审计与确认卡回显；
#: 管理员需要自定义撤销原因时仍可用文本命令 ``/admin revoke_permission`` 自行填写。
#:
#: 术语统一（Trace #469 S-1 遗漏，收尾批 L4a 实测发现）：取值随按钮标签一起从
#: 「收回」改成「撤销」，与 ``core/admin/notification._ACTION_LABEL`` 的
#: ``LOCAL_PERMISSION_REVOKE`` 项、``core/admin/management_card`` 覆盖行按钮的
#: ``label="撤销"`` 逐字一致——此前按钮写「撤销」、这段原因文字写「收回」，同一次
#: 操作在确认卡「原因」行/终态卡/群通知里出现两套说法（TOP-7 防倒退）。
#:
#: **已落库的旧值一律不回改**：这段文本进的是 ``pending_action.payload`` 的
#: ``reason`` 键（自由文本，见 ``adapters/postgres_pending_action.py``
#: ``prepare()`` 的 revoke 分支），渲染侧只做原样展示与定长截断；全仓没有任何
#: 一处按它的字面量做比较、匹配或解析（``notification._safe_reason`` 那条形状
#: 白名单作用于 ``pending_action.reason`` 机器码，不是这段自由文本），因此历史
#: 行继续原样渲染，换新值不会让任何一条旧记录解析失败或渲染异常。审计文本是
#: 历史事实，改成今天的说法反而会篡改"当时管理员看到的是什么"。
_MANAGEMENT_CARD_REVOKE_REASON = "管理卡逐行撤销"
_MANAGEMENT_CARD_GROUP_REVOKE_REASON = "管理卡撤销职位范围授权"

#: 表单提交成功创建待确认操作时 ``AdminRouteOutcome.content_key`` 的取值——与
#: ``router.py`` 里 ``_dispatch_write_action`` 最终成功分支写死的字面量一致，
#: 是本模块判断"这次提交要不要提示成功 toast"的唯一依据（见 ``router.py`` 该
#: 分支的 ``content_key="admin.write_action_pending"``）。
_WRITE_ACTION_PENDING_CONTENT_KEY = "admin.write_action_pending"


def _toast_from_route_outcome(outcome: _ManagementRouteOutcome) -> dict[str, Any]:
    """把 ``AdminCommandRouter.route()`` 的结论翻译成管理卡交互的 toast 应答
    （表单/撤销按钮的提交结果先以 toast 返回；原管理卡的不可操作状态由出带外
    持久化更新负责，见 ``core/admin/management_card.py`` 与 gateway 装配）。"""

    if not outcome.handled:
        # 结构上只会发生在"点击这一刻当前角色恰好已被撤销"——route() 内部会重新
        # 判定一次身份，不假设管理卡的历史交互权限仍然有效。
        return _toast_error("当前身份已无权限执行该操作，请重新查询 /admin user")
    toast_type = "success" if outcome.content_key == _WRITE_ACTION_PENDING_CONTENT_KEY else "error"
    return {"toast": {"type": toast_type, "content": outcome.reply_text}}


class PermissionRecomputeTrigger(Protocol):
    """确认执行成功后，对目标用户即时触发一次定向权限重算/发布的端口（Issue
    #438）。真实实现见
    ``adapters/postgres_permission_recompute_trigger.PermissionRecomputeAdapter``；
    它自己决定"这个 ``PendingAction`` 该按哪种方式重算"（停用走清空、其余四类
    走完整合并管线），``card_callback.py`` 不需要、也不应该知道这些细节——本类
    只负责"确认执行成功了，在恰当的时刻调用它一次，失败了就记审计"，业务规则
    全部留在权限模块（代码框架第二节：``core/admin`` 与 ``core/permission`` 是
    两个平级领域模块，互不下沉对方的规则）。
    """

    def trigger(self, pending: PendingAction) -> None: ...


class AdminCardCallbackHandler:
    """``card.action.trigger`` 事件的唯一处理入口。见模块文档。"""

    def __init__(
        self,
        *,
        pending_actions: PendingActionDecider,
        confirm_cards: AdminCardTransport,
        group_notifier: GroupNotifier | None,
        group_chat_id: str | None,
        audit: AuditSink,
        display_names: AdminDisplayNames,
        management_actions: ManagementActionRouter | None = None,
        recompute_trigger: PermissionRecomputeTrigger | None = None,
        management_context_store: ManagementCardContextReader | None = None,
        management_state_lookup: Callable[[str], AdminUserStatusView | None] | None = None,
        management_card_refresher: ManagementCardRefresher | None = None,
    ) -> None:
        self._pending_actions = pending_actions
        self._confirm_cards = confirm_cards
        self._group_notifier = group_notifier
        self._group_chat_id = group_chat_id
        self._audit = audit
        # 必填（Trace #469 S-1）：终态卡「目标：」字段与管理群广播都不再允许
        # 退回展示 open_id——见 core/admin/display_names.AdminDisplayNames 模块
        # 文档「安全边界」一节，与 ConfirmCardDispatcher 同一条纪律。
        self._display_names = display_names
        # 未装配时管理卡的表单/按钮交互回复"该功能当前不可用"，不假装已经路由
        # 过任何命令——与本类既有确认/取消两个端口"未装配=构造时必须传入"不同
        # （#439 新增，向后兼容既有全部调用点：不传这个参数时行为逐字节不变）。
        self._management_actions = management_actions
        # 定向权限重算触发口（Issue #438）：``None`` 表示装配层还没接（与
        # ``group_notifier=None`` 同一姿态）——不报错、不重试，异步下发这一层
        # 纵深缺席时，每日批仍是保底，确认事务本身的结果不被改变。
        self._recompute_trigger = recompute_trigger
        self._management_context_store = management_context_store
        self._management_state_lookup = management_state_lookup
        self._management_card_refresher = management_card_refresher

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
        """管理卡「职位+公司范围补充授权」表单提交（#493）。

        参数是**已经从飞书表单回传值解析出来的干净字段**——原始 ``card.action.
        trigger`` 事件体的解析（含如何从表单字段里取出 ``company_id``/
        ``metric_name``/``reason``）是 gateway 接线层的职责，与既有
        ``operator_open_id``/``pending_action_id``/``decision`` 三个干净参数同一
        分工（见模块文档"``handle()`` 的返回值就是飞书卡片回调的应答帧"一节，
        原始事件解析同样不在本类里）。

        任何一个必填字段为空（下拉未选择、目录不可用时的占位选项、输入框留空）
        都在这里直接拒绝，不构造一条注定是 ``UNKNOWN`` 的命令文本去打扰
        ``route()``——三个字段的校验判据本身就是"非空"，与 ``core/admin/
        commands.py`` 的既有语法门重复，但在这里提前判断能给出"请选择/填写
        哪一项"这种更具体的 toast，而不是笼统的"未识别的管理命令"。
        """

        if self._management_actions is None:
            return _toast_error("该功能当前不可用，请改用文本命令")
        position_form = bool(position_name or company_scope)
        if admin_action not in (ADMIN_ACTION_GRANT, ADMIN_ACTION_SUPPRESS):
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
        context = context_check.context
        current_status = context_check.status
        if context is not None and not identifier:
            identifier = context.identifier
        elif context is not None:
            # The card context is authoritative.  Never allow a tampered hidden
            # identifier to redirect a management-card action to another user.
            identifier = context.identifier
        if not identifier:
            # opus 审查坐实并修复：不校验就直接拼命令文本时，`identifier` 为空
            # 会让下面的 f-string 拼出两个连续空白（`.../{sub_command} {""} {company_id} ...`）
            # ——`core/admin/commands.py::parse_admin_command` 按 `str.split()`
            # 解析，连续空白被当成一个分隔符吃掉，company_id/metric_name/reason
            # 的第一个词依次整体左移一位，落进 `_parse_permission_command` 后仍
            # 可能是一条形状合法的 grant/suppress 命令——只是语义已经完全错位
            # （真正的目标公司被当成 identifier 吞掉，真正的指标被当成公司，
            # 原因的第一个词被当成指标名），管理员看到的却是"已生成待确认操作"
            # 这类正常回执，察觉不到目标已经变成别人。必须在拼接命令文本之前
            # 拦住，不能指望下游语法校验替它兜底。
            self._audit.record(
                "admin.card_callback.management_missing_identifier",
                admin_action=admin_action,
                trace_id=trace_id,
            )
            return _toast_error("未识别到目标用户标识，请重新查询 /admin user 后再操作")
        if position_form:
            if admin_action != ADMIN_ACTION_GRANT:
                return _toast_error("职位+公司范围只支持补充授权")
            if not reason.strip():
                return _toast_error("请填写原因")
            if not position_name.strip():
                return _toast_error("请选择银河职位")
            if not company_scope.strip():
                return _toast_error("请选择公司范围")
            if any(ch.isspace() for ch in position_name) or any(
                ch.isspace() for ch in company_scope
            ):
                return _toast_error("职位或公司范围无效，请重新选择")
            text = f"/admin grant_position {identifier} {position_name} {company_scope} {reason.strip()}"
            outcome = self._route_management_action(
                operator_open_id=operator_open_id,
                text=text,
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

        if not company_id:
            return _toast_error("请选择公司")
        if not metric_name:
            return _toast_error("请选择指标")
        if not reason.strip():
            return _toast_error("请填写原因")
        # 纵深加固（Trace #469 修复包 B，B-2）：上面的"非空"校验挡不住一个
        # 非空但含空白字符的取值——`str.split()` 按任意空白切分（含全角空格
        # U+3000，Python `str.isspace()` 同样判它为空白），审查实测
        # `company_id="1011 sub_new_count"` 能把管理员选的指标静默左移替换掉
        # （与上面 identifier 为空时同一个根因：拼接前不拦住，下游语法校验
        # 认不出这类错位）。当前调用点全部来自服务端渲染的下拉选项/受控字段，
        # 结构上不可达，但值得纵深——不假设未来的调用点也一样受控。三个字段
        # 分别落回各自既有的拒绝文案，不新造一套措辞。
        if any(ch.isspace() for ch in identifier):
            self._audit.record(
                "admin.card_callback.management_missing_identifier",
                admin_action=admin_action,
                trace_id=trace_id,
            )
            return _toast_error("未识别到目标用户标识，请重新查询 /admin user 后再操作")
        if any(ch.isspace() for ch in company_id):
            return _toast_error("请选择公司")
        if any(ch.isspace() for ch in metric_name):
            return _toast_error("请选择指标")

        sub_command = "grant_permission" if admin_action == ADMIN_ACTION_GRANT else "suppress_permission"
        text = f"/admin {sub_command} {identifier} {company_id} {metric_name} {reason.strip()}"
        outcome = self._route_management_action(
            operator_open_id=operator_open_id,
            text=text,
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
        confirmation can link back to the management card.  Historical test/plugin
        routers may expose the pre-#493 signature; introspection keeps those callers
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
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
        except (TypeError, ValueError):
            supports_origin = False
        if supports_origin:
            kwargs["origin_card_message_id"] = message_id or None
        return self._management_actions.route(**kwargs)  # type: ignore[arg-type]

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

        store = self._management_context_store
        if store is None or not message_id:
            return _ManagementContextCheck(context=None, status=None)
        try:
            context = store.lookup_context(message_id=message_id)
        except Exception as error:  # noqa: BLE001 - 读上下文失败不得放行
            self._audit.record(
                "admin.card_callback.management_context_lookup_failed",
                error=type(error).__name__,
                trace_id=trace_id,
            )
            return _ManagementContextCheck(context=None, status=None, stale=True)
        if context is None:
            # A production management-card action must have a persistent
            # binding.  A miss can be a forged message id, a pre-migration
            # card, or a retained row that was removed; never trust the hidden
            # identifier and route a write without that binding.
            self._audit.record(
                "admin.card_callback.management_context_missing",
                trace_id=trace_id,
            )
            return _ManagementContextCheck(context=None, status=None, stale=True)
        if context.initiated_by_open_id and context.initiated_by_open_id != operator_open_id:
            self._audit.record(
                "admin.card_callback.management_not_initiator",
                trace_id=trace_id,
            )
            return _ManagementContextCheck(
                context=context, status=None, forbidden=True
            )
        if identifier and identifier != context.identifier:
            self._audit.record(
                "admin.card_callback.management_identifier_mismatch",
                trace_id=trace_id,
            )
            return _ManagementContextCheck(
                context=context, status=None, forbidden=True
            )
        if context.state in {"closed", "submitted", "dispatching"}:
            # A closed card must not become a write entry point again if a stale
            # callback is replayed. Likewise, once a form submission has already
            # produced its confirmation card, a duplicate delivery must not create
            # a second logical operation. Terminal effective/incomplete states
            # intentionally remain actionable: the renderer restores the form for
            # a fresh operation against the latest snapshot.
            self._audit.record(
                "admin.card_callback.management_context_not_actionable",
                trace_id=trace_id,
                state=context.state,
            )
            return _ManagementContextCheck(context=context, status=None, stale=True)
        expired = context.context_deadline_at <= datetime.now(timezone.utc)
        if expired:
            self._audit.record(
                "admin.card_callback.management_context_expired", trace_id=trace_id
            )
        status = None
        if self._management_state_lookup is not None:
            try:
                status = self._management_state_lookup(context.identifier)
            except Exception as error:  # noqa: BLE001 - fail closed on state read errors
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
                # 变化后尽力把原卡更新到最新状态；无论更新成败都不继续写入旧快照。
                try:
                    refreshed_context = store.update_state(
                        message_id=message_id,
                        state="closed",
                        dispatch_status="idle",
                        snapshot_fingerprint=fingerprint,
                        last_trace_id=trace_id,
                    ) or context
                except Exception as error:  # noqa: BLE001 - card refresh is best effort
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
                return _ManagementContextCheck(context=context, status=status, stale=True)
        if expired:
            return _ManagementContextCheck(context=context, status=status, stale=True)
        return _ManagementContextCheck(context=context, status=status)

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
        except Exception as error:  # noqa: BLE001 - database/card update is best effort
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
        except Exception as error:  # noqa: BLE001 - management card is best effort
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
        except Exception as error:  # noqa: BLE001
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
        """管理卡「撤销」按钮点击（#439 B 档新增交互分支）。

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
        target_id = permission_group_id or override_id
        if not target_id:
            # 与 `handle_management_form_submit` 同一条纪律：不校验就拼命令
            # 文本，`override_id` 为空时下面这行会拼出连续空白，交给
            # `parse_admin_command` 解析——当前固定原因文案不含空格，恰好只会
            # 落进 token 数不足的 UNKNOWN 分支，但拼接前拦住不依赖这个偶然
            # 事实（固定原因文案将来改动时不应该重新引入这一类角落）。
            self._audit.record(
                "admin.card_callback.management_missing_override_id",
                trace_id=trace_id,
            )
            return _toast_error(
                "未识别到待撤销的权限组，请重新查询 /admin user 后再操作"
                if permission_group_id
                else "未识别到待撤销的覆盖行，请重新查询 /admin user 后再操作"
            )
        if context is not None and current_status is not None:
            found = (
                any(item.group_id == permission_group_id for item in current_status.local_overrides)
                if permission_group_id
                else any(item.override_id == override_id for item in current_status.local_overrides)
            )
            if not found:
                self._audit.record(
                    "admin.card_callback.management_override_mismatch",
                    trace_id=trace_id,
                )
                return _toast_error(
                    "未识别到待撤销的权限组，请重新查询 /admin user 后再操作"
                    if permission_group_id
                    else "未识别到待撤销的覆盖行，请重新查询 /admin user 后再操作"
                )
        reason = (
            _MANAGEMENT_CARD_GROUP_REVOKE_REASON
            if permission_group_id
            else _MANAGEMENT_CARD_REVOKE_REASON
        )
        text = f"/admin revoke_permission {target_id} {reason}"
        outcome = self._route_management_action(
            operator_open_id=operator_open_id,
            text=text,
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

    def handle(
        self, *, operator_open_id: str, pending_action_id: str, decision: str, trace_id: str
    ) -> dict[str, Any]:
        """处理一次卡片按钮点击，返回飞书卡片回调的应答载荷（见模块文档「载体 #96」）。

        返回值不是内部结论，是**要直接交给飞书的应答帧**——``apps/gateway`` 原样
        ``return`` 它，一路透传到 ``adapters/feishu_longconn.py`` 的
        ``_RawEventSink._do_without_validation``，由 lark SDK marshal 进
        ``resp.data``。五类应答形状：

        1. 点击后达到/已处于终态（``executed``/``cancelled``/``expired``/
           ``failed``，含幂等重放）：``{"toast": {"type": "success"（仅
           ``executed``）/"info"（其余三种）, "content": <终态文案>},
           "card": {"type": "raw", "data": <终态卡 2.0 JSON 对象>}}``——终态卡
           JSON 复用 :func:`~lingxi.core.admin.notification.render_card_payload`，
           与出带外 ``update()`` 调用共用同一份构造，不允许分叉出第二份。
        2. 点击人不是发起人：``pending_action`` 保持 ``pending`` 不变（见
           ``decide_confirm``/``decide_cancel`` 文档），toast error，**不带
           ``card``**——不能把终态卡展示给非发起人，也不能改动发起人正在看的
           那张卡片。
        3. 动作不存在（``decision`` 不是 ``"confirm"``/``"cancel"``，或
           ``pending_action_id`` 从未存在过/未真正送达）：toast error，不带
           ``card``。
        4. 审计写入失败（:class:`PendingActionAuditWriteFailed`，事务已整体
           回滚）：toast error「系统繁忙请重试」，不带 ``card``。
        5. 数据库瞬时故障（:class:`PendingActionTransientFailure`——死锁、锁等待
           超时或其他操作性错误，事务已整体回滚，批次 4 F1，Issue #304）：toast
           error「系统繁忙，请稍后重试」，不带 ``card``。与分支 4 分开列是因为
           触发条件不同（分支 4 是审计 sink 本身失败；这里是数据库锁冲突）且文案
           故意不同（多一个逗号与"稍后"，便于审计日志按文案徒手区分两类故障），
           但产品含义相同：这次点击结构上"没有发生过"，可以直接重新点击卡片重试。

        ``decision`` 只识别 ``"confirm"``/``"cancel"`` 两个字面量（见
        ``core/admin/notification.py`` 的 ``DECISION_CONFIRM``/``DECISION_CANCEL``
        常量）；不认识的取值（篡改的回调 payload）记审计后原样拒绝，不猜测意图。
        """

        if decision not in (DECISION_CONFIRM, DECISION_CANCEL):
            self._audit.record(
                "admin.card_callback.unknown_decision", trace_id=trace_id
            )
            return _toast_error("操作不存在或已失效")

        try:
            if decision == DECISION_CONFIRM:
                outcome = self._pending_actions.confirm(
                    pending_action_id=pending_action_id, clicker_open_id=operator_open_id
                )
            else:
                outcome = self._pending_actions.cancel(
                    pending_action_id=pending_action_id, clicker_open_id=operator_open_id
                )
        except PendingActionAuditWriteFailed:
            # 审计写入失败：事务已回滚，pending_action 与目标账号均未改变。不更新
            # 卡片终态、不带 card（这次点击结构上"没有发生过"，卡片仍然是可以
            # 重新点击的 pending 态，符合接口设计错误码 audit_write_failed 的
            # "调用方可重试"），只记一条审计。
            self._audit.record(
                "admin.card_callback.audit_write_failed",
                pending_action_id=pending_action_id,
                trace_id=trace_id,
            )
            return _toast_error("系统繁忙请重试")
        except PendingActionTransientFailure as error:
            # 数据库瞬时故障（死锁、锁等待超时，或其他操作性错误）：事务已回滚，
            # pending_action 与目标账号均未改变，与上面的审计写入失败同一姿态
            # （不更新卡片、不带 card，可以直接重新点击重试）——批次 4 F1，
            # Issue #304：opus 审查在真库上三次复现清理路径（``clear_delivered_
            # content_for_user``）与 ``/new``/空闲扫描锁序相反导致的死锁；「停用
            # 正在聊天的用户」还容易撞见非死锁的锁等待超时变体（confirm 对目标
            # 用户全部会话 FOR UPDATE，其中一个恰好被 gateway 入站事务持锁超过
            # lock_timeout）。审计记录带上 ``error.classification``（抛出方
            # psycopg 异常的类名，例如 ``DeadlockDetected``/``LockNotAvailable``）
            # 供事后按故障类别检索，不进 toast 文案（管理员不需要知道底层是哪种
            # 数据库异常）。不吞其他异常：只有这一个具体类型被捕获，见模块文档
            # 「只依赖注入的 Protocol 端口」——本类不 import adapters/，因此不能
            # 直接 except psycopg 的异常类型，真正的捕获与转译在
            # ``adapters/postgres_pending_action.py`` 完成（见
            # ``PendingActionTransientFailure`` 类文档）。
            self._audit.record(
                "admin.card_callback.transient_failure",
                pending_action_id=pending_action_id,
                trace_id=trace_id,
                classification=error.classification,
            )
            return _toast_error("系统繁忙，请稍后重试")

        if outcome.pending is None:
            # 从未存在过的待确认操作 ID（含伪造回调）——没有可展示的卡片，只记审计。
            self._audit.record(
                "admin.card_callback.not_found",
                pending_action_id=pending_action_id,
                trace_id=trace_id,
            )
            return _toast_error("操作不存在或已失效")

        pending = outcome.pending
        self._audit.record(
            "admin.card_callback.handled",
            pending_action_id=pending.id,
            decision=decision,
            status=pending.status.value,
            trace_id=trace_id,
        )

        # 卡片视觉收敛：只要这一行**现在**是终态就渲染终态卡 JSON，不要求这次点击
        # 本身是让它第一次落进终态的那一次（见模块文档，含幂等重放）。出带外
        # update() 调用是冗余纵深，即使它失败，card_payload 已经在
        # _update_card_to_terminal 内部算好并原样返回——应答本身仍然带着正确的
        # 终态卡（模块文档「载体 #96」）。
        card_payload: dict[str, Any] | None = None
        if pending.status is not PendingActionStatus.PENDING:
            card_payload = self._update_card_to_terminal(pending)

        # 群通知：只在这次点击**首次**产生了新的终态时发送，避免幂等重放刷屏。
        if outcome.decision.terminal_status is not None:
            self._notify_group(pending)

        # 职位表单产生的确认卡带有原管理卡反向链接。确认后立刻把原卡置为
        # 「下发中」，取消/失败则恢复成最新只读状态；重算完成回调可再次刷新为
        # 「已生效」或「未完成」。先推进原卡再入队重算，避免极快的后台结果（尤其
        # ``UNCHANGED`` 无需启动观察线程）被后面的“已提交”刷新覆盖回去。
        if outcome.decision.terminal_status is not None:
            self._refresh_origin_management_card(pending=pending, trace_id=trace_id)

        # 定向权限重算（Issue #438）：只在这次点击**首次**让操作真正执行成功时
        # 触发，与群通知同一去重判据（幂等重放/未改变任何状态的拒绝都不重复
        # 触发）；额外要求 ``status is EXECUTED``——``CANCELLED``/``EXPIRED``/
        # ``FAILED`` 都不该让任何用户的发布内容发生变化。best-effort：失败只
        # 记审计,不影响已经落库的确认结果（模块文档「载体 #96」同一姿态）。
        if outcome.decision.terminal_status is not None and pending.status is PendingActionStatus.EXECUTED:
            self._trigger_recompute(pending)

        if pending.status is PendingActionStatus.PENDING:
            # 到这里仍是 pending 的唯一分支是点击人不是发起人——其余分支都会让
            # pending_action 落进某个终态（见 decide_confirm/decide_cancel）。
            return _toast_error("只有发起人本人可以操作此卡片")

        toast_content = outcome.decision.message
        if pending.status is PendingActionStatus.EXECUTED and _is_position_scope_pending(pending):
            # #493 的确认结果先是事务已记录，随后才由后台重算发布；不要把成功
            # toast 误写成已经生效。旧的逐公司×指标命令保持历史文案兼容。
            toast_content = _outcome_text(pending)
        response: dict[str, Any] = {
            "toast": {
                "type": "success" if pending.status is PendingActionStatus.EXECUTED else "info",
                # 接线级修复（Trace #469 S-1 TOP-3）：直接用这次点击产生的
                # ``decision.message``——``decide_confirm``/``decide_cancel``
                # 已经为每个分支写好完整的友好中文（例如 ROLE_REVOKED 的"当前
                # 角色已无权执行该操作，请重新查询后再发起。"），此前这里改用
                # ``_outcome_text(pending)`` 重新按 ``pending.status`` 派生一句
                # 更粗糙的文案（FAILED 分支曾经直出机器码 "未执行（role_
                # revoked）"）。到这一行之前的分支已经排除了 NOT_FOUND/
                # NOT_INITIATOR（未改变 pending 状态的情形），因此
                # ``outcome.decision.message`` 在这里恒非空、且恰好描述"这次
                # 点击的结果"——toast 本来就是对**这次点击**的即时反馈，用
                # ``decision.message`` 比用只看持久状态的 ``_outcome_text``
                # 更准确（例如幂等重放会说"该操作已经执行过，不会重复执行。"
                # 而不是重新展示第一次的"已确认执行"）。
                "content": toast_content,
            }
        }
        if card_payload is not None:
            response["card"] = {"type": "raw", "data": card_payload}
        return response

    def _resolve_scope_labels(self, pending: PendingAction) -> tuple[str | None, str | None]:
        """本地权限三类动作的「公司/指标」人性化展示标签（Trace #469 S-1）；
        非本地权限动作或 payload 不可用时返回 ``(None, None)``，调用方据此让
        渲染函数走既有的降级路径（见 ``notification.permission_scope_ids``
        文档）。"""

        scope_ids = permission_scope_ids(pending)
        if scope_ids is None:
            return None, None
        company_id, metric_id = scope_ids
        return (
            self._display_names.company_label(company_id=company_id),
            self._display_names.metric_label(metric_id=metric_id),
        )

    def _update_card_to_terminal(self, pending: PendingAction) -> dict[str, Any] | None:
        """渲染终态卡的 CardKit JSON，并尽力而为地出带外更新已发出的那张卡片。

        **返回值与出带外调用的成败无关**：``card_payload`` 在进入 ``try`` 块之前
        就已经算好，``self._confirm_cards.update()`` 失败只记审计、不影响返回值
        ——回调应答（``handle()`` 的主路径）需要在出带外更新失败时仍然携带正确
        的终态卡（模块文档「载体 #96」：应答换卡是主路径，出带外更新是冗余纵深）。

        「目标：」字段与「范围」区块（Trace #469 S-1）经
        :class:`~lingxi.core.admin.display_names.AdminDisplayNames` 翻译成人类
        可读文本——持久化终态卡展示的是 ``pending`` 本身的状态，不随点击者是谁
        而变化，因此这里的 ``outcome_text`` 继续由 :func:`_outcome_text` 按
        ``pending.status``/``pending.reason`` 派生（与 ``handle()`` 里 toast 用
        ``outcome.decision.message`` 是两件不同的事，见该处注释）。
        """

        if pending.card_id is None:
            return None
        target_label = self._display_names.user_label(open_id=pending.target_open_id)
        company_label, metric_label = self._resolve_scope_labels(pending)
        card = render_terminal_card(
            pending,
            target_label=target_label,
            outcome_text=_outcome_text(pending),
            company_label=company_label,
            metric_label=metric_label,
        )
        card_payload = render_card_payload(card)
        try:
            # 换一个本次调用专用的 sequence（外部审查交叉裁定，opus P2-1）：同一张
            # 卡片可能因为回调重投被多次调用 update，CardKit 要求每次调用携带严格
            # 递增的 sequence，见 adapters/feishu_admin_card.py 该方法的文档。
            sequence = self._pending_actions.next_card_sequence(pending_action_id=pending.id)
            self._confirm_cards.update(card_id=pending.card_id, sequence=sequence, card=card)
        except Exception as error:  # noqa: BLE001 - 卡片视觉更新失败不影响已经落库的业务结果
            self._audit.record(
                "admin.card_callback.card_update_failed",
                pending_action_id=pending.id,
                error=type(error).__name__,
            )
        return card_payload

    def _trigger_recompute(self, pending: PendingAction) -> None:
        if self._recompute_trigger is None:
            return
        try:
            self._recompute_trigger.trigger(pending)
        except Exception as error:  # noqa: BLE001 - 失败降级回每日批，不影响已经落库的确认结果
            self._audit.record(
                "admin.card_callback.recompute_trigger_failed",
                pending_action_id=pending.id,
                error=type(error).__name__,
            )

    def _refresh_origin_management_card(self, *, pending: PendingAction, trace_id: str) -> None:
        origin_message_id = getattr(pending, "origin_card_message_id", None)
        store = self._management_context_store
        lookup = self._management_state_lookup
        if not origin_message_id or store is None or lookup is None:
            return
        try:
            context = store.lookup_context(message_id=origin_message_id)
            if context is None:
                return
            status = lookup(context.identifier)
            if status is None:
                return
            if pending.status is PendingActionStatus.EXECUTED:
                state, dispatch_status = "dispatching", "publishing"
            elif pending.status is PendingActionStatus.CANCELLED:
                # 这里是**确认卡**上的取消：按 #493 生命周期约定，确认卡处理完
                # 后原管理卡回到最新只读快照并恢复表单。管理卡自身的“取消”按钮
                # 走 ``handle_management_cancel``，才会把上下文置为 ``closed``。
                state, dispatch_status = "ready", "idle"
            elif pending.status is PendingActionStatus.FAILED:
                state, dispatch_status = "incomplete", "incomplete"
            else:
                state, dispatch_status = "closed", "idle"
            updated = store.update_state(
                message_id=origin_message_id,
                state=state,
                dispatch_status=dispatch_status,
                snapshot_fingerprint=management_card_fingerprint(status),
                last_trace_id=trace_id,
            )
            if updated is not None:
                self._refresh_management_card(
                    context=updated,
                    status=status,
                    state=state,
                    # ``idle`` 是持久层机器状态，不应原样出现在卡片的人类可见
                    # 文案；ready 分支传 None 让 renderer 只展示恢复后的表单。
                    dispatch_status=None if state == "ready" else dispatch_status,
                    trace_id=trace_id,
                )
        except Exception as error:  # noqa: BLE001 - refresh is best effort after commit
            self._audit.record(
                "admin.card_callback.management_card_refresh_failed",
                error=type(error).__name__,
                trace_id=trace_id,
            )

    def _notify_group(self, pending: PendingAction) -> None:
        if self._group_notifier is None or not self._group_chat_id:
            return
        # 群通知只用 render_group_notice 内部按形状白名单渲染出的脱敏摘要（见该
        # 函数文档），不转发本次点击结论的原始 message 文本。目标用户身份/公司/
        # 指标同样经 AdminDisplayNames 翻译（Trace #469 S-1 C 项：管理群终态
        # 广播不再维持隐私折衷，改为显示姓名+邮箱与可读内容）。
        target_label = self._display_names.user_label(open_id=pending.target_open_id)
        company_label, metric_label = self._resolve_scope_labels(pending)
        text = render_group_notice(
            pending,
            target_label=target_label,
            company_label=company_label,
            metric_label=metric_label,
        )
        try:
            self._group_notifier.send_text(
                chat_id=self._group_chat_id, text=text, dedupe_key=pending.id
            )
        except Exception as error:  # noqa: BLE001 - 群通知失败不影响已经落库的业务结果
            self._audit.record(
                "admin.card_callback.group_notify_failed",
                pending_action_id=pending.id,
                error=type(error).__name__,
            )


def _toast_error(content: str) -> dict[str, Any]:
    """错误类应答的公共形状：只有 toast，不带 ``card``（见模块文档「载体 #96」
    ``handle()`` 的分支 2-4：这三类分支都不产生可以安全展示给这次点击者的卡片，
    要么点击者不是发起人，要么这次点击对应的动作根本不存在或没有留下可信状态）。
    """

    return {"toast": {"type": "error", "content": content}}


def _is_position_scope_pending(pending: PendingAction) -> bool:
    """识别 #493 的职位范围授权，供即时回执采用异步下发文案。"""

    if pending.action_type is not PendingActionType.LOCAL_PERMISSION_GRANT:
        return False
    if not pending.payload:
        return False
    try:
        payload = json.loads(pending.payload)
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and bool(payload.get("position_name"))


def _outcome_text(pending: PendingAction) -> str:
    if pending.status is PendingActionStatus.EXECUTED:
        # 数据库事务已记录，但重算/发布由后台队列异步完成；不能把「已记录」
        # 误报为「即时生效」。完成后由后台回调把原管理卡刷新为最终状态。
        return "操作已记录，权限正在下发"
    if pending.status is PendingActionStatus.CANCELLED:
        return "已取消"
    if pending.status is PendingActionStatus.EXPIRED:
        return "已过期，未执行"
    if pending.status is PendingActionStatus.FAILED:
        # 接线级修复（Trace #469 S-1 TOP-3）：``pending.reason`` 是
        # ``adapters/postgres_pending_action.py`` 写入的机器码（例如
        # "role_revoked"），此前原样拼进这句话直出给管理员——改用
        # ``describe_failed_reason`` 翻译成中文，与群通知
        # （``notification._group_outcome_text``）共用同一份词表，不允许两处
        # 出现不同说法。
        return f"未执行（{describe_failed_reason(pending.reason)}）"
    return "状态未知"  # pragma: no cover - 调用点已确保 status 非 PENDING
