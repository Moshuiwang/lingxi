"""判定一个异常是不是「外部依赖暂时不可用」，而不是编码缺陷。

常驻消费循环（``apps/worker/service.py``）需要在这两类异常之间做出取舍：前者
应当降级重试，后者必须原样上抛并让进程退出——裸 ``except Exception`` 会把两者
混为一谈，把编码缺陷也悄悄吞掉，因此判定单独收口成一个具名函数，不散落在
调用方各处各写一份。

**本模块不导入数据库驱动**：仓库纪律是驱动只能由 ``lingxi.adapters.postgres``
的连接入口延迟导入，正式代码其余地方一律不碰（``scripts/ci/check_db_timeouts.py``
钉住这一条）。因此这里按**异常自身的类型信息**判定——沿继承链找模块归属为
``psycopg`` 且名为 ``OperationalError`` 的类——不需要驱动在场。

**兜底刻意收窄到「连不上」这一类，不是整个 ``OSError``。** 独立内审与外审各自
独立报出同一条：整个 ``OSError`` 太宽，``PermissionError`` / ``FileNotFoundError``
/ ``IsADirectoryError`` 这些**本地配置或代码缺陷**会被判成「数据库抖了一下」，
于是每轮记一条降级日志、永远转下去——证书路径写错、权限配错这类问题从「响亮
崩溃」变成「静默重试」，永远不会有人发现。判宽的代价比判窄大得多：判窄了只是
多退出一次进程，判宽了是真 bug 永远不暴露。
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
