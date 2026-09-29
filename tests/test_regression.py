import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from regression import contract_hash, select_cases


class TestRegression(unittest.TestCase):
    def test_targeted_regression_selects_linked_cases(self):
        cases = [{"id": "A"}, {"id": "B"}]
        self.assertEqual(select_cases(cases, "targeted", {"case_ids": ["B"]}), [{"id": "B"}])

    def test_contract_hash_is_stable(self):
        self.assertEqual(contract_hash({"b": 1, "a": 2}), contract_hash({"a": 2, "b": 1}))


if __name__ == "__main__":
    unittest.main()
