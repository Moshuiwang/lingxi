"""Agent 会话 JSONL 的物理清理（Issue #153）。

只做形状：给定一个会话根目录与 ``agent_session_id``，删除匹配的 JSONL 文件。真正
「这个 session id 该不该删」的判定住在数据库（``clear_agent_session`` /
``sweep_idle_conversations`` / ``discard_stale_agent_session``（入队轮换判废，
2026-08-23） / ``clear_delivered_content_for_user`` / ``_queue_overwritten_session``
（收口写回覆盖旧值）五处触发点各自排队，排队实现在
``adapters/postgres_conversation/_transaction.py`` 的
``_Transaction._queue_session_cleanup``；本文件消费的两个方法
``claim_session_cleanups``/``mark_session_cleanups_done`` 在
``adapters/postgres_conversation/_queue_session_cleanup.py``，Issue #239 起
按读写边界拆分自原单文件，既有调用点用到的名字全部保留，见包 __init__.py 的边界说明）；
本模块不读写数据库，只碰文件系统——与 ``core/`` 「无 I/O」的边界对称地反过来：这里
只有 I/O、不含判定，因此放在 ``apps/worker/`` 而不是 ``core/``。

**为什么按文件名搜索，而不是直接拼目录路径**：Claude Agent SDK（Claude Code CLI）
把会话 JSONL 存在 ``$HOME/.claude/projects/<按 cwd 编码的目录>/<session_id>.jsonl``，
目录名的编码规则是 SDK/CLI 内部行为、不属于本仓库的公开契约，且当前 Worker 尚未
实现按用户分离的 ``cwd``（``apps/worker/config.py`` 的 ``workspace`` 是一个进程级
静态值；per-user/per-conversation 工作目录隔离属 ``provisioning/``，代码框架已登记
「待建立」，架构设计 5.3 节描述的 uid 切换也尚未实现）。反过来依赖文件名——
``<session_id>.jsonl``——是更稳定的公开事实：会话文件名恒为该会话 id 加 ``.jsonl``
后缀，与目录编码规则无关。在一个有边界的根目录下按文件名精确匹配，删除动作因此
不依赖未经验证的目录编码假设，误删或漏删的面都收窄到"这个根目录下是否存在恰好
这个文件名"。

真实 Claude Code CLI 是否确实遵守这个文件命名约定，属未经 L4a 验证的假设（与
``adapters/feishu_delivery.py`` 声明未验证的卡片载荷形状同一类证据等级）；如果
Bot-Test/Stage 实测发现命名规则不同，只需要改这里的匹配规则，调用方（Worker 的
周期性收口）不用变。

## 删除前先归档（Issue #291 L6 取证结论，2026-08-22）

2026-08-22 那次 L6 真实取证：验收现场为了复现问题打了一个 ``/new``，触发的
``agent_session_cleanup`` 立刻把出事那次回合的会话 JSONL **物理删除**了——取证
现场被自己的清理机制毁掉，只能靠结构化日志的间接线索重建事实，而不是直接读
原始事件流。物理删除本身没有错（这是它的设计目的），错的是"删除"和"证据保全"
共用同一个动作、没有任何缓冲。

因此这里改成**删除前先归档**：命中的 JSONL 先被移动（不是复制）到
``<user_env_root>/_archive/<user_id>/<agent_session_id>.jsonl``，而不是直接
``unlink``。选 ``user_env_root`` 而不是新建一个目录，是因为它已经是"按
``user_id`` 分目录、由部署配置指定根路径"的既有结构（``adapters/user_mcp_
config.py`` 的 ``<user_env_root>/<user_id>/.mcp.json``）——复用它的每用户目录
边界，不必为归档另起一套目录权限判断。归档目录权限与用户目录同一收紧口径
（见 ``_ARCHIVE_DIR_MODE``），不做 ``user_environment.py`` 写侧那一整套
``O_NOFOLLOW``/dir_fd 硬化：那一层是为"进程正要往目录里写明文令牌"防符号链接
劫持设计的，这里搬的是已经落盘的会话转录文本，风险模型更窄，参照
``user_mcp_config.py`` 模块文档「为什么不复用」一节的同类取舍。

归档不是无限保留：单个用户的归档目录按数量与时间双重上限做保留清理（见
``_prune_archive``），防止长期运行后归档目录本身无界膨胀。

行为受环境变量 ``LINGXI_WORKER_SESSION_ARCHIVE_ENABLED`` 控制，**默认开启**
（取证价值优先于磁盘占用；显式设为 ``0``/``false``/``no``/``off`` 才关闭，
关闭后退回本 Story 之前的直接物理删除语义）。调用方没有提供
``user_env_root``/``user_id``（例如未来新增的调用点、或历史测试直接调用本函数
不传这两个新增关键字参数）时同样退回直接删除——没有目标目录就没有"归档"这件
事可言，不能假装做到了。
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Mapping

logger = logging.getLogger(__name__)

DEFAULT_SESSION_SUBDIR = (".claude", "projects")

#: 归档开关的环境变量名；未设置或设置为空值时默认**开启**（见模块文档「删除前
#: 先归档」）。
ARCHIVE_ENABLED_ENV_VAR = "LINGXI_WORKER_SESSION_ARCHIVE_ENABLED"
_ARCHIVE_DISABLED_VALUES = frozenset({"0", "false", "no", "off"})

#: 归档子目录名，挂在 ``user_env_root`` 根下（与 ``<user_env_root>/<user_id>``
#: 平级），不与任何用户的 ``.mcp.json`` 目录同名，因此不存在用户 id 恰好叫这个
#: 名字导致的碰撞（用户 id 由内部账号体系分配，不是用户可控输入）。
_ARCHIVE_SUBDIR_NAME = "_archive"
#: 与 ``adapters/user_environment.py`` 的 ``HOME_DIR_MODE`` 同一收紧口径——归档
#: 目录装的是同一批用户会话转录文本，权限边界不应该比用户目录本身更宽。
_ARCHIVE_DIR_MODE = 0o700
#: 保留上限：默认"最近 10 个或 7 天"（先到先裁，两个条件任一命中就裁剪），防止
#: 一个长期运行、清理频繁的用户把归档目录撑到无界大小。具体取值是实现决定，
#: 不是产品承诺的保留期限——这里只做"不无限增长"的下界防护。
_DEFAULT_ARCHIVE_RETENTION_COUNT = 10
_DEFAULT_ARCHIVE_RETENTION_DAYS = 7.0


def default_session_root(env: Mapping[str, str] | None = None) -> Path | None:
    """默认会话根目录：``$HOME/.claude/projects``。

    取不到 ``HOME`` 时返回 ``None``——调用方据此跳过物理清理并保持诚实的"没有配置
    可用根目录"状态，而不是拼出一个错误的相对路径去误删或静默什么都不做却假装
    已经处理。当前部署镜像固定 ``HOME=/tmp``（见 ``Dockerfile``），因此生产环境下
    这里恒能取到值；``None`` 分支只服务测试与未来可能的部署变化。
    """

    source = os.environ if env is None else env
    home = (source.get("HOME") or "").strip()
    if not home:
        return None
    return Path(home).joinpath(*DEFAULT_SESSION_SUBDIR)


def _archive_enabled(env: Mapping[str, str] | None) -> bool:
    """归档开关默认开启，见模块文档「删除前先归档」。"""

    source = os.environ if env is None else env
    raw = (source.get(ARCHIVE_ENABLED_ENV_VAR) or "").strip().lower()
    return raw not in _ARCHIVE_DISABLED_VALUES


def _resolve_archive_dir(
    *,
    user_env_root: str | Path | None,
    user_id: str | None,
    env: Mapping[str, str] | None,
) -> Path | None:
    """算出这个用户的归档目录；算不出或建不出时返回 ``None``，调用方据此退回
    直接物理删除（宁可丢证据保全、也不能让清理本身卡住不干活）。
    """

    if not user_env_root or not user_id:
        return None
    if not _archive_enabled(env):
        return None
    archive_dir = Path(user_env_root) / _ARCHIVE_SUBDIR_NAME / user_id
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_dir.chmod(_ARCHIVE_DIR_MODE)
    except OSError as error:
        logger.error(
            "会话归档目录不可用，本次退回直接物理删除 error=%s", type(error).__name__
        )
        return None
    return archive_dir


def _archive_one(match: Path, archive_dir: Path) -> None:
    """把一个已匹配的会话 JSONL 移动进归档目录，同名覆盖（同一会话 id 理论上
    不会在归档目录里撞出第二份不同内容——文件名即会话 id，内容由 Claude Agent
    SDK 一次性写定）。"""

    shutil.move(str(match), str(archive_dir / match.name))


def _prune_archive(archive_dir: Path, *, retention_count: int, retention_days: float) -> None:
    """按数量与时间双重上限裁剪归档目录，避免无界膨胀（见模块文档「删除前先
    归档」）。两个上限任一命中就裁剪该文件；``<= 0`` 表示不启用对应维度的上限。
    单条裁剪失败不影响其余条目，也不影响本轮清理本身的成败——这里已经是
    "保全动作之后的收尾"，不能反过来让收尾失败拖累主流程。
    """

    try:
        entries = [entry for entry in archive_dir.iterdir() if entry.is_file()]
    except OSError as error:
        logger.error("会话归档保留上限检查失败 error=%s", type(error).__name__)
        return
    if not entries:
        return

    try:
        entries.sort(key=lambda entry: entry.stat().st_mtime, reverse=True)
    except OSError as error:
        logger.error("会话归档保留上限检查失败 error=%s", type(error).__name__)
        return

    now = time.time()
    cutoff = now - retention_days * 86400 if retention_days > 0 else None
    for index, entry in enumerate(entries):
        expired_by_count = retention_count > 0 and index >= retention_count
        expired_by_age = False
        if cutoff is not None:
            try:
                expired_by_age = entry.stat().st_mtime < cutoff
            except OSError:
                continue
        if expired_by_count or expired_by_age:
            try:
                entry.unlink()
            except OSError as error:
                logger.error(
                    "会话归档保留上限裁剪失败 error=%s", type(error).__name__
                )


def delete_agent_session_files(
    session_root: Path,
    agent_session_id: str,
    *,
    user_env_root: str | Path | None = None,
    user_id: str | None = None,
    env: Mapping[str, str] | None = None,
    retention_count: int = _DEFAULT_ARCHIVE_RETENTION_COUNT,
    retention_days: float = _DEFAULT_ARCHIVE_RETENTION_DAYS,
) -> int:
    """在 ``session_root`` 下递归查找 ``<agent_session_id>.jsonl``，先归档、
    再（在归档不可用时）物理删除，见模块文档「删除前先归档」。

    幂等：根目录不存在或没有匹配文件都不是错误，返回 ``0``——上一轮已经处理过、
    这个会话其实从未真正落过盘（例如任务在建会话前就失败）都是正常情况，不能
    被当作物理清理失败。``agent_session_id`` 只接受不含路径分隔符的字符串，防止
    经数据库读回一个被篡改的值后意外跨目录匹配或通配。

    ``user_env_root``/``user_id`` 都不提供时（旧调用方、或环境变量关闭了归档）
    退回本 Story 之前的直接物理删除语义，行为与签名变更前完全一致。
    """

    if not agent_session_id or "/" in agent_session_id or "\\" in agent_session_id:
        raise ValueError(f"非法的 agent_session_id：{agent_session_id!r}")
    if not session_root.is_dir():
        return 0

    archive_dir = _resolve_archive_dir(user_env_root=user_env_root, user_id=user_id, env=env)

    handled = 0
    filename = f"{agent_session_id}.jsonl"
    for match in session_root.rglob(filename):
        try:
            if archive_dir is not None:
                _archive_one(match, archive_dir)
            else:
                match.unlink()
            handled += 1
        except FileNotFoundError:
            # 并发处理或已经不存在：目标状态已经达成，不算失败。
            continue
        except OSError as error:
            # 不记完整路径：目录编码可能携带 cwd 片段，只留长度与错误类型足够排障。
            logger.error(
                "Agent 会话 JSONL %s失败 path_length=%d error=%s",
                "归档" if archive_dir is not None else "物理删除",
                len(str(match)),
                type(error).__name__,
            )

    if archive_dir is not None and handled:
        _prune_archive(archive_dir, retention_count=retention_count, retention_days=retention_days)

    return handled
