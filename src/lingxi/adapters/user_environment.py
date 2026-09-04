"""用户环境创建：家目录 + 按用户的 ``.mcp.json``。

本模块承载一次已批准的架构承诺变更：从「用户环境不放任何凭据、问数 MCP 一律
经本机代理」改为「沿用旧系统老路，把逐用户 Bearer 写进 ``.mcp.json``」，权限
与脱敏纪律参照旧系统先例（文件 ``0440``、日志脱敏）；正文见决策记录索引下的
《用户环境持有问数 MCP 令牌》。本模块是那次承诺变更的全部保护，每一条防线
都不是可选加固：目录/文件权限构造期强制为定值、全程 dirfd 锚定拒绝符号链接、
同目录临时文件原子替换、
每次写入前清扫残留外加启动期全量清扫、日志与异常里只有标识/文件名/errno
符号名。已知边界（明确接受）：异常帧 locals 可能被错误上报平台采集到明文
令牌（当前部署无此类平台）；``chmod`` 不清除扩展 ACL、不拒绝硬链接（用户
目录由本模块独占创建，接受）。不做的事：不创建 Linux 账号/uid/chown（由
宿主机统一分配）；不碰用户目录里除 ``.mcp.json`` 外的任何文件；不负责
worker 真的用上配置（读侧在 ``adapters/user_mcp_config.py``）。
"""

from __future__ import annotations

import errno as errno_module
import json
import logging
import os
import re
import secrets
import stat
import sys
from pathlib import Path

from lingxi.core.identity.onboarding_runner import EnvironmentResult
from lingxi.core.mcp_naming import QUERY_MCP_SERVER_NAME

logger = logging.getLogger(__name__)

#: 用户环境里那份配置的文件名。Claude Agent SDK / Claude Code 的项目级 MCP 配置约定。
MCP_CONFIG_FILENAME = ".mcp.json"

#: 原子写用的临时文件前缀与后缀。提成常量是因为**清扫要按它们来找**：进程被 ``SIGKILL``
#: 掉在「临时文件已带令牌、还没 ``os.replace``」之间时，那个文件会带着明文令牌留在磁盘上，
#: 而同一次调用里的清理根本没有机会执行。
TEMPORARY_PREFIX = ".mcp.json."
TEMPORARY_SUFFIX = ".tmp"

#: 根目录权限：``rwxr-x---``。**不能停在 umask 默认值**（实测 ``0775``）：部署前提是
#: 「用户在同一台机器上有 shell」，人人可读的根目录会把全部内部用户标识列出来；属组可写时，
#: 任何同组用户还能在某个人**首次开通之前**把 ``<root>/<user_id>`` 预置成指向别处的符号
#: 链接。属组保留 ``r-x`` 是给 worker 那个组留遍历权限。
ROOT_DIR_MODE = 0o750

#: 家目录权限：``rwx------``。用户环境之间互相看不到对方的产物与会话（架构设计 5.3）。
HOME_DIR_MODE = 0o700

#: 落盘凭据的文件权限：``r--r-----``。旧系统先例。
CREDENTIAL_FILE_MODE = 0o440

#: 内部用户标识的合法形态。它会成为一段路径分量，因此**只接受**这张白名单——``..``、``/``、
#: 空串、前导点都会被这条正则挡住，而不是靠调用方记得别传。
_USER_ID_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")

#: 写进 ``.mcp.json`` 的 MCP 服务名——单一事实来源。``apps/worker/config.py``
#: 的只读工具白名单要求每一项都以 ``mcp__{QUERY_MCP_SERVER_NAME}__`` 开头并在
#: 装配期断言，两侧共用这一个常量、不各自维护一份字符串（曾经两处服务名不
#: 同步，导致真实工具调用被无声拒绝）。改这个值须同时确认 worker 白名单的
#: 装配期断言仍然通过。定义在 ``lingxi.core.mcp_naming``，本模块重新导出。


