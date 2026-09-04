"""管理命令面的文本回复渲染：把查询结果说成管理员看得懂的一段话。

**不回显内部标识**：外部标识、公司编号、指标标识一律经展示名端口翻译；翻译不出来时用
明确的占位说法，不退回原样回显——那等于把内部标识说给人听。
"""

from __future__ import annotations

from collections.abc import Sequence

from lingxi.config.content import ContentCatalog, RenderedContent, default_content_catalog
from lingxi.core.admin.commands import AdminRejectReason
from lingxi.core.admin.display_names import AdminDisplayNames
from lingxi.core.admin.views import (
    AdminEventView,
    AdminTraceView,
    AdminUserStatusView,
    GalaxySourceSummary,
    LocalPermissionOverrideView,
)


def render_help(roles: Sequence[str]) -> str:
    """帮助文案。

    术语与管理卡按钮、确认卡、终态卡、群通知共用同一份说法：「补充授权」「屏蔽指标」
    「撤销」。帮助里**只公开还在受理的命令**——补充授权统一走管理卡表单，已经撤除的命令
    不再出现在这里。撤销那一行也不再声称"覆盖标识见查询结果"：查询回显不再展示裸内部
    标识，已知标识时仍可直接使用，多数场景请改用「标识+公司+指标」形式或卡片按钮。
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
        "/admin revoke_permission <标识> <公司> <指标> <原因> — 发起撤销本地覆盖"
        "（需本人飞书确认卡片；服务端按标识+公司+指标反查覆盖ID）\n"
        "/admin revoke_permission <覆盖ID/权限组ID> <原因> — 已知 ID 时直接发起撤销"
        "（多数场景请改用上一行的标识+公司+指标形式，或使用管理卡撤销按钮）\n"
        f"当前角色：{role_line}"
    )


#: 覆盖行原因文本在 ``/admin user`` 回显时的截断长度：不回显 reason 全文，只给足够定位这是哪一次特批/收回的前 20 字预览——
#: 与群通知的脱敏纪律同一
#: 精神，管理员查询回显不是审计全文检索入口，完整原因见对应的确认卡终态文案
#: 或未来的审计检索。
_OVERRIDE_REASON_PREVIEW_LENGTH = 20

#: 覆盖方向取值 → 中文展示文案，只在这里（展示层）出现，
#: 不反向依赖纯逻辑层的枚举类型：展示文案就地维护一份。术语与确认卡、群通知三处同步。
_OVERRIDE_DIRECTION_LABEL: dict[str, str] = {"grant": "补充授权", "suppress": "屏蔽指标"}


def _render_local_overrides(
    overrides: Sequence[LocalPermissionOverrideView], *, display_names: AdminDisplayNames
) -> str:
    """``/admin user`` 回显的「当前生效本地覆盖」段。

    新职位+范围授权的展开行共享 ``permission_group_id``，因此在用户可见文本中也
    聚合成一个职位+范围项；只有历史 ``permission_group_id IS NULL`` 行维持逐行
    展示。无覆盖时返回一行「无本地覆盖」。

    **不展示内部覆盖标识**——它只留审计；管理员发起撤销时用「标识+公司+指标」形式或
    卡片按钮，都不需要先看到它。公司与指标一律翻成人类可读文本。
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


#: 零银河权限用户曾经有一句「本地授权暂不生效」的边界提示，**已撤销**：合并不再挂在
#: "有银河权限"判据之后，管理员对这类用户的本地授权现在无条件参与合并，那句提示已经
#: 不实。


#: 银河来源摘要"算不出来"的三个原因码 → 中文提示，与
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
    """用户查询回显里的「银河来源」段。

    仅供展示，不参与任何权限判定；公司编号一律翻成「中文名（编号）」。
    """
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


#: 内部标识前缀白名单：管理员可见文案零 ou_/lpo_/lpg_/pac_ 是
#: 结构性硬要求，即使这个值是管理员自己刚刚敲进来的输入也不例外——真正需要
#: 隐藏的是"这串文本长得像系统内部标识"这件事本身，与它的来源（系统生成 /
#: 管理员键入）无关。非内部 ID 形状的输入（多数情况下是邮箱，或管理员的一次
#: 手误）原样回显，不额外做资料查找。
_INTERNAL_ID_PREFIXES: tuple[str, ...] = ("ou_", "lpo_", "lpg_", "pac_")


def _safe_identifier_echo(identifier: str) -> str:
    if identifier.startswith(_INTERNAL_ID_PREFIXES):
        return "该用户"
    return identifier


#: 开通与账号状态机器码 → 中文：与数据库两个 CHECK 约束的取值域一一对应。未登记的取值
#: 原样展示、不当成异常——约束已经在数据库层面把取值收窄到这张表列出的成员，"未登记"
#: 分支只在约束被改而词表忘了同步时才会命中，失败开放比拒绝渲染整条回复更安全。
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

