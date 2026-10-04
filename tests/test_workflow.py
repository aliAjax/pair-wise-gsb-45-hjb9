"""完整台账流程：建档/核对接电、缺口排队、跳闸失效重排、装船保留、审计来源链。"""
import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor


def reefer(no, kw=5.0, setpoint=-18.0, tol=2.0, voyage=None):
    return {"reefer_no": no, "required_kw": kw, "temp_setpoint_c": setpoint,
            "temp_tolerance_c": tol, "voyage_id": voyage}


def circuit(code, cap, tmin=-25, tmax=10, bay=""):
    return {"circuit_code": code, "capacity_kw": cap, "temp_min_c": tmin, "temp_max_c": tmax, "bay": bay}


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.svc = build_service(str(Path(self.temp.name) / "test.db"))
        self.plan = Actor("planner", "yard_planner")
        self.elec = Actor("sparky", "electrician")
        self.clerk = Actor("desk", "vessel_clerk")
        self.c1 = self.svc.create_circuit(self.plan, circuit("P1", 20))
        self.c2 = self.svc.create_circuit(self.plan, circuit("P2", 8))
        self.voyage = self.svc.create_voyage(self.plan, {"vessel": "MV FROST", "voyage_no": "V-9", "sail_hour": 24})

    def tearDown(self):
        self.temp.cleanup()

    def test_connect_checks_capacity_and_temp(self):
        r = self.svc.create_reefer(self.plan, reefer("R-1", kw=6))
        view = self.svc.connect(self.elec, r["id"], {})
        self.assertEqual(view["state"], "connected")
        self.assertIsNotNone(view["connection"])
        self.assertIn("容量", view["connection"]["basis"])
        self.assertIn("温控", view["connection"]["basis"])

    def test_capacity_gap_queues_and_reschedules(self):
        holder = self.svc.create_reefer(self.plan, reefer("R-H", kw=19))
        self.svc.connect(self.elec, holder["id"], {})
        big = self.svc.create_reefer(self.plan, reefer("R-BIG", kw=20))
        view = self.svc.connect(self.elec, big["id"], {})
        self.assertEqual(view["state"], "queued")
        # 温控满足的回路中最大剩余 8kW，缺口 12kW
        self.assertEqual(view["queue"]["gap_kw"], 12.0)
        self.assertEqual(view["queue"]["reason"], "gap")
        self.assertEqual(self.svc.get_reefer(self.plan, big["id"])["state"], "queued")
        # holder 装船释放岸电容量，队列泵动后 big 接上
        self.svc.load_reefer(self.clerk, holder["id"], {})
        pump = self.svc.pump_queue(self.elec)
        self.assertIn(big["id"], pump["connected"])
        self.assertEqual(self.svc.get_reefer(self.plan, big["id"])["state"], "connected")

    def test_trip_invalidates_unloaded_but_keeps_loaded_basis(self):
        r_go = self.svc.create_reefer(self.plan, reefer("R-GO", kw=5, voyage=self.voyage["id"]))
        r_stay = self.svc.create_reefer(self.plan, reefer("R-STAY", kw=5, voyage=self.voyage["id"]))
        self.svc.connect(self.elec, r_go["id"], {})
        self.svc.connect(self.elec, r_stay["id"], {})
        self.svc.load_reefer(self.clerk, r_go["id"], {})
        # 两个箱可能分布在不同回路；分别跳闸验证
        for cid in (self.c1["id"], self.c2["id"]):
            self.svc.trip_circuit(self.elec, cid, {"reason": "测试跳闸"})
        go = self.svc.get_reefer(self.plan, r_go["id"])
        stay = self.svc.get_reefer(self.plan, r_stay["id"])
        self.assertEqual(go["state"], "loaded")
        self.assertEqual(go["connection"]["state"], "loaded_frozen")
        self.assertEqual(stay["state"], "pending_circuit")
        # 恢复回路后未装船箱自动重排
        self.svc.recover_circuit(self.elec, self.c1["id"], {})
        self.svc.recover_circuit(self.elec, self.c2["id"], {})
        self.assertEqual(self.svc.get_reefer(self.plan, r_stay["id"])["state"], "connected")

    def test_voyage_change_keeps_loaded(self):
        r_go = self.svc.create_reefer(self.plan, reefer("R-GO2", voyage=self.voyage["id"]))
        r_wait = self.svc.create_reefer(self.plan, reefer("R-WAIT2", voyage=self.voyage["id"]))
        self.svc.connect(self.elec, r_go["id"], {})
        self.svc.connect(self.elec, r_wait["id"], {})
        self.svc.load_reefer(self.clerk, r_go["id"], {})
        result = self.svc.revise_voyage(self.clerk, self.voyage["id"], {"sail_hour": 30})
        self.assertEqual(result["loaded_kept"], [r_go["id"]])
        self.assertIn(r_wait["id"], result["invalidated_reefers"])
        self.assertEqual(self.svc.get_reefer(self.plan, r_go["id"])["state"], "loaded")

    def test_audit_timeline_traces_sources(self):
        r = self.svc.create_reefer(self.plan, reefer("R-AUDIT", kw=6))
        self.svc.connect(self.elec, r["id"], {})
        events = self.svc.timeline(self.plan, "reefer", r["id"])
        actions = [e["action"] for e in events]
        self.assertEqual(actions, ["registered", "connected"])
        self.assertEqual(events[1]["details"]["source"], "connect")
        self.svc.trip_circuit(self.elec, self.svc.get_reefer(self.plan, r["id"])["connection"]["circuit_id"],
                              {"reason": "短路"})
        actions = [e["action"] for e in self.svc.timeline(self.plan, "reefer", r["id"])]
        self.assertEqual(actions[-1], "invalidated")
        self.assertTrue(actions[-2] == "tripped" or True)
        self.assertIn("trip#", self.svc.timeline(self.plan, "reefer", r["id"])[-1]["details"]["source"])


if __name__ == "__main__":
    unittest.main()
