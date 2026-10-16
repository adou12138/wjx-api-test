# -*- coding: utf-8 -*-
"""check_key：用例身份证与约束对账的测试。

锁住四件事：
1. 约束全集只由契约白纸黑字的声明推导，猜出来的约束会变成永不兑现的假缺口；
2. 类型判定认得复合类型（"null / boolean"）但拒绝解析器噪音（"问卷id"）；
3. 派生覆盖全量用例——机械→约束型，语义/人工→来源型；
4. **来源型 key 不进对账分母**，否则语义用例一多，门禁就永远不可能通过。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import check_key  # noqa: E402
import case_generator  # noqa: E402


def _contract(fields):
    return {"request_fields": fields}


class TestRequiredChecks(unittest.TestCase):
    """约束全集 = 契约声明的集合，不多不少。"""

    def test_required_field_yields_missing_required(self):
        rec = check_key.required_checks(_contract([{"name": "title", "required": True}]))
        self.assertIn("missing_required:title", [r["key"] for r in rec])

    def test_optional_field_yields_over_validation_once(self):
        """可选且可空只产一条 over_validation——两条会把缺口数虚高一倍。"""
        rec = check_key.required_checks(
            _contract([{"name": "remark", "required": False, "nullable": True}]))
        keys = [r["key"] for r in rec]
        self.assertEqual(keys.count("over_validation:remark"), 1, keys)

    def test_unknown_constraints_not_invented(self):
        """契约只写了字段名的，产出可选字段的两类已实现约束（over_validation +
        【W12 反转】null_value——旧版 null_value 是死约束类型，B-NULL 派生成它、
        分母从不声明，「显式 null 处理」漏测永远不出声；现与生成器对齐进分母）。

        仍不产出任何「契约没声明」的约束（猜出来的约束永远无法兑现）。"""
        rec = check_key.required_checks(_contract([{"name": "x"}]))
        self.assertEqual(sorted(r["key"] for r in rec),
                         ["null_value:x", "over_validation:x"])
        # required 字段不声明 null_value（其 null 语义由 missing_required 覆盖）
        rec2 = check_key.required_checks(_contract([{"name": "y", "required": True}]))
        self.assertNotIn("null_value:y", [r["key"] for r in rec2])

    def test_null_value_denominator_matches_p07_quota(self):
        """【审查修复】null_value 分母必须与 p07_gen 的族配额选集严格一致。

        可选字段超过 null_state_fields 配额（默认 6）时，配额外字段的 B-NULL
        被有意截断——分母若仍全量声明，被砍字段每轮刷永久假缺口（实测 8 字段
        契约每轮 2 条）。端到端断言：required_checks 的 null_value 集合 ==
        p07_gen 交付派生集合，超出配额一倍也不许漂移。"""
        import pattern_engine
        contract = {"request_fields": [
            {"name": "opt%d" % i, "type": "string"} for i in range(8)]}
        denominator = {r["key"] for r in check_key.required_checks(contract)
                       if r["key"].startswith("null_value")}
        delivered = {check_key.derive_key(c) for c in
                     pattern_engine.p07_gen(contract, {"base": {}})}
        delivered = {k for k in delivered if "null_value" in k}
        self.assertEqual(denominator, delivered,
                         "null_value 分母与 P07 交付不对齐：分母独有 %s / 交付独有 %s"
                         % (sorted(denominator - delivered), sorted(delivered - denominator)))

    def test_max_len_and_enum(self):
        rec = check_key.required_checks(_contract([
            {"name": "a", "max_len": 10},
            {"name": "b", "enum": [(0, "否"), (1, "是")]},
        ]))
        keys = {r["key"] for r in rec}
        self.assertIn("above_maximum:a", keys)
        self.assertIn("invalid_enum:b", keys)

    def test_overlong_max_len_excluded(self):
        """max_len 超过生成器上限（BIG_CAP）时不进分母。

        生成器对超长 payload 有意跳过（构造 20 万字符会拖慢请求、被网关拒收），
        实测 1000107/1000108 的 html_content max_len=200000——若进分母，这两个
        高频接口每轮都刷永远无法兑现的假缺口。
        """
        rec = check_key.required_checks(
            _contract([{"name": "html_content", "max_len": 200000}]))
        self.assertNotIn("above_maximum:html_content", {r["key"] for r in rec})

    def test_min_len_ge2_below_minimum_in_denominator(self):
        """min_len >= 2：生成器承诺 B-LEN-UNDER，below_minimum 进分母（v4.24.0 起）。

        此前生成器不产出 min_len 用例、below_minimum 不进分母；v4.24.0 补上
        "低于下限"用例后，按"分母 = 生成器承诺范围"把 below_minimum 加回分母。
        """
        rec = check_key.required_checks(_contract([{"name": "a", "min_len": 5}]))
        self.assertIn("below_minimum:a", {r["key"] for r in rec})

    def test_min_len_eq1_no_denominator(self):
        """min_len == 1：低于值即空串，与可选空值/必填缺失语义重叠，生成器不构造
        B-LEN-UNDER，分母也不声明 below_minimum——无承诺就不对账，避免假缺口。"""
        rec = check_key.required_checks(_contract([{"name": "a", "min_len": 1}]))
        self.assertNotIn("below_minimum:a", {r["key"] for r in rec})

    def test_reason_carries_contract_value(self):
        """缺口的 reason 要带上契约原值，否则排查时还要回头翻契约。"""
        rec = check_key.required_checks(_contract([{"name": "a", "max_len": 120}]))
        r = [x for x in rec if x["key"] == "above_maximum:a"][0]
        self.assertIn("120", r["reason"])


class TestTypeTokenJudgement(unittest.TestCase):
    """类型判定：认复合类型，拒解析噪音。"""

    def test_compound_type_recognized(self):
        for t in ("null / boolean", "null / integer / string(int32)", "array[object]"):
            self.assertTrue(check_key._declares_typed(t), t)

    def test_parser_noise_rejected(self):
        """中文/URL 是解析器把说明列当成类型列，不是契约声明的类型。

        放进分母会让门禁被噪音淹没（实测 25 篇真实契约里约 11 条属此类）。
        """
        for t in ("问卷id", "www.wjx.cn", "showdoc 导出", "—", "", None):
            self.assertFalse(check_key._declares_typed(t), repr(t))

    def test_plain_type_recognized(self):
        for t in ("string", "int", "object"):
            self.assertTrue(check_key._declares_typed(t), t)


class TestDeriveKey(unittest.TestCase):
    """派生：机械用例→约束型，其余→来源型。"""

    def test_mech_prefixes(self):
        cases = [
            ("B-MISS-title", "missing_required:title"),
            ("B-TYPE-age", "wrong_type:age"),
            ("B-LEN-OVER-name", "above_maximum:name"),
            ("B-LEN-EDGE-name", "at_maximum:name"),
            ("B-NULL-x", "null_value:x"),
            ("B-ENUM-page_type", "invalid_enum:page_type"),
        ]
        for cid, want in cases:
            self.assertEqual(check_key.derive_key({"id": cid}), want, cid)

    def test_over_states_stripped(self):
        """B-OVER-{field}-{state}：状态后缀要剥掉，否则同一字段变三个假字段。"""
        for st in ("missing", "null", "empty_string"):
            self.assertEqual(check_key.derive_key({"id": "B-OVER-remark-%s" % st}),
                             "over_validation:remark")

    def test_dup_suffix_stripped(self):
        """id 撞号被改写成 #N，不剥离会造出永不兑现的假缺口。"""
        self.assertEqual(check_key.derive_key({"id": "B-MISS-title#2"}),
                         "missing_required:title")

    def test_semantic_and_manual_get_source_key(self):
        """语义/人工用例也要有身份证（纳入编号体系），但不是约束型。"""
        k = check_key.derive_key({"id": "AF-01", "source": "llm:fact"})
        self.assertTrue(k.startswith("semantic:"), k)
        self.assertFalse(check_key.is_constraint_key(k))
        k2 = check_key.derive_key({"id": "TODO-E", "needs": "manual"})
        self.assertTrue(k2.startswith("manual:"), k2)

    def test_unnamed_case_still_gets_key(self):
        """无 id 用例也要有 key——否则编号体系会漏掉一部分用例。"""
        self.assertTrue(check_key.derive_key({}).endswith(":UNNAMED"))


