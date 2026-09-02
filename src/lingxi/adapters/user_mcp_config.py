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

### A1/A2/A3：同机第三方可写利用面（外部独立审查登记，明确接受、不修）

以下三条外部独立审查逐条点出过，判定是**明确接受、登记复审条件，不在本轮
处理**——不是遗漏：

- **A1｜``lstat`` → ``open`` 之间的 TOCTOU**：``_open_home_nofollow``/
  ``_read_config`` 都是先 ``lstat`` 判定"当时不是符号链接"，再按名字
  ``open(..., O_NOFOLLOW)``；两步之间如果目标被替换成指向别处的链接，读到的
  就不是原来 ``lstat`` 看到的那个文件。
- **A2｜未拒绝硬链接**：``O_NOFOLLOW`` 挡的是符号链接，不挡硬链接——一个
  事先建好、指向别处内容的硬链接会被原样当成 ``.mcp.json`` 读进来。
- **A3｜未验证权限位 / 属主 / 扩展 ACL**：本模块只判定"是不是符号链接、
  是不是目录 / 普通文件"，不核对 ``.mcp.json`` 的属主是不是运行进程本身、
  权限位是不是 ``0440``、有没有被扩展 ACL 放宽给其他账号读取。

**接受理由（判据是可达性，三条共用）**：这三条全部要求"同一时刻，同一台
机器上存在一个能写用户目录的第三方"才谈得上被利用。而本轮：用户目录由
scheduler 经 ``LocalUserEnvironment`` 独占创建；JumpServer 高级工作台（用户
在同一台机器上拿到 shell 的唯一途径）明确不在本轮范围；执行层工具白名单
只含一个只读问数工具，Agent 没有读写文件或执行命令的能力。三个前提任一
成立之前，这条利用面不可达。

**读写两侧口径必须一致**：``adapters/user_environment.py`` 的「已知边界」
一节已经对写侧的同一族问题（``chmod`` 不清除扩展 ACL、不拒绝硬链接）作出
同样的接受——理由同样是"当前部署的用户目录由 Lingxi 独占创建"。读侧在这里
登记同一结论，不能一边放行写侧、一边把读侧的同族问题判成必修。

**复审条件**（与
[决策记录《用户之间的操作系统级隔离本轮不做，设为 JumpServer 高级工作台的
硬前置》](../../../docs/决策记录/2026-08-19-用户间操作系统级隔离本轮不做.md)
逐条一致；任一条成立都必须回到该决策记录重新判断，不得继续沿用本节的接受
结论）：

