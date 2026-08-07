"""gateway 进程级断言：真实进程、真实信号、真实套接字表。

认领断言：`V-部署-03`（``SIGTERM`` 后停止接收新事件、完成在途事件、超时内退出）、
`V-接入-10`（进程不监听任何入站端口）。

这两条都必须在**真实进程**上验证：单测里 import 一个模块然后断言"它没调用 bind"
证明不了任何事——真正要问的是这个进程在操作系统的套接字表里有没有留下监听项。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"

# 子进程脚本：用假传输层跑真实的 supervisor 与真实的信号处理器。
# 不连飞书、不连数据库——本文件断的是进程行为，不是业务逻辑。
CHILD_SCRIPT = """
import sys, time, threading
sys.path.insert(0, {source!r})

from lingxi.adapters.feishu_longconn import BackoffPolicy, LongConnectionSupervisor
from lingxi.apps.gateway import install_signal_handlers

handled = []
draining = {{"value": False}}


# 一直有事件进来。
class ForeverTransport:
    def stream(self):
        while True:
            yield {{"header": {{"event_id": "evt", "event_type": "im.message.receive_v1"}}}}
            time.sleep(0.02)


# 连接活着但没有任何用户消息——夜间与周末的常态。只产出空闲心跳，
# 真实传输层在这种状态下就是这样：阻塞在队列上等，每隔一个轮询间隔让出一次控制权。
class IdleTransport:
    def stream(self):
        while True:
            time.sleep(0.02)
            yield None


def handle(payload):
    # 在途事件：故意慢一点，让 SIGTERM 有机会落在处理中间。
    time.sleep(0.05)
    handled.append(payload)
    print("HANDLED", len(handled), flush=True)


stop_event = threading.Event()
install_signal_handlers(stop_event)

