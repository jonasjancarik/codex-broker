"""A bounded, no-model check that Codex's managed Linux sandbox is usable."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from .auth import render_managed_codex_config
from .bundles import BundleRegistry, materialized_skill_path
from .config import BrokerConfig
from .state import StateStore
from .util import clean_process_env, ensure_dir, redact, utc_now


PROBE_PROFILE = "broker-workspace-write"
READ_ONLY_PROBE_PROFILE = "broker-read-only"


@dataclass(frozen=True)
class SandboxProbeResult:
    status: str
    platform: str
    backend: str | None
    codex_version: str | None
    permission_profile: str
    checked_at: str
    duration_seconds: float
    admin_diagnostic: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "platform": self.platform,
            "backend": self.backend,
            "codexVersion": self.codex_version,
            "permissionProfile": self.permission_profile,
            "checkedAt": self.checked_at,
            "durationSeconds": self.duration_seconds,
        }


class SandboxProbe:
    """Runs once per process and makes later callers observe the cached result."""

    def __init__(
        self,
        config: BrokerConfig,
        *,
        platform_name: str | None = None,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        version_provider: Callable[[tuple[str, ...], float], str | None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        startup_timeout_seconds: float = 5.0,
        request_timeout_seconds: float = 5.0,
    ) -> None:
        self.config = config
        self.platform_name = platform_name or sys.platform
        self._popen_factory = popen_factory
        self._version_provider = version_provider or self._default_version
        self._monotonic = monotonic
        self.startup_timeout_seconds = startup_timeout_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self._lock = threading.RLock()
        self._result: SandboxProbeResult | None = None

    def result(self) -> SandboxProbeResult | None:
        with self._lock:
            return self._result

    def run_once(self) -> SandboxProbeResult:
        with self._lock:
            if self._result is not None:
                return self._result
            started = self._monotonic()
            if self.config.sandbox_preflight_mode == "disabled":
                result = self._make_result("skipped", started, backend=None)
            elif self.platform_name != "linux":
                result = self._make_result("unsupported", started, backend=None)
            else:
                result = self._run_linux(started)
            self._result = result
            return result

    def mark_unhealthy(self, diagnostic: str) -> SandboxProbeResult:
        """Invalidate a formerly healthy result after a classified runtime failure."""
        with self._lock:
            previous = self._result
            self._result = SandboxProbeResult(
                status="failed",
                platform=self.platform_name,
                backend="bubblewrap" if self.platform_name == "linux" else None,
                codex_version=previous.codex_version if previous else None,
                permission_profile=PROBE_PROFILE,
                checked_at=utc_now(),
                duration_seconds=0.0,
                admin_diagnostic=redact(diagnostic),
            )
            return self._result

    def _run_linux(self, started: float) -> SandboxProbeResult:
        process: Any | None = None
        state: StateStore | None = None
        version = self._safe_version()
        try:
            with tempfile.TemporaryDirectory(prefix="codex-broker-sandbox-") as root_text:
                root = Path(root_text)
                home = root / "codex-home"
                jobs = root / "jobs"
                workspace = jobs / "own-job"
                sibling_workspace = jobs / "sibling-job"
                bundle_root = root / "bundles"
                bundle_dir = bundle_root / "sandbox-probe"
                mounted_skill = bundle_dir / "skills" / "normalize-report-references"
                canary = root / "protected-canary"
                control_plane = workspace / "control-plane"
                home.mkdir()
                workspace.mkdir(parents=True)
                sibling_workspace.mkdir(parents=True)
                mounted_skill.mkdir(parents=True)
                control_plane.mkdir()
                canary.write_text("sandbox-probe-canary", encoding="utf-8")
                (control_plane / "secret-canary").write_text("control-plane-canary", encoding="utf-8")
                (workspace / "ordinary.txt").write_text("ordinary", encoding="utf-8")
                (workspace / ".env").write_text("SECRET=workspace-env-canary", encoding="utf-8")
                (sibling_workspace / "sibling-sentinel").write_text("sibling-job-canary", encoding="utf-8")
                (sibling_workspace / "output").mkdir()
                (sibling_workspace / ".agents" / "skills").mkdir(parents=True)
                skill_marker = "sandbox-probe-skill-v1\n"
                source_skill = mounted_skill / "SKILL.md"
                source_skill.write_text(skill_marker, encoding="utf-8")
                source_skill.chmod(0o444)
                mounted_skill.chmod(0o555)
                probe_config = replace(
                    self.config,
                    data_dir=root / "broker-data",
                    allowed_workspace_roots=(workspace,),
                    allowed_bundle_roots=(bundle_root,),
                    sandbox_deny_paths=(*self.config.sandbox_deny_paths, control_plane),
                )
                for path in (
                    probe_config.data_dir,
                    probe_config.auth_root,
                    probe_config.inline_bundle_root,
                    probe_config.overlay_root,
                    probe_config.runtime_home_root,
                ):
                    ensure_dir(path)
                bundle_dir.mkdir(exist_ok=True)
                (bundle_dir / "bundle.json").write_text(
                    json.dumps(
                        {
                            "id": "sandbox-probe",
                            "skills": [
                                {
                                    "name": "normalize-report-references",
                                    "source": {"type": "mount", "path": str(mounted_skill)},
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                state = StateStore(probe_config.state_db_path)
                bundles = BundleRegistry(probe_config, state)
                bundle = bundles.resolve("sandbox-probe")
                assert bundle is not None
                overlay = bundles.materialize(bundle, "turn-sandbox-probe")
                assert overlay is not None
                sibling_overlay = bundles.materialize(bundle, "turn-sibling-probe")
                assert sibling_overlay is not None
                attached_skill = materialized_skill_path(overlay, bundle.skills[0])
                sibling_skill = materialized_skill_path(sibling_overlay, bundle.skills[0])
                if attached_skill.is_symlink() or attached_skill.parent.is_symlink():
                    raise RuntimeError("managed sandbox materialized the attached skill as a symlink")
                if attached_skill.stat().st_ino == source_skill.stat().st_ino:
                    raise RuntimeError("managed sandbox materialized the attached skill as a linked source")
                if sibling_skill.stat().st_ino == attached_skill.stat().st_ino:
                    raise RuntimeError("managed sandbox reused a skill snapshot across jobs")
                config_path = home / "config.toml"
                config_path.write_text(render_managed_codex_config(probe_config), encoding="utf-8")
                config_path.chmod(0o600)
                environment = clean_process_env(self.config.codex_passthrough_env)
                environment.update(
                    {
                        "CODEX_HOME": str(home),
                        "HOME": str(home),
                        "CODEX_CREDENTIAL_STORE": self.config.credential_store,
                        "FAKE_CODEX_PROBE_SKILL_SOURCE": str(source_skill),
                        "FAKE_CODEX_PROBE_SKILL_SNAPSHOT": str(attached_skill),
                    }
                )
                process = self._popen_factory(
                    [*self.config.codex_command, "app-server", "--listen", "stdio://", "--strict-config"],
                    cwd=str(workspace), env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, bufsize=1,
                )
                rpc = _ProbeRpc(process, self.request_timeout_seconds, self._monotonic)
                rpc.request("initialize", {"clientInfo": {"name": "codex-broker-sandbox-probe", "version": "1"}, "capabilities": {"experimentalApi": True}})
                rpc.notify("initialized", {})

                readable = rpc.request(
                    "command/exec",
                    {
                        "command": ["test", "-r", str(workspace / "ordinary.txt")],
                        "cwd": str(workspace),
                        "permissionProfile": READ_ONLY_PROBE_PROFILE,
                    },
                )
                if not _command_succeeded(readable):
                    raise RuntimeError(
                        "managed read-only sandbox could not read an ordinary workspace file: "
                        f"exitCode={readable.get('exitCode')!r}; "
                        f"stderr={redact(str(readable.get('stderr') or ''))}"
                    )
                attached_skill_readable = rpc.request(
                    "command/exec",
                    {
                        "command": [
                            "sh",
                            "-c",
                            f"test -r {attached_skill} && grep -qx sandbox-probe-skill-v1 {attached_skill}",
                        ],
                        "cwd": str(overlay),
                        "permissionProfile": READ_ONLY_PROBE_PROFILE,
                    },
                )
                if not _command_succeeded(attached_skill_readable):
                    raise RuntimeError("managed sandbox could not read the attached skill overlay")
                snapshot_marker = attached_skill.read_text(encoding="utf-8")
                snapshot_mode = attached_skill.stat().st_mode & 0o777
                snapshot_digest = hashlib.sha256(attached_skill.read_bytes()).hexdigest()
                # command/exec cannot accept runtimeWorkspaceRoots. Running it
                # from the materialized overlay with the production
                # workspace-write profile verifies that the attached snapshot
                # remains read-only during a managed turn; multi-root wiring is
                # covered by the thread/turn parameter tests.
                snapshot_mutation = rpc.request(
                    "command/exec",
                    {
                        "command": [
                            "sh",
                            "-c",
                            f"chmod u+w {attached_skill} && printf sandbox-probe-skill-mutated > {attached_skill}",
                        ],
                        "cwd": str(overlay),
                        "permissionProfile": PROBE_PROFILE,
                    },
                )
                if _command_succeeded(snapshot_mutation):
                    raise RuntimeError("managed sandbox could modify the attached skill snapshot")
                if attached_skill.read_text(encoding="utf-8") != snapshot_marker:
                    raise RuntimeError("attached skill snapshot content changed")
                if attached_skill.stat().st_mode & 0o777 != snapshot_mode:
                    raise RuntimeError("attached skill snapshot mode changed")
                if hashlib.sha256(attached_skill.read_bytes()).hexdigest() != snapshot_digest:
                    raise RuntimeError("attached skill snapshot digest changed")
                if source_skill.read_text(encoding="utf-8") != skill_marker:
                    raise RuntimeError("mounted skill target content changed")
                if source_skill.stat().st_mode & 0o777 != 0o444:
                    raise RuntimeError("mounted skill target mode changed")
                read_only_write = rpc.request(
                    "command/exec",
                    {
                        "command": ["sh", "-c", "printf denied > .sandbox-probe-read-only-write"],
                        "cwd": str(workspace),
                        "permissionProfile": READ_ONLY_PROBE_PROFILE,
                    },
                )
                if _command_succeeded(read_only_write) or (workspace / ".sandbox-probe-read-only-write").exists():
                    raise RuntimeError(
                        "managed read-only sandbox could write the probe workspace: "
                        f"exitCode={read_only_write.get('exitCode')!r}; "
                        f"stderr={redact(str(read_only_write.get('stderr') or ''))}"
                    )
                write = rpc.request(
                    "command/exec",
                    {
                        "command": ["sh", "-c", "printf ok > .sandbox-probe-write"],
                        "cwd": str(workspace),
                        "permissionProfile": PROBE_PROFILE,
                    },
                )
                if not _command_succeeded(write) or not (workspace / ".sandbox-probe-write").is_file():
                    raise RuntimeError(
                        "managed sandbox command could not write the probe workspace: "
                        f"exitCode={write.get('exitCode')!r}; stderr={redact(str(write.get('stderr') or ''))}"
                    )
                # command/exec in Codex 0.153.2 has no runtimeWorkspaceRoots parameter.
                # Its pinned processor reloads the named profile for this absolute cwd,
                # making cwd the production-equivalent runtime workspace root.
                denied_canaries = (
                    (canary, "protected canary"),
                    (control_plane / "secret-canary", "control-plane canary"),
                    (workspace / ".env", "workspace .env canary"),
                )
                for profile in (READ_ONLY_PROBE_PROFILE, PROBE_PROFILE):
                    for path, label in denied_canaries:
                        denied = rpc.request(
                            "command/exec",
                            {
                                "command": ["test", "!", "-r", str(path)],
                                "cwd": str(workspace),
                                "permissionProfile": profile,
                            },
                        )
                        if not _command_succeeded(denied):
                            raise RuntimeError(f"managed sandbox could read the {label} with {profile}")
                for path, label in (
                    (sibling_workspace / "sibling-sentinel", "sibling job sentinel"),
                    (sibling_workspace / "output", "sibling job output"),
                    (sibling_workspace / ".agents" / "skills", "sibling job skills"),
                    (sibling_skill, "sibling job skill snapshot"),
                ):
                    denied = rpc.request(
                        "command/exec",
                        {
                            "command": ["test", "!", "-r", str(path)],
                            "cwd": str(workspace),
                            "permissionProfile": PROBE_PROFILE,
                        },
                    )
                    if not _command_succeeded(denied):
                        raise RuntimeError(f"managed sandbox could discover the {label}")
                bundles.cleanup_overlay("turn-sandbox-probe")
                bundles.cleanup_overlay("turn-sibling-probe")
                if overlay.exists() or sibling_overlay.exists():
                    raise RuntimeError("managed sandbox did not remove per-turn skill snapshots")
                return self._make_result("healthy", started, backend="bubblewrap", version=version)
        except TimeoutError as exc:
            return self._make_result("failed", started, backend="bubblewrap", version=version, diagnostic=f"sandbox probe timed out: {exc}")
        except Exception as exc:
            return self._make_result("failed", started, backend="bubblewrap", version=version, diagnostic=str(exc))
        finally:
            if process is not None:
                _reap(process)
            if state is not None:
                state.close()

    def _make_result(self, status: str, started: float, *, backend: str | None, version: str | None = None, diagnostic: str | None = None) -> SandboxProbeResult:
        return SandboxProbeResult(status, self.platform_name, backend, version, PROBE_PROFILE, utc_now(), max(0.0, self._monotonic() - started), redact(diagnostic) if diagnostic else None)

    def _safe_version(self) -> str | None:
        try:
            return self._version_provider(self.config.codex_command, self.startup_timeout_seconds)
        except Exception:
            return None

    @staticmethod
    def _default_version(command: tuple[str, ...], timeout: float) -> str | None:
        completed = subprocess.run([*command, "--version"], capture_output=True, text=True, timeout=timeout, check=False)
        value = (completed.stdout or completed.stderr).strip()
        return value[:200] or None


class _ProbeRpc:
    def __init__(self, process: Any, timeout: float, monotonic: Callable[[], float]) -> None:
        self.process, self.timeout, self.monotonic, self.next_id = process, timeout, monotonic, 1
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        try:
            for line in self.process.stdout:
                self.lines.put(line)
        finally:
            self.lines.put(None)

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"method": method, "params": params})

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        self._send({"id": request_id, "method": method, "params": params})
        deadline = self.monotonic() + self.timeout
        while True:
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise TimeoutError(method)
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError(method) from exc
            if line is None:
                raise RuntimeError("Codex app-server exited before responding")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError(str(message["error"]))
            result = message.get("result")
            return result if isinstance(result, dict) else {"result": result}

    def _send(self, message: dict[str, Any]) -> None:
        if self.process.poll() is not None:
            raise RuntimeError("Codex app-server exited")
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()


def _command_succeeded(result: dict[str, Any]) -> bool:
    value = result.get("exitCode", result.get("exit_code", result.get("status")))
    return value in (None, 0, "completed", "success") and result.get("success", True) is not False


def _reap(process: Any) -> None:
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
    except Exception:
        pass
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(process, stream_name, None)
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass


def main() -> int:
    """Run the production no-model canary once for image and operator checks."""
    config = BrokerConfig.from_env()
    result = SandboxProbe(config).run_once()
    print(json.dumps(result.public(), sort_keys=True))
    if result.status != "healthy":
        if result.admin_diagnostic:
            print(result.admin_diagnostic, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
