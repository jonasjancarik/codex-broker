---
read_when: planning or implementing a Claude Code backend, provider-neutral runtime interfaces, Claude authentication, external-tool continuations, session recovery, or Claude workspace isolation.
---

# Add a Claude Code backend

Status: PROPOSED — saved for future implementation; no backend implementation is authorized by this document.

Prepared on 21 October 2026 against commit `e92f22b`, with concurrent local
changes present. Incorporates an Astra architecture review of the same design.
SDK behavior below comes from documentation inspected on that date, unless
explicitly described as a local observation. No live Claude SDK feasibility
checks have been performed.

## Purpose and recommended approach

Add Claude Code as another execution backend behind the existing broker HTTP
API. Keep the broker responsible for product ownership, credential selection,
turn admission, durable records, event delivery, tool authorization and policy.
Use the official Claude Agent SDK in a broker-supervised internal worker rather
than creating a separate public Claude service.

The first step is a feasibility spike. Prove the SDK's external-tool handoff,
session and cancellation behavior before generalizing the scheduler. A model
response containing tool calls can finish an HTTP response while the SDK query
remains alive, waiting for the caller's results. That difference determines the
runtime interface and cannot be inferred from a successful text chat.

If the immediate product requirement is only Claude models with caller-run
functions and API-key authentication, evaluate the Anthropic Messages API first.
Its native request/tool-result lifecycle may meet that narrower requirement
without a suspended Claude Code runtime. Choose the Agent SDK when Claude
Code's harness, resumable sessions, skills or workspace execution are required.
Record that choice after the spike; do not implement both paths speculatively.

This plan is not a container migration, a public API redesign or permission to
replace current Codex behavior. It also does not schedule subscription login,
arbitrary user-supplied executable tools or cross-provider conversation moves.

## Current state and evidence

The reusable control plane already exists, but execution is coupled to Codex.
The scheduler constructs Codex options, checks out an App Server, ensures a
Codex thread, injects history and starts a turn. Authentication also creates
Codex configuration, while event and interaction normalization understand
Codex method names. Extract these dependencies only where a second backend
requires it.

| Existing component | Reuse and necessary changes |
| --- | --- |
| [`services.py`](../src/codex_broker/services.py) | Preserve shared service composition; replace unconditional Codex pool/probe construction with configured backend services. |
| [`scheduler.py`](../src/codex_broker/scheduler.py), [`scheduler_threads.py`](../src/codex_broker/scheduler_threads.py) | Preserve ownership, gates, queues and admission; delegate execution and session operations through a semantic backend interface. |
| [`identity.py`](../src/codex_broker/identity.py), [`openai_auth.py`](../src/codex_broker/openai_auth.py) | Preserve trusted identity policy and immutable API-key bindings; include backend selection in server-side bindings. |
| [`auth.py`](../src/codex_broker/auth.py), [`app_server.py`](../src/codex_broker/app_server.py) | Separate common credential lifecycle from Codex login, configuration, process launch and transport details. Keep Codex behavior in its adapter. |
| [`state_schema.py`](../src/codex_broker/state_schema.py), [`state.py`](../src/codex_broker/state.py) | Preserve records, owner scoping and transactions; add backend-aware references with a data-preserving migration. |
| [`events.py`](../src/codex_broker/events.py), [`interactions.py`](../src/codex_broker/interactions.py) | Share public event/interaction concepts; implement provider-specific translation without inventing missing fields. |
| [`openai_protocol.py`](../src/codex_broker/openai_protocol.py), [`openai_api.py`](../src/codex_broker/openai_api.py) | Reuse request validation and public wire formats; separate option/model resolution and history preparation from Codex RPCs and raw events. |
| [`scheduler_continuations.py`](../src/codex_broker/scheduler_continuations.py) | Reuse matching, admission, expiry and retry rules where applicable; do not reuse pending Codex RPC IDs as a generic execution contract. |
| [`bundles.py`](../src/codex_broker/bundles.py), [`sandbox_probe.py`](../src/codex_broker/sandbox_probe.py) | Preserve reviewed inputs, workspace checks and fail-closed gating; add Claude-specific materialization and enforcement checks. |

