# Codex Broker

Codex Broker is an internal service you run next to your product app when that app needs to use Codex. In the normal deployment, you add it to the same Docker Compose project as your app, and your app calls it over HTTP.

Think of the broker as your app's local Codex service. Your app sends it chat messages or job requests, and the broker starts and manages the Codex work behind the scenes.

A host app still owns its product behavior: users, permissions, database records, UI, prompts, evidence semantics, job queues, and business rules. The broker owns the reusable Codex operations for that app deployment: Codex auth homes, long-lived app-server processes, thread and turn lifecycle, same-thread locking, event streaming, and bundle mounting.

You can run one broker for more than one app if you deliberately want a shared internal service, but that is not the main mental model. Start with one broker container for one product app.

The project spec lives in [codex-broker-spec.md](codex-broker-spec.md).

## Why This Exists

Before this broker, each app that wanted Codex had to solve the same hard problems:

- how to run `codex app-server` as a long-lived child process,
- how to keep different users' Codex credentials isolated,
- how to map product chats or jobs to Codex threads,
- how to prevent two turns on the same Codex thread from racing,
- how to stream Codex events back to a UI or worker,
- how to mount skills, prompts, MCP servers, or host-owned tools into Codex,
- how to restart or fail work cleanly when a Codex process dies.

Those problems are generic. They do not belong in app-specific chat routes or workers. The broker puts them behind a product-facing HTTP API that the app can call from its own backend.

## Main Use Cases

Use this broker when a product app needs to run Codex, but the app should not own Codex process management directly.

### Live Chat

Example: a product support or research chat.

The product app owns the logged-in user, chat records, chat memory, UI streaming, and evidence semantics. The broker runs Codex for each chat thread, serializes turns for the same chat, streams normalized events, and exposes declared host tools to Codex through a mounted bundle.

### Background Jobs

Example: document review or report-normalization jobs.

The product app owns the queue, job records, input/output files, artifacts, and review workflow. The broker runs Codex turns for those jobs using the same broker API that live chat uses.

### Per-User Codex Auth

Example: an app where each product user brings their own Codex login.

The host app decides who the product user is and whether they may use Codex. The broker creates a separate owner/profile auth home, runs device auth or API-key auth, and keeps credentials out of host app databases.

### Reusable Bundles

Example: a reviewed bundle that gives Codex a skill, a prompt overlay, a mounted MCP server, or a broker-hosted tool adapter.

The broker validates and mounts the bundle. The host app still owns what its tools mean and whether a user is allowed to use them.

## What The Broker Owns

The broker owns generic Codex infrastructure:

- `codex app-server` child processes and pooling,
- per-owner and per-profile `CODEX_HOME` directories,
- Codex login status, device auth, API-key auth, and logout,
- broker-thread to Codex-thread mappings,
- turn creation, turn status, interruption, steering, and archive behavior,
- one active turn at a time per broker thread,
- normalized event persistence and Server-Sent Events streaming,
- configuration profiles for model, sandbox, approval, workspace, and bundle policy,
- mounted bundles, inline bundle validation, skill/prompt overlays, mounted MCP servers, and broker-hosted adapter transport,
- audit logs, structured logs, metrics, readiness checks, and recovery of abandoned turns after restart.

## What Host Apps Own

Host apps own product-specific behavior:

- product identity and session auth,
- deciding whether a user may call the broker,
- product database records and data models,
- UI and user-facing streaming behavior,
- prompts and product-specific assistant behavior,
- app-specific tool semantics,
- evidence search, report generation, file formats, artifacts, and job queues,
- final authorization checks inside host-owned tool endpoints.

This split is important. The broker should not know what a product evidence hit means or how a host-owned report should be reviewed. It should only expose the controlled interface that lets Codex call those host-owned capabilities.

## Important Terms

- **Host app**: the product using the broker.
- **Owner**: the stable product identity that owns Codex credentials. Usually this is a user id or service-account id supplied by the host app.
- **Profile**: a named Codex auth profile under an owner. `default` is enough for many apps.
- **Broker thread**: the broker's durable thread id. Host apps submit turns to this id. Host apps may supply this id when creating a thread, or omit it and let the broker generate one.
- **Codex thread id**: the raw thread id returned by `codex app-server`. The broker stores it so host apps do not need to manage app-server details.
- **Turn**: one unit of Codex work submitted to a broker thread.
- **Bundle**: reviewed material that can provide skills, prompts, MCP servers, hosted-tool adapters, allowed paths, and sandbox policy.
- **Configuration profile**: a named set of broker-side defaults and policy for model, sandbox, approval mode, allowed bundles, and workspace roots. API requests choose one with `configProfile`.

## Normal Request Flow

A typical host integration follows this shape.

1. The host app authenticates its own user.
2. The host app chooses an `ownerId`, usually the product user id or a service-account id.
3. The host app checks or starts Codex auth for that owner/profile.
4. The host app creates or reuses a broker thread, optionally with a caller-supplied `threadId`.
5. The host app submits a turn to the broker thread.
6. The host app streams normalized broker events from `/events`.
7. The host app maps those events into its own UI, job logs, database rows, or artifacts.

