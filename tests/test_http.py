"""HTTP 端到端冒烟：真实起服务，覆盖主要接口与 409 冲突候选响应体。"""
import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path

from app import build_service
from src.http_api import create_server


class HttpSmokeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service = build_service(str(Path(self.temp.name) / "http.db"))
        self.server = create_server("127.0.0.1", 0, service, Path(__file__).resolve().parent.parent / "static")
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def _req(self, method, path, body=None, role="yard_planner", user="u1"):
        headers = {"X-User-Id": user, "X-Role": role}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request("http://127.0.0.1:%s%s" % (self.port, path),
                                         data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_flow_over_http(self):
        request = urllib.request.Request("http://127.0.0.1:%s/health" % self.port)
        with urllib.request.urlopen(request, timeout=10) as resp:
            self.assertEqual(resp.status, 200)

        self.assertEqual(self._req("POST", "/api/circuits",
                                   {"circuit_code": "C-A", "capacity_kw": 10})[0], 201)
        self.assertEqual(self._req("POST", "/api/voyages",
                                   {"voyage_no": "V1", "vessel": "X",
                                    "etd": "2026-10-09T08:00:00+08:00"})[0], 201)
        self.assertEqual(self._req("POST", "/api/reefers",
                                   {"reefer_code": "R1", "required_kw": 6,
                                    "set_temp_c": -18, "voyage_no": "V1"})[0], 201)
        self.assertEqual(self._req("POST", "/api/batches", {"batch_no": "B1"},
                                   role="electrician")[0], 201)

        status, connected = self._req(
            "POST", "/api/connect-requests",
            {"reefer_code": "R1", "request_id": "K1", "actual_temp_c": -18, "batch_no": "B1"},
            role="electrician")
        self.assertEqual(status, 201)
        self.assertEqual(connected["outcome"], "connected")

        # 第二份接电：409，details 带冲突候选信息
        status, err = self._req(
            "POST", "/api/connect-requests",
            {"reefer_code": "R1", "request_id": "K2", "actual_temp_c": -18},
            role="electrician", user="u2")
        self.assertEqual(status, 409)
        self.assertEqual(err["error"], "conflict")
        self.assertEqual(err["details"]["outcome"], "conflict_candidate")

        # 候选记录可查
        status, candidates = self._req("GET", "/api/candidates", role="electrician")
        self.assertEqual(status, 200)
        self.assertTrue(any(q["outcome"] == "conflict_candidate" for q in candidates["items"]))

        # 封存批次、装船放行
        self.assertEqual(self._req("POST", "/api/batches/B1/complete", {},
                                   role="electrician")[0], 200)
        status, released = self._req("POST", "/api/gate-release",
                                     {"voyage_no": "V1", "reefer_codes": ["R1"]})
        self.assertEqual(status, 200)
        self.assertEqual(len(released["released"]), 1)

        # 跳闸与恢复
        self.assertEqual(self._req("POST", "/api/trips", {"circuit_code": "C-A"},
                                   role="electrician")[0], 201)
        status, trips = self._req("GET", "/api/trips?state=open", role="electrician")
        trip_id = trips["items"][0]["id"]
        status, rec = self._req("POST", "/api/trips/%s/recover" % trip_id, {},
                                role="electrician")
        self.assertEqual(status, 200)
        self.assertEqual(rec["batch"]["batch_no"], "B1")

        # 审计与统计
        status, audit = self._req("GET", "/api/audit?limit=20", role="electrician")
        self.assertEqual(status, 200)
        actions = {e["action"] for e in audit["items"]}
        for expected in ("connected", "connect_conflict_candidate", "loaded", "tripped",
                         "recovered", "gate_release"):
            self.assertIn(expected, actions)
        self.assertEqual(self._req("GET", "/api/stats")[0], 200)

    def test_index_page_served(self):
        request = urllib.request.Request("http://127.0.0.1:%s/" % self.port)
        with urllib.request.urlopen(request, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("冷藏箱供电回路台账", resp.read().decode("utf-8"))

    def test_temp_mismatch_rejected(self):
        self._req("POST", "/api/circuits", {"circuit_code": "C-A", "capacity_kw": 10})
        self._req("POST", "/api/reefers",
                  {"reefer_code": "R9", "required_kw": 6, "set_temp_c": -18})
        status, err = self._req(
            "POST", "/api/connect-requests",
            {"reefer_code": "R9", "request_id": "K9", "actual_temp_c": 5},
            role="electrician")
        self.assertEqual(status, 422)
        self.assertIn("温控", err["message"])


if __name__ == "__main__":
    unittest.main()
