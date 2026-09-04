"""用户环境创建与落盘凭据纪律的用例（Epic D / S-D-02）。

产品负责人 2026-08-17 裁定 worker→MCP 令牌走**用户环境 Bearer**（写进 `.mcp.json`
header），并要求落盘凭据参照 biai-agent 先例做权限纪律：**文件 440、日志脱敏**。这份
用例把那两条钉成契约——它们是这次架构承诺变更唯一的补偿措施，只活在注释里不算被守住。

改写或作废的验收断言：`V-交付-06`（原文断言用户环境零凭据）。
"""

from __future__ import annotations

import errno
import json
import logging
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lingxi.adapters.user_environment import (
    CREDENTIAL_FILE_MODE,
    HOME_DIR_MODE,
    MCP_CONFIG_FILENAME,
    QUERY_MCP_SERVER_NAME,
    ROOT_DIR_MODE,
    TEMPORARY_PREFIX,
    TEMPORARY_SUFFIX,
    LocalUserEnvironment,
    UserEnvironmentError,
    build_mcp_config,
)

TOKEN = "s3cret-plaintext-token-value"
ENDPOINT = "https://mcp.example.internal/query"


class ConstructionTests(unittest.TestCase):
    def test_a_plain_http_endpoint_is_refused(self) -> None:
        """明文 Bearer 走 HTTP 等于把令牌发到网络上。"""

        with self.assertRaises(ValueError):
            LocalUserEnvironment(root="/tmp/x", mcp_endpoint="http://mcp.example.internal")

    def test_a_world_readable_credential_mode_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            LocalUserEnvironment(
                root="/tmp/x", mcp_endpoint=ENDPOINT, credential_file_mode=0o444
            )

    def test_an_empty_root_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            LocalUserEnvironment(root="  ", mcp_endpoint=ENDPOINT)

    def test_a_relative_root_is_refused(self) -> None:
        """相对路径会让「令牌落在哪」取决于进程的工作目录。"""

        with self.assertRaises(ValueError):
            LocalUserEnvironment(root="users", mcp_endpoint=ENDPOINT)

    def test_a_world_readable_root_mode_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            LocalUserEnvironment(root="/tmp/x", mcp_endpoint=ENDPOINT, root_dir_mode=0o755)

    def test_every_mode_must_be_exactly_the_invariant(self) -> None:
        """三个模式是安全不变量，不是可调参数——「稍微宽一点」也拒。"""

        cases = (
            {"root_dir_mode": 0o751},
            {"root_dir_mode": 0o700},
            {"home_dir_mode": 0o750},
            {"home_dir_mode": 0o777},
            {"home_dir_mode": 0o600},
            {"credential_file_mode": 0o400},
            {"credential_file_mode": 0o460},
            {"credential_file_mode": 0o640},
        )
        for override in cases:
            with self.subTest(override=override):
                with self.assertRaises(ValueError):
                    LocalUserEnvironment(root="/tmp/x", mcp_endpoint=ENDPOINT, **override)

    def test_a_group_writable_root_mode_is_refused(self) -> None:
        """属组可写时，同组用户能在首次开通**之前**把家目录预置成符号链接。"""

        with self.assertRaises(ValueError):
            LocalUserEnvironment(root="/tmp/x", mcp_endpoint=ENDPOINT, root_dir_mode=0o770)


class ConfigShapeTests(unittest.TestCase):
    def test_the_bearer_header_carries_the_user_s_own_token(self) -> None:
        document = json.loads(build_mcp_config(server_name="query", endpoint=ENDPOINT, token=TOKEN))
        server = document["mcpServers"]["query"]
        self.assertEqual(server["type"], "http")
        self.assertEqual(server["url"], ENDPOINT)
        self.assertEqual(server["headers"]["Authorization"], f"Bearer {TOKEN}")

    def test_the_rendering_is_byte_stable(self) -> None:
        """幂等比较靠"内容逐字节相同"，键序漂移会让每次开通都重写一次文件。"""

        first = build_mcp_config(server_name="query", endpoint=ENDPOINT, token=TOKEN)
        second = build_mcp_config(server_name="query", endpoint=ENDPOINT, token=TOKEN)
        self.assertEqual(first, second)
        self.assertTrue(first.endswith("\n"))


class FileSystemTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="lingxi-user-env-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name) / "users"
        self.environment = LocalUserEnvironment(root=str(self.root), mcp_endpoint=ENDPOINT)

    def _config(self, user_id: str = "usr_01H") -> Path:
        return self.root / user_id / MCP_CONFIG_FILENAME

    def test_the_credential_file_is_440_the_home_is_700_and_the_root_is_750(self) -> None:
        """biai-agent 先例，裁定明列：文件 440。根目录不得停在 umask 默认值。"""

        result = self.environment.ensure(user_id="usr_01H", mcp_token=TOKEN)

        self.assertTrue(result.created)
        config = self._config()
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), CREDENTIAL_FILE_MODE)
        self.assertEqual(stat.S_IMODE(config.parent.stat().st_mode), HOME_DIR_MODE)
        # 根目录：umask 默认值（实测 0775）会把全部内部用户标识列给同机任何用户看。
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), ROOT_DIR_MODE)
        self.assertEqual(ROOT_DIR_MODE & 0o007, 0)
        # 其他用户一个字节都读不到。
        self.assertEqual(stat.S_IMODE(config.stat().st_mode) & 0o007, 0)

    def test_the_default_server_name_is_the_shared_query_mcp_server_name_constant(self) -> None:
        """单一事实来源（Issue #291 根因 #1）：不传 ``mcp_server_name`` 时写进
        ``.mcp.json`` 的服务名必须是 ``QUERY_MCP_SERVER_NAME``——``apps/worker/
        config.py`` 的只读工具白名单前缀断言就是拿这同一个常量来核对的，两侧
        分道扬镳正是 2026-08-21 那次事故的根因。"""

        self.environment.ensure(user_id="usr_default_name", mcp_token=TOKEN)

        document = json.loads(self._config("usr_default_name").read_text(encoding="utf-8"))
        self.assertIn(QUERY_MCP_SERVER_NAME, document["mcpServers"])

    def test_two_users_never_share_one_configuration(self) -> None:
        """每个人只拿到自己那一份令牌。"""

        self.environment.ensure(user_id="usr_a", mcp_token="token-a")
        self.environment.ensure(user_id="usr_b", mcp_token="token-b")

        first = self._config("usr_a").read_text(encoding="utf-8")
        second = self._config("usr_b").read_text(encoding="utf-8")
        self.assertIn("token-a", first)
        self.assertNotIn("token-b", first)
        self.assertIn("token-b", second)
        self.assertNotIn("token-a", second)

    def test_the_temporary_file_is_never_wider_than_the_target(self) -> None:
        """磁盘上带令牌的那一刻起，权限就已经是最终值。"""

        observed: list[int] = []
        real_replace = os.replace

        def spy(src, dst, **kwargs):  # type: ignore[no-untyped-def]
            observed.append(
                stat.S_IMODE(os.stat(src, dir_fd=kwargs["src_dir_fd"]).st_mode)
            )
            return real_replace(src, dst, **kwargs)

        with mock.patch("lingxi.adapters.user_environment.os.replace", spy):
            self.environment.ensure(user_id="usr_01H", mcp_token=TOKEN)

        self.assertEqual(observed, [CREDENTIAL_FILE_MODE])

    def test_an_externally_widened_config_is_tightened_even_when_unchanged(self) -> None:
        """内容相同也要收一次权限，否则 440 这条纪律只在首次开通那一刻成立。"""

        self.environment.ensure(user_id="usr_01H", mcp_token=TOKEN)
        os.chmod(self._config(), 0o666)

        result = self.environment.ensure(user_id="usr_01H", mcp_token=TOKEN)

        self.assertFalse(result.created)
        self.assertEqual(stat.S_IMODE(self._config().stat().st_mode), CREDENTIAL_FILE_MODE)

    def test_a_second_call_with_the_same_token_does_not_rewrite_the_file(self) -> None:
        first = self.environment.ensure(user_id="usr_01H", mcp_token=TOKEN)
        second = self.environment.ensure(user_id="usr_01H", mcp_token=TOKEN)

        self.assertTrue(first.created)
        self.assertFalse(second.created, "同一份令牌不该被重复写出去")

    def test_a_changed_token_is_written_and_the_mode_is_still_440(self) -> None:
        self.environment.ensure(user_id="usr_01H", mcp_token=TOKEN)
        result = self.environment.ensure(user_id="usr_01H", mcp_token="another-token")

        self.assertTrue(result.created)
        config = self._config()
        self.assertIn("another-token", config.read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), CREDENTIAL_FILE_MODE)

    def test_no_temporary_file_survives_a_successful_write(self) -> None:
        self.environment.ensure(user_id="usr_01H", mcp_token=TOKEN)
        names = sorted(item.name for item in (self.root / "usr_01H").iterdir())
        self.assertEqual(names, [MCP_CONFIG_FILENAME], "原子写不得留下带令牌的临时文件")

    def test_a_traversing_user_id_never_becomes_a_path(self) -> None:
        for candidate in ("../escape", "a/b", "", ".hidden", "x" * 200):
            with self.subTest(candidate=candidate):
                with self.assertRaises(UserEnvironmentError):
                    self.environment.ensure(user_id=candidate, mcp_token=TOKEN)
        self.assertFalse(self.root.exists(), "非法标识不得留下任何目录")

    def test_an_empty_token_is_refused_before_anything_is_written(self) -> None:
        with self.assertRaises(UserEnvironmentError):
            self.environment.ensure(user_id="usr_01H", mcp_token="")
        self.assertFalse(self.root.exists())

    def test_a_leftover_temporary_file_is_swept_with_a_warning(self) -> None:
        """`SIGKILL` 落在「临时文件已含明文令牌」与 `os.replace` 之间时，`finally` 不会跑。

        那个 `440` 的文件会带着明文令牌留在磁盘上、无人清扫——而这正是本次架构承诺变更
        新增的那条外泄通道。下一次对同一用户调用必须把它清掉，并且**留声**。
        """

        home = self.root / "usr_01H"
        home.mkdir(parents=True)
        leftover = home / f"{TEMPORARY_PREFIX}abcd1234.tmp"
        leftover.write_text("leftover-with-a-token", encoding="utf-8")

        with self.assertLogs("lingxi.adapters.user_environment", level=logging.WARNING) as caught:
            self.environment.ensure(user_id="usr_01H", mcp_token=TOKEN)

        self.assertFalse(leftover.exists(), "带令牌的残留临时文件必须被清掉")
        self.assertTrue(
            any("临时文件" in line for line in caught.output), "清扫必须留声，不能静默"
        )
        self.assertTrue(self._config().exists())

    def test_an_unsweepable_directory_refuses_to_take_the_credential(self) -> None:
        """列不动或删不掉 = 我们管不了这个目录，而下一步正要往里写明文令牌。"""

        home = self.root / "usr_01H"
        home.mkdir(parents=True)
        # 目标位置被换成目录：`glob` 能列出它，`unlink` 删不掉——「目录被换成别的东西」
        # 这一类形态的最小复现。
        (home / f"{TEMPORARY_PREFIX}stuck.tmp").mkdir()

        with self.assertRaises(UserEnvironmentError) as caught:
            self.environment.ensure(user_id="usr_01H", mcp_token=TOKEN)

        self.assertTrue(str(caught.exception).startswith("sweep_failed_"), caught.exception)
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertFalse(self._config().exists(), "扫不动就一个字节的凭据都不写进去")

    def test_an_unlistable_directory_also_refuses_to_take_the_credential(self) -> None:
        """列不动与删不掉是同一件事：我们管不了这个目录。

        以 root 跑测试时权限位挡不住扫描（chmod 对 root 无效），因此直接注入
        `PermissionError`——要证明的是**分支存在且失败关闭**，不是 libc 的权限语义。
        """

        (self.root / "usr_01H").mkdir(parents=True)

        with mock.patch(
            "lingxi.adapters.user_environment.os.scandir",
            side_effect=PermissionError(errno.EACCES, "denied"),
        ):
            with self.assertRaises(UserEnvironmentError) as caught:
                self.environment.ensure(user_id="usr_01H", mcp_token=TOKEN)

        self.assertEqual(caught.exception.code, "sweep_failed_EACCES")
        self.assertFalse(self._config().exists(), "列不动就一个字节的凭据都不写进去")

    def test_the_error_code_tells_ops_which_errno_it_was(self) -> None:
        """`EACCES`（权限不对）与 `ENOTDIR`（挂载点不对）是两种不同的运维动作。"""

        home = self.root / "usr_01H"
        home.mkdir(parents=True)
        (home / f"{TEMPORARY_PREFIX}stuck.tmp").mkdir()

        with self.assertRaises(UserEnvironmentError) as caught:
            self.environment.ensure(user_id="usr_01H", mcp_token=TOKEN)

        self.assertNotEqual(caught.exception.code, "sweep_failed_unknown")

    def test_a_symlinked_home_is_refused(self) -> None:
        """JumpServer 用户同机有 shell：把家目录换成指向别处的软链是可达攻击。"""

        self.root.mkdir(parents=True)
        elsewhere = Path(self._temporary.name) / "elsewhere"
        elsewhere.mkdir()
        (self.root / "usr_01H").symlink_to(elsewhere)

        with self.assertRaises(UserEnvironmentError) as caught:
            self.environment.ensure(user_id="usr_01H", mcp_token=TOKEN)

        self.assertEqual(caught.exception.code, "home_is_symlink")
        self.assertEqual(list(elsewhere.iterdir()), [], "一个字节都不得落到软链指向的地方")

    def test_a_symlinked_root_is_refused(self) -> None:
        elsewhere = Path(self._temporary.name) / "elsewhere-root"
        elsewhere.mkdir()
        self.root.symlink_to(elsewhere)

        with self.assertRaises(UserEnvironmentError) as caught:
            self.environment.ensure(user_id="usr_01H", mcp_token=TOKEN)

        self.assertEqual(caught.exception.code, "root_is_symlink")
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_a_symlinked_config_pointing_nowhere_is_replaced(self) -> None:
        """既有 `.mcp.json` 被换成软链时，写入必须落在真正的家目录里。"""

        home = self.root / "usr_01H"
        home.mkdir(parents=True)
        elsewhere = Path(self._temporary.name) / "stolen.json"
        (home / MCP_CONFIG_FILENAME).symlink_to(elsewhere)

        self.environment.ensure(user_id="usr_01H", mcp_token=TOKEN)

        self.assertFalse(elsewhere.exists(), "不得顺着软链把令牌写到别处")
        self.assertFalse((home / MCP_CONFIG_FILENAME).is_symlink())
        self.assertIn(TOKEN, (home / MCP_CONFIG_FILENAME).read_text(encoding="utf-8"))

    def test_a_symlinked_config_is_neither_read_through_nor_chmodded(self) -> None:
        """软链指向一个**内容恰好相同**的真实文件：最危险的那一种。

        顺着读会判成「内容没变」→ 于是跳过写入、并把 `chmod` 打到**软链指向的那个文件**
        上。攻击者据此可以拿到一个被我们改成 `0440`、而且始终不被覆盖的位置。
        """

        home = self.root / "usr_01H"
        home.mkdir(parents=True)
        planted = Path(self._temporary.name) / "planted.json"
        planted.write_text(
            build_mcp_config(server_name="query", endpoint=ENDPOINT, token=TOKEN),
            encoding="utf-8",
        )
        os.chmod(planted, 0o666)
        (home / MCP_CONFIG_FILENAME).symlink_to(planted)

        result = self.environment.ensure(user_id="usr_01H", mcp_token=TOKEN)

        self.assertTrue(result.created, "不得把软链背后的内容当成「我们已经写好的那一份」")
        self.assertFalse((home / MCP_CONFIG_FILENAME).is_symlink())
        self.assertEqual(
            stat.S_IMODE(planted.stat().st_mode), 0o666, "不得把 chmod 打到软链指向的文件上"
        )
        self.assertEqual(
            stat.S_IMODE((home / MCP_CONFIG_FILENAME).stat().st_mode), CREDENTIAL_FILE_MODE
        )

    def test_a_token_that_cannot_be_encoded_never_reaches_an_exception(self) -> None:
        """孤立代理项会让 `UnicodeEncodeError.object` 带上整份 JSON——也就是完整令牌。"""

        broken = "tok\ud800en"

        with self.assertRaises(UserEnvironmentError) as caught:
            self.environment.ensure(user_id="usr_01H", mcp_token=broken)

        self.assertEqual(caught.exception.code, "config_encode_failed")
        self.assertNotIn("tok", repr(caught.exception))
        self.assertFalse(self._config().exists())

    def test_a_failed_discard_is_loud_and_not_swallowed(self) -> None:
        """删不掉那个带令牌的临时文件是必须让人知道的事，绝不静默吞。"""

        real_unlink = os.unlink
        seen = {"blocked": False}

        def spy(path, **kwargs):  # type: ignore[no-untyped-def]
            if isinstance(path, str) and path.startswith(TEMPORARY_PREFIX) and not seen["blocked"]:
                seen["blocked"] = True
                raise PermissionError(errno.EACCES, "denied")
            return real_unlink(path, **kwargs)

        with mock.patch(
            "lingxi.adapters.user_environment.os.replace",
            side_effect=OSError(errno.EXDEV, "cross device"),
        ):
            with mock.patch("lingxi.adapters.user_environment.os.unlink", spy):
                with self.assertLogs(
                    "lingxi.adapters.user_environment", level=logging.WARNING
                ) as caught:
                    with self.assertRaises(UserEnvironmentError) as raised:
                        self.environment.ensure(user_id="usr_01H", mcp_token=TOKEN)

        # 原始失败原因不被清理失败盖掉，清理失败本身也留了声。
        self.assertEqual(raised.exception.code, "config_write_EXDEV")
        self.assertTrue(
            any("可能残留明文令牌" in line for line in caught.output), caught.output
        )

    def _failing_close(self):
        """让**普通文件**那次 close 失败；目录 fd 的 close 属于 dirfd 生命周期，不动它。"""

        real_close = os.close

        def spy(fd):  # type: ignore[no-untyped-def]
            is_regular = stat.S_ISREG(os.fstat(fd).st_mode)
            real_close(fd)
            if is_regular:
                raise OSError(errno.EIO, "close failed")

        return mock.patch("lingxi.adapters.user_environment.os.close", spy)

    def test_a_close_failure_is_logged_and_does_not_lose_the_config(self) -> None:
        """`close()` 抛错但数据已 `fsync`、`replace` 成功：如实记一条，操作照常成立。"""

        with self._failing_close():
            with self.assertLogs("lingxi.adapters.user_environment", level=logging.WARNING) as caught:
                result = self.environment.ensure(user_id="usr_01H", mcp_token=TOKEN)

        self.assertTrue(result.created)
        self.assertIn(TOKEN, self._config().read_text(encoding="utf-8"))
        self.assertTrue(any("关闭失败" in line for line in caught.output), caught.output)

    def test_a_close_failure_does_not_skip_the_cleanup_or_mask_the_real_error(self) -> None:
        """`close()` 与 `replace()` 同时失败：报出来的必须是 replace，且临时文件被清掉。

        这正是审查指出的形状——`close()` 在 `finally` 里抛错会让后面的清理分支根本不执行，
        原始错误也被覆盖。
        """

        home = self.root / "usr_01H"

        with self._failing_close():
            with mock.patch(
                "lingxi.adapters.user_environment.os.replace",
                side_effect=OSError(errno.EXDEV, "cross device"),
            ):
                with self.assertRaises(UserEnvironmentError) as caught:
                    self.environment.ensure(user_id="usr_01H", mcp_token=TOKEN)

        self.assertEqual(caught.exception.code, "config_write_EXDEV", "真正的失败原因不得被盖掉")
        leftovers = [item.name for item in home.iterdir()]
        self.assertEqual(leftovers, [], "清理不得因为 close 抛错而被跳过")

    def test_the_token_never_appears_in_logs(self) -> None:
        """日志脱敏：裁定明列的第二条纪律。"""

        with self.assertLogs("lingxi.adapters.user_environment", level=logging.DEBUG) as captured:
            self.environment.ensure(user_id="usr_01H", mcp_token=TOKEN)
            self.environment.ensure(user_id="usr_01H", mcp_token=TOKEN)
        rendered = "\n".join(captured.output)
        self.assertNotIn(TOKEN, rendered)
        self.assertNotIn("Bearer", rendered)

    def test_a_write_failure_reports_only_an_error_code(self) -> None:
        """异常正文里不得出现路径、内容或凭据片段。"""

        # 让**替换**那一步确定性失败：目标位置是一个目录，`os.replace` 无论以什么身份
        # 运行都换不过去。用权限位造失败在 root 下不成立（chmod 挡不住 root），
        # 那会让这条用例在 CI 与本机之间时红时绿。
        self.root.mkdir(parents=True)
        home = self.root / "usr_01H"
        home.mkdir()
        (home / MCP_CONFIG_FILENAME).mkdir()
        with self.assertRaises(UserEnvironmentError) as caught:
            self.environment.ensure(user_id="usr_01H", mcp_token=TOKEN)
        message = str(caught.exception)
        self.assertNotIn(TOKEN, message)
        self.assertNotIn(str(home), message)
        self.assertTrue(message.startswith("config_write_"), message)


