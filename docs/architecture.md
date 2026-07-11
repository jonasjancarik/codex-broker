# Architecture

read_when: changing process management, state storage, auth handling, bundle materialization, event streaming, recovery behavior, or host integration boundaries.

Codex Broker is an internal HTTP service that runs next to a host application. The host app calls the broker over HTTP, and the broker manages Codex auth homes, `codex app-server` child processes, durable broker thread and turn state, bundle mounting, and normalized event streaming.

The host app still owns product identity, product authorization, UI state, business records, prompts, artifact storage, and tool behavior. The broker owns the reusable Codex runtime boundary.

## Runtime Shape

```text
host app backend or worker
  |
  | HTTP JSON requests
  | SSE event stream
  v
codex-broker process
  |
  +-- HTTP API
  |     authenticates broker-key requests
  |     exposes health, readiness, OpenAPI, metrics, auth, threads, turns, events
  |
  +-- AuthManager
  |     hashes owner ids
  |     creates owner/profile CODEX_HOME directories
  |     runs codex login/status/logout commands
  |
  +-- TurnScheduler
  |     creates broker threads and turns
  |     enforces one active turn per broker thread
  |     runs reject, queue, and steer behavior
  |     normalizes Codex app-server notifications into broker events
  |
  +-- AppServerPool
  |     starts long-lived codex app-server children
  |     routes JSON-RPC requests, responses, notifications, and server requests
  |     reuses or closes children by pool key
  |
  +-- BundleRegistry
  |     resolves mounted and inline task bundles
  |     validates workspace paths, MCP commands, and hosted-tool endpoints
  |     materializes per-turn overlays
  |
  +-- StateStore
        persists SQLite tables for profiles, threads, turns, events,
        bundle digests, app-server processes, audit logs, and interactions
```

The implementation intentionally uses the Python standard library HTTP server and `subprocess` instead of a web framework. The console entry point is `codex_broker.__main__:main`, which builds `BrokerConfig.from_env()` and starts the HTTP server.

## Source Map

| Module | Responsibility |
| --- | --- |
| `src/codex_broker/__main__.py` | Console entry point. |
| `src/codex_broker/config.py` | Environment parsing and derived filesystem paths. |
| `src/codex_broker/http_api.py` | HTTP routes, auth header checks, readiness, metrics, SSE, OpenAPI. |
| `src/codex_broker/auth.py` | Owner hashing, profile normalization, CODEX_HOME creation, Codex login/status/logout. |
| `src/codex_broker/scheduler.py` | Thread and turn lifecycle, same-thread gates, config profiles, Codex request params, metrics. |
| `src/codex_broker/app_server.py` | `codex app-server` process pool, JSON-RPC transport, interaction mediation. |
| `src/codex_broker/bundles.py` | Mounted and inline bundle lookup, validation, overlays, MCP config, hosted-tool declarations. |
| `src/codex_broker/tool_adapter_mcp.py` | Local MCP server that forwards broker-hosted tool calls to host-owned HTTP endpoints. |
| `src/codex_broker/state.py` | SQLite schema, persistence, restart recovery, event and audit queries. |
| `src/codex_broker/events.py` | Codex app-server notification to broker event normalization. |
| `src/codex_broker/interactions.py` | Validation and fallback response shapes for approvals, permissions, user input, and MCP elicitations. |
| `src/codex_broker/client.py` | Small Python client for host services and workers. |

## Startup

On startup, the broker:

1. Reads environment into `BrokerConfig`.
2. Ensures `data_dir`, `auth_root`, `inline_bundle_root`, and `overlay_root` exist.
3. Opens the SQLite state store at `data_dir/state/broker.sqlite`.
4. Marks abandoned `starting`, `queued`, and `running` turns as failed.
5. Marks pending interactions failed with their fallback response.
6. Prunes persisted raw app-server event fields when raw capture retention is enabled.
7. Constructs auth, bundle, app-server pool, and scheduler services.
8. Starts `ThreadingHTTPServer` on `CODEX_BROKER_HOST:CODEX_BROKER_PORT`.

Only `GET /healthz` and `GET /readyz` are unauthenticated. Every product route, `/metrics`, `/openapi.json`, and `/v1/bundles/inline` requires the broker key unless `CODEX_BROKER_ALLOW_UNAUTHENTICATED=true`.

## Data Layout

The default data directory is `.data`; Docker deployments usually set it to `/data`.

