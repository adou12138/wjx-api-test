import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import llm_semantics as LS             # noqa: E402
from contract_parser import load_contract  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
SIMPLE = str(FIX / "simple_contract.md")
SOJUMP = str(FIX / "sojump_contract.md")


def _contract(path=SIMPLE):
    return load_contract(path)


def _text(path=SIMPLE):
    return Path(path).read_text(encoding="utf-8")


class TestPrep(unittest.TestCase):
    def test_prompt_has_field_list_and_excerpt(self):
        p = LS.build_prompt(_contract(), _text())
        self.assertIn("字段清单", p)
        self.assertIn("契约原文摘录", p)
        self.assertIn("连续子串", p)
        self.assertIn("unknowns", p)
        self.assertIn("must_not_assert", p)

    def test_prompt_lists_every_field(self):
        c = _contract()
        p = LS.build_prompt(c, _text())
        names = [f["name"] for f in (c.get("request_fields") or [])]
        for n in names:
            self.assertIn(n, p)


class TestVerify(unittest.TestCase):
    def _rows(self, facts):
        rows, clean = LS.verify(facts, _contract(), _text())
        return rows, clean

    def test_hallucinated_field_blocked(self):
        rows, clean = self._rows({"fields": {
            "ghost_field": {"role": "display_name", "evidence": "任意"}}})
        self.assertTrue(any(r[2] == LS.BLOCKED and r[0] == "ghost_field" for r in rows))
        self.assertNotIn("ghost_field", clean["fields"])

    def test_evidence_not_in_contract_blocked(self):
        rows, _ = self._rows({"fields": {
            "title": {"role": "display_name", "evidence": "这是我编的一句话"}}})
        self.assertTrue(any(r[2] == LS.BLOCKED and r[1] == "evidence" for r in rows))

    def test_missing_evidence_blocked(self):
        rows, _ = self._rows({"fields": {"title": {"role": "display_name"}}})
        self.assertTrue(any(r[2] == LS.BLOCKED for r in rows))

    def test_unknown_role_rejected(self):
        rows, _ = self._rows({"fields": {
            "title": {"role": "绝密角色", "evidence": "标题"}}})
        self.assertTrue(any(r[2] == LS.MISMATCH for r in rows))

    def test_relation_with_unknown_field_blocked(self):
        rows, clean = self._rows({"fields": {}, "relations": [
            {"type": "alias", "between": ["title", "不存在字段"], "evidence": "标题"}]})
        self.assertTrue(any(r[2] == LS.BLOCKED for r in rows))
        self.assertEqual(clean["relations"], [])

    def test_rejected_fact_is_dropped_entirely(self):
        """半对半错的事实整条丢弃，不做部分保留。

        用契约明确写出长度的那份 fixture（title max_len=100），
        否则契约没给基准时无法证伪，属有意放行而非缺陷。
        """
        rows, clean = LS.verify(
            {"fields": {"title": {"role": "display_name", "evidence": "主页标题",
                                  "constraints": {"max_len": 99999}}}},
            load_contract(SOJUMP), Path(SOJUMP).read_text(encoding="utf-8"))
        self.assertTrue(any(r[2] == LS.MISMATCH for r in rows))
        self.assertNotIn("title", clean["fields"])


class TestMerge(unittest.TestCase):
    def test_merge_writes_field_keys(self):
        c = _contract()
        facts = {"fields": {"title": {
            "role": "display_name", "evidence": "标题", "confidence": "explicit",
            "constraints": {}, "test_implications": ["边界"]}}}
        rows, clean = LS.verify(facts, c, _text())
        if any(r[2] not in LS.PASS_STATES for r in rows):
            self.skipTest("fixture 契约不含可引用的「标题」原文，跳过字段级合并断言")
        LS.merge(c, clean)
        f = next(f for f in c["request_fields"] if f["name"] == "title")
        self.assertEqual(f.get("llm_role"), "display_name")
        self.assertEqual(f.get("llm_evidence"), "标题")

    def test_merge_keeps_parser_constraints(self):
        """语义层只做补充，不覆盖解析器已有的结构化约束。"""
        c = _contract()
        before = {f["name"]: f.get("max_len") for f in c["request_fields"]}
        LS.merge(c, {"fields": {}, "relations": [], "unknowns": []})
        after = {f["name"]: f.get("max_len") for f in c["request_fields"]}
        self.assertEqual(before, after)

    def test_contract_level_keys(self):
        c = _contract()
        LS.merge(c, {"fields": {}, "relations": [{"type": "alias", "between": []}],
                     "unknowns": [{"item": "x"}]})
        self.assertEqual(len(c["llm_relations"]), 1)
        self.assertEqual(len(c["llm_unknowns"]), 1)
        self.assertEqual(c["llm_facts_count"], 0)


