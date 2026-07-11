from __future__ import annotations

import json
import hashlib
import os
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .bundles import McpServerRef
from .config import BrokerConfig
from .events import (
    APPROVAL_REQUEST_METHODS,
    USER_INPUT_REQUEST_METHOD,
)
from .interactions import (
    INTERACTION_REQUEST_METHODS,
    interaction_kind,
    resolved_notification_params,
    response_event_method,
    safe_response_for_method,
)
from .state import StateStore
from .util import clean_process_env, env_with, json_log, random_id, redact


class AppServerError(RuntimeError):
    pass


_CODEX_VERSION_CACHE: dict[tuple[tuple[str, ...], str, str], str] = {}
_CODEX_VERSION_CACHE_LOCK = threading.Lock()


@dataclass
class JsonRpcWaiter:
    event: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None
    error: BaseException | None = None


@dataclass
class PendingServerInteraction:
    interaction_id: str
    message_id: Any
    owner_hash: str
    method: str
    request_params: dict[str, Any]
    fallback_response: dict[str, Any]
    context: "TurnContext | None"
    ambiguous: bool
    event: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None
    source: str | None = None
    completed_in_state: bool = False


@dataclass
class PoolCreationGate:
    lock: threading.Lock = field(default_factory=threading.Lock)
    users: int = 0


class TurnContext(Protocol):
    owner_hash: str
    thread_id: str
    turn_id: str
    codex_thread_id: str | None
    codex_turn_id: str | None
    product_correlation_id: str | None

    def register_thread(self, codex_thread_id: str) -> None: ...

    def register_turn(self, codex_turn_id: str) -> None: ...

    def handle_notification(self, method: str, params: dict[str, Any], *, ambiguous: bool) -> None: ...

    def record_tool_requested(self, method: str, params: dict[str, Any], *, ambiguous: bool) -> None: ...

    def fail(self, message: str) -> None: ...


def notification_thread_id(params: dict[str, Any]) -> str | None:
    for key in ("threadId", "thread_id"):
        if isinstance(params.get(key), str):
            return str(params[key])
    thread = params.get("thread")
    if isinstance(thread, dict) and isinstance(thread.get("id"), str):
        return str(thread["id"])
    return None


def notification_turn_id(params: dict[str, Any]) -> str | None:
    for key in ("turnId", "turn_id"):
        if isinstance(params.get(key), str):
            return str(params[key])
    turn = params.get("turn")
    if isinstance(turn, dict) and isinstance(turn.get("id"), str):
        return str(turn["id"])
    return None