#: 追溯回显里的入站事件类型 → 中文。本模块不 import 适配器，因此这里独立登记一份取值，
#: 不反向依赖产生这些字面量的那个模块；未登记的取值走统一回退，不假装认识每一个未来
#: 可能新增的事件类型。
_EVENT_TYPE_LABEL: dict[str, str] = {
    "im.message.receive_v1": "用户消息",
    "card.action.trigger": "卡片按钮/表单交互",
}

#: 入站事件处理方式 → 中文：与
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

#: 开通失败原因机器码 → 中文，覆盖开通链与停摆收口现有登记的全部原因码。与上面两张表
#: 同一姿态：白名单式展示层翻译，不反向依赖产生这些字面量的具体模块；未登记的取值走统一
#: 回退，不崩、不假装认识。
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


#: 任务状态 → 中文：与数据库里 ``task`` 的
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

#: 任务失败机器码 → 中文，**同时覆盖两列**：细分失败码，与被压平成用户文案分类之后的
#: 粗粒度错误类别。细分码里的多种失败在粗粒度那一列全部塌进同一个"会话失败"，正是这里
#: 要消灭的"什么都看不出来"；反过来没有经过终态写入的失败（心跳超时回收、投递到期、
#: 排队超时）在细分列上恒为空，只有粗粒度说得出原因。因此回显按「有细分码用它，否则
#: 退回粗粒度」取值，一张表服务两列。日报另有一份只翻译粗粒度的词表，共有的词逐字同措辞。
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

# 文档投递状态 → 中文：文档消费在
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
    "unsupported_nested_blocks": "正文含无法定位的块结构",  # 表格已支持，这个码只剩"无处安放的块"
    # 改走服务端一次建档之后新增的三个码。不登记的后果不是报错，
    # 而是 `/admin trace` 把机器码原样显示成"body_too_long（未登记显示名）"——
    # 管理员看得懂但要多猜一步，而这三个码恰恰是他最常需要解释给用户听的三种。
    "body_too_long": "正文过长，已改走纯文本段落",
    "title_not_embeddable": "标题含特殊字符，已改走纯文本段落",
    "server_simplified_body": "飞书自动排版时简化了部分结构",
}


def _display_or_unregistered(value: str, table: dict[str, str]) -> str:
    """把一个机器码翻成中文；词表里没有时回退成"原值（未登记显示名）"。

    既不原样吞掉、也不假装认识：管理员至少能看到原始取值用于反馈，同时明确知道这是词表
    遗漏而不是真的没有这个状态，不会误以为系统坏了。
    """
    label = table.get(value)
    if label is None:
        return f"{value}（未登记显示名）"
    return label


def render_user_status(
    identifier: str, status: AdminUserStatusView | None, *, display_names: AdminDisplayNames
) -> str:
    """``/admin user`` 的文本回复（与管理卡并存，见 ``_dispatch`` 调用点）。

    查到用户时头部一律显示展示名端口翻译出来的姓名
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


def render_audit_query(
    identifier: str | None, window_hours: int, events: Sequence[AdminEventView]
) -> str:
    """最近事件列表的文本回显。

    传进来的标识已经反查过：邮箱反查失败时原样是那个邮箱，成功或直接输入外部标识时是
    外部标识。这里**不**再做一次用户资料查找——审计查询是高频诊断动作，多一次数据库往返
    不值得：非内部标识形状的值原样展示，内部标识形状退化为通用占位，满足"管理员可见文案
    里不出现内部标识"这条结构性要求。代价是外部标识场景下不显示姓名，这与用户状态回显会
    完整翻译不同——那里已经确认是一个真实存在的用户，多查一次换来更好的可读性。
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


def render_trace(trace_id: str, trace: AdminTraceView | None) -> str:
    """``/admin trace`` 的回显：管理员凭追溯号拿到一条链的经过。

    查无这个追溯号时给明确的「不存在」文案，不是空白也不是报错；查到了但没有失败原因时
    如实回「无失败记录」并带上当前能查到的开通状态——不能因为没有失败原因就假装这条追溯
    号也查无此人。

    Returns:
        一段可直接回复给管理员的多行文本。
    """
    if trace is None:
        return f"追溯号 {trace_id}：查无此追溯号"
    lines = [
        f"追溯号 {trace_id}：{trace.event_count} 条入站事件",
        f"首次接收: {trace.first_received_at}",
        # 事件类型与处理方式一律翻成中文，不直出内部枚举取值。
        f"最近事件类型: {_display_or_unregistered(trace.last_event_type, _EVENT_TYPE_LABEL)}",
        f"最近处理方式: "
        f"{_display_or_unregistered(trace.last_handled_as, _HANDLED_AS_LABEL) if trace.last_handled_as else '(未标记)'}",
        f"是否已认领: {'是' if trace.dispatched else '否'}",
    ]
    if trace.provisioning_state is not None:
        # 复用用户状态回显的同一份词表，不允许两处出现不同翻译。
        lines.append(
            f"开通状态: {_PROVISIONING_STATE_LABEL.get(trace.provisioning_state, trace.provisioning_state)}"
        )
        lines.append(
            f"账号状态: {_ACCOUNT_STATE_LABEL.get(trace.account_state, trace.account_state)}"
        )
    if trace.failure_reason is not None:
        lines.append(
            f"失败原因: {_display_or_unregistered(trace.failure_reason, _FAILURE_REASON_LABEL)}"
            f"（{trace.failure_event_type}，{trace.failure_occurred_at}）"
        )
    else:
        lines.append("无开通失败记录")
    lines.extend(_trace_task_lines(trace))
    lines.extend(_trace_document_lines(trace))
    return "\n".join(lines)


