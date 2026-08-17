"""问数 MCP 令牌加解密的断言（Issue #156 / S-C-02）。

认领断言：`V-权限-11`（新建行必须携带 Lingxi 签发的 token_cipher；**明文与密钥不进
outbox、日志与告警**）的加密面。

这份用例分两半，故意分开：

- **不需要 ``cryptography`` 的那一半**（主密钥校验、形状判据、明文生成）总是跑。
  它们守的是"配错了要拒绝启动"和"明文不会被当成密文写出去"，在任何机器上都必须有效。
- **需要 ``cryptography`` 的那一半**（互操作向量、加解密回环、IV 随机性）在缺库的机器上
  明确跳过并说明原因，不静默通过（代码框架第四节）。

**测试向量里的主密钥是规格公开的测试向量，非生产密钥**（biai-agent
``docs/mcp/mcp-encryption-spec.md`` v1 的自验章节），因此可以入库；它的明文是
``demo-token-DO-NOT-USE``，本身就写着不要用。生产主密钥只从环境变量注入
（``LINGXI_MCP_TOKEN_ENCRYPT_KEY``），一次都不出现在仓库里。
"""

from __future__ import annotations

import base64
import unittest

from lingxi.adapters.mcp_token_cipher import (
    IV_BYTES,
    MASTER_KEY_BYTES,
    MASTER_KEY_ENV,
    McpTokenCipher,
    McpTokenCipherError,
    load_master_key,
    looks_like_cipher,
    new_token,
)

try:  # pragma: no cover - 取决于机器上装没装
    import cryptography  # noqa: F401

    HAS_CRYPTOGRAPHY = True
except ModuleNotFoundError:  # pragma: no cover
    HAS_CRYPTOGRAPHY = False

CRYPTO_SKIP = "跳过：未安装 cryptography，AES-256-CBC 互操作向量与回环未验证"

# ---------------------------------------------------------------------------
# biai-agent 加密规格 v1 的自验向量。**规格公开、非生产密钥**，可入仓库。
# master_key 的 base64 解出来是 ASCII "0123456789abcdef0123456789abcdef"。
# 该向量的 IV 固定为 ASCII "FIXEDIV123456789"——**只有向量如此**，生产加密每次随机取 IV
# （由 IvRandomnessTest 钉住）。
# ---------------------------------------------------------------------------
SPEC_MASTER_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
SPEC_TOKEN_CIPHER = "RklYRURJVjEyMzQ1Njc4OX5gpf2vKqJiLgzu2n4kug1V1rz6DDt1OCgAZVpg1pL+"
SPEC_PLAINTEXT = "demo-token-DO-NOT-USE"
SPEC_FIXED_IV = b"FIXEDIV123456789"


class MasterKeyValidationTest(unittest.TestCase):
    """主密钥非法时**拒绝启动**，不静默降级（本 Story 的否定断言之一）。"""

    def test_accepts_the_thirty_two_byte_spec_key(self) -> None:
        self.assertEqual(load_master_key(SPEC_MASTER_KEY), b"0123456789abcdef0123456789abcdef")
        self.assertEqual(len(load_master_key(SPEC_MASTER_KEY)), MASTER_KEY_BYTES)

    def test_rejects_missing_key(self) -> None:
        for value in (None, "", "   ", 32, b"0123456789abcdef0123456789abcdef"):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(ValueError) as caught:
                    load_master_key(value)
                self.assertIn(MASTER_KEY_ENV, str(caught.exception))

    def test_rejects_invalid_base64(self) -> None:
        with self.assertRaises(ValueError) as caught:
            load_master_key("这不是 base64！！")
        self.assertIn("base64", str(caught.exception))

    def test_rejects_wrong_length_key(self) -> None:
        for raw_length in (16, 24, 31, 33, 64):
            with self.subTest(raw_length=raw_length):
                encoded = base64.b64encode(b"k" * raw_length).decode()
                with self.assertRaises(ValueError) as caught:
                    load_master_key(encoded)
                self.assertIn(str(MASTER_KEY_BYTES), str(caught.exception))

    def test_error_never_echoes_the_key(self) -> None:
        """密钥一旦进异常消息，就会被日志、CI 输出和工单一路复制出去。"""

        secret = base64.b64encode(b"k" * 31).decode()
        with self.assertRaises(ValueError) as caught:
            load_master_key(secret)
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn("kkkk", str(caught.exception))

    def test_constructor_rejects_bad_key_without_touching_crypto(self) -> None:
        """构造即校验：不合法的密钥不会等到第一次加密才失败。"""

        with self.assertRaises(ValueError):
            McpTokenCipher(base64.b64encode(b"short").decode())

    def test_repr_does_not_leak_the_key(self) -> None:
        cipher = McpTokenCipher(SPEC_MASTER_KEY)
        self.assertNotIn(SPEC_MASTER_KEY, repr(cipher))
        self.assertNotIn("0123456789abcdef", repr(cipher))


