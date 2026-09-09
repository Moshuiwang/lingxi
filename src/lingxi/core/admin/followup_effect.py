"""一次处理器内的每次外发都读取本代消费者最新的失权结果。"""

from contextlib import contextmanager
from contextvars import ContextVar

_guard = ContextVar("followup_effect_guard", default=None)
_lease_guard = ContextVar("followup_lease_guard", default=None)


@contextmanager
def effect_guard(check, lease_check=None):
    """按执行上下文隔离检查器，不能把上一项失权带到下一项。"""
    token = _guard.set(check)
    lease_token = _lease_guard.set(lease_check)
    try:
        yield
    finally:
        _lease_guard.reset(lease_token)
        _guard.reset(token)


def effect_allowed():
    """数据库租约检查之外，再尊重集中续租者已经发现的失败。"""
    check = _guard.get()
    return check is None or check()


def effect_lease_allowed():
    """实际外发前复查数据库当前代数，再读续租者最新的失权结果。"""
    lease_check = _lease_guard.get()
    return effect_allowed() and (lease_check is None or lease_check()) and effect_allowed()
