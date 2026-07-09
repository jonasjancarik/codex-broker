# Codex Broker Spec

read_when: changing broker APIs, replacing local Codex integration code, adding per-user Codex auth, sharing Codex process-management code across app services, or supplying Codex skills/prompts/tools from a host application.

## Goal

Build a reusable internal Docker service that brokers Codex app-server sessions for a host application.

The broker should support:

- live chat backed by Codex,
- background job workers backed by Codex,
- per-owner Codex authentication,
- task bundles that mount skills, prompts, MCP servers, or broker-hosted adapters,
- future host apps that need Codex threads, turns, streaming events, and Codex execution policy without copying app-server process code.

The host app owns product identity, authorization, data models, UI, and app-specific tool behavior. The broker owns the generic Codex-facing interface for exposing those tools to Codex, either through mounted bundles, MCP servers, or broker-hosted adapters.

The broker also owns Codex process management, Codex auth homes, app-server JSON-RPC dispatch, event multiplexing, and bundle resolution.

## Design Principles

- Keep the broker product-facing, not a raw JSON-RPC passthrough.
- Keep product authorization and app-specific data behavior in the host app.
- Run long-lived `codex app-server` processes instead of spawning one process per turn.
- Isolate Codex credentials by owner and profile.
- Enforce one active turn at a time per broker thread.
- Allow different threads and different owners to run concurrently.
- Keep tool declarations explicit, reviewed, and validated.
- Avoid mutating owner auth homes when mounting task-specific files and config.
- Persist enough state to recover cleanly after broker restart.

## Non-Goals

- Public internet exposure. The broker is an internal service.
- Product user management. Host apps authenticate users and authorize broker calls.
- Product-specific evidence search, report generation, file formats, or business logic.
- Browser UI. Host apps render their own UIs.
- Direct storage of product secrets in bundle files.

## Terms

- **Host app**: the product using the broker.
- **Owner**: a host-app identity that owns a Codex auth home. Usually a product user id or service-account id.
- **Profile**: a named Codex auth profile under an owner.
- **Configuration profile**: broker-side policy and defaults for model, sandbox, approval policy, workspace roots, and enabled bundles. API requests choose one with `configProfile`.
- **Broker thread**: the broker's durable thread id used by host apps.
- **Codex thread**: the app-server conversation id managed by the broker.
- **Turn**: one unit of Codex work on a broker thread.
- **Bundle**: reviewed material for a class of work.
- **App-server lease**: a checked-out app-server client from the broker pool.

## Architecture

```text
Host app backend / worker
  |
  | HTTP + SSE
  v
Codex Broker
  |
  +-- auth manager
  |     +-- owner/profile CODEX_HOME directories
  |
  +-- thread and turn scheduler
  |     +-- one active turn per broker thread
  |     +-- reject, queue, and steer modes
  |
  +-- app-server pool
  |     +-- long-lived codex app-server children
  |     +-- pool key: owner + profile + configuration profile + MCP config
  |
  +-- bundle registry
  |     +-- mounted bundles
  |     +-- inline bundles when enabled
  |     +-- ephemeral overlays
  |
  +-- state store
        +-- threads, turns, events, auth state, audit logs
```

## App-Server Pooling

The broker maintains a pool of app-server clients keyed by:

- owner hash,
- auth profile,
- Codex command/version,
- credential-store mode,
- configuration profile (`configProfile`),
- resolved MCP config.

Task bundle selection should not force a new app-server process unless it changes process-level state. Prefer per-turn app-server parameters and ephemeral overlays over mutating shared auth homes.

Each app-server client must:

- initialize exactly once,
- route JSON-RPC responses to request waiters,
- route notifications to active turn contexts,
- reject unsupported app-server approval requests safely,
- fail active turns if the child process crashes.

Idle app-server processes may be closed after a configurable TTL. Active processes must not be killed except during shutdown, explicit owner logout, or unrecoverable health failure.

## Turn Scheduling

The broker enforces:

- one active turn per broker thread,
- explicit same-thread behavior per request,
- durable turn status,
- normalized event streaming.

Supported same-thread modes:

- `reject`: fail fast if another turn is active.
- `queue`: wait until the active turn finishes, then run.
- `steer`: send input to the active turn using Codex steering when possible; otherwise reject.

Different broker threads may run concurrently. Different owners may run concurrently with isolated auth homes. A global concurrency cap may exist for resource protection, but correctness must not depend on it.

## Auth Homes

Broker-managed auth layout:

