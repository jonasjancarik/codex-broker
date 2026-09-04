from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
import re
from typing import Any

from .bundles import BundleError, ResolvedBundle, materialized_skill_path


_MANAGED_SANDBOX_PERMISSIONS = {
    "read-only": "broker-read-only",
    "workspace-write": "broker-workspace-write",
}
_CALLER_MANAGED_FIELDS = ("permissions", "runtimeWorkspaceRoots", "permissionProfile", "sandboxPolicy")
_APPROVAL_REVIEWERS = {"user", "auto_review"}


def _managed_approval_policy() -> dict[str, Any]:
    """Allow reviewed grants without permitting an unsandboxed shell bypass."""
    return {
        "granular": {
            "mcp_elicitations": True,
            "request_permissions": True,
            "rules": True,
            "sandbox_approval": False,
            "skill_approval": False,
        }
    }


def request_config_profile(body: dict[str, Any], fallback: Any = "default") -> str:
    return str(body.get("configProfile") or body.get("runtimeProfile") or fallback or "default")


def request_codex_options(body: dict[str, Any]) -> dict[str, Any]:
    options: dict[str, Any] = {}
    for key in ("runtime", "codexOptions"):
        value = body.get(key)
        if isinstance(value, dict):
            options.update(value)
    return options


def codex_option(options: dict[str, Any], profile: dict[str, Any], key: str, *aliases: str) -> Any:
    for source in (options, profile):
        for candidate in (key, *aliases):
            if source.get(candidate) is not None:
                return source[candidate]
    return None


