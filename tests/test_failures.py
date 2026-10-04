"""失败与边界：容量缺口排队、并发先到先得、船期/回路失效重排、权限、幂等。"""
import threading

from src.domain import Conflict, PermissionDenied, ValidationError
from tests.support import ELEC, ELEC2, ELEC3, OUTSIDER, PLANNER, LedgerTestCase


class CapacityQueueTest(LedgerTestCase):
    def test_capacity_shortage_queues_with_gap_then_auto_allocates(self):
        self.create_circuit("C-A", 10)
        self.create_reefer("RF-001", kw=7)
        self.create_reefer("RF-002", kw=6)
        r1 = self.connect("RF-001", request_id="Q1")
        self.assertEqual(r1["outcome"], "connected")
        # 10 - 7 = 3，第二箱要 6：排队，缺口 3
        r2 = self.connect("RF-002", request_id="Q2")
        self.assertEqual(r2["outcome"], "queued")
        self.assertEqual(r2["gap_kw"], 3.0)
        queued = self.active("RF-002")
        self.assertEqual(queued["state"], "queued")
        self.assertEqual(queued["basis"]["kind"], "queued_gap")

        # 新增一个容量足够的回路：排队箱立即自动补电（先到先得）
        self.service.register_circuit(PLANNER, {"circuit_code": "C-B", "capacity_kw": 20})
        active = self.active("RF-002")
        self.assertEqual(active["state"], "connected")
        self.assertEqual(active["circuit_code"], "C-B")
        # 依据可追到排队被补电的来源
        self.assertEqual(active["basis"]["kind"], "requeue_connect")
        self.assertEqual(active["parent_id"], queued["id"])

    def test_trip_drops_power_and_recovery_from_latest_complete_batch(self):
        self.create_circuit("C-A", 30)
        self.create_voyage("V-101")
        self.create_reefer("RF-001", kw=6, voyage_no="V-101")
        self.create_reefer("RF-002", kw=8, voyage_no="V-101")
        self.service.create_batch(ELEC, {"batch_no": "B-1"})
        a1 = self.connect("RF-001", request_id="T1", batch_no="B-1")["assignment"]
        a2 = self.connect("RF-002", request_id="T2", batch_no="B-1")["assignment"]
        self.service.complete_batch(ELEC, "B-1")

        # 只放行 RF-001 装船
        self.service.gate_release(PLANNER, {"voyage_no": "V-101", "reefer_codes": ["RF-001"]})

        # C-A 跳闸：RF-002 失电入队；RF-001 已装船，安排保留不动
        trip = self.service.trip_circuit(ELEC2, {"circuit_code": "C-A", "note": "开关跳闸"})
        self.assertEqual(len(trip["dropped"]), 1)
        self.assertEqual(trip["dropped"][0]["reefer_code"], "RF-002")
        # 失效安排保留为依据
        old = self.service.repository.readonly().get_assignment(a2["id"])
        self.assertEqual(old["state"], "invalid")
        self.assertIn("circuit_trip", old["invalid_reason"])
        # RF-001 的装船安排仍是 loaded，未被动
        loaded = self.service.repository.readonly().get_assignment(a1["id"])
        self.assertEqual(loaded["state"], "loaded")

        # 同容量恢复正常后从最近完整批次恢复
        result = self.service.recover_trip(ELEC2, trip["trip_id"], {})
        self.assertEqual(result["batch"]["batch_no"], "B-1")
        # RF-001 已装船不重接；RF-002 恢复供电
        restored_codes = {x["reefer_code"] for x in result["restored"]}
        self.assertEqual(restored_codes, {"RF-002"})
        active2 = self.active("RF-002")
        self.assertEqual(active2["state"], "connected")
        self.assertEqual(active2["basis"]["kind"], "recovery")
        self.assertEqual(active2["basis"]["recovered_from_batch"], "B-1")
        # 跳闸重排生成的排队安排被恢复安排取代，血缘可回溯
        self.assertIsNotNone(active2["parent_id"])
        parent = self.service.repository.readonly().get_assignment(active2["parent_id"])
        self.assertEqual(parent["state"], "superseded")
        self.assertEqual(parent["parent_id"], a2["id"])  # 再上一级是跳闸失效的原接电安排

    def test_recovery_pending_supply_when_no_capacity(self):
        # 构造“恢复即待补”：跳闸回路恢复后容量被批次内其他箱占满，最后一箱缺回路
        self.create_circuit("C-A", 30)
        self.create_reefer("RF-A", kw=18)
        self.create_reefer("RF-B", kw=18)
        self.service.create_batch(ELEC, {"batch_no": "BB"})
        # 批次完整时两箱曾分别在 C-A(30) 与 C-B(30)；模拟 C-B 已拆除
        self.service.register_circuit(PLANNER, {"circuit_code": "C-B", "capacity_kw": 30})
        self.connect("RF-A", request_id="BA1", batch_no="BB")
        self.connect("RF-B", request_id="BA2", batch_no="BB", preferred="C-B")
        self.service.complete_batch(ELEC, "BB")
        self.service.set_circuit_state(PLANNER, "C-B", {"state": "maintenance", "note": "拆除"})
        trip = self.service.trip_circuit(ELEC, {"circuit_code": "C-A"})
        # 恢复时只有 C-A(30)：先到的批次箱占 18，后到的另一箱只剩 12 < 18 → 待补
        result = self.service.recover_trip(ELEC, trip["trip_id"], {})
        restored_codes = {x["reefer_code"] for x in result["restored"]}
        pending_codes = {x["reefer_code"] for x in result["pending_supply"]}
        self.assertEqual(len(restored_codes), 1)
        self.assertEqual(len(pending_codes), 1)
        pending_code = next(iter(pending_codes))
        pending_assn = self.active(pending_code)
        self.assertEqual(pending_assn["state"], "pending_supply")
        self.assertEqual(pending_assn["basis"]["kind"], "recovery_pending")
        self.assertGreater(pending_assn["gap_kw"], 0)
        # 审计能追到跳闸与批次来源
        events = self.service.timeline(ELEC, "assignment", pending_assn["id"])
        self.assertEqual(events[0]["action"], "pending_supply")
        src = {(s["entity_type"], s.get("action")) for s in events[0]["sources"]}
        self.assertIn(("trip", "tripped"), src)
        self.assertIn(("batch", "complete"), src)
        # 新增容量后待补箱自动补电
        self.service.register_circuit(PLANNER, {"circuit_code": "C-C", "capacity_kw": 40})
        self.assertEqual(self.active(pending_code)["state"], "connected")


