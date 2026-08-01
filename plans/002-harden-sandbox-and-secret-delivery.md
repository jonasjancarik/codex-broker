# Plan 002: Protect broker credentials and deliver secret-safe runtime events

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report—do not improvise. When done, update the status row for this plan in
> `plans/README.md` unless a reviewer dispatched you and told you they maintain
> the index.
>
> **Drift check (run first)**:
> `git diff --stat aaca3b9..HEAD -- Dockerfile examples/docker-compose.yml .github/workflows/docker-publish.yml README.md src/codex_broker tests fern/docs/pages/operations/configuration-reference.mdx fern/docs/pages/operations/deployment.mdx fern/docs/pages/runtime/architecture.mdx plans/README.md`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P0
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: none
- **Category**: security
- **Planned at**: commit `aaca3b9`, 2026-08-01
- **Completed**: 2026-08-01

## Why this matters

Codex app-server must read its selected profile's `auth.json` to authenticate,
but commands launched for the model do not need that file. Today the app-server
and its sandboxed descendants share the `broker` Unix identity. Mode bits alone
therefore do not keep a broken or disabled sandbox from reading broker-owned
credentials, state, or Docker secrets.

The other half of the boundary is event delivery. Normalized assistant, tool,
interaction, and error payloads are currently stored and streamed without the
recursive redaction already applied to logs and optional raw debug fields. A
tool that prints a credential—or an assistant that repeats one—can therefore
put it into SQLite, native SSE, audit output, and the OpenAI-compatible API.

After this plan lands, supported sandboxed modes will be verified before a
model turn starts, broker credentials will be hidden from sandboxed commands,
and model/runtime-derived events will use a default-on secret sanitizer.
Operators will retain an explicit deployment-level `raw` event mode for trusted
use cases. Ordinary callers will be confined to broker-managed permission
profiles; `danger-full-access` requires a separately configured credential and
is never enabled merely because a request asks for it.

> **Security clarification incorporated 2026-08-01**: Workspace isolation is
> a default-deny read boundary, not only a write boundary. Broker profiles deny
> `:root`, restore only `:minimal`, grant the canonical authorized runtime
> workspace roots, and retain exact hard denies for broker auth, state, and
> secret paths. Auto-review evaluates eligible explicit permission requests; it
> does not weaken the profile or permit unsandboxed shell escalation.

## Security contract to preserve

### Process boundary

```text
broker process
  |
  +-- Codex app-server parent
  |     CODEX_HOME -> selected profile (may read its own auth.json)
  |     HOME       -> non-secret runtime home
  |
  +-- sandboxed shell/tool child
        may read/write only the selected permission profile
        cannot read broker auth, state, or mounted Docker secrets
```

The app-server parent still runs as the `broker` user and may read its own
credentials. The protection is a Codex/Bubblewrap filesystem policy applied to
the shell-like child, not a claim that Unix ownership separates parent and
child.

### Authorized execution modes

| Caller selection | App-server selection | Outside-workspace reads? | Notes |
|---|---|---|---|
| `read-only` | broker-managed default-deny read profile | denied unless explicitly granted; workspace remains readable | Sandbox preflight required. |
| `workspace-write` | broker-managed default-deny workspace profile | denied unless explicitly granted; workspace is writable | Canonical runtime roots supplied by the broker. |
| `danger-full-access` | legacy full-access sandbox selection | unrestricted | Requires a separate danger-full-access credential; event sanitization still follows deployment mode. |

The broker is the policy ceiling. Callers cannot supply permission profile ids,
runtime workspace roots, or low-level sandbox policies directly. The broker
maps the two ordinary public sandbox values to its managed profiles after
canonicalizing the effective cwd and proving it is beneath the configured
workspace allowlist. A request for `danger-full-access` is rejected unless the
deployment configured a separate secret and the caller supplied it through the
dedicated authorization header.

For the managed profiles, the broker defaults to Auto-review plus a granular
approval policy that permits `request_permissions` review but disables
unsandboxed shell escalation. Exact denies for control-plane paths win over
requested grants, so auth, state, and secret paths are non-auto-approvable.
Routine work inside the selected profile does not prompt.

### Event-delivery modes

Add `CODEX_BROKER_EVENT_SANITIZATION_MODE` with exactly two values:

| Mode | Default? | Stored normalized events | Native/OpenAI output | Logs and raw debug fields |
|---|---|---|---|---|
| `safe` | yes | secret-sanitized | secret-sanitized | secret-sanitized |
| `raw` | no | unchanged normalized payloads | unchanged normalized payloads | still secret-sanitized |

`raw` is an operator/deployment setting. Do not add a request, turn, bundle, or
configuration-profile override. This keeps an ordinary API caller or forwarded
user value from disabling the safeguard. A separate dual raw/sanitized storage
system is out of scope.

The sanitizer is for credentials and configured sensitive values, not general
content moderation. It must leave ordinary prose, code, tool output, and
reasoning summaries unchanged.

## Current state

### Authentication and process identity

- `src/codex_broker/auth.py:157-168` creates the selected Codex home inside the
  broker auth root and limits directories to mode `0700`:

  ```python
  profiles_root = (self.config.auth_root / auth_principal_hash / "profiles").resolve()
  home = (profiles_root / profile_key / "codex-home").resolve()
  ...
  for private_dir in (self.config.auth_root, profiles_root.parent, profiles_root, home.parent, home):
      private_dir.chmod(0o700)
  ```

