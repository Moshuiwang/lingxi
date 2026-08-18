"""向**用户本人**主动发一条纯文本消息（权限变化通知的出站面）。

[Issue #156](https://github.com/Moshuiwang/lingxi/issues/156) 的 S-C-03b。什么时候发、
发什么字，见 :mod:`lingxi.core.permission.notification`；本模块只把协议细节做对。

**与 :mod:`lingxi.adapters.feishu_group_message` 的关系**：同一个飞书接口
（``im/v1/messages``），只是 ``receive_id_type`` 从 ``chat_id`` 换成 ``open_id``。
沿用同一套姿态——urllib、**零新增依赖**、构造函数只存参数不建 client、去重 ``uuid``
由同一个 :func:`~lingxi.adapters.feishu_group_message.delivery_uuid` 算（前缀不同，
理由见那个函数的文档）。

**为什么不把两者合成一个类**：两条链的**收件人语义**完全不同，而这正是最不该被一个
参数悄悄切换的东西。管理群那条有一条硬约束——正文里不得有任何可执行入口
（`V-花名册-24`），且收件人是一个受控群；这条的收件人是**具体的自然人**，正文里带着
他自己的权限范围。合成一个类之后，"发错对象"退化成一次传参错误：把用户 open_id 传给
日报、或者把群 ID 传给权限通知，两次都能发出去。分成两个类之后，两边各自在入口校验
自己的收件人形状（``ou_`` / ``oc_``），传错在**发出去之前**就失败。

**本模块的真实调用未验证（证据等级 1）**，与 ``feishu_group_message.py`` 同一姿态：
全部断言跑在注入的假传输层上。「重试携带同一个 ``uuid``」是**代码事实**；「飞书因此
一定不会重复投递」是**平台行为**，未验证，属 L4a。同样未验证的还有：应用能不能主动
向一个从未与它对话过的用户发起私聊（真实私聊行为）。这两条沿用既有登记，不在本切片
声称。

凭据边界：``app_secret`` 只出现在**请求体**里，不进 URL、不进日志、不进异常消息；
``open_id`` 是外部标识，不进错误消息与日志正文；通知正文一个字都不进日志。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any, Callable

from lingxi.adapters.feishu_directory import urllib_transport
from lingxi.adapters.feishu_group_message import delivery_uuid

logger = logging.getLogger(__name__)

#: 飞书用户 ``open_id`` 的前缀。用它做形状校验，让"把群 ``chat_id`` 当成用户 open_id
#: 传进来"在**发出去之前**失败，而不是把一个人的权限范围发进一个群。
USER_OPEN_ID_PREFIX = "ou_"

#: 权限变化通知的投递去重 ID 前缀。与日报的 ``lingxi-roster-`` 分开：同一个 uuid 命名
#: 空间下混两条投递语义，运维在飞书侧就再也分不出它属于哪一条链。
#: 取值恒为 17 + 32 = 49，仍在飞书的 50 字符上限内（由 ``delivery_uuid`` 校验）。
NOTICE_UUID_PREFIX = "lingxi-perm-msg-"


class FeishuUserMessageError(RuntimeError):
    """用户私聊消息发送失败。``code`` 供程序判断，消息里不含 ``open_id``、凭据或正文。

    ``definite`` 的含义与 :class:`lingxi.adapters.feishu_group_message.
    FeishuGroupMessageError` 一致：飞书明确拒绝（收到业务错误码）为 ``True``，
    传输层异常与超时为 ``False``。
    """

    def __init__(self, code: str, *, definite: bool | None = None) -> None:
        super().__init__(f"飞书用户消息发送失败：{code}")
        self.code = code
        self.definite = definite if definite is not None else code.startswith("feishu_code_")


def validate_user_open_id(value: str, *, label: str = "用户 open_id") -> str:
    """校验用户 ``open_id`` 形状；不合法就快速失败，且**不回显取到的值**。

    只报字段名足以定位问题。这道校验挡的不是"格式洁癖"，而是**收件人错位**：
    群 ``chat_id`` 以 ``oc_`` 开头，用户 ``open_id`` 以 ``ou_`` 开头，两者在类型上
    都是字符串，混用不会有任何报错——只会把一个人的权限范围发进一个群。
    """

    text = (value or "").strip()
    if not text.startswith(USER_OPEN_ID_PREFIX) or len(text) <= len(USER_OPEN_ID_PREFIX):
        raise ValueError(
            f"{label}必须是飞书用户 open_id（以 {USER_OPEN_ID_PREFIX} 开头），不回显收到的值"
        )
    if any(character.isspace() for character in text):
        raise ValueError(f"{label}不得包含空白字符，不回显收到的值")
    return text


def _require_https(base_url: str) -> str:
    if not base_url.startswith("https://"):
        raise ValueError("飞书 base_url 必须以 https:// 开头（不回显收到的值）")
    return base_url.rstrip("/")


class FeishuUserMessages:
    """向指定用户发送纯文本消息。

    构造函数**只存参数**：不 import 任何 SDK、不建 client、不发请求、不读凭据文件
    （纪律同 :class:`lingxi.adapters.feishu_group_message.FeishuGroupMessages`）。
    传输层由 ``transport`` 注入，默认是 ``feishu_directory`` 里那份共用的 urllib 实现。
    """

    def __init__(
        self,
        *,
        base_url: str,
        app_id: str,
        app_secret: str,
        transport: Callable[..., Any] | None = None,
        uuid_prefix: str = NOTICE_UUID_PREFIX,
    ) -> None:
        self._base_url = _require_https(base_url)
        self._app_id = app_id
        self._app_secret = app_secret
        self._transport: Callable[..., Any] = transport or urllib_transport
        self._uuid_prefix = uuid_prefix

    def _tenant_access_token(self) -> str:
        """取应用身份令牌。每次发送现取。

        **与 ``feishu_group_message`` 的同名方法逐字同形**，这是一处**知情的重复**：
        两个模块各自拥有自己的错误码命名空间（``FeishuUserMessageError`` /
        ``FeishuGroupMessageError``），把这段抽成公共函数就得让它抛第三种异常再由两边
        翻译一次，改动面反而落到一个已经交付并验收过的模块上。重复的是 12 行协议细节，
        不是任何判定；两边的行为各自由自己的用例钉住。
        """

        response = self._transport(
            "POST",
            f"{self._base_url}/auth/v3/tenant_access_token/internal",
            body={"app_id": self._app_id, "app_secret": self._app_secret},
        )
        if not isinstance(response, Mapping):
            raise FeishuUserMessageError("invalid_response_shape")
        code = response.get("code")
        if code not in (None, 0, "0"):
            raise FeishuUserMessageError(f"feishu_code_{code}")
        token = response.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise FeishuUserMessageError("missing_tenant_access_token")
        return token

    def send_text(self, *, open_id: str, text: str, dedupe_key: str) -> None:
        """向 ``open_id`` 这个人发一条**纯文本**消息。

        刻意只支持文本、不支持卡片：权限变化通知是一条告知，**没有任何可操作入口**——
        用户要做的事（发起一次查询、联系管理员）都在正文里说清楚了，加按钮只会制造一个
        我们还没有定义过语义的回调面。

        ``dedupe_key`` 标识"这是同一次逻辑通知"。同一次变化的首发与全部重试必须传同一个
        值（调用方用 ``(用户, 权限版本)``），由 :func:`~lingxi.adapters.
        feishu_group_message.delivery_uuid` 折成飞书的 ``uuid`` 交服务端去重。做成
        **必填参数**而不是可选：忘了传就等于回到"结果不明时必然重复投递"。

        取令牌那一步没有 ``uuid``：它是幂等的读操作，重复取令牌不产生任何用户可见后果。
        """

        receiver = validate_user_open_id(open_id)
        if not isinstance(text, str) or not text.strip():
            # 空正文发出去就是一条噪声消息，而且它一定来自渲染侧的缺陷。
            raise ValueError("通知正文不能为空")
        if not isinstance(dedupe_key, str) or not dedupe_key.strip():
            raise ValueError("通知去重键不能为空")
        token = self._tenant_access_token()
        response = self._transport(
            "POST",
            f"{self._base_url}/im/v1/messages?receive_id_type=open_id",
            body={
                "receive_id": receiver,
                "msg_type": "text",
                # 飞书把 content 定义成一段 **JSON 字符串**，不是对象。
                "content": json.dumps({"text": text}, ensure_ascii=False),
                "uuid": delivery_uuid(receiver, dedupe_key, prefix=self._uuid_prefix),
            },
            token=token,
        )
        if not isinstance(response, Mapping):
            raise FeishuUserMessageError("invalid_response_shape")
        code = response.get("code")
        if code not in (None, 0, "0"):
            raise FeishuUserMessageError(f"feishu_code_{code}")
        # 只记「发过了」。open_id 与通知正文都不进日志：正文里是这个人的权限范围。
        logger.info("权限变化通知已发送 字符数=%s", len(text))


__all__ = [
    "FeishuUserMessageError",
    "FeishuUserMessages",
    "NOTICE_UUID_PREFIX",
    "USER_OPEN_ID_PREFIX",
    "validate_user_open_id",
]
