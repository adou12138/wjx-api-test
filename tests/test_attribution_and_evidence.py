import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from attribution_engine import attribute
from evidence_bundle import write_bundle
from minimizer import minimize_body


class TestAttributionAndEvidence(unittest.TestCase):
    def test_retry_network_error_is_environment(self):
        self.assertEqual(attribute("offline", retries=2)["category"], "ENV")

    def test_rate_limit_is_not_business_defect(self):
        self.assertEqual(attribute(response="请求频率过快")["category"], "RATE_LIMIT")

    def test_minimizer_keeps_failure_with_smallest_body(self):
        result = minimize_body({"action": 1, "title": "bad", "unused": 1}, lambda body: "title" in body)
        self.assertEqual(result["body"], {"title": "bad"})

    def test_evidence_redacts_sign(self):
        with tempfile.TemporaryDirectory() as out:
            path = write_bundle(out, "run1", "case1", {"sign": "secret", "result": False})
            text = Path(path).read_text(encoding="utf-8")
            self.assertNotIn("secret", text)


if __name__ == "__main__":
    unittest.main()
