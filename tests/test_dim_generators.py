import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import case_generator as CG            # noqa: E402
import dim_generators as DG            # noqa: E402
import case_runner as CR               # noqa: E402

FIX = ROOT / "tests" / "fixtures"
DOMAIN = str(FIX / "domain_contract.md")
EXPORT = str(FIX / "export_contract.md")
SIMPLE = str(FIX / "simple_contract.md")


def _dims(path):
    doc = CG.generate(path)
    out = {}
    for c in doc["cases"]:
        out.setdefault(str(c.get("dimension")), []).append(c)
    return doc, out


class TestDomainDimGeneration(unittest.TestCase):
    """域维度必须「有特征才生成」——造出无特征维度的用例是噪音，不是覆盖。"""

    def test_domain_contract_covers_field_driven_dims(self):
        _, by = _dims(DOMAIN)
        for dim in ("N", "Q", "W", "S", "T", "O", "P", "R", "U", "X", "I", "F", "V"):
            self.assertIn(dim, by, "维度 %s 应有用例产出" % dim)
            self.assertTrue(by[dim], "维度 %s 用例列表为空" % dim)

    def test_bool_field_uses_switch_not_ref(self):
        """开关型字段注入 -1 是无意义噪音：notify/share 应走 true/false 取值验证。"""
        _, by = _dims(DOMAIN)
        notify_ids = [c["id"] for c in by.get("O", [])]
        self.assertTrue(all("SWITCH" in i for i in notify_ids),
                        "布尔字段应生成开关用例，实际：%s" % notify_ids)
        self.assertTrue(any(c["params"]["notify"] is True for c in by["O"]))
        self.assertTrue(any(c["params"]["notify"] is False for c in by["O"]))

    def test_id_field_uses_ref_probe(self):
        _, by = _dims(DOMAIN)
        ids = [c["id"] for c in by["N"]]
        self.assertTrue(all("REF" in i for i in ids), ids)

    def test_simple_contract_has_no_domain_dims(self):
        """契约没有层级/附件/批量字段时，不得凭空造出对应维度用例。"""
        _, by = _dims(SIMPLE)
        for dim in ("N", "W", "X"):
            self.assertNotIn(dim, by, "无特征契约不应生成 %s 用例" % dim)

    def test_export_contract_generates_m_with_credential_check(self):
        _, by = _dims(EXPORT)
        self.assertIn("M", by)
        checks = [c.get("check") for c in by["M"]]
        self.assertIn("export_credential", checks)

    def test_encoding_cases_are_probe_tier(self):
        _, by = _dims(DOMAIN)
        self.assertTrue(all(c.get("tier") == "probe" for c in by["I"]),
                        "编码用例是字段×样本遍历，须标 probe 防止撑高业务覆盖")


class TestNewChecks(unittest.TestCase):
    """新增检查的正反分支：识别不到记观察，确定失效才判缺陷。"""

    def setUp(self):
        self.findings = []
        self._issue = CR.R.add_issue
        self._obs = CR.R.add_observation
        self._build = CR.R.build_payload
        self._call = CR.R.call
        CR.R.add_issue = lambda *a, **k: self.findings.append(("ISSUE", a[0], k.get("severity")))
        CR.R.add_observation = lambda *a, **k: self.findings.append(("OBS", a[0], k.get("severity")))
        CR.R.build_payload = lambda action, **kw: dict(kw, action=action)

    def tearDown(self):
        CR.R.add_issue = self._issue
        CR.R.add_observation = self._obs
        CR.R.build_payload = self._build
        CR.R.call = self._call

    def test_trace_missing_is_observation_not_defect(self):
        CR.R.call = lambda p: (200, "{}", {"result": True})
        CR._check_trace_id({"id": "V01", "action": "1", "params": {}}, {"result": True},
                           {"expect_distinct": True})
        self.assertTrue(self.findings)
        self.assertEqual(self.findings[0][0], "OBS")

    def test_trace_empty_is_defect(self):
        CR.R.call = lambda p: (200, "{}", {"trace_id": ""})
        CR._check_trace_id({"id": "V01", "action": "1", "params": {}}, {"result": True, "trace_id": ""},
                           {"expect_distinct": True})
        self.assertEqual(self.findings[0][0], "ISSUE")

    def test_trace_not_distinct_is_defect(self):
        CR.R.call = lambda p: (200, "{}", {"trace_id": "SAME"})
        CR._check_trace_id({"id": "V01", "action": "1", "params": {}}, {"result": True, "trace_id": "SAME"},
                           {"expect_distinct": True})
        self.assertEqual(self.findings[0][0], "ISSUE")

    def test_trace_distinct_passes(self):
        CR.R.call = lambda p: (200, "{}", {"trace_id": "T2"})
        CR._check_trace_id({"id": "V01", "action": "1", "params": {}}, {"result": True, "trace_id": "T1"},
                           {"expect_distinct": True})
        self.assertTrue(self.findings)
        self.assertEqual(self.findings[0][0], "OBS")

    def test_trace_found_in_nested_data(self):
        CR.R.call = lambda p: (200, "{}", {"data": {"requestId": "T9"}})
        CR._check_trace_id({"id": "V01", "action": "1", "params": {}},
                           {"data": {"requestId": "T8"}}, {"expect_distinct": True})
        self.assertEqual(self.findings[0][0], "OBS")

    def test_export_empty_credential_is_high_defect(self):
        CR.R.call = lambda p: (200, "{}", {"url": ""})
        CR._check_export_credential({"id": "M01", "action": "1", "params": {}},
                                    {"result": True, "url": ""}, {})
        self.assertEqual(self.findings[0][0], "ISSUE")
        self.assertEqual(self.findings[0][2], "高")

    def test_export_no_credential_field_is_observation(self):
        CR.R.call = lambda p: (200, "{}", {"result": True})
        CR._check_export_credential({"id": "M01", "action": "1", "params": {}},
                                    {"result": True}, {})
        self.assertEqual(self.findings[0][0], "OBS")


