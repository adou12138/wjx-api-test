# -*- coding: utf-8 -*-
"""v4.17.0 功能：显式跨接口依赖梳理 + 人可读计划渲染。

覆盖三件容易回归的事：
  1. 依赖取数纪律——只收显式声明，自引用要排除，定不位的进 unknowns；
  2. 计划渲染——四段结构齐全、缺字段不崩、待确认问题可汇总；
  3. 链路可序列化——关系一旦带 evidence，`__dict__` 就不再是 JSON 可序列化的。
"""
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import dependency_scan as DS
import plan_render as PR
from contract_model import ContractModel, Evidence, FieldModel, RelationModel
from plan_builder import build_plan
from risk_engine import score

SAMPLE = """# 2.1.15 更新AI主页[1000108]

# 接口地址
生产环境：`https://www.wjx.cn/openapi/default.aspx`

|参数|类型|说明|必需|
|:---|:---|:---|---|
|vid|string|AI主页编号|是|

`AI主页处于发布状态时不能直接更新，请先调用修改问卷状态接口1000102，传入state=2暂停主页后再更新。`

```json
{"action": "1000108", "vid": "123456"}
```
"""

NO_DEP = """# 2.1.14 创建AI主页[1000107]

|参数|类型|说明|必需|
|:---|:---|:---|---|
|title|string|标题|否|

```json
{"action": "1000107"}
```
"""

NAMED_BUT_UNRESOLVED = """# 2.2.1 答卷提交[1001001]

1、先调用上传文件接口获取 answer_file
"""


class TestDependencyScan(unittest.TestCase):
    def test_explicit_dependency_is_captured_with_evidence(self):
        res = DS.scan_text(SAMPLE, contract_path="c.md", self_action="1000108")
        deps = res["dependencies"]
        self.assertEqual(len(deps), 1)
        dep = deps[0]
        self.assertEqual(dep["target_action"], "1000102")
        self.assertEqual(dep["target_name"], "修改问卷状态")
        self.assertEqual(dep["kind"], "depends_on")
        self.assertEqual(dep["params"], {"state": "2"})
        # 参数值不得吞掉中文尾巴（原句里 "2" 后面紧跟"暂停主页后再更新"）
        self.assertNotIn("暂停", dep["params"]["state"])
        # 证据必须是契约原文的连续子串
        self.assertTrue(DS.evidence_is_substring(SAMPLE, dep["evidence"]))
        self.assertIn("请先调用修改问卷状态接口1000102", dep["evidence"])

    def test_self_reference_is_excluded(self):
        """请求示例里的 action 是文档自身标识，不能被当成依赖自己。"""
        res = DS.scan_text(SAMPLE, contract_path="c.md", self_action="1000108")
        self.assertNotIn("1000108", [d["target_action"] for d in res["dependencies"]])

    def test_no_declaration_means_no_dependency(self):
        """没有显式声明就一条都不产出——不做名称相似性推断。"""
        res = DS.scan_text(NO_DEP, contract_path="c.md", self_action="1000107")
        self.assertEqual(res["dependencies"], [])
        self.assertEqual(res["unknowns"], [])

    def test_named_api_without_action_goes_to_unknowns(self):
        """说了要先调接口却给不出 action 号：定不位，只留痕。"""
        res = DS.scan_text(NAMED_BUT_UNRESOLVED, contract_path="c.md", self_action="1001001")
        self.assertEqual(res["dependencies"], [])
        self.assertEqual(len(res["unknowns"]), 1)
        self.assertEqual(res["unknowns"][0]["kind"], "dependency_unresolved")
        self.assertTrue(DS.evidence_is_substring(NAMED_BUT_UNRESOLVED,
                                                 res["unknowns"][0]["evidence"]))

    def test_action_number_alone_is_not_a_dependency(self):
        """光出现 action 号但没有依赖触发词，不算依赖。"""
        text = "|vid|string|AI主页编号|是|只能传入当前企业下的AI主页编号1000102|"
        res = DS.scan_text(text, contract_path="c.md", self_action="1000108")
        self.assertEqual(res["dependencies"], [])

    def test_companion_read_declared_by_field(self):
        text = "read_action: 1000108\nlist_action: 1000109\n"
        res = DS.scan_text(text, contract_path="c.md", self_action="1000107",
                           contract={"read_action": "1000108", "list_action": "1000109"})
        targets = {d["target_action"] for d in res["dependencies"]}
        self.assertEqual(targets, {"1000108", "1000109"})
        self.assertTrue(all(d["kind"] == "companion_read" for d in res["dependencies"]))

    def test_chinese_param_value_is_not_greedily_captured(self):
        """中文取值没有可靠边界，宁可不记也不记错；原句仍留在 evidence 里。"""
        text = "`更新前请先调用修改问卷状态接口1000102，传入state=已暂停，再重试。`"
        res = DS.scan_text(text, contract_path="c.md", self_action="1000108")
        dep = res["dependencies"][0]
        self.assertIsNone(dep.get("params"))
        self.assertIn("state=已暂停", dep["evidence"])

    def test_to_relations_carries_evidence(self):
        res = DS.scan_text(SAMPLE, contract_path="c.md", self_action="1000108")
        relations = DS.to_relations(res, "c.md")
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0].relation_type, "depends_on")
        self.assertEqual(relations[0].target_operation, "1000102")
        self.assertTrue(relations[0].evidence[0].excerpt)

    def test_annotate_appends_without_dropping_inferred(self):
        model = ContractModel(operation="1000108", name="update homepage", path="/homepage/update",
                              fields=[FieldModel("vid")])
        model.relations = [RelationModel("update", "vid")]
        result = DS.annotate(model, "does-not-exist.md")
        self.assertEqual(len(model.relations), 1)  # 文件不存在时不追加、不报错
        self.assertEqual(result["dependencies"], [])


