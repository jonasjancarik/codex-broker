# Integrating With The Broker

read_when: writing host application code, job workers, chat backends, hosted tool endpoints, or agents that call Codex Broker over HTTP.

This guide is the practical contract for code that interacts with Codex Broker. Use it when implementing a host backend or worker. The broker is an internal service; browser clients should call your product backend, and your backend should call the broker with the internal key.

## Mental Model

The host app owns users, product authorization, database records, UI state, prompts, and product-specific tools. The broker owns Codex process management, Codex credentials, broker thread state, one-active-turn locking, normalized events, and bundle mounting.

Use stable product IDs:

- `ownerId`: stable product user id or service account id. This scopes Codex auth.
- `profile`: Codex auth profile under the owner. Defaults to `default`.
- `threadId`: broker thread id returned by the broker. Host apps may supply a stable chat/job id as `threadId` on create, or omit it and let the broker generate one. Submit turns to this id.
- `turnId`: broker turn id returned by the broker. Use it for polling, streaming filters, steering, and interrupting.
- `productCorrelationId`: optional host id for tracing one product action through broker events.

## Authentication

All product API routes require one of these headers:

```http
Authorization: Bearer <CODEX_BROKER_INTERNAL_KEY>
X-Codex-Broker-Key: <CODEX_BROKER_INTERNAL_KEY>
```

Only `GET /healthz` and `GET /readyz` are unauthenticated. `/metrics`, `/openapi.json`, `/v1/...`, and `/v1/bundles/inline` require the broker key unless the broker is running with the explicit development override.

Keep the broker key on the server side. Do not expose it to browser JavaScript. Browser `EventSource` also cannot set the required auth header, so product UIs should consume a product-owned stream or API that proxies broker events.

## Minimal HTTP Flow

Set these examples to match your deployment:

```bash
BROKER=http://127.0.0.1:3400
BROKER_KEY=dev-only-key
OWNER=service-account-1
```

The curl examples use path-safe IDs. If an `ownerId`, `threadId`, or `turnId` contains characters such as `/` or spaces, URL-encode that path segment. The bundled Python and TypeScript clients do this for you.

Check Codex auth for the owner/profile:

```bash
curl -sS \
  -H "Authorization: Bearer $BROKER_KEY" \
  "$BROKER/v1/owners/$OWNER/auth/status?profile=default"
```

The `state` field is one of `missing`, `present_unverified`, `authenticated`, `invalid`, `refresh_failed`, `failed`, or `unknown`. `authFingerprint` changes when the owner/profile auth file changes, and pooled app-server children are keyed by that fingerprint so refreshed auth starts fresh runtime processes.

For service-account style deployments, store an API key in the owner profile:

```bash
curl -sS \
  -X POST \
  -H "Authorization: Bearer $BROKER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"profile":"default","apiKey":"'"$OPENAI_API_KEY"'"}' \
  "$BROKER/v1/owners/$OWNER/auth/api-key"
```

If an administrator refreshes shared auth outside the broker, close the profile's pooled app-server children before retrying failed work:

```bash
curl -sS \
  -X POST \
  -H "Authorization: Bearer $BROKER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"profile":"default"}' \
  "$BROKER/v1/owners/$OWNER/auth/runtime/invalidate"
```

Create or reuse a broker thread:

```bash
curl -sS \
  -X POST \
  -H "Authorization: Bearer $BROKER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "threadId": "chat-123",
    "hostApp": "chat-app",
    "bundleId": "example-chat-v1",
    "configProfile": "default",
    "cwd": "/workspaces/app"
  }' \
  "$BROKER/v1/owners/$OWNER/threads"
```

Response shape:

```json
{
  "threadId": "chat-123",
  "codexThreadId": null,
  "profile": "default",
  "configProfile": "default",
  "hostApp": "chat-app",
  "bundleId": "example-chat-v1",
  "cwd": "/workspaces/app",
  "status": "active",
  "createdAt": "2026-06-28T12:00:00Z",
  "updatedAt": "2026-06-28T12:00:00Z"
}
```

Submit a turn:

```bash
THREAD_ID=chat-123

curl -sS \
  -X POST \
  -H "Authorization: Bearer $BROKER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "input": [
      { "type": "text", "text": "Summarize the evidence for this user question." }
    ],
    "hostApp": "chat-app",
    "bundleId": "example-chat-v1",
    "configProfile": "default",
    "mode": "queue",
    "productCorrelationId": "chat-123:message-456",
    "idempotencyKey": "chat-123:message-456"
  }' \
  "$BROKER/v1/owners/$OWNER/threads/$THREAD_ID/turns"
```

