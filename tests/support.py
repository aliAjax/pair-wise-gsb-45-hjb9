"""测试公共构造。"""
import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor
from src.rules import ROLE_ELECTRICIAN, ROLE_PLANNER


PLANNER = Actor("planner-1", ROLE_PLANNER)
ELEC = Actor("elec-1", ROLE_ELECTRICIAN)
ELEC2 = Actor("elec-2", ROLE_ELECTRICIAN)
ELEC3 = Actor("elec-3", ROLE_ELECTRICIAN)
OUTSIDER = Actor("outsider", "outsider")


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    # ---------- 场景构造辅助 ----------
    def create_circuit(self, code="C-A", capacity=15.0, state="normal"):
        return self.service.register_circuit(
            PLANNER, {"circuit_code": code, "capacity_kw": capacity, "location": "yard", "state": state})

    def create_voyage(self, voyage_no="V-101", etd="2026-10-08T18:00:00+08:00"):
        return self.service.create_voyage(
            PLANNER, {"voyage_no": voyage_no, "vessel": "MV DONGHAI", "etd": etd})

    def create_reefer(self, code="RF-001", kw=6.0, temp=-18.0, voyage_no=""):
        payload = {"reefer_code": code, "required_kw": kw, "set_temp_c": temp}
        if voyage_no:
            payload["voyage_no"] = voyage_no
        return self.service.register_reefer(PLANNER, payload)

    def connect(self, code, actor=None, request_id=None, actual_temp=-18.0,
                preferred="", batch_no=""):
        actor = actor or ELEC
        body = {"reefer_code": code, "request_id": request_id or ("REQ-" + code),
                "actual_temp_c": actual_temp}
        if preferred:
            body["preferred_circuit"] = preferred
        if batch_no:
            body["batch_no"] = batch_no
        return self.service.request_connect(actor, body)

    def active(self, code):
        tx = self.service.repository.readonly()
        try:
            reefer = tx.get_reefer_by_code(code)
            return tx.active_assignment(reefer["id"])
        finally:
            tx.c.close()