supervisor = LongConnectionSupervisor(
    transport={transport}(),
    handle_event=handle,
    backoff=BackoffPolicy(base_seconds=0.01, factor=2.0, ceiling_seconds=0.1),
    sleep=lambda seconds: None,
)
print("READY", flush=True)
reason = supervisor.run(should_stop=stop_event.is_set)
print("EXIT", reason.value, len(handled), flush=True)
"""


def _listening_socket_inodes() -> set[str]:
    """从 ``/proc/net/tcp{,6}`` 读出所有处于 LISTEN 状态的套接字 inode。"""

    inodes: set[str] = set()
    for name in ("tcp", "tcp6"):
        path = Path("/proc/net") / name
        if not path.exists():
            continue
        for line in path.read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) > 9 and fields[3] == "0A":  # 0A = TCP_LISTEN
                inodes.add(fields[9])
    return inodes


def _process_socket_inodes(pid: int) -> set[str]:
    """一个进程持有的全部套接字 inode。"""

    inodes: set[str] = set()
    fd_dir = Path("/proc") / str(pid) / "fd"
    try:
        entries = list(fd_dir.iterdir())
    except OSError:
        return inodes
    for entry in entries:
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if target.startswith("socket:["):
            inodes.add(target[len("socket:[") : -1])
    return inodes


class GatewayProcessTestCase(unittest.TestCase):
    def _spawn(self, transport: str = "ForeverTransport") -> subprocess.Popen:
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                CHILD_SCRIPT.format(source=str(SOURCE_ROOT), transport=transport),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self._reap, process)
        # 等它进入运行状态
        deadline = time.time() + 15
        while time.time() < deadline:
            line = process.stdout.readline()
            if line.startswith("READY"):
                return process
            if process.poll() is not None:
                self.fail(f"子进程提前退出：{process.stderr.read()}")
        self.fail("子进程没有在超时内就绪")

    def _reap(self, process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        # 显式关闭管道：不关会在 CI 里刷一片 ResourceWarning，把真正的告警淹掉。
        for stream in (process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()


class GracefulShutdownTests(GatewayProcessTestCase):
    """`V-部署-03`：``SIGTERM`` 后完成在途事件并在超时内退出。"""

    def test_sigterm_drains_and_exits_within_timeout(self) -> None:
        process = self._spawn()
        # 让它先处理几条事件，确保 SIGTERM 落在运行中而不是启动期
        deadline = time.time() + 10
        while time.time() < deadline:
            if process.stdout.readline().startswith("HANDLED"):
                break

        sent_at = time.time()
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.fail("收到 SIGTERM 后没有在超时内退出")
        elapsed = time.time() - sent_at

        self.assertEqual(process.returncode, 0, "优雅停机应以 0 退出")
        self.assertLess(elapsed, 20, "停机必须在超时内完成")
        remaining = process.stdout.read()
        self.assertIn("EXIT stopped", remaining, "必须以「已停止」而不是异常终止")

    def test_sigterm_is_seen_even_when_no_events_are_arriving(self) -> None:
        """**连接空闲时也必须停得下来。**

        独立复查实测出的阻塞项：传输层不带超时地阻塞在队列上时，supervisor 只在
        收到一条事件之后才检查停机信号，于是「当前没有用户消息」这个最常见的状态下
        ``SIGTERM`` 根本不会被看见，进程一直挂到编排层 SIGKILL。

        原先的用例用的是每 20ms 无限产出事件的传输层——恰好是那个设计唯一能工作的
        情形，所以它是绿的。这条用例换成只产出空闲心跳的传输层，直接打在缺陷上：
        把 supervisor 里跳过心跳、检查停机信号的那一段去掉，本条必须变红。
        """

        process = self._spawn(transport="IdleTransport")
        time.sleep(0.3)

        sent_at = time.time()
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.fail("连接空闲时收到 SIGTERM 没有退出——停机依赖于恰好有事件进来")
        elapsed = time.time() - sent_at

        self.assertEqual(process.returncode, 0)
        self.assertLess(elapsed, 10, "空闲连接的停机不得慢于一个轮询间隔加一点余量")
        tail = process.stdout.read()
        self.assertIn("EXIT stopped 0", tail, "空闲期间不应处理过任何事件")

    def test_no_new_events_are_handled_after_the_stop_signal(self) -> None:
        """停机后不再接收新事件：退出时的计数相对信号前不再增长。"""

        process = self._spawn()
        deadline = time.time() + 10
        last_seen = 0
        while time.time() < deadline:
            line = process.stdout.readline()
            if line.startswith("HANDLED"):
                last_seen = int(line.split()[1])
                if last_seen >= 2:
                    break

        process.send_signal(signal.SIGTERM)
        process.wait(timeout=20)

        tail = process.stdout.read()
        exit_line = [line for line in tail.splitlines() if line.startswith("EXIT")]
        self.assertTrue(exit_line, f"没有看到退出行：{tail}")
        final_count = int(exit_line[0].split()[2])
        self.assertIn("stopped", exit_line[0])
        # 收到信号后至多再完成**一条**在途事件，不得继续消费。
        self.assertLessEqual(
            final_count,
            last_seen + 1,
            f"停机后仍在处理新事件：信号前 {last_seen} 条，退出时 {final_count} 条",
        )


class NoInboundPortTests(GatewayProcessTestCase):
    """`V-接入-10`：gateway 进程不监听任何入站端口。"""

    @unittest.skipUnless(Path("/proc/net/tcp").exists(), "跳过：本平台没有 /proc/net/tcp")
    def test_process_holds_no_listening_socket(self) -> None:
        process = self._spawn()
        time.sleep(0.5)

        listening = _listening_socket_inodes()
        held = _process_socket_inodes(process.pid)

        self.assertEqual(
            held & listening,
            set(),
            "gateway 进程不得持有任何处于 LISTEN 状态的套接字："
            "事件只能经已认证的长连接通道进入，不存在第二个入站入口",
        )

    def test_the_check_would_catch_a_listening_socket(self) -> None:
        """反向验证上一条的判据真的会变红——否则它可能只是"什么都没查到"。"""

        script = (
            "import socket, time, sys\n"
            "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(1)\n"
            "print('READY', flush=True)\n"
            "time.sleep(30)\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True
        )
        self.addCleanup(self._reap, process)
        self.assertTrue(process.stdout.readline().startswith("READY"))
        time.sleep(0.3)

        held = _process_socket_inodes(process.pid)
        self.assertNotEqual(
            held & _listening_socket_inodes(),
            set(),
            "判据本身失效：一个真的在监听的进程都没被检出",
        )


if __name__ == "__main__":
    unittest.main()