The [caller-tool continuation plan](004-retain-openai-tool-threads.md) and
[validation notes](../docs/tool-continuation-validation-2026-10-21.md) describe
the existing Codex implementation. Its live execution, public response and
logical broker turn can have different lifetimes. Some defaults were changing
in the working tree when this document was written; inspect current effective
configuration instead of copying historical defaults from this plan.

The earlier comparison of `openclaw-claude-proxy` at revision
`17b112d35dbe65b43c7095cbf4d827654b0ba7f0` found a useful stream-json transport
reference, but not a suitable replacement control plane. Its shared account and
environment, unauthenticated HTTP routes, history-insensitive sticky reuse and
prompted JSON function bridge do not satisfy this broker's intended contract.
Those findings came from source inspection and offline probes, not a live
deployment assessment. Reuse ideas only after independently validating them.

## Contracts to preserve

Existing Codex clients must retain their behavior, identifiers and credentials
unless a separately documented change is necessary. Backend choice must be
resolved by trusted configuration, then fixed to a thread. Neither a public
session ID nor a tool-call ID grants access to another owner's records.

Persist events before exposing them through native or OpenAI-compatible APIs.
Retain sanitization on storage, history reads and output, mandatory diagnostic
redaction, audit correlation and existing retention controls. Sanitized public
events are not executable provider checkpoints.

Unsupported capabilities must fail explicitly. Do not silently translate
Codex reasoning efforts, Fast/service tiers, steering, model catalog metadata,
approval review or token fields into vaguely similar Claude features. Capability
checks apply to native requests as well as the OpenAI-compatible adapter.

Managed execution must continue to deny access outside authorized workspace
roots and protect credential/control-plane paths. A failed required Claude
preflight must block the affected Claude execution mode before model work
starts. It must not make a healthy Codex backend unavailable unnecessarily.

## Runtime and execution model

### Three identities with different lifetimes

A **broker thread** is the durable, owner-scoped conversation. A **provider
execution** is one live SDK query with its callbacks, worker generation and
lease. A **response segment** is one Chat/Responses result, represented by the
broker's existing response/turn records where possible. A segment may end in
external tool calls while its execution remains waiting.

Give provider executions a stable identity. Do not correlate late events by
whichever mutable `turn_id` happens to be attached to a worker. Every event
must identify its execution and the response segment to which it belongs.
Codex can retain its existing upstream turn IDs through its adapter; Claude
must not populate Codex-specific fields with unrelated IDs.

Suggested execution states are `starting`, `running`, `waiting_for_tools`,
`resuming`, `completed`, `failed` and `cancelled`. These are design concepts,
not a requirement to replace existing public status values. Persist the public
history separately from any process-local callback futures.

While waiting, retain the thread gate and runtime lease. Release active
inference capacity only when the backend can establish that model generation
has stopped. Do not return a busy worker to the idle pool. Recheck admission
and credential/policy validity when a result arrives.

### Minimal backend interface

Define operations by meaning, not by exposing a generic `request(method,
params)` that merely hides Codex RPC names. The spike must establish the exact
types, event ordering and cancellation acknowledgements before this interface
is finalized.

| Operation or event | Required meaning |
| --- | --- |
| Capabilities and readiness | Report supported model/options and which execution policies have passed preflight. |
| Open/resume session | Resolve an exact broker-owned upstream session under the selected credentials and policy. |
| Start execution | Accept normalized instructions, content and reviewed tool declarations. |
| Text, tool, usage and terminal events | Supply ordered events suitable for shared persistence and response reconstruction. |
| Sealed external-tool handoff | Declare the complete set of calls exposed in one response segment, with no late calls silently added to that segment. |
| Supply results or approval | Resolve specific pending invocations/interactions exactly once within the supported retry contract. |
| Interrupt, close and health | Acknowledge termination or declare uncertainty, then release resources safely. |

Normalize only information the backend actually provides. For example, current
usage normalization expects Codex totals and context-window metadata. Missing
Claude reasoning counts or context limits must remain unavailable rather than
becoming invented zeros. Keep private reasoning out of public reconstruction.

### Claude worker process

Start with one active broker session per Claude SDK client. Use a separately
supervised Python worker with its own async event loop and an authenticated
local transport, such as inherited pipes. Apply the broker's allowlisted
environment before starting that process. Do not mutate the broker's global
environment to select credentials for concurrent requests.

