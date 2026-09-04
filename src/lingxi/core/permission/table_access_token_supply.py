"""权限发布表读写所用短期令牌供给的方向无关外壳。

`build_loop` 的装配点已接好：缺供给时权限发布面按未接线原因失败关闭，本模块不
重复、也不改它。真正缺的是怎么构造那个供给——权限发布表用哪个身份写入还没有
裁定，但候选方向的令牌供给形状都一样：一个 ``Callable[[], str]``，拿不到就要
失败关闭且不泄漏令牌值。本模块只交付这个共同外壳，裁定落地后只需要把"怎么去
拿一份新令牌"这一小段（``fetch`` 参数）换成对应实现。形状照
``core.identity.access_token_supply.RosterAccessTokenProvider``：手上有新鲜的
直接给、没有就去拿一次、拿不到就分类失败关闭、审计同一天同一分类只记一条；
唯一差别是 ``fetch`` 通用注入而非硬编码，也没有新鲜令牌缓存。

两种失败状态天然可分辨：未接线走装配层既有分支；配了但拿不到令牌走本模块，
审计动作名不同。纪律：令牌值不进日志/审计/异常，只报分类与可选异常类名；
绝不返回空串。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Protocol

#: 供给失败的全部分类。刻意用固定词表：分类会进审计与异常正文，新增分类要先想清楚
#: "它会出现在审计里"这件事（纪律同 ``core.identity.access_token_supply``）。
TABLE_TOKEN_SUPPLY_FAILURE_REASONS = frozenset(
    {
        # 注入的 fetch 判定"现在拿不到"，并按本模块的失败关闭规则显式抛出。
        # 具体是"凭据缺失"还是"被占用"还是别的，属方向裁定后 fetch 内部自己的分类，
        # 本模块不替它细分。
        "fetch_unavailable",
        # 注入的 fetch 抛了本模块不认识的异常：只记类名，不记正文——原始异常可能
        # 带响应体或凭据材料。
        "fetch_error",
        # fetch 返回了空值：绝不能把空串当令牌交出去。
        "token_empty",
    }
)


class AuditSink(Protocol):
    """审计出口。与 ``core/alerting.py``、``apps/scheduler`` 的同名 Protocol 结构一致。"""

    def record(self, action: str, /, **fields: object) -> None: ...


class PermissionTableAccessTokenUnavailableError(RuntimeError):
    """权限发布表短期令牌供给失败。``reason`` 必须取自固定词表（构造期就校验）。"""

    def __init__(self, reason: str) -> None:
        """用固定词表里的失败分类构造；分类不在表里时拒绝构造。"""
        if reason not in TABLE_TOKEN_SUPPLY_FAILURE_REASONS:
            raise ValueError("权限发布表令牌供给的失败分类必须取自固定词表（不回显收到的值）")
        super().__init__(reason)
        self.reason = reason


class PermissionTableAccessTokenProvider:
    """权限发布表读写的短期令牌供给：``Callable[[], str]``。

    交给 ``build_loop`` 的 ``permission_table_access_token`` 参数。``fetch``
    应当返回一份非空的短期令牌明文，拿不到就抛
    :class:`PermissionTableAccessTokenUnavailableError`（或任意异常，本类会把它
    归类成 ``fetch_error`` 并只记异常类名）。本类自己不做任何刷新节奏、不做任何
    凭据落盘——那些属于 ``fetch`` 内部的方向特定逻辑；本类只负责失败关闭与审计
    的公共外壳，候选方向都能直接复用。
    """

    def __init__(
        self,
        *,
        fetch: Callable[[], str],
        audit: AuditSink | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """接线取令牌回调、可选审计出口与可选的时钟替身（测试用）。"""
        if not callable(fetch):
            raise ValueError("fetch 必须是返回短期令牌明文的可调用对象")
        self._fetch = fetch
        self._audit = audit
        self._clock = clock or (lambda: datetime.now(UTC))
        self._audited_on: date | None = None
        self._audited_reasons: set[str] = set()

    def __call__(self) -> str:
        """取一份可用的短期令牌；拿不到时抛 :class:`PermissionTableAccessTokenUnavailableError`。"""
        try:
            token = self._fetch()
        except PermissionTableAccessTokenUnavailableError as error:
            self._record(error.reason)
            raise
        except Exception as error:  # 只保留分类，异常正文可能带响应内容
            self._record("fetch_error", error_type=type(error).__name__)
            raise PermissionTableAccessTokenUnavailableError("fetch_error") from None
        if not token:
            self._record("token_empty")
            raise PermissionTableAccessTokenUnavailableError("token_empty")
        return token

    # ---- 内部 -------------------------------------------------------------

    def _record(self, reason: str, **sanitized: str) -> None:
        """记一条失败审计；同一天同一分类只记一条。只有分类与日期，绝不含令牌值。"""
        now = self._clock()
        today = now.date()
        if self._audited_on != today:
            self._audited_on = today
            self._audited_reasons = set()
        if reason in self._audited_reasons:
            return
        self._audited_reasons.add(reason)
        if self._audit is not None:
            self._audit.record(
                "permission_table_access_token.unavailable",
                reason=reason,
                report_date=today.isoformat(),
                **sanitized,
            )


__all__ = [
    "TABLE_TOKEN_SUPPLY_FAILURE_REASONS",
    "PermissionTableAccessTokenProvider",
    "PermissionTableAccessTokenUnavailableError",
]