class TokenGenerationTest(unittest.TestCase):
    def test_tokens_are_unique_and_long(self) -> None:
        tokens = {new_token() for _ in range(64)}
        self.assertEqual(len(tokens), 64)
        for token in tokens:
            self.assertGreaterEqual(len(token), 40)

    def test_plaintext_never_passes_the_cipher_shape_check(self) -> None:
        """把明文当密文写出去必须在没有密钥的地方就被拦住。

        ``token_urlsafe(32)`` 是 43 个字符的 URL 安全 base64（含 ``-``/``_``、长度不是
        4 的倍数），过不了标准 base64 校验——这正是 ``publish_row`` 能在 ``core`` 里
        （拿不到密钥）挡住"明文落表"的机制。
        """

        for _ in range(64):
            self.assertFalse(looks_like_cipher(new_token()))

    def test_cipher_shape_check_rejects_junk(self) -> None:
        for value in (None, "", "  ", 42, "not base64!", base64.b64encode(b"x" * 16).decode()):
            with self.subTest(value=repr(value)):
                self.assertFalse(looks_like_cipher(value))

    def test_cipher_shape_check_accepts_a_well_formed_cipher(self) -> None:
        self.assertTrue(looks_like_cipher(SPEC_TOKEN_CIPHER))


@unittest.skipUnless(HAS_CRYPTOGRAPHY, CRYPTO_SKIP)
class SpecInteroperabilityTest(unittest.TestCase):
    """规格自验向量：我们的解密必须还原出规格声明的明文。

    这条是**互操作**断言，不是自洽断言：只测"自己加密再自己解密"永远是绿的，哪怕
    我们把填充或字节序整个换掉——而换掉之后写出去的行在问数 MCP 那边解不开，
    失败形态是"这个人的权限静默不生效"。
    """

    def test_decrypts_the_published_vector(self) -> None:
        cipher = McpTokenCipher(SPEC_MASTER_KEY)
        self.assertEqual(cipher.decrypt(SPEC_TOKEN_CIPHER), SPEC_PLAINTEXT)

    def test_vector_layout_is_iv_then_ciphertext(self) -> None:
        raw = base64.b64decode(SPEC_TOKEN_CIPHER, validate=True)
        self.assertEqual(raw[:IV_BYTES], SPEC_FIXED_IV)
        self.assertEqual((len(raw) - IV_BYTES) % 16, 0)


@unittest.skipUnless(HAS_CRYPTOGRAPHY, CRYPTO_SKIP)
class RoundTripTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cipher = McpTokenCipher(SPEC_MASTER_KEY)

    def test_round_trip(self) -> None:
        for plaintext in (new_token(), "a", "x" * 16, "x" * 17, "中文令牌与符号 ~!@#"):
            with self.subTest(length=len(plaintext)):
                self.assertEqual(self.cipher.decrypt(self.cipher.encrypt(plaintext)), plaintext)

    def test_output_is_base64_of_iv_and_blocks(self) -> None:
        encoded = self.cipher.encrypt(new_token())
        self.assertTrue(looks_like_cipher(encoded))
        raw = base64.b64decode(encoded, validate=True)
        self.assertEqual(len(raw[:IV_BYTES]), IV_BYTES)
        self.assertEqual((len(raw) - IV_BYTES) % 16, 0)

    def test_ciphertext_does_not_contain_the_plaintext(self) -> None:
        plaintext = new_token()
        encoded = self.cipher.encrypt(plaintext)
        self.assertNotIn(plaintext, encoded)
        self.assertNotIn(plaintext.encode(), base64.b64decode(encoded, validate=True))


