"""阶段结果的管理员只读文案，未知不能被表达为成功或失败。"""

from __future__ import annotations

_STAGE_LABELS = {
    "permission_recompute": "权限重算",
    "publish_observe": "权限下发观察",
    "terminal_card_refresh": "确认卡更新",
    "management_card_refresh": "管理卡更新",
    "group_notify": "管理群通知",
    "confirmation_card_send": "确认卡发送",
    "innertest_preprovision": "首次开通",
    "innertest_readiness_check": "可用状态检查",
}
_STATUS_LABELS = {
    "pending": "等待处理",
    "running": "正在处理",
    "retry_wait": "等待恢复",
    "succeeded": "已完成",
    "skipped": "未执行",
    "failed": "未完成",
    "unknown": "结果待核实",
}


def render_followups(items):
    """返回附加段落；仅展示固定状态标签和不透明追溯号。"""
    if not items:
        return ""
    lines = ["管理后台工作："]
    for item in items:
        stage = _STAGE_LABELS.get(item.stage, "待兼容的后台阶段")
        status = _STATUS_LABELS.get(item.status, "状态待核实")
        lines.append(
            f"- {stage}：{status} · 追溯号 {item.trace_id or item.pending_action_id}"
            f" · 工作号 {item.followup_id}"
        )
    return "\n".join(lines)