- `src/codex_broker/app_server.py:147-166` starts app-server under that same
  service identity and points both `CODEX_HOME` and `HOME` into the auth tree:

  ```python
  env = env_with(
      clean_process_env(config.codex_passthrough_env),
      {
          "CODEX_HOME": str(codex_home),
          "CODEX_CREDENTIAL_STORE": config.credential_store,
          "HOME": str(codex_home.parent),
      },
  )
  self._process = subprocess.Popen(command, cwd=str(codex_home), env=env, ...)
  ```

- `Dockerfile:38-47` creates one `broker` user, gives it `/data`, and runs the
  service as that user. This is correct for the app-server parent, but `0700`
  cannot distinguish descendants with the same uid.

- `src/codex_broker/auth.py:661-665` already treats each profile's
  `config.toml` as broker-managed and overwrites it deterministically. Extending
  this generated file with named permission profiles does not overwrite
  caller-owned configuration because caller-owned config is not preserved
  today.

### Sandbox selection and readiness

- `src/codex_broker/scheduler_config.py:44-62` passes a legacy `sandbox`
  string to `thread/start`/`thread/resume`; request options intentionally win
  over bundle and profile defaults.

- `src/codex_broker/scheduler_config.py:142-172` maps process configuration but
  does not map `approvals_reviewer`.

- `src/codex_broker/http_api.py:310-333` checks SQLite, keys, binary presence,
  root readability, and auth-root writability. It never launches the sandbox,
  so a Bubblewrap namespace failure can pass readiness and fail only after a
  turn begins.

- `src/codex_broker/runtime_errors.py:50-64` classifies authentication and
  missing-rollout failures. Other errors—including Bubblewrap namespace
  failures—become a generic `codex_runtime_error` with the raw message used as
  the public message.

### Event persistence and delivery

- `src/codex_broker/util.py:15-68` has recursive key/pattern redaction, and
  `json_log` applies it to structured logs.

- `src/codex_broker/scheduler.py:136-159` redacts optional `raw_params` but
  passes the normalized `payload` unchanged:

  ```python
  raw_params = redact_json(params) if self.debug_raw_events else None
  self.state.append_event(..., event_type, payload, ..., raw_params=raw_params)
  ```

- `src/codex_broker/state.py:362-402` serializes the payload directly to
  `events.payload_json`. `src/codex_broker/state_transactions.py:18-87` does the
  same for terminal events and optional audit payloads.

- `src/codex_broker/events.py:50-54` returns every non-usage public event
  payload unchanged. `src/codex_broker/http_api.py:276-299` then writes those
  stored values to native SSE.

- `src/codex_broker/openai_api.py:250-321`, `324-380`, and `480-535` rebuild
  synchronous and streaming OpenAI output from the same persisted events.

- `src/codex_broker/app_server.py:534-615` persists pending interaction
  requests and responses without applying the normalized-event redactor.
  `src/codex_broker/state.py:720-781` similarly stores audit payloads unchanged.

- `tests/test_broker.py:421-452` verifies only that debug `raw_params` are
  redacted. The corresponding normalized event payload is not asserted and
  currently retains the synthetic credential material.

### Version-pinned Codex capabilities

The production Dockerfile pins Codex `0.144.6`. Verify behavior against that
binary rather than the executor's globally installed Codex version.

For `0.144.6`, the authoritative app-server documentation says:

- `thread/start`, `thread/resume`, and `turn/start` support
  `approvalsReviewer`;
- named profile selection uses `permissions` and optional
  `runtimeWorkspaceRoots`;
- `permissions` must not be sent together with the legacy `sandbox` or
  `sandboxPolicy` field;
- `command/exec` supports a named `permissionProfile` without starting a model
  turn. Its pinned request shape has no `runtimeWorkspaceRoots` field, so the
  preflight supplies the temporary authorized workspace as `cwd`; the
  permission profile resolves that cwd as its runtime workspace root.

References:

- <https://github.com/openai/codex/blob/rust-v0.144.6/codex-rs/app-server/README.md>
- <https://raw.githubusercontent.com/openai/codex/rust-v0.144.6/codex-rs/core/config.schema.json>
- <https://raw.githubusercontent.com/openai/codex/rust-v0.144.6/codex-rs/linux-sandbox/README.md>

The Linux sandbox notes also state that Bubblewrap is the default, requires
user and PID namespaces, supports denied filesystem carve-outs, and prefers a
system `bwrap` when present. Treat all of those as claims to verify with a real
canary in the built broker image; configuration parsing alone is not proof of
enforcement.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Confirm planned revision | `git rev-parse --short HEAD` | `aaca3b9`, or drift reviewed before proceeding |
| Generate pinned app-server schema | `docker run --rm --entrypoint codex codex-broker:security-plan app-server generate-json-schema --experimental --out /tmp/app-server-schema` | exit 0 using Codex `0.144.6` |
| Narrow sanitizer tests | `PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest discover -s tests -p 'test_security.py' -v` | all tests pass |
| Narrow sandbox tests | `PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest discover -s tests -p 'test_sandbox_probe.py' -v` | all tests pass |
| Configuration tests | `PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest discover -s tests -p 'test_config_profiles.py' -v` | all tests pass |
| Event tests | `PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest discover -s tests -p 'test_events.py' -v` | all tests pass |
| OpenAI compatibility tests | `PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest discover -s tests -p 'test_openai_compat.py' -v` | all tests pass |
| Full Python gate | `PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest discover -s tests` | all tests pass with no resource warnings |
| Export OpenAPI | `uv run python scripts/export_openapi.py` | exit 0; generated contract is current |
| Check Fern docs | `pnpm docs:check` | exit 0 |
| Build production image | `docker build --build-arg CODEX_VERSION=0.144.6 -t codex-broker:security-plan .` | exit 0; image reports Codex `0.144.6` |
| Check formatting damage | `git diff --check` | no output, exit 0 |

