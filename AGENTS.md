# Codex Broker Agent Guidance

## Reassess isolation before accepting untrusted executable tools

read_when: adding MCP servers, plugins, hooks, user-configurable tool execution, full-access workloads, or changing runtime isolation.

The shared-container design assumes trusted host backends and operator-reviewed
App Server and MCP executables. Managed command sandboxing does not contain a
compromised App Server or a malicious MCP executable.

Before enabling user-supplied executable MCP servers, plugins, hooks, or
unsandboxed workloads for unrelated owners, revisit and verify runtime
isolation. Keep tool authorization and credential protection even if containers
are introduced. See the
[architecture trust assumptions](fern/docs/pages/runtime/architecture.mdx#untrusted-executable-tools-require-stronger-runtime-isolation)
for the risks and criteria; this note does not schedule a container migration.
