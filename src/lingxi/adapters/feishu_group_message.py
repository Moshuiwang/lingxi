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

import hashlib
import json
import logging
from collections.abc import Mapping
from typing import Any, Callable

from lingxi.adapters.feishu_directory import urllib_transport

logger = logging.getLogger(__name__)

# 飞书群聊 `chat_id` 的前缀。用它做格式校验，是为了让「把用户 open_id 误配成群 ID」
# 这类错配在**进程启动时**就失败，而不是等到当天第一次发日报时才失败。
GROUP_CHAT_ID_PREFIX = "oc_"

# 投递去重 ID。飞书的 `im/v1/messages` 接受一个开发者自备的 `uuid`，用于**服务端**
# 对重复请求去重。长度上限 50，这里的取值恒为 14 + 32 = 46。
DELIVERY_UUID_PREFIX = "lingxi-roster-"
DELIVERY_UUID_MAX_LENGTH = 50
SendOutcomeCallback = Callable[[str, bool], None]


def delivery_uuid(chat_id: str, dedupe_key: str, *, prefix: str = DELIVERY_UUID_PREFIX) -> str:
    """由「接收方 ID + 去重键」算出稳定的投递去重 ID。

    **它解决的是"不确定态"，不是跨重启幂等。** HTTP POST 已经被飞书收下、而响应在
    回程超时的那一刻，调用方无法区分"没发出去"和"发出去了但没听见回音"——重试是唯一
    选择，而重试就会重复投递。带上同一个 `uuid` 之后，重试请求在服务端被认作同一条，
    这条路径因此从"确定会重复"变成"由平台去重"。

    取哈希而不是拼原值：群 ID 是外部标识，拼进 `uuid` 会让它出现在请求体的第二个位置，
    也会随日志与错误上下文扩散。同样输入永远得到同样输出，这正是重试要复用它的前提。

    **平台侧的去重窗口是飞书的属性，本切片未验证**（真实调用属 L4a）。因此本模块能
    承诺的是"重试一定携带同一个 `uuid`"这个**代码事实**，不是"平台一定不会重复投递"
    这个**平台行为**；后者要等 L4a 受控验收才能声称。

    `prefix` 参数化（Issue #156 / S-C-03b）：权限变化通知走的是同一个
    `im/v1/messages` 接口、需要的是同一种"重试携带同一个 uuid"的保证，但它是**另一条
    投递语义**——两条链共用同一个前缀，会让运维在飞书侧再也分不出一个 uuid 属于日报
    还是权限通知。取值域仍由长度上限守着（下面那一行按实际前缀算，不是按默认值算）。
    """

    digest = hashlib.sha256(f"{chat_id}\n{dedupe_key}".encode("utf-8")).hexdigest()[:32]
    if not isinstance(prefix, str) or not prefix.strip() or prefix.strip() != prefix:
        raise ValueError("投递去重 ID 的前缀必须是不含首尾空白的非空文本")
    value = f"{prefix}{digest}"
    if len(value) > DELIVERY_UUID_MAX_LENGTH:
        raise ValueError("投递去重 ID 超过飞书的 50 字符上限")
    return value


class FeishuGroupMessageError(RuntimeError):
    """群消息发送失败。``code`` 供程序判断，消息里不含群 ID、凭据或日报正文。

    ``definite`` 的含义与 :class:`lingxi.adapters.feishu_directory.FeishuDirectoryError`
    一致：飞书明确拒绝（收到业务错误码）为 ``True``，传输层异常与超时为 ``False``。
    """

    def __init__(self, code: str, *, definite: bool | None = None) -> None:
        super().__init__(f"飞书群消息发送失败：{code}")
        self.code = code
        self.definite = definite if definite is not None else code.startswith("feishu_code_")


def validate_group_chat_id(
    value: str, *, variable_name: str = "LINGXI_ADMIN_GROUP_CHAT_ID"
) -> str:
    """校验群 ID 形状；不合法就快速失败，且**不回显取到的值**。

    群 ID 本身不是密钥，但它是一个外部标识：错误消息里带上它，就会被日志、CI 输出和
    工单一路复制出去。只报变量名足以定位问题。``variable_name`` 可覆盖——scheduler
    与 gateway 各自读取自己命名空间下的变量（`LINGXI_ADMIN_GROUP_CHAT_ID` /
    `LINGXI_GATEWAY_ADMIN_GROUP_CHAT_ID`，Issue #153），错误消息必须指向调用方
    真正读取的那一个，否则会把运维导向去改一个不存在效果的变量。
    """

    text = (value or "").strip()
    if not text.startswith(GROUP_CHAT_ID_PREFIX) or len(text) <= len(GROUP_CHAT_ID_PREFIX):
        raise ValueError(
            f"环境变量 {variable_name} 必须是飞书群 chat_id（以 {GROUP_CHAT_ID_PREFIX} 开头），不回显收到的值"
        )
    if any(character.isspace() for character in text):
        raise ValueError(f"环境变量 {variable_name} 不得包含空白字符，不回显收到的值")
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
        on_send_outcome: SendOutcomeCallback | None = None,
    ) -> None:
        self._base_url = _require_https(base_url)
        self._app_id = app_id
        self._app_secret = app_secret
        self._transport: Callable[..., Any] = transport or urllib_transport
        self._on_send_outcome = on_send_outcome

    def _notify_send(self, operation: str, succeeded: bool) -> None:
        """把结果交给告警逻辑；告警回调失败不能改变发送语义。"""

        if self._on_send_outcome is None:
            return
        try:
            self._on_send_outcome(operation, succeeded)
        except Exception as error:  # noqa: BLE001 - 观察者不是发送链路的一部分
            logger.error(
                "飞书发送结果观察失败，发送结果不变 operation=%s error=%s",
                operation,
                type(error).__name__,
            )

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

    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None:
        """向 `chat_id` 发一条 **纯文本** 消息。

        刻意只支持文本、不支持卡片：卡片能带按钮，而管理群通知**不得有任何可执行入口**
        （`V-花名册-24`）。想加按钮的人会先撞上这个方法签名里没有卡片这件事。

        `dedupe_key` 标识"这是同一次逻辑投递"。调用方对同一天的日报（含失败重试）必须
        传同一个值，由 :func:`delivery_uuid` 折成飞书的 `uuid` 字段。做成**必填参数**
        而不是可选：忘了传就等于回到"不确定态必然重复投递"，那正是这次要修的缺陷，
        不该由一个默认值悄悄承担。

        取令牌那一步没有 `uuid`：它是幂等的读操作，重复取令牌不产生任何用户可见后果。
        """

        try:
            token = self._tenant_access_token()
            response = self._transport(
                "POST",
                f"{self._base_url}/im/v1/messages?receive_id_type=chat_id",
                body={
                    "receive_id": chat_id,
                    "msg_type": "text",
                    # 飞书把 content 定义成一段 **JSON 字符串**，不是对象。
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                    "uuid": delivery_uuid(chat_id, dedupe_key),
                },
                token=token,
            )
            if not isinstance(response, Mapping):
                raise FeishuGroupMessageError("invalid_response_shape")
            code = response.get("code")
            if code not in (None, 0, "0"):
                raise FeishuGroupMessageError(f"feishu_code_{code}")
        except Exception:
            self._notify_send("message_final", False)
            raise
        self._notify_send("message_final", True)
        # 只记「发过了」。群 ID 与日报正文都不进日志：正文虽已脱敏，但它是给管理群的，
        # 不是给运维日志的（`V-花名册-33`）。
        logger.info("管理群通知已发送 字符数=%s", len(text))
