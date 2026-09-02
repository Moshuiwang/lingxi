"""话题命令解析：``/new`` 与 ``/stop``，以及「看起来像命令但不被认识」的斜杠输入。

只做「这条消息是不是命令」的判定，不决定命令的后果——后果由
[接口设计 3.2](../../../../docs/技术设计/接口设计.md) 的处理次序决定，写在 ``pipeline`` 里。

命令识别刻意保守：只认整条消息就是命令本身的情况。合同把 ``/stop`` 定义成「用户在当前
话题发送 ``/stop``」，没有给「消息里包含 /stop」这种形态任何承诺；把「帮我查一下 /stop
的用法」当成停止命令，会让用户的问题被静默吞掉。

``is_unrecognized_slash_message``（Trace #304 批次 5 直修，产品负责人 biai-stage 真实
测试暴露）：执行层（Agent SDK 底层的 Claude Code CLI）把「第一个字符是 /」的用户文本
解析成系统斜杠命令而不是普通问题——``/config``/``/model``/``/help`` 会让会话瞬断，
``/loop`` 会让模型尝试调用内部工具。gateway 管线用这个函数在入队前把「不是我们认识的
命令、但确实是 / 开头」的消息拦下来，直接回绝而不是原样交给执行层。判断复用
``parse_command`` 的整条匹配语义，保证「什么算命令」只有一处定义。
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
    """这条消息去掉首尾空白后是否以 ``/`` 开头，且既不是 :func:`parse_command`
    认识的任何命令（``/new``/``/stop``，大小写不敏感），也不是 ``/memory`` 命令面
    的消息（:func:`is_memory_command_message`）。

    只看**去除首尾空白后的第一个字符**——句子中间出现的 ``/``（日期「8/26」、
    URL）不受影响；非字符串输入与 ``parse_command`` 同样的健壮性约定，一律
    返回 ``False``，不抛异常。

    ``/memory`` 的豁免只看**前缀**，不要求 :func:`parse_memory_command` 真的能
    解析出已知子命令：格式写错的 ``/memory`` 消息（子命令拼错、``remember`` 少了
    ``=>``）应该得到 ``/memory`` 专属的用法提示（``memory.usage_help``），而不是
    与 ``/config``/``/model`` 这类完全无关的斜杠输入共用同一条泛用拒绝文案——两者
    的用户行动指引不同（前者该改写命令语法，后者该去掉开头的斜杠）。
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
# /memory 命令面（Issue #357 S-H3-3，D1 显式登记范围）
# ----------------------------------------------------------------------
#
# 与上面 Command/parse_command 并列的第二个解析器：``/new``/``/stop`` 是「整条
# 消息逐字匹配」，``/memory remember <类型> <关键词> => <说明>`` 天然带自由文本
# 参数，parse_command 的写法不适用，需要一个新函数而不是塞进它（设计文 b 节）。

_MEMORY_COMMAND_PREFIX = "/memory"
_MEMORY_ID_PREFIX = "mem_"
_MEMORY_SEPARATOR = "=>"
#: 命令层的长度上限——数据库只校验非空白（迁移 0076），这两个上限是命令解析
#: 自身加的一道从紧防御，防止一次输入把审计字段/提示词撑成不可读的长文，与
#: `core/admin/commands.py` 的 `_PERMISSION_REASON_MAX_LENGTH` 同一姿态。
_MEMORY_KEY_MAX_LENGTH = 200
_MEMORY_VALUE_MAX_LENGTH = 2000
#: 子命令/参数的词边界——**只认水平空白**（Trace #373 H3 批 codex 外审②
#: 修复②），不用 ``str.split()`` 的默认空白语义（那会把换行也当分隔符）。只
#: 在确认消息不含「换行 + 换行后非空内容」之后才会用到，见
#: :func:`parse_memory_command` 文档。
_HORIZONTAL_WHITESPACE_RE = re.compile(r"[ \t]+")


class MemoryCommandKind(str, Enum):
    LIST = "list"
    CLEAR = "clear"
    FORGET = "forget"
    REMEMBER = "remember"
    NONE = ""


@dataclass(frozen=True)
class MemoryCommand:
    """``/memory`` 的解析结果。``NONE`` 表示这条消息不以 ``/memory`` 开头，或者
    以 ``/memory`` 开头但子命令形状不对——两种情况调用方都不应该当作已知命令处理，
    区分它们与「这条消息该不该被当成 /memory 命令面」是 :func:`is_memory_command_message`
    的职责，不是本类型的职责。

    ``memory_id``/``memory_serial`` 只在 ``kind is FORGET`` 时二选一非空
    （rc22 B-8-1，#439 TOP-10）：``memory_serial`` 是 ``/memory list`` 展示的
    短序号（1、2、3……），``memory_id`` 是原始 ``mem_`` 前缀 id（向后兼容旧
    习惯与已经复制在别处的 id）。序号到具体 ``mem_`` id 的解析需要查一次当前
    用户的记忆列表，属于 I/O，因此不在本模块（纯解析、无副作用）完成，留给
    调用方（``core/conversation/pipeline.py`` 的 ``_resolve_forget_target_id``）
    在真正执行删除前解析。"""

    kind: MemoryCommandKind
    memory_id: str | None = None
    memory_serial: int | None = None
    memory_type: str | None = None
    memory_key: str | None = None
    memory_value: str | None = None