```text
/data/
  state/
    broker.sqlite
  auth/
    owners/
      <owner-hash>/
        profiles/
          default/
            codex-home/
          work/
            codex-home/
```

The broker should hash or HMAC product owner ids before using them in paths. Raw product ids should not appear in filesystem paths by default.

Auth features:

- status checks,
- explicit active auth probe,
- device auth start/submit,
- API-key login,
- logout,
- profile deletion when explicitly requested.

Device auth responses should include verification URL, user code, expiry, and current status when available. The broker must not log access tokens, refresh tokens, API keys, or full auth files.

## API Shape

Core endpoints:

- `GET /healthz`
- `GET /readyz`
- `GET /metrics`
- `GET /openapi.json`
- `GET /v1/owners/{ownerId}/auth/status?profile=default`
- `POST /v1/owners/{ownerId}/auth/probe`
- `POST /v1/owners/{ownerId}/auth/device/start`
- `POST /v1/owners/{ownerId}/auth/device/submit`
- `POST /v1/owners/{ownerId}/auth/api-key`
- `POST /v1/owners/{ownerId}/auth/logout`
- `GET /v1/owners/{ownerId}/audit-logs`
- `POST /v1/owners/{ownerId}/threads`
- `GET /v1/owners/{ownerId}/threads/{threadId}`
- `POST /v1/owners/{ownerId}/threads/{threadId}/archive`
- `POST /v1/owners/{ownerId}/threads/{threadId}/turns`
- `GET /v1/owners/{ownerId}/threads/{threadId}/turns/{turnId}`
- `POST /v1/owners/{ownerId}/threads/{threadId}/turns/{turnId}/steer`
- `POST /v1/owners/{ownerId}/threads/{threadId}/turns/{turnId}/interrupt`
- `GET /v1/owners/{ownerId}/threads/{threadId}/events`

Thread create example:

```json
{
  "threadId": "chat-123",
  "hostApp": "chat-app",
  "bundleId": "example-chat-v1",
  "configProfile": "default",
  "cwd": "/workspaces/app"
}
```

Turn create example:

```json
{
  "input": [{ "type": "text", "text": "Answer the user." }],
  "mode": "queue",
  "configProfile": "default",
  "productCorrelationId": "chat-123:turn-456",
  "idempotencyKey": "chat-123:turn-456",
  "stream": true
}
```

The broker should return a `streamUrl` for turns so host apps can consume normalized events with SSE.

## Events

The broker emits normalized events while preserving optional raw app-server data for debugging when enabled.

Important event types:

- `thread.started`
- `thread.resumed`
- `turn.started`
- `turn.completed`
- `turn.failed`
- `turn.interrupted`
- `message.delta`
- `message.completed`
- `tool.requested`
- `tool.started`
- `tool.output.delta`
- `tool.completed`
- `approval.requested`
- `approval.resolved`
- `user_input.requested`
- `user_input.resolved`
- `mcp.elicitation.requested`
- `mcp.elicitation.resolved`
- `item.started`
- `item.completed`
- `error`

Every event should include:

- broker event id,
- owner hash,
- broker thread id,
- broker turn id when known,
- product correlation id when supplied,
- Codex thread id when known,
- Codex turn id when known,
- normalized payload,
- ambiguity flag when routing was inferred.

The dispatcher must tolerate app-server notifications that arrive before all metadata is known. It should attach them to the best active context or buffer them by Codex turn id until the turn is registered.

Approval, user-input, and MCP elicitation requests must also be persisted as host-resolvable interactions. Hosts answer them with `POST /v1/owners/{ownerId}/threads/{threadId}/turns/{turnId}/interactions/{interactionId}/resolve`; if no host answer arrives before the broker timeout, the broker must answer app-server with a fail-closed fallback and persist the fallback source.

## Bundles

A task bundle describes what Codex should know and be allowed to use for a class of work.

Example:

```json
{
  "id": "example-chat-v1",
  "version": "2026-06-26",
  "instructions": [
    "Use host evidence tools only through declared adapters."
  ],
  "skills": [
    {
      "name": "example-evidence",
      "source": {
        "type": "mount",
        "path": "/bundles/example-chat-v1/skills/example-evidence"
      }
    }
  ],
  "tools": [
    {
      "name": "host.evidence.search",
      "type": "broker-hosted",
      "description": "Search host-owned evidence.",
      "inputSchema": {
        "type": "object",
        "properties": {
          "query": { "type": "string" }
        },
        "required": ["query"]
      },
      "http": {
        "url": "http://app:3000/internal/codex/tools/evidence-search",
        "headers": {
          "X-Codex-Tool-Key": "env:CODEX_HOST_TOOL_KEY"
        }
      }
    }
  ],
  "allowedPaths": ["/workspaces/app"],
  "sandbox": { "mode": "workspace-write" }
}
```

