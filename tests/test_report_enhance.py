# -*- coding: utf-8 -*-
"""report_enhance 单测：把问题台账变成"可直接提单"的形态。

覆盖：描述归一化（聚类键）、复现命令生成、聚类去重、严重程度排序、
修复建议挂维度、以及增强 CSV / MD 两份产物。fixture 走真实写盘路径
（api_test_runner.finding_row + 标准 csv.writer），与回读校验同一套口径。
"""
import csv
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402
import api_test_runner as R     # noqa: E402
import report_enhance as RE     # noqa: E402

COLS = list(R.FINDING_CSV_COLS)


def _finding(fid, dimension, severity, desc, **over):
    d = {"id": fid, "kind": "缺陷", "severity": severity, "status": "待修复",
         "dimension": dimension, "desc": desc,
         "request": '{"encode":"json","appid":3585,"action":"1000107","sign":"***",'
                    '"ts":1757000000,"title":"x"}',
         "expected": "应被拒绝", "actual": "HTTP 200 result=true",
         "confidence": 0.9, "http_code": 200, "request_id": "req-1",
         "trace_id": "trace-1", "evidence_path": "evidence/E01.json",
         "attribution": "DEFECT", "retest_status": "待复测"}
    d.update(over)
    return d


class TestNorm(unittest.TestCase):
    def test_digits_and_brackets_and_punct_normalized(self):
        a = RE._norm("[F1] 写入超长标题123后接口仍返回成功")
        b = RE._norm("[F2] 写入超长标题456后接口仍返回成功")
        self.assertEqual(a, b)
        self.assertNotIn("123", a)
        self.assertNotIn("[", a)

    def test_different_phenomena_not_merged(self):
        self.assertNotEqual(RE._norm("越权访问返回成功"), RE._norm("写后不落库"))


class TestCurl(unittest.TestCase):
    def test_includes_url_header_and_escaped_quotes(self):
        c = RE._curl('{"a": 1}', base_url="https://env.sojump.cn/api")
        self.assertIn("curl -X POST", c)
        self.assertIn("Content-Type: application/json", c)
        self.assertIn("https://env.sojump.cn/api", c)
        self.assertIn('\\"a\\"', c)

    def test_dict_body_serialized(self):
        c = RE._curl({"a": 1, "b": "汉字"})
        self.assertIn("汉字", c)


class TestEnhance(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.d = self._td.name
        self.csv_path = os.path.join(self.d, "api_test_issues.csv")
        rows = [
            _finding("F1", "A", "中", "越权访问返回成功"),
            _finding("F2", "B8", "低", "写入超长标题111后接口仍返回成功"),
            _finding("F3", "B8", "高", "写入超长标题222后接口仍返回成功"),  # 与 F2 同族
            _finding("F4", "C", "中", "写后不落库"),
        ]
        with open(self.csv_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(COLS)
            for x in rows:
                w.writerow(R.finding_row(x))
        self.out = os.path.join(self.d, "enhanced")

    def tearDown(self):
        self._td.cleanup()

    def _enhanced(self):
        RE.enhance(self.csv_path, self.out)
        with open("%s.csv" % self.out, "r", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))

    def test_clusters_dedup_by_dim_and_norm_desc(self):
        rows = self._enhanced()
        # 4 条原始 → 3 类（F2/F3 合并）
        self.assertEqual(len(rows), 3)
        b8 = [r for r in rows if r["维度"] == "B8"][0]
        self.assertEqual(b8["出现次数"], "2")
        self.assertIn("F2", b8["问题单号"])
        self.assertIn("F3", b8["问题单号"])

    def test_severity_ordering_high_first(self):
        rows = self._enhanced()
        self.assertEqual(rows[0]["维度"], "B8")
        self.assertEqual(rows[0]["严重程度"], "高")   # 族内取最高严重程度

    def test_fix_suggestion_by_dimension_with_fallback(self):
        rows = self._enhanced()
        by_dim = {r["维度"]: r for r in rows}
        self.assertIn("参数校验", by_dim["B8"]["修复建议"])
        self.assertIn("鉴权", by_dim["A"]["修复建议"])
        # C 有映射
        self.assertIn("真值", by_dim["C"]["修复建议"])

    def test_outputs_csv_and_md(self):
        RE.enhance(self.csv_path, self.out)
        self.assertTrue(os.path.isfile("%s.csv" % self.out))
        self.assertTrue(os.path.isfile("%s.md" % self.out))
        with open("%s.md" % self.out, "r", encoding="utf-8") as f:
            md = f.read()
        self.assertIn("复现命令", md)
        self.assertIn("curl -X POST", md)

    def test_missing_csv_no_crash(self):
        RE.enhance(os.path.join(self.d, "nope.csv"), self.out)  # 不应抛异常
        self.assertFalse(os.path.isfile("%s.csv" % self.out))


if __name__ == "__main__":
    unittest.main()