Pool reuse must match backend, owner, principal, profile generation, workspace,
effective tools, settings and policy. Sharing an upstream account does not
authorize sharing a conversation. Start without cross-owner multiplexing or
aggressive prewarming. Keep queue and retained-worker caps, but do not describe
them as CPU, memory or process-tree budgets.

Pin the Agent SDK and the actual Claude CLI used by it together. Record their
versions in diagnostics and probe results. Disable ambient filesystem settings
and explicitly control hooks, plugins, skills, MCP discovery and any remaining
organization-managed policy. A settings flag alone is not proof that every
ambient capability has disappeared.

## Sessions, storage and migration

Add a backend identity to credential profiles and immutable thread bindings,
with existing data defaulting to Codex. Introduce generic provider-session and
execution references. Preserve existing Codex response fields as compatibility
fields; do not rename the public API wholesale or expose raw Claude session
enumeration to clients.

Use broker-controlled storage for Claude transcripts. Scope it by owner and
credential profile/generation, even when several owners share a principal.
Claude's documented cross-directory session lookup makes a shared transcript
directory especially unsuitable for tenant separation. Resume by an exact
stored ID, never by "most recent session" or an unchecked client-supplied ID.

Transcripts can contain prompts, tool results and private provider state that
public events do not contain. Define access, retention, backup and deletion
rules explicitly. Native model-run file tools must not be able to read other
sessions or mutate provider checkpoints. If authentication and transcripts live
under the same Claude configuration tree, distinguish the trusted runtime's
necessary access from commands' access.

The existing database rejects incompatible schema versions. Add a versioned,
transactional upgrade with a tested backup/rollback procedure. Verify that
threads, auth-profile instance bindings, idempotency records, interactions and
event history survive. Define downgrade behavior; do not tell operators to
empty the data directory to enable Claude. Keep model selection and backend
selection unambiguous, preferably through server-side configuration profiles
and compatibility-key bindings rather than inference from a model-name prefix.

## Authentication and credential delivery

Reuse the owner-to-principal policy and immutable API-key bindings. Implement
Claude-specific profile provisioning, active authentication probes, logout or
key removal, sanitization registration and revocation. Backend/profile changes
must invalidate affected workers and pending continuations without affecting
unrelated owners.

Start the product integration with Anthropic API keys. Personal SDK subscription
use and offering Claude login or subscription limits in a third-party product
are distinct cases in Anthropic's current documentation. Recheck those official
documents before designing subscription authentication; do not copy the
Codex device-login implementation or extract a user's keychain credentials.
Also record which billing mode the deployment actually uses rather than
assuming that a Claude plan implies free API execution.

Environment scrubbing removes unrelated inherited secrets. It does not protect
the Anthropic key intentionally delivered to the SDK if model-run descendants
can read its environment, files or process information. Before workspace mode,
prove a supported command-environment exclusion mechanism or use a separately
protected credential-injecting sampling proxy. If a proxy is needed, scope its
access to the authenticated worker/principal and approved upstream routes; the
model must not be able to select another principal or use it as a general
credential-bearing relay. Test process inspection as well as environment dumps.

Never place secrets in process arguments, saved runtime fingerprints, public
events or temporary test reports. Credential rotation must change the runtime
generation/fingerprint, retire stale idle workers and cancel or explicitly
reject waiting continuations under revoked credentials.

## Native tools and OpenAI-compatible handoffs

Register real Claude SDK MCP tools with JSON Schemas and handlers. Map internal
namespaced tool names to stable public function names and invocation IDs. Do
not implement tool calling by parsing assistant prose. Maintain separate paths
for reviewed broker-hosted tools and caller-executed OpenAI functions.

Hosted tools continue through the existing allowlists, endpoint authorization,
audit and error-sanitization policy. Caller-defined functions never choose a
bundle or authorize local execution. In the initial functions-only mode, remove
built-in shell, file, network, delegation and other unassigned tools; an
`allowed_tools` list is an auto-approval list, not an exclusive tool catalog.

