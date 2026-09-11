"""判定一个异常是不是「外部依赖暂时不可用」，而不是编码缺陷。

常驻消费循环需要在这两类之间取舍：前者降级重试，后者必须原样上抛让进程退出。
裸 ``except Exception`` 会把两者混为一谈，把编码缺陷也悄悄吞掉。

**不导入数据库驱动**：驱动只能由 ``lingxi.adapters.postgres`` 的连接入口延迟
导入（``scripts/ci/check_db_timeouts.py`` 钉住）。因此按异常自身的类型信息判定。

**兜底只收「连不上」这一类，不是整个 ``OSError``**：``PermissionError`` 等表示
本地配置或代码配错的异常若判成依赖抖动，就会每轮记一条降级日志、永远转下去，
真正要修的缺陷从此不再暴露。判宽的代价比判窄大得多——判窄只是多退出一次进程。
"""

from __future__ import annotations

import socket
import ssl

_DRIVER_ROOT = "psycopg"
_DRIVER_UNAVAILABLE_ERROR = "OperationalError"

#: 标准库里真正表示「连不上 / 连上了又断了 / 等超时」的那几类，全部是 ``OSError``
#: 的子类。``ConnectionError`` 一族含 ``ConnectionRefusedError`` /
#: ``ConnectionResetError`` / ``BrokenPipeError``；``socket.gaierror`` 是域名解析
#: 失败；``ssl.SSLError`` 是握手阶段失败。刻意不含 ``PermissionError`` 等表示
#: 「本地环境配错了」的那几类——那不是依赖抖动，是要人去修的缺陷。
_UNAVAILABLE_STDLIB_ERRORS: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    socket.gaierror,
    socket.herror,
    ssl.SSLError,
)


def is_dependency_unavailable(error: BaseException) -> bool:
    """``error`` 是不是数据库这类外部依赖暂时连不上，而不是代码本身的缺陷。

    调用方据此决定要不要把异常降级成「这一轮没有观察到任务」继续跑，还是原样
    上抛让进程退出——后者是唯一给编码缺陷保留的出口，不能被这里的判定误吞。
    """
    for klass in type(error).__mro__:
        module_root = (klass.__module__ or "").split(".", 1)[0]
        if module_root == _DRIVER_ROOT and klass.__name__ == _DRIVER_UNAVAILABLE_ERROR:
            return True
    return isinstance(error, _UNAVAILABLE_STDLIB_ERRORS)