class TestDegrade(unittest.TestCase):
    def test_no_facts_keeps_contract_untouched(self):
        """不传事实时纯规则链路照常工作——接入是增量不是替换。"""
        import case_generator as CG
        doc = CG.generate(SIMPLE)
        self.assertIsNone(doc.get("llm_verify_report"))
        self.assertFalse(any(str(c.get("id")) == "A08" for c in doc["cases"]))


class TestSemanticsFix(unittest.TestCase):
    """误判修复：编号 / 相对路径 / 域名不得被当作可直连 URL。"""
    def test_code_beats_url(self):
        import field_semantics as FS
        self.assertIn("code", FS.infer({"name": "sid", "desc": "短链接编号"}))
        self.assertNotIn("url", FS.infer({"name": "sid", "desc": "短链接编号"}))

    def test_relative_path_not_absolute(self):
        import field_semantics as FS
        f = {"name": "pc_path", "desc": "PC端问卷Url相对路径"}
        self.assertFalse(FS.is_absolute_url(f))

    def test_domain_not_absolute(self):
        import field_semantics as FS
        f = {"name": "activity_domain", "desc": "问卷访问域名"}
        self.assertFalse(FS.is_absolute_url(f))

    def test_real_url_still_absolute(self):
        import field_semantics as FS
        f = {"name": "download_url", "desc": "下载链接，可直接打开"}
        self.assertTrue(FS.is_absolute_url(f))


