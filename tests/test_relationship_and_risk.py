import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from contract_model import ContractModel, FieldModel
from relationship_inference import infer_relations
from risk_engine import score


class TestRelationshipAndRisk(unittest.TestCase):
    def test_status_and_resource_id_infer_relationships(self):
        model = ContractModel(operation="updateHomepage", name="update homepage", path="/homepage/update",
                              fields=[FieldModel("homepage_id"), FieldModel("status", type="int")])
        relations = infer_relations(model)
        self.assertTrue(any(r.relation_type == "update" for r in relations))
        self.assertTrue(any(r.relation_type == "state_machine" for r in relations))

    def test_write_state_and_auth_risk_is_deterministic(self):
        model = ContractModel(operation="1000108", name="更新主页", path="/update",
                              fields=[FieldModel("appid", type="int"), FieldModel("status", type="int")])
        first = score(model)
        second = score(model)
        self.assertEqual(first, second)
        self.assertGreaterEqual(first["score"], 40)
        self.assertIn("breakdown", first)


if __name__ == "__main__":
    unittest.main()
