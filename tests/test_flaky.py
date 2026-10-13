# -*- coding: utf-8 -*-
"""稳定性检测 detect_flaky（v4.29.0）：同一用例多次记录且终态不一致才判"不稳定"。

锁住：① 单次结果不判；② 终态一致不判；③ 终态不一致才点名；④ 忽略缺 id/status 的畸形记录。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import attribution_engine as A  # noqa: E402


class TestDetectFlaky(unittest.TestCase):
    def test_single_run_not_flagged(self):
        self.assertEqual(A.detect_flaky([{"case_no": "T1", "status": "PASS"},
                                         {"case_no": "T2", "status": "FAIL"}]), {})

    def test_consistent_not_flagged(self):
        self.assertEqual(A.detect_flaky([{"case_no": "T1", "status": "PASS"},
                                         {"case_no": "T1", "status": "PASS"}]), {})

    def test_inconsistent_flagged(self):
        r = A.detect_flaky([{"case_no": "T1", "status": "PASS"},
                            {"case_no": "T1", "status": "FAIL"}])
        self.assertEqual(r, {"T1": ["FAIL", "PASS"]})

    def test_ignores_malformed(self):
        self.assertEqual(A.detect_flaky([{"status": "PASS"}, "x",
                                         {"case_no": "T1"},
                                         {"id": "T2", "status": "ERROR"},
                                         {"id": "T2", "status": "ERROR"}]), {})


if __name__ == "__main__":
    unittest.main()
