"""Read-only operator dashboard routes under ``/admin``.

The page and its static assets contain no data and are served without
authentication. Every ``/admin/api/`` route requires the operator key and
never returns conversation content or raw owner IDs.
"""

from __future__ import annotations

import base64
import secrets
import time
from http import HTTPStatus
from pathlib import Path
from typing import Any

from . import __version__
from .state_admin import AdminQueries, clamp_limit
from .util import json_dumps, json_log

STATIC_DIR = Path(__file__).resolve().parent / "admin_static"
STATIC_FILES = {
    "/admin": ("index.html", "text/html; charset=utf-8"),
    "/admin/": ("index.html", "text/html; charset=utf-8"),
    "/admin/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/admin/app.css": ("app.css", "text/css; charset=utf-8"),
}
PAGE_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
LIVE_METRIC_KEYS = (
    "active_turns",
    "queued_turns",
    "active_app_server_children",
    "pending_app_server_creations",
    "app_server_restarts",
    "turns_started",
    "turns_completed",
    "turns_failed",
    "turns_interrupted",
    "auth_starts",
    "auth_successes",
    "auth_failures",
    "secret_replacements_total",
)


def is_admin_path(path: str) -> bool:
    return path == "/admin" or path.startswith("/admin/")


def admin_metric_path(path: str) -> str:
    segments = [part for part in path.strip("/").split("/") if part]
    if segments[:2] != ["admin", "api"]:
        return "admin"
    if len(segments) == 4 and segments[2] == "owners" and segments[3] != "lookup":
        return "admin/api/owners/ownerHash"
    return "/".join(segments[:4])


