"""单接收线程、四连接上限的本机 MCP listener，停止共享 scheduler 截止。"""

from __future__ import annotations

import json
import os
import selectors
import socket
import struct
import threading
import time

from lingxi.adapters.innertest_mcp import InnertestMcpSession
from lingxi.adapters.innertest_request import remaining, request_window
from lingxi.adapters.innertest_socket_path import SocketPathOwner
from lingxi.core.admin.followup import ShutdownReport

MAX_BYTES = 65536
MAX_CONNECTIONS = 4
REQUEST_SECONDS = 5


def peer_uid(connection):
    """Linux 内核返回的 UID 是唯一身份输入；其他平台不假定等价。"""
    if not hasattr(socket, "SO_PEERCRED"):
        raise RuntimeError("SO_PEERCRED_required")
    return struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]


class InnertestSocketListener:
    """每个短请求串行占一个数据库槽，网络等待不占数据库槽。"""

    def __init__(self, *, path, service, db_slots, socket_gid=None, stop=None):
        """只保存固定服务端配置，不从客户端接收路径或 UID。"""
        self.path, self.service, self.db_slots = path, service, db_slots
        self.socket_gid = socket_gid
        self._path_owner = SocketPathOwner(path)
        self._stop = threading.Event() if stop is None else stop
        self._thread = None
        self._server = None
        self._accepted = self._finished = 0

    def start(self):
        """独占锁覆盖绑定到退出，强杀后的遗留路径须先证明无人监听。"""
        if self._thread is not None or self._stop.is_set():
            return
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._path_owner.acquire()
            server.bind(self.path)
            self._path_owner.remember_bound()
            os.chmod(self.path, 0o660)
            if self.socket_gid is not None:
                os.chown(self.path, -1, self.socket_gid)
            server.listen(MAX_CONNECTIONS)
            server.setblocking(False)
            self._server = server
            self._thread = threading.Thread(
                target=self._run, name="lingxi-innertest-mcp", daemon=True
            )
            self._thread.start()
        except Exception:
            server.close()
            self._path_owner.close()
            self._thread = None
            raise

    def request_stop(self):
        """先关闭接收标记；事件循环最多一百毫秒观察到停止。"""
        self._stop.set()

    def drain_until(self, deadline_monotonic):
        """不延长 scheduler 的绝对截止，明确报告仍在运行的短请求。"""
        self.request_stop()
        if self._thread is not None:
            self._thread.join(max(0, deadline_monotonic - time.monotonic()))
        running = int(self._thread is not None and self._thread.is_alive())
        return ShutdownReport(
            accepted=self._accepted, finished=self._finished, still_running=running
        )

    def _run(self):
        """连接状态有界；空闲/半包五秒关闭，不能占住开通池。"""
        selector = selectors.DefaultSelector()
        clients = {}
        try:
            selector.register(self._server, selectors.EVENT_READ)
            while not self._stop.is_set():
                for key, _ in selector.select(0.1):
                    if self._stop.is_set():
                        break
                    if key.fileobj is self._server:
                        self._accept(selector, clients)
                    else:
                        self._read(selector, clients, key.fileobj)
                for client, state in tuple(clients.items()):
                    if time.monotonic() >= state[2]:
                        self._close(selector, clients, client)
        finally:
            for client in tuple(clients):
                self._close(selector, clients, client)
            selector.close()
            self._server.close()
            self._path_owner.close()

    def _accept(self, selector, clients):
        """第五连接立即拒绝；认证来自实际连接凭据。"""
        client, _ = self._server.accept()
        try:
            if len(clients) >= MAX_CONNECTIONS:
                client.close()
                return
            session = InnertestMcpSession(peer_uid=peer_uid(client), service=self.service)
            client.setblocking(False)
            clients[client] = [bytearray(), session, time.monotonic() + REQUEST_SECONDS]
            selector.register(client, selectors.EVENT_READ)
        except Exception:
            client.close()

    def _read(self, selector, clients, client):
        """协议不支持批量请求；超大行在 JSON 解析前拒绝。"""
        state = clients[client]
        try:
            data = client.recv(MAX_BYTES + 1)
            if not data:
                self._close(selector, clients, client)
                return
            state[0].extend(data)
            if len(state[0]) > MAX_BYTES:
                raise ValueError("request_too_large")
            while b"\n" in state[0] and not self._stop.is_set():
                line, _, remainder = state[0].partition(b"\n")
                state[0] = bytearray(remainder)
                self._accepted += 1
                with request_window():
                    if not self.db_slots.acquire(timeout=remaining()):
                        raise ValueError("request_timeout")
                    try:
                        response = state[1].handle(json.loads(line))
                    finally:
                        self.db_slots.release()
                if response is not None:
                    client.settimeout(REQUEST_SECONDS)
                    client.sendall(json.dumps(response, ensure_ascii=False).encode() + b"\n")
                    client.setblocking(False)
                self._finished += 1
                state[2] = time.monotonic() + REQUEST_SECONDS
        except (OSError, ValueError):
            self._close(selector, clients, client)

    @staticmethod
    def _close(selector, clients, client):
        """连接取消不回滚已经提交的业务请求。"""
        selector.unregister(client)
        clients.pop(client, None)
        client.close()
