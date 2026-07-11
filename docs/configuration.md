# Configuration

read_when: changing environment variables, Docker configuration, config profiles, request runtime options, bundle manifests, hosted tools, MCP server validation, sandbox policy, or workspace policy.

This is the reference for Codex Broker configuration. It covers process environment variables, Docker build arguments, configuration profiles, request-level Codex options, and task bundle manifest fields.

## Parsing Rules

Environment variables are read once at process startup by `BrokerConfig.from_env()`.

| Value type | Parsing |
| --- | --- |
| Boolean | `1`, `true`, `yes`, and `on` are true, case-insensitive. Any other set value is false. |
| Integer | Parsed with `int()`. Empty values use the default. |
| Float seconds | Parsed with `float()`. |
| CSV | Comma-separated, trimmed, empty items ignored. |
| Path list | Colon-separated, expanded with `~`, resolved to absolute paths, empty items ignored. |
| JSON | Parsed as UTF-8 JSON object. Invalid shape fails startup or request handling. |
| `CODEX_BIN` | Parsed with shell-like splitting, then broker appends Codex subcommands. |

## Environment Variables

### Server And Authentication

| Variable | Default | Description |
| --- | --- | --- |
| `CODEX_BROKER_HOST` | `127.0.0.1` | Host interface for the HTTP server. Docker sets `0.0.0.0`. |
| `CODEX_BROKER_PORT` | `3400` | HTTP server port. |
| `CODEX_BROKER_INTERNAL_KEY` | unset | Shared internal API key. Required unless unauthenticated dev mode is enabled. |
| `CODEX_BROKER_INTERNAL_KEY_FILE` | unset | File containing the internal API key. Used only when `CODEX_BROKER_INTERNAL_KEY` is not set. |
| `CODEX_BROKER_ALLOW_UNAUTHENTICATED` | `false` | Development escape hatch. When true and no key is configured, product routes accept unauthenticated requests. |
| `CODEX_BROKER_OWNER_HASH_KEY` | persistent generated key | Explicit HMAC key for owner hashes. When unset, the broker creates `state/owner-hash.key` once and reuses it across API-key rotations. |
| `CODEX_BROKER_OWNER_HASH_KEY_FILE` | unset | File containing the owner-hash HMAC key. Mutually exclusive with `CODEX_BROKER_OWNER_HASH_KEY`. |
| `CODEX_BROKER_AUTH_PRINCIPAL_MAP_JSON` | unset | Trusted JSON object mapping each `ownerId` to the `authPrincipalId` whose Codex account it may use. |
| `CODEX_BROKER_AUTH_PRINCIPAL_MAP_FILE` | unset | File containing the same mapping. Mutually exclusive with the JSON variable. |

Only `GET /healthz` and `GET /readyz` are unauthenticated by design. `/metrics`, `/openapi.json`, `/v1/...`, and `/v1/bundles/inline` require the broker key unless the development override is active. A distinct mapped auth principal requires an authenticated trusted-host connection; `authPrincipalId` in a request is an assertion checked against this deployment policy, never a free-form client choice.

### Filesystem And Policy Roots

| Variable | Default | Description |
| --- | --- | --- |
| `CODEX_BROKER_DATA_DIR` | `.data` | Persistent broker data root. Contains SQLite state, auth homes, inline bundles, and per-turn overlays. |
| `CODEX_BROKER_ALLOWED_WORKSPACE_ROOTS` | current working directory | Colon-separated host workspace roots. Request `cwd` values and bundle `allowedPaths` must be inside these roots unless the broker overlay root is used. |
| `CODEX_BROKER_ALLOWED_BUNDLE_ROOTS` | current working directory | Colon-separated reviewed bundle roots. Mounted bundle files, mounted skills, and mounted prompts must be inside these roots. |

Derived paths under `CODEX_BROKER_DATA_DIR`:

| Path | Purpose |
| --- | --- |
| `state/broker.sqlite` | SQLite state store. |
| `state/owner-hash.key` | Mode-`0600` persistent owner-hash key. Back it up with the database and auth data. |
| `auth/owners/<auth-principal-hash>/profiles/<profile>/codex-home` | Per-auth-principal/profile Codex home. The legacy `owners` directory name is preserved so existing installations need no filesystem move. |
| `bundles/inline/<digest>/bundle.json` | Accepted inline bundle content. |
| `workspaces/overlays/<turn-id>` | Per-turn generated files, symlinks, MCP config, and hosted-tool config. |

### Codex Command And Credentials

| Variable | Default | Description |
| --- | --- | --- |
| `CODEX_BIN` | `codex` | Codex CLI command. May include arguments because the value is split with `shlex.split()`. |
| `CODEX_CREDENTIAL_STORE` | `file` | Value written to each auth-principal/profile `config.toml` as `cli_auth_credentials_store` and passed to Codex children. |
| `CODEX_BROKER_PASSTHROUGH_ENV` | empty | CSV allowlist of exact environment variable names copied into app-server child processes in addition to the safe base environment. |

The broker always sets `CODEX_HOME`, `CODEX_CREDENTIAL_STORE`, and `HOME` for Codex auth commands and app-server children. The child process environment is otherwise scrubbed; variables containing `TOKEN`, `SECRET`, `KEY`, or `PASSWORD` are not passed unless explicitly allowlisted.

### Turn Execution And Pooling

| Variable | Default | Description |
| --- | --- | --- |
| `CODEX_BROKER_MAX_ACTIVE_TURNS` | `0` | Global active-turn cap. `0` uses the broker's bounded 32-worker default; same-thread serialization still applies. |
| `CODEX_BROKER_MAX_QUEUED_TURNS` | `1000` | Maximum turns waiting behind active turns. Further queue requests fail closed. |
| `CODEX_BROKER_POOL_IDLE_TTL_SECONDS` | `900` | Idle app-server children older than this are closed. Set `0` to disable idle sweeping. |
| `CODEX_BROKER_REQUEST_TIMEOUT_SECONDS` | `60` | Timeout for JSON-RPC request/response calls to an app-server child. |
| `CODEX_BROKER_TURN_TIMEOUT_SECONDS` | `0` | Maximum wall time for a Codex turn. `0` disables broker-level turn timeout. |
| `CODEX_BROKER_HOST_RESPONSE_TIMEOUT_SECONDS` | `30` | Time an app-server approval, permission request, user-input prompt, or MCP elicitation waits for host resolution before fallback. |

For user-input prompts, Codex may provide `autoResolutionMs`; the broker uses the smaller positive value between that prompt timeout and `CODEX_BROKER_HOST_RESPONSE_TIMEOUT_SECONDS`.

### Bundles, MCP, And Hosted Tools

| Variable | Default | Description |
| --- | --- | --- |
| `CODEX_BROKER_ALLOWED_TOOL_COMMANDS` | empty | CSV allowlist for bundle-declared MCP server commands. Entries may be command names such as `node`, or exact absolute executable paths. |
| `CODEX_BROKER_ALLOWED_HOSTED_TOOL_URL_PREFIXES` | `http://127.0.0.1,http://localhost,http://host.docker.internal` | CSV allowlist for broker-hosted HTTP tool endpoints. Match is by parsed scheme and host, with optional explicit port and path prefix. |
| `CODEX_BROKER_ENABLE_INLINE_BUNDLES` | `false` | Enables `POST /v1/bundles/inline`. |
| `CODEX_BROKER_INLINE_BUNDLE_MAX_BYTES` | `262144` | Maximum serialized inline bundle payload size. |
| `CODEX_BROKER_HOSTED_TOOL_MAX_RESPONSE_BYTES` | `1048576` | Default maximum hosted-tool response or error body. Bundles may override it with `maxResponseBytes`. |

