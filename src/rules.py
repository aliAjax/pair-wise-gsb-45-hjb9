"""冷藏箱供电台账领域规则：输入校验、容量与温控核对、排队与批次判定。

本模块只做纯计算与校验，不接触数据库。
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from .domain import ValidationError, number, optional_text, text


# 冷藏箱状态
REEFER_REGISTERED = "registered"
REEFER_LOADED = "loaded"

# 供电回路状态
CIRCUIT_NORMAL = "normal"
CIRCUIT_MAINTENANCE = "maintenance"
CIRCUIT_TRIPPED = "tripped"
CIRCUIT_STATES = (CIRCUIT_NORMAL, CIRCUIT_MAINTENANCE, CIRCUIT_TRIPPED)

# 接电安排状态（每个冷藏箱最多一条活跃安排）
ASSN_QUEUED = "queued"            # 排队等回路（容量缺口）
ASSN_CONNECTED = "connected"      # 已接电
ASSN_PENDING_SUPPLY = "pending_supply"  # 接电失败后缺回路，待补
ASSN_INVALID = "invalid"          # 船期或回路状态变化后失效（保留为依据）
ASSN_SUPERSEDED = "superseded"    # 被后来的安排取代（恢复/重排）

ACTIVE_ASSIGNMENT_STATES = (ASSN_QUEUED, ASSN_CONNECTED, ASSN_PENDING_SUPPLY)
TERMINAL_ASSIGNMENT_STATES = (ASSN_INVALID, ASSN_SUPERSEDED)

# 跳闸记录状态
TRIP_OPEN = "open"
TRIP_RECOVERED = "recovered"

# 批次状态
BATCH_OPEN = "open"
BATCH_COMPLETE = "complete"

# 角色
ROLE_PLANNER = "yard_planner"      # 堆场调度：建档、船期、放行
ROLE_ELECTRICIAN = "electrician"   # 电气员：接电、跳闸、恢复
KNOWN_ROLES = (ROLE_PLANNER, ROLE_ELECTRICIAN, "admin")


class DomainRules:
    # 温度容差（摄氏度）：设定温度与箱内实测温差超过该值，温控核对不通过
    TEMP_TOLERANCE_C = 3.0

    def known_role(self, role: str) -> bool:
        return role in KNOWN_ROLES

    # ---------- 建档校验 ----------
    def validate_reefer(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "reefer_code": text(payload, "reefer_code"),
            "voyage_no": optional_text(payload, "voyage_no"),
            "required_kw": number(payload, "required_kw", 0.1),
            "set_temp_c": number(payload, "set_temp_c", -60, 40),
        }

    def validate_circuit(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = {
            "circuit_code": text(payload, "circuit_code"),
            "capacity_kw": number(payload, "capacity_kw", 0.1),
            "location": optional_text(payload, "location"),
        }
        state = payload.get("state", CIRCUIT_NORMAL)
        if state not in CIRCUIT_STATES:
            raise ValidationError("state只能是%s" % "/".join(CIRCUIT_STATES))
        data["state"] = state
        return data

    def validate_voyage(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        etd = text(payload, "etd")
        try:
            datetime.fromisoformat(etd.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("etd必须是ISO-8601时间，例如2026-10-08T18:00:00+08:00") from exc
        return {
            "voyage_no": text(payload, "voyage_no"),
            "vessel": text(payload, "vessel"),
            "etd": etd,
        }

    # ---------- 接电前核对 ----------
    def temp_check(self, set_temp_c: float, actual_temp_c: float) -> Dict[str, Any]:
        delta = round(abs(float(actual_temp_c) - float(set_temp_c)), 2)
        ok = delta <= self.TEMP_TOLERANCE_C
        return {
            "temp_ok": ok,
            "set_temp_c": float(set_temp_c),
            "actual_temp_c": float(actual_temp_c),
            "temp_delta_c": delta,
        }

    def check_before_connect(self, reefer: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        """接电前核对：必带实测温度，温控不符直接拒绝；容量核对在选回路阶段做。"""
        actual = number(data, "actual_temp_c", -60, 40)
        temp = self.temp_check(reefer["set_temp_c"], actual)
        if not temp["temp_ok"]:
            raise ValidationError(
                "温控核对不通过：实测温度与设定温度相差%s°C，超过容差%s°C"
                % (temp["temp_delta_c"], self.TEMP_TOLERANCE_C)
            )
        return temp

    @staticmethod
    def circuit_available_kw(circuit: Dict[str, Any], used_kw: float) -> float:
        if circuit["state"] != CIRCUIT_NORMAL:
            return 0.0
        return round(float(circuit["capacity_kw"]) - float(used_kw), 2)

    @staticmethod
    def pick_circuit(circuits: List[Dict[str, Any]], required_kw: float) -> Optional[Dict[str, Any]]:
        """从带 remaining_kw 的回路里选：正常、剩余容量够用，按剩余容量最小优先（紧凑装箱）。"""
        candidates = [c for c in circuits if c.get("remaining_kw", 0) >= float(required_kw)]
        if not candidates:
            return None
        return sorted(candidates, key=lambda c: (c["remaining_kw"], c["circuit_code"]))[0]

    @staticmethod
    def capacity_gap(circuits: List[Dict[str, Any]], required_kw: float) -> float:
        """容量不够时的缺口：需求与最大可用剩余容量之差。"""
        best = max((c.get("remaining_kw", 0) for c in circuits), default=0.0)
        return round(max(0.0, float(required_kw) - best), 2)
