"""Agent 会话 JSONL 的物理清理（Issue #153）。

只做形状：给定一个会话根目录与 ``agent_session_id``，删除匹配的 JSONL 文件。真正
「这个 session id 该不该删」的判定住在数据库（``clear_agent_session`` /
``sweep_idle_conversations`` / ``clear_delivered_content_for_user`` 三处触发点各自
排队，三处各自的排队实现在 ``adapters/postgres_conversation/_transaction.py`` 的
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
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Mapping

logger = logging.getLogger(__name__)

DEFAULT_SESSION_SUBDIR = (".claude", "projects")


def default_session_root(env: Mapping[str, str] | None = None) -> Path | None:
    """默认会话根目录：``$HOME/.claude/projects``。

    取不到 ``HOME`` 时返回 ``None``——调用方据此跳过物理清理并保持诚实的"没有配置
    可用根目录"状态，而不是拼出一个错误的相对路径去误删或静默什么都不做却假装
    已经处理。当前部署镜像固定 ``HOME=/tmp``（见 ``Dockerfile``），因此生产环境下
    这里恒能取到值；``None`` 分支只服务测试与未来可能的部署变化。
    """

    import os

    source = os.environ if env is None else env
    home = (source.get("HOME") or "").strip()
    if not home:
        return None
    return Path(home).joinpath(*DEFAULT_SESSION_SUBDIR)


def delete_agent_session_files(session_root: Path, agent_session_id: str) -> int:
    """在 ``session_root`` 下递归查找并删除 ``<agent_session_id>.jsonl``。

    幂等：根目录不存在或没有匹配文件都不是错误，返回 ``0``——上一轮已经删过、
    这个会话其实从未真正落过盘（例如任务在建会话前就失败）都是正常情况，不能
    被当作物理清理失败。``agent_session_id`` 只接受不含路径分隔符的字符串，防止
    经数据库读回一个被篡改的值后意外跨目录匹配或通配。
    """

    if not agent_session_id or "/" in agent_session_id or "\\" in agent_session_id:
        raise ValueError(f"非法的 agent_session_id：{agent_session_id!r}")
    if not session_root.is_dir():
        return 0

    deleted = 0
    filename = f"{agent_session_id}.jsonl"
    for match in session_root.rglob(filename):
        try:
            match.unlink()
            deleted += 1
        except FileNotFoundError:
            # 并发删除或已经不存在：目标状态已经达成，不算失败。
            continue
        except OSError as error:
            # 不记完整路径：目录编码可能携带 cwd 片段，只留长度与错误类型足够排障。
            logger.error(
                "Agent 会话 JSONL 物理删除失败 path_length=%d error=%s",
                len(str(match)),
                type(error).__name__,
            )
    return deleted
