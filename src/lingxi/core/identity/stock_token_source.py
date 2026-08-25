"""存量令牌只读源端口（Issue #281 载体，Trace #304 批次 3，2026-08-25 改道裁定）。

产品负责人 2026-08-25 把「存量令牌学习登记」从一次独立的批量 ops 操作改道为**开通链内
自动复用**（见 #281 该日评论「执行卡改道」）：首聊建档签发令牌前，先按邮箱查一次旧系统
写入的正式表，有可用密文就解密沿用，没有就照旧签一份新的。本模块只定义这一步要用到的
只读端口——**不连网络、不碰主密钥**（`core/` 不 import 适配器或第三方库，代码框架第二节）；
真正的飞书读取与 AES 解密在 `adapters/stock_token_bitable.py`。

## 两层状态，刻意分开

正式表**能不能查到这一行、这一行有没有密文**，与**密文解不解得开**，是两件事、两种
责任主体：前者是外部数据的形状，后者是我们这边的主密钥配不配得上。混成一种"三态"会让
"解密失败"这种响亮失败被参与调用方一次 ``if/else`` 悄悄归并成"没有可用密文"，从而在
`onboarding_runner.AutoOnboardingRunner` 里退回签新——这正是 #281 改道裁定明确禁止的
分支（签新会让这个人的用户环境令牌与正式表错位，造成真实 MCP 认证失败）。

因此 :data:`ADOPTABLE`（拿到可用明文）与 :data:`DECRYPT_FAILED`（有密文但解不开）各是
独立状态，与 :data:`NO_ROW`（正式表查无此人）、:data:`NO_CIPHER`（有行但密文列是空的）
共四态，供调用方逐一分支、逐一审计。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

#: 正式表查无此邮箱：真正的新员工，走原签发路径（本内测期还会先被名单闸挡住）。
NO_ROW = "no_row"
#: 正式表有这一行，但 ``token_cipher`` 列是空的：没有可采纳的东西，同样走原签发路径。
NO_CIPHER = "no_cipher"
#: 正式表有这一行、有密文，且已用受控主密钥解密成功：:attr:`StockTokenLookup.secret`
#: 是可以直接拿去采纳（``TokenIssuer.adopt_token``）的明文。
ADOPTABLE = "adoptable"
#: 正式表有这一行、有密文，但解密失败：主密钥配错或数据本身损坏。**必须响亮失败、
#: 绝不回退签新**——签新会让这个人的用户环境令牌与正式表错位（#281 改道裁定）。
DECRYPT_FAILED = "decrypt_failed"


@dataclass(frozen=True)
class StockTokenLookup:
    """一次按邮箱查询存量令牌源的结果。

    ``secret`` 声明 ``repr=False``：与 ``adapters/postgres_mcp_token.IssuedToken.secret``
    同一条纪律——取用明文只走字段本身，不让调试器/日志/断言失败信息把它带出来。它只在
    ``state`` 为 :data:`ADOPTABLE` 时非空。

    ``status`` 是正式表该行的 ``status`` 列原值（如 ``"approved"``），只在 ``state`` 为
    :data:`ADOPTABLE` 或 :data:`DECRYPT_FAILED`（即行确实存在）时可能非空；供调用方
    审计标注"是否非 approved"，**不参与采纳与否的判定**——权限面由银河同步权威决定，
    不由本步裁量（#281 改道裁定第四条）。
    """

    state: str
    secret: str = field(repr=False, default="")
    status: str = ""


class StockTokenSource(Protocol):
    """按邮箱精确查存量令牌源正式表行的只读端口。**没有任何写方法**。

    多行命中（同一邮箱在正式表里出现不止一次）必须由实现失败关闭（抛异常），
    不得挑一行返回——本端口的返回值只可能是四态之一，不是候选列表。
    """

    def lookup(self, email: str) -> StockTokenLookup: ...


__all__ = [
    "ADOPTABLE",
    "DECRYPT_FAILED",
    "NO_CIPHER",
    "NO_ROW",
    "StockTokenLookup",
    "StockTokenSource",
]