The schema-generation command writes only inside an ephemeral container. If
the executor needs to inspect generated files locally, bind-mount a newly
created directory under `/private/tmp`; do not add generated Codex protocol
files to this repository.

## Scope

**In scope** (the only source, test, deployment, and docs paths to modify):

- `src/codex_broker/security.py` (create)
- `src/codex_broker/sandbox_probe.py` (create)
- `src/codex_broker/account_api.py`
- `src/codex_broker/config.py`
- `src/codex_broker/services.py`
- `src/codex_broker/auth.py`
- `src/codex_broker/app_server.py`
- `src/codex_broker/scheduler_config.py`
- `src/codex_broker/scheduler.py`
- `src/codex_broker/state.py`
- `src/codex_broker/state_transactions.py`
- `src/codex_broker/events.py`
- `src/codex_broker/http_api.py`
- `src/codex_broker/openai_api.py`
- `src/codex_broker/runtime_errors.py`
- `src/codex_broker/util.py`
- `tests/fake_codex.py`
- `tests/test_security.py` (create)
- `tests/test_sandbox_probe.py` (create)
- directly affected existing tests under `tests/`
- `Dockerfile`
- `examples/docker-compose.yml`
- `examples/seccomp/codex-broker.json` (create)
- `.github/workflows/docker-publish.yml`
- `README.md`
- `fern/docs/pages/operations/configuration-reference.mdx`
- `fern/docs/pages/operations/deployment.mdx`
- `fern/docs/pages/runtime/architecture.mdx`
- generated `fern/openapi/openapi.json` only if the exporter changes it
- `plans/002-harden-sandbox-and-secret-delivery.md`
- `plans/README.md`

**Out of scope** (do not implement as part of this plan):

- accepting arbitrary permission profile ids, runtime workspace roots, or
  low-level sandbox policies from API callers;
- accepting event-sanitization mode from an API request, bundle, or config
  profile;
- per-principal containers, separate Unix identities, or an authentication
  sidecar;
- an encrypted or dual raw/sanitized event store;
- rewriting historical SQLite rows that predate this feature;
- deleting threads or adding incident-purge endpoints;
- changing bundle skill injection—the broker already sends native skill input;
- enabling `seccomp=unconfined`, privileged containers, or broad capabilities
  in the example deployment;
- changing the public response shape except for additive readiness diagnostics
  and the documented `sandbox_unavailable` error code;
- adding runtime dependencies. Preserve the standard-library-only runtime.

## Git workflow

- Do not change branches without operator consent. If the operator requests a
  new branch, use `codex/harden-sandbox-secret-delivery`.
- Make logical commits if requested: protocol/config, sandbox enforcement,
  event sanitization, then docs/deployment verification.
- Match the repository's imperative commit subjects, for example
  `Negotiate supported MCP protocol versions`.
- Do not push or open a pull request unless explicitly instructed.

## Steps

### Step 1: Freeze the pinned Codex security contract with characterization tests

1. Build the image from the unmodified `Dockerfile` with
   `CODEX_VERSION=0.144.6`; do not use the host's globally installed Codex.
2. Generate the `0.144.6` app-server JSON Schema with `--experimental` into a temporary directory and
   confirm the exact wire fields for:
   - thread start/resume `permissions`;
   - turn start `permissions`, `runtimeWorkspaceRoots`, and
     `approvalsReviewer`;
   - `command/exec.permissionProfile` and `cwd` (not
     `runtimeWorkspaceRoots`);
   - the returned `activePermissionProfile` and `instructionSources` fields.
3. Add protocol characterization tests before changing the mapper. Extend
   `tests/test_config_profiles.py` using
   `test_config_profile_defaults_and_request_overrides_feed_app_server_params`
   as the structural pattern. The tests must assert:
   - `read-only` and `workspace-write` resolve to named broker profiles via
     `permissions`;
   - the request never contains both `permissions` and `sandbox`;
   - `danger-full-access` is rejected without separate authorization and
     retains the legacy field only after that authorization succeeds;
   - runtime roots are absolute and include the effective `cwd` exactly once;
   - managed profiles default to `auto_review`; explicit `user` and
     `auto_review` remain valid, and unknown values are rejected;
   - explicit `approvalPolicy: never` plus `auto_review` is rejected with a
     clear configuration error rather than silently doing nothing.
4. Extend `tests/fake_codex.py` so tests can assert the parameters supplied to
   `thread/start`, `thread/resume`, and `turn/start`. Do not teach the fake a
   different schema than the pinned generated protocol.

