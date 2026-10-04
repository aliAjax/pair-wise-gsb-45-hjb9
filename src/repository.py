"""SQLite 表结构与事务访问：冷藏箱、回路、航次、接电、跳闸、排队、批次、冲突候选、审计。"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from .domain import Conflict, NotFound


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(value: str) -> Any:
    return json.loads(value) if value else None


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def tx(self):
        """写事务：BEGIN IMMEDIATE 串行化，保证两人同时接电时先到先得。"""
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS reefers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reefer_no TEXT NOT NULL UNIQUE,
                    required_kw REAL NOT NULL,
                    temp_setpoint_c REAL NOT NULL,
                    temp_tolerance_c REAL NOT NULL,
                    cargo TEXT NOT NULL DEFAULT '',
                    voyage_id INTEGER,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS circuits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    circuit_code TEXT NOT NULL UNIQUE,
                    capacity_kw REAL NOT NULL,
                    temp_min_c REAL NOT NULL,
                    temp_max_c REAL NOT NULL,
                    bay TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'active',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS voyages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vessel TEXT NOT NULL,
                    voyage_no TEXT NOT NULL UNIQUE,
                    sail_hour REAL NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS connections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reefer_id INTEGER NOT NULL,
                    circuit_id INTEGER NOT NULL,
                    required_kw REAL NOT NULL,
                    basis TEXT NOT NULL,
                    batch_id INTEGER,
                    state TEXT NOT NULL DEFAULT 'connected',
                    void_reason TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    voided_at TEXT
                );
                CREATE TABLE IF NOT EXISTS trips (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    circuit_id INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    occurred_at TEXT NOT NULL,
                    recovered_at TEXT,
                    expected_recover_hour REAL
                );
                CREATE TABLE IF NOT EXISTS queues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reefer_id INTEGER NOT NULL UNIQUE,
                    reason TEXT NOT NULL,
                    gap_kw REAL NOT NULL DEFAULT 0,
                    preferred_circuit_id INTEGER,
                    source TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    snapshot TEXT NOT NULL DEFAULT '[]',
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS conflict_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reefer_id INTEGER NOT NULL,
                    actor_id TEXT NOT NULL,
                    requested_circuit_id INTEGER,
                    reason TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'candidate',
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reefers_state ON reefers(state);
                CREATE INDEX IF NOT EXISTS idx_connections_state ON connections(state);
                CREATE INDEX IF NOT EXISTS idx_connections_reefer ON connections(reefer_id, id);
                CREATE INDEX IF NOT EXISTS idx_trips_circuit ON trips(circuit_id, id);
                CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_events(entity_type, entity_id, id);
                """
            )

    # ---------- 审计 ----------
    def emit(self, c: sqlite3.Connection, entity_type: str, entity_id: int, action: str,
             actor_id: str, version: int, details: Dict[str, Any]) -> None:
        c.execute(
            "INSERT INTO audit_events(entity_type,entity_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?,?)",
            (entity_type, entity_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now()),
        )

    def emit_standalone(self, entity_type: str, entity_id: int, action: str, actor_id: str,
                        version: int, details: Dict[str, Any]) -> None:
        with self.tx() as c:
            self.emit(c, entity_type, entity_id, action, actor_id, version, details)

    def audit_timeline(self, entity_type: str, entity_id: int) -> List[Dict[str, Any]]:
        with self._connect() as c:
            rows = c.execute(
                "SELECT * FROM audit_events WHERE entity_type=? AND entity_id=? ORDER BY id",
                (entity_type, entity_id),
            ).fetchall()
        return [self._audit_row(r) for r in rows]

    def all_events(self, limit: int = 200, entity_type: str = None) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self._connect() as c:
            if entity_type:
                rows = c.execute(
                    "SELECT * FROM audit_events WHERE entity_type=? ORDER BY id DESC LIMIT ?",
                    (entity_type, limit),
                ).fetchall()
            else:
                rows = c.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._audit_row(r) for r in rows]

    @staticmethod
    def _audit_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["details"] = _loads(item["details"])
        return item

    # ---------- 冷藏箱 ----------
    def create_reefer(self, c: sqlite3.Connection, data: Dict[str, Any]) -> Dict[str, Any]:
        ts = now()
        try:
            cur = c.execute(
                "INSERT INTO reefers(reefer_no,required_kw,temp_setpoint_c,temp_tolerance_c,cargo,voyage_id,state,version,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (data["reefer_no"], data["required_kw"], data["temp_setpoint_c"], data["temp_tolerance_c"],
                 data["cargo"], data["voyage_id"], "waiting", 1, ts, ts),
            )
        except sqlite3.IntegrityError as exc:
            raise Conflict("冷藏箱编号已存在") from exc
        row = c.execute("SELECT * FROM reefers WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)

    def get_reefer(self, reefer_id: int, c: sqlite3.Connection = None) -> Dict[str, Any]:
        return self._get(c, "reefers", reefer_id, "冷藏箱不存在")

    def find_reefer_no(self, c: sqlite3.Connection, reefer_no: str) -> Optional[Dict[str, Any]]:
        row = c.execute("SELECT * FROM reefers WHERE reefer_no=?", (reefer_no,)).fetchone()
        return dict(row) if row else None

    def list_reefers(self, state: str = None, limit: int = 200) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self._connect() as c:
            if state:
                rows = c.execute("SELECT * FROM reefers WHERE state=? ORDER BY id LIMIT ?", (state, limit)).fetchall()
            else:
                rows = c.execute("SELECT * FROM reefers ORDER BY id LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def save_reefer(self, c: sqlite3.Connection, reefer: Dict[str, Any], fields: Iterable[str]) -> Dict[str, Any]:
        assignments = {key: reefer[key] for key in fields}
        assignments["version"] = int(reefer["version"]) + 1
        assignments["updated_at"] = now()
        sql = "UPDATE reefers SET " + ",".join("%s=?" % k for k in assignments) + " WHERE id=?"
        c.execute(sql, (*assignments.values(), reefer["id"]))
        row = c.execute("SELECT * FROM reefers WHERE id=?", (reefer["id"],)).fetchone()
        result = dict(row)
        reefer.clear()
        reefer.update(result)
        return result

    # ---------- 回路 ----------
    def create_circuit(self, c: sqlite3.Connection, data: Dict[str, Any]) -> Dict[str, Any]:
        ts = now()
        try:
            cur = c.execute(
                "INSERT INTO circuits(circuit_code,capacity_kw,temp_min_c,temp_max_c,bay,state,version,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (data["circuit_code"], data["capacity_kw"], data["temp_min_c"], data["temp_max_c"],
                 data["bay"], "active", 1, ts, ts),
            )
        except sqlite3.IntegrityError as exc:
            raise Conflict("回路编号已存在") from exc
        return dict(c.execute("SELECT * FROM circuits WHERE id=?", (cur.lastrowid,)).fetchone())

    def get_circuit(self, circuit_id: int, c: sqlite3.Connection = None) -> Dict[str, Any]:
        return self._get(c, "circuits", circuit_id, "回路不存在")

    def list_circuits(self) -> List[Dict[str, Any]]:
        with self._connect() as c:
            rows = c.execute("SELECT * FROM circuits ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def save_circuit(self, c: sqlite3.Connection, circuit: Dict[str, Any], fields: Iterable[str]) -> Dict[str, Any]:
        assignments = {key: circuit[key] for key in fields}
        assignments["version"] = int(circuit["version"]) + 1
        assignments["updated_at"] = now()
        sql = "UPDATE circuits SET " + ",".join("%s=?" % k for k in assignments) + " WHERE id=?"
        c.execute(sql, (*assignments.values(), circuit["id"]))
        result = dict(c.execute("SELECT * FROM circuits WHERE id=?", (circuit["id"],)).fetchone())
        circuit.clear()
        circuit.update(result)
        return result

    # ---------- 航次 ----------
    def create_voyage(self, c: sqlite3.Connection, data: Dict[str, Any]) -> Dict[str, Any]:
        ts = now()
        try:
            cur = c.execute(
                "INSERT INTO voyages(vessel,voyage_no,sail_hour,version,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (data["vessel"], data["voyage_no"], data["sail_hour"], 1, ts, ts),
            )
        except sqlite3.IntegrityError as exc:
            raise Conflict("航次号已存在") from exc
        return dict(c.execute("SELECT * FROM voyages WHERE id=?", (cur.lastrowid,)).fetchone())

    def get_voyage(self, voyage_id: int, c: sqlite3.Connection = None) -> Dict[str, Any]:
        return self._get(c, "voyages", voyage_id, "航次不存在")

    def list_voyages(self) -> List[Dict[str, Any]]:
        with self._connect() as c:
            rows = c.execute("SELECT * FROM voyages ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def save_voyage(self, c: sqlite3.Connection, voyage: Dict[str, Any], fields: Iterable[str]) -> Dict[str, Any]:
        assignments = {key: voyage[key] for key in fields}
        assignments["version"] = int(voyage["version"]) + 1
        assignments["updated_at"] = now()
        sql = "UPDATE voyages SET " + ",".join("%s=?" % k for k in assignments) + " WHERE id=?"
        c.execute(sql, (*assignments.values(), voyage["id"]))
        result = dict(c.execute("SELECT * FROM voyages WHERE id=?", (voyage["id"],)).fetchone())
        voyage.clear()
        voyage.update(result)
        return result

    # ---------- 接电记录 ----------
    def add_connection(self, c: sqlite3.Connection, reefer_id: int, circuit_id: int, required_kw: float,
                       basis: str, source: str, batch_id: int = None) -> Dict[str, Any]:
        cur = c.execute(
            "INSERT INTO connections(reefer_id,circuit_id,required_kw,basis,batch_id,state,source,created_at)"
            " VALUES(?,?,?,?,?,'connected',?,?)",
            (reefer_id, circuit_id, required_kw, basis, batch_id, source, now()),
        )
        return dict(c.execute("SELECT * FROM connections WHERE id=?", (cur.lastrowid,)).fetchone())

    def active_connection(self, c: sqlite3.Connection, reefer_id: int) -> Optional[Dict[str, Any]]:
        row = c.execute(
            "SELECT * FROM connections WHERE reefer_id=? AND state='connected' ORDER BY id DESC LIMIT 1",
            (reefer_id,),
        ).fetchone()
        return dict(row) if row else None

    def active_connections(self, c: sqlite3.Connection, circuit_id: int = None) -> List[Dict[str, Any]]:
        if circuit_id is not None:
            rows = c.execute(
                "SELECT * FROM connections WHERE state='connected' AND circuit_id=? ORDER BY id",
                (circuit_id,),
            ).fetchall()
        else:
            rows = c.execute("SELECT * FROM connections WHERE state='connected' ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def void_connections(self, c: sqlite3.Connection, connection_ids: Iterable[int], reason: str) -> None:
        ts = now()
        c.executemany(
            "UPDATE connections SET state='voided',void_reason=?,voided_at=? WHERE id=? AND state='connected'",
            [(reason, ts, cid) for cid in connection_ids],
        )

    def freeze_connection(self, c: sqlite3.Connection, connection_id: int) -> None:
        """装船放行：连接依据冻结保留，但不再占用岸电容量。"""
        c.execute(
            "UPDATE connections SET state='loaded_frozen',voided_at=? WHERE id=? AND state='connected'",
            (now(), connection_id),
        )

    def effective_connections(self, c: sqlite3.Connection, circuit_id: int = None) -> List[Dict[str, Any]]:
        """跳闸/检修需同时遍历在用与已装船冻结的依据。"""
        if circuit_id is not None:
            rows = c.execute(
                "SELECT * FROM connections WHERE state IN ('connected','loaded_frozen') AND circuit_id=? ORDER BY id",
                (circuit_id,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM connections WHERE state IN ('connected','loaded_frozen') ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def list_connections(self, reefer_id: int = None) -> List[Dict[str, Any]]:
        with self._connect() as c:
            if reefer_id is not None:
                rows = c.execute("SELECT * FROM connections WHERE reefer_id=? ORDER BY id", (reefer_id,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM connections ORDER BY id DESC LIMIT 500").fetchall()
        return [dict(r) for r in rows]

    # ---------- 跳闸 ----------
    def add_trip(self, c: sqlite3.Connection, circuit_id: int, reason: str,
                 expected_recover_hour: float = None) -> Dict[str, Any]:
        cur = c.execute(
            "INSERT INTO trips(circuit_id,reason,occurred_at,expected_recover_hour) VALUES(?,?,?,?)",
            (circuit_id, reason, now(), expected_recover_hour),
        )
        return dict(c.execute("SELECT * FROM trips WHERE id=?", (cur.lastrowid,)).fetchone())

    def close_trip(self, c: sqlite3.Connection, trip_id: int) -> None:
        c.execute("UPDATE trips SET recovered_at=? WHERE id=? AND recovered_at IS NULL", (now(), trip_id))

    def list_trips(self, circuit_id: int = None) -> List[Dict[str, Any]]:
        with self._connect() as c:
            if circuit_id is not None:
                rows = c.execute("SELECT * FROM trips WHERE circuit_id=? ORDER BY id", (circuit_id,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM trips ORDER BY id DESC LIMIT 200").fetchall()
        return [dict(r) for r in rows]

    # ---------- 排队 ----------
    def enqueue(self, c: sqlite3.Connection, reefer_id: int, reason: str, gap_kw: float,
                source: str, preferred_circuit_id: int = None) -> Dict[str, Any]:
        c.execute("DELETE FROM queues WHERE reefer_id=?", (reefer_id,))
        cur = c.execute(
            "INSERT INTO queues(reefer_id,reason,gap_kw,preferred_circuit_id,source,created_at) VALUES(?,?,?,?,?,?)",
            (reefer_id, reason, gap_kw, preferred_circuit_id, source, now()),
        )
        return dict(c.execute("SELECT * FROM queues WHERE id=?", (cur.lastrowid,)).fetchone())

    def dequeue(self, c: sqlite3.Connection, reefer_id: int) -> None:
        c.execute("DELETE FROM queues WHERE reefer_id=?", (reefer_id,))

    def list_queue(self) -> List[Dict[str, Any]]:
        with self._connect() as c:
            rows = c.execute(
                "SELECT * FROM queues ORDER BY CASE reason WHEN 'recovery' THEN 0 WHEN 'trip' THEN 1 ELSE 2 END, id"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 批次 ----------
    def create_batch(self, c: sqlite3.Connection, batch_no: str, size: int) -> Dict[str, Any]:
        cur = c.execute(
            "INSERT INTO batches(batch_no,state,size,snapshot,created_at) VALUES(?,?, 'running','[]',?)",
            (batch_no, size, now()),
        )
        return dict(c.execute("SELECT * FROM batches WHERE id=?", (cur.lastrowid,)).fetchone())

    def finish_batch(self, c: sqlite3.Connection, batch_id: int, state: str,
                     snapshot: List[Dict[str, Any]], note: str = "") -> None:
        c.execute(
            "UPDATE batches SET state=?,snapshot=?,note=?,finished_at=? WHERE id=?",
            (state, json.dumps(snapshot, ensure_ascii=False, sort_keys=True), note, now(), batch_id),
        )

    def get_batch(self, batch_id: int, c: sqlite3.Connection = None) -> Dict[str, Any]:
        row = self._query(c, "SELECT * FROM batches WHERE id=?", (batch_id,))
        if row is None:
            raise NotFound("批次不存在")
        item = dict(row)
        item["snapshot"] = _loads(item["snapshot"])
        return item

    def last_complete_batch(self, c: sqlite3.Connection, before_batch_id: int = None) -> Optional[Dict[str, Any]]:
        sql = "SELECT * FROM batches WHERE state='complete'"
        params: List[Any] = []
        if before_batch_id is not None:
            sql += " AND id<?"
            params.append(before_batch_id)
        sql += " ORDER BY id DESC LIMIT 1"
        row = c.execute(sql, params).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["snapshot"] = _loads(item["snapshot"])
        return item

    def list_batches(self) -> List[Dict[str, Any]]:
        with self._connect() as c:
            rows = c.execute("SELECT * FROM batches ORDER BY id DESC LIMIT 100").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["snapshot"] = _loads(item["snapshot"])
            result.append(item)
        return result

    # ---------- 冲突候选 ----------
    def add_conflict_candidate(self, c: sqlite3.Connection, reefer_id: int, actor_id: str,
                               reason: str, payload: Dict[str, Any], requested_circuit_id: int = None) -> Dict[str, Any]:
        cur = c.execute(
            "INSERT INTO conflict_candidates(reefer_id,actor_id,requested_circuit_id,reason,state,payload,created_at)"
            " VALUES(?,?,?,?,'candidate',?,?)",
            (reefer_id, actor_id, requested_circuit_id, reason,
             json.dumps(payload, ensure_ascii=False, sort_keys=True), now()),
        )
        return dict(c.execute("SELECT * FROM conflict_candidates WHERE id=?", (cur.lastrowid,)).fetchone())

    def list_conflict_candidates(self, state: str = None) -> List[Dict[str, Any]]:
        with self._connect() as c:
            if state:
                rows = c.execute("SELECT * FROM conflict_candidates WHERE state=? ORDER BY id DESC", (state,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM conflict_candidates ORDER BY id DESC LIMIT 200").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = _loads(item["payload"])
            result.append(item)
        return result

    def resolve_candidate(self, c: sqlite3.Connection, candidate_id: int, state: str) -> Dict[str, Any]:
        row = c.execute("SELECT * FROM conflict_candidates WHERE id=?", (candidate_id,)).fetchone()
        if row is None:
            raise NotFound("冲突候选不存在")
        if row["state"] != "candidate":
            raise Conflict("该冲突候选已处理")
        c.execute("UPDATE conflict_candidates SET state=?,resolved_at=? WHERE id=?", (state, now(), candidate_id))
        item = dict(c.execute("SELECT * FROM conflict_candidates WHERE id=?", (candidate_id,)).fetchone())
        item["payload"] = _loads(item["payload"])
        return item

    # ---------- 通用 ----------
    def _get(self, c: Optional[sqlite3.Connection], table: str, entity_id: int, message: str) -> Dict[str, Any]:
        row = self._query(c, "SELECT * FROM %s WHERE id=?" % table, (entity_id,))
        if row is None:
            raise NotFound(message)
        return dict(row)

    def _query(self, c: Optional[sqlite3.Connection], sql: str, params: tuple):
        if c is not None:
            return c.execute(sql, params).fetchone()
        with self._connect() as connection:
            return connection.execute(sql, params).fetchone()

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
