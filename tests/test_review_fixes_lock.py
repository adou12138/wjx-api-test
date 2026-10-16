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


if __name__ == "__main__":
    unittest.main()