**Verify**:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest discover -s tests -p 'test_config_profiles.py' -v
```

Expected before implementation: the new characterization assertions fail for
named permissions and Auto-review. Expected after the relevant later steps:
all tests pass.

### Step 2: Add explicit broker security configuration and pure sanitization primitives

1. Extend `BrokerConfig` in `src/codex_broker/config.py` with:
   - `event_sanitization_mode`, loaded from
     `CODEX_BROKER_EVENT_SANITIZATION_MODE`, default `safe`, allowed values
     `safe` and `raw` only;
   - `sandbox_preflight_mode`, loaded from
     `CODEX_BROKER_SANDBOX_PREFLIGHT`, default `required` on Linux and `warn`
     elsewhere, allowed values `required`, `warn`, and `disabled`;
   - a derived non-secret runtime-home root below
     `<data_dir>/workspaces/runtime-homes`;
   - optional extra denied paths from
     `CODEX_BROKER_SANDBOX_DENY_PATHS`, resolved as absolute paths. These are
     additive to, never replacements for, broker-owned protected paths.
   - an optional danger-full-access credential loaded only from
     `CODEX_BROKER_DANGER_FULL_ACCESS_KEY_FILE`. Without it, full access is
     disabled. Never pass or expose this value to app-server children.
2. Fail startup on invalid values and non-absolute configured denied paths.
   `raw` must produce one conspicuous structured startup warning without
   logging any secret value.
3. Create `src/codex_broker/security.py` containing pure, unit-testable
   components:
   - `SecretSanitizer`, configured once as `safe` or `raw`;
   - recursive structured-value sanitization using the existing sensitive-key
     and token patterns;
   - exact-value replacement for registered sensitive values;
   - registration scopes so auth-profile and turn-specific values can be
     refreshed or removed without leaving stale copies indefinitely;
   - `StreamingSecretSanitizer`, keyed by turn, item, and stream type, that
     holds enough incomplete suffix state to detect a credential split across
     app-server delta notifications.
4. Reuse or move the existing patterns from `src/codex_broker/util.py`; do not
   maintain two divergent pattern sets. Expand sensitive key recognition only
   for credential-shaped fields supported by tests, including access, refresh,
   identity, bearer, cookie, password, and API-key fields.
5. Keep `json_log` and optional debug `raw_params` on mandatory redaction in
   both event modes. `raw` affects normalized persisted/client payloads only.
6. Do not use entropy-only detection. It creates unacceptable false positives
   in source code and ordinary generated text. Exact registered values plus
   credential-shaped patterns are the contract.

Create `tests/test_security.py` and cover:

- recursive fields and labeled strings;
- an exact registered value appearing without a label;
- overlapping registered values, longest first;
- safe values and ordinary code remaining byte-for-byte unchanged;
- `raw` returning normalized payloads unchanged;
- log redaction remaining enabled in `raw` mode;
- every split point of synthetic Bearer, API-key, access-token, and registered
  canaries across two or more streaming deltas;
- independent item streams not sharing suffix state;
- flush on item completion, turn completion, interruption, and failure;
- concurrent registration/sanitization safety.

Use synthetic canaries only. Never read or print a developer's actual
`auth.json`, environment file, keychain, or process secrets in tests.

**Verify**:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest discover -s tests -p 'test_security.py' -v
```

Expected: all pure sanitizer tests pass without network or subprocess access.

### Step 3: Give app-server a non-secret runtime home and generate protected permission profiles

1. In `src/codex_broker/auth.py`, add a deterministic runtime-home resolver
   scoped by authentication principal and profile under
   `config.runtime_home_root`. Create it with mode `0700`. It must never contain
   `auth.json`, broker state, or secret mounts.
2. Keep `CODEX_HOME` pointed at the selected Codex auth home so the app-server
   parent can authenticate. Change only the app-server process's `HOME` in
   `src/codex_broker/app_server.py` to the non-secret runtime home. Pass that
   path explicitly through `AppServerPool.get`/`AppServerClient` and include it
   in the pool key if it can vary.
3. Expand `AuthManager._ensure_config` to render deterministic TOML containing:
   - the existing `cli_auth_credentials_store` setting;
   - one broker read-only permission profile extending `:read-only`;
   - one broker workspace profile extending `:workspace`;
   - `:root = "deny"` and `:minimal = "read"` in both profiles;
   - `:workspace_roots = "read"` for read-only and `"write"` for workspace;
   - absolute deny entries for `config.auth_root`, the state directory,
     `/run/secrets` when present, and configured extra denied paths;
   - workspace-relative deny globs for `.env` variants and private-key files,
     applied beneath `:workspace_roots`;
   - a bounded `glob_scan_max_depth` high enough to cover normal repositories.
4. Use correct TOML quoting for arbitrary absolute paths. Never construct
   config lines by unescaped interpolation. Keep the file mode `0600`.
5. Set `default_permissions = "broker-read-only"`. Pinned Codex 0.144.6 rejects
   any custom `[permissions]` table without a default. Sandboxed thread calls
   still select an explicit managed profile so the effective per-turn policy is
   visible, while auth-management and standalone commands fail toward the
   read-only boundary rather than an implicit broader default.
6. In `src/codex_broker/scheduler_config.py`, resolve the caller's existing
   sandbox choice once:
   - map `read-only` to the broker read-only profile;
   - map `workspace-write` to the broker workspace profile and supply the
     effective absolute cwd as `runtimeWorkspaceRoots`;
   - preserve `danger-full-access` as its legacy sandbox selection only after
     the HTTP layer verifies the separate danger-full-access credential;
   - reject unknown values;
   - never send `permissions` together with `sandbox`.