Example thread create:

```json
{
  "threadId": "chat-123",
  "hostApp": "chat-app",
  "bundleId": "example-chat-v1",
  "configProfile": "default",
  "cwd": "/workspaces/app"
}
```

If the same owner creates a thread with the same `threadId` again, the broker returns the existing broker thread.

Example turn create:

```json
{
  "input": [
    {
      "type": "text",
      "text": "Summarize the evidence for this user question."
    }
  ],
  "hostApp": "chat-app",
  "bundleId": "example-chat-v1",
  "configProfile": "default",
  "cwd": "/workspaces/app",
  "mode": "queue",
  "productCorrelationId": "chat-123:message-456",
  "idempotencyKey": "chat-123:message-456"
}
```

Use `idempotencyKey` when a host may retry the same request. A repeated turn create with the same owner, broker thread, and idempotency key returns the original broker turn instead of starting duplicate Codex work.

## Same-Thread Turn Behavior

The broker enforces one active turn at a time per broker thread. The `mode` field tells the broker what to do when another turn is already active:

- `reject`: fail immediately with a conflict.
- `queue`: wait until the current turn finishes, then run the new turn.
- `steer`: try to send input into the active turn; if there is no steerable active turn, behave like `reject`.

Use `queue` for background workers and for UI flows where a second request should wait. Use `reject` when the UI wants to prevent duplicate sends. Use `steer` only when the product intentionally appends input to an active Codex turn.

Different broker threads may run concurrently. Different owners may run concurrently with isolated auth homes.

## Tool And Bundle Boundary

Bundles are how host apps expose Codex capabilities without putting product logic in the broker.

A bundle can declare:

- mounted skills,
- mounted prompt files,
- mounted MCP servers,
- broker-hosted HTTP tool adapters,
- allowed workspace paths,
- sandbox policy.

For broker-hosted adapters, the broker acts as a transport shim. It validates the adapter declaration, resolves secret headers from environment variables, adds broker context, and forwards the tool call to a host-owned HTTP endpoint.

The host endpoint must still enforce product authorization and implement product semantics.

For example, the sample chat bundle declares `host.evidence.search`. The broker exposes it to Codex, but the actual evidence lookup happens in the host app's `POST /internal/codex/tools/evidence-search` endpoint. The host app validates `CODEX_HOST_TOOL_KEY` and decides what evidence results mean.

## API Overview

Core endpoints:

- `GET /healthz`
- `GET /readyz`
- `GET /metrics`
- `GET /openapi.json`
- `GET /v1/owners/{ownerId}/auth/status?profile=default`
- `POST /v1/owners/{ownerId}/auth/device/start`
- `POST /v1/owners/{ownerId}/auth/device/submit`
- `POST /v1/owners/{ownerId}/auth/api-key`
- `POST /v1/owners/{ownerId}/auth/runtime/invalidate`
- `POST /v1/owners/{ownerId}/auth/logout`
- `GET /v1/owners/{ownerId}/audit-logs`
- `POST /v1/owners/{ownerId}/threads`
- `GET /v1/owners/{ownerId}/threads/{threadId}`
- `POST /v1/owners/{ownerId}/threads/{threadId}/archive`
- `POST /v1/owners/{ownerId}/threads/{threadId}/turns`
- `GET /v1/owners/{ownerId}/threads/{threadId}/turns/{turnId}`
- `POST /v1/owners/{ownerId}/threads/{threadId}/turns/{turnId}/steer`
- `POST /v1/owners/{ownerId}/threads/{threadId}/turns/{turnId}/interrupt`
- `GET /v1/owners/{ownerId}/threads/{threadId}/events?after=0`

Requests other than health and readiness require `Authorization: Bearer <key>` or `X-Codex-Broker-Key: <key>`. This includes `/metrics` and `/openapi.json`.

Auth status reports `missing`, `present_unverified`, `authenticated`, `invalid`, or `refresh_failed`, plus an `authFingerprint` for the owner/profile auth file. Failed turns include `errorCode`, `publicMessage`, and `adminMessage`; host UIs should display `publicMessage` or `error` to end users and keep `adminMessage` for admin logs. After an administrator refreshes shared Codex auth, call `POST /v1/owners/{ownerId}/auth/runtime/invalidate` for the profile to close pooled app-server children that were started with the old auth.

Set `CODEX_BROKER_INTERNAL_KEY` or `CODEX_BROKER_INTERNAL_KEY_FILE`. Unauthenticated mode is only for local development and requires `CODEX_BROKER_ALLOW_UNAUTHENTICATED=true`.

## Run Locally

From the repository root, run the broker through `uv`:

```bash
uv run codex-broker
```

`uv` reads [pyproject.toml](pyproject.toml), builds the local package, and runs the `codex-broker` console script. Set environment variables before starting the process.

Useful local environment:

