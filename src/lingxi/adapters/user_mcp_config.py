"""按用户读取问数 MCP 配置：worker 处理某个用户任务时的入口。

配套 :mod:`lingxi.adapters.user_environment` 的写侧，读取它写出的
``.mcp.json``。红线（不可推翻）：读不到用户自己那份配置时必须失败关闭，
绝不回退到全进程共用的配置——回退意味着用不属于他的令牌去查数，是越权
返回数据。结构性地没有默认值、没有回退分支，唯一返回路径是"读到、解析
出、形状对"，其余一律以 :class:`UserMcpConfigError` 收口。

与写侧不同：读侧不写任何东西，风险模型更窄，仍做符号链接拒绝，但不需要
原子替换与残留清扫机制。已知边界（明确接受）：不做启动期预热或缓存；与
写侧独立维护同一形状的标识校验正则；根目录本身不做符号链接防护（来自
部署配置，非用户可控）；TOCTOU、未拒绝硬链接、未验证权限位/属主/ACL 三
类同机第三方可写利用面，因用户目录独占创建、无第三方可写前提，判定不
可达而接受，与写侧口径一致。
"""

from __future__ import annotations

import errno as errno_module
import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from lingxi.core.mcp_naming import QUERY_MCP_SERVER_NAME

#: 与 ``adapters/user_environment.py`` 的 ``MCP_CONFIG_FILENAME`` 同一形状
#: （Claude Code 项目级 MCP 配置文件名）。本模块独立持有这个字符串常量而不是
#: 跨模块 import 私有名字：两侧如果分道扬镳，这里的读取会先失败（文件名对不上），
#: 不会静默用错文件。
MCP_CONFIG_FILENAME = ".mcp.json"

#: 与 ``adapters/user_environment.py`` 的 ``_USER_ID_PATTERN`` 同一形状：内部
#: 用户标识会成为一段路径分量，因此**只接受**这张白名单——``..``、``/``、空串、
#: 前导点都会被这条正则挡住。
_USER_ID_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")

#: 单个 server 配置恰好允许的键集合，逐字对应 ``build_mcp_config`` 真实产出的
#: 形状——不多不少。额外键（``command``/``args`` 一类 stdio 字段）都判
#: ``config_shape_invalid``：只检查"是不是 dict"会放行一份伪装成 stdio 服务器
#: 的配置，读侧因此可能被诱导去启动任意本地进程。
_ALLOWED_SERVER_KEYS = frozenset({"type", "url", "headers"})

#: ``headers`` 只允许这一个键——同样逐字对应写侧的产出形状。
_ALLOWED_HEADER_KEYS = frozenset({"Authorization"})

#: 端点同源核对的可选环境变量。配置了就强制 ``.mcp.json`` 里的 URL 与它同源
#: （scheme + host + port 三项全等），没配就不做这一项——刻意做成可选：当前
#: worker-queue 的 env 文件里没有这个变量，做成必填会让一次没同步改配置的
#: 部署把每个用户的问数都失败关闭。这是一道装上就更紧、没装也不会更松的闸。
QUERY_MCP_ENDPOINT_ENV_VAR = "LINGXI_QUERY_MCP_ENDPOINT"

#: ``Authorization`` 必须是 ``Bearer <非空且不含空白的令牌>``。真实令牌是
#: base64/hex 一类字符集，不含空白；用这条形状本身筛掉空令牌（``Bearer``、
#: ``Bearer ``）与夹带额外内容的令牌（``Bearer x y``）。
_BEARER_TOKEN_PATTERN = re.compile(r"\ABearer \S+\Z")


