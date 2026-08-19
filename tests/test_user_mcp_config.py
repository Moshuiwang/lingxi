"""``adapters/user_mcp_config.py``：按用户读取问数 MCP 配置（Epic D 闸⑥）。

配套 ``adapters/user_environment.py`` 的写侧（S-D-02）：这里只验证读侧本身的
契约——失败关闭覆盖面、符号链接拒绝、以及与写侧 ``build_mcp_config`` 的往返
一致性。真正"worker 按用户覆盖 mcp_servers、绝不回退"的编排断言在
``tests/test_worker_queue_consumer.py``。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from lingxi.adapters.user_environment import build_mcp_config
from lingxi.adapters.user_mcp_config import UserMcpConfigError, load_user_mcp_servers


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class LoadUserMcpServersTest(unittest.TestCase):
    def test_round_trips_with_the_write_side_shape(self) -> None:
        """读侧必须认得写侧 ``build_mcp_config`` 真实产出的形状——两侧独立
        维护但必须对同一份契约。"""

        with tempfile.TemporaryDirectory() as root:
            content = build_mcp_config(
                server_name="query", endpoint="https://example.invalid/mcp", token="tok-123"
            )
            _write(Path(root) / "usr-1" / ".mcp.json", content)

            servers = load_user_mcp_servers(root=root, user_id="usr-1")

            self.assertEqual(
                servers,
                {
                    "query": {
                        "type": "http",
                        "url": "https://example.invalid/mcp",
                        "headers": {"Authorization": "Bearer tok-123"},
                    }
                },
            )

    def test_root_unconfigured_fails_closed(self) -> None:
        for root in ("", "   ", None):
            with self.subTest(root=root):
                with self.assertRaises(UserMcpConfigError) as caught:
                    load_user_mcp_servers(root=root, user_id="usr-1")  # type: ignore[arg-type]
                self.assertEqual(caught.exception.code, "root_unconfigured")

    def test_an_invalid_user_id_is_rejected_without_touching_the_filesystem(self) -> None:
        for user_id in ("", "..", "../etc", "a/b", ".hidden", "x" * 65):
            with self.subTest(user_id=user_id):
                with self.assertRaises(UserMcpConfigError) as caught:
                    load_user_mcp_servers(root="/does/not/matter", user_id=user_id)
                self.assertEqual(caught.exception.code, "invalid_user_id")

    def test_a_root_that_does_not_exist_fails_closed(self) -> None:
        with self.assertRaises(UserMcpConfigError) as caught:
            load_user_mcp_servers(root="/no/such/root/dir", user_id="usr-1")
        self.assertTrue(caught.exception.code.startswith("root_"))

    def test_a_user_who_never_onboarded_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(UserMcpConfigError) as caught:
                load_user_mcp_servers(root=root, user_id="usr-never-onboarded")
            self.assertEqual(caught.exception.code, "home_missing")

    def test_a_home_directory_that_is_a_symlink_is_refused(self) -> None:
        """闸⑥红线的读侧版本：把这个用户的目录换成指向别处（例如另一个用户）
        的软链，绝不能让 worker 用错的令牌去查数。"""

        with tempfile.TemporaryDirectory() as root:
            real_target = Path(root) / "elsewhere"
            real_target.mkdir()
            _write(
                real_target / ".mcp.json",
                build_mcp_config(
                    server_name="query", endpoint="https://example.invalid/mcp", token="not-mine"
                ),
            )
            os.symlink(real_target, Path(root) / "usr-1")

            with self.assertRaises(UserMcpConfigError) as caught:
                load_user_mcp_servers(root=root, user_id="usr-1")
            self.assertEqual(caught.exception.code, "home_is_symlink")

    def test_a_config_file_that_is_a_symlink_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "usr-1"
            home.mkdir()
            elsewhere = Path(root) / "elsewhere.json"
            _write(
                elsewhere,
                build_mcp_config(
                    server_name="query", endpoint="https://example.invalid/mcp", token="not-mine"
                ),
            )
            os.symlink(elsewhere, home / ".mcp.json")

            with self.assertRaises(UserMcpConfigError) as caught:
                load_user_mcp_servers(root=root, user_id="usr-1")
            self.assertEqual(caught.exception.code, "config_is_symlink")

    def test_malformed_json_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _write(Path(root) / "usr-1" / ".mcp.json", "{not valid json")

            with self.assertRaises(UserMcpConfigError) as caught:
                load_user_mcp_servers(root=root, user_id="usr-1")
            self.assertEqual(caught.exception.code, "config_invalid_json")

    def test_wrong_shape_fails_closed_instead_of_falling_back_to_an_empty_mapping(self) -> None:
        """空 dict 与"读取失败"必须同等对待：一份没有任何 MCP 服务器的
        ``.mcp.json`` 不能被解释成"这个用户没有可用工具、但会话照样跑"。"""

        cases = (
            "{}",
            json.dumps({"mcpServers": {}}),
            json.dumps({"mcpServers": "not-a-dict"}),
            json.dumps({"mcpServers": {"": {"type": "http"}}}),
            json.dumps({"mcpServers": {"query": "not-a-dict"}}),
            json.dumps([1, 2, 3]),
        )
        for raw in cases:
            with self.subTest(raw=raw):
                with tempfile.TemporaryDirectory() as root:
                    _write(Path(root) / "usr-1" / ".mcp.json", raw)
                    with self.assertRaises(UserMcpConfigError) as caught:
                        load_user_mcp_servers(root=root, user_id="usr-1")
                    self.assertEqual(caught.exception.code, "config_shape_invalid")

    def test_server_shape_is_validated_field_by_field_not_just_is_a_dict(self) -> None:
        """外部独立审查 F1：此前只检查"每个 server 的值是不是 dict"，一份空
        server 配置、缺 Authorization、令牌为空、甚至 ``stdio`` + ``command``
        （能启动任意本地进程的形态）都能原样通过——分别对应"用户拿到一个莫名
        其妙的失败而不是诚实的配置读不到终态"与"读侧被诱导去启动任意本地
        进程"两类问题。这里逐个构造这些畸形形态，确认都在同一个错误码下
        失败关闭。"""

        malformed_servers = (
            # 空 server 配置：不是"没有任何工具"，是"配置本身有问题"。
            {"query": {}},
            # 缺 Authorization。
            {"query": {"type": "http", "url": "https://mcp.example.invalid"}},
            # headers 存在但没有 Authorization。
            {
                "query": {
                    "type": "http",
                    "url": "https://mcp.example.invalid",
                    "headers": {},
                }
            },
            # Authorization 是空 Bearer。
            {
                "query": {
                    "type": "http",
                    "url": "https://mcp.example.invalid",
                    "headers": {"Authorization": "Bearer "},
                }
            },
            # Authorization 完全不是 Bearer 形状。
            {
                "query": {
                    "type": "http",
                    "url": "https://mcp.example.invalid",
                    "headers": {"Authorization": "token-without-bearer-prefix"},
                }
            },
            # headers 里混进了未知键。
            {
                "query": {
                    "type": "http",
                    "url": "https://mcp.example.invalid",
                    "headers": {"Authorization": "Bearer tok", "X-Extra": "1"},
                }
            },
            # url 不是 https：明文 Bearer 走 HTTP 等于把令牌发到网络上。
            {
                "query": {
                    "type": "http",
                    "url": "http://mcp.example.invalid",
                    "headers": {"Authorization": "Bearer tok"},
                }
            },
            # 危险形态：stdio + command，能启动任意本地进程。
            {
                "query": {
                    "type": "stdio",
                    "command": "/bin/sh",
                    "args": ["-c", "echo pwned"],
                }
            },
            # type 是 http，但混进了 stdio 才有的 command 键——多一个键也拒绝。
            {
                "query": {
                    "type": "http",
                    "url": "https://mcp.example.invalid",
                    "headers": {"Authorization": "Bearer tok"},
                    "command": "/bin/sh",
                }
            },
            # 未知 type。
            {
                "query": {
                    "type": "sse",
                    "url": "https://mcp.example.invalid",
                    "headers": {"Authorization": "Bearer tok"},
                }
            },
        )
        for servers in malformed_servers:
            with self.subTest(servers=servers):
                with tempfile.TemporaryDirectory() as root:
                    _write(
                        Path(root) / "usr-1" / ".mcp.json",
                        json.dumps({"mcpServers": servers}, ensure_ascii=False),
                    )
                    with self.assertRaises(UserMcpConfigError) as caught:
                        load_user_mcp_servers(root=root, user_id="usr-1")
                    self.assertEqual(caught.exception.code, "config_shape_invalid")

    def test_the_exact_write_side_shape_is_accepted(self) -> None:
        """正向对照：写侧唯一会产出的那一种形状必须仍然被接受——F1 的严格
        校验不能连正常配置一起挡住。"""

        with tempfile.TemporaryDirectory() as root:
            _write(
                Path(root) / "usr-1" / ".mcp.json",
                json.dumps(
                    {
                        "mcpServers": {
                            "query": {
                                "type": "http",
                                "url": "https://mcp.example.invalid",
                                "headers": {"Authorization": "Bearer tok-123"},
                            }
                        }
                    },
                    ensure_ascii=False,
                ),
            )
            servers = load_user_mcp_servers(root=root, user_id="usr-1")
            self.assertEqual(
                servers,
                {
                    "query": {
                        "type": "http",
                        "url": "https://mcp.example.invalid",
                        "headers": {"Authorization": "Bearer tok-123"},
                    }
                },
            )

    def test_error_code_never_echoes_the_configured_path_or_user_id(self) -> None:
        """error.code 只允许是本模块自定的安全码——不回显路径、内容或凭据。"""

        with self.assertRaises(UserMcpConfigError) as caught:
            load_user_mcp_servers(root="/no/such/root/dir/with-a-telling-name", user_id="usr-1")
        self.assertNotIn("telling-name", caught.exception.code)
        self.assertNotIn("usr-1", caught.exception.code)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