Inline bundles are stored by content digest. Re-sending the same payload is idempotent. A mounted bundle id cannot be shadowed by an inline bundle, and an accepted inline bundle id cannot later be reused with different content.

An empty hosted-tool URL allowlist disables hosted tools. The adapter never follows redirects and rejects response bodies above its configured byte limit.

### Logging, Events, And Shutdown

| Variable | Default | Description |
| --- | --- | --- |
| `CODEX_BROKER_DEBUG_RAW_EVENTS` | `false` | When true, persists redacted raw app-server method and params beside normalized events. |
| `CODEX_BROKER_RAW_EVENT_RETENTION_SECONDS` | `604800` | Startup pruning window for raw app-server method/params fields. |
| `CODEX_BROKER_HISTORY_RETENTION_SECONDS` | `7776000` | Retention for completed turns, normalized events, resolved interactions, audit logs, and closed child records. `0` disables age pruning. |
| `CODEX_BROKER_MAX_EVENTS_PER_TURN` | `10000` | Maximum normalized events retained per turn. |
| `CODEX_BROKER_JSON_LOGS` | `true` | Emits structured JSON logs to stderr. Set false to suppress broker JSON logs. |
| `CODEX_BROKER_SHUTDOWN_MODE` | `interrupt` | `interrupt` or `drain`. Invalid values behave as `interrupt`. |
| `CODEX_BROKER_SHUTDOWN_DRAIN_TIMEOUT_SECONDS` | `30` | Drain wait before interrupting leftovers when shutdown mode is `drain`. |

Logs and raw event payloads are redacted for common API key, token, bearer, password, secret, credential, and cookie fields.

### Configuration Profiles

| Variable | Default | Description |
| --- | --- | --- |
| `CODEX_BROKER_CONFIG_PROFILES_JSON` | unset | JSON object keyed by `configProfile` name. Takes precedence over the file variable. |
| `CODEX_BROKER_CONFIG_PROFILES_FILE` | unset | File containing the same JSON object. A configured missing, non-file, or empty path is a startup error. |

When no configuration profiles are configured, any request `configProfile` name is accepted and no profile defaults are applied. When profiles are configured, every request profile must exist in the configured object. Requests that omit `configProfile` use `default`, so include a `default` entry if you enable profiles and want omitted profile fields to work.

## Configuration Profile Shape

Configuration profiles are JSON objects keyed by profile name:

```json
{
  "default": {
    "model": "gpt-5",
    "approvalPolicy": "on-request",
    "sandbox": "workspace-write",
    "enabledBundles": ["example-chat-v1"],
    "allowedWorkspaceRoots": ["/workspaces/app"]
  },
  "background-review": {
    "model": "gpt-5",
    "effort": "high",
    "summary": "auto",
    "webSearch": "enabled",
    "modelVerbosity": "low",
    "features": {
      "image_generation": false
    },
    "enabledBundles": ["document-jobs-v1"],
    "allowedWorkspaceRoots": ["/workspaces/app", "/host-data/jobs"]
  }
}
```

Supported keys:

