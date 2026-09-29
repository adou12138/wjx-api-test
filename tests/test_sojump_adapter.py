import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.base import Response
from adapters.sojump import SojumpAdapter


class TestSojumpAdapter(unittest.TestCase):
    def test_requires_public_parameters(self):
        adapter = SojumpAdapter(lambda request: Response(200, {}))
        with self.assertRaises(ValueError):
            adapter.build_request({"params": {"title": "x"}}, {"base_url": "https://example.test"})

    def test_write_requires_explicit_test_environment(self):
        adapter = SojumpAdapter(lambda request: Response(200, {}))
        params = {"action": "1000107", "encode": "sha1", "appid": 1, "sign": "configured", "title": "x"}
        with self.assertRaises(PermissionError):
            adapter.build_request({"params": params, "write": True}, {"base_url": "https://test.example", "type": "test"})
        request = adapter.build_request({"params": params, "write": True}, {"base_url": "https://test.example", "type": "test", "write_enabled": True})
        self.assertEqual(request.body["action"], "1000107")


if __name__ == "__main__":
    unittest.main()