class InvalidationTest(LedgerTestCase):
    def test_reschedule_invalidates_unloaded_only(self):
        self.create_circuit("C-A", 30)
        self.create_voyage("V-101")
        self.create_reefer("RF-U", kw=5, voyage_no="V-101")
        self.create_reefer("RF-L", kw=5, voyage_no="V-101")
        self.connect("RF-U", request_id="U1")
        self.connect("RF-L", request_id="L1")
        self.service.gate_release(PLANNER, {"voyage_no": "V-101", "reefer_codes": ["RF-L"]})

        out = self.service.reschedule_voyage(
            PLANNER, "V-101", {"etd": "2026-10-09T06:00:00+08:00"})
        codes = {x["reefer_code"] for x in out["invalidated"]}
        self.assertEqual(codes, {"RF-U"})
        # 未装船：原安排失效，生成重排安排并重新接电
        active_u = self.active("RF-U")
        self.assertEqual(active_u["state"], "connected")
        self.assertEqual(active_u["basis"]["kind"], "requeue_connect")
        # 已装船：安排原样保留
        tx = self.service.repository.readonly()
        try:
            reefer_l = tx.get_reefer_by_code("RF-L")
            assns = [a for a in tx.list_assignments() if a["reefer_id"] == reefer_l["id"]]
        finally:
            tx.c.close()
        self.assertTrue(any(a["state"] == "loaded" for a in assns))
        self.assertFalse(any(a["state"] == "invalid" for a in assns))

    def test_circuit_maintenance_invalidates_and_normal_reallocates(self):
        self.create_circuit("C-A", 10)
        self.create_circuit("C-B", 10)
        self.create_reefer("RF-1", kw=6)
        self.create_reefer("RF-2", kw=6)
        c1 = self.connect("RF-1", request_id="M1")
        c2 = self.connect("RF-2", request_id="M2")
        # 两箱各占一条 10kW 回路（紧凑装箱）
        self.assertEqual({c1["assignment"]["circuit_code"], c2["assignment"]["circuit_code"]},
                         {"C-A", "C-B"})
        target = c1["assignment"]["circuit_code"]
        victim = "RF-1" if target == "C-A" else "RF-2"
        survivor = "RF-2" if victim == "RF-1" else "RF-1"
        out = self.service.set_circuit_state(
            PLANNER, target, {"state": "maintenance", "note": "定检"})
        self.assertEqual([x["reefer_code"] for x in out["invalidated"]], [victim])
        self.assertEqual(self.active(victim)["state"], "queued")
        self.assertEqual(self.active(survivor)["state"], "connected")
        # 回路恢复：FIFO，受害箱被补回（另一回路已被占，但原回路容量恢复）
        self.service.set_circuit_state(PLANNER, target, {"state": "normal"})
        self.assertEqual(self.active(victim)["state"], "connected")

    def test_tripped_circuit_cannot_be_set_normal_directly(self):
        self.create_circuit("C-A", 10)
        self.service.trip_circuit(ELEC, {"circuit_code": "C-A"})
        with self.assertRaises(ValidationError):
            self.service.set_circuit_state(PLANNER, "C-A", {"state": "normal"})