class AppServerClient:
    def __init__(
        self,
        config: BrokerConfig,
        *,
        owner_hash: str | None = None,
        auth_principal_hash: str | None = None,
        profile: str,
        codex_home: Path,
        config_profile: str,
        pool_key_hash: str,
        state: StateStore | None = None,
        mcp_servers: tuple[McpServerRef, ...] = (),
        codex_config_args: tuple[tuple[str, str], ...] = (),
    ) -> None:
        principal_hash = auth_principal_hash or owner_hash
        if not principal_hash:
            raise ValueError("auth_principal_hash is required.")
        self.config = config
        self.state = state
        self.auth_principal_hash = principal_hash
        self.owner_hash = principal_hash  # Backward-compatible alias for embedders.
        self.profile = profile
        self.config_profile = config_profile
        self.pool_key_hash = pool_key_hash
        self.codex_home = codex_home
        self.mcp_servers = mcp_servers
        self.codex_config_args = codex_config_args
        self.started_at = time.monotonic()
        self.last_used_at = self.started_at
        self._next_request_id = 1
        self._request_lock = threading.RLock()
        self._stdin_lock = threading.Lock()
        self._contexts_lock = threading.RLock()
        self._waiters: dict[int, JsonRpcWaiter] = {}
        self._contexts: set[TurnContext] = set()
        self._contexts_by_thread: dict[str, TurnContext] = {}
        self._contexts_by_turn: dict[str, TurnContext] = {}
        self._pending_notifications_by_turn: dict[str, list[tuple[str, dict[str, Any], bool]]] = {}
        self._pending_interactions: dict[str, PendingServerInteraction] = {}
        self._pending_interactions_lock = threading.RLock()
        self._closed = False
        self._process_record_id: int | None = None
        self._process_record_closed = False
        self._stderr_lines: list[str] = []
        command = self._build_command()
        env = env_with(
            clean_process_env(config.codex_passthrough_env),
            {
                "CODEX_HOME": str(codex_home),
                "CODEX_CREDENTIAL_STORE": config.credential_store,
                "HOME": str(codex_home.parent),
            },
        )
        env.update(self._mcp_process_env())
        self._process = subprocess.Popen(
            command,
            cwd=str(codex_home),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        assert self._process.stdin is not None
        assert self._process.stdout is not None
        assert self._process.stderr is not None
        self._stdin = self._process.stdin
        self._stdout = self._process.stdout
        self._stderr = self._process.stderr
        if self.state:
            self._process_record_id = self.state.record_app_server_start(
                pool_key_hash=pool_key_hash,
                auth_principal_hash=principal_hash,
                profile=profile,
                config_profile=config_profile,
                pid=self._process.pid,
            )
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()
        json_log(
            config.json_logs,
            "app_server.start",
            authPrincipalHash=principal_hash,
            profile=profile,
            configProfile=config_profile,
            pid=self._process.pid,
            poolKeyHash=pool_key_hash,
        )
        self.initialize()

    @property
    def closed(self) -> bool:
        return self._closed or self._process.poll() is not None

    @property
    def has_active_contexts(self) -> bool:
        with self._contexts_lock:
            return bool(self._contexts)

    def initialize(self) -> None:
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": self.config.client_name,
                    "title": self.config.client_title,
                    "version": self.config.client_version,
                },
                "capabilities": {
                    "experimentalApi": True,
                    "requestAttestation": False,
                    "mcpServerOpenaiFormElicitation": True,
                },
            },
        )
        self.send({"method": "initialized", "params": {}})

    def request(self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> dict[str, Any]:
        if self.closed:
            raise AppServerError("App Server is closed")
        self.last_used_at = time.monotonic()
        with self._request_lock:
            request_id = self._next_request_id
            self._next_request_id += 1
            waiter = JsonRpcWaiter()
            self._waiters[request_id] = waiter
        try:
            self.send({"id": request_id, "method": method, "params": params or {}})
            if not waiter.event.wait(timeout if timeout is not None else self.config.request_timeout_seconds):
                self.close()
                raise AppServerError(f"Timed out waiting for App Server response to {method}")
            if waiter.error:
                raise AppServerError(str(waiter.error)) from waiter.error
            response = waiter.response or {}
            if "error" in response:
                error = response["error"]
                if isinstance(error, dict):
                    raise AppServerError(str(error.get("message") or error))
                raise AppServerError(str(error))
            if "result" not in response:
                raise AppServerError(f"App Server response missing result for {method}")
            result = response["result"]
            return result if isinstance(result, dict) else {"value": result}
        finally:
            with self._request_lock:
                self._waiters.pop(request_id, None)

    def send(self, payload: dict[str, Any]) -> None:
        try:
            with self._stdin_lock:
                self._stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                self._stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise AppServerError("App Server stdin closed unexpectedly") from exc

    def register_context(self, context: TurnContext) -> None:
        with self._contexts_lock:
            self._contexts.add(context)
            if context.codex_thread_id:
                self._contexts_by_thread[context.codex_thread_id] = context
            if context.codex_turn_id:
                self._contexts_by_turn[context.codex_turn_id] = context

    def unregister_context(self, context: TurnContext) -> None:
        with self._contexts_lock:
            self._contexts.discard(context)
            for key, value in list(self._contexts_by_thread.items()):
                if value is context:
                    self._contexts_by_thread.pop(key, None)
            for key, value in list(self._contexts_by_turn.items()):
                if value is context:
                    self._contexts_by_turn.pop(key, None)

    def register_thread_for_context(self, context: TurnContext, codex_thread_id: str) -> None:
        context.register_thread(codex_thread_id)
        with self._contexts_lock:
            self._contexts_by_thread[codex_thread_id] = context

    def register_turn_for_context(self, context: TurnContext, codex_turn_id: str) -> None:
        context.register_turn(codex_turn_id)
        with self._contexts_lock:
            self._contexts_by_turn[codex_turn_id] = context
            pending = self._pending_notifications_by_turn.pop(codex_turn_id, [])
        for method, params, ambiguous in pending:
            context.handle_notification(method, params, ambiguous=ambiguous)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._stdin.close()
        except OSError:
            pass
        try:
            self._process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2)
        self._close_streams()
        self._record_process_close("closed", self._process.poll())
        self._cancel_pending_interactions("app_server_closed")
        self._fail_waiters(AppServerError("App Server closed"))
        self._fail_contexts("App Server closed")
        self._stdout_thread.join(timeout=1)
        self._stderr_thread.join(timeout=1)
        json_log(
            self.config.json_logs,
            "app_server.close",
            authPrincipalHash=getattr(self, "auth_principal_hash", self.owner_hash),
            profile=self.profile,
            configProfile=self.config_profile,
            pid=self._process.pid,
            poolKeyHash=self.pool_key_hash,
            exitCode=self._process.poll(),
        )

    def _build_command(self) -> list[str]:
        args = [*self.config.codex_command, "app-server", "--listen", "stdio://"]
        for key, value in self.codex_config_args:
            args.extend(["-c", f"{key}={value}"])
        for server in self.mcp_servers:
            name = server.name.replace('"', "")
            args.extend(["-c", f'mcp_servers."{name}".command={json.dumps(server.command)}'])
            if server.args:
                args.extend(["-c", f'mcp_servers."{name}".args={json.dumps(list(server.args))}'])
            if server.cwd:
                args.extend(["-c", f'mcp_servers."{name}".cwd={json.dumps(str(server.cwd))}'])
            if server.env:
                config_env = {key: value for key, value in server.env.items() if not value.startswith("env:")}
                if config_env:
                    env_items = ", ".join(f"{json.dumps(key)} = {json.dumps(value)}" for key, value in config_env.items())
                    args.extend(["-c", f'mcp_servers."{name}".env={{ {env_items} }}'])
        return args

    def _mcp_process_env(self) -> dict[str, str]:
        resolved: dict[str, str] = {}
        for server in self.mcp_servers:
            for target, value in server.env.items():
                if not value.startswith("env:"):
                    continue
                source = value.removeprefix("env:")
                if source not in os.environ:
                    raise AppServerError(f"Missing MCP env source: {source}")
                resolved[target] = os.environ[source]
        return resolved

    def _read_stdout(self) -> None:
        try:
            for line in self._stdout:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    message = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    clean = self._log_stream_line("stdout.invalid_json", stripped, limit=1000)
                    self._fail_contexts(f"Invalid App Server JSON: {clean}")
                    self._fail_waiters(exc)
                    return
                if isinstance(message, dict):
                    self._handle_message(message)
        finally:
            if not self._closed:
                code = self._process.poll()
                message = f"App Server exited unexpectedly with code {code}"
                self._record_process_close("exited", code)
                self._fail_contexts(message)
                self._fail_waiters(AppServerError(message))
            self._close_streams()

    def _record_process_close(self, status: str, exit_code: int | None) -> None:
        if not self.state or self._process_record_id is None or self._process_record_closed:
            return
        self._process_record_closed = True
        self.state.record_app_server_close(self._process_record_id, status=status, exit_code=exit_code)

    def _read_stderr(self) -> None:
        for line in self._stderr:
            clean = self._log_stream_line("stderr", line, limit=1200)
            if clean:
                self._stderr_lines.append(clean)
                self._stderr_lines = self._stderr_lines[-200:]

    def _log_stream_line(self, stream: str, line: str, *, limit: int = 1200) -> str:
        clean = redact(line.rstrip(), limit)
        if clean:
            json_log(
                self.config.json_logs,
                f"app_server.{stream}",
                authPrincipalHash=getattr(self, "auth_principal_hash", self.owner_hash),
                profile=self.profile,
                configProfile=self.config_profile,
                pid=getattr(self._process, "pid", None),
                poolKeyHash=self.pool_key_hash,
                line=clean,
            )
        return clean

    def _close_streams(self) -> None:
        for stream in (self._stdin, self._stdout, self._stderr):
            try:
                if not stream.closed:
                    stream.close()
            except OSError:
                pass

    def _handle_message(self, message: dict[str, Any]) -> None:
        message_id = message.get("id")
        if message_id is not None and ("result" in message or "error" in message):
            with self._request_lock:
                waiter = self._waiters.get(int(message_id)) if isinstance(message_id, int) else None
            if waiter:
                waiter.response = message
                waiter.event.set()
                return
        method = message.get("method")
        if not isinstance(method, str):
            return
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if message_id is not None:
            threading.Thread(
                target=self._handle_server_request,
                args=(method, message_id, params),
                name=f"app-server-request-{message_id}",
                daemon=True,
            ).start()
        else:
            self._handle_notification(method, params)

    def _context_for_params(
        self,
        params: dict[str, Any],
        *,
        allow_unbound_thread: bool = False,
    ) -> tuple[TurnContext | None, bool]:
        turn_id = notification_turn_id(params)
        thread_id = notification_thread_id(params)
        with self._contexts_lock:
            if turn_id and turn_id in self._contexts_by_turn:
                return self._contexts_by_turn[turn_id], False
            if thread_id and thread_id in self._contexts_by_thread:
                return self._contexts_by_thread[thread_id], False
            if turn_id or thread_id:
                if allow_unbound_thread and thread_id and not turn_id and len(self._contexts) == 1:
                    context = next(iter(self._contexts))
                    if context.codex_thread_id is None:
                        return context, True
                return None, True
            if len(self._contexts) == 1:
                return next(iter(self._contexts)), True
        return None, False

    def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        context, ambiguous = self._context_for_params(
            params,
            allow_unbound_thread=method in {"thread/started", "thread/resumed"},
        )
        if method in {"thread/started", "thread/resumed"} and context:
            thread_id = notification_thread_id(params)
            if thread_id:
                self.register_thread_for_context(context, thread_id)
        if method == "turn/started" and context:
            turn_id = notification_turn_id(params)
            if turn_id:
                self.register_turn_for_context(context, turn_id)
        if context:
            context.handle_notification(method, params, ambiguous=ambiguous)
            return
        turn_id = notification_turn_id(params)
        if turn_id:
            with self._contexts_lock:
                pending = self._pending_notifications_by_turn.setdefault(turn_id, [])
                pending.append((method, params, True))
                self._pending_notifications_by_turn[turn_id] = pending[-50:]

    def _handle_server_request(self, method: str, message_id: Any, params: dict[str, Any]) -> None:
        context, ambiguous = self._context_for_params(params)
        if method in INTERACTION_REQUEST_METHODS:
            self._handle_interaction_request(method, message_id, params, context, ambiguous)
        else:
            self.send({"id": message_id, "error": {"code": -32601, "message": f"Unsupported App Server request: {method}"}})

    def _handle_interaction_request(
        self,
        method: str,
        message_id: Any,
        params: dict[str, Any],
        context: TurnContext | None,
        ambiguous: bool,
    ) -> None:
        fallback = safe_response_for_method(method)
        if not context:
            self.send({"id": message_id, "result": fallback})
            return
        timeout = self._host_response_timeout(method, params)
        interaction = self._create_pending_interaction(method, message_id, params, fallback, context, ambiguous, timeout)
        request_params = dict(params)
        request_params["interactionId"] = interaction.interaction_id
        if context and method in APPROVAL_REQUEST_METHODS:
            context.record_tool_requested(method, request_params, ambiguous=ambiguous)
        context.handle_notification(method, request_params, ambiguous=ambiguous)
        try:
            response, source = self._await_interaction_response(interaction, timeout)
            self._complete_pending_interaction(interaction, response, source)
            try:
                self.send({"id": message_id, "result": response})
            except AppServerError:
                return
            context.handle_notification(
                response_event_method(method),
                resolved_notification_params(
                    method,
                    response,
                    interaction_id=interaction.interaction_id,
                    request_params=request_params,
                    source=source,
                ),
                ambiguous=ambiguous,
            )
        finally:
            with self._pending_interactions_lock:
                self._pending_interactions.pop(interaction.interaction_id, None)

    def _create_pending_interaction(
        self,
        method: str,
        message_id: Any,
        params: dict[str, Any],
        fallback: dict[str, Any],
        context: TurnContext,
        ambiguous: bool,
        timeout: float,
    ) -> PendingServerInteraction:
        self._ensure_pending_interaction_state()
        state = getattr(self, "state", None)
        if state:
            row = state.create_pending_interaction(
                context.owner_hash,
                context.thread_id,
                context.turn_id,
                kind=interaction_kind(method),
                method=method,
                request=params,
                fallback_response=fallback,
                product_correlation_id=context.product_correlation_id,
                codex_thread_id=context.codex_thread_id,
                codex_turn_id=context.codex_turn_id,
                timeout_seconds=timeout,
            )
            interaction_id = str(row["interaction_id"])
        else:
            interaction_id = random_id("int")
        interaction = PendingServerInteraction(
            interaction_id=interaction_id,
            message_id=message_id,
            owner_hash=context.owner_hash,
            method=method,
            request_params=params,
            fallback_response=fallback,
            context=context,
            ambiguous=ambiguous,
        )
        with self._pending_interactions_lock:
            self._pending_interactions[interaction_id] = interaction
        return interaction

    def _await_interaction_response(self, interaction: PendingServerInteraction, timeout: float) -> tuple[dict[str, Any], str]:
        if timeout > 0 and interaction.event.wait(timeout):
            return interaction.response or interaction.fallback_response, interaction.source or "host"
        with self._pending_interactions_lock:
            if interaction.event.is_set():
                return interaction.response or interaction.fallback_response, interaction.source or "host"
            interaction.response = interaction.fallback_response
            interaction.source = "fallback_timeout" if timeout > 0 else "fallback_no_wait"
            interaction.event.set()
        return interaction.fallback_response, interaction.source

    def _complete_pending_interaction(
        self,
        interaction: PendingServerInteraction,
        response: dict[str, Any],
        source: str,
    ) -> None:
        if interaction.completed_in_state:
            return
        state = getattr(self, "state", None)
        if state:
            state.complete_interaction(interaction.owner_hash, interaction.interaction_id, response=response, source=source)
        interaction.completed_in_state = True

    def resolve_pending_interaction(self, interaction_id: str, response: dict[str, Any], *, source: str = "host") -> dict[str, Any] | None:
        self._ensure_pending_interaction_state()
        with self._pending_interactions_lock:
            interaction = self._pending_interactions.get(interaction_id)
            if not interaction or interaction.event.is_set():
                return None
            interaction.response = response
            interaction.source = source
            interaction.event.set()
        state = getattr(self, "state", None)
        if state:
            row = state.complete_interaction(interaction.owner_hash, interaction_id, response=response, source=source)
            interaction.completed_in_state = True
            return row
        return None

    def _cancel_pending_interactions(self, source: str) -> None:
        self._ensure_pending_interaction_state()
        with self._pending_interactions_lock:
            pending = list(self._pending_interactions.values())
        for interaction in pending:
            with self._pending_interactions_lock:
                if interaction.event.is_set():
                    continue
                interaction.response = interaction.fallback_response
                interaction.source = source
                interaction.event.set()
            self._complete_pending_interaction(interaction, interaction.fallback_response, source)

    def _ensure_pending_interaction_state(self) -> None:
        if not hasattr(self, "_pending_interactions_lock"):
            self._pending_interactions_lock = threading.RLock()
        if not hasattr(self, "_pending_interactions"):
            self._pending_interactions = {}

    def _host_response_timeout(self, method: str, params: dict[str, Any]) -> float:
        timeout = float(getattr(getattr(self, "config", None), "host_response_timeout_seconds", 0) or 0)
        if method == USER_INPUT_REQUEST_METHOD and isinstance(params.get("autoResolutionMs"), int):
            prompt_timeout = max(float(params["autoResolutionMs"]) / 1000, 0)
            if timeout <= 0 or prompt_timeout < timeout:
                timeout = prompt_timeout
        return max(timeout, 0)

    def _fail_waiters(self, error: BaseException) -> None:
        with self._request_lock:
            waiters = list(self._waiters.values())
        for waiter in waiters:
            waiter.error = error
            waiter.event.set()

    def _fail_contexts(self, message: str) -> None:
        with self._contexts_lock:
            contexts = list(self._contexts)
        for context in contexts:
            context.fail(message)


