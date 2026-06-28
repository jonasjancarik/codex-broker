from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .app_server import AppServerError, AppServerPool
from .auth import AuthManager
from .bundles import BundleError, BundleRegistry
from .config import BrokerConfig
from .scheduler import ActiveTurnError, ConflictError, NotFoundError, TurnScheduler
from .state import StateStore
from .util import ensure_dir, json_dumps, json_log


@dataclass
class BrokerServices:
    config: BrokerConfig
    state: StateStore
    auth: AuthManager
    bundles: BundleRegistry
    pool: AppServerPool
    scheduler: TurnScheduler

    @classmethod
    def build(cls, config: BrokerConfig) -> "BrokerServices":
        for path in (config.data_dir, config.auth_root, config.inline_bundle_root, config.overlay_root):
            ensure_dir(path)
        state = StateStore(config.state_db_path)
        recovered_turns = state.recover_incomplete_turns("Broker restarted before the turn completed.")
        pruned_raw_events = 0
        if config.raw_event_retention_seconds > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=config.raw_event_retention_seconds)
            pruned_raw_events = state.prune_raw_events_before(cutoff.isoformat().replace("+00:00", "Z"))
        auth = AuthManager(config, state)
        bundles = BundleRegistry(config, state)
        pool = AppServerPool(config, state)
        scheduler = TurnScheduler(config=config, state=state, auth=auth, bundles=bundles, pool=pool)
        scheduler.note_recovered_turns(recovered_turns)
        scheduler.note_pruned_raw_events(pruned_raw_events)
        return cls(config=config, state=state, auth=auth, bundles=bundles, pool=pool, scheduler=scheduler)


def serve(config: BrokerConfig) -> None:
    services = BrokerServices.build(config)

    class Handler(BrokerHandler):
        broker = services

    server = ThreadingHTTPServer((config.host, config.port), Handler)
    try:
        server.serve_forever()
    finally:
        services.scheduler.shutdown(config.shutdown_mode, config.shutdown_drain_timeout_seconds)
        services.pool.close_all()
        services.state.close()


def metric_path_template(path: str) -> str:
    if path in {"/healthz", "/readyz", "/metrics", "/openapi.json"}:
        return path.strip("/") or "root"
    segments = [part for part in path.strip("/").split("/") if part]
    if segments[:2] == ["v1", "bundles"]:
        return "/".join(segments)
    if len(segments) >= 4 and segments[:2] == ["v1", "owners"]:
        templated = ["v1", "owners", "ownerId", *segments[3:]]
        if len(templated) >= 5 and templated[3] == "threads":
            templated[4] = "threadId"
        if len(templated) >= 7 and templated[5] == "turns":
            templated[6] = "turnId"
        return "/".join(templated)
    return "unknown"


def is_unauthenticated_path(method: str, path: str) -> bool:
    return method == "GET" and path in {"/healthz", "/readyz"}


