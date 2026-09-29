# -*- coding: utf-8 -*-
"""第三轮审查（2026-09-23，docs/reviews/2026-09-23-wjx-api-test-v4.2.2-issues-review.md）
12 项缺陷的修复锁定测试。编号与报告一致（A1~A9 / B1~B3 / C1~C2 / D2~D3）。"""
import io
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import api_test_runner as R   # noqa: E402
import case_runner as CR      # noqa: E402
import ui_baseline as UB      # noqa: E402
import pattern_engine as PE   # noqa: E402
import flow                   # noqa: E402
import smoke_runner as SM     # noqa: E402


def _reset():
    R.results.clear()
    R.findings.clear()
    R.LAST_RESPONSE_HEADERS = {}
    if hasattr(R, "CREATED_RESOURCES"):
        R.CREATED_RESOURCES.clear()
    return R


class _CallRestore(object):
    def setUp(self):
        self._orig_call = R.call

    def tearDown(self):
        R.call = self._orig_call


def _bl(field, pv, tz=0, dim="AB"):
    return [UB.Baseline(field=field, page_value=pv, action="1000107",
                        dimension=dim, tz_offset=tz)]


# ---------------------------------------------------------------- 第一批
class TestBaselineNormalization(unittest.TestCase):
    """A1/A2/A3/A4/A5：check_parity 的归一层与双向检查。"""

    def test_int_vs_str_no_false_positive(self):
        c, g, f = UB.check_parity(_bl("status", "1"), "1000107", {"data": {"status": 1}})
        self.assertEqual((c, f), (1, []), "1 与 \"1\" 是同一个口径值，不得误报")

    def test_time_with_tz_offset_no_false_positive(self):
        c, g, f = UB.check_parity(_bl("created_at", "2026-09-22 16:30", tz=480),
                                  "1000107", {"data": {"created_at": "2026-09-22T08:30:00Z"}})
        self.assertEqual(f, [], "已换算的时区差（接口 UTC、页面 +8）不得误报")

    def test_unconverted_time_is_caught(self):
        """历史缺陷模式：页面没有做时区转换（差 8 小时）必须被抓到。"""
        c, g, f = UB.check_parity(_bl("created_at", "2026-09-22 08:30", tz=480),
                                  "1000107", {"data": {"created_at": "2026-09-22T08:30:00Z"}})
        self.assertEqual(len(f), 1, "差 8 小时正是 AB3 高发缺陷，必须报")

    def test_nested_path_supported(self):
        c, g, f = UB.check_parity(_bl("data.created_at", "2026-09-22 16:30", tz=480),
                                  "1000107", {"data": {"created_at": "2026-09-22T08:30:00Z"}})
        self.assertEqual(c, 1, "点路径基线应能取到嵌套值参与比对")

    def test_reverse_check_interface_missing_field(self):
        c, g, f = UB.check_parity(_bl("created_at", "2026-09-22 16:30"),
                                  "1000107", {"data": {"title": "T"}})
        self.assertEqual(len(f), 1,
                         "基线有、接口没返回必须报——前端拿 undefined 渲染空白是重灾区")

    def test_dimension_prefix_match(self):
        c, g, f = UB.check_parity(_bl("title", "T", dim="AB3-01"),
                                  "1000107", {"data": {"title": "X"}})
        self.assertEqual(c, 1, "登记表式维度编号（AB3-01）应能匹配到维度 AB")

    def test_gaps_semantics_preserved(self):
        """兼容锁定：响应有、基线无 → 仍记 gaps，不得凭空编预期。"""
        c, g, f = UB.check_parity(_bl("title", "T"), "1000107",
                                  {"data": {"title": "T", "extra": 1}})
        self.assertEqual(g, ["extra"])


class TestRoundtripNoEcho(unittest.TestCase):
    """A7：创建接口不回显字段时，roundtrip 不得静默空转。"""

    def test_no_echoed_field_records_gap(self):
        _reset()
        CR._check_roundtrip({"id": "UI1-html", "action": "1000107", "dimension": "C",
                             "params": {"title": "<b>x</b>"}},
                            {"result": True, "data": {"id": 9}},
                            {"fields": ["title"]})
        self.assertTrue(R.findings, "响应不回显目标字段必须记缺口，不得静默 PASS")
        self.assertTrue(all(str(x.get("id", "")).startswith("COV-") for x in R.findings),
                        "空转缺口应以 COV- 编号进台账")

    def test_echoed_matching_field_still_passes(self):
        _reset()
        CR._check_roundtrip({"id": "B-RT", "action": "1000107", "dimension": "C",
                             "params": {"title": "x"}},
                            {"result": True, "data": {"title": "x"}}, {})
        self.assertEqual(R.findings, [], "回显一致时不得误报")