Response shape:

```json
{
  "threadId": "chat-123",
  "turnId": "turn_...",
  "codexTurnId": null,
  "profile": "default",
  "configProfile": "default",
  "hostApp": "chat-app",
  "bundleId": "example-chat-v1",
  "cwd": "/workspaces/app",
  "mode": "queue",
  "productCorrelationId": "chat-123:message-456",
  "status": "queued",
  "error": null,
  "errorCode": null,
  "publicMessage": null,
  "adminMessage": null,
  "createdAt": "2026-06-28T12:00:01Z",
  "startedAt": null,
  "completedAt": null,
  "updatedAt": "2026-06-28T12:00:01Z",
  "streamUrl": "/v1/owners/service-account-1/threads/chat-123/events?turnId=turn_..."
}
```

`streamUrl` is relative to the broker base URL and already filters to the submitted turn. It does not include the auth header; your client must still send broker authentication.

Stream events:

```bash
TURN_ID=turn_...

curl -N \
  -H "Authorization: Bearer $BROKER_KEY" \
  "$BROKER/v1/owners/$OWNER/threads/$THREAD_ID/events?turnId=$TURN_ID&after=0"
```

Poll the turn when streaming is not convenient:

```bash
curl -sS \
  -H "Authorization: Bearer $BROKER_KEY" \
  "$BROKER/v1/owners/$OWNER/threads/$THREAD_ID/turns/$TURN_ID"
```

Terminal turn statuses are `completed`, `failed`, `timed_out`, and `interrupted`. Nonterminal statuses are `starting`, `queued`, and `running`.

On failed turns, `error` remains the legacy display field. New integrations should prefer `publicMessage` for end-user UI, use `errorCode` for programmatic handling, and keep `adminMessage` for admin-only logs. For shared Codex auth refresh failures, `errorCode` is `codex_auth_requires_admin` and `publicMessage` tells the user to wait for administrator attention instead of asking them to log out. For missing Codex rollout/session failures, `errorCode` is `session_not_resumable`; host apps should start a new broker thread and reconstruct context from persisted workspace files.

## Python Client

Use the bundled Python client when the host code can import this package:

```python
from codex_broker.client import CodexBrokerClient

broker = CodexBrokerClient(
    "http://127.0.0.1:3400",
    internal_key="dev-only-key",
    timeout_seconds=60,
)

owner_id = "service-account-1"

thread = broker.create_thread(
    owner_id,
    {
        "threadId": "chat-123",
        "hostApp": "chat-app",
        "bundleId": "example-chat-v1",
        "configProfile": "default",
        "cwd": "/workspaces/app",
    },
)

turn = broker.start_turn(
    owner_id,
    thread["threadId"],
    {
        "input": [{"type": "text", "text": "Summarize the evidence."}],
        "mode": "queue",
        "productCorrelationId": "chat-123:message-456",
        "idempotencyKey": "chat-123:message-456",
    },
)

for event in broker.stream_events(owner_id, thread["threadId"], turn_id=turn["turnId"]):
    event_type = event["type"]
    payload = event.get("payload", {})

    if event_type == "message.delta":
        print(payload.get("delta") or "", end="", flush=True)

    if event_type in {"turn.completed", "turn.failed", "turn.interrupted"}:
        break

final_turn = broker.get_turn(owner_id, thread["threadId"], turn["turnId"])
print(final_turn["status"])
```

The Python client returns plain dictionaries and raises `CodexBrokerClientError` for HTTP errors.

## TypeScript Client

The repository includes a lightweight TypeScript client at `clients/typescript/codex-broker-client.ts`. It is a source file, not an npm package. It wraps JSON requests and exposes an event URL helper.

```ts
import { CodexBrokerClient } from "./codex-broker-client";

const broker = new CodexBrokerClient({
  baseUrl: "http://127.0.0.1:3400",
  internalKey: "dev-only-key",
});

const ownerId = "service-account-1";

const thread = await broker.createThread(ownerId, {
  threadId: "chat-123",
  hostApp: "chat-app",
  bundleId: "example-chat-v1",
  configProfile: "default",
  cwd: "/workspaces/app",
});

const turn = await broker.startTurn(ownerId, String(thread["threadId"]), {
  input: [{ type: "text", text: "Summarize the evidence." }],
  mode: "queue",
  productCorrelationId: "chat-123:message-456",
  idempotencyKey: "chat-123:message-456",
});
```

In Node or server runtimes, stream SSE with `fetch` so you can send the auth header:

```ts
async function* streamBrokerEvents(
  baseUrl: string,
  streamPath: string,
  internalKey: string,
): AsyncGenerator<Record<string, unknown>> {
  const response = await fetch(`${baseUrl}${streamPath}`, {
    headers: {
      Accept: "text/event-stream",
      Authorization: `Bearer ${internalKey}`,
    },
  });

  if (!response.ok || !response.body) {
    throw new Error(`Broker stream failed: ${response.status} ${await response.text()}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    for (;;) {
      const boundary = buffer.indexOf("\n\n");
      if (boundary === -1) break;
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);

      let id: string | undefined;
      let event: string | undefined;
      const data: string[] = [];

      for (const line of frame.split("\n")) {
        if (!line || line.startsWith(":")) continue;
        const colon = line.indexOf(":");
        const field = colon === -1 ? line : line.slice(0, colon);
        const valueText = colon === -1 ? "" : line.slice(colon + 1).trimStart();
        if (field === "id") id = valueText;
        if (field === "event") event = valueText;
        if (field === "data") data.push(valueText);
      }

      if (data.length) {
        const payload = JSON.parse(data.join("\n")) as Record<string, unknown>;
        if (id && payload["id"] === undefined) payload["id"] = Number(id);
        if (event && payload["type"] === undefined) payload["type"] = event;
        yield payload;
      }
    }
  }
}

for await (const event of streamBrokerEvents(
  "http://127.0.0.1:3400",
  String(turn["streamUrl"]),
  "dev-only-key",
)) {
  if (event["type"] === "message.delta") {
    const payload = event["payload"] as { delta?: string };
    process.stdout.write(payload.delta ?? "");
  }

  if (["turn.completed", "turn.failed", "turn.interrupted"].includes(String(event["type"]))) {
    break;
  }
}
```

## SSE Event Contract

The `/events` endpoint returns `text/event-stream`. Each broker event is sent as:

```text
id: 12
event: message.delta
data: {"id":12,"type":"message.delta","threadId":"thr_...","turnId":"turn_...","payload":{"delta":"hi"},"ambiguous":false}

