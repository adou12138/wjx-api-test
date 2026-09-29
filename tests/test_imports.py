import importlib
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


class TestExistingModulesImport(unittest.TestCase):
    def test_existing_modules_import_without_network_or_personal_config(self):
        for name in (
            "contract_parser",
            "field_semantics",
            "applicability",
            "case_generator",
            "case_runner",
        ):
            with self.subTest(module=name):
                module = importlib.import_module(name)
                self.assertIsNotNone(module)


if __name__ == "__main__":
    unittest.main()
