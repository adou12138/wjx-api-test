# -*- coding: utf-8 -*-
"""TRACE 评测 P2 项回归测试：接线、判定守卫与真链路。

重点覆盖此前"没有任何测试跑通 flow --execute → case_runner → CSV"的空白——
本次评测发现的多个集成缺陷（dimension 泄漏、run() 崩溃、门禁假绿）都属于这一类。
"""
import io
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, ROOT)


def _reset():
    import api_test_runner as R
    R.results.clear()
    R.findings.clear()
    R.COVERAGE_PLAN = []
    R.coverage_blocked = False
    if hasattr(R, "shallow_passes"):
        R.shallow_passes = 0
    return R


# ------------------------------------------------- judged 守卫（P2-1）
class TestClassifyGuards(unittest.TestCase):
    """P2-1：内部错误不得被判 N/A；强歧义关键词必须从 na_keywords 移出。"""

    def _classify(self, code, parsed, cfg):
        import smoke_runner as SR
        return SR.classify(code, parsed, json.dumps(parsed, ensure_ascii=False),
                           {"action": "1"}, cfg)

    def test_http_200_internal_error_is_fail(self):
        verdict, msg = self._classify(200, {"result": False, "errmsg": "系统内部错误"},
                                      {"na_keywords": ["内部错误"]})
        self.assertEqual(verdict, "FAIL", "HTTP 200 + 内部错误仍必须判 FAIL，不得被 N/A 吞掉")

    def test_http_200_traceback_is_fail(self):
        verdict, _ = self._classify(200, {"result": False, "errmsg": "unhandled exception"}, {})
        self.assertEqual(verdict, "FAIL")

    def test_5xx_still_fail(self):
        verdict, _ = self._classify(500, {"result": False, "errmsg": "内部错误"},
                                    {"na_keywords": ["内部错误"]})
        self.assertEqual(verdict, "FAIL")

    def test_legit_na_still_na(self):
        """修复不能误伤：真正属于环境限制的文案仍应判 N/A。"""
        verdict, _ = self._classify(200, {"result": False, "errmsg": "该功能未开通"},
                                    {"na_keywords": ["未开通"]})
        self.assertEqual(verdict, "N/A")

    def test_risky_keywords_removed_from_shipped_configs(self):
        risky = ["不存在", "查询为空", "参数不能为空", "无权限", "没有权限"]
        for name in ("smoke_config.example.json", "smoke_config_wjx.json"):
            doc = json.loads(open(os.path.join(ROOT, "assets", name),
                                  encoding="utf-8").read())
            words = doc.get("na_keywords") or []
            for w in risky:
                self.assertNotIn(w, words, "%s 仍含强歧义 N/A 关键词 %s" % (name, w))


# ------------------------------------------------- 接线（P2-2/2-3）
class TestWiringIsLive(unittest.TestCase):
    """P2-2/2-3：曾被声明但从未接线/从未被生产代码调用的模块必须真正生效。"""

    def _prod_refs(self, module):
        import re
        prod = []
        for d in ("scripts", "adapters"):
            for f in os.listdir(os.path.join(ROOT, d)):
                if not f.endswith(".py") or f == module + ".py":
                    continue
                t = open(os.path.join(ROOT, d, f), encoding="utf-8").read()
                if re.search(r"(import|from)\s+.*\b%s\b" % module, t):
                    prod.append(f)
        return prod

    def test_minimizer_has_production_caller(self):
        self.assertTrue(self._prod_refs("minimizer"),
                        "minimizer 必须被生产代码调用，不能只存在于测试里")

    def test_assertion_engine_has_production_caller(self):
        self.assertTrue(self._prod_refs("assertion_engine"),
                        "assertion_engine 必须被生产代码调用")

    def test_ui_baseline_has_production_caller(self):
        self.assertTrue(self._prod_refs("ui_baseline"))

    def test_sojump_write_gate_is_reachable_from_flow(self):
        import flow
        adapter = flow.build_adapter("sojump")
        allowed, why = flow.write_gate(adapter, "sojump", {}, "创建")
        self.assertFalse(allowed, "无测试环境声明时写操作必须被拦下")
        self.assertIn("写操作门禁", why)
        allowed_ok, _ = flow.write_gate(
            adapter, "sojump",
            {"environment": {"type": "test", "write_enabled": True}}, "创建")
        self.assertTrue(allowed_ok, "显式声明测试环境后应放行")

    def test_read_op_not_blocked_by_write_gate(self):
        import flow
        adapter = flow.build_adapter("sojump")
        allowed, _ = flow.write_gate(adapter, "sojump", {}, "查询")
        self.assertTrue(allowed, "只读操作不应被写门禁拦住")

    def test_profiles_filter_cases(self):
        import flow
        cases = [{"id": "a", "priority": "P0", "enabled": True},
                 {"id": "b", "priority": "P1", "enabled": True},
                 {"id": "c", "priority": "P0", "enabled": False}]
        self.assertEqual([c["id"] for c in flow.select_by_profile(cases, "smoke")], ["a"])
        self.assertEqual([c["id"] for c in flow.select_by_profile(cases, "full")], ["a", "b"])
        self.assertEqual(len(flow.select_by_profile(cases, "exploratory")), 3)


