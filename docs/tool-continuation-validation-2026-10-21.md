---
read_when: enabling OpenAI tool continuation or comparing its Pearwrite latency and limits.
---

# Pearwrite tool continuation results

Implementation: `4fe8de8`. The change is limited to the OpenAI-compatible
caller-tool path. It reuses existing broker threads, logical turn records,
scheduler gates, App Server contexts and leases. Native conversations retain
their existing lifecycle. No Pearwrite code or live document was changed by
this task; the original broker was not redeployed.

Batching and continuation are independent. Dispatched callbacks are collected
before handoff, while `CODEX_BROKER_OPENAI_TOOL_CONTINUATIONS=true` enables
retention of the suspended upstream turn. At the time of these benchmarks,
continuation was opt-in. It is now enabled by default for eligible unbundled
requests; set `CODEX_BROKER_OPENAI_TOOL_CONTINUATIONS=false` to opt out.
The live models still dispatched one caller function per response in every
benchmark. The measured improvement came from continuation, not live batching.
Fake-runtime regressions verify simultaneous callbacks and five raw calls,
including dispatch before a model response has finished listing its calls.

## Five runs per model and configuration

Pearwrite's real `demo-many-changes` evaluation ran through the unchanged
broker on port 3400 and an isolated, hardened canary on port 3410. Each trial
used a throwaway database and the same synthetic roof-garden proposal, source
instructions and tools. The canary retained read-only container settings,
no-new-privileges, the shipped seccomp profile and required sandbox preflight.
Both used Codex 0.163.0 and the Pearwrite binding/account. The five-run cohorts
were collected during implementation; separate smoke tests checked the committed
code after the lifecycle safeguards were finalized.

“Task time” is the sum of client-measured model-request durations, including
HTTP and broker overhead; it excludes Pearwrite's final five-second settling
wait. p95 uses nearest rank: with five task samples, it is the slowest run.

| Model | Median task seconds, before → after | p95 task seconds, before → after | Fully correct trials, before → after |
| --- | ---: | ---: | ---: |
| gpt-6.1-sol | 101.669 → 19.488 | 104.655 → 22.598 | 5/5 → 5/5 |
| gpt-6-luna | 83.811 → 25.420 | 106.863 → 28.723 | 2/5 → 5/5 |
| gpt-5.6-sol | 87.215 → 27.443 | 167.805 → 222.837 | 5/5 → 5/5 |

All fifteen continuation trials passed the four checks: meeting date, chair,
rail price and revised total, and funding gap. Luna's baseline passed only
14 of 20 individual checks. Its two fully correct baseline trials had a median
of 99.631 seconds; the table deliberately retains the failed runs rather than
silently dropping them. These samples do not establish a general quality gain.

| Model | Median HTTP-request seconds, before → after | p95 HTTP-request seconds, before → after | Calls per tool-bearing answer, before → after |
| --- | ---: | ---: | ---: |
| gpt-6.1-sol | 9.396 → 0.117 | 13.736 → 11.974 | 1 → 1 |
| gpt-6-luna | 4.037 → 0.846 | 8.651 → 3.122 | 1 → 1 |
| gpt-5.6-sol | 6.917 → 0.100 | 13.080 → 13.127 | 1 → 1 |

The 5.6 continuation outlier spent 207.534 seconds in its first fresh
`read_document` request, before any continuation. Its later script-only
follow-ups took 86–345 ms. Retaining execution does not remove slow initial
model requests, and the observed task p95 for 5.6 became worse, not better.

## Individual results

Each cell lists trials 1–5 in order. Times are seconds; request counts include
the final text answer.

