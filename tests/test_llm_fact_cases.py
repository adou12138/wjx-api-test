#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""语义层事实兑付层的测试。

重点不是"生成了几条"，而是三条硬约束不被违反：
1. 没并入事实就一条都不产出（兑付层不能凭空造用例）
2. 契约没说的一律不生成自动断言（must_not_assert）
3. 推断出来的关系不能被写成契约原文
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import llm_fact_cases as L  # noqa: E402

BASE = {"title": "t", "html_content": "c", "html": "c", "publish": False}


def _field(name, role="", constraints=None, evidence="", enum=None):
    return {"name": name, "type": "string", "required": False, "enum": enum,
            "llm_role": role, "llm_constraints": constraints or {},
            "llm_evidence": evidence}


def _contract(req=None, resp=None, relations=None, unknowns=None, facts=True):
    c = {"request_fields": req or [], "response_fields": resp or [],
         "llm_relations": relations or [], "llm_unknowns": unknowns or []}
    if facts:
        c["llm_facts_count"] = len(req or []) + len(resp or [])
    return c


def _ids(cases):
    return [c["id"] for c in cases]


class TestDegrade(unittest.TestCase):
    def test_no_facts_returns_empty(self):
        """没并入事实时兑付层必须一条不产——否则就是凭空造用例。"""
        c = _contract(req=[_field("title")], facts=False)
        self.assertEqual(L.run(c, dict(BASE), "1000107", {}), [])

    def test_facts_merged_but_no_recognized_kind_returns_empty(self):
        """并入了事实但没有本层认识的类型，同样不产出。"""
        c = _contract(req=[_field("title", role="display_name")])
        self.assertEqual(L.run(c, dict(BASE), "1000107", {}), [])

    def test_relations_only_still_produces(self):
        """只有关系事实（字段数 0）时不能整层哑火。

        fallback_chain / url_compose 的证据都挂在 relations 上，字段数完全可以是 0。
        只看字段数判空会把这类输入当成「没有事实」，整层静默不产出。
        """
        c = _contract(req=[], relations=[{
            "type": "fallback_chain", "between": ["title", "html_content"],
            "evidence": '未传时使用"未命名AI主页"', "confidence": "explicit"}])
        cases = L.run(c, dict(BASE), "1000107", {})
        self.assertTrue(any(i.startswith("LLM-FALLBACK-") for i in _ids(cases)))

    def test_unknowns_only_still_produces(self):
        """只有 unknowns 时同样要留痕，不能被判成「没有事实」。"""
        c = _contract(req=[], unknowns=[{"item": "业务唯一键", "reason": "契约未声明",
                                         "must_not_assert": True}])
        cases = L.run(c, dict(BASE), "1000107", {})
        self.assertTrue(any(i.startswith("LLM-UNK-") for i in _ids(cases)))


class TestQuotedDefault(unittest.TestCase):
    """默认值必须按语义位置取，不能取第一个引号对。

    原文里常先出现别的引号内容（禁用词、字段名），取第一个会把无关值当默认值。
    """

    def test_picks_default_not_earlier_quote(self):
        ev = ('最长100个字符；不能包含"问卷星"；未传时优先读取HTML中的title标签，'
              '仍为空时使用"未命名AI主页"')
        self.assertEqual(L._quoted(ev), "未命名AI主页")

    def test_no_default_semantics_returns_empty(self):
        self.assertEqual(L._quoted("html_content的兼容参数"), "")

    def test_empty_input_is_safe(self):
        self.assertEqual(L._quoted(""), "")
        self.assertEqual(L._quoted(None), "")


class TestFallbackChain(unittest.TestCase):
    REL = [{"type": "fallback_chain", "between": ["title", "html_content"],
            "evidence": '未传时读取HTML中的title标签，仍为空时使用"未命名AI主页"',
            "confidence": "explicit"}]

    def _run(self, ctx):
        c = _contract(req=[_field("title"), _field("html_content")], relations=self.REL)
        return L.run(c, dict(BASE), "1000107", ctx)

    def test_empty_branch_always_generated(self):
        """来源也为空 → 默认值分支，是 B 维度覆盖不到的，无论能否回读都要有。"""
        cases = self._run({})
        self.assertIn("LLM-FALLBACK-TITLE-EMPTY", _ids(cases))
        c = next(x for x in cases if x["id"] == "LLM-FALLBACK-TITLE-EMPTY")
        self.assertEqual(c["params"].get("html_content"), "")
        self.assertNotIn("title", c["params"])

    def test_fromsrc_branch_only_when_readback(self):
        """来源有值分支与 B 维度 missing 态参数相同，无回读通道时不重复发一次。"""
        self.assertNotIn("LLM-FALLBACK-TITLE-FROMSRC", _ids(self._run({})))
        self.assertIn("LLM-FALLBACK-TITLE-FROMSRC",
                      _ids(self._run({"read_action": "1000108"})))

    def test_default_value_extracted_from_evidence(self):
        """默认值取自契约原文引号内，不猜。"""
        cases = self._run({})
        note = next(x["note"] for x in cases if x["id"].endswith("-EMPTY"))
        self.assertIn("未命名AI主页", note)

    def test_no_default_value_does_not_invent_one(self):
        """原文没给默认值时，note 里不得出现凭空造的字面量。"""
        rel = [{"type": "fallback_chain", "between": ["title", "html_content"],
                "evidence": "未传时读取HTML中的title标签"}]
        c = _contract(req=[_field("title"), _field("html_content")], relations=rel)
        cases = L.run(c, dict(BASE), "1000107", {})
        note = next(x["note"] for x in cases if x["id"].endswith("-EMPTY"))
        self.assertIn("需回读确认", note)
        self.assertNotIn("预期默认值为", note)


