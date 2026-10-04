"""HTTP 路由与统一错误输出。"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .domain import Actor, DomainError, PermissionDenied, ValidationError


TRIP_RECOVER_RE = re.compile(r"^/api/trips/(\d+)/recover$")
TRIP_RE = re.compile(r"^/api/trips/(\d+)$")
VOYAGE_RESCHEDULE_RE = re.compile(r"^/api/voyages/([^/]+)/reschedule$")
CIRCUIT_STATE_RE = re.compile(r"^/api/circuits/([^/]+)/state$")
BATCH_COMPLETE_RE = re.compile(r"^/api/batches/([^/]+)/complete$")


def make_handler(service: Any, static_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "reefer-ledger/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _actor(self) -> Actor:
            user_id = self.headers.get("X-User-Id", "").strip()
            role = self.headers.get("X-Role", "").strip()
            if not user_id or not role:
                raise PermissionDenied("缺少X-User-Id或X-Role")
            return Actor(user_id=user_id, role=role, organization=self.headers.get("X-Org", ""))

        def _body(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValidationError("Content-Length无效") from exc
            if length > 1024 * 1024:
                raise ValidationError("请求体过大")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体必须是JSON") from exc
            if not isinstance(data, dict):
                raise ValidationError("JSON顶层必须是对象")
            return data

        def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
            if content_type.startswith("application/json"):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            else:
                body = payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_error(self, exc: Exception) -> None:
            if isinstance(exc, DomainError):
                payload = {"error": exc.code, "message": str(exc)}
                if getattr(exc, "details", None):
                    payload["details"] = exc.details
                self._send(exc.status, payload)
            else:
                self._send(500, {"error": "internal_error", "message": "服务内部错误"})

        # ---------- GET ----------
        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)
                if path == "/health":
                    self._send(200, {"status": "ok", "service": "reefer-ledger",
                                     "database": service.repository.health()})
                    return
                if path == "/":
                    page = (static_dir / "index.html").read_bytes()
                    self._send(200, page, "text/html; charset=utf-8")
                    return
                actor = self._actor()
                if path == "/api/reefers":
                    self._send(200, {"items": service.list_reefers(actor)})
                    return
                if path == "/api/circuits":
                    self._send(200, {"items": service.list_circuits(actor)})
                    return
                if path == "/api/voyages":
                    self._send(200, {"items": service.list_voyages(actor)})
                    return
                if path == "/api/batches":
                    self._send(200, {"items": service.list_batches(actor)})
                    return
                if path == "/api/trips":
                    self._send(200, {"items": service.list_trips(
                        actor, state=query.get("state", [None])[0])})
                    return
                match = TRIP_RE.match(path)
                if match:
                    self._send(200, self._trip_view(actor, int(match.group(1))))
                    return
                if path == "/api/assignments":
                    state = query.get("state", [None])[0]
                    active_only = query.get("active", ["0"])[0] in ("1", "true", "yes")
                    self._send(200, {"items": service.list_assignments(
                        actor, state=state, active_only=active_only)})
                    return
                if path == "/api/candidates":
                    self._send(200, {"items": service.list_candidates(actor)})
                    return
                if path == "/api/audit":
                    entity_type = query.get("entity_type", [None])[0]
                    entity_id_raw = query.get("entity_id", [None])[0]
                    entity_id = int(entity_id_raw) if entity_id_raw not in (None, "") else None
                    limit = int(query.get("limit", ["200"])[0])
                    self._send(200, {"items": service.timeline(
                        actor, entity_type=entity_type, entity_id=entity_id, limit=limit)})
                    return
                if path == "/api/stats":
                    self._send(200, service.stats(actor))
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

        def _trip_view(self, actor: Actor, trip_id: int) -> dict:
            trips = service.list_trips(actor)
            for item in trips:
                if item["id"] == trip_id:
                    return item
            from src.domain import NotFound
            raise NotFound("跳闸记录不存在")

        # ---------- POST ----------
        def do_POST(self) -> None:
            try:
                parsed = urlparse(self.path)
                path = parsed.path
                body = self._body()
                actor = self._actor()
                if path == "/api/reefers":
                    self._send(201, service.register_reefer(actor, body))
                    return
                if path == "/api/circuits":
                    self._send(201, service.register_circuit(actor, body))
                    return
                if path == "/api/voyages":
                    self._send(201, service.create_voyage(actor, body))
                    return
                if path == "/api/batches":
                    self._send(201, service.create_batch(actor, body))
                    return
                match = BATCH_COMPLETE_RE.match(path)
                if match:
                    self._send(200, service.complete_batch(actor, match.group(1)))
                    return
                match = VOYAGE_RESCHEDULE_RE.match(path)
                if match:
                    self._send(200, service.reschedule_voyage(actor, match.group(1), body))
                    return
                match = CIRCUIT_STATE_RE.match(path)
                if match:
                    self._send(200, service.set_circuit_state(actor, match.group(1), body))
                    return
                if path == "/api/connect-requests":
                    self._send(201, service.request_connect(actor, body))
                    return
                if path == "/api/gate-release":
                    self._send(200, service.gate_release(actor, body))
                    return
                if path == "/api/trips":
                    self._send(201, service.trip_circuit(actor, body))
                    return
                match = TRIP_RECOVER_RE.match(path)
                if match:
                    self._send(200, service.recover_trip(actor, int(match.group(1)), body))
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

    return Handler


def create_server(host: str, port: int, service: Any, static_dir: Path) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service, static_dir))
