"""应用身份令牌的进程内缓存与按需续期。

已知代价（已知情并接受）：写入不绑定到某个具体授权人，需要把应用加为该 Base 的
协作者。本模块只回答"手上这份令牌还能不能用、该不该现在去换一次"，真正的 HTTP
调用由注入的 ``fetch`` 提供——``core/`` 不做网络 I/O。不落盘、不进日志/审计/异常。

与 ``RosterAccessTokenProvider`` 的关键差异：没有频率上界。花名册那条"每 UTC 日
至多消费一次"保护的是一次性、消费一次就换代的 ``refresh_token``；应用身份令牌
换取用的是静态、可重复使用的 ``app_id``/``app_secret``，不存在"消费掉就没了"的
风险，因此本模块不设频率账本，换取频率天然被令牌有效期（约两小时）托底。

进程内持有者复用 ``core.identity.access_token_supply.DerivedAccessTokenHolder``——
一个只回答"密文 + 过期时间 + 安全余量"、不含任何身份域业务规则的通用缓存，跨
``core.permission``/``core.identity`` 复用，不重新发明一个已经正确的组件。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from lingxi.core.identity.access_token_supply import DerivedAccessTokenHolder
from lingxi.core.identity.credentials import DerivedAccessToken
from lingxi.core.permission.table_access_token_supply import (
    PermissionTableAccessTokenUnavailableError,
)


class TenantAccessTokenSupply:
    """应用身份令牌的短期供给：``Callable[[], str]``。

    作为 ``PermissionTableAccessTokenProvider`` 的 ``fetch`` 参数使用——外层
    Provider 负责"未接线 vs 配了但拿不到"的审计外壳与失败关闭语义，本类只负责
    "这次调用要不要真的去发一次请求"：手上有新鲜的直接给，没有或已临期才调用
    注入的 ``fetch`` 换一份新的；换回来的令牌寿命未知或一到手就临期时按
    ``fetch_unavailable`` 失败关闭（防御性兜底，``fetch`` 是可替换的注入点，
    不能假设实现总是自觉）。``fetch`` 本身抛出的异常原样上抛，不在这里吞掉。
    """

    def __init__(
        self,
        *,
        fetch: Callable[[], DerivedAccessToken],
        holder: DerivedAccessTokenHolder | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """接线取令牌回调、可选持有者（默认新建）与可选的时钟替身（测试用）。"""
        if not callable(fetch):
            raise ValueError("fetch 必须是返回 DerivedAccessToken 的可调用对象")
        self._fetch = fetch
        self._holder = holder if holder is not None else DerivedAccessTokenHolder()
        self._clock = clock or (lambda: datetime.now(UTC))

    def __call__(self) -> str:
        """取一份可用的应用身份令牌；拿不到时抛 ``PermissionTableAccessTokenUnavailableError``。"""
        now = self._clock()
        token = self._holder.fresh(now=now)
        if token is not None:
            return token.reveal()

        derived = self._fetch()
        if not isinstance(derived, DerivedAccessToken):
            raise TypeError("fetch 必须返回 DerivedAccessToken，避免明文令牌以裸字符串流转")
        if not self._holder.store(derived, now=now):
            raise PermissionTableAccessTokenUnavailableError("fetch_unavailable")

        moment = self._clock()
        token = self._holder.fresh(now=moment)
        if token is None:  # pragma: no cover - store 成功后 fresh 理应立即成立，防御性分支
            raise PermissionTableAccessTokenUnavailableError("fetch_unavailable")
        return token.reveal()


__all__ = ["TenantAccessTokenSupply"]
