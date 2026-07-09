from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import BrokerConfig
from .runtime_errors import CODEX_AUTH_REQUIRES_ADMIN, classify_runtime_error
from .state import StateStore
from .util import clean_process_env, ensure_dir, env_with, owner_digest, redact, utc_now


ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
URL_RE = re.compile(r"https?://[^\s\"'<>]+")
STRICT_CODE_RE = re.compile(r"\b([A-Z0-9]{4,8}-[A-Z0-9]{4,8}(?:-[A-Z0-9]{4,8})?)\b")
CONTEXT_CODE_RE = re.compile(r"\b([A-Z0-9]{6,12})\b")
EXPIRES_IN_RE = re.compile(r"\bexpires?\s+in\s+(\d+)\s+(seconds?|minutes?|hours?)\b", re.I)
PROFILE_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")
AUTH_PROBE_PROMPT = "Reply exactly: OK"


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def extract_login_url(text: str) -> str | None:
    match = URL_RE.search(strip_ansi(text))
    return match.group(0).rstrip("),.;") if match else None


def extract_user_code(text: str) -> str | None:
    lines = [line.strip() for line in strip_ansi(text).replace("\r", "\n").splitlines() if line.strip()]
    for line in lines:
        for match in STRICT_CODE_RE.finditer(line):
            token = match.group(1)
            if any(char.isdigit() for char in token):
                return token
    for idx, line in enumerate(lines):
        if not re.search(r"(login|verification|one-time|user|device)\s+code", line, re.I):
            continue
        context = "\n".join(lines[idx : idx + 3])
        for match in CONTEXT_CODE_RE.finditer(context):
            token = match.group(1)
            if any(char.isdigit() for char in token):
                return token
    return None


def extract_expires_at(text: str, *, now: datetime | None = None) -> str | None:
    match = EXPIRES_IN_RE.search(strip_ansi(text))
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    base = now or datetime.now(timezone.utc)
    if unit.startswith("second"):
        delta = timedelta(seconds=amount)
    elif unit.startswith("minute"):
        delta = timedelta(minutes=amount)
    else:
        delta = timedelta(hours=amount)
    return (base + delta).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_profile(profile: str | None = None) -> str:
    text = str(profile or "default").strip()
    return PROFILE_SAFE_RE.sub("_", text) or "default"


@dataclass
class DeviceAuthSession:
    session_id: str
    owner_hash: str
    profile: str
    command: list[str]
    started_at: str
    updated_at: str
    state: str = "starting"
    completed_at: str | None = None
    login_url: str | None = None
    user_code: str | None = None
    expires_at: str | None = None
    output: list[str] = field(default_factory=list)
    exit_code: int | None = None
    error: str | None = None
    process: subprocess.Popen[str] | None = None

    def public(self) -> dict[str, Any]:
        data = {
            "sessionId": self.session_id,
            "state": self.state,
            "profile": self.profile,
            "command": self.command,
            "startedAt": self.started_at,
            "updatedAt": self.updated_at,
            "completedAt": self.completed_at,
            "loginUrl": self.login_url,
            "userCode": self.user_code,
            "expiresAt": self.expires_at,
            "output": [] if self.state == "completed" else self.output[-80:],
            "exitCode": self.exit_code,
            "error": self.error,
        }
        if self.state == "completed":
            data["loginUrl"] = None
            data["userCode"] = None
            data["expiresAt"] = None
        return data


