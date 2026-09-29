# -*- coding: utf-8 -*-
"""把「可自动化的跳过项」真正实现掉：transport_contract / enum_exhaustive / concurrency / rate_limit。

这些检查此前躺在 MANUAL_CHECKS 里等于永久不执行——判据是"名字在不在字典里"，
属编码期事实，与运行时条件无关。本文件同时锁定两件事：
  1) 它们已具备可执行实现，且不再出现在 MANUAL_CHECKS；
  2) 实现本身不引入新的假绿（空转恒 PASS）与新的误报（把"没观测到"判成缺陷）。
"""
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import api_test_runner as R   # noqa: E402
import case_runner as CR      # noqa: E402


def _reset():
    R.results.clear()
    R.findings.clear()
    R.LAST_RESPONSE_HEADERS = {}
    return R


class _FakeResp(object):
    """最小的 urlopen 替身：只提供 read()/getcode()/headers。"""

    def __init__(self, body, headers):
        self._body = body.encode("utf-8")
        self.headers = headers

    def read(self):
        return self._body

    def getcode(self):
        return 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _CallRestoreMixin(object):
    """monkeypatch R.call 后必须恢复原函数。

    坑：不能在赋值之后才 addCleanup(setattr, R, "call", R.call)——那时读到的是
    已被替换的值，cleanup 等于把假函数再装回去，会污染后续所有测试用例。
    """

    def setUp(self):
        self._orig_call = R.call

    def tearDown(self):
        R.call = self._orig_call


class TestManualChecksShrunk(unittest.TestCase):
    """可自动化项必须离开 MANUAL_CHECKS 并进入 CHECKS。"""

    IMPLEMENTED = ("transport_contract", "enum_exhaustive", "concurrency", "rate_limit")

    def test_implemented_checks_left_manual(self):
        for name in self.IMPLEMENTED:
            self.assertNotIn(
                name, CR.MANUAL_CHECKS,
                "%s 已实现，不得再留在 MANUAL_CHECKS（等于永久不执行）" % name)
            self.assertIn(name, CR.CHECKS, "%s 缺少执行实现" % name)

    def test_truly_blocked_checks_still_declared(self):
        """反向对照：真缺外部能力的项仍须留在 MANUAL_CHECKS，不能连带"实现"掉。"""
        for name in ("event_delivery", "timezone", "batch_partial"):
            self.assertIn(name, CR.MANUAL_CHECKS,
                          "%s 缺外部观测通道/第二信息源，应仍留在 MANUAL_CHECKS" % name)


class TestResponseHeadersReachable(unittest.TestCase):
    """transport_contract 的前置：响应头必须能被检查读到，且不破坏 call() 的既有契约。"""

    def test_call_still_returns_triple(self):
        _reset()
        with mock.patch("urllib.request.urlopen",
                        return_value=_FakeResp('{"result":true}',
                                               {"Content-Type": "application/json"})):
            out = R.call({"action": "1"})
        self.assertEqual(len(out), 3, "call() 必须仍是三元组——全仓有十余处按此解包")

    def test_call_records_response_headers(self):
        _reset()
        with mock.patch("urllib.request.urlopen",
                        return_value=_FakeResp('{"result":true}',
                                               {"Content-Type": "application/json; charset=utf-8",
                                                "X-Trace-Id": "trace-1"})):
            R.call({"action": "1"})
        self.assertEqual(R.LAST_RESPONSE_HEADERS.get("X-Trace-Id"), "trace-1",
                         "响应头必须被记录，否则 transport_contract 永远无从检查")

    def test_call_meta_returns_four_tuple(self):
        _reset()
        with mock.patch("urllib.request.urlopen",
                        return_value=_FakeResp('{"result":true}',
                                               {"Content-Type": "application/json"})):
            code, _raw, parsed, headers = R.call_meta({"action": "1"})
        self.assertEqual(code, 200)
        self.assertIs(parsed.get("result"), True)
        self.assertIn("Content-Type", headers)


class TestTransportContractCheck(unittest.TestCase):
    """AE：传输层契约——光看 body 发现不了网关/SDK/中间件问题。"""

    def _case(self):
        return {"id": "AE01", "action": "1000107", "params": {}, "dimension": "AE"}

    def test_missing_content_type_is_defect(self):
        _reset()
        R.LAST_RESPONSE_HEADERS = {"X-Trace-Id": "t"}
        CR._check_transport_contract(self._case(), {"result": True}, {})
        self.assertTrue([f for f in R.findings if f.get("kind") == "缺陷"],
                        "响应缺 content-type 应记缺陷")

    def test_non_json_content_type_is_defect(self):
        _reset()
        R.LAST_RESPONSE_HEADERS = {"Content-Type": "text/html"}
        CR._check_transport_contract(self._case(), {"result": True}, {})
        self.assertTrue([f for f in R.findings if f.get("kind") == "缺陷"],
                        "声明 JSON 接口却回 text/html 应记缺陷")

    def test_missing_trace_id_is_only_observation(self):
        """无 trace-id 不判缺陷——很多环境由网关按需注入，硬判会制造误报。"""
        _reset()
        R.LAST_RESPONSE_HEADERS = {"Content-Type": "application/json; charset=utf-8"}
        CR._check_transport_contract(self._case(), {"result": True}, {})
        self.assertFalse([f for f in R.findings if f.get("kind") == "缺陷"],
                         "缺 trace-id 不得判缺陷")
        self.assertTrue(R.findings, "结论必须留痕，不得静默")

    def test_no_headers_recorded_reports_gap(self):
        _reset()
        R.LAST_RESPONSE_HEADERS = {}
        CR._check_transport_contract(self._case(), {"result": True}, {})
        self.assertTrue(R.findings, "拿不到响应头时必须记缺口，不得静默通过")