7. Preserve request, then bundle, then configuration-profile precedence only
   for the two managed public modes. Reject caller-supplied `permissions`,
   `runtimeWorkspaceRoots`, and `sandboxPolicy`. Canonicalize the effective cwd,
   require it beneath the authorized workspace allowlist, and supply it exactly
   once as the runtime workspace root.
8. Unless explicitly overridden with a compatible safer setting, managed
   profiles use `approvalsReviewer: auto_review` and a granular approval policy
   that enables reviewed `request_permissions` grants while disabling
   unsandboxed shell escalation. Routine in-profile work must not prompt.
9. Register exact credential values for safe output sanitization without
   logging them:
   - add an `AuthManager` helper that reads only the selected profile's
     `auth.json`, recursively extracts string leaves beneath credential-shaped
     keys, and replaces that sanitizer registration scope before each turn;
   - register broker keys and configured passthrough/MCP/hosted-tool secret
     environment values through named scopes;
   - remove profile registrations on profile deletion and turn-specific
     registrations after finalization;
   - never put a registered value in `repr`, logs, audits, exceptions, or test
     failure messages.

Extend `tests/test_auth.py`, `tests/test_config_profiles.py`, and
`tests/test_app_server.py` to assert:

- app-server gets separate `CODEX_HOME` and `HOME` paths;
- generated TOML parses with the pinned Codex `--strict-config` path;
- both broker profiles contain every mandatory deny entry;
- the auth parent can still report login status and start app-server;
- workspace/read-only thread calls select only the named profile;
- full access is rejected without the dedicated authorization credential and
  is never falsely reported as credential-isolated;
- no credential value appears in generated config, logs, `repr`, or assertion
  output.

**Verify**:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest discover -s tests -p 'test_auth.py' -v
PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest discover -s tests -p 'test_config_profiles.py' -v
PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest discover -s tests -p 'test_app_server.py' -v
```

Expected: all three test selections pass.

### Step 4: Exercise the real sandbox before declaring the broker ready

1. Create `src/codex_broker/sandbox_probe.py` with a frozen result type that
   records status, platform/backend, Codex version, checked permission profile,
   timestamp, duration, and a redacted administrator diagnostic. Never include
   canary contents or credentials.
2. Implement one bounded startup probe, cached in `BrokerServices`. Do not
   launch a new process on every `/readyz` call.
3. The Linux probe must exercise the same app-server permission-profile path
   without calling a model or requiring authentication:
   - create a temporary Codex home using the same managed profile renderer;
   - create a temporary workspace and a separate protected canary path;
   - start pinned `codex app-server --listen stdio:// --strict-config`;
   - send `initialize`/`initialized`;
   - call `command/exec` with the broker workspace permission profile and the
     temporary workspace as `cwd`. Pinned `0.144.6` does not accept
     `runtimeWorkspaceRoots` on this command, so `cwd` is the runtime workspace
     root for the canary;
   - verify a harmless command executes and can write inside the temporary
     workspace;
   - verify the protected canary is not readable;
   - close the process and delete the temporary paths in `finally`;
   - use hard startup/request/process timeouts and reap the child on every
     failure path.
4. On non-Linux platforms, return `unsupported` under the default `warn` mode;
   do not claim that the Linux Bubblewrap boundary was verified. A deployment
   that explicitly sets `required` must become not-ready if no supported probe
   exists.
5. Expose the cached result from `GET /readyz` as an additive
   `sandboxPreflight` object. Under `required`, any failed or unsupported probe
   makes readiness `503`; under `warn`, readiness stays `200` but includes the
   degraded result; `disabled` reports that it was intentionally skipped.
6. Before starting any `read-only` or `workspace-write` turn, require a healthy
   cached result when mode is `required`. Finalize the turn without contacting
   the model when unavailable. Separately authorized `danger-full-access` does
   not depend on Bubblewrap.
7. Add `SANDBOX_UNAVAILABLE = "sandbox_unavailable"` and a stable public
   message in `src/codex_broker/runtime_errors.py`. Recognize Bubblewrap/user
   namespace failures only when the message contains both a sandbox backend
   marker and a namespace/permission failure marker. Keep the exact redacted
   diagnostic for administrators; do not expose it as assistant-authored text.
8. If a runtime sandbox failure occurs after a successful startup probe, mark
   the cached result unhealthy for subsequent sandboxed turns until an
   operator-triggered restart or an explicitly implemented bounded reprobe.
   Do not loop probes on each failing turn.
9. Extend metrics with probe success/failure status and the count of turns
   rejected before model start.

Extend `tests/fake_codex.py` with a deterministic `command/exec` probe response
and create `tests/test_sandbox_probe.py` covering success, unreadable-canary
failure, command-start failure, timeout, child reaping, required/warn/disabled
readiness, scheduler rejection before model contact, and exact Bubblewrap error
classification.