class BrokerHandler(BaseHTTPRequestHandler):
    broker: BrokerServices
    server_version = "CodexBroker/0.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API.
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API.
        self._dispatch("POST")

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _dispatch(self, method: str) -> None:
        started_at = time.monotonic()
        self._metric_status = HTTPStatus.INTERNAL_SERVER_ERROR
        metric_endpoint = metric_path_template(urlparse(self.path).path)
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            if method == "GET" and path == "/healthz":
                self._json({"status": "ok"})
                return
            if method == "GET" and path == "/readyz":
                self._readyz()
                return
            if not is_unauthenticated_path(method, path) and not self._authorized():
                self._json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                return
            if method == "GET" and path == "/metrics":
                self._metrics()
                return
            if method == "GET" and path == "/openapi.json":
                self._json(openapi_document())
                return
            segments = [unquote(part) for part in path.strip("/").split("/") if part]
            if segments[:2] == ["v1", "bundles"] and method == "POST" and len(segments) == 3 and segments[2] == "inline":
                bundle = self.broker.bundles.accept_inline(self._read_json())
                self._json({"bundleId": bundle.bundle_id, "digest": bundle.digest, "source": bundle.source}, HTTPStatus.CREATED)
                return
            if len(segments) >= 4 and segments[:2] == ["v1", "owners"]:
                owner_id = segments[2]
                self._owner_route(method, segments[3:], owner_id, query)
                return
            self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
        except ActiveTurnError as exc:
            self._json({"error": str(exc) or "active_turn_exists"}, HTTPStatus.CONFLICT)
        except ConflictError as exc:
            self._json({"error": str(exc)}, HTTPStatus.CONFLICT)
        except NotFoundError as exc:
            self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
        except (ValueError, BundleError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except AppServerError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
        except Exception as exc:  # noqa: BLE001 - HTTP boundary must return JSON errors.
            self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        finally:
            elapsed = time.monotonic() - started_at
            status = int(getattr(self, "_metric_status", HTTPStatus.INTERNAL_SERVER_ERROR))
            self.broker.scheduler.note_http_request(metric_endpoint, status, elapsed)
            json_log(
                self.broker.config.json_logs,
                "http.request",
                method=method,
                endpoint=metric_endpoint,
                status=status,
                durationMs=round(elapsed * 1000, 3),
                **self._log_context_for_path(urlparse(self.path).path),
            )

    def _owner_route(self, method: str, tail: list[str], owner_id: str, query: dict[str, list[str]]) -> None:
        if tail[:1] == ["auth"]:
            self._auth_route(method, tail[1:], owner_id, query)
            return
        if tail == ["audit-logs"]:
            self._audit_route(method, owner_id, query)
            return
        if tail[:1] == ["threads"]:
            self._thread_route(method, tail[1:], owner_id, query)
            return
        self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def _audit_route(self, method: str, owner_id: str, query: dict[str, list[str]]) -> None:
        if method != "GET":
            self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        owner_hash = self.broker.auth.hash_owner(owner_id)
        limit = int(query.get("limit", ["100"])[0] or "100")
        logs = self.broker.state.list_audit_logs(
            owner_hash,
            action=query.get("action", [None])[0],
            profile=query.get("profile", [None])[0],
            thread_id=query.get("threadId", [None])[0],
            turn_id=query.get("turnId", [None])[0],
            limit=limit,
        )
        self._json({"ownerHash": owner_hash, "auditLogs": [self._public_audit(entry) for entry in logs]})

    def _auth_route(self, method: str, tail: list[str], owner_id: str, query: dict[str, list[str]]) -> None:
        profile = query.get("profile", ["default"])[0]
        if method == "GET" and tail == ["status"]:
            self._json(self.broker.auth.status(owner_id, profile))
            return
        if method == "POST" and tail == ["device", "start"]:
            body = self._read_json(allow_empty=True)
            self._json(self.broker.auth.start_device_auth(owner_id, str(body.get("profile") or profile)), HTTPStatus.ACCEPTED)
            return
        if method == "POST" and tail == ["device", "submit"]:
            body = self._read_json()
            self._json(
                self.broker.auth.submit_device_code(
                    owner_id,
                    str(body.get("code") or ""),
                    profile=str(body.get("profile") or profile),
                    session_id=body.get("sessionId") if isinstance(body.get("sessionId"), str) else None,
                )
            )
            return
        if method == "POST" and tail == ["api-key"]:
            body = self._read_json()
            result = self.broker.auth.login_api_key(owner_id, str(body.get("apiKey") or ""), str(body.get("profile") or profile))
            self.broker.pool.close_profile(result["ownerHash"], result["profile"])
            self._json(result)
            return
        if method == "POST" and tail == ["runtime", "invalidate"]:
            body = self._read_json(allow_empty=True)
            profile_key = self.broker.auth.profile_key(str(body.get("profile") or profile))
            owner_hash = self.broker.auth.hash_owner(owner_id)
            self.broker.pool.close_profile(owner_hash, profile_key)
            self.broker.state.append_audit(owner_hash, "auth.runtime.invalidate", {}, profile=profile_key)
            self._json({"ownerHash": owner_hash, "profile": profile_key, "invalidated": True})
            return
        if method == "POST" and tail == ["logout"]:
            body = self._read_json(allow_empty=True)
            result = self.broker.auth.logout(
                owner_id,
                str(body.get("profile") or profile),
                delete_profile=bool(body.get("deleteProfile")),
            )
            self.broker.pool.close_profile(result["ownerHash"], result["profile"])
            self._json(result)
            return
        self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def _thread_route(self, method: str, tail: list[str], owner_id: str, query: dict[str, list[str]]) -> None:
        if method == "POST" and tail == []:
            self._json(self.broker.scheduler.create_thread(owner_id, self._read_json(allow_empty=True)), HTTPStatus.CREATED)
            return
        if len(tail) < 1:
            self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        thread_id = tail[0]
        if method == "GET" and len(tail) == 1:
            self._json(self.broker.scheduler.get_thread(owner_id, thread_id))
            return
        if method == "POST" and tail[1:] == ["archive"]:
            self._json(self.broker.scheduler.archive_thread(owner_id, thread_id))
            return
        if method == "GET" and tail[1:] == ["events"]:
            self._sse_events(owner_id, thread_id, query)
            return
        if len(tail) >= 2 and tail[1] == "turns":
            self._turn_route(method, tail[2:], owner_id, thread_id)
            return
        self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def _turn_route(self, method: str, tail: list[str], owner_id: str, thread_id: str) -> None:
        if method == "POST" and tail == []:
            self._json(self.broker.scheduler.start_turn(owner_id, thread_id, self._read_json()), HTTPStatus.ACCEPTED)
            return
        if len(tail) < 1:
            self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        turn_id = tail[0]
        if method == "GET" and len(tail) == 1:
            self._json(self.broker.scheduler.get_turn(owner_id, thread_id, turn_id))
            return
        if method == "POST" and tail[1:] == ["steer"]:
            self._json(self.broker.scheduler.steer_turn(owner_id, thread_id, turn_id, self._read_json()))
            return
        if method == "POST" and tail[1:] == ["interrupt"]:
            self._json(self.broker.scheduler.interrupt_turn(owner_id, thread_id, turn_id))
            return
        self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def _public_audit(self, entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": entry["id"],
            "ownerHash": entry["owner_hash"],
            "profile": entry.get("profile"),
            "threadId": entry.get("thread_id"),
            "turnId": entry.get("turn_id"),
            "action": entry["action"],
            "payload": entry["payload"],
            "createdAt": entry["created_at"],
        }

    def _sse_events(self, owner_id: str, thread_id: str, query: dict[str, list[str]]) -> None:
        owner_hash = self.broker.auth.hash_owner(owner_id)
        after = int(query.get("after", ["0"])[0] or "0")
        turn_id = query.get("turnId", [None])[0]
        if not self.broker.state.get_thread(owner_hash, thread_id):
            raise NotFoundError("Thread not found.")
        if turn_id and not self.broker.state.get_turn(owner_hash, thread_id, turn_id):
            raise NotFoundError("Turn not found.")
        self._metric_status = HTTPStatus.OK
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last_heartbeat = time.monotonic()
        while True:
            events = self.broker.state.list_events(owner_hash, thread_id, after=after, turn_id=turn_id, limit=100)
            for event in events:
                after = int(event["id"])
                payload = {
                    "id": event["id"],
                    "type": event["event_type"],
                    "ownerHash": owner_hash,
                    "threadId": thread_id,
                    "turnId": event.get("turn_id"),
                    "productCorrelationId": event.get("product_correlation_id"),
                    "codexThreadId": event.get("codex_thread_id"),
                    "codexTurnId": event.get("codex_turn_id"),
                    "createdAt": event["created_at"],
                    "payload": event["payload"],
                    "ambiguous": event["ambiguous"],
                }
                if event.get("raw_method"):
                    payload["rawMethod"] = event["raw_method"]
                    payload["rawParams"] = event.get("raw_params")
                if not self._write_sse(event["event_type"], payload, event_id=after):
                    self.broker.scheduler.note_event_stream_disconnect()
                    return
            now = time.monotonic()
            if now - last_heartbeat > 10:
                if not self._write_raw(": heartbeat\n\n"):
                    self.broker.scheduler.note_event_stream_disconnect()
                    return
                last_heartbeat = now
            time.sleep(0.25)

    def _readyz(self) -> None:
        errors: list[str] = []
        if not self.broker.state.ping():
            errors.append("state store unavailable")
        if not self.broker.config.internal_key and not self.broker.config.allow_unauthenticated:
            errors.append("internal API key not configured")
        command = self.broker.config.codex_command[0]
        if not shutil.which(command) and not Path(command).exists():
            errors.append(f"Codex binary not found: {command}")
        for root in self.broker.config.allowed_workspace_roots:
            if not self._readable_dir(root):
                errors.append(f"workspace root unreadable: {root}")
        for root in self.broker.config.allowed_bundle_roots:
            if not self._readable_dir(root):
                errors.append(f"bundle root unreadable: {root}")
        try:
            ensure_dir(self.broker.config.auth_root)
            probe = self.broker.config.auth_root / ".write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            errors.append(f"auth root not writable: {exc}")
        status = HTTPStatus.OK if not errors else HTTPStatus.SERVICE_UNAVAILABLE
        self._json({"status": "ready" if not errors else "not_ready", "errors": errors}, status)

    @staticmethod
    def _readable_dir(path: Path) -> bool:
        return path.exists() and path.is_dir() and os.access(path, os.R_OK | os.X_OK)

    def _metrics(self) -> None:
        metrics = self.broker.scheduler.metrics()
        body = "\n".join(f"codex_broker_{key} {value}" for key, value in sorted(metrics.items())) + "\n"
        self._metric_status = HTTPStatus.OK
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def _authorized(self) -> bool:
        key = self.broker.config.internal_key
        if not key:
            return self.broker.config.allow_unauthenticated
        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {key}":
            return True
        return self.headers.get("X-Codex-Broker-Key") == key

    def _read_json(self, *, allow_empty: bool = False) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {} if allow_empty else (_ for _ in ()).throw(ValueError("JSON request body is required."))
        if length > 1_000_000:
            raise ValueError("JSON request body is too large.")
        data = self.rfile.read(length)
        parsed = json.loads(data.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("JSON request body must be an object.")
        return parsed

    def _json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json_dumps(payload).encode("utf-8")
        self._metric_status = status
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_sse(self, event: str, payload: dict[str, Any], *, event_id: int) -> bool:
        body = f"id: {event_id}\nevent: {event}\ndata: {json_dumps(payload)}\n\n"
        return self._write_raw(body)

    def _write_raw(self, body: str) -> bool:
        try:
            self.wfile.write(body.encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _log_context_for_path(self, path: str) -> dict[str, Any]:
        segments = [unquote(part) for part in path.strip("/").split("/") if part]
        context: dict[str, Any] = {}
        if len(segments) >= 3 and segments[:2] == ["v1", "owners"]:
            context["ownerHash"] = self.broker.auth.hash_owner(segments[2])
        if len(segments) >= 5 and segments[3] == "threads":
            context["threadId"] = segments[4]
        if len(segments) >= 7 and segments[5] == "turns":
            context["turnId"] = segments[6]
        return context


def openapi_document() -> dict[str, Any]:
    def ref(name: str) -> dict[str, str]:
        return {"$ref": f"#/components/schemas/{name}"}

    def json_response(schema: dict[str, Any], description: str = "OK") -> dict[str, Any]:
        return {"description": description, "content": {"application/json": {"schema": schema}}}

    def request_body(schema: dict[str, Any], *, required: bool = True) -> dict[str, Any]:
        return {"required": required, "content": {"application/json": {"schema": schema}}}

    owner_param = {"$ref": "#/components/parameters/ownerId"}
    thread_param = {"$ref": "#/components/parameters/threadId"}
    turn_param = {"$ref": "#/components/parameters/turnId"}
    return {
        "openapi": "3.1.0",
        "info": {"title": "Codex Broker", "version": "0.4.0"},
        "security": [{"brokerKey": []}],
        "paths": {
            "/healthz": {
                "get": {"security": [], "responses": {"200": json_response(ref("Health"), "Healthy")}}
            },
            "/readyz": {
                "get": {
                    "security": [],
                    "responses": {
                        "200": json_response(ref("Readiness"), "Ready"),
                        "503": json_response(ref("Readiness"), "Not ready"),
                    },
                }
            },
            "/metrics": {
                "get": {
                    "responses": {
                        "200": {
                            "description": "Prometheus metrics",
                            "content": {"text/plain": {"schema": {"type": "string"}}},
                        }
                    },
                }
            },
            "/openapi.json": {
                "get": {"responses": {"200": json_response({"type": "object"}, "OpenAPI document")}}
            },
            "/v1/owners/{ownerId}/auth/status": {
                "get": {
                    "parameters": [owner_param, {"$ref": "#/components/parameters/profile"}],
                    "responses": {"200": json_response(ref("AuthStatus")), "401": json_response(ref("Error"), "Unauthorized")},
                }
            },
            "/v1/owners/{ownerId}/auth/device/start": {
                "post": {
                    "parameters": [owner_param],
                    "requestBody": request_body(ref("ProfileRequest"), required=False),
                    "responses": {"202": json_response(ref("DeviceAuthSession"), "Device auth started")},
                }
            },
            "/v1/owners/{ownerId}/auth/device/submit": {
                "post": {
                    "parameters": [owner_param],
                    "requestBody": request_body(ref("DeviceCodeSubmitRequest")),
                    "responses": {"200": json_response(ref("DeviceAuthSession"), "Device code submitted")},
                }
            },
            "/v1/owners/{ownerId}/auth/api-key": {
                "post": {
                    "parameters": [owner_param],
                    "requestBody": request_body(ref("ApiKeyLoginRequest")),
                    "responses": {"200": json_response(ref("AuthCommandResult"), "API key stored in owner auth home")},
                }
            },
            "/v1/owners/{ownerId}/auth/runtime/invalidate": {
                "post": {
                    "parameters": [owner_param],
                    "requestBody": request_body(ref("ProfileRequest"), required=False),
                    "responses": {"200": json_response(ref("RuntimeInvalidationResult"), "Owner profile runtime invalidated")},
                }
            },
            "/v1/owners/{ownerId}/auth/logout": {
                "post": {
                    "parameters": [owner_param],
                    "requestBody": request_body(ref("ProfileRequest"), required=False),
                    "responses": {"200": json_response(ref("AuthCommandResult"), "Owner profile logged out")},
                }
            },
            "/v1/owners/{ownerId}/audit-logs": {
                "get": {
                    "parameters": [
                        owner_param,
                        {"$ref": "#/components/parameters/profile"},
                        {"$ref": "#/components/parameters/action"},
                        {"$ref": "#/components/parameters/threadIdQuery"},
                        {"$ref": "#/components/parameters/turnIdQuery"},
                        {"$ref": "#/components/parameters/limit"},
                    ],
                    "responses": {"200": json_response(ref("AuditLogList"), "Owner-scoped audit logs")},
                }
            },
            "/v1/owners/{ownerId}/threads": {
                "post": {
                    "parameters": [owner_param],
                    "requestBody": request_body(ref("ThreadCreateRequest"), required=False),
                    "responses": {"201": json_response(ref("Thread"), "Thread created")},
                }
            },
            "/v1/owners/{ownerId}/threads/{threadId}": {
                "get": {"parameters": [owner_param, thread_param], "responses": {"200": json_response(ref("Thread"))}}
            },
            "/v1/owners/{ownerId}/threads/{threadId}/archive": {
                "post": {
                    "parameters": [owner_param, thread_param],
                    "responses": {"200": json_response(ref("Thread"), "Thread archived")},
                }
            },
            "/v1/owners/{ownerId}/threads/{threadId}/turns": {
                "post": {
                    "parameters": [owner_param, thread_param],
                    "requestBody": request_body(ref("TurnStartRequest")),
                    "responses": {"202": json_response(ref("Turn"), "Turn accepted")},
                }
            },
            "/v1/owners/{ownerId}/threads/{threadId}/turns/{turnId}": {
                "get": {
                    "parameters": [owner_param, thread_param, turn_param],
                    "responses": {"200": json_response(ref("Turn"))},
                }
            },
            "/v1/owners/{ownerId}/threads/{threadId}/turns/{turnId}/steer": {
                "post": {
                    "parameters": [owner_param, thread_param, turn_param],
                    "requestBody": request_body(ref("TurnSteerRequest")),
                    "responses": {"200": json_response(ref("Turn"), "Turn steered")},
                }
            },
            "/v1/owners/{ownerId}/threads/{threadId}/turns/{turnId}/interrupt": {
                "post": {
                    "parameters": [owner_param, thread_param, turn_param],
                    "responses": {"200": json_response(ref("Turn"), "Turn interrupted")},
                }
            },
            "/v1/owners/{ownerId}/threads/{threadId}/events": {
                "get": {
                    "parameters": [owner_param, thread_param, {"$ref": "#/components/parameters/after"}, {"$ref": "#/components/parameters/turnIdQuery"}],
                    "responses": {
                        "200": {
                            "description": "SSE event stream of BrokerEvent JSON payloads",
                            "content": {"text/event-stream": {"schema": ref("BrokerEvent")}},
                        }
                    },
                }
            },
            "/v1/bundles/inline": {
                "post": {
                    "requestBody": request_body(ref("TaskBundle")),
                    "responses": {"201": json_response(ref("BundleAccepted"), "Inline bundle accepted")},
                }
            },
        },
        "components": {
            "securitySchemes": {
                "brokerKey": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "Authorization",
                    "description": "Use `Authorization: Bearer <key>` or `X-Codex-Broker-Key: <key>`.",
                }
            },
            "parameters": {
                "ownerId": {"name": "ownerId", "in": "path", "required": True, "schema": {"type": "string"}},
                "threadId": {"name": "threadId", "in": "path", "required": True, "schema": {"type": "string"}},
                "turnId": {"name": "turnId", "in": "path", "required": True, "schema": {"type": "string"}},
                "threadIdQuery": {"name": "threadId", "in": "query", "required": False, "schema": {"type": "string"}},
                "turnIdQuery": {"name": "turnId", "in": "query", "required": False, "schema": {"type": "string"}},
                "profile": {"name": "profile", "in": "query", "required": False, "schema": {"type": "string", "default": "default"}},
                "after": {"name": "after", "in": "query", "required": False, "schema": {"type": "integer", "minimum": 0, "default": 0}},
                "action": {"name": "action", "in": "query", "required": False, "schema": {"type": "string"}},
                "limit": {"name": "limit", "in": "query", "required": False, "schema": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100}},
            },
            "schemas": {
                "Error": {"type": "object", "required": ["error"], "properties": {"error": {"type": "string"}}},
                "Health": {"type": "object", "required": ["status"], "properties": {"status": {"const": "ok"}}},
                "Readiness": {
                    "type": "object",
                    "required": ["status", "errors"],
                    "properties": {
                        "status": {"enum": ["ready", "not_ready"]},
                        "errors": {"type": "array", "items": {"type": "string"}},
                    },
                },
                "ProfileRequest": {
                    "type": "object",
                    "properties": {
                        "profile": {"type": "string", "default": "default"},
                        "deleteProfile": {"type": "boolean", "default": False},
                    },
                },
                "DeviceCodeSubmitRequest": {
                    "type": "object",
                    "required": ["code"],
                    "properties": {"code": {"type": "string"}, "profile": {"type": "string"}, "sessionId": {"type": "string"}},
                },
                "ApiKeyLoginRequest": {
                    "type": "object",
                    "required": ["apiKey"],
                    "properties": {"apiKey": {"type": "string", "writeOnly": True}, "profile": {"type": "string"}},
                },
                "DeviceAuthSession": {
                    "type": "object",
                    "required": ["sessionId", "state", "profile", "expiresAt"],
                    "properties": {
                        "sessionId": {"type": "string"},
                        "state": {"type": "string"},
                        "profile": {"type": "string"},
                        "command": {"type": "array", "items": {"type": "string"}},
                        "startedAt": {"type": "string"},
                        "updatedAt": {"type": "string"},
                        "completedAt": {"type": ["string", "null"]},
                        "loginUrl": {"type": ["string", "null"]},
                        "userCode": {"type": ["string", "null"]},
                        "expiresAt": {"type": ["string", "null"]},
                        "output": {"type": "array", "items": {"type": "string"}},
                        "exitCode": {"type": ["integer", "null"]},
                        "error": {"type": ["string", "null"]},
                    },
                },
                "AuthStatus": {
                    "type": "object",
                    "required": ["ownerHash", "profile", "state", "authFilePresent"],
                    "properties": {
                        "ownerHash": {"type": "string"},
                        "profile": {"type": "string"},
                        "state": {"enum": ["missing", "present_unverified", "authenticated", "refresh_failed", "invalid", "failed", "unknown"]},
                        "deviceAuth": {"anyOf": [ref("DeviceAuthSession"), {"type": "null"}]},
                        "authFilePresent": {"type": "boolean"},
                        "authFingerprint": {"type": "string"},
                        "loginStatusExitCode": {"type": ["integer", "null"]},
                        "loginStatusOutput": {"type": "string"},
                    },
                },
                "AuthCommandResult": {
                    "type": "object",
                    "required": ["ownerHash", "profile", "state", "exitCode", "output"],
                    "properties": {
                        "ownerHash": {"type": "string"},
                        "profile": {"type": "string"},
                        "state": {"type": "string"},
                        "deleted": {"type": "boolean"},
                        "authFingerprint": {"type": "string"},
                        "exitCode": {"type": "integer"},
                        "output": {"type": "string"},
                    },
                },
                "RuntimeInvalidationResult": {
                    "type": "object",
                    "required": ["ownerHash", "profile", "invalidated"],
                    "properties": {
                        "ownerHash": {"type": "string"},
                        "profile": {"type": "string"},
                        "invalidated": {"type": "boolean"},
                    },
                },
                "AuditLog": {
                    "type": "object",
                    "required": ["id", "ownerHash", "action", "payload", "createdAt"],
                    "properties": {
                        "id": {"type": "integer"},
                        "ownerHash": {"type": "string"},
                        "profile": {"type": ["string", "null"]},
                        "threadId": {"type": ["string", "null"]},
                        "turnId": {"type": ["string", "null"]},
                        "action": {"type": "string"},
                        "payload": {"type": "object", "additionalProperties": True},
                        "createdAt": {"type": "string"},
                    },
                },
                "AuditLogList": {
                    "type": "object",
                    "required": ["ownerHash", "auditLogs"],
                    "properties": {
                        "ownerHash": {"type": "string"},
                        "auditLogs": {"type": "array", "items": ref("AuditLog")},
                    },
                },
                "ThreadCreateRequest": {
                    "type": "object",
                    "properties": {
                        "threadId": {"type": "string"},
                        "profile": {"type": "string", "default": "default"},
                        "configProfile": {"type": "string", "default": "default"},
                        "runtimeProfile": {"type": "string", "deprecated": True},
                        "hostApp": {"type": "string"},
                        "bundleId": {"type": "string"},
                        "cwd": {"type": "string"},
                    },
                },
                "Thread": {
                    "type": "object",
                    "required": ["threadId", "profile", "configProfile", "status", "createdAt", "updatedAt"],
                    "properties": {
                        "threadId": {"type": "string"},
                        "codexThreadId": {"type": ["string", "null"]},
                        "profile": {"type": "string"},
                        "configProfile": {"type": "string"},
                        "hostApp": {"type": ["string", "null"]},
                        "bundleId": {"type": ["string", "null"]},
                        "cwd": {"type": ["string", "null"]},
                        "status": {"type": "string"},
                        "createdAt": {"type": "string"},
                        "updatedAt": {"type": "string"},
                    },
                },
                "InputItem": {"type": "object", "additionalProperties": True},
                "CodexOptions": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {
                        "approvalPolicy": {"type": "string"},
                        "sandbox": {"type": "string"},
                        "serviceTier": {"type": "string"},
                        "model": {"type": "string"},
                        "effort": {"type": "string"},
                        "reasoningEffort": {"type": "string"},
                        "personality": {"type": "string"},
                        "summary": {"type": "string"},
                        "reasoningSummary": {"type": "string"},
                        "webSearch": {"type": "string"},
                        "modelVerbosity": {"type": "string"},
                        "imageGeneration": {"type": "boolean"},
                    },
                },
                "TurnStartRequest": {
                    "type": "object",
                    "required": ["input"],
                    "properties": {
                        "input": {"type": "array", "minItems": 1, "items": ref("InputItem")},
                        "mode": {"enum": ["reject", "queue", "steer"], "default": "reject"},
                        "profile": {"type": "string"},
                        "configProfile": {"type": "string"},
                        "runtimeProfile": {"type": "string", "deprecated": True},
                        "hostApp": {"type": "string"},
                        "bundleId": {"type": "string"},
                        "cwd": {"type": "string"},
                        "codexOptions": ref("CodexOptions"),
                        "runtime": ref("CodexOptions"),
                        "stream": {"type": "boolean", "default": True},
                        "idempotencyKey": {"type": "string"},
                        "productCorrelationId": {"type": "string"},
                        "correlationId": {"type": "string"},
                    },
                },
                "TurnSteerRequest": {
                    "type": "object",
                    "required": ["input"],
                    "properties": {"input": {"type": "array", "minItems": 1, "items": ref("InputItem")}},
                },
                "Turn": {
                    "type": "object",
                    "required": ["threadId", "turnId", "profile", "configProfile", "mode", "status", "createdAt", "updatedAt"],
                    "properties": {
                        "threadId": {"type": "string"},
                        "turnId": {"type": "string"},
                        "codexTurnId": {"type": ["string", "null"]},
                        "profile": {"type": "string"},
                        "configProfile": {"type": "string"},
                        "hostApp": {"type": ["string", "null"]},
                        "bundleId": {"type": ["string", "null"]},
                        "cwd": {"type": ["string", "null"]},
                        "mode": {"type": "string"},
                        "productCorrelationId": {"type": ["string", "null"]},
                        "status": {"type": "string"},
                        "error": {"type": ["string", "null"]},
                        "errorCode": {"type": ["string", "null"]},
                        "publicMessage": {"type": ["string", "null"]},
                        "adminMessage": {"type": ["string", "null"]},
                        "createdAt": {"type": "string"},
                        "startedAt": {"type": ["string", "null"]},
                        "completedAt": {"type": ["string", "null"]},
                        "updatedAt": {"type": "string"},
                        "streamUrl": {"type": "string"},
                    },
                },
                "BrokerEvent": {
                    "type": "object",
                    "required": ["id", "type", "threadId", "createdAt", "payload", "ambiguous"],
                    "properties": {
                        "id": {"type": "integer"},
                        "type": {"type": "string"},
                        "ownerHash": {"type": "string"},
                        "threadId": {"type": "string"},
                        "turnId": {"type": ["string", "null"]},
                        "productCorrelationId": {"type": ["string", "null"]},
                        "codexThreadId": {"type": ["string", "null"]},
                        "codexTurnId": {"type": ["string", "null"]},
                        "createdAt": {"type": "string"},
                        "payload": {"type": "object", "additionalProperties": True},
                        "ambiguous": {"type": "boolean"},
                        "rawMethod": {"type": "string"},
                        "rawParams": {"type": "object", "additionalProperties": True},
                    },
                },
                "TaskBundle": {
                    "type": "object",
                    "required": ["id"],
                    "additionalProperties": True,
                    "properties": {
                        "id": {"type": "string"},
                        "version": {"type": "string"},
                        "instructions": {"type": "array", "items": {"type": "string"}},
                        "skills": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                        "prompts": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                        "mcpServers": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                        "tools": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                        "allowedPaths": {"type": "array", "items": {"type": "string"}},
                        "sandbox": {"type": "object", "additionalProperties": True},
                    },
                },
                "BundleAccepted": {
                    "type": "object",
                    "required": ["bundleId", "digest", "source"],
                    "properties": {"bundleId": {"type": "string"}, "digest": {"type": "string"}, "source": {"type": "string"}},
                },
            },
        },
    }