```text
<data_dir>/
  state/
    broker.sqlite
  auth/
    owners/
      <owner-hash>/
        profiles/
          <profile>/
            codex-home/
              auth.json
              config.toml
  bundles/
    inline/
      <sha256-digest>/
        bundle.json
  workspaces/
    overlays/
      <turn-id>/
        AGENTS.md
        .agents/skills/<skill-name> -> mounted skill directory
        .codex/config.toml
        prompts/<prompt-name>.<ext> -> mounted prompt file
        tool-adapters.json
```

The broker never uses raw `ownerId` values in paths. `AuthManager.hash_owner()` uses an explicit owner-hash key or a broker-generated persistent key stored separately from the internal API key. Rotating the API key therefore does not orphan existing state. Profile ids are canonicalized; dot-segment ids are rejected, and deletion is containment-checked before touching disk.

## SQLite State

`StateStore` creates and migrates these tables:

| Table | Purpose |
| --- | --- |
| `owner_profiles` | Auth status, auth type, and auth fingerprint by owner hash and profile. |
| `threads` | Broker thread ids, Codex thread ids, profile/config profile, host app, bundle, cwd, status. |
| `turns` | Broker turns, Codex turn ids, input JSON, idempotency key, correlation id, status, errors, request fingerprint, bundle digest, resolved options, and broker version. |
| `events` | Normalized stream events plus optional redacted raw app-server method/params. |
| `bundle_digests` | Mounted or inline bundle id, digest, source, and path records. |
| `app_server_processes` | Durable start/close records for app-server child diagnostics. |
| `audit_logs` | Owner-scoped auth, turn, approval, interrupt, logout, and runtime failure actions. |
| `pending_interactions` | Host-mediated approvals, permissions, user input, and MCP elicitation requests. |

The database carries a schema version and rejects databases created by a newer broker. Restart recovery is conservative: in-progress turns are failed, pending interactions receive fallback responses, stale process rows become `orphaned`, and abandoned overlays are removed. Terminal turn state and its terminal event are committed together.

## Auth Boundary

The host app chooses an `ownerId` and optional `profile`. The broker hashes the owner id and creates an isolated Codex home at:

```text
<data_dir>/auth/owners/<owner-hash>/profiles/<profile>/codex-home
```

Codex login commands run with:

- `CODEX_HOME` set to that profile home,
- `CODEX_CREDENTIAL_STORE` set from broker config,
- `HOME` set to the profile parent directory,
- a scrubbed environment that keeps only safe base variables.

The broker supports cheap status checks, explicit active auth probes, device auth start/submit, API-key login, runtime invalidation, logout, and explicit profile deletion. Updating auth closes that owner/profile's pooled app-server children so the next turn starts with the new auth fingerprint.

## Thread And Turn Flow

A normal turn follows this path:

1. Host creates or reuses a broker thread with `POST /v1/owners/{ownerId}/threads`.
2. Host submits a turn with `POST /v1/owners/{ownerId}/threads/{threadId}/turns`.
3. The scheduler validates input, profile, config profile, bundle, and cwd.
4. The scheduler records the turn as `starting` or `queued`.
5. A bounded executor starts accepted work up to the configured active-turn limit, or a conservative worker bound when the limit is `0`.
6. An explicit per-thread FIFO queue enforces one active turn for the owner/thread pair.
7. The bundle registry materializes a per-turn overlay when a bundle is selected.
8. The app-server pool returns a matching child or starts a new one.
9. The scheduler starts or resumes the Codex thread, then starts the Codex turn.
10. App-server notifications are normalized and persisted as broker events.
11. The turn becomes `completed`, `failed`, `timed_out`, or `interrupted`.
12. The overlay is removed and pooled child reuse is decided.

Same-thread behavior is controlled by the turn `mode`:

| Mode | Behavior |
| --- | --- |
| `reject` | Fail immediately with `active_turn_exists` if another turn is active. |
| `queue` | Wait for the active turn to finish, then run. |
| `steer` | Send input into the active Codex turn when possible; otherwise fall back to `reject`. |

Different broker threads can run concurrently. `CODEX_BROKER_MAX_ACTIVE_TURNS` caps active workers and `CODEX_BROKER_MAX_QUEUED_TURNS` bounds waiting work. Idempotency lookup, creation, and scheduling share one critical section, so concurrent retries start one worker.

## App-Server Pool

`AppServerPool` starts children lazily with:

```text
<CODEX_BIN> app-server --listen stdio://
```

The pool key includes:

- owner hash,
- profile,
- owner/profile auth fingerprint,
- `configProfile`,
- Codex command,
- detected Codex version,
- credential-store mode,
- broker app-server client identity fields,
- process-level Codex config args,
- mounted MCP server declarations,
- hashes of resolved MCP `env:VAR` values.