class TestPassStates(unittest.TestCase):
    """通过分三态：按契约值核验一致 / 提交了但契约无值 / 未提交可机械比对的约束。

    三者必须在报告里可分。如果只要「有 evidence」就记 VERIFIED，「核验通过 N 条」
    会被读成 N 项都验过值——而实测里 19 条中真正比对过数值的只有 3 条。
    """

    def _gt(self, max_len=None, enum=None):
        return {"request_fields": [{"name": "title", "type": "string",
                                    "required": False, "max_len": max_len, "enum": enum}]}

    def _run(self, contract, constraints, evidence="这是一段足够长的契约原文证据",
             role="display_name"):
        facts = {"fields": {"title": {
            "role": role, "evidence": evidence, "constraints": constraints}}}
        return LS.verify(facts, contract, evidence)[0]

    def test_matched_constraint_is_verified(self):
        rows = self._run(self._gt(max_len=100), {"max_len": 100})
        self.assertEqual(rows[0][2], LS.PASS_VERIFIED)

    def test_no_checkable_constraint_is_evidence_only(self):
        """只写了 role、没提交任何可比对约束 → 必须记 EVIDENCE_ONLY。

        这是「核验数虚高」的直接防线：没有它，只验了证据的事实会和
        「按值核验一致」混成一态，报告里的核验条数就不可信。
        """
        rows = self._run(self._gt(), {})
        self.assertEqual(rows[0][2], LS.PASS_EVIDENCE_ONLY)
        self.assertIn("无可机械比对的约束", rows[0][3])

    def test_contract_has_no_value_but_evidence_supports_is_unverified(self):
        """契约没解析出长度，但 evidence 里确有该数字 → 放行但记 UNVERIFIED。"""
        rows = self._run(self._gt(max_len=None), {"max_len": 99999},
                         evidence="至少可以写最长99999个字符这样的表述")
        self.assertEqual(rows[0][2], LS.PASS_UNVERIFIED)
        self.assertIn("max_len", rows[0][3])

    def test_contract_has_no_value_and_evidence_lacks_it_is_mismatch(self):
        """契约无值、evidence 里也没有 → 这个数字是凭空来的，必须拦。"""
        rows = self._run(self._gt(max_len=None), {"max_len": 99999},
                         evidence="只说长度受限，没给具体数字")
        self.assertEqual(rows[0][2], LS.MISMATCH)
        self.assertIn("无据", rows[0][3])

    def test_non_list_enum_is_unverified(self):
        """形如 external_ref:3.8 的枚举引用不是清单，无值可比对。"""
        rows = self._run(self._gt(enum=None), {"enum": "external_ref:3.8 问卷状态"},
                         evidence="参见3.8 问卷状态说明")
        self.assertEqual(rows[0][2], LS.PASS_UNVERIFIED)
        self.assertIn("enum", rows[0][3])

    def test_enum_subset_of_contract_is_verified(self):
        rows = self._run(self._gt(enum=[[0, "网页"], [1, "海报"]]),
                         {"enum": [[0, "网页"]]}, evidence="0网页，1海报两种类型")
        self.assertEqual(rows[0][2], LS.PASS_VERIFIED)

    def test_enum_without_contract_value_needs_evidence(self):
        """契约无枚举值：声称的取值必须在 evidence 里出现，否则无据。"""
        rows = self._run(self._gt(enum=None), {"enum": [[0, "网页"], [7, "捏造"]]},
                         evidence="0网页类型说明")
        self.assertEqual(rows[0][2], LS.MISMATCH)
        self.assertIn("7", rows[0][3])

    def test_all_three_states_count_as_not_rejected(self):
        """拒收判定必须认三态，否则 EVIDENCE_ONLY 会被误当成拦截。"""
        facts = {"fields": {"title": {"role": "display_name",
                                      "evidence": "这是一段足够长的契约原文证据"}}}
        rows, clean = LS.verify(facts, self._gt(), "这是一段足够长的契约原文证据")
        self.assertEqual(rows[0][2], LS.PASS_EVIDENCE_ONLY)
        self.assertEqual([r for r in rows if r[2] not in LS.PASS_STATES], [])
        self.assertIn("title", clean["fields"])

    def test_weak_evidence_is_flagged(self):
        """过短的 evidence 仍可通过（可能是真事实），但必须标出来。"""
        rows = self._run(self._gt(), {}, evidence="签名")
        self.assertEqual(rows[0][2], LS.PASS_EVIDENCE_ONLY)
        self.assertIn("弱证据", rows[0][3])

    def test_alias_of_must_point_to_existing_field(self):
        """alias_of 驱动硬断言，指向不存在的字段必须拦。"""
        rows = self._run(self._gt(), {"alias_of": "ghost_field"})
        self.assertEqual(rows[0][2], LS.MISMATCH)
        self.assertIn("alias_of", rows[0][3])