| Key | Applies to | Description |
| --- | --- | --- |
| `approvalPolicy` | Codex thread | Passed to `thread/start` or `thread/resume` unless overridden by `codexOptions.approvalPolicy`. |
| `sandbox` | Codex thread | Default sandbox string. A request `codexOptions.sandbox` wins, then bundle `sandbox.mode`, then profile `sandbox`. |
| `model` | Codex thread and turn | Passed to thread and turn calls unless overridden by request options. |
| `personality` | Codex thread and turn | Passed to thread and turn calls unless overridden by request options. |
| `serviceTier` | Codex turn | Passed to `turn/start`. |
| `effort` | Codex turn and process config | Passed to `turn/start` and also mapped to Codex process config `model_reasoning_effort`. |
| `reasoningEffort` | Codex turn and process config | Alias for `effort`. |
| `modelReasoningEffort` | Process config | Alias used only for process config `model_reasoning_effort`. |
| `model_reasoning_effort` | Process config | Alias used only for process config `model_reasoning_effort`. |
| `summary` | Codex turn | Passed to `turn/start`. |
| `reasoningSummary` | Codex turn | Alias for `summary`. |
| `outputSchema` | Codex turn | JSON Schema for the final assistant message. |
| `output_schema` | Codex turn | Alias for `outputSchema`. |
| `webSearch` | Process config | Mapped to `-c web_search=<value>` when starting app-server. |
| `web_search` | Process config | Alias for `webSearch`. |
| `modelVerbosity` | Process config | Mapped to `-c model_verbosity=<value>`. |
| `model_verbosity` | Process config | Alias for `modelVerbosity`. |
| `imageGeneration` | Process feature config | Mapped to `-c features.image_generation=<value>`. |
| `features.image_generation` | Process feature config | Alias for `imageGeneration`. |
| `features` | Process feature config | Object of feature names to values, mapped to `features.<safe-name>=<value>`. |
| `enabledBundles` | Broker policy | Bundle allowlist for this profile. |
| `bundleIds` | Broker policy | Alias for `enabledBundles`. |
| `bundles` | Broker policy | Alias for `enabledBundles`. |
| `allowedWorkspaceRoots` | Broker policy | Per-profile cwd allowlist checked after global workspace or bundle validation. |
| `workspaceRoots` | Broker policy | Alias for `allowedWorkspaceRoots`. |

Request-level `codexOptions` override profile defaults for matching Codex options. The app-server pool key includes process-level options, so requests with different process-level settings do not accidentally reuse incompatible child processes.

### Model And Reasoning Selection

A turn can choose its model and reasoning effort directly:

```json
{
  "input": [{ "type": "text", "text": "Review these changes." }],
  "codexOptions": {
    "model": "gpt-5.6-sol",
    "effort": "high"
  }
}
```

Selection precedence is:

1. Turn request `codexOptions.model` and `codexOptions.effort`.
2. The selected configuration profile's `model` and `effort` defaults.
3. Codex's current recommended model and model-specific reasoning default.

The broker does not persist a request-level selection back into the configuration profile. `reasoningEffort` is accepted as an alias for `effort`. Model availability and supported effort values come from Codex and may differ by account, provider, and selected model.

## Request-Level Options

Thread create accepts:

| Field | Description |
| --- | --- |
| `threadId` | Optional caller-supplied stable broker thread id. Reusing the same owner/thread id returns the existing thread. |
| `authPrincipalId` | Optional assertion of the trusted owner-to-principal mapping. Omission uses configured policy, then defaults to `ownerId`. |
| `profile` | Auth profile under the resolved principal. Defaults to `default`; returned value is canonicalized and immutable for the thread lifetime. |
| `configProfile` | Configuration profile name. Defaults to `default`. |
| `runtimeProfile` | Deprecated alias for `configProfile`. |
| `hostApp` | Optional host app name for logs, metrics, and returned objects. |
| `bundleId` | Optional task bundle id. |
| `cwd` | Optional Codex working directory. Must be allowed by broker roots, bundle paths, and profile roots when configured. |

Turn start accepts:

| Field | Description |
| --- | --- |
| `input` | Required non-empty array of Codex input items. |
| `mode` | `reject`, `queue`, or `steer`. Defaults to `reject`. |
| `authPrincipalId` | Optional consistency assertion. It must resolve to the thread's immutable auth principal. |
| `profile` | Optional consistency assertion. It must canonicalize to the thread's immutable auth profile; it cannot override it. |
| `configProfile` | Overrides the thread config profile for this turn. |
| `runtimeProfile` | Deprecated alias for `configProfile`. |
| `hostApp` | Overrides or inherits the thread host app. |
| `bundleId` | Overrides or inherits the thread bundle id. |
| `cwd` | Overrides or inherits the thread cwd. |
| `codexOptions` | Canonical Codex option object. |
| `runtime` | Deprecated alias merged before `codexOptions`; `codexOptions` wins on conflicts. |
| `idempotencyKey` | Prevents duplicate work for retries on the same owner/thread. |
| `productCorrelationId` | Host trace id persisted on events and logs. |
| `correlationId` | Alias for `productCorrelationId`. |
| `stream` | Present in OpenAPI for compatibility; current broker always persists events and returns `streamUrl`. |

