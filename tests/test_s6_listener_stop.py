"""停止到达时，已开始的短事务可完成，缓冲区剩余请求不得继续接收。"""

import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from lingxi.adapters.innertest_socket import InnertestSocketListener
from lingxi.core.admin.followup_budget import FollowupDatabaseBudget
from lingxi.core.admin.innertest import envelope


@unittest.skipUnless(hasattr(socket, "SO_PEERCRED"), "需要Linux实际socket")
class ListenerStopTests(unittest.TestCase):
    def test_buffered_requests_after_stop_are_not_started(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        class Service:
            def authenticate(self, uid):
                return uid

            def call(self, principal, name, args):
                calls.append(name)
                entered.set()
                release.wait(2)
                return envelope(state="pending")

        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            path = str(Path(directory) / "s.sock")
            listener = InnertestSocketListener(
                path=path, service=Service(), db_slots=FollowupDatabaseBudget()
            )
            listener.start()
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(3)
            try:
                client.connect(path)
                initialize = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-11-25"},
                }
                client.sendall(json.dumps(initialize).encode() + b"\n")
                self.assertIn(b"protocolVersion", client.recv(8192))
                request = {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "get_innertest_batch",
                        "arguments": {"batch_id": "synthetic"},
                    },
                }
                client.sendall(
                    b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
                    + (json.dumps(request).encode() + b"\n") * 3
                )
                self.assertTrue(entered.wait(2))
                listener.request_stop()
                release.set()
                report = listener.drain_until(time.monotonic() + 3)
                self.assertEqual(report.still_running, 0)
                self.assertEqual(calls, ["get_innertest_batch"])
                self.assertFalse(Path(path).exists())
            finally:
                release.set()
                client.close()
                listener.drain_until(time.monotonic() + 3)
