"""Issue #153：Agent 会话 JSONL 物理清理的文件系统层断言（真实临时目录）。

只覆盖 ``apps/worker/session_cleanup.py`` 本身——数据库侧三个触发点（``/new``、
空闲到点、停用/权限变化）的排队逻辑与 ``WorkerService._cleanup_agent_sessions``
的认领/标记完成逻辑分别在 ``tests/test_delivery_outbox.py``（真库）与
``tests/test_worker_queue_consumer.py`` 覆盖。

``ArchiveOnCleanupTests``（Issue #291 L6 取证结论，2026-08-22）覆盖"删除前先
归档"这条新行为，见 ``session_cleanup.py`` 模块文档「删除前先归档」。

``ReclaimSessionTranscriptsTests``（Issue #494，2026-08-31）覆盖**常规容量回收**
这条与定点清理互相独立的路径：定点清理只在 ``/new``、权限刷新等触发点排队时才动
手，正常问数流程一次都不排，转录因此在容器的 256MB 内存盘上单调增长到写满（实测
约 45 分钟）。接线到 ``process_once()`` 的持续负载收敛断言在
``tests/test_worker_queue_consumer.py`` 的 ``SessionTranscriptReclamationTests``。
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
    reclaim_session_transcripts,
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


class ReclaimSessionTranscriptsTests(unittest.TestCase):
    """Issue #494 ①：会话转录的**常规**回收路径（按字节预算删最旧的）。

    每个用例都构造一份"改动前必然写满"的现场：没有任何 ``agent_session_cleanup``
    排队，只有正常问数流程产生的转录——这正是实测里那条从来没有人清理的路径。
    """

    def _write(self, root: Path, name: str, size: int, *, age_seconds: float) -> Path:
        path = root / f"{name}.jsonl"
        path.write_bytes(b"x" * size)
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        return path

    def _total(self, root: Path) -> int:
        return sum(entry.stat().st_size for entry in root.rglob("*.jsonl"))

    def test_under_budget_touches_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kept = self._write(root, "a", 100, age_seconds=10_000)

            outcome = reclaim_session_transcripts(root, budget_bytes=1000)

            self.assertEqual(outcome.files_deleted, 0)
            self.assertFalse(outcome.over_budget)
            self.assertTrue(kept.exists())

    def test_over_budget_deletes_oldest_first_down_to_the_low_water_mark(self) -> None:
        """核心断言：超预算时删**最旧的**、留最新的，并且一次压到低水位。

        删最旧而不是删最大，是取证姿态的一部分：刚出事的那次回合永远是最新的
        那一份（Issue #291 的教训正是"``/new`` 顺手把刚出事的转录删了"）。
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 5 份 × 100B = 500B，预算 200B、低水位 75% → 目标 150B。
            files = [
                self._write(root, f"s{index}", 100, age_seconds=10_000 - index * 100)
                for index in range(5)
            ]

            outcome = reclaim_session_transcripts(
                root, budget_bytes=200, low_water_ratio=0.75, min_age_seconds=60.0
            )

            self.assertEqual(outcome.bytes_before, 500)
            self.assertLessEqual(outcome.bytes_after, 150)
            self.assertFalse(outcome.over_budget)
            self.assertEqual(
                [path.exists() for path in files],
                [False, False, False, False, True],
                "必须从最旧的一端开始删，最新的那一份留到最后",
            )

    def test_recently_written_transcripts_are_protected_and_the_shortfall_is_reported(
        self,
    ) -> None:
        """保护窗口内的转录一律跳过——在途回合与用户刚聊过的会话不能被腾空间
        的动作误伤。删不动时如实给出 ``over_budget``，而不是把保护窗口降到 0
        去凑数（真正的兜底是健康检查按可用空间判红）。
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fresh = [self._write(root, f"n{index}", 100, age_seconds=1.0) for index in range(5)]

            outcome = reclaim_session_transcripts(
                root, budget_bytes=200, min_age_seconds=300.0
            )

            self.assertEqual(outcome.files_deleted, 0)
            self.assertEqual(outcome.files_protected, 5)
            self.assertTrue(
                outcome.over_budget,
                "所有可删的都删完仍然超预算时必须如实报出来，让运维看得见",
            )
            self.assertTrue(all(path.exists() for path in fresh))

    def test_a_zero_budget_disables_reclamation_entirely(self) -> None:
        """运维的显式逃生口（例如要保全一整段取证现场）：预算 0 = 一个都不删。"""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kept = [self._write(root, f"k{index}", 100, age_seconds=10_000) for index in range(5)]

            outcome = reclaim_session_transcripts(root, budget_bytes=0)

            self.assertEqual(outcome.files_deleted, 0)
            self.assertTrue(all(path.exists() for path in kept))

    def test_only_session_transcripts_are_considered(self) -> None:
        """``$HOME`` 下不止会话转录：Claude Code CLI 与 MCP 子进程的配置也写在
        这里。容量回收只碰 ``*.jsonl``，删掉别的东西会把一次容量维护变成一次
        配置事故。
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "old", 1000, age_seconds=10_000)
            settings = root / "settings.json"
            settings.write_bytes(b"y" * 1000)
            os.utime(settings, (time.time() - 10_000, time.time() - 10_000))

            outcome = reclaim_session_transcripts(root, budget_bytes=100, min_age_seconds=60.0)

            self.assertEqual(outcome.files_deleted, 1)
            self.assertTrue(settings.exists(), "非转录文件不得被容量回收顺手删掉")

    def test_a_missing_root_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist"

            outcome = reclaim_session_transcripts(missing, budget_bytes=100)

            self.assertEqual(outcome.files_deleted, 0)
            self.assertEqual(outcome.bytes_before, 0)

    def test_transcripts_nested_under_the_cwd_encoded_directory_are_found(self) -> None:
        """真实布局是 ``$HOME/.claude/projects/<按 cwd 编码的目录>/<id>.jsonl``：
        回收必须递归，否则在真实容器里一个文件都扫不到（与定点清理按文件名递归
        搜索同一条理由）。
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "-var-lib-lingxi-users"
            nested.mkdir()
            old = nested / "old.jsonl"
            old.write_bytes(b"x" * 1000)
            os.utime(old, (time.time() - 10_000, time.time() - 10_000))

            outcome = reclaim_session_transcripts(root, budget_bytes=100, min_age_seconds=60.0)

            self.assertEqual(outcome.files_deleted, 1)
            self.assertFalse(old.exists())
