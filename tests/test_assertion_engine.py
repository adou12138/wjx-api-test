import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assertion_engine import assert_contract, assert_error_keywords, assert_http_semantics


class TestAssertionEngine(unittest.TestCase):
    def test_server_error_fails(self):
        result = assert_http_semantics(500, "server_error")
        self.assertFalse(result.ok)

    def test_required_response_field_is_enforced(self):
        self.assertFalse(assert_contract({"result": True}, ["result", "resource_id"]).ok)

    def test_error_keyword_is_required(self):
        self.assertFalse(assert_error_keywords({"message": "invalid title"}, ["appid"]).ok)


if __name__ == "__main__":
    unittest.main()