```

The JSON `data` object has this stable envelope:

```json
{
  "id": 12,
  "type": "message.delta",
  "ownerHash": "owner_hash_...",
  "threadId": "thr_...",
  "turnId": "turn_...",
  "productCorrelationId": "chat-123:message-456",
  "codexThreadId": "codex_thread_...",
  "codexTurnId": "codex_turn_...",
  "createdAt": "2026-06-28T12:00:02Z",
  "payload": {},
  "ambiguous": false
}
```

Use `after=<last event id>` to resume a stream without replaying already processed events. Use `turnId=<turn id>` to filter a thread stream to one turn. The broker validates `threadId` and `turnId` before opening the stream; unknown ids return JSON 404 instead of a partial SSE stream.

Common event types:

| Type | Payload |
| --- | --- |
| `thread.started`, `thread.resumed`, `thread.status.changed`, `thread.settings.updated` | Codex thread lifecycle and settings. |
| `turn.started`, `turn.completed`, `turn.failed`, `turn.interrupted` | Turn lifecycle. Treat completed, failed, and interrupted as terminal for streaming loops. |
| `message.delta` | `{ "delta": "..." }` for assistant output deltas. Steering also emits `message.delta` with `{ "steered": true, "input": [...] }`. |
| `message.completed` | Completed Codex agent message item. |
| `reasoning.summary.started`, `reasoning.summary.delta`, `reasoning.completed` | Reasoning summary lifecycle and deltas. |
| `tool.started`, `tool.output.delta`, `tool.completed`, `tool.requested` | Tool lifecycle and output. |
| `approval.requested`, `approval.resolved` | Approval request lifecycle. Request payloads include `interactionId`; resolved payloads include `source` and `response`. |
| `user_input.requested`, `user_input.resolved` | User-input request lifecycle. Request payloads include `interactionId`; resolved payloads include `source`, `answers`, and `response`. |
| `mcp.elicitation.requested`, `mcp.elicitation.resolved` | MCP elicitation lifecycle. Request payloads include `interactionId`; resolved payloads include `source`, `action`, and `response`. |
| `plan.updated`, `plan.delta`, `goal.updated`, `goal.cleared`, `review.entered`, `review.exited` | Plan, goal, and review-mode surfaces normalized from Codex app-server notifications. |
| `error` | Normalized Codex app-server error payload. |

When `ambiguous` is `true`, the broker attached an early app-server notification to the best known active context before all Codex metadata was available. Host consumers may display these events, but should avoid using the Codex ids in them as authoritative.

## Host-Resolved Interactions

Approval, user-input, and MCP elicitation request events are backed by persisted interaction records. A host can list them with `GET /v1/owners/{ownerId}/threads/{threadId}/interactions`, optionally filtered by `turnId` or `status`. To answer a request, call:

```http
POST /v1/owners/{ownerId}/threads/{threadId}/turns/{turnId}/interactions/{interactionId}/resolve
```

Use the app-server response shape for the original method: approval `decision`, permission `permissions`, user-input `answers`, or MCP `action`/`content`/`_meta`. If the host does not resolve before `CODEX_BROKER_HOST_RESPONSE_TIMEOUT_SECONDS`, the broker resolves with fail-closed defaults and marks `resolutionSource` as a fallback source.

## Turn Concurrency

The broker allows one active turn per broker thread:

- `mode: "reject"` returns HTTP 409 with `{"error":"active_turn_exists"}` when another turn is active.
- `mode: "queue"` waits until the current same-thread turn finishes, then runs the new turn.
- `mode: "steer"` sends input into the active turn. If there is no steerable active turn, it behaves like `reject`.

Use `idempotencyKey` for host retries. Repeating the same owner, thread, and idempotency key returns the original turn instead of creating duplicate Codex work.

Different broker threads can run concurrently. Different owners can run concurrently with isolated Codex auth homes.

## Hosted Tool Endpoint Contract

Bundles can declare broker-hosted tools. The broker exposes those tools to Codex through a mounted MCP adapter, then forwards tool calls to host-owned HTTP endpoints.

The host endpoint receives a POST request with broker-added headers:

```http
Content-Type: application/json
Accept: application/json
X-Codex-Broker-Tool: host.evidence.search
```

It also receives any headers declared by the bundle, with `env:VAR` values resolved from the broker process environment.

Request body:

```json
{
  "tool": "host.evidence.search",
  "arguments": {
    "query": "refund policy",
    "limit": 5
  },
  "context": {
    "broker": {
      "ownerHash": "owner_hash_...",
      "profile": "default",
      "threadId": "thr_...",
      "turnId": "turn_...",
      "hostApp": "chat-app",
      "configProfile": "default",
      "productCorrelationId": "chat-123:message-456"
    },
    "tool": {
      "capability": "evidence-search"
    },
    "policy": {
      "approvalPolicy": "on-request",
      "scope": "profile",
      "networkPolicy": {
        "mode": "host-allowlist",
        "matchedPrefix": "http://app:3000"
      }
    }
  }
}
```

Host endpoints should validate the declared secret header and perform final product authorization. Do not treat `ownerHash` alone as proof that a product user is allowed to access data.

Response options:

- Return ordinary JSON or text when Codex only needs a textual representation. JSON is formatted and exposed to Codex as text.
- Return an MCP tool result shape when the host needs structured output, artifacts, or metadata to survive the adapter boundary.

MCP tool result response:

```json
{
  "content": [
    { "type": "text", "text": "Found 3 matching evidence records." }
  ],
  "isError": false,
  "structuredContent": {
    "records": [
      { "id": "ev_1", "title": "Refund policy", "score": 0.91 }
    ]
  },
  "_meta": {
    "source": "host-app"
  }
}
```

HTTP error responses become tool errors for Codex. The response body is exposed as the error text.

## Error Handling

Broker HTTP errors use JSON:

```json
{ "error": "Thread not found." }
```

Common statuses:

| Status | Meaning |
| --- | --- |
| 400 | Invalid request body, invalid mode, invalid bundle, invalid `cwd`, or policy validation failure. |
| 401 | Missing or invalid broker key. |
| 404 | Unknown route, thread, turn, or stream filter target. |
| 409 | Active turn conflict, archived thread, shutdown, or other conflict. |
| 502 | Codex app-server request failed. |
| 500 | Unexpected broker error. |

## Production Checklist

- Call the broker only from trusted server-side code.
- Choose stable `ownerId` values and avoid raw secrets or emails when a service-account id is enough.
- Store broker `threadId` next to product chat/job records.
- Send a caller-owned `threadId` on thread creation when you want idempotent host chat or job mapping.
- Send `productCorrelationId` and `idempotencyKey` on every retriable product action.
- Stream with `after=<last id>` when reconnecting.
- Handle terminal events and terminal turn statuses.
- Validate hosted-tool secret headers in the host app.
- Keep product authorization inside the host app, including hosted tool endpoints.
- Prefer configuration profiles and reviewed bundles over per-request ad hoc behavior.