**Verify**:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest discover -s tests -p 'test_sandbox_probe.py' -v
PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest discover -s tests -p 'test_readiness.py' -v
```

Expected: all probe and readiness tests pass, including cleanup assertions.

### Step 5: Sanitize model/runtime events before persistence and again at egress

Use one sanitization policy implementation at two boundaries: before storing
new model/runtime-derived data, and immediately before returning stored data to
a client. For rows created by older broker versions, the egress pass protects
credential-shaped patterns and values still present in the active registry; it
does not rewrite those historical rows or promise detection of an arbitrary
old value whose credential has already been rotated.

1. Construct one `SecretSanitizer` in `BrokerServices.build` and pass it to
   `StateStore`, `AuthManager`, `AppServerPool`, `TurnScheduler`, and HTTP/OpenAI
   response helpers instead of constructing ad hoc redactors.
2. In `BrokerTurnContext`, run all streaming text event types through a
   per-turn `StreamingSecretSanitizer` before `StateStore.append_event`:
   - `message.delta`;
   - `reasoning.summary.delta`;
   - `plan.delta`;
   - `tool.output.delta`;
   - any future normalized event explicitly marked as a text delta.
3. Flush pending safe text before its item-completed or terminal event. Preserve
   event order and do not drop the final suffix. A per-chunk call to the current
   regex helper is not acceptable because it leaks values split across chunks.
4. Apply recursive structured sanitization to completed message/tool items,
   raw Responses output items, approval/user-input/MCP events, errors, terminal
   payloads, and debug raw params.
5. Add a defensive sanitization hook inside `StateStore` and
   `state_transactions.finalize_turn` for model/runtime-derived:
   - normalized event payloads;
   - pending interaction requests/responses/fallbacks;
   - audit payloads;
   - turn error/public/admin messages.
   Keep raw turn input unchanged: queued execution and OpenAI history
   reconstruction require the caller's exact input. Document that intentionally
   supplied secrets remain the caller's responsibility and are a separate
   encryption-at-rest concern.
6. Sanitize immediately before native SSE, audit API responses, synchronous
   turn responses, OpenAI Responses output, OpenAI Responses SSE, and Chat
   Completions SSE. This second pass must use the same mode and policy, not a
   separate regex list.
7. In `raw` mode, normalized event persistence and client output must remain
   byte-for-byte compatible with the current implementation. Logs and optional
   raw debug fields remain redacted.
8. Add a structured counter for actual replacements. Count replacements, not
   calls to the sanitizer, and never label metrics with secret values or
   unbounded paths.

Regression tests must prove the complete path, not only helper behavior:

- extend the existing raw-event test in `tests/test_broker.py` to assert that
  safe normalized payload JSON in SQLite does not contain any synthetic
  credential;
- assert the same for terminal events, audit rows, and pending interaction
  rows;
- make fake Codex emit one synthetic credential split at every possible delta
  boundary and verify native SSE never contains it;
- verify synchronous Responses output, Responses SSE, and Chat Completions SSE
  never contain it in safe mode;
- verify the replacement marker and all ordinary surrounding text arrive in
  the correct order;
- pre-seed an old unsanitized event row containing a credential-shaped or
  currently registered synthetic canary and verify safe egress redacts it
  without modifying the row;
- verify `raw` mode returns the synthetic output unchanged through native and
  OpenAI-compatible paths;
- verify logs and debug raw fields still redact the same synthetic value in
  `raw` mode;
- verify one owner's streaming suffix state cannot affect another owner or
  turn.

**Verify**:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest discover -s tests -p 'test_security.py' -v
PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest discover -s tests -p 'test_broker.py' -v
PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest discover -s tests -p 'test_openai_compat.py' -v
```

Expected: all pass; test SQL rows and captured SSE bodies contain no safe-mode
canary value.

### Step 6: Expose Auto-review without presenting it as a sandbox replacement

1. In `src/codex_broker/scheduler_config.py`, accept
   `approvalsReviewer` and snake-case `approvals_reviewer` from request options
   and configuration profiles. Request values retain the existing precedence
   over profile defaults.
2. Map the resolved value to app-server `approvalsReviewer` for
   `thread/start`, `thread/resume`, and `turn/start` as supported by pinned
   `0.144.6`.
3. Accept public values `user` and `auto_review`. Do not advertise the legacy
   `guardian_subagent` alias even if pinned Codex accepts it. Reject unknown
   values before starting app-server.
4. Default managed profiles to Auto-review with a granular policy that enables
   reviewed `request_permissions` requests and disables unsandboxed shell
   escalation. Reject an explicit effective combination of `approvalPolicy: never` and
   `approvalsReviewer: auto_review` with a clear message: no approval requests
   would exist for Auto-review to evaluate. When the approval policy is omitted,
   send the broker's granular policy: `request_permissions` may be reviewed,
   while `sandbox_approval` remains false. Do not inherit an unspecified Codex
   approval default in managed mode.
5. Add the resolved reviewer, caller sandbox choice, actual permission profile,
   event-sanitization mode, and sandbox-preflight status to persisted resolved
   options or a bounded security audit event. Never include policy TOML,
   credentials, environment values, or raw auth paths in the public payload.
6. If app-server returns `activePermissionProfile` and `instructionSources`,
   record safe provenance for administrator diagnostics. Treat missing fields
   as a pinned-protocol mismatch under tests, not as proof that a skill reader
   is unavailable.