def _memory_none() -> MemoryCommand:
    return MemoryCommand(kind=MemoryCommandKind.NONE)


def is_memory_command_message(text: object) -> bool:
    """这条消息去掉首尾空白后是否以 ``/memory``（大小写不敏感）开头，作为独立的
    词——即 ``/memory`` 本身，或 ``/memory`` 后面紧跟空白。

    只做前缀判定，不管后续 token 是否能被 :func:`parse_memory_command` 解析成
    已知子命令：哪怕格式写错，这条消息的「命令归属」依然是 /memory 命令面，见
    :func:`is_unrecognized_slash_message` 的文档。``/memoryabc`` 这类没有词边界
    的输入不算——它不是 /memory 命令，是一条恰好以这几个字符开头的普通消息。

    **词边界判定用 ``str.split()`` 的默认空白语义**（Trace #373 H3 批量审查
    P2-4），不是只认字面 ASCII 空格：此前用 ``startswith(prefix + " ")`` 只认
    半角空格这一个字符，``/memory\\tlist``（Tab 分隔）、``/memory\\nlist`` 这类
    输入的第一个词其实就是 ``/memory``，却因为紧跟的不是半角空格而判定失败，
    落到通用的 ``slash_rejected`` 泛用拒绝文案，而不是本函数注释声明的
    ``/memory`` 专属用法提示——与设计意图相反（这不是安全绕过：两条路径都是
    拒绝，只是拒绝文案不对）。``str.split(maxsplit=1)`` 不传分隔符时按任意空白
    游程切分（空格、Tab、换行等），取切出的第一个词与 ``/memory`` 比较，天然
    覆盖这些形状，且不改变现有语义（多个连续空格、大小写不敏感、``/memoryabc``
    无词边界不算，都不受影响）。
    """

    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped:
        return False
    first_token = stripped.split(maxsplit=1)[0]
    return first_token.lower() == _MEMORY_COMMAND_PREFIX


def parse_memory_command(text: object) -> MemoryCommand:
    """把一条私聊文本解析成 ``/memory`` 的四种已知子命令之一，或 ``NONE``。

    语法（子命令与类型 token 大小写不敏感；``key``/``value`` 保留原始大小写）：

    - ``/memory list``                                     → LIST，无附加参数
    - ``/memory clear``                                    → CLEAR，无附加参数
    - ``/memory forget <序号或 memory_id>``                  → FORGET，参数二选一
      （rc22 B-8-1，#439 TOP-10）：不带前导零、不带符号的十进制正整数解析成
      ``memory_serial``（对应 ``/memory list`` 展示的短序号）；否则必须是
      ``mem_`` 前缀 + 26 位 Crockford ULID（与 ``core/ids.new_id("mem")`` 的
      生成形状逐字对应）才解析成 ``memory_id``——两种形状都不匹配时落 ``NONE``
    - ``/memory remember <memory_type> <key> => <value>``   → REMEMBER，
      ``memory_type`` 必须精确等于 :data:`~lingxi.core.user_memory.MEMORY_TYPES`
      三值之一；``<key>`` 与 ``<value>`` 是 ``=>`` 两侧的自由文本，各自去除首尾
      空白后必须非空、且不超过长度上限——这是「不存数据值」红线在命令语法层面
      的落点（设计文 b/e 节）：只接受 ``key => value`` 这种键值对形状，不接受
      任意自由文本直接登记。

    **只对首行做 token 化，水平空白 ``[ \\t]+`` 分隔**（Trace #373 H3 批 codex
    外审②修复②）：上一修复包把词边界判定改成 ``str.split()`` 的默认空白语义
    （空格、Tab、换行都算分隔符），本意只是让 ``/memory\\tlist`` 这类 Tab 分隔
    输入落进 /memory 命令面（见 :func:`is_memory_command_message` 文档），但
    连带让 ``/memory\\nclear`` 这种「消息第一行恰好是 ``/memory``、第二行恰好
    是 ``clear``」的多行粘贴消息也被当成合法的 ``clear`` 命令执行——多行粘贴
    是真实存在的输入形态（用户在别处复制一段文本贴进来），子命令/参数的词边界
    不该跨行识别，否则一次意外的换行巧合就能触发不可恢复的清空。因此本函数在
    确认 ``is_memory_command_message`` 之后，**消息里若存在换行且换行之后还有
    非空内容，直接判定为不构成合法 /memory 命令**（不猜测该按第一行还是全文
    解析），落 ``NONE`` 交调用方渲染用法提示；此时消息仍然属于 /memory 命令面
    （不会被判成完全无关的斜杠输入），只是子命令解析失败。真正参与子命令/参数
    切分的只有第一行，且只认水平空白（空格、Tab）为分隔符，不再用
    ``str.split()`` 默认语义——``/memory\\tlist``（整条消息只有一行）不受影响，
    仍然识别为 LIST（保留上一修复包的意图）。

    任何不匹配以上形状的输入（非字符串、空文本、未知子命令、参数数量或形状不对、
    ``memory_type`` 不在取值域内、``remember`` 缺少 ``=>``、``key``/``value`` 为空
    或越界）一律返回 ``NONE``——调用方据此渲染用法提示，不猜测意图，与
    ``core/admin/commands.py`` 的既有纪律一致。

    变异锚点：把下面「换行且换行后有非空内容 → 直接 ``NONE``」这段判据删掉，
    ``NewlineInjectionGuardTest`` 一组用例会从抛出 ``NONE``/零删除变红成
    ``/memory\\nclear`` 被当作合法 ``clear`` 执行。
    """

    if not isinstance(text, str):
        return _memory_none()
    stripped = text.strip()
    if not is_memory_command_message(stripped):
        return _memory_none()

    first_line, has_newline, after_newline = stripped.partition("\n")
    if has_newline and after_newline.strip():
        # 换行后还有非空内容：不是合法的单行 /memory 命令，形状不对——落
        # usage_help 路径，不猜测该按第一行还是全文解析。
        return _memory_none()

    rest_raw = first_line[len(_MEMORY_COMMAND_PREFIX) :]
    rest_raw = rest_raw.strip(" \t")
    if not rest_raw:
        # 裸 "/memory"：没有子命令，形状不对。
        return _memory_none()
    tokens = _HORIZONTAL_WHITESPACE_RE.split(rest_raw, maxsplit=1)
    sub = tokens[0].lower()
    remainder = tokens[1] if len(tokens) > 1 else ""

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


