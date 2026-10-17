import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assertion_engine import assert_contract, assert_error_keywords, assert_http_semantics


class TestAssertionEngine(unittest.TestCase):
    def test_server_error_semantic_matches_real_500(self):
        # 【审查修复·P2 同步】声明 semantic="server_error" 且实际拿到 500：预期与
        # 实际一致，应通过——旧版被 5xx 兜底翻成恒 FAIL（整类假缺陷）。
        self.assertTrue(assert_http_semantics(500, "server_error").ok)

    def test_server_error_semantic_rejects_other_5xx(self):
        self.assertFalse(assert_http_semantics(502, "server_error").ok)

    def test_5xx_never_passes_without_explicit_semantic(self):
        self.assertFalse(assert_http_semantics(500, "unmapped_semantic").ok)
        self.assertFalse(assert_http_semantics(503, "").ok)

    def test_required_response_field_is_enforced(self):
        self.assertFalse(assert_contract({"result": True}, ["result", "resource_id"]).ok)

    def test_error_keyword_is_required(self):
        self.assertFalse(assert_error_keywords({"message": "invalid title"}, ["appid"]).ok)


if __name__ == "__main__":
    unittest.main()
