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

    def list_auth_profiles(
        self,
        owner_id: str,
        *,
        auth_principal_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/owners/{quote(owner_id)}/auth/profiles",
            query=auth_query(auth_principal_id=auth_principal_id),
        )

    def auth_status(
        self,
        owner_id: str,
        *,
        profile: str = "default",
        auth_principal_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/owners/{quote(owner_id)}/auth/status",
            query=auth_query(profile, auth_principal_id),
        )

    def account_usage(
        self,
        owner_id: str,
        *,
        profile: str = "default",
        auth_principal_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/owners/{quote(owner_id)}/auth/usage",
            query=auth_query(profile, auth_principal_id),
        )

    def account_rate_limits(
        self,
        owner_id: str,
        *,
        profile: str = "default",
        auth_principal_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/owners/{quote(owner_id)}/auth/rate-limits",
            query=auth_query(profile, auth_principal_id),
        )

    def consume_rate_limit_reset_credit(
        self,
        owner_id: str,
        idempotency_key: str,
        *,
        profile: str = "default",
        auth_principal_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/owners/{quote(owner_id)}/auth/rate-limit-reset-credit/consume",
            with_auth_selection(
                {"idempotencyKey": idempotency_key},
                profile=profile,
                auth_principal_id=auth_principal_id,
            ),
        )

    def probe_auth(
        self,
        owner_id: str,
        *,
        profile: str = "default",
        auth_principal_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/owners/{quote(owner_id)}/auth/probe",
            with_auth_selection({}, profile=profile, auth_principal_id=auth_principal_id),
        )

    def start_device_auth(
        self,
        owner_id: str,
        *,
        profile: str = "default",
        auth_principal_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/owners/{quote(owner_id)}/auth/device/start",
            with_auth_selection({}, profile=profile, auth_principal_id=auth_principal_id),
        )

    def submit_device_code(
        self,
        owner_id: str,
        code: str,
        *,
        profile: str = "default",
        session_id: str | None = None,
        auth_principal_id: str | None = None,
    ) -> dict[str, Any]:
        body = with_auth_selection({"code": code}, profile=profile, auth_principal_id=auth_principal_id)
        if session_id:
            body["sessionId"] = session_id
        return self._request("POST", f"/v1/owners/{quote(owner_id)}/auth/device/submit", body)

    def login_api_key(
        self,
        owner_id: str,
        api_key: str,
        *,
        profile: str = "default",
        auth_principal_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/owners/{quote(owner_id)}/auth/api-key",
            with_auth_selection({"apiKey": api_key}, profile=profile, auth_principal_id=auth_principal_id),
        )

    def invalidate_auth_runtime(
        self,
        owner_id: str,
        *,
        profile: str = "default",
        auth_principal_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/owners/{quote(owner_id)}/auth/runtime/invalidate",
            with_auth_selection({}, profile=profile, auth_principal_id=auth_principal_id),
        )

    def logout(
        self,
        owner_id: str,
        *,
        profile: str = "default",
        delete_profile: bool = False,
        auth_principal_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/owners/{quote(owner_id)}/auth/logout",
            with_auth_selection(
                {"deleteProfile": delete_profile},
                profile=profile,
                auth_principal_id=auth_principal_id,
            ),
        )

    def list_audit_logs(
        self,
        owner_id: str,
        *,
        profile: str | None = None,
        action: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        after: int = 0,
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
        if after > 0:
            query["after"] = str(after)
        if limit is not None:
            query["limit"] = str(limit)
        return self._request("GET", f"/v1/owners/{quote(owner_id)}/audit-logs", query=query)

    def create_thread(
        self,
        owner_id: str,
        body: dict[str, Any] | None = None,
        *,
        profile: str | None = None,
        auth_principal_id: str | None = None,
    ) -> dict[str, Any]:
        payload = with_auth_selection(
            body or {},
            profile=profile,
            auth_principal_id=auth_principal_id,
        )
        return self._request("POST", f"/v1/owners/{quote(owner_id)}/threads", payload)

    def get_thread(self, owner_id: str, thread_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/owners/{quote(owner_id)}/threads/{quote(thread_id)}")

    def archive_thread(self, owner_id: str, thread_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/owners/{quote(owner_id)}/threads/{quote(thread_id)}/archive", {})

    def start_turn(
        self,
        owner_id: str,
        thread_id: str,
        body: dict[str, Any],
        *,
        profile: str | None = None,
        auth_principal_id: str | None = None,
    ) -> dict[str, Any]:
        payload = with_auth_selection(
            body,
            profile=profile,
            auth_principal_id=auth_principal_id,
        )
        return self._request("POST", f"/v1/owners/{quote(owner_id)}/threads/{quote(thread_id)}/turns", payload)

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


def auth_query(
    profile: str | None = None,
    auth_principal_id: str | None = None,
) -> dict[str, str]:
    query: dict[str, str] = {}
    if profile is not None:
        query["profile"] = profile
    if auth_principal_id is not None:
        query["authPrincipalId"] = auth_principal_id
    return query


def with_auth_selection(
    body: dict[str, Any],
    *,
    profile: str | None,
    auth_principal_id: str | None,
) -> dict[str, Any]:
    payload = dict(body)
    for key, value in (("profile", profile), ("authPrincipalId", auth_principal_id)):
        if value is None:
            continue
        if key in payload and payload[key] != value:
            raise ValueError(f"Conflicting {key} values.")
        payload[key] = value
    return payload