class TestUi1Generation(unittest.TestCase):
    """A6/A9：富文本字段优先、多字段覆盖、长度尊重契约。"""

    def _contract(self, **extra_title):
        rf = [{"name": "title", "type": "string"}, {"name": "html_content", "type": "string"},
              {"name": "num", "type": "int"}]
        rf[0].update(extra_title)
        return {"request_fields": rf,
                "response_fields": [{"name": "cover_url"}, {"name": "status"}]}

    def test_rich_text_field_is_injected(self):
        cases = PE.ui1_gen(self._contract(), {"base": {}})
        self.assertTrue(any(c["params"].get("html_content") for c in cases
                            if c.get("check") == "roundtrip"),
                        "富文本字段必须被注入代理参数，不能只测第一个字段")

    def test_long_value_respects_declared_max(self):
        cases = PE.ui1_gen(self._contract(max_length=10), {"base": {}})
        longs = [c for c in cases if "long" in c["id"]]
        self.assertTrue(longs, "long 用例仍须生成")
        injected = [c for c in longs if c["params"].get("title")]
        self.assertTrue(all(len(c["params"]["title"]) <= 10 for c in injected),
                        "契约声明 max_length 时注入值不得超限（超限被拒是正确行为，不应制造误报）")

    def test_tolerate_reject_covers_length_and_char_filters(self):
        for w in ("长度", "超长", "不能超过", "过长", "字符"):
            self.assertIn(w, PE.UI_TOLERATE_REJECT,
                          "被合理过滤/限长拒绝时不得记 FAIL（词表缺 %r）" % w)


# ---------------------------------------------------------------- 第二批
class TestEnumDangerGuard(_CallRestore, unittest.TestCase):
    """B1：危险终态枚举值不得被自动提交。"""

    def test_dangerous_enum_values_are_not_submitted(self):
        _reset()
        submitted = []

        def fake_call(payload, retries=2):
            submitted.append(payload.get("status"))
            return 200, '{"result":true}', {"result": True, "data": {}}

        R.call = fake_call
        CR._check_enum_exhaustive(
            {"id": "K-E", "action": "1000107", "dimension": "K", "params": {}},
            {"result": True},
            {"field": "status", "documented": [0, 1, 9],
             "enum_map": {"0": "草稿", "1": "发布", "9": "删除"}})
        self.assertNotIn(9, submitted, "含「删除」等终态语义的枚举值不得自动提交")
        self.assertTrue(R.findings, "跳过的危险值必须留痕（否则又是静默漏测）")

    def test_safe_values_still_all_submitted(self):
        _reset()
        submitted = []

        def fake_call(payload, retries=2):
            submitted.append(payload.get("status"))
            return 200, '{"result":true}', {"result": True, "data": {}}

        R.call = fake_call
        CR._check_enum_exhaustive(
            {"id": "K-E", "action": "1000107", "dimension": "K", "params": {}},
            {"result": True},
            {"field": "status", "documented": [0, 1],
             "enum_map": {"0": "草稿", "1": "发布"}})
        self.assertEqual(sorted(submitted), [0, 1], "安全枚举值应全部遍历")


class TestRateLimitOrdering(_CallRestore, unittest.TestCase):
    """B3：限流观测必须排到执行序列最后，防止污染后续用例。"""

    def test_rate_limit_cases_moved_to_tail(self):
        cases = [{"id": "a", "check": ""}, {"id": "j1", "check": "rate_limit"},
                 {"id": "b", "check": ""}]
        ordered = flow.order_cases_for_exec(cases)
        self.assertEqual(ordered[-1]["id"], "j1")
        self.assertEqual([c["id"] for c in ordered][:2], ["a", "b"], "其余用例保持原相对顺序")

    def test_rate_limit_cools_down_after_limiting(self):
        _reset()
        slept = []

        def fake_call(payload, retries=2):
            return 429, '{"result":false}', {"result": False, "errormsg": "too many"}

        R.call = fake_call
        orig_sleep = CR.time.sleep
        CR.time.sleep = lambda s: slept.append(s)
        try:
            CR._check_rate_limit({"id": "J01", "action": "1000107", "dimension": "J", "params": {}},
                                 {"result": True}, {"times": 3})
        finally:
            CR.time.sleep = orig_sleep
        self.assertTrue(slept and slept[0] > 0, "触发限流后必须冷却，避免污染后续用例")


