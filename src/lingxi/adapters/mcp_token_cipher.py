"""问数 MCP 访问令牌的加解密（Issue #156 / S-C-02）。

这是一份**互操作实现**，不是我们自选的加密方案：当前权限多维表格的 ``token_cipher``
列由旧系统 biai-agent 写入并由问数 MCP 消费，格式在 biai-agent 的
``docs/mcp/mcp-encryption-spec.md`` v1 里定稿（已签署的交接协议）。MCP 逐行解密后与
请求 ``Bearer`` 明文**等值匹配**，解密失败即不放行。因此本模块逐字执行那份规格，
**不允许"等价改进"**——换填充、换分组模式、换编码顺序，任何一项都会让我们写出去的行
在消费方那里解不开，而失败形态是"这个人的权限静默不生效"。

## 规格（逐条对应实现）

| 规格 | 实现 |
|---|---|
| AES-256-CBC，PKCS7 填充（块 16B） | :func:`McpTokenCipher.encrypt` / :func:`~McpTokenCipher.decrypt` |
| 主密钥 32B，以 base64 字符串存环境变量 | :func:`load_master_key`，**长度必须恰好 32B** |
| 每次加密随机生成 16B IV，不复用 | ``os.urandom(16)``，每次调用各取一次 |
| 输出 ``base64(IV(16B) ‖ 密文)`` | :func:`McpTokenCipher.encrypt` 的返回值 |
| 明文 UTF-8 编解码 | ``encode("utf-8")`` / ``decode("utf-8")``，严格模式 |
| 解密失败视为记录无效，不得回退放行 | 一律抛 :class:`McpTokenCipherError`，**没有任何返回 ``None`` 的分支** |

## 明文的生命周期

令牌明文由 :func:`new_token` 生成（``secrets.token_urlsafe(32)``，256 位熵）。它
**只在三个瞬间存在于内存**：签发、就绪探针取用、将来写入用户环境。它不落库（表结构里
没有列能放它，见迁移 ``0065``）、不进日志、不进 outbox、不进异常消息。

本模块的两条纪律支撑最后一点：

- **异常消息只有错误码**，从不包含明文、密文、密钥或它们的长度以外的任何片段。
  ``str(exception)`` 是凭据最常见的泄露路径——一次 ``logger.exception`` 就够。
- :class:`McpTokenCipher` 覆盖 ``__repr__``，密钥不出现在对象的字符串形态里。默认的
  ``dataclass``/对象 repr 会在调试器、日志与 ``assert`` 失败信息里把 ``self._key``
  原样打印出来。

## 为什么在 ``adapters/``

``cryptography`` 是第三方库，而 ``core/`` 不 import 适配器或第三方库（代码框架第二节）。
``import`` 写在函数体内：``src/lingxi/`` 里没有任何模块级第三方 import，一个空环境也要
能 import 本模块（代码框架第六节）。
"""

from __future__ import annotations

import base64
import binascii
import os
import secrets

#: 主密钥的环境变量名。**本模块不读它**——``core``/``adapters`` 不直接读
#: ``os.environ``（代码框架「三、横切约定」），配置在 ``apps/`` 入口一次性读入并把
#: 值注入进来。这里只登记名字，让将来的装配层与运维文档有唯一一处可引用的口径。
MASTER_KEY_ENV = "LINGXI_MCP_TOKEN_ENCRYPT_KEY"

#: AES-256 的主密钥长度。规格写死 32 字节，短一个字节都不是同一个算法。
MASTER_KEY_BYTES = 32
#: CBC 的初始化向量长度，同时也是分组长度。
IV_BYTES = 16
BLOCK_BYTES = 16
#: ``secrets.token_urlsafe`` 的熵字节数。32 字节 = 256 位。
TOKEN_ENTROPY_BYTES = 32


class McpTokenCipherError(RuntimeError):
    """令牌加解密失败。``code`` 供程序判断，消息里**没有明文、密文与密钥**。

    解密失败在产品语义上等于"这条记录无效"：调用方必须失败关闭，不得回退到"当作没有
    令牌"或"放行一次"。用独立异常类型而不是返回 ``None``，正是为了让"忘了处理"这件事
    在运行时炸出来，而不是安静地变成一次放行。
    """

    def __init__(self, code: str) -> None:
        super().__init__(f"MCP 令牌加解密失败：{code}")
        self.code = code


def new_token() -> str:
    """生成一个新的令牌明文。

    ``secrets.token_urlsafe(32)`` 给出 256 位熵的 URL 安全 base64 串（43 个字符）。
    用 ``secrets`` 而不是 ``random``：后者是可预测的伪随机数发生器，用它签发凭据等于
    让任何知道种子的人算出别人的令牌。
    """

    return secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)


def load_master_key(value: object) -> bytes:
    """把环境变量里的 base64 主密钥解成 32 字节，**任何偏差都拒绝**。

    三道校验缺一不可，且都是"拒绝启动"式的失败而不是降级：

    1. 必须是非空字符串——``None``（变量没配）在这里就要停，不能一路带到第一次加密；
    2. 必须是合法 base64（``validate=True``，非字母表字符不被静默忽略）；
    3. 解出来必须**恰好** 32 字节。短了不是 AES-256，长了说明配错了变量；
       ``cryptography`` 自己也会拒绝，但那时错误信息指向的是库内部，而不是"配置错了"。

    **不回显收到的值**，长度也只在越界时以数字形式出现：主密钥是全系统最敏感的一个值，
    它一旦进了异常消息，就会被日志、CI 输出和工单一路复制出去。
    """

    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"MCP 令牌主密钥必须由配置注入（环境变量 {MASTER_KEY_ENV}），不得为空（不回显收到的值）"
        )
    try:
        key = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("MCP 令牌主密钥不是合法的 base64（不回显收到的值）") from error
    if len(key) != MASTER_KEY_BYTES:
        raise ValueError(
            f"MCP 令牌主密钥必须是 {MASTER_KEY_BYTES} 字节，收到 {len(key)} 字节（不回显收到的值）"
        )
    return key


