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


def is_unrecognized_slash_message(text: object) -> bool:
    """这条消息去掉首尾空白后是否以 ``/`` 开头，且不是 :func:`parse_command`
    认识的任何命令（``/new``/``/stop``，大小写不敏感）。

    只看**去除首尾空白后的第一个字符**——句子中间出现的 ``/``（日期「8/26」、
    URL）不受影响；非字符串输入与 ``parse_command`` 同样的健壮性约定，一律
    返回 ``False``，不抛异常。
    """

    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped.startswith("/"):
        return False
    return parse_command(stripped) is Command.NONE