class TestCleanupWiring(_CallRestore, unittest.TestCase):
    """B2：cleanup_enabled 假开关转真接线；残留资源必须可见。"""

    def test_cleanup_enabled_is_consumed(self):
        policy = flow.apply_exec_policy({"execution": {"cleanup_enabled": True}})
        self.assertIs(policy.get("cleanup_enabled"), True, "cleanup_enabled 不得再是假开关")

    def test_created_resource_extracted_from_response(self):
        self.assertEqual(CR.extract_resource({"result": True, "data": {"id": 7}}, "1000107"),
                         {"action": "1000107", "id": 7})
        self.assertIsNone(CR.extract_resource({"result": True, "data": {}}, "1000107"))

    def test_cleanup_step_writes_manifest(self):
        _reset()
        R.CREATED_RESOURCES = [{"action": "1000107", "id": 7, "params": {"title": "t"}}]
        out = tempfile.mkdtemp()
        buf, old = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            flow.cleanup_step({"execution": {"cleanup_enabled": False}}, out)
        finally:
            sys.stdout = old
        p = os.path.join(out, "created_resources.json")
        self.assertTrue(os.path.isfile(p), "残留资源必须落清单文件，无论是否自动清理")

    def test_cleanup_step_deletes_when_enabled(self):
        _reset()
        R.CREATED_RESOURCES = [{"action": "1000107", "id": 7, "params": {}}]
        calls = []

        def fake_call(payload, retries=2):
            calls.append(payload)
            return 200, '{"result":true}', {"result": True}

        R.call = fake_call
        out = tempfile.mkdtemp()
        buf, old = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            flow.cleanup_step({"execution": {"cleanup_enabled": True,
                                             "cleanup_delete_action": "1000109"}}, out)
        finally:
            sys.stdout = old
        self.assertTrue(calls, "启用清理且提供删除 action 时必须真删除")
        self.assertEqual(calls[0].get("id"), 7)


# ---------------------------------------------------------------- 第三批
class TestCapCasesPriority(unittest.TestCase):
    """C1：截断必须保优先级，不得截掉 P0。"""

    def test_p0_survive_truncation(self):
        cases = [{"id": "p2a", "priority": "P2"}, {"id": "p2b", "priority": "P2"},
                 {"id": "p0a", "priority": "P0"}, {"id": "p1", "priority": "P1"}]
        buf, old = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            kept = flow.cap_cases(cases, {"max_cases": 2})
        finally:
            sys.stdout = old
        self.assertEqual([c["id"] for c in kept], ["p0a", "p1"], "截断按 P0>P1>P2 保留")


class TestRegressionWiring(unittest.TestCase):
    """C2：regression.py 接线——执行收尾落回归基线。"""

    def test_execute_writes_regression_baseline(self):
        out = tempfile.mkdtemp()
        doc = {"action": "1000107", "coverage_plan": ["B"],
               "cases": [{"id": "c1", "dimension": "B", "priority": "P0"}]}
        buf, old = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            flow.write_regression_artifacts(out, doc)
        finally:
            sys.stdout = old
        p = os.path.join(out, "regression_baseline.json")
        self.assertTrue(os.path.isfile(p))
        data = json.load(open(p, encoding="utf-8"))
        self.assertTrue(data.get("entries"))
        self.assertTrue(all(e.get("contract_hash") for e in data["entries"]),
                        "每条快照必须带契约哈希（STALE_BASELINE 判定的依据）")

    def test_retest_report_verdicts(self):
        base_path = os.path.join(tempfile.mkdtemp(), "rb.json")
        import regression as REG
        REG.save_baseline(base_path, [{"case_id": "c1", "contract_hash": "h1",
                                       "expectations": {"last_error": "e1"}}])
        verdicts = flow.retest_report(base_path, [{"case_id": "c1", "passed": False, "error": "e1"}])
        self.assertEqual(verdicts[0]["verdict"], "STILL_FAILING")


class TestLedgerClipping(unittest.TestCase):
    """D3：台账单元格超长截断，保可读；报告 JSON 仍保留全文。"""

    def test_long_request_clipped_in_csv_row(self):
        f = {"id": "N1", "kind": "缺陷", "desc": "d", "severity": "中", "status": "待修复",
             "dimension": "B", "expected": "e", "actual": "a",
             "request": json.dumps({"title": "x" * 3000}, ensure_ascii=False)}
        row = R.finding_row(dict(f))
        self.assertLess(len(row[6]), 700, "台账「请求Body」超长必须截断")
        self.assertIn("共", row[6], "截断必须标明原始长度")

    def test_full_json_report_keeps_full_body(self):
        R.results.clear()
        R.findings.clear()
        R.add_issue("d", {"title": "x" * 3000}, fid="N1", dimension="B")
        self.assertGreater(len(R.findings[-1]["request"]), 2500,
                           "完整报告（report_full.json / 内存 findings）不得截断，证据要保真")


class TestSmokeUiHints(unittest.TestCase):
    """D2：冒烟结果附 UI 线索（空值字段 / 异常 URL），不新增请求。"""

    def test_ui_hints_counts_empty_and_url(self):
        parsed = {"data": {"cover_url": "", "avatar": None, "title": "t"}}
        hints = SM.ui_hints(parsed)
        self.assertIn("空值", hints)
        self.assertIn("cover_url", hints)

    def test_ui_hints_empty_when_all_present(self):
        parsed = {"data": {"cover_url": "https://x/a.png", "title": "t"}}
        self.assertEqual(SM.ui_hints(parsed), "")


if __name__ == "__main__":
    unittest.main()