def _is_memory_id(token: str) -> bool:
    """``mem_`` 前缀 + 26 位 Crockford Base32 ULID——与 ``core/ids.new_id("mem")``
    的生成形状逐字对应，复用 ``core/ids.is_ulid`` 而不是自己重写一份大小写/
    字母表校验（全仓库唯一一份 ULID 实现，见该模块文档），与
    ``core/admin/commands.py`` 的 ``_is_override_id`` 同一手法。"""

    if not token.startswith(_MEMORY_ID_PREFIX):
        return False
    return is_ulid(token[len(_MEMORY_ID_PREFIX) :])


def _parse_memory_serial(token: str) -> int | None:
    """把 ``/memory forget`` 的参数解析成短序号（rc22 B-8-1，#439 TOP-10）：
    ``/memory list`` 把裸 ``mem_`` ULID 换成了 1、2、3…… 这种对普通用户友好的
    展示序号（``pipeline._render_memory_list``），这里是它在 forget 侧的镜像
    入口。

    只接受**不带符号、不带前导零**的十进制正整数字面量——``01``、``+1``、
    ``1.0`` 一律判 ``None``，不是"这不是数字"，是不让同一个字符串对应两种
    解析结果产生歧义（``01`` 到底是序号 1 还是格式错误）。真正把序号换算成
    具体 ``memory_id`` 需要查一次当前用户的记忆列表（I/O），留给调用方在
    真正执行删除前解析，见 :class:`MemoryCommand` 文档。
    """

    # ``isdecimal()`` 而不是 ``isdigit()``（Trace #544 A-2 同类）：上标数字
    # ``"²⁴"`` 让 ``isdigit()`` 为真、``int()`` 抛 ``ValueError``——这条路径任何
    # 用户发一句 ``/memory forget ²⁴`` 都能走到，不能靠上层兜底把它变成异常。
    if not token.isdecimal():
        return None
    if token != str(int(token)):
        return None
    value = int(token)
    return value if value >= 1 else None


def _parse_remember(remainder: str) -> MemoryCommand:
    """``remember`` 的解析：``<memory_type> <key> => <value>``。

    先按**水平空白**（``[ \\t]+``，同 :func:`parse_memory_command` 文档「只对
    首行做 token 化」的口径——调用方已经保证 ``remainder`` 来自不含换行的第一
    行）切出第一个 token 作为 ``memory_type``，剩余部分必须包含 ``=>``（取
    **第一次**出现的位置切分，允许 ``value`` 本身包含形似箭头的文本）；两侧各自
    ``strip()`` 后校验非空与长度上限。"""

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
