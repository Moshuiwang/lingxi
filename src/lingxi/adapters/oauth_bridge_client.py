"""OAuth Bridge 的协议与仅出站 WebSocket 客户端。

Worker 只把一次性授权结果转发到已经认证的 biai-stage 连接；本模块不理解
首次开通或正式重授权的业务规则。业务调用方可以为一次性 state 注册专用处理器，
未注册的 state 才交给默认处理器，从而让不同授权入口共享传输而不共享写入路径。
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

_STATE_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,256}\Z")


class OAuthBridgeMessageHandler(Protocol):
    """默认消息处理器的可替换形状：接收一条已解析的 OAuth Bridge 消息。"""

    def process(self, message: OAuthBridgeMessage) -> None:
        """处理一条未命中 state 专用处理器的授权结果消息。"""
        ...


class OAuthBridgeResultSender(Protocol):
    """向 Worker 回传授权结果的可替换形状。"""

    def send_result(
        self,
        state: str,
        status: str,
        debug_identity: dict[str, str | None] | None = None,
        debug_details: dict[str, object] | None = None,
    ) -> None:
        """把某个 state 的授权结果（及可选调试信息）回传给 Worker。"""
        ...


@dataclass(frozen=True)
class OAuthBridgeMessage:
    """Worker 转发的一次性授权结果；不保存业务身份或令牌。"""

    type: str
    state: str
    code: str | None = None

    @classmethod
    def parse(cls, raw: str) -> OAuthBridgeMessage:
        """把一条原始 JSON 文本解析并校验为一条授权结果消息。"""
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("type") not in {
            "oauth_code",
            "oauth_cancelled",
        }:
            raise ValueError("未识别的 OAuth 桥接消息")
        state = value.get("state")
        if not isinstance(state, str) or _STATE_PATTERN.fullmatch(state) is None:
            raise ValueError("无效的 OAuth 状态")
        code = value.get("code")
        if value["type"] == "oauth_code" and (not isinstance(code, str) or not code):
            raise ValueError("授权结果缺少一次性 code")
        return cls(value["type"], state, code)


StateHandler = Callable[[OAuthBridgeMessage], None]


class OAuthBridgeClient:
    """可自动重连的出站 WebSocket 与按 state 分派器。

    Worker 只验证 state 是不透明随机值，不知道 state 属于哪条业务路径；这里的
    精确注册表把重授权 state 固定交给 E1 处理器。注册关系在当前进程内保持到
    ``stop``，因此重复投递不会在处理器消失后意外落入首次开通默认路径；E1 自己
    的一次性 state store 仍是最终消费与拒绝边界。
    """

    def __init__(
        self,
        url: str,
        token: str,
        processor: OAuthBridgeMessageHandler | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """接入连接地址、鉴权令牌与默认处理器；不建立连接。"""
        self._url = url
        self._token = token
        self._processor = processor
        self._sleep = sleep
        self._stop = threading.Event()
        self._socket: object | None = None
        self._state_handlers: dict[str, StateHandler] = {}
        self._state_handlers_lock = threading.RLock()

    def set_processor(self, processor: OAuthBridgeMessageHandler) -> None:
        """设置未注册 state 的默认处理器，保持首次开通兼容路径。"""
        self._processor = processor

    def register_state_handler(self, state: str, handler: StateHandler) -> None:
        """为当前授权 state 注册一次性业务处理器。"""
        if not isinstance(state, str) or _STATE_PATTERN.fullmatch(state) is None:
            raise ValueError("无效的 OAuth 状态")
        if not callable(handler):
            raise TypeError("OAuth state 处理器必须可调用")
        with self._state_handlers_lock:
            if state in self._state_handlers:
                raise ValueError("OAuth 状态已经注册处理器")
            self._state_handlers[state] = handler

    def handle_message(self, message: OAuthBridgeMessage) -> None:
        """处理一条已完成结构校验的消息；供 WebSocket 与注入测试共用。"""
        if not isinstance(message, OAuthBridgeMessage):
            raise TypeError("OAuth Bridge 消息类型无效")
        with self._state_handlers_lock:
            handler = self._state_handlers.get(message.state)
        if handler is not None:
            handler(message)
            return
        processor = self._processor
        if processor is not None:
            processor.process(message)

    def start(self) -> threading.Thread:
        """在后台 daemon 线程里启动 `run_forever`，返回该线程。"""
        thread = threading.Thread(target=self.run_forever, name="lingxi-oauth-bridge", daemon=True)
        thread.start()
        return thread

    def run_forever(self) -> None:
        """保持出站连接、断线重连，直到 `stop` 被调用。"""
        from websockets.sync.client import connect

        while not self._stop.is_set():
            try:
                with connect(
                    self._url,
                    additional_headers={"Authorization": f"Bearer {self._token}"},
                    open_timeout=10,
                ) as socket:
                    self._socket = socket
                    for raw in socket:
                        if self._stop.is_set():
                            break
                        try:
                            self.handle_message(OAuthBridgeMessage.parse(raw))
                        except ValueError:
                            continue
            except Exception:
                # 断线仅触发重连；不输出凭据、授权码或身份资料。
                self._sleep(3)
            finally:
                self._socket = None

    def send_result(
        self,
        state: str,
        status: str,
        debug_identity: dict[str, str | None] | None = None,
        debug_details: dict[str, object] | None = None,
    ) -> None:
        """把授权结果回传给 Worker；连接未就绪时静默丢弃。"""
        socket = self._socket
        if socket is None:
            return
        payload: dict[str, object] = {"type": "oauth_result", "state": state, "status": status}
        if debug_identity is not None:
            payload["debug_identity"] = debug_identity
        if debug_details is not None:
            payload["debug_details"] = debug_details
        socket.send(json.dumps(payload))

    def stop(self) -> None:
        """请求停止重连循环并关闭当前连接（若有）。"""
        self._stop.set()
        socket = self._socket
        if socket is not None:
            socket.close()


__all__ = [
    "OAuthBridgeClient",
    "OAuthBridgeMessage",
    "OAuthBridgeMessageHandler",
    "OAuthBridgeResultSender",
]