def _f(name, role="", evidence="", ftype="string"):
    return {"name": name, "type": ftype, "required": False, "enum": None,
            "llm_role": role, "llm_evidence": evidence}


def _c(req, resp=None, name="创建对象"):
    return {"name": name, "request_fields": req, "response_fields": resp or []}


class TestRoleDimMap(unittest.TestCase):
    """语义角色 → 维度：字段名猜不出来时，用语义层读出的角色激活维度。"""

    def test_no_llm_role_keeps_rule_only(self):
        """没并入语义事实时行为不变——映射不能改变纯规则链路。"""
        out = DG.gen_domain_field_cases(_c([_f("creater")]), {"creater": "u1"}, "1")
        self.assertEqual([x for x in out if x["dimension"] == "Q"], [])

    def test_operator_identity_activates_q(self):
        """字段名不含 role，但语义判定为操作者身份 → Q 维度被激活。"""
        c = _c([_f("creater", role="operator_identity", evidence="子账户必须有管理员权限")])
        q = [x for x in DG.gen_domain_field_cases(c, {"creater": "u1"}, "1")
             if x["dimension"] == "Q"]
        self.assertEqual(len(q), 2)
        self.assertTrue(all(x["params"]["creater"] != "u1" for x in q))

    def test_unmapped_role_does_not_activate(self):
        """映射表外的角色一律不激活——映射过宽会让维度在无关接口上乱开。"""
        c = _c([_f("title", role="display_name"), _f("html", role="alias")])
        self.assertEqual(DG.gen_domain_field_cases(c, {"title": "t", "html": "h"}, "1"), [])

    def test_response_side_field_does_not_activate(self):
        """响应侧字段无法构造请求，激活了也产不出用例。"""
        c = _c([_f("x")], resp=[_f("pc_path", role="url_relative")])
        self.assertEqual([x for x in DG.gen_domain_field_cases(c, {"x": "1"}, "1")
                          if x["dimension"] == "W"], [])

    def test_source_marks_semantic_origin(self):
        """来源要能区分：语义激活的用例断言比 token 直接命中的弱一档。"""
        c = _c([_f("creater", role="operator_identity", evidence="子账户权限")])
        q = [x for x in DG.gen_domain_field_cases(c, {"creater": "u1"}, "1")
             if x["dimension"] == "Q"]
        self.assertTrue(all("(llm_role)" in x["source"] for x in q), q)

    def test_note_carries_contract_evidence(self):
        """语义激活必须写明契约依据，不能让读报告的人以为是契约白纸黑字写的。"""
        c = _c([_f("creater", role="operator_identity", evidence="子账户必须有管理员权限")])
        q = [x for x in DG.gen_domain_field_cases(c, {"creater": "u1"}, "1")
             if x["dimension"] == "Q"]
        for x in q:
            self.assertIn("子账户必须有管理员权限", x["note"])
            self.assertIn("人工确认", x["note"])



