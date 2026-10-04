"""HTTP 路由与统一错误输出。"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .domain import Actor, DomainError, PermissionDenied, ValidationError


RESOURCE_RE = re.compile(r"^/api/(reefers|circuits|voyages|trips|queues|batches|conflicts|events)/?$")
ITEM_AUDIT_RE = re.compile(r"^/api/(reefers|circuits|voyages|batches)/(\d+)/audit$")
ITEM_ACTION_RE = re.compile(
    r"^/api/(reefers|circuits|voyages|conflicts)/(\d+)/(connect|load|trip|recover|maintenance|revise|resolve)$")


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
            if length > 2 * 1024 * 1024:
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
                if getattr(exc, "data", None):
                    payload["data"] = exc.data
                self._send(exc.status, payload)
            else:
                self._send(500, {"error": "internal_error", "message": "服务内部错误"})

        def _list_resource(self, resource: str, query: dict) -> None:
            actor = self._actor()
            state = query.get("state", [None])[0]
            if resource == "reefers":
                self._send(200, {"items": service.list_reefers(actor, state=state)})
            elif resource == "circuits":
                self._send(200, {"items": service.list_circuits(actor)})
            elif resource == "voyages":
                self._send(200, {"items": service.list_voyages(actor)})
            elif resource == "trips":
                circuit_id = query.get("circuit_id", [None])[0]
                self._send(200, {"items": service.list_trips(
                    actor, int(circuit_id) if circuit_id else None)})
            elif resource == "queues":
                self._send(200, {"items": service.list_queue(actor)})
            elif resource == "batches":
                self._send(200, {"items": service.list_batches(actor)})
            elif resource == "conflicts":
                self._send(200, {"items": service.list_conflicts(actor)})
            elif resource == "events":
                entity_type = query.get("entity_type", [None])[0]
                limit = int(query.get("limit", ["200"])[0])
                self._send(200, {"items": service.events(actor, entity_type=entity_type, limit=limit)})

        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                path = parsed.path
                if path == "/health":
                    self._send(200, {"status": "ok", "service": "reefer-ledger",
                                     "database": service.repository.health()})
                    return
                if path == "/":
                    page = (static_dir / "index.html").read_bytes()
                    self._send(200, page, "text/html; charset=utf-8")
                    return
                if path.startswith("/static/"):
                    name = path.split("/static/", 1)[1]
                    if "/" in name or ".." in name or name not in {"app.js"}:
                        self._send(404, {"error": "not_found", "message": "资源不存在"})
                        return
                    content = (static_dir / name).read_bytes()
                    ctype = "application/javascript; charset=utf-8" if name.endswith(".js") else "text/plain; charset=utf-8"
                    self._send(200, content, ctype)
                    return
                if path == "/api/ledger":
                    self._send(200, service.ledger(self._actor()))
                    return
                if path == "/api/stats":
                    self._send(200, service.stats(self._actor()))
                    return
                match = RESOURCE_RE.match(path)
                if match:
                    self._list_resource(match.group(1), parse_qs(parsed.query))
                    return
                match = ITEM_AUDIT_RE.match(path)
                if match:
                    entity_name = {"reefers": "reefer", "circuits": "circuit",
                                   "voyages": "voyage", "batches": "batch"}[match.group(1)]
                    self._send(200, {"items": service.timeline(
                        self._actor(), entity_name, int(match.group(2)))})
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

        def do_POST(self) -> None:
            try:
                parsed = urlparse(self.path)
                path = parsed.path
                body = self._body()
                actor = self._actor()
                if path == "/api/reefers":
                    self._send(201, service.create_reefer(actor, body.get("data", body)))
                    return
                if path == "/api/circuits":
                    self._send(201, service.create_circuit(actor, body.get("data", body)))
                    return
                if path == "/api/voyages":
                    self._send(201, service.create_voyage(actor, body.get("data", body)))
                    return
                if path == "/api/batches/connect":
                    self._send(201, service.batch_connect(actor, body.get("data", body)))
                    return
                if path == "/api/batches/fail":
                    self._send(200, service.batch_fail(actor, body.get("data", body)))
                    return
                if path == "/api/batches/recover":
                    self._send(200, service.recover_from_batch(actor, body.get("data", body)))
                    return
                if path == "/api/queues/pump":
                    self._send(200, service.pump_queue(actor))
                    return
                match = ITEM_ACTION_RE.match(path)
                if match:
                    entity, entity_id, action = match.group(1), int(match.group(2)), match.group(3)
                    data = body.get("data", body)
                    if entity == "reefers" and action == "connect":
                        self._send(200, service.connect(actor, entity_id, data))
                    elif entity == "reefers" and action == "load":
                        self._send(200, service.load_reefer(actor, entity_id, data))
                    elif entity == "circuits" and action == "trip":
                        self._send(200, service.trip_circuit(actor, entity_id, data))
                    elif entity == "circuits" and action == "recover":
                        self._send(200, service.recover_circuit(actor, entity_id, data))
                    elif entity == "circuits" and action == "maintenance":
                        self._send(200, service.set_circuit_maintenance(actor, entity_id, data))
                    elif entity == "voyages" and action == "revise":
                        self._send(200, service.revise_voyage(actor, entity_id, data))
                    elif entity == "conflicts" and action == "resolve":
                        self._send(200, service.resolve_conflict(actor, entity_id, data))
                    else:
                        self._send(404, {"error": "not_found", "message": "操作不存在"})
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

    return Handler


def create_server(host: str, port: int, service: Any, static_dir: Path) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service, static_dir))
