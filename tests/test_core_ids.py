"""ULID 生成的最小断言。

标识约定见[接口设计「二、通用约定」](../docs/技术设计/接口设计.md)：内部标识一律
ULID（时间有序、可排序、无需协调）。生成器按[代码框架「三、横切约定」]
(../docs/技术设计/代码框架.md) 放在 ``core`` 的公共小模块里，全仓库只有一份。
"""

from __future__ import annotations

import unittest

from lingxi.core.ids import new_ulid


class UlidTest(unittest.TestCase):
    def test_is_twenty_six_crockford_base32_characters(self) -> None:
        value = new_ulid()

        self.assertEqual(len(value), 26)
        self.assertTrue(set(value) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ"), value)

    def test_later_timestamps_sort_after_earlier_ones(self) -> None:
        earlier = new_ulid(now_ms=1_700_000_000_000, randomness=b"\xff" * 10)
        later = new_ulid(now_ms=1_700_000_000_001, randomness=b"\x00" * 10)

        self.assertLess(earlier, later, "ULID 的时间有序性是它被选中的唯一理由")

    def test_two_ids_generated_in_the_same_millisecond_still_differ(self) -> None:
        first = new_ulid(now_ms=1_700_000_000_000)
        second = new_ulid(now_ms=1_700_000_000_000)

        self.assertNotEqual(first, second)

    def test_out_of_range_input_fails_instead_of_silently_truncating(self) -> None:
        with self.assertRaises(ValueError):
            new_ulid(now_ms=-1)
        with self.assertRaises(ValueError):
            new_ulid(randomness=b"\x00")


class UlidUpperBoundTest(unittest.TestCase):
    def test_first_character_above_seven_is_not_a_valid_ulid(self) -> None:
        """终轮 Codex：128 位上界——首字符 8..Z 无法表示合法时间戳+随机数。"""
        from lingxi.core.ids import is_ulid

        self.assertFalse(is_ulid("Z" * 26))
        self.assertTrue(is_ulid("0" * 26))
        self.assertTrue(is_ulid("7" + "Z" * 25))


if __name__ == "__main__":
    unittest.main()
