"""存量令牌只读源端口。

首聊建档签发令牌前，先按邮箱查一次旧系统写入的正式表，有可用密文就解密沿用，
没有就照旧签一份新的。本模块只定义这一步要用到的只读端口——不连网络、不碰主
密钥；真正的飞书读取与 AES 解密在 `adapters/stock_token_bitable.py`。

**两层状态，刻意分开**：正式表能不能查到这一行、这一行有没有密文，与密文解不
解得开，是两件事、两种责任主体——前者是外部数据的形状，后者是我们这边的主密钥
配不配得上。混成一种"三态"会让"解密失败"这种响亮失败被调用方一次 ``if/else``
悄悄归并成"没有可用密文"，从而退回签新——签新会让这个人的用户环境令牌与正式表
错位，造成真实 MCP 认证失败。因此 :data:`ADOPTABLE`（拿到可用明文）与
:data:`DECRYPT_FAILED`（有密文但解不开）各是独立状态，与 :data:`NO_ROW`（正式表
查无此人）、:data:`NO_CIPHER`（有行但密文列是空的）共四态，供调用方逐一分支、
逐一审计。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

#: 正式表查无此邮箱：真正的新员工，走原签发路径（本内测期还会先被名单闸挡住）。
NO_ROW = "no_row"
#: 正式表有这一行，但 ``token_cipher`` 列是空的：没有可采纳的东西，同样走原签发路径。
NO_CIPHER = "no_cipher"
#: 正式表有这一行、有密文，且已用受控主密钥解密成功：:attr:`StockTokenLookup.secret`
#: 是可以直接拿去采纳（供令牌签发端 ``adopt_token`` 使用）的明文。
ADOPTABLE = "adoptable"
#: 正式表有这一行、有密文，但解密失败：主密钥配错或数据本身损坏。**必须响亮失败、
#: 绝不回退签新**——签新会让这个人的用户环境令牌与正式表错位。
DECRYPT_FAILED = "decrypt_failed"


@dataclass(frozen=True)
class StockTokenLookup:
    """一次按邮箱查询存量令牌源的结果。

    ``secret`` 声明 ``repr=False``：取用明文只走字段本身，不让调试器/日志/断言
    失败信息把它带出来，只在 ``state`` 为 :data:`ADOPTABLE` 时非空。
    ``permissions`` 是该行 ``permissions`` 列原文，只在 :data:`ADOPTABLE` 时非
    ``None``（存量差集导入的唯一输入，解析与差集在
    ``core/permission/legacy_diff.py``）。``status`` 是正式表该行的 ``status``
    列原值，只在行确实存在时可能非空，供调用方审计标注，**不参与采纳与否的
    判定**——权限面由银河同步权威决定，不由本步裁量。
    """

    state: str
    secret: str = field(repr=False, default="")
    status: str = ""
    #: 正式表该行 ``permissions`` 单元格的**原文**：只在
    #: :data:`ADOPTABLE` 时携带，供开通链把「旧行权限 − 银河当前翻译」落成本地授权；
    #: 其余状态恒为 ``None``。它是权限文档不是凭据，但同样不进审计（审计只记计数）。
    permissions: str | None = None


class StockTokenSource(Protocol):
    """按邮箱精确查存量令牌源正式表行的只读端口。**没有任何写方法**。

    多行命中（同一邮箱在正式表里出现不止一次）必须由实现失败关闭（抛异常），
    不得挑一行返回——本端口的返回值只可能是四态之一，不是候选列表。
    """

    def lookup(self, email: str) -> StockTokenLookup:
        """按邮箱精确查存量令牌源正式表行，返回四态之一的结果。"""
        ...


__all__ = [
    "ADOPTABLE",
    "DECRYPT_FAILED",
    "NO_CIPHER",
    "NO_ROW",
    "StockTokenLookup",
    "StockTokenSource",
]
