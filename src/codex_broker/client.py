from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any


class CodexBrokerClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class CodexBrokerClient:
    base_url: str
    internal_key: str | None = None
    timeout_seconds: float = 60

    def auth_status(self, owner_id: str, *, profile: str = "default") -> dict[str, Any]:
        return self._request("GET", f"/v1/owners/{quote(owner_id)}/auth/status", query={"profile": profile})

    def probe_auth(self, owner_id: str, *, profile: str = "default") -> dict[str, Any]:
        return self._request("POST", f"/v1/owners/{quote(owner_id)}/auth/probe", {"profile": profile})

    def start_device_auth(self, owner_id: str, *, profile: str = "default") -> dict[str, Any]:
        return self._request("POST", f"/v1/owners/{quote(owner_id)}/auth/device/start", {"profile": profile})

    def submit_device_code(
        self,
        owner_id: str,
        code: str,
        *,
        profile: str = "default",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"profile": profile, "code": code}
        if session_id:
            body["sessionId"] = session_id
        return self._request("POST", f"/v1/owners/{quote(owner_id)}/auth/device/submit", body)

    def login_api_key(self, owner_id: str, api_key: str, *, profile: str = "default") -> dict[str, Any]:
        return self._request("POST", f"/v1/owners/{quote(owner_id)}/auth/api-key", {"profile": profile, "apiKey": api_key})

    def logout(self, owner_id: str, *, profile: str = "default", delete_profile: bool = False) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/owners/{quote(owner_id)}/auth/logout",
            {"profile": profile, "deleteProfile": delete_profile},
        )

    def list_audit_logs(
        self,
        owner_id: str,
        *,
        profile: str | None = None,
        action: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        query: dict[str, str] = {}
        if profile is not None:
            query["profile"] = profile
        if action is not None:
            query["action"] = action
        if thread_id is not None:
            query["threadId"] = thread_id
        if turn_id is not None:
            query["turnId"] = turn_id
        if limit is not None:
            query["limit"] = str(limit)
        return self._request("GET", f"/v1/owners/{quote(owner_id)}/audit-logs", query=query)

    def create_thread(self, owner_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("POST", f"/v1/owners/{quote(owner_id)}/threads", body or {})

    def get_thread(self, owner_id: str, thread_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/owners/{quote(owner_id)}/threads/{quote(thread_id)}")

    def archive_thread(self, owner_id: str, thread_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/owners/{quote(owner_id)}/threads/{quote(thread_id)}/archive", {})

    def start_turn(self, owner_id: str, thread_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/v1/owners/{quote(owner_id)}/threads/{quote(thread_id)}/turns", body)

    def get_turn(self, owner_id: str, thread_id: str, turn_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/owners/{quote(owner_id)}/threads/{quote(thread_id)}/turns/{quote(turn_id)}")

    def steer_turn(self, owner_id: str, thread_id: str, turn_id: str, input_items: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/owners/{quote(owner_id)}/threads/{quote(thread_id)}/turns/{quote(turn_id)}/steer",
            {"input": input_items},
        )

    def interrupt_turn(self, owner_id: str, thread_id: str, turn_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/owners/{quote(owner_id)}/threads/{quote(thread_id)}/turns/{quote(turn_id)}/interrupt",
            {},
        )

    def list_interactions(
        self,
        owner_id: str,
        thread_id: str,
        *,
        turn_id: str | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        query: dict[str, str] = {}
        if turn_id is not None:
            query["turnId"] = turn_id
        if status is not None:
            query["status"] = status
        if limit is not None:
            query["limit"] = str(limit)
        return self._request("GET", f"/v1/owners/{quote(owner_id)}/threads/{quote(thread_id)}/interactions", query=query)

    def resolve_interaction(
        self,
        owner_id: str,
        thread_id: str,
        turn_id: str,
        interaction_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            (
                f"/v1/owners/{quote(owner_id)}/threads/{quote(thread_id)}/turns/{quote(turn_id)}"
                f"/interactions/{quote(interaction_id)}/resolve"
            ),
            body,
        )

    def stream_events(
        self,
        owner_id: str,
        thread_id: str,
        *,
        after: int = 0,
        turn_id: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        query: dict[str, str] = {"after": str(after)}
        if turn_id:
            query["turnId"] = turn_id
        url = self._url(f"/v1/owners/{quote(owner_id)}/threads/{quote(thread_id)}/events", query)
        req = urllib.request.Request(url, method="GET", headers=self._headers())
        with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
            event_type: str | None = None
            event_id: str | None = None
            data_lines: list[str] = []
            for raw in response:
                line = raw.decode("utf-8").rstrip("\n")
                if line == "":
                    if data_lines:
                        payload = json.loads("\n".join(data_lines))
                        if event_type:
                            payload.setdefault("type", event_type)
                        if event_id:
                            payload.setdefault("id", int(event_id))
                        yield payload
                    event_type = None
                    event_id = None
                    data_lines = []
                    continue
                if line.startswith(":"):
                    continue
                field, _, value = line.partition(":")
                value = value[1:] if value.startswith(" ") else value
                if field == "event":
                    event_type = value
                elif field == "id":
                    event_id = value
                elif field == "data":
                    data_lines.append(value)

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        query: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = self._headers()
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self._url(path, query), data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace")
            raise CodexBrokerClientError(f"{exc.code} {exc.reason}: {payload}") from exc
        parsed = json.loads(payload)
        if not isinstance(parsed, dict):
            raise CodexBrokerClientError("Broker returned a non-object JSON response.")
        return parsed

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.internal_key:
            headers["Authorization"] = f"Bearer {self.internal_key}"
        return headers

    def _url(self, path: str, query: dict[str, str] | None = None) -> str:
        base = self.base_url.rstrip("/")
        suffix = path if path.startswith("/") else f"/{path}"
        if query:
            return f"{base}{suffix}?{urllib.parse.urlencode(query)}"
        return f"{base}{suffix}"


def quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")
