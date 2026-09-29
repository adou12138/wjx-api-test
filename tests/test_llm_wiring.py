#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""语义层接入层（case_generator）的容错与可审计性测试。

关注两件事：
1. 模型输出格式不听话（代码块围栏、前后解释、顶层非对象、彻底坏掉）时，
   不能把流程打断，也不能静默当成「这次没接语义层」——必须降级且出声；
2. 「抽取到的键」与「真正有消费方的键」必须能分开列，
   否则「并入 N 条事实」的口径会掩盖「实际只兑现了 M 条」。
"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import case_generator as G  # noqa: E402


def _write(directory, name, text):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


EMPTY_FACTS = {"fields": {}, "relations": [], "unknowns": []}


class TestFactsJsonTolerance(unittest.TestCase):
    """模型常不遵守「只输出 JSON」；格式容错不该让整次抽取白费。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_plain_json(self):
        raw = json.dumps({"fields": {"a": 1}}, ensure_ascii=False)
        facts, note = G._load_facts_json(_write(self.tmp, "a.json", raw))
        self.assertEqual(facts, {"fields": {"a": 1}})
        self.assertEqual(note, "")

    def test_fenced_code_block(self):
        raw = ("结果如下：\n```json\n%s\n```\n以上。"
               % json.dumps(EMPTY_FACTS, ensure_ascii=False))
        facts, note = G._load_facts_json(_write(self.tmp, "b.json", raw))
        self.assertEqual(facts, EMPTY_FACTS)
        self.assertIn("代码块", note)

    def test_mixed_text_without_fence(self):
        raw = "说明文字\n%s\n结束语" % json.dumps(EMPTY_FACTS, ensure_ascii=False)
        facts, note = G._load_facts_json(_write(self.tmp, "c.json", raw))
        self.assertEqual(facts, EMPTY_FACTS)
        self.assertIn("截取", note)

    def test_broken_json_raises_value_error(self):
        with self.assertRaises(ValueError):
            G._load_facts_json(_write(self.tmp, "d.json", "完全不是 JSON"))

    def test_non_object_raises_or_returns_non_dict(self):
        """顶层是数组时解析本身成功，由调用方负责判定类型。"""
        facts, _ = G._load_facts_json(_write(self.tmp, "e.json", "[1,2,3]"))
        self.assertNotIsInstance(facts, dict)


class TestEnrichDegradesSafely(unittest.TestCase):
    """接入失败必须降级 + 出声；不能崩，也不能静默当成「没接」。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _enrich(self, text):
        return G._enrich_llm_facts(
            {"request_fields": [], "response_fields": []},
            _write(self.tmp, "f.json", text), "")

    def test_top_level_array_returns_error_not_crash(self):
        _, report = self._enrich("[1,2,3]")
        self.assertIn("error", report)
        self.assertIn("顶层", report["error"])

    def test_broken_json_returns_error_not_crash(self):
        _, report = self._enrich("不是 JSON")
        self.assertIn("error", report)

    def test_missing_file_returns_error_not_crash(self):
        contract = {"request_fields": [], "response_fields": []}
        _, report = G._enrich_llm_facts(
            contract, os.path.join(self.tmp, "nope.json"), "")
        self.assertIn("error", report)

    def test_no_facts_path_returns_none(self):
        contract = {"request_fields": [], "response_fields": []}
        _, report = G._enrich_llm_facts(contract, None, "")
        self.assertIsNone(report)

    def test_fenced_facts_are_accepted_not_silently_dropped(self):
        """带围栏的输入应被接受并说明，而不是被当成「没接语义层」。"""
        payload = {"fields": {}, "relations": [{"type": "alias",
                                                "between": ["a", "b"],
                                                "evidence": "e",
                                                "confidence": "explicit"}],
                   "unknowns": []}
        _, report = self._enrich("```json\n%s\n```" % json.dumps(payload,
                                                                ensure_ascii=False))
        self.assertNotIn("error", report)
        self.assertIn("代码块", report.get("note", ""))

    def test_report_carries_three_states(self):
        """报告必须分别给出三态计数，不能只给一个「核验通过」。"""
        _, report = self._enrich(json.dumps(EMPTY_FACTS, ensure_ascii=False))
        for key in ("verified_count", "unverified_count", "evidence_only_count",
                    "rejected_count", "fact_utilization"):
            self.assertIn(key, report)


class TestFactUtilization(unittest.TestCase):
    """「抽取到的键」与「有消费方的键」必须分开，避免并入量虚高。"""

    def test_consumed_and_archived_are_split(self):
        clean = {"fields": {"f": {"role": "publish_flag",
                                  "constraints": {"alias_of": "x",
                                                  "fallback_chain": ["y"],
                                                  "forbid": ["词"],
                                                  "default": "d",
                                                  "is_url": False},
                                  "evidence": "e",
                                  "test_implications": ["i"]}},
                 "relations": [], "unknowns": []}
        u = G._fact_utilization(clean)
        self.assertIn("constraints.alias_of", u["consumed"])
        self.assertIn("constraints.fallback_chain", u["consumed"])
        self.assertIn("constraints.forbid", u["consumed"])
        self.assertIn("constraints.default", u["consumed"])
        self.assertIn("test_implications", u["archived"])
        self.assertIn("constraints.is_url", u["archived"])

    def test_empty_keys_are_not_counted_as_produced(self):
        clean = {"fields": {"f": {"role": "", "evidence": "",
                                  "constraints": {}}},
                 "relations": [], "unknowns": []}
        u = G._fact_utilization(clean)
        self.assertNotIn("role", u["produced"])
        self.assertNotIn("evidence", u["produced"])

    def test_relations_and_unknowns_count_when_present(self):
        clean = {"fields": {}, "relations": [{"type": "alias"}],
                 "unknowns": [{"item": "x"}]}
        u = G._fact_utilization(clean)
        self.assertIn("relations", u["consumed"])
        self.assertIn("unknowns", u["consumed"])


if __name__ == "__main__":
    unittest.main()
