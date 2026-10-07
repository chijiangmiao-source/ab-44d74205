"""HTTP 层集成测试：真实端口上的页面、健康、状态与候选接口。"""
import json
import threading
import unittest
import urllib.error
import urllib.request

from app.engine import Table
from app.server import _HTTPServer, make_handler


class ServerFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = _HTTPServer(("127.0.0.1", 0), make_handler(Table()))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def req(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                   data=data, method=method,
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()


class TestHTTP(ServerFixture):
    def test_health(self):
        code, body = self.req("GET", "/health")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["status"], "ok")

    def test_page(self):
        code, body = self.req("GET", "/")
        self.assertEqual(code, 200)
        text = body.decode("utf-8")
        self.assertIn("推导表", text)
        self.assertIn('id="grid"', text)

    def test_404(self):
        code, _ = self.req("GET", "/nope")
        self.assertEqual(code, 404)

    def test_candidate_flow_and_reset(self):
        self.req("POST", "/api/reset")
        code, body = self.req("POST", "/api/candidate", {"cell": "A1", "input": "5"})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["status"], "published")
        code, body = self.req("POST", "/api/candidate", {"cell": "B1", "input": "A1 / 0"})
        self.assertEqual(code, 409)
        self.assertEqual(json.loads(body)["error"]["kind"], "div_zero")
        code, body = self.req("GET", "/api/state")
        st = {c["id"]: c for c in json.loads(body)["cells"]}
        self.assertEqual(st["A1"]["value"], 5)
        self.assertFalse(st["B1"]["hasValue"])

    def test_bad_requests(self):
        code, _ = self.req("POST", "/api/candidate", {"cell": "Z9", "input": "1"})
        self.assertEqual(code, 400)
        code, _ = self.req("POST", "/api/candidate", {"cell": "A1"})
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