class UserEnvironmentError(RuntimeError):
    """用户环境创建失败。``code`` 只有错误码，**不带路径、内容或凭据片段**。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def build_mcp_config(*, server_name: str, endpoint: str, token: str) -> str:
    """渲染 ``.mcp.json`` 的正文。**纯函数，便于逐字节断言。**

    形状按 Claude Code 的 HTTP MCP 服务器约定：``mcpServers.<name>`` 下的 ``type`` /
    ``url`` / ``headers``。``Authorization: Bearer <明文令牌>`` 正是本模块要落盘的那一位。

    键排序固定（``sort_keys=True``）且末尾带换行：幂等比较靠的是「内容逐字节相同」，
    键序漂移会让每次开通都重写一次文件。
    """
    document = {
        "mcpServers": {
            server_name: {
                "type": "http",
                "url": endpoint,
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    }
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _errno_name(error: OSError) -> str:
    """把 ``OSError`` 折成一个可进日志的短错误码。

    只取 ``errno`` 的符号名（``ENOENT``/``EACCES``/``ELOOP``…）。``str(error)`` 带路径，
    ``strerror`` 在某些平台上还会带更多上下文，两者都不进日志。
    """
    return errno_module.errorcode.get(error.errno or 0) or "unknown"


class LocalUserEnvironment:
    """在本机文件系统上创建用户环境。构造时不碰文件系统。

    全程 dirfd 锚定：根目录与家目录各自打开一次（``O_NOFOLLOW`` + ``lstat``
    确认不是符号链接），此后的扫描、读取、创建、``chmod``、``unlink``、
    ``replace`` 全部相对那个 fd 进行，避免按路径重新解析一次多出的"解析到
    别处"窗口。中断点里唯一留隐患的是进程被 SIGKILL 在临时文件已写、还没
    ``os.replace`` 之间——留一个带明文令牌的临时文件，靠启动期全量清扫与
    下次调用时的目录内清扫收敛；其余中断点要么无残留要么响亮失败。
    """

    def __init__(
        self,
        *,
        root: str,
        mcp_endpoint: str,
        mcp_server_name: str = QUERY_MCP_SERVER_NAME,
        home_dir_mode: int = HOME_DIR_MODE,
        credential_file_mode: int = CREDENTIAL_FILE_MODE,
        root_dir_mode: int = ROOT_DIR_MODE,
    ) -> None:
        if not root or not str(root).strip():
            raise ValueError("用户环境根目录不能为空")
        if not str(root).startswith("/"):
            # 相对路径会让「令牌落在哪」取决于进程的工作目录——那是一个没人配置、也没人
            # 回读的隐式输入。不回显收到的值。
            raise ValueError("用户环境根目录必须是绝对路径")
        if not isinstance(mcp_endpoint, str) or not mcp_endpoint.startswith("https://"):
            # 明文 Bearer 走 HTTP 等于把令牌发到网络上。不回显收到的值。
            raise ValueError("问数 MCP 端点必须是 https")
        if not isinstance(mcp_server_name, str) or not mcp_server_name.strip():
            raise ValueError("MCP 服务名不能为空")
        # **三个模式强制为定值，不是可调参数。** 它们是这次架构承诺变更的安全不变量：
        # 允许「稍微宽一点」等于允许把凭据放进一个别人能读的地方。参数保留只是为了让这条
        # 拒绝本身可以被证伪（用例传别的值必须抛）。
        for label, value, expected in (
            ("根目录", root_dir_mode, ROOT_DIR_MODE),
            ("家目录", home_dir_mode, HOME_DIR_MODE),
            ("落盘凭据", credential_file_mode, CREDENTIAL_FILE_MODE),
        ):
            if value != expected:
                raise ValueError(f"{label}权限必须恰为 {expected:#o}：它是安全不变量，不是可调参数")
        self._root = Path(str(root))
        self._endpoint = mcp_endpoint
        self._server_name = mcp_server_name.strip()
        self._root_dir_mode = root_dir_mode
        self._home_dir_mode = home_dir_mode
        self._file_mode = credential_file_mode

    # ------------------------------------------------------------------
    # 对外
    # ------------------------------------------------------------------

    def home_of(self, user_id: str) -> Path:
        """该用户的家目录路径。**不创建**，只算路径并校验标识形态。"""
        return self._root / self._validated(user_id)

    def ensure(self, *, user_id: str, mcp_token: str) -> EnvironmentResult:
        """创建（或确认）用户环境，并把该用户的 MCP Bearer 落进 ``.mcp.json``。

        返回 ``created=True`` 表示这次调用真的写了配置（新建或内容变化），``False`` 表示
        本来就是这一份（此时仍然收了一次权限）。
        """
        name = self._validated(user_id)
        if not isinstance(mcp_token, str) or not mcp_token:
            raise UserEnvironmentError("empty_mcp_token")

        root_fd = self._open_root()
        try:
            home_fd = self._open_child_dir(root_fd, name, self._home_dir_mode, "home")
            try:
                # **先清扫，再判断要不要写**：上一次调用如果被 ``SIGKILL`` 掉在「临时文件
                # 已经带上令牌、还没 replace」之间，那个文件会带着明文令牌留在这里。
                self._sweep_dir(home_fd)
                desired = self._encode(
                    build_mcp_config(
                        server_name=self._server_name,
                        endpoint=self._endpoint,
                        token=mcp_token,
                    )
                )
                if self._read_config(home_fd) == desired:
                    # 内容没变也要把权限收一次：一份被外部改宽过的既有配置，如果因为内容
                    # 相同就跳过，`0440` 这条纪律只在首次开通那一刻成立。
                    self._chmod_config(home_fd)
                    logger.info("用户环境已就绪，配置无变化 user=%s", name)
                    return EnvironmentResult(created=False)
                self._atomic_write(home_fd, desired)
                # **日志里只有用户标识与文件名**，没有端点以外的任何内容，更没有令牌。
                logger.info("用户环境配置已写入 user=%s file=%s", name, MCP_CONFIG_FILENAME)
                return EnvironmentResult(created=True)
            finally:
                os.close(home_fd)
        finally:
            os.close(root_fd)

    def sweep_all(self) -> int:
        """**启动期全量清扫**：把根目录下每个家目录里的写入临时文件都清掉，返回清掉几个。

        没有它，被 ``SIGKILL`` 留下的带令牌临时文件只会在「这个用户下一次再走开通」时被
        清掉——而一个不再重试的用户意味着**没有时间上界**。装配开通职责时跑一次，把上界
        压到「至多一个进程生命周期」。

        根目录还不存在时返回 ``0``（还没有任何用户环境，不是错误）。其余失败一律抛出：
        扫不动就意味着我们管不了这个目录，而这个进程接下来正要往里写明文令牌。
        """
        try:
            root_fd = self._open_root(create=False)
        except FileNotFoundError:
            return 0
        cleaned = 0
        try:
            try:
                with os.scandir(root_fd) as entries:
                    names = [entry.name for entry in entries if entry.is_dir(follow_symlinks=False)]
            except OSError as error:
                logger.warning("用户环境根目录无法扫描 errno=%s", _errno_name(error))
                raise UserEnvironmentError(f"sweep_failed_{_errno_name(error)}") from None
            for name in names:
                if not _USER_ID_PATTERN.match(name):
                    # 不是我们建的那种名字：不碰。
                    continue
                home_fd = self._open_child_dir(root_fd, name, self._home_dir_mode, "home")
                try:
                    cleaned += self._sweep_dir(home_fd)
                finally:
                    os.close(home_fd)
        finally:
            os.close(root_fd)
        if cleaned:
            logger.warning("启动期清扫了 %s 个用户环境写入临时文件（可能带有令牌）", cleaned)
        return cleaned

    # ------------------------------------------------------------------
    # 内部：标识与目录
    # ------------------------------------------------------------------

    def _validated(self, user_id: str) -> str:
        if not isinstance(user_id, str) or not _USER_ID_PATTERN.match(user_id):
            # 不回显收到的值：一个被误接成令牌的参数不能因为报错而泄露原值。
            raise UserEnvironmentError("invalid_user_id")
        return user_id

    def _open_root(self, *, create: bool = True) -> int:
        parent = self._root.parent
        name = self._root.name
        if not name:  # pragma: no cover - 绝对路径校验已经挡住了根 "/"
            raise UserEnvironmentError("invalid_root")
        try:
            parent_fd = os.open(str(parent), os.O_RDONLY | os.O_DIRECTORY)
        except FileNotFoundError:
            if not create:
                raise
            raise UserEnvironmentError("root_parent_ENOENT") from None
        except OSError as error:
            raise UserEnvironmentError(f"root_parent_{_errno_name(error)}") from None
        try:
            return self._open_child_dir(parent_fd, name, self._root_dir_mode, "root", create=create)
        finally:
            os.close(parent_fd)

    def _open_child_dir(
        self, parent_fd: int, name: str, mode: int, label: str, *, create: bool = True
    ) -> int:
        """在 ``parent_fd`` 下建（或确认）一个目录并把它打开成 dirfd。

        三件事一起做，缺一不可：``mkdir`` 用目标权限而不是 umask 默认值；``O_NOFOLLOW``
        + ``lstat`` 拒绝符号链接（**JumpServer 用户同机有 shell，把家目录换成软链是可达
        攻击**）；``fchmod`` 把已存在目录的权限收回定值。
        """
        if create:
            try:
                os.mkdir(name, mode=mode, dir_fd=parent_fd)
            except FileExistsError:
                pass
            except OSError as error:
                raise UserEnvironmentError(f"{label}_mkdir_{_errno_name(error)}") from None
        try:
            info = os.lstat(name, dir_fd=parent_fd)
        except FileNotFoundError:
            if create:  # pragma: no cover - 刚建完不可能不存在
                raise UserEnvironmentError(f"{label}_missing") from None
            raise
        except OSError as error:
            raise UserEnvironmentError(f"{label}_lstat_{_errno_name(error)}") from None
        if stat.S_ISLNK(info.st_mode):
            # 顺着软链走下去，明文令牌就落到别人挑的位置了。
            logger.warning("用户环境目录是符号链接，拒绝使用 kind=%s", label)
            raise UserEnvironmentError(f"{label}_is_symlink")
        if not stat.S_ISDIR(info.st_mode):
            raise UserEnvironmentError(f"{label}_not_a_directory")
        try:
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        except OSError as error:
            raise UserEnvironmentError(f"{label}_open_{_errno_name(error)}") from None
        try:
            os.fchmod(fd, mode)
        except OSError as error:
            os.close(fd)
            raise UserEnvironmentError(f"{label}_chmod_{_errno_name(error)}") from None
        return fd

    # ------------------------------------------------------------------
    # 内部：清扫
    # ------------------------------------------------------------------

    def _sweep_dir(self, home_fd: int) -> int:
        """删掉这个用户目录里遗留的写入临时文件（可能带着明文令牌），返回删了几个。

        只删**本模块自己**建的那种名字，不碰用户的任何其他文件。

        **失败关闭，不是尽力而为**：列不动或删不掉都意味着管不了这个目录，而下一步
        正要往里写明文令牌。两条都抛 :class:`UserEnvironmentError`（错误码带
        ``errno`` 符号名），并各留一条不带路径的 ``WARNING``。用 ``os.scandir``
        而非 ``Path.glob``：后者会吞掉部分 ``OSError`` 并表现为空结果，让
        "目录不可列"悄悄读成"没有残留"。
        """
        try:
            with os.scandir(home_fd) as entries:
                names = [
                    entry.name
                    for entry in entries
                    if entry.name.startswith(TEMPORARY_PREFIX)
                    and entry.name.endswith(TEMPORARY_SUFFIX)
                ]
        except OSError as error:
            logger.warning("用户环境目录无法扫描，本次不写入配置 errno=%s", _errno_name(error))
            raise UserEnvironmentError(f"sweep_failed_{_errno_name(error)}") from None
        cleaned = 0
        for name in names:
            try:
                os.unlink(name, dir_fd=home_fd)
            except FileNotFoundError:
                continue
            except OSError as error:
                logger.warning("用户环境残留的写入临时文件删除失败 errno=%s", _errno_name(error))
                raise UserEnvironmentError(f"sweep_failed_{_errno_name(error)}") from None
            cleaned += 1
            logger.warning("清理了用户环境里遗留的写入临时文件（可能带有令牌）")
        return cleaned

    # ------------------------------------------------------------------
    # 内部：读写
    # ------------------------------------------------------------------

    def _encode(self, content: str) -> bytes:
        """把正文编成字节。**编码失败必须包装**。

        令牌若含孤立代理项，``UnicodeEncodeError`` 的 ``object`` 属性是**整份 JSON**——
        也就是完整令牌；它会随 ``repr(exception)`` 进日志与错误上报。
        """
        try:
            return content.encode("utf-8")
        except UnicodeEncodeError:
            raise UserEnvironmentError("config_encode_failed") from None

    def _read_config(self, home_fd: int) -> bytes | None:
        try:
            fd = os.open(MCP_CONFIG_FILENAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=home_fd)
        except OSError:
            # 读不出来（不存在、权限、是软链）一律当成「不是我们要的那一份」，重写一次。
            # **不记日志**：这条路径每次开通都会走（首次一定读不到），而它的内容是凭据。
            return None
        try:
            chunks = []
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        except OSError:
            return None
        finally:
            os.close(fd)

    def _chmod_config(self, home_fd: int) -> None:
        """把既有配置的权限收回定值。

        ``follow_symlinks=False`` 是**第二道**防线，今天走不到：一份软链化的 ``.mcp.json``
        在 :meth:`_read_config` 那一步就因为 ``O_NOFOLLOW`` 读不出来，于是必然落到重写
        分支、软链被 ``os.replace`` 顶掉，根本到不了这里。留着它是因为读取那一侧将来若被
        改动，这里不该跟着一起失守——但也**不为它编一条测不到的用例**：它的变异不会变红，
        如实登记在这里。
        """
        try:
            os.chmod(
                MCP_CONFIG_FILENAME,
                self._file_mode,
                dir_fd=home_fd,
                follow_symlinks=False,
            )
        except OSError as error:
            raise UserEnvironmentError(f"config_chmod_{_errno_name(error)}") from None

    def _atomic_write(self, home_fd: int, payload: bytes) -> None:
        """同目录临时文件 → ``fchmod`` → 写 → ``fsync`` → ``os.replace``。

        ``O_CREAT|O_EXCL|O_NOFOLLOW`` 建出来的文件初始权限是 ``0600``，随即 ``fchmod`` 到
        目标值——**磁盘上带令牌的那一刻起权限就已经是最终值**，中间没有「按名字再找一次」
        的窗口。``os.replace`` 在同一文件系统上是原子的，读取方要么看到旧的一份完整配置、
        要么看到新的，不会读到半截 JSON。
        """
        temporary = f"{TEMPORARY_PREFIX}{os.getpid()}.{secrets.token_hex(8)}{TEMPORARY_SUFFIX}"
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=home_fd,
            )
        except OSError as error:
            raise UserEnvironmentError(f"config_write_{_errno_name(error)}") from None
        handed_over = False
        try:
            try:
                os.fchmod(fd, self._file_mode)
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                # ``close`` 单独保护：它自己抛错时不得让下面的清理被跳过，也不得把上面
                # 真正的失败原因盖掉——那条才是运维要看的。
                try:
                    os.close(fd)
                except OSError as error:
                    logger.warning("用户环境临时文件关闭失败 errno=%s", _errno_name(error))
            os.replace(temporary, MCP_CONFIG_FILENAME, src_dir_fd=home_fd, dst_dir_fd=home_fd)
            handed_over = True
        except OSError as error:
            raise UserEnvironmentError(f"config_write_{_errno_name(error)}") from None
        finally:
            if not handed_over:
                self._discard(home_fd, temporary)

    def _discard(self, home_fd: int, temporary: str) -> None:
        """删掉那个还带着明文令牌的临时文件。**绝不静默吞。**

        删不掉是一件必须让人知道的事：一份带令牌的残留会无限期躺在磁盘上。因此总是留一条
        ``WARNING``；并且在**没有在途异常**时抛出——有在途异常时不抛，是为了不把真正的
        失败原因盖掉（两者都已经在日志里）。
        """
        try:
            os.unlink(temporary, dir_fd=home_fd)
            return
        except FileNotFoundError:
            return
        except OSError as error:
            code = _errno_name(error)
        logger.warning("用户环境写入临时文件删除失败，可能残留明文令牌 errno=%s", code)
        if sys.exc_info()[0] is None:
            raise UserEnvironmentError(f"config_cleanup_{code}")
