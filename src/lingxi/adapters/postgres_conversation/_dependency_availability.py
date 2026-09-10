"""判定一个异常是不是「外部依赖暂时不可用」，而不是编码缺陷。

常驻消费循环（``apps/worker/service.py``）需要在这两类异常之间做出取舍：前者
应当降级重试，后者必须原样上抛并让进程退出——裸 ``except Exception`` 会把两者
混为一谈，把编码缺陷也悄悄吞掉，因此判定单独收口成一个具名函数，不散落在
调用方各处各写一份。

**本模块不导入数据库驱动**：仓库纪律是驱动只能由 ``lingxi.adapters.postgres``
的连接入口延迟导入，正式代码其余地方一律不碰（``scripts/ci/check_db_timeouts.py``
钉住这一条）。因此这里改为**按异常自身的类型信息判定**——沿继承链找模块归属为
``psycopg`` 且名为 ``OperationalError`` 的类——不需要驱动在场，装没装都一样。
再退一步用标准库 ``OSError`` 兜底，覆盖连接失败与超时这类真实故障。
"""

from __future__ import annotations

_DRIVER_ROOT = "psycopg"
_DRIVER_UNAVAILABLE_ERROR = "OperationalError"


def is_dependency_unavailable(error: BaseException) -> bool:
    """``error`` 是不是数据库这类外部依赖暂时连不上，而不是代码本身的缺陷。

    调用方据此决定要不要把异常降级成「这一轮没有观察到任务」继续跑，还是原样
    上抛让进程退出——后者是唯一给编码缺陷保留的出口，不能被这里的判定误吞。
    """
    for klass in type(error).__mro__:
        module_root = (klass.__module__ or "").split(".", 1)[0]
        if module_root == _DRIVER_ROOT and klass.__name__ == _DRIVER_UNAVAILABLE_ERROR:
            return True
    return isinstance(error, OSError)
