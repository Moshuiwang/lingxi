"""一次处理器内的每次外发都读取本代消费者最新的失权结果。"""

from contextlib import contextmanager
from contextvars import ContextVar

_guard = ContextVar("followup_effect_guard", default=None)


@contextmanager
def effect_guard(check):
    """按执行上下文隔离检查器，不能把上一项失权带到下一项。"""
    token = _guard.set(check)
    try:
        yield
    finally:
        _guard.reset(token)


def effect_allowed():
    """数据库租约检查之外，再尊重集中续租者已经发现的失败。"""
    check = _guard.get()
    return check is None or check()