def handle_admin_route(handler: Any, method: str, path: str, query: dict[str, list[str]]) -> bool:
    if not is_admin_path(path):
        return False
    operator_key = handler.broker.config.operator_key
    if not operator_key:
        _send_json(handler, {"error": "not_found"}, HTTPStatus.NOT_FOUND)
        return True
    if path in STATIC_FILES:
        if method != "GET":
            _send_json(handler, {"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return True
        _send_static(handler, *STATIC_FILES[path])
        return True
    if not path.startswith("/admin/api/"):
        _send_json(handler, {"error": "not_found"}, HTTPStatus.NOT_FOUND)
        return True
    if not _operator_authorized(handler, operator_key):
        _send_json(handler, {"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
        return True
    route = AdminRoutes(handler)
    route.dispatch(method, [part for part in path.strip("/").split("/") if part][2:], query)
    json_log(
        handler.broker.config.json_logs,
        "admin.request",
        sanitizer=handler.broker.sanitizer,
        method=method,
        route=admin_metric_path(path),
        status=int(getattr(handler, "_metric_status", 0)),
    )
    return True


def _operator_authorized(handler: Any, operator_key: str) -> bool:
    auth = handler.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    return secrets.compare_digest(auth[len("Bearer ") :].encode("utf-8"), operator_key.encode("utf-8"))


def _send_json(handler: Any, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
    body = json_dumps(payload).encode("utf-8")
    handler._metric_status = status
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(body)


def _send_static(handler: Any, filename: str, content_type: str) -> None:
    body = (STATIC_DIR / filename).read_bytes()
    handler._metric_status = HTTPStatus.OK
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    for name, value in PAGE_SECURITY_HEADERS.items():
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(body)


def _encode_cursor(value: int) -> str:
    return base64.urlsafe_b64encode(str(value).encode("ascii")).decode("ascii").rstrip("=")


def _decode_cursor(value: str | None) -> int | None:
    if not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        number = int(base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid cursor.") from exc
    if number < 0:
        raise ValueError("Invalid cursor.")
    return number


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def _public(row: dict[str, Any], *, nested: tuple[str, ...] = ()) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in row.items():
        if key == "cursor_id":
            continue
        if key in nested and isinstance(value, dict):
            value = _public(value)
        result[_camel(key)] = value
    return result


def _public_turn(row: dict[str, Any]) -> dict[str, Any]:
    return _public(row)


def _public_owner(row: dict[str, Any]) -> dict[str, Any]:
    return _public(row, nested=("last_error",))


class AdminRoutes:
    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.broker = handler.broker
        self.queries = AdminQueries(handler.broker.state)

    def _param(self, query: dict[str, list[str]], name: str) -> str | None:
        value = query.get(name, [None])[0]
        return value or None

    def _limit(self, query: dict[str, list[str]]) -> int:
        raw = self._param(query, "limit")
        return clamp_limit(int(raw) if raw else None)

    def dispatch(self, method: str, tail: list[str], query: dict[str, list[str]]) -> None:
        if method == "POST" and tail == ["owners", "lookup"]:
            self.owner_lookup()
            return
        if method != "GET":
            _send_json(self.handler, {"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        if tail == ["overview"]:
            self.overview()
        elif tail == ["owners"]:
            self.owners(query)
        elif len(tail) == 2 and tail[0] == "owners":
            self.owner_detail(tail[1])
        elif tail == ["turns"]:
            self.turns(query)
        elif tail == ["processes"]:
            self.processes(query)
        elif tail == ["audit"]:
            self.audit(query)
        else:
            _send_json(self.handler, {"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def overview(self) -> None:
        metrics = self.broker.scheduler.metrics()
        counts = self.queries.overview_counts()
        failures = counts.pop("recentFailedTurns")
        _send_json(
            self.handler,
            {
                "version": __version__,
                "startedAt": self.broker.started_at,
                "uptimeSeconds": round(time.time() - self.broker.started_at, 1),
                "readiness": self.handler._readiness(),
                "live": {_camel(key): metrics.get(key, 0) for key in LIVE_METRIC_KEYS},
                "maxActiveTurns": self.broker.config.max_active_turns,
                "maxPooledAppServers": self.broker.config.max_pooled_app_servers,
                **counts,
                "recentFailedTurns": [_public_turn(turn) for turn in failures],
            },
        )

    def owners(self, query: dict[str, list[str]]) -> None:
        offset = _decode_cursor(self._param(query, "cursor")) or 0
        limit = self._limit(query)
        page = self.queries.list_owners(offset=offset, limit=limit + 1)
        has_more = len(page) > limit
        page = page[:limit]
        _send_json(
            self.handler,
            {
                "items": [_public_owner(owner) for owner in page],
                "nextCursor": _encode_cursor(offset + limit) if has_more else None,
            },
        )

    def owner_detail(self, owner_hash: str) -> None:
        rows = self.queries.list_owners(offset=0, limit=1, owner_hash=owner_hash)
        if not rows:
            _send_json(self.handler, {"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        owner = rows[0]
        _send_json(
            self.handler,
            {
                "owner": _public_owner(owner),
                "threads": [_public(thread) for thread in self.queries.owner_threads(owner_hash)],
                "recentTurns": [
                    _public_turn(turn) for turn in self.queries.list_turns(before=None, limit=50, owner_hash=owner_hash)
                ],
                "authProfiles": [
                    _public(profile) for profile in self.queries.auth_profiles(owner["auth_principal_hashes"])
                ],
            },
        )

    def owner_lookup(self) -> None:
        body = self.handler._read_json()
        owner_id = body.get("ownerId")
        if not isinstance(owner_id, str) or not owner_id:
            raise ValueError("ownerId must be a non-empty string.")
        owner_hash = self.broker.auth.hash_owner(owner_id)
        _send_json(self.handler, {"ownerHash": owner_hash, "known": self.queries.owner_known(owner_hash)})

    def turns(self, query: dict[str, list[str]]) -> None:
        limit = self._limit(query)
        items = self.queries.list_turns(
            before=_decode_cursor(self._param(query, "cursor")),
            limit=limit + 1,
            status=self._param(query, "status"),
            owner_hash=self._param(query, "ownerHash"),
            error_code=self._param(query, "errorCode"),
            since=self._param(query, "since"),
        )
        self._keyset_page(items, limit, "cursor_id", _public_turn)

    def processes(self, query: dict[str, list[str]]) -> None:
        limit = self._limit(query)
        items = self.queries.list_processes(
            before=_decode_cursor(self._param(query, "cursor")),
            limit=limit + 1,
            status=self._param(query, "status"),
        )
        pool = self.broker.pool.metrics()
        self._keyset_page(items, limit, "id", _public, extra={"live": {_camel(k): v for k, v in pool.items()}})

    def audit(self, query: dict[str, list[str]]) -> None:
        limit = self._limit(query)
        cursor = _decode_cursor(self._param(query, "cursor"))
        items = self.queries.list_audit(
            before=cursor,
            limit=limit + 1,
            action=self._param(query, "action"),
            owner_hash=self._param(query, "ownerHash"),
        )
        extra = {"actionCounts": self.broker.state.count_audit_actions()} if cursor is None else {}
        self._keyset_page(items, limit, "id", _public, extra=extra)

    def _keyset_page(
        self,
        items: list[dict[str, Any]],
        limit: int,
        key: str,
        render: Any,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        has_more = len(items) > limit
        items = items[:limit]
        next_cursor = _encode_cursor(int(items[-1][key])) if has_more and items else None
        _send_json(
            self.handler,
            {**(extra or {}), "items": [render(item) for item in items], "nextCursor": next_cursor},
        )
