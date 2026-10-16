# -*- coding: utf-8 -*-
"""TRACE 评测 P0 项回归测试。

每个测试对应评测报告「七、问题清单」中的一条 P0 问题，
断言的是「修复后必须成立的最小事实」，避免修复被后续改动悄悄回退。
"""
import io
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: F401,E402 环境替身（必须先于生产模块导入）


def _reset_runner():
    import api_test_runner as R
    R.results.clear()
    R.findings.clear()
    R.COVERAGE_PLAN = []
    R.coverage_blocked = False
    if hasattr(R, "shallow_passes"):
        R.shallow_passes = 0
    return R


# ---------------------------------------------------------------- P0-1 门禁
class TestCoverageGateHonoursStatus(unittest.TestCase):
    """P0-1：覆盖率门禁必须区分执行状态，被跳过的用例不算"触及维度"。"""

    def test_skipped_cases_do_not_count_as_covered(self):
        R = _reset_runner()
        R.COVERAGE_PLAN = ["AB", "AC"]
        for cid, dim in (("P02-1", "AB"), ("P08-1", "AC")):
            R.results.append({"case_no": cid, "name": "x", "status": "SKIPPED",
                              "dimension": dim, "http_code": 0, "response": {}})
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            R.coverage_gate()
        finally:
            sys.stdout = old
        out = buf.getvalue()
        self.assertNotIn("均已覆盖", out,
                         "被跳过的必测维度不得判为已覆盖：\n%s" % out)
        self.assertTrue(getattr(R, "coverage_blocked", False),
                        "必测维度全部被跳过时 coverage_blocked 必须为 True")
        self.assertGreaterEqual(len(R.findings), 1, "必须产出覆盖率缺口缺陷")

    def test_na_cases_do_not_count_as_covered(self):
        R = _reset_runner()
        R.COVERAGE_PLAN = ["AA"]
        R.results.append({"case_no": "AA1", "name": "x", "status": "N/A",
                          "dimension": "AA", "http_code": 0, "response": {}})
        buf, old = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            R.coverage_gate()
        finally:
            sys.stdout = old
        self.assertTrue(getattr(R, "coverage_blocked", False))

    def test_really_executed_dimension_still_passes(self):
        """修复不能误伤：真实执行过的维度仍应通过。"""
        R = _reset_runner()
        R.COVERAGE_PLAN = ["B"]
        R.results.append({"case_no": "B1", "name": "x", "status": "PASS",
                          "dimension": "B", "http_code": 200, "response": {}})
        buf, old = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            R.coverage_gate()
        finally:
            sys.stdout = old
        self.assertFalse(getattr(R, "coverage_blocked", False))
        self.assertIn("均已覆盖", buf.getvalue())

    def test_gate_reports_per_dimension_execution_count(self):
        """门禁应输出每个必测维度的实际执行数，让假绿无处藏身。"""
        R = _reset_runner()
        R.COVERAGE_PLAN = ["B", "AB"]
        R.results.append({"case_no": "B1", "name": "x", "status": "PASS",
                          "dimension": "B", "http_code": 200, "response": {}})
        R.results.append({"case_no": "AB1", "name": "y", "status": "SKIPPED",
                          "dimension": "AB", "http_code": 0, "response": {}})
        buf, old = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            R.coverage_gate()
        finally:
            sys.stdout = old
        out = buf.getvalue()
        self.assertIn("AB", out)
        self.assertIn("执行", out)


# ------------------------------------------------------- P0-2 产物脱敏
class TestSmokeArtifactsRedacted(unittest.TestCase):
    """P0-2：冒烟产物（结果 CSV / 问题 CSV / report JSON）必须脱敏敏感值。"""

    def _run(self):
        import smoke_runner
        d = tempfile.mkdtemp()
        cases = {"cases": [{"action": "1000107", "name": "创建", "params": {"title": "T"}}]}
        cfg = {"name": "redact_env", "base_url": "https://x.test/openapi/{endpoint}",
               "common_params": {"encode": "sha1", "appid": "3585", "sign": "REALSIGNVALUE123"},
               "account": {"main": "acc"}, "teardown_enabled": False}
        cp = os.path.join(d, "cfg.json")
        cc = os.path.join(d, "cases.json")
        open(cp, "w", encoding="utf-8").write(json.dumps(cfg, ensure_ascii=False))
        open(cc, "w", encoding="utf-8").write(json.dumps(cases, ensure_ascii=False))
        argv = sys.argv[:]
        sys.argv = ["smoke_runner.py", "--config", cp, "--cases", cc,
                    "--out-dir", os.path.join(d, "out"), "--dry-run"]
        try:
            smoke_runner.main()
        finally:
            sys.argv = argv
        return os.path.join(d, "out")

    def test_sign_is_masked_in_result_csv(self):
        out = self._run()
        files = [f for f in os.listdir(out) if f.startswith("smoke_results_")]
        self.assertTrue(files, "未产出结果 CSV")
        text = open(os.path.join(out, files[0]), encoding="utf-8-sig").read()
        self.assertNotIn("REALSIGNVALUE123", text,
                         "结果 CSV 泄漏了 sign 明文：\n%s" % text[:800])
        self.assertIn("sign", text, "脱敏不能把字段整体删掉，需保留字段名以便复现")
        # 只查完整密钥是不够的——截断式泄露（***SIGNVALUE123）会漏网，须查每一段
        for frag in ("REALSIGN", "SIGNVALUE", "VALUE123", "GNVALUE1"):
            self.assertNotIn(frag, text, "脱敏后仍残留 sign 片段 %r，等于泄露密钥：\n%s"
                             % (frag, text[:800]))


