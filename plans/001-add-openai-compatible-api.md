# Plan 001: Expose a verified OpenAI-compatible API façade

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat e65d754..HEAD -- Dockerfile pyproject.toml uv.lock src/codex_broker tests README.md codex-broker-spec.md docs fern/openapi/openapi.json plans/README.md`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: none
- **Category**: direction
- **Planned at**: commit `e65d754`, 2026-07-28

## Why this matters

Codex Broker already owns durable threads, turn scheduling, model discovery,
streaming, authentication profiles, and a long-lived Codex app-server pool.
An OpenAI-compatible HTTP façade would let existing libraries use that runtime
by changing only `base_url` and `api_key`, instead of requiring every host to
adopt the broker's owner/thread/turn API.

The Codex app-server is not itself an OpenAI-compatible REST server: its public
transport is JSON-RPC over JSONL, with an experimental WebSocket transport.
However, it provides the right backend primitives, including
`rawResponseItem/completed`, `thread/tokenUsage/updated`,
`thread/inject_items`, `model/list`, and thread/turn lifecycle methods. Build a
thin protocol façade on those primitives; do not add a second model-execution
stack or expose app-server JSON-RPC directly.

The first releasable target is a truthful OpenAI-compatible subset for text
and structured-output clients. Caller-defined function tools have a lifecycle
mismatch described in Step 8 and must remain explicitly unsupported until that
design passes a dedicated compatibility gate. Do not advertise universal
drop-in compatibility before that gate passes.

## Current state

### Repository architecture and constraints

- `Dockerfile:3` pins the production Codex CLI:

  ```dockerfile
  ARG CODEX_VERSION=0.144.6
  ```

- `codex-broker-spec.md:21-35` requires a product-facing internal service, not
  raw JSON-RPC or a public-internet API:

  ```markdown
  - Keep the broker product-facing, not a raw JSON-RPC passthrough.
  ...
  - Public internet exposure. The broker is an internal service.
  ```

- `src/codex_broker/services.py:20-55` builds one service graph containing
  config, state, auth, bundles, app-server pool, and scheduler. Add the
  compatibility auth/service here instead of constructing parallel state or
  pools per HTTP request.

- `src/codex_broker/http_api.py:59-105` dispatches all routes from one
  `ThreadingHTTPServer` handler. `src/codex_broker/http_api.py:331-338`
  currently applies one internal key globally:

  ```python
  def _authorized(self) -> bool:
      key = self.broker.config.internal_key
      if not key:
          return self.broker.config.allow_unauthenticated
      auth = self.headers.get("Authorization", "")
      if auth == f"Bearer {key}":
          return True
      return self.headers.get("X-Codex-Broker-Key") == key
  ```

  Compatibility routes need their own bearer-key resolver before this native
  authorization branch. Native `/v1/owners/...` behavior must not change.

- `src/codex_broker/http_api.py:245-280` already implements chunked SSE over
  persisted events, while `src/codex_broker/http_api.py:361-373` contains the
  low-level chunk writer. Reuse the wire-writing mechanism, but emit exact
  OpenAI event bodies rather than normalized broker SSE envelopes.

- `src/codex_broker/account_api.py:16-43` already calls app-server
  `model/list` under owner/principal/profile isolation. Extract or reuse that
  model-discovery service for `GET /v1/models`; do not make the compatibility
  route call the native HTTP route.

- `src/codex_broker/scheduler_config.py:44-88` maps the stable model, service
  tier, reasoning, and output-schema options:

  ```python
  for request_key, app_server_key, aliases in (
      ("serviceTier", "serviceTier", ()),
      ("model", "model", ()),
      ("effort", "effort", ("reasoningEffort",)),
      ("personality", "personality", ()),
      ("summary", "summary", ("reasoningSummary",)),
  ):
      ...
  if output_schema is not None:
      params["outputSchema"] = output_schema
  ```

  The same mapping should remain the single source of truth after OpenAI
  request fields are translated into broker turn options.

- `src/codex_broker/scheduler.py:705-761` builds the input, creates or resumes
  the app-server thread, starts the turn, and waits for the terminal
  notification. The history-injection hook must run after
  `_ensure_codex_thread(...)` and before `turn/start`.

- `src/codex_broker/app_server.py:205-221` already initializes app-server with
  `experimentalApi: True`, which is required by the raw Responses event
  surface. `src/codex_broker/app_server.py:486-491` rejects every server
  request that is not one of the broker's existing interactions:

  ```python
  if method in INTERACTION_REQUEST_METHODS:
      self._handle_interaction_request(...)
  else:
      self.send({
          "id": message_id,
          "error": {
              "code": -32601,
              "message": f"Unsupported App Server request: {method}",
          },
      })
  ```

  This is why app-server `dynamicTools` cannot simply be passed through as
  OpenAI function tools.

- `src/codex_broker/events.py:53-212` normalizes app-server notifications but
  currently collapses unknown completed notifications to `item.completed`.
  Add explicit stable event types for raw response items and token usage;
  never rely on optional debug-only `raw_method` or `raw_params`.

- `src/codex_broker/state_schema.py:42-105` stores owner-scoped threads, turns,
  and events. A turn already contains its original input and
  `resolved_options_json`; events already contain stable JSON payloads. Use
  those records to reconstruct compatible responses rather than introducing a
  second response store.

- `src/codex_broker/state.py:292-298` can find a turn only when its broker
  thread id is known. Compatibility response ids encode the broker turn id, so
  add an owner-scoped `find_turn_by_turn_id(owner_hash, turn_id)` query and an
  additive `(owner_hash, turn_id)` index.

- `src/codex_broker/util.py:33-34` creates broker turn ids as
  `turn_<random-token>`. Use a reversible, validated external mapping:
  `turn_<token>` to `resp_<token>` for Responses and
  `chatcmpl_<token>` for Chat Completions. Never accept arbitrary ids by string
  concatenation; parse the expected prefix and validate the token before
  looking it up.

- `src/codex_broker/state_schema.py:8-17` rejects incompatible database
  versions. The new index is additive and can be created with
  `CREATE INDEX IF NOT EXISTS` without changing stored row shapes. If
  implementation requires new columns or tables, stop and design a real v3 to
  v4 migration instead of silently changing the exact-version contract.

- `pyproject.toml:1-17` has no runtime or development dependencies. Preserve
  the standard-library-only runtime. The official `openai` package may be
  added only to the dev dependency group for black-box SDK tests.

### What app-server already provides

Before editing code, generate the protocol schemas from the repository-pinned
Codex version. In the schema expected for 0.144.6:

- `ThreadStartParams.experimentalRawEvents` enables
  `rawResponseItem/completed`.
- `RawResponseItemCompletedNotification` contains `threadId`, `turnId`, and a
  raw Responses `item`.
- `ThreadTokenUsageUpdatedNotification` contains last-turn and total token
  usage, including cached and reasoning tokens.
- `thread/inject_items` accepts raw Responses API items and appends them to
  model-visible history.
- `ThreadStartParams` contains base/developer instructions.
- `dynamicTools` causes an app-server request named `item/tool/call`; the
  app-server expects the host to return the tool result before the Codex turn
  can continue.

Authoritative references:

- Codex app-server protocol:
  <https://learn.chatgpt.com/docs/app-server#protocol>
- Codex app-server API overview:
  <https://learn.chatgpt.com/docs/app-server#api-overview>
- Responses-to-Chat migration and item semantics:
  <https://developers.openai.com/api/docs/guides/migrate-to-responses#migrating-from-chat-completions>
- Compatibility verification:
  <https://developers.openai.com/cookbook/articles/gpt-oss/verifying-implementations#quick-verification-of-tool-calling-and-api-shapes>

### Target request flow

```text
OpenAI SDK
  Authorization: Bearer <compatibility key>
  POST /v1/responses
        |
        v