class TestRelationEvidence(unittest.TestCase):
    """关系是跨字段推断，最需要证据；不应比字段分支更宽松。"""

    def _verify(self, rel, text):
        contract = {"request_fields": [{"name": "html", "type": "string"},
                                       {"name": "html_content", "type": "string"}]}
        return LS.verify({"relations": [rel]}, contract, text)

    def test_missing_evidence_is_blocked(self):
        rel = {"type": "alias", "between": ["html", "html_content"],
               "confidence": "explicit"}
        rows, clean = self._verify(rel, "任意原文")
        self.assertEqual(rows[0][2], LS.BLOCKED)
        self.assertEqual(clean["relations"], [])

    def test_evidence_not_in_text_is_blocked(self):
        rel = {"type": "alias", "between": ["html", "html_content"],
               "evidence": "捏造的证据", "confidence": "explicit"}
        rows, _ = self._verify(rel, "真实原文")
        self.assertEqual(rows[0][2], LS.BLOCKED)

    def test_inferred_relation_is_unverified_not_verified(self):
        """推断关系没有原文直接依据，不能记「比对一致」。"""
        rel = {"type": "url_compose", "between": ["html", "html_content"],
               "evidence": "相对路径", "confidence": "inferred"}
        rows, _ = self._verify(rel, "相对路径")
        self.assertEqual(rows[0][2], LS.PASS_UNVERIFIED)

    def test_explicit_relation_is_verified(self):
        """方向可机械判定（role 标明别名侧）的 explicit 关系记比对一致。"""
        contract = {"request_fields": [{"name": "html", "type": "string"},
                                       {"name": "html_content", "type": "string"}]}
        facts = {"fields": {"html_content": {"role": "alias", "evidence": "兼容参数"}},
                 "relations": [{"type": "alias", "between": ["html", "html_content"],
                                "evidence": "兼容参数", "confidence": "explicit"}]}
        rows, clean = LS.verify(facts, contract, "兼容参数")
        rel_row = next(r for r in rows if str(r[0]).startswith("relation:"))
        self.assertEqual(rel_row[2], LS.PASS_VERIFIED)
        self.assertEqual(clean["relations"][0].get("alias_direction"),
                         ["html_content", "html"])

    def test_alias_relation_without_direction_goes_to_unknowns(self):
        """方向判不出的 alias 关系不按数组顺序猜——转 unknowns 留痕。

        别名用例断言「只走别名也应生效」，方向反了断言完全错位；
        between 的顺序是模型输出习惯，不是契约事实。
        """
        rel = {"type": "alias", "between": ["html", "html_content"],
               "evidence": "兼容参数", "confidence": "explicit"}
        rows, clean = self._verify(rel, "兼容参数")
        self.assertEqual(rows[0][2], LS.MISMATCH)
        self.assertEqual([r for r in clean["relations"] if r.get("type") == "alias"], [])
        self.assertTrue(any("方向未定" in (u.get("item") or "")
                            for u in clean["unknowns"]))

    def test_single_field_relation_is_blocked(self):
        rel = {"type": "alias", "between": ["html"], "evidence": "兼容参数",
               "confidence": "explicit"}
        rows, _ = self._verify(rel, "兼容参数")
        self.assertEqual(rows[0][2], LS.BLOCKED)


class TestUnknownsLimit(unittest.TestCase):
    """unknowns 无上限会撑长清单并稀释有效信息。"""

    def test_duplicates_dropped_and_capped(self):
        facts = {"unknowns": [{"item": "重复项"}] * 3
                 + [{"item": "缺口%d" % i} for i in range(30)]}
        rows, clean = LS.verify(facts, {"request_fields": []}, "x")
        items = [u["item"] for u in clean["unknowns"]]
        self.assertEqual(len(items), len(set(items)), "必须去重")
        self.assertLessEqual(len(items), LS.MAX_UNKNOWNS)
        self.assertTrue(any("上限" in r[3] for r in rows))


class TestExcerptCoverage(unittest.TestCase):
    """摘录截断必须显式暴露：截断区外的字段没有可校验的 evidence。"""

    def test_no_truncation_returns_zero_total(self):
        text, total = LS._excerpt("短契约")
        self.assertEqual(total, 0)
        self.assertNotIn("截断", text)

    def test_truncation_reports_total(self):
        text, total = LS._excerpt("很长" * 100, limit=10)
        self.assertEqual(total, 200)
        self.assertIn("截断", text)

    def test_prompt_warns_when_truncated(self):
        c = {"request_fields": [{"name": "a", "type": "string"}]}
        p = LS.build_prompt(c, "字段说明很长" * 100, limit=20)
        self.assertIn("摘录已截断", p)
        self.assertIn("unknowns", p)

    def test_prompt_has_no_warning_when_fits(self):
        c = {"request_fields": [{"name": "a", "type": "string"}]}
        p = LS.build_prompt(c, "字段 a：说明")
        self.assertNotIn("摘录已截断", p)


class TestMergeReplacesWholesale(unittest.TestCase):
    def test_stale_llm_keys_are_cleared(self):
        """本次没事实的字段要清掉上一轮 llm_* 残留，避免新旧混合态。"""
        contract = {"request_fields": [
            {"name": "a", "type": "string", "llm_role": "旧角色",
             "llm_evidence": "旧证据", "llm_implications": ["旧建议"]},
            {"name": "b", "type": "string"}]}
        clean = {"fields": {"b": {"role": "display_name"}},
                 "relations": [], "unknowns": []}
        LS.merge(contract, clean)
        a = contract["request_fields"][0]
        for k in ("llm_role", "llm_evidence", "llm_implications"):
            self.assertNotIn(k, a)
        self.assertEqual(contract["request_fields"][1]["llm_role"], "display_name")


if __name__ == "__main__":
    unittest.main()
