import unittest

from lingxi.adapters.innertest_mcp import InnertestMcpSession
from lingxi.core.admin.innertest import (
    PROTOCOL_VERSION,
    InnertestError,
    envelope,
    validate_arguments,
)


class Service:
    enabled = True
    calls = 0

    def authenticate(self, uid):
        if uid != 1234 or not self.enabled:
            raise InnertestError("not_authenticated")
        return "server-bound"

    def call(self, principal, name, args):
        self.calls += 1
        return envelope()


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.service = Service()
        self.session = InnertestMcpSession(peer_uid=1234, service=self.service)

    def call(self, method, params=None):
        return self.session.handle(dict(jsonrpc="2.0", id=1, method=method, params=params or {}))

    def initialize(self):
        result = self.call("initialize", {"protocolVersion": PROTOCOL_VERSION})
        self.assertEqual(result["result"]["protocolVersion"], PROTOCOL_VERSION)
        self.call("notifications/initialized")

    def test_three_tools_and_strict_schema(self):
        self.initialize()
        tools = self.call("tools/list")["result"]["tools"]
        self.assertEqual(len(tools), 3)
        for tool in tools:
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
        for extra in ("open_id", "initiated_by", "role", "command", "sql"):
            result = self.call(
                "tools/call",
                {
                    "name": "prepare_innertest_additions",
                    "arguments": {"request_key": "key", "emails": ["x@example.test"], extra: "x"},
                },
            )
            self.assertTrue(result["result"]["isError"])
        self.assertEqual(self.service.calls, 0)

    def test_unknown_version_method_and_binding_revoke(self):
        self.assertIn("error", self.call("initialize", {"protocolVersion": "unknown"}))
        self.initialize()
        for method in ("execute", "resources/read", "shell", "tools/confirm"):
            self.assertIn("error", self.call(method))
        self.service.enabled = False
        self.assertEqual(self.call("tools/list")["error"]["message"], "not_authenticated")

    def test_twenty_raw_limit_and_xor(self):
        with self.assertRaisesRegex(InnertestError, "too_many_targets"):
            validate_arguments(
                "prepare_innertest_additions", {"request_key": "k", "emails": ["a@b.test"] * 21}
            )
        for args in ({}, {"batch_id": "a", "request_key": "b"}, {"batch_id": False}):
            with self.assertRaises(InnertestError):
                validate_arguments("get_innertest_batch", args)
        with self.assertRaises(InnertestError):
            validate_arguments("list_innertest_members", {"limit": True})

    def test_meta_cannot_supply_identity(self):
        session = InnertestMcpSession(peer_uid=999, service=self.service)
        result = session.handle(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "initialize",
                "params": {"protocolVersion": PROTOCOL_VERSION, "_meta": {"uid": 1234}},
            }
        )
        self.assertEqual(result["error"]["message"], "not_authenticated")
