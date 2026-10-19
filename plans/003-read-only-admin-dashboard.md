# Plan 003: Add a read-only operator dashboard

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report—do not improvise. When done, update the status row for this plan in
> `plans/README.md` unless a reviewer dispatched you and told you they maintain
> the index.
>
> **Drift check (run first)**:
> `git diff --stat 94cc4a6..HEAD -- src/codex_broker/http_api.py src/codex_broker/config.py src/codex_broker/state.py src/codex_broker/state_schema.py src/codex_broker/scheduler.py src/codex_broker/app_server.py src/codex_broker/openai_api.py tests fern/docs/pages/operations fern/docs/pages/runtime/architecture.mdx README.md plans/README.md`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: MEDIUM (new credential and a new cross-owner read path)
- **Depends on**: none
- **Category**: operations / observability
- **Planned at**: commit `94cc4a6`, 2026-10-19

## Why this matters

Operators can currently see the broker only through `/healthz`, `/readyz`, flat
Prometheus text on `/metrics`, container logs, and direct SQLite queries. None
of these answers everyday questions quickly: which owners are active, whose
sign-in is broken, which turns failed and why, whether App Server processes are
crashing, and what happened recently in the audit log.

This plan adds a read-only dashboard served by the broker itself at `/admin`,
backed by a small set of JSON endpoints under `/admin/api/`. It uses a separate
operator credential so that handing someone dashboard access never grants
the ability to run turns, and holding the host key never grants a
cross-owner view.

## Decisions already made

These were settled before planning. Do not reopen them during execution.

1. **Separate operator key.** A new `CODEX_BROKER_OPERATOR_KEY_FILE` holds the
   only credential the admin routes accept. The host key
   (`CODEX_BROKER_INTERNAL_KEY`), the danger-full-access key and
   OpenAI-compatible keys are all rejected on admin routes, and the operator
   key is rejected everywhere else.
2. **Read-only.** No endpoint changes broker state. Actions such as
   invalidating a runtime or interrupting a turn are out of scope (see
   "Out of scope").
3. **JSON endpoints plus one static page.** No frontend build step, no new
   Python or npm dependencies, no external scripts, fonts or stylesheets.
4. **Hash-only owners.** The broker keeps storing only `owner_hash` and
   `auth_principal_hash`. The dashboard shows hashes. It never stores or
   displays raw owner IDs, and it adds no display-name or label field.

## Current state

### Authentication

`src/codex_broker/http_api.py` dispatches every request through one method.
OpenAI-compatible paths are handled first with their own keys; everything else
except `/healthz` and `/readyz` requires the single host key:

```python
if openai_api.handle_openai_route(self, method, path, query):
    return
if not is_unauthenticated_path(method, path) and not self._authorized():
    self._json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
    return
```

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

`src/codex_broker/config.py` already reads a file-only secret the same way this
plan needs (`_danger_full_access_key()`, reading
`CODEX_BROKER_DANGER_FULL_ACCESS_KEY_FILE`) and compares it with
`secrets.compare_digest` in `_danger_full_access_authorized()`.

### Owner identity

Owner IDs are HMAC-hashed with `owner_hash_secret` (`AuthManager.hash_owner()`
in `src/codex_broker/auth.py`, `owner_digest()` in `util.py`). An operator
cannot compute an owner's hash without that secret, so the dashboard needs a
server-side lookup to find a known owner.

### Data available

- **Live:** `TurnScheduler.metrics()` (`scheduler.py`) and
  `AppServerPool.metrics()` (`app_server.py`: `active_app_server_children`,
  `app_server_restarts`, `pending_app_server_creations`); the sandbox probe
  result used by `/readyz`.
- **SQLite** (`state_schema.py`): `threads`, `turns`, `auth_profiles`,
  `app_server_processes`, `audit_logs`, `audit_action_counts`,
  `pending_interactions`, `events`.
- `turns.input_json`, `events.payload_json`, `events.raw_params_json`,
  `pending_interactions.request_json` and `response_json` contain conversation
  content. **The dashboard must never return them.**

### Metrics path templating

`metric_path_template()` returns `"unknown"` for any path it does not
recognise, so admin requests would be counted under `unknown` until it is
extended.

## Target design

### Configuration

| Variable | Meaning |
|---|---|
| `CODEX_BROKER_OPERATOR_KEY_FILE` | Path to a file containing the operator key. When unset, every `/admin` path returns `404 not_found` and the dashboard does not exist. |

Startup must fail with a clear error if the file is missing or empty, or if
the key equals the host key or the danger-full-access key. Only the file form
is supported; do not add an inline `CODEX_BROKER_OPERATOR_KEY` variable.
`CODEX_BROKER_ALLOW_UNAUTHENTICATED` must **not** open admin routes.

### Routing and auth

Handle `/admin` and `/admin/...` in `_dispatch` **before** the OpenAI route
and the host-key check, in a new module `src/codex_broker/admin_api.py`
(mirroring how `account_api.py` and `auth_api.py` are split out):