OpenAI compatibility router
  resolve key -> immutable owner/profile/config/bundle/workspace policy
  validate supported OpenAI request subset
  reconstruct prior raw items when previous_response_id is present
        |
        v
TurnScheduler
  create a fresh broker/Codex thread for this response
  inject prior raw items
  start one Codex turn with experimental raw events enabled
        |
        v
Persisted stable events
  compat.response.output_item
  compat.response.usage
  message.delta
  turn.completed / turn.failed
        |
        +--> synchronous OpenAI Response object
        +--> typed Responses SSE
        +--> Chat Completions adapter
```

A fresh Codex thread per OpenAI response is intentional. It lets the façade
reconstruct `previous_response_id` history through `thread/inject_items`
without accidentally carrying the prior response's `instructions`, which the
OpenAI Responses contract does not automatically carry forward. It also keeps
one broker turn equal to one externally addressable response.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Generate pinned app-server TypeScript | `codex app-server generate-ts --out /private/tmp/codex-app-server-ts-0.144.6` | exit 0; generated protocol files exist |
| Generate pinned app-server JSON Schema | `codex app-server generate-json-schema --out /private/tmp/codex-app-server-json-schema-0.144.6` | exit 0; generated schemas exist |
| Narrow tests | `PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest tests.test_openai_compat tests.test_config_profiles tests.test_events tests.test_state tests.test_openapi` | all tests pass with no resource warnings |
| Full Python gate | `PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest discover -s tests` | all tests pass with no resource warnings |
| Export OpenAPI | `uv run python scripts/export_openapi.py` | exit 0 and `fern/openapi/openapi.json` is updated |
| Check OpenAPI drift | `git diff --exit-code -- fern/openapi/openapi.json` | exit 0 after the generated file is staged or otherwise matches the generator output |
| Check Fern docs | `pnpm docs:check` | exit 0 |
| Check whitespace | `git diff --check` | no output, exit 0 |

If the installed `codex` is not exactly `0.144.6`, stage the 0.144.6 binary in
a temporary directory or use the Docker build pin to generate the schemas. Do
not edit the Docker pin merely to match a newer local binary.

## Suggested executor toolkit

- Use the `openai-docs` skill, if available, to inspect the current official
  OpenAPI shapes for every supported request, response, error, and streaming
  event. Do not implement protocol shapes from memory.
- Use the generated app-server TypeScript as the source of truth for
  `experimentalRawEvents`, raw response items, usage notifications,
  `thread/inject_items`, and dynamic tool server requests.

## Scope

**In scope** (the only files you should modify):

- `src/codex_broker/config.py`
- `src/codex_broker/services.py`
- `src/codex_broker/http_api.py`
- `src/codex_broker/account_api.py`
- `src/codex_broker/app_server.py`
- `src/codex_broker/events.py`
- `src/codex_broker/scheduler.py`
- `src/codex_broker/scheduler_config.py`
- `src/codex_broker/state.py`
- `src/codex_broker/state_schema.py`
- `src/codex_broker/openai_auth.py` (create)
- `src/codex_broker/openai_protocol.py` (create)
- `src/codex_broker/openai_api.py` (create)
- `tests/fake_codex.py`
- `tests/test_openai_compat.py` (create)
- `tests/test_config_profiles.py`
- `tests/test_events.py`
- `tests/test_state.py`
- `tests/test_openapi.py`
- `tests/test_metrics.py`
- `pyproject.toml`
- `uv.lock`
- `README.md`
- `codex-broker-spec.md`
- `docs/architecture.md`
- `docs/configuration.md`
- `docs/integrating-with-broker.md`
- `docs/app-server-modes.md`
- `fern/docs/pages/index.mdx`
- `fern/docs/pages/quickstart.mdx`
- `fern/docs/pages/concepts.mdx`
- `fern/openapi/openapi.json`
- `plans/README.md`

**Out of scope** (do NOT touch, even though these areas are adjacent):

- Exposing the broker directly to the public internet or adding browser CORS.
- Exposing app-server JSON-RPC or experimental WebSockets.
- Changing existing `/v1/owners/...` request or response shapes.
- Replacing `ThreadingHTTPServer` or adding a runtime web framework.
- Adding a runtime dependency on the OpenAI SDK.
- Changing Codex account login/profile semantics.
- Images, audio, files, remote URLs, computer-use input, or vector stores in
  the first compatibility release.
- Reinterpreting Codex bundle/MCP tools as caller-defined OpenAI functions.
- Claiming support for explicit parameters that Codex cannot honor, including
  sampling controls, by silently ignoring them.
- Selective deletion or `store: false` semantics until retention behavior can
  faithfully honor the contract.
- Editing or reading the existing untracked `.env`.

## Git workflow

- Do not create or switch branches without operator consent. If the operator
  authorizes a feature branch and does not name it, use
  `codex/openai-compatible-api`.
- Keep commits logical: protocol/auth foundation, Responses API, Chat adapter,
  then docs/generated contract. Include a useful body explaining supported and
  rejected compatibility surfaces.
- Do not push or open a pull request unless the operator instructs it.
- Before each commit, inspect `git status --short` and preserve the existing
  untracked `.env`.

## Steps

### Step 1: Pin the compatibility matrix to Codex 0.144.6 and current OpenAI schemas

Generate the app-server TypeScript and JSON Schemas from exactly Codex 0.144.6.
Record the raw item, usage, history injection, instruction, and dynamic-tool
shapes in `docs/app-server-modes.md`. Add an "OpenAI compatibility matrix" to
`docs/integrating-with-broker.md` with one row per endpoint and parameter.

The initial supported surface is:

- `GET /v1/models`
- `GET /v1/models/{model}`
- `POST /v1/responses`, synchronous and streaming
- `GET /v1/responses/{response_id}`
- `POST /v1/responses/{response_id}/cancel`
- `GET /v1/responses/{response_id}/input_items`
- `POST /v1/chat/completions`, synchronous and streaming, implemented as an
  adapter over the Responses service

Initially accept:

- `model`
- text `input` as a string or compatible message/input-text items ending in a
  user message
- `instructions`
- `stream`
- `previous_response_id`
- `reasoning.effort`
- `reasoning.summary`
- `service_tier`
- `text.format` JSON Schema, translated to app-server `outputSchema`
- `metadata`, persisted as response metadata but never sent as Codex input
- omitted `store` or `store: true`

Explicitly reject with an OpenAI-shaped 400 error:

- `store: false`
- `tools` and `tool_choice`
- `temperature`, `top_p`, penalties, logprobs, seed, and token-bias controls
- explicit output-token caps until Codex exposes an enforceable equivalent
- unsupported input modalities or item types
- background/deferred execution if it is not implemented in this plan
- any unknown parameter whose behavior could change model output or persistence

Use the official OpenAI OpenAPI schema to capture golden JSON fixtures for:
Responses objects, model objects/lists, Chat Completion objects/chunks, typed
Responses streaming events, and all error statuses. List unsupported fields in
docs; do not omit them from the matrix.

**Verify**:
`rg -n "rawResponseItem/completed|thread/tokenUsage/updated|thread/inject_items|item/tool/call" docs/app-server-modes.md docs/integrating-with-broker.md`
→ all four protocol names are documented, and the compatibility matrix names
every endpoint and rejected parameter group above.

### Step 2: Add compatibility-key identity bindings

Create `src/codex_broker/openai_auth.py` with immutable dataclasses for a
compatibility binding and a resolver. Load bindings from
`CODEX_BROKER_OPENAI_COMPAT_BINDINGS_FILE`; do not support raw secrets in a
JSON environment variable.

Use a JSON object keyed by `sha256:<lowercase-hex-digest>`:

```json
{
  "sha256:<digest-of-client-key>": {
    "ownerId": "service-account-id",
    "authPrincipalId": "optional-trusted-principal-id",
    "profile": "default",
    "configProfile": "openai-compatible",
    "hostApp": "openai-sdk",
    "bundleId": "optional-reviewed-bundle",
    "cwd": "/workspaces/app",
    "modelAliases": {
      "client-visible-model": "codex-model-id"
    }
  }
}
```

Requirements:

- The raw compatibility key is distributed separately and is never stored in
  the binding file, SQLite, logs, audits, exceptions, or public objects.
- Hash the presented bearer token with SHA-256 and use
  `hmac.compare_digest` for matching.
- Resolve `ownerId` and optional `authPrincipalId` through the existing trusted
  `AuthManager` policy. A compatibility binding cannot bypass principal
  mappings.
- Validate profile, configuration profile, bundle, and cwd through existing
  broker validators before starting a turn.
- The binding is the only way compatibility requests select owner, principal,
  profile, configuration profile, bundle, cwd, host app, or model aliases.
  Never accept those internal selectors from an OpenAI request body or custom
  header.
- An invalid or missing compatibility key returns the exact OpenAI
  authentication error shape without revealing whether a digest exists.
- Native routes continue to use `CODEX_BROKER_INTERNAL_KEY`.

Add config parsing and validation in `src/codex_broker/config.py`, instantiate
the resolver in `src/codex_broker/services.py`, and test malformed files,
duplicate/invalid digests, missing required fields, alias validation, valid
resolution, constant public errors, and secret redaction.

Document key generation without printing a real secret. Make clear that this
key authenticates the SDK to Codex Broker; it is separate from the upstream
Codex profile's login or API key.

**Verify**:
`PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest tests.test_config_profiles tests.test_openai_compat`
→ all compatibility binding tests pass, including a test proving a raw key
does not appear in logs, exceptions, config representations, or persisted
state.

### Step 3: Persist stable raw output items and usage, independent of debug capture

Verify the exact notification method names from Step 1, then add explicit
normalization in `src/codex_broker/events.py`:

- `rawResponseItem/completed` →
  `compat.response.output_item`, payload `{item: <raw item>}`
- `thread/tokenUsage/updated` →
  `compat.response.usage`, payload containing the exact last-turn and total
  usage objects needed by the assembler

Enable `experimentalRawEvents` only for compatibility-created Codex threads.
Do not enable it globally for native broker turns. Add a private, typed
compatibility execution context to `QueuedTurn`/`BrokerTurnContext` so the
scheduler can:

1. add `experimentalRawEvents: true` to `thread/start`;
2. call `thread/inject_items` after the fresh Codex thread starts and before
   `turn/start`; and
3. store canonical compatibility metadata in `resolved_options_json`.

This context must be created only by the compatibility service; native request
bodies must not be able to set raw history injection or compatibility markers.
Do not use `debug_raw_events`: it is optional, redacted, retention-pruned
diagnostic data rather than the durable response contract.

Extend `tests/fake_codex.py` to:

- validate `experimentalRawEvents`;
- accept and record `thread/inject_items`;
- emit raw message response items;
- emit token usage before terminal completion; and
- optionally omit/malformed these notifications for failure tests.

Add owner-scoped `find_turn_by_turn_id` and an additive index in
`src/codex_broker/state.py` and `src/codex_broker/state_schema.py`. Require the
stored compatibility protocol marker before exposing any turn as an OpenAI
response.

**Verify**:
`PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest tests.test_events tests.test_state tests.test_openai_compat`
→ raw output/usage survive with debug capture disabled, native turns do not
request raw events, injected history is ordered before `turn/start`, and one
compatibility binding cannot retrieve another owner's turn.

### Step 4: Implement a Responses-first protocol core

Create `src/codex_broker/openai_protocol.py` for pure validation, mapping, id
conversion, response assembly, streaming-event assembly, and OpenAI error
types. Keep HTTP and scheduler access out of this module so golden fixtures can
test it directly.

Create `src/codex_broker/openai_api.py` as the orchestration layer. It should:

1. resolve the compatibility binding;
2. parse only the supported request subset;
3. resolve a model alias, then verify the target against app-server
   `model/list`;
4. convert instructions/current user input into app-server inputs;
5. when `previous_response_id` is supplied, owner-scope every ancestor lookup,
   detect cycles and a bounded maximum chain length, and reconstruct canonical
   input/output items in chronological order;
6. create a fresh broker thread and one compatibility turn;
7. inject prior items before the turn;
8. wait on the state condition for synchronous requests without busy polling;
9. assemble the final OpenAI Response from durable raw output/usage events; and
10. translate terminal failures into OpenAI-shaped errors without leaking
    internal messages or raw app-server payloads.

Persist enough canonical metadata in the turn's `resolved_options_json` to
rebuild after restart:

- protocol/version marker;
- external object kind (`responses` or `chat.completions`);
- previous response id, if any;
- original canonical OpenAI input items;
- public metadata;
- requested and resolved model ids;
- supported reasoning/service-tier/format options;
- binding policy identifiers that are not secrets.

Do not persist the presented compatibility key or its digest in each turn.

Response construction rules:

- derive `id` reversibly from the broker turn id;
- derive `created_at` from the persisted turn timestamp;
- return the client-visible requested model while retaining the resolved Codex
  model only in private broker metadata/audit data;
- include exact `status`, `output`, `output_text`, `usage`, `metadata`,
  `previous_response_id`, and error/incomplete detail shapes required by the
  current official schema;
- include only output item types covered by the compatibility matrix;
- fail the response if required raw output or last-turn usage is missing or
  malformed instead of fabricating values;
- never expose normalized internal tool commands, file changes, approvals, or
  broker identifiers as OpenAI output.

For synchronous requests, closing the HTTP connection does not implicitly
cancel the turn. Cancellation occurs only through the cancel endpoint.

**Verify**:
`PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest tests.test_openai_compat`
→ tests cover a simple string input, message-item input, instructions,
structured output, reasoning options, service tier, model alias, metadata,
previous-response history reconstruction, cross-owner rejection, missing raw
output, malformed usage, terminal failure, and unsupported parameters.

### Step 5: Route Responses, models, retrieval, input-items, and cancellation

In `src/codex_broker/http_api.py`, recognize only the compatibility route set
before native broker-key authorization. Dispatch it to
`src/codex_broker/openai_api.py`; every other route retains current auth and
errors.

Implement:

- `GET /v1/models` using the binding's account/profile-scoped `model/list`;
- `GET /v1/models/{model}` with alias resolution and OpenAI 404 errors;
- `POST /v1/responses`;
- `GET /v1/responses/{response_id}`;
- `GET /v1/responses/{response_id}/input_items`; and
- `POST /v1/responses/{response_id}/cancel`, mapped to the existing scheduler
  interrupt path when the response is still active.

Update `metric_path_template` so response/model ids are templated and never
become unbounded metric labels. Add compatibility request counters and
latencies using the existing scheduler metric pattern. Log only owner hashes,
protocol endpoint templates, response ids, status, and durations; never log
bearer keys or raw prompts.

Retrieval/cancellation requirements:

- parse `resp_<token>` and perform an owner-scoped turn lookup;
- require `openaiCompat.protocol == "responses"` in resolved metadata;
- return the same response object after restart;
- distinguish missing, already terminal, and currently running responses using
  official status/error shapes;
- enforce pagination/limits on input-items if the official endpoint requires
  them; and
- never expose a native broker turn by constructing a response id for it.

Add these paths and exact schemas to the inline OpenAPI 3.1 document. Use a
separate compatibility bearer security scheme where necessary and preserve
the native security schemes.

**Verify**:
`PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest tests.test_openai_compat tests.test_metrics tests.test_openapi`
→ every route, auth boundary, id check, owner boundary, retrieval-after-reopen,
cancel status, metric template, and OpenAPI schema test passes.

### Step 6: Emit exact typed Responses streaming events

For `stream: true`, emit `Content-Type: text/event-stream` and the exact event
sequence and JSON shapes from the official Responses schema. At minimum, the
text-only flow must cover:

- `response.created`
- `response.in_progress`
- `response.output_item.added`
- `response.content_part.added`
- one or more `response.output_text.delta`
- corresponding text/content/output-item done events
- `response.completed`, or the official failure/incomplete terminal event

Use persisted events as the cursor source. Convert `message.delta` to output
text deltas and the stable raw completed message item to the authoritative
done/output object. Maintain monotonic `sequence_number` values. If an expected
raw item never arrives, emit the official failure event and close the stream.

Do not emit native broker SSE envelopes, raw app-server method names, debug
payloads, heartbeats as typed OpenAI events, or a Chat-style `[DONE]` marker
unless the official Responses contract requires it. Plain SSE comment
heartbeats are acceptable if the official SDK ignores them; verify this in the
black-box SDK test.

Always terminate the HTTP chunked body after a terminal event. Handle a broken
client connection without leaking a request thread; the Codex turn may
continue and remain retrievable.

**Verify**:
`PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest tests.test_openai_compat`
→ byte-level golden tests confirm ordering, sequence numbers, terminal closure,
multiple deltas, failure, client disconnect cleanup, and no leakage of broker
or app-server-only fields.

### Step 7: Add Chat Completions as a strict adapter over Responses

Implement `POST /v1/chat/completions` by translating Chat input into the same
canonical request and response assembler. Do not create a second scheduler or
event implementation.

For each Chat request:

- create a fresh broker/Codex thread;
- map `system` and `developer` messages to the verified base/developer
  instruction fields;
- inject prior user/assistant transcript items with `thread/inject_items`;
- submit the final user message as the current turn input;
- reject transcripts that cannot be mapped faithfully;
- translate `response.output_text` into one assistant choice;
- translate `text.format`/Chat structured-output fields according to the
  current official migration guide; and
- return `chatcmpl_<turn-token>` and exact Chat usage/finish-reason shapes.

Streaming must emit exact `chat.completion.chunk` objects and finish with
`data: [DONE]`. Cover role-only first chunks, multiple content chunks, finish
reason, usage options if supported, terminal errors before/after headers, and
client disconnect.

Function/tool messages remain unsupported until Step 8. Reject them with an
OpenAI-shaped error that names the offending parameter; do not drop them from
the transcript.

**Verify**:
`PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest tests.test_openai_compat`
→ synchronous and streaming Chat tests pass for system/developer instructions,
multi-message text history, structured output, model aliases, errors, usage,
finish reason, and `[DONE]`.

### Step 8: Gate caller-defined function tools behind a separate semantic spike

Do not pass `tools` into app-server `dynamicTools` as a simple field mapping.
OpenAI Responses returns a `function_call` to the client and accepts a
`function_call_output` in a later response. App-server instead sends the broker
an `item/tool/call` server request and waits synchronously for the result before
the same Codex turn continues. The current broker also rejects that server
request.

Before implementing tool compatibility, produce and test a design that answers:

- How does a logical OpenAI response finish while its app-server turn is
  waiting for `item/tool/call`?
- How is the pending server request persisted and recovered if the broker or
  child restarts?
- How does a later `function_call_output` resolve exactly one pending call,
  under the same owner and response chain?
- How are timeout, cancellation, duplicate output, parallel tool calls, and
  same-thread locking handled?
- How are one app-server turn's possible multiple logical OpenAI response ids
  represented without breaking retrieval and usage accounting?

Acceptable outcomes are:

1. a durable implementation with regression tests for every state transition;
2. an upstream app-server capability that exposes a natural response boundary;
   or
3. continued explicit rejection in the compatibility matrix.

Instruction-prompted JSON "tool calls", placeholder tool results, cancel-and-
resume tricks, and keeping an unpersisted waiter alive between HTTP requests
are not acceptable compatibility implementations.

Only after a durable design lands should the API description move from
"OpenAI-compatible text and structured-output subset" toward broader drop-in
claims.

**Verify**:
`rg -n "tools|tool_choice|function_call_output|item/tool/call" docs/integrating-with-broker.md tests/test_openai_compat.py`
→ the matrix and tests either explicitly reject the tool surface or document
and exercise a durable implementation; there is no silent-ignore path.

### Step 9: Verify through the official OpenAI SDK

After a quick dependency health check, add the current official `openai` Python
package to `[dependency-groups].dev` only. Add black-box tests that start the
fake broker and construct:

```python
OpenAI(
    base_url=f"{broker_url}/v1",
    api_key=compatibility_key,
)
```

Exercise:

- `client.models.list()` and model retrieval;
- `client.responses.create(...)` sync and streaming;
- `client.responses.retrieve(...)`;
- `client.responses.cancel(...)` while active;
- previous-response continuation;
- structured output;
- `client.chat.completions.create(...)` sync and streaming; and
- representative authentication, invalid-request, not-found, and conflict
  errors through SDK exception types.

These are dev-only integration tests. Production code must not import
`openai`.

If caller-defined tools are implemented in Step 8, also run the official
gpt-oss API compatibility suite in both Responses and Chat modes with
streaming. Treat zero invalid requests and greater than 90% pass metrics as a
smoke threshold, not proof of complete compatibility. If tools remain
unsupported, do not use a tool-calling score to market the text-only subset.

**Verify**:
`PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest tests.test_openai_compat`
→ all SDK black-box tests pass and
`rg -n "^from openai|^import openai" src/codex_broker` returns no matches.

### Step 10: Update public contracts and operational documentation

Update the README, spec, architecture, configuration, integration guide,
app-server matrix, and Fern pages in the Scope list. The documentation must:

- describe the façade as internal and server-to-server;
- show the `base_url` plus compatibility-key change for official Python and
  JavaScript OpenAI SDKs;
- distinguish the compatibility key from the upstream Codex credential;
- document the binding-file schema and key-digest provisioning;
- list every supported endpoint/field and every deliberate rejection;
- explain fresh-thread history reconstruction and retention implications;
- state that `store: false` is rejected;
- distinguish caller-defined OpenAI functions from reviewed Codex bundles,
  MCP tools, and broker-hosted tools;
- avoid "full OpenAI replacement" or universal "drop-in" language while tools
  and modalities remain unsupported; and
- include troubleshooting for invalid keys, unavailable models, missing
  profile auth, and unsupported parameters.

Extend `openapi_document()` with precise OpenAI compatibility schemas and
examples. Regenerate `fern/openapi/openapi.json`. Do not manually edit the
generated JSON after export.

**Verify**:

1. `uv run python scripts/export_openapi.py` → exit 0.
2. `pnpm docs:check` → exit 0.
3. `git diff --exit-code -- fern/openapi/openapi.json` → exit 0 after staging
   the generated result.
4. `rg -n "OpenAI-compatible" README.md codex-broker-spec.md docs fern/docs`
   → compatibility is described in every reader-facing entry point without
   overclaiming unsupported tools/modalities.

### Step 11: Run the complete release gate and inspect scope

Run the full Python suite under the CI ResourceWarning gate, regenerate the
OpenAPI artifact one final time, validate docs, and inspect the full diff.

**Verify**:

1. `PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest discover -s tests`
   → all tests pass.
2. `uv run python scripts/export_openapi.py` → exit 0.
3. `git diff --exit-code -- fern/openapi/openapi.json` → exit 0 after the
   generated file is staged or matches the intended diff.
4. `pnpm docs:check` → exit 0.
5. `git diff --check` → no output.
6. `git status --short` → only files in Scope plus the pre-existing untracked
   `.env`; no generated temporary schema directories are in the repository.

## Test plan

Create `tests/test_openai_compat.py` and model its server lifecycle and HTTP
helpers after `tests/test_broker.py`. Extend the existing focused suites for
the source module they already cover.

Required cases:

- Compatibility auth: missing/invalid/valid key, digest-file validation,
  principal-policy enforcement, no secret logging/persistence, native-key
  separation.
- Model catalog: actual ids, aliases, hidden/unavailable models, retrieval,
  owner/profile isolation.
- Request validation: every supported field and every rejected parameter group
  from Step 1, with exact OpenAI error bodies/statuses.
- Responses sync: simple text, multiple deltas, structured output,
  instructions, reasoning/service tier, metadata, usage, terminal failures.
- Response ids: reversible parsing, malformed prefixes/tokens, native-turn
  rejection, cross-owner rejection.
- History: `previous_response_id`, multiple ancestors, order, cycle/depth
  protection, no prior-instructions carry, missing/deleted ancestor.
- Retrieval/input items: in-progress and completed objects, restart
  reconstruction, pagination if applicable.
- Cancellation: queued/starting/running/completed/missing, repeated cancel.
- Responses streaming: exact event order, sequence numbers, completion/failure,
  terminal chunk closure, broken client cleanup.
- Chat sync/stream: system/developer mapping, transcript injection, output,
  usage, finish reason, `[DONE]`, tool-message rejection.
- Raw event contract: debug off, raw message item, usage, malformed/missing
  payloads, native turn unaffected.
- Scheduler ordering: thread start → history injection → turn start.
- Metrics: bounded route templates and expected counters.
- OpenAPI: all routes, compatibility security scheme, request/response/error
  schemas, stream content types.
- Official SDK: model list, Responses sync/stream/retrieve/cancel/continuation,
  Chat sync/stream, and SDK exception mapping.

Do not make real OpenAI or Codex network calls in the default test suite.

## Done criteria

- [x] The Codex 0.144.6 generated schemas confirm every app-server primitive
  used by the implementation.
- [x] An official OpenAI SDK works with only `base_url` and `api_key` changes
  for the documented text and structured-output surface.
- [x] Responses sync, typed streaming, retrieval, input-items, cancellation,
  previous-response continuation, models, and Chat sync/stream pass black-box
  tests.
- [x] Every unsupported behavior returns a documented OpenAI-shaped error;
  no behavior-changing field is silently ignored.
- [x] Compatibility keys map server-side to immutable owner/principal/profile/
  config/bundle/workspace policy and never appear in logs or state.
- [x] Native `/v1/owners/...` routes, auth, response shapes, and tests are
  unchanged.
- [x] Durable output items and usage do not depend on debug raw-event capture.
- [x] Completed compatible responses can be reconstructed after reopening the
  state store.
- [x] The standard-library-only runtime is preserved; `openai` is dev-only.
- [x] `PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest discover -s tests`
  exits 0.
- [ ] `pnpm docs:check` exits 0. Not run: the managed environment rejected the
  Fern command because it may send repository documentation to an external
  service. Local OpenAPI JSON parsing and source-document checks passed.
- [x] `uv run python scripts/export_openapi.py` leaves
  `fern/openapi/openapi.json` current.
- [x] `git diff --check` exits 0.
- [x] No files outside the in-scope list are modified, apart from the
  pre-existing untracked `.env`.
- [x] `plans/README.md` status row is updated.

## STOP conditions

Stop and report back; do not improvise if:

- The production-pinned Codex 0.144.6 schema does not contain
  `experimentalRawEvents`, `rawResponseItem/completed`,
  `thread/tokenUsage/updated`, or `thread/inject_items` in the expected
  request/notification direction.
- Raw response item ids cannot be correlated with text deltas well enough to
  emit the official streaming event order without fabrication.
- App-server history injection changes or rejects raw Responses items required
  for a two-response continuation fixture.
- A compatibility request can select or access an owner, auth principal,
  profile, thread, or turn outside its server-side binding.
- The implementation would need to store or log a raw compatibility key.
- A required response object or stream event would need invented token usage,
  output items, or status values.
- Implementing non-additive state changes requires a schema bump without a
  tested v3-to-v4 migration.
- Function-tool support would require an unpersisted waiter, placeholder tool
  result, or more than one logical OpenAI response per broker turn without a
  durable mapping design.
- Official SDK tests require production code to import the OpenAI SDK.
- Existing native broker API tests or response fixtures change for reasons
  other than additive OpenAPI paths/metrics.
- A verification command fails twice after a reasonable targeted fix.
- The change requires a file outside Scope.

## Maintenance notes

- The raw item and dynamic-tool surfaces are experimental app-server APIs.
  Every Codex version bump must regenerate schemas and rerun raw-item, usage,
  injection, and streaming fixtures before changing the Docker pin.
- OpenAI schemas evolve additively. Parsers must tolerate new response fields
  and streaming event types from upstream clients only where ignoring them is
  semantically safe; request fields that affect behavior remain reject-by-
  default until mapped and tested.
- Reviewers should scrutinize owner scoping, secret handling, previous-response
  reconstruction order, stream termination, and missing-event failure paths.
- Model aliases are deployment policy, not claims that two models are
  behaviorally identical.
- `store: false`, deletion, modalities, background mode, and caller-defined
  function tools are explicit follow-ups. Keep the compatibility matrix
  honest until each has its own storage/lifecycle tests.
