"""scheduler 内的最小 MCP 协议处理器，身份只从受保护的 peer 绑定取得。"""

from __future__ import annotations

import json

from lingxi.core.admin.innertest import (
    PROTOCOL_VERSION,
    InnertestError,
    envelope,
    tool_schemas,
    validate_arguments,
)


class InnertestMcpSession:
    """每个连接协商版本，每个请求重新检查身份，连接不缓存权限。"""

    def __init__(self, *, peer_uid, service):
        """Service 是固定业务适配器，不是动态命令分发器。"""
        self.peer_uid, self.service = peer_uid, service
        self.initialized = False
        self.ready = False

    def handle(self, message):
        """返回 JSON-RPC 对象；通知不产生 stdout 回应。"""
        request_id = message.get("id") if isinstance(message, dict) else None
        try:
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise InnertestError("invalid_request")
            if set(message) - {"jsonrpc", "id", "method", "params"}:
                raise InnertestError("invalid_request")
            principal = self.service.authenticate(self.peer_uid)
            result = self._dispatch(message, principal)
            if "id" not in message:
                return None
            return dict(jsonrpc="2.0", id=request_id, result=result)
        except InnertestError as error:
            return dict(jsonrpc="2.0", id=request_id, error=dict(code=-32602, message=error.code))
        except Exception:
            return dict(
                jsonrpc="2.0", id=request_id, error=dict(code=-32603, message="roster_unavailable")
            )

    def _dispatch(self, message, principal):
        """只暴露三工具，未协商/不支持方法在进入业务前拒绝。"""
        method, params = message.get("method"), message.get("params", {})
        if not isinstance(params, dict):
            raise InnertestError("invalid_request")
        if method == "initialize":
            if params.get("protocolVersion") != PROTOCOL_VERSION:
                raise InnertestError("unsupported_protocol_version")
            self.initialized = True
            return dict(
                protocolVersion=PROTOCOL_VERSION,
                capabilities={"tools": {}},
                serverInfo={"name": "lingxi-innertest", "version": "1"},
            )
        if not self.initialized:
            raise InnertestError("not_initialized")
        if method == "notifications/initialized":
            self.ready = True
            return None
        if method == "ping":
            return {}
        if not self.ready:
            raise InnertestError("not_initialized")
        if method == "tools/list" and not params:
            return {"tools": tool_schemas()}
        if method != "tools/call" or set(params) - {"name", "arguments", "_meta"}:
            raise InnertestError("method_not_allowed")
        name, args = params.get("name"), params.get("arguments", {})
        try:
            validate_arguments(name, args)
            result = self.service.call(principal, name, args)
        except InnertestError as error:
            result = envelope(error.code, state="rejected")
        return dict(
            content=[{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            structuredContent=result,
            isError=not result["ok"],
        )
