"""话题命令解析：``/new`` 与 ``/stop``。

只做「这条消息是不是命令」的判定，不决定命令的后果——后果由
[接口设计 3.2](../../../../docs/技术设计/接口设计.md) 的处理次序决定，写在 ``pipeline`` 里。

命令识别刻意保守：只认整条消息就是命令本身的情况。合同把 ``/stop`` 定义成「用户在当前
话题发送 ``/stop``」，没有给「消息里包含 /stop」这种形态任何承诺；把「帮我查一下 /stop
的用法」当成停止命令，会让用户的问题被静默吞掉。
"""

from __future__ import annotations

from enum import Enum


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
