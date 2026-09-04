"""Agent 会话 JSONL 的物理清理与容量回收。

只做形状：给定会话根目录与 ``agent_session_id``，删除匹配的 JSONL 文件；
「该不该删」的判定住在数据库，排队实现见 ``postgres_conversation/_transaction.py``。
按**文件名**匹配而不是拼目录路径——SDK 的目录编码规则不是公开契约，文件名
后缀是更稳定的事实。

删除前先归档到 ``<user_env_root>/_archive/<user_id>/``（取证优先于磁盘
占用，可用 ``LINGXI_WORKER_SESSION_ARCHIVE_ENABLED`` 关闭；缺目标目录参数
时退回直接物理删除）；归档目录按数量与时间双重上限裁剪，防止无界膨胀。

:func:`reclaim_session_transcripts` 是独立的常规容量回收：正常问数流程从不
触发定点清理排队，转录会单调增长直至写满内存盘。按总字节预算删最旧的、
跳过最近写过的；删不动就如实告警。删除的转录不是产品状态，事实源在数据库。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

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

#: 会话 id 的形状白名单：只接受文件名安全字符集（见 :func:`_validate_agent_
#: session_id` 的完整推导，含一处已登记未关闭的残余风险）。
_AGENT_SESSION_ID_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
#: 与 ``adapters/user_environment.py`` / ``adapters/user_mcp_config.py`` 的
#: ``_USER_ID_PATTERN`` 同一形状。归档目录是 ``<user_env_root>/_archive/
#: <user_id>/``，``user_id`` 未经校验就拼进路径：``"../escape"`` 会让归档落到
#: ``<user_env_root>/escape/``——那是用户目录的同级，恰好是放 ``.mcp.json`` 的
#: 那一层。同样只接受白名单。
_USER_ID_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
#: 与 ``adapters/user_environment.py`` 的 ``HOME_DIR_MODE`` 同一收紧口径——归档
#: 目录装的是同一批用户会话转录文本，权限边界不应该比用户目录本身更宽。
_ARCHIVE_DIR_MODE = 0o700
#: 保留上限：默认"最近 10 个或 7 天"（先到先裁，两个条件任一命中就裁剪），防止
#: 一个长期运行、清理频繁的用户把归档目录撑到无界大小。具体取值是实现决定，
#: 不是产品承诺的保留期限——这里只做"不无限增长"的下界防护。
_DEFAULT_ARCHIVE_RETENTION_COUNT = 10
_DEFAULT_ARCHIVE_RETENTION_DAYS = 7.0

#: 会话转录容量回收的默认字节预算（见 :func:`reclaim_session_transcripts`）。
#: 128MiB 是按实测标定的：worker 容器 ``/tmp`` 是 256MB 内存盘，预算取一半，
#: 回收后（低水位 75% → 96MiB）仍留出 160MiB 空闲，远高于健康检查的可用空间
#: 阈值，因此"回收刚好跑完"与"健康检查判红"之间有整整一个数量级的间隔，不会
#: 互相打架。
DEFAULT_SESSION_DISK_BUDGET_BYTES = 128 * 1024 * 1024
#: 触发回收后要压到预算的百分之多少。留出低水位而不是"删到刚好等于预算"，是为了
#: 让回收按批次发生，而不是每一轮都在预算线上反复删一两个文件。
DEFAULT_SESSION_DISK_LOW_WATER_RATIO = 0.75
#: 保护窗口：最近这么多秒内被写过的转录不参与删除，见模块文档「容量回收」。
#: 300 秒覆盖实测重查询单条耗时的数倍，足以罩住任何一次在途回合。
DEFAULT_SESSION_RECLAIM_MIN_AGE_SECONDS = 300.0


def default_session_root(env: Mapping[str, str] | None = None) -> Path | None:
    """默认会话根目录：``$HOME/.claude/projects``。

    取不到 ``HOME`` 时返回 ``None``——调用方据此跳过物理清理并保持诚实的"没有配置
    可用根目录"状态，而不是拼出一个错误的相对路径去误删或静默什么都不做却假装
    已经处理。当前部署镜像固定 ``HOME=/tmp``，因此生产环境下这里恒能取到值；
    ``None`` 分支只服务测试与未来可能的部署变化。
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
    """算出这个用户的归档目录；算不出或建不出时返回 ``None``。

    调用方据此退回直接物理删除——宁可丢证据保全、也不能让清理本身卡住不干活。
    """
    if not user_env_root or not user_id:
        return None
    if not _archive_enabled(env):
        return None
    if not isinstance(user_id, str) or not _USER_ID_PATTERN.match(user_id):
        # 拼路径之前先校验。返回 None（退回直接物理删除）而不是抛异常——本函数
        # 的既有语义就是"算不出归档目录就别归档"，一个畸形 user_id 不该让清理
        # 整个卡住不干活。归档目录建不出来时走的也是这一支。
        logger.error("归档目录的 user_id 形状非法，本次退回直接物理删除")
        return None
    archive_dir = Path(user_env_root) / _ARCHIVE_SUBDIR_NAME / user_id
    # 双保险：正则已经排除了 `..`/`/`，这里再核对拼出来的路径确实落在归档根之内。
    # 路径拼接的正确性不该只由"上游那条正则没被改坏"担保。
    archive_root = (Path(user_env_root) / _ARCHIVE_SUBDIR_NAME).resolve()
    if archive_root not in archive_dir.resolve().parents:
        logger.error("归档目录逃出归档根，本次退回直接物理删除")
        return None
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_dir.chmod(_ARCHIVE_DIR_MODE)
    except OSError as error:
        logger.error("会话归档目录不可用，本次退回直接物理删除 error=%s", type(error).__name__)
        return None
    return archive_dir


