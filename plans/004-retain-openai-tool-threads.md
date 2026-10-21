---
read_when: implementing continuity for OpenAI-compatible caller-tool requests.
---

# Batch and continue OpenAI-compatible caller tools

Status: APPROVED — implementation authorized on 21 October 2026.

The [live investigation](../docs/tool-call-investigation-2026-10-21.md) found
that models can generate several edits in one exec script, but the stateless
handoff interrupts that script at its first caller function. Retaining it would
let the next HTTP request supply the real result and continue execution without
rebuilding the thread or regenerating the remaining script. It does not by
itself promise several calls in every HTTP response.

## Scope and two separate issues

Native broker thread creation, thread/resume and multi-turn conversations already
work and retain their existing contract. Change only the OpenAI-compatible
adapter and its caller-tool handoff. Reuse the existing broker thread IDs,
logical turn records, scheduler gates, App Server contexts and pool leases.
Do not add a second conversation service or require Pearwrite-specific fields.

1. Batching: collect caller functions that have actually been dispatched before
   returning the HTTP response. Never execute or parse a model script to predict
   calls. Codex may serialize dispatch; dependent or later calls remain for the
   next response. This can reduce HTTP requests independently of thread reuse.
2. Continuation: retain the same upstream turn and suspended exec script after
   handoff. A matching HTTP follow-up becomes a new broker response/turn record
   on the existing broker thread, and supplies the real outputs to the waiting
   upstream requests. It must not call thread/start, inject_items or turn/start.
   This avoids regeneration even when only one call can be dispatched at a time.

## Matching and resuming

Start with unbundled, isolated caller-function bindings. Keep an in-memory
continuation record containing the App Server lease, upstream thread/turn IDs,
pending JSON-RPC request IDs, public call IDs, completed response identity,
canonical transcript digest, and effective configuration fingerprint. Retain
raw reasoning and exec state only in the upstream thread; never expose them in
OpenAI history or use sanitized diagnostic events as executable state.

Scope lookup to the authenticated binding, owner, principal, profile instance
and process generation. A public call ID or `previous_response_id` is a lookup
hint, never authorization. Match the full canonical transcript through the
returned assistant response and require precisely the pending tool outputs,
with unique call IDs. Match tool schemas/order, instructions, model, effort,
service tier and effective policy as well. Responses requests using
`previous_response_id` must resolve the same owner-scoped canonical history
before comparison. Changed history, changed settings, an unknown call, or an
expired record uses today's validated replay path. Preserve current malformed-
history rejection; do not turn invalid requests into successful fallbacks.

Use an atomic waiting-to-resuming transition so two follow-ups cannot deliver
a result twice. Cache the accepted follow-up digest and its public response:
identical retries attach to the same work or receive that response; competing
payloads cannot both resume it. Do not silently replay after ambiguous delivery
to a live thread. First establish whether results were accepted; otherwise
return a retryable error with no second execution. A deliberate edited-history
fork must not cancel another request already resuming the original thread.

On an exact match, answer the held `item/tool/call` request with the client's
output and attach a new HTTP response collector to the existing upstream turn.
Do not inject history or start another turn. Map upstream events and usage to
the current HTTP request, avoiding duplicated text or charges for the script
already generated. A script can produce more pending calls without a new model
response; return those without waiting for a nonexistent `rawResponse/completed`.
Define zero/new-generation usage semantics explicitly for these continuations.
Keep ordered, already-dispatched calls available for subsequent handoffs; never
invent success results to make a script advance. Batch calls only when their
actual dispatch establishes independence, not by parsing or rewriting JS.

## Lifetime and capacity

Proposed starting limits: five minutes waiting idle, thirty minutes absolute
chain age, one retained thread per owner and two globally, further constrained
by App Server capacity. Make limits configurable. Account waiting threads
separately from active inference, but pin their pool lease and reserve capacity
so ordinary idle eviction cannot destroy them. Recheck admission on resume.
At capacity, use the existing stateless handoff for new work; do not evict an
actively resuming chain. These are concurrency limits, not CPU/memory budgets.

On timeout, binding/profile revocation, client cancellation or shutdown,
interrupt the owning turn, resolve/reject pending transport requests as
cancelled, unregister collectors and release its lease exactly once. A failed
interrupt must not return a busy client to the pool. Use the existing retention
policy for stored transcript artifacts rather than keeping them indefinitely.
Restart discards all live continuation records and changes the generation ID;
valid client histories then replay normally. Never pretend a persisted record
can restore a lost exec isolate. Multi-process deployments need sticky routing
or should treat another process's calls as replay misses in this first version.

This reverses the spec's “abandoned follow-ups need no timeout or cleanup”
trade-off: continuity now consumes live state, needs cancellation and eviction,
and becomes unavailable across restarts. Preserve replay as a tested fallback.

## Implementation and measurement gates

First prove, against pinned Codex, delayed dynamic-tool replies, continuation
of the same exec cell, interrupt completion, and multiple callbacks from one
script. Then add the continuation registry and HTTP collector lifecycle behind
an operator opt-in flag. Tests must cover owner isolation, edited histories,
changed tools/settings, duplicate/conflicting retries, partial batches,
stream disconnects, timeout, capacity pressure, revocation, pool eviction and
restart. Update the spec before enabling the flag.

Benchmark the full synthetic Pearwrite scenario on all three requested models,
using identical input, tool outputs, effort and service tier, both cold and
warm. Run at least five paired trials per model. Record calls per response,
HTTP/model-request counts, time to each tool result and final answer, p50/p95
latency, setup/replay/queue/interrupt timings, cached/input/output/reasoning
tokens and waiting-thread occupancy. Validate every intended document edit and
comment, not just speed. Compare retained-thread hits with forced replay and
abandoned-chain load. Acceptance requires correct outputs, safe fallback and a
repeatable end-to-end gain; do not prescribe a speedup before measuring it.

Implementation is authorized; record validation and remaining limitations here.
