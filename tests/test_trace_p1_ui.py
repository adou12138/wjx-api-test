# -*- coding: utf-8 -*-
"""TRACE 评测 P1 项回归测试：UI 还原相关缺陷发现能力。

覆盖三条验收线：
  1. AB 维度不再"永远跳过"——有基线则执行比对，无基线则记覆盖缺口且不算已覆盖；
  2. plan_builder 的 AB 阻塞逻辑真正生效（此前是死代码）；
  3. 接口侧 UI 代理信号可自动执行（往返/资源链接/渲染降级）。
"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: F401,E402 环境替身（必须先于生产模块导入）


def _reset():
    import api_test_runner as R
    R.results.clear()
    R.findings.clear()
    R.COVERAGE_PLAN = []
    R.coverage_blocked = False
    if hasattr(R, "shallow_passes"):
        R.shallow_passes = 0
    return R


class TestUiBaselineLoader(unittest.TestCase):
    """P1-1：页面基线必须机器可读，且缺基线时不得凭空编预期值。"""

    def _write(self, rows):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "ui_baseline.json")
        open(p, "w", encoding="utf-8").write(
            json.dumps({"baselines": rows}, ensure_ascii=False))
        return p

    def test_load_from_file(self):
        import ui_baseline as UB
        p = self._write([{"action": "1000107", "field": "title",
                          "page_value": "页面标题", "source": "人工页面操作"}])
        bl, src = UB.load(p)
        self.assertEqual(len(bl), 1)
        self.assertEqual(bl[0].page_value, "页面标题")
        self.assertEqual(src, p)

    def test_missing_file_returns_empty(self):
        import ui_baseline as UB
        bl, src = UB.load(os.path.join(tempfile.mkdtemp(), "nope.json"))
        self.assertEqual(bl, [])
        self.assertEqual(src, "")

    def test_check_parity_detects_mismatch(self):
        import ui_baseline as UB
        p = self._write([{"action": "1000107", "field": "title", "page_value": "页面标题"}])
        bl, _ = UB.load(p)
        checked, gaps, findings = UB.check_parity(
            bl, "1000107", {"data": {"title": "接口标题"}}, {"fields": ["title"]})
        self.assertEqual(checked, 1)
        self.assertEqual(len(findings), 1)
        self.assertIn("口径不一致", findings[0][3])

    def test_check_parity_reports_gaps_not_fabricated_expectations(self):
        import ui_baseline as UB
        p = self._write([{"action": "1000107", "field": "other", "page_value": "x"}])
        bl, _ = UB.load(p)
        checked, gaps, findings = UB.check_parity(
            bl, "1000107", {"data": {"title": "接口标题"}}, {"fields": ["title"]})
        self.assertEqual(checked, 0)
        self.assertEqual(gaps, ["title"], "缺基线时不得凭空生成预期值")
        # 双向检查会把「基线登记了、接口未返回」的字段记为缺失（前端 undefined 渲染空白）；
        # 关键语义锁定：不得为响应字段 title 编造任何预期值
        self.assertFalse([f for f in findings if f[0] == "title"],
                         "不得为无基线的响应字段编造预期")
        self.assertEqual([f[0] for f in findings], ["other"],
                         "未返回的基线字段应被反向点名")

    def test_shipped_template_is_never_used_as_real_baseline(self):
        """包内 example 模板必须加载器主动跳过——示范值当实测值会造出假 AB 缺陷。"""
        import ui_baseline as UB
        tpl = os.path.join(ROOT, "assets", "ui_baseline.example.json")
        self.assertTrue(os.path.isfile(tpl), "缺少基线占位模板 assets/ui_baseline.example.json")
        bl, src = UB.load(tpl)
        self.assertEqual(bl, [], "template=true 的占位模板不得被当作真实基线")
        self.assertEqual(src, "")
        doc = json.load(open(tpl, encoding="utf-8"))
        self.assertTrue(doc.get("template"), "模板必须带 template=true 标记")
        self.assertTrue(doc.get("baselines"), "模板须示范 baselines 结构")

    def test_template_absent_from_default_search_path(self):
        """默认搜索路径里的 skill 内基线文件只能是真实文件，不能是 example。"""
        import ui_baseline as UB
        self.assertNotIn("example", " ".join(UB.DEFAULT_PATHS))
        self.assertFalse(os.path.isfile(os.path.join(ROOT, "assets", "ui_baseline.json")),
                         "包内不得预置 assets/ui_baseline.json（真实基线放包外）")


class TestAbIsNoLongerAlwaysSkipped(unittest.TestCase):
    """P1-1b：AB 维度必须真正可执行——有基线执行、无基线记缺口且不算已覆盖。"""

    def _case(self, path=None):
        c = {"id": "AB01", "name": "页面口径", "dimension": "AB", "action": "1000107",
             "params": {"title": "T"}, "check": "ui_parity", "needs": "ui_baseline",
             "expect": {"success": True}}
        if path:
            c["check_args"] = {"baseline_path": path}
        return c

    def test_no_baseline_records_coverage_gap(self):
        import case_runner as CR
        import api_test_runner as R
        R = _reset()
        orig = R.call
        R.call = lambda payload, retries=2: (200, '{"result": true}',
                                             {"result": True, "data": {"title": "T"}})
        try:
            CR.exec_case(self._case(), dry=False)
        finally:
            R.call = orig
        gaps = [f for f in R.findings if str(f.get("id", "")).startswith("COV-")]
        self.assertTrue(gaps, "无基线必须记覆盖率缺口，不得静默跳过")

    def test_no_baseline_does_not_count_as_covered(self):
        import case_runner as CR
        import api_test_runner as R
        R = _reset()
        orig = R.call
        R.call = lambda payload, retries=2: (200, '{"result": true}',
                                             {"result": True, "data": {"title": "T"}})
        try:
            CR.exec_case(self._case(), dry=False)
        finally:
            R.call = orig
        R.COVERAGE_PLAN = ["AB"]
        import io
        buf, old = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            R.coverage_gate()
        finally:
            sys.stdout = old
        self.assertTrue(R.coverage_blocked,
                        "AB 未真正执行时门禁必须拦截，实际输出：\n%s" % buf.getvalue())

    def test_with_baseline_ab_executes_and_detects_mismatch(self):
        import case_runner as CR
        import api_test_runner as R
        R = _reset()
        d = tempfile.mkdtemp()
        p = os.path.join(d, "bl.json")
        open(p, "w", encoding="utf-8").write(json.dumps(
            {"baselines": [{"action": "1000107", "field": "title",
                            "page_value": "页面显示标题"}]}, ensure_ascii=False))
        R.CONTRACT_KEYS = None
        orig = R.call
        R.call = lambda payload, retries=2: (200, '{"result": true}',
                                             {"result": True, "data": {"title": "接口标题"}})
        try:
            CR.exec_case(self._case(p), dry=False)
        finally:
            R.call = orig
        hits = [f for f in R.findings
                if "口径不一致" in str(f.get("desc", "")) and f.get("dimension") == "AB"]
        self.assertTrue(hits, "有基线时必须真正比对并报出口径不一致")


class TestPlanBuilderAbBlocking(unittest.TestCase):
    """P1-2：AB 在 required 且缺页面基线时必须进入 blocked（此前只比对 optional，从不触发）。"""

    def test_ab_in_required_is_blocked_without_baseline(self):
        from plan_builder import build_plan
        from contract_model import load_contract_model
        from relationship_inference import infer_relations
        from risk_engine import score
        from asset_registry import register
        m = load_contract_model(os.path.join(ROOT, "tests", "fixtures", "simple_contract.md"))
        m.relations = infer_relations(m)
        plan = build_plan(
            m,
            applicability={"coverage_plan": ["A", "AB"], "optional_plan": ["E"]},
            risk=score(m),
            assets=[register("contract", "x", trust="high")])
        dims = [b["dimension"] for b in plan["blocked"]]
        self.assertIn("AB", dims, "AB 在必测维度且无基线时必须进入 blocked")

    def test_ab_not_blocked_when_baseline_present(self):
        from plan_builder import build_plan
        from contract_model import load_contract_model
        from asset_registry import register
        m = load_contract_model(os.path.join(ROOT, "tests", "fixtures", "simple_contract.md"))
        plan = build_plan(
            m, applicability={"coverage_plan": ["AB"], "optional_plan": []},
            risk={"score": 0, "level": "low"},
            assets=[register("ui_baseline", "bl.json", trust="medium")])
        self.assertEqual([b for b in plan["blocked"] if b["dimension"] == "AB"], [])


class TestUiProxySignals(unittest.TestCase):
    """P1-3：接口侧 UI 代理信号必须可自动执行，且能真的报出问题。"""

    def test_render_degrade_reports_empty_declared_field(self):
        import case_runner as CR
        import api_test_runner as R
        R = _reset()
        CR._check_render_degrade(
            {"id": "UI1-degrade", "dimension": "Y", "params": {}},
            {"data": {"title": "ok", "description": None}},
            {"fields": ["title", "description"]})
        self.assertTrue(any("渲染降级" in f["desc"] for f in R.findings))

    def test_roundtrip_detects_escaped_html(self):
        import case_runner as CR
        import api_test_runner as R
        R = _reset()
        CR._check_roundtrip(
            {"id": "UI1-html", "dimension": "C",
             "params": {"html_content": "<b>粗体</b>"}},
            {"data": {"html_content": "&lt;b&gt;粗体&lt;/b&gt;"}},
            {"fields": ["html_content"]})
        self.assertTrue(any("往返不一致" in f["desc"] for f in R.findings),
                        "HTML 被转义必须被判为往返不一致")

    def test_roundtrip_clean_when_echo_is_exact(self):
        import case_runner as CR
        import api_test_runner as R
        R = _reset()
        CR._check_roundtrip({"id": "UI1-ok", "dimension": "C", "params": {"t": "x"}},
                            {"data": {"t": "x"}}, {"fields": ["t"]})
        self.assertEqual(R.findings, [])

    def test_resource_url_rejects_empty_resource(self):
        import api_test_runner as R
        ok, msg, meta = R.check_resource_url({"data": {}}, url_field="img_url")
        self.assertFalse(ok)

    def test_pattern_engine_emits_ui_proxy_cases(self):
        import case_generator as G
        doc = G.generate(os.path.join(ROOT, "tests", "fixtures", "sojump_contract.md"))
        ids = [c["id"] for c in doc["cases"]]
        self.assertTrue(any(i.startswith("UI1-") for i in ids),
                        "生成器必须产出接口侧 UI 代理用例，实际：%s" % ids)
        proxy = [c for c in doc["cases"] if c.get("category") == "接口侧UI代理"]
        self.assertGreaterEqual(len(proxy), 3)


if __name__ == "__main__":
    unittest.main()
