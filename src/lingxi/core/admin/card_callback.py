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

from typing import Any, Protocol

from lingxi.core.admin.management_card import ADMIN_ACTION_GRANT, ADMIN_ACTION_SUPPRESS
from lingxi.core.admin.notification import (
    DECISION_CANCEL,
    DECISION_CONFIRM,
    AdminCardTransport,
    GroupNotifier,
    render_card_payload,
    render_group_notice,
    render_terminal_card,
)
from lingxi.core.admin.pending_action import (
    PendingAction,
    PendingActionAuditWriteFailed,
    PendingActionStatus,
    PendingActionTransientFailure,
)


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
    ) -> _ManagementRouteOutcome: ...


#: 管理卡逐行「收回」按钮没有独立的原因输入框（issue #439 B 档设计：一键收回，
#: 不为收回单独加一个表单）——服务端补一个固定原因，供审计与确认卡回显；管理员
#: 需要自定义收回原因时仍可用文本命令 ``/admin revoke_permission`` 自行填写。
_MANAGEMENT_CARD_REVOKE_REASON = "管理卡逐行收回"

#: 表单提交成功创建待确认操作时 ``AdminRouteOutcome.content_key`` 的取值——与
#: ``router.py`` 里 ``_dispatch_write_action`` 最终成功分支写死的字面量一致，
#: 是本模块判断"这次提交要不要提示成功 toast"的唯一依据（见 ``router.py`` 该
#: 分支的 ``content_key="admin.write_action_pending"``）。
_WRITE_ACTION_PENDING_CONTENT_KEY = "admin.write_action_pending"


def _toast_from_route_outcome(outcome: _ManagementRouteOutcome) -> dict[str, Any]:
    """把 ``AdminCommandRouter.route()`` 的结论翻译成管理卡交互的 toast 应答
    （不带 ``card`` 键——管理卡面板本身不因为一次表单提交而改变展示，管理员可以
    继续在同一张卡上发起下一次操作，见 ``core/admin/management_card.py`` 模块
    文档"管理卡不支持 update()"一节）。"""

    if not outcome.handled:
        # 结构上只会发生在"点击这一刻当前角色恰好已被撤销"——route() 内部会重新
        # 判定一次身份，不假设管理卡的历史交互权限仍然有效。
        return _toast_error("当前身份已无权限执行该操作，请重新查询 /admin user")
    toast_type = "success" if outcome.content_key == _WRITE_ACTION_PENDING_CONTENT_KEY else "error"
    return {"toast": {"type": toast_type, "content": outcome.reply_text}}


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
        management_actions: ManagementActionRouter | None = None,
    ) -> None:
        self._pending_actions = pending_actions
        self._confirm_cards = confirm_cards
        self._group_notifier = group_notifier
        self._group_chat_id = group_chat_id
        self._audit = audit
        # 未装配时管理卡的表单/按钮交互回复"该功能当前不可用"，不假装已经路由
        # 过任何命令——与本类既有确认/取消两个端口"未装配=构造时必须传入"不同
        # （#439 新增，向后兼容既有全部调用点：不传这个参数时行为逐字节不变）。
        self._management_actions = management_actions

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
    ) -> dict[str, Any]:
        """管理卡「新增授权 / 新增抑制」表单提交（#439 B 档新增交互分支）。

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
        if admin_action not in (ADMIN_ACTION_GRANT, ADMIN_ACTION_SUPPRESS):
            self._audit.record(
                "admin.card_callback.management_unknown_action",
                admin_action=admin_action,
                trace_id=trace_id,
            )
            return _toast_error("操作不存在或已失效")
        if not company_id:
            return _toast_error("请选择公司")
        if not metric_name:
            return _toast_error("请选择指标")
        if not reason.strip():
            return _toast_error("请填写原因")

        sub_command = "grant_permission" if admin_action == ADMIN_ACTION_GRANT else "suppress_permission"
        text = f"/admin {sub_command} {identifier} {company_id} {metric_name} {reason.strip()}"
        outcome = self._management_actions.route(
            open_id=operator_open_id,
            text=text,
            trace_id=trace_id,
            chat_id=chat_id,
            thread_id=thread_id,
            message_id=message_id,
        )
        return _toast_from_route_outcome(outcome)

    def handle_management_revoke(
        self,
        *,
        operator_open_id: str,
        override_id: str,
        chat_id: str,
        thread_id: str | None,
        message_id: str,
        trace_id: str,
    ) -> dict[str, Any]:
        """管理卡逐行「收回」按钮点击（#439 B 档新增交互分支）。

        ``override_id`` 是建卡时写进按钮 ``behaviors.value`` 的这一行内部标识，
        直接复用旧形状 ``/admin revoke_permission <override_id> <原因>``（见
        ``core/admin/commands.py`` 文档"形状 1"）——按钮点击这一刻已经精确知道
        是哪一行，不需要走 A 档新增的「标识+公司+指标」反查形状。
        """

        if self._management_actions is None:
            return _toast_error("该功能当前不可用，请改用文本命令")
        text = f"/admin revoke_permission {override_id} {_MANAGEMENT_CARD_REVOKE_REASON}"
        outcome = self._management_actions.route(
            open_id=operator_open_id,
            text=text,
            trace_id=trace_id,
            chat_id=chat_id,
            thread_id=thread_id,
            message_id=message_id,
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

        if pending.status is PendingActionStatus.PENDING:
            # 到这里仍是 pending 的唯一分支是点击人不是发起人——其余分支都会让
            # pending_action 落进某个终态（见 decide_confirm/decide_cancel）。
            return _toast_error("只有发起人本人可以操作此卡片")

        response: dict[str, Any] = {
            "toast": {
                "type": "success" if pending.status is PendingActionStatus.EXECUTED else "info",
                "content": _outcome_text(pending),
            }
        }
        if card_payload is not None:
            response["card"] = {"type": "raw", "data": card_payload}
        return response

    def _update_card_to_terminal(self, pending: PendingAction) -> dict[str, Any] | None:
        """渲染终态卡的 CardKit JSON，并尽力而为地出带外更新已发出的那张卡片。

        **返回值与出带外调用的成败无关**：``card_payload`` 在进入 ``try`` 块之前
        就已经算好，``self._confirm_cards.update()`` 失败只记审计、不影响返回值
        ——回调应答（``handle()`` 的主路径）需要在出带外更新失败时仍然携带正确
        的终态卡（模块文档「载体 #96」：应答换卡是主路径，出带外更新是冗余纵深）。
        """

        if pending.card_id is None:
            return None
        card = render_terminal_card(
            pending, target_label=pending.target_open_id, outcome_text=_outcome_text(pending)
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

    def _notify_group(self, pending: PendingAction) -> None:
        if self._group_notifier is None or not self._group_chat_id:
            return
        # 群通知只用 render_group_notice 内部按形状白名单渲染出的脱敏摘要（见该
        # 函数文档），不转发本次点击结论的原始 message 文本。
        text = render_group_notice(pending)
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


def _outcome_text(pending: PendingAction) -> str:
    if pending.status is PendingActionStatus.EXECUTED:
        return "已确认执行"
    if pending.status is PendingActionStatus.CANCELLED:
        return "已取消"
    if pending.status is PendingActionStatus.EXPIRED:
        return "已过期，未执行"
    if pending.status is PendingActionStatus.FAILED:
        return f"未执行（{pending.reason or '内部原因'}）"
    return "状态未知"  # pragma: no cover - 调用点已确保 status 非 PENDING