```env
CODEX_BROKER_HOST=127.0.0.1
CODEX_BROKER_PORT=3400
CODEX_BROKER_DATA_DIR=.data
CODEX_BROKER_ALLOWED_WORKSPACE_ROOTS=/path/to/workspaces
CODEX_BROKER_ALLOWED_BUNDLE_ROOTS=/path/to/bundles
CODEX_BROKER_ALLOWED_HOSTED_TOOL_URL_PREFIXES=http://127.0.0.1,http://localhost,http://host.docker.internal
CODEX_BROKER_INTERNAL_KEY=dev-only-key
CODEX_BIN=codex
CODEX_CREDENTIAL_STORE=file
CODEX_BROKER_RAW_EVENT_RETENTION_SECONDS=604800
CODEX_BROKER_JSON_LOGS=true
CODEX_BROKER_SHUTDOWN_MODE=interrupt
CODEX_BROKER_SHUTDOWN_DRAIN_TIMEOUT_SECONDS=30

# Dev-only escape hatch when no key is configured:
# CODEX_BROKER_ALLOW_UNAUTHENTICATED=true

# Optional JSON object keyed by configProfile name:
# CODEX_BROKER_CONFIG_PROFILES_JSON={"review":{"model":"gpt-5","enabledBundles":["review-bundle"]}}
```

## Docker

The Docker image installs the official Codex CLI Linux release archive from `openai/codex` at build time. It runs as the non-root `broker` user and includes a `/readyz` healthcheck.

```bash
docker build -t codex-broker .
docker run --rm \
  -p 3400:3400 \
  -v codex-broker-data:/data \
  -v /path/to/workspaces:/workspaces:rw \
  -v /path/to/bundles:/bundles:ro \
  -e CODEX_BROKER_ALLOWED_WORKSPACE_ROOTS=/workspaces \
  -e CODEX_BROKER_ALLOWED_BUNDLE_ROOTS=/bundles \
  -e CODEX_BROKER_INTERNAL_KEY=dev-only-key \
  codex-broker
```

Override the pinned Codex version with `--build-arg CODEX_VERSION=<version>`.

See [docs/deployment.md](docs/deployment.md) and [examples/docker-compose.yml](examples/docker-compose.yml) for a Docker Compose example.

## Current Integrations

Implemented integration examples:

- A chat app can keep product prompt construction, chat state, UI streaming, and evidence semantics while the broker receives Codex turns and exposes the declared `host.evidence.search` adapter.
- A job worker can keep job records, queueing, artifacts, review rows, and UI streaming while the broker receives job turns and manages Codex thread and turn state.
- Example mounted bundles live under [examples/bundles](examples/bundles).
- Host clients are available in Python and TypeScript.

Still outside this repo:

- enabling a concrete chat integration in production deployment,
- enabling a concrete job-worker integration in production deployment,
- deciding whether inline bundles are needed in production.

## Development Status

Implemented in this repo:

- owner/profile auth homes with hashed owner paths,
- API-key, device-auth, status, logout, and explicit profile deletion flows,
- app-server stdio pooling with lazy restart after child failure,
- profile defaults and policy checks for model, approval, sandbox, enabled bundles, and workspace roots,
- startup recovery that marks abandoned `starting`, `queued`, and `running` turns failed after a broker restart,
- idle app-server pool cleanup after `CODEX_BROKER_POOL_IDLE_TTL_SECONDS`,
- explicit shutdown handling that rejects new turns and either interrupts or drains accepted work,
- request waiters and turn contexts for JSON-RPC routing,
- per-thread `reject`, `queue`, and `steer` turn behavior,
- normalized event persistence and SSE streaming with product correlation and Codex ids,
- optional caller-supplied broker `threadId` values for host chat or job ids,
- optional raw app-server event capture with recursive secret redaction and bounded raw-field retention,
- owner-scoped audit log API for auth, turn, approval, interrupt, and logout events,
- durable app-server child process lifecycle records for operational diagnosis,
- app-server 0.142.3 mode/capability event coverage for plan, goal, review, approvals, user input, and MCP elicitations,
- mounted bundles, inline bundle validation, skills/prompt overlays, mounted MCP, and broker-hosted tool adapters,
- readiness checks, Prometheus-style metrics, structured JSON logs, and schema-backed `/openapi.json`.

## Tests

```bash
uv run python -m unittest discover -s tests
```

For warning-sensitive verification:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run python -W always::ResourceWarning -m unittest discover -s tests
```

## More Reading

- [docs/host-integration.md](docs/host-integration.md): how host apps should call the broker.
- [docs/integrating-with-broker.md](docs/integrating-with-broker.md): copy-pasteable integration flow, client examples, SSE events, and hosted-tool endpoint contract.
- [docs/app-server-modes.md](docs/app-server-modes.md): version-pinned Codex app-server mode and capability coverage.
- [docs/deployment.md](docs/deployment.md): Docker mounts, secrets, deployment, and shutdown behavior.
- [examples/bundles/README.md](examples/bundles/README.md): example task bundles and hosted-tool declarations.
