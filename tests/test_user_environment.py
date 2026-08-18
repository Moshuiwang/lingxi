"""用户环境创建与落盘凭据纪律的用例（Epic D / S-D-02）。

产品负责人 2026-08-17 裁定 worker→MCP 令牌走**用户环境 Bearer**（写进 `.mcp.json`
header），并要求落盘凭据参照 biai-agent 先例做权限纪律：**文件 440、日志脱敏**。这份
用例把那两条钉成契约——它们是这次架构承诺变更唯一的补偿措施，只活在注释里不算被守住。

改写或作废的验收断言：`V-交付-06`（原文断言用户环境零凭据）。
"""

from __future__ import annotations

import json
import logging
import stat
import tempfile
import unittest
from pathlib import Path

from lingxi.adapters.user_environment import (
    CREDENTIAL_FILE_MODE,
    HOME_DIR_MODE,
    MCP_CONFIG_FILENAME,
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

    def test_the_credential_file_is_440_and_the_home_is_700(self) -> None:
        """biai-agent 先例，裁定明列：文件 440。"""

        result = self.environment.ensure(user_id="usr_01H", mcp_token=TOKEN)

        self.assertTrue(result.created)
        config = self._config()
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), CREDENTIAL_FILE_MODE)
        self.assertEqual(stat.S_IMODE(config.parent.stat().st_mode), HOME_DIR_MODE)
        # 其他用户一个字节都读不到。
        self.assertEqual(stat.S_IMODE(config.stat().st_mode) & 0o007, 0)

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