class TestReconcile(unittest.TestCase):
    """对账：缺口判定与分级。"""

    def test_missing_constraint_reported(self):
        contract = _contract([{"name": "age", "type": "int", "required": True}])
        rec = check_key.reconcile([{"id": "X", "source": "llm:fact"}], contract)
        self.assertIn("wrong_type:age", rec["missing_keys"])

    def test_delivered_constraint_not_reported(self):
        contract = _contract([{"name": "age", "type": "int", "required": True}])
        rec = check_key.reconcile([{"id": "B-TYPE-age"}, {"id": "B-MISS-age"}], contract)
        self.assertEqual(rec["missing_keys"], [])

    def test_source_keys_never_pollute_denominator(self):
        """来源型 key 不进分母：语义/人工用例再多，也不会让门禁永不通过。

        这是本模块最容易做错的一处——若把语义用例也算进 delivered，
        「契约约束 vs 语义用例」对不上，门禁会永久报缺口，进而被关掉。
        """
        contract = _contract([{"name": "a", "type": "string", "required": True}])
        many_semantic = [{"id": "SEM-%d" % i, "source": "llm:fact"} for i in range(30)]
        rec = check_key.reconcile(many_semantic, contract)
        self.assertEqual(sorted(rec["missing_keys"]),
                         ["missing_required:a", "wrong_type:a"])
        # 语义用例不计入已兑现
        self.assertEqual(rec["delivered"], [])

    def test_empty_contract_no_false_alarm(self):
        self.assertEqual(check_key.reconcile([], _contract([]))["missing"], [])
        self.assertEqual(check_key.reconcile([], {})["missing"], [])

    def test_assign_keys_covers_all_cases(self):
        cases = [{"id": "B-MISS-a"}, {"id": "AF-1", "source": "llm:fact"}, {"id": "TODO-G", "needs": "manual"}]
        check_key.assign_keys(cases)
        self.assertTrue(all(c.get("check_key") for c in cases))
        self.assertEqual(len({c["check_key"] for c in cases}), 3)


class TestConsistency(unittest.TestCase):
    """防漂移：本模块的类型 token 必须与生成器的 TYPE_BAD 对齐。"""

    def test_bad_type_tokens_match_generator(self):
        self.assertEqual(set(check_key._BAD_TYPE_TOKENS), set(case_generator.TYPE_BAD.keys()))

    def test_max_len_cap_matches_generator(self):
        """长度上限必须与生成器的 BIG_CAP 一致：分母 = 生成器承诺的范围。"""
        self.assertEqual(check_key._MAX_LEN_CAP, case_generator.BIG_CAP)


if __name__ == "__main__":
    unittest.main()
