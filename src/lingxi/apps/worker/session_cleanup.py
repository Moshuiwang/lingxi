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

## 容量回收：会话转录不能无界增长（Issue #494，2026-08-31）

上面那条路径是**按 id 定点清理**：只有 ``/new``、权限刷新、闲置话题清扫等触发点
往 ``agent_session_cleanup`` 排了队，对应的 JSONL 才会被处理。**正常问数流程一次
都不排队**——于是会话转录在容器里只增不减。rc22 收尾批 S-12 浸泡实测坐实了这个
形状：worker 容器的 ``HOME=/tmp`` 是一块 **256MB 内存盘**，接负载约 45 分钟后
``df -h /tmp`` 就是 ``256M 256M 0 100%``，``/tmp/.claude/projects`` 一家吃满全部
256MB（32 份转录、单份最大 15MB——重指标查询的工具回执很大）；清空后 33 分钟又
长回 190MB。这不是"稳态占用偏高"，是单调增长直到写满。

:func:`reclaim_session_transcripts` 补上常规回收路径：**按总字节预算、删最旧的**，
与定点清理各自独立（定点清理仍然先归档，语义不变）。三条设计取舍：

* **删除而不归档**。归档的目的地在持久卷上，一次容量回收要搬走的是"最旧的一批"
  而不是"刚出事的那一份"，把它们逐份复制到磁盘再按保留上限立刻裁掉，只是把内存
  盘的增长换成一次无谓的 I/O。真正需要保全的取证对象是**最近**发生的那次回合，
  而本函数恰恰**先删最旧、保留最新**——比 Issue #291 那次"``/new`` 顺手把刚出事的
  转录删了"的形状更安全，不是更危险。
* **保护"太新"的文件**。``min_age_seconds`` 之内被写过的转录一律跳过：正在跑的
  回合、以及用户刚刚聊过随时可能续用的会话不进删除集合。POSIX 上删掉一个仍被
  打开的文件不会让写入方报错（fd 还在），但会让下一次 ``resume`` 落空、用户丢掉
  会话内的上下文——那是用户可见的降级，不能为了腾空间随手换。
* **删不动就如实告警，不硬删**。跳过保护窗口后仍超预算，说明预算相对真实负载配
  小了（或者单份转录异常巨大），这时打一条 error 让人看见，而不是把保护窗口一
  路降到 0 去凑数——真正的兜底是健康检查（``lingxi.apps.healthcheck`` 的可用空间
  判定，同一个 Issue）会如实变红，不是这里偷偷删掉在用的会话。

删掉的转录**不是产品状态**：会话与消息的事实源是数据库，JSONL 只是 Agent SDK 用来
``resume`` 的本地缓存。丢一份的后果是这个会话续不上（``worker.session_resume_miss``
→ 退回全新会话），rc22 浸泡里这条降级分支已被实测走过 14 次、零任务失败。
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
#: 会话 id 的形状白名单（对抗审查 2026-09-02 W-3）。
#:
#: 这个值经 ``ResultMessage.session_id`` 直落数据库（``core/execution/
#: message_stream.py`` 只判"是不是非空字符串"），迁移 ``0057`` 的
#: ``agent_session_id`` 列也没有任何形状约束。从本模块看出去，它就是一个**外部
#: 来源的字符串**。此前只拒 ``/`` 与反斜杠，然后拿它去 ``rglob(f"{id}.jsonl")``
#: ——``*``、``?``、``[…]`` 全是 glob 元字符：``agent_session_id="*"`` 会把
#: ``session_root`` 下**所有**用户的转录一次匹配干净，再原样搬进
#: ``_archive/<发起这次清理的那个用户>/``。这不是"删多了"，是把 A 的问答原文
#: 搬进 B 的归档目录。
#:
#: 因此改成白名单——"拒绝已知坏字符"永远漏，"只接受已知好形状"不漏。
#:
#: **为什么不是更紧的 UUID 正则**：Claude Agent SDK 的会话 id 究竟是什么形状，
#: 本仓库**没有回源确认过**（venv 里有 SDK 包，但真实 CLI 输出未跑过；既有用例
#: 用的是 ULID 形状的假值）。把一条清理路径钉死在没验证过的上游形状上，代价是
#: "上游换了形状 → 清理直接抛异常 → 转录再也不被回收"。这条白名单取文件名安全
#: 字符集：排除全部 glob 元字符、路径分隔符与点（因而也排除 ``..``），足以关掉
#: W-3 的两条路径，同时对 UUID / ULID / 其它常见 id 形状都成立。要收紧到 UUID，
#: 先回源确认 SDK 的形状再改。
_AGENT_SESSION_ID_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
#: 与 ``adapters/user_environment.py`` / ``adapters/user_mcp_config.py`` 的
#: ``_USER_ID_PATTERN`` 同一形状（W-3）。归档目录是 ``<user_env_root>/_archive/
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
#: 阈值（``lingxi.apps.healthcheck`` 默认按总量 10% 判红 = 25.6MiB），因此"回收
#: 刚好跑完"与"健康检查判红"之间有整整一个数量级的间隔，不会互相打架。
DEFAULT_SESSION_DISK_BUDGET_BYTES = 128 * 1024 * 1024
#: 触发回收后要压到预算的百分之多少。留出低水位而不是"删到刚好等于预算"，是为了
#: 让回收按批次发生（实测负载下约每 5 分钟一次），而不是每一轮都在预算线上反复
#: 删一两个文件。
DEFAULT_SESSION_DISK_LOW_WATER_RATIO = 0.75
#: 保护窗口：最近这么多秒内被写过的转录不参与删除，见模块文档「容量回收」。
#: 300 秒覆盖实测重查询单条 113.8 秒的数倍，足以罩住任何一次在途回合。
DEFAULT_SESSION_RECLAIM_MIN_AGE_SECONDS = 300.0


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
    if not isinstance(user_id, str) or not _USER_ID_PATTERN.match(user_id):
        # W-3：拼路径之前先校验。返回 None（退回直接物理删除）而不是抛异常——
        # 本函数的既有语义就是"算不出归档目录就别归档"，一个畸形 user_id 不该
        # 让清理整个卡住不干活。归档目录建不出来时走的也是这一支。
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
                logger.error("会话归档保留上限裁剪失败 error=%s", type(error).__name__)


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

    if not isinstance(agent_session_id, str) or not _AGENT_SESSION_ID_PATTERN.match(
        agent_session_id
    ):
        # W-3：白名单而不是黑名单。原来只拒 `/` 与 `\`，`*`/`?`/`[…]` 一路放行到
        # `rglob()` 里当通配符用。不回显收到的值——它来自数据库，可能是攻击载荷。
        # **已知残余（登记，未关闭）**：能写数据库的人仍可把 agent_session_id 换成
        # 另一个用户的**真实**会话 id，把那一份转录移进自己的归档目录。转录树按
        # CLI cwd 的 slug 分目录、不按用户分目录（S-2b 把 cwd 固定成
        # /tmp/lingxi-workspace 之后全用户共用一个 slug），本模块拿不到任何
        # 「这份转录属于谁」的判据，关不掉。前提仍是数据库写权限。
        raise ValueError("非法的 agent_session_id：只接受文件名安全字符（不回显收到的值）")
    if not session_root.is_dir():
        return 0

    archive_dir = _resolve_archive_dir(user_env_root=user_env_root, user_id=user_id, env=env)

    handled = 0
    filename = f"{agent_session_id}.jsonl"
    # **精确文件名比对，不是 glob**（W-3）。上面的白名单已经让 `rglob` 拿不到
    # 任何元字符，这里再把匹配方式本身换掉：判据变成"名字逐字相等"，于是"这个函数
    # 会不会通配"不再取决于上游那条正则有没有被改坏，也不取决于 pathlib 将来对
    # glob 语法的解释。两道独立的防线，任一道单独成立都足够。
    for match in _files_named(session_root, filename):
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