class TestAlias(unittest.TestCase):
    def _contract(self, unknowns=None):
        return _contract(
            req=[_field("html", role="alias", constraints={"alias_of": "html_content"},
                        evidence="html_content的兼容参数"),
                 _field("html_content", role="body_content")],
            unknowns=unknowns or [])

    def test_both_paths_generated(self):
        cases = L.run(self._contract(), dict(BASE), "1000107", {})
        ids = _ids(cases)
        self.assertIn("LLM-ALIAS-HTML-ONLY", ids)
        self.assertIn("LLM-ALIAS-HTML_CONTENT-ONLY", ids)

    def test_only_alias_path_drops_canonical(self):
        cases = L.run(self._contract(), dict(BASE), "1000107", {})
        c = next(x for x in cases if x["id"] == "LLM-ALIAS-HTML-ONLY")
        self.assertNotIn("html_content", c["params"])

    def test_priority_unknown_produces_manual_only(self):
        """契约没说优先级 → 只能留痕，绝不替契约假定某一方优先。"""
        unk = [{"item": "html 与 html_content 同时传的优先级",
                "reason": "契约只说是兼容参数，未说优先级", "must_not_assert": True}]
        cases = L.run(self._contract(unk), dict(BASE), "1000107", {})
        c = next(x for x in cases if x["id"] == "LLM-ALIAS-BOTH")
        self.assertFalse(c["enabled"])
        self.assertEqual(c["needs"], "manual")
        self.assertIn("must_not_assert", c["note"])

    def test_no_unknown_no_both_case(self):
        cases = L.run(self._contract(), dict(BASE), "1000107", {})
        self.assertNotIn("LLM-ALIAS-BOTH", _ids(cases))


class TestPublishFlag(unittest.TestCase):
    def test_declared_default_is_auto_asserted(self):
        """契约声明的默认值必然合法 → 可自动断言。"""
        c = _contract(req=[_field("publish", role="publish_flag",
                                  evidence="默认不发布问卷",
                                  constraints={"default": False})])
        cases = L.run(c, dict(BASE), "1000107", {})
        by_id = {x["id"]: x for x in cases}
        self.assertIn("LLM-FLAG-PUBLISH-false", by_id)
        self.assertFalse(by_id["LLM-FLAG-PUBLISH-false"].get("needs"))
        self.assertFalse(by_id["LLM-FLAG-PUBLISH-false"]["params"]["publish"])

    def test_other_value_is_manual_never_auto_asserted(self):
        """另一个取值契约没说可否显式传入 → 只能留痕。

        用「只实现一个分支会造成能开不能关」这类通用经验替契约下判断，
        在服务端「创建只建草稿」的设计下会直接产出假缺陷。
        """
        c = _contract(req=[_field("publish", role="publish_flag",
                                  evidence="默认不发布问卷",
                                  constraints={"default": False})])
        cases = L.run(c, dict(BASE), "1000107", {})
        gap = next(x for x in cases if x["id"] == "LLM-FLAG-PUBLISH-GAP")
        self.assertFalse(gap["enabled"])
        self.assertEqual(gap["needs"], "manual")
        self.assertTrue(gap["params"]["publish"])
        self.assertIn("must_not_assert", gap["note"])

    def test_without_declared_default_only_manual_gap(self):
        """契约连默认值都没给 → 任一取值都无据，不得自动断言。"""
        c = _contract(req=[_field("publish", role="publish_flag", evidence="是否发布")])
        cases = L.run(c, dict(BASE), "1000107", {})
        self.assertEqual(_ids(cases), ["LLM-FLAG-PUBLISH-GAP"])
        self.assertEqual(cases[0]["needs"], "manual")


