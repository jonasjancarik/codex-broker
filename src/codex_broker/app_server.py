from __future__ import annotations

import json
import hashlib
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .bundles import McpServerRef
from .config import BrokerConfig
from .state import StateStore
from .util import clean_process_env, env_with, json_log, redact


class AppServerError(RuntimeError):
    pass


_CODEX_VERSION_CACHE: dict[tuple[tuple[str, ...], str, str], str] = {}
_CODEX_VERSION_CACHE_LOCK = threading.Lock()


@dataclass
class JsonRpcWaiter:
    event: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None
    error: BaseException | None = None


class TurnContext(Protocol):
    owner_hash: str
    thread_id: str
    codex_thread_id: str | None
    codex_turn_id: str | None

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
        owner_hash: str,
        profile: str,
        codex_home: Path,
        config_profile: str,
        pool_key_hash: str,
        state: StateStore | None = None,
        mcp_servers: tuple[McpServerRef, ...] = (),
        codex_config_args: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.config = config
        self.state = state
        self.owner_hash = owner_hash
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
        self._closed = False
        self._process_record_id: int | None = None
        self._process_record_closed = False
        self._stderr_lines: list[str] = []
        command = self._build_command()
        env = env_with(
            clean_process_env(),
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
                owner_hash=owner_hash,
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
            ownerHash=owner_hash,
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
                "capabilities": {"experimentalApi": True},
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
        except BrokenPipeError as exc:
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
        self._fail_waiters(AppServerError("App Server closed"))
        self._fail_contexts("App Server closed")
        self._stdout_thread.join(timeout=1)
        self._stderr_thread.join(timeout=1)
        json_log(
            self.config.json_logs,
            "app_server.close",
            ownerHash=self.owner_hash,
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
                ownerHash=self.owner_hash,
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
            self._handle_server_request(method, message_id, params)
        else:
            self._handle_notification(method, params)

    def _context_for_params(self, params: dict[str, Any]) -> tuple[TurnContext | None, bool]:
        turn_id = notification_turn_id(params)
        thread_id = notification_thread_id(params)
        with self._contexts_lock:
            if turn_id and turn_id in self._contexts_by_turn:
                return self._contexts_by_turn[turn_id], False
            if thread_id and thread_id in self._contexts_by_thread:
                return self._contexts_by_thread[thread_id], False
            if len(self._contexts) == 1:
                return next(iter(self._contexts)), True
        return None, bool(turn_id or thread_id)

    def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        context, ambiguous = self._context_for_params(params)
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
        if context and method.startswith("item/") and method.endswith("/requestApproval"):
            context.record_tool_requested(method, params, ambiguous=ambiguous)
        if context:
            context.handle_notification(method, params, ambiguous=ambiguous)
        if method.endswith("/requestApproval"):
            self.send({"id": message_id, "result": {"decision": "decline"}})
            if context:
                context.handle_notification(
                    "approval/resolved",
                    {"method": method, "decision": "decline", "params": params},
                    ambiguous=ambiguous,
                )
        elif method == "item/tool/requestUserInput":
            self.send({"id": message_id, "result": {"answers": {}}})
        else:
            self.send({"id": message_id, "error": {"code": -32601, "message": f"Unsupported App Server request: {method}"}})

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
        self._restart_count = 0

    def get(
        self,
        *,
        owner_hash: str,
        profile: str,
        codex_home: Path,
        config_profile: str,
        mcp_servers: tuple[McpServerRef, ...],
        codex_config_args: tuple[tuple[str, str], ...] = (),
    ) -> AppServerClient:
        key = (
            owner_hash,
            profile,
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
        with self._lock:
            self._sweep_locked()
            client = self._clients.get(key)
            if client and not client.closed:
                return client
            if client:
                self._clients.pop(key, None)
                self._restart_count += 1
                json_log(
                    self.config.json_logs,
                    "app_server.restart",
                    ownerHash=owner_hash,
                    profile=profile,
                    configProfile=config_profile,
                    poolKeyHash=key_hash,
                )
            client = AppServerClient(
                self.config,
                owner_hash=owner_hash,
                profile=profile,
                codex_home=codex_home,
                config_profile=config_profile,
                pool_key_hash=key_hash,
                state=self.state,
                mcp_servers=mcp_servers,
                codex_config_args=codex_config_args,
            )
            self._clients[key] = client
            return client

    def close_owner(self, owner_hash: str) -> None:
        self.close_profile(owner_hash, None)

    def close_profile(self, owner_hash: str, profile: str | None) -> None:
        with self._lock:
            for key, client in list(self._clients.items()):
                if key[0] == owner_hash and (profile is None or key[1] == profile):
                    self._clients.pop(key, None)
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
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            client.close()

    def _sweep_locked(self) -> None:
        if self.config.pool_idle_ttl_seconds <= 0:
            return
        now = time.monotonic()
        for key, client in list(self._clients.items()):
            if client.closed:
                self._clients.pop(key, None)
                self._restart_count += 1
                client.close()
            elif not client.has_active_contexts and now - client.last_used_at > self.config.pool_idle_ttl_seconds:
                self._clients.pop(key, None)
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
