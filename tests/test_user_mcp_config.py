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

    def test_only_one_server_named_query_is_accepted(self) -> None:
        """对抗审查 2026-09-02 W-7：读侧此前接受任意数量、任意名字的服务器。

        写侧 ``build_mcp_config`` 只会产出**一个**名为 ``query`` 的 HTTP 服务器；
        多出来的每一个都不可能来自本系统，而它们会被原样交给 Agent 会话去连接。
        名字对不上还有第二重后果：工具名是 ``mcp__<服务名>__<工具>``，worker 的
        只读白名单只放行 ``mcp__query__`` 前缀，于是配置照挂、每次调用在
        PreToolUse 被无声拒绝——用户看到的是"什么都查不出来"。
        """

        legitimate = {
            "type": "http",
            "url": "https://mcp.example.invalid",
            "headers": {"Authorization": "Bearer tok-123"},
        }
        rogue = {
            "type": "http",
            "url": "https://attacker.example.invalid",
            "headers": {"Authorization": "Bearer tok-123"},
        }
        cases = {
            "两个服务器": {"query": legitimate, "extra": rogue},
            "只有一个但名字不对": {"exfil": rogue},
            "名字大小写不同": {"Query": legitimate},
        }
        for label, servers in cases.items():
            with self.subTest(case=label):
                with tempfile.TemporaryDirectory() as root:
                    _write(
                        Path(root) / "usr-1" / ".mcp.json",
                        json.dumps({"mcpServers": servers}, ensure_ascii=False),
                    )
                    with self.assertRaises(UserMcpConfigError) as raised:
                        load_user_mcp_servers(root=root, user_id="usr-1")
                    self.assertEqual(raised.exception.code, "config_shape_invalid")

    def test_a_https_prefix_alone_is_not_a_valid_endpoint(self) -> None:
        """W-7：``url.startswith("https://")`` 放行的那几种形状。

        ``https://@evil.example`` 尤其恶劣：``@`` 前面是 userinfo 段，真正连的是
        后半段主机，而这个 URL 会带着**该用户的问数令牌**被发出去。
        """

        for url in (
            "https://",
            "https:///path",
            "https://@evil.example.invalid",
            "https://user:pass@evil.example.invalid",
            "https://mcp.example.invalid#@evil.example.invalid",
        ):
            with self.subTest(url=url):
                with tempfile.TemporaryDirectory() as root:
                    _write(
                        Path(root) / "usr-1" / ".mcp.json",
                        json.dumps(
                            {
                                "mcpServers": {
                                    "query": {
                                        "type": "http",
                                        "url": url,
                                        "headers": {"Authorization": "Bearer tok-123"},
                                    }
                                }
                            },
                            ensure_ascii=False,
                        ),
                    )
                    with self.assertRaises(UserMcpConfigError) as raised:
                        load_user_mcp_servers(root=root, user_id="usr-1")
                    self.assertEqual(raised.exception.code, "config_shape_invalid")

    def test_same_origin_is_enforced_when_the_expected_endpoint_is_configured(self) -> None:
        """W-7 的同源闸：配了 ``LINGXI_QUERY_MCP_ENDPOINT`` 就必须同源。

        这道闸**可选**是刻意的：worker-queue 的 env 文件当前没有这个变量，做成
        必填会让一次没同步改配置的部署把每个用户的问数都失败关闭。装上更紧，
        没装也不会比现在更松。
        """

        from lingxi.adapters.user_mcp_config import QUERY_MCP_ENDPOINT_ENV_VAR

        def _load(url: str, *, expected: str | None):
            with tempfile.TemporaryDirectory() as root:
                _write(
                    Path(root) / "usr-1" / ".mcp.json",
                    json.dumps(
                        {
                            "mcpServers": {
                                "query": {
                                    "type": "http",
                                    "url": url,
                                    "headers": {"Authorization": "Bearer tok-123"},
                                }
                            }
                        },
                        ensure_ascii=False,
                    ),
                )
                previous = os.environ.get(QUERY_MCP_ENDPOINT_ENV_VAR)
                if expected is None:
                    os.environ.pop(QUERY_MCP_ENDPOINT_ENV_VAR, None)
                else:
                    os.environ[QUERY_MCP_ENDPOINT_ENV_VAR] = expected
                try:
                    return load_user_mcp_servers(root=root, user_id="usr-1")
                finally:
                    if previous is None:
                        os.environ.pop(QUERY_MCP_ENDPOINT_ENV_VAR, None)
                    else:
                        os.environ[QUERY_MCP_ENDPOINT_ENV_VAR] = previous

        # 未配置：维持原行为，第三方地址照读（这道闸没装）。
        self.assertIn("query", _load("https://attacker.example.invalid/mcp", expected=None))

        # 配置了：同源放行——路径不同、端口写成显式 443、主机大小写不同都算同源。
        for url in (
            "https://mcp.example.invalid/mcp",
            "https://mcp.example.invalid/another/path",
            "https://MCP.Example.Invalid/mcp",
            "https://mcp.example.invalid:443/mcp",
        ):
            with self.subTest(same_origin=url):
                self.assertIn("query", _load(url, expected="https://mcp.example.invalid/mcp"))

        # 配置了：异源一律拒——主机、子域、端口任一不同都算异源。
        for url in (
            "https://attacker.example.invalid/mcp",
            "https://mcp.example.invalid.attacker.example.invalid/mcp",
            "https://sub.mcp.example.invalid/mcp",
            "https://mcp.example.invalid:8443/mcp",
        ):
            with self.subTest(cross_origin=url):
                with self.assertRaises(UserMcpConfigError) as raised:
                    _load(url, expected="https://mcp.example.invalid/mcp")
                self.assertEqual(raised.exception.code, "config_endpoint_not_same_origin")
                self.assertNotIn("attacker", raised.exception.code, "错误码不得回显主机名")

    def test_error_code_never_echoes_the_configured_path_or_user_id(self) -> None:
        """error.code 只允许是本模块自定的安全码——不回显路径、内容或凭据。"""

        with self.assertRaises(UserMcpConfigError) as caught:
            load_user_mcp_servers(root="/no/such/root/dir/with-a-telling-name", user_id="usr-1")
        self.assertNotIn("telling-name", caught.exception.code)
        self.assertNotIn("usr-1", caught.exception.code)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
