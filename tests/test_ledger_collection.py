# -*- coding: utf-8 -*-
"""台账采集类字段（http_code / request_id / trace_id）的补齐测试。

背景：这三列的**输入一直在手上**——HTTP 状态码是 `call()` 返回值的第一项，
响应头由 `_record_response_headers` 旁路采好，但此前没有任何代码把它们交给
`add_issue`/`add_observation`，导出的 CSV 里恒为空。消费方看到的是一张
"有证据链列、但证据链列全空"的表，且没有任何报错提示。

本类锁住：run() / assert_validation() 两条路径的缺陷条目都必须带上采集事实。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import api_test_runner as R  # noqa: E402


class TestRequestIdExtraction(unittest.TestCase):
    """响应头名各系统不统一，提取要够宽；取不到就留空，不填占位值。"""

    def test_common_header_names(self):
        h = {"X-Request-Id": "r1", "X-Trace-Id": "t1"}
        self.assertEqual(R.extract_request_ids(h), ("r1", "t1"))

    def test_case_insensitive(self):
        self.assertEqual(R.extract_request_ids({"x-request-id": "r", "x-trace-id": "t"}),
                         ("r", "t"))

    def test_alternative_names(self):
        self.assertEqual(R.extract_request_ids({"X-Correlation-Id": "c1"}), ("c1", ""))
        self.assertEqual(R.extract_request_ids({"X-B3-TraceId": "b3"}), ("", "b3"))

    def test_no_headers_returns_empty(self):
        """采不到就留空——空值是可见的"没采到"，填占位值反而是假的。"""
        self.assertEqual(R.extract_request_ids(None), ("", ""))
        self.assertEqual(R.extract_request_ids({}), ("", ""))


class TestAutoEvidence(unittest.TestCase):
    def test_reads_side_channel_headers(self):
        R._record_response_headers({"X-Request-Id": "rid-9"})
        ev = R.auto_evidence(503)
        self.assertEqual(ev["http_code"], 503)
        self.assertEqual(ev["request_id"], "rid-9")

    def test_explicit_headers_override_side_channel(self):
        """显式传入优先：并发场景下旁路缓存可能是别的请求的。"""
        R._record_response_headers({"X-Request-Id": "stale"})
        ev = R.auto_evidence(200, headers={"X-Request-Id": "fresh"})
        self.assertEqual(ev["request_id"], "fresh")


class TestLedgerCollection(unittest.TestCase):
    """两条执行路径的缺陷条目都必须带上采集事实。"""

    def setUp(self):
        self._orig_call = R.call
        R.findings.clear()
        R.results.clear()

    def tearDown(self):
        R.call = self._orig_call

    def _mock(self, code, err, headers=None):
        def fake_call(payload, retries=2, extra_headers=None):
            R._record_response_headers(headers or {})
            body = '{"result":false,"errormsg":"%s"}' % err
            return code, body, {"result": False, "errormsg": err}
        R.call = fake_call

    def test_run_records_http_code_on_defect(self):
        """5xx 场景：run() 记的缺陷必须带 http_code。"""
        self._mock(500, "内部错误", {"X-Request-Id": "req-1", "X-Trace-Id": "tr-1"})
        R.run("T01", "创建", "1000107", True, "正常")
        defects = [f for f in R.findings if f["kind"] == "缺陷"]
        self.assertTrue(defects, "应记录缺陷")
        for d in defects:
            self.assertEqual(d["http_code"], 500)
            self.assertEqual(d["request_id"], "req-1")
            self.assertEqual(d["trace_id"], "tr-1")

    def test_error_semantics_mismatch_records_http_code(self):
        """错误语义不匹配（最常见的失败形态）也要带 http_code。"""
        self._mock(400, "标题超长", {"X-Request-Id": "rid-2"})
        R.run("T02", "超长标题", "1000107", False, "参数校验", err_exact=["别的原因"])
        defects = [f for f in R.findings if f["kind"] == "缺陷"]
        self.assertTrue(defects)
        self.assertEqual(defects[0]["http_code"], 400)
        self.assertEqual(defects[0]["request_id"], "rid-2")

    def test_network_error_records_minus_one(self):
        """连接失败（code=-1）也要落 -1，不能留空——"没发出去"和"没记录"要能区分。"""
        def fake_call(payload, retries=2, extra_headers=None):
            return -1, "EXCEPTION:TimeoutError:timed out", None
        R.call = fake_call
        R.run("T03", "创建", "1000107", True, "正常")
        obs = [f for f in R.findings if f["kind"] == "待确认"]
        self.assertTrue(obs)
        self.assertEqual(obs[0]["http_code"], -1)

    def test_ledger_row_contains_collected_values(self):
        """端到端：采集值必须真的出现在渲染行里（不是只落在 findings 本体）。"""
        self._mock(503, "服务不可用", {"X-Request-Id": "req-render"})
        R.run("T04", "创建", "1000107", True, "正常")
        row = dict(zip(R.FINDING_CSV_COLS, R.finding_row(R.findings[0])))
        self.assertEqual(row["HTTP状态码"], 503)
        self.assertEqual(row["请求ID"], "req-render")


if __name__ == "__main__":
    unittest.main(verbosity=2)