1. JumpServer 高级工作台进入实施范围；
2. 执行层工具白名单新增任何具备读文件、写文件或执行命令能力的工具；
3. 用户环境目录中出现除 ``.mcp.json`` 以外的凭据或敏感产物；
4. 同一宿主机上出现 Lingxi 之外、可登录到用户环境目录的第三方进程。
"""

from __future__ import annotations

import errno as errno_module
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping

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

#: 单个 server 配置**恰好**允许的键集合，逐字对应 ``user_environment.py`` 的
#: ``build_mcp_config`` 真实产出的形状——不多不少。任何额外键（``command``/
#: ``args`` 一类 stdio 字段最典型）都判 ``config_shape_invalid``：外部独立审查
#: 指出旧版本只检查"是不是 dict"，一份形如 ``{"type": "stdio", "command": "…"}``
#: 的配置能原样通过，读侧因此可能被诱导去启动任意本地进程。本模块只服务
#: 写侧唯一会产出的那一种形状（HTTP + Bearer），出现其它形状一律失败关闭，
#: 不尝试兼容或部分接受。
_ALLOWED_SERVER_KEYS = frozenset({"type", "url", "headers"})

#: ``headers`` 只允许这一个键——同样逐字对应写侧的产出形状。
_ALLOWED_HEADER_KEYS = frozenset({"Authorization"})

#: 端点同源核对的**可选**环境变量（对抗审查 2026-09-02 W-7）。配置了就强制
#: ``.mcp.json`` 里的 URL 与它同源（scheme + host + port 三项全等），没配就不做
#: 这一项——**刻意做成可选**：当前 worker-queue 的 env 文件里没有这个变量
#: （它只在 scheduler 侧供就绪探针使用），做成必填会让一次没有同步改配置的部署
#: 把每一个用户的问数都失败关闭。这是一道装上就更紧、没装也不会更松的闸。
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

    对抗审查 2026-09-02 W-7：此前只判 ``url.startswith("https://")``。那条判定
    放行 ``https://@evil.example``（userinfo 段能把真正的主机藏在 ``@`` 后面，
    一眼扫过去像是连到前半段）、``https://`` 后跟空主机、以及任何指向第三方的
    合法 https 地址——而这个 URL 会被原样交给 Agent 会话，带着 ``Authorization:
    Bearer <该用户的问数令牌>`` 一起发出去。也就是说，改一行 URL 就能把一个真实
    用户的令牌定向送到任意主机。

    因此这里做三件事：

    1. 真的解析一次 URL，要求 scheme 恰为 ``https``、有非空主机名；
    2. 拒绝 userinfo（``user:pass@host``）与 fragment——写侧一个都不会产出，
       它们出现在这里只可能是为了让人读错主机；
    3. 配置了 ``LINGXI_QUERY_MCP_ENDPOINT`` 时核对**同源**（scheme + host + port
       三项全等）。这一项可选，理由见该常量的注释。
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
    # W-7：**恰好一个**服务器，且服务名必须是写侧唯一会写的那一个。
    #
    # 此前这里接受任意数量、任意名字的服务器。写侧 `build_mcp_config` 只会产出
    # 一个名为 `QUERY_MCP_SERVER_NAME` 的 HTTP 服务器——多出来的每一个都不可能
    # 来自本系统，而它们会被原样交给 Agent 会话去连接。"读侧接受写侧根本不会
    # 产出的形状"就是这道边界的全部漏洞：只要能往 .mcp.json 里塞一行，就能给
    # 模型挂上一个额外的、指向任意地址的 MCP 服务器。
    #
    # 服务名还有第二重后果：工具名是 `mcp__<服务名>__<工具>`，而 worker 的只读
    # 白名单只放行 `mcp__query__` 前缀（`apps/worker/config.py` 装配期断言）。
    # 名字对不上时旧实现照样把服务器挂上去，模型的每次调用再在 PreToolUse 被
    # 无声拒绝——用户看到的是"什么都查不出来"，而不是一条诚实的配置错误。
    if len(servers) != 1:
        raise UserMcpConfigError("config_shape_invalid")
    (name, value), = servers.items()
    if name != QUERY_MCP_SERVER_NAME:
        raise UserMcpConfigError("config_shape_invalid")
    _validate_server_shape(value)
    return servers


def _validate_server_shape(value: Any) -> None:
    """严格校验单个 server 配置**恰好**是写侧会产出的那一种形状（外部独立
    审查 F1）：``{"type": "http", "url": "https://…", "headers":
    {"Authorization": "Bearer <非空令牌>"}}``，不多一个键、不少一个键。

    此前的版本只检查"是不是 dict"：一份 ``{"query": {}}``（空 server 配置）、
    一份没有 ``Authorization`` 或令牌为空的配置、乃至一份
    ``{"type": "stdio", "command": "…"}`` 都能原样通过——分别对应"用户拿到一个
    莫名其妙的失败而不是诚实的配置读不到终态"与"读侧被诱导去启动任意本地
    进程"两类问题。这里**结构性地只接受写侧唯一会产出的那一种形状**，任何
    偏离（多一个键、少一个键、类型不是 http、URL 不是 https、Authorization
    形状不对）都判 ``config_shape_invalid``——不尝试兼容、不部分接受。
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
