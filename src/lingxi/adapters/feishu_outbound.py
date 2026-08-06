"""飞书出站：加表情与发文本。

按 2026-08-06 决策走官方 ``lark-oapi``。``lark_oapi`` 在**函数内延迟导入**，与仓库既有
惯例一致（``adapters/feishu_onboarding.py``）：不碰这两个类的测试无需装 SDK。

出站约束以[接口设计「四、飞书出站」](../../../docs/技术设计/接口设计.md)为准：
加表情失败不阻断后续处理（本模块只负责**抛出**，不阻断的语义由管线的
``_add_reaction`` 承担）；发送文本必须发到同一私聊或同一话题。

**本模块的真实行为未验证（证据等级 1）。** 全部 L2 断言跑在管线注入的假实现上，
真实加表情与真实发文本属 `V-接入-07a` 与 L4a 受控验收。
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# 收到消息即加的那个表情。合同只规定"添加一个表情"，没有规定是哪一个；
# 选 OnIt（飞书内置的"处理中"表情）贴近"已经收到"的语义。改这个值不改变任何承诺。
RECEIPT_EMOJI = "OnIt"


def build_client(*, app_id: str, app_secret: str) -> Any:
    """构造官方 SDK 客户端。

    凭据只从调用方传入，不在本模块读环境变量（代码框架第三节：配置在 ``apps/`` 入口
    一次性读取）。
    """

    import lark_oapi as lark

    return lark.Client.builder().app_id(app_id).app_secret(app_secret).build()


class LarkReactions:
    """加表情。实现 ``core.conversation.ports.Reactions``。"""

    def __init__(self, client: Any, *, emoji_type: str = RECEIPT_EMOJI) -> None:
        self._client = client
        self._emoji_type = emoji_type

    def add(self, *, message_id: str) -> None:
        from lark_oapi.api.im.v1 import (
            CreateMessageReactionRequest,
            CreateMessageReactionRequestBody,
            Emoji,
        )

        body = (
            CreateMessageReactionRequestBody.builder()
            .reaction_type(Emoji.builder().emoji_type(self._emoji_type).build())
            .build()
        )
        request = (
            CreateMessageReactionRequest.builder().message_id(message_id).request_body(body).build()
        )
        response = self._client.im.v1.message_reaction.create(request)
        if not response.success():
            # 抛出而不是静默返回：管线要能把"加表情失败"记进审计。不抛的话
            # `V-接入-08` 的注入失败用例就无从触发。
            raise RuntimeError(
                f"加表情失败：code={response.code} msg={response.msg} log_id={response.get_log_id()}"
            )


class LarkReplies:
    """发文本。实现 ``core.conversation.ports.Replies``。"""

    def __init__(self, client: Any) -> None:
        self._client = client

    def send_text(
        self, *, chat_id: str, thread_id: str | None, reply_to_message_id: str, text: str
    ) -> None:
        """回复触发本次处理的那条消息。

        用「回复」而不是「向 chat 发新消息」，是为了让接口设计的「必须发到同一私聊或
        同一话题」由构造保证：话题里的消息带 ``reply_in_thread``，回进同一话题；主窗口
        的消息就是普通回复。``chat_id`` 保留在签名里供实现选择与记录，本实现不需要它
        ——回复的目标由被回复的消息决定，这比自己拼 chat_id 少一次出错机会。
        """

        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

        # content 是一段 **JSON 字符串**（飞书如此定义），不是对象。
        content = json.dumps({"text": text}, ensure_ascii=False)
        body = (
            ReplyMessageRequestBody.builder()
            .content(content)
            .msg_type("text")
            .reply_in_thread(thread_id is not None)
            .build()
        )
        request = (
            ReplyMessageRequest.builder()
            .message_id(reply_to_message_id)
            .request_body(body)
            .build()
        )
        response = self._client.im.v1.message.reply(request)
        if not response.success():
            raise RuntimeError(
                f"发送文本失败：code={response.code} msg={response.msg} log_id={response.get_log_id()}"
            )
