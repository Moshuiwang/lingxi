"""话题命令解析：``/new`` 与 ``/stop``，以及「看起来像命令但不被认识」的斜杠输入。

只做「这条消息是不是命令」的判定，不决定命令的后果——后果由
[接口设计 3.2](../../../../docs/技术设计/接口设计.md) 的处理次序决定，写在 ``pipeline`` 里。

命令识别刻意保守：只认整条消息就是命令本身的情况。合同把 ``/stop`` 定义成「用户在当前
话题发送 ``/stop``」，没有给「消息里包含 /stop」这种形态任何承诺；把「帮我查一下 /stop
的用法」当成停止命令，会让用户的问题被静默吞掉。

``is_unrecognized_slash_message``：执行层底层 CLI 把「第一个字符是 /」的用户
文本解析成系统斜杠命令而不是普通问题——``/config``/``/model``/``/help`` 会让
会话瞬断，``/loop`` 会让模型尝试调用内部工具。gateway 管线用这个函数在入队前
把「不是我们认识的命令、但确实是 / 开头」的消息拦下来，直接回绝而不是原样
交给执行层，判断复用 ``parse_command`` 的整条匹配语义。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from lingxi.core.ids import is_ulid
from lingxi.core.user_memory import MEMORY_TYPES


class Command(str, Enum):
    """一条用户消息的命令归类。"""

    NEW = "/new"
    STOP = "/stop"
    NONE = ""


def parse_command(text: object) -> Command:
    """把消息正文解析成命令。

    只在**去掉首尾空白后整条等于**命令字面量时才判定为命令，大小写不敏感。
    非字符串（事件体缺字段、类型异常）一律当作普通消息，不抛异常——入站解析的
    健壮性属于 ``V-接入-12``：一条畸形消息不能把长连接带下去。
    """
    if not isinstance(text, str):
        return Command.NONE
    stripped = text.strip().lower()
    if stripped == Command.NEW.value:
        return Command.NEW
    if stripped == Command.STOP.value:
        return Command.STOP
    return Command.NONE


def is_unrecognized_slash_message(text: object) -> bool:
    """这条消息去掉首尾空白后是否以 ``/`` 开头，且既不是已知命令也不是 ``/memory`` 消息。

    只看去除首尾空白后的第一个字符——句子中间出现的 ``/``（日期「8/26」、URL）
    不受影响；非字符串输入一律返回 ``False``，不抛异常。``/memory`` 的豁免只看
    前缀，不要求真的能解析出已知子命令：格式写错的 ``/memory`` 消息应该得到
    专属的用法提示，而不是与完全无关的斜杠输入共用同一条泛用拒绝文案——两者
    的用户行动指引不同。
    """
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped.startswith("/"):
        return False
    if parse_command(stripped) is not Command.NONE:
        return False
    return not is_memory_command_message(stripped)


# ----------------------------------------------------------------------
# /memory 命令面：与上面 Command/parse_command 并列的第二个解析器——``/new``/
# ``/stop`` 是「整条消息逐字匹配」，``/memory remember <类型> <关键词> =>
# <说明>`` 天然带自由文本参数，parse_command 的写法不适用，需要一个新函数。
# ----------------------------------------------------------------------

_MEMORY_COMMAND_PREFIX = "/memory"
_MEMORY_ID_PREFIX = "mem_"
_MEMORY_SEPARATOR = "=>"
#: 命令层的长度上限——数据库只校验非空白（迁移 0076），这两个上限是命令解析
#: 自身加的一道从紧防御，防止一次输入把审计字段/提示词撑成不可读的长文，与
#: `core/admin/commands.py` 的 `_PERMISSION_REASON_MAX_LENGTH` 同一姿态。
_MEMORY_KEY_MAX_LENGTH = 200
_MEMORY_VALUE_MAX_LENGTH = 2000
#: 子命令/参数的词边界——只认水平空白，不用 ``str.split()`` 的默认空白语义
#: （那会把换行也当分隔符）。只在确认消息不含「换行 + 换行后非空内容」之后
#: 才会用到，见 :func:`_split_memory_subcommand` 文档。
_HORIZONTAL_WHITESPACE_RE = re.compile(r"[ \t]+")


class MemoryCommandKind(str, Enum):
    """``/memory`` 已知子命令的取值。"""

    LIST = "list"
    CLEAR = "clear"
    FORGET = "forget"
    REMEMBER = "remember"
    NONE = ""


@dataclass(frozen=True)
class MemoryCommand:
    """``/memory`` 的解析结果。

    ``NONE`` 表示这条消息不以 ``/memory`` 开头，或以 ``/memory`` 开头但子命令
    形状不对——区分它们是 :func:`is_memory_command_message` 的职责。
    ``memory_id``/``memory_serial`` 只在 ``kind is FORGET`` 时二选一非空：
    ``memory_serial`` 是短序号，``memory_id`` 是原始 ``mem_`` 前缀 id；序号到
    具体 id 的解析需要查一次记忆列表，属于 I/O，留给调用方在删除前解析。
    """

    kind: MemoryCommandKind
    memory_id: str | None = None
    memory_serial: int | None = None
    memory_type: str | None = None
    memory_key: str | None = None
    memory_value: str | None = None


def _memory_none() -> MemoryCommand:
    return MemoryCommand(kind=MemoryCommandKind.NONE)


def is_memory_command_message(text: object) -> bool:
    """这条消息去掉首尾空白后是否以 ``/memory``（大小写不敏感）开头，作为独立的词。

    即 ``/memory`` 本身，或 ``/memory`` 后面紧跟空白；只做前缀判定，不管后续
    token 是否能被 :func:`parse_memory_command` 解析成已知子命令——哪怕格式
    写错，这条消息的「命令归属」依然是 /memory 命令面。``/memoryabc`` 这类没有
    词边界的输入不算。词边界判定用 ``str.split()`` 的默认空白语义（空格、Tab、
    换行等任意空白游程），不是只认字面 ASCII 空格，取切出的第一个词与
    ``/memory`` 比较。
    """
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped:
        return False
    first_token = stripped.split(maxsplit=1)[0]
    return first_token.lower() == _MEMORY_COMMAND_PREFIX


def _split_memory_subcommand(stripped: str) -> tuple[str, str] | None:
    """从已确认属于 ``/memory`` 命令面的文本里切出子命令名与其余参数。

    多行粘贴消息（换行后还有非空内容）判定为不构成合法命令，返回 ``None``
    交调用方渲染用法提示，不猜测该按第一行还是全文解析——否则一次意外的换行
    巧合就能触发不可恢复的清空。真正参与切分的只有第一行，只认水平空白为
    分隔符；裸 ``/memory``（没有子命令）同样返回 ``None``。变异锚点：删掉
    换行判据，``NewlineInjectionGuardTest`` 一组用例会变红。
    """
    first_line, has_newline, after_newline = stripped.partition("\n")
    if has_newline and after_newline.strip():
        return None
    rest_raw = first_line[len(_MEMORY_COMMAND_PREFIX) :].strip(" \t")
    if not rest_raw:
        return None
    tokens = _HORIZONTAL_WHITESPACE_RE.split(rest_raw, maxsplit=1)
    sub = tokens[0].lower()
    remainder = tokens[1] if len(tokens) > 1 else ""
    return sub, remainder


def _dispatch_memory_subcommand(sub: str, remainder: str) -> MemoryCommand:
    """按已经切出的子命令名与其余参数构造对应的 :class:`MemoryCommand`。

    未知子命令、参数数量或形状不对，一律返回 ``NONE``——不猜测意图，与
    ``core/admin/commands.py`` 的既有纪律一致。
    """
    if sub == "list":
        if remainder.strip():
            return _memory_none()
        return MemoryCommand(kind=MemoryCommandKind.LIST)

    if sub == "clear":
        if remainder.strip():
            return _memory_none()
        return MemoryCommand(kind=MemoryCommandKind.CLEAR)

    if sub == "forget":
        token = remainder.strip()
        if not token or " " in token:
            return _memory_none()
        if _is_memory_id(token):
            return MemoryCommand(kind=MemoryCommandKind.FORGET, memory_id=token)
        serial = _parse_memory_serial(token)
        if serial is not None:
            return MemoryCommand(kind=MemoryCommandKind.FORGET, memory_serial=serial)
        return _memory_none()

    if sub == "remember":
        return _parse_remember(remainder)

    return _memory_none()


def parse_memory_command(text: object) -> MemoryCommand:
    """把一条私聊文本解析成 ``/memory`` 的四种已知子命令之一，或 ``NONE``。

    语法（token 大小写不敏感，``key``/``value`` 保留原始大小写）：
    ``/memory list``/``clear`` 无附加参数；``forget <序号或 memory_id>`` 参数
    二选一；``remember <memory_type> <key> => <value>``，``memory_type`` 须在
    :data:`~lingxi.core.user_memory.MEMORY_TYPES` 取值域内，``key``/``value``
    是 ``=>`` 两侧非空自由文本且不超长度上限——这是「不存数据值」红线在命令
    语法层面的落点。不匹配以上形状一律返回 ``NONE``。
    """
    if not isinstance(text, str):
        return _memory_none()
    stripped = text.strip()
    if not is_memory_command_message(stripped):
        return _memory_none()
    parsed = _split_memory_subcommand(stripped)
    if parsed is None:
        return _memory_none()
    sub, remainder = parsed
    return _dispatch_memory_subcommand(sub, remainder)


def _is_memory_id(token: str) -> bool:
    """判断是否 ``mem_`` 前缀 + 26 位 Crockford Base32 ULID。

    与 ``core/ids.new_id("mem")`` 的生成形状逐字对应，复用 ``core/ids.is_ulid``
    而不是自己重写一份大小写/字母表校验。
    """
    if not token.startswith(_MEMORY_ID_PREFIX):
        return False
    return is_ulid(token[len(_MEMORY_ID_PREFIX) :])


def _parse_memory_serial(token: str) -> int | None:
    """把 ``/memory forget`` 的参数解析成短序号，是 ``/memory list`` 展示序号的镜像入口。

    只接受不带符号、不带前导零的十进制正整数字面量——``01``、``+1``、``1.0``
    一律判 ``None``，是不让同一个字符串对应两种解析结果产生歧义。真正把序号
    换算成具体 ``memory_id`` 需要查一次记忆列表（I/O），留给调用方在删除前
    解析，见 :class:`MemoryCommand` 文档。
    """
    # 用 isdecimal() 而不是 isdigit()：上标数字 "²⁴" 让 isdigit() 为真、int()
    # 抛 ValueError——这条路径任何用户输入都能走到，不能靠上层兜底转成异常。
    if not token.isdecimal():
        return None
    if token != str(int(token)):
        return None
    value = int(token)
    return value if value >= 1 else None


def _parse_remember(remainder: str) -> MemoryCommand:
    r"""``remember`` 的解析：``<memory_type> <key> => <value>``。

    先按**水平空白**（``[ \t]+``，调用方已经保证 ``remainder`` 来自不含换行的
    第一行）切出第一个 token 作为 ``memory_type``，剩余部分必须包含 ``=>``
    （取第一次出现的位置切分，允许 ``value`` 本身包含形似箭头的文本）；两侧
    各自 ``strip()`` 后校验非空与长度上限。
    """
    type_tokens = _HORIZONTAL_WHITESPACE_RE.split(remainder, maxsplit=1)
    if len(type_tokens) < 2:
        return _memory_none()
    memory_type, rest = type_tokens[0].lower(), type_tokens[1]
    if memory_type not in MEMORY_TYPES:
        return _memory_none()
    if _MEMORY_SEPARATOR not in rest:
        return _memory_none()
    key_part, value_part = rest.split(_MEMORY_SEPARATOR, 1)
    key = key_part.strip()
    value = value_part.strip()
    if not key or not value:
        return _memory_none()
    if len(key) > _MEMORY_KEY_MAX_LENGTH or len(value) > _MEMORY_VALUE_MAX_LENGTH:
        return _memory_none()
    return MemoryCommand(
        kind=MemoryCommandKind.REMEMBER,
        memory_type=memory_type,
        memory_key=key,
        memory_value=value,
    )
