---
read_when: investigating OpenAI-compatible function-call latency, code mode, or caller-tool isolation.
---

# Caller-tool investigation, 21 October 2026

This is the initial investigation, before implementation. The subsequent
[continuation implementation and measurements](tool-continuation-validation-2026-10-21.md)
are recorded separately.

The broker returns only the first nested call even when the model has already
written a script containing many independent calls. No performance change was
shipped: configuration experiments did not produce a multi-call raw response.
The next step is the [thread-reuse proposal](../plans/004-retain-openai-tool-threads.md),
which requires review before implementation.

## What the live requests showed

Tests used the existing local Pearwrite binding (`inplace-cloud`, sign-in
`scenare`), port 3400, and Codex 0.163.0. The model cache advertised
`tool_mode: code_mode_only` and `use_responses_lite: true` for all three models.
In the pinned upstream source, `core/src/tools/mod.rs::requested_tool_mode`
selects catalog metadata before the feature flags. Setting `features.code_mode`
to false therefore does not disable catalog-selected code mode. In
`core/src/client.rs`, `parallel_tool_calls` is additionally constrained by
`!model_info.use_responses_lite`. The model configuration overrides in
`models-manager/src/model_info.rs` do not provide overrides for either field.
Source checkout: upstream tag `rust-v0.163.0`, commit
`83086188ae0614bd882818ff1aa23f3194962012`.

Separate diagnostic App Servers tested `features.code_mode.enabled: false`,
`features.code_mode.direct_only_tool_namespaces: ["functions"]`, and
`features.code_mode_only_strict_3p_tools: false`. The namespace setting did
remove the caller function from exec: the first script failed with
`TypeError: tools.replace_text is not a function`. The model then emitted a
raw `function_call`, but only one, despite a prompt requesting five independent
replacements together. Code mode itself remained available. This is not a
working batching fix; neither the catalog nor the running broker was changed.

The real broker events contain `custom_tool_call` items named `exec`, followed
by `item/tool/call` requests. In baseline turn
`turn_tFj8UUffS2wDbKSrO4hcLtdB`, the script contained eight replacements in
`Promise.all` plus two subsequent comments. The broker's
`handle_dynamic_tool_request` stopped collection at the first nested call.
The whole script was generated and counted, although the client received just
one replacement. That response reported 604 output tokens, including 27
reasoning tokens. This explains the otherwise invisible output; it is not
600 tokens of reasoning per tiny edit.

## Timings and caching

We replayed the saved synthetic `demo-many-changes` request immediately after
`read_document`: 20,240 characters of instructions, 13 tools,
user request and HTML from Pearwrite's 08:02:52 evaluation. Sections were
joined with blank lines. This reproduces the edit-step content, not necessarily
pi-ai's exact system-message serialization. Returned tools were not executed;
no project documents were changed. Times include the HTTP round trip.

| Model | First run: calls / seconds | Repeat: calls / seconds | First / repeat tool |
|---|---:|---:|---|
| gpt-6.1-sol | 1 / 10.691 | 1 / 10.112 | replace_element / find_replace |
| gpt-6-luna | 1 / 3.502 | 1 / 3.770 | read_source / read_source |
| gpt-5.6-sol | 1 / 10.980 | 1 / 3.906 | replace_element / read_project_document |

These are two unchanged-runtime samples, **not before/after improvement
measurements** or complete editing sessions. Model choices differ, particularly
for 5.6, so the shorter repeat does not establish a speedup. There is no
implemented performance change to measure “after.” The original user-supplied
12-request, 145-second run was not rerun end to end.

The 6.1 repeat reported 12,672 cached tokens out of 12,912 input tokens; its
first run reported zero. Luna and 5.6 reported zero cached tokens in both
samples. Fresh threads therefore do not categorically prevent prompt caching,
but cache hits are not guaranteed. The original three baseline turns took
about 22, 59 and 32 ms respectively from `tool.started` to broker
`turn.completed`; these observations do not support a fixed five-second
handoff cost. The code does contain a five-second interrupt wait ceiling.
Thread setup, queue time and model generation were not separately instrumented.

For correlation with retained broker events, first/repeat response IDs are
`tFj8UUffS2wDbKSrO4hcLtdB` / `yRaAciCmKJtLdjmuwbK9FXF` (6.1),
`tlZ5CB4l5ASBUW6Xg1J0e` / `sb7hAESWi62fytXqnGLwkcqH` (Luna), and
`qWkjyzKTt1Gif5KnIrlzPJiq` / `1gFlvaBlzz6KZosCyxXJoH9` (5.6).
Prefix with `chatcmpl_` for HTTP responses or `turn_` for broker turns.

## Isolation evidence and limits

Three live probes declared only a caller-defined `report` function. Broker raw
items recorded the executed scripts and their actual outputs, not just model
assertions. They attempted directory listings, `/etc/hostname`, environment
access, `https://example.com`, alternate globals, imports and tool dispatch.

- `process`, `require`, `fetch`, `Deno`, `Bun`, `XMLHttpRequest` and `WebSocket`
  were unavailable where tested. `Function('return typeof process')()` also
  returned `undefined`.
- Imports of `node:fs`, `file:///etc/hostname` and `https://example.com` failed
  with `unsupported import in exec`. Shell and file tools were not functions.
- MCP resource discovery returned empty lists; a file-URI read against an
  unconfigured server failed with `unknown MCP server 'diagnostic'`.
- No file contents, environment values or network response were obtained.
  The 5.6 probe declined the `/data/state` listing because of policy, so that
  particular attempt is not evidence of an enforced filesystem denial.
  Credentials were never requested or read.

The enabled bridge was not literally caller-functions-only: it also contained
MCP-resource helpers, and the 6.x probes included a clock helper. No configured
MCP resources were reachable on this binding. Do not generalize that observation
to bindings with other server configurations.

A live `/proc` inspection found `codex-code-mode-host` running as UID 1000 with
cwd equal to the sign-in's Codex home, root `/`, `NoNewPrivs: 1`, and
`Seccomp: 2`. Its environment names were `CODEX_CREDENTIAL_STORE`, `CODEX_HOME`,
`HOME`, `LANG`, and `PATH`. The upstream launcher spawns it directly; it does
not wrap it in the command sandbox. The V8 runtime installs a restricted set
of globals and rejects imports. This is language-runtime isolation inside a
trusted executable, not an OS sandbox preventing the host executable from
accessing broker files. The probes do not prove resistance to a V8/native-host
exploit. Probe turn suffixes are `ZSOqo1qHFbSqLULanFq3EmEY` (6.1),
`WVCxPHeN5G2J8XBDmMpgCk2q` (Luna), and `2at46U7m8oSAu6dAzPpEU4N` (5.6).
No script-accessible escape was found, so the requested security stop
condition was not triggered.

## Changes and verification

The compatibility regression now checks every isolation config setting at the
fake App Server's JSON-RPC input, alongside empty thread and turn environments.
An additional regression checks five raw calls in one answer; existing tests
cover two calls, streaming, and the single-call code-mode fallback. Comments
and the spec now describe the measured
limitations. No request behavior, deployment, credentials or model catalog was
changed. All 323 unit tests passed with `ResourceWarning` treated as an error;
`git diff --check` passed. Image builds, deployment and CI were intentionally
not run for this tests-and-documentation change. The required Claude editorial
attempt returned “Not logged in”; no Claude review was completed.
