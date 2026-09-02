# Codex Broker

Codex Broker gives applications an HTTP API for Codex. It includes an OpenAI-compatible surface, so supported OpenAI SDK workflows can run on Codex by changing their base URL and API key.

It is more than a protocol adapter. The broker runs and manages Codex on your application's behalf: authentication, long-lived processes, threads and turns, concurrency, event streaming, execution policy, and reusable skills and tools.

Your application continues to own its users, permissions, product data, UI, prompts, and business logic. Codex Broker owns the reusable runtime infrastructure needed to operate Codex reliably.

In the normal deployment, you run one broker container beside one product app, often in the same Docker Compose project. You can deliberately share one broker across multiple apps, but that is not the main mental model.

Read the hosted [Codex Broker documentation](https://codex-broker.docs.buildwithfern.com/)
for the overview, quickstart, integration guides, and API reference. The project
spec lives in [codex-broker-spec.md](codex-broker-spec.md), while Fern is the
single source of truth for reader-facing documentation. Its source starts at
[fern/docs/pages/index.mdx](fern/docs/pages/index.mdx); preview it with
`pnpm docs:dev` and validate it with `pnpm docs:check`. Root `docs/*.md` files
are compatibility pointers for existing links and agent routing.

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

The product app owns the logged-in user, chat records, chat memory, UI streaming, and evidence behavior. The broker runs Codex for each chat thread, serializes turns for the same chat, streams normalized events, and exposes declared host tools to Codex through a mounted bundle.

### Background Jobs

Example: document review or report-normalization jobs.

The product app owns the queue, job records, input/output files, artifacts, and review workflow. The broker runs Codex turns for those jobs using the same broker API that live chat uses.

### Per-Principal Codex Auth

Example: an app where each product user brings their own Codex login.

The host app decides who the product user is and whether they may use Codex. Trusted policy resolves an auth principal, and the broker creates a separate auth home for each principal/profile, runs device auth or API-key auth, and keeps credentials out of host app databases.

### Reusable Bundles

Example: a reviewed bundle that gives Codex a skill, a prompt overlay, a mounted MCP server, or a broker-hosted tool adapter.

The broker validates and mounts the bundle. The host app still owns what its tools mean and whether a user is allowed to use them.

## What The Broker Owns

The broker owns generic Codex infrastructure:

- `codex app-server` child processes and pooling,
- per-auth-principal and per-profile `CODEX_HOME` directories,
- Codex login status, active auth probe, device auth, API-key auth, and logout,
- account-scoped model discovery for reasoning efforts, Fast and other service tiers, modalities, personality support, defaults, and upgrade metadata,
- broker-thread to Codex-thread mappings,
- turn creation, turn status, interruption, steering, and archive behavior,
- one active turn at a time per broker thread,
- normalized event persistence and Server-Sent Events streaming,
- configuration profiles for model, sandbox, approval, workspace, and bundle policy,
- default-deny managed permission profiles, sandbox preflight, and separately authorized danger access,
- mounted bundles, inline bundle validation, skill/prompt overlays, mounted MCP servers, and broker-hosted adapter transport,
- audit logs, structured logs, metrics, readiness checks, and recovery of abandoned turns after restart.

## What Host Apps Own

Host apps own app-specific behavior:

- product identity and session auth,
- deciding whether a user may call the broker,
- product database records and data models,
- UI and user-facing streaming behavior,
- prompts and app-specific assistant behavior,
- app-specific tool behavior,
- evidence search, report generation, file formats, artifacts, and job queues,
- final authorization checks inside host-owned tool endpoints.

This split is important. The broker should not know what a product evidence hit means or how a host-owned report should be reviewed. It should only expose the controlled interface that lets Codex call those host-owned capabilities.

## Important Terms

- **Host app**: the product using the broker.
- **Owner**: the product user, tenant, or service account that owns broker threads, turns, events, authorization decisions, and audit records. The API field is `ownerId`.
- **Auth principal**: the identity whose upstream Codex credentials, usage, rate limits, auth homes, and App Server pool are used. The optional API assertion is `authPrincipalId`; trusted-host policy maps it from `ownerId`, and omission defaults it to `ownerId`.
- **Codex auth profile**: a named Codex credential set under an auth principal. The API field is `profile`, and `default` is enough for many apps.
- **Broker thread**: the broker's durable thread id. Host apps submit turns to this id. Host apps may supply this id when creating a thread, or omit it and let the broker generate one.
- **Codex thread id**: the raw thread id returned by `codex app-server`. The broker stores it so host apps do not need to manage app-server details.
- **Turn**: one unit of Codex work submitted to a broker thread.
- **Bundle**: reviewed material that can provide skills, prompts, MCP servers, hosted-tool adapters, allowed paths, and sandbox policy.
- **Configuration profile**: a named set of broker-side defaults and policy for model, sandbox, approval mode, allowed bundles, and workspace roots. API requests choose one with `configProfile`.

## Security boundaries

Managed `read-only` and `workspace-write` turns use broker-owned, default-deny
Codex permission profiles. The selected working directory is canonicalized and
must be inside an authorized workspace root; the profile denies filesystem
access outside its runtime workspace roots and denies broker auth/state paths
and common workspace credential files. Routine work inside that boundary does
not need approval. By default, eligible exceptions are reviewed with Codex's
`auto_review` reviewer, while the broker's granular policy disallows an
unsandboxed shell escalation. Managed mode sends that granular policy by
default; the only supported alternative is `approvalPolicy: "never"` with the
`user` reviewer, so it never inherits an unspecified Codex approval default.

For a bundled managed turn with an explicit working directory, the broker
passes exactly two runtime roots: that canonical working directory and the
turn's ephemeral overlay. Native skill input always names
`<overlay>/.agents/skills/<skill>/SKILL.md`, never the original mounted source
path. The broker snapshots the mounted skill directory into that unique,
per-turn path, verifies its content digest before Codex starts, and rejects
symbolic links or non-regular entries. The overlay contains only disposable
bundle material and is removed when the turn ends; it may share the
workspace-write profile, but it contains no broker state, credentials, or
persistent trusted configuration. Attached skill snapshots remain read-only
during managed turns, including `workspace-write`; executable supporting files
retain their execute bits. Mount skill sources must be trusted host inputs and
read-only to the broker during a turn. The Linux release path uses POSIX
descriptor-relative snapshotting and fails closed where that support is
unavailable. Job hosts must supply the individual job directory as `cwd`, not a
parent directory containing other jobs.

The no-model sandbox preflight uses `command/exec` with its temporary workspace
as `cwd`. Pinned Codex `0.152.1` does not expose `runtimeWorkspaceRoots` on
`command/exec`, so `cwd` is the preflight command's runtime workspace root.

`danger-full-access` is intentionally outside this isolation boundary. It is
available only when the deployment configures a separate secret and the caller
also supplies it in `X-Codex-Broker-Danger-Full-Access-Key`; it is not an
ordinary caller-selectable profile.

The broker sanitizes normalized events, persisted state, history reads, native
responses, and OpenAI-compatible responses by default. `raw` sanitization mode
is for explicitly trusted debugging only: normalized output is retained and
returned unchanged, but logs and raw debug event fields are still redacted.
Host apps should avoid intentionally supplying secrets in prompts or input
items; sanitization is defense in depth, not a substitute for keeping input
data out of model context.

## Normal Request Flow

A typical host integration follows this shape.

1. The host app authenticates its own user.
2. The host app chooses an `ownerId`, usually the product user id, tenant id, or service-account id.
3. Trusted deployment policy resolves the owner's auth principal; by default it is the same id.
4. The host app checks or starts Codex auth for that principal and auth profile.
5. The host app creates or reuses a broker thread, optionally with a caller-supplied `threadId`.
6. The host app submits a turn to the broker thread.
7. The host app streams normalized broker events from `/events`.
8. The host app maps those events into its own UI, job logs, database rows, or artifacts.

Example thread create:

```json
{
  "threadId": "chat-123",
  "profile": "default",
  "hostApp": "chat-app",
  "bundleId": "example-chat-v1",
  "configProfile": "default",
  "cwd": "/workspaces/app"
}
```

If the same user or service account creates a thread with the same `threadId` again, the broker returns the existing broker thread.

The resolved auth principal, canonical `profile`, and profile instance are immutable for the lifetime of a broker thread. A turn may omit `profile`, or send the same value as a consistency assertion, but it cannot switch accounts or profiles. Reusing a `threadId` with a different binding returns a conflict.

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

Use `idempotencyKey` when a host may retry the same request. A repeated turn create with the same user or service account, broker thread, and idempotency key returns the original broker turn instead of starting duplicate Codex work.

Native turns forward ordered text and image input items to Codex. Inline images
use a base64 data URL; a native `localImage` item may instead name a path that
the Codex runtime can read. For example:

```json
{
  "input": [
    { "type": "text", "text": "Read this receipt." },
    {
      "type": "image",
      "url": "data:image/png;base64,<base64-bytes>",
      "detail": "auto"
    }
  ]
}
```

Native turn-create and steer JSON bodies may be up to 32 MiB, as may compatible
Responses and Chat Completions requests. Other routes retain the default
1,000,000-byte JSON body limit. Compatible image input
has its own limits and data-URL restrictions; see the Fern
[OpenAI compatibility guide](fern/docs/pages/integrations/openai-compatibility.mdx).

## Same-Thread Turn Behavior

The broker enforces one active turn at a time per broker thread. The `mode` field tells the broker what to do when another turn is already active:

- `reject`: fail immediately with a conflict.
- `queue`: wait until the current turn finishes, then run the new turn.
- `steer`: try to send input into the active turn; if there is no steerable active turn, behave like `reject`.

Use `queue` for background workers and for UI flows where a second request should wait. Use `reject` when the UI wants to prevent duplicate sends. Use `steer` only when the product intentionally appends input to an active Codex turn.

Different broker threads may run concurrently. Different owners may run concurrently with isolated auth homes.

## Tool And Bundle Boundary

Bundles are how host apps expose Codex capabilities without putting product logic in the broker.

They declare what Codex may see or call for a class of work; they do not install binaries or carry host state, secrets, queues, artifacts, or authorization rules.

A bundle can declare:

- mounted skills,
- mounted prompt files,
- mounted MCP servers,
- broker-hosted HTTP tool adapters,
- allowed workspace paths,
- sandbox policy.

For broker-hosted adapters, the broker acts as a transport shim. It validates the adapter declaration, resolves secret headers from environment variables, adds broker context, and forwards the tool call to a host-owned HTTP endpoint.

The host endpoint must still enforce product authorization and implement app-specific behavior.

If a bundle instruction or skill tells Codex to use a CLI, that command must already be available inside the broker/Codex runtime: installed in the broker image, mounted into the broker container, present in the mounted workspace, or runnable through the workspace's package manager. For structured tool use, declare an MCP server and allowlist its command with `CODEX_BROKER_ALLOWED_TOOL_COMMANDS`.

Mounted skills are versioned, trusted read-only inputs. A turn must not search
sibling workspaces for a missing skill or reuse scripts found there; that is an
isolation defect, not a recovery path.

For example, the sample chat bundle declares `host.evidence.search`. The broker exposes it to Codex, but the actual evidence lookup happens in the host app's `POST /internal/codex/tools/evidence-search` endpoint. The host app validates `CODEX_HOST_TOOL_KEY` and decides what evidence results mean.

## API Overview

Core endpoints:

- `GET /healthz`
- `GET /readyz`
- `GET /metrics`
- `GET /openapi.json`
- `GET /v1/models`
- `GET /v1/models/{model}`
- `POST /v1/responses`
- `GET /v1/responses/{responseId}`
- `GET /v1/responses/{responseId}/input_items`
- `POST /v1/responses/{responseId}/cancel`
- `POST /v1/chat/completions`
- `GET /v1/owners/{ownerId}/auth/status`
- `GET /v1/owners/{ownerId}/auth/profiles`
- `GET /v1/owners/{ownerId}/auth/models`
- `GET /v1/owners/{ownerId}/auth/usage`
- `GET /v1/owners/{ownerId}/auth/rate-limits`
- `POST /v1/owners/{ownerId}/auth/rate-limit-reset-credit/consume`
- `POST /v1/owners/{ownerId}/auth/probe`
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
- `GET /v1/owners/{ownerId}/threads/{threadId}/interactions`
- `GET /v1/owners/{ownerId}/threads/{threadId}/turns/{turnId}/interactions`
- `GET /v1/owners/{ownerId}/threads/{threadId}/turns/{turnId}/interactions/{interactionId}`
- `POST /v1/owners/{ownerId}/threads/{threadId}/turns/{turnId}/interactions/{interactionId}/resolve`

Requests other than health and readiness require authentication. Native broker
routes use `Authorization: Bearer <CODEX_BROKER_INTERNAL_KEY>` or
`X-Codex-Broker-Key: <CODEX_BROKER_INTERNAL_KEY>`. OpenAI-compatible routes use
`Authorization: Bearer <compatibility-key>` and resolve the caller to a
server-side identity binding. The two credential types are intentionally not
interchangeable. `/metrics` and `/openapi.json` remain native broker routes.

OpenAI SDK clients can point at the broker without changing their normal request
shape:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:3400/v1",
    api_key="a-compatibility-key-issued-by-the-operator",
)
response = client.responses.create(
    model="gpt-5.6-sol",
    input="Summarize this change.",
)
print(response.output_text)
```

The façade is Responses-first and also provides a Chat Completions adapter. It
supports text and base64 image input, streaming, response retrieval,
input-item retrieval, cancellation, response chaining, reasoning controls,
service tiers, and JSON Schema output. Compatible requests accept at most 10
images, 20 MiB per image, and 20 MiB total decoded image data; their JSON body
limit is 32 MiB. Chat `max_tokens` and `max_completion_tokens` are accepted and
discarded without imposing an output cap, while Responses `max_output_tokens`
is rejected. It fails closed for caller-defined tools, `store: false`,
sampling and logprob controls, background mode, and unsupported content.
Reviewed Codex bundles and MCP tools remain deployment policy; OpenAI request
`tools` are not treated as equivalent capabilities. Compatible images must be
PNG, JPEG, WEBP, or GIF base64 data URLs; remote URLs, file IDs, and local paths
are not accepted. See the Fern
[OpenAI compatibility guide](fern/docs/pages/integrations/openai-compatibility.mdx)
for the exact compatibility contract.

Auth status reports `missing`, `present_unverified`, `authenticated`, `invalid`, or `refresh_failed`, plus an `authFingerprint` for the principal/profile auth file. `GET /auth/profiles` lists last-recorded profile state without running Codex. `GET /auth/status` runs Codex's local login-status check, while `POST /auth/probe` runs a tiny real Codex request. Failed turns include `errorCode`, `publicMessage`, and `adminMessage`; host UIs should display `publicMessage` or `error` to end users and keep `adminMessage` for admin logs. `session_not_resumable` means Codex reported that the previous thread/session state is gone; host apps should continue in a new thread from persisted workspace context. After an administrator refreshes shared Codex auth, call `POST /v1/owners/{ownerId}/auth/runtime/invalidate` for the profile to close pooled App Server children that were started with the old auth.

Model-picker clients should call `GET /v1/owners/{ownerId}/auth/models?profile=default` instead of hardcoding model names, reasoning levels, or Fast availability. The response comes from App Server `model/list` and includes `supportedReasoningEfforts`, `defaultReasoningEffort`, `serviceTiers`, `defaultServiceTier`, modalities, personality support, defaults, hidden state, and upgrade metadata. Use the selected entry's `model` slug in `codexOptions.model`, the effort in `codexOptions.effort`, and an advertised service-tier id such as `fast` in `codexOptions.serviceTier`; the entry's `id` is the stable catalog preset identifier.

Account usage and rate-limit routes query Codex for the selected `authPrincipalHash + profile` and return the current App Server payload under `usage` or `rateLimits`. These are shared upstream totals when several owners map to the same principal. Consuming a rate-limit reset credit mutates that shared account: send a stable, non-empty `idempotencyKey`; the action is still recorded only in the requesting owner's audit log.

Completed native turns expose exact Codex-reported token accounting under
`Turn.usage`, including per-turn counts, cumulative thread counts, and the model
context window. The same update is streamed as `turn.usage.updated`. Usage is
`null` until Codex reports it and may remain unavailable when a turn ends early.

## Shared Auth Principals And Account Replacement

Set `CODEX_BROKER_AUTH_PRINCIPAL_MAP_JSON` or `CODEX_BROKER_AUTH_PRINCIPAL_MAP_FILE` to define the trusted owner-to-principal mapping. For example, `{"team-a":"shared-codex","team-b":"shared-codex"}` gives two isolated broker owners one shared Codex account. Clients may omit `authPrincipalId`; if they send it, it is only an assertion and must exactly match policy or the broker returns `403`. Never expose the broker key or raw owner/principal selection directly to browsers or other untrusted clients.

To replace the upstream Codex account inside an existing profile safely:

1. Quiesce work for every owner sharing that principal/profile.
2. Call logout with `{"profile":"work","deleteProfile":true}`. This removes credentials and profile state and invalidates every old thread binding.
3. Authenticate the replacement account into `work`.
4. Create a new broker thread with a new `threadId` (or omit it). Old and queued threads fail closed and cannot resume under the replacement account.

Logout, runtime invalidation, reset-credit consumption, and profile deletion affect the shared principal/profile even though threads and audits remain owner-scoped.

Set `CODEX_BROKER_INTERNAL_KEY` or `CODEX_BROKER_INTERNAL_KEY_FILE`. Unauthenticated mode is only for local development and requires `CODEX_BROKER_ALLOW_UNAUTHENTICATED=true`.

## Run Locally

From the repository root, run the broker through `uv`:

```bash
uv run codex-broker
```

`uv` reads [pyproject.toml](pyproject.toml), builds the local package, and runs
the `codex-broker` console script. Set environment variables before starting
the process. The complete configuration reference is in
[Fern](fern/docs/pages/operations/configuration-reference.mdx).

Useful local environment:

```env
CODEX_BROKER_HOST=127.0.0.1
CODEX_BROKER_PORT=3400
CODEX_BROKER_DATA_DIR=.data
CODEX_BROKER_ALLOWED_WORKSPACE_ROOTS=/path/to/workspaces
CODEX_BROKER_ALLOWED_BUNDLE_ROOTS=/path/to/bundles
CODEX_BROKER_ALLOWED_TOOL_COMMANDS=python,node
CODEX_BROKER_ALLOWED_HOSTED_TOOL_URL_PREFIXES=http://127.0.0.1,http://localhost,http://host.docker.internal
CODEX_BROKER_INTERNAL_KEY=dev-only-key
CODEX_BROKER_OPENAI_COMPAT_BINDINGS_FILE=/run/secrets/codex-broker-openai-bindings.json
CODEX_BROKER_PASSTHROUGH_ENV=ESTF_ARCHIVER_API_URL,ESTF_ARCHIVER_API_KEY
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
# CODEX_BROKER_AUTH_PRINCIPAL_MAP_JSON={"team-a":"shared-codex","team-b":"shared-codex"}
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
  -e CODEX_BROKER_INTERNAL_KEY=dev-only-key \
  codex-broker
