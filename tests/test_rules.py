"""纯规则：温控容差、容量核对、选回路、缺口。"""
import unittest

from src.domain import ValidationError
from src.rules import DomainRules


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def test_temp_check_pass_and_fail(self):
        ok = self.rules.temp_check(-18.0, -16.0)
        self.assertTrue(ok["temp_ok"])
        bad = self.rules.temp_check(-18.0, -10.0)
        self.assertFalse(bad["temp_ok"])
        self.assertEqual(bad["temp_delta_c"], 8.0)

    def test_check_before_connect_rejects_temp_mismatch(self):
        reefer = {"set_temp_c": -18.0}
        self.assertTrue(self.rules.check_before_connect(reefer, {"actual_temp_c": -17.0})["temp_ok"])
        with self.assertRaises(ValidationError):
            self.rules.check_before_connect(reefer, {"actual_temp_c": 0.0})

    def test_pick_circuit_and_gap(self):
        circuits = [
            {"circuit_code": "A", "state": "normal", "capacity_kw": 20, "remaining_kw": 4},
            {"circuit_code": "B", "state": "normal", "capacity_kw": 20, "remaining_kw": 12},
            {"circuit_code": "X", "state": "tripped", "capacity_kw": 40, "remaining_kw": 0},
        ]
        picked = self.rules.pick_circuit(circuits, 5)
        self.assertEqual(picked["circuit_code"], "B")
        self.assertIsNone(self.rules.pick_circuit(circuits, 20))
        self.assertEqual(self.rules.capacity_gap(circuits, 20), 8.0)

    def test_non_normal_circuit_has_no_capacity(self):
        self.assertEqual(
            self.rules.circuit_available_kw({"state": "tripped", "capacity_kw": 30}, 0.0), 0.0)

    def test_voyage_etd_must_be_iso(self):
        with self.assertRaises(ValidationError):
            self.rules.validate_voyage({"voyage_no": "V1", "vessel": "X", "etd": "明天"})
        data = self.rules.validate_voyage(
            {"voyage_no": "V1", "vessel": "X", "etd": "2026-10-08T18:00:00+08:00"})
        self.assertEqual(data["voyage_no"], "V1")


if __name__ == "__main__":
    unittest.main()