class UserMcpConfigError(RuntimeError):
    """按用户读取 MCP 配置失败。``code`` 只有错误码，不带路径、内容或凭据片段。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _validate_endpoint(url: str) -> None:
    """URL 必须是形状完好的 https 端点；配了 ``QUERY_MCP_ENDPOINT_ENV_VAR`` 时还要同源。

    单看 ``url.startswith("https://")`` 不够：会放行 userinfo 段藏了真实主机的
    地址（``https://@evil.example``）、空主机名、以及任何指向第三方的合法
    https 地址——而这个 URL 会带着该用户的问数令牌原样交给 Agent 会话发出去，
    改一行 URL 就能把令牌定向送到任意主机。因此这里解析一次 URL 要求 scheme
    恰为 https、有非空主机名，拒绝 userinfo 与 fragment（写侧一个都不会产出），
    并在配了 ``LINGXI_QUERY_MCP_ENDPOINT`` 时核对同源（scheme+host+port 全等，
    该项可选，理由见该常量注释）。
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        raise UserMcpConfigError("config_shape_invalid") from None
    if parts.scheme != "https" or not parts.hostname:
        raise UserMcpConfigError("config_shape_invalid")
    if parts.username is not None or parts.password is not None or parts.fragment:
        raise UserMcpConfigError("config_shape_invalid")

    expected = (os.environ.get(QUERY_MCP_ENDPOINT_ENV_VAR) or "").strip()
    if not expected:
        return
    try:
        reference = urlsplit(expected)
    except ValueError:
        # 部署侧配错了这个变量不该把用户的问数一起打死：只是这道可选闸不生效。
        return
    if not reference.hostname:
        return
    if _origin(parts) != _origin(reference):
        # 不回显任何一侧的主机名：错误码要能进日志，而 URL 可能带用户可控内容。
        raise UserMcpConfigError("config_endpoint_not_same_origin")


def _origin(parts: Any) -> tuple[str, str, int | None]:
    """``(scheme, host, port)``——主机名大小写不敏感，端口按 scheme 归一。"""
    port = parts.port
    if port is None and parts.scheme == "https":
        port = 443
    return (parts.scheme.lower(), (parts.hostname or "").lower(), port)


def _errno_name(error: OSError) -> str:
    return errno_module.errorcode.get(error.errno or 0) or "unknown"


def load_user_mcp_servers(*, root: str, user_id: str) -> Mapping[str, Any]:
    """读取 ``user_id`` 自己的 ``.mcp.json``，返回其中的 ``mcpServers`` 映射
    （``server_name -> server_config``，与 ``WorkerConfig.mcp_servers`` 同一形状，
    可以直接作为 ``mcp_servers=`` 传给 ``build_agent_options``）。

    这是本模块**唯一的对外入口**，没有默认值参数、没有回退开关：任何失败都以
    :class:`UserMcpConfigError` 抛出，调用方据此走既有的失败关闭终态，不得吞掉
    异常后回落到别的配置来源（模块文档顶部的红线）。
    """
    if not isinstance(root, str) or not root.strip():
        raise UserMcpConfigError("root_unconfigured")
    name = _validated_user_id(user_id)

    try:
        root_fd = os.open(str(Path(root)), os.O_RDONLY | os.O_DIRECTORY)
    except OSError as error:
        raise UserMcpConfigError(f"root_{_errno_name(error)}") from None
    try:
        home_fd = _open_home_nofollow(root_fd, name)
        try:
            payload = _read_config(home_fd)
        finally:
            os.close(home_fd)
    finally:
        os.close(root_fd)

    return _parse_mcp_servers(payload)


def _validated_user_id(user_id: str) -> str:
    if not isinstance(user_id, str) or not _USER_ID_PATTERN.match(user_id):
        # 不回显收到的值：一个被误接成令牌或提示词片段的参数不能因为报错而泄露。
        raise UserMcpConfigError("invalid_user_id")
    return user_id


def _open_home_nofollow(root_fd: int, name: str) -> int:
    """``lstat`` 确认不是符号链接后再 ``O_NOFOLLOW`` 打开。

    两个已知、明确接受的利用面：``lstat``→``open`` 之间的 TOCTOU（目标在两步
    之间被替换）；硬链接不被 ``O_NOFOLLOW`` 拦截。两者都要求同一时刻同一台
    机器上存在能写用户目录的第三方，本轮部署下该前提不成立，与写侧
    （``adapters/user_environment.py``）同一口径接受。
    """
    try:
        info = os.lstat(name, dir_fd=root_fd)
    except FileNotFoundError:
        # 用户还没走完首次开通（环境创建在开通编排里，见
        # ``apps/scheduler/onboarding.py``），此时读不到属正常的失败关闭形状，
        # 不是异常情况。
        raise UserMcpConfigError("home_missing") from None
    except OSError as error:
        raise UserMcpConfigError(f"home_{_errno_name(error)}") from None
    if stat.S_ISLNK(info.st_mode):
        # 顺着软链走下去，就可能读到别的用户（甚至别的任意文件）的内容，
        # 当作这个用户自己的令牌用掉——正是本模块要挡的红线。
        raise UserMcpConfigError("home_is_symlink")
    if not stat.S_ISDIR(info.st_mode):
        raise UserMcpConfigError("home_not_a_directory")
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
    except OSError as error:
        raise UserMcpConfigError(f"home_{_errno_name(error)}") from None


