"""Process-local retention for isolated OpenAI caller tools, using existing threads."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Any

from .openai_protocol import output_text, raw_output_items, tool_calls
from .scheduler_errors import ConflictError


def normalized_history(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ignore wire-only message IDs; preserve content, ordering and tool arguments."""
    result: list[dict[str, Any]] = []
    for item in items:
        kind = item.get("type")
        if kind == "message":
            content = []
            for part in item.get("content", []):
                if part.get("type") in {"input_text", "output_text"}:
                    content.append({"type": "input_text", "text": part["text"]})
                else:
                    content.append(dict(part))
            if content:
                result.append({"type": "message", "role": item["role"], "content": content})
        elif kind == "function_call":
            arguments = item.get("arguments", "{}")
            try:
                arguments = json.loads(arguments)
            except (ValueError, TypeError):
                pass
            result.append({"type": kind, "call_id": item["call_id"], "name": item["name"], "arguments": arguments})
        elif kind == "function_call_output":
            result.append({"type": kind, "call_id": item["call_id"], "output": item["output"]})
        else:
            result.append(dict(item))
    return result


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def response_history(protocol: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if protocol == "chat.completions":
        text = output_text(events)
        items = [{"type": "message", "role": "assistant", "content": [{"type": "input_text", "text": text}]}] if text else []
    else:
        items = [i for i in raw_output_items(events) if i.get("type") == "message" and i.get("role") == "assistant"]
    return items + [
        {"type": "function_call", "call_id": c["callId"], "name": c["name"], "arguments": c["arguments"]}
        for c in tool_calls(events)
    ]


@dataclass
class RetainedTurn:
    context: Any
    key: str
    prefix: str
    calls: frozenset[str]
    touched: float
    claimed: bool = False


class Continuations:
    def __init__(self, scheduler: Any) -> None:
        self.scheduler = scheduler
        self.lock = threading.RLock()
        self.records: dict[str, RetainedTurn] = {}
        # Short-lived retry receipts contain hashes and response IDs, never bodies.
        self.receipts: dict[tuple[str, str, frozenset[str]], tuple[str, dict[str, Any], float]] = {}
        self.stopped = threading.Event()
        self.worker: threading.Thread | None = None
        if scheduler.config.openai_tool_continuations:
            self.worker = threading.Thread(target=self._reap, name="openai-tool-cleanup", daemon=True)
            self.worker.start()

    def _reap(self) -> None:
        while not self.stopped.wait(1):
            self.expire()

    def publish(self, context: Any, compat: Any) -> bool:
        config = self.scheduler.config
        if not config.openai_tool_continuations or not compat.continuation_key or not context.responses_completed or self.stopped.is_set() or context.codex_turn_done.is_set():
            return False
        now = time.monotonic()
        with self.lock:
            old = self.records.get(context.thread_id)
            if now - context.retention_started >= config.openai_tool_max_age_seconds:
                return False
            if old is None:
                # Keep a pool slot available for ordinary work/other principals.
                pool_limit = max(0, config.max_pooled_app_servers - 1) if config.max_pooled_app_servers else config.openai_tool_max_retained
                if len(self.records) >= min(config.openai_tool_max_retained, pool_limit):
                    return False
                if sum(r.context.owner_hash == context.owner_hash for r in self.records.values()) >= config.openai_tool_max_retained_per_owner:
                    return False
            events = self.scheduler.state.list_events(context.owner_hash, context.thread_id, turn_id=context.turn_id, limit=config.max_events_per_turn)
            prefix = digest(normalized_history([*compat.full_history, *response_history(compat.metadata["protocol"], events)]))
            self.records[context.thread_id] = RetainedTurn(context, compat.continuation_key, prefix, frozenset(context.published_call_ids), now)
            context.retained = True
            self.scheduler._metric("openai_tool_handoffs_retained", 1)
            return True

    def resume(self, key: str, full_history: list[dict[str, Any]], start: Any) -> dict[str, Any] | None:
        split = len(full_history)
        while split and full_history[split - 1].get("type") == "function_call_output":
            split -= 1
        outputs = full_history[split:]
        if not outputs:
            return None
        calls = frozenset(i["call_id"] for i in outputs)
        prefix = digest(normalized_history(full_history[:split]))
        output_digest = digest(sorted(outputs, key=lambda i: i["call_id"]))
        receipt_key = (key, prefix, calls)
        with self.lock:
            receipt = self.receipts.get(receipt_key)
            if receipt:
                if receipt[0] != output_digest:
                    raise ConflictError("Different results were already submitted for these tool calls.")
                return receipt[1]
            now = time.monotonic()
            for record in self.records.values():
                if record.key != key or record.prefix != prefix or record.calls != calls:
                    continue
                ctx = record.context
                if record.claimed or ctx.client.closed or ctx.codex_turn_done.is_set() or now - record.touched >= self.scheduler.config.openai_tool_idle_seconds or now - ctx.retention_started >= self.scheduler.config.openai_tool_max_age_seconds:
                    continue
                # _start_turn validates the existing auth/profile binding and uses
                # its normal admission, state records, gates and worker queue.
                record.claimed = True
                try:
                    result = start(ctx, outputs)
                except Exception:
                    record.claimed = False
                    raise
                self.receipts[receipt_key] = (output_digest, result, now)
                while len(self.receipts) > 256:
                    self.receipts.pop(next(iter(self.receipts)))
                self.scheduler._metric("openai_tool_continuations_resumed", 1)
                return result
        self.scheduler._metric("openai_tool_continuations_replayed", 1)
        return None

    def forget(self, context: Any) -> None:
        with self.lock:
            record = self.records.get(context.thread_id)
            if record and record.context is context:
                self.records.pop(context.thread_id)
            context.retained = False

    def cancel(self, owner_hash: str, thread_id: str, turn_id: str) -> None:
        with self.lock:
            record = self.records.get(thread_id)
            if not record or record.claimed or record.context.owner_hash != owner_hash or record.context.turn_id != turn_id:
                return
            self.records.pop(thread_id)
        self.scheduler._dispose_retained_context(record.context)

    def expire(self, *, include_claimed: bool = False) -> None:
        now = time.monotonic()
        expired = []
        with self.lock:
            for thread_id, record in list(self.records.items()):
                ctx = record.context
                thread = self.scheduler.state.get_thread(ctx.owner_hash, thread_id)
                profile = self.scheduler.state.get_profile(ctx.auth_principal_hash, ctx.auth_profile)
                revoked = (not profile or profile["instance_id"] != ctx.auth_profile_instance
                           or profile.get("auth_status") in {"missing", "failed", "requires_admin"}
                           or profile.get("auth_fingerprint") != ctx.auth_profile_fingerprint
                           or not thread or thread["status"] == "archived")
                if (not record.claimed or include_claimed) and (self.stopped.is_set() or revoked or ctx.client.closed or ctx.codex_turn_done.is_set() or now - record.touched >= self.scheduler.config.openai_tool_idle_seconds or now - ctx.retention_started >= self.scheduler.config.openai_tool_max_age_seconds):
                    self.records.pop(thread_id)
                    expired.append(ctx)
            self.receipts = {k: v for k, v in self.receipts.items() if now - v[2] < self.scheduler.config.openai_tool_idle_seconds}
        for ctx in expired:
            self.scheduler._dispose_retained_context(ctx)
            self.scheduler._metric("openai_tool_continuations_expired", 1)

    def shutdown(self) -> None:
        self.stopped.set()
        if self.worker:
            self.worker.join(timeout=2)
        self.expire()
