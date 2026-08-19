"""按用户读取问数 MCP 配置（Epic D 闸⑥）：worker 处理某个用户的任务时用的入口。

配套 :mod:`lingxi.adapters.user_environment` 的写侧（S-D-02）：那一侧把每个用户
自己的 ``.mcp.json`` 写进 ``<user_env_root>/<user_id>/.mcp.json``（形状见
``build_mcp_config``：``{"mcpServers": {<name>: {"type": "http", "url": ...,
"headers": {"Authorization": "Bearer <token>"}}}}``）。那份模块文档已经登记了
这条缺口：

    本模块**不**负责让 worker 真的用上这份配置。worker 当前从
    ``LINGXI_WORKER_MCP_SERVERS`` 读一份**全进程共用**的配置
    （``apps/worker/config.py``），改成按用户读 ``.mcp.json`` 属 worker 侧的
    接线，登记为后续。

本模块就是那条"后续"的读侧实现。**红线（不可推翻）**：

    读不到用户自己那份配置时必须失败关闭，绝不回退到全进程共用的那份。

理由：回退意味着用户 A 的问数用了一份不属于他的令牌去查数——那是越权返回数据，
是本项目最不能犯的错。因此这里**结构性地没有默认值、没有回退分支**：唯一的
返回路径是"读到、解析出、形状对"，其余全部路径都以 :class:`UserMcpConfigError`
收口，不返回 ``{}``、不返回 ``None``——那两者都会被上游误当成"这个用户没有可用
工具"而不是"失败关闭"。调用方（``apps/worker/service.py``）据此保证：从
``UserMcpConfigError`` 到"把某份配置交给 Agent 会话"之间**没有任何代码路径**。

## 为什么不复用 ``user_environment.py`` 的 dirfd 锚定写法

写侧的强锚定（``O_NOFOLLOW`` + ``lstat`` 拒绝符号链接、原子替换、残留清扫）是为了
防"进程正要往目录里写明文令牌"时被符号链接引到别处——写偏一次就是把凭据放进了
错误位置，后果不可逆，还需要处理"清理没写完的临时文件"。读侧完全不写任何东西，
风险模型更窄：唯一要挡的是"这个用户的目录 / 配置文件被换成指向别处（例如另一个
用户）的链接，于是 worker 用错的令牌去查了这次提问"——这正落在本模块要守的红线
内，因此仍然做**用户目录与配置文件的符号链接拒绝**（``O_NOFOLLOW`` + ``lstat``），
但不需要写侧那一整套原子替换与残留清扫机制。

## 已知边界（明确接受，不在本 Story 处理）

- **不做启动期预热或缓存**：每次任务都重新读一次磁盘。这是刻意的简单实现；
  未来如需要缓存必须同时处理"令牌轮换后缓存过期"的一致性问题，本 Story 不做。
- **与 ``user_environment.py`` 的 ``_USER_ID_PATTERN`` 独立维护同一形状的校验
  正则**（轻量正则，非私有符号跨模块复用）：两侧标识形态如果分道扬镳，这里会
  先报 ``invalid_user_id`` 而不是静默放行——比隐式依赖对方的私有实现更安全。
- **根目录本身不做符号链接防护**：``root`` 来自部署配置（``LINGXI_USER_ENV_ROOT``），
  不是用户可控输入；真正的攻击面在 ``<root>/<user_id>`` 这一级，见上一节。
"""

from __future__ import annotations

import errno as errno_module
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping

#: 与 ``adapters/user_environment.py`` 的 ``MCP_CONFIG_FILENAME`` 同一形状
#: （Claude Code 项目级 MCP 配置文件名）。本模块独立持有这个字符串常量而不是
#: 跨模块 import 私有名字：两侧如果分道扬镳，这里的读取会先失败（文件名对不上），
#: 不会静默用错文件。
MCP_CONFIG_FILENAME = ".mcp.json"

#: 与 ``adapters/user_environment.py`` 的 ``_USER_ID_PATTERN`` 同一形状：内部
#: 用户标识会成为一段路径分量，因此**只接受**这张白名单——``..``、``/``、空串、
#: 前导点都会被这条正则挡住。
_USER_ID_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


class UserMcpConfigError(RuntimeError):
    """按用户读取 MCP 配置失败。``code`` 只有错误码，不带路径、内容或凭据片段。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


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
    for name, value in servers.items():
        if not isinstance(name, str) or not name or not isinstance(value, dict):
            raise UserMcpConfigError("config_shape_invalid")
    return servers