Idle children are closed by a background sweeper after `CODEX_BROKER_POOL_IDLE_TTL_SECONDS`. Child startup happens outside the pool-wide lock behind a per-key creation lock, so one slow launch does not block unrelated keys. Active children are never TTL-swept.

App-server child environments are scrubbed by default. Secret-looking variables are not passed through unless their exact names are listed in `CODEX_BROKER_PASSTHROUGH_ENV`. MCP `env:VAR` values are resolved separately and included in the child environment so Codex can launch that MCP server without writing the secret value to generated config.

Turns that use broker-hosted HTTP adapters close their app-server child after finalization because the adapter config includes per-turn overlay context.

## Bundles And Overlays

Bundles are reviewed task manifests selected by `bundleId`. Mounted bundles are found under `CODEX_BROKER_ALLOWED_BUNDLE_ROOTS`. Inline bundles are accepted only when `CODEX_BROKER_ENABLE_INLINE_BUNDLES=true`.

A bundle can contribute:

- instruction text,
- mounted skills,
- mounted prompt files,
- mounted MCP servers,
- broker-hosted HTTP tool adapters,
- additional allowed workspace paths,
- a default sandbox mode.

The broker validates declarations and creates an overlay for the turn. Skills and prompts are linked into the overlay, instructions are written to `AGENTS.md`, MCP config is written under `.codex/config.toml`, and hosted-tool adapter config is written to `tool-adapters.json`.

The broker does not implement product tool behavior. Broker-hosted tools are exposed to Codex through a local MCP adapter process that forwards calls to host-owned HTTP endpoints. The host endpoint must still authenticate the request and enforce product authorization.

## Events And Interactions

The app-server client reads JSON-RPC messages from stdout. Responses are routed to request waiters; notifications are routed to the active turn context by Codex turn id, Codex thread id, or the only active context when necessary. Server-initiated approval and input requests run in independent handlers, so one host wait cannot stall unrelated responses sharing that child.

SSE readers wait on a state-store event signal rather than polling SQLite four times per second. Durable history is pruned by age and per-turn count at startup; audit metrics read a maintained summary table instead of grouping the complete log on each scrape.

Normalized events are stored in SQLite and streamed from:

```text
GET /v1/owners/{ownerId}/threads/{threadId}/events?turnId=<turnId>&after=<event-id>
```

SSE frames include a monotonically increasing event id and a JSON envelope with broker ids, Codex ids, product correlation id, payload, timestamp, and an `ambiguous` flag. When raw app-server capture is enabled, redacted `rawMethod` and `rawParams` are included until retention pruning clears them.

Approvals, permission requests, user-input prompts, and MCP elicitations are persisted as pending interactions before the broker answers the app-server request. Hosts can resolve them with the interaction API. If the host does not answer before `CODEX_BROKER_HOST_RESPONSE_TIMEOUT_SECONDS`, the broker responds with a safe fallback:

| Interaction | Fallback |
| --- | --- |
| Command or file approval | Decline. |
| Legacy approval | Deny. |
| Permission request | Grant no extra permissions for the turn. |
| User input | Empty answers object. |
| MCP elicitation | Decline. |

## Security Model

The broker is not a public API. It assumes a trusted host backend or worker calls it with an internal key. Browser clients should call the host app, not the broker directly.

The main controls are:

- broker-key authentication for product routes,
- owner/profile auth home isolation,
- hashed owner paths and owner hashes in logs,
- scrubbed child process environments,
- explicit pass-through environment allowlists,
- reviewed bundle roots,
- workspace root validation,
- MCP command allowlisting,
- hosted-tool URL allowlisting,
- `env:VAR` indirection for secret-looking headers and MCP environment values,
- redaction for logs and optional raw event persistence.

Readiness checks verify the internal key, Codex binary presence, configured workspace roots, configured bundle roots, SQLite access, and auth-root writability.

## Shutdown

On process shutdown, the server stops accepting work and calls scheduler shutdown:

- `CODEX_BROKER_SHUTDOWN_MODE=interrupt` interrupts active turns and finalizes them as interrupted.
- `CODEX_BROKER_SHUTDOWN_MODE=drain` rejects new turns, waits up to `CODEX_BROKER_SHUTDOWN_DRAIN_TIMEOUT_SECONDS`, then interrupts leftovers.

After scheduler shutdown, the pool closes all app-server children and the state store is closed.