class McpTokenCipher:
    """AES-256-CBC 的加解密面。构造即校验主密钥，不做任何 I/O。"""

    def __init__(self, master_key: str) -> None:
        self._key = load_master_key(master_key)

    def __repr__(self) -> str:
        """密钥不进对象的字符串形态（模块文档「明文的生命周期」）。"""

        return "McpTokenCipher(master_key=<已隐去>)"

    def encrypt(self, plaintext: str) -> str:
        """明文 → ``base64(IV(16B) ‖ 密文)``。

        **每次调用重新取一个随机 IV**，绝不复用：CBC 复用 IV 会让"两份密文的前缀相同"
        直接暴露"两份明文的前缀相同"。这一条由 ``tests/test_mcp_token_cipher.py`` 的
        ``IvRandomnessTest`` 钉着——同一明文连加两次必须得到不同的密文。
        """

        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        if not isinstance(plaintext, str) or not plaintext:
            raise McpTokenCipherError("empty_plaintext")
        iv = os.urandom(IV_BYTES)
        padder = padding.PKCS7(BLOCK_BYTES * 8).padder()
        padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()
        encryptor = Cipher(algorithms.AES(self._key), modes.CBC(iv)).encryptor()
        ciphertext = encryptor.update(padded) + encryptor.finalize()
        return base64.b64encode(iv + ciphertext).decode("ascii")

    def decrypt(self, token_cipher: object) -> str:
        """``base64(IV ‖ 密文)`` → 明文。**任何一步失败都抛错，没有回退放行的分支。**

        逐步校验的次序与规格一致：base64 解码 → 前 16B 是 IV → AES-256-CBC 解密 →
        去 PKCS7 → UTF-8 解码。形状校验（总长至少 32 字节、密文部分是 16 的整数倍）
        放在解密之前：``cryptography`` 对畸形长度抛的是库内部异常，而我们要的是一个
        **可分类**的错误码，否则运维分不清"这行数据坏了"和"我们的实现坏了"。
        """

        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        if not isinstance(token_cipher, str) or not token_cipher.strip():
            raise McpTokenCipherError("empty_cipher")
        try:
            raw = base64.b64decode(token_cipher.strip(), validate=True)
        except (binascii.Error, ValueError) as error:
            raise McpTokenCipherError("invalid_base64") from error
        if len(raw) < IV_BYTES + BLOCK_BYTES or (len(raw) - IV_BYTES) % BLOCK_BYTES != 0:
            raise McpTokenCipherError("invalid_cipher_length")
        iv, ciphertext = raw[:IV_BYTES], raw[IV_BYTES:]
        try:
            decryptor = Cipher(algorithms.AES(self._key), modes.CBC(iv)).decryptor()
            padded = decryptor.update(ciphertext) + decryptor.finalize()
            unpadder = padding.PKCS7(BLOCK_BYTES * 8).unpadder()
            plaintext = unpadder.update(padded) + unpadder.finalize()
        except Exception:  # 失败形态由 cryptography 决定
            # 只保留错误码：异常正文可能带上密文片段或填充细节（填充预言的经典入口）。
            raise McpTokenCipherError("decrypt_failed") from None
        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError:
            # 解出来的不是 UTF-8：密钥对不上但填充恰好合法时会走到这里，
            # 与"密钥正确、数据正确"必须可分辨，但一样不放行。
            raise McpTokenCipherError("invalid_utf8") from None


def looks_like_cipher(value: object) -> bool:
    """这个值**在形状上**是不是一份 ``token_cipher``（不解密，也不需要密钥）。

    存在的意义只有一个：让"把明文当密文写出去"在没有密钥的地方也能被拦住。
    ``new_token()`` 产出的明文是 43 个字符的 **URL 安全** base64（含 ``-``/``_``、
    长度不是 4 的倍数），过不了这里的标准 base64 校验；而任何一份真密文都是
    ``base64(16B IV ‖ 16B 的整数倍密文)``，长度至少 32 字节。

    :mod:`lingxi.core.permission.publish_row` 用同一套判据（那里只用标准库重写，
    因为 ``core`` 不 import ``adapters``）。两处形状判据必须一致，改动时一起改。
    """

    if not isinstance(value, str) or not value or value.strip() != value:
        return False
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return False
    return len(raw) >= IV_BYTES + BLOCK_BYTES and (len(raw) - IV_BYTES) % BLOCK_BYTES == 0


__all__ = [
    "BLOCK_BYTES",
    "IV_BYTES",
    "MASTER_KEY_BYTES",
    "MASTER_KEY_ENV",
    "McpTokenCipher",
    "McpTokenCipherError",
    "TOKEN_ENTROPY_BYTES",
    "load_master_key",
    "looks_like_cipher",
    "new_token",
]
