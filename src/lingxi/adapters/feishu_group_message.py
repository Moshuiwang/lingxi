"""向管理群发一条纯文本消息（自建群发出站）。

**为什么不复用 #57 的出站层**：`adapters/feishu_outbound.py` 的 `LarkReplies` 只能
「回复某条消息」——它的目标由被回复的消息决定，正是为了让「必须发到同一私聊或同一话题」
由参数表保证。每日日报没有可回复的消息，它要**主动发进一个群**，这是另一个接口
（`im/v1/messages` 而不是 `im/v1/messages/:id/reply`）。「复用 #57」字面不可行，
编排者已核实并登记（Issue #52 决策登记）。

**为什么用标准库 urllib 而不是 `lark-oapi`**：同链上的 `adapters/feishu_directory.py`
已经用 urllib 直接调飞书开放平台，`scheduler` 组因此至今不含 `lark-oapi`
（见 pyproject.toml 该组的依据注释）。走 SDK 就要把 `lark-oapi` 加进 `scheduler` 运行时
依赖——为一个还没有真实调用方的职责扩大常驻进程的依赖面。用 urllib 则**本切片零新增
依赖**：`scheduler` 组、`PROCESS_RUNTIME_IMPORTS` 与 CI 的 extras 矩阵都不动。

**本模块的真实调用未验证（证据等级 1）**，与 `feishu_directory.py` 同一姿态：全部断言跑在
注入的假传输层上，真实群通知属 L4a（Issue #52「不在本批」明列）。

凭据边界：`app_secret` 只出现在**请求体**里，不进 URL、不进日志、不进异常消息。
群 ID 从环境变量注入，代码库里没有任何真实值（`V-花名册-28`）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any, Callable

from lingxi.adapters.feishu_directory import urllib_transport

logger = logging.getLogger(__name__)

# 飞书群聊 `chat_id` 的前缀。用它做格式校验，是为了让「把用户 open_id 误配成群 ID」
# 这类错配在**进程启动时**就失败，而不是等到当天第一次发日报时才失败。
GROUP_CHAT_ID_PREFIX = "oc_"


class FeishuGroupMessageError(RuntimeError):
    """群消息发送失败。``code`` 供程序判断，消息里不含群 ID、凭据或日报正文。

    ``definite`` 的含义与 :class:`lingxi.adapters.feishu_directory.FeishuDirectoryError`
    一致：飞书明确拒绝（收到业务错误码）为 ``True``，传输层异常与超时为 ``False``。
    """

    def __init__(self, code: str, *, definite: bool | None = None) -> None:
        super().__init__(f"飞书群消息发送失败：{code}")
        self.code = code
        self.definite = definite if definite is not None else code.startswith("feishu_code_")


def validate_group_chat_id(value: str) -> str:
    """校验群 ID 形状；不合法就快速失败，且**不回显取到的值**。

    群 ID 本身不是密钥，但它是一个外部标识：错误消息里带上它，就会被日志、CI 输出和
    工单一路复制出去。只报变量名足以定位问题。
    """

    text = (value or "").strip()
    if not text.startswith(GROUP_CHAT_ID_PREFIX) or len(text) <= len(GROUP_CHAT_ID_PREFIX):
        raise ValueError(
            f"环境变量 LINGXI_ADMIN_GROUP_CHAT_ID 必须是飞书群 chat_id（以 {GROUP_CHAT_ID_PREFIX} 开头），不回显收到的值"
        )
    if any(character.isspace() for character in text):
        raise ValueError("环境变量 LINGXI_ADMIN_GROUP_CHAT_ID 不得包含空白字符，不回显收到的值")
    return text


def _require_https(base_url: str) -> str:
    if not base_url.startswith("https://"):
        raise ValueError("飞书 base_url 必须以 https:// 开头（不回显收到的值）")
    return base_url.rstrip("/")


class FeishuGroupMessages:
    """向指定群发送纯文本消息。

    构造函数**只存参数**：不 import 任何 SDK、不建 client、不发请求、不读凭据文件
    （`V-花名册-27`；反例是 `adapters/feishu_onboarding.LarkCardSender`，它在 `__init__`
    里就把 SDK client 建了出来，于是任何想构造它的测试都被迫装上整个 SDK）。传输层由
    `transport` 注入，默认是 `feishu_directory` 里那份共用的 urllib 实现。
    """

    def __init__(
        self,
        *,
        base_url: str,
        app_id: str,
        app_secret: str,
        transport: Callable[..., Any] | None = None,
    ) -> None:
        self._base_url = _require_https(base_url)
        self._app_id = app_id
        self._app_secret = app_secret
        self._transport: Callable[..., Any] = transport or urllib_transport

    def _tenant_access_token(self) -> str:
        """取应用身份令牌。每次发送现取：日报一天一次，缓存换不来任何东西，
        却会多出一份「缓存过期与失效」的失败模式。"""

        response = self._transport(
            "POST",
            f"{self._base_url}/auth/v3/tenant_access_token/internal",
            body={"app_id": self._app_id, "app_secret": self._app_secret},
        )
        if not isinstance(response, Mapping):
            raise FeishuGroupMessageError("invalid_response_shape")
        code = response.get("code")
        if code not in (None, 0, "0"):
            raise FeishuGroupMessageError(f"feishu_code_{code}")
        token = response.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise FeishuGroupMessageError("missing_tenant_access_token")
        return token

    def send_text(self, *, chat_id: str, text: str) -> None:
        """向 `chat_id` 发一条 **纯文本** 消息。

        刻意只支持文本、不支持卡片：卡片能带按钮，而管理群通知**不得有任何可执行入口**
        （`V-花名册-24`）。想加按钮的人会先撞上这个方法签名里没有卡片这件事。
        """

        token = self._tenant_access_token()
        response = self._transport(
            "POST",
            f"{self._base_url}/im/v1/messages?receive_id_type=chat_id",
            body={
                "receive_id": chat_id,
                "msg_type": "text",
                # 飞书把 content 定义成一段 **JSON 字符串**，不是对象。
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
            token=token,
        )
        if not isinstance(response, Mapping):
            raise FeishuGroupMessageError("invalid_response_shape")
        code = response.get("code")
        if code not in (None, 0, "0"):
            raise FeishuGroupMessageError(f"feishu_code_{code}")
        # 只记「发过了」。群 ID 与日报正文都不进日志：正文虽已脱敏，但它是给管理群的，
        # 不是给运维日志的（`V-花名册-33`）。
        logger.info("管理群通知已发送 字符数=%s", len(text))