For a caller function, the SDK handler registers an invocation and waits for a
future resolved by the broker. Once the adapter observes a complete, sealed
handoff, persist the response segment and return standard Chat `tool_calls` or
Responses `function_call` items. The execution stays alive without an open HTTP
connection. Do not supply a fake result to make the SDK yield control.

For example, response A asks the client to run `get_weather`. The client returns
that call's result in request B, using normal Chat history or Responses input
and `previous_response_id`. The broker authenticates B, reconstructs its
owner-scoped canonical history, verifies the retained execution's full binding,
then delivers the result to the original SDK callback. Claude continues and
the broker stores response B on the same thread.

Accept each expected result once, require the complete pending set supported
by that handoff, and reject unknown, duplicate or conflicting IDs. Correlate by
invocation identity, never only by function name and arguments; identical calls
can be distinct actions. Use an atomic waiting-to-resuming transition. Identical
retries attach to accepted work or receive the same stored response within the
documented guarantee; conflicting results cannot both be delivered.

Determine how Claude exposes all calls belonging to a model response and how
that relates to actual callback dispatch. A short timer or the first callback
does not establish a complete set. Test multiple and dependent calls, delayed
dispatch and tool results arriving while the message reader is active. Do not
promise parallel execution or multi-call batches unless the pinned runtime
demonstrates them. Document any narrower preview contract explicitly.

Claude MCP timeout/backgrounding settings can change whether a pending call
actually pauses the agent. Establish the non-interactive behavior, set those
controls deliberately, and verify that expiry, handler errors and cancellation
cannot release the agent to perform unapproved work. Keep the worker's message
reader responsive while callbacks wait.

## Failure, retry and recovery

Preserve durable broker records across process loss, but do not claim they can
restore callback futures. For the first preview, a lost or expired Claude
execution returns an explicit continuation-lost/expired error while previous
responses remain readable. Do not inherit Codex's replay fallback until Claude
has a verified equivalent for native roles, instructions, tool IDs and outputs.

Arbitrary client-supplied histories are a separate feasibility gate. Claude
session resume does not establish that edited assistant messages or an external
tool exchange can be injected faithfully. Never silently flatten those into a
user prompt. Decide which histories the preview supports, reject the others
before execution, and document the compatibility difference. Branch or replay
only through a demonstrated provider mechanism; switching backend on an
existing thread remains out of scope.

Track result acceptance and delivery against execution generation. Decide how
much of that journal is durable before promising retry behavior. If the broker
dies after delivery but before committing a response, surface an explicitly
uncertain outcome and avoid a second execution. Persisting "delivered" alone
does not prove the SDK consumed a result; external actions still need client or
tool-service idempotency. No exactly-once claim follows from this plan.

Timeout, explicit cancellation, revocation, shutdown and worker death must
resolve pending interactions, stop the correct execution and release its lease
once. A failed interrupt leaves the worker unusable until terminated and reaped.
Define HTTP disconnect separately: an ordinary lost stream must not accidentally
erase durable history or grant indefinite retention. Use finite idle/absolute
limits and reserved capacity for other work. Initially support one broker
process for live continuations unless routing/coordination is explicitly added;
requests reaching the wrong process must fail clearly instead of cold-replaying
an action.

## Isolation and workspace execution

Reuse the policy intent and authorization checks from
[Plan 002](002-harden-sandbox-and-secret-delivery.md), but implement and verify
Claude enforcement separately. Permission prompts are not an OS filesystem
sandbox, and the callback is not consulted for every auto-approved operation.
Do not map Codex `auto_review` to Claude's similarly named modes. Either provide
an explicitly supported broker approval path with deny-on-timeout behavior or
reject that policy combination.

The functions-only preview must show that unassigned native capabilities and
ambient executable configuration are unavailable. Workspace mode additionally
requires default-deny reads, permitted writes only in authorized roots,
credential/control-plane denies and network enforcement on every enabled tool
path. Claude's Bash sandbox settings do not by themselves prove restrictions
on native Read/Edit tools, WebFetch, local MCPs or hooks.

Set sandbox unavailability to fail closed, disallow unsandboxed command
fallback and reject excluded-command exceptions or weaker modes that undermine
the managed contract. Verify these on the actual hardened Linux image rather
than relaxing seccomp, adding privileged mode or relying on a laptop result.
If supported settings cannot meet the policy, use stronger runtime isolation or
keep workspace execution unavailable. A restricted worker launch, container or
credential proxy is a design option to assess after the spike, not a presumed
requirement to containerize every owner.

