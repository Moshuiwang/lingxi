"""Issue #153：Agent 会话 JSONL 物理清理的文件系统层断言（真实临时目录）。

只覆盖 ``apps/worker/session_cleanup.py`` 本身——数据库侧三个触发点（``/new``、
空闲到点、停用/权限变化）的排队逻辑与 ``WorkerService._cleanup_agent_sessions``
的认领/标记完成逻辑分别在 ``tests/test_delivery_outbox.py``（真库）与
``tests/test_worker_queue_consumer.py`` 覆盖。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lingxi.apps.worker.session_cleanup import default_session_root, delete_agent_session_files


class DefaultSessionRootTests(unittest.TestCase):
    def test_derives_from_home_environment_variable(self) -> None:
        root = default_session_root({"HOME": "/var/lib/lingxi/users/u1"})

        self.assertEqual(root, Path("/var/lib/lingxi/users/u1/.claude/projects"))

    def test_missing_home_returns_none_instead_of_a_relative_guess(self) -> None:
        self.assertIsNone(default_session_root({}))
        self.assertIsNone(default_session_root({"HOME": "  "}))


class DeleteAgentSessionFilesTests(unittest.TestCase):
    def test_deletes_a_matching_file_found_anywhere_under_the_root(self) -> None:
        """按文件名而不是目录编码匹配（模块头注释的核心取舍）：无论真实 Claude
        Code CLI 把它塞进 root 下哪一层子目录，只要文件名匹配就必须被找到并删除。
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "projects" / "-var-lib-lingxi-users-u1"
            nested.mkdir(parents=True)
            target = nested / "01J00000000000000000000SESS.jsonl"
            target.write_text("{}", encoding="utf-8")
            other = nested / "01J00000000000000000000OTHR.jsonl"
            other.write_text("{}", encoding="utf-8")

            deleted = delete_agent_session_files(root, "01J00000000000000000000SESS")

            self.assertEqual(deleted, 1)
            self.assertFalse(target.exists())
            self.assertTrue(other.exists(), "不相关的会话文件不得被误删")

    def test_missing_root_or_missing_file_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "does-not-exist"
            self.assertEqual(delete_agent_session_files(root, "01J00000000000000000000SESS"), 0)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(delete_agent_session_files(root, "01J00000000000000000000SESS"), 0)

    def test_is_idempotent_when_called_twice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "01J00000000000000000000SESS.jsonl"
            target.write_text("{}", encoding="utf-8")

            first = delete_agent_session_files(root, "01J00000000000000000000SESS")
            second = delete_agent_session_files(root, "01J00000000000000000000SESS")

            self.assertEqual(first, 1)
            self.assertEqual(second, 0, "文件已经不存在时重复调用必须是无害的")

    def test_rejects_a_session_id_containing_a_path_separator(self) -> None:
        """会话 id 来自数据库回读，防御性拒绝任何看起来像路径穿越的取值——
        即使调用方在这一层已经信任它，本函数也不应该把它原样拼进 glob 模式。
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                delete_agent_session_files(root, "../../etc/passwd")
            with self.assertRaises(ValueError):
                delete_agent_session_files(root, "")


if __name__ == "__main__":
    unittest.main()
