"""Linux 独占合成环境验证；实际内核 peer 和标准 MCP 客户端，不连接外部服务。"""

import asyncio
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from lingxi.adapters.innertest_binding import load_binding
from lingxi.adapters.innertest_socket import InnertestSocketListener
from lingxi.core.admin.followup_budget import FollowupDatabaseBudget
from lingxi.core.admin.innertest import InnertestError, envelope


class Service:
    enabled = True

    def authenticate(self, uid):
        if uid != 1234 or not self.enabled:
            raise InnertestError("not_authenticated")
        return "bound"

    def call(self, principal, name, args):
        return envelope(state="pending", batch_id="synthetic-batch")


@unittest.skipUnless(sys.platform == "linux" and os.geteuid() == 0, "需独占 root Linux 合成容器")
class LinuxProtocolTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="innertest-", dir="/opt"))
        os.chown(self.root, 0, 1234)
        os.chmod(self.root, 0o750)
        self.path = str(self.root / "mcp.sock")
        self.service = Service()
        self.listener = InnertestSocketListener(
            path=self.path, service=self.service, db_slots=FollowupDatabaseBudget(), socket_gid=1234
        )
        self.listener.start()

    def tearDown(self):
        report = self.listener.drain_until(time.monotonic() + 2)
        self.assertEqual(report.still_running, 0)
        self.assertFalse(Path(self.path).exists())
        shutil.rmtree(self.root)

    def client_script(self, uid, gid, source):
        code = f"import os;os.setgroups([]);os.setgid({gid});os.setuid({uid});" + source
        return subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=10
        )

    def test_actual_peer_uid_acl_and_protected_mapping(self):
        source = f'import socket,json;s=socket.socket(socket.AF_UNIX);s.connect({self.path!r});s.sendall(b\'{{"jsonrpc":"2.0","id":1,"method":"initialize","params":{{"protocolVersion":"2025-11-25"}}}}\\n\');print(s.recv(8192).decode())'
        result = self.client_script(1234, 1234, source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("protocolVersion", result.stdout)
        result = self.client_script(1235, 1234, source)
        self.assertEqual(json.loads(result.stdout)["error"]["message"], "not_authenticated")
        result = self.client_script(1235, 1235, source)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PermissionError", result.stderr)
        binding = self.root / "binding.json"
        raw = dict(
            schema_revision=1,
            binding_id="synthetic",
            host_uid=1234,
            peer_uid=1234,
            uid_map_sha256=hashlib.sha256(Path("/proc/self/uid_map").read_bytes()).hexdigest(),
            socket_gid=1234,
        )
        binding.write_text(json.dumps(raw))
        self.assertEqual(load_binding(binding).peer_uid, 1234)
        raw["peer_uid"] = 65534
        binding.write_text(json.dumps(raw))
        with self.assertRaises(InnertestError):
            load_binding(binding)
        os.chmod(binding, 0o666)
        with self.assertRaisesRegex(InnertestError, "binding_not_protected"):
            load_binding(binding)

    def test_real_sdk_stdio_relay_three_tools(self):
        relay = self.root / "innertest_relay.py"
        shutil.copy("/work/scripts/admin/innertest_relay.py", relay)
        os.chmod(relay, 0o644)
        relay.with_name("innertest-relay.json").write_text(
            json.dumps(
                dict(schema_revision=1, socket_path=self.path, relay_uid=1234, socket_owner_uid=0)
            )
        )
        launcher = self.root / "launcher.py"
        launcher.write_text(
            "import os,runpy,sys\nos.setgroups([])\nos.setgid(1234)\nos.setuid(1234)\nsys.argv=["
            + repr(str(relay))
            + "]\nrunpy.run_path("
            + repr(str(relay))
            + ',run_name="__main__")\n'
        )

        async def run():
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            async with stdio_client(
                StdioServerParameters(command=sys.executable, args=["-I", "-S", str(launcher)])
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    hello = await session.initialize()
                    self.assertEqual(hello.protocolVersion, "2025-11-25")
                    tools = await session.list_tools()
                    self.assertEqual(len(tools.tools), 3)
                    result = await session.call_tool(
                        "prepare_innertest_additions",
                        {"request_key": "same", "emails": ["a@example.test"]},
                    )
                    self.assertFalse(result.isError)
                    self.service.enabled = False
                    with self.assertRaises(Exception):
                        await session.list_tools()

        asyncio.run(run())

    def test_four_connection_bound_and_stop_cleanup(self):
        clients = []
        try:
            for _ in range(5):
                s = socket.socket(socket.AF_UNIX)
                s.connect(self.path)
                clients.append(s)
            clients[-1].settimeout(2)
            self.assertEqual(clients[-1].recv(1), b"")
        finally:
            for s in clients:
                s.close()
