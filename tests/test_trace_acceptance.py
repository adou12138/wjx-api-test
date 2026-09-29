# -*- coding: utf-8 -*-
"""TRACE 评测复评三条件（可执行验收）。

评测报告给出的三条复评条件，逐条固化为测试，防止修复被后续改动悄悄回退：
  1. `coverage_gate()` 在存在 SKIPPED/N/A 的必测维度时**必须**产出覆盖率缺口缺陷；
  2. AB 维度在**有基线**时必须产生实际执行记录，**无基线**时必须产出覆盖缺口；
  3. `smoke_results_*.csv` 的请求体列中 `sign` 值必须是打码状态。
"""
import csv
import io
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))


def _reset():
    import api_test_runner as R
    R.results.clear()
    R.findings.clear()
    R.COVERAGE_PLAN = []
    R.coverage_blocked = False
    if hasattr(R, "shallow_passes"):
        R.shallow_passes = 0
    return R


def _run_quiet(fn):
    buf, old = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        fn()
    finally:
        sys.stdout = old
    return buf.getvalue()


# ---------------------------------------------------------------- 条件 1
class Condition1CoverageGateGap(unittest.TestCase):
    """声明必测但只有 SKIPPED/N/A 的维度，必须被记为覆盖率缺口，不得算作已覆盖。"""

    def _gate(self, cases):
        import case_runner as CR
        R = _reset()
        orig = R.call
        R.call = lambda payload, retries=2: (200, '{"result": true}',
                                             {"result": True, "data": {"vid": "1"}})
        try:
            for c in cases:
                CR.exec_case(c, dry=False)
            out = _run_quiet(R.coverage_gate)
        finally:
            R.call = orig
        gaps = [f for f in R.findings
                if str(f.get("id", "")).startswith("COV-")
                or "覆盖率缺口" in str(f.get("desc", ""))]
        return out, gaps

    def test_skipped_must_be_skipped_dimension_is_a_gap(self):
        _out, gaps = self._gate([
            {"id": "B1", "name": "已执行", "dimension": "B", "action": "1000107",
             "params": {"title": "T"}, "check": "none", "expect": {"success": True}},
            {"id": "AB1", "name": "永不执行", "dimension": "AB", "action": "1000107",
             "params": {"title": "T"}, "check": "ui_parity", "needs": "ui_baseline",
             "expect": {"success": True}},
        ])
        dims = [f.get("dimension") for f in gaps]
        self.assertIn("AB", dims, "AB 只有 N/A 记录，门禁必须判为覆盖率缺口：%s" % gaps)

    def test_na_is_not_executed(self):
        """N/A 不能被算作"测过了"——否则"没测"会被伪装成"测了"。"""
        import api_test_runner as R
        R = _reset()
        R.results.append({"case_no": "K1", "name": "x", "status": "N/A",
                          "dimension": "K", "http_code": 0, "response": {}})
        R.COVERAGE_PLAN = ["K"]
        out = _run_quiet(R.coverage_gate)
        self.assertIn("K", out)
        self.assertTrue(any(str(f.get("id", "")).startswith("COV-") for f in R.findings),
                        "N/A 维度应产出覆盖率缺口")

    def test_pass_is_counted_as_executed(self):
        """反向对照：真正执行过的维度不得被误报为缺口（防门禁变成假警报源）。"""
        import api_test_runner as R
        R = _reset()
        R.results.append({"case_no": "B1", "name": "x", "status": "PASS",
                          "dimension": "B", "http_code": 200, "response": {}})
        R.COVERAGE_PLAN = ["B"]
        _run_quiet(R.coverage_gate)
        self.assertFalse([f for f in R.findings if str(f.get("id", "")).startswith("COV-")],
                         "PASS 过的维度不得报缺口")


# ---------------------------------------------------------------- 条件 2
class Condition2AbDimension(unittest.TestCase):
    """AB 维度：有基线执行、无基线记缺口，二分语义必须成立。"""

    def _case(self, path=None):
        c = {"id": "AB01", "name": "页面口径", "dimension": "AB", "action": "1000107",
             "params": {"title": "T"}, "check": "ui_parity", "needs": "ui_baseline",
             "expect": {"success": True}}
        if path:
            c["check_args"] = {"baseline_path": path}
        return c

    def _exec(self, case, api_value="页面标题"):
        import case_runner as CR
        import api_test_runner as R
        R = _reset()
        orig = R.call
        R.call = lambda payload, retries=2: (200, json.dumps({"result": True}),
                                             {"result": True, "data": {"title": api_value}})
        try:
            CR.exec_case(case, dry=False)
        finally:
            R.call = orig
        return R, [r for r in R.results if r.get("status") in ("PASS", "FAIL")]

    def test_with_baseline_ab_actually_executes(self):
        d = tempfile.mkdtemp()
        bl = os.path.join(d, "ui_baseline.json")
        open(bl, "w", encoding="utf-8").write(json.dumps(
            {"baselines": [{"field": "title", "action": "1000107",
                            "page_value": "页面标题"}]}, ensure_ascii=False))
        R, executed = self._exec(self._case(bl))
        self.assertTrue(executed, "有基线时 AB 必须真正执行（不得仍被跳过）")
        self.assertFalse([f for f in R.findings
                          if str(f.get("id", "")).startswith("COV-")], "有基线不该报缺口")

    def test_with_baseline_mismatch_is_a_defect(self):
        d = tempfile.mkdtemp()
        bl = os.path.join(d, "ui_baseline.json")
        open(bl, "w", encoding="utf-8").write(json.dumps(
            {"baselines": [{"field": "title", "action": "1000107",
                            "page_value": "页面标题"}]}, ensure_ascii=False))
        R, _executed = self._exec(self._case(bl), api_value="接口标题")
        hits = [f for f in R.findings if "口径不一致" in str(f.get("desc", ""))]
        self.assertTrue(hits, "接口值与页面基线不一致必须记缺陷")

    def test_without_baseline_is_a_coverage_gap(self):
        R, executed = self._exec(self._case(
            os.path.join(tempfile.mkdtemp(), "missing.json")))
        self.assertFalse(executed, "无基线时不得凭空编预期值执行断言")
        gaps = [f for f in R.findings if str(f.get("id", "")).startswith("COV-")]
        self.assertTrue(gaps, "无基线必须记覆盖率缺口，不得静默跳过")