class TestRedactorMasksWholeSecret(unittest.TestCase):
    """脱敏必须把敏感值**整体**替成 ***，不得保留任何片段。

    历史缺陷：正则里 `\s` 多转义了一层，字符类在 `(?i)` 下把字母 `s` 一并排除，
    于是 `REALSIGNVALUE123` 只匹配到 `REAL`，产物里留下 `***SIGNVALUE123`——
    脱敏形同虚设，大半个密钥仍然落盘。
    """

    def setUp(self):
        import evidence_bundle as EB
        self.EB = EB

    def test_no_partial_leak_for_any_keyword(self):
        payload = ('{"sign":"ABCDEF123456","token":"tok_98765","password":"p@ssw0rd",'
                   '"api_key":"key-abc","cookie":"sid=xyz","secret":"topsecret9",'
                   '"title":"keep-me"}')
        out = self.EB.redact(payload)
        for secret in ("ABCDEF123456", "tok_98765", "p@ssw0rd", "key-abc", "sid=xyz",
                       "topsecret9"):
            self.assertNotIn(secret, out, "敏感值 %r 未被完整脱敏：%s" % (secret, out))
            for frag in (secret[3:], secret[:4]):
                if len(frag) >= 4:
                    self.assertNotIn(frag, out,
                                     "敏感值 %r 的片段 %r 泄漏：%s" % (secret, frag, out))
        self.assertIn("keep-me", out, "非敏感字段不应被误脱敏")
        self.assertIn("***", out)

    def test_dict_payload_masks_value_entirely(self):
        out = self.EB.redact({"sign": "ABCDEF123456", "title": "keep"})
        self.assertNotIn("ABCDEF", out)
        self.assertIn("keep", out)

    def test_bundle_file_has_no_secret_fragment(self):
        d = tempfile.mkdtemp()
        path = self.EB.write_bundle(d, "run1", "case1",
                                    {"sign": "ABCDEF123456", "title": "T"})
        text = open(path, encoding="utf-8").read()
        self.assertNotIn("ABCDEF", text)


# --------------------------------------------------- P0-3 dry-run 请求体
class TestDryRunRequestBody(unittest.TestCase):
    """P0-3：dry-run 也必须记录完整请求报文，不得留空或错位。"""

    def _run(self):
        import smoke_runner
        d = tempfile.mkdtemp()
        cases = {"cases": [{"action": "1000107", "name": "创建", "params": {"title": "T"}}]}
        cfg = {"name": "dry_env", "base_url": "https://x.test/openapi/{endpoint}",
               "common_params": {"encode": "sha1", "appid": "3585", "sign": "S"},
               "account": {"main": "acc"}, "teardown_enabled": False}
        cp, cc = os.path.join(d, "cfg.json"), os.path.join(d, "cases.json")
        open(cp, "w", encoding="utf-8").write(json.dumps(cfg, ensure_ascii=False))
        open(cc, "w", encoding="utf-8").write(json.dumps(cases, ensure_ascii=False))
        argv = sys.argv[:]
        sys.argv = ["smoke_runner.py", "--config", cp, "--cases", cc,
                    "--out-dir", os.path.join(d, "out"), "--dry-run"]
        try:
            smoke_runner.main()
        finally:
            sys.argv = argv
        return os.path.join(d, "out")

    def test_dry_run_request_body_contains_public_params(self):
        import csv
        out = self._run()
        f = [x for x in os.listdir(out) if x.startswith("smoke_results_")][0]
        rows = list(csv.DictReader(open(os.path.join(out, f), encoding="utf-8-sig")))
        self.assertEqual(len(rows), 1)
        body = rows[0].get("请求Body") or ""
        self.assertNotIn(body.strip(), ("", "{}"),
                         "dry-run 下请求Body 不得为空：%r" % body)
        self.assertIn("1000107", body, "请求体应含 action")
        self.assertIn("encode", body, "请求体应含公共参数 encode")

    def test_dry_run_response_body_is_not_the_request(self):
        import csv
        out = self._run()
        f = [x for x in os.listdir(out) if x.startswith("smoke_results_")][0]
        rows = list(csv.DictReader(open(os.path.join(out, f), encoding="utf-8-sig")))
        resp = rows[0].get("响应Body") or ""
        self.assertNotIn("1000107", resp,
                         "dry-run 未发起调用，响应体不应承载请求参数：%r" % resp)