class TestEnumExhaustiveCheck(_CallRestoreMixin, unittest.TestCase):
    """K/Top6：文档声明的每个枚举值，写接口都必须接受（读写一致性）。"""

    def _case(self, params=None):
        return {"id": "K-ENUM-status", "action": "1000107", "dimension": "K",
                "params": params if params is not None else {"status": 0}}

    def test_empty_documented_is_not_silent_pass(self):
        """空转防护（陷阱 17）：文档枚举为空时循环 0 次会恒 PASS。"""
        _reset()
        CR._check_enum_exhaustive(self._case(), {"result": True},
                                  {"field": "status", "documented": []})
        self.assertTrue(R.findings, "枚举文档为空必须记缺口，不得静默通过")

    def test_all_values_accepted_passes(self):
        _reset()
        R.call = lambda payload, retries=2: (200, '{"result":true}', {"result": True, "data": {}})
        CR._check_enum_exhaustive(self._case(), {"result": True},
                                  {"field": "status", "documented": [0, 1]})
        self.assertFalse([f for f in R.findings if f.get("kind") == "缺陷"],
                         "文档枚举全部被接受时不应记缺陷")

    def test_rejected_documented_value_is_defect(self):
        _reset()

        def fake_call(payload, retries=2):
            if payload.get("status") == 1:
                return 200, '{"result":false,"errormsg":"status 非法"}', \
                    {"result": False, "errormsg": "status 非法"}
            return 200, '{"result":true}', {"result": True, "data": {}}

        R.call = fake_call
        CR._check_enum_exhaustive(self._case(), {"result": True},
                                  {"field": "status", "documented": [0, 1]})
        self.assertTrue([f for f in R.findings if f.get("kind") == "缺陷"],
                        "文档声明却写不进去的枚举值应记缺陷（读写不一致）")


class TestConcurrencyCheck(_CallRestoreMixin, unittest.TestCase):
    """J2/AC5：并发重复提交不得产生重复数据。"""

    def _case(self):
        return {"id": "J02", "action": "1000107", "dimension": "J",
                "params": {"title": "并发测试"}}

    def test_times_are_hard_capped(self):
        """误配置 times=999 不能真的打 999 次——必须有硬上限。"""
        _reset()
        seen = []
        R.call = lambda payload, retries=2: (seen.append(1), (200, '{"result":true}', {"result": True}))[1]
        CR._check_concurrency(self._case(), {"result": True}, {"times": 999})
        self.assertLessEqual(len(seen), CR.CONCURRENCY_MAX_TIMES,
                             "并发次数必须被硬上限截断（防误配置打爆服务）")
        self.assertGreater(len(seen), 0, "必须真的发过请求")

    def test_duplicate_success_is_defect_when_unique_field_known(self):
        _reset()
        R.call = lambda payload, retries=2: (200, '{"result":true}', {"result": True})
        CR._check_concurrency(self._case(), {"result": True},
                              {"times": 3, "unique_field": "title"})
        self.assertTrue([f for f in R.findings if f.get("kind") == "缺陷"],
                        "已知唯一键时并发全部成功应记缺陷（重复创建）")

    def test_duplicate_success_without_unique_field_is_observation(self):
        """不知道业务键是否唯一时不判缺陷——否则会把"允许多条"的正常接口判成 bug。"""
        _reset()
        R.call = lambda payload, retries=2: (200, '{"result":true}', {"result": True})
        CR._check_concurrency(self._case(), {"result": True}, {"times": 3})
        self.assertFalse([f for f in R.findings if f.get("kind") == "缺陷"],
                         "缺 unique_field 时不得判缺陷")
        self.assertTrue(R.findings, "并发结果必须留痕")


class TestRateLimitCheck(_CallRestoreMixin, unittest.TestCase):
    """J/Top9：限流的可观测性——结论必须是观察，不能把"没观测到"判成缺陷。"""

    def _case(self):
        return {"id": "J01", "action": "1000107", "dimension": "J", "params": {}}

    def test_no_limiting_observed_is_not_defect(self):
        _reset()
        R.call = lambda payload, retries=2: (200, '{"result":true}', {"result": True})
        CR._check_rate_limit(self._case(), {"result": True}, {"times": 3})
        self.assertFalse([f for f in R.findings if f.get("kind") == "缺陷"],
                         "未观测到限流不得判缺陷：阈值可能高于本次次数，真实阈值需压测授权")
        self.assertTrue(R.findings, "结论必须留痕")

    def test_http_429_records_observed_threshold(self):
        _reset()
        state = {"n": 0}

        def fake_call(payload, retries=2):
            state["n"] += 1
            if state["n"] >= 2:
                return 429, '{"result":false,"errormsg":"too many requests"}', \
                    {"result": False, "errormsg": "too many requests"}
            return 200, '{"result":true}', {"result": True}

        R.call = fake_call
        CR._check_rate_limit(self._case(), {"result": True}, {"times": 5})
        text = " ".join(str(f.get("desc", "")) + str(f.get("actual", "")) for f in R.findings)
        self.assertIn("限流", text, "观测到 429 必须留下「限流已生效」的记录")
        self.assertLessEqual(state["n"], CR.RATE_LIMIT_MAX_TIMES + 1,
                             "观测次数必须受上限约束")

    def test_times_are_hard_capped(self):
        _reset()
        seen = []
        R.call = lambda payload, retries=2: (seen.append(1), (200, '{"result":true}', {"result": True}))[1]
        CR._check_rate_limit(self._case(), {"result": True}, {"times": 999})
        self.assertLessEqual(len(seen), CR.RATE_LIMIT_MAX_TIMES,
                             "连发次数必须被硬上限截断")


if __name__ == "__main__":
    unittest.main()