class AuthManager:
    def __init__(self, config: BrokerConfig, state: StateStore) -> None:
        self.config = config
        self.state = state
        self._sessions: dict[tuple[str, str], DeviceAuthSession] = {}
        self._lock = threading.RLock()

    def hash_owner(self, owner_id: str) -> str:
        return owner_digest(owner_id, self.config.owner_hash_secret)

    def profile_key(self, profile: str | None = None) -> str:
        return normalize_profile(profile)

    def profile_home(self, owner_hash: str, profile: str = "default") -> Path:
        profile_key = self.profile_key(profile)
        home = self.config.auth_root / owner_hash / "profiles" / profile_key / "codex-home"
        ensure_dir(home)
        self._ensure_config(home)
        self.state.ensure_profile(owner_hash, profile_key)
        return home

    def codex_env(self, owner_hash: str, profile: str = "default") -> dict[str, str]:
        profile_key = self.profile_key(profile)
        home = self.profile_home(owner_hash, profile_key)
        return env_with(
            clean_process_env(),
            {
                "CODEX_HOME": str(home),
                "CODEX_CREDENTIAL_STORE": self.config.credential_store,
                "HOME": str(home.parent),
            },
        )

    def auth_file(self, owner_hash: str, profile: str = "default") -> Path:
        profile_key = self.profile_key(profile)
        return self.profile_home(owner_hash, profile_key) / "auth.json"

    def auth_fingerprint(self, owner_hash: str, profile: str = "default") -> str:
        auth_file = self.auth_file(owner_hash, profile)
        if not auth_file.exists():
            return "missing"
        try:
            digest = hashlib.sha256(auth_file.read_bytes()).hexdigest()
            stat = auth_file.stat()
        except OSError:
            return "unreadable"
        return f"sha256:{digest}:size:{stat.st_size}"

    def mark_runtime_auth_failure(
        self,
        owner_hash: str,
        profile: str,
        *,
        code: str,
        admin_message: str,
    ) -> None:
        profile_key = self.profile_key(profile)
        fingerprint = self.auth_fingerprint(owner_hash, profile_key)
        self.state.update_auth_status(owner_hash, profile_key, "refresh_failed", auth_fingerprint=fingerprint)
        self.state.append_audit(
            owner_hash,
            "auth.runtime.failure",
            {"code": code, "authFingerprint": fingerprint, "adminMessage": redact(admin_message, 1200)},
            profile=profile_key,
        )

    def status(self, owner_id: str, profile: str = "default") -> dict[str, Any]:
        profile_key = self.profile_key(profile)
        owner_hash = self.hash_owner(owner_id)
        home = self.profile_home(owner_hash, profile_key)
        session = self._session(owner_hash, profile_key)
        auth_file_exists = (home / "auth.json").exists()
        fingerprint = self.auth_fingerprint(owner_hash, profile_key)
        profile_row = self.state.get_profile(owner_hash, profile_key)
        remembered_state = str(profile_row.get("auth_status")) if profile_row else "unknown"
        remembered_fingerprint = str(profile_row.get("auth_fingerprint")) if profile_row and profile_row.get("auth_fingerprint") else None
        state = "missing" if not auth_file_exists else "present_unverified"
        output = ""
        exit_code: int | None = None
        command = [*self.config.codex_command, "login", "status"]
        if shutil.which(self.config.codex_command[0]):
            try:
                result = subprocess.run(
                    command,
                    cwd=str(home),
                    env=self.codex_env(owner_hash, profile_key),
                    text=True,
                    input="",
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                output = redact(f"{result.stdout}\n{result.stderr}".strip(), 1200)
                exit_code = result.returncode
                normalized = strip_ansi(output).lower()
                if result.returncode == 0 and "not logged in" not in normalized and "not authenticated" not in normalized:
                    if "logged in" in normalized or "authenticated" in normalized or auth_file_exists:
                        state = "authenticated"
                elif auth_file_exists:
                    state = "invalid"
                else:
                    state = "missing"
            except (OSError, subprocess.SubprocessError) as exc:
                output = redact(str(exc), 1200)
                state = "missing" if not auth_file_exists else "present_unverified"
        auth_file_exists = (home / "auth.json").exists()
        fingerprint = self.auth_fingerprint(owner_hash, profile_key)
        if remembered_state == "refresh_failed" and remembered_fingerprint == fingerprint:
            state = "refresh_failed"
        self.state.update_auth_status(owner_hash, profile_key, state, auth_fingerprint=fingerprint)
        return {
            "ownerHash": owner_hash,
            "profile": profile_key,
            "state": state,
            "deviceAuth": session.public() if session else None,
            "authFilePresent": auth_file_exists,
            "authFingerprint": fingerprint,
            "loginStatusExitCode": exit_code,
            "loginStatusOutput": output,
        }

    def probe(self, owner_id: str, profile: str = "default") -> dict[str, Any]:
        profile_key = self.profile_key(profile)
        owner_hash = self.hash_owner(owner_id)
        home = self.profile_home(owner_hash, profile_key)
        auth_file = home / "auth.json"
        auth_file_exists = auth_file.exists()
        previous_fingerprint = self.auth_fingerprint(owner_hash, profile_key)
        started_at = utc_now()
        started_monotonic = time.monotonic()
        command = self._probe_command(home)
        self.state.append_audit(
            owner_hash,
            "auth.probe.start",
            {"authFingerprint": previous_fingerprint},
            profile=profile_key,
        )

        output = ""
        exit_code: int | None = None
        error_code: str | None = None
        public_message: str | None = None
        admin_message: str | None = None
        if not auth_file_exists:
            state = "missing"
            self.state.update_auth_status(owner_hash, profile_key, state, auth_fingerprint=previous_fingerprint)
        else:
            try:
                timeout = max(5.0, min(float(self.config.request_timeout_seconds), 120.0))
                result = subprocess.run(
                    command,
                    cwd=str(home),
                    env=self.codex_env(owner_hash, profile_key),
                    input=f"{AUTH_PROBE_PROMPT}\n",
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                )
                exit_code = result.returncode
                output = redact(f"{result.stdout}\n{result.stderr}".strip(), 2000)
            except (OSError, subprocess.SubprocessError) as exc:
                exit_code = -1
                output = redact(str(exc), 2000)

            normalized = strip_ansi(output).lower()
            error_info = classify_runtime_error(output) if output else None
            if error_info and error_info.code == CODEX_AUTH_REQUIRES_ADMIN:
                state = "refresh_failed"
                error_code = error_info.code
                public_message = error_info.public_message
                admin_message = error_info.admin_message
                self.mark_runtime_auth_failure(
                    owner_hash,
                    profile_key,
                    code=error_info.code,
                    admin_message=error_info.admin_message,
                )
            elif exit_code == 0:
                state = "authenticated"
                self.state.update_auth_status(
                    owner_hash,
                    profile_key,
                    state,
                    auth_fingerprint=self.auth_fingerprint(owner_hash, profile_key),
                )
            elif "not logged in" in normalized or "not authenticated" in normalized:
                state = "invalid" if auth_file.exists() else "missing"
                self.state.update_auth_status(
                    owner_hash,
                    profile_key,
                    state,
                    auth_fingerprint=self.auth_fingerprint(owner_hash, profile_key),
                )
            else:
                state = "failed"
                if error_info:
                    error_code = error_info.code
                    public_message = error_info.public_message
                    admin_message = error_info.admin_message
                self.state.update_auth_status(
                    owner_hash,
                    profile_key,
                    state,
                    auth_fingerprint=self.auth_fingerprint(owner_hash, profile_key),
                )

        completed_at = utc_now()
        fingerprint = self.auth_fingerprint(owner_hash, profile_key)
        audit_payload = {
            "state": state,
            "exitCode": exit_code,
            "errorCode": error_code,
            "previousAuthFingerprint": previous_fingerprint,
            "authFingerprint": fingerprint,
        }
        self.state.append_audit(
            owner_hash,
            "auth.probe.success" if state == "authenticated" else "auth.probe.failure",
            audit_payload,
            profile=profile_key,
        )
        return {
            "ownerHash": owner_hash,
            "profile": profile_key,
            "state": state,
            "authFilePresent": auth_file.exists(),
            "authFingerprint": fingerprint,
            "previousAuthFingerprint": previous_fingerprint,
            "command": command,
            "startedAt": started_at,
            "completedAt": completed_at,
            "durationMs": round((time.monotonic() - started_monotonic) * 1000, 3),
            "exitCode": exit_code,
            "output": output,
            "errorCode": error_code,
            "publicMessage": public_message,
            "adminMessage": admin_message,
        }

    def start_device_auth(self, owner_id: str, profile: str = "default") -> dict[str, Any]:
        profile_key = self.profile_key(profile)
        owner_hash = self.hash_owner(owner_id)
        key = (owner_hash, profile_key)
        with self._lock:
            existing = self._sessions.get(key)
            if existing and existing.process and existing.process.poll() is None:
                return existing.public()
            session = DeviceAuthSession(
                session_id=f"codex-auth-{owner_hash[:12]}-{int(threading.get_native_id())}",
                owner_hash=owner_hash,
                profile=profile_key,
                command=[*self.config.codex_command, "login", "--device-auth"],
                started_at=utc_now(),
                updated_at=utc_now(),
            )
            self._sessions[key] = session
            self.state.append_audit(owner_hash, "auth.device.start", {"sessionId": session.session_id}, profile=profile_key)
            try:
                self._spawn_device_auth(session)
            except (OSError, subprocess.SubprocessError) as exc:
                error = redact(str(exc), 1200)
                session.state = "failed"
                session.error = error
                session.output.append(error)
                session.exit_code = -1
                session.completed_at = utc_now()
                session.updated_at = session.completed_at
                self.state.update_auth_status(owner_hash, profile_key, "failed", "chatgpt")
                self.state.append_audit(
                    owner_hash,
                    "auth.device.failure",
                    {"sessionId": session.session_id, "exitCode": -1},
                    profile=profile_key,
                )
            return session.public()

    def submit_device_code(self, owner_id: str, code: str, profile: str = "default", session_id: str | None = None) -> dict[str, Any]:
        profile_key = self.profile_key(profile)
        owner_hash = self.hash_owner(owner_id)
        session = self._session(owner_hash, profile_key)
        if not session or not session.process or session.process.poll() is not None:
            raise ValueError("No active Codex device-auth session.")
        if session_id and session.session_id != session_id:
            raise ValueError("Codex device-auth session id does not match the active session.")
        if not code.strip():
            raise ValueError("Login code is required.")
        assert session.process.stdin is not None
        session.process.stdin.write(f"{code.strip()}\n")
        session.process.stdin.flush()
        session.state = "submitting_code"
        session.updated_at = utc_now()
        return session.public()

    def login_api_key(self, owner_id: str, api_key: str, profile: str = "default") -> dict[str, Any]:
        if not api_key.strip():
            raise ValueError("apiKey is required.")
        profile_key = self.profile_key(profile)
        owner_hash = self.hash_owner(owner_id)
        home = self.profile_home(owner_hash, profile_key)
        command = [*self.config.codex_command, "login", "--with-api-key"]
        self.state.append_audit(owner_hash, "auth.api_key.start", {}, profile=profile_key)
        try:
            result = subprocess.run(
                command,
                cwd=str(home),
                env=self.codex_env(owner_hash, profile_key),
                input=f"{api_key.strip()}\n",
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            exit_code = result.returncode
            output = redact(f"{result.stdout}\n{result.stderr}".strip(), 1200)
        except (OSError, subprocess.SubprocessError) as exc:
            exit_code = -1
            output = redact(str(exc), 1200)
        status = "authenticated" if exit_code == 0 else "failed"
        self.state.update_auth_status(owner_hash, profile_key, status, "api-key", self.auth_fingerprint(owner_hash, profile_key))
        self.state.append_audit(
            owner_hash,
            "auth.api_key.success" if status == "authenticated" else "auth.api_key.failure",
            {"exitCode": exit_code},
            profile=profile_key,
        )
        return {
            "ownerHash": owner_hash,
            "profile": profile_key,
            "state": status,
            "authFingerprint": self.auth_fingerprint(owner_hash, profile_key),
            "exitCode": exit_code,
            "output": output,
        }

    def logout(self, owner_id: str, profile: str = "default", *, delete_profile: bool = False) -> dict[str, Any]:
        profile_key = self.profile_key(profile)
        owner_hash = self.hash_owner(owner_id)
        home = self.profile_home(owner_hash, profile_key)
        session = self._session(owner_hash, profile_key)
        if session and session.process and session.process.poll() is None:
            session.process.terminate()
        try:
            result = subprocess.run(
                [*self.config.codex_command, "logout"],
                cwd=str(home),
                env=self.codex_env(owner_hash, profile_key),
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )
            exit_code = result.returncode
            output = redact(f"{result.stdout}\n{result.stderr}".strip(), 1200)
        except (OSError, subprocess.SubprocessError) as exc:
            exit_code = -1
            output = redact(str(exc), 1200)
        auth_file = home / "auth.json"
        if auth_file.exists():
            auth_file.unlink()
        deleted = False
        if delete_profile:
            shutil.rmtree(home.parent, ignore_errors=True)
            self.state.delete_profile(owner_hash, profile_key)
            self.state.append_audit(owner_hash, "auth.profile.delete", {"exitCode": exit_code}, profile=profile_key)
            deleted = True
        else:
            self.state.update_auth_status(owner_hash, profile_key, "missing", auth_fingerprint=self.auth_fingerprint(owner_hash, profile_key))
        self.state.append_audit(owner_hash, "auth.logout", {"exitCode": exit_code, "deleteProfile": delete_profile}, profile=profile_key)
        return {
            "ownerHash": owner_hash,
            "profile": profile_key,
            "state": "deleted" if deleted else "unauthenticated",
            "deleted": deleted,
            "exitCode": exit_code,
            "output": output,
        }

    def _session(self, owner_hash: str, profile: str) -> DeviceAuthSession | None:
        with self._lock:
            return self._sessions.get((owner_hash, profile))

    def _ensure_config(self, home: Path) -> None:
        config_path = home / "config.toml"
        desired = f'cli_auth_credentials_store = "{self.config.credential_store}"\n'
        if not config_path.exists() or config_path.read_text(encoding="utf-8") != desired:
            config_path.write_text(desired, encoding="utf-8")

    def _probe_command(self, home: Path) -> list[str]:
        return [
            *self.config.codex_command,
            "--ask-for-approval",
            "never",
            "exec",
            "-c",
            'model_reasoning_effort="low"',
            "--cd",
            str(home),
            "--skip-git-repo-check",
            "--ephemeral",
            "-s",
            "read-only",
            "--json",
            "-",
        ]

    def _spawn_device_auth(self, session: DeviceAuthSession) -> None:
        home = self.profile_home(session.owner_hash, session.profile)
        process = subprocess.Popen(
            session.command,
            cwd=str(home),
            env=self.codex_env(session.owner_hash, session.profile),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        session.process = process
        assert process.stdout is not None
        assert process.stderr is not None
        threading.Thread(target=self._read_auth_stream, args=(session, process.stdout), daemon=True).start()
        threading.Thread(target=self._read_auth_stream, args=(session, process.stderr), daemon=True).start()
        threading.Thread(target=self._wait_auth_process, args=(session,), daemon=True).start()

    def _read_auth_stream(self, session: DeviceAuthSession, stream: Any) -> None:
        try:
            for chunk in stream:
                lines = [line.strip() for line in strip_ansi(chunk).replace("\r", "\n").splitlines() if line.strip()]
                if not lines:
                    continue
                with self._lock:
                    session.output.extend(redact(line, 800) for line in lines)
                    session.output = session.output[-80:]
                    text = "\n".join(session.output)
                    session.login_url = extract_login_url(text) or session.login_url
                    session.user_code = extract_user_code(text) or session.user_code
                    session.expires_at = extract_expires_at(text) or session.expires_at
                    if session.state == "starting" and (session.login_url or session.user_code):
                        session.state = "waiting_for_login"
                    session.updated_at = utc_now()
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _wait_auth_process(self, session: DeviceAuthSession) -> None:
        assert session.process is not None
        code = session.process.wait()
        if session.process.stdin:
            try:
                session.process.stdin.close()
            except OSError:
                pass
        with self._lock:
            session.exit_code = code
            session.completed_at = utc_now()
            session.updated_at = session.completed_at
            if code == 0:
                session.state = "completed"
                session.error = None
                self.state.update_auth_status(
                    session.owner_hash,
                    session.profile,
                    "authenticated",
                    "chatgpt",
                    self.auth_fingerprint(session.owner_hash, session.profile),
                )
                self.state.append_audit(
                    session.owner_hash,
                    "auth.device.success",
                    {"sessionId": session.session_id},
                    profile=session.profile,
                )
            else:
                session.state = "failed"
                session.error = session.output[-1] if session.output else f"codex login exited with {code}"
                self.state.update_auth_status(session.owner_hash, session.profile, "failed", "chatgpt")
                self.state.append_audit(
                    session.owner_hash,
                    "auth.device.failure",
                    {"sessionId": session.session_id, "exitCode": code},
                    profile=session.profile,
                )
