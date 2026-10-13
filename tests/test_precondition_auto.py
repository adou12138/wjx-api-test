# -*- coding: utf-8 -*-
"""规则直提 --auto（v4.28.0）测试：明确「前置话术 + 邻近 action 号」才直提，免 LLM。

锁住：① 明确形态直提成功、evidence 是原文子串、过 verify；② 话术命中但邻近无 action 号不提；
③ 自引用跳过；④ 无前置话术不提（不把普通 action 提及当依赖）；⑤ 产出仍是 candidate。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import precondition_scan as P  # noqa: E402


class TestAutoFacts(unittest.TestCase):
    def test_extract_clear_dep(self):
        cands = [{"case_no": "T01", "action": "1000108",
                  "response_excerpt": "发布态不能直接更新，请先调用 1000102 修改问卷状态"}]
        facts = P.auto_facts(cands)
        self.assertEqual(len(facts["dependencies"]), 1)
        d = facts["dependencies"][0]
        self.assertEqual(d["target_action"], "1000102")
        self.assertEqual(d["case_no"], "T01")
        self.assertIn(d["evidence"], cands[0]["response_excerpt"], "evidence 必须是原文子串")

    def test_skip_when_no_action_near_hint(self):
        cands = [{"case_no": "T01", "action": "1000108",
                  "response_excerpt": "请先完成前置条件，但未给出任何接口号"}]
        self.assertEqual(P.auto_facts(cands)["dependencies"], [], "邻近无 action 号不猜")

    def test_skip_self_reference(self):
        cands = [{"case_no": "T01", "action": "1000102",
                  "response_excerpt": "请先调用 1000102 完成初始化"}]
        self.assertEqual(P.auto_facts(cands)["dependencies"], [], "自引用跳过")

    def test_no_fire_without_precondition_phrase(self):
        # 有 action 号但无前置话术——不能把普通 action 提及当依赖
        cands = [{"case_no": "T01", "action": "1000108",
                  "response_excerpt": "参数错误，参见 action 1000102 文档"}]
        self.assertEqual(P.auto_facts(cands)["dependencies"], [])

    def test_auto_fact_passes_verify_as_candidate(self):
        cands = [{"case_no": "T01", "action": "1000108",
                  "response_excerpt": "请先调用 1000102 修改问卷状态"}]
        facts = P.auto_facts(cands)
        rows, clean = P.verify(facts, cands, None)
        self.assertEqual(len(clean), 1, "明确的直提事实应过机械校验")
        self.assertEqual(clean[0]["target_action"], "1000102")
        self.assertNotEqual(clean[0].get("status"), "confirmed", "直提产出仍是候选，不是已确认")

    def test_eight_digit_number_not_extracted(self):
        """8 位数字（时间戳/长 ID）不应截出错误的 7 位 action——v4.28.0 审查修复。

        无数字边界时 `1\d{6}` 会从 10001028 截出 1000102 且骗过 verify 的 fullmatch，
        形成错误候选依赖。
        """
        cands = [{"case_no": "T01", "action": "1000108",
                  "response_excerpt": "请先调用 10001028 完成（8 位，不是合法 action）"}]
        self.assertEqual(P.auto_facts(cands)["dependencies"], [], "8 位数字不应截出 7 位假 action")

    def test_seven_digit_still_extracted(self):
        cands = [{"case_no": "T01", "action": "1000108",
                  "response_excerpt": "请先调用 1000102 修改状态"}]
        self.assertEqual(len(P.auto_facts(cands)["dependencies"]), 1, "边界修复不影响正常 7 位号")


if __name__ == "__main__":
    unittest.main()