```text
/admin, /admin/            GET  static page; no auth (contains no data)
/admin/api/...             GET  requires operator key
/admin/api/owners/lookup   POST requires operator key; read-only
anything else under /admin       404
```

- Accept the operator key only as `Authorization: Bearer <key>`. Compare with
  `secrets.compare_digest`.
- Add a guard in `_authorized()` (host routes) that rejects a request whose
  presented key equals the operator key, even when
  `allow_unauthenticated` is on.
- Every admin response sets `Cache-Control: no-store`.
- The page response sets a strict CSP:
  `default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'`,
  plus `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`.
  Serve the page's JS and CSS as separate files (`/admin/app.js`,
  `/admin/app.css`) so no inline script is needed. These are also
  unauthenticated and contain no data.
- Log each admin API call with `json_log` (`admin.request`, route template,
  status). Do not write admin reads into the owner-scoped `audit_logs` table.

### Endpoints

All list endpoints take `limit` (default 50, max 200) and an opaque `cursor`,
and return `{"items": [...], "nextCursor": "..." | null}`. Timestamps are the
stored ISO strings. Hashes are returned in full; the page shortens them for
display.

| Endpoint | Returns |
|---|---|
| `GET /admin/api/overview` | Broker version and uptime; readiness (same checks as `/readyz`, reuse the logic rather than copying it); sandbox preflight result; live counters from `scheduler.metrics()` (active turns, queued turns, App Server children, restarts, pending creations); counts of threads by status, turns by status in the last 24 hours, pending interactions, distinct owners seen in the last 24 hours and 7 days; auth success and failure totals. |
| `GET /admin/api/owners` | One row per `owner_hash`: first seen, last activity, thread count, turns in the last 24 hours by status, most recent turn error code, distinct `auth_principal_hash` values. Sort by last activity, newest first. |
| `GET /admin/api/owners/{ownerHash}` | The owner row above, plus its threads (id, status, profile, config profile, host app, bundle id, created/updated), its last 50 turns (see turn fields below), and the `auth_profiles` rows for its principals (profile, auth type, auth status, fingerprint, updated). 404 if the hash has no rows. |
| `POST /admin/api/owners/lookup` | Body `{"ownerId": "..."}`; returns `{"ownerHash": "...", "known": true\|false}`. The raw ID travels only in the body, is never logged, stored or echoed back. |
| `GET /admin/api/turns` | Recent turns across all owners, newest first. Filters: `status`, `ownerHash`, `errorCode`, `since`. |
| `GET /admin/api/processes` | Live pool counters plus recent `app_server_processes` rows (pool key hash, principal hash, profile, config profile, pid, status, started, last seen, closed, exit code). Filter: `status`. |
| `GET /admin/api/audit` | `audit_logs` rows newest first. Filters: `action`, `ownerHash`. `payload_json` is parsed and passed through `broker.sanitizer` before returning. Also returns `audit_action_counts` on the first page. |

**Turn fields returned:** owner hash, thread id, turn id, principal hash,
profile, config profile, host app, bundle id, mode, status, error code,
`public_message`, `admin_message` (both sanitized), broker version, created,
started, completed. **Never** `input_json`, `resolved_options_json`,
`request_fingerprint` or `idempotency_key`.

**Never returned by any endpoint:** turn input, event payloads or raw params,
interaction request or response bodies, any secret or key file contents, raw
owner IDs.

Put the SQL in a new `src/codex_broker/state_admin.py` (or a clearly separated
section of `state.py` if that keeps file sizes reasonable) using explicit
column lists, never `select *`. Add only the indexes the queries need, with
`create index if not exists` in `state_schema.py`, for example
`turns(updated_at)`, `turns(status, updated_at)`,
`threads(owner_hash, updated_at)` and `app_server_processes(started_at)`.
Check each query with `explain query plan` on a seeded database before adding
an index.

### The page

One HTML file plus one JS and one CSS file under
`src/codex_broker/admin_static/`, included in the wheel and the Docker image.
Plain JS, no framework.

- **Sign-in:** on first load, the page asks for the operator key, keeps it in
  `sessionStorage` (cleared when the tab closes) and sends it as a bearer
  header on every fetch. A "Sign out" button clears it. On 401 the page
  returns to the key prompt. No cookies, so no CSRF surface.
- **Overview tab:** health banner (ready / not ready with the error list,
  sandbox status), live counters, turns by status in the last 24 hours, recent
  failed turns.
- **Owners tab:** owner table with shortened hashes and a copy button; a
  "Find an owner by ID" box that calls the lookup endpoint and opens the
  owner's detail; owner detail with threads, recent turns and sign-in status.
- **Turns tab:** filterable recent turns; failed turns show the public and
  admin messages.
- **App Servers tab:** live pool counters and recent processes, with non-zero
  exit codes highlighted.
- **Audit log tab:** filter by action and owner hash, paginated.
- Manual refresh button plus an optional auto-refresh every 10 seconds on the
  Overview tab only. No polling on other tabs.
