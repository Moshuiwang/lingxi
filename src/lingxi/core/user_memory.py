"""用户记忆（Issue #357 S-H3-3，D1 显式登记范围）：数据形状与纯渲染逻辑。

只放不碰数据库、不做 I/O 的部分——真正的存取在 ``adapters/postgres_conversation``
（``/memory`` 命令面复用的 CRUD 四方法）与 ``adapters/postgres_user_memory``
（worker 侧只读拼装）。三类记忆的取值域、写入上限与 worker 注入的防御性字符上限
三条常量集中在这里，两侧调用方共用同一份事实，不各自维护一份可能漂移的副本。

**不存数据值**（产品合同边界）：这条红线不落在这个模块——结构层面无法区分「一句话
映射描述」与「用户手滑粘贴的查询结果文本」，真正的边界落在命令面的登记语法（只接受
``key => value`` 形状，见 ``core/conversation/commands.py``）与产品文案提示，见
迁移 ``0076_user_memory.py`` 头部同一说明。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, Sequence

#: D1 范围固定的三类记忆，与迁移 0076 的 CHECK 约束、``/memory remember`` 命令解析
#: （``core/conversation/commands.py``）三处共享同一份取值域。
MEMORY_TYPES: tuple[str, ...] = (
    "term_mapping",
    "calibration_preference",
    "convention_template",
)

#: 单用户记忆条数上限（issue 正文估算 <50 条 ≈ 2500 token 的依据）。超过直接拒绝，
#: 不静默截断——见 ``adapters/postgres_conversation/_transaction.py``
#: ``remember_user_memory`` 与 ``core/conversation/commands.py`` 的同名注释。
MAX_MEMORY_ENTRIES_PER_USER = 50

#: worker 注入提示词段落的防御性总字符上限（设计文 d 节）：写入侧已经用「≤50 条」
#: 卡住上限，这里是第二道防御——防止未来上限校验被绕过或写入路径出现 bug 时提示词
#: 无限扩张。超限时按 ``created_at`` 只取最近若干条，不是简单按字符截断（那样会截
#: 出半条记忆，格式不完整）。
DEFAULT_MAX_PROMPT_CHARS = 6000

_PROMPT_HEADER = (
    "## 已登记的用户记忆（用户可通过 /memory 查看、删除或清空；不代表当前查询结果）"
)


@dataclass(frozen=True)
class UserMemoryEntry:
    """一条已登记的用户记忆，adapters 层查询结果与本模块渲染函数共用的形状。"""

    memory_id: str
    memory_type: str
    memory_key: str
    memory_value: str
    created_at: datetime


class MemoryTypeLabels(Protocol):
    """三类记忆的展示标签来源——只需要能按类型取一个短标签字符串。

    真实调用方是 ``lingxi.config.content.ContentCatalog``（``memory.type_label.*``
    三个键，用户可见文案纪律要求它们进 content.toml，见该文件），这里只声明用得到
    的最小形状，不在 ``core/`` 里 import content 模块的具体类型，保持本模块除
    dataclass/枚举常量外零依赖。
    """

    def __call__(self, memory_type: str) -> str: ...


@dataclass(frozen=True)
class RenderedUserMemoryPrompt:
    """一次记忆拼装的结果，供 worker 注入点与其单测使用。"""

    text: str
    truncated: bool
    total_entries: int
    kept_entries: int


def render_user_memory_prompt(
    entries: Sequence[UserMemoryEntry],
    *,
    type_label: MemoryTypeLabels,
    max_chars: int = DEFAULT_MAX_PROMPT_CHARS,
) -> RenderedUserMemoryPrompt:
    """把一个用户的全部记忆拼成 worker 任务级系统提示词的附加段落（设计文 d 节格式）。

    纯函数：不查库、不做任何 I/O，调用方（``adapters/postgres_user_memory.py``）
    负责把真实查询结果传进来。空输入返回空文本——调用方据此判断是否需要拼接。

    截断策略：按 ``created_at`` 升序排列后，超出 ``max_chars`` 时从最旧的一条开始
    丢弃，直到剩余文本落在上限之内——保留的是「最近登记」的记忆，与产品直觉一致
    （越新登记的口径偏好/术语映射越可能仍然有效）。
    """

    if not entries:
        return RenderedUserMemoryPrompt(text="", truncated=False, total_entries=0, kept_entries=0)

    ordered = sorted(entries, key=lambda entry: entry.created_at)
    lines = [_format_entry_line(entry, type_label) for entry in ordered]

    kept = lines
    while len(kept) > 1 and len(_compose(kept)) > max_chars:
        kept = kept[1:]
    # 连最新一条都超限时仍然保留表头 + 这一条，不整体丢弃：宁可提示范围被截断，
    # 也不让"注入失败"悄悄退化成"没有任何记忆生效"。``truncated`` 在这种单条
    # 仍超限的情形下同样为真——调用方据此决定是否记一条结构化告警，这正是最该
    # 被告警的情形（单条记忆本身异常巨大）。
    text = _compose(kept)
    truncated = len(kept) < len(lines) or len(text) > max_chars
    return RenderedUserMemoryPrompt(
        text=text,
        truncated=truncated,
        total_entries=len(entries),
        kept_entries=len(kept),
    )


def _format_entry_line(entry: UserMemoryEntry, type_label: MemoryTypeLabels) -> str:
    label = type_label(entry.memory_type)
    registered_on = entry.created_at.date().isoformat()
    return f"- [{label}] {entry.memory_key} => {entry.memory_value}（登记于 {registered_on}）"


def _compose(lines: Sequence[str]) -> str:
    return "\n".join((_PROMPT_HEADER, *lines))