@unittest.skipUnless(HAS_CRYPTOGRAPHY, CRYPTO_SKIP)
class IvRandomnessTest(unittest.TestCase):
    """IV 每次随机、绝不复用。CBC 复用 IV 会让"两份明文前缀相同"从密文上直接可见。"""

    def test_same_plaintext_encrypts_differently_every_time(self) -> None:
        cipher = McpTokenCipher(SPEC_MASTER_KEY)
        plaintext = "same-token-every-time"
        outputs = {cipher.encrypt(plaintext) for _ in range(32)}
        self.assertEqual(len(outputs), 32)

    def test_ivs_are_all_distinct(self) -> None:
        cipher = McpTokenCipher(SPEC_MASTER_KEY)
        ivs = {
            base64.b64decode(cipher.encrypt("same-token-every-time"), validate=True)[:IV_BYTES]
            for _ in range(32)
        }
        self.assertEqual(len(ivs), 32)
        self.assertNotIn(SPEC_FIXED_IV, ivs)


@unittest.skipUnless(HAS_CRYPTOGRAPHY, CRYPTO_SKIP)
class DecryptionFailureTest(unittest.TestCase):
    """解密失败一律失败关闭：没有任何返回 ``None`` 或"当作没有令牌"的分支。"""

    def setUp(self) -> None:
        self.cipher = McpTokenCipher(SPEC_MASTER_KEY)

    def test_rejects_empty_and_non_string(self) -> None:
        for value in (None, "", "   ", 42, b"abc"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(McpTokenCipherError) as caught:
                    self.cipher.decrypt(value)
                self.assertEqual(caught.exception.code, "empty_cipher")

    def test_rejects_invalid_base64(self) -> None:
        with self.assertRaises(McpTokenCipherError) as caught:
            self.cipher.decrypt("这不是 base64！！")
        self.assertEqual(caught.exception.code, "invalid_base64")

    def test_rejects_short_or_misaligned_payload(self) -> None:
        for raw in (b"x" * 16, b"x" * 31, b"x" * 33, b"x" * 47):
            with self.subTest(length=len(raw)):
                with self.assertRaises(McpTokenCipherError) as caught:
                    self.cipher.decrypt(base64.b64encode(raw).decode())
                self.assertEqual(caught.exception.code, "invalid_cipher_length")

    def test_wrong_key_does_not_return_a_plaintext(self) -> None:
        """换一把密钥解同一份密文：必须抛错，绝不能返回一串垃圾当明文放行。"""

        other = McpTokenCipher(base64.b64encode(b"z" * 32).decode())
        with self.assertRaises(McpTokenCipherError) as caught:
            other.decrypt(SPEC_TOKEN_CIPHER)
        self.assertIn(caught.exception.code, ("decrypt_failed", "invalid_utf8"))

    def test_tampered_ciphertext_is_rejected(self) -> None:
        raw = bytearray(base64.b64decode(self.cipher.encrypt(new_token()), validate=True))
        raw[-1] ^= 0xFF
        with self.assertRaises(McpTokenCipherError):
            self.cipher.decrypt(base64.b64encode(bytes(raw)).decode())

    def test_failure_message_carries_no_cipher_material(self) -> None:
        encoded = self.cipher.encrypt("secret-token-value")
        other = McpTokenCipher(base64.b64encode(b"z" * 32).decode())
        try:
            other.decrypt(encoded)
        except McpTokenCipherError as error:
            message = f"{error!r} {error}"
            self.assertNotIn(encoded, message)
            self.assertNotIn("secret-token-value", message)
        else:  # pragma: no cover - 上一条断言已经保证走不到
            self.fail("换密钥解密必须失败")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
