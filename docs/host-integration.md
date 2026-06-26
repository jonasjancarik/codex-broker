# Host Integration

The broker API is intentionally product-facing. Host apps keep product identity, authorization, data models, UI, and app-specific tool semantics. The broker owns the generic Codex work that every integration otherwise has to repeat: Codex process management, credential homes, thread locking, event streaming, and the Codex-facing interface for mounted bundles, MCP servers, or broker-hosted adapters.

## Shared Request Shape

Host apps should send stable product identifiers as `ownerId`, an optional `productThreadId` when creating a broker thread, `hostApp`, and a product correlation id for tracing:

```json
{
  "productThreadId": "chat-123",
  "hostApp": "chat-app",
  "bundleId": "example-chat-v1",
  "configProfile": "default",
  "cwd": "/workspaces/app"
}
```

Repeated thread creates with the same owner and `productThreadId` return the existing broker thread. Store the returned broker `threadId` for turn submission; the broker keeps `productThreadId`, broker `threadId`, and Codex `codexThreadId` linked after Codex creates or resumes the Codex thread.

Submit turns with the broker `threadId` and a product correlation id for tracing:

```json
{
  "input": [{ "type": "text", "text": "Summarize the sources." }],
  "hostApp": "chat-app",
  "bundleId": "example-chat-v1",
  "configProfile": "default",
  "cwd": "/workspaces/app",
  "mode": "reject",
  "productCorrelationId": "chat-turn-123",
  "idempotencyKey": "chat-turn-123"
}
```

Use `mode=reject` or `mode=steer` for live chat and `mode=queue` for job workers.

Use `idempotencyKey` for host retries. A repeated turn create with the same owner, broker `threadId`, and idempotency key returns the original broker turn without creating a second Codex turn.

`configProfile` is the canonical profile field. The broker also accepts the legacy `runtimeProfile` field as an alias for host integrations that were written against earlier broker drafts. `codexOptions` is the canonical per-request Codex options object. The broker also accepts `runtime` as an alias and normalizes common option aliases such as `reasoningEffort` to `effort` and `reasoningSummary` to `summary`.

Some Codex options affect the app-server child process rather than a single `turn/start` request. The broker launches and pools app-server children separately when `webSearch`, `modelVerbosity`, `imageGeneration`, or reasoning-effort process config differs, so one host turn cannot accidentally reuse a child started with incompatible runtime settings.

Profile ids are canonicalized before they are used for auth state or filesystem paths. Characters outside `A-Z`, `a-z`, `0-9`, `_`, `.`, and `-` are replaced with `_`, so host apps should treat the returned `profile` value as the broker's canonical profile id.

Auth logout removes Codex credentials for an owner/profile and closes pooled app-server children for that profile only. Other profiles for the same owner continue running. Passing `deleteProfile: true` also deletes the broker-managed profile directory and profile metadata while preserving thread/turn history.

Device-auth responses include `loginUrl`, `userCode`, `expiresAt`, and current `state` when the Codex CLI exposes them. `expiresAt` is `null` when no expiry can be inferred.

Owner-scoped audit logs are available from `GET /v1/owners/{ownerId}/audit-logs`, with optional `profile`, `action`, `threadId`, `turnId`, and `limit` filters. The response includes the broker owner hash, not the raw product owner id.

## Chat Apps

Keep in the host chat app:

- chat/session authorization,
- chat and evidence records,
- UI streaming and rendering,
- evidence or tool semantics.

Move to the broker:

- Codex auth homes,
- app-server process pooling and JSON-RPC dispatch,
- same-thread turn locking,
- bundle materialization for chat skills, MCP servers, and host-tool adapters.

Recommended chat flow:

1. Resolve the authenticated product user to a stable `ownerId`.
2. Create or reuse a broker thread for the host chat.
3. Submit normal chat turns with `mode=queue` so the broker serializes same-chat concurrency, or use `mode=steer` when the UI explicitly appends input to an active turn.
4. Stream `/events` and map normalized event types to the existing UI stream protocol.
5. Keep evidence search or product tools as host-owned HTTP endpoints or MCP servers, declared from the task bundle.

An existing chat service can switch from calling Codex directly to calling the broker. Configure the service with `CODEX_BROKER_BASE_URL`, `CODEX_BROKER_INTERNAL_KEY`, and a bundle id such as `example-chat-v1`. The chat service can keep product prompt construction, chat authorization, chat state, and UI stream mapping, while the broker owns Codex auth homes, app-server pooling, thread locks, bundle materialization, and hosted adapter exposure.

The sample chat bundle exposes `host.evidence.search` through a broker-hosted adapter targeting the host-owned `POST /internal/codex/tools/evidence-search` endpoint. Set the same `CODEX_HOST_TOOL_KEY` in the broker environment and the host app. The broker forwards tool calls and opaque broker context; the host app validates the tool key and owns the evidence search semantics.

