from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import ParseResult, urlparse

from .config import BrokerConfig
from .state import StateStore
from .util import SECRET_KEY_PATTERN, ensure_dir, is_relative_to, json_dumps, random_id


class BundleError(ValueError):
    pass


@dataclass(frozen=True)
class SkillRef:
    name: str
    path: Path


@dataclass(frozen=True)
class PromptRef:
    name: str
    path: Path


@dataclass(frozen=True)
class McpServerRef:
    name: str
    command: str
    args: tuple[str, ...]
    env: dict[str, str]
    cwd: Path | None = None


@dataclass(frozen=True)
class HostedToolRef:
    name: str
    description: str
    input_schema: dict[str, Any]
    endpoint: str
    timeout_seconds: float
    headers: dict[str, str]
    context: dict[str, Any]
    network_policy: dict[str, Any]
    approval_policy: str
    scope: str


@dataclass(frozen=True)
class ResolvedBundle:
    bundle_id: str
    version: str | None
    instructions: tuple[str, ...]
    skills: tuple[SkillRef, ...]
    prompts: tuple[PromptRef, ...]
    mcp_servers: tuple[McpServerRef, ...]
    hosted_tools: tuple[HostedToolRef, ...]
    allowed_paths: tuple[Path, ...]
    sandbox_mode: str | None
    source: str
    path: Path
    digest: str