class TestRiskFactorIsHonest(unittest.TestCase):
    def test_inferred_relation_alone_does_not_score_cross_api_dependency(self):
        """relationship_inference 产出的操作类型关系度量的是"本接口是写入类"，
        不是跨接口依赖，不应再给 cross_api_dependency 加分。"""
        model = ContractModel(operation="1000108", name="更新AI主页", path="/homepage/update",
                              fields=[FieldModel("vid")])
        model.relations = [RelationModel("update", "resource")]
        self.assertNotIn("cross_api_dependency", score(model)["breakdown"])

    def test_explicit_dependency_scores_cross_api_dependency(self):
        model = ContractModel(operation="1000108", name="更新AI主页", path="/homepage/update",
                              fields=[FieldModel("vid")])
        model.relations = [RelationModel("update", "resource"),
                           RelationModel("depends_on", "修改问卷状态", target_operation="1000102")]
        self.assertEqual(score(model)["breakdown"].get("cross_api_dependency"), 10)


class TestPlanBuilderDependencies(unittest.TestCase):
    def test_dependencies_are_part_of_plan(self):
        model = ContractModel(operation="1000108", name="更新AI主页", path="/homepage/update")
        plan = build_plan(model, dependencies=[{"target_action": "1000102", "kind": "depends_on",
                                                "evidence": "x"}],
                          dependency_unknowns=[{"kind": "dependency_unresolved"}])
        self.assertEqual(len(plan["dependencies"]), 1)
        self.assertEqual(len(plan["dependency_unknowns"]), 1)
        json.dumps(plan, ensure_ascii=False)  # 计划必须可序列化

    def test_plan_defaults_are_empty_lists(self):
        plan = build_plan(ContractModel(operation="1000107"))
        self.assertEqual(plan["dependencies"], [])
        self.assertEqual(plan["dependency_unknowns"], [])


class TestPlanRender(unittest.TestCase):
    def _plan(self, **extra):
        plan = {"operation": "1000108", "required": ["A", "B"], "optional": ["F"],
                "blocked": [{"dimension": "AB", "reason": "缺少页面基线", "action": "登记基线"}],
                "inferred_items": [{"field": "creater", "confidence": 0.5,
                                    "action": "exploratory_only"}],
                "risk": {"score": 35, "level": "medium",
                         "breakdown": {"write_operation": 25, "cross_api_dependency": 10}},
                "na_proofs": [{"dim": "M", "reason": "不涉及导出下载"}]}
        plan.update(extra)
        return plan

    def _doc(self):
        return {"action": "1000108", "name": "更新AI主页", "endpoint": "https://x/y",
                "op_type": "修改", "tier_stats": {"business": 38, "probe": 85, "manual": 4},
                "cases": [{"dimension": "B"}, {"dimension": "B"}, {"dimension": "G"}],
                "matched_patterns": [{"id": "UI1", "cases": 10}]}

    def test_four_sections_present(self):
        md = PR.render_plan_md(self._plan(), doc=self._doc(), contract_path="c.md")
        for heading in ("## 1. 用例总览", "## 2. 待确认问题",
                        "## 3. 前置依赖与关系", "## 4. 风险与不适用凭据"):
            self.assertIn(heading, md)
        self.assertIn("更新AI主页（action=1000108）", md)
        self.assertIn("| business | 38 |", md)

    def test_pending_items_collected_from_all_sources(self):
        doc = self._doc()
        doc["uncovered_optional_detail"] = [
            {"dimension": "W", "reason": "response_side_only", "reason_text": "字段仅在响应侧"}]
        plan = self._plan(dependency_unknowns=[{"kind": "dependency_unresolved",
                                                "evidence": "先调用上传文件接口"}])
        pending = PR.collect_pending(plan, doc)
        kinds = {p["type"] for p in pending}
        self.assertEqual(kinds, {"阻塞", "低置信度字段", "依赖无法定位", "选测零产出"})

    def test_missing_dependency_is_stated_not_silent(self):
        md = PR.render_plan_md(self._plan(), doc=self._doc())
        self.assertIn("未显式声明", md)
        self.assertIn("不等于本接口没有前置条件", md)

    def test_dependency_row_shows_target_and_evidence(self):
        plan = self._plan(dependencies=[{"kind": "depends_on", "target_action": "1000102",
                                         "target_name": "修改问卷状态", "line": 20,
                                         "evidence": "请先调用修改问卷状态接口1000102"}])
        md = PR.render_plan_md(plan, doc=self._doc())
        self.assertIn("1000102", md)
        self.assertIn("修改问卷状态", md)

    def test_empty_inputs_do_not_crash(self):
        md = PR.render_plan_md({}, doc={})
        self.assertIn("# 测试计划", md)
        self.assertIn("## 4. 风险与不适用凭据", md)


class TestRelationSerializationRegression(unittest.TestCase):
    def test_relation_with_evidence_is_json_serializable(self):
        """回归：关系一旦带 evidence，用 __dict__ 序列化会抛
        TypeError: Object of type Evidence is not JSON serializable。
        计划统一走 dataclasses.asdict，此测试锁定该约束。"""
        from dataclasses import asdict
        relation = RelationModel("depends_on", "修改问卷状态", target_operation="1000102",
                                 reason="契约正文显式声明",
                                 evidence=[Evidence(source="c.md", line=20, excerpt="请先调用")])
        payload = json.dumps([asdict(relation)], ensure_ascii=False)
        self.assertIn("1000102", payload)
        self.assertIn("请先调用", payload)


if __name__ == "__main__":
    unittest.main()
