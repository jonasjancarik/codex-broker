"""Read-only, cross-owner queries for the operator dashboard.

These queries deliberately select explicit columns. Conversation content
(`turns.input_json`, event payloads, interaction bodies) must never be read
here, so it can never be returned by the admin API.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .state import StateStore
from .util import json_loads

MAX_LIMIT = 200
DEFAULT_LIMIT = 50

_TURN_COLUMNS = """
  rowid as cursor_id, owner_hash, thread_id, turn_id, auth_principal_hash, profile,
  config_profile, host_app, bundle_id, mode, status, error_code, public_message,
  admin_message, broker_version, created_at, started_at, completed_at, updated_at
"""
_FAILED_STATUSES = ("failed", "timed_out")


def clamp_limit(value: int | None) -> int:
    if value is None:
        return DEFAULT_LIMIT
    return max(1, min(int(value), MAX_LIMIT))


def iso_ago(seconds: float) -> str:
    moment = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return moment.isoformat().replace("+00:00", "Z")


class AdminQueries:
    def __init__(self, store: StateStore) -> None:
        self._store = store

    def _rows(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> list[sqlite3.Row]:
        with self._store._lock:
            return self._store._conn.execute(sql, params).fetchall()

    def _turn(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for field in ("public_message", "admin_message"):
            if data.get(field) is not None:
                data[field] = self._store._sanitize(data[field])
        return data

    def overview_counts(self) -> dict[str, Any]:
        day_ago = iso_ago(24 * 60 * 60)
        week_ago = iso_ago(7 * 24 * 60 * 60)
        threads = self._rows("select status, count(*) as n from threads group by status")
        turns = self._rows(
            "select status, count(*) as n from turns where created_at >= ? group by status",
            (day_ago,),
        )
        pending = self._rows("select count(*) as n from pending_interactions where status = 'pending'")
        owners_day = self._rows("select count(distinct owner_hash) as n from turns where created_at >= ?", (day_ago,))
        owners_week = self._rows("select count(distinct owner_hash) as n from turns where created_at >= ?", (week_ago,))
        owners_total = self._rows("select count(distinct owner_hash) as n from threads")
        recent_failures = self._rows(
            f"""
            select {_TURN_COLUMNS} from turns
            where status in ({",".join("?" for _ in _FAILED_STATUSES)}) and created_at >= ?
            order by created_at desc limit 10
            """,
            (*_FAILED_STATUSES, day_ago),
        )
        return {
            "threadsByStatus": {str(row["status"]): int(row["n"]) for row in threads},
            "turnsLast24hByStatus": {str(row["status"]): int(row["n"]) for row in turns},
            "pendingInteractions": int(pending[0]["n"]),
            "ownersActiveLast24h": int(owners_day[0]["n"]),
            "ownersActiveLast7d": int(owners_week[0]["n"]),
            "ownersTotal": int(owners_total[0]["n"]),
            "recentFailedTurns": [self._turn(row) for row in recent_failures],
        }

    def list_owners(self, *, offset: int, limit: int, owner_hash: str | None = None) -> list[dict[str, Any]]:
        where = "where owner_hash = ?" if owner_hash else ""
        params: list[Any] = [owner_hash] if owner_hash else []
        rows = self._rows(
            f"""
            select owner_hash, min(created_at) as first_seen, max(updated_at) as thread_updated,
                   count(*) as thread_count
            from threads {where}
            group by owner_hash
            """,
            params,
        )
        if not rows:
            return []
        owners = {str(row["owner_hash"]): dict(row) for row in rows}
        turn_activity = self._rows(
            f"select owner_hash, max(updated_at) as turn_updated from turns {where} group by owner_hash",
            params,
        )
        for row in turn_activity:
            if row["owner_hash"] in owners:
                owners[row["owner_hash"]]["turn_updated"] = row["turn_updated"]
        ordered = sorted(
            owners.values(),
            key=lambda item: (max(item["thread_updated"] or "", item.get("turn_updated") or ""), item["owner_hash"]),
            reverse=True,
        )
        page = ordered[offset : offset + limit]
        self._attach_owner_details(page)
        return page

    def _attach_owner_details(self, owners: list[dict[str, Any]]) -> None:
        if not owners:
            return
        hashes = [owner["owner_hash"] for owner in owners]
        marks = ",".join("?" for _ in hashes)
        day_ago = iso_ago(24 * 60 * 60)
        turn_counts = self._rows(
            f"""
            select owner_hash, status, count(*) as n from turns
            where owner_hash in ({marks}) and created_at >= ?
            group by owner_hash, status
            """,
            (*hashes, day_ago),
        )
        principals = self._rows(
            f"select distinct owner_hash, auth_principal_hash from threads where owner_hash in ({marks})",
            hashes,
        )
        last_errors = self._rows(
            f"""
            select owner_hash, error_code, created_at from turns as t
            where owner_hash in ({marks}) and error_code is not null
              and created_at = (
                select max(created_at) from turns
                where owner_hash = t.owner_hash and error_code is not null
              )
            """,
            hashes,
        )
        by_hash = {owner["owner_hash"]: owner for owner in owners}
        for owner in owners:
            owner["last_activity"] = max(owner.pop("thread_updated") or "", owner.pop("turn_updated", None) or "")
            owner["turns_last_24h_by_status"] = {}
            owner["auth_principal_hashes"] = []
            owner["last_error"] = None
        for row in turn_counts:
            by_hash[row["owner_hash"]]["turns_last_24h_by_status"][row["status"]] = int(row["n"])
        for row in principals:
            by_hash[row["owner_hash"]]["auth_principal_hashes"].append(row["auth_principal_hash"])
        for row in last_errors:
            by_hash[row["owner_hash"]]["last_error"] = {"error_code": row["error_code"], "created_at": row["created_at"]}

    def owner_threads(self, owner_hash: str, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._rows(
            """
            select thread_id, auth_principal_hash, profile, config_profile, host_app, bundle_id,
                   status, created_at, updated_at
            from threads where owner_hash = ?
            order by updated_at desc limit ?
            """,
            (owner_hash, limit),
        )
        return [dict(row) for row in rows]

    def auth_profiles(self, principal_hashes: list[str]) -> list[dict[str, Any]]:
        if not principal_hashes:
            return []
        marks = ",".join("?" for _ in principal_hashes)
        rows = self._rows(
            f"""
            select auth_principal_hash, profile, auth_type, auth_status, auth_fingerprint,
                   created_at, updated_at
            from auth_profiles where auth_principal_hash in ({marks})
            order by updated_at desc
            """,
            principal_hashes,
        )
        return [dict(row) for row in rows]

    def owner_known(self, owner_hash: str) -> bool:
        rows = self._rows("select 1 from threads where owner_hash = ? limit 1", (owner_hash,))
        return bool(rows)

    def list_turns(
        self,
        *,
        before: int | None,
        limit: int,
        status: str | None = None,
        owner_hash: str | None = None,
        error_code: str | None = None,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if before is not None:
            where.append("rowid < ?")
            params.append(before)
        if status:
            where.append("status = ?")
            params.append(status)
        if owner_hash:
            where.append("owner_hash = ?")
            params.append(owner_hash)
        if error_code:
            where.append("error_code = ?")
            params.append(error_code)
        if since:
            where.append("created_at >= ?")
            params.append(since)
        clause = f"where {' and '.join(where)}" if where else ""
        params.append(limit)
        rows = self._rows(f"select {_TURN_COLUMNS} from turns {clause} order by rowid desc limit ?", params)
        return [self._turn(row) for row in rows]

    def list_processes(self, *, before: int | None, limit: int, status: str | None = None) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if before is not None:
            where.append("id < ?")
            params.append(before)
        if status:
            where.append("status = ?")
            params.append(status)
        clause = f"where {' and '.join(where)}" if where else ""
        params.append(limit)
        rows = self._rows(
            f"""
            select id, pool_key_hash, auth_principal_hash, profile, config_profile, pid, status,
                   started_at, last_seen_at, closed_at, exit_code
            from app_server_processes {clause}
            order by id desc limit ?
            """,
            params,
        )
        return [dict(row) for row in rows]

    def list_audit(
        self,
        *,
        before: int | None,
        limit: int,
        action: str | None = None,
        owner_hash: str | None = None,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if before is not None:
            where.append("id < ?")
            params.append(before)
        if action:
            where.append("action = ?")
            params.append(action)
        if owner_hash:
            where.append("owner_hash = ?")
            params.append(owner_hash)
        clause = f"where {' and '.join(where)}" if where else ""
        params.append(limit)
        rows = self._rows(
            f"""
            select id, owner_hash, auth_principal_hash, profile, thread_id, turn_id, action,
                   payload_json, created_at
            from audit_logs {clause}
            order by id desc limit ?
            """,
            params,
        )
        entries = []
        for row in rows:
            data = dict(row)
            data["payload"] = self._store._sanitize(json_loads(data.pop("payload_json"), {}))
            entries.append(data)
        return entries