| Model / configuration | Task times | HTTP requests | Output tokens |
| --- | --- | --- | --- |
| 6.1 before | 104.025, 97.654, 104.655, 101.669, 77.557 | 11, 12, 11, 11, 10 | 3399, 2602, 3233, 3232, 1963 |
| 6.1 after | 19.488, 22.598, 20.827, 18.409, 17.427 | 10, 10, 13, 11, 10 | 486, 559, 704, 421, 389 |
| Luna before | 50.751, 83.811, 92.398, 106.863, 75.079 | 9, 19, 23, 21, 17 | 1087, 3670, 2165, 2888, 1783 |
| Luna after | 25.420, 19.229, 26.900, 28.723, 15.332 | 15, 22, 27, 17, 20 | 1076, 1433, 2362, 1963, 882 |
| 5.6 before | 66.871, 87.215, 66.990, 167.805, 92.663 | 10, 12, 10, 16, 12 | 3594, 4135, 3414, 3944, 3707 |
| 5.6 after | 24.725, 27.443, 29.452, 222.837, 27.427 | 11, 11, 10, 10, 11 | 1035, 1259, 1314, 1020, 1419 |

Sol 6.1 had 7, 7, 10, 8 and 7 zero-usage requests in its continuation trials;
5.6 had 7, 7, 6, 6 and 7. These advanced an already-generated script without
another model generation. Luna sometimes generated another model response for
each call instead, so retention does not guarantee zero-token follow-ups.

## Evidence and limits

The six evaluation directories under the task's temporary
`/tmp/broker-tool-investigation/pearwrite-{3400,3410}/` retain summaries,
transcripts, before/after HTML, replies and tool calls:

- 6.1 before/after: `2026-10-21T11-29-15-custom_gpt-6.1-sol` /
  `2026-10-21T11-29-16-custom_gpt-6.1-sol`.
- Luna before/after: `2026-10-21T11-39-41-custom_gpt-6-luna` /
  `2026-10-21T11-34-19-custom_gpt-6-luna`.
- 5.6 before/after: `2026-10-21T11-49-44-custom_gpt-5.6-sol` /
  `2026-10-21T11-39-40-custom_gpt-5.6-sol`.

The Pearwrite checkout changed independently during this work, so its reported
end-of-run Git IDs differ. Saved instructions and tool definitions nevertheless
match across all thirty trials after normalizing only generated document IDs:
SHA-256 `318176d3ca1a48398265c5c3e33e33b6d26fac7af099ac1f03946ef1018c23ae`.
Every initial HTML document has SHA-256
`4c3ddde667e51f196f894484286535775801a08f7e0785ac2edec905a173c27e`.

Runs were sequential within each broker, with baseline and canary cohorts
sometimes overlapping. Order was not randomized, upstream cache state was not
controlled, and reasoning/service-tier settings used the same app defaults.
Five trials are too few for a stable tail-latency estimate. Setup, replay,
queue and interrupt phases were not separately timed; no sustained production
load test was performed. Capacity, abandonment and cleanup were exercised by
regression tests. This is local canary evidence, not a production rollout.

Smoke tests on committed `4fe8de8` passed all four document checks on all three
models. Each scenario reused one broker thread and one upstream Codex turn,
while assigning each HTTP response a separate broker turn:

| Model | HTTP requests | New model responses |
| --- | ---: | ---: |
| gpt-6.1-sol | 13 | 3 |
| gpt-6-luna | 13 | 5 |
| gpt-5.6-sol | 10 | 4 |

The runtime records new model-response counts alongside per-segment usage,
so script continuation is distinguishable from generation. These committed-code
smokes are separate from the five-run timing cohorts. Their identity and usage
evidence is in `/tmp/broker-tool-investigation/committed-canary-evidence.json`.
The temporary canary was stopped and removed after verification.

Validation: 349 unit tests passed with ResourceWarning treated as an error;
OpenAPI regeneration produced no contract diff; Fern checks and wheel/sdist
builds passed. Coverage includes independent batching with retention disabled,
sequential/raw callbacks, cross-owner isolation, native steering protection,
retry conflicts, ambiguous delivery, partial results, cancellation, expiry,
revocation, pool eviction, restart replay and failed handoff persistence.

The earlier required Claude editorial attempt returned “Not logged in.” No
Claude review was completed; documentation was reviewed directly.
