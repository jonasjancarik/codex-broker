# Deployment Notes

The broker is designed as one internal Docker image that a host app can run next to its own backend or worker services.

## Image

The Dockerfile installs the official Codex CLI Linux musl release from `openai/codex` and then installs the broker Python package. Build-time args:

- `CODEX_VERSION`: Codex release version without the `rust-v` prefix, default `0.142.2`.
- `TARGETARCH`: supplied by Docker BuildKit; `amd64` maps to `x86_64`, `arm64` maps to `aarch64`.

## Docker Mounts

Use one persistent `/data` volume for broker SQLite state, auth homes, inline bundles, and overlays. The image creates `/data` for the non-root `broker` user before startup. Mount host workspaces under `/workspaces`, stable reviewed bundles under `/bundles`, and host-owned job data under a separate path such as `/host-data`.

The example Compose file mounts one generic app workspace into the broker container and exposes port `3400` only to the Compose network by default. It also shows a `/host-data/jobs` mount for host-owned job workspaces. Host apps remain responsible for their own databases, queues, UI, authorization, and app-specific tool semantics. Add a local override with `ports: ["3400:3400"]` only when you intentionally want host-machine access.

When enabling the example chat bundle, the broker container must share a Docker network or route with the host app service named by the bundle endpoint, currently `http://app:3000/internal/codex/tools/evidence-search`. Set `CODEX_HOST_TOOL_KEY` in both the broker container and host app so the hosted adapter can authenticate to the host-owned evidence endpoint.

## Secrets

Set `CODEX_BROKER_INTERNAL_KEY_FILE` to a Docker secret path. The broker reports not-ready without an internal key unless `CODEX_BROKER_ALLOW_UNAUTHENTICATED=true` is set for local development. Do not pass auth files, API keys, or access tokens as bundle content. Owner Codex credentials are stored under hashed owner/profile paths in `/data/auth`.

Only `GET /healthz` and `GET /readyz` are intended for unauthenticated orchestrator probes. All product API routes, `/metrics`, `/openapi.json`, and bundle endpoints require the broker key unless the explicit development override is enabled.

## Configuration Profiles

Set `CODEX_BROKER_CONFIG_PROFILES_JSON` or `CODEX_BROKER_CONFIG_PROFILES_FILE` to define named configuration profiles. API requests refer to one with the `configProfile` field. Profile entries may set app-server defaults such as `model`, `approvalPolicy`, `sandbox`, `personality`, `serviceTier`, `effort`, and `summary`, plus policy fields `enabledBundles` and `allowedWorkspaceRoots`. When profiles are configured, unknown `configProfile` values are rejected.

## Readiness

`GET /readyz` checks:

- SQLite state store access,
- Codex binary presence,
- configured workspace roots are readable/searchable directories,
- configured bundle roots are readable/searchable directories,
- auth data directory writability.

## Operations

- App-server children are started lazily and keyed by owner, auth profile, configuration profile, Codex command/version, credential-store mode, mounted MCP config, and hashes of resolved MCP `env:VAR` values. They are closed after `CODEX_BROKER_POOL_IDLE_TTL_SECONDS` idle seconds. TTL cleanup skips children with active turn contexts.
- Turns using broker-hosted adapters close their per-turn app-server child after finalization because the adapter config includes per-turn overlay context.
- A crashed child fails its active turns and the pool restarts lazily before later work.
- A restarted broker marks abandoned `starting`, `queued`, and `running` turns failed on startup and emits a recovered `turn.failed` event.
- App-server child start/close metadata is recorded in SQLite for operational diagnosis across broker restarts.
- During process shutdown, `CODEX_BROKER_SHUTDOWN_MODE=interrupt` interrupts active turns and finalizes them as `interrupted`; `drain` rejects new turns and waits up to `CODEX_BROKER_SHUTDOWN_DRAIN_TIMEOUT_SECONDS` for accepted work before interrupting leftovers.
- `/metrics` includes in-memory broker counters, HTTP request count/duration sums and counts by templated endpoint, SSE disconnects, turn duration sums/counts, aggregate auth start/success/failure counters, and durable audit-derived counters such as turn start, approvals, interrupts, and logout.
- Auth command spawn and timeout failures are recorded as failed auth sessions/results with redacted output and durable `auth.*.failure` audit entries. Logout still invalidates broker-managed auth files or deletes the requested profile when the Codex logout command fails.
- Raw app-server event capture is disabled by default; when enabled, secret-looking fields and strings, including JSON-like quoted secret fields, are redacted before persistence. `CODEX_BROKER_RAW_EVENT_RETENTION_SECONDS` controls startup pruning of persisted raw app-server method/params fields while preserving normalized events.
- Structured JSON logs are enabled by default with `CODEX_BROKER_JSON_LOGS=true`. Log events include templated HTTP endpoints, owner hashes, broker thread/turn ids, product correlation ids, app-server process ids, and pool-key hashes; secret-looking fields are redacted before writing.
