# -*- coding: utf-8 -*-
"""批次1 P0 修复的行为回归测试（方案 doc/superpowers/plans 中 W1/W2/W9/W11/W13/W14/W18-W22/W32/W34）。

每条对应方案里的一个编号，修复前必红、修复后绿。行为反转的旧断言
（如 test_length_bounds 的 at_minimum 分母）已在原文件同步改写，
本文件只补原文件没有锁定的缺口。
"""
import io
import json
import os
import sys
import unittest
import contextlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import _bootstrap  # noqa: F401,E402 环境替身

import api_test_runner as R      # noqa: E402
import assertion_engine as AE    # noqa: E402
import case_generator as CG      # noqa: E402
import pattern_engine as PE      # noqa: E402
import contract_parser as CP     # noqa: E402
import check_key as CK           # noqa: E402


def _fake_caller(msg, result=False, code=200):
    """构造固定响应的 call 替身。注意：run() 读的是 errormsg 键（非 errmsg）。"""
    def fake(payload, retries=2, extra_headers=None):
        body = {"result": result, "errormsg": msg}
        return code, json.dumps(body, ensure_ascii=False), body
    return fake


class TestW1NoErrExactNotPass(unittest.TestCase):
    """W1：异常用例无 err_exact 时不得判 PASS（任何拒绝都可能是登录/网关拒绝）。"""

    def setUp(self):
        self._orig = R.call
        R.results.clear()
        R.findings.clear()

    def tearDown(self):
        R.call = self._orig

    def test_run_reject_without_err_exact_is_pending(self):
        R.call = _fake_caller("请先登录后重试")
        R.run("T1", "异常", "1000107", False, "业务-异常场景", creater="x", dimension="B")
        self.assertEqual(R.results[-1]["status"], "待确认")
        self.assertTrue(any(f["kind"] == "待确认" and f["severity"] == "高"
                            for f in R.findings), "必须留高危待确认痕迹供人工翻案")

    def test_assert_validation_reject_without_err_exact_is_pending(self):
        R.call = _fake_caller("请先登录后重试")
        R.assert_validation("T2", "校验", "1000107", creater="x", dimension="B")
        self.assertEqual(R.results[-1]["status"], "待确认")

    def test_exact_hit_still_pass(self):
        """回归保护：命中 err_exact 的仍 PASS。"""
        R.call = _fake_caller("标题不能为空")
        R.run("T3", "异常", "1000107", False, "业务-异常场景", creater="x",
              err_exact=["标题不能为空"], dimension="B")
        self.assertEqual(R.results[-1]["status"], "PASS")


class TestW2AssertValidationMismatch(unittest.TestCase):
    """W2：assert_validation 配了 err_exact 未命中 → FAIL（与 run() 口径统一）。"""

    def setUp(self):
        self._orig = R.call
        R.results.clear()
        R.findings.clear()

    def tearDown(self):
        R.call = self._orig

    def test_mismatch_is_fail_not_pass(self):
        R.call = _fake_caller("系统内部错误")
        R.assert_validation("T4", "校验", "1000107", creater="x",
                            err_exact=["标题不能为空"], dimension="B")
        self.assertEqual(R.results[-1]["status"], "FAIL")
        self.assertTrue(any(f["kind"] == "缺陷" for f in R.findings))