**Verify**:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest discover -s tests -p 'test_config_profiles.py' -v
PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest discover -s tests -p 'test_broker.py' -v
```

Expected: supported reviewer settings reach all required app-server calls;
invalid and ineffective combinations fail before model execution.

### Step 7: Make the production image prove its sandbox assumptions

1. Add the distribution's `bubblewrap` package to `Dockerfile` and retain the
   non-root `broker` user. Codex `0.144.6` prefers this system binary and falls
   back to its bundled helper only when absent.
2. Do not add `privileged`, `CAP_SYS_ADMIN`, `seccomp=unconfined`, or
   `apparmor=unconfined` to `examples/docker-compose.yml` as a guessed fix.
3. Add a CI image test after build that starts the broker image under the
   intended default container security settings and runs the real cached
   preflight. The test must prove both workspace writing and protected-canary
   denial.
4. If the default GitHub/Docker environment blocks user namespaces, diagnose
   whether the blocker is the Docker seccomp profile, host user-namespace
   policy, or AppArmor. Add only the narrowest tested deployment requirement
   to docs. If the only working configuration is an unconfined or privileged
   container, stop under the plan's STOP conditions instead of weakening the
   example silently.
5. The approved deployment requirement is
   `examples/seccomp/codex-broker.json`, derived byte-for-byte from Moby
   seccomp v0.1.0 except for the canary-proven Bubblewrap rules: mount and
   `pivot_root`, `umount2(MNT_DETACH)`, `clone` calls containing
   `CLONE_NEWUSER`, and exact `unshare(CLONE_NEWUSER)` or
   `unshare(CLONE_NEWNS)` calls. Wire it into Compose and CI together with
   `no-new-privileges`; do not send a legacy sandbox mode alongside the
   broker-owned permission profile.
6. Change `examples/docker-compose.yml` from Codex `0.144.3` to `0.144.6` so
   local deployments exercise the same protocol and sandbox behavior as the
   image default.

**Verify**:

```bash
docker build --build-arg CODEX_VERSION=0.144.6 -t codex-broker:security-plan .
docker run --rm --entrypoint codex codex-broker:security-plan --version
```

Expected: build exits 0 and the second command reports `codex-cli 0.144.6`.
Then run the repository's new container preflight command documented by the
implementation; it must exit 0 without an unconfined or privileged container.

### Step 8: Update operator and API documentation

Update the canonical Fern pages and concise README summary together with the
behavior change:

1. `fern/docs/pages/operations/configuration-reference.mdx`:
   - document `CODEX_BROKER_EVENT_SANITIZATION_MODE` and why it is not a request
     option;
   - document `CODEX_BROKER_SANDBOX_PREFLIGHT` and extra deny paths;
   - document `approvalsReviewer` and its relationship to `approvalPolicy`;
   - state that safe sanitization protects credentials, not arbitrary content;
   - state that logs/raw debug fields remain redacted in `raw` event mode.
2. `fern/docs/pages/operations/deployment.mdx`:
   - replace the old readiness list with the cached real-sandbox probe;
   - document verified Linux user-namespace/container requirements and exact
     failure diagnosis;
   - make clear that Auto-review does not replace Bubblewrap;
   - make clear that separately authorized `danger-full-access` bypasses
     filesystem read isolation;
   - tell operators to rotate credentials if prior output exposed them.
3. `fern/docs/pages/runtime/architecture.mdx`:
   - show app-server `CODEX_HOME` versus non-secret `HOME`;
   - show where permission-profile enforcement occurs;
   - show the safe persistence and egress sanitization boundaries;
   - explain that historical rows are egress-filtered for recognized patterns
     and currently registered values but are not rewritten.
4. `README.md`:
   - update implemented-capability bullets without duplicating the full
     configuration reference.
5. Regenerate OpenAPI only if the exporter produces a change for additive
   readiness fields or error documentation. Do not hand-edit generated JSON.
6. Read all new user-facing copy once for concrete language. Use
   "The command sandbox could not start" rather than internal phrases such as
   "reader unavailable" or unsupported claims about the exact container
   mechanism.

**Verify**:

```bash
uv run python scripts/export_openapi.py
pnpm docs:check
git diff --check
```

Expected: all commands exit 0 and the generated OpenAPI file has no unexplained
drift.

### Step 9: Run the complete gate and inspect the security diff

1. Run every narrow test above.
2. Run the full warning-sensitive suite.
3. Build and exercise the production Docker image with the real sandbox canary.
4. Inspect the complete uncolored diff. Confirm every changed file is in scope,
   no test fixture contains a real credential, no log/assertion renders a
   registered secret, and no broad container privilege was added.
5. Update Plan 002's status in `plans/README.md` only after all done criteria
   pass.

**Verify**:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest discover -s tests
uv run python scripts/export_openapi.py
git diff --exit-code -- fern/openapi/openapi.json
pnpm docs:check
git diff --check
git status --short
```

Expected: all gates exit 0; `git status --short` lists only intentional in-scope
changes before commit.

## Test plan

### Unit tests

- `tests/test_security.py`: recursive and exact-value sanitization, raw mode,
  streaming boundary handling, concurrency, and mandatory log redaction.
- `tests/test_sandbox_probe.py`: probe protocol, timeout/cleanup behavior,
  readiness modes, cached state, and runtime-error classification.
- `tests/test_config_profiles.py`: legacy sandbox-to-named-permissions mapping,
  precedence, absolute runtime roots, and Auto-review validation.
- `tests/test_auth.py`: generated permission TOML, separate runtime home,
  credential registration lifecycle, and unchanged authentication behavior.

