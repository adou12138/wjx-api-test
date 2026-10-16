# -*- coding: utf-8 -*-
"""空断言集合与幂等假通过防御（批次1 P0：W3/W4）。

两个「判据默认放行」型假通过：
- W3 check_idempotent：接口全失败时 None==None 恒真 → 判「幂等通过」。
  修复后：全失败/响应缺 result 键 → 返回 None（归 ENV/需重跑），绝不返回 True。
- W4 assertion_engine：assert_contract(body, []) / assert_error_keywords(body, [])
  空集合循环 0 次恒 ok=True。修复后：空集合返回 ok=False, code=VACUOUS，
  由调用方记覆盖缺口（断言没验到任何东西 ≠ 验证通过）。
"""
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import _bootstrap  # noqa: F401,E402 环境替身

import api_test_runner as R   # noqa: E402
import assertion_engine as AE  # noqa: E402


def _make_fake_call(codes_and_bodies):
    """按调用次序回放 (code, body_dict) 序列。"""
    seq = list(codes_and_bodies)
    state = {"i": 0}

    def fake_call(payload, retries=2, extra_headers=None):
        i = min(state["i"], len(seq) - 1)
        code, body = seq[i]
        state["i"] += 1
        raw = json.dumps(body, ensure_ascii=False)
        return code, raw, body if code != -1 else None
    return fake_call


class TestIdempotentNeverPassesOnFailure(unittest.TestCase):
    """W3：接口全失败时绝不返回 ok=True。"""

    def setUp(self):
        self._orig = R.call
        R.results.clear()
        R.findings.clear()

    def tearDown(self):
        R.call = self._orig

    def test_all_conn_fail_returns_none(self):
        """全 code=-1（连接挂）→ None，不是 True。"""
        R.call = _make_fake_call([(-1, None), (-1, None)])
        ok, _results, msg = R.check_idempotent("1000107", lambda: {"title": "t"})
        self.assertIsNone(ok, "连接全失败时不得判幂等通过: %s" % msg)
        self.assertIn("code=-1", msg)

    def test_all_none_result_returns_none(self):
        """全返回非 JSON/缺 result（parsed 恒 None）→ None，不是 True。"""
        R.call = _make_fake_call([(200, {"errmsg": "html error"}),
                                  (200, {"errmsg": "html error"})])
        ok, _results, msg = R.check_idempotent("1000107", lambda: {"title": "t"})
        self.assertIsNone(ok, "响应缺 result 键时不得判幂等通过: %s" % msg)

    def test_same_success_still_true(self):
        """回归保护：两次成功且一致 → 仍 True（不误伤正常路径）。"""
        body = {"result": True, "data": {"vid": 1}}
        R.call = _make_fake_call([(200, body), (200, body)])
        ok, _results, msg = R.check_idempotent("1000107", lambda: {"title": "t"})
        self.assertTrue(ok)
        self.assertIn("幂等性通过", msg)

    def test_differing_results_still_false(self):
        """回归保护：两次成功但结果漂移 → 仍 False。"""
        R.call = _make_fake_call([(200, {"result": True, "data": {"vid": 1}}),
                                  (200, {"result": True, "data": {"vid": 2}})])
        ok, _results, _msg = R.check_idempotent("1000107", lambda: {"title": "t"})
        self.assertFalse(ok)


class TestVacuousAssertions(unittest.TestCase):
    """W4：空断言集合恒 PASS 的陷阱（assert_non_empty 同源）。"""

    def test_contract_empty_required_is_vacuous(self):
        r = AE.assert_contract({"vid": 1}, [])
        self.assertFalse(r.ok, "空 required_fields 循环 0 次恒 PASS 是假通过")
        self.assertEqual(r.code, "VACUOUS")

    def test_contract_nonempty_missing_detected(self):
        r = AE.assert_contract({}, ["vid"])
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "RESPONSE_CONTRACT")

    def test_contract_nonempty_pass(self):
        r = AE.assert_contract({"vid": 1}, ["vid"])
        self.assertTrue(r.ok)

    def test_error_keywords_empty_is_vacuous(self):
        r = AE.assert_error_keywords({"errmsg": "标题不能为空"}, [])
        self.assertFalse(r.ok, "空 keywords 循环 0 次恒 PASS 是假通过")
        self.assertEqual(r.code, "VACUOUS")

    def test_error_keywords_hit(self):
        r = AE.assert_error_keywords({"errmsg": "标题不能为空"}, ["标题不能为空"])
        self.assertTrue(r.ok)

    def test_error_keywords_miss(self):
        r = AE.assert_error_keywords({"errmsg": "系统错误"}, ["标题不能为空"])
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "ERROR_SEMANTICS")


if __name__ == "__main__":
    unittest.main()
