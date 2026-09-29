import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import contract_model


class TestContractModel(unittest.TestCase):
    def test_round_trip_preserves_field_evidence(self):
        model = contract_model.ContractModel(
            operation="1000107",
            method="POST",
            path="/openapi/default.aspx",
            fields=[contract_model.FieldModel(
                name="title", location="body", type="string", required=True,
                evidence=[contract_model.Evidence(source="contract.md", line=12, confidence=0.94)]
            )],
        )
        restored = contract_model.ContractModel.from_dict(model.to_dict())
        self.assertEqual(restored.fields[0].name, "title")
        self.assertEqual(restored.fields[0].evidence[0].confidence, 0.94)
        self.assertEqual(restored.operation, "1000107")

    def test_evidence_confidence_must_be_between_zero_and_one(self):
        with self.assertRaises(ValueError):
            contract_model.Evidence(source="x", confidence=1.1)

    def test_load_contract_model_from_existing_parser(self):
        path = Path(__file__).parent / "fixtures" / "simple_contract.md"
        model = contract_model.load_contract_model(str(path))
        self.assertEqual(model.method, "POST")
        self.assertTrue(any(field.name == "title" for field in model.fields))
        self.assertTrue(model.fields[0].evidence)


if __name__ == "__main__":
    unittest.main()
