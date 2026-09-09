"""真实本机 socket 生命周期故障，不连接数据库或外部平台。"""

import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from lingxi.adapters.innertest_socket import InnertestSocketListener


class SocketRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory(prefix="ix-", dir="/tmp")
        self.path = str(Path(self.root.name) / "admin.sock")
        self.listeners = []

    def tearDown(self):
        for listener in self.listeners:
            self.assertEqual(listener.drain_until(time.monotonic() + 2).still_running, 0)
        self.root.cleanup()

    def listener(self):
        listener = InnertestSocketListener(
            path=self.path, service=None, db_slots=threading.Semaphore(2)
        )
        self.listeners.append(listener)
        return listener

    def test_sigkill_restarts_same_socket_and_live_owner_is_preserved(self):
        code = (
            "import threading,time;from lingxi.adapters.innertest_socket import InnertestSocketListener;"
            f"s=InnertestSocketListener(path={self.path!r},service=None,db_slots=threading.Semaphore(2));"
            "s.start();print('ready',flush=True);time.sleep(30)"
        )
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", code], stdout=subprocess.PIPE, text=True
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            inode = os.stat(self.path).st_ino
            with self.assertRaisesRegex(ValueError, "socket_in_use"):
                self.listener().start()
            self.assertEqual(os.stat(self.path).st_ino, inode)
        finally:
            process.kill()
            process.wait(timeout=3)
            process.stdout.close()
        self.assertTrue(Path(self.path).exists())
        self.listener().start()
        self.assertTrue(Path(self.path).exists())

    def test_noncooperating_active_socket_and_non_socket_are_preserved(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(self.path)
            server.listen(1)
            inode = os.stat(self.path).st_ino
            with self.assertRaisesRegex(ValueError, "socket_in_use"):
                self.listener().start()
            self.assertEqual(os.stat(self.path).st_ino, inode)
        os.unlink(self.path)
        Path(self.path).write_text("protected")
        with self.assertRaisesRegex(ValueError, "socket_path_invalid"):
            self.listener().start()
        self.assertEqual(Path(self.path).read_text(), "protected")

    def test_foreign_owner_and_symlink_are_never_deleted(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(self.path)
        real_lstat = os.lstat

        def foreign(path):
            info = real_lstat(path)
            if str(path) == self.path:
                values = list(info)
                values[4] = os.geteuid() + 1
                return os.stat_result(values)
            return info

        with patch("lingxi.adapters.innertest_socket_path.os.lstat", side_effect=foreign):
            with self.assertRaisesRegex(ValueError, "socket_path_invalid"):
                self.listener().start()
        self.assertTrue(Path(self.path).exists())
        os.unlink(self.path)
        os.symlink("missing", self.path)
        with self.assertRaisesRegex(ValueError, "socket_path_invalid"):
            self.listener().start()
        self.assertTrue(os.path.islink(self.path))

    def test_partial_start_failure_cleans_socket_and_releases_lock(self):
        with patch("lingxi.adapters.innertest_socket.os.chmod", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                self.listener().start()
        self.assertFalse(Path(self.path).exists())
        self.listener().start()