class StartupSweepTests(unittest.TestCase):
    """启动期全量清扫：把 `SIGKILL` 残留的上界从「无上界」压到「一个进程生命周期」。

    没有它，被强杀留下的带令牌临时文件只会在「这个用户下一次再走开通」时被清掉——
    而一个不再重试的用户意味着那份明文令牌可以无限期躺在磁盘上。
    """

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="lingxi-user-env-sweep-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name) / "users"
        self.environment = LocalUserEnvironment(root=str(self.root), mcp_endpoint=ENDPOINT)

    def _leftover(self, user_id: str) -> Path:
        home = self.root / user_id
        home.mkdir(parents=True, exist_ok=True)
        path = home / f"{TEMPORARY_PREFIX}{user_id}{TEMPORARY_SUFFIX}"
        path.write_text("leftover-with-a-token", encoding="utf-8")
        return path

    def test_it_cleans_every_home_not_just_the_one_being_onboarded(self) -> None:
        first = self._leftover("usr_a")
        second = self._leftover("usr_b")

        cleaned = self.environment.sweep_all()

        self.assertEqual(cleaned, 2)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())

    def test_it_leaves_the_users_own_files_alone(self) -> None:
        home = self.root / "usr_a"
        home.mkdir(parents=True)
        keep = home / "report.md"
        keep.write_text("用户自己的产物", encoding="utf-8")
        config = home / MCP_CONFIG_FILENAME
        config.write_text("{}", encoding="utf-8")

        self.environment.sweep_all()

        self.assertTrue(keep.exists(), "除了自己建的临时文件，什么都不碰")
        self.assertTrue(config.exists())

    def test_a_missing_root_is_not_an_error(self) -> None:
        self.assertEqual(self.environment.sweep_all(), 0)

    def test_an_unscannable_root_fails_closed(self) -> None:
        self.root.mkdir(parents=True)

        with mock.patch(
            "lingxi.adapters.user_environment.os.scandir",
            side_effect=PermissionError(errno.EACCES, "denied"),
        ):
            with self.assertRaises(UserEnvironmentError) as caught:
                self.environment.sweep_all()

        self.assertEqual(caught.exception.code, "sweep_failed_EACCES")

    def test_a_symlinked_home_is_refused_during_the_startup_sweep(self) -> None:
        self.root.mkdir(parents=True)
        elsewhere = Path(self._temporary.name) / "elsewhere"
        elsewhere.mkdir()
        (self.root / "usr_a").symlink_to(elsewhere)

        # 软链不是目录项里的 dir（follow_symlinks=False），因此直接被跳过，不跟随。
        self.assertEqual(self.environment.sweep_all(), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
