"""飞书外部标识的日志脱敏出口。

**这是全仓库唯一允许缩短飞书标识的地方，而且只允许用于日志。**
`open_user_id` 的 6 位前缀在 710 人实测中有 57 组碰撞（[Issue #16 硬约束 5]
(https://github.com/Moshuiwang/lingxi/issues/16)），因此缩短后的形式不具备
区分能力：任何比对、去重、定位都必须使用完整标识。

:func:`redact_identifier` 刻意返回一个**不能反查也不能比较**的字符串，
让「拿脱敏值当键用」在类型和取值上都走不通。
"""

from __future__ import annotations

LOG_PREFIX_LENGTH = 6
BLANK_IDENTIFIER = "<空标识>"


def redact_identifier(value: object) -> str:
    """把飞书标识缩成只可写进日志的短形式。

    返回值形如 ``ou_abc…(28)``：前缀会碰撞、长度也会碰撞，因此它**不是**标识。
    需要判断"是不是同一个人"时用完整 ``open_id`` / ``user_id``，不要用本函数。
    """

    if not isinstance(value, str):
        return BLANK_IDENTIFIER
    text = value.strip()
    if not text:
        return BLANK_IDENTIFIER
    return f"{text[:LOG_PREFIX_LENGTH]}…({len(text)})"
