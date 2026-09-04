"""gateway 的审计出口：结构化日志。

审计表属后续切片；管线只依赖 ``AuditSink`` 的签名，届时换实现不动管线。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("lingxi.apps.gateway")

class _LoggingAudit:
    """审计出口的当前实现：结构化日志。

    ``audit_event`` 表属后续切片；管线只依赖 ``AuditSink`` 的签名，届时换实现不动管线。
    这里**不记录消息正文**——未开通用户的内容"不保存"包括不写进日志。

    失败类动作（``reaction.failed``、``reply.failed``、``event.handler_failed``、
    ``event.unparsable`` 等）记 ``WARNING`` 而不是 ``INFO``：S-A-07 r15/r19 真实验收
    发现「已收到」表情缺失（#175/#185）时，唯一能回答"加表情调用到底怎么失败的"
    的证据就是 ``reaction.failed`` 这一行审计——它淹没在 INFO 级正常流水里，验收
    没有捕获到，问题因此无法定位。级别只影响日志可见性，动作名与字段不变，
    重放脚本 ``_AuditCapture``（level=INFO 的 Handler）仍照常收到这些记录。

    后缀规则之外还有一个显式名单（独立审核 F5）：``message.unsupported_type``
    不以失败后缀结尾，但它是"用户发了消息却什么都没发生"的唯一入站侧证据
    （非文本消息被判不支持、不建任务）——r19 首轮误判正是这一类。名单只收
    **用户本应得到回应却什么都没发生**的动作。据此：未开通、已停用这类有明确
    用户回复的拒绝分支不在此列；``event.rejected_non_private_chat`` 也不在此列，
    但理由不同（PR #186 补审 P3-6，Issue #318 修订边界描述）——**群聊边界从
    "完全静默"收窄为"默认静默，精确 @ 机器人本身时回一句固定引导"**：绝大多数
    群聊消息（未 @、@别人、未配置机器人 open_id、或同群刚发过还在节流窗口内）
    仍然不加表情、不回复、不入队，机器人不在群里暴露除这一句固定引导之外的任何
    工作痕迹；只有精确 @ 到机器人本身且未被节流的那一条消息，才会额外触发一次
    ``event.group_mention_hint_sent``（见 ``group_mention_hint.py`` 的
    ``GroupMentionHintResponder``）。
    后者本身带用户可见回复，不是"什么都没发生"，因此也不进
    ``_EXTRA_WARNING_ACTIONS``。``event.rejected_non_private_chat`` 这条审计在
    两种情况下都照常记录，维持 ``INFO``。停机期间的 ``reply.skipped_while_
    stopping`` 属正常停机路径，同样不在此列。
    """

    _EXTRA_WARNING_ACTIONS = frozenset({"message.unsupported_type"})

    def record(self, action: str, /, **fields: object) -> None:
        promote = (
            action.endswith(("failed", "error", "unparsable"))
            or action in self._EXTRA_WARNING_ACTIONS
        )
        log = logger.warning if promote else logger.info
        log("audit %s %s", action, fields)
