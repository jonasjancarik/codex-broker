# Example Bundles

This directory contains mounted host bundles used by `examples/docker-compose.yml`.

Expected layout:

```text
examples/bundles/
  example-chat-v1/
    bundle.json
    skills/example-evidence/SKILL.md
  document-jobs-v1/
    bundle.json
    skills/normalize-citations/SKILL.md
```

Each bundle can declare host-owned app-specific tool semantics through mounted MCP servers or broker-hosted HTTP adapters. The broker validates and exposes those declarations; it does not implement product-specific search or report logic. Hosted adapters may include declared headers and opaque context for host-side authorization and data lookup. Hosted adapter URLs must match `CODEX_BROKER_ALLOWED_HOSTED_TOOL_URL_PREFIXES` by parsed scheme and host, with optional explicit port and path-prefix restrictions; secret-looking headers must use `env:VAR` indirection.

The chat example declares a broker-hosted `host.evidence.search` adapter that forwards to `http://app:3000/internal/codex/tools/evidence-search`. Set `CODEX_HOST_TOOL_KEY` in the broker process environment when enabling that bundle, and set the same key in the host app. The host endpoint remains responsible for product authorization, evidence lookup, data models, and result semantics.

The document-job example declares a mounted normalization skill and workspace policy only. The host app keeps job records, queue semantics, review rows, and generated artifacts.

Bundles can also declare mounted prompt files under `prompts`. The broker validates those paths, links them into the per-turn overlay, and injects their text before the host turn input. Prefer skills for stable workflows; use prompts for legacy instructions that still need direct text injection.