class TestZeroOutputReason(unittest.TestCase):
    """零产出的选测维度必须带原因：只列字母会把能力缺口记成「本接口无需测」。"""

    def _contract(self, request, response, name="t"):
        mk = lambda n, t="string": {"name": n, "type": t, "required": False,
                                    "enum": None, "max_len": None, "forbid": None,
                                    "default": None, "desc": "", "semantics": []}
        return {"name": name, "action": "1", "request_fields": [mk(n) for n in request],
                "response_fields": [mk(n) for n in response]}

    def test_unimplemented_dim_is_reported_as_capability_gap(self):
        """没有生成器实现的维度，必须说明是能力缺口而不是契约无特征。"""
        r = DG.explain_zero_output(self._contract([], []), ["ZZ"])
        self.assertEqual(r["ZZ"], DG.REASON_NOT_IMPLEMENTED)
        self.assertIn("能力缺口", DG.REASON_TEXT[r["ZZ"]])

    def test_l_is_implemented_and_not_reported_as_gap(self):
        """L 已有生成器实现，不能再被报成能力缺口——否则等于没修。"""
        self.assertIn("L", DG.IMPLEMENTED_DIMS)
        r = DG.explain_zero_output(self._contract(["title"], ["sid"]), ["L"])
        self.assertNotEqual(r["L"], DG.REASON_NOT_IMPLEMENTED)

    def test_response_side_only_field_is_not_reported_as_no_feature(self):
        """W 的 token 命中响应侧字段时，不能记成「契约无此特征」。"""
        c = self._contract(request=["title"], response=["file_url"])
        r = DG.explain_zero_output(c, ["W"])
        self.assertEqual(r["W"], DG.REASON_RESPONSE_SIDE_ONLY)

    def test_truly_absent_feature_reports_no_feature(self):
        r = DG.explain_zero_output(self._contract(["title"], ["sid"]), ["W", "Q"])
        self.assertEqual(r["W"], DG.REASON_NO_FEATURE)
        self.assertEqual(r["Q"], DG.REASON_NO_FEATURE)

    def test_generated_dim_has_no_reason(self):
        """产出用例的维度不该被标任何零产出原因——生成与解释共用判定，不能漂移。"""
        c = self._contract(["file_id"], [])
        cases = DG.gen_domain_field_cases(c, {}, "1")
        self.assertTrue(cases)
        self.assertIsNone(DG._domain_dim_resolve(c)["W"]["reason"])

    def test_role_activated_dim_has_no_reason(self):
        """语义激活同样算命中，不能被误标为无特征。"""
        c = self._contract(["creater"], [])
        c["request_fields"][0]["llm_role"] = "operator_identity"
        self.assertIsNone(DG._domain_dim_resolve(c)["Q"]["reason"])
        self.assertEqual(DG.explain_zero_output(c, ["Q"])["Q"], DG.REASON_NO_FEATURE)

    def test_explain_covers_every_requested_dim(self):
        r = DG.explain_zero_output(self._contract(["title"], []), ["L", "Q", "W", "ZZ"])
        self.assertEqual(sorted(r), ["L", "Q", "W", "ZZ"])
        self.assertEqual(r["ZZ"], DG.REASON_NOT_IMPLEMENTED)

    def test_detail_is_in_artifact(self):
        """产物里必须能读到原因明细，而不只是维度字母列表。"""
        doc = CG.generate(DOMAIN)
        detail = doc.get("uncovered_optional_detail") or []
        self.assertEqual([d["dimension"] for d in detail], doc["uncovered_optional"])
        for d in detail:
            self.assertIn(d["reason"], DG.REASON_TEXT)
            self.assertTrue(d["reason_text"])



class TestClientVariant(unittest.TestCase):
    """L 维度：跨客户端兼容的黑盒降级版。"""

    def _contract(self, request, response):
        mk = lambda n: {"name": n, "type": "string", "required": False, "enum": None,
                        "max_len": None, "forbid": None, "default": None, "desc": "",
                        "semantics": []}
        return {"name": "t", "action": "1", "request_fields": [mk(n) for n in request],
                "response_fields": [mk(n) for n in response]}

    def test_generates_one_case_with_all_variants(self):
        cases = DG.gen_client_variant_cases(self._contract(["title"], ["sid"]), {}, "1")
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["dimension"], "L")
        self.assertEqual(cases[0]["check"], "client_variant")
        self.assertEqual(len(cases[0]["check_args"]["user_agents"]), 3)

    def test_no_response_fields_produces_nothing(self):
        """没有响应字段就无法做结构对照，不该生成空转用例。"""
        self.assertEqual(DG.gen_client_variant_cases(self._contract(["title"], []), {}, "1"), [])

    def test_note_declares_it_is_a_downgraded_version(self):
        """必须写明是黑盒降级版，不能让人以为是完整的多端覆盖。"""
        c = DG.gen_client_variant_cases(self._contract(["title"], ["sid"]), {}, "1")[0]
        self.assertIn("降级版", c["note"])
        self.assertIn("多端环境", c["note"])

    def test_check_is_registered(self):
        """生成器产出的 check 必须在执行器里注册，否则执行时会被当成未知检查跳过。"""
        self.assertIn("client_variant", CR.CHECKS)


if __name__ == "__main__":
    unittest.main()
