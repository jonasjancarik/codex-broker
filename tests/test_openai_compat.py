from __future__ import annotations

import base64
from dataclasses import replace
import json
import os
import struct
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from typing import Any

from openai import BadRequestError, OpenAI

from codex_broker.http_api import BrokerHandler
from codex_broker.openai_auth import compatibility_key_digest
from codex_broker.openai_protocol import (
    OpenAIProtocolError,
    chat_id_from_turn,
    parse_chat_request,
    parse_responses_request,
    response_id_from_turn,
    response_object,
    turn_id_from_response,
)
from codex_broker.services import BrokerHTTPServer, BrokerServices
from test_broker import config_for, wait_turn


COMPAT_KEY = "sk-codex-broker-test"
OWNER = "openai-client"
OTHER_COMPAT_KEY = "sk-codex-broker-other"
OTHER_OWNER = "openai-client-other"


def png_data_url(*, padding_bytes: int = 0) -> str:
    """Build a valid 1x1 PNG, optionally padded with a valid ancillary chunk."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)

    raw = b"\x89PNG\r\n\x1a\n"
    raw += chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    raw += chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
    if padding_bytes:
        raw += chunk(b"tEXt", b"padding\0" + (b"x" * padding_bytes))
    raw += chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


class OpenAICompatApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        config = replace(
            config_for(Path(self._tmp.name)),
            debug_raw_events=False,
            openai_compat_bindings={
                compatibility_key_digest(COMPAT_KEY): {
                    "ownerId": OWNER,
                    "profile": "default",
                    "configProfile": "default",
                    "hostApp": "sdk-test",
                    "modelAliases": {"gpt-compatible": "gpt-5.6-sol"},
                },
                compatibility_key_digest(OTHER_COMPAT_KEY): {
                    "ownerId": OTHER_OWNER,
                    "profile": "default",
                    "configProfile": "default",
                    "hostApp": "sdk-test",
                    "modelAliases": {"gpt-compatible": "gpt-5.6-sol"},
                },
            },
        )
        self.config = config
        self.services = BrokerServices.build(config)
        self.services.auth.login_api_key(OWNER, "sk-upstream-test", "default")
        self.services.auth.login_api_key(OTHER_OWNER, "sk-upstream-test", "default")
        services = self.services

        class Handler(BrokerHandler):
            broker = services

        self.server = BrokerHTTPServer(("127.0.0.1", 0), Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(1)
        self.services.scheduler.shutdown("interrupt", 1)
        self.services.pool.close_all()
        self.services.state.close()
        self._tmp.cleanup()
        os.environ.pop("FAKE_CODEX_REQUIRE_INJECT_BEFORE_TURN", None)
        os.environ.pop("FAKE_CODEX_EXPECT_THREAD_PARAMS", None)
        os.environ.pop("FAKE_CODEX_EXPECT_TURN_PARAMS", None)
        os.environ.pop("FAKE_CODEX_EXPECT_INJECT_ITEMS", None)
        os.environ.pop("FAKE_CODEX_FORBID_TURN_PARAMS", None)
        os.environ.pop("FAKE_CODEX_DISABLE_IMAGE_INPUT", None)
        os.environ.pop("FAKE_CODEX_OMIT_RAW_RESPONSE", None)
        os.environ.pop("FAKE_CODEX_MALFORMED_TOKEN_USAGE", None)
        os.environ.pop("FAKE_CODEX_TURN_COMPLETED_ERROR", None)
        os.environ.pop("FAKE_CODEX_RESPONSE_SPLITS_JSON", None)

    def test_models_use_openai_shape_and_aliases(self) -> None:
        models = self._request("GET", "/v1/models")
        self.assertEqual(models["object"], "list")
        self.assertEqual(
            [model["id"] for model in models["data"]],
            ["gpt-5.6-sol", "gpt-compatible"],
        )
        model = self._request("GET", "/v1/models/gpt-compatible")
        self.assertEqual(model["id"], "gpt-compatible")
        self.assertEqual(model["object"], "model")
        with self.assertRaises(urllib.error.HTTPError) as hidden:
            self._request("GET", "/v1/models/gpt-5.6-terra")
        self.assertEqual(hidden.exception.code, 404)

    def test_responses_sync_retrieve_and_input_items(self) -> None:
        schema = {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        }
        os.environ["FAKE_CODEX_EXPECT_THREAD_PARAMS"] = json.dumps(
            {
                "model": "gpt-5.6-sol",
                "developerInstructions": "Be concise.",
            }
        )
        os.environ["FAKE_CODEX_EXPECT_TURN_PARAMS"] = json.dumps(
            {
                "model": "gpt-5.6-sol",
                "effort": "low",
                "summary": "auto",
                "serviceTier": "fast",
                "outputSchema": schema,
            }
        )
        created = self._request(
            "POST",
            "/v1/responses",
            {
                "model": "gpt-compatible",
                "instructions": "Be concise.",
                "input": "Hello",
                "metadata": {"request": "one"},
                "reasoning": {"effort": "low", "summary": "auto"},
                "service_tier": "fast",
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "answer",
                        "schema": schema,
                        "strict": True,
                    }
                },
            },
        )
        self.assertEqual(created["object"], "response")
        self.assertEqual(created["status"], "completed")
        self.assertEqual(created["model"], "gpt-compatible")
        self.assertEqual(created["output"][0]["content"][0]["text"], "hello")
        self.assertEqual(created["usage"]["input_tokens"], 7)
        self.assertEqual(created["usage"]["input_tokens_details"]["cached_tokens"], 2)
        self.assertEqual(created["usage"]["output_tokens_details"]["reasoning_tokens"], 1)
        self.assertEqual(created["metadata"], {"request": "one"})
        self.assertEqual(created["text"]["format"]["type"], "json_schema")
        persisted = self.services.state.find_turn_by_turn_id(
            self.services.auth.hash_owner(OWNER),
            turn_id_from_response(created["id"]),
        )
        self.assertIsNotNone(persisted)
        self.assertNotIn(COMPAT_KEY, json.dumps(persisted))

        retrieved = self._request("GET", f"/v1/responses/{created['id']}")
        self.assertEqual(retrieved, created)
        items = self._request("GET", f"/v1/responses/{created['id']}/input_items")
        self.assertEqual(items["object"], "list")
        self.assertEqual(items["data"][0]["role"], "user")
        self.assertEqual(items["data"][0]["content"][0]["text"], "Hello")

    def test_previous_response_reconstructs_history_before_turn(self) -> None:
        os.environ["FAKE_CODEX_REQUIRE_INJECT_BEFORE_TURN"] = "1"
        try:
            first = self._request(
                "POST",
                "/v1/responses",
                {
                    "model": "gpt-compatible",
                    "input": [
                        {"role": "assistant", "content": [{"type": "output_text", "text": "Earlier"}]},
                        {"role": "user", "content": [{"type": "input_text", "text": "First"}]},
                    ],
                },
            )
        except urllib.error.HTTPError as exc:
            self.fail(exc.read().decode("utf-8"))
        second = self._request(
            "POST",
            "/v1/responses",
            {
                "model": "gpt-compatible",
                "previous_response_id": first["id"],
                "input": "Second",
            },
        )
        self.assertEqual(second["previous_response_id"], first["id"])
        self.assertEqual(second["status"], "completed")

    def test_image_history_is_persisted_retrieved_after_restart_and_injected_in_order(self) -> None:
        image = png_data_url()
        earlier = {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Earlier"}],
        }
        user = {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Describe this."},
                {"type": "input_image", "image_url": image, "detail": "high"},
            ],
        }
        os.environ["FAKE_CODEX_REQUIRE_INJECT_BEFORE_TURN"] = "1"
        os.environ["FAKE_CODEX_EXPECT_INJECT_ITEMS"] = json.dumps({"items": [earlier]})
        os.environ["FAKE_CODEX_EXPECT_TURN_PARAMS"] = json.dumps(
            {
                "model": "gpt-5.6-sol",
                "input": [
                    {"type": "text", "text": "Describe this.", "text_elements": []},
                    {"type": "image", "url": image, "detail": "high"},
                ],
            }
        )
        first = self._request(
            "POST",
            "/v1/responses",
            {"model": "gpt-compatible", "input": [earlier, user]},
        )
        expected_items = self._request("GET", f"/v1/responses/{first['id']}/input_items")
        self.assertEqual(expected_items["data"], [earlier, user])

        self._restart_broker()
        retrieved_items = self._request("GET", f"/v1/responses/{first['id']}/input_items")
        self.assertEqual(retrieved_items["data"], [earlier, user])

        raw_output = {
            "id": "msg1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "hello"}],
        }
        os.environ["FAKE_CODEX_EXPECT_INJECT_ITEMS"] = json.dumps(
            {"items": [earlier, user, raw_output]}
        )
        os.environ["FAKE_CODEX_EXPECT_TURN_PARAMS"] = json.dumps(
            {
                "model": "gpt-5.6-sol",
                "input": [{"type": "text", "text": "Follow up", "text_elements": []}],
            }
        )
        second = self._request(
            "POST",
            "/v1/responses",
            {
                "model": "gpt-compatible",
                "previous_response_id": first["id"],
                "input": "Follow up",
            },
        )
        self.assertEqual(second["status"], "completed")

    def test_image_only_responses_and_chat_inputs_are_forwarded(self) -> None:
        image = png_data_url()
        expected = [{"type": "image", "url": image}]
        os.environ["FAKE_CODEX_EXPECT_TURN_PARAMS"] = json.dumps(
            {"model": "gpt-5.6-sol", "input": expected}
        )
        response = self._request(
            "POST",
            "/v1/responses",
            {
                "model": "gpt-compatible",
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_image", "image_url": image}],
                    }
                ],
            },
        )
        self.assertEqual(response["status"], "completed")

        os.environ["FAKE_CODEX_EXPECT_TURN_PARAMS"] = json.dumps(
            {"model": "gpt-5.6-sol", "input": expected}
        )
        completion = self._request(
            "POST",
            "/v1/chat/completions",
            {
                "model": "gpt-compatible",
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "image_url", "image_url": {"url": image}}],
                    }
                ],
            },
        )
        self.assertEqual(completion["choices"][0]["message"]["content"], "hello")

        thread = self._request(
            "POST", f"/v1/owners/{OWNER}/threads", {}, api_key="test-key"
        )
        turn = self._request(
            "POST",
            f"/v1/owners/{OWNER}/threads/{thread['threadId']}/turns",
            {"input": expected, "codexOptions": {"model": "gpt-5.6-sol"}},
            api_key="test-key",
        )
        completed = wait_turn(self.services, OWNER, thread["threadId"], turn["turnId"])
        self.assertEqual(completed["status"], "completed")

    def test_response_resources_are_owner_scoped_by_the_binding(self) -> None:
        created = self._request(
            "POST",
            "/v1/responses",
            {"model": "gpt-compatible", "input": "Private"},
        )
        with self.assertRaises(urllib.error.HTTPError) as cross_owner:
            self._request(
                "GET",
                f"/v1/responses/{created['id']}",
                api_key=OTHER_COMPAT_KEY,
            )
        self.assertEqual(cross_owner.exception.code, 404)

    def test_completed_response_is_reconstructed_after_restart(self) -> None:
        created = self._request(
            "POST",
            "/v1/responses",
            {"model": "gpt-compatible", "input": "Persist"},
        )
        self._restart_broker()
        retrieved = self._request("GET", f"/v1/responses/{created['id']}")
        self.assertEqual(retrieved, created)

    def test_completed_response_falls_back_to_normalized_agent_message(self) -> None:
        os.environ["FAKE_CODEX_OMIT_RAW_RESPONSE"] = "1"
        created = self._request(
            "POST",
            "/v1/responses",
            {"model": "gpt-compatible", "input": "Missing raw output"},
        )
        self.assertEqual(created["output"][0]["content"][0]["text"], "hello")
        self._restart_broker()
        retrieved = self._request("GET", f"/v1/responses/{created['id']}")
        self.assertEqual(retrieved, created)

    def test_completed_response_rejects_malformed_usage(self) -> None:
        os.environ["FAKE_CODEX_MALFORMED_TOKEN_USAGE"] = "1"
        with self.assertRaises(urllib.error.HTTPError) as malformed:
            self._request(
                "POST",
                "/v1/responses",
                {"model": "gpt-compatible", "input": "Malformed usage"},
            )
        self.assertEqual(malformed.exception.code, 500)
        error = json.loads(malformed.exception.read().decode("utf-8"))["error"]
        self.assertEqual(error["code"], "server_error")

    def test_failed_turn_returns_a_failed_response_object(self) -> None:
        os.environ["FAKE_CODEX_TURN_COMPLETED_ERROR"] = "simulated internal failure"
        failed = self._request(
            "POST",
            "/v1/responses",
            {"model": "gpt-compatible", "input": "Fail"},
        )
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"]["code"], "codex_runtime_error")
        self.assertNotIn("simulated internal failure", failed["error"]["message"])

    def test_responses_stream_emits_typed_events_and_closes(self) -> None:
        raw = self._request_text(
            "POST",
            "/v1/responses",
            {"model": "gpt-compatible", "input": "Stream", "stream": True},
        )
        self.assertIn("event: response.created", raw)
        self.assertIn("event: response.output_text.delta", raw)
        self.assertIn("event: response.output_item.done", raw)
        self.assertIn("event: response.completed", raw)
        self.assertLess(raw.index("event: response.created"), raw.index("event: response.completed"))

    def test_safe_mode_sanitizes_split_output_across_openai_response_forms(self) -> None:
        canary = "registered-openai-canary"
        self.services.sanitizer.register("test-canary", canary)
        os.environ["FAKE_CODEX_RESPONSE_SPLITS_JSON"] = json.dumps(
            ["before registered-open", "ai-canary after"]
        )
        responses_sync = json.dumps(
            self._request("POST", "/v1/responses", {"model": "gpt-compatible", "input": "Sync"})
        )
        responses_stream = self._request_text(
            "POST",
            "/v1/responses",
            {"model": "gpt-compatible", "input": "Stream", "stream": True},
        )
        chat_sync = json.dumps(
            self._request(
                "POST",
                "/v1/chat/completions",
                {"model": "gpt-compatible", "messages": [{"role": "user", "content": "Sync"}]},
            )
        )
        chat_stream = self._request_text(
            "POST",
            "/v1/chat/completions",
            {
                "model": "gpt-compatible",
                "messages": [{"role": "user", "content": "Stream"}],
                "stream": True,
            },
        )
        for rendered in (responses_sync, responses_stream, chat_sync, chat_stream):
            self.assertNotIn(canary, rendered)
            self.assertIn("<redacted>", rendered)

    def test_raw_mode_preserves_openai_output(self) -> None:
        self.config = replace(self.config, event_sanitization_mode="raw")
        self._restart_broker()
        canary = "raw-openai-canary"
        self.services.sanitizer.register("test-canary", canary)
        os.environ["FAKE_CODEX_RESPONSE_SPLITS_JSON"] = json.dumps(["raw-openai-", "canary"])
        sync = self._request("POST", "/v1/responses", {"model": "gpt-compatible", "input": "Raw"})
        stream = self._request_text(
            "POST",
            "/v1/chat/completions",
            {
                "model": "gpt-compatible",
                "messages": [{"role": "user", "content": "Raw"}],
                "stream": True,
            },
        )
        self.assertIn(canary, json.dumps(sync))
        streamed_text = "".join(
            str(payload.get("choices", [{}])[0].get("delta", {}).get("content") or "")
            for line in stream.splitlines()
            if line.startswith("data: {")
            for payload in (json.loads(line.removeprefix("data: ")),)
        )
        self.assertEqual(streamed_text, canary)

    def test_chat_sync_and_stream(self) -> None:
        os.environ["FAKE_CODEX_EXPECT_THREAD_PARAMS"] = json.dumps(
            {
                "model": "gpt-5.6-sol",
                "baseInstructions": "Be helpful.",
                "developerInstructions": "Use plain language.",
            }
        )
        completion = self._request(
            "POST",
            "/v1/chat/completions",
            {
                "model": "gpt-compatible",
                "messages": [
                    {"role": "system", "content": "Be helpful."},
                    {"role": "developer", "content": "Use plain language."},
                    {"role": "user", "content": "Hello"},
                ],
            },
        )
        self.assertEqual(completion["object"], "chat.completion")
        self.assertEqual(completion["choices"][0]["message"]["content"], "hello")
        self.assertEqual(completion["choices"][0]["finish_reason"], "stop")

        streamed = self._request_text(
            "POST",
            "/v1/chat/completions",
            {
                "model": "gpt-compatible",
                "messages": [
                    {"role": "system", "content": "Be helpful."},
                    {"role": "developer", "content": "Use plain language."},
                    {"role": "user", "content": "Hello"},
                ],
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        )
        self.assertIn('"object":"chat.completion.chunk"', streamed)
        self.assertIn('"content":"hello"', streamed)
        self.assertIn('"choices":[]', streamed)
        self.assertTrue(streamed.rstrip().endswith("data: [DONE]"))

    def test_openai_error_shapes_do_not_use_native_error_shape(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as invalid_key:
            self._request("GET", "/v1/models", api_key="wrong")
        self.assertEqual(invalid_key.exception.code, 401)
        auth_error = json.loads(invalid_key.exception.read().decode("utf-8"))
        self.assertEqual(auth_error["error"]["code"], "invalid_api_key")

        with self.assertRaises(urllib.error.HTTPError) as unsupported:
            self._request(
                "POST",
                "/v1/responses",
                {
                    "model": "gpt-compatible",
                    "input": "Hello",
                    "tools": [],
                },
            )
        self.assertEqual(unsupported.exception.code, 400)
        payload = json.loads(unsupported.exception.read().decode("utf-8"))
        self.assertEqual(payload["error"]["code"], "unsupported_parameter")
        self.assertEqual(payload["error"]["param"], "tools")

    def test_native_and_compatibility_credentials_are_not_interchangeable(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as native_on_compat:
            self._request("GET", "/v1/models", api_key="test-key")
        self.assertEqual(native_on_compat.exception.code, 401)

        with self.assertRaises(urllib.error.HTTPError) as compat_on_native:
            self._request(
                "GET",
                f"/v1/owners/{OWNER}/auth/status",
                api_key=COMPAT_KEY,
            )
        self.assertEqual(compat_on_native.exception.code, 401)

    def test_official_openai_sdk_parses_responses_chat_and_streams(self) -> None:
        client = OpenAI(
            base_url=f"{self.base_url}/v1",
            api_key=COMPAT_KEY,
            timeout=10,
            max_retries=0,
        )
        models = client.models.list()
        self.assertIn("gpt-compatible", [model.id for model in models.data])

        response = client.responses.create(model="gpt-compatible", input="SDK")
        self.assertEqual(response.status, "completed")
        self.assertEqual(response.output_text, "hello")
        structured = client.responses.create(
            model="gpt-compatible",
            input="SDK structured",
            text={
                "format": {
                    "type": "json_schema",
                    "name": "answer",
                    "schema": {
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                        "required": ["answer"],
                        "additionalProperties": False,
                    },
                    "strict": True,
                }
            },
        )
        self.assertEqual(structured.text.format.type, "json_schema")
        retrieved = client.responses.retrieve(response.id)
        self.assertEqual(retrieved.id, response.id)
        items = client.responses.input_items.list(response.id)
        self.assertEqual(items.data[0].role, "user")

        response_events = list(
            client.responses.create(model="gpt-compatible", input="SDK stream", stream=True)
        )
        self.assertEqual(response_events[0].type, "response.created")
        self.assertEqual(response_events[0].response.status, "in_progress")
        self.assertEqual(response_events[-1].type, "response.completed")

        completion = client.chat.completions.create(
            model="gpt-compatible",
            messages=[{"role": "user", "content": "SDK chat"}],
        )
        self.assertEqual(completion.choices[0].message.content, "hello")
        chunks = list(
            client.chat.completions.create(
                model="gpt-compatible",
                messages=[{"role": "user", "content": "SDK chat stream"}],
                stream=True,
            )
        )
        self.assertEqual(chunks[0].choices[0].delta.role, "assistant")
        self.assertEqual("".join(chunk.choices[0].delta.content or "" for chunk in chunks), "hello")

        with self.assertRaises(BadRequestError) as raised:
            client.responses.create(
                model="gpt-compatible",
                input="unsupported",
                temperature=0.2,
            )
        self.assertEqual(raised.exception.code, "unsupported_parameter")

    def test_official_openai_sdk_forwards_images_for_sync_and_streaming(self) -> None:
        image = png_data_url()
        client = OpenAI(
            base_url=f"{self.base_url}/v1",
            api_key=COMPAT_KEY,
            timeout=10,
            max_retries=0,
        )
        response_input = [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Describe this."},
                    {"type": "input_image", "image_url": image, "detail": "low"},
                ],
            }
        ]
        turn_input = [
            {"type": "text", "text": "Describe this.", "text_elements": []},
            {"type": "image", "url": image, "detail": "low"},
        ]
        os.environ["FAKE_CODEX_EXPECT_TURN_PARAMS"] = json.dumps(
            {"model": "gpt-5.6-sol", "input": turn_input}
        )
        response = client.responses.create(model="gpt-compatible", input=response_input)
        self.assertEqual(response.output_text, "hello")

        os.environ["FAKE_CODEX_EXPECT_TURN_PARAMS"] = json.dumps(
            {"model": "gpt-5.6-sol", "input": turn_input}
        )
        response_events = list(
            client.responses.create(model="gpt-compatible", input=response_input, stream=True)
        )
        self.assertEqual(response_events[-1].type, "response.completed")

        chat_input = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this."},
                    {"type": "image_url", "image_url": {"url": image, "detail": "low"}},
                ],
            }
        ]
        chat_turn_input = [
            {"type": "text", "text": "Describe this.", "text_elements": []},
            {"type": "image", "url": image, "detail": "low"},
        ]
        os.environ["FAKE_CODEX_EXPECT_TURN_PARAMS"] = json.dumps(
            {"model": "gpt-5.6-sol", "input": chat_turn_input}
        )
        completion = client.chat.completions.create(model="gpt-compatible", messages=chat_input)
        self.assertEqual(completion.choices[0].message.content, "hello")

        os.environ["FAKE_CODEX_EXPECT_TURN_PARAMS"] = json.dumps(
            {"model": "gpt-5.6-sol", "input": chat_turn_input}
        )
        chunks = list(
            client.chat.completions.create(
                model="gpt-compatible",
                messages=chat_input,
                stream=True,
            )
        )
        self.assertEqual("".join(chunk.choices[0].delta.content or "" for chunk in chunks), "hello")
        client.close()

    def test_chat_token_caps_accept_arbitrary_values_and_are_not_forwarded(self) -> None:
        image = png_data_url()
        os.environ["FAKE_CODEX_EXPECT_TURN_PARAMS"] = json.dumps(
            {
                "model": "gpt-5.6-sol",
                "input": [{"type": "image", "url": image}],
            }
        )
        os.environ["FAKE_CODEX_FORBID_TURN_PARAMS"] = json.dumps(
            ["max_tokens", "max_completion_tokens"]
        )
        completion = self._request(
            "POST",
            "/v1/chat/completions",
            {
                "model": "gpt-compatible",
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "image_url", "image_url": {"url": image}}],
                    }
                ],
                "max_tokens": {"arbitrary": [1, 2, 3]},
                "max_completion_tokens": "also arbitrary",
            },
        )
        self.assertEqual(completion["choices"][0]["message"]["content"], "hello")

        os.environ["FAKE_CODEX_EXPECT_TURN_PARAMS"] = json.dumps(
            {
                "model": "gpt-5.6-sol",
                "input": [{"type": "image", "url": image}],
            }
        )
        numeric = self._request(
            "POST",
            "/v1/chat/completions",
            {
                "model": "gpt-compatible",
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "image_url", "image_url": {"url": image}}],
                    }
                ],
                "max_tokens": 17,
                "max_completion_tokens": 23,
            },
        )
        self.assertEqual(numeric["choices"][0]["message"]["content"], "hello")

    def test_inline_image_request_over_one_megabyte_is_accepted(self) -> None:
        image = png_data_url(padding_bytes=1_100_000)
        self.assertGreater(len(json.dumps({"model": "gpt-compatible", "input": [image]})), 1_000_000)
        response = self._request(
            "POST",
            "/v1/responses",
            {
                "model": "gpt-compatible",
                "input": [{"role": "user", "content": [{"type": "input_image", "image_url": image}]}],
            },
        )
        self.assertEqual(response["status"], "completed")

    def test_image_input_requires_model_image_modality_including_history(self) -> None:
        image = png_data_url()
        first = self._request(
            "POST",
            "/v1/responses",
            {
                "model": "gpt-compatible",
                "input": [{"role": "user", "content": [{"type": "input_image", "image_url": image}]}],
            },
        )
        os.environ["FAKE_CODEX_DISABLE_IMAGE_INPUT"] = "1"
        # The fake App Server inherits its model fixture at process start.
        # Restart the broker so the capability change is visible to a fresh process.
        self._restart_broker()
        with self.assertRaises(urllib.error.HTTPError) as direct:
            self._request(
                "POST",
                "/v1/responses",
                {
                    "model": "gpt-compatible",
                    "input": [{"role": "user", "content": [{"type": "input_image", "image_url": image}]}],
                },
            )
        self.assertEqual(direct.exception.code, 400)
        direct_error = json.loads(direct.exception.read().decode("utf-8"))["error"]
        self.assertEqual(direct_error["param"], "model")

        with self.assertRaises(urllib.error.HTTPError) as history:
            self._request(
                "POST",
                "/v1/responses",
                {"model": "gpt-compatible", "previous_response_id": first["id"], "input": "Follow up"},
            )
        self.assertEqual(history.exception.code, 400)

    def test_official_sdk_can_cancel_an_active_streamed_response(self) -> None:
        os.environ["FAKE_CODEX_TURN_DELAY"] = "1"
        client = OpenAI(
            base_url=f"{self.base_url}/v1",
            api_key=COMPAT_KEY,
            timeout=10,
            max_retries=0,
        )
        stream = client.responses.create(model="gpt-compatible", input="Cancel", stream=True)
        first = next(iter(stream))
        self.assertEqual(first.type, "response.created")
        cancelled = client.responses.cancel(first.response.id)
        self.assertEqual(cancelled.status, "cancelled")
        stream.close()

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        api_key: str = COMPAT_KEY,
    ) -> dict[str, Any]:
        return json.loads(self._request_text(method, path, body, api_key=api_key))

    def _request_text(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        api_key: str = COMPAT_KEY,
    ) -> str:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.read().decode("utf-8")

    def _restart_broker(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(1)
        self.services.scheduler.shutdown("interrupt", 1)
        self.services.pool.close_all()
        self.services.state.close()

        self.services = BrokerServices.build(self.config)
        services = self.services

        class Handler(BrokerHandler):
            broker = services

        self.server = BrokerHTTPServer(("127.0.0.1", 0), Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"


class OpenAIProtocolTests(unittest.TestCase):
    def test_ids_are_reversible_and_strict(self) -> None:
        turn_id = "turn_Abcdefgh12345678"
        self.assertEqual(turn_id_from_response(response_id_from_turn(turn_id)), turn_id)
        self.assertEqual(chat_id_from_turn(turn_id), "chatcmpl_Abcdefgh12345678")
        with self.assertRaises(OpenAIProtocolError):
            turn_id_from_response("resp_bad!")

    def test_parsers_reject_behavior_fields_that_are_not_supported(self) -> None:
        with self.assertRaises(OpenAIProtocolError) as raised:
            parse_responses_request({"model": "m", "input": "x", "temperature": 0.2})
        self.assertEqual(raised.exception.code, "unsupported_parameter")
        with self.assertRaises(OpenAIProtocolError):
            parse_chat_request(
                {
                    "model": "m",
                    "messages": [{"role": "user", "content": "x"}],
                    "tools": [],
                }
            )
        with self.assertRaises(OpenAIProtocolError) as tool_message:
            parse_chat_request(
                {
                    "model": "m",
                    "messages": [
                        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
                        {"role": "user", "content": "x"},
                    ],
                }
            )
        self.assertEqual(tool_message.exception.code, "unsupported_parameter")

    def test_completed_response_requires_raw_item_and_usage(self) -> None:
        turn = {
            "turn_id": "turn_Abcdefgh12345678",
            "status": "completed",
            "created_at": "2026-07-28T10:00:00Z",
            "completed_at": "2026-07-28T10:00:01Z",
            "resolved_options": {
                "openaiCompat": {
                    "protocol": "responses",
                    "requestedModel": "m",
                    "text": {"format": {"type": "text"}},
                }
            },
        }
        with self.assertRaises(OpenAIProtocolError):
            response_object(turn, [])


if __name__ == "__main__":
    unittest.main()
