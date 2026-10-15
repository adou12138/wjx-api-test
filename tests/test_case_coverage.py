#!/usr/bin/env python3
"""用例执行率闭环契约测试（wjx-api-test v4.35.0）。

注意：既有 `coverage_gate()` **已经**按 EXECUTED_STATUSES 过滤（只有 PASS/FAIL/PRECONDITION_UNMET 算触及，
全被跳过的维度会记「假覆盖」缺陷）。本模块测的是它的增量部分：逐条对账、reason 必填、
类别级对账，以及把「假覆盖」从 stdout 提示升级为可落盘/可门禁的产物。

锁定的不变式（每条都对应方案里的一个缺口）：
1. 逐条对账：results.case_no 与 plan.id 对齐，缺失的进 silentLost（纯增量，既有门禁只做维度级）；
2. SKIPPED / N/A 从「已执行」里剥出并落成产物（判定口径与既有门禁一致，产物是增量）；
3. **把「只靠 SKIPPED 撑着已触及」的维度落进产物**（既有门禁只 stdout 打印、且是维度级）；
4. 未写 reason 的未执行项进 unexplained（门禁拦截，沿用"原因必填"纪律）；
5. 类别对账：声明的类别必须**至少有一条真实执行**才算覆盖（纯增量：既有门禁看不到整类缺席）；
6. 豁免必须显式且单列，不进 missing、不进分子也不进分母；
7. 计划外 id（results 里出现 plan 之外的）只告警不阻塞；
8. enforce 的三种退出码；
9. **零行为变更**：本模块不碰 case_runner / coverage_gate，既有门禁结论不受影响
   （用一条真实 report 的 findings/dimension 计算做前后对照）。
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

import case_coverage as CC  # noqa: E402


def case(cid, dim="B", category="业务-核心流程", **kw):
    c = {"id": cid, "name": cid + " 用例", "dimension": dim, "category": category,
         "enabled": kw.pop("enabled", True), "needs": kw.pop("needs", ""),
         "applicability": "适用", "tier": "probe", "source": "generator:test"}
    c.update(kw)
    return c


def res(cid, status="PASS", dim="B", reason=None, category="业务-核心流程"):
    r = {"case_no": cid, "name": cid + " 用例", "category": category,
         "dimension": dim, "action": "run", "http_code": 200,
         "status": status, "reason": reason if reason is not None else ""}
    return r


PLAN = {"action": "1000107", "name": "测试接口", "coverage_plan": ["B"],
        "cases": [
            case("B-LEN-OVER-title"), case("B-TYPE-age"), case("B-MISS-name"),
            case("E-01", dim="E"), case("AF1-01", dim="AF1", category="业务场景-主业务流"),
        ]}
RESULTS = {"results": [res("B-LEN-OVER-title"), res("B-TYPE-age", status="FAIL"),
                       res("B-MISS-name", status="SKIPPED", reason="未启用（needs=manual）"),
                       res("E-01", status="SKIPPED", dim="E", reason="缺配置"),
                       res("AF1-01", status="SKIPPED", dim="AF1", reason="未启用（AF 未合并）")],
           "findings": []}


class CaseCoverageTests(unittest.TestCase):
    def test_full_execution_rate(self):
        cov = CC.compute(PLAN, RESULTS["results"], [], [])
        self.assertEqual(cov["planned"]["total"], 5)
        self.assertEqual(cov["executed"]["total"], 2)
        self.assertEqual(cov["executed"]["rate"], 0.4)
        self.assertEqual(cov["notExecuted"]["total"], 3)
        self.assertEqual(cov["silentLost"], [])
        self.assertEqual(cov["unknownResults"], [])

    def test_silent_lost_detected(self):
        """纯增量：计划里有、results 里没有 → 静默丢失必须被点名（既有门禁不做逐条对账）。"""
        results = [r for r in RESULTS["results"] if r["case_no"] != "E-01"]
        cov = CC.compute(PLAN, results, [], [])
        self.assertEqual(cov["silentLost"], ["E-01"])
        self.assertEqual(cov["executed"]["total"], 2)
        self.assertEqual(cov["planned"]["total"], 5)

    def test_skipped_not_counted_as_executed(self):
        """与既有门禁一致的口径：SKIPPED / N/A 都不算「已执行」，只有 PASS/FAIL 算真判定。"""
        results = [res("B-LEN-OVER-title"), res("B-TYPE-age", status="N/A", reason="不适用: 缺参数")]
        cov = CC.compute(PLAN, results, [], [])
        self.assertEqual(cov["executed"]["total"], 1)
        self.assertEqual(cov["notExecuted"]["total"], 4)
        # 宽松口径（比 coverage_gate 更宽：SKIPPED 也计入触及，而门禁将其标为「假覆盖」）单列，便于对照差异
        self.assertEqual(cov["executed"]["looseTotal"], 2)

    def test_skipped_only_dimensions_named(self):
        """核心断言：只靠 SKIPPED 撑着已触及的维度必须落进产物（既有门禁只在 stdout 打印）。"""
        results = [res("B-LEN-OVER-title"), res("B-TYPE-age"),
                   res("E-01", status="SKIPPED", dim="E", reason="缺配置"),
                   res("AF1-01", status="SKIPPED", dim="AF1", reason="未启用")]
        cov = CC.compute(PLAN, results, [], [])
        self.assertEqual(sorted(cov["skippedOnlyDimensions"]), ["AF1", "E"])
        self.assertNotIn("B", cov["skippedOnlyDimensions"])

    def test_unexplained_when_no_reason(self):
        results = [res("B-LEN-OVER-title"), res("E-01", status="SKIPPED", dim="E", reason="")]
        cov = CC.compute(PLAN, results, [], [])
        self.assertTrue(any("未写明原因" in x for x in cov["notExecuted"]["unexplained"]),
                        cov["notExecuted"]["unexplained"])

    def test_category_covered_requires_real_execution(self):
        """纯增量：声明 AF 时，AF 用例全 SKIPPED 不算覆盖；有一条真跑过才算覆盖。"""
        cov_skipped = CC.compute(PLAN, RESULTS["results"], ["AF"], [])
        self.assertIn("AF", cov_skipped["categories"]["missing"])
        # 计划里有两条 AF，其中 AF1-02 真跑过 → 覆盖
        plan2 = json.loads(json.dumps(PLAN))
        plan2["cases"].append(case("AF1-02", dim="AF1", category="业务场景-主业务流"))
        results = list(RESULTS["results"]) + [res("AF1-02", dim="AF1", category="业务场景-主业务流")]
        cov_ran = CC.compute(plan2, results, ["AF"], [])
        self.assertNotIn("AF", cov_ran["categories"]["missing"])
        self.assertIn("AF", cov_ran["categories"]["covered"])

    def test_exempt_category_listed_not_missing(self):
        cov = CC.compute(PLAN, RESULTS["results"], ["AF"], ["AF:本轮未启用大模型链路"])
        self.assertNotIn("AF", cov["categories"]["missing"])
        self.assertEqual(cov["categories"]["exempt"], [{"name": "AF", "reason": "本轮未启用大模型链路"}])

    def test_unknown_results_only_warned(self):
        results = list(RESULTS["results"]) + [res("ZZ-99", dim="Z")]
        cov = CC.compute(PLAN, results, [], [])
        self.assertEqual(cov["unknownResults"], ["ZZ-99"])

    def test_rerun_takes_executed_record_over_skipped(self):
        """回归（审查抓到）：results 是纯 append 列表、无覆盖机制（case_runner 每次执行都
        append），所以同一用例多条记录是**正常产物**（重跑/回归并进）。原实现用
        setdefault 取首条 → 「首轮 SKIPPED、重跑 PASS」的用例被记成未执行，
        且因首条带 reason 而**静默**（不落 unexplained），与同批 v4.29.0 的
        detect_flaky（按多条记录判不稳）口径直接矛盾。"""
        results = [res("B-TYPE-age", status="SKIPPED", reason="首轮未启用")]
        results += [res("B-TYPE-age", status="PASS")]
        results += [res("B-LEN-OVER-title", status="PASS")]
        cov = CC.compute(PLAN, results, [], [])
        self.assertIn("B-TYPE-age", cov["executed"]["caseIds"],
                      "重跑通过的用例必须算已执行（真判定优先于跳过）")
        self.assertNotIn("B-TYPE-age", cov["silentLost"])
        self.assertEqual(cov["silentLost"], ["AF1-01", "B-MISS-name", "E-01"],
                         "未给结果的用例进静默丢失（与重跑情形无关）")
        self.assertIn("B-TYPE-age", cov["multipleRecords"]["caseIds"],
                      "多记录情况必须落进产物，不静默")

    def test_multiple_records_flaky_is_named(self):
        """多记录且终态不一致 = 不稳定，应被点名（与 detect_flaky 同口径）。"""
        results = [res("B-TYPE-age", status="PASS"),
                   res("B-TYPE-age", status="FAIL"),
                   res("B-LEN-OVER-title", status="PASS")]
        cov = CC.compute(PLAN, results, [], [])
        self.assertIn("B-TYPE-age", cov["multipleRecords"]["flakyCaseIds"],
                      "同用例终态不一致必须点名为不稳定")
        self.assertNotIn("B-LEN-OVER-title", cov["multipleRecords"]["flakyCaseIds"])

    def test_enforce_exit_codes(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            plan_f = tmp / "cases.json"
            plan_f.write_text(json.dumps(PLAN, ensure_ascii=False), encoding="utf-8")
            res_f = tmp / "report_full.json"
            out_f = tmp / "cov.json"

            # ① SKIPPED 没写 reason → 失败
            res_f.write_text(json.dumps({"results": [res("E-01", status="SKIPPED", dim="E", reason="")]},
                                        ensure_ascii=False), encoding="utf-8")
            r = subprocess.run([sys.executable, str(SCRIPTS / "case_coverage.py"),
                                "--plan", str(plan_f), "--results", str(res_f), "--enforce"],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 1, r.stdout[-200:])
            self.assertIn("未写明原因", r.stderr)

            # ② 执行率低于下限 → 失败
            res_f.write_text(json.dumps({"results": [res("B-LEN-OVER-title")]}, ensure_ascii=False),
                             encoding="utf-8")
            r = subprocess.run([sys.executable, str(SCRIPTS / "case_coverage.py"),
                                "--plan", str(plan_f), "--results", str(res_f),
                                "--enforce", "--min-rate", "0.9"], capture_output=True, text=True)
            self.assertEqual(r.returncode, 1)
            self.assertIn("低于下限", r.stderr)

            # ③ 声明类别缺席且未豁免 → 失败
            res_f.write_text(json.dumps({"results": [res("B-LEN-OVER-title"), res("B-TYPE-age"),
                                                      res("B-MISS-name"), res("E-01", dim="E")]},
                                        ensure_ascii=False), encoding="utf-8")
            r = subprocess.run([sys.executable, str(SCRIPTS / "case_coverage.py"),
                                "--plan", str(plan_f), "--results", str(res_f),
                                "--expect-categories", "AF", "--enforce"], capture_output=True, text=True)
            self.assertEqual(r.returncode, 1)
            self.assertIn("类别", r.stderr)

            # ④ 全 PASS + 豁免齐全 → 通过，产物落盘
            res_f.write_text(json.dumps({"results": [res(c["id"], dim=c["dimension"]) for c in PLAN["cases"]]},
                                        ensure_ascii=False), encoding="utf-8")
            r = subprocess.run([sys.executable, str(SCRIPTS / "case_coverage.py"),
                                "--plan", str(plan_f), "--results", str(res_f), "--out", str(out_f),
                                "--expect-categories", "AF", "--enforce", "--min-rate", "1.0"],
                              capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            cov = json.loads(out_f.read_text(encoding="utf-8"))
            self.assertEqual(cov["executed"]["rate"], 1.0)

    def test_zero_impact_on_existing_gates(self):
        """零行为变更：既有 coverage_gate 的口径把"有 result"就算触及，本模块只点名差异。

        对照关系：宽松口径 looseTotal == 计划内有任何 result 的用例数（含 SKIPPED/N-A），
        严格口径 executed.total 只数 PASS/FAIL；两者差额由 skippedOnlyDimensions 点名。
        既有门禁仍按宽松口径判定，本模块不改它——只把差额摆到明面上。
        """
        results = RESULTS["results"]
        cov = CC.compute(PLAN, results, [], [])
        self.assertEqual(cov["executed"]["looseTotal"], 5, "宽松口径：计划内 5 条都有 result")
        self.assertEqual(cov["executed"]["total"], 2, "严格口径：只有 PASS/FAIL 算真判定")
        self.assertEqual(sorted(cov["skippedOnlyDimensions"]), ["AF1", "E"],
                         "只靠 SKIPPED 撑着的维度必须被点名")
        self.assertEqual(set(cov["executed"]["byDimension"]), {"B"})
        self.assertIn("既有门禁结论不受影响", cov["note"])


if __name__ == "__main__":
    unittest.main()