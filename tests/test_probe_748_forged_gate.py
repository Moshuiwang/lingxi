"""【探针，用完即关】Trace #770 E-1 ③：这条用例刻意失败——主干版门禁复跑该头提交时必须判红。"""

import unittest


class Probe748ForgedGateTest(unittest.TestCase):
    def test_real_gate_must_fail_on_this_head(self):
        self.assertTrue(False, "探针：伪造门禁的头提交，真实门禁必须判红")


if __name__ == "__main__":
    unittest.main()
