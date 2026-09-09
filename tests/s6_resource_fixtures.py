"""共同资源样本的合成首聊执行者与真实Unix客户端。"""

import json
import os
import socket
import threading
import time
from types import SimpleNamespace

from lingxi.core.admin.followup_budget import FollowupDatabaseBudget
from lingxi.core.conversation.ports import OnboardingResult, OnboardingState


class ObservedBudget(FollowupDatabaseBudget):
    def __init__(self):
        super().__init__()
        self.guard = threading.Lock()
        self.active = self.peak = self.listener = self.listener_peak = 0

    def acquire(self, blocking=True, timeout=None):
        nested = getattr(self._local, "depth", 0) > 0
        ok = super().acquire(blocking, timeout)
        if ok and not nested:
            with self.guard:
                self.active += 1
                self.peak = max(self.peak, self.active)
                if threading.current_thread().name == "lingxi-innertest-mcp":
                    self.listener += 1
                    self.listener_peak = max(self.listener_peak, self.listener)
        return ok

    def release(self):
        if getattr(self._local, "depth", 0) == 1:
            with self.guard:
                self.active -= 1
                if threading.current_thread().name == "lingxi-innertest-mcp":
                    self.listener -= 1
        super().release()


class SyntheticRunner:
    def __init__(self, fixture, executor, budget):
        self.fixture, self.executor, self.budget = fixture, executor, budget
        self.system_calls = []
        self.normal_calls = []

    def start_system(self, *, email, trace_id, initiated_by_open_id):
        del trace_id, initiated_by_open_id
        number = email.split("@")[0]
        self.system_calls.append((number, threading.current_thread().name))
        with self.budget:
            self.fixture.sql(
                "INSERT INTO app_user(id,feishu_open_id,feishu_user_id,feishu_union_id,display_name,department,tenant_key,provisioning_state,permission_version) VALUES(%s,%s,%s,%s,'合成','合成','synthetic','active',1)",
                ("usr_" + number, "ou_" + number, "fs_" + number, "un_" + number),
            )
            self.fixture.sql(
                "INSERT INTO publish_outbox(id,user_id,permission_version,reason,payload,status,published_at) VALUES(%s,%s,1,'synthetic','{}','published',now())",
                ("pub_" + number, "usr_" + number),
            )
        return SimpleNamespace(failure_reason=None)

    def start(self, *, event_id, open_id, trace_id, claim_token=None):
        del claim_token, trace_id

        def work():
            self.normal_calls.append((event_id, open_id, threading.current_thread().name))
            time.sleep(0.4)

        accepted = self.executor.submit(work)
        return OnboardingResult(
            state=OnboardingState.STARTED if accepted else OnboardingState.INTERNAL_ERROR,
            failure_reason=None if accepted else "capacity_pending",
        )


def socket_clients(path, batch_id, pipe):
    os.setgroups([])
    os.setgid(1234)
    os.setuid(1234)
    streams = []
    count = 0
    try:
        for _ in range(4):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(4)
            sock.connect(path)
            stream = sock.makefile("rwb", buffering=0)
            stream.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2025-11-25"},
                    }
                ).encode()
                + b"\n"
            )
            response = json.loads(stream.readline())
            if "error" in response:
                raise RuntimeError(response)
            stream.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            streams.append((sock, stream))
        pipe.send("ready")
        while not pipe.poll():
            for _, stream in streams:
                stream.write(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {
                                "name": "get_innertest_batch",
                                "arguments": {"batch_id": batch_id},
                            },
                        }
                    ).encode()
                    + b"\n"
                )
                response = json.loads(stream.readline())
                if "error" in response:
                    raise RuntimeError(response)
                count += 1
            time.sleep(0.05)
        pipe.recv()
        pipe.send(count)
    finally:
        for sock, stream in streams:
            stream.close()
            sock.close()
