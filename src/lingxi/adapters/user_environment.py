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

#: 落盘凭据的文件权限：`r--r-----`。biai-agent 先例，裁定明列。
CREDENTIAL_FILE_MODE = 0o440

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
    ) -> None:
        if not root or not str(root).strip():
            raise ValueError("用户环境根目录不能为空")
        if not isinstance(mcp_endpoint, str) or not mcp_endpoint.startswith("https://"):
            # 明文 Bearer 走 HTTP 等于把令牌发到网络上。不回显收到的值。
            raise ValueError("问数 MCP 端点必须是 https")
        if not isinstance(mcp_server_name, str) or not mcp_server_name.strip():
            raise ValueError("MCP 服务名不能为空")
        if credential_file_mode & 0o007:
            # "其他人"位一旦被放开，同一台机器上任何用户都能读到这份令牌。
            raise ValueError("落盘凭据的文件权限不得对其他用户开放")
        self._root = Path(str(root))
        self._endpoint = mcp_endpoint
        self._server_name = mcp_server_name.strip()
        self._home_dir_mode = home_dir_mode
        self._file_mode = credential_file_mode

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
            home.mkdir(parents=True, exist_ok=True)
            os.chmod(home, self._home_dir_mode)
        except OSError as error:
            # 只留 errno 的符号名，不留 strerror（它带路径）。
            raise UserEnvironmentError(f"home_mkdir_{_errno_name(error)}") from None

        desired = build_mcp_config(
            server_name=self._server_name, endpoint=self._endpoint, token=mcp_token
        )
        target = home / MCP_CONFIG_FILENAME
        if _read_text_or_none(target) == desired:
            logger.info("用户环境已就绪，配置无变化 user=%s", user_id)
            return EnvironmentResult(created=False)
        self._atomic_write(target, desired)
        # **日志里只有用户标识与文件名**，没有端点以外的任何内容，更没有令牌。
        logger.info("用户环境配置已写入 user=%s file=%s", user_id, MCP_CONFIG_FILENAME)
        return EnvironmentResult(created=True)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

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
                dir=str(target.parent), prefix=".mcp.json.", suffix=".tmp"
            )
            handle = os.fdopen(descriptor, "w", encoding="utf-8")
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            handle = None
            os.chmod(temporary, self._file_mode)
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
