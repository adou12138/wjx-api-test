# -*- coding: utf-8 -*-
"""P2 闭环能力验证：回归基线快照/复测判定 + 缺陷回流候选池。"""
import sys, csv, tempfile, unittest
from pathlib import Path

SKILL = Path(r"D:/TestCase/MySharedSkills/wjx-api-test")
sys.path.insert(0, str(SKILL / "scripts"))

import regression
import pattern_feedback


class TestRegressionBaseline(unittest.TestCase):
    def test_baseline_round_trip(self):
        p = Path(self.enterContext(tempfile.TemporaryDirectory())) / "baseline.json"
        regression.save_baseline(p, [
            {"case_id": "C1", "contract_hash": "h1", "expectations": {"last_error": ""}},
        ])
        base = regression.load_baseline(p)
        self.assertIn("C1", base)
        self.assertEqual(base["C1"]["contract_hash"], "h1")

    def test_retest_verdicts(self):
        base = {
            "C1": {"contract_hash": "h", "expectations": {"last_error": "旧错误"}},
            "C2": {"contract_hash": "h", "expectations": {"last_error": "旧错误"}},
            "C3": {"contract_hash": "h", "expectations": {"last_error": "旧错误"}},
            "C4": {"contract_hash": "old", "expectations": {"last_error": "x"}},
        }
        results = [
            {"case_id": "C1", "passed": True, "error": ""},
            {"case_id": "C2", "passed": False, "error": "旧错误"},
            {"case_id": "C3", "passed": False, "error": "新的错误"},
            {"case_id": "C4", "passed": True, "error": ""},
            {"case_id": "C5", "passed": False, "error": "boom"},
            {"case_id": "C6", "passed": False, "error": "connection timeout"},
        ]
        verdicts = {v["case_id"]: v["verdict"]
                    for v in regression.retest_verdicts(base, results, current_contract_hash="h")}
        self.assertEqual(verdicts["C1"], "FIXED")
        self.assertEqual(verdicts["C2"], "STILL_FAILING")
        self.assertEqual(verdicts["C3"], "REGRESSED")
        self.assertEqual(verdicts["C4"], "STALE_BASELINE")
        self.assertEqual(verdicts["C5"], "UNBASELINED")
        self.assertEqual(verdicts["C6"], "BLOCKED_ENV")


class TestPatternFeedback(unittest.TestCase):
    def _pool(self):
        return Path(self.enterContext(tempfile.TemporaryDirectory())) / "pattern_candidates.md"

    def test_mine_only_defect_rows(self):
        p = Path(self.enterContext(tempfile.TemporaryDirectory())) / "issues.csv"
        with open(p, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["编号", "类型", "维度", "问题描述"])
            w.writeheader()
            w.writerow({"编号": "1", "类型": "缺陷", "维度": "B", "问题描述": "可选字段传空被拒"})
            w.writerow({"编号": "2", "类型": "环境", "维度": "B", "问题描述": "网络超时"})
        items = pattern_feedback.mine_from_csv(str(p))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["count"], 1)

    def test_append_candidates_writes_pool(self):
        pool = self._pool()
        orig = pattern_feedback.CANDIDATES
        pattern_feedback.CANDIDATES = str(pool)
        try:
            n = pattern_feedback.append_candidates(
                [{"dim": "B", "symptom": "可选字段传空被拒绝", "count": 2, "evidence": "SMK-001"}])
            self.assertEqual(n, 1)
            text = pool.read_text(encoding="utf-8")
            self.assertIn("可选字段传空被拒绝", text)
            self.assertIn("待评审", text)
            # 幂等：同一现象重复登记不新增
            n2 = pattern_feedback.append_candidates(
                [{"dim": "B", "symptom": "可选字段传空被拒绝", "count": 1, "evidence": "SMK-001"}])
            self.assertEqual(n2, 0)
        finally:
            pattern_feedback.CANDIDATES = orig


if __name__ == "__main__":
    unittest.main()
