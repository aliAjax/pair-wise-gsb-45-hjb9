"""SQLite 表结构与事务访问。

台账实体：冷藏箱、供电回路、船期、接电批次、接电安排、批次成员、
接电申请（含冲突候选）、跳闸记录、全局审计事件。

所有写操作由 service 在单个 BEGIN IMMEDIATE 事务中完成，
因此并发接电申请按到达顺序串行裁决，只认先到的一份。
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from .domain import Conflict, NotFound


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


JSON_ARGS = {"ensure_ascii": False, "sort_keys": True}


def dumps(data: Any) -> str:
    return json.dumps(data, **JSON_ARGS)


class Tx:
    """绑定单个连接的底层读写，供 service 在一个事务内编排。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.c = connection

    # ---------- 通用 ----------
    @staticmethod
    def _row(row: sqlite3.Row, json_keys: List[str] = ()) -> Dict[str, Any]:
        item = dict(row)
        for key in json_keys:
            if item.get(key) is not None:
                item[key] = json.loads(item[key])
        return item

    # ---------- 冷藏箱 ----------
    def list_reefers(self) -> List[Dict[str, Any]]:
        rows = self.c.execute("SELECT * FROM reefers ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def get_reefer(self, reefer_id: int) -> Dict[str, Any]:
        row = self.c.execute("SELECT * FROM reefers WHERE id=?", (reefer_id,)).fetchone()
        if row is None:
            raise NotFound("冷藏箱不存在")
        return dict(row)

    def get_reefer_by_code(self, code: str) -> Optional[Dict[str, Any]]:
        row = self.c.execute("SELECT * FROM reefers WHERE reefer_code=?", (code,)).fetchone()
        return dict(row) if row else None

    def insert_reefer(self, data: Dict[str, Any]) -> Dict[str, Any]:
        ts = now()
        cur = self.c.execute(
            "INSERT INTO reefers(reefer_code,voyage_no,required_kw,set_temp_c,state,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (data["reefer_code"], data.get("voyage_no", ""), data["required_kw"],
             data["set_temp_c"], "registered", ts),
        )
        return self.get_reefer(int(cur.lastrowid))

    def mark_reefer_loaded(self, reefer_id: int, voyage_no: str) -> None:
        self.c.execute(
            "UPDATE reefers SET state=?, voyage_no=?, loaded_voyage_no=?, loaded_at=? WHERE id=?",
            ("loaded", voyage_no, voyage_no, now(), reefer_id),
        )

    def set_reefer_voyage(self, reefer_id: int, voyage_no: str) -> None:
        self.c.execute("UPDATE reefers SET voyage_no=? WHERE id=?", (voyage_no, reefer_id))

    # ---------- 供电回路 ----------
    def list_circuits(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.c.execute("SELECT * FROM circuits ORDER BY circuit_code").fetchall()]

    def get_circuit(self, code: str) -> Optional[Dict[str, Any]]:
        row = self.c.execute("SELECT * FROM circuits WHERE circuit_code=?", (code,)).fetchone()
        return dict(row) if row else None

    def insert_circuit(self, data: Dict[str, Any]) -> Dict[str, Any]:
        self.c.execute(
            "INSERT INTO circuits(circuit_code,capacity_kw,location,state,created_at)"
            " VALUES(?,?,?,?,?)",
            (data["circuit_code"], data["capacity_kw"], data.get("location", ""), data["state"], now()),
        )
        row = self.get_circuit(data["circuit_code"])
        assert row is not None
        return row

    def set_circuit_state(self, code: str, state: str) -> None:
        self.c.execute("UPDATE circuits SET state=? WHERE circuit_code=?", (state, code))

    def circuit_usage(self) -> Dict[str, float]:
        """各回路上已接电且未装船的负荷（已装船箱释放岸电容量）。"""
        rows = self.c.execute(
            "SELECT a.circuit_code AS code, COALESCE(SUM(a.required_kw),0) AS used"
            " FROM assignments a JOIN reefers r ON r.id=a.reefer_id"
            " WHERE a.state='connected' AND r.state!='loaded' AND a.circuit_code IS NOT NULL"
            " GROUP BY a.circuit_code"
        ).fetchall()
        return {r["code"]: round(float(r["used"]), 2) for r in rows}

    # ---------- 船期 ----------
    def list_voyages(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.c.execute("SELECT * FROM voyages ORDER BY etd").fetchall()]

    def get_voyage(self, voyage_no: str) -> Optional[Dict[str, Any]]:
        row = self.c.execute("SELECT * FROM voyages WHERE voyage_no=?", (voyage_no,)).fetchone()
        return dict(row) if row else None

    def insert_voyage(self, data: Dict[str, Any]) -> Dict[str, Any]:
        ts = now()
        self.c.execute(
            "INSERT INTO voyages(voyage_no,vessel,etd,created_at,updated_at) VALUES(?,?,?,?,?)",
            (data["voyage_no"], data["vessel"], data["etd"], ts, ts),
        )
        row = self.get_voyage(data["voyage_no"])
        assert row is not None
        return row

    def update_voyage_etd(self, voyage_no: str, etd: str) -> Dict[str, Any]:
        self.c.execute(
            "UPDATE voyages SET etd=?, updated_at=? WHERE voyage_no=?", (etd, now(), voyage_no)
        )
        row = self.get_voyage(voyage_no)
        assert row is not None
        return row

    # ---------- 批次 ----------
    def list_batches(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.c.execute("SELECT * FROM batches ORDER BY id DESC").fetchall()]

    def get_batches_by_no(self, batch_no: str) -> Optional[Dict[str, Any]]:
        row = self.c.execute("SELECT * FROM batches WHERE batch_no=?", (batch_no,)).fetchone()
        return dict(row) if row else None

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        row = self.c.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFound("批次不存在")
        return dict(row)

    def insert_batch(self, batch_no: str, note: str) -> Dict[str, Any]:
        cur = self.c.execute(
            "INSERT INTO batches(batch_no,state,note,created_at,completed_at) VALUES(?,?,?,?,?)",
            (batch_no, "open", note, now(), None),
        )
        return self.get_batch(int(cur.lastrowid))

    def open_auto_batch(self) -> Dict[str, Any]:
        existing = self.get_batches_by_no("AUTO")
        if existing and existing["state"] == "open":
            return existing
        cur = self.c.execute(
            "INSERT INTO batches(batch_no,state,note,created_at,completed_at) VALUES(?,?,?,?,?)",
            ("AUTO", "open", "未指定批次的接电申请自动归入", now(), None),
        )
        return self.get_batch(int(cur.lastrowid))

    def complete_batch(self, batch_id: int) -> Dict[str, Any]:
        self.c.execute(
            "UPDATE batches SET state=?, completed_at=? WHERE id=?", ("complete", now(), batch_id)
        )
        return self.get_batch(batch_id)

    def latest_complete_batch(self, before: str = None) -> Optional[Dict[str, Any]]:
        if before:
            row = self.c.execute(
                "SELECT * FROM batches WHERE state='complete' AND created_at<=? ORDER BY id DESC LIMIT 1",
                (before,),
            ).fetchone()
        else:
            row = self.c.execute(
                "SELECT * FROM batches WHERE state='complete' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def add_batch_connection(self, batch_id: int, reefer_id: int, circuit_code: Optional[str],
                             required_kw: float) -> None:
        self.c.execute(
            "INSERT OR IGNORE INTO batch_connections(batch_id,reefer_id,circuit_code,required_kw)"
            " VALUES(?,?,?,?)",
            (batch_id, reefer_id, circuit_code, required_kw),
        )

    def list_batch_connections(self, batch_id: int) -> List[Dict[str, Any]]:
        rows = self.c.execute(
            "SELECT bc.*, r.reefer_code, r.state AS reefer_state FROM batch_connections bc"
            " JOIN reefers r ON r.id=bc.reefer_id WHERE bc.batch_id=? ORDER BY bc.rowid",
            (batch_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 接电安排 ----------
    def insert_assignment(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        cur = self.c.execute(
            "INSERT INTO assignments(reefer_id,state,required_kw,circuit_code,gap_kw,temp_check,"
            "basis,request_id,batch_id,parent_id,invalid_reason,created_by,created_at,finalized_at)"
            " VALUES(:reefer_id,:state,:required_kw,:circuit_code,:gap_kw,:temp_check,:basis,"
            ":request_id,:batch_id,:parent_id,:invalid_reason,:created_by,:created_at,NULL)",
            {
                "reefer_id": fields["reefer_id"],
                "state": fields["state"],
                "required_kw": fields["required_kw"],
                "circuit_code": fields.get("circuit_code"),
                "gap_kw": fields.get("gap_kw"),
                "temp_check": dumps(fields.get("temp_check", {})),
                "basis": dumps(fields.get("basis", {})),
                "request_id": fields.get("request_id"),
                "batch_id": fields.get("batch_id"),
                "parent_id": fields.get("parent_id"),
                "invalid_reason": fields.get("invalid_reason", ""),
                "created_by": fields["created_by"],
                "created_at": now(),
            },
        )
        return self.get_assignment(int(cur.lastrowid))

    def get_assignment(self, assignment_id: int) -> Dict[str, Any]:
        row = self.c.execute(
            "SELECT a.*, r.reefer_code AS reefer_code, r.voyage_no AS voyage_no,"
            " r.state AS reefer_state FROM assignments a JOIN reefers r ON r.id=a.reefer_id"
            " WHERE a.id=?",
            (assignment_id,),
        ).fetchone()
        if row is None:
            raise NotFound("接电安排不存在")
        return self._row(row, ("temp_check", "basis"))

    def active_assignment(self, reefer_id: int) -> Optional[Dict[str, Any]]:
        row = self.c.execute(
            "SELECT a.*, r.reefer_code AS reefer_code, r.voyage_no AS voyage_no,"
            " r.state AS reefer_state FROM assignments a JOIN reefers r ON r.id=a.reefer_id"
            " WHERE a.reefer_id=? AND a.state IN ('queued','connected','pending_supply')",
            (reefer_id,),
        ).fetchone()
        return self._row(row, ("temp_check", "basis")) if row else None

    def list_assignments(self, state: Optional[str] = None, active_only: bool = False) -> List[Dict[str, Any]]:
        sql = (
            "SELECT a.*, r.reefer_code AS reefer_code, r.voyage_no AS voyage_no,"
            " r.state AS reefer_state FROM assignments a JOIN reefers r ON r.id=a.reefer_id"
        )
        if state:
            rows = self.c.execute(sql + " WHERE a.state=? ORDER BY a.id DESC", (state,)).fetchall()
        elif active_only:
            rows = self.c.execute(
                sql + " WHERE a.state IN ('queued','connected','pending_supply') ORDER BY a.id"
            ).fetchall()
        else:
            rows = self.c.execute(sql + " ORDER BY a.id DESC").fetchall()
        return [self._row(r, ("temp_check", "basis")) for r in rows]

    def finalize_assignment(self, assignment_id: int, state: str, reason: str = "",
                            circuit_code: Optional[str] = None, gap_kw: Optional[float] = None,
                            basis: Optional[Dict[str, Any]] = None) -> None:
        if basis is not None:
            self.c.execute(
                "UPDATE assignments SET state=?, invalid_reason=?, finalized_at=?, circuit_code=?,"
                " gap_kw=?, basis=? WHERE id=?",
                (state, reason, now(), circuit_code, gap_kw, dumps(basis), assignment_id),
            )
        else:
            self.c.execute(
                "UPDATE assignments SET state=?, invalid_reason=?, finalized_at=? WHERE id=?",
                (state, reason, now(), assignment_id),
            )

    # ---------- 接电申请 / 冲突候选 ----------
    def insert_request(self, request_key: str, reefer_id: int, actor_id: str,
                       outcome: str, data: Dict[str, Any], winner_assignment_id: Optional[int],
                       conflict_assignment_id: Optional[int]) -> int:
        cur = self.c.execute(
            "INSERT INTO connect_requests(request_key,reefer_id,actor_id,outcome,"
            "winner_assignment_id,conflict_assignment_id,data,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (request_key, reefer_id, actor_id, outcome, winner_assignment_id,
             conflict_assignment_id, dumps(data), now()),
        )
        return int(cur.lastrowid)

    def get_request_by_key(self, request_key: str) -> Optional[Dict[str, Any]]:
        row = self.c.execute(
            "SELECT * FROM connect_requests WHERE request_key=?", (request_key,)
        ).fetchone()
        return self._row(row, ("data",)) if row else None

    def list_requests(self, reefer_id: Optional[int] = None) -> List[Dict[str, Any]]:
        if reefer_id is None:
            rows = self.c.execute(
                "SELECT q.*, r.reefer_code AS reefer_code FROM connect_requests q"
                " JOIN reefers r ON r.id=q.reefer_id ORDER BY q.id"
            ).fetchall()
        else:
            rows = self.c.execute(
                "SELECT q.*, r.reefer_code AS reefer_code FROM connect_requests q"
                " JOIN reefers r ON r.id=q.reefer_id WHERE q.reefer_id=? ORDER BY q.id",
                (reefer_id,),
            ).fetchall()
        return [self._row(r, ("data",)) for r in rows]

    # ---------- 跳闸 ----------
    def insert_trip(self, circuit_code: str, actor_id: str, note: str) -> Dict[str, Any]:
        cur = self.c.execute(
            "INSERT INTO trips(circuit_code,tripped_at,recovered_at,recovered_batch_id,state,note,actor_id)"
            " VALUES(?,?,NULL,NULL,?,?,?)",
            (circuit_code, now(), "open", note, actor_id),
        )
        return self.get_trip(int(cur.lastrowid))

    def get_trip(self, trip_id: int) -> Dict[str, Any]:
        row = self.c.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
        if row is None:
            raise NotFound("跳闸记录不存在")
        return dict(row)

    def open_trip_for(self, circuit_code: str) -> Optional[Dict[str, Any]]:
        row = self.c.execute(
            "SELECT * FROM trips WHERE circuit_code=? AND state='open' ORDER BY id DESC LIMIT 1",
            (circuit_code,),
        ).fetchone()
        return dict(row) if row else None

    def list_trips(self, state: Optional[str] = None) -> List[Dict[str, Any]]:
        if state:
            rows = self.c.execute("SELECT * FROM trips WHERE state=? ORDER BY id DESC", (state,)).fetchall()
        else:
            rows = self.c.execute("SELECT * FROM trips ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

    def mark_trip_recovered(self, trip_id: int, batch_id: Optional[int], note: str) -> None:
        self.c.execute(
            "UPDATE trips SET state=?, recovered_at=?, recovered_batch_id=?, note=? WHERE id=?",
            ("recovered", now(), batch_id, note, trip_id),
        )

    # ---------- 审计 ----------
    def add_event(self, entity_type: str, entity_id: Optional[int], ref: str, action: str,
                  actor_id: str, sources: List[Dict[str, Any]], details: Dict[str, Any]) -> int:
        cur = self.c.execute(
            "INSERT INTO audit_events(entity_type,entity_id,ref,action,actor_id,sources,details,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (entity_type, entity_id, ref, action, actor_id, dumps(sources), dumps(details), now()),
        )
        return int(cur.lastrowid)

    def audit_timeline(self, entity_type: Optional[str] = None, entity_id: Optional[int] = None,
                       limit: int = 200) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        sql = "SELECT * FROM audit_events"
        clauses = []
        params: List[Any] = []
        if entity_type:
            clauses.append("entity_type=?")
            params.append(entity_type)
        if entity_id is not None:
            clauses.append("entity_id=?")
            params.append(entity_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self.c.execute(sql, params).fetchall()
        return [self._row(r, ("sources", "details")) for r in rows]


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None,
                                     check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS reefers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reefer_code TEXT NOT NULL UNIQUE,
                    voyage_no TEXT NOT NULL DEFAULT '',
                    required_kw REAL NOT NULL,
                    set_temp_c REAL NOT NULL,
                    state TEXT NOT NULL DEFAULT 'registered',
                    loaded_at TEXT,
                    loaded_voyage_no TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS circuits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    circuit_code TEXT NOT NULL UNIQUE,
                    capacity_kw REAL NOT NULL,
                    location TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'normal',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS voyages (
                    voyage_no TEXT PRIMARY KEY,
                    vessel TEXT NOT NULL,
                    etd TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL DEFAULT 'open',
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reefer_id INTEGER NOT NULL REFERENCES reefers(id),
                    state TEXT NOT NULL,
                    required_kw REAL NOT NULL,
                    circuit_code TEXT,
                    gap_kw REAL,
                    temp_check TEXT NOT NULL DEFAULT '{}',
                    basis TEXT NOT NULL DEFAULT '{}',
                    request_id TEXT,
                    batch_id INTEGER REFERENCES batches(id),
                    parent_id INTEGER REFERENCES assignments(id),
                    invalid_reason TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    finalized_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_assignment_active
                    ON assignments(reefer_id)
                    WHERE state IN ('queued','connected','pending_supply');
                CREATE INDEX IF NOT EXISTS idx_assignment_state ON assignments(state);
                CREATE TABLE IF NOT EXISTS batch_connections (
                    batch_id INTEGER NOT NULL REFERENCES batches(id),
                    reefer_id INTEGER NOT NULL REFERENCES reefers(id),
                    circuit_code TEXT,
                    required_kw REAL NOT NULL,
                    PRIMARY KEY (batch_id, reefer_id)
                );
                CREATE TABLE IF NOT EXISTS connect_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_key TEXT NOT NULL UNIQUE,
                    reefer_id INTEGER NOT NULL REFERENCES reefers(id),
                    actor_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    winner_assignment_id INTEGER,
                    conflict_assignment_id INTEGER,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_request_reefer ON connect_requests(reefer_id, id);
                CREATE TABLE IF NOT EXISTS trips (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    circuit_code TEXT NOT NULL,
                    tripped_at TEXT NOT NULL,
                    recovered_at TEXT,
                    recovered_batch_id INTEGER REFERENCES batches(id),
                    state TEXT NOT NULL DEFAULT 'open',
                    note TEXT NOT NULL DEFAULT '',
                    actor_id TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_trip_circuit ON trips(circuit_code, id);
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER,
                    ref TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    sources TEXT NOT NULL DEFAULT '[]',
                    details TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_events(entity_type, entity_id, id);
                CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_events(id);
                """
            )

    @contextmanager
    def tx(self) -> Iterator[Tx]:
        """写事务：BEGIN IMMEDIATE 立即取锁，提交/回滚由上下文管理。"""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield Tx(connection)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def readonly(self) -> Tx:
        """只读句柄。SQLite 下读事务会持有快照，用完应立即 close。"""
        return Tx(self._connect())

    # ---------- 只读便捷方法 ----------
    def audit_timeline(self, entity_type: Optional[str] = None, entity_id: Optional[int] = None,
                       limit: int = 200) -> List[Dict[str, Any]]:
        tx = self.readonly()
        try:
            return tx.audit_timeline(entity_type, entity_id, limit)
        finally:
            tx.c.close()

    def stats(self) -> Dict[str, Any]:
        tx = self.readonly()
        try:
            def count(table: str, where: str = "", *params: Any) -> int:
                row = tx.c.execute("SELECT COUNT(*) AS n FROM %s %s" % (table, where), params).fetchone()
                return int(row["n"])
            return {
                "reefers": count("reefers"),
                "reefers_loaded": count("reefers", "WHERE state='loaded'"),
                "circuits": count("circuits"),
                "circuits_tripped": count("circuits", "WHERE state='tripped'"),
                "open_trips": count("trips", "WHERE state='open'"),
                "assignments_connected": count("assignments", "WHERE state='connected'"),
                "assignments_queued": count("assignments", "WHERE state='queued'"),
                "assignments_pending_supply": count("assignments", "WHERE state='pending_supply'"),
                "conflict_candidates": count("connect_requests", "WHERE outcome='conflict_candidate'"),
                "batches_open": count("batches", "WHERE state='open'"),
                "batches_complete": count("batches", "WHERE state='complete'"),
            }
        finally:
            tx.c.close()

    def health(self) -> bool:
        try:
            connection = self._connect()
            connection.execute("SELECT 1").fetchone()
            connection.close()
            return True
        except sqlite3.Error:
            return False
