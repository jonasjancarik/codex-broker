# Codex App-Server Modes

read_when: Changing Codex app-server protocol handling, model discovery, service tiers, normalized events, approvals, plan mode, goal tracking, review flows, user input, or MCP elicitation support.

This matrix is pinned to the Codex CLI app-server protocol generated from `codex-cli 0.144.3`:

```bash
codex app-server generate-ts --out /private/tmp/codex-app-server-ts-0.144.3
codex app-server generate-json-schema --out /private/tmp/codex-app-server-json-schema-0.144.3
```

The generated protocol is unchanged from `0.144.0` to `0.144.3` for every surface in this matrix.

The app-server protocol only exposes two collaboration mode kinds in `ModeKind`: `default` and `plan`. Goal tracking, review, approvals, user-input prompts, and MCP elicitations are separate app-server capabilities, not additional `ModeKind` values.

| Capability | 0.144.3 app-server surface | Broker support | Host-facing surface |
| --- | --- | --- | --- |
| Model and selector capabilities | `model/list`, including reasoning efforts, input modalities, personality support, service tiers, defaults, visibility, and upgrade metadata | Implemented with auth-principal/profile scoping and pagination | `GET /v1/owners/{ownerId}/auth/models` |
| Default chat/work turns | `thread/start`, `thread/resume`, `turn/start`, `turn/steer`, `turn/interrupt` | Implemented | Thread and turn HTTP APIs, `message.*`, `reasoning.*`, `tool.*`, `turn.*` events |
| Plan collaboration mode | `ModeKind = "default" \| "plan"`, `ThreadSettings.collaborationMode`, `turn/plan/updated`, `item/plan/delta` | Event normalization implemented | `thread.settings.updated`, `plan.updated`, `plan.delta` events |
| Goal tracking | `thread/goal/set`, `thread/goal/get`, `thread/goal/clear`, `thread/goal/updated`, `thread/goal/cleared` | Event normalization implemented; direct broker goal API not yet implemented | `goal.updated`, `goal.cleared` events |
| Review mode | `review/start`, `enteredReviewMode`, `exitedReviewMode`, auto-approval review notifications | Event normalization implemented; direct broker review API not yet implemented | `review.entered`, `review.exited`, `approval.review.started`, `approval.review.completed` events |
| Tool/file/permission approvals | `item/commandExecution/requestApproval`, `item/fileChange/requestApproval`, `item/permissions/requestApproval`, legacy `applyPatchApproval`, `execCommandApproval` | Implemented with pending interaction storage, host resolve API, and safe fallback responses. The fallback declines command/file requests, denies legacy requests, and grants no extra permissions for permission-profile requests. | `tool.requested`, `approval.requested`, `approval.resolved`, interaction APIs, owner audit entries |
| User input prompts | `item/tool/requestUserInput` | Implemented with pending interaction storage, host resolve API, and safe fallback empty answers | `user_input.requested`, `user_input.resolved`, interaction APIs |
| MCP elicitations | `mcpServer/elicitation/request`; initialize capability `mcpServerOpenaiFormElicitation` | Implemented with pending interaction storage, host resolve API, and safe fallback decline | `mcp.elicitation.requested`, `mcp.elicitation.resolved`, interaction APIs |

## API Direction

The broker treats app-server mode-like behavior as observable event state unless there is already a stable product-facing API. Model discovery is a direct, stable mapping of `model/list`; the response keeps each model object forward-compatible so new advertised capabilities are not dropped.

Future product APIs should follow these boundaries:

- Plan mode: expose current collaboration state and plan updates from events. Add a setter only if a generated app-server method appears for updating `ThreadSettings.collaborationMode`, or if the broker implements a supported Codex command path for switching modes.
- Model catalog: keep discovery scoped to trusted owner-to-principal policy and the selected auth profile. Preserve pagination and all advertised model fields, including Fast and future service tiers.
- Goal tracking: add `POST /v1/owners/{ownerId}/threads/{threadId}/goal`, `GET .../goal`, and `DELETE .../goal` around app-server `thread/goal/*` once the broker can safely run these methods against a loaded Codex thread outside an active turn.
- Review: add `POST /v1/owners/{ownerId}/threads/{threadId}/reviews` around `review/start` once the response and detached/inline delivery semantics are mapped to broker turn state.
- Approvals, user input, and MCP elicitations: keep the pending interaction records, resolve API, and fallback response shapes in sync with generated app-server response schemas. The broker must always fall back on timeout or child loss so an app-server child is never left waiting indefinitely for a host UI.

When updating this file for a new Codex version, regenerate the TypeScript and JSON schema artifacts first and compare `ModeKind`, `ClientRequest`, `ServerRequest`, and the response types for every row above.