class TestMetadataConsistency(unittest.TestCase):
    """P2-6：空值四态 / 往返用例必须带 assertion_mode，供下游正确分类。"""

    def test_null_triad_and_roundtrip_have_assertion_mode(self):
        import case_generator as G
        doc = G.generate(os.path.join(ROOT, "tests", "fixtures", "simple_contract.md"))
        for c in doc["cases"]:
            if c["id"].startswith("B-NULL-"):
                self.assertEqual(c.get("assertion_mode"), "null_triad",
                                 "B-NULL 用例缺 assertion_mode：%s" % c["id"])
            if c["id"] == "B-RT01":
                self.assertEqual(c.get("assertion_mode"), "roundtrip")

    def test_roundtrip_case_is_executable(self):
        """往返验证是历史最高发触发点，不得标成 needs=manual 而永不执行。"""
        import case_generator as G
        doc = G.generate(os.path.join(ROOT, "tests", "fixtures", "simple_contract.md"))
        rt = [c for c in doc["cases"] if c["id"] == "B-RT01"][0]
        self.assertNotEqual(rt.get("needs"), "manual", "往返验证不得被永久跳过")


# ---------------------------------------------------- 真链路（P2-4）
class TestFlowEndToEnd(unittest.TestCase):
    """P2-4：跑通 flow --execute → case_runner → CSV，锁死集成缺陷。"""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.out = os.path.join(self.d, "out")
        self.cfg = os.path.join(self.d, "exec.json")
        open(self.cfg, "w", encoding="utf-8").write(json.dumps({
            "environment": {"name": "unit_test_env", "type": "test", "write_enabled": True},
            "execution": {"profile": "smoke"},
        }, ensure_ascii=False))
        self.contract = os.path.join(ROOT, "tests", "fixtures", "simple_contract.md")

    def _run_flow(self, extra=None):
        import flow
        import api_test_runner as R
        _reset()
        orig = R.call
        R.call = lambda payload, retries=2: (200, '{"result": true}',
                                             {"result": True, "data": {"vid": "1"}})
        argv = sys.argv[:]
        sys.argv = ["flow.py", "--contract", self.contract, "--execute",
                    "--config", self.cfg, "--out-dir", self.out] + (extra or [])
        buf, old = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            flow.main()
        finally:
            sys.stdout = old
            sys.argv = argv
            R.call = orig
        return buf.getvalue()

    def test_full_chain_produces_artifacts(self):
        self._run_flow()
        for name in ("intelligent_test_plan.json", "generated_cases.json",
                     "api_test_issues.csv", "api_test_report_full.json"):
            self.assertTrue(os.path.isfile(os.path.join(self.out, name)),
                            "真链路缺少产物 %s" % name)

    def test_issues_csv_has_unified_columns_and_no_empty_evidence(self):
        import csv
        self._run_flow()
        path = os.path.join(self.out, "api_test_issues.csv")
        rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
        import case_runner as CR
        self.assertEqual(list(rows[0].keys()) if rows else CR.CSV_COLS, CR.CSV_COLS,
                         "台账列必须与统一扩展列一致")
        if rows:
            self.assertTrue(any((r.get("置信度") or "").strip() for r in rows),
                            "扩展列不得恒为空（此前 7 列永远没有值）")

    def test_config_profile_is_honored(self):
        """执行配置里的 execution.profile 必须真正生效——它曾因 --profile 写死默认值而成死配置。"""
        import flow
        import api_test_runner as R
        _reset()
        orig = R.call
        R.call = lambda payload, retries=2: (200, '{"result": true}',
                                             {"result": True, "data": {"vid": "1"}})
        argv = sys.argv[:]
        sys.argv = ["flow.py", "--contract", self.contract, "--execute",
                    "--config", self.cfg, "--out-dir", self.out]
        buf, old = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            flow.main()
        finally:
            sys.stdout = old
            sys.argv = argv
            R.call = orig
        out = buf.getvalue()
        self.assertIn("档位=smoke", out, "配置里的 profile=smoke 未被采用")
        executed = [r for r in R.results if r.get("status") in ("PASS", "FAIL")]
        skipped = [r for r in R.results if r.get("status") not in ("PASS", "FAIL")]
        self.assertTrue(executed, "smoke 档位至少应执行一条 P0 用例")
        self.assertTrue(skipped, "smoke 档位应筛掉非 P0 用例（用例总数 %d）" % len(R.results))

    def test_dimension_never_leaks_into_request(self):
        import api_test_runner as R
        sent = []
        import flow
        _reset()
        orig = R.call

        def spy(payload, retries=2):
            sent.append(dict(payload))
            return 200, '{"result": true}', {"result": True, "data": {"vid": "1"}}

        R.call = spy
        argv = sys.argv[:]
        sys.argv = ["flow.py", "--contract", self.contract, "--execute",
                    "--config", self.cfg, "--out-dir", self.out]
        buf, old = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            flow.main()
        finally:
            sys.stdout = old
            sys.argv = argv
            R.call = orig
        self.assertTrue(sent, "真链路未发出任何请求")
        for p in sent:
            self.assertNotIn("dimension", p, "dimension 泄漏进了真实请求：%s" % p)
            self.assertNotIn("category", p, "category 泄漏进了真实请求：%s" % p)

    def test_write_gate_blocks_without_test_env(self):
        import flow
        import api_test_runner as R
        _reset()
        cfg2 = os.path.join(self.d, "prod.json")
        open(cfg2, "w", encoding="utf-8").write(json.dumps({"environment": {}}))
        orig = R.call
        R.call = lambda payload, retries=2: (200, '{"result": true}', {"result": True})
        argv = sys.argv[:]
        sys.argv = ["flow.py", "--contract", self.contract, "--execute",
                    "--config", cfg2, "--out-dir", self.out]
        buf, old = io.StringIO(), sys.stdout
        err, olderr = io.StringIO(), sys.stderr
        sys.stdout, sys.stderr = buf, err
        try:
            flow.main()
        finally:
            sys.stdout, sys.stderr = old, olderr
            sys.argv = argv
            R.call = orig
        self.assertIn("写操作门禁未放行", err.getvalue())
        self.assertTrue(os.path.isfile(os.path.join(self.out, "generated_cases.json")),
                        "被门禁拦下时仍应保留已生成的用例")


