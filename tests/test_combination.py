# -*- coding: utf-8 -*-
"""机械组合覆盖（B-COMBO-*，v4.25.0）测试。

锁住：① 多字段同时取各自单个即非法的值 → 生成组合用例、预期拒绝、不要求特定字段；
② 有界（字段代表数 + 组合总数双上限，防 n² 爆炸）；③ B-COMBO-* 归 probe 层、
走来源型身份证、不进对账分母（不造假缺口）；④ 单字段/无可解析类型的字段不生成。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import case_generator  # noqa: E402
import check_key  # noqa: E402
import discovery_profile  # noqa: E402


def _gen(fields):
    return case_generator.gen_combination_cases({"request_fields": fields}, {}, "act")


class _Reset(unittest.TestCase):
    def setUp(self):
        discovery_profile.activate("conservative")

    def tearDown(self):
        discovery_profile.activate("conservative")


class TestCombinationGeneration(_Reset):
    def test_pair_generated_combined_invalid(self):
        cases = _gen([{"name": "a", "type": "integer", "required": True},
                      {"name": "b", "type": "boolean", "required": True}])
        self.assertEqual(len(cases), 1)
        c = cases[0]
        self.assertTrue(c["id"].startswith("B-COMBO-"))
        self.assertFalse(c["expect"]["success"], "组合后应被拒绝")
        self.assertNotIn("err_keywords", c["expect"], "不要求错误提及特定字段（后端可能只报第一个）")
        self.assertIn("a", c["params"])
        self.assertIn("b", c["params"])

    def test_bounded_no_explosion(self):
        fields = [{"name": "f%d" % i, "type": "integer", "required": True} for i in range(20)]
        cases = _gen(fields)
        self.assertLessEqual(len(cases), case_generator.COMBO_MAX_CASES)
        # 代表字段数也受限：C(COMBO_MAX_FIELDS, 2) 才是理论上限
        import math
        self.assertLessEqual(len(cases), math.comb(case_generator.COMBO_MAX_FIELDS, 2))

    def test_single_field_no_combo(self):
        self.assertEqual(_gen([{"name": "a", "type": "integer", "required": True}]), [])

    def test_unparseable_type_excluded(self):
        # 无类型 token 的"类型"（解析噪音）造不出非法值，不参与组合
        self.assertEqual(_gen([{"name": "a", "type": "问卷id"},
                               {"name": "b", "type": "www.wjx.cn"}]), [])


class TestCombinationReconcile(_Reset):
    def test_probe_tier_and_not_in_denominator(self):
        cases = _gen([{"name": "a", "type": "integer", "required": True},
                      {"name": "b", "type": "boolean", "required": True}])
        self.assertEqual(case_generator._tier_of(cases[0]), "probe")
        mech = {p for p, _ in check_key._MECH_PREFIXES}
        self.assertNotIn("B-COMBO-", mech, "组合用例不是单字段约束，不进对账分母")
        key = check_key.derive_key(cases[0])
        self.assertTrue(key.startswith("generator:"), "B-COMBO 应走来源型身份证：%s" % key)

    def test_no_false_gap_when_added(self):
        """组合用例并入后，reconcile 不得因它报出 below/above 之类的假缺口。"""
        contract = {"request_fields": [
            {"name": "a", "type": "integer", "required": True, "max_len": 10},
            {"name": "b", "type": "boolean", "required": True}]}
        cases = case_generator.gen_field_mutations(contract, {}, "act") + _gen(contract["request_fields"])
        rec = check_key.reconcile(cases, contract)
        missing = {r["key"] for r in rec["missing_keys"]}
        self.assertNotIn("wrong_type:a", missing)
        self.assertNotIn("missing_required:a", missing)


if __name__ == "__main__":
    unittest.main()