def _read_config(home_fd: int) -> bytes:
    try:
        info = os.lstat(MCP_CONFIG_FILENAME, dir_fd=home_fd)
    except FileNotFoundError:
        raise UserMcpConfigError("config_missing") from None
    except OSError as error:
        raise UserMcpConfigError(f"config_{_errno_name(error)}") from None
    if stat.S_ISLNK(info.st_mode):
        raise UserMcpConfigError("config_is_symlink")
    if not stat.S_ISREG(info.st_mode):
        raise UserMcpConfigError("config_not_a_file")
    try:
        fd = os.open(MCP_CONFIG_FILENAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=home_fd)
    except OSError as error:
        raise UserMcpConfigError(f"config_{_errno_name(error)}") from None
    try:
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as error:
        raise UserMcpConfigError(f"config_read_{_errno_name(error)}") from None
    finally:
        os.close(fd)


def _parse_mcp_servers(payload: bytes) -> Mapping[str, Any]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise UserMcpConfigError("config_not_utf8") from None
    try:
        document = json.loads(text)
    except ValueError:
        raise UserMcpConfigError("config_invalid_json") from None
    if not isinstance(document, dict):
        raise UserMcpConfigError("config_shape_invalid")
    servers = document.get("mcpServers")
    # 空 dict 与"没有这个键"同等对待：一份"配置了、但一个 MCP 服务器都没有"的
    # `.mcp.json` 与"读取失败"效果必须相同——两者都不该让这个用户的会话在没有
    # 任何工具的情况下静默跑起来（那同样是"悄悄改变了这个用户的能力范围"，
    # 只是方向相反）。
    if not isinstance(servers, dict) or not servers:
        raise UserMcpConfigError("config_shape_invalid")
    # 恰好一个服务器，且服务名必须是写侧唯一会写的那一个：写侧 `build_mcp_config`
    # 只产出一个名为 `QUERY_MCP_SERVER_NAME` 的 HTTP 服务器，多出来的每一个都不
    # 可能来自本系统却会被原样交给 Agent 会话去连接；服务名对不上时工具名前缀
    # `mcp__query__` 与 worker 白名单也对不上，模型的调用会在 PreToolUse 被无声
    # 拒绝，而不是一条诚实的配置错误。
    if len(servers) != 1:
        raise UserMcpConfigError("config_shape_invalid")
    ((name, value),) = servers.items()
    if name != QUERY_MCP_SERVER_NAME:
        raise UserMcpConfigError("config_shape_invalid")
    _validate_server_shape(value)
    return servers


def _validate_server_shape(value: Any) -> None:
    """严格校验单个 server 配置恰好是写侧会产出的那一种形状：
    ``{"type": "http", "url": "https://…", "headers":
    {"Authorization": "Bearer <非空令牌>"}}``，不多一个键、不少一个键。

    只检查"是不是 dict"不够：``{"type": "stdio", "command": "…"}`` 这类形状
    也能原样通过，读侧因此可能被诱导去启动任意本地进程。这里结构性地只
    接受写侧唯一会产出的那一种形状，任何偏离（多/少键、类型不是 http、
    URL 不是 https、Authorization 形状不对）都判 ``config_shape_invalid``
    ——不尝试兼容、不部分接受。
    """
    if not isinstance(value, dict):
        raise UserMcpConfigError("config_shape_invalid")
    if set(value) != _ALLOWED_SERVER_KEYS:
        raise UserMcpConfigError("config_shape_invalid")
    if value.get("type") != "http":
        raise UserMcpConfigError("config_shape_invalid")
    url = value.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        # 明文 Bearer 走 HTTP 等于把令牌发到网络上——与写侧
        # ``LocalUserEnvironment.__init__`` 对 ``mcp_endpoint`` 的同一条校验。
        raise UserMcpConfigError("config_shape_invalid")
    _validate_endpoint(url)
    headers = value.get("headers")
    if not isinstance(headers, dict) or set(headers) != _ALLOWED_HEADER_KEYS:
        raise UserMcpConfigError("config_shape_invalid")
    authorization = headers.get("Authorization")
    if not isinstance(authorization, str) or not _BEARER_TOKEN_PATTERN.match(authorization):
        raise UserMcpConfigError("config_shape_invalid")
