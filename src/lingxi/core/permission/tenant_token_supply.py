"""应用身份令牌的进程内缓存与按需续期（Issue #226 裁定 3：应用身份写入权限发布表）。

产品负责人 2026-08-18 裁定理由（原文）：「没有凭据生命周期——不需要他再点授权、
不会过期、不需要轮换」。2026-08-18 上午刚为专用授权凭据到期紧急处理过一次（部署
scheduler 接管轮换），再增加一条会轮换的凭据是代价最高的选项。已知代价（产品负责人
已知情）：写入不绑定到某个具体授权人；需要把应用加为该 Base 的协作者。

本模块只回答"手上这份令牌还能不能用、该不该现在去换一次"，真正的 HTTP 调用由注入
的 ``fetch``（:class:`~lingxi.adapters.feishu_tenant_token.FeishuTenantTokenClient.
fetch`）提供——``core/`` 不做网络 I/O（代码框架第二节）。不落盘、不进日志/审计/异常，
凭据边界与 :mod:`lingxi.core.identity.access_token_supply` 同一条。

## 与 ``RosterAccessTokenProvider``（#215）的关键差异：没有频率上界

花名册那条"每 UTC 日至多消费一次"保护的是 ``refresh_token``——一次性、消费一次就
换代的凭据，高频消费正是 2026-08-08 授权码被烧那次事故的形状。应用身份令牌换取用的
是 ``app_id``/``app_secret``：**静态、可重复使用**的凭据，Feishu 允许按需重复换取，
不存在"消费掉就没了"的风险。因此本模块**不设频率账本**，只做"手上有没有新鲜的、
没有就去换一次"——换取频率天然被令牌有效期（通常约两小时）托底：同一进程只有在
缓存过期后才会再发一次请求。

## 复用的通用组件

进程内持有者复用 :class:`~lingxi.core.identity.access_token_supply.
DerivedAccessTokenHolder`——它是一个只回答"密文 + 过期时间 + 安全余量"的缓存，不含
任何身份域业务规则；:class:`~lingxi.core.identity.credentials.DerivedAccessToken`/
``SecretToken`` 同样是与身份域无关的通用小组件（不可打印的凭据载体、寿命字段）。
跨 ``core.permission``/``core.identity`` 复用它们，与代码框架"标识、时间、错误信封、
幂等……放在 ``core/`` 的公共小模块中，全仓库只有一份"同一条理由——这里不是引入一条
新的领域耦合，只是不重新发明一个已经正确、已经被测试钉住的"密文+寿命"缓存。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from lingxi.core.identity.access_token_supply import DerivedAccessTokenHolder
from lingxi.core.identity.credentials import DerivedAccessToken
from lingxi.core.permission.table_access_token_supply import (
    PermissionTableAccessTokenUnavailable,
)


class TenantAccessTokenSupply:
    """应用身份令牌的短期供给：``Callable[[], str]``。

    作为 :class:`~lingxi.core.permission.table_access_token_supply.
    PermissionTableAccessTokenProvider` 的 ``fetch`` 参数使用——外层 Provider 负责
    "未接线 vs 配了但拿不到"的审计外壳与失败关闭语义（已交付、不改），本类只负责
    "这次调用要不要真的去发一次请求"。

    一次调用的判定次序：

    1. **手上有新鲜的就直接给**——发布消费在一轮里可能对多条意图重复取令牌，不能
       每次都去换一次；
    2. 没有或已临期 → 调用注入的 ``fetch`` 换一份新的；
    3. ``fetch`` 换回来的令牌**寿命未知或一到手就临期**（
       :meth:`DerivedAccessTokenHolder.store` 返回 ``False``）→ 按已知分类
       :class:`~lingxi.core.permission.table_access_token_supply.
       PermissionTableAccessTokenUnavailable` (``fetch_unavailable``) 失败关闭。
       实践中 ``FeishuTenantTokenClient.fetch`` 已经在飞书没给寿命时直接拒绝
       （不会走到这一步），这里的兜底是防御性的——``fetch`` 是可替换的注入点，
       不能假设注入进来的实现总是自觉。

    ``fetch`` 本身抛出的任何异常**原样上抛**，不在这里吞掉或改写：外层 Provider
    的 ``except Exception`` 分支会把它归类成 ``fetch_error`` 并只记异常类名。
    """

    def __init__(
        self,
        *,
        fetch: Callable[[], DerivedAccessToken],
        holder: DerivedAccessTokenHolder | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(fetch):
            raise ValueError("fetch 必须是返回 DerivedAccessToken 的可调用对象")
        self._fetch = fetch
        self._holder = holder if holder is not None else DerivedAccessTokenHolder()
        self._clock = clock or (lambda: datetime.now(UTC))

    def __call__(self) -> str:
        now = self._clock()
        token = self._holder.fresh(now=now)
        if token is not None:
            return token.reveal()

        derived = self._fetch()
        if not isinstance(derived, DerivedAccessToken):
            raise TypeError("fetch 必须返回 DerivedAccessToken，避免明文令牌以裸字符串流转")
        if not self._holder.store(derived, now=now):
            raise PermissionTableAccessTokenUnavailable("fetch_unavailable")

        moment = self._clock()
        token = self._holder.fresh(now=moment)
        if token is None:  # pragma: no cover - store 成功后 fresh 理应立即成立，防御性分支
            raise PermissionTableAccessTokenUnavailable("fetch_unavailable")
        return token.reveal()


__all__ = ["TenantAccessTokenSupply"]
