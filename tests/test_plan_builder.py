import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from contract_model import ContractModel, FieldModel
from plan_builder import build_plan


class TestPlanBuilder(unittest.TestCase):
    def test_missing_ui_baseline_is_blocked_not_required(self):
        model = ContractModel(operation="1000107", fields=[FieldModel("title")])
        plan = build_plan(model, {"coverage_plan": ["B"], "optional_plan": ["AB"]})
        self.assertEqual(plan["required"], ["B"])
        self.assertEqual(plan["blocked"][0]["dimension"], "AB")


if __name__ == "__main__":
    unittest.main()