Supported `codexOptions` keys match the configuration profile keys listed above, plus aliases. Options that affect app-server process config are included in the pool key. Options that affect a single thread or turn are sent in the corresponding app-server request.

The broker persists the resolved auth-principal hash, canonical profile, and profile-instance id on every thread and turn. Existing schema-v2 installations migrate with `authPrincipalId = ownerId`. Legacy threads that historically used several turn profiles are marked unsafe and must be replaced instead of resumed.

## Bundle Manifest Shape

Mounted bundles are resolved from each `CODEX_BROKER_ALLOWED_BUNDLE_ROOTS` entry in this order:

1. `<root>/<bundleId>/bundle.json`
2. `<root>/<bundleId>.json`
3. `<root>/<bundleId>`
4. Any recursive `bundle.json` under the root whose JSON `id` equals `bundleId`

Base manifest fields:

| Field | Required | Description |
| --- | --- | --- |
| `id` | yes | Bundle id selected by request `bundleId`. |
| `version` | no | Informational version string. |
| `instructions` | no | Array of strings injected before host input and written to overlay `AGENTS.md`. |
| `skills` | no | Mounted skill entries. |
| `prompts` | no | Mounted prompt entries injected before host input. |
| `mcpServers` | no | Structured MCP server declarations. |
| `tools` | no | Broker-hosted HTTP adapter declarations. |
| `allowedPaths` | no | Additional cwd roots for this bundle; each must be inside global workspace roots. |
| `sandbox.mode` | no | Bundle default sandbox when request options do not set one. |

### Skills

```json
{
  "name": "normalize-citations",
  "source": {
    "type": "mount",
    "path": "/bundles/document-jobs-v1/skills/normalize-citations"
  }
}
```

Only mounted skills are supported. The path must be under an allowed bundle root and must either be a `SKILL.md` file or a directory containing `SKILL.md`. The broker links the skill directory into the overlay and prepends a Codex `skill` input item.

### Prompts

```json
{
  "name": "legacy-review-prompt",
  "source": {
    "type": "mount",
    "path": "/bundles/review/prompts/legacy-review.md"
  }
}
```

Prompt sources default to `type: "mount"` when omitted. The path must be a file under an allowed bundle root. The broker links the file into the overlay and injects its text before the host turn input.

### MCP Servers

```json
{
  "name": "host-files",
  "command": "node",
  "args": ["/bundles/tools/host-files-mcp.js"],
  "env": {
    "HOST_FILES_TOKEN": "env:HOST_FILES_TOKEN"
  },
  "cwd": "/bundles/tools"
}
```

Rules:

- `name` and `command` are required.
- `command` must match `CODEX_BROKER_ALLOWED_TOOL_COMMANDS` by command name or exact absolute path.
- `args` are passed as strings.
- `cwd`, when set, must be inside an allowed bundle root or workspace root.
- Secret-looking env keys must use `env:VAR` indirection.
- `env:VAR` values are resolved from the broker process environment and passed to the app-server child environment.
- Literal env values are written into generated Codex MCP config.

### Broker-Hosted Tools

