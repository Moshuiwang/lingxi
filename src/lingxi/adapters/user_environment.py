"""用户环境创建：家目录 + 按用户的 ``.mcp.json``（Epic D / S-D-02）。

## 这条模块存在的原因是一次**已经批准的架构承诺变更**

架构设计 5.3 原本写着「用户环境里**不放任何凭据**」，问数 MCP 一律经本机代理（
`/run/lingxi/query-mcp.sock` + `SO_PEERCRED`）。**产品负责人 2026-08-17 裁定推翻了这条**：
worker → 问数 MCP 的逐用户令牌**沿用 biai-agent 老路，把 Bearer 写进用户环境配置
（`.mcp.json` 的 header）**。留痕见 [Trace #203 的裁定评论](https://github.com/Moshuiwang/lingxi/issues/203)，
长期正文见[决策记录《用户环境持有问数 MCP 令牌》](../../../docs/决策记录/2026-08-18-用户环境持有问数MCP令牌.md)。

裁定同时要求：**落盘凭据的权限与脱敏纪律参照 biai-agent 先例——文件 `440`、日志脱敏**。
本模块就是那两条纪律的实现点：

1. **`0o440`**（`r--r-----`）：属主与属组只读，其他人一个字节都读不到；写入用「同目录临时
   文件 → `chmod` → `os.replace`」，因此**任何时刻磁盘上都不存在一个权限更宽的中间态**。
   直接 `open(path, "w")` 会先按 umask 建出文件、再 `chmod`，那之间存在一个可读窗口。
2. **日志脱敏**：本模块的日志、审计与异常里**没有任何一条路径**会带上令牌明文、明文长度
   之外的片段，或它的任何编码形态。异常一律换成本模块自己的错误码
   （:class:`UserEnvironmentError`），不透传 `OSError` 的 `strerror`——它可能带上路径与
   文件内容片段。

## 失败中断各自留下什么（谁来清、怎么清）

本模块是这条链上唯一会在**磁盘**上留下东西的一步，因此把它的残留逐条写出来：

| 中断点 | 留下什么 | 谁清、怎么清 |
|---|---|---|
| 建目录之后、写配置之前失败 | 一个**空的**家目录（`700`），不含任何凭据 | 无人清，也不需要清：下一次开通复用它；账号删除流程会连同用户环境一起删 |
| 临时文件已写、``os.replace`` 之前抛异常 | 无——``finally`` 里 ``unlink`` 掉了 | 已在代码里处理 |
| 临时文件已写、进程被 ``SIGKILL`` | 一个 `440` 的临时文件，**里面有明文令牌** | 由 :meth:`LocalUserEnvironment._sweep_stale` 在**下一次**对同一用户调用时删除并留 ``WARNING``；那之前它留在磁盘上（权限已收紧，但确实存在） |
| 配置写成功、后续步骤失败（发布失败 / 就绪超时） | 一份**有效的** ``.mcp.json`` 属于一个没走到 ``active`` 的用户 | 无人清。它不构成越权：MCP 侧要能解出发布表那一行才放行，而那一行没写出去时这个令牌什么也拿不到；重跑同一条链会原样复用它 |

**唯一需要人介入的是第三行**，而它只在「进程被强杀」这一种终止方式下产生。没有为它新建
清理职责：这个目录只有本模块一条写路径，下一次调用顺手清掉的代价是零，而多一条常驻
清理职责要多一份配置、多一个失败面。

## 本模块**不**做的事（登记的边界，不是遗漏）

- **不创建 Linux 账号、不分配 uid、不 `chown`**。架构设计 5.3 的用户隔离要求
  「每个用户一个 Linux 账号 + 家目录」，而账号与 uid 由**宿主机**统一分配；在容器里做
  这件事需要新的宿主机权限，属 Trace 的 amendment 条件（「环境创建需新常驻资源 / 新权限」）。
  因此本模块只保证**目录与配置文件存在且权限收紧**，属主是运行进程本身。谁来创建账号、
  怎么把 `.mcp.json` 的属主交给目标 uid，是一次尚未做出的产品/运维决定。
- **不读、不改、不删用户自己的任何文件**。除了 `.mcp.json` 这一个文件名之外，本模块不碰
  用户目录里的任何东西。
- **不负责让 worker 真的用上这份配置**。worker 当前从
  `LINGXI_WORKER_MCP_SERVERS` 读一份**全进程共用**的配置（`apps/worker/config.py`），
  改成按用户读 `.mcp.json` 属 worker 侧的接线，登记为后续。

## 硬切窗口重签存量令牌时，重投的入口就是这里

产品负责人 2026-08-18 裁定 6：既有 26 行（旧系统 biai-agent 签发、我们不知其明文）在
**硬切窗口**由 Lingxi 统一重签、覆写发布行密文，并经用户环境链**重投** ``.mcp.json``。
那条编排的第三步就是拿新明文再调一次 :meth:`LocalUserEnvironment.ensure`——它对内容变化
就重写，权限仍是 ``440``，因此**这一侧不需要任何新代码**。

缺的是前两步，都不在本模块：①**覆写式签发**（当前 ``issue_token`` 幂等且刻意*不*覆盖，
理由见 ``adapters/postgres_mcp_token.py`` 的「为什么不提供轮换」）；②以新密文排一条发布
意图去覆写那一行。两者属硬切编排那一个 Story。

## 幂等

同一个用户反复调用 :meth:`LocalUserEnvironment.ensure`：目录已存在就不重建，配置内容逐字节
相同就**不重写**（`created=False`）。令牌签发本身也是幂等的（`adapters/postgres_mcp_token.py`
的 `ON CONFLICT DO NOTHING`），因此重入不会让同一个人的 `.mcp.json` 在两次调用之间换成
另一份令牌——而那正是"用户某天忽然问不了数"的形状。
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path

from lingxi.core.identity.onboarding_runner import EnvironmentResult

logger = logging.getLogger(__name__)

#: 用户环境里那份配置的文件名。Claude Agent SDK / Claude Code 的项目级 MCP 配置约定。
MCP_CONFIG_FILENAME = ".mcp.json"

#: 家目录权限：`rwx------`。用户环境之间互相看不到对方的产物与会话，是架构设计 5.3 的
#: 隔离边界；这里先把目录本身收紧，属主移交见模块文档的登记边界。
HOME_DIR_MODE = 0o700

#: 根目录权限：`rwxr-x---`。**不能停在 umask 默认值**（实测 `0775`）：部署前提是"用户在
#: 同一台机器上有 shell"，一个人人可读的根目录会把全部内部用户标识列出来；而属组可写时，
#: 任何同组用户还能在某个人**首次开通之前**把 `<root>/<user_id>` 预置成指向别处的符号链接，
#: 让那份令牌落到他挑的位置。属组保留 `r-x` 是给 worker 那个组留遍历权限。
ROOT_DIR_MODE = 0o750

#: 落盘凭据的文件权限：`r--r-----`。biai-agent 先例，裁定明列。
CREDENTIAL_FILE_MODE = 0o440

#: 原子写用的临时文件前缀。提成常量是因为**清扫要按它来找**：进程被 ``SIGKILL`` 掉在
#: 「临时文件已带令牌、还没 ``os.replace``」之间时，那个文件会带着明文令牌留在磁盘上，
#: 而 ``finally`` 里的清理根本没有机会执行。见 :meth:`LocalUserEnvironment._sweep_stale`。
TEMPORARY_PREFIX = ".mcp.json."

#: 内部用户标识的合法形态。它会成为一段路径，因此**只接受**这张白名单——`..`、`/`、
#: 空串、前导点都会被这条正则挡住，而不是靠调用方记得别传。
_USER_ID_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


class UserEnvironmentError(RuntimeError):
    """用户环境创建失败。``code`` 只有错误码，**不带路径、内容或凭据片段**。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def build_mcp_config(*, server_name: str, endpoint: str, token: str) -> str:
    """渲染 ``.mcp.json`` 的正文。**纯函数，便于逐字节断言。**

    形状按 Claude Code 的 HTTP MCP 服务器约定：``mcpServers.<name>`` 下的 ``type`` /
    ``url`` / ``headers``。``Authorization: Bearer <明文令牌>`` 正是这次裁定要落盘的那一位。

    键排序固定（``sort_keys=True``）且末尾带换行：幂等比较靠的是"内容逐字节相同"，
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


class LocalUserEnvironment:
    """在本机文件系统上创建用户环境。构造时不碰文件系统。"""

    def __init__(
        self,
        *,
        root: str,
        mcp_endpoint: str,
        mcp_server_name: str = "query",
        home_dir_mode: int = HOME_DIR_MODE,
        credential_file_mode: int = CREDENTIAL_FILE_MODE,
        root_dir_mode: int = ROOT_DIR_MODE,
    ) -> None:
        if not root or not str(root).strip():
            raise ValueError("用户环境根目录不能为空")
        if not str(root).startswith("/"):
            # 相对路径会让"令牌落在哪"取决于进程的工作目录——那是一个没人配置、也没人
            # 回读的隐式输入。不回显收到的值。
            raise ValueError("用户环境根目录必须是绝对路径")
        if not isinstance(mcp_endpoint, str) or not mcp_endpoint.startswith("https://"):
            # 明文 Bearer 走 HTTP 等于把令牌发到网络上。不回显收到的值。
            raise ValueError("问数 MCP 端点必须是 https")
        if not isinstance(mcp_server_name, str) or not mcp_server_name.strip():
            raise ValueError("MCP 服务名不能为空")
        if credential_file_mode & 0o007:
            # 「其他人」位一旦被放开，同一台机器上任何用户都能读到这份令牌。
            raise ValueError("落盘凭据的文件权限不得对其他用户开放")
        if root_dir_mode & 0o007 or root_dir_mode & 0o020:
            # 根目录对其他人可见 = 内部用户标识全表泄露；对属组可写 = 首次开通前可被
            # 预置成符号链接。两者都不是"权限稍宽一点"，是两条具体的攻击路径。
            raise ValueError("用户环境根目录不得对其他用户开放，也不得对属组可写")
        self._root = Path(str(root))
        self._endpoint = mcp_endpoint
        self._server_name = mcp_server_name.strip()
        self._home_dir_mode = home_dir_mode
        self._file_mode = credential_file_mode
        self._root_dir_mode = root_dir_mode

    def home_of(self, user_id: str) -> Path:
        """该用户的家目录路径。**不创建**，只算路径并校验标识形态。"""

        if not isinstance(user_id, str) or not _USER_ID_PATTERN.match(user_id):
            # 不回显收到的值：一个被误接成令牌的参数不能因为报错而泄露原值。
            raise UserEnvironmentError("invalid_user_id")
        return self._root / user_id

    def ensure(self, *, user_id: str, mcp_token: str) -> EnvironmentResult:
        """创建（或确认）用户环境，并把该用户的 MCP Bearer 落进 ``.mcp.json``。

        返回 ``created=True`` 表示这次调用真的写了配置（新建或内容变化），``False`` 表示
        本来就是这一份。
        """

        if not isinstance(mcp_token, str) or not mcp_token:
            raise UserEnvironmentError("empty_mcp_token")
        home = self.home_of(user_id)
        try:
            # **逐级建、逐级收紧**，不用 `parents=True`：那条路径上被顺手创建出来的中间
            # 目录会停在 umask 默认值，而根目录恰恰是最不该宽的那一格（见 ROOT_DIR_MODE）。
            self._root.mkdir(mode=self._root_dir_mode, exist_ok=True)
            os.chmod(self._root, self._root_dir_mode)
            home.mkdir(mode=self._home_dir_mode, exist_ok=True)
            os.chmod(home, self._home_dir_mode)
        except OSError as error:
            # 只留 errno 的符号名，不留 strerror（它带路径）。
            raise UserEnvironmentError(f"home_mkdir_{_errno_name(error)}") from None

        # **先清扫，再判断要不要写。** 上一次调用如果被 ``SIGKILL`` 掉在「临时文件已经
        # 带上令牌、还没 ``os.replace``」之间，那个文件会带着明文令牌留在磁盘上——
        # ``finally`` 里的清理在那种终止方式下根本没有机会执行。清扫放在这里而不是别处，
        # 是因为这是**唯一**会碰这个目录的写路径：不需要为它新建一条清理职责。
        self._sweep_stale(home)

        desired = build_mcp_config(
            server_name=self._server_name, endpoint=self._endpoint, token=mcp_token
        )
        target = home / MCP_CONFIG_FILENAME
        if _read_text_or_none(target) == desired:
            # 内容没变也要把权限收一次：一份被外部改宽过的既有配置，如果因为内容相同而
            # 直接跳过，会让「440」这条纪律只在首次开通那一刻成立。
            try:
                os.chmod(target, self._file_mode)
            except OSError as error:
                raise UserEnvironmentError(f"config_chmod_{_errno_name(error)}") from None
            logger.info("用户环境已就绪，配置无变化 user=%s", user_id)
            return EnvironmentResult(created=False)
        self._atomic_write(target, desired)
        # **日志里只有用户标识与文件名**，没有端点以外的任何内容，更没有令牌。
        logger.info("用户环境配置已写入 user=%s file=%s", user_id, MCP_CONFIG_FILENAME)
        return EnvironmentResult(created=True)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _sweep_stale(self, home: Path) -> None:
        """删掉这个用户目录里遗留的写入临时文件（可能带着明文令牌）。

        只删**本模块自己**建的那种名字（``.mcp.json.*.tmp``），不碰用户的任何其他文件。

        **失败关闭，不是尽力而为**：列不动或删不掉，都意味着我们管不了这个目录，而下一步
        正要往里写一份明文令牌——继续写等于把凭据放进一个自己既看不见也清不掉的地方。
        两条都抛 :class:`UserEnvironmentError`（错误码带 ``errno`` 符号名，让运维能直接
        分辨 ``EACCES`` 与 ``ENOTDIR``），并各留一条不带路径的 ``WARNING``。
        """

        try:
            leftovers = list(home.glob(f"{TEMPORARY_PREFIX}*.tmp"))
        except OSError as error:
            # **扫不动就不往这里放凭据。** 列不出目录内容意味着我们管不了这个目录，
            # 而下一步正要在里面写一份明文令牌——继续写等于把凭据放进一个自己既看不见、
            # 也清不掉的地方。这条路径同时挡住"目录被换成了别的东西"这类形态。
            logger.warning("用户环境目录无法扫描，本次不写入配置 errno=%s", _errno_name(error))
            raise UserEnvironmentError(f"sweep_failed_{_errno_name(error)}") from None
        for leftover in leftovers:
            try:
                leftover.unlink()
            except OSError as error:
                # 删不掉同样是"管不了这个目录"：一份带令牌的残留会无限期躺在那里，
                # 而我们正准备再放一份进去。响亮失败，不静默继续。
                logger.warning(
                    "用户环境残留的写入临时文件删除失败 errno=%s", _errno_name(error)
                )
                raise UserEnvironmentError(f"sweep_failed_{_errno_name(error)}") from None
            logger.warning("清理了用户环境里遗留的写入临时文件（可能带有令牌）")

    def _atomic_write(self, target: Path, content: str) -> None:
        """同目录临时文件 → 收紧权限 → ``os.replace``。

        `tempfile.mkstemp` 建出来的文件权限是 `0o600`（不受 umask 影响），因此从它存在的
        第一刻起就不比目标权限更宽；`os.replace` 在同一文件系统上是原子的，读取方要么看到
        旧的一份完整配置、要么看到新的，不会读到半截 JSON。
        """

        handle = None
        temporary: str | None = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                dir=str(target.parent), prefix=TEMPORARY_PREFIX, suffix=".tmp"
            )
            # **先收紧权限，再写内容**：`mkstemp` 给的是 `0600`，已经不比目标宽，但把
            # `fchmod` 提到写入之前，磁盘上带令牌的那一刻起权限就已经是最终值。用
            # `fchmod` 而不是按路径 `chmod`：作用于已经打开的这个文件描述符，中间没有
            # 任何"按名字再找一次"的窗口（将来补 `chown` 时同一条理由更要紧）。
            os.fchmod(descriptor, self._file_mode)
            handle = os.fdopen(descriptor, "w", encoding="utf-8")
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            handle = None
            os.replace(temporary, target)
            temporary = None
        except OSError as error:
            raise UserEnvironmentError(f"config_write_{_errno_name(error)}") from None
        finally:
            if handle is not None:  # pragma: no cover - 只在写失败时触发
                handle.close()
            if temporary is not None:
                # 失败路径上绝不留下一个带令牌的临时文件。
                try:
                    os.unlink(temporary)
                except OSError:  # pragma: no cover - 清理失败不掩盖原因
                    pass


def _read_text_or_none(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        # 读不出来（不存在、权限、编码）一律当成"不是我们要的那一份"，重写一次。
        # **不记日志**：这条路径每次开通都会走（首次一定读不到），而它的内容是凭据。
        return None


def _errno_name(error: OSError) -> str:
    """把 ``OSError`` 折成一个可进日志的短错误码。

    只取 ``errno`` 的符号名（``ENOENT``/``EACCES``…）。``str(error)`` 带路径，
    ``strerror`` 在某些平台上会带上更多上下文，两者都不进日志。
    """

    import errno as errno_module

    name = errno_module.errorcode.get(error.errno or 0)
    return name or "unknown"