class ConcurrencyTest(LedgerTestCase):
    def test_simultaneous_connects_first_wins_later_is_candidate(self):
        self.create_circuit("C-A", 30)
        self.create_reefer("RF-001", kw=6)
        barrier = threading.Barrier(2)
        results = []

        def submit(actor, rid):
            barrier.wait()
            try:
                results.append(("ok", self.service.request_connect(
                    actor, {"reefer_code": "RF-001", "request_id": rid, "actual_temp_c": -18.0})))
            except Conflict as exc:
                results.append(("conflict", exc))
            except Exception as exc:  # noqa: BLE001
                results.append(("error", exc))

        t1 = threading.Thread(target=submit, args=(ELEC, "RACE-1"))
        t2 = threading.Thread(target=submit, args=(ELEC2, "RACE-2"))
        t1.start(); t2.start(); t1.join(); t2.join()

        outcomes = sorted(r[0] for r in results)
        self.assertEqual(outcomes, ["conflict", "ok"])
        winner = next(r[1] for r in results if r[0] == "ok")
        loser = next(r[1] for r in results if r[0] == "conflict")
        self.assertEqual(winner["outcome"], "connected")
        self.assertEqual(loser.details["outcome"], "conflict_candidate")
        self.assertEqual(loser.details["winner"]["assignment_id"], winner["assignment"]["id"])

        # 冷藏箱仍然只有一条活跃安排
        active = self.active("RF-001")
        self.assertEqual(active["id"], winner["assignment"]["id"])
        # 后来者作为冲突候选留在申请记录里
        reqs = self.service.list_candidates(ELEC3)
        candidate = [q for q in reqs if q["outcome"] == "conflict_candidate"]
        self.assertEqual(len(candidate), 1)
        self.assertEqual(candidate[0]["winner_assignment_id"], active["id"])

    def test_idempotent_replay_returns_same_outcome(self):
        self.create_circuit("C-A", 10)
        self.create_reefer("RF-001", kw=6)
        first = self.connect("RF-001", actor=ELEC, request_id="DUP-1")
        replay = self.connect("RF-001", actor=ELEC2, request_id="DUP-1")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["assignment"]["id"], first["assignment"]["id"])


class PermissionTest(LedgerTestCase):
    def test_roles_and_duplicates(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_reefer(OUTSIDER, {"reefer_code": "X", "required_kw": 1, "set_temp_c": -1})
        with self.assertRaises(PermissionDenied):
            self.service.request_connect(PLANNER,
                {"reefer_code": "X", "request_id": "P1", "actual_temp_c": -18})
        self.create_circuit("C-A", 10)
        self.create_reefer("RF-001", kw=6)
        with self.assertRaises(Exception):
            self.create_reefer("RF-001", kw=6)

    def test_batch_cannot_complete_with_unconnected_member(self):
        self.create_circuit("C-A", 5)
        self.create_reefer("RF-001", kw=9)
        self.service.create_batch(ELEC, {"batch_no": "B-OPEN"})
        self.connect("RF-001", request_id="GAP1", batch_no="B-OPEN")
        with self.assertRaises(ValidationError):
            self.service.complete_batch(ELEC, "B-OPEN")

    def test_gate_release_requires_connected(self):
        self.create_circuit("C-A", 5)
        self.create_voyage("V-9")
        self.create_reefer("RF-Q", kw=9, voyage_no="V-9")
        self.connect("RF-Q", request_id="GQ1")
        with self.assertRaises(ValidationError):
            self.service.gate_release(PLANNER, {"voyage_no": "V-9", "reefer_codes": ["RF-Q"]})


if __name__ == "__main__":
    unittest.main()
