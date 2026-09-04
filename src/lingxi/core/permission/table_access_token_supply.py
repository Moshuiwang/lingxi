"""权限发布表读写所用短期令牌供给的**方向无关**外壳（Issue #226 前置）。

## 背景

`build_loop` 的 ``permission_table_access_token`` 参数（``Callable[[], str] | None``）
早已存在，装配点也早已接好（见 :mod:`lingxi.apps.scheduler` 的
``_build_permission_publish_duty``）：缺它时权限发布面按
``permission_table_access_token_unwired`` 失败关闭且**恰一条**审计，其余职责照常
运行——这条行为不属于本模块，本模块也不改它。

真正缺的是**怎么构造那个供给**，而这件事卡在一个只有产品负责人能定的问题上：权限
发布表（与花名册不在同一个 Base）用哪个身份写入？三个候选方向——复用 #215 已交付的
专用主体、新建一个专用主体、用应用身份——**令牌供给的形状是一样的**：都是一个
``Callable[[], str]``，拿不到就要失败关闭且不泄漏令牌值。本模块只交付这个共同形状，
不替产品负责人选方向；裁定落地后，只需要把"怎么去拿一份新令牌"这一小段
（``fetch`` 参数）换成对应方向的实现，装配点、失败关闭语义与审计边界不需要再动。

## 与 ``RosterAccessTokenProvider`` 的关系

形状照 :mod:`lingxi.core.identity.access_token_supply` 的
``RosterAccessTokenProvider`` （Issue #215 已交付，#226 明确指向的先例）：同一套
"手上有新鲜的就直接给、没有就去拿一次、拿不到就分类失败关闭、审计只记分类且同一天
同一分类只记一条"的外壳。**唯一的差别**：那个类把"怎么续期"硬编码成花名册凭据轮换
职责的 ``refresh_for_supply``（花名册只有一个专用主体，不需要抽象这一步）；这里改成
一个通用的 ``fetch`` 参数，因为权限发布表用哪个身份写入还没有裁定——裁定后不同方向
对应完全不同的取token方式（复用主体是"轮换职责按需换一次"、新建主体是同一形状但
换一套凭据、应用身份可能是"用 app_id/app_secret 换一份 tenant_access_token，没有
用户授权、没有一次性 refresh_token 的消费节奏"），本模块不预判是哪一种。

**没有新鲜令牌缓存（没有 holder）**：是否需要缓存、缓存多久，同样是方向裁定的一部分
——应用身份令牌的续期节奏很可能与用户授权令牌完全不同。方向裁定后如果需要缓存，
可以在 ``fetch`` 内部自己做，或者回到这里加一层；本模块不在方向未定时替这件事拍板。

## 两种要能分辨的失败状态（Issue #226 完成标准）

1. **未接线**：``build_loop`` 收到的 ``permission_table_access_token`` 是 ``None``
   ——调用方压根没交出任何供给。这条分支已经在装配层处理
   （``permission_table_access_token_unwired``），本模块不重复、也不改它。
2. **配了但拿不到令牌**：调用方交出了一个供给（本模块 :class:`PermissionTable
   AccessTokenProvider` 的实例），但它在运行期拿不到令牌。这条分支由
   :meth:`PermissionTableAccessTokenProvider.__call__` 产生，审计动作是
   ``permission_table_access_token.unavailable``，与"未接线"是不同的审计动作名，
   因此两者天然可分辨——不依赖任何额外的判断逻辑。

## 纪律（与花名册那份同源）

- 令牌**值**不进日志、审计与异常：失败只报固定词表里的分类（``reason``）与可选的
  异常**类名**，绝不回显 ``fetch`` 抛出的原始异常正文或返回值；
- 审计**同一天同一分类只记一条**：定时循环默认每分钟一轮，逐次记录会把真正的信号
  淹掉（与 ``RosterAccessTokenProvider`` 同一条理由）；
- **绝不返回空串**：空串会让"没有凭据"伪装成"发布表读写失败"，把排障指向错误的
  地方。
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


class PermissionTableAccessTokenUnavailable(RuntimeError):
    """权限发布表短期令牌供给失败。``reason`` 必须取自固定词表（构造期就校验）。"""

    def __init__(self, reason: str) -> None:
        if reason not in TABLE_TOKEN_SUPPLY_FAILURE_REASONS:
            raise ValueError("权限发布表令牌供给的失败分类必须取自固定词表（不回显收到的值）")
        super().__init__(reason)
        self.reason = reason


class PermissionTableAccessTokenProvider:
    """权限发布表读写的短期令牌供给：``Callable[[], str]``，交给
    ``build_loop`` 的 ``permission_table_access_token`` 参数。

    ``fetch`` 是**方向裁定后才注入**的那一小段：它应当返回一份非空的短期令牌明文，
    拿不到就抛 :class:`PermissionTableAccessTokenUnavailable`（或任意异常，本类会把
    它归类成 ``fetch_error`` 并只记异常类名）。本类自己不做任何刷新节奏、不做任何
    凭据落盘——那些属于 ``fetch`` 内部的方向特定逻辑；本类只负责失败关闭与审计的
    公共外壳，三个候选方向都能直接复用。
    """

    def __init__(
        self,
        *,
        fetch: Callable[[], str],
        audit: AuditSink | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(fetch):
            raise ValueError("fetch 必须是返回短期令牌明文的可调用对象")
        self._fetch = fetch
        self._audit = audit
        self._clock = clock or (lambda: datetime.now(UTC))
        self._audited_on: date | None = None
        self._audited_reasons: set[str] = set()

    def __call__(self) -> str:
        try:
            token = self._fetch()
        except PermissionTableAccessTokenUnavailable as error:
            self._record(error.reason)
            raise
        except Exception as error:  # noqa: BLE001 - 只保留分类，异常正文可能带响应内容
            self._record("fetch_error", error_type=type(error).__name__)
            raise PermissionTableAccessTokenUnavailable("fetch_error") from None
        if not token:
            self._record("token_empty")
            raise PermissionTableAccessTokenUnavailable("token_empty")
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
    "PermissionTableAccessTokenUnavailable",
]