### Integration tests

- `tests/test_broker.py`: SQLite event/audit/interaction contents, terminal
  paths, native SSE, resolved security provenance, and raw-mode compatibility.
- `tests/test_openai_compat.py`: safe synchronous Responses, Responses SSE,
  Chat Completions SSE, historical-row egress protection, and raw mode.
- `tests/test_app_server.py`: process environment, pool-key behavior, and exact
  pinned app-server parameters.
- `tests/test_readiness.py`: required/warn/disabled readiness results.
- `tests/fake_codex.py`: deterministic split-delta, permission-profile, and
  sandbox-probe fixtures only.

### Container test

The built Linux image must run a real no-model app-server `command/exec`
preflight under ordinary intended deployment settings. A mocked Python test or
successful config parse is not sufficient evidence that Bubblewrap denied the
canary.

## Done criteria

All conditions must hold:

- [x] The pinned `0.144.6` schema and real image confirm the named-permission
      and Auto-review fields used by the implementation.
- [x] App-server authenticates with its selected `CODEX_HOME` while its shell
      environment uses a non-secret `HOME`.
- [x] Real `read-only` and `workspace-write` container canaries can execute,
      workspace-write can write in cwd, and neither can read protected broker
      paths.
- [x] Both managed profiles deny reads outside the canonical authorized
      workspace roots except for `:minimal`; exact broker auth, state, and
      secret denies cannot be overridden by Auto-review grants.
- [x] `/readyz` becomes not-ready under required mode when that canary fails.
- [x] Sandboxed turns fail with `sandbox_unavailable` before model contact when
      required preflight is unhealthy.
- [x] `danger-full-access` is rejected without the separate authorization
      credential and is documented as not providing broker credential read
      isolation.
- [x] Safe mode is the default and cannot be changed through a request, bundle,
      or configuration profile.
- [x] Safe-mode SQLite event, audit, interaction, and terminal payloads contain
      no synthetic registered or credential-shaped canary.
- [x] Safe-mode native SSE and every OpenAI-compatible response form contain no
      canary, including canaries split across delta boundaries.
- [x] Raw mode preserves normalized payloads while logs and debug raw fields
      remain redacted.
- [x] `approvalsReviewer: auto_review` reaches pinned app-server only with a
      compatible approval policy.
- [x] Routine in-profile commands do not prompt, while eligible explicit
      boundary requests are reviewed without enabling unsandboxed shell
      escalation.
- [x] The example Compose pin matches Dockerfile Codex `0.144.6`.
- [x] Full Python tests, OpenAPI drift check, Fern docs check, Docker build,
      container canary, and `git diff --check` all pass.
- [x] No runtime dependency or database schema version was added.
- [x] No file outside the in-scope list was modified.
- [x] `plans/README.md` marks Plan 002 DONE only after every prior item passes.

## STOP conditions

Stop and report back rather than improvising if:

- an in-scope file has materially drifted from the current-state excerpts since
  commit `aaca3b9`;
- Codex `0.144.6` does not support the documented `permissions`,
  `runtimeWorkspaceRoots`, `permissionProfile`, or `approvalsReviewer` fields;
- the real `0.144.6` Linux canary can read a denied path even though config
  parsing succeeds;
- denying `config.auth_root` prevents the app-server parent—not only its
  sandboxed child—from authenticating;
- the implementation would need to send both named `permissions` and legacy
  `sandbox` fields on the same app-server request;
- the production image can pass the canary only with privileged mode,
  `CAP_SYS_ADMIN`, or an unconfined seccomp/AppArmor profile;
- streaming sanitization cannot prevent cross-chunk leakage without dropping
  or reordering ordinary output; do not fall back to independent per-chunk
  regex replacement;
- exact-value registration would require logging, serializing, or exposing a
  credential value;
- a real credential is discovered in a tracked file or test fixture; record
  only its type and location, stop, and ask the operator to rotate it;
- a database schema migration becomes necessary; design that separately rather
  than changing the exact-version contract inside this plan;
- completing a step requires a file outside the stated scope;
- any verification fails twice after a reasonable scoped correction.

## Maintenance notes

- Re-run the generated-schema checks and real container canary whenever the
  pinned Codex version changes. Permission-profile syntax and sandbox backends
  are versioned behavior.
- The broker-managed profiles protect sandboxed commands. They do not make
  separately authorized `danger-full-access` safe, and Auto-review is an
  approval decision mechanism, not a sandbox backend.
- Keep event sanitization deployment-scoped. Adding a convenient per-turn raw
  override would undo the trust boundary established here.
- Review new normalized event types for text deltas. Every new delta source must
  opt into the streaming sanitizer or explicitly prove it cannot carry text.
- This plan deliberately does not rewrite historical SQLite rows. Safe egress
  protects recognized patterns and currently registered values, while existing
  retention eventually removes old rows; it cannot recover an arbitrary old
  secret after that value has been rotated out of the registry. A targeted
  incident purge/backfill command is a worthwhile separate plan.
- Per-principal workers or containers would provide a stronger boundary against
  app-server/process compromise and would make full-access isolation possible.
  That architectural change remains deferred.
- A future `dual` mode could preserve encrypted raw diagnostics behind a
  separate administrator capability. Do not overload this plan's `raw` mode or
  ordinary event endpoints with that responsibility.
