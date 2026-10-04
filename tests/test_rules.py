"""规则计算：温控覆盖、容量核对、回路选择与缺口。"""
import unittest

from src.rules import DomainRules


def circuit(id, code, cap, tmin=-30, tmax=25, state="active"):
    return {"id": id, "circuit_code": code, "capacity_kw": cap, "temp_min_c": tmin,
            "temp_max_c": tmax, "state": state, "bay": "", "version": 1}


def reefer(kw=5.0, setpoint=-18.0, tol=2.0):
    return {"required_kw": kw, "temp_setpoint_c": setpoint, "temp_tolerance_c": tol}


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def test_temp_coverage(self):
        c = circuit(1, "C1", 20, -25, 10)
        self.assertTrue(self.rules.temp_ok(reefer(setpoint=-18, tol=2), c))
        # 设定 -18±2 需要下限 <= -20
        self.assertFalse(self.rules.temp_ok(reefer(setpoint=-18, tol=2), circuit(2, "C2", 20, -19, 10)))
        # 深冷箱 -30±2 普通冷藏回路不覆盖
        self.assertFalse(self.rules.temp_ok(reefer(5, -30, 2), c))

    def test_capacity_and_best_fit(self):
        c_big = circuit(1, "BIG", 20)
        c_small = circuit(2, "SML", 8)
        conns = [{"circuit_id": 1, "required_kw": 16}, {"circuit_id": 2, "required_kw": 3}]
        views = self.rules.circuit_views([c_big, c_small], conns)
        free = {v["circuit_code"]: v["free_kw"] for v in views}
        self.assertEqual(free, {"BIG": 4.0, "SML": 5.0})
        # 5kW 两个回路都够，best-fit 选剩余最小的 SML
        chosen = self.rules.best_circuit(reefer(5), views)
        self.assertEqual(chosen["circuit_code"], "SML")
        # 6kW：BIG 剩余4不够，SML 剩余5不够 -> 无候选，缺口=6-max(4,5)=1
        box6 = reefer(kw=6)
        self.assertIsNone(self.rules.best_circuit(box6, views))
        self.assertEqual(self.rules.largest_gap(box6, views), 1.0)
        # 释放 BIG：6kW 只能选 BIG
        views_free_big = self.rules.circuit_views([c_big, c_small], [{"circuit_id": 2, "required_kw": 3}])
        chosen = self.rules.best_circuit(reefer(kw=6), views_free_big)
        self.assertEqual(chosen["circuit_code"], "BIG")

    def test_tripped_circuit_excluded(self):
        views = self.rules.circuit_views([circuit(1, "UP", 20, state="tripped")], [])
        eligible, blockers = self.rules.candidates_for(reefer(5), views)
        self.assertEqual(eligible, [])
        self.assertIn("跳闸", blockers[0])

    def test_gap_when_no_temp_match(self):
        views = self.rules.circuit_views([circuit(1, "WARM", 20, 0, 20)], [])
        box = reefer(5, -18, 2)
        self.assertIsNone(self.rules.best_circuit(box, views))
        # 没有任何温控匹配回路，缺口按全部需求记录
        self.assertEqual(self.rules.largest_gap(box, views), 5.0)

    def test_role_matrix(self):
        self.assertTrue(self.rules.can("electrician", "connect"))
        self.assertFalse(self.rules.can("vessel_clerk", "connect"))
        self.assertTrue(self.rules.can("vessel_clerk", "load"))
        self.assertTrue(self.rules.can("admin", "anything"))


if __name__ == "__main__":
    unittest.main()