```

Override the pinned Codex version with `--build-arg CODEX_VERSION=<version>`.

Managed sandbox deployments need the shipped
[`examples/seccomp/codex-broker.json`](examples/seccomp/codex-broker.json)
profile. It starts from Docker Engine 29.4.0's pinned Moby seccomp v0.1.0
default and adds only Bubblewrap's required mount/pivot-root calls,
`umount2(MNT_DETACH)`, `clone` calls containing `CLONE_NEWUSER`, and exact
`unshare(CLONE_NEWUSER)` or `unshare(CLONE_NEWNS)` calls. The example Compose
service loads it with `no-new-privileges:true`:

```bash
docker compose -f examples/docker-compose.yml config
docker compose -f examples/docker-compose.yml up -d
```

CI runs the no-model sandbox canary using the same profile before publishing an
image. It checks an attached overlay snapshot, its read-only mounted source, an
ordinary job workspace read/write, and sibling-job sentinel, output, and skill
paths. `command/exec` has no multi-root parameter, so the canary runs from the
snapshot root and proves that neither the snapshot nor its mounted source can
be modified. Parameter tests assert the exact ordered `[cwd, overlay]` roots on
normal thread and turn calls. Do not publish an image until a release-time Linux
container canary has also exercised the actual job-plus-overlay roots. Do not replace it with
`seccomp=unconfined`, privileged mode, or
`CAP_SYS_ADMIN`; those remove the outer-container protection that makes the
managed profile meaningful.

Docker hosts with AppArmor also need the shipped profile that preserves
Docker's default container restrictions while allowing Bubblewrap's nested
user-namespace mounts:

```bash
sudo apparmor_parser -r -W examples/apparmor/codex-broker-bwrap
docker compose \
  -f examples/docker-compose.yml \
  -f examples/docker-compose.apparmor.yml \
  up -d