def _files_named(root: Path, filename: str) -> list[Path]:
    """递归找出 ``root`` 下**文件名逐字等于** ``filename`` 的常规文件。

    刻意不用 ``rglob(filename)``：那条路把调用方传进来的字符串当 glob 模式解释。
    这里遍历目录树、逐个比较名字，没有任何模式语义。不跟随符号链接
    （``os.walk`` 默认 ``followlinks=False``），因此一条指向别处的链接目录不能
    把匹配范围拉出 ``root``。
    """
    found: list[Path] = []
    for current, _directories, files in os.walk(root):
        if filename in files:
            candidate = Path(current) / filename
            # 只处理常规文件：符号链接指向的目标可能在 root 之外，移动/删除它
            # 等于对 root 之外的东西动手。
            if candidate.is_file() and not candidate.is_symlink():
                found.append(candidate)
    return found


def _archive_one(match: Path, archive_dir: Path) -> None:
    """把一个已匹配的会话 JSONL 移动进归档目录，同名覆盖。

    同一会话 id 理论上不会在归档目录里撞出第二份不同内容——文件名即会话 id，
    内容由 Claude Agent SDK 一次性写定。
    """
    shutil.move(str(match), str(archive_dir / match.name))


def _prune_archive(archive_dir: Path, *, retention_count: int, retention_days: float) -> None:
    """按数量与时间双重上限裁剪归档目录，避免无界膨胀。

    两个上限任一命中就裁剪该文件；``<= 0`` 表示不启用对应维度的上限。单条
    裁剪失败不影响其余条目，也不影响本轮清理本身的成败——这里已经是"保全
    动作之后的收尾"，不能反过来让收尾失败拖累主流程。
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
                logger.error("会话归档保留上限裁剪失败 error=%s", type(error).__name__)


def _validate_agent_session_id(agent_session_id: str) -> None:
    """校验 ``agent_session_id`` 只含文件名安全字符；不合规直接抛错。

    白名单而不是黑名单：只拒已知坏字符永远漏得掉，只接受已知好形状才不漏。
    不回显收到的值——它来自数据库，可能是攻击载荷。**已知残余（登记，未
    关闭）**：能写数据库的人仍可换成另一个用户的真实会话 id，把那份转录
    移进自己的归档目录——转录树按 CLI cwd 的 slug 分目录、不按用户分目录，
    本模块拿不到「这份转录属于谁」的判据；前提仍是数据库写权限。
    """
    if not isinstance(agent_session_id, str) or not _AGENT_SESSION_ID_PATTERN.match(
        agent_session_id
    ):
        raise ValueError("非法的 agent_session_id：只接受文件名安全字符（不回显收到的值）")


def _archive_or_delete_matches(matches: list[Path], archive_dir: Path | None) -> int:
    """把匹配到的会话文件逐个归档或物理删除，返回成功处理的数量。

    单个文件失败不中断本批其余文件；并发场景下目标已不存在不算失败。
    """
    handled = 0
    for match in matches:
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
    return handled


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
    """在 ``session_root`` 下递归查找 ``<agent_session_id>.jsonl``。

    先归档、再（在归档不可用时）物理删除，见模块文档「删除前先归档」。幂等：
    根目录不存在或没有匹配文件都不是错误，返回 ``0``。``user_env_root``/
    ``user_id`` 都不提供时（旧调用方、或环境变量关闭了归档）退回直接物理
    删除语义，行为与本函数扩出归档参数之前完全一致。
    """
    _validate_agent_session_id(agent_session_id)
    if not session_root.is_dir():
        return 0

    archive_dir = _resolve_archive_dir(user_env_root=user_env_root, user_id=user_id, env=env)
    filename = f"{agent_session_id}.jsonl"
    # 精确文件名比对，不是 glob：判据是"名字逐字相等"，不依赖 pathlib 对 glob
    # 语法的解释，与 _validate_agent_session_id 的白名单是两道独立防线。
    handled = _archive_or_delete_matches(_files_named(session_root, filename), archive_dir)

    if archive_dir is not None and handled:
        _prune_archive(archive_dir, retention_count=retention_count, retention_days=retention_days)

    return handled


@dataclass(frozen=True)
class ReclaimOutcome:
    """一次容量回收的实际结果，供调用方记日志与测试断言。

    ``bytes_after`` 仍然大于 ``budget_bytes`` 时 ``over_budget`` 为真：这一轮已经
    把所有**可删**的都删了，剩下的全在保护窗口内。这是一个诚实的"删不动了"信号，
    调用方据此告警，而不是缩短保护窗口去凑数（见模块文档「容量回收」）。
    """

    files_seen: int
    bytes_before: int
    files_deleted: int
    bytes_freed: int
    bytes_after: int
    files_protected: int
    budget_bytes: int

    @property
    def over_budget(self) -> bool:
        """删完之后是否仍超预算——诚实的"删不动了"信号（见类文档）。"""
        return self.budget_bytes > 0 and self.bytes_after > self.budget_bytes


_EMPTY_RECLAIM = ReclaimOutcome(
    files_seen=0,
    bytes_before=0,
    files_deleted=0,
    bytes_freed=0,
    bytes_after=0,
    files_protected=0,
    budget_bytes=0,
)


def _scan_session_transcripts(session_root: Path) -> tuple[list[tuple[float, int, Path]], int]:
    """遍历 ``session_root`` 下全部 ``*.jsonl``，返回条目列表与总字节数。

    条目形状是 ``(mtime, size, path)``。``stat`` 失败的条目直接跳过，不猜
    大小、不计入总量——竞态删除或不可读都不算失败，下一轮重扫即可。
    """
    entries: list[tuple[float, int, Path]] = []
    total = 0
    for match in session_root.rglob("*.jsonl"):
        try:
            info = match.stat()
        except OSError:
            continue
        if not os.path.isfile(match):
            continue
        entries.append((info.st_mtime, info.st_size, match))
        total += info.st_size
    return entries, total


def _delete_oldest_until_target(
    entries: list[tuple[float, int, Path]], *, target: int, cutoff: float, total: int
) -> tuple[int, int, int, int]:
    """按最旧优先删到 ``target`` 以内，跳过 ``cutoff`` 之后写过的条目。

    返回 ``(files_deleted, bytes_freed, files_protected, bytes_after)``。
    保护"太新"的文件：在途回合、用户刚聊过随时可能续用的会话不进删除集合，
    POSIX 上删掉一个仍被打开的文件不会让写入方报错，但会让下一次 resume
    落空、用户丢掉会话内的上下文——那是用户可见的降级。
    """
    ordered = sorted(entries, key=lambda item: item[0])
    remaining = total
    freed = 0
    deleted = 0
    protected = 0
    for mtime, size, path in ordered:
        if remaining <= target:
            break
        if mtime >= cutoff:
            protected += 1
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            remaining -= size
            freed += size
            continue
        except OSError as error:
            # 不记完整路径：目录名里带 cwd 片段，只留错误类型足够排障。
            logger.error("会话转录容量回收删除失败 error=%s", type(error).__name__)
            continue
        remaining -= size
        freed += size
        deleted += 1
    return deleted, freed, protected, remaining


def reclaim_session_transcripts(
    session_root: Path,
    *,
    budget_bytes: int = DEFAULT_SESSION_DISK_BUDGET_BYTES,
    low_water_ratio: float = DEFAULT_SESSION_DISK_LOW_WATER_RATIO,
    min_age_seconds: float = DEFAULT_SESSION_RECLAIM_MIN_AGE_SECONDS,
    now: Callable[[], float] = time.time,
) -> ReclaimOutcome:
    """把 ``session_root`` 下的会话转录总占用压回 ``budget_bytes`` 以内。

    与"按 id 定点清理"（:func:`delete_agent_session_files`）互相独立：定点
    清理只在触发点排队时才发生，正常问数流程一次都不排（模块文档「容量
    回收」有实测数据）。``budget_bytes <= 0`` 表示关闭回收，直接返回空结果、
    不扫描目录；根目录不存在同样返回空结果。删除而不归档——容量回收要搬走
    的是"最旧的一批"而不是"刚出事的那一份"，逐份复制到持久卷再立刻裁掉只是
    把内存盘的增长换成一次无谓的 I/O。
    """
    if budget_bytes <= 0:
        return _EMPTY_RECLAIM
    if not session_root.is_dir():
        return _EMPTY_RECLAIM

    entries, total = _scan_session_transcripts(session_root)
    if total <= budget_bytes:
        return ReclaimOutcome(
            files_seen=len(entries),
            bytes_before=total,
            files_deleted=0,
            bytes_freed=0,
            bytes_after=total,
            files_protected=0,
            budget_bytes=budget_bytes,
        )

    # 低水位：一次回收到预算的 low_water_ratio，避免每一轮都贴着预算线反复删。
    target = int(budget_bytes * low_water_ratio)
    cutoff = now() - min_age_seconds
    deleted, freed, protected, remaining = _delete_oldest_until_target(
        entries, target=target, cutoff=cutoff, total=total
    )
    return ReclaimOutcome(
        files_seen=len(entries),
        bytes_before=total,
        files_deleted=deleted,
        bytes_freed=freed,
        bytes_after=remaining,
        files_protected=protected,
        budget_bytes=budget_bytes,
    )


def run_session_transcript_reclaim(
    session_root: Path,
    *,
    budget_bytes: int,
    low_water_ratio: float,
    min_age_seconds: float,
) -> ReclaimOutcome | None:
    """跑一次容量回收并把结果记成结构化日志；失败返回 ``None`` 且不抛。

    与 :func:`reclaim_session_transcripts` 分开，是为了让调用方（``WorkerService``
    的每轮收口）只保留"要不要现在跑"这一件事：回收是收口顺带做的维护动作，
    **任何失败都不能带走任务职责**——与心跳、告警 tick 同一姿态。
    """
    try:
        outcome = reclaim_session_transcripts(
            session_root,
            budget_bytes=budget_bytes,
            low_water_ratio=low_water_ratio,
            min_age_seconds=min_age_seconds,
        )
    except Exception as error:  # 维护动作失败不能带走任务职责
        logger.error("会话转录容量回收失败 error=%s", type(error).__name__)
        return None
    if outcome.files_deleted:
        logger.warning(
            "worker.session_transcripts_reclaimed files_deleted=%d bytes_freed=%d "
            "bytes_before=%d bytes_after=%d budget_bytes=%d files_protected=%d",
            outcome.files_deleted,
            outcome.bytes_freed,
            outcome.bytes_before,
            outcome.bytes_after,
            outcome.budget_bytes,
            outcome.files_protected,
        )
    if outcome.over_budget:
        # 删不动了：能删的都删了，剩下的全在保护窗口内。如实告警而不是缩短保护
        # 窗口去凑数——真正的兜底是健康检查按可用空间判定。
        logger.error(
            "worker.session_transcripts_over_budget bytes_after=%d budget_bytes=%d "
            "files_protected=%d",
            outcome.bytes_after,
            outcome.budget_bytes,
            outcome.files_protected,
        )
    return outcome
