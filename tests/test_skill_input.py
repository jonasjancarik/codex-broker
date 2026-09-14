from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_broker.bundles import BundleError, BundleSkillUnavailableError
from codex_broker.runtime_errors import (
    BUNDLE_SKILL_UNAVAILABLE,
    BUNDLE_SKILL_UNAVAILABLE_PUBLIC_MESSAGE,
    classify_runtime_error,
    classify_runtime_exception,
)
from codex_broker.scheduler_config import build_input
from codex_broker.services import BrokerServices
from tests.test_broker import config_for, wait_turn


class SkillInputTests(unittest.TestCase):
    def test_native_skill_inputs_include_exact_snapshot_paths_for_each_fresh_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = config_for(Path(raw))
            services = BrokerServices.build(config)
            try:
                bundle = self._bundle_with_skills(services, "two-skills", ("first", "second"))
                host_input = [{"type": "text", "text": "Host turn.", "text_elements": []}]

                self.assertEqual(build_input(host_input, None), host_input)

                first = services.bundles.materialize(bundle, "fresh-turn")
                second = services.bundles.materialize(bundle, "resumed-turn")
                assert first is not None and second is not None

                for overlay in (first, second):
                    first_path = overlay / ".agents" / "skills" / "first" / "SKILL.md"
                    second_path = overlay / ".agents" / "skills" / "second" / "SKILL.md"
                    self.assertEqual(
                        build_input(host_input, bundle, overlay),
                        [
                            {"type": "skill", "name": "first", "path": str(first_path)},
                            self._skill_instruction(first_path),
                            {"type": "skill", "name": "second", "path": str(second_path)},
                            self._skill_instruction(second_path),
                            {"type": "text", "text": "Use the attached skills.", "text_elements": []},
                            *host_input,
                        ],
                    )
                self.assertNotEqual(first, second)
            finally:
                services.pool.close_all()
                services.state.close()

    def test_missing_or_unreadable_snapshot_uses_typed_required_skill_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = config_for(Path(raw))
            services = BrokerServices.build(config)
            try:
                bundle = self._bundle_with_skills(services, "one-skill", ("required",))
                overlay = services.bundles.materialize(bundle, "missing-snapshot")
                assert overlay is not None
                skill_path = overlay / ".agents" / "skills" / "required" / "SKILL.md"
                skill_path.unlink()
                with self.assertRaises(BundleSkillUnavailableError) as missing:
                    build_input([], bundle, overlay)
                self._assert_required_skill_error(missing.exception)

                unreadable_overlay = services.bundles.materialize(bundle, "unreadable-snapshot")
                assert unreadable_overlay is not None
                with patch.object(Path, "open", side_effect=PermissionError("snapshot read denied")):
                    with self.assertRaises(BundleSkillUnavailableError) as unreadable:
                        build_input([], bundle, unreadable_overlay)
                self._assert_required_skill_error(unreadable.exception)
            finally:
                services.pool.close_all()
                services.state.close()

    def test_missing_source_during_snapshot_is_typed_with_its_materialization_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = config_for(Path(raw))
            services = BrokerServices.build(config)
            try:
                bundle = self._bundle_with_skills(services, "missing-source", ("required",))
                bundle.skills[0].path.unlink()

                with self.assertRaises(BundleSkillUnavailableError) as raised:
                    services.bundles.materialize(bundle, "source-missing")

                self.assertEqual(raised.exception.skill_name, "required")
                self.assertIn("Mounted skill is missing SKILL.md", str(raised.exception))
                classified = classify_runtime_exception(raised.exception)
                self.assertEqual(classified.code, BUNDLE_SKILL_UNAVAILABLE)
                self.assertEqual(classified.public_message, BUNDLE_SKILL_UNAVAILABLE_PUBLIC_MESSAGE)
                self.assertIn("Mounted skill is missing SKILL.md", classified.admin_message)
            finally:
                services.pool.close_all()
                services.state.close()

    def test_turn_persists_typed_skill_preparation_failure_before_starting_codex(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = config_for(Path(raw))
            services = BrokerServices.build(config)
            try:
                bundle = self._bundle_with_skills(services, "turn-missing-snapshot", ("required",))
                thread = services.scheduler.create_thread(
                    "owner-a",
                    {"bundleId": bundle.bundle_id, "cwd": str(config.allowed_workspace_roots[0])},
                )
                original_materialize = services.bundles.materialize_with_provenance

                def materialize_then_remove(*args: object, **kwargs: object):
                    overlay = original_materialize(*args, **kwargs)
                    assert overlay is not None
                    (overlay.path / ".agents" / "skills" / "required" / "SKILL.md").unlink()
                    return overlay

                with (
                    patch.object(services.bundles, "materialize_with_provenance", side_effect=materialize_then_remove),
                    patch.object(services.pool, "checkout", side_effect=AssertionError("Codex must not start")),
                ):
                    started = services.scheduler.start_turn(
                        "owner-a",
                        thread["threadId"],
                        {"input": [{"type": "text", "text": "Start the task.", "text_elements": []}]},
                    )
                    failed = wait_turn(services, "owner-a", thread["threadId"], started["turnId"])

                self.assertEqual(failed["status"], "failed")
                self.assertEqual(failed["errorCode"], BUNDLE_SKILL_UNAVAILABLE)
                self.assertEqual(failed["publicMessage"], BUNDLE_SKILL_UNAVAILABLE_PUBLIC_MESSAGE)
                self.assertEqual(failed["error"], BUNDLE_SKILL_UNAVAILABLE_PUBLIC_MESSAGE)
                self.assertEqual(failed["adminMessage"], "Materialized skill is unavailable: required")
            finally:
                services.pool.close_all()
                services.state.close()

    def test_initial_and_resumed_turns_each_receive_their_own_skill_snapshot_input(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = config_for(Path(raw))
            services = BrokerServices.build(config)
            try:
                bundle = self._bundle_with_skills(services, "turn-skill-input", ("required",))
                thread = services.scheduler.create_thread(
                    "owner-a",
                    {"bundleId": bundle.bundle_id, "cwd": str(config.allowed_workspace_roots[0])},
                )
                observed_inputs: list[list[dict[str, object]]] = []
                original_turn_params = services.scheduler._turn_params

                def observe_turn_params(
                    codex_thread_id: str,
                    input_items: list[dict[str, object]],
                    *args: object,
                    **kwargs: object,
                ) -> dict[str, object]:
                    observed_inputs.append(input_items)
                    return original_turn_params(codex_thread_id, input_items, *args, **kwargs)

                with patch.object(services.scheduler, "_turn_params", side_effect=observe_turn_params):
                    first = services.scheduler.start_turn(
                        "owner-a",
                        thread["threadId"],
                        {"input": [{"type": "text", "text": "First turn.", "text_elements": []}]},
                    )
                    self.assertEqual(wait_turn(services, "owner-a", thread["threadId"], first["turnId"])["status"], "completed")
                    second = services.scheduler.start_turn(
                        "owner-a",
                        thread["threadId"],
                        {"input": [{"type": "text", "text": "Resumed turn.", "text_elements": []}]},
                    )
                    self.assertEqual(wait_turn(services, "owner-a", thread["threadId"], second["turnId"])["status"], "completed")

                self.assertEqual(len(observed_inputs), 2)
                paths = []
                for input_items in observed_inputs:
                    skill_item, instruction = input_items[:2]
                    path = Path(str(skill_item["path"]))
                    self.assertEqual(skill_item, {"type": "skill", "name": "required", "path": str(path)})
                    self.assertEqual(instruction, self._skill_instruction(path))
                    self.assertEqual(input_items[2]["text"], "Use the attached skills.")
                    paths.append(path)
                self.assertNotEqual(paths[0].parents[3], paths[1].parents[3])
                self.assertIsNotNone(services.scheduler.get_thread("owner-a", thread["threadId"])["codexThreadId"])
            finally:
                services.pool.close_all()
                services.state.close()

    def test_only_the_typed_preparation_error_uses_the_required_skill_code(self) -> None:
        self.assertEqual(classify_runtime_error("Permission denied").code, "codex_runtime_error")
        self.assertEqual(classify_runtime_exception(BundleError("Prompt is unavailable")).code, "codex_runtime_error")

    def _assert_required_skill_error(self, error: BundleSkillUnavailableError) -> None:
        classified = classify_runtime_exception(error)
        self.assertEqual(classified.code, BUNDLE_SKILL_UNAVAILABLE)
        self.assertEqual(classified.public_message, BUNDLE_SKILL_UNAVAILABLE_PUBLIC_MESSAGE)
        self.assertTrue(classified.admin_message.startswith("Materialized skill is unavailable: required"))

    @staticmethod
    def _skill_instruction(path: Path) -> dict[str, object]:
        return {
            "type": "text",
            "text": (
                f"Read and follow the verified skill at {path}. "
                f"Resolve every relative file named by that skill from {path.parent}."
            ),
            "text_elements": [],
        }

    @staticmethod
    def _bundle_with_skills(services: BrokerServices, bundle_id: str, names: tuple[str, ...]):
        bundle_dir = services.config.allowed_bundle_roots[0] / bundle_id
        skills = []
        for name in names:
            skill_dir = bundle_dir / "skills" / name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
            skills.append({"name": name, "source": {"type": "mount", "path": str(skill_dir)}})
        (bundle_dir / "bundle.json").write_text(
            json.dumps({"id": bundle_id, "instructions": ["Use the attached skills."], "skills": skills}),
            encoding="utf-8",
        )
        bundle = services.bundles.resolve(bundle_id)
        assert bundle is not None
        return bundle
