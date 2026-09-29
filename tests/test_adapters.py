import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.base import Response, TransportError
from adapters.rest import RestAdapter


class TestRestAdapter(unittest.TestCase):
    def test_builds_request_and_classifies_response_without_network(self):
        seen = []
        def transport(request):
            seen.append(request)
            return Response(200, {"result": True}, {"X-Trace": "t1"}, "r1", "t1")
        adapter = RestAdapter(transport)
        request = adapter.build_request({"params": {"title": "x"}}, {"base_url": "https://example.test"})
        response = adapter.send(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen[0].body["title"], "x")
        self.assertEqual(adapter.classify_response(response)["status"], "ok")

    def test_transport_error_is_normalized(self):
        def transport(_):
            raise OSError("offline")
        with self.assertRaises(TransportError):
            RestAdapter(transport).send(object())


if __name__ == "__main__":
    unittest.main()
