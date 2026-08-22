"""Issue #153：Agent 会话 JSONL 物理清理的文件系统层断言（真实临时目录）。

只覆盖 ``apps/worker/session_cleanup.py`` 本身——数据库侧三个触发点（``/new``、
空闲到点、停用/权限变化）的排队逻辑与 ``WorkerService._cleanup_agent_sessions``
的认领/标记完成逻辑分别在 ``tests/test_delivery_outbox.py``（真库）与
``tests/test_worker_queue_consumer.py`` 覆盖。

``ArchiveOnCleanupTests``（Issue #291 L6 取证结论，2026-08-22）覆盖"删除前先
归档"这条新行为，见 ``session_cleanup.py`` 模块文档「删除前先归档」。
"""

from __future__ import annotations

import os
import stat
import tempfile
import time
import unittest
from pathlib import Path

from lingxi.apps.worker.session_cleanup import (
    ARCHIVE_ENABLED_ENV_VAR,
    default_session_root,
    delete_agent_session_files,
)


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


class ArchiveOnCleanupTests(unittest.TestCase):
    """Issue #291 L6 取证结论（2026-08-22）：验收现场的一次 ``/new`` 触发
    ``agent_session_cleanup``，把出事那次回合的会话 JSONL 物理删除、毁了取证
    现场。这组用例覆盖"删除前先归档"这条新行为，夹具用真实调用形状——
    ``WorkerService._cleanup_agent_sessions`` 会传 ``user_env_root``/``user_id``，
    见 ``tests/test_worker_queue_consumer.py`` 里对应的接线断言。"""

    def test_archives_instead_of_deleting_when_user_env_root_and_user_id_are_given(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            nested = root / "projects" / "-var-lib-lingxi-users-u1"
            nested.mkdir(parents=True)
            target = nested / "01J00000000000000000000SESS.jsonl"
            target.write_text("{}", encoding="utf-8")
            user_env_root = Path(tmp) / "user-env"
            user_env_root.mkdir()

            handled = delete_agent_session_files(
                root,
                "01J00000000000000000000SESS",
                user_env_root=user_env_root,
                user_id="usr-1",
                env={},  # 空 env：归档开关走默认值（开）
            )

            self.assertEqual(handled, 1)
            self.assertFalse(target.exists(), "原路径的文件必须已经被移走，不是复制")
            archived = user_env_root / "_archive" / "usr-1" / "01J00000000000000000000SESS.jsonl"
            self.assertTrue(archived.exists(), "归档目标必须存在")
            self.assertEqual(archived.read_text(encoding="utf-8"), "{}", "内容必须原样保留")

    def test_archive_directory_permission_matches_the_tightened_user_directory_mode(
        self,
    ) -> None:
        """归档目录权限同用户目录收紧口径（0700），不比用户目录本身更宽。"""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            root.mkdir()
            (root / "01J00000000000000000000SESS.jsonl").write_text("{}", encoding="utf-8")
            user_env_root = Path(tmp) / "user-env"
            user_env_root.mkdir()

            delete_agent_session_files(
                root,
                "01J00000000000000000000SESS",
                user_env_root=user_env_root,
                user_id="usr-1",
                env={},
            )

            archive_dir = user_env_root / "_archive" / "usr-1"
            mode = stat.S_IMODE(archive_dir.stat().st_mode)
            self.assertEqual(oct(mode), oct(0o700))

    def test_falls_back_to_plain_delete_without_user_env_root_or_user_id(self) -> None:
        """向后兼容：不传新增关键字参数（旧调用方、旧测试）时行为与签名变更前
        完全一致——物理删除，不归档、不建 ``_archive`` 目录。"""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "01J00000000000000000000SESS.jsonl"
            target.write_text("{}", encoding="utf-8")

            handled = delete_agent_session_files(root, "01J00000000000000000000SESS")

            self.assertEqual(handled, 1)
            self.assertFalse(target.exists())
            self.assertFalse((root / "_archive").exists())

    def test_env_var_off_falls_back_to_plain_delete_even_with_a_destination(self) -> None:
        """行为受环境变量开关控制：显式关闭时，即使调用方提供了
        ``user_env_root``/``user_id``，也必须退回旧的直接物理删除语义，不归档。
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            root.mkdir()
            target = root / "01J00000000000000000000SESS.jsonl"
            target.write_text("{}", encoding="utf-8")
            user_env_root = Path(tmp) / "user-env"
            user_env_root.mkdir()

            handled = delete_agent_session_files(
                root,
                "01J00000000000000000000SESS",
                user_env_root=user_env_root,
                user_id="usr-1",
                env={ARCHIVE_ENABLED_ENV_VAR: "false"},
            )

            self.assertEqual(handled, 1)
            self.assertFalse(target.exists())
            self.assertFalse((user_env_root / "_archive").exists(), "关闭开关时不得创建归档目录")

    def test_retention_count_prunes_the_oldest_archived_entries(self) -> None:
        """保留上限——数量维度：归档目录里超过 ``retention_count`` 的部分必须被
        裁掉，只留最近的那几个。"""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            root.mkdir()
            user_env_root = Path(tmp) / "user-env"
            archive_dir = user_env_root / "_archive" / "usr-1"
            archive_dir.mkdir(parents=True)

            # 预先放 2 份"更旧"的归档（mtime 早于即将归档的新文件）。
            now = time.time()
            old_files = []
            for index in range(2):
                old_file = archive_dir / f"01J0000000000000000000OLD{index}.jsonl"
                old_file.write_text("{}", encoding="utf-8")
                os.utime(old_file, (now - 3600 * (index + 1), now - 3600 * (index + 1)))
                old_files.append(old_file)

            new_session_file = root / "01J00000000000000000000NEW.jsonl"
            new_session_file.write_text("{}", encoding="utf-8")

            delete_agent_session_files(
                root,
                "01J00000000000000000000NEW",
                user_env_root=user_env_root,
                user_id="usr-1",
                env={},
                retention_count=2,
                retention_days=0,  # 只测数量维度，时间维度关闭
            )

            remaining = sorted(p.name for p in archive_dir.iterdir())
            self.assertEqual(len(remaining), 2, "超过保留数量的旧归档必须被裁剪")
            self.assertIn("01J00000000000000000000NEW.jsonl", remaining, "刚归档的必须保留")
            self.assertNotIn(old_files[1].name, remaining, "最旧的一份必须被裁掉")

    def test_retention_days_prunes_entries_older_than_the_window(self) -> None:
        """保留上限——时间维度：超过 ``retention_days`` 的归档必须被裁掉，即使
        总数量还没超过 ``retention_count``。"""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            root.mkdir()
            user_env_root = Path(tmp) / "user-env"
            archive_dir = user_env_root / "_archive" / "usr-1"
            archive_dir.mkdir(parents=True)

            now = time.time()
            stale_file = archive_dir / "01J0000000000000000000OLD0.jsonl"
            stale_file.write_text("{}", encoding="utf-8")
            os.utime(stale_file, (now - 30 * 86400, now - 30 * 86400))  # 30 天前

            new_session_file = root / "01J00000000000000000000NEW.jsonl"
            new_session_file.write_text("{}", encoding="utf-8")

            delete_agent_session_files(
                root,
                "01J00000000000000000000NEW",
                user_env_root=user_env_root,
                user_id="usr-1",
                env={},
                retention_count=10,  # 数量远没到上限，只测时间维度
                retention_days=7,
            )

            remaining = sorted(p.name for p in archive_dir.iterdir())
            self.assertNotIn(stale_file.name, remaining, "超过保留天数的归档必须被裁掉")
            self.assertIn("01J00000000000000000000NEW.jsonl", remaining)


if __name__ == "__main__":
    unittest.main()