```json
{
  "name": "host.evidence.search",
  "type": "broker-hosted",
  "description": "Search host-owned product evidence.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": { "type": "string" },
      "limit": { "type": "integer", "minimum": 1, "maximum": 20 }
    },
    "required": ["query"]
  },
  "context": {
    "capability": "evidence-search"
  },
  "policy": {
    "approval": "on-request",
    "scope": "profile"
  },
  "networkPolicy": {
    "mode": "host-allowlist"
  },
  "http": {
    "url": "http://app:3000/internal/codex/tools/evidence-search",
    "headers": {
      "X-Codex-Tool-Key": "env:CODEX_HOST_TOOL_KEY"
    },
    "timeoutSeconds": 30
  }
}
```

Supported fields:

| Field | Description |
| --- | --- |
| `name` | Required tool name exposed to Codex. |
| `type` or `adapter` | `broker-hosted` or `host-http`. Defaults to `broker-hosted`. |
| `description` | Tool description shown to Codex. |
| `inputSchema` | JSON Schema for tool arguments. Defaults to `{ "type": "object" }`. |
| `context` | Opaque JSON object forwarded to the host endpoint. |
| `policy.approval`, `approval`, `approvalPolicy` | `never`, `on-request`, or `always`. Defaults to `never`. |
| `policy.scope`, `scope` | `owner` or `profile`. Defaults to `owner`. |
| `networkPolicy`, `policy.networkPolicy`, `policy.network` | Only `host-allowlist` is supported. |
| `http.url`, `http.endpoint`, `url`, `endpoint` | HTTP or HTTPS endpoint. Must match `CODEX_BROKER_ALLOWED_HOSTED_TOOL_URL_PREFIXES`. |
| `http.headers`, `headers` | Header map. Secret-looking header names must use `env:VAR`. |
| `http.timeoutSeconds`, `timeoutSeconds` | HTTP call timeout. Defaults to `30`. |

The adapter sends a `POST` request with this JSON body:

```json
{
  "tool": "host.evidence.search",
  "arguments": { "query": "example" },
  "context": {
    "broker": {
      "ownerHash": "<owner-hash>",
      "threadId": "<broker-thread-id>",
      "turnId": "<broker-turn-id>",
      "hostApp": "chat-app",
      "productCorrelationId": "chat-123:message-456",
      "configProfile": "default",
      "profile": "default"
    },
    "tool": {
      "capability": "evidence-search"
    },
    "policy": {
      "approvalPolicy": "on-request",
      "scope": "profile",
      "networkPolicy": {
        "mode": "host-allowlist"
      }
    }
  }
}
```

The adapter always sets `Content-Type: application/json`, `Accept: application/json`, and `X-Codex-Broker-Tool: <tool-name>`. Declared headers are added after resolving `env:VAR` references.

Host endpoints may return ordinary JSON or text. Ordinary JSON is formatted as text for Codex. If the JSON response already has MCP tool result shape with `content`, optional `isError`, optional `structuredContent`, and optional `_meta`, the adapter passes it through directly.

## Docker Build Arguments

| Build arg | Default | Description |
| --- | --- | --- |
| `CODEX_VERSION` | `0.144.0` | Codex CLI release version without the `rust-v` prefix. |
| `TARGETARCH` | supplied by BuildKit | `amd64` maps to `x86_64`; `arm64` maps to `aarch64`. Other values fail the build. |

The Docker image installs the official Codex CLI Linux musl archive, installs the broker package, runs as the non-root `broker` user, exposes port `3400`, and declares `/data` as a volume.

## Minimal Local Example

```env
CODEX_BROKER_HOST=127.0.0.1
CODEX_BROKER_PORT=3400
CODEX_BROKER_DATA_DIR=.data
CODEX_BROKER_ALLOWED_WORKSPACE_ROOTS=/path/to/workspaces
CODEX_BROKER_ALLOWED_BUNDLE_ROOTS=/path/to/bundles
CODEX_BROKER_ALLOWED_TOOL_COMMANDS=python,node
CODEX_BROKER_INTERNAL_KEY=dev-only-key
CODEX_BIN=codex
CODEX_CREDENTIAL_STORE=file
```

Run with:

```bash
uv run codex-broker
```
