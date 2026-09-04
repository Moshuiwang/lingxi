"""gateway 的审计出口：结构化日志。

审计表属后续切片；管线只依赖审计端口的签名，届时换实现不动管线。这里**不记录消息
正文**——未开通用户的内容"不保存"包括不写进日志。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("lingxi.apps.gateway")

# 后缀规则之外的显式名单：这些动作不以失败后缀结尾，但它们是"用户发了消息却什么都
# 没发生"的唯一入站侧证据。名单**只收**这一类；有明确用户回复的拒绝分支（未开通、
# 已停用、群聊越界）不在此列，停机期间跳过回复也不在此列。
_EXTRA_WARNING_ACTIONS = frozenset({"message.unsupported_type"})


class LoggingAudit:
    """审计出口的当前实现：把动作名与字段打成一行结构化日志。

    失败类动作升到 ``WARNING`` 而不是 ``INFO``：真实验收里「已收到」表情缺失时，
    唯一能回答"加表情调用到底怎么失败的"的证据就是那一行审计，而它淹没在 INFO 级
    正常流水里没被捕获，问题因此无法定位。级别只影响可见性，动作名与字段不变。
    """

    _EXTRA_WARNING_ACTIONS = _EXTRA_WARNING_ACTIONS

    def record(self, action: str, /, **fields: object) -> None:
        """记一条审计；失败类动作与显式名单里的动作升到 ``WARNING``。"""
        promote = (
            action.endswith(("failed", "error", "unparsable"))
            or action in self._EXTRA_WARNING_ACTIONS
        )
        log = logger.warning if promote else logger.info
        log("audit %s %s", action, fields)