def format_codex_config_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def feature_config_key(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", value).strip("._-")
    if not name:
        raise ValueError("Feature name must contain at least one alphanumeric character.")
    return f"features.{name}"


def _reject_caller_managed_fields(body: dict[str, Any], options: dict[str, Any]) -> None:
    for source in (body, options):
        for field in _CALLER_MANAGED_FIELDS:
            if field in source:
                raise ValueError(f"{field} is managed by the broker and cannot be supplied by callers.")


def _reject_profile_managed_fields(profile: dict[str, Any]) -> None:
    for field in _CALLER_MANAGED_FIELDS:
        if field in profile:
            raise ValueError(
                f"Configuration profile field {field} is managed by the broker; "
                "select sandbox with read-only, workspace-write, or danger-full-access instead."
            )


def _is_internal_overlay(scheduler: Any, path: Path) -> bool:
    return path.is_relative_to(scheduler.config.overlay_root.resolve())


def _effective_workspace_roots(
    scheduler: Any,
    cwd: Path | None,
    profile: dict[str, Any],
    runtime_read_root: Path | None = None,
    *,
    allow_internal_overlay: bool = False,
) -> list[str]:
    if cwd is None:
        raise ValueError("A managed sandbox requires an effective cwd.")
    if not cwd.is_absolute():
        raise ValueError("Managed sandbox cwd must be absolute.")
    effective_cwd = cwd.resolve()
    allowed_roots = [Path(root).expanduser().resolve() for root in scheduler.config.allowed_workspace_roots]
    if not (
        any(effective_cwd.is_relative_to(root) for root in allowed_roots)
        or (allow_internal_overlay and _is_internal_overlay(scheduler, effective_cwd))
    ):
        raise BundleError(f"cwd is outside broker workspace roots: {effective_cwd}")
    validate_config_profile_cwd(
        scheduler,
        effective_cwd,
        profile,
        allow_internal_overlay=allow_internal_overlay,
    )
    roots = [str(effective_cwd)]
    if runtime_read_root is not None:
        effective_read_root = runtime_read_root.resolve()
        if not _is_internal_overlay(scheduler, effective_read_root):
            raise BundleError(f"Runtime overlay is outside broker overlay root: {effective_read_root}")
        if str(effective_read_root) not in roots:
            roots.append(str(effective_read_root))
    return roots


def _sandbox_choice(
    options: dict[str, Any],
    bundle: ResolvedBundle | None,
    profile: dict[str, Any],
) -> str | None:
    # Keep the historic request > bundle > profile order for public choices only.
    value = options.get("sandbox")
    if value is None and bundle and bundle.sandbox_mode is not None:
        value = bundle.sandbox_mode
    if value is None:
        value = profile.get("sandbox")
    if value is None:
        return "read-only"
    if not isinstance(value, str) or value not in {*_MANAGED_SANDBOX_PERMISSIONS, "danger-full-access"}:
        raise ValueError("sandbox must be read-only, workspace-write, or danger-full-access.")
    return value


def _execution_policy_params(
    scheduler: Any,
    cwd: Path | None,
    body: dict[str, Any],
    bundle: ResolvedBundle | None,
    profile: dict[str, Any],
    *,
    danger_full_access_authorized: bool,
    runtime_read_root: Path | None = None,
    allow_internal_overlay: bool = False,
) -> dict[str, Any]:
    options = request_codex_options(body)
    _reject_caller_managed_fields(body, options)
    sandbox = _sandbox_choice(options, bundle, profile)
    reviewer = codex_option(options, profile, "approvalsReviewer", "approvals_reviewer")
    if reviewer is not None and reviewer not in _APPROVAL_REVIEWERS:
        raise ValueError("approvalsReviewer must be user or auto_review.")
    approval_policy = codex_option(options, profile, "approvalPolicy")
    if reviewer == "auto_review" and approval_policy == "never":
        raise ValueError("approvalPolicy never is incompatible with approvalsReviewer auto_review.")

    params: dict[str, Any] = {}
    if sandbox in _MANAGED_SANDBOX_PERMISSIONS:
        params["permissions"] = _MANAGED_SANDBOX_PERMISSIONS[sandbox]
        params["runtimeWorkspaceRoots"] = _effective_workspace_roots(
            scheduler,
            cwd,
            profile,
            runtime_read_root,
            allow_internal_overlay=allow_internal_overlay,
        )
        params["approvalsReviewer"] = reviewer or "auto_review"
        if approval_policy is None:
            params["approvalPolicy"] = _managed_approval_policy()
        elif approval_policy == "never" and params["approvalsReviewer"] == "user":
            params["approvalPolicy"] = approval_policy
        elif isinstance(approval_policy, dict):
            granular = approval_policy.get("granular")
            if not isinstance(granular, dict) or granular.get("sandbox_approval") is not False:
                raise ValueError(
                    "Managed sandbox approvalPolicy must disable sandbox_approval; use request_permissions for reviewed exceptions."
                )
            params["approvalPolicy"] = approval_policy
        else:
            raise ValueError(
                "Managed sandbox approvalPolicy must use the broker granular policy or never with user review."
            )
    elif sandbox == "danger-full-access":
        if not danger_full_access_authorized:
            raise ValueError("danger-full-access requires separate authorization.")
        params["sandbox"] = sandbox
        if approval_policy is not None:
            params["approvalPolicy"] = approval_policy
        if reviewer is not None:
            params["approvalsReviewer"] = reviewer
    else:
        if approval_policy is not None:
            params["approvalPolicy"] = approval_policy
        if reviewer is not None:
            params["approvalsReviewer"] = reviewer
    return params


def thread_params(
    scheduler: Any,
    cwd: Path | None,
    body: dict[str, Any],
    bundle: ResolvedBundle | None,
    profile: dict[str, Any] | None = None,
    *,
    danger_full_access_authorized: bool = False,
    runtime_read_root: Path | None = None,
    allow_internal_overlay: bool = False,
) -> dict[str, Any]:
    options = request_codex_options(body)
    profile = profile or {}
    params: dict[str, Any] = {}
    if cwd:
        params["cwd"] = str(cwd)
    for key in ("model", "personality", "baseInstructions", "developerInstructions"):
        value = codex_option(options, profile, key)
        if value is not None:
            params[key] = value
    params.update(
        _execution_policy_params(
            scheduler,
            cwd,
            body,
            bundle,
            profile,
            danger_full_access_authorized=danger_full_access_authorized,
            runtime_read_root=runtime_read_root,
            allow_internal_overlay=allow_internal_overlay,
        )
    )
    return params


def turn_params(
    scheduler: Any,
    codex_thread_id: str,
    input_items: list[dict[str, Any]],
    body: dict[str, Any],
    profile: dict[str, Any] | None = None,
    *,
    cwd: Path | None = None,
    bundle: ResolvedBundle | None = None,
    danger_full_access_authorized: bool = False,
    runtime_read_root: Path | None = None,
    allow_internal_overlay: bool = False,
) -> dict[str, Any]:
    options = request_codex_options(body)
    profile = profile or {}
    params: dict[str, Any] = {"threadId": codex_thread_id, "input": codex_image_items(input_items)}
    for request_key, app_server_key, aliases in (
        ("serviceTier", "serviceTier", ()),
        ("model", "model", ()),
        ("effort", "effort", ("reasoningEffort",)),
        ("personality", "personality", ()),
        ("summary", "summary", ("reasoningSummary",)),
    ):
        value = codex_option(options, profile, request_key, *aliases)
        if value is not None:
            params[app_server_key] = value
    output_schema = codex_option(options, profile, "outputSchema", "output_schema")
    if output_schema is not None:
        params["outputSchema"] = output_schema
    params.update(
        _execution_policy_params(
            scheduler,
            cwd,
            body,
            bundle,
            profile,
            danger_full_access_authorized=danger_full_access_authorized,
            runtime_read_root=runtime_read_root,
            allow_internal_overlay=allow_internal_overlay,
        )
    )
    return params


def codex_image_items(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map unsupported low image detail on execution copies, preserving stored input."""
    result: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            result.append(item)
            continue
        prepared = dict(item)
        # Codex 0.153.2 can replace low-detail images with an omission notice.
        if prepared.get("type") in ("image", "localImage", "input_image") and prepared.get("detail") == "low":
            prepared["detail"] = "high"
        if prepared.get("type") == "message" and isinstance(prepared.get("content"), list):
            prepared["content"] = codex_image_items(prepared["content"])
        result.append(prepared)
    return result


def build_input(
    input_items: list[dict[str, Any]],
    bundle: ResolvedBundle | None,
    overlay: Path | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if bundle:
        if bundle.skills and overlay is None:
            raise BundleError("A bundle skill requires a materialized per-turn overlay.")
        assert overlay is not None or not bundle.skills
        for skill in bundle.skills:
            path = materialized_skill_path(overlay, skill)
            if not path.is_file():
                raise BundleError(f"Materialized skill is unavailable: {skill.name}")
            items.append({"type": "skill", "name": skill.name, "path": str(path)})
        if bundle.instructions:
            items.append({"type": "text", "text": "\n\n".join(bundle.instructions), "text_elements": []})
        for prompt in bundle.prompts:
            items.append(
                {
                    "type": "text",
                    "text": prompt.path.read_text(encoding="utf-8"),
                    "text_elements": [],
                    "name": prompt.name,
                }
            )
    return [*items, *input_items]


def config_profile_config(scheduler: Any, name: str) -> dict[str, Any]:
    if not scheduler.config.config_profiles:
        return {}
    profile = scheduler.config.config_profiles.get(name)
    if profile is None:
        raise ValueError(f"Unknown configuration profile: {name}")
    _reject_profile_managed_fields(profile)
    return profile


def validate_config_profile_bundle(profile: dict[str, Any], bundle_id: str | None) -> None:
    enabled = profile.get("enabledBundles")
    if enabled is None:
        enabled = profile.get("bundleIds") if profile.get("bundleIds") is not None else profile.get("bundles")
    if enabled is None or bundle_id is None:
        return
    allowed = {str(value) for value in enabled} if isinstance(enabled, list) else {str(enabled)}
    if bundle_id not in allowed:
        raise BundleError(f"Bundle {bundle_id} is not enabled for configuration profile.")


def validate_config_profile_cwd(
    scheduler: Any,
    cwd: Path | None,
    profile: dict[str, Any],
    *,
    allow_internal_overlay: bool = False,
) -> None:
    if cwd is None:
        return
    roots = profile.get("allowedWorkspaceRoots", profile.get("workspaceRoots"))
    if roots is None:
        return
    raw_roots = roots if isinstance(roots, list) else [roots]
    allowed_roots = [Path(str(value)).expanduser().resolve() for value in raw_roots]
    effective_cwd = cwd.resolve()
    if not (
        any(effective_cwd.is_relative_to(root) for root in allowed_roots)
        or (allow_internal_overlay and _is_internal_overlay(scheduler, effective_cwd))
    ):
        raise BundleError(f"cwd is outside configuration profile workspace roots: {cwd}")


def codex_process_config_args(
    scheduler: Any,
    body: dict[str, Any],
    profile: dict[str, Any] | None = None,
) -> tuple[tuple[str, str], ...]:
    options = request_codex_options(body)
    profile = profile or {}
    args: list[tuple[str, str]] = []
    for request_key, config_key, aliases in (
        ("webSearch", "web_search", ("web_search",)),
        ("modelVerbosity", "model_verbosity", ("model_verbosity",)),
        ("effort", "model_reasoning_effort", ("reasoningEffort", "modelReasoningEffort", "model_reasoning_effort")),
    ):
        value = codex_option(options, profile, request_key, *aliases)
        if value is not None:
            args.append((config_key, format_codex_config_value(value)))
    features: dict[str, Any] = {}
    for key in ("imageGeneration", "features.image_generation"):
        if profile.get(key) is not None:
            features["image_generation"] = profile[key]
    if isinstance(profile.get("features"), dict):
        features.update(profile["features"])
    for key in ("imageGeneration", "features.image_generation"):
        if options.get(key) is not None:
            features["image_generation"] = options[key]
    if isinstance(options.get("features"), dict):
        features.update(options["features"])
    for name, value in sorted(features.items()):
        if value is not None:
            args.append((feature_config_key(str(name)), format_codex_config_value(value)))
    return tuple(args)