Reviewed SDK workers and MCP executables remain trusted processes. A command
sandbox does not contain a compromised runtime or malicious MCP executable.
Accepting arbitrary user executables requires the separate reassessment already
required by [the architecture trust assumptions](../fern/docs/pages/runtime/architecture.mdx#untrusted-executable-tools-require-stronger-runtime-isolation).
Concurrency limits do not impose workload CPU, memory or process budgets; use
the existing [resource-control discussion](../fern/docs/pages/runtime/architecture.mdx#resource-limits-do-not-require-a-container-per-user)
if those become necessary.

## Delivery sequence and acceptance gates

### Phase 0: Revalidate and prove feasibility

Re-read the source map and current continuation defaults. Record exact broker,
SDK and CLI revisions, deployment OS and relevant upstream settings. Confirm
that Claude Code capabilities, rather than model-only access, justify the SDK.
Install experimental dependencies only in an isolated development environment;
do not alter the production image or use production conversations as fixtures.

Build the smallest internal worker experiment before changing the shared schema
or scheduler. Use throwaway credentials/data under the normal authorization
rules and finite budgets. Report each check as passed, failed or unverified:

- Two native conversation turns resume the exact recorded Claude session.
- A standard Chat function call and a Responses call/result chain each span
  two HTTP response segments over one live execution, without prose parsing.
- Two same-name/same-argument invocations retain distinct identities; multiple
  calls and delayed callback dispatch do not lose calls or misattribute events.
- A delayed result keeps callbacks and stream reading responsive, with no
  unapproved model continuation through timeout or backgrounding.
- Changed instructions, tools, history, owner or profile generation cannot
  attach to an old execution. Concurrent identical/conflicting retries follow
  the declared result-delivery rules.
- Cancel, disconnect, idle expiry, absolute expiry, worker crash and credential
  revocation clean up correctly; loss after result delivery has an explicit
  uncertain outcome rather than silent replay.
- Functions-only sessions cannot invoke shell/file/network tools or ambient
  executable hooks/MCPs. Sentinel secrets from the broker stay unavailable.
- Record which arbitrary histories, restart cases and transcript resumes are
  actually supported. Missing usage remains missing and is never double-counted.

If a complete handoff cannot be detected or callbacks cannot wait safely, stop
that approach and assess a narrower SDK contract or Messages API alternative.
Do not hide a failed requirement behind timing heuristics or prompt instructions.
Save the observed event sequence and decision with this plan before Phase 1.

### Phase 1: Extract the shared runtime contract

Wrap existing Codex execution behind the semantic interface established by
Phase 0. Separate execution identity from response collection, provider event
normalization from persistence, and credential policy from provider mechanics.
Keep HTTP schemas and Codex option behavior compatible.

Add fake-backend contract tests covering admission, ordered events, handoff,
late events, cancellation, process loss and cleanup. Existing Codex tests and
focused real-runtime canaries must still pass on the exact resulting revision.
Do not broaden the refactor to unrelated modules or pool optimizations.

### Phase 2: Add provider-aware state and Claude preview

Implement the transactional migration, backend-bound profiles/threads,
supervised worker, API-key lifecycle, exact session resume, event translation
and native SDK function handlers. Share the existing OpenAI parser, history
validation and public response storage while applying the proven Claude
capability restrictions. Include explicit continuation-expired and unsupported
history behavior; keep unproven fallback disabled.

The preview acceptance gate is an isolated real-runtime run through the broker
HTTP API, using streamed and non-streamed Chat and Responses. A random tool
result available only to the client must reach the final answer, including a
successive tool round trip on the same execution. Independently inspect broker
records, native execution identity, call/result IDs and usage accounting.

Verify cross-owner/principal/profile rejection, invalid results, duplicate
requests, capacity exhaustion, process restart, secret redaction, migration
preservation and Codex regression coverage. Keep Claude disabled by default
until the exact image/configuration has passed those checks. Publishing or
deploying a preview is a separate authorized action; a local pass is not a
production rollout.

### Phase 3: Enable reviewed workspace capabilities

Prove the credential-delivery design and Claude-specific preflight before
exposing read-only or workspace-write modes. Use sentinel data, not real
credentials, for forbidden-read and exfiltration tests. Exercise native file
tools, Bash and each reviewed MCP/network path separately.

Verify another owner's workspace and transcript store, broker auth/state,
credential files, shell environment and process inspection. Include symlinks,
path traversal, denied workspace secrets, out-of-workspace artifacts, sandbox
unavailability, excluded commands, ambient configuration and revocation while
waiting. Check allowed operations too, so a probe that denies everything does
not count as a working execution mode.

Only advertise the modes and bundles that passed. Route approval and user-input
interactions through the existing broker interaction lifecycle with a verified
Claude mapping. Unsupported permissions, skills or bundle fields fail before
execution. Subscription auth, arbitrary executable MCPs and cross-provider
thread migration remain deferred.

## Documentation, verification and completion

Update the Fern architecture, authentication/integration, OpenAI compatibility,
configuration and deployment pages when behavior changes. Document provider
selection, capability errors, state migration, transcript storage, credential
delivery, failed-continuation recovery and operational probes. Keep root
compatibility-pointer documents as pointers. Add `read_when` routing where
needed and update API/SDK definitions if the public contract changes.

Implementation validation must include the repository's normal Python checks,
OpenAPI generation and documentation validation, plus backend contract tests
and the relevant real-runtime canaries. Discover exact commands from the live
repository rather than treating this plan as a frozen toolchain prescription.
Pin dependencies in the repository's existing uv workflow after their release
activity and compatibility have been checked.

Completion means the supported Claude modes pass their recorded gates on the
exact built revision, Codex regressions pass, migration preserves existing
data, and documented failure paths match behavior. Report local implementation,
tests, CI, image publication and deployment separately. Update this plan and
its index with actual scope, evidence and remaining limitations.

## Decisions still open

- Does the target product need the Claude Code harness, or would Messages API
  access satisfy its immediate requirement?
- Which native SDK event sequence can seal the complete handoff, and what
  pinned timeout/backgrounding configuration makes waiting safe?
- Which client-supplied histories and restart cases can be supported faithfully
  without an equivalent to Codex history injection?
- How should durable result-delivery journaling represent an uncertain outcome
  without implying exactly-once execution?
- Can Claude's supported controls satisfy credential and default-deny workspace
  policies, or does its worker need additional OS isolation or a sampling proxy?
- Which approval and model-discovery capabilities can be exposed accurately?

Resolve these from experiments and explicit product needs. Avoid calendar or
effort commitments before the feasibility results establish the actual work.

## Sources and review notes

Official references inspected on 21 October 2026; recheck the versions and
behavior before implementation:

- [Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview):
  harness and authentication scope.
- [Python SDK reference](https://code.claude.com/docs/en/agent-sdk/python):
  environment inheritance, settings sources, tools, interrupts and sandbox
  controls. These controls are version-sensitive.
- [Sessions](https://code.claude.com/docs/en/agent-sdk/sessions): exact resume,
  local transcripts, configuration directories and session-store options.
- [Custom tools](https://code.claude.com/docs/en/agent-sdk/custom-tools): native
  SDK MCP handlers and schemas.
- [Permissions](https://code.claude.com/docs/en/agent-sdk/permissions): approval
  evaluation order and callback limitations.
- [MCP backgrounding](https://code.claude.com/docs/en/mcp#automatic-backgrounding-of-long-tool-calls):
  configuration that can affect pending tool execution.
- [Secure deployment](https://code.claude.com/docs/en/agent-sdk/secure-deployment):
  isolation, credential injection and network controls.
- [SDK use with Claude plans](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan):
  subscription and billing guidance; distinguish it from third-party login.
- [Messages API tool lifecycle](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls):
  the simpler model-and-functions alternative.

Astra reviewed the architecture proposal and recommended the execution/segment
distinction, explicit loss semantics, credential-delivery checks and a
feasibility-first sequence. That was a design review, not live SDK validation.
The Claude Sonnet editorial pass was unavailable because the CLI reported that
it was not logged in earlier in this conversation; no successful editorial
review is claimed.
