"""完整流程：建档→开批次→接电→封存完整批次→装船放行→审计溯源。"""
from tests.support import ELEC, PLANNER, LedgerTestCase


class WorkflowTest(LedgerTestCase):
    def _seed(self):
        self.create_circuit("C-A", 15)
        self.create_circuit("C-B", 8)
        self.create_voyage("V-101")
        self.create_reefer("RF-001", 6, -18, "V-101")
        self.create_reefer("RF-002", 5, -18, "V-101")
        self.service.create_batch(ELEC, {"batch_no": "B-1", "note": "夜班接电"})

    def test_connect_complete_batch_load_and_audit(self):
        self._seed()
        r1 = self.connect("RF-001", request_id="REQ-1", batch_no="B-1")
        r2 = self.connect("RF-002", request_id="REQ-2", batch_no="B-1")
        self.assertEqual(r1["outcome"], "connected")
        self.assertEqual(r2["outcome"], "connected")
        # 紧凑装箱：RF-001(6kW) 装进 8kW 回路 C-B（剩2kW），RF-002(5kW) 进 C-A
        self.assertEqual(r1["assignment"]["circuit_code"], "C-B")
        self.assertEqual(r2["assignment"]["circuit_code"], "C-A")

        # 批次内全部接电后可封存为完整批次
        batch = self.service.complete_batch(ELEC, "B-1")
        self.assertEqual(batch["state"], "complete")

        # 装船放行：必须已接电；装船后安排定格 loaded，回路容量释放
        release = self.service.gate_release(
            PLANNER, {"voyage_no": "V-101", "reefer_codes": ["RF-001", "RF-002"]})
        self.assertEqual(len(release["released"]), 2)
        assn1 = self.active("RF-001")
        self.assertIsNone(assn1)  # loaded 不再是活跃安排

        # 审计可追到接电与装船依据
        rid = self.service.repository
        reefer1 = rid.readonly().get_reefer_by_code("RF-001")
        events = self.service.timeline(ELEC, "reefer", reefer1["id"])
        actions = [e["action"] for e in events]
        self.assertIn("registered", actions)

        assn_events = self.service.timeline(ELEC, "assignment", r1["assignment"]["id"])
        # 时间线按时间倒序返回：装船在接电之后，故顺序为 loaded -> connected
        assn_actions = [e["action"] for e in assn_events]
        self.assertEqual(assn_actions, ["loaded", "connected"])
        loaded_event = assn_events[0]
        self.assertIn("frozen_basis", loaded_event["details"])  # 已装船保留原依据
        # 来源链指回接电安排与船期放行
        kinds = {(s["entity_type"], s.get("action")) for s in loaded_event["sources"]}
        self.assertIn(("assignment", "connected"), kinds)
        self.assertIn(("voyage", "gate_release"), kinds)

    def test_temp_check_before_connect_rejects(self):
        self._seed()
        from src.domain import ValidationError
        with self.assertRaises(ValidationError):
            self.connect("RF-001", request_id="REQ-BAD", actual_temp=5.0)
        # 被拒绝后没有活跃安排
        self.assertIsNone(self.active("RF-001"))
        events = self.service.timeline(ELEC)
        self.assertTrue(any(e["action"] == "connect_rejected" for e in events))