class TestW9GateDenominator(unittest.TestCase):
    """W9：coverage_gate 分母——缺配置观察不补计维度，真缺陷补计。

    【审查修复】旧版在测试里逐行复刻门禁收集逻辑（本地副本）——门禁实现被改坏
    时本测试仍绿。改为驱动**真 coverage_gate()**：声明 COVERAGE_PLAN 后跑门禁，
    断言缺口清单里的维度构成。"""

    def setUp(self):
        self._orig_plan = R.COVERAGE_PLAN
        self._orig_blocked = R.coverage_blocked
        R.results.clear()
        R.findings.clear()

    def tearDown(self):
        R.COVERAGE_PLAN = self._orig_plan
        R.coverage_blocked = self._orig_blocked
        R.results.clear()
        R.findings.clear()

    def _gate_missing(self):
        """跑真门禁，返回缺口（COV- 台账）中的维度集合。"""
        R.coverage_gate()
        return {str(f.get("dimension") or "").strip()
                for f in R.findings if R._is_coverage_gap(f)}

    def test_need_config_observation_not_executed(self):
        """只有「缺配置未执行」观察的维度必须出现在缺口清单——不许被观察冒充已执行。"""
        R.COVERAGE_PLAN = ["X"]
        R.add_observation("[x] check=pagination 未执行：缺少配置 read_action",
                          {}, fid="x", severity="低", dimension="X")
        self.assertIn("X", self._gate_missing())

    def test_real_defect_counts(self):
        """有真缺陷（add_issue，真实执行产物）的维度不算缺口。"""
        R.COVERAGE_PLAN = ["B"]
        R.add_issue("[b] 响应缺字段", {}, fid="b", dimension="B")
        self.assertNotIn("B", self._gate_missing())


class TestW11EdgeDenominator(unittest.TestCase):
    """W11：at_maximum/at_minimum 进对账分母（边界 off-by-one 不再脱离门禁）。"""

    def test_at_maximum_declared_with_max_len(self):
        ks = {k["key"] for k in CK.required_checks(
            {"request_fields": [{"name": "t", "type": "string", "max_len": 10}]})}
        self.assertIn("at_maximum:t", ks)

    def test_at_minimum_declared_min_len_ge2_no_enum(self):
        ks = {k["key"] for k in CK.required_checks(
            {"request_fields": [{"name": "t", "type": "string", "min_len": 6}]})}
        self.assertIn("at_minimum:t", ks)

    def test_at_minimum_skipped_for_enum_fields(self):
        ks = {k["key"] for k in CK.required_checks(
            {"request_fields": [{"name": "t", "type": "string", "min_len": 6,
                                 "enum": ["a", "b"]}]})}
        self.assertNotIn("at_minimum:t", ks)


class TestW13W14PatternEngine(unittest.TestCase):
    """W13：模式异常不静默消失（TODO 留痕）；W14：字符串枚举越界可生成。"""

    def test_string_enum_bad_value(self):
        cases = PE.p06_gen({"request_fields": [
            {"name": "level", "type": "string", "enum": [("A", "级A"), ("B", "级B")]}]},
            {"base": {}})
        bad = [c for c in cases if c["id"].startswith("B-ENUM-")]
        self.assertTrue(bad and bad[0]["params"]["level"] == "__invalid_enum__")

    def test_match_exception_leaves_todo(self):
        def boom(c, ctx):
            raise RuntimeError("x")
        saved = list(PE.PATTERNS)
        PE.PATTERNS.append(("ZZ", "z", "Z9", boom, lambda c, ctx: []))
        try:
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                cases, matched = PE.run({"request_fields": []}, {"base": {}})
            self.assertTrue(any(m["id"] == "ZZ" and m.get("error") for m in matched))
            self.assertTrue(any(c["id"] == "TODO-ZZ" and c.get("needs") == "manual"
                                for c in cases))
            self.assertIn("异常", buf.getvalue())
        finally:
            PE.PATTERNS[:] = saved


