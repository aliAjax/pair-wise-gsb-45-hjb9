"""冷藏箱供电台账用例编排。

规则落地：
- 接电前核对容量与温控；容量不够入队（queued）并记录容量缺口 gap_kw。
- 船期变更、回路状态变化：未装船的活跃安排立即失效并重排；已装船保留原依据不动。
- 两个合法接电申请同时提交，只认先到（写事务按到达顺序取锁）；
  后来者不落安排，只留 conflict_candidate 申请记录，返回 409。
- 接电失败（跳闸）后从最近完整批次恢复；恢复时缺回路的箱按 pending_supply 待补，
  随后立即触发排队重排。
- 每一步写全局审计事件并带 sources 来源链，可追到接电、跳闸和重排来源。
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder, link
from .domain import (
    Actor,
    Conflict,
    DomainError,
    NotFound,
    PermissionDenied,
    ValidationError,
    optional_text,
    text,
)
from .repository import Repository, Tx
from .rules import (
    ASSN_CONNECTED,
    ASSN_INVALID,
    ASSN_PENDING_SUPPLY,
    ASSN_QUEUED,
    ASSN_SUPERSEDED,
    BATCH_COMPLETE,
    BATCH_OPEN,
    CIRCUIT_MAINTENANCE,
    CIRCUIT_NORMAL,
    CIRCUIT_TRIPPED,
    KNOWN_ROLES,
    REEFER_LOADED,
    ROLE_ELECTRICIAN,
    ROLE_PLANNER,
    DomainRules,
)


class Service:
    def __init__(self, repository: Repository, rules: DomainRules = None) -> None:
        self.repository = repository
        self.rules = rules or DomainRules()

    # ---------- 身份与角色 ----------
    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        if actor.role not in KNOWN_ROLES:
            raise PermissionDenied("未知角色，无权访问台账")
        return actor

    def _check_role(self, actor: Actor, *roles: str) -> None:
        if actor.role != "admin" and actor.role not in roles:
            raise PermissionDenied("角色无权执行该操作")

    @staticmethod
    def _basis(kind: str, **kw: Any) -> Dict[str, Any]:
        data = {"kind": kind}
        data.update(kw)
        return data

    # ---------- 建档 ----------
    def register_reefer(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._check_role(actor, ROLE_PLANNER)
        data = self.rules.validate_reefer(payload or {})
        if data.get("voyage_no"):
            voyage_no = data["voyage_no"]
        else:
            voyage_no = ""
        with self.repository.tx() as tx:
            if voyage_no and tx.get_voyage(voyage_no) is None:
                raise ValidationError("船期%s不存在，请先建船期" % voyage_no)
            if tx.get_reefer_by_code(data["reefer_code"]):
                raise Conflict("冷藏箱编号已存在")
            data["voyage_no"] = voyage_no
            reefer = tx.insert_reefer(data)
            AuditRecorder(tx).record(
                "reefer", reefer["id"], reefer["reefer_code"], "registered", actor.user_id,
                details={"required_kw": reefer["required_kw"], "set_temp_c": reefer["set_temp_c"],
                         "voyage_no": voyage_no},
            )
        return reefer

    def register_circuit(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._check_role(actor, ROLE_PLANNER)
        data = self.rules.validate_circuit(payload or {})
        with self.repository.tx() as tx:
            if tx.get_circuit(data["circuit_code"]):
                raise Conflict("回路编号已存在")
            circuit = tx.insert_circuit(data)
            AuditRecorder(tx).record(
                "circuit", None, circuit["circuit_code"], "registered", actor.user_id,
                details={"capacity_kw": circuit["capacity_kw"], "state": circuit["state"]},
            )
            # 新回路可能释放容量，立即重排一次排队箱
            self._allocate(tx, actor.user_id, reason="new_circuit:%s" % circuit["circuit_code"])
        return circuit

    def create_voyage(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._check_role(actor, ROLE_PLANNER)
        data = self.rules.validate_voyage(payload or {})
        with self.repository.tx() as tx:
            if tx.get_voyage(data["voyage_no"]):
                raise Conflict("航次已存在")
            voyage = tx.insert_voyage(data)
            AuditRecorder(tx).record(
                "voyage", None, voyage["voyage_no"], "created", actor.user_id,
                details={"vessel": voyage["vessel"], "etd": voyage["etd"]},
            )
        return voyage

    def create_batch(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._check_role(actor, ROLE_PLANNER, ROLE_ELECTRICIAN)
        batch_no = text(payload or {}, "batch_no")
        note = optional_text(payload or {}, "note")
        with self.repository.tx() as tx:
            if tx.get_batches_by_no(batch_no):
                raise Conflict("批次号已存在")
            batch = tx.insert_batch(batch_no, note)
            AuditRecorder(tx).record(
                "batch", batch["id"], batch_no, "opened", actor.user_id, details={"note": note}
            )
        return batch

    def complete_batch(self, actor: Actor, batch_no: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._check_role(actor, ROLE_PLANNER, ROLE_ELECTRICIAN)
        with self.repository.tx() as tx:
            batch = self._require_batch_no(tx, batch_no)
            if batch["state"] != BATCH_OPEN:
                raise ValidationError("批次不是进行中状态，不能封存")
            members = tx.list_batch_connections(batch["id"])
            without_circuit = [m["reefer_code"] for m in members if not m["circuit_code"]]
            if without_circuit:
                raise ValidationError(
                    "批次内仍有未接电箱，不能封存为完整批次：%s" % "、".join(without_circuit)
                )
            batch = tx.complete_batch(batch["id"])
            AuditRecorder(tx).record(
                "batch", batch["id"], batch_no, "completed", actor.user_id,
                details={"members": len(members)},
            )
        return batch

    # ---------- 船期变更 ----------
    def reschedule_voyage(self, actor: Actor, voyage_no: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._check_role(actor, ROLE_PLANNER)
        new_etd = text(payload or {}, "etd")
        try:
            datetime.fromisoformat(new_etd.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("etd必须是ISO-8601时间") from exc
        affected: List[Dict[str, Any]] = []
        with self.repository.tx() as tx:
            voyage = tx.get_voyage(voyage_no)
            if voyage is None:
                raise NotFound("船期不存在")
            old_etd = voyage["etd"]
            if new_etd == old_etd:
                raise ValidationError("新船期与原船期相同")
            voyage = tx.update_voyage_etd(voyage_no, new_etd)
            audit = AuditRecorder(tx)
            audit.record(
                "voyage", None, voyage_no, "rescheduled", actor.user_id,
                details={"old_etd": old_etd, "new_etd": new_etd},
            )
            # 该航次未装船的活跃安排立即失效并重排；已装船保留原依据
            for reefer in tx.list_reefers():
                if reefer["voyage_no"] != voyage_no or reefer["state"] == REEFER_LOADED:
                    continue
                active = tx.active_assignment(reefer["id"])
                if active is None:
                    continue
                self._invalidate_and_requeue(
                    tx, actor.user_id, active,
                    reason="voyage_rescheduled:%s %s->%s" % (voyage_no, old_etd, new_etd),
                    trigger=link("voyage", None, voyage_no, "rescheduled"),
                    inherit_circuit=active.get("circuit_code"),
                )
                affected.append({"reefer_code": reefer["reefer_code"]})
            self._allocate(tx, actor.user_id, reason="voyage_rescheduled:%s" % voyage_no,
                           trigger=link("voyage", None, voyage_no, "rescheduled"))
        return {"voyage": voyage, "invalidated": affected}

    # ---------- 回路状态 ----------
    def set_circuit_state(self, actor: Actor, circuit_code: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._check_role(actor, ROLE_PLANNER, ROLE_ELECTRICIAN)
        new_state = text(payload or {}, "state")
        if new_state not in (CIRCUIT_NORMAL, CIRCUIT_MAINTENANCE):
            raise ValidationError("该接口只能切换normal/maintenance，跳闸请用跳闸接口")
        note = optional_text(payload or {}, "note")
        affected: List[Dict[str, Any]] = []
        with self.repository.tx() as tx:
            circuit = self._require_circuit(tx, circuit_code)
            if circuit["state"] == CIRCUIT_TRIPPED:
                raise ValidationError("回路处于跳闸状态，请先做跳闸恢复，不能直接切换")
            if circuit["state"] == new_state:
                raise ValidationError("回路已经是%s状态" % new_state)
            tx.set_circuit_state(circuit_code, new_state)
            audit = AuditRecorder(tx)
            audit.record(
                "circuit", None, circuit_code, "state_changed", actor.user_id,
                details={"old_state": circuit["state"], "new_state": new_state, "note": note},
            )
            if new_state == CIRCUIT_MAINTENANCE:
                # 检修导致失电：未装船接电箱立即失效重排；已装船不动。
                # 每次失效都会新增排队安排，需重新查询，避免游标/快照漏箱。
                while True:
                    victim = next(
                        (a for a in tx.list_assignments(active_only=True)
                         if a.get("circuit_code") == circuit_code
                         and a["reefer_state"] != REEFER_LOADED),
                        None,
                    )
                    if victim is None:
                        break
                    self._invalidate_and_requeue(
                        tx, actor.user_id, victim,
                        reason="circuit_maintenance:%s" % circuit_code,
                        trigger=link("circuit", None, circuit_code, "state_changed"),
                        inherit_circuit=None,
                    )
                    affected.append({"reefer_code": victim["reefer_code"],
                                     "assignment_id": victim["id"]})
            self._allocate(tx, actor.user_id, reason="circuit_state:%s:%s" % (circuit_code, new_state),
                           trigger=link("circuit", None, circuit_code, "state_changed"))
        return {"circuit_code": circuit_code, "state": new_state, "invalidated": affected}

    # ---------- 接电申请（并发裁决核心） ----------
    def request_connect(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._check_role(actor, ROLE_ELECTRICIAN)
        data = payload or {}
        reefer_code = text(data, "reefer_code")
        request_key = text(data, "request_id")
        preferred = optional_text(data, "preferred_circuit")
        batch_no = optional_text(data, "batch_no")
        outcome: Dict[str, Any] = {}

        connection = self.repository._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")  # 先到先得：后到者阻塞在此
            tx = Tx(connection)
            audit = AuditRecorder(tx)
            reefer = tx.get_reefer_by_code(reefer_code)
            if reefer is None:
                raise NotFound("冷藏箱不存在")

            # 幂等：同一申请单重放，返回原裁决
            existing = tx.get_request_by_key(request_key)
            if existing is not None:
                if existing["reefer_id"] != reefer["id"]:
                    raise ValidationError("request_id已用于其他冷藏箱")
                connection.execute("COMMIT")
                replay_assignment = None
                if existing["winner_assignment_id"]:
                    replay_assignment = tx.get_assignment(existing["winner_assignment_id"])
                return {"outcome": existing["outcome"], "request_id": existing["id"],
                        "idempotent_replay": True, "assignment": replay_assignment,
                        "winner": self._winner_summary(tx, existing)}

            if reefer["state"] == REEFER_LOADED:
                raise ValidationError("冷藏箱已装船，不再安排岸电")

            # 接电前核对：温控不符直接拒绝（不排队、不产生安排）
            try:
                temp = self.rules.check_before_connect(reefer, data)
            except ValidationError as exc:
                audit.record(
                    "reefer", reefer["id"], reefer_code, "connect_rejected", actor.user_id,
                    details={"reason": str(exc)},
                )
                connection.execute("COMMIT")
                raise

            batch = self._resolve_batch(tx, batch_no)

            # 活跃安排存在：后来者只留冲突候选
            active = tx.active_assignment(reefer["id"])
            if active is not None:
                req_id = tx.insert_request(
                    request_key, reefer["id"], actor.user_id, "conflict_candidate", data,
                    winner_assignment_id=active["id"], conflict_assignment_id=active["id"],
                )
                audit.record(
                    "reefer", reefer["id"], reefer_code, "connect_conflict_candidate", actor.user_id,
                    sources=[link("assignment", active["id"], reefer_code, active["state"]),
                             link("request", req_id, request_key, "conflict_candidate")],
                    details={"request_id": request_key, "winner_assignment_id": active["id"],
                             "winner_state": active["state"]},
                )
                connection.execute("COMMIT")
                outcome = {"outcome": "conflict_candidate", "request_id": req_id,
                           "message": "该冷藏箱已有在先接电安排，本申请留作冲突候选",
                           "winner": {"assignment_id": active["id"], "state": active["state"],
                                      "circuit_code": active.get("circuit_code"),
                                      "created_by": active["created_by"]}}
                raise Conflict(outcome["message"], details=outcome)

            # 接电前核对：容量
            usage = tx.circuit_usage()
            circuits = self._circuits_with_remaining(tx, usage)
            chosen = None
            if preferred:
                chosen_circuit = tx.get_circuit(preferred)
                if chosen_circuit is None:
                    raise NotFound("指定回路不存在")
                if chosen_circuit["state"] != CIRCUIT_NORMAL:
                    raise ValidationError("指定回路%s当前不可用(%s)" % (preferred, chosen_circuit["state"]))
                remaining = self.rules.circuit_available_kw(chosen_circuit, usage.get(preferred, 0.0))
                if remaining >= reefer["required_kw"]:
                    chosen = dict(chosen_circuit, remaining_kw=remaining)
                else:
                    chosen = None  # 指定回路不够则排队，不偷偷换回路
            else:
                chosen = self.rules.pick_circuit(circuits, reefer["required_kw"])

            basis_base = {"request_id": request_key, "batch_id": batch["id"], "batch_no": batch["batch_no"],
                          "actor": actor.user_id}
            if chosen is not None:
                basis = self._basis(
                    "connect",
                    circuit_code=chosen["circuit_code"],
                    capacity_kw=chosen["capacity_kw"],
                    used_kw=usage.get(chosen["circuit_code"], 0.0),
                    remaining_kw=chosen["remaining_kw"],
                    **basis_base,
                )
                assignment = tx.insert_assignment({
                    "reefer_id": reefer["id"], "state": ASSN_CONNECTED,
                    "required_kw": reefer["required_kw"],
                    "circuit_code": chosen["circuit_code"], "gap_kw": 0.0,
                    "temp_check": temp, "basis": basis,
                    "request_id": request_key, "batch_id": batch["id"],
                    "created_by": actor.user_id,
                })
                tx.add_batch_connection(batch["id"], reefer["id"], chosen["circuit_code"],
                                        reefer["required_kw"])
                tx.insert_request(request_key, reefer["id"], actor.user_id, "connected", data,
                                  winner_assignment_id=assignment["id"], conflict_assignment_id=None)
                audit.record(
                    "assignment", assignment["id"], reefer_code, "connected", actor.user_id,
                    sources=[link("circuit", None, chosen["circuit_code"], "available"),
                             link("batch", batch["id"], batch["batch_no"], BATCH_OPEN),
                             link("request", None, request_key, "connected")],
                    details={"circuit_code": chosen["circuit_code"],
                             "remaining_kw": chosen["remaining_kw"], "temp_check": temp},
                )
                result = {"outcome": "connected", "assignment": assignment, "batch": batch}
            else:
                gap = self.rules.capacity_gap(circuits, reefer["required_kw"])
                basis = self._basis(
                    "queued_gap",
                    gap_kw=gap,
                    max_remaining_kw=(max((c["remaining_kw"] for c in circuits), default=0.0)),
                    preferred_circuit=preferred,
                    **basis_base,
                )
                assignment = tx.insert_assignment({
                    "reefer_id": reefer["id"], "state": ASSN_QUEUED,
                    "required_kw": reefer["required_kw"],
                    "circuit_code": None, "gap_kw": gap,
                    "temp_check": temp, "basis": basis,
                    "request_id": request_key, "batch_id": batch["id"],
                    "created_by": actor.user_id,
                })
                tx.add_batch_connection(batch["id"], reefer["id"], None, reefer["required_kw"])
                tx.insert_request(request_key, reefer["id"], actor.user_id, "queued", data,
                                  winner_assignment_id=assignment["id"], conflict_assignment_id=None)
                audit.record(
                    "assignment", assignment["id"], reefer_code, "queued", actor.user_id,
                    sources=[link("batch", batch["id"], batch["batch_no"], BATCH_OPEN),
                             link("request", None, request_key, "queued")],
                    details={"gap_kw": gap, "temp_check": temp,
                             "preferred_circuit": preferred},
                )
                result = {"outcome": "queued", "assignment": assignment, "gap_kw": gap,
                          "batch": batch}

            # 同事务立即重排（新建队列后通常无变化，但保证容量一旦可用即先到先得）
            self._allocate(tx, actor.user_id, reason="after_connect:%s" % reefer_code)
            if result["outcome"] == "connected":
                result["assignment"] = tx.get_assignment(result["assignment"]["id"])
            connection.execute("COMMIT")
            return result
        except Conflict:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        except DomainError:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    # ---------- 装船放行 ----------
    def gate_release(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._check_role(actor, ROLE_PLANNER)
        voyage_no = text(payload or {}, "voyage_no")
        codes = payload.get("reefer_codes", [])
        if not isinstance(codes, list) or not codes or any(not isinstance(c, str) or not c.strip() for c in codes):
            raise ValidationError("reefer_codes必须是非空文本列表")
        codes = [c.strip() for c in codes]
        released: List[Dict[str, Any]] = []
        with self.repository.tx() as tx:
            voyage = tx.get_voyage(voyage_no)
            if voyage is None:
                raise NotFound("船期不存在")
            audit = AuditRecorder(tx)
            for code in codes:
                reefer = tx.get_reefer_by_code(code)
                if reefer is None:
                    raise NotFound("冷藏箱不存在:%s" % code)
                active = tx.active_assignment(reefer["id"])
                if active is None:
                    raise ValidationError("冷藏箱%s没有接电/排队安排，不能放行" % code)
                if active["state"] != ASSN_CONNECTED:
                    raise ValidationError("冷藏箱%s当前为%s，必须已接电才能装船" % (code, active["state"]))
                # 已装船保留原依据：安排定格为 loaded（不再活跃），回路容量释放
                tx.finalize_assignment(
                    active["id"], "loaded", reason="gate_release:%s" % voyage_no,
                    basis=self._extend_basis(active.get("basis", {}),
                                             {"gate_release_voyage": voyage_no}),
                )
                tx.mark_reefer_loaded(reefer["id"], voyage_no)
                released.append({"reefer_code": code, "assignment_id": active["id"],
                                 "circuit_code": active.get("circuit_code")})
                audit.record(
                    "assignment", active["id"], code, "loaded", actor.user_id,
                    sources=[link("assignment", active["id"], code, "connected"),
                             link("voyage", None, voyage_no, "gate_release")],
                    details={"voyage_no": voyage_no, "circuit_code": active.get("circuit_code"),
                             "frozen_basis": active.get("basis", {})},
                )
            audit.record(
                "voyage", None, voyage_no, "gate_release", actor.user_id,
                sources=[link("voyage", None, voyage_no, "created")],
                details={"reefer_codes": codes, "count": len(codes)},
            )
            # 装船释放容量后立即重排未装船排队箱
            self._allocate(tx, actor.user_id, reason="gate_release:%s" % voyage_no,
                           trigger=link("voyage", None, voyage_no, "gate_release"))
        return {"voyage_no": voyage_no, "released": released}

    # ---------- 跳闸 ----------
    def trip_circuit(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._check_role(actor, ROLE_ELECTRICIAN)
        circuit_code = text(payload or {}, "circuit_code")
        note = optional_text(payload or {}, "note")
        with self.repository.tx() as tx:
            circuit = self._require_circuit(tx, circuit_code)
            if circuit["state"] == CIRCUIT_TRIPPED and tx.open_trip_for(circuit_code):
                raise ValidationError("该回路已有未恢复的跳闸记录")
            tx.set_circuit_state(circuit_code, CIRCUIT_TRIPPED)
            trip = tx.insert_trip(circuit_code, actor.user_id, note)
            audit = AuditRecorder(tx)
            audit.record(
                "circuit", trip["id"], circuit_code, "tripped", actor.user_id,
                sources=[link("trip", trip["id"], str(trip["id"]), "tripped")],
                details={"note": note, "trip_id": trip["id"]},
            )
            dropped: List[Dict[str, Any]] = []
            # 该回路上未装船的接电箱立即失电：失效并入队重排；已装船不动。
            # 逐箱重新查询活跃安排，避免失效产生新安排后漏处理同回路其他箱。
            while True:
                victim = next(
                    (a for a in tx.list_assignments(active_only=True)
                     if a.get("circuit_code") == circuit_code
                     and a["reefer_state"] != REEFER_LOADED),
                    None,
                )
                if victim is None:
                    break
                self._invalidate_and_requeue(
                    tx, actor.user_id, victim,
                    reason="circuit_trip:%s trip#%s" % (circuit_code, trip["id"]),
                    trigger=link("trip", trip["id"], str(trip["id"]), "tripped"),
                    inherit_circuit=None,
                )
                dropped.append({"reefer_code": victim["reefer_code"], "assignment_id": victim["id"]})
            # 跳闸后不立即补电，等待“从最近完整批次恢复”；此处只刷新缺口
            self._allocate(tx, actor.user_id, reason="circuit_trip:%s" % circuit_code,
                           trigger=link("trip", trip["id"], str(trip["id"]), "tripped"),
                           fill=False)
        return {"trip_id": trip["id"], "circuit_code": circuit_code, "dropped": dropped}

    # ---------- 跳闸恢复（最近完整批次） ----------
    def recover_trip(self, actor: Actor, trip_id: int, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._check_role(actor, ROLE_ELECTRICIAN)
        data = payload or {}
        batch_no = optional_text(data, "batch_no")
        with self.repository.tx() as tx:
            trip = tx.get_trip(trip_id)
            if trip["state"] != "open":
                raise ValidationError("该跳闸记录已恢复")
            circuit = self._require_circuit(tx, trip["circuit_code"])
            audit = AuditRecorder(tx)
            if batch_no:
                batch = self._require_batch_no(tx, batch_no)
                if batch["state"] != BATCH_COMPLETE:
                    raise ValidationError("指定批次不是完整批次")
            else:
                batch = tx.latest_complete_batch(before=trip["tripped_at"])
                if batch is None:
                    # 没有任何完整批次：无法恢复，记录后结束
                    tx.set_circuit_state(circuit["circuit_code"], CIRCUIT_NORMAL)
                    tx.mark_trip_recovered(trip_id, None, "无完整批次可恢复，失电箱全部按待补排队处理")
                    audit.record(
                        "trip", trip_id, str(trip_id), "recovered", actor.user_id,
                        sources=[link("trip", trip_id, str(trip_id), "tripped")],
                        details={"batch": None, "restored": 0, "pending_supply": 0},
                    )
                    self._allocate(tx, actor.user_id, reason="trip_recover_nobatch#%s" % trip_id,
                                   trigger=link("trip", trip_id, str(trip_id), "recovered"))
                    return {"trip_id": trip_id, "batch": None, "restored": [], "pending_supply": []}

            members = tx.list_batch_connections(batch["id"])
            restored: List[Dict[str, Any]] = []
            pending: List[Dict[str, Any]] = []
            batch_reefer_ids = {m["reefer_id"] for m in members}
            # 恢复时刻：跳闸回路回到正常，再按实时容量逐个恢复批次成员
            tx.set_circuit_state(trip["circuit_code"], CIRCUIT_NORMAL)
            usage = tx.circuit_usage()
            circuits = self._circuits_with_remaining(tx, usage)

            for member in members:
                reefer = tx.get_reefer(member["reefer_id"])
                if reefer["state"] == REEFER_LOADED:
                    continue  # 已装船保留原依据，不重接
                active = tx.active_assignment(reefer["id"])
                if active is not None and active["state"] == ASSN_CONNECTED:
                    continue  # 重排中已恢复供电，不重复接
                basis_circuit = member["circuit_code"]
                chosen = None
                target = tx.get_circuit(basis_circuit) if basis_circuit else None
                if target is not None and target["state"] == CIRCUIT_NORMAL:
                    remaining = self.rules.circuit_available_kw(target, usage.get(target["circuit_code"], 0.0))
                    if remaining >= reefer["required_kw"]:
                        chosen = dict(target, remaining_kw=remaining)
                if chosen is None:
                    picked = self.rules.pick_circuit(circuits, reefer["required_kw"])
                    chosen = picked
                parent_id = active["id"] if active is not None else None
                if active is not None:
                    tx.finalize_assignment(active["id"], ASSN_SUPERSEDED,
                                           reason="trip_recovery_batch:%s" % batch["batch_no"])
                if chosen is not None:
                    basis = self._basis(
                        "recovery",
                        recovered_from_trip=trip_id,
                        recovered_from_batch=batch["batch_no"],
                        circuit_code=chosen["circuit_code"],
                        origin_circuit_code=basis_circuit,
                        remaining_kw=chosen["remaining_kw"],
                    )
                    new_assn = tx.insert_assignment({
                        "reefer_id": reefer["id"], "state": ASSN_CONNECTED,
                        "required_kw": reefer["required_kw"],
                        "circuit_code": chosen["circuit_code"], "gap_kw": 0.0,
                        "temp_check": {"restored_from_batch": True},
                        "basis": basis, "request_id": None, "batch_id": batch["id"],
                        "parent_id": parent_id, "created_by": actor.user_id,
                    })
                    usage[chosen["circuit_code"]] = round(
                        usage.get(chosen["circuit_code"], 0.0) + reefer["required_kw"], 2)
                    circuits = self._circuits_with_remaining(tx, usage)
                    restored.append({"reefer_code": reefer["reefer_code"],
                                     "assignment_id": new_assn["id"],
                                     "circuit_code": chosen["circuit_code"]})
                    audit.record(
                        "assignment", new_assn["id"], reefer["reefer_code"], "restored", actor.user_id,
                        sources=[link("trip", trip_id, str(trip_id), "tripped"),
                                 link("batch", batch["id"], batch["batch_no"], BATCH_COMPLETE)]
                        + ([link("assignment", parent_id, reefer["reefer_code"], "superseded")]
                           if parent_id else []),
                        details={"circuit_code": chosen["circuit_code"],
                                 "origin_circuit_code": basis_circuit},
                    )
                else:
                    # 冷藏箱缺回路：按待补处理
                    gap = self.rules.capacity_gap(circuits, reefer["required_kw"])
                    basis = self._basis(
                        "recovery_pending",
                        recovered_from_trip=trip_id,
                        recovered_from_batch=batch["batch_no"],
                        gap_kw=gap,
                        origin_circuit_code=basis_circuit,
                    )
                    new_assn = tx.insert_assignment({
                        "reefer_id": reefer["id"], "state": ASSN_PENDING_SUPPLY,
                        "required_kw": reefer["required_kw"],
                        "circuit_code": None, "gap_kw": gap,
                        "temp_check": {"restored_from_batch": True},
                        "basis": basis, "request_id": None, "batch_id": batch["id"],
                        "parent_id": parent_id, "created_by": actor.user_id,
                    })
                    pending.append({"reefer_code": reefer["reefer_code"],
                                    "assignment_id": new_assn["id"], "gap_kw": gap})
                    audit.record(
                        "assignment", new_assn["id"], reefer["reefer_code"], "pending_supply", actor.user_id,
                        sources=[link("trip", trip_id, str(trip_id), "tripped"),
                                 link("batch", batch["id"], batch["batch_no"], BATCH_COMPLETE)]
                        + ([link("assignment", parent_id, reefer["reefer_code"], "superseded")]
                           if parent_id else []),
                        details={"gap_kw": gap, "origin_circuit_code": basis_circuit},
                    )
            tx.mark_trip_recovered(
                trip_id, batch["id"],
                "从批次%s恢复：恢复%s箱，待补%s箱" % (batch["batch_no"], len(restored), len(pending)),
            )
            audit.record(
                "trip", trip_id, str(trip_id), "recovered", actor.user_id,
                sources=[link("trip", trip_id, str(trip_id), "tripped"),
                         link("batch", batch["id"], batch["batch_no"], BATCH_COMPLETE)],
                details={"batch_no": batch["batch_no"], "restored": len(restored),
                         "pending_supply": len(pending)},
            )
            # 恢复后立即重排其他排队/待补箱（非本批次成员）；
            # 批次成员在上面已按恢复依据处理，不再被普通重排覆盖血缘
            filled = self._allocate(
                tx, actor.user_id, reason="trip_recovery#%s" % trip_id,
                trigger=link("trip", trip_id, str(trip_id), "recovered"),
                exclude_reefer_ids=batch_reefer_ids,
            )
            # 批次中缺回路的待补箱：恢复后若容量可用则直接补电（保留 recovery_pending 血缘）
            still_pending: List[Dict[str, Any]] = []
            for p in pending:
                reefer = tx.get_reefer_by_code(p["reefer_code"])
                assn = tx.active_assignment(reefer["id"])
                now_circuits = self._circuits_with_remaining(tx, tx.circuit_usage())
                chosen = self.rules.pick_circuit(now_circuits, assn["required_kw"])
                if chosen is not None and assn is not None and assn["state"] == ASSN_PENDING_SUPPLY:
                    tx.c.execute(
                        "UPDATE assignments SET state='connected', circuit_code=?, gap_kw=0.0,"
                        " finalized_at=NULL WHERE id=?",
                        (chosen["circuit_code"], assn["id"]),
                    )
                    if assn.get("batch_id"):
                        tx.add_batch_connection(assn["batch_id"], reefer["id"],
                                                chosen["circuit_code"], assn["required_kw"])
                    audit.record(
                        "assignment", assn["id"], p["reefer_code"], "pending_supply_filled", "system",
                        sources=[link("trip", trip_id, str(trip_id), "tripped"),
                                 link("batch", batch["id"], batch["batch_no"], BATCH_COMPLETE)],
                        details={"circuit_code": chosen["circuit_code"], "kept_lineage": True},
                    )
                    restored.append({**p, "circuit_code": chosen["circuit_code"], "via_allocation": True})
                else:
                    still_pending.append(p)
            result = tx.get_trip(trip_id)
        return {"trip_id": trip_id, "batch": batch, "restored": restored,
                "pending_supply": still_pending, "trip": result}

    # ---------- 查询 ----------
    def list_reefers(self, actor: Actor) -> List[Dict[str, Any]]:
        self._actor(actor)
        tx = self.repository.readonly()
        try:
            return tx.list_reefers()
        finally:
            tx.c.close()

    def list_circuits(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        tx = self.repository.readonly()
        try:
            usage = tx.circuit_usage()
            circuits = self._circuits_with_remaining(tx, usage)
            return circuits
        finally:
            tx.c.close()

    def list_voyages(self, actor: Actor) -> List[Dict[str, Any]]:
        self._actor(actor)
        tx = self.repository.readonly()
        try:
            return tx.list_voyages()
        finally:
            tx.c.close()

    def list_batches(self, actor: Actor) -> List[Dict[str, Any]]:
        self._actor(actor)
        tx = self.repository.readonly()
        try:
            return tx.list_batches()
        finally:
            tx.c.close()

    def list_trips(self, actor: Actor, state: Optional[str] = None) -> List[Dict[str, Any]]:
        self._actor(actor)
        tx = self.repository.readonly()
        try:
            return tx.list_trips(state)
        finally:
            tx.c.close()

    def list_assignments(self, actor: Actor, state: Optional[str] = None,
                         active_only: bool = False) -> List[Dict[str, Any]]:
        self._actor(actor)
        tx = self.repository.readonly()
        try:
            return tx.list_assignments(state=state, active_only=active_only)
        finally:
            tx.c.close()

    def list_candidates(self, actor: Actor, reefer_id: Optional[int] = None) -> List[Dict[str, Any]]:
        self._actor(actor)
        tx = self.repository.readonly()
        try:
            return tx.list_requests(reefer_id)
        finally:
            tx.c.close()

    def timeline(self, actor: Actor, entity_type: Optional[str] = None,
                 entity_id: Optional[int] = None, limit: int = 200) -> List[Dict[str, Any]]:
        self._actor(actor)
        return self.repository.audit_timeline(entity_type, entity_id, limit)

    def stats(self, actor: Actor) -> Dict[str, Any]:
        self._actor(actor)
        return self.repository.stats()

    # ---------- 内部辅助 ----------
    @staticmethod
    def _require_circuit(tx: Tx, code: str) -> Dict[str, Any]:
        circuit = tx.get_circuit(code)
        if circuit is None:
            raise NotFound("回路不存在")
        return circuit

    @staticmethod
    def _require_batch_no(tx: Tx, batch_no: str) -> Dict[str, Any]:
        batch = tx.get_batches_by_no(batch_no)
        if batch is None:
            raise NotFound("批次不存在")
        return batch

    def _resolve_batch(self, tx: Tx, batch_no: str) -> Dict[str, Any]:
        if batch_no:
            batch = tx.get_batches_by_no(batch_no)
            if batch is None:
                raise NotFound("批次不存在")
            if batch["state"] != BATCH_OPEN:
                raise ValidationError("批次%s已封存，不能再接新箱" % batch_no)
            return batch
        return tx.open_auto_batch()

    @staticmethod
    def _circuits_with_remaining(tx: Tx, usage: Dict[str, float] = None) -> List[Dict[str, Any]]:
        if usage is None:
            usage = tx.circuit_usage()
        result = []
        for circuit in tx.list_circuits():
            item = dict(circuit)
            item["used_kw"] = usage.get(circuit["circuit_code"], 0.0)
            item["remaining_kw"] = (
                round(circuit["capacity_kw"] - item["used_kw"], 2)
                if circuit["state"] == CIRCUIT_NORMAL else 0.0
            )
            result.append(item)
        return result

    @staticmethod
    def _extend_basis(basis: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(basis or {})
        merged.update(extra)
        return merged

    @staticmethod
    def _winner_summary(tx: Tx, request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        winner_id = request.get("winner_assignment_id")
        if not winner_id:
            return None
        assn = tx.get_assignment(winner_id)
        return {"assignment_id": assn["id"], "state": assn["state"],
                "circuit_code": assn.get("circuit_code"), "created_by": assn["created_by"]}

    def _invalidate_and_requeue(self, tx: Tx, actor_id: str, active: Dict[str, Any],
                                reason: str, trigger: Dict[str, Any],
                                inherit_circuit: Optional[str]) -> None:
        """活跃安排立即失效（保留为依据），同一冷藏箱生成排队重排候选。"""
        audit = AuditRecorder(tx)
        tx.finalize_assignment(active["id"], ASSN_INVALID, reason=reason)
        audit.record(
            "assignment", active["id"], active["reefer_code"], "invalidated", actor_id,
            sources=[trigger] + (
                [link("assignment", active["id"], active["reefer_code"], active["state"])]),
            details={"reason": reason, "previous_state": active["state"],
                     "previous_circuit": active.get("circuit_code")},
        )
        requeue_basis = self._basis(
            "requeue",
            invalid_reason=reason,
            inherited_circuit=inherit_circuit,
            parent_assignment_id=active["id"],
        )
        queued = tx.insert_assignment({
            "reefer_id": active["reefer_id"], "state": ASSN_QUEUED,
            "required_kw": active["required_kw"],
            "circuit_code": None,
            "gap_kw": None,
            "temp_check": active.get("temp_check", {}),
            "basis": requeue_basis,
            "request_id": active.get("request_id"),
            "batch_id": active.get("batch_id"),
            "parent_id": active["id"],
            "created_by": "system",
        })
        audit.record(
            "assignment", queued["id"], active["reefer_code"], "requeued", actor_id,
            sources=[link("assignment", active["id"], active["reefer_code"], "invalid"), trigger],
            details={"reason": reason},
        )

    def _allocate(self, tx: Tx, actor_id: str, reason: str,
                  trigger: Optional[Dict[str, Any]] = None,
                  fill: bool = True,
                  exclude_reefer_ids: Optional[set] = None) -> List[Dict[str, Any]]:
        """排队重排：FIFO 消费 queued/pending_supply，按当前容量尽量补电。

        fill=False 时只刷新缺口、不补电——跳闸后必须先走“最近完整批次恢复”，
        不在跳闸瞬间偷偷把箱重接到别的回路。exclude_reefer_ids 中的箱由调用方
        另行处理（例如跳闸恢复的批次成员），普通重排不覆盖其血缘。
        """
        exclude_reefer_ids = exclude_reefer_ids or set()
        audit = AuditRecorder(tx)
        allocated: List[Dict[str, Any]] = []
        usage = tx.circuit_usage()
        circuits = self._circuits_with_remaining(tx, usage)
        waiting = tx.list_assignments(active_only=True)
        # FIFO：按创建顺序走一遍，先到先得；被跳过者留到下一次触发（新容量/释放/恢复）
        waiting = [a for a in waiting if a["state"] in (ASSN_QUEUED, ASSN_PENDING_SUPPLY)
                   and a["reefer_state"] != REEFER_LOADED
                   and a["reefer_id"] not in exclude_reefer_ids]
        for assn in waiting:
            chosen = self.rules.pick_circuit(circuits, assn["required_kw"]) if fill else None
            if chosen is None:
                gap = self.rules.capacity_gap(circuits, assn["required_kw"])
                if assn.get("gap_kw") != gap:
                    tx.c.execute("UPDATE assignments SET gap_kw=? WHERE id=?", (gap, assn["id"]))
                continue
            tx.finalize_assignment(assn["id"], ASSN_SUPERSEDED, reason="allocation:%s" % reason)
            kind = "requeue_connect" if assn["state"] == ASSN_QUEUED else "pending_supply_filled"
            basis = self._basis(
                kind,
                circuit_code=chosen["circuit_code"],
                remaining_kw=chosen["remaining_kw"],
                parent_assignment_id=assn["id"],
                allocation_reason=reason,
            )
            new_assn = tx.insert_assignment({
                "reefer_id": assn["reefer_id"], "state": ASSN_CONNECTED,
                "required_kw": assn["required_kw"],
                "circuit_code": chosen["circuit_code"], "gap_kw": 0.0,
                "temp_check": assn.get("temp_check", {}),
                "basis": basis, "request_id": assn.get("request_id"),
                "batch_id": assn.get("batch_id"), "parent_id": assn["id"],
                "created_by": "system",
            })
            if assn.get("batch_id"):
                tx.add_batch_connection(assn["batch_id"], assn["reefer_id"],
                                        chosen["circuit_code"], assn["required_kw"])
            usage[chosen["circuit_code"]] = round(
                usage.get(chosen["circuit_code"], 0.0) + assn["required_kw"], 2)
            circuits = self._circuits_with_remaining(tx, usage)
            allocated.append({"reefer_code": assn["reefer_code"],
                              "assignment_id": new_assn["id"],
                              "circuit_code": chosen["circuit_code"]})
            audit.record(
                "assignment", new_assn["id"], assn["reefer_code"], "allocated", "system",
                sources=[link("assignment", assn["id"], assn["reefer_code"], assn["state"]),
                         link("circuit", None, chosen["circuit_code"], "available")]
                + ([trigger] if trigger else []),
                details={"reason": reason, "from_state": assn["state"],
                         "circuit_code": chosen["circuit_code"]},
            )
        return allocated