```

Do not substitute `apparmor=unconfined`; hosts without AppArmor should use the
base Compose file alone.

See the Fern [deployment guide](fern/docs/pages/operations/deployment.mdx) and
[examples/docker-compose.yml](examples/docker-compose.yml) for a Docker Compose
example.

## Current Integrations

Implemented integration examples:

- A chat app can keep product prompt construction, chat state, UI streaming, and evidence behavior while the broker receives Codex turns and exposes the declared `host.evidence.search` adapter.
- A job worker can keep job records, queueing, artifacts, review rows, and UI streaming while the broker receives job turns and manages Codex thread and turn state.
- Example mounted bundles live under [examples/bundles](examples/bundles).
- Host clients are available in Python and TypeScript.

Still outside this repo:

- enabling a concrete chat integration in production deployment,
- enabling a concrete job-worker integration in production deployment,
- deciding whether inline bundles are needed in production.

## Development Status

Implemented in this repo:

- auth-principal/profile auth homes with HMAC-derived paths,
- API-key, device-auth, status, active probe, logout, and explicit profile deletion flows,
- app-server stdio pooling with lazy restart after child failure,
- profile defaults and policy checks for model, approval, sandbox, enabled bundles, and workspace roots,
- startup recovery that marks abandoned `starting`, `queued`, and `running` turns failed after a broker restart,
- idle app-server pool cleanup after `CODEX_BROKER_POOL_IDLE_TTL_SECONDS`,
- explicit shutdown handling that rejects new turns and either interrupts or drains accepted work,
- request waiters and turn contexts for JSON-RPC routing,
- per-thread `reject`, `queue`, and `steer` turn behavior,
- normalized event persistence and SSE streaming with product correlation and Codex ids,
- optional caller-supplied broker `threadId` values for host chat or job ids,
- safe-by-default secret sanitization for persistence and egress, split-secret streaming protection, and mandatory redaction for logs and raw debug fields,
- managed default-deny permission profiles with sandbox preflight/readiness checks, separated runtime homes, and separately authorized `danger-full-access`,
- user-scoped audit log API for auth, turn, approval, interrupt, and logout events,
- durable app-server child process lifecycle records for operational diagnosis,
- app-server 0.146.0 model discovery and mode/capability event coverage for plan, goal, review, approvals, user input, and MCP elicitations,
- host-mediated approval, user-input, and MCP elicitation interaction records with resolve APIs and fail-closed fallback,
- mounted bundles, inline bundle validation, skills/prompt overlays, mounted MCP, and broker-hosted tool adapters,
- readiness checks, Prometheus-style metrics, structured JSON logs, and schema-backed `/openapi.json`.
- a typed TypeScript client under `clients/typescript`, plus Fern configuration for regenerating a full SDK from the OpenAPI contract.

## Tests

```bash
uv run python -m unittest discover -s tests
```

For warning-sensitive verification:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run python -W always::ResourceWarning -m unittest discover -s tests
```

Regenerate the API contract and typed TypeScript SDK with:

```bash
pnpm openapi:generate
pnpm sdk:generate
```

## More Reading

- [Fern overview](fern/docs/pages/index.mdx): responsibilities, runtime loop, and reader paths.
- [OpenAI compatibility](fern/docs/pages/integrations/openai-compatibility.mdx): SDK setup, identity bindings, supported endpoints, streams, chaining, and explicit limits.
- [Host integration](fern/docs/pages/integrations/host-integration.mdx): how native product backends and workers call the broker.
- [Configuration reference](fern/docs/pages/operations/configuration-reference.mdx): environment variables, compatibility bindings, profiles, and request options.
- [Architecture](fern/docs/pages/runtime/architecture.mdx): process boundaries, storage, scheduling, pooling, recovery, and security.
- [App-server modes](fern/docs/pages/runtime/app-server-modes.mdx): version-pinned Codex protocol and capability coverage.
- [Deployment](fern/docs/pages/operations/deployment.mdx): Docker mounts, secrets, readiness, and shutdown behavior.
- [examples/bundles/README.md](examples/bundles/README.md): example task bundles and hosted-tool declarations.
