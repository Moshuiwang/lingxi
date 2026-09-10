"""判定一个异常是不是"外部依赖暂时不可用"，而不是编码缺陷。

常驻消费循环（``apps/worker/service.py``）需要在这两类异常之间做出取舍：前者
应当降级重试，后者必须原样上抛并让进程退出——裸 ``except Exception`` 会把两者
混为一谈，把编码缺陷也悄悄吞掉，因此判定单独收口成一个具名函数，不散落在
调用方各处各写一份。

``psycopg`` 是本模块唯一关心的驱动，按仓库既有纪律延迟导入（模块顶层不能
import 第三方 SDK）：本机没装这个驱动时——大多数单测环境正是如此——退回按
标准库 ``OSError`` 判断，覆盖面比精确匹配窄，但足以覆盖连接失败/超时这类真实
故障，且不需要为了跑单测而装数据库驱动。
"""

from __future__ import annotations


def is_dependency_unavailable(error: BaseException) -> bool:
    """``error`` 是不是数据库这类外部依赖暂时连不上，而不是代码本身的缺陷。

    调用方据此决定要不要把异常降级成"这一轮没有观察到任务"继续跑，还是原样
    上抛让进程退出——后者是唯一给编码缺陷保留的出口，不能被这里的判定误吞。
    """
    try:
        import psycopg
    except ModuleNotFoundError:
        return isinstance(error, OSError)
    return isinstance(error, psycopg.OperationalError)
