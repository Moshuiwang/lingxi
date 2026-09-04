"""内部标识生成：全仓库唯一一份 ULID 实现。

标识约定见[接口设计「二、通用约定」](../../../docs/技术设计/接口设计.md)：内部主键与
``trace_id`` 一律 ULID——时间有序、可排序、无需协调，且不把外部系统标识（飞书
``open_id`` 等）当主键。放在 ``core`` 的公共小模块里是[代码框架「三、横切约定」]
(../../../docs/技术设计/代码框架.md)的要求：这类横切能力只允许存在一份，各模块不自造。

不引第三方 ULID 库：26 个字符的 Crockford Base32 编码用标准库就能写完，而依赖越少，
`biai-stage` 与生产的安装面越小。
"""

from __future__ import annotations

import os
import time

# Crockford Base32：去掉了 I、L、O、U，避免人工转抄时的歧义。
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_TIME_BITS = 48
_RANDOM_BYTES = 10
_ENCODED_LENGTH = 26


def new_ulid(*, now_ms: int | None = None, randomness: bytes | None = None) -> str:
    """生成一个 ULID：48 位毫秒时间戳 + 80 位随机数。

    ``now_ms`` 与 ``randomness`` 只为测试可重复而开放；正常调用不传。越界输入直接
    抛错，不静默截断——一个被悄悄截断的标识会在排序和唯一性上同时说谎。
    """

    timestamp = int(time.time() * 1000) if now_ms is None else int(now_ms)
    if not 0 <= timestamp < (1 << _TIME_BITS):
        raise ValueError(f"时间戳超出 ULID 的 48 位范围：{timestamp}")

    entropy = os.urandom(_RANDOM_BYTES) if randomness is None else bytes(randomness)
    if len(entropy) != _RANDOM_BYTES:
        raise ValueError(f"ULID 的随机部分必须是 {_RANDOM_BYTES} 字节，收到 {len(entropy)} 字节")

    value = (timestamp << (_RANDOM_BYTES * 8)) | int.from_bytes(entropy, "big")
    return "".join(
        _ALPHABET[(value >> shift) & 0x1F] for shift in range((_ENCODED_LENGTH - 1) * 5, -1, -5)
    )


def new_id(prefix: str, *, now_ms: int | None = None) -> str:
    """按接口设计的前缀约定生成内部标识，例如 ``usr_01HXYZ...``。"""

    if not prefix or not prefix.isascii() or not prefix.replace("_", "").isalnum():
        raise ValueError("标识前缀必须是 ASCII 字母数字")
    return f"{prefix}_{new_ulid(now_ms=now_ms)}"


_CROCKFORD = frozenset("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def is_ulid(value: object) -> bool:
    """26 位 Crockford Base32 的形状校验（大小写不敏感）。

    外部传入的 trace_id 必须过这道：任意字符串直通会破坏全仓库的 ULID 排序
    约定，误接的令牌还会随错误输出原样外泄（Codex 复查发现）。"""

    if not isinstance(value, str) or len(value) != 26:
        return False
    upper = value.upper()
    # 128 位上界：首字符只能是 0-7，否则最高两个填充位非零，
    # 不是合法的 48 位时间戳 + 80 位随机数（终轮 Codex 复查发现）。
    if upper[0] not in "01234567":
        return False
    return all(ch in _CROCKFORD for ch in upper)
