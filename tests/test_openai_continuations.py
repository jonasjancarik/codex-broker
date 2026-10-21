from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
import unittest
import urllib.error

import test_openai_compat as compat


class ContinuationTests(unittest.TestCase):
    _request = compat.OpenAICompatApiTests._request
    _request_text = compat.OpenAICompatApiTests._request_text
    _restart_broker = compat.OpenAICompatApiTests._restart_broker
    WEATHER_TOOL = compat.OpenAICompatApiTests.WEATHER_TOOL

    def setUp(self):
        compat.OpenAICompatApiTests.setUp(self)
        self.config = replace(self.config, openai_tool_continuations=True)
        self._restart_broker()
        os.environ["FAKE_CODEX_TOOL_CONTINUE"] = "1"
        os.environ["FAKE_CODEX_CODE_MODE"] = "1"
        os.environ["FAKE_CODEX_SERVER_REQUEST_TIMEOUT"] = "30"
        os.environ["FAKE_CODEX_TOOL_CALLS_JSON"] = json.dumps(compat.OpenAICompatApiTests.TWO_CALLS)
        self.messages = [{"role": "user", "content": "Weather?"}]

    def tearDown(self):
        compat.OpenAICompatApiTests.tearDown(self)
        for key in ["FAKE_CODEX_TOOL_CONTINUE", "FAKE_CODEX_TOOL_PARALLEL", "FAKE_CODEX_SERVER_REQUEST_TIMEOUT"]:
            os.environ.pop(key, None)

    def chat(self, messages=None, **kwargs):
        return self._request("POST", "/v1/chat/completions", {
            "model": "gpt-compatible", "messages": messages or self.messages,
            "tools": [self.WEATHER_TOOL], **kwargs,
        })

    def append_results(self, response):
        message = response["choices"][0]["message"]
        self.messages.append({k: v for k, v in message.items() if k in {"role", "content", "tool_calls"}})
        for call in message["tool_calls"]:
            self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": "proof-" + call["id"]})

    def row(self, response):
        return self.services.state.find_turn_by_turn_id(self.services.auth.hash_owner(compat.OWNER), "turn_" + response["id"].split("_", 1)[1])

    def test_sequential_calls_continue_same_upstream_turn_without_replay(self):
        first = self.chat()
        self.append_results(first)
        second = self.chat()
        self.assertEqual(second["usage"]["total_tokens"], 0)
        self.append_results(second)
        final = self.chat()
        self.assertIn("proof-exec-call_a", final["choices"][0]["message"]["content"])
        self.assertIn("proof-exec-call_b", final["choices"][0]["message"]["content"])
        rows = [self.row(r) for r in [first, second, final]]
        self.assertEqual(len({r["thread_id"] for r in rows}), 1)
        self.assertEqual(len({r["codex_turn_id"] for r in rows}), 1)
        self.assertEqual(len({r["turn_id"] for r in rows}), 3)
        self.assertEqual(final["usage"]["total_tokens"], 3)
        self.assertEqual(self.services.scheduler.continuations.records, {})

    def test_dispatched_parallel_calls_are_batched(self):
        os.environ["FAKE_CODEX_TOOL_PARALLEL"] = "1"
        first = self.chat()
        self.assertEqual(len(first["choices"][0]["message"]["tool_calls"]), 2)
        self.append_results(first)
        final = self.chat()
        self.assertEqual(final["choices"][0]["finish_reason"], "stop")
        self.assertEqual(self.row(first)["thread_id"], self.row(final)["thread_id"])

    def test_raw_calls_dispatched_sequentially_receive_their_real_outputs(self):
        os.environ.pop("FAKE_CODEX_CODE_MODE")
        first = self.chat()
        self.assertEqual(len(first["choices"][0]["message"]["tool_calls"]), 2)
        self.append_results(first)
        final = self.chat()
        self.assertIn("proof-call_b", final["choices"][0]["message"]["content"])
        self.assertEqual(self.row(first)["thread_id"], self.row(final)["thread_id"])

    def test_identical_concurrent_retries_share_one_response_and_conflicts_fail(self):
        self.append_results(self.chat())
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.chat(), range(2)))
        self.assertEqual(results[0]["id"], results[1]["id"])
        self.messages[-1]["content"] = "different"
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.chat()
        self.assertEqual(raised.exception.code, 400)
        self.assertIn("already submitted", raised.exception.read().decode())

    def test_edited_history_and_changed_model_options_replay(self):
        first = self.chat()
        self.append_results(first)
        self.messages[0]["content"] = "Edited weather request"
        replay = self.chat(reasoning_effort="high")
        self.assertNotEqual(self.row(first)["thread_id"], self.row(replay)["thread_id"])

    def test_owner_cannot_resume_another_owners_calls(self):
        first = self.chat()
        self.append_results(first)
        other = self._request("POST", "/v1/chat/completions", {
            "model": "gpt-compatible", "messages": self.messages, "tools": [self.WEATHER_TOOL]
        }, api_key=compat.OTHER_COMPAT_KEY)
        self.assertEqual(other["choices"][0]["message"]["content"], "hello")
        self.assertEqual(self.services.scheduler.metrics().get("openai_tool_continuations_resumed", 0), 0)

    def test_timeout_releases_context_and_replays(self):
        first = self.chat()
        self.append_results(first)
        record = next(iter(self.services.scheduler.continuations.records.values()))
        record.touched -= 301
        self.services.scheduler.continuations.expire()
        self.assertIsNone(record.context.client)
        final = self.chat()
        self.assertNotEqual(self.row(first)["thread_id"], self.row(final)["thread_id"])

    def test_restart_replays_full_history(self):
        first = self.chat()
        self.append_results(first)
        self._restart_broker()
        final = self.chat()
        self.assertNotEqual(self.row(first)["thread_id"], self.row(final)["thread_id"])

    def test_capacity_does_not_retain_second_chain(self):
        self.chat()
        self.chat([{"role": "user", "content": "Another independent request"}])
        self.assertEqual(len(self.services.scheduler.continuations.records), 1)

    def test_responses_previous_response_id_continues_same_thread(self):
        tool = self.WEATHER_TOOL["function"]
        base = {"model": "gpt-compatible", "tools": [{"type": "function", **tool}]}
        first = self._request("POST", "/v1/responses", {**base, "input": "Weather?"})
        calls = [i for i in first["output"] if i["type"] == "function_call"]
        second = self._request("POST", "/v1/responses", {**base, "previous_response_id": first["id"], "input": [
            {"type": "function_call_output", "call_id": calls[0]["call_id"], "output": "sunny"}
        ]})
        self.assertEqual(self.row(first)["thread_id"], self.row(second)["thread_id"])

    def test_pool_idle_eviction_keeps_retained_lease(self):
        self.chat()
        record = next(iter(self.services.scheduler.continuations.records.values()))
        client = record.context.client
        client.last_used_at -= 100000
        self.services.pool._sweep()
        self.assertFalse(client.closed)
        self.assertTrue(client.has_active_contexts)

    def test_profile_revocation_and_absolute_age_release_waiters(self):
        self.chat()
        record = next(iter(self.services.scheduler.continuations.records.values()))
        self.services.state.update_auth_status(record.context.auth_principal_hash, "default", "missing")
        self.services.scheduler.continuations.expire()
        self.assertIsNone(record.context.client)
        self.assertEqual(self.services.scheduler.continuations.records, {})

    def test_absolute_age_releases_waiter(self):
        self.chat()
        record = next(iter(self.services.scheduler.continuations.records.values()))
        record.context.retention_started -= 1801
        self.services.scheduler.continuations.expire()
        self.assertIsNone(record.context.client)

    def test_native_turn_cannot_replace_a_waiting_tool_turn(self):
        first = self.chat()
        with self.assertRaisesRegex(Exception, "waiting for OpenAI"):
            self.services.scheduler.start_turn(compat.OWNER, self.row(first)["thread_id"], {"input": [{"type": "text", "text": "new native turn"}]})

    def test_partial_parallel_results_are_rejected_without_consuming_waiter(self):
        os.environ["FAKE_CODEX_TOOL_PARALLEL"] = "1"
        self.append_results(self.chat())
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.chat(self.messages[:-1])
        self.assertEqual(raised.exception.code, 400)
        final = self.chat()
        self.assertEqual(final["choices"][0]["finish_reason"], "stop")

    def test_changed_tools_replay(self):
        first = self.chat()
        self.append_results(first)
        changed = json.loads(json.dumps(self.WEATHER_TOOL))
        changed["function"]["description"] = "Changed tool contract"
        final = self.chat(tools=[changed])
        self.assertNotEqual(self.row(first)["thread_id"], self.row(final)["thread_id"])

    def test_streaming_continuation_preserves_response_identity_and_usage(self):
        first = self.chat()
        self.append_results(first)
        data = self._request_text("POST", "/v1/chat/completions", {
            "model": "gpt-compatible", "tools": [self.WEATHER_TOOL], "messages": self.messages,
            "stream": True, "stream_options": {"include_usage": True},
        })
        chunks = [json.loads(line[6:]) for line in data.splitlines() if line.startswith("data: {")]
        self.assertTrue(any(c.get("usage", {}).get("total_tokens") == 0 for c in chunks if c.get("usage")))
        self.assertTrue(any(c["choices"] and c["choices"][0]["delta"].get("tool_calls") for c in chunks))
        self.assertEqual(self.services.scheduler.metrics()["openai_tool_continuations_resumed"], 1)

    def test_cancel_completed_responses_handoff_releases_live_execution(self):
        tool = self.WEATHER_TOOL["function"]
        first = self._request("POST", "/v1/responses", {"model": "gpt-compatible", "tools": [{"type": "function", **tool}], "input": "Weather?"})
        record = next(iter(self.services.scheduler.continuations.records.values()))
        self._request("POST", f"/v1/responses/{first['id']}/cancel", {})
        self.assertIsNone(record.context.client)
        self.assertEqual(self.services.scheduler.continuations.records, {})

    def test_dead_app_server_falls_back_without_reusing_execution(self):
        first = self.chat()
        self.append_results(first)
        record = next(iter(self.services.scheduler.continuations.records.values()))
        self.services.pool.close_client(record.context.client)
        final = self.chat()
        self.assertNotEqual(self.row(first)["thread_id"], self.row(final)["thread_id"])

    def test_ambiguous_result_delivery_is_not_replayed_on_retry(self):
        from codex_broker.app_server import AppServerError
        first = self.chat()
        self.append_results(first)
        record = next(iter(self.services.scheduler.continuations.records.values()))
        client = record.context.client
        original = client.send
        def fail_after_send(message):
            original(message)
            if message.get("result", {}).get("success") is True:
                raise AppServerError("diagnostic delivery failure after send")
        client.send = fail_after_send
        for _ in range(2):
            with self.assertRaises(urllib.error.HTTPError):
                self.chat()
        self.assertEqual(self.services.scheduler.metrics()["openai_tool_continuations_resumed"], 1)
        self.assertEqual(self.services.scheduler.continuations.records, {})

    def test_batching_works_with_continuation_disabled(self):
        self.config = replace(self.config, openai_tool_continuations=False)
        self._restart_broker()
        os.environ["FAKE_CODEX_TOOL_PARALLEL"] = "1"
        first = self.chat()
        self.assertEqual(len(first["choices"][0]["message"]["tool_calls"]), 2)
        self.assertEqual(self.services.scheduler.continuations.records, {})
        self.append_results(first)
        final = self.chat()
        self.assertNotEqual(self.row(first)["thread_id"], self.row(final)["thread_id"])

    def test_native_steering_routes_cannot_change_retained_history(self):
        first = self.chat()
        row = self.row(first)
        for mode in ("endpoint", "steer"):
            with self.subTest(mode=mode), self.assertRaisesRegex(Exception, "cannot be steered"):
                if mode == "endpoint":
                    self.services.scheduler.steer_turn(compat.OWNER, row["thread_id"], row["turn_id"], {"input": [{"type": "text", "text": "changed"}]})
                else:
                    self.services.scheduler.start_turn(compat.OWNER, row["thread_id"], {"mode": "steer", "input": [{"type": "text", "text": "changed"}]})

    def test_single_process_pool_reserves_capacity_instead_of_retaining(self):
        self.config = replace(self.config, max_pooled_app_servers=1)
        self._restart_broker()
        self.chat()
        self.assertEqual(self.services.scheduler.continuations.records, {})

    def test_missing_raw_usage_is_unknown_and_does_not_retain(self):
        os.environ.pop("FAKE_CODEX_TOOL_CONTINUE")
        os.environ["FAKE_CODEX_OMIT_RAW_RESPONSE"] = "1"
        first = self.chat()
        self.assertIsNone(first["usage"])
        self.assertEqual(self.services.scheduler.continuations.records, {})

    def test_failed_handoff_persistence_does_not_leave_a_live_waiter(self):
        from unittest.mock import patch
        with patch.object(self.services.scheduler, "_persist_context_terminal", side_effect=RuntimeError("diagnostic persistence failure")):
            with self.assertRaises(urllib.error.HTTPError):
                self.chat()
        self.assertEqual(self.services.scheduler.continuations.records, {})
        self.assertTrue(all(not client.has_active_contexts for client in self.services.pool._clients.values()))