- Works at phone width; respects `prefers-color-scheme`.
- Write the interface copy for an operator, not for a developer: "Sign-in
  failing" rather than `auth_status=failed`, "Waiting to start" rather than
  `queued`. Keep raw values available in a tooltip or detail row.

### Metrics

Extend `metric_path_template()` so admin paths are templated, e.g.
`admin/api/owners/ownerHash`, `admin/api/turns`, `admin` for page and static
files.

## Steps

1. **Config.** Add `operator_key` to `BrokerConfig` (with `repr=False`),
   loaded by a `_operator_key()` helper modelled on `_danger_full_access_key()`.
   Validate it differs from the host and danger keys.
   *Verify:* new tests in `tests/test_config_profiles.py` or a new
   `tests/test_admin_api.py` for missing file, empty file, equal-to-host-key,
   equal-to-danger-key, and unset.
2. **Routing and auth.** Create `admin_api.py` with
   `handle_admin_route(handler, method, path, query) -> bool`, call it in
   `_dispatch` before `handle_openai_route`, and add the operator-key
   rejection to `_authorized()`.
   *Verify:* tests that (a) admin routes 404 when the key is unset, (b) host
   key, danger key and an OpenAI-compatible key get 401 on `/admin/api/*`,
   (c) operator key gets 401 on `/v1/owners/...` and `/metrics`, also with
   `allow_unauthenticated` on, (d) `X-Codex-Broker-Key` with the operator key
   is rejected on admin routes.
3. **Queries.** Implement the read queries and indexes. Seed a state database
   in tests with several owners, threads, turns in each status, processes and
   audit rows.
   *Verify:* tests per endpoint for shape, ordering, filters, pagination and
   `limit` clamping; `explain query plan` shows index use for the owners and
   turns list queries.
4. **Content-leak test.** In a test that runs a real turn through
   `tests/fake_codex.py` with a distinctive input string and a fake secret
   that the sanitizer knows, call every admin endpoint and assert neither
   string appears in any response body.
5. **Owner lookup.** Implement the lookup endpoint.
   *Verify:* a known owner returns its hash and `known: true`; an unknown one
   returns `known: false`; the raw owner ID does not appear in captured JSON
   logs.
6. **Static page.** Add the three files, serve them with the headers above,
   package them (`pyproject.toml` wheel includes, `Dockerfile` copy if
   needed).
   *Verify:* run the broker locally with a fake Codex and an operator key
   file, open `/admin` in the in-app browser, sign in, and check every tab at
   desktop and phone width, in light and dark mode. Confirm the browser
   console has no CSP violations.
7. **Metrics templating.** Extend `metric_path_template()`.
   *Verify:* extend `tests/test_metrics.py`.
8. **OpenAPI.** Decide with the reviewer whether admin endpoints belong in
   `/openapi.json`. Default: leave them **out** of the public document and the
   generated SDK, and document them in the operations docs instead.
9. **Docs.** Add `CODEX_BROKER_OPERATOR_KEY_FILE` to
   `fern/docs/pages/operations/configuration-reference.mdx`; add a "Status
   dashboard" section to `fern/docs/pages/operations/deployment.mdx` covering
   how to create the key file, mount it as a Docker secret, and that `/admin`
   should not be exposed publicly without a reverse proxy or access control in
   front; note the new read path in the trust section of
   `fern/docs/pages/runtime/architecture.mdx`; update `README.md` if it lists
   configuration. Update `compose.yaml` only with a commented-out example.
10. **Full gate.**
    `PYTHONDONTWRITEBYTECODE=1 uv run python -W error::ResourceWarning -m unittest discover -s tests`
    and `pnpm docs:check`.

## STOP conditions

- Any endpoint would need to return turn input, event payloads, interaction
  bodies or raw owner IDs to be useful.
- A query needed for an endpoint cannot avoid a full scan of `events` or
  `audit_logs` on a realistically sized database.
- Serving the page requires inline script, an external asset or a new
  dependency.
- Admin routing cannot be placed before the OpenAI and host-key checks
  without changing behaviour of existing routes (existing tests fail).
- Any change appears to require writing to state from an admin endpoint.

## Out of scope

- **Actions** (invalidate runtime, interrupt turn, resolve interaction,
  archive thread). They need their own permission, an audit trail of operator
  actions, and confirmation UI. Plan separately once the read-only view has
  been used for a while.
- **Owner display names or labels.** Rejected under decision 4. If needed
  later, the host application can map hashes to names on its side by calling
  the lookup endpoint.
- **Time-series history and charts.** Use Prometheus and Grafana on
  `/metrics` for trends; this dashboard shows the current state and recent
  records.
- **Multiple operator keys, roles, or SSO.** One key is enough for now; put
  SSO in front with a reverse proxy if needed.
- **Streaming updates** (SSE or WebSocket) to the page.

## Adjacent finding

`_authorized()` compares the host key with `==` rather than
`secrets.compare_digest`. This is a small, independent fix; make it in its own
commit during step 2 since that step touches the same method.
