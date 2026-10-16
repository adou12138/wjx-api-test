import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests"))
import _bootstrap  # noqa: F401,E402 环境替身（必须先于生产模块导入）
import flow


class TestIntelligentFlow(unittest.TestCase):
    def test_build_intelligence_artifacts_contains_plan_risk_and_evidence(self):
        contract = ROOT / "tests" / "fixtures" / "simple_contract.md"
        with tempfile.TemporaryDirectory() as out:
            result = flow.build_intelligence_artifacts(str(contract), out)
            self.assertIn("plan", result)
            self.assertIn("risk", result)
            self.assertTrue(Path(result["plan_path"]).exists())
            payload = json.loads(Path(result["plan_path"]).read_text(encoding="utf-8"))
            self.assertIn("required", payload)
            self.assertIn("risk", payload)
            self.assertEqual(payload["contract"]["source"], str(contract))

    def test_reset_runtime_prevents_results_leaking_between_runs(self):
        flow.reset_runtime()
        flow.case_runner.R.results.append({"status": "PASS"})
        flow.reset_runtime()
        self.assertEqual(flow.case_runner.R.results, [])
        self.assertEqual(flow.case_runner.R.findings, [])


if __name__ == "__main__":
    unittest.main()
