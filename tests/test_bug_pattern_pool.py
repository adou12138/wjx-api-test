# -*- coding: utf-8 -*-
"""缺陷模式池 bug_pattern_pool（v4.27.0）测试。

锁住：① 从 report_full.json 的 findings 抽取；② --merge 累积 + 指纹去重；
③ 幂等（同一来源重复合并不改变池、不重复计 hits）；④ 跨来源命中累加 hits；
⑤ severity 规整兼容 P 级与中文 高/中/低；⑥ --rank 按等级+置信度排序。
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import bug_pattern_pool as B  # noqa: E402


def _report(findings):
    return {"results": [], "findings": findings, "coverage_plan": []}


def _write(path, findings):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_report(findings), f, ensure_ascii=False)


class TestExtractAndSeverity(unittest.TestCase):
    def test_extract_findings_key(self):
        self.assertEqual(len(B.extract_items(_report([{"dimension": "B", "msg": "x"}]))), 1)

    def test_extract_bare_list(self):
        self.assertEqual(len(B.extract_items([{"a": 1}, "notdict"])), 1)

    def test_results_not_a_defect_source(self):
        """results 是逐用例执行结果（含 PASS），不是缺陷——不得当缺陷源（v4.29.0 审查修复）。"""
        # findings 为空时应返回空，不回落到 results
        self.assertEqual(B.extract_items({"results": [{"dimension": "B", "status": "PASS"}],
                                          "findings": []}), [])
        # 连 findings 都没有、只有 results 时也不提取
        self.assertEqual(B.extract_items({"results": [{"dimension": "B"}]}), [])

    def test_severity_normalization(self):
        self.assertEqual(B.severity_of({"severity": "P0"}), "P0")
        self.assertEqual(B.severity_of({"severity": "高"}), "P1")
        self.assertEqual(B.severity_of({"severity": "中"}), "P2")
        self.assertEqual(B.severity_of({"severity": "低"}), "P3")
        self.assertEqual(B.severity_of({}), "P2")


class TestMerge(unittest.TestCase):
    def test_accumulate_and_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            pool = os.path.join(d, "pool.json")
            rpt = os.path.join(d, "r1_report_full.json")
            _write(rpt, [{"dimension": "B", "category": "参数校验-类型", "severity": "P1", "msg": "类型错误"},
                         {"dimension": "K", "category": "响应", "severity": "中", "msg": "缺字段"}])
            B.cmd_merge(pool, rpt)
            p1 = B.load_pool(pool)
            self.assertEqual(len(p1["patterns"]), 2)
            B.cmd_merge(pool, rpt)   # 同一来源再合并 → 幂等
            p2 = B.load_pool(pool)
            self.assertEqual(len(p2["patterns"]), 2)
            self.assertEqual(sorted(p["hits"] for p in p2["patterns"]), [1, 1],
                             "同一来源不得重复计 hits")

    def test_hits_accumulate_across_sources(self):
        with tempfile.TemporaryDirectory() as d:
            pool = os.path.join(d, "pool.json")
            r1 = os.path.join(d, "r1_report_full.json")
            r2 = os.path.join(d, "r2_report_full.json")
            finding = {"dimension": "B", "category": "c", "severity": "P1", "msg": "same symptom"}
            _write(r1, [finding])
            _write(r2, [dict(finding)])
            B.cmd_merge(pool, r1)
            B.cmd_merge(pool, r2)
            p = B.load_pool(pool)
            self.assertEqual(len(p["patterns"]), 1)
            self.assertEqual(p["patterns"][0]["hits"], 2, "两个不同来源命中应累加 hits")

    def test_severity_escalates_not_deescalates(self):
        with tempfile.TemporaryDirectory() as d:
            pool = os.path.join(d, "pool.json")
            r1 = os.path.join(d, "r1_report_full.json")
            r2 = os.path.join(d, "r2_report_full.json")
            _write(r1, [{"dimension": "B", "category": "c", "severity": "P2", "msg": "s"}])
            _write(r2, [{"dimension": "B", "category": "c", "severity": "P0", "msg": "s"}])
            B.cmd_merge(pool, r1)
            B.cmd_merge(pool, r2)
            self.assertEqual(B.load_pool(pool)["patterns"][0]["severity"], "P0", "等级就高")


class TestRank(unittest.TestCase):
    def test_rank_orders_by_severity_then_confidence(self):
        with tempfile.TemporaryDirectory() as d:
            rpt = os.path.join(d, "r_report_full.json")
            _write(rpt, [{"dimension": "K", "severity": "低", "msg": "low", "confidence": 0.9},
                         {"dimension": "B", "severity": "P0", "msg": "crit", "confidence": 0.5}])
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                B.cmd_rank(rpt, 8, None)
            out = buf.getvalue()
            self.assertLess(out.index("crit"), out.index("low"), "P0 应排在低等级之前")


if __name__ == "__main__":
    unittest.main()
