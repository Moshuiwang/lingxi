"""【探针，用完即关】#748 E-4 后半：这条用例刻意失败，证明真实门禁一定判红。"""

import unittest


class Probe748MustFailTest(unittest.TestCase):
    def test_real_gate_must_be_red(self):
        self.fail("探针：真实门禁必须判红（同名伪造不得被采信）")
