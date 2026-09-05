"""向用户本人主动发一张卡片（主动告知的出站面）。

发什么、什么时候发见 :mod:`lingxi.core.outreach`；本模块只把协议细节做对。与
:mod:`lingxi.adapters.feishu_user_message` 同一个接口（``im/v1/messages``、
``receive_id_type=open_id``），只是 ``msg_type`` 从 ``text`` 换成
``interactive``、``content`` 换成 schema 2.0 卡片 JSON。

**与 ``feishu_admin_card`` 刻意不合并**：那条链的收件人是管理员、卡片是回复
一条命令消息（靠"回复同一条私聊"决定落点）；这条链的收件人是具体自然人、
是**主动发起**的私聊，正文带着他自己的权限范围。合成一个类后"发错对象"退化成
一次传参错误；分开后各自在入口校验自己的收件人形状。

真实调用未验证：断言跑在注入的假传输层上，真实送达属 `biai-stage` L4a。凭据
边界：``app_secret`` 只出现在请求体里；``open_id`` 与卡片正文都不进日志。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from typing import Any

from lingxi.adapters.feishu_group_message import delivery_uuid
from lingxi.adapters.feishu_user_message import (
    FeishuUserMessageError,
    no_redirect_transport,
    validate_user_open_id,
)

logger = logging.getLogger(__name__)

#: 主动告知的投递去重 ID 前缀。与权限变化通知的 ``lingxi-perm-msg-`` 分开：同一个
#: uuid 命名空间下混两条投递语义，运维在飞书侧就分不出它属于哪一条链。
#: 取值恒为 16 + 32 = 48，仍在飞书的 50 字符上限内（由 ``delivery_uuid`` 校验）。
OUTREACH_UUID_PREFIX = "lingxi-outreach-"


class FeishuUserCardError(RuntimeError):
    """向用户发送卡片失败。``code`` 供程序判断，消息里不含 ``open_id``、凭据或正文。

    ``definite`` 的含义与 :class:`~lingxi.adapters.feishu_user_message.
    FeishuUserMessageError` 一致：飞书明确拒绝为 ``True``，传输层异常与超时为
    ``False``。
    """

    def __init__(self, code: str, *, definite: bool | None = None) -> None:
        """记录错误码，并按 `definite` 或错误码前缀判定是否为飞书明确拒绝。"""
        super().__init__(f"飞书用户卡片发送失败：{code}")
        self.code = code
        self.definite = definite if definite is not None else code.startswith("feishu_code_")


def _require_https(base_url: str) -> str:
    if not base_url.startswith("https://"):
        raise ValueError("飞书 base_url 必须以 https:// 开头（不回显收到的值）")
    return base_url.rstrip("/")


class FeishuUserCards:
    """向指定用户主动发送一张 schema 2.0 卡片。

    构造函数**只存参数**：不 import 任何 SDK、不建 client、不发请求、不读凭据文件。
    默认传输复用 ``feishu_user_message.no_redirect_transport``——"不跟随 3xx"是一条
    安全性质（一个 302 就能把应用身份令牌转发到别处），复制第二份就会漂移；它抛出的
    异常在本模块边界翻译成 :class:`FeishuUserCardError`，错误码逐字保留。
    """

    def __init__(
        self,
        *,
        base_url: str,
        app_id: str,
        app_secret: str,
        transport: Callable[..., Any] | None = None,
        uuid_prefix: str = OUTREACH_UUID_PREFIX,
    ) -> None:
        """接入卡片发送所需的凭据、传输层与去重前缀；不建 client、不发请求。"""
        self._base_url = _require_https(base_url)
        self._app_id = app_id
        self._app_secret = app_secret
        self._transport: Callable[..., Any] = transport or no_redirect_transport
        self._uuid_prefix = uuid_prefix

    def _call(self, method: str, url: str, **kwargs: Any) -> Any:
        """调一次传输层，并把共用实现的异常翻译进本模块的错误命名空间。"""
        try:
            return self._transport(method, url, **kwargs)
        except FeishuUserMessageError as error:
            raise FeishuUserCardError(error.code, definite=error.definite) from error

    def _tenant_access_token(self) -> str:
        """取应用身份令牌。每次发送现取，不缓存。

        与 ``feishu_user_message`` 的同名方法同形，是一处**知情的重复**：两个模块各自
        拥有自己的错误码命名空间，抽成公共函数就得让它抛第三种异常再由两边翻译一次。
        重复的是协议细节，不是任何判定。
        """
        response = self._call(
            "POST",
            f"{self._base_url}/auth/v3/tenant_access_token/internal",
            body={"app_id": self._app_id, "app_secret": self._app_secret},
        )
        code = _response_code(response)
        if code is not None:
            raise FeishuUserCardError(f"feishu_code_{code}")
        token = response.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise FeishuUserCardError("missing_tenant_access_token")
        return token

    def send_card(self, *, open_id: str, card: Mapping[str, Any], dedupe_key: str) -> str | None:
        """向 ``open_id`` 这个人主动发一张卡片，返回平台回读标识 ``message_id``。

        ``dedupe_key`` 标识"这是同一次逻辑发送"，首发与全部重试必须传同一个值，
        做成必填参数而不是可选：忘了传就等于回到"结果不明时必然重复投递"。返回
        ``message_id`` 是为了 L4a 的双通道核对（平台回读 + 服务端记录）；平台
        没给标识时返回 ``None``，不伪造一个。
        """
        receiver = validate_user_open_id(open_id)
        if not isinstance(card, Mapping) or not card:
            raise ValueError("卡片载荷不能为空")
        if not isinstance(dedupe_key, str) or not dedupe_key.strip():
            raise ValueError("发送去重键不能为空")
        token = self._tenant_access_token()
        response = self._call(
            "POST",
            f"{self._base_url}/im/v1/messages?receive_id_type=open_id",
            body={
                "receive_id": receiver,
                "msg_type": "interactive",
                # 飞书把 content 定义成一段 **JSON 字符串**，不是对象。
                "content": json.dumps(dict(card), ensure_ascii=False),
                "uuid": delivery_uuid(receiver, dedupe_key, prefix=self._uuid_prefix),
            },
            token=token,
        )
        code = _response_code(response)
        if code is not None:
            raise FeishuUserCardError(f"feishu_code_{code}")
        message_id = _message_id(response)
        # 只记「发过了」。open_id 与卡片正文都不进日志：正文里是这个人的权限范围。
        logger.info("主动告知卡片已发送 段数=%s", len(card.get("body", {}).get("elements", ())))
        return message_id


def _response_code(response: Any) -> Any:
    """返回飞书业务错误码；成功（``0``/缺省）返回 ``None``。"""
    if not isinstance(response, Mapping):
        raise FeishuUserCardError("invalid_response_shape")
    code = response.get("code")
    return None if code in (None, 0, "0") else code


def _message_id(response: Mapping[str, Any]) -> str | None:
    """从发送响应里取平台回读标识；取不到返回 ``None``，不伪造。"""
    data = response.get("data")
    if not isinstance(data, Mapping):
        return None
    value = data.get("message_id")
    return value if isinstance(value, str) and value else None


__all__ = [
    "OUTREACH_UUID_PREFIX",
    "FeishuUserCardError",
    "FeishuUserCards",
]
