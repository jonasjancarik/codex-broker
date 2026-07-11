from __future__ import annotations

import hashlib
import threading
import re
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .app_server import AppServerClient, AppServerError, AppServerPool
from .auth import AuthManager
from .bundles import BundleRegistry, ResolvedBundle
from .config import BrokerConfig
from .events import normalize_app_server_event
from .runtime_errors import CODEX_AUTH_REQUIRES_ADMIN, RuntimeErrorInfo, classify_app_server_error, classify_runtime_error
from .scheduler_errors import ActiveTurnError, ConflictError, NotFoundError
from . import scheduler_config, scheduler_interactions, scheduler_threads
from .state import StateStore
from .util import json_dumps, json_log, redact_json, utc_now


def metric_key(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", value).strip("_").lower()


def optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None
@dataclass
class ThreadGate:
    binding_lock: threading.RLock = field(default_factory=threading.RLock)
    active_context: "BrokerTurnContext | None" = None
    running: bool = False
    queue: deque["QueuedTurn"] | None = None

    def pending(self) -> deque["QueuedTurn"]:
        if self.queue is None:
            self.queue = deque()
        return self.queue
@dataclass(frozen=True)
class QueuedTurn:
    owner_hash: str
    thread_id: str
    turn_id: str
    body: dict[str, Any]


class BrokerTurnContext:
    def __init__(
        self,
        *,
        state: StateStore,
        owner_hash: str,
        auth_principal_hash: str,
        thread_id: str,
        turn_id: str,
        codex_thread_id: str | None,
        product_correlation_id: str | None,
        debug_raw_events: bool,
    ) -> None:
        self.state = state
        self.owner_hash = owner_hash
        self.auth_principal_hash = auth_principal_hash
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.codex_thread_id = codex_thread_id
        self.product_correlation_id = product_correlation_id
        self.codex_turn_id: str | None = None
        self.client: AppServerClient | None = None
        self.completed_event = threading.Event()
        self.final_status: str | None = None
        self.error_text: str | None = None
        self.error_code: str | None = None
        self.public_message: str | None = None
        self.admin_message: str | None = None
        self.debug_raw_events = debug_raw_events
        self.terminal_event_type: str | None = None
        self.terminal_payload: dict[str, Any] | None = None
        self.terminal_raw_method: str | None = None
        self.terminal_raw_params: dict[str, Any] | None = None
        self.terminal_ambiguous = False
        self._lock = threading.RLock()

    def register_thread(self, codex_thread_id: str) -> None:
        with self._lock:
            self.codex_thread_id = codex_thread_id
            self.state.set_codex_thread_id(self.owner_hash, self.thread_id, codex_thread_id)

    def register_turn(self, codex_turn_id: str) -> None:
        with self._lock:
            self.codex_turn_id = codex_turn_id
            self.state.update_turn(self.owner_hash, self.thread_id, self.turn_id, codex_turn_id=codex_turn_id)

    def handle_notification(self, method: str, params: dict[str, Any], *, ambiguous: bool) -> None:
        event_type, payload = self._normalize(method, params)
        error_info: RuntimeErrorInfo | None = None
        if method == "turn/completed":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            status = str(turn.get("status") or "completed")
            error = turn.get("error")
            self.final_status = "completed" if status == "completed" else "failed"
            error_info = classify_app_server_error(error)
            self._set_error_info(error_info)
            if error_info:
                payload = payload | error_info.public_payload()
            self.terminal_event_type = event_type
            self.terminal_payload = payload
            self.terminal_raw_method = method if self.debug_raw_events else None
            self.terminal_raw_params = redact_json(params) if self.debug_raw_events else None
            self.terminal_ambiguous = ambiguous
            self.completed_event.set()
        else:
            self._append_event(event_type, payload, method, params, ambiguous=ambiguous)
        if event_type in {"approval.requested", "approval.resolved"}:
            self.state.append_audit(
                self.owner_hash,
                event_type,
                payload,
                auth_principal_hash=self.auth_principal_hash,
                thread_id=self.thread_id,
                turn_id=self.turn_id,
            )

    def record_tool_requested(self, method: str, params: dict[str, Any], *, ambiguous: bool) -> None:
        self._append_event("tool.requested", {"method": method, "params": params}, method, params, ambiguous=ambiguous)

    def _append_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        method: str,
        params: dict[str, Any],
        *,
        ambiguous: bool,
    ) -> None:
        raw_method = method if self.debug_raw_events else None
        raw_params = redact_json(params) if self.debug_raw_events else None
        self.state.append_event(
            self.owner_hash,
            self.thread_id,
            self.turn_id,
            event_type,
            payload,
            product_correlation_id=self.product_correlation_id,
            codex_thread_id=self.codex_thread_id,
            codex_turn_id=self.codex_turn_id,
            raw_method=raw_method,
            raw_params=raw_params,
            ambiguous=ambiguous,
        )

    def finish(self, status: str, message: str, event_type: str | None = None) -> None:
        with self._lock:
            if self.completed_event.is_set():
                return
            error_info = classify_runtime_error(message)
            self.final_status = status
            self._set_error_info(error_info)
            self.terminal_event_type = event_type or ("turn.failed" if status != "interrupted" else "turn.interrupted")
            self.terminal_payload = error_info.public_payload()
            self.completed_event.set()

    def fail(self, message: str) -> None:
        self.finish("failed", message, "turn.failed")

    def _normalize(self, method: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return normalize_app_server_event(
            method,
            params,
            codex_thread_id=self.codex_thread_id,
            codex_turn_id=self.codex_turn_id,
        )

    def _set_error_info(self, error_info: RuntimeErrorInfo | None) -> None:
        if not error_info:
            self.error_text = None
            self.error_code = None
            self.public_message = None
            self.admin_message = None
            return
        self.error_text = error_info.public_message
        self.error_code = error_info.code
        self.public_message = error_info.public_message
        self.admin_message = error_info.admin_message


class TurnScheduler:
    def __init__(
        self,
        *,
        config: BrokerConfig,
        state: StateStore,
        auth: AuthManager,
        bundles: BundleRegistry,
        pool: AppServerPool,
    ) -> None:
        self.config = config
        self.state = state
        self.auth = auth
        self.bundles = bundles
        self.pool = pool
        self._gates: dict[tuple[str, str], ThreadGate] = {}
        self._gates_lock = threading.RLock()
        self._shutdown = threading.Event()
        self._shutdown_mode = "interrupt"
        worker_limit = config.max_active_turns if config.max_active_turns > 0 else min(32, config.max_queued_turns)
        self._worker_limit = worker_limit
        self._outstanding_turns = 0
        self._executor = ThreadPoolExecutor(max_workers=worker_limit, thread_name_prefix="codex-turn")
        self._workers: set[Future[None]] = set()
        self._future_work: dict[Future[None], QueuedTurn] = {}
        self._workers_lock = threading.RLock()
        self._metrics_lock = threading.Lock()
        self._metrics = {
            "active_turns": 0,
            "queued_turns": 0,
            "turns_started": 0,
            "turns_completed": 0,
            "turns_failed": 0,
            "turns_interrupted": 0,
            "turns_recovered": 0,
            "raw_events_pruned": 0,
        }

    def create_thread(self, owner_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return scheduler_threads.create_thread(self, owner_id, body)

    def get_thread(self, owner_id: str, thread_id: str) -> dict[str, Any]:
        return scheduler_threads.get_thread(self, owner_id, thread_id)

    def archive_thread(self, owner_id: str, thread_id: str) -> dict[str, Any]:
        return scheduler_threads.archive_thread(self, owner_id, thread_id)

    def start_turn(self, owner_id: str, thread_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if self._shutdown.is_set():
            raise ConflictError("Broker is shutting down.")
        owner_hash = self.auth.hash_owner(owner_id)
        thread = self.state.get_thread(owner_hash, thread_id)
        if not thread:
            raise NotFoundError("Thread not found.")
        scope = scheduler_threads.validate_turn_auth_binding(self, owner_id, thread, body)
        if thread["status"] == "archived":
            raise ConflictError("Thread is archived.")
        input_items = body.get("input")
        if not isinstance(input_items, list) or not input_items:
            raise ValueError("input must be a non-empty array.")
        mode = str(body.get("mode") or "reject")
        if mode not in {"reject", "queue", "steer"}:
            raise ValueError("mode must be reject, queue, or steer.")
        if mode == "steer":
            steered = self._steer_active(owner_hash, thread_id, input_items)
            if steered:
                return steered
            mode = "reject"
        key = body.get("idempotencyKey")
        correlation_id = body.get("productCorrelationId") or body.get("correlationId")
        if isinstance(key, str) and key:
            existing = self.state.find_turn_by_idempotency(owner_hash, thread_id, key)
            if existing:
                public = self._public_turn(existing)
                public["streamUrl"] = self._stream_url(owner_id, thread_id, existing["turn_id"])
                return public
        bundle_id = str(body.get("bundleId") or thread.get("bundle_id") or "") or None
        config_profile = self._request_config_profile(body, thread.get("config_profile") or "default")
        config_profile_config = self._config_profile_config(config_profile)
        host_app = optional_text(body.get("hostApp")) or thread.get("host_app")
        profile = str(thread["profile"])
        self._validate_config_profile_bundle(config_profile_config, bundle_id)
        bundle = self.bundles.resolve(bundle_id) if bundle_id else None
        cwd = self.bundles.validate_cwd(body.get("cwd") or thread.get("cwd"), bundle)
        self._validate_config_profile_cwd(cwd, config_profile_config)
        with self._gates_lock:
            if isinstance(key, str) and key:
                existing = self.state.find_turn_by_idempotency(owner_hash, thread_id, key)
                if existing:
                    public = self._public_turn(existing)
                    public["streamUrl"] = self._stream_url(owner_id, thread_id, existing["turn_id"])
                    return public
            gate = self._gate_locked(owner_hash, thread_id)
            busy = gate.running or bool(gate.pending())
            if mode == "reject" and busy:
                raise ActiveTurnError("active_turn_exists")
            queued_count = sum(len(item.pending()) for item in self._gates.values())
            if busy and queued_count >= self.config.max_queued_turns:
                raise ConflictError("Turn queue is full.")
            if self._outstanding_turns >= self._worker_limit + self.config.max_queued_turns:
                raise ConflictError("Turn queue is full.")
            turn = self.state.create_turn(
                owner_hash,
                thread_id,
                auth_principal_hash=scope.auth_principal_hash,
                auth_profile_instance_id=str(thread["auth_profile_instance_id"]),
                profile=profile,
                config_profile=config_profile,
                host_app=host_app,
                bundle_id=bundle_id,
                cwd=str(cwd) if cwd else None,
                mode=mode,
                input_items=input_items,
                idempotency_key=key if isinstance(key, str) and key else None,
                product_correlation_id=correlation_id if isinstance(correlation_id, str) and correlation_id else None,
                status="queued" if busy else "starting",
                request_fingerprint=hashlib.sha256(json_dumps(body).encode("utf-8")).hexdigest(),
                bundle_digest=bundle.digest if bundle else None,
                resolved_options={
                    "authPrincipalHash": scope.auth_principal_hash,
                    "profile": profile,
                    "configProfile": config_profile,
                    "hostApp": host_app,
                    "bundleId": bundle_id,
                    "cwd": str(cwd) if cwd else None,
                    "codexOptions": self._request_codex_options(body),
                    "configProfileOptions": config_profile_config,
                },
                broker_version=self.config.client_version,
            )
            if turn.pop("_created", False):
                self._outstanding_turns += 1
                work = QueuedTurn(owner_hash, thread_id, turn["turn_id"], dict(body))
                if busy:
                    gate.pending().append(work)
                    self._metric("queued_turns", 1)
                else:
                    gate.running = True
                    self._submit_work(work)
        public = self._public_turn(turn)
        public["streamUrl"] = self._stream_url(owner_id, thread_id, turn["turn_id"])
        return public

    def get_turn(self, owner_id: str, thread_id: str, turn_id: str) -> dict[str, Any]:
        owner_hash = self.auth.hash_owner(owner_id)
        turn = self.state.get_turn(owner_hash, thread_id, turn_id)
        if not turn:
            raise NotFoundError("Turn not found.")
        return self._public_turn(turn)

    def steer_turn(self, owner_id: str, thread_id: str, turn_id: str, body: dict[str, Any]) -> dict[str, Any]:
        owner_hash = self.auth.hash_owner(owner_id)
        input_items = body.get("input")
        if not isinstance(input_items, list) or not input_items:
            raise ValueError("input must be a non-empty array.")
        active = self._active_context(owner_hash, thread_id)
        if not active or active.turn_id != turn_id:
            raise ActiveTurnError("active_turn_not_found")
        if not active.client or not active.codex_thread_id:
            raise ConflictError("Active turn is not steerable yet.")
        params: dict[str, Any] = {"threadId": active.codex_thread_id, "input": input_items}
        if active.codex_turn_id:
            params["turnId"] = active.codex_turn_id
        active.client.request("turn/steer", params)
        self.state.append_event(
            owner_hash,
            thread_id,
            turn_id,
            "message.delta",
            {"steered": True, "input": input_items},
            product_correlation_id=active.product_correlation_id,
            codex_thread_id=active.codex_thread_id,
            codex_turn_id=active.codex_turn_id,
        )
        return self.get_turn(owner_id, thread_id, turn_id)

    def interrupt_turn(self, owner_id: str, thread_id: str, turn_id: str) -> dict[str, Any]:
        owner_hash = self.auth.hash_owner(owner_id)
        active = self._active_context(owner_hash, thread_id)
        if not active or active.turn_id != turn_id:
            raise ActiveTurnError("active_turn_not_found")
        if active.client and active.codex_thread_id:
            params: dict[str, Any] = {"threadId": active.codex_thread_id}
            if active.codex_turn_id:
                params["turnId"] = active.codex_turn_id
            active.client.request("turn/interrupt", params)
        active.finish("interrupted", "Turn interrupted.", "turn.interrupted")
        if self._persist_context_terminal(active):
            self.state.append_audit(
                owner_hash,
                "turn.interrupt",
                {},
                auth_principal_hash=active.auth_principal_hash,
                thread_id=thread_id,
                turn_id=turn_id,
            )
            self._metric("turns_interrupted", 1)
        return self.get_turn(owner_id, thread_id, turn_id)

    def list_interactions(
        self,
        owner_id: str,
        thread_id: str,
        *,
        turn_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        return scheduler_interactions.list_interactions(self, owner_id, thread_id, turn_id=turn_id, status=status, limit=limit)

    def get_interaction(self, owner_id: str, thread_id: str, turn_id: str, interaction_id: str) -> dict[str, Any]:
        return scheduler_interactions.get_interaction(self, owner_id, thread_id, turn_id, interaction_id)

    def resolve_interaction(
        self,
        owner_id: str,
        thread_id: str,
        turn_id: str,
        interaction_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return scheduler_interactions.resolve_interaction(self, owner_id, thread_id, turn_id, interaction_id, body)

    def metrics(self) -> dict[str, int | float]:
        with self._metrics_lock:
            metrics = dict(self._metrics)
        metrics.update(self.pool.metrics())
        audit_counts = self.state.count_audit_actions()
        metrics["auth_starts"] = (
            audit_counts.get("auth.device.start", 0)
            + audit_counts.get("auth.api_key.start", 0)
            + audit_counts.get("auth.probe.start", 0)
        )
        metrics["auth_successes"] = (
            audit_counts.get("auth.device.success", 0)
            + audit_counts.get("auth.api_key.success", 0)
            + audit_counts.get("auth.probe.success", 0)
        )
        metrics["auth_failures"] = (
            audit_counts.get("auth.device.failure", 0)
            + audit_counts.get("auth.api_key.failure", 0)
            + audit_counts.get("auth.probe.failure", 0)
        )
        for action, count in audit_counts.items():
            metrics[f"audit_{metric_key(action)}"] = count
        return metrics

    def note_recovered_turns(self, count: int) -> None:
        if count > 0:
            self._metric("turns_recovered", count)
            self._metric("turns_failed", count)

    def note_pruned_raw_events(self, count: int) -> None:
        if count > 0:
            self._metric("raw_events_pruned", count)

    def note_http_request(self, endpoint: str, status: int, elapsed_seconds: float) -> None:
        key = metric_key(endpoint) or "root"
        self._metric("http_requests_total", 1)
        self._metric(f"http_requests_{key}_status_{status}", 1)
        self._metric("http_request_duration_seconds_count", 1)
        self._metric("http_request_duration_seconds_sum", elapsed_seconds)
        self._metric(f"http_request_duration_seconds_count_{key}", 1)
        self._metric(f"http_request_duration_seconds_sum_{key}", elapsed_seconds)

    def note_event_stream_disconnect(self) -> None:
        self._metric("event_stream_disconnects", 1)

    def note_turn_duration(self, host_app: str | None, bundle_id: str | None, elapsed_seconds: float) -> None:
        self._metric("turn_duration_seconds_count", 1)
        self._metric("turn_duration_seconds_sum", elapsed_seconds)
        host_key = metric_key(host_app) if host_app else None
        if host_key:
            self._metric(f"turn_duration_seconds_count_host_app_{host_key}", 1)
            self._metric(f"turn_duration_seconds_sum_host_app_{host_key}", elapsed_seconds)
        if bundle_id:
            bundle_key = metric_key(bundle_id)
            self._metric(f"turn_duration_seconds_count_bundle_{bundle_key}", 1)
            self._metric(f"turn_duration_seconds_sum_bundle_{bundle_key}", elapsed_seconds)
            if host_key:
                self._metric(f"turn_duration_seconds_count_host_app_{host_key}_bundle_{bundle_key}", 1)
                self._metric(f"turn_duration_seconds_sum_host_app_{host_key}_bundle_{bundle_key}", elapsed_seconds)

    def shutdown(self, mode: str = "interrupt", timeout_seconds: float = 30) -> None:
        if mode not in {"interrupt", "drain"}:
            mode = "interrupt"
        self._shutdown_mode = mode
        self._shutdown.set()
        json_log(self.config.json_logs, "broker.shutdown.start", mode=mode, timeoutSeconds=timeout_seconds)
        if mode == "interrupt":
            self._cancel_pending_futures("Broker shutting down.")
            self._cancel_queued_turns("Broker shutting down.")
            self._interrupt_active_contexts("Broker shutting down.")
        self._wait_for_workers(timeout_seconds)
        if mode == "drain" and self._worker_count() > 0:
            self._shutdown_mode = "interrupt"
            self._cancel_pending_futures("Broker shutdown drain timed out.")
            self._cancel_queued_turns("Broker shutdown drain timed out.")
            self._interrupt_active_contexts("Broker shutdown drain timed out.")
            self._wait_for_workers(min(timeout_seconds, 5))
        self._executor.shutdown(wait=False, cancel_futures=True)
        json_log(self.config.json_logs, "broker.shutdown.finish", mode=mode, remainingWorkers=self._worker_count())

    def _interrupt_active_contexts(self, message: str) -> None:
        for context in self._active_contexts():
            if context.completed_event.is_set():
                continue
            if context.client and context.codex_thread_id:
                params: dict[str, Any] = {"threadId": context.codex_thread_id}
                if context.codex_turn_id:
                    params["turnId"] = context.codex_turn_id
                try:
                    context.client.request("turn/interrupt", params, timeout=5)
                except AppServerError:
                    pass
            context.finish("interrupted", message, "turn.interrupted")
            if self._persist_context_terminal(
                context,
                audit_action="turn.interrupt",
                audit_payload={"reason": "shutdown"},
            ):
                self._metric("turns_interrupted", 1)

    def _active_contexts(self) -> list[BrokerTurnContext]:
        with self._gates_lock:
            return [gate.active_context for gate in self._gates.values() if gate.active_context is not None]

    def _wait_for_workers(self, timeout_seconds: float) -> None:
        with self._workers_lock:
            workers = set(self._workers)
        if workers:
            wait(workers, timeout=max(timeout_seconds, 0))

    def _worker_count(self) -> int:
        with self._workers_lock:
            self._workers = {worker for worker in self._workers if not worker.done()}
            return len(self._workers)

    def _submit_work(self, work: QueuedTurn) -> None:
        future = self._executor.submit(self._execute_work, work)
        with self._workers_lock:
            self._workers.add(future)
            self._future_work[future] = work
        future.add_done_callback(self._work_done)

    def _execute_work(self, work: QueuedTurn) -> None:
        try:
            self._run_turn(work.owner_hash, work.thread_id, work.turn_id, work.body)
        finally:
            self._advance_queue(work.owner_hash, work.thread_id)

    def _work_done(self, future: Future[None]) -> None:
        with self._workers_lock:
            self._workers.discard(future)
            self._future_work.pop(future, None)
        with self._gates_lock:
            self._outstanding_turns = max(0, self._outstanding_turns - 1)

    def _advance_queue(self, owner_hash: str, thread_id: str) -> None:
        with self._gates_lock:
            gate = self._gate_locked(owner_hash, thread_id)
            if self._shutdown.is_set() and self._shutdown_mode == "interrupt":
                gate.running = False
                return
            pending = gate.pending()
            if not pending:
                gate.running = False
                return
            work = pending.popleft()
            self._metric("queued_turns", -1)
            self.state.update_turn(owner_hash, thread_id, work.turn_id, status="starting")
            self._submit_work(work)

    def _cancel_pending_futures(self, message: str) -> None:
        with self._workers_lock:
            scheduled = list(self._future_work.items())
        for future, work in scheduled:
            if not future.cancel():
                continue
            self._finalize_interrupted(work.owner_hash, work.thread_id, work.turn_id, message)
            with self._gates_lock:
                self._gate_locked(work.owner_hash, work.thread_id).running = False

    def _cancel_queued_turns(self, message: str) -> None:
        cancelled: list[QueuedTurn] = []
        with self._gates_lock:
            for gate in self._gates.values():
                pending = gate.pending()
                while pending:
                    cancelled.append(pending.popleft())
        if cancelled:
            self._metric("queued_turns", -len(cancelled))
        for work in cancelled:
            self._finalize_interrupted(work.owner_hash, work.thread_id, work.turn_id, message)

    def _finalize_interrupted(self, owner_hash: str, thread_id: str, turn_id: str, message: str) -> None:
        turn = self.state.get_turn(owner_hash, thread_id, turn_id)
        finalized = self.state.finalize_turn(
            owner_hash,
            thread_id,
            turn_id,
            status="interrupted",
            error=message,
            event_type="turn.interrupted",
            event_payload={"message": message},
            auth_principal_hash=turn.get("auth_principal_hash") if turn else owner_hash,
            audit_action="turn.interrupt",
            audit_payload={"reason": "shutdown"},
        )
        if finalized:
            self._metric("turns_interrupted", 1)

    def _persist_context_terminal(
        self,
        context: BrokerTurnContext,
        *,
        audit_action: str | None = None,
        audit_payload: dict[str, Any] | None = None,
    ) -> bool:
        status = context.final_status or "completed"
        return self.state.finalize_turn(
            context.owner_hash,
            context.thread_id,
            context.turn_id,
            status=status,
            error=context.error_text,
            error_code=context.error_code,
            public_message=context.public_message,
            admin_message=context.admin_message,
            event_type=context.terminal_event_type or ("turn.completed" if status == "completed" else "turn.failed"),
            event_payload=context.terminal_payload or {},
            auth_principal_hash=context.auth_principal_hash,
            product_correlation_id=context.product_correlation_id,
            codex_thread_id=context.codex_thread_id,
            codex_turn_id=context.codex_turn_id,
            raw_method=context.terminal_raw_method,
            raw_params=context.terminal_raw_params,
            ambiguous=context.terminal_ambiguous,
            audit_action=audit_action,
            audit_payload=audit_payload,
        )

    def _run_turn(self, owner_hash: str, thread_id: str, turn_id: str, body: dict[str, Any]) -> None:
        gate = self._gate(owner_hash, thread_id)
        active_metric_incremented = False
        overlay: Path | None = None
        context: BrokerTurnContext | None = None
        client: AppServerClient | None = None
        bundle: ResolvedBundle | None = None
        profile: str | None = None
        turn_started_at: float | None = None
        duration_bundle_id: str | None = None
        duration_host_app: str | None = None
        try:
            if self._shutdown.is_set() and self._shutdown_mode == "interrupt":
                self._finalize_interrupted(owner_hash, thread_id, turn_id, "Broker shutting down.")
                return
            self._metric("active_turns", 1)
            active_metric_incremented = True
            self._metric("turns_started", 1)
            turn = self.state.get_turn(owner_hash, thread_id, turn_id)
            thread = self.state.get_thread(owner_hash, thread_id)
            if not turn or not thread:
                raise NotFoundError("Turn or thread not found.")
            turn_started_at = time.monotonic()
            duration_bundle_id = turn.get("bundle_id")
            duration_host_app = turn.get("host_app")
            self.state.update_turn(owner_hash, thread_id, turn_id, status="running", started=True)
            self.state.append_audit(
                owner_hash,
                "turn.start",
                {
                    "bundleId": turn.get("bundle_id"),
                    "hostApp": turn.get("host_app"),
                    "configProfile": turn.get("config_profile"),
                },
                auth_principal_hash=str(turn["auth_principal_hash"]),
                profile=str(turn["profile"]),
                thread_id=thread_id,
                turn_id=turn_id,
            )
            json_log(
                self.config.json_logs,
                "turn.start",
                ownerHash=owner_hash,
                authPrincipalHash=turn.get("auth_principal_hash"),
                threadId=thread_id,
                turnId=turn_id,
                bundleId=turn.get("bundle_id"),
                hostApp=turn.get("host_app"),
                configProfile=turn.get("config_profile"),
                productCorrelationId=turn.get("product_correlation_id"),
            )
            bundle = self.bundles.resolve(turn.get("bundle_id")) if turn.get("bundle_id") else None
            config_profile_config = self._config_profile_config(str(turn["config_profile"]))
            overlay = (
                self.bundles.materialize(
                    bundle,
                    turn_id,
                    adapter_context={
                        "ownerHash": owner_hash,
                        "authPrincipalHash": turn.get("auth_principal_hash"),
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "hostApp": turn.get("host_app"),
                        "productCorrelationId": turn.get("product_correlation_id"),
                        "configProfile": turn.get("config_profile"),
                        "profile": turn.get("profile"),
                    },
                )
                if bundle
                else None
            )
            input_items = self._build_input(turn["input"], bundle)
            cwd = Path(turn["cwd"]).resolve() if turn.get("cwd") else overlay
            if cwd:
                self.bundles.validate_cwd(str(cwd), bundle)
                self._validate_config_profile_cwd(cwd, config_profile_config)
            profile = str(turn["profile"])
            auth_principal_hash = str(turn["auth_principal_hash"])
            with self.auth.profile_guard(auth_principal_hash, profile):
                self._validate_execution_auth_binding(thread, turn)
                codex_home = self.auth.profile_home(auth_principal_hash, profile)
                self._validate_execution_auth_binding(thread, turn)
                mcp_servers = self.bundles.mcp_servers_for_bundle(bundle, overlay) if bundle else ()
                codex_config_args = self._codex_process_config_args(body, config_profile_config)
                auth_fingerprint = self.auth.auth_fingerprint(auth_principal_hash, profile)
                client = self.pool.get(
                    auth_principal_hash=auth_principal_hash,
                    profile=profile,
                    codex_home=codex_home,
                    config_profile=str(turn["config_profile"]),
                    mcp_servers=mcp_servers,
                    tenant_scope_hash=owner_hash,
                    codex_config_args=codex_config_args,
                    auth_fingerprint=auth_fingerprint,
                )
            context = BrokerTurnContext(
                state=self.state,
                owner_hash=owner_hash,
                auth_principal_hash=auth_principal_hash,
                thread_id=thread_id,
                turn_id=turn_id,
                codex_thread_id=thread.get("codex_thread_id"),
                product_correlation_id=turn.get("product_correlation_id"),
                debug_raw_events=self.config.debug_raw_events,
            )
            context.client = client
            with self._gates_lock:
                gate.active_context = context
            client.register_context(context)
            codex_thread_id = self._ensure_codex_thread(client, context, thread, cwd, body, bundle, config_profile_config)
            params = self._turn_params(codex_thread_id, input_items, body, config_profile_config)
            result = client.request("turn/start", params)
            turn_data = result.get("turn") if isinstance(result.get("turn"), dict) else {}
            if turn_data.get("id"):
                client.register_turn_for_context(context, str(turn_data["id"]))
            timeout = self.config.turn_timeout_seconds if self.config.turn_timeout_seconds > 0 else None
            if not context.completed_event.wait(timeout):
                try:
                    client.request("turn/interrupt", {"threadId": codex_thread_id})
                except AppServerError:
                    pass
                context.fail("Turn timed out.")
                context.final_status = "timed_out"
                if self._persist_context_terminal(context):
                    self._metric("turns_failed", 1)
                return
            status = context.final_status or "completed"
            finalized = self._persist_context_terminal(context)
            if context.error_code == CODEX_AUTH_REQUIRES_ADMIN:
                self.auth.mark_runtime_auth_failure(
                    owner_hash,
                    auth_principal_hash,
                    profile,
                    code=context.error_code,
                    admin_message=context.admin_message or context.error_text or "",
                )
                self.pool.close_profile(auth_principal_hash, profile)
            if finalized:
                self._metric("turns_completed" if status == "completed" else "turns_failed", 1)
        except Exception as exc:  # noqa: BLE001 - background worker must persist failure state.
            message = str(exc)
            error_info = classify_runtime_error(message)
            turn = self.state.get_turn(owner_hash, thread_id, turn_id)
            finalized = self.state.finalize_turn(
                owner_hash,
                thread_id,
                turn_id,
                status="failed",
                error=error_info.public_message,
                error_code=error_info.code,
                public_message=error_info.public_message,
                admin_message=error_info.admin_message,
                event_type="turn.failed",
                event_payload=error_info.public_payload(),
                auth_principal_hash=turn.get("auth_principal_hash") if turn else owner_hash,
                product_correlation_id=turn.get("product_correlation_id") if turn else None,
                codex_thread_id=thread.get("codex_thread_id") if thread else None,
                codex_turn_id=turn.get("codex_turn_id") if turn else None,
            )
            if error_info.code == CODEX_AUTH_REQUIRES_ADMIN and profile:
                auth_principal_hash = str(turn["auth_principal_hash"]) if turn else owner_hash
                self.auth.mark_runtime_auth_failure(
                    owner_hash,
                    auth_principal_hash,
                    profile,
                    code=error_info.code,
                    admin_message=error_info.admin_message,
                )
                self.pool.close_profile(auth_principal_hash, profile)
            if finalized:
                self._metric("turns_failed", 1)
        finally:
            if context and client:
                client.unregister_context(context)
            if client and bundle and bundle.hosted_tools:
                self.pool.close_client(client)
            with self._gates_lock:
                if gate.active_context is context:
                    gate.active_context = None
            if active_metric_incremented:
                self._metric("active_turns", -1)
            if overlay:
                self.bundles.cleanup_overlay(turn_id)
            if turn_started_at is not None:
                elapsed = time.monotonic() - turn_started_at
                self.note_turn_duration(duration_host_app, duration_bundle_id, elapsed)
                final_turn = self.state.get_turn(owner_hash, thread_id, turn_id)
                json_log(
                    self.config.json_logs,
                    "turn.finish",
                    ownerHash=owner_hash,
                    authPrincipalHash=final_turn.get("auth_principal_hash") if final_turn else None,
                    threadId=thread_id,
                    turnId=turn_id,
                    status=final_turn.get("status") if final_turn else None,
                    error=final_turn.get("error") if final_turn else None,
                    bundleId=final_turn.get("bundle_id") if final_turn else duration_bundle_id,
                    hostApp=final_turn.get("host_app") if final_turn else duration_host_app,
                    configProfile=final_turn.get("config_profile") if final_turn else None,
                    productCorrelationId=final_turn.get("product_correlation_id") if final_turn else None,
                    durationMs=round(elapsed * 1000, 3),
                )

    def _validate_execution_auth_binding(
        self,
        thread: dict[str, Any],
        turn: dict[str, Any],
    ) -> None:
        for key in ("auth_principal_hash", "auth_profile_instance_id", "profile"):
            if turn.get(key) != thread.get(key):
                raise ConflictError("Turn authentication binding does not match its broker thread.")
        profile = self.state.get_profile(str(turn["auth_principal_hash"]), str(turn["profile"]))
        if not profile or profile.get("instance_id") != turn.get("auth_profile_instance_id"):
            raise ConflictError("The Codex account for this auth profile was removed or replaced; start a new broker thread.")

    def _ensure_codex_thread(
        self,
        client: AppServerClient,
        context: BrokerTurnContext,
        thread: dict[str, Any],
        cwd: Path | None,
        body: dict[str, Any],
        bundle: ResolvedBundle | None,
        config_profile_config: dict[str, Any],
    ) -> str:
        params = self._thread_params(cwd, body, bundle, config_profile_config)
        codex_thread_id = thread.get("codex_thread_id")
        if codex_thread_id:
            client.request("thread/resume", {"threadId": codex_thread_id, **params})
            client.register_thread_for_context(context, str(codex_thread_id))
            return str(codex_thread_id)
        result = client.request("thread/start", params)
        thread_data = result.get("thread") if isinstance(result.get("thread"), dict) else {}
        codex_thread_id = str(thread_data.get("id") or "")
        if not codex_thread_id:
            raise AppServerError("App Server did not return a thread id.")
        client.register_thread_for_context(context, codex_thread_id)
        return codex_thread_id

    def _thread_params(
        self,
        cwd: Path | None,
        body: dict[str, Any],
        bundle: ResolvedBundle | None,
        config_profile_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return scheduler_config.thread_params(self, cwd, body, bundle, config_profile_config)

    def _turn_params(
        self,
        codex_thread_id: str,
        input_items: list[dict[str, Any]],
        body: dict[str, Any],
        config_profile_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return scheduler_config.turn_params(self, codex_thread_id, input_items, body, config_profile_config)

    def _build_input(self, input_items: list[dict[str, Any]], bundle: ResolvedBundle | None) -> list[dict[str, Any]]:
        return scheduler_config.build_input(input_items, bundle)

    def _config_profile_config(self, config_profile: str) -> dict[str, Any]:
        return scheduler_config.config_profile_config(self, config_profile)

    def _validate_config_profile_bundle(self, profile_config: dict[str, Any], bundle_id: str | None) -> None:
        scheduler_config.validate_config_profile_bundle(profile_config, bundle_id)

    def _validate_config_profile_cwd(self, cwd: Path | None, profile_config: dict[str, Any]) -> None:
        scheduler_config.validate_config_profile_cwd(self, cwd, profile_config)

    @staticmethod
    def _request_config_profile(body: dict[str, Any], fallback: Any = "default") -> str:
        return scheduler_config.request_config_profile(body, fallback)

    @staticmethod
    def _request_codex_options(body: dict[str, Any]) -> dict[str, Any]:
        return scheduler_config.request_codex_options(body)

    def _codex_process_config_args(
        self,
        body: dict[str, Any],
        profile_config: dict[str, Any] | None = None,
    ) -> tuple[tuple[str, str], ...]:
        return scheduler_config.codex_process_config_args(self, body, profile_config)

    @staticmethod
    def _codex_option(codex_options: dict[str, Any], profile_config: dict[str, Any], key: str, *aliases: str) -> Any:
        return scheduler_config.codex_option(codex_options, profile_config, key, *aliases)

    @staticmethod
    def _format_codex_config_value(value: Any) -> str:
        return scheduler_config.format_codex_config_value(value)

    def _gate(self, owner_hash: str, thread_id: str) -> ThreadGate:
        with self._gates_lock:
            return self._gate_locked(owner_hash, thread_id)

    def _gate_locked(self, owner_hash: str, thread_id: str) -> ThreadGate:
        key = (owner_hash, thread_id)
        gate = self._gates.get(key)
        if not gate:
            gate = ThreadGate()
            self._gates[key] = gate
        return gate

    def _active_context(self, owner_hash: str, thread_id: str) -> BrokerTurnContext | None:
        return self._gate(owner_hash, thread_id).active_context

    def _steer_active(self, owner_hash: str, thread_id: str, input_items: list[dict[str, Any]]) -> dict[str, Any] | None:
        active = self._active_context(owner_hash, thread_id)
        if not active or not active.client or not active.codex_thread_id:
            return None
        params: dict[str, Any] = {"threadId": active.codex_thread_id, "input": input_items}
        if active.codex_turn_id:
            params["turnId"] = active.codex_turn_id
        active.client.request("turn/steer", params)
        self.state.append_event(
            owner_hash,
            thread_id,
            active.turn_id,
            "message.delta",
            {"steered": True, "input": input_items},
            product_correlation_id=active.product_correlation_id,
            codex_thread_id=active.codex_thread_id,
            codex_turn_id=active.codex_turn_id,
        )
        turn = self.state.get_turn(owner_hash, thread_id, active.turn_id)
        return self._public_turn(turn) if turn else None

    def _metric(self, key: str, delta: int | float) -> None:
        with self._metrics_lock:
            self._metrics[key] = max(0, self._metrics.get(key, 0) + delta)

    def _public_thread(self, thread: dict[str, Any]) -> dict[str, Any]:
        return scheduler_threads.public_thread(thread)

    def _public_turn(self, turn: dict[str, Any]) -> dict[str, Any]:
        return scheduler_threads.public_turn(turn)

    def _stream_url(self, owner_id: str, thread_id: str, turn_id: str) -> str:
        return f"/v1/owners/{quote(owner_id, safe='')}/threads/{quote(thread_id, safe='')}/events?turnId={quote(turn_id, safe='')}"