class TestLedgerColumnContract(unittest.TestCase):
    """台账列口径必须**单源**：深度流程与声明式执行器导出的表头不得分叉。"""

    def test_case_runner_aliases_runner_columns(self):
        import api_test_runner as R
        import case_runner as CR
        self.assertIs(CR.CSV_COLS, R.FINDING_CSV_COLS,
                      "case_runner 必须别名引用 api_test_runner.FINDING_CSV_COLS，不得各写一份")
        self.assertEqual(len(R.FINDING_CSV_COLS), 16)

    def test_finding_row_aligned_with_columns(self):
        import api_test_runner as R
        R.findings.clear()
        R.add_finding("T-1", "缺陷", "d", {"action": "1000107", "sign": "abcd1234"},
                      expected="e", actual="a", severity="高", dimension="B")
        row = R.finding_row(R.findings[-1])
        self.assertEqual(len(row), len(R.FINDING_CSV_COLS),
                         "行长度必须与表头列数一致，否则 CSV 会错列")
        self.assertEqual(row[R.FINDING_CSV_COLS.index("维度")], "B")
        self.assertTrue(str(row[R.FINDING_CSV_COLS.index("置信度")]).strip(),
                        "扩展列（置信度）必须有值")

    def test_finding_row_appends_minimal_repro(self):
        import api_test_runner as R
        R.findings.clear()
        R.add_finding("T-2", "缺陷", "d", {"action": "1000107"}, actual="原始实际结果")
        R.findings[-1]["minimal_repro"] = "curl -X POST ..."
        row = R.finding_row(R.findings[-1])
        actual = row[R.FINDING_CSV_COLS.index("实际结果")]
        self.assertIn("原始实际结果", actual)
        self.assertIn("最小复现请求", actual)

    def test_both_writers_emit_same_header(self):
        """深度流程与声明式执行器分别落盘，表头必须逐字相同。"""
        import csv
        import io
        import tempfile
        import api_test_runner as R
        import case_runner as CR
        R.results.clear()
        R.findings.clear()
        R.add_issue("同一份台账", {"action": "1000107"}, fid="X-1")
        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                CR.write_reports(out_prefix="decl")
                shallow_header = list(csv.reader(open("decl_issues.csv", encoding="utf-8-sig")))[0]
            finally:
                os.chdir(cwd)
        self.assertEqual(shallow_header, list(R.FINDING_CSV_COLS))


