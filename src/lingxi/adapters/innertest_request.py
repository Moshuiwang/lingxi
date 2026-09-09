"""受限入口的绝对请求截止；逐语句使用剩余时间，不累计五秒等待。"""

import threading
import time
from contextlib import contextmanager

from lingxi.adapters.postgres import connect as postgres_connect
from lingxi.core.admin.innertest import InnertestError

_LOCAL = threading.local()


@contextmanager
def request_window():
    """一个请求共享五秒截止，业务事务不重新开始计时。"""
    _LOCAL.deadline = time.monotonic() + 5
    try:
        yield
    finally:
        _LOCAL.deadline = None


def remaining():
    """预算耗尽时拒绝下一条语句，不能无限延长短请求。"""
    deadline = getattr(_LOCAL, "deadline", None)
    if deadline is None:
        return 5.0
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise InnertestError("request_timeout")
    return seconds


@contextmanager
def connect(dsn):
    """底层仍只由统一 PostgreSQL 连接入口创建。"""
    with postgres_connect(dsn) as connection:
        yield RequestConnection(connection)


class RequestConnection:
    """保留 psycopg 事务语义，仅给执行语句附上剩余截止。"""

    def __init__(self, connection):
        """连接归统一连接池，包装器不单独持久保存。"""
        self.raw = connection

    def __getattr__(self, name):
        """事务与提交仍由调用方原端口控制。"""
        return getattr(self.raw, name)

    def cursor(self):
        """每个游标共享线程内同一个请求截止。"""
        return RequestCursor(self, self.raw.cursor())


class RequestCursor:
    """固定 SET LOCAL 值只来自剩余毫秒，不接受客户端 SQL。"""

    def __init__(self, connection, cursor):
        """Connection 属性让定位函数继续复用同一连接。"""
        self.connection, self.raw = connection, cursor

    def __getattr__(self, name):
        """读取结果保持底层游标语义。"""
        return getattr(self.raw, name)

    def __enter__(self):
        """游标随调用方事务作用域结束。"""
        self.raw.__enter__()
        return self

    def __exit__(self, *args):
        """异常仍向外传播并回滚原业务事务。"""
        return self.raw.__exit__(*args)

    def execute(self, query, params=None):
        """每条业务语句受剩余时间及原三秒上限双重限制。"""
        milliseconds = max(1, min(3000, int(remaining() * 1000)))
        self.raw.execute("SET LOCAL statement_timeout = '" + str(milliseconds) + "ms'")
        self.raw.execute(query, params)
        return self
