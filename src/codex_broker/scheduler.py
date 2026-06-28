from __future__ import annotations

import threading
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .app_server import AppServerClient, AppServerError, AppServerPool
from .auth import AuthManager
from .bundles import BundleError, BundleRegistry, ResolvedBundle
from .config import BrokerConfig
from .events import normalize_app_server_event
from .state import StateStore
from .util import json_dumps, json_log, redact_json, utc_now


class ActiveTurnError(RuntimeError):
    pass


class NotFoundError(RuntimeError):
    pass


class ConflictError(RuntimeError):
    pass


def metric_key(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", value).strip("_").lower()


def feature_config_key(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", value).strip("._-")
    if not name:
        raise ValueError("Feature name must contain at least one alphanumeric character.")
    return f"features.{name}"


def optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


@dataclass
class ThreadGate:
    lock: threading.Lock
    active_context: "BrokerTurnContext | None" = None


class BrokerTurnContext:
    def __init__(
        self,
        *,
        state: StateStore,
        owner_hash: str,
        thread_id: str,
        turn_id: str,
        codex_thread_id: str | None,
        product_correlation_id: str | None,
        debug_raw_events: bool,
    ) -> None:
        self.state = state
        self.owner_hash = owner_hash
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.codex_thread_id = codex_thread_id
        self.product_correlation_id = product_correlation_id
        self.codex_turn_id: str | None = None
        self.client: AppServerClient | None = None
        self.completed_event = threading.Event()
        self.final_status: str | None = None
        self.error_text: str | None = None
        self.debug_raw_events = debug_raw_events
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
        self._append_event(event_type, payload, method, params, ambiguous=ambiguous)
        if event_type in {"approval.requested", "approval.resolved"}:
            self.state.append_audit(
                self.owner_hash,
                event_type,
                payload,
                thread_id=self.thread_id,
                turn_id=self.turn_id,
            )
        if method == "turn/completed":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            status = str(turn.get("status") or "completed")
            error = turn.get("error")
            self.final_status = "completed" if status == "completed" else "failed"
            self.error_text = json_dumps(error) if error else None
            self.completed_event.set()

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
            self.final_status = status
            self.error_text = message
            self.state.append_event(
                self.owner_hash,
                self.thread_id,
                self.turn_id,
                event_type or ("turn.failed" if status != "interrupted" else "turn.interrupted"),
                {"message": message},
                product_correlation_id=self.product_correlation_id,
                codex_thread_id=self.codex_thread_id,
                codex_turn_id=self.codex_turn_id,
            )
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
        self._workers: set[threading.Thread] = set()
        self._workers_lock = threading.RLock()
        self._global_semaphore = (
            threading.BoundedSemaphore(config.max_active_turns) if config.max_active_turns > 0 else None
        )
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
        owner_hash = self.auth.hash_owner(owner_id)
        profile = self.auth.profile_key(body.get("profile") if body.get("profile") is not None else "default")
        config_profile = self._request_config_profile(body)
        config_profile_config = self._config_profile_config(config_profile)
        host_app = optional_text(body.get("hostApp"))
        if "productThreadId" in body:
            raise ValueError("productThreadId has been removed; pass threadId instead.")
        requested_thread_id = optional_text(body.get("threadId"))
        if requested_thread_id:
            existing = self.state.get_thread(owner_hash, requested_thread_id)
            if existing:
                return self._public_thread(existing)
        bundle_id = str(body["bundleId"]) if body.get("bundleId") else None
        self._validate_config_profile_bundle(config_profile_config, bundle_id)
        bundle = self.bundles.resolve(bundle_id) if bundle_id else None
        cwd = self.bundles.validate_cwd(body.get("cwd"), bundle)
        self._validate_config_profile_cwd(cwd, config_profile_config)
        self.auth.profile_home(owner_hash, profile)
        thread = self.state.create_thread(
            owner_hash,
            thread_id=requested_thread_id,
            profile=profile,
            config_profile=config_profile,
            host_app=host_app,
            bundle_id=str(bundle_id) if bundle_id else None,
            cwd=str(cwd) if cwd else None,
        )
        return self._public_thread(thread)

    def get_thread(self, owner_id: str, thread_id: str) -> dict[str, Any]:
        owner_hash = self.auth.hash_owner(owner_id)
        thread = self.state.get_thread(owner_hash, thread_id)
        if not thread:
            raise NotFoundError("Thread not found.")
        return self._public_thread(thread)

    def archive_thread(self, owner_id: str, thread_id: str) -> dict[str, Any]:
        owner_hash = self.auth.hash_owner(owner_id)
        thread = self.state.archive_thread(owner_hash, thread_id)
        if not thread:
            raise NotFoundError("Thread not found.")
        return self._public_thread(thread)

    def start_turn(self, owner_id: str, thread_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if self._shutdown.is_set():
            raise ConflictError("Broker is shutting down.")
        owner_hash = self.auth.hash_owner(owner_id)
        thread = self.state.get_thread(owner_hash, thread_id)
        if not thread:
            raise NotFoundError("Thread not found.")
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
        gate = self._gate(owner_hash, thread_id)
        preacquired = False
        if mode == "reject":
            preacquired = gate.lock.acquire(blocking=False)
            if not preacquired:
                active = gate.active_context
                if active and active.completed_event.is_set():
                    preacquired = gate.lock.acquire(timeout=1)
                if not preacquired:
                    raise ActiveTurnError("active_turn_exists")
        elif mode == "queue":
            self._metric("queued_turns", 1)
        bundle_id = str(body.get("bundleId") or thread.get("bundle_id") or "") or None
        config_profile = self._request_config_profile(body, thread.get("config_profile") or "default")
        config_profile_config = self._config_profile_config(config_profile)
        host_app = optional_text(body.get("hostApp")) or thread.get("host_app")
        profile = self.auth.profile_key(body.get("profile") if body.get("profile") is not None else thread.get("profile") or "default")
        self._validate_config_profile_bundle(config_profile_config, bundle_id)
        bundle = self.bundles.resolve(bundle_id) if bundle_id else None
        cwd = self.bundles.validate_cwd(body.get("cwd") or thread.get("cwd"), bundle)
        self._validate_config_profile_cwd(cwd, config_profile_config)
        turn = self.state.create_turn(
            owner_hash,
            thread_id,
            profile=profile,
            config_profile=config_profile,
            host_app=host_app,
            bundle_id=bundle_id,
            cwd=str(cwd) if cwd else None,
            mode=mode,
            input_items=input_items,
            idempotency_key=key if isinstance(key, str) and key else None,
            product_correlation_id=correlation_id if isinstance(correlation_id, str) and correlation_id else None,
            status="starting" if preacquired else "queued",
        )
        worker = threading.Thread(
            target=self._run_turn,
            args=(owner_hash, thread_id, turn["turn_id"], body, preacquired),
            daemon=True,
        )
        with self._workers_lock:
            self._workers.add(worker)
        worker.start()
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
        self.state.update_turn(owner_hash, thread_id, turn_id, status="interrupted", completed=True)
        self.state.append_audit(owner_hash, "turn.interrupt", {}, thread_id=thread_id, turn_id=turn_id)
        self._metric("turns_interrupted", 1)
        return self.get_turn(owner_id, thread_id, turn_id)

    def metrics(self) -> dict[str, int | float]:
        with self._metrics_lock:
            metrics = dict(self._metrics)
        metrics.update(self.pool.metrics())
        audit_counts = self.state.count_audit_actions()
        metrics["auth_starts"] = (
            audit_counts.get("auth.device.start", 0)
            + audit_counts.get("auth.api_key.start", 0)
        )
        metrics["auth_successes"] = (
            audit_counts.get("auth.device.success", 0)
            + audit_counts.get("auth.api_key.success", 0)
        )
        metrics["auth_failures"] = (
            audit_counts.get("auth.device.failure", 0)
            + audit_counts.get("auth.api_key.failure", 0)
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
            self._interrupt_active_contexts("Broker shutting down.")
        self._wait_for_workers(timeout_seconds)
        if mode == "drain" and self._worker_count() > 0:
            self._interrupt_active_contexts("Broker shutdown drain timed out.")
            self._wait_for_workers(min(timeout_seconds, 5))
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
            self.state.update_turn(context.owner_hash, context.thread_id, context.turn_id, status="interrupted", error=message, completed=True)
            self.state.append_audit(context.owner_hash, "turn.interrupt", {"reason": "shutdown"}, thread_id=context.thread_id, turn_id=context.turn_id)
            self._metric("turns_interrupted", 1)

    def _active_contexts(self) -> list[BrokerTurnContext]:
        with self._gates_lock:
            return [gate.active_context for gate in self._gates.values() if gate.active_context is not None]

    def _wait_for_workers(self, timeout_seconds: float) -> None:
        deadline = time.monotonic() + max(timeout_seconds, 0)
        while True:
            with self._workers_lock:
                workers = [worker for worker in self._workers if worker.is_alive()]
                self._workers = set(workers)
            if not workers:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            for worker in workers:
                worker.join(timeout=min(0.1, remaining))

    def _worker_count(self) -> int:
        with self._workers_lock:
            self._workers = {worker for worker in self._workers if worker.is_alive()}
            return len(self._workers)

    def _run_turn(self, owner_hash: str, thread_id: str, turn_id: str, body: dict[str, Any], preacquired: bool) -> None:
        gate = self._gate(owner_hash, thread_id)
        acquired_global = False
        acquired_gate = preacquired
        active_metric_incremented = False
        overlay: Path | None = None
        context: BrokerTurnContext | None = None
        client: AppServerClient | None = None
        bundle: ResolvedBundle | None = None
        turn_started_at: float | None = None
        duration_bundle_id: str | None = None
        duration_host_app: str | None = None
        try:
            if self._global_semaphore:
                self._global_semaphore.acquire()
                acquired_global = True
            if not preacquired:
                gate.lock.acquire()
                acquired_gate = True
                self._metric("queued_turns", -1)
            self._metric("active_turns", 1)
            active_metric_incremented = True
            self._metric("turns_started", 1)
            turn = self.state.get_turn(owner_hash, thread_id, turn_id)
            thread = self.state.get_thread(owner_hash, thread_id)
            if not turn or not thread:
                raise NotFoundError("Turn or thread not found.")
            if self._shutdown.is_set() and self._shutdown_mode == "interrupt":
                raise RuntimeError("Broker shutting down.")
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
                profile=str(turn["profile"]),
                thread_id=thread_id,
                turn_id=turn_id,
            )
            json_log(
                self.config.json_logs,
                "turn.start",
                ownerHash=owner_hash,
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
            codex_home = self.auth.profile_home(owner_hash, profile)
            mcp_servers = self.bundles.mcp_servers_for_bundle(bundle, overlay) if bundle else ()
            codex_config_args = self._codex_process_config_args(body, config_profile_config)
            client = self.pool.get(
                owner_hash=owner_hash,
                profile=profile,
                codex_home=codex_home,
                config_profile=str(turn["config_profile"]),
                mcp_servers=mcp_servers,
                codex_config_args=codex_config_args,
            )
            context = BrokerTurnContext(
                state=self.state,
                owner_hash=owner_hash,
                thread_id=thread_id,
                turn_id=turn_id,
                codex_thread_id=thread.get("codex_thread_id"),
                product_correlation_id=turn.get("product_correlation_id"),
                debug_raw_events=self.config.debug_raw_events,
            )
            context.client = client
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
                self.state.update_turn(owner_hash, thread_id, turn_id, status="timed_out", error="Turn timed out.", completed=True)
                self._metric("turns_failed", 1)
                return
            status = context.final_status or "completed"
            self.state.update_turn(
                owner_hash,
                thread_id,
                turn_id,
                status=status,
                error=context.error_text,
                completed=True,
            )
            self._metric("turns_completed" if status == "completed" else "turns_failed", 1)
        except Exception as exc:  # noqa: BLE001 - background worker must persist failure state.
            message = str(exc)
            self.state.update_turn(owner_hash, thread_id, turn_id, status="failed", error=message, completed=True)
            turn = self.state.get_turn(owner_hash, thread_id, turn_id)
            self.state.append_event(
                owner_hash,
                thread_id,
                turn_id,
                "turn.failed",
                {"message": message},
                product_correlation_id=turn.get("product_correlation_id") if turn else None,
                codex_thread_id=thread.get("codex_thread_id") if thread else None,
                codex_turn_id=turn.get("codex_turn_id") if turn else None,
            )
            self._metric("turns_failed", 1)
        finally:
            if context and client:
                client.unregister_context(context)
            if client and bundle and bundle.hosted_tools:
                self.pool.close_client(client)
            gate.active_context = None
            if acquired_gate:
                try:
                    gate.lock.release()
                except RuntimeError:
                    pass
            if acquired_global and self._global_semaphore:
                self._global_semaphore.release()
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
            with self._workers_lock:
                self._workers.discard(threading.current_thread())

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
        codex_options = self._request_codex_options(body)
        profile_config = config_profile_config or {}
        params: dict[str, Any] = {}
        if cwd:
            params["cwd"] = str(cwd)
        if self._codex_option(codex_options, profile_config, "approvalPolicy") is not None:
            params["approvalPolicy"] = self._codex_option(codex_options, profile_config, "approvalPolicy")
        if codex_options.get("sandbox") or bundle and bundle.sandbox_mode or profile_config.get("sandbox") is not None:
            params["sandbox"] = codex_options.get("sandbox") or (bundle.sandbox_mode if bundle and bundle.sandbox_mode else profile_config.get("sandbox"))
        if self._codex_option(codex_options, profile_config, "model") is not None:
            params["model"] = self._codex_option(codex_options, profile_config, "model")
        if self._codex_option(codex_options, profile_config, "personality") is not None:
            params["personality"] = self._codex_option(codex_options, profile_config, "personality")
        return params

    def _turn_params(
        self,
        codex_thread_id: str,
        input_items: list[dict[str, Any]],
        body: dict[str, Any],
        config_profile_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        codex_options = self._request_codex_options(body)
        profile_config = config_profile_config or {}
        params: dict[str, Any] = {"threadId": codex_thread_id, "input": input_items}
        for request_key, app_server_key, aliases in (
            ("serviceTier", "serviceTier", ()),
            ("model", "model", ()),
            ("effort", "effort", ("reasoningEffort",)),
            ("personality", "personality", ()),
            ("summary", "summary", ("reasoningSummary",)),
        ):
            value = self._codex_option(codex_options, profile_config, request_key, *aliases)
            if value is not None:
                params[app_server_key] = value
        return params

    def _build_input(self, input_items: list[dict[str, Any]], bundle: ResolvedBundle | None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if bundle:
            for skill in bundle.skills:
                items.append({"type": "skill", "name": skill.name, "path": str(skill.path)})
            if bundle.instructions:
                items.append({"type": "text", "text": "\n\n".join(bundle.instructions), "text_elements": []})
            for prompt in bundle.prompts:
                text = prompt.path.read_text(encoding="utf-8")
                items.append({"type": "text", "text": text, "text_elements": [], "name": prompt.name})
        items.extend(input_items)
        return items

    def _config_profile_config(self, config_profile: str) -> dict[str, Any]:
        if not self.config.config_profiles:
            return {}
        profile = self.config.config_profiles.get(config_profile)
        if profile is None:
            raise ValueError(f"Unknown configuration profile: {config_profile}")
        return profile

    def _validate_config_profile_bundle(self, profile_config: dict[str, Any], bundle_id: str | None) -> None:
        enabled = (
            profile_config.get("enabledBundles")
            if profile_config.get("enabledBundles") is not None
            else profile_config.get("bundleIds") if profile_config.get("bundleIds") is not None else profile_config.get("bundles")
        )
        if enabled is None or bundle_id is None:
            return
        allowed = {str(value) for value in enabled} if isinstance(enabled, list) else {str(enabled)}
        if bundle_id not in allowed:
            raise BundleError(f"Bundle {bundle_id} is not enabled for configuration profile.")

    def _validate_config_profile_cwd(self, cwd: Path | None, profile_config: dict[str, Any]) -> None:
        if cwd is None:
            return
        roots = profile_config.get("allowedWorkspaceRoots", profile_config.get("workspaceRoots"))
        if roots is None:
            return
        raw_roots = roots if isinstance(roots, list) else [roots]
        allowed_roots = [Path(str(value)).expanduser().resolve() for value in raw_roots]
        allowed_roots.append(self.config.overlay_root)
        if not any(cwd.resolve().is_relative_to(root) for root in allowed_roots):
            raise BundleError(f"cwd is outside configuration profile workspace roots: {cwd}")

    @staticmethod
    def _request_config_profile(body: dict[str, Any], fallback: Any = "default") -> str:
        return str(body.get("configProfile") or body.get("runtimeProfile") or fallback or "default")

    @staticmethod
    def _request_codex_options(body: dict[str, Any]) -> dict[str, Any]:
        options: dict[str, Any] = {}
        runtime = body.get("runtime")
        if isinstance(runtime, dict):
            options.update(runtime)
        codex_options = body.get("codexOptions")
        if isinstance(codex_options, dict):
            options.update(codex_options)
        return options

    def _codex_process_config_args(
        self,
        body: dict[str, Any],
        profile_config: dict[str, Any] | None = None,
    ) -> tuple[tuple[str, str], ...]:
        codex_options = self._request_codex_options(body)
        profile = profile_config or {}
        args: list[tuple[str, str]] = []
        for request_key, config_key, aliases in (
            ("webSearch", "web_search", ("web_search",)),
            ("modelVerbosity", "model_verbosity", ("model_verbosity",)),
            ("effort", "model_reasoning_effort", ("reasoningEffort", "modelReasoningEffort", "model_reasoning_effort")),
        ):
            value = self._codex_option(codex_options, profile, request_key, *aliases)
            if value is not None:
                args.append((config_key, self._format_codex_config_value(value)))
        feature_values: dict[str, Any] = {}
        for key in ("imageGeneration", "features.image_generation"):
            if profile.get(key) is not None:
                feature_values["image_generation"] = profile.get(key)
        for source in (profile.get("features"),):
            if isinstance(source, dict):
                feature_values.update(source)
        for key in ("imageGeneration", "features.image_generation"):
            if codex_options.get(key) is not None:
                feature_values["image_generation"] = codex_options.get(key)
        if isinstance(codex_options.get("features"), dict):
            feature_values.update(codex_options["features"])
        for name, value in sorted(feature_values.items()):
            if value is not None:
                args.append((feature_config_key(str(name)), self._format_codex_config_value(value)))
        return tuple(args)

    @staticmethod
    def _codex_option(codex_options: dict[str, Any], profile_config: dict[str, Any], key: str, *aliases: str) -> Any:
        for candidate in (key, *aliases):
            if candidate in codex_options and codex_options[candidate] is not None:
                return codex_options[candidate]
        for candidate in (key, *aliases):
            if profile_config.get(candidate) is not None:
                return profile_config.get(candidate)
        return None

    @staticmethod
    def _format_codex_config_value(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def _gate(self, owner_hash: str, thread_id: str) -> ThreadGate:
        with self._gates_lock:
            key = (owner_hash, thread_id)
            gate = self._gates.get(key)
            if not gate:
                gate = ThreadGate(threading.Lock())
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
        return {
            "threadId": thread["thread_id"],
            "codexThreadId": thread.get("codex_thread_id"),
            "profile": thread["profile"],
            "configProfile": thread["config_profile"],
            "hostApp": thread.get("host_app"),
            "bundleId": thread.get("bundle_id"),
            "cwd": thread.get("cwd"),
            "status": thread["status"],
            "createdAt": thread["created_at"],
            "updatedAt": thread["updated_at"],
        }

    def _public_turn(self, turn: dict[str, Any]) -> dict[str, Any]:
        return {
            "threadId": turn["thread_id"],
            "turnId": turn["turn_id"],
            "codexTurnId": turn.get("codex_turn_id"),
            "profile": turn["profile"],
            "configProfile": turn["config_profile"],
            "hostApp": turn.get("host_app"),
            "bundleId": turn.get("bundle_id"),
            "cwd": turn.get("cwd"),
            "mode": turn["mode"],
            "productCorrelationId": turn.get("product_correlation_id"),
            "status": turn["status"],
            "error": turn.get("error"),
            "createdAt": turn["created_at"],
            "startedAt": turn.get("started_at"),
            "completedAt": turn.get("completed_at"),
            "updatedAt": turn["updated_at"],
        }

    def _stream_url(self, owner_id: str, thread_id: str, turn_id: str) -> str:
        return f"/v1/owners/{quote(owner_id, safe='')}/threads/{quote(thread_id, safe='')}/events?turnId={quote(turn_id, safe='')}"