class TestStateGap(unittest.TestCase):
    def _contract(self, enum=None):
        return _contract(resp=[_field("status", role="state", evidence="参见3.8 问卷状态",
                                      enum=enum)],
                         unknowns=[{"item": "status / verify_status 的枚举取值",
                                    "reason": "外部引用 3.8，契约内不可得",
                                    "must_not_assert": True}])

    def test_gap_is_manual_never_auto_asserted(self):
        cases = L.run(self._contract(), dict(BASE), "1000107", {})
        c = next(x for x in cases if x["id"] == "LLM-STATE-STATUS")
        self.assertFalse(c["enabled"])
        self.assertEqual(c["needs"], "manual")
        self.assertEqual(c["dimension"], "E")
        self.assertIn("must_not_assert", c["note"])

    def test_enum_already_known_is_left_to_rules(self):
        """契约已给出枚举时交给规则层遍历，兑付层不重复产出状态缺口用例。"""
        cases = L.run(self._contract(enum=[0, 1, 2]), dict(BASE), "1000107", {})
        self.assertFalse([x for x in cases if str(x["id"]).startswith("LLM-STATE")])


class TestUrlCompose(unittest.TestCase):
    def _cases(self, confidence):
        rel = [{"type": "url_compose", "between": ["activity_domain", "pc_path"],
                "evidence": "PC端问卷Url相对路径", "confidence": confidence}]
        c = _contract(resp=[_field("pc_path", role="url_relative"),
                            _field("activity_domain", role="domain_prefix")],
                      relations=rel)
        return L.run(c, dict(BASE), "1000107", {})

    def test_inferred_not_claimed_as_contract_text(self):
        """推断的拼接关系不能被引作契约原文——那是编造依据。"""
        c = next(x for x in self._cases("inferred") if x["id"].startswith("LLM-URLCOMPOSE"))
        self.assertIn("confidence=inferred", c["note"])
        self.assertNotIn("契约原文", c["note"])
        self.assertFalse(c["enabled"])

    def test_explicit_uses_contract_wording(self):
        c = next(x for x in self._cases("explicit") if x["id"].startswith("LLM-URLCOMPOSE"))
        self.assertIn("契约原文", c["note"])


class TestCommon(unittest.TestCase):
    def test_every_case_carries_evidence_or_empty(self):
        """兑付出的用例必须能追溯到契约依据，没有依据的要显式留空而非省略字段。"""
        rel = [{"type": "fallback_chain", "between": ["title", "html_content"],
                "evidence": "兜底说明"}]
        c = _contract(req=[_field("title"), _field("html_content"),
                           _field("publish", role="publish_flag")],
                      relations=rel)
        for case in L.run(c, dict(BASE), "1000107", {}):
            self.assertIn("evidence", case)

    def test_ids_prefixed_for_tier_attribution(self):
        """统一 LLM- 前缀：分层逻辑靠前缀归因，前缀散了会掉进 probe 层。"""
        rel = [{"type": "fallback_chain", "between": ["title", "html_content"],
                "evidence": "兜底说明"}]
        c = _contract(req=[_field("title"), _field("html_content")], relations=rel)
        for case in L.run(c, dict(BASE), "1000107", {}):
            self.assertTrue(case["id"].startswith("LLM-"), case["id"])



class TestUnknownGap(unittest.TestCase):
    """契约没说的事必须留痕点名，且不得与已兑付项重复。"""

    def _run(self, unknowns):
        c = {"action": "1000107", "name": "t",
             "request_fields": [{"name": "title", "type": "string", "required": True,
                                 "enum": None, "max_len": None, "llm_role": "display_name",
                                 "llm_evidence": "标题", "llm_constraints": {}}],
             "response_fields": [],
             "llm_facts_count": 1, "llm_unknowns": unknowns,
             "llm_relations": []}
        return L.run(c, dict(BASE), "1000107", {})

    def test_unknown_is_traced_not_dropped(self):
        cases = self._run([{"item": "签名算法与失效规则", "reason": "契约只写「签名」二字"}])
        self.assertEqual(len(cases), 1)
        self.assertFalse(cases[0]["enabled"])
        self.assertEqual(cases[0]["needs"], "manual")
        self.assertIn("must_not_assert", cases[0]["note"])

    def test_already_consumed_unknown_is_not_traced_twice(self):
        """已被别的兑付器引用的 unknown 不该重复留痕——重复会让清单虚高。"""
        consumed = {"已兑付项"}
        c = {"action": "1000107", "name": "t", "request_fields": [], "response_fields": [],
             "llm_facts_count": 1, "llm_relations": [],
             "llm_unknowns": [{"item": "已兑付项", "reason": "x"},
                              {"item": "未兑付项", "reason": "y"}]}
        out = L._unknown_gap_cases(c, dict(BASE), "1000107", consumed)
        self.assertEqual([x["id"] for x in out], ["LLM-UNK-02"])

    def test_every_unknown_gets_exactly_one_trace(self):
        """每条 unknown 至多一条留痕，不能一条 unknown 炸出多条。"""
        cases = self._run([{"item": "A", "reason": "x"}, {"item": "A", "reason": "x"}])
        ids = [x["id"] for x in cases if str(x["id"]).startswith("LLM-UNK")]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
