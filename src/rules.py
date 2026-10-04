"""冷藏箱台账领域规则：校验、温控/容量评估、回路选择、批次恢复。"""
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import Conflict, ValidationError, choice, number, optional_text, text


# 实体状态
REEFER_STATES = {"waiting", "connected", "queued", "pending_circuit", "pending_recovery", "loaded", "cancelled"}
CIRCUIT_STATES = {"active", "tripped", "maintenance"}
QUEUE_REASONS = {"gap", "trip", "voyage_change", "circuit_change", "recovery", "batch_rollback", "preferred"}
CONNECTION_STATES = {"connected", "voided", "superseded", "loaded_frozen"}

# 温控边界（℃）：超出即视为不具备冷藏接电条件
TEMP_MIN, TEMP_MAX = -35.0, 30.0

CREATE_ROLES = {"yard_planner"}
ROLE_MATRIX = {
    "reefer_create": {"yard_planner"},
    "circuit_update": {"electrician", "yard_planner"},
    "voyage_update": {"vessel_clerk", "yard_planner"},
    "connect": {"electrician", "yard_planner"},
    "load": {"vessel_clerk", "yard_planner"},
    "batch": {"electrician", "yard_planner"},
}


class DomainRules:
    def known_role(self, role: str) -> bool:
        roles = set(CREATE_ROLES)
        for allowed in ROLE_MATRIX.values():
            roles.update(allowed)
        return role == "admin" or role in roles

    def can(self, role: str, permission: str) -> bool:
        return role == "admin" or role in ROLE_MATRIX.get(permission, set())

    # ---------- 输入校验 ----------
    def validate_reefer(self, p: Dict[str, Any]) -> Dict[str, Any]:
        voyage_raw = p.get("voyage_id")
        if voyage_raw is None:
            voyage_value = None
        elif isinstance(voyage_raw, bool):
            raise ValidationError("voyage_id必须为整数ID")
        elif isinstance(voyage_raw, int):
            voyage_value = int(voyage_raw)
        elif isinstance(voyage_raw, str) and voyage_raw.strip().isdigit():
            voyage_value = int(voyage_raw.strip())
        else:
            raise ValidationError("voyage_id必须为整数ID")
        data = {
            "reefer_no": text(p, "reefer_no"),
            "required_kw": round(number(p, "required_kw", 0.1, 200), 2),
            "temp_setpoint_c": round(number(p, "temp_setpoint_c", TEMP_MIN, TEMP_MAX), 1),
            "temp_tolerance_c": round(number(p, "temp_tolerance_c", 0.1, 10), 1),
            "cargo": optional_text(p, "cargo"),
            "voyage_id": voyage_value,
        }
        return data

    def validate_circuit(self, p: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "circuit_code": text(p, "circuit_code"),
            "capacity_kw": round(number(p, "capacity_kw", 0.1, 1000), 2),
            "temp_min_c": round(number(p, "temp_min_c", TEMP_MIN, TEMP_MAX), 1),
            "temp_max_c": round(number(p, "temp_max_c", TEMP_MIN, TEMP_MAX), 1),
            "bay": optional_text(p, "bay"),
        }

    def validate_voyage(self, p: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "vessel": text(p, "vessel"),
            "voyage_no": text(p, "voyage_no"),
            "sail_hour": number(p, "sail_hour", 0, 240),
        }

    # ---------- 温控与容量 ----------
    def temp_ok(self, reefer: Dict[str, Any], circuit: Dict[str, Any]) -> bool:
        lo = reefer["temp_setpoint_c"] - reefer["temp_tolerance_c"]
        hi = reefer["temp_setpoint_c"] + reefer["temp_tolerance_c"]
        return circuit["temp_min_c"] <= lo and circuit["temp_max_c"] >= hi

    def circuit_view(self, circuit: Dict[str, Any], connections: List[Dict[str, Any]]) -> Dict[str, Any]:
        used = round(sum(c["required_kw"] for c in connections if c["circuit_id"] == circuit["id"]), 2)
        return {
            "id": circuit["id"],
            "circuit_code": circuit["circuit_code"],
            "state": circuit["state"],
            "capacity_kw": circuit["capacity_kw"],
            "used_kw": used,
            "free_kw": round(circuit["capacity_kw"] - used, 2),
            "temp_min_c": circuit["temp_min_c"],
            "temp_max_c": circuit["temp_max_c"],
            "bay": circuit.get("bay", ""),
            "version": circuit["version"],
        }

    def circuit_views(self, circuits: Iterable[Dict[str, Any]], connections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [self.circuit_view(c, connections) for c in circuits]

    def candidates_for(self, reefer: Dict[str, Any], views: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
        """返回（可用候选回路视图列表， 不满足原因列表）。"""
        eligible: List[Dict[str, Any]] = []
        blockers: List[str] = []
        for view in views:
            if view["state"] != "active":
                blockers.append("%s:回路%s" % (view["circuit_code"], {"tripped": "跳闸", "maintenance": "检修"}.get(view["state"], view["state"])))
                continue
            if not self.temp_ok(reefer, view):
                blockers.append("%s:温控不覆盖(%s±%s℃)" % (view["circuit_code"], reefer["temp_setpoint_c"], reefer["temp_tolerance_c"]))
                continue
            if view["free_kw"] + 1e-9 < reefer["required_kw"]:
                blockers.append("%s:容量缺口%.2fkW" % (view["circuit_code"], round(reefer["required_kw"] - view["free_kw"], 2)))
                continue
            eligible.append(view)
        # 最佳适配：满足温控的前提下，剩余容量最小者优先
        eligible.sort(key=lambda v: (v["free_kw"], v["id"]))
        return eligible, blockers

    def best_circuit(self, reefer: Dict[str, Any], views: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        eligible, _ = self.candidates_for(reefer, views)
        return eligible[0] if eligible else None

    def largest_gap(self, reefer: Dict[str, Any], views: List[Dict[str, Any]]) -> float:
        """容量不足排队时记录的缺口：温控满足但容量不够的最优回路缺口；没有温控匹配的回路则记全部需求。"""
        temp_ok_views = [v for v in views if v["state"] == "active" and self.temp_ok(reefer, v)]
        if not temp_ok_views:
            return round(reefer["required_kw"], 2)
        richest = max(temp_ok_views, key=lambda v: v["free_kw"])
        return round(max(reefer["required_kw"] - richest["free_kw"], 0.0), 2)

    # ---------- 状态守卫 ----------
    REEFER_TRANSITIONS = {
        "waiting": {"connected", "queued", "pending_circuit", "cancelled"},
        "connected": {"queued", "pending_circuit", "pending_recovery", "loaded", "cancelled"},
        "queued": {"connected", "pending_circuit", "pending_recovery", "cancelled"},
        "pending_circuit": {"connected", "queued", "pending_recovery", "cancelled"},
        "pending_recovery": {"connected", "queued", "cancelled"},
        "loaded": set(),
        "cancelled": set(),
    }

    def require_reefer_state(self, reefer: Dict[str, Any], allowed: Iterable[str], hint: str) -> None:
        if reefer["state"] not in set(allowed):
            raise Conflict("冷藏箱当前状态%s，%s" % (reefer["state"], hint))

    def require_circuit_state(self, circuit: Dict[str, Any], allowed: Iterable[str], hint: str) -> None:
        if circuit["state"] not in set(allowed):
            raise Conflict("回路当前状态%s，%s" % (circuit["state"], hint))