class AppServerPool:
    def __init__(self, config: BrokerConfig, state: StateStore | None = None) -> None:
        self.config = config
        self.state = state
        self._lock = threading.RLock()
        self._clients: dict[tuple[Any, ...], AppServerClient] = {}
        self._creation_locks: dict[tuple[Any, ...], PoolCreationGate] = {}
        self._restart_count = 0
        self._closed = False
        self._sweeper_stop = threading.Event()
        self._sweeper_thread: threading.Thread | None = None
        if config.pool_idle_ttl_seconds > 0:
            self._sweeper_thread = threading.Thread(target=self._sweeper_loop, name="app-server-pool-sweeper", daemon=True)
            self._sweeper_thread.start()

    def get(
        self,
        *,
        owner_hash: str | None = None,
        auth_principal_hash: str | None = None,
        profile: str,
        codex_home: Path,
        config_profile: str,
        mcp_servers: tuple[McpServerRef, ...],
        tenant_scope_hash: str | None = None,
        codex_config_args: tuple[tuple[str, str], ...] = (),
        auth_fingerprint: str | None = None,
    ) -> AppServerClient:
        principal_hash = auth_principal_hash or owner_hash
        if not principal_hash:
            raise ValueError("auth_principal_hash is required.")
        key = (
            principal_hash,
            profile,
            auth_fingerprint or "unknown-auth",
            tenant_scope_hash if mcp_servers else None,
            config_profile,
            tuple(self.config.codex_command),
            self._codex_version(),
            self.config.credential_store,
            self.config.client_name,
            self.config.client_title,
            self.config.client_version,
            codex_config_args,
            tuple((server.name, server.command, server.args, tuple(sorted(server.env.items())), str(server.cwd)) for server in mcp_servers),
            self._mcp_env_fingerprint(mcp_servers),
        )
        key_hash = hashlib.sha256(repr(key).encode("utf-8")).hexdigest()[:16]
        if self._closed:
            raise AppServerError("App Server pool is closed")
        self._sweep()
        if auth_fingerprint is not None:
            self._close_idle_stale_auth(principal_hash, profile, key[2])
        with self._lock:
            client = self._clients.get(key)
            if client and not client.closed:
                return client
        with self._creation_gate(key):
            with self._lock:
                client = self._clients.get(key)
                if client and not client.closed:
                    return client
                if client:
                    self._clients.pop(key, None)
                    self._restart_count += 1
            if client:
                client.close()
                json_log(
                    self.config.json_logs,
                    "app_server.restart",
                    authPrincipalHash=principal_hash,
                    profile=profile,
                    configProfile=config_profile,
                    poolKeyHash=key_hash,
                )
            client = AppServerClient(
                self.config,
                auth_principal_hash=principal_hash,
                profile=profile,
                codex_home=codex_home,
                config_profile=config_profile,
                pool_key_hash=key_hash,
                state=self.state,
                mcp_servers=mcp_servers,
                codex_config_args=codex_config_args,
            )
            with self._lock:
                closed_during_start = self._closed
                if not closed_during_start:
                    self._clients[key] = client
            if closed_during_start:
                client.close()
                raise AppServerError("App Server pool closed during child startup")
            return client

    @contextmanager
    def _creation_gate(self, key: tuple[Any, ...]) -> Any:
        with self._lock:
            gate = self._creation_locks.setdefault(key, PoolCreationGate())
            gate.users += 1
        try:
            with gate.lock:
                yield
        finally:
            with self._lock:
                gate.users -= 1
                if gate.users == 0 and self._creation_locks.get(key) is gate:
                    self._creation_locks.pop(key, None)

    def close_owner(self, owner_hash: str) -> None:
        self.close_profile(owner_hash, None)

    def close_auth_principal(self, auth_principal_hash: str) -> None:
        self.close_profile(auth_principal_hash, None)

    def close_profile(self, auth_principal_hash: str, profile: str | None) -> None:
        with self._lock:
            clients: list[AppServerClient] = []
            for key, client in list(self._clients.items()):
                if key[0] == auth_principal_hash and (profile is None or key[1] == profile):
                    self._clients.pop(key, None)
                    clients.append(client)
        for client in clients:
            client.close()

    def close_client(self, client: AppServerClient) -> None:
        with self._lock:
            for key, current in list(self._clients.items()):
                if current is client:
                    self._clients.pop(key, None)
                    break
        client.close()

    def metrics(self) -> dict[str, int]:
        with self._lock:
            active_children = sum(1 for client in self._clients.values() if not client.closed)
            restarts = self._restart_count
        return {"active_app_server_children": active_children, "app_server_restarts": restarts}

    def close_all(self) -> None:
        self._sweeper_stop.set()
        if self._sweeper_thread and self._sweeper_thread is not threading.current_thread():
            self._sweeper_thread.join(timeout=1)
        with self._lock:
            self._closed = True
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            client.close()

    def _sweeper_loop(self) -> None:
        interval = max(0.1, min(self.config.pool_idle_ttl_seconds / 2, 60))
        while not self._sweeper_stop.wait(interval):
            self._sweep()

    def _sweep(self) -> None:
        if self.config.pool_idle_ttl_seconds <= 0:
            return
        now = time.monotonic()
        stale: list[AppServerClient] = []
        with self._lock:
            for key, client in list(self._clients.items()):
                if client.closed:
                    self._clients.pop(key, None)
                    self._restart_count += 1
                    stale.append(client)
                elif not client.has_active_contexts and now - client.last_used_at > self.config.pool_idle_ttl_seconds:
                    self._clients.pop(key, None)
                    stale.append(client)
        for client in stale:
            client.close()

    def _close_idle_stale_auth(
        self,
        auth_principal_hash: str,
        profile: str,
        auth_fingerprint: str,
    ) -> None:
        stale: list[AppServerClient] = []
        with self._lock:
            for key, client in list(self._clients.items()):
                if (
                    key[0] == auth_principal_hash
                    and key[1] == profile
                    and key[2] != auth_fingerprint
                    and not client.has_active_contexts
                ):
                    self._clients.pop(key, None)
                    stale.append(client)
        for client in stale:
            client.close()

    def _mcp_env_fingerprint(self, mcp_servers: tuple[McpServerRef, ...]) -> tuple[tuple[str, str, str, str], ...]:
        fingerprint: list[tuple[str, str, str, str]] = []
        for server in mcp_servers:
            for target, value in sorted(server.env.items()):
                if not value.startswith("env:"):
                    continue
                source = value.removeprefix("env:")
                raw = os.environ.get(source)
                digest = hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw is not None else "missing"
                fingerprint.append((server.name, target, source, digest))
        return tuple(fingerprint)

    def _codex_version(self) -> str:
        key = (tuple(self.config.codex_command), os.environ.get("PATH", ""), os.environ.get("FAKE_CODEX_VERSION", ""))
        with _CODEX_VERSION_CACHE_LOCK:
            cached = _CODEX_VERSION_CACHE.get(key)
        if cached is not None:
            return cached
        version = self._detect_codex_version()
        with _CODEX_VERSION_CACHE_LOCK:
            _CODEX_VERSION_CACHE[key] = version
        return version

    def _detect_codex_version(self) -> str:
        try:
            result = subprocess.run(
                [*self.config.codex_command, "--version"],
                env=clean_process_env(),
                text=True,
                input="",
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return f"unavailable:{type(exc).__name__}"
        output = f"{result.stdout}\n{result.stderr}".strip()
        return redact(output, 500) if output else f"exit:{result.returncode}"
