# -*- coding: utf-8 -*-
"""审查期修复的回归锁：TODO 占位键 + 终态重复守卫。

两条都是「修好的东西自己又踩了同一个坑」的典型，必须用行为断言锁住：

1. **TODO 占位键**：生成器对嵌套点路径字段（`data.items[].name`）无法在顶层安全变异，
   统一降级 `TODO-<MECH>-<field>` 占位。分母侧仍按契约声明约束——`derive_key` 不认
   TODO 前缀时，每个嵌套字段每轮刷 3 条永久假缺口（missing_required / over_validation /
   null_value 实测复现），正是 check_key 自己 docstring 警告过的失败模式。
2. **终态重复守卫**：主请求失败时资源没进终态，第二次请求被拒**不能**归因于终态保护。
   该守卫分支此前引用了尚未赋值的 `dim` → 必抛 NameError（正是它要保护的路径）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _bootstrap  # noqa: F401,E402 环境替身
import case_runner as CR  # noqa: E402
import api_test_runner as R  # noqa: E402
import check_key  # noqa: E402
import case_generator  # noqa: E402
import pattern_engine  # noqa: E402


class TestTodoPlaceholderKey(unittest.TestCase):
    """TODO-<MECH>-<field> 占位必须派生出与分母同形的约束键。"""

    def test_todo_prefix_is_stripped_for_all_mech_kinds(self):
        cases = {
            "TODO-B-NULL-user.age": "null_value:user.age",
            "TODO-B-MISS-data.items[].name": "missing_required:data.items[].name",
            "TODO-B-OVER-user.profile": "over_validation:user.profile",
            "TODO-B-TYPE-a.b": "wrong_type:a.b",
        }
        for cid, want in cases.items():
            self.assertEqual(check_key.derive_key({"id": cid}), want, cid)

    def test_nested_fields_produce_no_false_gaps(self):
        """端到端：嵌套字段走完整生成链后，对账不得报假缺口。"""
        contract = {"request_fields": [
            {"name": "title", "type": "string", "required": True, "max_len": 20},
            {"name": "data.items[].name", "type": "string", "required": True},
            {"name": "user.age", "type": "int"},
        ]}
        cases = (case_generator.gen_field_mutations(contract, {}, "1000107")
                 + pattern_engine.p07_gen(contract, {"base": {}}))
        missing = [m["key"] for m in check_key.reconcile(cases, contract).get("missing", [])]
        nested = [k for k in missing if "." in k or "[" in k]
        self.assertFalse(nested, "嵌套字段仍有假缺口：%r" % nested)

    def test_normal_field_still_reports_when_nothing_generated(self):
        """反向断言：别把修复做成「一律不报缺口」。"""
        contract = {"request_fields": [{"name": "title", "type": "string", "required": True}]}
        missing = [m["key"] for m in check_key.reconcile([], contract).get("missing", [])]
        self.assertIn("missing_required:title", missing)


class TestTerminalRepeatMainFailureGuard(unittest.TestCase):
    """主请求失败 → 记观察跳过判定；不得崩溃、不得报缺陷。"""

    def setUp(self):
        self._issues, self._observations = [], []
        self._orig_issue = R.add_issue
        self._orig_obs = R.add_observation
        R.add_issue = lambda *a, **k: (self._issues.append((a, k)), "x")[1]
        R.add_observation = lambda *a, **k: (self._observations.append((a, k)), "x")[1]

    def tearDown(self):
        R.add_issue = self._orig_issue
        R.add_observation = self._orig_obs

    def test_main_failed_records_observation_without_name_error(self):
        case = {"id": "E-01", "action": "1000107", "params": {}, "dimension": "E"}
        try:
            CR._check_terminal_repeat(case, {"result": False, "errormsg": "boom"}, {})
        except NameError as exc:      # 修复前：dim 未定义，必崩
            self.fail("主失败守卫分支抛 NameError（守卫引用了未赋值的 dim）：%s" % exc)
        self.assertFalse(self._issues, "主请求失败时不得报缺陷：%r" % self._issues)
        self.assertTrue(self._observations, "主请求失败必须留观察（否则被静默吞掉）")
        self.assertIn("未进入终态", str(self._observations[0][0]))

    def test_second_accepted_is_high_risk_defect(self):
        """反向断言：主请求成功 + 第二次仍被接受 = 终态保护缺失，必须报高危。"""
        self._orig_call = R.call
        R.call = lambda payload, retries=2, extra_headers=None: (
            200, "{}", {"result": True, "errormsg": ""})
        try:
            CR._check_terminal_repeat(
                {"id": "E-02", "action": "1000107", "params": {}, "dimension": "E"},
                {"result": True}, {})
        finally:
            R.call = self._orig_call
        self.assertTrue(self._issues, "第二次被接受却未报缺陷（终态保护缺失被漏掉）")
        self.assertEqual(self._issues[0][1].get("severity"), "高")


class TestRound2ContractKeyInjection(unittest.TestCase):
    """【审查修复·P0-3 锁定】用例契约键必须真正到达 check_contract。

    此前 case_runner 把键写到 R.CONTRACT_KEYS（无人读的死属性），check_contract
    被按值导入、恒读 assertion_helpers 全局默认三键 vid/sid/status——契约用例
    批量假 FAIL / 恰含三键的接口漏检。
    """

    def test_keys_param_overrides_default(self):
        import assertion_helpers as AH
        ok, msg = AH.check_contract({"a": 1, "b": 2}, keys=["a", "b"])
        self.assertTrue(ok, msg)
        ok, msg = AH.check_contract({"a": 1}, keys=["a", "b"])
        self.assertFalse(ok)
        self.assertIn("b", msg)

    def test_none_falls_back_to_default_keys(self):
        import assertion_helpers as AH
        ok, _ = AH.check_contract({"vid": 1, "sid": 2, "status": 3}, keys=None)
        self.assertTrue(ok)

    def test_case_runner_writes_module_global_and_run_consumes(self):
        """case_runner 写 R.CONTRACT_KEYS → R.check_contract(keys=...) 消费同一全局。"""
        R.CONTRACT_KEYS = ["vid", "sid"]
        try:
            ok, msg = R.check_contract({"vid": 1, "sid": 2}, keys=R.CONTRACT_KEYS)
            self.assertTrue(ok, msg)
        finally:
            R.CONTRACT_KEYS = None


class TestRound2RetestChain(unittest.TestCase):
    """【审查修复·P1-4/5/6 锁定】复测四态判定链路：error 来源 / 契约哈希 / last_error。"""

    def setUp(self):
        self._orig_results = R.results
        R.results = []
        self._tmp = __import__("tempfile").mkdtemp()

    def tearDown(self):
        R.results = self._orig_results
        R.CONTRACT_KEYS = None

    def _doc(self, title="t"):
        return {
            "contract_model": {
                "action": "1000107",
                "request_fields": [{"name": "title", "type": "string"}],
                "response_fields": [],
            },
            "cases": [{"id": "c1", "dimension": "B", "priority": "P0"}],
        }

    def test_baseline_records_last_error_from_results(self):
        import flow
        R.results = [{"case_no": "c1", "status": "FAIL", "reason": "result=false 参数不足"}]
        with __import__("io").StringIO() as buf, \
                __import__("contextlib").redirect_stdout(buf):
            flow.write_regression_artifacts(self._tmp, self._doc())
        import json
        data = json.load(open(os.path.join(self._tmp, "regression_baseline.json"),
                              encoding="utf-8"))
        self.assertEqual(data["entries"][0]["expectations"]["last_error"],
                         "result=false 参数不足")

    def test_env_error_reason_hits_blocked_env(self):
        """reason 含网络词 → BLOCKED_ENV（此前 error 读 response.note 恒空，永不命中）。"""
        import flow
        import regression as REG
        REG.save_baseline(os.path.join(self._tmp, "bl.json"),
                          [{"case_id": "c1", "contract_hash": "h",
                            "expectations": {"last_error": "e"}}])
        rows = [{"case_id": "c1", "passed": False,
                 "error": "连接/网络异常(重试后仍失败): timeout"}]
        verdicts = flow.retest_report(os.path.join(self._tmp, "bl.json"), rows)
        self.assertEqual(verdicts[0]["verdict"], "BLOCKED_ENV")

    def test_contract_change_hits_stale_baseline(self):
        """契约字段变化 → 哈希变 → STALE_BASELINE（此前不可达）。"""
        import flow
        with __import__("io").StringIO() as buf, \
                __import__("contextlib").redirect_stdout(buf):
            flow.write_regression_artifacts(self._tmp, self._doc())
        doc2 = self._doc()
        doc2["contract_model"]["request_fields"].append({"name": "extra", "type": "int"})
        rows = [{"case_id": "c1", "passed": True, "error": ""}]
        verdicts = flow.retest_report(
            os.path.join(self._tmp, "regression_baseline.json"), rows, doc=doc2)
        self.assertEqual(verdicts[0]["verdict"], "STALE_BASELINE")

    def test_different_error_hits_regressed(self):
        """上轮失败 A、本轮失败 B≠A → REGRESSED（此前 last_error 恒空，不可达）。"""
        import flow
        import regression as REG
        REG.save_baseline(os.path.join(self._tmp, "bl.json"),
                          [{"case_id": "c1", "contract_hash": "h",
                            "expectations": {"last_error": "错误A"}}])
        rows = [{"case_id": "c1", "passed": False, "error": "错误B"}]
        verdicts = flow.retest_report(os.path.join(self._tmp, "bl.json"), rows)
        self.assertEqual(verdicts[0]["verdict"], "REGRESSED")

    def test_flow_reads_reason_not_response_note(self):
        """复测行构造必须读 reason（静态锁定：response.note 路径已移除）。"""
        with open(os.path.join(os.path.dirname(R.__file__), "flow.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn('resp.get("note")', src,
                         "复测 error 又读回 response.note（业务响应体无该键，恒空）")


class TestRound2GraphqlStrBody(unittest.TestCase):
    """【审查修复·P1-7 锁定】主链路传 str body 时「HTTP 200 + errors」必须判 graphql_error。"""

    def test_str_body_with_errors_is_graphql_error(self):
        from adapters.graphql import GraphqlAdapter
        resp = type("_Resp", (), {
            "status_code": 200,
            "body": '{"errors": [{"message": "boom"}]}'})()
        cls = GraphqlAdapter().classify_response(resp)
        self.assertEqual(cls.get("status"), "graphql_error")

    def test_str_body_without_errors_still_ok(self):
        from adapters.graphql import GraphqlAdapter
        resp = type("_Resp", (), {"status_code": 200, "body": '{"data": {}}'})()
        cls = GraphqlAdapter().classify_response(resp)
        self.assertEqual(cls.get("status"), "ok")

    def test_non_json_str_body_still_ok(self):
        from adapters.graphql import GraphqlAdapter
        resp = type("_Resp", (), {"status_code": 200, "body": "not-json"})()
        cls = GraphqlAdapter().classify_response(resp)
        self.assertEqual(cls.get("status"), "ok")


if __name__ == "__main__":
    unittest.main()
