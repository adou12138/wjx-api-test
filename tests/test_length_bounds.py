# -*- coding: utf-8 -*-
"""长度下边界（min_len / below_minimum）与对账分母对齐的测试（v4.24.0）。

锁住四件事：
1. min_len >= 2 的字段生成 B-LEN-UNDER（低于下限应拒绝）+ B-LEN-MIN（等于下限应成功）；
2. min_len == 1 不生成 B-LEN-UNDER（低于值即空串，与空值/缺失语义重叠），分母也不声明 below_minimum；
3. 有枚举的字段只生成 B-LEN-UNDER（应拒绝，安全），不生成 B-LEN-MIN（业务字符串不在枚举内，
   构造"应成功"会变假缺陷）；
4. below_minimum 分母与生成器承诺严格对齐——生成了才进分母，缺 min_len 的字段不刷假缺口。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import case_generator  # noqa: E402
import check_key  # noqa: E402
import discovery_profile  # noqa: E402


def _keys(rec):
    return {r["key"] for r in rec}


class _Reset(unittest.TestCase):
    def setUp(self):
        discovery_profile.activate("conservative")

    def tearDown(self):
        discovery_profile.activate("conservative")


class TestMinLenGeneration(_Reset):
    def _gen(self, field):
        contract = {"request_fields": [field]}
        return case_generator.gen_field_mutations(contract, {}, "act")

    def test_min_len_ge2_generates_under_and_min(self):
        cases = self._gen({"name": "code", "type": "string", "required": True, "min_len": 6})
        under = [c for c in cases if c["id"] == "B-LEN-UNDER-code"]
        edge = [c for c in cases if c["id"] == "B-LEN-MIN-code"]
        self.assertEqual(len(under), 1, "min_len>=2 应生成低于下限用例")
        self.assertEqual(len(edge), 1, "无枚举字段应生成等于下限用例")
        self.assertFalse(under[0]["expect"]["success"], "低于下限应被拒绝")
        self.assertEqual(under[0]["expect"].get("err_keywords"), ["code"])
        self.assertTrue(edge[0]["expect"]["success"], "等于下限应成功")
        # 值长度正确（ASCII 业务形态，恰好 ml-1 / ml）
        self.assertEqual(len(under[0]["params"]["code"]), 5)
        self.assertEqual(len(edge[0]["params"]["code"]), 6)

    def test_min_len_eq1_no_under(self):
        cases = self._gen({"name": "x", "type": "string", "required": True, "min_len": 1})
        self.assertEqual([c for c in cases if c["id"].startswith("B-LEN-UNDER-")], [],
                         "min_len==1 的低于值是空串，与空值/缺失语义重叠，不应构造")
        self.assertEqual(len([c for c in cases if c["id"] == "B-LEN-MIN-x"]), 1,
                         "min_len==1 仍应生成等于下限（1 字符，应成功）")

    def test_min_len_with_enum_skips_success_edge(self):
        cases = self._gen({"name": "st", "type": "string", "required": False,
                           "min_len": 6, "enum": [(1, "a"), (2, "b")]})
        self.assertEqual(len([c for c in cases if c["id"] == "B-LEN-UNDER-st"]), 1,
                         "有枚举也应生成低于下限（应拒绝，安全）")
        self.assertEqual([c for c in cases if c["id"] == "B-LEN-MIN-st"], [],
                         "有枚举时业务字符串不在枚举内，构造'应成功'即假缺陷，须跳过")

    def test_no_min_len_no_bounds(self):
        cases = self._gen({"name": "t", "type": "string", "required": True, "max_len": 20})
        self.assertEqual([c for c in cases if c["id"].startswith("B-LEN-UNDER-")], [])
        self.assertEqual([c for c in cases if c["id"].startswith("B-LEN-MIN-")], [])


class TestMinLenDenominator(_Reset):
    def test_below_minimum_in_denominator_ge2(self):
        contract = {"request_fields": [{"name": "code", "type": "string", "required": True, "min_len": 6}]}
        self.assertIn("below_minimum:code", _keys(check_key.required_checks(contract)))

    def test_below_minimum_absent_eq1(self):
        contract = {"request_fields": [{"name": "x", "type": "string", "required": True, "min_len": 1}]}
        self.assertNotIn("below_minimum:x", _keys(check_key.required_checks(contract)))

    def test_no_min_len_no_false_gap(self):
        contract = {"request_fields": [{"name": "t", "type": "string", "required": True, "max_len": 20}]}
        self.assertNotIn("below_minimum:t", _keys(check_key.required_checks(contract)))

    def test_below_minimum_delivered_no_gap(self):
        contract = {"request_fields": [{"name": "code", "type": "string", "required": True, "min_len": 6}]}
        cases = case_generator.gen_field_mutations(contract, {}, "act")
        rec = check_key.reconcile(cases, contract)
        self.assertNotIn("below_minimum:code", _keys(rec["missing_keys"]),
                         "生成了 B-LEN-UNDER 就不该报 below_minimum 缺口")

    def test_at_minimum_not_in_denominator(self):
        """at_minimum 是'应成功'边界bonus，与 at_maximum 一样不进分母。"""
        contract = {"request_fields": [{"name": "code", "type": "string", "required": True, "min_len": 6}]}
        self.assertNotIn("at_minimum:code", _keys(check_key.required_checks(contract)))

    def test_derive_key_maps_new_prefixes(self):
        self.assertEqual(check_key.derive_key({"id": "B-LEN-UNDER-code"}), "below_minimum:code")
        self.assertEqual(check_key.derive_key({"id": "B-LEN-MIN-code"}), "at_minimum:code")


if __name__ == "__main__":
    unittest.main()
