"""业务用例编排：接电核对、跳闸/船期失效重排、先到先得、批次恢复与审计来源链。"""
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ValidationError, number, optional_text, text
from .repository import Repository
from .rules import DomainRules

UNLOADED_REEFER_STATES = {"waiting", "connected", "queued", "pending_circuit", "pending_recovery"}
LOADED = "loaded"


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    # ---------- 身份 ----------
    def _actor(self, actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _require(self, actor: Actor, permission: str) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")
        if not self.rules.can(actor.role, permission):
            raise PermissionDenied("角色无权执行该操作")

    def _require_known(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    # ---------- 视图组装 ----------
    def _views(self, c) -> List[Dict[str, Any]]:
        return self.rules.circuit_views(self._circuits(c), self.repository.active_connections(c))

    @staticmethod
    def _circuits(c) -> List[Dict[str, Any]]:
        return [dict(r) for r in c.execute("SELECT * FROM circuits ORDER BY id").fetchall()]

    def _reefer_view(self, c, reefer: Dict[str, Any]) -> Dict[str, Any]:
        view = dict(reefer)
        queue_row = c.execute("SELECT * FROM queues WHERE reefer_id=?", (reefer["id"],)).fetchone()
        view["queue"] = dict(queue_row) if queue_row else None
        conn = self.repository.active_connection(c, reefer["id"])
        if conn is None and reefer["state"] == LOADED:
            row = c.execute(
                "SELECT * FROM connections WHERE reefer_id=? AND state='loaded_frozen' ORDER BY id DESC LIMIT 1",
                (reefer["id"],)).fetchone()
            conn = dict(row) if row else None
        view["connection"] = conn
        if conn:
            circuit = self.repository.get_circuit(conn["circuit_id"], c)
            view["circuit_code"] = circuit["circuit_code"]
        return view

    def ledger(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_known(actor)
        with self.repository.tx() as c:
            reefers = [dict(r) for r in c.execute("SELECT * FROM reefers ORDER BY id").fetchall()]
            items = [self._reefer_view(c, r) for r in reefers]
            circuits = self.rules.circuit_views(self._circuits(c), self.repository.active_connections(c))
        return {
            "reefers": items,
            "circuits": circuits,
            "voyages": self.repository.list_voyages(),
            "trips": self.repository.list_trips(),
            "queue": self.repository.list_queue(),
            "batches": self.repository.list_batches(),
            "conflicts": self.repository.list_conflict_candidates(),
        }

    def _circuit_overview(self) -> List[Dict[str, Any]]:
        with self.repository.tx() as c:
            return self.rules.circuit_views(self._circuits(c), self.repository.active_connections(c))

    # ---------- 建档 ----------
    def create_reefer(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require(actor, "reefer_create")
        data = self.rules.validate_reefer(payload or {})
        with self.repository.tx() as c:
            if data["voyage_id"]:
                self.repository.get_voyage(data["voyage_id"], c)
            reefer = self.repository.create_reefer(c, data)
            self.repository.emit(c, "reefer", reefer["id"], "registered", actor.user_id, reefer["version"],
                                 {"summary": "冷藏箱建档", "data": data})
        return self.get_reefer(actor, reefer["id"])

    def create_circuit(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require(actor, "circuit_update")
        data = self.rules.validate_circuit(payload or {})
        if data["temp_min_c"] >= data["temp_max_c"]:
            raise ValidationError("温控下限必须低于上限")
        with self.repository.tx() as c:
            circuit = self.repository.create_circuit(c, data)
            self.repository.emit(c, "circuit", circuit["id"], "created", actor.user_id, circuit["version"],
                                 {"summary": "供电回路登记", "data": data})
        return self._circuit_detail(circuit["id"])

    def create_voyage(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require(actor, "voyage_update")
        data = self.rules.validate_voyage(payload or {})
        with self.repository.tx() as c:
            voyage = self.repository.create_voyage(c, data)
            self.repository.emit(c, "voyage", voyage["id"], "created", actor.user_id, voyage["version"],
                                 {"summary": "船期登记", "data": data})
        return voyage

    # ---------- 接电 ----------
    def connect(self, actor: Actor, reefer_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require(actor, "connect")
        preferred = self._optional_id(data, "preferred_circuit_id")
        conflict_info: Optional[Dict[str, Any]] = None
        result: Dict[str, Any] = {}
        payload_out: Dict[str, Any] = {}
        with self.repository.tx() as c:
            reefer = self.repository.get_reefer(reefer_id, c)
            if reefer["state"] == LOADED:
                raise Conflict("冷藏箱已装船，安排冻结")
            if reefer["state"] not in UNLOADED_REEFER_STATES:
                raise Conflict("冷藏箱当前状态%s，无法接电" % reefer["state"])
            # 两人同时提交：已被先到者占用 -> 先到先得，后来者落冲突候选（先提交，再在事务外拒绝）
            if reefer["state"] in {"connected", "queued"}:
                active = self.repository.active_connection(c, reefer_id)
                payload = {"preferred_circuit_id": preferred, "note": optional_text(data or {}, "note")}
                candidate = self.repository.add_conflict_candidate(
                    c, reefer_id, actor.user_id, "reefer_busy", payload,
                    requested_circuit_id=(active["circuit_id"] if active else preferred))
                self.repository.emit(c, "reefer", reefer_id, "conflict_candidate", actor.user_id, reefer["version"],
                                     {"summary": "接电冲突，已保留为冲突候选#%s" % candidate["id"],
                                      "candidate_id": candidate["id"], "source": "connect"})
                conflict_info = {"candidate_id": candidate["id"], "reefer_state": reefer["state"]}
            else:
                result = self._place(c, reefer, "connect", actor=actor, preferred=preferred,
                                     note=optional_text(data or {}, "note"))
                payload_out = self._reefer_view(c, self.repository.get_reefer(reefer_id, c))
        if conflict_info is not None:
            raise Conflict("该冷藏箱已有在先%s安排，提交已保留为冲突候选#%s"
                           % ("接电" if conflict_info["reefer_state"] == "connected" else "排队",
                              conflict_info["candidate_id"]),
                           data={"candidate_id": conflict_info["candidate_id"]})
        if result.get("placed"):
            self._pump_queue("connect#%s" % result["connection_id"], actor)
        return payload_out

    def _place(self, c, reefer: Dict[str, Any], source: str, actor: Optional[Actor] = None,
               preferred: Optional[int] = None, reason: str = "gap", note: str = "",
               batch_id: Optional[int] = None) -> Dict[str, Any]:
        """接电前核对容量与温控；容量不足排队并写缺口。返回复位结果。"""
        actor_id = actor.user_id if actor else "system"
        views = self._views(c)
        chosen: Optional[Dict[str, Any]] = None
        blockers: List[str] = []

        if preferred is not None:
            target = next((v for v in views if v["id"] == preferred), None)
            if target is None:
                raise ValidationError("指定回路不存在")
            if target["state"] != "active":
                raise Conflict("指定回路当前不可用（%s）" % target["state"])
            if not self.rules.temp_ok(reefer, target):
                raise ValidationError("指定回路温控不覆盖冷藏箱设定")
            if target["free_kw"] + 1e-9 < reefer["required_kw"]:
                chosen = None
                blockers = ["%s:容量缺口%.2fkW" % (target["circuit_code"],
                                                   round(reefer["required_kw"] - target["free_kw"], 2))]
            else:
                chosen = target
        else:
            eligible, blockers = self.rules.candidates_for(reefer, views)
            chosen = eligible[0] if eligible else None

        if chosen is not None:
            self.repository.dequeue(c, reefer["id"])
            conn = self.repository.add_connection(
                c, reefer["id"], chosen["id"], reefer["required_kw"],
                basis="容量%.2f>=%.2fkW;温控%s~%s覆盖%s±%s℃" % (
                    chosen["free_kw"] + reefer["required_kw"], reefer["required_kw"],
                    chosen["temp_min_c"], chosen["temp_max_c"], reefer["temp_setpoint_c"], reefer["temp_tolerance_c"]),
                source=source, batch_id=batch_id)
            reefer["state"] = "connected"
            self.repository.save_reefer(c, reefer, ["state"])
            self.repository.emit(c, "reefer", reefer["id"], "connected", actor_id, reefer["version"],
                                 {"summary": "接电回路%s（%s）" % (chosen["circuit_code"], source),
                                  "connection_id": conn["id"], "circuit_id": chosen["id"],
                                  "circuit_code": chosen["circuit_code"], "source": source, "note": note})
            return {"placed": True, "connection_id": conn["id"], "circuit_id": chosen["id"]}

        gap = self.rules.largest_gap(reefer, views if preferred is None else
                                     [v for v in views if v["id"] == preferred])
        queue_reason = "preferred" if preferred is not None else reason
        self.repository.enqueue(c, reefer["id"], queue_reason, gap, source,
                                preferred_circuit_id=preferred)
        reefer["state"] = "queued"
        self.repository.save_reefer(c, reefer, ["state"])
        self.repository.emit(c, "reefer", reefer["id"], "queued", actor_id, reefer["version"],
                             {"summary": "容量不足排队，缺口%.2fkW（%s）" % (gap, source),
                              "gap_kw": gap, "blockers": blockers, "reason": queue_reason, "source": source})
        return {"placed": False, "gap_kw": gap, "blockers": blockers}

    # ---------- 跳闸 ----------
    def trip_circuit(self, actor: Actor, circuit_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require(actor, "circuit_update")
        reason = optional_text(data or {}, "reason", "现场跳闸")
        expected = None
        if (data or {}).get("expected_recover_hour") is not None:
            expected = number(data, "expected_recover_hour", 0, 240)
        affected: List[int] = []
        kept: List[int] = []
        with self.repository.tx() as c:
            circuit = self.repository.get_circuit(circuit_id, c)
            self.rules.require_circuit_state(circuit, {"active"}, "无法登记跳闸")
            circuit["state"] = "tripped"
            self.repository.save_circuit(c, circuit, ["state"])
            trip = self.repository.add_trip(c, circuit_id, reason, expected)
            self.repository.emit(c, "circuit", circuit_id, "tripped", actor.user_id, circuit["version"],
                                 {"summary": "回路%s跳闸：%s" % (circuit["circuit_code"], reason),
                                  "trip_id": trip["id"], "expected_recover_hour": expected})
            conns = self.repository.effective_connections(c, circuit_id)
            for conn in conns:
                reefer = self.repository.get_reefer(conn["reefer_id"], c)
                if reefer["state"] == LOADED or conn["state"] == "loaded_frozen":
                    # 已装船保留原依据
                    kept.append(reefer["id"])
                    self.repository.emit(c, "reefer", reefer["id"], "basis_kept", actor.user_id, reefer["version"],
                                         {"summary": "已装船，保留原接电依据#%s（回路跳闸）" % conn["id"],
                                          "connection_id": conn["id"], "source": "trip#%s" % trip["id"]})
                    continue
                self.repository.void_connections(c, [conn["id"]], "circuit_tripped")
                self.repository.enqueue(c, reefer["id"], "trip", 0,
                                        "trip#%s" % trip["id"])
                reefer["state"] = "pending_circuit"
                self.repository.save_reefer(c, reefer, ["state"])
                affected.append(reefer["id"])
                self.repository.emit(c, "reefer", reefer["id"], "invalidated", actor.user_id, reefer["version"],
                                     {"summary": "回路跳闸，安排失效待补回路", "void_connection_id": conn["id"],
                                      "source": "trip#%s" % trip["id"]})
        return {"circuit_id": circuit_id, "state": "tripped", "trip_id": trip["id"],
                "invalidated_reefers": affected, "loaded_kept": kept}

    def recover_circuit(self, actor: Actor, circuit_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require(actor, "circuit_update")
        with self.repository.tx() as c:
            circuit = self.repository.get_circuit(circuit_id, c)
            self.rules.require_circuit_state(circuit, {"tripped", "maintenance"}, "无法恢复投用")
            circuit["state"] = "active"
            self.repository.save_circuit(c, circuit, ["state"])
            open_trips = [dict(r) for r in c.execute(
                "SELECT * FROM trips WHERE circuit_id=? AND recovered_at IS NULL ORDER BY id", (circuit_id,)).fetchall()]
            for trip in open_trips:
                self.repository.close_trip(c, trip["id"])
            self.repository.emit(c, "circuit", circuit_id, "restored", actor.user_id, circuit["version"],
                                 {"summary": "回路%s恢复投用，触发重排" % circuit["circuit_code"],
                                  "closed_trip_ids": [t["id"] for t in open_trips], "source": "circuit_restore"})
        pump = self._pump_queue("circuit#%s_restored" % circuit_id, actor)
        return {"circuit_id": circuit_id, "state": "active", "pump": pump}

    def set_circuit_maintenance(self, actor: Actor, circuit_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require(actor, "circuit_update")
        reason = optional_text(data or {}, "reason", "计划检修")
        with self.repository.tx() as c:
            circuit = self.repository.get_circuit(circuit_id, c)
            self.rules.require_circuit_state(circuit, {"active"}, "无法转为检修")
            circuit["state"] = "maintenance"
            self.repository.save_circuit(c, circuit, ["state"])
            self.repository.emit(c, "circuit", circuit_id, "maintenance", actor.user_id, circuit["version"],
                                 {"summary": "回路%s检修停用，未装船安排失效重排：%s" % (circuit["circuit_code"], reason)})
            conns = self.repository.effective_connections(c, circuit_id)
            affected, kept = [], []
            for conn in conns:
                reefer = self.repository.get_reefer(conn["reefer_id"], c)
                if reefer["state"] == LOADED or conn["state"] == "loaded_frozen":
                    kept.append(reefer["id"])
                    self.repository.emit(c, "reefer", reefer["id"], "basis_kept", actor.user_id, reefer["version"],
                                         {"summary": "已装船，保留原接电依据#%s（回路检修）" % conn["id"],
                                          "connection_id": conn["id"], "source": "circuit_maintenance"})
                    continue
                self.repository.void_connections(c, [conn["id"]], "circuit_maintenance")
                self._place(c, reefer, "circuit_change", actor=actor, reason="circuit_change")
                affected.append(reefer["id"])
        return {"circuit_id": circuit_id, "state": "maintenance",
                "invalidated_reefers": affected, "loaded_kept": kept}

    # ---------- 船期变更 ----------
    def revise_voyage(self, actor: Actor, voyage_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require(actor, "voyage_update")
        sail_hour = number(data or {}, "sail_hour", 0, 240)
        invalidated, queued, kept = [], [], []
        with self.repository.tx() as c:
            voyage = self.repository.get_voyage(voyage_id, c)
            old_hour = voyage["sail_hour"]
            voyage["sail_hour"] = sail_hour
            self.repository.save_voyage(c, voyage, ["sail_hour"])
            self.repository.emit(c, "voyage", voyage_id, "revised", actor.user_id, voyage["version"],
                                 {"summary": "船期变更 %s->%s，未装船安排失效重排" % (old_hour, sail_hour),
                                  "old_sail_hour": old_hour, "new_sail_hour": sail_hour, "source": "voyage_revised"})
            rows = c.execute("SELECT * FROM reefers WHERE voyage_id=? ORDER BY id", (voyage_id,)).fetchall()
            for row in rows:
                reefer = dict(row)
                if reefer["state"] == LOADED:
                    kept.append(reefer["id"])
                    frozen = c.execute(
                        "SELECT * FROM connections WHERE reefer_id=? AND state='loaded_frozen' ORDER BY id DESC LIMIT 1",
                        (reefer["id"],)).fetchone()
                    self.repository.emit(c, "reefer", reefer["id"], "basis_kept", actor.user_id, reefer["version"],
                                         {"summary": "已装船，保留原接电依据#%s（船期变更）" % (
                                              dict(frozen)["id"] if frozen else "?"),
                                          "connection_id": dict(frozen)["id"] if frozen else None,
                                          "source": "voyage#%s_revised" % voyage_id})
                    continue
                conn = self.repository.active_connection(c, reefer["id"])
                if conn:
                    self.repository.void_connections(c, [conn["id"]], "voyage_changed")
                result = self._place(c, reefer, "voyage_change", actor=actor, reason="voyage_change")
                invalidated.append(reefer["id"])
                if not result["placed"]:
                    queued.append(reefer["id"])
        return {"voyage_id": voyage_id, "sail_hour": sail_hour,
                "invalidated_reefers": invalidated, "still_queued": queued, "loaded_kept": kept}

    # ---------- 装船放行 ----------
    def load_reefer(self, actor: Actor, reefer_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require(actor, "load")
        with self.repository.tx() as c:
            reefer = self.repository.get_reefer(reefer_id, c)
            conn = self.repository.active_connection(c, reefer_id)
            if conn is None:
                raise Conflict("未接电冷藏箱不得装船放行")
            self.rules.require_reefer_state(reefer, {"connected"}, "装船前须保持接电")
            reefer["state"] = LOADED
            self.repository.save_reefer(c, reefer, ["state"])
            self.repository.dequeue(c, reefer_id)
            release = dict(conn)
            # 装船后改由船方供电：释放岸电容量（不再计入在用），但接电依据冻结保留可追溯
            self.repository.freeze_connection(c, conn["id"])
            self.repository.emit(c, "reefer", reefer_id, "loaded", actor.user_id, reefer["version"],
                                 {"summary": "装船放行，原接电依据#%s保留（回路%s）" % (conn["id"],
                                              self.repository.get_circuit(conn["circuit_id"], c)["circuit_code"]),
                                  "basis_connection_id": conn["id"], "basis": release["basis"],
                                  "source": "manual_load"})
        return self.get_reefer(actor, reefer_id)

    # ---------- 批量接电与恢复 ----------
    def batch_connect(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require(actor, "batch")
        items = (payload or {}).get("reefer_ids", [])
        if not isinstance(items, list) or not items or any(not isinstance(x, int) for x in items):
            raise ValidationError("reefer_ids必须是非空整数列表")
        prefs = (payload or {}).get("preferred", {})
        if not isinstance(prefs, dict):
            raise ValidationError("preferred必须是{reefer_id: circuit_id}映射")
        batch_no = "B" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        with self.repository.tx() as c:
            reefers = []
            for rid in items:
                reefer = self.repository.get_reefer(int(rid), c)
                if reefer["state"] == LOADED:
                    raise ValidationError("冷藏箱#%s已装船，不能纳入批量接电" % rid)
                if reefer["state"] not in UNLOADED_REEFER_STATES:
                    raise ValidationError("冷藏箱#%s状态%s，不能纳入批量接电" % (rid, reefer["state"]))
                reefers.append(reefer)
            # 接电前核对：任一个温控/容量/回路不满足 -> 整批不落接电记录
            views = self._views(c)
            plan = {}
            for reefer in reefers:
                pref = prefs.get(str(reefer["id"]), prefs.get(reefer["id"]))
                pref = int(pref) if pref is not None else None
                if pref is not None:
                    target = next((v for v in views if v["id"] == pref), None)
                    if target is None or target["state"] != "active":
                        raise ValidationError("冷藏箱#%s指定回路不可用" % reefer["id"])
                    if not self.rules.temp_ok(reefer, target):
                        raise ValidationError("冷藏箱#%s与指定回路温控不匹配" % reefer["id"])
                    plan[reefer["id"]] = target
                else:
                    eligible, _ = self.rules.candidates_for(reefer, views)
                    if not eligible:
                        raise ValidationError("冷藏箱#%s无满足温控且有容量的回路，整批不接电" % reefer["id"])
                    plan[reefer["id"]] = eligible[0]
                # 模拟占用容量，保证批内不超额
                view = plan[reefer["id"]]
                view["free_kw"] = round(view["free_kw"] - reefer["required_kw"], 2)
            batch = self.repository.create_batch(c, batch_no, len(reefers))
            snapshot = []
            for reefer in reefers:
                view = plan[reefer["id"]]
                self.repository.dequeue(c, reefer["id"])
                conn = self.repository.add_connection(
                    c, reefer["id"], view["id"], reefer["required_kw"],
                    basis="批量接电；容量/温控接电前核对通过",
                    source="batch#%s" % batch["id"], batch_id=batch["id"])
                reefer["state"] = "connected"
                self.repository.save_reefer(c, reefer, ["state"])
                snapshot.append({"reefer_id": reefer["id"], "circuit_id": view["id"],
                                 "connection_id": conn["id"], "required_kw": reefer["required_kw"]})
                self.repository.emit(c, "reefer", reefer["id"], "connected", actor.user_id, reefer["version"],
                                     {"summary": "批量%s接电回路%s" % (batch_no, view["circuit_code"]),
                                      "connection_id": conn["id"], "circuit_id": view["id"],
                                      "batch_id": batch["id"], "source": "batch#%s" % batch["id"]})
            self.repository.finish_batch(c, batch["id"], "complete", snapshot, "整批核对通过并接电")
            self.repository.emit(c, "batch", batch["id"], "complete", actor.user_id, 1,
                                 {"summary": "批次%s完成，%s箱全部接电" % (batch_no, len(snapshot)),
                                  "size": len(snapshot)})
        self._pump_queue("batch#%s" % batch["id"], actor)
        return self.repository.get_batch(batch["id"])

    def batch_fail(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        """登记一次接电失败的批次：不落任何接电记录，记录失败批次供审计，随后按最近完整批次恢复。"""
        actor = self._actor(actor)
        self._require(actor, "batch")
        reefer_ids = (payload or {}).get("reefer_ids", [])
        reason = optional_text(payload or {}, "reason", "接电失败")
        if not isinstance(reefer_ids, list) or not reefer_ids:
            raise ValidationError("reefer_ids必须是非空列表")
        batch_no = "B" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        failed_batch_id: Optional[int] = None
        with self.repository.tx() as c:
            for rid in reefer_ids:
                if not isinstance(rid, int):
                    raise ValidationError("reefer_ids必须为整数")
                self.repository.get_reefer(rid, c)
            batch = self.repository.create_batch(c, batch_no, len(reefer_ids))
            failed_batch_id = batch["id"]
            self.repository.finish_batch(c, batch["id"], "failed", [], reason)
            self.repository.emit(c, "batch", batch["id"], "failed", actor.user_id, 1,
                                 {"summary": "批次%s接电失败：%s，整批回滚" % (batch_no, reason),
                                  "reefer_ids": reefer_ids, "source": "manual_fail"})
        recovery = self.recover_from_batch(actor, {"failed_batch_id": failed_batch_id, "reason": reason,
                                                   "reefer_ids": reefer_ids})
        return {"failed_batch_id": failed_batch_id, "recovery": recovery}

    def recover_from_batch(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        """从最近完整批次恢复；冷藏箱缺回路按待补处理。"""
        actor = self._actor(actor)
        self._require(actor, "batch")
        failed_before = (payload or {}).get("failed_batch_id")
        reason = optional_text(payload or {}, "reason", "接电失败恢复")
        requested = [int(x) for x in (payload or {}).get("reefer_ids", []) if isinstance(x, int)]
        restored, pending = [], []
        with self.repository.tx() as c:
            last = self.repository.last_complete_batch(c, before_batch_id=failed_before)
            if last is None:
                targets: List[int] = requested
                for rid in targets:
                    reefer = self.repository.get_reefer(rid, c)
                    if reefer["state"] == LOADED:
                        continue
                    conn = self.repository.active_connection(c, reefer["id"])
                    if conn:
                        self.repository.void_connections(c, [conn["id"]], "recovery_no_basis")
                    self._mark_pending(c, reefer, pending, actor.user_id,
                                       "无完整批次可恢复，按待补处理", "recovery:no_complete_batch", reason)
                self._emit_recovery_summary(c, None, None, restored, pending, actor.user_id)
                return {"source_batch_id": None, "restored": [], "pending": pending,
                        "note": "无完整批次，相关冷藏箱全部待补"}
            snapshot_ids = [int(e["reefer_id"]) for e in last["snapshot"]]
            entries = {int(e["reefer_id"]): e for e in last["snapshot"]}
            # 最近完整批次的箱优先按原依据恢复；本次失败中未被快照覆盖的箱随后统一核对
            targets = snapshot_ids + [rid for rid in requested if rid not in entries]
            for rid in targets:
                reefer = self.repository.get_reefer(rid, c)
                if reefer["state"] == LOADED:
                    continue
                if self.repository.active_connection(c, rid):
                    continue  # 仍有有效接电，无需恢复
                views = self._views(c)
                entry = entries.get(rid)
                chosen = None
                if entry is not None:
                    original = next((v for v in views if v["id"] == entry["circuit_id"]), None)
                    if original and original["state"] == "active" and self.rules.temp_ok(reefer, original) \
                            and original["free_kw"] + 1e-9 >= reefer["required_kw"]:
                        chosen = original
                if chosen is None:
                    eligible, _ = self.rules.candidates_for(reefer, views)
                    chosen = eligible[0] if eligible else None
                if chosen is None:
                    self._mark_pending(c, reefer, pending, actor.user_id,
                                       "恢复时缺回路，按待补处理",
                                       "recovery:batch#%s" % last["id"], reason)
                    continue
                self.repository.dequeue(c, rid)
                same = entry is not None and chosen["id"] == entry["circuit_id"]
                conn = self.repository.add_connection(
                    c, rid, chosen["id"], reefer["required_kw"],
                    basis="按完整批次#%s恢复；%s" % (last["id"],
                          "原回路同配" if same else ("原回路不可用，改配" if entry else "批次外箱，按当前容量/温控补配")),
                    source="recovery:batch#%s" % last["id"])
                reefer["state"] = "connected"
                self.repository.save_reefer(c, reefer, ["state"])
                restored.append({"reefer_id": rid, "circuit_id": chosen["id"], "connection_id": conn["id"],
                                 "same_circuit": same})
                self.repository.emit(c, "reefer", rid, "restored", actor.user_id, reefer["version"],
                                     {"summary": "按批次%s恢复接电至%s" % (last["batch_no"], chosen["circuit_code"]),
                                      "connection_id": conn["id"], "source_batch_id": last["id"],
                                      "source": "recovery:batch#%s" % last["id"]})
            self._emit_recovery_summary(c, last["id"], last["batch_no"], restored, pending, actor.user_id)
        pump = self._pump_queue("recovery:batch#%s" % (last["id"] if last else 0), actor)
        return {"source_batch_id": last["id"] if last else None,
                "source_batch_no": last["batch_no"] if last else None,
                "restored": restored, "pending": pending, "pump": pump}

    def _mark_pending(self, c, reefer: Dict[str, Any], pending: List[int], actor_id: str,
                      summary: str, source: str, reason: str = "") -> None:
        self.repository.enqueue(c, reefer["id"], "recovery", reefer["required_kw"], source)
        reefer["state"] = "pending_recovery"
        self.repository.save_reefer(c, reefer, ["state"])
        pending.append(reefer["id"])
        self.repository.emit(c, "reefer", reefer["id"], "pending_recovery", actor_id, reefer["version"],
                             {"summary": summary, "source": source, "reason": reason})

    def _emit_recovery_summary(self, c, batch_id, batch_no, restored, pending, actor_id) -> None:
        if batch_id is None:
            return
        self.repository.emit(c, "batch", batch_id, "recovery_applied", actor_id, 1,
                             {"summary": "按批次%s执行恢复：恢复%s，待补%s" % (batch_no, len(restored), len(pending)),
                              "restored": restored, "pending": pending,
                              "source": "recovery:batch#%s" % batch_id})

    # ---------- 队列自动消化 ----------
    def _pump_queue(self, trigger: str, actor: Optional[Actor]) -> Dict[str, Any]:
        actor_id = actor.user_id if actor else "system"
        connected, still_waiting = [], []
        progressed = True
        while progressed:
            progressed = False
            with self.repository.tx() as c:
                views = self._views(c)
                for item in self.repository.list_queue():
                    if item["reason"] == "recovery":
                        continue  # 待补项只由“按完整批次恢复”统一消费
                    reefer = c.execute("SELECT * FROM reefers WHERE id=?", (item["reefer_id"],)).fetchone()
                    if reefer is None:
                        continue
                    reefer = dict(reefer)
                    if reefer["state"] in {LOADED, "cancelled"}:
                        continue
                    eligible, _ = self.rules.candidates_for(reefer, views)
                    if not eligible:
                        still_waiting.append(reefer["id"])
                        continue
                    chosen = eligible[0]
                    self.repository.dequeue(c, reefer["id"])
                    conn = self.repository.add_connection(
                        c, reefer["id"], chosen["id"], reefer["required_kw"],
                        basis="队列重排：容量/温控核对通过", source="reschedule:%s" % trigger)
                    reefer["state"] = "connected"
                    self.repository.save_reefer(c, reefer, ["state"])
                    self.repository.emit(c, "reefer", reefer["id"], "rescheduled", actor_id, reefer["version"],
                                         {"summary": "触发%s后重排接电至%s" % (trigger, chosen["circuit_code"]),
                                          "connection_id": conn["id"], "circuit_id": chosen["id"],
                                          "source": "reschedule:%s" % trigger})
                    chosen["free_kw"] = round(chosen["free_kw"] - reefer["required_kw"], 2)
                    connected.append(reefer["id"])
                    progressed = True
        return {"connected": connected, "still_waiting": sorted(set(still_waiting)), "trigger": trigger}

    def pump_queue(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require(actor, "connect")
        return self._pump_queue("manual", actor)

    # ---------- 冲突候选 ----------
    def list_conflicts(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._require(actor, "connect")
        return self.repository.list_conflict_candidates()

    def resolve_conflict(self, actor: Actor, candidate_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require(actor, "connect")
        decision = text(data or {}, "decision")
        if decision not in {"discard", "queue"}:
            raise ValidationError("decision只能是discard/queue")
        with self.repository.tx() as c:
            candidate = self.repository.resolve_candidate(c, candidate_id, decision)
            reefer = self.repository.get_reefer(candidate["reefer_id"], c)
            if decision == "queue" and reefer["state"] != LOADED and not self.repository.active_connection(c, reefer["id"]):
                self._place(c, reefer, "conflict#%s" % candidate_id, actor=actor,
                            preferred=candidate.get("requested_circuit_id"))
            reefer = self.repository.get_reefer(reefer["id"], c)
            self.repository.emit(c, "reefer", reefer["id"], "conflict_resolved", actor.user_id,
                                 reefer["version"],
                                 {"summary": "冲突候选#%s处理：%s" % (candidate_id, decision),
                                  "candidate_id": candidate_id, "decision": decision})
            candidate = dict(c.execute("SELECT * FROM conflict_candidates WHERE id=?", (candidate_id,)).fetchone())
            candidate["payload"] = json.loads(candidate["payload"])
        return candidate

    # ---------- 查询 ----------
    def get_reefer(self, actor: Actor, reefer_id: int) -> Dict[str, Any]:
        self._require_known(self._actor(actor))
        with self.repository.tx() as c:
            reefer = self.repository.get_reefer(reefer_id, c)
            return self._reefer_view(c, reefer)

    def list_reefers(self, actor: Actor, state: str = None) -> List[Dict[str, Any]]:
        self._require_known(self._actor(actor))
        with self.repository.tx() as c:
            if state:
                rows = c.execute("SELECT * FROM reefers WHERE state=? ORDER BY id", (state,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM reefers ORDER BY id").fetchall()
            return [self._reefer_view(c, dict(r)) for r in rows]

    def _circuit_detail(self, circuit_id: int) -> Dict[str, Any]:
        with self.repository.tx() as c:
            return self.rules.circuit_view(self.repository.get_circuit(circuit_id, c),
                                           self.repository.active_connections(c))

    def list_circuits(self, actor: Actor) -> List[Dict[str, Any]]:
        self._require(self._actor(actor), "circuit_update")
        return self._circuit_overview()

    def list_voyages(self, actor: Actor) -> List[Dict[str, Any]]:
        self._require(self._actor(actor), "voyage_update")
        return self.repository.list_voyages()

    def list_trips(self, actor: Actor, circuit_id: int = None) -> List[Dict[str, Any]]:
        self._require(self._actor(actor), "circuit_update")
        return self.repository.list_trips(circuit_id)

    def list_queue(self, actor: Actor) -> List[Dict[str, Any]]:
        self._require(self._actor(actor), "connect")
        return self.repository.list_queue()

    def list_batches(self, actor: Actor) -> List[Dict[str, Any]]:
        self._require(self._actor(actor), "batch")
        return self.repository.list_batches()

    def timeline(self, actor: Actor, entity_type: str, entity_id: int) -> List[Dict[str, Any]]:
        self._require_known(self._actor(actor))
        return self.audit.timeline(entity_type, entity_id)

    def events(self, actor: Actor, entity_type: str = None, limit: int = 200) -> List[Dict[str, Any]]:
        self._require_known(self._actor(actor))
        return self.repository.all_events(limit=limit, entity_type=entity_type)

    def stats(self, actor: Actor) -> Dict[str, Any]:
        self._require_known(self._actor(actor))
        with self.repository.tx() as c:
            rows = c.execute("SELECT state, COUNT(*) AS total FROM reefers GROUP BY state").fetchall()
            reefer_stats = {r["state"]: r["total"] for r in rows}
            crows = c.execute("SELECT state, COUNT(*) AS total FROM circuits GROUP BY state").fetchall()
            circuit_stats = {r["state"]: r["total"] for r in crows}
        return {"reefers": dict(reefer_stats), "circuits": dict(circuit_stats),
                "queue": len(self.repository.list_queue()),
                "conflicts": len(self.repository.list_conflict_candidates())}

    @staticmethod
    def _optional_id(data: Dict[str, Any], key: str) -> Optional[int]:
        value = (data or {}).get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError("%s必须是整数ID" % key)
        return value
