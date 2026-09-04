from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codex_broker.app_server import AppServerClient, AppServerError
from codex_broker.openai_api import _app_server_models, _resolve_model, _visible_models
from codex_broker.openai_auth import OpenAICompatBinding
from codex_broker.openai_protocol import OpenAIProtocolError
from codex_broker.services import BrokerServices
from test_broker import config_for


class OpenAIModelAliasTests(unittest.TestCase):
    def test_model_catalog_checkout_cannot_be_evicted_while_request_is_in_flight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = replace(config_for(Path(tmp_raw)), max_pooled_app_servers=1)
            services = BrokerServices.build(config)
            services.auth.login_api_key("owner", "sk-test", "default")
            binding = OpenAICompatBinding(owner_id="owner")
            entered = threading.Event()
            continue_request = threading.Event()
            result: list[list[dict[str, object]]] = []
            errors: list[BaseException] = []
            original_request = AppServerClient.request
            testcase = self

            def blocking_request(self: AppServerClient, method: str, *args: object, **kwargs: object) -> dict[str, object]:
                if method == "model/list":
                    entered.set()
                    testcase.assertTrue(continue_request.wait(2))
                return original_request(self, method, *args, **kwargs)

            def load_models() -> None:
                try:
                    result.append(_app_server_models(services, binding))
                except BaseException as exc:  # surfaced after the synchronization assertions below
                    errors.append(exc)

            worker = threading.Thread(target=load_models)
            try:
                with patch.object(AppServerClient, "request", new=blocking_request):
                    worker.start()
                    self.assertTrue(entered.wait(2))
                    scope = services.auth.resolve_scope("owner")
                    profile = services.auth.profile_key("default")
                    with self.assertRaisesRegex(AppServerError, "capacity is exhausted"):
                        services.pool.checkout(
                            auth_principal_hash=scope.auth_principal_hash,
                            profile=profile,
                            codex_home=services.auth.profile_home(scope.auth_principal_hash, profile),
                            runtime_home=services.auth.runtime_home(scope.auth_principal_hash, profile),
                            config_profile="other",
                            mcp_servers=(),
                            auth_fingerprint=services.auth.auth_fingerprint(scope.auth_principal_hash, profile),
                        )
                    continue_request.set()
                    worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(result[0][0]["model"], "gpt-5.6-sol")
            finally:
                continue_request.set()
                worker.join(2)
                services.pool.close_all()
                services.state.close()

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
