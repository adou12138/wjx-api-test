# -*- coding: utf-8 -*-
"""发现档（discovery_profile）与复合类型 wrong_type 的测试。

锁住四件事：
1. ``type_bad_for`` 支持复合类型，且选出的值是**对所有成员都非法**的——
   否则注入的值恰好命中某个联合分支被判合法，就会产出"期望拒绝却成功"的假缺陷；
2. 默认档（conservative）的限额与接入前**逐项一致**（增量接入，行为不变）；
3. 发现档整体放宽（超长上限 / 载荷条数 / 字段族配额 / AF 条数）；
4. 三处 ``big_cap`` 常量（discovery_profile.DEFAULTS / case_generator /
   check_key / pattern_engine）保持一致，避免"放宽了一处、对账另一处"。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import case_generator  # noqa: E402
import check_key  # noqa: E402
import discovery_profile  # noqa: E402
import pattern_engine  # noqa: E402
import business_scenarios  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "simple_contract.md")


class _ProfileReset(unittest.TestCase):
    """每个测试前后都回到默认档，避免档位全局状态串味。"""

    def setUp(self):
        discovery_profile.activate("conservative")

    def tearDown(self):
        discovery_profile.activate("conservative")


class TestTypeBadCompound(_ProfileReset):
    """复合类型的类型错误值：对每个成员都非法。"""

    def test_compound_returns_universally_invalid(self):
        for t in ("null / boolean", "integer / string(int32)", "array[object]", "null / integer"):
            bad = case_generator.type_bad_for({"name": "x"}, t)
            self.assertIsNotNone(bad, "复合类型 %s 应能构造非法值" % t)
            toks = [x for x in case_generator.type_tokens(t) if x in case_generator.TYPE_BAD]
            self.assertTrue(toks, t)
            for tok in toks:
                self.assertFalse(case_generator._PRIMITIVE_VALID[tok](bad),
                                 "%s 判为合法 → 会产生假缺陷：%r" % (t, bad))

    def test_primitive_valid_covers_type_bad(self):
        """_PRIMITIVE_VALID 必须覆盖 TYPE_BAD 的每个键。

        否则将来给 TYPE_BAD 新增一个类型时，复合分支会因"成员无法判定"静默返回 None
        （不生成用例），缺口被悄悄漏掉。用集合包含关系守住这条不变量。
        """
        missing = set(case_generator.TYPE_BAD) - set(case_generator._PRIMITIVE_VALID)
        self.assertEqual(missing, set(), "以下类型缺少合法性判定，复合分支会静默退化：%s" % sorted(missing))

    def test_single_type_value_unchanged(self):
        """单类型字段仍返回 TYPE_BAD 里定义的非法值——复合支持不得改变原行为。"""
        for t, bad in case_generator.TYPE_BAD.items():
            if t in case_generator._STRING_TOKENS:
                continue  # 字符串走轮换形态池，另有单测
            self.assertEqual(case_generator.type_bad_for({"name": "x"}, t), bad, t)

    def test_parser_noise_returns_none(self):
        """解析器噪音（中文/URL）不含类型 token → 不构造用例，不造假缺口。"""
        for t in ("问卷id", "www.wjx.cn", "showdoc 导出", "—", "", None):
            self.assertIsNone(case_generator.type_bad_for({}, t), repr(t))

    def test_pure_string_keeps_rotation(self):
        """单一 string 字段仍按序轮换形态（避免全打同一分支）。"""
        f = {"name": "title", "semantics": []}
        vals = {repr(case_generator.type_bad_for(f, "string", i)) for i in range(3)}
        self.assertGreaterEqual(len(vals), 2, vals)

    def test_numeric_sem_string_uses_number(self):
        f = {"name": "creater", "semantics": ["id"]}
        self.assertEqual(case_generator.type_bad_for(f, "string"), 12345)

    def test_compound_closes_wrong_type_gap(self):
        """复合类型字段有了 B-TYPE 用例后，wrong_type 不再是缺口。"""
        contract = {"request_fields": [{"name": "flag", "type": "null / boolean", "required": True}]}
        rec = check_key.reconcile([{"id": "B-TYPE-flag"}], contract)
        self.assertNotIn("wrong_type:flag", rec["missing_keys"])


class TestDefaultUnchanged(_ProfileReset):
    """默认档 = 接入前的历史默认值，逐项对齐。"""

    def test_default_limits_equal_history(self):
        lim = discovery_profile.limits()
        self.assertEqual(lim["big_cap"], 5000)
        self.assertEqual(lim["payload_per_field_per_category"], None)
        self.assertEqual(lim["null_state_fields"], 6)
        self.assertEqual(lim["url_fields"], 5)
        self.assertEqual(lim["ui_proxy_fields"], 2)
        self.assertEqual(lim["af_max_cases"], 8)
        self.assertFalse(lim["overlong_probe"])

    def test_big_cap_consistent_across_modules(self):
        """分散在各模块的超长上限必须同源，否则一处放宽、另一处仍按旧值对账。"""
        self.assertEqual(discovery_profile.DEFAULTS["big_cap"], case_generator.BIG_CAP)
        self.assertEqual(discovery_profile.DEFAULTS["big_cap"], check_key._MAX_LEN_CAP)
        self.assertEqual(discovery_profile.DEFAULTS["big_cap"], pattern_engine.BIG_VALUE_CAP)

    def test_unknown_profile_falls_back_to_conservative(self):
        discovery_profile.activate("不存在的档位")
        self.assertEqual(discovery_profile.current(), "conservative")


class TestDiscoveryWidens(_ProfileReset):
    """发现档：限额整体放宽。"""

    def test_activate_discovery_widens(self):
        discovery_profile.activate("discovery")
        lim = discovery_profile.limits()
        self.assertGreater(lim["big_cap"], 5000)
        self.assertEqual(lim["payload_per_field_per_category"], 4)
        self.assertGreater(lim["null_state_fields"], 6)
        self.assertGreater(lim["url_fields"], 5)
        self.assertGreater(lim["ui_proxy_fields"], 2)
        self.assertGreater(lim["af_max_cases"], 8)
        self.assertTrue(lim["overlong_probe"])

    def test_denominator_widens_with_profile(self):
        """max_len=15000 在默认档不进分母，在发现档进分母（与生成器承诺同步放宽）。"""
        contract = {"request_fields": [{"name": "big", "max_len": 15000}]}
        discovery_profile.activate("conservative")
        self.assertNotIn("above_maximum:big",
                         {r["key"] for r in check_key.required_checks(contract)})
        discovery_profile.activate("discovery")
        self.assertIn("above_maximum:big",
                      {r["key"] for r in check_key.required_checks(contract)})

    def test_af_expected_count_widens(self):
        contract = {"request_fields": [{"name": "f%d" % i} for i in range(20)]}
        discovery_profile.activate("conservative")
        base = business_scenarios.expected_count(contract)
        discovery_profile.activate("discovery")
        self.assertGreater(business_scenarios.expected_count(contract), base)


class TestBigContentProbeTier(_ProfileReset):
    """B-BIG-*（发现档大内容健壮性探针）必须归 probe 层。

    它是机械生成（source=generator:big_content）、与 B-LEN-* 同类；若不注册进
    TIER_PROBE_PREFIXES，_tier_of 会把它算进 business 覆盖——这正是分层要防的
    "机械展开虚高业务覆盖"失败模式。注意它**不**该进 check_key._MECH_PREFIXES：
    它预期成功（健壮性），不是"应被拒绝"的约束，故不进对账分母。
    """

    def test_big_prefix_registered_as_probe(self):
        self.assertIn("B-BIG-", case_generator.TIER_PROBE_PREFIXES)
        self.assertEqual(case_generator._tier_of({"id": "B-BIG-html"}), "probe")
        self.assertEqual(case_generator._tier_of({"id": "B-LEN-OVER-html"}), "probe")

    def test_big_not_a_gate_constraint(self):
        """B-BIG 不该进对账分母（预期成功，非 must-reject），否则制造假缺口。"""
        mech = {p for p, _ in check_key._MECH_PREFIXES}
        self.assertNotIn("B-BIG-", mech)

    def test_overlong_probe_generates_probe_case(self):
        """发现档下，max_len 超上限的字段生成 B-BIG 探针，且 tier=probe。"""
        discovery_profile.activate("discovery")
        contract = {"request_fields": [
            {"name": "html", "type": "string", "required": True, "max_len": 200000}]}
        cases = case_generator.gen_field_mutations(contract, {}, "act")
        big = [c for c in cases if str(c["id"]).startswith("B-BIG-")]
        self.assertTrue(big, "发现档应为 max_len 超上限字段生成 B-BIG 大内容探针")
        self.assertEqual(case_generator._tier_of(big[0]), "probe")
        self.assertEqual(big[0]["expect"], {"success": True})


class TestGenerateIntegration(_ProfileReset):
    """端到端：发现档生成的用例数不少于默认档（增量且更宽）。"""

    def test_discovery_generates_at_least_as_many(self):
        discovery_profile.activate("conservative")
        base = case_generator.generate(FIXTURE)
        discovery_profile.activate("discovery")
        wide = case_generator.generate(FIXTURE)
        self.assertGreaterEqual(len(wide["cases"]), len(base["cases"]))
        # 默认档行为应与未激活时完全一致（增量）
        discovery_profile.activate("conservative")
        again = case_generator.generate(FIXTURE)
        self.assertEqual(len(again["cases"]), len(base["cases"]))


if __name__ == "__main__":
    unittest.main()
