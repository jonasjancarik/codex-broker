from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from codex_broker import __version__


class ReleaseMetadataTests(unittest.TestCase):
    def test_documented_codex_default_matches_dockerfile(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        dockerfile = (repository / "Dockerfile").read_text(encoding="utf-8")
        match = re.search(r"^ARG CODEX_VERSION=([^\s]+)$", dockerfile, re.MULTILINE)
        self.assertIsNotNone(match)
        assert match is not None
        version = match.group(1)

        for relative_path in (
            "fern/docs/pages/operations/deployment.mdx",
            "fern/docs/pages/operations/configuration-reference.mdx",
        ):
            with self.subTest(relative_path=relative_path):
                documentation = (repository / relative_path).read_text(encoding="utf-8")
                self.assertIn(f"| `CODEX_VERSION` | `{version}` |", documentation)

    def test_generated_openapi_version_matches_package_version(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        openapi = json.loads((repository / "fern/openapi/openapi.json").read_text(encoding="utf-8"))
        self.assertEqual(openapi["info"]["version"], __version__)

    def test_example_uses_the_documented_codex_and_host_security_profile(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        dockerfile = (repository / "Dockerfile").read_text(encoding="utf-8")
        match = re.search(r"^ARG CODEX_VERSION=([^\s]+)$", dockerfile, re.MULTILINE)
        self.assertIsNotNone(match)
        assert match is not None

        compose = (repository / "examples/docker-compose.yml").read_text(encoding="utf-8")
        local_compose = (repository / "examples/docker-compose.local.yml").read_text(encoding="utf-8")
        self.assertIn(f'CODEX_VERSION: "{match.group(1)}"', compose)
        self.assertIn("seccomp=/etc/codex-broker/security/v1/seccomp.json", compose)
        self.assertIn("no-new-privileges:true", compose)
        self.assertNotIn("../..:/workspaces", compose)
        self.assertIn("./workspace:/workspaces/app:rw", compose)
        self.assertIn('"127.0.0.1:3400:3400"', local_compose)

    def test_host_security_profiles_preserve_moby_hardening(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        seccomp = json.loads((repository / "examples/seccomp/codex-broker.json").read_text(encoding="utf-8"))
        apparmor = (repository / "examples/apparmor/codex-broker-bwrap").read_text(encoding="utf-8")

        self.assertIn("Moby seccomp/v0.2.3 baseline", seccomp["comment"])
        self.assertIn("upstream SHA-256", seccomp["comment"])
        architectures = {entry["architecture"] for entry in seccomp["archMap"]}
        self.assertTrue({"SCMP_ARCH_X86_64", "SCMP_ARCH_AARCH64", "SCMP_ARCH_LOONGARCH64"} <= architectures)
        self.assertIn("apparmor/v0.2.1", apparmor)
        self.assertIn("SHA-256", apparmor)
        self.assertIn("abi <abi/3.0>,", apparmor)
        self.assertIn("deny network alg,", apparmor)

        socket_rules = [
            rule
            for rule in seccomp["syscalls"]
            if rule["names"] == ["socket"] and rule["action"] == "SCMP_ACT_ALLOW"
        ]
        self.assertEqual(
            [(rule["args"][0]["op"], rule["args"][0]["value"]) for rule in socket_rules],
            [("SCMP_CMP_LT", 38), ("SCMP_CMP_EQ", 39), ("SCMP_CMP_GT", 40)],
        )
        socketcall_rules = [rule for rule in seccomp["syscalls"] if "socketcall" in rule["names"]]
        self.assertEqual(
            socketcall_rules,
            [
                {
                    "names": ["socketcall"],
                    "action": "SCMP_ACT_ERRNO",
                    "errnoRet": 38,
                    "comment": (
                        "Deny the legacy socketcall multiplexer. Seccomp cannot inspect its pointed-to "
                        "address-family argument, so allowing it would bypass the AF_ALG and AF_VSOCK "
                        "socket rules on compatibility architectures. ENOSYS prevents libseccomp from "
                        "generating a socketcall allow companion."
                    ),
                }
            ],
        )

    def test_host_profile_installer_is_syntax_checked_and_executable(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        installer = repository / "scripts/install-host-security-profiles.sh"
        self.assertTrue(installer.is_file())
        self.assertTrue(installer.stat().st_mode & 0o111)
        subprocess.run(["sh", "-n", str(installer)], check=True)
        installer_source = installer.read_text(encoding="utf-8")
        self.assertIn("/sys/module/apparmor/parameters/enabled", installer_source)
        self.assertIn("rerun --check with sudo", installer_source)
        self.assertIn("loaded in complain mode; enforcement is required", installer_source)
        self.assertNotIn('|| grep -Fqx "codex-broker-bwrap (complain)"', installer_source)
        self.assertIn('"$parser" -Q -K "$APPARMOR_SOURCE"', installer_source)
        self.assertNotIn('"$parser" -Q -W "$APPARMOR_SOURCE"', installer_source)

        workflow = (repository / ".github/workflows/docker-publish.yml").read_text(encoding="utf-8")
        self.assertIn("sudo ./scripts/install-host-security-profiles.sh --check", workflow)

    def test_host_profile_installer_fails_without_a_checksum_utility(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        installer = repository / "scripts/install-host-security-profiles.sh"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            for command in ("dirname", "grep", "python3"):
                executable = shutil.which(command, path="/usr/bin:/bin")
                self.assertIsNotNone(executable)
                (bin_dir / command).symlink_to(str(executable))

            security_root = root / "security"
            installed_seccomp = security_root / "v1" / "seccomp.json"
            installed_seccomp.parent.mkdir(parents=True)
            installed_seccomp.write_bytes((repository / "examples/seccomp/codex-broker.json").read_bytes())
            environment = {
                **os.environ,
                "PATH": str(bin_dir),
                "CODEX_BROKER_SECURITY_ROOT": str(security_root),
                "CODEX_BROKER_APPARMOR_PATH": str(root / "apparmor-profile"),
            }
            completed = subprocess.run(
                [str(installer), "--check"],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("sha256sum or shasum is required", completed.stderr)


if __name__ == "__main__":
    unittest.main()