Approval-gated tool work emits `tool.requested` before `approval.requested`, followed by `approval.resolved` after the broker answers the app-server approval request. Host UIs can use `tool.requested` for generic tool lifecycle display and approval events for approval-specific state.

Reasoning summary notifications are normalized as `reasoning.summary.started`, `reasoning.summary.delta`, and `reasoning.completed` events. The payload includes `itemId`, `summaryIndex`, and a stable `summaryId` when Codex supplies both item id and summary index.

If app-server notifications arrive before all Codex turn metadata is known, the broker either attaches them to the best active context or buffers them by Codex turn id until the turn is registered. Such events are marked `ambiguous` so host consumers can distinguish early routed events from fully keyed events.

The `/events` stream validates the broker `threadId` and optional `turnId` filter before opening the SSE response. Unknown ids return the same JSON 404 behavior as other thread and turn endpoints.

## Job Workers

Keep in the host job worker:

- job queue and job records,
- worker scheduling,
- generated report artifacts.

Move to the broker:

- shared app-server process management,
- turn lifecycle,
- Codex thread mappings,
- owner/profile auth isolation.

Recommended worker flow:

1. Use the job owner or service account as `ownerId`.
2. Store broker `threadId` alongside the job.
3. Submit initial and follow-up work with `mode=queue`.
4. Use the broker HTTP API directly or the Python `CodexBrokerClient` from `codex_broker.client` to keep worker code small.

A job worker can support an opt-in broker execution mode with settings such as `CODEX_RUNTIME_MODE=broker`, `CODEX_BROKER_BASE_URL`, `CODEX_BROKER_INTERNAL_KEY`, and `CODEX_BROKER_BUNDLE_ID=document-jobs-v1`. The app keeps job records, queueing, artifacts, review rows, and UI streaming; an existing `codex_thread_id` or equivalent field can store the broker thread id for follow-up turns.

## Tool Exposure

Bundle-declared `prompts` are mounted from reviewed bundle roots, materialized into a per-turn overlay, and injected as text input before the host's turn input. Prefer skills for reusable workflow behavior; prompts are mainly for legacy host instructions that have not yet become skills.

When a bundle turn omits `cwd`, the broker runs Codex from the broker-owned per-turn overlay it just materialized. Explicit host `cwd` values still must be under configured host workspace roots or bundle `allowedPaths`; the overlay root is allowed separately because it is owned and cleaned up by the broker.

When inline bundles are enabled, `POST /v1/bundles/inline` stores the bundle by content digest and records its `bundleId` for later turn requests. Re-sending the same payload is idempotent; reusing an accepted inline `bundleId` with different content is rejected. Inline bundle ids also cannot shadow mounted bundle ids.

Bundle-declared `tools` with `type: "broker-hosted"` become a broker-hosted MCP adapter that forwards tool calls to host-owned HTTP endpoints. The broker validates the declaration and transports calls; it does not implement product-specific evidence or business logic. Hosted tool URLs must match `CODEX_BROKER_ALLOWED_HOSTED_TOOL_URL_PREFIXES` by parsed scheme and host, with optional explicit port and path-prefix restrictions. Broker-hosted HTTP tools support the `host-allowlist` network policy in v1; unsupported policy modes are rejected.

Hosted tool endpoints may return ordinary JSON, which the adapter exposes to Codex as formatted text. If an endpoint returns a valid MCP tool result shape with `content`, optional `isError`, optional `structuredContent`, and optional `_meta`, the adapter passes that result through directly. Use this for host-owned tools that need to expose artifact metadata, resource content, or file paths without the broker flattening them into a JSON text blob.

Adapters are transport shims. They may declare HTTP headers and opaque tool context. Secret-looking headers such as `Authorization`, cookies, tokens, keys, and secrets must use `env:VAR` indirection so secrets come from the broker process environment rather than bundle files. The broker also includes broker context such as `ownerHash`, profile, broker `threadId`, broker `turnId`, `hostApp`, `configProfile`, and `productCorrelationId`, plus the validated hosted-tool `approvalPolicy`, `scope`, and `networkPolicy`. Host endpoints should use those opaque fields to map back to their own authorization, identity, and data models.

Hosted tools may declare `approvalPolicy` as `never`, `on-request`, or `always`, and `scope` as `owner` or `profile`. The broker validates those values and preserves them in the adapter configuration for host-side enforcement.

Bundle-declared `mcpServers` are mounted into the Codex process only when their command name or exact absolute command path is allowlisted by broker configuration. Reviewed bundle roots do not automatically make every executable under them usable as an MCP command.

Secret-looking MCP env keys must use `env:VAR` indirection. The broker resolves those values into the app-server process environment and omits them from generated Codex config, avoiding secret values in mounted bundle files and command-line config.