class TestW18W21Parser(unittest.TestCase):
    """W18-W21：$ref 兄弟键 / allOf / 数组 items required / nullable。"""

    def _openapi(self, schema):
        return {"openapi": "3.0.0", "servers": [{"url": "https://x"}],
                "paths": {"/t": {"post": {"requestBody": {
                    "content": {"application/json": {"schema": schema}}}}}}}

    def test_ref_sibling_keys_merged(self):
        spec = {"swagger": "2.0", "host": "x", "basePath": "/",
                "paths": {"/t": {"post": {"parameters": [
                    {"in": "body", "name": "title",
                     "schema": {"$ref": "#/definitions/I", "maxLength": 12}}]}}},
                "definitions": {"I": {"type": "string"}}}
        f = CP.parse_openapi(spec, "t")["request_fields"][0]
        self.assertEqual(f["max_len"], 12)

    def test_alloff_expanded(self):
        c = CP.parse_openapi(self._openapi({"allOf": [
            {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
            {"type": "object", "properties": {"b": {"type": "integer"}}}]}), "t")
        names = {f["name"] for f in c["request_fields"]}
        self.assertSetEqual({"a", "b"}, names)
        self.assertTrue(next(f for f in c["request_fields"] if f["name"] == "a")["required"])

    def test_oneof_leaves_marker(self):
        c = CP.parse_openapi(self._openapi({"oneOf": [
            {"type": "object", "properties": {"x": {"type": "string"}}}]}), "t")
        self.assertTrue(any("<oneOf_combo>" in f["name"] for f in c["request_fields"]))

    def test_array_items_required_propagated(self):
        c = CP.parse_openapi(self._openapi({
            "type": "object", "properties": {"items": {"type": "array", "items": {
                "type": "object", "properties": {"nm": {"type": "string"}},
                "required": ["nm"]}}}}), "t")
        nm = next(f for f in c["request_fields"] if f["name"] == "items[].nm")
        self.assertTrue(nm["required"])

    def test_nullable_parsed(self):
        c = CP.parse_openapi(self._openapi({
            "type": "object", "required": ["note"],
            "properties": {"note": {"type": "string", "nullable": True}}}), "t")
        self.assertTrue(next(f for f in c["request_fields"] if f["name"] == "note")["nullable"])


class TestW22NestedPath(unittest.TestCase):
    """W22：嵌套点路径字段降级 TODO，不产无效变异用例。"""

    def test_nested_missing_downgrades(self):
        cases = CG.gen_field_mutations(
            {"request_fields": [{"name": "a.b", "type": "string", "required": True}]},
            {}, "act")
        self.assertTrue(any(c["id"].startswith("TODO-B-MISS") for c in cases))
        self.assertFalse(any(c["id"].startswith("B-MISS-a.b") for c in cases))

    def test_flat_missing_unaffected(self):
        cases = CG.gen_field_mutations(
            {"request_fields": [{"name": "t", "type": "string", "required": True}]},
            {"t": "x"}, "act")
        self.assertTrue(any(c["id"] == "B-MISS-t" for c in cases))


class TestW32Ui1MaxLen(unittest.TestCase):
    """W32：UI1 按 max_len（解析器统一键名）截断长文本。"""

    def test_max_len_key_read(self):
        cases = PE.ui1_gen({"request_fields": [
            {"name": "content", "type": "string", "max_len": 50}],
            "response_fields": []}, {"base": {}})
        long_case = next(c for c in cases if c["id"].startswith("UI1-long-content"))
        self.assertLessEqual(len(long_case["params"]["content"]), 50)


class TestW34ConfigGate(unittest.TestCase):
    """W34：空配置时真实执行被拦（exit 2），产出空报告的路径被封死。"""
    # 行为验证在脚本里以子进程完成（见工作日志）；此处锁 ensure_runnable_config
    # 的幂等性与 dry-run 不拦截。

    def test_dry_run_not_blocked_by_config(self):
        import case_runner as CR
        # dry=True 直接 return，不触发校验——配置为空也不影响执行计划展示
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            CR.exec_case({"id": "d1", "name": "plan", "dimension": "B",
                          "expect": {"success": True}}, dry=True)
        finally:
            sys.stdout = old
        self.assertIn("plan", buf.getvalue())

    def test_ensure_config_idempotent_when_configured(self):
        import case_runner as CR
        # 【审查修复】_CONFIG_VERIFIED 是模块级全局，同进程更早的 exec_case 测试
        # 会把它置 True → 两次调用全是 no-op，「幂等」空转假通过。重置后再验证。
        original = CR._CONFIG_VERIFIED
        CR._CONFIG_VERIFIED = False
        try:
            CR.ensure_runnable_config()
            self.assertTrue(CR._CONFIG_VERIFIED, "首次调用应置位")
            CR.ensure_runnable_config()  # 第二次：已置位短路，不重复校验也不报错
            self.assertTrue(CR._CONFIG_VERIFIED)
        finally:
            CR._CONFIG_VERIFIED = original


if __name__ == "__main__":
    unittest.main()