class TestExecConfigIsWired(unittest.TestCase):
    """执行配置里的策略字段必须真正被消费——模板声明了却无人读取等于配置项是假的。"""

    def tearDown(self):
        import api_test_runner as R
        import case_runner as CR
        R.RETRY_COUNT = 2
        CR.MINIMIZE_ENABLED = True

    def test_policy_applied(self):
        import flow
        import api_test_runner as R
        import case_runner as CR
        flow.apply_exec_policy({"execution": {
            "retry_network_errors": 5, "minimize_findings": False,
            "max_cases": 3, "request_interval_ms": 0}})
        self.assertEqual(R.RETRY_COUNT, 5, "retry_network_errors 未落到 R.RETRY_COUNT")
        self.assertFalse(CR.MINIMIZE_ENABLED, "minimize_findings=false 未关闭最小化")

    def test_cap_cases_is_loud(self):
        import flow
        cases = [{"id": "c%d" % i} for i in range(5)]
        buf, old = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            kept = flow.cap_cases(cases, {"max_cases": 2})
        finally:
            sys.stdout = old
        self.assertEqual(len(kept), 2)
        self.assertIn("max_cases", buf.getvalue(), "截断必须明确告知，不得静默少跑")
        self.assertEqual(len(flow.cap_cases(cases, {"max_cases": 0})), 5, "0 表示不限")

    def test_run_honors_retry_count(self):
        import api_test_runner as R
        calls = []
        orig_urlopen = R.urllib.request.urlopen

        def boom(req, timeout=None, context=None):
            calls.append(1)
            raise R.urllib.error.URLError("network down")

        R.urllib.request.urlopen = boom
        R.RETRY_COUNT = 4
        try:
            code, _raw, _parsed = R.call(R.build_payload("1000107"), retries=R.RETRY_COUNT)
        finally:
            R.urllib.request.urlopen = orig_urlopen
        self.assertEqual(code, -1)
        self.assertEqual(len(calls), 5, "重试次数应为 RETRY_COUNT+1，实际 %d" % len(calls))

    def test_example_config_keys_are_all_consumed(self):
        """模板里出现的键必须全部有消费点，防止再长出"声明不接线"的假开关。"""
        import flow
        import api_test_runner as R
        import case_runner as CR

        def keys(d):
            return {k for k in d if not k.startswith("_")}

        tpl = json.load(open(os.path.join(ROOT, "assets", "intelligent_config.example.json"),
                             encoding="utf-8"))
        ex = tpl.get("execution") or {}
        flow.apply_exec_policy(tpl)
        policy = flow.apply_exec_policy(tpl)
        self.assertEqual(R.RETRY_COUNT, ex["retry_network_errors"],
                         "retry_network_errors 未接线")
        self.assertEqual(CR.MINIMIZE_ENABLED, bool(ex["minimize_findings"]),
                         "minimize_findings 未接线")
        self.assertEqual(policy["max_cases"], ex["max_cases"], "max_cases 未接线")
        self.assertEqual(policy["request_interval_ms"], ex["request_interval_ms"],
                         "request_interval_ms 未接线")
        self.assertIs(policy.get("cleanup_enabled"), bool(ex.get("cleanup_enabled")),
                      "cleanup_enabled 未接线（曾是被测试白名单放行的假开关）")
        self.assertEqual(keys(ex) - {"profile", "retry_network_errors", "request_interval_ms",
                                     "max_cases", "minimize_findings",
                                     "cleanup_enabled", "cleanup_delete_action"}, set(),
                         "execution 段新增了未接线的键（新键须先在 apply_exec_policy/"
                         "cleanup_step 消费再加入本白名单）")
        self.assertEqual(keys(tpl.get("environment") or {}) - {"name", "type", "write_enabled"},
                         set(), "environment 段新增了未接线的键（写门禁只读 type/write_enabled）")
        self.assertEqual(keys(tpl.get("inputs") or {}) - {"contracts", "historical_issues",
                                                         "ui_baseline"}, set(),
                         "inputs 段新增了未接线的键")
        # adapter 也必须被消费：配置里写了 adapter 却在命令行被默认值覆盖过（已是历史缺陷）
        import argparse
        src = open(os.path.join(ROOT, "scripts", "flow.py"), encoding="utf-8").read()
        self.assertIn('exec_cfg.get("adapter")', src, "配置里的 adapter 未被读取")
        self.assertIsNotNone(argparse)

    def test_inputs_ui_baseline_reaches_plan(self):
        """配置里给了 ui_baseline，plan 就必须带上该资产——否则配了也白配。"""
        import flow
        d = tempfile.mkdtemp()
        bl = os.path.join(d, "ui_baseline.json")
        open(bl, "w", encoding="utf-8").write(json.dumps({
            "baselines": [{"field": "title", "action": "1000107", "page_value": "页面标题"}]},
            ensure_ascii=False))
        inputs = flow.resolve_inputs({"inputs": {"ui_baseline": bl}})
        self.assertEqual(inputs["ui_baseline"], bl)
        out = flow.build_intelligence_artifacts(
            os.path.join(ROOT, "tests", "fixtures", "simple_contract.md"), d,
            ui_baseline=inputs["ui_baseline"])
        kinds = [a.get("kind") for a in out["plan"].get("assets") or []]
        self.assertIn("ui_baseline", kinds, "ui_baseline 未进入计划资产，AB 会被误判为缺基线")

    def test_inputs_contract_fallback_and_history(self):
        import flow
        d = tempfile.mkdtemp()
        contract = os.path.join(ROOT, "tests", "fixtures", "simple_contract.md")
        inputs = flow.resolve_inputs({"inputs": {"contracts": [contract],
                                                 "historical_issues": [{"id": "N1"}]}})
        self.assertEqual(inputs["contract"], contract)
        self.assertEqual(len(inputs["history"]), 1)
        hist = os.path.join(d, "hist.csv")
        open(hist, "w", encoding="utf-8-sig").write("编号,类型\nN1,缺陷\n")
        self.assertEqual(len(flow.load_history(hist)), 1)
        self.assertEqual(flow.load_history(os.path.join(d, "missing.csv")), [],
                         "历史问题文件缺失应忽略而非崩溃")


if __name__ == "__main__":
    unittest.main()