class BundleRegistry:
    def __init__(self, config: BrokerConfig, state: StateStore) -> None:
        self.config = config
        self.state = state
        ensure_dir(config.inline_bundle_root)
        ensure_dir(config.overlay_root)

    def resolve(self, bundle_id: str | None) -> ResolvedBundle | None:
        if not bundle_id:
            return None
        bundle_path = self._find_mounted_bundle(bundle_id)
        source = "mount"
        if not bundle_path:
            record = self.state.get_bundle_record(bundle_id)
            if record and record.get("source") == "inline":
                path = Path(str(record.get("path") or "")).expanduser().resolve()
                if is_relative_to(path, self.config.inline_bundle_root) and path.is_file():
                    bundle_path = path
                    source = "inline"
        if not bundle_path:
            raise BundleError(f"Unknown bundle: {bundle_id}")
        raw = bundle_path.read_text(encoding="utf-8")
        payload = json.loads(raw)
        bundle = self._parse(payload, bundle_path, source, raw)
        self.state.record_bundle(bundle.bundle_id, bundle.digest, bundle.source, str(bundle.path))
        return bundle

    def accept_inline(self, payload: dict[str, Any]) -> ResolvedBundle:
        if not self.config.enable_inline_bundles:
            raise BundleError("Inline bundles are disabled.")
        raw = json_dumps(payload)
        if len(raw.encode("utf-8")) > self.config.inline_bundle_max_bytes:
            raise BundleError("Inline bundle exceeds size limit.")
        digest = hashlib.sha256(json_dumps(payload).encode("utf-8")).hexdigest()
        bundle_dir = self.config.inline_bundle_root / digest
        bundle_path = bundle_dir / "bundle.json"
        bundle = self._parse(payload, bundle_path, "inline", raw)
        existing = self.state.get_bundle_record(bundle.bundle_id)
        if self._find_mounted_bundle(bundle.bundle_id):
            raise BundleError(f"Inline bundle id conflicts with mounted bundle: {bundle.bundle_id}")
        if existing and (existing.get("source") != "inline" or existing.get("digest") != digest):
            raise BundleError(f"Inline bundle id already exists with a different digest: {bundle.bundle_id}")
        ensure_dir(bundle_dir)
        if not bundle_path.exists():
            bundle_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.state.record_bundle(bundle.bundle_id, bundle.digest, bundle.source, str(bundle.path))
        return bundle

    def materialize(
        self,
        bundle: ResolvedBundle | None,
        turn_id: str,
        adapter_context: dict[str, Any] | None = None,
    ) -> Path | None:
        if bundle is None:
            return None
        overlay = ensure_dir(self.config.overlay_root / turn_id)
        skills_root = ensure_dir(overlay / ".agents" / "skills")
        for skill in bundle.skills:
            target = skills_root / re.sub(r"[^A-Za-z0-9_.-]", "_", skill.name)
            if target.exists() or target.is_symlink():
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            target.symlink_to(skill.path.parent, target_is_directory=True)
        if bundle.prompts:
            prompts_root = ensure_dir(overlay / "prompts")
            for prompt in bundle.prompts:
                suffix = prompt.path.suffix or ".txt"
                safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", prompt.name)
                target = prompts_root / f"{safe_name}{suffix}"
                if target.exists() or target.is_symlink():
                    if target.is_dir() and not target.is_symlink():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                target.symlink_to(prompt.path)
        if bundle.instructions:
            (overlay / "AGENTS.md").write_text("\n\n".join(bundle.instructions), encoding="utf-8")
        if bundle.hosted_tools:
            adapter_config = {
                "tools": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "inputSchema": tool.input_schema,
                        "endpoint": tool.endpoint,
                        "timeoutSeconds": tool.timeout_seconds,
                        "headers": tool.headers,
                        "context": tool.context,
                        "networkPolicy": tool.network_policy,
                        "approvalPolicy": tool.approval_policy,
                        "scope": tool.scope,
                    }
                    for tool in bundle.hosted_tools
                ],
                "brokerContext": adapter_context or {},
            }
            (overlay / "tool-adapters.json").write_text(
                json.dumps(adapter_config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        mcp_servers = self.mcp_servers_for_bundle(bundle, overlay)
        if mcp_servers:
            codex_dir = ensure_dir(overlay / ".codex")
            (codex_dir / "config.toml").write_text(self._mcp_config_toml(mcp_servers), encoding="utf-8")
        return overlay

    def mcp_servers_for_bundle(self, bundle: ResolvedBundle, overlay: Path | None = None) -> tuple[McpServerRef, ...]:
        servers = list(bundle.mcp_servers)
        if bundle.hosted_tools:
            if overlay is None:
                overlay = ensure_dir(self.config.overlay_root / f"adapter-{bundle.digest[:16]}-{random_id('tmp')}")
            config_path = overlay / "tool-adapters.json"
            servers.append(
                McpServerRef(
                    name=f"broker_hosted_{bundle.digest[:12]}",
                    command=sys.executable,
                    args=("-m", "codex_broker.tool_adapter_mcp", str(config_path)),
                    env={},
                    cwd=None,
                )
            )
        return tuple(servers)

    def cleanup_overlay(self, turn_id: str) -> None:
        path = self.config.overlay_root / turn_id
        if path.exists():
            shutil.rmtree(path)

    def validate_cwd(self, cwd: str | None, bundle: ResolvedBundle | None = None) -> Path | None:
        if not cwd:
            return None
        path = Path(cwd).expanduser().resolve()
        allowed = [*self.config.allowed_workspace_roots, self.config.overlay_root]
        if bundle:
            allowed.extend(bundle.allowed_paths)
        if not any(is_relative_to(path, root) for root in allowed):
            raise BundleError(f"cwd is outside allowed workspace roots: {path}")
        return path

    def _find_mounted_bundle(self, bundle_id: str) -> Path | None:
        candidates: list[Path] = []
        for root in self.config.allowed_bundle_roots:
            candidates.extend(
                [
                    root / bundle_id / "bundle.json",
                    root / f"{bundle_id}.json",
                    root / bundle_id,
                ]
            )
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
        for root in self.config.allowed_bundle_roots:
            if not root.exists():
                continue
            for path in root.rglob("bundle.json"):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if payload.get("id") == bundle_id:
                    return path.resolve()
        return None

    def _parse(self, payload: dict[str, Any], path: Path, source: str, raw: str) -> ResolvedBundle:
        bundle_id = str(payload.get("id") or "").strip()
        if not bundle_id:
            raise BundleError("Bundle id is required.")
        instructions = tuple(str(item) for item in payload.get("instructions") or [])
        allowed_paths = tuple(self._validated_workspace_path(value) for value in payload.get("allowedPaths") or [])
        skills: list[SkillRef] = []
        for entry in payload.get("skills") or []:
            if not isinstance(entry, dict):
                raise BundleError("Skill entries must be objects.")
            name = str(entry.get("name") or "").strip()
            source_info = entry.get("source") if isinstance(entry.get("source"), dict) else {}
            if source_info.get("type") != "mount":
                raise BundleError("Only mounted skill sources are supported in v1.")
            skill_path = Path(str(source_info.get("path") or "")).expanduser().resolve()
            if not any(is_relative_to(skill_path, root) for root in self.config.allowed_bundle_roots):
                raise BundleError(f"Skill path is outside allowed bundle roots: {skill_path}")
            skill_md = skill_path / "SKILL.md" if skill_path.is_dir() else skill_path
            if not skill_md.exists():
                raise BundleError(f"Skill SKILL.md not found: {skill_md}")
            skills.append(SkillRef(name=name or skill_md.parent.name, path=skill_md))
        prompts: list[PromptRef] = []
        for entry in payload.get("prompts") or []:
            if not isinstance(entry, dict):
                raise BundleError("Prompt entries must be objects.")
            name = str(entry.get("name") or "").strip()
            source_info = entry.get("source") if isinstance(entry.get("source"), dict) else entry
            if source_info.get("type", "mount") != "mount":
                raise BundleError("Only mounted prompt sources are supported in v1.")
            prompt_path = Path(str(source_info.get("path") or "")).expanduser().resolve()
            if not any(is_relative_to(prompt_path, root) for root in self.config.allowed_bundle_roots):
                raise BundleError(f"Prompt path is outside allowed bundle roots: {prompt_path}")
            if not prompt_path.is_file():
                raise BundleError(f"Prompt file not found: {prompt_path}")
            prompts.append(PromptRef(name=name or prompt_path.stem, path=prompt_path))
        mcp_servers = tuple(self._parse_mcp_server(entry) for entry in payload.get("mcpServers") or [])
        hosted_tools = tuple(self._parse_hosted_tool(entry) for entry in payload.get("tools") or [])
        sandbox = payload.get("sandbox") if isinstance(payload.get("sandbox"), dict) else {}
        digest = hashlib.sha256(json_dumps(payload).encode("utf-8")).hexdigest()
        return ResolvedBundle(
            bundle_id=bundle_id,
            version=str(payload.get("version")) if payload.get("version") is not None else None,
            instructions=instructions,
            skills=tuple(skills),
            prompts=tuple(prompts),
            mcp_servers=mcp_servers,
            hosted_tools=hosted_tools,
            allowed_paths=allowed_paths,
            sandbox_mode=str(sandbox.get("mode")) if sandbox.get("mode") else None,
            source=source,
            path=path,
            digest=digest,
        )

    def _validated_workspace_path(self, value: str) -> Path:
        path = Path(str(value)).expanduser().resolve()
        if not any(is_relative_to(path, root) for root in self.config.allowed_workspace_roots):
            raise BundleError(f"Bundle allowed path is outside broker allowlist: {path}")
        return path

    def _parse_mcp_server(self, entry: Any) -> McpServerRef:
        if not isinstance(entry, dict):
            raise BundleError("MCP server entries must be objects.")
        name = str(entry.get("name") or "").strip()
        command = str(entry.get("command") or "").strip()
        if not name or not command:
            raise BundleError("MCP server name and command are required.")
        self._validate_command(command)
        args = tuple(str(arg) for arg in entry.get("args") or [])
        env = self._parse_mcp_env(entry.get("env") if isinstance(entry.get("env"), dict) else {})
        cwd_value = entry.get("cwd")
        cwd = Path(str(cwd_value)).expanduser().resolve() if cwd_value else None
        if cwd and not any(is_relative_to(cwd, root) for root in (*self.config.allowed_bundle_roots, *self.config.allowed_workspace_roots)):
            raise BundleError(f"MCP cwd is outside allowed roots: {cwd}")
        return McpServerRef(name=name, command=command, args=args, env=env, cwd=cwd)

    def _parse_mcp_env(self, env: dict[str, Any]) -> dict[str, str]:
        parsed: dict[str, str] = {}
        for key, value in env.items():
            name = str(key)
            text = str(value)
            if not name or any(char in name for char in "\r\n="):
                raise BundleError(f"Invalid MCP env name: {name!r}")
            if any(char in text for char in "\r\n"):
                raise BundleError(f"Invalid MCP env value for {name}.")
            if SECRET_KEY_PATTERN.search(name) and not text.startswith("env:"):
                raise BundleError(f"MCP secret env {name} must use env:VAR indirection.")
            if text.startswith("env:"):
                env_name = text.removeprefix("env:")
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
                    raise BundleError(f"Invalid MCP env indirection for {name}.")
            parsed[name] = text
        return parsed

    def _parse_hosted_tool(self, entry: Any) -> HostedToolRef:
        if not isinstance(entry, dict):
            raise BundleError("Tool entries must be objects.")
        tool_type = str(entry.get("type") or entry.get("adapter") or "broker-hosted")
        if tool_type not in {"broker-hosted", "host-http"}:
            raise BundleError(f"Unsupported tool adapter type: {tool_type}")
        name = str(entry.get("name") or "").strip()
        if not name:
            raise BundleError("Hosted tool name is required.")
        http = entry.get("http") if isinstance(entry.get("http"), dict) else entry
        endpoint = str(http.get("url") or http.get("endpoint") or "").strip()
        if not endpoint.startswith(("http://", "https://")):
            raise BundleError("Hosted tool endpoint must be http:// or https://.")
        matched_network_prefix = self._validate_hosted_tool_endpoint(endpoint)
        input_schema = entry.get("inputSchema") if isinstance(entry.get("inputSchema"), dict) else {"type": "object"}
        headers_raw = http.get("headers") if isinstance(http.get("headers"), dict) else entry.get("headers")
        headers = self._parse_headers(headers_raw if isinstance(headers_raw, dict) else {})
        context = entry.get("context") if isinstance(entry.get("context"), dict) else {}
        policy = entry.get("policy") if isinstance(entry.get("policy"), dict) else {}
        network_policy = self._parse_hosted_network_policy(entry, policy, matched_network_prefix)
        approval_policy = str(policy.get("approval") or entry.get("approval") or entry.get("approvalPolicy") or "never")
        if approval_policy not in {"never", "on-request", "always"}:
            raise BundleError(f"Unsupported hosted tool approval policy: {approval_policy}")
        scope = str(policy.get("scope") or entry.get("scope") or "owner")
        if scope not in {"owner", "profile"}:
            raise BundleError(f"Unsupported hosted tool scope: {scope}")
        return HostedToolRef(
            name=name,
            description=str(entry.get("description") or ""),
            input_schema=input_schema,
            endpoint=endpoint,
            timeout_seconds=float(http.get("timeoutSeconds") or entry.get("timeoutSeconds") or 30),
            headers=headers,
            context=dict(context),
            network_policy=network_policy,
            approval_policy=approval_policy,
            scope=scope,
        )

    def _parse_headers(self, headers: dict[str, Any]) -> dict[str, str]:
        parsed: dict[str, str] = {}
        for key, value in headers.items():
            name = str(key)
            text = str(value)
            if not name or any(char in name for char in "\r\n:"):
                raise BundleError(f"Invalid hosted tool header name: {name!r}")
            if any(char in text for char in "\r\n"):
                raise BundleError(f"Invalid hosted tool header value for {name}.")
            if SECRET_KEY_PATTERN.search(name) and not text.startswith("env:"):
                raise BundleError(f"Hosted tool secret header {name} must use env:VAR indirection.")
            if text.startswith("env:"):
                env_name = text.removeprefix("env:")
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
                    raise BundleError(f"Invalid hosted tool header environment variable for {name}.")
            parsed[name] = text
        return parsed

    def _parse_hosted_network_policy(self, entry: dict[str, Any], policy: dict[str, Any], matched_prefix: str | None) -> dict[str, Any]:
        raw = entry.get("networkPolicy", policy.get("networkPolicy", policy.get("network")))
        if raw is None:
            mode = "host-allowlist"
        elif isinstance(raw, str):
            mode = raw
        elif isinstance(raw, dict):
            mode = str(raw.get("mode") or raw.get("type") or "host-allowlist")
        else:
            raise BundleError("Hosted tool network policy must be a string or object.")
        if mode != "host-allowlist":
            raise BundleError(f"Unsupported hosted tool network policy: {mode}")
        result: dict[str, Any] = {"mode": "host-allowlist"}
        if matched_prefix:
            result["matchedPrefix"] = matched_prefix
        return result

    def _validate_hosted_tool_endpoint(self, endpoint: str) -> str | None:
        prefixes = self.config.allowed_hosted_tool_url_prefixes
        if not prefixes:
            return None
        for prefix in prefixes:
            if self._hosted_tool_url_matches(endpoint, prefix):
                return prefix
        raise BundleError(f"Hosted tool endpoint is outside network allowlist: {endpoint}")

    def _hosted_tool_url_matches(self, endpoint: str, prefix: str) -> bool:
        target = urlparse(endpoint)
        allowed = urlparse(prefix)
        if target.scheme not in {"http", "https"} or allowed.scheme not in {"http", "https"}:
            return False
        if not target.hostname or not allowed.hostname:
            return False
        if target.scheme != allowed.scheme:
            return False
        if target.hostname.lower() != allowed.hostname.lower():
            return False
        allowed_port = self._url_port(allowed)
        if allowed_port is not None:
            target_port = self._url_port(target) or (443 if target.scheme == "https" else 80)
            if target_port != allowed_port:
                return False
        allowed_path = allowed.path.rstrip("/")
        if allowed_path:
            target_path = target.path or "/"
            if target_path != allowed_path and not target_path.startswith(f"{allowed_path}/"):
                return False
        return True

    @staticmethod
    def _url_port(parsed: ParseResult) -> int | None:
        try:
            return parsed.port
        except ValueError:
            return None

    def _validate_command(self, command: str) -> None:
        allowed_names: set[str] = set()
        allowed_paths: set[Path] = set()
        for value in self.config.allowed_tool_commands:
            allowed = str(value).strip()
            if not allowed:
                continue
            allowed_path = Path(allowed).expanduser()
            if allowed_path.is_absolute():
                allowed_paths.add(allowed_path.resolve())
            else:
                allowed_names.add(allowed)
        command_path = Path(command).expanduser()
        if command_path.is_absolute():
            resolved = command_path.resolve()
            if resolved in allowed_paths:
                return
            raise BundleError(f"MCP command path is not allowlisted: {resolved}")
        if command in allowed_names:
            return
        raise BundleError(f"MCP command is not allowlisted: {command}")

    def _mcp_config_toml(self, servers: tuple[McpServerRef, ...]) -> str:
        lines: list[str] = []
        for server in servers:
            table_name = server.name.replace('"', "")
            lines.append(f'[mcp_servers."{table_name}"]')
            lines.append(f"command = {json.dumps(server.command)}")
            if server.args:
                lines.append(f"args = {json.dumps(list(server.args))}")
            if server.cwd:
                lines.append(f"cwd = {json.dumps(str(server.cwd))}")
            config_env = {key: value for key, value in server.env.items() if not value.startswith("env:")}
            if config_env:
                env_items = ", ".join(f"{json.dumps(key)} = {json.dumps(value)}" for key, value in config_env.items())
                lines.append(f"env = {{ {env_items} }}")
            lines.append("")
        return "\n".join(lines)
