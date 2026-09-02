from __future__ import annotations

import base64
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from codex_broker.openai_protocol import OpenAIProtocolError, parse_chat_request, parse_responses_request
from codex_broker.scheduler import TurnScheduler
from codex_broker.scheduler_config import codex_image_items


def data_url(mime_type: str = "image/png", data: bytes = b"\x89PNG\r\n\x1a\n") -> str:
    return f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"


class OpenAIImageProtocolTests(unittest.TestCase):
    def test_execution_mapping_preserves_requested_details_and_non_image_content(self) -> None:
        requested = [
            {"type": kind, "detail": detail, "url": data_url()}
            for kind in ("image", "localImage", "input_image", "text")
            for detail in (None, "low", "high", "auto", "original")
        ]
        requested.append({"type": "image", "url": data_url()})
        before = deepcopy(requested)
        prepared = codex_image_items(requested)
        self.assertEqual(requested, before)
        for original, actual in zip(requested, prepared):
            expected = dict(original)
            if original["type"] != "text" and original.get("detail") == "low":
                expected["detail"] = "high"
            self.assertEqual(actual, expected)

    def test_both_steering_routes_map_low_detail_without_changing_recorded_input(self) -> None:
        requested = [{"type": "image", "url": data_url(), "detail": "low"}]
        for method in ("steer_turn", "_steer_active"):
            with self.subTest(method=method):
                scheduler = Mock()
                active = SimpleNamespace(
                    client=Mock(), codex_thread_id="codex_thread", codex_turn_id="codex_turn",
                    turn_id="turn", product_correlation_id=None,
                )
                scheduler._active_context.return_value = active
                if method == "steer_turn":
                    TurnScheduler.steer_turn(scheduler, "owner", "thread", "turn", {"input": requested})
                else:
                    TurnScheduler._steer_active(scheduler, "owner_hash", "thread", requested)
                active.client.request.assert_called_once_with("turn/steer", {
                    "threadId": "codex_thread", "turnId": "codex_turn",
                    "input": [{"type": "image", "url": data_url(), "detail": "high"}],
                })
                self.assertEqual(requested[0]["detail"], "low")
                event_payload = scheduler.state.append_event.call_args.args[4]
                self.assertEqual(event_payload["input"][0]["detail"], "low")

    def test_responses_preserves_mixed_user_content_order_for_turn_and_history(self) -> None:
        image = data_url()
        parsed = parse_responses_request(
            {
                "model": "vision",
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "Before "},
                            {"type": "input_image", "image_url": image, "detail": "high"},
                            {"type": "input_text", "text": "after."},
                        ],
                    }
                ],
            }
        )

        self.assertEqual(parsed.current_text, "Before after.")
        self.assertEqual(parsed.request_history, ())
        self.assertEqual(
            list(parsed.canonical_input[0]["content"]),
            [
                {"type": "input_text", "text": "Before "},
                {"type": "input_image", "image_url": image, "detail": "high"},
                {"type": "input_text", "text": "after."},
            ],
        )
        self.assertEqual(
            parsed.turn_input(),
            [
                {"type": "text", "text": "Before ", "text_elements": []},
                {"type": "image", "url": image, "detail": "high"},
                {"type": "text", "text": "after.", "text_elements": []},
            ],
        )

    def test_chat_accepts_an_image_only_user_message_and_canonicalizes_history(self) -> None:
        image = data_url("image/gif", b"GIF89a")
        parsed = parse_chat_request(
            {
                "model": "vision",
                "messages": [
                    {"role": "user", "content": [{"type": "image_url", "image_url": {"url": image}}]},
                    {"role": "assistant", "content": "I see it."},
                    {"role": "user", "content": "Describe it again."},
                ],
            }
        )

        self.assertEqual(parsed.request_history[0]["content"], [{"type": "input_image", "image_url": image}])
        self.assertEqual(parsed.request_history[1]["content"], [{"type": "output_text", "text": "I see it."}])
        self.assertEqual(parsed.current_text, "Describe it again.")

        image_only = parse_chat_request(
            {
                "model": "vision",
                "messages": [
                    {"role": "user", "content": [{"type": "image_url", "image_url": {"url": image, "detail": "low"}}]}
                ],
            }
        )
        self.assertEqual(image_only.current_text, "")
        self.assertEqual(image_only.turn_input(), [{"type": "image", "url": image, "detail": "low"}])

    def test_chat_completion_limits_are_accepted_and_discarded_without_validation(self) -> None:
        parsed = parse_chat_request(
            {
                "model": "text",
                "max_tokens": {"any": "value"},
                "max_completion_tokens": ["also", "ignored"],
                "messages": [{"role": "user", "content": "Hello"}],
            }
        )

        self.assertEqual(parsed.current_text, "Hello")
        self.assertNotIn("max_tokens", parsed.__dict__)
        self.assertNotIn("max_completion_tokens", parsed.__dict__)

    def test_images_are_limited_to_user_messages_and_inline_data_urls(self) -> None:
        with self.assertRaisesRegex(OpenAIProtocolError, "only in user messages"):
            parse_chat_request(
                {
                    "model": "vision",
                    "messages": [
                        {"role": "system", "content": [{"type": "image_url", "image_url": {"url": data_url()}}]},
                        {"role": "user", "content": "Hello"},
                    ],
                }
            )
        with self.assertRaisesRegex(OpenAIProtocolError, "inline base64 image data URLs"):
            parse_responses_request(
                {
                    "model": "vision",
                    "input": [
                        {
                            "role": "user",
                            "content": [{"type": "input_image", "image_url": "https://example.com/image.png"}],
                        }
                    ],
                }
            )
        with self.assertRaisesRegex(OpenAIProtocolError, "does not match its declared MIME type"):
            parse_responses_request(
                {
                    "model": "vision",
                    "input": [
                        {
                            "role": "user",
                            "content": [{"type": "input_image", "image_url": data_url("image/jpeg")}],
                        }
                    ],
                }
            )

    def test_image_count_and_total_byte_limits_do_not_require_large_test_payloads(self) -> None:
        image = data_url()
        request = {
            "model": "vision",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": image},
                        {"type": "input_image", "image_url": image},
                    ],
                }
            ],
        }
        with patch("codex_broker.openai_protocol.OPENAI_MAX_IMAGES_PER_REQUEST", 1):
            with self.assertRaisesRegex(OpenAIProtocolError, "at most 1 images"):
                parse_responses_request(request)
        with patch("codex_broker.openai_protocol.OPENAI_MAX_IMAGE_TOTAL_BYTES", 8):
            with self.assertRaisesRegex(OpenAIProtocolError, "total at most 8"):
                parse_responses_request(request)
        with patch("codex_broker.openai_protocol.OPENAI_MAX_IMAGE_BYTES", 7):
            with self.assertRaisesRegex(OpenAIProtocolError, "at most 7 decoded bytes"):
                parse_responses_request(request)

    def test_invalid_image_fields_report_the_correct_parameter(self) -> None:
        for image, param in (
            ({"url": data_url(), "detail": []}, "messages.0.content.0.image_url.detail"),
            ({"url": "data:image/png;base64,a"}, "messages.0.content.0.image_url.url"),
            ({"url": "file:///etc/passwd"}, "messages.0.content.0.image_url.url"),
            ({"url": data_url("image/svg+xml")}, "messages.0.content.0.image_url.url"),
        ):
            with self.subTest(image=image):
                with self.assertRaises(OpenAIProtocolError) as raised:
                    parse_chat_request({
                        "model": "vision",
                        "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": image}]}],
                    })
                self.assertEqual(raised.exception.param, param)

    def test_assistant_text_with_annotations_can_still_be_replayed(self) -> None:
        parsed = parse_responses_request({
            "model": "vision",
            "input": [
                {"role": "assistant", "content": [{"type": "output_text", "text": "Earlier", "annotations": []}]},
                {"role": "user", "content": [{"type": "input_image", "image_url": data_url()}]},
            ],
        })
        self.assertEqual(parsed.request_history[0]["content"], [{"type": "output_text", "text": "Earlier"}])


if __name__ == "__main__":
    unittest.main()
