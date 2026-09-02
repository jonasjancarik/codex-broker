from __future__ import annotations

import unittest
from unittest.mock import patch

from codex_broker.openai_api import _resolve_model, _visible_models
from codex_broker.openai_auth import OpenAICompatBinding
from codex_broker.openai_protocol import OpenAIProtocolError


class OpenAIModelAliasTests(unittest.TestCase):
    def test_default_applies_to_omitted_and_empty_custom_aliases(self) -> None:
        for binding in (
            OpenAICompatBinding(owner_id="owner"),
            OpenAICompatBinding.from_json({"ownerId": "owner"}),
            OpenAICompatBinding.from_json({"ownerId": "owner", "modelAliases": {}}),
        ):
            self.assertEqual(binding.model_aliases, {"gpt-5.6": "gpt-5.6-sol"})

    def test_custom_aliases_extend_defaults_and_overrides_are_binding_scoped(self) -> None:
        custom = {"gpt-5.6": "gpt-5.6-luna", "fast": "gpt-5.6-luna"}
        binding = OpenAICompatBinding.from_json({"ownerId": "owner", "modelAliases": custom})
        with patch("codex_broker.openai_api._app_server_models", return_value=[
            {"model": "gpt-5.6-sol"}, {"model": "gpt-5.6-luna"},
        ]):
            self.assertEqual(_resolve_model(None, binding, "gpt-5.6"), "gpt-5.6-luna")
            self.assertEqual(_visible_models(None, binding)["fast"], "gpt-5.6-luna")
        binding.model_aliases["gpt-5.6"] = "changed"
        self.assertEqual(custom["gpt-5.6"], "gpt-5.6-luna")
        self.assertEqual(OpenAICompatBinding(owner_id="other").model_aliases, {"gpt-5.6": "gpt-5.6-sol"})
        extended = OpenAICompatBinding(owner_id="third", model_aliases={"fast": "gpt-5.6-luna"})
        self.assertEqual(extended.model_aliases, {"gpt-5.6": "gpt-5.6-sol", "fast": "gpt-5.6-luna"})

    def test_alias_is_not_advertised_or_resolved_when_its_target_is_unavailable(self) -> None:
        for custom in ({}, {"gpt-5.6": "unavailable"}):
            with self.subTest(custom=custom):
                binding = OpenAICompatBinding(owner_id="owner", model_aliases=custom)
                models = [] if not custom else [{"model": "gpt-5.6-sol"}]
                with patch("codex_broker.openai_api._app_server_models", return_value=models):
                    self.assertNotIn("gpt-5.6", _visible_models(None, binding))
                    with self.assertRaises(OpenAIProtocolError) as raised:
                        _resolve_model(None, binding, "gpt-5.6")
                    self.assertEqual(raised.exception.code, "model_not_found")
