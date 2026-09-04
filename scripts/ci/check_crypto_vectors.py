#!/usr/bin/env python3
"""断言 MCP 令牌加解密的互操作向量**真的跑过了**（Issue #156 / S-C-02）。

它挡的是一种特别安静的失败：``tests/test_mcp_token_cipher.py`` 里需要 ``cryptography``
的那几组用例带着 ``skipUnless``，缺库时**跳过而不是失败**。而 ``src/lingxi`` 的第三方
import 全是函数内延迟导入，"模块 import 成功"证明不了依赖装上了——于是一个没装
``cryptography`` 的环境里，整组 AES 断言（互操作向量、IV 随机性、解密失败不放行）
一条都没跑，测试输出却是绿的。

跳过是对的（代码框架第四节：``unittest discover`` 必须能在无外部依赖的机器上跑完），
**但门禁不能跟着一起跳过**。本脚本因此做两件事：

1. ``cryptography`` 没装就**明确失败**并给出安装命令，不 try/except 跳过
   （纪律与 ``check_alembic_revisions.py`` 对 ``alembic`` 完全一致）；
2. 实际跑一遍那几组用例，并要求**执行数非零、跳过数为零**。只要求"跑通"是不够的——
   一个全被跳过的测试组同样是"跑通"的。

用法：

    python3 scripts/ci/check_crypto_vectors.py
"""

from __future__ import annotations

import pathlib
import sys
import unittest

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
TESTS_ROOT = REPOSITORY_ROOT / "tests"

# 这几组是**互操作**断言：它们证明我们写出去的密文，问数 MCP 那一侧解得开。
# 只测"自己加密再自己解密"永远是绿的，哪怕把填充或字节序整个换掉。
REQUIRED_TEST_GROUPS = (
    "test_mcp_token_cipher.SpecInteroperabilityTest",
    "test_mcp_token_cipher.RoundTripTest",
    "test_mcp_token_cipher.IvRandomnessTest",
    "test_mcp_token_cipher.DecryptionFailureTest",
)


def _fail_without_cryptography() -> None:
    try:
        import cryptography  # noqa: F401
    except ModuleNotFoundError as error:
        print(
            "cryptography 没装，MCP 令牌加解密的互操作向量无法验证。这不是可跳过的情况："
            "门禁跳过它等于没有这组断言，而那组断言守的是「我们写出去的密文消费方解得开」。\n"
            "  安装：python3 -m pip install '.[scheduler]'\n"
            f"  原始错误：{error}",
            file=sys.stderr,
        )
        raise SystemExit(1)


def main() -> int:
    _fail_without_cryptography()

    for path in (SOURCE_ROOT, TESTS_ROOT):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))

    suite = unittest.defaultTestLoader.loadTestsFromNames(REQUIRED_TEST_GROUPS)
    planned = suite.countTestCases()
    if planned == 0:
        print(
            "加密互操作用例一条都没加载到：测试组被改名或删掉了。"
            f"期望的组：{'、'.join(REQUIRED_TEST_GROUPS)}",
            file=sys.stderr,
        )
        return 1

    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=0).run(suite)
    failures: list[str] = []
    if not result.wasSuccessful():
        failures.append(
            f"加密互操作用例未全部通过：失败 {len(result.failures)}，错误 {len(result.errors)}"
        )
    if result.skipped:
        # 关键的一条：**跳过也算不通过**。缺依赖时上面已经失败了，走到这里还有跳过，
        # 说明有人给这几组加了别的跳过条件——那等于把断言悄悄关掉。
        skipped = "、".join(f"{case}（{reason}）" for case, reason in result.skipped)
        failures.append(f"加密互操作用例被跳过 {len(result.skipped)} 条，门禁不接受跳过：{skipped}")
    executed = result.testsRun - len(result.skipped)
    if executed <= 0:
        failures.append("加密互操作用例实际执行数为零")

    if failures:
        print("MCP 令牌加密互操作检查：不通过", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print(f"MCP 令牌加密互操作：通过（{executed} 条实际执行，零跳过）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