# ---------------------------------------------------------------- 条件 3
class Condition3SmokeArtifactsRedacted(unittest.TestCase):
    """冒烟主产物的请求体列中，sign 等敏感值必须是打码状态。"""

    def _run_smoke(self, extra=None):
        import smoke_runner
        d = tempfile.mkdtemp()
        cfg = {"name": "acc_env", "base_url": "https://x.test/openapi/{endpoint}",
               "common_params": {"encode": "sha1", "appid": "3585",
                                 "sign": "REALSIGNVALUE123"},
               "account": {"main": "acc"}, "teardown_enabled": False}
        cases = {"cases": [{"action": "1000107", "name": "创建", "params": {"title": "T"}}]}
        cp, cc = os.path.join(d, "cfg.json"), os.path.join(d, "cases.json")
        open(cp, "w", encoding="utf-8").write(json.dumps(cfg, ensure_ascii=False))
        open(cc, "w", encoding="utf-8").write(json.dumps(cases, ensure_ascii=False))
        out = os.path.join(d, "out")
        argv = sys.argv[:]
        sys.argv = ["smoke_runner.py", "--config", cp, "--cases", cc,
                    "--out-dir", out, "--dry-run"] + (extra or [])
        try:
            _run_quiet(smoke_runner.main)
        finally:
            sys.argv = argv
        return out

    def test_sign_is_masked_in_results_csv(self):
        out = self._run_smoke()
        files = [f for f in os.listdir(out) if f.startswith("smoke_results_")]
        self.assertTrue(files, "缺少冒烟结果 CSV")
        text = open(os.path.join(out, files[0]), encoding="utf-8-sig").read()
        self.assertIn("sign", text, "请求体列应保留键名（结构可读）")
        self.assertNotIn("REALSIGNVALUE123", text, "sign 明文不得落盘")
        self.assertIn("***", text, "敏感值应打码")

    def test_results_csv_keeps_full_request_body(self):
        """脱敏只打码值、不删字段——开发仍需看到完整报文结构。"""
        out = self._run_smoke()
        files = [f for f in os.listdir(out) if f.startswith("smoke_results_")]
        rows = list(csv.DictReader(open(os.path.join(out, files[0]), encoding="utf-8-sig")))
        self.assertTrue(rows)
        body = " ".join(str(v) for v in rows[0].values())
        for key in ("encode", "appid", "action"):
            self.assertIn(key, body, "公共参数 %s 不得因脱敏被删" % key)
        self.assertNotIn("REALSIGNVALUE123", body, "sign 明文不得落盘")
        self.assertNotIn("SIGNVALUE123", body, "sign 片段同样不得落盘（截断式泄露）")


# ---------------------------------------------------------------- 条件 4
class Condition4SkipLeavesTrace(unittest.TestCase):
    """跳过必须留痕：同维度里有别的用例跑过，不得掩盖"这个检查从没跑过"。"""

    def _exec_all(self, cases):
        import case_runner as CR
        R = _reset()
        orig = R.call
        R.call = lambda payload, retries=2: (200, '{"result": true}',
                                             {"result": True, "data": {}})
        try:
            for c in cases:
                CR.exec_case(c, dry=False)
        finally:
            R.call = orig
        return R

    def _base(self, cid, **kw):
        c = {"id": cid, "name": cid, "dimension": "D", "action": "1000107",
             "params": {"title": "T"}, "expect": {"success": True}}
        c.update(kw)
        return c

    def test_manual_check_skip_records_coverage_gap(self):
        R = self._exec_all([
            self._base("D1", check="none"),
            self._base("D2", name="时区口径", check="timezone"),
        ])
        gaps = [f for f in R.findings if str(f.get("id", "")).startswith("COV-")]
        self.assertTrue(gaps, "被 MANUAL_CHECKS 跳过的检查必须记覆盖缺口——"
                              "否则同维度里有别的用例跑过，它就彻底静默了")
        self.assertEqual(gaps[0].get("dimension"), "D")

    def test_needs_manual_skip_records_coverage_gap(self):
        R = self._exec_all([
            self._base("D1", check="none"),
            self._base("D2", name="需人工核对", needs="manual"),
        ])
        gaps = [f for f in R.findings if str(f.get("id", "")).startswith("COV-")]
        self.assertTrue(gaps, "needs=manual 的跳过同样必须留痕")

    def test_disabled_case_is_not_a_gap(self):
        """反向对照：用户主动 enabled=false 是显式决定，不得报缺口（防门禁变噪音源）。"""
        R = self._exec_all([
            self._base("D1", check="none"),
            self._base("D2", name="主动禁用", enabled=False),
        ])
        gaps = [f for f in R.findings if str(f.get("id", "")).startswith("COV-")]
        self.assertFalse(gaps, "主动禁用的用例不该记覆盖缺口：%s" % gaps)


if __name__ == "__main__":
    unittest.main()
