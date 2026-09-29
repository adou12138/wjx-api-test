import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from case_generator import generate


class TestIntelligentCaseGeneration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = generate(str(ROOT / "tests" / "fixtures" / "simple_contract.md"))

    def test_optional_field_gets_over_validation_states(self):
        cases = [c for c in self.doc["cases"] if c.get("assertion_mode") == "over_validation"]
        self.assertGreaterEqual(len(cases), 2)
        states = {c.get("mutation_state") for c in cases}
        self.assertTrue({"missing", "null", "empty_string"}.issubset(states))
        self.assertTrue(all(c.get("expect", {}).get("success") is True for c in cases))

    def test_cases_carry_risk_evidence_and_cleanup_metadata(self):
        self.assertTrue(self.doc["cases"])
        self.assertTrue(all("risk" in c and "evidence" in c and "cleanup" in c for c in self.doc["cases"]))

    def test_invalid_mutations_never_expect_success(self):
        invalid = [c for c in self.doc["cases"] if c.get("category", "") in ("参数校验-必填", "参数校验-类型", "参数校验-边界") and "边界内" not in c.get("name", "")]
        self.assertTrue(invalid)
        self.assertTrue(all(c.get("expect", {}).get("success") is False for c in invalid))


if __name__ == "__main__":
    unittest.main()