def reclaim_session_transcripts(
    session_root: Path,
    *,
    budget_bytes: int = DEFAULT_SESSION_DISK_BUDGET_BYTES,
    low_water_ratio: float = DEFAULT_SESSION_DISK_LOW_WATER_RATIO,
    min_age_seconds: float = DEFAULT_SESSION_RECLAIM_MIN_AGE_SECONDS,
    now: Callable[[], float] = time.time,
) -> ReclaimOutcome:
    """把 ``session_root`` 下的会话转录总占用压回 ``budget_bytes`` 以内（Issue
    #494），**删最旧的、跳过最近 ``min_age_seconds`` 内被写过的**。

    这是与"按 id 定点清理"（:func:`delete_agent_session_files`）互相独立的常规
    回收路径：定点清理只在 ``/new``、权限刷新等触发点排队时才发生，正常问数流程
    一次都不排——没有这一条，转录就是单调增长直到把内存盘写满（模块文档「容量
    回收」有实测数据）。

    ``budget_bytes <= 0`` 表示**关闭回收**，直接返回一份空结果、不扫描目录：这是
    留给运维的显式逃生口（例如为了保全一整段取证现场），不是默认值。根目录不存在
    同样返回空结果——与定点清理一致，"没有目录"不是错误。

    单个文件删除失败只记类型、不中断本轮（同批其余文件仍要被回收）；``stat`` 失败
    的条目直接跳过，不猜它的大小，也不把它算进总量。
    """

    if budget_bytes <= 0:
        return _EMPTY_RECLAIM
    if not session_root.is_dir():
        return _EMPTY_RECLAIM

    entries: list[tuple[float, int, Path]] = []
    total = 0
    for match in session_root.rglob("*.jsonl"):
        try:
            info = match.stat()
        except OSError:
            # 竞态删除或不可读：不猜大小、不计入总量，也不当作失败——下一轮重扫。
            continue
        if not os.path.isfile(match):
            continue
        entries.append((info.st_mtime, info.st_size, match))
        total += info.st_size

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
    entries.sort(key=lambda item: item[0])  # 最旧的排在前面

    remaining = total
    freed = 0
    deleted = 0
    protected = 0
    for mtime, size, path in entries:
        if remaining <= target:
            break
        if mtime >= cutoff:
            # 在途回合或用户刚聊过的会话：删了会让 resume 落空、用户丢上下文。
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
        # 窗口去凑数——真正的兜底是健康检查按可用空间判红（同一个 Issue）。
        logger.error(
            "worker.session_transcripts_over_budget bytes_after=%d budget_bytes=%d "
            "files_protected=%d",
            outcome.bytes_after,
            outcome.budget_bytes,
            outcome.files_protected,
        )
    return outcome