# ------------------------------------------- P0-4 可自动化检查不得永久跳过
class TestAutomatableChecksAreWired(unittest.TestCase):
    """P0-4：技术上可自动化的检查必须真正执行，不得放进 MANUAL_CHECKS 永久跳过。"""

    AUTOMATABLE = ("pagination", "uniqueness", "roundtrip", "http_semantics", "terminal_repeat")

    def test_automatable_checks_not_in_manual_list(self):
        import case_runner as CR
        for name in self.AUTOMATABLE:
            self.assertNotIn(name, CR.MANUAL_CHECKS,
                             "%s 可自动化，不应放在 MANUAL_CHECKS（等于永久不执行）" % name)

    def test_automatable_checks_have_implementation(self):
        import case_runner as CR
        for name in self.AUTOMATABLE:
            self.assertIn(name, CR.CHECKS, "%s 缺少执行实现" % name)

    def test_exec_case_does_not_skip_automatable_check(self):
        """带可自动化 check 的用例在 dry=False 时不得被记为 SKIPPED。"""
        import case_runner as CR
        import api_test_runner as R
        _reset_runner()
        case = {"id": "T1", "name": "t", "dimension": "H", "action": "1000107",
                "params": {"title": "x"}, "check": "http_semantics",
                "check_args": {"expected_http": 200}, "expect": {"success": True}}
        # 用 fake transport 替代真实网络，避免依赖外部环境
        orig = R.call
        R.call = lambda payload, retries=2: (200, '{"result": true}', {"result": True})
        try:
            CR.exec_case(case, dry=False)
        finally:
            R.call = orig
        statuses = [r.get("status") for r in R.results]
        self.assertNotIn("SKIPPED", statuses,
                         "可自动化检查不应被跳过，实际状态：%s" % statuses)


# --------------------------------- P0-5 控制参数不得泄漏进真实请求报文
class TestControlKeysDoNotLeakIntoRequest(unittest.TestCase):
    """P0-5：dimension 等框架控制参数绝不能进入真实请求报文，且 run() 不得崩溃。

    历史缺陷：run() 未声明 dimension → dimension 落进 **kwargs 被拼进请求，
    既污染发给服务端的报文，又在结果记录处抛 NameError 使深度执行链整体崩溃。"""

    def _fake(self):
        import api_test_runner as R
        captured = {}
        orig = R.call

        def fake_call(payload, retries=2):
            captured["payload"] = dict(payload)
            return 200, '{"result": true}', {"result": True}

        R.call = fake_call
        return R, captured, orig

    def test_dimension_not_sent_in_request(self):
        R, captured, orig = self._fake()
        try:
            R.run("C1", "n", "1000107", True, "cat", dimension="H", title="T")
        finally:
            R.call = orig
        self.assertNotIn("dimension", captured["payload"],
                         "dimension 是框架参数，不得进入请求：%s" % captured["payload"])
        self.assertEqual(captured["payload"].get("title"), "T")

    def test_dimension_recorded_in_results(self):
        R, _c, orig = self._fake()
        R.results.clear()
        try:
            R.run("C1", "n", "1000107", True, "cat", dimension="H", title="T")
        finally:
            R.call = orig
        self.assertEqual(R.results[-1]["dimension"], "H",
                         "dimension 必须写入结果，供覆盖率门禁统计")

    def test_run_does_not_raise_nameerror(self):
        """run() 全路径不得抛 NameError（此前 results.append 处引用未定义局部变量）。"""
        import api_test_runner as R
        orig = R.call
        R.call = lambda payload, retries=2: (200, '{"result": true}', {"result": True})
        R.results.clear()
        try:
            R.run("C2", "n", "1000107", True, "cat", dimension="B", title="T")
        except NameError as e:
            self.fail("run() 抛 NameError：%s" % e)
        finally:
            R.call = orig

    def test_unexpected_control_key_is_rejected_loudly(self):
        """未知控制键应显式报错，而不是静默拼进请求。"""
        import api_test_runner as R
        orig = R.call
        R.call = lambda payload, retries=2: (200, '{"result": true}', {"result": True})
        try:
            with self.assertRaises(TypeError):
                R.run("C3", "n", "1000107", True, "cat", dimension="B",
                      category="不该出现", title="T")
        finally:
            R.call = orig


if __name__ == "__main__":
    unittest.main()
