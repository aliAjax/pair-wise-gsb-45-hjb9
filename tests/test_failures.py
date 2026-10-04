"""失败场景：权限、重复、并发先到先得/冲突候选、批次失败回滚与待补恢复。"""
import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


def circuit(code, cap, tmin=-25, tmax=10):
    return {"circuit_code": code, "capacity_kw": cap, "temp_min_c": tmin, "temp_max_c": tmax}


def reefer(no, kw=5.0, voyage=None):
    return {"reefer_no": no, "required_kw": kw, "temp_setpoint_c": -18, "temp_tolerance_c": 2,
            "voyage_id": voyage}


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.svc = build_service(str(Path(self.temp.name) / "test.db"))
        self.plan = Actor("planner", "yard_planner")
        self.elec = Actor("sparky", "electrician")
        self.clerk = Actor("desk", "vessel_clerk")
        self.c1 = self.svc.create_circuit(self.plan, circuit("P1", 20))

    def tearDown(self):
        self.temp.cleanup()

    def test_permission_and_duplicate(self):
        with self.assertRaises(PermissionDenied):
            self.svc.create_reefer(Actor("x", "outsider"), reefer("R-1"))
        with self.assertRaises(PermissionDenied):
            # 船务单证员不能登记回路
            self.svc.create_circuit(self.clerk, circuit("PX", 9))
        self.svc.create_reefer(self.plan, reefer("R-DUP"))
        with self.assertRaises(Conflict):
            self.svc.create_reefer(self.plan, reefer("R-DUP"))

    def test_temp_mismatch_rejected(self):
        warm = self.svc.create_circuit(self.plan, circuit("WARM", 30, tmin=0, tmax=20))
        r = self.svc.create_reefer(self.plan, reefer("R-COLD"))
        # 指定温区不覆盖的回路强接 -> 拒绝
        with self.assertRaises(ValidationError):
            self.svc.connect(self.elec, r["id"], {"preferred_circuit_id": warm["id"]})
        # P1 温控覆盖且有容量 -> 正常接电
        view = self.svc.connect(self.elec, r["id"], {})
        self.assertEqual(view["state"], "connected")

    def test_concurrent_connect_first_wins_later_is_candidate(self):
        r = self.svc.create_reefer(self.plan, reefer("R-RACE"))
        outcomes = {}

        def submit(uid):
            try:
                self.svc.connect(Actor(uid, "electrician"), r["id"], {})
                outcomes[uid] = "won"
            except Conflict as exc:
                outcomes[uid] = ("conflict", exc.data.get("candidate_id"))

        threads = [threading.Thread(target=submit, args=(u,)) for u in ("E-A", "E-B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(list(outcomes.values()).count("won"), 1)
        loser = [v for v in outcomes.values() if v != "won"]
        self.assertEqual(len(loser), 1)
        self.assertEqual(loser[0][0], "conflict")
        self.assertIsNotNone(loser[0][1])
        candidates = self.svc.list_conflicts(self.elec)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["state"], "candidate")
        self.assertEqual(self.svc.get_reefer(self.plan, r["id"])["state"], "connected")

    def test_batch_rolls_back_and_recovers_from_last_complete(self):
        c2 = self.svc.create_circuit(self.plan, circuit("P2", 20))
        r1 = self.svc.create_reefer(self.plan, reefer("R-B1", kw=6))
        r2 = self.svc.create_reefer(self.plan, reefer("R-B2", kw=6))
        r3 = self.svc.create_reefer(self.plan, reefer("R-B3", kw=5))
        good = self.svc.batch_connect(self.elec, {"reefer_ids": [r1["id"], r2["id"]],
                                                  "preferred": {str(r1["id"]): self.c1["id"],
                                                                str(r2["id"]): c2["id"]}})
        self.assertEqual(good["state"], "complete")
        original = {s["reefer_id"]: s["circuit_id"] for s in good["snapshot"]}
        self.assertEqual(original, {r1["id"]: self.c1["id"], r2["id"]: c2["id"]})
        # 两个回路先后跳闸：两箱安排均失效
        self.svc.trip_circuit(self.elec, self.c1["id"], {"reason": "故障"})
        self.svc.trip_circuit(self.elec, c2["id"], {"reason": "故障"})
        for rid in (r1["id"], r2["id"]):
            self.assertIn(self.svc.get_reefer(self.plan, rid)["state"],
                          {"pending_circuit", "queued"})
        # 失败批次（含批次外的 r3）：回路仍跳闸 -> 全部待补，失败批次不落接电记录
        result = self.svc.batch_fail(self.elec, {"reefer_ids": [r1["id"], r2["id"], r3["id"]], "reason": "插头坏"})
        recovery = result["recovery"]
        self.assertEqual(recovery["source_batch_id"], good["id"])
        self.assertEqual(sorted(recovery["pending"]), [r1["id"], r2["id"], r3["id"]])
        for rid in (r1["id"], r2["id"], r3["id"]):
            self.assertEqual(self.svc.get_reefer(self.plan, rid)["state"], "pending_recovery")
        # 恢复两个原回路后再执行恢复：r1/r2 按最近完整批次同回路恢复；r3 按当前容量补配
        self.svc.recover_circuit(self.elec, self.c1["id"], {})
        self.svc.recover_circuit(self.elec, c2["id"], {})
        again = self.svc.recover_from_batch(self.elec, {"reefer_ids": [r1["id"], r2["id"], r3["id"]]})
        self.assertEqual(len(again["restored"]), 3)
        by_id = {x["reefer_id"]: x for x in again["restored"]}
        for rid in (r1["id"], r2["id"]):
            self.assertTrue(by_id[rid]["same_circuit"])
            self.assertEqual(by_id[rid]["circuit_id"], original[rid])
        self.assertFalse(by_id[r3["id"]]["same_circuit"])

    def test_recovery_without_any_complete_batch(self):
        r = self.svc.create_reefer(self.plan, reefer("R-FIRST"))
        result = self.svc.batch_fail(self.elec, {"reefer_ids": [r["id"]], "reason": "现场断电"})
        self.assertIsNone(result["recovery"]["source_batch_id"])
        self.assertEqual(result["recovery"]["pending"], [r["id"]])
        self.assertEqual(self.svc.get_reefer(self.plan, r["id"])["state"], "pending_recovery")

    def test_loaded_reefer_cannot_be_reconnected(self):
        r = self.svc.create_reefer(self.plan, reefer("R-LOADED"))
        self.svc.connect(self.elec, r["id"], {})
        self.svc.load_reefer(self.clerk, r["id"], {})
        with self.assertRaises(Conflict):
            self.svc.connect(self.elec, r["id"], {})

    def test_cannot_load_unconnected_reefer(self):
        r = self.svc.create_reefer(self.plan, reefer("R-NO-POWER", kw=99))
        with self.assertRaises(Conflict):
            self.svc.load_reefer(self.clerk, r["id"], {})


if __name__ == "__main__":
    unittest.main()
