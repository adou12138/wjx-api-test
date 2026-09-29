# -*- coding: utf-8 -*-
"""v4.16.0 五项优化的单元测试。

覆盖：字段族截断（field_family / pick_by_family）、双次抽取稳定性比对（compare）、
分片 prep（_shard_blocks / _shard_fields / 分片提示词）、分片合并（combine_facts）、
relation alias 方向判定与字段级兜底、禁用词与默认值兑付。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import field_semantics as FS            # noqa: E402
import llm_semantics as LS              # noqa: E402
import llm_fact_cases as L              # noqa: E402


BASE = {"title": "首页标题", "creater": "main_acct", "html_content": "<p>x</p>"}


def _field(name, **kw):
    f = {"name": name, "type": "string", "required": False}
    f.update(kw)
    return f


# ---------------------------------------------------------------- 字段族
class TestFieldFamily(unittest.TestCase):

    def test_role_is_strongest_signal(self):
        self.assertEqual(FS.field_family({"name": "creater",
                                          "llm_role": "operator_identity"}),
                         "role:operator_identity")

    def test_semantics_before_name_stem(self):
        self.assertEqual(FS.field_family({"name": "foo_id", "semantics": ["code"]}),
                         "sem:code")

    def test_common_suffix_groups_fields(self):
        self.assertEqual(FS.field_family({"name": "item_id"}),
                         FS.field_family({"name": "item_no"}))

    def test_plain_name_is_own_family(self):
        self.assertEqual(FS.field_family({"name": "title"}), "name:title")


class TestPickByFamily(unittest.TestCase):

    def test_small_input_untouched(self):
        fs = [_field("a"), _field("b")]
        self.assertEqual(FS.pick_by_family(fs, 5), fs)

    def test_every_family_gets_a_representative(self):
        # item_id/item_no/item_num 同词干一族（3 个字段）+ 三个单字段族：
        # 6 字段取 4，四个族各保一个代表，同族不会三席全占
        fs = [_field("item_id"), _field("item_no"), _field("item_num"),
              _field("title"), _field("status", semantics=["state"]), _field("x")]
        names = [f["name"] for f in FS.pick_by_family(fs, 4)]
        self.assertEqual(len(names), 4)
        self.assertEqual(len(set(names)), 4)
        self.assertEqual(sum(1 for n in names if n.startswith("item")), 1)
        self.assertIn("title", names)
        self.assertIn("status", names)
        self.assertIn("x", names)

    def test_quota_leftover_filled_in_original_order(self):
        fs = [_field("a_id"), _field("b_id"), _field("t1"), _field("t2")]
        names = [f["name"] for f in FS.pick_by_family(fs, 3)]
        # 两族各一个代表（a_id, t1），剩一席按原序补 b_id
        self.assertEqual(names, ["a_id", "b_id", "t1"])

    def test_result_preserves_original_order(self):
        fs = [_field("x"), _field("a_id"), _field("b_id"), _field("title")]
        picked = FS.pick_by_family(fs, 3)
        idx = [fs.index(f) for f in picked]
        self.assertEqual(idx, sorted(idx))


# ---------------------------------------------------------------- compare
class TestCompare(unittest.TestCase):

    def _facts(self, role="display_name", max_len=100, alias_of="", required=None):
        return {"fields": {"title": {
            "role": role, "confidence": "explicit", "required": required,
            "evidence": "标题", "constraints": {"max_len": max_len, "alias_of": alias_of}}},
            "relations": [{"type": "url_compose", "between": ["domain", "path"]}],
            "unknowns": [{"item": "唯一键", "reason": "契约未声明"}]}

    def test_identical_facts_fully_agree(self):
        rows = LS.compare(self._facts(), self._facts())
        self.assertEqual([r for r in rows if r[2] == "分歧"], [])
        self.assertEqual([r for r in rows if r[2] == "仅单次抽到"], [])

    def test_disagreement_is_reported_with_both_values(self):
        rows = LS.compare(self._facts(max_len=100), self._facts(max_len=200))
        diff = [r for r in rows if r[2] == "分歧"]
        self.assertEqual(len(diff), 1)
        self.assertIn("100", diff[0][3])
        self.assertIn("200", diff[0][3])

    def test_single_side_field_is_flagged(self):
        a = self._facts()
        b = self._facts()
        del b["fields"]["title"]
        rows = LS.compare(a, b)
        self.assertEqual(rows[0][2], "仅单次抽到")

    def test_relations_and_unknowns_sets_compared(self):
        b = self._facts()
        b["relations"] = []
        rows = LS.compare(self._facts(), b)
        rel_row = next(r for r in rows if r[0] == "relations")
        self.assertEqual(rel_row[2], "分歧")

    def test_enum_compared_as_value_set(self):
        a = self._facts()
        b = self._facts()
        a["fields"]["title"]["constraints"]["enum"] = [[0, "网页"], [1, "海报"]]
        b["fields"]["title"]["constraints"]["enum"] = [[1, "海报"], [0, "网页"]]
        rows = LS.compare(a, b)
        # 顺序不同不算分歧
        self.assertEqual([r for r in rows if r[0] == "title" and r[1] == "enum"][0][2],
                         "一致")


# ---------------------------------------------------------------- 分片 prep
class TestShardPrep(unittest.TestCase):

    def test_blocks_are_split_by_paragraph(self):
        text = "段一" * 100 + "\n\n" + "段二" * 100 + "\n\n" + "段三" * 100
        shards = LS._shard_blocks(text, 250)
        self.assertGreaterEqual(len(shards), 2)
        for s in shards:
            self.assertLessEqual(len(s), 250)

    def test_no_paragraph_halved(self):
        text = "\n\n".join("段落%d：" % i + "内容" * 30 for i in range(4))
        shards = LS._shard_blocks(text, 200)
        rejoined = "\n\n".join(shards)
        for i in range(4):
            self.assertIn("段落%d：" % i, rejoined)

    def test_shard_fields_assigned_by_text_presence(self):
        contract = {"request_fields": [_field("alpha"), _field("beta")]}
        subset = LS._shard_fields(contract, "这里只提到 alpha 字段的说明")
        self.assertEqual(subset, {"alpha"})

    def test_shard_prompt_limits_field_list(self):
        contract = {"name": "接口", "action": "1", "endpoint": "/x",
                    "request_fields": [_field("alpha"), _field("beta")]}
        p = LS.build_prompt(contract, "", shard=("alpha 的说明文字", 1, 2))
        self.assertIn("alpha[请求]", p)
        self.assertNotIn("beta[请求]", p)
        self.assertIn("分片抽取（第 1/2 片）", p)

    def test_unsharded_prompt_keeps_full_field_list(self):
        contract = {"name": "接口", "action": "1", "endpoint": "/x",
                    "request_fields": [_field("alpha"), _field("beta")]}
        p = LS.build_prompt(contract, "短的原文", limit=6000)
        self.assertIn("alpha[请求]", p)
        self.assertIn("beta[请求]", p)


# ---------------------------------------------------------------- combine
class TestCombineFacts(unittest.TestCase):

    def test_union_of_disjoint_fields(self):
        a = {"fields": {"x": {"role": "display_name"}}, "relations": [], "unknowns": []}
        b = {"fields": {"y": {"role": "body_content"}}, "relations": [], "unknowns": []}
        c = LS.combine_facts([a, b])
        self.assertEqual(set(c["fields"]), {"x", "y"})

    def test_conflict_keeps_first_and_keeps_going(self):
        a = {"fields": {"x": {"role": "display_name"}}, "relations": [], "unknowns": []}
        b = {"fields": {"x": {"role": "body_content"}}, "relations": [], "unknowns": []}
        c = LS.combine_facts([a, b])
        self.assertEqual(c["fields"]["x"]["role"], "display_name")

    def test_unknowns_deduped_by_item(self):
        a = {"fields": {}, "relations": [],
             "unknowns": [{"item": "唯一键", "reason": "r"}]}
        b = {"fields": {}, "relations": [],
             "unknowns": [{"item": "唯一键", "reason": "r"}, {"item": "时区", "reason": "r"}]}
        c = LS.combine_facts([a, b])
        self.assertEqual(sorted(u["item"] for u in c["unknowns"]), ["唯一键", "时区"])

    def test_relations_concatenated(self):
        a = {"fields": {}, "relations": [{"type": "url_compose", "between": ["a", "b"]}],
             "unknowns": []}
        b = {"fields": {}, "relations": [{"type": "fallback_chain", "between": ["c", "d"]}],
             "unknowns": []}
        self.assertEqual(len(LS.combine_facts([a, b])["relations"]), 2)


# ------------------------------------------- alias 方向判定与字段级兜底
class TestAliasDirectionFallback(unittest.TestCase):

    def _contract(self):
        return {"request_fields": [_field("html"), _field("html_content")]}

    def test_role_alias_determines_direction(self):
        facts = {"fields": {"html_content": {"role": "alias", "evidence": "兼容参数"}},
                 "relations": [{"type": "alias", "between": ["html", "html_content"],
                                "evidence": "兼容参数", "confidence": "explicit"}]}
        rows, clean = LS.verify(facts, self._contract(), "兼容参数")
        self.assertEqual(clean["relations"][0]["alias_direction"],
                         ["html_content", "html"])

    def test_alias_of_cross_confirms_direction(self):
        facts = {"fields": {"html_content": {
                    "role": "body_content", "evidence": "兼容参数",
                    "constraints": {"alias_of": "html"}}},
                 "relations": [{"type": "alias", "between": ["html_content", "html"],
                                "evidence": "兼容参数", "confidence": "explicit"}]}
        _, clean = LS.verify(facts, self._contract(), "兼容参数")
        self.assertEqual(clean["relations"][0]["alias_direction"],
                         ["html_content", "html"])

    def test_merge_backfills_field_alias_of(self):
        facts = {"fields": {"html_content": {"role": "alias", "evidence": "兼容参数"}},
                 "relations": [{"type": "alias", "between": ["html", "html_content"],
                                "evidence": "兼容参数", "confidence": "explicit"}]}
        _, clean = LS.verify(facts, self._contract(), "兼容参数")
        c = LS.merge(self._contract(), clean)
        f = next(f for f in c["request_fields"] if f["name"] == "html_content")
        self.assertEqual(f["llm_constraints"]["alias_of"], "html")

    def test_merge_does_not_overwrite_explicit_alias_of(self):
        facts = {"fields": {"html_content": {
                    "role": "alias", "evidence": "兼容参数",
                    "constraints": {"alias_of": "html"}}},
                 "relations": [{"type": "alias", "between": ["html_content", "html"],
                                "evidence": "兼容参数", "confidence": "explicit"}]}
        _, clean = LS.verify(facts, self._contract(), "兼容参数")
        c = LS.merge(self._contract(), clean)
        f = next(f for f in c["request_fields"] if f["name"] == "html_content")
        self.assertEqual(f["llm_constraints"]["alias_of"], "html")

    def test_relation_only_alias_still_yields_alias_cases(self):
        """模型只给 relation alias 时不丢别名用例——verify 判方向，merge 兜底回填。"""
        facts = {"fields": {"html_content": {"role": "alias", "evidence": "兼容参数"}},
                 "relations": [{"type": "alias", "between": ["html", "html_content"],
                                "evidence": "兼容参数", "confidence": "explicit"}]}
        _, clean = LS.verify(facts, self._contract(), "兼容参数")
        c = LS.merge(self._contract(), clean)
        c["llm_facts_count"] = len(clean["fields"])
        cases = L.run(c, {"html": "a", "html_content": "b"}, "1", {})
        ids = [x["id"] for x in cases]
        self.assertIn("LLM-ALIAS-HTML_CONTENT-ONLY", ids)


# ---------------------------------------------------------------- 禁用词/默认值
class TestForbidAndDefault(unittest.TestCase):

    def _contract(self, forbid, default, evidence):
        return {"llm_facts_count": 1, "llm_relations": [], "llm_unknowns": [],
                "request_fields": [_field(
                    "title", llm_role="display_name", llm_evidence=evidence,
                    llm_constraints={"forbid": forbid, "default": default})]}

    def test_evidenced_forbid_generates_reject_case(self):
        c = self._contract(["问卷星"], "", "不能包含\"问卷星\"")
        cases = L.run(c, dict(BASE), "1", {})
        self.assertEqual([x["id"] for x in cases if x["id"].startswith("LLM-FORBID")],
                         ["LLM-FORBID-TITLE-01"])
        fc = next(x for x in cases if x["id"] == "LLM-FORBID-TITLE-01")
        self.assertEqual(fc["expect"]["success"], False)
        self.assertEqual(fc["params"]["title"], "问卷星")

    def test_unsupported_forbid_is_not_paid(self):
        """forbid 值不在 evidence 里 = 模型凭惯例推的，不兑付（防假缺陷）。"""
        c = self._contract(["脚本"], "", "不能包含\"问卷星\"")
        cases = L.run(c, dict(BASE), "1", {})
        self.assertEqual([x for x in cases if x["id"].startswith("LLM-FORBID")], [])

    def test_evidenced_default_generates_case_without_the_field(self):
        c = self._contract([], "默认标题", "未传时使用\"默认标题\"")
        cases = L.run(c, dict(BASE), "1", {})
        dc = next(x for x in cases if x["id"] == "LLM-DEFAULT-TITLE")
        self.assertNotIn("title", dc["params"])
        self.assertEqual(dc["expect"]["success"], True)
        self.assertIn("默认标题", dc["note"])

    def test_unsupported_default_is_not_paid(self):
        c = self._contract([], "神秘值", "标题字段")
        cases = L.run(c, dict(BASE), "1", {})
        self.assertEqual([x for x in cases if x["id"].startswith("LLM-DEFAULT")], [])


# ------------------------------------------------- 字段级 fallback_chain 兜底
class TestFieldLevelFallbackChain(unittest.TestCase):

    def _contract(self):
        return {"llm_facts_count": 1, "llm_relations": [], "llm_unknowns": [],
                "request_fields": [
                    _field("title", llm_role="display_name",
                           llm_evidence="未传时读取html，仍为空时使用\"默认标题\"",
                           llm_constraints={"fallback_chain": ["html_content"]}),
                    _field("html_content", llm_role="body_content",
                           llm_evidence="富文本", llm_constraints={})]}

    def test_field_level_chain_generates_cases(self):
        cases = L.run(self._contract(), dict(BASE), "1", {})
        self.assertIn("LLM-FALLBACK-TITLE-EMPTY", [x["id"] for x in cases])

    def test_relation_and_field_level_do_not_duplicate(self):
        """同一对 (target, src) 两种表达只兑付一次——重复即同参用例翻倍。"""
        c = self._contract()
        c["llm_relations"] = [{"type": "fallback_chain", "between": ["title", "html_content"],
                               "evidence": "未传时读取html", "confidence": "explicit"}]
        cases = L.run(c, dict(BASE), "1", {})
        empty = [x for x in cases if x["id"] == "LLM-FALLBACK-TITLE-EMPTY"]
        self.assertEqual(len(empty), 1)

    def test_self_reference_chain_is_ignored(self):
        c = self._contract()
        c["request_fields"][0]["llm_constraints"]["fallback_chain"] = ["title"]
        cases = L.run(c, dict(BASE), "1", {})
        self.assertEqual([x for x in cases if x["id"].startswith("LLM-FALLBACK")], [])


if __name__ == "__main__":
    unittest.main()