Bundles must be:

- loaded from configured bundle roots or accepted through the inline bundle API,
- validated before use,
- rejected if they declare paths outside the broker allowlist,
- rejected if MCP commands are not allowlisted,
- rejected if hosted tool endpoints are not allowlisted,
- rejected if secret-looking headers or env vars contain literal secrets instead of `env:VAR` indirection.

Bundle materialization should avoid mutating owner auth homes. Prefer ephemeral per-turn overlays:

- symlink or copy bundle files into `/data/overlays/<turn-id>/`,
- pass skills as turn input,
- generate MCP config in the overlay,
- clean up overlays after the turn.

## Tool Exposure

The broker supports three tool categories:

- Codex built-in tools controlled through app-server parameters.
- Host app tools exposed through a broker-hosted HTTP adapter or app-specific MCP server.
- Bundle-declared MCP servers mounted into app-server config.

App-specific bridges should live in bundles or app-specific MCP servers, not in the generic broker core.

Broker-hosted adapters are transport shims. They forward declared headers, input arguments, and opaque broker context to host-owned HTTP endpoints. Host endpoints remain responsible for final authorization and app-specific behavior.

## Durable State

The broker stores:

- owner/profile auth status,
- broker thread id to Codex thread id mappings,
- turns and turn status,
- normalized events,
- audit logs,
- app-server process metadata,
- inline bundle metadata when enabled.

SQLite is enough for a single-replica Docker image. The schema should remain straightforward to migrate to a client/server database if multiple broker replicas are required later.

Host apps may also store thread and turn state. The broker remains the source of truth for Codex thread and process mappings and active turn locks.

## Observability

The broker should provide:

- `/readyz` for orchestrator readiness,
- `/metrics` for Prometheus-style counters and durations,
- structured JSON logs,
- owner-scoped audit logs,
- redacted app-server stderr/stdout diagnostics,
- optional raw event capture with retention.

Readiness should require:

- state store access,
- internal API key configured unless development override is enabled,
- Codex binary present,
- configured workspace roots readable,
- configured bundle roots readable,
- auth root writable.

## Shutdown

Shutdown modes:

- `interrupt`: reject new turns, interrupt active turns, close app-server children.
- `drain`: reject new turns, wait for active turns up to a timeout, then interrupt leftovers.

In both modes, connected streams should receive terminal events for interrupted or failed turns when possible.

## Host Integration Patterns

### Chat Apps

Keep in the host app:

- product auth,
- chat records,
- UI streaming,
- prompt construction,
- evidence/tool behavior.

Move to the broker:

- Codex auth homes,
- app-server pooling,
- same-thread turn locking,
- Codex thread mappings,
- bundle materialization.

### Job Workers

Keep in the host app:

- queue behavior,
- job records,
- generated artifacts,
- review state.

Move to the broker:

- shared app-server process management,
- turn lifecycle,
- Codex thread mappings,
- owner/profile auth isolation.

## Implementation Phases

1. Contract and tests
   - Define OpenAPI schema.
   - Add app-server protocol fixtures.

2. Broker core
   - Implement app-server stdio client.
   - Implement request waiters and turn contexts.
   - Implement per-thread locks.
   - Implement SSE streaming.

3. Auth homes
   - Implement per-owner `CODEX_HOME`.
   - Implement device auth, API-key auth, and active auth probes.
   - Implement logout.

4. Bundles
   - Implement bundle registry.
   - Materialize skills and prompts into overlays.
   - Validate tools, paths, and MCP servers.

5. Host integrations
   - Add chat-app integration.
   - Add job-worker integration.
   - Preserve host-owned UI and data behavior.

6. Operational hardening
   - Add metrics and structured logs.
   - Add restart recovery.
   - Add shutdown modes.

## Acceptance Criteria

- Two different threads can run concurrently for the same owner.
- Two different owners can run concurrently with isolated auth homes.
- Two turns on the same thread cannot race.
- A same-thread second request either rejects, queues, or steers based on explicit mode.
- A crashed app-server process fails active turns and can restart for later turns.
- Device auth works without exposing token material to the host app UI.
- Mounted skills can be supplied by a host app bundle and used by Codex.
- Inline bundles, if enabled, are size-limited and path-validated.
- A chat app and a job worker can use the same broker image.
- No global concurrency cap is required for correctness.
