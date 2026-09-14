import unittest

from lingxi.adapters.innertest_mcp import InnertestMcpSession
from lingxi.core.admin.innertest import (
    PROTOCOL_VERSION,
    InnertestError,
    ToolRegistry,
    ToolSpec,
    envelope,
    validate_arguments,
)
from lingxi.core.admin.restricted_tools import CHANNEL_TOOLS, READ_ONLY_TOOL_NAMES

SIX_TOOLS = (
    "list_innertest_members",
    "prepare_innertest_additions",
    "get_innertest_batch",
    "get_user_status",
    "get_user_permission_sources",
    "get_pending_actions",
)
VALID_ARGUMENTS = {
    "list_innertest_members": {},
    "prepare_innertest_additions": {"request_key": "key", "emails": ["x@example.test"]},
    "get_innertest_batch": {"batch_id": "b"},
    "get_user_status": {"identifier": "ou_x"},
    "get_user_permission_sources": {"identifier": "ou_x"},
    "get_pending_actions": {},
}


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

    def notify(self, method, params=None):
        return self.session.handle(dict(jsonrpc="2.0", method=method, params=params or {}))

    def initialize(self):
        result = self.call("initialize", {"protocolVersion": PROTOCOL_VERSION})
        self.assertEqual(result["result"]["protocolVersion"], PROTOCOL_VERSION)
        self.call("notifications/initialized")

    def test_six_tools_and_strict_schema(self):
        self.initialize()
        tools = self.call("tools/list")["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], list(SIX_TOOLS))
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

    def test_unknown_tool_name_is_invalid_request(self):
        self.initialize()
        for name in ("confirm_pending_action", "execute", "run_sql", "", None):
            result = self.call("tools/call", {"name": name, "arguments": {}})
            self.assertTrue(result["result"]["isError"])
            self.assertEqual(result["result"]["structuredContent"]["code"], "invalid_request")
        self.assertEqual(self.service.calls, 0)

    def test_any_client_version_gets_the_only_supported_version(self):
        for version in ("unknown", "2024-11-05", "2026-07-28", None):
            session = InnertestMcpSession(peer_uid=1234, service=self.service)
            params = {} if version is None else {"protocolVersion": version}
            result = session.handle(dict(jsonrpc="2.0", id=1, method="initialize", params=params))
            self.assertEqual(result["result"]["protocolVersion"], PROTOCOL_VERSION)

    def test_unknown_method_and_binding_revoke(self):
        self.initialize()
        for method in ("execute", "resources/read", "shell", "tools/confirm"):
            self.assertIn("error", self.call(method))
        self.service.enabled = False
        self.assertEqual(self.call("tools/list")["error"]["message"], "not_authenticated")

    def test_notifications_never_produce_a_frame(self):
        self.assertIsNone(self.notify("notifications/cancelled", {"requestId": 9}))
        self.initialize()
        for method in (
            "notifications/cancelled",
            "notifications/progress",
            "notifications/roots/list_changed",
            "notifications/unknown",
        ):
            self.assertIsNone(self.notify(method, {"anything": 1}))
        self.assertIsNone(self.notify("execute"))
        self.assertIn("error", self.call("execute"))
        self.service.enabled = False
        self.assertIsNone(self.notify("notifications/initialized"))
        malformed = self.session.handle({"not": "jsonrpc"})
        self.assertEqual(
            (malformed["id"], malformed["error"]["message"]), (None, "invalid_request")
        )

    def test_tools_list_accepts_cursor_and_meta_only(self):
        self.initialize()
        for params in (
            {"cursor": "abc"},
            {"_meta": {"progressToken": 1}},
            {"cursor": "a", "_meta": {}},
        ):
            self.assertEqual(len(self.call("tools/list", params)["result"]["tools"]), 6)
        self.assertIn("error", self.call("tools/list", {"filter": "x"}))

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

    def test_read_only_tool_arguments_are_strict(self):
        validate = CHANNEL_TOOLS.validate
        validate("get_user_status", {"identifier": "ou_x"})
        validate("get_user_status", {"trace_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV"})
        for args in ({}, {"identifier": "a", "trace_id": "b"}, {"identifier": ""}, {"trace_id": 1}):
            with self.assertRaises(InnertestError):
                validate("get_user_status", args)
        for args in ({}, {"identifier": "x" * 129}, {"identifier": "a", "open_id": "b"}):
            with self.assertRaises(InnertestError):
                validate("get_user_permission_sources", args)
        validate("get_pending_actions", {})
        validate("get_pending_actions", {"limit": 20, "status": "pending"})
        validate("get_pending_actions", {"pending_action_id": "pac_1"})
        for args in (
            {"limit": 0},
            {"limit": 21},
            {"limit": True},
            {"status": "done"},
            {"pending_action_id": "pac_1", "limit": 1},
            {"initiated_by": "ou_x"},
        ):
            with self.assertRaises(InnertestError):
                validate("get_pending_actions", args)

    def test_registry_rejects_duplicate_names(self):
        spec = ToolSpec(
            name="get_user_status",
            description="重名",
            input_schema=dict(type="object", properties={}, additionalProperties=False),
            validate=lambda args: args,
        )
        with self.assertRaises(ValueError):
            ToolRegistry([spec, spec])
        self.assertEqual(CHANNEL_TOOLS.names, SIX_TOOLS)
        self.assertEqual(READ_ONLY_TOOL_NAMES, SIX_TOOLS[3:])

    def test_unbound_uid_is_rejected_for_every_tool(self):
        session = InnertestMcpSession(peer_uid=999, service=self.service)
        for name in SIX_TOOLS:
            result = session.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": VALID_ARGUMENTS[name]},
                }
            )
            self.assertEqual(result["error"]["message"], "not_authenticated")
        self.assertEqual(self.service.calls, 0)

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
        for name in SIX_TOOLS:
            result = session.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": name,
                        "arguments": VALID_ARGUMENTS[name],
                        "_meta": {"uid": 1234, "open_id": "ou_admin"},
                    },
                }
            )
            self.assertEqual(result["error"]["message"], "not_authenticated")

    def test_arguments_cannot_supply_identity(self):
        self.initialize()
        for name in SIX_TOOLS:
            for extra in ("open_id", "peer_uid", "binding_id", "initiated_by", "role"):
                result = self.call(
                    "tools/call", {"name": name, "arguments": {**VALID_ARGUMENTS[name], extra: "x"}}
                )
                self.assertEqual(result["result"]["structuredContent"]["code"], "invalid_request")
        self.assertEqual(self.service.calls, 0)

    def test_registry_revocation_rejects_every_tool_on_the_next_request(self):
        self.initialize()
        for name in SIX_TOOLS:
            result = self.call("tools/call", {"name": name, "arguments": VALID_ARGUMENTS[name]})
            self.assertFalse(result["result"]["isError"])
        self.assertEqual(self.service.calls, 6)
        self.service.enabled = False
        for name in SIX_TOOLS:
            result = self.call("tools/call", {"name": name, "arguments": VALID_ARGUMENTS[name]})
            self.assertEqual(result["error"]["message"], "not_authenticated")
        self.assertEqual(self.service.calls, 6)