def _trace_task_lines(trace: AdminTraceView) -> list[str]:
    """这条追溯号派生的问数任务收口结果。

    没有派生任务时整段省略，不摆一排空值。在任务分类码落库之前，管理员唯一能拿到的是
    「无失败记录」——开通没失败、问数任务失败了，而任务那一侧的分类码与失败签名只进
    worker 的标准错误，管理员看不到。
    """
    if trace.task_status is None:
        return []
    suffix = f"（{trace.task_ended_at}）" if trace.task_ended_at is not None else ""
    lines = [f"任务结果: {_display_or_unregistered(trace.task_status, _TASK_STATUS_LABEL)}{suffix}"]
    # 有细分失败码用它，否则退回错误类别：没有经过终态写入的失败（心跳超时回收、投递
    # 到期、排队超时）在细分列上恒为空，只有错误类别说得出原因，不能因此整行消失。
    task_failure = trace.task_failure_code or trace.task_error_kind
    if task_failure is not None:
        lines.append(f"任务失败原因: {_display_or_unregistered(task_failure, _TASK_FAILURE_LABEL)}")
    if trace.task_failure_signature is not None:
        # 通常是底层异常**类型名**，不是异常正文；结构化外因也可能是固定分类签名，同样
        # 不是自由文本。这里不翻译——它是稳定的低敏标识、没有可枚举的取值域，翻译只能
        # 靠猜；管理员把它原样贴给研发就是最有用的一手信息。
        signature_label = (
            "失败签名" if trace.task_failure_code == "mcp_bad_gateway" else "底层异常类型"
        )
        lines.append(f"{signature_label}: {trace.task_failure_signature}")
    return lines


def _trace_document_lines(trace: AdminTraceView) -> list[str]:
    """文档交付结果。

    文档投递是任务收口**之后**由另一条独立消费循环完成的状态机，因此不能把"任务成功"
    当作文档已成功；降级事实只存在于检查点列里，必须在同一条回显里明确区分。
    """
    if trace.document_delivery_status is None:
        return []
    lines = [
        "文档交付结果: "
        + _display_or_unregistered(trace.document_delivery_status, _DOCUMENT_DELIVERY_STATUS_LABEL)
    ]
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
    return lines


#: 「以 ``/admin`` 开头但没解析成功」时，按失败落点告诉管理员**哪一段**没看懂。缺陷现场：
#: 连发三条命令都只收到同一句"未识别的管理命令"，发送者无从自救，也无法判断是邮箱被客户端
#: 自动链接化了还是公司那一段填了中文名。**刻意不回显管理员输入的原文**：出站是一条平台
#: 文本消息，而那种消息里的 @ 标记是有语义的，把输入原样拼进回复等于开一个可控文本的反射
#: 面；段名 ＋ 期望形状已经足够自救。
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

#: 闲聊得到的既有笼统文案键（正文逐字不变；移进
#: ``config/content.toml`` 的版本化目录）。管理命令面**没有 ``/admin`` 前缀预检**，
#: 管理员的任何一句闲聊都会走到 UNKNOWN；对它们做分段报错等于对每句闲聊解释语法。
_UNKNOWN_COMMAND_KEY = "admin.unknown_command"
#: 已判定出"哪一段没看懂"时的分段报错键。
_UNKNOWN_COMMAND_DETAIL_KEY = "admin.unknown_command_detail"


def render_unknown(
    reject_reason: AdminRejectReason | None,
    segment_count: int,
    catalog: ContentCatalog | None = None,
) -> RenderedContent:
    """``UNKNOWN`` 的回复：说清哪一段没看懂，以及实际分成了几段参数。

    ``segment_count`` 是管理员自救的关键事实——那次现场，管理员发的是"一个邮箱
    + 24"两段、解析器数出三段；只有把这个数字说出来，才可能意识到"客户端把邮箱
    拆开了"，而不是反复重发同一条命令。它来自分段计数，**不回显任何输入原文**。
    """
    catalog = catalog if catalog is not None else default_content_catalog()
    hint = _REJECT_HINTS.get(reject_reason) if reject_reason is not None else None
    if hint is None:
        return catalog.text(_UNKNOWN_COMMAND_KEY)
    return catalog.text(_UNKNOWN_COMMAND_DETAIL_KEY, hint=hint, segment_count=segment_count)
