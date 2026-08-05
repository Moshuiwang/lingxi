"""内部标识的唯一生成出口：ULID。

`docs/技术设计/接口设计.md` 的「二、通用约定」规定内部实体一律用 ULID，并规定
飞书侧标识永不作为内部主键。全仓库只在这里生成 ULID，避免各模块自造格式。

不引入第三方 ULID 库：规范只有 48 位毫秒时间戳 + 80 位随机数 + Crockford
Base32 三件事，自己写比多一条依赖便宜，而依赖数量是要在 `pyproject.toml` 里
交代清楚的（V-部署-08）。
"""

from __future__ import annotations

import os
import time

# Crockford Base32：去掉 I、L、O、U，避免与 1/0 混淆，也避免拼出脏词。
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_LENGTH = 26


def new_ulid(*, now_ms: int | None = None) -> str:
    """生成一个 26 字符的 ULID。同一毫秒内的多个值按字典序不保证严格递增。"""

    timestamp = int(time.time() * 1000) if now_ms is None else now_ms
    if timestamp < 0 or timestamp >= 1 << 48:
        raise ValueError("ULID 的时间部分只有 48 位")
    value = (timestamp << 80) | int.from_bytes(os.urandom(10), "big")
    characters = []
    for shift in range(_ULID_LENGTH - 1, -1, -1):
        characters.append(_ALPHABET[(value >> (shift * 5)) & 0b11111])
    return "".join(characters)


def new_id(prefix: str, *, now_ms: int | None = None) -> str:
    """按接口设计的前缀约定生成内部标识，例如 ``usr_01HXYZ...``。"""

    if not prefix or not prefix.isascii() or not prefix.replace("_", "").isalnum():
        raise ValueError("标识前缀必须是 ASCII 字母数字")
    return f"{prefix}_{new_ulid(now_ms=now_ms)}"
